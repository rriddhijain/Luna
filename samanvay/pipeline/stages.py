"""Seat 3 · pipeline/stages — the ordered run, with every stage switchable from config.

    load -> coarse init (P2) -> canonicalise (P1) -> tiled match (P5 quotas)
         -> verify ladder (P2) -> sub-pixel refine (P4) -> re-verify
         -> metrics -> warp -> write

The switches are real plumbing rather than an `if` bolted on the night before the
demo, because the ablation table IS the evidence that the physics stage does the
work. `photometry.canonicalise=false` is the toggle we flip live on stage.

Timing per stage lands in metrics.json["stage_s"], which is where bench/harness
reads it from — it cannot measure stages from outside the pipeline.

The three "auto" switches (match.method, photometry.phase_congruency, photometry.clahe)
resolve HERE and nowhere else, because this is the first point that has both products'
metadata. The resolved values are what every later stage sees and what provenance.json
records, so a run is reproducible from its own record without re-deriving the rule.
"""

import copy
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
from samanvay.geometry.tps import pullback
from samanvay.geometry.uniformity import grid_shape
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

# The delta-sun bar "auto" resolves on, in degrees. It is io/preflight.py's bar and its
# reasoning verbatim, deliberately: the recommendation `samanvay check` prints and the
# choice `samanvay register` makes must be the same rule, or a judge reading both is
# reading two different engines.
_AUTO_SUN_BAR_DEG = 20.0
# Which resolved matchers read the phase-congruency map, and which read intensity.
# detect.py's own split (l2/rift peak-pick the PC map, sift/orb are handed to OpenCV).
_PC_METHODS = ("rift", "l2")
_INTENSITY_METHODS = ("sift", "orb")


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


def delta_sun_az_deg(src_meta, ref_meta):
    """Circular difference of the two sun azimuths in degrees, or None if either is unknown.

    Circular, not arithmetic: 350 deg and 10 deg are 20 deg apart, and the linear
    difference of 340 would send every near-midnight pair down the RIFT arm on a
    difference that does not exist.
    """
    a = (src_meta or {}).get("sun_az_deg")
    b = (ref_meta or {}).get("sun_az_deg")
    if a is None or b is None:
        return None
    try:
        d = abs(float(a) - float(b)) % 360.0
    except (TypeError, ValueError):
        return None
    return round(min(d, 360.0 - d), 2)


def resolve_auto(cfg: dict, src_meta: dict, ref_meta: dict) -> tuple:
    """Resolve the three "auto" switches from the pair's metadata; returns (cfg, info).

    * `match.method` — unknown delta sun azimuth or >= 20 deg gives "rift", else "sift".
      Shipping a fixed "sift" turned the project's headline differentiator off: at delta
      sun 50 deg every SIFT arm returns zero inliers where RIFT returns 173
      (bench/baselines.md).
    * `photometry.phase_congruency` — True iff the resolved matcher consumes the PC map.
      On the SIFT path the map cost ~11.5 s per image and was then discarded.
    * `photometry.clahe` — True iff the resolved matcher is intensity-based. On the RIFT
      arm the descriptor reads phase and is contrast-invariant by construction, so CLAHE
      there only injects a spatially varying non-linearity and a step at every tile
      boundary (docs/decisions.md).

    A value that is not "auto" is passed through untouched and the reason says so: this
    resolves defaults, it never overrides what an ablation asked for.
    """
    cfg = copy.deepcopy(cfg or {})
    match = cfg.setdefault("match", {})
    photometry = cfg.setdefault("photometry", {})

    dsun = delta_sun_az_deg(src_meta, ref_meta)
    recommended = "rift" if (dsun is None or dsun >= _AUTO_SUN_BAR_DEG) else "sift"
    requested = str(match.get("method", "auto") or "auto").strip().lower()

    if requested == "auto":
        method = recommended
        where = "unknown" if dsun is None else "%s deg" % dsun
        reason = ("delta sun azimuth is %s, %s the %g deg bar, so the %s arm was chosen"
                  % (where, "at or over" if method == "rift" else "under",
                     _AUTO_SUN_BAR_DEG, method))
    else:
        method = requested
        reason = ("match.method was pinned to %r, so nothing was resolved" % requested)
    match["method"] = method

    pc_req = photometry.get("phase_congruency", "auto")
    if str(pc_req).strip().lower() == "auto":
        pc_on = method in _PC_METHODS
        pc_reason = ("the %s descriptor %s the phase-congruency map"
                     % (method, "reads" if pc_on else "never reads"))
    else:
        pc_on, pc_reason = bool(pc_req), "photometry.phase_congruency was set explicitly"
    photometry["phase_congruency"] = pc_on

    clahe_req = photometry.get("clahe", "auto")
    if str(clahe_req).strip().lower() == "auto":
        clahe_on = method in _INTENSITY_METHODS
        clahe_reason = ("%s matches on intensity, where local contrast is what the "
                        "descriptor has to work with" % method if clahe_on else
                        "%s is contrast-invariant by construction, so CLAHE would only "
                        "add a spatially varying non-linearity" % method)
    else:
        clahe_on, clahe_reason = bool(clahe_req), "photometry.clahe was set explicitly"
    photometry["clahe"] = clahe_on

    info = {
        "delta_sun_az_deg": dsun,
        "match_method_requested": requested,
        "match_method_resolved": method,
        "match_method_reason": reason,
        # What the same rule would have recommended regardless of what was pinned, so
        # run.py can say out loud that the engine knew and was overridden.
        "match_method_recommended": recommended,
        "phase_congruency_resolved": pc_on,
        "phase_congruency_reason": pc_reason,
        "clahe_resolved": clahe_on,
        "clahe_reason": clahe_reason,
    }
    return cfg, info


def acceptance(metrics: dict) -> tuple:
    """Is this a registration anyone may quote? Returns (accepted, reasons).

    `verify_status` answers "did a model fit", which is not the same question. On the real
    Chandrayaan-2 TMC -> LRO WAC pair — the closest thing in this repo to the actual
    cross-sensor task — a model fitted on 4 inliers out of 24 matches, the held-out rig
    produced NOTHING (check_status "skipped_too_few_matches", n_check 0, check_rmse_px
    null), SDI came out at 0.09, and the run still reported verify_status "ok" and exited
    0. A judge pointing the tool at those two products got a green result backed by four
    tie-points. Every honesty mechanism in this project was present and bypassed, because
    the exit code asked the wrong question.

    The three bars below are NOT new numbers. Each is a threshold this repo already sets
    and already justifies from measurement, reused here rather than re-invented:
      * verify_status      geometry/verify.py — a model survived robust fitting.
      * check_status       geometry/verify.py _MIN_CHECK / _CONTROL_MULTIPLE — enough
                           tie-points existed to hold any out. Without it there is no
                           independent accuracy figure at all, only the fit's own sample.
      * rmse_trustworthy   geometry/verify.py _TRUST_REDUNDANCY = 10, measured in
                           runs/calibrate/redundancy.md as the first bin where no fit
                           understates its own error by more than 3x.

    A run that clears all three is not thereby ACCURATE — see docs/limitations.md on
    held-out self-consistency versus geodetic truth. It is merely a run whose numbers mean
    what they say. That is the only claim this gate makes.
    """
    reasons = []
    if metrics.get("verify_status") != "ok":
        reasons.append("verify_status=%s (%s)" % (
            metrics.get("verify_status"),
            metrics.get("verify_reason") or "no model survived verification"))
    check = metrics.get("check_status")
    if check != "ok":
        reasons.append(
            "no independent accuracy figure: check_status=%s, n_check=%s — the held-out "
            "rig produced nothing, so nothing here is validated against points the fit "
            "did not see" % (check, metrics.get("n_check")))
    if not metrics.get("rmse_trustworthy"):
        reasons.append(
            "redundancy %s is below the trust bar of 10: the fit is close to reproducing "
            "its own sample, so its RMSE is near-zero by construction"
            % metrics.get("redundancy"))
    return (not reasons), reasons


def _apply_seed(seed):
    """Seed the one RNG this pipeline has, or say plainly that nothing was seeded.

    The only stochastic estimator in the registration path is OpenCV's RANSAC/MAGSAC
    sampler, driven by cv2's global RNG. Everything else that could have used randomness
    does not: the control/check split is a hash of the coordinates (geometry/verify.py),
    ANMS breaks ties by index, and no numpy generator is constructed anywhere under
    samanvay/ outside trn.py's frame simulator. So cv2.setRNGSeed is the whole of what
    --seed can control, and provenance records exactly that rather than implying a
    reproducibility guarantee over stages that have no RNG to fix.
    """
    if seed is None:
        return {"seed_applied": False, "seed_applied_to": None,
                "seed_reason": "no seed given: nobody seeded"}
    try:
        cv2.setRNGSeed(int(seed))
    except (AttributeError, TypeError, ValueError, cv2.error) as exc:
        return {"seed_applied": False, "seed_applied_to": None,
                "seed_reason": "cv2.setRNGSeed refused the value (%s)" % type(exc).__name__}
    return {"seed_applied": True,
            "seed_applied_to": "cv2 global RNG (RANSAC/MAGSAC sampling in geometry/verify)",
            "seed_reason": "the rest of the pipeline is deterministic without an RNG"}


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
        os_ = __import__("os")
        stat = os_.stat(product.path)
        # The absolute path is in the identity, not just in the namespace fallback.
        # Without it the key was (product_id, params, size, mtime-to-the-second, shape),
        # and synth/sweep.py emits fixtures that match on every one of those: same
        # renderer so the same product_id, same shape, near-identical size, all written
        # inside one second. Two delta-sun steps then shared a cache entry and the second
        # one silently matched on the first one's albedo — which would have quietly
        # corrupted the sweep table in bench/baselines.md rather than failing.
        # mtime at nanosecond resolution for the same reason: a file rewritten within the
        # same second at the same size must not look unchanged.
        identity = {"params": photometry, "size": stat.st_size,
                    "mtime_ns": int(stat.st_mtime_ns),
                    "path": os_.path.abspath(product.path),
                    "shape": list(meta.get("shape") or ())}
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


def _warp(source, reference, registration, cfg):
    """Warp the source onto the reference grid under the DELIVERED model.

    A projective-only fit stays on cv2.warpPerspective: it is the common path and there
    is nothing to gain by making it build a dense map first.

    When verify_matches accepted a spline, the projective matrix ALONE is not the model
    the metrics were measured on, and warping with it delivers a raster that ignores the
    warp while metrics.json advertises tps_applied=true and a held-out improvement —
    a number on a slide the product does not honour. So the source coordinate of every
    output pixel comes from tps.pullback (the global pull-back, then the spline, in that
    frozen order) and cv2.remap resamples through it. remap takes the x and y maps as
    two separate float32 arrays, and its default BORDER_CONSTANT 0 fill is the same
    uncovered-pixel value warpPerspective leaves, which is what io/writers declares as
    nodata.
    """
    src = _as_array(source.array)
    ref_shape = (reference.meta or {}).get("shape")
    if not ref_shape:
        ref_arr = _as_array(reference.array)
        ref_shape = ref_arr.shape[:2]
    height, width = int(ref_shape[0]), int(ref_shape[1])
    interp = _INTERP.get(str(cfg.get("warp_interp", "cubic")).lower(), cv2.INTER_CUBIC)
    matrix = np.asarray(registration.params, dtype=np.float64)
    if matrix.shape != (3, 3) or not np.isfinite(matrix).all():
        matrix = np.eye(3)

    warp = getattr(registration, "warp", None)
    if warp is None:
        return cv2.warpPerspective(src, matrix, (width, height), flags=interp)

    xs, ys = np.meshgrid(np.arange(width, dtype=np.float64),
                         np.arange(height, dtype=np.float64))
    xy = pullback(matrix, np.column_stack([xs.ravel(), ys.ravel()]), warp)
    if xy is None:
        # A matrix pullback cannot invert is one warpPerspective cannot use either, but
        # it is cv2's failure to report, not ours to invent a map for.
        return cv2.warpPerspective(src, matrix, (width, height), flags=interp)
    map_x = np.ascontiguousarray(xy[:, 0].reshape(height, width), dtype=np.float32)
    map_y = np.ascontiguousarray(xy[:, 1].reshape(height, width), dtype=np.float32)
    return cv2.remap(src, map_x, map_y, interpolation=interp,
                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)


def run_pipeline(source_path: str, ref_path: str, out_dir: str, config: dict = None) -> dict:
    """Register source onto reference and write the six artifacts; returns the metrics."""
    cfg = load_config(overrides=config or {})
    timer = _Timer()
    started = time.perf_counter()
    seed_info = _apply_seed(cfg.get("seed"))

    with timer("load"):
        # band_cfg decides how a multi-band cube becomes the one 2-D map we match on.
        # Without it --set band.reduce / band.index are accepted, printed by
        # show-config, and then silently ignored while io/bands' pc1 default runs.
        band_cfg = cfg.get("band")
        source = load_product(source_path, band_cfg=band_cfg)
        reference = load_product(ref_path, band_cfg=band_cfg)

    # Both products are loaded, so the three "auto" switches can resolve — and they must
    # resolve BEFORE canonicalise, which is where phase_congruency and clahe are read.
    # The resolved cfg is what every stage below and provenance.json see.
    cfg, auto_info = resolve_auto(cfg, source.meta, reference.meta)
    # provenance.json carries config verbatim; these three keys are the only route a
    # seed's honesty has into it without the writer inventing a claim of its own.
    cfg.update({k: seed_info[k] for k in ("seed_applied", "seed_applied_to", "seed_reason")})

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
    # The grid is rows x cols, not grid_n x grid_n: cell_budgets given the square count
    # is short by exactly the extra cells on a strip, and the missing ids then fall back
    # to match_tiled's hardcoded quotas with nothing saying so.
    aspect = bool(cfg.get("grid_aspect", True))
    grid_rows, grid_cols = grid_shape(src_canon.albedo.shape[:2],
                                      int(cfg.get("grid_n", 4)), aspect)
    budgets = cell_budgets(cfg, n_cells=grid_rows * grid_cols)
    # grid_aspect lives at the top level of the config and match_tiled reads it from the
    # match section, so it has to travel: without this --set grid_aspect=false is ignored
    # by the matcher while metrics.py honours it, and the two disagree about cell ids.
    match_cfg = dict(cfg.get("match") or {})
    match_cfg["grid_aspect"] = aspect

    with timer("match"):
        cascade_info = None
        if match_cfg.get("cascade_enabled", True):
            # cell_budgets rides INSIDE config here, unlike match_tiled's separate kwarg.
            # Passing it positionally would silently drop the P5 quotas with no error.
            casc_cfg = dict(match_cfg)
            casc_cfg.update({
                "grid_n": int(cfg.get("grid_n", 4)),
                "halo_px": int(cfg.get("halo_px", 64)),
                "geometry": cfg.get("geometry"),
                "cell_budgets": budgets,
            })
            matches, cascade_info = match_cascade(src_canon, ref_canon,
                                                  config=casc_cfg, init=init)
            cell_info = cascade_info.get("cell_info")
        else:
            matches, cell_info = match_tiled(
                src_canon, ref_canon,
                grid_n=int(cfg.get("grid_n", 4)),
                halo_px=int(cfg.get("halo_px", 64)),
                config=match_cfg,
                cell_budgets=budgets,
                init=init,
            )

    # The init the VERIFY gate uses is not always the init the matcher used. The cascade
    # has already fitted this pair and stored the result; gating on the metadata prior
    # instead throws that away. Measured on fixtures/dsun_sweep/dsun_50 with a +32
    # reference-px offset added to the prior: 188 matches and a healthy fit collapse to
    # verify_status=failed, 0 inliers, gt_rmse_px 66.2, because exactly 4 matches pass
    # the gate. Offsets of 0, 8, 16, 24, 48, 64 and 128 px all register, so it presents
    # as an unreproducible one-off rather than as a bug in the gate.
    verify_init, verify_init_source = init, ("coarse_init" if init is not None else "none")
    if cascade_info is not None and cascade_info.get("transform") is not None:
        verify_init, verify_init_source = cascade_info["transform"], "cascade_transform"

    with timer("verify"):
        registration = verify_matches(matches, cfg.get("geometry"), init=verify_init)

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
            registration = verify_matches(matches, cfg.get("geometry"), init=verify_init)
            registration.sigma = sigma       # NaN where refinement honestly declined
            refined = True

    with timer("metrics"):
        gt = find_ground_truth(source_path)
        gt_H = np.asarray(gt["H_src_to_ref"], dtype=np.float64) if gt and "H_src_to_ref" in gt else None
        verify_metrics = dict(registration.metrics or {})
        registration.metrics = compute_metrics(
            matches, registration, src_canon.albedo.shape,
            grid_n=int(cfg.get("grid_n", 4)), mask=src_canon.mask,
            gt_H=gt_H, runtime_s=round(time.perf_counter() - started, 3), aspect=aspect,
        )
        registration.metrics.update({
            "stage_s": timer.stages,
            # What the three "auto" switches resolved to and why. Without these the auto
            # default is unauditable: metrics.json would name a method with no evidence
            # for it, and the ablation could not tell a resolved arm from a pinned one.
            **auto_info,
            **seed_info,
            "seed": cfg.get("seed"),
            "subpixel_applied": refined,
            "refined_count": refined_count,
            "init_method": init_info.get("method"),
            "init_crs_match": init_info.get("crs_match"),
            # The P1 headline: which illumination mode actually ran, and whether the
            # phase-congruency map is real or a disabled placeholder.
            "illum_mode": src_canon.params.get("illum_mode"),
            "pc_status": src_canon.params.get("pc_status"),
            # The rest of what the canonicaliser decided. mask_fill_px stays int or null:
            # "reflect" that filled nothing and a run that never reached the fill are
            # different states, and 0 would report the second as the first.
            **{k: src_canon.params.get(k) for k in (
                "clahe_applied", "clahe_clip", "clahe_grid", "mask_fill", "mask_fill_px",
                "phase_congruency_requested")},
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
            "verify_reason": verify_metrics.get("reason"),
            # The init gate's own counters, promised by docs/CONTRACTS.md and dropped by
            # the hand-copied passthrough this replaces. They are exactly the numbers
            # that make a bad prior visible: 184 of 188 matches gated out is a diagnosis,
            # "0 inliers" alone is not.
            "init_gated_out": verify_metrics.get("init_gated_out"),
            "init_gate_px": verify_metrics.get("init_gate_px"),
            "init_gate_fallback": verify_metrics.get("init_gate_fallback"),
            "verify_init_source": verify_init_source,
            "cell_info": cell_info,
            "refine_method": refine_method,
            # The cascade's own per-level inlier count is NOT this run's inlier count:
            # stages re-verifies the returned MatchSet at full source resolution and gets
            # a materially different fit. Keep them separately labelled.
            "cascade": _json_safe(cascade_info) if cascade_info else None,
            "gt_source": "gt.json" if gt_H is not None else None,
        })
        # Computed last, because it reads the assembled metrics rather than any one stage.
        accepted, why = acceptance(registration.metrics)
        registration.metrics["accepted"] = accepted
        registration.metrics["acceptance_reasons"] = why

    with timer("warp"):
        registered = _warp(source, reference, registration, cfg)
        # The source-grid product is the source's own pixels, unresampled: io/writers
        # georeferences them by composing the reference geotransform with the fitted
        # transform, so nothing is lost to a resample onto a coarser reference grid
        # (ch2_wac: 3000x3000 delivered as 128x128). Only built when asked for.
        grid = str(((cfg.get("output") or {}).get("grid") or "both")).lower()
        registered_source = (_as_array(source.array)
                             if grid in ("source", "both") else None)
        registration.metrics.update({
            "output_grid": grid,
            "registered_source_written": registered_source is not None,
        })

    with timer("write"):
        registration.metrics["stage_s"] = timer.stages
        write_outputs(out_dir, source, reference, registration, matches, registered, cfg,
                      registered_source=registered_source)

    return registration.metrics
