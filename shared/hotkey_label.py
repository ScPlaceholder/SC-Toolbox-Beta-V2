"""The hotkey a tool's title bar should SHOW: its real binding, not a literal.

Every tool used to hardcode its label ("Ctrl+1", "Shift+7"...), so rebinding a
key in the launcher left the old label on screen, and a changed default left a
stale one in the source.  This reads the tool's binding from the launcher's
settings file (the same key the launcher binds with) and falls back to the
tool's own default.

    hotkey_label("hotkey_mining_signals", "<ctrl>+1")   # -> "Ctrl+1"
"""
from __future__ import annotations

import json
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from shared.user_settings import launcher_settings, load_json
SETTINGS_FILE, _LEGACY_SETTINGS_FILE = launcher_settings(_ROOT)

_NAMES = {"ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt",
          "cmd": "Win", "win": "Win", "super": "Win"}


def format_hotkey(binding: str) -> str:
    """'<ctrl>+<shift>+f5' -> 'Ctrl+Shift+F5';  '<shift>+`' -> 'Shift+`'."""
    parts = []
    for raw in (binding or "").split("+"):
        tok = raw.strip().strip("<>")
        if not tok:
            continue
        low = tok.lower()
        parts.append(_NAMES.get(low) or (tok.upper() if len(tok) <= 3 else tok.capitalize()))
    return "+".join(parts)


def hotkey_label(settings_key: str, default: str) -> str:
    """The display label for the tool whose launcher settings key is ``settings_key``."""
    binding = default
    try:
        saved = load_json(SETTINGS_FILE, legacy=_LEGACY_SETTINGS_FILE).get(settings_key)
        if isinstance(saved, str) and saved.strip():
            binding = saved
    except AttributeError:
        pass            # garbled launcher settings: the tool's default is the binding
    return format_hotkey(binding)
