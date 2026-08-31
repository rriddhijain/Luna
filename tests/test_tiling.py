"""
tests/test_tiling.py

OWNER: seat 5 (Systems & Performance).

The load-bearing guarantee for tiled processing: reassembling every tile's
CORE region reconstructs the original array EXACTLY, for any shape / tile size
/ halo. If this ever breaks, tiled phase congruency silently corrupts the
image at tile boundaries.
"""

from __future__ import annotations

import numpy as np
import pytest

from samanvay.core.tiling import (
    TiledReader,
    apply_tiled,
    plan_tiles,
    read_tile,
    stitch_tiles,
)

CASES = [
    ((512, 512), 128, 64),
    ((1000, 700), 256, 64),
    ((333, 257), 100, 17),
    ((64, 64), 64, 64),
    ((129, 129), 64, 128),  # halo larger than tile
    ((1, 1), 1, 0),
    ((977, 13), 64, 32),  # skinny
]


@pytest.mark.parametrize("shape,tile,halo", CASES)
def test_core_regions_partition_exactly(shape, tile, halo):
    rng = np.random.default_rng(0)
    arr = rng.integers(0, 255, size=shape).astype(np.float32)

    tiles = plan_tiles(shape, tile_shape=tile, halo=halo)
    recon = np.zeros(shape, np.float32)
    covered = np.zeros(shape, np.int32)
    for t in tiles:
        buf = read_tile(arr, t)
        recon[t.core_slice] = buf[t.core_in_read]
        covered[t.core_slice] += 1

    assert np.array_equal(recon, arr), "core reassembly must be exact"
    assert np.all(covered == 1), "cores must tile with no gaps or overlap"


@pytest.mark.parametrize("shape,tile,halo", CASES)
def test_apply_tiled_identity_matches_whole_image(shape, tile, halo):
    rng = np.random.default_rng(1)
    arr = rng.random(shape).astype(np.float32)
    out = apply_tiled(arr, lambda b: b * 2.0 + 1.0, tile_shape=tile, halo=halo)
    assert np.allclose(out, arr * 2.0 + 1.0)


def test_grid_n_partitions_whole_area():
    tiles = plan_tiles((512, 500), grid_n=4, halo=32)
    area = sum(t.core_h * t.core_w for t in tiles)
    assert area == 512 * 500


def test_read_windows_are_inside_bounds_and_haloed():
    shape = (400, 400)
    tiles = plan_tiles(shape, tile_shape=128, halo=64)
    for t in tiles:
        assert t.read_row >= 0 and t.read_col >= 0
        assert t.read_row + t.read_h <= shape[0]
        assert t.read_col + t.read_w <= shape[1]
        # interior tiles must actually carry a halo on at least one side
        assert t.read_h >= t.core_h and t.read_w >= t.core_w


def test_tiled_reader_array_backend_matches_slice():
    rng = np.random.default_rng(2)
    arr = rng.random((200, 150)).astype(np.float32)
    reader = TiledReader.from_array(arr)
    assert reader.shape == (200, 150)
    win = reader.read_window(10, 20, 30, 40)
    assert np.array_equal(win, arr[10:40, 20:60])


def test_tiled_reader_rasterio_backend(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    arr = np.random.default_rng(3).integers(0, 255, (128, 96)).astype(np.uint8)
    path = str(tmp_path / "r.tif")
    with rasterio.open(
        path, "w", driver="GTiff", height=128, width=96, count=1, dtype="uint8",
        transform=from_origin(0, 0, 1, 1), crs="EPSG:32601",
    ) as dst:
        dst.write(arr, 1)

    with TiledReader.open(path) as reader:
        assert reader.shape == (128, 96)
        win = reader.read_window(5, 7, 20, 30)
        assert np.array_equal(win, arr[5:25, 7:37])
        # exactness through the reader too
        recon = np.zeros((128, 96), np.uint8)
        for t in plan_tiles(reader.shape, tile_shape=40, halo=16):
            buf = reader.read_tile(t)
            recon[t.core_slice] = buf[t.core_in_read]
        assert np.array_equal(recon, arr)


def test_stitch_rejects_wrong_core_shape():
    tiles = plan_tiles((64, 64), tile_shape=32, halo=8)
    bad = [np.zeros((1, 1), np.float32) for _ in tiles]
    with pytest.raises(ValueError):
        stitch_tiles(tiles, bad, (64, 64))


def test_apply_tiled_preserves_trailing_dims():
    arr = np.random.default_rng(4).random((100, 100)).astype(np.float32)

    def to_stack(b):
        return np.stack([b, b * 2, b * 3], axis=-1)

    out = apply_tiled(arr, to_stack, tile_shape=32, halo=8)
    assert out.shape == (100, 100, 3)
    assert np.allclose(out[..., 1], arr * 2)
