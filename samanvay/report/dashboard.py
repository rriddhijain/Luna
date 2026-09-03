"""Seat 4 · report/dashboard — the cross-run comparison table and the per-run inspector.

Pillar: evidence a judge can check. Two surfaces, both self-contained:

  build_dashboard(runs_root, out_path) — one HTML file listing every run under
  runs_root, sorted on any column, with the per-stage runtime breakdown and the
  uniformity grid in a per-run detail row.

  build_viewer(run_dir, out_path) — viewer/index.html + viewer/app.js with that
  run's metrics.json, matches.csv and (when readable) its rasters inlined as one
  JSON literal, so the inspector works from a plain file path.

Every number on both pages comes from a run's own artifacts. A run whose
metrics.json is missing or unreadable is listed with an explicit "no metrics"
state; a field the run did not report renders as an em-dash, never as a default.
There is not one external asset, no CDN and no fetch: fetch() is blocked on
file:// anyway, which is why the run payload is embedded rather than loaded.

ponytail: the http path (a ?run= query param fetching a run's viewer_data.json
when the tree is actually served over a web server) is not implemented — the
air-gapped file:// path is the requirement, and app.js marks the hook.
"""

import base64
import csv
import datetime as _dt
import html as _htmlmod
import json
import math
import os
from pathlib import Path

import cv2
import numpy as np

from samanvay.io.loaders import load_product
from samanvay.report.render import _display, _full_shape

# A directory is a run if it carries any pipeline artifact. Deliberately not keyed
# on metrics.json alone: a run that died before writing metrics must still be listed.
RUN_MARKERS = ("metrics.json", "transform.json", "provenance.json",
               "report.html", "matches.csv", "registered.tif")

VIEWER_DIR = Path(__file__).resolve().parents[2] / "viewer"

MAX_VIEWER_MATCHES = 5000   # beyond this the page bloats; the excess is reported, not hidden
VIEWER_MAX_PX = 1100        # longest edge of an inlined raster


# ---------------------------------------------------------------- reading runs

def _finite(obj):
    """Deep-copy with every non-finite float replaced by None so json.dumps is strict-valid."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def _load_json(path):
    """(object, None) on success, (None, honest reason) otherwise. Never raises."""
    if not os.path.exists(path):
        return None, f"{os.path.basename(path)} not found"
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return _finite(json.load(fh)), None
    except Exception as exc:                       # truncated, empty, not JSON, unreadable
        return None, f"{os.path.basename(path)} unreadable: {type(exc).__name__}: {exc}"


def find_runs(runs_root):
    """Every directory under runs_root that carries a pipeline artifact, sorted by path."""
    root = Path(runs_root)
    if not root.is_dir():
        return []
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        if here == root:
            continue
        if any(marker in filenames for marker in RUN_MARKERS):
            found.append(here)
    return sorted(found)


def _rel(target, start):
    """Relative link from the page to a run artifact, forward slashes, or None if absent."""
    if not os.path.exists(target):
        return None
    return os.path.relpath(target, start).replace(os.sep, "/")


def _row(run_dir, root, link_base):
    """One dashboard row. Absent fields stay None; they render as "not reported"."""
    metrics, metrics_err = _load_json(run_dir / "metrics.json")
    transform, _ = _load_json(run_dir / "transform.json")
    prov, _ = _load_json(run_dir / "provenance.json")
    m = metrics if isinstance(metrics, dict) else {}
    t = transform if isinstance(transform, dict) else {}
    p = prov if isinstance(prov, dict) else {}
    inputs = p.get("inputs") or {}

    row = {
        "name": str(run_dir.relative_to(root)).replace(os.sep, "/"),
        "status": "ok" if metrics is not None else "no metrics",
        "status_detail": metrics_err,
        "model_type": m.get("model_type") or t.get("model_type"),
        # check_rmse_px is the accuracy number: held out from the fit. rmse_px is kept
        # beside it and labelled in-sample, never as a substitute.
        "check_rmse_px": m.get("check_rmse_px"),
        "check_rmse_all_px": m.get("check_rmse_all_px"),
        "check_p90_px": m.get("check_p90_px"),
        "check_status": m.get("check_status"),
        "n_check": m.get("n_check"),
        "n_control": m.get("n_control"),
        "rmse_px": m.get("rmse_px"),
        "gt_rmse_px": m.get("gt_rmse_px"),
        "inlier_count": m.get("inlier_count"),
        "match_count": m.get("match_count"),
        "inlier_ratio": m.get("inlier_ratio"),
        "inlier_ratio_pass": m.get("inlier_ratio_pass"),
        "inlier_ratio_target": m.get("inlier_ratio_target"),
        "sdi": m.get("sdi"),
        "sdi_definition": m.get("sdi_definition"),
        "coverage_pct": m.get("coverage_pct"),
        "dispersion_cv": m.get("dispersion_cv"),
        "tps_status": m.get("tps_status"),
        "tps_n_control": m.get("tps_n_control"),
        "tps_check_rmse_before_px": m.get("tps_check_rmse_before_px"),
        "tps_check_rmse_after_px": m.get("tps_check_rmse_after_px"),
        "match_method_resolved": m.get("match_method_resolved"),
        "match_method_reason": m.get("match_method_reason"),
        "verify_init_source": m.get("verify_init_source"),
        "mask_fill": m.get("mask_fill"),
        "clahe_applied": m.get("clahe_applied"),
        "seed_applied": m.get("seed_applied"),
        "seed_reason": m.get("seed_reason"),
        "illum_mode": m.get("illum_mode"),
        "pc_status": m.get("pc_status"),
        "canonicalised": m.get("canonicalised"),
        "runtime_s": m.get("runtime_s"),
        "rmse_trustworthy": m.get("rmse_trustworthy"),
        "rmse_warning": m.get("rmse_warning"),
        "verify_status": m.get("verify_status"),
        "stage_s": m.get("stage_s") if isinstance(m.get("stage_s"), dict) else None,
        "cell_counts": m.get("cell_counts"),
        "cell_states": m.get("cell_states"),
        "grid_n": m.get("grid_n"),
        # cell_counts is shaped rows x cols, which is only grid_n x grid_n on a square
        # source; geometry.uniformity.grid_shape keeps cells near-square otherwise.
        "grid_rows": m.get("grid_rows"),
        "grid_cols": m.get("grid_cols"),
        "report": _rel(run_dir / "report.html", link_base),
        "viewer": _rel(run_dir / "viewer.html", link_base),
        "timestamp_utc": p.get("timestamp_utc"),
        "git_sha": p.get("git_sha"),
        "source_path": (inputs.get("source") or {}).get("path"),
        "reference_path": (inputs.get("reference") or {}).get("path"),
    }
    return row


def _json_script(payload):
    """JSON safe to drop inside a <script> element."""
    try:
        text = json.dumps(payload, allow_nan=False, default=str)
    except ValueError:                    # a non-finite slipped past _finite
        text = json.dumps(_finite(payload), allow_nan=False, default=str)
    return text.replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def _now():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------- the dashboard

def _fallback_line(row):
    """One <li> of the noscript list: the run, its state, and the untrusted-rmse flag."""
    esc = _htmlmod.escape
    text = esc(row["name"]) + " \u2014 " + esc(row["status"])
    if row["status"] != "ok" and row["status_detail"]:
        text += " (" + esc(str(row["status_detail"])) + ")"
    if row["rmse_trustworthy"] is False:
        text += " \u2014 rmse not trustworthy: " + esc(str(row["rmse_warning"] or "flagged by the redundancy gate"))
    # The headline accuracy figure and the plan's inlier bar, before any JS runs.
    if row["check_rmse_px"] is not None:
        text += " \u2014 check_rmse_px (held out) " + esc(str(row["check_rmse_px"]))
    elif row["check_status"] not in (None, "ok"):
        text += " \u2014 no held-out rmse: check_status=" + esc(str(row["check_status"]))
    if row["inlier_ratio_pass"] is not None:
        text += " \u2014 inlier ratio " + ("PASS" if row["inlier_ratio_pass"] else "FAIL") + \
                " (" + esc(str(row["inlier_ratio"])) + " vs " + esc(str(row["inlier_ratio_target"])) + ")"
    return "<li>" + text + "</li>"



def build_dashboard(runs_root="runs", out_path="runs/index.html", viewers=False):
    """Write one self-contained HTML index of every run under runs_root. Returns out_path.

    viewers=True also generates <run>/viewer.html per run; it reads each run's rasters,
    so it is off by default. A run whose viewer cannot be built is still listed.
    """
    root = Path(runs_root)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    link_base = str(out.parent)

    runs = find_runs(root)
    if viewers:
        for run_dir in runs:
            try:
                build_viewer(run_dir)
            except Exception:             # one bad run must not lose the whole index
                pass

    rows = [_row(run_dir, root, link_base) for run_dir in runs]
    payload = {"runs_root": str(root), "generated_utc": _now(), "runs": rows}
    fallback = "".join(_fallback_line(r) for r in rows) or "<li>no runs found</li>"
    page = (_DASH_HTML
            .replace("__DATA__", _json_script(payload))
            .replace("__FALLBACK__", fallback))
    out.write_text(page, encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------- the inspector

def _num(text):
    """CSV cell to float/None. Empty, 'nan' and 'inf' all mean unknown, never zero."""
    if text is None:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return text
    return value if math.isfinite(value) else None


def _read_matches(path):
    """(rows, note). Rows are dicts of numbers; a missing or broken file is a note, not a crash."""
    if not os.path.exists(path):
        return [], "matches.csv not found"
    try:
        with open(path, "r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            rows = []
            for raw in reader:
                rows.append({k: _num(v) for k, v in raw.items() if k})
    except Exception as exc:
        return [], f"matches.csv unreadable: {type(exc).__name__}: {exc}"
    note = None
    if len(rows) > MAX_VIEWER_MATCHES:
        note = f"showing the first {MAX_VIEWER_MATCHES} of {len(rows)} matches"
        rows = rows[:MAX_VIEWER_MATCHES]
    return rows, note


def _embed_image(path):
    """(image dict, None) or (None, reason). Inlines a decimated PNG as a data URI."""
    if path is None:
        return None, "path not recorded in provenance.json"
    candidates = [Path(path)] if os.path.isabs(path) else [Path.cwd() / path, Path(path)]
    found = next((c for c in candidates if c.exists()), None)
    if found is None:
        return None, f"not found: {path}"
    try:
        product = load_product(str(found))
        img, scale = _display(product.array, VIEWER_MAX_PX)
        if img is None:
            return None, f"unreadable raster: {path}"
        u8 = (np.clip(np.asarray(img, dtype=np.float32), 0.0, 1.0) * 255.0).astype(np.uint8)
        ok, buf = cv2.imencode(".png", u8)
        if not ok:
            return None, f"PNG encode failed: {path}"
        full = _full_shape(product)
        if full is None:
            full = (int(round(u8.shape[0] / scale)), int(round(u8.shape[1] / scale))) if scale else None
        return {
            "src": "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii"),
            "w": int(u8.shape[1]), "h": int(u8.shape[0]),
            "full_w": int(full[1]) if full else None,
            "full_h": int(full[0]) if full else None,
            "path": str(path),
        }, None
    except Exception as exc:
        return None, f"{type(exc).__name__}: {exc}"


def build_viewer(run_dir, out_path=None):
    """Write <run_dir>/viewer.html: viewer/index.html + app.js with this run inlined.

    Returns the written path. Raises FileNotFoundError only if the viewer template
    itself is missing — a run missing artifacts still produces a page that says so.
    """
    run_dir = Path(run_dir)
    out = Path(out_path) if out_path else run_dir / "viewer.html"
    shell = VIEWER_DIR / "index.html"
    script = VIEWER_DIR / "app.js"
    for part in (shell, script):
        if not part.exists():
            raise FileNotFoundError(f"viewer template missing: {part}")

    metrics, metrics_err = _load_json(run_dir / "metrics.json")
    transform, transform_err = _load_json(run_dir / "transform.json")
    prov, _ = _load_json(run_dir / "provenance.json")
    matches, matches_note = _read_matches(run_dir / "matches.csv")
    p = prov if isinstance(prov, dict) else {}
    inputs = p.get("inputs") or {}

    images, notes = {}, {}
    wanted = {"source": (inputs.get("source") or {}).get("path"),
              "reference": (inputs.get("reference") or {}).get("path"),
              "registered": str(run_dir / "registered.tif")}
    for key, src in wanted.items():
        if key == "registered" and not os.path.exists(src):
            notes[key] = "registered.tif not found"
            continue
        image, reason = _embed_image(src)
        if image is None:
            notes[key] = reason
        else:
            images[key] = image

    payload = {
        "run": run_dir.name,
        "run_path": str(run_dir),
        "generated_utc": _now(),
        "metrics": metrics,
        "metrics_error": metrics_err,
        "transform": transform,
        "transform_error": transform_err,
        "matches": matches,
        "matches_note": matches_note,
        "images": images,
        "image_notes": notes,
        "provenance": {"timestamp_utc": p.get("timestamp_utc"), "git_sha": p.get("git_sha"),
                       "git_dirty": p.get("git_dirty")},
        "report": _rel(run_dir / "report.html", str(out.parent)),
    }

    page = shell.read_text(encoding="utf-8")
    page = page.replace('<script id="samanvay-run" type="application/json">null</script>',
                        '<script id="samanvay-run" type="application/json">'
                        + _json_script(payload) + '</script>')
    page = page.replace('<script src="app.js"></script>',
                        "<script>\n" + script.read_text(encoding="utf-8") + "\n</script>")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    return str(out)


# ---------------------------------------------------------------- the page itself
# Palette matches viewer/index.html and report/render.py so the three surfaces read
# as one system. Zero external assets: no CDN, no webfont link, no fetch.

_DASH_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SAMANVAY - run index</title>
<style>
:root{--bg:#0a0c10;--card:#12161e;--border:#242a35;--cyan:#00bcd4;--purple:#9c27b0;
      --text:#f5f6f9;--muted:#8a99ad;--amber:#ffb300;--outlier:#ff5370;}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:'Outfit',system-ui,-apple-system,sans-serif;padding:24px}
h1{font-size:20px;font-weight:600;letter-spacing:.5px}
.badge{background:linear-gradient(135deg,var(--cyan),var(--purple));color:#000;font-weight:800;
       padding:5px 10px;border-radius:6px;font-size:12px;letter-spacing:1px;text-transform:uppercase}
header{display:flex;align-items:center;gap:12px;margin-bottom:6px}
.sub{color:var(--muted);font-size:12px;margin-bottom:18px}
.wrap{background:var(--card);border:1px solid var(--border);border-radius:12px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{padding:8px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
th{position:sticky;top:0;background:#151a24;color:var(--muted);font-size:11px;text-transform:uppercase;
   letter-spacing:1px;cursor:pointer;user-select:none}
th:hover{color:var(--cyan)}
th.sorted{color:var(--cyan)}
tbody tr.run{cursor:pointer}
tbody tr.run:hover{background:rgba(0,188,212,.06)}
td.num{font-family:'JetBrains Mono',ui-monospace,monospace}
.none{color:var(--muted)}
tr.untrusted td{background:rgba(255,179,0,.07)}
tr.untrusted td:first-child{border-left:3px solid var(--amber)}
tr.nometrics td{background:rgba(255,83,112,.07)}
tr.nometrics td:first-child{border-left:3px solid var(--outlier)}
.flag{color:var(--amber);font-weight:700}
.state{font-size:11px;padding:2px 7px;border-radius:10px;border:1px solid var(--border);color:var(--muted)}
.state.ok{color:var(--cyan);border-color:rgba(0,188,212,.4)}
.state.bad{color:var(--outlier);border-color:rgba(255,83,112,.5)}
a{color:var(--cyan)}
tr.detail td{background:#0d1117;padding:16px 20px}
.panels{display:flex;gap:28px;flex-wrap:wrap;align-items:flex-start}
.panel{min-width:230px}
.ptitle{font-size:11px;text-transform:uppercase;letter-spacing:1.4px;color:var(--muted);margin-bottom:8px}
.warn{border-left:3px solid var(--amber);background:rgba(255,179,0,.08);padding:8px 12px;
      border-radius:4px;color:var(--amber);font-size:12px;margin-bottom:14px;max-width:900px;white-space:normal}
.err{border-left:3px solid var(--outlier);background:rgba(255,83,112,.08);padding:8px 12px;
     border-radius:4px;color:var(--outlier);font-size:12px;margin-bottom:14px;white-space:normal}
.bar{display:grid;grid-template-columns:90px 1fr 60px;gap:8px;align-items:center;font-size:12px;margin:3px 0}
.bar .track{background:#1b2129;border-radius:3px;height:9px}
.bar .fill{background:linear-gradient(90deg,var(--cyan),var(--purple));height:9px;border-radius:3px}
.bar .v{font-family:ui-monospace,monospace;color:var(--muted);text-align:right}
.ugrid{display:grid;gap:2px;width:max-content}
.cell{width:30px;height:30px;border-radius:3px;display:flex;align-items:center;justify-content:center;
      font-size:11px;font-family:ui-monospace,monospace;border:1px solid var(--border)}
.s-populated{background:#123039;color:var(--text)}
.s-insufficient_texture{background:repeating-linear-gradient(45deg,#3a2f12,#3a2f12 4px,#0a0c10 4px,#0a0c10 8px);
      border-color:var(--amber)}
.s-masked_invalid{background:repeating-linear-gradient(-45deg,#232833,#232833 4px,#0a0c10 4px,#0a0c10 8px);
      border-color:var(--muted)}
.s-unknown{background:radial-gradient(circle,#2b1f2e 1.5px,#0a0c10 1.5px);background-size:6px 6px;
      border-color:var(--purple)}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:11px;color:var(--muted);margin-top:8px}
.legend i{display:inline-block;width:12px;height:12px;border-radius:2px;vertical-align:-2px;
      margin-right:5px;border:1px solid var(--border)}
.kv{font-size:12px;color:var(--muted);line-height:1.7;white-space:normal;max-width:520px}
.kv b{color:var(--text);font-weight:600}
</style>
</head>
<body>
<header><span class="badge">SAMANVAY</span><h1>run index</h1></header>
<div class="sub" id="sub"></div>
<div class="wrap"><table id="t"><thead><tr id="hdr"></tr></thead><tbody id="tb"></tbody></table></div>
<noscript><ul>__FALLBACK__</ul></noscript>
<script id="dash-data" type="application/json">__DATA__</script>
<script>
var D = JSON.parse(document.getElementById("dash-data").textContent);
var COLS = [
  ["name","run",0],["status","state",0],["model_type","model",0],
  ["check_rmse_px","check rmse px (held out)",1],["rmse_px","rmse px (in-sample)",1],
  ["gt_rmse_px","gt rmse px",1],["inlier_count","inliers",1],["inlier_ratio","inlier ratio",1],
  ["inlier_ratio_pass","ratio vs plan",0],["sdi","sdi",1],
  ["coverage_pct","coverage %",1],["dispersion_cv","disp cv",1],["tps_status","tps",0],
  ["match_method_resolved","arm",0],["illum_mode","illum",0],
  ["pc_status","pc",0],["canonicalised","canon",0],["runtime_s","runtime s",1]
];
var STATES = {populated:"s-populated", insufficient_texture:"s-insufficient_texture",
              masked_invalid:"s-masked_invalid"};
var sortKey = "name", sortDir = 1;

function fmt(v, num){
  if (v === null || v === undefined) return null;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (num && typeof v === "number") return Math.abs(v) >= 1000 ? v.toFixed(1) : v.toFixed(3);
  return String(v);
}
function cell(row, key, num){
  var td = document.createElement("td");
  if (num) td.className = "num";
  var text = fmt(row[key], num);
  if (text === null){ td.className += " none"; td.textContent = "—";
                      td.title = key + " not reported by this run"; }
  else td.textContent = text;
  return td;
}
function el(tag, cls, text){
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = text;
  return e;
}
function grid(counts, states, n, gr, gc){
  var box = el("div");
  if (!counts || (!n && !gc)){ box.appendChild(el("div","kv","no uniformity grid reported")); return box; }
  // The grid is rows x cols, not n x n: a non-square source gets near-square cells from
  // geometry.uniformity.grid_shape, so 400x1200 at grid_n=4 is 4 rows by 12 columns.
  // grid_n is only the fallback for a run written before grid_rows/grid_cols existed.
  var rows = gr ? gr : n, cols = gc ? gc : n;
  if (counts.length !== rows*cols){
    box.appendChild(el("div","kv","cell_counts has "+counts.length+" cells, grid is "+
                    rows+"x"+cols+" which needs "+(rows*cols)));
    return box;
  }
  var max = 1;
  for (var j=0;j<counts.length;j++) if (typeof counts[j] === "number" && counts[j] > max) max = counts[j];
  var g = el("div","ugrid"); g.style.gridTemplateColumns = "repeat("+cols+",1fr)";
  for (var i=0;i<rows*cols;i++){
    var st = (states && states[i]) ? String(states[i]) : "unknown";
    var c = el("div","cell "+(STATES[st] || "s-unknown"));
    var cnt = counts[i];
    c.title = "cell "+i+" (col "+(i%cols)+", row "+Math.floor(i/cols)+") · "+st+" · count "+
              (cnt === null || cnt === undefined ? "unknown" : cnt);
    if (st === "populated"){
      c.textContent = (cnt === null || cnt === undefined) ? "?" : cnt;
      var f = (typeof cnt === "number") ? Math.max(0.12, cnt/max) : 0.12;
      c.style.background = "rgba(0,188,212,"+(0.10 + 0.72*f).toFixed(3)+")";
      c.style.color = f > 0.55 ? "#04222a" : "#f5f6f9";
    }
    g.appendChild(c);
  }
  box.appendChild(g);
  var leg = el("div","legend");
  [["s-populated","populated (count shown, shade = density)"],
   ["s-insufficient_texture","insufficient texture"],
   ["s-masked_invalid","masked / invalid"],
   ["s-unknown","state not reported"]].forEach(function(pair){
    var s = el("span"); var i2 = document.createElement("i");
    i2.className = pair[0]; s.appendChild(i2); s.appendChild(document.createTextNode(pair[1]));
    leg.appendChild(s);
  });
  box.appendChild(leg);
  return box;
}
function stages(stage_s){
  var box = el("div");
  if (!stage_s){ box.appendChild(el("div","kv","stage_s not reported by this run")); return box; }
  var keys = Object.keys(stage_s), max = 0;
  keys.forEach(function(k){ if (typeof stage_s[k] === "number" && stage_s[k] > max) max = stage_s[k]; });
  if (!keys.length){ box.appendChild(el("div","kv","stage_s is empty")); return box; }
  keys.forEach(function(k){
    var v = stage_s[k];
    var r = el("div","bar");
    r.appendChild(el("span",null,k));
    var track = el("div","track"), fill = el("div","fill");
    fill.style.width = (typeof v === "number" && max > 0 ? (100*v/max) : 0).toFixed(1) + "%";
    track.appendChild(fill); r.appendChild(track);
    r.appendChild(el("span","v", typeof v === "number" ? v.toFixed(3)+"s" : "—"));
    box.appendChild(r);
  });
  return box;
}
function detail(row){
  var td = el("td"); td.colSpan = COLS.length;
  if (row.status !== "ok")
    td.appendChild(el("div","err","no metrics: " + (row.status_detail || "metrics.json unavailable")));
  if (row.rmse_trustworthy === false)
    td.appendChild(el("div","warn","rmse not trustworthy · " +
      (row.rmse_warning || "rmse_trustworthy is false and no warning text was recorded")));
  var panels = el("div","panels");
  var p1 = el("div","panel"); p1.appendChild(el("div","ptitle","stage runtime"));
  p1.appendChild(stages(row.stage_s)); panels.appendChild(p1);
  var p2 = el("div","panel"); p2.appendChild(el("div","ptitle","tie-point uniformity"));
  p2.appendChild(grid(row.cell_counts, row.cell_states, row.grid_n, row.grid_rows, row.grid_cols));
  p2.appendChild(el("div","kv", row.sdi_definition || "sdi_definition not reported"));
  panels.appendChild(p2);
  var pA = el("div","panel"); pA.appendChild(el("div","ptitle","held-out accuracy"));
  var akv = el("div","kv");
  [["check_rmse_px (held out)", row.check_rmse_px],
   ["check_rmse_all_px", row.check_rmse_all_px],["check_p90_px", row.check_p90_px],
   ["n_check / n_control", (row.n_check === null || row.n_check === undefined ? "—" : row.n_check) +
                           " / " + (row.n_control === null || row.n_control === undefined ? "—" : row.n_control)],
   ["check_status", row.check_status],
   ["rmse_px (IN-SAMPLE, not accuracy)", row.rmse_px],
   ["inlier ratio vs plan", (row.inlier_ratio_pass === null || row.inlier_ratio_pass === undefined
      ? "unknown" : (row.inlier_ratio_pass ? "PASS" : "FAIL")) + " (" +
      (row.inlier_ratio === null || row.inlier_ratio === undefined ? "—" : row.inlier_ratio) +
      " vs " + (row.inlier_ratio_target === null || row.inlier_ratio_target === undefined
                ? "—" : row.inlier_ratio_target) + ")"],
   ["tps_status", row.tps_status],["tps_n_control", row.tps_n_control],
   ["tps check rmse before", row.tps_check_rmse_before_px],
   ["tps check rmse after", row.tps_check_rmse_after_px],
   ["match_method_resolved", row.match_method_resolved],
   ["match_method_reason", row.match_method_reason],
   ["verify_init_source", row.verify_init_source],["mask_fill", row.mask_fill],
   ["clahe_applied", row.clahe_applied],["seed_applied", row.seed_applied],
   ["seed_reason", row.seed_reason]].forEach(function(pair){
    var line = el("div");
    line.appendChild(el("b", null, pair[0] + ": "));
    line.appendChild(document.createTextNode(pair[1] === null || pair[1] === undefined
        ? "not reported" : String(pair[1])));
    akv.appendChild(line);
  });
  pA.appendChild(akv); panels.appendChild(pA);
  var p3 = el("div","panel"); p3.appendChild(el("div","ptitle","run"));
  var kv = el("div","kv");
  [["matches", row.match_count],["verify", row.verify_status],["run at", row.timestamp_utc],
   ["git", row.git_sha ? String(row.git_sha).slice(0,10) : null],
   ["source", row.source_path],["reference", row.reference_path]].forEach(function(pair){
    var line = el("div");
    line.appendChild(el("b", null, pair[0] + ": "));
    line.appendChild(document.createTextNode(pair[1] === null || pair[1] === undefined
        ? "not reported" : String(pair[1])));
    kv.appendChild(line);
  });
  var links = el("div"); links.style.marginTop = "8px";
  [["report.html", row.report],["viewer.html", row.viewer]].forEach(function(pair){
    if (pair[1]){ var a = document.createElement("a"); a.href = pair[1]; a.textContent = pair[0];
                  a.style.marginRight = "14px"; links.appendChild(a); }
    else { var s = el("span","none", pair[0]+" not built"); s.style.marginRight = "14px";
           links.appendChild(s); }
  });
  kv.appendChild(links);
  p3.appendChild(kv); panels.appendChild(p3);
  td.appendChild(panels);
  var tr = el("tr","detail"); tr.appendChild(td); tr.style.display = "none";
  return tr;
}
function render(){
  var rows = D.runs.slice().sort(function(a,b){
    var x = a[sortKey], y = b[sortKey];
    if (x === null || x === undefined) return 1;      // unknown always sinks
    if (y === null || y === undefined) return -1;
    if (typeof x === "number" && typeof y === "number") return (x-y)*sortDir;
    return String(x).localeCompare(String(y))*sortDir;
  });
  var hdr = document.getElementById("hdr"); hdr.innerHTML = "";
  COLS.forEach(function(c){
    var th = el("th", c[0] === sortKey ? "sorted" : null,
                c[1] + (c[0] === sortKey ? (sortDir > 0 ? " ▲" : " ▼") : ""));
    th.onclick = function(){ if (sortKey === c[0]) sortDir = -sortDir; else { sortKey = c[0]; sortDir = 1; }
                             render(); };
    hdr.appendChild(th);
  });
  var tb = document.getElementById("tb"); tb.innerHTML = "";
  rows.forEach(function(row){
    var tr = el("tr","run" + (row.status !== "ok" ? " nometrics" :
                              (row.rmse_trustworthy === false ? " untrusted" : "")));
    COLS.forEach(function(c){
      if (c[0] === "name"){
        var td = el("td");
        if (row.report){ var a = document.createElement("a"); a.href = row.report; a.textContent = row.name;
                         a.onclick = function(e){ e.stopPropagation(); }; td.appendChild(a); }
        else td.textContent = row.name;
        tr.appendChild(td);
      } else if (c[0] === "status"){
        var td2 = el("td");
        td2.appendChild(el("span","state "+(row.status === "ok" ? "ok" : "bad"), row.status));
        tr.appendChild(td2);
      } else if (c[0] === "inlier_ratio_pass"){
        // The plan's >85% bar. A miss is shown as a miss, in the failure colour.
        var td4 = el("td");
        if (row.inlier_ratio_pass === null || row.inlier_ratio_pass === undefined){
          td4.className = "none"; td4.textContent = "—";
          td4.title = "inlier_ratio_pass not reported by this run";
        } else {
          td4.appendChild(el("span","state "+(row.inlier_ratio_pass ? "ok" : "bad"),
                             row.inlier_ratio_pass ? "PASS" : "FAIL"));
          td4.title = "inlier_ratio " + row.inlier_ratio + " vs target " + row.inlier_ratio_target;
        }
        tr.appendChild(td4);
      } else {
        var td3 = cell(row, c[0], c[2]);
        if (c[0] === "rmse_px" && row.rmse_trustworthy === false){
          td3.appendChild(el("span","flag"," ⚠"));
          td3.title = row.rmse_warning || "rmse_trustworthy is false";
        }
        tr.appendChild(td3);
      }
    });
    var det = detail(row);
    tr.onclick = function(){ det.style.display = det.style.display === "none" ? "table-row" : "none"; };
    tb.appendChild(tr); tb.appendChild(det);
  });
  document.getElementById("sub").textContent =
    D.runs.length + " run(s) under " + D.runs_root + " · generated " + D.generated_utc +
    " · click a row for stage timings and the uniformity grid";
}
render();
</script>
</body>
</html>
"""
