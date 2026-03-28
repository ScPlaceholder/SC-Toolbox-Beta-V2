"""Disk-cache wrapper for Craft Database using shared.cache_manager."""

from __future__ import annotations

import os
import logging

from shared.cache_manager import DiskCache
from shared.errors import Result

log = logging.getLogger(__name__)

_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".craft_cache")


class CraftCache:
    """Wraps DiskCache for blueprint and filter-hint data."""

    CACHE_VERSION = 1

    def __init__(self, cache_dir: str = _CACHE_DIR) -> None:
        os.makedirs(cache_dir, exist_ok=True)
        self._blueprints = DiskCache(
            os.path.join(cache_dir, "blueprints.json"),
            cache_version=self.CACHE_VERSION,
        )
        self._hints = DiskCache(
            os.path.join(cache_dir, "hints.json"),
            cache_version=self.CACHE_VERSION,
        )
        self._stats = DiskCache(
            os.path.join(cache_dir, "stats.json"),
            cache_version=self.CACHE_VERSION,
        )
        log.debug("CraftCache initialised at %s", cache_dir)

    # ── blueprints ───────────────────────────────────────────────────────

    def load_blueprints(self, ttl: int) -> Result[dict]:
        r = self._blueprints.load(ttl)
        log.debug("Cache load blueprints: %s", "hit" if r.ok else "miss")
        return r

    def save_blueprints(self, data: dict) -> Result[None]:
        r = self._blueprints.save(data)
        log.debug("Cache save blueprints: %s", "ok" if r.ok else r.error)
        return r

    # ── filter hints ─────────────────────────────────────────────────────

    def load_hints(self, ttl: int) -> Result[dict]:
        r = self._hints.load(ttl)
        log.debug("Cache load hints: %s", "hit" if r.ok else "miss")
        return r

    def save_hints(self, data: dict) -> Result[None]:
        r = self._hints.save(data)
        log.debug("Cache save hints: %s", "ok" if r.ok else r.error)
        return r

    # ── stats ────────────────────────────────────────────────────────────

    def load_stats(self, ttl: int) -> Result[dict]:
        r = self._stats.load(ttl)
        log.debug("Cache load stats: %s", "hit" if r.ok else "miss")
        return r

    def save_stats(self, data: dict) -> Result[None]:
        r = self._stats.save(data)
        log.debug("Cache save stats: %s", "ok" if r.ok else r.error)
        return r
