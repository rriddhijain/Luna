"""
geometry/verify.py

OWNER: seat 6 (Geometry & Validation). Consumes MatchSet from seat 1.

Implements P2 "robust transform estimation": MAGSAC++ (cv2.USAC_MAGSAC)
over a model ladder -- similarity -> affine -> homography -- selecting the
simplest model whose RMSE is within a chosen margin of the best-fitting
model. Simpler models generalise better on sparse / noisy real data, so we
do not default straight to homography just because it is available.

Model-selection margin: 0.10 (10%) by default -- i.e. we accept a simpler
model if its RMSE is no more than 10% worse than the best RMSE achieved by
any model in the ladder. This number is a design decision owned by seat 6;
document it and be ready to justify it to a judge/reviewer.
"""

from __future__ import annotations

import time
from typing import Optional

import cv2
import numpy as np

from geometric_types import MatchSet, Registration

MODEL_LADDER = ("similarity", "affine", "homography")

# minimum matches required to even attempt each model (degrees of freedom)
MIN_POINTS = {
    "similarity": 2,
    "affine": 3,
    "homography": 4,
}

DEFAULT_REPROJ_THRESHOLD_PX = 3.0
DEFAULT_MODEL_MARGIN = 0.10
DEFAULT_MAGSAC_CONFIDENCE = 0.999
DEFAULT_MAGSAC_MAX_ITERS = 5000


def _to_3x3(model_type: str, M: np.ndarray) -> np.ndarray:
    """Normalise an OpenCV 2x3 (similarity/affine) or 3x3 (homography)
    matrix into a 3x3 homogeneous matrix."""
    if M is None:
        return None
    if model_type in ("similarity", "affine") and M.shape == (2, 3):
        out = np.eye(3)
        out[:2, :] = M
        return out
    return M


def _apply(M: np.ndarray, xy: np.ndarray) -> np.ndarray:
    n = xy.shape[0]
    homog = np.hstack([xy, np.ones((n, 1))])
    out = (M @ homog.T).T
    out = out[:, :2] / out[:, 2:3]
    return out


def _fit_model(
    model_type: str,
    src_xy: np.ndarray,
    ref_xy: np.ndarray,
    reproj_threshold_px: float,
    confidence: float,
    max_iters: int,
) -> tuple[Optional[np.ndarray], Optional[np.ndarray]]:
    """Fit one model from the ladder using MAGSAC++ where OpenCV supports
    it. Returns (M_3x3_or_None, inlier_mask_or_None)."""

    src = src_xy.astype(np.float32).reshape(-1, 1, 2)
    ref = ref_xy.astype(np.float32).reshape(-1, 1, 2)

    if model_type == "similarity":
        M, mask = cv2.estimateAffinePartial2D(
            src,
            ref,
            method=cv2.USAC_MAGSAC,
            ransacReprojThreshold=reproj_threshold_px,
            confidence=confidence,
            maxIters=max_iters,
        )
    elif model_type == "affine":
        M, mask = cv2.estimateAffine2D(
            src,
            ref,
            method=cv2.USAC_MAGSAC,
            ransacReprojThreshold=reproj_threshold_px,
            confidence=confidence,
            maxIters=max_iters,
        )
    elif model_type == "homography":
        M, mask = cv2.findHomography(
            src,
            ref,
            method=cv2.USAC_MAGSAC,
            ransacReprojThreshold=reproj_threshold_px,
            confidence=confidence,
            maxIters=max_iters,
        )
    else:
        raise ValueError(f"Unknown model_type: {model_type}")

    if M is None:
        return None, None

    mask = mask.astype(bool).reshape(-1)
    return _to_3x3(model_type, M), mask


def _rmse_on_inliers(
    M: np.ndarray, src_xy: np.ndarray, ref_xy: np.ndarray, inliers: np.ndarray
) -> float:
    if inliers.sum() == 0:
        return float("inf")
    pred = _apply(M, src_xy[inliers])
    resid = pred - ref_xy[inliers]
    return float(np.sqrt(np.mean(np.sum(resid**2, axis=1))))


def verify(
    matches: MatchSet,
    init_params: Optional[np.ndarray] = None,
    model_ladder: tuple = MODEL_LADDER,
    reproj_threshold_px: float = DEFAULT_REPROJ_THRESHOLD_PX,
    model_margin: float = DEFAULT_MODEL_MARGIN,
    confidence: float = DEFAULT_MAGSAC_CONFIDENCE,
    max_iters: int = DEFAULT_MAGSAC_MAX_ITERS,
) -> Registration:
    """Robustly estimate a source->reference transform from a MatchSet.

    Fits every model in `model_ladder` with MAGSAC++, then picks the
    SIMPLEST model whose RMSE (measured on its own inlier set) is within
    `model_margin` (relative) of the best RMSE achieved by any model.

    On failure (too few points, or every model fails to fit) returns a
    Registration with model_type="failed" and an explanatory metrics
    dict -- callers must check this rather than assuming success.
    """
    t0 = time.time()
    n = len(matches)

    results = {}
    for model_type in model_ladder:
        if n < MIN_POINTS[model_type]:
            continue
        try:
            M, inlier_mask = _fit_model(
                model_type,
                matches.src_xy,
                matches.ref_xy,
                reproj_threshold_px,
                confidence,
                max_iters,
            )
        except cv2.error:
            M, inlier_mask = None, None

        if M is None or inlier_mask is None or inlier_mask.sum() < MIN_POINTS[model_type]:
            continue

        rmse = _rmse_on_inliers(M, matches.src_xy, matches.ref_xy, inlier_mask)
        results[model_type] = {"M": M, "inliers": inlier_mask, "rmse": rmse}

    runtime_s = time.time() - t0

    if not results:
        return Registration(
            model_type="failed",
            params=np.eye(3),
            init_params=init_params,
            inliers=np.zeros(n, dtype=bool),
            residuals=np.zeros((n, 2)),
            sigma=np.zeros(n),
            metrics={
                "rmse_px": float("nan"),
                "inlier_count": 0,
                "inlier_ratio": 0.0,
                "runtime_s": runtime_s,
                "status": "failed_no_model_fit",
            },
        )

    best_rmse = min(r["rmse"] for r in results.values())
    chosen_type = None
    for model_type in model_ladder:  # ladder order = simplest first
        if model_type not in results:
            continue
        r = results[model_type]
        if r["rmse"] <= best_rmse * (1.0 + model_margin):
            chosen_type = model_type
            break
    if chosen_type is None:
        chosen_type = min(results, key=lambda k: results[k]["rmse"])

    chosen = results[chosen_type]
    M = chosen["M"]
    inliers = chosen["inliers"]

    pred = _apply(M, matches.src_xy)
    residuals = pred - matches.ref_xy  # NOTE: currently in reference pixels;
    # geometry/refine.py and metrics.py convert to source pixels using the
    # local scale of M where needed (see convert_residuals_to_source_px).

    metrics = {
        "rmse_px": chosen["rmse"],
        "inlier_count": int(inliers.sum()),
        "inlier_ratio": float(inliers.sum() / n) if n > 0 else 0.0,
        "runtime_s": runtime_s,
        "status": "ok",
        "model_margin_used": model_margin,
        "candidates": {k: round(v["rmse"], 4) for k, v in results.items()},
    }

    return Registration(
        model_type=chosen_type,
        params=M,
        init_params=init_params,
        inliers=inliers,
        residuals=residuals,
        sigma=np.full(n, np.nan),  # filled in by geometry/refine.py
        metrics=metrics,
    )


if __name__ == "__main__":
    # Smoke test using seat 6's own fake-match generator (no dependency on
    # seat 1's real matcher).
    from bench.fake_matches import generate_fake_matches

    matches, H_true = generate_fake_matches(
        n_points=300, noise_std=0.4, outlier_frac=0.25, seed=42
    )
    reg = verify(matches)
    print(f"Chosen model: {reg.model_type}")
    print(f"RMSE: {reg.metrics['rmse_px']:.4f} px (injected noise_std=0.4)")
    print(f"Inliers: {reg.metrics['inlier_count']} / {len(matches)}")
    print(f"Candidates tried: {reg.metrics['candidates']}")
