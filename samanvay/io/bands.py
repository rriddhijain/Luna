"""Seat 3 · io/bands — reduce a multi-band cube to the one 2-D map the matcher consumes.

IIRS is one of the three Chandrayaan-2 instruments named in the ISRO problem statement
and it delivers ~250 bands. `src.read(1)` on that cube is not a choice of band, it is an
accident: band 1 is one end of the spectrometer, and the bands there are the noisiest in
the product. Nothing downstream can tell that it was handed the worst band of 250 rather
than the only band there was.

What this module does instead, for `reduce: "pc1"` (the default):

    1. one decimated read of every band, sized so the whole cube stays inside a fixed
       sample budget (a 3000x3000 250-band cube reads as 250 x 253 x 253, 128 MB of
       float64, not 250 full frames);
    2. a per-band SNR proxy = |mean| / std over that decimated frame. It is a proxy,
       not a radiometric SNR: we have no dark frame and no per-band noise model, so what
       it actually separates is "band carries scene structure" from "band is mostly
       detector noise", which is the decision being made here;
    3. drop the bands below `min_snr`, then keep at most `max_bands` of the survivors,
       evenly spaced across the spectrum rather than the first N (the first N are one
       contiguous spectral region and are strongly correlated with each other);
    4. PCA on the decimated survivors — an eigendecomposition of a k x k covariance, so
       the expensive dimension is the small one — and the first principal component is
       then evaluated at FULL resolution by streaming one band at a time, since PC1 is
       just a weighted sum of centred bands. Peak memory is one band plus the output.

The PCA sign is arbitrary: `-v` is as valid an eigenvector as `v`. Left alone, roughly
half of all runs would deliver a contrast-inverted pseudo-panchromatic map, and a crater
rim would read as a crater floor. The sign is therefore fixed so PC1 correlates
positively with the plain band mean, which makes the output reproducible and keeps
"bright is bright".

A single-band product never enters any of this: it is `src.read(1)`, byte for byte.
"""

import numpy as np

# This module owns its own defaults, as every module in this repo does; pipeline/config
# registers the same keys so a typo is visible in `samanvay show-config`.
DEFAULTS = {"index": None, "reduce": "pc1", "min_snr": 2.0, "max_bands": 64}

# Total decimated samples across ALL bands. 16e6 float64 == 128 MB while the covariance
# is being formed, and it is the only place the whole cube is resident at once.
_SAMPLE_BUDGET = 16_000_000
_MAX_SIDE = 512  # a bigger decimated frame buys no accuracy in a k x k covariance


def _decimated_shape(height, width, n_bands):
    """Read shape for the screening pass: the cube inside the sample budget, aspect kept."""
    per_band = max(1024.0, _SAMPLE_BUDGET / max(1, int(n_bands)))
    scale = min(1.0, float(np.sqrt(per_band / max(1.0, float(height) * float(width)))))
    dh = int(min(height, _MAX_SIDE, max(2, round(height * scale))))
    dw = int(min(width, _MAX_SIDE, max(2, round(width * scale))))
    return max(1, dh), max(1, dw)


def _snr(rows):
    """|mean| / std per row, 0.0 where a band is empty or constant (constant == no signal)."""
    out = np.zeros(len(rows), dtype=np.float64)
    for i, row in enumerate(rows):
        vals = row[np.isfinite(row)]
        if vals.size < 2:
            continue
        std = float(vals.std())
        if std > 0.0:
            out[i] = abs(float(vals.mean())) / std
    return out


def _evenly_spaced(indices, k):
    """At most k of `indices`, evenly spaced across the list (never reordered, no repeats)."""
    if k is None or k <= 0 or len(indices) <= k:
        return list(indices)
    picks = np.unique(np.linspace(0, len(indices) - 1, int(k)).round().astype(int))
    return [indices[i] for i in picks]


def _read(src, band, shape):
    """Read one band, passing out_shape only when it actually changes the read.

    The single-band path must be `src.read(1)` and nothing else — a caller comparing
    against today's behaviour has to get the identical array, not a resampled one that
    happens to agree.
    """
    if tuple(shape) == (int(src.height), int(src.width)):
        return src.read(int(band))
    return src.read(int(band), out_shape=tuple(shape))


def _read_stack(src, bands, out_shape, nodata):
    """Decimated read of `bands` as float64 with nodata turned into NaN."""
    data = src.read(indexes=list(bands), out_shape=(len(bands),) + tuple(out_shape))
    data = np.asarray(data, dtype=np.float64).reshape(len(bands), -1)
    if nodata is not None and np.isfinite(nodata):
        data[data == float(nodata)] = np.nan
    return data


def _fill_nan(rows):
    """Replace NaN with the band's own mean; a band that is entirely NaN becomes 0."""
    for row in rows:
        bad = ~np.isfinite(row)
        if bad.any():
            good = row[~bad]
            row[bad] = float(good.mean()) if good.size else 0.0
    return rows


def _stream_full_res(src, bands, weights, offsets, out_shape, nodata):
    """sum_k w_k * (band_k - offset_k) at full resolution, one band resident at a time."""
    height, width = out_shape
    out = np.zeros((height, width), dtype=np.float32)
    for band, weight, offset in zip(bands, weights, offsets):
        plane = _read(src, band, out_shape).astype(np.float32)
        if nodata is not None and np.isfinite(nodata):
            # A nodata pixel contributes exactly 0 to the sum rather than a wild value;
            # the validity mask is photometry's job, not this module's.
            plane[plane == np.float32(nodata)] = np.float32(offset)
        out += np.float32(weight) * (plane - np.float32(offset))
    return out


def reduce_bands(src, cfg=None, out_shape=None):
    """Reduce an open rasterio dataset to one 2-D array plus a record of how.

    `src` is an OPEN rasterio dataset; `cfg` is config["band"]. `out_shape` is an
    optional (height, width) passed straight to the reads, so a caller that is already
    decimating at read time (loaders.load_product) decimates this too rather than
    reducing at full resolution and throwing the pixels away afterwards.

    Returns (2-D ndarray, info). The info dict always carries n_bands, n_bands_used,
    n_bands_dropped_snr, reduce and explained_var_frac — explained_var_frac is None
    wherever no PCA was run, never 0.0.
    """
    cfg = {**DEFAULTS, **(cfg or {})}
    n_bands = int(src.count)
    shape = tuple(out_shape) if out_shape else (int(src.height), int(src.width))
    info = {"n_bands": n_bands, "n_bands_used": 1, "n_bands_dropped_snr": 0,
            "reduce": None, "explained_var_frac": None, "bands_used": None,
            "min_snr": None, "sign_flipped": None}

    if n_bands == 1:
        # The single-band path is today's behaviour exactly: same read, same dtype.
        info["reduce"] = "single"
        info["bands_used"] = [1]
        return _read(src, 1, shape), info

    index = cfg.get("index")
    reduce = str(cfg.get("reduce") or "pc1").lower()
    if index is not None or reduce == "band":
        # index=None with reduce="band" means band 1, which is what the config says.
        band = int(index) if index is not None else 1
        clamped = max(1, min(n_bands, band))
        info["reduce"] = "band"
        info["bands_used"] = [clamped]
        info["index"] = clamped
        if clamped != band:
            # A pinned band outside the cube is a config error, and delivering the nearest
            # band without saying so is exactly the silent substitution this module exists
            # to stop. The run continues; the record says what it actually read.
            info["index_requested"] = band
            info["index_note"] = f"band.index={band} is outside 1..{n_bands}; read {clamped}"
        return _read(src, clamped, shape), info

    nodata = src.nodata
    screen_shape = _decimated_shape(int(src.height), int(src.width), n_bands)
    all_bands = list(range(1, n_bands + 1))
    stack = _read_stack(src, all_bands, screen_shape, nodata)

    min_snr = float(cfg.get("min_snr") or 0.0)
    snr = _snr(stack)
    keep = [b for b, s in zip(all_bands, snr) if s >= min_snr]
    if not keep:
        # Every band failed the screen. Dropping all of them would leave nothing to
        # match, so keep them all and say so: the screen is advice, not a veto.
        keep = all_bands
        info["snr_screen"] = "no band passed min_snr; screen ignored"
    dropped = n_bands - len(keep)
    keep = _evenly_spaced(keep, cfg.get("max_bands"))

    rows = _fill_nan(stack[[b - 1 for b in keep]].copy())
    means = rows.mean(axis=1)
    centred = rows - means[:, None]

    info.update({"n_bands_used": len(keep), "n_bands_dropped_snr": int(dropped),
                 "bands_used": [int(b) for b in keep], "min_snr": min_snr,
                 "snr_range": [round(float(snr.min()), 3), round(float(snr.max()), 3)],
                 "screen_shape": [int(screen_shape[0]), int(screen_shape[1])]})

    if reduce == "mean" or len(keep) < 2:
        if reduce != "mean":
            info["reduce_note"] = f"{reduce} needs >= 2 bands; {len(keep)} survived"
        info["reduce"] = "mean"
        weights = np.full(len(keep), 1.0 / len(keep))
        offsets = np.zeros(len(keep))
        return _stream_full_res(src, keep, weights, offsets, shape, nodata), info

    # PCA on the k x k covariance of the decimated bands: k <= max_bands (64), so this
    # is milliseconds regardless of how many pixels the cube has.
    cov = np.cov(centred)
    evals, evecs = np.linalg.eigh(cov)
    weights = np.asarray(evecs[:, -1], dtype=np.float64)
    total = float(evals.sum())
    info["reduce"] = "pc1"
    info["explained_var_frac"] = (round(float(evals[-1]) / total, 6)
                                  if np.isfinite(total) and total > 0 else None)

    # PCA sign is arbitrary: -v is as valid an eigenvector as v, so half of all runs would
    # deliver a contrast-inverted map. Fix it against the plain band mean. Both PC1 and
    # that mean are linear in the centred bands, so the correlation that decides the sign
    # comes straight out of the k x k covariance eigh already formed — no second pass over
    # the pixels:  cov(w.X, mean(X)) = w^T C u, with u = 1/k.
    u = np.full(len(keep), 1.0 / len(keep))
    cross = float(weights @ cov @ u)
    spread = float(weights @ cov @ weights) * float(u @ cov @ u)
    corr = cross / np.sqrt(spread) if spread > 0 else 0.0
    info["sign_flipped"] = bool(corr < 0)
    info["pc1_band_mean_corr"] = round(abs(float(corr)), 4) if spread > 0 else None
    if corr < 0:
        weights = -weights

    return _stream_full_res(src, keep, weights, means, shape, nodata), info
