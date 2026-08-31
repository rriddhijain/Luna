import numpy as np

from samanvay.types import CanonicalImage, Product


def canonicalise(product: Product, params: dict | None = None) -> CanonicalImage:
    if params is None:
        params = {}

    # In Walking Skeleton mode, this is a grayscale passthrough.
    # L0/L1 baseline uses raw or slightly normalised grayscale.
    albedo = product.array.copy().astype(np.float32)
    # Norm to [0, 1] if max > 1
    max_val = albedo.max()
    if max_val > 1.0:
        albedo /= max_val

    # Phase congruency map (0 to 1) - placeholder
    pc = np.zeros_like(albedo)
    # Orientation map - placeholder
    pc_orient = np.zeros_like(albedo)
    # Mask: 0 = valid
    mask = np.zeros_like(albedo, dtype=np.uint8)

    return CanonicalImage(
        albedo=albedo,
        pc=pc,
        pc_orient=pc_orient,
        mask=mask,
        params=params
    )
