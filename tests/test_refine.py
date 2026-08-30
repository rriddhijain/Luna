import cv2
import numpy as np
import scipy.ndimage as ndi

from samanvay.types import MatchSet
from samanvay.geometry.refine import refine_matches

PTS = np.array([[64.0, 64.0], [192.0, 64.0], [64.0, 192.0],
                [192.0, 192.0], [128.0, 128.0]])
TRUE_D = np.array([-0.62, 0.37])          # (dx, dy) the reference content is displaced by


def texture(n=256, sigma=1.6, seed=0):
    rng = np.random.default_rng(seed)
    img = ndi.gaussian_filter(rng.normal(size=(n, n)), sigma).astype(np.float32)
    img -= img.min()
    return img / img.max()


# A shifted copy of one image correlates almost perfectly, so phase_cross_correlation
# returns err ~ 0 and geometry/refine.py reads rho ~ 1.0 -- above _RHO_MAX_VALIDATED,
# where the calibrated noise model does not apply and sigma is deliberately NaN.
# These synthetic pairs therefore test the CORRECTION (which is what P4 requires) and
# not the uncertainty. Real pairs sit below the gate: bench/calibrate.py measures 0.0%
# NaN on the fixture arm, and test_realistic_pair_reports_finite_sigma guards that.


def make_set(src_xy, ref_xy):
    n = len(src_xy)
    return MatchSet(src_xy=np.array(src_xy, dtype=np.float64),
                    ref_xy=np.array(ref_xy, dtype=np.float64),
                    score=np.ones(n, dtype=np.float32),
                    method=np.zeros(n, dtype=np.uint8),
                    cell=np.zeros(n, dtype=np.int32))


def test_recovers_known_subpixel_offset():
    src = texture()
    ref = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    # ref_xy starts at the source location: wrong by exactly TRUE_D.
    m = make_set(PTS, PTS.copy())
    out, sigma = refine_matches(m, src, ref, np.eye(3), {"patch": 48})

    assert np.array_equal(out.src_xy, PTS), "src_xy must not move"

    before = np.hypot(*TRUE_D)
    after = np.hypot(*(out.ref_xy - PTS - TRUE_D).T)
    assert after.max() < 0.1, f"residual {after.max():.4f} px"
    assert after.max() < before / 5


def test_refinement_survives_a_scaled_transform():
    # H carries a 2x scale, so the reference patch must be warped back through it
    # before correlating or the correlation peak is meaningless.
    src = texture()
    H = np.array([[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 1.0]])
    big = cv2.warpAffine(src, H[:2], (512, 512), flags=cv2.INTER_CUBIC,
                         borderMode=cv2.BORDER_REFLECT_101)
    ref = ndi.shift(big, (2 * TRUE_D[1], 2 * TRUE_D[0]), order=3, mode="reflect")
    out, sigma = refine_matches(make_set(PTS, PTS * 2.0), src, ref, H, {"patch": 48})

    # Select points that were actually corrected. Selecting on isfinite(sigma) would
    # silently select nothing here: sigma is NaN in this near-perfect-correlation
    # regime even though every point refined.
    ok = np.abs(out.ref_xy - PTS * 2.0).max(axis=1) > 1e-9
    assert ok.sum() >= 4
    # The correction lands in reference pixels: 2x the source-pixel displacement.
    err = out.ref_xy[ok] - (PTS[ok] * 2.0 + 2 * TRUE_D)
    assert np.abs(err).max() < 0.2, f"{err}"


def test_rejection_fires_on_noise():
    src = texture()
    rng = np.random.default_rng(99)
    ref = rng.random(src.shape).astype(np.float32)      # uncorrelated with src
    m = make_set(PTS, PTS.copy())
    out, sigma = refine_matches(m, src, ref, np.eye(3), {"patch": 48})

    assert np.isnan(sigma).all(), "an ambiguous peak must not be trusted"
    assert np.array_equal(out.ref_xy, PTS), "rejected points keep their coordinates"


def test_sigma_varies_with_noise():
    src = texture()
    ref = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    rng = np.random.default_rng(7)
    # Noise amplitude ramps left to right, so the right-hand points are less certain.
    ramp = np.linspace(0.0, 0.35, src.shape[1])[None, :]
    ref = ref + rng.normal(size=src.shape) * ramp

    pts = np.array([[48.0, 128.0], [110.0, 128.0], [170.0, 128.0], [215.0, 128.0]])
    out, sigma = refine_matches(make_set(pts, pts.copy()), src, ref,
                                np.eye(3), {"patch": 48})

    good = np.isfinite(sigma)
    assert good.sum() >= 2
    assert len(np.unique(sigma[good])) == good.sum(), "sigma must not be a constant"
    assert sigma[good][-1] > sigma[good][0], "noisier point must carry larger sigma"


def test_degenerate_inputs_do_not_crash():
    src = texture(n=64)
    ref = src.copy()

    empty = make_set(np.zeros((0, 2)), np.zeros((0, 2)))
    out, sigma = refine_matches(empty, src, ref, np.eye(3))
    assert len(out.src_xy) == 0 and sigma.shape == (0,)

    # points outside the image, a constant patch, and NaN coordinates
    flat = np.zeros((64, 64), dtype=np.float32)
    pts = np.array([[-50.0, -50.0], [1000.0, 1000.0], [32.0, 32.0], [np.nan, 5.0]])
    out, sigma = refine_matches(make_set(pts, pts.copy()), flat, flat, np.eye(3))
    assert np.isnan(sigma).all()

    # unusable geometry
    for bad in (None, np.zeros((3, 3)), np.full((3, 3), np.nan), np.eye(4)):
        out, sigma = refine_matches(make_set(PTS, PTS.copy()), src, ref, bad)
        assert np.isnan(sigma).all()
        assert np.array_equal(out.ref_xy, PTS)


def test_max_shift_gate():
    src = texture()
    ref = ndi.shift(src, (0.0, 9.0), order=3, mode="reflect")   # 9 px away
    m = make_set(PTS, PTS.copy())
    out, sigma = refine_matches(m, src, ref, np.eye(3), {"patch": 48, "max_shift_px": 3.0})
    assert np.isnan(sigma).all()
    assert np.array_equal(out.ref_xy, PTS)


def test_sigma_is_calibrated_against_the_real_error():
    # sigma must be a usable uncertainty, not just a varying number: on a noisy pair
    # its median has to sit in the same order of magnitude as the observed RMS error.
    rng = np.random.default_rng(5)
    src = texture(n=512, seed=5)
    ref = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    ref = ref + rng.normal(size=src.shape) * 0.2 * src.std()
    pts = rng.uniform(60, 452, size=(120, 2))
    out, sigma = refine_matches(make_set(pts, pts.copy()), src, ref,
                                np.eye(3), {"patch": 32})

    ok = np.isfinite(sigma)
    assert ok.sum() > 50
    err = np.hypot(*(out.ref_xy[ok] - pts[ok] - TRUE_D).T)
    rms = float(np.sqrt(np.mean(err ** 2)))
    ratio = float(np.median(sigma[ok])) / rms
    assert 0.2 < ratio < 5.0, f"sigma/error = {ratio:.2f} (sigma {np.median(sigma[ok]):.4f}, rms {rms:.4f})"


def test_ultra_clean_regime_reports_unknown_sigma_but_still_refines():
    """The NaN contract, asserted rather than discovered.

    Above rho = _RHO_MAX_VALIDATED the single-constant noise model does not apply,
    so sigma is NaN ("uncertainty unknown"). That must NOT be read as "the point
    was not refined" — ref_xy is still corrected, which is what P4 requires.
    """
    src = texture()                               # shifted copy: the unvalidated regime
    ref = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    out, sigma = refine_matches(make_set(PTS, PTS.copy()), src, ref, np.eye(3),
                                {"patch": 48})

    assert not np.isfinite(sigma).all(), (
        "a noiseless pair sits above the calibrated correlation range; sigma there "
        "must be NaN rather than an optimistic number")
    # The point of the contract: refinement still happened.
    after = np.hypot(*(out.ref_xy - PTS - TRUE_D).T)
    assert after.max() < 0.1, f"ref_xy was not corrected: residual {after.max():.4f} px"


def test_realistic_pair_reports_finite_sigma():
    """Guard against sigma silently becoming NaN everywhere.

    The NaN branch is correct for near-perfect synthetic correlation, but if it ever
    swallowed real pairs too, mean_sigma_px would read null on every run and P4 would
    have no uncertainty to report. This pins the regime that actually ships.
    """
    import warnings
    from samanvay.pipeline.stages import run_pipeline

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        m = run_pipeline("fixtures/dsun_sweep/dsun_30/source.tif",
                         "fixtures/dsun_sweep/dsun_30/reference.tif",
                         "runs/_test_sigma", config={"cache": {"enabled": False}})

    assert m["refined_count"] > 0, "nothing refined on a pair that should refine"
    assert m["mean_sigma_px"] is not None, (
        "sigma is NaN on a real pair — the calibrated regime no longer covers "
        "anything that ships")
    assert 0.0 < m["mean_sigma_px"] < 5.0, m["mean_sigma_px"]
