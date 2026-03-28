"""HTTP client for the UEX Corp API with structured errors and retry.

Thin wrapper around ``shared.http_client.HttpClient`` that adds
UEX-specific response unwrapping (status/data envelope).
"""

from __future__ import annotations

import logging
from typing import Any

from shared.http_client import HttpClient
from shared.errors import Result

from .config import API_BASE, API_BACKOFF_BASE, API_HEADERS, API_MAX_RETRIES, API_TIMEOUT

log = logging.getLogger(__name__)


class UexApiClient:
    """Handles all HTTP communication with the UEX Corp API.

    Every public method returns a ``Result`` — never silently returns
    an empty list on failure.  Network errors and API errors are
    distinguished so callers can react appropriately.
    """

    def __init__(
        self,
        base_url: str = API_BASE,
        timeout: int = API_TIMEOUT,
        max_retries: int = API_MAX_RETRIES,
        backoff_base: float = API_BACKOFF_BASE,
    ) -> None:
        self._client = HttpClient(
            base_url,
            headers=API_HEADERS,
            timeout=timeout,
            max_retries=max_retries,
            backoff_base=backoff_base,
        )

    def get(self, endpoint: str) -> Result[list[dict[str, Any]]]:
        """Fetch a JSON list from *endpoint* with exponential-backoff retry.

        Retries up to ``max_retries`` times on network errors and 429s.
        Client errors (4xx except 429) are not retried.
        """
        result = self._client.get_json(endpoint)
        if not result.ok:
            return Result.failure(result.error, error_type=result.error_type)

        body = result.data

        # UEX envelope: {"status": "ok", "data": [...]}
        if isinstance(body, dict) and body.get("status") == "ok":
            data = body.get("data")
            if isinstance(data, list):
                return Result.success(data)
            if isinstance(data, dict):
                return Result.success([data])
            if data is None:
                return Result.success([])

        # Non-envelope response or unexpected status
        if isinstance(body, dict):
            return Result.failure(
                f"Unexpected response shape (status={body.get('status')})",
                error_type="api",
            )

        # Raw list (some endpoints return bare arrays)
        if isinstance(body, list):
            return Result.success(body)

        return Result.failure("Unexpected response type", error_type="api")
