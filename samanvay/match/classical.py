import cv2
import numpy as np

from samanvay.match.describe import describe_keypoints
from samanvay.match.detect import detect_keypoints
from samanvay.types import CanonicalImage, MatchSet


def match_images(source: CanonicalImage, reference: CanonicalImage, config: dict | None = None) -> MatchSet:
    """
    Orchestrates the matching of source and reference images.
    Delegates keypoint detection and description tasks to separate modules.
    """
    if config is None:
        config = {}

    method_name = config.get("method", "sift").lower()

    # Pre-process image boundaries/types
    src_img = (source.albedo * 255).astype(np.uint8)
    ref_img = (reference.albedo * 255).astype(np.uint8)

    # 1. Feature Detection
    kps_src = detect_keypoints(src_img, method=method_name, pc_map=source.pc)
    kps_ref = detect_keypoints(ref_img, method=method_name, pc_map=reference.pc)

    # 2. Feature Description
    kps_src, desc_src = describe_keypoints(src_img, kps_src, method=method_name, pc_orient=source.pc_orient)
    kps_ref, desc_ref = describe_keypoints(ref_img, kps_ref, method=method_name, pc_orient=reference.pc_orient)

    if len(kps_src) == 0 or len(kps_ref) == 0 or desc_src.size == 0 or desc_ref.size == 0:
        return MatchSet(
            src_xy=np.zeros((0, 2)),
            ref_xy=np.zeros((0, 2)),
            score=np.zeros((0,)),
            method=np.zeros((0,), dtype=np.uint8),
            cell=np.zeros((0,), dtype=np.int32)
        )

    # 3. Cross-Matching
    if method_name == "orb":
        matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
    else:
        matcher = cv2.BFMatcher(cv2.NORM_L2)

    # Relax ratio threshold for L2 descriptor matching as orientation histograms are self-similar
    thresh = config.get("ratio_threshold", 0.9 if method_name == "l2" else 0.75)

    if len(desc_ref) < 2:
        # Fallback to 1-nn match if reference set is too small
        raw_matches: list[tuple[cv2.DMatch, ...]] = [tuple(m) for m in matcher.knnMatch(desc_src, desc_ref, k=1) if len(m) > 0]
    else:
        raw_matches = [tuple(m) for m in matcher.knnMatch(desc_src, desc_ref, k=2)]

    src_pts = []
    ref_pts = []
    scores = []

    for m in raw_matches:
        if len(m) == 2:
            m1, m2 = m
            if m1.distance == 0 or m1.distance < thresh * m2.distance:
                src_pts.append(kps_src[m1.queryIdx].pt)
                ref_pts.append(kps_ref[m1.trainIdx].pt)
                scores.append(1.0 - (m1.distance / (m2.distance + 1e-6)))
        elif len(m) == 1:
            m1 = m[0]
            src_pts.append(kps_src[m1.queryIdx].pt)
            ref_pts.append(kps_ref[m1.trainIdx].pt)
            scores.append(1.0)

    src_xy = np.array(src_pts, dtype=np.float64).reshape(-1, 2)
    ref_xy = np.array(ref_pts, dtype=np.float64).reshape(-1, 2)
    score_arr = np.array(scores, dtype=np.float32)

    method_map = {"sift": 0, "orb": 1, "l2": 2}
    method_id = method_map.get(method_name, 0)
    method_arr = np.full(len(src_xy), method_id, dtype=np.uint8)

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
