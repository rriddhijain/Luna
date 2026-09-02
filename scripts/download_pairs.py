#!/usr/bin/env python3
"""Download an OVERLAPPING LRO NAC pair with a large illumination difference.

docs/DATA.md section 4 says to start with NAC-to-NAC: same ground, very different
incidence. This queries the Washington University ODE REST API for NAC EDR products
near a point, picks the pair with the biggest incidence difference THAT ACTUALLY
OVERLAPS, downloads both, and hands them to `samanvay check`.

The overlap gate is the whole point, and it is not optional. ODE's `locr` radius does
not confine results to the region -- a locr=0.5 query at latitude -85 comes back with
products spanning +78 to -82 -- and because incidence angle tracks latitude, ranking
by incidence difference ALONE reliably picks the two most distant frames on the Moon.
Measured against the live API: the ungated version selected M1417937558LE at latitude
+1.13 and M1417942939LE at -82.13, zero overlap, ~1 GB of download for two pictures of
different ground. That is docs/DATA.md section 8's "most common real-data mistake",
automated. Overlap is the hard gate; illumination only ranks what survives it.

    python scripts/download_pairs.py --lat -85.0 --lon 25.0 --radius 0.5

Sidecars carry only what ODE MEASURED -- incidence, emission, phase. Sun azimuth is
not in the ODE product record, so it is not written: samanvay/io/metadata.py marks it
unknown and the run degrades to `empirical` illumination, which is the honest mode.
Substituting a plausible azimuth would be recorded as `sidecar` provenance and become
indistinguishable from a measured value, which is exactly the guarantee docs/DATA.md
section 3 makes. Sun elevation is likewise left out: metadata.py derives it from
incidence and labels it `derived_from_incidence`.
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request

from pick_pairs import overlap_frac

ODE = "https://oderest.rsl.wustl.edu/live2/"


def query_ode(lat, lon, radius):
    """NAC EDR product records near (lat, lon). Returns the raw ODE Product list."""
    url = (f"{ODE}?target=moon&ihid=LRO&iid=LROC&pt=EDRNAC4&query=product&results=m"
           f"&latitude={lat}&longitude={lon}&locr={radius}&output=json")
    req = urllib.request.Request(url, headers={"User-Agent": "samanvay/download_pairs"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    products = data.get("ODEResults", {}).get("Products", {}).get("Product", [])
    return products if isinstance(products, list) else [products]


def bbox(p):
    """(west, east, south, north) degrees, or None if unusable.

    Cross-meridian footprints are dropped rather than unwrapped: ODE flags them and a
    wrapped box would report a nonsense overlap fraction against an unwrapped one.
    ponytail: no dateline unwrap, matches scripts/pick_pairs.py. Polar NAC strips at
    the sites docs/DATA.md recommends do not need it.
    """
    if str(p.get("Footprints_cross_meridian", "")).lower() == "true":
        return None
    try:
        w = float(p["Westernmost_longitude"])
        e = float(p["Easternmost_longitude"])
        s = float(p["Minimum_latitude"])
        n = float(p["Maximum_latitude"])
    except (KeyError, ValueError, TypeError):
        return None
    return (min(w, e), max(w, e), min(s, n), max(s, n))


def incidence(p):
    try:
        return float(p["Incidence_angle"])
    except (KeyError, ValueError, TypeError):
        return None


def best_pair(products, min_overlap=0.10):
    """Overlapping pair with the largest incidence difference, or None.

    Same NAC sensor on both sides (LE with LE): the two CCDs have their own radiometry,
    and mixing them adds a difference that is not the illumination difference we are
    trying to isolate.
    """
    usable = []
    for p in products:
        name = str(p.get("Product_name", ""))
        box, inc = bbox(p), incidence(p)
        if name.endswith("LE.IMG") and box and inc is not None and p.get("LabelURL"):
            usable.append((p, box, inc))

    best, best_diff = None, 0.0
    for i in range(len(usable)):
        for j in range(i + 1, len(usable)):
            (pa, ba, ia), (pb, bb, ib) = usable[i], usable[j]
            if overlap_frac(ba, bb) < min_overlap:
                continue                      # hard gate, before illumination is looked at
            diff = abs(ia - ib)
            if diff > best_diff:
                best, best_diff = (pa, pb, overlap_frac(ba, bb)), diff
    return best, best_diff, len(usable)


def download(url, dest):
    """Fetch url to dest, skipping a file already on disk. NAC EDRs are ~250-500 MB."""
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"  have {dest} ({os.path.getsize(dest) / 1e6:.0f} MB), skipping")
        return
    print(f"  {url}\n    -> {dest}")
    req = urllib.request.Request(url, headers={"User-Agent": "samanvay/download_pairs"})
    with urllib.request.urlopen(req, timeout=600) as resp, open(dest, "wb") as f:
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)


def sidecar(p, dest):
    """Write the MEASURED illumination fields only. See the module docstring."""
    meta = {"product_id": os.path.splitext(str(p.get("Product_name", "")))[0],
            "instrument": "LROC"}
    for key, ode_key in (("incidence_deg", "Incidence_angle"),
                         ("emission_deg", "Emission_angle"),
                         ("phase_deg", "Phase_angle")):
        try:
            meta[key] = float(p[ode_key])
        except (KeyError, ValueError, TypeError):
            pass                              # absent stays absent; never invented
    with open(dest, "w") as f:
        json.dump(meta, f, indent=4)
    print(f"  sidecar -> {dest}  ({', '.join(k for k in meta if k.endswith('_deg'))})")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--lat", type=float, default=-85.0, help="target latitude")
    ap.add_argument("--lon", type=float, default=25.0, help="target longitude (0-360)")
    ap.add_argument("--radius", type=float, default=0.5, help="ODE search radius, degrees")
    ap.add_argument("--min-overlap", type=float, default=0.10,
                    help="reject pairs sharing less than this fraction of the smaller footprint")
    ap.add_argument("--out-dir", default="data/real")
    args = ap.parse_args(argv)

    os.makedirs(args.out_dir, exist_ok=True)

    print(f"Querying ODE for LROC NAC EDR near ({args.lat}, {args.lon}) r={args.radius}...")
    products = query_ode(args.lat, args.lon, args.radius)
    print(f"  {len(products)} products returned")

    pair, diff, n_usable = best_pair(products, args.min_overlap)
    if pair is None:
        print(f"\nNo overlapping NAC pair among {n_usable} usable products.")
        print("ODE's search radius does not confine results to the region, so a wide")
        print("--radius returns frames from all over the Moon. Try a different --lat/--lon,")
        print("or lower --min-overlap if you accept a sliver.")
        return 1

    src_p, ref_p, frac = pair
    print(f"\nSelected pair: incidence difference {diff:.1f} deg, overlap {frac:.0%}")
    for tag, p in (("source", src_p), ("reference", ref_p)):
        print(f"  {tag:9} {p['Product_name']}  inc={p['Incidence_angle']}  "
              f"lat[{p['Minimum_latitude']}..{p['Maximum_latitude']}]")
    if diff < 20.0:
        print(f"  note: {diff:.1f} deg is below the 20 deg RIFT threshold in docs/DATA.md.")

    paths = {}
    for tag, p in (("src", src_p), ("ref", ref_p)):
        label_url = p["LabelURL"]
        img_url = label_url.rsplit(".", 1)[0] + ".IMG"
        img = os.path.join(args.out_dir, f"{tag}.img")
        print(f"\n{tag}:")
        download(label_url, os.path.join(args.out_dir, f"{tag}.xml"))
        download(img_url, img)
        sidecar(p, img + ".json")
        paths[tag] = img

    print("\nThese are raw PDS .IMG. If samanvay check cannot read them, convert once:")
    print(f"  gdal_translate {paths['src']} {args.out_dir}/src.tif")
    print("\nRunning preflight...")
    return subprocess.run(["samanvay", "check",
                           "--source", paths["src"], "--ref", paths["ref"]]).returncode


def _selfcheck():
    """The overlap gate must beat illumination. This is the bug the module exists to avoid."""
    def prod(name, inc, w, e, s, n):
        return {"Product_name": name, "Incidence_angle": str(inc), "LabelURL": "x.xml",
                "Westernmost_longitude": str(w), "Easternmost_longitude": str(e),
                "Minimum_latitude": str(s), "Maximum_latitude": str(n)}

    # The real failure, in miniature: a huge incidence difference over different ground
    # must lose to a smaller one over shared ground.
    near_a = prod("aLE.IMG", 80.0, 25.0, 26.0, -85.0, -84.0)
    near_b = prod("bLE.IMG", 50.0, 25.0, 26.0, -85.0, -84.0)   # 30 deg, full overlap
    far = prod("cLE.IMG", 1.0, 295.0, 296.0, 1.0, 2.0)         # 79 deg, no overlap
    pair, diff, _ = best_pair([near_a, near_b, far])
    assert pair is not None and abs(diff - 30.0) < 1e-9, diff
    assert {p["Product_name"] for p in pair[:2]} == {"aLE.IMG", "bLE.IMG"}, pair

    # No overlapping pair at all is a refusal, not a fallback to the best non-overlapping one.
    assert best_pair([near_a, far])[0] is None

    # Right sensor is not mixed with left, and a product without a footprint is skipped.
    assert best_pair([near_a, prod("bRE.IMG", 50.0, 25.0, 26.0, -85.0, -84.0)])[0] is None
    assert bbox({"Footprints_cross_meridian": "True"}) is None
    assert bbox({"Westernmost_longitude": "26", "Easternmost_longitude": "25",
                 "Minimum_latitude": "-84", "Maximum_latitude": "-85"}) == (25.0, 26.0, -85.0, -84.0)

    # Sidecar carries measured angles only -- never a substituted sun azimuth or a
    # derived elevation, which metadata.py produces itself with honest provenance.
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "s.json")
        sidecar({"Product_name": "M1.IMG", "Incidence_angle": "83.4",
                 "Emission_angle": "2.1"}, path)
        with open(path) as f:
            meta = json.load(f)
    assert meta["product_id"] == "M1", meta
    assert meta["incidence_deg"] == 83.4 and meta["emission_deg"] == 2.1, meta
    assert "sun_az_deg" not in meta and "sun_el_deg" not in meta, meta
    assert "phase_deg" not in meta, meta      # absent in, absent out
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        sys.exit(main())
