from dataclasses import dataclass
import numpy as np

# Conventions:
# - Coordinates are (x, y) = (column, row), floating point, pixel-centre at integer coordinates.
# - Transforms map source → reference.
# - All residuals are expressed in source pixels.

@dataclass
class Product:            # ③ produces · everyone consumes
    path: str
    array: np.ndarray     # or TiledReader
    meta: dict            # product_id, instrument, gsd_m, sun_az_deg, sun_el_deg,
                          # incidence_deg, emission_deg, geotransform, crs,
                          # shape, dtype
    
@dataclass
class CanonicalImage:     # ② produces · ① consumes · ⑥ consumes the mask
    albedo:  np.ndarray   # float32, illumination divided out
    pc:      np.ndarray   # float32 [0,1], phase congruency
    pc_orient: np.ndarray # float32, dominant orientation
    mask:    np.ndarray   # uint8: 0=valid 1=shadow 2=nodata 3=saturated
    params:  dict         # everything needed to reproduce this exactly

@dataclass
class MatchSet:           # ① produces · ⑥ consumes · ④ renders
    src_xy: np.ndarray    # (N,2) float64
    ref_xy: np.ndarray    # (N,2) float64
    score:  np.ndarray    # (N,) float32
    method: np.ndarray    # (N,) uint8  — which path found it (L0/L1/L2/L3)
    cell:   np.ndarray    # (N,) int32  — uniformity grid cell id

@dataclass
class Registration:       # ⑥ produces · ③ warps with it · ④ displays it
    model_type: str       # "similarity" | "affine" | "homography", optionally "+tps"
    params: np.ndarray    # 3x3 — the GLOBAL model, always. `warp` is a residual on top.
    init_params: np.ndarray
    inliers: np.ndarray   # (N,) bool
    residuals: np.ndarray # (N,2) float64, in source pixels
    sigma: np.ndarray     # (N,) float64, per-point uncertainty
    metrics: dict         # rmse_px, check_rmse_px, inlier_count, inlier_ratio,
                          # coverage_pct, dispersion_cv, sdi, grid_n, runtime_s

    # P1.4 — the control/check split. 0 = control (the fit saw it), 1 = check (held out
    # entirely). check_rmse_px is computed ONLY over role == 1, which is the only error
    # figure in this repo that is not measured on the fit's own sample. None means no
    # split was made (too few matches), and check_rmse_px is then null, never optimistic.
    roles: np.ndarray = None      # (N,) uint8

    # Optional non-rigid residual applied AFTER `params`, in the SOURCE frame:
    #     src_predicted = warp.apply(inv(params) @ ref)
    # None means the registration is purely projective. Everything that reads `params`
    # as a 3x3 keeps working unchanged; only code that wants the full model consults
    # this, via geometry.tps.pullback(params, ref_xy, warp).
    warp: object = None           # geometry.tps.ThinPlateSpline | None
