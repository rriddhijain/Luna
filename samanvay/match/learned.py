import numpy as np
import torch
import kornia.feature as KF
from samanvay.types import CanonicalImage, MatchSet

def match_loftr(source: CanonicalImage, reference: CanonicalImage, config: dict = None) -> MatchSet:
    """
    Performs learned matching using Kornia's pre-trained LoFTR model.
    Auto-detects CPU or GPU hardware acceleration.
    """
    if config is None:
        config = {}
        
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    
    # Load LoFTR model
    loftr = KF.LoFTR(pretrained="outdoor").to(device).eval()
    
    # Convert numpy arrays to float32 tensors with dimensions (1, 1, H, W)
    src_t = torch.from_numpy(source.albedo).float().unsqueeze(0).unsqueeze(0).to(device)
    ref_t = torch.from_numpy(reference.albedo).float().unsqueeze(0).unsqueeze(0).to(device)
    
    # Inference without tracking gradients
    with torch.no_grad():
        input_dict = {"image0": src_t, "image1": ref_t}
        correspondences = loftr(input_dict)
        
    # Parse keypoints and confidences
    kp0 = correspondences["keypoints0"].cpu().numpy()
    kp1 = correspondences["keypoints1"].cpu().numpy()
    scores = correspondences["confidence"].cpu().numpy()
    
    if len(kp0) == 0:
        return MatchSet(
            src_xy=np.zeros((0, 2)),
            ref_xy=np.zeros((0, 2)),
            score=np.zeros((0,)),
            method=np.zeros((0,), dtype=np.uint8),
            cell=np.zeros((0,), dtype=np.int32)
        )
        
    score_thresh = config.get("score_threshold", 0.2)
    valid_idx = scores >= score_thresh
    
    src_xy = kp0[valid_idx].astype(np.float64)
    ref_xy = kp1[valid_idx].astype(np.float64)
    score_arr = scores[valid_idx].astype(np.float32)
    
    # Method ID 3 for LoFTR
    method_arr = np.full(len(src_xy), 3, dtype=np.uint8)
    
    # Calculate cell assignments based on source points in a 4x4 grid
    cell_arr = np.zeros(len(src_xy), dtype=np.int32)
    if len(src_xy) > 0:
        h, w = source.albedo.shape[:2]
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
