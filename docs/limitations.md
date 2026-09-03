# SAMANVAY — what it does not do, and where it fails

SIH26166. Last reviewed **2026-09-03**. Section 9 was rewritten from the code on
2026-09-02 and amended on 2026-09-03 with what an end-to-end walk of every documented
entry point turned up; sections 1-8 were re-read and left standing, with the amendments
marked below.

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

**This is the largest single unknown in the project.** Amended 2026-09-02: real pairs
*have* now been registered, and they still do not close this gap, because none of them has
ground truth. Read §9's amendment to this section alongside it.

Every number the system reports **with a truth reference** comes from
`fixtures/synth_pair_A` or `fixtures/dsun_sweep`, a rendered DEM warped through a known
homography. `docs/decisions.md` D6 documents what is mitigated
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
held-out set alongside the tuned set.** The real-world *accuracy* of this system is still
**not known** — real pairs now run, but `gt_rmse_px` is `null` on every one of them, so
what they produce is held-out self-consistency, not geodetic truth. No slide should imply
otherwise.

---

## 7. IIRS input is not yet well-defined

IIRS is an imaging spectrometer. "The IIRS image" is not a thing until a band or a
composite is chosen, and the team has not chosen one (`docs/decisions.md` D5 is **OPEN**).

Amended 2026-09-02: the loader no longer reads band 1 blindly. `samanvay/io/bands.py`
SNR-screens the bands, drops the noisy ones and returns the first principal component
(`band.reduce: "pc1"`, the default) — the plan's pseudo-panchromatic map. That is a
*default*, not the team's answer: it is candidate 3 of the three D5 lists, chosen because
the plan names it, with its known cost (a data-dependent basis that changes scene to
scene) unaddressed. **It has never run on a real IIRS cube** — only on synthetic ones in
`tests/test_io.py`. Any IIRS result carries both caveats, and
`Product.meta["band_reduction"]` records exactly what was done.

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

## 9. Currently broken / in flight — rewritten from the code 2026-09-02, amended 2026-09-03

**Every item this section used to contain was false, and the falsehoods ran in both
directions.** They are recorded here rather than deleted, because a limitations file whose
own history is invisible is a limitations file nobody can audit.

**Withdrawn — these four claims were checked on 2026-09-02 and are wrong:**

| the old claim | what is actually true |
|---|---|
| "`stages.py` does not unpack the `(MatchSet, cell_info)` tuple, does not pass a coarse init, does not call `compute_metrics`. The end-to-end CLI therefore fails" | All three are false. `make demo` registers `fixtures/synth_pair_A` and exits 0; `metrics.json` carries the full contract including `cell_info` |
| "three tests fail" (`README.md`), "all on the same `match_tiled` tuple seam" | `.venv/bin/pytest -q` → **429 passed, 0 failed** of 429 collected, 56.7 s (2026-09-03). The one failure this table reported on 2026-09-02 — `test_preflight.py::test_scan_ranks_the_real_fixture_sweep`, a fixture-vs-test disagreement between two seats after the Δsun sweep was extended to 180° — has since been closed |
| "`viewer/index.html` is a static mockup with hardcoded placeholder metrics — RMSE 0.42px, Inliers 48/50, Coverage 93.8%, Runtime 0.18s" | None of those four strings appears anywhere in `viewer/app.js` or `viewer/index.html`. The page parses one embedded JSON payload written by `report.dashboard.build_viewer` and renders every number from it; with no payload it says "no run loaded" rather than showing a figure. Verified: `runs/demo_01/viewer.html` contains that run's own `check_rmse_px`, 1.5712 |
| "`bench/baselines.md` is entirely 'not measured'" · "no real pair has been processed" | As rewritten on 2026-09-02, `baselines.md` carries a measured 14-pair Δsun sweep (§1), a stage ablation (§2, 18 arms as of 2026-09-03) and a real-LROC-NAC section (§3), each dated with the command that produces it. Real LROC NAC and Chandrayaan-2 pairs **have** been registered — see the amendment to §6 below |

The last of those did real damage while it stood. `README.md` tells the reader to consult
this file before quoting a number; this file then told them our own visualiser was
fabricated. That converts the project's best asset — self-declared limits — into evidence
we do not know our own code.

### What is actually in flight, verified 2026-09-02

- **The inlier ratio misses the plan's 0.85 bar on every pair anyone would demo.**
  `fixtures/synth_pair_A` → 0.294. `fixtures/dsun_sweep/dsun_50 --dem` → 0.576. Real LROC
  NAC `dsun115` → 0.829, the closest anything gets. Real NAC `dsun085` → 0.037. Real
  `ch2_wac` → 0.167. On the 14-pair synthetic sweep the shipped default clears the bar on
  three pairs only — Δ0°, Δ10°, Δ20°, at 0.994 / 0.986 / 0.930, all of which resolve to
  SIFT — and runs 0.691 down to 0.333 across Δ30°–Δ180° (`bench/baselines.md` §1). So the
  bar is cleared exactly where the project's differentiator is not needed. The CLI prints
  `inlier ratio vs plan FAIL (0.2936 vs 0.85)` and `metrics.json` carries
  `inlier_ratio_pass: false`. This is a real, current miss against a stated requirement,
  and it is not hidden anywhere in the output.
- **`--seed` controls nothing.** Verified: identical output under seeds 42 and 999. The
  pipeline now says so out loud — `seed_applied: false` with `seed_reason` in
  `metrics.json` — rather than implying reproducibility control it does not have. The
  determinism the project does have (the control/check split, ANMS, the fixtures) comes
  from having no RNG at all, not from seeding one.
- **`residual_units` never reaches `metrics.json`.** `verify_matches` sets it; the
  pipeline's passthrough does not copy it. A consumer that wants to assert the residual
  frame must read the `Registration`. Same for `Product.meta["band_reduction"]` and
  `Product.meta["read_decimation"]`: recorded on the product, not surfaced in any artifact.
- **The container has never been built and CI has never run on GitHub.** Re-checked
  2026-09-03: `docker version` reports client 29.5.3 and then fails to reach the API at
  `unix://…/.colima/default/docker.sock`, and `docker compose version` answers "unknown
  command". `tests/test_deploy.py` asserts what a Dockerfile can be asserted about
  statically — 3.11 base, lock installed before source, non-root final `USER`,
  `MPLBACKEND=Agg` — and nothing more. Nobody has watched the image start.
- **`bench/baselines.md` §5 is an archive of superseded tables and must not be quoted.**
  The file opens with an index naming what each section measured and when; §1–§3 are the
  2026-09-02 measurements, §4 is descriptor-level evidence from 2026-08-29, and §5 keeps
  one 2026-08-30 table because it is the before-half of a comparison. It is labelled "Do
  not quote it as current" and that label is load-bearing: the pre-revamp numbers (RIFT
  173 inliers at Δ50°, SIFT zero) are *not* the current ones (99 and 9 respectively), and
  they were quoted as current in this repository's own docs until 2026-09-02.

### New limits this revamp created or revealed

- **CLAHE off on the RIFT arm is contradicted by true error across the whole Δsun
  range, and defended only by held-out error.** `photometry.clahe: auto` turns CLAHE off
  for RIFT and on for SIFT/ORB. An earlier revision of this bullet called the RIFT half
  "split", on two fixtures. Measured across five Δsun fixtures on 2026-09-03 (`samanvay
  register`, `--set cache.enabled=false`, no DEM, one switch at a time) it is not split;
  it is consistent and it goes against the default on true error:

  | fixture | `gt_rmse_px` default / CLAHE on | `check_rmse_px` default / CLAHE on | inliers default / on |
  |---|---|---|---|
  | `dsun_30` | 1.143 / **0.530** | 1.415 / **0.621** | 183 / 190 |
  | `dsun_50` | 1.887 / **0.664** | **1.174** / 1.322 | 99 / 94 |
  | `dsun_80` | 4.512 / **1.305** | **1.152** / 2.164 | 49 / 36 |
  | `dsun_120` | 6.824 / **1.821** | **1.449** / 1.816 | 46 / 41 |
  | `dsun_180` | **0.592** / 1.041 | **1.298** / 1.401 | 64 / **119** |
  | mean | 2.991 / **1.072** | **1.298** / 1.465 | 88.2 / 96.0 |

  CLAHE on is **2.8x better on mean true error** and 13% worse on mean held-out error.
  On `fixtures/synth_pair_A` it is decisively worse on every column (`gt_rmse_px` 6.757
  against 2.929, 23 inliers against 64, 68.75% coverage against 100%) — that pair is 2x
  scale and 10° rotation as well as Δsun, so it is not the same experiment. The default
  therefore stands on the held-out figure plus one adversarial fixture, against a true-error
  measurement that runs the other way on four of five sweep pairs. **That is a live
  disagreement, not a settled call**, and it is the photometry seat's and the lead's to
  settle; it is recorded here rather than acted on. **The SIFT half is still genuinely
  untested** — no ablation arm pins an intensity matcher and toggles CLAHE. See
  `docs/decisions.md` D8.
- **`mask_fill: reflect` is defended by a boundary-response measurement, not an accuracy
  one.** On a contiguous cast shadow the manufactured edge goes from 35.5x the interior PC
  response to 1.07x. On `fixtures/dsun_sweep`, whose masks are 1.1% scattered speckle, the
  fill does not buy accuracy. Measured 2026-09-03 the same way as the CLAHE table above:

  | fixture | `gt_rmse_px` reflect / zero | `check_rmse_px` reflect / zero | inliers reflect / zero |
  |---|---|---|---|
  | `dsun_20` (SIFT arm, no PC) | 0.206 / 0.206 | 0.391 / 0.391 | 292 / 292 |
  | `dsun_30` | 1.143 / **0.950** | 1.415 / **1.089** | 183 / **199** |
  | `dsun_50` | 1.887 / **0.931** | 1.174 / **1.016** | 99 / **110** |
  | `dsun_80` | 4.512 / **3.801** | **1.152** / 1.521 | 49 / **57** |
  | `dsun_120` | 6.824 / **5.399** | **1.449** / 1.515 | 46 / **51** |
  | `dsun_180` | **0.592** / 3.366 | **1.298** / 2.234 | **64** / 57 |
  | mean, five RIFT pairs | 2.991 / **2.890** | **1.298** / 1.475 | 88.2 / **94.8** |

  So the ablation's single `dsun_50` row — `mask_fill_zero` 0.931 px against the default's
  1.887 px — is the *most* favourable point for `zero`, not a representative one, and an
  earlier revision of the README read it as "zero beats reflect on every column". Across
  the range `zero` wins on true error on four of five RIFT pairs and loses badly on the
  fifth, while `reflect` wins on mean held-out error. Do not quote either direction as
  evidence the fill helps or hurts accuracy: on these masks it is close to a wash. The
  evidence for the fill is the 35.5x → 1.07x boundary line and the argument that speckle
  repeatability is a rendering artefact (`docs/decisions.md` D12), and no fixture here has
  the contiguous cast shadow the fill exists for.
- **The held-out check set is small on hard pairs, and null on some.** `make demo` splits
  132 control / 23 check, so `check_rmse_px` is an RMSE over 23 points and carries the
  variance of 23 points. On the real `ch2_wac` pair the split was skipped entirely
  (`check_status: "skipped_too_few_matches"`, `check_rmse_px: null`) because there were
  not enough matches to hold any out. A pair with no held-out number has no independently
  measured accuracy, and the file says so instead of substituting the in-sample one.
- **`check_rmse_px` is thresholded; `check_rmse_all_px` is not.** The headline held-out
  figure is RMSE over check points *within the RANSAC threshold*, which excludes gross
  mismatches. On `make demo` that is 1.5712 px against `check_rmse_all_px` 6.7293 px and
  `check_outlier_frac` 0.565 — more than half the held-out points are outside the
  threshold. Quote `check_rmse_px` **with** the outlier fraction beside it, or the number
  flatters.
- **The TPS is accepted on the strength of ~20 held-out points.** D9's two conditions make
  a bad spline hard to accept, but the evidence base is the check set, and on a small one
  the decision is correspondingly noisy. `tps_check_rmse_before_px` /
  `_after_px` are always reported so the margin is visible: on `make demo` it was
  6.9059 → 6.7293 px, which is a thin margin honestly recorded.
- **A near-zero `rmse_px` still happens, and is now labelled rather than believed.**
  Registering the real `ch2_wac` pair on 2026-09-02 produced `rmse_px = 0.0` from 4 inliers
  on a 4-dof similarity — redundancy 2. `metrics.json` carries
  `rmse_trustworthy: false` and an `rmse_warning` naming the redundancy, and
  `check_rmse_px` is `null`. The mechanism from the withdrawn 5.2e-13 report (below) is
  real; the guard now fires on it. **A zero RMSE in this repository means "no redundancy",
  never "perfect fit".**
- **The N x M grid caps the long axis at 64 cells.** A 888 x 11952 NAC strip is 13.5:1;
  the cap binds on anything past 16:1 at `grid_n=4`, and beyond it cells stop being
  near-square again. Stated so nobody discovers it on a limb strip.
- **PCA band reduction has never run on a real IIRS cube.** `io/bands.py` is unit-tested
  on synthetic cubes; no Chandrayaan-2 IIRS product has been through it. The single-band
  path is proven identical to `src.read(1)`; the `pc1` path is not proven on real data.
- **Read-time decimation is implemented but off by default.** `load_product(max_pixels=)`
  exists, folds the factor into `gsd_m` and records it. Nothing in the pipeline passes a
  budget, so the gap `docs/CONTRACTS.md` describes — a `TiledReader` above 512 MiB that the
  warp path cannot consume — is still open in practice.

### Found 2026-09-03 by walking every documented entry point

Every `make` target, every README command and the CI smoke body were run as a judge would
run them. Three findings, all of the same kind: not a crash, but a number that does not
support the claim standing on top of it.

#### 1. The phase-congruency cache collides across different products, and it silently corrupts the Δsun sweep

`pipeline/stages.py::_canonicalise_cached` keys the cache on `(product_id, photometry
params, file size, int(mtime), shape)`. Every fixture in `fixtures/dsun_sweep` declares
`product_id: "synth_source"`, every source raster is exactly 1474265 bytes, and
`python -m synth.sweep` writes several of them inside one wall-clock second — so the key
has nothing left to discriminate on.

**What it does.** Measured 2026-09-03: `dsun_70` registered on a cache warmed by `dsun_60`
returns `gt_rmse_px` **2.1098**, 73 inliers, 153 matches — `dsun_60`'s row, exactly. With
`.cache` removed first it returns its own **3.5141**, 49 inliers, 126 matches. The same
collision hits `dsun_100`/`dsun_120`. `dsun_20`/`dsun_30` share an mtime second too and do
*not* collide, because one resolves to SIFT and the other to RIFT and that changes the
photometry params inside the key — which is the proof that the mtime is the only thing
separating the rest.

**Clearing the cache first does not save you.** Measured with `.cache` removed immediately
before `bench.harness`, the same two of fourteen rows are still wrong: the harness warms
the cache as it iterates, so pair 7 collides with the entry pair 6 just wrote. Giving the
fourteen fixtures distinct mtimes and re-running makes all fourteen distinct **and
reproduces `docs/HANDOVER.md` §1 exactly** — both the proof of cause and an independent
confirmation that the §1 table itself is correct.

**It is a race, not a deterministic bug, which is worse.** `python -m synth.sweep` into a
clean directory took 29 s here and left the fourteen sources 2 s apart — no collision —
while the fourteen committed to this repository were written faster and several share a
second. A quicker machine collides. The sweep is reproducible on one machine and wrong on
another, with no signal either way.

**Where it stands.** An earlier revision of `make bench` and of the `bench` compose
service did `rm -rf .cache` first and this file said that was a partial guard. It is not a
guard at all against the failure that matters: measured with `.cache` removed immediately
before the run, the same two of fourteen rows were still another pair's numbers, because
the harness warms the cache as it iterates. Both surfaces now run the manifest through
`bench/harness.py`'s own `SWEEP_CONFIG = {"cache": {"enabled": False}}`, which is what
`run_arms()` already does and why `bench/baselines.md` §1 was measured correctly. Verified
2026-09-03: `make bench` then returns fourteen distinct rows reproducing §1 exactly, in 85 s
of pipeline time. **The raw `python -m bench.harness --manifest …` CLI is still wrong**,
because `run_manifest()` does not apply `SWEEP_CONFIG` and the CLI exposes no way to pass
it. Two fixes are still owed, both outside this file's ownership: apply `SWEEP_CONFIG` on
the manifest path, and **fix the key** — put the file path or a content hash into it, in
`pipeline/stages.py::_canonicalise_cached`. Until the key is fixed, any sweep run with the
cache on is suspect on any two pairs sharing a resolved photometry arm and an mtime second.

**One more thing a reader must know before trusting a local sweep.** The fourteen
`fixtures/dsun_sweep/*/source.tif` files on this machine had their mtimes rewritten with
`os.utime` on 2026-09-03 (content untouched; git does not track mtime) to prove the
diagnosis. That currently disarms the collision here. A fresh `python -m synth.sweep
--force` re-randomises them and may re-arm it. **Do not read the current correctness of a
local sweep as evidence the defect is fixed** — it is fixed when the cache key changes.

#### 2. ANMS almost never binds on the shipped fixtures, so its ablation row measures nothing

ANMS runs only when a cell offers more candidates than `match.max_matches` (50). Counted
from `cell_info` on 2026-09-03, and re-counted the same day by a second reader: across
the 14-pair sweep it fired in **33 of 224 cells** — 16 on the Δ0° pair, 16 on the Δ10°
pair (790 and 780 inliers, both SIFT), and exactly **one** on the Δ30° pair (183 inliers,
RIFT). The Δ20° pair is the third densest at 292 inliers and fires in **none**: its
busiest cell offers 26 candidates against a quota of 50, so "the three densest pairs" is
the wrong description and the earlier revision of this line said it. On
`fixtures/synth_pair_A` it fired in **0 of 16**. That is why `anms_off` in the ablation is
byte-identical to the baseline — the switch had nothing to switch, and that row must never
be presented as evidence either way.

The end-to-end evidence that does exist comes from forcing the quota down to 10, where it
does bind. It says the same ambiguous thing on both fixtures:

| pair, `match.max_matches=10` | `gt_rmse_px` ANMS on / off | `check_rmse_px` on / off | inliers on / off |
|---|---|---|---|
| `fixtures/dsun_sweep/dsun_50` | **1.396** / 1.652 | 1.314 / **1.105** | 83 / 83 |
| `fixtures/synth_pair_A` | **3.013** / 3.179 | 2.171 / **0.757** | 38 / 46 |

Those four runs were checked at the cell level, not just at the metric: with
`match.max_matches=10` the ANMS branch is entered on **12 of 16 cells** on `dsun_50` and
**9 of 16** on `synth_pair_A` (`cell_info.cells[*].anms == true`), against **0 of 16** on
both with `match.anms=false` and 0 of 16 at the shipped quota of 50. The regime is real;
it is just not the shipped one.

**ANMS wins on true error and loses on held-out error, on both pairs, at a quota the
shipped config does not use.** That is the whole end-to-end case for it today. The
implementation itself is correct and unit-tested in `tests/test_tile.py`; what is missing
is a fixture dense enough to exercise it at the shipped quota of 50.

#### 3. Eight of the eighteen ablation rows on `synth_pair_A` are silent no-ops

Measured 2026-09-03, `python -m bench.ablate` on that pair, 18 arms, 9 min 25 s, and each
arm below independently re-run on 2026-09-03 as a single `samanvay register --set
cache.enabled=false` with one switch. **Eight** arms that are *supposed* to change
something return exactly the baseline — `gt_rmse_px` 2.9287054567839337, `check_rmse_px`
1.5712308090704932, 64 inliers, 100% coverage, SDI 0.5784128280412532:

| inert arm | why, on this pair |
|---|---|
| `photometric_model_none` | no `--dem` is passed, so the photometric model never runs |
| `photometric_lunar_lambert` | same |
| `mask_fill_zero` | the mask is 1.1% scattered speckle; there is no boundary to fill across |
| `clahe_off` | `clahe: auto` already resolves to off on the RIFT arm, so this arm restates the default |
| `anms_off` | ANMS never reaches the per-cell quota here (finding 2) |
| `uniformity_off` | the switch changes no surviving point on this pair |
| `quota10_uniformity_off` | with uniformity off the per-cell quota is not applied at all, so forcing it to 10 changes nothing |
| `tps_forced` | `tps: auto` already accepted the spline on this pair, so forcing it changes nothing |

An earlier revision of this table listed six, omitting `clahe_off` and `tps_forced` —
which understated exactly the problem this finding exists to name. Each reason is
legitimate on its own. The problem is the reading: an ablation table where **eight of
eighteen** rows are identical says "these stages do nothing", which is the opposite of
what it exists to show. **The rows must be labelled inert in the table itself, or the table
argues against the project.**

The arms that do move on this pair, for contrast: `canonicaliser_off` and
`phase_congruency_off` (both to a failed fit, `gt_rmse_px` 28.665 — the error of the prior),
`matcher_sift` (failed, 28.951), `clahe_on` (6.757 px, 23 inliers, 68.75% coverage),
`subpixel_off` (3.523 px, 48 inliers), `tps_off` (3.235 px), `check_split_off` (3.221 px, 59
inliers), and the forced-quota pair `quota10_baseline` (3.013 px, 38 inliers) against
`quota10_anms_off` (3.179 px, 46 inliers) — where, as on `dsun_50`, ANMS wins on true error
and loses on held-out error (`check_rmse_px` 2.171 with, 0.757 without).

### Amendment to §6 — real pairs HAVE been processed; real *accuracy* is still unknown

§6 above says "no real Chandrayaan-2/LRO pair has been processed". That is no longer true
and the sentence is withdrawn. `runs/` contains registrations of:

- **LROC NAC against LROC NAC**, Apollo 16 site, 888 x 11952 strips at 2 m GSD, three
  Δsun-azimuth pairs (4°, 85°, 115°) built by `scripts/download_pairs.py` from ODE.
- **Chandrayaan-2 against Chandrayaan-2** (`ch2_ch2`, 3000 x 3000 at native GSD).
- **Chandrayaan-2 against LRO WAC** (`ch2_wac`, `ch2_wac_wide`).

**Everything §6 says still stands, for a different reason.** Those pairs have **no ground
truth**. The only error figure available on them is self-consistency — and after this
revamp, held-out self-consistency — never true geodetic error. `gt_rmse_px` is `null` on
every one of them. So:

- the synthetic-to-real gap is no longer *unmeasurable* for lack of data, it is
  **unmeasured for lack of truth**;
- the Δsun-85° NAC pair is where it nearly falls over: registered 2026-09-02 with its DEM
  it returns 184 inliers at ratio **0.037** over **42.8%** coverage (SDI 0.170), and the
  archived 2026-08-30 runs of that pair *without* a DEM failed outright on both matcher
  arms (zero matches, `verify_status: "failed"`). A held-out RMSE measured on under half
  the frame is not a frame-wide accuracy claim, and this is exactly the kind of result
  that must not be dropped from a summary;
- no accuracy number in any slide may be sourced from a real pair, because none of them
  has a number that means accuracy.

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
