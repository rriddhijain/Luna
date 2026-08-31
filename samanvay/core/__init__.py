"""
samanvay.core

Systems & performance layer (seat 5). Cross-cutting infrastructure that the
rest of the pipeline consumes so it "runs at all", stays reproducible, and
stays fast:

  - tiling  : windowed / halo reads over gigapixel products (seats 1 and 2)
  - cache   : content-addressed canonicalisation / phase-congruency cache
  - timing  : lightweight per-stage profiling used by the pipeline and bench

Nothing here is science; it is the plumbing that makes the science survivable
on a real OHRC strip instead of only on a 1k fixture.
"""

from __future__ import annotations

from samanvay.core.cache import CanonicalCache, cache_stats, canonicalise_cached
from samanvay.core.tiling import (
    Tile,
    TiledReader,
    apply_tiled,
    plan_tiles,
    stitch_tiles,
)
from samanvay.core.timing import StageTimer

__all__ = [
    "CanonicalCache",
    "StageTimer",
    "Tile",
    "TiledReader",
    "apply_tiled",
    "cache_stats",
    "canonicalise_cached",
    "plan_tiles",
    "stitch_tiles",
]
