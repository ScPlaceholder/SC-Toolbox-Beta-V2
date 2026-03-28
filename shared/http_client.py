"""Unified HTTP client with structured errors and exponential-backoff retry.

Uses only stdlib (``urllib``) so no external dependencies are needed.
Modeled on Market Finder's ``UexApiClient`` — the most mature pattern in
the codebase — but generalized for all skills.

Usage::

    from shared.http_client import HttpClient
    from shared.api_config import ERKUL_BASE_URL, ERKUL_HEADERS, ERKUL_TIMEOUT

    client = HttpClient(ERKUL_BASE_URL, headers=ERKUL_HEADERS, timeout=ERKUL_TIMEOUT)
    result = client.get_json("/live/ships")
    if result.ok:
        ships = result.data
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from shared.constants import DEFAULT_BACKOFF_BASE, DEFAULT_MAX_RETRIES
from shared.errors import Result

log = logging.getLogger(__name__)


class HttpClient:
    """Generic HTTP/JSON client with retry, backoff, and structured errors.

    Parameters
    ----------
    base_url:
        Root URL prepended to every *endpoint* (e.g. ``https://api.example.com``).
    headers:
        Default headers sent with every request.
    timeout:
        Socket timeout in seconds.
    max_retries:
        Total number of attempts (including the first).
    backoff_base:
        Base delay in seconds for exponential backoff (``base * 2^(attempt-1)``).
    """

    def __init__(
        self,
        base_url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: int = 30,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = headers or {}
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base

    def get_json(self, endpoint: str) -> Result[Any]:
        """Fetch *endpoint* as JSON with exponential-backoff retry.

        Retries on network errors and HTTP 429 (rate limit).
        Client errors (4xx except 429) are **not** retried.

        Returns ``Result.success(parsed_json)`` or ``Result.failure(msg, type)``.
        """
        sep = "" if endpoint.startswith("/") else "/"
        url = f"{self._base_url}{sep}{endpoint}"
        last_error: str = ""
        last_type: str = "unknown"

        for attempt in range(1, self._max_retries + 1):
            t0 = time.monotonic()
            try:
                req = urllib.request.Request(url, headers=self._headers)
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    body = json.loads(resp.read().decode())

                latency_ms = (time.monotonic() - t0) * 1000
                log.debug(
                    "HTTP GET %s attempt=%d latency=%.0fms status=ok",
                    endpoint, attempt, latency_ms,
                )
                return Result.success(body)

            except urllib.error.HTTPError as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                last_error = f"HTTP {exc.code}: {exc.reason}"
                last_type = "api"
                log.warning(
                    "HTTP GET %s attempt=%d http_error=%d latency=%.0fms",
                    endpoint, attempt, exc.code, latency_ms,
                )
                # Don't retry 4xx except 429 rate-limit
                if 400 <= exc.code < 500 and exc.code != 429:
                    break

            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                last_error = f"Network error: {exc}"
                last_type = "network"
                log.warning(
                    "HTTP GET %s attempt=%d network_error=%s latency=%.0fms",
                    endpoint, attempt, exc, latency_ms,
                )

            except (json.JSONDecodeError, ValueError) as exc:
                latency_ms = (time.monotonic() - t0) * 1000
                last_error = f"Parse error: {exc}"
                last_type = "unknown"
                log.error(
                    "HTTP GET %s attempt=%d parse_error=%s latency=%.0fms",
                    endpoint, attempt, exc, latency_ms,
                )

            if attempt < self._max_retries:
                delay = self._backoff_base * (2 ** (attempt - 1))
                time.sleep(delay)

        return Result.failure(last_error, error_type=last_type)
