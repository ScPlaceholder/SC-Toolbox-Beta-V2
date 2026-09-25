"""Voice command ears for the Starmap tool.

An optional, dependency-gated subsystem:

  * input_devices — bindings + a press-anything capture dialog that
    accepts keyboard keys, mouse buttons, and (via pygame) joystick /
    gamepad buttons.
  * ears — the EarsController: mic capture (sounddevice) + FastWhisper
    (faster-whisper) transcription behind a trigger binding, toggle or
    push-to-talk.
  * commands - the CommandRouter mapping transcripts to panel actions.
    Its register() is public: repurposed WingmanAI/set_route logic
    (destination_engine, route_setter) plugs in there.
  * mouth — spoken feedback: Windows SAPI TTS (PowerShell System.Speech)
    behind a speak queue, so the voice loop can confirm actions aloud
    ("Navigate to Area 18"). No pip dependency; silently absent off Windows.


Every dependency is optional — the panel degrades to a disabled ears
button with a "pip install ..." tooltip when pieces are missing.
"""
from __future__ import annotations

import importlib
from typing import List

# module -> pip package name
_DEPS = {
    "sounddevice": "sounddevice",
    "faster_whisper": "faster-whisper",
    "numpy": "numpy",
    "pynput": "pynput",
}


def missing_deps() -> List[str]:
    """pip packages missing for core ears (mic + whisper + trigger)."""
    out = []
    for mod, pip in _DEPS.items():
        try:
            importlib.import_module(mod)
        except ImportError:
            out.append(pip)
    return out


def joystick_available() -> bool:
    try:
        importlib.import_module("pygame")
        return True
    except ImportError:
        return False
