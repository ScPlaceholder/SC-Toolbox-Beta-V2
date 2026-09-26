"""Terminal -> items index for the Starmap tool (ported from Market Finder).

Answers "what items sell at this location?" by indexing UEX ``items_prices_all``
(one bulk payload, disk-cached exactly like the commodity side caches
``commodities_prices_all``) and joining each price row back to the ``items``
metadata endpoint so the index is self-contained: every entry carries the
item's name and category, no external item list needed.

Two resilience layers, both deliberate:
  * offline / endpoint missing -> the index simply comes back empty and the
    location dialog says "no item data" instead of crashing;
  * every network read happens on a worker thread; the UI gets a Qt signal.

Keyed by (system, body) normalised names so a map click on a planet, moon,
city or station body resolves straight to its terminals.

``items_prices_all`` rows carry only ``id_terminal`` and ``terminal_name``
(no system, no station or city), so each row is joined to the UEX
``terminals`` list by id first, the same terminal -> location lookup
Market Finder does (service.py ``term_map``, ui/widgets.py). Without that
join every row was skipped and the index came back empty.
"""
from __future__ import annotations

import threading
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, Signal

from .data import norm_loc

IndexKey = Tuple[str, str]


class ItemsIndexLoader(QObject):
    """Builds the (system, body) -> items index off-thread.

    Emits ``done(index, source)`` where source is one of
    ``"live"``, ``"cache"`` or ``"offline"``.
    """

    done = Signal(object, str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True, name="ItemsIndexBuild").start()

    def _run(self) -> None:
        from . import uex_api
        index: Dict[IndexKey, List[dict]] = {}
        source = "offline"
        try:
            res = uex_api.get("items_prices_all")
            if res.ok and res.data:
                source = "live"
            elif res.data:
                source = "cache"
            meta: Dict[int, dict] = {}
            try:
                mres = uex_api.get("items")
                if mres.ok:
                    meta = {int(i["id"]): i
                            for i in mres.data if i.get("id") is not None}
            except Exception:
                meta = {}
            terminals: Dict[int, dict] = {}
            try:
                tres = uex_api.get("terminals")
                terminals = {int(t["id"]): t for t in (tres.data or [])
                             if t.get("id") is not None}
            except Exception:
                terminals = {}
            index = build_index(res.data, terminals, meta)
        except Exception:
            # Still degrade to an empty index (the dialog says "no item data"), but say why.
            import logging as _lg
            _lg.getLogger(__name__).warning("item price index build failed; showing no item data",
                                            exc_info=True)
        self.done.emit(index, source)


# where a terminal is, most specific first; each becomes an index key
_PLACE_FIELDS = ("space_station_name", "city_name", "outpost_name", "terminal_name")


def build_index(price_rows: List[dict], terminals: Dict[int, dict],
                meta: Optional[Dict[int, dict]] = None) -> Dict[IndexKey, List[dict]]:
    """(system, place) -> items, from items_prices_all joined to terminals.

    A row is filed under its terminal's station, city or outpost name and
    under the terminal name, so a map body ("Area 18", "Port Tressler")
    and a terminal name both resolve. A row whose terminal is unknown is
    kept only if the row itself names a system.
    """
    meta = meta or {}
    index: Dict[IndexKey, List[dict]] = {}
    for r in price_rows or []:
        try:
            item_id = r.get("id_item")
            if item_id is None:
                continue
            term = terminals.get(int(r.get("id_terminal") or 0)) or {}
            sys_n = norm_loc(str(term.get("star_system_name")
                                 or r.get("star_system_name") or ""))
            if not sys_n:
                continue
            places = []
            for fld in _PLACE_FIELDS:
                v = norm_loc(str(term.get(fld) or r.get(fld) or ""))
                if v and v not in places:
                    places.append(v)
            if not places:
                continue
            m = meta.get(int(item_id)) or {}
            entry = {
                "item_id": int(item_id),
                "price_buy": float(r.get("price_buy") or 0),
                "name": m.get("name") or r.get("item_name") or "Item #%s" % item_id,
                "category": m.get("category") or m.get("section") or "",
            }
            for place in places:
                index.setdefault((sys_n, place), []).append(entry)
        except (TypeError, ValueError):
            continue
    return index


def index_counts(index: Dict[IndexKey, List[dict]]) -> Tuple[int, int]:
    """(distinct items, places) in an index, for the status line."""
    items = {e["item_id"] for rows in (index or {}).values() for e in rows}
    return len(items), len(index or {})


def items_at(index: Dict[IndexKey, List[dict]], body: str, system: str) -> List[dict]:
    """All known items sold at a map body, cheapest first."""
    sys_n = norm_loc(system or "")
    body_n = norm_loc(body or "")
    if not sys_n or not body_n:
        return []
    rows = list(index.get((sys_n, body_n), []))
    # Lenient retry: UEX terminal names sometimes differ from the map body
    # names beyond formatting, so also accept guarded substring matches of
    # the normalised names within this system.
    if not rows:
        for (sys_k, body_key), cand in index.items():
            if sys_k != sys_n or not body_key:
                continue
            if body_n in body_key or (len(body_key) >= 4 and body_key in body_n):
                merged = {e["item_id"]: e for e in rows}
                for e in cand:
                    cur = merged.get(e["item_id"])
                    if cur is None or (0 < e["price_buy"] < (cur["price_buy"] or 1e18)):
                        merged[e["item_id"]] = dict(e)
                rows = list(merged.values())
    # one row per item: a city or station has several shops selling the
    # same thing, and the card lists items, so keep the cheapest
    best: Dict[int, dict] = {}
    for e in rows:
        cur = best.get(e["item_id"])
        if cur is None or (0 < e["price_buy"] < (cur["price_buy"] or 1e18)):
            best[e["item_id"]] = e
    rows = list(best.values())
    rows.sort(key=lambda e: (e["price_buy"] <= 0, e["price_buy"] or 1e18,
                             str(e.get("name") or "")))
    return rows
