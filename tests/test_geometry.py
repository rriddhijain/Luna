"""Seat 6 (geometry) — tests for the coarse init and the verification model ladder."""

import numpy as np
import pytest

from bench.fake_matches import generate_fake_matches
from samanvay.geometry.init import apply_transform, coarse_init, coarse_init_info, project_box
from samanvay.geometry.verify import verify_matches
from samanvay.types import MatchSet, Product, Registration


def _product(gt=None, crs=None, gsd=None, shape=(200, 300)):
    meta = {"shape": shape}
    if gt is not None:
        meta["geotransform"] = list(gt)
    if crs is not None:
        meta["crs"] = crs
    if gsd is not None:
        meta["gsd_m"] = gsd
    return Product(path="mem", array=np.zeros(shape, dtype=np.float32), meta=meta)


def _matchset(src, ref):
    n = len(src)
    return MatchSet(
        src_xy=np.asarray(src, dtype=np.float64).reshape(-1, 2),
        ref_xy=np.asarray(ref, dtype=np.float64).reshape(-1, 2),
        score=np.ones(n, dtype=np.float32),
        method=np.zeros(n, dtype=np.uint8),
        cell=np.zeros(n, dtype=np.int32),
    )


# --------------------------------------------------------------------------- init


def test_coarse_init_recovers_known_geotransform_pair():
    """Source 2 m/px, reference 1 m/px, north-up, shared CRS: exact composition."""
    src = _product(gt=(1000.0, 2.0, 0.0, 5000.0, 0.0, -2.0), crs="EPSG:32601", gsd=2.0)
    ref = _product(gt=(900.0, 1.0, 0.0, 5200.0, 0.0, -1.0), crs="EPSG:32601", gsd=1.0)

    H, info = coarse_init_info(src, ref)
    assert info["method"] == "geotransform"
    assert info["crs_match"] is True

    # Pixel centres: world_x = 1000 + 2*(x+0.5) = 2x + 1001, so
    #   ref_x = (world_x - 900)/1 - 0.5 = 2x + 100.5.
    # world_y = 5000 - 2*(y+0.5) = -2y + 4999, so
    #   ref_y = (world_y - 5200)/(-1) - 0.5 = 2y + 200.5.
    expected = np.array([[2.0, 0.0, 100.5], [0.0, 2.0, 200.5], [0.0, 0.0, 1.0]])
    assert np.allclose(H, expected, atol=1e-9)
    assert np.allclose(coarse_init(src, ref), expected, atol=1e-9)
    assert np.allclose(apply_transform(H, [[0.0, 0.0], [10.0, 10.0]]),
                       [[100.5, 200.5], [120.5, 220.5]], atol=1e-9)


def test_coarse_init_reads_rasterio_affine_ordering_too():
    """rasterio's Affine order (dx, rx, x0, ry, dy, y0) gives the same matrix."""
    gdal = _product(gt=(1000.0, 2.0, 0.0, 5000.0, 0.0, -2.0), crs="EPSG:32601")
    rio = _product(gt=(2.0, 0.0, 1000.0, 0.0, -2.0, 5000.0), crs="EPSG:32601")
    ref = _product(gt=(900.0, 1.0, 0.0, 5200.0, 0.0, -1.0), crs="EPSG:32601")
    assert np.allclose(coarse_init(gdal, ref), coarse_init(rio, ref), atol=1e-9)


def test_coarse_init_crs_mismatch_falls_back_to_gsd_ratio():
    src = _product(gt=(1000.0, 2.0, 0.0, 5000.0, 0.0, -2.0), crs="EPSG:32601",
                   gsd=2.0, shape=(200, 300))
    ref = _product(gt=(900.0, 1.0, 0.0, 5200.0, 0.0, -1.0), crs="ESRI:104903",
                   gsd=1.0, shape=(400, 600))
    H, info = coarse_init_info(src, ref)
    assert info["method"] == "gsd_ratio"
    assert info["crs_match"] is False
    assert "CRS" in info["reason"]
    assert H[0, 0] == pytest.approx(2.0) and H[1, 1] == pytest.approx(2.0)
    # Centred: the source centre lands on the reference centre.
    assert np.allclose(apply_transform(H, [[149.5, 99.5]]), [[299.5, 199.5]], atol=1e-9)


def test_coarse_init_degrades_to_identity_and_never_raises():
    bare_src, bare_ref = _product(), _product()
    H, info = coarse_init_info(bare_src, bare_ref)
    assert info["method"] == "identity"
    assert np.allclose(H, np.eye(3))

    junk = Product(path="junk", array=None, meta={"geotransform": "not a transform",
                                                  "gsd_m": "nan", "crs": None})
    H, info = coarse_init_info(junk, junk)
    assert info["method"] == "identity" and np.allclose(H, np.eye(3))
    assert info["crs_match"] is None

    singular = _product(gt=(0.0, 0.0, 0.0, 0.0, 0.0, 0.0), crs="EPSG:32601", gsd=1.0)
    H, info = coarse_init_info(singular, singular)
    assert info["method"] == "gsd_ratio"          # singular gt, but GSD is known
    assert np.allclose(coarse_init(None, None), np.eye(3))   # not even a Product


def test_project_box():
    H = np.array([[2.0, 0.0, 100.0], [0.0, 2.0, 200.5], [0.0, 0.0, 1.0]])
    assert project_box(H, 0.0, 0.0, 10.0, 20.0) == pytest.approx((100.0, 200.5, 120.0, 240.5))

    # 90 degree rotation: the bbox is the rotated corner hull, not the input box.
    R = np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])
    assert project_box(R, 0.0, 0.0, 10.0, 4.0) == pytest.approx((-4.0, 0.0, 0.0, 10.0))


# ------------------------------------------------------------------------- verify


def test_rmse_matches_injected_noise_in_source_pixels():
    """RMSE is Euclidean and in source pixels: sqrt(2)*sigma_ref / scale."""
    matches, gt_H = generate_fake_matches(num_points=300, noise_std=0.5,
                                          outlier_fraction=0.2, seed=7)
    reg = verify_matches(matches)
    assert reg.metrics["status"] == "ok"

    scale = np.sqrt(abs(np.linalg.det(gt_H[:2, :2])))       # 1.2
    expected = np.sqrt(2.0) * 0.5 / scale                   # ~0.589
    assert reg.metrics["rmse_px"] == pytest.approx(expected, rel=0.2)
    assert reg.metrics["residual_units"] == "source_px"

    # The two bugs this replaced: reference-frame residuals would be `scale` times
    # larger, per-component RMS would be sqrt(2) times smaller. Both are excluded.
    assert reg.metrics["rmse_px"] < expected * scale * 0.95
    assert reg.metrics["rmse_px"] > expected / np.sqrt(2.0) * 1.05

    assert reg.metrics["inlier_count"] >= 0.75 * 300 * 0.8
    assert reg.inliers.shape == (300,) and reg.residuals.shape == (300, 2)
    assert reg.sigma.shape == (300,) and np.all(reg.sigma == 0.0)
    assert reg.metrics["model_margin"] == 0.10


def test_residuals_are_in_source_pixels_under_2x_scale():
    """One point displaced 20 px in the reference must show a 10 px source residual."""
    rng = np.random.default_rng(3)
    src = rng.uniform(0.0, 500.0, size=(120, 2))
    ref = 2.0 * src
    ref[5] += np.array([20.0, 0.0])

    reg = verify_matches(_matchset(src, ref))
    assert reg.metrics["status"] == "ok"
    assert reg.metrics["scale_src_to_ref"] == pytest.approx(2.0, rel=1e-3)
    assert np.linalg.norm(reg.residuals[5]) == pytest.approx(10.0, abs=0.05)
    assert not reg.inliers[5]
    others = np.delete(np.linalg.norm(reg.residuals, axis=1), 5)
    assert others.max() < 1e-6


def test_ladder_picks_similarity_for_similarity_data():
    matches, _ = generate_fake_matches(num_points=250, noise_std=0.4,
                                       outlier_fraction=0.15, seed=11)
    reg = verify_matches(matches)
    assert reg.model_type == "similarity"
    c = reg.metrics["model_candidates"]
    assert set(c) == {"similarity", "affine", "homography"}
    # Selected because it is within the stated margin of the best, not because it won.
    best = min(v["rmse_common_px"] for v in c.values())
    assert c["similarity"]["rmse_common_px"] <= best * 1.10 + 1e-9


def test_ladder_picks_homography_for_projective_data():
    rng = np.random.default_rng(5)
    src = rng.uniform(50.0, 950.0, size=(250, 2))
    H_gt = np.array([[1.05, 0.02, 30.0],
                     [-0.03, 0.98, -20.0],
                     [2.0e-4, 1.2e-4, 1.0]])
    ref = apply_transform(H_gt, src) + rng.normal(0.0, 0.2, size=(250, 2))

    reg = verify_matches(_matchset(src, ref))
    assert reg.model_type == "homography"
    assert reg.metrics["rmse_px"] < 1.0
    # Compare where it matters — projected positions, not raw matrix entries.
    assert np.abs(apply_transform(reg.params, src) - apply_transform(H_gt, src)).max() < 0.2


def test_pinned_model_is_respected():
    matches, _ = generate_fake_matches(num_points=200, noise_std=0.3,
                                       outlier_fraction=0.1, seed=13)
    reg = verify_matches(matches, {"model": "affine"})
    assert reg.model_type == "affine"
    assert set(reg.metrics["model_candidates"]) == {"affine"}

    junk = verify_matches(matches, {"model": "quadratic"})   # unknown pin -> auto
    assert junk.model_type in ("similarity", "affine", "homography")
    assert junk.metrics["model_request_ignored"] == "quadratic"


def test_init_gate_rejects_matches_the_init_already_disproves():
    matches, gt_H = generate_fake_matches(num_points=200, noise_std=0.4,
                                          outlier_fraction=0.25, seed=17)
    reg = verify_matches(matches, {"init_gate_px": 20.0}, init=gt_H)
    assert reg.metrics["init_used"] is True
    assert reg.metrics["init_gate_fallback"] is False
    # 50 injected outliers; the gate should have caught nearly all of them.
    assert reg.metrics["init_gated_out"] >= 40
    assert not reg.inliers[:50].any()
    assert np.allclose(reg.init_params, gt_H)


def test_bad_init_does_not_kill_every_match():
    matches, _ = generate_fake_matches(num_points=200, noise_std=0.4,
                                       outlier_fraction=0.1, seed=19)
    bad = np.array([[1.0, 0.0, 90000.0], [0.0, 1.0, 90000.0], [0.0, 0.0, 1.0]])
    reg = verify_matches(matches, {"init_gate_px": 5.0}, init=bad)
    assert reg.metrics["init_gate_fallback"] is True
    assert reg.metrics["status"] == "ok"
    assert reg.metrics["inlier_count"] > 100

    # A structurally invalid init is rejected outright rather than used.
    reg2 = verify_matches(matches, init=np.zeros((3, 3)))
    assert reg2.metrics["init_rejected"] is True
    assert reg2.metrics["init_used"] is False


@pytest.mark.parametrize("name", ["empty", "three_points", "collinear", "identical",
                                  "nan_coords", "length_mismatch"])
def test_degenerate_inputs_return_a_failed_registration(name):
    rng = np.random.default_rng(23)
    if name == "empty":
        ms = _matchset(np.zeros((0, 2)), np.zeros((0, 2)))
    elif name == "three_points":
        p = rng.uniform(0, 100, size=(3, 2))
        ms = _matchset(p, p + 1.0)
    elif name == "collinear":
        x = np.linspace(0.0, 900.0, 60)
        p = np.stack([x, 0.5 * x + 3.0], axis=1)
        ms = _matchset(p, 1.5 * p + 10.0)
    elif name == "identical":
        p = np.tile([5.0, 5.0], (20, 1))
        ms = _matchset(p, p)
    elif name == "nan_coords":
        p = rng.uniform(0, 100, size=(30, 2))
        q = p * 1.1
        q[4] = np.nan
        ms = _matchset(p, q)
    else:
        ms = MatchSet(src_xy=rng.uniform(0, 100, size=(10, 2)),
                      ref_xy=rng.uniform(0, 100, size=(6, 2)),
                      score=np.ones(10, dtype=np.float32),
                      method=np.zeros(10, dtype=np.uint8),
                      cell=np.zeros(10, dtype=np.int32))

    n = len(ms.src_xy)
    reg = verify_matches(ms)
    assert isinstance(reg, Registration)
    assert reg.model_type == "failed"
    assert reg.metrics["status"] == "failed" and reg.metrics["reason"]
    assert np.isnan(reg.metrics["rmse_px"])          # never a fabricated 0.0
    assert reg.metrics["inlier_count"] == 0
    assert reg.inliers.shape == (n,) and not reg.inliers.any()
    assert reg.residuals.shape == (n, 2)
    assert reg.sigma.shape == (n,)
    assert reg.params.shape == (3, 3) and np.isfinite(reg.params).all()


def test_all_outliers_returns_without_pretending_to_succeed():
    rng = np.random.default_rng(29)
    ms = _matchset(rng.uniform(0, 900, size=(80, 2)), rng.uniform(0, 900, size=(80, 2)))
    reg = verify_matches(ms)
    assert isinstance(reg, Registration)
    assert reg.inliers.shape == (80,) and reg.residuals.shape == (80, 2)
    if reg.metrics["status"] == "ok":
        assert reg.metrics["inlier_ratio"] < 0.5
    else:
        assert reg.model_type == "failed"


# --- redundancy gate ---------------------------------------------------------------
# A model supported by exactly its own minimal sample passes through those points by
# construction. Its RMSE is ~0 no matter how wrong the transform is, which is the
# easiest way in this project to put a meaningless sub-pixel number on a slide.

def test_exactly_determined_homography_is_rejected():
    """4 inliers for an 8-dof homography must not be reported as a sub-pixel fit."""
    from samanvay.types import MatchSet
    from samanvay.geometry.verify import verify_matches

    rng = np.random.RandomState(0)
    # 4 consistent points plus 40 pure outliers: any homography that keeps only the
    # 4 will show rmse ~ 0.
    good_src = np.array([[10.0, 10.0], [200.0, 12.0], [15.0, 190.0], [205.0, 195.0]])
    good_ref = good_src * 1.5 + np.array([7.0, -3.0])
    bad_src = rng.uniform(0, 256, size=(40, 2))
    bad_ref = rng.uniform(0, 256, size=(40, 2))

    src = np.vstack([good_src, bad_src])
    ref = np.vstack([good_ref, bad_ref])
    matches = MatchSet(src_xy=src, ref_xy=ref,
                       score=np.ones(len(src), dtype=np.float32),
                       method=np.zeros(len(src), dtype=np.uint8),
                       cell=np.zeros(len(src), dtype=np.int32))

    reg = verify_matches(matches, {"model": "homography"})
    rmse = reg.metrics.get("rmse_px")
    if reg.model_type == "failed":
        return                                   # rejected outright: the honest outcome
    # If a model was returned at all it must carry real redundancy, and a
    # low-redundancy fit must never be silently presented as a good RMSE.
    assert reg.metrics.get("redundancy", 0) >= 1
    if rmse is not None and rmse < 0.5:
        assert reg.metrics.get("rmse_trustworthy") is True, (
            "a near-zero RMSE was reported without flagging its redundancy")


def test_a_well_supported_fit_is_marked_trustworthy():
    """The guard must not fire on a healthy fit — otherwise it just adds noise."""
    from samanvay.geometry.verify import verify_matches
    from bench.fake_matches import generate_fake_matches

    matches, _ = generate_fake_matches(num_points=120, noise_std=0.3,
                                       outlier_fraction=0.1, seed=7)
    reg = verify_matches(matches, {})
    assert reg.model_type != "failed"
    assert reg.metrics["redundancy"] >= 3
    assert reg.metrics["rmse_trustworthy"] is True


# --- P1.4: the control/check split -------------------------------------------------
# The split is the one thing standing between "our RMSE is 0.4 px" and a number the fit
# produced about itself. These tests pin the three properties that make it mean anything:
# it is reproducible, the fit genuinely never sees a check point, and turning it off
# leaves the pre-split engine untouched.


def _split_set(n=300, seed=7):
    matches, gt_H = generate_fake_matches(num_points=n, noise_std=0.5,
                                          outlier_fraction=0.2, seed=seed)
    return matches, gt_H


def test_split_is_reproducible_and_independent_of_match_order():
    matches, _ = _split_set()
    a = verify_matches(matches, {})
    b = verify_matches(matches, {})
    assert a.metrics["check_status"] == "ok"
    assert np.array_equal(a.roles, b.roles)                  # byte for byte on a rerun

    # The hash is over coordinates, not positions in the array, so shuffling the match
    # order must hold out the SAME points. A seeded RNG would fail this outright.
    perm = np.random.default_rng(0).permutation(len(matches.src_xy))
    shuffled = MatchSet(src_xy=matches.src_xy[perm], ref_xy=matches.ref_xy[perm],
                        score=matches.score[perm], method=matches.method[perm],
                        cell=matches.cell[perm])
    c = verify_matches(shuffled, {})
    held_a = {tuple(p) for p in matches.src_xy[a.roles == 1]}
    held_c = {tuple(p) for p in shuffled.src_xy[c.roles == 1]}
    assert held_a == held_c and len(held_a) == a.metrics["n_check"]


def test_split_is_stratified_over_the_grid():
    matches, _ = _split_set()
    reg = verify_matches(matches, {})
    cells = np.asarray(matches.cell)
    populated = {int(c) for c in np.unique(cells) if (cells == c).sum() >= 5}
    held = {int(c) for c in np.unique(cells[reg.roles == 1])}
    # Every cell with points to spare contributes: a check set sitting in one corner
    # would measure that corner, not the frame.
    assert populated and held >= populated


def test_the_fit_never_sees_a_check_point():
    """The fitted transform must be bit-identical to one fitted on the control set alone."""
    matches, _ = _split_set()
    # The spline is off on both sides on purpose: this test is about the split, and a
    # model_type that gained "+tps" on one side only would fail it for another reason.
    reg = verify_matches(matches, {"tps": False})
    control = reg.roles == 0
    only_control = MatchSet(src_xy=matches.src_xy[control], ref_xy=matches.ref_xy[control],
                            score=matches.score[control], method=matches.method[control],
                            cell=matches.cell[control])
    alone = verify_matches(only_control, {"check_fraction": 0.0, "tps": False})
    assert np.array_equal(reg.params, alone.params)
    assert reg.model_type == alone.model_type
    assert reg.metrics["n_control"] == int(control.sum())
    assert reg.metrics["n_check"] == int((reg.roles == 1).sum())
    assert reg.metrics["n_control"] + reg.metrics["n_check"] == len(matches.src_xy)


def test_check_fraction_zero_reproduces_the_pre_split_engine():
    """Measured against the pre-split module (commit c7c0f17) on the same fixtures.

    The split is the largest change this file has taken; with it off, nothing may move.
    The literals below are that module's output, not this one's.
    """
    expected = {7: 0.5529969723107404, 11: 0.608840457186335, 19: 0.5795066457687573}
    for seed, rmse in expected.items():
        matches, _ = _split_set(seed=seed)
        reg = verify_matches(matches, {"check_fraction": 0.0})
        assert reg.metrics["rmse_px"] == rmse
        assert reg.metrics["check_status"] == "disabled"
        assert reg.metrics["check_rmse_px"] is None      # unknown, never 0.0
        assert reg.roles is None and reg.warp is None
        assert reg.model_type == "similarity"


def test_check_metrics_are_null_when_the_split_would_starve_the_fit():
    rng = np.random.default_rng(31)
    src = rng.uniform(0.0, 500.0, size=(12, 2))
    reg = verify_matches(_matchset(src, 1.5 * src + 4.0), {})
    assert reg.metrics["check_status"] == "skipped_too_few_matches"
    assert reg.metrics["n_check"] == 0
    assert reg.roles is None
    for key in ("check_rmse_px", "check_rmse_all_px", "check_p90_px",
                "check_outlier_frac"):
        assert reg.metrics[key] is None


def test_a_good_check_point_counts_toward_the_delivered_inliers():
    matches, _ = _split_set()
    reg = verify_matches(matches, {})
    check = reg.roles == 1
    mag = np.hypot(reg.residuals[:, 0], reg.residuals[:, 1])
    thresh = reg.metrics["ransac_thresh_px"]
    # Coverage and dispersion measure the tie-points we hand over, so a held-out point
    # that lands inside the threshold is delivered and must be an inlier.
    assert np.array_equal(reg.inliers[check], mag[check] <= thresh)
    assert reg.metrics["n_check_inlier"] == int((check & (mag <= thresh)).sum())
    assert reg.metrics["inlier_count"] == int(reg.inliers.sum())
    assert reg.metrics["check_outlier_frac"] == pytest.approx(
        1.0 - reg.metrics["n_check_inlier"] / reg.metrics["n_check"])
    # check_rmse_px is over the check inliers only; check_rmse_all_px keeps the outliers.
    assert reg.metrics["check_rmse_px"] <= reg.metrics["check_rmse_all_px"]
    assert reg.metrics["check_p90_px"] > 0.0


def test_roles_cover_every_match_including_the_ones_the_init_gate_dropped():
    matches, gt_H = _split_set()
    reg = verify_matches(matches, {"init_gate_px": 20.0}, init=gt_H)
    assert reg.metrics["init_gated_out"] > 0
    assert reg.roles.shape == (len(matches.src_xy),)
    assert set(np.unique(reg.roles)) <= {0, 1}
    # A point the gate dropped was never held out from anything, so it is control: the
    # check count is exactly the number of role-1 points, gated points included nowhere.
    assert int((reg.roles == 1).sum()) == reg.metrics["n_check"]
    assert reg.metrics["n_control"] + reg.metrics["n_check"] <= len(matches.src_xy)


# --- the spline, and the hold-out test that makes it safe ---------------------------


def _relief_matches(n=400, amplitude=6.0, noise=0.1, seed=101):
    """A projective pair plus a smooth bulge no homography can absorb — synthetic relief."""
    rng = np.random.default_rng(seed)
    src = rng.uniform(0.0, 1000.0, size=(n, 2))
    H = np.array([[1.2, 0.0, 15.0], [0.0, 1.2, -8.0], [0.0, 0.0, 1.0]])
    bump = np.exp(-((src - 500.0) ** 2).sum(axis=1) / (2.0 * 250.0 ** 2))
    src_deformed = src + np.stack([amplitude * bump, -0.66 * amplitude * bump], axis=1)
    ref = apply_transform(H, src_deformed) + rng.normal(0.0, noise, size=(n, 2))
    cell = ((src[:, 0] // 250).astype(np.int32) + 4 * (src[:, 1] // 250).astype(np.int32))
    return MatchSet(src_xy=src, ref_xy=ref, score=np.ones(n, dtype=np.float32),
                    method=np.zeros(n, dtype=np.uint8), cell=cell)


def test_tps_is_rejected_when_it_does_not_improve_held_out_error():
    """Pure similarity plus noise: there is no relief to absorb, so the spline must go.

    Swept over the contamination of the check set, because that is what decides whether
    check_rmse_all_px can see the spline at all — un-thresholded, it is mostly a
    measurement of the gross mismatches. On check_rmse_all_px alone the verdict here is
    0/15, 4/15 and 7/15 wrong at 0%, 5% and 20% outliers; on the shipped pair of
    conditions it is 0/15 at all three.
    """
    for outlier_fraction in (0.0, 0.05, 0.2):
        for seed in (3, 7, 11):
            matches, _ = generate_fake_matches(num_points=300, noise_std=0.5,
                                               outlier_fraction=outlier_fraction,
                                               seed=seed)
            reg = verify_matches(matches, {})
            assert reg.metrics["check_status"] == "ok"
            assert reg.metrics["tps_status"] == "rejected_no_improvement"
            assert reg.metrics["tps_applied"] is False
            assert reg.warp is None and "+tps" not in reg.model_type
            # Both numbers that fed the decision are recorded, and both are held out.
            assert reg.metrics["tps_check_rmse_before_px"] is not None
            assert reg.metrics["tps_check_rmse_after_px"] is not None

            # And rejecting it must leave the registration exactly where it was.
            off = verify_matches(matches, {"tps": False})
            assert reg.metrics["rmse_px"] == off.metrics["rmse_px"]
            assert reg.metrics["check_rmse_all_px"] == off.metrics["check_rmse_all_px"]


def test_an_overfit_spline_cannot_talk_rmse_below_the_injected_noise():
    """The failure the second acceptance condition exists to stop.

    On seed 7 with 20% outliers, check_rmse_all_px is 268.66 px and an overfit spline
    "improves" it by 0.013% — noise on the gross mismatches, not evidence about the
    model. Accepting on that alone drops in-sample rmse_px from 0.5530 to 0.4670, i.e.
    21% BELOW the sqrt(2)*0.5/1.2 = 0.589 px of noise actually injected, which is a fit
    reporting an accuracy the data cannot contain. The spline must be rejected and
    rmse_px must stay at the noise floor.
    """
    matches, gt_H = _split_set(seed=7)
    reg = verify_matches(matches, {})
    injected = np.sqrt(2.0) * 0.5 / np.sqrt(abs(np.linalg.det(gt_H[:2, :2])))
    assert reg.metrics["check_rmse_all_px"] > 100.0     # outlier-dominated, as designed
    assert reg.warp is None
    assert reg.metrics["rmse_px"] == pytest.approx(injected, rel=0.2)


def test_tps_is_applied_only_when_it_improves_points_it_never_saw():
    matches = _relief_matches()
    off = verify_matches(matches, {"tps": False})
    on = verify_matches(matches, {})

    assert off.metrics["tps_status"] == "disabled" and off.warp is None
    assert on.metrics["tps_status"] == "applied"
    assert on.model_type == off.model_type + "+tps"
    assert on.warp is not None and on.warp.n_control == on.metrics["tps_n_control"]

    # The decision is recorded with the two numbers that made it, both on held-out points.
    before = on.metrics["tps_check_rmse_before_px"]
    after = on.metrics["tps_check_rmse_after_px"]
    assert after < before
    assert before == pytest.approx(off.metrics["check_rmse_all_px"], rel=1e-9)
    assert on.metrics["check_rmse_all_px"] == pytest.approx(after, rel=1e-9)
    # 6 px of relief: the spline takes the held-out error from ~1.7 px to ~0.18 px.
    assert before > 1.0 and after < 0.5

    # Residuals must be the DELIVERED model's, not the global part of it — writers.py
    # and metrics.py both quote them.
    from samanvay.geometry.tps import pullback
    full = pullback(on.params, matches.ref_xy, on.warp) - matches.src_xy
    assert np.allclose(on.residuals, full, atol=1e-12)


def test_tps_needs_control_points_and_says_so_when_it_has_too_few():
    rng = np.random.default_rng(37)
    src = rng.uniform(0.0, 500.0, size=(20, 2))
    reg = verify_matches(_matchset(src, 1.4 * src + 3.0), {"tps_min_control": 25})
    assert reg.metrics["tps_status"] == "too_few_control"
    assert reg.metrics["tps_applied"] is False and reg.warp is None
    assert reg.metrics["tps_check_rmse_before_px"] is None


def test_tps_false_never_fits_a_spline():
    reg = verify_matches(_relief_matches(), {"tps": False})
    assert reg.metrics["tps_status"] == "disabled"
    assert reg.metrics["tps_check_rmse_before_px"] is None
    assert reg.warp is None


# --- the spline itself --------------------------------------------------------------


def test_tps_reproduces_a_known_displacement_field_and_round_trips():
    from samanvay.geometry.tps import ThinPlateSpline, fit_tps, pullback

    rng = np.random.default_rng(5)
    node = rng.uniform(0.0, 100.0, size=(60, 2))
    # A bump, not a linear ramp: an affine field is reproduced by the spline's affine
    # part alone and would say nothing about the kernel or about lam.
    bump = np.exp(-((node - 50.0) ** 2).sum(axis=1) / (2.0 * 25.0 ** 2))
    field = np.stack([2.0 * bump, -1.5 * bump], axis=1)
    warp = fit_tps(node + field, node, lam=0.0)
    assert warp is not None and warp.n_control == 60
    assert np.allclose(warp.displacement(node), field, atol=1e-6)
    assert np.allclose(warp.apply(node), node + field, atol=1e-6)

    back = ThinPlateSpline.from_dict(warp.to_dict())
    assert np.allclose(back.displacement(node), warp.displacement(node), atol=1e-12)
    assert warp.to_dict()["type"] == "thin_plate_spline"

    # lam stiffens: at lam=0 the spline interpolates its control points exactly, at
    # lam=50 it is pulled towards the affine part and no longer passes through them.
    stiff = fit_tps(node + field, node, lam=50.0)
    assert (np.abs(stiff.displacement(node) - field).max()
            > 100.0 * np.abs(warp.displacement(node) - field).max())
    assert stiff.lam == 50.0 and warp.lam == 0.0

    # pullback with no warp is exactly inv(H) @ ref; with one it adds the displacement.
    H = np.array([[2.0, 0.0, 10.0], [0.0, 2.0, -5.0], [0.0, 0.0, 1.0]])
    ref = apply_transform(H, node)
    assert np.allclose(pullback(H, ref), node, atol=1e-9)
    assert np.allclose(pullback(H, ref, warp), node + field, atol=1e-6)
    assert pullback(np.zeros((3, 3)), ref) is None            # singular, not an exception


def test_fit_tps_returns_none_instead_of_raising():
    from samanvay.geometry.tps import fit_tps

    assert fit_tps(np.zeros((3, 2)), np.zeros((3, 2))) is None        # too few points
    assert fit_tps(np.zeros((5, 2)), np.zeros((4, 2))) is None        # length mismatch
    assert fit_tps(np.zeros((8, 2)), np.zeros((8, 2))) is None        # all in one place
    bad = np.tile([[1.0, 2.0]], (8, 1)).copy()
    bad[0] = np.nan
    assert fit_tps(bad, np.arange(16, dtype=float).reshape(8, 2)) is None
    assert fit_tps("not points", "either") is None
