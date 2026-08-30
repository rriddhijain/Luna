"""Seat 3 · io/preflight — answer "is this data usable?" before anyone runs a registration.

Real products arrive with missing sun angles, mismatched CRS, no overlap, or a DEM that
covers somewhere else. Every one of those produces a confusing registration failure ten
minutes later instead of a clear message now. This module reads what SAMANVAY will
actually see and says what will happen.

Nothing here is a judgement about the data's quality — only about what the pipeline can
do with it. A missing sun angle is a DEGRADED MODE, not a failure: canonicalise falls
back to an empirical illumination estimate that works. The loader never invents a value,
so preflight never has to guess whether one was invented.

Issue levels:
    blocker  the pipeline cannot produce a result at all
    warning  it will run, in a named degraded mode
    note     informational; nothing to fix
"""

import os

import numpy as np
import rasterio

from samanvay.geometry.init import coarse_init_info, project_box
from samanvay.io.loaders import load_product

# Fields and the capability each one unlocks. Used for both the report and `enables`.
_PHYSICS_FIELDS = ("sun_az_deg", "sun_el_deg", "gsd_m")
_GEO_FIELDS = ("geotransform",)


def _issue(level, message, fix=""):
    """One finding. `fix` must name a concrete action, not restate the problem."""
    return {"level": level, "message": message, "fix": fix}


def _raster_stats(path):
    """Shape, dtype, band count and a cheap content sanity check; never raises."""
    out = {"shape": None, "dtype": None, "band_count": None, "size_mb": None,
           "constant": None, "nodata_frac": None}
    try:
        out["size_mb"] = round(os.path.getsize(path) / 1e6, 2)
    except OSError:
        pass
    try:
        with rasterio.open(path) as src:
            out["shape"] = (src.height, src.width)
            out["dtype"] = str(src.dtypes[0])
            out["band_count"] = src.count
            # Decimated read: enough to spot a blank or mostly-nodata frame without
            # pulling a gigapixel strip into RAM.
            step = max(1, min(src.height, src.width) // 512)
            band = src.read(1, out_shape=(1, max(1, src.height // step),
                                          max(1, src.width // step)))[0]
            finite = np.isfinite(band)
            if src.nodata is not None:
                finite &= band != src.nodata
            out["nodata_frac"] = round(float(1.0 - finite.mean()), 4)
            vals = band[finite]
            out["constant"] = bool(vals.size and float(vals.min()) == float(vals.max()))
    except Exception:
        pass
    return out


def check_product(path, role="source") -> dict:
    """What SAMANVAY will see in one product, and which capabilities it supports."""
    result = {"path": str(path), "role": role, "exists": os.path.exists(str(path)),
              "readable": False, "meta": {}, "meta_source": {}, "issues": [],
              "enables": {"matching": False, "physics": False, "geo_init": False}}
    result.update(_raster_stats(path) if result["exists"] else {})

    if not result["exists"]:
        result["issues"].append(_issue(
            "blocker", f"{role}: file does not exist: {path}",
            "check the path; the pipeline does not search for it"))
        return result

    try:
        product = load_product(str(path))
    except Exception as exc:
        result["issues"].append(_issue(
            "blocker", f"{role}: cannot be opened as a raster ({type(exc).__name__}: {exc})",
            "convert to GeoTIFF, or check the file is not truncated"))
        return result

    result["readable"] = True
    result["enables"]["matching"] = True
    meta = product.meta or {}
    result["meta"] = {k: meta.get(k) for k in (
        "product_id", "instrument", "gsd_m", "sun_az_deg", "sun_el_deg",
        "incidence_deg", "emission_deg", "crs", "geotransform", "nodata")}
    result["meta_source"] = dict(meta.get("meta_source") or {})

    # A DEM is terrain, not an observation: it has no sun geometry to be missing, and
    # its geotransform is checked at the pair level against the source's footprint.
    is_dem = role == "dem"
    missing_physics = [f for f in _PHYSICS_FIELDS if meta.get(f) is None]
    result["enables"]["physics"] = not missing_physics
    result["enables"]["geo_init"] = all(meta.get(f) is not None for f in _GEO_FIELDS)

    sidecar = f"{path}.json"
    if missing_physics and not is_dem:
        result["issues"].append(_issue(
            "warning",
            f"{role}: {', '.join(missing_physics)} unknown, so the P1 physics stage "
            f"falls back to empirical illumination (this works, it is just not the "
            f"DEM-driven claim)",
            f"add them to a sidecar JSON at {sidecar}, e.g. "
            f'{{"sun_az_deg": 132.4, "sun_el_deg": 23.1, "gsd_m": 0.28}}'))
    if meta.get("sun_el_deg") is not None and result["meta_source"].get("sun_el_deg") == "derived":
        result["issues"].append(_issue(
            "note", f"{role}: sun elevation was derived as 90 - incidence, not read directly"))
    if not result["enables"]["geo_init"] and not is_dem:
        result["issues"].append(_issue(
            "warning",
            f"{role}: no geotransform, so P2 coarse init degrades to a GSD-ratio guess "
            f"or identity and matching starts much further from the answer",
            f"georeference the product, or put a 6-element rasterio-order geotransform "
            f"in {sidecar}"))
    if meta.get("crs") in (None, "", "None") and not is_dem:
        result["issues"].append(_issue(
            "note", f"{role}: no CRS. Not fatal — registration is in pixels."))
    if (result.get("band_count") or 1) > 1:
        result["issues"].append(_issue(
            "note", f"{role}: {result['band_count']} bands; the pipeline uses band 1 only",
            "for IIRS, choose a band or build a composite first and say which"))
    if result.get("constant"):
        result["issues"].append(_issue(
            "blocker", f"{role}: the raster is constant — no texture to match",
            "check the file is not a blank or fill-only tile"))
    if (result.get("nodata_frac") or 0) > 0.5:
        result["issues"].append(_issue(
            "warning",
            f"{role}: {result['nodata_frac']:.0%} of the frame is nodata",
            "crop to the valid region so uniformity is not measured over empty space"))
    shape = result.get("shape") or (0, 0)
    if min(shape) and min(shape) < 128:
        result["issues"].append(_issue(
            "warning", f"{role}: only {shape[0]}x{shape[1]} px; tiling and quotas need room",
            "use --set grid_n=2, or a larger crop"))
    return result


def _overlap(src_check, ref_check, H):
    """Fraction of the projected source box that lands inside the reference frame."""
    try:
        sh, sw = src_check["shape"]
        rh, rw = ref_check["shape"]
        x0, y0, x1, y1 = project_box(H, 0, 0, sw - 1, sh - 1)
        ix = max(0.0, min(x1, rw - 1) - max(x0, 0.0))
        iy = max(0.0, min(y1, rh - 1) - max(y0, 0.0))
        box = max(1e-9, (x1 - x0) * (y1 - y0))
        return float(max(0.0, ix * iy) / box)
    except Exception:
        return None


def check_pair(source_path, ref_path, dem_path=None) -> dict:
    """Both products plus the pair-level findings, a verdict, and a recommended command."""
    src = check_product(source_path, "source")
    ref = check_product(ref_path, "reference")
    out = {"source": src, "reference": ref, "dem": None, "issues": [],
           "scale_ratio": None, "scale_ratio_from_geotransform": None,
           "crs_match": None, "delta_sun_az_deg": None, "delta_sun_el_deg": None,
           "init_method": None, "overlap_frac": None,
           "recommended_config": {}, "recommended_reasons": [], "verdict": "blocked"}

    if not (src["readable"] and ref["readable"]):
        out["verdict"] = "blocked"
        return out

    sm, rm = src["meta"], ref["meta"]

    if sm.get("gsd_m") and rm.get("gsd_m"):
        out["scale_ratio"] = round(float(sm["gsd_m"]) / float(rm["gsd_m"]), 4)

    try:
        source, reference = load_product(str(source_path)), load_product(str(ref_path))
        H, info = coarse_init_info(source, reference)
        out["init_method"] = info.get("method")
        out["crs_match"] = info.get("crs_match")
        out["scale_ratio_from_geotransform"] = (
            round(float(info["scale"]), 4) if info.get("scale") else None)
        out["overlap_frac"] = _overlap(src, ref, H)
    except Exception as exc:
        out["issues"].append(_issue(
            "warning", f"coarse init could not be evaluated ({type(exc).__name__})",
            "the pipeline will still run, starting from identity"))

    a, b = out["scale_ratio"], out["scale_ratio_from_geotransform"]
    if a and b and max(a, b) / max(1e-9, min(a, b)) > 1.2:
        out["issues"].append(_issue(
            "warning",
            f"scale ratio disagrees: {a} from gsd_m, {b} from the geotransforms",
            "one of the two is wrong; trust the geotransform and fix gsd_m in the sidecar"))

    if out["crs_match"] is False:
        out["issues"].append(_issue(
            "note", "the two products report different CRS — normal for a cross-mission "
                    "pair, and coarse init falls back to a GSD-ratio scale"))

    if out["overlap_frac"] is not None and out["overlap_frac"] < 0.05:
        out["issues"].append(_issue(
            "blocker",
            f"the source projects almost entirely outside the reference "
            f"({out['overlap_frac']:.1%} overlap) — these are not the same ground",
            "check you paired the right two products, and that both geotransforms "
            "are in the same CRS"))
    elif out["overlap_frac"] is not None and out["overlap_frac"] < 0.4:
        out["issues"].append(_issue(
            "warning", f"only {out['overlap_frac']:.0%} of the source lands in the reference",
            "crop to the shared region for better uniformity coverage"))

    if sm.get("sun_az_deg") is not None and rm.get("sun_az_deg") is not None:
        d = abs(float(sm["sun_az_deg"]) - float(rm["sun_az_deg"])) % 360.0
        out["delta_sun_az_deg"] = round(min(d, 360.0 - d), 2)
    if sm.get("sun_el_deg") is not None and rm.get("sun_el_deg") is not None:
        out["delta_sun_el_deg"] = round(
            abs(float(sm["sun_el_deg"]) - float(rm["sun_el_deg"])), 2)

    if dem_path:
        dem = check_product(dem_path, "dem")
        out["dem"] = dem
        if dem["readable"] and sm.get("gsd_m") and dem["meta"].get("gsd_m"):
            ratio = float(dem["meta"]["gsd_m"]) / float(sm["gsd_m"])
            dem["gsd_ratio_to_source"] = round(ratio, 2)
            if ratio > 4.0:
                out["issues"].append(_issue(
                    "note",
                    f"the DEM is {ratio:.0f}x coarser than the source, so P1 runs in "
                    f"dem_lowfreq mode: it removes the gross illumination field only "
                    f"and leaves fine structure to phase congruency"))

    # --- recommendation ------------------------------------------------------------
    cfg, why = {}, []
    dsun = out["delta_sun_az_deg"]
    if dsun is None or dsun >= 20.0:
        cfg["match.method"] = "rift"
        why.append(
            f"delta sun azimuth is {'unknown' if dsun is None else str(dsun) + ' deg'}; "
            f"RIFT is the illumination-robust path and is the only arm that holds past "
            f"40 deg (see bench/baselines.md)")
    else:
        cfg["match.method"] = "sift"
        why.append(f"delta sun azimuth is only {dsun} deg, where intensity matching is "
                   f"more accurate than RIFT")
    if dem_path and (out["dem"] or {}).get("readable"):
        cfg["--dem"] = str(dem_path)
        why.append("a readable DEM was supplied, so P1 can render predicted illumination")
    shape = src.get("shape") or (0, 0)
    if min(shape) and min(shape) < 512:
        cfg["grid_n"] = 2
        why.append(f"the source is only {shape[0]}x{shape[1]} px; a 4x4 grid would leave "
                   f"too little per cell")
    out["recommended_config"], out["recommended_reasons"] = cfg, why

    all_issues = src["issues"] + ref["issues"] + out["issues"]
    all_issues += (out["dem"] or {}).get("issues", [])
    if any(i["level"] == "blocker" for i in all_issues):
        out["verdict"] = "blocked"
    elif any(i["level"] == "warning" for i in all_issues):
        out["verdict"] = "ready_degraded"
    else:
        out["verdict"] = "ready"
    return out


def _fmt_product(c):
    """One product's block: what parsed, and where each field came from."""
    lines = [f"  {c['role']}: {c['path']}"]
    if not c["readable"]:
        return lines + ["    NOT READABLE"]
    lines.append(f"    {c['shape'][0]}x{c['shape'][1]} px, {c['dtype']}, "
                 f"{c['band_count']} band(s), {c['size_mb']} MB")
    for key in ("gsd_m", "sun_az_deg", "sun_el_deg", "crs", "instrument"):
        value = c["meta"].get(key)
        src = c["meta_source"].get(key, "unknown")
        lines.append(f"    {key:14} {'unknown' if value is None else value}"
                     f"   [{src}]")
    return lines


def format_report(result) -> str:
    """The report a human reads. Blockers first, then what will happen, then the command."""
    L = ["SAMANVAY preflight", "=" * 60, ""]
    L += _fmt_product(result["source"]) + [""]
    L += _fmt_product(result["reference"]) + [""]
    if result.get("dem"):
        L += _fmt_product(result["dem"]) + [""]

    L.append("  pair")
    for label, key in (("scale ratio (gsd)", "scale_ratio"),
                       ("scale ratio (geotransform)", "scale_ratio_from_geotransform"),
                       ("delta sun azimuth", "delta_sun_az_deg"),
                       ("delta sun elevation", "delta_sun_el_deg"),
                       ("coarse init", "init_method"),
                       ("CRS match", "crs_match")):
        value = result.get(key)
        L.append(f"    {label:28} {'unknown' if value is None else value}")
    overlap = result.get("overlap_frac")
    L.append(f"    {'source inside reference':28} "
             f"{'unknown' if overlap is None else format(overlap, '.1%')}")
    L.append("")

    issues = (result["source"]["issues"] + result["reference"]["issues"]
              + (result.get("dem") or {}).get("issues", []) + result["issues"])
    for level in ("blocker", "warning", "note"):
        picked = [i for i in issues if i["level"] == level]
        if not picked:
            continue
        L.append(f"  {level.upper()}S")
        for i in picked:
            L.append(f"    - {i['message']}")
            if i["fix"]:
                L.append(f"      fix: {i['fix']}")
        L.append("")

    L.append(f"  VERDICT: {result['verdict'].upper()}")
    for reason in result.get("recommended_reasons", []):
        L.append(f"    - {reason}")
    L.append("")

    if result["verdict"] == "blocked":
        L.append("  Fix the blockers above before running a registration.")
        return "\n".join(L)

    cfg = dict(result.get("recommended_config") or {})
    dem = cfg.pop("--dem", None)
    cmd = ["  samanvay register \\",
           f"      --source {result['source']['path']} \\",
           f"      --ref    {result['reference']['path']} \\"]
    if dem:
        cmd.append(f"      --dem    {dem} \\")
    for key, value in cfg.items():
        cmd.append(f"      --set {key}={value} \\")
    cmd.append("      --out runs/real_01 --viewer")
    L.append("  Run next:")
    L += cmd
    L.append("")
    L.append("  NOTE: a real pair has no ground truth, so metrics.json gt_rmse_px will be")
    L.append("  null. Judge the result on inlier_count, coverage_pct and rmse_trustworthy")
    L.append("  — a low rmse_px with few inliers is the fit reproducing its own sample.")
    return "\n".join(L)
