"""Mission DB worker: missions, blueprint->mission join, where to mine.

Loads scmdb.net data through MissionDataManager exactly as the window does
(its disk cache is read when still inside its TTL, never written here).
The blueprint join is logic.missions_for_blueprint, lifted out of the
Qt modal so it can run headless.
"""
from __future__ import annotations

import threading
import time

import _assist_logic as L

from data import cache as _cache
from data.manager import MissionDataManager

try:
    from config import MINING_GROUP_TYPES
except Exception:                                           # noqa: BLE001
    MINING_GROUP_TYPES = {}

_cache.save_cache = lambda *a, **k: None       # read-only in the Assistant

_m = MissionDataManager()
_state = {"ts": 0.0}
_TTL = 3600.0


def _wait(fn, what: str, timeout: float = 120.0) -> None:
    ev = threading.Event()
    fn(on_done=ev.set)
    if not ev.wait(timeout):
        raise L.ToolFail(f"Mission DB {what} load timed out")


def _core() -> MissionDataManager:
    if not _m.loaded or time.time() - _state["ts"] > _TTL:
        if _m.loaded:
            _m.loaded = False
            _m.crafting_loaded = False
            _m.mining_loaded = False
        # load() leaves `loading` stuck True when a fetch fails, and then
        # returns without ever calling on_done; clear it so a retry works.
        _m.loading = False
        _wait(_m.load, "mission")
        if not _m.contracts:
            raise L.ToolFail("Mission DB could not load mission data: "
                             + (_m.error or "no contracts returned"))
        _state["ts"] = time.time()
    return _m


def _crafting() -> MissionDataManager:
    m = _core()
    if not m.crafting_loaded:
        _wait(m.load_crafting, "crafting")
    return m


def _mining() -> MissionDataManager:
    m = _core()
    if not m.mining_loaded:
        _wait(m.load_mining, "mining")
        if not m.all_resource_names:
            raise L.ToolFail("Mission DB could not load mining data")
    return m


def missions_for_blueprint(name: str, limit: int = 10) -> dict:
    m = _crafting()
    names = set()
    for pool in m.blueprint_pools.values():
        for it in pool.get("blueprints", []) or []:
            if isinstance(it, dict) and it.get("name"):
                names.add(it["name"])
    if not names:
        raise L.ToolFail("Mission DB has no blueprint reward pools loaded")
    best, alts = L.resolve_one(name, sorted(names), what="mission-reward blueprint")
    rows = L.missions_for_blueprint(best, m.blueprint_pools, m.contracts, m.get_faction)
    grouped = L.collapse_missions(rows, limit=max(1, min(int(limit or 10), 25)))
    res = {
        "blueprint": best,
        "missions": grouped,
        "mission_count": len(rows),
        "game_version": m.version,
        "other_matches": alts,
    }
    if not rows:
        res["empty"] = True
        res["note"] = "the blueprint is in a reward pool but no current contract draws from it"
    return res


def where_to_mine(resource: str, limit: int = 12) -> dict:
    m = _mining()
    best, alts = L.resolve_one(resource, m.all_resource_names, what="mineable resource")
    locs = L.dedupe_locations(m.resource_to_locations.get(best, []), MINING_GROUP_TYPES)
    n = max(1, min(int(limit or 12), 40))
    res = {
        "resource": best,
        "locations": locs[:n],
        "location_count": len(locs),
        "systems": sorted({l["system"] for l in locs if l.get("system")}),
        "game_version": m.version,
        "other_matches": alts,
        "note": "probability = chance a deposit of this type contains it; max_pct = best share of a rock",
    }
    if not locs:
        res["empty"] = True
    return res


def search_missions(faction: str = "", system: str = "", mission_type: str = "",
                    limit: int = 10) -> dict:
    m = _core()
    rows = m.contracts
    used = {}
    if faction:
        fac, _ = L.resolve_one(faction, m.all_faction_names, what="faction")
        used["faction"] = fac
        rows = [c for c in rows if m.get_faction(c.get("factionGuid", "")).get("name") == fac]
    if system:
        sysname, _ = L.resolve_one(system, m.all_systems, what="star system", min_score=0.6)
        used["system"] = sysname
        rows = [c for c in rows
                if sysname in (c.get("systems") or c.get("availableSystems") or [])]
    if mission_type:
        types = [t for t in m.all_mission_types
                 if L.norm(mission_type) in L.norm(t)] or \
                [L.resolve_one(mission_type, m.all_mission_types, what="mission type")[0]]
        used["mission_type"] = types
        rows = [c for c in rows if c.get("missionType") in types]
    flat = []
    for c in rows:
        title = c.get("title", "?") or "?"
        if title.startswith("@"):
            title = c.get("debugName", title)
        flat.append({
            "title": title,
            "faction": m.get_faction(c.get("factionGuid", "")).get("name", "?"),
            "mission_type": c.get("missionType", ""),
            "systems": c.get("systems") or c.get("availableSystems") or [],
            "reward_uec": c.get("rewardUEC"),
            "chance": 1,
            "illegal": bool(c.get("illegal")),
        })
    grouped = L.collapse_missions(flat, limit=max(1, min(int(limit or 10), 25)))
    grouped.sort(key=lambda g: -(g.get("reward_uec") or 0))
    for g in grouped:
        g.pop("chance", None)
    res = {"filters": used, "matching_contracts": len(flat), "missions": grouped,
           "game_version": m.version}
    if not flat:
        res["empty"] = True
        res["note"] = "no contract matched all filters"
    return res


EXPORTS = {
    "missions_for_blueprint": missions_for_blueprint,
    "where_to_mine": where_to_mine,
    "search_missions": search_missions,
}
