"""Minimal UEX API client for the standalone Starmap tool.

Interface-compatible with the ``UexApiClient.get()`` shape the ported
Market Finder helpers expect — ``get(endpoint)`` returns a small Result
with ``.ok`` and ``.data`` (a list of dicts) — but implemented on
``urllib`` with the same on-disk cache conventions as :mod:`.uex`, so
the Starmap tool adds no third-party HTTP dependency.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import dataclass, field
from typing import List, Optional

_BASE = "https://api.uexcorp.space/2.0"
_UA = "WingmanAI-Starmap/1.0"
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "starmap", "uex_cache")

# Default TTLs per endpoint family (seconds).
_TTLS = {
    "items": 86400.0,
    "items_prices_all": 1800.0,
    "terminals": 86400.0,
    "terminals_distances": 30 * 86400.0,
}
_DEFAULT_TTL = 3600.0
_STALE_TTL = 7 * 86400.0       # stale cache still beats nothing, for a week


@dataclass
class Result:
    ok: bool
    data: List[dict] = field(default_factory=list)
    error: str = ""


def _cache_path(key: str) -> str:
    safe = "".join(c if c.isalnum() else "_" for c in key)[:120]
    return os.path.join(_CACHE_DIR, safe + ".json")


def get(endpoint: str, ttl: Optional[float] = None) -> Result:
    """GET ``endpoint`` (may already contain a query string).

    Cache-fresh hit returns instantly; otherwise fetches live and refreshes
    the cache; on failure falls back to any cache entry younger than the
    stale window. Never raises — errors come back as ``Result(ok=False)``.
    """
    ttl = _TTLS.get(endpoint.split("?")[0], _DEFAULT_TTL) if ttl is None else ttl
    cp = _cache_path(endpoint)
    try:
        if os.path.isfile(cp) and (time.time() - os.path.getmtime(cp)) < ttl:
            with open(cp, "r", encoding="utf-8") as fh:
                return Result(ok=True, data=json.load(fh))
    except (OSError, json.JSONDecodeError):
        pass
    try:
        req = urllib.request.Request(
            _BASE + "/" + endpoint, headers={"User-Agent": _UA})
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.load(resp)
        data = payload.get("data")
        if not isinstance(data, list):
            data = [] if data is None else [data]
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = cp + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        os.replace(tmp, cp)
        return Result(ok=True, data=data)
    except Exception as exc:
        try:
            if os.path.isfile(cp) and (time.time() - os.path.getmtime(cp)) < _STALE_TTL:
                with open(cp, "r", encoding="utf-8") as fh:
                    return Result(ok=True, data=json.load(fh))
        except (OSError, json.JSONDecodeError):
            pass
        return Result(ok=False, error=str(exc))


class UexApiClient:
    """Tiny class wrapper so ported code can do ``UexApiClient()`` + ``.get()``
    exactly like the Market Finder client."""

    def get(self, endpoint: str) -> Result:
        return get(endpoint)
