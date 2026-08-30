"""Preflight must be right about what the pipeline will do, and must never crash.

A preflight tool that dies on a malformed file is worse than useless: malformed files
are exactly what it exists to find.
"""

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
