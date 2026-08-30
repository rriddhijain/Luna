"""Seat 6 · C9 — least-squares matching. The four things LSM claims over correlation:
sub-pixel accuracy, a local affine, radiometric invariance, and an honest refusal."""

import cv2
import numpy as np
import scipy.ndimage as ndi

from samanvay.geometry.lsm import lsm_refine
from samanvay.geometry.refine import refine_matches
from samanvay.types import MatchSet

PTS = np.array([[64.0, 64.0], [192.0, 64.0], [64.0, 192.0],
                [192.0, 192.0], [128.0, 128.0]])
TRUE_D = np.array([-0.62, 0.37])          # (dx, dy) the reference content is displaced by


def texture(n=256, sigma=1.6, seed=0):
    rng = np.random.default_rng(seed)
    img = ndi.gaussian_filter(rng.normal(size=(n, n)), sigma).astype(np.float32)
    img -= img.min()
    return img / img.max()


def make_set(src_xy, ref_xy):
    src_xy = np.asarray(src_xy, dtype=np.float64)
    n = len(src_xy)
    return MatchSet(src_xy=src_xy.copy(),
                    ref_xy=np.asarray(ref_xy, dtype=np.float64).copy(),
                    score=np.ones(n, dtype=np.float32),
                    method=np.zeros(n, dtype=np.uint8),
                    cell=np.zeros(n, dtype=np.int32))


def affine_about(centre, scale, deg):
    """3x3 similarity of `scale` and `deg` degrees about `centre`, source -> reference."""
    th = np.radians(deg)
    L = scale * np.array([[np.cos(th), -np.sin(th)], [np.sin(th), np.cos(th)]])
    H = np.eye(3)
    H[:2, :2] = L
    H[:2, 2] = np.asarray(centre, dtype=np.float64) - L @ np.asarray(centre, dtype=np.float64)
    return H


def test_recovers_known_subpixel_shift():
    src = texture()
    ref = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    out, sigma, info = lsm_refine(make_set(PTS, PTS), src, ref, np.eye(3), {"patch": 48})

    assert np.array_equal(out.src_xy, PTS), "src_xy must never move"
    assert info["n_refined"] == len(PTS), info["rejections"]
    err = np.hypot(*(out.ref_xy - PTS - TRUE_D).T)
    assert err.max() < 0.05, f"residual {err.max():.4f} px"
    # sigma is a genuine by-product of the adjustment, so it exists wherever it converged.
    assert np.isfinite(sigma).all()
    assert (sigma > 0).all()
    assert info["converged"].all()
    assert (info["iterations"] > 0).all()


def test_recovers_a_local_affine_that_translation_cannot():
    # The reference is a 1.05x / 3 deg warp of the source, but H says identity, so the
    # affine has to be found by the fit. Phase correlation has no parameter for it.
    src = texture()
    W = affine_about((128.0, 128.0), 1.05, 3.0)
    ref = cv2.warpAffine(src, W[:2], (256, 256), flags=cv2.INTER_CUBIC,
                         borderMode=cv2.BORDER_REFLECT_101)
    truth = (np.hstack([PTS, np.ones((len(PTS), 1))]) @ W.T)[:, :2]
    start = truth + np.array([0.6, -0.4])          # something for both methods to recover
    cfg = {"patch": 48, "max_affine_drift_px": 4.0}

    out, sigma, info = lsm_refine(make_set(PTS, start), src, ref, np.eye(3), cfg)
    pc, _ = refine_matches(make_set(PTS, start), src, ref, np.eye(3), cfg)

    lsm_err = np.hypot(*(out.ref_xy - truth).T)
    pc_err = np.hypot(*(pc.ref_xy - truth).T)
    assert info["n_refined"] == len(PTS), info["rejections"]
    assert lsm_err.max() < 0.10, f"lsm {lsm_err}"
    # The affine is the whole point: modelling it must beat assuming it away.
    assert lsm_err.max() < 0.5 * pc_err.max(), f"lsm {lsm_err.max()} vs pc {pc_err.max()}"


def test_invariant_to_gain_and_offset():
    # A cross-sensor pair differs radiometrically. The gain/offset parameters exist so
    # that difference costs nothing in geometry.
    src = texture()
    shifted = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    plain, _, _ = lsm_refine(make_set(PTS, PTS), src, shifted, np.eye(3), {"patch": 48})
    bright, sigma, info = lsm_refine(make_set(PTS, PTS), src, 3.7 * shifted - 0.42,
                                     np.eye(3), {"patch": 48})

    assert info["n_refined"] == len(PTS), info["rejections"]
    err = np.hypot(*(bright.ref_xy - PTS - TRUE_D).T)
    assert err.max() < 0.05, f"residual {err.max():.4f} px"
    assert np.abs(bright.ref_xy - plain.ref_xy).max() < 1e-6, "gain must not move the answer"


def test_rejects_a_noise_patch():
    src = texture()
    rng = np.random.default_rng(99)
    ref = rng.random(src.shape).astype(np.float32)      # uncorrelated with src
    out, sigma, info = lsm_refine(make_set(PTS, PTS), src, ref, np.eye(3), {"patch": 48})

    assert info["n_refined"] == 0, info["rejections"]
    assert info["n_rejected"] == len(PTS)
    assert np.isnan(sigma).all(), "a point that did not fit has no uncertainty to report"
    assert np.array_equal(out.ref_xy, PTS), "a rejected point keeps its coordinates"
    assert not info["converged"].any()
    assert "ok" not in info["rejections"]


def test_sigma_is_nan_exactly_where_it_did_not_converge():
    src = texture()
    ref = ndi.shift(src, (TRUE_D[1], TRUE_D[0]), order=3, mode="reflect")
    # Three good points and two that cannot be fitted: one off the edge of the reference,
    # one displaced far beyond the plausibility bound.
    pts = np.vstack([PTS[:3], [[128.0, 128.0], [128.0, 128.0]]])
    ref_xy = np.vstack([PTS[:3], [[-500.0, -500.0], [128.0 + 40.0, 128.0]]])
    out, sigma, info = lsm_refine(make_set(pts, ref_xy), src, ref, np.eye(3), {"patch": 48})

    assert np.isfinite(sigma[:3]).all()
    assert np.isnan(sigma[3:]).all()
    assert info["converged"][:3].all() and not info["converged"][3:].any()
    assert np.array_equal(out.ref_xy[3:], ref_xy[3:]), "rejected points keep coordinates"
    assert info["n_refined"] == 3 and info["n_rejected"] == 2


def test_degenerate_inputs_never_crash():
    src = texture()
    ref = texture(seed=1)
    empty = make_set(np.zeros((0, 2)), np.zeros((0, 2)))
    out, sigma, info = lsm_refine(empty, src, ref, np.eye(3))
    assert len(out.ref_xy) == 0 and len(sigma) == 0 and info["n_points"] == 0

    # constant patches: nothing to correlate, nothing to fit
    flat = np.zeros((256, 256), dtype=np.float32)
    out, sigma, info = lsm_refine(make_set(PTS, PTS), flat, flat, np.eye(3), {"patch": 32})
    assert info["n_refined"] == 0 and np.isnan(sigma).all()
    assert np.array_equal(out.ref_xy, PTS)

    # points on and past the border, plus a non-finite one
    edge = np.array([[1.0, 1.0], [255.0, 255.0], [1e6, 1e6], [np.nan, 10.0]])
    out, sigma, info = lsm_refine(make_set(edge, edge), src, ref, np.eye(3), {"patch": 32})
    assert np.isnan(sigma).all() and np.array_equal(out.ref_xy[:3], edge[:3])
    assert info["reasons"][3] == "bad_point"

    # no geometry at all, and a broken one
    for H in (None, np.full((3, 3), np.nan), np.eye(2), np.zeros((3, 3))):
        out, sigma, info = lsm_refine(make_set(PTS, PTS), src, ref, H, {"patch": 32})
        assert np.isnan(sigma).all() and np.array_equal(out.ref_xy, PTS)
        assert info["n_refined"] == 0

    # a patch too small to carry 8 parameters is declined, not attempted
    out, sigma, info = lsm_refine(make_set(PTS, PTS), src, ref, np.eye(3), {"patch": 4})
    assert info["n_refined"] == 0 and info["rejections"] == {"patch_too_small": len(PTS)}


def test_works_through_a_2x_scale_jacobian():
    # The pipeline's real case: H carries the scale, so A must start at the Jacobian.
    src = texture()
    H = np.array([[2.0, 0.0, 0.0], [0.0, 2.0, 0.0], [0.0, 0.0, 1.0]])
    big = cv2.warpAffine(src, H[:2], (512, 512), flags=cv2.INTER_CUBIC,
                         borderMode=cv2.BORDER_REFLECT_101)
    ref = ndi.shift(big, (2 * TRUE_D[1], 2 * TRUE_D[0]), order=3, mode="reflect")
    out, sigma, info = lsm_refine(make_set(PTS, PTS * 2.0), src, ref, H, {"patch": 48})

    assert info["n_refined"] == len(PTS), info["rejections"]
    # The correction lands in reference pixels: 2x the source-pixel displacement.
    err = np.hypot(*(out.ref_xy - (PTS * 2.0 + 2 * TRUE_D)).T)
    assert err.max() < 0.10, f"{err}"
