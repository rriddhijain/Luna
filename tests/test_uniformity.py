import json

import numpy as np
import pytest

from samanvay.types import MatchSet, Registration
from samanvay.geometry.uniformity import assign_cells, uniformity_report
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
