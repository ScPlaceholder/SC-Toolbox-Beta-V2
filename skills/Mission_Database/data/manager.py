"""Mission data manager -- orchestrates API, cache, and indexing."""
import logging
import os
import threading
import time
from typing import Optional

from config import MINING_GROUP_TYPES, HIDDEN_LOCATIONS, VERSION_RECHECK_INTERVAL
from data import api, cache
from services.indexing import (
    index_contracts,
    index_mining,
    get_location_resources as _get_loc_res,
)

log = logging.getLogger(__name__)


def format_as_of(ts: float) -> str:
    """Human 'as of' stamp for a cache timestamp (local time)."""
    if not ts:
        return "an unknown time"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


class MissionDataManager:
    """Fetches and caches mission data from scmdb.net."""

    HIDDEN_LOCATIONS = HIDDEN_LOCATIONS

    def __init__(self) -> None:
        self.loaded = False
        self.loading = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()

        self.version = ""
        self.contracts: list = []
        self.legacy_contracts: list = []
        self.factions: dict = {}
        self.location_pools: dict = {}
        self.ship_pools: dict = {}
        self.blueprint_pools: dict = {}
        self.scopes: dict = {}
        self.availability_pools: list = []
        self.faction_rewards_pools: list = []
        self.resource_pools: dict = {}
        self.partial_reward_pools: list = []

        # Derived lookups
        self.all_categories: list = []
        self.all_systems: list = []
        self.all_mission_types: list = []
        self.all_faction_names: list = []
        self.faction_by_guid: dict = {}
        self.min_reward = 0
        self.max_reward = 0
        self.available_versions: list = []  # [{version, file}, ...]

        # Freshness of the mission data currently held.
        #   data_source: "network" | "cache" (within TTL) | "stale" (expired
        #   cache served because scmdb.net could not be reached)
        self.data_source = ""
        self.data_as_of = 0.0
        self.notice: Optional[str] = None   # player-facing status override
        # Upstream version check (one versions.json GET per show, throttled)
        self._check_inflight = False
        self._last_check = 0.0              # time.monotonic() of last attempt

        # Crafting / Fabricator data
        self.crafting_blueprints: list = []
        self.crafting_items: list = []
        self.crafting_resources: list = []
        self.crafting_gem_items: list = []
        self.crafting_properties: dict = {}
        self.crafting_dismantle: dict = {}
        self.crafting_meta: dict = {}
        self.crafting_items_map: dict = {}
        self.crafting_items_by_name: dict = {}
        self.crafting_manufacturers: dict = {}
        self.crafting_loaded = False
        self.crafting_loading = False
        # Which game version the crafting data belongs to ("" = never tried).
        # A load that FAILED still records the version it tried, so a page
        # switch does not re-hit the network; the show-check retries it.
        self.crafting_version = ""
        self.crafting_as_of = 0.0
        self.crafting_stale = False          # served from expired cache
        self.crafting_error: Optional[str] = None   # network failure, no cache
        self._crafting_force = False

        # Mining / Resources data
        self.mining_locations: list = []
        self.mining_elements: dict = {}
        self.mining_compositions: dict = {}
        self.mining_clustering: dict = {}
        self.mining_equipment_lasers: list = []
        self.mining_equipment_modules: list = []
        self.mining_equipment_gadgets: list = []
        self.mining_loaded = False
        self.mining_loading = False

        # Derived mining lookups
        self.resource_to_locations: dict = {}
        self.location_to_resources: dict = {}
        self.all_resource_names: list = []
        self.all_location_types: list = []
        self.all_mining_systems: list = []
        self.resource_categories: dict = {}

        self.MINING_GROUP_TYPES = MINING_GROUP_TYPES

    # ------------------------------------------------------------------
    # Core load (latest LIVE or PTU)
    # ------------------------------------------------------------------

    def load(self, on_done=None, force: bool = False) -> None:
        """Load the latest mission data.

        Order: fresh cache (within CACHE_TTL) -> scmdb.net -> expired cache.
        ``force`` skips the fresh-cache step (manual refresh) but still falls
        back to the cache if scmdb.net cannot be reached, so a refresh while
        offline never empties the window.
        """
        with self._lock:
            if self.loading:
                return
            self.loading = True

        def _run():
            try:
                data = None
                source = ""
                if not force:
                    data = cache.load_cache()
                    if data:
                        source = "cache"
                if not data:
                    with self._lock:
                        self._last_check = time.monotonic()
                    fresh = self._fetch_fresh()
                    if fresh:
                        cache.save_cache(fresh)
                        data, source = fresh, "network"
                if not data:
                    data = cache.load_cache_any_age()
                    if data:
                        source = "stale"
                        log.warning("scmdb.net unreachable; serving cache as of %s",
                                    format_as_of(cache.cache_timestamp(data)))

                if not data:
                    with self._lock:
                        self.error = ("Could not reach scmdb.net and no cached "
                                      "mission data exists yet")
                    return

                if source != "network":
                    # Restore metadata that was stored in the cache
                    self.version = data.get("_scmdb_version", "")
                    self.available_versions = data.get("_versions", [])

                indexed = index_contracts(data)
                with self._lock:
                    self.error = None
                    self.data_source = source
                    self.data_as_of = cache.cache_timestamp(data)
                    self.notice = self._stale_notice() if source == "stale" else None
                self._apply_index(indexed, mark_loaded=True)

            except (OSError, KeyError, TypeError, ValueError) as exc:
                self.error = str(exc)
            finally:
                with self._lock:
                    self.loading = False
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    def _stale_notice(self) -> str:
        return ("scmdb.net unreachable -- showing cached data as of "
                f"{format_as_of(self.data_as_of)}")

    # ------------------------------------------------------------------
    # Per-show upstream check (new patch detection)
    # ------------------------------------------------------------------

    def check_for_update(self, channel: str, on_reload=None, on_checked=None,
                         force: bool = False) -> bool:
        """Ask scmdb.net (versions.json) whether *channel* has a new version.

        Called every time the window is shown and once after launch.  At most
        one check runs at a time and, unless ``force``, at most one per
        VERSION_RECHECK_INTERVAL, so rapid show/hide never hammers scmdb.net.

        * new version  -> ``load_version`` it; ``on_reload`` fires when done.
        * same version but we were serving an expired cache -> reload fresh.
        * unreachable  -> keep current data, set ``notice``; ``on_checked``.
        * current      -> clear any offline notice; ``on_checked``.

        Returns True if a check was started.
        """
        now = time.monotonic()
        with self._lock:
            if self.loading or self._check_inflight or not self.loaded:
                return False
            if (not force and self._last_check
                    and now - self._last_check < VERSION_RECHECK_INTERVAL):
                return False
            self._check_inflight = True
            self._last_check = now

        def _run():
            reload_started = False
            try:
                fresh = api.fetch_versions()
                if not fresh:
                    with self._lock:
                        self.notice = ("scmdb.net unreachable -- showing cached data "
                                       f"as of {format_as_of(self.data_as_of)}")
                    return
                chan = (channel or "live").lower()
                new_ver = ""
                for v in fresh:
                    ver = v.get("version", "")
                    if chan in ver.lower():
                        new_ver = ver
                        break
                with self._lock:
                    self.available_versions = fresh
                    stale = self.data_source == "stale"
                    current = self.version
                if new_ver and new_ver != current:
                    log.info("scmdb.net has a new version: %s (was %s)", new_ver, current)
                    reload_started = True
                    self.load_version(new_ver, on_done=on_reload)
                elif stale:
                    reload_started = True
                    # Back online on the same version: replace the expired cache.
                    self.load(on_done=on_reload, force=True)
                else:
                    with self._lock:
                        self.notice = None
            except (OSError, KeyError, TypeError, ValueError) as exc:
                log.warning("version check failed: %s", exc)
            finally:
                with self._lock:
                    self._check_inflight = False
                if not reload_started and on_checked:
                    on_checked()

        threading.Thread(target=_run, daemon=True).start()
        return True

    def crafting_needs_load(self) -> bool:
        """True if crafting data was never attempted for the current version."""
        with self._lock:
            return bool(self.version) and self.crafting_version != self.version

    def crafting_needs_retry(self) -> bool:
        """True if the crafting data we hold is not a fresh copy (failed/stale)."""
        with self._lock:
            return (self.crafting_version == self.version and bool(self.version)
                    and (self.crafting_stale or self.crafting_error is not None))

    def invalidate_crafting(self, force_network: bool = False) -> None:
        """Mark crafting data as needing a reload (e.g. manual refresh)."""
        with self._lock:
            self.crafting_version = ""
            self._crafting_force = force_network

    def _fetch_fresh(self, prefer: str = "live") -> Optional[dict]:
        """Fetch versions.json then the preferred merged data (live or ptu)."""
        versions = api.fetch_versions()
        if not versions:
            return None
        self.available_versions = versions

        target = None
        for v in versions:
            ver = v.get("version", "")
            if prefer.lower() in ver.lower():
                target = v
                break
        if not target:
            target = versions[0] if versions else None
        if not target:
            return None

        self.version = target.get("version", "")
        file_name = target.get("file", "")
        if not file_name:
            return None

        data = api.fetch_game_data(file_name)
        if data:
            data["_scmdb_version"] = self.version
            data["_versions"] = versions
        return data

    # ------------------------------------------------------------------
    # Version-specific load
    # ------------------------------------------------------------------

    def load_version(self, version_str: str, on_done=None) -> None:
        """Load a specific game version (e.g. '4.7.0-ptu...' or '4.6.0-live...')."""
        ver_cache_path = cache.version_cache_path(version_str)

        def _find(versions):
            for v in versions or []:
                if v.get("version") == version_str:
                    return v
            return None

        def _run():
            try:
                # Try version-specific cache first
                data = cache.load_cache(ver_cache_path)
                source = "cache" if data else ""
                failure = ""

                if not data:
                    # Find the file for this version.  The in-memory list may
                    # predate the version (e.g. restored from an old cache),
                    # so ask scmdb.net once if it is not listed.
                    versions = self.available_versions
                    target = _find(versions)
                    if not target:
                        versions = api.fetch_versions() or versions
                        target = _find(versions)
                        if versions:
                            self.available_versions = versions
                    if not target:
                        failure = f"Version {version_str} not found"
                    else:
                        file_name = target.get("file", "")
                        data = api.fetch_game_data(file_name) if file_name else None
                        if data:
                            data["_scmdb_version"] = version_str
                            data["_versions"] = versions
                            cache.save_cache(data, ver_cache_path)
                            source = "network"
                        else:
                            failure = f"Failed to fetch {version_str}"

                if not data:
                    data = cache.load_cache_any_age(ver_cache_path)
                    if data:
                        source = "stale"

                if not data:
                    with self._lock:
                        if had_data:
                            # Keep showing what we had rather than blanking it.
                            self.loaded = True
                            self.error = None
                            self.notice = (f"{failure} from scmdb.net -- still "
                                           f"showing {self.version}")
                        else:
                            self.error = failure or f"Failed to fetch {version_str}"
                    return

                self.version = version_str
                indexed = index_contracts(data)
                with self._lock:
                    self.error = None
                    self.data_source = source
                    self.data_as_of = cache.cache_timestamp(data)
                    self.notice = self._stale_notice() if source == "stale" else None
                self._apply_index(indexed, mark_loaded=True)

            except (OSError, KeyError, TypeError, ValueError) as exc:
                self.error = str(exc)
            finally:
                with self._lock:
                    self.loading = False
                if on_done:
                    on_done()

        with self._lock:
            had_data = self.loaded
            self.loading = True
            self.loaded = False
        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Crafting / Fabricator data
    # ------------------------------------------------------------------

    def load_crafting(self, on_done=None, force: bool = False) -> None:
        """Load crafting_blueprints + crafting_items for the current version.

        Order: fresh per-version cache (within CACHE_TTL) -> scmdb.net ->
        expired per-version cache.  A network failure is recorded in
        ``crafting_error`` (never reported as "no data for this version"),
        and an expired cache is flagged ``crafting_stale`` with its time.
        """
        with self._lock:
            if self.crafting_loading:
                log.debug("load_crafting: already in progress, skipping")
                return
            self.crafting_loading = True
            force = force or self._crafting_force
            self._crafting_force = False

        ver = self.version
        if not ver:
            with self._lock:
                self.crafting_loading = False
            if on_done:
                on_done()
            return

        def _run():
            try:
                path = cache.crafting_cache_path(ver)
                bp_data = items_data = None
                source = ""
                net_error: Optional[str] = None
                as_of = 0.0

                cached = None if force else cache.load_cache(path)
                if cached and cached.get("_scmdb_version") == ver:
                    bp_data, items_data = cached.get("bp"), cached.get("items")
                    source = "cache"
                    as_of = cache.cache_timestamp(cached)
                else:
                    bp_res = api.fetch_crafting_blueprints_result(ver)
                    if bp_res.ok and bp_res.data:
                        items_res = api.fetch_crafting_items_result(ver)
                        if items_res.ok:
                            bp_data, items_data = bp_res.data, items_res.data
                            source = "network"
                            cache.save_cache({"_scmdb_version": ver, "bp": bp_data,
                                              "items": items_data}, path)
                        else:
                            net_error = items_res.error or "crafting items fetch failed"
                    elif not api.is_not_found(bp_res):
                        net_error = bp_res.error or "crafting blueprints fetch failed"

                    if source != "network":
                        stale = cache.load_cache_any_age(path)
                        if stale and stale.get("_scmdb_version") == ver:
                            bp_data, items_data = stale.get("bp"), stale.get("items")
                            source = "stale"
                            as_of = cache.cache_timestamp(stale)
                        elif net_error and bp_res.ok and bp_res.data:
                            # Blueprints arrived, item names did not and there is
                            # no cache: show blueprints (names fall back) and say so.
                            bp_data, source = bp_res.data, "partial"
                    if source == "network":
                        as_of = time.time()

                # Build all data in local variables first
                _blueprints = bp_data.get("blueprints", []) if bp_data else []
                _resources = bp_data.get("resources", []) if bp_data else []
                _gem_items = bp_data.get("items", []) if bp_data else []
                _properties = bp_data.get("properties", {}) if bp_data else {}
                _dismantle = bp_data.get("dismantle", {}) if bp_data else {}
                _meta = bp_data.get("meta", {}) if bp_data else {}

                _items = items_data.get("items", []) if items_data else []
                _manufacturers = items_data.get("manufacturers", {}) if items_data else {}
                _items_map = {}
                _items_by_name = {}
                for item in _items:
                    ec = item.get("entityClass", "")
                    if ec:
                        _items_map[ec] = item
                    name = item.get("name", "")
                    if name:
                        _items_by_name[name] = item

                _loaded = bool(bp_data)

                # Atomically swap under lock
                with self._lock:
                    self.crafting_version = ver
                    self.crafting_as_of = as_of
                    self.crafting_stale = source in ("stale", "partial")
                    self.crafting_error = net_error if source in ("", "partial") else None
                    self.crafting_blueprints = _blueprints
                    self.crafting_resources = _resources
                    self.crafting_gem_items = _gem_items
                    self.crafting_properties = _properties
                    self.crafting_dismantle = _dismantle
                    self.crafting_meta = _meta
                    self.crafting_items = _items
                    self.crafting_manufacturers = _manufacturers
                    self.crafting_items_map = _items_map
                    self.crafting_items_by_name = _items_by_name
                    self.crafting_loaded = _loaded
                    if not bp_data and items_data:
                        self.crafting_items = []
                        self.crafting_items_map = {}
                        self.crafting_items_by_name = {}

            except (OSError, KeyError, TypeError, ValueError) as exc:
                log.warning("load_crafting error: %s", exc)
                with self._lock:
                    self.crafting_loaded = False
                    self.crafting_version = ver
                    self.crafting_error = str(exc)
            finally:
                with self._lock:
                    self.crafting_loading = False
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Mining / Resources data
    # ------------------------------------------------------------------

    def load_mining(self, on_done=None) -> None:
        """Fetch mining_data and mining_equipment JSONs for the current version."""
        with self._lock:
            if self.mining_loading:
                return
            self.mining_loading = True

        ver = self.version
        if not ver:
            with self._lock:
                self.mining_loading = False
            if on_done:
                on_done()
            return

        def _run():
            try:
                mining_data = api.fetch_mining_data(ver)
                equip_data = api.fetch_mining_equipment(ver)

                # Build in locals
                _locations = mining_data.get("locations", []) if mining_data else []
                _elements = mining_data.get("mineableElements", {}) if mining_data else {}
                _compositions = mining_data.get("compositions", {}) if mining_data else {}
                _clustering = mining_data.get("clusteringPresets", {}) if mining_data else {}
                _lasers = equip_data.get("lasers", []) if equip_data else []
                _modules = equip_data.get("modules", []) if equip_data else []
                _gadgets = equip_data.get("gadgets", []) if equip_data else []

                # Swap raw data atomically under lock
                with self._lock:
                    self.mining_locations = _locations
                    self.mining_elements = _elements
                    self.mining_compositions = _compositions
                    self.mining_clustering = _clustering
                    self.mining_equipment_lasers = _lasers
                    self.mining_equipment_modules = _modules
                    self.mining_equipment_gadgets = _gadgets

                # Index mining data using the services module
                if mining_data:
                    indexed = index_mining(_locations, _compositions)
                    self._apply_mining_index(indexed)

                with self._lock:
                    self.mining_loaded = bool(mining_data and _locations)

            except (OSError, KeyError, TypeError, ValueError) as exc:
                log.warning("load_mining error: %s", exc)
                with self._lock:
                    self.mining_loaded = False
            finally:
                with self._lock:
                    self.mining_loading = False
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Index application helpers
    # ------------------------------------------------------------------

    def _apply_index(self, indexed: dict, mark_loaded: bool = False):
        """Atomically swap all indexed contract data under lock."""
        with self._lock:
            self.contracts = indexed["contracts"]
            self.legacy_contracts = indexed["legacy_contracts"]
            self.factions = indexed["factions"]
            self.location_pools = indexed["location_pools"]
            self.ship_pools = indexed["ship_pools"]
            self.blueprint_pools = indexed["blueprint_pools"]
            self.scopes = indexed["scopes"]
            self.availability_pools = indexed["availability_pools"]
            self.faction_rewards_pools = indexed["faction_rewards_pools"]
            self.resource_pools = indexed["resource_pools"]
            self.partial_reward_pools = indexed["partial_reward_pools"]
            self.faction_by_guid = indexed["faction_by_guid"]
            self.all_categories = indexed["all_categories"]
            self.all_systems = indexed["all_systems"]
            self.all_mission_types = indexed["all_mission_types"]
            self.all_faction_names = indexed["all_faction_names"]
            self.min_reward = indexed["min_reward"]
            self.max_reward = indexed["max_reward"]
            if mark_loaded:
                self.loaded = True
                self.loading = False

    def _apply_mining_index(self, indexed: dict):
        """Atomically swap mining index data under lock."""
        with self._lock:
            self.resource_to_locations = indexed["resource_to_locations"]
            self.location_to_resources = indexed["location_to_resources"]
            self.all_resource_names = indexed["all_resource_names"]
            self.all_location_types = indexed["all_location_types"]
            self.all_mining_systems = indexed["all_mining_systems"]
            self.resource_categories = indexed["resource_categories"]

    # ------------------------------------------------------------------
    # Getters
    # ------------------------------------------------------------------

    def get_faction(self, guid: str) -> dict:
        return self.faction_by_guid.get(guid, {})

    def get_location(self, guid: str) -> dict:
        return self.location_pools.get(guid, {})

    def get_availability(self, idx) -> dict:
        try:
            return self.availability_pools[idx]
        except (IndexError, TypeError):
            return {}

    def get_blueprint_product(self, bp: dict) -> Optional[dict]:
        """Get the crafting_items entry for a blueprint's product."""
        ec = bp.get("productEntityClass", "")
        return self.crafting_items_map.get(ec)

    def get_blueprint_product_name(self, bp: dict) -> str:
        """Get display name for a blueprint product."""
        prod = self.get_blueprint_product(bp)
        if prod:
            return prod.get("name", bp.get("productName", bp.get("tag", "?")))
        return bp.get("productName", bp.get("tag", "?"))

    def get_location_resources(self, loc_name: str) -> list:
        """Get deduplicated resources for a location, sorted by max_pct desc."""
        return _get_loc_res(self.location_to_resources, loc_name)

    # ------------------------------------------------------------------
    # Thread-safe state checks and setters
    # ------------------------------------------------------------------

    def is_crafting_loaded(self) -> bool:
        with self._lock:
            return self.crafting_loaded

    def is_mining_loaded(self) -> bool:
        with self._lock:
            return self.mining_loaded

    def is_data_loaded(self) -> bool:
        with self._lock:
            return self.loaded

    def is_data_loading(self) -> bool:
        with self._lock:
            return self.loading

    def set_crafting_loaded(self, value: bool) -> None:
        with self._lock:
            self.crafting_loaded = value

    def set_loaded(self, value: bool) -> None:
        with self._lock:
            self.loaded = value
