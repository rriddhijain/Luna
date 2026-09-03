"""Seat 2 · S2 sweep — the accuracy-vs-delta-sun-azimuth fixture series (pillar: the evidence chart).

Renders one pair per sun-azimuth difference with *everything else* frozen: same seed, same DEM,
same albedo field, same homography, same elevation, same noise level. The only variable across the
series is the source sun azimuth, so any trend in registration accuracy across the series is
attributable to illumination difference and nothing else.

The series spans 0-180 deg. It has to: the measured accuracy trough is orthogonal illumination,
not opposite illumination, and a sweep that stopped at 50 deg could see neither the trough nor
the recovery at 180 (bench/baselines.md section 1).

Regenerate with:  python -m synth.sweep       # renders what is missing, reuses what is there
"""

import json
import os

from synth.render_pair import render_synthetic_pair

# 0-180, not 0-50. The trough is NOT at the ends: measured RIFT nn-correct is 0.551 at
# delta 80 and 0.531 at 90, recovering to 1.000 at 180 (the full curve is measured in
# bench/baselines.md section 4). Orthogonal illumination is the hard case; the plan's literal
# "opposite suns cast inverse shadows" case is the easy one for a phase-congruency
# descriptor, because an inverted shadow is still the same edge.
# A sweep that stopped at 50 could not see either fact.
DEFAULT_DELTAS = (0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100, 120, 150, 180)

_FIXTURE_FILES = ("source.tif", "reference.tif", "dem.tif", "gt.json", "README.md",
                  "source.tif.json", "reference.tif.json")


def _existing(sub, want):
    """Entry fields for an already-rendered pair, or None if it cannot be reused.

    `want` is the subset of gt.json["params"] this call would have rendered with. A directory
    that disagrees on any of them is a DIFFERENT fixture: reusing it would write a manifest
    stating a seed, a shape and a sun the rasters on disk do not have. Re-render instead.
    """
    if not all(os.path.exists(os.path.join(sub, n)) for n in _FIXTURE_FILES):
        return None
    with open(os.path.join(sub, "gt.json")) as fh:
        gt = json.load(fh)
    params = gt.get("params") or {}
    if any(params.get(k) != v for k, v in want.items()):
        return None
    return {"source": os.path.join(sub, "source.tif"),
            "reference": os.path.join(sub, "reference.tif"),
            "gt": os.path.join(sub, "gt.json"),
            "H_src_to_ref": gt["H_src_to_ref"]}


def render_sweep(out_dir="fixtures/dsun_sweep", deltas=DEFAULT_DELTAS, ref_sun_az_deg=45.0,
                 sun_el_deg=25.0, ref_shape=(512, 512), seed=0, skip_existing=True, **kw):
    """Render one fixture pair per sun-azimuth delta into out_dir/dsun_NN; returns a list of dicts.

    skip_existing reuses a complete pair directory instead of re-rendering it. The render is
    deterministic, so the bytes would be identical either way — but extending the sweep must not
    rewrite fixtures another process is reading, and re-rendering 14 pairs to regenerate one
    manifest is minutes of nothing.
    """
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for d in deltas:
        sub = os.path.join(out_dir, "dsun_%02d" % int(round(float(d))))
        want = {"seed": seed, "ref_shape": list(ref_shape),
                "ref_sun_az_el": [float(ref_sun_az_deg), float(sun_el_deg)],
                "src_sun_az_el": [float(ref_sun_az_deg) + float(d), float(sun_el_deg)]}
        fields = _existing(sub, want) if skip_existing else None
        reused = fields is not None
        if not reused:
            info = render_synthetic_pair(
                out_dir=sub, ref_shape=ref_shape, seed=seed,
                ref_sun=(float(ref_sun_az_deg), float(sun_el_deg)),
                src_sun=(float(ref_sun_az_deg) + float(d), float(sun_el_deg)), **kw)
            fields = {"source": info["source"], "reference": info["reference"],
                      "gt": info["gt"], "H_src_to_ref": info["H"].tolist()}
        entries.append({"delta_sun_az_deg": float(d), "dir": sub, "reused": reused, **fields})

    manifest = os.path.join(out_dir, "manifest.json")
    with open(manifest, "w") as fh:
        json.dump({
            "note": ("Accuracy-vs-delta-sun-azimuth series. Geometry, seed, DEM, albedo, sun "
                     "elevation and noise are identical in every entry; only the source sun "
                     "azimuth changes. Register each pair, score against gt_points_holdout in "
                     "that pair's gt.json, and plot RMSE against delta_sun_az_deg. Delta is the "
                     "WORLD sun azimuth difference; the source raster is rotated 10 deg, so the "
                     "delta a matcher reads off the two sidecars is |delta - 10|."),
            "ref_sun_az_deg": float(ref_sun_az_deg), "sun_el_deg": float(sun_el_deg),
            "ref_shape": list(ref_shape), "seed": seed, "n_pairs": len(entries),
            # "reused" is this call's history, not a property of the fixture. Persisting it
            # would make a tracked manifest differ between a clean render and an extension
            # of an existing one, for no information.
            "pairs": [{k: v for k, v in e.items() if k != "reused"} for e in entries],
        }, fh, indent=2)
    for e in entries:
        e["manifest"] = manifest
    return entries


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Regenerate the SAMANVAY delta-sun-azimuth sweep.")
    ap.add_argument("--out-dir", default="fixtures/dsun_sweep")
    ap.add_argument("--size", type=int, default=512, help="reference image side in pixels")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--deltas", type=float, nargs="*", default=list(DEFAULT_DELTAS))
    ap.add_argument("--force", action="store_true",
                    help="re-render pairs that already exist (default: reuse them)")
    args = ap.parse_args()
    out = render_sweep(out_dir=args.out_dir, deltas=args.deltas, seed=args.seed,
                       ref_shape=(args.size, args.size), skip_existing=not args.force)
    for e in out:
        print("dsun %5.1f deg -> %s%s" % (e["delta_sun_az_deg"], e["dir"],
                                          " (reused)" if e.get("reused") else ""))
    print("manifest:", out[0]["manifest"] if out else "(no pairs)")
