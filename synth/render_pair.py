import os

import numpy as np
import rasterio
from rasterio.transform import from_origin


def render_synthetic_pair(out_src: str = "data/source.tif", out_ref: str = "data/reference.tif"):
    """
    Renders a synthetic pair of images simulating a lunar crater under two sun illumination angles.
    Writes two GeoTIFFs to disk.
    """
    for out_path in (out_src, out_ref):
        parent = os.path.dirname(out_path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    transform = from_origin(0, 0, 1.0, 1.0)

    # Create simple structure (crater pattern)
    x = np.linspace(-10, 10, 1024)
    y = np.linspace(-10, 10, 1024)
    xx, yy = np.meshgrid(x, y)
    r = np.sqrt(xx**2 + yy**2)
    crater = np.sin(r) / (r + 1.0)

    # Simulated shading by gradient
    dy, dx = np.gradient(crater)
    shading1 = dx * 1.0 + dy * 1.0
    shading2 = dx * -1.0 + dy * -1.0

    src_img = (crater + shading1 * 0.1).astype(np.float32)
    ref_img = (crater + shading2 * 0.1).astype(np.float32)

    # Normalize to [0, 255]
    src_img = ((src_img - src_img.min()) / (src_img.max() - src_img.min()) * 255).astype(np.uint8)
    ref_img = ((ref_img - ref_img.min()) / (ref_img.max() - ref_img.min()) * 255).astype(np.uint8)

    # Write source GeoTIFF
    with rasterio.open(
        out_src, "w", driver="GTiff", height=1024, width=1024, count=1, dtype="uint8", crs="EPSG:32601", transform=transform
    ) as dst:
        dst.write(src_img, 1)

    # Write reference GeoTIFF
    with rasterio.open(
        out_ref, "w", driver="GTiff", height=1024, width=1024, count=1, dtype="uint8", crs="EPSG:32601", transform=transform
    ) as dst:
        dst.write(ref_img, 1)

if __name__ == "__main__":
    render_synthetic_pair()
    print("Synthetic pair rendered successfully.")
