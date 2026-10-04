"""Config persistence with schema validation, defaults, and versioning."""
import json
import logging
import os
from typing import Any, Dict, List, Optional

from models.items import MAX_MODULE_SLOTS, NONE_GADGET, NONE_LASER, NONE_MODULE, SHIPS

log = logging.getLogger("MiningLoadout.config")

_LEGACY_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mining_loadout_config.json",
)
# Outside the install folder, which every update replaces; the old in-folder file is read once as a migration.
try:
    from shared.user_settings import settings_path, source_path
    _CONFIG_PATH = settings_path("mining_loadout", "config.json")
except ImportError:                  # run without the toolbox root on sys.path
    source_path = None
    _CONFIG_PATH = _LEGACY_CONFIG_PATH

# Crafted-laser fields are optional and additive, so they need no version bump:
# a file without them reads exactly as before, and a reader that predates them
# only looks up "laser"/"modules" and never sees the extra keys.
try:
    from shared import mining_crafting as _mc
except ImportError:                  # run without the toolbox root on sys.path
    _mc = None

CONFIG_VERSION = 2

_DEFAULT_CONFIG: Dict[str, Any] = {
    "version": CONFIG_VERSION,
    "ship": "MOLE",
    "hotkey": "ctrl+shift+m",
    "loadout": {},
    "gadget": NONE_GADGET,
}


def _validate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and migrate config, filling in defaults for missing keys.

    Returns a clean config dict with all required keys present.
    """
    cfg = dict(_DEFAULT_CONFIG)

    # Migrate v1 (no version key) to v2
    version = raw.get("version", 1)
    if version < CONFIG_VERSION:
        log.info("Migrating config from v%d to v%d", version, CONFIG_VERSION)

    # Ship
    ship = raw.get("ship", cfg["ship"])
    if ship not in SHIPS:
        log.warning("Unknown ship %r in config, falling back to %s", ship, cfg["ship"])
        ship = cfg["ship"]
    cfg["ship"] = ship

    # Hotkey
    hotkey = raw.get("hotkey", "")
    if isinstance(hotkey, str) and hotkey.strip():
        cfg["hotkey"] = hotkey.strip()

    # Loadout
    loadout = raw.get("loadout", {})
    if isinstance(loadout, dict):
        validated_loadout: Dict[str, Any] = {}
        ship_cfg = SHIPS.get(ship)
        max_turrets = ship_cfg.turrets if ship_cfg else 1
        for i in range(max_turrets):
            key = f"turret_{i}"
            td = loadout.get(key, {})
            if isinstance(td, dict):
                laser = td.get("laser", NONE_LASER)
                mods = td.get("modules", [NONE_MODULE] * MAX_MODULE_SLOTS)
                if not isinstance(mods, list):
                    mods = [NONE_MODULE] * MAX_MODULE_SLOTS
                # Pad to MAX_MODULE_SLOTS
                while len(mods) < MAX_MODULE_SLOTS:
                    mods.append(NONE_MODULE)
                validated_loadout[key] = {"laser": str(laser), "modules": [str(m) for m in mods[:MAX_MODULE_SLOTS]]}
                crafted = _mc.clean_crafted(td.get(_mc.FIELD_CRAFTED)) if _mc else None
                if crafted is not None:
                    validated_loadout[key][_mc.FIELD_CRAFTED] = crafted
            else:
                validated_loadout[key] = {"laser": NONE_LASER, "modules": [NONE_MODULE] * MAX_MODULE_SLOTS}
        cfg["loadout"] = validated_loadout
    else:
        cfg["loadout"] = {}

    # Gadget
    gadget = raw.get("gadget", NONE_GADGET)
    cfg["gadget"] = str(gadget) if isinstance(gadget, str) else NONE_GADGET

    # Crafted combine rule (optional; only kept when it names a known rule)
    if _mc and raw.get(_mc.FIELD_COMBINE) in _mc.COMBINE_RULES:
        cfg[_mc.FIELD_COMBINE] = raw[_mc.FIELD_COMBINE]

    cfg["version"] = CONFIG_VERSION
    return cfg


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load config from disk with validation.

    Returns a validated config dict with all required keys.
    Falls back to defaults on any error.
    """
    config_path = path
    if config_path is None:
        config_path = _CONFIG_PATH
        if source_path is not None:
            config_path = source_path(_CONFIG_PATH, _LEGACY_CONFIG_PATH) or _CONFIG_PATH
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            log.warning("Config file is not a dict, using defaults")
            return dict(_DEFAULT_CONFIG)
        return _validate_config(raw)
    except FileNotFoundError:
        log.info("No config file found, using defaults")
        return dict(_DEFAULT_CONFIG)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Config load failed (%s), using defaults", exc)
        return dict(_DEFAULT_CONFIG)


def save_config(
    ship: str,
    hotkey: str,
    turret_lasers: List[str],
    turret_modules: List[List[str]],
    gadget: str,
    path: Optional[str] = None,
    turret_crafted: Optional[List[Optional[Dict[str, Any]]]] = None,
    crafted_combine: Optional[str] = None,
) -> bool:
    """Save config to disk. Returns True on success.

    turret_crafted: optional per-turret "crafted" object (None = not crafted).
    crafted_combine: the combine rule the crafted factors were computed with.
    With neither given the file is byte-for-byte what it was before crafting existed.
    """
    config_path = path or _CONFIG_PATH
    loadout: Dict[str, Any] = {}
    for i in range(len(turret_lasers)):
        mods = turret_modules[i] if i < len(turret_modules) else [NONE_MODULE] * MAX_MODULE_SLOTS
        loadout[f"turret_{i}"] = {
            "laser": turret_lasers[i],
            "modules": list(mods),
        }
        crafted = turret_crafted[i] if turret_crafted and i < len(turret_crafted) else None
        if crafted and _mc:
            loadout[f"turret_{i}"][_mc.FIELD_CRAFTED] = crafted

    cfg = {
        "version": CONFIG_VERSION,
        "ship": ship,
        "hotkey": hotkey,
        "loadout": loadout,
        "gadget": gadget,
    }
    if crafted_combine and _mc:
        cfg[_mc.FIELD_COMBINE] = crafted_combine

    try:
        os.makedirs(os.path.dirname(config_path) or ".", exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        return True
    except OSError as exc:
        log.warning("Config save failed: %s", exc)
        return False
