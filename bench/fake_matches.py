"""
bench/fake_matches.py

OWNER: seat 6 (Geometry & Validation).

Generates a synthetic MatchSet from a known homography, with configurable
Gaussian noise and an outlier fraction. This is the D0 "never-blocked" stub
that lets seat 6 build and test geometry/verify.py, geometry/refine.py and
geometry/uniformity.py before seat 1's real matcher exists.

It is also the harness used later for the canonical accuracy self-check:
run verify() on output from this function and confirm the recovered RMSE
is close to the injected noise_std.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np

from samanvay.types import MatchSet


def random_homography(
    image_shape: Tuple[int, int] = (1024, 1024),
    max_rotation_deg: float = 8.0,
    scale_range: Tuple[float, float] = (0.5, 2.0),
    max_translation_frac: float = 0.05,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Build a random-but-plausible 3x3 homography (rotation + anisotropic
    scale + small perspective wobble + translation), used as ground truth
    for synthetic pairs.

    Returns a 3x3 matrix mapping SOURCE pixel -> REFERENCE pixel.
    """
    rng = rng or np.random.default_rng()
    h, w = image_shape

    theta = np.deg2rad(rng.uniform(-max_rotation_deg, max_rotation_deg))
    sx = rng.uniform(*scale_range)
    sy = rng.uniform(*scale_range) if rng.random() < 0.3 else sx  # mostly isotropic
    tx = rng.uniform(-max_translation_frac, max_translation_frac) * w
    ty = rng.uniform(-max_translation_frac, max_translation_frac) * h

    cos_t, sin_t = np.cos(theta), np.sin(theta)
    # rotation + anisotropic scale about the image centre, then translate
    cx, cy = w / 2.0, h / 2.0
    R = np.array(
        [
            [sx * cos_t, -sy * sin_t, 0.0],
            [sx * sin_t, sy * cos_t, 0.0],
            [0.0, 0.0, 1.0],
        ]
    )
    T_to_origin = np.array([[1, 0, -cx], [0, 1, -cy], [0, 0, 1]], dtype=float)
    T_back = np.array([[1, 0, cx + tx], [0, 1, cy + ty], [0, 0, 1]], dtype=float)

    H = T_back @ R @ T_to_origin

    # tiny perspective wobble so the homography model is occasionally the
    # correct one to select (not just affine)
    if rng.random() < 0.3:
        H[2, 0] += rng.uniform(-1e-5, 1e-5)
        H[2, 1] += rng.uniform(-1e-5, 1e-5)

    return H


def apply_homography(H: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Apply a 3x3 homogeneous transform to an (N,2) array of points."""
    n = xy.shape[0]
    homog = np.hstack([xy, np.ones((n, 1))])
    out = (H @ homog.T).T
    out = out[:, :2] / out[:, 2:3]
    return out


def generate_fake_matches(
    n_points: int = 200,
    image_shape: Tuple[int, int] = (1024, 1024),
    homography: np.ndarray | None = None,
    noise_std: float = 0.3,
    outlier_frac: float = 0.2,
    outlier_magnitude: float = 150.0,
    seed: int | None = 0,
    num_points: int | None = None,
    outlier_fraction: float | None = None,
) -> Tuple[MatchSet, np.ndarray]:
    """Generate a synthetic MatchSet plus the ground-truth homography used
    to build it.

    Parameters
    ----------
    n_points : number of putative matches to generate
    image_shape : (h, w) of the (imaginary) source image
    homography : ground-truth 3x3 transform; a random one is built if None
    noise_std : Gaussian noise (in reference pixels) added to inlier matches
    outlier_frac : fraction of points replaced with random, unrelated matches
    outlier_magnitude : how far (in pixels) outliers deviate from the true
        correspondence
    seed : RNG seed for reproducibility (None = nondeterministic)

    Returns
    -------
    (matches, H_true)
    """
    if num_points is not None:
        n_points = num_points
    if outlier_fraction is not None:
        outlier_frac = outlier_fraction

    rng = np.random.default_rng(seed)
    h, w = image_shape

    if homography is None:
        homography = random_homography(image_shape, rng=rng)

    src_xy = rng.uniform(low=[0, 0], high=[w, h], size=(n_points, 2))
    ref_xy_true = apply_homography(homography, src_xy)

    ref_xy = ref_xy_true + rng.normal(scale=noise_std, size=ref_xy_true.shape)

    n_outliers = int(round(outlier_frac * n_points))
    if n_outliers > 0:
        idx = rng.choice(n_points, size=n_outliers, replace=False)
        offsets = rng.normal(scale=outlier_magnitude, size=(n_outliers, 2))
        # ensure outliers are actually far from the true match (not
        # accidentally close due to small random offsets)
        offsets = offsets + np.sign(offsets) * outlier_magnitude * 0.5
        ref_xy[idx] = ref_xy_true[idx] + offsets

    score = rng.uniform(0.5, 1.0, size=n_points).astype(np.float32)
    method = np.zeros(n_points, dtype=np.uint8)
    cell = -np.ones(n_points, dtype=np.int32)

    matches = MatchSet(
        src_xy=src_xy, ref_xy=ref_xy, score=score, method=method, cell=cell
    )
    return matches, homography


if __name__ == "__main__":
    # Quick smoke test when run directly: python -m bench.fake_matches
    matches, H = generate_fake_matches(n_points=300, noise_std=0.4, outlier_frac=0.25)
    print(f"Generated {len(matches.src_xy)} fake matches")
    print("Ground-truth homography:\n", H)
    print("src_xy sample:\n", matches.src_xy[:3])
    print("ref_xy sample:\n", matches.ref_xy[:3])

