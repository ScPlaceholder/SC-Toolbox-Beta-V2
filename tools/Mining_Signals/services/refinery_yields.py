"""Fetch and expose per-refinery yield bonuses from scmdb.net.

The raw data lives in the same ``mining_data-<version>.json`` blob that
:mod:`services.mining_chart_data` already caches.  We piggyback on that
cache so there's no duplicate download.

Data model
----------
``refineries``
    List of ``{name, system, profileId}`` — 19 entries (4.7.1-live).
``refineryProfiles``
    Dict ``{profileId: {mineral_name: pct, ...}}``.  Values are
    integer yield modifiers (positive = bonus, negative = penalty).
    Some refineries share profiles (e.g. HUR-L1 shares its profile
    with 7 other stations).
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from shared.cache_manager import DiskCache
from shared.errors import Result
from .http_retry import urlopen_with_retry

log = logging.getLogger(__name__)

_SCMDB_BASE = "https://scmdb.net"
_USER_AGENT = "SC-Toolbox/MiningSignals"

_TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_TOOL_DIR, ".mining_chart_cache.json")
_CACHE_VERSION = 2
_DEFAULT_TTL = 24 * 3600


# ── Data model ──────────────────────────────────────────────────


@dataclass
class RefineryEntry:
    name: str
    system: str
    profile_id: str


@dataclass
class RefineryYieldData:
    """Parsed yield data ready for the UI."""
    refineries: list[RefineryEntry] = field(default_factory=list)
    profiles: dict[str, dict[str, int]] = field(default_factory=dict)
    all_minerals: list[str] = field(default_factory=list)
    version: str = ""


# ── Helpers ─────────────────────────────────────────────────────


_SHORT_RE = re.compile(
    r"^((?:ARC|CRU|HUR|MIC)-L\d)"      # "ARC-L1" prefix
    r"|^(Terra Gateway|Pyro Gateway|Nyx Gateway|Stanton Gateway)"
    r"|^(Levski|Ruin Station|Orbituary|Checkmate)"
)


def short_name(full_name: str) -> str:
    """Compact column-header label for a refinery name.

    >>> short_name("ARC-L1 Wide Forest Station")
    'ARC-L1'
    >>> short_name("Levski")
    'Levski'
    """
    m = _SHORT_RE.match(full_name)
    if m:
        return m.group(1) or m.group(2) or m.group(3)
    # Fallback: first two words
    parts = full_name.split()
    return " ".join(parts[:2]) if len(parts) >= 2 else full_name


def shared_profile_count(
    entry: RefineryEntry,
    all_entries: list[RefineryEntry],
) -> int:
    """Return how many *other* refineries share the same profile.

    Returns 0 if the profile is unique to this refinery.
    """
    return sum(
        1 for r in all_entries
        if r.profile_id == entry.profile_id and r.name != entry.name
    )


def shared_profile_label(
    entry: RefineryEntry,
    all_entries: list[RefineryEntry],
) -> str:
    """Return e.g. ``"+7 others"`` if the profile is shared, else ``""``."""
    n = shared_profile_count(entry, all_entries)
    if n == 0:
        return ""
    return f"+{n} other{'s' if n != 1 else ''}"


# ── Loader ──────────────────────────────────────────────────────


def load_refinery_yields(force: bool = False) -> Result[RefineryYieldData]:
    """Return refinery yield data, using the mining-chart disk cache.

    The cache file is the same one written by
    :class:`services.mining_chart_data.MiningChartFetcher`; if it
    doesn't exist yet (user never opened the Mining Chart tab) we
    fetch fresh from scmdb.net and write it ourselves.
    """
    cache = DiskCache(_CACHE_FILE, cache_version=_CACHE_VERSION)

    if not force:
        cached = cache.load(ttl=_DEFAULT_TTL)
        if cached.ok and cached.data:
            raw = cached.data.get("mining_data")
            if raw:
                # The game version lives inside the mining_data blob
                # itself; the cache wrapper's "version" key is
                # overwritten by DiskCache's _cache_version stamp.
                version = raw.get("version", "")
                return Result.success(_index(raw, version))

    # Need to fetch fresh.
    try:
        versions = _fetch_json(f"{_SCMDB_BASE}/data/versions.json")
        if not versions:
            return Result.failure("Failed to fetch scmdb versions")

        target = None
        for v in versions:
            if "live" in v.get("version", "").lower():
                target = v
                break
        if not target:
            target = versions[0]

        version = target.get("version", "")
        raw = _fetch_json(f"{_SCMDB_BASE}/data/mining_data-{version}.json")
        if not raw:
            return Result.failure(f"Failed to fetch mining_data-{version}")

        cache.save({"version": version, "mining_data": raw})
        return Result.success(_index(raw, version))

    except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
        return Result.failure(f"Load error: {exc}")


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    })
    with urlopen_with_retry(req, timeout=30) as resp:
        return json.load(resp)


def _index(raw: dict, version: str) -> RefineryYieldData:
    refs_raw = raw.get("refineries", [])
    profiles_raw = raw.get("refineryProfiles", {})

    refineries = [
        RefineryEntry(
            name=r.get("name", ""),
            system=r.get("system", ""),
            profile_id=r.get("profileId", ""),
        )
        for r in refs_raw
        if r.get("name") and r.get("profileId")
    ]

    # Deduplicate profiles — keep the original dicts.
    profiles: dict[str, dict[str, int]] = {}
    for pid, yields in profiles_raw.items():
        profiles[pid] = {k: int(v) for k, v in yields.items()}

    # Collect all mineral names across every profile.
    all_minerals: set[str] = set()
    for yields in profiles.values():
        all_minerals.update(yields.keys())

    return RefineryYieldData(
        refineries=refineries,
        profiles=profiles,
        all_minerals=sorted(all_minerals),
        version=version,
    )
