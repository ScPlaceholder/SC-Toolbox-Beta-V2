"""Star Citizen game.log scanner.

Extracts blueprint names from the live Game.log file and all logbackups,
and matches them against the loaded crafting blueprint data so they can be
auto-marked as owned.

Inspired by Battle Buddy's SC install auto-detection.
"""

from __future__ import annotations

import json
import logging
import os
import re
import string
import threading
import time
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

_NEW_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "mission_db")
_SETTINGS_PATH = os.path.join(_NEW_DIR, "settings.json")
_OLD_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".mission_db_cache")
_OLD_SETTINGS_PATH = os.path.join(_OLD_CACHE_DIR, "settings.json")

# Matches lines like: "Received Blueprint: <name>: <extra>"
_BP_PATTERN = re.compile(r"Received Blueprint:\s*(.*?):")


# ── Settings persistence ───────────────────────────────────────────────────

def load_settings() -> dict:
    # Try new path first
    try:
        with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        log.warning("Settings corrupted at new path, trying legacy")
    # Fallback to old path + migrate
    try:
        with open(_OLD_SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        log.info("Migrating settings from legacy path: %s", _OLD_SETTINGS_PATH)
        save_settings(data)
        return data
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        log.warning("Legacy settings also corrupted, starting fresh")
        return {}


def save_settings(s: dict) -> None:
    try:
        os.makedirs(os.path.dirname(_SETTINGS_PATH), exist_ok=True)
        with open(_SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError as e:
        log.warning("Failed to save settings: %s", e)


# ── SC folder discovery ────────────────────────────────────────────────────

_CHANNELS = ("LIVE", "HOTFIX", "PTU", "EPTU", "TECH-PREVIEW")
_LOG_NAMES = ("Game.log", "game.log")


def _log_file_in(folder: str) -> Optional[str]:
    for name in _LOG_NAMES:
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            return p
    return None


def auto_detect_sc_folder() -> Optional[str]:
    """Scan drives A-Z for common SC install paths. Returns the channel
    folder (e.g. ".../StarCitizen/LIVE") of the most recently updated Game.log."""
    candidates: list[tuple[str, float]] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        for base in (
            f"{drive}Star Citizen/StarCitizen",
            f"{drive}StarCitizen",
            f"{drive}Program Files/Roberts Space Industries/StarCitizen",
            f"{drive}Games/StarCitizen",
            f"{drive}Games/Star Citizen/StarCitizen",
        ):
            for channel in _CHANNELS:
                folder = os.path.join(base, channel)
                lf = _log_file_in(folder)
                if lf:
                    try:
                        candidates.append((folder, os.path.getmtime(lf)))
                    except OSError:
                        pass

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0].replace("\\", "/")


def get_sc_folder() -> Optional[str]:
    """Return the saved SC channel folder, or auto-detect + persist."""
    s = load_settings()
    folder = s.get("sc_folder")
    if folder and os.path.isdir(folder):
        return folder
    # The launcher's shared install root (first-launch popup): use the channel
    # whose Game.log was written most recently.  Not persisted here, so a later
    # change in the launcher still reaches this tool.
    try:
        from shared.sc_install import get_sc_root, newest_game_log
        game_log = newest_game_log(get_sc_root())
        if game_log:
            return os.path.dirname(game_log).replace("\\", "/")
    except ImportError:
        pass
    detected = auto_detect_sc_folder()
    if detected:
        s["sc_folder"] = detected
        save_settings(s)
        return detected
    return None


def set_sc_folder(folder: str) -> None:
    s = load_settings()
    s["sc_folder"] = folder.replace("\\", "/")
    save_settings(s)


# ── Log scanning ───────────────────────────────────────────────────────────

def scan_blueprint_names(sc_folder: str) -> set[str]:
    """Scan Game.log + logbackups/*.log for 'Received Blueprint:' entries."""
    found: set[str] = set()
    paths: list[str] = []

    backups = os.path.join(sc_folder, "logbackups")
    if os.path.isdir(backups):
        try:
            for fname in os.listdir(backups):
                if fname.lower().endswith(".log"):
                    paths.append(os.path.join(backups, fname))
        except OSError as e:
            log.warning("Failed to list logbackups: %s", e)

    main_log = _log_file_in(sc_folder)
    if main_log:
        paths.append(main_log)

    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    m = _BP_PATTERN.search(line)
                    if m:
                        name = m.group(1).strip()
                        if name:
                            found.add(name)
        except OSError as e:
            log.warning("Failed to read %s: %s", path, e)

    log.info("Scanned %d log files, found %d unique blueprint names", len(paths), len(found))
    return found


# ── Matching names to crafting blueprints ──────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


_POLL_INTERVAL = 0.5    # seconds between readline polls
_REOPEN_DELAY = 2.0     # seconds before re-opening after error/rotation


class BlueprintLogWatcher:
    """Tails Star Citizen's Game.log and fires a callback for every new
    'Received Blueprint: <name>:' line. Background-thread safe; callback
    runs on the watcher thread — the caller is responsible for marshaling
    to the UI thread (e.g. via a Qt Signal)."""

    def __init__(self, sc_folder: str, on_name: Callable[[str], None]) -> None:
        self._sc_folder = sc_folder
        self._on_name = on_name
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pos: int = 0
        self._path: Optional[str] = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="BlueprintLogWatcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    def _resolve_path(self) -> Optional[str]:
        return _log_file_in(self._sc_folder)

    def _get_file_size(self) -> int:
        try:
            return os.path.getsize(self._path) if self._path else 0
        except OSError:
            return 0

    def _run(self) -> None:
        fh = None
        # Seek to end of existing file so only NEW Received Blueprint lines
        # are dispatched. The initial bulk scan handles historical entries.
        self._path = self._resolve_path()
        self._pos = self._get_file_size()

        while not self._stop_event.is_set():
            try:
                if fh is None:
                    if not self._path or not os.path.exists(self._path):
                        # Game log may appear later (game started after watcher)
                        time.sleep(_REOPEN_DELAY)
                        self._path = self._resolve_path()
                        if not self._path:
                            continue
                        self._pos = 0
                    fh = open(self._path, "r", encoding="utf-8", errors="replace")
                    fh.seek(self._pos)

                # Detect truncation/rotation (new SC session recreated Game.log)
                current_size = self._get_file_size()
                if current_size < self._pos:
                    log.info("BlueprintLogWatcher: log rotated, seeking to 0")
                    self._pos = 0
                    fh.seek(0)

                line = fh.readline()
                if line:
                    self._pos = fh.tell()
                    m = _BP_PATTERN.search(line)
                    if m:
                        name = m.group(1).strip()
                        if name:
                            try:
                                self._on_name(name)
                            except Exception:
                                log.exception("BlueprintLogWatcher callback failed")
                else:
                    if self._stop_event.wait(_POLL_INTERVAL):
                        break

            except OSError as exc:
                log.warning("BlueprintLogWatcher read error: %s — retrying", exc)
                if fh:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    fh = None
                if self._stop_event.wait(_REOPEN_DELAY):
                    break

        if fh:
            try:
                fh.close()
            except OSError:
                pass


def match_blueprints(names: Iterable[str], crafting_blueprints: list[dict],
                     data_mgr=None) -> list[dict]:
    """Return the subset of crafting_blueprints whose identity matches one
    of the provided names. Matches against tag, productName, productEntityClass,
    and the resolved display name (via data_mgr if available)."""
    if not crafting_blueprints:
        return []

    norm_names = {_norm(n): n for n in names if n}
    if not norm_names:
        return []

    matched: list[dict] = []
    seen_ids: set[int] = set()

    for bp in crafting_blueprints:
        candidates = [
            bp.get("tag") or "",
            bp.get("productName") or "",
            bp.get("productEntityClass") or "",
        ]
        if data_mgr is not None:
            try:
                candidates.append(data_mgr.get_blueprint_product_name(bp) or "")
                prod = data_mgr.get_blueprint_product(bp)
                if prod:
                    candidates.append(prod.get("name", "") or "")
                    candidates.append(prod.get("itemName", "") or "")
            except Exception:
                pass

        for cand in candidates:
            if cand and _norm(cand) in norm_names:
                if id(bp) not in seen_ids:
                    matched.append(bp)
                    seen_ids.add(id(bp))
                break

    return matched
