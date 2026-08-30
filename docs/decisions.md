# SAMANVAY — Architecture Decision Record

SIH26166 · lunar image registration engine.

Every decision below is dated **2026-08-29** and marked **PROPOSED — needs team
ratification**. Several are explicitly joint calls (D2, D4, D5); nothing here is
settled until the team says it is. If you disagree with one, say so in the ratification
pass rather than quietly writing code that assumes the opposite — that is precisely the
failure mode this file exists to prevent.

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
