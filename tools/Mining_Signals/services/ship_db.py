"""
Lightweight Star Citizen vehicle database for the Mining Ledger.

Loads ship names and crew sizes from the Market Finder UEX cache
(``skills/Market_Finder/.uex_cache.json``).  Falls back to an
empty list if the cache doesn't exist yet.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import List

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
)
_UEX_CACHE = os.path.join(_PROJECT_ROOT, "skills", "Market_Finder", ".uex_cache.json")


@dataclass(frozen=True)
class ShipModel:
    name: str       # display name, e.g. "Carrack"
    name_full: str  # e.g. "Anvil Carrack"
    crew_max: int   # max crew count


_cache: list[ShipModel] | None = None


def _parse_crew(raw: str) -> int:
    """Parse crew string like '1', '2,5', '4,8' → max value."""
    try:
        parts = [int(x.strip()) for x in raw.split(",") if x.strip()]
        return max(parts) if parts else 1
    except (ValueError, TypeError):
        return 1


def load_ship_db() -> list[ShipModel]:
    """Return the full ship list, cached after first load."""
    global _cache
    if _cache is not None:
        return _cache

    ships: list[ShipModel] = []
    try:
        if os.path.isfile(_UEX_CACHE):
            with open(_UEX_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
            vehicles = data.get("vehicles", [])
            for v in vehicles:
                name = v.get("name", "")
                if not name:
                    continue
                ships.append(ShipModel(
                    name=name,
                    name_full=v.get("name_full", name),
                    crew_max=_parse_crew(str(v.get("crew", "1"))),
                ))
            ships.sort(key=lambda s: s.name.lower())
            log.info("ship_db: loaded %d vehicles from UEX cache", len(ships))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        log.warning("ship_db: failed to load UEX cache: %s", exc)

    _cache = ships
    return ships


def fuzzy_match(query: str, ships: list[ShipModel] | None = None, limit: int = 15) -> list[ShipModel]:
    """Simple fuzzy match: substring on name or name_full, case-insensitive."""
    if ships is None:
        ships = load_ship_db()
    if not query:
        return ships[:limit]
    q = query.lower()
    # Exact prefix first, then substring
    prefix = [s for s in ships if s.name.lower().startswith(q) or s.name_full.lower().startswith(q)]
    substr = [s for s in ships if s not in prefix and (q in s.name.lower() or q in s.name_full.lower())]
    return (prefix + substr)[:limit]


def crew_for_model(model_name: str) -> int:
    """Look up max crew for a ship model name. Returns 1 if not found."""
    for s in load_ship_db():
        if s.name.lower() == model_name.lower():
            return s.crew_max
    return 1
