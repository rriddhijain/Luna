"""Tests for samanvay/trn.py — the terrain-relative navigation demo.

Everything here runs on small synthetic rasters written into tmp_path so the suite
stays in seconds. The measured numbers on the real fixture live in the seat report,
not in an assertion: these tests check behaviour and trends, not magic constants.
"""

import json
import math
import os

import cv2
import numpy as np
import pytest
import rasterio
from rasterio.transform import Affine

from samanvay import trn
from samanvay.io.loaders import load_product
from samanvay.photometry.shading import predicted_illumination

SUN_AZ, SUN_EL = 135.0, 60.0


def _terrain(size, seed=0, scale=8):
    """Smooth random relief: enough structure for SIFT, small enough to be fast."""
    rng = np.random.default_rng(seed)
    coarse = rng.random((max(2, size // scale), max(2, size // scale)))
    return cv2.resize(coarse.astype(np.float32), (size, size),
                      interpolation=cv2.INTER_CUBIC)


def _write(path, array, meta):
    """Write a single-band GeoTIFF plus the sidecar JSON the metadata adapter reads."""
    array = np.asarray(array)
    with rasterio.open(path, "w", driver="GTiff", height=array.shape[0],
                       width=array.shape[1], count=1, dtype=array.dtype,
                       transform=Affine(1.0, 0.0, 0.0, 0.0, -1.0, array.shape[0])) as dst:
        dst.write(array, 1)
    with open(str(path) + ".json", "w") as handle:
        json.dump(meta, handle)
    return str(path)


def _scene(tmp_path, size=256, seed=0, with_dem=True):
    """A basemap (and its DEM) that are physically consistent: image = albedo x shading."""
    dem = (_terrain(size, seed=seed, scale=8) * 40.0).astype(np.float64)
    albedo = 0.5 + 0.5 * _terrain(size, seed=seed + 100, scale=4)
    illum = predicted_illumination(dem, 1.0, {"sun_az_deg": SUN_AZ, "sun_el_deg": SUN_EL,
                                              "emission_deg": 0.0})
    img = np.clip(albedo * illum, 0.0, 1.0) * 60000.0
    meta = {"product_id": "test_ref", "gsd_m": 1.0, "sun_az_deg": SUN_AZ,
            "sun_el_deg": SUN_EL, "emission_deg": 0.0}
    ref_path = _write(tmp_path / "reference.tif", img.astype(np.uint16), meta)
    dem_path = None
    if with_dem:
        dem_path = _write(tmp_path / "dem.tif", dem.astype(np.float32),
                          {"product_id": "test_dem", "gsd_m": 1.0})
    return ref_path, dem_path


def test_easy_case_localises_to_sub_pixel(tmp_path):
    """Same sun, small scale change: the fix must land within a stated 2 px of truth."""
    ref_path, dem_path = _scene(tmp_path)
    sim = trn.simulate_descent_frame(
        ref_path, dem_path=dem_path, center_xy=(130.0, 120.0), altitude_scale=1.05,
        sun=(SUN_AZ, SUN_EL), seed=3, frame_size=128, rotation_deg=2.0,
        noise_sigma=0.01)
    assert sim["reillumination"] == "dem_physical"
    assert sim["true_center_xy"] == (130.0, 120.0)
    assert sim["frame_gsd_m"] == pytest.approx(1.0 / 1.05)

    fix = trn.localise(sim, ref_path)
    assert fix["status"] == "ok", fix["reason"]
    # 2 px is the tolerance this test asserts, not the accuracy the engine delivers —
    # the fixture demo measures ~0.12 m at 1 m GSD.
    assert fix["error_px"] < 2.0
    assert fix["error_m"] == pytest.approx(fix["error_px"])       # reference GSD is 1 m
    assert fix["ellipse"] is not None
    assert fix["ellipse"]["semi_major_px"] >= fix["ellipse"]["semi_minor_px"] > 0
    assert fix["ellipse"]["chi2_factor"] == pytest.approx(5.9915, abs=1e-3)

    # The fix in metres: 1 m GSD, and the test raster's geotransform is
    # Affine(1, 0, 0, 0, -1, H), so world x = px + 0.5 and world y = H - (py + 0.5).
    ex, ey = fix["estimated_center_xy"]
    assert fix["estimated_center_m"] == pytest.approx((ex, ey))
    wx, wy = fix["estimated_center_world_xy"]
    assert (wx, wy) == pytest.approx((ex + 0.5, 256.0 - (ey + 0.5)))


def test_no_dem_says_so_and_does_not_claim_physics(tmp_path):
    """Without a DEM the frame is still produced, but never labelled a physical render."""
    ref_path, _ = _scene(tmp_path, with_dem=False)
    sim = trn.simulate_descent_frame(ref_path, dem_path=None, center_xy=(128.0, 128.0),
                                     altitude_scale=1.0, sun=(20.0, 30.0), seed=1,
                                     frame_size=96)
    assert sim["reillumination"] == "intensity_only"
    assert "NOT a physical re-illumination" in sim["reillumination_info"]["note"]


def test_unknown_reference_gsd_gives_no_metres(tmp_path):
    """A basemap without a GSD yields a fix in pixels and None metres — never a fake 1.0."""
    size = 192
    img = (_terrain(size, seed=5, scale=6) * 60000).astype(np.uint16)
    ref_path = _write(tmp_path / "nogsd.tif", img, {"product_id": "nogsd"})
    sim = trn.simulate_descent_frame(ref_path, center_xy=(96.0, 96.0), altitude_scale=1.0,
                                     sun=(SUN_AZ, SUN_EL), seed=0, frame_size=96)
    assert sim["frame_gsd_m"] is None
    fix = trn.localise(sim, ref_path)
    assert fix["reference_gsd_m"] is None
    assert fix["gsd_source"] == "unknown"
    assert fix["error_m"] is None
    assert fix["estimated_center_m"] is None          # no GSD, no metres
    assert fix["estimated_center_xy"] is not None     # the pixel fix still stands
    if fix["ellipse"]:
        assert fix["ellipse"]["semi_major_m"] is None


def test_position_covariance_declines_without_redundancy():
    """2 points against an 8-parameter model is not a covariance, it is wishful thinking."""
    src = np.array([[0.0, 0.0], [10.0, 4.0]])
    cov, why = trn.position_covariance("homography", np.eye(3), src, src, (5.0, 2.0))
    assert cov is None
    assert "dof" in why
    # An unknown model must not silently produce numbers either.
    cov, why = trn.position_covariance("bilinear", np.eye(3), src, src, (5.0, 2.0))
    assert cov is None and "parametrisation" in why


def test_ellipse_is_none_when_inliers_are_too_few(tmp_path, monkeypatch):
    """The inlier floor is a real gate: raise it and the ellipse disappears with a reason."""
    ref_path, dem_path = _scene(tmp_path)
    sim = trn.simulate_descent_frame(ref_path, dem_path=dem_path, center_xy=(130.0, 130.0),
                                     altitude_scale=1.0, sun=(SUN_AZ, SUN_EL), seed=0,
                                     frame_size=128)
    monkeypatch.setattr(trn, "_MIN_ELLIPSE_INLIERS", 10 ** 6)
    fix = trn.localise(sim, ref_path)
    assert fix["status"] == "ok"
    assert fix["estimated_center_xy"] is not None      # the fix survives; the ellipse does not
    assert fix["ellipse"] is None
    assert "below the stated minimum" in fix["ellipse_reason"]
    assert fix["trustworthy"] is False


def test_error_grows_with_sun_difference(tmp_path):
    """The trend the whole project rests on: a bigger sun change is a harder localisation."""
    ref_path, dem_path = _scene(tmp_path, size=320, seed=7)
    errors = {}
    for d_az in (0.0, 40.0, 80.0):
        sim = trn.simulate_descent_frame(
            ref_path, dem_path=dem_path, center_xy=(160.0, 160.0), altitude_scale=1.0,
            sun=(SUN_AZ + d_az, SUN_EL), seed=0, frame_size=160, rotation_deg=0.0,
            noise_sigma=0.01)
        fix = trn.localise(sim, ref_path)
        # A failed localisation IS the extreme of the trend; score it as unbounded error.
        errors[d_az] = fix["error_px"] if fix["error_px"] is not None else float("inf")
    assert errors[80.0] > errors[0.0], errors
    assert (errors[40.0] + errors[80.0]) / 2.0 > errors[0.0], errors


def test_flat_frame_fails_honestly(tmp_path):
    """Zero matches must return a failed fix, not a crash and not a confident zero."""
    ref_path, _ = _scene(tmp_path, with_dem=False)
    reference = load_product(ref_path)
    flat = {"image": np.full((96, 96), 0.5, dtype=np.float32),
            "meta": {"product_id": "flat", "gsd_m": 1.0, "shape": (96, 96)},
            "true_center_xy": (48.0, 48.0)}
    fix = trn.localise(flat, reference)
    assert fix["status"] == "failed"
    assert fix["estimated_center_xy"] is None
    assert fix["error_m"] is None
    assert fix["ellipse"] is None
    assert "registration failed" in fix["ellipse_reason"]
    assert fix["trustworthy"] is False


def test_demo_writes_json_and_png(tmp_path):
    """The deliverable: trn.json parses and the PNG exists."""
    ref_path, dem_path = _scene(tmp_path)
    out = tmp_path / "run"
    summary = trn.run_trn_demo(ref_path, str(out), n_frames=2, dem_path=dem_path,
                               sun=(SUN_AZ + 30.0, SUN_EL - 10.0), frame_size=128,
                               track_px=20.0)
    assert os.path.exists(summary["json_path"])
    assert os.path.getsize(summary["png_path"]) > 1000
    with open(summary["json_path"]) as handle:
        loaded = json.load(handle)
    assert loaded["n_frames"] == 2
    assert len(loaded["frames"]) == 2
    assert loaded["reillumination"] == "dem_physical"
    # Scale rises as the lander descends.
    assert loaded["frames"][1]["altitude_scale"] > loaded["frames"][0]["altitude_scale"]
    # The keys pipeline/run.py's `trn` command prints must all be present.
    for key in ("n_localised", "n_with_ellipse", "max_error_m", "ellipse_checked",
                "ellipse_coverage_frac", "confidence"):
        assert key in summary
    if summary["n_localised"]:
        assert summary["mean_error_m"] is not None
        assert summary["p90_error_m"] >= 0.0


def test_demo_survives_total_failure(tmp_path):
    """A textureless basemap localises nothing — and still writes both artifacts."""
    flat = np.full((160, 160), 1000, dtype=np.uint16)
    ref_path = _write(tmp_path / "flat.tif", flat,
                      {"product_id": "flat", "gsd_m": 1.0, "sun_az_deg": SUN_AZ,
                       "sun_el_deg": SUN_EL})
    out = tmp_path / "run"
    summary = trn.run_trn_demo(ref_path, str(out), n_frames=2, frame_size=96,
                               track_px=10.0)
    assert summary["n_localised"] == 0
    assert summary["mean_error_m"] is None      # no fix means no number, not 0.0
    assert summary["p90_error_m"] is None
    assert summary["ellipse_coverage_frac"] is None
    assert os.path.exists(summary["json_path"])
    assert os.path.exists(summary["png_path"])
    with open(summary["json_path"]) as handle:
        assert json.load(handle)["frames"][0]["status"] == "failed"


def test_error_ellipse_orientation_follows_the_covariance():
    """A covariance elongated along y must report a major axis near 90 degrees."""
    cov = np.diag([1.0, 9.0])
    ell = trn.error_ellipse(cov, confidence=0.95, gsd_m=2.0)
    assert ell["semi_major_px"] == pytest.approx(math.sqrt(5.99146 * 9.0), rel=1e-4)
    assert ell["semi_major_m"] == pytest.approx(2.0 * ell["semi_major_px"])
    assert abs(abs(ell["orientation_deg"]) - 90.0) < 1e-6
    assert ell["frame"] == "reference_px"
