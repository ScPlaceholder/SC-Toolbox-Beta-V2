"""Terminal-to-terminal distance telemetry from UEX, copied from Trade Hub.

Used by the grocery list's "Plot Route" feature.  :func:`site_distance`
is the route planner's cost model, in gigametres (Gm):

1. UEX ``terminals_distances`` telemetry for any terminal pair of the two
   sites (the API is symmetric, so either direction counts);
2. else the straight-line distance between the sites' bodies in
   ``bodies.json`` (metres; it reproduces UEX: Area 18 -> New Babbage is
   59.46 Gm vs UEX 59, Area 18 -> Ruin Station via the Pyro gateway
   99.24 Gm vs UEX 99).  A cross-system leg is routed through the gateway
   stations along the jump path;
3. plus :data:`JUMP_PENALTY_GM` per system-gateway jump, because the jump
   tunnel takes minutes but covers no Gm (UEX leaves it out).

Distances are persisted in a JSON cache so repeat plots are instant and
work offline; missing pairs are fetched in parallel by
:meth:`fetch_missing`.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import math
import re
from typing import Dict, List, Optional, Set, Tuple

from ..api_client import UexApiClient
from .data import LOC_ALIASES, Galaxy, load_bodies, norm_loc

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


# -- route cost model ---------------------------------------------------------------

#: Travel cost added per system-gateway jump, in Gm.  PRODUCT DECISION: the
#: jump tunnel covers no Gm but takes roughly three minutes, about what
#: ~50 Gm of quantum travel takes at 0.283 Gm/s (Trade Hub's ETA speed).  It
#: makes the planner prefer finishing a system before jumping, and never
#: jump out and back for an item it can buy at home for the same price.
JUMP_PENALTY_GM = 50.0

_geo_lock = threading.Lock()
_geo: Optional[Tuple[Galaxy, Dict[str, Dict[str, object]]]] = None


def _geometry() -> Tuple[Galaxy, Dict[str, Dict[str, object]]]:
    """(Galaxy, {SYSTEM_CODE: {normalised body name: Body}}), loaded once."""
    global _geo
    with _geo_lock:
        if _geo is None:
            gal = Galaxy.load()
            by: Dict[str, Dict[str, object]] = {}
            for code, bodies in load_bodies().items():
                d: Dict[str, object] = {}
                for b in bodies:
                    d.setdefault(norm_loc(b.name), b)
                by[code.upper()] = d
            _geo = (gal, by)
        return _geo


def system_code(name: str) -> str:
    """UEX system name ('Stanton') -> galaxy code ('STANTON')."""
    gal, _by = _geometry()
    n = (name or "").strip().lower()
    for s in gal.systems:
        if s.name.lower() == n or s.code.lower() == n:
            return s.code
    return (name or "").strip().upper()


def resolve_site(system: str, places: List[str]):
    """The ``bodies.json`` body a site sits at: the finest of *places* that
    names a body in *system* (a city resolves to its planet only when the
    city itself is not a body).  Returns ``(system_code, Body)`` or
    ``(system_code, None)``."""
    _gal, by = _geometry()
    code = system_code(system)
    bodies = by.get(code, {})
    for p in places or []:
        n = norm_loc(p)
        n = LOC_ALIASES.get(n, n)
        for cand in (n, re.sub(r"station$", "", n)):
            if cand and cand in bodies:
                return code, bodies[cand]
    return code, None


def jump_path(code_a: str, code_b: str) -> Optional[List[str]]:
    """System codes along the jump route (inclusive), or None if unconnected."""
    gal, _by = _geometry()
    return gal.shortest_path(code_a, code_b)


def _xyz(body) -> Tuple[float, float, float]:
    return (body.x, body.y, body.z)


def body_distance(a: dict, b: dict) -> Optional[float]:
    """Gm between two sites from body coordinates, via gateways across systems,
    plus :data:`JUMP_PENALTY_GM` per jump.  None when a site cannot be placed
    or the systems are not connected."""
    ca, ba = resolve_site(a.get("system") or "", a.get("places") or [])
    cb, bb = resolve_site(b.get("system") or "", b.get("places") or [])
    if ba is None or bb is None:
        return None
    if ca == cb:
        return math.dist(_xyz(ba), _xyz(bb)) / 1e9
    path = jump_path(ca, cb)
    if not path:
        return None
    gal, by = _geometry()
    total = 0.0
    cur = _xyz(ba)
    for x, y in zip(path, path[1:]):
        out_gw = by.get(x, {}).get(norm_loc(gal.by_code[y].name + " Gateway"))
        in_gw = by.get(y, {}).get(norm_loc(gal.by_code[x].name + " Gateway"))
        if out_gw is None or in_gw is None:
            return None
        total += math.dist(cur, _xyz(out_gw)) / 1e9 + JUMP_PENALTY_GM
        cur = _xyz(in_gw)
    return total + math.dist(cur, _xyz(bb)) / 1e9


def telemetry_distance(a_ids: List[int], b_ids: List[int]) -> Optional[float]:
    """Cached UEX distance between any terminal of site A and any of site B
    (either direction), or None."""
    for o in a_ids or []:
        for d in b_ids or []:
            v = get_distance(o, d)
            if v is None:
                v = get_distance(d, o)
            if v is not None:
                return v
    return None


def site_distance(a: dict, b: dict) -> Optional[float]:
    """The route planner's cost between two sites, in Gm (see module doc)."""
    if a.get("key") is not None and a.get("key") == b.get("key"):
        return 0.0
    ca, cb = system_code(a.get("system") or ""), system_code(b.get("system") or "")
    t = telemetry_distance(a.get("terminal_ids") or [], b.get("terminal_ids") or [])
    if t is not None:
        if ca == cb:
            return t
        path = jump_path(ca, cb)
        return t + JUMP_PENALTY_GM * (len(path) - 1 if path else 1)
    return body_distance(a, b)


def telemetry_pairs(sites: List[dict], limit: int = 120) -> Set[Tuple[int, int]]:
    """One representative terminal pair per site pair not already known, at
    most *limit* of them (telemetry is symmetric, so one direction each)."""
    out: Set[Tuple[int, int]] = set()
    for i, a in enumerate(sites):
        for b in sites[i + 1:]:
            ia, ib = a.get("terminal_ids") or [], b.get("terminal_ids") or []
            if not ia or not ib or a.get("key") == b.get("key"):
                continue
            if telemetry_distance(ia, ib) is not None:
                continue
            out.add((min(ia), min(ib)))
            if len(out) >= limit:
                return out
    return out


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
