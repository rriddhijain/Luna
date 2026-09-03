import json

import numpy as np
import pytest

from samanvay.types import MatchSet, Registration
from samanvay.geometry.uniformity import (assign_cells, grid_shape,
                                          spatial_distribution_index,
                                          uniformity_report)
from samanvay.geometry.metrics import compute_metrics

SHAPE = (256, 256)


def approx(v):
    return pytest.approx(v, abs=1e-6)


def make_set(xy):
    xy = np.array(xy, dtype=np.float64).reshape(-1, 2)
    n = len(xy)
    return MatchSet(src_xy=xy, ref_xy=xy + 1.0,
                    score=np.ones(n, dtype=np.float32),
                    method=np.zeros(n, dtype=np.uint8),
                    cell=np.zeros(n, dtype=np.int32))


def make_reg(n, inliers=None, residuals=None, sigma=None, H=None, metrics=None):
    return Registration(
        model_type="affine",
        params=np.eye(3) if H is None else H,
        init_params=np.eye(3),
        inliers=np.ones(n, dtype=bool) if inliers is None else inliers,
        residuals=np.zeros((n, 2)) if residuals is None else residuals,
        sigma=np.full(n, np.nan) if sigma is None else sigma,
        metrics={} if metrics is None else metrics,
    )


def test_assign_cells_layout():
    # cell id = col + grid_n*row, (x, y) = (column, row)
    xy = [[10, 10], [200, 10], [10, 200], [200, 200], [255, 255]]
    cells = assign_cells(xy, SHAPE, 2)
    assert cells.dtype == np.int32
    assert list(cells) == [0, 1, 2, 3, 3]
    assert assign_cells(np.zeros((0, 2)), SHAPE, 4).shape == (0,)


def test_three_distinct_cell_states():
    # 2x2 grid. Cell 0 populated. Cell 3 mostly shadow. Cells 1 and 2 are clean
    # imagery that simply produced no matches.
    mask = np.zeros(SHAPE, dtype=np.uint8)
    mask[128:, 128:] = 1                       # shadow over cell 3
    rep = uniformity_report(make_set([[30, 30], [60, 40]]), SHAPE, 2, mask=mask)

    assert rep["cell_states"] == ["populated", "insufficient_texture",
                                  "insufficient_texture", "masked_invalid"]
    assert rep["counts"] == [2, 0, 0, 0]
    assert set(rep["cell_states"]) == {"populated", "insufficient_texture", "masked_invalid"}


def test_masked_cell_leaves_the_coverage_denominator():
    mask = np.zeros(SHAPE, dtype=np.uint8)
    unmasked = uniformity_report(make_set([[30, 30], [200, 30]]), SHAPE, 2, mask=mask)
    assert unmasked["coverage_pct"] == 50.0          # 2 of 4 cells

    mask[128:, :] = 2                                # nodata over cells 2 and 3
    masked = uniformity_report(make_set([[30, 30], [200, 30]]), SHAPE, 2, mask=mask)
    assert masked["coverage_pct"] == 100.0           # 2 of the 2 cells we could use
    assert masked["cell_states"][2:] == ["masked_invalid", "masked_invalid"]


def test_populated_beats_masked_when_we_actually_matched_there():
    mask = np.full(SHAPE, 1, dtype=np.uint8)
    rep = uniformity_report(make_set([[30, 30]]), SHAPE, 2, mask=mask)
    assert rep["cell_states"][0] == "populated"
    assert rep["coverage_pct"] == 100.0              # 1 usable cell, and it is covered


def test_dispersion_and_undefined_cases():
    single = uniformity_report(make_set([[30, 30]]), SHAPE, 1)
    assert single["dispersion_cv"] == 0.0            # one cell is trivially uniform

    none_at_all = uniformity_report(make_set(np.zeros((0, 2))), SHAPE, 2)
    assert none_at_all["coverage_pct"] == 0.0
    assert none_at_all["dispersion_cv"] is None      # not 0.0: undefined

    all_masked = uniformity_report(make_set(np.zeros((0, 2))), SHAPE, 2,
                                   mask=np.full(SHAPE, 2, dtype=np.uint8))
    assert all_masked["coverage_pct"] is None
    assert all_masked["dispersion_cv"] is None

    even = uniformity_report(make_set([[30, 30], [200, 30], [30, 200], [200, 200]]), SHAPE, 2)
    uneven = uniformity_report(make_set([[30, 30]] * 4), SHAPE, 2)
    assert even["dispersion_cv"] == 0.0
    assert uneven["dispersion_cv"] > even["dispersion_cv"]


def test_uniformity_honours_inliers_and_odd_mask_shape():
    xy = [[30, 30], [200, 30], [30, 200], [200, 200]]
    inl = np.array([True, False, False, False])
    rep = uniformity_report(make_set(xy), SHAPE, 2, inliers=inl)
    assert rep["counts"] == [1, 0, 0, 0]

    # A mask at a different resolution still splits into the same fractional grid.
    small = np.zeros((64, 64), dtype=np.uint8)
    small[32:, 32:] = 1
    rep = uniformity_report(make_set(xy), SHAPE, 2, mask=small)
    assert rep["cell_states"][3] == "populated"
    assert rep["cell_states"][0] == "populated"


def test_metrics_are_json_serialisable_and_complete():
    xy = np.array([[30.0, 30.0], [200.0, 30.0], [30.0, 200.0], [200.0, 200.0]])
    res = np.full((4, 2), 0.3)
    sigma = np.array([0.1, 0.2, np.nan, 0.4])
    reg = make_reg(4, residuals=res, sigma=sigma, metrics={"model_margin": 1.5})
    m = compute_metrics(make_set(xy), reg, SHAPE, grid_n=2, runtime_s=1.25)

    for key in ("rmse_px", "inlier_count", "inlier_ratio", "coverage_pct",
                "dispersion_cv", "grid_n", "runtime_s", "mean_sigma_px",
                "refined_count", "model_type", "model_margin", "cell_counts"):
        assert key in m, key

    assert json.loads(json.dumps(m))["grid_n"] == 2
    assert m["rmse_px"] == approx(np.sqrt(2 * 0.09))
    assert m["inlier_count"] == 4 and m["inlier_ratio"] == 1.0
    # Points with a KNOWN uncertainty. Not the same as points refined: refine.py
    # returns NaN sigma for points it did move at correlations above its calibrated
    # range, so compute_metrics cannot count refinements and honestly says None.
    # pipeline/stages.py, which can compare pre- and post-refinement coordinates,
    # overwrites refined_count with the true figure.
    assert m["sigma_known_count"] == 3
    assert m["refined_count"] is None
    assert m["mean_sigma_px"] == approx((0.1 + 0.2 + 0.4) / 3)
    assert m["model_type"] == "affine" and m["model_margin"] == 1.5
    assert m["cell_counts"] == [1, 1, 1, 1]
    assert m["runtime_s"] == 1.25


def test_metrics_emit_null_not_zero_when_undefined():
    empty = make_set(np.zeros((0, 2)))
    m = compute_metrics(empty, make_reg(0), SHAPE, grid_n=2)
    assert m["rmse_px"] is None                      # zero RMSE from zero matches is a lie
    assert m["inlier_ratio"] is None
    assert m["mean_sigma_px"] is None
    assert m["model_margin"] is None                 # verify.py did not report one
    assert m["gt_rmse_px"] is None
    assert m["inlier_count"] == 0
    json.dumps(m)

    # matches exist but every one is an outlier
    xy = np.array([[30.0, 30.0], [200.0, 200.0]])
    reg = make_reg(2, inliers=np.zeros(2, dtype=bool), residuals=np.full((2, 2), 9.0))
    m = compute_metrics(make_set(xy), reg, SHAPE, grid_n=2)
    assert m["rmse_px"] is None and m["inlier_ratio"] == 0.0
    assert m["coverage_pct"] == 0.0
    json.dumps(m)


def test_ground_truth_metrics():
    xy = np.array([[30.0, 30.0], [200.0, 200.0]])
    gt = np.array([[1.0, 0.0, 5.0], [0.0, 1.0, -2.0], [0.0, 0.0, 1.0]])
    est = np.array([[1.0, 0.0, 5.5], [0.0, 1.0, -2.0], [0.0, 0.0, 1.0]])
    reg = make_reg(2, residuals=np.zeros((2, 2)), H=est)
    m = compute_metrics(make_set(xy), reg, SHAPE, grid_n=2, gt_H=gt)

    # est translates 0.5 px too far in x, so truth says every point is off by 0.5 px.
    assert m["gt_rmse_px"] == approx(0.5)
    assert m["gt_bias_x"] == approx(-0.5)
    assert m["gt_bias_y"] == approx(0.0)
    assert m["gt_p90_px"] == approx(0.5)
    # self-consistency says zero; truth says 0.5. Both must be quotable.
    assert m["rmse_px"] == 0.0
    json.dumps(m)

    # a singular estimate cannot be compared against truth
    bad = make_reg(2, residuals=np.zeros((2, 2)), H=np.zeros((3, 3)))
    assert compute_metrics(make_set(xy), bad, SHAPE, gt_H=gt)["gt_rmse_px"] is None


# --- N x M grid ------------------------------------------------------------

def _old_square_assign(xy, shape, grid_n):
    """assign_cells exactly as it stood before grid_shape existed (git bc3ad8e)."""
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


def test_grid_shape_on_a_square_image_is_byte_identical_to_the_old_grid():
    """The aspect rule must be a no-op on a square image, cell id for cell id."""
    rng = np.random.default_rng(11)
    xy = rng.uniform(-20.0, 300.0, size=(4000, 2))          # includes out-of-frame points
    xy[:5] = [[np.nan, 5.0], [5.0, np.nan], [np.inf, 1.0], [-np.inf, 1.0], [0.0, 0.0]]
    for grid_n in (1, 2, 3, 4, 7, 16):
        assert grid_shape(SHAPE, grid_n) == (grid_n, grid_n)
        new = assign_cells(xy, SHAPE, grid_n)
        old = _old_square_assign(xy, SHAPE, grid_n)
        assert new.dtype == old.dtype
        assert np.array_equal(new, old), grid_n
        assert new.tobytes() == old.tobytes(), grid_n


def test_grid_shape_follows_the_aspect_and_caps_the_long_axis():
    # grid_n counts the SHORT axis; the long axis is scaled and rounded near-square.
    assert grid_shape((888, 11952), 4) == (4, 54)            # 4 * 11952/888 = 53.8
    assert grid_shape((11952, 888), 4) == (54, 4)            # tall strips too
    assert grid_shape((888, 11952), 4, aspect=False) == (4, 4)
    # 1:200 would ask for 800 cells of ~15 px, which measures noise. Capped at 64.
    assert grid_shape((100, 20000), 4) == (4, 64)
    # Degenerate inputs fall back to the square grid rather than raising.
    assert grid_shape((0, 0), 3) == (3, 3)
    assert grid_shape((10, 3000), 0) == (1, 64)


def test_uniformity_report_partitions_a_strip_by_rows_and_cols():
    strip = (100, 800)                                       # 8:1, so 2 -> 2 x 16
    rep = uniformity_report(make_set([[10, 10], [750, 90]]), strip, 2)
    assert (rep["grid_rows"], rep["grid_cols"]) == (2, 16)
    assert rep["grid_n"] == 2                                # short-axis count, unchanged
    assert len(rep["counts"]) == 32 and len(rep["cell_states"]) == 32
    # cell id stays col + cols*row: (750, 90) is col 15, row 1.
    assert list(assign_cells([[750, 90]], strip, 2)) == [31]
    assert rep["counts"][0] == 1 and rep["counts"][31] == 1
    assert rep["coverage_pct"] == pytest.approx(100.0 * 2 / 32)

    square_grid = uniformity_report(make_set([[10, 10], [750, 90]]), strip, 2, aspect=False)
    assert (square_grid["grid_rows"], square_grid["grid_cols"]) == (2, 2)
    assert len(square_grid["counts"]) == 4


def test_masked_cells_follow_the_aspect_grid_too():
    strip = (100, 800)
    mask = np.zeros(strip, dtype=np.uint8)
    mask[:, 400:] = 2                                        # nodata over columns 8..15
    rep = uniformity_report(make_set([[10, 10]]), strip, 2, mask=mask)
    states = rep["cell_states"]
    for row in range(2):
        for col in range(16):
            expect_masked = col >= 8
            assert (states[col + 16 * row] == "masked_invalid") is expect_masked, (row, col)
    # 16 cells left in the denominator, one of them reached.
    assert rep["coverage_pct"] == pytest.approx(100.0 / 16)


# --- SDI -------------------------------------------------------------------

def test_sdi_is_derived_from_coverage_and_dispersion():
    assert spatial_distribution_index(100.0, 0.0) == approx(1.0)
    assert spatial_distribution_index(50.0, 1.0) == approx(0.25)
    assert spatial_distribution_index(0.0, 0.0) == approx(0.0)
    # Perfectly even over 4 of 4 cells is the only way to score 1.0.
    even = uniformity_report(make_set([[30, 30], [200, 30], [30, 200], [200, 200]]), SHAPE, 2)
    assert even["sdi"] == approx(1.0)
    # Same coverage, clumped counts: coverage cannot hide the clumping.
    clumped = uniformity_report(make_set([[30, 30]] * 7 + [[200, 30], [30, 200], [200, 200]]),
                                SHAPE, 2)
    assert clumped["coverage_pct"] == even["coverage_pct"]
    assert clumped["sdi"] < even["sdi"]
    # Half the cells reached. An unreached cell counts twice — it is missing from
    # coverage AND it is a zero in the dispersion — so the scalar drops to 0.25,
    # not to the 0.5 the coverage term alone would suggest.
    half = uniformity_report(make_set([[30, 30], [200, 30]]), SHAPE, 2)
    assert (half["coverage_pct"], half["dispersion_cv"]) == (50.0, 1.0)
    assert half["sdi"] == approx(0.25)
    assert even["sdi_definition"] == "sdi = (coverage_pct/100) * 1/(1 + dispersion_cv)"


def test_sdi_is_null_when_either_input_is_undefined():
    assert spatial_distribution_index(None, 0.0) is None
    assert spatial_distribution_index(100.0, None) is None
    # No matches anywhere: dispersion is undefined, so the scalar is null, not 0.0.
    none_at_all = uniformity_report(make_set(np.zeros((0, 2))), SHAPE, 2)
    assert none_at_all["dispersion_cv"] is None and none_at_all["sdi"] is None
    # Every cell written off: coverage is undefined too.
    all_masked = uniformity_report(make_set(np.zeros((0, 2))), SHAPE, 2,
                                   mask=np.full(SHAPE, 2, dtype=np.uint8))
    assert all_masked["sdi"] is None

    m = compute_metrics(make_set(np.zeros((0, 2))), make_reg(0), SHAPE, grid_n=2)
    assert m["sdi"] is None
    assert m["sdi_definition"]
    assert m["grid_rows"] == 2 and m["grid_cols"] == 2
    json.dumps(m)


# --- metrics passthrough ---------------------------------------------------

def test_metrics_carry_the_held_out_and_tps_keys_from_the_registration():
    """These are computed in verify.py; compute_metrics is what puts them in metrics.json."""
    reg_metrics = {
        "check_rmse_px": 0.83, "check_rmse_all_px": 1.42, "check_p90_px": 2.05,
        "n_check": 24, "n_control": 96, "n_check_inlier": 21,
        "check_outlier_frac": 0.125, "check_fraction": 0.2, "check_status": "ok",
        "tps_applied": np.True_, "tps_status": "applied",
        "tps_check_rmse_before_px": 1.42, "tps_check_rmse_after_px": 1.09,
        "tps_n_control": 88,
    }
    xy = np.array([[30.0, 30.0], [200.0, 200.0]])
    m = compute_metrics(make_set(xy), make_reg(2, metrics=reg_metrics), SHAPE, grid_n=2)
    for key, value in reg_metrics.items():
        assert m[key] == value, key
    assert m["tps_applied"] is True                  # np.bool_ would not survive json.dump
    assert isinstance(m["n_check"], int)
    json.dumps(m)


def test_held_out_keys_are_null_when_the_stage_did_not_run():
    xy = np.array([[30.0, 30.0], [200.0, 200.0]])
    m = compute_metrics(make_set(xy), make_reg(2), SHAPE, grid_n=2)
    for key in ("check_rmse_px", "check_rmse_all_px", "check_p90_px", "n_check",
                "n_control", "n_check_inlier", "check_outlier_frac", "check_fraction",
                "check_status", "tps_applied", "tps_status", "tps_n_control",
                "tps_check_rmse_before_px", "tps_check_rmse_after_px"):
        assert key in m and m[key] is None, key
    # A NaN check RMSE is an absence, not a score of nan.
    nan_run = compute_metrics(make_set(xy), make_reg(2, metrics={"check_rmse_px": np.nan}),
                              SHAPE, grid_n=2)
    assert nan_run["check_rmse_px"] is None
    json.dumps(nan_run)


def test_inlier_ratio_is_scored_against_the_plan_bar():
    xy = np.tile(np.array([[30.0, 30.0], [200.0, 200.0]]), (10, 1))
    n = len(xy)

    inl = np.zeros(n, dtype=bool)
    inl[:18] = True                                          # 0.90
    passing = compute_metrics(make_set(xy), make_reg(n, inliers=inl), SHAPE, grid_n=2)
    assert passing["inlier_ratio"] == approx(0.9)
    assert passing["inlier_ratio_pass"] is True
    assert passing["inlier_ratio_target"] == 0.85

    inl = np.zeros(n, dtype=bool)
    inl[:16] = True                                          # 0.80 — a miss, and it shows
    failing = compute_metrics(make_set(xy), make_reg(n, inliers=inl), SHAPE, grid_n=2)
    assert failing["inlier_ratio_pass"] is False

    inl = np.zeros(n, dtype=bool)
    inl[:17] = True                                          # exactly 0.85 passes the >= bar
    edge = compute_metrics(make_set(xy), make_reg(n, inliers=inl), SHAPE, grid_n=2)
    assert edge["inlier_ratio"] == approx(0.85) and edge["inlier_ratio_pass"] is True

    # No matches at all is an absence, not a failed run: null, and it must be json-safe.
    empty = compute_metrics(make_set(np.zeros((0, 2))), make_reg(0), SHAPE, grid_n=2)
    assert empty["inlier_ratio"] is None and empty["inlier_ratio_pass"] is None
    json.dumps(empty)


def test_a_non_finite_count_is_null_not_a_number():
    """int(inf) raises OverflowError, which is not ValueError. A count that arrived
    non-finite is an absence, and must not take the run down with it either."""
    xy = np.array([[30.0, 30.0], [200.0, 200.0]])
    m = compute_metrics(make_set(xy), make_reg(2, metrics={"n_check": float("inf"),
                                                           "n_control": float("nan")}),
                        SHAPE, grid_n=2)
    assert m["n_check"] is None and m["n_control"] is None
    json.dumps(m)


def test_sdi_is_null_with_no_matches_even_on_a_one_cell_grid():
    """A single live cell is dispersion_cv 0.0 by convention. With nothing in it that
    convention would score a run that delivered zero tie-points at 0.0."""
    empty = uniformity_report(make_set(np.zeros((0, 2))), SHAPE, 1)
    assert empty["grid_rows"] == 1 and empty["grid_cols"] == 1
    assert (empty["coverage_pct"], empty["dispersion_cv"]) == (0.0, 0.0)
    assert empty["sdi"] is None
    # One match in that cell is a defined, and perfect, distribution.
    one = uniformity_report(make_set([[30.0, 30.0]]), SHAPE, 1)
    assert one["sdi"] == approx(1.0)
