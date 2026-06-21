"""Minimal UEX API client for the star map's commodity detail page.

Fetches /commodities, /commodities_prices and /commodities_prices_history with a
small on-disk cache. Network calls block, so the UI uses CommodityFetcher (runs
the page build in a worker thread, emits a Qt signal). Everything degrades to
empty results on error — the page shows 'no data' rather than crashing.
"""
from __future__ import annotations

import datetime
import json
import os
import threading
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from PySide6.QtCore import QObject, Signal

_BASE = "https://api.uexcorp.space/2.0"
_UA = "WingmanAI-TradeHub-StarMap/1.0"
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "trade_hub", "uex_cache")


def _cache_path(key: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in key)[:120]
    return os.path.join(_CACHE_DIR, safe + ".json")


def _get(endpoint: str, params: Optional[dict] = None, ttl: float = 600.0) -> list:
    url = _BASE + "/" + endpoint
    if params:
        url += "?" + urllib.parse.urlencode(params)
    cp = _cache_path(endpoint + (json.dumps(params, sort_keys=True) if params else ""))
    try:
        if os.path.isfile(cp) and (time.time() - os.path.getmtime(cp)) < ttl:
            with open(cp, "r", encoding="utf-8") as f:
                return json.load(f)
    except (OSError, json.JSONDecodeError):
        pass
    try:
        req = urllib.request.Request(url, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            payload = json.load(r)
        data = payload.get("data")
        if not isinstance(data, list):
            data = [] if data is None else [data]
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, cp)
        return data
    except Exception:
        try:
            with open(cp, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return []


def commodities() -> List[dict]:
    return _get("commodities", ttl=86400) or []


def find_commodity(name: str) -> Optional[dict]:
    n = (name or "").strip().lower()
    partial = None
    for c in commodities():
        if (c.get("name", "").lower() == n or c.get("slug", "").lower() == n
                or c.get("code", "").lower() == n):
            return c
        if partial is None and n and n in c.get("name", "").lower():
            partial = c
    return partial


def prices_for_commodity(cid: int) -> List[dict]:
    return _get("commodities_prices", {"id_commodity": cid}, ttl=600) or []


def prices_all() -> List[dict]:
    """Every terminal's current buy/sell price (for the local history logger)."""
    return _get("commodities_prices_all", ttl=1800) or []


def history(cid: int, tid: int) -> List[dict]:
    return _get("commodities_prices_history",
                {"id_commodity": cid, "id_terminal": tid}, ttl=3600) or []


# ── page assembly ───────────────────────────────────────────────────────────

def _term_name(r: dict) -> str:
    return (r.get("terminal_name") or r.get("space_station_name")
            or r.get("outpost_name") or r.get("city_name") or "?")


def _sell_scu(r: dict) -> float:
    return r.get("scu_sell_stock") or r.get("scu_sell") or 0


def _avg(rows: list, key: str) -> float:
    vals = [r.get(key) or 0 for r in rows if (r.get(key) or 0) > 0]
    return sum(vals) / len(vals) if vals else 0.0


def _aggregate_history(cid: int, prices: list, max_terminals: int = 40) -> dict:
    # UEX history is sparse and unevenly spread across terminals (busy terminals
    # can lag a reset while a few quiet ones hold the only rows), so sample EVERY
    # terminal that trades this commodity — capped — rather than just the top few
    # by stock, so no available snapshot is missed. Still ordered by stock so the
    # cap (rare high-terminal-count commodities) keeps the most relevant ones.
    tids: List[int] = []
    for r in sorted(prices, key=lambda r: (r.get("scu_buy") or 0) + _sell_scu(r), reverse=True):
        tid = r.get("id_terminal")
        if tid and tid not in tids:
            tids.append(tid)
        if len(tids) >= max_terminals:
            break
    snaps: List[dict] = []
    if tids:
        try:
            with ThreadPoolExecutor(max_workers=10) as ex:
                for rows in ex.map(lambda t: history(cid, t), tids):
                    snaps.extend(rows or [])
        except Exception:
            for t in tids:
                snaps.extend(history(cid, t) or [])
    # Merge the toolbox's own locally-logged daily snapshots so trends build up
    # from our observations even while UEX's history feature is sparse. UEX snaps
    # also feed the Datarunner (reports) count; local snaps feed price trends only.
    local: List[dict] = []
    try:
        from .price_log import history_for_commodity
        local = history_for_commodity(cid)
    except Exception:
        local = []

    if not snaps and not local:
        return {"buy": [], "sell": [], "profit": [], "reports": []}

    buyd, selld, monthc = defaultdict(list), defaultdict(list), defaultdict(int)
    for s in snaps:
        da = s.get("date_added")
        if not da:
            continue
        day = (int(da) // 86400) * 86400
        if s.get("price_buy"):
            buyd[day].append(s["price_buy"])
        if s.get("price_sell"):
            selld[day].append(s["price_sell"])
        try:
            d = datetime.date.fromtimestamp(da)
            monthc[(d.year, d.month)] += 1
        except (OSError, ValueError, OverflowError):
            pass
    for s in local:                       # our daily snapshots — price trends only
        day = (int(s["date_added"]) // 86400) * 86400
        if s.get("price_buy"):
            buyd[day].append(s["price_buy"])
        if s.get("price_sell"):
            selld[day].append(s["price_sell"])
    buy = sorted((d, sum(v) / len(v)) for d, v in buyd.items())
    sell = sorted((d, sum(v) / len(v)) for d, v in selld.items())
    bd = dict(buy)
    profit = sorted((d, sv - bd[d]) for d, sv in sell if d in bd)
    reports = [(datetime.date(y, m, 1).strftime("%b %y"), cnt)
               for (y, m), cnt in sorted(monthc.items())]
    return {"buy": buy, "sell": sell, "profit": profit, "reports": reports}


def build_commodity_page(name: str) -> Optional[dict]:
    c = find_commodity(name)
    if not c:
        return None
    cid = c.get("id")
    prices = prices_for_commodity(cid) if cid else []
    buy_rows = [r for r in prices if (r.get("price_buy") or 0) > 0]
    sell_rows = [r for r in prices if (r.get("price_sell") or 0) > 0]

    best: dict = {}
    if buy_rows:
        bs = max(buy_rows, key=lambda r: r.get("scu_buy") or 0)
        bp = min(buy_rows, key=lambda r: r.get("price_buy") or 1e18)
        best["buy_stock"] = (bs.get("scu_buy") or 0, _term_name(bs))
        best["buy_price"] = (bp.get("price_buy") or 0, _term_name(bp))
    if sell_rows:
        ss = max(sell_rows, key=_sell_scu)
        sp = max(sell_rows, key=lambda r: r.get("price_sell") or 0)
        best["sell_stock"] = (_sell_scu(ss), _term_name(ss))
        best["sell_price"] = (sp.get("price_sell") or 0, _term_name(sp))

    avgs = {
        # Price avgs use the commodity-level reference (matches the UEX cards);
        # SCU avgs are aggregated from the per-terminal _avg fields.
        "buy_price": (c.get("price_buy") or 0) or _avg(buy_rows, "price_buy_avg"),
        "sell_price": (c.get("price_sell") or 0) or _avg(sell_rows, "price_sell_avg"),
        "buy_scu": _avg(buy_rows, "scu_buy_avg") or _avg(buy_rows, "scu_buy"),
        "sell_scu": _avg(sell_rows, "scu_sell_stock_avg") or _avg(sell_rows, "scu_sell_stock"),
    }

    locations = sorted(
        ({"terminal": _term_name(r), "system": r.get("star_system_name", ""),
          "price_buy": r.get("price_buy") or 0, "price_sell": r.get("price_sell") or 0,
          "scu_buy": r.get("scu_buy") or 0, "scu_sell": _sell_scu(r)} for r in prices),
        key=lambda l: -l["price_sell"])

    return {
        "commodity": c,
        "best": best,
        "avgs": avgs,
        "locations": locations,
        "history": _aggregate_history(cid, prices) if cid else {},
    }


class CommodityFetcher(QObject):
    """Builds the commodity page in a worker thread, emits ``done(page|None)``."""

    done = Signal(object)

    def fetch(self, name: str) -> None:
        def run() -> None:
            try:
                data = build_commodity_page(name)
            except Exception:
                data = None
            self.done.emit(data)
        threading.Thread(target=run, daemon=True).start()
