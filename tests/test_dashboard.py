"""Seat 4 — the cross-run dashboard and the run inspector.

Two things are being defended here: that a run is never silently dropped or given a
plausible default when its metrics are missing, and that both pages stay air-gap
clean — no CDN, no webfont, no fetch, nothing that resolves over a network.
"""

import json
import os
import re

import pytest

from samanvay.report.dashboard import build_dashboard, build_viewer, find_runs

VIEWER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "viewer")

# The fabricated figures that shipped as static text in the old viewer/index.html.
FABRICATED = ("0.42px", "48/50", "93.8%", "0.18s")

GOOD = {
    "model_type": "affine", "rmse_px": 0.512, "gt_rmse_px": 0.55, "inlier_count": 52,
    "match_count": 197, "inlier_ratio": 0.264, "coverage_pct": 93.75, "dispersion_cv": 0.67,
    "grid_n": 2, "runtime_s": 3.14, "illum_mode": "empirical", "pc_status": "computed",
    "canonicalised": True, "rmse_trustworthy": True, "rmse_warning": None,
    "stage_s": {"load": 0.04, "match": 2.8, "refine": 0.08},
    "cell_counts": [7, 0, 3, 0],
    "cell_states": ["populated", "insufficient_texture", "populated", "masked_invalid"],
}
THIN = dict(GOOD, model_type="homography", rmse_px=0.00004, inlier_count=5,
            rmse_trustworthy=False,
            rmse_warning="5 inliers for a 4-point model (redundancy 1): rmse_px is near-zero "
                         "by construction and must not be quoted as accuracy")


def _run(root, name, metrics=None, extra=None):
    d = root / name
    d.mkdir(parents=True)
    if metrics is not None:
        (d / "metrics.json").write_text(json.dumps(metrics))
    (d / "transform.json").write_text(json.dumps({"model_type": "affine", "params": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}))
    (d / "provenance.json").write_text(json.dumps({"timestamp_utc": "2026-08-29T00:00:00+00:00",
                                                   "git_sha": "abc123", "inputs": {}}))
    (d / "matches.csv").write_text(
        "id,src_x,src_y,ref_x,ref_y,score,is_inlier,residual_px,sigma_px,grid_cell\n"
        "0,10.5,20.5,11.0,21.0,0.9,1,0.31,nan,0\n"
        "1,90.0,80.0,91.0,81.0,0.4,0,7.90,0.12,3\n")
    for name_, text in (extra or {}).items():
        (d / name_).write_text(text)
    return d


@pytest.fixture()
def runs_root(tmp_path):
    root = tmp_path / "runs"
    _run(root, "alpha_run", GOOD, extra={"report.html": "<html></html>"})
    _run(root, "beta_run", THIN)
    return root


def test_both_runs_appear(runs_root, tmp_path):
    out = build_dashboard(runs_root, tmp_path / "index.html")
    text = open(out, encoding="utf-8").read()
    assert text.lstrip().startswith("<!DOCTYPE html>")
    assert text.rstrip().endswith("</html>")
    assert "alpha_run" in text and "beta_run" in text


def test_embedded_payload_is_valid_json(runs_root, tmp_path):
    out = build_dashboard(runs_root, tmp_path / "index.html")
    text = open(out, encoding="utf-8").read()
    blob = re.search(r'<script id="dash-data" type="application/json">(.*?)</script>', text, re.S).group(1)
    payload = json.loads(blob)
    names = {r["name"] for r in payload["runs"]}
    assert names == {"alpha_run", "beta_run"}
    alpha = next(r for r in payload["runs"] if r["name"] == "alpha_run")
    # links are relative to the page, which here sits one level above runs_root
    assert alpha["rmse_px"] == 0.512 and alpha["report"] == "runs/alpha_run/report.html"


def test_missing_metrics_is_shown_not_dropped(runs_root, tmp_path):
    _run(runs_root, "gamma_run", metrics=None)
    out = build_dashboard(runs_root, tmp_path / "index.html")
    text = open(out, encoding="utf-8").read()
    assert "gamma_run" in text
    assert "no metrics" in text
    payload = json.loads(re.search(r'id="dash-data" type="application/json">(.*?)</script>',
                                   text, re.S).group(1))
    gamma = next(r for r in payload["runs"] if r["name"] == "gamma_run")
    assert gamma["status"] == "no metrics"
    assert gamma["rmse_px"] is None                      # never invented
    assert "not found" in gamma["status_detail"]


def test_unreadable_metrics_is_shown_not_dropped(runs_root, tmp_path):
    _run(runs_root, "delta_run", metrics=None, extra={"metrics.json": "{ truncated"})
    out = build_dashboard(runs_root, tmp_path / "index.html")
    payload = json.loads(re.search(r'id="dash-data" type="application/json">(.*?)</script>',
                                   open(out, encoding="utf-8").read(), re.S).group(1))
    delta = next(r for r in payload["runs"] if r["name"] == "delta_run")
    assert delta["status"] == "no metrics"
    assert "unreadable" in delta["status_detail"]


def test_dashboard_is_air_gap_clean(runs_root, tmp_path):
    text = open(build_dashboard(runs_root, tmp_path / "index.html"), encoding="utf-8").read()
    assert "http" not in text                            # covers http://, https:// and any CDN host
    assert "fetch(" not in text and "XMLHttpRequest" not in text
    assert "<link" not in text and "src=\"" not in text.replace('type="application/json"', "")


def test_untrusted_rmse_is_visibly_marked(runs_root, tmp_path):
    text = open(build_dashboard(runs_root, tmp_path / "index.html"), encoding="utf-8").read()
    assert "rmse not trustworthy" in text                # static, before any JS runs
    assert THIN["rmse_warning"] in text                  # the reason, not just the flag
    assert "untrusted" in text                           # the row styling hook
    payload = json.loads(re.search(r'id="dash-data" type="application/json">(.*?)</script>',
                                   text, re.S).group(1))
    beta = next(r for r in payload["runs"] if r["name"] == "beta_run")
    assert beta["rmse_trustworthy"] is False


def test_uniformity_states_survive_to_the_page(runs_root, tmp_path):
    text = open(build_dashboard(runs_root, tmp_path / "index.html"), encoding="utf-8").read()
    assert "insufficient_texture" in text and "masked_invalid" in text
    assert ".s-insufficient_texture" in text and ".s-masked_invalid" in text   # distinct styling


def test_stage_breakdown_is_embedded(runs_root, tmp_path):
    text = open(build_dashboard(runs_root, tmp_path / "index.html"), encoding="utf-8").read()
    payload = json.loads(re.search(r'id="dash-data" type="application/json">(.*?)</script>',
                                   text, re.S).group(1))
    alpha = next(r for r in payload["runs"] if r["name"] == "alpha_run")
    assert alpha["stage_s"] == GOOD["stage_s"]


def test_empty_and_missing_roots_do_not_crash(tmp_path):
    assert find_runs(tmp_path / "nope") == []
    out = build_dashboard(tmp_path / "nope", tmp_path / "a" / "index.html")
    assert "no runs found" in open(out, encoding="utf-8").read()
    (tmp_path / "empty").mkdir()
    out2 = build_dashboard(tmp_path / "empty", tmp_path / "b.html")
    assert os.path.exists(out2)


def test_viewer_shell_carries_no_metrics():
    shell = open(os.path.join(VIEWER_DIR, "index.html"), encoding="utf-8").read()
    app = open(os.path.join(VIEWER_DIR, "app.js"), encoding="utf-8").read()
    for fake in FABRICATED:
        assert fake not in shell, f"{fake} still hardcoded in viewer/index.html"
        assert fake not in app, f"{fake} still hardcoded in viewer/app.js"
    assert "cdnjs" not in shell and "fonts.googleapis" not in shell
    assert "http" not in shell
    assert '<script id="samanvay-run" type="application/json">null</script>' in shell


def test_build_viewer_embeds_the_run(runs_root, tmp_path):
    out = build_viewer(runs_root / "alpha_run")
    text = open(out, encoding="utf-8").read()
    for fake in FABRICATED:
        assert fake not in text
    assert "cdnjs" not in text and "fonts.googleapis" not in text
    blob = re.search(r'<script id="samanvay-run" type="application/json">(.*?)</script>', text, re.S).group(1)
    payload = json.loads(blob)
    assert payload["metrics"]["coverage_pct"] == 93.75
    assert len(payload["matches"]) == 2
    assert payload["matches"][0]["sigma_px"] is None      # nan is unknown, not zero
    assert payload["matches"][1]["is_inlier"] == 0
    assert "app.js" not in text.split("<script>")[0]      # app.js inlined, not linked
    assert payload["image_notes"]                          # rasters absent: said so, not faked


def test_build_viewer_on_a_bare_run_directory(tmp_path):
    bare = tmp_path / "runs" / "bare"
    bare.mkdir(parents=True)
    out = build_viewer(bare)
    payload = json.loads(re.search(r'id="samanvay-run" type="application/json">(.*?)</script>',
                                   open(out, encoding="utf-8").read(), re.S).group(1))
    assert payload["metrics"] is None
    assert "not found" in payload["metrics_error"]
    assert payload["matches"] == []


def test_dashboard_links_generated_viewers(runs_root, tmp_path):
    out = build_dashboard(runs_root, runs_root / "index.html", viewers=True)
    payload = json.loads(re.search(r'id="dash-data" type="application/json">(.*?)</script>',
                                   open(out, encoding="utf-8").read(), re.S).group(1))
    alpha = next(r for r in payload["runs"] if r["name"] == "alpha_run")
    assert alpha["viewer"] == "alpha_run/viewer.html"
    assert os.path.exists(runs_root / "alpha_run" / "viewer.html")
