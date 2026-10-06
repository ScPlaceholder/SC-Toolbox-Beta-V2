"""Put Pico Pals and Battle Buddy back in the middle of the main screen.

Settings > Tools > "Reset position for Pico Pals and Battle Buddy" (J, 2026-10-06: "in the case that
the user accidentally managed to get one stuck somewhere you cant grab it"). Both are small frameless
windows that are dragged around the desktop and remember where they were left, so either can end up
where the mouse cannot reach it: on a monitor that has since been unplugged, or under something.

The launcher never works out a coordinate. Each tool already knows how to centre itself on the
primary screen in its own units (Pico drops the launcher's UI scale, Battle Buddy keeps it), so the
reset only has to make the tool do that:

    tool running      {"type": "reset_position"} is appended to the command file the launcher already
                      sends show / hide / quit through (core/process_manager.py). The tool moves and
                      saves the new spot itself. Its settings file is NOT edited from here: Battle
                      Buddy writes its position back whenever its window is hidden, and a file changed
                      under a running tool would be overwritten with the old spot.
    tool not running  the position keys are removed from its settings file, and nothing else in it is
                      touched. A tool with no saved position opens centred.

A settings file that is missing, empty or not valid JSON is left exactly as it is: both tools read such
a file as "no settings" and centre themselves, so there is nothing to remove and nothing worth risking.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, NamedTuple

from shared import user_settings

log = logging.getLogger(__name__)

RESET_CMD = {"type": "reset_position"}

MOVED = "moved"        # running: told to centre itself now; it saves the new position itself
CLEARED = "cleared"    # not running: no saved position is left, so it opens centred
FAILED = "failed"      # a saved position is still there and could not be removed


class Target(NamedTuple):
    skill_id: str
    keys: tuple[str, ...]
    path: Callable[[], str]
    legacy: Callable[[str], str | None]
    legacy_name: str | None


def pico_settings_path() -> str:
    """%APPDATA%/PicoPal/settings.json, as tools/Pico/sprite_pal.py works it out."""
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "PicoPal", "settings.json")


def battle_buddy_settings_path() -> str:
    """~/.sctoolbox/battle_buddy/settings.json, as tools/Battle_Buddy/ui/options_popup.py works it out."""
    return os.path.join(os.path.expanduser("~"), ".sctoolbox", "battle_buddy", "settings.json")


def _battle_buddy_legacy(root: str) -> str | None:
    """The in-folder file Battle Buddy still reads once when it has no settings of its own yet."""
    return os.path.join(root, "tools", "Battle_Buddy", "battle_buddy_settings.json") if root else None


TARGETS = (
    Target("pico", ("x", "y"), pico_settings_path, lambda root: None, None),
    Target("battle_buddy", ("window_x", "window_y"), battle_buddy_settings_path,
           _battle_buddy_legacy, "battle_buddy_settings.json"),
)


def forget_position(path: str, keys: tuple[str, ...], legacy: str | None = None,
                    legacy_name: str | None = None) -> bool:
    """Remove keys from the JSON settings file at path; every other setting stays as it was.

    When path does not exist but the tool would migrate a legacy file on its next start, that file's
    settings are written to path without the keys: otherwise the migration would bring the old
    position straight back.

    True when no saved position is left for the tool to read. False only when one may still be there
    (the file could not be read or written)."""
    src = user_settings.source_path(path, legacy, legacy_name)
    if src is None:
        return True
    try:
        with open(src, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError:
        return True                      # empty or corrupt: the tool reads it as no settings at all
    except OSError as exc:
        log.warning("position reset: could not read %s: %s", src, exc)
        return False
    if not isinstance(data, dict):
        return True
    if src == path and not any(k in data for k in keys):
        return True                      # nothing saved: do not rewrite a file that needs no change
    kept = {k: v for k, v in data.items() if k not in keys}
    tmp = path + ".reset.tmp"
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(kept, fh, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("position reset: could not write %s: %s", path, exc)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False
    return True


def reset_positions(process_for: Callable[[str], Any] | None = None,
                    root: str | None = None) -> dict[str, str]:
    """Centre both tools. Returns {skill id: MOVED | CLEARED | FAILED}.

    process_for(skill_id) is the launcher's ProcessManager.get: the managed process, or None for a
    tool that is not registered (disabled, or not installed). root is the toolbox folder."""
    results: dict[str, str] = {}
    for target in TARGETS:
        mp = process_for(target.skill_id) if process_for is not None else None
        if mp is not None and mp.running and mp.send(dict(RESET_CMD)):
            results[target.skill_id] = MOVED
            continue
        ok = forget_position(target.path(), target.keys, target.legacy(root or ""), target.legacy_name)
        results[target.skill_id] = CLEARED if ok else FAILED
    log.info("position reset: %s", results)
    return results
