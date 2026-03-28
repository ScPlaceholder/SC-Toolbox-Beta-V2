"""
Dynamic skill discovery — replaces the static SKILLS list.

Scans the ``skills/`` directory for ``skill.json`` metadata files.
Falls back to a built-in default list so existing skill folders
(which may not yet have a skill.json) continue to work.
"""
from __future__ import annotations

import json
import logging
import os

from shared.config_models import SkillConfig
from shared.i18n import N_

log = logging.getLogger(__name__)

# Built-in defaults — used when a skill folder has no skill.json
_BUILTIN_SKILLS: list[dict] = [
    {
        "id": "dps", "name": N_("DPS Calculator"), "icon": "\u2694",
        "color": "#ff7733", "folder": "DPS_Calculator",
        "script": "dps_calc_app.py", "hotkey": "<shift>+1",
        "settings_key": "hotkey_dps",
    },
    {
        "id": "cargo", "name": N_("Cargo Loader"), "icon": "\U0001f4e6",
        "color": "#33ccdd", "folder": "Cargo_loader",
        "script": "cargo_app.py", "hotkey": "<shift>+2",
        "settings_key": "hotkey_cargo",
    },
    {
        "id": "missions", "name": N_("Mission Database"), "icon": "\U0001f4cb",
        "color": "#33dd88", "folder": "Mission_Database",
        "script": "mission_db_app.py", "hotkey": "<shift>+3",
        "settings_key": "hotkey_missions",
    },
    {
        "id": "mining", "name": N_("Mining Loadout"), "icon": "\u26cf",
        "color": "#ffaa22", "folder": "Mining_Loadout",
        "script": "mining_loadout_app.py", "hotkey": "<shift>+4",
        "settings_key": "hotkey_mining",
    },
    {
        "id": "market", "name": N_("Market Finder"), "icon": "\U0001f6d2",
        "color": "#aa66ff", "folder": "Market_Finder",
        "script": "uex_item_browser.py", "hotkey": "<shift>+5",
        "settings_key": "hotkey_market",
    },
    {
        "id": "trade", "name": N_("Trade Hub"), "icon": "\U0001f4b0",
        "color": "#ffcc00", "folder": "Trade_Hub",
        "script": "trade_hub_app.py", "hotkey": "<shift>+6",
        "settings_key": "hotkey_trade",
        "custom_args": ["300", "500"],
    },
    {
        "id": "craft_db", "name": N_("Craft Database"), "icon": "\U0001f3ed",
        "color": "#44ccbb", "folder": "Craft_Database",
        "script": "craft_db_app.py", "hotkey": "<shift>+7",
        "settings_key": "hotkey_craft_db",
    },
]

_BUILTIN_INDEX: dict[str, dict] = {s["id"]: s for s in _BUILTIN_SKILLS}


def _try_load_skill_json(skill_dir: str) -> SkillConfig | None:
    """Load a ``skill.json`` from *skill_dir*, or return None."""
    path = os.path.join(skill_dir, "skill.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        cfg = SkillConfig.from_dict(data)
        if not cfg.id or not cfg.script:
            log.warning("skill_registry: invalid skill.json in %s (missing id/script)", skill_dir)
            return None
        return cfg
    except (json.JSONDecodeError, OSError, KeyError, TypeError) as exc:
        log.warning("skill_registry: failed to load %s: %s", path, exc)
        return None


def discover_skills(base_dir: str) -> list[SkillConfig]:
    """Scan for skills and return an ordered list of SkillConfig objects.

    Discovery order:
    1. Scan ``<base_dir>/skills/`` for directories containing ``skill.json``
    2. For known built-in skills whose folders exist but lack ``skill.json``,
       use the built-in default metadata
    3. Result is sorted: discovered skills first (alphabetical), then
       built-in skills in their canonical order

    Parameters
    ----------
    base_dir:
        The SC_Toolbox root directory (contains ``skills/``).
    """
    skills_root = os.path.join(base_dir, "skills")
    parent_skills = os.path.dirname(base_dir)  # custom_skills/ level

    found: dict[str, SkillConfig] = {}

    # Phase 1: scan for skill.json files
    if os.path.isdir(skills_root):
        try:
            for entry in sorted(os.listdir(skills_root)):
                entry_path = os.path.join(skills_root, entry)
                if not os.path.isdir(entry_path):
                    continue
                cfg = _try_load_skill_json(entry_path)
                if cfg:
                    # Override folder to match actual directory name
                    cfg.folder = entry
                    found[cfg.id] = cfg
                    log.debug("skill_registry: discovered %s from skill.json", cfg.id)
        except OSError as exc:
            log.warning("skill_registry: error scanning %s: %s", skills_root, exc)

    # Phase 2: fill in built-in skills that weren't discovered via skill.json
    result: list[SkillConfig] = []
    for builtin in _BUILTIN_SKILLS:
        sid = builtin["id"]
        if sid in found:
            result.append(found.pop(sid))
            continue

        # Check if the folder exists (either under skills/ or parent custom_skills/)
        local = os.path.join(skills_root, builtin["folder"])
        parent = os.path.join(parent_skills, builtin["folder"])
        if os.path.isdir(local) or os.path.isdir(parent):
            result.append(SkillConfig.from_dict(builtin))
            log.debug("skill_registry: using built-in metadata for %s", sid)

    # Phase 3: append any extra discovered skills not in the built-in list
    for cfg in sorted(found.values(), key=lambda c: c.name):
        result.append(cfg)

    log.info("skill_registry: %d skill(s) registered", len(result))
    return result


def resolve_skill_path(skill: SkillConfig, base_dir: str) -> str | None:
    """Return the absolute directory path for a skill, or None if not found."""
    skills_root = os.path.join(base_dir, "skills")
    parent_skills = os.path.dirname(base_dir)

    local = os.path.join(skills_root, skill.folder)
    if os.path.isdir(local):
        return local
    parent = os.path.join(parent_skills, skill.folder)
    if os.path.isdir(parent):
        return parent
    return None


def resolve_script_path(skill: SkillConfig, base_dir: str) -> str | None:
    """Return the absolute path to the skill's entry script, or None."""
    folder = resolve_skill_path(skill, base_dir)
    if not folder:
        return None
    script = os.path.join(folder, skill.script)
    return script if os.path.isfile(script) else None
