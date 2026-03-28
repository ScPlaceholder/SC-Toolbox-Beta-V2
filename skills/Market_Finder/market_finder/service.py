"""Data service layer — orchestrates API, cache, and threading."""

from __future__ import annotations

import logging
import threading
import time
import urllib.error
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from .api_client import UexApiClient
from .cache import CacheManager
from .config import (
    API_MAX_WORKERS,
    CACHE_TTL,
    CATEGORY_IDS,
    PRICE_CACHE_MAX_SIZE,
    PRICE_CACHE_TTL,
    item_tab,
)
from .errors import Result

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TTL + LRU price cache
# ---------------------------------------------------------------------------

class _TTLCache:
    """Simple thread-safe LRU cache with per-entry TTL eviction."""

    def __init__(self, max_size: int, ttl: int) -> None:
        self._max_size = max_size
        self._ttl = ttl
        self._data: OrderedDict[int, tuple[float, list[dict]]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: int) -> list[dict] | None:
        """Return cached value or ``None`` if missing / expired."""
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            ts, value = entry
            if time.time() - ts > self._ttl:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: int, value: list[dict]) -> None:
        """Insert or update a cache entry, evicting LRU if at capacity."""
        with self._lock:
            self._data[key] = (time.time(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._max_size:
                self._data.popitem(last=False)

    def clear(self) -> None:
        """Drop all entries."""
        with self._lock:
            self._data.clear()


# ---------------------------------------------------------------------------
# DataService
# ---------------------------------------------------------------------------

class DataService:
    """Central data service managing fetch, cache, and thread safety.

    All shared-state writes happen under ``_lock``.  UI reads reference
    assignments which are atomic under the GIL, so direct attribute
    access is safe once ``is_loaded()`` returns ``True``.
    """

    def __init__(
        self,
        api: UexApiClient | None = None,
        cache_mgr: CacheManager | None = None,
    ) -> None:
        self._api = api or UexApiClient()
        self._cache = cache_mgr or CacheManager()

        # Public read-only data (written atomically under _lock)
        self.items: list[dict] = []
        self.vehicles: list[dict] = []
        self.rentals: list[dict] = []
        self.vehicle_purchases: list[dict] = []
        self.terminals: dict[int, dict] = {}
        self.rental_by_vehicle: dict[int, list[dict]] = {}
        self.purchase_by_vehicle: dict[int, list[dict]] = {}
        self.items_by_tab: dict[str, list[dict]] = {}
        self.search_index: dict[int, str] = {}

        # Private state
        self._lock = threading.Lock()
        self._loaded: bool = False
        self._progress: str = ""
        self._error: str = ""

        # Price cache with LRU + TTL
        self._price_cache = _TTLCache(PRICE_CACHE_MAX_SIZE, PRICE_CACHE_TTL)
        self._fetching_prices: set[int] = set()
        self._fetching_prices_lock = threading.Lock()

        # Cancellation
        self._cancel_event = threading.Event()

        # Configurable cache TTL
        self.cache_ttl: int = CACHE_TTL

    # -- Thread-safe accessors -----------------------------------------------

    def is_loaded(self) -> bool:
        """Whether initial data fetch has completed."""
        with self._lock:
            return self._loaded

    def get_status(self) -> str:
        """Current human-readable progress string."""
        with self._lock:
            return self._progress

    def get_error(self) -> str:
        """Last error message (empty if none)."""
        with self._lock:
            return self._error

    # -- Cancellation --------------------------------------------------------

    def cancel(self) -> None:
        """Signal cancellation for in-flight requests."""
        self._cancel_event.set()

    def _check_cancelled(self) -> bool:
        return self._cancel_event.is_set()

    # -- Index building (called under _lock) ---------------------------------

    def _index_rentals(self) -> None:
        idx: dict[int, list[dict]] = {}
        for r in self.rentals:
            vid = r.get("id_vehicle")
            if vid is not None:
                idx.setdefault(vid, []).append(r)
        self.rental_by_vehicle = idx

    def _index_purchases(self) -> None:
        idx: dict[int, list[dict]] = {}
        for p in self.vehicle_purchases:
            vid = p.get("id_vehicle")
            if vid is not None:
                idx.setdefault(vid, []).append(p)
        self.purchase_by_vehicle = idx

    def _index_items_by_tab(self) -> None:
        by_tab: dict[str, list[dict]] = {"All": list(self.items)}
        for it in self.items:
            tab = item_tab(it)
            by_tab.setdefault(tab, []).append(it)
        self.items_by_tab = by_tab

        search_idx: dict[int, str] = {}
        for it in self.items:
            item_id = it.get("id")
            if item_id is not None:
                search_idx[item_id] = " ".join([
                    (it.get("name") or "").lower(),
                    (it.get("category") or "").lower(),
                    (it.get("company_name") or "").lower(),
                    (it.get("section") or "").lower(),
                ])
        self.search_index = search_idx

    # -- Main fetch ----------------------------------------------------------

    def fetch_all(self, force: bool = False) -> None:
        """Fetch all data from API (or cache).  Runs on a background thread."""
        self._cancel_event.clear()

        if not force:
            cache_result = self._cache.load(ttl=self.cache_ttl)
            if cache_result.ok:
                data = cache_result.data
                with self._lock:
                    self.items = data["items"]
                    self.vehicles = data["vehicles"]
                    self.rentals = data["rentals"]
                    self.vehicle_purchases = data["vehicle_purchases"]
                    self.terminals = data["terminals"]
                    self._index_rentals()
                    self._index_purchases()
                    self._index_items_by_tab()
                    self._loaded = True
                    self._progress = "Loaded from cache"
                return
            log.info("Cache miss: %s", cache_result.error)

        try:
            self._fetch_from_api()
        except (OSError, urllib.error.URLError, KeyError, TypeError, ValueError) as exc:
            with self._lock:
                self._error = str(exc)
                self._progress = f"Error: {exc}"
            log.error("fetch_all failed: %s", exc)

    def _fetch_from_api(self) -> None:
        """Fetch all data from the UEX API with progress updates."""
        errors: list[str] = []

        # --- Terminals ---
        with self._lock:
            self._progress = "Fetching terminals..."
        if self._check_cancelled():
            return
        term_result = self._api.get("terminals")
        term_map: dict[int, dict] = {}
        if term_result.ok:
            for t in term_result.data:
                tid = t.get("id")
                if tid is not None:
                    term_map[tid] = t
        else:
            errors.append(f"terminals: {term_result.error}")
            log.warning("Failed to fetch terminals: %s", term_result.error)

        # --- Vehicles ---
        with self._lock:
            self._progress = "Fetching vehicles..."
        if self._check_cancelled():
            return
        veh_result = self._api.get("vehicles")
        vehicles: list[dict] = veh_result.data if veh_result.ok else []
        if not veh_result.ok:
            errors.append(f"vehicles: {veh_result.error}")
            log.warning("Failed to fetch vehicles: %s", veh_result.error)

        # --- Items (parallel by category from config) ---
        all_items: list[dict] = []
        with self._lock:
            self._progress = "Fetching items (parallel)..."
        if self._check_cancelled():
            return

        with ThreadPoolExecutor(max_workers=API_MAX_WORKERS) as pool:
            futures = {
                pool.submit(self._api.get, f"items?id_category={cid}"): cid
                for cid in CATEGORY_IDS
            }
            for fut in as_completed(futures):
                if self._check_cancelled():
                    pool.shutdown(wait=False, cancel_futures=True)
                    return
                cid = futures[fut]
                try:
                    result = fut.result()
                    if result.ok and result.data:
                        all_items.extend(result.data)
                    elif not result.ok and result.error_type != "api":
                        log.debug("Category %d: %s", cid, result.error)
                except (OSError, urllib.error.URLError, KeyError, TypeError, ValueError) as exc:
                    log.warning("Category %d exception: %s", cid, exc)

        # --- Rentals ---
        with self._lock:
            self._progress = "Fetching rentals..."
        if self._check_cancelled():
            return
        rent_result = self._api.get("vehicles_rentals_prices_all")
        rentals: list[dict] = rent_result.data if rent_result.ok else []
        if not rent_result.ok:
            errors.append(f"rentals: {rent_result.error}")

        # --- Vehicle purchases ---
        with self._lock:
            self._progress = "Fetching ship purchase prices..."
        if self._check_cancelled():
            return
        purch_result = self._api.get("vehicles_purchases_prices_all")
        vehicle_purchases: list[dict] = purch_result.data if purch_result.ok else []
        if not purch_result.ok:
            errors.append(f"purchases: {purch_result.error}")

        # --- Commit atomically ---
        with self._lock:
            self.terminals = term_map
            self.vehicles = vehicles
            self.items = all_items
            self.rentals = rentals
            self.vehicle_purchases = vehicle_purchases
            self._index_rentals()
            self._index_purchases()
            self._index_items_by_tab()
            self._loaded = True
            if errors:
                self._error = "; ".join(errors)
                self._progress = (
                    f"Loaded {len(self.items)} items, "
                    f"{len(self.vehicles)} vehicles "
                    f"({len(errors)} error(s))"
                )
            else:
                self._error = ""
                self._progress = (
                    f"Loaded {len(self.items)} items, "
                    f"{len(self.vehicles)} vehicles"
                )

        # Persist to disk (non-critical path)
        self._cache.save({
            "items": all_items,
            "vehicles": vehicles,
            "rentals": rentals,
            "vehicle_purchases": vehicle_purchases,
            "terminals": term_map,
        })

    # -- Item prices ---------------------------------------------------------

    def fetch_item_prices(self, item_id: int) -> Result[list[dict]]:
        """Fetch prices for a specific item.  Uses LRU+TTL cache."""
        cached = self._price_cache.get(item_id)
        if cached is not None:
            return Result.success(cached)

        # Prevent duplicate concurrent fetches
        with self._fetching_prices_lock:
            if item_id in self._fetching_prices:
                return Result.failure("Already fetching", "in_progress")
            self._fetching_prices.add(item_id)

        try:
            result = self._api.get(f"items_prices?id_item={item_id}")
            if result.ok:
                self._price_cache.put(item_id, result.data)
            return result
        finally:
            with self._fetching_prices_lock:
                self._fetching_prices.discard(item_id)

    # -- Cache management ----------------------------------------------------

    def clear_cache(self) -> None:
        """Clear both disk cache and in-memory price cache."""
        self._cache.delete()
        self._price_cache.clear()
