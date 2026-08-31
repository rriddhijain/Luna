"""
samanvay/core/timing.py

OWNER: seat 5 (Systems & Performance).

A tiny, dependency-free per-stage timer. The pipeline wraps each stage in a
``StageTimer`` so every run can report where its seconds went, and the
benchmark harness reads those numbers back. Kept deliberately minimal -- this
is a stopwatch, not an observability stack.

Usage
-----
    timer = StageTimer()
    with timer("load"):
        ...
    with timer("canonicalise"):
        ...
    timer.as_dict()  # {"load": 0.01, "canonicalise": 0.42, ...}
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["StageTimer"]


class StageTimer:
    """Accumulates wall-clock seconds per named stage (insertion-ordered)."""

    def __init__(self) -> None:
        self._times: dict[str, float] = {}

    @contextmanager
    def __call__(self, name: str) -> Iterator[None]:
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            self._times[name] = self._times.get(name, 0.0) + dt

    def record(self, name: str, seconds: float) -> None:
        self._times[name] = self._times.get(name, 0.0) + float(seconds)

    def as_dict(self) -> dict[str, float]:
        return {k: round(v, 6) for k, v in self._times.items()}

    @property
    def total(self) -> float:
        return round(sum(self._times.values()), 6)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        parts = ", ".join(f"{k}={v:.3f}s" for k, v in self._times.items())
        return f"StageTimer({parts}, total={self.total:.3f}s)"
