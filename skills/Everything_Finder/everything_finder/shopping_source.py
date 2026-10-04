# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""Where the shopping list gets its data, preferring what an open tab already has.

* Items: Item Finder's ``DataService``. If the Item Finder tab is open, its
  live service is reused; otherwise one is created and loaded the way Item
  Finder loads (disk cache first, then UEX).
* Commodities: Trade Hub ``Route`` objects. If the Trade Hub tab is open, its
  ``_all_routes`` are reused; otherwise Trade Hub's own ``DataFetcher`` fetches
  them - the same fetch Trade Hub does when it opens, including its background
  distance warm-up into the shared distance cache.
* Distances: Trade Hub's singleton ``DistanceCache`` (shared with Trade Hub).

Every method that can touch the network is BLOCKING and meant for a worker
thread; the pop-out never calls them on the UI thread.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .shopping_list import Entry

log = logging.getLogger(__name__)


class ShoppingSource:
    def __init__(self,
                 item_service_getter: Callable[[], Any] = lambda: None,
                 routes_getter: Callable[[], List[Any]] = lambda: []) -> None:
        self._item_service_getter = item_service_getter
        self._routes_getter = routes_getter
        self._own_service = None
        self._own_routes: Optional[List[Any]] = None
        self._lock = threading.Lock()

    # items
    def item_service(self):
        svc = self._item_service_getter()
        if svc is not None:
            return svc
        with self._lock:
            if self._own_service is None:
                from .tool_loader import ensure_item_finder_path
                ensure_item_finder_path()
                from market_finder.service import DataService
                self._own_service = DataService()
            svc = self._own_service
        if not svc.is_loaded():
            svc.fetch_all()
        return svc

    def item_catalog(self) -> Dict[str, int]:
        """Item name -> UEX id_item."""
        svc = self.item_service()
        out: Dict[str, int] = {}
        for it in list(getattr(svc, "items", None) or []):
            name = str(it.get("name") or "").strip()
            iid = it.get("id")
            if name and iid is not None and name not in out:
                out[name] = int(iid)
        return out

    def item_prices(self, entries: Sequence[Entry], catalog: Optional[Dict[str, int]] = None
                    ) -> Dict[str, List[dict]]:
        """Item name -> raw UEX price rows, for the item entries."""
        items = [e for e in entries if e.kind == "item"]
        if not items:
            return {}
        svc = self.item_service()
        cat = catalog if catalog is not None else None
        out: Dict[str, List[dict]] = {}
        for e in items:
            iid = e.item_id
            if iid is None:
                if cat is None:
                    cat = self.item_catalog()
                iid = cat.get(e.name)
            if iid is None:
                out[e.name] = []
                continue
            rows: List[dict] = []
            for _attempt in range(10):           # another thread may be fetching this id
                res = svc.fetch_item_prices(int(iid))
                if getattr(res, "ok", False):
                    rows = list(res.data or [])
                    break
                if getattr(res, "error_type", "") != "in_progress":
                    break
                time.sleep(0.3)
            out[e.name] = rows
        return out

    # commodities
    def routes(self) -> List[Any]:
        live = list(self._routes_getter() or [])
        if live:
            return live
        with self._lock:
            if self._own_routes is not None:
                return list(self._own_routes)
        from .tool_loader import ensure_trade_hub_path
        ensure_trade_hub_path()
        from trade_hub_data import DataFetcher
        try:
            routes = DataFetcher._fetch_api(on_distances_done=lambda _r: None)
        except Exception as exc:
            log.warning("Everything Finder: Trade Hub route fetch failed: %s", exc)
            routes = []
        with self._lock:
            self._own_routes = list(routes)
        return list(routes)

    def commodity_names(self) -> List[str]:
        from .tool_loader import ensure_trade_hub_path
        ensure_trade_hub_path()
        from trade_hub_data import get_unique_commodities
        return list(get_unique_commodities(self.routes()))

    def dist_cache(self):
        from .tool_loader import ensure_trade_hub_path
        ensure_trade_hub_path()
        from trade_hub_data import _dist_cache
        return _dist_cache

    def start_terminals(self) -> Dict[str, int]:
        """Terminal name -> UEX id, from whatever is already loaded (never fetches)."""
        out: Dict[str, int] = {}
        svc = self._item_service_getter() or self._own_service
        if svc is not None:
            for tid, t in dict(getattr(svc, "terminals", None) or {}).items():
                name = str(t.get("name") or t.get("displayname") or "").strip()
                try:
                    if name:
                        out.setdefault(name, int(tid))
                except (TypeError, ValueError):
                    continue
        routes = list(self._routes_getter() or []) or list(self._own_routes or [])
        for r in routes:
            if getattr(r, "buy_terminal", "") and getattr(r, "id_terminal_buy", 0):
                out.setdefault(r.buy_terminal, int(r.id_terminal_buy))
        return out
