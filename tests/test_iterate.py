"""Seat 2 · Pillar P1 tests — canonicalising at a CORRECTED pose (the two-pass loop).

`canonicalise` positions the DEM through the image's own geotransform, and that
geotransform carries the very metadata error this project exists to correct (~36 px on
the shipped fixture). The rendered illumination therefore sits tens of pixels away from
the terrain that made it, and dividing by it injects structured error at exactly the
scale SIFT keys on. Two things have to be true for the fix to be worth anything, and
both are asserted here rather than eyeballed:

  1. `canonicalise_with_transform` at a KNOWN-GOOD pose renders an illumination field
     measurably better aligned with the image than the metadata pose does (correlation),
     and with H=None or H=identity it is exactly the old behaviour.
  2. The default policy is the safe one: with no pose confidence asserted, "auto" picks
     dem_lowfreq, which only removes the gross gradient and survives a wrong pose.

Helpers come from tests/test_photometry.py — same seat, same synthetic terrain.
"""

import numpy as np
import pytest

from test_photometry import (GSD, corr, hill_dem, meta_for, render, true_albedo,
                             write_dem)

from samanvay.types import Product
from samanvay.photometry.normalize import (_DEFAULTS, _illumination, canonicalise,
                                           canonicalise_with_transform)
from samanvay.photometry.shading import illumination_shift_px

SHIFT_PX = 12.0            # how wrong we make the stated pose; ~1/10 of the test image
NO_PC = {"phase_congruency": False}    # PC is seat 1's arm and costs 10x this test


def gt_meta(shift_px=0.0, az=45.0, el=25.0):
    """meta whose geotransform is `shift_px` pixels wrong; 0 means it is exact.

    Rasterio order (a, b, c, d, e, f), which is what normalize.py reads, and the same
    order the fixture sidecars carry. The DEM is written at Affine(GSD,0,0, 0,-GSD,0),
    so shift_px = 0 makes image pixel == DEM pixel and H = identity is the true pose.
    """
    meta = dict(meta_for(az, el))
    meta["geotransform"] = [GSD, 0.0, shift_px * GSD, 0.0, -GSD, -shift_px * GSD]
    return meta


def merged(params):
    """The params dict `_illumination` expects: defaults with the caller's keys over them."""
    p = dict(_DEFAULTS)
    p.update(params)
    return p


def scene(tmp_path, shift_px=SHIFT_PX, az=45.0, el=25.0):
    """(product, dem_path, true_albedo) — DN rendered at the TRUE pose, meta lying by shift_px."""
    dem = hill_dem()
    albedo = true_albedo()
    img = render(dem, albedo, az, el)
    dem_path = write_dem(tmp_path / "dem.tif", dem, GSD)
    return Product("src.tif", img, gt_meta(shift_px, az, el)), dem_path, albedo


# --------------------------------------------------------------- 1. identity == old path

def test_identity_and_none_reproduce_canonicalise(tmp_path):
    """H=None is canonicalise by construction; H=identity must land on the same pixels."""
    dem = hill_dem()
    img = render(dem, true_albedo(), 45.0, 25.0)
    dem_path = write_dem(tmp_path / "dem.tif", dem, GSD)
    # No geotransform in meta, DEM on the image grid: canonicalise samples it 1:1, which
    # is precisely what H=identity asks for. Any other pair of poses would differ.
    prod = Product("a.tif", img, meta_for(45.0, 25.0))
    params = dict(NO_PC, dem_path=dem_path)

    base = canonicalise(prod, params)
    none_h = canonicalise_with_transform(prod, params, None)
    ident = canonicalise_with_transform(prod, params, np.eye(3))

    assert base.params["illum_mode"] == "dem"
    for other in (none_h, ident):
        assert other.params["illum_mode"] == base.params["illum_mode"]
        assert np.array_equal(other.albedo, base.albedo)
        assert np.array_equal(other.mask, base.mask)
        assert np.array_equal(other.pc_orient, base.pc_orient)
    # Provenance is the one thing that must NOT be identical: metrics.json has to be able
    # to say which pose rendered the field.
    assert base.params["dem_transform"] == "metadata_geotransform"
    assert ident.params["dem_transform"] == "caller_supplied"
    assert ident.params["dem_align"] == "corrected_transform"


# --------------------------------------------------- 2. a corrected pose aligns better

def test_corrected_pose_beats_metadata_pose_on_correlation(tmp_path):
    """The whole claim, as a number: illumination rendered at the true pose fits the image."""
    prod, dem_path, albedo = scene(tmp_path)
    params = merged(dict(NO_PC, dem_path=dem_path, illum_scale="full"))
    img = np.asarray(prod.array, dtype=np.float64)

    meta_illum, _, meta_mode, meta_info = _illumination(img, prod.meta, params, None)
    true_illum, _, true_mode, true_info = _illumination(img, prod.meta, params, np.eye(3))
    assert meta_mode == true_mode == "dem"          # both full renders, only the pose differs
    assert meta_info["dem_align"] == "geotransform"
    assert true_info["dem_align"] == "corrected_transform"

    lit = (meta_illum > 0) & (true_illum > 0) & (img > 0)
    assert lit.sum() > img.size // 4                 # enough pixels to correlate honestly
    c_meta = corr(img, meta_illum, lit)
    c_true = corr(img, true_illum, lit)
    assert c_true > c_meta + 0.05, (c_true, c_meta)

    # And the point of it: the albedo recovered at the true pose is closer to the real
    # albedo. Public API only — this is what the matcher actually sees.
    a_meta = canonicalise_with_transform(prod, dict(NO_PC, dem_path=dem_path,
                                                    illum_scale="full"), None)
    a_true = canonicalise_with_transform(prod, dict(NO_PC, dem_path=dem_path,
                                                    illum_scale="full"), np.eye(3))
    sel = (a_meta.mask == 0) & (a_true.mask == 0)
    assert corr(a_true.albedo, albedo, sel) > corr(a_meta.albedo, albedo, sel)


# ------------------------------------------------------------------- 3. the auto policy

def test_auto_picks_lowfreq_until_the_pose_is_asserted(tmp_path):
    """No pose confidence -> low-frequency removal, which a wrong pose cannot poison."""
    prod, dem_path, _ = scene(tmp_path)
    base = dict(NO_PC, dem_path=dem_path)

    auto = canonicalise(prod, base)
    assert auto.params["illum_scale"] == "auto"      # the default, not something we set
    assert auto.params["illum_mode"] == "dem_lowfreq"
    assert auto.params["pose_uncertainty_px"] == "unknown"
    assert auto.params["dem_gsd_ratio"] == pytest.approx(1.0)   # not the D2 coarse-DEM gate

    # The pipeline asserting a good pose after pass one is what unlocks the full render.
    trusted = canonicalise(prod, dict(base, pose_trusted=True))
    assert trusted.params["illum_mode"] == "dem"

    # So is handing over a recovered transform: supplying it IS the assertion.
    corrected = canonicalise_with_transform(prod, base, np.eye(3))
    assert corrected.params["illum_mode"] == "dem"

    # ...unless the caller declines to make the claim.
    modest = canonicalise_with_transform(prod, dict(base, pose_trusted=False), np.eye(3))
    assert modest.params["illum_mode"] == "dem_lowfreq"

    # Both explicit policies override the evidence, in both directions.
    assert canonicalise(prod, dict(base, illum_scale="full")).params["illum_mode"] == "dem"
    assert canonicalise(prod, dict(base, illum_scale="lowfreq", pose_trusted=True)
                        ).params["illum_mode"] == "dem_lowfreq"

    # An unknown policy string falls back to the safe one and records what was asked for.
    bogus = canonicalise(prod, dict(base, illum_scale="lowfrequency"))
    assert bogus.params["illum_scale"] == "auto"
    assert bogus.params["illum_scale_requested"] == "lowfrequency"
    assert bogus.params["illum_mode"] == "dem_lowfreq"


def test_stated_pose_uncertainty_decides_when_it_is_known(tmp_path):
    """Evidence, not a guess: a metre-level pose is trusted, a 100 m one is not."""
    prod, dem_path, _ = scene(tmp_path)
    base = dict(NO_PC, dem_path=dem_path)

    good = canonicalise(prod, dict(base, pose_uncertainty_m=GSD))        # 1 px
    assert good.params["illum_mode"] == "dem"
    assert good.params["pose_uncertainty_px"] == pytest.approx(1.0)

    bad = canonicalise(prod, dict(base, pose_uncertainty_m=10 * GSD))    # 10 px
    assert bad.params["illum_mode"] == "dem_lowfreq"
    # Smoothing is sized by the pose error, not by a constant somebody picked.
    assert bad.params["lowfreq_sigma_basis"] == "pose_uncertainty_px"
    assert bad.params["lowfreq_sigma_px"] == pytest.approx(10.0)


def test_illumination_shift_px_says_unknown_rather_than_zero():
    """Unknown pose error is never quietly read as a good pose."""
    assert illumination_shift_px({"gsd_m": 5.0})[0] is None
    assert illumination_shift_px({"gsd_m": 5.0, "pose_uncertainty_m": 20.0})[0] == 4.0
    assert illumination_shift_px({"gsd_m": 5.0, "geotransform_exact": True})[0] == 0.0
    # Uncertainty known, scale not: still unknown, not a number invented from one half.
    px, info = illumination_shift_px({"pose_uncertainty_m": 20.0})
    assert px is None and info["pose_uncertainty_m"] == 20.0
    # Junk in every slot, no exception out.
    assert illumination_shift_px(None)[0] is None
    assert illumination_shift_px({"gsd_m": 0.0}, pose_uncertainty_m="nonsense")[0] is None


# ------------------------------------------------------- 4. every mode stays honest

@pytest.mark.parametrize("scale", ["auto", "full", "lowfreq"])
@pytest.mark.parametrize("h_name", ["none", "identity", "singular", "off_dem"])
def test_every_mode_is_finite_with_sane_masks(tmp_path, scale, h_name):
    prod, dem_path, _ = scene(tmp_path)
    H = {"none": None,
         "identity": np.eye(3),
         "singular": np.zeros((3, 3)),
         "off_dem": np.array([[1.0, 0.0, 5e4], [0.0, 1.0, 5e4], [0.0, 0.0, 1.0]])}[h_name]

    c = canonicalise_with_transform(prod, dict(NO_PC, dem_path=dem_path,
                                               illum_scale=scale), H)

    assert c.albedo.shape == c.mask.shape == prod.array.shape
    assert np.isfinite(c.albedo).all()
    assert c.albedo.min() >= 0.0 and c.albedo.max() <= 1.0
    assert set(np.unique(c.mask)).issubset({0, 1, 2, 3})
    assert (c.mask == 0).any()                      # a lit hill is not entirely masked
    assert c.params["illum_mode"] in ("dem", "dem_lowfreq")
    if h_name in ("singular", "off_dem"):
        # A pose we cannot use falls back to the stated one and SAYS SO. Never a crash,
        # never a silent substitution that reads downstream as a corrected render.
        assert "dem_transform_fallback" in c.params
        assert c.params["dem_align"] == "geotransform"


def test_degenerate_inputs_do_not_crash(tmp_path):
    """Zero-size, all-nodata and no-DEM inputs return an honest result, not an exception."""
    _, dem_path, _ = scene(tmp_path)
    empty = Product("e.tif", np.zeros((0, 0), dtype=np.float32), gt_meta())
    c = canonicalise_with_transform(empty, dict(NO_PC, dem_path=dem_path), np.eye(3))
    assert c.albedo.shape == (0, 0) and c.mask.shape == (0, 0)
    assert c.params["illum_mode"] == "none"          # not a claim that anything was rendered

    flat = Product("f.tif", np.zeros((16, 16), dtype=np.float32), gt_meta())
    c = canonicalise_with_transform(flat, dict(NO_PC, dem_path=dem_path), np.eye(3))
    assert np.isfinite(c.albedo).all()

    # No DEM: H_to_dem has nothing to position, so it is ignored rather than obeyed.
    c = canonicalise_with_transform(flat, dict(NO_PC), np.eye(3))
    assert c.params["illum_mode"] == "empirical"
    assert np.isfinite(c.albedo).all()
