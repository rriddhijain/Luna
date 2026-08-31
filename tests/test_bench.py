"""
tests/test_bench.py

OWNER: seat 5 (Systems & Performance).

The benchmark harness is part of CI's contract: it must run a pair through the
real pipeline, recover the known transform to sub-pixel-ish accuracy, and
demonstrate that caching canonicalisation is faster on the second run.
"""

from __future__ import annotations

import numpy as np

from bench.fixtures import PairSpec, ci_spec, render_registration_pair
from bench.harness import run_cache_benchmark, run_pair


def test_fixture_registers_to_known_transform(tmp_path):
    spec = ci_spec()
    result = run_pair(spec, str(tmp_path))
    assert result.status == "ok"
    assert result.inlier_count >= 8
    # the pipeline should recover the known similarity transform closely
    assert result.gt_rmse_px < 2.0
    # timings are populated
    assert result.total_s > 0
    assert "match" in result.timings


def test_render_pair_writes_expected_files(tmp_path):
    gt = render_registration_pair(str(tmp_path), PairSpec(name="t", size=128, seed=1))
    import os

    assert os.path.exists(gt["source"])
    assert os.path.exists(gt["reference"])
    assert os.path.exists(os.path.join(str(tmp_path), "gt.json"))
    H = np.array(gt["H_gt_source_to_ref"])
    assert H.shape == (3, 3)


def test_cache_benchmark_shows_speedup():
    # small size keeps the test fast; the mechanism (disk hit) is what matters
    res = run_cache_benchmark(size=256, seed=3)
    assert res.warm_s <= res.cold_s
    assert res.speedup >= 1.0
    assert res.stats["hits"] >= 1


def test_harness_quick_main_returns_zero(tmp_path):
    from bench.harness import main

    rc = main(["--quick", "--no-cache-bench", "--out", str(tmp_path / "results"),
               "--work", str(tmp_path / "work")])
    assert rc == 0
