import numpy as np
from samanvay.types import MatchSet

def generate_fake_matches(
    num_points: int = 100,
    noise_std: float = 0.5,
    outlier_fraction: float = 0.2,
    seed: int = 42
) -> tuple[MatchSet, np.ndarray]:
    """
    Generates a MatchSet using a known homography.
    Returns (MatchSet, ground_truth_H)
    """
    np.random.seed(seed)
    
    # Define a known homography (scale, rotation, translation)
    theta = np.radians(15.0) # 15 degree rotation
    c, s = np.cos(theta), np.sin(theta)
    scale = 1.2
    
    # 3x3 homography matrix
    H = np.array([
        [scale * c, -scale * s, 100.0],
        [scale * s,  scale * c, -50.0],
        [0.0,        0.0,        1.0]
    ])
    
    # Generate source points
    src_xy = np.random.uniform(50, 950, size=(num_points, 2))
    src_h = np.hstack([src_xy, np.ones((num_points, 1))])
    
    # Project to reference
    ref_proj = (H @ src_h.T).T
    ref_xy = ref_proj[:, :2] / ref_proj[:, 2:3]
    
    # Add gaussian noise
    ref_xy += np.random.normal(0, noise_std, size=ref_xy.shape)
    
    # Inject outliers
    num_outliers = int(num_points * outlier_fraction)
    if num_outliers > 0:
        ref_xy[:num_outliers] = np.random.uniform(50, 950, size=(num_outliers, 2))
        
    score = np.random.uniform(0.5, 0.99, size=(num_points,)).astype(np.float32)
    method = np.zeros(num_points, dtype=np.uint8)
    cell = (src_xy[:, 0] // 256).astype(np.int32) + 4 * (src_xy[:, 1] // 256).astype(np.int32)
    
    match_set = MatchSet(
        src_xy=src_xy,
        ref_xy=ref_xy,
        score=score,
        method=method,
        cell=cell
    )
    
    return match_set, H

if __name__ == "__main__":
    matches, H_gt = generate_fake_matches()
    print(f"Generated {len(matches.src_xy)} matches with GT homography:")
    print(H_gt)
