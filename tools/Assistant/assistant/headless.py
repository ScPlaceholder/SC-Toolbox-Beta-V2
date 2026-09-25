"""In-process headless helpers — the few things that need no tool worker.

  * ship_info()          -- cargo SCU from shared.ships (no network)
  * ship_scu()           -- same, 0 when unknown
  * route_popup_data()   -- shape a find_trade_routes route for Trade
                            Hub's pinned popup (pure)
  * resolve_skill()      -- map a spoken tool name to a launcher skill id
  * call()               -- run a function in a tool's worker subprocess

Everything that imports a skill's own modules runs in a per-tool worker
(worker_pool.py), because the skills reuse top-level package names and
cannot share one interpreter. This module only imports ``shared`` and the
launcher's ``core.skill_registry``, which every process already has.

The old in-process trade route and market price paths were removed: they
used UEX's /commodities_routes and bare /items, both of which UEX now
answers with HTTP 400.
"""
from __future__ import annotations

import logging

from . import logic
from .tools import ToolError
from .worker_pool import get_pool

log = logging.getLogger(__name__)


def call(base_dir: str, tool_key: str, fn: str, args: dict, timeout: float = 90.0):
    """Run ``fn(**args)`` in the *tool_key* worker; ToolError on failure."""
    return get_pool(base_dir).call(tool_key, fn, args, timeout=timeout)


def ship_info(base_dir: str, name: str) -> dict:
    """Cargo capacity and preset info for a ship (fuzzy name match)."""
    try:
        from shared import ships as ships_mod
    except Exception as exc:                                # noqa: BLE001
        raise ToolError(f"ship database unavailable: {exc}")
    presets = getattr(ships_mod, "SHIP_PRESETS", {})
    needle = (name or "").strip().lower()
    if not needle:
        raise ToolError("say a ship name, e.g. Caterpillar")
    for key, scu in presets.items():
        if needle in key.lower() or key.lower() in needle:
            return {"ship": key, "scu": scu}
    near = logic.best_matches(name, presets.keys(), limit=1, min_score=0.6)
    if near:
        key = near[0][1]
        return {"ship": key, "scu": presets[key]}
    raise ToolError(f"ship '{name}' not found — try another name")


def ship_scu(base_dir: str, name: str) -> int:
    """Best-effort SCU lookup; 0 when unknown (ranking still works)."""
    try:
        return int(ship_info(base_dir, name).get("scu", 0))
    except ToolError:
        return 0


def route_popup_data(route: dict, ship: str = "", ship_scu: int = 0) -> dict:
    """Shape a find_trade_routes() entry into Trade Hub RouteDetailDialog
    data contract (see TradeHubWindow._open_route_detail)."""
    eff = int(route.get("effective_scu", 0))
    price_buy = float(route.get("price_buy", 0))
    margin = float(route.get("margin_per_scu", 0))
    roi = (margin / price_buy * 100.0) if price_buy > 0 else 0.0
    if ship and ship_scu:
        ship_lbl = f"{ship} ({ship_scu:,} SCU)"
    else:
        ship_lbl = ship or "No ship"
    return {
        "type": "single",
        "ship": ship_lbl,
        "commodity": route.get("commodity", "?"),
        "eff_scu": eff,
        "price_buy": price_buy,
        "price_sell": float(route.get("price_sell", 0)),
        "margin": margin,
        "profit": float(route.get("est_profit", eff * margin)),
        "roi": roi,
        "buy_terminal": route.get("buy_terminal", "?"),
        "buy_location": route.get("buy_location", "?"),
        "buy_system": route.get("buy_system", "?"),
        "sell_terminal": route.get("sell_terminal", "?"),
        "sell_location": route.get("sell_location", "?"),
        "sell_system": route.get("sell_system", "?"),
        "scu_available": int(route.get("scu_available", 0)),
        "scu_demand": int(route.get("scu_demand", 0)),
        "distance": float(route.get("distance_gm") or 0),
    }


# spoken aliases -> launcher skill id (ids come from core.skill_registry)
_SKILL_ALIASES = {
    "trade": "trade", "trading": "trade", "trade hub": "trade", "routes": "trade",
    "market": "market", "market finder": "market", "shop": "market", "prices": "market",
    "missions": "missions", "mission database": "missions", "mission db": "missions",
    "craft": "craft_db", "crafting": "craft_db", "craft database": "craft_db",
    "blueprints": "craft_db",
    "mining": "mining", "mining loadout": "mining",
    "signals": "mining_signals", "mining signals": "mining_signals", "scanner": "mining_signals",
    "cargo": "cargo", "cargo loader": "cargo",
    "dps": "dps", "dps calculator": "dps", "loadout builder": "dps",
    "battle buddy": "battle_buddy", "hud": "battle_buddy",
    "playtime": "playtime", "play time": "playtime",
    "starmap": "starmap", "star map": "starmap", "map": "starmap",
    "mouse blocker": "mouse_blocker",
    "suit": "suitmk2", "suitmk2": "suitmk2",
    "assistant": "assistant",
}


def list_skills(base_dir: str) -> list:
    """The launcher's discovered skills: [{id, name, folder}]."""
    try:
        from core.skill_registry import discover_skills
    except Exception as exc:                                # noqa: BLE001
        raise ToolError(f"launcher skill registry unavailable: {exc}")
    return [{"id": s.id, "name": str(s.name), "folder": s.folder}
            for s in discover_skills(base_dir)]


def resolve_skill(base_dir: str, name: str) -> dict:
    """Map a spoken tool name to one discovered skill (ToolError if none)."""
    skills = list_skills(base_dir)
    by_id = {s["id"]: s for s in skills}
    q = logic.norm(name)
    if not q:
        raise ToolError("say which tool to open, e.g. Trade Hub")
    if q.replace(" ", "_") in by_id:
        return by_id[q.replace(" ", "_")]
    if q in _SKILL_ALIASES and _SKILL_ALIASES[q] in by_id:
        return by_id[_SKILL_ALIASES[q]]
    labels = []
    for s in skills:
        labels += [(s["name"], s), (s["folder"].replace("_", " "), s), (s["id"].replace("_", " "), s)]
    labels += [(alias, by_id[sid]) for alias, sid in _SKILL_ALIASES.items() if sid in by_id]
    try:
        best, _ = logic.resolve_one(name, labels, key=lambda t: t[0], what="toolbox tool")
    except logic.ToolFail as exc:
        raise ToolError(f"{exc}. Tools: " + ", ".join(s["name"] for s in skills))
    return best[1]
