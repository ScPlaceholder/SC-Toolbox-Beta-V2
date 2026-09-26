"""Battle Buddy worker: current FPS loadout, replayed from Game.log.

Same pipeline as the HUD's startup backfill (hud_app._backfill_active_
session): find the last "{Join PU}", feed every later line through
InventoryParser -> InventoryTracker. The log is opened read-only; the HUD's
settings file is read (for a custom log path), never written.
"""
from __future__ import annotations

import json
import os
import time

import _assist_logic as L

from core.inventory_parser import InventoryParser
from core.inventory_tracker import InventoryTracker

_CANDIDATES = (
    r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"C:\Star Citizen\StarCitizen\LIVE\Game.log",
    r"C:\StarCitizen\LIVE\Game.log",
    r"D:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"D:\StarCitizen\LIVE\Game.log",
)


_SETTINGS = (
    os.path.join(os.path.expanduser("~"), ".sctoolbox", "battle_buddy", "settings.json"),
    os.path.join(os.getcwd(), "battle_buddy_settings.json"),        # pre-2.4 location
)


def _log_path() -> str:
    for settings in _SETTINGS:
        try:
            with open(settings, encoding="utf-8") as f:
                p = (json.load(f) or {}).get("log_path") or ""
            if p and os.path.isfile(p):
                return p
        except (OSError, ValueError):
            continue
    try:                                  # the launcher's shared install folder
        from shared.sc_install import get_sc_root, newest_game_log
        p = newest_game_log(get_sc_root())
        if p:
            return p
    except ImportError:
        pass
    for p in _CANDIDATES:
        if os.path.isfile(p):
            return p
    return ""


def _session_lines(path: str):
    """Lines from the last {Join PU} to EOF, and whether the session ended."""
    cap = 64 * 1024 * 1024          # a session never needs more than the tail
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(max(0, size - cap))
        data = fh.read().decode("utf-8", "replace")
    idx = data.rfind("{Join PU}")
    if idx < 0:
        return None, False
    start = data.rfind("\n", 0, idx) + 1
    lines = [ln for ln in data[start:].split("\n") if ln.strip()]
    ended = any(("SystemQuit" in ln or "Disconnecting from" in ln) for ln in lines)
    return lines, ended


def current_loadout() -> dict:
    path = _log_path()
    if not path:
        raise L.ToolFail("could not find Star Citizen's Game.log (set it in Battle Buddy's options)")
    lines, ended = _session_lines(path)
    if lines is None:
        return {"empty": True, "log_path": path,
                "note": "no {Join PU} in the current Game.log: not in the verse this session"}
    parser, tracker = InventoryParser(), InventoryTracker()
    parser.subscribe(tracker.on_event)
    for ln in lines:
        parser.on_line(ln)
    if hasattr(tracker, "flush"):
        tracker.flush()
    s = tracker.get_state()
    weapons = []
    for slot, w in sorted(s.weapons.items()):
        weapons.append({"slot": slot, "name": w.display_name or w.class_name,
                        "type": w.weapon_type, "ammo": w.ammo_type,
                        "spare_mags": w.spare_mags, "module": w.module or None})
    pens = {k: len(v) for k, v in s.pens_by_category().items()}
    res = {
        "weapons": weapons,
        "medpens": s.medpens, "oxypens": s.oxypens, "pens_by_type": pens,
        "grenades": s.grenades_by_type(),
        "session_active": not ended,
        "log_path": path,
        "log_age_min": round((time.time() - os.path.getmtime(path)) / 60, 1),
        "lines_replayed": len(lines),
    }
    if ended:
        res["note"] = "the last session in Game.log has ended; this is the loadout at its end"
    if not weapons and not pens and not s.grenades:
        res["empty"] = True
        res["note"] = (res.get("note", "") + " no loadout events since joining").strip()
    return res


EXPORTS = {"current_loadout": current_loadout}
