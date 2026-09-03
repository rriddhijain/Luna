"""Seat 2 · Pillar P1 — illumination canonicalisation.

Turns a measured Product into a CanonicalImage whose `albedo` band is (as far as the
available geometry allows) independent of the sun angle it was acquired under.  This is
the whole thesis of SAMANVAY: OHRC at 70 deg incidence and LRO NAC at 20 deg incidence
are not correlatable as DN, but their albedo estimates are.

THE DEM-RESOLUTION LANDMINE, stated out loud: SLDEM2015 is ~60 m/px, OHRC is ~0.25 m/px.
You cannot render 0.25 m shading from a 60 m DEM -- anyone who tries is rendering
interpolation artefacts and calling them physics.  So there are exactly three modes, and
which one ran is recorded in params["illum_mode"]:

  "dem"         DEM supplied and within ~4x of the image GSD -> full physical render.
  "dem_lowfreq" DEM supplied but far coarser -> upsample, render, and remove only the
                LOW-FREQUENCY illumination field.  Fine structure is deliberately left
                alone; recovering it is phase congruency's job, not the DEM's.
  "empirical"   no usable DEM -> estimate the illumination field as a heavily smoothed
                version of the image itself (flat-field / homomorphic estimate) and
                divide that out.  Defensible and mandatory: there will not be a
                co-registered DEM for every pair.

THE POSE LANDMINE, which is the one that actually broke the DEM arm.  A DEM is sampled
onto the image grid through the image's own geotransform -- and that geotransform carries
exactly the real-world metadata error this project exists to correct (~36 px on the
shipped fixture, worse on real Chandrayaan-2 products).  So the rendered illumination is
misaligned with the terrain that actually made it, and dividing by it injects structured
error at precisely the spatial scale SIFT keys on.  Measured: full-resolution shading from
a wrong pose is *worse than no shading at all*.

So the scale of the correction is a second, independent choice from the mode, made on
evidence via `photometry.shading.illumination_shift_px`, and set by params["illum_scale"]:

  "auto"     (default) full-resolution render only when the pose is trustworthy --
             the caller asserted it (params["pose_trusted"], which the pipeline sets
             after its first registration pass), or supplied a corrected image->DEM
             transform, or the DEM already lands on the image grid, or the stated pose
             uncertainty is within params["pose_trust_px"].  Otherwise dem_lowfreq.
  "full"     force the full-resolution render.  Honest only when the pose is known good.
  "lowfreq"  force low-frequency removal even from a matching-resolution DEM.

Low-frequency removal is robust to tens of pixels of pose error *because* it only removes
the gross illumination gradient and the largest shadows -- structure far coarser than the
pose is wrong by -- while phase congruency, which needs no DEM and no pose at all, carries
the fine structure.  That is exactly the position docs/decisions.md D2 already takes; this
module now takes it by default instead of only when the DEM is coarse.

THE MASK-BOUNDARY LANDMINE.  The returned `albedo` is zero outside the validity mask,
which is right for a map that is consumed as radiometry.  Feeding that same array to
phase congruency was not: every mask boundary became a hard step edge, positioned by the
sun that cast the shadow, inside the map whose entire purpose is illumination invariance.
`photometry.mask_fill` (default "reflect") now fills the invalid pixels from their valid
neighbourhood for the PC input only; "zero" restores the old array exactly, so the
difference stays measurable in the ablation.  `_fill_invalid` carries the numbers.

What it is worth, measured, and the honest answer is "depends on the mask".  The fill
erases the manufactured edge completely where the invalid region is CONTIGUOUS -- one
cast shadow, 29.9% invalid: boundary PC 35.5x the interior with "zero", 1.07x with
"reflect".  That scene is tests/test_photometry.py::masked_scene at n=512, so the number
is rerunnable from this repo rather than quoted from a scratch harness.  On
fixtures/dsun_sweep, whose masks are 1.1% scattered single-pixel dark speckle rather than
regions, there is almost no manufactured edge to remove and the fit does not improve;
registering the 0-50 deg sweep both ways with match.method=rift:

    dsun   gt_rmse_px zero/reflect   inliers zero/reflect
      0      0.384 / 0.369             336 / 287
     10      0.288 / 0.431             313 / 276
     20      0.295 / 0.825             263 / 224
     30      0.950 / 1.004             199 / 181
     40      1.657 / 1.125             148 / 142
     50      1.980 / 1.887             111 /  99

Mean gt_rmse 0.926 -> 0.940 px: a wash, with 10-15% fewer inliers because the zeroed
speckle was itself a repeatable feature in a synthetic pair rendered from ONE DEM.  That
repeatability does not survive to a real pair, which is why the default stays "reflect";
but nobody should quote this sweep as evidence the fill improves accuracy, because it
does not.  The evidence for the fill is the 35.5x -> 1.07x line above.

`photometry.clahe` is off on this arm on purpose -- see the comment at the call site.

`canonicalise_with_transform(product, params, H_to_dem)` renders the illumination through
a CORRECTED image-pixel -> DEM-pixel mapping instead of the product's own geotransform,
which is what makes a two-pass loop possible: register once, recover the true transform,
re-canonicalise at the corrected pose, re-match.
"""

import os

import numpy as np
import cv2
import rasterio
from scipy.ndimage import map_coordinates

from samanvay.types import Product, CanonicalImage
from samanvay.photometry.shading import predicted_illumination, illumination_shift_px
from samanvay.photometry.mask import build_mask, MASK_VALID

_DEFAULTS = {
    "dem_path": None,
    "photometric_model": "lommel_seeliger",   # "lommel_seeliger" | "lunar_lambert" | "lambert" | "none"
    "phase_congruency": True,                 # True | False | "auto" (see _resolve_flag)
    "mask_fill": "reflect",                   # how invalid pixels enter the PC transform
    "clahe": False,                           # True | False | "auto"
    "clahe_clip": 2.0,
    "clahe_grid": 8,
    "epsilon": 1e-3,
    "smooth_sigma": None,                     # None -> derived from image size / DEM ratio
    "nscale": 4,
    "norient": 6,
    "lambert_weight": 0.0,
    "nodata_value": None,
    "dem_gsd_ratio_max": 4.0,                 # beyond this the DEM is "far coarser"
    "illum_scale": "auto",                    # "auto" | "full" | "lowfreq"
    "pose_trusted": None,                     # None -> decide from evidence
    "pose_trust_px": 2.0,                     # stated pose error this small is trusted
    "pose_uncertainty_m": None,               # overrides whatever meta says
}

_ILLUM_SCALES = ("auto", "full", "lowfreq")
_MASK_FILLS = ("reflect", "zero")
# A DEM that lands on the image grid by construction has no pose to be wrong about.
_TRUSTED_ALIGNMENTS = ("same_grid", "same_shape_no_geotransform", "corrected_transform")


def _as_gray_float(array):
    """Coerce a Product.array (ndarray or TiledReader) to a 2-D float32 image."""
    if not isinstance(array, np.ndarray):
        array = array.read_all()          # core.tiling.TiledReader
    a = np.asarray(array)
    if a.ndim == 3:
        # Band-interleaved either way round; collapse the short axis.
        a = a.mean(axis=int(np.argmin(a.shape)))
    if a.ndim == 1:
        a = a[None, :]
    if a.ndim != 2:
        raise ValueError(f"expected a 2-D image, got shape {np.shape(array)}")
    return a.astype(np.float32)


def _as_3x3(H):
    """A 3x3 float64 homography, or None if it is not one we can use."""
    if H is None:
        return None
    try:
        M = np.asarray(H, dtype=np.float64).reshape(3, 3)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(M).all() or abs(np.linalg.det(M)) < 1e-12:
        return None
    return M


def _sample_dem_by_geotransform(dem, dem_gt, image_gt, shape, H_to_dem=None):
    """Sample a DEM onto the image grid, through both geotransforms or through H_to_dem.

    A DEM and an image essentially never share a pixel grid: the DEM is a separate
    product at its own scale and origin, and our source is a differently-scaled,
    rotated view. Stretching the DEM onto the image shape assumes their footprints
    match, which misaligns the predicted illumination with the actual terrain and
    turns the P1 division into structured noise. Composing the geotransforms puts
    each image pixel where it really is on the DEM -- as well as the *stated* pose
    allows, which is the whole problem. `H_to_dem` (3x3, image pixel centres -> DEM
    pixel centres, the repo's (x, y) convention) replaces that stated pose with one
    the caller has actually recovered.
    """
    h, w = shape
    cols, rows = np.meshgrid(np.arange(w, dtype=np.float64),
                             np.arange(h, dtype=np.float64))

    M = _as_3x3(H_to_dem)
    if H_to_dem is not None and M is None:
        return None                            # caller asked for a pose we cannot use
    if M is not None:
        den = M[2, 0] * cols + M[2, 1] * rows + M[2, 2]
        den = np.where(np.abs(den) < 1e-12, np.nan, den)
        dem_row = (M[1, 0] * cols + M[1, 1] * rows + M[1, 2]) / den
        dem_col = (M[0, 0] * cols + M[0, 1] * rows + M[0, 2]) / den
        coords = np.stack([dem_row, dem_col])  # already centre-referenced
    else:
        try:
            a_img = rasterio.Affine(*[float(v) for v in image_gt][:6])
            a_dem = rasterio.Affine(*[float(v) for v in dem_gt][:6])
            img_to_dem = ~a_dem * a_img        # image pixel -> world -> DEM pixel
        except Exception:
            return None
        # Affine maps pixel CORNERS, so offset to centres and back (repo convention:
        # pixel centre sits at integer coordinates in array index space).
        dem_col = img_to_dem.a * (cols + 0.5) + img_to_dem.b * (rows + 0.5) + img_to_dem.c
        dem_row = img_to_dem.d * (cols + 0.5) + img_to_dem.e * (rows + 0.5) + img_to_dem.f
        coords = np.stack([dem_row - 0.5, dem_col - 0.5])

    finite = np.isfinite(coords).all(axis=0)
    if not finite.any():
        return None
    # Non-finite coordinates count as outside rather than poisoning the whole sample.
    coords = np.where(finite[None], coords, -1e9)

    inside = finite & ((coords[0] >= -0.5) & (coords[0] <= dem.shape[0] - 0.5) &
                       (coords[1] >= -0.5) & (coords[1] <= dem.shape[1] - 0.5))
    if inside.mean() < 0.25:
        return None                            # DEM barely covers the image: not usable

    out = map_coordinates(dem, coords, order=1, mode="nearest").astype(np.float64)
    return out, float(inside.mean())


def _load_dem(dem_path, shape, image_gsd_m, image_gt=None, H_to_dem=None):
    """Load a DEM onto the image grid; returns (dem, native_gsd_m, align_info) or (None, None, {})."""
    try:
        with rasterio.open(dem_path) as src:
            dem = src.read(1).astype(np.float64)
            nodata = src.nodata
            native_gsd = abs(float(src.transform.a)) if src.transform is not None else None
            dem_gt = list(src.transform)[:6] if src.transform is not None else None
            dem_crs = str(src.crs) if src.crs else None
    except Exception:
        return None, None, {}                  # unreadable / not a raster -> empirical

    if dem.size == 0:
        return None, None, {}
    if nodata is not None:
        dem[dem == nodata] = np.nan
    finite = np.isfinite(dem)
    if not finite.any():
        return None, None, {}                  # all-nodata DEM: does not cover anything
    # A DEM with holes still renders; fill with its own median rather than dropping it.
    dem = np.where(finite, dem, float(np.median(dem[finite])))

    if native_gsd is None or not np.isfinite(native_gsd) or native_gsd <= 0:
        native_gsd = float(image_gsd_m)        # unknown DEM scale: assume it matches

    h, w = shape
    extra = {}
    if H_to_dem is not None:
        # A recovered pose beats the stated one whatever the shapes say, so this is
        # tried first and needs no geotransform on either side.
        got = _sample_dem_by_geotransform(dem, dem_gt, image_gt, (h, w), H_to_dem=H_to_dem)
        if got is not None:
            sampled, covered = got
            return sampled, native_gsd, {"dem_align": "corrected_transform",
                                         "dem_coverage_frac": round(covered, 4),
                                         "dem_crs": dem_crs}
        # Unusable or non-covering H: say so, then fall back to the stated pose.
        extra["dem_transform_fallback"] = "supplied H_to_dem unusable or off the DEM"

    if dem.shape == (h, w) and image_gt is None:
        return dem, native_gsd, dict(extra, dem_align="same_grid")

    if image_gt is not None and dem_gt is not None:
        got = _sample_dem_by_geotransform(dem, dem_gt, image_gt, (h, w))
        if got is not None:
            sampled, covered = got
            return sampled, native_gsd, dict(extra, dem_align="geotransform",
                                             dem_coverage_frac=round(covered, 4),
                                             dem_crs=dem_crs)

    if dem.shape == (h, w):
        return dem, native_gsd, dict(extra, dem_align="same_shape_no_geotransform")

    # ponytail: last resort. Stretching assumes the DEM footprint equals the image
    # footprint; it is wrong for any real cross-product pair and is kept only so a
    # DEM without a geotransform still renders something. Ceiling is named in the
    # returned dem_align field so metrics.json shows when we fell back to it.
    dem = cv2.resize(dem.astype(np.float32), (w, h),
                     interpolation=cv2.INTER_LINEAR).astype(np.float64)
    return dem, native_gsd, dict(extra, dem_align="resize_assumed_footprint")


def _lowpass(img, sigma):
    """Wide Gaussian, computed on a decimated copy — the field is smooth by construction.

    A direct sigma=256 convolution on a 4k image takes ~15 s and adds nothing: the result
    has no detail finer than sigma anyway.
    """
    img = np.asarray(img, dtype=np.float32)
    sigma = float(sigma)
    f = max(1, int(sigma / 4.0))
    h, w = img.shape
    if f == 1 or h < 2 * f or w < 2 * f:
        return cv2.GaussianBlur(img, (0, 0), sigmaX=sigma, sigmaY=sigma,
                                borderType=cv2.BORDER_REFLECT)
    small = cv2.resize(img, (max(2, w // f), max(2, h // f)), interpolation=cv2.INTER_AREA)
    small = cv2.GaussianBlur(small, (0, 0), sigmaX=sigma / f, sigmaY=sigma / f,
                             borderType=cv2.BORDER_REFLECT)
    return cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)


def _normalised_field(lf, fallback_shape):
    """Scale a low-frequency field into (0,1]; all-ones when there is nothing to correct."""
    peak = float(np.max(lf)) if lf.size else 0.0
    if not np.isfinite(peak) or peak <= 0:
        return np.ones(fallback_shape, dtype=np.float32)   # say so honestly, do not scale
    return np.clip(lf / peak, 1e-3, 1.0).astype(np.float32)


def _empirical_illumination(img, sigma):
    """Flat-field estimate: the image's own low-frequency envelope, normalised to (0,1]."""
    finite = np.isfinite(img)
    fill = float(np.median(img[finite])) if finite.any() else 0.0
    clean = np.where(finite, img, fill).astype(np.float32)
    return _normalised_field(_lowpass(clean, sigma), clean.shape)


def _pose_policy(meta, p, align, image_gsd):
    """Can the pose that positioned this DEM be trusted to render full-resolution shading?

    Returns (trusted bool, pose_px float or None, info dict). `pose_px` is None when the
    pose uncertainty is genuinely unknown, and unknown is never quietly read as zero --
    an unknown pose is an untrusted pose, which is what sends "auto" to dem_lowfreq.
    """
    pose_px, shift_info = illumination_shift_px(meta, image_gsd, p["pose_uncertainty_m"])
    info = {
        "pose_uncertainty_px": (round(float(pose_px), 3) if pose_px is not None
                                else "unknown"),
        "pose_uncertainty_source": shift_info["source"],
    }

    def out(trusted, reason):
        return bool(trusted), pose_px, dict(info, pose_trusted_reason=reason)

    if p["pose_trusted"] is not None:
        return out(p["pose_trusted"], "asserted by caller via params['pose_trusted']")
    if align in _TRUSTED_ALIGNMENTS:
        return out(True, f"DEM sampled by {align}; no stated pose is involved")
    if pose_px is None:
        return out(False, "pose uncertainty unknown; a stated geotransform is not evidence")
    if pose_px <= float(p["pose_trust_px"]):
        return out(True, "stated pose uncertainty %.4g px <= pose_trust_px %.4g"
                   % (pose_px, float(p["pose_trust_px"])))
    return out(False, "stated pose uncertainty %.4g px > pose_trust_px %.4g"
               % (pose_px, float(p["pose_trust_px"])))


def _lowfreq_sigma(p, ratio, pose_px, trusted, shape):
    """Smoothing scale for dem_lowfreq, and the evidence it came from.

    Two independent floors, both physical: nothing finer than the DEM resolves is terrain,
    and nothing finer than the pose is wrong by is in the right place.
    """
    if p["smooth_sigma"]:
        return float(p["smooth_sigma"]), "params.smooth_sigma"
    floor = max(2.0, float(ratio))
    if trusted:
        return floor, "dem_gsd_ratio"
    if pose_px is not None:
        return max(floor, float(pose_px)), "pose_uncertainty_px"
    # ponytail: pose unknown, so fall back to the same image-fraction scale the empirical
    # estimator uses -- a stated choice, not a measurement. Ceiling: it over-smooths a
    # product whose pose is actually good. Upgrade path is for io/metadata.py to carry a
    # pose uncertainty, or for the pipeline to assert pose_trusted after pass one.
    return max(floor, 8.0, max(shape) / 16.0), "unknown_pose_image_fraction"


def _illumination(img, meta, p, H_to_dem=None):
    """Return (illum float32 in (0,1], shadow bool or None, illum_mode, info dict)."""
    h, w = img.shape
    info = {}
    dem_path = p["dem_path"]
    image_gsd = meta.get("gsd_m")
    have_sun = (meta.get("sun_az_deg") is not None and meta.get("sun_el_deg") is not None)

    if dem_path and os.path.exists(dem_path) and have_sun and image_gsd:
        dem, native_gsd, align = _load_dem(dem_path, (h, w), image_gsd,
                                           image_gt=meta.get("geotransform"),
                                           H_to_dem=H_to_dem)
        if dem is not None:
            ratio = float(native_gsd) / float(image_gsd)
            info["dem_native_gsd_m"] = float(native_gsd)
            info["dem_gsd_ratio"] = ratio
            info.update(align)
            try:
                illum = predicted_illumination(
                    dem, float(image_gsd), meta,
                    model=p["photometric_model"], lambert_weight=p["lambert_weight"])
            except ValueError:
                illum = None
            if illum is not None:
                trusted, pose_px, pose_info = _pose_policy(
                    meta, p, align.get("dem_align"), image_gsd)
                info.update(pose_info)

                scale = p["illum_scale"]
                full = (scale == "full") or (scale == "auto" and trusted)
                if full and ratio > p["dem_gsd_ratio_max"]:
                    # D2's physical gate still binds: a DEM this coarse cannot render
                    # fine shading no matter how well we know where it sits.
                    full = False
                    info["illum_scale_reason"] = (
                        "dem_gsd_ratio %.4g > dem_gsd_ratio_max %.4g"
                        % (ratio, float(p["dem_gsd_ratio_max"])))
                else:
                    info["illum_scale_reason"] = "illum_scale=%s, pose %s" % (
                        scale, "trusted" if trusted else "untrusted")

                # A shadow rendered from an untrusted pose is displaced by the same error
                # as the shading, so applying it masks the wrong pixels. Drop it and let
                # build_mask's pose-free noise-floor test find the dark ones.
                shadow = (illum <= 0.0) if trusted else None
                info["rendered_shadow_applied"] = bool(trusted)

                if full:
                    return illum, shadow, "dem", info
                sigma, basis = _lowfreq_sigma(p, ratio, pose_px, trusted, (h, w))
                info["lowfreq_sigma_px"] = float(sigma)
                info["lowfreq_sigma_basis"] = basis
                lf = _normalised_field(_lowpass(illum, sigma), illum.shape)
                return lf, shadow, "dem_lowfreq", info

    # No DEM, unreadable DEM, unknown GSD, or unknown sun geometry -> empirical.
    if dem_path and not os.path.exists(dem_path):
        info["dem_missing"] = True
    if not have_sun:
        info["sun_geometry"] = "unknown"
    if not image_gsd:
        info["gsd_m"] = "unknown"
    sigma = p["smooth_sigma"] or max(8.0, max(h, w) / 16.0)
    info["empirical_sigma_px"] = float(sigma)
    return _empirical_illumination(img, sigma), None, "empirical", info


def _resolve_flag(value, auto):
    """Resolve a tri-state config flag to a bool. "auto" -> `auto`.

    `photometry.phase_congruency` and `photometry.clahe` are "auto" in the shipped config
    and pipeline/stages resolves both from the matcher it picked before calling here --
    it is the only layer that knows which matcher won. A direct caller (the ablation, a
    test, trn.py) has no matcher, so "auto" falls back to the value that is right without
    one: compute PC (the RIFT path is the default arm), and leave CLAHE off.
    """
    if isinstance(value, str):
        v = value.strip().lower()
        if v == "auto":
            return bool(auto)
        return v in ("1", "true", "yes", "on")
    return bool(value)


def _fill_invalid(albedo, valid):
    """Fill invalid pixels from their valid neighbourhood. Returns (filled, n_filled).

    Zeroing them instead -- what this module did until now, and what mask_fill="zero"
    still does -- manufactures a hard step edge along every mask boundary, positioned by
    the sun, inside the one map whose entire purpose is illumination invariance.

    Measured here, mean PC in the 1-px band on the VALID side of the boundary divided by
    the mean over the interior (>10 px from any invalid pixel):

        one contiguous cast shadow, 29.9% invalid  zero 35.5x   reflect 1.07x
        fixtures/dsun_sweep, 1.1% invalid         zero 10.8x   reflect  9.7x

    The first is the real-NAC geometry and the whole 34.4x excess is manufactured: the
    fill removes it. The second is the synthetic sweep, whose invalid pixels are scattered
    single-pixel dark speckle rather than a region -- most of that 10.8x is genuine
    texture, and the fill can only take the 1.1x that is not. See the sweep note at the
    top of this module for what that does to the fit.

    Telea fast-marching inpainting over the invalid region. The uint8 view is deliberate:
    OpenCV 5.0.0's float32 inpaint path returns values outside the input range (measured
    -1.106 to 1.986 from a smooth [0,1] input), which would inject exactly the structure
    this exists to remove. Only the invalid pixels take the quantised value; every valid
    pixel keeps its exact float, so the residual step across the boundary is at most
    1/255 instead of the full albedo. Cost measured at 0.98 s on 3000x3000 with 40%
    invalid, against the ~11.5 s of the phase congruency it feeds.
    """
    invalid = ~valid
    n = int(np.count_nonzero(invalid))
    if n == 0:
        return albedo, 0                       # nothing to fill: 0 is the measured answer
    if n == invalid.size:
        # Every pixel is invalid, so there is no neighbourhood to fill FROM. Reporting 0
        # here would read exactly like the line above -- "the fill ran and found nothing
        # to do" -- on a frame where the fill was asked for, had `n` pixels of work, and
        # could not run. That is the plausible-default lie the repo's null rule exists to
        # stop, so mask_fill_px is null and the caller can tell the two apart.
        return albedo, None
    u8 = np.clip(albedo * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)
    filled = cv2.inpaint(u8, invalid.astype(np.uint8), 3, cv2.INPAINT_TELEA)
    out = albedo.copy()
    out[invalid] = filled[invalid].astype(np.float32) / 255.0
    return out, n


def canonicalise(product: Product, params: dict = None) -> CanonicalImage:
    """Divide out predicted illumination and return albedo + phase congruency + mask."""
    return canonicalise_with_transform(product, params, None)


def canonicalise_with_transform(product: Product, params: dict = None,
                                H_to_dem: np.ndarray = None) -> CanonicalImage:
    """canonicalise(), but rendering the illumination through a pose the caller recovered.

    `H_to_dem` is a 3x3 float64 homography mapping IMAGE pixels to DEM pixels, in the
    frozen convention: (x, y) = (column, row), pixel centre at integer coordinates. It
    replaces the product's own geotransform for the purpose of positioning the DEM, and
    nothing else -- the sun geometry, the photometric model and the mask are unchanged.

    This is what makes the two-pass loop possible: canonicalise once (the pose is unknown,
    so "auto" stays in dem_lowfreq), register, compose the recovered source->reference
    transform with reference->DEM, then call this with the result and re-match at a
    full-resolution render that actually lines up with the terrain.

    Supplying H_to_dem IS the caller's assertion that the pose is good, so it flips the
    "auto" policy to a full-resolution render; pass params["pose_trusted"]=False to
    canonicalise at the corrected pose without making that claim. With H_to_dem=None this
    is exactly canonicalise().

    A DEM the supplied transform does not cover, or a singular H, falls back to the stated
    geotransform and records `dem_transform_fallback` -- never a crash and never a silent
    substitution.
    """
    p = dict(_DEFAULTS)
    if params:
        p.update(params)
    for k in ("epsilon", "nscale", "norient", "photometric_model", "lambert_weight",
              "dem_gsd_ratio_max", "illum_scale", "pose_trust_px", "mask_fill",
              "clahe_clip", "clahe_grid"):
        if p.get(k) is None:
            p[k] = _DEFAULTS[k]
    scale_requested = str(p["illum_scale"])
    if scale_requested not in _ILLUM_SCALES:
        p["illum_scale"] = _DEFAULTS["illum_scale"]      # unknown policy: the safe one
    mask_fill = str(p["mask_fill"]).strip().lower()
    if mask_fill not in _MASK_FILLS:
        mask_fill = _DEFAULTS["mask_fill"]               # unknown policy: the safe one
    pc_on = _resolve_flag(p["phase_congruency"], auto=True)
    clahe_on = _resolve_flag(p["clahe"], auto=False)
    meta = product.meta or {}

    img = _as_gray_float(product.array)
    h, w = img.shape
    eps = float(p["epsilon"])

    if h == 0 or w == 0:
        # An empty tile is a legitimate input — a fully masked grid cell, a zero-width
        # strip off the edge of a swath — and there is no illumination to render for it.
        # Every downstream call (GaussianBlur, resize, percentile) raises on empty, so
        # the honest empty result is returned here rather than guarded five times.
        empty = np.zeros((h, w), dtype=np.float32)
        return CanonicalImage(albedo=empty, pc=empty.copy(), pc_orient=empty.copy(),
                              mask=np.zeros((h, w), dtype=np.uint8),
                              params={"illum_mode": "none", "pc_status": "skipped_empty",
                                      "shape": (h, w), "dem_path": p["dem_path"],
                                      "reason": "image has no pixels"})

    illum, shadow, illum_mode, info = _illumination(img, meta, p, H_to_dem=H_to_dem)

    nodata_value = p["nodata_value"]
    if nodata_value is None:
        nodata_value = meta.get("nodata")
    # `shadow` is already illum <= 0 from the physical render; build_mask's own illum
    # test would be the same comparison twice.
    mask = build_mask(img, shadow=shadow, nodata_value=nodata_value)

    # Guard the division BEFORE masking: a shadow or nodata pixel divided by a
    # near-zero illumination is what turns a dark corner into a 1e6 outlier.
    albedo = np.zeros((h, w), dtype=np.float32)
    # illum is 0 only where shadowed and floored above 0 everywhere else, so `> 0`
    # separates shadow from lit and eps clamps the tail.
    safe = np.isfinite(img) & np.isfinite(illum) & (illum > 0.0)
    np.divide(img, np.maximum(illum, eps), out=albedo, where=safe)
    albedo[~np.isfinite(albedo)] = 0.0

    valid = (mask == MASK_VALID) & safe
    sel = albedo[valid]
    if sel.size < 16:                      # too few valid pixels to set a scale honestly
        sel = albedo[np.isfinite(albedo)]
    if sel.size:
        lo, hi = np.percentile(sel, [2.0, 98.0])
    else:
        lo, hi = 0.0, 1.0
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        albedo = np.zeros((h, w), dtype=np.float32)
    else:
        albedo = np.clip((albedo - lo) / (hi - lo), 0.0, 1.0).astype(np.float32)

    # CLAHE, on the stretched albedo and on the 8-bit view cv2.createCLAHE takes. It is
    # off on the phase-congruency arm and that is a decision, not an oversight: the RIFT
    # descriptor reads phase, which is already contrast-invariant, so CLAHE there only
    # adds a spatially varying non-linearity and a step at every tile boundary. On the
    # intensity arms (SIFT/ORB) local contrast is what the descriptor has to work with,
    # so stages.py resolves "auto" to True there and to False here.
    clahe_applied = False
    if clahe_on and albedo.size:
        g = max(1, int(p["clahe_grid"]))
        equalised = cv2.createCLAHE(clipLimit=float(p["clahe_clip"]),
                                    tileGridSize=(g, g)).apply(
            np.clip(albedo * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8))
        albedo = (equalised.astype(np.float32) / 255.0)
        clahe_applied = True

    albedo[mask != MASK_VALID] = 0.0

    # Phase congruency does NOT see those zeros. `_fill_invalid` explains why; the short
    # version is that a zeroed mask boundary is a sun-positioned edge inside the map whose
    # job is to have no sun in it. The RETURNED albedo keeps the zeros -- the matcher, the
    # ablation and the writers all read it -- so only the transform input changes.
    pc_input, mask_fill_px = albedo, 0
    if pc_on and mask_fill == "reflect":
        pc_input, mask_fill_px = _fill_invalid(albedo, mask == MASK_VALID)

    pc = np.zeros((h, w), dtype=np.float32)
    pc_orient = np.zeros((h, w), dtype=np.float32)
    pc_status = "disabled"
    if pc_on:
        try:
            from samanvay.photometry.phasecong import phase_congruency
            out = phase_congruency(pc_input, nscale=int(p["nscale"]), norient=int(p["norient"]))
            pc = np.nan_to_num(np.asarray(out["pc"], dtype=np.float32))
            pc_orient = np.nan_to_num(np.asarray(out["orientation"], dtype=np.float32))
            pc_status = "computed"
        except ImportError:
            # Honest: PC was asked for and did not run. Do not claim zeros are a PC map.
            pc_status = "unavailable"

    out_params = {
        "illum_mode": illum_mode,
        "dem_path": p["dem_path"],
        "photometric_model": p["photometric_model"],
        "lambert_weight": p["lambert_weight"],
        "epsilon": eps,
        "smooth_sigma": p["smooth_sigma"],
        "phase_congruency": pc_on,
        "phase_congruency_requested": p["phase_congruency"],
        "mask_fill": mask_fill,
        "mask_fill_px": mask_fill_px,
        "clahe": p["clahe"],
        "clahe_applied": clahe_applied,
        "clahe_clip": p["clahe_clip"],
        "clahe_grid": p["clahe_grid"],
        "pc_status": pc_status,
        "nscale": int(p["nscale"]),
        "norient": int(p["norient"]),
        "nodata_value": nodata_value,
        "dem_gsd_ratio_max": p["dem_gsd_ratio_max"],
        "illum_scale": p["illum_scale"],
        "illum_scale_requested": scale_requested,
        "pose_trusted": p["pose_trusted"],
        "pose_trust_px": p["pose_trust_px"],
        "dem_transform": "caller_supplied" if H_to_dem is not None else "metadata_geotransform",
        "sun_az_deg": meta.get("sun_az_deg"),
        "sun_el_deg": meta.get("sun_el_deg"),
        "emission_deg": meta.get("emission_deg"),
        "gsd_m": meta.get("gsd_m"),
        "shape": (h, w),
    }
    out_params.update(info)

    return CanonicalImage(albedo=albedo, pc=pc, pc_orient=pc_orient, mask=mask,
                          params=out_params)
