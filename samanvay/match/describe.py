"""Seat 1 (matching) · pillar P3 — descriptors. `sift`/`orb` are OpenCV; `l2`/`rift` is RIFT.

RIFT (Li et al., arXiv:1804.09493) is the published answer to exactly the failure this
arm had: ~800 matches and almost no inliers, because the old descriptor histogrammed
raw phase-congruency ORIENTATION ANGLES. Averaging and binning angles over a patch
destroys the structure it is meant to encode, and in flat terrain the bins fill with
whatever the argmax happened to pick out of noise.

RIFT histograms the Maximum Index Map instead: per pixel, the *index* of the log-Gabor
orientation carrying maximum energy. An index is a discrete label, so a non-linear
intensity change (which is what a 50 deg sun-azimuth swing does to a lunar scene) has
to move a pixel's energy from one orientation channel to another before the descriptor
notices — a far higher bar than perturbing a continuous angle.

Two departures from the paper, both deliberate:

* UPRIGHT ONLY. The paper's rotation invariance comes from building norient shifted
  MIMs per keypoint and keeping the best; decision D3 records that this prototype
  assumes near-upright imagery (nadir-ish push-broom against a north-up reference), so
  that variant is not built. Ceiling: it degrades past roughly +/-20 deg of relative
  rotation. Upgrade path is the multi-MIM variant, at norient times the descriptor cost.
* MULTI-SCALE PATCHES. The paper describes one patch size, which silently assumes the
  two images share a pixel scale. Ours do not: the shipped fixture pairs an 858 px
  source against a 512 px reference, a 2x ratio, and a fixed-size upright patch cannot
  survive that. Each keypoint therefore emits one descriptor per patch size in
  `patch_sizes` (all of them the same dimensionality, so they live in one matcher
  index), and the correct-scale pairing is the one the nearest-neighbour search finds.

Every pixel is weighted by its phase-congruency value, so a crater rim outvotes the
featureless regolith between craters instead of being drowned by it.
"""

import cv2
import numpy as np

_NORIENT = 6          # must agree with photometry.normalize's norient (its default is 6)
_NGRID = 6            # JxJ pooling grid, as in the paper; measured better here than 4 or 8
# sqrt(2)-spaced patch sides. Five of them measured best across the whole dsun sweep;
# three (24, 48, 96) costs ~10% less runtime for ~30% fewer inliers.
_PATCH_SIZES = (24, 34, 48, 68, 96)
# A patch whose MEAN phase congruency is below this carries no structure to describe.
# Emitting a unit-norm descriptor for it would be a confident answer about nothing.
_MIN_MEAN_PC = 1e-3
_CLIP = 0.2           # SIFT's non-linear-illumination clip, applied to the L2-normed vector

_CELL_CACHE = {}


def mim_from_orientation(pc_orient, norient: int = _NORIENT) -> np.ndarray:
    """Maximum Index Map from a dominant-orientation map.

    `photometry.phasecong` stores `orientation = o * pi / norient` for the winning
    channel o, so the index is recoverable exactly; tests/test_describe.py asserts this
    against phasecong's own `mim` output rather than taking it on faith. Deriving it
    here is what lets the frozen CanonicalImage (which carries pc_orient but not mim)
    feed RIFT without growing a field.
    """
    o = np.asarray(pc_orient, dtype=np.float64)
    o = np.nan_to_num(o, nan=0.0, posinf=0.0, neginf=0.0)
    norient = max(int(norient), 1)
    return (np.rint(o / (np.pi / norient)).astype(np.int64) % norient).astype(np.uint8)


def _cell_offsets(patch: int, ngrid: int, norient: int) -> np.ndarray:
    """(patch, patch) map from pixel to the base index of its subregion's histogram."""
    key = (patch, ngrid, norient)
    out = _CELL_CACHE.get(key)
    if out is None:
        idx = (np.arange(patch) * ngrid) // patch      # gapless, near-equal subregions
        out = ((idx[:, None] * ngrid + idx[None, :]) * norient).astype(np.int64)
        _CELL_CACHE[key] = out
    return out


def _normalise(desc: np.ndarray) -> np.ndarray:
    """L2 -> clip -> L2 again. The clip is what buys tolerance to illumination spikes."""
    n = np.linalg.norm(desc)
    if n <= 0:
        return desc
    desc = desc / n
    np.minimum(desc, _CLIP, out=desc)
    n = np.linalg.norm(desc)
    return desc / n if n > 0 else desc


def rift_descriptor(mim, weight, x, y, patch, ngrid=_NGRID, norient=_NORIENT):
    """One RIFT descriptor at (x, y), or None if the patch is off-image or featureless."""
    h, w = mim.shape[:2]
    half = patch // 2
    x0, y0 = int(round(x)) - half, int(round(y)) - half
    if patch < ngrid or x0 < 0 or y0 < 0 or x0 + patch > w or y0 + patch > h:
        return None                                    # border keypoint: skipped, not clamped
    wp = weight[y0:y0 + patch, x0:x0 + patch]
    if float(wp.sum()) < _MIN_MEAN_PC * patch * patch:
        return None                                    # textureless: no confident wrong answer
    codes = _cell_offsets(patch, ngrid, norient) + mim[y0:y0 + patch, x0:x0 + patch]
    hist = np.bincount(codes.ravel(), weights=wp.ravel().astype(np.float64),
                       minlength=ngrid * ngrid * norient)
    return _normalise(hist.astype(np.float32))


def _mim_and_weight(img, pc_orient, pc, norient):
    """Resolve the (MIM index, per-pixel weight) pair this patch descriptor runs on."""
    have_orient = pc_orient is not None and np.any(pc_orient)
    have_pc = pc is not None and np.any(pc)
    if have_orient and have_pc:
        return mim_from_orientation(pc_orient, norient), np.asarray(pc, dtype=np.float32)

    # ponytail: the caller holds a phase-congruency map already but the current
    # match/tile.py signature only forwards pc_orient, so we recompute PC on the tile to
    # get the weights. Ceiling: ~30 ms per tile per image, and a tile-local noise
    # estimate rather than the strip-wide one. Upgrade path: forward pc= from tile.py's
    # _describe and this branch never runs.
    from samanvay.photometry.phasecong import phase_congruency
    out = phase_congruency(np.asarray(img, dtype=np.float32), norient=norient)
    weight = np.asarray(out["pc"], dtype=np.float32)
    if have_orient:
        return mim_from_orientation(pc_orient, norient), weight
    if np.any(weight):
        return np.asarray(out["mim"], dtype=np.uint8), weight

    # No phase congruency anywhere: a synthetic or perfectly flat tile. Fall back to
    # gradient orientation binned the same way, and say so — this is NOT illumination
    # invariant, it is only enough to keep the arm from returning nothing.
    f = np.asarray(img, dtype=np.float32)
    dx = cv2.Sobel(f, cv2.CV_32F, 1, 0, ksize=3)
    dy = cv2.Sobel(f, cv2.CV_32F, 0, 1, ksize=3)
    ang = np.mod(np.arctan2(dy, dx), np.pi)
    mim = np.clip((ang / (np.pi / norient)).astype(np.int64), 0, norient - 1).astype(np.uint8)
    return mim, np.hypot(dx, dy).astype(np.float32)


def describe_keypoints(
    img: np.ndarray,
    kps: list,
    method: str = "sift",
    pc_orient: np.ndarray = None,
    pc: np.ndarray = None,
    norient: int = _NORIENT,
    ngrid: int = _NGRID,
    patch_sizes=_PATCH_SIZES,
) -> tuple:
    """Descriptors for `kps`; returns (kps, descs) with the two kept parallel.

    `l2` and `rift` are the same RIFT path — `l2` is the name the config and the
    ablation table already use, `rift` is what it actually is. Keypoints whose patch
    falls off the image or lands on featureless terrain are DROPPED, so the returned
    keypoint list is a subset of the input; multi-scale keypoints are repeated once per
    patch size that fits, so it can also be longer. Either way it matches `descs`.
    """
    method = str(method).lower()

    if method in ("sift", "orb"):
        if len(kps) == 0:
            return kps, np.zeros((0, 32 if method == "orb" else 128),
                                 dtype=np.uint8 if method == "orb" else np.float32)
        extractor = cv2.SIFT_create() if method == "sift" else cv2.ORB_create()
        kps_out, descs = extractor.compute(img, kps)
        if descs is None or len(descs) == 0:
            return [], np.zeros((0, 32 if method == "orb" else 128),
                                dtype=np.uint8 if method == "orb" else np.float32)
        return kps_out, descs.astype(np.uint8 if method == "orb" else np.float32)

    if method not in ("l2", "rift"):
        raise ValueError(f"Unknown description method: {method}")

    norient = max(int(norient), 1)
    ngrid = max(int(ngrid), 1)
    dim = ngrid * ngrid * norient
    sizes = sorted({int(p) for p in np.atleast_1d(patch_sizes) if int(p) >= ngrid})
    img = np.asarray(img)
    if len(kps) == 0 or img.ndim != 2 or img.size == 0 or not sizes:
        return [], np.zeros((0, dim), dtype=np.float32)

    mim, weight = _mim_and_weight(img, pc_orient, pc, norient)
    if mim.shape[:2] != img.shape[:2] or weight.shape[:2] != img.shape[:2]:
        return [], np.zeros((0, dim), dtype=np.float32)
    weight = np.nan_to_num(weight, nan=0.0, posinf=0.0, neginf=0.0)

    out_kps, out_desc = [], []
    for kp in kps:
        x, y = kp.pt
        for patch in sizes:
            desc = rift_descriptor(mim, weight, x, y, patch, ngrid, norient)
            if desc is None:
                continue
            out_kps.append(cv2.KeyPoint(x=float(x), y=float(y), size=float(patch),
                                        response=float(kp.response)))
            out_desc.append(desc)

    if not out_desc:
        return [], np.zeros((0, dim), dtype=np.float32)
    return out_kps, np.asarray(out_desc, dtype=np.float32)
