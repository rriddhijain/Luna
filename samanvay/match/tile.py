"""Seat ① (matching) x Seat ⑥ (geometry) — interaction I6, pillar P2 (uniformity).

Tiled matching driven by the coarse init: the SOURCE is partitioned into a
grid_n x grid_n grid, and each source tile is matched against the reference
window it actually projects into under `init` (source -> reference), not
against the reference tile with the same grid index. Per-cell quotas are
enforced *during* matching by relaxing the ratio threshold, and every cell
reports what happened to it so uniformity_report can distinguish
"insufficient_texture" from "never attempted".
"""

import logging

import cv2
import numpy as np

from samanvay.types import CanonicalImage, MatchSet
from samanvay.match.detect import detect_keypoints
from samanvay.match.describe import describe_keypoints

try:
    from samanvay.geometry.init import project_box
except ImportError:  # geometry/init.py is owned by the geom-init seat.
    def project_box(H, x0, y0, x1, y1):
        """Axis-aligned bbox of the four corners of (x0,y0,x1,y1) pushed through H."""
        corners = np.array([[x0, y0, 1.0], [x1, y0, 1.0], [x1, y1, 1.0], [x0, y1, 1.0]]).T
        p = np.asarray(H, dtype=np.float64) @ corners
        w = np.where(np.abs(p[2]) < 1e-12, 1e-12, p[2])
        xs, ys = p[0] / w, p[1] / w
        return float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())

LOG = logging.getLogger(__name__)

_METHOD_ID = {"sift": 0, "orb": 1, "l2": 2, "rift": 2}   # "rift" is the same RIFT path as "l2"

# Expected coarse-init positioning error, as a fraction of the projected tile size.
# ponytail: a fixed fraction stands in for a real init covariance — the ceiling is
# that a badly-scaled init silently drops out of the window instead of widening it.
# Upgrade path: have coarse_init report its own residual and size the margin from it.
_INIT_ERROR_FRAC = 0.25
_MIN_MARGIN_PX = 16.0


def _edges(size: int, n: int) -> np.ndarray:
    """Grid boundaries that partition [0, size) into n half-open, gapless spans."""
    return np.round(np.linspace(0, size, n + 1)).astype(np.int64)


def _as_u8(albedo: np.ndarray) -> np.ndarray:
    """Canonical [0,1] albedo to the uint8 image the OpenCV detectors want."""
    return (np.clip(albedo, 0.0, 1.0) * 255).astype(np.uint8)


def _describe(img_u8, pc, pc_orient, method):
    """Detect and describe once; returns (points (N,2) float64, descriptors)."""
    kps = detect_keypoints(img_u8, method=method, pc_map=pc)
    kps, desc = describe_keypoints(img_u8, kps, method=method, pc_orient=pc_orient)
    if len(kps) == 0 or desc is None or desc.size == 0:
        return np.zeros((0, 2)), None
    return np.array([k.pt for k in kps], dtype=np.float64), desc


def _knn_pairs(desc_src, desc_ref, norm):
    """One BFMatcher pass: per source descriptor, its 1st/2nd reference distances."""
    matcher = cv2.BFMatcher(norm)
    raw = matcher.knnMatch(desc_src, desc_ref, k=2 if len(desc_ref) >= 2 else 1)
    q, t, d1, d2 = [], [], [], []
    for m in raw:
        if not m:
            continue
        q.append(m[0].queryIdx)
        t.append(m[0].trainIdx)
        d1.append(m[0].distance)
        # Only one candidate exists: no ratio test is possible, so it always passes.
        d2.append(m[1].distance if len(m) > 1 else np.inf)
    return (np.array(q, dtype=np.int64), np.array(t, dtype=np.int64),
            np.array(d1, dtype=np.float64), np.array(d2, dtype=np.float64))


def _empty_matchset() -> MatchSet:
    """A MatchSet with no matches — the honest answer for a degenerate pair."""
    return MatchSet(
        src_xy=np.zeros((0, 2)),
        ref_xy=np.zeros((0, 2)),
        score=np.zeros((0,), dtype=np.float32),
        method=np.zeros((0,), dtype=np.uint8),
        cell=np.zeros((0,), dtype=np.int32),
    )


def match_tiled(
    source: CanonicalImage,
    reference: CanonicalImage,
    grid_n: int = 4,
    halo_px: int = 64,
    config: dict = None,
    cell_budgets: dict = None,
    init: np.ndarray = None,
) -> tuple:
    """Match source tiles against the reference windows they project into; returns (MatchSet, cell_info)."""
    config = dict(config or {})
    cell_budgets = cell_budgets or {}

    src_h, src_w = source.albedo.shape[:2]
    ref_h, ref_w = reference.albedo.shape[:2]

    method = str(config.get("method", "sift")).lower()
    if method in ("l2", "rift") and not np.any(source.pc):
        # Config asked for the phase-congruency path but there is no PC to run it on.
        method = str(config.get("l2_fallback_method", "sift")).lower()
    norm = cv2.NORM_HAMMING if method == "orb" else cv2.NORM_L2
    method_id = _METHOD_ID.get(method, 0)

    base_ratio = float(config.get("ratio_threshold", 0.9 if method in ("l2", "rift") else 0.75))
    relax_attempts = max(1, int(config.get("relax_attempts", 4)))
    relax_step = float(config.get("relax_ratio_step", 0.05))
    ratio_ceiling = float(config.get("ratio_ceiling", 0.95))
    max_masked_frac = float(config.get("max_masked_frac", 0.5))
    margin_cfg = config.get("search_margin_px")

    degraded = init is None
    if degraded:
        LOG.warning(
            "match_tiled: no init supplied — falling back to same-grid tiling, which "
            "assumes source and reference already share a pixel grid and scale. "
            "DEGRADED MODE, not for demo."
        )
    else:
        init = np.asarray(init, dtype=np.float64).reshape(3, 3)

    info = {
        "mode": "degraded_same_grid" if degraded else "init_projected",
        "grid_n": int(grid_n),
        "method": method,
        "search_margin_px": margin_cfg,   # None => derived per cell
        "cells": {},
    }

    if grid_n < 1 or src_h == 0 or src_w == 0 or ref_h == 0 or ref_w == 0:
        return _empty_matchset(), info

    sy_e, sx_e = _edges(src_h, grid_n), _edges(src_w, grid_n)
    ry_e, rx_e = _edges(ref_h, grid_n), _edges(ref_w, grid_n)

    out_src, out_ref, out_score, out_method, out_cell = [], [], [], [], []

    for row in range(grid_n):
        for col in range(grid_n):
            cell_id = col + grid_n * row
            budget = cell_budgets.get(cell_id, {})
            min_matches = int(budget.get("min_matches", 5))
            max_matches = int(budget.get("max_matches", 50))

            # Core: the span this cell owns exclusively. Cores partition the source,
            # so a match is attributed to exactly one cell and never double-counted.
            cy0, cy1 = int(sy_e[row]), int(sy_e[row + 1])
            cx0, cx1 = int(sx_e[col]), int(sx_e[col + 1])

            cell = {
                "status": "empty_tile",
                "attempts": 0,
                "ratio_threshold": None,   # None = never attempted, not "0.75 by default"
                "count": 0,
                "masked_frac": None,
                "src_core": (cx0, cy0, cx1, cy1),
                "ref_window": None,
                "search_margin_px": None,
            }
            info["cells"][cell_id] = cell

            if cy1 <= cy0 or cx1 <= cx0:
                continue

            core_mask = source.mask[cy0:cy1, cx0:cx1]
            masked_frac = float(np.count_nonzero(core_mask)) / float(core_mask.size)
            cell["masked_frac"] = masked_frac
            if masked_frac > max_masked_frac:
                # Shadow/nodata: not worth matching, and excluded from the coverage denominator.
                cell["status"] = "masked_invalid"
                continue

            # Source tile with halo.
            sy0, sy1 = max(0, cy0 - halo_px), min(src_h, cy1 + halo_px)
            sx0, sx1 = max(0, cx0 - halo_px), min(src_w, cx1 + halo_px)

            if degraded:
                ry0 = max(0, int(ry_e[row]) - halo_px)
                ry1 = min(ref_h, int(ry_e[row + 1]) + halo_px)
                rx0 = max(0, int(rx_e[col]) - halo_px)
                rx1 = min(ref_w, int(rx_e[col + 1]) + halo_px)
            else:
                px0, py0, px1, py1 = project_box(init, sx0, sy0, sx1, sy1)
                if not np.all(np.isfinite([px0, py0, px1, py1])):
                    cell["status"] = "no_reference_overlap"
                    continue
                margin = (float(margin_cfg) if margin_cfg is not None else
                          max(_MIN_MARGIN_PX, _INIT_ERROR_FRAC * max(px1 - px0, py1 - py0)))
                cell["search_margin_px"] = margin
                rx0, rx1 = int(np.floor(px0 - margin)), int(np.ceil(px1 + margin))
                ry0, ry1 = int(np.floor(py0 - margin)), int(np.ceil(py1 + margin))
                rx0, rx1 = max(0, rx0), min(ref_w, rx1)
                ry0, ry1 = max(0, ry0), min(ref_h, ry1)

            if rx1 <= rx0 or ry1 <= ry0:
                cell["status"] = "no_reference_overlap"
                continue
            cell["ref_window"] = (rx0, ry0, rx1, ry1)

            # Detect + describe ONCE per tile; relaxation below re-filters, never re-detects.
            sp, desc_s = _describe(_as_u8(source.albedo[sy0:sy1, sx0:sx1]),
                                   source.pc[sy0:sy1, sx0:sx1],
                                   source.pc_orient[sy0:sy1, sx0:sx1], method)
            rp, desc_r = _describe(_as_u8(reference.albedo[ry0:ry1, rx0:rx1]),
                                   reference.pc[ry0:ry1, rx0:rx1],
                                   reference.pc_orient[ry0:ry1, rx0:rx1], method)
            if desc_s is None or desc_r is None:
                cell["status"] = "insufficient_texture"
                continue

            sp = sp + np.array([sx0, sy0], dtype=np.float64)
            rp = rp + np.array([rx0, ry0], dtype=np.float64)

            q, t, d1, d2 = _knn_pairs(desc_s, desc_r, norm)
            if q.size == 0:
                cell["status"] = "insufficient_texture"
                continue

            cand_src, cand_ref = sp[q], rp[t]

            # Attribute to this cell only if the SOURCE point sits in the core, and
            # drop points that fall on masked source pixels.
            keep_base = (
                (cand_src[:, 0] >= cx0) & (cand_src[:, 0] < cx1) &
                (cand_src[:, 1] >= cy0) & (cand_src[:, 1] < cy1)
            )
            mx = np.clip(np.round(cand_src[:, 0]).astype(np.int64), 0, src_w - 1)
            my = np.clip(np.round(cand_src[:, 1]).astype(np.int64), 0, src_h - 1)
            keep_base &= source.mask[my, mx] == 0

            keep = np.zeros(q.size, dtype=bool)
            thresh = base_ratio
            for attempt in range(relax_attempts):
                thresh = min(ratio_ceiling, base_ratio + attempt * relax_step)
                keep = keep_base & ((d1 == 0) | (d1 < thresh * d2))
                cell["attempts"] = attempt + 1
                cell["ratio_threshold"] = thresh
                if int(keep.sum()) >= min_matches or thresh >= ratio_ceiling:
                    break

            k_src, k_ref = cand_src[keep], cand_ref[keep]
            k_d1, k_d2 = d1[keep], d2[keep]
            k_score = np.where(np.isfinite(k_d2), 1.0 - k_d1 / (k_d2 + 1e-6), 1.0).astype(np.float32)

            if len(k_src) > max_matches:
                top = np.argsort(k_score)[::-1][:max_matches]
                k_src, k_ref, k_score = k_src[top], k_ref[top], k_score[top]

            cell["count"] = int(len(k_src))
            cell["status"] = "populated" if len(k_src) else "insufficient_texture"
            if len(k_src) == 0:
                continue

            out_src.append(k_src)
            out_ref.append(k_ref)
            out_score.append(k_score)
            out_method.append(np.full(len(k_src), method_id, dtype=np.uint8))
            out_cell.append(np.full(len(k_src), cell_id, dtype=np.int32))

    if not out_src:
        return _empty_matchset(), info

    src_xy, ref_xy = np.vstack(out_src), np.vstack(out_ref)
    score = np.concatenate(out_score)
    method_arr, cell_arr = np.concatenate(out_method), np.concatenate(out_cell)

    # One tie-point per (source, reference) LOCATION. SIFT and ORB emit several keypoints
    # at the same pixel — one per dominant orientation — so a single correspondence comes
    # back several times: identical coordinates, different descriptor score. Counting them
    # as independent inliers inflates `inlier_count` and therefore `redundancy`, and
    # `rmse_trustworthy` is judged on redundancy. Measured at 3.6x on a real cross-mission
    # pair, where 100 reported inliers were 28 distinct points and the run still claimed
    # rmse_trustworthy=true. Keep the best-scoring row per location.
    order = np.argsort(score, kind="stable")[::-1]
    _, first = np.unique(np.hstack([src_xy, ref_xy])[order], axis=0, return_index=True)
    keep = np.sort(order[first])
    src_xy, ref_xy = src_xy[keep], ref_xy[keep]
    score, method_arr, cell_arr = score[keep], method_arr[keep], cell_arr[keep]

    # cell["count"] was recorded before the dedup, so make info describe what is returned.
    # Only cells the matcher actually attempted are touched: masked_invalid must survive,
    # because a cell correctly declined is not a cell that found nothing.
    for cell_id, cell in info["cells"].items():
        if cell.get("status") in ("populated", "insufficient_texture"):
            n = int(np.count_nonzero(cell_arr == cell_id))
            cell["count"] = n
            cell["status"] = "populated" if n else "insufficient_texture"

    return MatchSet(src_xy=src_xy, ref_xy=ref_xy, score=score,
                    method=method_arr, cell=cell_arr), info
