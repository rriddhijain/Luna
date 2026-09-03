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

P1.4 — the control/check split. Before anything is fitted, the kept matches are cut
into a CONTROL set and an independent CHECK set, stratified by grid cell so the held-out
points are spread over the frame rather than clustered in one corner. Everything below —
the rough pre-fit, all three rungs of the ladder, the model comparison, the spline — sees
control points only. `rmse_px` is therefore still an in-sample number and stays exactly
what it was; `check_rmse_px` is the one accuracy figure in this repo measured on points
no estimator was shown, and it is the number to quote.

The split is deterministic without an RNG: the order within a cell comes from a hash of
the rounded coordinates, so the same pair reproduces the same partition byte for byte,
across processes, numpy versions and match orderings. A seeded RNG would not survive any
of those three.

config keys: model ("similarity"|"affine"|"homography"|"auto"), model_margin,
ransac_thresh_px, init_gate_px, grid_n, check_fraction, tps, tps_lambda,
tps_min_control.
"""

import time

import cv2
import numpy as np

from samanvay.types import MatchSet, Registration
from samanvay.geometry.tps import fit_tps, pullback

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

# Guards on the split. Below either bar the split is not made at all, because a check
# set that cannot measure anything is worse than no check set: it puts a number with no
# statistical content next to the words "independent check points".
#   * control must keep 4x the model's minimal sample, i.e. redundancy the fit can lose
#     without falling through the redundancy gate below.
#   * 8 check points is the floor at which a check RMSE stops being one point's luck;
#     it is the same bar match/cascade.py uses to accept a seed fit.
_CONTROL_MULTIPLE = 4
_MIN_CHECK = 8

# FNV-1a over the four rounded coordinates, then the splitmix64 finaliser. FNV alone
# leaves the low bits of neighbouring coordinates correlated, which would put adjacent
# tie-points on the same side of the split and defeat the stratification.
_FNV_OFFSET = np.uint64(0xCBF29CE484222325)
_FNV_PRIME = np.uint64(0x100000001B3)
# Coordinates are hashed at 1e-3 px. Finer than any correspondence this pipeline can
# produce (refine.py works to ~0.05 px), coarse enough that a float64 round-trip through
# a CSV cannot move a point across the split.
_HASH_QUANTUM = 1000.0


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


def _residuals(H, src, ref, warp=None):
    """Residuals in SOURCE pixels, full_model^-1(ref) - src; None if H is not invertible.

    `warp` is the optional TPS residual, so this is the residual of the model we actually
    deliver, not of the global part of it. tps.pullback owns the global-then-spline order.
    """
    xy = pullback(H, ref, warp)
    return None if xy is None else xy - src


def _rmse(res):
    """Euclidean RMSE of (N,2) residuals: sqrt(mean(dx^2 + dy^2)). NaN when empty."""
    if res is None or len(res) == 0:
        return float("nan")
    return float(np.sqrt(np.mean(res[:, 0] ** 2 + res[:, 1] ** 2)))


def _opt(value):
    """A float that json can carry, or None. An RMSE over nothing is unknown, not 0.0."""
    if value is None:
        return None
    value = float(value)
    return value if np.isfinite(value) else None


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


def _point_hash(src, ref):
    """A stable uint64 per match, from the rounded (src, ref) coordinates only.

    Not np.random: the partition has to be a pure function of the data so that a rerun,
    a different match ORDER, or another machine reproduces it exactly. A seed would only
    reproduce it under an unchanged numpy and an unchanged number of prior draws.
    """
    q = np.round(np.column_stack([src, ref]) * _HASH_QUANTUM)
    q = np.nan_to_num(q, nan=0.0, posinf=0.0, neginf=0.0)
    q = q.astype(np.int64).astype(np.uint64)
    h = np.full(len(q), _FNV_OFFSET, dtype=np.uint64)
    for col in range(q.shape[1]):
        h = (h ^ q[:, col]) * _FNV_PRIME
    h ^= h >> np.uint64(33)
    h *= np.uint64(0xFF51AFD7ED558CCD)
    h ^= h >> np.uint64(33)
    h *= np.uint64(0xC4CEB9FE1A85EC53)
    h ^= h >> np.uint64(33)
    return h


def _check_mask(src, ref, cell, frac):
    """Boolean check-point mask, stratified by cell and deterministic (no RNG).

    Within each cell the points are ordered by `_point_hash` and held out on a running
    quota, so every cell contributes its own share of the check set and the held-out
    points are spatially spread instead of being whichever cell RANSAC happened to like.
    A cell with fewer than 1/frac points contributes none, which is the honest outcome:
    it has no point to spare.
    """
    n = len(src)
    check = np.zeros(n, dtype=bool)
    if n == 0 or not (0.0 < frac < 1.0):
        return check
    h = _point_hash(src, ref)
    cells = np.asarray(cell).ravel()
    if cells.size != n:
        cells = np.zeros(n, dtype=np.int64)
    for c in np.unique(cells):
        idx = np.flatnonzero(cells == c)
        order = idx[np.argsort(h[idx], kind="stable")]
        quota = np.floor(np.arange(1, len(order) + 1) * frac).astype(np.int64)
        take = np.diff(np.concatenate([[0], quota])) >= 1
        check[order[take]] = True
    return check


def _null_check(frac, status):
    """The frozen check-split keys with nothing measured yet.

    Every figure is null rather than 0.0: a 0.0 check RMSE reads as a perfect
    registration, which is exactly the lie this split exists to make impossible.
    """
    return {"check_fraction": float(frac), "check_status": status,
            "n_check": 0, "n_control": 0, "n_check_inlier": 0,
            "check_rmse_px": None, "check_rmse_all_px": None,
            "check_p90_px": None, "check_outlier_frac": None}


def _null_tps(status):
    """The frozen TPS keys for a run that never got as far as fitting a spline."""
    return {"tps_applied": False, "tps_status": status, "tps_n_control": 0,
            "tps_check_rmse_before_px": None, "tps_check_rmse_after_px": None}


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
    frac = float(cfg.get("check_fraction", 0.2) or 0.0)
    tps_req = cfg.get("tps", "auto")
    tps_auto = str(tps_req).strip().lower() == "auto"
    tps_forced = tps_req is True or str(tps_req).strip().lower() == "true"

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
    # A run that fails before the split still reports the split it did not make, so
    # nothing downstream has to distinguish "no key" from "no hold-out".
    metrics.update(_null_check(frac, "disabled" if frac <= 0.0 else
                               "skipped_too_few_matches"))
    metrics.update(_null_tps("disabled" if not (tps_auto or tps_forced)
                             else "too_few_control"))
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

    # --- P1.4: the control/check split, after the init gate and before ANY fit --------
    # The rough pre-fit below is included in "any fit": it sets the RANSAC threshold,
    # which decides who counts as an inlier, so a check point must not reach it either.
    kept_idx = np.flatnonzero(keep)
    # With model "auto" the ladder may end on the homography, so the control floor uses
    # the largest minimal sample on offer, not the rung we happen to select later.
    min_sample = _MIN_SAMPLE.get(pinned, max(_MIN_SAMPLE.values()))
    check_kept = np.zeros(len(s), dtype=bool)
    check_status = "disabled" if frac <= 0.0 else "skipped_too_few_matches"
    if frac > 0.0:
        cells = (np.asarray(matches.cell).ravel() if matches.cell is not None
                 else np.zeros(n, dtype=np.int64))
        if cells.size != n:
            cells = np.zeros(n, dtype=np.int64)
        proposed = _check_mask(s, r, cells[keep], frac)
        if (proposed.sum() >= _MIN_CHECK
                and len(s) - int(proposed.sum()) >= _CONTROL_MULTIPLE * min_sample):
            check_kept = proposed
            check_status = "ok"
    control_kept = ~check_kept
    fit_s, fit_r = s[control_kept], r[control_kept]
    check_full = np.zeros(n, dtype=bool)
    check_full[kept_idx[check_kept]] = True
    # Points the init gate dropped are control, not check: they were never held out from
    # anything, and labelling them check would pad n_check with points no fit could use.
    roles = check_full.astype(np.uint8) if check_status == "ok" else None
    metrics["check_status"] = check_status
    metrics["n_check"] = int(check_kept.sum())
    metrics["n_control"] = int(control_kept.sum())

    # --- threshold from source px to the reference px OpenCV measures in --------------
    scale = None
    if init_H is not None:
        scale = float(np.sqrt(abs(np.linalg.det(init_H[:2, :2]))))
    if scale is None or not np.isfinite(scale) or scale <= 0:
        rough = _fit("similarity", fit_s, fit_r, thresh_src)
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
        got = _fit(name, fit_s, fit_r, thresh_ref)
        if got is None:
            continue
        H, inl = got
        res = _residuals(H, fit_s, fit_r)
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
    control_idx = kept_idx[control_kept]
    fit_inliers = np.zeros(n, dtype=bool)        # the control points RANSAC kept
    fit_inliers[control_idx[cand[chosen]["inl"]]] = True

    residuals = _residuals(H, src, ref)          # all N points, in source pixels
    if residuals is None:
        return done(_failed(n, init_H, "selected model is not invertible", metrics))

    # --- the spline, and the only test that makes it safe to ship --------------------
    # Fitted on the control inliers, judged on the check set. A spline that has memorised
    # its control points cannot lower an RMSE measured on points it was never shown, so
    # overfitting is discarded by the evidence rather than argued about.
    warp = None
    tps_status = "disabled"
    tps_before = tps_after = None
    # The control inliers the spline was offered — 0 when it was never offered any.
    n_tps_control = int(fit_inliers.sum()) if (tps_auto or tps_forced) else 0
    if tps_auto or tps_forced:
        ctrl = np.flatnonzero(fit_inliers)
        if len(ctrl) < int(cfg.get("tps_min_control", 25)):
            tps_status = "too_few_control"
        else:
            back = pullback(H, ref[ctrl])
            candidate = (None if back is None else
                         fit_tps(src[ctrl], back, cfg.get("tps_lambda", 0.5)))
            res_tps = (None if candidate is None else
                       _residuals(H, src, ref, candidate))
            if candidate is None or res_tps is None or not np.isfinite(res_tps).all():
                tps_status = "singular"
            elif not check_full.any():
                # No hold-out exists, so the acceptance test cannot be run at all. Under
                # "auto" an unvalidated non-rigid warp is not shipped; `tps: true` forces
                # it, and the null before/after says no comparison stands behind it.
                tps_status = "applied" if tps_forced else "rejected_no_improvement"
            else:
                tps_before = _opt(_rmse(residuals[check_full]))
                tps_after = _opt(_rmse(res_tps[check_full]))
                # check_rmse_all_px is the primary criterion and carries no threshold, so
                # no bar can be moved until the spline passes. Alone it is not sufficient:
                # un-thresholded, it is mostly a measurement of the gross mismatches in
                # the check set (268 px against the 0.58 px the spline is on trial for, at
                # 20% outliers), and a 700 px mismatch jostled 1 px swamps the signal.
                # Measured on bench/fake_matches, 15 seeds, 300 pts, similarity + 0.5 px
                # noise and NO relief, so "reject" is the only correct verdict:
                #
                #   gross outliers in the check set    0%     5%     20%
                #   accepted on check_rmse_all_px      0/15   4/15   7/15
                #   accepted on both conditions        0/15   0/15   0/15
                #
                # and on synthetic relief both conditions accept 5/5, so the second one
                # costs no true positive. It is: do not degrade the held-out points that
                # were already inside the threshold. `settled` is computed from the
                # PRE-spline residuals and then held fixed — recomputing it afterwards
                # would compare two different point sets and penalise a spline for the
                # act of pulling an outlier back inside the threshold. With no settled
                # check point at all both sides are NaN, the comparison is false and the
                # spline is rejected: nothing was available to validate it on.
                settled = np.hypot(*residuals[check_full].T) <= thresh_src
                improved = (tps_before is not None and tps_after is not None
                            and tps_after < tps_before
                            and _rmse(res_tps[check_full][settled])
                            <= _rmse(residuals[check_full][settled]))
                tps_status = "applied" if (improved or tps_forced) \
                    else "rejected_no_improvement"
            if tps_status == "applied":
                warp = candidate
                residuals = res_tps               # residuals must be the DELIVERED model
    metrics.update({"tps_applied": warp is not None, "tps_status": tps_status,
                    "tps_n_control": n_tps_control,
                    "tps_check_rmse_before_px": tps_before,
                    "tps_check_rmse_after_px": tps_after})

    # --- what we deliver, and what we measure it on ----------------------------------
    mag = np.hypot(residuals[:, 0], residuals[:, 1])
    # A check point inside the threshold is a delivered tie-point: coverage and dispersion
    # measure the points we hand over, so leaving a good held-out point out of `inliers`
    # would under-report the very uniformity the grid exists to enforce. It still never
    # entered a fit — being counted here buys it no influence over the transform.
    check_inliers = check_full & (mag <= thresh_src)
    inliers = fit_inliers | check_inliers
    if check_full.any():
        metrics.update({
            "n_check_inlier": int(check_inliers.sum()),
            "check_rmse_px": _opt(_rmse(residuals[check_inliers])),
            "check_rmse_all_px": _opt(_rmse(residuals[check_full])),
            "check_p90_px": _opt(np.percentile(mag[check_full], 90)),
            "check_outlier_frac": float(1.0 - check_inliers.sum() / check_full.sum()),
        })

    metrics["status"] = "ok"
    metrics["model"] = chosen
    # rmse_px is unchanged, and deliberately so: in-sample, over the fit's own inliers.
    # With no split that is every inlier, exactly as before; with a split it is the
    # control inliers, which is what "in-sample" has always meant here.
    metrics["rmse_px"] = _rmse(residuals[fit_inliers])
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
            "construction and must not be quoted as accuracy — quote check_rmse_px, "
            "measured on points no estimator saw"
            % (int(fit_inliers.sum()), _MIN_SAMPLE[chosen], redundancy))

    return done(Registration(
        model_type=(chosen + "+tps") if warp is not None else chosen,
        params=H,
        init_params=(init_H.copy() if init_H is not None else np.eye(3)),
        inliers=inliers,
        residuals=residuals,
        sigma=np.zeros(n),          # geometry/refine.py fills this
        metrics=metrics,
        roles=roles,
        warp=warp,
    ))
