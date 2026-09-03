# Bringing real data in

You have lunar products and want a registration. This is the whole path.

Everything measured so far is on synthetic fixtures with exact ground truth. Real pairs
have none, so §7 — how to tell whether a real result is trustworthy — matters more than
any other section here.

---

## 1. Where files go

Anywhere. Nothing in the pipeline depends on the location, and paths are passed on the
command line. `data/` is already gitignored, so `data/real/` is convenient:

```
data/real/
  src.tif          the moving image (Chandrayaan-2 OHRC / TMC / IIRS, or NAC #1)
  ref.tif          the fixed image  (LRO NAC / WAC, SELENE, or NAC #2)
  dem.tif          optional
```

---

## 2. Formats, and the order metadata is read in

`samanvay/io/metadata.py` looks in three places, first hit wins per field:

| Priority | Source | Notes |
|---|---|---|
| 1 | **Sidecar JSON** at `<file>.json` | Always works. The escape hatch for anything that will not parse. |
| 2 | **PDS3 `.LBL` / PDS4 `.xml` label** beside the image | Tolerant key/value scan, best-effort. |
| 3 | **GeoTIFF tags** | Geotransform, CRS, shape, dtype, nodata. |

The image itself is read by rasterio, so GeoTIFF is the smoothest path. If you have a raw
PDS `.IMG`, converting it to GeoTIFF once (`gdal_translate`) removes a whole class of
problem.

Every field records where it came from, and you can see it: `meta_source` in the preflight
output shows `sidecar`, `pds_label`, `geotiff`, `derived` or `unknown` per field.

---

## 3. The fields that matter

**A missing field is a degraded mode, not a failure.** The loader marks it `unknown` and
never substitutes a plausible value — that is deliberate, and it is why a preflight report
can be trusted.

| Field | Unlocks | If missing |
|---|---|---|
| `sun_az_deg`, `sun_el_deg` | **P1 physics** — DEM shading, cast shadows, Lommel-Seeliger | Falls back to `empirical` illumination: a smoothed self-estimate. Works, but it is not the DEM-driven claim. |
| `gsd_m` | P1, and the scale-ratio estimate | Scale is taken from the geotransforms instead. |
| `geotransform` | **P2 coarse init** — starts matching near the answer | Degrades to a GSD-ratio scale, then identity. Matching starts much further away. |
| `crs` | Cross-checking the two frames | Nothing fatal; registration is in pixels. |
| `nodata` | Masking | Non-finite pixels are still caught. |
| *(nothing at all)* | Matching still runs | You get a registration, in the weakest mode. |

If only `incidence_deg` is present, sun elevation is derived as `90 - incidence` and
recorded as `derived` — you will see that in the report.

**Sidecar template** — drop this at `data/real/src.tif.json`:

```json
{
  "product_id": "ch2_ohrc_ndn_20200114",
  "instrument": "OHRC",
  "gsd_m": 0.28,
  "sun_az_deg": 132.4,
  "sun_el_deg": 23.1,
  "incidence_deg": 66.9,
  "emission_deg": 2.1
}
```

### PDS keywords the parser recognises

`PRODUCT_ID`, `PRODUCT_NAME`, `IMAGE_ID`, `LOGICAL_IDENTIFIER` ·
`SUB_SOLAR_AZIMUTH`, `SOLAR_AZIMUTH`, `SUN_AZIMUTH` ·
`INCIDENCE_ANGLE`, `EMISSION_ANGLE`, `PHASE_ANGLE` ·
`MAP_SCALE`, `PIXEL_RESOLUTION`, `PIXEL_SCALE`, `RESOLUTION` ·
`INSTRUMENT_ID`, `INSTRUMENT_NAME`

Anything else, put in a sidecar. The exact alias tables are at the top of
`samanvay/io/metadata.py`.

---

## 4. Which pair to try first

**Start with LRO NAC ↔ LRO NAC: same site, very different incidence angle.**

It is public, needs no ISSDC account, and it proves the hardest part of the problem —
illumination invariance — on *real lunar imagery*. A Chandrayaan-2 ↔ NAC pair is the full
claim, but it stacks cross-mission radiometry, cross-mission geodetic offset and a large
scale ratio all at once; when it fails you will not know which one broke.

Get a NAC↔NAC result first. Then the Ch-2 pair is a comparison, not a mystery.

`scripts/download_pairs.py` finds one for you — it queries the ODE REST API, takes the
pair with the largest incidence difference **that actually overlaps**, writes sidecars
and runs the preflight:

```bash
python scripts/download_pairs.py --lat -85.0 --lon 25.0 --radius 0.5
```

Overlap is the gate, not a filter applied afterwards. ODE's search radius does not
confine results to the region, and incidence angle tracks latitude, so ranking by
illumination alone picks the two most *distant* frames on the Moon — §8's most common
mistake, automated. It writes only the angles ODE measured; sun azimuth is not in the
product record, so it is left unknown and the run degrades to `empirical` rather than
carrying a substituted value that would read as measured.

---

## 5. Run it

```bash
# 1. Preflight — never skip this. Exits non-zero if the pair is unusable.
samanvay check --source data/real/src.tif --ref data/real/ref.tif --dem data/real/dem.tif

# 2. It prints the exact register command for your data. Roughly:
samanvay register \
    --source data/real/src.tif \
    --ref    data/real/ref.tif \
    --dem    data/real/dem.tif \
    --set match.method=rift \
    --out runs/real_01 --viewer

# 3. Look at it
open runs/real_01/report.html        # figures, residuals, uniformity grid
open runs/real_01/viewer.html        # swipe, checkerboard, click a match

# 4. Compare runs
samanvay dashboard --runs runs
```

`samanvay check` chooses `match.method` for you: **`rift`** when the sun difference is 20°
or more *or unknown*, `sift` below that. That threshold comes from the measured sweep in
[`bench/baselines.md`](../bench/baselines.md) — at Δ50° every intensity-based arm returns
zero inliers while RIFT returns 173.

---

## 6. What a run writes

`registered.tif` · `matches.csv` · `transform.json` · `metrics.json` · `report.html` ·
`provenance.json` · `control_network.pvl` (ISIS3) · `tiepoints.csv`

---

## 7. Reading a real result — the important part

**There is no ground truth for a real pair.** `metrics.json["gt_rmse_px"]` will be `null`.
Every accuracy figure quoted elsewhere in this repo came from synthetic fixtures where the
true homography is known analytically. On real data you have only:

- `rmse_px` — **self-consistency**. How well the fit reproduces the points it was fitted
  to. This is *not* accuracy.
- `rmse_trustworthy` — whether `rmse_px` means anything at all.
- `redundancy` — inliers beyond the minimum the model needs.

**A low `rmse_px` with few inliers means nothing.** A homography has 8 degrees of freedom;
4 points determine it exactly, so its residual is zero by construction no matter how wrong
the transform is. We measured a fit reporting 0.00004 px against 214 px of true error.
`rmse_trustworthy` exists precisely to catch this, and the threshold is measured, not
guessed: below redundancy 10, fits understate their own error by 2.5–10× (see
`runs/calibrate/redundancy.md`).

### What good looks like

| Signal | Healthy | Worry |
|---|---|---|
| `inlier_count` | hundreds | under ~20 |
| `redundancy` | comfortably > 10 | ≤ 3 |
| `rmse_trustworthy` | `true` | `false` — do not quote the RMSE |
| `coverage_pct` | > 90% | clustered in one corner |
| `dispersion_cv` | low | high — matches piled on one crater |
| `model_type` | `similarity` or `affine` | `failed` |

**To claim accuracy on a real pair** you need something outside the fit: hold-out manually
measured tie-points, or an ISIS3 cross-check via the exported `control_network.pvl`.
Neither has been done yet.

---

## 8. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `check` says **blocked, source projects outside reference** | Not the same ground, or geotransforms in different CRS | Confirm the two products overlap. This is the most common real-data mistake. |
| **Zero inliers** | Sun difference too large for `sift` | Use `--set match.method=rift`. |
| Still zero with `rift` | Genuinely too little overlap or texture | Crop both to the shared region; try `--set grid_n=2`. |
| `illum_mode` reads **`empirical`** when you passed a DEM | Sun angles unknown, or the DEM does not cover the source | Add sun angles to a sidecar; check the DEM footprint. |
| `illum_mode` reads **`dem_lowfreq`**, you expected `dem` | DEM far coarser than the image, or the pose is not trusted | Expected and correct. Full-resolution shading from a wrong pose is *worse* than none — see `docs/decisions.md` D2. |
| **Suspiciously perfect RMSE** (< 0.1 px, few inliers) | Degenerate fit reproducing its own sample | Check `rmse_trustworthy` and `redundancy`. Do not quote it. |
| **CRS mismatch** note | Normal for cross-mission pairs | Coarse init falls back to a GSD-ratio scale. Not fatal. |
| **Huge scale ratio** (> 6×) | Cross-tier pair | The P3 cascade handles it automatically and gets *faster*, not slower. |
| Mostly **nodata** | Frame is largely empty | Crop, or uniformity coverage will be measured over empty space. |
| Crash on a gigapixel strip | A path that materialises the full array | Report it — tiled I/O exists but not every path uses it. Real strips are untested. |

---

## 9. What has never been tested

Stated plainly, because you will be the first person to hit these:

- ~~**No real Chandrayaan-2 or LRO product has been through this pipeline.**~~
  **Corrected 2026-09-02: false.** Real LROC NAC ↔ NAC pairs (Apollo 16, 888 x 11952
  strips at 2 m GSD, Δsun 4° / 85° / 115°), Chandrayaan-2 ↔ Chandrayaan-2 and
  Chandrayaan-2 ↔ LRO WAC pairs have all been registered. What is still true, and
  matters more: **none of them has ground truth.** `gt_rmse_px` is `null` on every real
  run, so §7's advice below is the whole of how you judge one.
- **Gigapixel strips are untested.** Tiled reading and the canonicalisation cache exist,
  but OHRC-scale inputs have never been run. The largest real input registered so far is
  10.6 Mpx per image, at 195 s wall clock.
- **The ISIS3 control network has never been opened by an ISIS3 binary.**
- **PDS3/PDS4 label parsing is best-effort** against the alias tables, not against real
  ISSDC products.

Expect the first real pair to need tuning. That is the work, not a failure.


---

## 10. Attribution, and what this repository does not contain

**No mission data is redistributed here.** `data/` is gitignored and the only rasters under
version control are the synthetic fixtures this project renders itself (`synth/`, seed 0,
byte-reproducible). Fetch real products from the agencies' own archives —
`scripts/download_pairs.py` does the LROC NAC case end to end — and keep their terms with
them.

| products | credit | archive |
|---|---|---|
| LROC NAC / WAC imagery, LROC-derived DTMs, SLDEM | **NASA / Goddard Space Flight Center / Arizona State University** | PDS Cartography and Imaging Sciences Node (LROC PDS archive); ODE for search |
| Chandrayaan-2 OHRC / TMC-2 / IIRS | **ISRO / Physical Research Laboratory** | ISSDC PRADAN, under ISRO's data policy |
| SELENE / Kaguya | **JAXA** | JAXA / SELENE data archive |

Each agency's terms apply to its own data. SAMANVAY's own Apache-2.0 licence
([`LICENSE`](../LICENSE)) covers the **code only**, and nothing in it grants a right to
redistribute mission data.

If you publish a result from a real pair, credit the products it was computed from — and
carry `provenance.json` with it, which already records the input paths, product ids and
per-field metadata sources.
