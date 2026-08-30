#!/usr/bin/env python3
import os
import json
import rasterio
from rasterio.windows import Window

def crop_image(in_path, out_path, y0, y1, x0, x1):
    print(f"Cropping {in_path} -> {out_path} at [{y0}:{y1}, {x0}:{x1}]...")
    with rasterio.open(in_path) as src:
        w = Window(x0, y0, x1 - x0, y1 - y0)
        data = src.read(1, window=w)
        profile = src.profile.copy()
        profile.update({
            "height": y1 - y0,
            "width": x1 - x0,
            "driver": "GTiff",
            "count": 1
        })
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)

def main():
    in_src = "data/real/ch2_ohr_ncp_20260103T1203563771_d_img_d18.xml"
    in_ref = "data/real/ref.img"
    
    out_src = "data/real/src_crop.tif"
    out_ref = "data/real/ref_crop.tif"
    
    # Crop a 2048x2048 region from the middle of the overlapping pixel space
    # Overlap space is limited by reference size: H=52224, W=2532
    y0, y1 = 25000, 27048
    x0, x1 = 200, 2248
    
    crop_image(in_src, out_src, y0, y1, x0, x1)
    crop_image(in_ref, out_ref, y0, y1, x0, x1)
    
    # Generate sidecars for cropped images
    # We will copy the parsed lighting angles from the XML label to the sidecars
    src_sidecar = {
        "product_id": "ch2_ohrc_crop",
        "instrument": "OHRC",
        "incidence_deg": 83.42,
        "emission_deg": 2.1,
        "sun_az_deg": 341.86,
        "sun_el_deg": 6.58,
        "gsd_m": 0.25
    }
    
    ref_sidecar = {
        "product_id": "lroc_nac_crop",
        "instrument": "LROC",
        "incidence_deg": 84.04,
        "emission_deg": 1.16,
        "sun_az_deg": 180.0,
        "sun_el_deg": 5.96,
        "gsd_m": 0.5
    }
    
    with open(out_src + ".json", "w") as f:
        json.dump(src_sidecar, f, indent=4)
    with open(out_ref + ".json", "w") as f:
        json.dump(ref_sidecar, f, indent=4)
        
    print("\nCrops and sidecars generated successfully.")

if __name__ == "__main__":
    main()
