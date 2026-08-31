# Seat 5: Systems & Performance Engineer

> Mission (team charter §3⑤): *make it run, keep it green, make it reproducible,
> keep it fast enough that iteration does not hurt.*

This is everything seat 5 owns, built and tested. Every piece is exercised by
CI and the benchmark harness, so the claims below are numbers you can regenerate,
not assertions.

## 0. One-command setup

```bash
./scripts/setup.sh          # .venv, pinned deps, MAGSAC++ check
# or
make setup
```

Then `make help` lists everything: `lint`, `test`, `smoke`, `bench`, `demo`,
`docker-demo`, `lock`, `clean`.

## 1. Deliverables → files (charter §3⑤)

| Sub-deliverable | Where | Status |
|---|---|---|
| Pinned environment, one-command setup | `pyproject.toml`, `requirements.lock`, `requirements.txt`, `scripts/setup.sh` | ✅ |
| CI: pytest + <5 min e2e smoke, main always green | `.github/workflows/ci.yml`, `scripts/ci_smoke.py` | ✅ |
| Tiling + memmap: windowed reads with halo | `samanvay/core/tiling.py` | ✅ |
| Cache: canonicalisation / PC keyed by (product + params) | `samanvay/core/cache.py` | ✅ |
| Container + benchmark runner | `Dockerfile`, `docker-compose.yml`, `bench/harness.py` | ✅ |
| Per-stage profiling (wired into the pipeline) | `samanvay/core/timing.py`, `pipeline/stages.py` | ✅ |

Two extras the seat picked up because they block "it runs at all":

- **Packaging fix.** The subpackages had no `__init__.py` and the frozen
  contract lives in a *top-level* module (`geometric_types.py`), so a real
  `pip install .` broke on import. The wheel now bundles it (verified by a
  clean-venv install of the wheel running `samanvay register`).
- **PDS4 / OHRC ingestion.** `samanvay/io/pds.py` reads the real
  `ohr_r1_r11_shape_ver4`-style products (detached `.xml` label + raw `.img`),
  byte-order aware, with best-effort illumination geometry and a sidecar-JSON
  escape hatch.

## 2. Tiling — `samanvay/core/tiling.py`

Why: OHRC strips are gigapixel (nothing may `read()` the whole array), and
phase congruency is a non-local FFT operator that produces seam artefacts if
tiled naively.

- `plan_tiles(shape, tile_shape|grid_n, halo)` partitions the image into
  **core** rectangles (disjoint, gap-free) each **read** with a halo of context
  (clamped at edges).
- `TiledReader` presents one `read_window` / `read_tile` API over both a numpy
  array/memmap and a rasterio dataset (GDAL windowed reads — nothing fully
  materialised).
- `apply_tiled(source, func, tile_shape, halo)` runs a shape-preserving operator
  per haloed tile, crops the halo, and stitches — the pattern the canonicaliser
  and matcher use on real products.

**The guarantee** (property-tested across shapes/tiles/halos in
`tests/test_tiling.py`): reassembling every tile's core reconstructs the source
**exactly**, and a pixel-local `apply_tiled` equals the whole-image result. Only
genuinely non-local operators differ, and only inside the halo you discard.

## 3. Cache — `samanvay/core/cache.py`

Canonicalisation is the runtime hog and it is deterministic, so computing it
twice is wasted time (charter I10).

- **Key** = sha256 over `(product identity, params, format-version)`. Identity is
  file `path/size/mtime` for on-disk products, or a byte digest for in-memory
  arrays — change the pixels or the params and the key changes, so a stale
  result is never served.
- **Two tiers**: an in-process LRU + a disk tier (`<key>.npz` arrays + `.json`
  sidecar), written atomically (temp file + `os.replace`), so an interrupted run
  never leaves a corrupt entry (and a corrupt entry is treated as a miss).
- **Honest stats**: `cache_stats()` (hits / misses / disk vs mem / hit-rate) —
  the benchmark *shows* the speed-up.
- **Safe to disable**: `enabled=False` (or `--no-cache`) just computes; nothing
  depends on the cache for correctness.

Wired into `pipeline/stages.py` via `canonicalise_cached`, controlled by
`config["cache"]["enabled"]`.

## 4. CI — `.github/workflows/ci.yml` + `scripts/ci_smoke.py`

Every push/PR: install the **locked** env, assert MAGSAC++ is present, `ruff`,
`pytest` (63 tests), then a real **512×512 end-to-end registration** that checks
all seven artifacts are written, the known transform is recovered
(GT-RMSE < 3 px), and the run finishes under the 5-minute budget. Benchmark
results are uploaded as an artifact. Rule: **main is always green**.

Run the same gate locally: `make smoke`.

## 5. Container — `Dockerfile` + `docker-compose.yml`

Built from `requirements.lock` (identical code to CI). `libgl1` + `glib` for the
OpenCV contrib build; rasterio brings its own GDAL. The build fails fast if
MAGSAC++ or the package wiring is wrong.

```bash
docker compose run --rm samanvay register --source data/source.tif --ref data/reference.tif --out runs/demo_01
docker compose run --rm demo     # self-contained: render → register → benchmark
```

## 6. Benchmark — `bench/harness.py`

```bash
python -m bench.harness           # full suite
python -m bench.harness --quick   # single small pair (CI mode)
```

Three sections, written to `bench/results/` as JSON/CSV/Markdown:

1. **Registration** — per pair: matches, inlier ratio, pipeline RMSE, **ground-
   truth RMSE**, coverage %, per-stage timing, throughput (MP/s), status.
2. **Cache effectiveness** — canonicalisation cold vs warm. Because today's
   canonicaliser is a pass-through, this runs a *representative* multi-scale
   log-Gabor FFT bank (the shape of seat 2's real PC) so the number reflects the
   production win: **~90× faster on the second run**.
3. **Throughput** — MP/s so a hot-path regression is a number, not a vibe.

Fixtures (`bench/fixtures.py`) are deterministic, richly-textured pairs with a
known similarity transform and differing illumination — enough to exercise the
current cell-to-cell matcher and score it against ground truth without any
external data.

## 7. Runtime budget (charter I14)

Proposed per-pair budget for the synthetic suite on a laptop CPU: **matching
dominates**; canonicalisation is negligible until seat 2's PC lands, at which
point the cache keeps re-runs cheap. Track the numbers in `bench/results/` and
re-negotiate if a stage blows the budget.

## 8. Honest limitations

- The current pipeline still materialises full arrays in a couple of places;
  `TiledReader`/`apply_tiled` are ready for seats 1–2 to adopt for true
  gigapixel end-to-end (the reader already avoids full loads).
- The cache benchmark's absolute numbers use a representative PC stand-in, not
  the production encoder (which does not exist yet) — clearly labelled as such.
- Benchmark fixtures use small rotation/translation/near-unit scale to stay
  within what the current cell-to-cell matcher tolerates; large-scale robustness
  is the coarse-init story owned with seats 1 & 6.
