"""Retry wrapper for urllib network requests.

Provides :func:`urlopen_with_retry`, a drop-in replacement for
``urllib.request.urlopen`` that retries transient failures with
exponential backoff.  Intended for the handful of network calls
scattered across the services layer (sheet_fetcher, mining_chart_data,
refinery_yields, refinery_distances).

Usage::

    from services.http_retry import urlopen_with_retry

    req = urllib.request.Request(url, headers={...})
    with urlopen_with_retry(req, timeout=30) as resp:
        data = resp.read()
"""

from __future__ import annotations

import logging
import time
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

# ── Configuration ──
DEFAULT_RETRIES = 3          # total attempts = 1 + retries
DEFAULT_BACKOFF_BASE = 1.0   # first retry after 1s, then 2s, then 4s
DEFAULT_BACKOFF_MAX = 10.0   # cap individual sleep to 10s

# Transient HTTP status codes worth retrying
_RETRYABLE_HTTP_CODES = frozenset({429, 500, 502, 503, 504})


def urlopen_with_retry(
    req: urllib.request.Request | str,
    *,
    timeout: float = 30,
    retries: int = DEFAULT_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    backoff_max: float = DEFAULT_BACKOFF_MAX,
) -> Any:
    """Open a URL with retry and exponential backoff.

    Parameters
    ----------
    req
        A ``Request`` object or URL string.
    timeout
        Per-attempt socket timeout in seconds.
    retries
        Maximum number of retry attempts after the initial try.
    backoff_base
        Initial backoff delay (doubled each retry, capped at *backoff_max*).
    backoff_max
        Maximum sleep between retries.

    Returns
    -------
    http.client.HTTPResponse
        The response object (caller should use as context manager).

    Raises
    ------
    urllib.error.URLError
        After all retries are exhausted, the last error is re-raised.
    """
    last_exc: BaseException | None = None

    for attempt in range(1 + retries):
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in _RETRYABLE_HTTP_CODES:
                raise  # 4xx client errors (except 429) are not retryable
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc

        if attempt < retries:
            delay = min(backoff_base * (2 ** attempt), backoff_max)
            url_str = req if isinstance(req, str) else req.full_url
            log.warning(
                "http_retry: attempt %d/%d failed for %s — "
                "retrying in %.1fs (%s)",
                attempt + 1, 1 + retries, url_str, delay, last_exc,
            )
            time.sleep(delay)

    # All retries exhausted — re-raise last error
    raise last_exc  # type: ignore[misc]
