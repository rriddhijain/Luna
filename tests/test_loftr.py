import numpy as np
import pytest
from samanvay.types import CanonicalImage
from samanvay.match.learned import match_loftr

def test_loftr_matching():
    np.random.seed(42)
    img1 = np.random.uniform(0, 1, size=(256, 256)).astype(np.float32)
    img2 = np.random.uniform(0, 1, size=(256, 256)).astype(np.float32)
    
    src = CanonicalImage(
        albedo=img1,
        pc=np.zeros_like(img1),
        pc_orient=np.zeros_like(img1),
        mask=np.zeros_like(img1, dtype=np.uint8),
        params={}
    )
    
    ref = CanonicalImage(
        albedo=img2,
        pc=np.zeros_like(img2),
        pc_orient=np.zeros_like(img2),
        mask=np.zeros_like(img2, dtype=np.uint8),
        params={}
    )
    
    matches = match_loftr(src, ref, {"score_threshold": 0.0})
    assert isinstance(matches.src_xy, np.ndarray)
    assert isinstance(matches.ref_xy, np.ndarray)
    assert len(matches.src_xy) == len(matches.ref_xy)
