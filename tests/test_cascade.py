import cv2
import numpy as np
import pytest

from samanvay.types import CanonicalImage, MatchSet, Registration
from samanvay.geometry.init import apply_transform
from samanvay.match import cascade
from samanvay.match import tile
from samanvay.match.cascade import match_cascade, chain_registrations, plan_levels
from samanvay.photometry.phasecong import phase_congruency


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


def test_an_unmeetable_seeding_bar_skips_every_seed_but_still_delivers(pair_2x):
    """The bar stops a weak fit SEEDING a finer level; it must not eat the result.

    This test used to assert the opposite — that an unmeetable bar returns zero matches.
    That contradicted the module's own guarantee that "the cascade is never worse than
    not cascading", and on an 80x OHRC-class pair, where the size cap forces K = 1 and
    the only level IS the finest, it made the cascade return 0 matches where the same
    pair with the cascade disabled fitted a similarity at gt_rmse_px 0.543.
    """
    src, ref, _ = pair_2x
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    cfg = {"method": "sift", "grid_n": 3, "halo_px": 32,
           "cascade": {"min_seed_inliers": 10 ** 6}}       # nothing can clear this bar
    matches, info = match_cascade(src, ref, config=cfg, init=init)

    # Every level that had a finer level below it refused to seed it.
    seeding = [l for l in info["level_info"] if int(l["level"]) > 0]
    assert seeding, "fixture must have more than one level for this to mean anything"
    assert all(l["status"] == "skipped" for l in seeding)
    assert "below the seeding bar" in " ".join(
        str(l.get("reason") or "") for l in seeding)

    # The finest level had nothing to seed, so it delivered instead of being discarded.
    finest = [l for l in info["level_info"] if int(l["level"]) == 0]
    assert finest and finest[0]["status"] == "ok"
    assert len(matches.src_xy) > 0
    assert info["transform"] is not None

    # The guarantee, stated as an assertion: no worse than not cascading.
    direct, _ = match_cascade(src, ref, init=init,
                              config={"method": "sift", "grid_n": 3, "halo_px": 32,
                                      "cascade": {"max_levels": 1}})
    assert len(matches.src_xy) >= len(direct.src_xy) or len(direct.src_xy) == 0


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


# ------------------------------------------------------- what the level hands back --

def _budgets(n_cells, max_matches, min_matches=3):
    return {i: {"min_matches": min_matches, "max_matches": max_matches}
            for i in range(n_cells)}


def test_cell_info_is_the_level_that_produced_the_returned_matches(pair_4x):
    src, ref, _ = pair_4x
    init = np.array([[0.25, 0, 0], [0, 0.25, 0], [0, 0, 1.0]])
    matches, info = match_cascade(src, ref, config={"method": "sift", "grid_n": 3,
                                                    "halo_px": 32}, init=init)
    assert info["status"] == "ok"
    cells = info["cell_info"]
    assert cells is not None                        # null on every default run before this
    # Levels replace rather than merge, so the authoritative cell state is the finest
    # successful level's — the one whose ids `matches.cell` carries.
    assert info["cell_info_level"] == info["final_level"]
    assert cells["level"] == info["final_level"]
    assert cells["src_decimation"] == pytest.approx(info["source_decimation"])
    assert set(np.unique(matches.cell)) <= set(cells["cells"])
    # Per-cell counts describe the returned set, not some earlier level's.
    for cid, cell in cells["cells"].items():
        assert cell["count"] == int(np.count_nonzero(matches.cell == cid))
    assert sum(c["count"] for c in cells["cells"].values()) == len(matches.src_xy)
    assert any(c["status"] == "populated" for c in cells["cells"].values())
    # The boxes are in the level's own pixels, and it says so rather than leaving a
    # reader to assume full resolution.
    assert "level pixels" in cells["coords_frame"]
    core = cells["cells"][0]["src_core"]
    # Anchor on the FINAL level, not on level_info[-1]: the last entry is the last level
    # ATTEMPTED, which on a stopped descent is the rejected one and a strictly larger
    # frame — an assertion against it passes even when cell_info came from the wrong
    # level. test_cell_info_follows_a_stopped_descent covers that case.
    lvl0 = [l for l in info["level_info"] if l["level"] == info["cell_info_level"]][0]
    assert core[2] <= lvl0["src_shape"][1]
    # The frame is published as the level's SHAPE, not as the nominal decimation:
    # _level_shape rounds, so src_decimation is not always the achieved factor and a
    # reader scaling src_core by it lands off the edge (858/4.0 -> 214, factor 4.0093
    # on fixtures/dsun_sweep/dsun_50 level 1).
    final = [l for l in info["level_info"] if l["level"] == info["cell_info_level"]][0]
    assert tuple(cells["src_shape"]) == tuple(final["src_shape"])
    assert tuple(cells["ref_shape"]) == tuple(final["ref_shape"])


def test_cell_info_stays_null_when_no_level_succeeded(pair_2x):
    src, ref, _ = pair_2x
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    cfg = {"method": "sift", "grid_n": 3, "halo_px": 32,
           "cascade": {"min_seed_inliers": 10 ** 6}}
    matches, info = match_cascade(src, ref, config=cfg, init=init)
    # The finest level is exempt from the seeding bar (nothing below it to seed), so it
    # succeeds and its cells are the ones that describe the returned points.
    assert info["status"] == "ok" and len(matches.src_xy) > 0
    assert info["cell_info"] is not None
    assert int(info["cell_info_level"]) == 0


@pytest.mark.parametrize("anms", [True, False])
def test_the_anms_gate_reaches_the_cells_of_the_final_level(pair_4x, anms):
    src, ref, _ = pair_4x
    init = np.array([[0.25, 0, 0], [0, 0.25, 0], [0, 0, 1.0]])
    # A quota of 4 per cell bites on this pair, so the selection rule actually runs.
    cfg = {"method": "sift", "grid_n": 3, "halo_px": 32, "anms": anms,
           "cell_budgets": _budgets(9, max_matches=4)}
    matches, info = match_cascade(src, ref, config=cfg, init=init)
    cells = info["cell_info"]
    assert cells is not None and cells["anms"] is anms
    bit = [c["anms"] for c in cells["cells"].values() if c["anms"] is not None]
    assert bit and set(bit) == {anms}
    assert all(c["count"] <= 4 for c in cells["cells"].values())


# ------------------------------------------------ the method actually used per level --

def _canon_pc(albedo):
    """A CanonicalImage with a real phase-congruency map — what the rift arm needs."""
    out = phase_congruency(albedo, nscale=4, norient=6)
    return CanonicalImage(albedo=albedo,
                          pc=np.asarray(out["pc"], np.float32),
                          pc_orient=np.asarray(out["orientation"], np.float32),
                          mask=np.zeros(albedo.shape, np.uint8),
                          params={"nscale": 4, "norient": 6})


def test_the_configured_method_reaches_the_detector_at_every_level(monkeypatch):
    src_a = _texture(512, seed=7)
    H_gt = np.array([[0.5, 0, 4.0], [0, 0.5, -3.0], [0, 0, 1.0]])
    ref_a = cv2.warpPerspective(src_a, H_gt, (256, 256), flags=cv2.INTER_AREA)
    src, ref = _canon_pc(src_a), _canon_pc(ref_a)

    seen = []
    real_detect = tile.detect_keypoints

    def spy(img, method="sift", pc_map=None, **kw):
        seen.append((str(method), pc_map is not None and bool(np.any(pc_map))))
        return real_detect(img, method=method, pc_map=pc_map, **kw)

    monkeypatch.setattr(tile, "detect_keypoints", spy)
    init = np.array([[0.5, 0, 0], [0, 0.5, 0], [0, 0, 1.0]])
    _, info = match_cascade(src, ref, init=init,
                            config={"method": "rift", "grid_n": 2, "halo_px": 16,
                                    "cascade": {"min_level_side": 96}})

    assert info["levels"] == 2                       # a one-level run proves nothing here
    assert len(info["level_info"]) == 2
    # No level re-reads a stale "auto" or drops to the intensity arm: the cascade passes
    # the resolved method straight through, and every level detected on the PC map.
    assert {m for m, _ in seen} == {"rift"}
    assert all(has_pc for _, has_pc in seen)
    assert [l["method"] for l in info["level_info"]] == ["rift", "rift"]
    assert len(seen) >= 2 * 2 * (2 * 2)              # 2 levels x 2 images x 4 cells


def test_cell_info_follows_a_stopped_descent(pair_4x):
    """The finest level FAILS after a coarser one worked: cell_info must be the coarser one.

    This is the only case where "which level's cell_info" can actually be answered wrong.
    On a clean run the finest successful level is level 0 and every candidate coincides,
    so a run that finishes proves nothing; here level 0 is rejected, the returned matches
    come from level 1, and cell_info has to follow the matches rather than the last level
    attempted. Level 0 is sabotaged rather than found in the wild because a stop needs a
    level that fits at 8x decimation and then collapses at 4x, which no synthetic pair
    reliably does.
    """
    src, ref, _ = pair_4x
    init = np.array([[0.25, 0, 0], [0, 0.25, 0], [0, 0, 1.0]])
    cfg = {"method": "sift", "grid_n": 3, "halo_px": 32}
    real = cascade.match_tiled

    def sabotage_finest(src_l, ref_l, **kw):
        m, ci = real(src_l, ref_l, **kw)
        # levels=2 puts the source at 128 px on level 1 and 256 px on level 0.
        return (tile._empty_matchset(), ci) if src_l.albedo.shape[0] >= 256 else (m, ci)

    try:
        cascade.match_tiled = sabotage_finest
        matches, info = match_cascade(src, ref, config=cfg, init=init, levels=2)
    finally:
        cascade.match_tiled = real

    assert info["status"] == "stopped"
    assert info["final_level"] == 1                    # level 0 rejected, level 1 kept
    assert info["level_info"][-1]["level"] == 0        # the last ATTEMPTED level is not it
    cells = info["cell_info"]
    assert cells is not None
    assert info["cell_info_level"] == 1 and cells["level"] == 1
    # The frame is the surviving level's, not the rejected one's.
    lvl = [l for l in info["level_info"] if l["level"] == 1][0]
    assert tuple(cells["src_shape"]) == tuple(lvl["src_shape"])
    assert tuple(cells["ref_shape"]) == tuple(lvl["ref_shape"])
    assert cells["src_decimation"] == pytest.approx(info["source_decimation"])
    # ...and the counts describe the points that were actually returned.
    for cid, cell in cells["cells"].items():
        assert cell["count"] == int(np.count_nonzero(matches.cell == cid))
    assert sum(c["count"] for c in cells["cells"].values()) == len(matches.src_xy) > 0
    assert max(c["src_core"][2] for c in cells["cells"].values()) <= lvl["src_shape"][1]
