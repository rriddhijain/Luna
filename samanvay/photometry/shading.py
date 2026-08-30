"""Seat 2 · Pillar P1 — physics-informed illumination canonicalisation (rendering half).

Renders the illumination field we *predict* from a DEM plus solar geometry, so that
the measured DN can be divided by it and what remains is close to surface albedo.
Two Chandrayaan-2 / LRO frames of the same terrain under wildly different sun angles
look nothing alike; their albedo maps do.

All arrays are image-space: axis 0 = row = y (increasing downward), axis 1 = col = x.
"""

import numpy as np

# Illumination floor for lit pixels, so predicted_illumination stays strictly in (0,1]
# and division by it can never blow up. Shadowed pixels are set to exactly 0.
_ILLUM_FLOOR = 1e-3


def _slopes(dem, gsd_m):
    """Central-difference dz/dy, dz/dx in metres-per-metre, degenerate axes give zero slope."""
    dzdy = np.gradient(dem, gsd_m, axis=0) if dem.shape[0] >= 2 else np.zeros_like(dem)
    dzdx = np.gradient(dem, gsd_m, axis=1) if dem.shape[1] >= 2 else np.zeros_like(dem)
    return np.nan_to_num(dzdy), np.nan_to_num(dzdx)


def sun_vector(sun_az_deg, sun_el_deg):
    """Unit vector pointing from the surface toward the sun, in (x, y, z) image axes.

    Azimuth convention: degrees CLOCKWISE FROM NORTH, and north is -y (image row 0 is
    the northern edge, the usual north-up raster).  So az=0 -> (0,-1), az=90 (east) ->
    (+1,0), az=180 (south) -> (0,+1), az=270 (west) -> (-1,0).  Getting this backwards
    is the classic silent failure: shading still looks plausible, it is just lit from
    the wrong side, and every downstream correlation quietly degrades.
    """
    az = np.deg2rad(float(sun_az_deg))
    el = np.deg2rad(float(sun_el_deg))
    ce = np.cos(el)
    return np.array([ce * np.sin(az), -ce * np.cos(az), np.sin(el)], dtype=np.float64)


def surface_normals(dem, gsd_m):
    """Unit surface normals (H,W,3) float32 from a DEM in metres at gsd_m metres/pixel."""
    dem = np.asarray(dem, dtype=np.float64)
    if dem.ndim != 2:
        raise ValueError(f"dem must be 2-D, got shape {dem.shape}")
    if dem.size == 0:
        return np.zeros(dem.shape + (3,), dtype=np.float32)

    gsd_m = float(gsd_m)
    if not np.isfinite(gsd_m) or gsd_m <= 0:
        raise ValueError(f"gsd_m must be finite and positive, got {gsd_m}")

    dzdy, dzdx = _slopes(dem, gsd_m)
    n = np.stack([-dzdx, -dzdy, np.ones_like(dem)], axis=-1)
    norm = np.linalg.norm(n, axis=-1, keepdims=True)
    norm[norm == 0] = 1.0
    return (n / norm).astype(np.float32)


def cos_incidence(normals, sun_az_deg, sun_el_deg):
    """Cosine of the solar incidence angle, clipped at 0 (self-shadowed slopes give 0).

    Azimuth is degrees clockwise from north with north = -y; see sun_vector().
    """
    normals = np.asarray(normals, dtype=np.float32)
    if normals.ndim != 3 or normals.shape[-1] != 3:
        raise ValueError(f"normals must be (H,W,3), got shape {normals.shape}")
    s = sun_vector(sun_az_deg, sun_el_deg).astype(np.float32)
    # Explicit dot rather than `normals @ s`: matmul routes a large float32 (H,W,3)
    # contraction through BLAS, whose SIMD tail reads uninitialised lanes and emits
    # spurious divide-by-zero/overflow/invalid warnings on every real-size image.
    # The result is bit-identical (verified); this just stops training us to ignore
    # warnings. ponytail: revisit if numpy ever fixes the false positive.
    dot = normals[..., 0] * s[0] + normals[..., 1] * s[1] + normals[..., 2] * s[2]
    return np.clip(dot, 0.0, 1.0).astype(np.float32)


def cast_shadow_mask(dem, gsd_m, sun_az_deg, sun_el_deg, max_steps=None):
    """Boolean cast-shadow mask: True where terrain upsun subtends more than the sun elevation.

    Marches along the sun azimuth in image space, one pixel per step, testing the whole
    array at every step (a per-pixel Python loop is unusably slow at OHRC sizes).
    """
    dem = np.asarray(dem, dtype=np.float64)
    if dem.ndim != 2:
        raise ValueError(f"dem must be 2-D, got shape {dem.shape}")
    h, w = dem.shape
    if h == 0 or w == 0:
        return np.zeros(dem.shape, dtype=bool)

    el = float(sun_el_deg)
    if not np.isfinite(el):
        raise ValueError("sun_el_deg must be finite")
    if el <= 0.0:
        # Sun at or below the local horizon: nothing is lit. Honest, not a guess.
        return np.ones(dem.shape, dtype=bool)

    dem = np.nan_to_num(dem, nan=float(np.nanmedian(dem)) if np.any(np.isfinite(dem)) else 0.0)
    s = sun_vector(sun_az_deg, el)
    dx, dy = float(s[0]), float(s[1])
    if dx == 0.0 and dy == 0.0:  # sun exactly at zenith
        return np.zeros(dem.shape, dtype=bool)

    # ponytail: march is capped at max_steps pixels and offsets are rounded to whole
    # pixels (<=0.5 px aliasing). Ceiling: a ridge further upsun than max_steps, or
    # outside the DEM footprint, cannot cast. Upgrade path is Dozier's horizon-angle
    # sweep (one O(N) pass per azimuth, unbounded range) if long shadows start to matter.
    if max_steps is None:
        max_steps = int(min(256, max(h, w)))
    max_steps = max(1, int(max_steps))

    tan_el = np.tan(np.deg2rad(el))
    pad = max_steps
    # Edge replication: terrain outside the DEM is treated as flat, so border pixels are
    # only shadowed by terrain we actually have. Never invents a ridge we cannot see.
    padded = np.pad(dem, pad, mode="edge")
    shadow = np.zeros(dem.shape, dtype=bool)

    for k in range(1, max_steps + 1):
        oy = int(round(k * dy))
        ox = int(round(k * dx))
        upsun = padded[pad + oy:pad + oy + h, pad + ox:pad + ox + w]
        # Elevation angle subtended by the upsun sample, seen from this pixel.
        shadow |= (upsun - dem) > (tan_el * k * float(gsd_m))

    return shadow


def lommel_seeliger(cos_i, cos_e, lambert_weight=0.0):
    """Lommel-Seeliger disk function cos_i/(cos_i+cos_e), optionally blended with Lambert.

    The Moon is not Lambertian: a plain cosine correction is the baseline this is meant to
    beat. lambert_weight in [0,1] mixes in cos_i so the team can A/B the two directly.
    """
    cos_i = np.clip(np.asarray(cos_i, dtype=np.float32), 0.0, 1.0)
    cos_e = np.clip(np.asarray(cos_e, dtype=np.float32), 0.0, 1.0)
    denom = cos_i + cos_e
    ls = np.divide(cos_i, denom, out=np.zeros_like(cos_i), where=denom > 1e-6)
    w = float(np.clip(lambert_weight, 0.0, 1.0))
    if w > 0.0:
        ls = (1.0 - w) * ls + w * cos_i
    return np.clip(ls, 0.0, 1.0).astype(np.float32)


def predicted_illumination(dem, gsd_m, meta, model="lommel_seeliger", lambert_weight=0.0,
                           max_steps=None):
    """Predicted illumination field in (0,1], exactly 0 where cast-shadowed.

    meta supplies sun_az_deg, sun_el_deg (required) and emission_deg (optional).  If the
    emission angle is unknown we drop to the Lambert term rather than inventing a viewing
    geometry.  Missing sun geometry raises: the caller must fall back to the empirical
    mode, not to a plausible default sun.

    Because lit pixels are floored above zero, `illum <= 0` is exactly the shadow mask --
    callers get it for free without a second ray march.
    """
    sun_az = meta.get("sun_az_deg")
    sun_el = meta.get("sun_el_deg")
    if sun_az is None or sun_el is None or not np.isfinite(float(sun_az)) or not np.isfinite(float(sun_el)):
        raise ValueError("sun_az_deg / sun_el_deg unknown; cannot render illumination")

    normals = surface_normals(dem, gsd_m)
    if normals.size == 0:
        return np.zeros(np.shape(dem), dtype=np.float32)

    cos_i = cos_incidence(normals, sun_az, sun_el)

    emission = meta.get("emission_deg")
    have_emission = emission is not None and np.isfinite(float(emission))

    if model == "none":
        r = np.ones_like(cos_i)
    elif model == "lambert" or not have_emission:
        r = cos_i
    else:
        cos_e = np.float32(np.cos(np.deg2rad(float(emission))))
        r = lommel_seeliger(cos_i, cos_e, lambert_weight)

    r = np.clip(r, _ILLUM_FLOOR, 1.0)
    # Self-shadow (facet turned away from the sun) is geometry, not a photometric model
    # choice, so it zeroes the field for every model including "none".
    r[cos_i <= 0.0] = 0.0
    r[cast_shadow_mask(dem, gsd_m, sun_az, sun_el, max_steps=max_steps)] = 0.0
    return r.astype(np.float32)


def illumination_shift_px(meta, gsd_m=None, pose_uncertainty_m=None):
    """Expected spatial error of a rendered illumination field, in IMAGE pixels.

    An illumination field is rendered by sampling a DEM through the image's stated pose.
    If that pose is wrong by d metres then every rendered slope and every rendered shadow
    sits d/gsd pixels away from the terrain that actually made it, and dividing the image
    by a field displaced at the scale its own features live at injects structured error
    exactly where a keypoint detector looks.  That displacement is the evidence the
    pipeline needs to choose between a full-resolution render and a low-frequency one
    (docs/decisions.md D2), so it is computed rather than guessed.

    Returns ``(shift_px, info)``.  ``shift_px`` is **None** when the pose uncertainty is
    genuinely unknown -- the common case, because most products do not state one, and
    because `io/metadata.py` currently keeps no uncertainty field.  Unknown is not zero:
    a caller that gets None must treat the render as untrustworthy at fine scales, not
    assume the metadata is right.

    Uncertainty is taken from, in order: the explicit argument, meta["pose_uncertainty_m"],
    meta["geotransform_max_error_m"], and meta["geotransform_exact"] is True (-> 0 px).

    ponytail: horizontal term only.  A vertical DEM error dz also moves a cast shadow by
    dz/tan(sun_el) metres, which dominates at low sun; add that term when a DEM actually
    ships a stated vertical accuracy (SLDEM2015 quotes one) instead of inventing it here.
    """
    meta = meta or {}
    info = {"pose_uncertainty_m": None, "source": "unknown", "gsd_m": None}

    unc, src = None, "unknown"
    for value, name in ((pose_uncertainty_m, "argument"),
                        (meta.get("pose_uncertainty_m"), "meta.pose_uncertainty_m"),
                        (meta.get("geotransform_max_error_m"), "meta.geotransform_max_error_m")):
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(v) and v >= 0.0:
            unc, src = v, name
            break
    if unc is None and meta.get("geotransform_exact") is True:
        unc, src = 0.0, "meta.geotransform_exact"

    info["pose_uncertainty_m"] = unc
    info["source"] = src
    if unc is None:
        info["reason"] = "no stated pose uncertainty; treat the render as untrusted"
        return None, info

    gsd = gsd_m if gsd_m is not None else meta.get("gsd_m")
    try:
        gsd = float(gsd)
    except (TypeError, ValueError):
        gsd = None
    if gsd is None or not np.isfinite(gsd) or gsd <= 0:
        info["reason"] = "pose uncertainty known but gsd_m is not; cannot convert to pixels"
        return None, info

    info["gsd_m"] = gsd
    return unc / gsd, info
