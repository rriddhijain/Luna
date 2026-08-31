"""
geometric_types.py

Canonical dataclasses for geometric verification, matching, and pipeline stages.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Product:
    """Produced by seat 3 (data/backend). Consumed by everyone."""

    path: str
    array: np.ndarray  # or a tiled/memmap reader in the real pipeline
    meta: dict = field(default_factory=dict)
    # meta is expected to contain (when available):
    #   product_id, instrument, gsd_m, sun_az_deg, sun_el_deg,
    #   incidence_deg, emission_deg, geotransform (GDAL 6-tuple),
    #   crs, shape, dtype


@dataclass
class CanonicalImage:
    """Produced by seat 2 (physics/photometry). Consumed by seat 1 (matcher)
    and by seat 6 (uniformity masking)."""

    albedo: np.ndarray  # float32, illumination divided out
    pc: np.ndarray  # float32 in [0,1], phase congruency
    pc_orient: np.ndarray  # float32, dominant local orientation
    mask: np.ndarray  # uint8: 0=valid 1=shadow 2=nodata 3=saturated
    params: dict = field(default_factory=dict)


@dataclass
class MatchSet:
    """Produced by seat 1 (matcher). Consumed by seat 6 (verification) and
    rendered by seat 4 (viewer)."""

    src_xy: np.ndarray  # (N,2) float64
    ref_xy: np.ndarray  # (N,2) float64
    score: np.ndarray  # (N,) float32
    method: np.ndarray  # (N,) uint8 -- which path found it (L0/L1/L2/L3)
    cell: np.ndarray  # (N,) int32 -- uniformity grid cell id (-1 if unset)

    def __post_init__(self) -> None:
        n = len(self.src_xy)
        assert self.ref_xy.shape == (n, 2), "src_xy/ref_xy length mismatch"
        if self.score is None or len(self.score) == 0:
            self.score = np.ones(n, dtype=np.float32)
        if self.method is None or len(self.method) == 0:
            self.method = np.zeros(n, dtype=np.uint8)
        if self.cell is None or len(self.cell) == 0:
            self.cell = -np.ones(n, dtype=np.int32)

    def __len__(self) -> int:
        return len(self.src_xy)


@dataclass
class Registration:
    """Produced by seat 6 (geometry/validation). Consumed by seat 3 (warp
    and export) and seat 4 (viewer)."""

    model_type: str  # "similarity" | "affine" | "homography"
    params: np.ndarray  # 3x3, source -> reference
    init_params: np.ndarray | None = None  # 3x3, coarse init used
    inliers: np.ndarray | None = None  # (N,) bool
    residuals: np.ndarray | None = None  # (N,2) float64, source pixels
    sigma: np.ndarray | None = None  # (N,) float64, per-point uncertainty
    metrics: dict = field(default_factory=dict)
    # metrics is expected to contain (when available):
    #   rmse_px, inlier_count, inlier_ratio, coverage_pct,
    #   dispersion_cv, grid_n, runtime_s


__all__ = ["CanonicalImage", "MatchSet", "Product", "Registration"]
