# Conventions:
# - Coordinates are (x, y) = (column, row), floating point, pixel-centre at integer coordinates.
# - Transforms map source → reference.
# - All residuals are expressed in source pixels.

from geometric_types import CanonicalImage, MatchSet, Product, Registration

__all__ = ["CanonicalImage", "MatchSet", "Product", "Registration"]

