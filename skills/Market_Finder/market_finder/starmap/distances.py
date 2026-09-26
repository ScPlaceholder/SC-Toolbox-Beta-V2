"""Terminal-to-terminal distance telemetry from UEX, copied from Trade Hub.

Used by the grocery list's "Plot Optimal Route" feature: it orders the
shopping stops by real in-game travel distance (``terminals_distances``)
instead of guessing.  Distances are persisted in a JSON cache so repeat
plots are instant and work offline; missing pairs are fetched in parallel
by :meth:`fetch_missing`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, Optional, Set, Tuple

from ..api_client import UexApiClient

log = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "market_finder")
_CACHE_PATH = os.path.join(_CACHE_DIR, "distance_cache.json")

_lock = threading.Lock()
_cache: Optional[Dict[str, float]] = None


def _load() -> Dict[str, float]:
    global _cache
    with _lock:
        if _cache is None:
            try:
                with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
                    _cache = json.load(fh)
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                _cache = {}
        return _cache


def _save() -> None:
    with _lock:
        data = dict(_cache or {})
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, _CACHE_PATH)
    except OSError:
        log.warning("Could not save distance cache")


def _key(origin_id: int, dest_id: int) -> str:
    return f"{origin_id}-{dest_id}"


def get_distance(origin_id: int, dest_id: int) -> Optional[float]:
    """Cached UEX distance for a terminal pair, or None if unknown."""
    if not origin_id or not dest_id:
        return None
    if origin_id == dest_id:
        return 0.0
    return _load().get(_key(origin_id, dest_id))


def _fetch_one(api: UexApiClient, origin_id: int, dest_id: int) -> Optional[float]:
    try:
        res = api.get(
            f"terminals_distances?id_terminal_origin={origin_id}"
            f"&id_terminal_destination={dest_id}"
        )
        if res.ok and res.data:
            return float(res.data[0].get("distance", 0) or 0)
    except Exception:
        pass
    log.debug("Could not fetch distance %d->%d", origin_id, dest_id)
    return None


def fetch_missing(pairs: Set[Tuple[int, int]], on_progress=None) -> None:
    """Fetch distances for (origin_id, dest_id) pairs not in cache, in parallel.

    *on_progress(done, total)* is called periodically so the UI can show a
    status update.  Unknown pairs stay uncached (callers fall back to the
    galaxy jump-graph distance).
    """
    cache = _load()
    missing = [(o, d) for o, d in pairs if o and d and _key(o, d) not in cache]
    if not missing:
        return
    total = len(missing)
    log.info("Fetching %d missing terminal distances (parallel)...", total)
    api = UexApiClient()
    fetched = 0
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {
            pool.submit(_fetch_one, api, o, d): (o, d)
            for o, d in missing
        }
        for future in as_completed(futures):
            o, d = futures[future]
            dist = future.result()
            if dist is not None:
                with _lock:
                    cache[_key(o, d)] = dist
                fetched += 1
            if on_progress and fetched % 10 == 0:
                try:
                    on_progress(fetched, total)
                except Exception:
                    pass
    if fetched:
        log.info("Fetched %d distances, saving cache", fetched)
        _save()
