"""Seat 4 tests: a report that always renders, never phones home, and a valid DZI pyramid."""

import os
import re
import sys
import xml.etree.ElementTree as ET

import cv2
import numpy as np
import rasterio

from samanvay.report.render import render_report
from samanvay.types import MatchSet, Product, Registration

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO not in sys.path:
    sys.path.insert(0, REPO)

from viewer.tiles import build_dzi, dzi_max_level, level_size, tile_grid  # noqa: E402

SHAPE = (140, 180)  # (h, w), deliberately not square


def product(name, seed=0, gsd_m=5.0):
    rng = np.random.default_rng(seed)
    arr = (rng.random(SHAPE) * 1000.0).astype(np.float32)
    arr[40:70, 60:110] += 400.0  # something with an edge in it
    return Product(path=f"/nowhere/{name}.tif", array=arr,
                   meta={"product_id": name, "instrument": "TMC-2", "gsd_m": gsd_m,
                         "shape": SHAPE, "dtype": "float32", "sun_el_deg": 42.0})


def full_run(n=24, grid_n=4):
    """A plausible successful run: matches, inliers, residuals and a full metrics dict."""
    rng = np.random.default_rng(3)
    src_xy = np.column_stack([rng.uniform(5, SHAPE[1] - 5, n),
                              rng.uniform(5, SHAPE[0] - 5, n)])
    matches = MatchSet(src_xy=src_xy, ref_xy=src_xy + np.array([2.0, -1.0]),
                       score=rng.random(n).astype(np.float32),
                       method=np.zeros(n, np.uint8), cell=np.zeros(n, np.int32))
    inliers = np.ones(n, bool)
    inliers[:4] = False
    residuals = rng.normal(0.0, 0.3, size=(n, 2))
    residuals[~inliers] *= 12.0
    rmse = float(np.sqrt(np.mean(np.sum(residuals[inliers] ** 2, axis=1))))
    counts = [3, 2, 0, 0, 4, 1, 2, 0, 0, 3, 1, 2, 0, 1, 3, 2]
    states = ["populated" if c else ("masked_invalid" if i % 3 == 0 else "insufficient_texture")
              for i, c in enumerate(counts)]
    metrics = {"rmse_px": rmse, "inlier_count": int(inliers.sum()), "match_count": n,
               "inlier_ratio": float(inliers.mean()), "coverage_pct": 68.75,
               "dispersion_cv": 0.41, "grid_n": grid_n, "runtime_s": 1.25,
               "mean_sigma_px": 0.22, "refined_count": n, "model_type": "affine",
               "model_margin": None, "cell_counts": counts, "cell_states": states}
    registration = Registration(model_type="affine", params=np.eye(3),
                                init_params=np.eye(3), inliers=inliers,
                                residuals=residuals, sigma=np.full(n, 0.22), metrics=metrics)
    return matches, registration


def test_report_renders_end_to_end_and_is_air_gapped(tmp_path):
    src, ref = product("CH2_OHRC_0001", seed=1), product("LRO_NAC_M0001", seed=2)
    matches, registration = full_run()
    registered = np.roll(src.array, 2, axis=1)

    path = render_report(str(tmp_path), src, ref, registration, matches, registered,
                         {"grid_n": 4, "seed": 7})
    assert path == os.path.join(str(tmp_path), "report.html")
    html = open(path, encoding="utf-8").read()

    # Air gap: the deployment target has no network and this gets emailed to judges.
    assert "http://" not in html
    assert "https://" not in html
    assert "<script" not in html.lower()
    assert "<link" not in html.lower()
    srcs = re.findall(r'src="([^"]*)"', html)
    assert len(srcs) >= 6, f"expected all six figures embedded, got {len(srcs)}"
    assert all(s.startswith("data:image/png;base64,") for s in srcs)
    assert "not available" not in html, "every figure had its input; none should be skipped"

    # The numbers a judge asks for, as selectable HTML rather than pixels.
    assert "<table" in html and "rmse_px" in html and "dispersion_cv" in html
    assert f"{registration.metrics['rmse_px']:.6g}" in html
    assert "EXAGGERATION x" in html or "exaggerat" in html.lower()
    assert "REGISTERED" in html


def test_report_renders_when_registration_failed(tmp_path):
    src, ref = product("CH2_OHRC_0002", seed=4), product("LRO_NAC_M0002", seed=5)
    empty = MatchSet(src_xy=np.empty((0, 2)), ref_xy=np.empty((0, 2)),
                     score=np.empty(0, np.float32), method=np.empty(0, np.uint8),
                     cell=np.empty(0, np.int32))
    registration = Registration(model_type="homography", params=np.eye(3),
                                init_params=np.eye(3), inliers=np.zeros(0, bool),
                                residuals=np.empty((0, 2)), sigma=np.empty(0),
                                metrics={"rmse_px": None, "inlier_count": 0,
                                         "match_count": 0, "coverage_pct": None})
    path = render_report(str(tmp_path), src, ref, registration, empty, None, None)
    html = open(path, encoding="utf-8").read()

    assert "FAILED" in html
    assert html.count("not available:") >= 4  # matches, checkerboard, quiver, histogram, grid
    assert "no tie-points were found" in html
    assert "no registered array was produced" in html
    assert "unknown" in html                  # a missing RMSE stays unknown, never 0.0
    assert "http://" not in html and "https://" not in html
    assert re.search(r'src="data:image/png;base64,', html), "figure 1 still has its inputs"


def test_report_survives_a_meta_with_no_gsd(tmp_path):
    src, ref = product("A", seed=6, gsd_m=None), product("B", seed=7, gsd_m=None)
    matches, registration = full_run()
    path = render_report(str(tmp_path), src, ref, registration, matches, src.array, {})
    html = open(path, encoding="utf-8").read()
    # No gsd means no scale bar, but nothing may be skipped and nothing may be invented.
    assert len(re.findall(r'src="data:image/png;base64,', html)) >= 6
    assert "not available" not in html
    assert "<table" in html


def dzi_source(tmp_path, shape=(200, 300)):
    path = str(tmp_path / "big.tif")
    rng = np.random.default_rng(11)
    arr = rng.integers(0, 4000, size=shape, dtype=np.uint16)
    with rasterio.open(path, "w", driver="GTiff", height=shape[0], width=shape[1],
                       count=1, dtype="uint16", tiled=True,
                       blockxsize=64, blockysize=64) as dst:
        dst.write(arr, 1)
    return path


def test_dzi_descriptor_and_pyramid(tmp_path):
    shape = (200, 300)  # (h, w)
    src = dzi_source(tmp_path, shape)
    out = str(tmp_path / "pyr.dzi")
    info = build_dzi(src, out, tile_size=64, overlap=1, quality=70)

    root = ET.parse(out).getroot()
    assert root.tag.endswith("Image")
    assert root.attrib["TileSize"] == "64"
    assert root.attrib["Overlap"] == "1"
    assert root.attrib["Format"] == "jpg"
    size = list(root)[0]
    assert size.tag.endswith("Size")
    assert (int(size.attrib["Width"]), int(size.attrib["Height"])) == (shape[1], shape[0])

    max_level = dzi_max_level(shape[1], shape[0])
    assert max_level == 9 and info["levels"] == max_level + 1

    total = 0
    for level in range(max_level + 1):
        lw, lh = level_size(shape[1], shape[0], level, max_level)
        cols, rows = tile_grid(lw, lh, 64)
        level_dir = os.path.join(info["files_dir"], str(level))
        names = sorted(os.listdir(level_dir))
        assert len(names) == cols * rows, f"level {level}: {len(names)} tiles, want {cols * rows}"
        assert set(names) == {f"{c}_{r}.jpg" for c in range(cols) for r in range(rows)}
        total += cols * rows
    assert info["tiles"] == total
    assert os.listdir(info["files_dir"]) != []
    assert not os.path.exists(os.path.join(info["files_dir"], str(max_level + 1)))


def test_dzi_tiles_carry_the_overlap(tmp_path):
    shape = (200, 300)
    src = dzi_source(tmp_path, shape)
    out = str(tmp_path / "pyr.dzi")
    info = build_dzi(src, out, tile_size=64, overlap=1, quality=70)
    top = os.path.join(info["files_dir"], str(info["max_level"]))

    # Full-res level: 5 cols x 4 rows. A corner tile grows by the overlap on its two
    # interior edges only; an interior tile grows on all four.
    corner = cv2.imread(os.path.join(top, "0_0.jpg"), cv2.IMREAD_UNCHANGED)
    interior = cv2.imread(os.path.join(top, "1_1.jpg"), cv2.IMREAD_UNCHANGED)
    last = cv2.imread(os.path.join(top, "4_3.jpg"), cv2.IMREAD_UNCHANGED)
    assert corner.shape[:2] == (65, 65)
    assert interior.shape[:2] == (66, 66)
    assert last.shape[:2] == (200 - 192 + 1, 300 - 256 + 1)
