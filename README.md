# SAMANVAY — illumination-robust lunar image registration

Register a lunar image pair taken under **different sun angles** into a common
frame, and produce the evidence that proves it worked: a georeferenced product,
sub-pixel tie-points, uniformity metrics, an ablation of the physics stage, and
a deep-zoom viewer.

This is the SIH 2026 prototype (project **SAMANVAY**, repo *Luna*). One command
is the whole promise:

```bash
samanvay register \
  --source data/source.tif \
  --ref    data/reference.tif \
  --out    runs/demo_01
```

which writes, in `runs/demo_01/`:

| Artifact | Contents |
|---|---|
| `registered.tif` | source warped into the reference frame, georeferenced |
| `matches.csv` | every tie-point: id, src/ref x·y, score, inlier, residual, sigma, grid cell |
| `transform.json` | model type, parameters, coarse init |
| `metrics.json` | RMSE (px), inliers, coverage %, dispersion CV, runtime |
| `report.html` | side-by-side / match overlay / metrics |
| `provenance.json` | inputs, config, package versions, seed |
| `timings.json` | per-stage wall-clock + cache stats *(seat 5)* |

## Quickstart

```bash
./scripts/setup.sh                 # .venv + pinned deps + MAGSAC++ check  (one command)
. .venv/bin/activate

python -m synth.render_pair        # make a synthetic pair in data/
samanvay register --source data/source.tif --ref data/reference.tif --out runs/demo_01

pytest                             # 63 tests
python -m bench.harness            # benchmark table (accuracy + timing + cache)
```

On a machine with Docker and nothing else:

```bash
docker compose run --rm demo       # render → register → benchmark, self-contained
```

### Real Chandrayaan-2 data

The loader ingests OHRC / TMC-2 **PDS4** products directly — point it at the
product directory, the `.xml` label, or the raw `.img`:

```bash
samanvay register --source /path/to/ohr_r1_r11_shape_ver4 \
                  --ref    /path/to/reference_strip.xml \
                  --out    runs/ohrc_01
```

It resolves the data file (ignoring browse thumbnails), decodes the raw array
(byte-order aware), and harvests illumination geometry from the label. Anything
the label lacks can be supplied via a sidecar `<path>.json`, which always wins.

## Repository layout

```
samanvay/
  core/          seat 5 — systems & performance
    tiling.py      windowed / halo reads over gigapixel rasters (exact reassembly)
    cache.py       content-addressed canonicalisation / PC cache (product + params)
    timing.py      per-stage StageTimer
  io/            seat 3 — loaders (GeoTIFF + PDS4/OHRC + sidecar), writers
  photometry/    seat 2 — canonicalise (albedo / phase congruency / mask)
  match/         seat 1 — classical matcher, tiled matching with cell budgets
  geometry/      seat 6 — MAGSAC++ verify (pipeline path)
  pipeline/      seat 3 — run.py (CLI), stages.py (orchestration)
  types.py       frozen contracts (re-exports geometric_types)
geometry/        seat 6 — full model ladder, sub-pixel refine, uniformity, metrics
bench/           seat 5 — harness.py, fixtures.py  (+ seat 6 fake_matches, ablate)
synth/           seat 2 — synthetic pair renderer
scripts/         seat 5 — setup.sh, ci_smoke.py
.github/workflows/ci.yml   seat 5 — lint + tests + 512² e2e (main stays green)
Dockerfile, docker-compose.yml, requirements.lock, Makefile   seat 5
```

## The systems & performance layer (seat 5)

See [`README_SEAT5.md`](README_SEAT5.md) for the full write-up. In short:

- **Reproducible env.** `pyproject.toml` + fully pinned `requirements.lock`; one
  command (`scripts/setup.sh`) or one container. `pip install .` is verified to
  work on a clean machine (top-level `geometric_types` is bundled).
- **Tiling.** `core/tiling.py` reads gigapixel rasters window-by-window with a
  halo so FFT-based phase congruency has context and no seam artefacts —
  guaranteed to reassemble the image exactly (property-tested).
- **Cache.** `core/cache.py` memoises the deterministic-but-expensive
  canonicalisation, keyed by `(product identity, params)`. The benchmark shows a
  ~**90×** speed-up on the second run under a representative phase-congruency load.
- **CI.** `.github/workflows/ci.yml` lints, runs the suite, and registers a
  512×512 fixture end-to-end (well under the 5-minute budget) so *main is always
  green*.
- **Benchmark.** `bench/harness.py` runs a set of pairs and emits a metrics
  table (accuracy vs ground truth, per-stage timing, throughput, cache).

## Requirements

Python ≥ 3.10. The **`opencv-contrib`** build is mandatory (MAGSAC++ /
`cv2.USAC_MAGSAC` lives only there); the setup script and CI both assert it.

## License

MIT.
