import numpy as np
import cv2
from samanvay.io.loaders import load_product
from samanvay.photometry.normalize import canonicalise
from samanvay.match.tile import match_tiled
from samanvay.geometry.verify import verify_matches
from samanvay.io.writers import write_outputs

def run_pipeline(source_path: str, ref_path: str, out_dir: str, config: dict = None) -> None:
    if config is None:
        config = {}
        
    # 1. Load source and reference
    source = load_product(source_path)
    reference = load_product(ref_path)
    
    # 2. Canonicalisation (Physics stage)
    source_canon = canonicalise(source, config.get("photometry"))
    ref_canon = canonicalise(reference, config.get("photometry"))
    
    # 3. Matching
    # Default to tiled matching
    matches = match_tiled(
        source_canon,
        ref_canon,
        grid_n=config.get("grid_n", 4),
        halo_px=config.get("halo_px", 64),
        config=config.get("match")
    )
    
    # 4. Geometry verification
    registration = verify_matches(matches, config.get("geometry"))
    
    # 5. Warp source image into reference frame
    ref_h, ref_w = reference.array.shape[:2]
    # registration.params maps source -> reference
    # cv2.warpPerspective expects the matrix mapping source -> destination
    registered_array = cv2.warpPerspective(
        source.array,
        registration.params,
        (ref_w, ref_h),
        flags=cv2.INTER_LINEAR
    )
    
    # 6. Write outputs
    write_outputs(out_dir, source, reference, registration, matches, registered_array, config)
