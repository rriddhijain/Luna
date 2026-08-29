import numpy as np
from samanvay.types import CanonicalImage, MatchSet
from samanvay.match.classical import match_images

def match_tiled(
    source: CanonicalImage,
    reference: CanonicalImage,
    grid_n: int = 4,
    halo_px: int = 64,
    config: dict = None
) -> MatchSet:
    """
    Splits source and reference images into grid_n x grid_n cells.
    Matches feature points within each cell including a halo, and filters out duplicates outside boundaries.
    """
    if config is None:
        config = {}
        
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
                
            # Match current tile
            tile_matches = match_images(src_tile, ref_tile, config)
            
            if len(tile_matches.src_xy) > 0:
                # Project coordinates back to global space
                src_global = tile_matches.src_xy + np.array([src_x0, src_y0])
                ref_global = tile_matches.ref_xy + np.array([ref_x0, ref_y0])
                
                # Check if points fall in the core region of the tile to avoid duplicate matching
                core_y0 = row * tile_h_src
                core_y1 = (row + 1) * tile_h_src if row < grid_n - 1 else src_h
                core_x0 = col * tile_w_src
                core_x1 = (col + 1) * tile_w_src if col < grid_n - 1 else src_w
                
                in_core = (
                    (src_global[:, 0] >= core_x0) & (src_global[:, 0] < core_x1) &
                    (src_global[:, 1] >= core_y0) & (src_global[:, 1] < core_y1)
                )
                
                if np.any(in_core):
                    all_src_xy.append(src_global[in_core])
                    all_ref_xy.append(ref_global[in_core])
                    all_scores.append(tile_matches.score[in_core])
                    all_methods.append(tile_matches.method[in_core])
                    all_cells.append(np.full(np.sum(in_core), cell_id, dtype=np.int32))
                    
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
