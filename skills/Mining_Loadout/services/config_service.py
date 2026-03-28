"""Config persistence with schema validation, defaults, and versioning."""
import json
import logging
import os
from typing import Any, Dict, List, Optional

from models.items import NONE_GADGET, NONE_LASER, NONE_MODULE, SHIPS

log = logging.getLogger("MiningLoadout.config")

_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "mining_loadout_config.json",
)

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
                mods = td.get("modules", [NONE_MODULE, NONE_MODULE])
                if not isinstance(mods, list):
                    mods = [NONE_MODULE, NONE_MODULE]
                # Pad to 2 slots
                while len(mods) < 2:
                    mods.append(NONE_MODULE)
                validated_loadout[key] = {"laser": str(laser), "modules": [str(m) for m in mods[:2]]}
            else:
                validated_loadout[key] = {"laser": NONE_LASER, "modules": [NONE_MODULE, NONE_MODULE]}
        cfg["loadout"] = validated_loadout
    else:
        cfg["loadout"] = {}

    # Gadget
    gadget = raw.get("gadget", NONE_GADGET)
    cfg["gadget"] = str(gadget) if isinstance(gadget, str) else NONE_GADGET

    cfg["version"] = CONFIG_VERSION
    return cfg


def load_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load config from disk with validation.

    Returns a validated config dict with all required keys.
    Falls back to defaults on any error.
    """
    config_path = path or _CONFIG_PATH
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
) -> bool:
    """Save config to disk. Returns True on success."""
    config_path = path or _CONFIG_PATH
    loadout: Dict[str, Any] = {}
    for i in range(len(turret_lasers)):
        mods = turret_modules[i] if i < len(turret_modules) else [NONE_MODULE, NONE_MODULE]
        loadout[f"turret_{i}"] = {
            "laser": turret_lasers[i],
            "modules": list(mods),
        }

    cfg = {
        "version": CONFIG_VERSION,
        "ship": ship,
        "hotkey": hotkey,
        "loadout": loadout,
        "gadget": gadget,
    }

    try:
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        return True
    except OSError as exc:
        log.warning("Config save failed: %s", exc)
        return False
