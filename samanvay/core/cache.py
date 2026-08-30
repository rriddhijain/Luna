"""Seat 5 · core/cache — phase congruency is expensive and deterministic, so compute it once.

Keys are content-addressed over (product_id, params) with sha256; hash() is not
used anywhere here because it is salted per process and would miss every time.
"""

import hashlib
import json
import os
import tempfile

import numpy as np

DEFAULT_CACHE_DIR = os.path.join(".cache", "samanvay")


def _jsonable(obj):
    """JSON fallback for the few non-JSON types that legitimately appear in params."""
    if isinstance(obj, np.generic):
        return obj.item()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, (set, frozenset)):
        return sorted(str(v) for v in obj)
    if isinstance(obj, os.PathLike):
        return os.fspath(obj)
    # Anything else has no stable text form (repr of an object embeds its address),
    # and a key that changes per process is worse than no cache at all.
    raise TypeError(f"cache_key: {type(obj).__name__} is not stably serialisable")


def cache_key(product_id: str, params: dict) -> str:
    """Stable hex digest over a product id and its parameter dict, identical across processes."""
    blob = json.dumps(
        {"product_id": str(product_id), "params": params if params else {}},
        sort_keys=True, separators=(",", ":"), default=_jsonable,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _path(key, cache_dir):
    return os.path.join(cache_dir or DEFAULT_CACHE_DIR, f"{key}.npz")


def cache_load(key, cache_dir=DEFAULT_CACHE_DIR):
    """Return the cached dict of arrays, or None on miss / corrupt / partial entry."""
    path = _path(key, cache_dir)
    if not os.path.exists(path):
        return None
    try:
        with np.load(path) as z:
            return {name: z[name] for name in z.files}  # materialise inside the guard
    except Exception:
        return None  # a poisoned entry is a miss, never an exception


def cache_store(key, arrays: dict, cache_dir=DEFAULT_CACHE_DIR):
    """Write a dict of arrays atomically (temp file then rename) so a killed run cannot poison it."""
    cache_dir = cache_dir or DEFAULT_CACHE_DIR
    os.makedirs(cache_dir, exist_ok=True)
    path = _path(key, cache_dir)
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".tmp-", suffix=".npz")
    try:
        with os.fdopen(fd, "wb") as f:  # file object, so savez cannot re-append ".npz"
            np.savez(f, **arrays)
        os.replace(tmp, path)  # atomic on POSIX and on Windows
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
