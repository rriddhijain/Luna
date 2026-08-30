"""Rank candidate Chandrayaan-2 scene pairs before you spend a download slot.

Two things decide whether a pair is worth downloading, and the Pradan results
table shows neither directly:

  1. Do the footprints OVERLAP?  A perfect sun difference over different ground
     is worthless. This is the hard gate.
  2. How different is the ILLUMINATION?  Governed by the synodic month
     (29.53 d), so two scenes a whole number of synodic months apart are lit
     almost identically however far apart the calendar dates are. Only the
     remainder matters -- which is why "pick different dates" is not enough.

Usage -- one scene per line, ID first, then optionally its bounding box as
four numbers  W E S N  (west east south north, degrees), read off the PDS
label you get by clicking the product ID in the results list:

    ch2_tmc_ndn_20211122T1726562077_d_oth_d18   31.2 33.8 -70.9 -68.1
    ch2_tmc_ndn_20240124T0838058678_d_oth_d18   30.9 34.1 -71.2 -67.6

    python scripts/pick_pairs.py candidates.txt

Omit the four numbers and you get the illumination ranking only, with overlap
reported as unknown. Extra trailing columns are ignored.

The sun figure is an ESTIMATE from timestamps -- it knows nothing about the
site or the orbit. The real number comes from the label after download, and
`samanvay check` reports it.
"""

import re
import sys
from itertools import combinations

SYNODIC_DAYS = 29.530588   # mean synodic month

# ch2_tmc_ndn_20211122T1726562077_d_oth_d18 -> date, time, varying sub-second tail.
_STAMP = re.compile(r"(\d{8})T(\d{6})(\d*)")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")


def parse_jdn(text):
    """Julian Day Number (fractional) of the first timestamp in `text`, or None.

    Absolute epoch is irrelevant here -- only differences are ever used.
    Hand-rolled rather than strptime because the sub-second digit count varies
    between products and instruments.
    """
    m = _STAMP.search(text)
    if not m:
        return None
    ymd, hms = m.group(1), m.group(2)
    y, mo, d = int(ymd[:4]), int(ymd[4:6]), int(ymd[6:8])
    hh, mm, ss = int(hms[:2]), int(hms[2:4]), int(hms[4:6])
    a = (14 - mo) // 12                       # Fliegel-Van Flandern, exact in ints
    yy = y + 4800 - a
    jdn = d + (153 * (mo + 12 * a - 3) + 2) // 5 + 365 * yy \
        + yy // 4 - yy // 100 + yy // 400 - 32045
    return jdn + (hh * 3600 + mm * 60 + ss) / 86400.0


def hour_angle_delta(jdn_a, jdn_b):
    """Change in solar hour angle across the gap, folded to [0, 180] degrees."""
    deg = 360.0 * (abs(jdn_a - jdn_b) % SYNODIC_DAYS) / SYNODIC_DAYS
    return 360.0 - deg if deg > 180.0 else deg


def parse_bbox(text):
    """Trailing 'W E S N' degrees, or None. Order is strict.

    No attempt to auto-detect a swapped lat/lon order: for a mid-latitude strip
    both pairs are inside [-90, 90] and the two readings are indistinguishable,
    so guessing would silently invent a footprint. Out-of-range latitudes are
    rejected instead, and the caller says which line it dropped.

    ponytail: no dateline wrap. A strip spanning +-180 lon reports 0% overlap
    and needs splitting by hand; Ch-2 polar strips do not hit this.
    """
    nums = [float(n) for n in _NUM.findall(text.split(maxsplit=1)[-1])] \
        if " " in text.strip() else []
    # The timestamp inside the ID is not a coordinate: only read numbers that
    # appear AFTER the first whitespace, which the split above guarantees.
    if len(nums) < 4:
        return None
    w, e, s, n = nums[:4]
    if not (-90 <= s <= 90 and -90 <= n <= 90):
        return None
    return (min(w, e), max(w, e), min(s, n), max(s, n))


def overlap_frac(a, b):
    """Intersection as a fraction of the SMALLER box, in [0, 1].

    Fraction of the smaller box, not of the union: what matters for registration
    is whether the smaller scene is covered, not how much area the pair shares.
    """
    ix = max(0.0, min(a[1], b[1]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[2], b[2]))
    inter = ix * iy
    smaller = min((a[1] - a[0]) * (a[3] - a[2]), (b[1] - b[0]) * (b[3] - b[2]))
    return 0.0 if smaller <= 0 else inter / smaller


def verdict(delta_deg, frac):
    if frac is not None and frac <= 0.0:
        return "NO OVERLAP  different ground -- do not download"
    if frac is not None and frac < 0.10:
        return "sliver      too little shared ground"
    sun = ("too similar" if delta_deg < 15 else
           "modest" if delta_deg < 40 else
           "good" if delta_deg <= 120 else
           "extreme -- check neither is near-terminator")
    if delta_deg < 15:
        return f"SKIP        sun {sun}"
    if frac is None:
        return f"sun {sun:<12} overlap UNKNOWN -- check the label"
    if delta_deg < 40:
        return f"weak        sun {sun}"
    return f"TAKE        sun {sun}"


def rank(lines):
    scenes = []
    for raw in lines:
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        jdn = parse_jdn(raw)
        if jdn is None:
            print(f"  ! no timestamp, skipped: {raw[:70]}", file=sys.stderr)
            continue
        scenes.append((raw.split()[0], jdn, parse_bbox(raw)))

    pairs = []
    for (ida, ja, ba), (idb, jb, bb) in combinations(scenes, 2):
        frac = overlap_frac(ba, bb) if (ba and bb) else None
        pairs.append((ida, idb, hour_angle_delta(ja, jb), frac))
    # Overlapping pairs first, then by illumination difference. An unknown
    # overlap sorts with the overlapping ones -- it is a prompt, not a rejection.
    pairs.sort(key=lambda p: (0 if p[3] is None or p[3] >= 0.10 else 1, -p[2]))
    return scenes, pairs


def main(argv):
    src = open(argv[1]) if len(argv) > 1 else sys.stdin
    scenes, pairs = rank(src)
    if len(scenes) < 2:
        print("Need at least 2 scenes with parseable timestamps.")
        return 1

    n_box = sum(1 for _, _, b in scenes if b)
    print(f"\n{len(scenes)} scenes ({n_box} with a bounding box), "
          f"{len(pairs)} candidate pairs\n")
    print(f"{'d(sun)':>8} {'overlap':>8}  verdict")
    print("-" * 78)
    for a, b, delta, frac in pairs:
        ov = "   ?" if frac is None else f"{frac:6.0%}"
        print(f"{delta:6.1f}deg {ov:>8}  {verdict(delta, frac)}")
        print(f"           A  {a}")
        print(f"           B  {b}")
    if n_box < len(scenes):
        print(f"\n{len(scenes) - n_box} scene(s) had no bounding box. Click the product ID "
              "in the\nresults list, read W/E/S/N off the PDS label, append them to the line.")
    print("\nd(sun) is estimated from timestamps only. Confirm after download:")
    print("  samanvay check --source A --ref B --dem DTM")
    return 0


def _selfcheck():
    j = parse_jdn("ch2_tmc_ndn_20211122T1726562077_d_oth_d18")
    assert j is not None and abs(j - 2459541.226) < 0.6, j
    # A whole synodic month apart must read as near-identical lighting.
    assert hour_angle_delta(0.0, SYNODIC_DAYS) < 0.1
    assert hour_angle_delta(0.0, 2 * SYNODIC_DAYS) < 0.1
    assert abs(hour_angle_delta(0.0, SYNODIC_DAYS / 2) - 180.0) < 0.1
    assert 88 < hour_angle_delta(0.0, SYNODIC_DAYS / 4) < 92

    # Overlap, as a fraction of the smaller box.
    assert overlap_frac((0, 10, 0, 10), (0, 10, 0, 10)) == 1.0
    assert overlap_frac((0, 10, 0, 10), (20, 30, 0, 10)) == 0.0
    assert overlap_frac((0, 10, 0, 10), (0, 5, 0, 10)) == 1.0     # small inside big
    assert abs(overlap_frac((0, 10, 0, 10), (5, 15, 0, 10)) - 0.5) < 1e-9
    assert overlap_frac((0, 10, 0, 10), (10, 20, 0, 10)) == 0.0   # edge-touching

    # The ID's own timestamp digits must never be read as coordinates.
    assert parse_bbox("ch2_tmc_ndn_20211122T1726562077_d_oth_d18") is None
    assert parse_bbox("ch2_x_20211122T172656_d 31.2 33.8 -70.9 -68.1") \
        == (31.2, 33.8, -70.9, -68.1)
    # Latitudes out of range are rejected, never reinterpreted.
    assert parse_bbox("id 31.2 33.8 -70.9 -168.1") is None

    # No overlap must dominate the verdict however good the sun difference is.
    assert verdict(90.0, 0.0).startswith("NO OVERLAP")
    assert verdict(90.0, 0.6).startswith("TAKE")
    assert verdict(4.0, 0.9).startswith("SKIP")
    assert "UNKNOWN" in verdict(90.0, None)

    # A no-overlap pair must sort below an overlapping one despite a better sun.
    _, pairs = rank(["a_20200101T000000 0 1 0 1",
                     "b_20200108T000000 50 51 50 51",
                     "c_20200109T000000 0 1 0 1"])
    assert pairs[0][3] and pairs[0][3] > 0, pairs[0]
    print("selfcheck ok")


if __name__ == "__main__":
    if "--selfcheck" in sys.argv:
        _selfcheck()
    else:
        sys.exit(main(sys.argv))
