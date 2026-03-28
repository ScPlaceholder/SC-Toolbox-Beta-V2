"""Market Finder disk cache — extends ``shared.cache_manager.DiskCache``
with UEX-specific schema validation, terminal key normalisation, and
v2 → v3 migration.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.cache_manager import DiskCache
from shared.errors import Result

from .config import CACHE_FILE, CACHE_TTL, CACHE_VERSION

log = logging.getLogger(__name__)

# Required top-level keys and their expected types
_SCHEMA: dict[str, type | tuple] = {
    "version": int,
    "timestamp": (int, float),
    "items": list,
    "vehicles": list,
    "rentals": list,
    "vehicle_purchases": list,
    "terminals": dict,
}

# Required fields in each item dict (spot-checked)
_ITEM_REQUIRED_FIELDS: set[str] = {"id", "name"}


def _validate(data: dict[str, Any]) -> list[str]:
    """Combined schema + item spot-check validation."""
    errors: list[str] = []
    for key, expected in _SCHEMA.items():
        if key not in data:
            errors.append(f"Missing key: {key}")
            continue
        if not isinstance(data[key], expected):
            errors.append(f"'{key}' type {type(data[key]).__name__}, expected {expected}")
    for i, item in enumerate(data.get("items", [])[:5]):
        if not isinstance(item, dict):
            errors.append(f"Item[{i}] is not a dict")
            continue
        for field in _ITEM_REQUIRED_FIELDS:
            if field not in item:
                errors.append(f"Item[{i}] missing '{field}'")
    return errors


class CacheManager(DiskCache):
    """UEX cache with schema validation and terminal key normalisation."""

    def __init__(
        self,
        cache_file: str = CACHE_FILE,
        cache_version: int = CACHE_VERSION,
    ) -> None:
        super().__init__(cache_file, cache_version, validate=_validate)

    def load(self, ttl: int = CACHE_TTL) -> Result[dict[str, Any]]:
        result = super().load(ttl)
        if not result.ok:
            # Try v2 → v3 migration on version mismatch
            if result.error_type == "cache_version":
                migrated = self._try_migrate()
                if migrated is not None:
                    return super().load(ttl)
            return result

        # Normalise terminal keys to int
        cache = result.data
        raw_terminals = cache.get("terminals", {})
        cache["terminals"] = {int(k): v for k, v in raw_terminals.items()}
        return Result.success(cache)

    def save(self, data: dict[str, Any]) -> Result[None]:
        # Normalise terminal keys to str for JSON serialisation
        payload = dict(data)
        if "terminals" in payload:
            payload["terminals"] = {str(k): v for k, v in payload["terminals"].items()}
        return super().save(payload)

    def _try_migrate(self) -> dict | None:
        """Attempt v2 → v3 migration by re-reading and bumping version."""
        import json
        try:
            with open(self._cache_file, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return None
        if cache.get("version") == 2 and self._cache_version == 3:
            cache["version"] = 3
            self.save(cache)
            return cache
        return None
