"""HTTP client for the sc-craft.tools API."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote

from shared.api_config import (
    SC_CRAFT_BASE_URL,
    SC_CRAFT_HEADERS,
    SC_CRAFT_TIMEOUT,
    SC_CRAFT_VERSION,
)
from shared.errors import Result
from shared.http_client import HttpClient

log = logging.getLogger(__name__)


def _q(value: str) -> str:
    """URL-encode a query parameter value."""
    return quote(str(value), safe="")


class CraftApiClient:
    """Thin wrapper around *HttpClient* for sc-craft.tools endpoints."""

    def __init__(
        self,
        base_url: str = SC_CRAFT_BASE_URL,
        timeout: int = SC_CRAFT_TIMEOUT,
        version: str = SC_CRAFT_VERSION,
    ) -> None:
        self._http = HttpClient(
            base_url=base_url,
            headers=SC_CRAFT_HEADERS,
            timeout=timeout,
        )
        self._version = version

    # ── helpers ──────────────────────────────────────────────────────────

    def _vq(self, extra: str = "") -> str:
        """Build the version query-string fragment."""
        qs = f"version={_q(self._version)}"
        if extra:
            qs += f"&{extra}"
        return qs

    # ── public endpoints ─────────────────────────────────────────────────

    def fetch_stats(self) -> Result[dict]:
        log.debug("Fetching stats")
        r = self._http.get_json(f"/stats?{self._vq()}")
        if r.ok:
            log.debug("Stats fetched successfully")
        else:
            log.warning("Stats fetch failed: %s", r.error)
        return r

    def fetch_filter_hints(self) -> Result[dict]:
        log.debug("Fetching filter hints")
        r = self._http.get_json(f"/filter-hints?{self._vq()}")
        if r.ok:
            log.debug("Filter hints fetched successfully")
        else:
            log.warning("Filter hints fetch failed: %s", r.error)
        return r

    def fetch_blueprints(
        self,
        page: int = 1,
        limit: int = 50,
        search: str = "",
        ownable: bool | None = None,
        resource: str = "",
        mission_type: str = "",
        location: str = "",
        contractor: str = "",
        category: str = "",
    ) -> Result[dict]:
        parts = [
            f"page={page}",
            f"limit={limit}",
            f"search={_q(search)}",
        ]
        if ownable is not None:
            parts.append(f"ownable={'1' if ownable else '0'}")
        if resource:
            parts.append(f"resource={_q(resource)}")
        if mission_type:
            parts.append(f"mission_type={_q(mission_type)}")
        if location:
            parts.append(f"location={_q(location)}")
        if contractor:
            parts.append(f"contractor={_q(contractor)}")
        if category:
            parts.append(f"category={_q(category)}")

        qs = "&".join(parts)
        log.debug("Fetching blueprints page=%d search=%r", page, search)
        r = self._http.get_json(f"/blueprints?{self._vq(qs)}")
        if r.ok:
            items = r.data.get("items", []) if isinstance(r.data, dict) else []
            log.debug("Fetched %d blueprints", len(items))
        else:
            log.warning("Blueprint fetch failed: %s", r.error)
        return r

    def fetch_blueprint_detail(self, bp_id: int) -> Result[dict]:
        log.debug("Fetching blueprint detail id=%d", bp_id)
        r = self._http.get_json(f"/blueprints/{bp_id}?{self._vq()}")
        if r.ok:
            log.debug("Blueprint detail fetched: %s", r.data.get("name", "?") if isinstance(r.data, dict) else "?")
        else:
            log.warning("Blueprint detail fetch failed (id=%d): %s", bp_id, r.error)
        return r
