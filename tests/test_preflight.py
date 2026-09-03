"""Preflight must be right about what the pipeline will do, and must never crash.

A preflight tool that dies on a malformed file is worse than useless: malformed files
are exactly what it exists to find.
"""

import os
from pathlib import Path

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from samanvay.io.preflight import check_pair, check_product, format_report

FIX = "fixtures/synth_pair_A"


def _write(path, array, transform=None, crs="EPSG:32601", nodata=None):
    with rasterio.open(path, "w", driver="GTiff", height=array.shape[0],
                       width=array.shape[1], count=1, dtype=str(array.dtype),
                       crs=crs, transform=transform or from_origin(0, 0, 1, 1),
                       nodata=nodata) as dst:
        dst.write(array, 1)
    return str(path)


def _texture(n=256, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((n, n)) * 255).astype(np.float32)


def test_fixture_pair_is_ready_and_enables_physics():
    r = check_pair(f"{FIX}/source.tif", f"{FIX}/reference.tif", f"{FIX}/dem.tif")
    assert r["verdict"] in ("ready", "ready_degraded")
    assert r["source"]["enables"]["physics"] is True
    assert r["source"]["enables"]["geo_init"] is True
    # The fixture is a deliberate 2x pair; both estimates must agree on it.
    assert r["scale_ratio"] == pytest.approx(0.5, abs=0.01)
    assert r["scale_ratio_from_geotransform"] == pytest.approx(0.5, abs=0.01)
    assert r["overlap_frac"] > 0.9


def test_a_dem_is_not_faulted_for_having_no_sun_angles():
    """A DEM is terrain, not an observation. Warning about it is a false positive."""
    r = check_pair(f"{FIX}/source.tif", f"{FIX}/reference.tif", f"{FIX}/dem.tif")
    dem_msgs = " ".join(i["message"] for i in r["dem"]["issues"])
    assert "sun_az_deg" not in dem_msgs


def test_bare_geotiff_is_degraded_not_blocked(tmp_path):
    """No metadata must never be a blocker: empirical mode is a real fallback."""
    a = _write(tmp_path / "a.tif", _texture())
    b = _write(tmp_path / "b.tif", _texture(seed=1))
    r = check_pair(a, b)
    assert r["verdict"] == "ready_degraded"
    assert r["source"]["enables"]["physics"] is False
    assert r["source"]["enables"]["matching"] is True
    levels = {i["level"] for i in r["source"]["issues"]}
    assert "blocker" not in levels
    fixes = " ".join(i["fix"] for i in r["source"]["issues"])
    assert ".json" in fixes, "the fix must name the sidecar path"


def test_missing_file_blocks_without_raising():
    r = check_pair("does/not/exist.tif", f"{FIX}/reference.tif")
    assert r["verdict"] == "blocked"
    assert any(i["level"] == "blocker" for i in r["source"]["issues"])
    assert isinstance(format_report(r), str)


def test_non_overlapping_pair_is_blocked(tmp_path):
    """The most common real-data mistake: two products of different ground."""
    a = _write(tmp_path / "a.tif", _texture(), transform=from_origin(0, 0, 1, 1))
    b = _write(tmp_path / "b.tif", _texture(seed=2),
               transform=from_origin(500000, 500000, 1, 1))
    r = check_pair(a, b)
    assert r["overlap_frac"] is not None and r["overlap_frac"] < 0.05
    assert r["verdict"] == "blocked"


def test_constant_raster_is_blocked(tmp_path):
    a = _write(tmp_path / "flat.tif", np.full((256, 256), 7.0, dtype=np.float32))
    b = _write(tmp_path / "b.tif", _texture())
    r = check_pair(a, b)
    assert r["verdict"] == "blocked"


def test_every_issue_carries_an_actionable_fix(tmp_path):
    """A message without a fix is a complaint, not help."""
    a = _write(tmp_path / "a.tif", _texture(n=64))
    b = _write(tmp_path / "b.tif", _texture(n=64, seed=1))
    r = check_pair(a, b)
    issues = r["source"]["issues"] + r["reference"]["issues"] + r["issues"]
    for i in issues:
        if i["level"] in ("blocker", "warning"):
            assert i["fix"], f"no fix given for: {i['message']}"


def test_recommendation_follows_the_sun_difference():
    """Large or unknown delta sun must recommend RIFT; that is the measured result."""
    r = check_pair(f"{FIX}/source.tif", f"{FIX}/reference.tif")
    assert r["delta_sun_az_deg"] is not None and r["delta_sun_az_deg"] >= 20
    assert r["recommended_config"]["match.method"] == "rift"
    assert r["recommended_reasons"]


def test_report_renders_for_every_verdict(tmp_path):
    good = format_report(check_pair(f"{FIX}/source.tif", f"{FIX}/reference.tif"))
    bad = format_report(check_pair("nope.tif", "also_nope.tif"))
    assert "VERDICT" in good and "samanvay register" in good
    assert "VERDICT" in bad
    # A blocked pair must not hand the user a command that cannot work.
    assert "samanvay register" not in bad


# --- batch directory scan -------------------------------------------------

from samanvay.io.preflight import (  # noqa: E402
    _box_overlap, _candidate_paths, _delta_sun, _looks_like_dem, _stamp_of,
    format_scan, rank_pairs, scan_products)


def test_box_overlap_is_fraction_of_the_smaller_box():
    assert _box_overlap((0, 10, 0, 10), (0, 10, 0, 10)) == 1.0
    assert _box_overlap((0, 10, 0, 10), (20, 30, 0, 10)) == 0.0
    # A small box wholly inside a big one is fully covered, not 25% covered.
    assert _box_overlap((0, 10, 0, 10), (0, 5, 0, 10)) == 1.0
    assert abs(_box_overlap((0, 10, 0, 10), (5, 15, 0, 10)) - 0.5) < 1e-9
    assert _box_overlap((0, 10, 0, 10), (10, 20, 0, 10)) == 0.0   # edge-touching
    assert _box_overlap((0, 0, 0, 0), (0, 10, 0, 10)) == 0.0      # degenerate


def test_dem_detected_for_both_naming_conventions():
    assert _looks_like_dem("ch2_tmc_ndn_20211122T1726562077_d_dtm_d18.xml")
    assert _looks_like_dem("dem.tif")
    assert _looks_like_dem("site_dem.tif")
    assert not _looks_like_dem("ch2_tmc_ndn_20211122T1726562077_d_oth_d18.xml")
    assert not _looks_like_dem("source.tif")


def test_stamp_extraction_tolerates_subsecond_length():
    assert _stamp_of("ch2_tmc_ndn_20211122T1726562077_d_oth_d18") == "20211122T1726562077"
    assert _stamp_of("ch2_x_20200114T103022_d_img") == "20200114T103022"
    assert _stamp_of("reference.tif") is None


def test_delta_sun_wraps_the_short_way():
    assert _delta_sun({"sun_az_deg": 350}, {"sun_az_deg": 10}) == 20.0
    assert _delta_sun({"sun_az_deg": 10}, {"sun_az_deg": 350}) == 20.0
    assert _delta_sun({"sun_az_deg": None}, {"sun_az_deg": 10}) is None


def test_pds4_img_is_shadowed_by_its_own_xml(tmp_path):
    (tmp_path / "p.xml").write_text("<x/>")
    (tmp_path / "p.img").write_bytes(b"\0")
    (tmp_path / "other.tif").write_bytes(b"\0")
    (tmp_path / "bundle.zip").write_bytes(b"\0")
    names = {os.path.basename(p) for p in _candidate_paths(tmp_path)}
    # One PDS4 product must be scanned once, via the label GDAL actually opens.
    assert names == {"p.xml", "other.tif"}


def test_scan_ranks_the_real_fixture_sweep():
    root = Path(__file__).resolve().parents[1] / "fixtures" / "dsun_sweep"
    if not root.exists():
        pytest.skip("fixture sweep not generated")
    products, _ = scan_products(root)
    assert products, "no rasters found"
    assert any(p["is_dem"] for p in products)
    # Colliding basenames must be disambiguated or the report is unreadable.
    assert len({p["name"] for p in products}) == len(products)

    pairs = rank_pairs(products)
    assert pairs and all(not p["a"]["is_dem"] and not p["b"]["is_dem"] for p in pairs)
    # Over identical ground, the widest sun difference ranks first. Asserted as the
    # property rather than as a literal: the sweep used to stop at 50 deg and now runs to
    # 180, and a test pinned to the fixture set fails on the fixture growing rather than
    # on the ranking breaking — which is the only thing this line is here to check.
    widest = max(p["delta_sun_az_deg"] for p in pairs)
    assert pairs[0]["delta_sun_az_deg"] == widest
    assert widest >= 50.0
    assert pairs[0]["overlap"] > 0.9
    assert "samanvay register" in format_scan(products, pairs, [])


def test_no_overlap_sorts_below_overlap_despite_a_better_sun():
    def scene(name, box, az, dem=False):
        return {"path": f"/x/{name}", "name": name, "box": box, "crs": "EPSG:1",
                "sun_az_deg": az, "is_dem": dem, "stamp": None, "shape": (2, 2)}
    far = scene("far.tif", (99, 100, 99, 100), 200.0)     # 90 deg apart, no shared ground
    a = scene("a.tif", (0, 1, 0, 1), 110.0)
    b = scene("b.tif", (0, 1, 0, 1), 150.0)               # 40 deg apart, same ground
    pairs = rank_pairs([a, b, far])
    assert pairs[0]["overlap"] == 1.0
    assert pairs[-1]["overlap"] == 0.0


def test_a_multi_band_cube_is_a_warning_that_names_the_reduction(tmp_path):
    """A 250-band IIRS cube certified "ready" with a note reads as "nothing to know here".

    What the pipeline matches is a derived map, not any band in the file, and which map it
    is changes the answer — that is a degraded mode and it is what a warning is for.
    """
    cube = np.stack([_texture(seed=s) for s in range(4)])
    path = str(tmp_path / "cube.tif")
    with rasterio.open(path, "w", driver="GTiff", height=cube.shape[1],
                       width=cube.shape[2], count=cube.shape[0], dtype="float32",
                       crs="EPSG:32601", transform=from_origin(0, 0, 1, 1)) as dst:
        dst.write(cube)
    other = _write(tmp_path / "b.tif", _texture(seed=9))

    r = check_pair(path, other)
    band_issues = [i for i in r["source"]["issues"] if "bands" in i["message"]]
    assert len(band_issues) == 1
    assert band_issues[0]["level"] == "warning"
    assert "band.reduce" in band_issues[0]["message"]
    assert band_issues[0]["fix"]
    assert r["verdict"] == "ready_degraded"           # not "ready"
    # preflight must not spend a PCA pass over the cube just to say it will happen.
    assert "band.reduce" in format_report(r)
