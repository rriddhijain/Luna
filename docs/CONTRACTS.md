# SAMANVAY — frozen contracts

SIH26166. Sections 0 and 4 verified against the code on 2026-08-29; sections 1, 2 and 3
re-verified on **2026-09-02** after the revamp, by reading each signature and by inspecting
a real `runs/demo_01`. Changes made under the section 5 protocol are marked **(2026-09-02)**.

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
    model_type:  str          # "similarity" | "affine" | "homography" | "failed",
                              #   optionally suffixed "+tps" when a spline was accepted
    params:      np.ndarray   # 3x3 float64, source -> reference — the GLOBAL model, always
    init_params: np.ndarray   # 3x3 float64, the coarse init it started from
    inliers:     np.ndarray   # (N,) bool
    residuals:   np.ndarray   # (N,2) float64, SOURCE pixels
    sigma:       np.ndarray   # (N,) float64, per-point uncertainty, source pixels
    metrics:     dict
    roles:       np.ndarray = None   # (2026-09-02) (N,) uint8: 0 = control, 1 = check
    warp:        object = None       # (2026-09-02) geometry.tps.ThinPlateSpline | None
```

**(2026-09-02) `roles`** is the control/check partition, filled for ALL N matches. `0` =
control (the fit saw it), `1` = check (held out entirely). Points dropped by the init gate
are control — they were never held out. `roles is None` means no split was made, and
`check_rmse_px` is then `null`, never optimistic.

**(2026-09-02) `warp`** is an optional non-rigid residual applied AFTER `params`, in the
SOURCE frame:

```python
src_predicted = warp.apply(inv(params) @ ref_xy)     # == geometry.tps.pullback(params, ref_xy, warp)
```

`params` stays a 3x3 and stays the global model, so **every consumer that reads `params`
keeps working unchanged**. Only code that wants the full model consults `warp`, and it
must do so through `geometry.tps.pullback` — that function is the single place the
global-then-spline order is defined. `None` means the registration is purely projective.

A failed fit is `model_type="failed"`, `params` = the init (or identity), all-`False`
inliers, `NaN` residuals, and `metrics["status"] = "failed"` with a `reason` string. It is
**never** an exception and never a zero RMSE.

`metrics` keys guaranteed by `geometry/metrics.compute_metrics` (the metrics.json
contract; any of them may be `null` when genuinely unknown):

`rmse_px`, `inlier_count`, `inlier_ratio`, `coverage_pct`, `dispersion_cv`, `grid_n`,
`runtime_s`, `match_count`, `mean_sigma_px`, `refined_count`, `model_type`,
`model_margin`, `cell_counts`, `cell_states`, `gt_rmse_px`, `gt_bias_x`, `gt_bias_y`,
`gt_p90_px`.

**(2026-09-02) New keys in `metrics.json`.** All verified present in `runs/demo_01` and
`runs/ci_smoke`. A `null` is legal and honest; a missing key is a contract break, and
`.github/workflows/ci.yml` gates on their presence.

| key | type | meaning |
|---|---|---|
| `check_rmse_px` | float\|null | **the number to quote.** RMSE over check points within the RANSAC threshold |
| `check_rmse_all_px` | float\|null | RMSE over ALL check points, no threshold — the ungamed figure |
| `check_p90_px` | float\|null | 90th percentile of check residual magnitude, all check points |
| `check_outlier_frac` | float\|null | `1 - n_check_inlier/n_check` |
| `n_check`, `n_control`, `n_check_inlier` | int | held-out / fitting / held-out-and-inside-threshold counts |
| `check_fraction` | float | what was requested (`geometry.check_fraction`, default 0.2) |
| `check_status` | str | `"ok"` \| `"skipped_too_few_matches"` \| `"disabled"` |
| `tps_applied` | bool | whether a spline is in the delivered model |
| `tps_status` | str | `"applied"` \| `"rejected_no_improvement"` \| `"too_few_control"` \| `"disabled"` \| `"singular"` |
| `tps_check_rmse_before_px`, `tps_check_rmse_after_px` | float\|null | the held-out comparison that decided it |
| `tps_n_control` | int | control inliers the spline was fitted on |
| `sdi` | float\|null | `(coverage_pct/100) * 1/(1 + dispersion_cv)`, in [0,1] |
| `sdi_definition` | str | that formula as a literal string |
| `grid_rows`, `grid_cols` | int | the N x M grid. `grid_n` still means the SHORT-axis count |
| `inlier_ratio_pass` | bool\|null | `inlier_ratio >= inlier_ratio_target` |
| `inlier_ratio_target` | float | 0.85, the plan's bar |
| `match_method_requested` / `_resolved` / `_recommended` / `_reason` | str | `"auto"` resolution, and why |
| `phase_congruency_requested` / `_resolved` / `_reason` | bool, bool, str | same, for the PC map |
| `clahe_resolved`, `clahe_applied`, `clahe_reason`, `clahe_clip`, `clahe_grid` | | same, for CLAHE |
| `delta_sun_az_deg` | float\|null | the circular difference that drove the resolution |
| `mask_fill`, `mask_fill_px` | str, int | which PC mask-boundary path ran, and how many pixels it filled |
| `verify_init_source` | str | which matrix verification was seeded from (e.g. `"cascade_transform"`) |
| `init_gate_px`, `init_gated_out`, `init_gate_fallback` | float, int, bool | the init gate, promised here since 2026-08-29 and now actually emitted |
| `seed`, `seed_applied`, `seed_applied_to`, `seed_reason` | | **`seed_applied` is `false`** with a reason unless a stage is genuinely seeded. `--seed` must not imply reproducibility control it does not have |
| `redundancy`, `rmse_trustworthy`, `rmse_warning` | int, bool, str\|null | when `rmse_px` is near-zero by construction, this says so in words |
| `output_grid`, `registered_source_written` | str, bool | which products were written |
| `stage_s` | dict | per-stage wall clock |

`verify_matches` additionally reports `status`, `residual_units` (`"source_px"`),
`ransac_thresh_px`, `scale_src_to_ref`, `model_candidates`, `model_common_set`,
`init_used`, `init_gate_px`, `init_gated_out`.

**Known gap (2026-09-02, unfixed).** `residual_units` is set by `verify_matches` but does
NOT survive into `metrics.json` — the pipeline's passthrough does not copy it. A consumer
that wants to assert the residual frame must read it off the `Registration`, not the file.
Likewise `Product.meta["band_reduction"]` and `Product.meta["read_decimation"]` (below) are
recorded on the `Product` but are not currently copied into `metrics.json` or
`provenance.json`. Both are pipeline-owned; both should be closed.

---

## 2. Public API, by module

Signatures below are the **current code**, re-read on 2026-09-02. Call them as written; do
not reimplement another seat's function. Every 2026-09-02 change below is additive with a
default — no existing call site breaks.

### `samanvay/io` — loading and provenance

```python
load_product(path: str, max_bytes: int = None, band_cfg: dict = None,
             max_pixels: int = None) -> Product                        # (2026-09-02)
    # Open a raster as a Product: ONE 2-D array, or a TiledReader when it is large.
    # band_cfg is config["band"] and decides how a multi-band cube is reduced to that
    #   array (see reduce_bands). None keeps the module defaults.
    # max_pixels caps the read: above it the raster is read decimated (rasterio
    #   out_shape) and shape / geotransform / gsd_m are corrected to match. OFF by
    #   default and deliberately so — a slow read is recoverable, a silently rescaled
    #   GSD is not — so a caller opts in with a budget it can defend.
    # Records Product.meta["band_reduction"] and Product.meta["read_decimation"] on
    #   every call, decimated or not.

read_metadata(path: str) -> dict
    # Collect raw metadata for a product, keyed by source: sidecar JSON, PDS label, GeoTIFF.

normalise_meta(raw: dict, path: str) -> dict
    # Fold raw mission metadata into the canonical Product.meta; anything absent stays None.

write_outputs(out_dir, source, reference, registration, matches,
              registered_array, config,
              registered_source=None) -> None                          # (2026-09-02)
    # Write the run artifacts. Falls back to a minimal report.html if report/ is absent.
    # registered_source is the same registration on the SOURCE pixel grid, produced by
    #   the pipeline stage. config["output"]["grid"] decides what is written:
    #     "reference" -> registered.tif on the reference grid (the pre-2026-09-02 path)
    #     "source"    -> registered.tif IS the source-grid product
    #     "both"      -> both, the second as registered_source_grid.tif   (the default)
    #   Missing array = the extra file is not written, no error. A caller that does not
    #   produce one loses the file, not the run.
```

### `samanvay/io/bands.py` — multi-band reduction (2026-09-02, NEW MODULE)

```python
reduce_bands(src, cfg=None, out_shape=None) -> tuple[np.ndarray, dict]
    # src is an OPEN rasterio dataset; cfg is config["band"]:
    #   index (None|int), reduce ("pc1"|"mean"|"band"), min_snr (2.0), max_bands (64)
    # out_shape is passed straight to the reads, so a caller already decimating at read
    #   time decimates this too rather than reducing at full resolution and discarding.
    # count == 1              -> read(1) unchanged; a single-band product behaves EXACTLY
    #                            as it did before this module existed.
    # index is not None       -> read(index), recorded.
    # "pc1"                   -> per-band SNR on a decimated read, drop below min_snr, cap
    #                            at max_bands evenly spaced, PCA on the centred band
    #                            matrix, FIRST principal component, sign-fixed to correlate
    #                            positively with the band mean.
    # "mean"                  -> mean of the surviving bands.
    # info always carries: n_bands, n_bands_used, n_bands_dropped_snr, reduce,
    #   explained_var_frac (None wherever no PCA ran, never 0.0), bands_used, min_snr,
    #   sign_flipped. Lands in Product.meta["band_reduction"].
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

anms_quadtree(xy, score, k, bbox, max_depth=8) -> np.ndarray   # (2026-09-02) match/anms.py
    # Indices of <= k points spread over bbox, the quota split evenly by quadrant, best
    # score per leaf. Indices INTO xy, sorted ascending, never duplicated.
    # k <= 0 or len(xy) <= k -> arange(len(xy)): a quota that does not bite must not
    # reorder or drop anything. Never raises; a degenerate bbox falls back to the plain
    # score sort, which is what the caller would have done anyway.
    # Wired into match/tile.py and match/cascade.py where the per-cell quota is applied,
    # gated on config["match"]["anms"] (default True) and recorded as cell["anms"].

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
    # Signature UNCHANGED. Behaviour extended 2026-09-02: it now also makes the
    # control/check split and fits/validates the TPS, and fills Registration.roles
    # and Registration.warp.
    # config: model ("auto"|"similarity"|"affine"|"homography"), model_margin (0.10),
    #         ransac_thresh_px (3.0, SOURCE px), init_gate_px, grid_n,
    #         check_fraction (0.2), tps ("auto"|True|False), tps_lambda (0.5),
    #         tps_min_control (25)
    # Order inside the function is fixed and load-bearing: init gate -> SPLIT ->
    # fit on control only -> TPS on control inliers -> accept/reject on check.

refine_matches(matches: MatchSet, src_img, ref_img, H,
               config: dict = None) -> (MatchSet, np.ndarray)
    # Sub-pixel refine of ref_xy by correlating H-aligned patches.
    # sigma is (N,) float64 in SOURCE px; NaN for points it declined to move.

grid_shape(shape, grid_n, aspect=True) -> (rows, cols)       # (2026-09-02)
    # THE only place the grid shape is computed. aspect=False -> (grid_n, grid_n).
    # aspect=True -> grid_n cells along the SHORT axis; the long axis is scaled by the
    # aspect ratio, rounded, floored at 1 and CAPPED AT 64 so a 1:200 strip cannot
    # explode the grid. On a square image this is byte-identical to the old square grid.
    # Cell id is unchanged: col + cols * row.

assign_cells(xy, shape, grid_n, aspect=True) -> np.ndarray   # (N,) int32
uniformity_report(matches, shape, grid_n, mask=None, inliers=None,
                  aspect=True) -> dict                       # (2026-09-02)
    # {"coverage_pct", "dispersion_cv", "sdi", "sdi_definition",
    #  "grid_n", "grid_rows", "grid_cols", "counts", "cell_states"}
    # grid_n stays the SHORT-axis count, so nothing downstream that reads it breaks.
    # cell_states entries are exactly one of:
    #   "populated" | "insufficient_texture" | "masked_invalid"

spatial_distribution_index(coverage_pct, dispersion_cv) -> float | None   # (2026-09-02)
    # (coverage_pct/100) * 1/(1 + dispersion_cv), in [0,1]. None when either input is
    # None — never 0.0. A convenience scalar OVER coverage/dispersion/cell_states, not
    # a replacement for them (docs/decisions.md D10).

compute_metrics(matches, registration, shape, grid_n=4, mask=None,
                gt_H=None, runtime_s=0.0, aspect=True) -> dict           # (2026-09-02)
    # The metrics.json contract. Owns coverage_pct, dispersion_cv and sdi —
    # verify_matches deliberately does not emit them, because a MatchSet carries neither
    # the image shape nor the validity mask and a placeholder would fabricate a headline
    # number. Ground-truth error is evaluated through the FULL model, spline included.
```

### `samanvay/geometry/tps.py` — the non-rigid residual (2026-09-02, NEW MODULE)

```python
class ThinPlateSpline:
    displacement(xy) -> np.ndarray     # (N,2) correction to ADD, in SOURCE px
    apply(xy) -> np.ndarray            # xy + displacement(xy)
    to_dict() -> dict                  # JSON-safe: control points, weights, affine,
                                       #   lam, center, scale — what transform.json stores
    from_dict(d) -> ThinPlateSpline    # classmethod; round-trips to_dict exactly
    n_control: int
    lam: float

fit_tps(control_src_xy, control_pullback_xy, lam=0.5) -> ThinPlateSpline | None
    # Fits the displacement (src - pullback) over the pull-back positions. Returns None
    # when singular, mismatched or too few points. NEVER RAISES: a warp that cannot be
    # fitted is a warp we do not ship, not an exception mid-registration.
    # Kernel U(r) = r^2 log(r^2), U(0) = 0. lam is Tikhonov regularisation on the bending
    # energy: 0 interpolates exactly (overfits), larger is stiffer.

pullback(H, ref_xy, warp=None) -> np.ndarray | None
    # inv(H) @ ref_xy, then + warp.displacement(...) when warp is not None.
    # THE one place the full model is evaluated — verify, metrics and the warp all route
    # through it, so the global-then-spline order is defined exactly once. None when H is
    # singular or non-finite.
```

Fitting the spline in the source frame means **no inverse TPS is ever needed**: residuals
and `cv2.remap` both want exactly this quantity.

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

## 3. Run artifacts

**(2026-09-02)** This is no longer "the six files" — it was already seven, and
`output.grid` adds an eighth. Verified by listing `runs/demo_01` after `make demo`.

| file | contents |
|---|---|
| `registered.tif` | source warped onto the REFERENCE grid; reference CRS + geotransform, source dtype |
| `registered_source_grid.tif` | **(2026-09-02)** the same registration at SOURCE resolution, georeferenced by composing the reference geotransform with the fitted transform. Written when `output.grid` is `"both"` (default) or `"source"` — and under `"source"` it *is* `registered.tif`. Measured on the real `ch2_wac` pair: 128x128 on the reference grid, 3000x3000 on the source grid |
| `matches.csv` | `id, src_x, src_y, ref_x, ref_y, score, is_inlier, residual_px, sigma_px, grid_cell, role`. **(2026-09-02)** `role` is the LAST column: `"control"` \| `"check"` \| `""` when `registration.roles is None`. Blank, never a guess. Residuals in SOURCE px; unknown fields blank, never `0`. (The column names `is_inlier` and `grid_cell` are what the writer actually emits; earlier revisions of this file said `inlier` and `cell` and were wrong) |
| `transform.json` | `model_type`, `params` (3x3), `init_params`, `model_margin`, `rejected_models`, and **(2026-09-02)** a `warp` block — `type`, `control`, `weights`, `affine`, `lam`, `center`, `scale` — when a TPS was accepted, so a non-rigid run is reproducible from its artifacts alone |
| `metrics.json` | the `Registration.metrics` dict, section 1 |
| `provenance.json` | UTC timestamp, git SHA/branch/dirty, seed, full config, package versions, input paths, product ids, per-field `meta_source` |
| `report.html` | the visual report (or the minimal fallback) |
| `metrics_report.pdf` | **(2026-09-02)** the metrics as a PDF, via matplotlib `PdfPages`. Written when `config["report"]["pdf"]` is true (the default); degrades to no-PDF with a recorded reason rather than failing the run |
| `control_network.pvl`, `tiepoints.csv` | ISIS3-format control network and tie-point list. Format-compatible, **not validated against any ISIS binary** (`docs/limitations.md`) |

`samanvay register --metrics PATH` writes a second copy of `metrics.json` at `PATH` — the
CLI shape the plan specifies. `samanvay.report.dashboard.build_viewer(run_dir)` adds
`viewer.html` inside a run; `samanvay dashboard --runs runs` writes `runs/index.html`.

**(2026-09-02) The CLI's exit code is part of the contract.** `samanvay register` exits
**1** when `verify_status != "ok"`, printing a one-line reason first, and **0** otherwise.
A caller may gate on the exit code alone; `.github/workflows/ci.yml` now does.

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
7. **(2026-09-02)** `Registration.params` is the GLOBAL model even when `warp` is set.
   Anything that needs the full model calls `geometry.tps.pullback(params, ref_xy, warp)`
   — never `inv(params) @ ref_xy` by hand, or it silently drops the spline.
8. **(2026-09-02)** `roles` is `None` or has length N. When it is `None`, every `check_*`
   metric is `null` and `check_status` says why. A `check_rmse_px` of `0.0` from an empty
   check set would be the exact lie rule 6 exists to prevent.
9. **(2026-09-02)** `inliers` after a split is the union of the control RANSAC inliers and
   the check points inside the threshold. Coverage, dispersion and SDI measure *delivered*
   tie-points, and a good check point is a delivered tie-point.
10. **(2026-09-02)** `grid_shape` is the only place the grid shape is computed. Anything
    that needs a cell count uses `rows * cols` from it, not `grid_n ** 2` — on a
    non-square image those differ, and `cell_budgets` silently under-allocates if you
    assume the square form.

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
