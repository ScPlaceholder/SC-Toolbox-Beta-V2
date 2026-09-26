"""mic.py - which microphone the toolbox ears listen to (Star Map, Assistant, SuitMk2).

J, 2026-09-26: "nothing could hear me" in-game, headset mic. Two fixes he asked for, both here:

1. A PICKER. ``mic_device`` in ~/.sctoolbox/shared_settings.json holds a device NAME ("" = Windows default).
   Stored by name, never by index: PortAudio indices change when devices come and go.
2. RE-CHECK THE DEFAULT. PortAudio reads the device list once, when a process first uses sounddevice, so a
   headset plugged in (or a Windows default changed) after a tool started was invisible to it for the whole
   session. ``stream_device(refresh=True)`` re-reads the list before the ears open the mic - but only when no
   sounddevice stream is playing, because re-initialising PortAudio kills every stream in the process (it
   would cut Elah off mid-sentence). SuitMk2 streams its speech through its own OutputStream, which that check
   cannot see, so it calls with refresh=False and keeps its start-up list.

Names are listed from the default host API only, so a device appears once, not once per Windows audio API.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

SETTING_KEY = "mic_device"
DEFAULT_LABEL = "Windows default"


# -- the setting -------------------------------------------------------------------------------------------------
def get_choice() -> str:
    """The chosen device name, or '' for the Windows default."""
    try:
        from shared import sc_install
        return str(sc_install._load().get(SETTING_KEY) or "")
    except Exception:
        return ""


def set_choice(name: str) -> str:
    from shared import sc_install
    data = sc_install._load()
    data[SETTING_KEY] = str(name or "")
    sc_install._save(data)
    return data[SETTING_KEY]


# -- devices -----------------------------------------------------------------------------------------------------
def refresh_if_idle() -> bool:
    """Re-read the device list so a newly plugged mic / new Windows default is seen. Skipped (False) while a
    sounddevice convenience stream (sd.play) is active, because re-initialising PortAudio would kill it."""
    try:
        import sounddevice as sd
    except Exception:
        return False
    try:
        if sd.get_stream().active:
            return False
    except RuntimeError:
        pass                       # no convenience stream exists: nothing to interrupt
    except Exception:
        return False
    try:
        sd._terminate()
        sd._initialize()
        return True
    except Exception:
        log.exception("mic: could not refresh the device list")
        return False


def _inputs_on_default_api(sd) -> list[tuple[int, str]]:
    api = sd.default.hostapi
    out, seen = [], set()
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0 and dev["hostapi"] == api and dev["name"] not in seen:
            seen.add(dev["name"])
            out.append((i, dev["name"]))
    return out


def list_inputs(refresh: bool = False) -> list[str]:
    """Input device names a player can pick from (default host API only)."""
    try:
        import sounddevice as sd
        if refresh:
            refresh_if_idle()
        return [name for _i, name in _inputs_on_default_api(sd)
                if not name.lower().startswith("microsoft sound mapper")]
    except Exception:
        log.exception("mic: could not list input devices")
        return []


def stream_device(refresh: bool = True) -> Optional[int]:
    """The device index to open for the chosen mic, or None for the Windows default (also the fallback when the
    chosen mic is not connected, so the ears never go deaf because a saved device vanished)."""
    try:
        import sounddevice as sd
    except Exception:
        return None
    if refresh:
        refresh_if_idle()
    name = get_choice()
    if not name:
        return None
    try:
        for i, dev_name in _inputs_on_default_api(sd):
            if dev_name == name:
                return i
        log.warning("mic: chosen input %r is not connected; using the Windows default", name)
    except Exception:
        log.exception("mic: could not resolve %r", name)
    return None
