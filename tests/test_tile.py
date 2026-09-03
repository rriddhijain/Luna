import cv2
import numpy as np
import pytest

from samanvay.types import CanonicalImage
from samanvay.match.anms import anms_quadtree
from samanvay.match.tile import match_tiled

# Ground-truth source -> reference homography: 1.5x scale + a large translation, so the
# reference tile with the same grid index is emphatically NOT the right place to look.
GT_H = np.array([[1.5, 0.0, 300.0],
                 [0.0, 1.5, 250.0],
                 [0.0, 0.0, 1.0]], dtype=np.float64)
SRC_N = 256
REF_N = 700


def _canon(albedo, mask=None):
    return CanonicalImage(
        albedo=albedo.astype(np.float32),
        pc=np.zeros_like(albedo, dtype=np.float32),
        pc_orient=np.zeros_like(albedo, dtype=np.float32),
        mask=np.zeros(albedo.shape, dtype=np.uint8) if mask is None else mask,
        params={},
    )


def _texture(n, seed=7):
    """Blurred noise: dense, isotropic, SIFT-friendly texture with no repeated motif."""
    rng = np.random.default_rng(seed)
    img = rng.uniform(0.0, 1.0, size=(n, n)).astype(np.float32)
    img = cv2.GaussianBlur(img, (0, 0), 1.6)
    img -= img.min()
    return img / max(img.max(), 1e-6)


@pytest.fixture(scope="module")
def pair():
    src = _texture(SRC_N)
    ref = cv2.warpPerspective(src, GT_H, (REF_N, REF_N), flags=cv2.INTER_LINEAR)
    return _canon(src), _canon(ref)


def _edges(size, n):
    return np.round(np.linspace(0, size, n + 1)).astype(int)


def _gt_error_src_px(matches):
    """Reprojection error of each match under GT_H, expressed in SOURCE pixels."""
    if len(matches.src_xy) == 0:
        return np.zeros((0,))
    h = np.hstack([matches.src_xy, np.ones((len(matches.src_xy), 1))])
    p = (GT_H @ h.T).T
    proj = p[:, :2] / p[:, 2:3]
    scale = np.sqrt(abs(np.linalg.det(GT_H[:2, :2])))
    return np.linalg.norm(proj - matches.ref_xy, axis=1) / scale


def test_matches_land_on_ground_truth(pair):
    src, ref = pair
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                config={"method": "sift"}, init=GT_H)

    assert info["mode"] == "init_projected"
    assert len(matches.src_xy) >= 8
    err = _gt_error_src_px(matches)
    assert np.median(err) < 2.0
    assert np.mean(err < 2.0) > 0.6
    populated = [c for c in info["cells"].values() if c["status"] == "populated"]
    assert len(populated) >= 3


def test_cores_partition_source_without_double_counting(pair):
    src, ref = pair
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                config={"method": "sift"}, init=GT_H)

    ye, xe = _edges(SRC_N, 2), _edges(SRC_N, 2)
    for (x, y), cid in zip(matches.src_xy, matches.cell):
        row, col = int(cid) // 2, int(cid) % 2
        assert xe[col] <= x < xe[col + 1]
        assert ye[row] <= y < ye[row + 1]

    # Every match is booked to exactly one cell, and the cell ledger agrees.
    assert sum(c["count"] for c in info["cells"].values()) == len(matches.src_xy)
    for cid, cell in info["cells"].items():
        assert cell["count"] == int(np.sum(matches.cell == cid))


def test_max_matches_is_honoured_per_cell(pair):
    src, ref = pair
    budgets = {i: {"min_matches": 1, "max_matches": 3} for i in range(4)}
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                config={"method": "sift"}, cell_budgets=budgets, init=GT_H)

    for cid in range(4):
        assert int(np.sum(matches.cell == cid)) <= 3
    assert len(matches.src_xy) > 0


def test_relaxation_raises_the_count_in_a_starved_cell(pair):
    src, ref = pair
    cfg = {"method": "sift", "ratio_threshold": 0.5, "relax_ratio_step": 0.1}

    strict, strict_info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                      config=dict(cfg, relax_attempts=1),
                                      cell_budgets={i: {"min_matches": 1, "max_matches": 10 ** 6} for i in range(4)},
                                      init=GT_H)
    relaxed, relaxed_info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                        config=dict(cfg, relax_attempts=4),
                                        cell_budgets={i: {"min_matches": 10 ** 6, "max_matches": 10 ** 6} for i in range(4)},
                                        init=GT_H)

    assert len(relaxed.src_xy) > len(strict.src_xy)
    for cid in range(4):
        assert strict_info["cells"][cid]["ratio_threshold"] == pytest.approx(0.5)
        assert relaxed_info["cells"][cid]["attempts"] == 4
        assert relaxed_info["cells"][cid]["ratio_threshold"] == pytest.approx(0.8)


def test_fully_masked_cell_is_skipped_and_reported(pair):
    src, ref = pair
    mask = np.zeros((SRC_N, SRC_N), dtype=np.uint8)
    mask[128:, 128:] = 2  # nodata over the whole core of cell 3
    masked_src = _canon(src.albedo, mask)

    matches, info = match_tiled(masked_src, ref, grid_n=2, halo_px=16,
                                config={"method": "sift"}, init=GT_H)

    cell3 = info["cells"][3]
    assert cell3["status"] == "masked_invalid"
    assert cell3["masked_frac"] == 1.0
    assert cell3["count"] == 0
    assert cell3["attempts"] == 0
    assert cell3["ratio_threshold"] is None  # never attempted, not a fabricated default
    assert np.sum(matches.cell == 3) == 0
    assert len(matches.src_xy) > 0


def test_init_none_is_degraded_but_does_not_crash(pair):
    src, ref = pair
    degraded, degraded_info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                          config={"method": "sift"}, init=None)
    guided, _ = match_tiled(src, ref, grid_n=2, halo_px=16,
                            config={"method": "sift"}, init=GT_H)

    assert degraded_info["mode"] == "degraded_same_grid"
    assert degraded.src_xy.shape[1] == 2
    # Same-grid tiling looks in the wrong place, so it finds far fewer true matches.
    assert np.sum(_gt_error_src_px(degraded) < 2.0) < np.sum(_gt_error_src_px(guided) < 2.0)


def test_degenerate_inputs_return_empty_not_crash():
    flat = _canon(np.zeros((64, 64), dtype=np.float32))
    matches, info = match_tiled(flat, flat, grid_n=4, halo_px=8,
                                config={"method": "sift"}, init=np.eye(3))
    assert len(matches.src_xy) == 0
    assert all(c["status"] == "insufficient_texture" for c in info["cells"].values())

    tiny = _canon(np.zeros((2, 2), dtype=np.float32))
    matches, info = match_tiled(tiny, tiny, grid_n=8, halo_px=4, init=np.eye(3))
    assert len(matches.src_xy) == 0

    # An init that throws the source clean off the reference.
    src, ref = _canon(_texture(64)), _canon(_texture(64, seed=3))
    off = np.array([[1.0, 0.0, 5000.0], [0.0, 1.0, 5000.0], [0.0, 0.0, 1.0]])
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=8, init=off)
    assert len(matches.src_xy) == 0
    assert all(c["status"] == "no_reference_overlap" for c in info["cells"].values())


def test_orb_and_l2_paths(pair):
    src, ref = pair

    orb, orb_info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                config={"method": "orb"}, init=GT_H)
    assert orb_info["method"] == "orb"
    assert np.all(orb.method == 1)

    # config asks for L2 but there is no phase congruency to run it on: fall back,
    # and say so, rather than pretending the L2 path ran.
    fell_back, fb_info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                     config={"method": "l2"}, init=GT_H)
    assert fb_info["method"] == "sift"
    assert np.all(fell_back.method == 0)

    pc_src = _canon(src.albedo)
    pc_src.pc = src.albedo.copy()
    pc_ref = _canon(ref.albedo)
    pc_ref.pc = ref.albedo.copy()
    l2, l2_info = match_tiled(pc_src, pc_ref, grid_n=2, halo_px=16,
                              config={"method": "l2"}, init=GT_H)
    assert l2_info["method"] == "l2"
    assert np.all(l2.method == 2)


def test_no_duplicate_tie_points(pair):
    """One row per (source, reference) location.

    SIFT emits several keypoints at one pixel — one per dominant orientation — so the
    same correspondence used to come back several times with different descriptor
    scores. That inflated `inlier_count` and `redundancy`, and `rmse_trustworthy` is
    judged on redundancy: a real cross-mission run reported 100 inliers that were 28
    distinct points, and still claimed the RMSE was trustworthy.
    """
    src, ref = pair
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=16,
                                config={"method": "sift"}, init=GT_H)

    assert len(matches.src_xy) > 0
    pairs = np.hstack([matches.src_xy, matches.ref_xy])
    assert len(np.unique(pairs, axis=0)) == len(pairs)

    # The surviving row per location must be the best-scoring one, not an arbitrary one,
    # and the cell ledger must describe what is actually returned.
    assert np.all(np.isfinite(matches.score))
    assert sum(c["count"] for c in info["cells"].values()) == len(matches.src_xy)


# --- ANMS ------------------------------------------------------------------

def _quadrant(xy, bbox=(0.0, 0.0, 100.0, 100.0)):
    """Which quarter of the bbox each point is in: 0 TL, 1 TR, 2 BL, 3 BR."""
    xm, ym = 0.5 * (bbox[0] + bbox[2]), 0.5 * (bbox[1] + bbox[3])
    xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    return (xy[:, 0] >= xm).astype(int) + 2 * (xy[:, 1] >= ym).astype(int)


def _clustered_cell():
    """90 high-scoring points in one corner, 10 low-scoring ones spread over the rest.

    This is the lunar case the grid alone does not solve: one textured patch inside an
    otherwise smooth mare cell.
    """
    rng = np.random.default_rng(0)
    corner = rng.uniform(0.0, 10.0, size=(90, 2))
    spread = np.array([[75., 25.], [85., 15.], [60., 40.],
                       [25., 75.], [15., 85.], [40., 60.],
                       [75., 75.], [85., 85.], [60., 90.], [90., 60.]])
    xy = np.vstack([corner, spread])
    score = np.concatenate([rng.uniform(0.9, 1.0, 90), np.full(10, 0.1)])
    return xy, score


def test_anms_spreads_a_quota_the_score_sort_clusters():
    xy, score = _clustered_cell()
    bbox = (0.0, 0.0, 100.0, 100.0)
    k = 8

    by_score = np.argsort(score)[::-1][:k]
    assert len(set(_quadrant(xy[by_score]).tolist())) == 1      # all 8 in one corner

    kept = anms_quadtree(xy, score, k, bbox)
    assert len(kept) == k
    assert len(set(_quadrant(xy[kept]).tolist())) >= 3
    # The quota is split by region, not by score, so the corner cannot take more than
    # its quarter even though it holds 90% of the candidates and every top score.
    assert int(np.sum(_quadrant(xy[kept]) == 0)) <= k // 4 + 1
    # ... and within the corner it still takes the best of what is there.
    assert score[kept].max() > 0.9


def test_anms_returns_unique_ascending_indices_and_is_deterministic():
    xy, score = _clustered_cell()
    bbox = (0.0, 0.0, 100.0, 100.0)
    for k in (1, 3, 4, 8, 17, 50, 99):
        kept = anms_quadtree(xy, score, k, bbox)
        assert len(kept) == k                                   # the quota is spent in full
        assert len(np.unique(kept)) == k
        assert np.all(np.diff(kept) > 0)
        assert kept.min() >= 0 and kept.max() < len(xy)
        assert np.array_equal(kept, anms_quadtree(xy, score, k, bbox))


def test_anms_never_reorders_a_quota_that_does_not_bite():
    xy, score = _clustered_cell()
    bbox = (0.0, 0.0, 100.0, 100.0)
    assert np.array_equal(anms_quadtree(xy, score, 100, bbox), np.arange(100))
    assert np.array_equal(anms_quadtree(xy, score, 500, bbox), np.arange(100))
    assert np.array_equal(anms_quadtree(xy, score, 0, bbox), np.arange(100))
    assert np.array_equal(anms_quadtree(xy, score, -5, bbox), np.arange(100))
    assert anms_quadtree(np.zeros((0, 2)), np.zeros(0), 5, bbox).shape == (0,)


def test_anms_degrades_to_the_score_sort_instead_of_raising():
    xy, score = _clustered_cell()
    best3 = np.sort(np.argsort(score)[::-1][:3])

    # A zero-area or non-finite bbox has no quadrants to spread over.
    assert np.array_equal(anms_quadtree(xy, score, 3, (0, 0, 0, 0)), best3)
    assert np.array_equal(anms_quadtree(xy, score, 3, (0, 0, np.nan, 100)), best3)
    assert np.array_equal(anms_quadtree(xy, score, 3, (0, 0)), best3)

    # Coincident points cannot be separated at any depth; the quota is still filled.
    same = np.zeros((20, 2)) + 5.0
    assert len(anms_quadtree(same, np.arange(20.0), 4, (0, 0, 100, 100))) == 4

    # A NaN coordinate cannot be placed in the tree, and a NaN score is the worst
    # point there is — neither may raise, and neither may be silently mis-located.
    holed = xy.copy()
    holed[3] = np.nan
    kept = anms_quadtree(holed, score, 8, (0, 0, 100, 100))
    assert len(kept) == 8 and len(np.unique(kept)) == 8
    bad_score = score.copy()
    bad_score[:5] = np.nan
    assert len(anms_quadtree(xy, bad_score, 8, (0, 0, 100, 100))) == 8


def test_anms_gate_is_reported_and_off_keeps_the_score_sort(pair):
    src, ref = pair
    budgets = {i: {"min_matches": 1, "max_matches": 3} for i in range(4)}
    kw = dict(grid_n=2, halo_px=16, cell_budgets=budgets, init=GT_H)

    on, on_info = match_tiled(src, ref, config={"method": "sift", "anms": True}, **kw)
    off, off_info = match_tiled(src, ref, config={"method": "sift", "anms": False}, **kw)

    assert on_info["anms"] is True and off_info["anms"] is False
    assert (on_info["grid_rows"], on_info["grid_cols"]) == (2, 2)

    # The flag is per cell and only claimed where the quota actually bit: a cell that
    # never reached its budget ran no selection rule at all, so neither answer is true.
    bit_on = [c for c in on_info["cells"].values() if c["anms"] is not None]
    bit_off = [c for c in off_info["cells"].values() if c["anms"] is not None]
    assert bit_on and bit_off
    assert all(c["anms"] is True for c in bit_on)
    assert all(c["anms"] is False for c in bit_off)

    for cid in range(4):
        assert int(np.sum(on.cell == cid)) <= 3
        assert int(np.sum(off.cell == cid)) <= 3

    # anms=False is the pre-ANMS rule verbatim — argsort(score)[::-1][:k] — so each
    # cell's kept scores come back in descending order. ANMS returns them in index
    # order instead, because it picks by region.
    for cell in off_info["cells"]:
        s = off.score[off.cell == cell]
        assert np.all(np.diff(s) <= 1e-6), (cell, s)

    # Both arms are on true tie-points; ANMS trades score for spread, not for accuracy.
    assert len(on.src_xy) > 0
    assert np.median(_gt_error_src_px(on)) < 2.0


def _cell_quadrants(matches, rows, cols, size=SRC_N):
    """Total occupied sub-quadrants over all populated cells; max is 4 per cell.

    The number the grid cannot see: coverage_pct and dispersion_cv are per CELL and
    score 100% / 0.0 whether a cell's points fill it or sit in one corner of it.
    """
    y_e = np.round(np.linspace(0, size, rows + 1))
    x_e = np.round(np.linspace(0, size, cols + 1))
    total = 0
    for cell_id in np.unique(matches.cell):
        row, col = divmod(int(cell_id), cols)
        x_m, y_m = 0.5 * (x_e[col] + x_e[col + 1]), 0.5 * (y_e[row] + y_e[row + 1])
        here = matches.src_xy[matches.cell == cell_id]
        total += len({(x >= x_m, y >= y_m) for x, y in here})
    return total


def test_a_bound_quota_takes_the_anms_path_and_spreads_within_the_cell(pair):
    """The regime the shipped ablation never reached: candidates EXCEED the quota.

    `match.max_matches` is 50 by default and both shipped fixtures top out at 49
    candidates per cell, so `if len(k_src) > max_matches` is never entered, `cell["anms"]`
    stays None and an anms on/off ablation row measures nothing at all
    (bench/baselines.md section 2). It binds unaided on real LROC NAC — 168 of 172 cells
    on apollo16_dsun004 at the shipped 50 — and here it is forced with a budget of 6.
    """
    src, ref = pair
    budgets = {i: {"min_matches": 1, "max_matches": 6} for i in range(4)}
    kw = dict(grid_n=2, halo_px=16, cell_budgets=budgets, init=GT_H)

    on, on_info = match_tiled(src, ref, config={"method": "sift", "anms": True}, **kw)
    off, off_info = match_tiled(src, ref, config={"method": "sift", "anms": False}, **kw)

    # The branch ran: every one of the four cells had more candidates than its budget.
    bound = [c for c in on_info["cells"].values() if c["anms"] is not None]
    assert len(bound) == 4
    assert all(c["anms"] is True for c in bound)
    assert all(c["anms"] is False for c in off_info["cells"].values() if c["anms"] is not None)

    # ... and the points it kept are spread where the score sort's are not. Measured
    # 16/16 quadrants with ANMS against 13/16 on the score sort.
    quad_on = _cell_quadrants(on, 2, 2)
    quad_off = _cell_quadrants(off, 2, 2)
    assert quad_on > quad_off, (quad_on, quad_off)
    assert quad_on == 16

    # The spread is not bought by dropping points: every cell spends its whole quota on
    # the ANMS arm (the score sort can end up one short, because the location dedup that
    # runs after the quota removes a duplicate it happened to keep). And the points are
    # still on the ground truth — ANMS trades score for position, not for correctness.
    for cell_id in range(4):
        assert int(np.sum(on.cell == cell_id)) == 6
        assert int(np.sum(off.cell == cell_id)) <= 6
    assert np.median(_gt_error_src_px(on)) < 2.0


def test_a_zero_budget_keeps_nothing_on_either_arm(pair):
    """anms_quadtree reads k <= 0 as "no quota"; the cell branch must not inherit that.

    max_matches=0 asks for no matches. Handing 0 to the quad-tree returns every
    candidate instead, so the ANMS arm would deliver more points than the score-sort
    arm at the one budget where both must deliver none.
    """
    src, ref = pair
    budgets = {i: {"min_matches": 1, "max_matches": 0} for i in range(4)}
    kw = dict(grid_n=2, halo_px=16, cell_budgets=budgets, init=GT_H)

    on, on_info = match_tiled(src, ref, config={"method": "sift", "anms": True}, **kw)
    off, _ = match_tiled(src, ref, config={"method": "sift", "anms": False}, **kw)

    assert len(on.src_xy) == 0 and len(off.src_xy) == 0
    # The gate is still True for the run; no cell claims a quad-tree selection it
    # did not get, because the score sort is what actually ran there.
    assert on_info["anms"] is True
    assert all(c["anms"] in (None, False) for c in on_info["cells"].values())


# ------------------------------------------------- relaxation accounting (inlier_ratio)

def test_relaxation_accounting_separates_strict_from_relaxed_putatives(pair):
    """inlier_ratio's denominator grows when a cell loosens its ratio test; say by how much.

    The strict subset is defined in score space so it is recoverable from matches.csv
    alone: score >= strict_score_min is the Lowe test at the configured threshold.
    """
    src, ref = pair
    cfg = {"method": "sift", "ratio_threshold": 0.3, "relax_attempts": 5,
           "relax_ratio_step": 0.2, "ratio_ceiling": 0.95}
    # A quota no cell can fill at ratio 0.3, so every cell is driven up the ladder.
    budgets = {c: {"min_matches": 400, "max_matches": 2000} for c in range(4)}
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=16, config=cfg,
                                cell_budgets=budgets, init=GT_H)

    assert info["ratio_base"] == 0.3
    assert info["strict_score_min"] == pytest.approx(0.7)
    assert info["putative_count"] == len(matches.src_xy)
    assert info["relaxed_cells"] > 0
    assert 0 < info["strict_count"] < info["putative_count"]
    # The published threshold reproduces the count from the returned scores.
    assert info["strict_count"] == int((matches.score >= info["strict_score_min"]).sum())
    assert sum(c["count_strict"] for c in info["cells"].values()
               if c["count_strict"] is not None) == info["strict_count"]


def test_no_cell_is_marked_relaxed_when_the_base_threshold_already_suffices(pair):
    src, ref = pair
    cfg = {"method": "sift", "ratio_threshold": 0.9, "relax_attempts": 4}
    budgets = {c: {"min_matches": 1, "max_matches": 200} for c in range(4)}
    matches, info = match_tiled(src, ref, grid_n=2, halo_px=16, config=cfg,
                                cell_budgets=budgets, init=GT_H)

    assert info["relaxed_cells"] == 0
    assert info["strict_count"] == info["putative_count"] == len(matches.src_xy)
    for cell in info["cells"].values():
        # None only where the matcher never reached the ratio loop.
        assert cell["relaxed"] in (None, False)
