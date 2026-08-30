# SAMANVAY — frozen contracts

SIH26166. Verified against the code on 2026-08-29.

Ten people write this repo in parallel. The only thing that makes that survivable is that
the seams between modules are written down, and that nobody changes one without telling
everyone. This file is that written-down seam. If the code and this file disagree, **the
code is right and this file is stale** — fix it in the same commit that made it stale.

---

## 0. The conventions (see `docs/decisions.md` D1)

These are frozen repo-wide and restated at the top of `samanvay/types.py`. Violating one
is the most expensive class of bug in this project, because the result stays finite and
plausible.

| rule | value |
|---|---|
| coordinate order | `(x, y) = (column, row)`, floating point |
| pixel grid | pixel **centre** at integer coordinates |
| transform direction | every transform maps **SOURCE → REFERENCE** |
| transform type | `3x3 float64` homogeneous matrix |
| residual frame | **SOURCE pixels**, always: `residual = H^-1(ref_xy) - src_xy` |
| RMSE definition | 2-D point RMSE, `sqrt(mean(dx^2 + dy^2))` — not per-component RMS |
| mask codes | `0 = valid`, `1 = shadow`, `2 = nodata`, `3 = saturated` |
| unknown quantity | `null` / `NaN` / an explicit state string — **never** a plausible default |

Two consequences worth stating separately, because they have already caused bugs:

- **GDAL geotransforms are corner-based.** `geometry/init.py` evaluates them at
  `(x + 0.5, y + 0.5)` to reach the pixel-centre convention. That half-pixel does not
  cancel when the two products have different GSDs.
- **OpenCV's RANSAC threshold is a forward reprojection error in reference pixels.**
  `config["ransac_thresh_px"]` is in source pixels like everything else, and
  `verify_matches` converts it using the scale implied by the init. Any new estimator must
  do the same.

---

## 1. The four frozen dataclasses

Defined in `samanvay/types.py`. **This file is frozen: do not edit it without the protocol
in section 5.**

### `Product` — produced by I/O, consumed by everyone

```python
@dataclass
class Product:
    path: str
    array: np.ndarray     # band 1, native dtype — or a TiledReader for products > 512 MiB
    meta: dict
```

`meta` keys, all optional and **`None` when unknown** (a missing sun angle stays missing;
it is never replaced by a plausible value):

`product_id`, `instrument`, `gsd_m`, `sun_az_deg`, `sun_el_deg`, `incidence_deg`,
`emission_deg`, `geotransform` (GDAL 6-tuple), `crs`, `shape`, `dtype`, `nodata`,
`meta_source`.

### `CanonicalImage` — produced by photometry, consumed by matching

```python
@dataclass
class CanonicalImage:
    albedo:    np.ndarray   # float32 in [0,1], illumination divided out
    pc:        np.ndarray   # float32 in [0,1], phase congruency
    pc_orient: np.ndarray   # float32, dominant orientation, radians
    mask:      np.ndarray   # uint8: 0=valid 1=shadow 2=nodata 3=saturated
    params:    dict         # everything needed to reproduce this exactly
```

`params` always carries `illum_mode` ∈ `{"dem", "dem_lowfreq", "empirical"}` and
`pc_status` ∈ `{"computed", "disabled", "unavailable"}`. `albedo` is forced to `0.0`
wherever `mask != 0`.

### `MatchSet` — produced by matching, consumed by geometry and the report

```python
@dataclass
class MatchSet:
    src_xy: np.ndarray    # (N,2) float64, source pixels
    ref_xy: np.ndarray    # (N,2) float64, reference pixels
    score:  np.ndarray    # (N,) float32
    method: np.ndarray    # (N,) uint8 — 0=sift(L0) 1=orb(L0) 2=phase-congruency(L2)
    cell:   np.ndarray    # (N,) int32 — uniformity cell id, col + grid_n*row
```

All five arrays have the same length `N`. `N == 0` is a legal, expected value and every
consumer must handle it without raising.

### `Registration` — produced by geometry, consumed by the warp, writers and the report

```python
@dataclass
class Registration:
    model_type:  str          # "similarity" | "affine" | "homography" | "failed"
    params:      np.ndarray   # 3x3 float64, source -> reference
    init_params: np.ndarray   # 3x3 float64, the coarse init it started from
    inliers:     np.ndarray   # (N,) bool
    residuals:   np.ndarray   # (N,2) float64, SOURCE pixels
    sigma:       np.ndarray   # (N,) float64, per-point uncertainty, source pixels
    metrics:     dict
```

A failed fit is `model_type="failed"`, `params` = the init (or identity), all-`False`
inliers, `NaN` residuals, and `metrics["status"] = "failed"` with a `reason` string. It is
**never** an exception and never a zero RMSE.

`metrics` keys guaranteed by `geometry/metrics.compute_metrics` (the metrics.json
contract; any of them may be `null` when genuinely unknown):

`rmse_px`, `inlier_count`, `inlier_ratio`, `coverage_pct`, `dispersion_cv`, `grid_n`,
`runtime_s`, `match_count`, `mean_sigma_px`, `refined_count`, `model_type`,
`model_margin`, `cell_counts`, `cell_states`, `gt_rmse_px`, `gt_bias_x`, `gt_bias_y`,
`gt_p90_px`.

`verify_matches` additionally reports `status`, `residual_units` (`"source_px"`),
`ransac_thresh_px`, `scale_src_to_ref`, `model_candidates`, `model_common_set`,
`init_used`, `init_gate_px`, `init_gated_out`.

---

## 2. Public API, by module

Signatures below are the **current code**, read on 2026-08-29. Call them as written; do
not reimplement another seat's function.

### `samanvay/io` — loading and provenance

```python
load_product(path: str, max_bytes: int = None) -> Product
    # Open a raster as a Product: band 1 as an ndarray, or a TiledReader when it is large.

read_metadata(path: str) -> dict
    # Collect raw metadata for a product, keyed by source: sidecar JSON, PDS label, GeoTIFF.

normalise_meta(raw: dict, path: str) -> dict
    # Fold raw mission metadata into the canonical Product.meta; anything absent stays None.

write_outputs(out_dir, source, reference, registration, matches,
              registered_array, config) -> None
    # Write the six run artifacts. Falls back to a minimal report.html if report/ is absent.
```

### `samanvay/core` — tiling and cache

```python
class TiledReader:      # .shape .dtype .read_window(y0,y1,x0,x1) .read_all() .close()
class ArrayReader:      # same interface, backed by an in-memory ndarray
open_reader(path)                        # path, ndarray, or reader -> one interface
iter_tiles(shape, tile=512, halo=64)     # dicts: core_y0/y1/x0/x1, halo_y0/y1/x0/x1

cache_key(product_id: str, params: dict) -> str      # stable hex digest across processes
cache_load(key, cache_dir=DEFAULT_CACHE_DIR)         # dict of arrays, or None
cache_store(key, arrays: dict, cache_dir=DEFAULT_CACHE_DIR)   # atomic write
```

### `samanvay/photometry` — illumination canonicalisation

```python
canonicalise(product: Product, params: dict = None) -> CanonicalImage
    # Divide out predicted illumination; returns albedo + phase congruency + mask.
    # params: dem_path, photometric_model, phase_congruency, epsilon, smooth_sigma,
    #         nscale, norient, lambert_weight, nodata_value, dem_gsd_ratio_max (4.0)

build_mask(array, illum=None, shadow=None, nodata_value=None, dark_frac=0.02) -> np.uint8
MASK_VALID, MASK_SHADOW, MASK_NODATA, MASK_SATURATED = 0, 1, 2, 3

sun_vector(sun_az_deg, sun_el_deg)
surface_normals(dem, gsd_m)                                  # (H,W,3) float32, unit
cos_incidence(normals, sun_az_deg, sun_el_deg)               # (H,W) float32
cast_shadow_mask(dem, gsd_m, sun_az_deg, sun_el_deg, max_steps=None)   # (H,W) bool
lommel_seeliger(cos_i, cos_e, lambert_weight=0.0)            # (H,W) float32
predicted_illumination(dem, gsd_m, meta, model="lommel_seeliger",
                       lambert_weight=0.0, max_steps=None)   # (H,W) float32 in (0,1]

phase_congruency(img, nscale=4, norient=6, min_wavelength=3.0, mult=2.1,
                 sigma_onf=0.55, k=2.0, cut_off=0.5, g=10.0) -> dict
    # {"pc": float32 (H,W) in [0,1], "orientation": float32 radians, "mim": uint8}
```

### `samanvay/match` — detection, description, tiled matching

```python
detect_keypoints(img: np.ndarray, method="sift", pc_map=None) -> list[cv2.KeyPoint]
    # method in {"sift", "orb", "l2"}; "l2" peaks the phase-congruency map.

describe_keypoints(img, kps, method="sift", pc_orient=None) -> (list[cv2.KeyPoint], np.ndarray)

match_images(source: CanonicalImage, reference: CanonicalImage,
             config: dict = None) -> MatchSet
    # Whole-image path. No uniformity guarantee; match_tiled is the pipeline path.

match_tiled(source: CanonicalImage, reference: CanonicalImage, grid_n=4, halo_px=64,
            config=None, cell_budgets=None, init=None) -> tuple
    # RETURNS (MatchSet, cell_info) — a TUPLE, not a MatchSet. Callers must unpack.
    # `init` is the coarse source->reference matrix. WITHOUT it the matcher falls back to
    # same-grid tiling, logs a DEGRADED MODE warning, and assumes the pair already shares
    # a pixel grid and scale. Always pass an init.
    # cell_info: {"mode", "grid_n", "method", "search_margin_px", "cells": {id: {...}}}
    #   cell status in {"populated", "insufficient_texture", "masked_invalid",
    #                   "empty_tile", "no_reference_overlap"}
```

### `samanvay/geometry` — init, verification, refinement, metrics

```python
coarse_init(source: Product, reference: Product) -> np.ndarray
    # 3x3 float64 source->reference from metadata. Never raises.
    # Degradation ladder: "geotransform" -> "gsd_ratio" -> "identity".
coarse_init_info(source, reference) -> (np.ndarray, dict)   # which rung produced it
apply_transform(H: np.ndarray, xy: np.ndarray) -> np.ndarray            # (N,2) -> (N,2)
project_box(H, x0, y0, x1, y1) -> (x0, y0, x1, y1)          # bbox of projected corners

verify_matches(matches: MatchSet, config: dict = None,
               init: np.ndarray = None) -> Registration
    # Model ladder similarity -> affine -> homography; simplest within model_margin wins.
    # config: model ("auto"|"similarity"|"affine"|"homography"), model_margin (0.10),
    #         ransac_thresh_px (3.0, SOURCE px), init_gate_px, grid_n

refine_matches(matches: MatchSet, src_img, ref_img, H,
               config: dict = None) -> (MatchSet, np.ndarray)
    # Sub-pixel refine of ref_xy by correlating H-aligned patches.
    # sigma is (N,) float64 in SOURCE px; NaN for points it declined to move.

assign_cells(xy, shape, grid_n) -> np.ndarray                # (N,) int32
uniformity_report(matches, shape, grid_n, mask=None, inliers=None) -> dict
    # {"coverage_pct", "dispersion_cv", "grid_n", "counts", "cell_states"}
    # cell_states entries are exactly one of:
    #   "populated" | "insufficient_texture" | "masked_invalid"

compute_metrics(matches, registration, shape, grid_n=4, mask=None,
                gt_H=None, runtime_s=0.0) -> dict
    # The metrics.json contract. Owns coverage_pct and dispersion_cv — verify_matches
    # deliberately does not emit them, because a MatchSet carries neither the image
    # shape nor the validity mask and a placeholder would fabricate a headline number.
```

### `samanvay/pipeline` — orchestration (CLI entry point)

```python
run_pipeline(source_path: str, ref_path: str, out_dir: str, config: dict = None) -> None
    # CLI: samanvay register --source SRC --ref REF --out OUT
```

### `samanvay/report`

```python
render_report(out_dir, source, reference, registration, matches,
              registered_array, config) -> str      # returns path to report.html
```

`io/writers.write_outputs` imports this lazily and degrades to a minimal built-in report if
it is missing or raises, so a report failure never costs a run.

### `synth` — ground-truth fixtures (imports nothing from `samanvay`, deliberately)

```python
render_synthetic_pair(out_dir="fixtures/synth_pair_A", ref_shape=(1024,1024),
                      scale_ratio=2, ref_gsd_m=1.0, seed=0,
                      src_sun=(45.0,25.0), ref_sun=(135.0,65.0),
                      rot_deg=10.0, proj_strength=0.02, ...) -> dict
    # Writes source.tif, reference.tif, dem.tif, *.tif.json sidecars, gt.json, README.md
make_dem(shape=(2048,2048), gsd_m=0.5, seed=0, ...) -> (np.ndarray, float)

render_sweep(out_dir="fixtures/dsun_sweep", deltas=DEFAULT_DELTAS, ref_sun_az_deg=45.0,
             sun_el_deg=25.0, ref_shape=(512,512), seed=0) -> list
    # One pair per sun-azimuth delta into out_dir/dsun_NN, plus manifest.json:
    # the accuracy-vs-delta-sun curve. Run: python -m synth.sweep
```

### `bench` — sweeps and ablation

```python
run_one(name, source, reference, out_dir, config=None) -> dict   # a failure is a row, not an abort
run_manifest(manifest_path, out_root="runs/bench", config=None) -> list
write_table(rows, out_dir, stem="bench") -> (csv_path, md_path)
ablate(source, reference, out_root="runs/ablate", variants=None, config=None) -> list
```

Config keys the pipeline must honour for the ablation table to mean anything:
`photometry.canonicalise`, `photometry.phase_congruency`, `photometry.photometric_model`,
`geometry.subpixel`, `match.uniformity`, `match.method`. If every variant returns an
identical `rmse_px`, `ablate` warns that the pipeline is ignoring them — that warning means
the table proves nothing.

---

## 3. Run artifacts (the six files)

`write_outputs` writes exactly these into `--out`:

| file | contents |
|---|---|
| `registered.tif` | source warped onto the reference grid; reference CRS + geotransform, source dtype |
| `matches.csv` | `id, src_x, src_y, ref_x, ref_y, score, inlier, residual_px, sigma_px, cell` — residuals in SOURCE px; unknown fields are blank, never `0` |
| `transform.json` | `model_type`, `params` (3x3), `init_params`, `model_margin`, `rejected_models` |
| `metrics.json` | the `Registration.metrics` dict, section 1 |
| `provenance.json` | UTC timestamp, git SHA/branch/dirty, seed, full config, package versions, input paths + product ids |
| `report.html` | the visual report (or the minimal fallback) |

---

## 4. Cross-module invariants

Assert these; do not assume them.

1. All five `MatchSet` arrays have the same length. `N == 0` is legal everywhere.
2. `match_tiled` returns a **tuple**. `match_images` returns a bare `MatchSet`.
3. `verify_matches` does **not** set `coverage_pct` / `dispersion_cv`. `compute_metrics`
   does. A pipeline that skips `compute_metrics` produces an incomplete `metrics.json` and
   CI will fail on the missing keys.
4. `refine_matches` fills `sigma`; `verify_matches` leaves it as zeros. `NaN` sigma means
   "declined to refine", not "zero uncertainty".
5. `coarse_init` never raises and always returns an invertible 3x3. Pass it into **both**
   `match_tiled(init=...)` and `verify_matches(init=...)`.
6. Anything unknown is `None`/`NaN`/an explicit state string. Never substitute a default
   and let it flow into a metric.

---

## 5. Contract-change protocol

A contract change is any edit to `samanvay/types.py`, to the conventions in section 0, to
a public signature in section 2, or to the artifact set in section 3.

1. **Propose it.** Say what breaks and why the change is worth breaking it. Not a
   commit message — a message to the team, before the edit.
2. **Update `samanvay/types.py`** (or the owning module) once agreement exists. One
   author, one commit.
3. **Update every producer and every consumer of the changed type the same day.** Not
   "next sprint". A repo where two modules disagree about a dataclass is a repo where
   every downstream number is suspect and nobody knows which ones.
4. **Update this file and `docs/decisions.md` in the same commit.** A stale contract file
   is worse than none, because people trust it.
5. **CI must be green before and after.** "main is always green" is the rule that makes a
   ten-person parallel build possible; a contract change is exactly the moment it matters
   most.

If you need something another module owns, **import it by the signature above and trust it
exists.** Do not reimplement it, and do not create a placeholder file for it — a
placeholder that shadows a real module is a bug that takes hours to find.
