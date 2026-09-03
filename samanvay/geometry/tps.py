"""Seat 6 (geometry) — the non-rigid residual: a thin plate spline over the global model.

A homography assumes the imaged surface is a plane. The Moon is not one: across a
3 km crater a projective model leaves a smooth, systematic residual that no amount of
RANSAC removes, because it is signal and not an outlier population. The spline absorbs
exactly that leftover, and nothing else.

Direction is frozen. The full model is

    src_predicted = warp.apply(inv(params) @ ref_xy)

i.e. the spline is a displacement field in the SOURCE frame, applied AFTER the global
pull-back. Fitting it in that direction is deliberate: every consumer — the residuals in
verify.py, the dense map cv2.remap wants, the report's quiver — asks for source
coordinates given reference coordinates, so no inverse TPS is ever needed. A TPS has no
closed-form inverse, so the alternative direction would cost an iterative solve per
pixel.

How we know the warp is not overfitting: it is rejected by points it never saw. The
spline is fitted on the CONTROL inliers only, and verify.py keeps it only if it improves
`check_rmse_all_px` — the RMSE over the held-out check set, with no threshold applied to
flatter it — without degrading the held-out points that were already inside the
threshold. A spline that has memorised its control points cannot improve a number
measured on points it was not shown, so an overfit TPS is discarded automatically rather
than argued about. That self-validation is the whole safety case for shipping a non-rigid
warp in a mission context. On bench/fake_matches (pure similarity plus noise, no relief
to absorb) the spline is rejected on 45 of 45 seed/contamination combinations, and on
synthetic relief it is accepted on 5 of 5 — the two outcomes that prove the test bites in
both directions. verify.py carries the measurements and why one condition is not enough.

`lam` is Tikhonov regularisation on the bending energy: 0 interpolates the control
points exactly (maximum overfit), larger is stiffer and tends towards the affine part
alone. It acts on the kernel built from control points normalised to unit RMS radius,
so the same `lam` means the same stiffness on a 512 px fixture and an 11952 px NAC
strip. Without that normalisation `lam` would be a scene-dependent number, which is the
kind of hidden knob this repo does not ship.
"""

import numpy as np

from samanvay.geometry.init import apply_transform

# 3 control points fix the affine part exactly and leave the spline part with nothing to
# fit; the 4th is the first observation the bending term actually sees. Below that a
# "TPS" is an affine wearing a hat, and returning None says so.
_MIN_CONTROL = 4

# Evaluation is chunked over the query points: the kernel matrix is (chunk x K), so a
# dense map over a 3000x3000 output grid against 200 control points would otherwise ask
# for a single 14 GiB allocation.
_CHUNK = 4096


def _kernel(r2):
    """U(r) = r^2 log(r^2) with U(0) = 0, evaluated from r^2 directly (no sqrt)."""
    out = np.zeros_like(r2)
    nz = r2 > 0
    out[nz] = r2[nz] * np.log(r2[nz])
    return out


class ThinPlateSpline:
    """A 2-D displacement field in source pixels, sampled at `control` points.

    `control` holds the pull-back positions the spline was fitted at, NOT the source
    positions: the field is a function of where the global model lands a point, which is
    where it will be evaluated at warp time.
    """

    def __init__(self, control, weights, affine, lam, center, scale):
        self.control = np.asarray(control, dtype=np.float64).reshape(-1, 2)
        self.weights = np.asarray(weights, dtype=np.float64).reshape(-1, 2)
        self.affine = np.asarray(affine, dtype=np.float64).reshape(3, 2)
        self.lam = float(lam)
        self.center = np.asarray(center, dtype=np.float64).reshape(2)
        self.scale = float(scale)

    @property
    def n_control(self) -> int:
        return int(len(self.control))

    def displacement(self, xy) -> np.ndarray:
        """(N,2) correction to ADD to `xy`, in source pixels."""
        p = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        if len(p) == 0:
            return np.zeros((0, 2))
        nodes = (self.control - self.center) / self.scale
        out = np.empty((len(p), 2), dtype=np.float64)
        for i in range(0, len(p), _CHUNK):
            q = (p[i:i + _CHUNK] - self.center) / self.scale
            r2 = ((q[:, None, :] - nodes[None, :, :]) ** 2).sum(axis=2)
            # einsum, not `@`: matmul routes through BLAS, whose SIMD tail reads
            # uninitialised lanes and emits spurious divide-by-zero/overflow/invalid
            # warnings on every call. Same false positive geometry/init.py documents;
            # results agree, this only stops the noise that trains us to ignore warnings.
            out[i:i + _CHUNK] = (np.einsum("nk,kj->nj", _kernel(r2), self.weights)
                                 + np.einsum("nk,kj->nj",
                                             np.hstack([np.ones((len(q), 1)), q]),
                                             self.affine))
        return out

    def apply(self, xy) -> np.ndarray:
        """xy + displacement(xy) — the pulled-back point corrected for relief."""
        p = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        if len(p) == 0:
            return np.zeros((0, 2))
        return p + self.displacement(p)

    def to_dict(self) -> dict:
        """JSON-safe: control points, weights and the affine part, in plain lists."""
        return {
            "type": "thin_plate_spline",
            "control": self.control.tolist(),
            "weights": self.weights.tolist(),
            "affine": self.affine.tolist(),
            "lam": self.lam,
            "center": self.center.tolist(),
            "scale": self.scale,
        }

    @classmethod
    def from_dict(cls, d) -> "ThinPlateSpline":
        return cls(d["control"], d["weights"], d["affine"],
                   d.get("lam", 0.0), d["center"], d["scale"])


def fit_tps(control_src_xy, control_pullback_xy, lam=0.5):
    """Fit the displacement (src - pullback) over the pull-back positions.

    Returns a ThinPlateSpline, or None when the system is singular, the inputs disagree
    or there are too few points. Never raises: a warp that cannot be fitted is a warp we
    do not ship, not an exception in the middle of a registration.
    """
    try:
        src = np.asarray(control_src_xy, dtype=np.float64).reshape(-1, 2)
        node = np.asarray(control_pullback_xy, dtype=np.float64).reshape(-1, 2)
    except (TypeError, ValueError):
        return None
    k = len(node)
    if k != len(src) or k < _MIN_CONTROL:
        return None
    if not (np.isfinite(src).all() and np.isfinite(node).all()):
        return None

    v = src - node                       # the displacement the spline must reproduce
    center = node.mean(axis=0)
    scale = float(np.sqrt(np.mean(np.sum((node - center) ** 2, axis=1))))
    if not np.isfinite(scale) or scale <= 0.0:
        return None                      # every control point in one place
    q = (node - center) / scale

    r2 = ((q[:, None, :] - q[None, :, :]) ** 2).sum(axis=2)
    K = _kernel(r2)
    try:
        lam = float(lam)
    except (TypeError, ValueError):
        lam = 0.0                    # a config that cannot be read is no regularisation
    if not np.isfinite(lam) or lam < 0.0:
        lam = 0.0
    if lam > 0.0:
        # Tikhonov on the diagonal. Exactly the classical regularised TPS: the solution
        # minimises ||f(node) - v||^2 + lam * bending energy, so lam > 0 stops the
        # spline from chasing the sub-pixel noise on its own control points.
        K = K + lam * np.eye(k)
    P = np.hstack([np.ones((k, 1)), q])

    A = np.zeros((k + 3, k + 3), dtype=np.float64)
    A[:k, :k] = K
    A[:k, k:] = P
    A[k:, :k] = P.T
    b = np.vstack([v, np.zeros((3, 2))])
    try:
        sol = np.linalg.solve(A, b)
    except np.linalg.LinAlgError:
        return None
    if not np.isfinite(sol).all():
        return None
    # A merely ill-conditioned system is left to the caller's held-out test rather than
    # to a condition-number threshold nobody has calibrated: a spline whose weights blew
    # up cannot improve check_rmse_all_px, so verify.py discards it on the evidence.
    return ThinPlateSpline(node, sol[:k], sol[k:], lam, center, scale)


def pullback(H, ref_xy, warp=None) -> np.ndarray:
    """Reference points in the SOURCE frame under the full model, or None if H is singular.

    THE one place the full model is evaluated — verify.py, the metrics and the warp all
    route through here, so the global-then-spline order can only be defined once.
    """
    try:
        Hi = np.linalg.inv(np.asarray(H, dtype=np.float64))
    except (np.linalg.LinAlgError, ValueError, TypeError):
        return None
    if not np.isfinite(Hi).all():
        return None
    xy = apply_transform(Hi, ref_xy)
    if warp is not None:
        xy = warp.apply(xy)
    return xy
