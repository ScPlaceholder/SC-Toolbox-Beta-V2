"""Headless data services — information without opening any tool window.

These functions let the LLM *summon* toolbox data directly:

  * ship_info()          -- cargo SCU, presets (shared.ships)
  * find_trade_routes()  -- UEX commodities_routes ranked by est. profit,
                            reusing Trade Hub's own UEXClient + route_engine
  * route_popup_data()   -- shape a route for Trade Hub's pinned popup
  * find_market_price()  -- UEX item prices at every terminal

Skill modules are imported lazily with a one-time sys.path insertion, so
this module loads fine even if a skill folder is missing — the tools just
return a clean error for the LLM to relay.
"""
from __future__ import annotations

import logging
import os
import sys

from .tools import ToolError

log = logging.getLogger(__name__)

_imports_done = False
_uex_client_mod = None
_route_engine = None
_ships = None


def _skill_dir(base_dir: str, folder: str) -> str:
    return os.path.join(base_dir, "skills", folder)


def _wire_imports(base_dir: str) -> None:
    """One-time lazy import of Trade Hub's data layer and shared.ships."""
    global _imports_done, _uex_client_mod, _route_engine, _ships
    if _imports_done:
        return
    _imports_done = True

    try:
        from shared import ships as ships_mod
        _ships = ships_mod
    except Exception as exc:                              # noqa: BLE001
        log.warning("headless: shared.ships unavailable: %s", exc)

    th = _skill_dir(base_dir, "Trade_Hub")
    if os.path.isdir(th) and th not in sys.path:
        sys.path.insert(0, th)
    try:
        import uex_client as uc
        _uex_client_mod = uc
    except Exception as exc:                              # noqa: BLE001
        log.warning("headless: Trade Hub uex_client unavailable: %s", exc)
        return
    try:
        import route_engine as re_mod
        _route_engine = re_mod
    except Exception as exc:                              # noqa: BLE001
        log.warning("headless: Trade Hub route_engine unavailable: %s", exc)


def ship_info(base_dir: str, name: str) -> dict:
    """Cargo capacity and preset info for a ship (fuzzy name match)."""
    _wire_imports(base_dir)
    if _ships is None:
        raise ToolError("ship database unavailable")
    presets = getattr(_ships, "SHIP_PRESETS", {})
    needle = (name or "").strip().lower()
    if not needle:
        raise ToolError("say a ship name, e.g. Caterpillar")
    for key, scu in presets.items():
        if needle in key.lower() or key.lower() in needle:
            return {"ship": key, "scu": scu}
    raise ToolError(f"ship '{name}' not found — try another name")


def ship_scu(base_dir: str, name: str) -> int:
    """Best-effort SCU lookup; 0 when unknown (ranking still works)."""
    try:
        return int(ship_info(base_dir, name).get("scu", 0))
    except ToolError:
        return 0


def find_trade_routes(base_dir: str, ship: str = "", commodity: str = "",
                      system: str = "", top_n: int = 5,
                      allow_illegal: bool = True) -> list:
    """Top trade routes ranked by estimated profit for the ship."""
    _wire_imports(base_dir)
    if _uex_client_mod is None or _route_engine is None:
        raise ToolError("Trade Hub data layer unavailable")

    scu = ship_scu(base_dir, ship) if ship else 0
    client = _uex_client_mod.UEXClient()
    try:
        routes = client.get_routes()
    except Exception as exc:                              # noqa: BLE001
        raise ToolError(f"UEX route fetch failed: {exc}") from exc
    if not routes:
        raise ToolError("UEX returned no routes — try again in a moment")

    fs = _route_engine.FilterState(
        system=system or "",
        commodity=commodity or "",
        allow_illegal=allow_illegal,
    )
    filtered = _route_engine.apply_filters(routes, fs)
    ranked = _route_engine.top_routes(filtered, ship_scu=scu, n=max(1, min(top_n, 20)))
    ship_lbl = ship if ship else "any ship"

    out = []
    for r in ranked:
        eff = r.effective_scu(scu)
        out.append({
            "commodity": r.commodity,
            "buy_system": r.buy_system,
            "buy_location": r.buy_location,
            "buy_terminal": r.buy_terminal,
            "sell_system": r.sell_system,
            "sell_location": r.sell_location,
            "sell_terminal": r.sell_terminal,
            "price_buy": r.price_buy,
            "price_sell": r.price_sell,
            "scu_available": r.scu_available,
            "scu_demand": r.scu_demand,
            "effective_scu": eff,
            "margin_per_scu": r.margin,
            "est_profit": r.estimated_profit(scu),
            "investment": r.investment(scu),
            "illegal": bool(getattr(r, "is_illegal", False)),
            "ship": ship_lbl,
        })
    return out


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
        "distance": 0,   # terminal-ID telemetry not available headless
    }


def find_market_price(base_dir: str, item_name: str, top_n: int = 5) -> dict:
    """Where to buy an item, from UEX items + items_prices."""
    from shared.api_config import UEX_BASE_URL, UEX_HEADERS, UEX_TIMEOUT
    from shared.http_client import HttpClient

    needle = (item_name or "").strip().lower()
    if not needle:
        raise ToolError("say an item name, e.g. P4-AR rifle")
    http = HttpClient(UEX_BASE_URL, headers=UEX_HEADERS, timeout=UEX_TIMEOUT)

    # Resolve item id by name (UEX /items is a flat list).
    result = http.get_json("/items")
    if not result.ok:
        raise ToolError("UEX items fetch failed: {0}".format(result.error))
    payload = result.data
    items = payload if isinstance(payload, list) else (payload or {}).get("data", [])
    match = None
    for it in items:
        nm = (it.get("name") or "").lower()
        if needle in nm:
            match = it
            if nm == needle:
                break
    if match is None:
        raise ToolError(f"item '{item_name}' not found on UEX")
    item_id = match.get("id")
    if not item_id:
        raise ToolError(f"UEX item '{item_name}' has no id")

    price_result = http.get_json("items_prices?id_item={0}".format(item_id))
    if not price_result.ok:
        raise ToolError("UEX price fetch failed: {0}".format(price_result.error))
    ppayload = price_result.data
    rows = ppayload if isinstance(ppayload, list) else (ppayload or {}).get("data", [])

    def _row(r: dict) -> dict:
        return {
            "terminal": r.get("terminal_name") or r.get("location_name") or "?",
            "price": r.get("price") or r.get("price_buy") or 0,
            "stock": r.get("stock") or r.get("inventory") or 0,
        }

    buys = [_row(r) for r in rows if (r.get("price_buy") or r.get("price"))]
    buys.sort(key=lambda x: (x["price"] or 0))
    return {
        "item": match.get("name"),
        "cheapest": buys[:max(1, min(top_n, 10))],
    }
