"""Seat 2 · S2 — tests for the synthetic illumination-pair fixture generator.

Everything here runs on tiny grids (<= 256 px) so the whole file stays a couple of seconds.
"""

import json
import os

import numpy as np
import pytest
import rasterio

from synth.render_pair import project_points, render_illumination, render_synthetic_pair
from synth.sweep import DEFAULT_DELTAS, render_sweep
from synth.terrain import make_dem

TINY = dict(ref_shape=(128, 128), seed=3)


def _bowl_dem(n=192, radius_px=50.0, depth_m=12.0):
    """One clean crater on a flat plain — an unambiguous target for shadow-direction assertions."""
    yy, xx = np.mgrid[0:n, 0:n]
    r = np.hypot(xx - (n - 1) / 2.0, yy - (n - 1) / 2.0) / radius_px
    z = np.where(r <= 1.0, -depth_m * (1.0 - r * r) + 0.3 * depth_m * r ** 4,
                 0.3 * depth_m * np.maximum(r, 1.0) ** -3.0)
    return z.astype(np.float32)


def _read(path):
    with rasterio.open(path) as src:
        return src.read(1)


# --------------------------------------------------------------------------- determinism

def test_terrain_is_deterministic_under_a_seed():
    a, gsd = make_dem(shape=(256, 256), gsd_m=0.5, seed=11)
    b, _ = make_dem(shape=(256, 256), gsd_m=0.5, seed=11)
    c, _ = make_dem(shape=(256, 256), gsd_m=0.5, seed=12)
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)
    assert gsd == 0.5 and a.dtype == np.float32
    assert np.ptp(a) > 0.5  # the surface actually has relief


def test_fixture_generation_is_deterministic(tmp_path):
    one = render_synthetic_pair(out_dir=str(tmp_path / "one"), **TINY)
    two = render_synthetic_pair(out_dir=str(tmp_path / "two"), **TINY)
    for key in ("source", "reference", "dem"):
        assert np.array_equal(_read(one[key]), _read(two[key])), key
    assert np.array_equal(one["H"], two["H"])
    assert json.load(open(one["gt"])) == json.load(open(two["gt"]))


def test_a_different_seed_gives_a_different_scene(tmp_path):
    one = render_synthetic_pair(out_dir=str(tmp_path / "s3"), ref_shape=(128, 128), seed=3)
    two = render_synthetic_pair(out_dir=str(tmp_path / "s4"), ref_shape=(128, 128), seed=4)
    assert not np.array_equal(_read(one["reference"]), _read(two["reference"]))


# --------------------------------------------------------------------------- ground truth

def test_gt_homography_maps_gt_source_points_onto_gt_reference_points(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "gt"), **TINY)
    gt = json.load(open(info["gt"]))
    h = np.array(gt["H_src_to_ref"], dtype=np.float64)
    assert np.allclose(h, info["H"])
    for key in ("gt_points", "gt_points_holdout"):
        src = np.array(gt[key]["src_xy"], dtype=np.float64)
        ref = np.array(gt[key]["ref_xy"], dtype=np.float64)
        assert len(src) == gt[key]["n"] > 0
        assert np.max(np.abs(project_points(h, src) - ref)) < 1e-9, key


def test_holdout_points_share_nothing_with_the_tuning_grid(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "ho"), **TINY)
    gt = json.load(open(info["gt"]))
    tune = np.array(gt["gt_points"]["src_xy"])
    hold = np.array(gt["gt_points_holdout"]["src_xy"])
    d = np.linalg.norm(tune[:, None, :] - hold[None, :, :], axis=-1)
    assert d.min() > 1e-6  # no shared point, so a tuned matcher cannot have seen the held-out set


def test_gt_points_fall_inside_both_images(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "in"), **TINY)
    gt = json.load(open(info["gt"]))
    sh, sw = info["src_shape"]
    rh, rw = info["ref_shape"]
    for key in ("gt_points", "gt_points_holdout"):
        src = np.array(gt[key]["src_xy"])
        ref = np.array(gt[key]["ref_xy"])
        assert src[:, 0].min() >= 0 and src[:, 0].max() <= sw - 1
        assert src[:, 1].min() >= 0 and src[:, 1].max() <= sh - 1
        assert ref[:, 0].min() >= 0 and ref[:, 0].max() <= rw - 1
        assert ref[:, 1].min() >= 0 and ref[:, 1].max() <= rh - 1


def test_scale_between_the_pair_is_a_real_factor_of_two(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "sc"), **TINY)
    # singular values of the linear part: a 1:1 fixture would let scale-fragile code look healthy
    sv = np.linalg.svd(info["H"][:2, :2], compute_uv=False)
    assert 0.45 < sv.min() and sv.max() < 0.56
    assert info["scale_src_to_ref"] == pytest.approx(0.5)


def test_the_source_geotransform_is_an_imperfect_prior_and_says_so(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "geo"), **TINY)
    meta = json.load(open(info["source_sidecar"]))
    assert meta["geotransform_exact"] is False
    assert meta["geotransform_max_error_m"] > 1.0
    assert json.load(open(info["reference_sidecar"]))["geotransform_exact"] is True
    assert not np.allclose(info["H_prior"], info["H"])


# --------------------------------------------------------------------------- illumination

def test_the_two_renders_genuinely_differ():
    dem = _bowl_dem()
    a = render_illumination(dem, 1.0, 45.0, 25.0)["radiance"]
    b = render_illumination(dem, 1.0, 135.0, 65.0)["radiance"]
    assert np.corrcoef(a.ravel(), b.ravel())[0, 1] < 0.5


def test_shadows_fall_on_opposite_sides_for_opposite_suns():
    dem = _bowl_dem(n=192, radius_px=50.0)
    centre = (192 - 1) / 2.0
    east = render_illumination(dem, 1.0, 90.0, 20.0)["shadow"]   # sun from the east
    west = render_illumination(dem, 1.0, 270.0, 20.0)["shadow"]  # sun from the west
    assert east.sum() > 100 and west.sum() > 100
    cx_east = np.nonzero(east)[1].mean()
    cx_west = np.nonzero(west)[1].mean()
    # opposite sides of the crater centre, and well separated
    assert (cx_east - centre) * (cx_west - centre) < 0
    assert abs(cx_east - cx_west) > 0.4 * 50.0
    # a bowl's dark wall is the one facing away from the sun, i.e. on the sun's own side
    assert cx_east > centre > cx_west
    assert np.mean(east & west) < 0.02 * np.mean(east | west) + 1e-6


def test_a_sun_below_the_horizon_shadows_everything_instead_of_crashing():
    dem = _bowl_dem(n=64, radius_px=20.0)
    out = render_illumination(dem, 1.0, 45.0, -5.0)
    assert out["shadow"].all()
    assert np.all(np.isfinite(out["radiance"]))


# --------------------------------------------------------------------------- sidecars

def test_sidecars_carry_the_sun_angles(tmp_path):
    src_sun, ref_sun = (40.0, 22.0), (160.0, 58.0)
    info = render_synthetic_pair(out_dir=str(tmp_path / "side"), ref_shape=(128, 128), seed=5,
                                 src_sun=src_sun, ref_sun=ref_sun)
    src = json.load(open(info["source_sidecar"]))
    ref = json.load(open(info["reference_sidecar"]))
    assert src["sun_az_world_deg"] == pytest.approx(src_sun[0])
    assert src["sun_el_deg"] == pytest.approx(src_sun[1])
    assert src["incidence_deg"] == pytest.approx(90.0 - src_sun[1])
    assert ref["sun_az_deg"] == pytest.approx(ref_sun[0])
    assert ref["sun_el_deg"] == pytest.approx(ref_sun[1])
    assert ref["incidence_deg"] == pytest.approx(90.0 - ref_sun[1])
    # the source is rotated, so its image-frame azimuth is the world azimuth less the rotation
    assert src["sun_az_frame"] == "image"
    assert src["sun_az_deg"] == pytest.approx(src_sun[0] - 10.0, abs=1e-6)
    for meta, key in ((src, "source"), (ref, "reference")):
        assert meta["gsd_m"] > 0 and meta["emission_deg"] == 0.0
        assert meta["shape"] == list(info["src_shape"] if key == "source" else info["ref_shape"])


def test_the_written_rasters_match_the_declared_shapes_and_dtype(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "ras"), **TINY)
    src, ref, dem = _read(info["source"]), _read(info["reference"]), _read(info["dem"])
    assert src.shape == info["src_shape"] and src.dtype == np.uint16
    assert ref.shape == info["ref_shape"] and ref.dtype == np.uint16
    assert dem.shape == info["ref_shape"] and dem.dtype == np.float32
    assert src.std() > 0 and ref.std() > 0  # not a flat frame
    assert os.path.getsize(info["readme"]) > 2000


# --------------------------------------------------------------------------- sweep

def test_sweep_produces_one_pair_per_delta(tmp_path):
    out = str(tmp_path / "sweep")
    entries = render_sweep(out_dir=out, ref_shape=(96, 96), seed=2)
    assert len(entries) == len(DEFAULT_DELTAS) == 6
    manifest = json.load(open(os.path.join(out, "manifest.json")))
    assert manifest["n_pairs"] == 6
    for entry, delta in zip(entries, DEFAULT_DELTAS):
        assert entry["delta_sun_az_deg"] == float(delta)
        gt = json.load(open(entry["gt"]))
        assert gt["delta_sun_az_deg"] == pytest.approx(float(delta))
        assert gt["delta_sun_el_deg"] == pytest.approx(0.0)  # only azimuth varies
        for name in ("source.tif", "reference.tif", "dem.tif", "gt.json", "README.md",
                     "source.tif.json", "reference.tif.json"):
            assert os.path.exists(os.path.join(entry["dir"], name)), name


def test_sweep_holds_geometry_fixed_and_varies_only_illumination(tmp_path):
    entries = render_sweep(out_dir=str(tmp_path / "sw2"), deltas=(0, 30), ref_shape=(96, 96), seed=2)
    h0, h1 = (np.array(e["H_src_to_ref"]) for e in entries)
    assert np.allclose(h0, h1)  # identical geometry across the series
    a, b = (_read(e["reference"]) for e in entries)
    assert np.array_equal(a, b)  # the reference sun never moves, so the reference is byte-identical
    c, d = (_read(e["source"]) for e in entries)
    assert not np.array_equal(c, d)  # only the source illumination changes


# --------------------------------------------------------------------------- degenerate input

def test_a_tiny_grid_still_produces_a_usable_fixture(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "tiny"), ref_shape=(32, 32), seed=1)
    gt = json.load(open(info["gt"]))
    h = np.array(gt["H_src_to_ref"])
    src = np.array(gt["gt_points"]["src_xy"])
    assert np.max(np.abs(project_points(h, src) - np.array(gt["gt_points"]["ref_xy"]))) < 1e-9
    assert _read(info["source"]).size > 0


def test_a_featureless_dem_renders_without_nan(tmp_path):
    info = render_synthetic_pair(out_dir=str(tmp_path / "flat"), ref_shape=(64, 64), seed=1,
                                 albedo_contrast=0.0,
                                 dem_kw={"rms_slope": 0.0, "n_craters": 0, "slope_deg": 0.0})
    for key in ("source", "reference", "dem"):
        assert np.all(np.isfinite(_read(info[key]).astype(np.float64)))
