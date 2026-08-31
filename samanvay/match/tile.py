import numpy as np

from samanvay.match.classical import match_images
from samanvay.types import CanonicalImage, MatchSet


def match_tiled(
    source: CanonicalImage,
    reference: CanonicalImage,
    grid_n: int = 4,
    halo_px: int = 64,
    config: dict | None = None,
    cell_budgets: dict | None = None,
) -> MatchSet:
    """
    Splits source and reference images into grid_n x grid_n cells.
    Matches feature points within each cell including a halo, and enforces uniformity cell budgets (I6):
    - Underpopulated cells: Dynamically relaxes the ratio threshold to harvest more matches.
    - Overpopulated cells: Restricts output to the top-K matches based on score.
    """
    if config is None:
        config = {}
    if cell_budgets is None:
        cell_budgets = {}

    src_h, src_w = source.albedo.shape[:2]
    ref_h, ref_w = reference.albedo.shape[:2]

    tile_h_src = src_h // grid_n
    tile_w_src = src_w // grid_n

    tile_h_ref = ref_h // grid_n
    tile_w_ref = ref_w // grid_n

    all_src_xy = []
    all_ref_xy = []
    all_scores = []
    all_methods = []
    all_cells = []

    for row in range(grid_n):
        for col in range(grid_n):
            cell_id = col + grid_n * row

            # Retrieve budget for this specific cell
            budget = cell_budgets.get(cell_id, {})
            min_matches = budget.get("min_matches", 5)
            max_matches = budget.get("max_matches", 50)

            # Source bounds with halo
            src_y0 = max(0, row * tile_h_src - halo_px)
            src_y1 = min(src_h, (row + 1) * tile_h_src + halo_px)
            src_x0 = max(0, col * tile_w_src - halo_px)
            src_x1 = min(src_w, (col + 1) * tile_w_src + halo_px)

            # Reference bounds with halo
            ref_y0 = max(0, row * tile_h_ref - halo_px)
            ref_y1 = min(ref_h, (row + 1) * tile_h_ref + halo_px)
            ref_x0 = max(0, col * tile_w_ref - halo_px)
            ref_x1 = min(ref_w, (col + 1) * tile_w_ref + halo_px)

            # Extract sub-images
            src_tile = CanonicalImage(
                albedo=source.albedo[src_y0:src_y1, src_x0:src_x1],
                pc=source.pc[src_y0:src_y1, src_x0:src_x1],
                pc_orient=source.pc_orient[src_y0:src_y1, src_x0:src_x1],
                mask=source.mask[src_y0:src_y1, src_x0:src_x1],
                params=source.params
            )

            ref_tile = CanonicalImage(
                albedo=reference.albedo[ref_y0:ref_y1, ref_x0:ref_x1],
                pc=reference.pc[ref_y0:ref_y1, ref_x0:ref_x1],
                pc_orient=reference.pc_orient[ref_y0:ref_y1, ref_x0:ref_x1],
                mask=reference.mask[ref_y0:ref_y1, ref_x0:ref_x1],
                params=reference.params
            )

            if src_tile.albedo.size == 0 or ref_tile.albedo.size == 0:
                continue

            # Matching loop with dynamic relaxation of constraints (ratio threshold)
            # if we get fewer than min_matches.
            current_config = config.copy()
            initial_ratio = config.get("ratio_threshold", 0.75)

            tile_matches = None
            src_global = np.zeros((0, 2))
            ref_global = np.zeros((0, 2))
            scores_global = np.zeros((0,))
            methods_global = np.zeros((0,), dtype=np.uint8)

            core_y0 = row * tile_h_src
            core_y1 = (row + 1) * tile_h_src if row < grid_n - 1 else src_h
            core_x0 = col * tile_w_src
            core_x1 = (col + 1) * tile_w_src if col < grid_n - 1 else src_w

            for attempt in range(4): # up to 4 iterations (e.g. 0.75 -> 0.80 -> 0.85 -> 0.90)
                ratio_val = min(0.95, initial_ratio + attempt * 0.05)
                current_config["ratio_threshold"] = ratio_val

                tile_matches = match_images(src_tile, ref_tile, current_config)

                if len(tile_matches.src_xy) > 0:
                    src_candidates = tile_matches.src_xy + np.array([src_x0, src_y0])
                    ref_candidates = tile_matches.ref_xy + np.array([ref_x0, ref_y0])

                    # Filter candidates that fall strictly in the core tile boundary
                    in_core = (
                        (src_candidates[:, 0] >= core_x0) & (src_candidates[:, 0] < core_x1) &
                        (src_candidates[:, 1] >= core_y0) & (src_candidates[:, 1] < core_y1)
                    )

                    if np.any(in_core):
                        src_global = src_candidates[in_core]
                        ref_global = ref_candidates[in_core]
                        scores_global = tile_matches.score[in_core]
                        methods_global = tile_matches.method[in_core]

                # Break early if we satisfied the minimum match budget
                if len(src_global) >= min_matches:
                    break

            # Enforce max_matches by taking top-K sorted by score
            if len(src_global) > max_matches:
                sort_idx = np.argsort(scores_global)[::-1] # descending sort
                top_idx = sort_idx[:max_matches]

                src_global = src_global[top_idx]
                ref_global = ref_global[top_idx]
                scores_global = scores_global[top_idx]
                methods_global = methods_global[top_idx]

            if len(src_global) > 0:
                all_src_xy.append(src_global)
                all_ref_xy.append(ref_global)
                all_scores.append(scores_global)
                all_methods.append(methods_global)
                all_cells.append(np.full(len(src_global), cell_id, dtype=np.int32))

    if len(all_src_xy) > 0:
        src_xy = np.vstack(all_src_xy)
        ref_xy = np.vstack(all_ref_xy)
        score = np.concatenate(all_scores)
        method = np.concatenate(all_methods)
        cell = np.concatenate(all_cells)
    else:
        src_xy = np.zeros((0, 2))
        ref_xy = np.zeros((0, 2))
        score = np.zeros((0,), dtype=np.float32)
        method = np.zeros((0,), dtype=np.uint8)
        cell = np.zeros((0,), dtype=np.int32)

    return MatchSet(
        src_xy=src_xy,
        ref_xy=ref_xy,
        score=score,
        method=method,
        cell=cell
    )
