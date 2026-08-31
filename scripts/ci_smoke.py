#!/usr/bin/env python3
"""
scripts/ci_smoke.py

OWNER: seat 5 (Systems & Performance).

The CI end-to-end gate (roadmap D1): render a 512x512 fixture with a known
ground-truth transform, run the WHOLE pipeline on it, and assert:

  * all six P0 artifacts (+ timings.json) are written;
  * the pipeline recovers the known transform (ground-truth RMSE within budget);
  * the run finishes well under the 5-minute CI budget.

Exits non-zero on any failure so `main` can only ever be green when a clean
machine can actually register a pair end to end.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

# Make the repo root importable when run as `python scripts/ci_smoke.py`
# (script invocation puts scripts/ on sys.path, not the project root).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from bench.fixtures import ci_spec, render_registration_pair
from samanvay.pipeline.stages import run_pipeline

BUDGET_S = 300.0  # 5 minutes
GT_RMSE_BUDGET_PX = 3.0
REQUIRED = [
    "registered.tif",
    "matches.csv",
    "transform.json",
    "metrics.json",
    "report.html",
    "provenance.json",
    "timings.json",
]


def _project(H, pts):
    q = H @ pts
    return q[:2] / q[2]


def main() -> int:
    spec = ci_spec()
    work = tempfile.mkdtemp(prefix="samanvay_ci_")
    fx = os.path.join(work, spec.name)
    gt = render_registration_pair(fx, spec)
    H_gt = np.array(gt["H_gt_source_to_ref"])

    out = os.path.join(fx, "run")
    t0 = time.perf_counter()
    timings = run_pipeline(
        gt["source"], gt["reference"], out,
        {"seed": spec.seed, "grid_n": 4, "halo_px": 64, "cache": {"enabled": False}},
    )
    elapsed = time.perf_counter() - t0

    # 1. artifacts
    missing = [f for f in REQUIRED if not os.path.exists(os.path.join(out, f))]
    if missing:
        print(f"FAIL: missing artifacts: {missing}")
        return 1

    # 2. accuracy vs known transform
    metrics = json.load(open(os.path.join(out, "metrics.json")))
    H_est = np.array(json.load(open(os.path.join(out, "transform.json")))["params"])
    ys, xs = np.mgrid[0 : spec.size : 32, 0 : spec.size : 32]
    pts = np.stack([xs.ravel(), ys.ravel(), np.ones(xs.size)], axis=1).T
    gt_rmse = float(np.sqrt(np.mean(np.sum((_project(H_est, pts) - _project(H_gt, pts)) ** 2, axis=0))))

    print(f"artifacts:   all {len(REQUIRED)} present")
    print(f"inliers:     {metrics.get('inlier_count')} (ratio {metrics.get('inlier_ratio', 0):.2f})")
    print(f"pipeline RMSE: {metrics.get('rmse_px', float('nan')):.3f} px")
    print(f"GT RMSE:     {gt_rmse:.3f} px (budget {GT_RMSE_BUDGET_PX})")
    print(f"elapsed:     {elapsed:.2f} s (budget {BUDGET_S})")
    print(f"stage times: {timings}")

    if metrics.get("inlier_count", 0) < 4:
        print("FAIL: pipeline did not find a valid model (inlier_count < 4)")
        return 1
    if not np.isfinite(gt_rmse) or gt_rmse > GT_RMSE_BUDGET_PX:
        print(f"FAIL: GT RMSE {gt_rmse:.3f} exceeds budget {GT_RMSE_BUDGET_PX}")
        return 1
    if elapsed > BUDGET_S:
        print(f"FAIL: run took {elapsed:.1f}s, over the {BUDGET_S}s budget")
        return 1

    print("\nCI SMOKE PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
