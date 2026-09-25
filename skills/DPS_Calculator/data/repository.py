"""Refactored DataManager -- now ComponentRepository.

Uses ErkulApiClient / FleetyardsApiClient for HTTP,
DiskCache / FleetyardsCache for persistence,
and delegates compute functions to services.*.
"""
import json
import logging
import threading
import time
from typing import Optional

import requests

from data.api_client import ErkulApiClient, FleetyardsApiClient
from data.cache import DiskCache, FleetyardsCache
from data.scunpacked_repository import ScunpackedRepository
from data.source import use_scunpacked, erkul_network_allowed, ErkulNetworkDisabled
from services.dps_calculator import compute_weapon_stats
from services.stat_computation import (
    compute_shield_stats,
    compute_cooler_stats,
    compute_radar_stats,
    compute_missile_stats,
    compute_powerplant_stats_erkul,
    compute_qdrive_stats_erkul,
    compute_missile_rack_stats,
    compute_mount_stats,
    compute_emp_stats,
    compute_qed_stats,
    compute_bomb_stats,
    compute_turret_stats,
    compute_mining_laser_stats,
    compute_tool_arm_stats,
    compute_salvage_head_stats,
    compute_mining_modifier_stats,
    compute_salvage_modifier_stats,
    compute_ore_pod_stats,
    compute_fuel_tank_stats,
    compute_erkul_module_stats,
)
import os
import re

from shared.api_config import (
    ERKUL_BASE_URL, ERKUL_HEADERS,
    FLEETYARDS_BASE_URL, FLEETYARDS_HEADERS,
    CACHE_TTL_ERKUL, CACHE_TTL_CARGO,
)
from shared.data_enrichment import enrich_component_stats

# Inline constants to avoid data -> ui dependency
_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
API_BASE    = ERKUL_BASE_URL
API_HEADERS = ERKUL_HEADERS
FY_BASE    = FLEETYARDS_BASE_URL
FY_HEADERS = FLEETYARDS_HEADERS
CACHE_FILE       = os.path.join(_DATA_DIR, ".erkul_cache.json")
CACHE_TTL        = CACHE_TTL_ERKUL
CACHE_VERSION    = 7
FY_HP_CACHE_FILE = os.path.join(_DATA_DIR, ".fy_hardpoints_cache.json")
FY_HP_TTL        = CACHE_TTL_CARGO
SUPPLEMENT_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "erkul_supplement.json")


def _fy_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _fy_hp_group(fy_list: list) -> dict:
    groups: dict = {}
    for hp in (fy_list or []):
        # FleetYards' current API names the group "category" (main_thrusters,
        # retro_thrusters, maneuvering_thrusters); older responses used "type"
        t = hp.get("type") or hp.get("category") or "unknown"
        groups.setdefault(t, []).append(hp)
    return groups

_log = logging.getLogger(__name__)


# ── Snapshot ──────────────────────────────────────────────────────────────────

class _IndexSnapshot:
    """Immutable-ish container for all ComponentRepository index dicts.

    Built in the background thread and swapped in via a single atomic
    reference assignment (``self._idx = snap``) so that readers never
    see a half-populated state -- no lock required on the read path.
    """
    __slots__ = ('weapons_by_ref', 'weapons_by_name', 'shields_by_ref', 'shields_by_name',
                 'coolers_by_ref', 'coolers_by_name', 'radars_by_ref', 'radars_by_name',
                 'missiles_by_ref', 'missiles_by_name', 'powerplants_by_ref', 'powerplants_by_name',
                 'qdrives_by_ref', 'qdrives_by_name', 'ships_by_name',
                 'by_local_name', 'raw_by_local_name', 'raw_by_ref',
                 # scunpacked-sourced component indexes
                 'thrusters_by_ref', 'thrusters_by_name', 'thrusters_by_local_name',
                 'cmls_by_ref', 'cmls_by_name', 'cmls_by_local_name',
                 # new Erkul endpoints
                 'missile_racks_by_ref', 'missile_racks_by_name',
                 'mounts_by_ref',        'mounts_by_name',
                 'emps_by_ref',          'emps_by_name',
                 'qeds_by_ref',          'qeds_by_name',
                 'bombs_by_ref',         'bombs_by_name',
                 'turrets_by_ref',       'turrets_by_name',
                 'mining_lasers_by_ref', 'mining_lasers_by_name',
                 # /live/utilities
                 'tool_arms_by_ref',          'tool_arms_by_name',
                 'salvage_heads_by_ref',      'salvage_heads_by_name',
                 'mining_modifiers_by_ref',   'mining_modifiers_by_name',
                 'salvage_modifiers_by_ref',  'salvage_modifiers_by_name',
                 'ore_pods_by_ref',           'ore_pods_by_name',
                 'fuel_tanks_by_ref',         'fuel_tanks_by_name',
                 # /live/modules
                 'erkul_modules_by_ref',      'erkul_modules_by_name')

    def __init__(self):
        for s in self.__slots__:
            setattr(self, s, {})


# ── Repository ────────────────────────────────────────────────────────────────

class ComponentRepository:
    """Drop-in replacement for the old DataManager.

    Public API is identical: all properties, find_* methods, *_for_size
    methods, fetch_fy_hardpoints, load(), get_ship_names(), get_ship_data().
    """

    def __init__(self):
        self.raw: dict = {}          # endpoint -> raw list
        self.loaded  = False
        self.loading = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()
        self._cancel = threading.Event()

        # Track cached game version for auto-update detection
        self.cached_game_version: str = ""

        # Indexed dicts live inside an _IndexSnapshot so readers can access
        # a consistent set without acquiring _lock.
        self._idx = _IndexSnapshot()

        # API clients
        self._erkul = ErkulApiClient(API_BASE, API_HEADERS)
        self._fy_api = FleetyardsApiClient(FY_BASE, FY_HEADERS)

        # Caches
        self._cache = DiskCache(CACHE_FILE, CACHE_TTL, CACHE_VERSION)
        self._fy_cache = FleetyardsCache(FY_HP_CACHE_FILE, FY_HP_TTL)

        # scunpacked-data repository (thrusters, CMLs) -- legacy erkul path only
        self._scunpacked = ScunpackedRepository()

        # scunpacked-data window provider (the default source; data/source.py)
        self.source = "scunpacked" if use_scunpacked() else "erkul"
        self.source_info: dict = {}          # build, commit, source, fetched_at
        self._sc_adapter_weapons: dict = {}  # cls -> adapter weapon record
        self._sc_weapons_by_cls: dict = {}   # cls -> window weapon row

    # ── Backward-compatible property accessors into _IndexSnapshot ────────

    @property
    def weapons_by_ref(self):     return self._idx.weapons_by_ref
    @weapons_by_ref.setter
    def weapons_by_ref(self, v):  self._idx.weapons_by_ref = v
    @property
    def weapons_by_name(self):    return self._idx.weapons_by_name
    @weapons_by_name.setter
    def weapons_by_name(self, v): self._idx.weapons_by_name = v
    @property
    def shields_by_ref(self):     return self._idx.shields_by_ref
    @shields_by_ref.setter
    def shields_by_ref(self, v):  self._idx.shields_by_ref = v
    @property
    def shields_by_name(self):    return self._idx.shields_by_name
    @shields_by_name.setter
    def shields_by_name(self, v): self._idx.shields_by_name = v
    @property
    def coolers_by_ref(self):     return self._idx.coolers_by_ref
    @coolers_by_ref.setter
    def coolers_by_ref(self, v):  self._idx.coolers_by_ref = v
    @property
    def coolers_by_name(self):    return self._idx.coolers_by_name
    @coolers_by_name.setter
    def coolers_by_name(self, v): self._idx.coolers_by_name = v
    @property
    def radars_by_ref(self):      return self._idx.radars_by_ref
    @radars_by_ref.setter
    def radars_by_ref(self, v):   self._idx.radars_by_ref = v
    @property
    def radars_by_name(self):     return self._idx.radars_by_name
    @radars_by_name.setter
    def radars_by_name(self, v):  self._idx.radars_by_name = v
    @property
    def missiles_by_ref(self):    return self._idx.missiles_by_ref
    @missiles_by_ref.setter
    def missiles_by_ref(self, v): self._idx.missiles_by_ref = v
    @property
    def missiles_by_name(self):   return self._idx.missiles_by_name
    @missiles_by_name.setter
    def missiles_by_name(self, v):self._idx.missiles_by_name = v
    @property
    def powerplants_by_ref(self):     return self._idx.powerplants_by_ref
    @powerplants_by_ref.setter
    def powerplants_by_ref(self, v):  self._idx.powerplants_by_ref = v
    @property
    def powerplants_by_name(self):    return self._idx.powerplants_by_name
    @powerplants_by_name.setter
    def powerplants_by_name(self, v): self._idx.powerplants_by_name = v
    @property
    def qdrives_by_ref(self):     return self._idx.qdrives_by_ref
    @qdrives_by_ref.setter
    def qdrives_by_ref(self, v):  self._idx.qdrives_by_ref = v
    @property
    def qdrives_by_name(self):    return self._idx.qdrives_by_name
    @qdrives_by_name.setter
    def qdrives_by_name(self, v): self._idx.qdrives_by_name = v
    @property
    def ships_by_name(self):      return self._idx.ships_by_name
    @ships_by_name.setter
    def ships_by_name(self, v):   self._idx.ships_by_name = v

    # ── scunpacked accessors ──────────────────────────────────────────────
    @property
    def thrusters_by_ref(self):           return self._scunpacked.thrusters_by_ref
    @property
    def thrusters_by_name(self):          return self._scunpacked.thrusters_by_name
    @property
    def thrusters_by_local_name(self):    return self._scunpacked.thrusters_by_local_name
    @property
    def cmls_by_ref(self):                return self._scunpacked.cmls_by_ref
    @property
    def cmls_by_name(self):               return self._scunpacked.cmls_by_name
    @property
    def cmls_by_local_name(self):         return self._scunpacked.cmls_by_local_name
    @property
    def modules_by_ref(self):             return self._scunpacked.modules_by_ref
    @property
    def modules_by_name(self):            return self._scunpacked.modules_by_name
    @property
    def modules_by_local_name(self):      return self._scunpacked.modules_by_local_name

    # ── new Erkul component accessors ────────────────────────────────────
    @property
    def missile_racks_by_ref(self):   return self._idx.missile_racks_by_ref
    @missile_racks_by_ref.setter
    def missile_racks_by_ref(self, v): self._idx.missile_racks_by_ref = v
    @property
    def missile_racks_by_name(self):  return self._idx.missile_racks_by_name
    @missile_racks_by_name.setter
    def missile_racks_by_name(self, v): self._idx.missile_racks_by_name = v

    @property
    def mounts_by_ref(self):   return self._idx.mounts_by_ref
    @mounts_by_ref.setter
    def mounts_by_ref(self, v): self._idx.mounts_by_ref = v
    @property
    def mounts_by_name(self):  return self._idx.mounts_by_name
    @mounts_by_name.setter
    def mounts_by_name(self, v): self._idx.mounts_by_name = v

    @property
    def emps_by_ref(self):   return self._idx.emps_by_ref
    @emps_by_ref.setter
    def emps_by_ref(self, v): self._idx.emps_by_ref = v
    @property
    def emps_by_name(self):  return self._idx.emps_by_name
    @emps_by_name.setter
    def emps_by_name(self, v): self._idx.emps_by_name = v

    @property
    def qeds_by_ref(self):   return self._idx.qeds_by_ref
    @qeds_by_ref.setter
    def qeds_by_ref(self, v): self._idx.qeds_by_ref = v
    @property
    def qeds_by_name(self):  return self._idx.qeds_by_name
    @qeds_by_name.setter
    def qeds_by_name(self, v): self._idx.qeds_by_name = v

    @property
    def bombs_by_ref(self):   return self._idx.bombs_by_ref
    @bombs_by_ref.setter
    def bombs_by_ref(self, v): self._idx.bombs_by_ref = v
    @property
    def bombs_by_name(self):  return self._idx.bombs_by_name
    @bombs_by_name.setter
    def bombs_by_name(self, v): self._idx.bombs_by_name = v

    @property
    def turrets_by_ref(self):   return self._idx.turrets_by_ref
    @turrets_by_ref.setter
    def turrets_by_ref(self, v): self._idx.turrets_by_ref = v
    @property
    def turrets_by_name(self):  return self._idx.turrets_by_name
    @turrets_by_name.setter
    def turrets_by_name(self, v): self._idx.turrets_by_name = v

    @property
    def mining_lasers_by_ref(self):   return self._idx.mining_lasers_by_ref
    @mining_lasers_by_ref.setter
    def mining_lasers_by_ref(self, v): self._idx.mining_lasers_by_ref = v
    @property
    def mining_lasers_by_name(self):  return self._idx.mining_lasers_by_name
    @mining_lasers_by_name.setter
    def mining_lasers_by_name(self, v): self._idx.mining_lasers_by_name = v

    # ── /live/utilities accessors ─────────────────────────────────────────
    @property
    def tool_arms_by_ref(self):   return self._idx.tool_arms_by_ref
    @tool_arms_by_ref.setter
    def tool_arms_by_ref(self, v): self._idx.tool_arms_by_ref = v
    @property
    def tool_arms_by_name(self):  return self._idx.tool_arms_by_name
    @tool_arms_by_name.setter
    def tool_arms_by_name(self, v): self._idx.tool_arms_by_name = v

    @property
    def salvage_heads_by_ref(self):   return self._idx.salvage_heads_by_ref
    @salvage_heads_by_ref.setter
    def salvage_heads_by_ref(self, v): self._idx.salvage_heads_by_ref = v
    @property
    def salvage_heads_by_name(self):  return self._idx.salvage_heads_by_name
    @salvage_heads_by_name.setter
    def salvage_heads_by_name(self, v): self._idx.salvage_heads_by_name = v

    @property
    def mining_modifiers_by_ref(self):   return self._idx.mining_modifiers_by_ref
    @mining_modifiers_by_ref.setter
    def mining_modifiers_by_ref(self, v): self._idx.mining_modifiers_by_ref = v
    @property
    def mining_modifiers_by_name(self):  return self._idx.mining_modifiers_by_name
    @mining_modifiers_by_name.setter
    def mining_modifiers_by_name(self, v): self._idx.mining_modifiers_by_name = v

    @property
    def salvage_modifiers_by_ref(self):   return self._idx.salvage_modifiers_by_ref
    @salvage_modifiers_by_ref.setter
    def salvage_modifiers_by_ref(self, v): self._idx.salvage_modifiers_by_ref = v
    @property
    def salvage_modifiers_by_name(self):  return self._idx.salvage_modifiers_by_name
    @salvage_modifiers_by_name.setter
    def salvage_modifiers_by_name(self, v): self._idx.salvage_modifiers_by_name = v

    @property
    def ore_pods_by_ref(self):   return self._idx.ore_pods_by_ref
    @ore_pods_by_ref.setter
    def ore_pods_by_ref(self, v): self._idx.ore_pods_by_ref = v
    @property
    def ore_pods_by_name(self):  return self._idx.ore_pods_by_name
    @ore_pods_by_name.setter
    def ore_pods_by_name(self, v): self._idx.ore_pods_by_name = v

    @property
    def fuel_tanks_by_ref(self):   return self._idx.fuel_tanks_by_ref
    @fuel_tanks_by_ref.setter
    def fuel_tanks_by_ref(self, v): self._idx.fuel_tanks_by_ref = v
    @property
    def fuel_tanks_by_name(self):  return self._idx.fuel_tanks_by_name
    @fuel_tanks_by_name.setter
    def fuel_tanks_by_name(self, v): self._idx.fuel_tanks_by_name = v

    # ── /live/modules accessors ───────────────────────────────────────────
    @property
    def erkul_modules_by_ref(self):   return self._idx.erkul_modules_by_ref
    @erkul_modules_by_ref.setter
    def erkul_modules_by_ref(self, v): self._idx.erkul_modules_by_ref = v
    @property
    def erkul_modules_by_name(self):  return self._idx.erkul_modules_by_name
    @erkul_modules_by_name.setter
    def erkul_modules_by_name(self, v): self._idx.erkul_modules_by_name = v

    # ── Fleetyards hardpoints ─────────────────────────────────────────────

    def fetch_fy_hardpoints(self, ship_name: str, on_done=None):
        """Fetch Fleetyards hardpoints for *ship_name* in a background thread.

        Calls ``on_done(grouped_dict)`` when done.  Uses both an in-memory
        and a disk cache keyed by slug.
        """
        slug = _fy_slug(ship_name)

        # 1. Check in-memory / disk cache
        cached = self._fy_cache.get(slug)
        if cached is not None:
            if on_done:
                on_done(_fy_hp_group(cached))
            return

        def _run():
            try:
                data = self._fy_api.fetch_hardpoints(slug)
                if data:
                    try:
                        self._fy_cache.put(slug, data)
                    except (TypeError, ValueError, OSError) as e:
                        # FleetYards now returns ints past 64 bits (e.g. an
                        # unlimited jump range); the fast JSON writer refuses
                        # them. Show the data anyway, just do not cache it.
                        _log.warning("FY cache write skipped for %s: %s", slug, e)
                    if on_done:
                        on_done(_fy_hp_group(data))
                    return
            except (requests.RequestException, ValueError) as e:
                _log.warning("FY hardpoints fetch failed for %s: %s", slug, e)
            if on_done:
                on_done({})   # failed -- caller gets empty dict

        threading.Thread(target=_run, daemon=True).start()

    # ── Public state management ──────────────────────────────────────────

    def invalidate_and_reload(self, on_done=None):
        """Reset state and trigger a fresh load.  Thread-safe."""
        with self._lock:
            self.loaded  = False
            self.loading = False
            self.error   = None
        self.load(on_done=on_done)

    def save_cache_with_version(self, game_version: str):
        """Persist the current raw data to disk with *game_version* tag."""
        self._cache.save(self.raw, game_version)

    # ── Main load ─────────────────────────────────────────────────────────

    def cancel_load(self) -> None:
        """Signal the background load thread to abort between stages."""
        self._cancel.set()

    def load(self, on_done=None, on_stage=None, preloaded_cache=None, needs_refresh=True):
        """Load data in staged phases.

        *on_done*  – called (no args) when loading finishes or fails.
        *on_stage* – called(stage_name: str, stage_num: int, total: int)
                     between stages so the UI can show progress.
        *needs_refresh* – if False and preloaded_cache is provided, skip
                          network fetch entirely (cache was fresh).
        """
        with self._lock:
            if self.loading:
                _log.info("load() called but already loading, skipping")
                return
            self.loading = True
        self._cancel = threading.Event()

        def _emit_stage(name, num, total):
            _log.info("  [stage %d/%d] %s", num, total, name)
            if on_stage:
                try:
                    on_stage(name, num, total)
                except Exception:
                    pass

        def _cancelled():
            return self._cancel.is_set()

        def _run():
            TOTAL_STAGES = 5
            try:
                _log.info("Data load thread started (tid=%s, daemon=%s)",
                          threading.current_thread().name,
                          threading.current_thread().daemon)

                # ── Stage 1: Acquire raw data (cache or network) ──────
                _emit_stage("Loading cache", 1, TOTAL_STAGES)
                cached = preloaded_cache
                if cached:
                    _log.info("  Using preloaded cache (%d keys)", len(cached))
                else:
                    _log.info("  Cache file: %s", self._cache.path)
                    _log.info("  Cache file exists: %s", os.path.isfile(self._cache.path))
                    if os.path.isfile(self._cache.path):
                        _log.info("  Cache file size: %.1f MB",
                                  os.path.getsize(self._cache.path) / 1048576)
                    _log.info("  Calling self._cache.load()...")
                    cached = self._cache.load()
                    _log.info("  self._cache.load() returned: %s",
                              "data" if cached else "None")

                if _cancelled():
                    _log.info("  Cancelled after stage 1")
                    return

                # Start scunpacked load in parallel — fire-and-forget; it has its own
                # cache and completes independently of the Erkul pipeline.
                self._scunpacked.load(stale_ok=True)

                if cached:
                    _log.info("  Using cached data (needs_refresh=%s)", needs_refresh)
                    raw = cached
                    # load_game_version() is free when self._cache already
                    # parsed the file (metadata cached in load()).  When using
                    # preloaded_cache the repo's own DiskCache was never loaded,
                    # so just use whatever metadata it has (empty is fine).
                    self.cached_game_version = self._cache.load_game_version()
                    _log.info("  Game version: %s", self.cached_game_version)
                else:
                    if not erkul_network_allowed():
                        raise ErkulNetworkDisabled("DPS Calculator catalogs")
                    _log.info("  No cache, fetching from erkul.games...")
                    raw = {}
                    endpoints = [
                        ("/live/weapons",        "/live/weapons"),
                        ("/live/shields",        "/live/shields"),
                        ("/live/coolers",        "/live/coolers"),
                        ("/live/missiles",       "/live/missiles"),
                        ("/live/radars",         "/live/radars"),
                        ("/live/powerplants",    "/live/power-plants"),
                        ("/live/quantumdrives",  "/live/qdrives"),
                        ("/live/thrusters",      "/live/thrusters"),
                        ("/live/paints",         "/live/paints"),
                        # New endpoints discovered in audit
                        ("/live/missile-racks",  "/live/missile-racks"),
                        ("/live/mounts",         "/live/mounts"),
                        ("/live/emps",           "/live/emps"),
                        ("/live/qeds",           "/live/qeds"),
                        ("/live/bombs",          "/live/bombs"),
                        ("/live/turrets",        "/live/turrets"),
                        ("/live/mining-lasers",  "/live/mining-lasers"),
                        ("/live/utilities",      "/live/utilities"),
                        ("/live/modules",        "/live/modules"),
                    ]
                    for key, path in endpoints:
                        if _cancelled():
                            _log.info("  Cancelled during API fetch")
                            return
                        raw[key] = self._erkul.fetch_safe(path)
                    if _cancelled():
                        return
                    raw["/live/ships"] = self._erkul.fetch_all_ships()
                    self._cache.save(raw, self.cached_game_version)

                if _cancelled():
                    return

                # ── Indexer helper ─────────────────────────────────────
                def _index(entries, compute_fn, by_ref, by_name, filt=None):
                    for e in entries:
                        d = e.get("data", {})
                        if filt and not filt(d):
                            continue
                        try:
                            stats = compute_fn(e)
                        except (KeyError, TypeError, ValueError):
                            continue
                        enrich_component_stats(stats, d)
                        ref = stats["ref"]
                        # Use local_name as key (unique per item) to avoid
                        # collisions when multiple items share the same name+size
                        # but differ by required_tags (e.g. ship-specific mounts).
                        key = f"{stats.get('local_name') or stats['name'].lower()}_{stats['size']}"
                        if ref:
                            by_ref[ref] = stats
                        by_name[key] = stats

                snap = _IndexSnapshot()

                # ── Stage 2: Index weapons & shields (critical path) ──
                _emit_stage("Indexing weapons & shields", 2, TOTAL_STAGES)
                _index(raw.get("/live/weapons", []), compute_weapon_stats,
                       snap.weapons_by_ref, snap.weapons_by_name,
                       filt=lambda d: d.get("type") == "WeaponGun")
                _index(raw.get("/live/shields", []), compute_shield_stats,
                       snap.shields_by_ref, snap.shields_by_name)

                if _cancelled():
                    return

                # ── Stage 3: Index remaining components ───────────────
                _emit_stage("Indexing components", 3, TOTAL_STAGES)
                _index(raw.get("/live/coolers", []), compute_cooler_stats,
                       snap.coolers_by_ref, snap.coolers_by_name)
                _index(raw.get("/live/radars", []), compute_radar_stats,
                       snap.radars_by_ref, snap.radars_by_name)
                _index(raw.get("/live/missiles", []), compute_missile_stats,
                       snap.missiles_by_ref, snap.missiles_by_name)
                _index(raw.get("/live/powerplants", []), compute_powerplant_stats_erkul,
                       snap.powerplants_by_ref, snap.powerplants_by_name)
                _index(raw.get("/live/quantumdrives", []), compute_qdrive_stats_erkul,
                       snap.qdrives_by_ref, snap.qdrives_by_name)
                # New Erkul endpoints
                _index(raw.get("/live/missile-racks", []), compute_missile_rack_stats,
                       snap.missile_racks_by_ref, snap.missile_racks_by_name,
                       filt=lambda d: (
                           d.get("type") == "MissileLauncher" and
                           any(any(it.get("type") == "Missile"
                                   for it in (p.get("itemTypes") or []))
                               for p in (d.get("ports") or []))))
                _index(raw.get("/live/mounts", []), compute_mount_stats,
                       snap.mounts_by_ref, snap.mounts_by_name)
                _index(raw.get("/live/emps", []), compute_emp_stats,
                       snap.emps_by_ref, snap.emps_by_name)
                _index(raw.get("/live/qeds", []), compute_qed_stats,
                       snap.qeds_by_ref, snap.qeds_by_name)
                _index(raw.get("/live/bombs", []), compute_bomb_stats,
                       snap.bombs_by_ref, snap.bombs_by_name)
                _index(raw.get("/live/turrets", []), compute_turret_stats,
                       snap.turrets_by_ref, snap.turrets_by_name)
                _index(raw.get("/live/mining-lasers", []), compute_mining_laser_stats,
                       snap.mining_lasers_by_ref, snap.mining_lasers_by_name)
                # /live/utilities — filter by type within the same endpoint
                _index(raw.get("/live/utilities", []), compute_tool_arm_stats,
                       snap.tool_arms_by_ref, snap.tool_arms_by_name,
                       filt=lambda d: d.get("type") == "ToolArm")
                _index(raw.get("/live/utilities", []), compute_salvage_head_stats,
                       snap.salvage_heads_by_ref, snap.salvage_heads_by_name,
                       filt=lambda d: d.get("type") == "SalvageHead")
                _index(raw.get("/live/utilities", []), compute_mining_modifier_stats,
                       snap.mining_modifiers_by_ref, snap.mining_modifiers_by_name,
                       filt=lambda d: d.get("type") == "MiningModifier")
                _index(raw.get("/live/utilities", []), compute_salvage_modifier_stats,
                       snap.salvage_modifiers_by_ref, snap.salvage_modifiers_by_name,
                       filt=lambda d: d.get("type") == "SalvageModifier")
                _index(raw.get("/live/utilities", []), compute_ore_pod_stats,
                       snap.ore_pods_by_ref, snap.ore_pods_by_name,
                       filt=lambda d: d.get("type") == "Container" and d.get("subType") == "Cargo")
                _index(raw.get("/live/utilities", []), compute_fuel_tank_stats,
                       snap.fuel_tanks_by_ref, snap.fuel_tanks_by_name,
                       filt=lambda d: d.get("type") == "ExternalFuelTank")
                # /live/modules — ship swap modules
                _index(raw.get("/live/modules", []), compute_erkul_module_stats,
                       snap.erkul_modules_by_ref, snap.erkul_modules_by_name)

                if _cancelled():
                    return

                # ── Stage 4: Index ships ──────────────────────────────
                _emit_stage("Indexing ships", 4, TOTAL_STAGES)
                sbn = {}
                for e in raw.get("/live/ships", []):
                    d = e.get("data", {})
                    n = d.get("name", "")
                    if n:
                        sbn[n]         = d
                        sbn[n.lower()] = d
                snap.ships_by_name = sbn

                if _cancelled():
                    return

                # ── Stage 5: Build cross-category lookups ─────────────
                _emit_stage("Building lookups", 5, TOTAL_STAGES)
                bln = {}
                rln = {}
                rbr = {}
                for by_ref in (snap.weapons_by_ref, snap.shields_by_ref,
                               snap.coolers_by_ref, snap.radars_by_ref,
                               snap.powerplants_by_ref, snap.qdrives_by_ref,
                               snap.missile_racks_by_ref, snap.mounts_by_ref,
                               snap.emps_by_ref, snap.qeds_by_ref,
                               snap.bombs_by_ref, snap.turrets_by_ref,
                               snap.mining_lasers_by_ref,
                               snap.tool_arms_by_ref, snap.salvage_heads_by_ref,
                               snap.mining_modifiers_by_ref, snap.salvage_modifiers_by_ref,
                               snap.ore_pods_by_ref, snap.fuel_tanks_by_ref,
                               snap.erkul_modules_by_ref):
                    for ref, stats in by_ref.items():
                        ln = stats.get("local_name")
                        if ln:
                            bln[ln] = stats
                for ep_key, entries in raw.items():
                    if not isinstance(entries, list):
                        continue
                    for entry in entries:
                        ln = entry.get("localName")
                        d  = entry.get("data", {})
                        if ln:
                            rln[ln] = d
                        r = d.get("ref")
                        if r:
                            rbr[r] = d
                # Merge supplement data (items absent from Erkul API)
                try:
                    with open(SUPPLEMENT_FILE, encoding="utf-8") as _sf:
                        _supplement = json.load(_sf)
                    for _entry in _supplement:
                        _ln = _entry.get("localName", "")
                        _d  = _entry.get("data", {})
                        _r  = _d.get("ref", "")
                        if _ln:
                            rln.setdefault(_ln, _d)
                        if _r:
                            rbr.setdefault(_r, _d)
                except (FileNotFoundError, json.JSONDecodeError):
                    pass

                # Merge scunpacked local-name maps so raw_lookup() resolves thrusters/CMLs
                # even before the scunpacked background thread finishes (it may already
                # have data from its own cache at this point).
                for _local, _stats in self._scunpacked.thrusters_by_local_name.items():
                    rln.setdefault(_local, _stats)
                    if _stats.get("ref"):
                        rbr.setdefault(_stats["ref"], _stats)
                for _local, _stats in self._scunpacked.cmls_by_local_name.items():
                    rln.setdefault(_local, _stats)
                    if _stats.get("ref"):
                        rbr.setdefault(_stats["ref"], _stats)
                for _local, _stats in self._scunpacked.modules_by_local_name.items():
                    rln.setdefault(_local, _stats)
                    if _stats.get("ref"):
                        rbr.setdefault(_stats["ref"], _stats)

                snap.by_local_name = bln
                snap.raw_by_local_name = rln
                snap.raw_by_ref = rbr

                _log.info("  Indexing complete: %d weapons, %d shields, %d ships",
                          len(snap.weapons_by_ref), len(snap.shields_by_ref),
                          len(snap.ships_by_name) // 2)

                with self._lock:
                    self.raw     = raw
                    self._idx    = snap
                    self.loaded  = True
                    self.loading = False
                _log.info("  Data swap complete, loaded=True")

            except Exception as exc:
                _log.error("Data load FAILED: %s", exc, exc_info=True)
                with self._lock:
                    self.error   = str(exc)
                    self.loading = False
            finally:
                with self._lock:
                    self.loading = False
                _log.info("  Firing on_done callback...")
                if on_done:
                    on_done()
                _log.info("  on_done callback fired")

        if self.source == "scunpacked":
            def target():
                self._run_scunpacked(on_done, _emit_stage, _cancelled, preloaded_cache)
        else:
            target = _run
        threading.Thread(target=target, daemon=True).start()

    # ── scunpacked-data load ──────────────────────────────────────────────

    _SC_KIND_ATTRS = {
        "weapons": "weapons", "shields": "shields", "coolers": "coolers",
        "radars": "radars", "missiles": "missiles", "powerplants": "powerplants",
        "qdrives": "qdrives", "missile_racks": "missile_racks", "mounts": "mounts",
        "emps": "emps", "qeds": "qeds", "bombs": "bombs", "turrets": "turrets",
        "mining_lasers": "mining_lasers", "tool_arms": "tool_arms",
        "salvage_heads": "salvage_heads", "mining_modifiers": "mining_modifiers",
        "salvage_modifiers": "salvage_modifiers", "ore_pods": "ore_pods",
        "fuel_tanks": "fuel_tanks", "modules": "erkul_modules",
    }

    def _run_scunpacked(self, on_done, emit_stage, cancelled, preloaded) -> None:
        """Index the scunpacked window index (data/scunpacked_provider.py).

        *preloaded* is the index dict, parsed on the main thread before Qt
        started. Only dict building happens here."""
        try:
            emit_stage("Loading scunpacked-data", 1, 3)
            idx = preloaded
            if not idx:
                from data import scunpacked_provider as scp
                idx = scp.load_window_index(allow_fetch=True)
            if cancelled():
                return
            emit_stage("Indexing items", 2, 3)
            snap = _IndexSnapshot()
            items = idx.get("items") or {}
            for kind, attr in self._SC_KIND_ATTRS.items():
                by_ref = getattr(snap, f"{attr}_by_ref")
                by_name = getattr(snap, f"{attr}_by_name")
                for st in items.get(kind) or []:
                    if st.get("ref"):
                        by_ref[st["ref"]] = st
                    by_name[f"{st['local_name']}_{st['size']}"] = st
            bln = {}
            for kind in self._SC_KIND_ATTRS:
                for st in items.get(kind) or []:
                    bln.setdefault(st["local_name"], st)
            snap.by_local_name = bln
            # erkul-shaped power records (data/scunpacked_provider._power_raw):
            # what the power allocator's raw lookup resolves ports against
            praw = idx.get("power_raw") or {}
            snap.raw_by_local_name = dict(praw)
            snap.raw_by_ref = {r["ref"]: r for r in praw.values() if r.get("ref")}
            if cancelled():
                return
            emit_stage("Indexing ships", 3, 3)
            sbn = {}
            for name, sd in (idx.get("ships") or {}).items():
                sbn[name] = sd
                sbn.setdefault(name.lower(), sd)
            snap.ships_by_name = sbn
            info = {k: idx.get(k) for k in ("build", "commit", "source", "fetched_at", "dir")}
            with self._lock:
                self.raw = {"/live/ships": [{"data": sd} for sd in (idx.get("ships") or {}).values()]}
                self._idx = snap
                self._sc_adapter_weapons = idx.get("adapter_weapons") or {}
                self._sc_weapons_by_cls = {w["cls"]: w for w in items.get("weapons") or []}
                self.source_info = info
                self.cached_game_version = info.get("build") or ""
                self.loaded = True
                self.loading = False
            _log.info("scunpacked %s (%s): %d ships, %d weapons, %d shields",
                      info.get("build"), (info.get("commit") or "")[:12],
                      len(idx.get("ships") or {}), len(items.get("weapons") or []),
                      len(items.get("shields") or []))
        except Exception as exc:
            _log.error("scunpacked load FAILED: %s", exc, exc_info=True)
            with self._lock:
                self.error = str(exc)
        finally:
            with self._lock:
                self.loading = False
            if on_done:
                on_done()

    def weapon_candidates_for_slot(self, slot: dict) -> list:
        """Weapons that may go in one window gun slot. For scunpacked slots the
        size / player-gun / ship-lock / locked-port rule is the Assistant
        adapter's ``fits`` (data/scunpacked_provider.candidates)."""
        if not slot.get("sc_slots"):
            return self.weapons_for_size_filtered(slot.get("max_size") or 1,
                                                  slot.get("required_tags", ""))
        from data import scunpacked_provider as scp
        return scp.candidates(slot, self._sc_adapter_weapons, self._sc_weapons_by_cls)

    # ── Ship accessors ────────────────────────────────────────────────────

    def get_ship_names(self) -> list:
        seen, names = set(), []
        for e in self.raw.get("/live/ships", []):
            n = e.get("data", {}).get("name", "")
            if n and n not in seen:
                seen.add(n)
                names.append(n)
        return sorted(names)

    def get_ship_data(self, name: str) -> Optional[dict]:
        idx = self._idx  # snapshot read
        hit = idx.ships_by_name.get(name) or idx.ships_by_name.get(name.lower())
        if hit or self.source != "scunpacked" or not name:
            return hit
        # scunpacked names carry the manufacturer ("Aegis Gladius"); voice/IPC
        # commands often do not ("Gladius"). Match without it, then by
        # substring, shortest name first.
        q = name.strip().lower()
        qt = q.split()
        best = None
        for n, sd in idx.ships_by_name.items():
            if n != sd.get("name"):
                continue
            nl = n.lower()
            bare = nl.split(" ", 1)[1] if " " in nl else nl
            if q == bare:
                return sd
            if (q in nl or all(t in nl.split() for t in qt)) and \
                    (best is None or len(n) < len(best.get("name", ""))):
                best = sd
        return best

    # ── Fast cross-category lookups (O(1)) ──────────────────────────────

    def lookup_by_local_name(self, local_name: str) -> Optional[dict]:
        """Return enriched stats dict for a component by its localName."""
        return self._idx.by_local_name.get(local_name)

    def raw_lookup(self, identifier: str) -> Optional[dict]:
        """Return raw erkul data dict by localName or ref UUID."""
        idx = self._idx
        return idx.raw_by_local_name.get(identifier) or idx.raw_by_ref.get(identifier)

    # ── Component lookup ──────────────────────────────────────────────────

    def _find(self, by_ref: dict, by_name: dict,
              query: str, max_size: int = None) -> Optional[dict]:
        """Search by_name values so all size variants are considered.

        When *max_size* is given, only return items whose size <= max_size;
        among those return the largest-size match.  When *max_size* is None
        return the overall largest-size match.
        """
        q = query.strip()
        if not q:
            return None

        # 1. Direct ref lookup (UUID)
        if q in by_ref:
            s = by_ref[q]
            if max_size is None or s["size"] <= max_size:
                return s

        ql = q.lower()

        def size_ok(v: dict) -> bool:
            return max_size is None or v["size"] <= max_size

        # 1b. local_name match (erkul localName)
        for v in by_name.values():
            ln = v.get("local_name", "")
            if ln and ln.lower() == ql and size_ok(v):
                return v
        for v in by_ref.values():
            ln = v.get("local_name", "")
            if ln and ln.lower() == ql and size_ok(v):
                return v

        candidates: list = []

        # 2. Exact name match (all size variants)
        for v in by_name.values():
            if v["name"].lower() == ql and size_ok(v):
                candidates.append(v)

        # 3. Prefix match
        if not candidates:
            for v in by_name.values():
                if v["name"].lower().startswith(ql) and size_ok(v):
                    candidates.append(v)

        # 4. Substring match
        if not candidates:
            for v in by_name.values():
                if ql in v["name"].lower() and size_ok(v):
                    candidates.append(v)

        if candidates:
            # Return largest size within constraint
            return max(candidates, key=lambda x: x["size"])

        return None

    # Each find_* captures self._idx once so the entire lookup uses a single
    # consistent snapshot even if a background load swaps _idx mid-call.
    def find_weapon(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.weapons_by_ref,  idx.weapons_by_name,  q, max_size)

    def find_shield(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.shields_by_ref,  idx.shields_by_name,  q, max_size)

    def find_cooler(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.coolers_by_ref,  idx.coolers_by_name,  q, max_size)

    def find_radar(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.radars_by_ref,   idx.radars_by_name,   q, max_size)

    def find_missile(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.missiles_by_ref, idx.missiles_by_name, q, max_size)

    def find_powerplant(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.powerplants_by_ref, idx.powerplants_by_name, q, max_size)

    def find_qdrive(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.qdrives_by_ref, idx.qdrives_by_name, q, max_size)

    # ── scunpacked-sourced lookups ─────────────────────────────────────────
    def find_thruster(self, q, max_size=None):
        return self._scunpacked.find_thruster(q, max_size)

    def find_cml(self, q, max_size=None):
        return self._scunpacked.find_cml(q, max_size)

    def find_module(self, q, max_size=None):
        return self._scunpacked.find_module(q, max_size)

    def find_missile_rack(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.missile_racks_by_ref, idx.missile_racks_by_name, q, max_size)

    def find_mount(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.mounts_by_ref, idx.mounts_by_name, q, max_size)

    def find_emp(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.emps_by_ref, idx.emps_by_name, q, max_size)

    def find_qed(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.qeds_by_ref, idx.qeds_by_name, q, max_size)

    def find_bomb(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.bombs_by_ref, idx.bombs_by_name, q, max_size)

    def find_turret(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.turrets_by_ref, idx.turrets_by_name, q, max_size)

    def find_mining_laser(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.mining_lasers_by_ref, idx.mining_lasers_by_name, q, max_size)

    def _list_for_size(self, by_name: dict, max_size: int) -> list:
        # scunpacked rows carry "listable" (False for NPC / placeholder / ship-
        # locked items); erkul rows have no such key and are unaffected.
        return sorted(
            [v for v in by_name.values()
             if v["size"] <= max_size and v.get("listable") is not False],
            key=lambda x: (-x["size"], x["name"]),
        )

    def _list_for_size_tagged(self, by_name: dict, max_size: int, required_tags: str) -> list:
        """Like _list_for_size but also filters by required_tags equality."""
        return sorted(
            [v for v in by_name.values()
             if v["size"] <= max_size and v.get("required_tags", "") == required_tags],
            key=lambda x: (-x["size"], x["name"]),
        )

    def _list_for_size_no_tags(self, by_name: dict, max_size: int) -> list:
        """Return only items with no required_tags (generic, fits any slot)."""
        return sorted(
            [v for v in by_name.values()
             if v["size"] <= max_size and not v.get("required_tags", "")
             and v.get("listable") is not False],
            key=lambda x: (-x["size"], x["name"]),
        )

    def weapons_for_size(self, sz):      return self._list_for_size(self._idx.weapons_by_name,      sz)
    def shields_for_size(self, sz):      return self._list_for_size(self._idx.shields_by_name,      sz)
    def coolers_for_size(self, sz):      return self._list_for_size(self._idx.coolers_by_name,      sz)
    def radars_for_size(self, sz):       return self._list_for_size(self._idx.radars_by_name,       sz)
    def missiles_for_size(self, sz):     return self._list_for_size(self._idx.missiles_by_name,     sz)
    def powerplants_for_size(self, sz):  return self._list_for_size(self._idx.powerplants_by_name,  sz)
    def qdrives_for_size(self, sz):      return self._list_for_size(self._idx.qdrives_by_name,      sz)
    def thrusters_for_size(self, sz):      return self._scunpacked.thrusters_for_size(sz)
    def cmls_for_size(self, sz):           return self._scunpacked.cmls_for_size(sz)
    def modules_for_size(self, sz):        return self._scunpacked.modules_for_size(sz)
    def missile_racks_for_size(self, sz) -> list:
        """Missile racks for an exact slot size, deduplicated by display name.

        Racks must fill the slot exactly (size == sz) — Erkul only shows
        exact-size matches, and undersized racks waste hardpoint space.
        """
        all_racks = [v for v in self._idx.missile_racks_by_name.values()
                     if v["size"] == sz and v.get("listable") is not False]
        seen: dict = {}
        for r in sorted(all_racks, key=lambda x: len(x.get("local_name", ""))):
            key = (r["name"].strip(), r["size"])
            seen.setdefault(key, r)
        return sorted(seen.values(), key=lambda x: (-x["size"], x["name"]))
    def mounts_for_size(self, sz):         return self._list_for_size(self._idx.mounts_by_name,        sz)
    def emps_for_size(self, sz):           return self._list_for_size(self._idx.emps_by_name,          sz)
    def qeds_for_size(self, sz):           return self._list_for_size(self._idx.qeds_by_name,          sz)
    def bombs_for_size(self, sz):          return self._list_for_size(self._idx.bombs_by_name,         sz)
    def turrets_for_size(self, sz):        return self._list_for_size(self._idx.turrets_by_name,       sz)
    def mining_lasers_for_size(self, sz):  return self._list_for_size(self._idx.mining_lasers_by_name, sz)

    # ── required_tags-aware lookups ────────────────────────────────────────────
    def weapons_for_size_filtered(self, sz, required_tags: str = "") -> list:
        """Weapons for a slot: tagged weapons only when slot has a specific tag,
        generic weapons only (required_tags='') for standard slots."""
        if required_tags:
            # Ship-specific slot: show tagged weapons plus generic ones (player
            # can always downgrade to a standard weapon).
            tagged  = self._list_for_size_tagged(self._idx.weapons_by_name, sz, required_tags)
            generic = self._list_for_size_no_tags(self._idx.weapons_by_name, sz)
            seen = {v["name"] for v in tagged}
            return tagged + [v for v in generic if v["name"] not in seen]
        # Standard slot: hide ship-specific weapons entirely.
        return self._list_for_size_no_tags(self._idx.weapons_by_name, sz)

    def mounts_for_size_tagged(self, sz, required_tags: str = "") -> list:
        """Mounts for an exact slot size, filtered by required_tags, deduplicated.

        Gimbals must fill the slot exactly (size == sz), not just fit (size <= sz),
        because undersized gimbals waste hardpoint space and Erkul only shows
        exact-size matches in its gimbal picker.

        Many gimbals have ship-specific positioning variants (e.g. six
        ``mount_gimbal_s3_perseus_*`` entries all named "VariPuck S3") that
        carry no required_tags and are functionally identical to the canonical
        ``mount_gimbal_s3``.  Showing all variants in the picker clutters the
        list, so we keep only one entry per (name, size) — the one with the
        shortest local_name, which is always the canonical generic variant.
        """
        all_mounts = [v for v in self._idx.mounts_by_name.values()
                      if v["size"] == sz
                      and v.get("required_tags", "") == required_tags]
        # Deduplicate: sort by local_name length so the shortest (most generic)
        # variant wins, then keep the first entry per (name, size) key.
        seen: dict = {}
        for m in sorted(all_mounts, key=lambda x: len(x.get("local_name", ""))):
            key = (m["name"].strip(), m["size"])
            seen.setdefault(key, m)
        return sorted(seen.values(), key=lambda x: (-x["size"], x["name"]))

    def mining_lasers_for_size_tagged(self, sz, required_tags: str = "") -> list:
        """Mining lasers filtered by required_tags (miningMount vs DRAK_miningMount etc.)."""
        if required_tags:
            return self._list_for_size_tagged(self._idx.mining_lasers_by_name, sz, required_tags)
        # No known tag — show all (size 0 always passes the <= sz check).
        return self._list_for_size(self._idx.mining_lasers_by_name, sz)

    # ── /live/utilities find_ methods ─────────────────────────────────────────
    def find_tool_arm(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.tool_arms_by_ref, idx.tool_arms_by_name, q, max_size)

    def find_salvage_head(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.salvage_heads_by_ref, idx.salvage_heads_by_name, q, max_size)

    def find_mining_modifier(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.mining_modifiers_by_ref, idx.mining_modifiers_by_name, q, max_size)

    def find_salvage_modifier(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.salvage_modifiers_by_ref, idx.salvage_modifiers_by_name, q, max_size)

    def find_ore_pod(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.ore_pods_by_ref, idx.ore_pods_by_name, q, max_size)

    def find_fuel_tank(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.fuel_tanks_by_ref, idx.fuel_tanks_by_name, q, max_size)

    # ── /live/modules find_ method ────────────────────────────────────────────
    def find_erkul_module(self, q, max_size=None):
        idx = self._idx
        return self._find(idx.erkul_modules_by_ref, idx.erkul_modules_by_name, q, max_size)

    # ── *_for_size methods ────────────────────────────────────────────────────
    def tool_arms_for_size(self, sz):           return self._list_for_size(self._idx.tool_arms_by_name,          sz)
    def salvage_heads_for_size(self, sz):       return self._list_for_size(self._idx.salvage_heads_by_name,      sz)
    def mining_modifiers_for_size(self, sz):    return self._list_for_size(self._idx.mining_modifiers_by_name,   sz)
    def salvage_modifiers_for_size(self, sz):   return self._list_for_size(self._idx.salvage_modifiers_by_name,  sz)
    def ore_pods_for_size(self, sz):            return self._list_for_size(self._idx.ore_pods_by_name,           sz)
    def erkul_modules_for_size(self, sz):       return self._list_for_size(self._idx.erkul_modules_by_name,      sz)

    def fuel_tanks_for_size_tagged(self, sz, required_tags: str = "") -> list:
        """Fuel tanks filtered by required_tags (e.g. MISC_Starfarer_Base)."""
        if required_tags:
            return self._list_for_size_tagged(self._idx.fuel_tanks_by_name, sz, required_tags)
        return self._list_for_size_no_tags(self._idx.fuel_tanks_by_name, sz)

    def erkul_modules_for_size_tagged(self, sz, required_tags: str = "") -> list:
        """Ship modules filtered by required_tags (slot-specific modules)."""
        if required_tags:
            return self._list_for_size_tagged(self._idx.erkul_modules_by_name, sz, required_tags)
        return self._list_for_size(self._idx.erkul_modules_by_name, sz)

    def salvage_heads_for_size_tagged(self, sz, required_tags: str = "") -> list:
        """Salvage heads filtered by required_tags (salvageMount)."""
        if required_tags:
            return self._list_for_size_tagged(self._idx.salvage_heads_by_name, sz, required_tags)
        return self._list_for_size(self._idx.salvage_heads_by_name, sz)
