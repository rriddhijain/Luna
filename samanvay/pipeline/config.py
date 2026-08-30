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
    "halo_px": 64,
    "nodata": None,
    "warp_interp": "cubic",
    "cache": {"enabled": True, "dir": ".cache/samanvay"},
    "photometry": {
        "canonicalise": True,
        "phase_congruency": True,
        "photometric_model": "lommel_seeliger",
        "dem_path": None,
    },
    "match": {
        "method": "sift",
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
    },
    # method: "phase" (default, phase_cross_correlation) | "lsm" (least-squares matching).
    # Phase correlation is the measured default; LSM wins on blurred or noisy imagery
    # and refuses rather than degrading the set under hard cross-illumination.
    "refine": {"method": "phase"},
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


def cell_budgets(cfg: dict) -> dict:
    """Per-cell quotas for match_tiled, or None when uniformity enforcement is off.

    P5 quotas are applied DURING matching, not as a post-hoc filter: filtering
    afterwards throws away work and yields worse coverage than asking each cell
    for its share in the first place.
    """
    match = cfg.get("match") or {}
    if not match.get("uniformity", True):
        return None
    grid_n = int(cfg.get("grid_n", 4))
    budget = {
        "min_matches": int(match.get("min_matches", 5)),
        "max_matches": int(match.get("max_matches", 50)),
    }
    return {cell: dict(budget) for cell in range(grid_n * grid_n)}


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
