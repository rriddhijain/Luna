"""Seat 2 · Pillar P1 tests — does dividing out predicted illumination actually work?

The load-bearing test is test_canonicalise_beats_raw_correlation: it renders the same
terrain and the same true albedo under two opposed sun azimuths and asserts the
canonicalised pair is far more correlated than the raw pair. That claim is the project.
"""

import numpy as np
import pytest
import rasterio
from rasterio import Affine

from samanvay.types import Product
from samanvay.photometry.shading import (
    surface_normals, cos_incidence, cast_shadow_mask, lommel_seeliger,
    predicted_illumination, sun_vector,
)
from samanvay.photometry.mask import (
    build_mask, MASK_VALID, MASK_SHADOW, MASK_NODATA, MASK_SATURATED,
)
from samanvay.photometry.normalize import canonicalise

N = 128
GSD = 10.0


def hill_dem(n=N, height=400.0, sigma_px=22.0):
    """A single Gaussian hill: smooth, steep enough to cast real shadows at low sun."""
    y, x = np.mgrid[0:n, 0:n].astype(np.float64)
    c = (n - 1) / 2.0
    r2 = (x - c) ** 2 + (y - c) ** 2
    return height * np.exp(-r2 / (2.0 * sigma_px ** 2))


def true_albedo(n=N, seed=0):
    """High-frequency ground truth albedo: speckle plus a couple of bright/dark patches."""
    rng = np.random.default_rng(seed)
    a = 0.15 + 0.10 * rng.random((n, n))
    a[20:45, 20:45] += 0.10
    a[70:100, 30:60] -= 0.05
    return np.clip(a, 0.02, 0.6).astype(np.float64)


def meta_for(az, el, emission=8.0, gsd=GSD):
    return {"gsd_m": gsd, "sun_az_deg": az, "sun_el_deg": el,
            "emission_deg": emission, "product_id": f"az{az}"}


def render(dem, albedo, az, el, gsd=GSD):
    """Synthetic DN: true albedo modulated by the physically predicted illumination."""
    illum = predicted_illumination(dem, gsd, meta_for(az, el, gsd=gsd))
    return (albedo * illum).astype(np.float32)


def write_dem(path, dem, gsd):
    with rasterio.open(path, "w", driver="GTiff", height=dem.shape[0], width=dem.shape[1],
                       count=1, dtype="float32",
                       transform=Affine(gsd, 0.0, 0.0, 0.0, -gsd, 0.0)) as dst:
        dst.write(dem.astype(np.float32), 1)
    return str(path)


def corr(a, b, sel):
    """Pearson correlation over the selected pixels, 0.0 if either side is constant."""
    x = np.asarray(a, dtype=np.float64)[sel]
    y = np.asarray(b, dtype=np.float64)[sel]
    if x.size < 2 or x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


# --------------------------------------------------------------------------- (a)

def test_shadow_mask_moves_when_azimuth_flips():
    dem = hill_dem()
    east = cast_shadow_mask(dem, GSD, 90.0, 12.0)     # sun in the east
    west = cast_shadow_mask(dem, GSD, 270.0, 12.0)    # sun in the west

    assert east.any() and west.any(), "a 400 m hill at 12 deg sun must cast something"
    # Shadow falls away from the sun: east sun -> west-side shadow, and vice versa.
    cx_east = np.mean(np.nonzero(east)[1])
    cx_west = np.mean(np.nonzero(west)[1])
    assert cx_east < cx_west, f"shadow centroids did not swap sides ({cx_east}, {cx_west})"
    overlap = np.count_nonzero(east & west) / max(1, np.count_nonzero(east | west))
    assert overlap < 0.25, f"shadow masks barely moved (overlap {overlap:.2f})"


def test_shadow_mask_degenerates_honestly():
    dem = hill_dem(n=32)
    assert cast_shadow_mask(dem, GSD, 90.0, -5.0).all()           # sun below horizon
    assert not cast_shadow_mask(np.zeros((16, 16)), GSD, 90.0, 30.0).any()  # flat plane
    assert cast_shadow_mask(np.zeros((0, 0)), GSD, 90.0, 30.0).shape == (0, 0)


# --------------------------------------------------------------------------- (b)

def test_cos_incidence_is_highest_on_slopes_facing_the_sun():
    dem = hill_dem()
    n = surface_normals(dem, GSD)
    assert n.shape == (N, N, 3)
    np.testing.assert_allclose(np.linalg.norm(n, axis=-1), 1.0, atol=1e-5)

    c = (N - 1) / 2.0
    x = np.mgrid[0:N, 0:N][1]
    east_face = x > c + 5          # normals point +x
    west_face = x < c - 5          # normals point -x

    ci_east_sun = cos_incidence(n, 90.0, 30.0)     # sun to the east (+x)
    assert ci_east_sun[east_face].mean() > ci_east_sun[west_face].mean()

    ci_west_sun = cos_incidence(n, 270.0, 30.0)    # sun to the west (-x)
    assert ci_west_sun[west_face].mean() > ci_west_sun[east_face].mean()

    # Azimuth convention: clockwise from north, north = -y.
    np.testing.assert_allclose(sun_vector(0.0, 0.0), [0.0, -1.0, 0.0], atol=1e-9)
    np.testing.assert_allclose(sun_vector(90.0, 0.0), [1.0, 0.0, 0.0], atol=1e-9)
    # Flat ground: cos_i is sin(elevation), independent of azimuth.
    flat = surface_normals(np.zeros((8, 8)), GSD)
    np.testing.assert_allclose(cos_incidence(flat, 137.0, 30.0), 0.5, atol=1e-5)


def test_lommel_seeliger_is_not_lambert_and_is_guarded():
    cos_i = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
    ls = lommel_seeliger(cos_i, np.float32(0.5))
    np.testing.assert_allclose(ls, [[0.0, 0.5, 2.0 / 3.0]], atol=1e-6)
    # cos_i = cos_e = 0 must not divide by zero.
    assert np.isfinite(lommel_seeliger(np.zeros((4, 4)), 0.0)).all()
    # The blend knob reaches plain Lambert, which is the baseline we must beat.
    np.testing.assert_allclose(lommel_seeliger(cos_i, np.float32(0.5), lambert_weight=1.0),
                               cos_i, atol=1e-6)


# --------------------------------------------------------------------------- (c)

def test_canonicalise_beats_raw_correlation(tmp_path):
    """THE thesis: opposed sun azimuths correlate poorly raw, well after canonicalisation."""
    dem = hill_dem()
    alb = true_albedo()
    az1, az2, el = 45.0, 225.0, 25.0
    img1, img2 = render(dem, alb, az1, el), render(dem, alb, az2, el)

    dem_path = write_dem(tmp_path / "dem.tif", dem, GSD)
    p = {"dem_path": dem_path, "phase_congruency": False}
    c1 = canonicalise(Product(str(tmp_path / "a.tif"), img1, meta_for(az1, el)), p)
    c2 = canonicalise(Product(str(tmp_path / "b.tif"), img2, meta_for(az2, el)), p)

    assert c1.params["illum_mode"] == "dem"
    assert c2.params["illum_mode"] == "dem"

    sel = (c1.mask == MASK_VALID) & (c2.mask == MASK_VALID)
    assert sel.sum() > 1000, "not enough commonly-valid pixels to judge"

    raw = corr(img1, img2, sel)
    canon = corr(c1.albedo, c2.albedo, sel)
    assert canon > 0.9, f"canonicalised pair should be near-identical, got {canon:.3f}"
    assert canon > raw + 0.3, f"no improvement: raw={raw:.3f} canon={canon:.3f}"


def test_empirical_mode_also_improves_correlation(tmp_path):
    """No DEM is the common case; the flat-field fallback must still earn its place."""
    dem = hill_dem()
    alb = true_albedo()
    az1, az2, el = 45.0, 225.0, 25.0
    img1, img2 = render(dem, alb, az1, el), render(dem, alb, az2, el)

    p = {"phase_congruency": False}
    c1 = canonicalise(Product("a.tif", img1, meta_for(az1, el)), p)
    c2 = canonicalise(Product("b.tif", img2, meta_for(az2, el)), p)
    assert c1.params["illum_mode"] == "empirical"

    sel = (c1.mask == MASK_VALID) & (c2.mask == MASK_VALID)
    raw = corr(img1, img2, sel)
    canon = corr(c1.albedo, c2.albedo, sel)
    assert canon > raw + 0.15, f"empirical fallback did not help: raw={raw:.3f} canon={canon:.3f}"


def test_coarse_dem_selects_lowfreq_mode(tmp_path):
    """SLDEM2015 vs OHRC: an 8x coarser DEM must not pretend to render fine shading."""
    dem = hill_dem()
    img = render(dem, true_albedo(), 45.0, 25.0)
    coarse = dem[::8, ::8]
    dem_path = write_dem(tmp_path / "coarse.tif", coarse, GSD * 8)

    c = canonicalise(Product("a.tif", img, meta_for(45.0, 25.0)),
                     {"dem_path": dem_path, "phase_congruency": False})
    assert c.params["illum_mode"] == "dem_lowfreq"
    assert c.params["dem_gsd_ratio"] == pytest.approx(8.0)
    assert np.isfinite(c.albedo).all()

    # A DEM at matching resolution takes the full physical path instead.
    fine_path = write_dem(tmp_path / "fine.tif", dem, GSD)
    c2 = canonicalise(Product("a.tif", img, meta_for(45.0, 25.0)),
                      {"dem_path": fine_path, "phase_congruency": False})
    assert c2.params["illum_mode"] == "dem"


def test_missing_or_broken_dem_falls_back_not_crashes(tmp_path):
    img = render(hill_dem(), true_albedo(), 45.0, 25.0)
    prod = Product("a.tif", img, meta_for(45.0, 25.0))

    c = canonicalise(prod, {"dem_path": str(tmp_path / "nope.tif"), "phase_congruency": False})
    assert c.params["illum_mode"] == "empirical" and c.params["dem_missing"] is True

    junk = tmp_path / "junk.tif"
    junk.write_bytes(b"not a geotiff")
    c = canonicalise(prod, {"dem_path": str(junk), "phase_congruency": False})
    assert c.params["illum_mode"] == "empirical"

    # DEM present but sun geometry unknown -> must not invent a sun.
    no_sun = dict(meta_for(45.0, 25.0))
    no_sun["sun_az_deg"] = None
    no_sun["sun_el_deg"] = None
    dem_path = write_dem(tmp_path / "dem.tif", hill_dem(), GSD)
    c = canonicalise(Product("a.tif", img, no_sun),
                     {"dem_path": dem_path, "phase_congruency": False})
    assert c.params["illum_mode"] == "empirical"
    assert c.params["sun_geometry"] == "unknown"


def test_missing_sun_geometry_raises_rather_than_guessing():
    with pytest.raises(ValueError):
        predicted_illumination(hill_dem(n=16), GSD, {"sun_az_deg": None, "sun_el_deg": 20.0})


# --------------------------------------------------------------------------- (d)

def test_every_mask_code_is_reachable():
    img = np.full((8, 8), 0.5, dtype=np.float32)
    img[0, 0] = 1.0              # dtype ceiling for [0,1] float -> saturated
    img[1, 1] = np.nan           # non-finite -> nodata
    img[2, 2] = -9999.0          # explicit sentinel -> nodata
    img[3, 3] = 0.0              # noise floor -> shadow
    shadow = np.zeros((8, 8), bool)
    shadow[4, 4] = True          # predicted cast shadow

    m = build_mask(img, nodata_value=-9999.0, shadow=shadow)
    assert m[0, 0] == MASK_SATURATED
    assert m[1, 1] == MASK_NODATA
    assert m[2, 2] == MASK_NODATA
    assert m[3, 3] == MASK_SHADOW
    assert m[4, 4] == MASK_SHADOW
    assert m[6, 6] == MASK_VALID
    assert set(np.unique(m)) == {MASK_VALID, MASK_SHADOW, MASK_NODATA, MASK_SATURATED}

    # illum <= 0 is an equivalent shadow source.
    illum = np.ones((8, 8), np.float32)
    illum[5, 5] = 0.0
    assert build_mask(np.full((8, 8), 0.5, np.float32), illum=illum)[5, 5] == MASK_SHADOW

    # Integer imagery: ceiling from the dtype, not from the observed max.
    u = np.full((4, 4), 100, dtype=np.uint8)
    u[0, 0] = 255
    mu = build_mask(u)
    assert mu[0, 0] == MASK_SATURATED
    assert build_mask(np.zeros((0, 0), np.float32)).shape == (0, 0)


def test_canonicalise_mask_codes_flow_through(tmp_path):
    dem = hill_dem()
    img = render(dem, true_albedo(), 45.0, 12.0).astype(np.float32)
    img[0:4, 0:4] = np.nan
    dem_path = write_dem(tmp_path / "dem.tif", dem, GSD)
    c = canonicalise(Product("a.tif", img, meta_for(45.0, 12.0)),
                     {"dem_path": dem_path, "phase_congruency": False})
    assert (c.mask[0:4, 0:4] == MASK_NODATA).all()
    assert (c.mask == MASK_SHADOW).any(), "a 12 deg sun on a 400 m hill must give shadow"
    assert (c.mask == MASK_VALID).any()
    assert (c.albedo[c.mask != MASK_VALID] == 0).all(), "invalid pixels must not carry signal"


# --------------------------------------------------------------------------- (e)

@pytest.mark.parametrize("bad", [
    np.full((32, 32), np.nan, dtype=np.float32),          # all nodata
    np.zeros((32, 32), dtype=np.float32),                 # all zero
    np.full((32, 32), 7.0, dtype=np.float32),             # constant
    np.ones((1, 1), dtype=np.float32),                    # single pixel
    np.ones((2, 2), dtype=np.float32),                    # smaller than any kernel
    np.array([[1.0, np.inf], [-np.inf, 2.0]], np.float32),
])
def test_canonicalise_never_returns_non_finite(bad):
    c = canonicalise(Product("x.tif", bad, meta_for(45.0, 25.0)), {"phase_congruency": False})
    for name in ("albedo", "pc", "pc_orient"):
        arr = getattr(c, name)
        assert arr.shape == bad.shape
        assert np.isfinite(arr).all(), f"{name} went non-finite"
    assert c.albedo.min() >= 0.0 and c.albedo.max() <= 1.0
    assert c.mask.dtype == np.uint8


def test_canonicalise_on_real_render_is_finite_and_reproducible(tmp_path):
    dem = hill_dem()
    img = render(dem, true_albedo(), 45.0, 25.0)
    dem_path = write_dem(tmp_path / "dem.tif", dem, GSD)
    p = {"dem_path": dem_path, "phase_congruency": False, "epsilon": 1e-3}
    prod = Product("a.tif", img, meta_for(45.0, 25.0))

    c1 = canonicalise(prod, p)
    c2 = canonicalise(prod, p)
    np.testing.assert_array_equal(c1.albedo, c2.albedo)
    assert np.isfinite(c1.albedo).all()
    assert c1.albedo.dtype == np.float32 and c1.pc.dtype == np.float32
    for key in ("illum_mode", "dem_path", "epsilon", "photometric_model",
                "phase_congruency", "pc_status", "nscale", "norient"):
        assert key in c1.params


def test_phase_congruency_ablation_switch(tmp_path):
    img = render(hill_dem(), true_albedo(), 45.0, 25.0)
    prod = Product("a.tif", img, meta_for(45.0, 25.0))

    off = canonicalise(prod, {"phase_congruency": False})
    assert off.params["pc_status"] == "disabled"
    assert not off.pc.any() and not off.pc_orient.any()

    on = canonicalise(prod, {"phase_congruency": True})
    # "unavailable" is honest reporting while photometry/phasecong.py is still landing;
    # it must never be reported as a real PC map.
    assert on.params["pc_status"] in ("computed", "unavailable")
    if on.params["pc_status"] == "unavailable":
        assert not on.pc.any()
    assert np.isfinite(on.pc).all() and np.isfinite(on.pc_orient).all()
