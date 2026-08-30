import numpy as np
import pytest

from samanvay.photometry.phasecong import phase_congruency


def lunar_patch(h=192, w=192, seed=7):
    """Crater rims, a bright line and a broad step — the structures pc has to find."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = 0.35 + 0.05 * np.sin(xx / 37.0) * np.cos(yy / 51.0)
    for cx, cy, r in [(70, 90, 28), (150, 55, 18), (110, 160, 34)]:
        d = np.hypot(xx - cx, yy - cy)
        img += 0.25 * np.exp(-(d - r) ** 2 / 18.0) * np.sign(xx - cx)
        img -= 0.12 * (d < r)
    img[:, 150:154] += 0.2
    img[130:, :] += 0.05
    img += 0.005 * rng.standard_normal((h, w))
    return np.clip(img, 0.0, 1.0).astype(np.float32)


def corr(a, b):
    a = a.ravel().astype(np.float64)
    b = b.ravel().astype(np.float64)
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def test_contrast_and_brightness_invariance():
    """The property the whole module exists for: pc(a*I + b) == pc(I) for a > 0."""
    img = lunar_patch()
    base = phase_congruency(img)["pc"]
    assert corr(base, phase_congruency(2.5 * img + 0.3)["pc"]) > 0.95   # measured 1.000
    assert corr(base, phase_congruency(0.01 * img)["pc"]) > 0.95        # measured 1.000
    assert corr(base, phase_congruency(img - img.mean())["pc"]) > 0.95  # measured 1.000


def test_monotonic_nonlinear_remap_invariance():
    """Gamma remap — closer to real cross-sensor radiometry, so a looser bound."""
    img = lunar_patch()
    base = phase_congruency(img)["pc"]
    # measured: gamma 0.5 -> 0.980, gamma 2.0 -> 0.954 (192x192 patch above)
    assert corr(base, phase_congruency(img ** 0.5)["pc"]) > 0.90
    assert corr(base, phase_congruency(img ** 2.0)["pc"]) > 0.90


def test_responds_at_edges_and_stays_flat_elsewhere():
    step = np.zeros((128, 128), np.float32)
    step[:, 64:] = 1.0
    pc = phase_congruency(step)["pc"]
    assert pc[:, 63:65].max() > 0.5
    assert pc[:, 5:50].mean() < 0.05

    line = np.zeros((128, 128), np.float32)
    line[:, 60] = 1.0
    pc = phase_congruency(line)["pc"]
    assert pc[:, 59:62].max() > 0.5
    assert pc[:, 5:50].mean() < 0.05


@pytest.mark.parametrize("shape", [(64, 64), (65, 63), (31, 64), (17, 17)])
def test_shape_dtype_finite_even_and_odd(shape):
    out = phase_congruency(lunar_patch(*shape))
    assert set(out) == {"pc", "orientation", "mim"}
    assert out["pc"].shape == shape and out["pc"].dtype == np.float32
    assert out["orientation"].shape == shape and out["orientation"].dtype == np.float32
    assert out["mim"].shape == shape and out["mim"].dtype == np.uint8
    assert np.isfinite(out["pc"]).all() and np.isfinite(out["orientation"]).all()
    assert out["pc"].min() >= 0.0 and out["pc"].max() <= 1.0
    assert out["orientation"].min() >= 0.0 and out["orientation"].max() < np.pi
    assert out["mim"].max() < 6


def test_mim_agrees_with_orientation():
    out = phase_congruency(lunar_patch(96, 96), norient=6)
    assert np.allclose(out["orientation"], out["mim"] * (np.pi / 6), atol=1e-6)


def test_degenerate_inputs_do_not_crash():
    for arr in (np.full((64, 64), 0.4, np.float32),     # perfectly flat
                np.zeros((0, 4), np.float32),           # empty
                np.zeros((8, 8), np.float32),           # tiny and all-zero
                np.zeros((1, 5), np.float32)):
        out = phase_congruency(arr)
        assert out["pc"].shape == arr.shape
        assert np.isfinite(out["pc"]).all()
    # a flat image has no structure: pc must be exactly zero, never a fabricated value
    assert phase_congruency(np.full((64, 64), 0.4, np.float32))["pc"].max() == 0.0
    # a tiny image still runs the full bank
    assert phase_congruency(lunar_patch(12, 12))["pc"].shape == (12, 12)
    with pytest.raises(ValueError):
        phase_congruency(np.zeros((8, 8, 3), np.float32))


def test_integer_input_and_nan_are_handled():
    img = lunar_patch(96, 96)
    base = phase_congruency(img)["pc"]
    assert corr(base, phase_congruency((img * 255).astype(np.uint8))["pc"]) > 0.95

    poisoned = img.copy()
    poisoned[10:20, 10:20] = np.nan
    poisoned[30, 30] = np.inf
    out = phase_congruency(poisoned)
    assert np.isfinite(out["pc"]).all() and np.isfinite(out["orientation"]).all()
