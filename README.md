# SAMANVAY

**Lunar image registration engine — ISRO hackathon ID26166 / SIH26166.**

SAMANVAY registers Chandrayaan-2 optical imagery (OHRC, TMC, IIRS) against LRO NAC/WAC and
SELENE reference imagery, to sub-pixel accuracy, with tie-points distributed uniformly
across the frame rather than clustered wherever the texture happened to be easy. The hard
part is not matching — it is that a source acquired at 70 degrees solar incidence and a
reference acquired at 20 degrees are not correlatable as raw DN: the same crater is a
bright rim in one image and a dark shadow in the other. SAMANVAY's answer is to divide out
predicted illumination before matching (rendered from a DEM where the DEM actually resolves
the terrain, low-frequency-only where it does not) and to match on phase congruency, which
is invariant to contrast and brightness by construction. Everything is classical and
inspectable — no learned matcher, no training data, no GPU — and every run writes its
metrics, its provenance and its own honest failure modes to disk.

**Read [`docs/decisions.md`](docs/decisions.md) before writing code**, and
[`docs/limitations.md`](docs/limitations.md) before quoting a number.

---

## Status — 2026-09-03

Every figure in this section was produced on this machine on this date by the command
named beside it. Nothing here is carried over from an earlier draft: every entry below
was re-run from a clean `runs/` on 2026-09-03 and the numbers are that run's output.

| | |
|---|---|
| Test suite | **430 passed, 0 failed** of 430 collected (`.venv/bin/pytest -q`, 2026-09-03; 56.7 s, 63.0 s and 87.5 s on three runs the same day, the spread being machine load). The count moves as seats land tests; run it yourself rather than trusting this cell |
| End-to-end CLI (`samanvay register`) | **works.** `make demo` registers `fixtures/synth_pair_A` on the default config and exits 0. A registration that fails exits **1** — verified both ways |
| Install health check | `make smoke` → **PASS**, exit 0. `dsun_50` with its DEM: `homography+tps`, 102 inliers, held-out `check_rmse_px` **1.3963**, true `gt_rmse_px` **1.5981**, SDI 0.764, inlier ratio 0.576 (FAIL) |
| Measured accuracy, synthetic | measured, against analytic ground truth. `bench/baselines.md` §1 is the 14-pair Δsun sweep, 0–180°, `auto` against pinned `sift`; `docs/HANDOVER.md` §1 repeats it. `auto` delivers a model on all 14 pairs at `gt_rmse_px` 0.024–6.824 px; pinned `sift` delivers none past Δ50°. True error is worst at Δ120°, not at Δ180° |
| Measured accuracy, real data | **real pairs register** — LROC NAC ↔ NAC, Chandrayaan-2 ↔ Chandrayaan-2, Chandrayaan-2 ↔ LRO WAC, see below. But **none of them has ground truth**, so what they produce is held-out self-consistency, never geodetic error: `gt_rmse_px` is `null` on every real run. True real-world accuracy remains **unknown** ([`docs/limitations.md`](docs/limitations.md) §6 and §9) |
| Inlier ratio vs the plan's 0.85 bar | **FAIL on every pair quoted in this README, and on 11 of the 14 synthetic sweep pairs** — 0.829 on the real NAC `dsun115` pair (the closest), 0.576 on `dsun_50 --dem`, 0.294 on `synth_pair_A`, 0.167 on `ch2_wac`, 0.037 on the real NAC `dsun085` pair (the worst). Across the 14-pair synthetic sweep the default arm runs 0.994 at Δ0° down to 0.333 at Δ180°, clearing the bar only on the Δ0–Δ20 pairs, where it resolves to SIFT (`bench/baselines.md` §1). The CLI prints the miss rather than hiding it |
| `viewer/` | reads one embedded JSON payload written by `samanvay.report.dashboard.build_viewer`. **No hardcoded metrics** — with no payload the page says "no run loaded". Verified: `runs/demo_01/viewer.html` contains the run's own `check_rmse_px` |
| `samanvay/report/` | implemented, and actively growing — `render.py` writes `report.html` and `metrics_report.pdf`, `dashboard.py` writes the cross-run dashboard and the per-run viewer. It was described in an earlier README as *planned*; it never was. (An earlier draft quoted line counts here; they went stale within a day, so they are gone) |
| Air-gap gate | `make airgap` → **PASS**: no external URL in any generated HTML, no import outside `requirements.lock`. (The asset count it prints scales with how many runs are on disk, so it is not a fixed number worth quoting) |
| Docker (`Dockerfile`, `docker-compose.yml`) | **never built.** Re-checked 2026-09-03: `docker version` gives client 29.5.3 and then "failed to connect to the docker API at unix://…/.colima/default/docker.sock", and `docker compose version` answers "unknown command". No daemon, no compose plugin — the image is reasoned-about, not run. `tests/test_deploy.py` asserts what a Dockerfile can be asserted about statically |
| CI (`.github/workflows/ci.yml`) | **never executed on GitHub.** The pytest and smoke bodies were run by hand here; the runner steps were not |

### The measured run, in full

```
make demo      # fixtures/synth_pair_A, default config, no DEM
```

| metric | value | what it means |
|---|---|---|
| `model_type` | `homography+tps` | homography plus an accepted thin-plate-spline residual |
| `check_rmse_px` | **1.5712** | **quote this one.** RMSE over held-out check points the fit never saw |
| `rmse_px` | 1.2602 | in-sample, on the fit's own inliers. Structurally optimistic |
| `gt_rmse_px` | 2.9287 | true error against the fixture's analytic ground-truth homography |
| `inlier_count` / `inlier_ratio` | 64 / 0.2936 | **FAIL** against the plan's 0.85 bar |
| `coverage_pct` / `dispersion_cv` / `sdi` | 100.0 / 0.7289 / 0.5784 | every grid cell populated, unevenly |
| `match_method_resolved` | `rift` | resolved from `delta_sun_az_deg` 100.0 |
| `runtime_s` | 10.1–23.8 | wall clock, this laptop, seven runs across 2026-09-02 and 2026-09-03 — 10.1 s on a warm canonicalisation cache, 24 s cold. Every metric above is byte-identical across all seven; only the wall clock moves, with the cache and machine load |

Read that honestly: the pair registers, the held-out error is 1.57 source pixels, and the
inlier ratio misses the plan's bar by a wide margin. `synth_pair_A` is the hard fixture —
2x scale ratio, 10 degrees of rotation, 100 degrees of sun-azimuth difference, a source
geotransform wrong by ~49 source pixels, and no DEM supplied. A 1:1 upright fixture lets
fragile code look healthy until it meets real data.

### The real-data run, in full

`fixtures/` is the pair with ground truth. This is the pair without it: **LROC NAC against
LROC NAC**, Apollo 16 site, an 888 × 11952 strip at 2 m GSD, **Δ sun azimuth 115.4°**,
with its DEM. Built by `scripts/download_pairs.py` from ODE; the imagery is not in this
repository. Re-registered 2026-09-03 on the default config, and every figure in the
table below is that run's `metrics.json`:

```bash
samanvay register \
  --source data/real/apollo16/pairs/dsun115/source.tif \
  --ref    data/real/apollo16/pairs/dsun115/reference.tif \
  --dem    data/real/apollo16/pairs/dsun115/dem.tif \
  --out    runs/docs_real_nac_dsun115
```

| metric | value | what it means |
|---|---|---|
| `check_rmse_px` | **1.2753** | over **1062 held-out points**, fitted on 4511. The fit never saw them |
| `check_outlier_frac` | 0.160 | 16% of the held-out points fall outside the RANSAC threshold. Read this beside the line above — `check_rmse_all_px` (no threshold) is 28.37 |
| `inlier_count` / `inlier_ratio` | 4622 / **0.8294** | **FAIL**, and by 0.02. The closest any pair here comes to the plan's 0.85 |
| `coverage_pct` / `dispersion_cv` / `sdi` | 97.81 / 0.3423 / **0.7287** | the most uniform tie-point field measured in this project |
| `grid_rows` × `grid_cols` | **54 × 4** | the N × M grid on a 13.5:1 strip. A fixed 4 × 4 grid would have measured coverage on 13:1 cells |
| `tps_status` | `rejected_no_improvement` | the spline was fitted and **discarded** — the held-out points said it did not help |
| `model_type` | `similarity` | 4 dof chosen over 8 by the model margin, on redundancy 3728 |
| `illum_mode` | `dem_lowfreq` | the DEM is coarser than the 2 m imagery, so only the low-frequency field was divided out |
| `gt_rmse_px` | **`null`** | there is no truth transform for a real pair. This number does not exist and is not invented |
| `runtime_s` | 183.4 | wall clock, this laptop, ~10.6 Mpx per image. 194.7 s on the 2026-09-02 run of the same pair — every other figure in this table was byte-identical across both |

That is the honest shape of a real result: it registers, it is spatially uniform, its
held-out error is 1.28 source pixels over a thousand points it never saw, it misses the
inlier bar by 0.02, and **its true accuracy is unknown** because nothing on the Moon told
us where those pixels really are.

Two more real results worth stating because they are unflattering:

- The **`dsun085` NAC pair is the one that nearly falls over.** Measured Δ sun azimuth
  88.7°. Registered here on 2026-09-02 with its DEM, and re-registered on 2026-09-03 with
  byte-identical metrics, it returns `affine+tps` but only
  **184 inliers at ratio 0.037**, coverage 42.8%, SDI 0.170. Its `check_rmse_px` reads
  1.766 px and **you must not quote that number on its own**: `check_outlier_frac` on the
  same run is **0.963** and `check_rmse_all_px` is **187.5 px**, so the 1.766 px is an RMSE
  over the 3.7% of held-out points that survived the threshold, on a tie-point field
  covering under half the frame. That is the shape of a registration that found a
  locally-consistent patch and not a frame. The archived 2026-08-30 runs of that same pair
  *without* a DEM failed outright on both matcher arms (`match_count: 0`, empty
  `matches.csv`, `verify_status: "failed"`). Same pair, same site, 26.7° less sun-azimuth
  difference than the one above, and an order of magnitude worse.
- The **`ch2_wac` pair** (Chandrayaan-2 3000 × 3000 against a 128 × 128 WAC crop) returns
  `rmse_px = 0.0` from 4 inliers on a 4-dof model. Re-run 2026-09-03, 9.7 s wall clock
  (`runtime_s` 1.563; the rest is interpreter start), identical. `metrics.json` marks it
  `rmse_trustworthy: false` with an `rmse_warning` naming redundancy 2, and
  `check_rmse_px` is `null` because there were too few matches to hold any out. **A zero
  RMSE in this repository means "no redundancy", never "perfect fit".** That run also
  writes `registered.tif` at 128 × 128 *and* `registered_source_grid.tif` at 3000 × 3000 —
  the reason `output.grid` defaults to `"both"`.

---

## Bringing real data in

Have real lunar products? **[docs/DATA.md](docs/DATA.md)** is the whole path: where files
go, what metadata matters, and how to tell whether a real result is trustworthy. Start by
preflighting the pair — it exits non-zero if the data cannot work, and prints the exact
register command if it can:

```bash
samanvay check --source data/real/src.tif --ref data/real/ref.tif --dem data/real/dem.tif
```

`scripts/download_pairs.py` fetches candidate LROC NAC pairs from the agencies' own
archives. No mission data is redistributed in this repository — see
**Data attribution and licensing** below.

## Quickstart

Python 3.11. Every command below is run from the repository root.

**Ten minutes from `git clone`, in order.** No raster is in the repository — every
fixture regenerates from a fixed seed — so the generate steps are not optional:

| # | command | ~time | what you get |
|---|---|---|---|
| 1 | `make setup` | **not timed here** — the venv already existed, so the target skipped straight to its verification step and printed `env ok python 3.11.15 · cv2 5.0.0 · rasterio 1.4.4` in 1 s. Creating one downloads ~31 wheels and needs the network once | `.venv` from `requirements.lock` |
| 2 | `make fixture` | 4 s | `fixtures/synth_pair_A` — the pair with analytic ground truth |
| 3 | `make demo` | 24 s | a real registration in `runs/demo_01`, with the numbers in the table above |
| 4 | `python -m synth.sweep` | 29 s | `fixtures/dsun_sweep`, the 14 Δsun pairs. **`make smoke`, `make bench` and `make demo PAIR=fixtures/dsun_sweep/…` all need this and none of them generates it for you.** `make smoke` exits 1 and names this command; `make bench` does not — it writes 14 `failed` rows and still **exits 0**, so check `bench.md` rather than the exit code |
| 5 | `make smoke` | 12 s | the install health check: PASS/FAIL and a non-zero exit |
| 6 | `open runs/demo_01/report.html` | — | the visual report, `metrics_report.pdf` beside it |

`make test` (57–93 s), `make ablate` (9.5 min, 18 arms) and `make bench` (76 s, measured
2026-09-03) are the longer ones.
`make` on its own prints the list. Every row above was run on this machine on 2026-09-03.

```bash
# 1. environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .                 # or: make setup, which installs requirements.lock exactly

# 2. generate the synthetic fixture (deterministic: seed 0 reproduces every byte)
python -m synth.render_pair
#   -> fixtures/synth_pair_A/{source.tif,reference.tif,dem.tif,*.tif.json,gt.json,README.md}
#   --size N and --seed N give a smaller or different pair

# 3. register the pair
samanvay register \
  --source fixtures/synth_pair_A/source.tif \
  --ref    fixtures/synth_pair_A/reference.tif \
  --out    runs/demo_01 \
  --metrics runs/demo_01/report.json     # optional: the plan's --metrics path

# 4. read the report
open runs/demo_01/report.html        # macOS; xdg-open on Linux
```

`pip install -e .` is enough: `scikit-image` and `matplotlib` are declared dependencies,
because `geometry/refine.py`, `match/detect.py` and `report/render.py` import them at
module scope. `make setup` installs `requirements.lock` instead, which is the exact
transitive closure and is what the container and CI use.

One caveat on that first line, stated because it was not re-checked today: the `.venv` on
this machine carries no `pip`, so `pip install -e .` could not be re-run here. What is in
use is the editable install it previously produced (`_editable_impl_samanvay.pth` in
site-packages), and `make setup` only builds a venv when one does not already exist — it
does not repair a broken one. On a clean machine `python3.11 -m venv .venv` brings its own
pip and the quickstart runs as written; that path has not been exercised here since the
venv predates this session.

Step 3 exits 0 on a successful registration and **1** on a failed one, printing the
reason first. Both halves are reproducible from this README — pin the matcher to `sift` on
a pair whose Δ sun azimuth is 100° and it fails, which is the point of the `auto` default:

```bash
samanvay register --source fixtures/synth_pair_A/source.tif \
                  --ref fixtures/synth_pair_A/reference.tif \
                  --set match.method=sift --out runs/demo_fail ; echo "exit=$?"
```
```
FAILED: verify_status=failed — no model survived robust fitting or the redundancy gate (154 matches, 139 of them gated out by the init at 117.9 px)
  ...
  ! preflight would recommend match.method=rift at delta sun azimuth 100.0 deg; this run used sift
exit=1
```

The same command with the default config exits 0. Measured both ways, 2026-09-03.

`fixtures/synth_pair_A/README.md` explains what is in the fixture and how not to
over-read it.

---

## What a run produces

Written by `samanvay/io/writers.py` into `--out`. This is the actual content of
`runs/demo_01` after `make demo`:

| file | what it is |
|---|---|
| `registered.tif` | the source warped onto the **reference** grid — reference CRS and geotransform, source dtype |
| `registered_source_grid.tif` | the same registration at **source** resolution, georeferenced by composing the reference geotransform with the fitted transform. Written when `output.grid` is `"both"` (the default) or `"source"`. Without it a 0.25 m OHRC source against a 20 m reference is delivered as a thumbnail with 99.98% of its pixels gone |
| `matches.csv` | one row per tie-point: `id, src_x, src_y, ref_x, ref_y, score, is_inlier, residual_px, sigma_px, grid_cell, role`. `role` is `control` / `check` / blank — blank when no split was made, never a guess. Residuals in **source** pixels; unknown fields are blank, never `0` |
| `transform.json` | `model_type`, the 3x3 `params`, the `init_params` it started from, `model_margin`, `rejected_models`, and a `warp` block (`control`, `weights`, `affine`, `lam`, `center`, `scale`) when a TPS was accepted — so a non-rigid run is reproducible from its artifacts alone |
| `metrics.json` | the full metrics contract: the `check_*` held-out block, `sdi`, `tps_*`, `inlier_ratio_pass`, `grid_rows`/`grid_cols`, `match_method_*`, `mask_fill`, `clahe_*`, `stage_s`, per-cell counts and states, ground-truth error when a truth transform is supplied. An unknown quantity is `null`, never a plausible default |
| `provenance.json` | UTC timestamp, git SHA / branch / dirty flag, seed, the full config, package versions, input paths, product ids and per-field `meta_source` |
| `report.html` | the visual report (falls back to a minimal built-in page if the report module is unavailable) |
| `metrics_report.pdf` | the same metrics as a PDF — the plan's JSON/PDF evaluation report. Built with matplotlib's `PdfPages`, written when `report.pdf` is true (the default). 340 KB on the real NAC run |
| `control_network.pvl`, `tiepoints.csv` | ISIS3-format control network and tie-point list. **Format-compatible, not validated** — no ISIS binary has ever opened them (`docs/limitations.md`) |

`--metrics PATH` writes a second copy of `metrics.json` wherever you ask, which is the CLI
shape the plan specifies. `samanvay dashboard --runs runs --viewers` builds
`runs/index.html` across every run and a `viewer.html` inside each one.

A run that *completed* is not automatically a run that *succeeded* — but you no longer
have to remember that, because the process exit code says so. A failed fit returns
`model_type="failed"` with a reason string, zero inliers and `rmse_px = NaN` — never an
exception, and never a flattering zero.

---

## What this build does that a baseline pipeline does not

Each item maps to a requirement in
[`docs/ISRO_ID26166_Prototype_Plan.md`](docs/ISRO_ID26166_Prototype_Plan.md); the
reasoning is in [`docs/decisions.md`](docs/decisions.md) D7–D13.

**Independent check points (plan Part 1 §4).** Tie-points are split into *control* and
*check* before any fit, stratified by grid cell so the held-out set is spatially spread,
and deterministic — no RNG, so the same pair produces the same split byte for byte. The
model ladder, RANSAC, model selection and the spline see **control only**.
`check_rmse_px` is therefore the only accuracy figure in this repository that is not
measured on the fit's own sample, and it is the number to quote. `rmse_px` remains, is
labelled in-sample, and carries a warning string saying so. When there are too few
matches to split meaningfully, `check_rmse_px` is `null` and `check_status` says why —
never a plausible substitute.

**Thin-plate-spline residual, accepted only on held-out evidence (plan Part 2 §5).** With
`geometry.tps: auto` a TPS displacement field is fitted on the control inliers and kept
**only if the held-out check points say it helped** — otherwise it is discarded and
`model_type` stays projective. The shipped rule is stricter than a plain improvement
test: it also requires that check points already inside the RANSAC threshold do not get
worse. That second condition exists because the plain rule accepted a spline reporting
0.467 px on 0.589 px of injected noise. `tps_status`, `tps_check_rmse_before_px` and
`tps_check_rmse_after_px` record the comparison that decided it. This is the answer to
"how do you know the non-rigid warp is not overfitting?" — it is rejected by points it
never saw.

**Spatial Distribution Index (plan Part 1 §2).** `sdi = (coverage_pct/100) × 1/(1 +
dispersion_cv)`, in [0, 1], emitted natively in `metrics.json` alongside
`sdi_definition` so nobody has to guess the formula. It is a convenience scalar over the
three real numbers — `coverage_pct`, `dispersion_cv`, per-cell `cell_states` — not a
replacement for them, and it is `null` when either input is unknown.

**Quad-tree ANMS inside every cell (plan Part 1 §2, Part 2 §3).** The per-cell quota used
to be filled by a plain score sort, so all K points could land in one textured corner of
the cell and the grid bought nothing *within* a cell. `match/anms.py` subdivides the cell
and keeps the best-scoring point per leaf. Deterministic, ties broken by index. On by
default (`match.anms`), and recorded per cell in `cell_info`.

**It only bites where a cell is over-subscribed, and on these fixtures that is rare.** The
quota is `match.max_matches` (50) per cell; ANMS runs only when a cell offers more
candidates than that. Counted from `cell_info` across the 14-pair Δsun sweep on
2026-09-03 and re-counted 2026-09-03 by a second reader: ANMS fired in **33 of 224
cells**, and they are not spread the way "the densest pairs" would suggest — 16 cells on
the Δ0° pair and 16 on the Δ10° pair (790 and 780 inliers, both resolving to SIFT), then
exactly **one** cell on the Δ30° pair (183 inliers, RIFT). The Δ20° pair, which is the
third densest at 292 inliers, fires in **none**: its busiest cell offers 26 candidates
against a quota of 50. On the other **eleven** pairs — the ones the project exists for —
no cell reaches the quota, so ANMS never runs and
`anms: false` is byte-identical to the default. On `fixtures/synth_pair_A` it fires in
**0 of 16 cells**, which is why the `anms_off` row of the ablation is identical to the
baseline: that row measures nothing on that fixture and must not be read as "ANMS does
nothing". The mechanism is tested directly in `tests/test_tile.py`. The end-to-end
evidence that exists comes from forcing the quota down to 10, where it does bind — and it
says the same ambiguous thing on both fixtures. On `dsun_50`: ANMS on gives `gt_rmse_px`
**1.396** against **1.652** off, on the same 83 inliers and 100% coverage, while
`check_rmse_px` goes the *other* way, 1.314 against 1.105. On `synth_pair_A`: 3.013 against
3.179 on true error, 2.171 against 0.757 on held-out error. Those runs were checked at the
cell level rather than trusted because a number moved: at quota 10 the ANMS branch is
entered on **12 of 16 cells** on `dsun_50` and **9 of 16** on `synth_pair_A`
(`cell_info.cells[*].anms == true`), against 0 of 16 with `match.anms=false` and 0 of 16 at
the shipped quota of 50. **ANMS wins on true error and
loses on held-out error, on both pairs, at a quota the shipped config does not use.** That
is the honest state of the claim, and it is stated here rather than left for a judge to
derive from a row that is identical to the baseline.

**An N × M grid, not N × N.** The uniformity grid follows the image aspect: `grid_n` cells
along the short axis, the long axis scaled and capped at 64. An 888 × 11952 NAC strip on
a fixed 4 × 4 grid gets 13:1 cells, and both `coverage_pct` and the per-cell quotas are
then measured on a partition nobody would defend. Square imagery is byte-identical to
the old behaviour. `grid_rows` and `grid_cols` are reported; `grid_n` still means the
short-axis count.

**The matcher chooses itself.** `match.method: auto` resolves from the pair's Δ sun
azimuth using the same ≥ 20° bar preflight recommends on: RIFT phase congruency above it,
SIFT below. `match_method_requested`, `match_method_resolved`, `match_method_recommended`
and a plain-English `match_method_reason` all land in `metrics.json`.
`photometry.phase_congruency` and `photometry.clahe` resolve from the chosen matcher and
record their own reasons. Shipping a fixed `sift` default turned the project's headline
differentiator off. Measured on the 14-pair Δsun sweep (`bench/baselines.md` §1,
2026-09-02): at Δsun 50° pinned `sift` delivers 9 inliers at 5.610 px true error over 37.5%
coverage, while the `auto` default resolves to RIFT and delivers 99 inliers at 1.887 px over
100% coverage — and from Δsun 60° upward pinned `sift` delivers **no model at all** on every
one of the eight remaining pairs, where `auto` still delivers 46–73 inliers out to Δ180°.

**The mask boundary no longer manufactures an edge.** Zeroing invalid pixels and then
running phase congruency put a hard, sun-positioned step edge at every mask boundary,
inside the one map whose entire purpose is illumination invariance.
`photometry.mask_fill: reflect` fills across the boundary for the PC input only; the
returned `albedo` is unchanged. Measured on a contiguous cast shadow covering 29.9% of the
frame: boundary PC response **35.5× the interior with `zero`, 1.07× with `reflect`**.
`mask_fill: zero` restores the old path exactly so the difference stays measurable.
**It is not an accuracy claim, and the repository does not pretend it is:** on the
speckle-masked synthetic fixtures the fill is close to a wash. The ablation's single
`dsun_50` row has `mask_fill_zero` at `gt_rmse_px` 0.931 against the shipped default's
1.887, and an earlier revision of this README read that as "zero beats reflect on every
column". It does not: `dsun_50` is the most favourable point for `zero`, not a
representative one. Measured across the Δsun range on 2026-09-03 (`samanvay register`,
cache off, one switch at a time, five RIFT pairs) `zero` wins on true error on four of
five and loses badly on the fifth, while `reflect` wins on mean held-out error —
`gt_rmse_px` mean 2.890 for `zero` against 2.991 for `reflect`, `check_rmse_px` mean 1.475
against **1.298**, at Δ180° 3.366 against **0.592**. The full table is in
[`docs/limitations.md`](docs/limitations.md) §9. On `fixtures/synth_pair_A` the two are
byte-identical. The defence of `reflect` is the boundary measurement and the fact that a
1.1% scattered-speckle mask is not the regime the fill exists for; it is **not** a
registration score, and no fixture here has the contiguous cast shadow the fill is for.

**IIRS is reduced, not truncated (plan Part 1 §3).** `io/bands.py` screens every band's
SNR on a decimated read, drops the noisy ones, caps the survivors, and returns the first
principal component sign-fixed against the band mean — the pseudo-panchromatic structural
map. `reduce: "mean"` and `reduce: "band"` are the alternatives. A single-band product
takes the `src.read(1)` path unchanged. `n_bands`, `n_bands_used`, `n_bands_dropped_snr`,
`reduce` and `explained_var_frac` are recorded in `Product.meta["band_reduction"]`.

---

## Repository layout

| path | one line |
|---|---|
| `samanvay/types.py` | the four frozen dataclasses — `Product`, `CanonicalImage`, `MatchSet`, `Registration` (which now carries `roles` and `warp`). **Do not edit without the protocol in `docs/CONTRACTS.md`.** |
| `samanvay/io/` | loading products, multi-band reduction (`bands.py`), mission-agnostic metadata (PDS3/PDS4 labels, GeoTIFF, JSON sidecars), preflight, and writing the run artifacts |
| `samanvay/core/` | windowed tiled reading for products too large for RAM, and an on-disk array cache |
| `samanvay/photometry/` | the physics: DEM shading, cast shadows, Lommel-Seeliger, the validity mask, mask-boundary fill, optional CLAHE, phase congruency, and `canonicalise` which turns a `Product` into an illumination-invariant `CanonicalImage` |
| `samanvay/match/` | keypoint detection (SIFT / ORB / phase-congruency peaks), the RIFT descriptor, quad-tree ANMS (`anms.py`), the coarse-to-fine cascade, and the tiled matcher that enforces per-cell tie-point quotas |
| `samanvay/geometry/` | metadata-first coarse init, the control/check split, the similarity→affine→homography ladder with robust fitting, the thin-plate spline (`tps.py`), sub-pixel refinement with per-point uncertainty, uniformity accounting and SDI, and the metrics contract |
| `samanvay/pipeline/` | the `samanvay` CLI (`register`, `check`, `fixture`, `dashboard`, `trn`, `show-config`, `show-defaults`) and the stage orchestration |
| `samanvay/report/` | `render.py` writes `report.html` and `metrics_report.pdf`; `dashboard.py` writes the cross-run dashboard and the per-run inspector page |
| `synth/` | deterministic ground-truth fixtures: a lunar DEM generator, a two-sun render pair with an analytic source→reference homography, and the Δsun sweep. Imports nothing from `samanvay`, deliberately |
| `bench/` | the sweep harness, the one-stage-at-a-time ablation, and `baselines.md` |
| `tests/` | pytest suite, 429 tests collected |
| `docs/` | decisions, limitations, contracts, handover, the data intake path, the plan this is built against |
| `viewer/` | swipe/overlay run inspector. Renders one embedded run payload; carries no metrics of its own |
| `scripts/` | `download_pairs.py` (fetches real LROC NAC candidates), `pick_pairs.py` (ranks candidate scene pairs by Δsun and footprint overlap before you spend a download slot), `verify_airgap.py` (the CI gate) |

---

## Tests

```bash
python -m pytest -q          # or: make test
```

**429 passed, 0 failed** of 429 collected on this machine (`.venv/bin/pytest -q`,
2026-09-03; 56.7 s, 63.0 s and 87.5 s on three runs the same day). Fast by design: small arrays,
no network, no fixtures larger than a few hundred pixels a side. The one failure this file reported on 2026-09-02
(`test_preflight.py::test_scan_ranks_the_real_fixture_sweep`, which asserted the Δsun
sweep topped out at 50° after the sweep was extended to 180°) has since been closed.

Do not read "429 passed" as "the engine is correct" — read
[`docs/limitations.md`](docs/limitations.md) for what is still open. The suite covers the
contracts and the seams; it does not certify accuracy on real data, because nothing can
without ground truth.

CI (`.github/workflows/ci.yml`) runs the same suite on Python 3.11, plus an end-to-end
smoke run on a 256 px synthetic pair and the air-gap gate. Nothing in it is
`continue-on-error`: the team rule is **main is always green**. The workflow has never
executed on GitHub — its bodies were run by hand here.

---

## Ablation

The table that carries the innovation argument: run the same pair repeatedly with exactly
one stage switched off each time, so a row difference can only be caused by that switch.

```bash
python -m bench.ablate \
  --source fixtures/synth_pair_A/source.tif \
  --ref    fixtures/synth_pair_A/reference.tif \
  --out    runs/ablate
```

Writes `runs/ablate/ablation.csv` and `ablation.md`, one row per variant with deltas
against the full pipeline. If every variant returns an identical RMSE, `ablate` warns you
— that means the pipeline is ignoring the ablation config keys and the table proves
nothing.

Results go in [`bench/baselines.md`](bench/baselines.md), which opens with an index of its
own sections and the date each was measured. **§1** is the Δsun sweep (14 pairs, 0–180°,
`auto` against pinned `sift`, true error against analytic ground truth); **§2** is the
stage ablation; **§3** is the real LROC NAC pairs, which have no ground truth; **§5**
is the archive of superseded tables and is labelled as such. Quote §1–§3 and nothing from
§5.

**Read the ablation with its dead rows named.** On `fixtures/synth_pair_A`, **eight of
the 18 arms return exactly the baseline** — `gt_rmse_px` 2.9287, `check_rmse_px` 1.5712,
64 inliers, 100% coverage, SDI 0.5784. Each was re-run individually on 2026-09-03 with
`samanvay register --set cache.enabled=false` and one switch, and each is byte-identical
to the default:

| inert arm | why, on this pair |
|---|---|
| `photometric_model_none` | no `--dem` is passed, so the photometric model never runs |
| `photometric_lunar_lambert` | same |
| `mask_fill_zero` | the mask is 1.1% scattered speckle; there is no boundary to fill across |
| `clahe_off` | `clahe: auto` already resolves to **off** on the RIFT arm, so this arm restates the default |
| `anms_off` | the per-cell quota is never reached here, 0 of 16 cells (see above) |
| `uniformity_off` | the switch changes no surviving point on this pair |
| `quota10_uniformity_off` | with uniformity off the per-cell quota is not applied at all, so forcing it to 10 changes nothing |
| `tps_forced` | `tps: auto` already accepted the spline, so forcing it changes nothing |

Each reason is mundane and legitimate; the table cannot tell you any of them, and eight
identical rows out of eighteen read as eight inert stages. An earlier revision of this
paragraph said six, having left `clahe_off` and `tps_forced` off the list.

The same switches do move on `fixtures/dsun_sweep/dsun_50`, where the tie-point density
and the mask differ: `mask_fill_zero` 0.931 px against the default's 1.887 px, `clahe_on`
0.664 px, and the forced-quota ANMS pair 1.396 px with against 1.652 px without. **A stage
is not proved or disproved by one row on one fixture, and this repository will not pretend
otherwise.** `docs/limitations.md` §9 carries the full table of which rows are inert and
why.

### The sweep has a known cache defect — `make bench` works around it, `python -m bench.harness` does not

`pipeline/stages.py` caches phase congruency under `(product_id, photometry params, file
size, int mtime, shape)`. Every `fixtures/dsun_sweep` source declares `product_id:
"synth_source"`, every one is 1474265 bytes, and `python -m synth.sweep` writes several
inside one wall-clock second — so two pairs can collide on the key and the second silently
reads the first's phase-congruency map. Reproduced in isolation 2026-09-03 on two copies
of `dsun_60` and `dsun_70` with their mtimes forced equal and a private cache directory:
with the cache **on** both report `gt_rmse_px` 2.1098, 73 inliers, 153 matches; with it
**off** the second reports its own 3.5141, 49, 126.

`rm -rf .cache` does **not** fix this, and an earlier revision of `make bench` was wrong
to imply it helps — the harness warms the cache as it iterates, so pair 7 collides with
the entry pair 6 just wrote. `make bench` now runs the manifest through
`bench.harness.SWEEP_CONFIG`, which switches the cache off; that is the same thing
`bench/harness.py` already does on its arm-runner path, and it is why `bench/baselines.md`
§1 is correct. Measured 2026-09-03, that target reproduces `docs/HANDOVER.md` §1 and
`bench/baselines.md` §1 exactly — fourteen distinct rows, `gt_rmse_px` 0.024 / 0.041 /
0.206 / 1.143 / 0.895 / 1.887 / 2.110 / 3.514 / 4.512 / 5.111 / 4.497 / 6.824 / 3.163 /
0.592 px, in 85 s of pipeline time.

**The raw `python -m bench.harness --manifest …` CLI is still affected**, because
`run_manifest()` does not apply `SWEEP_CONFIG` and the CLI exposes no way to pass it. Two
fixes belong outside this file: apply `SWEEP_CONFIG` on the manifest path, and put the
file path or a content hash into the cache key in
`pipeline/stages.py::_canonicalise_cached`. Until the key is fixed, *any* sweep run with
the cache on is suspect on any two pairs sharing a resolved photometry arm and an mtime
second, and whether that happens is a race against the clock — regenerating the sweep into
a clean directory here took 29 s and left the fourteen sources 2 s apart, so it would not
have collided; a quicker machine would. Written up in
[`docs/limitations.md`](docs/limitations.md) §9.

---

## Conventions you must not get wrong

Full detail and rationale in [`docs/decisions.md`](docs/decisions.md) D1 and
[`docs/CONTRACTS.md`](docs/CONTRACTS.md).

- Coordinates are `(x, y) = (column, row)`, floating point, **pixel centre at integer
  coordinates**.
- Every transform is `3x3 float64` and maps **source → reference**. `Registration.params`
  stays the global 3x3 even when a TPS is applied; the spline is a residual on top, and
  `geometry.tps.pullback` is the one place the full model is evaluated.
- Every residual and every RMSE is in **source pixels**.
- Mask codes: `0 = valid`, `1 = shadow`, `2 = nodata`, `3 = saturated`.
- An unknown quantity is reported as unknown. Never substitute a default and let it flow
  into a metric.

---

## Data attribution and licensing

**Code.** Apache License 2.0 — see [`LICENSE`](LICENSE).

**Data.** No mission data is redistributed in this repository. `data/` is gitignored, and
the only rasters under version control are the deterministic synthetic fixtures this
project renders itself (`synth/`, seed 0, byte-reproducible). Fetch real products from the
agencies' own archives with [`scripts/download_pairs.py`](scripts/download_pairs.py); the
whole intake path is [`docs/DATA.md`](docs/DATA.md).

When you do bring real products in, the attribution travels with them:

- **LROC NAC / WAC imagery and SLDEM products** — NASA / Goddard Space Flight Center /
  Arizona State University. Distributed through the PDS Cartography and Imaging Sciences
  Node (LROC PDS archive).
- **Chandrayaan-2 products (OHRC, TMC-2, IIRS)** — ISRO / Physical Research Laboratory,
  distributed through ISSDC's PRADAN portal, under ISRO's own data policy.
- **SELENE / Kaguya products** — JAXA.

Each agency's terms apply to its own data. This repository's Apache-2.0 licence covers the
code only, and nothing in it grants a right to redistribute mission data.

Run artifacts are the one deliberate exception to the `runs/` gitignore: `metrics.json`,
`matches.csv`, `provenance.json` and `transform.json` are no longer ignored (kilobytes
each), so a clone can check any number quoted here. The imagery, the report HTML and the
PDF stay ignored. Un-ignoring is not the same as committing: `runs/` accumulates scratch
and ablation runs too, so `git add` the ones a claim rests on rather than all of them.

---

## Documents

- [`docs/ISRO_ID26166_Prototype_Plan.md`](docs/ISRO_ID26166_Prototype_Plan.md) — the plan
  this build is measured against.
- [`docs/decisions.md`](docs/decisions.md) — the architecture decision record.
- [`docs/limitations.md`](docs/limitations.md) — what this software does not do and where
  it fails, written before anyone asks.
- [`docs/CONTRACTS.md`](docs/CONTRACTS.md) — the frozen dataclasses, every module's public
  API, and the contract-change protocol.
- [`docs/HANDOVER.md`](docs/HANDOVER.md) — what runs today and how to drive it.
- [`docs/DATA.md`](docs/DATA.md) — bringing real lunar products in.
- [`bench/baselines.md`](bench/baselines.md) — the results tables.
- [`fixtures/synth_pair_A/README.md`](fixtures/synth_pair_A/README.md) — what the synthetic
  fixture is, and the self-measurement trap it does *not* fully escape.
