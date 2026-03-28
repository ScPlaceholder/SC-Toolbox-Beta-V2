"""Centralized HTTP client for scmdb.net API.

Uses ``shared.http_client.HttpClient`` for consistent retry/backoff.
"""
import logging
from typing import Optional

from shared.api_config import SCMDB_TIMEOUT
from shared.http_client import HttpClient

from config import SCMDB_BASE, API_HEADERS

log = logging.getLogger(__name__)

_client = HttpClient(SCMDB_BASE, headers=API_HEADERS, timeout=SCMDB_TIMEOUT)


def fetch_json(url: str, timeout: int = 30) -> Optional[dict]:
    """Fetch JSON from URL. Returns parsed dict or None on failure.

    Accepts full URLs for backward compatibility — strips the base URL
    prefix to extract the endpoint path.
    """
    if url.startswith(SCMDB_BASE):
        endpoint = url[len(SCMDB_BASE):]
    else:
        endpoint = url
    result = _client.get_json(endpoint)
    if result.ok:
        return result.data
    log.warning("Fetch error for %s: %s", url, result.error)
    return None


def fetch_versions() -> list:
    return fetch_json(f"{SCMDB_BASE}/data/versions.json") or []


def fetch_game_data(file_name: str) -> Optional[dict]:
    return fetch_json(f"{SCMDB_BASE}/data/{file_name}")


def fetch_crafting_blueprints(version: str) -> Optional[dict]:
    return fetch_json(f"{SCMDB_BASE}/data/crafting_blueprints-{version}.json")


def fetch_crafting_items(version: str) -> Optional[dict]:
    return fetch_json(f"{SCMDB_BASE}/data/crafting_items-{version}.json")


def fetch_mining_data(version: str) -> Optional[dict]:
    return fetch_json(f"{SCMDB_BASE}/data/mining_data-{version}.json")


def fetch_mining_equipment(version: str) -> Optional[dict]:
    return fetch_json(f"{SCMDB_BASE}/data/mining_equipment-{version}.json")
