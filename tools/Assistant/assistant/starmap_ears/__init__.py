"""The Star Map's former ears, moved here from skills/Starmap/starmap/voice/.

READ THIS BEFORE USING ANYTHING IN HERE. Nothing imports this package at run
time. The Assistant's live voice-to-text is ``assistant/voice.py``
(``EarsController``) with ``assistant/voice_input.py``; it was itself adapted
from this code and has since grown past it (self-hearing gate, echo filter,
the speaking signal).

Why it was moved rather than deleted (2026-10-04): J asked for voice-to-text to
live in one place, the Assistant, so the Star Map could stop owning - and
arming - a microphone. Two things in the Star Map's copy had no counterpart
here and should not be lost in a delete:

  * input_devices - bindings and a press-anything capture dialog that accept
    JOYSTICK and GAMEPAD buttons (via pygame) as well as keyboard and mouse.
    ``assistant/voice_input.py`` is keyboard + mouse only. Wiring a HOTAS
    button as the Assistant's mic key means porting this in; it is not done.
  * ears - the Star Map's EarsController. When the folder was moved its
    working copy carried an UNCOMMITTED change by someone else (the whisper
    preload / thread-count / no-timestamps work, ported from voice.py on
    2026-10-02). The move kept that change exactly as it was: uncommitted, on
    this file. It is theirs to commit or drop.

``mouth.py`` (Windows SAPI TTS) did not come along: ``assistant.voice.Mouth``
is the same class.

Still an optional, dependency-gated subsystem:

  * input_devices - bindings + the capture dialog (keyboard, mouse, joystick,
    gamepad).
  * ears - mic capture (sounddevice) + FastWhisper (faster-whisper)
    transcription behind a trigger binding, toggle or push-to-talk.
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
