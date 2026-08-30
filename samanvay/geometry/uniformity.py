"""Seat 6 · Pillar P5 — tie-point uniformity over the source image.

Coverage is only an honest number if a cell you correctly declined to match
(shadow, nodata) is dropped from the denominator instead of counted as a miss.
So every cell lands in exactly one of three states and the two failure states
are kept strictly apart.
"""

import numpy as np

from samanvay.types import MatchSet

# Mask codes are frozen: 0 = valid, 1 = shadow, 2 = nodata, 3 = saturated.
_MASK_VALID = 0
# A cell is written off only when most of it is unusable.
_MASKED_FRACTION = 0.5

POPULATED = "populated"
INSUFFICIENT_TEXTURE = "insufficient_texture"
MASKED_INVALID = "masked_invalid"


def assign_cells(xy, shape, grid_n) -> np.ndarray:
    """Grid cell id (col + grid_n*row) for each (x, y) point over an image of `shape`."""
    grid_n = max(1, int(grid_n))
    h, w = int(shape[0]), int(shape[1])
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    if len(xy) == 0 or h <= 0 or w <= 0:
        return np.zeros(len(xy), dtype=np.int32)
    x = np.nan_to_num(xy[:, 0], nan=0.0, posinf=w - 1, neginf=0.0)
    y = np.nan_to_num(xy[:, 1], nan=0.0, posinf=h - 1, neginf=0.0)
    col = np.clip((x * grid_n / w).astype(np.int64), 0, grid_n - 1)
    row = np.clip((y * grid_n / h).astype(np.int64), 0, grid_n - 1)
    return (col + grid_n * row).astype(np.int32)


def _masked_cells(mask, grid_n):
    """Boolean (grid_n**2,) — True where most of the cell is shadow/nodata/saturated."""
    flags = np.zeros(grid_n * grid_n, dtype=bool)
    if mask is None:
        return flags
    m = np.asarray(mask)
    if m.ndim != 2 or m.size == 0:
        return flags
    # The grid is fractional, so a mask at a different resolution still splits
    # into the same grid_n x grid_n cells — no resampling, no silent misalignment.
    ye = np.round(np.linspace(0, m.shape[0], grid_n + 1)).astype(np.int64)
    xe = np.round(np.linspace(0, m.shape[1], grid_n + 1)).astype(np.int64)
    invalid = m != _MASK_VALID
    for row in range(grid_n):
        for col in range(grid_n):
            block = invalid[ye[row]:ye[row + 1], xe[col]:xe[col + 1]]
            if block.size and block.mean() > _MASKED_FRACTION:
                flags[col + grid_n * row] = True
    return flags


def uniformity_report(matches: MatchSet, shape, grid_n, mask=None, inliers=None) -> dict:
    """Per-cell counts and states, coverage_pct and dispersion_cv over non-masked cells."""
    grid_n = max(1, int(grid_n))
    total = grid_n * grid_n

    xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    if inliers is not None:
        sel = np.asarray(inliers, dtype=bool).ravel()
        xy = xy[sel] if len(sel) == len(xy) else xy[:0]

    cells = assign_cells(xy, shape, grid_n)
    counts = np.bincount(cells, minlength=total)[:total]
    masked = _masked_cells(mask, grid_n)

    # A cell we actually matched in is covered, even if it is mostly masked:
    # we plainly did not decline it, so it stays in both numerator and denominator.
    states = np.where(counts > 0, POPULATED,
                      np.where(masked, MASKED_INVALID, INSUFFICIENT_TEXTURE))
    counted = states != MASKED_INVALID
    denom = int(counted.sum())
    populated = int((states == POPULATED).sum())

    coverage_pct = 100.0 * populated / denom if denom > 0 else None

    live = counts[counted].astype(np.float64)
    if denom == 0:
        dispersion_cv = None            # every cell written off: undefined, not 0.0
    elif denom == 1:
        dispersion_cv = 0.0             # a single cell is trivially uniform
    elif live.mean() <= 0:
        dispersion_cv = None            # no matches anywhere: undefined, not 0.0
    else:
        dispersion_cv = float(live.std() / live.mean())

    return {
        "coverage_pct": coverage_pct,
        "dispersion_cv": dispersion_cv,
        "grid_n": int(grid_n),
        "counts": [int(c) for c in counts],
        "cell_states": [str(s) for s in states],
    }
