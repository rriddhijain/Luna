"""Seat 3 (I/O) — product loading.

Pillar: honest provenance. A product is the file on disk plus the metadata that really
came with it; nothing here invents geometry, and a missing file is an error, not a mock.

Two things happen between the file and the 2-D array the rest of the pipeline matches on,
and both are recorded in ``Product.meta`` because both change what a pixel means:

    meta["band_reduction"]  how a multi-band cube became one map (io/bands.reduce_bands).
                            ``src.read(1)`` on a 250-band IIRS cube is not a choice of
                            band, it is an accident nothing downstream can see.
    meta["read_decimation"] whether the raster was read at reduced resolution, and by
                            what factor. A decimated read that leaves gsd_m and the
                            geotransform alone makes every metre-valued metric wrong by
                            exactly that factor, silently — so shape, geotransform and
                            gsd_m are all folded here or the decimation does not happen.
"""

import math
import os

import numpy as np
import rasterio

from samanvay.io.bands import reduce_bands
from samanvay.io.metadata import normalise_meta, read_metadata
from samanvay.types import Product

# Above this, attach a tiled reader instead of pulling the whole raster into RAM.
MAX_INMEMORY_BYTES = 1 << 29  # 512 MiB

# The same key set the decimated branch emits (_fold_decimation), so a consumer never
# has to branch on which one it got. read_shape/full_shape are filled from the file.
_NO_DECIMATION = {"applied": False, "factor_x": 1.0, "factor_y": 1.0,
                  "read_shape": None, "full_shape": None, "max_pixels": None}


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


def _read_shape(shape, max_pixels):
    """Read shape for a raster above max_pixels, or None to read it at full resolution.

    One integer factor on both axes so the aspect ratio survives; floor division so the
    result is at or under the budget rather than one row over it.
    """
    height, width = int(shape[0]), int(shape[1])
    if not max_pixels or max_pixels <= 0 or height * width <= int(max_pixels):
        return None
    factor = int(math.ceil(math.sqrt(height * width / float(max_pixels))))
    return max(1, height // factor), max(1, width // factor)


def _fold_decimation(meta, full_shape, read_shape, max_pixels):
    """Rewrite shape, geotransform and gsd_m for a decimated read, and record the factors.

    Every residual in this repo is in pixels of the array that was matched. If the array
    was read at half resolution and gsd_m still says the full-resolution value, a
    sub-pixel RMSE converts to metres 2x too small and nothing in the output reveals it.
    The geotransform is the same problem in the other direction: it maps pixels to ground,
    and the pixels just changed size.
    """
    height, width = int(full_shape[0]), int(full_shape[1])
    new_h, new_w = int(read_shape[0]), int(read_shape[1])
    sx, sy = width / float(new_w), height / float(new_h)

    meta["shape"] = (new_h, new_w)
    where = meta.get("meta_source") or {}
    gt = meta.get("geotransform")
    if gt is not None and len(gt) == 6:
        a, b, c, d, e, f = (float(v) for v in gt)
        meta["geotransform"] = (a * sx, b * sy, c, d * sx, e * sy, f)
        where["geotransform"] = f"{where.get('geotransform', 'unknown')}+read_decimation"
    if meta.get("gsd_m") is not None:
        # Geometric mean of the two axis factors: the axes only differ by the rounding of
        # one integer factor, and a GSD is one number.
        meta["gsd_m"] = float(meta["gsd_m"]) * math.sqrt(sx * sy)
        where["gsd_m"] = f"{where.get('gsd_m', 'unknown')}+read_decimation"
    where["shape"] = f"{where.get('shape', 'unknown')}+read_decimation"
    meta["meta_source"] = where
    meta["read_decimation"] = {
        "applied": True, "factor_x": round(sx, 6), "factor_y": round(sy, 6),
        "read_shape": [new_h, new_w], "full_shape": [height, width],
        "max_pixels": int(max_pixels),
    }


def load_product(path: str, max_bytes: int = None, band_cfg: dict = None,
                 max_pixels: int = None) -> Product:
    """Open a raster as a Product: one 2-D array, or a TiledReader when it is large.

    `band_cfg` is config["band"] and decides how a multi-band cube is reduced to that
    single array. `max_pixels` caps the read: above it the raster is read decimated
    (rasterio out_shape) and shape/geotransform/gsd_m are corrected to match. It is off
    by default — a slow read is recoverable, a silently rescaled GSD is not — so a caller
    opts in with a budget it can defend.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"product not found: {path}")

    meta = normalise_meta(read_metadata(path), path)
    meta["read_decimation"] = dict(_NO_DECIMATION)
    limit = MAX_INMEMORY_BYTES if max_bytes is None else max_bytes

    with rasterio.open(path) as src:
        full_shape = (int(src.height), int(src.width))
        read_shape = _read_shape(full_shape, max_pixels)
        meta["read_decimation"].update(
            read_shape=[full_shape[0], full_shape[1]],
            full_shape=[full_shape[0], full_shape[1]],
            max_pixels=None if max_pixels is None else int(max_pixels))
        if src.count == 1 and read_shape is None and _nbytes(meta, path) > limit:
            try:
                from samanvay.core.tiling import open_reader
                array = open_reader(path)
                meta["band_reduction"] = {
                    "n_bands": 1, "n_bands_used": 1, "n_bands_dropped_snr": 0,
                    "reduce": "single", "explained_var_frac": None,
                    "note": "windowed reader over band 1; nothing to reduce",
                }
                return Product(path=path, array=array, meta=meta)
            except ImportError:
                # ponytail: no tiling module -> fall back to a full read. Upgrade path is
                # simply that samanvay.core.tiling lands; nothing here changes.
                pass
        # A cube is never handed to the tiled reader: that reader is band 1, which is the
        # exact defect reduce_bands exists to fix. reduce_bands screens on a decimated
        # read and streams the result one band at a time, so the cube is never resident.
        array, info = reduce_bands(src, band_cfg, out_shape=read_shape)

    if read_shape is not None:
        _fold_decimation(meta, full_shape, read_shape, max_pixels)
    if meta.get("dtype") and str(array.dtype) != meta["dtype"]:
        # PC1 and the band mean are float32 combinations of the cube; the on-disk integer
        # dtype no longer describes what the pipeline (or the writer) is holding.
        info["source_dtype"] = meta["dtype"]
        meta["dtype"] = str(array.dtype)
        meta["meta_source"]["dtype"] = "band_reduction"
    meta["band_reduction"] = info

    return Product(path=path, array=array, meta=meta)
