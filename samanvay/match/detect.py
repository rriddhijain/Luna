import numpy as np
import cv2
from scipy.ndimage import maximum_filter

def detect_keypoints(img: np.ndarray, method: str = "sift", pc_map: np.ndarray = None) -> list[cv2.KeyPoint]:
    """
    Detects keypoints using classical (SIFT/ORB) or phase congruency (L2) feature maps.
    If pc_map is not provided or all zeros, L2 falls back to gradient-magnitude/structure-tensor corners.
    """
    method = method.lower()
    
    if method in ("sift", "orb"):
        # L0 baseline
        if method == "sift":
            detector = cv2.SIFT_create()
        else:
            detector = cv2.ORB_create(nfeatures=2000)
        kps = detector.detect(img, None)
        return kps
        
    elif method == "l2":
        # Feature detection on phase congruency map (or local structure fallback)
        if pc_map is None or np.all(pc_map == 0):
            # Fallback: Compute gradient magnitude of input image as a proxy for feature strength
            img_f = img.astype(np.float32)
            dx = cv2.Sobel(img_f, cv2.CV_32F, 1, 0, ksize=3)
            dy = cv2.Sobel(img_f, cv2.CV_32F, 0, 1, ksize=3)
            feature_map = np.sqrt(dx**2 + dy**2)
        else:
            feature_map = pc_map.astype(np.float32)
            
        # Detect local maxima of the feature_map
        # Using a maximum filter to find peak coordinates
        size = 9
        local_max = maximum_filter(feature_map, size=size) == feature_map
        # Remove flat regions
        local_max = local_max & (feature_map > (feature_map.max() * 0.1))
        
        y_indices, x_indices = np.nonzero(local_max)
        
        kps = []
        for y, x in zip(y_indices, x_indices):
            # Create a cv2.KeyPoint (x, y, size, response)
            kp = cv2.KeyPoint(
                x=float(x),
                y=float(y),
                size=float(size),
                response=float(feature_map[y, x])
            )
            kps.append(kp)
            
        # Sort by response and limit to top 2000
        kps = sorted(kps, key=lambda k: k.response, reverse=True)[:2000]
        return kps
        
    else:
        raise ValueError(f"Unknown detection method: {method}")
