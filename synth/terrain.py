"""Seat 2 · S2 synthetic fixtures — deterministic lunar DEM synthesis (pillar: honest ground truth).

Heights only, no photometry: a 1/f**beta fractal base, a crater size-frequency population
(bowl + raised rim + ejecta blanket) and a planar regional slope. Everything derives from one
seed, so a fixture regenerated on another machine is bit-identical.

Craters are what a feature matcher actually keys on under changing illumination, so they get a
real radial profile rather than a bump.
"""

import numpy as np


def fractal_surface(shape, beta=2.2, rng=None):
    """Zero-mean, unit-variance surface whose power spectrum falls as 1/f**beta."""
    h, w = int(shape[0]), int(shape[1])
    if h < 2 or w < 2:
        return np.zeros((max(h, 1), max(w, 1)), dtype=np.float64)
    rng = np.random.default_rng(0) if rng is None else rng
    fy = np.fft.fftfreq(h)[:, None]
    fx = np.fft.rfftfreq(w)[None, :]
    f = np.hypot(fy, fx)
    amp = np.zeros_like(f)
    np.divide(1.0, f ** (beta / 2.0), out=amp, where=f > 0)  # DC left at zero -> zero mean
    z = np.fft.irfft2(np.fft.rfft2(rng.standard_normal((h, w))) * amp, s=(h, w))
    z -= z.mean()
    sd = float(z.std())
    return z / sd if sd > 0 else z


def crater_diameters(n, d_min_m, d_max_m, alpha=2.0, rng=None):
    """Diameters (m) from a truncated Pareto size-frequency law N(>D) proportional to D**-alpha."""
    if n <= 0 or d_max_m <= d_min_m:
        return np.zeros(0)
    rng = np.random.default_rng(0) if rng is None else rng
    a, b = d_min_m ** -alpha, d_max_m ** -alpha
    return (a - rng.random(int(n)) * (a - b)) ** (-1.0 / alpha)


def add_craters(dem, gsd_m, n_craters, rng=None, min_diam_px=6.0, max_diam_frac=0.22,
                sfd_alpha=2.0, depth_ratio=0.15, rim_ratio=0.045, ejecta_reach=3.0):
    """Superimpose a crater population in place: parabolic bowl, raised rim, r**-3 ejecta blanket."""
    rng = np.random.default_rng(0) if rng is None else rng
    h, w = dem.shape
    d_min = float(min_diam_px) * gsd_m
    d_max = max(float(max_diam_frac) * min(h, w) * gsd_m, d_min * 2.0)
    diam = crater_diameters(n_craters, d_min, d_max, sfd_alpha, rng)
    if diam.size == 0:
        return dem
    cx = rng.uniform(-0.1 * w, 1.1 * w, diam.size)  # allow centres off-array so edges are not bare
    cy = rng.uniform(-0.1 * h, 1.1 * h, diam.size)
    fresh = rng.uniform(0.35, 1.0, diam.size)  # degradation state: old craters are shallower
    # ponytail: craters superpose additively instead of excavating the pre-existing surface.
    # Ceiling: overlapping large craters stack their rims unphysically. Upgrade path is to
    # composite with a max/replace rule inside each footprint if the stacking ever shows.
    for D, x0, y0, fr in zip(diam, cx, cy, fresh):
        r_px = 0.5 * D / gsd_m
        if r_px < 0.75:
            continue
        reach = int(np.ceil(ejecta_reach * r_px))
        xs0, xs1 = max(int(x0) - reach, 0), min(int(x0) + reach + 1, w)
        ys0, ys1 = max(int(y0) - reach, 0), min(int(y0) + reach + 1, h)
        if xs1 <= xs0 or ys1 <= ys0:
            continue
        yy, xx = np.mgrid[ys0:ys1, xs0:xs1]
        r = np.hypot(xx - x0, yy - y0) / r_px
        depth = fr * depth_ratio * D
        rim = fr * rim_ratio * D
        # bowl -> rim crest at r=1 -> ejecta decay; both branches equal `rim` at r=1, so C0 continuous
        inner = -depth * (1.0 - r * r) + rim * r ** 4
        outer = rim * np.maximum(r, 1.0) ** -3.0
        dem[ys0:ys1, xs0:xs1] += np.where(r <= 1.0, inner, outer)
    return dem


def regional_slope(shape, gsd_m, slope_deg, azimuth_deg):
    """Planar regional slope in metres, dipping toward azimuth_deg (0 = up-image, clockwise)."""
    h, w = int(shape[0]), int(shape[1])
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.radians(azimuth_deg)
    dx, dy = np.sin(a), -np.cos(a)  # downhill direction in (x=col, y=row-down)
    return -np.tan(np.radians(slope_deg)) * gsd_m * ((xx - w / 2.0) * dx + (yy - h / 2.0) * dy)


def make_dem(shape=(2048, 2048), gsd_m=0.5, seed=0, beta=2.2, rms_slope=0.12,
             crater_density_px=4.0e-3, n_craters=None, slope_deg=1.5, slope_az_deg=210.0,
             **crater_kw):
    """Deterministic lunar DEM in metres plus its GSD: fractal relief + craters + regional slope."""
    h, w = int(shape[0]), int(shape[1])
    if h < 2 or w < 2:
        return np.zeros((max(h, 1), max(w, 1)), dtype=np.float32), float(gsd_m)
    rng = np.random.default_rng(seed)
    dem = fractal_surface((h, w), beta=beta, rng=rng)
    # Normalise the fractal by its RMS slope, not its peak-to-peak relief: with beta ~2.2 the slope
    # spectrum is nearly scale-free, so a relief-normalised surface gets wildly rougher per pixel as
    # the grid grows, and the shadow fraction stops being reproducible across fixture sizes.
    gy, gx = np.gradient(dem, float(gsd_m))
    s = float(np.sqrt(np.mean(gx * gx + gy * gy)))
    dem *= (float(rms_slope) / s) if s > 0 else 0.0
    n = int(round(crater_density_px * h * w)) if n_craters is None else int(n_craters)
    if n > 0:
        add_craters(dem, float(gsd_m), n, rng, **crater_kw)
    dem += regional_slope((h, w), float(gsd_m), slope_deg, slope_az_deg)
    return dem.astype(np.float32), float(gsd_m)


if __name__ == "__main__":  # smallest runnable check: determinism + a crater actually dents
    a, g = make_dem(shape=(256, 256), gsd_m=0.5, seed=7, n_craters=40)
    b, _ = make_dem(shape=(256, 256), gsd_m=0.5, seed=7, n_craters=40)
    assert np.array_equal(a, b) and g == 0.5
    flat, _ = make_dem(shape=(256, 256), gsd_m=0.5, seed=7, n_craters=0, rms_slope=0.0, slope_deg=0.0)
    assert np.ptp(flat) < 1e-6 and np.ptp(a) > 1.0
    print("terrain ok:", a.shape, "relief %.1f m" % np.ptp(a))
