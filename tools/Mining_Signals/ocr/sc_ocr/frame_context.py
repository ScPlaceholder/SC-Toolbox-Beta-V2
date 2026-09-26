"""Per-scan shared frame context — the percept layer (the "skin").

Detectors across the pipeline each independently rebuild the same
normalized views of the captured frame: the max-of-RGB-channels
grayscale (chromatic-aberration-resilient, the canonical detection
input) is reconstructed ~6 times per scan from the same image. This
module builds each normalization ONCE and hands out the cached array,
so every detector reads one shared percept of the world rather than
touching raw pixels independently — the foundation that later lets a
change-map / dirty-rect feed live in exactly one place.

Concurrency: the pipeline runs scans on a thread pool, so the cache is
a strict OPTIMIZATION that can never return a wrong array. The value
returned is always the one built for THIS call's image (a local), and
the one-entry cache is keyed on (id, size, mode): a new frame evicts
the prior, and id-reuse after GC can't alias because size/mode are in
the key. The expensive build runs outside the lock; only the tiny
read/store is serialized. Worst case under contention is a cache miss
(rebuild) — never stale data.
"""
from __future__ import annotations

import threading
from typing import Optional

import numpy as np
from PIL import Image

_lock = threading.Lock()
_key: Optional[tuple] = None
_max_channel: Optional[np.ndarray] = None


def _img_key(img: "Image.Image") -> tuple:
    return (id(img), img.size, img.mode)


def max_channel(img: "Image.Image") -> np.ndarray:
    """max-of-RGB-channels grayscale (uint8), built once per frame.

    This is the canonical detection grayscale: per-pixel brightest
    channel, which preserves stroke shape under the HUD's chromatic
    aberration where luma would blur it. Identical output to
    ``np.array(img.convert("RGB"), dtype=np.uint8).max(axis=2)``.
    """
    global _key, _max_channel
    k = _img_key(img)
    with _lock:
        if _key == k and _max_channel is not None:
            return _max_channel
    mc = (
        np.array(img.convert("RGB"), dtype=np.uint8)
        .max(axis=2)
        .astype(np.uint8)
    )
    with _lock:
        _key = k
        _max_channel = mc
    return mc


def reset() -> None:
    """Drop the cached frame (tests / explicit invalidation)."""
    global _key, _max_channel
    with _lock:
        _key = None
        _max_channel = None
