"""
geometry/metrics.py

OWNER: seat 6 (Geometry & Validation). This is the "numbers seat" module --
nothing should appear on a slide or in a report unless it was produced
here. Assembles the final metrics.json content from a Registration +
UniformityReport, and builds ablation tables comparing pipeline
configurations (canonicaliser on/off, sub-pixel on/off, uniformity
enforcement on/off).
"""

from __future__ import annotations

from typing import Any

import numpy as np

from geometric_types import Registration
from geometry.uniformity import UniformityReport


def build_metrics(
    registration: Registration,
    uniformity: UniformityReport | None = None,
    runtime_s: float | None = None,
) -> dict:
    """Assemble the final metrics dict that gets written to metrics.json.
    Prefers the sub-pixel RMSE if refinement has been run, falls back to
    the model-fit RMSE otherwise, and always records which one was used.
    """
    m = dict(registration.metrics)  # copy, don't mutate the Registration

    if "rmse_px_subpixel" in m and np.isfinite(m["rmse_px_subpixel"]):
        m["rmse_px_final"] = m["rmse_px_subpixel"]
        m["rmse_px_source"] = "subpixel_refined"
    else:
        m["rmse_px_final"] = m.get("rmse_px", float("nan"))
        m["rmse_px_source"] = "model_fit_only"

    if uniformity is not None:
        m.update(uniformity.as_dict())

    if runtime_s is not None:
        m["runtime_s"] = runtime_s

    m["model_type"] = registration.model_type
    return m


def compare_to_ground_truth(
    registration: Registration,
    src_xy: np.ndarray,
    true_ref_xy: np.ndarray,
) -> dict:
    """When exact ground truth is available (synthetic pairs, or held-out
    manually measured tie-points on real pairs), compute the true
    registration error rather than relying on the model's own inlier
    residuals (which can look artificially good if the model overfits the
    same noisy points it was fit to).
    """
    pred = _apply_homog_batch(registration.params, src_xy)
    err = pred - true_ref_xy
    sq = np.sum(err**2, axis=1)
    return {
        "gt_rmse_px": float(np.sqrt(np.mean(sq))),
        "gt_mean_err_px": float(np.mean(np.sqrt(sq))),
        "gt_max_err_px": float(np.sqrt(sq).max()),
        "gt_n_points": len(src_xy),
    }


def _apply_homog_batch(M: np.ndarray, xy: np.ndarray) -> np.ndarray:
    n = xy.shape[0]
    homog = np.hstack([xy, np.ones((n, 1))])
    out = (M @ homog.T).T
    return out[:, :2] / out[:, 2:3]


def build_ablation_table(runs: dict[str, dict]) -> list[dict]:
    """Given a dict mapping a config label (e.g. "full",
    "no_canonicaliser", "no_subpixel", "no_uniformity") to that run's
    metrics dict, produce a flat table (list of rows) suitable for a
    report or a printed comparison. Also computes the delta vs the "full"
    run for the key headline metrics, when present.
    """
    headline_keys = ["rmse_px_final", "inlier_ratio", "coverage_pct", "runtime_s"]
    baseline = runs.get("full")

    table: list[dict[str, Any]] = []
    for label, m in runs.items():
        row: dict[str, Any] = {"config": label}
        for k in headline_keys:
            row[k] = m.get(k, float("nan"))
            if baseline is not None and label != "full" and k in baseline:
                base_val = baseline.get(k, float("nan"))
                if isinstance(base_val, (int, float)) and np.isfinite(base_val) and base_val != 0:
                    val = float(row[k])
                    row[f"{k}_delta_pct"] = 100.0 * (val - float(base_val)) / float(base_val)
        table.append(row)
    return table


def print_ablation_table(table: list[dict]) -> None:
    """Pretty-print an ablation table produced by build_ablation_table."""
    if not table:
        print("(empty ablation table)")
        return
    cols = ["config", "rmse_px_final", "inlier_ratio", "coverage_pct", "runtime_s"]
    widths = {c: max(len(c), 14) for c in cols}
    header = "  ".join(c.ljust(widths[c]) for c in cols)
    print(header)
    print("-" * len(header))
    for row in table:
        cells = []
        for c in cols:
            v = row.get(c, float("nan"))
            if isinstance(v, float):
                cells.append(f"{v:.4f}".ljust(widths[c]))
            else:
                cells.append(str(v).ljust(widths[c]))
        print("  ".join(cells))


if __name__ == "__main__":
    from bench.fake_matches import apply_homography, generate_fake_matches
    from geometry.uniformity import compute_uniformity
    from geometry.verify import verify

    matches, H_true = generate_fake_matches(n_points=250, noise_std=0.4, outlier_frac=0.2, seed=7)
    reg = verify(matches)
    assert reg.inliers is not None
    uni = compute_uniformity(matches.src_xy, reg.inliers, image_shape=(1024, 1024), grid_n=8)

    metrics = build_metrics(reg, uni)
    print("Assembled metrics.json content:")
    for k, v in metrics.items():
        print(f"  {k}: {v}")

    true_ref_xy = apply_homography(H_true, matches.src_xy)
    gt_check = compare_to_ground_truth(reg, matches.src_xy, true_ref_xy)
    print("\nGround-truth check:")
    for k, v in gt_check.items():
        print(f"  {k}: {v}")

    # fake ablation demo
    print("\nAblation table demo:")
    runs = {
        "full": metrics,
        "no_uniformity": {**metrics, "coverage_pct": 41.2, "rmse_px_final": metrics["rmse_px_final"] * 1.05},
        "no_subpixel": {**metrics, "rmse_px_final": metrics["rmse_px_final"] * 1.8},
    }
    table = build_ablation_table(runs)
    print_ablation_table(table)
