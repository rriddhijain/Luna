#!/usr/bin/env python3
import os
import sys
import json
import urllib.request
import rasterio
from rasterio.windows import Window

def download_file(url: str, dest_path: str):
    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 0:
        print(f"File already exists: {dest_path}")
        return
    print(f"Downloading {url} -> {dest_path}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            f.write(chunk)

def crop_window(in_path, out_path, center_y, center_x, size=2048):
    half = size // 2
    with rasterio.open(in_path) as src:
        H, W = src.height, src.width
        y0 = max(0, min(center_y - half, H - size))
        y1 = y0 + size
        x0 = max(0, min(center_x - half, W - size))
        x1 = x0 + size
        
        print(f"Cropping {in_path} [{H}x{W}] -> {out_path} at window Y:[{y0}:{y1}], X:[{x0}:{x1}]...")
        w = Window(x0, y0, size, size)
        data = src.read(1, window=w)
        
        profile = src.profile.copy()
        profile.update({
            "height": size,
            "width": size,
            "driver": "GTiff",
            "count": 1
        })
        with rasterio.open(out_path, "w", **profile) as dst:
            dst.write(data, 1)

def main():
    os.makedirs("data/real", exist_ok=True)
    
    # 1. Download the exact overlapping LRO NAC product (M1379255373RE)
    ref_xml_url = "https://pds.lroc.im-ldi.com/data/LRO-L-LROC-2-EDR-V1.0/LROLRC_0048A/DATA/ESM4/2021176/NAC/M1379255373RE.xml"
    ref_img_url = "https://pds.lroc.im-ldi.com/data/LRO-L-LROC-2-EDR-V1.0/LROLRC_0048A/DATA/ESM4/2021176/NAC/M1379255373RE.IMG"
    
    ref_xml_path = "data/real/ref_matched.xml"
    ref_img_path = "data/real/ref_matched.img"
    src_xml_path = "data/real/ch2_ohr_ncp_20260103T1203563771_d_img_d18.xml"
    
    download_file(ref_xml_url, ref_xml_path)
    download_file(ref_img_url, ref_img_path)
    
    # 2. Coordinates of Chandrayaan-2 strip
    # Lat range: [-85.327, -84.529]
    # Lon range: [22.792, 27.939]
    src_lat_min, src_lat_max = -85.327, -84.529
    
    # Coordinates of LRO NAC strip (M1379255373RE)
    # Lat range: [-85.046, -84.590]
    ref_lat_min, ref_lat_max = -85.046, -84.590
    
    # Common overlap latitude:
    overlap_lat = (max(src_lat_min, ref_lat_min) + min(src_lat_max, ref_lat_max)) / 2.0
    print(f"\nComputed common target latitude on Lunar Surface: {overlap_lat:.4f}°")
    
    # 3. Map common latitude to pixel row in both images
    with rasterio.open(src_xml_path) as src_ds, rasterio.open(ref_img_path) as ref_ds:
        src_H, src_W = src_ds.height, src_ds.width
        ref_H, ref_W = ref_ds.height, ref_ds.width
        
        # Chandrayaan-2 is ascending / south-to-north
        src_frac = (overlap_lat - src_lat_min) / (src_lat_max - src_lat_min)
        src_center_y = int(src_frac * src_H)
        src_center_x = src_W // 2
        
        # LRO NAC is descending / north-to-south
        ref_frac = (ref_lat_max - overlap_lat) / (ref_lat_max - ref_lat_min)
        ref_center_y = int(ref_frac * ref_H)
        ref_center_x = ref_W // 2
        
        print(f"Chandrayaan-2 Target Center: Row {src_center_y}/{src_H}, Col {src_center_x}/{src_W}")
        print(f"LRO NAC Target Center: Row {ref_center_y}/{ref_H}, Col {ref_center_x}/{ref_W}")

    # 4. Crop 2048x2048 tiles
    out_src = "data/real/src_geo.tif"
    out_ref = "data/real/ref_geo.tif"
    crop_window(src_xml_path, out_src, src_center_y, src_center_x, size=2048)
    crop_window(ref_img_path, out_ref, ref_center_y, ref_center_x, size=2048)
    
    # 5. Write metadata sidecars
    src_sidecar = {
        "product_id": "ch2_ohrc_matched_geo",
        "instrument": "OHRC",
        "incidence_deg": 83.42,
        "emission_deg": 2.1,
        "sun_az_deg": 341.86,
        "sun_el_deg": 6.58,
        "gsd_m": 0.25
    }
    ref_sidecar = {
        "product_id": "lroc_nac_matched_geo",
        "instrument": "LROC",
        "incidence_deg": 86.32,
        "emission_deg": 1.5,
        "sun_az_deg": 180.0,
        "sun_el_deg": 3.68,
        "gsd_m": 0.5
    }
    
    with open(out_src + ".json", "w") as f:
        json.dump(src_sidecar, f, indent=4)
    with open(out_ref + ".json", "w") as f:
        json.dump(ref_sidecar, f, indent=4)
        
    print("\nGeoreferenced crater tiles created successfully!")

if __name__ == "__main__":
    main()
