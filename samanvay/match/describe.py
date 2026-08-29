import cv2
import numpy as np


def describe_keypoints(
    img: np.ndarray,
    kps: list[cv2.KeyPoint],
    method: str = "sift",
    pc_orient: np.ndarray | None = None,
) -> tuple[list[cv2.KeyPoint], np.ndarray]:
    """
    Computes descriptors for keypoints.
    For L2, constructs an orientation-histogram patch descriptor from the pc_orient map.
    """
    method = method.lower()
    
    if len(kps) == 0:
        return kps, np.zeros((0, 128), dtype=np.float32)
        
    if method in ("sift", "orb"):
        if method == "sift":
            descriptor_extractor = cv2.SIFT_create()  # type: ignore[attr-defined]
        else:
            descriptor_extractor = cv2.ORB_create()  # type: ignore[attr-defined]
            
        kps_computed, descs = descriptor_extractor.compute(img, kps)
        if descs is None:
            dtype = np.uint8 if method == "orb" else np.float32
            return [], np.zeros((0, 32 if method == "orb" else 128), dtype=dtype)
        if method == "orb":
            return kps_computed, descs.astype(np.uint8)
        return kps_computed, descs.astype(np.float32)
        
    elif method == "l2":
        if pc_orient is None or np.all(pc_orient == 0):
            # Fallback orientation computed from standard image gradients
            img_f = img.astype(np.float32)
            dx = cv2.Sobel(img_f, cv2.CV_32F, 1, 0, ksize=3)
            dy = cv2.Sobel(img_f, cv2.CV_32F, 0, 1, ksize=3)
            orientations = np.arctan2(dy, dx)
        else:
            orientations = pc_orient.copy()
            
        # Normalise to [0, 2*pi]
        orientations = np.mod(orientations + 2 * np.pi, 2 * np.pi)
        
        h, w = img.shape[:2]
        patch_size = 16
        half_p = patch_size // 2
        
        valid_kps = []
        descriptors = []
        
        for kp in kps:
            x, y = round(kp.pt[0]), round(kp.pt[1])
            # Bound check
            if x - half_p < 0 or x + half_p >= w or y - half_p < 0 or y + half_p >= h:
                continue
                
            patch_orient = orientations[y - half_p:y + half_p, x - half_p:x + half_p]
            
            # Construct a histogram-of-orientations descriptor
            # 16x16 patch split into 4x4 spatial blocks. Each block has 8 orientation bins.
            # Output feature size: 4 * 4 * 8 = 128 dimensions.
            desc_list: list[float] = []
            for sy in range(4):
                for sx in range(4):
                    block = patch_orient[sy*4 : (sy+1)*4, sx*4 : (sx+1)*4]
                    hist, _ = np.histogram(block, bins=8, range=(0, 2*np.pi))
                    desc_list.extend(hist.astype(float))
            
            desc_arr = np.array(desc_list, dtype=np.float32)
            norm = np.linalg.norm(desc_arr)
            if norm > 0:
                desc_arr /= norm
            descriptors.append(desc_arr)
            valid_kps.append(kp)
            
        if len(descriptors) == 0:
            return [], np.zeros((0, 128), dtype=np.float32)
            
        return valid_kps, np.array(descriptors, dtype=np.float32)
        
    else:
        raise ValueError(f"Unknown description method: {method}")
