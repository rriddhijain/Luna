"""Seat 1 · tests for the RIFT (L2/L3) descriptor path in match/describe.py."""

import cv2
import numpy as np
import pytest

from samanvay.match.describe import (
    describe_keypoints,
    mim_from_orientation,
    rift_descriptor,
)
from samanvay.match.detect import detect_keypoints
from samanvay.photometry.phasecong import phase_congruency

NORIENT = 6
DIM = 6 * 6 * NORIENT          # module default ngrid=6


def _terrain(n=256, seed=0):
    """A textured, crater-ish scene: enough structure for phase congruency to bite on."""
    rng = np.random.default_rng(seed)
    img = rng.normal(0.5, 0.08, (n, n)).astype(np.float32)
    img = cv2.GaussianBlur(img, (0, 0), 2.0)
    yy, xx = np.mgrid[0:n, 0:n]
    for cx, cy, r in [(60, 70, 22), (170, 90, 30), (100, 180, 26), (200, 200, 16)]:
        d = np.hypot(xx - cx, yy - cy)
        img += 0.25 * np.exp(-((d - r) ** 2) / (2 * 3.0 ** 2))     # a rim, not a blob
    return np.clip(img, 0, 1).astype(np.float32)


def _kps(img, pc):
    return detect_keypoints((img * 255).astype(np.uint8), method="l2", pc_map=pc)


def _descs(img, orient, pc, kps=None, **kw):
    u8 = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    if kps is None:
        kps = detect_keypoints(u8, method="l2", pc_map=pc)
    return describe_keypoints(u8, kps, method="l2", pc_orient=orient, pc=pc, **kw)


# ---------------------------------------------------------------- MIM derivation

def test_mim_index_matches_phasecong_mim():
    """CanonicalImage carries pc_orient but not mim; the index must be recoverable from it."""
    out = phase_congruency(_terrain(), norient=NORIENT)
    derived = mim_from_orientation(out["orientation"], NORIENT)
    assert derived.shape == out["mim"].shape
    agree = float((derived == out["mim"]).mean())
    assert agree == 1.0, f"MIM derivation disagrees with phasecong on {1 - agree:.4%} of pixels"


def test_mim_index_is_a_label_in_range():
    idx = mim_from_orientation(np.array([[0.0, -np.pi, 7.3, np.nan, np.inf]]), NORIENT)
    assert idx.min() >= 0 and idx.max() < NORIENT


# ---------------------------------------------------------------- invariance

def _height_field(n=192, seed=3):
    """A small crater field, as heights — so we can actually re-illuminate it."""
    rng = np.random.default_rng(seed)
    z = cv2.GaussianBlur(rng.normal(0, 1, (n, n)).astype(np.float32), (0, 0), 6.0) * 40
    yy, xx = np.mgrid[0:n, 0:n]
    for cx, cy, r in [(50, 60, 20), (130, 70, 26), (80, 140, 22), (150, 150, 14), (40, 150, 12)]:
        d = np.hypot(xx - cx, yy - cy)
        z -= 12 * np.exp(-(d / r) ** 2) * (1 - (d / r) ** 2)      # bowl with a raised rim
    return z


def _shade(z, az_deg, el_deg=25.0):
    """Lambertian render of `z` under one sun. Changing az is the actual problem statement."""
    gy, gx = np.gradient(z.astype(np.float64))
    nrm = np.dstack([-gx, -gy, np.ones_like(z)])
    nrm /= np.linalg.norm(nrm, axis=2, keepdims=True)
    a, e = np.radians(az_deg), np.radians(el_deg)
    sun = np.array([np.cos(e) * np.sin(a), np.cos(e) * np.cos(a), np.sin(e)])
    return np.clip(0.03 + 0.9 * np.clip(nrm @ sun, 0, None), 0, 1).astype(np.float32)


def _old_orientation_histogram(orient, x, y, patch=16, grid=4, bins=8):
    """The descriptor RIFT replaces, reproduced exactly: 16 px patch, 4x4 grid, 8 angle
    bins over [0, 2pi), unweighted, L2-normalised."""
    half = patch // 2
    p = np.mod(orient[y - half:y + half, x - half:x + half] + 2 * np.pi, 2 * np.pi)
    step = patch // grid
    desc = []
    for sy in range(grid):
        for sx in range(grid):
            block = p[sy * step:(sy + 1) * step, sx * step:(sx + 1) * step]
            desc.extend(np.histogram(block, bins=bins, range=(0, 2 * np.pi))[0])
    desc = np.asarray(desc, dtype=np.float64)
    n = np.linalg.norm(desc)
    return desc / n if n else desc


def _nn_correct(da, db):
    """Fraction of points whose nearest neighbour in the other image is itself.

    Drift alone is a bad metric — a constant descriptor has zero drift and zero value.
    This is the property matching actually needs.
    """
    nn = np.argmin(((da[:, None, :] - db[None, :, :]) ** 2).sum(-1), axis=1)
    return float((nn == np.arange(len(da))).mean())


def _both_descriptors(img_a, img_b, pts):
    a = phase_congruency(img_a, norient=NORIENT)
    b = phase_congruency(img_b, norient=NORIENT)
    kps = [cv2.KeyPoint(x=float(x), y=float(y), size=96.0) for x, y in pts]
    _, da = _descs(img_a, a["orientation"], a["pc"], kps, patch_sizes=(96,))
    _, db = _descs(img_b, b["orientation"], b["pc"], kps, patch_sizes=(96,))
    assert len(da) == len(db) == len(pts), "the same points must survive on both sides"
    oa = np.array([_old_orientation_histogram(a["orientation"], x, y) for x, y in pts])
    ob = np.array([_old_orientation_histogram(b["orientation"], x, y) for x, y in pts])
    return (da, db), (oa, ob)


_PTS = [(x, y) for y in range(56, 140, 12) for x in range(56, 140, 12)]


# Measured on this fixture; the assertions below sit under the measured values with room.
#   remap        RIFT nn-correct   old-descriptor nn-correct
#   a*I + b          1.000              0.653
#   gamma 2.2        1.000              0.551
#   sun az +50 deg   0.918              0.122
@pytest.mark.parametrize("name,rift_floor,old_ceiling", [
    ("affine", 0.95, 0.80),
    ("gamma", 0.95, 0.75),
    ("sun_az", 0.80, 0.35),
])
def test_rift_beats_orientation_histogram_under_illumination_change(name, rift_floor, old_ceiling):
    z = _height_field()
    img_a = _shade(z, 45.0)
    img_b = {
        "affine": lambda: np.clip(1.6 * img_a + 0.12, 0, 1).astype(np.float32),
        "gamma": lambda: np.clip(img_a, 0, 1).astype(np.float32) ** 2.2,
        "sun_az": lambda: _shade(z, 95.0),
    }[name]()

    (da, db), (oa, ob) = _both_descriptors(img_a, img_b, _PTS)
    rift, old = _nn_correct(da, db), _nn_correct(oa, ob)
    assert rift >= rift_floor, f"{name}: RIFT nn-correct {rift:.3f} < {rift_floor}"
    assert old <= old_ceiling, f"{name}: old descriptor unexpectedly good ({old:.3f})"
    assert rift > old, f"{name}: RIFT {rift:.3f} did not beat the old descriptor {old:.3f}"


def test_rift_drift_under_monotonic_remap_is_small():
    """Absolute invariance number, not just a relative win: unit vectors, so <0.6 is close."""
    z = _height_field()
    img_a = _shade(z, 45.0)
    for name, img_b in [("affine", np.clip(1.6 * img_a + 0.12, 0, 1).astype(np.float32)),
                        ("gamma", np.clip(img_a, 0, 1).astype(np.float32) ** 2.2)]:
        (da, db), _ = _both_descriptors(img_a, img_b, _PTS)
        drift = float(np.mean(np.linalg.norm(da - db, axis=1)))
        assert drift < 0.6, f"{name}: mean RIFT drift {drift:.4f}"


# ---------------------------------------------------------------- shape contract

def test_descriptors_are_unit_norm_and_correctly_shaped():
    img = _terrain()
    out = phase_congruency(img, norient=NORIENT)
    kps, desc = _descs(img, out["orientation"], out["pc"])
    assert len(kps) == len(desc) > 0
    assert desc.shape[1] == DIM and desc.dtype == np.float32
    assert np.allclose(np.linalg.norm(desc, axis=1), 1.0, atol=1e-5)
    assert np.isfinite(desc).all()


def test_rift_alias_matches_l2():
    img = _terrain()
    out = phase_congruency(img, norient=NORIENT)
    k1, d1 = _descs(img, out["orientation"], out["pc"])
    u8 = (img * 255).astype(np.uint8)
    kps = detect_keypoints(u8, method="rift", pc_map=out["pc"])
    k2, d2 = describe_keypoints(u8, kps, method="rift",
                                pc_orient=out["orientation"], pc=out["pc"])
    assert len(d1) == len(d2) and np.allclose(d1, d2)


def test_multiscale_emits_one_descriptor_per_fitting_patch():
    """The 2x source/reference scale ratio is why more than one patch size exists."""
    img = _terrain()
    out = phase_congruency(img, norient=NORIENT)
    kp = [cv2.KeyPoint(x=128.0, y=128.0, size=1.0)]
    k1, d1 = _descs(img, out["orientation"], out["pc"], kp, patch_sizes=(48,))
    k3, d3 = _descs(img, out["orientation"], out["pc"], kp, patch_sizes=(24, 48, 96))
    assert len(d1) == 1 and len(d3) == 3
    assert {k.size for k in k3} == {24.0, 48.0, 96.0}
    assert not np.allclose(d3[0], d3[2])          # different scales say different things


# ---------------------------------------------------------------- degenerate input

def test_textureless_patch_is_dropped_not_described():
    """A flat patch has no structure; a confident unit descriptor there is a lie."""
    flat = np.full((128, 128), 0.5, np.float32)
    zeros = np.zeros_like(flat)
    assert rift_descriptor(np.zeros((128, 128), np.uint8), zeros, 64, 64, 96) is None
    kps, desc = _descs(flat, zeros, zeros)
    assert len(kps) == 0 and desc.shape == (0, DIM)


def test_border_keypoint_is_skipped_not_crashed():
    img = _terrain(96)
    out = phase_congruency(img, norient=NORIENT)
    kps = [cv2.KeyPoint(x=1.0, y=1.0, size=1.0),        # every patch overruns
           cv2.KeyPoint(x=48.0, y=48.0, size=1.0)]      # 96 fits exactly, centred
    k, d = _descs(img, out["orientation"], out["pc"], kps, patch_sizes=(24, 48, 96))
    assert len(k) == len(d)
    assert all(p.pt == (48.0, 48.0) for p in k)
    assert rift_descriptor(out["mim"], out["pc"], 0, 0, 96) is None


def test_no_keypoints_and_empty_image_return_empty_not_crash():
    img = _terrain(64)
    out = phase_congruency(img, norient=NORIENT)
    for kps, arr in [([], img), ([cv2.KeyPoint(x=1.0, y=1.0, size=1.0)], np.zeros((0, 0), np.float32))]:
        k, d = describe_keypoints((np.clip(arr, 0, 1) * 255).astype(np.uint8), kps,
                                  method="l2", pc_orient=out["orientation"], pc=out["pc"])
        assert len(k) == 0 and d.shape == (0, DIM)


def test_works_without_pc_maps_supplied():
    """match/tile.py forwards pc_orient only; describe must still produce a weight map."""
    img = _terrain()
    u8 = (img * 255).astype(np.uint8)
    kps = detect_keypoints(u8, method="l2", pc_map=None)
    k, d = describe_keypoints(u8, kps, method="l2")
    assert len(k) == len(d) > 0 and d.shape[1] == DIM
    assert np.allclose(np.linalg.norm(d, axis=1), 1.0, atol=1e-5)


def test_unknown_method_raises():
    with pytest.raises(ValueError):
        describe_keypoints(np.zeros((8, 8), np.uint8), [], method="nope")
