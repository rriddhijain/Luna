"""Seat 3 (I/O) — mission-agnostic metadata adapter.

Pillar: honest provenance. Missions disagree about field names; this module is the
adapter that folds a sidecar JSON, a PDS3/PDS4 label or plain GeoTIFF tags into one
canonical dict. A field that is not present stays ``None`` and is recorded as
"unknown" in ``meta_source`` — it is never replaced by a plausible default.
"""

import json
import math
import os
import re
import xml.etree.ElementTree as ET

import rasterio

# Canonical key -> raw spellings, searched in order. Keys are compared upper-cased.
_ALIASES = {
    "product_id": ("PRODUCT_ID", "PRODUCT_NAME", "IMAGE_ID", "LOGICAL_IDENTIFIER"),
    "instrument": ("INSTRUMENT", "INSTRUMENT_ID", "INSTRUMENT_NAME", "INSTRUMENT_HOST_ID"),
    "gsd_m": ("GSD_M", "MAP_SCALE", "PIXEL_RESOLUTION", "PIXEL_SCALE", "RESOLUTION"),
    "sun_az_deg": ("SUN_AZ_DEG", "SUB_SOLAR_AZIMUTH", "SOLAR_AZIMUTH", "SUN_AZIMUTH",
                   "SUB_SOLAR_AZIMUTH_ANGLE"),
    "sun_el_deg": ("SUN_EL_DEG", "SOLAR_ELEVATION", "SUN_ELEVATION", "SUB_SOLAR_ELEVATION"),
    "incidence_deg": ("INCIDENCE_DEG", "INCIDENCE_ANGLE", "SOLAR_INCIDENCE_ANGLE"),
    "emission_deg": ("EMISSION_DEG", "EMISSION_ANGLE"),
    "phase_deg": ("PHASE_DEG", "PHASE_ANGLE"),
}
_ANGLE_KEYS = ("sun_az_deg", "sun_el_deg", "incidence_deg", "emission_deg", "phase_deg")
_SOURCES = ("sidecar", "pds_label", "geotiff")          # descending priority
_RASTER_FIRST = ("geotiff", "sidecar", "pds_label")     # shape/dtype: the file wins
_LABEL_EXTS = (".LBL", ".lbl", ".XML", ".xml", ".lblx", ".LBLX")
_STRUCTURE_KEYS = ("OBJECT", "END_OBJECT", "GROUP", "END_GROUP", "END")
_KEY_RE = re.compile(r"^[\^A-Z0-9_:]+$")
_NUM_RE = re.compile(r"^\s*([-+]?[0-9]*\.?[0-9]+(?:[eEdD][-+]?[0-9]+)?)\s*(?:<([^>]*)>)?\s*$")


def _number(value):
    """Parse a PDS-style scalar into (float, unit); returns (None, None) if it is not one."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        v = float(value)
        return (v, None) if math.isfinite(v) else (None, None)
    if not isinstance(value, str):
        return None, None
    m = _NUM_RE.match(value)
    if m is None:
        return None, None
    try:
        v = float(m.group(1).replace("D", "E").replace("d", "e"))
    except ValueError:
        return None, None
    return (v, m.group(2)) if math.isfinite(v) else (None, None)


def _to_metres(value, unit):
    """Convert a labelled map scale to metres/pixel; None when the unit is not a length."""
    if value is None:
        return None
    if unit is None:
        # ponytail: a unitless map scale is read as metres/pixel (true for LRO/Chandrayaan
        # GeoTIFFs we have seen). Upgrade: reject unitless and force the caller to supply it.
        return value
    u = unit.strip().upper()
    if u.startswith("KM"):
        return value * 1000.0
    if u.startswith("M"):
        return value
    return None  # DEG/PIXEL and friends are not a ground sample distance in metres


def _parse_pds3_label(text):
    """Tolerant key/value scan of a PDS3 label — no grammar, just what survives on real products."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("/*", "#")) or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().upper()
        if key in _STRUCTURE_KEYS or not _KEY_RE.match(key):
            continue
        value = value.strip().strip('"').strip()
        if value:
            out.setdefault(key, value)  # first occurrence wins; groups repeat keys
    return out


def _parse_pds4_label(path):
    """Flatten a PDS4 XML label into upper-cased leaf tag -> text (unit folded into the text)."""
    out = {}
    root = ET.parse(path).getroot()
    for el in root.iter():
        text = (el.text or "").strip()
        if len(el) or not text:
            continue
        key = el.tag.split("}")[-1].upper()
        unit = el.get("unit")
        out.setdefault(key, f"{text} <{unit}>" if unit else text)
    return out


def _find_label(path):
    """Return the PDS label sitting beside an image, or None."""
    stem = os.path.splitext(path)[0]
    for ext in _LABEL_EXTS:
        candidate = stem + ext
        if os.path.exists(candidate):
            return candidate
    return None


def _read_label(path):
    """Parse a PDS3 or PDS4 label, best-effort, standard library only."""
    try:
        if path.lower().endswith((".xml", ".lblx")):
            return _parse_pds4_label(path)
        with open(path, "r", errors="replace") as fh:
            return _parse_pds3_label(fh.read())
    except (OSError, ET.ParseError):
        return {}


def _read_geotiff(path):
    """Raster geometry plus GDAL tags, upper-cased; {} when the file is not a readable raster."""
    out = {}
    try:
        with rasterio.open(path) as src:
            out["GEOTRANSFORM"] = list(src.transform)[:6]
            out["CRS"] = str(src.crs) if src.crs else None
            out["SHAPE"] = (int(src.height), int(src.width))
            out["DTYPE"] = str(src.dtypes[0])
            out["NODATA"] = src.nodata
            out["COUNT"] = int(src.count)
            crs = src.crs
            if crs is not None and crs.is_projected and \
                    str(crs.linear_units or "").lower() in ("metre", "meter", "m", "metres", "meters"):
                out["GSD_M"] = abs(float(src.transform.a))
            for tags in (src.tags(), src.tags(1)):
                for k, v in tags.items():
                    out.setdefault(str(k).upper(), v)
    except Exception:
        return {}
    return {k: v for k, v in out.items() if v is not None}


def read_metadata(path: str) -> dict:
    """Collect raw metadata for a product, keyed by source: sidecar JSON, PDS label, GeoTIFF."""
    raw = {"path": path, "label_path": None, "sidecar": {}, "pds_label": {}, "geotiff": {}}
    sidecar = path + ".json"
    if os.path.exists(sidecar):
        try:
            with open(sidecar, "r") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                raw["sidecar"] = {str(k).upper(): v for k, v in loaded.items()}
        except (OSError, ValueError):
            pass
    label = _find_label(path)
    if label is not None:
        raw["label_path"] = label
        raw["pds_label"] = _read_label(label)
    raw["geotiff"] = _read_geotiff(path)
    return raw


def _lookup(raw, aliases, order=_SOURCES):
    """First (value, source) hit for any alias, searching sources in priority order."""
    for source in order:
        found = raw.get(source) or {}
        for alias in aliases:
            if found.get(alias) is not None:
                return found[alias], source
    return None, "unknown"


def _six(value):
    """Validate a 6-element geotransform (rasterio Affine order a,b,c,d,e,f)."""
    try:
        out = tuple(float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return out if len(out) == 6 and all(math.isfinite(v) for v in out) else None


def normalise_meta(raw: dict, path: str) -> dict:
    """Fold raw mission metadata into the canonical Product.meta; anything absent stays None."""
    if not any(k in raw for k in _SOURCES):  # tolerate a flat dict from a caller
        raw = {"sidecar": {str(k).upper(): v for k, v in raw.items()}}
    meta, where = {}, {}

    for key in ("product_id", "instrument"):
        value, source = _lookup(raw, _ALIASES[key])
        meta[key] = str(value) if value is not None else None
        where[key] = source if meta[key] else "unknown"
    if meta["product_id"] is None:
        meta["product_id"] = os.path.basename(path)
        where["product_id"] = "filename"

    value, source = _lookup(raw, _ALIASES["gsd_m"])
    gsd = _to_metres(*_number(value))
    meta["gsd_m"] = gsd
    where["gsd_m"] = source if gsd is not None else "unknown"

    for key in _ANGLE_KEYS:
        value, source = _lookup(raw, _ALIASES[key])
        number, _unit = _number(value)
        meta[key] = number
        where[key] = source if number is not None else "unknown"

    # Flat-surface identity: elevation = 90 - incidence. Recorded as derived, never as measured.
    if meta["sun_el_deg"] is None and meta["incidence_deg"] is not None:
        meta["sun_el_deg"] = 90.0 - meta["incidence_deg"]
        where["sun_el_deg"] = "derived_from_incidence"
    elif meta["incidence_deg"] is None and meta["sun_el_deg"] is not None:
        meta["incidence_deg"] = 90.0 - meta["sun_el_deg"]
        where["incidence_deg"] = "derived_from_sun_elevation"

    value, source = _lookup(raw, ("GEOTRANSFORM",))
    geotransform = _six(value)
    meta["geotransform"] = geotransform
    where["geotransform"] = source if geotransform is not None else "unknown"

    value, source = _lookup(raw, ("CRS", "COORDINATE_SYSTEM_NAME", "MAP_PROJECTION_TYPE"))
    meta["crs"] = str(value) if value else None
    where["crs"] = source if meta["crs"] else "unknown"

    value, source = _lookup(raw, ("SHAPE",), order=_RASTER_FIRST)
    try:
        shape = (int(value[0]), int(value[1])) if value is not None and len(value) >= 2 else None
    except (TypeError, ValueError):
        shape = None
    meta["shape"] = shape
    where["shape"] = source if shape is not None else "unknown"

    value, source = _lookup(raw, ("DTYPE",), order=_RASTER_FIRST)
    meta["dtype"] = str(value) if value else None
    where["dtype"] = source if meta["dtype"] else "unknown"

    value, source = _lookup(raw, ("NODATA", "MISSING_CONSTANT", "CORE_NULL"))
    nodata, _unit = _number(value)
    meta["nodata"] = nodata
    where["nodata"] = source if nodata is not None else "unknown"

    meta["meta_source"] = where
    return meta
