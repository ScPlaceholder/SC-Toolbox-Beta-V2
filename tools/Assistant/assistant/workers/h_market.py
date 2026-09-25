"""Market Finder worker: item prices and ship buy/rent, via its DataService.

DataService fetches items per category (items?id_category=...), which is
the path that works; bare /items now returns HTTP 400. Its disk cache is
READ when fresh and never written from here.
"""
from __future__ import annotations

import time

import _assist_logic as L

from market_finder.service import DataService

_svc = DataService()
_svc._cache.save = lambda *a, **k: None       # never write the tool's cache
_svc._cache.delete = lambda *a, **k: None
_state = {"ts": 0.0}
_TTL = 1800.0


def _loaded() -> DataService:
    if not _svc.is_loaded or time.time() - _state["ts"] > _TTL:
        _svc.fetch_all(force=_state["ts"] > 0)   # first time: disk cache if fresh
        if not _svc.is_loaded or not _svc.items:
            raise L.ToolFail("Market data could not be loaded: "
                             + (_svc.get_error() or "no items returned"))
        _state["ts"] = time.time()
    return _svc


def _price_rows(rows, field):
    return [r for r in (rows or []) if (r.get(field) or 0) > 0]


def _term(r: dict) -> dict:
    system = r.get("star_system_name") or ""
    if not system:   # vehicle price rows carry only the terminal id
        tid = r.get("id_terminal")
        t = _svc.terminals.get(tid) or _svc.terminals.get(str(tid)) or {}
        system = t.get("star_system_name") or ""
    return {"terminal": r.get("terminal_name") or r.get("terminal") or "?", "system": system}


def find_item_price(item: str, top_n: int = 5) -> dict:
    svc = _loaded()
    best, alts = L.resolve_one(item, svc.items, key=lambda i: i.get("name"),
                               what="item on UEX")
    r = svc.fetch_item_prices(best["id"])
    if not r.ok:
        raise L.ToolFail(f"UEX price fetch failed for {best.get('name')}: {r.error}")
    n = max(1, min(int(top_n or 5), 15))
    buys = sorted(_price_rows(r.data, "price_buy"), key=lambda x: x["price_buy"])
    sells = sorted(_price_rows(r.data, "price_sell"), key=lambda x: -x["price_sell"])
    res = {
        "item": best.get("name"),
        "category": best.get("category"),
        "manufacturer": best.get("company_name"),
        "cheapest_buy": [dict(_term(x), price=x["price_buy"]) for x in buys[:n]],
        "best_sell": [dict(_term(x), price=x["price_sell"]) for x in sells[:3]],
        "terminals_listed": len(r.data or []),
        "other_matches": alts,
    }
    if not buys and not sells:
        res["empty"] = True
        res["note"] = "UEX lists this item but no terminal currently reports a price"
    return res


def ship_buy_rent(ship: str) -> dict:
    svc = _loaded()
    best, alts = L.resolve_one(ship, svc.vehicles,
                               key=lambda v: v.get("name_full") or v.get("name"),
                               what="ship on UEX")
    vid = best.get("id")
    buys = sorted(_price_rows(svc.purchase_by_vehicle.get(vid, []), "price_buy"),
                  key=lambda x: x["price_buy"])
    rents = sorted(_price_rows(svc.rental_by_vehicle.get(vid, []), "price_rent"),
                   key=lambda x: x["price_rent"])
    res = {
        "ship": best.get("name_full") or best.get("name"),
        "buy": [dict(_term(x), price=x["price_buy"]) for x in buys[:6]],
        "rent_per_day": [dict(_term(x), price=x["price_rent"]) for x in rents[:6]],
        "other_matches": alts,
    }
    if not buys and not rents:
        res["empty"] = True
        res["note"] = ("no in-game terminal sells or rents this ship right now "
                       "(pledge-store / not purchasable in the verse)")
    return res


EXPORTS = {"find_item_price": find_item_price, "ship_buy_rent": ship_buy_rent}
