"""Seat 1 · Pillar P2 — quad-tree adaptive non-maximal suppression.

A per-cell quota filled by a plain score sort has no coordinate term at all, so
all K points can land in one textured corner of the cell and the grid buys
nothing WITHIN a cell. The quad-tree gives the quota to the cell's REGIONS
instead of to its points: every occupied quadrant is allotted an equal share,
recursively, and the share is spent on the best-scoring points down there.

That even allotment is the whole mechanism, and it is why the quota cannot be
re-collapsed by score at the end: on a cell where 90 of 100 candidates sit in
one corner, a score sort returns 8 points from 1 quadrant and this returns 2
from each of the 4.

Deterministic throughout: quadrants are visited in a fixed order, a remainder
goes to the fuller quadrant first, and the best point of a region is its highest
score at the lowest index. The same cell yields the same K points every run.
"""

import numpy as np


def _best_first(idx, score, m):
    """The m best of `idx` by score descending, ties broken by ascending index."""
    idx = np.asarray(idx, dtype=np.int64)
    order = np.lexsort((idx, -score[idx]))     # last key is primary
    return idx[order[:m]]


def _splittable(node, max_depth):
    """True when the node still has points to separate and a bbox to separate them in."""
    x0, y0, x1, y1, depth, idx = node
    if depth >= max_depth or len(idx) < 2:
        return False
    # At float resolution a midpoint can equal an edge; then that axis cannot split.
    return (x0 < 0.5 * (x0 + x1) < x1) or (y0 < 0.5 * (y0 + y1) < y1)


def _split(node, xy):
    """Four quadrants of the node bbox, empty ones dropped; the point set is partitioned."""
    x0, y0, x1, y1, depth, idx = node
    xm, ym = 0.5 * (x0 + x1), 0.5 * (y0 + y1)
    # >= puts a point on a boundary in the upper quadrant, and points outside the
    # bbox (a halo keypoint attributed to this cell) fall into the nearest one.
    right = xy[idx, 0] >= xm
    lower = xy[idx, 1] >= ym
    out = []
    for rx, ly, box in ((False, False, (x0, y0, xm, ym)),
                        (True, False, (xm, y0, x1, ym)),
                        (False, True, (x0, ym, xm, y1)),
                        (True, True, (xm, ym, x1, y1))):
        sel = idx[(right == rx) & (lower == ly)]
        if len(sel):
            out.append((box[0], box[1], box[2], box[3], depth + 1, sel))
    return out


def _allot(caps, quota):
    """`quota` units spread as evenly as possible over children, capped by each count.

    One unit at a time to the least-served child that still has room, so a quadrant
    holding 90 candidates gets no more of the quota than one holding 3 — that is the
    difference between this and the score sort. A remainder goes to the fuller
    quadrant first, then to the earlier one, so the allotment is a total order.
    ponytail: O(quota * 4) with quota a per-cell budget (50 by default). A heap is
    more code than the loop it would replace.
    """
    rank = {i: r for r, i in enumerate(sorted(range(len(caps)), key=lambda i: (-caps[i], i)))}
    out = [0] * len(caps)
    for _ in range(int(quota)):
        room = [i for i in range(len(caps)) if out[i] < caps[i]]
        if not room:
            break
        out[min(room, key=lambda i: (out[i], rank[i]))] += 1
    return out


def _collect(node, xy, score, quota, max_depth, out):
    """Append `quota` indices from this node, spread over its occupied quadrants."""
    idx = node[5]
    if quota <= 0:
        return
    if quota >= len(idx):
        # The quota does not bite down here: everything survives, and nothing is
        # dropped for being in a crowded region.
        out.extend(int(i) for i in idx)
        return
    if quota == 1 or not _splittable(node, max_depth):
        out.extend(int(i) for i in _best_first(idx, score, quota))
        return
    children = _split(node, xy)
    # A split can return a single child (every point on one side). Its quota is
    # unchanged and its box is half the size, so the recursion still converges on
    # the separation — max_depth is what terminates a pile of coincident points.
    shares = _allot([len(c[5]) for c in children], quota)
    for child, share in zip(children, shares):
        _collect(child, xy, score, share, max_depth, out)


def anms_quadtree(xy, score, k, bbox, max_depth=8) -> np.ndarray:
    """Indices of <= k points spread over `bbox`, the quota split evenly by quadrant.

    Returns indices INTO xy, sorted ascending, never duplicated. k <= 0 or
    len(xy) <= k returns everything: a quota that does not bite must not reorder
    or drop anything. Never raises — a degenerate bbox falls back to the score
    sort, which is exactly what the caller would have done without ANMS.
    """
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    n = len(xy)
    k = int(k)
    if k <= 0 or n <= k:
        return np.arange(n, dtype=np.int64)

    score = np.asarray(score, dtype=np.float64).ravel()
    if score.size != n:
        score = np.zeros(n, dtype=np.float64)
    # A non-finite score is not a reason to raise; it is the worst possible point.
    score = np.where(np.isfinite(score), score, -np.inf)

    box = np.asarray(bbox, dtype=np.float64).ravel()
    finite_xy = np.isfinite(xy).all(axis=1)
    if box.size != 4 or not np.isfinite(box).all() or box[2] <= box[0] or box[3] <= box[1] \
            or not finite_xy.any():
        return np.sort(_best_first(np.arange(n), score, k))

    # Points with no usable coordinate cannot be placed in the tree — a NaN compares
    # False against every midpoint and would silently land in one quadrant. They stay
    # eligible for the top-up below, so they are ranked but never mis-located.
    placed = np.flatnonzero(finite_xy)
    root = (float(box[0]), float(box[1]), float(box[2]), float(box[3]), 0, placed)

    kept = []
    _collect(root, xy, score, min(k, len(placed)), max_depth, kept)
    kept = np.asarray(kept, dtype=np.int64)

    if len(kept) < k:
        # Only reachable when some point had no usable coordinate. Dropping points the
        # quota allows would be a worse answer than the score sort, so top up.
        rest = np.setdiff1d(np.arange(n, dtype=np.int64), kept, assume_unique=False)
        kept = np.concatenate([kept, _best_first(rest, score, k - len(kept))])

    return np.sort(kept)
