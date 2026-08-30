"""End-to-end walking skeleton: one command produces all six artifacts.

The numbers on a 128 px fixture are not expected to be good — this asserts that
the plumbing runs and that every artifact lands, which is milestone M1.
"""

import json
import os
import shutil

import pytest

from samanvay.pipeline.stages import run_pipeline
from synth.render_pair import render_synthetic_pair

ARTIFACTS = ["registered.tif", "matches.csv", "transform.json",
             "metrics.json", "report.html", "provenance.json"]

# The keys .github/workflows/ci.yml gates on; metrics.json is the contract with
# every downstream consumer (report, viewer, bench harness, ablation table).
REQUIRED_METRIC_KEYS = ["rmse_px", "inlier_count", "inlier_ratio", "coverage_pct",
                        "dispersion_cv", "grid_n", "runtime_s"]


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    out = tmp_path_factory.mktemp("fixture")
    info = render_synthetic_pair(out_dir=str(out), ref_shape=(128, 128), seed=0)
    return info["source"], info["reference"], str(out)


def _run(pair, tmp_path, name, config=None):
    source, reference, _ = pair
    out_dir = str(tmp_path / name)
    metrics = run_pipeline(source, reference, out_dir, config=config or {})
    return out_dir, metrics


def test_pipeline_writes_all_six_artifacts(pair, tmp_path):
    out_dir, _ = _run(pair, tmp_path, "demo")
    for name in ARTIFACTS:
        assert os.path.exists(os.path.join(out_dir, name)), f"missing artifact: {name}"


def test_metrics_json_satisfies_the_contract(pair, tmp_path):
    out_dir, _ = _run(pair, tmp_path, "metrics")
    with open(os.path.join(out_dir, "metrics.json")) as handle:
        metrics = json.load(handle)
    missing = [k for k in REQUIRED_METRIC_KEYS if k not in metrics]
    assert not missing, f"metrics.json missing required keys: {missing}"
    # An undefined metric must be null, never a plausible-looking zero.
    if metrics["inlier_count"] == 0:
        assert metrics["rmse_px"] is None


def test_provenance_records_a_real_sha_and_no_invented_seed(pair, tmp_path):
    out_dir, _ = _run(pair, tmp_path, "prov")
    with open(os.path.join(out_dir, "provenance.json")) as handle:
        prov = json.load(handle)
    assert prov.get("git_sha") != "skeleton-sha-12345"
    # Nobody passed a seed, so provenance must not claim one was used.
    assert prov.get("seed") is None


def test_canonicaliser_toggle_actually_changes_the_run(pair, tmp_path):
    """The headline ablation. If this passes trivially, the toggle is not wired."""
    _, on = _run(pair, tmp_path, "canon_on", {"photometry": {"canonicalise": True}})
    _, off = _run(pair, tmp_path, "canon_off", {"photometry": {"canonicalise": False}})
    assert on["canonicalised"] is True and off["canonicalised"] is False
    assert off["illum_mode"] == "none"
    assert on["illum_mode"] in ("dem", "dem_lowfreq", "empirical")
    assert on["pc_status"] == "computed" and off["pc_status"] == "disabled"


def test_stage_timings_are_recorded(pair, tmp_path):
    _, metrics = _run(pair, tmp_path, "timing")
    stage_s = metrics.get("stage_s") or {}
    for stage in ("load", "coarse_init", "canonicalise", "match", "verify", "metrics", "warp"):
        assert stage in stage_s, f"bench/harness reads stage_s; missing {stage}"


def test_subpixel_toggle_is_honoured(pair, tmp_path):
    _, off = _run(pair, tmp_path, "sub_off", {"geometry": {"subpixel": False}})
    assert off["subpixel_applied"] is False


def test_coarse_init_is_used_by_default(pair, tmp_path):
    """P2: matching blind is a degraded mode we must never enter by accident."""
    _, metrics = _run(pair, tmp_path, "init")
    assert metrics.get("init_method") not in (None, "disabled")
