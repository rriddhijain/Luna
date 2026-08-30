"""Seat 6 (geometry) — pillar P2: metadata-first coarse initialisation.

Professional pipelines never match blind. Before a single descriptor is compared we
compose what the two products already claim about themselves — their geotransforms —
into a 3x3 affine that maps SOURCE pixels to REFERENCE pixels. The tiled matcher uses
it to decide which reference window a source tile projects into, and geometry/verify.py
uses it to gate putative matches.

Geotransform ordering (the GDAL 6-tuple, which is what this repo's metadata carries):

    gt = (x0, dx, rx, y0, ry, dy)
    x_world = x0 + dx * col + rx * row
    y_world = y0 + ry * col + dy * row

with (col, row) addressing the pixel CORNER — gt maps the upper-left corner of the
raster to (x0, y0). rasterio's Affine iterates in a DIFFERENT order,
(a, b, c, d, e, f) = (dx, rx, x0, ry, dy, y0), and both orderings currently reach us
through metadata, so `_as_gdal` sniffs which one it was handed.

This module's own convention (frozen, repo-wide) puts pixel CENTRES at integer
coordinates, so the corner-based gt is evaluated at (x + 0.5, y + 0.5); that half-pixel
is baked into the matrices below and does not cancel when the two GSDs differ.

Degradation ladder, in order — never raises, always returns something usable:
    1. "geotransform" — full composition, inv(A_ref) @ A_src.
    2. "gsd_ratio"    — scale-only from meta["gsd_m"], centred on the two images.
    3. "identity"     — nothing was known.
`coarse_init_info` reports which rung produced the matrix.
"""

import numpy as np

from samanvay.types import Product


def apply_transform(H: np.ndarray, xy: np.ndarray) -> np.ndarray:
    """Push (N,2) points through a 3x3 homogeneous matrix; returns (N,2) float64."""
    p = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    if len(p) == 0:
        return np.zeros((0, 2))
    # einsum rather than `@`: matmul routes this through BLAS, whose SIMD tail reads
    # uninitialised lanes and emits spurious divide-by-zero/overflow/invalid warnings
    # on every call. Results agree to 6e-14 (verified); this only stops the noise that
    # trains us to ignore real warnings. Same false positive as photometry/shading.py.
    M = np.asarray(H, dtype=np.float64)
    q = np.einsum("ij,nj->in", M, np.hstack([p, np.ones((len(p), 1))]))
    w = np.where(np.abs(q[2]) < 1e-12, 1e-12, q[2])
    return np.stack([q[0] / w, q[1] / w], axis=1)


def project_box(H: np.ndarray, x0: float, y0: float, x1: float, y1: float) -> tuple:
    """Axis-aligned bbox (x0, y0, x1, y1) of the four corners of a box pushed through H."""
    corners = np.array([[x0, y0], [x1, y0], [x1, y1], [x0, y1]], dtype=np.float64)
    p = apply_transform(H, corners)
    return (float(p[:, 0].min()), float(p[:, 1].min()),
            float(p[:, 0].max()), float(p[:, 1].max()))


def _as_gdal(gt):
    """Normalise a 6-element geotransform to GDAL order (x0, dx, rx, y0, ry, dy), or None."""
    if gt is None:
        return None
    try:
        g = [float(v) for v in list(gt)[:6]]
    except (TypeError, ValueError):
        return None
    if len(g) != 6 or not all(np.isfinite(g)):
        return None
    # ponytail: ordering is sniffed from the zero pattern of a north-up transform —
    # GDAL puts the rotations at [2],[4]; rasterio's Affine puts them at [1],[3].
    # Ceiling: a rotated (fully non-zero) tuple is read as GDAL without proof.
    # Upgrade path: have io/metadata.normalise_meta emit one ordering, then delete this.
    if g[1] == 0.0 and g[3] == 0.0:
        return [g[2], g[0], g[1], g[5], g[3], g[4]]
    if g[2] == 0.0 and g[4] == 0.0:
        return g
    # Fully non-zero (rotated): unambiguous only by convention. io/metadata.normalise_meta
    # is the sole producer and emits rasterio Affine order, so read it as rasterio.
    return [g[2], g[0], g[1], g[5], g[3], g[4]]


def _gt_matrix(g):
    """Pixel-CENTRE (x, y) -> world 3x3 for a GDAL 6-tuple, or None if it is singular."""
    if abs(g[1] * g[5] - g[2] * g[4]) < 1e-15:
        return None
    return np.array([
        [g[1], g[2], g[0] + 0.5 * g[1] + 0.5 * g[2]],
        [g[4], g[5], g[3] + 0.5 * g[4] + 0.5 * g[5]],
        [0.0, 0.0, 1.0],
    ], dtype=np.float64)


def _crs_equal(a, b):
    """True/False when both CRS are known, None when either is missing — never guesses."""
    if a is None or b is None:
        return None
    sa, sb = str(a).strip(), str(b).strip()
    if not sa or not sb or sa.lower() in ("none", "null") or sb.lower() in ("none", "null"):
        return None
    if sa == sb:
        return True
    try:
        from rasterio.crs import CRS
        return CRS.from_user_input(sa) == CRS.from_user_input(sb)
    except Exception:
        return False


def _shape(product):
    """(height, width) from meta["shape"] or the attached array/reader, else None."""
    meta = getattr(product, "meta", None) or {}
    for s in (meta.get("shape"), getattr(getattr(product, "array", None), "shape", None)):
        try:
            return int(list(s)[0]), int(list(s)[1])
        except (TypeError, ValueError, IndexError):
            continue
    return None


def _scale_of(H):
    """Mean linear scale (reference px per source px) implied by a 3x3."""
    return float(np.sqrt(abs(np.linalg.det(np.asarray(H, dtype=np.float64)[:2, :2]))))


def coarse_init_info(source: Product, reference: Product) -> tuple:
    """Coarse source->reference init plus the companion info naming which rung produced it."""
    info = {"method": "identity", "reason": "", "crs_match": None, "scale": None}
    try:
        smeta = getattr(source, "meta", None) or {}
        rmeta = getattr(reference, "meta", None) or {}
        info["crs_match"] = _crs_equal(smeta.get("crs"), rmeta.get("crs"))

        gs, gr = _as_gdal(smeta.get("geotransform")), _as_gdal(rmeta.get("geotransform"))
        if info["crs_match"] is False:
            why = "source and reference CRS strings differ (%r vs %r)" % (
                smeta.get("crs"), rmeta.get("crs"))
        elif gs is None or gr is None:
            why = "geotransform missing or unusable on %s" % (
                "source" if gs is None else "reference")
        else:
            A_s, A_r = _gt_matrix(gs), _gt_matrix(gr)
            if A_s is None or A_r is None:
                why = "geotransform is singular on %s" % ("source" if A_s is None else "reference")
            else:
                H = np.linalg.inv(A_r) @ A_s
                if np.isfinite(H).all() and abs(np.linalg.det(H)) > 1e-12:
                    info["method"] = "geotransform"
                    info["scale"] = _scale_of(H)
                    info["reason"] = "composed inv(reference gt) @ source gt"
                    if info["crs_match"] is None:
                        info["reason"] += "; CRS unknown on at least one side, not verified"
                    return H, info
                why = "composed geotransform is degenerate"

        # Rung 2: GSD ratio only, centred on the two images.
        gsd_s, gsd_r = smeta.get("gsd_m"), rmeta.get("gsd_m")
        try:
            gsd_s, gsd_r = float(gsd_s), float(gsd_r)
        except (TypeError, ValueError):
            gsd_s = gsd_r = 0.0
        if gsd_s > 0 and gsd_r > 0 and np.isfinite(gsd_s) and np.isfinite(gsd_r):
            s = gsd_s / gsd_r
            shp_s, shp_r = _shape(source), _shape(reference)
            if shp_s is None or shp_r is None:
                cxs = cys = cxr = cyr = 0.0
                note = "; image shape unknown, scaled about the pixel origin"
            else:
                cxs, cys = (shp_s[1] - 1) / 2.0, (shp_s[0] - 1) / 2.0
                cxr, cyr = (shp_r[1] - 1) / 2.0, (shp_r[0] - 1) / 2.0
                note = ""
            info["method"] = "gsd_ratio"
            info["scale"] = s
            info["reason"] = "%s; fell back to GSD ratio %.6g/%.6g%s" % (why, gsd_s, gsd_r, note)
            return np.array([[s, 0.0, cxr - s * cxs],
                             [0.0, s, cyr - s * cys],
                             [0.0, 0.0, 1.0]], dtype=np.float64), info

        info["reason"] = "%s; gsd_m missing or non-positive, fell back to identity" % why
        info["scale"] = 1.0
        return np.eye(3), info
    except Exception as exc:  # a bad init must never be worse than no init
        info["method"] = "identity"
        info["reason"] = "coarse_init failed (%s: %s)" % (type(exc).__name__, exc)
        info["scale"] = 1.0
        return np.eye(3), info


def coarse_init(source: Product, reference: Product) -> np.ndarray:
    """3x3 float64 affine mapping source pixels to reference pixels; never raises."""
    return coarse_init_info(source, reference)[0]
