"""Seat 3 (I/O) — the deliverable export layer.

Pillar: honest provenance. Six artifacts per run: a genuinely georeferenced
registered.tif on the reference grid, matches.csv on the frozen schema, the transform
and the model ladder's verdict, the metrics, a real reproducibility record, and a report.
"""

import csv
import datetime as _dt
import hashlib
import importlib.metadata as _im
import json
import math
import os
import struct

import numpy as np
import rasterio

from samanvay.types import MatchSet, Product, Registration

MATCH_COLUMNS = ["id", "src_x", "src_y", "ref_x", "ref_y", "score",
                 "is_inlier", "residual_px", "sigma_px", "grid_cell"]
_PACKAGES = ("numpy", "scipy", "rasterio", "scikit-image", "matplotlib", "click",
             "pyyaml", "pytest", "opencv-python-headless", "opencv-python", "samanvay")
_HASH_LIMIT = 8 << 20  # only re-hash files up to 8 MiB when checking the tree for changes
_LADDER = ("similarity", "affine", "homography")  # simplest first, as geometry fits them


# ---------------------------------------------------------------- git, stdlib only

def _find_git_dir(start):
    """Walk up from a path to the repository's .git directory (handles a .git file)."""
    path = os.path.abspath(start)
    while True:
        candidate = os.path.join(path, ".git")
        if os.path.isdir(candidate):
            return candidate
        if os.path.isfile(candidate):
            try:
                text = open(candidate).read().strip()
            except OSError:
                return None
            if text.startswith("gitdir:"):
                gitdir = text.split(":", 1)[1].strip()
                return gitdir if os.path.isabs(gitdir) else os.path.join(path, gitdir)
            return None
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


def _resolve_ref(git_dir, ref):
    """Resolve a ref to a 40-hex sha, loose file first then packed-refs."""
    loose = os.path.join(git_dir, ref)
    if os.path.isfile(loose):
        return open(loose).read().strip()
    packed = os.path.join(git_dir, "packed-refs")
    if os.path.isfile(packed):
        for line in open(packed):
            if line.startswith(("#", "^")):
                continue
            parts = line.split()
            if len(parts) == 2 and parts[1] == ref:
                return parts[0]
    return None


def _blob_sha1(path):
    """git blob sha1 of a file, or None if it cannot be read."""
    try:
        data = open(path, "rb").read()
    except OSError:
        return None
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def _index_dirty(git_dir, work_tree):
    """True/False if the tracked tree differs from .git/index; None if the index is unreadable.

    ponytail: tracked files only, and files over 8 MiB whose mtime moved are called dirty
    rather than re-hashed. Untracked files do not flip the flag. Upgrade path: read
    .gitignore and walk the tree, which is a real ignore-matcher's worth of code.
    """
    try:
        data = open(os.path.join(git_dir, "index"), "rb").read()
    except OSError:
        return None
    if len(data) < 12 or data[:4] != b"DIRC":
        return None
    version, count = struct.unpack(">II", data[4:12])
    if version not in (2, 3):
        return None  # v4 path-compresses; not worth the parser
    offset = 12
    for _ in range(count):
        if offset + 62 > len(data):
            return None
        fields = struct.unpack(">10I", data[offset:offset + 40])
        size = fields[9]
        mtime_s = fields[2]
        sha = data[offset + 40:offset + 60].hex()
        flags = struct.unpack(">H", data[offset + 60:offset + 62])[0]
        base = 62 + (2 if flags & 0x4000 else 0)
        namelen = flags & 0x0FFF
        start = offset + base
        if namelen == 0x0FFF:
            end = data.index(b"\x00", start)
        else:
            end = start + namelen
        name = data[start:end].decode("utf-8", "replace")
        offset += (base + (end - start) + 8) & ~7  # entries are self-padded, not aligned
        full = os.path.join(work_tree, name)
        try:
            st = os.stat(full)
        except OSError:
            return True  # tracked file gone
        if st.st_size != size:
            return True
        if int(st.st_mtime) != mtime_s:
            if st.st_size > _HASH_LIMIT or _blob_sha1(full) != sha:
                return True
    return False


def _git_provenance(start):
    """The repository's real head sha, branch and dirty flag; unknowns stay None."""
    out = {"sha": None, "branch": None, "dirty": None, "git_dir": None}
    git_dir = _find_git_dir(start)
    if git_dir is None:
        return out
    out["git_dir"] = git_dir
    try:
        head = open(os.path.join(git_dir, "HEAD")).read().strip()
    except OSError:
        return out
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        out["branch"] = ref.rsplit("/", 1)[-1]
        sha = _resolve_ref(git_dir, ref)
    else:
        sha = head  # detached HEAD
    if sha and len(sha) == 40 and all(c in "0123456789abcdef" for c in sha.lower()):
        out["sha"] = sha.lower()
    work_tree = os.path.dirname(git_dir)
    out["dirty"] = _index_dirty(git_dir, work_tree)
    return out


def _package_versions():
    """Installed versions of the packages this run depends on; None when not installed."""
    versions = {}
    for name in _PACKAGES:
        try:
            versions[name] = _im.version(name)
        except _im.PackageNotFoundError:
            continue
    return versions


# ---------------------------------------------------------------- artifact writers

def _as_dtype(array, dtype):
    """Cast a warped array back to the source dtype, rounding and clipping for integers."""
    dtype = np.dtype(dtype)
    if array.dtype == dtype:
        return array
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(np.rint(array), info.min, info.max).astype(dtype)
    return array.astype(dtype)


def _write_registered(out_path, registered_array, source, reference, config):
    """Write the warped source on the reference grid: reference CRS/transform, source dtype."""
    array = np.asarray(registered_array)
    if array.ndim == 2:
        array = array[None, :, :]
    elif array.ndim == 3:
        array = np.moveaxis(array, 2, 0)
    else:
        raise ValueError(f"registered_array must be 2-D or 3-D, got shape {array.shape}")

    gt = reference.meta.get("geotransform")
    transform = rasterio.Affine(*gt) if gt is not None else None
    crs = reference.meta.get("crs")

    dtype = np.dtype(source.meta.get("dtype") or array.dtype)
    array = _as_dtype(array, dtype)

    # ponytail: uncovered pixels are whatever the warp filled in (cv2 leaves 0). We declare
    # that value as nodata unless the source or config names one. Upgrade: have the warp
    # stage hand back its own fill value / validity mask.
    nodata = config.get("nodata") if isinstance(config, dict) else None
    if nodata is None:
        nodata = source.meta.get("nodata")
    if nodata is None:
        nodata = 0

    with rasterio.open(
        out_path, "w", driver="GTiff",
        height=array.shape[1], width=array.shape[2], count=array.shape[0],
        dtype=str(array.dtype), crs=crs, transform=transform, nodata=nodata,
        tiled=True, blockxsize=256, blockysize=256, compress="deflate",
    ) as dst:
        dst.write(array)


def _match_rows(matches, registration):
    """Yield matches.csv rows; empty registration arrays become blanks, not invented numbers."""
    n = len(matches.src_xy)
    inliers = np.asarray(registration.inliers) if registration.inliers is not None else np.empty(0)
    residuals = np.asarray(registration.residuals) if registration.residuals is not None else np.empty((0, 2))
    sigma = np.asarray(registration.sigma) if registration.sigma is not None else np.empty(0)
    cell = np.asarray(matches.cell) if matches.cell is not None else np.empty(0)

    for name, arr in (("ref_xy", matches.ref_xy), ("score", matches.score)):
        if len(arr) != n:
            raise ValueError(f"MatchSet.{name} has {len(arr)} entries, src_xy has {n}")
    for name, arr in (("inliers", inliers), ("residuals", residuals), ("sigma", sigma), ("cell", cell)):
        if len(arr) not in (0, n):
            raise ValueError(f"{name} has {len(arr)} entries, MatchSet has {n} matches")

    for i in range(n):
        yield [
            i,
            float(matches.src_xy[i, 0]), float(matches.src_xy[i, 1]),
            float(matches.ref_xy[i, 0]), float(matches.ref_xy[i, 1]),
            float(matches.score[i]),
            int(bool(inliers[i])) if len(inliers) else "",
            # residuals are already in SOURCE pixels (frozen convention)
            float(np.linalg.norm(residuals[i])) if len(residuals) else "",
            float(sigma[i]) if len(sigma) else "",
            int(cell[i]) if len(cell) else "",
        ]


def _json_safe(obj):
    """JSON-strict tree: numpy becomes python, NaN/inf become null (a failed fit has no number)."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return _json_safe(obj.tolist())
    if isinstance(obj, np.generic):
        obj = obj.item()
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    return str(obj)


def _ladder_verdict(metrics, chosen):
    """Which models the ladder did not pick and why, using only numbers geometry reported."""
    explicit = metrics.get("rejected_models")
    if explicit is not None:
        return explicit
    candidates = metrics.get("model_candidates") or {}
    verdict = []
    for name, stats in candidates.items():
        if name == chosen:
            continue
        stats = stats if isinstance(stats, dict) else {}
        if name in _LADDER and chosen in _LADDER:
            simpler = _LADDER.index(name) < _LADDER.index(chosen)
            reason = ("inlier RMSE outside model_margin of the best fit" if simpler
                      else "not needed: a simpler model fitted within model_margin")
        else:
            reason = "not selected"
        verdict.append({
            "model_type": name,
            "reason": reason,
            "rmse_px": stats.get("rmse_px"),
            "rmse_common_px": stats.get("rmse_common_px"),
            "inlier_count": stats.get("inlier_count"),
        })
    return verdict


def _fallback_report(out_html, source, reference, registration, matches):
    """Minimal self-contained HTML, used when the report module is unavailable."""
    m = registration.metrics or {}
    with open(out_html, "w") as f:
        f.write(f"""<!DOCTYPE html>
<html>
<head>
    <title>Samanvay Registration Report</title>
    <style>
        body {{ font-family: sans-serif; margin: 20px; background: #1e1e1e; color: #fff; }}
        h1 {{ color: #00bcd4; }}
        .metric-card {{ background: #2d2d2d; padding: 15px; border-radius: 8px; display: inline-block; margin-right: 15px; }}
    </style>
</head>
<body>
    <h1>SAMANVAY Registration Report</h1>
    <p>Source: {source.path}</p>
    <p>Reference: {reference.path}</p>
    <div class="metric-card"><h3>RMSE (source px)</h3><p>{m.get('rmse_px')}</p></div>
    <div class="metric-card"><h3>Inliers</h3><p>{m.get('inlier_count')} / {len(matches.src_xy)}</p></div>
    <div class="metric-card"><h3>Coverage</h3><p>{m.get('coverage_pct')}</p></div>
    <div class="metric-card"><h3>Model</h3><p>{registration.model_type}</p></div>
</body>
</html>
""")


def write_outputs(
    out_dir: str,
    source: Product,
    reference: Product,
    registration: Registration,
    matches: MatchSet,
    registered_array: np.ndarray,
    config: dict,
) -> None:
    """Write the six run artifacts: registered.tif, matches.csv, transform/metrics/provenance, report."""
    os.makedirs(out_dir, exist_ok=True)
    config = config or {}
    metrics = registration.metrics or {}

    _write_registered(os.path.join(out_dir, "registered.tif"),
                      registered_array, source, reference, config)

    # ISIS3 hand-off: we do not replace ISIS3, we feed it. Format-compatible PVL plus a
    # plain CSV. NOT validated by an ISIS3 binary — see samanvay/io/isis.py.
    try:
        from samanvay.io.isis import write_control_network, write_tiepoint_csv
        network_id = "samanvay_" + os.path.basename(os.path.normpath(out_dir))
        write_control_network(os.path.join(out_dir, "control_network.pvl"),
                              matches, registration, source, reference,
                              network_id=network_id)
        write_tiepoint_csv(os.path.join(out_dir, "tiepoints.csv"),
                           matches, registration, source, reference)
    except Exception:
        pass  # an export we cannot write is not a reason to lose the registration

    with open(os.path.join(out_dir, "matches.csv"), "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(MATCH_COLUMNS)
        writer.writerows(_match_rows(matches, registration))

    with open(os.path.join(out_dir, "transform.json"), "w") as f:
        json.dump(_json_safe({
            "model_type": registration.model_type,
            "params": np.asarray(registration.params, dtype=float).tolist(),
            "init_params": (None if registration.init_params is None
                            else np.asarray(registration.init_params, dtype=float).tolist()),
            # margin and ladder verdict come from the geometry stage; absent means not reported.
            "model_margin": metrics.get("model_margin"),
            "rejected_models": _ladder_verdict(metrics, registration.model_type),
        }), f, indent=4, allow_nan=False)

    with open(os.path.join(out_dir, "metrics.json"), "w") as f:
        json.dump(_json_safe(metrics), f, indent=4, allow_nan=False)

    git = _git_provenance(os.path.dirname(os.path.abspath(__file__)))
    inputs = {}
    for role, product in (("source", source), ("reference", reference)):
        entry = {"path": product.path, "size_bytes": None,
                 "product_id": product.meta.get("product_id"),
                 "meta_source": product.meta.get("meta_source")}
        try:
            entry["size_bytes"] = os.path.getsize(product.path)
        except OSError:
            pass
        inputs[role] = entry
    with open(os.path.join(out_dir, "provenance.json"), "w") as f:
        json.dump(_json_safe({
            "timestamp_utc": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "git_sha": git["sha"],
            "git_branch": git["branch"],
            "git_dirty": git["dirty"],
            "seed": config.get("seed"),
            "config": config,
            "package_versions": _package_versions(),
            "inputs": inputs,
        }), f, indent=4, allow_nan=False)

    out_html = os.path.join(out_dir, "report.html")
    try:
        from samanvay.report.render import render_report
        render_report(out_dir, source, reference, registration, matches, registered_array, config)
    except Exception:
        # ponytail: any failure in the rich report degrades to the minimal one rather than
        # losing the run. Upgrade: let the report seat surface its own error into the page.
        pass
    if not os.path.exists(out_html):
        _fallback_report(out_html, source, reference, registration, matches)
