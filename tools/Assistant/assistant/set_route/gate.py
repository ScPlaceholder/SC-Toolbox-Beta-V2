"""The In-Game switch: nothing is sent to the game while it is off.

Setting a route in game takes the mouse and keyboard for about twelve seconds.
The Star Map has always required its "In-Game" button to be on before it did
that. The switch moved here with the code it guards, and it is ONE switch: a
small file both the Assistant's and the Star Map's "In-Game" buttons read and
write, so turning it off in either window turns it off for both.

    ~/.sctoolbox/set_route/settings.json   {"in_game": true}

Until the pilot touches the switch after this move, their old choice is read
from the Star Map's saved state ("game_route"). That file is only read here.

Fails closed: a settings file that exists but cannot be read means OFF.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

KEY = "in_game"
LEGACY_KEY = "game_route"


def settings_path() -> str:
    return os.path.join(os.path.expanduser("~"), ".sctoolbox", "set_route", "settings.json")


def legacy_path() -> str:
    """The Star Map's state file, where the switch was saved before 2026-10-04."""
    return os.path.join(os.path.expanduser("~"), ".sctoolbox", "starmap", "starmap_state.json")


def _load(path: str) -> Optional[dict]:
    """The JSON object at *path*; ``None`` when there is no such file.

    Raises ValueError when the file is there but is not a JSON object."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError("not a JSON object")
    return data


def in_game_enabled(legacy: Optional[bool] = None) -> bool:
    """True when the pilot has switched in-game plotting on.

    *legacy* is the Star Map's saved "game_route" when the caller already has
    it (the Star Map does); otherwise it is read from the Star Map's state
    file. It only counts while the switch has never been set here."""
    try:
        own = _load(settings_path())
    except (OSError, ValueError) as exc:
        log.warning("set route: cannot read the In-Game switch at %s (%s: %s); treating it as OFF",
                    settings_path(), type(exc).__name__, exc)
        return False
    if own is not None and KEY in own:
        return own[KEY] is True
    if legacy is not None:
        return legacy is True
    try:
        old = _load(legacy_path())
    except (OSError, ValueError):
        return False
    return bool(old) and old.get(LEGACY_KEY) is True


def set_in_game(on: bool) -> bool:
    """Save the switch. Returns False (and logs) when it could not be written."""
    path = settings_path()
    try:
        try:
            data = _load(path) or {}
        except ValueError:
            data = {}
        data[KEY] = bool(on)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.warning("set route: could not save the In-Game switch to %s (%s: %s)",
                    path, type(exc).__name__, exc)
        return False
