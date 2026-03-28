"""Data repository — orchestrates API + cache for the Craft Database."""

from __future__ import annotations

import logging
import threading
from typing import Callable

from shared.api_config import CACHE_TTL_CRAFT
from shared.errors import Result

from domain.models import Blueprint, CraftStats, FilterHints, Pagination
from data.api_client import CraftApiClient
from data.cache import CraftCache

log = logging.getLogger(__name__)


class CraftRepository:
    """Thread-safe repository that fetches from API and caches to disk."""

    def __init__(self) -> None:
        self._api = CraftApiClient()
        self._cache = CraftCache()
        self._lock = threading.Lock()

        self._stats: CraftStats | None = None
        self._hints: FilterHints | None = None
        self._blueprints: list[Blueprint] = []
        self._pagination: Pagination = Pagination()
        self._loaded = False
        self._loading = False
        self._error: str | None = None
        self._cancel = threading.Event()

    # ── public state ─────────────────────────────────────────────────────

    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def is_loading(self) -> bool:
        with self._lock:
            return self._loading

    def get_error(self) -> str | None:
        with self._lock:
            return self._error

    def get_stats(self) -> CraftStats | None:
        with self._lock:
            return self._stats

    def get_hints(self) -> FilterHints | None:
        with self._lock:
            return self._hints

    def get_blueprints(self) -> list[Blueprint]:
        with self._lock:
            return list(self._blueprints)

    def get_pagination(self) -> Pagination:
        with self._lock:
            return self._pagination

    # ── loading ──────────────────────────────────────────────────────────

    def load_async(self, on_done: Callable[[], None] | None = None) -> None:
        with self._lock:
            if self._loading:
                return
            self._loading = True
            self._error = None
            self._cancel.clear()

        def _worker():
            try:
                self._fetch_initial()
            except Exception as exc:
                log.exception("Craft repo load failed")
                with self._lock:
                    self._error = str(exc)
            finally:
                with self._lock:
                    self._loading = False
                    self._loaded = self._error is None
                if on_done:
                    on_done()

        threading.Thread(target=_worker, daemon=True).start()

    def cancel(self) -> None:
        self._cancel.set()

    # ── fetch helpers ────────────────────────────────────────────────────

    def _fetch_initial(self) -> None:
        log.info("Starting initial data load")
        self._fetch_stats()
        if self._cancel.is_set():
            return
        self._fetch_hints()
        if self._cancel.is_set():
            return
        self._fetch_blueprints_page(page=1, limit=50, ownable=True)
        log.info("Initial data load complete")

    def _fetch_stats(self) -> None:
        cached = self._cache.load_stats(CACHE_TTL_CRAFT)
        if cached.ok and "payload" in cached.data:
            with self._lock:
                self._stats = CraftStats.from_dict(cached.data["payload"])
            return

        result = self._api.fetch_stats()
        if result.ok:
            self._cache.save_stats({"payload": result.data})
            with self._lock:
                self._stats = CraftStats.from_dict(result.data)
        else:
            log.warning("Failed to fetch stats: %s", result.error)

    def _fetch_hints(self) -> None:
        cached = self._cache.load_hints(CACHE_TTL_CRAFT)
        if cached.ok and "payload" in cached.data:
            with self._lock:
                self._hints = FilterHints.from_dict(cached.data["payload"])
            return

        result = self._api.fetch_filter_hints()
        if result.ok:
            self._cache.save_hints({"payload": result.data})
            with self._lock:
                self._hints = FilterHints.from_dict(result.data)
        else:
            log.warning("Failed to fetch hints: %s", result.error)

    def fetch_blueprints(
        self,
        page: int = 1,
        limit: int = 50,
        search: str = "",
        ownable: bool | None = True,
        resource: str = "",
        mission_type: str = "",
        location: str = "",
        contractor: str = "",
        category: str = "",
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Fetch a page of blueprints in a background thread."""

        def _worker():
            try:
                self._fetch_blueprints_page(
                    page=page, limit=limit, search=search,
                    ownable=ownable, resource=resource,
                    mission_type=mission_type, location=location,
                    contractor=contractor, category=category,
                )
            except Exception:
                log.exception("Blueprint fetch failed")
            finally:
                if on_done:
                    on_done()

        threading.Thread(target=_worker, daemon=True).start()

    def _fetch_blueprints_page(
        self,
        page: int = 1,
        limit: int = 50,
        search: str = "",
        ownable: bool | None = True,
        resource: str = "",
        mission_type: str = "",
        location: str = "",
        contractor: str = "",
        category: str = "",
    ) -> None:
        result = self._api.fetch_blueprints(
            page=page, limit=limit, search=search,
            ownable=ownable, resource=resource,
            mission_type=mission_type, location=location,
            contractor=contractor, category=category,
        )
        if result.ok and isinstance(result.data, dict):
            items = result.data.get("items", [])
            pag = result.data.get("pagination", {})
            bps = [Blueprint.from_dict(d) for d in items]
            with self._lock:
                self._blueprints = bps
                self._pagination = Pagination.from_dict(pag)
            log.info("Loaded %d blueprints (page %d/%d)",
                     len(bps), self._pagination.page, self._pagination.pages)
        else:
            log.warning("Blueprint fetch error: %s", result.error)
            with self._lock:
                self._error = result.error or "Unknown error"
