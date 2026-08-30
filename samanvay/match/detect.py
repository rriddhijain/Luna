"""Seat 1 (matching) · pillar P1 — keypoint detection.

`sift` and `orb` are the L0/L1 baselines and are handed straight to OpenCV.
`l2` / `rift` detect on the phase-congruency map instead of on intensity, because
PC is contrast- and brightness-invariant by construction and therefore survives the
sun-angle difference that is the whole problem statement.

Two things the previous PC detector got wrong, both fixed here:

* the response floor was a fixed fraction of the map maximum, so a single bright
  crater rim raised the bar for the entire rest of the image and starved every other
  tile of keypoints. It is now a PERCENTILE of the positive PC responses, which is
  scale-free and does not care what the brightest structure in the frame is doing.
* local maxima came from a bare `maximum_filter` equality test, which admits whole
  plateaus and clusters. Peaks now carry an enforced minimum separation, so keypoints
  spread out instead of piling onto one feature — that is also what pillar P5
  (uniform tie-point distribution) is asking for.
"""

import cv2
import numpy as np
from skimage.feature import peak_local_max

# Percentile of the positive PC responses a peak must clear. 70 keeps the upper
# third of structured pixels and still leaves low-contrast terrain represented.
_RESPONSE_PCTL = 70.0
_MIN_SEPARATION = 6      # px between accepted peaks
_MAX_KEYPOINTS = 2000


def _feature_map(img, pc_map):
    """PC map when we have one, otherwise gradient magnitude as an honest stand-in."""
    if pc_map is not None and np.any(pc_map):
        fm = np.asarray(pc_map, dtype=np.float32)
        if fm.shape == np.shape(img)[:2]:
            return np.nan_to_num(fm, nan=0.0, posinf=0.0, neginf=0.0)
    # ponytail: gradient magnitude is NOT illumination invariant, so this fallback
    # gives up the property the L2 arm exists to demonstrate. Ceiling: the arm
    # degrades to an L0-like detector on any tile with no PC. Upgrade path: compute
    # phase congruency here instead of falling back to Sobel.
    img_f = np.asarray(img, dtype=np.float32)
    dx = cv2.Sobel(img_f, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(img_f, cv2.CV_32F, 0, 1, ksize=3)
    return np.hypot(dx, dy)


def detect_keypoints(
    img: np.ndarray,
    method: str = "sift",
    pc_map: np.ndarray = None,
    min_separation: int = _MIN_SEPARATION,
    response_percentile: float = _RESPONSE_PCTL,
    max_keypoints: int = _MAX_KEYPOINTS,
) -> list:
    """Detect keypoints; `l2`/`rift` peak-pick the phase-congruency map with NMS."""
    method = str(method).lower()

    if method in ("sift", "orb"):
        detector = cv2.SIFT_create() if method == "sift" else cv2.ORB_create(nfeatures=2000)
        return detector.detect(img, None)

    if method not in ("l2", "rift"):
        raise ValueError(f"Unknown detection method: {method}")

    fm = _feature_map(img, pc_map)
    if fm.size == 0 or not np.any(fm > 0):
        return []                       # flat or empty tile: no keypoints, no invention

    positive = fm[fm > 0]
    thresh = float(np.percentile(positive, float(response_percentile)))
    if not np.isfinite(thresh) or thresh <= 0:
        thresh = float(positive.min())
    # peak_local_max compares STRICTLY greater than threshold_abs. On a quantised or
    # saturated response map (a synthetic checkerboard, a clipped tile) the percentile
    # lands exactly on the maximum and every peak is then silently excluded. Step the bar
    # one tick below the top of the map. The tick has to be taken in the map's own float32
    # or NumPy's weak scalar promotion rounds it straight back up to the maximum.
    ceiling = np.nextafter(np.float32(positive.max()), np.float32(-np.inf))
    thresh = float(min(np.float32(thresh), ceiling))

    sep = max(1, int(min_separation))
    peaks = peak_local_max(
        fm,
        min_distance=sep,
        threshold_abs=thresh,
        num_peaks=max(1, int(max_keypoints)),   # finite => spacing is actually enforced
        exclude_border=sep,
    )
    if len(peaks) == 0:
        return []

    # peak_local_max returns (row, col) strongest-first; keypoints are (x, y).
    return [cv2.KeyPoint(x=float(c), y=float(r), size=float(2 * sep + 1),
                         response=float(fm[r, c]))
            for r, c in peaks]
