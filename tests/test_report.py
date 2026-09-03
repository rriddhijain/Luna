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


def full_run(n=24, grid_n=4, rows=None, cols=None, counts=None, extra=None, roles=True):
    """A plausible successful run: matches, inliers, residuals and a full metrics dict.

    rows/cols default to grid_n x grid_n. Pass them (with matching counts) to build a
    non-square grid, which is what a non-square source actually produces.
    """
    rows = grid_n if rows is None else rows
    cols = grid_n if cols is None else cols
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
    # Every 4th point is held out, exactly as the deterministic split does it.
    role_arr = np.zeros(n, np.uint8)
    role_arr[::4] = 1
    check = role_arr == 1
    check_rmse = float(np.sqrt(np.mean(np.sum(residuals[check] ** 2, axis=1))))
    if counts is None:
        counts = [3, 2, 0, 0, 4, 1, 2, 0, 0, 3, 1, 2, 0, 1, 3, 2]
    states = ["populated" if c else ("masked_invalid" if i % 3 == 0 else "insufficient_texture")
              for i, c in enumerate(counts)]
    metrics = {"rmse_px": rmse, "inlier_count": int(inliers.sum()), "match_count": n,
               "inlier_ratio": float(inliers.mean()), "coverage_pct": 68.75,
               "dispersion_cv": 0.41, "grid_n": grid_n, "grid_rows": rows, "grid_cols": cols,
               "runtime_s": 1.25,
               "mean_sigma_px": 0.22, "refined_count": n, "model_type": "affine",
               "model_margin": None, "cell_counts": counts, "cell_states": states,
               # P1.4: the held-out numbers the report leads with.
               "check_rmse_px": check_rmse, "check_rmse_all_px": check_rmse * 1.1,
               "check_p90_px": check_rmse * 1.6, "check_status": "ok",
               "n_check": int(check.sum()), "n_control": int((~check).sum()),
               "check_fraction": 0.2,
               "inlier_ratio_pass": False, "inlier_ratio_target": 0.85,
               "sdi": 0.4876, "sdi_definition": "sdi = (coverage_pct/100) * 1/(1 + dispersion_cv)",
               "tps_status": "rejected_no_improvement", "tps_applied": False,
               "tps_n_control": 14, "tps_check_rmse_before_px": 0.9,
               "tps_check_rmse_after_px": 1.4,
               "match_method_resolved": "rift",
               "match_method_reason": "delta sun azimuth is 100.0 deg, over the 20 deg bar",
               "verify_init_source": "cascade_transform", "mask_fill": "reflect",
               "mask_fill_px": 32343, "clahe_applied": False, "seed_applied": False,
               "seed_reason": "no seed given: nobody seeded"}
    metrics.update(extra or {})
    registration = Registration(model_type="affine", params=np.eye(3),
                                init_params=np.eye(3), inliers=inliers,
                                residuals=residuals, sigma=np.full(n, 0.22), metrics=metrics,
                                roles=role_arr if roles else None)
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
    assert len(srcs) >= 7, f"expected all seven figures embedded, got {len(srcs)}"
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
    assert len(re.findall(r'src="data:image/png;base64,', html)) >= 7
    assert "not available" not in html
    assert "<table" in html


def _png_shape(section_html):
    """(h, w) of the PNG embedded in one report section."""
    import base64
    b64 = re.search(r'src="data:image/png;base64,([^"]+)"', section_html).group(1)
    img = cv2.imdecode(np.frombuffer(base64.b64decode(b64), np.uint8), cv2.IMREAD_COLOR)
    return int(img.shape[0]), int(img.shape[1])


def _uniformity_section(html):
    """Just section 6, so an assertion about the grid cannot be satisfied by another figure."""
    return re.search(r'<span class="num">6</span>Uniformity grid.*?</section>', html, re.S).group(0)


def test_uniformity_heatmap_renders_on_a_non_square_grid(tmp_path):
    """The Phase B repro: shape (400,1200) at grid_n=4 gives 4x12 = 48 cells, not 16.

    Before the fix _fig_uniformity raised _Missing("cell_counts has 48 cells, grid_n=4
    needs 16") and the coverage heatmap — the plan's uniformity evidence — was silently
    dropped from the report on every non-square source.
    """
    counts = [(i * 7) % 5 for i in range(48)]
    matches, registration = full_run(rows=4, cols=12, counts=counts)
    src, ref = product("NS_SRC", seed=1), product("NS_REF", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    section = _uniformity_section(html)
    assert "not available" not in section, section[:400]
    assert "<img" in section
    # 12 columns over 4 rows must come out wider than tall; a 4x4 reshape could not.
    h, w = _png_shape(section)
    assert w > h, f"the 4x12 grid rendered {w}x{h}"
    sq_matches, sq_reg = full_run()
    square = _uniformity_section(open(
        render_report(str(tmp_path / "sq"), src, ref, sq_reg, sq_matches, src.array, {}),
        encoding="utf-8").read())
    assert w > _png_shape(square)[1], "the 4x12 grid is no wider than the 4x4 one"


def test_uniformity_heatmap_falls_back_to_grid_n_when_the_shape_is_absent(tmp_path):
    """A run written before grid_rows/grid_cols existed still renders its square grid."""
    matches, registration = full_run()
    for key in ("grid_rows", "grid_cols"):
        registration.metrics.pop(key)
    src, ref = product("SQ_SRC", seed=1), product("SQ_REF", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    section = _uniformity_section(html)
    assert "not available" not in section
    h, w = _png_shape(section)
    assert abs(w - h) < 0.35 * max(w, h), f"the square grid rendered {w}x{h}"


def test_a_grid_whose_cell_count_matches_nothing_is_declined_with_its_numbers(tmp_path):
    matches, registration = full_run(rows=4, cols=12, counts=[1] * 47)
    src, ref = product("BAD", seed=1), product("BAD2", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert "cell_counts has 47 cells, grid is 4x12 which needs 48" in html


def test_check_rmse_is_the_headline_and_rmse_px_is_labelled_in_sample(tmp_path):
    matches, registration = full_run()
    m = registration.metrics
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert f'<div class="headnum">{m["check_rmse_px"]:.6g}' in html
    assert "check RMSE · inliers (held out)" in html
    assert "IN-SAMPLE, not accuracy" in html
    assert "rmse_px (in-sample)" in html
    # The two counts that make the held-out number meaningful.
    assert "n_check / n_control" in html
    assert f'{m["n_check"]}' in html and f'{m["n_control"]}' in html
    # And the held-out scatter, which is the picture of the same claim.
    assert "Held-out check points" in html
    assert "not available" not in html


def test_a_missing_check_rmse_says_why_in_words(tmp_path):
    matches, registration = full_run(
        roles=False,
        extra={"check_rmse_px": None, "check_rmse_all_px": None, "check_p90_px": None,
               "check_status": "skipped_too_few_matches", "n_check": 0})
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert "not measured" in html
    assert "too few surviving matches to hold any out" in html
    assert "must not be quoted as accuracy" in html
    # The scatter declines for the same reason rather than drawing an empty axis.
    assert "no control/check split was made: skipped_too_few_matches" in html


def test_the_inlier_ratio_miss_is_shown_as_a_miss(tmp_path):
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert 'class="chip fail"' in html
    assert ">FAIL<" in html
    assert "inlier ratio vs plan target 0.85" in html
    assert f'{registration.metrics["inlier_ratio"]:.6g}' in html


def test_sdi_and_its_formula_are_both_on_the_page(tmp_path):
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert ">SDI<" in html
    assert "0.4876" in html
    assert "sdi = (coverage_pct/100) * 1/(1 + dispersion_cv)" in html
    assert "coverage %" in html and "dispersion cv" in html


def test_a_rejected_tps_shows_the_numbers_that_rejected_it(tmp_path):
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert "rejected_no_improvement" in html
    assert "DISCARDED" in html
    assert "check RMSE before spline" in html and "check RMSE after spline" in html
    assert "0.9" in html and "1.4" in html


def test_the_resolved_arms_are_on_the_page(tmp_path):
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    for key in ("match_method_resolved", "match_method_reason", "verify_init_source",
                "mask_fill", "clahe_applied", "seed_applied"):
        assert key in html, key
    assert "cascade_transform" in html and "reflect" in html


def _pdf_pages(path):
    """Page count straight out of the PDF byte stream — no new dependency to read one."""
    raw = open(path, "rb").read()
    assert raw.startswith(b"%PDF-"), "not a PDF"
    assert raw.rstrip().endswith(b"%%EOF"), "PDF has no trailer"
    return len(re.findall(rb"/Type\s*/Page(?![s])", raw)), len(raw)


def test_pdf_is_written_beside_the_report(tmp_path):
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array,
                              {"report": {"pdf": True}}), encoding="utf-8").read()
    pdf = os.path.join(str(tmp_path), "metrics_report.pdf")
    assert os.path.exists(pdf)
    pages, size = _pdf_pages(pdf)
    assert pages == 4, f"title + grid + quiver + check scatter, got {pages}"
    assert size > 20_000
    assert "metrics_report.pdf written beside this page: 4 pages." in html


def test_the_pdf_spills_rather_than_dropping_metrics(tmp_path):
    """A real run has ~70 metrics keys. None of them may fall off the bottom of page 1."""
    matches, registration = full_run(extra={f"extra_key_{i:03d}": float(i) for i in range(200)})
    src, ref = product("A", seed=1), product("B", seed=2)
    render_report(str(tmp_path), src, ref, registration, matches, src.array, {})
    pages, _ = _pdf_pages(os.path.join(str(tmp_path), "metrics_report.pdf"))
    assert pages > 4, f"200 extra keys must spill onto further pages, got {pages}"


def test_pdf_is_skipped_with_a_reason_when_the_config_says_so(tmp_path):
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array,
                              {"report": {"pdf": False}}), encoding="utf-8").read()
    assert not os.path.exists(os.path.join(str(tmp_path), "metrics_report.pdf"))
    assert "metrics_report.pdf NOT written: report.pdf is false" in html


def test_a_failed_pdf_never_costs_the_report(tmp_path, monkeypatch):
    import samanvay.report.render as render_mod

    def boom(*_a, **_k):
        raise RuntimeError("no disk")
    monkeypatch.setattr(render_mod, "_pdf_text_page", boom)
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert "metrics_report.pdf NOT written: PDF export failed (RuntimeError: no disk)" in html
    assert "REGISTERED" in html and "<table" in html


def test_a_pdf_that_failed_half_way_leaves_no_half_pdf(tmp_path, monkeypatch):
    """PdfPages flushes the pages it already wrote when the build raises after page 1.

    That leaves a valid, openable, TRUNCATED metrics_report.pdf beside a report.html
    saying it was never written. A judge would read numbers the report denies exist.
    """
    import samanvay.report.render as render_mod

    def boom(*_a, **_k):
        raise RuntimeError("bad png")
    monkeypatch.setattr(render_mod, "_pdf_image_page", boom)   # raises after the text page
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array, {}),
                encoding="utf-8").read()
    assert "metrics_report.pdf NOT written: PDF export failed (RuntimeError: bad png)" in html
    assert not os.path.exists(os.path.join(str(tmp_path), "metrics_report.pdf"))


def test_a_rerun_with_pdf_off_does_not_leave_the_previous_runs_pdf(tmp_path):
    """Every other artifact is overwritten on a re-run into the same out_dir; the PDF
    was not, so report.pdf false left the earlier run's numbers sitting in the directory
    under a page that says no PDF was written."""
    matches, registration = full_run()
    src, ref = product("A", seed=1), product("B", seed=2)
    render_report(str(tmp_path), src, ref, registration, matches, src.array,
                  {"report": {"pdf": True}})
    pdf = os.path.join(str(tmp_path), "metrics_report.pdf")
    assert os.path.exists(pdf)
    html = open(render_report(str(tmp_path), src, ref, registration, matches, src.array,
                              {"report": {"pdf": False}}), encoding="utf-8").read()
    assert "metrics_report.pdf NOT written" in html
    assert not os.path.exists(pdf), "the previous run's PDF survived a pdf-off re-run"


def test_the_scatter_does_not_call_init_gated_points_fitted(tmp_path):
    """roles==0 means "not held out", NOT "fitted".

    The init gate drops role-0 points before RANSAC, so the cyan cloud is larger than
    n_control. Labelling it "control (fitted) n=346" contradicted the n_control 200 chip
    on the same page; the legend must say what the two numbers are.
    """
    import samanvay.report.render as render_mod
    matches, registration = full_run()
    n_role0 = int((registration.roles == 0).sum())
    registration.metrics["n_control"] = n_role0 - 3        # 3 points the init gate dropped
    labels = []
    real_b64 = render_mod._b64

    def spy(fig):
        for ax in fig.axes:
            leg = ax.get_legend()
            if leg is not None:
                labels.extend(t.get_text() for t in leg.get_texts())
        return real_b64(fig)

    render_mod._b64 = spy
    try:
        src, ref = product("A", seed=1), product("B", seed=2)
        render_report(str(tmp_path), src, ref, registration, matches, src.array, {})
    finally:
        render_mod._b64 = real_b64
    control = [t for t in labels if t.startswith("control")]
    assert control, f"no control legend entry was drawn; got {labels}"
    assert not any("fitted) n=" in t for t in control), control
    assert any(f"n={n_role0}" in t and f"{n_role0 - 3} entered the fit" in t
               for t in control), control


def test_the_pdf_still_writes_when_a_figure_is_missing(tmp_path):
    """A failed registration has no quiver and no scatter; the PDF says so on its pages."""
    src, ref = product("A", seed=1), product("B", seed=2)
    empty = MatchSet(src_xy=np.empty((0, 2)), ref_xy=np.empty((0, 2)),
                     score=np.empty(0, np.float32), method=np.empty(0, np.uint8),
                     cell=np.empty(0, np.int32))
    registration = Registration(model_type="failed", params=np.eye(3), init_params=np.eye(3),
                                inliers=np.zeros(0, bool), residuals=np.empty((0, 2)),
                                sigma=np.empty(0),
                                metrics={"rmse_px": None, "check_rmse_px": None,
                                         "check_status": "skipped_too_few_matches",
                                         "inlier_count": 0, "match_count": 0})
    render_report(str(tmp_path), src, ref, registration, empty, None, {})
    pages, _ = _pdf_pages(os.path.join(str(tmp_path), "metrics_report.pdf"))
    assert pages == 4


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
