"""
samanvay/core/tiling.py

OWNER: seat 5 (Systems & Performance).

Windowed, halo-aware tiling over large rasters. Two problems this solves:

  1. Memory. OHRC strips are gigapixel. Nothing downstream may `read()` the
     whole array (see the `Product` contract in geometric_types.py: the array
     is "a memmap or tiled reader, never a full load"). `TiledReader` reads
     only the window a stage currently needs.

  2. Seam artefacts. Phase congruency (seat 2) is an FFT-based, non-local
     operator. Computing it tile-by-tile with no overlap produces visible
     discontinuities at tile boundaries. Every tile is therefore READ with a
     halo (64-128 px of context on each side), the operator runs on the
     haloed buffer, and only the tile's CORE region is written back -- the
     halo is context that is thrown away, never double-counted.

Convention (matches types.py): image arrays are indexed [row, col] = [y, x].
Tile geometry is expressed in row/col pixel offsets into the full image.

The load-bearing guarantee, verified in tests/test_tiling.py:

    reassembling every tile's CORE region reconstructs the original array
    EXACTLY -- `plan_tiles` partitions the image with no gaps and no overlap
    in the core regions, for any shape / tile size / halo.

So a stage that is pixel-local (a passthrough, a per-pixel normalisation, a
copy) is bit-identical whether it runs whole-image or tiled; only genuinely
non-local operators (PC) differ, and only inside the halo you deliberately
discarded.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np

__all__ = [
    "Tile",
    "TiledReader",
    "apply_tiled",
    "plan_tiles",
    "read_tile",
    "stitch_tiles",
]


@dataclass(frozen=True)
class Tile:
    """One tile of a tiling plan.

    A tile owns a CORE rectangle (its slice of the output, disjoint from every
    other tile) and is READ over a larger rectangle (core expanded by the halo
    and clamped to the image) so non-local operators have context.

    All offsets/sizes are in full-image pixels, [row, col] order.
    """

    # Core region: this tile's exclusive slice of the output.
    core_row: int
    core_col: int
    core_h: int
    core_w: int
    # Read region: core grown by the halo, clamped to the image bounds.
    read_row: int
    read_col: int
    read_h: int
    read_w: int

    @property
    def core_slice(self) -> tuple[slice, slice]:
        """Where the core sits in the FULL image."""
        return (
            slice(self.core_row, self.core_row + self.core_h),
            slice(self.core_col, self.core_col + self.core_w),
        )

    @property
    def read_slice(self) -> tuple[slice, slice]:
        """Where the haloed read window sits in the FULL image."""
        return (
            slice(self.read_row, self.read_row + self.read_h),
            slice(self.read_col, self.read_col + self.read_w),
        )

    @property
    def core_in_read(self) -> tuple[slice, slice]:
        """Where the core sits INSIDE a buffer shaped like the read window.

        Use this to crop the halo off a per-tile result before stitching:
        ``result[tile.core_in_read]`` is exactly the core-sized output.
        """
        r0 = self.core_row - self.read_row
        c0 = self.core_col - self.read_col
        return (slice(r0, r0 + self.core_h), slice(c0, c0 + self.core_w))


def plan_tiles(
    shape: tuple[int, int],
    tile_shape: int | tuple[int, int] = 1024,
    halo: int = 64,
    *,
    grid_n: int | None = None,
) -> list[Tile]:
    """Partition ``shape`` (H, W) into a grid of :class:`Tile` objects.

    Parameters
    ----------
    shape : (H, W) of the full image.
    tile_shape : core tile size in pixels; an int is square. Ignored when
        ``grid_n`` is given.
    halo : context margin (px) added on every side of the core for reads,
        clamped to the image edge. Choose 64-128 for phase congruency.
    grid_n : if set, split the image into ``grid_n x grid_n`` roughly-equal
        cores instead of using ``tile_shape`` (mirrors ``match_tiled``).

    The CORE regions tile the image with no gaps and no overlap; the READ
    regions overlap by up to ``halo`` and are always inside the image.
    """
    h, w = int(shape[0]), int(shape[1])
    if h <= 0 or w <= 0:
        raise ValueError(f"invalid image shape {shape!r}")
    if halo < 0:
        raise ValueError("halo must be >= 0")

    row_bounds = _core_bounds(h, tile_shape, grid_n, axis=0)
    col_bounds = _core_bounds(w, tile_shape, grid_n, axis=1)

    tiles: list[Tile] = []
    for r0, r1 in row_bounds:
        for c0, c1 in col_bounds:
            rr0 = max(0, r0 - halo)
            cc0 = max(0, c0 - halo)
            rr1 = min(h, r1 + halo)
            cc1 = min(w, c1 + halo)
            tiles.append(
                Tile(
                    core_row=r0,
                    core_col=c0,
                    core_h=r1 - r0,
                    core_w=c1 - c0,
                    read_row=rr0,
                    read_col=cc0,
                    read_h=rr1 - rr0,
                    read_w=cc1 - cc0,
                )
            )
    return tiles


def _core_bounds(
    length: int, tile_shape: int | tuple[int, int], grid_n: int | None, axis: int
) -> list[tuple[int, int]]:
    """Contiguous (start, stop) core intervals covering [0, length)."""
    if grid_n is not None:
        if grid_n < 1:
            raise ValueError("grid_n must be >= 1")
        edges = np.linspace(0, length, grid_n + 1).round().astype(int)
        # Guard against zero-width cells when grid_n > length.
        bounds = [(int(edges[i]), int(edges[i + 1])) for i in range(grid_n)]
        return [(a, b) for a, b in bounds if b > a]

    if isinstance(tile_shape, tuple):
        step = tile_shape[axis]
    else:
        step = tile_shape
    step = int(step)
    if step < 1:
        raise ValueError("tile_shape must be >= 1")

    bounds = []
    start = 0
    while start < length:
        stop = min(start + step, length)
        bounds.append((start, stop))
        start = stop
    return bounds


class TiledReader:
    """Lazy, windowed reader over a raster or an in-memory array.

    Presents a uniform ``read_window`` / ``read_tile`` API regardless of
    backing store, so seats 1 and 2 write one code path that works on both a
    1k synthetic fixture (numpy array) and a gigapixel OHRC GeoTIFF (rasterio
    windowed reads, nothing fully materialised).

    Backends
    --------
    * numpy array / memmap : sliced directly (a copy is returned).
    * rasterio dataset path : opened lazily; each window is a GDAL windowed
      read of a single band.
    """

    def __init__(
        self,
        array: np.ndarray | None = None,
        *,
        path: str | None = None,
        band: int = 1,
    ) -> None:
        if (array is None) == (path is None):
            raise ValueError("provide exactly one of array= or path=")
        self._array = array
        self._path = path
        self._band = band
        self._ds = None  # lazily opened rasterio dataset

        if array is not None:
            if array.ndim != 2:
                raise ValueError("TiledReader expects a 2-D (single band) array")
            self._shape = (int(array.shape[0]), int(array.shape[1]))
            self._dtype = array.dtype
        else:
            self._shape, self._dtype = self._probe_raster(path, band)

    # -- construction helpers -------------------------------------------------
    @classmethod
    def open(cls, path: str, band: int = 1) -> TiledReader:
        """Open a raster path for windowed reading (nothing is read yet)."""
        return cls(path=path, band=band)

    @classmethod
    def from_array(cls, array: np.ndarray) -> TiledReader:
        return cls(array=array)

    @staticmethod
    def _probe_raster(path: str, band: int) -> tuple[tuple[int, int], np.dtype]:
        import rasterio

        with rasterio.open(path) as ds:
            return (int(ds.height), int(ds.width)), np.dtype(ds.dtypes[band - 1])

    # -- properties -----------------------------------------------------------
    @property
    def shape(self) -> tuple[int, int]:
        return self._shape

    @property
    def dtype(self) -> np.dtype:
        return self._dtype

    # -- reading --------------------------------------------------------------
    def read_window(self, row: int, col: int, h: int, w: int) -> np.ndarray:
        """Read the [row:row+h, col:col+w] window as a materialised array."""
        H, W = self._shape
        if row < 0 or col < 0 or row + h > H or col + w > W:
            raise IndexError(
                f"window ({row},{col},{h},{w}) out of bounds for shape {self._shape}"
            )
        if self._array is not None:
            return np.ascontiguousarray(self._array[row : row + h, col : col + w])

        import rasterio
        from rasterio.windows import Window

        if self._ds is None:
            self._ds = rasterio.open(self._path)
        window = Window(col_off=col, row_off=row, width=w, height=h)
        return self._ds.read(self._band, window=window)

    def read_tile(self, tile: Tile) -> np.ndarray:
        """Read a tile's haloed region (the buffer a stage operates on)."""
        return self.read_window(tile.read_row, tile.read_col, tile.read_h, tile.read_w)

    def read_full(self) -> np.ndarray:
        """Materialise the whole image. Use only for small rasters / tests."""
        return self.read_window(0, 0, self._shape[0], self._shape[1])

    # -- lifecycle ------------------------------------------------------------
    def close(self) -> None:
        if self._ds is not None:
            self._ds.close()
            self._ds = None

    def __enter__(self) -> TiledReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _as_reader(source: np.ndarray | TiledReader) -> TiledReader:
    if isinstance(source, TiledReader):
        return source
    if isinstance(source, np.ndarray):
        return TiledReader.from_array(source)
    raise TypeError(f"expected ndarray or TiledReader, got {type(source)!r}")


def read_tile(source: np.ndarray | TiledReader, tile: Tile) -> np.ndarray:
    """Read a tile's haloed region from an array or reader."""
    return _as_reader(source).read_tile(tile)


def stitch_tiles(
    tiles: list[Tile],
    cores: list[np.ndarray],
    out_shape: tuple[int, ...],
    dtype: np.dtype | None = None,
) -> np.ndarray:
    """Assemble core-sized tile outputs into a full-image array.

    ``cores[i]`` must be shaped like ``tiles[i].core`` (already halo-cropped).
    Because the cores partition the image, the result has every pixel written
    exactly once.
    """
    if len(tiles) != len(cores):
        raise ValueError("tiles and cores length mismatch")
    if dtype is None:
        dtype = cores[0].dtype if cores else np.float32
    out = np.zeros(out_shape, dtype=dtype)
    for tile, core in zip(tiles, cores, strict=True):
        expected = (tile.core_h, tile.core_w)
        if core.shape[:2] != expected:
            raise ValueError(
                f"core shape {core.shape[:2]} != tile core {expected}; "
                "did you forget tile.core_in_read?"
            )
        out[tile.core_slice] = core
    return out


def apply_tiled(
    source: np.ndarray | TiledReader,
    func: Callable[[np.ndarray], np.ndarray],
    *,
    tile_shape: int | tuple[int, int] = 1024,
    halo: int = 64,
    grid_n: int | None = None,
    out_dtype: np.dtype | None = None,
) -> np.ndarray:
    """Run ``func`` over the image tile-by-tile with halo, then stitch.

    ``func`` receives a haloed 2-D buffer and must return an array of the SAME
    height/width (extra trailing dims are allowed, e.g. an orientation stack).
    The halo is cropped off each result before stitching, so boundary context
    informs the computation without leaking into neighbouring cores.

    For a pixel-local ``func`` the output is identical to ``func(full_image)``;
    for a non-local ``func`` (phase congruency) it is the standard
    overlap-crop tiling that keeps seams out of the written region.
    """
    reader = _as_reader(source)
    tiles = plan_tiles(reader.shape, tile_shape=tile_shape, halo=halo, grid_n=grid_n)
    cores: list[np.ndarray] = []
    trailing: tuple[int, ...] = ()
    for tile in tiles:
        buf = reader.read_tile(tile)
        result = func(buf)
        if result.shape[:2] != buf.shape[:2]:
            raise ValueError(
                f"func changed spatial shape {buf.shape[:2]} -> {result.shape[:2]}; "
                "tiled operators must be shape-preserving"
            )
        trailing = result.shape[2:]
        cores.append(result[tile.core_in_read])
    out_shape = (reader.shape[0], reader.shape[1], *trailing)
    return stitch_tiles(tiles, cores, out_shape, dtype=out_dtype)


def iter_tiles(
    shape: tuple[int, int],
    tile_shape: int | tuple[int, int] = 1024,
    halo: int = 64,
    *,
    grid_n: int | None = None,
) -> Iterator[Tile]:
    """Convenience generator over a tiling plan."""
    yield from plan_tiles(shape, tile_shape=tile_shape, halo=halo, grid_n=grid_n)
