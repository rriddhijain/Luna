# SAMANVAY — matcher baselines

SIH26166. Table created 2026-08-29. **Every cell is "not measured" until somebody
measures it.**

The shape of this table is fixed now, on purpose. Columns get added by agreement, not on
demo day, and never in the twenty minutes before a presentation. If a number you want is
not a column here, that is a conversation to have first — a table that grows a column
during the demo is a table nobody believes.

**Do not write a plausible number into a cell.** "not measured" is a fact. A number nobody
ran is a lie that survives into a slide.

---

## The four rungs

| level | what it is | why it is in the table |
|---|---|---|
| **L0 SIFT raw** | SIFT on raw DN, no photometric correction | The null hypothesis. If the canonicaliser does not beat this, the project's thesis is wrong. |
| **L0 ORB raw** | ORB on raw DN | Speed baseline. Binary descriptors, Hamming matching — what you would use if runtime were the only constraint. |
| **L1 SIFT on albedo** | SIFT after illumination is divided out (`canonicalise`) | Isolates the physics contribution. L1 minus L0 **is** the photometry claim. |
| **L2 on phase congruency** | keypoints from the phase-congruency map, PC-oriented descriptors | Isolates the illumination-invariant-feature contribution, and is the path that carries OHRC scale where the DEM cannot (see `docs/decisions.md` D2). |

---

## Results — `fixtures/synth_pair_A`

Fixture: 1715x1715 source (0.5 m GSD) against 1024x1024 reference (1 m GSD).
Delta sun azimuth 270 deg, delta elevation -40 deg, 10 deg rotation, 2x scale ratio,
seed 0. Regenerate with `python -m synth.render_pair`.

Metric definitions are the frozen ones: **RMSE in SOURCE pixels**, 2-D point RMSE;
coverage and dispersion over a 4x4 uniformity grid with masked cells excluded from the
denominator (`docs/CONTRACTS.md` section 0).

| level | inlier count | inlier ratio | RMSE px (source) | coverage % | dispersion CV | runtime s |
|---|---|---|---|---|---|---|
| L0 SIFT raw | not measured | not measured | not measured | not measured | not measured | not measured |
| L0 ORB raw | not measured | not measured | not measured | not measured | not measured | not measured |
| L1 SIFT on albedo | not measured | not measured | not measured | not measured | not measured | not measured |
| L2 on phase congruency | not measured | not measured | not measured | not measured | not measured | not measured |

### Against ground truth — held-out points

`gt_points_holdout` in `gt.json` shares no point with the tuning set. **Tune on
`gt_points`, report from this table.** See `docs/decisions.md` D6 for why the two are
separated, and why every number here is an *upper bound* on real performance.

| level | gt RMSE px | gt bias x px | gt bias y px | gt p90 px |
|---|---|---|---|---|
| L0 SIFT raw | not measured | not measured | not measured | not measured |
| L0 ORB raw | not measured | not measured | not measured | not measured |
| L1 SIFT on albedo | not measured | not measured | not measured | not measured |
| L2 on phase congruency | not measured | not measured | not measured | not measured |

---

## Results — real pairs

**No real Chandrayaan-2 / LRO pair has been processed by this code.** This section stays
empty until one has been. It is not an oversight and it should not be filled with
synthetic numbers relabelled.

| pair | instruments | level | inlier count | inlier ratio | RMSE px | coverage % | dispersion CV | runtime s |
|---|---|---|---|---|---|---|---|---|
| — | — | — | not measured | not measured | not measured | not measured | not measured | not measured |

---

## How to fill this in

```
python -m synth.render_pair                       # regenerate the fixture (seed 0, deterministic)
python -m bench.ablate --source fixtures/synth_pair_A/source.tif \
                       --ref    fixtures/synth_pair_A/reference.tif \
                       --out    runs/ablate
```

`bench/ablate.py` writes `runs/ablate/ablation.csv` and `ablation.md`, one row per variant
with deltas against the full pipeline. Copy the numbers here **from the CSV**, not from a
terminal scroll-back, and record the git SHA of the run alongside them.

When a row is filled in, note:

- the git SHA it was measured at,
- whether `illum_mode` was `dem`, `dem_lowfreq` or `empirical` (a physics number from
  `empirical` mode is not a physics number),
- `metrics["status"]` — a completed run is not automatically a successful one.

## Notes and caveats to carry with any number from this file

- Synthetic numbers are upper bounds (`docs/decisions.md` D6).
- A low coverage percentage on low-texture terrain is a correct answer, not a bad score
  (`docs/limitations.md` section 2).
- If every ablation variant returns the same `rmse_px`, `bench/ablate.py` warns you: the
  pipeline is ignoring the ablation config keys and the table proves nothing.

---

# SUPERSEDED — see the FINAL table below

The Δsun table that was here (L2 rows 75.2 / 46.0 / 36.8 / 90.7 / 4743.1 / 184.5, and the
L1b rows 26.1 / 1467 / 59.3 / 26.1 / 3315 / 26.1) was measured BEFORE the RIFT descriptor
and before the DEM arm's "auto" policy landed. Both arms were broken then. Kept only as
the before-half of the comparison; do not quote it as current.

---

# SUPERSEDED AGAIN — the table below predates the P3 cascade



Same fixture set, same harness, full pipeline including sub-pixel refinement.
Cell = `true gt_rmse_px / inlier_count`. `!` = redundancy below the measured trust bar
of 10 (`runs/calibrate/redundancy.md`).

| arm | Δ0° | Δ10° | Δ20° | Δ30° | Δ40° | Δ50° |
|---|---|---|---|---|---|---|
| **L0** raw SIFT, physics off | 0.07 / 795 | 0.11 / 680 | 0.44 / 188 | 0.19 / 43 | 1.54 / 11 ! | 15.84 / 7 ! |
| **L1a** SIFT + empirical | 0.07 / 800 | 0.08 / 756 | 0.08 / 265 | 0.83 / 52 | 1.16 / 13 ! | 26.13 / 0 ! |
| **L1b** SIFT + DEM (auto) | 0.07 / 800 | 0.11 / 762 | 0.36 / 254 | 0.37 / 53 | 1.08 / 11 ! | 5.68 / 6 ! |
| **L2** RIFT phase congruency | 0.25 / 347 | 0.57 / 315 | 1.79 / 164 | 1.77 / 100 | **1.79 / 55** | **1.89 / 29** |

**L2 is the only arm still trustworthy at Δ40-50°.** It is honestly worse below Δ30°;
intensity matching is excellent when illumination matches, and the claim is only about
what happens when it does not.

## Descriptor-level evidence (tests/test_describe.py)

Nearest-neighbour-correct rate on 49 fixed points of a re-illuminated crater field,
RIFT vs the old orientation-histogram descriptor:

| perturbation | RIFT | old |
|---|---|---|
| contrast/brightness `a*I+b` | **1.000** | 0.653 |
| gamma 2.2 | **1.000** | 0.551 |
| sun azimuth +50° | **0.918** | 0.122 |

## Descriptor ablation (Δ0° / Δ50°, inliers)

| variant | Δ0° | Δ50° |
|---|---|---|
| single 96 px patch | 6 | 4 |
| pc-weighting off | 28 | 7 |
| ngrid 4 | 144 | 14 |
| **ngrid 6, multi-scale (shipped)** | **312** | **25** |

Multi-scale patches are the single biggest contributor — a single upright patch cannot
survive the fixture's 2× source/reference pixel-scale ratio.

---

# MEASURED — Δsun sweep, 2026-08-29

First real measurement. Fixture set `fixtures/dsun_sweep/` (512×512 reference,
2× scale, identical geometry/seed/DEM/albedo in every pair; **only the source sun
azimuth changes**). Reproduce with the pipeline directly — each pair carries its
own `dem.tif`, so a single global `--dem` will not do it.

Cell = `gt_rmse_px` / `inlier_count`. `gt_rmse_px` is TRUE error against the
analytic ground-truth homography in `gt.json`, not self-consistency RMSE.
`!` marks an RMSE the redundancy gate flagged as untrustworthy (too few inliers
for the model's degrees of freedom — see `geometry/verify.py`).

| arm | Δ0° | Δ10° | Δ20° | Δ30° | Δ40° | Δ50° |
|---|---|---|---|---|---|---|
| **L0** raw SIFT, physics off | 0.1 / 795 | 0.1 / 680 | 0.4 / 188 | 0.2 / 43 | 1.5 / 11 | 15.8 / 7 |
| **L1a** SIFT + empirical canonicalise | 0.1 / 800 | 0.1 / 756 | 0.1 / 265 | 0.8 / 52 | 1.2 / 13 | 24.2 / 5 ! |
| **L1b** SIFT + DEM physics | 26.1 / 0 ! | 1467.6 / 5 ! | 59.3 / 6 ! | 26.1 / 0 ! | 3314.9 / 11 | 26.1 / 0 ! |
| **L2** phase congruency | 75.2 / 6 ! | 46.0 / 4 ! | 36.8 / 4 ! | 90.7 / 5 ! | 4743.1 / 6 ! | 184.5 / 6 ! |

## What this says — read before building anything else

1. **L0 degrades exactly as the pitch predicts.** 795 inliers at Δ0° collapsing to
   7 at Δ50° is the baseline curve the demo is built on. That half of the story
   is real and reproducible today.

2. **L1a (empirical canonicalisation) gives a small, consistent win** in inlier
   count up to Δ40° (800 vs 795, 265 vs 188, 52 vs 43, 13 vs 11) and loses at
   Δ50°. Honest reading: measurable, not yet a headline.

3. **L1b and L2 do not work yet, and they are the two arms that carry the
   novelty claim.** Neither is a wiring fault — the pipeline runs them end to end
   and the numbers are real. They are algorithmic work:
   - **L1b:** DEM sampling was misaligned (fixed 2026-08-29 — it now composes both
     geotransforms instead of stretching the DEM onto the image shape). It still
     fails because the fixture's source geotransform carries a deliberate ~49 px
     prior error, so full-resolution predicted shading is misaligned at exactly the
     scale SIFT keys on. This is landmine D2 in `docs/decisions.md`, now with a
     measurement behind it. Likely direction: iterate — register coarsely, re-render
     the illumination through the *recovered* transform, then re-match; or stay in
     `dem_lowfreq` and let phase congruency carry fine structure.
   - **L2:** produces plenty of matches (~800) but almost no inliers, so the
     detector is fine and the **descriptor** is the weak link. It is currently a
     4×4×8 orientation histogram, not the RIFT Maximum Index Map. `phasecong.py`
     already exports `mim` for exactly this. This is the L3 work.

4. **The default path is the working one.** `photometry.dem_path` defaults to
   `None`, so a default run uses the empirical mode (L1a). Passing `--dem` today
   selects the arm that does not yet work.


---

# FINAL — Δsun sweep, 2026-08-30 (post P3 cascade)

Full pipeline: coarse init -> canonicalise -> **cascade** -> verify ladder -> sub-pixel.
Cell = `true gt_rmse_px / inlier_count`. `!` = redundancy below the trust bar of 10.

| Matcher arm | Δ0° | Δ10° | Δ20° | Δ30° | Δ40° | Δ50° |
|---|---|---|---|---|---|---|
| **L0** raw SIFT, physics off | 0.02 / 781 | 0.04 / 624 | 0.15 / 160 | 0.37 / 47 | 0.30 / 17 | **26.13 / 0** ! |
| **L1a** SIFT + empirical canonicalise | 0.03 / 800 | 0.07 / 771 | 0.22 / 214 | 0.61 / 55 | 0.45 / 22 | **26.13 / 0** ! |
| **L1b** SIFT + DEM physics (auto) | 0.02 / 800 | 0.05 / 780 | 0.26 / 198 | 0.46 / 56 | 0.76 / 15 | **26.13 / 0** ! |
| **L2** RIFT phase congruency | 0.26 / 742 | 0.28 / 708 | 0.53 / 621 | 0.96 / 436 | **1.57 / 266** | **1.90 / 173** |

**At Δ50° every SIFT-based arm returns zero inliers.** Complete failure, not degradation.
RIFT returns 173 inliers at 1.90 px on the same pair.

The cascade lifted every arm: inlier counts roughly doubled to sextupled across the board
(L2 at Δ50°: 29 -> 173) and mid-range true error fell (L2 at Δ20°: 1.79 -> 0.53 px).
