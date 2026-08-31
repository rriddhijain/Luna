"""
samanvay/pipeline/stages.py

The end-to-end registration pipeline: load -> canonicalise -> match -> verify
-> warp -> write. Owned by seat 3 (pipeline plumbing); the seat 5 additions
here are non-invasive and opt-out:

  * every stage is wrapped in a StageTimer so a run can report where its
    seconds went (written to ``timings.json`` and returned to callers such as
    the benchmark harness);
  * canonicalisation goes through the content-addressed cache (I10) so the
    second run of a pair -- and the reference image shared across a batch --
    is not recomputed. Set ``config["cache"]["enabled"] = False`` to bypass.

Behaviour with an empty/legacy config is unchanged: the cache defaults on but
is transparent, and the extra ``timings.json`` artifact is additive.
"""

from __future__ import annotations

import json
import os

import cv2
import numpy as np

from samanvay.core.cache import CanonicalCache, cache_stats, canonicalise_cached
from samanvay.core.timing import StageTimer
from samanvay.geometry.verify import verify_matches
from samanvay.io.loaders import load_product
from samanvay.io.writers import write_outputs
from samanvay.match.tile import match_tiled
from samanvay.photometry.normalize import canonicalise

# dtypes OpenCV can warp directly, in native byte order.
_CV_SAFE_DTYPES = {np.uint8, np.uint16, np.int16, np.float32, np.float64}


def _as_cv_image(arr: np.ndarray) -> np.ndarray:
    """Coerce an array into something ``cv2.warpPerspective`` accepts.

    Real PDS4 products arrive as (possibly big-endian) memmaps; OpenCV needs a
    contiguous, native-byte-order array of a supported dtype. Native-order
    supported arrays pass through untouched (so the synthetic uint8 path is
    unchanged); anything else is promoted to native float32.
    """
    arr = np.asarray(arr)
    native = arr.dtype.byteorder in ("=", "|") or (
        arr.dtype.byteorder == "<" and np.little_endian
    ) or (arr.dtype.byteorder == ">" and not np.little_endian)
    if arr.dtype.type in _CV_SAFE_DTYPES and native:
        return np.ascontiguousarray(arr)
    return np.ascontiguousarray(arr.astype(np.float32))


def _build_cache(config: dict) -> CanonicalCache:
    cache_cfg = config.get("cache") or {}
    enabled = cache_cfg.get("enabled", True)
    root = cache_cfg.get("dir")
    return CanonicalCache(root=root, enabled=enabled)


def run_pipeline(
    source_path: str,
    ref_path: str,
    out_dir: str,
    config: dict | None = None,
) -> dict:
    """Run the full registration pipeline and write artifacts to ``out_dir``.

    Returns a dict of per-stage wall-clock timings (seconds), also written to
    ``<out_dir>/timings.json``.
    """
    if config is None:
        config = {}

    timer = StageTimer()
    cache = _build_cache(config)
    photometry_params = config.get("photometry")

    # 1. Load source and reference
    with timer("load"):
        source = load_product(source_path)
        reference = load_product(ref_path)

    # 2. Canonicalisation (physics stage) -- cached, keyed by (product, params)
    with timer("canonicalise"):
        source_canon = canonicalise_cached(source, photometry_params, cache=cache)
        ref_canon = canonicalise_cached(reference, photometry_params, cache=cache)

    # 3. Matching (tiled, with halo)
    with timer("match"):
        matches = match_tiled(
            source_canon,
            ref_canon,
            grid_n=config.get("grid_n", 4),
            halo_px=config.get("halo_px", 64),
            config=config.get("match"),
        )

    # 4. Geometry verification
    with timer("verify"):
        registration = verify_matches(matches, config.get("geometry"))

    # 5. Warp source image into the reference frame (source -> reference)
    with timer("warp"):
        ref_h, ref_w = reference.array.shape[:2]
        registered_array = cv2.warpPerspective(
            _as_cv_image(source.array),
            registration.params,
            (ref_w, ref_h),
            flags=cv2.INTER_LINEAR,
        )

    # 6. Write outputs
    with timer("write"):
        write_outputs(
            out_dir, source, reference, registration, matches, registered_array, config
        )

    timings = timer.as_dict()
    timings["total_s"] = timer.total
    _write_timings(out_dir, timings, cache)
    return timings


def _write_timings(out_dir: str, timings: dict, cache: CanonicalCache) -> None:
    os.makedirs(out_dir, exist_ok=True)
    payload = {
        "stages_s": timings,
        "cache_enabled": cache.enabled,
        "cache_stats": cache_stats(),
    }
    with open(os.path.join(out_dir, "timings.json"), "w") as f:
        json.dump(payload, f, indent=2)


# Kept importable for callers that want to canonicalise without the cache
# (e.g. a strict "no cache" ablation run).
__all__ = ["canonicalise", "run_pipeline"]
