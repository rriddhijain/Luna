# SAMANVAY — Architecture Decision Record

SIH26166 · lunar image registration engine.

D1-D6 are dated **2026-08-29**. D7-D13 were made during the 2026-09-02 revamp and are
dated accordingly; each of those names the plan requirement it answers. Everything here
is marked **PROPOSED — needs team ratification**. Several are explicitly joint calls (D2,
D4, D5); nothing here is settled until the team says it is. If you disagree with one, say
so in the ratification pass rather than quietly writing code that assumes the opposite —
that is precisely the failure mode this file exists to prevent.

Status vocabulary: **PROPOSED** (written down, not yet agreed) · **RATIFIED** (agreed,
change it only through the protocol in `CONTRACTS.md`) · **OPEN** (the team must decide;
no default has been chosen and code must not assume one).

---

## D1 — Coordinate conventions

**Status:** PROPOSED — needs team ratification. **Date:** 2026-08-29.

**Decision.** Four rules, frozen repo-wide and restated in `samanvay/types.py`:

1. Coordinates are `(x, y) = (column, row)`, floating point.
2. A pixel **centre** sits at integer coordinates. Pixel `(0, 0)` is the centre of the
   top-left pixel, not its corner.
3. Every transform is a `3x3 float64` homogeneous matrix that maps **SOURCE → REFERENCE**.
4. Every residual and every RMSE is expressed in **SOURCE pixels**. A residual is
   `H^-1(ref_xy) - src_xy` — the reference point pulled back into the source frame — not
   `H(src_xy) - ref_xy`. RMSE is the 2-D point RMSE `sqrt(mean(dx^2 + dy^2))`, not the
   per-component RMS.

**Rationale.** Every one of these has a plausible opposite that some library already uses,
and mixing them produces errors that are *small enough to look like noise*:

- `(row, col)` is NumPy's natural order and `(x, y)` is OpenCV's. Both appear in this
  process at once. A silent transpose is not subtle, but a transposed *transform* on
  near-square imagery often still looks approximately right.
- Pixel-centre vs pixel-corner differs by exactly 0.5 px. On a project whose headline
  claim is sub-pixel accuracy, a constant 0.5 px bias is not a rounding detail — it is
  larger than the thing being claimed, and it survives every sanity check that only looks
  at a warped overlay. GDAL geotransforms are corner-based, so `geometry/init.py` evaluates
  them at `(x + 0.5, y + 0.5)`; that half-pixel does **not** cancel when the two GSDs differ.
- Source→reference vs reference→source is an inversion. On a pair with a 2x scale ratio
  (which the synthetic fixture deliberately has) an inverted convention gives residuals
  wrong by a factor of two, and "2x too good" is exactly the kind of number nobody
  questions.
- The residual frame matters for the same reason. `geometry/verify.py` originally computed
  residuals in reference pixels; at scale 0.5 that reports **half** the true source-pixel
  error. That is now fixed, and the module carries `metrics["residual_units"] =
  "source_px"` so a consumer can assert it rather than trust it.

The cost of getting one of these wrong is roughly a day: the pipeline runs, the numbers are
finite and plausible, and the bug is only found by someone hand-checking a correspondence.
The cost of writing them down is this paragraph.

**Consequence.** OpenCV measures its RANSAC threshold as a *forward* reprojection error in
reference pixels, so `verify_matches` converts `config["ransac_thresh_px"]` (source pixels,
like everything else) using the scale implied by the init. Any new estimator added to the
ladder must do the same conversion.

---

## D2 — DEM resolution strategy (interaction I13)

**Status:** PROPOSED — needs team ratification. This one is a joint call and the most
consequential decision in the project. **Date:** 2026-08-29.

**The problem, stated plainly.** The physics claim of SAMANVAY is that two images acquired
under different sun angles are correlatable once predicted illumination is divided out.
Predicting illumination requires a DEM. The DEM we have is **SLDEM2015 at ~60 m/px**. The
imagery we must register includes **OHRC at ~0.25 m/px** — a factor of roughly 240. You
cannot render 0.25 m shading from a 60 m DEM. Anything you produce at that scale is
interpolation artefacts wearing a physics costume, and dividing an image by interpolation
artefacts makes it *worse*, not more invariant.

**Decision — three modes, and the pipeline records which one ran.**
`samanvay/photometry/normalize.py` selects a mode per product and writes it to
`CanonicalImage.params["illum_mode"]`, so no number ever leaves the pipeline without the
provenance of how its illumination was handled:

| mode | condition | what is removed |
|---|---|---|
| `dem` | DEM present, sun geometry known, and `dem_gsd / image_gsd <= 4` | Full physical render: Lommel-Seeliger over DEM normals with a ray-marched cast-shadow field. The whole illumination field. |
| `dem_lowfreq` | DEM present but far coarser than the image | The DEM render is computed, then **only its low-frequency component** is divided out (Gaussian, sigma derived from the GSD ratio). Structure finer than the DEM can resolve is deliberately left alone. |
| `empirical` | no usable DEM, or sun geometry absent, or GSD unknown | Flat-field / homomorphic estimate: the image's own low-frequency envelope, divided out. |

The `4x` boundary in `dem_gsd_ratio_max` is a stated parameter, not a hidden constant, and
is subject to the same "must be defensible" rule as D4.

**Decision — the claim we make, per instrument.**

- At **TMC (~5 m/px) and IIRS (~80 m/px)** scale the DEM genuinely resolves the terrain
  that shapes the shading. Here we demonstrate the *full* physics claim: rendered
  illumination, divided out, measurably improves matching across a large sun-angle
  difference. This is the ablation row that carries the innovation argument.
- At **OHRC (~0.25 m/px)** scale we claim **only low-frequency illumination removal**, and
  we say so out loud. The fine structure that actually carries the tie-points at that scale
  is handled by **phase congruency**, which is contrast- and brightness-invariant by
  construction and needs no DEM at all. Phase congruency is not a fallback here; it is the
  correct tool for the regime where the DEM has nothing to say.

**Rationale.** Two reasons, one technical and one strategic.

Technical: `dem_lowfreq` is not a compromise, it is the honest decomposition. A 60 m DEM
carries real information about the *large-scale* illumination gradient across an OHRC
frame — that gradient is exactly what makes a 70-degree-incidence image and a
20-degree-incidence image un-correlatable as DN. Removing it is worth doing. Claiming the
DEM also explains the 0.5 m crater rims is what would be false.

Strategic: an ISRO domain jury contains people who know SLDEM's resolution by heart. They
*will* ask. A team that has already named the limit, implemented three modes, and reports
which one ran, is a team that understands its own physics. A team that presents a single
"physics-based correction" number at OHRC scale and gets asked "from which DEM?" has lost
the room. **Naming it first converts the project's biggest weakness into its most
credible moment.**

**Open sub-question for the team.** Whether `dem_lowfreq` should also be the default at
TMC scale when only SLDEM is available (ratio ~12x), or whether TMC gets `dem` via a
finer DEM (e.g. a Chandrayaan-2 TMC-derived DTM where one exists). Not decided.

---

## D3 — Rotation: the near-upright assumption

**Status:** PROPOSED — needs team ratification. **Date:** 2026-08-29.

**Decision.** The prototype assumes **near-upright imagery, relative rotation under about
15 degrees**, unless the matcher is explicitly run in rotation-invariant mode. The default
path uses SIFT/ORB descriptors with their orientation assignment intact, which is nominally
rotation-invariant, but the *tiled* search that gives us uniformity assumes the coarse init
places a source tile within a modest search margin of the correct reference window — an
assumption that degrades as rotation grows and the projected tile footprint stops being
roughly axis-aligned.

**The cost of each choice.**

- **Assume upright (current default).** Cheaper search windows, tighter ratio tests, higher
  inlier ratios, faster runs. Fails on any pair with a large relative roll — most obviously
  an ascending-vs-descending orbit pair, which is a completely realistic input. Failure is
  *graceful* (cells return `insufficient_texture`, coverage drops, the ladder reports a
  failed fit) rather than silent, but it is still a failure.
- **Run rotation-invariant.** Wider search margins, descriptor matching across a larger
  candidate pool, more false correspondences surviving the ratio test, and therefore a
  heavier RANSAC burden. Expect a several-fold runtime cost and a lower inlier ratio at the
  same recall. The upside is that the pair that would otherwise return nothing returns
  something.

**Where it already bites.** The synthetic fixture `fixtures/synth_pair_A` is generated with
a deliberate **10 degrees** of rotation plus a 2x scale ratio, which sits just inside the
assumption. That is intentional: a fixture that is upright and 1:1 lets rotation-fragile
code look healthy right up until it meets real data.

**What is not decided.** Whether rotation-invariant mode becomes a config flag the operator
sets, or something the pipeline infers from the coarse init's rotation component (which it
can compute — `geometry/init.py` composes the two geotransforms and the rotation falls out
of the 2x2 block). The inference route is better and is not implemented.

---

## D4 — Model-selection margin

**Status:** PROPOSED — needs team ratification. **Date:** 2026-08-29.

**Decision.** `geometry/verify.py` fits a ladder of three models — similarity (4 dof),
affine (6 dof), homography (8 dof) — and selects **the simplest model whose inlier RMSE is
within 10 percent of the best model's**. The margin lives in
`config["model_margin"]`, defaults to `0.10`, and is written into `metrics.json` as
`model_margin` on every single run.

Two details that make the comparison mean something:

- All candidates are scored on **one common point set** — the inliers of the
  best-supported candidate — so an 8-dof model cannot win by shrinking its own inlier set
  until the fit looks tight.
- `metrics["model_candidates"]` reports every rung's RMSE and inlier count, so the choice
  is auditable after the fact rather than asserted.

**Rationale.** Extra degrees of freedom always reduce residual on the points you fitted.
Without a margin, homography wins every time, including on pairs where the true transform
is a similarity — and an over-parameterised fit extrapolates badly outside the tie-point
hull, which is exactly the region a registered product gets judged on. The margin says: a
more complex model must earn its complexity by a *materially* better fit, not a marginally
better one.

**The number is a choice, and it must stay defensible.** 10 percent is a reasonable prior,
not a measurement. The rule the team agrees to is: **the margin is set once, before the
numbers are collected, and reported on every run.** It is not tuned until the demo looked
good. If we later change it, we change it because a ratified argument or a measurement on a
held-out fixture says so, we record the change here with its date, and we re-run every
number that was reported under the old value. A judge asking "why 10 percent?" gets an
argument; a judge asking "did you tune this?" gets a version history.

**Open.** Whether an information criterion (AIC/BIC on the residual likelihood) should
replace the fixed margin. It is more principled and needs a noise model we do not have yet.

---

## D5 — IIRS is a spectrometer, not a camera

**Status:** **OPEN — the team must decide and justify it.** No default has been chosen and
no code currently assumes one. **Date:** 2026-08-29.

**The problem.** Chandrayaan-2 IIRS is an imaging spectrometer: a scene is a cube of
~250 contiguous bands from roughly 0.8 to 5.0 microns, not a greyscale image. "Register the
IIRS image" is not a well-posed instruction until somebody says **which image**. The
choice materially changes the answer, because:

- Band SNR varies enormously across the range; the thermal end (beyond ~3 microns) carries
  a substantial emitted component, not just reflected sunlight, so the illumination physics
  in D2 does **not** apply there unchanged.
- Different bands see different things. A band chosen for mineralogical contrast may have
  poor spatial structure; a band chosen for texture may be radiometrically uninteresting.
- Whatever is chosen must be defensible against the reference we are registering *to*,
  which is a broadband visible product (LRO NAC/WAC, TMC). Matching a 2.8-micron band to a
  visible panchromatic reference is a harder cross-modal problem than matching a
  0.9-micron band to it.

**The candidate answers, none selected:**

1. **A single named band**, chosen for SNR and spatial contrast in the reflected-solar
   range, and justified in writing.
2. **A band average / synthetic panchromatic** over the reflected-solar range — better SNR,
   more like the broadband reference, at the cost of throwing away the spectral information
   that is IIRS's entire reason to exist.
3. **A PCA first component** over the reflected-solar bands — maximum variance, best
   texture, but a data-dependent basis that changes scene to scene, which makes
   reproducibility and cross-scene comparison awkward.

**Why this is flagged rather than defaulted.** Picking one silently would be exactly the
class of unstated assumption this file exists to catch, and it is a domain question a
lunar-science jury is qualified to interrogate. Whatever is chosen, `Product.meta` must
record the band or the reduction used, and `provenance.json` must carry it, so the answer
travels with the output.

**Until it is decided:** the pipeline treats IIRS input as an ordinary single-band raster —
band 1 of whatever file it is handed — and makes **no** claim about which band that is.
That is not a decision, it is the absence of one.

### Amendment, 2026-09-02 — a default now exists, and it is candidate 3

The sentence above ("no code currently assumes one") stopped being true when
`samanvay/io/bands.py` landed. `config["band"]` now defaults to
`{"index": None, "reduce": "pc1", "min_snr": 2.0, "max_bands": 64}`: a cube is
SNR-screened, capped and reduced to its first principal component, sign-fixed to
correlate positively with the band mean. That is **candidate 3**, and it was chosen
because the plan (Part 1 §3) names it explicitly.

The open question is not closed by that. Candidate 3's known cost is exactly the one
listed above — a data-dependent basis that changes scene to scene, which makes
cross-scene comparison awkward — and it is now a cost the code pays by default. What the
code does do is refuse to hide it: `n_bands`, `n_bands_used`, `n_bands_dropped_snr`,
`reduce`, `bands_used` and `explained_var_frac` are recorded in
`Product.meta["band_reduction"]`, so the reduction travels with the output as this
decision required, and `reduce: "band"` with an explicit `index` selects candidate 1 the
moment the team names a band. `preflight` raises a multi-band cube from `note` to
`warning` and says which reduction will run. **D5 stays OPEN**: a default chosen because
the plan named it is not the same as a default the team justified.

---

## D6 — Sub-pixel self-measurement

**Status:** PROPOSED — needs team ratification. **Date:** 2026-08-29.

**The problem.** The sub-pixel accuracy claim is currently measured on
`fixtures/synth_pair_A`, which is generated by warping a rendered scene through a known
homography. If you generate a pair by resampling with kernel K and then measure sub-pixel
accuracy against that same warp, part of what you are measuring is **K**, not your matcher.
A refiner whose interpolator resembles K will score better than one that does not,
independent of whether it is actually better on real imagery. This is circular, and it is
the first thing a careful reviewer asks about.

**Decision — the mitigation, all of which is implemented in `synth/render_pair.py`.**

1. **Ground-truth correspondences are analytic.** `gt_points` and `gt_points_holdout` in
   `gt.json` are source grid points pushed through `H_src_to_ref` in float64. They are never
   recovered by resampling, template matching, or any image operation, so they carry no
   kernel bias of their own.
2. **A held-out point set exists.** `gt_points_holdout` uses a different grid size, a wider
   margin and a deterministic jitter, so it shares no point with `gt_points`. **Tune on
   `gt_points`, report on `gt_points_holdout`.**
3. **Resampling is high order and in the magnifying direction.** The source is produced
   with a cubic kernel from a finer master grid, so there is no aliasing — only mild,
   isotropic smoothing.
4. **The reference is decimated physically, not interpolated.** A 2x box average is a
   sensor PSF model, not a resampling artefact.
5. **The fixture's forward photometry is independent of the pipeline's.**
   `synth/render_pair.py` imports nothing from `samanvay`. Rendering a fixture with the same
   code the pipeline uses to *correct* illumination would make the whole evaluation
   circular in a second, worse way.

**Decision — the residual circularity we accept, and state.** The source raster is still a
resampled version of the master render; its high-frequency content has been shaped by the
cubic kernel. A matcher that refines with a similar interpolator will look slightly better
here than on real data. Both images also come from the *same* DEM and the *same* albedo
field, so the fixture measures illumination and geometry robustness only — not true surface
change, not resolution-dependent detail the reference never saw, not per-instrument MTF.

**Therefore: every synthetic sub-pixel number is reported as an UPPER BOUND on real
performance, held-out set alongside tuned set, with this caveat attached.** It is not
presented as the accuracy of the system. The real number is unknown until real pairs
arrive (see `docs/limitations.md`).

**Rationale.** We cannot eliminate the circularity without real ground truth, and we do not
have real ground truth yet. The choice is between an unqualified synthetic number that a
reviewer will correctly discount to zero, and a qualified one that survives scrutiny. The
qualified one is worth more even though it is smaller.

---

## D7 — `match.method` defaults to `auto`, resolved from Δ sun azimuth

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 1 §1 (illumination and shadow inversion invariance).

**Decision.** `config["match"]["method"]` ships as `"auto"`. `pipeline/stages.py` resolves
it once both products are loaded, from the circular difference of the two `sun_az_deg`
values: **Δsun unknown or ≥ 20° → `rift`; below 20° → `sift`**. That is the same bar
`io/preflight.py` already recommends on, deliberately — one rule, two places that must
not disagree. `photometry.phase_congruency` and `photometry.clahe` are then resolved from
the *chosen* matcher (D8), before `canonicalise` is called. Every one of those decisions
lands in `metrics.json`: `match_method_requested`, `match_method_resolved`,
`match_method_recommended`, `match_method_reason`, `phase_congruency_resolved`,
`phase_congruency_reason`, `clahe_resolved`, `clahe_reason`.

**Rationale.** The old default was the fixed string `"sift"`, which meant a judge running
`samanvay register` with no flags got the arm that does not survive the problem the
project exists to solve. Measured on the 14-pair `fixtures/dsun_sweep`
(`bench/baselines.md` §1, 2026-09-02, `auto` against pinned `sift`):

| Δsun (world) | 30 | 40 | 50 | 60 → 180 |
|---|---|---|---|---|
| `auto` — `gt_rmse_px` / inliers | 1.143 / 183 | 0.895 / 142 | 1.887 / 99 | 2.11–6.82 px, 46–73 inliers, a model on all eight |
| `sift` pinned — `gt_rmse_px` / inliers | 0.435 / 69 | 2.052 / 23 | 5.610 / 9 | **no model at all**, all eight |

Pinned SIFT does not degrade gracefully past the bar: it collapses to 9 inliers at Δ50°
and then stops producing a model entirely from Δ60° up, on all eight remaining pairs.
Shipping a default that turns the headline differentiator off is not a neutral choice.

The bar is 20° and not something tuned: it is the value preflight was already written
against, so `auto` cannot recommend one thing and do another. RIFT is honestly *worse*
below Δ20° — the 2026-08-30 four-rung table archived in `bench/baselines.md` §5 puts a
pinned-RIFT arm at 0.26 px against raw SIFT's 0.02 px at Δ0°, and no current sweep re-runs
a pinned-RIFT arm below the bar, so that is the only figure available and it is an
archived one. That asymmetry is why `auto` is a switch and not a replacement — intensity
matching is superb when the illumination matches, and the claim is only about what happens
when it does not.

**The cost.** Two products with no sun metadata resolve to `rift`, which is slower and
less accurate than `sift` on an easy pair. That is the deliberate direction to fail in: an
unknown Δsun could be 100°, and the arm that survives 100° is the one to pick when you
cannot tell. `match_method_reason` says "delta sun azimuth is unknown" so the operator can
override with `--set match.method=sift`.

**Verified this session.** `make demo` on `fixtures/synth_pair_A` resolves to `rift` at
`delta_sun_az_deg` 100.0 and registers (`homography+tps`, 64 inliers, held-out RMSE
1.5712 px). `register` also prints the preflight recommendation when it disagrees with the
resolved config, so a judge can see the engine knew.

---

## D8 — CLAHE is OFF on the RIFT arm and ON for the intensity arms

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 2 Step 1 (radiometric normalisation: min-max stretch and CLAHE).

**Decision.** `photometry.clahe` ships as `"auto"` and resolves to **True** when the
resolved matcher is intensity-based (`sift` / `orb`) and **False** when it is `rift` /
`l2`. When enabled it is `cv2.createCLAHE(clipLimit=clahe_clip, tileGridSize=(g, g))` on
the uint8 view of the albedo, applied **after** the percentile stretch and **before** phase
congruency. `clahe_applied` is recorded in `CanonicalImage.params` and `clahe_resolved`
plus a plain-English `clahe_reason` in `metrics.json`.

**Rationale — why the plan's own step is switched off on our headline arm.** The plan asks
for CLAHE to "normalize visual contrast across vastly different sensors". That is the right
instruction for a descriptor that reads contrast. RIFT does not: its input is the phase
congruency map, which is invariant to contrast and brightness *by construction* — the whole
reason it is the arm that survives Δsun 100°. Applying CLAHE ahead of it cannot add
invariance the descriptor already has. What it can add is damage:

1. **A spatially varying non-linearity.** CLAHE applies a different transfer function in
   every tile. Phase congruency measures the alignment of Fourier phase across scales; a
   position-dependent intensity remap perturbs exactly that quantity, in a pattern set by
   the tile grid rather than by the terrain.
2. **Tile-boundary steps.** Bilinear interpolation between tile transfer functions
   softens but does not remove the seams, and a seam is a straight edge. Phase congruency
   is an edge detector. This is the same class of defect as the mask-boundary landmine in
   D12: manufactured structure injected into the one map whose purpose is to carry only
   real structure.

On the SIFT/ORB arms the calculus reverses. Those descriptors key on local gradient
magnitude and orientation, so local contrast equalisation genuinely helps a low-contrast
mare region contribute keypoints at all — which is the plan's cross-sensor normalisation
argument, applied where it holds.

**Amended 2026-09-02 — the RIFT half HAS been measured, and the evidence is split.**
An earlier revision of this record said neither half had been A/B'd. That was wrong:
`bench/baselines.md` §2 runs `clahe_on` against `clahe_off` as one-switch ablation arms on
two fixtures, and both baselines resolve to `rift`, so both rows measure exactly the
`rift → off` half of this decision.

| fixture (Δsun as the matcher reads it) | arm | `gt_rmse_px` | `check_rmse_px` | inliers | cov % | sdi |
|---|---|---|---|---|---|---|
| `synth_pair_A` (100°) | `clahe_off` (default) | **2.929** | **1.571** | **64** | **100.0** | **0.578** |
| `synth_pair_A` (100°) | `clahe_on` | 6.757 | 2.791 | 23 | 68.8 | 0.352 |
| `dsun_sweep/dsun_50` (40°) | `clahe_off` (default) | 1.887 | **1.174** | **99** | 100.0 | **0.749** |
| `dsun_sweep/dsun_50` (40°) | `clahe_on` | **0.664** | 1.322 | 94 | 100.0 | 0.635 |

On `synth_pair_A` the default wins on every column and it is not close — CLAHE on costs
two thirds of the tie-points and a third of the coverage. On `dsun_50` the result **splits
against itself**: CLAHE on is 2.8x better on *true* error (0.664 px against 1.887 px) while
being worse on the held-out figure this repository tells you to quote, on inlier count and
on uniformity. One fixture, two metrics, opposite verdicts.

**The decision stands, and it is now a judgement call rather than pure reasoning.** The
default is the arm that is better on `check_rmse_px` on both fixtures, which is the number
D11 exists to make quotable; and the `gt_rmse_px` win on `dsun_50` is a single pair, not a
trend — no sweep-wide CLAHE A/B has been run. **Anyone presenting this must not claim the
RIFT half is unmeasured, and must not claim it is settled.** `--set photometry.clahe=true`
forces it, so the sweep-wide run that would settle it is one flag away.

**What is still NOT claimed.** The `sift → on` half is untested. No ablation arm in
`bench/baselines.md` pins an intensity matcher *and* toggles CLAHE — `matcher_sift` runs
with CLAHE at its resolved value, and on `synth_pair_A` that arm fails outright. That half
of this decision is reasoned from how the descriptor works, and `clahe_reason` in
`metrics.json` states the reasoning, not a result.

---

## D9 — A thin-plate spline is accepted ONLY on held-out improvement, under two conditions

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 2 Step 5 (TPS for non-rigid local deformation) and Part 1 §4
(independent check points).

**Decision.** With `geometry.tps: "auto"` (the default), `verify_matches` fits a TPS
displacement field on the **control inliers only**, then decides whether to keep it using
the **check** points — which no part of the fit has seen. Both of these must hold:

1. `check_rmse_all_px` strictly improves — RMSE over **all** check points, no threshold.
2. The check points that were **already inside** the RANSAC threshold before the spline do
   not get worse. `settled` is computed from the pre-spline residuals and then held fixed.

Otherwise the spline is discarded, `warp` stays `None`, `model_type` stays projective, and
`tps_status` is `"rejected_no_improvement"`. `tps_check_rmse_before_px` and
`tps_check_rmse_after_px` record the comparison either way. `geometry.tps: true` forces it
(still reporting before/after); `false` disables it. It requires at least
`geometry.tps_min_control` (25) control inliers, else `tps_status = "too_few_control"`.

**Rationale for the first condition.** This is the strongest single answer in the
submission to "how do you know the non-rigid warp is not overfitting?" A TPS with enough
control points interpolates its own control set exactly; measuring it on those points is
not evidence of anything. Measuring it on points it never saw is. The criterion carries no
threshold precisely so that no bar can be moved until the spline passes.

**Rationale for the second condition — and the number that forced it.** The plain
improvement test is not sufficient. Un-thresholded, `check_rmse_all_px` is dominated by
gross mismatches in the check set — a 700 px mismatch jostled by 1 px swamps the sub-pixel
signal the spline is actually on trial for. Measured on `bench/fake_matches`, 15 seeds,
300 points, a similarity transform plus 0.5 px of noise and **no relief at all**, so
"reject" is the only correct verdict:

| gross outliers in the check set | 0% | 5% | 20% |
|---|---|---|---|
| accepted on `check_rmse_all_px` alone | 0/15 | 4/15 | **7/15** |
| accepted on both conditions | 0/15 | 0/15 | **0/15** |

The plain rule accepted a spline reporting 0.467 px against 0.589 px of injected noise —
a warp fitting the noise, dressed as an improvement. On synthetic relief, where a spline
*should* be kept, both conditions accept 5/5, so the second condition costs no true
positive.

`settled` is deliberately not recomputed after the spline: doing so would compare two
different point sets and penalise a spline for the act of pulling an outlier back inside
the threshold. With no settled check point at all, both sides are NaN, the comparison is
false, and the spline is rejected — nothing was available to validate it on.

**Consequence for the contract.** `Registration.params` stays the global 3x3 even when a
spline is applied; the spline is a residual on top, evaluated in the source frame by
`geometry.tps.pullback`. Nothing that reads `params` breaks. `transform.json` carries the
full `warp` block (control points, weights, affine, `lam`, centre, scale) so a TPS run is
reproducible from its artifacts alone.

---

## D10 — SDI is a derived scalar over three real numbers, not a replacement for them

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 1 §2 ("the automated evaluation metrics must natively calculate and
output a Spatial Distribution Index").

**Decision.** `geometry/uniformity.py` emits

```
sdi = (coverage_pct / 100) * 1 / (1 + dispersion_cv)        # [0, 1], 1.0 = perfectly uniform
```

into `uniformity_report` and therefore into `metrics.json`, alongside `sdi_definition` —
the formula as a literal string, so nobody has to guess what it means. It is `null`
whenever either input is `null`, never `0.0`. `coverage_pct`, `dispersion_cv` and the
per-cell `cell_states` array are **unchanged and still reported**.

**Rationale.** The plan asks for an SDI by name, and a jury looking for that name should
find it. But "SDI" is not a standardised quantity with one accepted definition, and this
repository already had a *stronger* description of spatial distribution than any single
scalar: what fraction of usable cells got tie-points, how unevenly they were spread, and —
critically — which cells were excluded from the denominator because they were majority
shadow or nodata. Collapsing that into one number and shipping only the number would trade
information for a label.

So the label is added and the information is kept. The two factors are the two ways a
tie-point field can be non-uniform and they are genuinely independent: `coverage_frac`
penalises empty cells, `1/(1 + dispersion_cv)` penalises unequal counts among the cells
that are populated. A field that fills every cell equally scores 1.0; one that fills half
the cells scores at most 0.5 however evenly it fills them.

**What must not be said about it.** SDI is not comparable to another team's SDI unless
they publish their formula, and it is not an accuracy metric — a perfectly uniform field
of wrong correspondences scores 1.0. Quote it beside `check_rmse_px`, never instead of it.
Measured on `make demo`: coverage 100.0%, dispersion CV 0.7289, **SDI 0.5784** — every
cell populated, and unevenly. One number would not have told you which.

---

## D11 — 20% of the tie-points are held out of the fit, always

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 1 §4 (the "mission-ready" evaluation rig).

**Decision.** `geometry.check_fraction` defaults to **0.2**. The split happens in
`verify_matches` *after* the init gate and *before* any fit. It is **stratified by
`matches.cell`**, so the held-out set is spatially spread rather than clustered, and
**deterministic** — no RNG anywhere; re-running the same pair produces the identical
partition byte for byte. RANSAC, the model ladder, model selection and the TPS all see
control points only. `Registration.roles` records the partition for all N matches (0 =
control, 1 = check). Metrics: `check_rmse_px`, `check_rmse_all_px`, `check_p90_px`,
`n_check`, `n_control`, `n_check_inlier`, `check_outlier_frac`, `check_fraction`,
`check_status`.

**What it costs, stated plainly.** One fifth of the tie-points no longer constrain the
transform. On a pair with 155 usable matches that is 132 fitting points instead of 155 —
and a fit on fewer points is a slightly worse fit, with slightly higher variance. There is
no way to hold points out for free.

**Why it is worth it.** Without the split, every accuracy number this project reports is
measured on the points the model was fitted to. That number is not wrong, it is
*structurally optimistic*, and by an amount nobody can bound from the number itself. A
domain jury knows this. `check_rmse_px` is the only figure in the repository that is not
measured on the fit's own sample, and one honest number is worth more than a set of
flattering ones. It is also what makes D9 possible at all: without held-out points there
is nothing to validate a non-rigid warp against, and a non-rigid warp validated on its own
control points is not validated.

**When the split is skipped, and why it is skipped rather than shrunk.** If it would leave
control below `4 × _MIN_SAMPLE[model]` or check below 8 points, no split is made:
`check_rmse_px` is `null` and `check_status` says `"skipped_too_few_matches"`. A check set
of three points does not measure anything — its RMSE is noise — and reporting it would be
worse than reporting nothing, because it would look like a number. Never silently proceed
with a split too small to mean anything.

**One subtlety worth stating.** `inliers` after the split is the union of the control
RANSAC inliers and the check points whose residual lands inside the threshold. Coverage,
dispersion and SDI measure *delivered tie-points*, and a good check point is a delivered
tie-point — excluding it would understate the product to protect the purity of a
bookkeeping category.

---

## D12 — Invalid pixels are reflected, not zeroed, into the phase-congruency input

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 1 §1 (illumination invariance) — by removing a defect that
partially destroyed it.

**Decision.** `photometry.mask_fill` defaults to `"reflect"`. Invalid pixels
(shadow/nodata/saturated) are filled from their valid neighbourhood before phase
congruency is computed. The **returned** `albedo` is unchanged — still zeroed wherever
`mask != MASK_VALID`, because it is consumed as radiometry and the ablation depends on it.
Only the PC input changes. `mask_fill: "zero"` restores the old path exactly.
`mask_fill` and `mask_fill_px` are recorded in `CanonicalImage.params`.

**The defect.** `albedo[mask != MASK_VALID] = 0.0` followed by `phase_congruency(albedo)`
manufactures a hard step edge at every mask boundary — and that boundary is *positioned by
the sun that cast the shadow*. Sun-dependent structure was being injected into the one map
whose entire purpose is to be sun-independent, and the detector then keyed on it.

**Measured, and the honest answer is "it depends on the mask".** On a contiguous invalid
region — one cast shadow, 29.9% of the frame, `tests/test_photometry.py::masked_scene` at
n=512, so it is rerunnable from this repository — the boundary PC response is **35.5× the
interior with `zero` and 1.07× with `reflect`**. The manufactured edge is gone.

On `fixtures/dsun_sweep`, whose masks are 1.1% scattered single-pixel dark speckle rather
than regions, there is almost no manufactured edge to remove, and registering the 0–50°
sweep both ways gives mean `gt_rmse` 0.926 → 0.940 px with 10–15% fewer inliers. **That is
a wash, and slightly negative on inlier count.** The reason is instructive: the zeroed
speckle was itself a repeatable feature in a synthetic pair rendered from ONE DEM, and
that repeatability is an artefact of the fixture, not a property real imagery has. The
default stays `"reflect"` because the 35.5× number is the physics and the sweep number is
the fixture. **Nobody should quote that sweep as evidence the fill improves accuracy,
because it does not.** The evidence for the fill is the 35.5× → 1.07× line.

The one-switch ablation added to `bench/baselines.md` §2 on 2026-09-02 says the same thing
and says it harder: on `dsun_sweep/dsun_50`, `mask_fill_zero` returns `gt_rmse_px` **0.931
against the default's 1.887**, with 110 inliers against 99. On `synth_pair_A` the two are
indistinguishable (2.929 px both ways, 217 matches against 218). So on the speckle-masked
synthetic fixtures the fill is neutral at best and measurably worse on one pair, and that
belongs in this record rather than in a file nobody cross-reads. The default is defended by
the boundary measurement, and by the argument that speckle repeatability is an artefact of
rendering from one DEM — not by any accuracy number in this repository.

---

## D13 — `output.grid` defaults to `"both"`

**Status:** PROPOSED — needs team ratification. **Date:** 2026-09-02.
**Answers:** plan Part 3 §2 (registered image products, preserving spatial metadata).

**Decision.** `config["output"]["grid"]` ships as `"both"`. `registered.tif` is the source
resampled onto the **reference** grid, as before; `registered_source_grid.tif` is the same
registration at **source** resolution, georeferenced by composing the reference
geotransform with the fitted transform. `"reference"` writes only the first, `"source"`
writes the source-resolution product *as* `registered.tif` — a caller asking for "source"
wants that delivered, not sidelined.

**Rationale.** Writing only on the reference grid throws away the resolution the mission
paid for. On the real `ch2_wac` pair in `runs/`, a 3000×3000 source came back as a 128×128
file: 99.98% of the pixels discarded to satisfy a grid choice nobody was asked about. A
0.25 m OHRC frame registered against a 20 m reference has the same problem by a factor of
6400. Both products are legitimate deliverables and they answer different questions — "how
does the source look in the reference's frame" versus "here is the source, correctly
georeferenced" — so the default produces both and the operator narrows it.

**The cost.** Disk. On `make demo` that is 1.4 MB of `registered.tif` plus 5.7 MB of
`registered_source_grid.tif`. `output.grid: reference` restores the old footprint exactly.

---

## D-cache · the phase-congruency cache key must carry the file path

**Status:** ACCEPTED — a defect fix, not a design choice. **Date:** 2026-09-03.
**Answers:** nothing in the plan. It protects every number the plan asks us to publish.

**Decision.** The `_canonicalise_cached` identity in `samanvay/pipeline/stages.py` carries
the absolute path and `st_mtime_ns`. It previously carried `(product_id, params, size,
int(st_mtime), shape)` and nothing else.

**Rationale.** Every one of those five fields is shared by the fixtures `synth/sweep.py`
emits: one renderer so one `product_id` (`synth_source` on all fourteen), one shape, byte-
identical size (1474265), and second-resolution mtime. Two Δsun steps written inside the
same second therefore shared a cache entry, and the second one silently matched on the
first one's albedo. Reproduced in isolation by copying `dsun_60` and `dsun_70` and forcing
equal mtimes: cache on, both report `gt_rmse_px` 2.1098 / 73 inliers; cache off, the second
reports its own 3.5141 / 49. The failure mode is a *plausible wrong number*, not a crash —
the worst kind for a project whose entire claim is that its numbers mean what they say.

**Blast radius, checked rather than assumed.** The shipped sweep fixtures are 37 s apart,
so no two ever collided and `bench/baselines.md` §1 is unaffected — confirmed independently
by `make bench`, which now reproduces all fourteen rows distinctly. The landmine was live
for anyone regenerating fixtures faster than one per second, which a smaller `--size` or a
faster machine does.

**The cost.** None. A path and a nanosecond timestamp in a hash. `tests/test_pipeline.py::
test_cache_key_separates_two_products_that_look_identical` fails without the fix and passes
with it — verified in both directions, because a regression test that passes before the fix
tests nothing.
