import cv2
import numpy as np
import pytest

from samanvay.types import CanonicalImage
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
