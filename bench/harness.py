"""Seat 5 · bench/harness — run the pipeline over a manifest of pairs, emit CSV + markdown.

Manifest (YAML or JSON; YAML parser handles both):

    pairs:
      - name: synth_A
        source: fixtures/synth_pair_A/source.tif
        reference: fixtures/synth_pair_A/reference.tif
        config: {}          # optional, merged over the sweep config

Run: python -m bench.harness --manifest bench/pairs.yaml --out runs/bench
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


def _collect_metrics(out_dir: str) -> dict:
    """Flatten metrics.json into scalar columns; absent or unreadable means no columns."""
    path = os.path.join(out_dir, "metrics.json")
    if not os.path.exists(path):
        return {}
    try:
        with open(path) as f:
            metrics = json.load(f)
    except Exception:
        return {}
    row = {}
    for key, val in (metrics or {}).items():
        if isinstance(val, dict):  # e.g. {"stage_s": {"match": 1.2, "verify": 0.3}}
            for sub, subval in val.items():
                if isinstance(subval, (int, float, str, bool)):
                    row[f"{key}.{sub}"] = subval
        elif val is None or isinstance(val, (int, float, str, bool)):
            row[key] = val
    return row


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


@click.command()
@click.option("--manifest", required=True, type=click.Path(exists=True), help="YAML/JSON pair list")
@click.option("--out", "out_root", default="runs/bench", help="Output root directory")
def main(manifest, out_root):
    """Run the pipeline over every pair in a manifest and tabulate the metrics."""
    rows = run_manifest(manifest, out_root)
    click.echo(open(os.path.join(out_root, "bench.md")).read())
    click.echo(f"{sum(r['status'] == 'ok' for r in rows)}/{len(rows)} runs ok -> {out_root}")


if __name__ == "__main__":
    main()
