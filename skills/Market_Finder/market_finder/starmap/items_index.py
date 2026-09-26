"""Terminal -> items index for the Market Finder star map.

Answers "what items sell at this location?" by indexing UEX items_prices_all
(one bulk payload, disk-cached exactly like the Trade Hub star map caches
commodities_prices_all) and joining each price row back to the terminal
list Market Finder already loads.

Two resilience layers, both deliberate:
  * offline / endpoint missing -> fall back to the per-item price rows already
    fetched this session (DataService price cache + grocery cards), so the map
    still shows *something* instead of an empty dialog;
  * every network read happens on a worker thread; the UI gets a Qt signal.

The index is keyed by (system, body) normalised names so a map click on a
planet, moon, city or station body resolves straight to its terminals.

Price rows from items_prices_all carry NO location names of their own --
only id_terminal -- so the build fetches the UEX terminals list once and
takes the system/body names from each rows terminal record, indexing the
row under every name the terminal is known by (_terminal_name_keys).
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from ..api_client import UexApiClient
from .data import norm_loc

_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "market_finder", "uex_cache")
_CACHE_PATH = os.path.join(_CACHE_DIR, "items_prices_all.json")
_CACHE_TTL = 1800.0          # 30 min fresh; stale cache used as offline fallback
_STALE_TTL = 7 * 86400.0     # after a week the price data does more harm than good

# Key: (normalised system, normalised body) -> list of {item_id, price_buy}
IndexKey = Tuple[str, str]


def _load_disk_cache() -> Optional[List[dict]]:
    try:
        if not os.path.isfile(_CACHE_PATH):
            return None
        age = time.time() - os.path.getmtime(_CACHE_PATH)
        if age > _STALE_TTL:
            return None
        with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


def _save_disk_cache(rows: List[dict]) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(rows, fh)
        os.replace(tmp, _CACHE_PATH)
    except OSError:
        pass


def _terminal_name_keys(t: dict) -> List[str]:
    """Every name a terminal might be known by on the map, normalised."""
    keys = []
    for f in ("city_name", "space_station_name", "moon_name", "planet_name",
              "orbit_name", "nickname", "displayname", "name", "fullname"):
        v = t.get(f)
        if v:
            n = norm_loc(str(v))
            if n and n not in keys:
                keys.append(n)
    return keys


def _lookup_terminal(terminals: Dict[int, dict], tid) -> Optional[dict]:
    """Terminal record for a price row id_terminal.

    Tolerates str/int key mismatch between the price rows and the terminal
    map (JSON caches key everything as str).
    """
    try:
        t = terminals.get(tid)
        if t is not None:
            return t
        if tid is not None:
            return terminals.get(int(tid))
    except (TypeError, ValueError):
        return None
    return None


def _add_price_row(index: Dict[IndexKey, List[dict]],
                   terminals: Dict[int, dict], r: dict, item_id: int) -> None:
    """Index one price row under every map body its terminal is known by.

    Shared by the bulk items_prices_all build and the per-item session
    fallback (merge_session_prices): price rows only carry id_terminal,
    so the system/body names come from the terminal record. With no
    terminal record, fall back to whatever names the row carries.
    """
    price = float(r.get("price_buy") or 0)
    t = _lookup_terminal(terminals, r.get("id_terminal"))
    if t is None:
        sys_n = norm_loc(str(r.get("star_system_name") or ""))
        body_n = norm_loc(str(r.get("space_station_name")
                              or r.get("city_name")
                              or r.get("moon_name")
                              or r.get("planet_name") or ""))
        if sys_n and body_n:
            _add(index, sys_n, body_n, item_id, price)
        return
    sys_n = norm_loc(str(t.get("star_system_name") or ""))
    if not sys_n:
        return
    for key in _terminal_name_keys(t):
        _add(index, sys_n, key, item_id, price)


class ItemsIndexLoader(QObject):
    """Builds the (system, body) -> items index off-thread.

    Emits done(index, source) where source is one of
    "live", "cache" or "session" (prices seen this session only).
    """

    done = Signal(object, str)

    def __init__(self, api: Optional[UexApiClient] = None,
                 parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._api = api or UexApiClient()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True, name="ItemsIndexBuild").start()

    def _load_terminals(self) -> Dict[int, dict]:
        """UEX terminals list keyed by id -- the join key for price rows."""
        terminals: Dict[int, dict] = {}
        try:
            res = self._api.get("terminals")
            if res.ok and res.data:
                for t in res.data:
                    tid = t.get("id")
                    if tid is not None:
                        terminals[int(tid)] = t
        except Exception:
            pass
        return terminals

    def _run(self) -> None:
        rows: Optional[List[dict]] = None
        source = "session"
        try:
            res = self._api.get("items_prices_all")
            if res.ok and res.data:
                rows = res.data
                source = "live"
                _save_disk_cache(rows)
        except Exception:
            rows = None
        if rows is None:
            cached = _load_disk_cache()
            if cached:
                rows = cached
                source = "cache"
        index: Dict[IndexKey, List[dict]] = {}
        if rows:
            terminals = self._load_terminals()
            for r in rows:
                try:
                    item_id = r.get("id_item")
                    if item_id is None:
                        continue
                    _add_price_row(index, terminals, r, int(item_id))
                except (TypeError, ValueError):
                    continue
        self.done.emit(index, source)


def merge_session_prices(index: Dict[IndexKey, List[dict]],
                         terminals: Dict[int, dict],
                         session_prices: Dict[int, List[dict]]) -> None:
    """Fold per-item prices already fetched this session into the index
    (offline fallback), matching each price row to its terminal bodies."""
    for _item_id, rows in (session_prices or {}).items():
        try:
            item_id = int(_item_id)
        except (TypeError, ValueError):
            continue
        for r in rows or []:
            try:
                _add_price_row(index, terminals, r, item_id)
            except (TypeError, ValueError):
                continue


def _add(index: Dict[IndexKey, List[dict]], sys_n: str, body_n: str,
         item_id: int, price: float) -> None:
    bucket = index.setdefault((sys_n, body_n), [])
    for e in bucket:
        if e["item_id"] == item_id:
            if price > 0 and (not e["price_buy"] or price < e["price_buy"]):
                e["price_buy"] = price
            return
    bucket.append({"item_id": item_id, "price_buy": price})


def items_at(index: Dict[IndexKey, List[dict]], body: str, system: str) -> List[dict]:
    """All known items sold at a map body, cheapest first."""
    sys_n = norm_loc(system or "")
    body_n = norm_loc(body or "")
    if not sys_n or not body_n:
        return []
    rows = list(index.get((sys_n, body_n), []))
    rows.sort(key=lambda e: (e["price_buy"] <= 0, e["price_buy"] or 1e18))
    return rows
