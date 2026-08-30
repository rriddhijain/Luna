"""Seat 6 (geometry) — pillar P2: robust verification over a transform model ladder.

Three models are fitted to the putative matches — similarity (4 dof), affine (6 dof)
and homography (8 dof) — each with a robust estimator, and the SIMPLEST one whose
inlier RMSE is within `model_margin` of the best is selected. The margin is a stated
number that ships in the metrics, not a knob tuned until the picture looked good.

Two conventions this module is strict about, both frozen repo-wide:

* Every transform maps SOURCE -> REFERENCE, as a 3x3 float64 matrix.
* Every residual and every RMSE is in SOURCE pixels. A residual is therefore
  H^-1(ref_xy) - src_xy, i.e. the reference point pulled back into the source frame,
  NOT H(src_xy) - ref_xy. With a 2x scale ratio the two differ by a factor of two.
  RMSE is Euclidean: sqrt(mean(dx^2 + dy^2)), not the per-component RMS.

OpenCV measures its RANSAC threshold as a forward reprojection error, in reference
pixels, so `config["ransac_thresh_px"]` (source pixels, like everything else) is
converted with the scale implied by the init or by a rough pre-fit.

config keys: model ("similarity"|"affine"|"homography"|"auto"), model_margin,
ransac_thresh_px, init_gate_px, grid_n.
"""

import time

import cv2
import numpy as np

from samanvay.types import MatchSet, Registration
from samanvay.geometry.init import apply_transform

_MODELS = ("similarity", "affine", "homography")   # simplest first — the ladder
_MIN_INLIERS = 4          # below this an "RMSE" is just the fit reproducing its own sample

# Points needed to determine each model exactly. A fit supported by exactly its own
# minimal sample passes through those points by construction, so its residual is 0
# and its RMSE measures nothing — the classic near-zero RMSE on a wrong transform.
# We require redundancy (observations beyond the minimum) before an RMSE is a number
# anyone may quote.
_MIN_SAMPLE = {"similarity": 2, "affine": 3, "homography": 4}

# Both thresholds are MEASURED, not chosen. bench/calibrate.py bins fits by redundancy
# and compares self-consistency rmse_px against true gt_rmse_px
# (runs/calibrate/redundancy.md). med_ratio = how far the fit understates its own error:
#
#   redundancy   1    2     3     4-5   6-9   10-19  20-49   (synthetic / real-run)
#   med_ratio   9.3  2.0   2.5    1.1   0.8   0.46   0.25
#               105  72.7  10.2                      1.02
#   % over 3x    75   42    43     11     4      0      0
#
# So redundancy 1 is catastrophic (100% of real runs off by >3x) and the old trust bar
# of 3 was issuing rmse_trustworthy=true on fits understating their error 2.5-10x.
# Redundancy 10 is the first bin where nothing exceeds 3x.
_MIN_REDUNDANCY = 2       # disqualify a fit with no real redundancy outright
_TRUST_REDUNDANCY = 10    # below this the RMSE is reported but flagged untrustworthy
_USAC = getattr(cv2, "USAC_MAGSAC", cv2.RANSAC)


def _fit(name, src, ref, thresh_ref):
    """Robustly fit one model; returns (H 3x3, inlier bool mask) or None if it failed."""
    try:
        if name == "similarity":
            # ponytail: OpenCV 5.0 rejects USAC_* in estimateAffinePartial2D (verified at
            # runtime), so the similarity rung uses RANSAC + local refinement. Ceiling: a
            # slightly weaker inlier set than MAGSAC on this rung only. Upgrade path: when
            # cv2 supports it, swap the method to _USAC like the other two.
            M, inl = cv2.estimateAffinePartial2D(
                src, ref, method=cv2.RANSAC, ransacReprojThreshold=thresh_ref,
                maxIters=5000, confidence=0.999, refineIters=10)
        elif name == "affine":
            M, inl = cv2.estimateAffine2D(
                src, ref, method=_USAC, ransacReprojThreshold=thresh_ref,
                maxIters=5000, confidence=0.999, refineIters=10)
        else:
            M, inl = cv2.findHomography(
                src, ref, _USAC, thresh_ref, maxIters=5000, confidence=0.999)
    except cv2.error:
        return None
    if M is None or inl is None:
        return None
    H = np.asarray(M, dtype=np.float64)
    if H.shape == (2, 3):
        H = np.vstack([H, [0.0, 0.0, 1.0]])
    if H.shape != (3, 3) or not np.isfinite(H).all() or abs(np.linalg.det(H)) < 1e-12:
        return None
    return H, np.asarray(inl).ravel().astype(bool)


def _residuals(H, src, ref):
    """Residuals in SOURCE pixels, H^-1(ref) - src; None if H cannot be inverted."""
    try:
        Hi = np.linalg.inv(np.asarray(H, dtype=np.float64))
    except np.linalg.LinAlgError:
        return None
    if not np.isfinite(Hi).all():
        return None
    return apply_transform(Hi, ref) - src


def _rmse(res):
    """Euclidean RMSE of (N,2) residuals: sqrt(mean(dx^2 + dy^2)). NaN when empty."""
    if res is None or len(res) == 0:
        return float("nan")
    return float(np.sqrt(np.mean(res[:, 0] ** 2 + res[:, 1] ** 2)))


def _collinear(xy):
    """True when the points span (essentially) a line — no 2D transform is determined."""
    if len(xy) < 3:
        return True
    s = np.linalg.svd(xy - xy.mean(axis=0), compute_uv=False)
    return not (s[0] > 0) or (s[1] / s[0]) < 1e-6


def _default_gate(src):
    """Init gate in source px: 5% of the match bbox diagonal, with a 16 px floor."""
    # ponytail: the true image diagonal is not in a MatchSet, so the source-point bbox
    # stands in for it. Ceiling: a match set clustered in one corner gets a gate sized to
    # that corner. Upgrade path: pass config["shape"] and use the real diagonal.
    if len(src) == 0:
        return 0.0
    span = src.max(axis=0) - src.min(axis=0)
    return max(16.0, 0.05 * float(np.hypot(span[0], span[1])))


def _valid_init(init):
    """A 3x3 float64 init that is finite and invertible, or None."""
    if init is None:
        return None
    H = np.asarray(init, dtype=np.float64)
    if H.shape != (3, 3) or not np.isfinite(H).all() or abs(np.linalg.det(H)) < 1e-12:
        return None
    return H


def _failed(n, init_H, reason, metrics):
    """An honest failed Registration: nothing fitted, nothing invented."""
    metrics = dict(metrics)
    metrics.update({
        "status": "failed",
        "reason": reason,
        # NaN, not 0.0: a failed fit has no RMSE, and 0.0 would read as a perfect one.
        "rmse_px": float("nan"),
        "inlier_count": 0,
        "inlier_ratio": 0.0,
        "model": "failed",
        "params_source": "init" if init_H is not None else "identity",
    })
    return Registration(
        model_type="failed",
        params=(init_H.copy() if init_H is not None else np.eye(3)),
        init_params=(init_H.copy() if init_H is not None else np.eye(3)),
        inliers=np.zeros(n, dtype=bool),
        residuals=np.full((n, 2), np.nan) if n else np.zeros((0, 2)),
        sigma=np.zeros(n),
        metrics=metrics,
    )


def verify_matches(matches: MatchSet, config: dict = None,
                   init: np.ndarray = None) -> Registration:
    """Fit the model ladder to the matches and return the simplest model that holds."""
    t0 = time.time()
    cfg = dict(config or {})
    margin = float(cfg.get("model_margin", 0.10))
    thresh_src = float(cfg.get("ransac_thresh_px", 3.0))
    pinned = str(cfg.get("model", "auto") or "auto").lower()

    src = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    ref = np.asarray(matches.ref_xy, dtype=np.float64).reshape(-1, 2)
    n = len(src)
    init_H = _valid_init(init)

    metrics = {
        "n_matches": n,
        "residual_units": "source_px",
        "model_margin": margin,
        "ransac_thresh_px": thresh_src,
        "grid_n": int(cfg.get("grid_n", 4)),
        "init_used": init_H is not None,
        "init_rejected": init is not None and init_H is None,
        "runtime_s": 0.0,
    }
    # coverage_pct / dispersion_cv are deliberately absent: they need the image shape and
    # the validity mask, which a MatchSet does not carry. geometry/metrics.compute_metrics
    # owns them. Emitting a placeholder here would fabricate a headline number.

    def done(reg):
        reg.metrics["runtime_s"] = round(time.time() - t0, 4)
        return reg

    if n != len(ref):
        return done(_failed(n, init_H, "src_xy and ref_xy have different lengths", metrics))
    if n < 4:
        return done(_failed(n, init_H, "fewer than 4 matches (%d)" % n, metrics))
    if not (np.isfinite(src).all() and np.isfinite(ref).all()):
        return done(_failed(n, init_H, "non-finite match coordinates", metrics))
    if _collinear(src) or _collinear(ref):
        return done(_failed(n, init_H, "degenerate point configuration (collinear)", metrics))

    # --- init gate: we do not match blind, and we do not verify blind either ----------
    keep = np.ones(n, dtype=bool)
    if init_H is not None:
        gate = float(cfg.get("init_gate_px", _default_gate(src)))
        r0 = _residuals(init_H, src, ref)
        if r0 is not None:
            keep = np.hypot(r0[:, 0], r0[:, 1]) <= gate
            metrics["init_gate_px"] = gate
            metrics["init_gated_out"] = int(n - keep.sum())
            if keep.sum() < _MIN_INLIERS:
                # A bad init must not be able to kill every match.
                keep = np.ones(n, dtype=bool)
                metrics["init_gate_fallback"] = True
    metrics.setdefault("init_gate_px", None)
    metrics.setdefault("init_gated_out", 0)
    metrics.setdefault("init_gate_fallback", False)
    s, r = src[keep], ref[keep]

    # --- threshold from source px to the reference px OpenCV measures in --------------
    scale = None
    if init_H is not None:
        scale = float(np.sqrt(abs(np.linalg.det(init_H[:2, :2]))))
    if scale is None or not np.isfinite(scale) or scale <= 0:
        rough = _fit("similarity", s, r, thresh_src)
        scale = float(np.sqrt(abs(np.linalg.det(rough[0][:2, :2])))) if rough else 1.0
    if not np.isfinite(scale) or scale <= 0:
        scale = 1.0
    thresh_ref = thresh_src * scale
    metrics["scale_src_to_ref"] = scale
    metrics["ransac_thresh_ref_px"] = thresh_ref

    names = (pinned,) if pinned in _MODELS else _MODELS
    metrics["model_requested"] = pinned if pinned in _MODELS or pinned == "auto" else "auto"
    if pinned not in _MODELS and pinned != "auto":
        metrics["model_request_ignored"] = pinned

    cand = {}
    for name in names:
        got = _fit(name, s, r, thresh_ref)
        if got is None:
            continue
        H, inl = got
        res = _residuals(H, s, r)
        if res is None or not np.isfinite(res).all() or inl.sum() < _MIN_INLIERS:
            continue
        # Redundancy gate: an exactly-determined fit reproduces its own sample, so its
        # RMSE is ~0 regardless of whether the transform is right.
        redundancy = int(inl.sum()) - _MIN_SAMPLE[name]
        if redundancy < _MIN_REDUNDANCY:
            metrics.setdefault("models_rejected", {})[name] = (
                "exactly determined: %d inliers for a %d-point model, RMSE would be "
                "0 by construction" % (int(inl.sum()), _MIN_SAMPLE[name]))
            continue
        cand[name] = {"H": H, "inl": inl, "rmse": _rmse(res[inl]), "res": res,
                      "redundancy": redundancy}

    if not cand:
        return done(_failed(
            n, init_H,
            "no model survived robust fitting or the redundancy gate", metrics))

    # Models are compared on ONE common point set — the inliers of the best-supported
    # candidate — so that an 8-dof fit cannot win by shrinking its own inlier set.
    ref_name = max(_MODELS, key=lambda k: (cand[k]["inl"].sum() if k in cand else -1))
    common = cand[ref_name]["inl"]
    for name, c in cand.items():
        c["rmse_common"] = _rmse(c["res"][common])

    best = min(c["rmse_common"] for c in cand.values())
    chosen = ref_name
    for name in _MODELS:
        if name in cand and cand[name]["rmse_common"] <= best * (1.0 + margin) + 1e-9:
            chosen = name
            break

    metrics["model_candidates"] = {
        name: {"rmse_px": c["rmse"], "rmse_common_px": c["rmse_common"],
               "inlier_count": int(c["inl"].sum()), "redundancy": c["redundancy"]}
        for name, c in cand.items()
    }
    metrics["model_common_set"] = ref_name
    metrics["model_common_count"] = int(common.sum())

    H = cand[chosen]["H"]
    inliers = np.zeros(n, dtype=bool)
    inliers[np.flatnonzero(keep)[cand[chosen]["inl"]]] = True

    residuals = _residuals(H, src, ref)          # all N points, in source pixels
    if residuals is None:
        return done(_failed(n, init_H, "selected model is not invertible", metrics))

    metrics["status"] = "ok"
    metrics["model"] = chosen
    metrics["rmse_px"] = _rmse(residuals[inliers])
    metrics["inlier_count"] = int(inliers.sum())
    metrics["inlier_ratio"] = float(inliers.sum()) / float(n)

    # Degrees of freedom behind the RMSE. With little redundancy the fit is close to
    # reproducing its own sample, so a small rmse_px says nothing about whether the
    # transform is correct — quote gt_rmse_px (synthetic) or hold-out points instead.
    redundancy = cand[chosen]["redundancy"]
    metrics["redundancy"] = redundancy
    metrics["rmse_trustworthy"] = bool(redundancy >= _TRUST_REDUNDANCY)
    if not metrics["rmse_trustworthy"]:
        metrics["rmse_warning"] = (
            "%d inliers for a %d-point model (redundancy %d): rmse_px is near-zero by "
            "construction and must not be quoted as accuracy"
            % (int(inliers.sum()), _MIN_SAMPLE[chosen], redundancy))

    return done(Registration(
        model_type=chosen,
        params=H,
        init_params=(init_H.copy() if init_H is not None else np.eye(3)),
        inliers=inliers,
        residuals=residuals,
        sigma=np.zeros(n),          # geometry/refine.py fills this
        metrics=metrics,
    ))
