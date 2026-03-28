"""HTTP API clients for erkul.games and fleetyards.net with retries.

Uses ``shared.http_client.HttpClient`` for consistent retry/backoff.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from shared.api_config import (
    ERKUL_TIMEOUT, FLEETYARDS_TIMEOUT, FLEETYARDS_BASE_URL,
)
from shared.errors import ApiError as _SharedApiError
from shared.http_client import HttpClient

_log = logging.getLogger(__name__)


# Backward-compatible alias for code that catches this type
ApiError = _SharedApiError


class ErkulApiClient:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        timeout: int = ERKUL_TIMEOUT,
        retries: int = 3,
    ) -> None:
        self.base_url = base_url
        self.headers = headers
        self._client = HttpClient(base_url, headers=headers, timeout=timeout, max_retries=retries)

    def fetch(self, path: str) -> list:
        result = self._client.get_json(path)
        if result.ok:
            return result.data if isinstance(result.data, list) else [result.data] if result.data else []
        raise ApiError(f"{self.base_url}{path}", message=result.error)

    def fetch_safe(self, path: str, warn_cb: Callable[[str], None] | None = None) -> list:
        result = self._client.get_json(path)
        if result.ok:
            return result.data if isinstance(result.data, list) else [result.data] if result.data else []
        if warn_cb:
            warn_cb(f"\u26a0 {path}: {result.error}")
        _log.warning("fetch_safe failed for %s: %s", path, result.error)
        return []

    def fetch_all_ships(self, warn_cb: Callable[[str], None] | None = None) -> list:
        seen = {}
        def merge(entries):
            for e in (entries or []):
                n = e.get("data", {}).get("name", "")
                if n and n not in seen:
                    seen[n] = e
        merge(self.fetch_safe("/live/ships?limit=500", warn_cb))
        merge(self.fetch_safe("/live/ships?type=ground", warn_cb))
        merge(self.fetch_safe("/live/ships?type=capital", warn_cb))
        merge(self.fetch_safe("/live/ships", warn_cb))
        ships = list(seen.values())
        if len(ships) < 30:
            if warn_cb:
                warn_cb("erkul returned < 30 ships \u2014 trying fleetyards.net fallback\u2026")
            fy_client = HttpClient(
                FLEETYARDS_BASE_URL, headers=self.headers,
                timeout=ERKUL_TIMEOUT, max_retries=2,
            )
            for page in range(1, 10):
                result = fy_client.get_json(f"/models?perPage=240&page={page}")
                if not result.ok:
                    if warn_cb:
                        warn_cb(f"fleetyards fallback failed: {result.error}")
                    break
                chunk = result.data
                if not chunk:
                    break
                for m in (chunk if isinstance(chunk, list) else []):
                    n = m.get("name", "")
                    if n and n not in seen:
                        seen[n] = {"data": {"name": n, "ref": m.get("slug", ""), "loadout": []}}
                if isinstance(chunk, list) and len(chunk) < 240:
                    break
            ships = list(seen.values())
        return ships


class FleetyardsApiClient:
    def __init__(
        self,
        base_url: str,
        headers: dict[str, str],
        timeout: int = FLEETYARDS_TIMEOUT,
        retries: int = 3,
    ) -> None:
        self._client = HttpClient(base_url, headers=headers, timeout=timeout, max_retries=retries)
        self.base_url = base_url

    def fetch_hardpoints(self, slug: str) -> list:
        result = self._client.get_json(f"/models/{slug}/hardpoints")
        if result.ok and isinstance(result.data, list):
            return result.data
        _log.warning("FY hardpoints fetch failed for %s: %s", slug, result.error)
        return []
