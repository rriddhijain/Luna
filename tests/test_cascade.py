import cv2
import numpy as np
import pytest

from samanvay.types import CanonicalImage, MatchSet, Registration
from samanvay.geometry.init import apply_transform
from samanvay.match import cascade
from samanvay.match.cascade import match_cascade, chain_registrations, plan_levels


# ----------------------------------------------------------------- synthetic pairs --

def _texture(n, seed=11):
    """Multi-scale blurred noise — structure that survives decimation, like terrain does."""
    rng = np.random.default_rng(seed)
    base = rng.normal(size=(n, n)).astype(np.float32)
    img = np.zeros((n, n), np.float32)
    for s in (1.5, 4.0, 12.0):
        img += cv2.GaussianBlur(base, (0, 0), s) * s
    img -= img.min()
    return (img / max(img.max(), 1e-6)).astype(np.float32)


def _canon(albedo):
    z = np.zeros(albedo.shape, np.float32)
    return CanonicalImage(albedo=albedo, pc=z, pc_orient=z.copy(),
                          mask=np.zeros(albedo.shape, np.uint8), params={})


def _pair(scale, src_n, seed=11, rot_deg=0.0, tx=0.0, ty=0.0):
    """(source, reference, GT source->reference). The reference is an area-decimated warp."""
    src = _texture(src_n, seed)
    th = np.deg2rad(rot_deg)
    R = np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]]) * scale
    H = np.array([[R[0, 0], R[0, 1], tx], [R[1, 0], R[1, 1], ty], [0, 0, 1.0]])
    n = int(round(src_n * scale))
    ref = cv2.warpPerspective(src, H, (n, n), flags=cv2.INTER_AREA)
    return _canon(src), _canon(ref), H


def _corner_err_src_px(H_est, H_gt, shape):
    """Max corner error in SOURCE px: the gt-projected corner pulled back through H_est."""
    h, w = shape
    c = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float)
    back = apply_transform(np.linalg.inv(np.asarray(H_est, float)), apply_transform(H_gt, c))
    return float(np.max(np.linalg.norm(back - c, axis=1)))


@pytest.fixture(scope="module")
def pair_4x():
    return _pair(0.25, 1024, seed=11, rot_deg=3.0, tx=6.0, ty=-4.0)


@pytest.fixture(scope="module")
def pair_2x():
    return _pair(0.5, 800, seed=5, rot_deg=2.0, tx=5.0, ty=3.0)


# ------------------------------------------------------------------ the level rule --

def _roomy(r, ref_n=4096):
    """Shapes for a ratio-r pair with enough pixels that the size cap cannot bind."""
    return (int(ref_n * r), int(ref_n * r)), (ref_n, ref_n)


def test_level_count_follows_the_scale_ratio():
    # One extra level per 4x of scale ratio, with plenty of pixels to spend.
    assert plan_levels(1.0, *_roomy(1)) == 1        # same resolution: no pyramid needed
    assert plan_levels(0.5, *_roomy(2)) == 2        # 2x
    assert plan_levels(0.25, *_roomy(4)) == 2       # 4x — still one step
    assert plan_levels(1.0 / 8, *_roomy(8)) == 3    # 8x
    assert plan_levels(1.0 / 64, *_roomy(64)) == 4  # 64x
    assert plan_levels(1.0 / 512, *_roomy(512)) == 5    # 512x wants 6, the pixels allow 5
    assert plan_levels(1.0 / 2048, (2 ** 24, 2 ** 24), (8192, 8192)) == 6   # max_levels cap
    # Symmetric: it is the RATIO that matters, not which side is finer.
    for r in (2.0, 8.0, 64.0):
        src, ref = _roomy(r)
        assert plan_levels(r, ref, src) == plan_levels(1.0 / r, src, ref)
    # Monotone in the ratio.
    counts = [plan_levels(1.0 / r, *_roomy(r)) for r in (1, 2, 8, 32, 128, 512)]
    assert counts == sorted(counts)


def test_level_count_is_capped_by_the_pixels_actually_available():
    # A 64x ratio wants 4 levels; whether it gets them depends on the pixels available.
    assert plan_levels(1.0 / 64, (262144, 262144), (4096, 4096)) == 4
    assert plan_levels(1.0 / 64, (32768, 32768), (512, 512)) == 2
    assert plan_levels(1.0 / 64, (6400, 6400), (100, 100)) == 1
    assert plan_levels(float("nan"), (512, 512), (512, 512)) == 1
    assert plan_levels(0.0, (512, 512), (512, 512)) == 1


# --------------------------------------------------------------------- the cascade --

def test_cascade_recovers_a_known_homography_on_a_4x_pair(pair_4x):
    src, ref, H_gt = pair_4x
    init = np.array([[0.25, 0, 0], [0, 0.25, 0], [0, 0, 1.0]])   # scale only: no rotation, no shift
    matches, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3,
                                                    "halo_px": 32}, init=init)
    assert info["status"] == "ok"
    assert info["scale_ratio"] == pytest.approx(4.0, rel=1e-6)
    assert info["final_level"] == 0
    assert len(matches.src_xy) > 0
    # Stated tolerance: 4 source px of max corner error. The finest level still decimates
    # the source 4x to meet the reference, so ~1 reference px is the accuracy ceiling and
    # 4 source px IS about one reference pixel. Measured 0.17 px on this pair.
    assert _corner_err_src_px(info["transform"], H_gt, src.albedo.shape) < 4.0


def test_matches_come_back_at_full_source_resolution(pair_4x):
    src, ref, _ = pair_4x
    init = np.array([[0.25, 0, 0], [0, 0.25, 0], [0, 0, 1.0]])
    matches, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3,
                                                    "halo_px": 32}, init=init)
    assert info["source_decimation"] == pytest.approx(4.0, rel=1e-6)
    finest = [l for l in info["level_info"] if l["status"] == "ok"][-1]
    # The level itself is 256 px wide; the returned coordinates must span the 1024 px source.
    assert finest["src_shape"][1] < 300
    assert matches.src_xy[:, 0].max() > 600
    assert matches.src_xy[:, 0].max() < src.albedo.shape[1]
    assert matches.ref_xy[:, 0].max() < ref.albedo.shape[1]


def test_the_search_margin_contracts_as_the_descent_proceeds(pair_2x):
    src, ref, _ = pair_2x
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    _, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3, "halo_px": 32},
                            init=init)
    assert info["levels"] == 2
    margins = [l["search_margin_px"] for l in info["level_info"]]
    assert margins[0] is None                    # coarsest: match_tiled's own wide margin
    assert margins[1] == pytest.approx(64.0)     # then it contracts
    assert info["level_info"][0]["level"] > info["level_info"][1]["level"]   # coarse to fine


def test_a_failing_level_stops_the_descent_instead_of_propagating_garbage(pair_2x, monkeypatch):
    src, ref, _ = pair_2x
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    _, good_info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3,
                                                   "halo_px": 32}, init=init)
    real = cascade.verify_matches
    seen = []

    def flaky(matches, config=None, init=None):
        seen.append(1)
        reg = real(matches, config, init)
        if len(seen) > 1:            # the finest level "fails" after the coarse one worked
            reg.metrics["status"] = "failed"
            reg.metrics["reason"] = "injected level failure"
            reg.metrics["inlier_count"] = 0
        return reg

    monkeypatch.setattr(cascade, "verify_matches", flaky)
    matches, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3,
                                                    "halo_px": 32}, init=init)
    assert len(seen) == 2                                  # it stopped, it did not carry on
    assert info["status"] == "stopped"
    assert info["final_level"] == 1                         # the coarse level, not the failed one
    assert info["level_info"][-1]["status"] == "rejected"
    assert "injected level failure" in info["stop_reason"]
    # The best result so far is returned, not an empty set and not the failed level's fit.
    assert len(matches.src_xy) > 0
    assert len(matches.src_xy) == info["level_info"][0]["match_count"]
    assert info["source_decimation"] == pytest.approx(4.0, rel=1e-6)   # honestly coarser
    assert not np.allclose(info["transform"], good_info["transform"])


def test_too_few_inliers_to_seed_stops_the_descent(pair_2x):
    src, ref, _ = pair_2x
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    cfg = {"method": "sift", "grid_n": 3, "halo_px": 32,
           "cascade": {"min_seed_inliers": 10 ** 6}}       # nothing can clear this bar
    matches, info = match_cascade(src, ref, config=cfg, init=init)
    assert info["status"] == "failed"
    assert info["transform"] is None
    assert len(matches.src_xy) == 0                        # honest empty, not a guessed fit
    assert "below the seeding bar" in info["stop_reason"]
    assert all(l["status"] == "skipped" for l in info["level_info"])


def test_a_wildly_wrong_scale_is_refused_rather_than_seeded(pair_2x):
    src, ref, _ = pair_2x
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    cfg = {"method": "sift", "grid_n": 3, "halo_px": 32,
           "cascade": {"max_scale_drift": 1.0000001}}      # any drift at all is too much
    matches, info = match_cascade(src, ref, config=cfg, init=init)
    assert info["status"] == "failed"
    assert len(matches.src_xy) == 0
    assert "drifted" in info["stop_reason"]


def test_scale_comes_from_gsd_when_there_is_no_init(pair_2x):
    src, ref, H_gt = pair_2x
    src.params["gsd_m"], ref.params["gsd_m"] = 0.5, 1.0    # source is 2x finer
    matches, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3,
                                                    "halo_px": 32})
    assert info["scale_source"] == "gsd_m"
    assert info["scale_src_to_ref"] == pytest.approx(0.5)
    assert info["levels"] == 2
    if info["status"] == "ok":
        assert _corner_err_src_px(info["transform"], H_gt, src.albedo.shape) < 6.0


def test_no_scale_evidence_stays_one_level_and_says_so():
    src, ref, _ = _pair(1.0, 320, seed=3)
    _, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 2, "halo_px": 16})
    assert info["scale_source"] == "unknown"
    assert info["levels_source"] == "no_scale_evidence"
    assert info["levels"] == 1


# ------------------------------------------------------------- degenerate input --

@pytest.mark.parametrize("shape", [(0, 0), (0, 32), (4, 4)])
def test_cascade_never_crashes_on_degenerate_images(shape):
    a = np.zeros(shape, np.float32)
    src = ref = _canon(a)
    matches, info = match_cascade(src, ref, init=np.eye(3))
    assert isinstance(matches, MatchSet)
    assert len(matches.src_xy) == 0
    assert info["status"] == "failed"
    assert info["transform"] is None
    assert info["stop_reason"]


def test_cascade_on_flat_images_returns_an_honest_empty_result():
    src = _canon(np.full((256, 256), 0.5, np.float32))
    ref = _canon(np.full((128, 128), 0.5, np.float32))
    matches, info = match_cascade(src, ref, init=np.array([[.5, 0, 0], [0, .5, 0], [0, 0, 1.]]))
    assert len(matches.src_xy) == 0
    assert info["status"] == "failed"
    assert np.isnan(info["level_info"][-1]["rmse_px"])


def test_cascade_survives_a_garbage_init():
    src, ref, _ = _pair(0.5, 256, seed=2)
    for bad in (np.zeros((3, 3)), np.full((3, 3), np.nan), np.eye(2)):
        matches, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 2},
                                      init=bad)
        assert isinstance(matches, MatchSet)
        assert info["scale_source"] == "unknown"     # rejected, not trusted


# ------------------------------------------------------------------- the chaining --

def _reg(H, rmse=None, trustworthy=True, redundancy=30):
    metrics = {"status": "ok"}
    if rmse is not None:
        metrics.update({"rmse_px": rmse, "rmse_trustworthy": trustworthy,
                        "redundancy": redundancy})
    return Registration(model_type="affine", params=np.asarray(H, float),
                        init_params=np.eye(3), inliers=np.ones(0, bool),
                        residuals=np.zeros((0, 2)), sigma=np.zeros(0), metrics=metrics)


def test_chain_composes_two_known_transforms_exactly():
    H1 = np.array([[0.5, 0.0, 10.0], [0.0, 0.5, -4.0], [0.0, 0.0, 1.0]])
    H2 = np.array([[2.0, 0.3, -7.0], [-0.1, 1.8, 22.0], [1e-5, 2e-5, 1.0]])
    H, cov, info = chain_registrations([_reg(H1, 1.0), _reg(H2, 1.0)])
    assert np.allclose(H, H2 @ H1)
    assert info["n_hops"] == 2 and info["status"] == "ok"
    # And it composes as a MAP, not just as a matrix product.
    p = np.array([[0.0, 0.0], [37.0, 91.0], [400.0, 12.0]])
    assert np.allclose(apply_transform(H, p), apply_transform(H2, apply_transform(H1, p)))
    assert info["scale_end_to_end"] == pytest.approx(
        np.sqrt(abs(np.linalg.det((H2 @ H1)[:2, :2]))))
    assert cov is not None


def test_a_single_hop_chains_to_itself():
    H1 = np.array([[1.3, 0.1, 4.0], [-0.2, 1.1, 9.0], [0.0, 0.0, 1.0]])
    H, cov, info = chain_registrations([_reg(H1, 2.0)])
    assert np.allclose(H, H1)
    assert cov == pytest.approx(np.eye(2) * 4.0)          # sigma 2 px, untouched
    assert info["sigma_end_to_end_px"] == pytest.approx(2.0)


def test_chained_covariance_is_strictly_larger_than_either_hop():
    th = np.deg2rad(30.0)
    rot = np.array([[np.cos(th), -np.sin(th), 3.0], [np.sin(th), np.cos(th), -2.0],
                    [0.0, 0.0, 1.0]])                    # unit scale: frames are comparable
    H1, H2 = rot, np.array([[1.0, 0.0, 40.0], [0.0, 1.0, 11.0], [0.0, 0.0, 1.0]])
    s1, s2 = 0.5, 0.8
    _, cov, info = chain_registrations([_reg(H1), _reg(H2)], sigmas=[s1, s2])

    assert cov is not None and info["cov_known"]
    c1, c2 = np.eye(2) * s1 ** 2, np.eye(2) * s2 ** 2
    # Strictly larger in the Loewner sense: cov - C_hop is positive definite, both ways.
    for c in (c1, c2):
        assert np.linalg.eigvalsh(cov - c).min() > 1e-12
    assert info["sigma_end_to_end_px"] == pytest.approx(np.hypot(s1, s2))
    assert info["sigma_end_to_end_px"] > max(s1, s2)
    assert info["chain_vs_largest_hop"] > 1.0
    # Per-hop and end-to-end are reported separately, as the pitch requires.
    assert [h["sigma_px"] for h in info["hops"]] == pytest.approx([s1, s2])
    assert info["sigma_largest_hop_px"] == pytest.approx(s2)


def test_a_scale_change_moves_the_downstream_hops_error_into_source_pixels():
    # Hop 1 halves the scale, so a 1 px error in hop 2's frame is 2 px of SOURCE error.
    H1 = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    _, cov, info = chain_registrations([_reg(H1), _reg(np.eye(3))], sigmas=[0.0, 1.0])
    assert info["hops"][1]["sigma_px"] == pytest.approx(1.0)
    assert info["hops"][1]["contrib_sigma_px"] == pytest.approx(2.0)
    assert cov == pytest.approx(np.eye(2) * 4.0)


def test_a_hop_with_no_covariance_yields_none_not_a_number():
    H = np.eye(3)
    # Bare matrices carry no uncertainty at all.
    _, cov, info = chain_registrations([H, H])
    assert cov is None
    assert info["cov_known"] is False
    assert info["sigma_end_to_end_px"] is None
    assert info["cov_missing_hops"] == [0, 1]
    assert "no usable uncertainty" in info["reason"]

    # One known hop and one unknown is still unknown — never a partial sum.
    _, cov, info = chain_registrations([_reg(H, 1.0), H])
    assert cov is None and info["cov_missing_hops"] == [1]
    assert info["hops"][0]["sigma_px"] == pytest.approx(1.0)
    assert info["hops"][1]["sigma_px"] is None


def test_an_untrustworthy_rmse_is_refused_rather_than_believed():
    H = np.eye(3)
    _, cov, info = chain_registrations([_reg(H, 0.001, trustworthy=False, redundancy=1),
                                        _reg(H, 1.0)])
    assert cov is None
    assert info["cov_missing_hops"] == [0]
    assert "untrustworthy" in info["hops"][0]["reason"]
    # Supplying it explicitly is the caller's call to make, and then it is used;
    # `None` for a hop means "use that hop's own trustworthy rmse", not "unknown".
    _, cov, info = chain_registrations([_reg(H, 0.001, trustworthy=False), _reg(H, 1.0)],
                                       sigmas=[0.5, None])
    assert cov == pytest.approx(np.eye(2) * (0.25 + 1.0))
    assert [h["sigma_source"] for h in info["hops"]] == ["caller", "rmse"]


def test_chain_degenerate_inputs_never_crash():
    H, cov, info = chain_registrations([])
    assert np.allclose(H, np.eye(3)) and cov is None and info["status"] == "empty"

    H, cov, info = chain_registrations([np.zeros((3, 3))])       # singular hop
    assert np.allclose(H, np.eye(3)) and cov is None
    assert info["status"] == "failed" and "not a finite invertible" in info["reason"]

    H, cov, info = chain_registrations([np.eye(3)], sigmas=[1.0, 2.0])   # length mismatch
    assert cov is None and info["status"] == "failed"

    # A NaN sigma is "unknown", so the hop falls back to its own trustworthy rmse...
    _, cov, info = chain_registrations([_reg(np.eye(3), 1.0)], sigmas=[float("nan")])
    assert cov == pytest.approx(np.eye(2)) and info["hops"][0]["sigma_source"] == "rmse"
    # ...and with nothing to fall back to, it stays unknown.
    _, cov, info = chain_registrations([np.eye(3)], sigmas=[float("nan")])
    assert cov is None and info["cov_missing_hops"] == [0]
