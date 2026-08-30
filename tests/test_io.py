"""Seat 3 — I/O contract tests: metadata honesty, loading, and the six exported artifacts."""

import csv
import json
import os

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from samanvay.io.loaders import load_product
from samanvay.io.metadata import normalise_meta, read_metadata
from samanvay.io.writers import MATCH_COLUMNS, write_outputs
from samanvay.types import MatchSet, Registration

CRS = "EPSG:32601"
TRANSFORM = from_origin(4000.0, 9000.0, 5.0, 5.0)


def _tif(path, h=32, w=48, dtype="uint8", crs=CRS, transform=TRANSFORM, tags=None):
    data = (np.arange(h * w).reshape(h, w) % 250).astype(dtype)
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=1,
                       dtype=dtype, crs=crs, transform=transform) as dst:
        dst.write(data, 1)
        if tags:
            dst.update_tags(**tags)
    return str(path)


def _matchset(n=5):
    rng = np.random.default_rng(0)
    src = rng.uniform(0, 32, size=(n, 2))
    return MatchSet(src_xy=src, ref_xy=src + 1.5,
                    score=np.full(n, 0.8, dtype=np.float32),
                    method=np.zeros(n, dtype=np.uint8),
                    cell=np.arange(n, dtype=np.int32))


def _registration(n=5):
    residuals = np.tile(np.array([[3.0, 4.0]]), (n, 1))  # norm == 5 source px
    return Registration(model_type="affine", params=np.eye(3), init_params=np.eye(3),
                        inliers=np.ones(n, dtype=bool), residuals=residuals,
                        sigma=np.full(n, 0.25), metrics={"rmse_px": 5.0, "inlier_count": n,
                                                         "model_margin": 0.31,
                                                         "rejected_models": [
                                                             {"model_type": "homography",
                                                              "reason": "margin below threshold"}]})


# ---------------------------------------------------------------- metadata

def test_bare_geotiff_does_not_invent_sun_angles(tmp_path):
    path = _tif(tmp_path / "bare.tif")
    meta = normalise_meta(read_metadata(path), path)
    assert meta["sun_az_deg"] is None
    assert meta["sun_el_deg"] is None          # the old loader fabricated 45.0 here
    assert meta["sun_el_deg"] != 45.0
    assert meta["incidence_deg"] is None
    assert meta["meta_source"]["sun_az_deg"] == "unknown"
    assert meta["meta_source"]["sun_el_deg"] == "unknown"
    # the raster facts are still known, and come from the file
    assert meta["shape"] == (32, 48)
    assert meta["dtype"] == "uint8"
    assert meta["gsd_m"] == 5.0
    assert meta["meta_source"]["geotransform"] == "geotiff"


def test_sidecar_overrides_geotiff_tags(tmp_path):
    path = _tif(tmp_path / "s.tif", tags={"SUN_AZIMUTH": "10.0", "SOLAR_ELEVATION": "11.0"})
    with open(path + ".json", "w") as f:
        json.dump({"sun_az_deg": 123.5, "sun_el_deg": 22.25, "instrument": "OHRC"}, f)
    meta = normalise_meta(read_metadata(path), path)
    assert meta["sun_az_deg"] == 123.5
    assert meta["sun_el_deg"] == 22.25
    assert meta["instrument"] == "OHRC"
    assert meta["meta_source"]["sun_az_deg"] == "sidecar"


def test_pds3_label_maps_spellings_and_derives_elevation(tmp_path):
    path = _tif(tmp_path / "m1.tif")
    (tmp_path / "m1.LBL").write_text(
        "PDS_VERSION_ID = PDS3\r\n"
        'PRODUCT_ID     = "M1234567890LE"\r\n'
        "INSTRUMENT_ID  = NACL\r\n"
        "SUB_SOLAR_AZIMUTH = 213.4 <deg>\r\n"
        "INCIDENCE_ANGLE   = 71.85 <deg>\r\n"
        "EMISSION_ANGLE    = 1.2 <deg>\r\n"
        "PHASE_ANGLE       = 70.6 <deg>\r\n"
        "MAP_SCALE         = 0.5 <KM/PIXEL>\r\n"
        "END\r\n")
    meta = normalise_meta(read_metadata(path), path)
    assert meta["product_id"] == "M1234567890LE"
    assert meta["instrument"] == "NACL"
    assert meta["sun_az_deg"] == 213.4
    assert meta["incidence_deg"] == 71.85
    assert meta["emission_deg"] == 1.2
    assert meta["sun_el_deg"] == pytest.approx(90.0 - 71.85)
    assert meta["meta_source"]["sun_el_deg"] == "derived_from_incidence"
    assert meta["meta_source"]["sun_az_deg"] == "pds_label"
    assert meta["gsd_m"] == 500.0                      # km/pixel converted to metres


def test_pds4_xml_label(tmp_path):
    path = _tif(tmp_path / "m2.tif")
    (tmp_path / "m2.xml").write_text(
        '<?xml version="1.0"?>'
        '<Product_Observational xmlns="http://pds.nasa.gov/pds4/pds/v1">'
        "<Identification_Area><logical_identifier>urn:isro:ch2:ohrc:0001</logical_identifier>"
        "<instrument>OHRC</instrument></Identification_Area>"
        '<Geometry><incidence_angle unit="deg">60.0</incidence_angle>'
        '<solar_azimuth unit="deg">12.5</solar_azimuth></Geometry>'
        "</Product_Observational>")
    meta = normalise_meta(read_metadata(path), path)
    assert meta["product_id"] == "urn:isro:ch2:ohrc:0001"
    assert meta["sun_az_deg"] == 12.5
    assert meta["sun_el_deg"] == pytest.approx(30.0)
    assert meta["meta_source"]["sun_el_deg"] == "derived_from_incidence"


def test_ungeoreferenced_tif_reports_unknown_crs(tmp_path):
    path = _tif(tmp_path / "plain.tif", crs=None, transform=None)
    meta = normalise_meta(read_metadata(path), path)
    assert meta["crs"] is None
    assert meta["gsd_m"] is None                        # identity transform is not a GSD in metres
    assert meta["meta_source"]["crs"] == "unknown"


# ---------------------------------------------------------------- loaders

def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_product(str(tmp_path / "nope.tif"))


def test_load_product_keeps_native_dtype(tmp_path):
    path = _tif(tmp_path / "l.tif", dtype="uint16")
    product = load_product(path)
    assert isinstance(product.array, np.ndarray)
    assert product.array.dtype == np.uint16
    assert product.array.shape == (32, 48)
    assert product.meta["crs"] == CRS


def test_large_product_gets_a_tiled_reader(tmp_path):
    pytest.importorskip("samanvay.core.tiling")
    path = _tif(tmp_path / "big.tif")
    product = load_product(path, max_bytes=1)           # force the tiled branch
    assert not isinstance(product.array, np.ndarray)
    assert tuple(product.array.shape) == (32, 48)


# ---------------------------------------------------------------- writers

def _run_write(tmp_path, matches=None, registration=None):
    src_path = _tif(tmp_path / "src.tif", h=32, w=48, dtype="uint8")
    ref_path = _tif(tmp_path / "ref.tif", h=40, w=50, dtype="uint16",
                    transform=from_origin(1000.0, 5000.0, 2.0, 2.0))
    source, reference = load_product(src_path), load_product(ref_path)
    matches = _matchset() if matches is None else matches
    registration = _registration() if registration is None else registration
    warped = np.zeros((40, 50), dtype=np.float32)
    warped[5:20, 5:20] = 200.7
    out = tmp_path / "out"
    write_outputs(str(out), source, reference, registration, matches, warped, {"seed": 7})
    return out, reference


def test_registered_tif_lives_on_the_reference_grid(tmp_path):
    out, reference = _run_write(tmp_path)
    for name in ("registered.tif", "matches.csv", "transform.json",
                 "metrics.json", "provenance.json", "report.html"):
        assert (out / name).exists(), name
    with rasterio.open(out / "registered.tif") as dst:
        assert str(dst.crs) == reference.meta["crs"]
        assert list(dst.transform)[:6] == list(reference.meta["geotransform"])
        assert dst.shape == (40, 50)
        assert dst.dtypes[0] == "uint8"                 # source dtype preserved, not float
        assert dst.nodata == 0
        assert dst.profile["tiled"] is True
        assert dst.profile["compress"].lower() == "deflate"
        band = dst.read(1)
    assert band[10, 10] == 201                          # float warp rounded into uint8
    assert band[0, 0] == 0                              # uncovered area == declared nodata


def test_matches_csv_schema_and_roundtrip(tmp_path):
    out, _ = _run_write(tmp_path)
    with open(out / "matches.csv") as f:
        rows = list(csv.reader(f))
    assert rows[0] == MATCH_COLUMNS
    assert len(rows) == 6
    first = dict(zip(MATCH_COLUMNS, rows[1]))
    assert int(first["id"]) == 0
    assert float(first["residual_px"]) == pytest.approx(5.0)   # ||(3,4)|| in SOURCE px
    assert float(first["sigma_px"]) == pytest.approx(0.25)
    assert first["is_inlier"] == "1"
    assert float(first["ref_x"]) - float(first["src_x"]) == pytest.approx(1.5)


def test_zero_matches_writes_header_only(tmp_path):
    empty = MatchSet(src_xy=np.zeros((0, 2)), ref_xy=np.zeros((0, 2)),
                     score=np.zeros(0, np.float32), method=np.zeros(0, np.uint8),
                     cell=np.zeros(0, np.int32))
    failed = Registration("similarity", np.eye(3), np.eye(3), np.zeros(0, bool),
                          np.zeros((0, 2)), np.zeros(0), {})
    out, _ = _run_write(tmp_path, matches=empty, registration=failed)
    with open(out / "matches.csv") as f:
        assert list(csv.reader(f)) == [MATCH_COLUMNS]


def test_length_mismatch_is_surfaced_not_padded(tmp_path):
    bad = Registration("affine", np.eye(3), np.eye(3), np.ones(3, bool),
                       np.zeros((3, 2)), np.zeros(3), {})
    with pytest.raises(ValueError):
        _run_write(tmp_path, registration=bad)          # 5 matches, 3 inliers


def test_transform_json_records_the_ladder(tmp_path):
    out, _ = _run_write(tmp_path)
    doc = json.loads((out / "transform.json").read_text())
    assert doc["model_type"] == "affine"
    assert np.allclose(doc["params"], np.eye(3))
    assert doc["model_margin"] == 0.31
    assert doc["rejected_models"][0]["model_type"] == "homography"


def test_provenance_has_a_real_git_sha(tmp_path):
    out, _ = _run_write(tmp_path)
    doc = json.loads((out / "provenance.json").read_text())
    sha = doc["git_sha"]
    assert isinstance(sha, str) and len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)
    assert sha != "skeleton-sha-12345"
    assert doc["git_dirty"] in (True, False)
    assert doc["seed"] == 7
    assert doc["package_versions"]["numpy"] == np.__version__
    assert doc["inputs"]["source"]["size_bytes"] > 0
    assert doc["timestamp_utc"].endswith("+00:00")


def test_transform_json_derives_the_ladder_from_model_candidates(tmp_path):
    reg = _registration()
    reg.metrics.pop("rejected_models")
    reg.metrics["model_candidates"] = {
        "similarity": {"rmse_px": 9.0, "rmse_common_px": 9.0, "inlier_count": 3},
        "affine": {"rmse_px": 5.0, "rmse_common_px": 5.0, "inlier_count": 5},
        "homography": {"rmse_px": 4.9, "rmse_common_px": 4.9, "inlier_count": 5},
    }
    out, _ = _run_write(tmp_path, registration=reg)
    doc = json.loads((out / "transform.json").read_text())
    by_model = {r["model_type"]: r for r in doc["rejected_models"]}
    assert set(by_model) == {"similarity", "homography"}       # "affine" was chosen
    assert "outside model_margin" in by_model["similarity"]["reason"]
    assert "simpler model" in by_model["homography"]["reason"]
    assert by_model["similarity"]["rmse_px"] == 9.0


def test_metrics_json_is_strict_json_when_the_fit_failed(tmp_path):
    failed = Registration("failed", np.eye(3), np.eye(3), np.ones(5, bool),
                          np.full((5, 2), np.nan), np.zeros(5),
                          {"rmse_px": float("nan"), "status": "failed",
                           "inlier_count": np.int64(0)})
    out, _ = _run_write(tmp_path, registration=failed)
    text = (out / "metrics.json").read_text()
    assert "NaN" not in text                              # JSON.parse in the viewer would choke
    doc = json.loads(text)
    assert doc["rmse_px"] is None                         # unknown, not 0.0
    assert doc["inlier_count"] == 0
    with open(out / "matches.csv") as f:
        rows = list(csv.reader(f))
    assert rows[1][MATCH_COLUMNS.index("residual_px")] == "nan"
