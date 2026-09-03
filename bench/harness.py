"""Seat 5 · bench/harness — run the pipeline over a manifest of pairs, emit CSV + markdown.

Manifest (YAML or JSON; YAML parser handles both):

    pairs:
      - name: synth_A
        source: fixtures/synth_pair_A/source.tif
        reference: fixtures/synth_pair_A/reference.tif
        config: {}          # optional, merged over the sweep config

Run: python -m bench.harness --manifest bench/pairs.yaml --out runs/bench
     python -m bench.harness sweep --manifest fixtures/dsun_sweep/manifest.json
"""

import csv
import json
import os
import time

import click
import yaml

# Columns worth seeing first; anything else the run reports is appended after these.
PREFERRED = ["name", "status", "wall_s", "rmse_px", "inlier_count",
             "inlier_ratio", "coverage_pct", "dispersion_cv", "error"]
MD_SKIP = {"source", "reference", "out_dir"}  # paths make the demo table unreadable


def deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge override into base, returning a new dict."""
    out = dict(base or {})
    for k, v in (override or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_manifest(path: str) -> list:
    """Load a YAML or JSON manifest into a list of pair dicts."""
    with open(path) as f:
        doc = yaml.safe_load(f)  # YAML is a superset of JSON, so one parser covers both
    if isinstance(doc, dict):
        doc = doc.get("pairs", [])
    return list(doc or [])


def _raw_metrics(out_dir: str) -> dict:
    """metrics.json as written, or {} — the nested keys _collect_metrics has to drop."""
    path = os.path.join(out_dir, "metrics.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _collect_metrics(out_dir: str) -> dict:
    """Flatten metrics.json into scalar columns; absent or unreadable means no columns."""
    metrics = _raw_metrics(out_dir)
    row = {}
    for key, val in (metrics or {}).items():
        if isinstance(val, dict):  # e.g. {"stage_s": {"match": 1.2, "verify": 0.3}}
            for sub, subval in val.items():
                if isinstance(subval, (int, float, str, bool)):
                    row[f"{key}.{sub}"] = subval
        elif val is None or isinstance(val, (int, float, str, bool)):
            row[key] = val
    return row


def _quota_bound(metrics: dict) -> dict:
    """How many cells the per-cell quota actually bit on — the condition ANMS lives under.

    `cell["anms"]` is None until `len(k_src) > max_matches` in match/tile.py, so it is the
    direct record of whether the selection rule ran at all. Without this column an
    `anms_off` row that is byte-identical to its baseline looks like a stage that does
    nothing, when what happened is that no stage ran. Absent cell_info means no columns,
    not a zero.
    """
    cells = ((metrics or {}).get("cell_info") or {}).get("cells") or {}
    if not cells:
        return {}
    return {"cells_total": len(cells),
            "cells_quota_bound": sum(1 for c in cells.values()
                                     if isinstance(c, dict) and c.get("anms") is not None)}


def _cell_spread(out_dir: str, source: str, metrics: dict = None) -> dict:
    """Mean number of the 4 sub-quadrants of a cell core that its delivered inliers occupy.

    This is the quantity ANMS exists to move and the one the grid alone cannot: coverage_pct
    and dispersion_cv are both computed per CELL and are blind to all K points sitting in one
    corner of it. 1.0 means every cell's tie-points are in a single quadrant, 4.0 means all
    four are occupied everywhere. Averaged over cells that delivered at least one inlier, so
    it does not double-count what coverage_pct already says.

    Needs the source raster only for its shape, and the grid the matcher actually used —
    cell_info's own grid_rows/grid_cols where the run recorded them, since the cell ids in
    matches.csv are `col + cols * row` under exactly that grid and a different `cols` would
    silently transpose every cell. Returns no columns rather than a guess if anything is
    missing.
    """
    path = os.path.join(out_dir, "matches.csv")
    if not source or not os.path.exists(path):
        return {}
    try:
        import numpy as np
        import rasterio

        from samanvay.geometry.uniformity import grid_shape

        with rasterio.open(source) as src:
            shape = (src.height, src.width)
        cell_info = (metrics or {}).get("cell_info") or {}
        rows, cols = cell_info.get("grid_rows"), cell_info.get("grid_cols")
        if not rows or not cols:
            rows, cols = grid_shape(shape, int((metrics or {}).get("grid_n") or 4), True)
        y_e = np.round(np.linspace(0, shape[0], rows + 1))
        x_e = np.round(np.linspace(0, shape[1], cols + 1))
        per_cell = {}
        with open(path) as handle:
            for rec in csv.DictReader(handle):
                if str(rec.get("is_inlier", "")).lower() not in ("1", "true"):
                    continue
                per_cell.setdefault(int(rec["grid_cell"]), []).append(
                    (float(rec["src_x"]), float(rec["src_y"])))
        if not per_cell:
            return {}
        quadrants, counts = [], []
        for cell_id, points in per_cell.items():
            row, col = divmod(cell_id, cols)
            x_m = 0.5 * (x_e[col] + x_e[col + 1])
            y_m = 0.5 * (y_e[row] + y_e[row + 1])
            quadrants.append(len({(x >= x_m, y >= y_m) for x, y in points}))
            counts.append(len(points))
        return {"cell_quadrants_mean": round(float(np.mean(quadrants)), 3),
                "cell_inliers_mean": round(float(np.mean(counts)), 3)}
    except Exception:
        # An unreadable raster or a matches.csv without the columns is a missing
        # measurement, not a run failure, and it must not take the row down with it.
        return {}


def run_one(name, source, reference, out_dir, config=None) -> dict:
    """Run the pipeline on one pair and return a result row; a failure is a row, not an abort."""
    row = {"name": name, "source": str(source), "reference": str(reference),
           "out_dir": out_dir, "status": "ok", "error": ""}
    os.makedirs(out_dir, exist_ok=True)
    start = time.perf_counter()
    try:
        # Lazy: pipeline.stages is rewritten independently; a module-level import
        # would break the whole harness on any transient error in that file.
        from samanvay.pipeline.stages import run_pipeline
        run_pipeline(source, reference, out_dir, config=config or {})
    except BaseException as exc:  # noqa: BLE001 - a sweep must survive any single run
        row["status"] = "failed"
        row["error"] = f"{type(exc).__name__}: {exc}"[:200]
    # ponytail: only total wall clock is measurable from out here. Per-stage times are
    # whatever the pipeline records in metrics.json["stage_s"]; upgrade by having
    # stages.py write that dict rather than by timing stages from outside.
    row["wall_s"] = round(time.perf_counter() - start, 3)
    metrics = _collect_metrics(out_dir)
    if row["status"] == "ok" and not metrics:
        row["status"] = "no_metrics"
    row.update(metrics)
    raw = _raw_metrics(out_dir)
    row.update(_quota_bound(raw))
    row.update(_cell_spread(out_dir, source, raw))
    return row


def run_manifest(manifest_path, out_root="runs/bench", config=None) -> list:
    """Run every pair in the manifest, write the table, return the rows."""
    rows = []
    for i, pair in enumerate(load_manifest(manifest_path)):
        pair = pair if isinstance(pair, dict) else {}
        name = str(pair.get("name") or f"pair_{i:02d}")
        rows.append(run_one(
            name, pair.get("source"), pair.get("reference"),
            os.path.join(out_root, name),
            deep_merge(config or {}, pair.get("config") or {}),
        ))
    write_table(rows, out_root, stem="bench")
    return rows


def _columns(rows):
    seen = []
    for row in rows:
        for key in row:
            if key not in seen:
                seen.append(key)
    head = [c for c in PREFERRED if c in seen]
    return head + [c for c in seen if c not in head]


def _fmt(val):
    """Render one cell; an unknown value prints as n/a and never as a fabricated number."""
    if val is None or val == "":
        return "n/a"
    if isinstance(val, float):
        return f"{val:.4g}"
    return str(val)


def write_table(rows, out_dir, stem="bench"):
    """Write rows as <stem>.csv and <stem>.md under out_dir; return (csv_path, md_path)."""
    os.makedirs(out_dir, exist_ok=True)
    cols = _columns(rows) or ["name"]
    csv_path = os.path.join(out_dir, f"{stem}.csv")
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({c: row.get(c, "") for c in cols})

    md_cols = [c for c in cols if c not in MD_SKIP]
    lines = ["| " + " | ".join(md_cols) + " |",
             "|" + "|".join(["---"] * len(md_cols)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_fmt(row.get(c)) for c in md_cols) + " |")
    md_path = os.path.join(out_dir, f"{stem}.md")
    with open(md_path, "w") as f:
        f.write("\n".join(lines) + "\n")
    return csv_path, md_path


# ---------------------------------------------------------------- delta-sun arm sweep

# The evidence chart: one manifest of pairs crossed with named config arms. Kept here
# rather than in a script because the numbers in bench/baselines.md have to be
# reproducible by running one command.
SWEEP_ARMS = {
    "auto": {},                                # shipped default: match.method resolves per pair
    "sift": {"match": {"method": "sift"}},     # pinned intensity arm, the contrast case
}

# Arms that are not part of the default matcher sweep but have to be reproducible from a
# command, because bench/baselines.md publishes their tables. `--arm` selects out of the
# union; the bare `sweep` still runs SWEEP_ARMS and nothing else.
ANMS_ARMS = {
    # Quad-tree spread inside each cell, at the SHIPPED max_matches. On the small synthetic
    # fixtures the quota never binds and these two arms are identical; on real NAC strips it
    # binds on 4-98% of cells (bench/baselines.md section 2d).
    "anms_on": {"match": {"anms": True}},
    "anms_off": {"match": {"anms": False}},
}
PHOTOM_ARMS = {
    # mask_fill x clahe. The RIFT arm ships mask_fill=reflect and clahe resolved off, so
    # `reflect_clahe_off` is the shipped configuration written out.
    "reflect_clahe_off": {"photometry": {"mask_fill": "reflect", "clahe": False}},
    "zero_clahe_off": {"photometry": {"mask_fill": "zero", "clahe": False}},
    "reflect_clahe_on": {"photometry": {"mask_fill": "reflect", "clahe": True}},
    "zero_clahe_on": {"photometry": {"mask_fill": "zero", "clahe": True}},
}
ALL_ARMS = {**SWEEP_ARMS, **ANMS_ARMS, **PHOTOM_ARMS}


# The canonicalisation cache is keyed on product_id + file size + int(mtime) + shape and
# NOT on the path (samanvay/pipeline/stages.py::_canonicalise_cached). Every pair in a
# rendered sweep carries product_id "synth_source", the same uncompressed size and the
# same second of mtime, so two pairs that differ only in illumination collide and the
# second run silently reuses the first one's albedo and phase congruency. Measured:
# dsun_60 and dsun_70 returned byte-identical gt_rmse_px 2.109761539475777 / 73 inliers.
# A sweep must therefore run with the cache off, or its middle columns are fiction.
SWEEP_CONFIG = {"cache": {"enabled": False}}


def run_arms(manifest_path, out_root="runs/dsun_sweep", arms=None, config=None, skip=()) -> list:
    """Run every manifest pair under every named arm; one row per (arm, pair)."""
    config = deep_merge(SWEEP_CONFIG, config or {})
    pairs = load_manifest(manifest_path)
    skip = set(skip or ())
    rows = []
    for arm, delta in (arms or SWEEP_ARMS).items():
        for i, pair in enumerate(pairs):
            pair = pair if isinstance(pair, dict) else {}
            # dsun manifests carry delta_sun_az_deg instead of a name.
            dsun = pair.get("delta_sun_az_deg")
            stem = (f"dsun_{int(round(float(dsun))):03d}" if dsun is not None
                    else str(pair.get("name") or f"pair_{i:02d}"))
            if stem in skip or str(pair.get("name")) in skip:
                continue
            row = run_one(f"{arm}@{stem}", pair.get("source"), pair.get("reference"),
                          os.path.join(out_root, arm, stem),
                          deep_merge(deep_merge(config or {}, delta), pair.get("config") or {}))
            row["arm"], row["pair"] = arm, stem
            # Two different quantities, so two columns. The manifest's delta is the fixture's
            # WORLD sun-azimuth difference and is what the evidence tables are indexed by;
            # `delta_sun_az_deg` is what the pipeline read off the two sidecars, and on a
            # rotated raster it is a different number (170 where the manifest says 180).
            # Writing the manifest value over the measured one would put a figure in the CSV
            # that contradicts the run's own metrics.json under the same key.
            if dsun is not None:
                row["manifest_delta_sun_az_deg"] = float(dsun)
            rows.append(row)
            print(f"{row['name']:20} {row['status']:8} "
                  f"gt_rmse_px={row.get('gt_rmse_px')} inliers={row.get('inlier_count')} "
                  f"{row['wall_s']}s", flush=True)
    write_table(rows, out_root, stem="dsun_sweep")
    return rows


@click.command()
@click.option("--manifest", default="fixtures/dsun_sweep/manifest.json",
              type=click.Path(exists=True), help="pair manifest to sweep")
@click.option("--out", "out_root", default="runs/dsun_sweep", help="Output root directory")
@click.option("--arm", "arm_names", multiple=True, help=f"subset of {sorted(ALL_ARMS)}")
@click.option("--skip-pair", "skip", multiple=True, help="manifest pair name to leave out")
def sweep(manifest, out_root, arm_names, skip):
    """Run a manifest under every matcher arm and tabulate accuracy against delta sun."""
    arms = {k: ALL_ARMS[k] for k in (arm_names or SWEEP_ARMS)}
    rows = run_arms(manifest, out_root, arms, skip=skip)
    click.echo(f"{sum(r['status'] == 'ok' for r in rows)}/{len(rows)} runs ok -> {out_root}")


@click.command()
@click.option("--manifest", required=True, type=click.Path(exists=True), help="YAML/JSON pair list")
@click.option("--out", "out_root", default="runs/bench", help="Output root directory")
def main(manifest, out_root):
    """Run the pipeline over every pair in a manifest and tabulate the metrics."""
    rows = run_manifest(manifest, out_root)
    click.echo(open(os.path.join(out_root, "bench.md")).read())
    click.echo(f"{sum(r['status'] == 'ok' for r in rows)}/{len(rows)} runs ok -> {out_root}")


if __name__ == "__main__":
    # `python -m bench.harness sweep ...` for the arm sweep; the bare form stays the
    # documented single-manifest run so the README and the Makefile keep working.
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        sweep(sys.argv[2:])
    else:
        main()
