"""Seat 2 · S2 synthetic illumination pair (pillar: exact ground truth for the sub-pixel claim).

Renders one DEM twice under two sun geometries, then maps one render through a KNOWN homography
so the pair has an analytic source -> reference transform.

The forward photometry here is deliberately independent of samanvay.photometry.shading. Rendering
a fixture with the same code the pipeline uses to *correct* illumination would make every
evaluation against that fixture circular, and it is the first thing a reviewer asks about.
Nothing in this module imports from samanvay.

Frame conventions (identical to the rest of the repo):
  - coordinates are (x, y) = (column, row), float, pixel CENTRE at integer coordinates
  - the returned homography maps SOURCE -> REFERENCE
  - sun azimuth 0 deg = up-image (-y), increasing clockwise; elevation is above the horizon
"""

import json
import os

import cv2
import numpy as np
import rasterio

from synth.terrain import fractal_surface, make_dem

# Moon 2000 sphere, simple equirectangular. Honest georeferencing rather than a borrowed Earth EPSG.
MOON_CRS = ("+proj=eqc +lat_ts=0 +lat_0=0 +lon_0=0 +x_0=0 +y_0=0 "
            "+a=1737400 +b=1737400 +units=m +no_defs")

DN_FULL_SCALE = 60000.0  # uint16 headroom; quantisation is ~1.7e-5 of full scale, i.e. irrelevant


# --------------------------------------------------------------------------- photometry (local)

def sun_vector(az_deg, el_deg):
    """Unit vector toward the sun in image axes (x=col, y=row-down, z=up); az 0 = up-image, CW."""
    a, e = np.radians(float(az_deg)), np.radians(float(el_deg))
    return np.array([np.cos(e) * np.sin(a), -np.cos(e) * np.cos(a), np.sin(e)], dtype=np.float64)


def cos_incidence(dem, gsd_m, sun_az_deg, sun_el_deg):
    """Cosine of the local solar incidence angle from DEM slopes, clipped at zero (self-shadowing)."""
    gy, gx = np.gradient(np.asarray(dem, dtype=np.float64), float(gsd_m))
    sx, sy, sz = sun_vector(sun_az_deg, sun_el_deg)
    num = -gx * sx - gy * sy + sz
    return np.clip(num / np.sqrt(gx * gx + gy * gy + 1.0), 0.0, 1.0).astype(np.float32)


def cast_shadow(dem, gsd_m, sun_az_deg, sun_el_deg, step_px=1.0, max_steps=512):
    """Ray-marched cast-shadow mask: True where terrain between a pixel and the sun occludes it."""
    dem = np.asarray(dem, dtype=np.float32)
    h, w = dem.shape
    el = np.radians(float(sun_el_deg))
    if el <= 0.0:
        return np.ones((h, w), dtype=bool)  # sun below the horizon: everything is shadow
    tan_el = float(np.tan(el))
    a = np.radians(float(sun_az_deg))
    hx, hy = float(np.sin(a)), float(-np.cos(a))
    reach_px = float(np.ptp(dem)) / max(tan_el, 1e-9) / float(gsd_m)
    n = int(min(int(max_steps), max(1, int(np.ceil(reach_px / max(step_px, 1e-6))))))
    shadow = np.zeros((h, w), dtype=bool)
    # ponytail: O(n_steps * H * W) brute-force march, ~n_steps warpAffine passes. Ceiling is fixture
    # build time (seconds, once). Upgrade path is a per-azimuth horizon-line sweep, O(H*W).
    for k in range(1, n + 1):
        d = k * float(step_px)
        m = np.float32([[1.0, 0.0, d * hx], [0.0, 1.0, d * hy]])
        sample = cv2.warpAffine(dem, m, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP,
                                borderMode=cv2.BORDER_REPLICATE)
        shadow |= sample > (dem + d * float(gsd_m) * tan_el)
    return shadow


def render_illumination(dem, gsd_m, sun_az_deg, sun_el_deg, albedo=None, ambient=0.02,
                        emission_deg=0.0, shadow_step_px=1.0, max_shadow_steps=512):
    """Lommel-Seeliger render of a DEM with ray-marched cast shadows; returns radiance/cos_i/shadow."""
    cos_i = cos_incidence(dem, gsd_m, sun_az_deg, sun_el_deg)
    cos_e = float(np.cos(np.radians(float(emission_deg))))
    shadow = cast_shadow(dem, gsd_m, sun_az_deg, sun_el_deg, shadow_step_px, max_shadow_steps)
    lit = np.where(shadow, np.float32(0.0), cos_i)
    ls = lit / np.maximum(lit + cos_e, 1e-6)  # Lommel-Seeliger, peaks at 1/(1+cos_e)
    alb = np.float32(1.0) if albedo is None else np.asarray(albedo, dtype=np.float32)
    return {"radiance": (alb * (ls + np.float32(ambient))).astype(np.float32),
            "cos_i": cos_i, "shadow": shadow}


# --------------------------------------------------------------------------- geometry

def project_points(h_mat, pts):
    """Apply a 3x3 homography to an (N,2) array of (x, y) points."""
    pts = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    q = np.concatenate([pts, np.ones((len(pts), 1))], axis=1) @ np.asarray(h_mat, np.float64).T
    return q[:, :2] / q[:, 2:3]


def _jacobian(h_mat, x, y):
    """2x2 Jacobian of a homography at one point — the best local affine approximation."""
    h_mat = np.asarray(h_mat, dtype=np.float64)
    w = h_mat[2, 0] * x + h_mat[2, 1] * y + h_mat[2, 2]
    xp, yp = project_points(h_mat, [[x, y]])[0]
    return np.array([[h_mat[0, 0] - xp * h_mat[2, 0], h_mat[0, 1] - xp * h_mat[2, 1]],
                     [h_mat[1, 0] - yp * h_mat[2, 0], h_mat[1, 1] - yp * h_mat[2, 1]]]) / w


def _src_to_master(src_shape, master_shape, rot_deg, proj_strength):
    """Homography from source pixels to master pixels: rotation + small projective, centred."""
    hs, ws = int(src_shape[0]), int(src_shape[1])
    th = np.radians(float(rot_deg))
    c, s = np.cos(th), np.sin(th)
    # projective terms normalised by image size so `proj_strength` is a fraction, not a magic epsilon
    p0 = float(proj_strength) / max(ws, 1)
    p1 = -0.6 * float(proj_strength) / max(hs, 1)
    centre = np.array([[1.0, 0.0, -(ws - 1) / 2.0], [0.0, 1.0, -(hs - 1) / 2.0], [0.0, 0.0, 1.0]])
    h0 = np.array([[c, -s, 0.0], [s, c, 0.0], [p0, p1, 1.0]]) @ centre
    corners = np.array([[0, 0], [ws - 1, 0], [ws - 1, hs - 1], [0, hs - 1]], dtype=np.float64)
    q = project_points(h0, corners)
    shift = np.array([(master_shape[1] - 1) / 2.0, (master_shape[0] - 1) / 2.0]) - 0.5 * (q.min(0) + q.max(0))
    h_mat = np.array([[1.0, 0.0, shift[0]], [0.0, 1.0, shift[1]], [0.0, 0.0, 1.0]]) @ h0
    return h_mat, (q.max(0) - q.min(0))


def _fit_source_shape(master_shape, rot_deg, proj_strength, fit_frac=0.97):
    """Largest square-ish source canvas whose projected footprint stays inside the master grid."""
    mh, mw = int(master_shape[0]), int(master_shape[1])
    hs, ws = mh, mw
    for _ in range(6):
        _, ext = _src_to_master((hs, ws), (mh, mw), rot_deg, proj_strength)
        f = min(fit_frac * mw / max(ext[0], 1e-9), fit_frac * mh / max(ext[1], 1e-9))
        if abs(f - 1.0) < 1e-3:
            break
        hs, ws = max(int(hs * f), 16), max(int(ws * f), 16)
    h_mat, _ = _src_to_master((hs, ws), (mh, mw), rot_deg, proj_strength)
    return (hs, ws), h_mat


def _master_to_ref(k):
    """Exact transform for a k-fold box decimation (cv2 INTER_AREA), pixel-centre convention."""
    k = float(k)
    off = -(k - 1.0) / (2.0 * k)
    return np.array([[1.0 / k, 0.0, off], [0.0, 1.0 / k, off], [0.0, 0.0, 1.0]])


def _gt_grid(src_shape, h_mat, n=8, margin=0.08, jitter=0.0, rng=None):
    """Analytic correspondence grid: source points and their exact homography images."""
    hs, ws = src_shape
    xs = np.linspace(margin * (ws - 1), (1.0 - margin) * (ws - 1), n)
    ys = np.linspace(margin * (hs - 1), (1.0 - margin) * (hs - 1), n)
    gx, gy = np.meshgrid(xs, ys)
    src = np.stack([gx.ravel(), gy.ravel()], axis=1)
    if jitter and rng is not None:
        src = src + rng.uniform(-jitter, jitter, src.shape)
    return src, project_points(h_mat, src)


# --------------------------------------------------------------------------- io helpers

def _write_geotiff(path, array, transform, crs=None, compress=None):
    """Write a single-band GeoTIFF with an explicit affine transform."""
    kw = {"compress": compress, "tiled": True, "blockxsize": 256, "blockysize": 256} if compress else {}
    with rasterio.open(path, "w", driver="GTiff", height=array.shape[0], width=array.shape[1],
                       count=1, dtype=str(array.dtype), crs=crs, transform=transform, **kw) as dst:
        dst.write(array, 1)


def _jsonable(obj):
    """Recursively convert numpy types to plain JSON-serialisable Python."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    return obj


def _to_dn(radiance, full_scale, gain, offset, snr_db, rng):
    """Radiance -> uint16 DN with a per-image radiometric gain/offset and Gaussian sensor noise."""
    dn = np.clip(radiance / full_scale, 0.0, None) * gain + offset
    dn = np.clip(dn, 0.0, 1.0) * DN_FULL_SCALE
    if snr_db is not None and np.isfinite(snr_db):
        rms = float(np.sqrt(np.mean(np.square(dn, dtype=np.float64))))
        sigma = rms / (10.0 ** (float(snr_db) / 20.0))
        dn = dn + rng.normal(0.0, sigma, dn.shape)
    return np.clip(np.rint(dn), 0, 65535).astype(np.uint16)


# --------------------------------------------------------------------------- the fixture

def render_synthetic_pair(out_dir="fixtures/synth_pair_A", ref_shape=(1024, 1024), scale_ratio=2,
                          ref_gsd_m=1.0, seed=0, src_sun=(45.0, 25.0), ref_sun=(135.0, 65.0),
                          rot_deg=10.0, proj_strength=0.02, fit_frac=0.97,
                          albedo_contrast=0.08, ambient=0.02, emission_deg=0.0,
                          snr_db=45.0, gain=(1.0, 0.85), offset=(0.0, 0.05),
                          geo_error_px=25.0, geo_error_rot_deg=1.0,
                          interp=cv2.INTER_CUBIC, shadow_step_px=1.0, max_shadow_steps=512,
                          gt_grid_n=8, holdout_grid_n=7, dem_kw=None):
    """Render one DEM under two suns, warp one by a known homography, and write the fixture to disk."""
    os.makedirs(out_dir, exist_ok=True)
    k = int(scale_ratio)
    if k < 1:
        raise ValueError("scale_ratio must be a positive integer")
    # ponytail: integer decimation only, so _master_to_ref is exact. Ceiling: no 1.7x scale fixture.
    # Upgrade path is warpPerspective + an explicit anti-alias prefilter for fractional ratios.
    ref_h, ref_w = int(ref_shape[0]), int(ref_shape[1])
    master_shape = (ref_h * k, ref_w * k)
    master_gsd = float(ref_gsd_m) / k  # the source's native sampling: it is the finer image

    rng = np.random.default_rng(seed)
    dem, _ = make_dem(shape=master_shape, gsd_m=master_gsd, seed=seed, **(dem_kw or {}))

    # Mild intrinsic albedo variation: real regolith is not a uniform Lambert grey, and an
    # illumination-invariant matcher must have *something* invariant to lock onto.
    albedo = None
    if albedo_contrast > 0:
        alb = fractal_surface(master_shape, beta=2.6, rng=np.random.default_rng(seed + 991))
        albedo = np.clip(1.0 + float(albedo_contrast) * alb, 0.05, None).astype(np.float32)

    ren_src = render_illumination(dem, master_gsd, src_sun[0], src_sun[1], albedo, ambient,
                                  emission_deg, shadow_step_px, max_shadow_steps)
    ren_ref = render_illumination(dem, master_gsd, ref_sun[0], ref_sun[1], albedo, ambient,
                                  emission_deg, shadow_step_px, max_shadow_steps)
    # Per-image exposure, as a real sensor's gain would be set for its own scene. A shared scale
    # would leave the high-sun render crushed into the top few percent of the DN range.
    def _expose(r):
        return max(float(np.percentile(r, 99.9)) * 1.05, 1e-6)

    fs_src, fs_ref = _expose(ren_src["radiance"]), _expose(ren_ref["radiance"])

    # ---- geometry: source -> master (rotation + projective), master -> reference (k-fold decimation)
    src_shape, h_src_master = _fit_source_shape(master_shape, rot_deg, proj_strength, fit_frac)
    h_master_ref = _master_to_ref(k)
    h_gt = h_master_ref @ h_src_master
    h_gt = h_gt / h_gt[2, 2]

    # ---- resample. Source: sample the master render at H(x,y) with a high-order kernel.
    src_img = cv2.warpPerspective(ren_src["radiance"], h_src_master, (src_shape[1], src_shape[0]),
                                  flags=interp | cv2.WARP_INVERSE_MAP,
                                  borderMode=cv2.BORDER_REPLICATE)
    # Reference: k-fold box average — what a coarser sensor's PSF plus sampling actually does.
    ref_img = cv2.resize(ren_ref["radiance"], (ref_w, ref_h), interpolation=cv2.INTER_AREA)

    src_dn = _to_dn(src_img, fs_src, gain[0], offset[0], snr_db, np.random.default_rng(seed + 101))
    ref_dn = _to_dn(ref_img, fs_ref, gain[1], offset[1], snr_db, np.random.default_rng(seed + 202))
    dem_ref = cv2.resize(dem, (ref_w, ref_h), interpolation=cv2.INTER_AREA)

    # ---- georeferencing. Reference is the anchor; the source carries a deliberately imperfect prior.
    # origin offset purely so the affine is never the (flipped) identity, which GDAL drops
    ref_tf = rasterio.transform.from_origin(0.0, ref_h * float(ref_gsd_m), ref_gsd_m, ref_gsd_m)
    j = _jacobian(h_gt, (src_shape[1] - 1) / 2.0, (src_shape[0] - 1) / 2.0)
    ctr_src = np.array([(src_shape[1] - 1) / 2.0, (src_shape[0] - 1) / 2.0])
    ctr_ref = project_points(h_gt, [ctr_src])[0]
    err_a = np.radians(float(geo_error_rot_deg))
    err_rot = np.array([[np.cos(err_a), -np.sin(err_a)], [np.sin(err_a), np.cos(err_a)]])
    err_dir = rng.uniform(0.0, 2.0 * np.pi)
    err_t = float(geo_error_px) * np.array([np.cos(err_dir), np.sin(err_dir)]) / max(k, 1)
    j_prior = err_rot @ j
    t_prior = ctr_ref + err_t - j_prior @ ctr_src
    h_prior = np.array([[j_prior[0, 0], j_prior[0, 1], t_prior[0]],
                        [j_prior[1, 0], j_prior[1, 1], t_prior[1]], [0.0, 0.0, 1.0]])
    src_tf = ref_tf @ rasterio.Affine(h_prior[0, 0], h_prior[0, 1], h_prior[0, 2],
                                      h_prior[1, 0], h_prior[1, 1], h_prior[1, 2])
    corners = np.array([[0, 0], [src_shape[1] - 1, 0],
                        [src_shape[1] - 1, src_shape[0] - 1], [0, src_shape[0] - 1]], dtype=np.float64)
    prior_err_m = float(np.max(np.linalg.norm(
        (project_points(h_prior, corners) - project_points(h_gt, corners)) * float(ref_gsd_m), axis=1)))

    # ---- sun azimuth expressed in each image's own pixel frame (see README: this is not cosmetic)
    world_dir = np.array([np.sin(np.radians(src_sun[0])), -np.cos(np.radians(src_sun[0]))])
    v = np.linalg.solve(j, world_dir)
    src_az_img = float(np.degrees(np.arctan2(v[0], -v[1])) % 360.0)

    # ---- ground truth points, computed analytically from h_gt, never by resampling
    gt_src, gt_ref = _gt_grid(src_shape, h_gt, n=gt_grid_n, margin=0.08)
    ho_src, ho_ref = _gt_grid(src_shape, h_gt, n=holdout_grid_n, margin=0.14,
                              jitter=0.35 * min(src_shape) / max(holdout_grid_n, 1),
                              rng=np.random.default_rng(seed + 7919))

    paths = {
        "source": os.path.join(out_dir, "source.tif"),
        "reference": os.path.join(out_dir, "reference.tif"),
        "dem": os.path.join(out_dir, "dem.tif"),
        "source_sidecar": os.path.join(out_dir, "source.tif.json"),
        "reference_sidecar": os.path.join(out_dir, "reference.tif.json"),
        "gt": os.path.join(out_dir, "gt.json"),
        "readme": os.path.join(out_dir, "README.md"),
    }
    _write_geotiff(paths["source"], src_dn, src_tf, MOON_CRS)
    _write_geotiff(paths["reference"], ref_dn, ref_tf, MOON_CRS)
    _write_geotiff(paths["dem"], dem_ref.astype(np.float32), ref_tf, MOON_CRS, compress="lzw")

    src_meta = {
        "product_id": "synth_source", "instrument": "SYNTH-OHRC", "synthetic": True,
        "gsd_m": master_gsd, "sun_az_deg": src_az_img, "sun_az_world_deg": float(src_sun[0]),
        "sun_az_frame": "image", "sun_el_deg": float(src_sun[1]),
        "incidence_deg": 90.0 - float(src_sun[1]), "emission_deg": float(emission_deg),
        "shape": [int(src_shape[0]), int(src_shape[1])], "dtype": "uint16",
        "geotransform": list(src_tf)[:6], "crs": MOON_CRS,
        "geotransform_exact": False, "geotransform_max_error_m": prior_err_m,
        "note": ("sun_az_deg is in THIS image's pixel frame; sun_az_world_deg is the reference/world "
                 "frame. incidence_deg/emission_deg are scene-centre values on the reference sphere, "
                 "not per-pixel. The geotransform is a deliberately imperfect prior."),
    }
    ref_meta = {
        "product_id": "synth_reference", "instrument": "SYNTH-NAC", "synthetic": True,
        "gsd_m": float(ref_gsd_m), "sun_az_deg": float(ref_sun[0]),
        "sun_az_world_deg": float(ref_sun[0]), "sun_az_frame": "image",
        "sun_el_deg": float(ref_sun[1]), "incidence_deg": 90.0 - float(ref_sun[1]),
        "emission_deg": float(emission_deg),
        "shape": [ref_h, ref_w], "dtype": "uint16",
        "geotransform": list(ref_tf)[:6], "crs": MOON_CRS,
        "geotransform_exact": True, "geotransform_max_error_m": 0.0,
        "note": "the reference defines the world frame; its geotransform is exact by construction.",
    }
    for key, meta in (("source_sidecar", src_meta), ("reference_sidecar", ref_meta)):
        with open(paths[key], "w") as fh:
            json.dump(_jsonable(meta), fh, indent=2)

    params = {
        "seed": seed, "ref_shape": [ref_h, ref_w], "src_shape": list(src_shape),
        "master_shape": list(master_shape), "scale_ratio": k, "ref_gsd_m": float(ref_gsd_m),
        "master_gsd_m": master_gsd, "src_sun_az_el": [float(src_sun[0]), float(src_sun[1])],
        "ref_sun_az_el": [float(ref_sun[0]), float(ref_sun[1])],
        "rot_deg": float(rot_deg), "proj_strength": float(proj_strength), "fit_frac": float(fit_frac),
        "albedo_contrast": float(albedo_contrast), "ambient": float(ambient),
        "emission_deg": float(emission_deg), "snr_db": snr_db,
        "gain": [float(gain[0]), float(gain[1])], "offset": [float(offset[0]), float(offset[1])],
        "geo_error_px": float(geo_error_px), "geo_error_rot_deg": float(geo_error_rot_deg),
        "interp": int(interp), "shadow_step_px": float(shadow_step_px),
        "max_shadow_steps": int(max_shadow_steps), "dn_full_scale": DN_FULL_SCALE,
        "radiance_full_scale": [fs_src, fs_ref], "dem_kw": dem_kw or {},
    }
    gt = {
        "H_src_to_ref": h_gt, "H_src_to_master": h_src_master, "H_master_to_ref": h_master_ref,
        "H_src_to_ref_prior": h_prior, "prior_max_corner_error_m": prior_err_m,
        "prior_max_corner_error_src_px": prior_err_m / master_gsd,
        "scale_src_to_ref": 1.0 / k, "rotation_deg": float(rot_deg),
        "delta_sun_az_deg": float(abs(((float(src_sun[0]) - float(ref_sun[0])) + 180.0) % 360.0 - 180.0)),
        "delta_sun_el_deg": float(src_sun[1]) - float(ref_sun[1]),
        "residual_shadow_fraction": {"source": float(ren_src["shadow"].mean()),
                                     "reference": float(ren_ref["shadow"].mean())},
        "gt_points": {"src_xy": gt_src, "ref_xy": gt_ref, "n": int(len(gt_src))},
        "gt_points_holdout": {"src_xy": ho_src, "ref_xy": ho_ref, "n": int(len(ho_src))},
        "source_sun_az_image_deg": src_az_img,
        "params": params,
        "self_measurement_note": (
            "GT points are analytic images of the source grid under H_src_to_ref; they are never "
            "read back off a resampled raster. Residual circularity remains in the pixels: the "
            "source raster was produced by cubic resampling of the master render, so a matcher is "
            "partly scored against that kernel. See README.md."),
    }
    with open(paths["gt"], "w") as fh:
        json.dump(_jsonable(gt), fh, indent=2)
    with open(paths["readme"], "w") as fh:
        fh.write(_readme(paths, gt, src_meta, ref_meta))

    return {**paths, "out_dir": out_dir, "H": h_gt, "H_prior": h_prior,
            "src_shape": tuple(src_shape), "ref_shape": (ref_h, ref_w),
            "scale_src_to_ref": 1.0 / k,
            "delta_sun_az_deg": gt["delta_sun_az_deg"]}


def _readme(paths, gt, src_meta, ref_meta):
    """Human-facing description of the fixture geometry and its residual circularity."""
    p = gt["params"]
    h = np.asarray(gt["H_src_to_ref"])
    rows = "\n".join("  [% .8e  % .8e  % .8e]" % tuple(r) for r in h)
    name = os.path.basename(os.path.abspath(os.path.dirname(paths["gt"])))
    d_az, d_el = gt["delta_sun_az_deg"], abs(gt["delta_sun_el_deg"])
    if d_az >= 30.0 or d_el >= 20.0:
        opening = (
            f"This pair is the **hard** case on purpose: {d_az:g} deg of sun-azimuth difference and "
            f"{d_el:g} deg of elevation difference. Off-the-shelf SIFT on the raw DN images finds "
            "far too few consistent correspondences here and RANSAC returns garbage. That is the "
            "problem SIH26166 poses, not a bug in the fixture, and it is the reason the pipeline "
            "has an illumination-normalisation stage at all.")
    else:
        opening = (
            f"This pair is a **mild** illumination case: {d_az:g} deg of sun-azimuth difference and "
            f"{d_el:g} deg of elevation difference. The geometric difficulty is undiminished (2x "
            "scale, rotation, projective term, noise), so if a naive matcher cannot reach sub-pixel "
            "here the problem is geometry, not illumination.")
    return f"""# Synthetic illumination pair — `{name}`

Generated by `synth/render_pair.py` (SAMANVAY seat 2, feature S2). Deterministic: seed
`{p['seed']}` reproduces every byte.

## What is in here

| file | what it is |
|---|---|
| `source.tif` | uint16, {p['src_shape'][0]}x{p['src_shape'][1]}, GSD {p['master_gsd_m']:g} m. The image to be registered. |
| `reference.tif` | uint16, {p['ref_shape'][0]}x{p['ref_shape'][1]}, GSD {p['ref_gsd_m']:g} m. The anchor / world frame. |
| `dem.tif` | float32 metres on the **reference** grid. |
| `source.tif.json`, `reference.tif.json` | metadata sidecars (`samanvay.io.loaders` merges these into `Product.meta`). |
| `gt.json` | ground-truth homography, correspondence points, every generation parameter. |

## Geometry

One DEM is rendered twice, on a *master* grid of {p['master_shape'][0]}x{p['master_shape'][1]} at
{p['master_gsd_m']:g} m/px, under two sun geometries:

* source sun: az {p['src_sun_az_el'][0]:g} deg, el {p['src_sun_az_el'][1]:g} deg
* reference sun: az {p['ref_sun_az_el'][0]:g} deg, el {p['ref_sun_az_el'][1]:g} deg
* delta azimuth {gt['delta_sun_az_deg']:g} deg, delta elevation {gt['delta_sun_el_deg']:g} deg
* cast shadow covers {gt['residual_shadow_fraction']['source'] * 100:.1f}% of the source render and
  {gt['residual_shadow_fraction']['reference'] * 100:.1f}% of the reference render

Then:

* **source** = the source-sun master render sampled through a known homography
  (rotation {p['rot_deg']:g} deg, projective strength {p['proj_strength']:g}, i.e. up to
  ~{p['proj_strength'] * 100:g}% perspective foreshortening across the frame), cubic kernel.
* **reference** = the reference-sun master render box-averaged {p['scale_ratio']}x — which is what a
  coarser sensor's PSF plus sampling physically does.

So the source is the **finer** image, as OHRC is finer than LRO NAC. The ground-truth transform
`H_src_to_ref` therefore has scale ~{gt['scale_src_to_ref']:g}: a **factor {p['scale_ratio']} scale
difference between the pair**, which is deliberate. A 1:1 fixture lets scale-fragile code look
healthy until it meets real data.

```
H_src_to_ref (source pixels -> reference pixels, (x, y) = (col, row), pixel centre at integers)
{rows}
```

`gt.json` also carries `H_src_to_master` and `H_master_to_ref` if you need the intermediate grid.
`dem.tif` is the master DEM box-averaged {p['scale_ratio']}x onto the reference grid; the render
itself used the full-resolution master DEM.

### Radiometry

Renders use Lommel-Seeliger reflectance with a ray-marched cast-shadow field, plus a small ambient
term ({p['ambient']:g}) so shadows are dark but not information-free, and a mild intrinsic albedo
field (contrast {p['albedo_contrast']:g}) so something illumination-invariant exists to match on.
The two images get different radiometric gain ({p['gain'][0]:g} vs {p['gain'][1]:g}) and offset
({p['offset'][0]:g} vs {p['offset'][1]:g}) and independent Gaussian sensor noise at
{p['snr_db']} dB SNR. DN full scale is {p['dn_full_scale']:g}.

### Sun azimuth is stored per-image, not per-world

The source image is rotated relative to the world, so a sun azimuth measured in world/reference
pixels is **not** the azimuth in source pixels. Each sidecar's `sun_az_deg` is in **that image's own
pixel frame** (`sun_az_frame: "image"`) because that is what a per-image photometric correction
needs; `sun_az_world_deg` carries the world value. Source: {src_meta['sun_az_deg']:.4f} deg image /
{src_meta['sun_az_world_deg']:g} deg world. Reference: {ref_meta['sun_az_deg']:g} deg (the reference
defines the world frame, so the two agree).

Sun *elevation* is a physical angle and is identical in both frames. `incidence_deg` and
`emission_deg` are scene-centre values on the reference sphere (`incidence = 90 - elevation`), not
per-pixel quantities — the per-pixel incidence varies with local slope and is not stored.

### Georeferencing is deliberately imperfect on the source

`reference.tif` defines the world frame; its geotransform is exact. `source.tif` carries an
**approximate** affine geotransform: the true source->reference mapping perturbed by
{p['geo_error_rot_deg']:g} deg of rotation and {p['geo_error_px']:g} source pixels of translation,
and it cannot express the projective term at all. Worst-corner error is
{gt['prior_max_corner_error_m']:.2f} m ({gt['prior_max_corner_error_src_px']:.1f} source pixels).
The sidecars say so (`geotransform_exact: false`). That residual is exactly what the registration
engine is supposed to remove — a fixture whose source is already perfectly georeferenced tests
nothing. `gt.json` stores this prior as `H_src_to_ref_prior`.

## Difficulty — read this before you conclude your matcher is broken

{opening}

The graded ramp, for bootstrapping:

* `fixtures/dsun_sweep/dsun_00` — identical illumination, **full** geometric difficulty (same 2x
  scale, same rotation, same projective term, same noise). If your geometry path is correct, plain
  SIFT plus RANSAC reaches sub-pixel here. Start here.
* `dsun_10`, `dsun_20` — still tractable naively, accuracy degrading.
* `dsun_30` and beyond — naive matching falls off a cliff. Everything past that cliff is what the
  photometric normalisation and phase-congruency path has to buy back.
* this pair — the flagship. Report it last.

Because the whole sweep shares one seed, one DEM and one homography, any difference in accuracy
across it is attributable to illumination and nothing else.

## Ground truth, and the self-measurement trap

**Read this before quoting a sub-pixel number from this fixture.**

If you generate a pair by warping an image and then measure sub-pixel accuracy against that same
warp, part of what you are measuring is your own interpolation kernel, not your matcher.

What is mitigated here:

1. **The correspondence points are analytic.** `gt_points` and `gt_points_holdout` in `gt.json` are
   source grid points pushed through `H_src_to_ref` in float64. They are never recovered by
   resampling, template matching, or any image operation, so they carry no kernel bias.
2. **The resampling is high order.** The source is produced with a cubic kernel, in the
   *magnifying* direction (master -> finer source grid), so there is no aliasing — only a mild,
   isotropic smoothing.
3. **The reference is decimated physically, not interpolated.** A {p['scale_ratio']}x box average is
   a sensor model, not a resampling artefact.
4. **A held-out point set exists.** `gt_points_holdout` uses a different grid size, a wider margin
   and a deterministic jitter, so it shares no point with `gt_points`. Tune on `gt_points`; report
   on `gt_points_holdout`.

What is **not** mitigated, stated plainly:

* The source raster is still a resampled version of the master render. Its high-frequency content
  has been shaped by the cubic kernel. A matcher that happens to use a similar interpolator when
  refining to sub-pixel will look slightly better here than on real data. Expect the real-data RMSE
  to be worse than the number this fixture gives, not better.
* Both images come from the *same* DEM and the *same* albedo field. Real pairs differ in true
  surface change, resolution-dependent detail the reference simply never saw, and per-instrument
  MTF. This fixture measures illumination and geometry robustness only.
* Shadows are ray-marched at master resolution and then resampled into the source with the image,
  rather than being re-marched on the source grid. Shadow *edges* in the source are therefore
  slightly smoother than a native render would give.

Conclusion: treat numbers from this fixture as an **upper bound** on real performance, and always
report the held-out set alongside the tuned set. Use `fixtures/dsun_sweep/` (see `synth/sweep.py`)
for the accuracy-vs-delta-sun-azimuth curve, where the *relative* trend across delta is meaningful
even where the absolute value is optimistic.

## Regenerate

```
python -m synth.render_pair            # this fixture
python -m synth.sweep                  # the delta-sun-azimuth sweep
```
"""


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Render the SAMANVAY synthetic illumination pair.")
    ap.add_argument("--out-dir", default="fixtures/synth_pair_A")
    ap.add_argument("--size", type=int, default=1024, help="reference image side in pixels")
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    info = render_synthetic_pair(out_dir=args.out_dir, ref_shape=(args.size, args.size), seed=args.seed)
    print("wrote", info["out_dir"], "source", info["src_shape"], "reference", info["ref_shape"])
    print("H_src_to_ref:\n", info["H"])
