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
            for r in res.data:
                try:
                    item_id = r.get("id_item")
                    if item_id is None:
                        continue
                    price = float(r.get("price_buy") or 0)
                    sys_n = norm_loc(str(r.get("star_system_name") or ""))
                    term_n = norm_loc(str(r.get("terminal_name")
                                          or r.get("space_station_name")
                                          or r.get("city_name") or ""))
                    if not sys_n or not term_n:
                        continue
                    m = meta.get(int(item_id)) or {}
                    index.setdefault((sys_n, term_n), []).append({
                        "item_id": int(item_id),
                        "price_buy": price,
                        "name": m.get("name") or "Item #%s" % item_id,
                        "category": m.get("category") or m.get("section") or "",
                    })
                except (TypeError, ValueError):
                    continue
        except Exception:
            pass
        self.done.emit(index, source)


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
    rows.sort(key=lambda e: (e["price_buy"] <= 0, e["price_buy"] or 1e18,
                             str(e.get("name") or "")))
    return rows
