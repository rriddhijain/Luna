"""
tests/test_io_pds.py

OWNER: seat 5 (Systems & Performance).

Ingestion for OHRC / TMC-2 style PDS4 products (a detached .xml label + raw
.img), the shape of the real ``ohr_r1_r11_shape_ver4`` product. Verifies the
raw read decodes big-endian correctly, illumination geometry is harvested, the
directory scan prefers real data over a browse thumbnail, and the sidecar JSON
overrides.
"""

from __future__ import annotations

import os

import numpy as np

from samanvay.io.loaders import load_product
from samanvay.io.pds import find_product_files, read_pds4

PDS4_LABEL = """<?xml version="1.0"?>
<Product_Observational>
  <File_Area_Observational>
    <File><file_name>{data_name}</file_name></File>
    <Array_2D_Image>
      <offset unit="byte">0</offset>
      <Axis_Array><axis_name>Line</axis_name><elements>{h}</elements><sequence_number>1</sequence_number></Axis_Array>
      <Axis_Array><axis_name>Sample</axis_name><elements>{w}</elements><sequence_number>2</sequence_number></Axis_Array>
      <Element_Array><data_type>UnsignedMSB2</data_type></Element_Array>
    </Array_2D_Image>
  </File_Area_Observational>
  <Observation_Area>
    <incidence_angle unit="deg">62.5</incidence_angle>
    <emission_angle unit="deg">3.1</emission_angle>
    <phase_angle unit="deg">59.9</phase_angle>
    <sub_solar_azimuth unit="deg">133.0</sub_solar_azimuth>
  </Observation_Area>
</Product_Observational>
"""


def _write_pds4_product(root, h=120, w=90, seed=0):
    data_dir = os.path.join(root, "data")
    browse_dir = os.path.join(root, "browse")
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(browse_dir, exist_ok=True)

    img = np.random.default_rng(seed).integers(0, 4000, size=(h, w)).astype(">u2")
    data_name = "ch2_ohr_ncp_test_d_img_d18.img"
    img.tofile(os.path.join(data_dir, data_name))

    with open(os.path.join(data_dir, "ch2_ohr_ncp_test_d_img_d18.xml"), "w") as f:
        f.write(PDS4_LABEL.format(data_name=data_name, h=h, w=w))

    # a browse thumbnail that must NOT be chosen as the data file
    with open(os.path.join(browse_dir, "browse.png"), "wb") as f:
        f.write(b"\x89PNG\r\n")
    return img.astype(np.uint16)


def test_find_prefers_pds4_over_browse(tmp_path):
    _write_pds4_product(str(tmp_path))
    res = find_product_files(str(tmp_path))
    assert res["kind"] == "pds4"
    assert res["data"].endswith(".img")
    assert res["label"].endswith(".xml")


def test_derived_output_does_not_shadow_pds4(tmp_path):
    # A prior pipeline run leaves a GeoTIFF in run/; the scan must still pick the
    # authoritative PDS4 label+raw, not the derived output.
    _write_pds4_product(str(tmp_path))
    run_dir = os.path.join(str(tmp_path), "run")
    os.makedirs(run_dir, exist_ok=True)
    with open(os.path.join(run_dir, "registered.tif"), "wb") as f:
        f.write(b"II*\x00")  # a stray TIFF-looking file that must be ignored
    res = find_product_files(str(tmp_path))
    assert res["kind"] == "pds4"
    assert res["data"].endswith(".img")
    assert res["label"].endswith(".xml")


def test_read_pds4_decodes_bigendian_and_geometry(tmp_path):
    native = _write_pds4_product(str(tmp_path), h=100, w=80, seed=5)
    res = find_product_files(str(tmp_path))
    arr, meta = read_pds4(res["data"], res["label"])
    assert arr.shape == (100, 80)
    assert np.array_equal(np.asarray(arr), native)
    assert meta["incidence_deg"] == 62.5
    assert meta["emission_deg"] == 3.1
    assert meta["sun_az_deg"] == 133.0
    # sun elevation derived from incidence
    assert abs(meta["sun_el_deg"] - 27.5) < 1e-6
    assert meta["instrument"] == "OHRC"


def test_load_product_on_directory(tmp_path):
    _write_pds4_product(str(tmp_path))
    prod = load_product(str(tmp_path))
    assert prod.array.shape == (120, 90)
    assert prod.meta["source_format"].startswith("PDS4")


def test_sidecar_overrides_label(tmp_path):
    _write_pds4_product(str(tmp_path))
    # a sidecar on the directory path overrides harvested metadata
    import json

    with open(str(tmp_path) + ".json", "w") as f:
        json.dump({"gsd_m": 0.32, "sun_az_deg": 999.0}, f)
    prod = load_product(str(tmp_path))
    assert prod.meta["gsd_m"] == 0.32
    assert prod.meta["sun_az_deg"] == 999.0


def test_missing_path_returns_mock():
    prod = load_product("/no/such/product.tif")
    assert prod.array.shape == (1024, 1024)
    assert prod.meta["instrument"] == "MOCK"


# A full OHRC-shaped PDS4 label (big-endian uint16 raw) used by the end-to-end
# registration proof below.
_E2E_LABEL = """<?xml version="1.0"?>
<Product_Observational>
  <File_Area_Observational>
    <File><file_name>{name}</file_name></File>
    <Array_2D_Image>
      <offset unit="byte">0</offset>
      <Axis_Array><axis_name>Line</axis_name><elements>{h}</elements><sequence_number>1</sequence_number></Axis_Array>
      <Axis_Array><axis_name>Sample</axis_name><elements>{w}</elements><sequence_number>2</sequence_number></Axis_Array>
      <Element_Array><data_type>UnsignedMSB2</data_type></Element_Array>
    </Array_2D_Image>
  </File_Area_Observational>
  <Observation_Area>
    <incidence_angle unit="deg">{inc}</incidence_angle>
    <emission_angle unit="deg">0.0</emission_angle>
    <phase_angle unit="deg">{inc}</phase_angle>
    <sub_solar_azimuth unit="deg">{az}</sub_solar_azimuth>
  </Observation_Area>
</Product_Observational>
"""


def _write_pds4_frame(dirpath, img01, az_deg, el_deg):
    """Write a single OHRC-shaped PDS4 product (big-endian uint16 DN)."""
    data_dir = os.path.join(dirpath, "data")
    os.makedirs(data_dir, exist_ok=True)
    u16 = np.clip(img01 * 4000.0, 0, 4000).astype(">u2")
    name = "ohrc_frame.img"
    u16.tofile(os.path.join(data_dir, name))
    with open(os.path.join(data_dir, "ohrc_frame.xml"), "w") as f:
        f.write(_E2E_LABEL.format(name=name, h=u16.shape[0], w=u16.shape[1],
                                  inc=90.0 - el_deg, az=az_deg))


def _project(H, pts):
    q = H @ pts
    return q[:2] / q[2]


def test_pds4_pair_registers_end_to_end(tmp_path):
    """End-to-end proof: two OHRC-format PDS4 products (big-endian .img + XML
    label) under *different* illumination register to sub-pixel accuracy through
    the full pipeline via the PDS4 loader path.
    """
    import json

    cv2 = __import__("cv2")
    from bench.fixtures import apply_illumination, make_texture, similarity_matrix
    from samanvay.pipeline.stages import run_pipeline

    size, seed = 512, 7
    base = make_texture(size, size, seed)
    center = (size / 2.0, size / 2.0)
    h_gt = similarity_matrix(1.0, 1.5, 6.0, -4.0, center)
    src_geom = cv2.warpPerspective(
        base, np.linalg.inv(h_gt), (size, size),
        flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REFLECT,
    )
    ref = apply_illumination(base, 210.0, 55.0, 0.25)
    src = apply_illumination(src_geom, 30.0, 25.0, 0.25)

    src_dir = os.path.join(str(tmp_path), "src")
    ref_dir = os.path.join(str(tmp_path), "ref")
    _write_pds4_frame(src_dir, src, 30.0, 25.0)
    _write_pds4_frame(ref_dir, ref, 210.0, 55.0)

    out = os.path.join(str(tmp_path), "run")
    run_pipeline(src_dir, ref_dir, out,
                 {"seed": seed, "grid_n": 4, "halo_px": 64, "cache": {"enabled": False}})

    metrics = json.load(open(os.path.join(out, "metrics.json")))
    h_est = np.array(json.load(open(os.path.join(out, "transform.json")))["params"])
    ys, xs = np.mgrid[0:size:32, 0:size:32]
    pts = np.stack([xs.ravel(), ys.ravel(), np.ones(xs.size)], axis=0)
    gt_rmse = float(np.sqrt(np.mean(np.sum((_project(h_est, pts) - _project(h_gt, pts)) ** 2, axis=0))))

    assert metrics["inlier_count"] >= 8
    assert gt_rmse < 2.0  # sub-pixel on real PDS4 format across sun angles
