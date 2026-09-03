"""Seat 6 · Pillars P4/P5 — the metrics.json contract.

Nothing goes on a slide unless it came out of here. Every value is a plain
Python float/int/str/list so json.dump works unaided, and an undefined quantity
is emitted as null: a 0.0 RMSE computed from zero matches is a lie, not a score.
"""

import numpy as np

from samanvay.types import MatchSet, Registration
from samanvay.geometry.tps import pullback
from samanvay.geometry.uniformity import uniformity_report

# Points at which the transform is compared against ground truth: a regular grid
# over the source extent, so gt_rmse_px measures the delivered transform over the
# whole scene and cannot be flattered by where the matches happened to land.
_GT_GRID_N = 9

# The plan's acceptance bar for the inlier ratio (docs/ISRO_ID26166_Prototype_Plan.md
# line 70, "Inlier Ratio (> 85%)"). Emitted as a pass/fail beside the value so a miss
# is visible rather than left for the reader to compare by eye.
_INLIER_RATIO_TARGET = 0.85

# Carried verbatim out of Registration.metrics, which verify_matches fills. These are
# the held-out (P1.4) and TPS numbers: they are computed where the split and the fit
# live, and compute_metrics is the only thing that puts them in metrics.json. Absent
# means the stage did not run, and the key is null — never a default that reads as a
# result. Floats go through _f, counts through _i, the rest verbatim.
_CARRY_FLOAT = ("check_rmse_px", "check_rmse_all_px", "check_p90_px",
                "check_outlier_frac", "check_fraction",
                "tps_check_rmse_before_px", "tps_check_rmse_after_px")
_CARRY_INT = ("n_check", "n_control", "n_check_inlier", "tps_n_control")
_CARRY_STR = ("check_status", "tps_status")


def _f(v):
    """Plain float, or None when the value is missing or non-finite."""
    if v is None:
        return None
    v = float(v)
    return v if np.isfinite(v) else None


def _i(v):
    """Plain int, or None when the value is missing or not a number."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError, OverflowError):
        # OverflowError is int(inf): a count that arrived non-finite is not a count.
        return None


def _apply(H, xy):
    """Push (N,2) points through a 3x3 homogeneous matrix."""
    p = np.hstack([xy, np.ones((len(xy), 1))]) @ np.asarray(H, dtype=np.float64).T
    w = np.where(np.abs(p[:, 2]) < 1e-12, np.nan, p[:, 2])
    return p[:, :2] / w[:, None]


def _gt_errors(H_est, gt_H, shape, warp=None):
    """Per-point true error in SOURCE pixels of the FULL delivered model, on a scene grid.

    For a scene point p the truth puts its reference position at gt_H(p), and the model's
    answer to "where in the source did that come from" is pullback(H_est, gt_H(p), warp) —
    the same function verify.py measures its own residuals with. The error is that minus p.

    With no spline the two-step evaluation collapses to the single composed matrix
    inv(H_est) @ gt_H, and that is the branch taken, so a projective run's gt_rmse_px is
    bit-for-bit what it was before this function learned about `warp` (test_io.py asserts
    it). When a spline shipped, the composed matrix measured only the 3x3 part of a model
    whose delivered raster included the spline: on the reviewer's run gt_rmse_px read
    3.2305 for a registration whose accepted TPS the number never touched.
    """
    h, w = int(shape[0]), int(shape[1])
    if h < 1 or w < 1:
        return None
    H_est = np.asarray(H_est, dtype=np.float64)
    gt_H = np.asarray(gt_H, dtype=np.float64)
    if H_est.shape != (3, 3) or gt_H.shape != (3, 3):
        return None
    if not (np.isfinite(H_est).all() and np.isfinite(gt_H).all()):
        return None
    try:
        E = np.linalg.inv(H_est) @ gt_H
    except np.linalg.LinAlgError:
        return None
    gx, gy = np.meshgrid(np.linspace(0, w - 1, _GT_GRID_N),
                        np.linspace(0, h - 1, _GT_GRID_N))
    pts = np.column_stack([gx.ravel(), gy.ravel()])
    if warp is None:
        err = _apply(E, pts) - pts
    else:
        est = pullback(H_est, _apply(gt_H, pts), warp)
        if est is None:
            return None
        err = est - pts
    err = err[np.isfinite(err).all(axis=1)]
    return err if len(err) else None


def compute_metrics(matches: MatchSet, registration: Registration, shape, grid_n=4,
                    mask=None, gt_H=None, runtime_s=0.0, aspect=True) -> dict:
    """Assemble the metrics.json dict: self-consistency, uniformity, sigma and (optional) truth."""
    n = len(np.asarray(matches.src_xy).reshape(-1, 2))

    inliers = np.asarray(getattr(registration, "inliers", None), dtype=bool).ravel()
    if inliers.size != n:
        inliers = np.zeros(n, dtype=bool)
    inlier_count = int(inliers.sum())

    # rmse_px: 2-D point RMSE, sqrt(mean(dx^2 + dy^2)), in SOURCE pixels.
    res = np.asarray(getattr(registration, "residuals", None), dtype=np.float64)
    if res.ndim != 2 or res.shape[1] != 2:
        res = np.zeros((0, 2))
    rmse = None
    if res.shape[0] == n and inlier_count > 0:
        r = res[inliers]
        r = r[np.isfinite(r).all(axis=1)]
        if len(r):
            rmse = float(np.sqrt(np.mean(np.sum(r * r, axis=1))))

    # sigma is NaN for points refine_matches declined to move; > 0 marks a real measurement.
    sigma = np.asarray(getattr(registration, "sigma", np.zeros(0)), dtype=np.float64).ravel()
    if sigma.size != n:
        sigma = np.full(n, np.nan)
    # NOT "points refined": refine.py returns NaN sigma for points it DID move when the
    # correlation sits above its calibrated range, so this counts points with a KNOWN
    # uncertainty. bench/calibrate.py measures the gap at 0% on fixture pairs but 36.6%
    # on clean same-scale synthetic ones. pipeline/stages.py knows which points actually
    # moved and overwrites refined_count with the true figure.
    refined = np.isfinite(sigma) & (sigma > 0)
    refined_inliers = refined & inliers
    mean_sigma = float(sigma[refined_inliers].mean()) if refined_inliers.any() else None

    uni = uniformity_report(matches, shape, grid_n, mask=mask, inliers=inliers,
                            aspect=aspect)

    inlier_ratio = _f(inlier_count / n) if n > 0 else None

    reg_metrics = getattr(registration, "metrics", None) or {}
    metrics = {
        "rmse_px": _f(rmse),
        "inlier_count": inlier_count,
        "inlier_ratio": inlier_ratio,
        # The plan's > 85% bar. Null when the ratio is unknown: no matches is not a fail,
        # it is an absence, and the two must not be reported as the same thing.
        "inlier_ratio_pass": (None if inlier_ratio is None
                              else bool(inlier_ratio >= _INLIER_RATIO_TARGET)),
        "inlier_ratio_target": _INLIER_RATIO_TARGET,
        "coverage_pct": _f(uni["coverage_pct"]),
        "dispersion_cv": _f(uni["dispersion_cv"]),
        # The plan's Spatial Distribution Index, derived from the two above.
        "sdi": _f(uni["sdi"]),
        "sdi_definition": uni["sdi_definition"],
        "grid_n": int(uni["grid_n"]),
        "grid_rows": int(uni["grid_rows"]),
        "grid_cols": int(uni["grid_cols"]),
        "runtime_s": _f(runtime_s),
        "match_count": int(n),
        "mean_sigma_px": _f(mean_sigma),
        "sigma_known_count": int(refined.sum()),
        # Overwritten by pipeline/stages.py, which can compare pre- and post-refinement
        # coordinates. None here means "nobody who could tell has said".
        "refined_count": None,
        "model_type": str(getattr(registration, "model_type", "") or "unknown"),
        # Model selection lives in verify.py; if it did not report its margin we say
        # so rather than inventing one that disagrees with the model it actually chose.
        "model_margin": _f(reg_metrics.get("model_margin")),
        "cell_counts": uni["counts"],
        "cell_states": uni["cell_states"],
        "gt_rmse_px": None,
        "gt_bias_x": None,
        "gt_bias_y": None,
        "gt_p90_px": None,
    }

    # The held-out and TPS numbers travel with the registration that produced them, so
    # they survive here whatever pipeline/stages.py chooses to copy back afterwards.
    # check_rmse_px is the accuracy figure to quote: rmse_px is in-sample, measured on
    # the fit's own inliers, and cannot fail the way a held-out number can.
    for key in _CARRY_FLOAT:
        metrics[key] = _f(reg_metrics.get(key))
    for key in _CARRY_INT:
        metrics[key] = _i(reg_metrics.get(key))
    for key in _CARRY_STR:
        value = reg_metrics.get(key)
        metrics[key] = None if value is None else str(value)
    # np.bool_ is not JSON-serialisable, and this dict is dumped unaided.
    tps_applied = reg_metrics.get("tps_applied")
    metrics["tps_applied"] = None if tps_applied is None else bool(tps_applied)

    if gt_H is not None:
        # The warp comes off the Registration, not off a parameter: the truth metric must
        # describe the model that was actually delivered, and a caller cannot forget to
        # hand it over.
        err = _gt_errors(getattr(registration, "params", None), gt_H, shape,
                         getattr(registration, "warp", None))
        if err is not None:
            d = np.hypot(err[:, 0], err[:, 1])
            metrics["gt_rmse_px"] = _f(np.sqrt(np.mean(d * d)))
            metrics["gt_bias_x"] = _f(err[:, 0].mean())
            metrics["gt_bias_y"] = _f(err[:, 1].mean())
            metrics["gt_p90_px"] = _f(np.percentile(d, 90))

    return metrics
