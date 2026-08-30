"""Seat 5 · core/tiling — streaming I/O: a gigapixel OHRC strip never lands in RAM.

Frozen conventions: coordinates are (x, y) = (column, row); windows here are
half-open pixel bounds [y0, y1) x [x0, x1) in row/column order, because that is
what NumPy slicing and rasterio windows both want.
"""

import numpy as np
import rasterio
from rasterio.windows import Window

# 64 Mpx == 256 MB as float32. An OHRC strip is orders of magnitude past this.
READ_ALL_MAX_PX = 64_000_000


def _clamp(y0, y1, x0, x1, shape):
    """Clamp a half-open window to the image; never returns an inverted box."""
    h, w = int(shape[0]), int(shape[1])
    y0 = int(max(0, min(y0, h)))
    y1 = int(max(y0, min(y1, h)))
    x0 = int(max(0, min(x0, w)))
    x1 = int(max(x0, min(x1, w)))
    return y0, y1, x0, x1


def _refuse_if_huge(shape, max_px):
    """Raise rather than silently allocating a gigapixel array."""
    if max_px is not None and int(shape[0]) * int(shape[1]) > max_px:
        raise MemoryError(
            f"read_all() refused: {shape[0]}x{shape[1]} px exceeds max_px={max_px}. "
            "Use read_window()/iter_tiles(), or pass max_px=None to force."
        )


class TiledReader:
    """Windowed float32 reader over one band of a rasterio dataset."""

    def __init__(self, path, band=1):
        self._ds = rasterio.open(path)
        self.path = str(path)
        self.band = int(band)
        self.shape = (self._ds.height, self._ds.width)
        self.dtype = np.dtype(self._ds.dtypes[self.band - 1])  # on-disk dtype; reads are float32

    def read_window(self, y0, y1, x0, x1):
        """Read [y0,y1) x [x0,x1) as float32, clamped to the image (empty box -> empty array)."""
        y0, y1, x0, x1 = _clamp(y0, y1, x0, x1, self.shape)
        if y1 <= y0 or x1 <= x0:
            return np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
        win = Window(x0, y0, x1 - x0, y1 - y0)
        return self._ds.read(self.band, window=win).astype(np.float32)

    def read_all(self, max_px=READ_ALL_MAX_PX):
        """Read the whole band as float32; refuses above max_px (pass None to force)."""
        _refuse_if_huge(self.shape, max_px)
        return self._ds.read(self.band).astype(np.float32)

    def close(self):
        """Close the underlying dataset; safe to call twice."""
        if not self._ds.closed:
            self._ds.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    def __repr__(self):
        return f"TiledReader({self.path!r}, shape={self.shape}, dtype={self.dtype})"


class ArrayReader:
    """Same interface as TiledReader, backed by an in-memory 2-D ndarray."""

    def __init__(self, array, path="<ndarray>"):
        arr = np.asarray(array)
        if arr.ndim != 2:
            raise ValueError(f"ArrayReader needs a 2-D array, got shape {arr.shape}")
        self._a = arr
        self.path = str(path)
        self.band = 1
        self.shape = arr.shape
        self.dtype = arr.dtype

    def read_window(self, y0, y1, x0, x1):
        """Read [y0,y1) x [x0,x1) as float32, clamped to the image (empty box -> empty array)."""
        y0, y1, x0, x1 = _clamp(y0, y1, x0, x1, self.shape)
        return self._a[y0:y1, x0:x1].astype(np.float32)

    def read_all(self, max_px=None):
        """Return the whole array as float32 (already resident, so no size refusal by default)."""
        _refuse_if_huge(self.shape, max_px)
        return self._a.astype(np.float32, copy=False)

    def close(self):
        """No-op; the array is not owned by this reader."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __repr__(self):
        return f"ArrayReader(shape={self.shape}, dtype={self.dtype})"


def open_reader(path):
    """Open a raster path, wrap an ndarray, or pass a reader through — one interface either way."""
    if isinstance(path, (TiledReader, ArrayReader)):
        return path
    if isinstance(path, np.ndarray):
        return ArrayReader(path)
    return TiledReader(path)


def iter_tiles(shape, tile=512, halo=64):
    """Yield {core_*, halo_*} boxes tiling an (H, W) image.

    Core boxes partition the image exactly: no gaps, no overlap, so every output
    pixel is produced by exactly one tile. Each core is padded by `halo` pixels,
    clamped at the image edge, because tiled phase congruency computed without a
    halo produces visible seam artefacts at the tile boundaries — the log-Gabor
    filters are wide and need real context outside the core, not zero padding.
    Compute on the halo box, keep only the core.
    """
    h, w = int(shape[0]), int(shape[1])
    tile = int(tile)
    if tile <= 0:
        raise ValueError("tile must be positive")
    halo = max(0, int(halo))
    for y0 in range(0, h, tile):
        y1 = min(y0 + tile, h)
        for x0 in range(0, w, tile):
            x1 = min(x0 + tile, w)
            yield {
                "core_y0": y0, "core_y1": y1, "core_x0": x0, "core_x1": x1,
                "halo_y0": max(0, y0 - halo), "halo_y1": min(h, y1 + halo),
                "halo_x0": max(0, x0 - halo), "halo_x1": min(w, x1 + halo),
            }
