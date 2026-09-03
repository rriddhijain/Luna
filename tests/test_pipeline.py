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


def test_auto_resolves_the_method_and_the_two_photometry_switches(pair, tmp_path):
    """match.method="auto" is the shipped default; nothing downstream understands it.

    The rule is io/preflight.py's: unknown or >= 20 deg delta sun azimuth -> rift. The
    run must record what it resolved AND what the rule recommended, or the choice is
    unauditable from metrics.json alone.
    """
    _, metrics = _run(pair, tmp_path, "auto")
    assert metrics["match_method_requested"] == "auto"
    assert metrics["match_method_resolved"] in ("rift", "sift")
    assert metrics["match_method_resolved"] == metrics["match_method_recommended"]
    assert metrics["match_method_reason"]
    dsun = metrics["delta_sun_az_deg"]
    expected = "rift" if (dsun is None or dsun >= 20.0) else "sift"
    assert metrics["match_method_resolved"] == expected
    # PC is computed only for the descriptor that reads it; CLAHE only for the arms that
    # match on intensity. Both are booleans by the time canonicalise sees them.
    assert metrics["phase_congruency_resolved"] is (expected in ("rift", "l2"))
    assert metrics["clahe_resolved"] is (expected in ("sift", "orb"))
    assert metrics["clahe_applied"] is metrics["clahe_resolved"]
    assert metrics["pc_status"] == ("computed" if metrics["phase_congruency_resolved"]
                                   else "disabled")


def test_a_pinned_method_is_not_overridden_but_the_recommendation_is_still_reported(
        pair, tmp_path):
    """An ablation asked for sift. It gets sift — and the run says the rule disagreed."""
    _, metrics = _run(pair, tmp_path, "pinned", {"match": {"method": "sift"}})
    assert metrics["match_method_resolved"] == "sift"
    assert metrics["match_method_recommended"] == "rift"   # 50 deg on this fixture
    assert metrics["clahe_resolved"] is True and metrics["phase_congruency_resolved"] is False


def test_the_delivered_raster_honours_an_accepted_spline(pair, tmp_path):
    """The worst defect class in this repo: metrics.json advertising a warp the file ignores.

    _warp is exercised directly with a known non-zero spline, because whether a 128 px
    fixture's TPS is accepted is the geometry stage's business, not the warp's.
    """
    import numpy as np

    from samanvay.geometry.tps import ThinPlateSpline
    from samanvay.io.loaders import load_product
    from samanvay.pipeline.stages import _warp

    source_path, ref_path, _ = pair
    source, reference = load_product(source_path), load_product(ref_path)
    H = np.eye(3)

    class _Reg:
        params = H
        warp = None

    projective = _warp(source, reference, _Reg(), {"warp_interp": "linear"})

    # A spline that shifts everything 3 px in x: control points on a coarse grid, zero
    # bending, the constant term carrying the shift. Any live remap path must move the
    # image; warpPerspective on the same params cannot.
    nodes = np.array([[0.0, 0.0], [64.0, 0.0], [0.0, 64.0], [64.0, 64.0]])
    tps = ThinPlateSpline(nodes, np.zeros((4, 2)), np.array([[3.0, 0.0], [0.0, 0.0],
                                                            [0.0, 0.0]]),
                          0.0, nodes.mean(axis=0), 32.0)
    assert np.allclose(tps.displacement([[10.0, 10.0]]), [[3.0, 0.0]])

    class _RegTps(_Reg):
        warp = tps

    warped = _warp(source, reference, _RegTps(), {"warp_interp": "linear"})
    assert warped.shape == projective.shape
    assert not np.array_equal(warped, projective), "the spline never reached the raster"
    # And it is the RIGHT displacement, not merely a different one: remap pulls from
    # (x + 3, y), so the delivered pixel at x equals the projective pixel at x + 3.
    h, w = projective.shape[:2]
    assert np.allclose(warped[10:h - 10, 10:w - 13].astype(float),
                       projective[10:h - 10, 13:w - 10].astype(float), atol=1.0)


def test_metrics_and_provenance_carry_the_run_that_produced_them(pair, tmp_path):
    """The passthrough contract: canonicaliser params, the init gate counters, the seed."""
    out_dir, metrics = _run(pair, tmp_path, "carry", {"seed": 7})
    for key in ("clahe_applied", "clahe_clip", "clahe_grid", "mask_fill",
                "phase_congruency_requested", "init_gated_out", "init_gate_px",
                "init_gate_fallback", "verify_init_source", "cell_info"):
        assert key in metrics, f"metrics.json dropped {key}"
    # mask_fill_px is an int or null — never 0 standing in for "the fill never ran".
    assert metrics["mask_fill_px"] is None or isinstance(metrics["mask_fill_px"], int)
    # cell_info comes off the cascade level whose matches were returned, so it is null
    # exactly when no level fitted anything — which is the honest state on a 128 px
    # fixture. Where the run did register it must be there: it was null on EVERY default
    # run before Phase C, which is what made the per-cell ANMS state unverifiable.
    if metrics["verify_status"] == "ok":
        assert metrics["cell_info"] is not None, "cell_info is null on the default path"
    # --seed reaches exactly one RNG and provenance says which. A run that seeded nothing
    # must not imply reproducibility control it does not have.
    assert metrics["seed_applied"] is True and "cv2" in metrics["seed_applied_to"]
    with open(os.path.join(out_dir, "provenance.json")) as handle:
        prov = json.load(handle)
    assert prov["seed"] == 7 and prov["config"]["seed_applied"] is True
    # The resolved switches, not the literal "auto", are what provenance records.
    assert prov["config"]["match"]["method"] in ("rift", "sift")
    assert isinstance(prov["config"]["photometry"]["phase_congruency"], bool)


def test_default_output_grid_also_writes_the_source_resolution_product(pair, tmp_path):
    """output.grid defaults to "both"; without the pipeline half only the thumbnail lands."""
    out_dir, metrics = _run(pair, tmp_path, "grid")
    assert metrics["output_grid"] == "both" and metrics["registered_source_written"] is True
    assert os.path.exists(os.path.join(out_dir, "registered_source_grid.tif"))


def _fake_run(status, out_dir, extra=None):
    """A run_pipeline stand-in that writes the metrics.json the CLI copies and reads."""
    def run_pipeline(source, ref, out, config=None):
        metrics = {"verify_status": status, "verify_reason": "synthetic",
                   "match_count": 3, "inlier_ratio": None, "inlier_ratio_target": 0.85,
                   "inlier_ratio_pass": None, "match_method_resolved": "sift",
                   "match_method_recommended": "rift", "delta_sun_az_deg": 50.0}
        metrics.update(extra or {})
        os.makedirs(out, exist_ok=True)
        with open(os.path.join(out, "metrics.json"), "w") as handle:
            json.dump(metrics, handle)
        return metrics
    return run_pipeline


def test_cli_exits_non_zero_on_a_failed_registration(tmp_path, monkeypatch):
    """A failed registration that exits 0 is how the README's own quickstart failed silently."""
    from click.testing import CliRunner

    from samanvay.pipeline import run as run_module
    import samanvay.pipeline.stages as stages

    out = str(tmp_path / "failed")
    monkeypatch.setattr(stages, "run_pipeline", _fake_run("failed", out))
    result = CliRunner().invoke(run_module.register, [
        "--source", "s.tif", "--ref", "r.tif", "--out", out])
    assert result.exit_code != 0
    assert "FAILED" in result.output


def test_cli_writes_the_metrics_copy_and_flags_a_pinned_method(tmp_path, monkeypatch):
    """--metrics PATH is the plan's CLI shape; the preflight disagreement must be visible."""
    from click.testing import CliRunner

    from samanvay.pipeline import run as run_module
    import samanvay.pipeline.stages as stages

    out = str(tmp_path / "ok")
    extra = str(tmp_path / "elsewhere" / "report.json")
    monkeypatch.setattr(stages, "run_pipeline", _fake_run("ok", out))
    result = CliRunner().invoke(run_module.register, [
        "--source", "s.tif", "--ref", "r.tif", "--out", out, "--metrics", extra])
    assert result.exit_code == 0, result.output
    assert json.load(open(extra))["verify_status"] == "ok"
    assert "preflight would recommend match.method=rift" in result.output


def test_cli_survives_a_failure_where_the_init_gate_never_ran(tmp_path, monkeypatch):
    """verify.py reports init_gated_out=0 with init_gate_px=None when there was no init.

    Keying the failure line's gate clause on the COUNT rather than on the threshold made
    the CLI raise TypeError: float(None) instead of printing the reason — reproduced end
    to end with --set match.coarse_init=false --set match.cascade_enabled=false on
    fixtures/synth_pair_A, where the traceback replaced the whole summary block.
    """
    from click.testing import CliRunner

    from samanvay.pipeline import run as run_module
    import samanvay.pipeline.stages as stages

    out = str(tmp_path / "nogate")
    monkeypatch.setattr(stages, "run_pipeline", _fake_run(
        "failed", out, {"init_gated_out": 0, "init_gate_px": None,
                        "init_gate_fallback": False}))
    result = CliRunner().invoke(run_module.register, [
        "--source", "s.tif", "--ref", "r.tif", "--out", out])
    assert result.exit_code == 1, result.output
    assert not isinstance(result.exception, TypeError), result.exception
    assert "FAILED: verify_status=failed" in result.output
    # No gate ran, so the line must not mention one at all.
    assert "gated out" not in result.output
    assert "model_type" in result.output, "the summary block never printed"


def test_cache_key_separates_two_products_that_look_identical(tmp_path):
    """Two different images sharing product_id, shape, size and mtime-second must not collide.

    synth/sweep.py emits exactly that: one renderer so one product_id, one shape,
    near-identical size, all written inside the same second. Before the path and the
    nanosecond mtime went into the identity, the second delta-sun step matched on the
    FIRST step's cached albedo — a silent wrong answer that would have corrupted the
    sweep table in bench/baselines.md rather than failing.
    """
    import numpy as np
    import rasterio

    from samanvay.pipeline.stages import _canonicalise_cached
    from samanvay.types import Product

    cache = {"enabled": True, "dir": str(tmp_path / "cache")}
    rng = np.random.default_rng(0)
    canon = []
    for name in ("a.tif", "b.tif"):
        path = tmp_path / name
        # Same shape and same dtype, so the two files are the same size on disk.
        data = rng.random((64, 64), dtype=np.float32)
        with rasterio.open(path, "w", driver="GTiff", height=64, width=64,
                           count=1, dtype="float32") as dst:
            dst.write(data, 1)
        os.utime(path, (1_700_000_000, 1_700_000_000))   # identical mtime second
        product = Product(path=str(path), array=data,
                          meta={"product_id": "SAME_ID", "shape": (64, 64),
                                "dtype": "float32", "sun_az_deg": 10.0, "sun_el_deg": 45.0})
        canon.append(_canonicalise_cached(product, {"photometry": {"phase_congruency": False},
                                                    "cache": cache}))

    assert not np.array_equal(canon[0].albedo, canon[1].albedo), \
        "the second product was served the first product's cached albedo"


def test_a_fit_with_no_held_out_points_is_not_accepted():
    """verify_status "ok" is not the same question as "may anyone quote this".

    The real Chandrayaan-2 TMC -> LRO WAC pair fitted a model on 4 inliers of 24 matches,
    held nothing out (check_status "skipped_too_few_matches", n_check 0), scored SDI 0.09
    — and reported verify_status "ok" and exit 0. Reproduced end to end against
    data/real/ch2_wac before this gate existed.
    """
    from samanvay.pipeline.stages import acceptance

    clean = {"verify_status": "ok", "check_status": "ok", "rmse_trustworthy": True,
             "n_check": 23, "redundancy": 60}
    accepted, reasons = acceptance(clean)
    assert accepted and reasons == []

    # The exact shape of runs/docs_real_ch2_wac.
    wac = {"verify_status": "ok", "check_status": "skipped_too_few_matches",
           "rmse_trustworthy": False, "n_check": 0, "redundancy": 2}
    accepted, reasons = acceptance(wac)
    assert not accepted
    assert any("check_status=skipped_too_few_matches" in r for r in reasons)
    assert any("trust bar" in r for r in reasons)

    # A failed fit fails for its own reason and still carries the others.
    accepted, reasons = acceptance({"verify_status": "failed", "verify_reason": "no model"})
    assert not accepted and any("verify_status=failed" in r for r in reasons)
