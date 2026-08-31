"""
samanvay/io/pds.py

OWNER: seat 5 (Systems & Performance), feeding seat 3's loader.

Ingestion for Chandrayaan-2 OHRC / TMC-2 style PDS4 products, i.e. a detached
XML label describing a raw ``.img`` (e.g. the ``ohr_r1_r11_shape_ver4``
product). Three layers, most robust first:

  1. GDAL/rasterio can often open a PDS4 ``.xml`` label directly -- try that.
  2. Otherwise parse the label ourselves (namespace-agnostic) for the array
     dimensions, element type and data offset, and ``memmap`` the raw image so
     a gigapixel strip is never fully materialised on load.
  3. Failing everything, the caller falls back to a sidecar ``<file>.json``.

Illumination geometry (incidence / emission / phase / sub-solar azimuth) is
harvested best-effort by scanning the label for the usual tag names, because
missions disagree about exactly where they put it. Anything not found is left
for a sidecar JSON to supply -- the pipeline must never silently invent a sun
angle.
"""

from __future__ import annotations

import os
import xml.etree.ElementTree as ET

import numpy as np

__all__ = [
    "find_product_files",
    "pds4_available",
    "read_pds4",
]

# PDS4 element data_type -> numpy dtype string (byte order encoded).
_PDS4_DTYPES = {
    "IEEE754MSBSingle": ">f4",
    "IEEE754LSBSingle": "<f4",
    "IEEE754MSBDouble": ">f8",
    "IEEE754LSBDouble": "<f8",
    "SignedMSB2": ">i2",
    "SignedLSB2": "<i2",
    "UnsignedMSB2": ">u2",
    "UnsignedLSB2": "<u2",
    "SignedMSB4": ">i4",
    "SignedLSB4": "<i4",
    "UnsignedMSB4": ">u4",
    "UnsignedLSB4": "<u4",
    "SignedByte": "i1",
    "UnsignedByte": "u1",
}

_RASTER_EXTS = (".tif", ".tiff", ".jp2", ".png")
_LABEL_EXTS = (".xml", ".lbl")
_RAW_EXTS = (".img", ".qub", ".dat", ".raw")


def pds4_available() -> bool:
    """True if GDAL exposes a PDS4 driver (nice-to-have fast path)."""
    try:
        from osgeo import gdal  # type: ignore

        return gdal.GetDriverByName("PDS4") is not None
    except Exception:
        return False


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find_product_files(path: str) -> dict:
    """Resolve a user-supplied path to concrete data/label files.

    ``path`` may be a directory (an unpacked product), a label, a raster, or a
    raw image. Returns ``{"kind", "data", "label"}`` where ``kind`` is one of
    ``raster`` | ``pds4`` | ``unknown``.
    """
    if os.path.isdir(path):
        return _scan_dir(path)

    ext = os.path.splitext(path)[1].lower()
    if ext in _RASTER_EXTS:
        return {"kind": "raster", "data": path, "label": None}
    if ext in _LABEL_EXTS:
        return {"kind": "pds4", "data": _data_for_label(path), "label": path}
    if ext in _RAW_EXTS:
        label = _label_for_data(path)
        return {"kind": "pds4" if label else "unknown", "data": path, "label": label}
    return {"kind": "unknown", "data": path, "label": None}


# Directories that hold *derived* products or prior pipeline output, never the
# authoritative input. Pruned during the scan so a leftover ``run/registered.tif``
# (or a cache) can't shadow the real PDS4 label+raw it was produced from.
_DERIVED_DIRS = {
    "run",
    "runs",
    "output",
    "outputs",
    "result",
    "results",
    ".git",
    "__pycache__",
    ".samanvay_cache",
}


def _scan_dir(root: str) -> dict:
    data_rasters: list[str] = []
    browse_rasters: list[str] = []
    labels: list[str] = []
    raws: list[str] = []
    for dirpath, dirs, files in os.walk(root):
        # Prune derived/output dirs in place so previous runs don't pollute the
        # scan. We only prune *children*, so pointing directly at e.g. a dir
        # literally named "run" still works.
        dirs[:] = [d for d in dirs if d.lower() not in _DERIVED_DIRS]
        for name in files:
            full = os.path.join(dirpath, name)
            ext = os.path.splitext(name)[1].lower()
            if ext in _RASTER_EXTS:
                (browse_rasters if _looks_like_browse(full) else data_rasters).append(full)
            elif ext in _LABEL_EXTS:
                labels.append(full)
            elif ext in _RAW_EXTS:
                raws.append(full)

    # A PDS4 label that actually resolves to an on-disk data file IS the product
    # and takes precedence over incidental rasters (e.g. a derived GeoTIFF that
    # slipped into the tree). Labels that resolve to nothing (stray metadata
    # XML, ISO sidecars) are ignored so a plain-GeoTIFF directory still works.
    resolved = [(lab, _data_for_label(lab)) for lab in labels]
    resolved = [(lab, data) for lab, data in resolved if data]
    if resolved:
        resolved.sort(key=lambda t: len(t[0]))
        lab, data = resolved[0]
        return {"kind": "pds4", "data": data, "label": lab}
    # Otherwise a real (non-browse) georeferenced raster.
    if data_rasters:
        data_rasters.sort(key=len)
        return {"kind": "raster", "data": data_rasters[0], "label": None}
    # A raw image with no resolvable label (best-effort same-stem match).
    if raws:
        raws.sort(key=len)
        return {"kind": "pds4", "data": raws[0], "label": _label_for_data(raws[0])}
    # Last: a browse thumbnail is better than nothing.
    if browse_rasters:
        browse_rasters.sort(key=len)
        return {"kind": "raster", "data": browse_rasters[0], "label": None}
    return {"kind": "unknown", "data": root, "label": None}


def _looks_like_browse(path: str) -> int:
    p = path.lower()
    return 1 if ("browse" in p or "thumb" in p or "preview" in p) else 0


def _data_for_label(label_path: str) -> str | None:
    """Prefer the file the label names; fall back to a same-stem raw file."""
    try:
        info = _parse_label(label_path)
        if info.get("data_file"):
            cand = os.path.join(os.path.dirname(label_path), info["data_file"])
            if os.path.exists(cand):
                return cand
    except Exception:
        pass
    stem = os.path.splitext(label_path)[0]
    for ext in _RAW_EXTS:
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def _label_for_data(data_path: str) -> str | None:
    stem = os.path.splitext(data_path)[0]
    for ext in _LABEL_EXTS:
        if os.path.exists(stem + ext):
            return stem + ext
    return None


def _findall_local(root: ET.Element, local_name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _strip_ns(e.tag) == local_name]


def _first_text(root: ET.Element, local_name: str) -> str | None:
    for e in root.iter():
        if _strip_ns(e.tag) == local_name and e.text and e.text.strip():
            return e.text.strip()
    return None


def _parse_label(label_path: str) -> dict:
    """Namespace-agnostic parse of the bits we need from a PDS4 label."""
    tree = ET.parse(label_path)
    root = tree.getroot()
    info: dict = {}

    # Data file name (File_Area_Observational/File/file_name)
    info["data_file"] = _first_text(root, "file_name")

    # Array_2D_Image geometry: axes + element type + offset
    arrays = _findall_local(root, "Array_2D_Image") or _findall_local(root, "Array_2D")
    if arrays:
        arr = arrays[0]
        axes = {}
        for ax in _findall_local(arr, "Axis_Array"):
            axis_name = (_first_text(ax, "axis_name") or "").lower()
            elements = _first_text(ax, "elements")
            seq = _first_text(ax, "sequence_number")
            if elements is not None:
                axes[axis_name] = {
                    "elements": int(elements),
                    "sequence": int(seq) if seq else None,
                }
        info["axes"] = axes
        # Line = rows (height), Sample = cols (width)
        info["height"] = axes.get("line", {}).get("elements")
        info["width"] = axes.get("sample", {}).get("elements")
        dtype_name = _first_text(arr, "data_type")
        info["data_type"] = dtype_name
        info["np_dtype"] = _PDS4_DTYPES.get(dtype_name or "")
        off = _first_text(arr, "offset")
        info["offset_bytes"] = int(off) if off and off.isdigit() else 0
    else:
        info["axes"] = {}

    # Best-effort illumination geometry, scanning by tag substring.
    geom = {}
    wanted = {
        "incidence": "incidence_deg",
        "emission": "emission_deg",
        "phase": "phase_deg",
        "sub_solar_azimuth": "sun_az_deg",
        "subsolar_azimuth": "sun_az_deg",
        "solar_azimuth": "sun_az_deg",
        "sub_solar": "sub_solar_deg",
    }
    for e in root.iter():
        local = _strip_ns(e.tag).lower()
        if not e.text or not e.text.strip():
            continue
        for needle, key in wanted.items():
            if needle in local and key not in geom:
                try:
                    geom[key] = float(e.text.strip())
                except ValueError:
                    pass
    info["geometry"] = geom
    return info


def read_pds4(data_path: str | None, label_path: str | None) -> tuple[np.ndarray, dict]:
    """Read a PDS4 product into ``(array, meta)``.

    Tries GDAL first (handles projection and odd encodings), then a manual
    memmap read driven by the parsed label. ``array`` is a read-only memmap for
    the manual path so gigapixel strips are not copied into RAM on load.
    """
    # Fast path: GDAL can often read the label directly.
    if label_path:
        arr_meta = _try_gdal(label_path)
        if arr_meta is not None:
            return arr_meta

    if not label_path or not os.path.exists(label_path):
        raise FileNotFoundError(f"No PDS4 label found for {data_path!r}")

    info = _parse_label(label_path)
    data = data_path or _data_for_label(label_path)
    if not data or not os.path.exists(data):
        raise FileNotFoundError(
            f"PDS4 label {label_path!r} references a data file that was not found"
        )

    dtype = info.get("np_dtype")
    h, w = info.get("height"), info.get("width")
    if dtype is None or not h or not w:
        raise ValueError(
            f"PDS4 label {label_path!r} is missing dimensions/data_type; "
            "supply a sidecar JSON with shape+dtype instead"
        )

    itemsize = np.dtype(dtype).itemsize
    expected = info.get("offset_bytes", 0) + h * w * itemsize
    file_size = os.path.getsize(data)
    if file_size < expected:
        raise ValueError(
            f"raw file {data!r} is {file_size} bytes, label implies >= {expected}"
        )

    array = np.memmap(
        data,
        dtype=np.dtype(dtype),
        mode="r",
        offset=info.get("offset_bytes", 0),
        shape=(int(h), int(w)),
    )

    meta = {
        "product_id": os.path.splitext(os.path.basename(label_path))[0],
        "instrument": "OHRC",
        "gsd_m": 0.25,  # OHRC nominal; over<-ride via sidecar for the real value
        "geotransform": [0.0, 1.0, 0.0, 0.0, 0.0, -1.0],
        "crs": None,
        "shape": (int(h), int(w)),
        "dtype": str(np.dtype(dtype)),
        "source_format": "PDS4",
        "label_path": label_path,
        "data_path": data,
    }
    meta.update(info.get("geometry", {}))
    # Derive sun elevation from incidence if only incidence is present.
    if "sun_el_deg" not in meta and "incidence_deg" in meta:
        meta["sun_el_deg"] = 90.0 - float(meta["incidence_deg"])
    return array, meta


def _try_gdal(label_path: str) -> tuple[np.ndarray, dict] | None:
    """Attempt to read a PDS4 label via rasterio/GDAL; None if unsupported."""
    try:
        import rasterio
    except Exception:
        return None
    try:
        with rasterio.open(label_path) as ds:
            array = ds.read(1)
            meta = {
                "product_id": os.path.splitext(os.path.basename(label_path))[0],
                "instrument": "OHRC",
                "gsd_m": abs(ds.transform.a) if ds.transform else 0.25,
                "geotransform": list(ds.transform)[:6],
                "crs": str(ds.crs) if ds.crs else None,
                "shape": (ds.height, ds.width),
                "dtype": str(ds.dtypes[0]),
                "source_format": "PDS4/GDAL",
                "label_path": label_path,
            }
            # Merge any illumination geometry from the label text too.
            try:
                meta.update(_parse_label(label_path).get("geometry", {}))
                if "sun_el_deg" not in meta and "incidence_deg" in meta:
                    meta["sun_el_deg"] = 90.0 - float(meta["incidence_deg"])
            except Exception:
                pass
            return array, meta
    except Exception:
        return None
