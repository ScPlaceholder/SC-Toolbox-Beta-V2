"""Fetch and index mining location data from scmdb.net (same source the
Mission Database skill uses). Produces a Regolith-style chart data model
with a hierarchical location list and resource percentages split into
"ship mining" and "FPS / ground-vehicle" groups.

All HTTP + disk IO is isolated in this module so the UI layer only sees
a simple data structure (``MiningChartData``).
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
import json
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from shared.cache_manager import DiskCache
from shared.errors import Result
from .http_retry import urlopen_with_retry

log = logging.getLogger(__name__)

_SCMDB_BASE = "https://scmdb.net"
_USER_AGENT = "SC-Toolbox/MiningSignals"

# Cache files live in the Mining_Signals tool directory, alongside the
# other ``.*_cache.json`` files already tracked by git.
_TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CACHE_FILE = os.path.join(_TOOL_DIR, ".mining_chart_cache.json")
_CACHE_VERSION = 2
_DEFAULT_TTL = 24 * 3600  # 24 hours

# Locations to hide from the chart (matches Mission_Database config.HIDDEN_LOCATIONS)
_HIDDEN = frozenset({
    "Akiro Cluster", "Pyro Belt (Cool 1)", "Pyro Belt (Cool 2)",
    "Pyro Belt (Warm 1)", "Pyro Belt (Warm 2)", "Lagrange G",
    "Lagrange (Occupied)", "Asteroid Cluster (Low Yield)",
    "Asteroid Cluster (Medium Yield)", "Ship Graveyard", "Space Derelict",
})

# Order locations are displayed within a system.
_TYPE_ORDER = {
    "planet": 0, "moon": 1, "lagrange": 2, "belt": 3,
    "cluster": 4, "cave": 5, "special": 6, "event": 7,
}

# Ship-mining groups (blue columns in the chart)
_SHIP_GROUPS = {"SpaceShip_Mineables", "SpaceShip_Mineables_Rare"}
# FPS / ground-vehicle groups (gold columns in the chart)
_FPS_GROUPS = {"FPS_Mineables", "GroundVehicle_Mineables"}


# ─────────────────────────────────────────────────────────────────────────────
# Public data types
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class LocationRow:
    """A single row in the chart."""
    name: str
    system: str
    loc_type: str                # planet / moon / lagrange / …
    depth: int                   # indent level (0 = system, 1 = planet/moon)
    parent: Optional[str] = None # planet name for moons/mining bases
    # resource_name -> max_pct (0..100)
    ship_resources: dict[str, float] = field(default_factory=dict)
    fps_resources: dict[str, float] = field(default_factory=dict)


@dataclass
class MiningChartData:
    version: str = ""
    rows: list[LocationRow] = field(default_factory=list)
    # Column order: deterministic, grouped by ship/FPS then alphabetical.
    ship_columns: list[str] = field(default_factory=list)
    fps_columns: list[str] = field(default_factory=list)
    error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Fetcher
# ─────────────────────────────────────────────────────────────────────────────


class MiningChartFetcher:
    """Downloads scmdb.net mining data and produces a ``MiningChartData``."""

    def __init__(self, ttl: int = _DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._cache = DiskCache(_CACHE_FILE, cache_version=_CACHE_VERSION)

    # ── public ──

    def load(self, force_refresh: bool = False) -> Result[MiningChartData]:
        """Return mining chart data, using cache when valid."""
        if not force_refresh:
            cached = self._cache.load(ttl=self._ttl)
            if cached.ok and cached.data:
                raw = cached.data.get("mining_data")
                version = cached.data.get("version", "")
                if raw:
                    log.debug("mining_chart: serving from cache (%s)", version)
                    return Result.success(self._index(raw, version))

        return self._fetch_and_cache()

    # ── internals ──

    def _fetch_and_cache(self) -> Result[MiningChartData]:
        try:
            versions = self._fetch_json(f"{_SCMDB_BASE}/data/versions.json")
            if not versions:
                return Result.failure("Failed to fetch scmdb versions list")

            # Prefer first 'live' entry; fall back to first entry.
            target = None
            for v in versions:
                if "live" in v.get("version", "").lower():
                    target = v
                    break
            if not target:
                target = versions[0]

            version = target.get("version", "")
            file_name = f"mining_data-{version}.json"
            raw = self._fetch_json(f"{_SCMDB_BASE}/data/{file_name}")
            if not raw:
                return Result.failure(f"Failed to fetch {file_name}")

            # Cache the raw payload so re-indexing is cheap.
            self._cache.save({"version": version, "mining_data": raw})
            return Result.success(self._index(raw, version))

        except urllib.error.URLError as exc:
            return Result.failure(f"Network error: {exc}")
        except (OSError, ValueError, KeyError) as exc:
            return Result.failure(f"Load error: {exc}")

    @staticmethod
    def _fetch_json(url: str) -> Optional[dict]:
        req = urllib.request.Request(url, headers={
            "User-Agent": _USER_AGENT,
            "Accept": "application/json",
        })
        with urlopen_with_retry(req, timeout=30) as resp:
            return json.load(resp)

    # ── indexing ──

    @staticmethod
    def _index(raw: dict, version: str) -> MiningChartData:
        """Build the row list and column order from a raw mining_data blob."""
        locations = raw.get("locations", []) or []
        compositions = raw.get("compositions", {}) or {}

        # location_name -> LocationRow
        rows_by_name: dict[str, LocationRow] = {}
        # (system, type) -> list[name] (preserves insertion order)
        system_buckets: dict[tuple[str, str], list[str]] = defaultdict(list)

        ship_cols_set: set[str] = set()
        fps_cols_set: set[str] = set()

        for loc in locations:
            loc_name = loc.get("locationName", "")
            if not loc_name or loc_name in _HIDDEN:
                continue

            loc_type = loc.get("locationType", "")
            system = loc.get("system", "")

            # Depth heuristic: planets/belts/lagrange/clusters sit at depth 1.
            # Moons, mining-base presets, and cave presets hang under a planet
            # when we can infer one from the preset file name.
            depth = 1

            row = LocationRow(
                name=loc_name,
                system=system,
                loc_type=loc_type,
                depth=depth,
            )

            for group in loc.get("groups", []):
                grp_name = group.get("groupName", "")
                if grp_name not in _SHIP_GROUPS and grp_name not in _FPS_GROUPS:
                    continue

                deposits = group.get("deposits", [])
                total_prob = sum(d.get("relativeProbability", 0) for d in deposits)
                if total_prob <= 0:
                    continue

                for dep in deposits:
                    comp_guid = dep.get("compositionGuid", "")
                    comp = compositions.get(comp_guid, {})
                    dep_pct = (dep.get("relativeProbability", 0) / total_prob) * 100.0

                    for part in comp.get("parts", []):
                        elem_name = part.get("elementName", "")
                        if not elem_name:
                            continue
                        # Strip the "(Ore)" / "(Raw)" suffixes for a compact header.
                        clean = elem_name
                        for suffix in (" (Ore)", " (Raw)", " (Gem)"):
                            clean = clean.replace(suffix, "")

                        target = row.ship_resources if grp_name in _SHIP_GROUPS else row.fps_resources
                        cols_set = ship_cols_set if grp_name in _SHIP_GROUPS else fps_cols_set
                        # Keep the highest pct if the element appears twice.
                        prev = target.get(clean, 0.0)
                        if dep_pct > prev:
                            target[clean] = dep_pct
                        cols_set.add(clean)

            # Only keep rows that actually contain something (avoids empty
            # salvage-only or harvestable-only locations cluttering the chart).
            if not row.ship_resources and not row.fps_resources:
                continue

            rows_by_name[loc_name] = row
            system_buckets[(system, loc_type)].append(loc_name)

        # Build the final ordered row list: system header → locations grouped
        # by location type in a sensible order.
        ordered: list[LocationRow] = []
        systems = sorted({k[0] for k in system_buckets.keys()})
        for system in systems:
            # Synthetic "system" row (depth 0) so the chart can show a header.
            ordered.append(LocationRow(
                name=system, system=system, loc_type="system", depth=0,
            ))
            # Pull this system's (type, names) tuples in the preferred order.
            type_keys = sorted(
                (t for (s, t) in system_buckets.keys() if s == system),
                key=lambda t: _TYPE_ORDER.get(t, 99),
            )
            for t in type_keys:
                for name in system_buckets[(system, t)]:
                    ordered.append(rows_by_name[name])

        ship_columns = sorted(ship_cols_set)
        fps_columns = sorted(fps_cols_set)

        return MiningChartData(
            version=version,
            rows=ordered,
            ship_columns=ship_columns,
            fps_columns=fps_columns,
        )
