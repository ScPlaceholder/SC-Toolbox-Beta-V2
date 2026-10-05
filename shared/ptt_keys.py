"""ptt_keys.py - the two push-to-talk keys: their defaults, their names, and whether they are the same key.

J, 2026-10-05: "For the assistant and suit Mk 2 can you have individual push to talk buttons which also
auto-route to the right ai?"

Each tool keeps its key where it always has, in its own format:

    Assistant   ~/.sctoolbox/assistant_panel.json      "binding":  {"kind": "key", "code": "pause"}
                (assistant/voice_input.py InputBinding: a pynput key NAME or character)
    SuitMk2     ~/.sctoolbox/suitmk2/settings.json     "talk_key": {"kind": "keyboard", "code": 145, "label": ...}
                (core/voice_in/input_devices.py InputBinding: a virtual-key number plus the text to show)

Nothing here changes either file. This module is what lets something that is neither tool (the window the two
are tabs of, the launcher's Settings) read both, name both the same way, and tell when the two are one key.

THE DEFAULTS, for a tool that has never had a key set: Pause for the Assistant, Scroll Lock for SuitMk2. The
launcher's hotkeys are all Ctrl or Shift plus another key, so neither collides there. For the game: Star Citizen's default
keyboard profile uses the letters, the digits, F1-F12, the numpad and the navigation block heavily, and as far as
is known binds neither of these two. That has NOT been checked against the game's own default profile, which is
one reason both are rebindable. They are two different keys, so the two tools can never start out sharing one. A
key somebody already chose is never replaced by these.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

ASSISTANT = "assistant"
SUIT = "suitmk2"

ASSISTANT_DEFAULT: Dict[str, Any] = {"kind": "key", "code": "pause"}
# The same dict is DEFAULT_TALK_KEY in tools/SuitMk2/core/settings.py (which cannot import this package: the
# model service loads it in a process of its own); shared/tests/test_ptt_keys.py holds the two equal.
SUIT_DEFAULT: Dict[str, Any] = {"kind": "keyboard", "code": 145, "label": "SCROLL_LOCK", "joy_index": 0}

_ASSISTANT_STATE = os.path.join(os.path.expanduser("~"), ".sctoolbox", "assistant_panel.json")
_SUIT_SETTINGS = os.path.join(os.path.expanduser("~"), ".sctoolbox", "suitmk2", "settings.json")


def assistant_binding(state: Optional[dict]) -> Dict[str, Any]:
    """The Assistant's key from its saved state: what was saved, or the default when nothing usable was."""
    raw = (state or {}).get("binding")
    if isinstance(raw, dict) and raw.get("code"):
        return {"kind": str(raw.get("kind") or "key"), "code": str(raw["code"])}
    return dict(ASSISTANT_DEFAULT)


def suit_binding(settings: Optional[dict]) -> Dict[str, Any]:
    """SuitMk2's key from its settings: what was saved, or the default when none was (missing, or null)."""
    raw = (settings or {}).get("talk_key")
    if isinstance(raw, dict) and raw.get("code") is not None:
        return dict(raw)
    return dict(SUIT_DEFAULT)


def ident(binding: Optional[dict]) -> Optional[Tuple]:
    """Something two bindings share exactly when they are the same physical key or button, whichever tool's
    format each is in. None for no binding."""
    if not isinstance(binding, dict) or binding.get("code") in (None, ""):
        return None
    kind, code = str(binding.get("kind") or ""), binding.get("code")
    if kind == "key":                                   # Assistant: "pause", "f8", "z", sometimes "Key.pause"
        return ("key", str(code).replace("Key.", "").lower())
    if kind == "keyboard":                              # SuitMk2: the label is the same name, upper-cased
        name = str(binding.get("label") or "").lower()
        return ("key", name) if name else ("vk", int(code))
    if kind == "mouse":                                 # both store pynput's "Button.x1"
        return ("mouse", str(code).replace("Button.", "").lower())
    return (kind, int(binding.get("joy_index") or 0), str(code))


def label(binding: Optional[dict]) -> str:
    """The key as a person reads it: "Pause", "Scroll Lock", "Z", "F8", "Mouse X1". "" for no binding."""
    key = ident(binding)
    if key is None:
        return ""
    if key[0] == "key":
        name = key[1]
        return name.upper() if len(name) <= 3 else name.replace("_", " ").title()
    if key[0] == "mouse":
        return "Mouse " + key[1].upper()
    if key[0] == "vk":
        return "Key %d" % key[1]
    return str((binding or {}).get("label") or "%s button %s" % (key[0], key[2]))


def _read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def saved() -> Dict[str, Dict[str, Any]]:
    """Both tools' keys as they are on disk now (defaults where none was saved), by tab key."""
    return {ASSISTANT: assistant_binding(_read(_ASSISTANT_STATE)), SUIT: suit_binding(_read(_SUIT_SETTINGS))}


def saved_labels() -> Dict[str, str]:
    return {k: label(b) for k, b in saved().items()}
