"""
luna-geometry/tests/test_geometry.py

OWNER: seat 6 (Geometry & Validation).
Run with: pytest luna-geometry/tests/test_geometry.py -v
"""

from __future__ import annotations

import numpy as np
import pytest

from bench.fake_matches import apply_homography, generate_fake_matches
from geometric_types import MatchSet, Registration
from geometry.init import coarse_init, geotransform_to_matrix, search_window_from_offset
from geometry.metrics import (
    build_ablation_table,
    build_metrics,
    compare_to_ground_truth,
)
from geometry.refine import refine_point_subpixel, refine_registration
from geometry.uniformity import assign_cells, compute_uniformity
from geometry.verify import verify

# ---------------------------------------------------------------------------
# geometry/init.py
# ---------------------------------------------------------------------------

def test_geotransform_to_matrix_identity_like():
    gt = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    M = geotransform_to_matrix(gt)
    np.testing.assert_allclose(M, np.eye(3))


def test_coarse_init_recovers_pure_offset():
    src_gt = (100.0, 1.0, 0.0, 200.0, 0.0, 1.0)
    ref_gt = (95.0, 1.0, 0.0, 205.0, 0.0, 1.0)
    M = coarse_init(src_gt, ref_gt)
    # source pixel (0,0) -> world (100,200) -> ref pixel (5, -5)
    pt = M @ np.array([0.0, 0.0, 1.0])
    pt = pt[:2] / pt[2]
    np.testing.assert_allclose(pt, [5.0, -5.0], atol=1e-9)


def test_coarse_init_rejects_crs_mismatch():
    gt = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    with pytest.raises(ValueError):
        coarse_init(gt, gt, src_crs="EPSG:4326", ref_crs="EPSG:32633")


def test_search_window_contains_projected_corners():
    src_gt = (0.0, 0.5, 0.0, 0.0, 0.0, 0.5)
    ref_gt = (0.0, 0.5, 0.0, 0.0, 0.0, 0.5)
    M = coarse_init(src_gt, ref_gt)
    window = search_window_from_offset(M, src_shape=(100, 100), uncertainty_px=10)
    min_x, min_y, max_x, max_y = window
    assert min_x < 0 <= max_x
    assert min_y < 0 <= max_y


# ---------------------------------------------------------------------------
# bench/fake_matches.py
# ---------------------------------------------------------------------------

def test_generate_fake_matches_shapes():
    matches, H = generate_fake_matches(n_points=50, seed=1)
    assert len(matches) == 50
    assert matches.src_xy.shape == (50, 2)
    assert matches.ref_xy.shape == (50, 2)
    assert H.shape == (3, 3)


def test_generate_fake_matches_reproducible_with_seed():
    m1, h1 = generate_fake_matches(n_points=30, seed=42)
    m2, h2 = generate_fake_matches(n_points=30, seed=42)
    np.testing.assert_allclose(m1.src_xy, m2.src_xy)
    np.testing.assert_allclose(h1, h2)


# ---------------------------------------------------------------------------
# geometry/verify.py  -- this is the core correctness test for the seat
# ---------------------------------------------------------------------------

def test_verify_recovers_known_homography_low_noise():
    matches, _ = generate_fake_matches(
        n_points=200, noise_std=0.2, outlier_frac=0.0, seed=10
    )
    reg = verify(matches)
    assert reg.model_type != "failed"
    # with zero outliers and low noise, recovered RMSE should be small and
    # close to the injected noise level
    assert reg.metrics["rmse_px"] < 1.0
    assert reg.metrics["inlier_ratio"] > 0.9


def test_verify_is_robust_to_outliers():
    matches, _ = generate_fake_matches(
        n_points=300, noise_std=0.3, outlier_frac=0.3, seed=11
    )
    reg = verify(matches)
    assert reg.model_type != "failed"
    # inlier ratio should roughly match (1 - outlier_frac), with some slack
    assert 0.55 < reg.metrics["inlier_ratio"] < 0.85
    # RMSE should stay close to the injected noise despite 30% outliers
    assert reg.metrics["rmse_px"] < 1.5


def test_verify_recovers_accurate_transform_vs_ground_truth():
    matches, H_true = generate_fake_matches(
        n_points=250, noise_std=0.3, outlier_frac=0.2, seed=12
    )
    reg = verify(matches)
    true_ref_xy = apply_homography(H_true, matches.src_xy)
    gt = compare_to_ground_truth(reg, matches.src_xy, true_ref_xy)
    # the fitted model, evaluated on ALL points (including outliers, which
    # it correctly ignored), should be very close to the true transform
    assert gt["gt_rmse_px"] < 0.5


def test_verify_handles_degenerate_input_gracefully():
    # too few points for even the simplest model
    matches = MatchSet(
        src_xy=np.array([[1.0, 1.0]]),
        ref_xy=np.array([[2.0, 2.0]]),
        score=np.array([1.0], dtype=np.float32),
        method=np.array([0], dtype=np.uint8),
        cell=np.array([-1], dtype=np.int32),
    )
    reg = verify(matches)
    assert reg.model_type == "failed"
    assert reg.metrics["status"] == "failed_no_model_fit"
    assert reg.metrics["inlier_count"] == 0


def test_verify_handles_all_outliers_gracefully():
    rng = np.random.default_rng(0)
    n = 50
    matches = MatchSet(
        src_xy=rng.uniform(0, 1000, size=(n, 2)),
        ref_xy=rng.uniform(0, 1000, size=(n, 2)),  # totally unrelated
        score=np.ones(n, dtype=np.float32),
        method=np.zeros(n, dtype=np.uint8),
        cell=-np.ones(n, dtype=np.int32),
    )
    # must not raise, even if it fails to find a good model
    reg = verify(matches)
    assert reg.model_type in ("failed", "similarity", "affine", "homography")


# ---------------------------------------------------------------------------
# geometry/refine.py
# ---------------------------------------------------------------------------

def test_refine_point_subpixel_recovers_known_shift():
    from scipy.ndimage import shift as nd_shift

    rng = np.random.default_rng(2)
    base = rng.normal(size=(64, 64))
    true_dx, true_dy = 0.8, -1.3
    shifted = nd_shift(base, shift=(true_dy, true_dx), order=3, mode="reflect")

    r = 16
    c = 32
    src_patch = base[c - r : c + r + 1, c - r : c + r + 1]
    ref_patch = shifted[c - r : c + r + 1, c - r : c + r + 1]

    shift_xy, sigma, _ = refine_point_subpixel(src_patch, ref_patch)
    assert shift_xy is not None
    assert abs(shift_xy[0] - true_dx) < 0.3
    assert abs(shift_xy[1] - true_dy) < 0.3
    assert sigma is not None and sigma > 0


def test_refine_point_subpixel_rejects_uncorrelated_patches():
    rng = np.random.default_rng(3)
    src_patch = rng.normal(size=(48, 48))
    ref_patch = rng.normal(size=(48, 48))  # unrelated noise
    shift_xy, sigma, _ = refine_point_subpixel(src_patch, ref_patch, max_error=0.3)
    assert shift_xy is None
    assert sigma is None


def test_refine_registration_improves_accuracy_vs_ground_truth():
    import cv2
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(4)
    img_shape = (400, 400)
    src_img = gaussian_filter(rng.normal(size=img_shape), sigma=2.0)
    H_true = np.array([[1.0, 0.0, 2.0], [0.0, 1.0, -1.5], [0.0, 0.0, 1.0]])
    ref_img = cv2.warpPerspective(src_img, H_true, (img_shape[1], img_shape[0]))

    matches, _ = generate_fake_matches(
        n_points=100, image_shape=img_shape, homography=H_true,
        noise_std=1.5, outlier_frac=0.1, seed=6,
    )
    reg = verify(matches)
    reg_refined = refine_registration(reg, matches, src_img, ref_img, max_error=1.5)

    refined_mask = ~np.isnan(reg_refined.sigma)
    assert refined_mask.sum() > 0, "expected at least some points to be refined"

    true_ref_xy = apply_homography(H_true, matches.src_xy)
    err_before = np.sqrt(
        np.mean(np.sum((matches.ref_xy[refined_mask] - true_ref_xy[refined_mask]) ** 2, axis=1))
    )
    refined_ref_xy = matches.ref_xy + reg_refined.residuals
    err_after = np.sqrt(
        np.mean(np.sum((refined_ref_xy[refined_mask] - true_ref_xy[refined_mask]) ** 2, axis=1))
    )
    assert err_after < err_before, "sub-pixel refinement should reduce per-point error vs ground truth"


# ---------------------------------------------------------------------------
# geometry/uniformity.py
# ---------------------------------------------------------------------------

def test_assign_cells_within_bounds():
    xy = np.array([[0.0, 0.0], [999.0, 999.0], [500.0, 500.0]])
    cells = assign_cells(xy, image_shape=(1000, 1000), grid_n=10)
    assert cells.min() >= 0
    assert cells.max() < 100


def test_uniformity_clustered_scores_worse_than_spread():
    rng = np.random.default_rng(0)
    image_shape = (1000, 1000)

    clustered_xy = rng.uniform(low=[0, 0], high=[200, 200], size=(150, 2))
    spread_xy = rng.uniform(low=[0, 0], high=[1000, 1000], size=(150, 2))
    inliers = np.ones(150, dtype=bool)

    report_clustered = compute_uniformity(clustered_xy, inliers, image_shape, grid_n=8)
    report_spread = compute_uniformity(spread_xy, inliers, image_shape, grid_n=8)

    assert report_spread.coverage_pct > report_clustered.coverage_pct
    assert report_spread.dispersion_cv < report_clustered.dispersion_cv


def test_uniformity_excludes_masked_cells_from_denominator():
    rng = np.random.default_rng(0)
    image_shape = (800, 800)
    spread_xy = rng.uniform(low=[0, 0], high=[800, 800], size=(200, 2))
    inliers = np.ones(200, dtype=bool)

    mask = np.zeros(image_shape, dtype=np.uint8)
    mask[:400, :400] = 1  # shadow the whole top-left quadrant

    report = compute_uniformity(spread_xy, inliers, image_shape, grid_n=8, validity_mask=mask)
    # roughly a quarter of an 8x8 grid should be masked
    assert report.n_masked_cells >= 12  # ~16 expected, allow slack for edges
    total_non_masked = report.n_populated_cells + report.n_insufficient_texture_cells
    assert total_non_masked == 64 - report.n_masked_cells


# ---------------------------------------------------------------------------
# geometry/metrics.py
# ---------------------------------------------------------------------------

def test_build_metrics_prefers_subpixel_rmse_when_present():
    reg = Registration(
        model_type="affine",
        params=np.eye(3),
        metrics={"rmse_px": 1.0, "rmse_px_subpixel": 0.3},
    )
    m = build_metrics(reg)
    assert m["rmse_px_final"] == 0.3
    assert m["rmse_px_source"] == "subpixel_refined"


def test_build_metrics_falls_back_when_no_subpixel():
    reg = Registration(model_type="affine", params=np.eye(3), metrics={"rmse_px": 1.0})
    m = build_metrics(reg)
    assert m["rmse_px_final"] == 1.0
    assert m["rmse_px_source"] == "model_fit_only"


def test_build_ablation_table_computes_deltas():
    runs = {
        "full": {"rmse_px_final": 0.5, "inlier_ratio": 0.8, "coverage_pct": 90.0, "runtime_s": 0.1},
        "no_uniformity": {"rmse_px_final": 0.6, "inlier_ratio": 0.8, "coverage_pct": 40.0, "runtime_s": 0.1},
    }
    table = build_ablation_table(runs)
    row = next(r for r in table if r["config"] == "no_uniformity")
    assert row["coverage_pct_delta_pct"] < 0  # coverage got worse
    assert row["rmse_px_final_delta_pct"] > 0  # RMSE got worse (higher)


# ---------------------------------------------------------------------------
# End-to-end smoke test tying every seat-6 module together
# ---------------------------------------------------------------------------

def test_end_to_end_pipeline_on_synthetic_pair():
    import cv2
    from scipy.ndimage import gaussian_filter

    rng = np.random.default_rng(99)
    img_shape = (600, 600)
    src_img = gaussian_filter(rng.normal(size=img_shape), sigma=2.0)
    H_true = np.array([[1.03, 0.01, 5.0], [-0.01, 1.03, -4.0], [0.0, 0.0, 1.0]])
    ref_img = cv2.warpPerspective(src_img, H_true, (img_shape[1], img_shape[0]))

    matches, _ = generate_fake_matches(
        n_points=200, image_shape=img_shape, homography=H_true,
        noise_std=0.5, outlier_frac=0.2, seed=99,
    )

    reg = verify(matches)
    assert reg.model_type != "failed"

    reg = refine_registration(reg, matches, src_img, ref_img, max_error=1.5)

    uni = compute_uniformity(matches.src_xy, reg.inliers, img_shape, grid_n=8)
    metrics = build_metrics(reg, uni)

    # sanity: every required metrics.json field is present
    for key in ["rmse_px_final", "inlier_count", "inlier_ratio", "coverage_pct", "dispersion_cv"]:
        assert key in metrics

    assert metrics["inlier_count"] > 0
    assert 0.0 <= metrics["inlier_ratio"] <= 1.0
    assert 0.0 <= metrics["coverage_pct"] <= 100.0
