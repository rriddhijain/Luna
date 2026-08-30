"""Seat 2 · pillar C4 — the phase-congruency structural encoder.

Kovesi's log-Gabor phase congruency, computed in the frequency domain. Phase, not
intensity, so the representation survives the non-linear radiometry between a
Chandrayaan-2 strip and an LRO reference acquired at a different sun angle. This is
the substrate RIFT (arXiv:1804.09493) is built on: `pc`, the dominant orientation,
and the maximum-index map.
"""

import numpy as np
from scipy import fft

_SQRT_LOG4 = np.sqrt(np.log(4.0))


def _polar_grids(rows, cols):
    """Frequency-domain radius and sin/cos of polar angle, ifftshifted so DC sits at [0,0]."""
    # Kovesi's sampling: even length spans [-n/2, n/2-1]/n, odd spans [-(n-1)/2, (n-1)/2]/(n-1).
    def axis(n):
        span = np.arange(-(n // 2), (n - 1) // 2 + 1, dtype=np.float32)
        return span / (n if n % 2 == 0 else max(n - 1, 1))

    x, y = np.meshgrid(axis(cols), axis(rows))
    radius = np.hypot(x, y)
    theta = np.arctan2(-y, x)
    radius = fft.ifftshift(radius)
    theta = fft.ifftshift(theta)
    # Butterworth low-pass (cutoff .45, order 15) kills the corner aliasing of the bank.
    with np.errstate(under="ignore"):  # low radii underflow to 0 in float32, which is the wanted answer
        lowpass = (1.0 / (1.0 + (radius / 0.45) ** 30)).astype(np.float32)
    radius[0, 0] = 1.0  # avoid log(0); the DC bin is zeroed in every filter anyway
    return radius, lowpass, np.sin(theta), np.cos(theta)


def _log_gabor_bank(radius, lowpass, nscale, min_wavelength, mult, sigma_onf):
    """The nscale radial log-Gabor transfer functions, built once per (shape, params)."""
    denom = 2.0 * np.log(sigma_onf) ** 2
    bank = []
    for s in range(nscale):
        fo = 1.0 / (min_wavelength * mult ** s)
        lg = np.exp(-(np.log(radius / fo) ** 2) / denom).astype(np.float32) * lowpass
        lg[0, 0] = 0.0
        bank.append(lg)
    return bank


def phase_congruency(img, nscale=4, norient=6, min_wavelength=3.0, mult=2.1,
                     sigma_onf=0.55, k=2.0, cut_off=0.5, g=10.0) -> dict:
    """Log-Gabor phase congruency; returns {"pc", "orientation", "mim"}."""
    img = np.asarray(img)
    if img.ndim != 2:
        raise ValueError(f"phase_congruency expects a 2D array, got shape {img.shape}")
    img = np.nan_to_num(img.astype(np.float32, copy=False), nan=0.0, posinf=0.0, neginf=0.0)

    rows, cols = img.shape
    nscale = max(int(nscale), 1)
    norient = max(int(norient), 1)
    pc = np.zeros((rows, cols), np.float32)
    orientation = np.zeros((rows, cols), np.float32)
    mim = np.zeros((rows, cols), np.uint8)

    # Empty or perfectly flat: there is no structure to report and we will not invent one.
    spread_rms = 0.0 if img.size == 0 else float(img.std())
    if spread_rms == 0.0:
        return {"pc": pc, "orientation": orientation, "mim": mim}

    # Scale-relative guard term, so pc(a*I) == pc(I) holds for any a > 0.
    eps = np.float32(1e-4 * spread_rms)

    radius, lowpass, sintheta, costheta = _polar_grids(rows, cols)
    bank = _log_gabor_bank(radius, lowpass, nscale, min_wavelength, mult, sigma_onf)
    del radius, lowpass

    # One forward transform of the real image; the inverses must stay complex because the
    # angular spread makes each filter one-sided in frequency, i.e. an analytic (quadrature) pair.
    spectrum = fft.fft2(img, workers=-1)

    # Kovesi's Rayleigh noise compensation, summed over the geometric scale series.
    # ponytail: tau and eps are statistics of the array handed in, so pc over a tile is not
    # bit-identical to pc over the whole strip (a near-featureless tile thresholds differently).
    # Upgrade path: estimate tau once on a strip-wide sample and pass it in as a fixed noise level.
    tau_gain = nscale if mult <= 1.0 else (1.0 - (1.0 / mult) ** nscale) / (1.0 - 1.0 / mult)
    noise_gain = tau_gain * (np.sqrt(np.pi / 2.0) + k * np.sqrt((4.0 - np.pi) / 2.0))

    energy_all = np.zeros((rows, cols), np.float32)
    an_all = np.zeros((rows, cols), np.float32)
    best_pc = np.full((rows, cols), -1.0, np.float32)

    for o in range(norient):
        angl = o * np.pi / norient
        # Angular spread; recomputed once per orientation, never per scale.
        dtheta = np.abs(np.arctan2(sintheta * np.cos(angl) - costheta * np.sin(angl),
                                   costheta * np.cos(angl) + sintheta * np.sin(angl)))
        spread = (np.cos(np.minimum(dtheta * norient / 2.0, np.pi)) + 1.0) / 2.0

        # O(nscale) arrays live at once, never O(nscale * norient).
        eo = [fft.ifft2(spectrum * (lg * spread), workers=-1) for lg in bank]

        sum_an = np.abs(eo[0])
        tau = float(np.median(sum_an)) / _SQRT_LOG4  # Rayleigh estimate from the smallest scale
        max_an = sum_an.copy()
        for e in eo[1:]:
            an = np.abs(e)
            sum_an += an
            np.maximum(max_an, an, out=max_an)

        sum_e = np.zeros((rows, cols), np.float32)
        sum_o = np.zeros((rows, cols), np.float32)
        for e in eo:
            sum_e += e.real
            sum_o += e.imag
        x_energy = np.hypot(sum_e, sum_o) + eps
        mean_e = sum_e / x_energy
        mean_o = sum_o / x_energy
        energy = np.zeros((rows, cols), np.float32)
        for e in eo:
            energy += e.real * mean_e + e.imag * mean_o - np.abs(e.real * mean_o - e.imag * mean_e)
        del eo

        energy -= np.float32(tau * noise_gain)
        np.maximum(energy, 0.0, out=energy)

        # Sigmoid frequency-spread weight: penalise energy carried by a single scale.
        if nscale > 1:
            width = (sum_an / (max_an + eps) - 1.0) / (nscale - 1)
            energy *= 1.0 / (1.0 + np.exp((cut_off - width) * g))

        energy_all += energy
        an_all += sum_an

        pc_o = energy / (sum_an + eps)
        better = pc_o > best_pc
        best_pc[better] = pc_o[better]
        orientation[better] = angl
        mim[better] = o

    # Numerator and denominator both scale linearly with contrast, so this ratio is already in [0,1].
    np.divide(energy_all, an_all + eps, out=pc)
    np.clip(pc, 0.0, 1.0, out=pc)
    # orientation/mim are only meaningful where pc > 0; in flat regions they stay at the o=0 default.
    return {"pc": pc, "orientation": orientation, "mim": mim}
