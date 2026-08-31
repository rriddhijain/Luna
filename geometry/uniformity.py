"""
geometry/uniformity.py

OWNER: seat 6 (Geometry & Validation). Consumes MatchSet from seat 1 and
the validity/shadow mask (CanonicalImage.mask) from seat 2.

Implements P5 "uniformity-enforced tie-point selection" -- specifically
the MEASUREMENT side: grid assignment, coverage %, and a dispersion index
(coefficient of variation of per-cell inlier counts). ENFORCEMENT (min/max
quotas applied during matching) lives in seat 1's matcher, per the design
agreed in the seat1<->seat6 D3 pairing session -- this module defines the
grid and the metric that enforcement is judged against, and can also be
used as a post-hoc filter/report before enforcement is wired in upstream.

Three cell states are tracked, and must not be conflated:
  - POPULATED:            >=1 inlier match landed in this cell
  - INSUFFICIENT_TEXTURE:  0 inliers, but the cell is mostly VALID pixels
                            (the matcher tried and failed / found nothing
                            distinctive -- an honest gap)
  - MASKED:                the cell is mostly invalid pixels (shadow,
                            nodata, saturated) per seat 2's mask -- it
                            should NOT count against the uniformity score,
                            since there was nothing matchable there.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CELL_POPULATED = 0
CELL_INSUFFICIENT_TEXTURE = 1
CELL_MASKED = 2

CELL_STATE_NAMES = {
    CELL_POPULATED: "populated",
    CELL_INSUFFICIENT_TEXTURE: "insufficient_texture",
    CELL_MASKED: "masked",
}


@dataclass
class UniformityReport:
    grid_n: int
    cell_counts: np.ndarray  # (grid_n, grid_n) int, inlier count per cell
    cell_states: np.ndarray  # (grid_n, grid_n) int, one of CELL_* constants
    coverage_pct: float  # % of NON-MASKED cells that are POPULATED
    dispersion_cv: float  # coefficient of variation of counts over non-masked cells
    n_masked_cells: int
    n_insufficient_texture_cells: int
    n_populated_cells: int

    def as_dict(self) -> dict:
        return {
            "grid_n": self.grid_n,
            "coverage_pct": self.coverage_pct,
            "dispersion_cv": self.dispersion_cv,
            "n_masked_cells": self.n_masked_cells,
            "n_insufficient_texture_cells": self.n_insufficient_texture_cells,
            "n_populated_cells": self.n_populated_cells,
        }


def assign_cells(
    xy: np.ndarray, image_shape: tuple[int, int], grid_n: int
) -> np.ndarray:
    """Assign each (x, y) point to a flattened cell id in an grid_n x grid_n
    grid over the image. Points outside the image bounds are clipped into
    the nearest edge cell rather than dropped.

    Returns an (N,) int32 array of cell ids in [0, grid_n*grid_n).
    """
    h, w = image_shape
    x = np.clip(xy[:, 0], 0, w - 1e-6)
    y = np.clip(xy[:, 1], 0, h - 1e-6)

    col = np.clip((x / w * grid_n).astype(np.int32), 0, grid_n - 1)
    row = np.clip((y / h * grid_n).astype(np.int32), 0, grid_n - 1)

    return row * grid_n + col


def _mask_fraction_per_cell(
    mask: np.ndarray | None, image_shape: tuple[int, int], grid_n: int
) -> np.ndarray:
    """Compute, per grid cell, the fraction of pixels that are INVALID
    (mask != 0, i.e. shadow/nodata/saturated). Returns (grid_n, grid_n).
    If mask is None, returns all zeros (nothing is masked)."""
    h, w = image_shape
    if mask is None:
        return np.zeros((grid_n, grid_n))

    invalid = (mask != 0).astype(np.float64)
    frac = np.zeros((grid_n, grid_n))
    row_edges = np.linspace(0, h, grid_n + 1).astype(int)
    col_edges = np.linspace(0, w, grid_n + 1).astype(int)
    for r in range(grid_n):
        for c in range(grid_n):
            block = invalid[row_edges[r] : row_edges[r + 1], col_edges[c] : col_edges[c + 1]]
            frac[r, c] = block.mean() if block.size > 0 else 1.0
    return frac


def compute_uniformity(
    src_xy: np.ndarray,
    inliers: np.ndarray,
    image_shape: tuple[int, int],
    grid_n: int = 8,
    validity_mask: np.ndarray | None = None,
    masked_cell_threshold: float = 0.5,
) -> UniformityReport:
    """Compute the uniformity report for a set of inlier match locations.

    Parameters
    ----------
    src_xy : (N,2) all match locations (source pixel coords)
    inliers : (N,) bool, which of src_xy are inliers (only inliers count
        toward coverage -- outliers should not inflate the score)
    image_shape : (h, w) of the source image
    grid_n : grid is grid_n x grid_n
    validity_mask : optional (h,w) uint8 mask from seat 2
        (0=valid, 1=shadow, 2=nodata, 3=saturated)
    masked_cell_threshold : a cell is classified MASKED if more than this
        fraction of its pixels are invalid

    Returns
    -------
    UniformityReport
    """
    inlier_xy = src_xy[inliers]
    cell_ids = assign_cells(inlier_xy, image_shape, grid_n) if len(inlier_xy) else np.array([], dtype=np.int32)

    cell_counts = np.zeros((grid_n, grid_n), dtype=np.int64)
    if len(cell_ids):
        rows, counts = np.unique(cell_ids, return_counts=True)
        for cid, cnt in zip(rows, counts, strict=True):
            r, c = divmod(int(cid), grid_n)
            cell_counts[r, c] = cnt

    mask_frac = _mask_fraction_per_cell(validity_mask, image_shape, grid_n)
    is_masked = mask_frac > masked_cell_threshold

    cell_states = np.full((grid_n, grid_n), CELL_INSUFFICIENT_TEXTURE, dtype=np.int64)
    cell_states[is_masked] = CELL_MASKED
    cell_states[(cell_counts > 0) & (~is_masked)] = CELL_POPULATED

    non_masked = ~is_masked
    n_non_masked = int(non_masked.sum())
    n_populated = int(((cell_states == CELL_POPULATED)).sum())
    n_insufficient = int(((cell_states == CELL_INSUFFICIENT_TEXTURE)).sum())
    n_masked = int(is_masked.sum())

    coverage_pct = 100.0 * n_populated / n_non_masked if n_non_masked > 0 else 0.0

    non_masked_counts = cell_counts[non_masked]
    if len(non_masked_counts) > 0 and non_masked_counts.mean() > 0:
        dispersion_cv = float(non_masked_counts.std() / non_masked_counts.mean())
    else:
        dispersion_cv = float("nan")

    return UniformityReport(
        grid_n=grid_n,
        cell_counts=cell_counts,
        cell_states=cell_states,
        coverage_pct=coverage_pct,
        dispersion_cv=dispersion_cv,
        n_masked_cells=n_masked,
        n_insufficient_texture_cells=n_insufficient,
        n_populated_cells=n_populated,
    )


if __name__ == "__main__":
    rng = np.random.default_rng(0)
    image_shape = (1000, 1000)

    # Case A: matches clustered in one corner (bad uniformity)
    clustered_xy = rng.uniform(low=[0, 0], high=[200, 200], size=(150, 2))
    inliers_a = np.ones(150, dtype=bool)
    report_a = compute_uniformity(clustered_xy, inliers_a, image_shape, grid_n=8)
    print("Clustered matches:")
    print(f"  coverage={report_a.coverage_pct:.1f}%  dispersion_cv={report_a.dispersion_cv:.3f}")

    # Case B: matches spread evenly (good uniformity)
    spread_xy = rng.uniform(low=[0, 0], high=[1000, 1000], size=(150, 2))
    inliers_b = np.ones(150, dtype=bool)
    report_b = compute_uniformity(spread_xy, inliers_b, image_shape, grid_n=8)
    print("Spread matches:")
    print(f"  coverage={report_b.coverage_pct:.1f}%  dispersion_cv={report_b.dispersion_cv:.3f}")

    # Case C: spread matches, but with a masked (shadowed) region seat 2
    # would have flagged -- confirm the masked region does not get scored
    # as a coverage failure.
    mask = np.zeros(image_shape, dtype=np.uint8)
    mask[:300, :300] = 1  # top-left 300x300 block is shadow
    report_c = compute_uniformity(spread_xy, inliers_b, image_shape, grid_n=8, validity_mask=mask)
    print("Spread matches with a masked shadow region:")
    print(f"  coverage={report_c.coverage_pct:.1f}%  dispersion_cv={report_c.dispersion_cv:.3f}")
    print(f"  masked_cells={report_c.n_masked_cells}  populated={report_c.n_populated_cells}  insufficient={report_c.n_insufficient_texture_cells}")
