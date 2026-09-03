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


# --- Lunar-Lambert (McEwen) -------------------------------------------------

def test_mcewen_L_matches_the_published_coefficients():
    """L(g) is the ISIS3 LunarLambertMcEwen polynomial; these are its values, not ours."""
    from samanvay.photometry.shading import lunar_lambert_L
    assert lunar_lambert_L(0.0) == pytest.approx(1.0)
    # Hand-evaluated 1 - 0.019g + 2.42e-4 g^2 - 1.46e-6 g^3.
    assert lunar_lambert_L(30.0) == pytest.approx(0.60838, abs=1e-5)
    assert lunar_lambert_L(60.0) == pytest.approx(0.41584, abs=1e-5)
    assert lunar_lambert_L(90.0) == pytest.approx(0.18586, abs=1e-5)
    # Monotone decreasing over the range any orbiter actually flies.
    vals = [lunar_lambert_L(g) for g in range(0, 121, 5)]
    assert all(b <= a for a, b in zip(vals, vals[1:]))


def test_lunar_lambert_endpoints_are_lommel_seeliger_and_lambert():
    """L=1 must give 2*LS (not LS) and L=0 must give Lambert. The factor 2 is the point."""
    from samanvay.photometry.shading import lunar_lambert
    cos_i = np.array([[0.8, 0.5]], dtype=np.float32)
    cos_e = np.float32(1.0)
    assert lunar_lambert(cos_i, cos_e, 0.0) == pytest.approx(cos_i, abs=1e-6)
    expect_ls = 2.0 * cos_i / (cos_i + cos_e)
    assert lunar_lambert(cos_i, cos_e, 1.0) == pytest.approx(expect_ls, abs=1e-6)
    # At nadir both terms reach 1: that is what makes L a limb-darkening weight.
    one = np.array([[1.0]], dtype=np.float32)
    assert lunar_lambert(one, np.float32(1.0), 1.0) == pytest.approx(one, abs=1e-6)


def test_phase_angle_is_derived_only_near_nadir_and_never_invented():
    """g ~= i is bounded by the emission angle, so it is refused once emission opens up."""
    from samanvay.photometry.shading import phase_angle_deg
    g, info = phase_angle_deg({"phase_deg": 42.0, "incidence_deg": 10.0})
    assert (g, info["source"]) == (42.0, "label")
    g, info = phase_angle_deg({"incidence_deg": 30.24, "emission_deg": 1.5})
    assert g == pytest.approx(30.24) and info["source"] == "derived_from_incidence"
    assert info["max_error_deg"] == pytest.approx(1.5)
    # Off-nadir: the shortcut is not defensible and we say unknown rather than guess.
    g, info = phase_angle_deg({"incidence_deg": 30.0, "emission_deg": 35.0})
    assert g is None and info["source"] == "unknown"
    # A Chandrayaan-2 TMC label with no angles at all.
    g, info = phase_angle_deg({"sun_az_deg": 320.0, "sun_el_deg": 37.1})
    assert g is None


def test_lunar_lambert_degrades_to_lambert_without_an_emission_angle():
    """Ch-2 TMC labels carry no EMISSION_ANGLE; the model must not invent one."""
    from samanvay.photometry.shading import predicted_illumination
    dem = np.zeros((8, 8), dtype=np.float64)
    meta = {"sun_az_deg": 135.0, "sun_el_deg": 40.0, "incidence_deg": 50.0}
    ll = predicted_illumination(dem, 1.0, meta, model="lunar_lambert")
    lam = predicted_illumination(dem, 1.0, meta, model="lambert")
    assert ll == pytest.approx(lam, abs=1e-6)


def test_mcewen_L_is_clamped_not_negative_past_its_fitted_range():
    """The cubic goes negative past g=103.7 deg; a negative limb-darkening weight is nonsense."""
    from samanvay.photometry.shading import lunar_lambert_L
    raw = lambda g: 1 - 0.019 * g + 2.42e-4 * g ** 2 - 1.46e-6 * g ** 3
    assert raw(110.0) < 0.0                      # the polynomial really does go negative
    assert lunar_lambert_L(110.0) == 0.0         # and we clamp to Lambert rather than pass it on
    assert lunar_lambert_L(103.0) > 0.0          # just inside the root, still the fit


# --- mask_fill: the PC input, not the returned albedo -----------------------

def masked_scene(n=256):
    """Low-texture terrain with ONE contiguous cast shadow — the real-NAC mask geometry.

    The synthetic sweep fixture cannot stand in for this: its masks are 1.1% scattered
    single-pixel speckle, which manufactures almost no boundary edge to remove.
    """
    rng = np.random.default_rng(5)
    base = 0.30 + 0.25 * _smooth(rng.random((n, n)).astype(np.float32), 12.0)
    base += 0.01 * rng.standard_normal((n, n)).astype(np.float32)
    yy, xx = np.mgrid[0:n, 0:n]
    dark = (xx > 0.6 * n + 0.06 * n * np.sin(yy / 30.0)) & (yy < 0.75 * n)
    img = base.copy()
    img[dark] = 0.0
    return Product(path=None, array=img,
                   meta={"gsd_m": 5.0, "sun_az_deg": 90.0, "sun_el_deg": 20.0}), dark


def _smooth(a, sigma):
    import cv2
    a = cv2.GaussianBlur(a, (0, 0), sigma)
    return (a - a.min()) / max(float(a.max() - a.min()), 1e-12)


def boundary_excess(c, band_px=1, far_px=10):
    """Mean PC in the band just inside the mask boundary / mean PC in the interior."""
    import cv2
    valid = c.mask == MASK_VALID
    inv = (~valid).astype(np.uint8)
    band = cv2.dilate(inv, np.ones((2 * band_px + 1,) * 2, np.uint8)).astype(bool) & valid
    interior = valid & ~cv2.dilate(inv, np.ones((2 * far_px + 1,) * 2, np.uint8)).astype(bool)
    assert band.any() and interior.any()
    return float(c.pc[band].mean()) / float(c.pc[interior].mean())


def test_mask_fill_reflect_removes_the_manufactured_boundary_edge():
    """The claim this feature exists to make: 47.2x -> 2.1x on this scene at n=256."""
    prod, _ = masked_scene()
    z = canonicalise(prod, {"phase_congruency": True, "mask_fill": "zero"})
    r = canonicalise(prod, {"phase_congruency": True, "mask_fill": "reflect"})
    if z.params["pc_status"] != "computed":
        pytest.skip("phase congruency unavailable")
    ez, er = boundary_excess(z), boundary_excess(r)
    assert ez > 10.0                   # the defect is real and this scene shows it
    assert er < 4.0                    # and the fill takes essentially all of it
    assert er < 0.15 * ez


def test_mask_fill_does_not_touch_the_returned_albedo():
    """Only the phase-congruency input changes. The ablation and the writers read albedo."""
    prod, _ = masked_scene()
    z = canonicalise(prod, {"phase_congruency": True, "mask_fill": "zero"})
    r = canonicalise(prod, {"phase_congruency": True, "mask_fill": "reflect"})
    assert np.array_equal(z.albedo, r.albedo)
    assert np.array_equal(z.mask, r.mask)
    assert not (r.albedo[r.mask != MASK_VALID]).any()      # still zeroed outside the mask
    if z.params["pc_status"] == "computed":
        assert not np.array_equal(z.pc, r.pc)              # ...and the PC map really moved


def test_mask_fill_zero_is_byte_for_byte_the_old_path():
    """"zero" must reproduce phase_congruency(albedo) exactly, or the gain is not measurable."""
    prod, _ = masked_scene()
    c = canonicalise(prod, {"phase_congruency": True, "mask_fill": "zero"})
    if c.params["pc_status"] != "computed":
        pytest.skip("phase congruency unavailable")
    from samanvay.photometry.phasecong import phase_congruency
    out = phase_congruency(c.albedo, nscale=int(c.params["nscale"]),
                           norient=int(c.params["norient"]))
    assert np.array_equal(c.pc, np.nan_to_num(np.asarray(out["pc"], dtype=np.float32)))
    assert c.params["mask_fill_px"] == 0


def test_mask_fill_px_counts_the_invalid_pixels_and_is_recorded():
    prod, _ = masked_scene()
    r = canonicalise(prod, {"phase_congruency": True, "mask_fill": "reflect"})
    assert r.params["mask_fill"] == "reflect"
    assert r.params["mask_fill_px"] == int((r.mask != MASK_VALID).sum()) > 0
    z = canonicalise(prod, {"phase_congruency": True, "mask_fill": "zero"})
    assert (z.params["mask_fill"], z.params["mask_fill_px"]) == ("zero", 0)
    # An unknown policy falls back to the safe one rather than crashing the run.
    assert canonicalise(prod, {"mask_fill": "bilinear"}).params["mask_fill"] == "reflect"


def test_a_fully_masked_image_has_nothing_to_fill_from():
    """No valid pixel means no neighbourhood; the fill must decline, not invent one.

    And it must say so as null, not as 0. Every one of these 1024 pixels was invalid and
    the fill was asked for, so "0 pixels filled" would read identically to a clean frame
    where the fill ran and had no work — the plausible-default the repo's null rule bans.
    """
    img = np.full((32, 32), np.nan, dtype=np.float32)
    prod = Product(path=None, array=img,
                   meta={"gsd_m": 5.0, "sun_az_deg": 90.0, "sun_el_deg": 30.0})
    c = canonicalise(prod, {"phase_congruency": True, "mask_fill": "reflect"})
    assert (c.mask != MASK_VALID).all()                 # there really was work to do
    assert c.params["mask_fill_px"] is None             # ...and it could not be done
    assert np.isfinite(c.pc).all() and np.isfinite(c.albedo).all()
    # Distinguishable from the deliberate no-op, which is a measured zero.
    z = canonicalise(prod, {"phase_congruency": True, "mask_fill": "zero"})
    assert z.params["mask_fill_px"] == 0


# --- the tri-state flags and CLAHE ------------------------------------------

def test_auto_resolves_pc_true_and_clahe_false_inside_canonicalise():
    """stages.py resolves "auto" from the matcher; a direct caller has no matcher."""
    prod, _ = masked_scene(64)
    c = canonicalise(prod, {"phase_congruency": "auto", "clahe": "auto"})
    assert c.params["phase_congruency"] is True
    assert c.params["phase_congruency_requested"] == "auto"
    assert c.params["clahe_applied"] is False
    assert c.params["pc_status"] in ("computed", "unavailable")
    # An explicit string still means what it says.
    assert canonicalise(prod, {"phase_congruency": "false"}).params["phase_congruency"] is False
    assert canonicalise(prod, {"clahe": "true"}).params["clahe_applied"] is True


def test_clahe_changes_the_albedo_only_when_enabled_and_says_so():
    prod, _ = masked_scene()
    off = canonicalise(prod, {"phase_congruency": False, "clahe": False})
    on = canonicalise(prod, {"phase_congruency": False, "clahe": True,
                             "clahe_clip": 2.0, "clahe_grid": 8})
    assert off.params["clahe_applied"] is False and on.params["clahe_applied"] is True
    assert (on.params["clahe_clip"], on.params["clahe_grid"]) == (2.0, 8)
    assert not np.array_equal(off.albedo, on.albedo)
    assert np.isfinite(on.albedo).all() and on.albedo.min() >= 0.0 and on.albedo.max() <= 1.0
    # CLAHE runs before the mask is stamped in, so the zeroing still holds afterwards.
    assert not on.albedo[on.mask != MASK_VALID].any()
    # Local contrast is what CLAHE is for: it must widen the valid-pixel spread.
    v = on.mask == MASK_VALID
    assert on.albedo[v].std() > off.albedo[v].std()
