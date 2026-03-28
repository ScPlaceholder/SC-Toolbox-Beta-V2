"""Refactored DataManager -- now ComponentRepository.

Uses ErkulApiClient / FleetyardsApiClient for HTTP,
DiskCache / FleetyardsCache for persistence,
and delegates compute functions to services.*.
"""
import logging
import threading
import time
from typing import Optional

import requests

from data.api_client import ErkulApiClient, FleetyardsApiClient
from data.cache import DiskCache, FleetyardsCache
from services.dps_calculator import compute_weapon_stats
from services.stat_computation import (
    compute_shield_stats,
    compute_cooler_stats,
    compute_radar_stats,
    compute_missile_stats,
    compute_powerplant_stats_erkul,
    compute_qdrive_stats_erkul,
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
CACHE_VERSION    = 5
FY_HP_CACHE_FILE = os.path.join(_DATA_DIR, ".fy_hardpoints_cache.json")
FY_HP_TTL        = CACHE_TTL_CARGO


def _fy_slug(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"\s+", "-", s.strip())
    return s


def _fy_hp_group(fy_list: list) -> dict:
    groups: dict = {}
    for hp in (fy_list or []):
        t = hp.get("type", "unknown")
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
                 'by_local_name', 'raw_by_local_name', 'raw_by_ref')

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
                    self._fy_cache.put(slug, data)
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
                    _log.info("  No cache, fetching from erkul.games...")
                    raw = {}
                    endpoints = [
                        ("/live/weapons",       "/live/weapons"),
                        ("/live/shields",       "/live/shields"),
                        ("/live/coolers",       "/live/coolers"),
                        ("/live/missiles",      "/live/missiles"),
                        ("/live/radars",        "/live/radars"),
                        ("/live/powerplants",   "/live/power-plants"),
                        ("/live/quantumdrives", "/live/qdrives"),
                        ("/live/thrusters",     "/live/thrusters"),
                        ("/live/paints",        "/live/paints"),
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
                        key = f"{stats['name'].lower()}_{stats['size']}"
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
                               snap.powerplants_by_ref, snap.qdrives_by_ref):
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

        threading.Thread(target=_run, daemon=True).start()

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
        return idx.ships_by_name.get(name) or idx.ships_by_name.get(name.lower())

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

    def _list_for_size(self, by_name: dict, max_size: int) -> list:
        return sorted(
            [v for v in by_name.values() if v["size"] <= max_size],
            key=lambda x: (-x["size"], x["name"]),
        )

    def weapons_for_size(self, sz):      return self._list_for_size(self._idx.weapons_by_name,      sz)
    def shields_for_size(self, sz):      return self._list_for_size(self._idx.shields_by_name,      sz)
    def coolers_for_size(self, sz):      return self._list_for_size(self._idx.coolers_by_name,      sz)
    def radars_for_size(self, sz):       return self._list_for_size(self._idx.radars_by_name,       sz)
    def missiles_for_size(self, sz):     return self._list_for_size(self._idx.missiles_by_name,     sz)
    def powerplants_for_size(self, sz):  return self._list_for_size(self._idx.powerplants_by_name,  sz)
    def qdrives_for_size(self, sz):      return self._list_for_size(self._idx.qdrives_by_name,      sz)
