import numpy as np
import cv2
from samanvay.types import CanonicalImage, MatchSet

def match_images(source: CanonicalImage, reference: CanonicalImage, config: dict = None) -> MatchSet:
    if config is None:
        config = {}
    
    method_name = config.get("method", "sift").lower()
    ratio_thresh = config.get("ratio_threshold", 0.75)
    
    # Convert image arrays to uint8 in [0, 255] for OpenCV detector
    # Ensure they are valid 2D numpy arrays
    src_img = (source.albedo * 255).astype(np.uint8)
    ref_img = (reference.albedo * 255).astype(np.uint8)
    
    if method_name == "sift":
        detector = cv2.SIFT_create()
        matcher = cv2.BFMatcher(cv2.NORM_L2)
    elif method_name == "orb":
        detector = cv2.ORB_create(nfeatures=2000)
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    else:
        raise ValueError(f"Unknown matching method: {method_name}")
        
    kp1, desc1 = detector.detectAndCompute(src_img, None)
    kp2, desc2 = detector.detectAndCompute(ref_img, None)
    
    if desc1 is None or desc2 is None or len(kp1) == 0 or len(kp2) == 0:
        return MatchSet(
            src_xy=np.zeros((0, 2)),
            ref_xy=np.zeros((0, 2)),
            score=np.zeros((0,)),
            method=np.zeros((0,), dtype=np.uint8),
            cell=np.zeros((0,), dtype=np.int32)
        )
        
    raw_matches = matcher.knnMatch(desc1, desc2, k=2)
    
    src_pts = []
    ref_pts = []
    scores = []
    
    for m in raw_matches:
        if len(m) == 2:
            m1, m2 = m
            if m1.distance < ratio_thresh * m2.distance:
                src_pts.append(kp1[m1.queryIdx].pt)
                ref_pts.append(kp2[m1.trainIdx].pt)
                # Normalised match quality metric
                scores.append(1.0 - (m1.distance / (m2.distance + 1e-6)))
        elif len(m) == 1:
            m1 = m[0]
            src_pts.append(kp1[m1.queryIdx].pt)
            ref_pts.append(kp2[m1.trainIdx].pt)
            scores.append(1.0)
            
    src_xy = np.array(src_pts, dtype=np.float64).reshape(-1, 2)
    ref_xy = np.array(ref_pts, dtype=np.float64).reshape(-1, 2)
    score_arr = np.array(scores, dtype=np.float32)
    
    method_id = 0 if method_name == "sift" else 1
    method_arr = np.full(len(src_xy), method_id, dtype=np.uint8)
    
    # Calculate cell assignments based on source points in a 4x4 grid
    cell_arr = np.zeros(len(src_xy), dtype=np.int32)
    if len(src_xy) > 0:
        h, w = src_img.shape[:2]
        cell_x = np.clip((src_xy[:, 0] / (w / 4.0)).astype(np.int32), 0, 3)
        cell_y = np.clip((src_xy[:, 1] / (h / 4.0)).astype(np.int32), 0, 3)
        cell_arr = cell_x + 4 * cell_y
        
    return MatchSet(
        src_xy=src_xy,
        ref_xy=ref_xy,
        score=score_arr,
        method=method_arr,
        cell=cell_arr
    )
