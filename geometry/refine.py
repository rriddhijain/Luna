"""
geometry/refine.py

OWNER: seat 6 (Geometry & Validation). Consumes images from seat 3 /
CanonicalImage.albedo from seat 2, and a Registration from verify.py.

Implements P4 "sub-pixel refinement": for every inlier tie-point, extract
a local patch from the source and reference images (source patch warped
into the reference frame at the current estimate), and use phase
correlation with DFT upsampling (Guizar-Sicairos et al., 2008 --
skimage.registration.phase_cross_correlation, upsample_factor=100) to
refine the match to sub-pixel precision. A per-point uncertainty (sigma)
is estimated from the correlation peak sharpness, and low-quality peaks
are rejected rather than trusted.

This module has two independent entry points:
  - refine_point_subpixel(...)  operates on a single pair of patches, and
    is unit-testable with pure synthetic image patches (no full pipeline
    needed).
  - refine_registration(...)    operates on a full Registration + a pair
    of images, looping over all inliers.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional, Tuple

import numpy as np
from skimage.registration import phase_cross_correlation

from geometric_types import MatchSet, Registration

DEFAULT_PATCH_RADIUS = 24  # patch is (2*radius+1) x (2*radius+1)
DEFAULT_UPSAMPLE_FACTOR = 100
# reject a refinement if the correlation error metric exceeds this --
# phase_cross_correlation's normalized RMS error, higher = worse match
DEFAULT_MAX_ERROR = 0.6


def _extract_patch(
    image: np.ndarray, center_xy: Tuple[float, float], radius: int
) -> Optional[np.ndarray]:
    """Extract a (2r+1, 2r+1) patch centred (to the nearest pixel) on
    center_xy=(x,y). Returns None if the patch would fall outside the
    image bounds."""
    x, y = center_xy
    cx, cy = int(round(x)), int(round(y))
    h, w = image.shape[:2]
    x0, x1 = cx - radius, cx + radius + 1
    y0, y1 = cy - radius, cy + radius + 1
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h:
        return None
    return image[y0:y1, x0:x1]


def refine_point_subpixel(
    src_patch: np.ndarray,
    ref_patch: np.ndarray,
    upsample_factor: int = DEFAULT_UPSAMPLE_FACTOR,
    max_error: float = DEFAULT_MAX_ERROR,
) -> Tuple[Optional[np.ndarray], Optional[float], float]:
    """Refine a single correspondence given two same-shape patches, one
    centred on the (integer-pixel) source point warped into the reference
    frame, one centred on the current reference-side estimate.

    Returns
    -------
    (shift_xy, sigma, error)
        shift_xy : the sub-pixel (dx, dy) correction to apply, in the
            *reference* patch's pixel units, or None if rejected.
        sigma : a rough per-point uncertainty estimate (pixels), derived
            from the correlation error, or None if rejected.
        error : phase_cross_correlation's normalized RMS error (always
            returned, even on rejection, for diagnostics).
    """
    if src_patch.shape != ref_patch.shape:
        raise ValueError("src_patch and ref_patch must have the same shape")

    src_f = src_patch.astype(np.float64)
    ref_f = ref_patch.astype(np.float64)

    # phase_cross_correlation returns (row_shift, col_shift) i.e. (dy, dx)
    # of ref relative to src.
    shift_yx, error, _phasediff = phase_cross_correlation(
        ref_f, src_f, upsample_factor=upsample_factor, normalization=None
    )
    dy, dx = shift_yx

    if error > max_error or not np.isfinite(error):
        return None, None, float(error)

    # Heuristic uncertainty: scale linearly with normalized error, floored
    # at a small epsilon so a "perfect" correlation doesn't report zero
    # uncertainty. This is intentionally simple and documented as such --
    # it is not a formal Cramer-Rao bound.
    sigma = float(max(0.02, error) * 1.0)

    return np.array([dx, dy]), sigma, float(error)


def refine_registration(
    registration: Registration,
    matches: MatchSet,
    src_image: np.ndarray,
    ref_image: np.ndarray,
    patch_radius: int = DEFAULT_PATCH_RADIUS,
    upsample_factor: int = DEFAULT_UPSAMPLE_FACTOR,
    max_error: float = DEFAULT_MAX_ERROR,
) -> Registration:
    """Refine every inlier of `registration` to sub-pixel accuracy.

    For each inlier i:
      1. Predict its reference location with the current transform.
      2. Extract a patch from src_image around the source point, and a
         patch from ref_image around the predicted reference location.
      3. Run phase correlation to get a sub-pixel correction.
      4. Update residuals[i] and sigma[i]; points whose patches fall out
         of bounds or whose correlation is low-quality are marked with
         NaN sigma and left out of the sub-pixel-refined RMSE.

    Returns a NEW Registration (does not mutate the input) with updated
    `residuals`, `sigma`, and `metrics["rmse_px_subpixel"]`.
    """
    n = len(matches)
    residuals = registration.residuals.copy() if registration.residuals is not None else np.zeros((n, 2))
    sigma = np.full(n, np.nan)

    inliers = registration.inliers
    if inliers is None:
        inliers = np.zeros(n, dtype=bool)

    refined_count = 0
    refined_sq_errors = []

    for i in np.where(inliers)[0]:
        src_xy = matches.src_xy[i]
        ref_xy_pred = _apply_homog(registration.params, src_xy)

        src_patch = _extract_patch(src_image, tuple(src_xy), patch_radius)
        ref_patch = _extract_patch(ref_image, tuple(ref_xy_pred), patch_radius)

        if src_patch is None or ref_patch is None:
            continue

        shift_xy, point_sigma, _err = refine_point_subpixel(
            src_patch, ref_patch, upsample_factor=upsample_factor, max_error=max_error
        )
        if shift_xy is None:
            continue

        refined_ref_xy = ref_xy_pred + shift_xy
        residuals[i] = refined_ref_xy - matches.ref_xy[i]
        sigma[i] = point_sigma
        refined_count += 1
        refined_sq_errors.append(float(np.sum((refined_ref_xy - matches.ref_xy[i]) ** 2)))

    new_metrics = dict(registration.metrics)
    new_metrics["subpixel_refined_count"] = refined_count
    new_metrics["subpixel_refine_rate"] = (
        refined_count / int(inliers.sum()) if inliers.sum() > 0 else 0.0
    )
    if refined_sq_errors:
        new_metrics["rmse_px_subpixel"] = float(np.sqrt(np.mean(refined_sq_errors)))
    else:
        new_metrics["rmse_px_subpixel"] = float("nan")

    return replace(registration, residuals=residuals, sigma=sigma, metrics=new_metrics)


def _apply_homog(M: np.ndarray, xy: np.ndarray) -> np.ndarray:
    homog = np.array([xy[0], xy[1], 1.0])
    out = M @ homog
    return out[:2] / out[2]


if __name__ == "__main__":
    # Smoke test 1: pure synthetic patches with a known sub-pixel shift.
    rng = np.random.default_rng(0)
    base = rng.normal(size=(96, 96)).astype(np.float64)
    from scipy.ndimage import shift as nd_shift

    true_dx, true_dy = 1.37, -0.62
    shifted = nd_shift(base, shift=(true_dy, true_dx), order=3, mode="reflect")

    r = 24
    c = 48
    src_patch = base[c - r : c + r + 1, c - r : c + r + 1]
    ref_patch = shifted[c - r : c + r + 1, c - r : c + r + 1]

    shift_xy, sigma, err = refine_point_subpixel(src_patch, ref_patch)
    print(f"Injected shift: dx={true_dx}, dy={true_dy}")
    print(f"Recovered shift: dx={shift_xy[0]:.3f}, dy={shift_xy[1]:.3f}  (sigma={sigma:.4f}, err={err:.4f})")

    # Smoke test 2: full refine_registration on a synthetic image pair.
    # Use a smoothly-textured image (not white noise) -- phase correlation
    # relies on real structure/frequency content, just like a real lunar
    # image patch would provide, and white noise is an unrealistically
    # hard case. We evaluate the refined position against the TRUE
    # (noise-free) warped position, not against the noisy detector match
    # -- that noisy match is exactly what refinement is meant to correct.
    print("\n--- full refine_registration smoke test ---")
    from scipy.ndimage import gaussian_filter
    from bench.fake_matches import generate_fake_matches, apply_homography
    from geometry.verify import verify
    import cv2

    img_shape = (512, 512)
    src_img = gaussian_filter(rng.normal(size=img_shape), sigma=2.0)
    H_true = np.array([[1.02, 0.0, 3.4], [0.0, 1.02, -2.1], [0.0, 0.0, 1.0]])
    ref_img = cv2.warpPerspective(src_img, H_true, (img_shape[1], img_shape[0]))

    matches, _ = generate_fake_matches(
        n_points=150, image_shape=img_shape, homography=H_true, noise_std=1.5,
        outlier_frac=0.1, seed=1,
    )
    reg = verify(matches)
    reg_refined = refine_registration(reg, matches, src_img, ref_img, max_error=1.5)

    # Ground-truth check: for each refined point, recover the ACTUAL
    # refined reference-frame position (predicted position + residual,
    # since residuals = predicted - detected) and compare it against the
    # true, noise-free warp of the source point. This is the fair way to
    # judge sub-pixel refinement: did it move us closer to the truth than
    # the noisy detector match was?
    refined_mask = ~np.isnan(reg_refined.sigma)
    true_ref_xy = apply_homography(H_true, matches.src_xy)

    # refine_registration defines residuals[i] = refined_ref_xy - matches.ref_xy[i]
    # so the actual refined reference-frame position is:
    refined_ref_xy = matches.ref_xy + reg_refined.residuals

    err_before = np.sqrt(np.mean(np.sum((matches.ref_xy[refined_mask] - true_ref_xy[refined_mask]) ** 2, axis=1)))
    err_after = np.sqrt(np.mean(np.sum((refined_ref_xy[refined_mask] - true_ref_xy[refined_mask]) ** 2, axis=1)))

    print(f"Refined {refined_mask.sum()} / {reg.metrics['inlier_count']} inliers (injected detector noise_std=1.5 px)")
    print(f"RMSE vs ground truth BEFORE refinement: {err_before:.4f} px")
    print(f"RMSE vs ground truth AFTER  refinement: {err_after:.4f} px")
