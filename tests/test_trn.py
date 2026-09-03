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
from samanvay.geometry.init import apply_transform
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


# ------------------------------------------- the basemap the caller actually configured


def _cube(path, bands, meta):
    """A multi-band basemap: what an IIRS-class reference looks like to load_product."""
    stack = np.asarray(bands, dtype=np.uint16)
    with rasterio.open(path, "w", driver="GTiff", height=stack.shape[1],
                       width=stack.shape[2], count=stack.shape[0], dtype="uint16",
                       transform=Affine(1.0, 0.0, 0.0, 0.0, -1.0, stack.shape[1])) as dst:
        dst.write(stack)
    with open(str(path) + ".json", "w") as handle:
        json.dump(meta, handle)
    return str(path)


def test_a_cube_basemap_is_reduced_by_the_configured_band_not_the_default(tmp_path):
    """A multi-band reference must go through config["band"], not io/bands.py's pc1 default.

    The simulated frame is cut out of the basemap, so if the simulator reduces the cube
    one way and localise() reduces it another, the demo is matching two different images
    and every TRN number describes a pairing that could not occur in a real descent.
    """
    size = 96
    structure = (_terrain(size, seed=11, scale=6) * 50000).astype(np.uint16)
    flipped = np.ascontiguousarray(structure[::-1, ::-1])
    ref_path = _cube(tmp_path / "cube.tif", [structure, structure, flipped],
                     {"product_id": "cube_ref", "gsd_m": 1.0,
                      "sun_az_deg": SUN_AZ, "sun_el_deg": SUN_EL})

    pinned = {"band": {"index": 3, "reduce": "band"}}
    sim = trn.simulate_descent_frame(
        ref_path, center_xy=(size / 2.0, size / 2.0), altitude_scale=1.0,
        sun=(SUN_AZ, SUN_EL), seed=0, frame_size=size, rotation_deg=0.0,
        noise_sigma=0.0, config=pinned)

    # The oracle is the same renderer fed the configured reduction directly: at scale 1,
    # no rotation and no noise the frame is that render, pixel for pixel.
    want, _, _ = trn._reilluminated(ref_path, None, SUN_AZ, SUN_EL, "lommel_seeliger",
                                    json.dumps(trn._band_cfg(pinned), sort_keys=True))
    assert np.allclose(sim["image"], want, atol=1e-5)

    # And it is genuinely a different image from the default reduction — band 3 is the
    # flipped copy, so a run that ignored the config would fail this.
    default, _, _ = trn._reilluminated(ref_path, None, SUN_AZ, SUN_EL, "lommel_seeliger",
                                       "null")
    assert not np.allclose(sim["image"], default, atol=1e-3)


def test_a_non_square_frame_gets_a_budget_for_every_cell(tmp_path, monkeypatch):
    """cell_budgets must cover the N x M grid match_tiled actually tiles, not grid_n**2.

    grid_n**2 is right only for a square frame. On a 2:1 frame at grid_n=2 the grid is
    2 x 4, and the four cell ids past the budget dict fall back to match_tiled's own
    hardcoded quotas — the configured min/max silently stop applying to half the frame.
    """
    from samanvay.types import MatchSet

    captured = {}

    def _stub(src_canon, ref_canon, **kwargs):
        captured["budgets"] = kwargs.get("cell_budgets")
        captured["grid_n"] = kwargs.get("grid_n")
        empty = MatchSet(src_xy=np.zeros((0, 2)), ref_xy=np.zeros((0, 2)),
                         score=np.zeros(0, np.float32), method=np.zeros(0, np.uint8),
                         cell=np.zeros(0, np.int32))
        return empty, {}

    monkeypatch.setattr(trn, "match_tiled", _stub)

    frame = (_terrain(160, seed=2, scale=6) * 60000).astype(np.uint16)[:, :]
    frame = np.hstack([frame, frame])                       # 160 x 320, a 1:2 frame
    ref = (_terrain(160, seed=3, scale=6) * 60000).astype(np.uint16)
    frame_path = _write(tmp_path / "wide.tif", frame, {"product_id": "wide", "gsd_m": 1.0})
    ref_path = _write(tmp_path / "base.tif", ref, {"product_id": "base", "gsd_m": 1.0})

    fix = trn.localise(frame_path, ref_path, config={"grid_n": 2})
    assert captured["grid_n"] == 2
    assert sorted(captured["budgets"]) == list(range(8))     # 2 rows x 4 cols
    assert fix["status"] == "failed"                         # no matches from the stub


def test_a_square_frame_keeps_the_square_budget(tmp_path, monkeypatch):
    """The aspect rule must not move the square case: grid_n=2 on a square frame is 4 cells."""
    from samanvay.types import MatchSet

    captured = {}

    def _stub(src_canon, ref_canon, **kwargs):
        captured["budgets"] = kwargs.get("cell_budgets")
        empty = MatchSet(src_xy=np.zeros((0, 2)), ref_xy=np.zeros((0, 2)),
                         score=np.zeros(0, np.float32), method=np.zeros(0, np.uint8),
                         cell=np.zeros(0, np.int32))
        return empty, {}

    monkeypatch.setattr(trn, "match_tiled", _stub)
    img = (_terrain(160, seed=4, scale=6) * 60000).astype(np.uint16)
    path = _write(tmp_path / "sq.tif", img, {"product_id": "sq", "gsd_m": 1.0})
    trn.localise(path, path, config={"grid_n": 2})
    assert sorted(captured["budgets"]) == list(range(4))


def test_an_accepted_spline_does_not_cost_the_error_ellipse():
    """"similarity+tps" is still a similarity as far as the covariance is concerned.

    verify.py renames the model when a TPS is kept, and _DOF is keyed on the ladder's
    names — so before the suffix was stripped, every descent frame that fitted a spline
    came back with ellipse=None: a position fix with no uncertainty, which this module
    exists to refuse to produce.
    """
    rng = np.random.default_rng(0)
    src = rng.uniform(0.0, 100.0, size=(30, 2))
    ref = src + 0.05 * rng.standard_normal(src.shape)
    plain, dof = trn.position_covariance("similarity", np.eye(3), src, ref, (50.0, 50.0))
    tps, dof_tps = trn.position_covariance("similarity+tps", np.eye(3), src, ref,
                                           (50.0, 50.0))
    assert plain is not None and tps is not None
    assert dof_tps == dof
    assert np.array_equal(tps, plain)          # the 3x3 part is what is propagated
    # An unknown model is still refused: the strip must not turn every name into a match.
    assert trn.position_covariance("bilinear+tps", np.eye(3), src, ref, (5.0, 2.0))[0] is None


# ------------------------------------------- the artifact must name the delivered model


def test_grid_aspect_false_is_honoured_and_reaches_the_matcher(tmp_path, monkeypatch):
    """grid_aspect is a TOP-level key and match_tiled reads it from the match section.

    Without the hop pipeline/stages.py makes, neither side ever finds it: a caller who
    asked for a square grid still got the 2 x 4 aspect grid on a 160 x 320 frame, and
    nothing said so. Both the budget and the matcher must see the same answer, and it
    must be the one that was configured.
    """
    from samanvay.types import MatchSet

    captured = {}

    def _stub(src_canon, ref_canon, **kwargs):
        captured["budgets"] = kwargs.get("cell_budgets")
        captured["grid_aspect"] = (kwargs.get("config") or {}).get("grid_aspect")
        empty = MatchSet(src_xy=np.zeros((0, 2)), ref_xy=np.zeros((0, 2)),
                         score=np.zeros(0, np.float32), method=np.zeros(0, np.uint8),
                         cell=np.zeros(0, np.int32))
        return empty, {}

    monkeypatch.setattr(trn, "match_tiled", _stub)
    frame = (_terrain(160, seed=5, scale=6) * 60000).astype(np.uint16)
    frame = np.hstack([frame, frame])                        # 160 x 320
    path = _write(tmp_path / "wide.tif", frame, {"product_id": "wide", "gsd_m": 1.0})
    ref = _write(tmp_path / "base.tif",
                 (_terrain(160, seed=6, scale=6) * 60000).astype(np.uint16),
                 {"product_id": "base", "gsd_m": 1.0})

    trn.localise(path, ref, config={"grid_n": 2, "grid_aspect": False})
    assert captured["grid_aspect"] is False                  # it travelled to match_tiled
    assert sorted(captured["budgets"]) == list(range(4))     # 2 x 2, as asked

    trn.localise(path, ref, config={"grid_n": 2})            # default is still the aspect grid
    assert captured["grid_aspect"] is True
    assert sorted(captured["budgets"]) == list(range(8))


def test_the_fix_names_the_model_that_was_actually_delivered(tmp_path):
    """An accepted spline must be visible in trn.json, not only inside the ellipse.

    verify.py reports the ladder RUNG in metrics["model"] and the delivered model in
    model_type. Quoting the rung made trn.json claim a plain similarity while rmse_px
    beside it was the full model's residual — the same "the artifact does not describe
    what shipped" defect the transform.json warp block exists to close. geometry.tps
    defaults to "auto" and this synthetic scene accepts one, so the default TRN path was
    the affected path, not a corner case. `warp_applied` is a top-level key because
    ellipse["excludes_warp"] is absent on every fix that has no ellipse.
    """
    ref_path, dem_path = _scene(tmp_path, size=192)
    frame = trn.simulate_descent_frame(ref_path, dem_path, altitude_scale=1.0,
                                       sun=(SUN_AZ, SUN_EL), seed=0, frame_size=128,
                                       rotation_deg=0.0, noise_sigma=0.0)

    fix = trn.localise(frame, ref_path)
    assert fix["status"] == "ok"
    assert fix["warp_applied"] is True
    assert fix["model"].endswith("+tps")
    # The fix itself is still the global 3x3 — the spline maps reference -> source and no
    # inverse TPS exists — so the position and its ellipse come from the same model, and
    # warp_applied is what tells the reader that rmse_px does not.
    centre = apply_transform(np.asarray(fix["H_frame_to_ref"]), [fix["frame_center_xy"]])[0]
    assert fix["estimated_center_xy"] == pytest.approx(tuple(centre), abs=1e-12)

    off = trn.localise(frame, ref_path, config={"geometry": {"tps": False}})
    assert off["status"] == "ok"
    assert off["warp_applied"] is False
    assert "+tps" not in off["model"]
