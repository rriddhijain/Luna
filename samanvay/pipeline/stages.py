"""Seat 3 · pipeline/stages — the ordered run, with every stage switchable from config.

    load -> coarse init (P2) -> canonicalise (P1) -> tiled match (P5 quotas)
         -> verify ladder (P2) -> sub-pixel refine (P4) -> re-verify
         -> metrics -> warp -> write

The switches are real plumbing rather than an `if` bolted on the night before the
demo, because the ablation table IS the evidence that the physics stage does the
work. `photometry.canonicalise=false` is the toggle we flip live on stage.

Timing per stage lands in metrics.json["stage_s"], which is where bench/harness
reads it from — it cannot measure stages from outside the pipeline.
"""

import json
import time

import cv2
import numpy as np

from samanvay.core.cache import cache_key, cache_load, cache_store
from samanvay.core.tiling import open_reader
from samanvay.geometry.init import coarse_init_info
from samanvay.geometry.metrics import compute_metrics
from samanvay.geometry.lsm import lsm_refine
from samanvay.geometry.refine import refine_matches
from samanvay.geometry.verify import verify_matches
from samanvay.io.loaders import load_product
from samanvay.io.writers import _json_safe, write_outputs
from samanvay.match.cascade import match_cascade
from samanvay.match.tile import match_tiled
from samanvay.photometry.mask import build_mask
from samanvay.photometry.normalize import canonicalise
from samanvay.pipeline.config import cell_budgets, find_ground_truth, load_config
from samanvay.types import CanonicalImage

_INTERP = {"nearest": cv2.INTER_NEAREST, "linear": cv2.INTER_LINEAR,
           "cubic": cv2.INTER_CUBIC, "lanczos": cv2.INTER_LANCZOS4}


class _Timer:
    """Accumulate per-stage wall clock into a dict for metrics.json["stage_s"]."""

    def __init__(self):
        self.stages = {}

    def __call__(self, name):
        self._name = name
        return self

    def __enter__(self):
        self._t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.stages[self._name] = round(time.perf_counter() - self._t0, 3)
        return False


def _as_array(obj):
    """Materialise a Product.array that may be a TiledReader rather than an ndarray."""
    if isinstance(obj, np.ndarray):
        return obj
    return open_reader(obj).read_all()


def _passthrough_canonical(product) -> CanonicalImage:
    """The canonicaliser-OFF arm of the ablation: raw band, no illumination model, no PC.

    Deliberately not a call to canonicalise() with the physics disabled — this is
    the honest "what a team without the physics stage would have" baseline, and
    pc stays zero so the matcher falls back to intensity exactly as it would.
    """
    img = _as_array(product.array).astype(np.float32)
    finite = np.isfinite(img)
    if finite.any():
        lo, hi = np.percentile(img[finite], [2.0, 98.0])
        img = np.clip((img - lo) / (hi - lo), 0.0, 1.0) if hi > lo else np.zeros_like(img)
    else:
        img = np.zeros_like(img)
    img = np.nan_to_num(img).astype(np.float32)
    mask = build_mask(img, nodata_value=(product.meta or {}).get("nodata"))
    zeros = np.zeros_like(img, dtype=np.float32)
    return CanonicalImage(albedo=img, pc=zeros, pc_orient=zeros.copy(), mask=mask,
                          params={"illum_mode": "none", "pc_status": "disabled",
                                  "canonicalise": False})


def _canonicalise_cached(product, cfg) -> CanonicalImage:
    """Canonicalise, reusing a cached result keyed by the file identity plus the params.

    Phase congruency is the runtime hog and is fully deterministic, so computing it
    twice for the same product and parameters is the cheapest speed win available.
    The key carries file size and mtime, so editing an image invalidates it.
    """
    photometry = cfg.get("photometry") or {}
    cache_cfg = cfg.get("cache") or {}
    if not cache_cfg.get("enabled", True):
        return canonicalise(product, photometry)

    meta = product.meta or {}
    try:
        stat = __import__("os").stat(product.path)
        identity = {"params": photometry, "size": stat.st_size,
                    "mtime": int(stat.st_mtime), "shape": list(meta.get("shape") or ())}
        key = cache_key(str(meta.get("product_id") or product.path), identity)
    except (OSError, TypeError, ValueError):
        return canonicalise(product, photometry)  # unkeyable input is a miss, not a crash

    cache_dir = cache_cfg.get("dir")
    hit = cache_load(key, cache_dir)
    if hit and {"albedo", "pc", "pc_orient", "mask", "params"} <= set(hit):
        try:
            params = json.loads(str(hit["params"].item()))
            params["cache"] = "hit"
            return CanonicalImage(albedo=hit["albedo"], pc=hit["pc"],
                                  pc_orient=hit["pc_orient"], mask=hit["mask"],
                                  params=params)
        except (ValueError, AttributeError):
            pass  # corrupt payload is a miss

    out = canonicalise(product, photometry)
    try:
        cache_store(key, {"albedo": out.albedo, "pc": out.pc, "pc_orient": out.pc_orient,
                          "mask": out.mask,
                          "params": np.array(json.dumps(out.params, default=str))},
                    cache_dir)
    except Exception:
        pass  # a cache we cannot write is not a reason to fail a registration
    return out


def _warp(source, reference, H, cfg):
    """Warp the source onto the reference grid with the configured resampling kernel."""
    src = _as_array(source.array)
    ref_shape = (reference.meta or {}).get("shape")
    if not ref_shape:
        ref_arr = _as_array(reference.array)
        ref_shape = ref_arr.shape[:2]
    height, width = int(ref_shape[0]), int(ref_shape[1])
    interp = _INTERP.get(str(cfg.get("warp_interp", "cubic")).lower(), cv2.INTER_CUBIC)
    matrix = np.asarray(H, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        matrix = np.eye(3)
    return cv2.warpPerspective(src, matrix, (width, height), flags=interp)


def run_pipeline(source_path: str, ref_path: str, out_dir: str, config: dict = None) -> dict:
    """Register source onto reference and write the six artifacts; returns the metrics."""
    cfg = load_config(overrides=config or {})
    timer = _Timer()
    started = time.perf_counter()

    with timer("load"):
        source = load_product(source_path)
        reference = load_product(ref_path)

    # P2 — never match blind. The metadata puts us inside the residual cross-mission
    # offset instead of searching the whole reference frame.
    with timer("coarse_init"):
        if (cfg.get("match") or {}).get("coarse_init", True):
            init, init_info = coarse_init_info(source, reference)
        else:
            init, init_info = None, {"method": "disabled"}

    # P1 — the differentiator, and the toggle we flip on stage.
    with timer("canonicalise"):
        if (cfg.get("photometry") or {}).get("canonicalise", True):
            src_canon = _canonicalise_cached(source, cfg)
            ref_canon = _canonicalise_cached(reference, cfg)
        else:
            src_canon = _passthrough_canonical(source)
            ref_canon = _passthrough_canonical(reference)

    # P5 — quotas applied during matching, not as a post-hoc filter.
    # P3 — the cascade meets the two images at a comparable resolution rather than
    # bridging the whole scale ratio in one jump. Measured at ratio 16 it is both far
    # more accurate and ~14x FASTER than direct matching, because every level's search
    # window is bounded by the level above.
    with timer("match"):
        cascade_info = None
        if (cfg.get("match") or {}).get("cascade_enabled", True):
            # cell_budgets rides INSIDE config here, unlike match_tiled's separate kwarg.
            # Passing it positionally would silently drop the P5 quotas with no error.
            casc_cfg = dict(cfg.get("match") or {})
            casc_cfg.update({
                "grid_n": int(cfg.get("grid_n", 4)),
                "halo_px": int(cfg.get("halo_px", 64)),
                "geometry": cfg.get("geometry"),
                "cell_budgets": cell_budgets(cfg),
            })
            matches, cascade_info = match_cascade(src_canon, ref_canon,
                                                  config=casc_cfg, init=init)
            cell_info = cascade_info.get("cell_info")
        else:
            matches, cell_info = match_tiled(
                src_canon, ref_canon,
                grid_n=int(cfg.get("grid_n", 4)),
                halo_px=int(cfg.get("halo_px", 64)),
                config=cfg.get("match"),
                cell_budgets=cell_budgets(cfg),
                init=init,
            )

    with timer("verify"):
        registration = verify_matches(matches, cfg.get("geometry"), init=init)

    # P4 — refinement moves the points, so the model has to be refit on the corrected
    # set. Refining and then keeping the pre-refinement transform measures nothing.
    with timer("refine"):
        refined = False
        refined_count = 0
        refine_method = None
        lsm_info = None
        if (cfg.get("geometry") or {}).get("subpixel", True) and len(matches.src_xy) > 0:
            before_xy = np.array(matches.ref_xy, dtype=np.float64, copy=True)
            refine_cfg = cfg.get("refine") or {}
            # Phase correlation stays the default: measured, LSM only wins where the
            # image is blurred or noisy, and it rejects far more points under hard
            # cross-illumination. LSM's virtue there is that it REFUSES rather than
            # degrading the set — on synth_pair_A phase correlation makes the tie-point
            # set worse than not refining at all, while LSM leaves it alone.
            if str(refine_cfg.get("method", "phase")).lower() == "lsm":
                matches, sigma, lsm_info = lsm_refine(matches, src_canon, ref_canon,
                                                      registration.params, refine_cfg)
                refine_method = "lsm"
            else:
                matches, sigma = refine_matches(matches, src_canon, ref_canon,
                                                registration.params, refine_cfg)
                lsm_info, refine_method = None, "phase_cross_correlation"
            # The honest count: points whose coordinates actually moved. sigma is NaN for
            # points refined above refine.py's calibrated correlation range, so counting
            # finite sigma undercounts real refinements (metrics.py:86).
            after_xy = np.array(matches.ref_xy, dtype=np.float64)
            if after_xy.shape == before_xy.shape:
                refined_count = int((np.abs(after_xy - before_xy) > 1e-9).any(axis=1).sum())
            registration = verify_matches(matches, cfg.get("geometry"), init=init)
            registration.sigma = sigma       # NaN where refinement honestly declined
            refined = True

    with timer("metrics"):
        gt = find_ground_truth(source_path)
        gt_H = np.asarray(gt["H_src_to_ref"], dtype=np.float64) if gt and "H_src_to_ref" in gt else None
        verify_metrics = dict(registration.metrics or {})
        registration.metrics = compute_metrics(
            matches, registration, src_canon.albedo.shape,
            grid_n=int(cfg.get("grid_n", 4)), mask=src_canon.mask,
            gt_H=gt_H, runtime_s=round(time.perf_counter() - started, 3),
        )
        registration.metrics.update({
            "stage_s": timer.stages,
            "subpixel_applied": refined,
            "refined_count": refined_count,
            "init_method": init_info.get("method"),
            "init_crs_match": init_info.get("crs_match"),
            # The P1 headline: which illumination mode actually ran, and whether the
            # phase-congruency map is real or a disabled placeholder.
            "illum_mode": src_canon.params.get("illum_mode"),
            "pc_status": src_canon.params.get("pc_status"),
            # Which pose rendered the illumination field, and why "auto" chose the scale
            # it chose. Without these the new auto default is unauditable: metrics.json
            # would say "dem_lowfreq" with no way to see what evidence picked it.
            **{k: src_canon.params.get(k) for k in (
                "illum_scale", "illum_scale_reason", "dem_transform", "dem_align",
                "dem_coverage_frac", "pose_uncertainty_px", "pose_trusted_reason",
                "dem_transform_fallback")},
            "canonicalised": bool((cfg.get("photometry") or {}).get("canonicalise", True)),
            "model_candidates": verify_metrics.get("model_candidates"),
            # Redundancy travels with the RMSE: a near-zero rmse_px on an
            # exactly-determined fit is the easiest number in this project to
            # misread as accuracy, so the caveat must reach metrics.json.
            "redundancy": verify_metrics.get("redundancy"),
            "rmse_trustworthy": verify_metrics.get("rmse_trustworthy"),
            "rmse_warning": verify_metrics.get("rmse_warning"),
            "models_rejected": verify_metrics.get("models_rejected"),
            "verify_status": verify_metrics.get("status", "failed"),
            "cell_info": cell_info,
            "refine_method": refine_method,
            # The cascade's own per-level inlier count is NOT this run's inlier count:
            # stages re-verifies the returned MatchSet at full source resolution and gets
            # a materially different fit. Keep them separately labelled.
            "cascade": _json_safe(cascade_info) if cascade_info else None,
            "gt_source": "gt.json" if gt_H is not None else None,
        })

    with timer("warp"):
        registered = _warp(source, reference, registration.params, cfg)

    with timer("write"):
        registration.metrics["stage_s"] = timer.stages
        write_outputs(out_dir, source, reference, registration, matches, registered, cfg)

    return registration.metrics
