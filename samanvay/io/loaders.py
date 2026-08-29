import os
import json
import numpy as np
try:
    import rasterio
except ImportError:
    rasterio = None
from samanvay.types import Product

def load_product(path: str) -> Product:
    # If the file does not exist, check if there's a sidecar JSON or return mock data.
    # This acts as the D0 escape hatch/stub.
    if not os.path.exists(path):
        array = np.zeros((1024, 1024), dtype=np.float32)
        meta = {
            "product_id": os.path.basename(path),
            "instrument": "MOCK",
            "gsd_m": 1.0,
            "sun_az_deg": 45.0,
            "sun_el_deg": 30.0,
            "incidence_deg": 60.0,
            "emission_deg": 0.0,
            "geotransform": [0.0, 1.0, 0.0, 0.0, 0.0, -1.0],
            "crs": "EPSG:32601",
            "shape": (1024, 1024),
            "dtype": "float32"
        }
        # Try to find a JSON sidecar anyway
        sidecar_path = path + ".json"
        if os.path.exists(sidecar_path):
            try:
                with open(sidecar_path, "r") as f:
                    meta.update(json.load(f))
            except Exception:
                pass
    else:
        with rasterio.open(path) as src:
            array = src.read(1).astype(np.float32)
            meta = {
                "product_id": os.path.basename(path),
                "instrument": "GEOTIFF",
                "gsd_m": 1.0,
                "sun_az_deg": 0.0,
                "sun_el_deg": 45.0,
                "incidence_deg": 45.0,
                "emission_deg": 0.0,
                "geotransform": list(src.transform)[:6],
                "crs": str(src.crs),
                "shape": src.shape,
                "dtype": str(src.dtypes[0])
            }
            sidecar_path = path + ".json"
            if os.path.exists(sidecar_path):
                try:
                    with open(sidecar_path, "r") as f:
                        meta.update(json.load(f))
                except Exception:
                    pass
    return Product(path=path, array=array, meta=meta)
