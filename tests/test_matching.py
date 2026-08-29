import numpy as np
import pytest
from samanvay.types import CanonicalImage
from samanvay.match.classical import match_images
from samanvay.match.tile import match_tiled

def create_synthetic_image(h=512, w=512, pattern="random"):
    # Generate simple test pattern with details so detector can find points
    np.random.seed(42)
    img = np.zeros((h, w), dtype=np.float32)
    if pattern == "checkerboard":
        for i in range(0, h, 32):
            for j in range(0, w, 32):
                if (i // 32 + j // 32) % 2 == 0:
                    img[i:i+32, j:j+32] = 1.0
    else:
        # Random noise/texture
        img = np.random.uniform(0, 1, size=(h, w)).astype(np.float32)
        
    return CanonicalImage(
        albedo=img,
        pc=np.zeros_like(img),
        pc_orient=np.zeros_like(img),
        mask=np.zeros_like(img, dtype=np.uint8),
        params={}
    )

def test_sift_matching():
    src = create_synthetic_image(pattern="checkerboard")
    ref = create_synthetic_image(pattern="checkerboard")
    
    matches = match_images(src, ref, {"method": "sift"})
    assert isinstance(matches.src_xy, np.ndarray)
    assert len(matches.src_xy) > 0

def test_orb_matching():
    src = create_synthetic_image(pattern="checkerboard")
    ref = create_synthetic_image(pattern="checkerboard")
    
    matches = match_images(src, ref, {"method": "orb"})
    assert isinstance(matches.src_xy, np.ndarray)
    assert len(matches.src_xy) > 0

def test_l2_matching_with_gradient_fallback():
    src = create_synthetic_image(pattern="checkerboard")
    ref = create_synthetic_image(pattern="checkerboard")
    
    # pc and pc_orient maps are intentionally zero to test independent fallback
    matches = match_images(src, ref, {"method": "l2"})
    assert isinstance(matches.src_xy, np.ndarray)
    assert len(matches.src_xy) > 0
    assert np.all(matches.method == 2) # L2 method id

def test_tiled_matching():
    src = create_synthetic_image(pattern="checkerboard")
    ref = create_synthetic_image(pattern="checkerboard")
    
    matches = match_tiled(src, ref, grid_n=2, halo_px=16, config={"method": "sift"})
    assert isinstance(matches.src_xy, np.ndarray)
    assert len(matches.src_xy) > 0
    assert np.all(matches.cell >= 0)
    assert np.all(matches.cell < 4)
