"""Resolve the Star Citizen game resolution for the region detectors.

The HUD finders sweep many template scales because the SCAN RESULTS
panel and signature glyphs render at different pixel sizes depending on
the player's resolution. If we know the game's actual output
resolution we can predict the right scale and narrow that sweep —
faster, and more robust on non-reference displays.

Resolution is resolved in priority order:

  1. **Manual override** — ``config["game_resolution"]`` = ``{"w","h"}``
     set by the user via the Game Resolution dialog. Wins over
     everything (the user knows their setup).
  2. **Game.log** — Star Citizen writes its output resolution every
     session, e.g. ``Change resolution: 3840x2160 (Borderless at …)``.
     This is the resolution ``mss`` captures and the one HUD glyph size
     scales with, so it's the ideal automatic source. We take the LAST
     such line (the user may have changed settings mid-session).
  3. **Desktop** — native primary-monitor size via ``mss`` as a last
     resort when no log is found.

Note: the ``Reallocating render resources with scale factor 0.67 …``
line in Game.log is the *3D-scene* render scale (graphics quality), NOT
the HUD size — the UI is composited at the output resolution — so we
deliberately ignore it and key off ``Change resolution`` instead.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

log = logging.getLogger(__name__)

# "Change resolution: 3840x2160 (Borderless at 59.997Hz)"
_CHANGE_RES_RE = re.compile(r"Change resolution:\s*(\d{3,5})x(\d{3,5})")
# "Current display mode is 3840x2160x32"  (fallback within the log)
_DISPLAY_MODE_RE = re.compile(r"Current display mode is\s*(\d{3,5})x(\d{3,5})")

# Common Star Citizen install locations to probe when the config's
# game_dir isn't set. The first existing Game.log wins.
_COMMON_LOG_PATHS = (
    r"C:\Star Citizen\StarCitizen\LIVE\Game.log",
    r"C:\Program Files\Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"C:\Program Files (x86)\Roberts Space Industries\StarCitizen\LIVE\Game.log",
    r"D:\Roberts Space Industries\StarCitizen\LIVE\Game.log",
)

# Resolution the detector templates / world-model were calibrated at.
# The training captures (user_20260418) were taken at 4K, so a player at
# 2160p sits at the reference; the scale prior is the ratio of their
# vertical resolution to this. Kept here as the single source of truth
# so the detector-side scale math has one place to change.
REFERENCE_HEIGHT = 2160


def _discover_game_log(config: Optional[dict]) -> Optional[str]:
    """Return the first existing Game.log path: config game_dir, then
    the common install locations."""
    candidates: list[str] = []
    if config:
        gd = config.get("game_dir")
        if gd:
            candidates.append(os.path.join(str(gd), "Game.log"))
    candidates.extend(_COMMON_LOG_PATHS)
    for c in candidates:
        try:
            if c and os.path.isfile(c):
                return c
        except Exception:
            continue
    return None


def parse_log_resolution(log_path: str) -> Optional[tuple[int, int]]:
    """Return ``(w, h)`` from the LAST ``Change resolution`` line in the
    log, falling back to the last ``Current display mode`` line, else
    ``None``.

    Scans the whole file but only touches resolution-bearing lines — the
    log is small (a few hundred KB) and this runs off the GUI thread.
    """
    last_change: Optional[tuple[int, int]] = None
    last_mode: Optional[tuple[int, int]] = None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cheap pre-filter before the regex.
                low = line.lower()
                if "resolution" not in low and "display mode" not in low:
                    continue
                m = _CHANGE_RES_RE.search(line)
                if m:
                    last_change = (int(m.group(1)), int(m.group(2)))
                    continue
                m2 = _DISPLAY_MODE_RE.search(line)
                if m2:
                    last_mode = (int(m2.group(1)), int(m2.group(2)))
    except OSError as exc:
        log.debug("parse_log_resolution(%s): %s", log_path, exc)
        return None
    return last_change or last_mode


def desktop_resolution() -> Optional[tuple[int, int]]:
    """Native primary-monitor resolution via ``mss`` (matches capture
    pixels), or ``None`` if unavailable."""
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1]  # [0] = virtual desktop, [1] = primary
            return (int(mon["width"]), int(mon["height"]))
    except Exception as exc:
        log.debug("desktop_resolution failed: %s", exc)
        return None


def get_game_resolution(config: Optional[dict]) -> dict:
    """Resolve the effective game resolution.

    Returns ``{"w": int, "h": int, "source": str}`` where ``source`` is
    one of ``"manual"`` / ``"game.log"`` / ``"desktop"`` / ``"unknown"``.
    ``w``/``h`` are ``0`` only when nothing could be determined.
    """
    # 1. Manual override.
    if config:
        ov = config.get("game_resolution")
        if isinstance(ov, dict):
            try:
                w, h = int(ov.get("w") or 0), int(ov.get("h") or 0)
                if w > 0 and h > 0:
                    return {"w": w, "h": h, "source": "manual"}
            except (TypeError, ValueError):
                pass

    # 2. Game.log.
    log_path = _discover_game_log(config)
    if log_path:
        res = parse_log_resolution(log_path)
        if res:
            return {"w": res[0], "h": res[1], "source": "game.log"}

    # 3. Desktop.
    d = desktop_resolution()
    if d:
        return {"w": d[0], "h": d[1], "source": "desktop"}

    return {"w": 0, "h": 0, "source": "unknown"}


def predicted_scale(config: Optional[dict]) -> Optional[float]:
    """Scale of the HUD relative to the reference capture resolution.

    ``1.0`` means the player is at the reference height (2160p); ``0.5``
    means ~1080p (HUD elements render half as tall). Returns ``None``
    when the resolution can't be determined, so callers can fall back to
    the detectors' full multi-scale sweep.
    """
    res = get_game_resolution(config)
    if res["h"] <= 0:
        return None
    return res["h"] / float(REFERENCE_HEIGHT)
