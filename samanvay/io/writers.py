import os
import json
import csv
import numpy as np
import rasterio
from samanvay.types import Product, MatchSet, Registration

def write_outputs(
    out_dir: str,
    source: Product,
    reference: Product,
    registration: Registration,
    matches: MatchSet,
    registered_array: np.ndarray,
    config: dict
) -> None:
    os.makedirs(out_dir, exist_ok=True)

    # 1. Write registered.tif
    ref_meta = reference.meta
    gt = ref_meta.get("geotransform", [0.0, 1.0, 0.0, 0.0, 0.0, -1.0])
    transform = rasterio.Affine(gt[0], gt[1], gt[2], gt[3], gt[4], gt[5])
    
    out_tif = os.path.join(out_dir, "registered.tif")
    crs = ref_meta.get("crs", "EPSG:32601")
    
    # Write using rasterio
    with rasterio.open(
        out_tif,
        "w",
        driver="GTiff",
        height=registered_array.shape[0],
        width=registered_array.shape[1],
        count=1,
        dtype=str(registered_array.dtype),
        crs=crs,
        transform=transform,
    ) as dst:
        dst.write(registered_array, 1)

    # 2. Write matches.csv
    out_csv = os.path.join(out_dir, "matches.csv")
    with open(out_csv, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "id", "src_x", "src_y", "ref_x", "ref_y", "score",
            "is_inlier", "residual_px", "sigma_px", "grid_cell"
        ])
        for i in range(len(matches.src_xy)):
            inlier = int(registration.inliers[i]) if i < len(registration.inliers) else 1
            res = np.linalg.norm(registration.residuals[i]) if i < len(registration.residuals) else 0.0
            sig = registration.sigma[i] if i < len(registration.sigma) else 0.1
            writer.writerow([
                i,
                matches.src_xy[i, 0], matches.src_xy[i, 1],
                matches.ref_xy[i, 0], matches.ref_xy[i, 1],
                matches.score[i],
                inlier,
                res,
                sig,
                matches.cell[i]
            ])

    # 3. Write transform.json
    out_transform = os.path.join(out_dir, "transform.json")
    with open(out_transform, "w") as f:
        json.dump({
            "model_type": registration.model_type,
            "params": registration.params.tolist(),
            "init_params": registration.init_params.tolist() if registration.init_params is not None else None
        }, f, indent=4)

    # 4. Write metrics.json
    out_metrics = os.path.join(out_dir, "metrics.json")
    with open(out_metrics, "w") as f:
        json.dump(registration.metrics, f, indent=4)

    # 5. Write provenance.json
    out_provenance = os.path.join(out_dir, "provenance.json")
    with open(out_provenance, "w") as f:
        json.dump({
            "source_product": source.path,
            "reference_product": reference.path,
            "git_sha": "skeleton-sha-12345",
            "config": config,
            "package_versions": {
                "numpy": np.__version__,
                "rasterio": rasterio.__version__
            },
            "seed": config.get("seed", 42)
        }, f, indent=4)

    # 6. Write report.html (minimal template)
    out_html = os.path.join(out_dir, "report.html")
    with open(out_html, "w") as f:
        f.write(f"""<!DOCTYPE html>
<html>
<head>
    <title>Samanvay Registration Report - {registration.metrics.get('rmse_px', 0.0):.3f} RMSE</title>
    <style>
        body {{ font-family: sans-serif; margin: 20px; background: #1e1e1e; color: #fff; }}
        h1 {{ color: #00bcd4; }}
        .metric-card {{ background: #2d2d2d; padding: 15px; border-radius: 8px; display: inline-block; margin-right: 15px; }}
    </style>
</head>
<body>
    <h1>SAMANVAY Registration Report</h1>
    <p>Source: {source.path}</p>
    <p>Reference: {reference.path}</p>
    <div class="metric-card">
        <h3>RMSE</h3>
        <p>{registration.metrics.get('rmse_px', 0.0):.3f} px</p>
    </div>
    <div class="metric-card">
        <h3>Inliers</h3>
        <p>{registration.metrics.get('inlier_count', 0)} / {len(matches.src_xy)}</p>
    </div>
    <div class="metric-card">
        <h3>Coverage</h3>
        <p>{registration.metrics.get('coverage_pct', 0.0):.1f}%</p>
    </div>
</body>
</html>
""")
