# SAMANVAY — measured baselines

**The rule for this file has not changed: every cell is a number somebody ran, or the words
"not measured". Never a plausible default, never an interpolation, never a number carried
over from a different build.** A number nobody ran is a lie that survives into a slide.

Every table below carries the date it was measured, the fixture it was measured on, the
command that produces it and the state of the tree at the time. A table without those four
things is not evidence and does not belong here.

## What is in this file

| section | what | fixture | measured |
|---|---|---|---|
| 1 | Δsun sweep, 0-180°, two matcher arms | `fixtures/dsun_sweep/` (synthetic, ground truth) | 2026-09-02 |
| 2 | Stage ablation, 21 arms, two fixtures | `fixtures/synth_pair_A/`, `fixtures/dsun_sweep/dsun_50/` | 2026-09-03 |
| 2c | Which ablation rows are inert, and why each one is | as section 2 | 2026-09-03 |
| 2d | ANMS: where the per-cell quota binds, and what it does there | fixtures + `data/real/apollo16/pairs/` | 2026-09-03 |
| 2e | Photometric model, run with the fixtures' DEMs | as section 2 | 2026-09-03 |
| 2f | `mask_fill` x `clahe`, all 14 Δsun steps + 3 independent scenes | `fixtures/dsun_sweep/`, `fixtures/synth_pair_A/`, `data/real/apollo16/pairs/` | 2026-09-03, prose corrected 2026-09-03 (see 2f) |
| 3 | Real LROC NAC pairs, three Δsun | `data/real/apollo16/pairs/` (no ground truth) | 2026-09-02 |
| 4 | Descriptor-level nearest-neighbour rates | `tests/test_describe.py` | 2026-08-29, Δ sweep 2026-09-02 |
| 5 | Archive of superseded tables | — | see section |

Code state for section 1 and section 3: branch `feat/foundation-and-evidence`, git `bbd5cea`
with the Phase A-D revamp uncommitted in the working tree (`git_dirty: true` in every
`provenance.json` under `runs/`). Numbers here therefore describe the working tree, not
that commit; each run directory carries its own `provenance.json` with the exact config.
Section 2 was re-measured on 2026-09-03 on the same branch with the bench changes of that
day; the pipeline itself was not modified between the two dates and `baseline_full`
reproduces its 2026-09-02 numbers exactly on both fixtures.

---

## The four rungs

| level | what it is | how to run it now | why it is in the table |
|---|---|---|---|
| **L0 SIFT raw** | SIFT on raw DN, no photometric correction | `--set photometry.canonicalise=false --set match.method=sift` | The null hypothesis. If the canonicaliser does not beat this, the project's thesis is wrong. |
| **L0 ORB raw** | ORB on raw DN | `--set match.method=orb` | Speed baseline. Binary descriptors, Hamming matching. **Not measured in this revamp.** |
| **L1 SIFT on albedo** | SIFT after illumination is divided out | `--set match.method=sift` | Isolates the physics contribution. L1 minus L0 **is** the photometry claim. |
| **L2 RIFT on phase congruency** | keypoints and descriptors from the phase-congruency map | `--set match.method=rift` (or the `auto` default past 20° Δsun) | Isolates the illumination-invariant-feature contribution, and is the path that carries OHRC scale where the DEM cannot (`docs/decisions.md` D2). |

The `auto` arm below is the shipped default: it resolves to L1 (`sift`) below a 20° sun-azimuth
difference and to L2 (`rift`) at or above it, per pair, and records which it chose in
`metrics.json["match_method_resolved"]`.

What follows measures the `auto` and pinned-`sift` arms and 16 one-switch ablation arms. **The
L0 rung as defined above — canonicaliser off *and* SIFT pinned, together — is not among them**:
section 2's `canonicaliser_off` leaves the matcher on `auto`. L0 and the ORB rung are not
measured in this revamp.

---

## 1. Δsun sweep — synthetic, MEASURED 2026-09-02

Fixture set `fixtures/dsun_sweep/`, 14 pairs. 512x512 reference at 1 m GSD against an
858x858 source at 0.5 m (2x scale ratio), 10° rotation, a deliberately imperfect source
geotransform (36.2 source px of corner error), seed 0. **Geometry, DEM, albedo, sun
elevation and noise are byte-identical in all 14 pairs; only the source sun azimuth
changes.** Regenerate with `python -m synth.sweep`.

Reproduce this table:

```
python -m synth.sweep                                     # renders any missing pair, reuses the rest
python -m bench.harness sweep --manifest fixtures/dsun_sweep/manifest.json \
                              --out runs/dsun_sweep_full
```

Two arms, 28 runs. All 28 ran to completion; 8 of them delivered no model, which is
itself a result and is marked as such in the tables:

* **`auto`** — the shipped default config, nothing pinned. `match.method`,
  `photometry.phase_congruency` and `photometry.clahe` all resolve per pair.
* **`sift`** — `match.method=sift` pinned, everything else default. The contrast case.

Four things to hold while reading the columns:

1. The column header is the **world** Δsun of the fixture. The source raster is rotated 10°,
   so the difference the engine reads off the two sidecars is |Δ - 10|, and that is what the
   `auto` rule sees. At Δ20 the engine sees 10°, below the bar, so it picks `sift`; at Δ30 it
   sees 20°, at the bar, so it flips to `rift`. That is why the Δ0-Δ20 columns of the two arms
   are identical: two separate runs whose resolved config is the same. Only the wall-clock row
   differs there, and that is machine contention, not the pipeline.
2. `gt_rmse_px` is TRUE error in source pixels against the analytic homography in `gt.json`,
   evaluated through the full delivered model including the TPS. `check_rmse_px` is measured
   on tie-points held out of the fit entirely. `rmse_px` (in-sample) is not tabulated here.
3. In `runs/dsun_sweep_full/dsun_sweep.csv` that world Δ is the `manifest_delta_sun_az_deg`
   column. `delta_sun_az_deg` there is the pipeline's own measurement off the sidecars and
   matches each run's `metrics.json` — 170 where this table's header says 180.
4. **The canonicalisation cache was disabled for every run in this section**
   (`cache.enabled=false`). It has to be: its key is product id + file size + integer mtime +
   shape, with no path, so two sweep pairs that differ only in illumination collide. Measured
   with the cache on, Δ60 and Δ70 returned byte-identical `gt_rmse_px` 2.109761539475777 and
   73 inliers; with it off they are 2.110 and 3.514 px, 73 and 49 inliers. See the note at the
   end of this section.

**True error against ground truth — `gt_rmse_px` (source px)**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 0.024 | 0.041 | 0.206 | 1.143 | 0.895 | 1.887 | 2.110 | 3.514 | 4.512 | 5.111 | 4.497 | 6.824 | 3.163 | 0.592 |
| `sift` | 0.024 | 0.041 | 0.206 | 0.435 | 2.052 | 5.610 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**Held-out tie-point error — `check_rmse_px` (source px)**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 0.218 | 0.366 | 0.391 | 1.415 | 1.175 | 1.174 | 1.274 | 1.568 | 1.152 | 1.051 | 1.433 | 1.449 | 1.751 | 1.298 |
| `sift` | 0.218 | 0.366 | 0.391 | n/a | n/a | n/a | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**Delivered tie-points — `inlier_count`**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 790 | 780 | 292 | 183 | 142 | 99 | 73 | 49 | 49 | 50 | 51 | 46 | 59 | 64 |
| `sift` | 790 | 780 | 292 | 69 | 23 | 9 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**Grid coverage — `coverage_pct`**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 100.0 | 87.5 | 100.0 | 93.8 | 100.0 | 87.5 | 100.0 | 93.8 |
| `sift` | 100.0 | 100.0 | 100.0 | 100.0 | 81.2 | 37.5 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**Spatial distribution index — `sdi`**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 0.982 | 0.971 | 0.825 | 0.770 | 0.735 | 0.749 | 0.680 | 0.553 | 0.652 | 0.595 | 0.662 | 0.534 | 0.626 | 0.622 |
| `sift` | 0.982 | 0.971 | 0.825 | 0.681 | 0.480 | 0.148 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**`inlier_ratio` (the plan's 0.85 bar)**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 0.994 | 0.986 | 0.930 | 0.691 | 0.628 | 0.572 | 0.477 | 0.389 | 0.348 | 0.413 | 0.336 | 0.359 | 0.388 | 0.333 |
| `sift` | 0.994 | 0.986 | 0.930 | 0.639 | 0.160 | 0.062 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**Matcher actually used — `match_method_resolved`**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | sift | sift | sift | rift | rift | rift | rift | rift | rift | rift | rift | rift | rift | rift |
| `sift` | sift | sift | sift | sift | sift | sift | sift | sift | sift | sift | sift | sift | sift | sift |

**Model delivered — `model_type`**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | homography | homography | homography | homography | homography+tps | homography | homography+tps | homography | homography+tps | homography | homography | similarity+tps | similarity | homography |
| `sift` | homography | homography | homography | homography | homography | homography | failed | failed | failed | failed | failed | failed | failed | failed |

**Wall clock, seconds (contended machine)**

| arm | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto` | 5.7 | 3.5 | 3.5 | 11.2 | 9.2 | 8.2 | 10.3 | 10.5 | 12.4 | 13.8 | 10.4 | 10.7 | 9.6 | 8.6 |
| `sift` | 5.0 | 5.1 | 5.9 | 4.5 | 2.9 | 3.0 | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** | **no model** |

**no model** = `verify_status: failed`, no transform was delivered. Those runs still write a
`gt_rmse_px` of 26.128 px, which is the error of the *prior* the pipeline falls back to and
not an accuracy figure; it is identical across all eight cells because the fixtures share one
geometry. Read the status row, not that number.

### What this says

1. **The pinned SIFT arm stops registering entirely at Δ60 and never comes back.** Zero
   inliers, `verify_status: failed`, in all eight cells from Δ60 to Δ180. It is not
   degradation, it is a cliff: 292 inliers at Δ20, 69 at Δ30, 23 at Δ40, 9 at Δ50, 0 at Δ60.
   The default `auto` arm delivers a model at all 14 steps.

2. **The hard case is orthogonal illumination, not opposite illumination.** True error on the
   `auto` arm peaks at Δ120 (6.824 px) and then *falls* to 3.163 px at Δ150 and 0.592 px at
   Δ180. Δ180 — the plan's literal "craters illuminated from opposite sun angles cast inverse
   shadows" case — is the **most** accurate cell in the entire RIFT range (Δ30-Δ180), below
   Δ40's 0.895 px and Δ30's 1.143 px. A phase-congruency descriptor keys on where the
   intensity structure is, and an inverted shadow is the same edge in the same place; a
   shadow rotated 90° is a different
   edge somewhere else. This is measured, it is the opposite of the intuition the plan is
   written on, and nothing in our docs said it before this sweep.

3. **`check_rmse_px` does not track true error.** Across the same eight `auto` cells where
   `gt_rmse_px` runs 2.110 -> 6.824 px, `check_rmse_px` stays inside 1.05-1.75 px. Held-out
   tie-points catch blunders and overfitting; they cannot catch a fit that is *consistent with
   its own tie-points and wrong*, which is what cross-illumination matching produces. On a real
   pair `check_rmse_px` is the only accuracy number we have, and this is the honest size of what
   it does not see. Quote it as held-out self-consistency, never as ground truth.

4. **The plan's 0.85 inlier-ratio bar is met at Δ0, Δ10 and Δ20 (0.994, 0.986, 0.930) and
   nowhere else.** The best value past that is 0.691 at Δ30 and the RIFT range sits near 0.35.
   `samanvay register` prints this as `inlier ratio vs plan FAIL`, and it should: the bar as
   written is a statement about easy pairs.

5. **Coverage and SDI degrade far more gently than accuracy.** `coverage_pct` never drops
   below 87.5% on the `auto` arm across the whole range and `sdi` falls from 0.982 to 0.534,
   while the pinned SIFT arm goes to 0.0% coverage from Δ60. Tie-point *spread* is not the
   thing that breaks under cross-illumination; tie-point *correctness* is.

### Defect found while measuring this (not ours to fix — reported)

`samanvay/pipeline/stages.py::_canonicalise_cached` keys the phase-congruency cache on
`(product_id, resolved photometry params, file size, int(mtime), shape)` and **not on the
path**. Every synthetic fixture carries `product_id: "synth_source"`, the same uncompressed
size and, when a sweep renders them in one go, the same integer mtime — so pair B silently
reuses pair A's albedo and phase congruency. Confirmed by hashing the key directly: Δ60 and
Δ70 (and Δ100 and Δ120) produce identical digests. Any benchmark over a rendered fixture
series must run with `cache.enabled=false` until the path is in the key.

---

## 2. Stage ablation — MEASURED 2026-09-03

Every arm is the shipped default configuration with exactly one key overridden, so a row
difference can only be caused by that one switch. Two named pairs (`quota10_*`, `dem_*`)
are one switch away from their OWN sub-baseline instead, because the switch they test does
nothing at all under the shipped one — see 2c. 21 arms, run twice: once on
`fixtures/synth_pair_A` (the README quickstart pair: 90° world sun-azimuth difference, 100°
as the engine reads it off the sidecars, and a -40° elevation difference on top) and once on
`fixtures/dsun_sweep/dsun_50` (50° world, 40° as read). synth_pair_A is a 1715x1715 source at
0.5 m GSD against a 1024x1024 reference at 1 m, 10° rotation, 2x scale ratio, 49.2 source px of
deliberate prior error, seed 0. The cache is off in both runs, for the reason in section 1.

```
python -m bench.ablate --source fixtures/synth_pair_A/source.tif \
                       --ref    fixtures/synth_pair_A/reference.tif \
                       --dem    fixtures/synth_pair_A/dem.tif --out runs/ablate_A
python -m bench.ablate --source fixtures/dsun_sweep/dsun_50/source.tif \
                       --ref    fixtures/dsun_sweep/dsun_50/reference.tif \
                       --dem    fixtures/dsun_sweep/dsun_50/dem.tif --out runs/ablate_dsun50
```

**Two columns are new and both exist to stop a row lying by omission.**

* `cells quota-bound` — how many grid cells had more candidates than `match.max_matches`,
  i.e. how many times the per-cell selection rule ran at all. `match/tile.py` only enters
  the ANMS/score-sort branch inside `if len(k_src) > max_matches`, so `0/16` means the arm
  under test never executed and its row measures nothing. It is read off `cell["anms"]`,
  which is null until the branch runs.
* `quad/cell` — the mean number of the four sub-quadrants of a cell core that the cell's
  delivered inliers occupy, 1.0 (all in one corner) to 4.0. `coverage_pct` and
  `dispersion_cv` are both computed PER CELL and score a cell 100% / 0.0 whether its points
  fill it or pile into one corner of it, so neither can see what ANMS is for.
  `bench/harness.py::_cell_spread`.

**Repeatability, measured before any of this was read as a delta:** four consecutive runs of
`baseline_full` on each fixture returned bit-identical `gt_rmse_px`, `check_rmse_px`,
`inlier_count` and `sdi` (1.8873479555747705 / 1.1740808755561176 / 99 / 0.7494845351091993
on dsun_50; 2.9287054567839337 / 1.5712308090704932 / 64 / 0.5784128280412532 on
synth_pair_A; `runs/repeat/`). The noise floor is zero, so every difference below is caused
by the switch and nothing else. It also means a row identical to the baseline is a stage
that did not run, never a coincidence.

### 2a. `fixtures/synth_pair_A` (Δsun azimuth 100° as seen by the matcher, Δelevation -40°)

| arm | verify | gt rmse px | check rmse px | in-sample rmse px | inliers | matches | inlier ratio | cov % | sdi | quad/cell | cells quota-bound | model | tps | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `baseline_full` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 64.7 |
| `canonicaliser_off` | failed | 28.665 | n/a | n/a | 0 | 0 | n/a | 0.0 | n/a | n/a | n/a | failed | too_few_control | 11.8 |
| `phase_congruency_off` | failed | 28.665 | n/a | n/a | 0 | 0 | n/a | 0.0 | n/a | n/a | n/a | failed | too_few_control | 8.6 |
| `photometric_model_none` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 46.4 |
| `photometric_lunar_lambert` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 41.4 |
| `mask_fill_zero` | ok | 2.929 | 1.571 | 1.260 | 64 | 217 | 0.295 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 38.5 |
| `clahe_on` | ok | 6.757 | 2.791 | 2.167 | 23 | 319 | 0.072 | 68.8 | 0.352 | 1.727 | 1/16 | affine | too_few_control | 42.1 |
| `clahe_off` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 53.2 |
| `anms_off` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 41.7 |
| `quota10_baseline` | ok | 3.013 | 2.171 | 1.752 | 38 | 136 | 0.279 | 93.8 | 0.629 | 2.067 | 9/16 | homography | rejected_no_improvement | 43.1 |
| `quota10_anms_off` | ok | 3.179 | 0.757 | 1.801 | 46 | 132 | 0.348 | 93.8 | 0.590 | 1.867 | 9/16 | homography | rejected_no_improvement | 53.0 |
| `uniformity_off` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 48.1 |
| `quota10_uniformity_off` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 40.1 |
| `matcher_sift` | failed | 28.951 | n/a | n/a | 0 | 154 | 0.000 | 0.0 | n/a | n/a | 0/16 | failed | too_few_control | 7.1 |
| `subpixel_off` | ok | 3.523 | 1.661 | 2.158 | 48 | 218 | 0.220 | 93.8 | 0.526 | 2.067 | 0/16 | homography | rejected_no_improvement | 43.4 |
| `tps_off` | ok | 3.235 | 1.853 | 1.838 | 64 | 218 | 0.294 | 100.0 | 0.565 | 2.25 | 0/16 | homography | disabled | 36.2 |
| `tps_forced` | ok | 2.929 | 1.571 | 1.260 | 64 | 218 | 0.294 | 100.0 | 0.578 | 2.25 | 0/16 | homography+tps | applied | 56.0 |
| `check_split_off` | ok | 3.221 | n/a | 1.632 | 59 | 212 | 0.278 | 93.8 | 0.521 | 2.2 | 0/16 | homography | rejected_no_improvement | 22.5 |
| `dem_baseline` | ok | 3.185 | 2.039 | 1.611 | 52 | 215 | 0.242 | 93.8 | 0.560 | 2.2 | 0/16 | homography | rejected_no_improvement | 20.7 |
| `dem_photometric_model_none` | ok | 3.148 | 1.806 | 1.634 | 62 | 249 | 0.249 | 100.0 | 0.606 | 2.188 | 0/16 | homography | rejected_no_improvement | 20.1 |
| `dem_lunar_lambert` | ok | 3.269 | 1.385 | 1.560 | 53 | 174 | 0.305 | 93.8 | 0.530 | 2.067 | 0/16 | homography | rejected_no_improvement | 19.4 |

### 2b. `fixtures/dsun_sweep/dsun_50` (Δsun azimuth 40° as seen by the matcher, same elevation)

| arm | verify | gt rmse px | check rmse px | in-sample rmse px | inliers | matches | inlier ratio | cov % | sdi | quad/cell | cells quota-bound | model | tps | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `baseline_full` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 8.1 |
| `canonicaliser_off` | failed | 26.128 | n/a | n/a | 0 | 0 | n/a | 0.0 | n/a | n/a | n/a | failed | too_few_control | 1.6 |
| `phase_congruency_off` | failed | 26.128 | n/a | n/a | 0 | 0 | n/a | 0.0 | n/a | n/a | n/a | failed | too_few_control | 1.7 |
| `photometric_model_none` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 8.0 |
| `photometric_lunar_lambert` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 7.6 |
| `mask_fill_zero` | ok | 0.931 | 1.016 | 0.867 | 110 | 194 | 0.567 | 100.0 | 0.770 | 3.188 | 0/16 | homography+tps | applied | 8.2 |
| `clahe_on` | ok | 0.664 | 1.322 | 0.914 | 94 | 173 | 0.543 | 100.0 | 0.635 | 2.938 | 0/16 | homography+tps | applied | 7.1 |
| `clahe_off` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 6.6 |
| `anms_off` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 5.7 |
| `quota10_baseline` | ok | 1.396 | 1.314 | 1.085 | 83 | 141 | 0.589 | 100.0 | 0.727 | 2.812 | 12/16 | homography+tps | applied | 6.3 |
| `quota10_anms_off` | ok | 1.652 | 1.105 | 1.390 | 83 | 130 | 0.638 | 100.0 | 0.724 | 2.688 | 12/16 | homography | rejected_no_improvement | 5.9 |
| `uniformity_off` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 5.3 |
| `quota10_uniformity_off` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | rejected_no_improvement | 5.0 |
| `matcher_sift` | ok | 5.610 | n/a | 1.406 | 9 | 146 | 0.062 | 37.5 | 0.148 | 1.5 | 0/16 | homography | too_few_control | 1.6 |
| `subpixel_off` | ok | 1.920 | 1.781 | 1.916 | 61 | 173 | 0.353 | 100.0 | 0.711 | 2.375 | 0/16 | homography | rejected_no_improvement | 4.9 |
| `tps_off` | ok | 1.887 | 1.174 | 1.445 | 99 | 173 | 0.572 | 100.0 | 0.749 | 2.875 | 0/16 | homography | disabled | 5.0 |
| `tps_forced` | ok | 1.473 | 1.221 | 1.062 | 100 | 173 | 0.578 | 100.0 | 0.748 | 2.875 | 0/16 | homography+tps | applied | 5.3 |
| `check_split_off` | ok | 2.075 | n/a | 1.415 | 100 | 173 | 0.578 | 100.0 | 0.743 | 2.812 | 0/16 | homography | rejected_no_improvement | 4.8 |
| `dem_baseline` | ok | 1.598 | 1.396 | 1.036 | 102 | 177 | 0.576 | 100.0 | 0.764 | 3.125 | 0/16 | homography+tps | applied | 5.7 |
| `dem_photometric_model_none` | ok | 1.762 | 1.260 | 1.427 | 102 | 178 | 0.573 | 100.0 | 0.767 | 3.062 | 0/16 | homography | rejected_no_improvement | 5.2 |
| `dem_lunar_lambert` | ok | 1.258 | 1.463 | 0.974 | 99 | 172 | 0.576 | 100.0 | 0.755 | 3.125 | 0/16 | homography+tps | applied | 5.7 |

A `gt rmse px` on a row whose `verify` is `failed` is the fallback prior's error, not an
accuracy figure.

### 2c. Every row that is identical to its baseline, and why

The 2026-09-02 edition of this table had rows byte-identical to `baseline_full` and did not
say which of them were stages that did nothing and which were stages that never ran. An
ablation where a third of the rows are inert and unlabelled reads as "these stages do
nothing", which is the opposite of what the table exists to show.

The list below is now exhaustive and is checked mechanically rather than by eye — the rows
that match `baseline_full` on all of `gt_rmse_px`, `check_rmse_px`, `rmse_px`, `inlier_count`,
`coverage_pct`, `dispersion_cv`, `sdi`, `match_count` and `model_type` in
`runs/ablate_A/ablation.csv` and `runs/ablate_dsun50/ablation.csv` are **seven on each
fixture**, and every one of the seven appears here. (The 2026-09-03 edition of this section
listed six and missed `clahe_off`, `tps_forced` and `tps_off`, which is the same defect one
level down.)

| row | identical to | why | verdict |
|---|---|---|---|
| `anms_off` | `baseline_full` | The ANMS/score-sort branch is inside `if len(k_src) > max_matches` (`match/tile.py`). At the shipped `max_matches=50` the quota binds on **0 of 16** cells on both fixtures, so no selection rule of either kind ran. `cell["anms"]` is null in all 16 cells. | **measures nothing here.** Not a bug, not a useless stage — an untested one. Superseded by 2d. |
| `uniformity_off` | `baseline_full` | `config.cell_budgets` returns `None` when `match.uniformity` is false, and `match/tile.py` then falls back to `budget.get("min_matches", 5)` / `("max_matches", 50)` — exactly the 5 and 50 the config was asking for. Switching the quotas off restores the quotas. | **inert by construction at the shipped quotas.** `quota10_uniformity_off` is the row that proves it: at `max_matches=10` it reproduces `baseline_full` (2.929 px / 64 inliers on A, 1.887 px / 99 on dsun_50) rather than `quota10_baseline`, which is the quota being removed doing exactly what the code says it does. |
| `photometric_model_none` | `baseline_full` | No DEM is passed, so `photometry/normalize.py` takes the `empirical` illumination branch and never consults `photometric_model` at all (`illum_mode: "empirical"` in both runs' metrics). | **inert without a DEM.** Superseded by the `dem_*` rows in 2e, which are real. |
| `photometric_lunar_lambert` | `baseline_full` | Same. | Same. |
| `matcher_l2` | `baseline_full` | `match/tile.py:54` maps `"l2"` and `"rift"` to the same descriptor id and `match/describe.py:145` says so in as many words. `l2` *is* the baseline's matcher under an older name. | **removed from the arm list.** A row that can only ever equal the baseline is not an independent arm. |
| `clahe_off` (both fixtures) | `baseline_full` | Not an ablation of anything: `photometry.clahe` ships as `auto`, and `auto` resolves to **off** on the RIFT arm (`clahe_resolved: false`, reason "the rift descriptor reads the phase-congruency map"). Both fixtures resolve to RIFT, so `clahe_off` *is* the shipped configuration spelled out. | **the default written out.** Identical by definition, and useful only as a check that `auto` resolved the way the config comment says. The arm that moves is `clahe_on`. |
| `tps_forced` on synth_pair_A | `baseline_full` | `geometry.tps: auto` already **accepted** the spline on this pair (`tps_status: applied`), so forcing it asks for a decision that had already been taken. | **no-op on this fixture, live on the other.** On dsun_50 `auto` rejects and `tps_forced` moves the row a long way: 1.887 -> 1.473 px gt. |
| `tps_off` on dsun_50 | `baseline_full` | The mirror image: `auto` **rejected** the spline here (`tps_status: rejected_no_improvement`), so the delivered model was already homography-only and disabling the TPS removes nothing. | **no-op on this fixture, live on the other.** On synth_pair_A `tps_off` costs 2.929 -> 3.235 px. Read the two `tps_*` rows as one pair across both fixtures, never one row on one fixture. |
| `mask_fill_zero` on synth_pair_A | `baseline_full` | Here the switch IS live: the baseline fills 32,343 invalid source pixels (`mask_fill_px: 32343`) and this arm fills none (`mask_fill_px: 0`), so the two phase-congruency inputs genuinely differ. The run still lands on the same 64 inliers and the same RMSE to four decimals, with 217 putatives against 218. | **a real measured null result on this pair**, not a stage that did not run. It is emphatically not null on dsun_50 (0.931 vs 1.887 px) or across the sweep (2f). |

### 2d. ANMS — where the per-cell quota actually binds, and what ANMS does there

**On the shipped fixtures at the shipped `match.max_matches=50`, it does not bind at all,
and the `anms_off` row above is therefore worthless.** Measured directly, by raising the
quota out of the way and then walking it down:

```
for q in 100000 50 30 20 10 6; do
  samanvay register --source fixtures/<pair>/source.tif --ref fixtures/<pair>/reference.tif \
      --out runs/anms_quota/<pair>_q${q}_true --no-cache \
      --set match.max_matches=$q --set match.anms=true
done
```

| fixture | delivered tie-points per cell, unbounded quota | cells bound at max_matches 50 / 30 / 20 / 10 / 6 |
|---|---|---|
| `synth_pair_A` | 5, 5, 7, 7, 8, 8, 9, 9, 12, 13, 13, 13, 15, 20, 25, **49** | 0/16, 1/16, 2/16, 9/16, 14/16 |
| `dsun_50` | 6, 6, 7, 9, 9, 9, 9, 10, 11, 12, 12, 14, 14, 15, 15, 15 | 0/16, 0/16, 2/16, 12/16, 16/16 |

The busiest cell on synth_pair_A holds 49 candidates against a quota of 50. It misses by
one. (The counts above are what survives the location dedup at the end of `match_tiled`,
while the quota is applied before it — which is why dsun_50 binds 2 cells at a quota of 20
even though nothing it delivers exceeds 15.)

**On real LROC NAC it binds at the shipped default, unaided.** Same 4-along-the-short-axis
grid, on 872x9296 to 888x11952 strips, so 160-216 cells instead of 16:

| real pair | cells | cells quota-bound at `max_matches=50` |
|---|---|---|
| `apollo16_dsun004` | 172 | **168 (98%)** |
| `apollo16_dsun085` | 160 | 7 (4%) |
| `apollo16_dsun115` | 216 | **95 (44%)** |

So the honest statement is: **ANMS is inert on the small synthetic fixtures and active on
real NAC imagery**, and the 2026-09-02 ablation measured it on exactly the data where it
cannot run.

#### What it does where it does bind — fixtures, quota forced down

```
for q in 20 10 6; do for a in true false; do
  samanvay register --source fixtures/<pair>/source.tif --ref fixtures/<pair>/reference.tif \
      --out runs/anms_quota/<pair>_q${q}_${a} --no-cache \
      --set match.max_matches=$q --set match.anms=$a
done; done
```

| pair | max_matches | anms | cells quota-bound | gt rmse px | check rmse px | inliers | matches | cov % | dispersion cv | sdi | quad/cell | inliers/cell |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `synth_pair_A` | 20 | on | 2/16 | 3.161 | 1.835 | 58 | 184 | 93.8 | 0.7092 | 0.549 | 2.267 | 3.867 |
| `synth_pair_A` | 20 | off | 2/16 | 3.510 | 1.559 | 57 | 184 | 93.8 | 0.6801 | 0.558 | 2.2 | 3.8 |
| `synth_pair_A` | 10 | on | 9/16 | 3.013 | 2.171 | 38 | 136 | 93.8 | 0.4909 | 0.629 | 2.067 | 2.533 |
| `synth_pair_A` | 10 | off | 9/16 | 3.179 | 0.757 | 46 | 132 | 93.8 | 0.5882 | 0.590 | 1.867 | 3.067 |
| `synth_pair_A` | 6 | on | 14/16 | 3.190 | 1.333 | 28 | 94 | 87.5 | 0.6547 | 0.529 | 1.857 | 2.0 |
| `synth_pair_A` | 6 | off | 14/16 | 3.522 | 2.154 | 28 | 92 | 87.5 | 0.6851 | 0.519 | 1.5 | 2.0 |
| `dsun_50` | 20 | on | 2/16 | 1.887 | 1.174 | 99 | 173 | 100.0 | 0.3343 | 0.749 | 2.875 | 6.188 |
| `dsun_50` | 20 | off | 2/16 | 1.370 | 1.333 | 96 | 172 | 100.0 | 0.3632 | 0.734 | 2.812 | 6.0 |
| `dsun_50` | 10 | on | 12/16 | 1.396 | 1.314 | 83 | 141 | 100.0 | 0.3747 | 0.727 | 2.812 | 5.188 |
| `dsun_50` | 10 | off | 12/16 | 1.652 | 1.105 | 83 | 130 | 100.0 | 0.3808 | 0.724 | 2.688 | 5.188 |
| `dsun_50` | 6 | on | 16/16 | 1.547 | 1.698 | 61 | 95 | 93.8 | 0.3735 | 0.683 | 2.867 | 4.067 |
| `dsun_50` | 6 | off | 16/16 | 1.249 | 1.061 | 52 | 83 | 100.0 | 0.4142 | 0.707 | 2.062 | 3.25 |

#### What it does where it does bind — real LROC NAC, shipped `max_matches=50`

```
python -m bench.harness sweep --manifest bench/real_pairs.yaml \
    --arm anms_on --arm anms_off --arm reflect_clahe_off \
    --skip-pair apollo16_dsun004 --out runs/real_arms
```

| pair | anms | cells quota-bound | check rmse px | inliers | matches | cov % | dispersion cv | sdi | quad/cell | inliers/cell | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|
| `apollo16_dsun085` | on | 7/160 | 1.949 | 163 | 5023 | 49.7 | 1.4676 | 0.201 | 1.456 | 2.063 | 74.8 |
| `apollo16_dsun115` | on | 95/216 | 1.313 | 4266 | 5230 | 100.0 | 0.4029 | 0.713 | 3.562 | 31.139 | 79.1 |
| `apollo16_dsun085` | off | 7/160 | 2.085 | 158 | 5009 | 48.4 | 1.4209 | 0.200 | 1.403 | 2.052 | 92.3 |
| `apollo16_dsun115` | off | 95/216 | 1.223 | 3633 | 4331 | 100.0 | 0.3749 | 0.727 | 3.409 | 26.518 | 86.0 |

Read the two blocks together and the ANMS claim is narrow and defensible:

* **Within-cell spread: ANMS wins 8 of 8.** `quad/cell` is higher with ANMS on in every one
  of the six forced-quota fixture comparisons and both real-pair comparisons — 1.5 -> 1.857
  at the extreme (`synth_pair_A`, quota 6) and 2.062 -> 2.867 on `dsun_50` at quota 6. This
  is the one thing ANMS is for and the one thing the grid cannot do on its own, and it is
  the only column with a consistent direction.
* **Accuracy: no direction, in either sign.** Over the six fixture comparisons `gt_rmse_px`
  favours ANMS 4-2 and `check_rmse_px` favours the score sort 4-2; on the two real pairs
  `check_rmse_px` is one each way (1.313 vs 1.223 on dsun115, 1.949 vs 2.085 on dsun085).
  There is no honest reading of this table in which ANMS buys accuracy, and none in which it
  costs it either.
* **Tie-point yield: no cost overall, but it is not free everywhere.** 3 wins / 1 loss /
  2 ties on the fixtures and 2-0 on the real pairs (4266 vs 3633 inliers on dsun115, 163 vs
  158 on dsun085). The loss is real and is the largest single difference in the column:
  `synth_pair_A` at quota 10 delivers **38 inliers with ANMS against 46 without**. Spreading
  the quota over the cell core can pick points that later fail RANSAC where the score sort's
  top-10 would have survived, and on that one row it costs 8 tie-points.
* **`dispersion_cv` and `sdi` do not agree with each other across the two data sets** —
  ANMS improves `dispersion_cv` on 5 of 6 fixture comparisons and worsens it on both real
  pairs. Both are per-cell statistics, so this is a reminder that neither of them measures
  in-cell spread; `quad/cell` does.
* **Cost: nothing that rises above the measurement noise.** 79.1 s (on) vs 86.0 s (off) on
  dsun115 and 74.8 s vs 92.3 s on dsun085 — ANMS is the *faster* arm in both, which it has no
  business being, so read this row as "no measurable cost" and not as a speed-up. These
  laptop timings were taken with other jobs running.

The claim this evidence supports is therefore: *ANMS distributes tie-points inside a cell
where a score sort clusters them, at no net measured accuracy or tie-point cost across eight
paired comparisons and no measured runtime cost, and it engages on real NAC imagery at the
shipped quota where it does not engage on the small synthetic fixtures.* "No net cost" is the
honest word: one of the eight comparisons costs 8 tie-points and four of the eight cost
held-out accuracy. The claim it does NOT support is that ANMS improves registration
accuracy. `bench/ablate.py`'s `anms_off` row alone supports neither.

### 2e. The photometric model is only an arm when there is a DEM

All three fixtures ship a `dem.tif`, and `bench.ablate --dem` now uses it. Without one,
`photometry/normalize.py` reports `illum_mode: "empirical"` and never reads
`photometric_model`; with one it reports `illum_mode: "dem_lowfreq"` and the model is live.
`dem_baseline` is the comparison row for the two model arms, because a `dem_*` row differs
from `baseline_full` by the DEM as well as by the model.

| fixture | arm | illum_mode | gt rmse px | check rmse px | inliers | sdi |
|---|---|---|---|---|---|---|
| `synth_pair_A` | `baseline_full` (no DEM) | empirical | 2.929 | 1.571 | 64 | 0.578 |
| `synth_pair_A` | `dem_baseline` (lommel_seeliger) | dem_lowfreq | 3.185 | 2.039 | 52 | 0.560 |
| `synth_pair_A` | `dem_photometric_model_none` | dem_lowfreq | 3.148 | 1.806 | 62 | 0.606 |
| `synth_pair_A` | `dem_lunar_lambert` | dem_lowfreq | 3.269 | 1.385 | 53 | 0.530 |
| `dsun_50` | `baseline_full` (no DEM) | empirical | 1.887 | 1.174 | 99 | 0.749 |
| `dsun_50` | `dem_baseline` (lommel_seeliger) | dem_lowfreq | 1.598 | 1.396 | 102 | 0.764 |
| `dsun_50` | `dem_photometric_model_none` | dem_lowfreq | 1.762 | 1.260 | 102 | 0.767 |
| `dsun_50` | `dem_lunar_lambert` | dem_lowfreq | 1.258 | 1.463 | 99 | 0.755 |

What this says, and it is not comfortable: **the model is live and it moves the numbers, but
the shipped `lommel_seeliger` is not the best of the three on either metric on either
fixture.** On true error it is second on synth_pair_A (3.185, behind `none` at 3.148) and
second on dsun_50 (1.598, behind `lunar_lambert` at 1.258); on held-out error it is *last*
on synth_pair_A (2.039) and second on dsun_50 (1.396). The three models sit inside 0.5 px of
each other and the ordering flips between two fixtures, so this is not a mandate to change
the default — two synthetic fixtures cannot settle a photometric-model choice — but it is
also not the evidence for `lommel_seeliger` that the config comment implies. **What is
needed is this table on real pairs with a real DEM, which we have not run.**

Note also that turning the DEM ON costs accuracy on synth_pair_A (2.929 -> 3.185 px, 64 -> 52
inliers) and gains it on dsun_50 (1.887 -> 1.598 px). Both fixtures' DEMs are synthetic
renders, so this says nothing about a real DEM either way.

### 2f. `mask_fill` and `clahe` across the whole 0-180° Δsun sweep

The 2026-09-02 ablation measured both switches on two fixtures, and the two fixtures
disagreed: on `dsun_50` both `mask_fill_zero` (0.931 px) and `clahe_on` (0.664 px) beat the
shipped default (1.887 px) by roughly 2x, while on `synth_pair_A` `mask_fill_zero` was
identical and `clahe_on` was 2.3x worse. Two fixtures cannot settle a default. All four
combinations were therefore run across all 14 steps of the Δsun sweep:

```
python -m bench.harness sweep --manifest fixtures/dsun_sweep/manifest.json \
    --arm reflect_clahe_off --arm zero_clahe_off --arm reflect_clahe_on --arm zero_clahe_on \
    --out runs/photom_grid
```

**Which arm each row belongs to is not the same for all 14 steps, and that decides how the
table is read.** `match.method: auto` resolves on the pair's measured Δsun azimuth: below 20°
it picks SIFT, at or above it picks RIFT. Read off the sidecars the fixture rows named
`dsun_000`, `dsun_010` and `dsun_020` measure 10°, 0° and 10° of azimuth difference (the
manifest label is the world Δsun, the engine sees the rotated raster — section 1), so
`match_method_resolved` is `sift` on those three rows and `rift` on the other eleven. `photometry.clahe: auto`
resolves the opposite way — **on** for the intensity arm, **off** for RIFT — so
`reflect_clahe_off` is the shipped configuration only for the eleven RIFT steps, and
`reflect_clahe_on` is the shipped configuration for the three SIFT steps. The two groups are
tallied separately below and mixing them would have inverted the answer.

`fill px` is `mask_fill_px` from the run — how many invalid pixels the fill actually touched,
and it is only ever non-zero on a `reflect` row, because `zero` performs no fill by definition.
**Where the `reflect` row's `fill px` is 0 the `mask_fill` switch is not live and the two arms
are identical by construction, not by measurement**: that is the same Δsun 0, 10 and 20.

The reason it is 0 there is NOT that the fixture has no invalid pixels — it has plenty.
`mask_fill` only ever touches the phase-congruency input (`photometry/normalize.py`), and on
those three steps `auto` resolves to SIFT, so `phase_congruency_resolved` is false,
`pc_status` is `disabled` and there is no PC map to fill. Measured directly, with the matcher
pinned so the PC map is built:

```
for f in reflect zero; do
  samanvay register --source fixtures/dsun_sweep/dsun_20/source.tif \
      --ref fixtures/dsun_sweep/dsun_20/reference.tif --out runs/dsun20_rift_$f --no-cache \
      --set match.method=rift --set photometry.clahe=false --set photometry.mask_fill=$f
done
reflect  mask_fill_px 8250   pc_status computed   gt 0.465  check 0.886  224 inliers
zero     mask_fill_px    0   pc_status computed   gt 0.295  check 0.843  263 inliers
```

So both switches are live on exactly the eleven RIFT steps of the `auto` sweep — not because
the other three have nothing to fill, but because the shipped default does not build the map
the fill acts on there.

| pair | matcher | mask_fill | clahe | fill px | gt rmse px | check rmse px | inliers | matches | cov % | sdi |
|---|---|---|---|---|---|---|---|---|---|---|
| `dsun_000` | sift | reflect | off | 0 | 0.034 | 0.224 | 775 | 783 | 100.0 | 0.975 |
| `dsun_000` | sift | zero | off | 0 | 0.034 | 0.224 | 775 | 783 | 100.0 | 0.975 |
| `dsun_000` | sift | reflect | on | 0 | 0.024 | 0.218 | 790 | 795 | 100.0 | 0.982 |
| `dsun_000` | sift | zero | on | 0 | 0.024 | 0.218 | 790 | 795 | 100.0 | 0.982 |
| `dsun_010` | sift | reflect | off | 0 | 0.083 | 0.430 | 741 | 759 | 100.0 | 0.939 |
| `dsun_010` | sift | zero | off | 0 | 0.083 | 0.430 | 741 | 759 | 100.0 | 0.939 |
| `dsun_010` | sift | reflect | on | 0 | 0.041 | 0.366 | 780 | 791 | 100.0 | 0.971 |
| `dsun_010` | sift | zero | on | 0 | 0.041 | 0.366 | 780 | 791 | 100.0 | 0.971 |
| `dsun_020` | sift | reflect | off | 0 | 0.321 | 0.734 | 209 | 233 | 100.0 | 0.765 |
| `dsun_020` | sift | zero | off | 0 | 0.321 | 0.734 | 209 | 233 | 100.0 | 0.765 |
| `dsun_020` | sift | reflect | on | 0 | 0.206 | 0.391 | 292 | 314 | 100.0 | 0.825 |
| `dsun_020` | sift | zero | on | 0 | 0.206 | 0.391 | 292 | 314 | 100.0 | 0.825 |
| `dsun_030` | rift | reflect | off | 8152 | 1.143 | 1.415 | 183 | 265 | 100.0 | 0.770 |
| `dsun_030` | rift | zero | off | 0 | 0.950 | 1.089 | 199 | 276 | 100.0 | 0.812 |
| `dsun_030` | rift | reflect | on | 8152 | 0.530 | 0.621 | 190 | 298 | 100.0 | 0.733 |
| `dsun_030` | rift | zero | on | 0 | 0.521 | 0.653 | 201 | 314 | 100.0 | 0.768 |
| `dsun_040` | rift | reflect | off | 8155 | 0.895 | 1.175 | 142 | 226 | 100.0 | 0.735 |
| `dsun_040` | rift | zero | off | 0 | 0.851 | 1.033 | 151 | 238 | 100.0 | 0.786 |
| `dsun_040` | rift | reflect | on | 8155 | 0.478 | 1.019 | 135 | 226 | 100.0 | 0.673 |
| `dsun_040` | rift | zero | on | 0 | 0.561 | 1.068 | 152 | 248 | 100.0 | 0.719 |
| `dsun_050` | rift | reflect | off | 8101 | 1.887 | 1.174 | 99 | 173 | 100.0 | 0.749 |
| `dsun_050` | rift | zero | off | 0 | 0.931 | 1.016 | 110 | 194 | 100.0 | 0.770 |
| `dsun_050` | rift | reflect | on | 8101 | 0.664 | 1.322 | 94 | 173 | 100.0 | 0.635 |
| `dsun_050` | rift | zero | on | 0 | 0.758 | 1.222 | 96 | 185 | 100.0 | 0.696 |
| `dsun_060` | rift | reflect | off | 7784 | 2.110 | 1.274 | 73 | 153 | 100.0 | 0.680 |
| `dsun_060` | rift | zero | off | 0 | 1.734 | 1.487 | 87 | 159 | 100.0 | 0.687 |
| `dsun_060` | rift | reflect | on | 7784 | 1.788 | 2.087 | 62 | 143 | 100.0 | 0.643 |
| `dsun_060` | rift | zero | on | 0 | 2.054 | 1.958 | 63 | 159 | 100.0 | 0.645 |
| `dsun_070` | rift | reflect | off | 7611 | 3.514 | 1.568 | 49 | 126 | 87.5 | 0.553 |
| `dsun_070` | rift | zero | off | 0 | 4.354 | 1.799 | 52 | 131 | 93.8 | 0.614 |
| `dsun_070` | rift | reflect | on | 7611 | 2.598 | 1.554 | 40 | 166 | 87.5 | 0.504 |
| `dsun_070` | rift | zero | on | 0 | 1.482 | 1.587 | 43 | 157 | 87.5 | 0.496 |
| `dsun_080` | rift | reflect | off | 7366 | 4.512 | 1.152 | 49 | 141 | 100.0 | 0.652 |
| `dsun_080` | rift | zero | off | 0 | 3.801 | 1.521 | 57 | 140 | 100.0 | 0.656 |
| `dsun_080` | rift | reflect | on | 7366 | 1.305 | 2.164 | 36 | 152 | 100.0 | 0.652 |
| `dsun_080` | rift | zero | on | 0 | 2.118 | 2.323 | 29 | 147 | 87.5 | 0.529 |
| `dsun_090` | rift | reflect | off | 7251 | 5.111 | 1.051 | 50 | 121 | 93.8 | 0.595 |
| `dsun_090` | rift | zero | off | 0 | 4.794 | 1.150 | 57 | 127 | 100.0 | 0.614 |
| `dsun_090` | rift | reflect | on | 7251 | 3.021 | 2.312 | 34 | 145 | 93.8 | 0.616 |
| `dsun_090` | rift | zero | on | 0 | 1.931 | 1.300 | 39 | 190 | 93.8 | 0.624 |
| `dsun_100` | rift | reflect | off | 7085 | 4.497 | 1.433 | 51 | 152 | 100.0 | 0.662 |
| `dsun_100` | rift | zero | off | 0 | 5.984 | 1.212 | 53 | 153 | 100.0 | 0.632 |
| `dsun_100` | rift | reflect | on | 7085 | 3.227 | 1.198 | 30 | 145 | 100.0 | 0.669 |
| `dsun_100` | rift | zero | on | 0 | 4.536 | 2.096 | 33 | 158 | 81.2 | 0.514 |
| `dsun_120` | rift | reflect | off | 6919 | 6.824 | 1.449 | 46 | 128 | 87.5 | 0.534 |
| `dsun_120` | rift | zero | off | 0 | 5.399 | 1.515 | 51 | 126 | 93.8 | 0.586 |
| `dsun_120` | rift | reflect | on | 6919 | 1.821 | 1.816 | 41 | 141 | 93.8 | 0.598 |
| `dsun_120` | rift | zero | on | 0 | 1.955 | 1.968 | 42 | 141 | 93.8 | 0.561 |
| `dsun_150` | rift | reflect | off | 6898 | 3.163 | 1.751 | 59 | 152 | 100.0 | 0.626 |
| `dsun_150` | rift | zero | off | 0 | 4.563 | 2.106 | 50 | 170 | 100.0 | 0.704 |
| `dsun_150` | rift | reflect | on | 6898 | 0.967 | 1.511 | 93 | 206 | 100.0 | 0.732 |
| `dsun_150` | rift | zero | on | 0 | 1.278 | 1.692 | 98 | 213 | 100.0 | 0.760 |
| `dsun_180` | rift | reflect | off | 6841 | 0.592 | 1.298 | 64 | 192 | 93.8 | 0.622 |
| `dsun_180` | rift | zero | off | 0 | 3.366 | 2.234 | 57 | 190 | 93.8 | 0.611 |
| `dsun_180` | rift | reflect | on | 6841 | 1.041 | 1.401 | 119 | 239 | 100.0 | 0.778 |
| `dsun_180` | rift | zero | on | 0 | 0.837 | 1.363 | 123 | 249 | 100.0 | 0.763 |

`synth_pair_A` is the fifteenth pair and an independent scene rather than another
illumination of the sweep's one scene. It resolves to RIFT (Δsun azimuth 100° as read), so
`reflect`/`off` is its shipped configuration too. Its three cells come out of the ablation in
2a: `baseline_full` = reflect/off, `mask_fill_zero` = zero/off, `clahe_on` = reflect/on.

| pair | mask_fill | clahe | fill px | gt rmse px | check rmse px | inliers | matches | cov % | sdi |
|---|---|---|---|---|---|---|---|---|---|
| `synth_pair_A` | reflect | off | 32343 | 2.929 | 1.571 | 64 | 218 | 100.0 | 0.578 |
| `synth_pair_A` | zero | off | 0 | 2.929 | 1.571 | 64 | 217 | 100.0 | 0.578 |
| `synth_pair_A` | reflect | on | 32343 | 6.757 | 2.791 | 23 | 319 | 68.8 | 0.352 |

And on real LROC NAC, where there is no ground truth and `check_rmse_px` is the accuracy
number. Both pairs resolve to RIFT (Δsun azimuth 88.7° and 115.4°), so `reflect`/`off` is the
shipped configuration for both:

```
python -m bench.harness sweep --manifest bench/real_pairs.yaml \
    --arm reflect_clahe_off --skip-pair apollo16_dsun004 --out runs/real_arms
python -m bench.harness sweep --manifest bench/real_pairs.yaml \
    --arm zero_clahe_off --arm reflect_clahe_on \
    --skip-pair apollo16_dsun004 --skip-pair apollo16_dsun115 --out runs/real_arms_b
python -m bench.harness sweep --manifest bench/real_pairs.yaml --arm reflect_clahe_on \
    --skip-pair apollo16_dsun004 --skip-pair apollo16_dsun085 --out runs/real_arms_c
```

| pair | mask_fill | clahe | fill px | check rmse px | inliers | matches | cov % | dispersion cv | sdi |
|---|---|---|---|---|---|---|---|---|---|
| `apollo16_dsun085` | reflect | off | 588952 | 1.949 | 163 | 5023 | 49.7 | 1.4676 | 0.201 |
| `apollo16_dsun085` | zero | off | 0 | 1.884 | 143 | 4796 | 43.4 | 1.5979 | 0.167 |
| `apollo16_dsun085` | reflect | on | 588952 | 1.417 | 85 | 4526 | 30.8 | 2.0532 | 0.101 |
| `apollo16_dsun115` | reflect | off | 4400938 | 1.313 | 4266 | 5230 | 100.0 | 0.4029 | 0.713 |
| `apollo16_dsun115` | zero | off | n/a | n/a | n/a | n/a | n/a | n/a | n/a |
| `apollo16_dsun115` | reflect | on | 4400938 | 1.214 | 3662 | 4474 | 100.0 | 0.5102 | 0.662 |

Two cells are `n/a` because two runs did not complete, and that is itself a measurement:
see "The runs that did not finish" at the end of this section. `apollo16_dsun004` is absent
from every real table for the same reason.

#### `mask_fill`: the evidence is mixed, and it supports keeping `reflect`

Over the 11 live comparisons — Δsun 30-180, which is also exactly the set that resolves to
RIFT — `reflect` against `zero`:

| statistic | reflect (shipped) | zero | reading |
|---|---|---|---|
| head-to-head `gt_rmse_px` | 4 wins | **7 wins** | zero wins more often |
| mean `gt_rmse_px` | **3.113** | 3.339 | ... but reflect is better on average |
| worst `gt_rmse_px` | 6.824 | **5.984** | zero's worst case is better |
| head-to-head `check_rmse_px` | **7 wins** | 4 wins | reflect wins more often |
| mean `check_rmse_px` | **1.340** | 1.469 | and on average |
| worst `check_rmse_px` | **1.751** | 2.234 | and on the worst case, by 0.48 px |
| head-to-head `inlier_count` | 2 | **9** | zero delivers more tie-points |
| head-to-head `sdi` | 2 | **9** | and a more uniform set |

Nothing here has a consistent direction. `zero` — the old path that manufactures a hard step
edge at every mask boundary — wins more true-error comparisons and delivers more tie-points
on this sweep; `reflect` wins the held-out comparisons, has the better mean on both error
metrics, and has a much better worst case on the only accuracy number a pair without ground
truth can produce. Two of the individual differences are large and they point opposite ways:
Δsun 50 is 1.887 (reflect) vs 0.931 (zero), Δsun 180 is 0.592 vs 3.366.

The one real pair where both arms completed inverts the sweep's tie-point column: on
`apollo16_dsun085`, `zero` is marginally better on held-out error (1.884 vs 1.949 px) and
worse on everything else — 143 inliers against 163, 43.4% coverage against 49.7%, sdi 0.167
against 0.201.

**The sweep's tie-point columns are an artefact of the fixture and must not be read as a
result.** `samanvay/photometry/normalize.py::_fill_invalid` already measured why, and this
table has to be read against it: the sweep's masks are **1.1% scattered single-pixel dark
speckle**, not regions, so there is barely any manufactured boundary edge for the fill to
remove (boundary PC response 10.8x with `zero` against 9.7x with `reflect` — most of that
excess is genuine texture). What the zeroed speckle *is*, in a set of 14 pairs rendered from
ONE DEM, is a perfectly repeatable synthetic feature present in both images, so `zero`
harvests 10-15% more tie-points from it. That is a property of the renderer, not of the
method. On the one real pair where both arms completed, whose invalid region is a contiguous
588,952-pixel shadow rather than speckle, the tie-point column **inverts**: `reflect` 163
inliers / 49.7% coverage / sdi 0.201 against `zero` 143 / 43.4% / 0.167.

**Recommendation: leave `photometry.mask_fill` at `reflect`.** The case rests, in this order:
on the physics measurement it was designed for (boundary PC response 35.5x the interior with
`zero`, 1.07x with `reflect`, on a contiguous cast shadow — `tests/test_photometry.py`'s
masked scene, rerunnable from this repo); on the real pair, where `reflect` keeps the
tie-points and the coverage; and on `check_rmse_px` across the sweep, where it wins 7-4 with
the better mean and a 0.48 px better worst case. It does **not** rest on true error or on
tie-point yield across the sweep, and this section does not claim it does.

**A correction to what this file said on 2026-09-03.** The 2026-09-03 edition reported that
"the claim that `reflect` was chosen on a measured dsun_20 accuracy improvement
(0.825 -> 0.295 px) does not reproduce". That was wrong twice over and is withdrawn:

* **No such claim was ever published.** Those two numbers are one row of the Δ0-50 table in
  `photometry/normalize.py`'s module docstring, and in it they read `zero` **0.295** /
  `reflect` **0.825** — `reflect` is the *worse* half of that row. The table is presented
  there as evidence that the fill does **not** improve accuracy on this fixture set, in those
  words: *"nobody should quote this sweep as evidence the fill improves accuracy, because it
  does not."* Reading it as a justification for `reflect` inverted it.
* **It does reproduce, in the configuration it was measured in.** That table was run with
  `match.method=rift` pinned. Re-run on this tree at Δsun 20 with the same pin: `zero` 0.295 /
  263 inliers, `reflect` 0.465 / 224 inliers — the inlier counts reproduce exactly and the
  `zero` figure to three decimals; only `reflect`'s gt has moved (0.825 -> 0.465), which is
  the Phase A-D fit, not the fill. Under the shipped `auto` config it looks inert at Δ20 only
  because `auto` picks SIFT there and never builds a PC map.

#### `clahe` on the SIFT arm: the default is on, and the evidence agrees

The three Δsun steps that resolve to SIFT are the only ones in this file that test the
intensity arm, and on all three the shipped `clahe on` is better on every column:

| Δsun | gt rmse px off / **on** | check rmse px off / **on** | inliers off / **on** |
|---|---|---|---|
| 0 | 0.034 / **0.024** | 0.224 / **0.218** | 775 / **790** |
| 10 | 0.083 / **0.041** | 0.430 / **0.366** | 741 / **780** |
| 20 | 0.321 / **0.206** | 0.734 / **0.391** | 209 / **292** |

3 of 3 on true error, held-out error and tie-point count. **`photometry.clahe: auto` on the
intensity arm needs no change**; this is the first measured support it has had.

#### `clahe` on the RIFT arm: a consistent cost against an inconsistent benefit

Over the eleven RIFT-resolved Δsun steps, `clahe on` against the shipped `clahe off`, at
`mask_fill=reflect`:

| statistic | clahe off (shipped) | clahe on | reading |
|---|---|---|---|
| head-to-head `gt_rmse_px` | 1 win | **10 wins** | a large, consistent true-error gain |
| mean `gt_rmse_px` | 3.113 | **1.585** | ... roughly halved |
| worst `gt_rmse_px` | 6.824 | **3.227** | ... and the worst case halved |
| head-to-head `check_rmse_px` | **6** | 5 | no direction |
| mean `check_rmse_px` | **1.340** | 1.546 | held-out error is worse with it on |
| worst `check_rmse_px` | **1.751** | 2.312 | and its worst case is worse |
| head-to-head `inlier_count` | **8** | 3 | fewer tie-points, 8 times out of 11 |
| head-to-head `sdi` | 5 | 6 | no direction |

Those eleven rows are **one scene rendered under eleven illuminations**, not eleven
independent pairs. The three scenes that are not that scene are the ones that decide this,
and every one of them says the same thing about the cost:

| scene | clahe | check rmse px | inliers | cov % | sdi | model |
|---|---|---|---|---|---|---|
| `synth_pair_A` | off | **1.571** | **64** | **100.0** | **0.578** | homography+tps |
| `synth_pair_A` | on | 2.791 | 23 | 68.8 | 0.352 | affine |
| `apollo16_dsun085` | off | 1.949 | **163** | **49.7** | **0.201** | similarity |
| `apollo16_dsun085` | on | **1.417** | 85 | 30.8 | 0.101 | affine+tps |
| `apollo16_dsun115` | off | 1.313 | **4266** | 100.0 | **0.713** | similarity |
| `apollo16_dsun115` | on | **1.214** | 3662 | 100.0 | 0.662 | similarity |

**Recommendation: leave `photometry.clahe` at `auto`, i.e. off on the RIFT arm** — but the
reason is not the one currently written down, and the case is closer than the old table
implied.

* **The cost is consistent: CLAHE loses tie-points and loses uniformity on 4 scenes out of
  4.** 64 -> 23, 163 -> 85, 4266 -> 3662 inliers; sdi 0.578 -> 0.352, 0.201 -> 0.101,
  0.713 -> 0.662; coverage 100% -> 69% and 49.7% -> 30.8%. For an engine whose second pillar
  is spatial uniformity, halving the coverage of a hard real pair is not a price worth an
  accuracy figure.
* **The benefit is inconsistent.** It halves true error on the sweep's scene (10 of the 11
  RIFT steps), improves held-out error on both real pairs (1.949 -> 1.417 and 1.313 -> 1.214),
  is worse than useless on `synth_pair_A` (1.571 -> 2.791 held-out, the model falling back to
  `affine` because the check split is left with too few control points), and is worse on the
  sweep's mean held-out error.
* **What would settle it is more independent real pairs, not more illuminations of one
  synthetic scene.** On the two real pairs we have, CLAHE improved the held-out accuracy both
  times. That is the strongest argument against the current default anywhere in this file,
  and it is two pairs. This is the config key most likely to be wrong, and the one to
  re-measure first when more real data arrives.

The claim in `docs/decisions.md` that CLAHE is off "because the descriptor is contrast-
invariant by construction" is an argument, not a measurement, and this table does not support
it as stated: CLAHE clearly changes what RIFT finds, in both directions.

#### The runs that did not finish

Two real-pair runs were killed rather than reported, and both are worth more than the rows
they would have filled:

| run | killed after | what is known |
|---|---|---|
| `anms_off@apollo16_dsun004` | 23 min | A `faulthandler` stack taken at 300 s intervals put it inside `geometry/tps.py::displacement`, called from `tps.pullback` in `pipeline/stages.py::_warp`, on every dump. |
| `zero_clahe_off@apollo16_dsun115` | 9 min | No output written. The other five arms on the same pair complete in 63-89 s. Not diagnosed; the symptom matches the row above. |

The `dsun004` case is diagnosed, and the cost is now measured rather than estimated. That
pair registers in **133 s** when the TPS is *rejected* (`tps_status: rejected_no_improvement`,
`tps_n_control: 6695`, `model_type: affine`, `runs/anms_real/apollo16_dsun004/anms_on`) and
does not finish in 23 minutes when a different tie-point set gets it accepted.
`pipeline/stages.py::_warp` then builds a dense map by calling `tps.pullback` at **every
output pixel**, and `tps.displacement` costs O(pixels x control points): timed on this machine
with 6695 control points, 200,000 points take **23.9 s**, i.e. 120 µs/px. The dsun004
reference grid is 872x9296 = 8.11e6 px, so one dense map is **16.2 minutes** — and
`output.grid` defaults to `"both"`, so a source-grid map of similar size is built as well.
The 23-minute kill is exactly where that arithmetic puts it. **This is
a defect in the TPS/warp path, not an ablation result, and it is reported to the geometry and
pipeline seats rather than fixed here.** It is also why `apollo16_dsun004` is absent from
every real table above: the arm it stalls on is half of the comparison, so publishing the
other half would be publishing a row that measures nothing — the exact failure this whole
section exists to remove.

### What this says

1. **Photometry is not an improvement here, it is the difference between registering and
   not.** On synth_pair_A both `canonicaliser_off` and `phase_congruency_off` return **zero
   matches** and no model; on dsun_50 the same two arms do the same. The RIFT arm consumes the
   phase-congruency map, so switching the physics off removes the descriptor's input entirely.
   The honest framing of the headline demo is "this pair does not register without it", and
   that is a stronger claim than a delta in px.

2. **The TPS earns its place and its acceptance rule costs accuracy on one of the two pairs.**
   synth_pair_A: `tps_off` 3.235 px vs 2.929 px with it — the spline is accepted and it helps.
   dsun_50: the `auto` rule *rejects* the spline (no held-out improvement) and leaves 1.887 px
   on the table, while `tps_forced` gets 1.473 px. Held-out RMSE and true error disagree about
   the spline on that pair, in the direction that costs us. The rule is still the right default —
   it is what makes a non-rigid warp safe on a pair with no ground truth — but "TPS auto is
   never worse" is a claim this table does not support. On a real NAC strip an accepted TPS is
   also the one stage that can fail to terminate: see "The runs that did not finish" in 2f.

3. **Sub-pixel refinement helps on both pairs**: 2.929 vs 3.523 px on synth_pair_A (and 64 vs
   48 inliers), 1.887 vs 1.920 px on dsun_50 (99 vs 61 inliers). The inlier column is the bigger
   effect on dsun_50 and an RMSE-only table would have missed it.

4. **The matcher choice is the largest single effect in the table.** `matcher_sift` returns
   zero inliers on synth_pair_A and 9 on dsun_50 (37.5% coverage, 5.610 px) against RIFT's 64
   and 99. `match.method: auto` resolving to RIFT on both is the difference between a
   registration and a failure, which is why it is the default.

5. **ANMS is inert on these fixtures and active on real imagery** — section 2d, which is the
   table the `anms_off` row was pretending to be. Where the quota binds, ANMS raises within-cell
   spread in 8 of 8 paired comparisons at no measured accuracy or runtime cost, and it does not
   improve accuracy.

6. **The photometric model is only an arm when a DEM is passed** — section 2e — and with one
   passed, the shipped `lommel_seeliger` is not the best of the three models on either metric on
   either fixture.

7. **`mask_fill` and `clahe` are measured across 14 Δsun steps and 3 independent scenes in
   section 2f, and both defaults survive.** `clahe: auto` gets its first measured support on
   the SIFT arm (3 of 3) and its first serious challenge on the RIFT arm (held-out error
   improves with CLAHE on **both** real pairs, against a tie-point and uniformity cost on
   4 scenes out of 4). `mask_fill=reflect` survives on the physics measurement and on the real
   pair, not on the sweep, which favours `zero` on true error and tie-point yield for a reason
   that is a property of the fixture renderer — 2f says which, and one paragraph of the
   2026-09-03 edition that mis-stated the prior evidence for `reflect` is withdrawn there.

8. **The held-out split is not free.** `check_split_off` on synth_pair_A: 3.221 px against
   2.929 px with the split, and the TPS is rejected without it. On dsun_50: 2.075 vs 1.887 px.
   Holding 20% of the tie-points out of the fit changes the fit. It buys the only accuracy
   number that exists on a real pair, and it costs something real.

---

## 3. Real pairs — LROC NAC, MEASURED 2026-09-02

**The claim that used to sit here — "no real Chandrayaan-2 / LRO pair has been processed by
this code" — was false when it was written and is false now.** Real pairs were processed on
2026-08-30 (`runs/ch2_wac`, `runs/ch2_ch2`, `runs/real_01`) and again by the runs below.

Three same-site LROC NAC pairs over Apollo 16, both halves at 2 m GSD from the same
instrument, cut to a common overlap by `scripts/download_pairs.py`. The only variable
across them is illumination. Source data: NASA/GSFC/Arizona State University.

| pair | source product | reference product | Δsun az (sidecars) | source sun el | reference sun el |
|---|---|---|---|---|---|
| `dsun004` | M177535538R | M129187331R | 3.9° | 20.3° | 35.7° |
| `dsun085` | M177535538R | M109134835R | 88.7° | 20.3° | 82.4° |
| `dsun115` | M1282366833R | M1164718010R | 115.4° | 59.8° | 78.7° |

```
python -m bench.harness sweep --manifest bench/real_pairs.yaml --out runs/bench_real
```

**There is no ground truth for these pairs.** `gt_rmse_px` is null and stays null;
`check_rmse_px` — the RMSE over tie-points held out of the fit — is the accuracy number, with
the caveat measured in section 1 that it does not see a consistent-but-wrong fit.

| run | status | Δsun az ° | check rmse px | check p90 px | in-sample rmse px | inliers | matches | inlier ratio | cov % | sdi | model | matcher | gt rmse | wall s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `auto@apollo16_dsun004` | ok | 3.91 | 0.127 | 0.171 | 0.135 | 8355 | 8383 | 0.997 | 100.0 | 0.985 | affine | sift | null | 60.6 |
| `auto@apollo16_dsun085` | ok | 88.72 | 1.949 | 275.221 | 1.990 | 163 | 5023 | 0.032 | 49.7 | 0.201 | similarity | rift | null | 159.1 |
| `auto@apollo16_dsun115` | ok | 115.39 | 1.313 | 4.305 | 1.271 | 4266 | 5230 | 0.816 | 100.0 | 0.713 | similarity | rift | null | 197.4 |
| `sift@apollo16_dsun004` | ok | 3.91 | 0.127 | 0.171 | 0.135 | 8355 | 8383 | 0.997 | 100.0 | 0.985 | affine | sift | null | 67.0 |
| `sift@apollo16_dsun085` | failed | 88.72 | **no model** | **no model** | **no model** | **no model** | 0 | **no model** | **no model** | **no model** | **no model** | sift | null | 20.1 |
| `sift@apollo16_dsun115` | ok | 115.39 | 1.281 | 301.791 | 1.156 | 440 | 1238 | 0.355 | 86.9 | 0.455 | similarity | sift | null | 22.5 |

### What this says

1. **The synthetic cliff is real on real data.** At Δsun 88.72° the pinned SIFT arm returns
   **zero matches and no model**; the default arm returns 163 inliers at 1.949 px held-out
   RMSE. That is the same failure the synthetic sweep shows from Δ60 (world), here on two
   real LROC NAC frames of the same ground.

2. **Δ3.91° is where an intensity matcher belongs and `auto` sends it there.** 8355 inliers of
   8383 matches, inlier ratio 0.997 — the only *real-pair* cell that passes the plan's 0.85
   bar (section 1 clears it too, but only on the synthetic Δ0/Δ10/Δ20 fixtures). `auto` and the
   pinned `sift` arm resolve to the same configuration at that Δ.

3. **Δ115.39° registers on both arms, and the default arm delivers 4266 tie-points against
   440**: 1.313 px against 1.281 px held out, coverage 100% against 86.9%, `sdi` 0.713 against
   0.455. The held-out RMSE is a wash; everything about the *number and distribution* of the
   tie-points is not.

4. **`check_p90_px` is the number that shows what `check_rmse_px` hides.** 275 px on the
   Δ88.72° run and 302 px on the pinned-SIFT Δ115° run: a minority of held-out points are
   nowhere near the model, on runs whose RMSE-over-inliers reads under 2 px. The two check
   sets hold 941 and 206 points respectively. Read the pair, never the RMSE alone.

5. **These are 872-888 px wide strips up to 11952 px long**, so the aspect-following grid is
   40x4 to 54x4 cells, not 4x4. Coverage and SDI on this table are measured over that grid.

An earlier run of the same Δ88.72° pair — `runs/real_dsun085_rift`, 2026-08-30, git 368bcd4 —
returned 0 matches and `verify_status: failed`. It is not a controlled A/B against the row
above: it also passed `--dem` and pinned `match.method=rift`. What can be said is that the
pair now registers on the shipped default configuration and did not then.

---

## 4. Descriptor-level evidence — MEASURED 2026-08-29 and 2026-09-02

Nearest-neighbour-correct rate on 49 fixed points of a re-illuminated crater field, RIFT
against the orientation-histogram descriptor it replaced (`tests/test_describe.py`), 2026-08-29:

| perturbation | RIFT | old |
|---|---|---|
| contrast/brightness `a*I+b` | **1.000** | 0.653 |
| gamma 2.2 | **1.000** | 0.551 |
| sun azimuth +50° | **0.918** | 0.122 |

RIFT nearest-neighbour-correct across the full sweep range, MEASURED 2026-09-02 on the same
49 points and the same re-illuminated crater field, by shading one height field at 45° and at
45°+Δ (the helpers in `tests/test_describe.py`; `_shade`, `_both_descriptors`, `_nn_correct`).
This is the descriptor-level version of section 1 and it has no pipeline in it:

| Δsun ° | 0 | 10 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 180 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| RIFT | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.918 | 0.816 | 0.633 | 0.551 | 0.531 | 0.776 | 0.980 | 1.000 | 1.000 |
| old | 1.000 | 0.796 | 0.327 | 0.265 | 0.163 | 0.122 | 0.020 | 0.061 | 0.041 | 0.041 | 0.061 | 0.082 | 0.163 | 0.673 |

The trough is Δ80-Δ90 and both descriptors recover by Δ180. That is section 1's headline
result reproduced one level down, on a fixture with no geometry, no matcher and no RANSAC in
it, which is why section 1's Δ180 cell is not a fluke of one homography fit.

Descriptor ablation at Δ0° / Δ50°, inliers (same date, pre-cascade pipeline):

| variant | Δ0° | Δ50° |
|---|---|---|
| single 96 px patch | 6 | 4 |
| pc-weighting off | 28 | 7 |
| ngrid 4 | 144 | 14 |
| **ngrid 6, multi-scale (shipped)** | **312** | **25** |

Multi-scale patches are the single biggest contributor — a single upright patch cannot
survive the fixture's 2x source/reference pixel-scale ratio. The absolute inlier counts are
pre-cascade and are superseded by section 1; the *ranking* between descriptor variants is
what this table is for.

---

## 5. Archive — superseded tables

Three tables that used to be in this file have been **deleted**, not moved here:

* the 2026-08-29 `synth_pair_A` "not measured in every cell" tables, and the "no real pair has
  been processed" table. Sections 2 and 3 measure both, and an empty table sitting above a full
  one is worse than either.
* the pre-RIFT Δsun table (L2 rows 75.2 / 46.0 / 36.8 / 90.7 / 4743.1 / 184.5) and the
  pre-cascade one. Both measured code that no longer exists.

One table is kept, because it is the before-half of a comparison worth keeping:

### SUPERSEDED — Δsun sweep, 2026-08-30, post-P3-cascade, pre-Phase-A-D

Superseded by section 1. Cell = `gt_rmse_px / inlier_count`. Do not quote it as current.

| Matcher arm | Δ0° | Δ10° | Δ20° | Δ30° | Δ40° | Δ50° |
|---|---|---|---|---|---|---|
| **L0** raw SIFT, physics off | 0.02 / 781 | 0.04 / 624 | 0.15 / 160 | 0.37 / 47 | 0.30 / 17 | 26.13 / 0 |
| **L1a** SIFT + empirical canonicalise | 0.03 / 800 | 0.07 / 771 | 0.22 / 214 | 0.61 / 55 | 0.45 / 22 | 26.13 / 0 |
| **L1b** SIFT + DEM physics (auto) | 0.02 / 800 | 0.05 / 780 | 0.26 / 198 | 0.46 / 56 | 0.76 / 15 | 26.13 / 0 |
| **L2** RIFT phase congruency | 0.26 / 742 | 0.28 / 708 | 0.53 / 621 | 0.96 / 436 | 1.57 / 266 | 1.90 / 173 |

Section 1's `auto` arm on the same six fixtures reads 0.024 / 0.041 / 0.206 / 1.143 / 0.895 /
1.887 px at 790 / 780 / 292 / 183 / 142 / 99 inliers. Two comparisons are legitimate and they
point in opposite directions:

* Δ0-Δ20, where `auto` resolves to SIFT, against the L0/L1a rows above: more tie-points
  (790/780/292 against 781/624/160) at comparable true error.
* Δ30-Δ50, where `auto` resolves to RIFT, against the L2 row: true error is flat to better
  (0.96 -> 1.14, 1.57 -> 0.90, 1.90 -> 1.89) and the tie-point count is roughly halved
  (436 -> 183, 266 -> 142, 173 -> 99). Phase A-D holds 20% of the matches out of the fit and
  redefines the delivered inlier set (control inliers plus check points inside the threshold),
  so a like-for-like count is not available. **No run in this file isolates the cause of that
  halving.**

---

## Notes and caveats to carry with any number from this file

- Synthetic numbers are upper bounds (`docs/decisions.md` D6). The source raster was produced
  by cubic resampling of the same master render the reference came from, so a matcher is
  partly scored against that kernel.
- Wall-clock seconds in these tables were measured on a laptop running three other agents'
  jobs concurrently. Treat them as an order of magnitude, not a benchmark.
- A low coverage percentage on low-texture terrain is a correct answer, not a bad score
  (`docs/limitations.md` section 2).
- `bench/ablate.py` warns if every variant returns an identical `rmse_px` — that would mean the
  pipeline is ignoring the ablation keys and the table proves nothing. It did not fire on
  either run above.
- A row identical to its baseline is never a coincidence here: four consecutive runs of one
  arm return bit-identical metrics on both fixtures (section 2). Section 2c says, for every
  such row, whether the stage did nothing or never ran.
- Copy numbers into slides from the CSV in the run directory (`runs/*/dsun_sweep.csv`,
  `runs/*/ablation.csv`), never from a terminal scroll-back, and carry the run's
  `provenance.json` git SHA with them.
- Those two roll-up CSVs are **not** in git: `.gitignore` un-ignores only `metrics.json`,
  `matches.csv`, `provenance.json` and `transform.json` under `runs/`. A clone therefore has
  every cell of sections 1-3 one run at a time — `runs/dsun_sweep_full/<arm>/<pair>/metrics.json`,
  `runs/ablate_A/<arm>/metrics.json` — but has to re-run the two commands above to get the
  roll-up tables back. Nothing in this file depends on a file a clone cannot see.
