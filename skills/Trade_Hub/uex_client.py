# NOTE: Only used by trade_hub_window.py — may be deprecated
"""
UEX Corp API client for Trade Hub.
Fetches trade route data from https://api.uexcorp.space/2.0
Thread-safe, with TTL-based caching and background refresh support.

Uses ``shared.http_client.HttpClient`` for HTTP retry/backoff.
"""
from __future__ import annotations

import sqlite3
import threading
import time
import logging
import traceback
from dataclasses import dataclass
from typing import Optional, List

import shared.path_setup  # noqa: E402  # centralised path config
from shared.api_config import UEX_BASE_URL as UEX_BASE, UEX_USER_AGENT, UEX_HEADERS, UEX_TIMEOUT, CACHE_TTL_SHORT
from shared.http_client import HttpClient

logger = logging.getLogger(__name__)


@dataclass
class RouteData:
    """Normalized trade route data."""
    commodity: str = ""
    buy_system: str = ""
    buy_location: str = ""     # Most specific location (outpost / city / moon / planet)
    buy_terminal: str = ""
    sell_system: str = ""
    sell_location: str = ""
    sell_terminal: str = ""
    price_buy: float = 0.0
    price_sell: float = 0.0
    scu_available: int = 0     # Stock at buy terminal
    scu_demand: int = 0        # Demand at sell terminal
    margin: float = 0.0        # Per-SCU profit
    margin_pct: float = 0.0
    score: float = 0.0

    def effective_scu(self, ship_scu: int = 0) -> int:
        if self.scu_available <= 0:
            stock = 0
        elif self.scu_available > 0 and self.scu_demand > 0:
            stock = min(self.scu_available, self.scu_demand)
        else:
            stock = max(self.scu_available, self.scu_demand)
        if ship_scu <= 0:
            return stock
        return min(ship_scu, stock)

    def estimated_profit(self, ship_scu: int) -> float:
        return self.effective_scu(ship_scu) * self.margin

    def investment(self, ship_scu: int) -> float:
        return self.effective_scu(ship_scu) * self.price_buy


@dataclass
class ShipData:
    """Ship cargo data from UEX Corp."""
    name: str = ""
    scu: int = 0
    manufacturer: str = ""


class UEXClient:
    """Thread-safe UEX Corp API client with TTL caching."""

    def __init__(self, cache_ttl: float = CACHE_TTL_SHORT) -> None:
        self._cache_routes: Optional[List[RouteData]] = None
        self._cache_ships: Optional[List[ShipData]] = None
        self._cache_routes_time: float = 0.0
        self._cache_ships_time: float = 0.0
        self._cache_ttl = cache_ttl
        self._lock = threading.Lock()
        self._fetching_routes = False
        self._client = HttpClient(
            UEX_BASE, headers=UEX_HEADERS, timeout=UEX_TIMEOUT,
        )

    def close(self) -> None:
        """No-op — HttpClient uses stdlib urllib (no persistent session)."""

    def __enter__(self) -> UEXClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def get_routes(self, force_refresh: bool = False) -> List[RouteData]:
        """Return normalized trade routes (cached up to cache_ttl seconds)."""
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cache_routes is not None
                and (now - self._cache_routes_time) < self._cache_ttl
            ):
                return self._cache_routes
            # Another thread is already fetching — return stale cache.
            # NOTE: This assumes a single concurrent caller for force_refresh.
            # If two threads both call get_routes(force_refresh=True) simultaneously,
            # the second will get stale data. This is acceptable because refresh_async
            # is the primary entry point and serializes via _fetching_routes.
            if self._fetching_routes:
                return self._cache_routes or []
            self._fetching_routes = True

        try:
            routes = self._fetch_routes()
        finally:
            with self._lock:
                self._fetching_routes = False

        with self._lock:
            self._cache_routes = routes
            self._cache_routes_time = time.time()

        return routes

    def get_ships(self, force_refresh: bool = False) -> List[ShipData]:
        """Return cargo ships from UEX Corp (cached)."""
        with self._lock:
            now = time.time()
            if (
                not force_refresh
                and self._cache_ships is not None
                and (now - self._cache_ships_time) < self._cache_ttl
            ):
                return self._cache_ships

        ships = self._fetch_ships()

        with self._lock:
            self._cache_ships = ships
            self._cache_ships_time = time.time()

        return ships

    def refresh_async(self, callback=None) -> None:
        """
        Fetch routes in a background thread.
        Tries the local UEXCorp SQLite DB first (same data, no extra API call).
        Falls back to the live API if the DB is unavailable or empty.
        Calls callback(routes, source) when done.
        """
        def _worker():
            routes, source = self._fetch_routes_with_source()
            with self._lock:
                self._cache_routes = routes
                self._cache_routes_time = time.time()
            if callback:
                try:
                    callback(routes, source)
                except TypeError:
                    try:
                        callback(routes)
                    except TypeError:
                        pass

        threading.Thread(target=_worker, daemon=True, name="TradeHubRefresh").start()

    def _fetch_routes_with_source(self):
        """Try local DB first, then live API. Returns (routes, source_label)."""
        try:
            from local_db_reader import find_uexcorp_db, read_routes_from_db
        except ImportError as exc:
            logger.info("[TradeHub] local_db_reader not available: %s", exc)
        else:
            try:
                db_path = find_uexcorp_db()
                if db_path:
                    routes = read_routes_from_db(db_path)
                    if routes:
                        logger.info(f"[TradeHub] Loaded {len(routes)} routes from local DB")
                        return routes, "Local DB"
            except (sqlite3.Error, OSError) as exc:
                logger.warning("[TradeHub] Local DB read failed: %s\n%s", exc, traceback.format_exc())

        # Fall back to live API
        routes = self._fetch_routes()
        return routes, "UEX API"

    def last_refresh_time(self) -> Optional[float]:
        return self._cache_routes_time if self._cache_routes_time > 0 else None

    # ── Private helpers ───────────────────────────────────────────────────────

    def _fetch_routes(self) -> List[RouteData]:
        result = self._client.get_json("/commodities_routes")
        if not result.ok:
            logger.error("[TradeHub] Failed to fetch routes: %s", result.error)
            with self._lock:
                return self._cache_routes or []
        raw = result.data.get("data", []) if isinstance(result.data, dict) else result.data
        routes = [self._normalize_route(r) for r in (raw or [])]
        routes = [r for r in routes if r.commodity and r.margin > 0]
        routes.sort(key=lambda x: x.score, reverse=True)
        logger.info("[TradeHub] Fetched %d profitable routes from UEX Corp", len(routes))
        return routes

    def _fetch_ships(self) -> List[ShipData]:
        result = self._client.get_json("/vehicles")
        if not result.ok:
            logger.error("[TradeHub] Failed to fetch ships: %s", result.error)
            with self._lock:
                return self._cache_ships or []
        raw = result.data.get("data", []) if isinstance(result.data, dict) else result.data
        ships = []
        for r in (raw or []):
            scu = int(
                r.get("scu", 0)
                or r.get("cargo_capacity", 0)
                or r.get("scu_cargo", 0)
                or 0
            )
            name = r.get("name", "").strip()
            if scu > 0 and name:
                ships.append(ShipData(
                    name=name,
                    scu=scu,
                    manufacturer=r.get("manufacturer_name", r.get("manufacturer", "")),
                ))
        ships.sort(key=lambda x: x.name)
        return ships

    def _normalize_route(self, r: dict) -> RouteData:
        """Map a raw API route dict → RouteData, handling multiple field-name conventions."""
        rd = RouteData()

        rd.commodity = r.get("commodity_name", "") or ""

        # Buy (origin)
        rd.buy_system   = r.get("star_system_origin", "") or ""
        rd.buy_location = self._best_location(r, "origin")
        rd.buy_terminal = (
            r.get("terminal_origin", "")
            or r.get("terminal_name_origin", "")
            or r.get("name_terminal_origin", "")
            or ""
        )

        # Sell (destination)
        rd.sell_system   = r.get("star_system_destination", "") or ""
        rd.sell_location = self._best_location(r, "destination")
        rd.sell_terminal = (
            r.get("terminal_destination", "")
            or r.get("terminal_name_destination", "")
            or r.get("name_terminal_destination", "")
            or ""
        )

        # Prices — try both naming conventions
        po = r.get("price_origin")
        rd.price_buy  = float(po if po is not None else (r.get("price_buy") or 0))
        pd = r.get("price_destination")
        rd.price_sell = float(pd if pd is not None else (r.get("price_sell") or 0))

        # Stock
        so = r.get("scu_origin")
        rd.scu_available = int(so if so is not None else (r.get("scu_buy") or 0))
        sd = r.get("scu_destination")
        rd.scu_demand    = int(sd if sd is not None else (r.get("scu_sell") or 0))

        # Profit
        rd.margin = float(r.get("profit_margin", 0) or r.get("margin", 0) or 0)
        if rd.margin == 0 and rd.price_sell > rd.price_buy:
            rd.margin = rd.price_sell - rd.price_buy

        rd.margin_pct = float(
            r.get("profit_margin_percentage", 0) or r.get("margin_pct", 0) or 0
        )
        if rd.margin_pct == 0 and rd.price_buy > 0:
            rd.margin_pct = (rd.margin / rd.price_buy) * 100

        rd.score = float(r.get("score", 0) or 0)
        return rd

    @staticmethod
    def _best_location(r: dict, suffix: str) -> str:
        """Return the most specific non-empty location name for origin/destination."""
        for key in (
            f"outpost_{suffix}",
            f"city_{suffix}",
            f"space_station_{suffix}",
            f"moon_{suffix}",
            f"planet_{suffix}",
            f"star_system_{suffix}",
        ):
            val = (r.get(key) or "").strip()
            if val:
                return val
        return ""
