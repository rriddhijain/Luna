"""
geometric_types.py

Re-exports dataclasses from samanvay.types for compatibility.
"""

from __future__ import annotations

from samanvay.types import CanonicalImage, MatchSet, Product, Registration

__all__ = ["CanonicalImage", "MatchSet", "Product", "Registration"]
