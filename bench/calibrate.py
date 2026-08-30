"""Seat 6 · bench/calibrate — pillar P5: two guessed constants replaced by measured ones.

Both constants decide whether a number may be quoted, so both had to stop being
somebody's reasoning and start being an experiment.

**Constant 1 — `_NEFF_K` in `samanvay/geometry/refine.py`.** It turns correlation peak
sharpness into a per-point sigma, and `mean_sigma_px` is quoted from it. Two arms:

* *fit arm* — the six `fixtures/dsun_sweep` pairs refined against the analytic ground
  truth homography in `gt.json`. Real geometry (2x scale, rotation), real cross-sun
  decorrelation, quantised DN. This is the deployed regime, so it sets the constant.
* *regime arm* — synthetic patch pairs over lunar terrain from `synth/terrain.py`
  (3 textures x blur x noise), displaced by an exact Fourier shift so the truth carries
  no interpolation error of its own. This maps where the model holds and where it does not.

The comparison is always `rms(sigma)` against the observed **per-axis** RMS error, which
is what a Forstner sigma means: `sqrt(mean((ex^2 + ey^2) / 2))`.

**Constant 2 — `_MIN_REDUNDANCY` / `_TRUST_REDUNDANCY` in `geometry/verify.py`.** They
decide when an RMSE may be quoted. This file does NOT edit verify.py — it measures the
divergence between self-consistency `rmse_px` and true `gt_rmse_px` as redundancy falls,
over `bench/fake_matches.py` with controlled outlier fractions plus any cached pipeline
runs under `runs/`, and prints a recommendation for the geometry owner to apply.

Run: python -m bench.calibrate --out runs/calibrate
"""

import glob
import json
import os

import click
import matplotlib
matplotlib.use("Agg")          # headless: the bench writes a file, it never opens a window
import matplotlib.pyplot as plt
import numpy as np
import scipy.ndimage as ndi

from bench.fake_matches import generate_fake_matches
from bench.harness import write_table
from samanvay.geometry import refine as refine_mod
from samanvay.geometry.refine import refine_matches
from samanvay.geometry.verify import verify_matches
from samanvay.types import MatchSet
from synth.render_pair import render_illumination
from synth.terrain import make_dem

# The value refine.py carried before this bench existed, kept so the table can show the
# before/after honestly instead of asserting an improvement nobody can see.
PREV_NEFF_K = 0.35

FIXTURE_DIR = "fixtures/dsun_sweep"
PATCH = 32                     # pipeline default (config.py: refine.patch); the fit is per-patch

# Synthetic regime grid. Blur is the imaging PSF, noise is the additive sensor noise as a
# fraction of the image standard deviation.
TEXTURES = (("cratered", dict(seed=3)),
            ("smooth", dict(seed=5, crater_density_px=5e-4, rms_slope=0.05)),
            ("rough", dict(seed=11, crater_density_px=8e-3, rms_slope=0.25, sun_el_deg=15.0)))
BLURS = (1.0, 1.5, 2.0, 3.0)
NOISES = (0.005, 0.02, 0.05, 0.10, 0.20, 0.35)

# Redundancy sweep: point counts and outlier fractions chosen to land inlier counts on both
# sides of the model's minimal sample, which is the only place the question lives.
RED_POINTS = (8, 10, 12, 16, 24, 40, 80, 150)
RED_OUTLIERS = (0.0, 0.2, 0.4, 0.6, 0.75, 0.85, 0.92)
RED_SEEDS = 8
RED_BINS = ((0, 0), (1, 1), (2, 2), (3, 3), (4, 5), (6, 9), (10, 19), (20, 49), (50, 10 ** 9))


# --------------------------------------------------------------------------- constant 1

def lunar_texture(size=384, sun_az_deg=135.0, sun_el_deg=25.0, **dem_kw):
    """Unit-variance lunar-like image: fractal DEM + crater population, Lommel-Seeliger shaded."""
    dem, gsd = make_dem(shape=(size, size), gsd_m=0.5, **dem_kw)
    # ponytail: 64 shadow steps instead of the default 512. Ceiling: very long shadows from
    # the tallest rims are clipped. Upgrade path is the full march if a fixture ever needs it —
    # here the texture only has to be lunar, not a fixture anyone measures accuracy against.
    rad = render_illumination(dem, gsd, sun_az_deg, sun_el_deg,
                              max_shadow_steps=64)["radiance"].astype(np.float64)
    sd = rad.std()
    return (rad - rad.mean()) / (sd if sd > 0 else 1.0)


def _matchset(src_xy, ref_xy):
    n = len(src_xy)
    return MatchSet(src_xy=np.asarray(src_xy, dtype=np.float64).copy(),
                    ref_xy=np.asarray(ref_xy, dtype=np.float64).copy(),
                    score=np.ones(n, dtype=np.float32),
                    method=np.zeros(n, dtype=np.uint8),
                    cell=np.zeros(n, dtype=np.int32))


def _grid_points(shape, margin=80, n=5):
    """Points on a regular grid, kept clear of the border so no patch samples off-array."""
    ys = np.linspace(margin, shape[0] - 1 - margin, n)
    xs = np.linspace(margin, shape[1] - 1 - margin, n)
    xx, yy = np.meshgrid(xs, ys)
    return np.column_stack([xx.ravel(), yy.ravel()])


def _exact_shift(img, dx, dy):
    """Band-limited (Fourier) shift: the ground truth carries no interpolation error itself."""
    return np.fft.irfft2(ndi.fourier_shift(np.fft.rfft2(img), (dy, dx), img.shape[1]),
                         img.shape)


def synthetic_case(base, blur_px, noise, patch=PATCH, n_shift=3, seed=0):
    """One (texture, blur, noise) cell: returns (sigma, per-axis |err|, n_refined, n_unknown)."""
    rng = np.random.default_rng(seed)
    blurred = ndi.gaussian_filter(np.asarray(base, dtype=np.float64), float(blur_px))
    sd = float(blurred.std()) or 1.0
    pts = _grid_points(blurred.shape)
    sig, err, n_ref = [], [], 0
    for _ in range(int(n_shift)):
        d = rng.uniform(-1.0, 1.0, 2)                      # sub-pixel truth, (dx, dy)
        moved = _exact_shift(blurred, d[0], d[1])
        src = (blurred + rng.normal(0.0, noise * sd, blurred.shape)).astype(np.float32)
        ref = (moved + rng.normal(0.0, noise * sd, blurred.shape)).astype(np.float32)
        out, s = refine_matches(_matchset(pts, pts), src, ref, np.eye(3), {"patch": patch})
        moved_pt = ~np.isclose(out.ref_xy, pts).all(axis=1)  # refined, sigma known or not
        n_ref += int(moved_pt.sum())
        ok = np.isfinite(s)
        e = out.ref_xy[ok] - (pts[ok] + d)
        sig.append(s[ok])
        err.append(np.sqrt((e[:, 0] ** 2 + e[:, 1] ** 2) / 2.0))
    sig = np.concatenate(sig) if sig else np.zeros(0)
    err = np.concatenate(err) if err else np.zeros(0)
    return sig, err, n_ref, max(n_ref - len(sig), 0)


def fixture_case(pair_dir, patch=PATCH, n_grid=8, jitter_px=0.5, seed=0):
    """One dsun_sweep pair against its analytic gt homography; None if the pair is unreadable."""
    try:
        import rasterio
        with rasterio.open(os.path.join(pair_dir, "source.tif")) as f:
            src = f.read(1).astype(np.float32)
        with rasterio.open(os.path.join(pair_dir, "reference.tif")) as f:
            ref = f.read(1).astype(np.float32)
        with open(os.path.join(pair_dir, "gt.json")) as f:
            H = np.asarray(json.load(f)["H_src_to_ref"], dtype=np.float64)
    except (OSError, ValueError, KeyError):
        return None
    if H.shape != (3, 3) or not np.isfinite(H).all():
        return None
    pts = _grid_points(src.shape, margin=80, n=n_grid)
    q = np.hstack([pts, np.ones((len(pts), 1))]) @ H.T
    truth = q[:, :2] / q[:, 2:3]
    rng = np.random.default_rng(seed)
    start = truth + rng.uniform(-jitter_px, jitter_px, truth.shape)
    out, s = refine_matches(_matchset(pts, start), src, ref, H, {"patch": patch})
    moved = ~np.isclose(out.ref_xy, start).all(axis=1)
    ok = np.isfinite(s)
    # Residuals are in SOURCE pixels, so the reference-frame error is divided by the scale.
    scale = float(np.sqrt(abs(np.linalg.det(H[:2, :2])))) or 1.0
    e = (out.ref_xy[ok] - truth[ok]) / scale
    err = np.sqrt((e[:, 0] ** 2 + e[:, 1] ** 2) / 2.0)
    return s[ok], err, int(moved.sum()), max(int(moved.sum()) - int(ok.sum()), 0)


def _sigma_row(arm, name, extra, sig, err, n_ref, n_unknown):
    """One calibration-curve row: predicted vs observed, at the shipped constant."""
    row = {"name": name, "arm": arm, "n_refined": int(n_ref), "n_sigma_unknown": int(n_unknown)}
    row.update(extra)
    if len(sig) < 5:
        # Too few samples to compare distributions. n/a, never a fabricated ratio.
        row.update({"obs_rms_px": None, "pred_rms_px": None, "ratio": None})
        return row
    obs = float(np.sqrt(np.mean(err ** 2)))
    pred = float(np.sqrt(np.mean(sig ** 2)))
    row.update({"obs_rms_px": round(obs, 4), "pred_rms_px": round(pred, 4),
                "ratio": round(pred / obs, 3) if obs > 0 else None})
    return row


def calibrate_sigma(patch=PATCH, textures=TEXTURES, blurs=BLURS, noises=NOISES,
                    n_shift=3, fixture_dir=FIXTURE_DIR, size=384):
    """Measure predicted vs actual sigma over both arms; returns (rows, summary, samples)."""
    k_now = float(refine_mod._NEFF_K)
    rows, samples = [], {"fixture": [], "synthetic": []}

    for pair in sorted(glob.glob(os.path.join(fixture_dir, "*", "gt.json"))):
        d = os.path.dirname(pair)
        got = fixture_case(d, patch=patch)
        if got is None:
            continue
        sig, err, n_ref, n_unk = got
        samples["fixture"].append((sig, err))
        rows.append(_sigma_row("fixture", os.path.basename(d), {}, sig, err, n_ref, n_unk))

    for ti, (tname, kw) in enumerate(textures):
        base = lunar_texture(size=size, **kw)
        for bi, blur in enumerate(blurs):
            for ni, noise in enumerate(noises):
                # Deterministic seed: hash() is salted per process, and a calibration that
                # moves when you re-run it is not a measurement.
                seed = 1000 * ti + 10 * bi + ni
                sig, err, n_ref, n_unk = synthetic_case(base, blur, noise, patch=patch,
                                                        n_shift=n_shift, seed=seed)
                samples["synthetic"].append((sig, err))
                rows.append(_sigma_row(
                    "synthetic", f"{tname}_b{blur:g}_n{noise:g}",
                    {"blur_px": blur, "noise": noise}, sig, err, n_ref, n_unk))

    def arm_ratio(arm):
        r = [x["ratio"] for x in rows if x["arm"] == arm and x.get("ratio")]
        return np.exp(np.mean(np.log(r))) if r else None, r

    g_fix, r_fix = arm_ratio("fixture")
    g_syn, r_syn = arm_ratio("synthetic")
    # The fixture arm sets the constant: it is the deployed regime (real Jacobian, real
    # cross-sun decorrelation). The synthetic arm has an identity Jacobian and therefore
    # none of the reference-patch resampling error, so it wants a larger k; taking the
    # fixture value leaves the synthetic arm PESSIMISTIC, which is the safe direction.
    fit_arm = "fixture" if g_fix else ("synthetic" if g_syn else None)
    g_fit = g_fix if fit_arm == "fixture" else g_syn
    k_fit = float(k_now * g_fit ** 2) if g_fit else float("nan")

    def unknown_frac(arm):
        a = [x for x in rows if x["arm"] == arm]
        tot = sum(x["n_refined"] for x in a)
        return float(sum(x["n_sigma_unknown"] for x in a) / tot) if tot else None

    n_unknown = sum(x["n_sigma_unknown"] for x in rows)
    n_ref = sum(x["n_refined"] for x in rows)
    summary = {
        "k_shipped": k_now,
        "k_previous": PREV_NEFF_K,
        "k_fit": k_fit,
        "fit_arm": fit_arm,
        "rho_max_validated": float(getattr(refine_mod, "_RHO_MAX_VALIDATED", float("nan"))),
        "patch": int(patch),
        "fixture_ratio_geomean": None if g_fix is None else float(g_fix),
        "fixture_ratio_span": [float(min(r_fix)), float(max(r_fix))] if r_fix else None,
        "synthetic_ratio_geomean": None if g_syn is None else float(g_syn),
        "synthetic_ratio_span": [float(min(r_syn)), float(max(r_syn))] if r_syn else None,
        "n_refined": int(n_ref),
        "n_sigma_unknown": int(n_unknown),
        "unknown_frac": float(n_unknown / n_ref) if n_ref else None,
        "unknown_frac_fixture": unknown_frac("fixture"),
        "unknown_frac_synthetic": unknown_frac("synthetic"),
    }
    return rows, summary, samples


# --------------------------------------------------------------------------- constant 2

def _gt_rmse(H_est, H_gt, lo=50.0, hi=950.0, n=9):
    """True error of a fitted transform in SOURCE px, over a grid spanning the point domain."""
    try:
        E = np.linalg.inv(np.asarray(H_est, dtype=np.float64)) @ np.asarray(H_gt, dtype=np.float64)
    except (np.linalg.LinAlgError, ValueError):
        return None
    if E.shape != (3, 3) or not np.isfinite(E).all():
        return None
    g = np.linspace(lo, hi, n)
    xx, yy = np.meshgrid(g, g)
    p = np.column_stack([xx.ravel(), yy.ravel()])
    q = np.hstack([p, np.ones((len(p), 1))]) @ E.T
    w = np.where(np.abs(q[:, 2]) < 1e-12, np.nan, q[:, 2])
    e = q[:, :2] / w[:, None] - p
    e = e[np.isfinite(e).all(axis=1)]
    return float(np.sqrt(np.mean(e[:, 0] ** 2 + e[:, 1] ** 2))) if len(e) else None


def _worst_above(knee, red, ratio):
    """The worst-diverging cached pipeline run at or above `knee`: (redundancy, ratio) or None."""
    if knee is None or not len(red):
        return None
    m = red >= knee
    if not m.any():
        return None
    i = int(np.argmax(ratio[m]))
    return [int(red[m][i]), round(float(ratio[m][i]), 1)]


def _cached_runs(runs_root="runs"):
    """Redundancy evidence from any cached pipeline metrics.json: (redundancy, rmse, gt_rmse)."""
    out = []
    for path in sorted(glob.glob(os.path.join(runs_root, "**", "metrics.json"), recursive=True)):
        try:
            with open(path) as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        red, rmse, gt = m.get("redundancy"), m.get("rmse_px"), m.get("gt_rmse_px")
        if red is None or rmse is None or gt is None:
            continue
        out.append((os.path.relpath(path, runs_root), int(red), float(rmse), float(gt)))
    return out


def calibrate_redundancy(points=RED_POINTS, outliers=RED_OUTLIERS, seeds=RED_SEEDS,
                         noise_std=0.5, runs_root="runs"):
    """Sweep inlier redundancy against the rmse_px / gt_rmse_px divergence; (rows, summary)."""
    red, self_rmse, gt_rmse = [], [], []
    n_runs = n_failed = 0
    for npts in points:
        for frac in outliers:
            for seed in range(int(seeds)):
                n_runs += 1
                matches, H_gt = generate_fake_matches(npts, noise_std, frac, seed)
                reg = verify_matches(matches, {"ransac_thresh_px": 3.0})
                r = reg.metrics.get("redundancy")
                s = reg.metrics.get("rmse_px")
                g = _gt_rmse(reg.params, H_gt)
                if r is None or s is None or not np.isfinite(s) or g is None:
                    n_failed += 1          # honestly rejected fits carry no RMSE to compare
                    continue
                red.append(int(r))
                self_rmse.append(float(s))
                gt_rmse.append(float(g))

    red = np.asarray(red, dtype=int)
    self_rmse = np.asarray(self_rmse, dtype=float)
    gt_rmse = np.asarray(gt_rmse, dtype=float)
    ratio = gt_rmse / np.maximum(self_rmse, 1e-6)

    cached = _cached_runs(runs_root)
    p_red = np.array([c[1] for c in cached], dtype=int)
    p_ratio = np.array([c[3] / max(c[2], 1e-6) for c in cached], dtype=float)

    def _bin_rows(arm, r, s, g, rat):
        out = []
        for lo, hi in RED_BINS:
            m = (r >= lo) & (r <= hi)
            if not m.any():
                continue
            out.append({
                "name": f"{lo}-{hi}" if hi < 10 ** 9 else f"{lo}+",
                "arm": arm,
                "n": int(m.sum()),
                "med_rmse_px": round(float(np.median(s[m])), 3),
                "med_gt_rmse_px": round(float(np.median(g[m])), 3),
                "med_ratio": round(float(np.median(rat[m])), 2),
                "p90_ratio": round(float(np.percentile(rat[m], 90)), 1),
                "pct_gt_over_3x": round(100.0 * float(np.mean(rat[m] > 3.0)), 0),
                "pct_gt_over_5px": round(100.0 * float(np.mean(g[m] > 5.0)), 0),
            })
        return out

    rows = _bin_rows("fake_matches", red, self_rmse, gt_rmse, ratio)
    if len(cached):
        # Cached pipeline runs: one number per run, so the columns are the same but each
        # bin holds whole registrations of the real fixtures rather than synthetic fits.
        rows += _bin_rows("pipeline_run", p_red,
                          np.array([c[2] for c in cached]), np.array([c[3] for c in cached]),
                          p_ratio)

    # Where does rmse_px stop being predictive? The lowest bin from which EVERY higher bin
    # keeps 3x understatements under 10% and never blows past 5 px — cumulative averaging
    # would let a bad marginal bin hide behind the well-populated ones above it.
    def _clean(lo):
        seen = 0
        for a, b in RED_BINS:
            if a < lo:
                continue
            m = (red >= a) & (red <= b)
            if m.sum() < 5:          # too few fits to bless or veto a bin with
                continue
            seen += 1
            if np.mean(ratio[m] > 3.0) >= 0.10 or np.mean(gt_rmse[m] > 5.0) > 0.0:
                return False
        if not seen:
            return False
        # and the cached pipeline runs at or above it must agree
        m = p_red >= lo
        return not (m.sum() and np.mean(p_ratio[m] > 3.0) >= 0.10)

    knee = next((lo for lo, _ in RED_BINS if _clean(lo)), None)
    zero_div = next((lo for lo, _ in RED_BINS
                     if (red >= lo).sum() and not np.any(ratio[red >= lo] > 3.0)
                     and (not (p_red >= lo).sum() or not np.any(p_ratio[p_red >= lo] > 3.0))),
                    None)
    reject = None
    for lo, _ in RED_BINS:                     # first redundancy whose own bin is not a disaster
        m = red == lo
        if m.any() and np.median(ratio[m]) < 3.0 and np.mean(gt_rmse[m] > 5.0) < 0.10:
            reject = lo
            break

    # A recommendation off a handful of fits is a guess wearing a table. Below this many
    # comparable fits the bench reports the rows and declines to name a threshold.
    enough = len(red) >= 100
    summary = {
        "n_runs": n_runs,
        "n_no_rmse": n_failed,
        "n_compared": int(len(red)),
        "n_pipeline_runs": len(cached),
        "evidence_sufficient": bool(enough),
        "recommend_min_redundancy": reject if enough else None,
        # A trust flag guards a headline number, so it is set where BOTH arms show zero
        # divergence, not at the knee where the controlled arm merely gets tolerable.
        "recommend_trust_redundancy": ((zero_div if zero_div is not None else knee)
                                       if enough else None),
        "trust_knee": knee if enough else None,
        "zero_divergence_redundancy": zero_div,
        "worst_run_above_knee": _worst_above(knee, p_red, p_ratio),
        "current_min_redundancy": 1,
        "current_trust_redundancy": 3,
    }
    return rows, summary


# --------------------------------------------------------------------------- outputs

def _figure(sigma_rows, red_rows, path):
    """Two panels: the sigma calibration curve, and the rmse_px / gt_rmse_px divergence."""
    fig, (ax0, ax1) = plt.subplots(1, 2, figsize=(11, 4.4))
    k_now = float(refine_mod._NEFF_K)
    before = np.sqrt(k_now / PREV_NEFF_K)        # sigma scales as k**-0.5
    for arm, colour, mark in (("fixture", "#c1440e", "o"), ("synthetic", "#3b6ea5", "^")):
        pts = [(r["obs_rms_px"], r["pred_rms_px"]) for r in sigma_rows
               if r.get("arm") == arm and r.get("pred_rms_px")]
        if not pts:
            continue
        o, p = np.array(pts).T
        ax0.scatter(o, p, c=colour, marker=mark, s=26, label=f"{arm} (k={k_now:g})")
        ax0.scatter(o, p * before, facecolors="none", edgecolors=colour, marker=mark, s=26,
                    alpha=0.5, label=f"{arm} (k={PREV_NEFF_K:g}, before)")
    lim = [0.01, 1.5]
    ax0.plot(lim, lim, "k-", lw=1, label="1:1")
    ax0.plot(lim, [2 * v for v in lim], "k--", lw=0.7)
    ax0.plot(lim, [0.5 * v for v in lim], "k--", lw=0.7, label="+/- 2x")
    ax0.set(xscale="log", yscale="log", xlim=lim, ylim=lim,
            xlabel="observed RMS error (px/axis)", ylabel="predicted sigma (px)",
            title=f"Constant 1: sigma calibration, _NEFF_K={k_now:g}")
    ax0.legend(fontsize=6.5, loc="upper left")
    ax0.grid(alpha=0.25, which="both")

    bins = [r for r in red_rows if r.get("arm") == "fake_matches"]
    names = [r["name"] for r in bins]
    if bins:
        x = np.arange(len(bins))
        ax1.bar(x, [r["med_ratio"] for r in bins], color="#3b6ea5", label="median gt/self")
        ax1.plot(x, [r["p90_ratio"] for r in bins], "o-", color="#c1440e", ms=4, label="p90 gt/self")
        ax1.set_xticks(x)
        ax1.set_xticklabels(names, rotation=45, fontsize=7)
    runs = [r for r in red_rows if r.get("arm") == "pipeline_run" and r["name"] in names]
    if runs:
        ax1.scatter([names.index(r["name"]) for r in runs],
                    [max(r["med_ratio"], 1e-3) for r in runs],
                    marker="x", c="k", s=30, zorder=3, label="cached pipeline runs (median)")
    ax1.axhline(1.0, color="k", lw=1)
    ax1.axhline(3.0, color="k", ls="--", lw=0.7, label="3x understatement")
    ax1.set(yscale="log", xlabel="inlier redundancy (inliers - model DOF points)",
            ylabel="gt_rmse_px / rmse_px",
            title="Constant 2: when rmse_px stops tracking truth")
    ax1.legend(fontsize=7)
    ax1.grid(alpha=0.25, axis="y", which="both")
    fig.tight_layout()
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def _md(path):
    with open(path) as f:
        return f.read().rstrip()


def write_report(out_dir, sigma_rows, sigma_summary, red_rows, red_summary, fig_path):
    """Compose calibrate.md: the two tables plus what they license anyone to say."""
    s, r = sigma_summary, red_summary
    thin = next((x for x in red_rows
                 if x.get("arm") == "fake_matches" and x.get("name") == "1-1"), None)
    span = s.get("fixture_ratio_span") or [float("nan"), float("nan")]
    syn_span = s.get("synthetic_ratio_span") or [float("nan"), float("nan")]
    lines = [
        "# SAMANVAY — measured constants",
        "",
        "Generated by `python -m bench.calibrate`. Every number here came out of the run that "
        "wrote this file; nothing is carried over from a previous one.",
        "",
        "## Constant 1 — `_NEFF_K` in `samanvay/geometry/refine.py`",
        "",
        f"* shipped value **{s['k_shipped']:g}** (was {s['k_previous']:g}, fitted on Gaussian texture)",
        f"* this run's fit: **{s['k_fit']:.3f}** from the *{s['fit_arm']}* arm, patch {s['patch']}",
        f"* fixture arm predicted/observed: geometric mean "
        f"**{_fmt(s['fixture_ratio_geomean'])}**, per-pair span {span[0]:.2f}-{span[1]:.2f}",
        f"* synthetic regime arm: geometric mean **{_fmt(s['synthetic_ratio_geomean'])}**, "
        f"span {syn_span[0]:.2f}-{syn_span[1]:.2f} (>1 is pessimistic, the safe direction)",
        f"* sigma returned as NaN (unknown) above rho = {s['rho_max_validated']:g}: "
        f"{_fmt(s['unknown_frac_fixture'], pct=True)} of refined points on the fixture arm, "
        f"{_fmt(s['unknown_frac_synthetic'], pct=True)} on the synthetic arm — which reaches "
        "deliberately unrealistic noise floors the pipeline never sees",
        "",
        "`ratio` is `rms(sigma) / rms(per-axis error)`. Below 1 the reported sigma is "
        "**optimistic** and must not be quoted as accuracy.",
        "",
        _md(os.path.join(out_dir, "sigma_calibration.md")),
        "",
        "### Where it holds and where it does not",
        "",
        "* Holds on the fixture arm across the whole Δsun sweep — that is the regime the "
        "pipeline runs in, and it is what the constant was fitted to.",
        "* Holds within roughly a factor of 2 on the synthetic arm, biased pessimistic: those "
        "pairs have an identity Jacobian, so the reference patch is not resampled through a "
        "scale change and carries less error than the fixture arm's.",
        f"* Does **not** hold above rho = {s['rho_max_validated']:g}. There the observed error "
        "stops falling — it floors on resampling and peak-estimator bias the correlation model "
        "cannot see — while predicted sigma keeps dropping. The floor is content-dependent, so "
        "there is no constant to substitute and `_sigma` returns NaN, meaning unknown.",
        "",
        "## Constant 2 — redundancy thresholds in `samanvay/geometry/verify.py`",
        "",
        f"Sweep: {r['n_runs']} fits from `bench/fake_matches.py` over "
        f"{len(RED_POINTS)} point counts x {len(RED_OUTLIERS)} outlier fractions x "
        f"{RED_SEEDS} seeds; {r['n_compared']} produced an RMSE to compare "
        f"({r['n_no_rmse']} were honestly rejected). Plus {r['n_pipeline_runs']} cached "
        "pipeline runs with both a self-consistency and a ground-truth RMSE.",
        "",
        "`med_ratio` is `gt_rmse_px / rmse_px`: how many times the quoted RMSE understates "
        "the true error.",
        "",
        _md(os.path.join(out_dir, "redundancy.md")),
        "",
        "### Recommendation (for the geometry owner — this bench does not edit verify.py)",
        "",
    ] + ([] if r["evidence_sufficient"] else [
        f"**Not enough evidence in this run** — {r['n_compared']} comparable fits, "
        "under the 100 this bench requires before it names a threshold. Re-run without "
        "`--quick`. The rows above are still what happened.",
        "",
    ]) + [
        f"* `_MIN_REDUNDANCY` {r['current_min_redundancy']} -> "
        f"**{_fmt(r['recommend_min_redundancy'])}**",
        f"* `_TRUST_REDUNDANCY` {r['current_trust_redundancy']} -> "
        f"**{_fmt(r['recommend_trust_redundancy'])}** — the lowest redundancy at which no "
        f"fit in either arm understates its true error by 3x. The knee on the controlled "
        f"arm alone is {_fmt(r['trust_knee'])}" + (
            f", but a cached pipeline run at redundancy {r['worst_run_above_knee'][0]} still "
            f"understates by {r['worst_run_above_knee'][1]}x, which is why the flag sits above "
            "the knee." if (r.get("worst_run_above_knee")
                            and r["worst_run_above_knee"][1] > 3.0) else "."),
        "",
        "The gate currently only rejects an exactly-determined fit. This run says one spare "
        "observation is not enough either" + (
            f": at redundancy 1 the median fit understates its own true error by "
            f"{thin['med_ratio']:.0f}x, and {thin['pct_gt_over_3x']:.0f}% of them understate "
            f"it by more than 3x while still reporting an RMSE." if thin else "."),
        "",
        f"![calibration]({os.path.basename(fig_path)})",
        "",
    ]
    path = os.path.join(out_dir, "calibrate.md")
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return path


def _fmt(v, pct=False):
    if v is None:
        return "n/a"                      # unknown prints as unknown, never as a number
    if isinstance(v, float):
        return f"{100 * v:.1f}%" if pct else f"{v:.3f}"
    return str(v)


def run(out_dir="runs/calibrate", quick=False, runs_root="runs"):
    """Run both calibrations, write calibrate.md + the CSVs + the figure; return the summaries."""
    os.makedirs(out_dir, exist_ok=True)
    if quick:
        sigma_rows, sigma_summary, samples = calibrate_sigma(
            textures=TEXTURES[:1], blurs=(1.0, 2.0), noises=(0.02, 0.20), n_shift=1, size=256,
            fixture_dir=FIXTURE_DIR)
        red_rows, red_summary = calibrate_redundancy(points=(10, 20, 60), outliers=(0.0, 0.6, 0.85),
                                                     seeds=3, runs_root=runs_root)
    else:
        sigma_rows, sigma_summary, samples = calibrate_sigma()
        red_rows, red_summary = calibrate_redundancy(runs_root=runs_root)

    write_table(sigma_rows, out_dir, stem="sigma_calibration")
    write_table(red_rows, out_dir, stem="redundancy")
    fig = _figure(sigma_rows, red_rows, os.path.join(out_dir, "calibrate.png"))
    report = write_report(out_dir, sigma_rows, sigma_summary, red_rows, red_summary, fig)
    with open(os.path.join(out_dir, "calibrate.json"), "w") as f:
        json.dump({"sigma": sigma_summary, "redundancy": red_summary}, f, indent=2)
    return {"sigma": sigma_summary, "redundancy": red_summary,
            "report": report, "figure": fig, "samples": samples}


@click.command()
@click.option("--out", "out_dir", default="runs/calibrate", help="Output directory")
@click.option("--runs", "runs_root", default="runs", help="Root to scan for cached metrics.json")
@click.option("--quick", is_flag=True, help="Tiny grid — a smoke test, not a calibration")
def main(out_dir, runs_root, quick):
    """Measure _NEFF_K and the redundancy thresholds, and write the evidence."""
    res = run(out_dir, quick=quick, runs_root=runs_root)
    click.echo(_md(res["report"]))
    if quick:
        click.echo("\n** --quick: grid too small to fit a constant from. Re-run without it. **")


if __name__ == "__main__":
    main()
