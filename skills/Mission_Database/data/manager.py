"""Mission data manager -- orchestrates API, cache, and indexing."""
import logging
import os
import threading
import time
from typing import Optional

from config import MINING_GROUP_TYPES, HIDDEN_LOCATIONS
from data import api, cache
from services.indexing import (
    index_contracts,
    index_mining,
    get_location_resources as _get_loc_res,
)

log = logging.getLogger(__name__)


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

    def load(self, on_done=None) -> None:
        with self._lock:
            if self.loading:
                return
            self.loading = True

        def _run():
            try:
                data = cache.load_cache()
                if data:
                    # Restore metadata that the original code stored in cache
                    self.version = data.get("_scmdb_version", "")
                    self.available_versions = data.get("_versions", [])
                else:
                    data = self._fetch_fresh()
                    if data:
                        cache.save_cache(data)

                if not data:
                    self.error = "Failed to fetch mission data"
                    return

                indexed = index_contracts(data)
                self._apply_index(indexed, mark_loaded=True)

            except (OSError, KeyError, TypeError, ValueError) as exc:
                self.error = str(exc)
                with self._lock:
                    self.loading = False
            finally:
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

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

        def _run():
            try:
                # Try version-specific cache first
                data = cache.load_cache(ver_cache_path)

                if not data:
                    # Find the file for this version
                    versions = self.available_versions or api.fetch_versions()
                    target = None
                    for v in versions:
                        if v.get("version") == version_str:
                            target = v
                            break
                    if not target:
                        self.error = f"Version {version_str} not found"
                        return

                    file_name = target.get("file", "")
                    data = api.fetch_game_data(file_name)
                    if data:
                        data["_scmdb_version"] = version_str
                        data["_versions"] = versions
                        cache.save_cache(data, ver_cache_path)

                if not data:
                    self.error = f"Failed to fetch {version_str}"
                    return

                self.version = version_str
                indexed = index_contracts(data)
                self._apply_index(indexed, mark_loaded=True)

                with self._lock:
                    self.error = None

            except (OSError, KeyError, TypeError, ValueError) as exc:
                self.error = str(exc)
                with self._lock:
                    self.loading = False
            finally:
                if on_done:
                    on_done()

        with self._lock:
            self.loading = True
            self.loaded = False
        threading.Thread(target=_run, daemon=True).start()

    # ------------------------------------------------------------------
    # Crafting / Fabricator data
    # ------------------------------------------------------------------

    def load_crafting(self, on_done=None) -> None:
        """Fetch crafting_blueprints and crafting_items JSONs for the current version."""
        with self._lock:
            if self.crafting_loading:
                return
            self.crafting_loading = True

        ver = self.version
        if not ver:
            with self._lock:
                self.crafting_loading = False
            if on_done:
                on_done()
            return

        def _run():
            try:
                bp_data = api.fetch_crafting_blueprints(ver)
                items_data = api.fetch_crafting_items(ver)

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
