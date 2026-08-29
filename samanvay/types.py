"""
samanvay/types.py

Conventions:
  - Coordinates are (x, y) = (column, row), floating point.
    Pixel (0, 0) has its centre at coordinate (0.0, 0.0).
  - All transforms map SOURCE -> REFERENCE (source pixel in, reference pixel out).
  - All residuals are expressed in SOURCE pixels.
  - Transform matrices are 3x3 homogeneous matrices: [x_ref, y_ref, 1]^T ~ M @ [x_src, y_src, 1]^T
"""

from __future__ import annotations

from geometric_types import CanonicalImage, MatchSet, Product, Registration

__all__ = ["CanonicalImage", "MatchSet", "Product", "Registration"]
