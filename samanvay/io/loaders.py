"""
samanvay/io/loaders.py

OWNER: seat 3 (Data & Backend). Highest fan-out seat -- five people consume
``load_product``.

Dispatch order for a given path:
  1. a directory / PDS4 label / raw ``.img`` -> OHRC-style PDS4 reader
     (samanvay.io.pds), so the real ``ohr_r1_r11_shape_ver4`` product loads;
  2. a GeoTIFF (or anything GDAL opens) -> rasterio;
  3. nothing on disk -> a mock Product (the D0 stub) so no seat is ever blocked.

A sidecar ``<path>.json`` is ALWAYS merged last, so any metadata the label
does not carry (or gets wrong) can be corrected without code changes -- the
escape hatch that keeps the sprint moving.
"""

from __future__ import annotations

import contextlib
import json
import os

import numpy as np

from samanvay.io.pds import find_product_files, read_pds4
from samanvay.types import Product

_RASTER_EXTS = (".tif", ".tiff", ".jp2", ".png")
_PDS4_EXTS = (".xml", ".lbl", ".img", ".qub", ".dat", ".raw")


def _apply_sidecar(path: str, meta: dict) -> dict:
    sidecar_path = path + ".json"
    if os.path.exists(sidecar_path):
        with contextlib.suppress(Exception), open(sidecar_path) as f:
            meta.update(json.load(f))
    return meta


def _mock_meta(path: str) -> dict:
    return {
        "product_id": os.path.basename(path),
        "instrument": "MOCK",
        "gsd_m": 1.0,
        "sun_az_deg": 45.0,
        "sun_el_deg": 30.0,
        "incidence_deg": 60.0,
        "emission_deg": 0.0,
        "geotransform": [0.0, 1.0, 0.0, 0.0, 0.0, -1.0],
        "crs": "EPSG:32601",
        "shape": (1024, 1024),
        "dtype": "float32",
    }


def _load_raster(path: str) -> Product:
    import rasterio

    with rasterio.open(path) as src:
        array = src.read(1).astype(np.float32)
        meta = {
            "product_id": os.path.basename(path),
            "instrument": "GEOTIFF",
            "gsd_m": abs(src.transform.a) if src.transform else 1.0,
            "sun_az_deg": 0.0,
            "sun_el_deg": 45.0,
            "incidence_deg": 45.0,
            "emission_deg": 0.0,
            "geotransform": list(src.transform)[:6],
            "crs": str(src.crs),
            "shape": src.shape,
            "dtype": str(src.dtypes[0]),
        }
    return Product(path=path, array=array, meta=_apply_sidecar(path, meta))


def _load_pds4(resolved: dict, path: str) -> Product:
    array, meta = read_pds4(resolved.get("data"), resolved.get("label"))
    # Present a float32 view to the rest of the pipeline while keeping the
    # on-disk memmap semantics for the read (no forced full copy here).
    meta.setdefault("product_id", os.path.basename(path.rstrip("/")))
    return Product(path=path, array=array, meta=_apply_sidecar(path, meta))


def load_product(path: str) -> Product:
    """Load any supported product into a :class:`Product`.

    Supports GeoTIFF/JP2/PNG (rasterio), Chandrayaan-2 OHRC / TMC-2 PDS4
    products (directory, ``.xml`` label, or raw ``.img``), and a mock fallback
    for a non-existent path (the never-blocked stub). A sidecar ``<path>.json``
    always overrides.
    """
    if not os.path.exists(path):
        meta = _apply_sidecar(path, _mock_meta(path))
        return Product(path=path, array=np.zeros((1024, 1024), dtype=np.float32), meta=meta)

    ext = os.path.splitext(path)[1].lower()

    # PDS4 / OHRC: a directory, a label, or a raw image.
    if os.path.isdir(path) or ext in _PDS4_EXTS:
        resolved = find_product_files(path)
        if resolved.get("kind") == "pds4":
            with contextlib.suppress(Exception):
                return _load_pds4(resolved, path)
        if resolved.get("kind") == "raster" and resolved.get("data"):
            return _load_raster(resolved["data"])
        # Fall through to the generic raster attempt below.

    if ext in _RASTER_EXTS:
        return _load_raster(path)

    # Last resort: let GDAL try (covers unusual but readable formats).
    with contextlib.suppress(Exception):
        return _load_raster(path)

    raise ValueError(
        f"Unrecognised product {path!r}. Provide a GeoTIFF, a PDS4 label/.img, "
        f"or a sidecar {path}.json describing the array."
    )
