# SAMANVAY

**Lunar image registration engine — SIH26166.**

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

## Status — 2026-08-29

Honest, because a README that oversells is found out in the first demo.

| | |
|---|---|
| Photometry, matching, geometry, I/O, metrics, synthetic fixtures | implemented, unit-tested |
| End-to-end CLI (`samanvay register`) | **currently failing** — `samanvay/pipeline/stages.py` does not unpack the `(MatchSet, cell_info)` tuple that `match_tiled` now returns, does not pass the coarse init into matching or verification, and does not call `compute_metrics`. CI is red until it is fixed. |
| Measured accuracy | **none.** `bench/baselines.md` is "not measured" everywhere. No real Chandrayaan-2/LRO pair has been processed. |
| `viewer/` | static mockup with **hardcoded placeholder metrics**. Not wired to any run. Do not screenshot it as a result. |
| Docker (`Dockerfile`, `docker-compose.yml`) | present, not verified against the current code |

---

## Bringing real data in

Have real lunar products? **[docs/DATA.md](docs/DATA.md)** is the whole path: where files
go, what metadata matters, and how to tell whether a real result is trustworthy. Start by
preflighting the pair — it exits non-zero if the data cannot work, and prints the exact
register command if it can:

```bash
samanvay check --source data/real/src.tif --ref data/real/ref.tif --dem data/real/dem.tif
```

## Quickstart

Python 3.11. Every command below is run from the repository root.

```bash
# 1. environment
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
pip install pytest scikit-image matplotlib   # used by the code, not yet in pyproject deps

# 2. generate the synthetic fixture (deterministic: seed 0 reproduces every byte)
python -m synth.render_pair
#   -> fixtures/synth_pair_A/{source.tif,reference.tif,dem.tif,*.tif.json,gt.json,README.md}
#   --size N and --seed N give a smaller or different pair

# 3. register the pair
samanvay register \
  --source fixtures/synth_pair_A/source.tif \
  --ref    fixtures/synth_pair_A/reference.tif \
  --out    runs/demo_01

# 4. read the report
open runs/demo_01/report.html        # macOS; xdg-open on Linux
```

Step 3 fails today — see **Status** above. Steps 1, 2 and 4 work.

The fixture is deliberately not easy: a 2x scale ratio, 10 degrees of rotation, a 270-degree
sun-azimuth difference, and a source geotransform that is wrong by ~49 source pixels. A 1:1
upright fixture lets fragile code look healthy until it meets real data.
`fixtures/synth_pair_A/README.md` explains what is in it and how not to over-read it.

---

## What a run produces

Six files in `--out`, written by `samanvay/io/writers.py`:

| file | what it is |
|---|---|
| `registered.tif` | the source warped onto the reference grid — reference CRS and geotransform, source dtype |
| `matches.csv` | one row per tie-point: `id, src_x, src_y, ref_x, ref_y, score, inlier, residual_px, sigma_px, cell`. Residuals in **source** pixels; unknown fields are blank, never `0` |
| `transform.json` | `model_type`, the 3x3 `params`, the `init_params` it started from, the model-selection margin, the rejected rungs |
| `metrics.json` | `rmse_px`, `inlier_count`, `inlier_ratio`, `coverage_pct`, `dispersion_cv`, per-cell counts and states, ground-truth error when a truth transform is supplied. An unknown quantity is `null`, never a plausible default |
| `provenance.json` | UTC timestamp, git SHA / branch / dirty flag, seed, the full config, package versions, input paths and product ids |
| `report.html` | the visual report (falls back to a minimal built-in page if the report module is unavailable) |

A run that *completed* is not automatically a run that *succeeded*: check
`metrics["status"]`. A failed fit returns `model_type="failed"` with a reason string, zero
inliers and `rmse_px = NaN` — never an exception, and never a flattering zero.

---

## Repository layout

| path | one line |
|---|---|
| `samanvay/types.py` | the four frozen dataclasses — `Product`, `CanonicalImage`, `MatchSet`, `Registration`. **Do not edit without the protocol in `docs/CONTRACTS.md`.** |
| `samanvay/io/` | loading products, mission-agnostic metadata (PDS3/PDS4 labels, GeoTIFF, JSON sidecars), and writing the six artifacts |
| `samanvay/core/` | windowed tiled reading for products too large for RAM, and an on-disk array cache |
| `samanvay/photometry/` | the physics: DEM shading, cast shadows, Lommel-Seeliger, the validity mask, phase congruency, and `canonicalise` which turns a `Product` into an illumination-invariant `CanonicalImage` |
| `samanvay/match/` | keypoint detection (SIFT / ORB / phase-congruency peaks), description, and the tiled matcher that enforces per-cell tie-point quotas |
| `samanvay/geometry/` | metadata-first coarse init, the similarity→affine→homography model ladder with robust fitting, sub-pixel refinement with per-point uncertainty, uniformity accounting, and the metrics contract |
| `samanvay/pipeline/` | the `samanvay` CLI and the stage orchestration |
| `samanvay/report/` | the HTML report (*planned* — `write_outputs` degrades to a minimal built-in page until it lands) |
| `synth/` | deterministic ground-truth fixtures: a lunar DEM generator and a two-sun render pair with an analytic source→reference homography. Imports nothing from `samanvay`, deliberately |
| `bench/` | the sweep harness, the one-stage-at-a-time ablation, and `baselines.md` |
| `tests/` | pytest suite |
| `docs/` | decisions, limitations, contracts |
| `viewer/` | swipe/overlay visualiser — **static mockup today**, not wired to a run |

---

## Tests

```bash
python -m pytest -q
```

Fast by design (small arrays, a few seconds). Three tests currently fail, all on the same
`match_tiled` tuple seam described in **Status**; they are left failing rather than
weakened, because a green suite that hides a broken integration is worse than a red one.

CI (`.github/workflows/ci.yml`) runs the same suite on Python 3.11 plus an end-to-end smoke
test on a small synthetic pair, with a five-minute budget. Nothing in it is
`continue-on-error`: the team rule is **main is always green**.

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
against the full pipeline. Variants: canonicaliser on/off (the headline pair), phase
congruency off, photometric model off, sub-pixel off, uniformity quotas off, and the
phase-congruency matcher. If every variant returns an identical RMSE, `ablate` warns you —
that means the pipeline is ignoring the ablation config keys and the table proves nothing.

Results go in [`bench/baselines.md`](bench/baselines.md), whose row and column structure is
frozen. Every cell there currently reads "not measured", which is a fact rather than a
placeholder; do not fill one with a plausible number.

A sweep over a manifest of pairs (`python -m bench.harness --manifest ... --out ...`) is
implemented, but no manifest is committed yet — *planned*.

---

## Conventions you must not get wrong

Full detail and rationale in [`docs/decisions.md`](docs/decisions.md) D1 and
[`docs/CONTRACTS.md`](docs/CONTRACTS.md).

- Coordinates are `(x, y) = (column, row)`, floating point, **pixel centre at integer
  coordinates**.
- Every transform is `3x3 float64` and maps **source → reference**.
- Every residual and every RMSE is in **source pixels**.
- Mask codes: `0 = valid`, `1 = shadow`, `2 = nodata`, `3 = saturated`.
- An unknown quantity is reported as unknown. Never substitute a default and let it flow
  into a metric.

---

## Documents

- [`docs/decisions.md`](docs/decisions.md) — the architecture decision record. Six
  decisions, all **PROPOSED**, awaiting team ratification. D2 (DEM resolution) and D5
  (which IIRS band is "the image") are the two that most need a joint call.
- [`docs/limitations.md`](docs/limitations.md) — what this software does not do and where
  it fails, written before anyone asks.
- [`docs/CONTRACTS.md`](docs/CONTRACTS.md) — the frozen dataclasses, every module's public
  API, and the contract-change protocol.
- [`bench/baselines.md`](bench/baselines.md) — the results table.
- [`fixtures/synth_pair_A/README.md`](fixtures/synth_pair_A/README.md) — what the synthetic
  fixture is, and the self-measurement trap it does *not* fully escape.
