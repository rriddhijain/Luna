"""Seat 5 tests: exact windowed reads, an exact tiling, and a cache that cannot lie."""

import os
import subprocess
import sys

import numpy as np
import pytest
import rasterio

from samanvay.core.cache import cache_key, cache_load, cache_store
from samanvay.core.tiling import ArrayReader, TiledReader, iter_tiles, open_reader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHAPE = (300, 220)  # deliberately not a multiple of the tile size


@pytest.fixture
def raster(tmp_path):
    """A small tiled GeoTIFF plus the array it holds."""
    rng = np.random.default_rng(7)
    arr = rng.integers(0, 4000, size=SHAPE, dtype=np.uint16)
    path = str(tmp_path / "src.tif")
    with rasterio.open(path, "w", driver="GTiff", height=SHAPE[0], width=SHAPE[1],
                       count=1, dtype="uint16", tiled=True,
                       blockxsize=64, blockysize=64) as dst:
        dst.write(arr, 1)
    return path, arr


def test_tiled_read_equals_whole_read_bit_for_bit(raster):
    path, arr = raster
    with TiledReader(path) as reader:
        assert reader.shape == SHAPE
        assert reader.dtype == np.dtype("uint16")
        whole = reader.read_all()
        recon = np.zeros(SHAPE, dtype=np.float32)
        for t in iter_tiles(SHAPE, tile=64, halo=16):
            recon[t["core_y0"]:t["core_y1"], t["core_x0"]:t["core_x1"]] = reader.read_window(
                t["core_y0"], t["core_y1"], t["core_x0"], t["core_x1"])
    assert whole.dtype == np.float32
    assert np.array_equal(whole, arr.astype(np.float32))
    assert np.array_equal(recon, whole)  # bit for bit, not allclose


def test_halo_reads_agree_with_the_whole_image(raster):
    path, arr = raster
    with TiledReader(path) as reader:
        for t in iter_tiles(SHAPE, tile=128, halo=32):
            got = reader.read_window(t["halo_y0"], t["halo_y1"], t["halo_x0"], t["halo_x1"])
            want = arr[t["halo_y0"]:t["halo_y1"], t["halo_x0"]:t["halo_x1"]].astype(np.float32)
            assert np.array_equal(got, want)


@pytest.mark.parametrize("shape,tile,halo", [
    (SHAPE, 64, 16), (SHAPE, 512, 64), ((1, 1), 4, 4), ((17, 5), 4, 0), ((300, 220), 100, 7),
])
def test_iter_tiles_cores_partition_and_halos_clamp(shape, tile, halo):
    h, w = shape
    cover = np.zeros(shape, dtype=np.int32)
    tiles = list(iter_tiles(shape, tile=tile, halo=halo))
    assert tiles
    for t in tiles:
        assert t["core_y1"] > t["core_y0"] and t["core_x1"] > t["core_x0"]
        cover[t["core_y0"]:t["core_y1"], t["core_x0"]:t["core_x1"]] += 1
        # halo clamps at the edge and never shrinks below its core
        assert 0 <= t["halo_y0"] <= t["core_y0"] and t["core_y1"] <= t["halo_y1"] <= h
        assert 0 <= t["halo_x0"] <= t["core_x0"] and t["core_x1"] <= t["halo_x1"] <= w
        assert t["halo_y0"] == max(0, t["core_y0"] - halo)
        assert t["halo_y1"] == min(h, t["core_y1"] + halo)
        assert t["halo_x0"] == max(0, t["core_x0"] - halo)
        assert t["halo_x1"] == min(w, t["core_x1"] + halo)
    assert np.all(cover == 1)  # no gaps, no overlap


def test_iter_tiles_degenerate_shapes():
    assert list(iter_tiles((0, 100))) == []
    assert list(iter_tiles((100, 0))) == []
    with pytest.raises(ValueError):
        list(iter_tiles(SHAPE, tile=0))


def test_read_window_clamps_and_returns_empty_outside(raster):
    path, arr = raster
    with TiledReader(path) as reader:
        assert np.array_equal(reader.read_window(-5, 5, -5, 5), arr[0:5, 0:5].astype(np.float32))
        assert reader.read_window(SHAPE[0] + 10, SHAPE[0] + 20, 0, 10).size == 0
        assert reader.read_window(10, 10, 0, 10).size == 0  # zero-height window


def test_read_all_refuses_above_threshold(raster):
    path, _ = raster
    with TiledReader(path) as reader:
        with pytest.raises(MemoryError):
            reader.read_all(max_px=100)
        assert reader.read_all(max_px=None).shape == SHAPE


def test_open_reader_takes_a_path_or_an_ndarray(raster):
    path, arr = raster
    from_path = open_reader(path)
    from_array = open_reader(arr)
    assert isinstance(from_path, TiledReader) and isinstance(from_array, ArrayReader)
    assert from_path.shape == from_array.shape == SHAPE
    assert np.array_equal(from_path.read_window(10, 40, 5, 25),
                          from_array.read_window(10, 40, 5, 25))
    assert np.array_equal(from_array.read_all(), arr.astype(np.float32))
    assert open_reader(from_array) is from_array  # idempotent
    from_path.close()
    from_path.close()  # double close is safe
    with pytest.raises(ValueError):
        open_reader(np.zeros((4, 4, 3)))


def test_cache_key_ignores_dict_ordering_and_tracks_content():
    a = cache_key("OHRC_001", {"a": 1, "b": {"x": 1, "y": [2, 3]}, "nscale": 4})
    b = cache_key("OHRC_001", {"nscale": 4, "b": {"y": [2, 3], "x": 1}, "a": 1})
    assert a == b
    assert a != cache_key("OHRC_001", {"a": 1, "b": {"x": 1, "y": [2, 3]}, "nscale": 5})
    assert a != cache_key("OHRC_002", {"a": 1, "b": {"x": 1, "y": [2, 3]}, "nscale": 4})
    assert cache_key("p", None) == cache_key("p", {})
    # numpy scalars key as their python value; float32(0.55) is a different number
    # from 0.55 and must key differently, or a cache hit would return the wrong PC.
    assert cache_key("p", {"s": np.float64(0.55)}) == cache_key("p", {"s": 0.55})
    assert cache_key("p", {"n": np.int64(4)}) == cache_key("p", {"n": 4})
    assert cache_key("p", {"s": np.float32(0.55)}) != cache_key("p", {"s": 0.55})
    with pytest.raises(TypeError):
        cache_key("p", {"bad": object()})  # no stable text form -> loud, not a per-run key


def test_cache_key_is_stable_across_processes():
    code = ("from samanvay.core.cache import cache_key;"
            "print(cache_key('OHRC_001', {'nscale': 4, 'mult': 2.1, 'k': 2.0}))")
    outs = []
    for _ in range(2):
        proc = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                              capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        outs.append(proc.stdout.strip())
    assert outs[0] == outs[1]
    assert outs[0] == cache_key("OHRC_001", {"nscale": 4, "mult": 2.1, "k": 2.0})


def test_cache_roundtrip_and_atomic_write(tmp_path):
    cache_dir = str(tmp_path / "cache")
    key = cache_key("OHRC_001", {"nscale": 4})
    assert cache_load(key, cache_dir) is None  # missing dir is a miss
    arrays = {"pc": np.random.default_rng(0).random((16, 16)).astype(np.float32),
              "mim": np.zeros((16, 16), dtype=np.uint8)}
    cache_store(key, arrays, cache_dir)
    got = cache_load(key, cache_dir)
    assert set(got) == {"pc", "mim"}
    assert np.array_equal(got["pc"], arrays["pc"]) and got["pc"].dtype == np.float32
    assert os.listdir(cache_dir) == [f"{key}.npz"]  # no temp file left behind


def test_corrupt_or_partial_cache_entry_is_a_miss(tmp_path):
    cache_dir = str(tmp_path / "cache")
    key = cache_key("p", {})
    cache_store(key, {"pc": np.ones((8, 8), dtype=np.float32)}, cache_dir)
    path = os.path.join(cache_dir, f"{key}.npz")

    good = open(path, "rb").read()
    with open(path, "wb") as f:  # truncated: header fine, member payload gone
        f.write(good[: len(good) // 2])
    assert cache_load(key, cache_dir) is None

    with open(path, "wb") as f:
        f.write(b"this is not an npz")
    assert cache_load(key, cache_dir) is None
