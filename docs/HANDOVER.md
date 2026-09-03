# SAMANVAY — Handover

**SIH26166 · ISRO · Chandrayaan-2 ↔ LRO/SELENE registration**
Updated **2026-09-03**. Status: **429 of 429 tests green · all four matcher arms working ·
the illumination claim is measured and holds · real pairs now register, without ground
truth.**

Sections 1-7 were written 2026-08-30. Every claim re-checked on 2026-09-02 is corrected
in place and marked; the amendment at §8 covers what the revamp added, and §9 covers what
walking every documented entry point on 2026-09-03 turned up.

The headline result: **the RIFT phase-congruency arm is the only one that survives a
large sun-angle difference.** Pinned SIFT delivers no model at all past Δ50°; the shipped
`auto` default delivers one on all 14 sweep pairs out to Δ180°. That is the argument the
whole project is built on, and it is generated from the fixtures rather than asserted (§1).
What it is *not* is an accuracy claim on real data: no real pair here has ground truth.

---

## 1. The evidence — accuracy vs Δ sun angle

**Rewritten 2026-09-02.** The table that stood here until then was the 2026-08-30 four-rung
sweep, which stopped at Δ50° and measured code that no longer exists. It is archived in
[`bench/baselines.md`](../bench/baselines.md) §5 and must not be quoted as current; the
numbers below supersede it and come from that file's §1.

`fixtures/dsun_sweep/`: 14 pairs. Geometry, seed, DEM, albedo, sun elevation and noise are
identical in every pair; **only the source sun azimuth changes.** `gt_rmse_px` is true
error in source pixels against the analytic homography in `gt.json`, evaluated through the
full delivered model including the spline. `check_rmse_px` is measured on tie-points held
out of the fit entirely.

Two arms: **`auto`** (the shipped default, nothing pinned) and **`sift`** (`match.method`
pinned, everything else default).

| Δsun (world) | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` `gt_rmse_px` | 0.024 | 0.041 | 0.206 | 1.143 | 0.895 | 1.887 | 2.110 | 3.514 | 4.512 | 5.111 | 4.497 | 6.824 | 3.163 | 0.592 |
| `auto` inliers | 790 | 780 | 292 | 183 | 142 | 99 | 73 | 49 | 49 | 50 | 51 | 46 | 59 | 64 |
| `auto` `check_rmse_px` | 0.218 | 0.366 | 0.391 | 1.415 | 1.175 | 1.174 | 1.274 | 1.568 | 1.152 | 1.051 | 1.433 | 1.449 | 1.751 | 1.298 |
| `sift` `gt_rmse_px` | 0.024 | 0.041 | 0.206 | 0.435 | 2.052 | 5.610 | — | — | — | — | — | — | — | — |
| `sift` inliers | 790 | 780 | 292 | 69 | 23 | 9 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

How to read it:

1. **The source raster is rotated 10°**, so the difference the engine reads off the two
   sidecars is `|Δ - 10|`. That is why the Δ0–Δ20 columns of the two arms are identical:
   `auto` sees under 20° there and picks `sift`, so they are the same run. `auto` flips to
   `rift` at Δ30 and stays there.
2. **Pinned SIFT does not degrade, it stops.** 9 inliers at Δ50°, then **no model at all**
   on all eight pairs from Δ60° up. `auto` delivers a model on all 14 and never drops below
   46 inliers. That is the whole argument of the project, and it now extends to Δ180°
   rather than stopping at Δ50°.
3. **`auto` is honestly worse where SIFT works.** At Δ30 pinned SIFT is more accurate
   (0.435 px against 1.143 px) on fewer points. The bar sits at 20° because that is where
   preflight already recommends, not because it is the accuracy optimum for every pair.
4. **Δ180° is the EASY case, and the trough is Δ90–Δ120.** True error peaks at 6.824 px at
   Δ120 and falls back to 0.592 px at Δ180. Opposite suns re-illuminate the same relief
   with inverted contrast, which phase congruency is invariant to; a 120° difference does
   not. Any pitch that treats "opposite suns" as the hard case has it backwards.
5. **The `check_rmse_px` row is flat (1.0–1.8 px past Δ30) while `gt_rmse_px` climbs to
   6.8 px.** Held-out self-consistency does not track true error here. Neither number is
   wrong; they measure different things, and this row is the reason `gt_rmse_px` is still
   reported wherever a truth transform exists.

Reproduce the *current* sweep, not this table:

```
python -m synth.sweep
make bench SWP_OUT=runs/dsun_sweep_full      # or the line it runs:
python -c "from bench.harness import run_manifest, SWEEP_CONFIG; \
           run_manifest('fixtures/dsun_sweep/manifest.json', 'runs/dsun_sweep_full', SWEEP_CONFIG)"
```

**Run 2026-09-03, that command reproduces the table above exactly** — fourteen distinct
rows, `gt_rmse_px` 0.024 / 0.041 / 0.206 / 1.143 / 0.895 / 1.887 / 2.110 / 3.514 / 4.512 /
5.111 / 4.497 / 6.824 / 3.163 / 0.592 px, inliers 790 / 780 / 292 / 183 / 142 / 99 / 73 /
49 / 49 / 50 / 51 / 46 / 59 / 64, `check_rmse_px` 0.218 / 0.366 / 0.391 / 1.415 / 1.175 /
1.174 / 1.274 / 1.568 / 1.152 / 1.051 / 1.433 / 1.449 / 1.751 / 1.298, in 85 s of pipeline
time.

**`python -m bench.harness --manifest …` on its own does NOT reproduce it, and that is a
defect, not the weather.** The phase-congruency cache keys on `(product_id, photometry
params, file size, int mtime, shape)`; every sweep fixture is `product_id:
"synth_source"`, 1474265 bytes, and several are written inside one wall-clock second, so
two pairs collide on the key and the second reads the first's phase congruency.
Reproduced in isolation 2026-09-03 on two copies of `dsun_60` and `dsun_70` with their
mtimes forced equal and a private cache directory: cache **on**, both report 2.1098 px /
73 inliers / 153 matches; cache **off**, the second reports its own 3.5141 px / 49 / 126.
An earlier revision of this file said `rm -rf .cache` first; **it does not fix it** — the
harness warms the cache as it iterates, and a run with `.cache` removed immediately
beforehand still returned 2 of 14 rows as another pair's numbers. What does fix it is
switching the cache off, which is what `SWEEP_CONFIG` above does and what
`bench/harness.py::run_arms()` already does — which is why `bench/baselines.md` §1 is
correct. `run_manifest()` does not apply it and the CLI exposes no way to pass it; that is
the first of the two fixes still owed. The second, and the real one, is the key itself:
put the file path or a content hash into it in
`pipeline/stages.py::_canonicalise_cached`. Until then, whether a regeneration re-arms the
collision is a race against the clock — `python -m synth.sweep` into a clean directory
took 29 s here and left the fourteen sources 2 s apart (no collision), while the fourteen
in this repository were written faster and several share a second. A quicker machine
collides, and nothing in the output says which happened. Full write-up in
`docs/limitations.md` §9.

`python -m bench.ablate --source ... --ref ...` produces the stage ablation
(`bench/baselines.md` §2), which is a different question.

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
| Run artifacts | `registered.tif` (correct CRS + transform), **`registered_source_grid.tif`** (source resolution, 2026-09-02), `matches.csv` (with a `role` column), `transform.json` (with a `warp` block when a TPS is kept), `metrics.json`, `report.html`, `provenance.json` (real git SHA), `control_network.pvl`, `tiepoints.csv`, **`metrics_report.pdf`** |
| **Dashboard** | `runs/index.html` — every run, sortable, untrustworthy RMSE flagged, per-stage timings, uniformity grid. 61 runs scanned in 0.05 s |
| **Per-run inspector** | `--viewer` → synced pan/zoom, swipe, checkerboard, match overlay, click-to-inspect |
| **TRN demo** | 5/5 frames localised, **0.0318 m mean / 0.0489 m p90**, covariance-propagated error ellipses |
| **ISIS3 hand-off** | every run writes `control_network.pvl` + `tiepoints.csv`. Round-trip bit-exact. **Never opened by an ISIS3 binary** |
| **P3 cascade** | on by default; `match.cascade_enabled=false` to disable. Chaining propagates covariance across hops |
| **LSM** | built, off by default on measured evidence. `--set refine.method=lsm` |
| **Air-gap** | `scripts/verify_airgap.py` → PASS, re-run 2026-09-02. 68 modules, 31 locked packages, zero external URLs. The web-asset count scales with how many runs are on disk, so it is not a fixed figure |
| Tests | **429 passing, 0 failing** of 429 collected (`.venv/bin/pytest -q`, 56.7 s, 2026-09-03). The one failure reported here on 2026-09-02 — `test_preflight.py::test_scan_ranks_the_real_fixture_sweep`, a fixture-vs-test disagreement after the Δsun sweep was extended to 180° — has since been closed |

---

## 3. Honest limits — say these before a judge finds them

- **The TRN error ellipse is over-confident.** Measured coverage is **1/5 at a nominal
  95%**. It is *formal precision* propagated from tie-point residuals; a bias common to
  all tie points falls outside it by construction. `samanvay trn` prints this warning
  itself. Fixing it means adding a measured systematic term.
- ~~**No real Chandrayaan-2 or LRO data has been through this.**~~ **Corrected
  2026-09-02: false.** Real LROC NAC ↔ NAC (Apollo 16, three Δsun pairs), Chandrayaan-2 ↔
  Chandrayaan-2 and Chandrayaan-2 ↔ LRO WAC pairs have all been registered. The limit that
  remains is different and just as important: **none of them has ground truth**, so
  `gt_rmse_px` is `null` on every real run and the only error figure available is held-out
  self-consistency. Every number in §1 is still synthetic-with-truth. See
  `README.md` → "The real-data run, in full" and `docs/limitations.md` §9.
- **The two-pass illumination loop does not bootstrap.** Rendering shading at a *corrected*
  pose is decisively better (Δ50°: 37 inliers / 1.30 px vs 5 / 49.9 for empirical), but the
  full-resolution render tolerates under ~1 source px of pose error and pass 1 delivers
  13–1830 px on exactly the pairs that need pass 2. Independently reproduced by the audit:
  every fixture got *worse* on pass 2. **Do not wire it as a default.**
- **The Docker image has never been built** and **CI has never run on GitHub.** Re-checked
  2026-09-03: `docker version` reaches no daemon (the colima socket does not exist) and
  `docker compose` is not a known subcommand on this machine. Both files are written and
  correct as far as can be checked offline; neither is verified.
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

1. ~~**Real data.**~~ **Done (2026-09-02)** — the Tier-B NAC ↔ NAC insurance pair and the
   Chandrayaan-2 pairs all register. What replaces it: **ground truth for a real pair.**
   Held-out self-consistency is the strongest figure available today; a geodetic check
   against an independently controlled product is what would turn it into an accuracy
   claim. **The intake path is [docs/DATA.md](DATA.md), and `samanvay check` preflights a
   pair before you spend time on a registration.**
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

**Superseded 2026-09-02.** The 2026-08-30 delivery has since been committed and the
2026-09-02 revamp sits on top of it, partly committed and partly in the working tree.
`git log --oneline` and `git status` are the authority; a hand-maintained file count in a
handover document goes stale within a day and this one did.

- **429 of 429 tests passing**, up from 286 on 2026-08-30 and from 6 at the start


---

## 8. Amendment — the 2026-09-02 revamp

What changed, and where the reasoning lives. Nothing below is a rewrite; every item is
additive with a default, so no existing call site broke.

| what | where | why it matters to a judge |
|---|---|---|
| **Control / check split**, deterministic and cell-stratified | `geometry/verify.py`, `geometry.check_fraction` = 0.2 | `check_rmse_px` is the only accuracy figure here not measured on the fit's own sample. **Quote it, not `rmse_px`.** Plan Part 1 §4 |
| **Thin-plate spline**, accepted only on held-out improvement under two conditions | `geometry/tps.py`, `decisions.md` D9 | answers "how do you know the non-rigid warp is not overfitting?" — it is rejected by points it never saw. Plan Part 2 §5 |
| **SDI** emitted natively beside coverage and dispersion | `geometry/uniformity.py`, `decisions.md` D10 | the plan asks for it by name (Part 1 §2); the three real numbers stay |
| **Quad-tree ANMS** inside every cell | `match/anms.py` | the grid used to be filled by a plain score sort, so K points could pile into one corner of a cell. Plan Part 1 §2 |
| **N × M grid** following image aspect | `geometry/uniformity.grid_shape` | the real NAC strip is 13.5:1 and gets a **54 × 4** grid. A fixed 4 × 4 measured coverage on 13:1 cells |
| **`match.method: auto`** from Δsun azimuth | `pipeline/stages.py`, `decisions.md` D7 | the default used to be `sift`, which collapses to 9 inliers at 5.610 px at Δsun 50° and delivers **no model at all** from Δ60° up (`bench/baselines.md` §1) |
| **Mask-boundary fill** for the PC input | `photometry/normalize.py`, `decisions.md` D12 | boundary PC response 35.5× → 1.07× the interior on a contiguous shadow |
| **`output.grid: both`** | `io/writers.py`, `decisions.md` D13 | the real `ch2_wac` run delivered a 128×128 file from a 3000×3000 source. It now delivers both |
| **IIRS band reduction** (SNR screen → PCA first component) | `io/bands.py`, `decisions.md` D5 amendment | Plan Part 1 §3. Never run on a real cube |
| **`--metrics PATH`** and a **non-zero exit** on a failed registration | `pipeline/run.py` | the plan's CLI shape, and a run you can gate a script on |
| **LICENSE (Apache-2.0)**, data attribution, tracked run evidence | repo root, `.gitignore` | a clone can now verify `metrics.json` / `matches.csv` / `provenance.json` / `transform.json` for every run, while imagery stays out of git |

**Still open after the revamp** (`docs/limitations.md` §9 has the full list): the inlier
ratio misses 0.85 on every pair measured; `--seed` controls nothing and says so;
`residual_units`, `band_reduction` and `read_decimation` are recorded on the objects but
never reach an artifact; no real pair has ground truth; the container has still never been
built and CI has still never run on GitHub.

---

## 9. Amendment — every entry point walked cold, 2026-09-03

Run as a judge would run them, from the README, on this machine, on 2026-09-03. This is
the table to check before demo day: if a row here stops being true, the README is lying.

| entry point | result |
|---|---|
| `make setup` | **ok**, exit 0 — `env ok python 3.11.15 · cv2 5.0.0 · rasterio 1.4.4` |
| `make fixture` | **ok**, 4.0 s — writes `fixtures/synth_pair_A`, source 1715², reference 1024² |
| `make demo` | **ok**, exit 0, 23.8 s — `homography+tps`, 64 inliers, held-out 1.5712 px, true 2.9287 px, SDI 0.5784, and it prints `inlier ratio vs plan FAIL (0.2936 vs 0.85)` |
| `make smoke` | **PASS**, exit 0, 11.9 s — `dsun_50 --dem`, 102 inliers, 1.60 px true error, `homography+tps` |
| `make ablate` | **ok**, exit 0, 9 min 25 s — 18 arms, `ablation.csv` + `ablation.md`. **Eight** of the eighteen return exactly the baseline on this pair, each independently re-run and confirmed 2026-09-03; see `docs/limitations.md` §9 |
| `make bench` | **ok**, exit 0 — 14/14 runs, `bench.csv` + `bench.md`, fourteen distinct rows reproducing §1 exactly. It no longer runs the plain `bench.harness` CLI: that path leaves the phase-congruency cache on and returned two of the fourteen rows as another pair's numbers, with or without `rm -rf .cache` first. See §1 and `docs/limitations.md` §9 |
| `make dashboard` | **ok** — serves `/viewer/` (HTTP 200) and `/runs/index.html` (HTTP 200) on loopback |
| `make airgap` | **PASS**, exit 0 — 68 modules, 31 locked packages, zero external URLs. The web-asset count scales with how many runs are on disk (it printed 408 on 2026-09-03 and 159 earlier the same day), so it is not a figure to quote |
| `make clean` | **ok** — verified on a copied tree rather than the live one, because other seats were writing to `runs/` at the time. Removes `runs/ .cache/ .pytest_cache/` and every `__pycache__`; leaves `fixtures/` and `.venv/` |
| README quickstart step 3, verbatim, with `--metrics` | **ok**, exit 0 — **11** files in `--out`, including `registered_source_grid.tif` and the second metrics copy at the `--metrics` path. `--viewer` adds a twelfth |
| `samanvay register` exit code | **verified both ways** — 0 on the default config, **1** with `--set match.method=sift` on the same pair, reason printed first |
| `samanvay check` on a pair | **ok**, exit 0 — `VERDICT: READY`, and it prints the exact `register` command to run next |
| `samanvay check --dir fixtures/dsun_sweep` | **ok**, exit 0 — ranks **378** candidate pairs (2026-09-03; the count is a function of what is in the directory, not a fixed figure), names the one to register |
| `samanvay register --viewer` | **ok** — writes `viewer.html` carrying that run's own `check_rmse_px` |
| `samanvay dashboard --runs runs --viewers` | **ok** — `runs/index.html`, real per-run values, no error string in the generated HTML |
| Real pair `ch2_wac` | **ok**, exit 0, 9.7 s wall clock (`runtime_s` 1.563 — the rest is interpreter start) — `registered.tif` **128 × 128** *and* `registered_source_grid.tif` **3000 × 3000**, both georeferenced, verified with rasterio. `rmse_px 0.0` from 4 inliers, `check_rmse_px` null, `rmse_trustworthy: false` |
| Real pair NAC `dsun115` | **ok**, exit 0, 183.4 s — every figure in README's real-data table reproduced exactly; `registered.tif` and `registered_source_grid.tif` both 888 × 11952 |
| Real pair NAC `dsun085` (the bad one) | **ok**, exit 0, 182.4 s — reproduced exactly: `affine+tps`, 184 inliers at ratio **0.037**, coverage 42.77%, SDI 0.170, `check_rmse_px` 1.766 with `check_outlier_frac` **0.963** and `check_rmse_all_px` **187.45**. Quote the last three together or not at all |
| CI smoke body, run by hand | **ok**, exit 0 — artifacts and all 22 contract keys present. The fixture is now 512 px, not 256: at 256 the run lands on 6 inliers with `check_status: skipped_too_few_matches` |
| `docker compose build` | **not run.** No daemon, no compose plugin on this machine |
| `.github/workflows/ci.yml` on GitHub | **not run.** The runner steps — checkout, setup-python, the linux/x86_64 install of a macOS-generated `requirements.lock`, upload-artifact — cannot be checked offline |
