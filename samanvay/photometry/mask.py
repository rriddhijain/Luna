"""Seat 2 · Pillar P1 — per-pixel validity mask.

Codes are frozen: 0=valid, 1=shadow, 2=nodata, 3=saturated.  Everything downstream
(detection, matching, uniformity accounting) reads this to know which pixels carry real
radiometry, so a wrong code here silently poisons the tie-point statistics.
"""

import numpy as np

MASK_VALID, MASK_SHADOW, MASK_NODATA, MASK_SATURATED = 0, 1, 2, 3

# Fraction of the observed dynamic range below which a pixel counts as "at the noise
# floor", i.e. shadowed rather than merely dark. Tunable: real detectors differ.
_DARK_FRAC = 0.02


def _ceiling(vals, dtype):
    """Saturation ceiling for this dtype, or None when it cannot be known."""
    if np.issubdtype(dtype, np.integer):
        return float(np.iinfo(dtype).max)
    if vals.size == 0:
        return None
    hi = float(vals.max())
    # Float imagery is either normalised to [0,1] or carries raw DN. Below 1.0 there is
    # no observable ceiling, so we decline to guess one and nothing is called saturated.
    return 1.0 if hi <= 1.0 else hi


def build_mask(array, illum=None, shadow=None, nodata_value=None, dark_frac=_DARK_FRAC):
    """Build the uint8 validity mask (0=valid 1=shadow 2=nodata 3=saturated).

    Shadow comes from the predicted shadow mask (`shadow`, or `illum <= 0` as returned by
    photometry.shading.predicted_illumination) OR from a near-noise-floor intensity test.
    Nodata comes from an explicit sentinel and from non-finite pixels.  Saturation comes
    from the dtype ceiling.  Precedence: nodata > saturated > shadow, because "no data" is
    the strongest statement available about a pixel.
    """
    array = np.asarray(array)
    out = np.full(array.shape, MASK_VALID, dtype=np.uint8)
    if array.size == 0:
        return out

    # --- nodata first: sentinels like -9999 would otherwise wreck every range estimate --
    finite = np.isfinite(array) if np.issubdtype(array.dtype, np.floating) \
        else np.ones(array.shape, dtype=bool)
    is_nodata = ~finite
    if nodata_value is not None and np.isfinite(float(nodata_value)):
        is_nodata |= array == nodata_value
    good = finite & ~is_nodata
    vals = array[good]

    # --- shadow --------------------------------------------------------------------
    is_shadow = np.zeros(array.shape, dtype=bool)
    if shadow is not None:
        is_shadow |= np.asarray(shadow, dtype=bool)
    if illum is not None:
        is_shadow |= np.asarray(illum, dtype=np.float32) <= 0.0
    if vals.size:
        lo, hi = float(vals.min()), float(vals.max())
        if hi > lo:      # a flat image has no noise floor worth speaking of
            is_shadow |= good & (array <= lo + dark_frac * (hi - lo))
    out[is_shadow] = MASK_SHADOW

    # --- saturated -----------------------------------------------------------------
    ceil = _ceiling(vals, array.dtype)
    if ceil is not None:
        out[good & (array >= ceil)] = MASK_SATURATED

    # --- nodata wins ---------------------------------------------------------------
    out[is_nodata] = MASK_NODATA
    return out
