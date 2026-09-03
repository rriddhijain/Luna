"""Seat 3 · pipeline/config — the default config tree and the merge rules.

Every stage switch a judge might ask us to flip lives here, so an ablation is a
flag rather than a rewrite. Tuning CONSTANTS deliberately do not live here: each
module owns its own defaults and applies them to any key we leave unset, so a
number has exactly one home and cannot drift between two copies. Anything you
put in a YAML file is passed through to the owning module verbatim.

Config sections and who reads them:

    seed                   io/writers  (provenance; None means "nobody seeded")
    grid_n, halo_px        match/tile, geometry/verify, geometry/metrics
    nodata                 io/writers
    cache.*                pipeline/stages
    photometry.*           photometry/normalize.canonicalise
      .canonicalise        pipeline/stages   <- headline ablation, the live demo toggle
    match.*                match/tile.match_tiled
      .uniformity          pipeline/stages   -> becomes the cell_budgets argument
      .coarse_init         pipeline/stages   -> P2 metadata init on/off
      .cascade_enabled     pipeline/stages   -> P3 coarse-to-fine on/off
      .cascade.*           match/cascade.match_cascade
    geometry.*             geometry/verify.verify_matches
      .subpixel            pipeline/stages   -> gates the refiner
    refine.*               geometry/refine.refine_matches or geometry/lsm.lsm_refine
      .method              pipeline/stages   -> "phase" (default) | "lsm"
    warp_interp            pipeline/stages  (nearest|linear|cubic|lanczos)

NOTE on match.cascade: match_cascade takes cell_budgets INSIDE its config dict, not as
the separate keyword match_tiled uses. stages.py packs it; anyone calling match_cascade
directly and copying match_tiled's call shape will silently lose the P5 quotas.

Passthrough keys the modules accept but we do not default (they own the value):
    photometry: epsilon smooth_sigma nscale norient lambert_weight
                nodata_value dem_gsd_ratio_max
    match:      ratio_threshold relax_attempts relax_ratio_step ratio_ceiling
                max_masked_frac search_margin_px
    geometry:   ransac_thresh_px init_gate_px
    refine:     patch upsample min_peak min_peak_ratio max_shift_px
"""

import copy
import json
import os

import yaml

# grid_n lives at the TOP level and is pushed down into geometry at load time.
# Three modules used to read it from three different places; resolve() is the
# single point that keeps them agreeing.
DEFAULTS = {
    "seed": None,
    "grid_n": 4,
    # The uniformity grid follows the image aspect: grid_n is the count along the SHORT
    # axis and the long axis gets proportionally more cells, so cells stay near-square.
    # A 888x11952 NAC strip on a fixed 4x4 grid gets 13:1 cells, and both coverage_pct
    # and the per-cell quotas are then measured on a partition nobody would defend.
    # Set false to force exactly grid_n x grid_n.
    "grid_aspect": True,
    "halo_px": 64,
    "nodata": None,
    "warp_interp": "cubic",
    # Which pixel grid registered.tif is written on. "reference" resamples the source to
    # the reference grid (the old, only behaviour): correct CRS, but a 0.25 m OHRC source
    # against a 20 m reference is delivered as a thumbnail with 99.98% of its pixels gone.
    # "source" keeps the source resolution and georeferences it from the fitted transform.
    # "both" writes registered.tif (reference grid) and registered_source_grid.tif.
    "output": {"grid": "both"},
    "cache": {"enabled": True, "dir": ".cache/samanvay"},
    "photometry": {
        "canonicalise": True,
        # "auto" = compute the phase-congruency map iff the resolved matcher consumes it.
        # On the SIFT path a computed PC map was pure waste (~11.5 s/image, then discarded).
        # True/False still force it, which is what the ablation needs.
        "phase_congruency": "auto",
        "photometric_model": "lommel_seeliger",
        "dem_path": None,
        # How invalid (shadow/nodata/saturated) pixels enter the phase-congruency
        # transform. "zero" was the old behaviour and manufactures a hard step edge at
        # every mask boundary — sun-positioned structure injected into the one map whose
        # entire purpose is illumination invariance. "reflect" fills across the boundary
        # so no edge is created, and the mask still suppresses keypoints afterwards.
        "mask_fill": "reflect",
        # CLAHE on the albedo. Off by default and that is a decision, not an oversight:
        # the descriptor is contrast-invariant by construction, so CLAHE only adds a
        # spatially varying non-linearity and tile-boundary steps. See docs/decisions.md.
        # On for the intensity (SIFT/ORB) arms where local contrast genuinely helps.
        "clahe": "auto",
        "clahe_clip": 2.0,
        "clahe_grid": 8,
    },
    "match": {
        # "auto" resolves in pipeline/stages from the pair's delta sun azimuth, using the
        # same >= 20 deg bar preflight already recommends on. Shipping "sift" as the fixed
        # default turned the project's headline differentiator off: at delta-sun 50 deg
        # every SIFT arm returns ZERO inliers where RIFT returns 173 (bench/baselines.md).
        "method": "auto",
        # Quad-tree adaptive non-maximal suppression inside each cell. Without it the
        # per-cell quota is filled by a plain score sort, so all K points may pile into
        # one textured corner of the cell and the grid buys nothing within-cell.
        "anms": True,
        "coarse_init": True,
        "cascade_enabled": True,
        "uniformity": True,
        "min_matches": 5,
        "max_matches": 50,
        # P3 cascade. Registered so a typo is visible in `samanvay show-config`
        # rather than silently ignored; the module owns the tuning values.
        "cascade": {},
    },
    "geometry": {
        "model": "auto",
        "model_margin": 0.10,
        "subpixel": True,
        # P1.4 — fraction of tie-points held out of the fit entirely, stratified by grid
        # cell so the check set is spatially spread rather than clustered. 0 disables the
        # split, and check_rmse_px is then null. The fit NEVER sees a check point.
        "check_fraction": 0.2,
        # Thin-plate-spline residual on top of the global model, for relief the projective
        # model cannot absorb. "auto" fits it and keeps it ONLY if it improves the
        # held-out check RMSE — which is what makes a non-rigid warp safe to ship: an
        # overfit TPS is rejected by points it never saw. True forces, False disables.
        "tps": "auto",
        "tps_lambda": 0.5,
        "tps_min_control": 25,
    },
    # Which band a multi-band product (IIRS is ~250) is reduced to before matching.
    # reduce: "pc1" (SNR-screened PCA first principal component, the pseudo-panchromatic
    # structural map), "mean", or "band" to take index verbatim. index=None means band 1
    # for single-band products and the reduction for cubes.
    "band": {"index": None, "reduce": "pc1", "min_snr": 2.0, "max_bands": 64},
    # method: "phase" (default, phase_cross_correlation) | "lsm" (least-squares matching).
    # Phase correlation is the measured default; LSM wins on blurred or noisy imagery
    # and refuses rather than degrading the set under hard cross-illumination.
    "refine": {"method": "phase"},
    "report": {"pdf": True},
}


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into a copy of base; dicts merge, scalars replace."""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def parse_scalar(text: str):
    """Parse a --set value as JSON, falling back to the bare string."""
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        lowered = str(text).strip().lower()
        if lowered in ("true", "false"):
            return lowered == "true"
        if lowered in ("none", "null"):
            return None
        return text


def set_dotted(cfg: dict, dotted: str, value) -> dict:
    """Set cfg['a']['b'] from the key 'a.b', creating intermediate dicts."""
    node = cfg
    parts = str(dotted).split(".")
    for part in parts[:-1]:
        if not isinstance(node.get(part), dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = value
    return cfg


def resolve(cfg: dict) -> dict:
    """Push shared values into the sections that read them, so one number has one home."""
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("geometry", {})["grid_n"] = cfg.get("grid_n", DEFAULTS["grid_n"])
    cfg["geometry"]["grid_aspect"] = cfg.get("grid_aspect", DEFAULTS["grid_aspect"])
    return cfg


def load_config(path: str = None, overrides: dict = None, sets=None) -> dict:
    """Build the run config: DEFAULTS, then a YAML file, then a dict, then --set pairs."""
    cfg = copy.deepcopy(DEFAULTS)
    if path:
        with open(path, "r") as handle:
            cfg = deep_merge(cfg, yaml.safe_load(handle) or {})
    if overrides:
        cfg = deep_merge(cfg, overrides)
    for item in sets or ():
        if "=" not in item:
            raise ValueError(f"--set expects key=value, got {item!r}")
        key, _, raw = item.partition("=")
        set_dotted(cfg, key.strip(), parse_scalar(raw))
    return resolve(cfg)


def cell_budgets(cfg: dict, n_cells: int = None) -> dict:
    """Per-cell quotas for match_tiled, or None when uniformity enforcement is off.

    P5 quotas are applied DURING matching, not as a post-hoc filter: filtering
    afterwards throws away work and yields worse coverage than asking each cell
    for its share in the first place.

    `n_cells` is rows*cols from geometry.uniformity.grid_shape, which needs the image
    aspect this function does not have. Omitting it assumes the square grid_n x grid_n
    grid — correct for square imagery, and short by exactly the extra cells on a strip,
    where the missing ids would silently fall back to hardcoded quotas in match_tiled.
    """
    match = cfg.get("match") or {}
    if not match.get("uniformity", True):
        return None
    if n_cells is None:
        n_cells = int(cfg.get("grid_n", 4)) ** 2
    budget = {
        "min_matches": int(match.get("min_matches", 5)),
        "max_matches": int(match.get("max_matches", 50)),
    }
    return {cell: dict(budget) for cell in range(int(n_cells))}


def find_ground_truth(source_path: str) -> dict:
    """Load gt.json beside the source image, or None. Synthetic fixtures only."""
    if not source_path:
        return None
    candidate = os.path.join(os.path.dirname(str(source_path)), "gt.json")
    if not os.path.exists(candidate):
        return None
    try:
        with open(candidate, "r") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None
