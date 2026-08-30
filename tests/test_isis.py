"""Seats 3 + 6 — C11/C12: the ISIS3 control network we hand to the professional stack.

The 1-based conversion and the round trip are the two things that are invisible when they
are wrong, so they are asserted here rather than eyeballed in qnet.
"""

import csv

import numpy as np
import pytest

from samanvay.io.isis import (ISIS_ORIGIN_OFFSET, TIEPOINT_COLUMNS, read_control_network,
                              write_control_network, write_tiepoint_csv)
from samanvay.types import MatchSet, Product, Registration


def _product(product_id="synth_source", instrument="SYNTH-OHRC", path="/tmp/s.tif"):
    return Product(path=path, array=np.zeros((4, 4), dtype=np.uint8),
                   meta={"product_id": product_id, "instrument": instrument,
                         "shape": (4, 4), "dtype": "uint8"})


def _matches(src, ref, cell=None):
    n = len(src)
    return MatchSet(src_xy=np.asarray(src, dtype=float),
                    ref_xy=np.asarray(ref, dtype=float),
                    score=np.linspace(0.1, 0.9, n).astype(np.float32),
                    method=np.zeros(n, dtype=np.uint8),
                    cell=(np.arange(n, dtype=np.int32) if cell is None
                          else np.asarray(cell, dtype=np.int32)))


def _registration(n, inliers=None, sigma=None, init=None):
    return Registration(
        model_type="affine",
        params=np.eye(3),
        init_params=np.eye(3) if init is None else init,
        inliers=np.ones(n, dtype=bool) if inliers is None else np.asarray(inliers, dtype=bool),
        residuals=np.tile([[0.3, 0.4]], (n, 1)),
        sigma=np.full(n, 0.2) if sigma is None else np.asarray(sigma, dtype=float),
        metrics={"rmse_px": 0.5, "inlier_count": n})


def _write(tmp_path, matches, registration, name="net.pvl", reference=None):
    path = str(tmp_path / name)
    reference = _product("synth_ref", "LROC-NAC", "/tmp/r.tif") if reference is None else reference
    write_control_network(path, matches, registration, _product(), reference)
    return path


# ---------------------------------------------------------------- the 1-based conversion

def test_zero_based_origin_serialises_as_sample_one(tmp_path):
    """(0, 0) in SAMANVAY is the centre of the first pixel: ISIS calls it (1.0, 1.0)."""
    matches = _matches([[0.0, 0.0]], [[0.0, 0.0]])
    net = read_control_network(_write(tmp_path, matches, _registration(1)))

    assert ISIS_ORIGIN_OFFSET == 1.0
    for measure in net["points"][0]["measures"]:
        assert measure["Sample"] == 1.0
        assert measure["Line"] == 1.0


def test_sample_is_x_plus_one_and_line_is_y_plus_one(tmp_path):
    """Not x+1 for both: sample follows the column, line follows the row."""
    matches = _matches([[10.0, 20.0]], [[30.5, 40.25]])
    net = read_control_network(_write(tmp_path, matches, _registration(1)))
    src, ref = net["points"][0]["measures"]

    assert (src["Sample"], src["Line"]) == (11.0, 21.0)
    assert (ref["Sample"], ref["Line"]) == (31.5, 41.25)


# ---------------------------------------------------------------- round trip

def test_round_trip_preserves_every_coordinate_bit_exactly(tmp_path):
    rng = np.random.default_rng(7)
    src = rng.uniform(0, 2048, size=(24, 2))
    ref = src * 1.9997 + rng.normal(0, 0.3, size=(24, 2))
    matches = _matches(src, ref)
    net = read_control_network(_write(tmp_path, matches, _registration(24)))

    assert len(net["points"]) == 24
    for i, point in enumerate(net["points"]):
        got_src, got_ref = point["measures"]
        assert got_src["Sample"] == src[i, 0] + 1.0     # exact, not approx
        assert got_src["Line"] == src[i, 1] + 1.0
        assert got_ref["Sample"] == ref[i, 0] + 1.0
        assert got_ref["Line"] == ref[i, 1] + 1.0


def test_round_trip_preserves_header_and_point_ids(tmp_path):
    path = str(tmp_path / "net.pvl")
    write_control_network(path, _matches([[1.0, 2.0]], [[3.0, 4.0]]), _registration(1),
                          _product(), _product("synth_ref", "LROC-NAC"),
                          network_id="sih26166_pair_A", target="moon")
    net = read_control_network(path)

    assert net["NetworkId"] == "sih26166_pair_A"
    assert net["TargetName"] == "MOON"
    assert net["Version"] == 5
    assert net["points"][0]["PointId"] == "sih26166_pair_A_000000"   # a string, not 0.0
    assert net["points"][0]["PointType"] == "Free"


# ---------------------------------------------------------------- outliers stay visible

def test_outliers_are_ignored_not_dropped(tmp_path):
    inliers = [True, False, True, False]
    matches = _matches([[0, 0], [1, 1], [2, 2], [3, 3]], [[0, 0], [1, 1], [2, 2], [3, 3]])
    path = _write(tmp_path, matches, _registration(4, inliers=inliers))
    net = read_control_network(path)

    assert len(net["points"]) == 4                       # nothing vanished
    assert [p["Ignore"] for p in net["points"]] == [False, True, False, True]
    ignore_lines = [ln.split("=")[1].strip() for ln in open(path)
                    if ln.strip().startswith("Ignore")]
    assert ignore_lines == ["False", "True", "False", "True"]


def test_unknown_inlier_state_writes_no_ignore_key(tmp_path):
    """A registration with no inlier array must not claim every point is good."""
    reg = Registration(model_type="failed", params=np.eye(3), init_params=np.eye(3),
                       inliers=np.empty(0, dtype=bool), residuals=np.empty((0, 2)),
                       sigma=np.empty(0), metrics={"status": "failed"})
    net = read_control_network(_write(tmp_path, _matches([[1.0, 1.0]], [[2.0, 2.0]]), reg))

    assert "Ignore" not in net["points"][0]


# ---------------------------------------------------------------- structure

def test_every_object_and_group_is_closed(tmp_path):
    text = open(_write(tmp_path, _matches([[0, 0], [5, 5]], [[1, 1], [6, 6]]),
                       _registration(2))).read()
    body = [ln.strip() for ln in text.splitlines() if not ln.strip().startswith("#")]

    assert sum(ln.startswith("Object =") for ln in body) == \
           sum(ln == "End_Object" for ln in body) == 3      # network + 2 points
    assert sum(ln.startswith("Group =") for ln in body) == \
           sum(ln == "End_Group" for ln in body) == 4       # 2 measures per point
    assert body[-1] == "End"


def test_reader_is_strict_about_unclosed_blocks(tmp_path):
    path = _write(tmp_path, _matches([[0, 0]], [[1, 1]]), _registration(1))
    lines = open(path).read().splitlines()

    truncated = tmp_path / "truncated.pvl"
    truncated.write_text("\n".join(ln for ln in lines if ln.strip() != "End_Group"))
    with pytest.raises(ValueError):
        read_control_network(str(truncated))

    crossed = tmp_path / "crossed.pvl"
    crossed.write_text("\n".join(ln.replace("End_Group", "End_Object") for ln in lines))
    with pytest.raises(ValueError):
        read_control_network(str(crossed))


def test_measure_type_reports_whether_we_refined_it(tmp_path):
    """RegisteredSubPixel is a claim about sub-pixel work; NaN sigma must not make it."""
    matches = _matches([[0, 0], [1, 1]], [[2, 2], [3, 3]])
    net = read_control_network(_write(tmp_path, matches,
                                      _registration(2, sigma=[0.2, np.nan])))

    assert [p["measures"][1]["MeasureType"] for p in net["points"]] == \
           ["RegisteredSubPixel", "RegisteredPixel"]
    assert all(p["measures"][0]["Reference"] is True for p in net["points"])
    assert "Reference" not in net["points"][0]["measures"][1]


def test_apriori_comes_from_the_init_and_is_omitted_when_unknown(tmp_path):
    init = np.array([[2.0, 0.0, 5.0], [0.0, 2.0, 7.0], [0.0, 0.0, 1.0]])
    matches = _matches([[3.0, 4.0]], [[11.5, 15.5]])
    net = read_control_network(_write(tmp_path, matches, _registration(1, init=init)))
    ref_measure = net["points"][0]["measures"][1]

    assert ref_measure["AprioriSample"] == 2.0 * 3.0 + 5.0 + 1.0     # 12.0
    assert ref_measure["AprioriLine"] == 2.0 * 4.0 + 7.0 + 1.0       # 16.0
    assert "AprioriSample" not in net["points"][0]["measures"][0]    # source has no prior

    reg = _registration(1, init=np.full((3, 3), np.nan))
    blind = read_control_network(_write(tmp_path, matches, reg, name="blind.pvl"))
    assert "AprioriSample" not in blind["points"][0]["measures"][1]


def test_identical_products_still_get_distinguishable_serials(tmp_path):
    """Two measures of one point may not share a serial, and the file explains the suffix."""
    path = _write(tmp_path, _matches([[0, 0]], [[1, 1]]), _registration(1),
                  name="same.pvl", reference=_product())
    net = read_control_network(path)
    src, ref = net["points"][0]["measures"]

    assert src["SerialNumber"] != ref["SerialNumber"]
    assert src["SerialNumber"].endswith("/SOURCE")
    assert "identical instrument/product_id metadata" in open(path).read()


def test_serial_number_says_unknown_rather_than_inventing_one(tmp_path):
    path = str(tmp_path / "net.pvl")
    bare = Product(path="/tmp/mystery.tif", array=np.zeros((2, 2)), meta={})
    write_control_network(path, _matches([[0, 0]], [[1, 1]]), _registration(1), bare, bare)
    net = read_control_network(path)

    assert net["points"][0]["measures"][0]["SerialNumber"].startswith(
        "SAMANVAY/UNKNOWN_INSTRUMENT/mystery")      # from the filename, flagged unknown
    assert "NOT VALIDATED BY ISIS3" in open(path).read()


# ---------------------------------------------------------------- degenerate input

def test_empty_matchset_writes_a_valid_empty_network(tmp_path):
    empty = MatchSet(src_xy=np.empty((0, 2)), ref_xy=np.empty((0, 2)),
                     score=np.empty(0, dtype=np.float32),
                     method=np.empty(0, dtype=np.uint8), cell=np.empty(0, dtype=np.int32))
    reg = Registration(model_type="failed", params=np.eye(3), init_params=np.eye(3),
                       inliers=np.empty(0, dtype=bool), residuals=np.empty((0, 2)),
                       sigma=np.empty(0), metrics={"status": "failed", "rmse_px": None})
    net = read_control_network(_write(tmp_path, empty, reg))

    assert net["points"] == []
    assert net["TargetName"] == "MOON"


def test_non_finite_points_are_omitted_and_the_file_says_so(tmp_path):
    matches = _matches([[0.0, 0.0], [np.nan, 2.0]], [[1.0, 1.0], [3.0, 3.0]])
    path = _write(tmp_path, matches, _registration(2))
    net = read_control_network(path)

    assert len(net["points"]) == 1
    assert "1 of 2 tie-points omitted" in open(path).read()


def test_none_matchset_does_not_crash(tmp_path):
    path = str(tmp_path / "none.pvl")
    write_control_network(path, None, None, _product(), _product())
    assert read_control_network(path)["points"] == []


# ---------------------------------------------------------------- the CSV

def test_csv_header_matches_the_documented_schema(tmp_path):
    path = str(tmp_path / "tiepoints.csv")
    write_tiepoint_csv(path, _matches([[0.0, 0.0]], [[1.0, 2.0]]), _registration(1),
                       _product(), _product("synth_ref"))
    rows = list(csv.reader(open(path)))

    assert rows[0] == TIEPOINT_COLUMNS
    assert rows[0] == ["point_id", "source_sample", "source_line",
                       "reference_sample", "reference_line",
                       "score", "residual_px", "sigma_px", "is_inlier", "grid_cell"]
    assert rows[1][1:5] == ["1.0", "1.0", "2.0", "3.0"]        # 1-based, like the network
    assert float(rows[1][6]) == pytest.approx(0.5)             # |(0.3, 0.4)| source px
    assert rows[1][8] == "1"


def test_csv_leaves_unknown_fields_blank_never_zero(tmp_path):
    path = str(tmp_path / "tiepoints.csv")
    reg = _registration(2, inliers=[True, False], sigma=[np.nan, 0.25])
    write_tiepoint_csv(path, _matches([[0, 0], [1, 1]], [[2, 2], [3, 3]]), reg,
                       _product(), _product("synth_ref"))
    rows = list(csv.reader(open(path)))

    assert rows[1][7] == ""            # sigma unknown: blank, not 0.0
    assert rows[2][7] == "0.25"
    assert rows[2][8] == "0"


def test_csv_point_ids_match_the_network(tmp_path):
    matches = _matches([[0, 0], [4, 4]], [[1, 1], [5, 5]])
    reg = _registration(2)
    csv_path = str(tmp_path / "tiepoints.csv")
    write_tiepoint_csv(csv_path, matches, reg, _product(), _product("synth_ref"))
    net = read_control_network(_write(tmp_path, matches, reg))

    ids = [row["point_id"] for row in csv.DictReader(open(csv_path))]
    assert ids == [p["PointId"] for p in net["points"]]


def test_empty_matchset_writes_a_header_only_csv(tmp_path):
    path = str(tmp_path / "tiepoints.csv")
    empty = MatchSet(src_xy=np.empty((0, 2)), ref_xy=np.empty((0, 2)),
                     score=np.empty(0), method=np.empty(0, dtype=np.uint8),
                     cell=np.empty(0, dtype=np.int32))
    write_tiepoint_csv(path, empty, None, _product(), _product())
    assert list(csv.reader(open(path))) == [TIEPOINT_COLUMNS]
