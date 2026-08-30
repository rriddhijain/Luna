"""Seat 6 — tests for bench/calibrate.py and the sigma calibration it fits.

The bench exists to stop two constants being guesses, so these tests check the things
that would let a guess back in: that the fit runs end to end and returns a real number,
that the sigma it calibrates actually ranks errors on data it was not fitted on, and
that outside the validated regime it says "unknown" instead of something flattering.
"""

import os

import numpy as np
import pytest
from click.testing import CliRunner
from scipy.stats import spearmanr

from bench import calibrate
from samanvay.geometry import refine

# One small texture reused by every test: rendering it is the slow part.
BASE = calibrate.lunar_texture(size=256, seed=77)


def test_sigma_calibration_returns_a_finite_constant():
    rows, summary, _ = calibrate.calibrate_sigma(
        textures=(("cratered", dict(seed=3)),), blurs=(1.5,), noises=(0.05, 0.20),
        n_shift=1, size=256, fixture_dir="does/not/exist")

    assert summary["fit_arm"] == "synthetic"          # no fixtures: says so, does not pretend
    assert np.isfinite(summary["k_fit"]) and summary["k_fit"] > 0
    assert len(rows) == 2
    assert all(r["obs_rms_px"] > 0 and r["pred_rms_px"] > 0 for r in rows)


def test_fit_uses_the_fixture_arm_when_the_fixtures_are_there():
    if not os.path.isdir(calibrate.FIXTURE_DIR):
        pytest.skip("dsun_sweep fixtures not present")
    rows, summary, _ = calibrate.calibrate_sigma(
        textures=(("cratered", dict(seed=3)),), blurs=(1.5,), noises=(0.20,),
        n_shift=1, size=256)

    assert summary["fit_arm"] == "fixture"
    # The shipped constant is what this bench fitted, so a re-run must land back on it.
    assert summary["k_fit"] == pytest.approx(refine._NEFF_K, rel=0.35)
    assert summary["fixture_ratio_geomean"] == pytest.approx(1.0, abs=0.25)


def test_predicted_sigma_ranks_error_on_held_out_samples():
    # Held out: seeds and noise levels the fit above never touched.
    sig, err = [], []
    for noise in (0.02, 0.06, 0.15, 0.30):
        s, e, _, _ = calibrate.synthetic_case(BASE, 1.5, noise, n_shift=1,
                                              seed=4242 + int(noise * 100))
        sig.append(s)
        err.append(e)
    sig, err = np.concatenate(sig), np.concatenate(err)

    assert len(sig) > 40
    rho = spearmanr(sig, err).statistic
    # Measured 0.52 on this sample; the floor is what makes sigma worth reporting at all.
    assert rho > 0.4, f"sigma does not rank error: spearman {rho:.3f}"


def test_sigma_is_unknown_outside_the_validated_regime():
    curv, patch = 0.2, 32
    assert np.isnan(refine._sigma(0.9999, curv, patch)), "near-perfect peak must read unknown"
    assert np.isnan(refine._sigma(1.0, curv, patch))
    assert np.isfinite(refine._sigma(refine._RHO_MAX_VALIDATED - 0.01, curv, patch))


def test_a_noiseless_pair_refines_but_reports_unknown_uncertainty():
    sig, err, n_refined, n_unknown = calibrate.synthetic_case(BASE, 1.5, 0.0, n_shift=1, seed=1)

    assert n_refined > 0
    # NaN here means "refined, uncertainty unknown", not "left alone": the points moved.
    assert n_unknown > 0.5 * n_refined, f"{n_unknown}/{n_refined} unknown"
    assert np.isfinite(sig).all() and (sig > 0).all()   # whatever survived is a real number


def test_redundancy_sweep_finds_where_rmse_stops_tracking_truth():
    rows, summary = calibrate.calibrate_redundancy(
        points=(10, 16, 40, 80), outliers=(0.0, 0.4, 0.7, 0.9), seeds=10,
        runs_root="does/not/exist")

    assert summary["n_compared"] >= 100 and summary["n_pipeline_runs"] == 0
    assert summary["evidence_sufficient"]
    assert summary["recommend_min_redundancy"] is not None
    assert summary["recommend_trust_redundancy"] > summary["recommend_min_redundancy"]
    thin = [r for r in rows if r["name"] == "1-1"]
    fat = [r for r in rows if r["name"] == "50+"]
    if thin and fat:      # the whole point: a thin fit understates its own error
        assert thin[0]["med_ratio"] > fat[0]["med_ratio"]


def test_command_writes_its_table_and_figure(tmp_path):
    out = tmp_path / "cal"
    res = CliRunner().invoke(calibrate.main,
                             ["--out", str(out), "--runs", str(tmp_path / "empty"), "--quick"])

    assert res.exit_code == 0, res.output
    md = (out / "calibrate.md").read_text()
    assert "| name |" in md and "_NEFF_K" in md and "_TRUST_REDUNDANCY" in md
    assert (out / "calibrate.png").stat().st_size > 1000
    assert (out / "sigma_calibration.csv").exists() and (out / "redundancy.csv").exists()


def test_degenerate_inputs_do_not_crash():
    assert calibrate.fixture_case("does/not/exist") is None
    rows, summary, _ = calibrate.calibrate_sigma(
        textures=(), blurs=(), noises=(), fixture_dir="does/not/exist")
    assert rows == [] and summary["fit_arm"] is None and np.isnan(summary["k_fit"])
