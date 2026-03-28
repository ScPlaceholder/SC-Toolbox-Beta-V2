"""Generalized disk cache with TTL, version gating, and atomic writes.

Extracted from Market Finder's ``CacheManager`` — the most complete cache
implementation in the codebase — but stripped of Market Finder-specific
schema validation so any skill can use it.

Usage::

    from shared.cache_manager import DiskCache

    cache = DiskCache(".my_cache.json", cache_version=2)
    result = cache.load(ttl=3600)
    if result.ok:
        data = result.data
    else:
        data = fetch_fresh()
        cache.save(data)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from typing import Any, Callable

from shared.errors import Result

log = logging.getLogger(__name__)


class DiskCache:
    """Thread-safe, atomic disk cache with version and TTL validation.

    Parameters
    ----------
    cache_file:
        Path to the JSON cache file on disk.
    cache_version:
        Integer version tag — cache is rejected if it doesn't match.
    validate:
        Optional callback ``(data: dict) -> list[str]`` returning a list
        of validation errors (empty = valid).  Called after version/TTL
        checks but before returning data from ``load()``.
    """

    def __init__(
        self,
        cache_file: str,
        cache_version: int = 1,
        *,
        validate: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> None:
        self._cache_file = cache_file
        self._cache_version = cache_version
        self._cache_dir = os.path.dirname(os.path.abspath(cache_file))
        self._validate = validate

    @property
    def path(self) -> str:
        """Return the cache file path."""
        return self._cache_file

    def load(self, ttl: int) -> Result[dict[str, Any]]:
        """Load and validate cache from disk.

        Returns ``Result`` with cache dict on success, or an error on
        miss / corruption / expiry / version mismatch / validation failure.
        """
        if not os.path.exists(self._cache_file):
            return Result.failure("Cache file does not exist", "cache_miss")

        try:
            with open(self._cache_file, "r", encoding="utf-8") as fh:
                cache: dict[str, Any] = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            return Result.failure(f"Cache read failed: {exc}", "cache_corrupt")

        # Version gate
        stored_version = cache.get("version") or cache.get("_cache_version")
        if stored_version != self._cache_version:
            return Result.failure(
                f"Version mismatch: got {stored_version}, want {self._cache_version}",
                "cache_version",
            )

        # TTL gate
        ts = cache.get("timestamp") or cache.get("_ts") or 0
        age = time.time() - ts
        if age > ttl:
            return Result.failure(
                f"Cache expired (age={age:.0f}s, ttl={ttl}s)", "cache_expired",
            )

        # Optional schema/content validation
        if self._validate:
            errors = self._validate(cache)
            if errors:
                return Result.failure(
                    f"Validation failed: {'; '.join(errors)}", "cache_schema",
                )

        return Result.success(cache)

    def save(self, data: dict[str, Any]) -> Result[None]:
        """Atomically write data to cache file.

        Stamps ``version`` and ``timestamp`` into *data* before writing.
        Uses tempfile + ``os.replace()`` for crash-safe atomic rename.
        """
        data["version"] = self._cache_version
        data["_cache_version"] = self._cache_version
        data["timestamp"] = time.time()
        data["_ts"] = time.time()

        os.makedirs(self._cache_dir, exist_ok=True)

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self._cache_dir, suffix=".tmp", prefix=".cache_",
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, separators=(",", ":"))
                os.replace(tmp_path, self._cache_file)
            except OSError:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except (OSError, TypeError, ValueError) as exc:
            log.error("Cache save failed: %s", exc)
            return Result.failure(f"Cache save failed: {exc}", "cache_write")

        return Result.success(None)

    def delete(self) -> None:
        """Remove the cache file from disk."""
        try:
            if os.path.exists(self._cache_file):
                os.remove(self._cache_file)
        except OSError as exc:
            log.warning("Failed to remove cache file: %s", exc)
