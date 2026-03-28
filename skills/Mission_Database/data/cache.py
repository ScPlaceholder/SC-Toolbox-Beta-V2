"""Atomic cache with version and TTL validation.

Uses ``shared.cache_manager.DiskCache`` for the core logic;
provides Mission Database-specific helpers for version-specific paths.
"""
import logging
import os
from typing import Optional

from shared.cache_manager import DiskCache

from config import CACHE_TTL, CACHE_VERSION

log = logging.getLogger(__name__)

# Mission_Database directory (parent of data/)
_CACHE_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


def cache_dir() -> str:
    """Return the Mission_Database directory where cache files live."""
    return _CACHE_DIR


def default_cache_path() -> str:
    """Return the path to the default cache file."""
    return os.path.join(_CACHE_DIR, ".scmdb_cache.json")


def version_cache_path(version_str: str) -> str:
    """Return a version-specific cache path, e.g. .scmdb_cache_4_0_2.json."""
    key = version_str.replace(".", "_").replace("-", "_")
    return os.path.join(_CACHE_DIR, f".scmdb_cache_{key}.json")


def load_cache(path: Optional[str] = None) -> Optional[dict]:
    """Load and validate a cache file.

    Returns the cached dict if it exists, has the correct cache_version,
    and is within CACHE_TTL.  Returns None otherwise.
    """
    path = path or default_cache_path()
    cache = DiskCache(path, CACHE_VERSION)
    result = cache.load(ttl=CACHE_TTL)
    if result.ok:
        return result.data
    if result.error_type != "cache_miss":
        log.warning("Cache load failed for %s: %s", path, result.error)
    return None


def save_cache(data: dict, path: Optional[str] = None) -> None:
    """Atomically write data to cache file.

    Stamps _cache_version and _ts before writing.
    """
    path = path or default_cache_path()
    cache = DiskCache(path, CACHE_VERSION)
    result = cache.save(data)
    if not result.ok:
        log.warning("Failed to write cache %s: %s", path, result.error)
