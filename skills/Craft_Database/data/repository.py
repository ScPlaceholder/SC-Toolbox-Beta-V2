"""Data repository for the Craft Database: datamined blueprints, in memory.

The data is the pinned scunpacked-data ``blueprints.json`` (see
``data/datamine.py``). Loading reads a prebuilt index (or builds it once)
in a background thread; every search, filter and page after that is an
in-memory lookup, so nothing here touches the network except
``download_async``, which the user starts with the Download button.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from domain.models import Blueprint, CraftStats, FilterHints, Pagination
from data.datamine import DatamineMissing, DatamineSource
from services.filter_service import matches_search

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BlueprintQuery:
    """Immutable set of parameters for a blueprint query."""
    page: int = 1
    limit: int = 50
    search: str = ""
    ownable: bool | None = True
    resource: str = ""
    mission_type: str = ""      # not in the datamine; ignored
    location: str = ""          # not in the datamine; ignored
    contractor: str = ""        # not in the datamine; ignored
    category: str = ""


def query_blueprints(all_bps: list[Blueprint], q: BlueprintQuery) -> tuple[list[Blueprint], Pagination]:
    """Filter + paginate (pure). Category matches itself and its sub-categories."""
    res = all_bps
    if q.ownable:
        res = [b for b in res if b.obtainable]
    if q.category:
        c = q.category.strip().lower()
        res = [b for b in res
               if b.category.lower() == c or b.category.lower().startswith(c + " / ")]
    if q.resource:
        r = q.resource.strip().lower()
        res = [b for b in res if any(r == n.lower() for n in b.ingredient_names)]
    if q.search:
        s = q.search.strip()
        res = [b for b in res if matches_search(b, s) or s.lower() in b.output_class.lower()]
    limit = max(1, q.limit)
    total = len(res)
    pages = max(1, (total + limit - 1) // limit)
    page = min(max(1, q.page), pages)
    chunk = res[(page - 1) * limit: page * limit]
    return chunk, Pagination(page=page, limit=limit, total=total, pages=pages)


class CraftRepository:
    """Thread-safe holder of the datamined blueprint index."""

    def __init__(self, source: DatamineSource | None = None) -> None:
        self._source = source or DatamineSource()
        self._lock = threading.Lock()

        self._stats: CraftStats | None = None
        self._hints: FilterHints | None = None
        self._all: list[Blueprint] = []
        self._blueprints: list[Blueprint] = []
        self._pagination: Pagination = Pagination()
        self._loaded = False
        self._loading = False
        self._missing = False
        self._error: str | None = None
        self._last_query = BlueprintQuery()

    # ── public state ─────────────────────────────────────────────────────

    @property
    def source(self) -> DatamineSource:
        return self._source

    def game_label(self) -> str:
        return self._source.label()

    def is_loaded(self) -> bool:
        with self._lock:
            return self._loaded

    def is_loading(self) -> bool:
        with self._lock:
            return self._loading

    def is_missing(self) -> bool:
        """True when the data has not been downloaded yet (offer Download)."""
        with self._lock:
            return self._missing

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

    def get_all(self) -> list[Blueprint]:
        with self._lock:
            return list(self._all)

    def get_pagination(self) -> Pagination:
        with self._lock:
            return self._pagination

    def find(self, blueprint_id: str) -> Blueprint | None:
        with self._lock:
            return next((b for b in self._all if b.blueprint_id == blueprint_id), None)

    # ── loading ──────────────────────────────────────────────────────────

    def load(self) -> bool:
        """Load (building the index if needed) on the calling thread."""
        try:
            idx = self._source.load()
        except DatamineMissing as exc:
            with self._lock:
                self._missing, self._error, self._loaded = True, str(exc), False
            return False
        except Exception as exc:                    # noqa: BLE001
            log.exception("Craft data load failed")
            with self._lock:
                self._missing, self._error, self._loaded = False, str(exc), False
            return False
        bps = [Blueprint.from_dict(d) for d in idx.get("blueprints", [])]
        with self._lock:
            self._all = bps
            self._stats = CraftStats.from_dict(idx.get("stats", {}))
            self._hints = FilterHints.from_dict(idx.get("hints", {}))
            self._missing, self._error, self._loaded = False, None, True
        self._apply(self._last_query)
        log.info("Craft data loaded: %d blueprints (%s)", len(bps), self._source.build)
        return True

    def _run_async(self, work: Callable[[], None], on_done: Callable[[], None] | None) -> None:
        with self._lock:
            if self._loading:
                return
            self._loading = True

        def _worker():
            try:
                work()
            except Exception as exc:                # noqa: BLE001
                log.exception("Craft data worker failed")
                with self._lock:
                    self._error = str(exc)
            finally:
                with self._lock:
                    self._loading = False
                if on_done:
                    on_done()

        threading.Thread(target=_worker, daemon=True).start()

    def load_async(self, on_done: Callable[[], None] | None = None) -> None:
        self._run_async(self.load, on_done)

    def download_async(self, on_done: Callable[[], None] | None = None) -> None:
        """Fetch the pinned blueprints.json (GitHub raw), index it, load it."""
        def work():
            try:
                self._source.download()
            except Exception as exc:                # noqa: BLE001
                log.warning("Craft data download failed: %s", exc)
                with self._lock:
                    self._error = f"Download failed: {exc}"
                return
            self.load()
        self._run_async(work, on_done)

    def cancel(self) -> None:
        """Kept for callers; loading is local and short."""

    # ── queries ──────────────────────────────────────────────────────────

    def _apply(self, q: BlueprintQuery) -> None:
        with self._lock:
            all_bps = self._all
        chunk, pag = query_blueprints(all_bps, q)
        with self._lock:
            self._blueprints, self._pagination, self._last_query = chunk, pag, q

    def fetch_blueprints(
        self,
        query: BlueprintQuery | None = None,
        on_done: Callable[[], None] | None = None,
    ) -> None:
        """Apply *query* to the loaded data (in memory, immediate)."""
        self._apply(query or BlueprintQuery())
        if on_done:
            on_done()
