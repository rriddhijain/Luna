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
