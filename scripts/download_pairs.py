import os
import sys
import json
import argparse
import urllib.request
import subprocess

def download_file(url: str, dest_path: str):
    print(f"Downloading {url} -> {dest_path}...")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp, open(dest_path, "wb") as f:
        while True:
            chunk = resp.read(8192)
            if not chunk:
                break
            f.write(chunk)

def main():
    parser = argparse.ArgumentParser(description="Query and download LRO NAC image pairs with sun angle differences.")
    parser.add_argument("--lat", type=float, default=-89.9, help="Latitude of target location")
    parser.add_argument("--lon", type=float, default=0.0, help="Longitude of target location")
    parser.add_argument("--radius", type=float, default=0.1, help="Search radius in degrees")
    parser.add_argument("--out-dir", default="data/real", help="Directory to save downloaded files")
    args = parser.parse_args()

    os.makedirs(args.out_dir, exist_ok=True)

    # 1. Query ODE REST API for products overlapping target region
    print(f"Querying Washington University ODE REST API for LRO LROC products at ({args.lat}, {args.lon})...")
    url = (
        f"https://oderest.rsl.wustl.edu/live2/?"
        f"target=moon&ihid=LRO&iid=LROC&pt=EDRNAC4&"
        f"query=product&results=m&latitude={args.lat}&longitude={args.lon}&locr={args.radius}&output=json"
    )
    
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    
    products = data.get("ODEResults", {}).get("Products", {}).get("Product", [])
    if not isinstance(products, list):
        products = [products]
        
    print(f"Found {len(products)} matching LROC products.")
    
    # 2. Select pair with maximum incidence angle difference
    best_pair = None
    max_diff = 0.0
    
    # Group by Left/Right sensor to compare Left-to-Left or Right-to-Right
    left_nac = [p for p in products if p.get("Product_name", "").endswith("LE.IMG")]
    
    # Iterate pairs and find the one with the biggest incidence angle difference
    for i in range(len(left_nac)):
        for j in range(i + 1, len(left_nac)):
            p1 = left_nac[i]
            p2 = left_nac[j]
            
            try:
                inc1 = float(p1.get("Incidence_angle", 0.0))
                inc2 = float(p2.get("Incidence_angle", 0.0))
            except (ValueError, TypeError):
                continue
                
            diff = abs(inc1 - inc2)
            if diff > max_diff:
                max_diff = diff
                best_pair = (p1, p2)

    if not best_pair:
        print("Error: Could not find any valid pair of NAC products for comparison.")
        sys.exit(1)

    src_p, ref_p = best_pair
    print(f"\nSelected best pair with incidence angle difference: {max_diff:.2f} degrees")
    print(f"  Source: {src_p.get('Product_name')} (Incidence: {src_p.get('Incidence_angle')})")
    print(f"  Reference: {ref_p.get('Product_name')} (Incidence: {ref_p.get('Incidence_angle')})")

    # 3. Resolve file download links
    src_label_url = src_p.get("LabelURL")
    ref_label_url = ref_p.get("LabelURL")

    # Swap extension from .xml to .IMG for the image file
    src_img_url = src_label_url.rsplit(".", 1)[0] + ".IMG"
    ref_img_url = ref_label_url.rsplit(".", 1)[0] + ".IMG"

    # Save destinations
    src_img_path = os.path.join(args.out_dir, "src.img")
    src_lbl_path = os.path.join(args.out_dir, "src.xml")
    ref_img_path = os.path.join(args.out_dir, "ref.img")
    ref_lbl_path = os.path.join(args.out_dir, "ref.xml")

    # 4. Download label and image files
    download_file(src_label_url, src_lbl_path)
    download_file(src_img_url, src_img_path)
    download_file(ref_label_url, ref_lbl_path)
    download_file(ref_img_url, ref_img_path)

    # 5. Generate standardized JSON sidecars containing illumination angles
    def make_sidecar(p, dest_path):
        sidecar = {
            "product_id": p.get("pdsid"),
            "instrument": "LROC",
            "incidence_deg": float(p.get("Incidence_angle", 0.0)),
            "emission_deg": float(p.get("Emission_angle", 0.0)),
            "phase_deg": float(p.get("Phase_angle", 0.0)),
            "sun_az_deg": 180.0,  # fallback sun azimuth
            "sun_el_deg": 90.0 - float(p.get("Incidence_angle", 0.0))
        }
        with open(dest_path, "w") as f:
            json.dump(sidecar, f, indent=4)
        print(f"Created sidecar metadata -> {dest_path}")

    make_sidecar(src_p, src_img_path + ".json")
    make_sidecar(ref_p, ref_img_path + ".json")

    print("\nPair downloaded and pre-processed successfully.")
    
    # 6. Run preflight check automatically
    print("\nRunning Samanvay Preflight Check...")
    cmd = ["samanvay", "check", "--source", src_img_path, "--ref", ref_img_path]
    subprocess.run(cmd)

if __name__ == "__main__":
    main()
