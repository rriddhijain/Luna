"""Seat 6 · Pillar P4 · feature C9 — least-squares matching (LSM).

The classical photogrammetric sub-pixel refiner, and the alternative to the
correlation-peak method in `geometry/refine.py`.  For each tie point it fits, by
Gauss-Newton on the raw intensity residual,

    g * ref(A q + t) + o  ~=  src(q + p_src)

over a w x w patch, where `q` runs over patch offsets in SOURCE pixels, `A` (2x2) and
`t` (2-vector, reference pixels) are the local affine warp, and `g`, `o` are a
radiometric gain and offset.  Eight parameters, one per column of the design matrix.

Why bother, given phase correlation already works: correlation assumes the two patches
differ by a pure translation and by nothing radiometric.  A cross-mission pair differs
by a local scale and shear (different GSD, different look angle) and by a wholly
different brightness transfer (different sensor, different sun).  LSM models both
instead of assuming them away.

**MEASURED against `geometry/refine.refine_matches` on 2026-08-30**, same points, same
patch (32), same 64-point grid started 0.5 px off the analytic truth in `gt.json`.
Errors are true errors against that truth, in source pixels; `kept` is points the method
accepted (both methods leave the rest untouched).

    pair            phase correlation            LSM
    dsun_00         0.138 px, 61/64, 0.6 ms      0.065 px, 64/64, 1.4 ms
    dsun_50         1.201 px, 54/64, 0.4 ms      0.606 px, 41/64, 2.1 ms
    synth_pair_A    3.474 px, 26/64, 0.3 ms      2.461 px,  1/64, 3.0 ms

Read it in three parts.  (1) **On the points it accepts, LSM is about 2x more accurate,
everywhere** — including the Δ50° sun pair, which is the case this project exists for.
(2) **It accepts fewer**, and the gap widens with illumination difference: 100% / 64% /
2%.  (3) On `synth_pair_A` (Δ90° azimuth *and* Δ40° elevation) phase correlation makes
the tie points **worse than not refining at all** — 0.86 px in, 2.32 px out over all 64
points — while LSM refuses almost everything and leaves the set at 0.91 px.  Its
rejection is the useful half of the result there, not a failure of it.

The same ordering holds on canonicalised albedo (the array the pipeline actually passes)
to within 0.01 px, and on a pure sub-pixel translation with exact Fourier-shift truth
LSM wins wherever the imagery is blurred or noisy (blur 2 px: 0.058 vs 0.096; blur 1 px
+ 30% noise: 0.041 vs 0.062) and loses slightly on sharp, clean texture (0.029 vs 0.026)
where there is no local geometry left to model.

So: **not a free replacement.** LSM buys accuracy and honesty with yield and ~4x
runtime. Phase correlation remains the right default for bulk tie-point refinement;
LSM is the right tool when a smaller number of accurate, self-assessed points is worth
more than a larger number of approximate ones.

**Initialisation.** `A` starts at the local 2x2 Jacobian of `H` at the source point, so
a 2x-scale pair starts on the right sheet instead of walking there.  `t` starts at the
incoming `ref_xy`.

**Uncertainty.** `sigma` is the standard photogrammetric a-posteriori estimate, derived
from the adjustment itself and not from any calibration constant:

    sigma0^2 = v'v / (n - u),  Cxx = sigma0^2 * inv(N),  sigma = rms(sd(t_x), sd(t_y))

with n = w*w observations, u = 8 parameters, N the (undamped) normal matrix of the
final iteration.  Because the increment is parametrised in patch coordinates, and patch
coordinates ARE source pixels by construction, the translation block of Cxx is already
in source pixels; no scale conversion is applied or needed.  It is NaN wherever it is
not computable (rejected point, singular N, no redundancy) and is never clipped to a
plausible-looking range.

MEASURED honesty of that sigma, rms(sigma) over the true per-axis RMS error, same runs
as above: **0.79 on dsun_00, 0.26 on dsun_50** (1.0 would be honest; below 1.0 is
optimistic). So it is trustworthy to about 25% when the two images are illuminated
alike, and reads 4x optimistic when they are not — because there the residual is a
systematic illumination difference, not the white noise the estimate assumes.  Quote it
with that bias attached, or not at all on cross-sun pairs.

**Rejection.** A runaway warp that "converges" somewhere wrong is the classic LSM
failure, so acceptance needs all of: convergence inside `max_iter`, a final correlation
above `min_corr`, a total translation inside `max_shift_px`, and an affine that has not
drifted more than `max_affine_drift_px` (measured as corner displacement) from its
initialisation.  A rejected point keeps its original coordinates and gets NaN sigma.
"""

import numpy as np

# Reused rather than re-implemented: the patch-cutting, bounds-margin and image-unwrapping
# conventions must not drift between the two refiners, or their measured comparison stops
# being a comparison of the estimators.
from samanvay.geometry.refine import _WARPABLE, _as_array, _jacobian, _sample_patch
from samanvay.types import MatchSet

_N_PARAMS = 8
_MAX_COND = 1e10        # above this the normal matrix is not solvable to useful precision
_MIN_GAIN = 0.05        # a collapsing gain kills the geometric derivatives: bail, do not divide
_LAM0 = 1e-3            # Marquardt damping, relative to diag(N)
_LAM_MAX = 1e8


def _corr(a, b):
    """Pearson correlation of two flat arrays; 0.0 if either is constant."""
    a = a - a.mean()
    b = b - b.mean()
    da, db = a.std(), b.std()
    if not np.isfinite(da) or not np.isfinite(db) or da < 1e-12 or db < 1e-12:
        return 0.0
    return float(np.clip(np.mean(a * b) / (da * db), -1.0, 1.0))


def _solve(Jm, res, lam):
    """Damped normal-equation step. Returns (dp, N) or (None, None) if unusable."""
    # einsum, not `@`: matmul on these tall arrays routes through BLAS, whose SIMD tail
    # reads uninitialised lanes and emits spurious divide/overflow/invalid warnings on
    # every call. Same false positive geometry/init.py and photometry/shading.py dodge.
    N = np.einsum("ij,ik->jk", Jm, Jm)
    if not np.isfinite(N).all():
        return None, None
    d = np.diag(N)
    if (d <= 0).any():
        return None, None                     # a parameter the data cannot see at all
    Nd = N + lam * np.diag(d)
    if np.linalg.cond(Nd) > _MAX_COND:
        return None, None
    try:
        dp = np.linalg.solve(Nd, -np.einsum("ij,i->j", Jm, res))
    except np.linalg.LinAlgError:
        return None, None
    return (dp, N) if np.isfinite(dp).all() else (None, None)


def _fail(reason, iters=0):
    return {"ok": False, "reason": reason, "iters": int(iters),
            "sigma": float("nan"), "rho": float("nan"), "xy": None}


def _lsm_point(src, ref, sx, sy, rx, ry, A0, w, opt):
    """Fit one tie point. Returns a dict: ok, reason, iters, sigma, rho, xy."""
    r = (w - 1) / 2.0
    xx, yy = np.meshgrid(np.arange(w) - r, np.arange(w) - r)   # patch coords == source px
    uu, vv = xx.ravel(), yy.ravel()
    corners = np.array([[-r, -r], [r, -r], [-r, r], [r, r]])

    S = _sample_patch(src, sx, sy, np.eye(2), w)
    if S is None:
        return _fail("source_oob")
    S = np.asarray(S, dtype=np.float64)
    if not np.isfinite(S).all() or S.std() < 1e-8:
        return _fail("source_flat")
    S = ((S - S.mean()) / S.std()).ravel()

    A = A0.copy()
    t0 = np.array([rx, ry], dtype=np.float64)
    t = t0.copy()
    R = _sample_patch(ref, t[0], t[1], A, w)
    if R is None:
        return _fail("ref_oob")
    R = np.asarray(R, dtype=np.float64)
    if not np.isfinite(R).all() or R.std() < 1e-8:
        return _fail("ref_flat")
    # Fixed normalisation constants: g and o are estimated RELATIVE to these, so they are
    # taken once from the initial patch and never recomputed inside the loop.
    r_mu, r_sd = R.mean(), R.std()
    R = (R - r_mu) / r_sd

    g, o, lam = 1.0, 0.0, _LAM0
    res = g * R.ravel() + o - S
    sse = float(np.sum(res * res))
    N_final = None
    converged = False
    it = 0
    for it in range(1, opt["max_iter"] + 1):
        Rv = R.ravel()
        Ry, Rx = np.gradient(R)                 # d/drow, d/dcol -> (y, x) in patch units
        Rx, Ry = Rx.ravel(), Ry.ravel()
        # Incremental warp in patch space, q -> (I + dB) q + s, composed onto (A, t).
        Jm = np.column_stack([g * Rx * uu, g * Rx * vv, g * Ry * uu, g * Ry * vv,
                              g * Rx, g * Ry, Rv, np.ones_like(Rv)])
        dp, N = _solve(Jm, res, lam)
        if dp is None:
            return _fail("singular", it)

        dB = np.array([[dp[0], dp[1]], [dp[2], dp[3]]])
        A_t = A @ (np.eye(2) + dB)
        t_t = t + A @ dp[4:6]
        g_t, o_t = g + dp[6], o + dp[7]

        Rn = _sample_patch(ref, t_t[0], t_t[1], A_t, w)
        ok_step = Rn is not None
        if ok_step:
            Rn = (np.asarray(Rn, dtype=np.float64) - r_mu) / r_sd
            ok_step = np.isfinite(Rn).all()
        if ok_step:
            res_t = g_t * Rn.ravel() + o_t - S
            sse_t = float(np.sum(res_t * res_t))
            ok_step = np.isfinite(sse_t) and sse_t <= sse
        if not ok_step:
            # Damp harder and retry from the same point — the Marquardt half of the loop.
            lam *= 10.0
            if lam > _LAM_MAX:
                return _fail("no_descent", it)
            continue

        A, t, g, o, R, res, sse, N_final = A_t, t_t, g_t, o_t, Rn, res_t, sse_t, N
        lam = max(lam / 3.0, 1e-6)
        if abs(g) < _MIN_GAIN:
            return _fail("gain_collapse", it)
        if np.abs(corners @ dB.T + dp[4:6]).max() < opt["conv_px"]:
            converged = True
            break

    if not converged:
        return _fail("max_iter", it)

    try:
        A0inv = np.linalg.inv(A0)
    except np.linalg.LinAlgError:
        return _fail("singular", it)
    shift = A0inv @ (t - t0)                     # total translation, in source pixels
    if not np.isfinite(shift).all() or np.hypot(*shift) > opt["max_shift_px"]:
        return _fail("shift", it)
    drift = np.abs(corners @ (A0inv @ A - np.eye(2)).T).max()
    if not np.isfinite(drift) or drift > opt["max_drift_px"]:
        return _fail("affine_drift", it)
    rho = _corr(g * R.ravel() + o, S)
    if rho < opt["min_corr"]:
        return _fail("low_corr", it)

    # a-posteriori variance of unit weight, then the translation block of sigma0^2 inv(N)
    # ponytail: unit weight matrix — every pixel independent and equally good. Ceiling:
    # interpolation and the sensor PSF correlate neighbouring residuals, so the estimate
    # reads optimistic (MEASURED 0.79x on dsun_00, 0.26x on dsun_50, where the residual
    # is a systematic illumination difference rather than noise). Upgrade path, in order
    # of effort: a measured variance-inflation factor per Δsun regime; then a banded
    # weight matrix from the residual autocorrelation, which is the real fix.
    # ponytail: a scalar sigma, not the 2x2 block — a point on a linear rim reads equally
    # certain along and across the ridge. Same ceiling refine.py carries, and the same
    # upgrade path: widen types.MatchSet/Registration to take the block first.
    sigma = float("nan")
    dof = w * w - _N_PARAMS
    if N_final is not None and dof > 0:
        try:
            Q = np.linalg.inv(N_final)
        except np.linalg.LinAlgError:
            Q = None
        if Q is not None:
            var = (sse / dof) * np.array([Q[4, 4], Q[5, 5]])
            if np.isfinite(var).all() and (var >= 0).all():
                sigma = float(np.sqrt(var.mean()))     # per-axis, source pixels
    return {"ok": True, "reason": "ok", "iters": int(it), "sigma": sigma,
            "rho": float(rho), "xy": t}


def lsm_refine(matches: MatchSet, src_img, ref_img, H, config: dict = None) -> tuple:
    """Least-squares refine ref_xy; returns (MatchSet, sigma, info). Never raises.

    Same call shape as `geometry.refine.refine_matches` plus a third `info` element, so
    the pipeline can swap one for the other. `sigma` is NaN wherever the uncertainty is
    not computable — including every rejected point, which also keeps its original
    coordinates. NaN does not mean "certain" and does not mean "moved".
    """
    if config is None:
        config = {}
    w = int(config.get("patch", 32))
    opt = {
        # 50, not 20: MEASURED on fixtures/dsun_sweep/dsun_00, where a 20-iteration cap
        # rejected 5/64 points that were still descending — at 50 the same pair keeps
        # 64/64 with the RMSE unchanged (0.0637 -> 0.0646 px). Iterations are cheap
        # (~0.03 ms each at patch 32); a wrongly rejected good point is not.
        "max_iter": int(config.get("max_iter", 50)),
        "conv_px": float(config.get("conv_px", 0.01)),
        "min_corr": float(config.get("min_corr", 0.5)),
        "max_shift_px": float(config.get("max_shift_px", w / 4.0)),
        "max_drift_px": float(config.get("max_affine_drift_px", 2.0)),
    }

    n = len(matches.src_xy)
    sigma = np.full(n, np.nan, dtype=np.float64)
    ref_xy = np.array(matches.ref_xy, dtype=np.float64).reshape(-1, 2).copy()
    out = MatchSet(src_xy=matches.src_xy, ref_xy=ref_xy, score=matches.score,
                   method=matches.method, cell=matches.cell)
    info = {"iterations": np.zeros(n, dtype=np.int32),
            "converged": np.zeros(n, dtype=bool),
            "rho": np.full(n, np.nan, dtype=np.float64),
            "reasons": ["not_attempted"] * n,
            "n_points": n, "n_refined": 0, "n_rejected": n, "rejections": {}, "patch": w}

    def _finish():
        info["n_rejected"] = n - info["n_refined"]
        counts = {}
        for reason in info["reasons"]:
            counts[reason] = counts.get(reason, 0) + 1
        info["rejections"] = {k: v for k, v in sorted(counts.items()) if k != "ok"}
        return out, sigma, info

    if n == 0 or w < 8:
        if w < 8 and n:
            info["reasons"] = ["patch_too_small"] * n
        return _finish()

    Hm = np.asarray(H, dtype=np.float64) if H is not None else None
    if Hm is None or Hm.shape != (3, 3) or not np.isfinite(Hm).all():
        info["reasons"] = ["no_geometry"] * n       # no usable init: refine nothing
        return _finish()

    src, ref = _as_array(src_img), _as_array(ref_img)
    if src.ndim != 2 or ref.ndim != 2 or src.size == 0 or ref.size == 0:
        info["reasons"] = ["bad_image"] * n
        return _finish()
    if src.dtype.type not in _WARPABLE:
        src = src.astype(np.float32)
    if ref.dtype.type not in _WARPABLE:
        ref = ref.astype(np.float32)

    src_xy = np.asarray(matches.src_xy, dtype=np.float64).reshape(-1, 2)
    # ponytail: one Python-level Gauss-Newton loop per point, no batching. Ceiling is
    # MEASURED at 1.4-3.0 ms/point (patch 32) against phase correlation's 0.3-0.6 —
    # ~4x, and it is 4x of a stage that is not the pipeline's bottleneck. Upgrade path
    # if it ever is: the points are independent, so a process pool over slices of the
    # loop costs nothing in accuracy.
    for i in range(n):
        sx, sy = src_xy[i]
        rx, ry = ref_xy[i]
        if not np.isfinite([sx, sy, rx, ry]).all():
            info["reasons"][i] = "bad_point"
            continue
        A0 = _jacobian(Hm, sx, sy)
        if A0 is None:
            info["reasons"][i] = "bad_jacobian"
            continue
        got = _lsm_point(src, ref, sx, sy, rx, ry, A0, w, opt)
        info["iterations"][i] = got["iters"]
        info["reasons"][i] = got["reason"]
        info["rho"][i] = got["rho"]
        if got["ok"]:
            info["converged"][i] = True
            ref_xy[i] = got["xy"]
            sigma[i] = got["sigma"]
            info["n_refined"] += 1

    return _finish()
