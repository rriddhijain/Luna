"""Seat 5 · bench/ablate — one stage off at a time. The table is the proof of the innovation.

Every variant is BASELINE with exactly one key overridden, so a row difference can
only be caused by that one switch. BASELINE is the SHIPPED default configuration,
written out explicitly: a reader of the table has to be able to see what "on" was.

Config keys the pipeline must honour for the ablation to mean anything (they sit in the
sections stages.py already passes down):

    photometry.canonicalise      True | False        <- headline, the live demo toggle
    photometry.phase_congruency  "auto" | True | False
    photometry.photometric_model "lommel_seeliger" | "lunar_lambert" | "none"
    photometry.mask_fill         "reflect" | "zero"  (how invalid px enter the PC map)
    photometry.clahe             "auto" | True | False
    match.method                 "auto" | "sift" | "l2"
    match.anms                   True | False        (quad-tree spread inside each cell)
    match.uniformity             True | False        (per-cell quota enforcement)
    geometry.subpixel            True | False
    geometry.tps                 "auto" | True | False  (non-rigid residual)
    geometry.check_fraction      0.2 | 0.0           (0 disables the held-out split)

Both gt_rmse_px and inlier_count are reported for every arm, and neither alone is the
answer: subpixel_off measured 1.920 vs 1.887 px on dsun_50 — indistinguishable — against
61 vs 99 inliers (runs/ablate_dsun50/ablation.csv). An RMSE-only table calls that stage
inert.

Two arms are only an arm under a condition, and the condition is part of the row:

* The ANMS gate is inside `if len(k_src) > max_matches` in match/tile.py. On both shipped
  fixtures the quota never binds at `match.max_matches` 50 — the busiest cell delivers 49
  tie-points on synth_pair_A and 15 on dsun_50 — so `cell["anms"]` is None in all 16 cells
  and `anms_off` measures NOTHING. It is kept, and labelled, because a reader has to be told that. The arm that
  measures ANMS is `quota10_baseline` vs `quota10_anms_off`, which lowers the quota to 10
  (9/16 cells bind on synth_pair_A, 12/16 on dsun_50) and then flips the one switch.
  Measured on real LROC NAC the shipped quota binds unaided: 168/172 cells on
  apollo16_dsun004, 95/216 on dsun115, 7/160 on dsun085 (bench/baselines.md section 2d).
* `match.uniformity` is the same shape of problem one level down. Turning it off makes
  config.cell_budgets return None, and match/tile.py's own fallbacks are 5 and 50 — the
  values the config was asking for. `uniformity_off` is therefore identical to the
  baseline by construction at the shipped quotas; `quota10_uniformity_off` is where the
  switch has something to remove.
* `photometric_model` is only consulted when a DEM gives the physics a surface to work on.
  Without `--dem`, `canonicalise` falls back to the empirical illumination field and the
  three models are one arm. Pass `--dem PATH` to get the `dem_*` rows, which are the ones
  that measure the photometric model.

`matcher_l2` used to sit in this list. `match/describe.py:145` and `match/tile.py:54` map
"l2" and "rift" onto the same RIFT path, so it was the baseline under another name, not an
independent arm; a row that can only ever equal the baseline is removed rather than
presented.

Run: python -m bench.ablate --source SRC.tif --ref REF.tif --out runs/ablate
     python -m bench.ablate --source SRC.tif --ref REF.tif --dem DEM.tif --out runs/ablate
"""

import os
import warnings

import click

from bench.harness import deep_merge, run_one, write_table

BASELINE = {
    # Off for the ablation, not because caching is wrong but because a cache hit across
    # two arms would make them look identical for a reason that is not the switch under
    # test. See bench/harness.py::SWEEP_CONFIG for the key collision that motivates it.
    "cache": {"enabled": False},
    "photometry": {"canonicalise": True, "phase_congruency": "auto",
                   "photometric_model": "lommel_seeliger", "mask_fill": "reflect",
                   "clahe": "auto"},
    "geometry": {"subpixel": True, "tps": "auto", "check_fraction": 0.2},
    # max_matches is written out because it is the condition the ANMS arm lives under,
    # not because it is being varied: 50 is the shipped default.
    "match": {"method": "auto", "anms": True, "uniformity": True, "max_matches": 50},
}

# Headline pair first: canonicaliser on vs off is what gets demoed live. Everything
# after it is one switch away from BASELINE, in the order photometry -> match -> geometry,
# except the two labelled pairs (quota10_*, dem_*) which are one switch away from their own
# named sub-baseline because the switch they test does nothing under the shipped one.
VARIANTS = [
    ("baseline_full", {}),
    ("canonicaliser_off", {"photometry": {"canonicalise": False}}),
    ("phase_congruency_off", {"photometry": {"phase_congruency": False}}),
    # Inert without a DEM — kept to show that, superseded by the dem_* rows. See module docstring.
    ("photometric_model_none", {"photometry": {"photometric_model": "none"}}),
    ("photometric_lunar_lambert", {"photometry": {"photometric_model": "lunar_lambert"}}),
    ("mask_fill_zero", {"photometry": {"mask_fill": "zero"}}),
    ("clahe_on", {"photometry": {"clahe": True}}),
    # Identical to baseline_full on both fixtures and not an ablation of anything: clahe
    # ships as "auto", which resolves to off on the RIFT arm, so this row is the shipped
    # default spelled out. Kept as the check that "auto" resolved the way config.py says.
    ("clahe_off", {"photometry": {"clahe": False}}),
    # Inert at max_matches=50 on both shipped fixtures — the quota never binds, so the
    # ANMS branch never runs. Read quota10_anms_off against quota10_baseline instead.
    ("anms_off", {"match": {"anms": False}}),
    ("quota10_baseline", {"match": {"max_matches": 10}}),
    ("quota10_anms_off", {"match": {"max_matches": 10, "anms": False}}),
    # Also inert at the shipped defaults, and for a reason that is visible in the code
    # rather than mysterious: config.cell_budgets returns None when uniformity is off,
    # and match/tile.py then falls back to `budget.get("min_matches", 5)` /
    # `("max_matches", 50)` — the same 5 and 50 the config asked for. Switching the
    # quotas off restores the quotas. quota10_uniformity_off is the row that shows it:
    # at max_matches=10 it should reproduce baseline_full, not quota10_baseline.
    ("uniformity_off", {"match": {"uniformity": False}}),
    ("quota10_uniformity_off", {"match": {"max_matches": 10, "uniformity": False}}),
    ("matcher_sift", {"match": {"method": "sift"}}),
    ("subpixel_off", {"geometry": {"subpixel": False}}),
    # These two are a PAIR across both fixtures, never one row on one fixture: geometry.tps
    # "auto" accepts the spline on synth_pair_A (so tps_forced is a no-op there and tps_off
    # moves 2.929 -> 3.235 px) and rejects it on dsun_50 (so tps_off is the no-op there and
    # tps_forced moves 1.887 -> 1.473 px). Either row alone reads as an inert stage.
    ("tps_off", {"geometry": {"tps": False}}),
    ("tps_forced", {"geometry": {"tps": True}}),
    ("check_split_off", {"geometry": {"check_fraction": 0.0}}),
]

# The photometric-model arms, which need a DEM to be arms at all. dem_baseline is their
# comparison row: it differs from baseline_full by the DEM, so reading a dem_* row against
# baseline_full would mix two switches.
DEM_VARIANTS = [
    ("dem_baseline", {}),
    ("dem_photometric_model_none", {"photometry": {"photometric_model": "none"}}),
    ("dem_lunar_lambert", {"photometry": {"photometric_model": "lunar_lambert"}}),
]


def variants(dem=None) -> list:
    """The arm list; the dem_* rows exist only when a DEM was supplied to run them with."""
    out = list(VARIANTS)
    if dem:
        out += [(name, deep_merge({"photometry": {"dem_path": str(dem)}}, delta))
                for name, delta in DEM_VARIANTS]
    return out


# gt_rmse_px is the true error and the one to read where it exists; check_rmse_px is the
# held-out number that exists on a real pair too. inlier_count is here because a stage can
# move the tie-point set a long way without moving the RMSE at all.
DELTA_KEYS = ["gt_rmse_px", "check_rmse_px", "rmse_px", "inlier_count",
              "coverage_pct", "dispersion_cv", "sdi"]


def _add_deltas(rows, baseline_name="baseline_full", prefix="d_"):
    """Add <prefix><metric> columns against the named row; missing on either side stays absent."""
    base = next((r for r in rows if r.get("name") == baseline_name), None)
    if base is None:
        return
    for row in rows:
        for key in DELTA_KEYS:
            a, b = row.get(key), base.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                row[f"{prefix}{key}"] = round(float(a) - float(b), 4)
            # else: leave the column absent — an unknown delta prints n/a, never 0.0


def ablate(source, reference, out_root="runs/ablate", arms=None, config=None, dem=None) -> list:
    """Run the pair once per variant, write ablation.csv/.md, return the rows."""
    rows = []
    for name, delta in (arms or variants(dem)):
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
    # The two labelled pairs are read against their own sub-baseline, not against the
    # shipped one, so their deltas are computed a second time under a d2_ prefix.
    _add_deltas([r for r in rows if str(r.get("name")).startswith("quota10_")],
                "quota10_baseline", prefix="d2_")
    _add_deltas([r for r in rows if str(r.get("name")).startswith("dem_")],
                "dem_baseline", prefix="d2_")
    write_table(rows, out_root, stem="ablation")
    return rows


@click.command()
@click.option("--source", required=True, help="Source image path")
@click.option("--ref", "reference", required=True, help="Reference image path")
@click.option("--dem", default=None, help="DEM for the dem_* photometric-model arms")
@click.option("--out", "out_root", default="runs/ablate", help="Output root directory")
def main(source, reference, dem, out_root):
    """Run the same pair with one stage switched off at a time and tabulate the difference."""
    ablate(source, reference, out_root, dem=dem)
    click.echo(open(os.path.join(out_root, "ablation.md")).read())


if __name__ == "__main__":
    main()
