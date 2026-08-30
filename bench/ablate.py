"""Seat 5 · bench/ablate — one stage off at a time. The table is the proof of the innovation.

Every variant is BASELINE with exactly one key overridden, so a row difference can
only be caused by that one switch. Config keys the pipeline must honour for the
ablation to mean anything (they sit in the sections stages.py already passes down):

    photometry.canonicalise      True | False        <- headline, the live demo toggle
    photometry.phase_congruency  True | False
    photometry.photometric_model "lommel_seeliger" | "none"
    geometry.subpixel            True | False
    match.uniformity             True | False        (per-cell quota enforcement)
    match.method                 "sift" | "l2"

Run: python -m bench.ablate --source SRC.tif --ref REF.tif --out runs/ablate
"""

import os
import warnings

import click

from bench.harness import deep_merge, run_one, write_table

BASELINE = {
    "photometry": {"canonicalise": True, "phase_congruency": True,
                   "photometric_model": "lommel_seeliger"},
    "geometry": {"subpixel": True},
    "match": {"uniformity": True, "method": "sift"},
}

# Headline pair first: canonicaliser on vs off is what gets demoed live.
VARIANTS = [
    ("canonicaliser_on", {}),
    ("canonicaliser_off", {"photometry": {"canonicalise": False}}),
    ("phase_congruency_off", {"photometry": {"phase_congruency": False}}),
    ("photometric_model_none", {"photometry": {"photometric_model": "none"}}),
    ("subpixel_off", {"geometry": {"subpixel": False}}),
    ("uniformity_off", {"match": {"uniformity": False}}),
    ("matcher_l2", {"match": {"method": "l2"}}),
]

# Metrics compared against the full pipeline. Sign convention: delta = variant - baseline,
# so a positive d_rmse_px means the ablated run is worse.
DELTA_KEYS = ["rmse_px", "coverage_pct", "inlier_count", "dispersion_cv"]


def _add_deltas(rows, baseline_name="canonicaliser_on"):
    """Add d_<metric> columns against the full-pipeline row; missing on either side stays absent."""
    base = next((r for r in rows if r.get("name") == baseline_name), None)
    if base is None:
        return
    for row in rows:
        for key in DELTA_KEYS:
            a, b = row.get(key), base.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                row[f"d_{key}"] = round(float(a) - float(b), 4)
            # else: leave the column absent — an unknown delta prints n/a, never 0.0


def ablate(source, reference, out_root="runs/ablate", variants=None, config=None) -> list:
    """Run the pair once per variant, write ablation.csv/.md, return the rows."""
    rows = []
    for name, delta in (variants or VARIANTS):
        cfg = deep_merge(deep_merge(BASELINE, config or {}), delta)
        rows.append(run_one(name, source, reference, os.path.join(out_root, name), cfg))

    ok = [r for r in rows if r.get("status") == "ok" and isinstance(r.get("rmse_px"), (int, float))]
    if len(ok) > 1 and len({round(r["rmse_px"], 9) for r in ok}) == 1:
        warnings.warn(
            "every variant produced an identical rmse_px — the pipeline is probably "
            "ignoring the ablation config keys; the table proves nothing until it honours them",
            stacklevel=2,
        )
    _add_deltas(rows)
    write_table(rows, out_root, stem="ablation")
    return rows


@click.command()
@click.option("--source", required=True, help="Source image path")
@click.option("--ref", "reference", required=True, help="Reference image path")
@click.option("--out", "out_root", default="runs/ablate", help="Output root directory")
def main(source, reference, out_root):
    """Run the same pair with one stage switched off at a time and tabulate the difference."""
    ablate(source, reference, out_root)
    click.echo(open(os.path.join(out_root, "ablation.md")).read())


if __name__ == "__main__":
    main()
