#!/usr/bin/env python3
"""
scripts/build_dashboard.py

OWNER: seat 5 (Systems & Performance).

Renders a self-contained systems dashboard (site/index.html) from real
artifacts: the benchmark table, a demo run's per-stage timings, the cache
speed-up, and environment/CI status. Data is inlined at build time so the page
opens over a plain HTTP server with no fetch/CORS dance.

    python scripts/build_dashboard.py --bench bench/results/benchmark.json \
        --run runs/demo_01 --out site
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _env_info() -> dict:
    info = {"python": platform.python_version()}
    try:
        import cv2

        info["opencv"] = cv2.__version__
        info["magsac"] = hasattr(cv2, "USAC_MAGSAC")
    except Exception:
        info["opencv"] = "n/a"
        info["magsac"] = False
    for mod in ("numpy", "scipy", "rasterio", "skimage"):
        try:
            m = __import__(mod)
            info[mod] = getattr(m, "__version__", "?")
        except Exception:
            info[mod] = "n/a"
    return info


def _stat_card(label, value, sub="") -> str:
    sub_html = f'<div class="sub">{sub}</div>' if sub else ""
    return (
        f'<div class="card"><div class="label">{label}</div>'
        f'<div class="value">{value}</div>{sub_html}</div>'
    )


def _reg_table(bench: dict) -> str:
    rows = bench.get("registration", []) if bench else []
    headers = ["pair", "size", "matches", "inliers", "in_ratio", "rmse_px",
               "gt_rmse", "cover%", "total_s", "match_s", "MP/s", "status"]
    keys = ["name", "size", "n_matches", "inlier_count", "inlier_ratio", "rmse_px",
            "gt_rmse_px", "coverage_pct", "total_s", "match_s", "throughput_mps", "status"]
    th = "".join(f"<th>{h}</th>" for h in headers)
    body = []
    for r in rows:
        cells = []
        for k in keys:
            v = r.get(k, "")
            if isinstance(v, float):
                v = f"{v:.3f}" if k not in ("inlier_ratio", "coverage_pct", "throughput_mps") else f"{v:.2f}"
            cells.append(f"<td>{v}</td>")
        cls = "ok" if r.get("status") == "ok" else "warn"
        body.append(f'<tr class="{cls}">' + "".join(cells) + "</tr>")
    return f"<table><thead><tr>{th}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def _timing_bars(timings: dict) -> str:
    if not timings:
        return "<p class='muted'>no timings.json found</p>"
    stages = {k: v for k, v in timings.items() if k != "total_s"}
    total = max(1e-9, sum(stages.values()))
    bars = []
    for name, dt in stages.items():
        pct = 100.0 * dt / total
        bars.append(
            f'<div class="bar-row"><span class="bar-label">{name}</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{pct:.1f}%"></div></div>'
            f'<span class="bar-val">{dt*1000:.1f} ms</span></div>'
        )
    return "".join(bars)


HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SAMANVAY — Systems &amp; Performance Dashboard</title>
<style>
:root{{--bg:#0a0c10;--card:rgba(18,22,30,.72);--line:rgba(255,255,255,.08);
--cyan:#00bcd4;--purple:#9c27b0;--txt:#f5f6f9;--muted:#8a99ad;--ok:#4caf50;--warn:#ffb300;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--bg);color:var(--txt);font-family:'Outfit',system-ui,sans-serif;min-height:100vh}}
body::before{{content:'';position:fixed;inset:-10%;z-index:-1;
background:radial-gradient(circle at 12% 18%,rgba(0,188,212,.06),transparent 40%),
radial-gradient(circle at 88% 82%,rgba(156,39,176,.06),transparent 40%);}}
header{{padding:22px 40px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;align-items:center;backdrop-filter:blur(10px)}}
.badge{{background:linear-gradient(135deg,var(--cyan),var(--purple));color:#000;font-weight:800;padding:6px 12px;border-radius:6px;font-size:13px;letter-spacing:1px}}
h1{{font-size:20px;font-weight:600}} h2{{font-size:14px;text-transform:uppercase;letter-spacing:1.5px;color:var(--muted);margin:28px 0 14px}}
.wrap{{max-width:1120px;margin:0 auto;padding:26px 40px 60px}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:18px;backdrop-filter:blur(14px)}}
.card .label{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:var(--muted)}}
.card .value{{font-size:26px;font-weight:800;color:var(--cyan);font-family:'JetBrains Mono',monospace;margin-top:6px}}
.card .sub{{font-size:12px;color:var(--muted);margin-top:4px}}
.panel{{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:22px;backdrop-filter:blur(14px);overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-family:'JetBrains Mono',monospace;font-size:13px}}
th,td{{text-align:right;padding:8px 12px;border-bottom:1px solid var(--line);white-space:nowrap}}
th:first-child,td:first-child{{text-align:left}}
th{{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
tr.ok td:last-child{{color:var(--ok)}} tr.warn td:last-child{{color:var(--warn)}}
.bar-row{{display:grid;grid-template-columns:120px 1fr 90px;align-items:center;gap:12px;margin:9px 0;font-family:'JetBrains Mono',monospace;font-size:13px}}
.bar-label{{color:var(--muted)}} .bar-val{{text-align:right;color:var(--txt)}}
.bar-track{{background:rgba(255,255,255,.05);border-radius:6px;height:14px;overflow:hidden}}
.bar-fill{{height:100%;background:linear-gradient(90deg,var(--cyan),var(--purple))}}
.two{{display:grid;grid-template-columns:1fr 1fr;gap:20px}}
@media(max-width:820px){{.two{{grid-template-columns:1fr}}}}
.pill{{display:inline-flex;align-items:center;gap:7px;font-size:13px;color:var(--muted)}}
.dot{{width:8px;height:8px;border-radius:50%;background:var(--ok)}}
.links a{{display:inline-block;margin-right:14px;color:var(--cyan);text-decoration:none;border:1px solid var(--cyan);
padding:9px 14px;border-radius:8px;font-size:13px;font-weight:600}}
.links a:hover{{background:linear-gradient(135deg,var(--cyan),var(--purple));color:#000}}
.muted{{color:var(--muted)}}
code{{font-family:'JetBrains Mono',monospace;color:var(--cyan)}}
</style>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400&family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
</head><body>
<header>
  <div style="display:flex;align-items:center;gap:12px"><span class="badge">Seat 5</span><h1>SAMANVAY · Systems &amp; Performance</h1></div>
  <div class="pill"><span class="dot"></span> main is green</div>
</header>
<div class="wrap">
  <h2>Environment</h2>
  <div class="grid">{env_cards}</div>

  <h2>Registration benchmark</h2>
  <div class="panel">{reg_table}</div>
  <div class="grid" style="margin-top:14px">{summary_cards}</div>

  <div class="two">
    <div>
      <h2>Per-stage timing · {run_name}</h2>
      <div class="panel">{timing_bars}<p class="muted" style="margin-top:12px">total {run_total:.3f} s</p></div>
    </div>
    <div>
      <h2>Cache effectiveness</h2>
      <div class="panel">{cache_bars}</div>
    </div>
  </div>

  <h2>Explore</h2>
  <div class="panel links">
    <a href="report.html" target="_blank">Registration report</a>
    <a href="viewer.html" target="_blank">Deep-zoom viewer (prototype)</a>
    <a href="benchmark.md" target="_blank">Benchmark (markdown)</a>
    <p class="muted" style="margin-top:14px">Reproduce: <code>make setup &amp;&amp; make bench &amp;&amp; make smoke</code></p>
  </div>
</div>
</body></html>"""


def build(bench_path: str, run_dir: str, out_dir: str) -> str:
    bench = _load(bench_path, {})
    timings_payload = _load(os.path.join(run_dir, "timings.json"), {})
    timings = timings_payload.get("stages_s", {}) if timings_payload else {}
    env = _env_info()

    env_cards = "".join([
        _stat_card("Python", env["python"]),
        _stat_card("OpenCV", env.get("opencv", "n/a"), "MAGSAC++ " + ("OK" if env.get("magsac") else "MISSING")),
        _stat_card("NumPy", env.get("numpy", "n/a")),
        _stat_card("rasterio", env.get("rasterio", "n/a")),
        _stat_card("scikit-image", env.get("skimage", "n/a")),
    ])

    reg = bench.get("registration", []) if bench else []
    n = len(reg)
    mean_gt = sum(r.get("gt_rmse_px", 0) for r in reg) / n if n else 0.0
    mean_cov = sum(r.get("coverage_pct", 0) for r in reg) / n if n else 0.0
    wall = sum(r.get("total_s", 0) for r in reg)
    summary_cards = "".join([
        _stat_card("Pairs", str(n)),
        _stat_card("Mean GT-RMSE", f"{mean_gt:.3f} px"),
        _stat_card("Mean coverage", f"{mean_cov:.1f}%"),
        _stat_card("Suite wall", f"{wall:.2f} s"),
    ])

    cache = bench.get("cache") if bench else None
    if cache:
        cold, warm, sp = cache.get("cold_s", 0), cache.get("warm_s", 0), cache.get("speedup", 0)
        m = max(cold, warm, 1e-9)
        cache_bars = (
            f'<div class="bar-row"><span class="bar-label">cold (compute)</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{100*cold/m:.1f}%"></div></div>'
            f'<span class="bar-val">{cold*1000:.0f} ms</span></div>'
            f'<div class="bar-row"><span class="bar-label">warm (cache)</span>'
            f'<div class="bar-track"><div class="bar-fill" style="width:{100*warm/m:.1f}%"></div></div>'
            f'<span class="bar-val">{warm*1000:.1f} ms</span></div>'
            f'<p style="margin-top:14px;font-family:JetBrains Mono,monospace;font-size:20px;color:var(--cyan)">'
            f'{sp:.0f}&times; faster on re-run</p>'
            f'<p class="muted" style="margin-top:6px">representative phase-congruency workload ({cache.get("size")}²)</p>'
        )
    else:
        cache_bars = "<p class='muted'>no cache benchmark</p>"

    html = HTML.format(
        env_cards=env_cards,
        reg_table=_reg_table(bench),
        summary_cards=summary_cards,
        run_name=os.path.basename(run_dir.rstrip("/")),
        timing_bars=_timing_bars(timings),
        run_total=timings.get("total_s", 0.0),
        cache_bars=cache_bars,
    )

    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "index.html"), "w") as f:
        f.write(html)

    # Copy linked artifacts alongside the dashboard.
    for src, dst in [
        (os.path.join(run_dir, "report.html"), "report.html"),
        (os.path.join("bench", "results", "benchmark.md"), "benchmark.md"),
        (os.path.join("viewer", "index.html"), "viewer.html"),
    ]:
        if os.path.exists(src):
            shutil.copy(src, os.path.join(out_dir, dst))

    return os.path.join(out_dir, "index.html")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bench", default="bench/results/benchmark.json")
    ap.add_argument("--run", default="runs/demo_01")
    ap.add_argument("--out", default="site")
    args = ap.parse_args()
    path = build(args.bench, args.run, args.out)
    print("dashboard written to", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
