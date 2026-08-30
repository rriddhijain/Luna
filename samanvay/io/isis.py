"""Seats 3 + 6 (I/O · geometry) — C11/C12: ISIS3-compatible tie-point export.

We do not replace ISIS3, we feed it. This module turns a SAMANVAY ``MatchSet`` plus its
``Registration`` into the two files a planetary photogrammetrist actually wants: an ISIS3
control network in its PVL text form (``qnet``/``cnetpvl2bin``/``jigsaw``) and a plain
tie-point CSV for a spreadsheet.

**NOT VALIDATED BY ISIS3.** No ISIS3 installation existed on the machine that wrote this
module, so the output is *format-compatible by construction and by round-trip test only*.
Somebody must load a written network with the real tools — ``cnetpvl2bin from=x.pvl
to=x.net`` then ``qnet`` or ``cneteditor`` — before demo day. Until that happens the claim
is "we emit the format", not "ISIS3 reads it".

The conversion that silently ruins everything
---------------------------------------------
ISIS3 ``Sample``/``Line`` are **1-based, pixel-centred**: the centre of the first pixel is
(1.0, 1.0). SAMANVAY is 0-based, pixel-centred: the same pixel centre is (0.0, 0.0). So::

    sample = x + 1.0        line = y + 1.0

Get it wrong and every tie-point is one pixel off, the fit still converges, and nobody
sees it until they open the network in qnet. ``test_isis.py`` asserts it explicitly.

Conventions chosen here, and why
--------------------------------
* ``PointType = Free``. A Free point's body-fixed XYZ is solved by ``jigsaw`` from its
  measures. ``Fixed``/``Constrained`` require an a priori lat/lon/radius that SAMANVAY
  does not produce — writing one would be a fabricated ground control point.
* ``Reference = True`` sits on the **source** measure, not on the measure from the
  reference image. ISIS's "reference measure" means the anchor the other measures were
  registered *to*; SAMANVAY detects keypoints in the source and registers them into the
  reference image, so the source measure is the ISIS reference measure. The two senses of
  "reference" are unrelated. Measure order in each point is source first, then reference.
* Outliers (``registration.inliers`` False) are written with ``Ignore = True``, never
  dropped: a control network that silently omits its rejected points cannot be reviewed.
* ``AprioriSample``/``AprioriLine`` on the reference measure is the coarse-init
  prediction ``init_params @ src_xy`` — a genuine a priori. It is omitted entirely when
  the init is missing or singular rather than being back-filled with the measurement.
* ``SerialNumber`` is a placeholder, ``SAMANVAY/<instrument>/<product_id>``. ISIS derives
  a real serial from instrument metadata in a cube label (``getsn from=x.cub``); we have
  no cube. The file says so in a comment at the top instead of inventing something that
  looks official.
* Residuals are *not* written as ``SampleResidual``/``LineResidual``: SAMANVAY residuals
  are in SOURCE pixels (frozen convention) and ISIS expects each measure's own frame, so
  the numbers would be a unit lie. They go in a per-point comment and in the CSV, and
  ``jigsaw`` recomputes its own anyway.

Public API::

    write_control_network(path, matches, registration, source, reference,
                          network_id=None, target="MOON") -> path
    write_tiepoint_csv(path, matches, registration, source, reference) -> path
    read_control_network(path) -> dict
"""

import csv
import datetime as _dt
import getpass
import math
import os

import numpy as np

# 0-based pixel centre (SAMANVAY) -> 1-based pixel centre (ISIS3). See the module docstring.
ISIS_ORIGIN_OFFSET = 1.0

# ControlNetVersioner's current on-disk version. Written into the header verbatim.
ISIS_CNET_VERSION = 5

TIEPOINT_COLUMNS = ["point_id", "source_sample", "source_line",
                    "reference_sample", "reference_line",
                    "score", "residual_px", "sigma_px", "is_inlier", "grid_cell"]

_UNVALIDATED = (
    "# ISIS3 control network (PVL) written by SAMANVAY.",
    "#",
    "# NOT VALIDATED BY ISIS3: no ISIS3 installation was available where this file was",
    "# produced. The format is compatible by construction, not by test. Load it once with",
    "#     cnetpvl2bin from=<this file> to=<name>.net    then qnet / cneteditor / jigsaw",
    "# before trusting it.",
    "#",
    "# Sample/Line are 1-based pixel centres: sample = x + 1, line = y + 1 from SAMANVAY's",
    "# 0-based (x, y) pixel-centre convention.",
    "#",
    "# SerialNumber values below are PLACEHOLDERS of the form SAMANVAY/<instrument>/",
    "# <product_id>. They are NOT the serial ISIS derives from a cube's instrument",
    "# metadata. Replace them with `getsn from=<cube>` output before running jigsaw.",
    "#",
    "# Reference = True marks the SOURCE measure (the anchor keypoints were detected in),",
    "# which is not the same thing as SAMANVAY's reference image. Points rejected by RANSAC",
    "# are present with Ignore = True rather than dropped.",
)


# ---------------------------------------------------------------- small helpers

def _text(value, fallback):
    """A one-token identifier from possibly-missing metadata; never invents a real name."""
    if value is None:
        return fallback
    out = "".join(c if (c.isalnum() or c in "._-/") else "_" for c in str(value).strip())
    return out or fallback


def _serial(product, fallback_role):
    """Placeholder ISIS serial number for a product. Unknowns say UNKNOWN, out loud."""
    meta = getattr(product, "meta", None) or {}
    instrument = _text(meta.get("instrument"), "UNKNOWN_INSTRUMENT")
    product_id = meta.get("product_id")
    if product_id is None:
        path = getattr(product, "path", None)
        product_id = os.path.splitext(os.path.basename(path))[0] if path else None
    return "SAMANVAY/{}/{}".format(instrument, _text(product_id, "UNKNOWN_" + fallback_role.upper()))


def _fmt(value):
    """Render a Python scalar as a PVL scalar. Floats use repr, so a read-back is exact."""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (int, np.integer)):
        return str(int(value))
    if isinstance(value, (float, np.floating)):
        return repr(float(value))
    return '"{}"'.format(str(value).replace('"', "'"))


def _kw(lines, indent, key, value, width=14, raw=False):
    """Append one PVL assignment. raw=True emits an ISIS enum token (Free, Candidate, ...)."""
    text = str(value) if raw else _fmt(value)
    lines.append("{}{:<{w}} = {}".format(" " * indent, key, text, w=width))


def _aligned(registration, n):
    """(inliers, residual_px, sigma) padded to n; a missing array becomes all-unknown."""
    def _get(name, shape):
        arr = getattr(registration, name, None) if registration is not None else None
        arr = np.asarray(arr) if arr is not None else np.empty(shape)
        return arr if len(arr) == n else np.empty(shape)

    inliers = _get("inliers", (0,))
    residuals = _get("residuals", (0, 2))
    sigma = _get("sigma", (0,))
    resid_px = (np.linalg.norm(np.asarray(residuals, dtype=float), axis=1)
                if len(residuals) else np.full(n, np.nan))
    return (inliers if len(inliers) else None,
            resid_px,
            np.asarray(sigma, dtype=float) if len(sigma) else np.full(n, np.nan))


def _apriori(init_params, src_xy):
    """Coarse-init prediction of each source point in the reference image, or None."""
    if init_params is None:
        return None
    H = np.asarray(init_params, dtype=float)
    if H.shape != (3, 3) or not np.all(np.isfinite(H)):
        return None
    from samanvay.geometry.init import apply_transform
    try:
        out = apply_transform(H, np.asarray(src_xy, dtype=float))
    except Exception:
        return None
    return out if np.asarray(out).shape == np.asarray(src_xy).shape else None


def _points(matches):
    """(n, src_xy, ref_xy, score, cell) from a possibly-empty or None MatchSet."""
    if matches is None or matches.src_xy is None or len(matches.src_xy) == 0:
        return 0, np.empty((0, 2)), np.empty((0, 2)), np.empty(0), np.empty(0, dtype=int)
    src = np.asarray(matches.src_xy, dtype=float).reshape(-1, 2)
    ref = np.asarray(matches.ref_xy, dtype=float).reshape(-1, 2)
    n = len(src)
    if len(ref) != n:
        raise ValueError(f"MatchSet.ref_xy has {len(ref)} entries, src_xy has {n}")
    score = np.asarray(matches.score, dtype=float) if matches.score is not None else np.empty(0)
    cell = np.asarray(matches.cell) if matches.cell is not None else np.empty(0)
    return (n, src, ref,
            score if len(score) == n else np.full(n, np.nan),
            cell if len(cell) == n else np.empty(0, dtype=int))


# ---------------------------------------------------------------- writers

def write_control_network(path, matches, registration, source, reference,
                          network_id=None, target="MOON"):
    """Write an ISIS3 control network (PVL text) for these tie-points; returns ``path``.

    One ``Object = ControlPoint`` per match, each with two ``Group = ControlMeasure``
    entries (source first, then reference). Outliers are kept with ``Ignore = True``.
    Points with a non-finite coordinate cannot be written as a PVL number and are omitted
    with the count stated in a comment — never silently.
    """
    n, src_xy, ref_xy, score, cell = _points(matches)
    inliers, resid_px, sigma = _aligned(registration, n)
    apriori = _apriori(getattr(registration, "init_params", None), src_xy)

    src_meta = getattr(source, "meta", None) or {}
    if network_id is None:
        network_id = _text("samanvay_" + str(src_meta.get("product_id") or "run"), "samanvay")
    src_serial = _serial(source, "source")
    ref_serial = _serial(reference, "reference")
    collided = src_serial == ref_serial
    if collided:
        # ISIS needs one serial per image; identical metadata would make the two measures
        # of a point indistinguishable. Suffix the placeholders and say so in the file.
        src_serial, ref_serial = src_serial + "/SOURCE", ref_serial + "/REFERENCE"

    model = getattr(registration, "model_type", None) or "unknown"
    metrics = getattr(registration, "metrics", None) or {}
    rmse = metrics.get("rmse_px")
    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    try:
        user = getpass.getuser()
    except Exception:                                   # no passwd entry (container)
        user = "unknown"

    good = [i for i in range(n)
            if np.all(np.isfinite(src_xy[i])) and np.all(np.isfinite(ref_xy[i]))]
    skipped = n - len(good)

    lines = list(_UNVALIDATED)
    if skipped:
        lines.append(f"#\n# {skipped} of {n} tie-points omitted: non-finite coordinates.")
    lines.append("")
    lines.append("Object = ControlNetwork")
    _kw(lines, 2, "NetworkId", network_id)
    _kw(lines, 2, "TargetName", str(target).upper())
    _kw(lines, 2, "UserName", user)
    _kw(lines, 2, "Created", now)
    _kw(lines, 2, "LastModified", now)
    _kw(lines, 2, "Description",
        "SAMANVAY {} registration, {} tie-points, RMSE {} source px. "
        "Not validated by an ISIS3 binary.".format(
            model, len(good), "unknown" if rmse is None else f"{float(rmse):.3f}"))
    _kw(lines, 2, "Version", ISIS_CNET_VERSION)
    lines.append("")
    lines.append("  # Source     measure serial: {}".format(src_serial))
    lines.append("  # Reference  measure serial: {}".format(ref_serial))
    if collided:
        lines.append("  # The two products carry identical instrument/product_id metadata,")
        lines.append("  # so the /SOURCE and /REFERENCE suffixes above were added to keep")
        lines.append("  # the measures distinguishable. Neither is an ISIS-derived serial.")

    for i in good:
        ignored = bool(inliers is not None and not bool(inliers[i]))
        lines.append("")
        lines.append("  Object = ControlPoint")
        _kw(lines, 4, "PointType", "Free", width=13, raw=True)   # an ISIS enum, unquoted
        _kw(lines, 4, "PointId", f"{network_id}_{i:06d}", width=13)
        _kw(lines, 4, "ChooserName", "samanvay", width=13)
        _kw(lines, 4, "DateTime", now, width=13)
        if inliers is not None:
            # Ignore = True is how a rejected point stays reviewable in qnet.
            _kw(lines, 4, "Ignore", ignored, width=13)
        if math.isfinite(resid_px[i]):
            lines.append("    # residual = {:.4f} px in SOURCE pixels (SAMANVAY frame; "
                         "jigsaw recomputes its own)".format(resid_px[i]))
        if len(score) and math.isfinite(score[i]):
            lines.append("    # match score = {:.4f}".format(float(score[i])))

        for role, xy, serial in (("source", src_xy[i], src_serial),
                                 ("reference", ref_xy[i], ref_serial)):
            subpixel = role == "reference" and math.isfinite(sigma[i])
            lines.append("    Group = ControlMeasure")
            _kw(lines, 6, "SerialNumber", serial, width=13)
            _kw(lines, 6, "MeasureType",
                "Candidate" if role == "source" else
                ("RegisteredSubPixel" if subpixel else "RegisteredPixel"),
                width=13, raw=True)
            _kw(lines, 6, "ChooserName", "samanvay", width=13)
            _kw(lines, 6, "DateTime", now, width=13)
            _kw(lines, 6, "Sample", float(xy[0]) + ISIS_ORIGIN_OFFSET, width=13)
            _kw(lines, 6, "Line", float(xy[1]) + ISIS_ORIGIN_OFFSET, width=13)
            if role == "reference" and apriori is not None and np.all(np.isfinite(apriori[i])):
                _kw(lines, 6, "AprioriSample", float(apriori[i][0]) + ISIS_ORIGIN_OFFSET, width=13)
                _kw(lines, 6, "AprioriLine", float(apriori[i][1]) + ISIS_ORIGIN_OFFSET, width=13)
            if role == "reference" and math.isfinite(sigma[i]):
                lines.append("      # 1-sigma = {:.4f} SOURCE px".format(sigma[i]))
            if role == "source":
                _kw(lines, 6, "Reference", True, width=13)
            lines.append("    End_Group")
        lines.append("  End_Object")

    lines.append("End_Object")
    lines.append("End")
    lines.append("")

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def write_tiepoint_csv(path, matches, registration, source, reference):
    """Write the interchange CSV (``TIEPOINT_COLUMNS``); returns ``path``.

    Sample/line are 1-based ISIS coordinates so the rows line up with the control network
    row for row. ``residual_px`` and ``sigma_px`` are SOURCE pixels; an unknown field is
    blank, never zero.
    """
    n, src_xy, ref_xy, score, cell = _points(matches)
    inliers, resid_px, sigma = _aligned(registration, n)
    src_meta = getattr(source, "meta", None) or {}
    network_id = _text("samanvay_" + str(src_meta.get("product_id") or "run"), "samanvay")

    def _num(value):
        return float(value) if value is not None and math.isfinite(value) else ""

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(TIEPOINT_COLUMNS)
        for i in range(n):
            writer.writerow([
                f"{network_id}_{i:06d}",
                float(src_xy[i, 0]) + ISIS_ORIGIN_OFFSET,
                float(src_xy[i, 1]) + ISIS_ORIGIN_OFFSET,
                float(ref_xy[i, 0]) + ISIS_ORIGIN_OFFSET,
                float(ref_xy[i, 1]) + ISIS_ORIGIN_OFFSET,
                _num(score[i]) if len(score) else "",
                _num(resid_px[i]),
                _num(sigma[i]),
                int(bool(inliers[i])) if inliers is not None else "",
                int(cell[i]) if len(cell) else "",
            ])
    return path


# ---------------------------------------------------------------- reader

def _scalar(token):
    """PVL scalar -> Python. A quoted token stays a string, so "000012" is not a float."""
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    if token in ("True", "true", "TRUE"):
        return True
    if token in ("False", "false", "FALSE"):
        return False
    try:
        return float(token)
    except ValueError:
        return token


def _parse_pvl(text):
    """Strict nested Object/Group reader. Raises ValueError on anything unbalanced.

    ponytail: line-oriented, one keyword per line, whole-line comments only — exactly the
    subset this module writes. It is a validator for our own output, not a PVL library.
    Upgrade path: continuation lines, (a, b) sequences and inline /* */ comments, which is
    where a real PVL parser starts.
    """
    root = {"name": "ROOT", "kind": "root", "keywords": {}, "children": []}
    stack = [root]
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("/*"):
            continue
        low = line.lower()
        if low in ("end_object", "end_group"):
            kind = low.split("_", 1)[1]
            if len(stack) == 1:
                raise ValueError(f"line {lineno}: {line} closes nothing")
            node = stack.pop()
            if node["kind"] != kind:
                raise ValueError(f"line {lineno}: {line} closes a {node['kind']} "
                                 f"({node['name']})")
            continue
        if low == "end":
            continue
        if "=" not in line:
            raise ValueError(f"line {lineno}: not a PVL assignment: {line!r}")
        key, value = (part.strip() for part in line.split("=", 1))
        if key.lower() in ("object", "group"):
            node = {"name": value.strip('"\''), "kind": key.lower(),
                    "keywords": {}, "children": []}
            stack[-1]["children"].append(node)
            stack.append(node)
            continue
        if len(stack) == 1:
            raise ValueError(f"line {lineno}: keyword {key} outside any Object/Group")
        stack[-1]["keywords"][key] = _scalar(value)
    if len(stack) != 1:
        raise ValueError(f"unclosed {stack[-1]['kind']} = {stack[-1]['name']}")
    return root


def read_control_network(path):
    """Parse a control network written by :func:`write_control_network`.

    Returns the header keywords plus ``points``: a list of the ControlPoint keywords, each
    with ``measures``, the list of its ControlMeasure keyword dicts in file order (source
    then reference). Sample/Line come back as floats in the 1-based ISIS convention — the
    caller subtracts :data:`ISIS_ORIGIN_OFFSET` to get back to SAMANVAY (x, y).
    """
    with open(path) as f:
        root = _parse_pvl(f.read())
    networks = [c for c in root["children"] if c["name"] == "ControlNetwork"]
    if len(networks) != 1:
        raise ValueError(f"expected exactly one ControlNetwork object, found {len(networks)}")
    net = networks[0]
    out = dict(net["keywords"])
    points = []
    for child in net["children"]:
        if child["name"] != "ControlPoint":
            raise ValueError(f"unexpected {child['kind']} = {child['name']} in ControlNetwork")
        point = dict(child["keywords"])
        measures = []
        for sub in child["children"]:
            if sub["name"] != "ControlMeasure":
                raise ValueError(f"unexpected {sub['kind']} = {sub['name']} in ControlPoint")
            measures.append(dict(sub["keywords"]))
        point["measures"] = measures
        points.append(point)
    out["points"] = points
    return out
