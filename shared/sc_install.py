"""One shared answer to "where is Star Citizen installed?".

Before this, five tools each found the game on their own, with five different
detectors, five settings files, and three different SHAPES of answer (a
Game.log path, a channel folder like .../LIVE, or the install root).  A user
with the game in an unusual place had to point every tool at it separately.

Now the launcher asks once (first-launch popup) and stores the INSTALL ROOT,
the folder that holds LIVE / PTU / ...  Each tool falls back to it whenever
its own setting is empty or points somewhere that no longer exists, so a
user's deliberate per-tool choice still wins.

    root = get_sc_root()            # saved root, or None
    root = get_or_detect_sc_root()  # saved, else detected (not saved)
    channel_dir(root, "LIVE")       # .../StarCitizen/LIVE
    newest_game_log(root)           # newest Game.log across channels

Stored in ~/.sctoolbox/shared_settings.json, OUTSIDE the install folder,
because a Velopack update replaces the install folder wholesale.
"""
from __future__ import annotations

import json
import logging
import os
import string
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SHARED_DIR = Path.home() / ".sctoolbox"
SHARED_FILE = SHARED_DIR / "shared_settings.json"

CHANNELS = ("LIVE", "PTU", "EPTU", "HOTFIX", "TECH-PREVIEW", "TECH-PREVIEW-2")

# Union of every install root the individual tools used to probe, per drive.
_ROOT_PATTERNS = (
    "Program Files/Roberts Space Industries/StarCitizen",
    "Roberts Space Industries/StarCitizen",
    "Star Citizen/StarCitizen",
    "StarCitizen",
    "Games/StarCitizen",
    "Games/Star Citizen/StarCitizen",
    "Games/Roberts Space Industries/StarCitizen",
    "Program Files (x86)/Roberts Space Industries/StarCitizen",
)


# ── storage ──────────────────────────────────────────────────────────────────

def _load() -> dict:
    try:
        with open(SHARED_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("shared settings unreadable (%s); treating as empty", exc)
        return {}


def _save(data: dict) -> None:
    SHARED_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SHARED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, SHARED_FILE)


# ── shape handling ───────────────────────────────────────────────────────────

def is_install_root(path: str | os.PathLike | None) -> bool:
    """True if ``path`` is a folder holding at least one channel folder."""
    if not path:
        return False
    p = Path(path)
    return p.is_dir() and any((p / ch).is_dir() for ch in CHANNELS)


def normalise_root(path: str | os.PathLike | None) -> Optional[str]:
    """Turn whatever a user picked into the install root, or None.

    Accepts the root itself, a channel folder (.../LIVE), a Game.log path, or
    a logbackups folder, because people pick all of those.
    """
    if not path:
        return None
    p = Path(path)
    if p.is_file():
        p = p.parent
    for cand in (p, p.parent, p.parent.parent):
        if is_install_root(cand):
            return str(cand).replace("\\", "/")
    return None


def channel_dir(root: str, channel: str = "LIVE") -> str:
    return str(Path(root) / channel).replace("\\", "/")


def newest_game_log(root: Optional[str]) -> Optional[str]:
    """The most recently written Game.log under ``root`` (any channel)."""
    if not root:
        return None
    logs = [Path(root) / ch / "Game.log" for ch in CHANNELS]
    logs = [p for p in logs if p.is_file()]
    if not logs:
        return None
    return str(max(logs, key=lambda p: p.stat().st_mtime)).replace("\\", "/")


# ── detection ────────────────────────────────────────────────────────────────

def detect_sc_root() -> Optional[str]:
    """Probe every drive for an install root; prefer the most recently played."""
    found: list[Path] = []
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:/")
        try:
            if not drive.is_dir():
                continue
        except OSError:
            continue
        for pat in _ROOT_PATTERNS:
            cand = drive / pat
            try:
                if is_install_root(cand):
                    found.append(cand)
            except OSError:
                continue
    if not found:
        return None

    def recency(root: Path) -> float:
        log_ = newest_game_log(str(root))
        return Path(log_).stat().st_mtime if log_ else 0.0

    return str(max(found, key=recency)).replace("\\", "/")


# ── public API ───────────────────────────────────────────────────────────────

def get_sc_root() -> Optional[str]:
    """The saved install root, if it is still a real install."""
    root = _load().get("sc_root")
    return root if is_install_root(root) else None


def set_sc_root(path: str | os.PathLike) -> str:
    """Save ``path`` (any accepted shape) as the install root; returns it."""
    root = normalise_root(path)
    if root is None:
        raise ValueError(f"not a Star Citizen install folder: {path}")
    data = _load()
    data["sc_root"] = root
    data["sc_prompt_done"] = True
    _save(data)
    return root


def get_or_detect_sc_root() -> Optional[str]:
    return get_sc_root() or detect_sc_root()


def first_launch_prompt_needed() -> bool:
    """Show the popup only until the user has answered it once (either way)."""
    return not _load().get("sc_prompt_done") and get_sc_root() is None


def mark_prompt_done() -> None:
    """User chose Skip: don't ask again; tools keep auto-detecting."""
    data = _load()
    data["sc_prompt_done"] = True
    _save(data)
