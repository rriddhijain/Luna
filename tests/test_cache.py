"""
tests/test_cache.py

OWNER: seat 5 (Systems & Performance).

The cache must (a) never serve a stale result when identity or params change,
(b) survive a fresh process via the disk tier, and (c) be a transparent no-op
when disabled -- correctness must never depend on it.
"""

from __future__ import annotations

import numpy as np

from samanvay.core import cache as cachemod
from samanvay.core.cache import CanonicalCache, fingerprint
from samanvay.types import CanonicalImage, Product


def _make_product(seed=0, pid="unit"):
    arr = np.random.default_rng(seed).random((64, 64)).astype(np.float32)
    return Product(path="", array=arr, meta={"product_id": pid, "sun_el_deg": 30})


def _counter_compute():
    calls = {"n": 0}

    def compute(product, params):
        calls["n"] += 1
        a = np.asarray(product.array, dtype=np.float32)
        return CanonicalImage(
            albedo=a.copy(),
            pc=np.zeros_like(a),
            pc_orient=np.zeros_like(a),
            mask=np.zeros_like(a, np.uint8),
            params=params or {},
        )

    return compute, calls


def test_memory_and_disk_hits(tmp_path):
    cachemod.reset_cache_stats()
    compute, calls = _counter_compute()
    prod = _make_product()

    c1 = CanonicalCache(root=str(tmp_path), enabled=True)
    r1 = c1.get_or_compute(prod, {"a": 1}, compute)  # miss -> compute + write
    r2 = c1.get_or_compute(prod, {"a": 1}, compute)  # memory hit
    assert calls["n"] == 1
    assert np.array_equal(r1.albedo, r2.albedo)

    # Fresh cache object (cold memory) must hit the disk tier.
    c2 = CanonicalCache(root=str(tmp_path), enabled=True)
    r3 = c2.get_or_compute(prod, {"a": 1}, compute)
    assert calls["n"] == 1  # still no recompute
    assert np.array_equal(r1.albedo, r3.albedo)

    stats = cachemod.cache_stats()
    assert stats["hits"] == 2 and stats["misses"] == 1


def test_params_change_invalidates(tmp_path):
    compute, calls = _counter_compute()
    prod = _make_product()
    c = CanonicalCache(root=str(tmp_path), enabled=True)
    c.get_or_compute(prod, {"a": 1}, compute)
    c.get_or_compute(prod, {"a": 2}, compute)  # different params -> recompute
    assert calls["n"] == 2


def test_input_change_invalidates(tmp_path):
    compute, calls = _counter_compute()
    c = CanonicalCache(root=str(tmp_path), enabled=True)
    c.get_or_compute(_make_product(seed=0), {"a": 1}, compute)
    c.get_or_compute(_make_product(seed=1), {"a": 1}, compute)  # different pixels
    assert calls["n"] == 2


def test_disabled_cache_is_transparent(tmp_path):
    compute, calls = _counter_compute()
    prod = _make_product()
    c = CanonicalCache(root=str(tmp_path), enabled=False)
    c.get_or_compute(prod, {"a": 1}, compute)
    c.get_or_compute(prod, {"a": 1}, compute)
    assert calls["n"] == 2  # always recomputes, never writes


def test_fingerprint_is_stable_and_order_independent():
    prod = _make_product()
    k1 = fingerprint(prod, {"a": 1, "b": 2})
    k2 = fingerprint(prod, {"b": 2, "a": 1})
    assert k1 == k2
    assert k1 != fingerprint(prod, {"a": 1, "b": 3})


def test_file_backed_identity_uses_stat(tmp_path):
    # Two products with identical arrays but different files/ids must not collide.
    p1 = tmp_path / "a.bin"
    p2 = tmp_path / "b.bin"
    p1.write_bytes(b"x")
    p2.write_bytes(b"x")
    arr = np.zeros((8, 8), np.float32)
    prod1 = Product(path=str(p1), array=arr, meta={"product_id": "a"})
    prod2 = Product(path=str(p2), array=arr, meta={"product_id": "b"})
    assert fingerprint(prod1, {}) != fingerprint(prod2, {})


def test_corrupt_entry_is_treated_as_miss(tmp_path):
    compute, calls = _counter_compute()
    prod = _make_product()
    c = CanonicalCache(root=str(tmp_path), enabled=True)
    key = fingerprint(prod, {"a": 1})
    c.get_or_compute(prod, {"a": 1}, compute)
    # Corrupt the stored npz.
    with open(c._npz_path(key), "wb") as f:
        f.write(b"not a real npz")
    c2 = CanonicalCache(root=str(tmp_path), enabled=True)
    c2.get_or_compute(prod, {"a": 1}, compute)  # must recompute, not crash
    assert calls["n"] == 2
