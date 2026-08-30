"""Seat 6 x Seat 1 · feature S1 — Terrain-Relative Navigation on the registration engine.

Pillar P2 (geometry / validation) pointed at landing rather than mosaicking: a
descending lander's camera frame is localised against an orbital basemap using the
SAME canonicalise -> match_tiled -> verify -> sub-pixel refine -> re-verify path the
registration pipeline runs, and the answer is reported as a position fix WITH a covariance, because a
navigation fix without an uncertainty is not a fix.

Three functions:

  simulate_descent_frame  crop a patch of the basemap, re-illuminate it under a
                          DIFFERENT sun (a lander arrives when it arrives), scale it
                          (altitude), rotate it, add sensor noise. Returns the frame
                          plus the TRUE centre so error is measurable.
  localise                run the engine, map the frame centre into reference pixels
                          and metres, and propagate the tie-point residual covariance
                          into a 2x2 covariance for that centre -> error ellipse.
  run_trn_demo            fly a short descent, localise every frame, write trn.json
                          and a PNG.

Conventions, frozen repo-wide and load-bearing here:
  * (x, y) = (column, row), pixel CENTRE at integer coordinates.
  * Every 3x3 maps SOURCE -> REFERENCE. Here SOURCE is the lander frame, so
    Registration.params IS the frame -> basemap map and the position fix is just
    that matrix applied to the frame centre.
  * Registration residuals/RMSE stay in SOURCE (frame) pixels. The error ellipse is
    a position covariance and therefore lives in REFERENCE pixels/metres; it is
    computed here from FORWARD residuals H(src) - ref and every returned key says
    which frame it is in. The two are different quantities, not a convention slip.
"""

import json
import math
import os
from functools import lru_cache

import cv2
import numpy as np
from scipy.stats import chi2 as _chi2

from samanvay.geometry.init import _as_gdal, _gt_matrix, apply_transform
from samanvay.geometry.refine import refine_matches
from samanvay.geometry.verify import verify_matches
from samanvay.io.loaders import load_product
# Reuse rather than re-derive: _load_dem composes the DEM and image geotransforms
# (the landmine documented in photometry/normalize.py), and _json_safe already
# knows how to turn NaN into null instead of writing invalid JSON.
from samanvay.io.writers import _json_safe
from samanvay.match.tile import match_tiled
from samanvay.photometry.normalize import _load_dem, canonicalise
from samanvay.photometry.shading import predicted_illumination
from samanvay.pipeline.config import cell_budgets, load_config
from samanvay.types import Product

# Parameter count of each model in the verify ladder — the k in "2N - k" degrees of
# freedom behind the covariance. Must agree with geometry/verify.py's ladder.
_DOF = {"similarity": 4, "affine": 6, "homography": 8}

# Below this many inliers a 2x2 covariance is a decoration, not a measurement: the
# residual scatter of 5 points estimates sigma to about +-30%, and RANSAC has already
# truncated the tail. Stated, not tuned.
_MIN_ELLIPSE_INLIERS = 8

# A lander frame is one small patch, not a mosaic tile: the uniformity grid that
# serves a 4k x 4k strip has nothing to spread across 320 px, and grid_n=1 with
# init=None is exactly "match this whole frame against the whole basemap".
_TRN_CONFIG = {
    "grid_n": 1,
    "halo_px": 0,
    "match": {"uniformity": True, "min_matches": 8, "max_matches": 600},
    "photometry": {"dem_path": None},   # a lander has no DEM registered to its own frame
}


def _frame_center(shape) -> tuple:
    """The frame's optical centre, in frame pixels. One definition, used everywhere."""
    h, w = int(shape[0]), int(shape[1])
    return (w / 2.0, h / 2.0)


def _stretch(img):
    """2-98 percentile stretch to [0,1] float32; zeros when the image has no range."""
    a = np.asarray(img, dtype=np.float32)
    finite = np.isfinite(a)
    if not finite.any():
        return np.zeros(a.shape, dtype=np.float32)
    lo, hi = np.percentile(a[finite], [2.0, 98.0])
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return np.zeros(a.shape, dtype=np.float32)
    return np.clip((np.nan_to_num(a) - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)


@lru_cache(maxsize=4)
def _reilluminated(reference_path, dem_path, sun_az, sun_el, model):
    """Basemap re-rendered under a new sun: (image [0,1], mode, info).

    With a DEM this is a real re-render: divide the measured basemap by the
    illumination predicted for the sun it was imaged under, then multiply by the
    illumination predicted for the new sun. Without one there is no physics to be
    had, so we apply a stated intensity transform and say so — `mode` is what the
    caller reports, and it never claims "dem" for a gamma curve.

    ponytail: memoised on the argument tuple so a 5-frame descent renders the
    illumination once (~1 s per render at 1024^2, cast-shadow march included).
    Ceiling: holds up to 4 basemaps in RAM. Upgrade path: pass the rendered basemap
    in explicitly if a caller ever needs more.
    """
    product = load_product(reference_path)
    meta = product.meta or {}
    img = _stretch(np.asarray(product.array, dtype=np.float64))
    gsd = meta.get("gsd_m")
    have_ref_sun = meta.get("sun_az_deg") is not None and meta.get("sun_el_deg") is not None

    if dem_path and os.path.exists(dem_path) and gsd and have_ref_sun:
        dem, native_gsd, align = _load_dem(dem_path, img.shape, float(gsd),
                                           image_gt=meta.get("geotransform"))
        if dem is not None:
            try:
                illum_ref = predicted_illumination(dem, float(gsd), meta, model=model)
                illum_new = predicted_illumination(
                    dem, float(gsd),
                    {"sun_az_deg": sun_az, "sun_el_deg": sun_el,
                     "emission_deg": meta.get("emission_deg")}, model=model)
            except ValueError:
                illum_ref = illum_new = None
            if illum_ref is not None:
                lit = illum_ref > 0.0
                albedo = np.zeros_like(img)
                np.divide(img, np.maximum(illum_ref, 1e-3), out=albedo, where=lit)
                # Shadowed reference pixels carry no albedo information. Fill with the
                # scene median rather than zero, and say how much of the frame that was.
                if lit.any():
                    albedo[~lit] = float(np.median(albedo[lit]))
                out = _stretch(albedo * illum_new)
                info = {"dem_native_gsd_m": native_gsd,
                        "ref_shadow_frac": round(float((~lit).mean()), 4)}
                info.update(align)
                return out, "dem_physical", info

    # No DEM (or no geometry to render one with): an honest intensity transform.
    # A lower sun means longer shadows and more contrast; a gamma curve plus a ramp
    # along the new azimuth reproduces the DN statistics, NOT the terrain response.
    el_ref = float(meta.get("sun_el_deg") or 45.0) if have_ref_sun else 45.0
    ratio = max(math.sin(math.radians(max(1.0, el_ref))), 1e-3) / \
        max(math.sin(math.radians(max(1.0, float(sun_el)))), 1e-3)
    gamma = float(np.clip(ratio, 0.4, 2.5))
    h, w = img.shape
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    az = math.radians(float(sun_az))
    ramp = 1.0 + 0.25 * (math.sin(az) * (xx / max(w - 1, 1) - 0.5) -
                         math.cos(az) * (yy / max(h - 1, 1) - 0.5)) * 2.0
    out = _stretch(np.power(np.clip(img, 0.0, 1.0), gamma) * ramp)
    return out, "intensity_only", {
        "gamma": gamma,
        "note": "no DEM supplied: gamma + azimuthal ramp, NOT a physical re-illumination",
        "ref_sun_el_assumed": None if have_ref_sun else 45.0,
    }


def simulate_descent_frame(reference_path, dem_path=None, center_xy=None,
                           altitude_scale=1.5, sun=(175.0, 45.0), seed=0,
                           frame_size=320, rotation_deg=6.0, noise_sigma=0.02,
                           photometric_model="lommel_seeliger") -> dict:
    """Simulate one lander camera frame over a known basemap point.

    altitude_scale is frame pixels per reference pixel: 1.0 is the basemap's own
    resolution, 2.0 is half the altitude and half the ground footprint. There is no
    focal length here, so no altitude in metres is invented — the honest statement of
    "how low" is the frame GSD, which is ref_gsd / altitude_scale.

    Returns the frame, its metadata, the TRUE centre in reference pixels, and the true
    frame -> reference matrix, so localisation error is measurable rather than asserted.
    """
    product = load_product(reference_path)
    meta = product.meta or {}
    base_h, base_w = np.asarray(product.array).shape[:2]
    sun_az, sun_el = float(sun[0]), float(sun[1])

    base, illum_mode, illum_info = _reilluminated(
        str(reference_path), str(dem_path) if dem_path else None,
        sun_az, sun_el, str(photometric_model))

    if center_xy is None:
        center_xy = _frame_center((base_h, base_w))
    cx, cy = float(center_xy[0]), float(center_xy[1])

    s = float(altitude_scale)
    if not np.isfinite(s) or s <= 0:
        raise ValueError(f"altitude_scale must be finite and positive, got {altitude_scale}")
    size = int(frame_size)
    if size < 8:
        raise ValueError(f"frame_size must be at least 8 px, got {frame_size}")

    # reference -> frame: recentre on (cx,cy), rotate, scale, put it at the frame centre.
    th = math.radians(float(rotation_deg))
    M = s * np.array([[math.cos(th), -math.sin(th)], [math.sin(th), math.cos(th)]])
    fx, fy = _frame_center((size, size))
    A = np.eye(3)
    A[:2, :2] = M
    A[:2, 2] = np.array([fx, fy]) - M @ np.array([cx, cy])

    frame = cv2.warpAffine(base, A[:2].astype(np.float64), (size, size),
                           flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                           borderValue=0.0)
    rng = np.random.default_rng(seed)
    frame = np.clip(frame + rng.normal(0.0, float(noise_sigma), frame.shape), 0.0, 1.0)
    frame = frame.astype(np.float32)

    ref_gsd = meta.get("gsd_m")
    frame_meta = {
        "product_id": f"trn_frame_s{s:g}",
        "instrument": "SIM-LANDER-CAM",
        "synthetic": True,
        # ref_gsd may legitimately be unknown; unknown stays None, never 1.0.
        "gsd_m": (float(ref_gsd) / s) if ref_gsd else None,
        "sun_az_deg": sun_az,
        "sun_el_deg": sun_el,
        "emission_deg": meta.get("emission_deg"),
        "shape": (size, size),
        "dtype": "float32",
        "geotransform": None,
        "crs": None,
        "nodata": None,
    }
    return {
        "image": frame,
        "meta": frame_meta,
        "true_center_xy": (cx, cy),
        "H_true": np.linalg.inv(A),          # frame -> reference, the repo direction
        "altitude_scale": s,
        "rotation_deg": float(rotation_deg),
        "sun_az_deg": sun_az,
        "sun_el_deg": sun_el,
        "d_sun_az_deg": (None if meta.get("sun_az_deg") is None
                         else abs(sun_az - float(meta["sun_az_deg"]))),
        "reillumination": illum_mode,
        "reillumination_info": illum_info,
        "reference_path": str(reference_path),
        "reference_gsd_m": float(ref_gsd) if ref_gsd else None,
        "frame_gsd_m": frame_meta["gsd_m"],
        "noise_sigma": float(noise_sigma),
        "seed": int(seed),
        "shape": (size, size),
    }


def _as_product(obj) -> Product:
    """Accept a Product, a simulate_descent_frame() dict, or a path — return a Product."""
    if isinstance(obj, Product):
        return obj
    if isinstance(obj, dict) and "image" in obj:
        return Product(path=str(obj.get("reference_path") or "<simulated_frame>"),
                       array=obj["image"], meta=dict(obj.get("meta") or {}))
    if isinstance(obj, str):
        return load_product(obj)
    raise TypeError(f"expected Product, frame dict or path, got {type(obj).__name__}")


def _model_jacobian(model, H, xy):
    """d(mapped point)/d(model parameters) at each point: (N, 2, k), reference px per unit.

    Parameter vectors, matching the ladder in geometry/verify.py:
      similarity  (a, b, tx, ty)      with [[a,-b,tx],[b,a,ty]]
      affine      (a, b, tx, c, d, ty)
      homography  the 8 free entries of H normalised so H[2,2] == 1
    """
    p = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
    n = len(p)
    x, y = p[:, 0], p[:, 1]
    one, zero = np.ones(n), np.zeros(n)
    if model == "similarity":
        rows_x = np.stack([x, -y, one, zero], axis=1)
        rows_y = np.stack([y, x, zero, one], axis=1)
    elif model == "affine":
        rows_x = np.stack([x, y, one, zero, zero, zero], axis=1)
        rows_y = np.stack([zero, zero, zero, x, y, one], axis=1)
    elif model == "homography":
        M = np.asarray(H, dtype=np.float64)
        if abs(M[2, 2]) < 1e-12:
            return None
        M = M / M[2, 2]
        w = M[2, 0] * x + M[2, 1] * y + 1.0
        w = np.where(np.abs(w) < 1e-12, 1e-12, w)
        mapped = apply_transform(M, p)
        X, Y = mapped[:, 0], mapped[:, 1]
        rows_x = np.stack([x / w, y / w, 1.0 / w, zero, zero, zero,
                           -X * x / w, -X * y / w], axis=1)
        rows_y = np.stack([zero, zero, zero, x / w, y / w, 1.0 / w,
                           -Y * x / w, -Y * y / w], axis=1)
    else:
        return None
    return np.stack([rows_x, rows_y], axis=1)


def position_covariance(model, H, src_xy, ref_xy, at_xy):
    """2x2 covariance (reference px^2) of `at_xy` mapped through H; (cov, dof) or (None, reason).

    Standard linearised least-squares propagation: the tie-point residual covariance S
    (2x2, reference px, from the FORWARD residuals H(src) - ref) gives the parameter
    covariance inv(sum J_i^T S^-1 J_i), which the Jacobian at the query point carries
    into a position covariance.

    ponytail: assumes the tie-point errors are independent and identically distributed
    and that the model is correct. RANSAC has already truncated the residual tail at its
    threshold, so S — and therefore the ellipse — is optimistic by roughly that
    truncation. Ceiling: it is a precision, not a guarantee of accuracy. Upgrade path:
    a bootstrap over the inlier set, or a MAD-based robust S.
    """
    src = np.asarray(src_xy, dtype=np.float64).reshape(-1, 2)
    ref = np.asarray(ref_xy, dtype=np.float64).reshape(-1, 2)
    k = _DOF.get(str(model))
    if k is None:
        return None, f"model {model!r} has no covariance parametrisation"
    n = len(src)
    dof = 2 * n - k
    if n < 2 or dof < 2:
        return None, f"{n} points against a {k}-parameter model leaves {dof} dof"

    resid = apply_transform(H, src) - ref                  # reference px, forward
    if not np.isfinite(resid).all():
        return None, "non-finite residuals"
    S = (resid.T @ resid) * 2.0 / dof                      # 2x2, unbiased under iid errors
    S = S + np.eye(2) * 1e-12                              # a perfect fit still has no zero cov
    J = _model_jacobian(model, H, src)
    Jc = _model_jacobian(model, H, np.asarray(at_xy, dtype=np.float64).reshape(1, 2))
    if J is None or Jc is None:
        return None, f"no Jacobian for model {model!r}"
    try:
        Sinv = np.linalg.inv(S)
        normal = np.einsum("nak,ab,nbl->kl", J, Sinv, J)
        cov_p = np.linalg.inv(normal)
    except np.linalg.LinAlgError:
        return None, "normal matrix is singular (degenerate point configuration)"
    cov = Jc[0] @ cov_p @ Jc[0].T
    if not np.isfinite(cov).all():
        return None, "non-finite covariance"
    return cov, dof


def error_ellipse(cov, confidence=0.95, gsd_m=None) -> dict:
    """Confidence ellipse of a 2x2 position covariance, with the chi-square factor stated."""
    evals, evecs = np.linalg.eigh(np.asarray(cov, dtype=np.float64))
    evals = np.clip(evals, 0.0, None)
    k = float(_chi2.ppf(float(confidence), 2))             # 2 dof: 95% -> 5.991
    major_vec = evecs[:, int(np.argmax(evals))]
    a = math.sqrt(k * float(evals.max()))
    b = math.sqrt(k * float(evals.min()))
    return {
        "semi_major_px": a,
        "semi_minor_px": b,
        # A PRECISION ellipse: it states how tightly the tie points pin the fix down,
        # and it has been measured to match the seed-to-seed scatter of the estimator.
        # It cannot see a systematic bias common to every tie point (resampling and
        # detector bias), so `truth_inside` below is reported, not assumed — measured
        # coverage on fixtures/synth_pair_A is 1/5 at 95%, i.e. the residual bias is
        # still ~1.5x the ellipse. ponytail: the honest fix is a bias term calibrated
        # against ground truth per scale, not a fudge factor on this covariance.
        "kind": "formal_precision",
        # Orientation of the major axis measured from +x toward +y, i.e. clockwise on a
        # north-up raster where y increases downward.
        "orientation_deg": math.degrees(math.atan2(float(major_vec[1]), float(major_vec[0]))),
        "confidence": float(confidence),
        "chi2_factor": k,
        "semi_major_m": (a * float(gsd_m)) if gsd_m else None,
        "semi_minor_m": (b * float(gsd_m)) if gsd_m else None,
        "cov_px2": np.asarray(cov, dtype=float).tolist(),
        "frame": "reference_px",
    }


def _world_xy(meta, xy):
    """Reference pixel (x, y) -> map coordinates through the reference geotransform, or None.

    Reuses geometry/init.py's reader so the half-pixel centre convention and the
    rasterio-vs-GDAL ordering sniff have exactly one implementation in the repo.
    """
    g = _as_gdal((meta or {}).get("geotransform"))
    A = _gt_matrix(g) if g is not None else None
    if A is None:
        return None
    p = apply_transform(A, [xy])[0]
    return (float(p[0]), float(p[1]))


def _deep(base, override):
    """Merge override into base, dicts recursively (the config sections must not clobber)."""
    for k, v in (override or {}).items():
        base[k] = _deep(dict(base[k]), v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return base


def localise(frame, reference, config=None, confidence=0.95) -> dict:
    """Localise one lander frame against the basemap; returns the fix and its ellipse.

    Runs the registration engine unchanged — canonicalise -> match_tiled ->
    verify_matches -> refine_matches -> verify again — so the TRN claim rests on the
    same code the registration numbers do. No second matcher exists in this file.
    """
    cfg = load_config(overrides=_deep(dict(_TRN_CONFIG), dict(config or {})))
    src_product = _as_product(frame)
    ref_product = _as_product(reference)

    src_canon = canonicalise(src_product, cfg.get("photometry"))
    ref_canon = canonicalise(ref_product, cfg.get("photometry"))

    matches, cell_info = match_tiled(
        src_canon, ref_canon,
        grid_n=int(cfg.get("grid_n", 1)), halo_px=int(cfg.get("halo_px", 0)),
        config=cfg.get("match"), cell_budgets=cell_budgets(cfg), init=None)
    reg = verify_matches(matches, cfg.get("geometry"), init=None)

    # P4, exactly as pipeline/stages.py runs it: refinement moves the points, so the
    # model is refit on the corrected set. Measured on the shipped fixture it is worth
    # a factor of ~4 in the fix (0.15 px -> 0.04 px at 1.5x): the detector's keypoint
    # bias between a resampled frame and the native basemap is the dominant error term
    # here, not the sun angle, and this is the stage that removes it.
    refined = False
    if (cfg.get("geometry") or {}).get("subpixel", True) and len(matches.src_xy) > 0:
        matches, _sigma = refine_matches(matches, src_canon, ref_canon, reg.params,
                                         cfg.get("refine"))
        reg = verify_matches(matches, cfg.get("geometry"), init=None)
        refined = True

    shape = src_canon.albedo.shape[:2]
    center = _frame_center(shape)
    ref_meta = ref_product.meta or {}
    gsd = ref_meta.get("gsd_m")
    gsd = float(gsd) if gsd else None            # unknown stays unknown: no metres, no fake

    truth = frame.get("true_center_xy") if isinstance(frame, dict) else None
    metrics = dict(reg.metrics or {})
    n_inl = int(metrics.get("inlier_count", 0))
    ok = metrics.get("status") == "ok"

    out = {
        "status": "ok" if ok else "failed",
        "reason": None if ok else metrics.get("reason"),
        "frame_center_xy": center,
        "true_center_xy": (None if truth is None else (float(truth[0]), float(truth[1]))),
        "estimated_center_xy": None,
        "estimated_center_m": None,
        "estimated_center_world_xy": None,
        "center_m_frame": ("metres along the reference grid axes from the raster origin "
                           "(x*gsd, y*gsd)" if gsd else "unknown: reference GSD not in metadata"),
        "world_crs": (ref_meta.get("crs") if _world_xy(ref_meta, (0.0, 0.0)) else None),
        "error_px": None,
        "error_m": None,
        "ellipse": None,
        "ellipse_reason": None,
        "model": metrics.get("model"),
        "inlier_count": n_inl,
        "n_matches": int(metrics.get("n_matches", 0)),
        "rmse_px": metrics.get("rmse_px"),            # SOURCE (frame) px, repo convention
        "redundancy": metrics.get("redundancy"),
        "rmse_trustworthy": bool(metrics.get("rmse_trustworthy", False)),
        "trustworthy": False,
        "reference_gsd_m": gsd,
        "gsd_source": "reference metadata" if gsd else "unknown",
        "H_frame_to_ref": np.asarray(reg.params, dtype=float).tolist(),
        "cell_info": cell_info,
        "search": "global (no navigation prior; whole basemap searched)",
        "subpixel_applied": refined,
    }
    if not ok:
        out["ellipse_reason"] = "registration failed: %s" % metrics.get("reason")
        return out

    est = apply_transform(reg.params, [center])[0]
    out["estimated_center_xy"] = (float(est[0]), float(est[1]))
    # The fix in metres, two honest readings: distance along the reference grid from its
    # origin (needs only the GSD), and true map coordinates (needs the geotransform).
    # Neither is invented when its input is missing.
    out["estimated_center_m"] = (float(est[0]) * gsd, float(est[1]) * gsd) if gsd else None
    out["estimated_center_world_xy"] = _world_xy(ref_meta, est)
    if truth is not None:
        err = float(np.hypot(est[0] - truth[0], est[1] - truth[1]))
        out["error_px"] = err
        out["error_m"] = err * gsd if gsd else None

    # A fix is trustworthy on the same redundancy logic verify.py applies to an RMSE:
    # a model supported by barely more points than it has parameters reproduces its own
    # sample, and its covariance is a decoration.
    out["trustworthy"] = bool(metrics.get("rmse_trustworthy", False)
                              and n_inl >= _MIN_ELLIPSE_INLIERS)
    if n_inl < _MIN_ELLIPSE_INLIERS:
        out["ellipse_reason"] = (
            "%d inliers is below the stated minimum of %d for a meaningful covariance"
            % (n_inl, _MIN_ELLIPSE_INLIERS))
        return out
    if not metrics.get("rmse_trustworthy", False):
        out["ellipse_reason"] = metrics.get(
            "rmse_warning", "fit has too little redundancy for a covariance")
        return out

    inl = np.asarray(reg.inliers, dtype=bool)
    cov, dof = position_covariance(reg.model_type, reg.params,
                                   matches.src_xy[inl], matches.ref_xy[inl], center)
    if cov is None:
        out["ellipse_reason"] = str(dof)
        return out
    out["ellipse"] = error_ellipse(cov, confidence=confidence, gsd_m=gsd)
    out["ellipse"]["dof"] = int(dof)
    out["ellipse"]["from_inliers"] = n_inl
    if out["error_px"] is not None:
        # Does the truth actually fall inside the stated ellipse? The one honest test
        # of a covariance: Mahalanobis distance against the same chi-square factor.
        d = np.array([est[0] - truth[0], est[1] - truth[1]], dtype=np.float64)
        try:
            m2 = float(d @ np.linalg.inv(cov) @ d)
            out["ellipse"]["mahalanobis_sq"] = m2
            out["ellipse"]["truth_inside"] = bool(m2 <= out["ellipse"]["chi2_factor"])
        except np.linalg.LinAlgError:
            out["ellipse"]["mahalanobis_sq"] = None
    return out


def run_trn_demo(reference_path, out_dir, n_frames=5, dem_path=None,
                 sun=(175.0, 45.0), altitude_scales=None, frame_size=320,
                 seed=0, confidence=0.95, config=None, track_px=60.0) -> dict:
    """Fly a short descent over the basemap, localise every frame, write trn.json + PNG."""
    os.makedirs(out_dir, exist_ok=True)
    reference = load_product(reference_path)
    base_h, base_w = np.asarray(reference.array).shape[:2]
    gsd = reference.meta.get("gsd_m") if reference.meta else None
    gsd = float(gsd) if gsd else None

    n = max(1, int(n_frames))
    if altitude_scales is None:
        # Descent: the ground footprint halves as the lander drops, so scale rises.
        altitude_scales = list(np.linspace(1.0, 2.0, n)) if n > 1 else [1.5]
    scales = [float(s) for s in altitude_scales][:n]

    cx0, cy0 = base_w / 2.0 - track_px, base_h / 2.0 - track_px
    frames = []
    for i, s in enumerate(scales):
        t = i / max(len(scales) - 1, 1)
        center = (cx0 + track_px * t, cy0 + track_px * t)
        sim = simulate_descent_frame(
            reference_path, dem_path=dem_path, center_xy=center, altitude_scale=s,
            sun=sun, seed=seed + i, frame_size=frame_size,
            rotation_deg=4.0 * (i - (len(scales) - 1) / 2.0))
        fix = localise(sim, reference, config=config, confidence=confidence)
        fix.update({
            "frame": i,
            "altitude_scale": s,
            "frame_gsd_m": sim["frame_gsd_m"],
            "rotation_deg": sim["rotation_deg"],
            "reillumination": sim["reillumination"],
            "d_sun_az_deg": sim["d_sun_az_deg"],
        })
        frames.append((sim, fix))

    errors = [f["error_m"] for _, f in frames if f["error_m"] is not None]
    localised = [f for _, f in frames if f["status"] == "ok"]
    checked = [f["ellipse"]["truth_inside"] for _, f in frames
               if f["ellipse"] and f["ellipse"].get("truth_inside") is not None]
    summary = {
        "reference": str(reference_path),
        "dem": str(dem_path) if dem_path else None,
        "n_frames": len(frames),
        "n_localised": len(localised),
        "n_trustworthy": sum(1 for f in localised if f["trustworthy"]),
        "n_with_ellipse": sum(1 for f in localised if f["ellipse"]),
        "sun_az_deg": float(sun[0]),
        "sun_el_deg": float(sun[1]),
        "reillumination": frames[0][0]["reillumination"] if frames else None,
        "reference_gsd_m": gsd,
        "confidence": float(confidence),
        # NaN, not 0.0: nothing localised means there is no error to report.
        "mean_error_m": float(np.mean(errors)) if errors else None,
        "p90_error_m": float(np.percentile(errors, 90)) if errors else None,
        "max_error_m": float(np.max(errors)) if errors else None,
        "error_units": "metres on the reference grid, from reference gsd_m"
                       if gsd else "unknown: reference GSD not in metadata",
        # The ellipse's own scorecard. It is a precision ellipse propagated from
        # tie-point scatter, so it does NOT contain a bias shared by every tie point;
        # reporting how often the truth actually landed inside it is the only honest
        # way to ship it. Under-coverage here is a measurement, not a bug to hide.
        "ellipse_checked": len(checked),
        "ellipse_truth_inside": int(sum(checked)),
        "ellipse_coverage_frac": (float(sum(checked)) / len(checked)) if checked else None,
        "ellipse_note": ("95% ellipse is formal precision from the tie-point residual "
                         "covariance; a systematic bias common to all tie points "
                         "(resampling, detector bias) is outside it by construction"),
        "frames": [f for _, f in frames],
    }

    json_path = os.path.join(out_dir, "trn.json")
    with open(json_path, "w") as handle:
        json.dump(_json_safe(summary), handle, indent=2)
    png_path = _plot(reference, frames, out_dir, summary)
    summary["json_path"] = json_path
    summary["png_path"] = png_path
    return summary


def _plot(reference, frames, out_dir, summary):
    """Basemap + track, the error ellipses at true scale, and per-frame error."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Ellipse, Polygon

    base = _stretch(np.asarray(reference.array))
    fig, (ax, ex_ax, bx) = plt.subplots(1, 3, figsize=(17, 5.6),
                                        gridspec_kw={"width_ratios": [3, 2, 2]})
    ax.imshow(base, cmap="gray", origin="upper", interpolation="nearest")
    ax.set_title("Descent over the basemap")
    ax.set_xlabel("reference x (px)")
    ax.set_ylabel("reference y (px)")

    for sim, fix in frames:
        tx, ty = sim["true_center_xy"]
        # The frame's ground footprint, shrinking as the lander descends.
        h, w = sim["shape"]
        corners = apply_transform(sim["H_true"], [[0, 0], [w, 0], [w, h], [0, h]])
        ax.add_patch(Polygon(corners, closed=True, fill=False, color="#58a6ff",
                             lw=0.8, alpha=0.7))
        ax.plot(tx, ty, "x", color="#39d353", markersize=9, markeredgewidth=2,
                label="true position" if fix["frame"] == 0 else None)
        if fix["estimated_center_xy"] is None:
            ax.annotate("frame %d: no fix" % fix["frame"], (tx, ty),
                        textcoords="offset points", xytext=(8, -12),
                        color="#ff6b6b", fontsize=8)
            continue
        ex, ey = fix["estimated_center_xy"]
        ax.plot(ex, ey, "o", mfc="none", color="#ffa657", markersize=9,
                label="estimated fix" if fix["frame"] == 0 else None)
        ax.annotate("%d" % fix["frame"], (ex, ey), textcoords="offset points",
                    xytext=(7, 6), color="#ffa657", fontsize=8)
    handles, _ = ax.get_legend_handles_labels()
    if handles:
        ax.legend(loc="upper right", fontsize=8)

    # The fixes are sub-pixel, so on the basemap the ellipses are smaller than a
    # marker. Magnifying them would be a lie; this panel replots each fix relative
    # to its own truth at the origin, where the ellipse is at TRUE scale and you can
    # see directly whether the truth falls inside it.
    gsd = summary["reference_gsd_m"]
    unit = "m" if gsd else "px"
    k = float(gsd) if gsd else 1.0
    for _, fix in frames:
        if fix["estimated_center_xy"] is None or fix["true_center_xy"] is None:
            continue
        dx = (fix["estimated_center_xy"][0] - fix["true_center_xy"][0]) * k
        dy = (fix["estimated_center_xy"][1] - fix["true_center_xy"][1]) * k
        ex_ax.plot(dx, dy, "o", color="#ffa657", markersize=6)
        ex_ax.annotate("%d" % fix["frame"], (dx, dy), textcoords="offset points",
                       xytext=(6, 4), color="#ffa657", fontsize=8)
        ell = fix["ellipse"]
        if ell:
            ex_ax.add_patch(Ellipse((dx, dy), 2 * ell["semi_major_px"] * k,
                                    2 * ell["semi_minor_px"] * k,
                                    angle=ell["orientation_deg"], fill=False,
                                    color="#58a6ff", lw=1.4))
    ex_ax.plot(0, 0, "x", color="#39d353", markersize=11, markeredgewidth=2,
               label="truth")
    ex_ax.axhline(0, color="#888", lw=0.5)
    ex_ax.axvline(0, color="#888", lw=0.5)
    ex_ax.set_aspect("equal", adjustable="datalim")
    ex_ax.invert_yaxis()                     # y grows downward, as in the raster
    ex_ax.set_xlabel(f"east/x error ({unit})")
    ex_ax.set_ylabel(f"south/y error ({unit})")
    cov = summary["ellipse_coverage_frac"]
    ex_ax.set_title("fix vs truth, %d%% ellipses (true scale)\ntruth inside: %s" % (
        round(100 * summary["confidence"]),
        "n/a" if cov is None else "%d/%d" % (summary["ellipse_truth_inside"],
                                             summary["ellipse_checked"])),
        fontsize=10)
    ex_ax.legend(loc="best", fontsize=8)

    idx = [f["frame"] for _, f in frames]
    err = [(f["error_m"] if f["error_m"] is not None else np.nan) for _, f in frames]
    bx.bar(idx, err, color=["#58a6ff" if f["trustworthy"] else "#ff6b6b"
                            for _, f in frames])
    for i, (_, f) in enumerate(frames):
        if f["error_m"] is None:
            bx.annotate("no fix", (idx[i], 0), ha="center", va="bottom",
                        color="#ff6b6b", fontsize=8)
    unit = "m" if summary["reference_gsd_m"] else "m (GSD unknown)"
    bx.set_xlabel("descent frame (altitude decreasing ->)")
    bx.set_ylabel(f"localisation error ({unit})")
    mean_m, p90_m = summary["mean_error_m"], summary["p90_error_m"]
    bx.set_title("mean %s · p90 %s · %d/%d localised" % (
        "n/a" if mean_m is None else "%.2f m" % mean_m,
        "n/a" if p90_m is None else "%.2f m" % p90_m,
        summary["n_localised"], summary["n_frames"]))
    bx.set_xticks(idx)
    bx.grid(axis="y", alpha=0.3)

    fig.tight_layout()
    path = os.path.join(out_dir, "trn.png")
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path
