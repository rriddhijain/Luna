# SAMANVAY — what it does not do, and where it fails

SIH26166. Last reviewed 2026-08-29.

This is the honest list. It is the source for the "known limitations" slide, and it is
deliberately written before anyone asks. A domain jury trusts a team that can name its own
failure modes; it does not trust a team whose system apparently has none.

Nothing here is an apology. Each entry is a scope boundary or a measured ceiling, with the
upgrade path where one exists. Where the code cuts a corner deliberately, the source
carries a `ponytail:` comment naming the same ceiling — the two lists agree.

---

## 1. The DEM is far coarser than the imagery, and physics cannot be rendered at OHRC scale

SLDEM2015 is ~60 m/px. OHRC is ~0.25 m/px. That is a factor of ~240. **We do not render
0.25 m shading from a 60 m DEM, and no result claims we do.**

What actually happens is a three-way split, recorded per product in
`CanonicalImage.params["illum_mode"]` (see `docs/decisions.md` D2):

- `dem` — full physical illumination removal. Only when the DEM is within ~4x of the image
  GSD. Realistic for TMC and IIRS; **not** for OHRC against SLDEM.
- `dem_lowfreq` — only the low-frequency illumination field is divided out. Everything
  finer than the DEM can resolve is left alone, because it is interpolation, not terrain.
  This is the OHRC-against-SLDEM case.
- `empirical` — no usable DEM, or missing sun geometry: a flat-field estimate from the
  image's own low-frequency envelope.

**Consequence.** The full physics claim is demonstrated at TMC/IIRS scale. At OHRC scale
the claim is low-frequency illumination removal plus phase congruency for fine structure,
and that is what the report says. Anyone quoting a "physics-based correction" number at
OHRC scale without naming the DEM is quoting a number this project does not stand behind.

**Downstream effect.** Cast shadows are computed from the DEM, so at coarse-DEM scale the
predicted shadow mask captures large-scale terrain shadowing and misses small-crater
shadows entirely. Those pixels are marked valid when they are not, and they contribute
outliers. The shadow ray-march is also capped at a fixed step count, so very long shadows
at low sun elevation are truncated.

---

## 2. Low-texture terrain returns "insufficient texture", not matches

Smooth mare surfaces, and any region where the illumination is flat, genuinely do not
contain tie-points. The system reports that rather than manufacturing correspondences.

Every uniformity grid cell lands in exactly one of three states
(`geometry/uniformity.py`):

- `populated` — tie-points found.
- `insufficient_texture` — the cell was attempted and nothing survived. **A real,
  reported failure.**
- `masked_invalid` — the cell is majority shadow/nodata/saturated. Excluded from the
  coverage *denominator*, because a cell you correctly declined to match is not a miss.

**Consequence you should expect:** on mare-dominated scenes, `coverage_pct` will be low
and `dispersion_cv` high, and that is the correct answer, not a bug. The fix for a real
scene is a different sensor band, a different reference tier, or accepting that this
region does not support a dense uniform tie-point field — not a lower quality bar.

**What we do not do:** fabricate a match to fill a cell, interpolate a tie-point from its
neighbours, or count a masked cell as covered. A coverage percentage that cannot be traced
to real correspondences is worse than a low one.

---

## 3. Near-upright imagery is assumed

Relative rotation beyond roughly 15 degrees degrades the default matching path (see
`docs/decisions.md` D3). Rotation-invariant mode exists but costs several-fold runtime and
a lower inlier ratio, and choosing it is currently manual — the pipeline does not yet infer
the rotation from the coarse init and switch by itself, although it has the information to.

**Failure is graceful, not silent:** cells return `insufficient_texture`, coverage drops,
and the model ladder reports a failed fit with a reason string. You get a bad-looking
honest result, not a good-looking wrong one. An ascending-vs-descending orbit pair is a
completely realistic input that this prototype will handle poorly.

---

## 4. No cross-tier chaining

Registering OHRC (0.25 m) directly against WAC (100 m) is a ~400x scale step. The right
answer is to chain through an intermediate tier — OHRC → NAC → WAC — composing the
transforms and propagating the uncertainty. **That is not implemented.** Every run
registers exactly one source against exactly one reference.

**Consequence.** For extreme scale ratios you must supply an appropriate reference
yourself. The coarse init will still position the pair from metadata, but the descriptor
matching has no scale-bridging strategy beyond what SIFT's own scale space provides.

---

## 5. No learned matcher

Everything here is classical: SIFT/ORB descriptors, phase-congruency keypoints, log-Gabor
filters, RANSAC/MAGSAC. There is no SuperPoint, no LoFTR, no learned detector or matcher,
and no trained model of any kind in the repository.

This is a deliberate scope choice — classical methods are inspectable, deterministic, need
no training data (which for Chandrayaan-2/LRO cross-registration barely exists in labelled
form), and run without a GPU. It is still a limitation: on hard cross-modal pairs a learned
matcher would very likely beat this, and the `method` field in `MatchSet` reserves a code
for one so it can be added as another rung rather than a rewrite.

---

## 6. The synthetic-to-real gap is unmeasured

**This is the largest single unknown in the project.**

Every number the system currently reports comes from `fixtures/synth_pair_A`, a rendered
DEM warped through a known homography. `docs/decisions.md` D6 documents what is mitigated
(analytic ground-truth points, a held-out point set, independent forward photometry, a
physically-decimated reference) and what is not. What is not mitigated:

- The source raster is still a resampled version of the master render, so its
  high-frequency content is shaped by the cubic kernel. A matcher that refines with a
  similar interpolator scores slightly better here than it will on real data.
- Both images derive from the **same** DEM and the **same** albedo field. Real pairs differ
  in genuine surface change, in detail the coarser sensor never resolved, and in
  per-instrument MTF. The fixture measures illumination and geometry robustness only.
- Real data brings compression artefacts, detector striping, bad lines, and metadata that
  is wrong rather than merely absent. None of that is in the fixture.

**Therefore every synthetic accuracy number is an upper bound, reported as such, with the
held-out set alongside the tuned set.** Until real Chandrayaan-2/LRO pairs are processed,
the real-world accuracy of this system is **not known**, and no slide should imply
otherwise. `bench/baselines.md` records "not measured" rather than a placeholder, for
exactly this reason.

---

## 7. IIRS input is not yet well-defined

IIRS is an imaging spectrometer. "The IIRS image" is not a thing until a band or a
composite is chosen, and the team has not chosen one (`docs/decisions.md` D5 is **OPEN**).
Today the loader reads band 1 of whatever raster it is handed and makes no claim about what
that band is. Any IIRS result carries that caveat.

---

## 8. Operational and engineering limits

- **Whole-image warp.** The final resample runs `cv2.warpPerspective` over the full array
  in memory. Products above ~512 MiB load as a `TiledReader` instead of an ndarray, and the
  warp step does not consume one. Very large products will therefore fail at the warp, not
  register slowly. The tiling machinery exists (`core/tiling.py`); the warp path does not
  use it yet.
- **The output is a 2-D resample, not an orthorectification.** `registered.tif` is the
  source pushed through one global similarity/affine/homography onto the reference grid.
  Terrain-induced parallax is *not* corrected per pixel. On high-relief terrain with a
  significant emission-angle difference, residual terrain-dependent error remains and is
  not modelled.
- **Per-point uncertainty is an isotropic scalar.** `sigma` is a Förstner-style scalar in
  source pixels, not a 2x2 covariance, so a tie-point on a linear rim — well-constrained
  across the ridge, loose along it — reports the same uncertainty in both axes. The
  calibration constant was fitted on synthetic Gaussian texture and can read up to ~3x
  optimistic where interpolation bias dominates rather than noise.
- **Metadata gaps are reported, not filled.** A missing sun angle makes the product
  `empirical` rather than assuming a plausible geometry; an unknown quantity is `null` in
  `metrics.json` rather than `0.0`. This means some runs legitimately produce fewer numbers
  than others, and a `null` is information, not a bug.
- **A unitless map scale in a PDS label is read as metres/pixel.** True for LRO and
  Chandrayaan products; a label using different units would be misread.
- **No GPU path, no multiprocessing, no per-stage timing.** Only total wall-clock is
  measured from outside (`bench/harness.py`); the pipeline does not yet write per-stage
  times into `metrics.json`.
- **Failure modes return an honest failed result, not an exception.** Zero matches, all
  outliers, collinear points, an empty tile, an all-nodata image, or a DEM that does not
  cover the scene each produce a `Registration` with `model_type="failed"`, a `reason`
  string, `rmse_px = NaN` and zero inliers. This is intentional. It also means **a run that
  "completed" is not automatically a run that succeeded** — check `metrics["status"]`.

---

## 9. Currently broken / in flight

Tracked separately from the design limits above, because these are expected to close.

- `samanvay/pipeline/stages.py` does not unpack the `(MatchSet, cell_info)` tuple that
  `match_tiled` now returns, does not pass a coarse init into matching or verification, and
  does not call `geometry.metrics.compute_metrics`. The end-to-end CLI therefore fails, and
  CI is red until it is fixed. See `README.md` → Status.
- `viewer/index.html` is a static mockup. Its metric tiles are **hardcoded placeholder
  numbers** — `RMSE 0.42px`, `Inliers 48/50`, `Coverage 93.8%`, `Runtime 0.18s` — and it
  reads no run directory. Nothing in the pipeline produces those values. Do not screenshot
  it, present it, or quote it as a result until it is wired to `runs/<name>/metrics.json`;
  a fabricated 0.42 px on a slide is the single fastest way to lose a technical jury.
- `bench/baselines.md` is entirely "not measured".
- No real Chandrayaan-2 / LRO pair has been processed by this code.

---

## Added 2026-08-30 — cascade, LSM, ISIS3

**ISIS3 export is format-compatible, not validated.** `samanvay/io/isis.py` writes a PVL
control network and a tie-point CSV, and every run now emits `control_network.pvl` and
`tiepoints.csv`. **No ISIS3 binary has ever opened them** — none is installed and none
could be. Before demo day somebody must run
`cnetpvl2bin from=control_network.pvl to=x.net` and load it in `qnet`/`cneteditor`.
Until then the only honest claim is *"we emit the format"*. Two further caveats already
stated inside the generated file: the `SerialNumber` values are placeholders of the form
`SAMANVAY/<instrument>/<product_id>`, not the serial ISIS derives from a cube (replace
with `getsn` output before `jigsaw`); and residuals are written as PVL comments rather
than `SampleResidual`/`LineResidual`, because SAMANVAY residuals are in *source* pixels
while ISIS expects each measure's own frame — writing them into those keywords would be
a unit lie.

**The cascade is a 2-level algorithm at fixture sizes.** `min_level_side` binds before
the scale ratio does, so ratios of 2×, 8× and 16× all descend exactly 2 levels on our
fixtures. The level rule is correct and tested against ratio-scaled shapes, but
**multi-level descents are unproven on mission-sized imagery** — do not promise them
until someone runs genuinely large arrays.

**Two inlier counts exist and they are different fits.** `metrics.json["cascade"]["inlier_count"]`
is the count at the cascade's final level; `metrics.json["inlier_count"]` is from
re-verifying the returned matches at full source resolution. Measured 189 vs 176 on one
pair. Never quote one beside the other's RMSE.

**LSM is not the default, on measured evidence.** Phase correlation wins on sharp clean
texture; LSM wins where the image is blurred or noisy, and its real virtue under hard
cross-illumination is that it *refuses* — on `synth_pair_A` phase correlation degrades
the whole tie-point set below its unrefined state (0.99 → 2.50 source px) while LSM
rejects almost everything and leaves it at 0.99. Select with `--set refine.method=lsm`.
LSM's sigma is a raw a-posteriori estimate measuring ~0.7× honest under matched
illumination and ~4× optimistic cross-sun; it is **not interchangeable** with
`refine.py`'s calibrated sigma. Never average the two.

**One published number was withdrawn.** An earlier report of a homography fit returning
`rmse_px` 5.2e-13 against 3326 px of true error at a 16× scale ratio could **not** be
reproduced by an independent audit (0 of 200 RANSAC seeds produced a non-failed fit;
`verify_matches` refused outright instead). The mechanism it illustrates is real and
tested — the redundancy gate flags exactly this case — but that specific figure must not
appear on a slide until someone re-derives it and saves the artifact.
