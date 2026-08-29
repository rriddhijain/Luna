"""
geometry/init.py

OWNER: seat 6 (Geometry & Validation). Consumes geotransforms/metadata
produced by seat 3 (data/backend).

Implements P2 "metadata-first geometric initialisation": both source and
reference products carry an approximate geolocation (GDAL-style
geotransform). We compose them into an initial source -> reference
transform so that fine matching only has to search a small residual
window (the cross-mission geodetic offset), instead of searching blind
across the whole image.

GDAL geotransform convention (6-tuple GT):
    Xgeo = GT[0] + Xpixel * GT[1] + Yline * GT[2]
    Ygeo = GT[3] + Xpixel * GT[4] + Yline * GT[5]

i.e. pixel -> world is the affine matrix:
    [[GT1, GT2, GT0],
     [GT4, GT5, GT3],
     [0,   0,   1  ]]
"""

from __future__ import annotations

from typing import Sequence, Tuple

import numpy as np


def geotransform_to_matrix(geotransform: Sequence[float]) -> np.ndarray:
    """Convert a GDAL 6-tuple geotransform into a 3x3 homogeneous matrix
    mapping PIXEL -> WORLD coordinates."""
    if len(geotransform) != 6:
        raise ValueError(f"geotransform must have 6 elements, got {len(geotransform)}")
    gt0, gt1, gt2, gt3, gt4, gt5 = geotransform
    return np.array(
        [
            [gt1, gt2, gt0],
            [gt4, gt5, gt3],
            [0.0, 0.0, 1.0],
        ]
    )


def coarse_init(
    src_geotransform: Sequence[float],
    ref_geotransform: Sequence[float],
    src_crs: str | None = None,
    ref_crs: str | None = None,
) -> np.ndarray:
    """Compose two geotransforms into an initial SOURCE pixel -> REFERENCE
    pixel transform.

    NOTE: this assumes src_crs == ref_crs (both already in the same
    selenographic / projected frame). If they differ, seat 3 must
    reproject one of the products before calling this — cross-CRS
    reprojection is explicitly out of scope for this function.

    Parameters
    ----------
    src_geotransform, ref_geotransform : GDAL 6-tuples
    src_crs, ref_crs : optional CRS strings, used only for a sanity check

    Returns
    -------
    3x3 homogeneous matrix, source pixel -> reference pixel
    """
    if src_crs is not None and ref_crs is not None and src_crs != ref_crs:
        raise ValueError(
            f"CRS mismatch: src={src_crs!r} ref={ref_crs!r}. "
            "Reproject one product onto the other's CRS before calling "
            "coarse_init (this is seat 3's responsibility)."
        )

    src_pixel_to_world = geotransform_to_matrix(src_geotransform)
    ref_pixel_to_world = geotransform_to_matrix(ref_geotransform)
    ref_world_to_pixel = np.linalg.inv(ref_pixel_to_world)

    # source pixel -> world -> reference pixel
    src_to_ref = ref_world_to_pixel @ src_pixel_to_world
    return src_to_ref


def search_window_from_offset(
    init_params: np.ndarray,
    src_shape: Tuple[int, int],
    uncertainty_px: float = 50.0,
) -> Tuple[float, float, float, float]:
    """Given a coarse init transform and the source image shape, return a
    (min_x, min_y, max_x, max_y) bounding box in REFERENCE pixel space to
    restrict the fine-matching search to, allowing for a geodetic offset
    uncertainty margin.

    `uncertainty_px` is expressed in reference pixels and should be set
    generously (e.g. the known worst-case cross-mission geolocation error,
    or a config default like 50-100 px) since coarse init is only
    approximate.
    """
    h, w = src_shape
    corners = np.array([[0, 0], [w, 0], [0, h], [w, h]], dtype=float)
    homog = np.hstack([corners, np.ones((4, 1))])
    projected = (init_params @ homog.T).T
    projected = projected[:, :2] / projected[:, 2:3]

    min_xy = projected.min(axis=0) - uncertainty_px
    max_xy = projected.max(axis=0) + uncertainty_px
    return float(min_xy[0]), float(min_xy[1]), float(max_xy[0]), float(max_xy[1])


if __name__ == "__main__":
    # Smoke test: two overlapping but offset/rotated geotransforms
    src_gt = (500000.0, 0.25, 0.0, 2000000.0, 0.0, -0.25)  # 0.25 m/px, OHRC-like
    ref_gt = (499998.0, 0.5, 0.0, 2000003.0, 0.0, -0.5)  # 0.5 m/px, NAC-like, offset

    M = coarse_init(src_gt, ref_gt)
    print("Coarse init source->reference matrix:\n", M)

    window = search_window_from_offset(M, src_shape=(2048, 2048), uncertainty_px=80)
    print("Search window in reference pixels (min_x, min_y, max_x, max_y):", window)
