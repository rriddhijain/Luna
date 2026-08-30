"""Seat 6 · Pillar P4 — sub-pixel refinement of tie-point reference coordinates.

Each match gets a patch cut around the source point and a patch resampled from
the reference through the local linearisation of H, so the two live in a common
(source-pixel) frame before correlating.  Upsampled cross-correlation gives the
sub-pixel offset; a peak-quality gate rejects refinements that would move a good
point, and every accepted point carries its own uncertainty — or an explicit NaN where
the sigma calibration does not reach, which is not the same as being certain.
"""

import numpy as np
import cv2
from skimage.registration import phase_cross_correlation

from samanvay.types import MatchSet

# dtypes cv2.warpAffine handles natively (avoid copying a whole scene to cast)
_WARPABLE = (np.uint8, np.uint16, np.int16, np.float32, np.float64)

_MARGIN_PX = 2.0        # cubic interpolation needs a couple of pixels of room
_SIGMA_FLOOR_PX = 0.01  # even a noiseless peak is not exact
_SIGMA_CEIL_PX = 10.0

# Independent resolution cells inside a Hann-windowed patch, N_eff = k * W^2 * curvature.
# MEASURED, not reasoned — refit by bench/calibrate.py on 2026-08-29 at patch = 32
# (the pipeline default; the fit is per-patch-size).
#   Fit arm: the six fixtures/dsun_sweep pairs (Delta sun 0-50 deg), refined against the
#   analytic ground-truth homography in gt.json — 350 points. At k = 0.22 rms(sigma)
#   tracks the true per-axis RMS error to a geometric mean of 1.00, per-pair 0.86-1.24.
#   The old value 0.35 read 1.27x OPTIMISTIC on the same data (0.68-0.99).
#   Regime arm: 61 synthetic cases — 3 lunar terrains from synth/terrain.py x blur
#   1-3 px x noise 0.5-35%, displaced by an exact Fourier shift. At k = 0.22 sigma reads
#   1.16x PESSIMISTIC there (span 0.47-1.97). Those pairs have an identity Jacobian, so the
#   reference patch is never resampled through a scale change and carries less error than
#   the fixture arm's; the fixture arm is the deployed regime, so it sets the constant and
#   the residual bias is left on the safe side.
# Residual bias to carry with any quoted mean_sigma_px: +/- 25% on fixture-like pairs,
# up to 2x either way on clean same-scale pairs.
_NEFF_K = 0.22

# Above this peak correlation the model is unvalidated and sigma is NaN, not a number.
# Measured over 4362 samples with rho > 0.98: the observed error stops falling and floors
# near 0.12 px/axis on resampling and peak-estimator bias — which a correlation-response
# model cannot see — while the predicted sigma keeps dropping, reaching 1.7x to 4.5x
# optimistic. The floor is content-dependent (0.02 px on smooth Gaussian texture, 0.13 px
# on cratered terrain at 2 px blur), so there is no single number to substitute for it:
# the honest answer above this rho is "unknown". It costs little in practice — 0% of the
# refined points on the dsun_sweep fixtures land there, against 37% of the synthetic
# sweep, which deliberately reaches noise floors the pipeline never sees.
_RHO_MAX_VALIDATED = 0.99


def _as_array(img):
    """Accept a raw 2-D array or a CanonicalImage and return the pixel array."""
    a = np.asarray(getattr(img, "albedo", img))
    return a if a.ndim == 2 else a[..., 0]


def _jacobian(H, x, y):
    """2x2 local Jacobian of the projective map H at source point (x, y)."""
    w = H[2, 0] * x + H[2, 1] * y + H[2, 2]
    if not np.isfinite(w) or abs(w) < 1e-12:
        return None
    u = (H[0, 0] * x + H[0, 1] * y + H[0, 2]) / w
    v = (H[1, 0] * x + H[1, 1] * y + H[1, 2]) / w
    J = np.array([[H[0, 0] - u * H[2, 0], H[0, 1] - u * H[2, 1]],
                  [H[1, 0] - v * H[2, 0], H[1, 1] - v * H[2, 1]]], dtype=np.float64) / w
    if not np.isfinite(J).all() or abs(np.linalg.det(J)) < 1e-12:
        return None
    return J


def _sample_patch(img, cx, cy, J, w):
    """Resample a w x w patch centred on (cx, cy) with local frame J; None if out of bounds."""
    r = (w - 1) / 2.0
    offs = np.array([[-r, -r], [r, -r], [-r, r], [r, r]])
    corners = offs @ J.T + np.array([cx, cy])
    h_img, w_img = img.shape
    if (corners[:, 0].min() < _MARGIN_PX or corners[:, 1].min() < _MARGIN_PX
            or corners[:, 0].max() > w_img - 1 - _MARGIN_PX
            or corners[:, 1].max() > h_img - 1 - _MARGIN_PX):
        return None
    M = np.empty((2, 3), dtype=np.float64)
    M[:, :2] = J
    M[:, 2] = np.array([cx, cy]) - J @ np.array([r, r])
    return cv2.warpAffine(img, M, (w, w),
                          flags=cv2.INTER_CUBIC | cv2.WARP_INVERSE_MAP,
                          borderMode=cv2.BORDER_REFLECT_101)


def _prep(patch, win):
    """Mean-remove, window and validate a patch; None if non-finite or constant."""
    p = np.asarray(patch, dtype=np.float64)
    if not np.isfinite(p).all():
        return None
    p = (p - p.mean()) * win
    p -= p.mean()
    if p.std() < 1e-8:
        return None
    return p


def _cc_surface(a, b):
    """Circular normalised cross-correlation of two zero-mean patches, peak == 1 for a == b."""
    F = np.fft.rfft2(a) * np.conj(np.fft.rfft2(b))
    cc = np.fft.irfft2(F, a.shape) / (a.size * a.std() * b.std())
    return cc


def _peak_quality(cc):
    """Return (peak, ratio to the next local maximum, peak curvature per px^2)."""
    h, w = cc.shape
    py, px = np.unravel_index(int(np.argmax(cc)), cc.shape)
    peak = float(cc[py, px])
    ys = (np.arange(-3, 4) + py) % h
    xs = (np.arange(-3, 4) + px) % w
    other = cc.copy()
    other[np.ix_(ys, xs)] = -np.inf
    second = float(other.max())
    ratio = peak / max(second, 1e-3)
    cx = 2 * peak - cc[py, (px - 1) % w] - cc[py, (px + 1) % w]
    cy = 2 * peak - cc[(py - 1) % h, px] - cc[(py + 1) % h, px]
    return peak, ratio, float(max(0.5 * (cx + cy), 1e-3))


def _sigma(rho, curvature, patch):
    """Forstner-style scalar location uncertainty in source pixels."""
    # sigma^2 = (1 - rho^2) / (rho^2 * curvature * N_eff): peak SNR from the response
    # error, peak sharpness from the surface curvature, averaged over the independent
    # samples the patch actually contains.
    # ponytail: isotropic scalar, single calibration constant. Two ceilings. (1) No 2x2
    # covariance, so an elongated peak (a point on a linear rim, well-fixed across the
    # ridge and loose along it) reads as equally certain in both axes; upgrade path is
    # the inverse Hessian of the peak. (2) One constant cannot serve both error regimes:
    # it scales the noise term, and above _RHO_MAX_VALIDATED the error is a resampling
    # bias floor instead, so that whole regime is answered with NaN rather than an
    # optimistic number. Upgrade path is a second, measured term added in quadrature —
    # bench/calibrate.py already measures the floor per regime (0.02-0.13 px/axis); it is
    # not a constant, so it needs a predictor (peak curvature is the obvious candidate)
    # before it can replace the NaN.
    rho = min(max(rho, 1e-3), 1.0)
    if rho > _RHO_MAX_VALIDATED:
        return float("nan")           # outside the calibrated range: uncertainty unknown
    n_eff = max(_NEFF_K * patch * patch * curvature, 1.0)
    s = np.sqrt(max(1.0 - rho * rho, 0.0) / (rho * rho * curvature * n_eff))
    return float(np.clip(s, _SIGMA_FLOOR_PX, _SIGMA_CEIL_PX))


def refine_matches(matches: MatchSet, src_img, ref_img, H, config: dict = None) -> tuple:
    """Sub-pixel refine ref_xy by correlating H-aligned patches; returns (MatchSet, sigma)."""
    if config is None:
        config = {}
    patch = int(config.get("patch", 32))
    upsample = int(config.get("upsample", 100))
    min_peak = float(config.get("min_peak", 0.30))
    min_ratio = float(config.get("min_peak_ratio", 1.25))
    max_shift = float(config.get("max_shift_px", patch / 4.0))

    n = len(matches.src_xy)
    # sigma is NaN wherever the uncertainty is unknown — either the point was not refined,
    # or it was refined at a correlation above _RHO_MAX_VALIDATED, where the calibration
    # does not reach. Unknown is not zero and not infinite, and a NaN here does NOT mean
    # ref_xy was left alone. Consumers must use np.isfinite / np.nanmean.
    sigma = np.full(n, np.nan, dtype=np.float64)
    ref_xy = np.array(matches.ref_xy, dtype=np.float64).reshape(-1, 2).copy()
    out = MatchSet(src_xy=matches.src_xy, ref_xy=ref_xy, score=matches.score,
                   method=matches.method, cell=matches.cell)
    if n == 0 or patch < 8:
        return out, sigma

    Hm = np.asarray(H, dtype=np.float64) if H is not None else None
    if Hm is None or Hm.shape != (3, 3) or not np.isfinite(Hm).all():
        return out, sigma          # no usable geometry: honestly refine nothing

    src = _as_array(src_img)
    ref = _as_array(ref_img)
    if src.ndim != 2 or ref.ndim != 2 or src.size == 0 or ref.size == 0:
        return out, sigma
    if src.dtype.type not in _WARPABLE:
        src = src.astype(np.float32)
    if ref.dtype.type not in _WARPABLE:
        ref = ref.astype(np.float32)

    eye = np.eye(2)
    win = np.outer(np.hanning(patch), np.hanning(patch))
    src_xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)

    for i in range(n):
        sx, sy = src_xy[i]
        rx, ry = ref_xy[i]
        if not np.isfinite([sx, sy, rx, ry]).all():
            continue
        J = _jacobian(Hm, sx, sy)
        if J is None:
            continue
        a = _sample_patch(src, sx, sy, eye, patch)
        b = _sample_patch(ref, rx, ry, J, patch)
        if a is None or b is None:
            continue
        a = _prep(a, win)
        b = _prep(b, win)
        if a is None or b is None:
            continue

        cc = _cc_surface(a, b)
        peak, ratio, curv = _peak_quality(cc)
        if peak < min_peak or ratio < min_ratio:
            continue                      # weak or ambiguous: leave the point alone

        shift, err, _ = phase_cross_correlation(a, b, upsample_factor=upsample,
                                                normalization=None)
        if not np.isfinite(shift).all():
            continue
        # shift registers b onto a, so the reference content sits at -shift.
        d = np.array([-shift[1], -shift[0]], dtype=np.float64)
        if not np.hypot(*d) <= max_shift:
            continue                      # implausible jump (also catches NaN)

        ref_xy[i] = np.array([rx, ry]) + J @ d
        rho = np.sqrt(max(1.0 - float(err) ** 2, 0.0)) if np.isfinite(err) else peak
        sigma[i] = _sigma(rho, curv, patch)

    return out, sigma
