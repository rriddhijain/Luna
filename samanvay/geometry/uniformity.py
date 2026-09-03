"""Seat 6 · Pillar P5 — tie-point uniformity over the source image.

Coverage is only an honest number if a cell you correctly declined to match
(shadow, nodata) is dropped from the denominator instead of counted as a miss.
So every cell lands in exactly one of three states and the two failure states
are kept strictly apart.

The grid follows the image aspect: `grid_shape` is the ONLY place the grid
shape is computed, here or anywhere else. A 888x11952 NAC strip on a fixed 4x4
grid gets 13:1 cells, and coverage_pct is then measured over a partition nobody
would defend.
"""

import numpy as np

from samanvay.types import MatchSet

# Mask codes are frozen: 0 = valid, 1 = shadow, 2 = nodata, 3 = saturated.
_MASK_VALID = 0
# A cell is written off only when most of it is unusable.
_MASKED_FRACTION = 0.5
# Ceiling on the long axis: at 1:200 the aspect rule would ask for 800 cells of
# ~15 px each, which measures noise rather than distribution.
_MAX_LONG_CELLS = 64

POPULATED = "populated"
INSUFFICIENT_TEXTURE = "insufficient_texture"
MASKED_INVALID = "masked_invalid"

# The jury-facing scalar. It is a convenience over coverage_pct and dispersion_cv,
# NOT a replacement: it cannot say whether a low score came from unreached cells or
# from clumping, and cell_states is the only place a declined cell is visible.
SDI_DEFINITION = "sdi = (coverage_pct/100) * 1/(1 + dispersion_cv)"


def grid_shape(shape, grid_n, aspect=True) -> tuple:
    """(rows, cols) of the uniformity grid over an image of `shape`.

    grid_n counts cells along the SHORT axis; the long axis is scaled by the
    aspect ratio and rounded so cells stay near-square. aspect=False forces
    grid_n x grid_n, which is what a square image gets either way.
    """
    grid_n = max(1, int(grid_n))
    h, w = int(shape[0]), int(shape[1])
    if not aspect or h <= 0 or w <= 0:
        return grid_n, grid_n
    long_n = int(round(grid_n * max(h, w) / float(min(h, w))))
    long_n = int(min(_MAX_LONG_CELLS, max(1, long_n)))
    return (grid_n, long_n) if w >= h else (long_n, grid_n)


def assign_cells(xy, shape, grid_n, aspect=True) -> np.ndarray:
    """Grid cell id (col + cols*row) for each (x, y) point over an image of `shape`."""
    rows, cols = grid_shape(shape, grid_n, aspect)
    h, w = int(shape[0]), int(shape[1])
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    if len(xy) == 0 or h <= 0 or w <= 0:
        return np.zeros(len(xy), dtype=np.int32)
    x = np.nan_to_num(xy[:, 0], nan=0.0, posinf=w - 1, neginf=0.0)
    y = np.nan_to_num(xy[:, 1], nan=0.0, posinf=h - 1, neginf=0.0)
    col = np.clip((x * cols / w).astype(np.int64), 0, cols - 1)
    row = np.clip((y * rows / h).astype(np.int64), 0, rows - 1)
    return (col + cols * row).astype(np.int32)


def _masked_cells(mask, rows, cols):
    """Boolean (rows*cols,) — True where most of the cell is shadow/nodata/saturated."""
    flags = np.zeros(int(rows) * int(cols), dtype=bool)
    if mask is None:
        return flags
    m = np.asarray(mask)
    if m.ndim != 2 or m.size == 0:
        return flags
    # The grid is fractional, so a mask at a different resolution still splits
    # into the same rows x cols cells — no resampling, no silent misalignment.
    ye = np.round(np.linspace(0, m.shape[0], rows + 1)).astype(np.int64)
    xe = np.round(np.linspace(0, m.shape[1], cols + 1)).astype(np.int64)
    invalid = m != _MASK_VALID
    for row in range(rows):
        for col in range(cols):
            block = invalid[ye[row]:ye[row + 1], xe[col]:xe[col + 1]]
            if block.size and block.mean() > _MASKED_FRACTION:
                flags[col + cols * row] = True
    return flags


def spatial_distribution_index(coverage_pct, dispersion_cv):
    """The SDI scalar, or None when either input is undefined.

    Both factors are needed and neither is sufficient: coverage alone passes a set
    that reaches every cell with wildly uneven counts, dispersion alone passes a set
    that is perfectly even across the three cells it managed to reach.
    """
    if coverage_pct is None or dispersion_cv is None:
        return None
    sdi = (float(coverage_pct) / 100.0) * (1.0 / (1.0 + float(dispersion_cv)))
    return float(sdi) if np.isfinite(sdi) else None


def uniformity_report(matches: MatchSet, shape, grid_n, mask=None, inliers=None,
                      aspect=True) -> dict:
    """Per-cell counts and states, coverage_pct, dispersion_cv and sdi over non-masked cells."""
    grid_n = max(1, int(grid_n))
    rows, cols = grid_shape(shape, grid_n, aspect)
    total = rows * cols

    xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    if inliers is not None:
        sel = np.asarray(inliers, dtype=bool).ravel()
        xy = xy[sel] if len(sel) == len(xy) else xy[:0]

    cells = assign_cells(xy, shape, grid_n, aspect)
    counts = np.bincount(cells, minlength=total)[:total]
    masked = _masked_cells(mask, rows, cols)

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
        # The plan's Spatial Distribution Index, derived from the two numbers above so
        # it can never disagree with them. Null when either is undefined, never 0.0.
        # populated == 0 is the one case the two inputs cannot express between them: a
        # single live cell reports dispersion_cv 0.0 by the convention above (one cell
        # is trivially uniform), so a grid_n=1 run with NO matches would otherwise score
        # a flat 0.0 rather than admitting it measured nothing.
        "sdi": (spatial_distribution_index(coverage_pct, dispersion_cv)
                if populated else None),
        "sdi_definition": SDI_DEFINITION,
        # grid_n stays the SHORT-axis count so nothing downstream that reads it breaks;
        # grid_rows/grid_cols are what counts and cell_states are actually shaped by.
        "grid_n": int(grid_n),
        "grid_rows": int(rows),
        "grid_cols": int(cols),
        "counts": [int(c) for c in counts],
        "cell_states": [str(s) for s in states],
    }
