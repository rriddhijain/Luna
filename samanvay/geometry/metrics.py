"""Seat 6 · Pillars P4/P5 — the metrics.json contract.

Nothing goes on a slide unless it came out of here. Every value is a plain
Python float/int/str/list so json.dump works unaided, and an undefined quantity
is emitted as null: a 0.0 RMSE computed from zero matches is a lie, not a score.
"""

import numpy as np

from samanvay.types import MatchSet, Registration
from samanvay.geometry.uniformity import uniformity_report

# Points at which the transform is compared against ground truth: a regular grid
# over the source extent, so gt_rmse_px measures the delivered transform over the
# whole scene and cannot be flattered by where the matches happened to land.
_GT_GRID_N = 9


def _f(v):
    """Plain float, or None when the value is missing or non-finite."""
    if v is None:
        return None
    v = float(v)
    return v if np.isfinite(v) else None


def _apply(H, xy):
    """Push (N,2) points through a 3x3 homogeneous matrix."""
    p = np.hstack([xy, np.ones((len(xy), 1))]) @ np.asarray(H, dtype=np.float64).T
    w = np.where(np.abs(p[:, 2]) < 1e-12, np.nan, p[:, 2])
    return p[:, :2] / w[:, None]


def _gt_errors(H_est, gt_H, shape):
    """Per-point true error in SOURCE pixels: inv(H_est) o gt_H applied to a scene grid."""
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
    err = _apply(E, pts) - pts
    err = err[np.isfinite(err).all(axis=1)]
    return err if len(err) else None


def compute_metrics(matches: MatchSet, registration: Registration, shape, grid_n=4,
                    mask=None, gt_H=None, runtime_s=0.0) -> dict:
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

    uni = uniformity_report(matches, shape, grid_n, mask=mask, inliers=inliers)

    reg_metrics = getattr(registration, "metrics", None) or {}
    metrics = {
        "rmse_px": _f(rmse),
        "inlier_count": inlier_count,
        "inlier_ratio": _f(inlier_count / n) if n > 0 else None,
        "coverage_pct": _f(uni["coverage_pct"]),
        "dispersion_cv": _f(uni["dispersion_cv"]),
        "grid_n": int(uni["grid_n"]),
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

    if gt_H is not None:
        err = _gt_errors(getattr(registration, "params", None), gt_H, shape)
        if err is not None:
            d = np.hypot(err[:, 0], err[:, 1])
            metrics["gt_rmse_px"] = _f(np.sqrt(np.mean(d * d)))
            metrics["gt_bias_x"] = _f(err[:, 0].mean())
            metrics["gt_bias_y"] = _f(err[:, 1].mean())
            metrics["gt_p90_px"] = _f(np.percentile(d, 90))

    return metrics
