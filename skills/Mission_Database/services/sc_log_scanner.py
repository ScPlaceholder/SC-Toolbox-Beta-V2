"""Star Citizen game.log scanner.

Extracts blueprint names from the live Game.log file and all logbackups,
and matches them against the loaded crafting blueprint data so they can be
auto-marked as owned.

The game writes one line per blueprint it hands out:

    <2026-10-04T18:29:21.297Z> [Notice] <SHUDEvent_OnNotification> Added
    notification "Received Blueprint: GOLEM MC-4 Ore Pod : " [79] to queue. ...

HOW IT STAYS CHEAP.  A player's logbackups folder is large (1,077 files and
4.5 GB on the machine this was written on) and the player is in game while
this runs.  So :class:`IncrementalLogScanner` remembers what it has read:

* every backup file is read ONCE, then recognised by name + size + mtime;
* the live Game.log is tailed from the byte offset reached last time;
* when the game starts it moves Game.log into logbackups and begins a new
  one.  That is noticed (the first bytes of the file change, or it shrinks),
  the new file is read from 0, and the old one is picked up from logbackups
  as a file never seen before, so the end of the last session is not lost.

No file handle is kept open between polls.  The previous watcher held
Game.log open for as long as the tool ran, and a handle opened by Python on
Windows does not allow the file to be renamed, which is exactly what the game
does to it at launch.

Scans only ever ADD names.  A deleted or truncated log removes nothing.

Inspired by Battle Buddy's SC install auto-detection.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import string
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

log = logging.getLogger(__name__)

_NEW_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "mission_db")
_SETTINGS_PATH = os.path.join(_NEW_DIR, "settings.json")
_OLD_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".mission_db_cache")
_OLD_SETTINGS_PATH = os.path.join(_OLD_CACHE_DIR, "settings.json")

# What has been read so far.  Lives beside the inventory, OUTSIDE the install
# folder, because an update replaces the install folder wholesale.
_STATE_PATH = os.path.join(_NEW_DIR, "log_scan_state.json")
_STATE_VERSION = 1

# Matches lines like: "Received Blueprint: <name>: <extra>"
_BP_PATTERN = re.compile(r"Received Blueprint:\s*(.*?):")
# The same on raw bytes, never crossing a line end, so a whole chunk of log is
# searched in one pass instead of line by line.
_BP_BYTES = re.compile(rb"Received Blueprint:[ \t]*([^\r\n]*?):")

_CHUNK = 1 << 20            # bytes read at a time
_MAX_LINE = 8 << 20         # a "line" longer than this is skipped, not buffered
_SIG_LEN = 64               # leading bytes that identify one Game.log
# (its first line starts with the session's own millisecond timestamp)


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


# ── Reading one log file ───────────────────────────────────────────────────

def _collect(buf: bytes, into: set) -> None:
    for m in _BP_BYTES.finditer(buf):
        name = m.group(1).decode("utf-8", "ignore").strip()
        if name:
            into.add(name)


def read_blueprint_names(path: str, start: int = 0, whole_file: bool = True,
                         stop: Optional[threading.Event] = None
                         ) -> tuple[set, int, int, bool]:
    """Stream ``path`` from byte ``start`` and collect blueprint names.

    Returns ``(names, end_offset, bytes_read, finished)``.

    ``whole_file=False`` is for a log the game is still writing: a last line
    with no line end yet is left alone and ``end_offset`` stops in front of
    it, so the next call reads that line once it is complete.  ``finished`` is
    False only when ``stop`` was set part-way through.
    """
    names: set = set()
    pos = start
    read = 0
    carry = b""
    finished = True
    with open(path, "rb") as f:
        if start:
            f.seek(start)
        while True:
            if stop is not None and stop.is_set():
                finished = False
                break
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            read += len(chunk)
            buf = carry + chunk if carry else chunk
            cut = buf.rfind(b"\n") + 1
            if cut == 0:
                if len(buf) > _MAX_LINE:
                    pos += len(buf)
                    carry = b""
                else:
                    carry = buf
                continue
            _collect(buf[:cut], names)
            pos += cut
            carry = buf[cut:]
    if finished and whole_file and carry:
        _collect(carry, names)
        pos += len(carry)
    return names, pos, read, finished


def scan_blueprint_names(sc_folder: str) -> set[str]:
    """Scan Game.log + logbackups/*.log for 'Received Blueprint:' entries.

    Reads EVERYTHING, every call.  Kept for callers that want a one-off
    answer; the tool itself uses :class:`IncrementalLogScanner`.
    """
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
            found |= read_blueprint_names(path)[0]
        except OSError as e:
            log.warning("Failed to read %s: %s", path, e)

    log.info("Scanned %d log files, found %d unique blueprint names", len(paths), len(found))
    return found


# ── Incremental scanning ───────────────────────────────────────────────────

@dataclass
class PollResult:
    """What one :meth:`IncrementalLogScanner.poll` did."""
    folder: Optional[str] = None        # None == Star Citizen folder not found
    new_names: list = field(default_factory=list)
    names_total: int = 0
    files_read: int = 0
    backups_read: int = 0
    bytes_read: int = 0
    rotated: bool = False               # the game started a new Game.log
    listed_backups: bool = False
    backlog_done: bool = True           # False == stopped before finishing
    full: bool = False
    seconds: float = 0.0


def _head_sig(path: str, n: int = _SIG_LEN) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(n)).hexdigest()


class IncrementalLogScanner:
    """Finds blueprint names in the game logs without re-reading old ones.

    Qt-free.  :meth:`poll` is meant to be called from a worker thread on a
    modest interval; a second call while one is running returns ``None``.
    State is persisted to ``state_path`` so the next launch of the tool only
    reads what is new.
    """

    FOLDER_RETRY_S = 60.0     # how often to look again for a missing install
    RELIST_S = 300.0          # how often to re-list logbackups with no rotation
    SAVE_EVERY_S = 60.0       # how often a moved live offset alone is saved

    def __init__(self, state_path: Optional[str] = None,
                 folder_resolver: Optional[Callable[[], Optional[str]]] = None,
                 backlog_pause: float = 0.002) -> None:
        self._state_path = state_path or _STATE_PATH
        self._resolver = folder_resolver or get_sc_folder
        self._pause = backlog_pause
        self._lock = threading.RLock()
        self._poll_lock = threading.Lock()
        self._folder: Optional[str] = None
        self._folder_checked: Optional[float] = None   # monotonic, last look
        self._listed_at: Optional[float] = None        # monotonic, last listing
        self._saved_at = time.monotonic()
        self._dirty = False
        self._names: set = set()
        self._backups: dict = {}            # file name -> [size, mtime_ns]
        self._live: dict = {}               # {"offset", "sig"}
        self._state_folder = ""
        self._load()

    # ── persistence ──

    def _load(self) -> None:
        try:
            with open(self._state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            log.warning("log scan state unreadable; every log will be read again")
            return
        if not isinstance(data, dict) or data.get("version") != _STATE_VERSION:
            return
        try:
            self._state_folder = str(data.get("folder") or "")
            self._names = {str(n) for n in data.get("names") or []}
            self._backups = {str(k): [int(v[0]), int(v[1])]
                             for k, v in (data.get("backups") or {}).items()}
            live = data.get("live") or {}
            if live.get("sig"):
                self._live = {"offset": int(live.get("offset") or 0),
                              "sig": str(live["sig"])}
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            log.warning("log scan state malformed; every log will be read again")
            self._backups, self._live = {}, {}

    def flush(self) -> None:
        """Write the state to disk if it changed (atomic; never raises)."""
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": _STATE_VERSION,
                "folder": self._state_folder,
                "names": sorted(self._names),
                "live": dict(self._live),
                "backups": self._backups,
            }
            try:
                os.makedirs(os.path.dirname(self._state_path), exist_ok=True)
                tmp = self._state_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f)
                os.replace(tmp, self._state_path)
                self._dirty = False
                self._saved_at = time.monotonic()
            except OSError as e:
                log.warning("could not save log scan state: %s", e)

    # ── queries ──

    def names(self) -> set:
        """Every blueprint name found in the logs so far (a copy)."""
        with self._lock:
            return set(self._names)

    def forget_folder(self) -> None:
        """The player picked another folder: resolve it again on the next poll."""
        with self._lock:
            self._folder = None
            self._folder_checked = None

    # ── one poll ──

    def _resolve_folder(self, force: bool) -> Optional[str]:
        if self._folder and os.path.isdir(self._folder):
            return self._folder
        now = time.monotonic()
        if (not force and self._folder_checked is not None
                and now - self._folder_checked < self.FOLDER_RETRY_S):
            return None
        self._folder_checked = now
        try:
            self._folder = self._resolver()
        except Exception:
            log.exception("locating the Star Citizen folder failed")
            self._folder = None
        return self._folder

    def poll(self, on_names: Optional[Callable[[list], None]] = None,
             on_progress: Optional[Callable[[int, int], None]] = None,
             stop: Optional[threading.Event] = None,
             full: bool = False) -> Optional[PollResult]:
        """Read whatever is new.  ``full=True`` forgets what was read and
        reads every file again (the manual "Scan Game Log" fallback).

        ``on_names(list)`` is called as soon as names not seen before turn
        up, so a long first scan shows results while it is still running.
        """
        if not self._poll_lock.acquire(blocking=False):
            return None
        t0 = time.perf_counter()
        res = PollResult(full=full)
        try:
            with self._lock:
                forced = full or self._folder_checked is None
            folder = self._resolve_folder(forced)
            res.folder = folder
            if not folder:
                return res
            with self._lock:
                if folder != self._state_folder:
                    # Another install/channel: nothing read there yet.  Names
                    # already found stay; they are still the player's.
                    self._backups, self._live = {}, {}
                    self._state_folder = folder
                    self._dirty = True
                if full:
                    self._backups, self._live = {}, {}
                    self._dirty = True

            self._poll_live(folder, res, on_names, stop)

            now = time.monotonic()
            if (full or res.rotated or self._listed_at is None
                    or now - self._listed_at >= self.RELIST_S):
                self._listed_at = now
                res.listed_backups = True
                self._poll_backups(folder, res, on_names, on_progress, stop)

            with self._lock:
                res.names_total = len(self._names)
            if (res.new_names or res.backups_read or res.rotated or full
                    or time.monotonic() - self._saved_at >= self.SAVE_EVERY_S):
                self.flush()
            return res
        finally:
            res.seconds = time.perf_counter() - t0
            self._poll_lock.release()

    def _take(self, found: set, res: PollResult,
              on_names: Optional[Callable[[list], None]]) -> None:
        with self._lock:
            new = sorted(found - self._names)
            if not new:
                return
            self._names.update(new)
            self._dirty = True
        res.new_names.extend(new)
        if on_names is not None:
            try:
                on_names(new)
            except Exception:
                log.exception("blueprint name callback failed")

    def _poll_live(self, folder: str, res: PollResult, on_names, stop) -> None:
        path = _log_file_in(folder)
        if not path:
            return
        try:
            size = os.path.getsize(path)
            with self._lock:
                live = dict(self._live)
            offset = int(live.get("offset") or 0)
            if live and size == offset:
                return                      # nothing written since last poll
            if size < _SIG_LEN:
                # Too new to identify (and too short to hold a blueprint
                # line).  If another file was being followed, it was rotated.
                if live:
                    res.rotated = True
                    with self._lock:
                        self._live = {}
                        self._dirty = True
                return
            sig = _head_sig(path)
            if live and (sig != live.get("sig") or size < offset):
                res.rotated = True
                offset = 0
                log.info("Game.log was replaced by the game; reading the new one from the start")
            elif not live:
                offset = 0
            names, end, read, _fin = read_blueprint_names(
                path, start=offset, whole_file=False, stop=stop)
            res.files_read += 1
            res.bytes_read += read
            with self._lock:
                if self._live != {"offset": end, "sig": sig}:
                    self._live = {"offset": end, "sig": sig}
                    self._dirty = True
            self._take(names, res, on_names)
        except OSError as e:
            log.warning("could not read %s: %s", path, e)

    def _poll_backups(self, folder: str, res: PollResult, on_names,
                      on_progress, stop) -> None:
        backups = os.path.join(folder, "logbackups")
        entries = []
        try:
            with os.scandir(backups) as it:
                for e in it:
                    if not e.name.lower().endswith(".log"):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    entries.append((e.name, int(st.st_size), int(st.st_mtime_ns)))
        except FileNotFoundError:
            return
        except OSError as e:
            log.warning("could not list %s: %s", backups, e)
            return

        with self._lock:
            present = {n for n, _s, _m in entries}
            for n in [n for n in self._backups if n not in present]:
                # The game (or the player) deleted it.  What it contained
                # stays known: scans only add.
                del self._backups[n]
                self._dirty = True
            pending = [e for e in entries if self._backups.get(e[0]) != [e[1], e[2]]]
        pending.sort(key=lambda e: e[2], reverse=True)      # newest first
        total = len(pending)
        for i, (name, size, mtime) in enumerate(pending, 1):
            if stop is not None and stop.is_set():
                res.backlog_done = False
                break
            try:
                names, _end, read, finished = read_blueprint_names(
                    os.path.join(backups, name), stop=stop)
            except OSError as e:
                log.warning("could not read %s: %s", name, e)
                continue                    # not marked read: tried again later
            if not finished:
                res.backlog_done = False
                break
            res.files_read += 1
            res.backups_read += 1
            res.bytes_read += read
            with self._lock:
                self._backups[name] = [size, mtime]
                self._dirty = True
            self._take(names, res, on_names)
            if on_progress is not None and (i == total or i % 10 == 0):
                try:
                    on_progress(i, total)
                except Exception:
                    log.exception("scan progress callback failed")
            if time.monotonic() - self._saved_at >= 5.0:
                self.flush()                # a closed tool resumes, not restarts
            if self._pause and i < total:
                # Leave the disk some air: the player is in game.
                if stop is not None:
                    stop.wait(self._pause)
                else:
                    time.sleep(self._pause)


# ── Matching names to crafting blueprints ──────────────────────────────────

def _norm(s: str) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "").lower())


def match_names(names: Iterable[str], crafting_blueprints: list[dict],
                data_mgr=None) -> tuple[list[dict], list[str]]:
    """Return ``(matched blueprints, names that matched nothing)``.

    A name matches a blueprint on tag, productName, productEntityClass, or
    the resolved display name (via data_mgr if available), ignoring case,
    spaces, hyphens and underscores.
    """
    wanted: dict = {}
    for n in names:
        if n:
            wanted.setdefault(_norm(n), n)
    if not crafting_blueprints or not wanted:
        return [], sorted(wanted.values())

    matched: list[dict] = []
    hit: set = set()
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

        took = False
        for cand in candidates:
            key = _norm(cand) if cand else ""
            if key and key in wanted:
                hit.add(key)
                if not took:
                    matched.append(bp)
                    took = True

    unmatched = sorted(v for k, v in wanted.items() if k not in hit)
    return matched, unmatched


def match_blueprints(names: Iterable[str], crafting_blueprints: list[dict],
                     data_mgr=None) -> list[dict]:
    """Return the subset of crafting_blueprints whose identity matches one
    of the provided names. Matches against tag, productName, productEntityClass,
    and the resolved display name (via data_mgr if available)."""
    return match_names(names, crafting_blueprints, data_mgr)[0]


@dataclass
class ApplyResult:
    matched: int = 0                                # blueprints the names resolve to
    added: list = field(default_factory=list)       # ids newly marked owned
    held_back: list = field(default_factory=list)   # ids the player removed by hand
    unmatched: list = field(default_factory=list)   # names not in the blueprint list


def apply_log_names(names: Iterable[str], crafting_blueprints: list[dict],
                    inventory, data_mgr=None,
                    restore_removed: bool = False) -> ApplyResult:
    """Mark the blueprints named in the logs as owned.  Only ever adds.

    A blueprint the player removed by hand is NOT put back unless
    ``restore_removed`` is set (they asked for it).
    """
    from services.inventory import blueprint_key

    matched, unmatched = match_names(names, crafting_blueprints, data_mgr)
    res = ApplyResult(matched=len(matched), unmatched=unmatched)
    pairs = []
    for bp in matched:
        bp_id = blueprint_key(bp)
        if bp_id:
            pairs.append((bp_id, bp))
    if pairs:
        out = inventory.add_from_scan(pairs, restore_removed=restore_removed)
        if out:
            res.added, res.held_back = list(out[0]), list(out[1])
    return res
