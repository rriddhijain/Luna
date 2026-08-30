"""Seat 3 (I/O) — product loading.

Pillar: honest provenance. A product is the file on disk plus the metadata that really
came with it; nothing here invents geometry, and a missing file is an error, not a mock.
"""

import os

import numpy as np
import rasterio

from samanvay.io.metadata import normalise_meta, read_metadata
from samanvay.types import Product

# Above this, attach a tiled reader instead of pulling the whole raster into RAM.
MAX_INMEMORY_BYTES = 1 << 29  # 512 MiB


def _nbytes(meta, path):
    """Best estimate of the in-memory size of band 1, falling back to the file size."""
    shape, dtype = meta.get("shape"), meta.get("dtype")
    if shape and dtype:
        try:
            return int(shape[0]) * int(shape[1]) * np.dtype(dtype).itemsize
        except TypeError:
            pass
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def load_product(path: str, max_bytes: int = None) -> Product:
    """Open a raster as a Product: band 1 as an ndarray, or a TiledReader when it is large."""
    if not os.path.exists(path):
        raise FileNotFoundError(f"product not found: {path}")

    meta = normalise_meta(read_metadata(path), path)
    limit = MAX_INMEMORY_BYTES if max_bytes is None else max_bytes

    array = None
    if _nbytes(meta, path) > limit:
        try:
            from samanvay.core.tiling import open_reader
            array = open_reader(path)
        except ImportError:
            # ponytail: no tiling module yet -> fall back to a full read. Upgrade path is
            # simply that samanvay.core.tiling lands; nothing here changes.
            array = None
    if array is None:
        with rasterio.open(path) as src:
            array = src.read(1)  # native dtype: writers preserve it, photometry casts as needed

    return Product(path=path, array=array, meta=meta)
