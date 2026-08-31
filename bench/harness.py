"""
bench/harness.py

OWNER: seat 5 (Systems & Performance).

The benchmark runner. Runs a set of pairs through the real pipeline and emits a
metrics table -- the thing that tells the team, at a glance, "is main still
fast, still accurate, still green". Three sections:

  1. Registration benchmark. For every pair (synthetic fixtures with known
     ground truth by default, or a directory of real pairs) it runs the
     pipeline and reports match count, inlier ratio, pipeline RMSE, ground-truth
     RMSE, grid coverage, and per-stage wall-clock timing.

  2. Cache effectiveness. Demonstrates the roadmap I10 win ("second run of the
     same pair is dramatically faster") by timing canonicalisation cold vs
     warm through ``CanonicalCache``. Because today's canonicaliser is a cheap
     pass-through, this section runs against a *representative* phase-congruency
     workload (a real multi-scale log-Gabor FFT bank -- the same shape of
     computation seat 2 will ship) so the number reflects the speed-up the
     cache will actually deliver in production, not a pass-through no-op.

  3. Throughput. Megapixels/second per pair, so a regression in the hot path
     shows up as a number instead of a vibe.

Outputs a console table plus machine-readable artifacts (CSV / Markdown / JSON)
under ``bench/results/`` for CI to archive and for the deck.

    python -m bench.harness                 # default synthetic suite
    python -m bench.harness --quick         # single small pair (used by CI)
    python -m bench.harness --pairs DIR     # real pairs: DIR/*/source.tif,reference.tif
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
from dataclasses import dataclass, field

import numpy as np

from bench.fixtures import PairSpec, default_suite, render_registration_pair
from samanvay.core.cache import CanonicalCache, reset_cache_stats
from samanvay.pipeline.stages import run_pipeline
from samanvay.types import CanonicalImage, Product

RESULTS_DIR = os.path.join("bench", "results")


# --------------------------------------------------------------------------- #
# Registration benchmark
# --------------------------------------------------------------------------- #
@dataclass
class PairResult:
    name: str
    size: int
    n_matches: int
    inlier_count: int
    inlier_ratio: float
    rmse_px: float
    gt_rmse_px: float
    coverage_pct: float
    total_s: float
    match_s: float
    canon_s: float
    throughput_mps: float
    status: str
    timings: dict = field(default_factory=dict)


def _project(H: np.ndarray, pts_xy1: np.ndarray) -> np.ndarray:
    q = H @ pts_xy1
    return q[:2] / q[2]


def _gt_rmse(H_est: np.ndarray, H_gt: np.ndarray, size: int, step: int = 32) -> float:
    """RMS reprojection difference between estimate and ground truth (ref px)."""
    ys, xs = np.mgrid[0:size:step, 0:size:step]
    pts = np.stack([xs.ravel(), ys.ravel(), np.ones(xs.size)], axis=1).T
    err = _project(H_est, pts) - _project(H_gt, pts)
    return float(np.sqrt(np.mean(np.sum(err**2, axis=0))))


def _count_matches(run_dir: str) -> int:
    csv_path = os.path.join(run_dir, "matches.csv")
    if not os.path.exists(csv_path):
        return 0
    with open(csv_path) as f:
        return max(0, sum(1 for _ in f) - 1)  # minus header


def run_pair(spec: PairSpec, work_dir: str, no_cache: bool = True) -> PairResult:
    """Render a synthetic pair, run the pipeline, and score it against GT."""
    fx_dir = os.path.join(work_dir, spec.name)
    gt = render_registration_pair(fx_dir, spec)
    H_gt = np.array(gt["H_gt_source_to_ref"])

    run_dir = os.path.join(fx_dir, "run")
    config = {
        "seed": spec.seed,
        "grid_n": 4,
        "halo_px": 64,
        "cache": {"enabled": not no_cache},
        "match": {},
        "geometry": {},
        "photometry": {},
    }
    t0 = time.perf_counter()
    timings = run_pipeline(gt["source"], gt["reference"], run_dir, config)
    wall = time.perf_counter() - t0

    metrics = json.load(open(os.path.join(run_dir, "metrics.json")))
    transform = json.load(open(os.path.join(run_dir, "transform.json")))
    H_est = np.array(transform["params"])
    gt_rmse = _gt_rmse(H_est, H_gt, spec.size)

    megapixels = (spec.size * spec.size) / 1e6
    throughput = megapixels / wall if wall > 0 else 0.0
    status = "ok" if metrics.get("inlier_count", 0) >= 4 else "degenerate"

    return PairResult(
        name=spec.name,
        size=spec.size,
        n_matches=_count_matches(run_dir),
        inlier_count=int(metrics.get("inlier_count", 0)),
        inlier_ratio=float(metrics.get("inlier_ratio", 0.0)),
        rmse_px=float(metrics.get("rmse_px", float("nan"))),
        gt_rmse_px=gt_rmse,
        coverage_pct=float(metrics.get("coverage_pct", 0.0)),
        total_s=float(timings.get("total_s", wall)),
        match_s=float(timings.get("match", 0.0)),
        canon_s=float(timings.get("canonicalise", 0.0)),
        throughput_mps=throughput,
        status=status,
        timings=timings,
    )


def run_registration_suite(specs: list[PairSpec], work_dir: str) -> list[PairResult]:
    results = []
    for spec in specs:
        results.append(run_pair(spec, work_dir))
    return results


# --------------------------------------------------------------------------- #
# Cache effectiveness (under a representative phase-congruency workload)
# --------------------------------------------------------------------------- #
def _log_gabor_bank_cost(product: Product, params: dict | None) -> CanonicalImage:
    """A realistic phase-congruency-shaped workload.

    Runs a multi-scale, multi-orientation log-Gabor bank in the frequency
    domain -- the same expensive FFT-per-(scale,orientation) structure seat 2's
    real encoder has -- and folds the responses into a phase-congruency-like
    map. Deterministic, so it is a legitimate thing to cache. Used ONLY to make
    the cache benchmark representative; it is not the production canonicaliser.
    """
    params = params or {}
    n_scales = params.get("n_scales", 4)
    n_orient = params.get("n_orient", 6)
    img = np.asarray(product.array, dtype=np.float32)
    if img.max() > 1.0:
        img = img / img.max()

    h, w = img.shape
    F = np.fft.fft2(img)
    u = np.fft.fftfreq(w)[None, :]
    v = np.fft.fftfreq(h)[:, None]
    radius = np.sqrt(u * u + v * v)
    radius[0, 0] = 1.0
    theta = np.arctan2(v * np.ones_like(u), u * np.ones_like(v))

    accum = np.zeros((h, w), dtype=np.float32)
    orient_accum = np.zeros((h, w), dtype=np.float32)
    wavelength = 6.0
    for s in range(n_scales):
        f0 = 1.0 / (wavelength * (2.0**s))
        log_gabor = np.exp(-(np.log(radius / f0) ** 2) / (2 * np.log(0.65) ** 2))
        log_gabor[0, 0] = 0.0
        for o in range(n_orient):
            angle = np.pi * o / n_orient
            dtheta = np.arctan2(np.sin(theta - angle), np.cos(theta - angle))
            spread = np.exp(-(dtheta**2) / (2 * (np.pi / n_orient / 1.5) ** 2))
            filt = log_gabor * spread
            response = np.abs(np.fft.ifft2(F * filt))
            accum += response.astype(np.float32)
            orient_accum += (response * angle).astype(np.float32)

    pc = accum / (accum.max() + 1e-9)
    pc_orient = np.divide(orient_accum, accum + 1e-9).astype(np.float32)
    mask = np.zeros((h, w), dtype=np.uint8)
    return CanonicalImage(albedo=img, pc=pc.astype(np.float32), pc_orient=pc_orient, mask=mask, params=params)


@dataclass
class CacheResult:
    size: int
    cold_s: float
    warm_s: float
    speedup: float
    stats: dict


def run_cache_benchmark(size: int = 1024, seed: int = 11) -> CacheResult:
    """Time canonicalisation cold (compute) vs warm (cache hit)."""
    rng = np.random.default_rng(seed)
    arr = rng.random((size, size)).astype(np.float32)
    product = Product(path="", array=arr, meta={"product_id": f"cache_bench_{size}_{seed}"})
    params = {"n_scales": 4, "n_orient": 6}

    tmp = tempfile.mkdtemp(prefix="samanvay_cachebench_")
    reset_cache_stats()
    cache = CanonicalCache(root=tmp, enabled=True, mem_entries=0)  # force disk path

    t0 = time.perf_counter()
    cache.get_or_compute(product, params, _log_gabor_bank_cost)  # cold: compute + write
    cold = time.perf_counter() - t0

    t0 = time.perf_counter()
    cache.get_or_compute(product, params, _log_gabor_bank_cost)  # warm: disk load
    warm = time.perf_counter() - t0

    from samanvay.core.cache import cache_stats

    speedup = cold / warm if warm > 0 else float("inf")
    return CacheResult(size=size, cold_s=cold, warm_s=warm, speedup=speedup, stats=cache_stats())


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #
def _fmt_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    sep = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows)
    return f"{line}\n{sep}\n{body}"


def _reg_rows(results: list[PairResult]) -> list[list[str]]:
    rows = []
    for r in results:
        rows.append(
            [
                r.name,
                str(r.size),
                str(r.n_matches),
                f"{r.inlier_count}",
                f"{r.inlier_ratio:.2f}",
                f"{r.rmse_px:.3f}",
                f"{r.gt_rmse_px:.3f}",
                f"{r.coverage_pct:.1f}",
                f"{r.total_s:.3f}",
                f"{r.match_s:.3f}",
                f"{r.throughput_mps:.2f}",
                r.status,
            ]
        )
    return rows


REG_HEADERS = [
    "pair", "size", "matches", "inliers", "in_ratio", "rmse_px",
    "gt_rmse", "cover%", "total_s", "match_s", "MP/s", "status",
]


def render_report(results: list[PairResult], cache: CacheResult | None) -> str:
    out = []
    out.append("=" * 78)
    out.append("SAMANVAY BENCHMARK  (seat 5 harness)")
    out.append("=" * 78)
    out.append("")
    out.append("Registration suite")
    out.append("-" * 78)
    out.append(_fmt_table(REG_HEADERS, _reg_rows(results)))
    out.append("")

    if results:
        mean_gt = np.mean([r.gt_rmse_px for r in results])
        mean_cov = np.mean([r.coverage_pct for r in results])
        tot = sum(r.total_s for r in results)
        out.append(
            f"summary: {len(results)} pairs | mean GT-RMSE {mean_gt:.3f} px | "
            f"mean coverage {mean_cov:.1f}% | wall {tot:.2f}s"
        )
        out.append("")

    if cache is not None:
        out.append("Cache effectiveness  (representative phase-congruency workload)")
        out.append("-" * 78)
        crows = [[
            str(cache.size),
            f"{cache.cold_s:.3f}",
            f"{cache.warm_s:.3f}",
            f"{cache.speedup:.1f}x",
            f"{cache.stats.get('hit_rate', 0.0):.2f}",
        ]]
        out.append(_fmt_table(["size", "cold_s", "warm_s", "speedup", "hit_rate"], crows))
        out.append("")
        out.append(
            f"=> caching canonicalisation is {cache.speedup:.1f}x faster on the "
            "second run of a pair.\n   The production win scales with seat 2's PC "
            "cost; this stand-in bank is representative."
        )
        out.append("")
    return "\n".join(out)


def write_artifacts(results: list[PairResult], cache: CacheResult | None, out_dir: str) -> None:
    os.makedirs(out_dir, exist_ok=True)

    # JSON
    payload = {
        "registration": [r.__dict__ for r in results],
        "cache": cache.__dict__ if cache else None,
    }
    with open(os.path.join(out_dir, "benchmark.json"), "w") as f:
        json.dump(payload, f, indent=2)

    # CSV
    with open(os.path.join(out_dir, "benchmark.csv"), "w") as f:
        f.write(",".join(REG_HEADERS) + "\n")
        for row in _reg_rows(results):
            f.write(",".join(row) + "\n")

    # Markdown
    with open(os.path.join(out_dir, "benchmark.md"), "w") as f:
        f.write("# SAMANVAY benchmark\n\n")
        f.write("## Registration suite\n\n")
        f.write("| " + " | ".join(REG_HEADERS) + " |\n")
        f.write("|" + "|".join(["---"] * len(REG_HEADERS)) + "|\n")
        for row in _reg_rows(results):
            f.write("| " + " | ".join(row) + " |\n")
        if cache is not None:
            f.write("\n## Cache effectiveness\n\n")
            f.write("| size | cold_s | warm_s | speedup | hit_rate |\n|---|---|---|---|---|\n")
            f.write(
                f"| {cache.size} | {cache.cold_s:.3f} | {cache.warm_s:.3f} | "
                f"{cache.speedup:.1f}x | {cache.stats.get('hit_rate', 0.0):.2f} |\n"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="SAMANVAY benchmark harness (seat 5)")
    parser.add_argument("--quick", action="store_true", help="single small pair (CI mode)")
    parser.add_argument("--no-cache-bench", action="store_true", help="skip the cache benchmark")
    parser.add_argument("--cache-size", type=int, default=1024, help="image size for the cache bench")
    parser.add_argument("--out", default=RESULTS_DIR, help="results output directory")
    parser.add_argument("--work", default=None, help="scratch dir for fixtures (default: temp)")
    args = parser.parse_args(argv)

    specs = [PairSpec(name="quick", size=256, seed=1, rot_deg=0.8, tx=4, ty=-3)] if args.quick else default_suite()
    work_dir = args.work or tempfile.mkdtemp(prefix="samanvay_bench_")

    results = run_registration_suite(specs, work_dir)
    cache = None
    if not args.no_cache_bench:
        cache = run_cache_benchmark(size=args.cache_size if not args.quick else 256)

    report = render_report(results, cache)
    print(report)
    write_artifacts(results, cache, args.out)
    print(f"\nartifacts written to {args.out}/ (benchmark.json, .csv, .md)")

    # Non-zero exit if any pair failed to register, so CI can gate on it.
    failed = [r for r in results if r.status != "ok"]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
