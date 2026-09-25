"""Trade Hub worker: routes from Trade Hub's own working fetch path.

Uses trade_hub_data.DataFetcher._fetch_api (commodities_prices_all +
terminals + commodities), the path the Trade Hub window runs today. NOT
uex_client's /commodities_routes, which UEX now answers with HTTP 400,
and NOT the WingmanAI uexcorp.db, which is a stale March snapshot.

Read-only: the distance crawl (thousands of /terminals_distances calls)
and the DistanceCache write are both disabled in this process. Distances
already in the user's cache are still applied.
"""
from __future__ import annotations

import time

import _assist_logic as L

import trade_hub_data as T
from shared.ships import SHIP_PRESETS, scu_for_ship

# no crawl, no write — headless calls must not hammer UEX or touch disk
T._dist_cache.fetch_missing = lambda *a, **k: None
T._dist_cache._save = lambda *a, **k: None

_ROUTES_TTL = 300.0
_state = {"routes": None, "ts": 0.0}


def _routes() -> list:
    now = time.time()
    if _state["routes"] is None or now - _state["ts"] > _ROUTES_TTL:
        try:
            routes = T.DataFetcher._fetch_api()
        except Exception as exc:                            # noqa: BLE001
            if _state["routes"]:
                return _state["routes"]   # stale beats nothing; age reported
            raise L.ToolFail(f"UEX price fetch failed: {exc}")
        if not routes:
            raise L.ToolFail("UEX returned no prices, try again in a moment")
        _state["routes"], _state["ts"] = routes, now
    return _state["routes"]


def _ship(ship: str):
    if not ship:
        return "", 0
    scu = scu_for_ship(ship)
    if scu:
        best, _ = L.resolve_one(ship, SHIP_PRESETS.keys(), what="ship", min_score=0.5)
        return best, scu
    near = [n for _, n in L.best_matches(ship, SHIP_PRESETS.keys(), limit=3, min_score=0.4)]
    raise L.ToolFail(f"I don't know the cargo size of '{ship}'"
                     + (f" (closest: {', '.join(near)})" if near else ""))


def find_trade_routes(ship: str = "", commodity: str = "", system: str = "",
                      top_n: int = 5, allow_illegal: bool = True) -> dict:
    ship_key, scu = _ship(ship)
    routes = _routes()
    comm = ""
    comm_alts: list = []
    if commodity:
        comm, comm_alts = L.resolve_one(commodity, T.get_unique_commodities(routes),
                                        what="traded commodity", min_score=0.55)
    fs = T.FilterState(system=system or "", allow_illegal=bool(allow_illegal))
    filtered = T.apply_filters(routes, fs)
    if comm:
        filtered = [r for r in filtered if r.commodity == comm]
    ranked = sorted(filtered, key=lambda r: T.calc_profit(r, scu), reverse=True)
    n = max(1, min(int(top_n or 5), 20))
    out = []
    for r in ranked[:n]:
        eff = r.effective_scu(scu)
        out.append({
            "commodity": r.commodity,
            "buy_terminal": r.buy_terminal, "buy_location": r.buy_location,
            "buy_system": r.buy_system,
            "sell_terminal": r.sell_terminal, "sell_location": r.sell_location,
            "sell_system": r.sell_system,
            "price_buy": r.price_buy, "price_sell": r.price_sell,
            "margin_per_scu": r.margin,
            "scu_available": r.scu_available, "scu_demand": r.scu_demand,
            "effective_scu": eff,
            "est_profit": round(T.calc_profit(r, scu)),
            "investment": round(r.price_buy * eff),
            "distance_gm": round(r.distance, 1) if r.distance else None,
            "illegal": bool(r.is_illegal),
        })
    res = {
        "ship": ship_key or "any ship", "ship_scu": scu,
        "commodity": comm or None, "system": system or None,
        "routes": out,
        "data_age_s": round(time.time() - _state["ts"]),
        "note": ("est_profit = effective SCU x margin; effective SCU is capped by "
                 "ship size, stock at the buy terminal and demand at the sell terminal. "
                 "Trade Hub keeps the 5000 most profitable pairs."),
    }
    if comm_alts:
        res["commodity_alternatives"] = comm_alts
    if not out:
        res["empty"] = True
        res["note"] = "no profitable route matched those filters"
    return res


EXPORTS = {"find_trade_routes": find_trade_routes}
