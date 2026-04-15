"""Load Mining_Loadout JSON files into Mining Signals.

The companion tool ``skills/Mining_Loadout`` persists each ship's
configuration as a JSON file with this schema (see
``Mining_Loadout/services/config_service.py``)::

    {
        "version": 2,
        "ship": "MOLE" | "Prospector" | "Golem",
        "hotkey": "...",
        "loadout": {
            "turret_0": {"laser": "<name>", "modules": ["<name>", ...]},
            ...
        },
        "gadget": "<name>"
    }

Users can point each slot in the Mining Signals "Mining Ships" tab at
a saved file of this format (its own live config, or an exported
copy), and the selected ship's loadout feeds the breakability
calculator.

Two-layer design
----------------
1. **Parse layer** (:func:`load_loadout_file`) — pure JSON parsing,
   returns a :class:`LoadoutSnapshot` with the raw names from disk.
   This works without any network access or external dependencies.

2. **Resolve layer** (:func:`snapshot_to_laser_configs`) — converts
   the name strings into :class:`services.breakability.LaserConfig`
   objects with real numeric stats (max_power, resistance_modifier).
   This imports Mining_Loadout's ``api_client`` to read the shared
   UEX item database (cached at ``skills/Mining_Loadout/.api_cache/``)
   and re-implements the per-turret stat math inline so we don't hit
   the ``services`` namespace collision between the two tools.

   Resistance stacking matches the upstream Mort13 breakability tool:
   laser resistance, per-turret module resistances (additive within
   the turret), and the gadget are stacked multiplicatively into a
   single factor per turret. When multiple turrets are combined at
   breakability time, their factors multiply again — meaning a
   gadget applied to N turrets compounds N-fold, which matches how
   mining gadgets behave in-game (one per beam).

Keeping these layers separate means the UI can persist and display
loadout file paths today, and the stat bridge resolves lazily only
when breakability actually needs the numbers.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from typing import Any, List, Optional

from .breakability import LaserConfig

log = logging.getLogger(__name__)

# Path to the companion Mining_Loadout skill (for UEX data + models).
# Computed lazily so that a missing companion tool doesn't break
# Mining Signals at import time.
_ML_SKILL_DIR = os.path.normpath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "..", "..", "skills", "Mining_Loadout",
    )
)

# Cached handles to the dynamically-loaded Mining_Loadout modules.
# Populated by :func:`_ensure_ml_modules` on first use.
_ml_modules_lock = threading.Lock()
_ml_modules: dict[str, Any] | None = None

# Cached UEX item database (lasers, modules, gadgets) keyed by name.
# Populated on first resolve; invalidated if the resolver is asked
# with force_refresh=True.
_item_db_lock = threading.Lock()
_item_db: dict[str, Any] | None = None


# ─────────────────────────────────────────────────────────────
# Parse layer
# ─────────────────────────────────────────────────────────────

@dataclass
class TurretSnapshot:
    """One turret's selected laser + module names (strings, from disk)."""
    laser: str
    modules: List[str] = field(default_factory=list)


@dataclass
class LoadoutSnapshot:
    """Parsed Mining_Loadout JSON — names only, no resolved stats."""
    ship: str                             # "MOLE" | "Prospector" | "Golem"
    turrets: List[TurretSnapshot]
    gadget: str
    source_path: str                      # absolute path of the file
    version: int = 1


def load_loadout_file(path: str) -> Optional[LoadoutSnapshot]:
    """Read and parse a Mining_Loadout JSON from disk.

    Returns None (with a warning log) if the file is missing,
    malformed, or missing required keys. Does not raise — the UI
    should treat None as "file unusable" and clear the slot.
    """
    if not path or not os.path.isfile(path):
        log.warning("loadout_loader: file not found: %s", path)
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("loadout_loader: failed to read %s: %s", path, exc)
        return None

    if not isinstance(raw, dict):
        log.warning("loadout_loader: %s: root is not an object", path)
        return None

    ship = str(raw.get("ship", "")).strip()
    if not ship:
        log.warning("loadout_loader: %s: missing 'ship' key", path)
        return None

    # Support both v1 (turrets: [...]) and v2 (loadout: {turret_0: {...}, ...})
    turrets: List[TurretSnapshot] = []

    turrets_raw = raw.get("turrets")
    loadout_raw = raw.get("loadout")

    if isinstance(turrets_raw, list):
        # v1 format: "turrets" is a plain array of {laser, modules}
        for td in turrets_raw:
            if not isinstance(td, dict):
                continue
            laser = str(td.get("laser", "")).strip()
            mods_raw = td.get("modules", [])
            mods = [str(m).strip() for m in mods_raw if m] if isinstance(mods_raw, list) else []
            turrets.append(TurretSnapshot(laser=laser, modules=mods))
    elif isinstance(loadout_raw, dict):
        # v2 format: "loadout" is {turret_0: {laser, modules}, ...}
        keys = sorted(
            (k for k in loadout_raw.keys() if k.startswith("turret_")),
            key=lambda k: int(k.split("_", 1)[1]) if k.split("_", 1)[1].isdigit() else 999,
        )
        for key in keys:
            td = loadout_raw[key]
            if not isinstance(td, dict):
                continue
            laser = str(td.get("laser", "")).strip()
            mods_raw = td.get("modules", [])
            mods = [str(m).strip() for m in mods_raw if m] if isinstance(mods_raw, list) else []
            turrets.append(TurretSnapshot(laser=laser, modules=mods))
    else:
        log.warning("loadout_loader: %s: no 'turrets' or 'loadout' key found", path)
        return None

    if not turrets:
        log.warning("loadout_loader: %s: no turrets found", path)
        return None

    return LoadoutSnapshot(
        ship=ship,
        turrets=turrets,
        gadget=str(raw.get("gadget", "")).strip(),
        source_path=os.path.abspath(path),
        version=int(raw.get("version", 1)) if isinstance(raw.get("version"), int) else 1,
    )


def describe_snapshot(snap: LoadoutSnapshot) -> str:
    """Human-readable one-liner for showing the loaded file in the UI."""
    if not snap:
        return "(none)"
    turret_count = len(snap.turrets)
    lasers = ", ".join(t.laser or "—" for t in snap.turrets)
    return f"{snap.ship} · {turret_count} turret(s) · {lasers}"


# ─────────────────────────────────────────────────────────────
# Resolve layer — dynamic bridge to Mining_Loadout's UEX cache
# ─────────────────────────────────────────────────────────────

def _ensure_ml_modules() -> dict[str, Any] | None:
    """Lazy-import Mining_Loadout's models + api_client.

    Both tools have a top-level ``services/`` package so we cannot
    naively add Mining_Loadout to ``sys.path`` and use its
    ``services.api_client`` — that would shadow Mining Signals' own
    ``services`` namespace. Instead we load ``api_client.py`` via
    ``importlib.util.spec_from_file_location`` under a unique name,
    while letting its ``from models.items import ...`` resolve
    normally (``models`` has no collision since Mining Signals
    doesn't have a package of that name).

    Returns a dict with ``items`` and ``api`` module references, or
    None if the companion skill is missing or imports fail.
    """
    global _ml_modules
    with _ml_modules_lock:
        if _ml_modules is not None:
            return _ml_modules if _ml_modules else None

        if not os.path.isdir(_ML_SKILL_DIR):
            log.warning(
                "loadout_loader: Mining_Loadout skill not found at %s — "
                "breakability cannot resolve loadout stats",
                _ML_SKILL_DIR,
            )
            _ml_modules = {}
            return None

        # Prepend skill dir so ``from models.items import ...`` resolves
        # when the api_client module body runs. We leave it in place
        # afterwards — harmless because Mining Signals has no ``models``
        # package of its own to collide with.
        if _ML_SKILL_DIR not in sys.path:
            sys.path.insert(0, _ML_SKILL_DIR)

        try:
            import models.items as ml_items  # noqa: F401 — side effect: cached

            api_path = os.path.join(_ML_SKILL_DIR, "services", "api_client.py")
            spec = importlib.util.spec_from_file_location(
                "_ml_api_client", api_path,
            )
            if spec is None or spec.loader is None:
                raise ImportError(f"could not build spec for {api_path}")
            ml_api = importlib.util.module_from_spec(spec)
            sys.modules["_ml_api_client"] = ml_api
            spec.loader.exec_module(ml_api)

            _ml_modules = {"items": ml_items, "api": ml_api}
            log.info("loadout_loader: Mining_Loadout bridge initialized")
            return _ml_modules
        except Exception as exc:
            log.warning(
                "loadout_loader: failed to import Mining_Loadout bridge: %s",
                exc,
            )
            _ml_modules = {}
            return None


def _load_item_db(force_refresh: bool = False) -> dict[str, Any] | None:
    """Fetch the UEX item database via Mining_Loadout's cached client.

    Builds three name-keyed dicts (lasers, modules, gadgets) from the
    typed models. The underlying ``fetch_mining_data`` call uses a
    24-hour disk cache at ``skills/Mining_Loadout/.api_cache/``, so
    after the first call this is essentially free.

    Returns None if the Mining_Loadout bridge isn't available or if
    the fetch returned nothing.
    """
    global _item_db
    with _item_db_lock:
        if _item_db is not None and not force_refresh:
            return _item_db if _item_db else None

        mods = _ensure_ml_modules()
        if not mods:
            return None

        try:
            lasers, modules_list, gadgets = mods["api"].fetch_mining_data(
                use_cache=True,
            )
        except Exception as exc:
            log.warning("loadout_loader: fetch_mining_data failed: %s", exc)
            _item_db = {}
            return None

        if not lasers and not modules_list and not gadgets:
            log.warning("loadout_loader: UEX item database is empty")
            _item_db = {}
            return None

        _item_db = {
            "lasers": {l.name: l for l in lasers},
            "modules": {m.name: m for m in modules_list},
            "gadgets": {g.name: g for g in gadgets},
        }
        log.info(
            "loadout_loader: item db loaded — %d lasers, %d modules, %d gadgets",
            len(lasers), len(modules_list), len(gadgets),
        )
        return _item_db


@dataclass
class TurretStats:
    """Per-turret stats split into passive-only and with-active-modules."""
    min_power_passive: float
    max_power_passive: float
    resistance_modifier_passive: float
    min_power_active: float
    max_power_active: float
    resistance_modifier_active: float
    active_module_uses: int       # min(uses) across active modules, 0 if none
    active_module_duration: float  # max(duration) across active modules, 0 if none
    active_module_names: str = "" # comma-separated active module names


def _compute_turret_stats(
    laser: Any,
    mods: List[Any],
    gadget: Any | None,
) -> TurretStats:
    """Compute per-turret stats with passive-only and with-active splits.

    Module power/resistance contributions are computed twice:
    - passive_only: only modules where item_type != "Active"
    - with_actives: all modules (passive + active)

    Gadget resistance is NOT applied here — it's handled separately
    in the breakability layer so gadgets can be toggled on/off.
    """
    passive_mods = [m for m in mods if m is not None and getattr(m, "item_type", "Passive") != "Active"]
    active_mods = [m for m in mods if m is not None and getattr(m, "item_type", "Passive") == "Active"]
    all_mods = [m for m in mods if m is not None]

    def _calc_power(mod_list: List[Any]) -> tuple[float, float]:
        pwr_delta = 0.0
        for m in mod_list:
            pct = getattr(m, "power_pct", None)
            if pct is not None:
                pwr_delta += (pct - 100.0) / 100.0
        pwr_mult = 1.0 + pwr_delta
        return (laser.min_power or 0.0) * pwr_mult, (laser.max_power or 0.0) * pwr_mult

    def _calc_resistance(mod_list: List[Any]) -> float:
        res_values: List[float] = []
        if laser.resistance is not None:
            res_values.append(laser.resistance)
        turret_mod_sum = sum(
            m.resistance for m in mod_list
            if m is not None and m.resistance is not None
        )
        if turret_mod_sum:
            res_values.append(turret_mod_sum)
        # Note: gadget resistance NOT included here — handled by breakability layer
        factor = 1.0
        for v in res_values:
            factor *= 1.0 + v / 100.0
        return factor

    min_p, max_p = _calc_power(passive_mods)
    res_passive = _calc_resistance(passive_mods)

    min_a, max_a = _calc_power(all_mods)
    res_active = _calc_resistance(all_mods)

    # Active module metadata
    uses = 0
    duration = 0.0
    mod_names = ""
    if active_mods:
        use_values = [getattr(m, "uses", 0) for m in active_mods if getattr(m, "uses", 0) > 0]
        uses = min(use_values) if use_values else 0
        dur_values = [getattr(m, "duration", 0.0) or 0.0 for m in active_mods]
        duration = max(dur_values) if dur_values else 0.0
        mod_names = ", ".join(getattr(m, "name", "") for m in active_mods if getattr(m, "name", ""))

    return TurretStats(
        min_power_passive=min_p,
        max_power_passive=max_p,
        resistance_modifier_passive=res_passive,
        min_power_active=min_a,
        max_power_active=max_a,
        resistance_modifier_active=res_active,
        active_module_uses=uses,
        active_module_duration=duration,
        active_module_names=mod_names,
    )


def snapshot_to_laser_configs(
    snap: LoadoutSnapshot,
    force_refresh: bool = False,
) -> List[LaserConfig]:
    """Convert a parsed snapshot into LaserConfig objects.

    Uses Mining_Loadout's cached UEX item database to resolve laser,
    module, and gadget names into numeric stats, then computes a
    per-turret :class:`LaserConfig` suitable for feeding to
    :func:`services.breakability.power_percentage`.

    Returns an empty list (with a warning log) if the bridge is not
    available or if the loadout's lasers cannot be resolved against
    the current UEX data — the UI stays functional, breakability
    just reports "no laser setup loaded" until the user picks a ship
    whose items are in the database.

    Pass ``force_refresh=True`` to bypass the 24-hour cache and
    re-fetch the UEX data (rarely needed — only if the user updates
    their Mining_Loadout tool with new items).
    """
    if not snap:
        return []

    db = _load_item_db(force_refresh=force_refresh)
    if not db:
        return []

    lasers_by_name = db["lasers"]
    modules_by_name = db["modules"]
    gadgets_by_name = db["gadgets"]

    # Resolve the gadget once — it applies per turret.
    gadget_item = gadgets_by_name.get(snap.gadget) if snap.gadget else None

    configs: List[LaserConfig] = []
    for idx, turret in enumerate(snap.turrets):
        laser_item = lasers_by_name.get(turret.laser)
        if laser_item is None:
            # Empty slots in Mining_Loadout save as literal placeholders
            # like "— No Laser —"; skip silently.
            log.debug(
                "loadout_loader: turret %d laser %r not in item db — skipping",
                idx, turret.laser,
            )
            continue

        mod_items = [modules_by_name.get(m) for m in turret.modules]
        mod_items = [m for m in mod_items if m is not None]

        # Gadget is NOT passed to turret stats — gadget application is
        # handled by the breakability layer (compute_with_gadgets) so
        # gadgets can be toggled on/off independently.
        stats = _compute_turret_stats(laser_item, mod_items, gadget=None)

        turret_label = _ml_turret_label(snap.ship, idx)
        name = f"{turret_label}: {laser_item.name}"
        configs.append(LaserConfig(
            name=name,
            max_power=stats.max_power_passive,
            min_power=stats.min_power_passive,
            resistance_modifier=stats.resistance_modifier_passive,
            visible=True,
            max_power_active=stats.max_power_active,
            resistance_modifier_active=stats.resistance_modifier_active,
            active_module_uses=stats.active_module_uses,
            active_module_names=stats.active_module_names,
        ))

    if not configs:
        log.warning(
            "loadout_loader: no usable lasers resolved from %s "
            "(ship=%s, turrets=%d)",
            snap.source_path, snap.ship, len(snap.turrets),
        )

    return configs


def get_gadget_list() -> dict[str, Any]:
    """Return the UEX gadget database as {name: GadgetItem}.

    Used by the Gadgets tab to populate the list. Returns an empty
    dict if the Mining_Loadout bridge isn't available.
    """
    db = _load_item_db()
    if not db:
        return {}
    return db.get("gadgets", {})


def _ml_turret_label(ship: str, turret_index: int) -> str:
    """Return the human-readable turret name for a ship + index.

    Falls back to ``Turret {N}`` if the ship isn't in the Mining_Loadout
    ship definitions or the index is out of range.
    """
    mods = _ensure_ml_modules()
    if mods:
        try:
            ships = mods["items"].SHIPS
            cfg = ships.get(ship)
            if cfg and 0 <= turret_index < len(cfg.turret_names):
                return cfg.turret_names[turret_index]
        except Exception:
            pass
    return f"Turret {turret_index + 1}"
