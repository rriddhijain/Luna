# Seat 6: Geometry & Validation Engineer

This folder contains everything you (seat 6) own, fully working and tested.
Every module runs standalone (no dependency on seats 1/2/3's real code) via
the fake-match / synthetic-image generators, so you can build and test all
of this before any real Chandrayaan-2 data or real matcher output exists.

## 1. Setup (do this once)

```bash
# from the repo root
python3 -m venv venv
source venv/bin/activate        # on Windows: venv\Scripts\activate
pip install -r requirements.txt
```

If `opencv-contrib-python` fails to install on Windows without WSL, use
WSL2 + Ubuntu, or Docker — GDAL/OpenCV contrib builds are far less painful
on Linux. `cv2.USAC_MAGSAC` (MAGSAC++) is ONLY available in the `contrib`
build, not plain `opencv-python` — double check with:

```bash
python3 -c "import cv2; print(hasattr(cv2, 'USAC_MAGSAC'))"
```

This must print `True`.

## 2. File map (what you own)

```
luna-geometry/
  types.py                    <- shared dataclasses (co-owned, frozen Day 0)
  geometry/
    init.py                   <- P2: metadata-first coarse initialisation
    verify.py                 <- P2: MAGSAC++ model ladder (CORE MODULE)
    refine.py                 <- P4: sub-pixel refinement
    uniformity.py             <- P5: grid coverage + dispersion metrics
    metrics.py                <- the "numbers seat" — metrics.json + ablation
  bench/
    fake_matches.py           <- synthetic MatchSet generator (never-blocked stub)
    ablate.py                 <- ablation table runner
  tests/
    test_geometry.py          <- full pytest suite (21 tests, all passing)
```

## 3. How to run things

Run any single module directly to see its built-in smoke test / demo:

```bash
python3 -m bench.fake_matches
python3 -m geometry.init
python3 -m geometry.verify
python3 -m geometry.refine
python3 -m geometry.uniformity
python3 -m geometry.metrics
python3 -m bench.ablate
```

Run the full test suite (do this after every change — this is your
personal CI):

```bash
pytest tests/test_geometry.py -v
```

Expected: `21 passed`.

## 4. What each module actually does, in one line

- **`geometry/init.py`** — composes source and reference GDAL geotransforms
  into an initial source→reference transform, so fine matching only has to
  search a small residual window instead of the whole image.
- **`geometry/verify.py`** — the core module. Fits similarity, affine, and
  homography models with MAGSAC++ (`cv2.USAC_MAGSAC`), and picks the
  simplest model whose RMSE is within 10% of the best model. This is your
  most important deliverable — everyone downstream depends on its output
  (`Registration`).
- **`geometry/refine.py`** — for every inlier, refines its position to
  sub-pixel accuracy using phase correlation with DFT upsampling
  (`skimage.registration.phase_cross_correlation`, `upsample_factor=100`).
  Rejects low-quality correlation peaks rather than trusting them.
- **`geometry/uniformity.py`** — assigns matches to a grid, computes
  coverage % and a dispersion index (coefficient of variation), and
  correctly excludes masked (shadow/nodata) cells from the score so you
  don't get penalised for terrain that had nothing to match.
- **`geometry/metrics.py`** — assembles the final `metrics.json` content
  and builds ablation comparison tables. Nothing goes on a slide unless it
  came from this module.
- **`bench/fake_matches.py`** — generates synthetic correspondences from a
  known homography with configurable noise and outlier fraction. This is
  what makes you never-blocked on seat 1.
- **`bench/ablate.py`** — runs the canonical ablation experiments
  (sub-pixel on/off, uniformity on/off) end to end on synthetic data.

## 5. Real numbers you already have (from the smoke tests above)

- MAGSAC++ verification recovers RMSE within ~0.15px of injected noise,
  even with 25-30% outliers.
- Sub-pixel refinement improves PER-POINT accuracy from ~1.74px to
  ~0.40px against exact ground truth on a synthetic test (noisy detector
  input, phase-correlation refined).
- Uniformity correctly distinguishes clustered matches (6% coverage) from
  spread matches (94% coverage), and correctly excludes masked/shadow
  cells from the denominator.

**Important honest caveat for your Q&A prep (Q1 in the roadmap):** with a
large, well-distributed inlier set (100+ points), the globally-fit affine
model already averages out per-point noise and can show equal or lower
*aggregate* RMSE than individually sub-pixel-refined points, because
per-point refinement carries its own patch-correlation noise floor.
Sub-pixel refinement's real value is *per-point* precision — this matters
most with sparse tie-points (e.g. the TRN position fix, or
control-network tie-points), not aggregate affine RMSE with hundreds of
points. Report both numbers. If a judge asks "why doesn't refinement
improve your headline RMSE," this is the honest, correct answer — don't
hide it.

## 6. When real data / real MatchSets arrive (from seat 1 / seat 3)

Your functions are already shaped to accept the real objects:

```python
from types import MatchSet, Product
from geometry.init import coarse_init
from geometry.verify import verify
from geometry.refine import refine_registration
from geometry.uniformity import compute_uniformity
from geometry.metrics import build_metrics

# 1. coarse init from real geotransforms (seat 3)
init_M = coarse_init(src_product.meta["geotransform"], ref_product.meta["geotransform"])

# 2. verify a real MatchSet (seat 1)
reg = verify(real_matches, init_params=init_M)

# 3. sub-pixel refine using real images (seat 2's canonicalised albedo, or
#    seat 3's raw product arrays)
reg = refine_registration(reg, real_matches, src_image, ref_image)

# 4. uniformity, using seat 2's validity mask
uni = compute_uniformity(
    real_matches.src_xy, reg.inliers, image_shape=src_image.shape,
    grid_n=8, validity_mask=canonical_image.mask,
)

# 5. final metrics.json content
metrics = build_metrics(reg, uni)
```

No function signatures need to change — this is why the types.py contract
matters so much. If seat 1's real MatchSet or seat 3's real Product don't
match the frozen dataclass shape, flag it immediately rather than silently
adapting around it.
