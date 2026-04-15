"""Real-distance ranking of refineries using the UEX terminal-distance API.

Fetches actual in-game Gigameter (Gm) distances between any player
location and every refinery, caches them locally, and returns the
closest N refineries sorted by distance.

Terminal IDs are from the UEX ``/terminals`` endpoint (type=commodity).
Refinery stations share the same physical location as commodity
terminals, so the distance is effectively zero between the two
terminal types at the same station.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from typing import Optional

from services.refinery_locations import REFINERIES, RefineryLocation
from services.http_retry import urlopen_with_retry

log = logging.getLogger(__name__)

_UEX_BASE = "https://api.uexcorp.space/2.0"
_USER_AGENT = "SC-Toolbox/MiningSignals"
_TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_TOOL_DIR, ".refinery_distance_cache.json")

# ── Terminal-ID mappings ────────────────────────────────────────
# UEX commodity terminal IDs for every known player-reachable
# location.  Built from ``/terminals?type=commodity``.

# Player locations (log scanner human names → UEX commodity terminal IDs)
_PLAYER_LOC_IDS: dict[str, int] = {
    "ARC-L1": 1, "ARC-L2": 2, "ARC-L3": 3, "ARC-L4": 4, "ARC-L5": 5,
    "Baijini Point": 13,
    "Area 18": 9,
    "CRU-L1": 19, "CRU-L4": 20, "CRU-L5": 22,
    "Seraphim Station": 259,
    "Orison": 261,
    "HUR-L1": 44, "HUR-L2": 45, "HUR-L3": 46, "HUR-L4": 47, "HUR-L5": 48,
    "Everus Harbor": 25,
    "Lorville": 30,
    "MIC-L1": 54, "MIC-L2": 55, "MIC-L3": 56, "MIC-L4": 57, "MIC-L5": 58,
    "Port Tressler": 66,
    "New Babbage": 60,
    "Pyro Gateway": 252,
    "Terra Gateway": 251,
    "Ruin Station": 466,
    "Checkmate Station": 436,
    "Orbituary (Pyro III)": 443, "Orbituary": 443,
    "Stanton Gateway (Pyro)": 520,
    "Levski": 778,
    "Levski Station": 778,
    "Stanton Gateway (Nyx)": 802,
}

# Refinery locations → UEX commodity terminal IDs at the same station.
_REFINERY_IDS: dict[str, int] = {
    "HUR-L1 Green Glade Station":       44,
    "CRU-L1 Ambitious Dream Station":   19,
    "ARC-L1 Wide Forest Station":       1,
    "ARC-L2 Lively Pathway Station":    2,
    "ARC-L4 Faint Glen Station":        4,
    "MIC-L1 Shallow Frontier Station":  54,
    "MIC-L2 Long Forest Station":       55,
    "MIC-L5 Modern Icarus Station":     58,
    "Pyro Gateway":                     252,
    "Terra Gateway":                    251,
    "Ruin Station":                     466,
    "Checkmate Station":                436,
    "Orbituary Station":                443,
    "Stanton Gateway (Pyro)":           520,
    "Levski Station":                   778,
    "Stanton Gateway (Nyx)":            802,
    # Magnus Gateway is too new for UEX — no terminal ID yet
}


# ── Distance cache ──────────────────────────────────────────────


class _DistanceCache:
    """Simple JSON file cache of ``{origin_id}-{dest_id} → distance_gm``."""

    def __init__(self) -> None:
        self._data: dict[str, float] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            if os.path.isfile(_CACHE_FILE):
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    self._data = json.load(f)
        except (OSError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
            tmp = _CACHE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f)
            os.replace(tmp, _CACHE_FILE)
        except OSError:
            pass

    @staticmethod
    def _key(a: int, b: int) -> str:
        return f"{a}-{b}"

    def get(self, origin: int, dest: int) -> Optional[float]:
        with self._lock:
            return self._data.get(self._key(origin, dest))

    def put(self, origin: int, dest: int, distance: float) -> None:
        with self._lock:
            self._data[self._key(origin, dest)] = distance

    def save(self) -> None:
        with self._lock:
            self._save()


_cache = _DistanceCache()


def _fetch_distance(origin_id: int, dest_id: int) -> Optional[float]:
    """Fetch the Gm distance between two UEX terminal IDs."""
    cached = _cache.get(origin_id, dest_id)
    if cached is not None:
        return cached

    try:
        url = (
            f"{_UEX_BASE}/terminals_distances"
            f"?id_terminal_origin={origin_id}"
            f"&id_terminal_destination={dest_id}"
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })
        with urlopen_with_retry(req, timeout=10, retries=2) as resp:
            body = json.load(resp)

        data = body.get("data")
        if isinstance(data, dict):
            dist = data.get("distance")
        elif isinstance(data, list) and data:
            dist = data[0].get("distance")
        else:
            return None

        if dist is not None:
            dist = float(dist)
            _cache.put(origin_id, dest_id, dist)
            return dist
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        pass
    return None


def _resolve_player_terminal(player_loc: str) -> Optional[int]:
    """Map a human-readable player location to a UEX terminal ID."""
    if not player_loc:
        return None
    # Direct match
    if player_loc in _PLAYER_LOC_IDS:
        return _PLAYER_LOC_IDS[player_loc]
    # Loose substring match (handles "HUR-L1 Green Glade Station" → "HUR-L1")
    low = player_loc.lower()
    for name, tid in _PLAYER_LOC_IDS.items():
        if name.lower() in low or low in name.lower():
            return tid
    return None


# ── Public API ──────────────────────────────────────────────────


def fmt_distance(gm: float) -> str:
    """Format Gigameters for display (matches Trade Hub convention)."""
    if gm >= 1000:
        return f"{gm / 1000:.1f} Tm"
    return f"{gm:.0f} Gm"


def nearest_refineries(
    player_loc: str,
    n: int = 3,
) -> list[tuple[RefineryLocation, Optional[float]]]:
    """Return the ``n`` closest refineries with real Gm distances.

    Each entry is ``(refinery, distance_gm)`` where ``distance_gm``
    is ``None`` if the distance couldn't be resolved (unknown terminal
    or API failure).  Results are sorted by distance ascending, with
    ``None`` distances at the end.

    Fetches missing distances from the UEX API on-demand and caches
    them to ``.refinery_distance_cache.json``.  Call from a background
    thread if latency matters.
    """
    origin_id = _resolve_player_terminal(player_loc)

    results: list[tuple[RefineryLocation, Optional[float]]] = []
    for ref in REFINERIES:
        dest_id = _REFINERY_IDS.get(ref.name)
        if origin_id is None or dest_id is None:
            results.append((ref, None))
            continue

        if origin_id == dest_id:
            # Same station
            results.append((ref, 0.0))
            continue

        dist = _fetch_distance(origin_id, dest_id)
        results.append((ref, dist))

    # Save any newly-fetched distances
    _cache.save()

    # Sort: known distances ascending, then None at end.
    # Note: ``t[1] or 999999`` is wrong because 0.0 is falsy.
    results.sort(key=lambda t: (t[1] is None, t[1] if t[1] is not None else 999999))
    return results[:n]
