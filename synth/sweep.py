"""Seat 2 · S2 sweep — the accuracy-vs-delta-sun-azimuth fixture series (pillar: the evidence chart).

Renders one pair per sun-azimuth difference with *everything else* frozen: same seed, same DEM,
same albedo field, same homography, same elevation, same noise level. The only variable across the
series is the source sun azimuth, so any trend in registration accuracy across the series is
attributable to illumination difference and nothing else.

Regenerate with:  python -m synth.sweep
"""

import json
import os

from synth.render_pair import render_synthetic_pair

DEFAULT_DELTAS = (0, 10, 20, 30, 40, 50)


def render_sweep(out_dir="fixtures/dsun_sweep", deltas=DEFAULT_DELTAS, ref_sun_az_deg=45.0,
                 sun_el_deg=25.0, ref_shape=(512, 512), seed=0, **kw):
    """Render one fixture pair per sun-azimuth delta into out_dir/dsun_NN; returns a list of dicts."""
    os.makedirs(out_dir, exist_ok=True)
    entries = []
    for d in deltas:
        sub = os.path.join(out_dir, "dsun_%02d" % int(round(float(d))))
        info = render_synthetic_pair(
            out_dir=sub, ref_shape=ref_shape, seed=seed,
            ref_sun=(float(ref_sun_az_deg), float(sun_el_deg)),
            src_sun=(float(ref_sun_az_deg) + float(d), float(sun_el_deg)), **kw)
        entries.append({
            "delta_sun_az_deg": float(d), "dir": sub,
            "source": info["source"], "reference": info["reference"], "gt": info["gt"],
            "H_src_to_ref": info["H"].tolist(),
        })

    manifest = os.path.join(out_dir, "manifest.json")
    with open(manifest, "w") as fh:
        json.dump({
            "note": ("Accuracy-vs-delta-sun-azimuth series. Geometry, seed, DEM, albedo, sun "
                     "elevation and noise are identical in every entry; only the source sun "
                     "azimuth changes. Register each pair, score against gt_points_holdout in "
                     "that pair's gt.json, and plot RMSE against delta_sun_az_deg."),
            "ref_sun_az_deg": float(ref_sun_az_deg), "sun_el_deg": float(sun_el_deg),
            "ref_shape": list(ref_shape), "seed": seed, "n_pairs": len(entries),
            "pairs": entries,
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
    args = ap.parse_args()
    out = render_sweep(out_dir=args.out_dir, deltas=args.deltas, seed=args.seed,
                       ref_shape=(args.size, args.size))
    for e in out:
        print("dsun %5.1f deg -> %s" % (e["delta_sun_az_deg"], e["dir"]))
    print("manifest:", out[0]["manifest"] if out else "(no pairs)")
