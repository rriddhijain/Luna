# SAMANVAY — Handover

**SIH26166 · ISRO · Chandrayaan-2 ↔ LRO/SELENE registration**
Updated 2026-08-30. Status: **286 tests green · all four matcher arms working · the
illumination claim is measured and holds.**

The headline result: **the RIFT phase-congruency arm is the only one that survives a
large sun-angle difference**, and it is the only arm whose RMSE still has enough
redundancy to be quoted at Δ40–50°. That is the argument the whole project is built on,
and it is now generated from the fixtures rather than asserted.

---

## 1. The evidence — accuracy vs Δ sun angle

`fixtures/dsun_sweep/`: geometry, seed, DEM, albedo, sun elevation and noise identical in
every pair; **only the source sun azimuth changes**. Cells are
`true RMSE (px) / inlier count`, where true RMSE is measured against the analytic
ground-truth homography in `gt.json` — not self-consistency.
`!` marks a fit whose redundancy is below the measured trust bar of 10 (§4).

| Matcher arm | Δ0° | Δ10° | Δ20° | Δ30° | Δ40° | Δ50° |
|---|---|---|---|---|---|---|
| **L0** raw SIFT, physics off | 0.02 / 781 | 0.04 / 624 | 0.15 / 160 | 0.37 / 47 | 0.30 / 17 | **26.13 / 0** ! |
| **L1a** SIFT + empirical canonicalise | 0.03 / 800 | 0.07 / 771 | 0.22 / 214 | 0.61 / 55 | 0.45 / 22 | **26.13 / 0** ! |
| **L1b** SIFT + DEM physics (auto) | 0.02 / 800 | 0.05 / 780 | 0.26 / 198 | 0.46 / 56 | 0.76 / 15 | **26.13 / 0** ! |
| **L2** RIFT phase congruency | 0.26 / 742 | 0.28 / 708 | 0.53 / 621 | 0.96 / 436 | **1.57 / 266** | **1.90 / 173** |

How to read it:

1. **Every intensity-based arm fails outright at Δ50°** — zero inliers, no fit at all.
   Not "degraded": failed. That is the collapse the pitch describes, and it is now total.
2. **L2 holds across the entire sweep** — 173 inliers at 1.90 px where the others return
   nothing, and it is the only arm still trustworthy past Δ30°.
3. **L2 is honestly worse below Δ20°** (0.26 px vs 0.02 px at Δ0°). Say this first.
   Intensity matching is superb when the illumination matches; the claim is only about
   what happens when it does not.
4. **The P3 cascade lifted every arm.** Against the pre-cascade table it roughly doubled
   to sextupled inlier counts everywhere (L2 at Δ50°: 29 → 173) and cut true error at
   mid-range (L2 at Δ20°: 1.79 → 0.53 px). At a 16x scale ratio the audit measured it at
   4.2 s against 57.4 s for direct matching — more accurate *and* ~14x faster, because
   each level bounds the next one's search window.

Reproduce: `python -m bench.ablate --source ... --ref ...`, or the per-pair loop in
[`bench/baselines.md`](../bench/baselines.md).

---

## 2. What runs today

```bash
samanvay register --source S.tif --ref R.tif --out runs/demo    # register a pair
samanvay register ... --set match.method=rift --dem DEM.tif     # the illumination-robust path
samanvay register ... --no-canonicalise                         # the live ablation toggle
samanvay trn --ref BASEMAP.tif --dem DEM.tif --out runs/trn     # descent localisation
samanvay dashboard --runs runs                                  # compare every run
make demo | make ablate | make bench | make airgap
```

| Surface | State |
|---|---|
| Six run artifacts | `registered.tif` (correct CRS + transform), `matches.csv`, `transform.json`, `metrics.json`, `report.html`, `provenance.json` (real git SHA) |
| **Dashboard** | `runs/index.html` — every run, sortable, untrustworthy RMSE flagged, per-stage timings, uniformity grid. 61 runs scanned in 0.05 s |
| **Per-run inspector** | `--viewer` → synced pan/zoom, swipe, checkerboard, match overlay, click-to-inspect |
| **TRN demo** | 5/5 frames localised, **0.0318 m mean / 0.0489 m p90**, covariance-propagated error ellipses |
| **ISIS3 hand-off** | every run writes `control_network.pvl` + `tiepoints.csv`. Round-trip bit-exact. **Never opened by an ISIS3 binary** |
| **P3 cascade** | on by default; `match.cascade_enabled=false` to disable. Chaining propagates covariance across hops |
| **LSM** | built, off by default on measured evidence. `--set refine.method=lsm` |
| **Air-gap** | `scripts/verify_airgap.py` → PASS. 87 web assets, 55 modules, 31 locked packages, zero external URLs |
| Tests | **286 passing** |

---

## 3. Honest limits — say these before a judge finds them

- **The TRN error ellipse is over-confident.** Measured coverage is **1/5 at a nominal
  95%**. It is *formal precision* propagated from tie-point residuals; a bias common to
  all tie points falls outside it by construction. `samanvay trn` prints this warning
  itself. Fixing it means adding a measured systematic term.
- **No real Chandrayaan-2 or LRO data has been through this.** Every number above is on
  synthetic fixtures with exact ground truth. The loader and PDS adapter are ready; the
  claim is not.
- **The two-pass illumination loop does not bootstrap.** Rendering shading at a *corrected*
  pose is decisively better (Δ50°: 37 inliers / 1.30 px vs 5 / 49.9 for empirical), but the
  full-resolution render tolerates under ~1 source px of pose error and pass 1 delivers
  13–1830 px on exactly the pairs that need pass 2. Independently reproduced by the audit:
  every fixture got *worse* on pass 2. **Do not wire it as a default.**
- **The Docker image has never been built** and **CI has never run on GitHub.** Both are
  written and correct as far as can be checked offline; neither is verified.
- **Upright only** (< ~15° rotation). The rotation-invariant multi-MIM RIFT variant is not
  built.
- **`photometry.norient` must stay 6.** `describe.py` hardcodes it; setting 8 silently
  costs 41 inliers and 0.16 px on Δ0° with no warning. See §5.

Full list: [`docs/limitations.md`](limitations.md).

---

## 4. Numbers that were guesses and are now measured

- **`_NEFF_K`** in `refine.py`: 0.35 → **0.22**. Predicted-vs-actual sigma agreement went
  0.790 → **0.995** (1.0 = honest). Sigma is NaN above the calibrated correlation range —
  0% of points on fixture pairs, 36.6% on unrealistically clean synthetic ones.
- **Redundancy thresholds** in `verify.py`: reject 1 → **2**, trust 3 → **10**. Evidence in
  `runs/calibrate/redundancy.md`: at redundancy 3 a fit understates its own error by 2.47×
  (10.2× on real runs) with 43% off by more than 3×; at 10–19 nothing exceeds 3×. The old
  bar was issuing `rmse_trustworthy=true` on fits that were not.
- **`refined_count`** now means points that actually moved, computed in `stages.py`.
  `sigma_known_count` is the separate count of points with a known uncertainty.

---

## 5. What is left

Ordered by value. Nothing here is structural — the pipeline runs end to end.

1. **Real data.** ISSDC Pradan + LROC registration, then a Tier-B insurance pair
   (NAC ↔ NAC, same site, very different incidence) which needs no ISSDC access at all.
   This is the only thing standing between the project and a real-data claim.
2. **Push a branch and watch CI go green**, and run `docker compose build` once. Both are
   unverifiable offline.
3. **Forward `geotransform_exact` / `geotransform_max_error_m`** through
   `io/metadata.normalise_meta`. The fixture sidecars carry them (source: 36.2 px stated
   error; reference: exact) but they are dropped, so photometry's `auto` policy always
   picks `lowfreq` on evidence it never reads. Two lines — but it flips the reference to a
   full render, so **re-measure the arm table if you take it.**
4. **Thread `photometry.norient` into `describe_keypoints`, or assert it.** Silent
   degradation today.
5. **Calibrate the TRN ellipse** with a systematic term, or relabel it "formal precision"
   everywhere it appears.
7. **Open `control_network.pvl` with a real ISIS3 binary** (`cnetpvl2bin`, then `qnet`).
   Until somebody does, the only honest claim is "we emit the format". Replace the
   placeholder SerialNumbers with `getsn` output before running `jigsaw`.
8. **Prove the cascade multi-level on mission-sized imagery.** At fixture sizes
   `min_level_side` binds first, so every ratio descends exactly 2 levels.
6. **Optional speedup:** `match/tile.py` could pass `pc` into `describe_keypoints` and skip
   an internal phase-congruency recompute (~1 s/pair). Every number in §1 was measured on
   the recompute path, so **re-run the sweep if you take it.**

---

## 6. What was deliberately not built

- **Learned matcher (LoFTR/LightGlue).** Needs torch + kornia, which is a download, and it
  is outside P0 in the charter. The existing LoFTR sketch sits unmerged on `origin/cv/ml`.
- **REST API, job queue, PostGIS, ONNX/TensorRT, GPU batching.** All need a dependency
  that cannot be downloaded here, or are explicitly post-prototype.
- **Gigapixel deep-zoom.** `viewer/tiles.py` generates DZI pyramids, but OpenSeadragon is
  not vendored (a download), so the inspector uses a hand-written canvas pan/zoom with a
  marked hook where OpenSeadragon slots in.
- *(Cross-tier chaining and ISIS3 export were on this list and are now built — see §2.)*
- **Rotation-invariant RIFT, trilinear descriptor binning.** Named upgrade paths in the
  module docstrings.

---

## 7. Delivery

**Nothing is committed.** All 66 changed and added files sit in the working tree so the
whole delivery reads as one `git diff`.

- 286 tests passing, up from 6
- 12,000 lines of Python, 4,443 of them tests
- 66 files changed or added
