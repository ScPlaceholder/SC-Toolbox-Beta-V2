"""Fetch mining signal data from Google Sheets and cache locally.

Downloads the published spreadsheet as CSV, parses it into a list of
resource dicts, and writes the result to a DiskCache-backed JSON file.
"""

from __future__ import annotations

import csv
import io
import logging
import os
import time
import urllib.error
import urllib.request

from shared.cache_manager import DiskCache
from shared.errors import Result
from .http_retry import urlopen_with_retry

log = logging.getLogger(__name__)

SHEET_ID = "1n4qqOfwbtsOubUTMWJ532pBGoFbx567S"
GID = "1407052029"
EXPORT_URL = (
    f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
    f"/export?format=csv&gid={GID}"
)

_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".signals_cache.json",
)
_CACHE_VERSION = 1
_DEFAULT_TTL = 3600  # 1 hour

# Additional rows not in the spreadsheet
_EXTRA_ROWS: list[dict] = [
    {"name": "ROC Mineables",  "rarity": "ROC",     "1": 4000,  "2": 8000,  "3": 12000, "4": 16000, "5": 20000, "6": 24000, "7": 28000},
    {"name": "FPS Mineables",  "rarity": "FPS",     "1": 3000,  "2": 6000,  "3": 9000,  "4": 12000, "5": 15000, "6": 18000, "7": 21000, "8": 24000, "9": 27000, "10": 30000},
    {"name": "Salvage",        "rarity": "Salvage", "1": 2000,  "2": 4000,  "3": 6000,  "4": 8000,  "5": 10000, "6": 12000, "7": 14000, "8": 16000, "9": 18000, "10": 20000, "11": 22000, "12": 24000, "13": 26000, "14": 28000, "15": 30000},
]


class SheetFetcher:
    """Downloads and caches mining signal data from Google Sheets."""

    def __init__(self, ttl: int = _DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._cache = DiskCache(_CACHE_FILE, cache_version=_CACHE_VERSION)

    def load(self, force_refresh: bool = False) -> Result[list[dict]]:
        """Return signal data, using cache when valid.

        Returns a list of dicts like::

            [{"name": "Ice", "rarity": "Common",
              "1": 4300, "2": 8600, ...}, ...]
        """
        if not force_refresh:
            cached = self._cache.load(ttl=self._ttl)
            if cached.ok and cached.data:
                rows = cached.data.get("rows")
                if rows:
                    log.debug("sheet_fetcher: serving %d rows from cache", len(rows))
                    return Result.success(rows + _EXTRA_ROWS)

        result = self._fetch_and_cache()
        if result.ok and result.data:
            return Result.success(result.data + _EXTRA_ROWS)
        return result

    def _fetch_and_cache(self) -> Result[list[dict]]:
        """Download CSV from Google Sheets and write to cache."""
        log.info("sheet_fetcher: fetching CSV from Google Sheets")
        try:
            req = urllib.request.Request(
                EXPORT_URL,
                headers={"User-Agent": "SC-Toolbox/1.0"},
            )
            with urlopen_with_retry(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8-sig")
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            log.warning("sheet_fetcher: download failed: %s", exc)
            # Fall back to stale cache (ignore TTL)
            stale = self._cache.load(ttl=10 * 365 * 86400)
            if stale.ok and stale.data:
                rows = stale.data.get("rows", [])
                if rows:
                    log.info("sheet_fetcher: using stale cache (%d rows)", len(rows))
                    return Result.success(rows)
            return Result.failure(f"Download failed: {exc}", "network")

        rows = self._parse_csv(raw)
        if not rows:
            return Result.failure("No data parsed from CSV", "parse")

        self._cache.save({"rows": rows})
        log.info("sheet_fetcher: cached %d rows", len(rows))
        return Result.success(rows)

    @staticmethod
    def _parse_csv(raw: str) -> list[dict]:
        """Parse CSV text into a list of resource dicts."""
        reader = csv.DictReader(io.StringIO(raw))
        rows: list[dict] = []
        for row in reader:
            try:
                entry: dict = {
                    "name": row.get("RockName", "").strip(),
                    "rarity": row.get("Rarity", "").strip(),
                }
                for col in ("1", "2", "3", "4", "5", "6"):
                    val = row.get(col, "0").strip()
                    entry[col] = int(val) if val else 0
                if entry["name"]:
                    rows.append(entry)
            except (ValueError, KeyError) as exc:
                log.debug("sheet_fetcher: skipping malformed row: %s", exc)
        return rows
