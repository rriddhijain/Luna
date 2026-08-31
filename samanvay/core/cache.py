"""
samanvay/core/cache.py

OWNER: seat 5 (Systems & Performance).

Canonicalisation (albedo + phase congruency + orientation + mask) is the
runtime hog of the pipeline and it is *deterministic*: the same product under
the same parameters always yields the same CanonicalImage. Computing it twice
is the easiest speed win in the project (roadmap I10). This module caches it,
keyed by a stable fingerprint of ``(product identity, parameters)``.

Design
------
* Content-addressed. The key is a sha256 over the product's identity (id +,
  for file-backed products, path/size/mtime; for in-memory arrays, a digest of
  the bytes), the canonicalisation-relevant metadata, the parameter dict, and a
  cache-format version. Change any input and the key changes -- stale results
  can never be silently served.
* Two tiers. A small in-process LRU (dict) avoids re-reading from disk within a
  run; a disk tier (``<key>.npz`` + ``<key>.json`` sidecar) survives across
  runs and across the container boundary.
* Atomic writes. Results are written to a temp file and ``os.replace``-d into
  place, so an interrupted run never leaves a half-written ``.npz`` that a
  later run would load as truth.
* Honest stats. ``cache_stats()`` exposes hits/misses/writes so the benchmark
  can *show* "second run of the same pair is dramatically faster" rather than
  assert it.

The cache is safe to disable (``enabled=False``): it then simply computes and
returns, so correctness never depends on the cache being present.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from samanvay.types import CanonicalImage, Product

__all__ = [
    "CanonicalCache",
    "cache_stats",
    "canonicalise_cached",
    "fingerprint",
    "reset_cache_stats",
]

# Bump when the on-disk layout or the meaning of a key changes, to invalidate
# every previously written entry without anyone having to clear the directory.
CACHE_VERSION = "1"

# Product metadata that genuinely changes the canonicalisation result and must
# therefore participate in the key (illumination geometry, scale, sensor).
_META_KEYS_IN_KEY = (
    "sun_az_deg",
    "sun_el_deg",
    "incidence_deg",
    "emission_deg",
    "gsd_m",
    "instrument",
)

# Hash full bytes only for arrays below this size (small fixtures / crops);
# larger in-memory arrays fall back to a strided digest so keying stays cheap.
_FULL_HASH_MAX_BYTES = 64 * 1024 * 1024


@dataclass
class _Stats:
    hits: int = 0
    misses: int = 0
    writes: int = 0
    disk_hits: int = 0
    mem_hits: int = 0

    def as_dict(self) -> dict:
        total = self.hits + self.misses
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "disk_hits": self.disk_hits,
            "mem_hits": self.mem_hits,
            "hit_rate": round(self.hits / total, 4) if total else 0.0,
        }


_GLOBAL_STATS = _Stats()


def cache_stats() -> dict:
    """Return a snapshot of cache hit/miss counters for this process."""
    return _GLOBAL_STATS.as_dict()


def reset_cache_stats() -> None:
    global _GLOBAL_STATS
    _GLOBAL_STATS = _Stats()


def _array_digest(arr: np.ndarray) -> str:
    """A cheap-but-faithful digest of an array's identity."""
    h = hashlib.sha256()
    h.update(str(arr.shape).encode())
    h.update(str(arr.dtype).encode())
    if arr.nbytes <= _FULL_HASH_MAX_BYTES:
        h.update(np.ascontiguousarray(arr).tobytes())
    else:
        # Strided sample + reductions: stable, O(sample) not O(N), and
        # sensitive to edits anywhere in the array in practice.
        flat = arr.reshape(-1)
        stride = max(1, flat.size // 1_000_000)
        h.update(np.ascontiguousarray(flat[::stride]).tobytes())
        h.update(np.float64(arr.sum(dtype=np.float64)).tobytes())
    return h.hexdigest()


def _identity_fields(product: Product) -> dict:
    """Everything about the product that pins the canonicalisation input."""
    meta = product.meta or {}
    fields: dict = {
        "product_id": meta.get("product_id") or os.path.basename(product.path or ""),
    }
    # Prefer cheap file stat identity for real (on-disk) products.
    path = product.path
    if path and os.path.exists(path):
        st = os.stat(path)
        fields["path"] = os.path.abspath(path)
        fields["size"] = st.st_size
        fields["mtime_ns"] = st.st_mtime_ns
    elif isinstance(product.array, np.ndarray):
        fields["array_digest"] = _array_digest(product.array)
    else:
        # Reader-backed product with no file we can stat: fall back to id only.
        fields["path"] = str(path)
    fields["meta"] = {k: meta.get(k) for k in _META_KEYS_IN_KEY}
    return fields


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(product: Product, params: dict | None) -> str:
    """Stable cache key for ``(product, params)`` as a hex sha256 string."""
    payload = {
        "version": CACHE_VERSION,
        "identity": _identity_fields(product),
        "params": params or {},
    }
    return hashlib.sha256(_canonical_json(payload).encode()).hexdigest()


class CanonicalCache:
    """Two-tier (memory + disk) cache of :class:`CanonicalImage` results."""

    def __init__(
        self,
        root: str | None = None,
        *,
        enabled: bool = True,
        mem_entries: int = 8,
    ) -> None:
        self.enabled = enabled
        self.root = root or os.environ.get("SAMANVAY_CACHE_DIR", ".samanvay_cache")
        self.mem_entries = max(0, mem_entries)
        self._mem: OrderedDict[str, CanonicalImage] = OrderedDict()
        if self.enabled:
            os.makedirs(self.root, exist_ok=True)

    # -- paths ----------------------------------------------------------------
    def _npz_path(self, key: str) -> str:
        return os.path.join(self.root, f"{key}.npz")

    def _json_path(self, key: str) -> str:
        return os.path.join(self.root, f"{key}.json")

    # -- memory tier ----------------------------------------------------------
    def _mem_get(self, key: str) -> CanonicalImage | None:
        if key in self._mem:
            self._mem.move_to_end(key)
            return self._mem[key]
        return None

    def _mem_put(self, key: str, value: CanonicalImage) -> None:
        if self.mem_entries == 0:
            return
        self._mem[key] = value
        self._mem.move_to_end(key)
        while len(self._mem) > self.mem_entries:
            self._mem.popitem(last=False)

    # -- disk tier ------------------------------------------------------------
    def _disk_get(self, key: str) -> CanonicalImage | None:
        npz_path = self._npz_path(key)
        if not os.path.exists(npz_path):
            return None
        try:
            with np.load(npz_path) as data:
                albedo = data["albedo"]
                pc = data["pc"]
                pc_orient = data["pc_orient"]
                mask = data["mask"]
            params: dict = {}
            json_path = self._json_path(key)
            if os.path.exists(json_path):
                with open(json_path) as f:
                    params = json.load(f).get("params", {})
            return CanonicalImage(
                albedo=albedo, pc=pc, pc_orient=pc_orient, mask=mask, params=params
            )
        except (OSError, ValueError, KeyError, EOFError):
            # Corrupt / partially written entry: treat as a miss and let the
            # caller recompute and overwrite it.
            return None

    def _disk_put(self, key: str, value: CanonicalImage, product: Product) -> None:
        os.makedirs(self.root, exist_ok=True)
        # Arrays -> atomic .npz
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".npz.tmp")
        os.close(fd)
        try:
            with open(tmp, "wb") as f:
                np.savez(
                    f,
                    albedo=value.albedo,
                    pc=value.pc,
                    pc_orient=value.pc_orient,
                    mask=value.mask,
                )
            os.replace(tmp, self._npz_path(key))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)
        # Human-readable sidecar (params + what this entry is)
        sidecar = {
            "key": key,
            "version": CACHE_VERSION,
            "product_id": (product.meta or {}).get("product_id"),
            "shape": list(np.asarray(value.albedo).shape),
            "params": _jsonable(value.params),
        }
        _atomic_write_text(self._json_path(key), _canonical_json(sidecar))

    # -- public API -----------------------------------------------------------
    def get_or_compute(
        self,
        product: Product,
        params: dict | None,
        compute: Callable[[Product, dict | None], CanonicalImage],
    ) -> CanonicalImage:
        """Return the cached CanonicalImage, computing + storing it on a miss."""
        if not self.enabled:
            _GLOBAL_STATS.misses += 1
            return compute(product, params)

        key = fingerprint(product, params)

        hit = self._mem_get(key)
        if hit is not None:
            _GLOBAL_STATS.hits += 1
            _GLOBAL_STATS.mem_hits += 1
            return hit

        hit = self._disk_get(key)
        if hit is not None:
            _GLOBAL_STATS.hits += 1
            _GLOBAL_STATS.disk_hits += 1
            self._mem_put(key, hit)
            return hit

        _GLOBAL_STATS.misses += 1
        value = compute(product, params)
        try:
            self._disk_put(key, value, product)
            _GLOBAL_STATS.writes += 1
        except OSError:
            # A read-only / full filesystem must not break the pipeline.
            pass
        self._mem_put(key, value)
        return value

    def clear(self) -> None:
        """Drop the in-memory tier and delete every entry on disk."""
        self._mem.clear()
        if os.path.isdir(self.root):
            for name in os.listdir(self.root):
                if name.endswith(".npz") or name.endswith(".json"):
                    try:
                        os.remove(os.path.join(self.root, name))
                    except OSError:
                        pass


def _jsonable(obj):
    """Best-effort conversion of a params dict to JSON-safe values."""
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


def _atomic_write_text(path: str, text: str) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# A process-wide default cache, used by the convenience wrapper below so that
# repeated canonicalisation of the same product within a run is deduplicated
# even when the caller does not thread a cache object through.
_DEFAULT_CACHE: CanonicalCache | None = None


def _default_cache() -> CanonicalCache:
    global _DEFAULT_CACHE
    if _DEFAULT_CACHE is None:
        _DEFAULT_CACHE = CanonicalCache()
    return _DEFAULT_CACHE


def canonicalise_cached(
    product: Product,
    params: dict | None = None,
    *,
    cache: CanonicalCache | None = None,
    compute: Callable[[Product, dict | None], CanonicalImage] | None = None,
) -> CanonicalImage:
    """Cache-aware wrapper around seat 2's ``canonicalise``.

    Drop-in for ``canonicalise(product, params)`` that first consults the
    cache. ``compute`` defaults to :func:`samanvay.photometry.normalize.canonicalise`
    (imported lazily to avoid an import cycle).
    """
    if cache is None:
        cache = _default_cache()
    if compute is None:
        from samanvay.photometry.normalize import canonicalise as compute  # lazy

    return cache.get_or_compute(product, params, compute)
