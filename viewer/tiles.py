"""Seat 4 · viewer/tiles — Deep Zoom (DZI) pyramid generation for OpenSeadragon.

Pillar: a judge who can zoom into a tie-point trusts the system. Written on rasterio
+ numpy + cv2 only: no pyvips, no gdal2tiles, no PIL. Every tile is produced from a
decimated windowed read, so the working set is one tile — a gigapixel OHRC strip is
never resident.
"""

import json
import math
import os
import sys

import cv2
import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window

# The Deep Zoom descriptor namespace. It is an identifier, not a fetch: nothing here
# and nothing OpenSeadragon does ever goes to the network for it.
DZI_NS = "http://schemas.microsoft.com/deepzoom/2008"

TILE_SIZE = 254
OVERLAP = 1
QUALITY = 85
STATS_MAX_DIM = 1024  # longest edge of the overview used to pick the display stretch


def _ceil_div(a, b):
    """Integer ceiling division."""
    return -(-int(a) // int(b))


def dzi_max_level(width, height) -> int:
    """Top pyramid level index: ceil(log2(longest edge)); level 0 is a single pixel."""
    return int(math.ceil(math.log2(max(1, int(width), int(height)))))


def level_size(width, height, level, max_level) -> tuple:
    """(w, h) of one pyramid level, by the Deep Zoom halving rule."""
    factor = 2 ** (max_level - int(level))
    return max(1, _ceil_div(width, factor)), max(1, _ceil_div(height, factor))


def tile_grid(level_w, level_h, tile_size=TILE_SIZE) -> tuple:
    """(cols, rows) of tiles covering a level."""
    return _ceil_div(level_w, tile_size), _ceil_div(level_h, tile_size)


def _stretch_range(ds, band):
    """(lo, hi) display range from a small overview, so every tile is scaled identically."""
    factor = max(1, _ceil_div(max(ds.height, ds.width), STATS_MAX_DIM))
    arr = ds.read(band,
                  out_shape=(max(1, ds.height // factor), max(1, ds.width // factor)),
                  resampling=Resampling.average).astype(np.float32)
    nodata = ds.nodatavals[band - 1]
    if nodata is not None:
        arr = np.where(arr == nodata, np.nan, arr)
    finite = arr[np.isfinite(arr)]
    if finite.size == 0:
        return 0.0, 0.0  # all-nodata raster: flat tiles, not invented contrast
    lo, hi = (float(v) for v in np.percentile(finite, [2.0, 98.0]))
    if hi <= lo:
        lo, hi = float(finite.min()), float(finite.max())
    return lo, hi


def _to_u8(arr, lo, hi, nodata):
    """Scale a float window into 8-bit for JPEG, folding nodata to the low end."""
    a = np.asarray(arr, dtype=np.float32)
    if nodata is not None:
        a = np.where(a == nodata, lo, a)
    a = np.where(np.isfinite(a), a, lo)
    if hi <= lo:
        return np.zeros(a.shape, dtype=np.uint8)
    return np.clip((a - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8)


def build_dzi(src_path, out_dzi=None, tile_size=TILE_SIZE, overlap=OVERLAP,
              quality=QUALITY, band=1) -> dict:
    """Write a .dzi descriptor plus its _files/<level>/<col>_<row>.jpg pyramid; return a summary."""
    tile_size = int(tile_size)
    overlap = max(0, int(overlap))
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    if out_dzi is None:
        out_dzi = os.path.splitext(str(src_path))[0] + ".dzi"
    stem = out_dzi[:-4] if out_dzi.lower().endswith(".dzi") else out_dzi
    files_dir = stem + "_files"
    parent = os.path.dirname(os.path.abspath(out_dzi))
    if parent:
        os.makedirs(parent, exist_ok=True)

    written = 0
    with rasterio.open(src_path) as ds:
        width, height = int(ds.width), int(ds.height)
        nodata = ds.nodatavals[band - 1]
        lo, hi = _stretch_range(ds, band)
        max_level = dzi_max_level(width, height)

        for level in range(max_level + 1):
            lw, lh = level_size(width, height, level, max_level)
            factor = 2 ** (max_level - level)
            cols, rows = tile_grid(lw, lh, tile_size)
            level_dir = os.path.join(files_dir, str(level))
            os.makedirs(level_dir, exist_ok=True)
            for row in range(rows):
                for col in range(cols):
                    x0 = col * tile_size - (overlap if col > 0 else 0)
                    x1 = min(col * tile_size + tile_size + (overlap if col < cols - 1 else 0), lw)
                    y0 = row * tile_size - (overlap if row > 0 else 0)
                    y1 = min(row * tile_size + tile_size + (overlap if row < rows - 1 else 0), lh)
                    tw, th = x1 - x0, y1 - y0

                    sx0, sy0 = x0 * factor, y0 * factor
                    sx1, sy1 = min(width, x1 * factor), min(height, y1 * factor)
                    # Read decimated: GDAL streams the window block by block into this
                    # buffer, so at most ~(2*tile)^2 pixels are ever resident.
                    ow = min(sx1 - sx0, tw * 2)
                    oh = min(sy1 - sy0, th * 2)
                    arr = ds.read(band, window=Window(sx0, sy0, sx1 - sx0, sy1 - sy0),
                                  out_shape=(max(1, oh), max(1, ow)),
                                  resampling=Resampling.average)
                    img = _to_u8(arr, lo, hi, nodata)
                    if img.shape[:2] != (th, tw):
                        img = cv2.resize(img, (tw, th), interpolation=cv2.INTER_AREA)
                    cv2.imwrite(os.path.join(level_dir, f"{col}_{row}.jpg"), img,
                                [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
                    written += 1

    # ponytail: every level re-reads the source decimated, so total work is
    # O(levels x pixels) even though memory stays at one tile. Upgrade path when a
    # gigapixel strip takes too long: build GDAL overviews first, or assemble each
    # level from the four tiles above it instead of going back to the source.
    with open(out_dzi, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n'
                f'<Image TileSize="{tile_size}" Overlap="{overlap}" Format="jpg"\n'
                f'       xmlns="{DZI_NS}">\n'
                f'  <Size Width="{width}" Height="{height}"/>\n'
                '</Image>\n')

    return {"dzi": out_dzi, "files_dir": files_dir, "width": width, "height": height,
            "tile_size": tile_size, "overlap": overlap, "levels": max_level + 1,
            "max_level": max_level, "tiles": written}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python viewer/tiles.py <raster> [out.dzi] [tile_size]", file=sys.stderr)
        raise SystemExit(2)
    src = sys.argv[1]
    out = sys.argv[2] if len(sys.argv) > 2 else None
    ts = int(sys.argv[3]) if len(sys.argv) > 3 else TILE_SIZE
    print(json.dumps(build_dzi(src, out, tile_size=ts), indent=2))
