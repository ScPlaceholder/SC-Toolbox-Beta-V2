"""Star Citizen Game.log scanner — turns log files into play *sessions*.

Star Citizen writes a fresh ``Game.log`` every time the game launches and
rotates the previous one into ``logbackups/`` with a timestamped name.  So
each log file corresponds to exactly one play session: the first line's
timestamp is when the client started, the last line's timestamp is when it
shut down.  Session length = last - first.

Every line is prefixed with an ISO-8601 UTC stamp::

    <2026-06-13T01:33:04.519Z> Log started on Sat Jun 13 01:33:04 2026

We read only the first and last timestamps of each file (seeking to the tail
rather than reading megabytes), which makes scanning a thousand backups fast.
Results are cached per file by (size, mtime) so subsequent scans only re-read
the live ``Game.log`` and any new backups.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Optional

from . import settings as _settings

log = logging.getLogger(__name__)

# Star Citizen release channels (each is a sibling folder under the install root).
CHANNELS = ("LIVE", "PTU", "EPTU", "HOTFIX", "TECH-PREVIEW", "TECH-PREVIEW-2")
_CHANNEL_SET = {c.upper() for c in CHANNELS}
_LOG_NAMES = ("Game.log", "game.log")

_TS_RE = re.compile(r"<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z>")
_BUILD_RE = re.compile(r"Build\((\d+)\)")

# How much of the tail to read when hunting for the last timestamp.  Log lines
# are short and frequent, so 128 KiB always contains many stamped lines.
_TAIL_BYTES = 128 * 1024
_HEAD_LINES = 200  # scan this many leading lines for the first timestamp


@dataclass
class Session:
    """One play session derived from a single Game.log file."""
    path: str
    channel: str
    build: str
    start: datetime          # timezone-aware UTC
    end: datetime            # timezone-aware UTC
    duration_seconds: float

    @property
    def start_local(self) -> datetime:
        return self.start.astimezone()

    @property
    def end_local(self) -> datetime:
        return self.end.astimezone()


# ── SC folder discovery ──────────────────────────────────────────────────────

def _log_file_in(folder: str) -> Optional[str]:
    for name in _LOG_NAMES:
        p = os.path.join(folder, name)
        if os.path.isfile(p):
            return p
    return None


def _has_logs(folder: str) -> bool:
    if _log_file_in(folder):
        return True
    backups = os.path.join(folder, "logbackups")
    if os.path.isdir(backups):
        try:
            return any(f.lower().endswith(".log") for f in os.listdir(backups))
        except OSError:
            return False
    return False


def auto_detect_sc_folder() -> Optional[str]:
    """Scan drives for a Star Citizen install.  Returns the *install root*
    (the folder containing LIVE/PTU/...) when found, so a scan picks up every
    channel; falls back to a lone channel folder otherwise."""
    import string

    bases: list[str] = []
    for letter in string.ascii_uppercase:
        drive = f"{letter}:\\"
        if not os.path.isdir(drive):
            continue
        for base in (
            f"{drive}Star Citizen\\StarCitizen",
            f"{drive}StarCitizen",
            f"{drive}Program Files\\Roberts Space Industries\\StarCitizen",
            f"{drive}Roberts Space Industries\\StarCitizen",
            f"{drive}Games\\StarCitizen",
            f"{drive}Games\\Star Citizen\\StarCitizen",
        ):
            if os.path.isdir(base):
                bases.append(base)

    best_root: Optional[str] = None
    best_mtime = -1.0
    for base in bases:
        for channel in CHANNELS:
            lf = _log_file_in(os.path.join(base, channel))
            if lf:
                try:
                    mt = os.path.getmtime(lf)
                except OSError:
                    continue
                if mt > best_mtime:
                    best_mtime = mt
                    best_root = base  # the install root, not the channel
    if best_root:
        return best_root.replace("\\", "/")

    # No channel layout found — accept a directly-linked channel folder.
    for base in bases:
        if _has_logs(base):
            return base.replace("\\", "/")
    return None


def _shared_sc_root() -> Optional[str]:
    """The folder linked once in the launcher's first-launch popup, if any."""
    try:
        from shared.sc_install import get_sc_root
    except ImportError:
        return None
    return get_sc_root()


def get_or_detect_folder() -> Optional[str]:
    """Saved folder if still valid, else the launcher's shared one, else auto-detect.

    The shared folder is not copied into this tool's own setting, so changing
    it in the launcher later reaches PlayTime too.
    """
    saved = _settings.get_sc_folder()
    if saved and os.path.isdir(saved):
        return saved
    shared = _shared_sc_root()
    if shared:
        return shared
    detected = auto_detect_sc_folder()
    if detected:
        _settings.set_sc_folder(detected)
    return detected


# ── Log file enumeration ─────────────────────────────────────────────────────

def _channel_of(path: str) -> str:
    """Best-effort channel name from a log file path."""
    parts = re.split(r"[\\/]+", path)
    for seg in parts:
        if seg.upper() in _CHANNEL_SET:
            return seg.upper()
    return "Other"


def _collect_from(folder: str, out: set[str]) -> None:
    lf = _log_file_in(folder)
    if lf:
        out.add(os.path.normpath(lf))
    backups = os.path.join(folder, "logbackups")
    if os.path.isdir(backups):
        try:
            for fname in os.listdir(backups):
                if fname.lower().endswith(".log"):
                    out.add(os.path.normpath(os.path.join(backups, fname)))
        except OSError as exc:
            log.warning("playtime: cannot list %s: %s", backups, exc)


def find_log_files(folder: str, recurse: bool = True) -> list[str]:
    """Return every Game.log + logbackups/*.log under *folder*.

    Handles both linking the install root (which contains LIVE/PTU/... as
    subfolders) and linking a single channel folder directly.
    """
    found: set[str] = set()
    if not folder or not os.path.isdir(folder):
        return []

    _collect_from(folder, found)
    if recurse:
        try:
            for entry in os.listdir(folder):
                sub = os.path.join(folder, entry)
                if os.path.isdir(sub) and entry != "logbackups":
                    _collect_from(sub, found)
        except OSError as exc:
            log.warning("playtime: cannot list %s: %s", folder, exc)

    return sorted(found)


# ── Single-file parsing ──────────────────────────────────────────────────────

def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_log_file(path: str) -> Optional[tuple[str, str, str]]:
    """Return (first_ts_iso, last_ts_iso, build) for *path*, or None if the
    file has no recognisable timestamps."""
    first_ts: Optional[str] = None
    build = ""
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for _ in range(_HEAD_LINES):
                line = f.readline()
                if not line:
                    break
                if not build:
                    bm = _BUILD_RE.search(line)
                    if bm:
                        build = bm.group(1)
                m = _TS_RE.search(line)
                if m:
                    first_ts = m.group(1)
                    break
    except OSError as exc:
        log.warning("playtime: cannot read head of %s: %s", path, exc)
        return None

    if first_ts is None:
        return None

    # Last timestamp: read the tail and take the final match.
    last_ts = first_ts
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fb:
            if size > _TAIL_BYTES:
                fb.seek(size - _TAIL_BYTES)
            chunk = fb.read().decode("utf-8", errors="ignore")
        matches = _TS_RE.findall(chunk)
        if matches:
            last_ts = matches[-1]
    except OSError as exc:
        log.warning("playtime: cannot read tail of %s: %s", path, exc)

    if not build:
        build = _BUILD_RE.search(os.path.basename(path))
        build = build.group(1) if build else ""

    return first_ts, last_ts, build


def _session_from_record(path: str, rec: dict) -> Optional[Session]:
    start = _parse_dt(rec.get("start", ""))
    end = _parse_dt(rec.get("end", ""))
    if start is None or end is None:
        return None
    dur = (end - start).total_seconds()
    if dur < 0:
        # Clock went backwards / corrupt tail — treat as an instantaneous start.
        end = start
        dur = 0.0
    return Session(
        path=path,
        channel=rec.get("channel") or _channel_of(path),
        build=rec.get("build", ""),
        start=start,
        end=end,
        duration_seconds=dur,
    )


# ── Full scan with incremental cache ─────────────────────────────────────────

def scan(
    folder: str,
    recurse: bool = True,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> list[Session]:
    """Scan every log under *folder* and return a list of Sessions.

    Uses the on-disk cache keyed by (size, mtime) so unchanged backups are not
    re-read.  ``progress_cb(done, total)`` is called as files are processed;
    ``cancel_cb()`` returning True aborts early (partial results returned).
    """
    paths = find_log_files(folder, recurse=recurse)
    total = len(paths)
    cache = _settings.load_cache()
    new_cache: dict[str, dict] = {}
    sessions: list[Session] = []

    for i, path in enumerate(paths):
        if cancel_cb and cancel_cb():
            break
        try:
            st = os.stat(path)
            size, mtime = st.st_size, st.st_mtime
        except OSError:
            continue

        rec = cache.get(path)
        # The live Game.log is rewritten in place, so never trust the cache for
        # a file whose size/mtime changed.
        if not (rec and rec.get("size") == size and rec.get("mtime") == mtime):
            parsed = parse_log_file(path)
            if parsed is None:
                # Remember the miss so we don't reparse an unstamped file forever.
                new_cache[path] = {"size": size, "mtime": mtime, "start": "",
                                   "end": "", "build": "", "channel": _channel_of(path)}
                if progress_cb:
                    progress_cb(i + 1, total)
                continue
            first_ts, last_ts, build = parsed
            rec = {
                "size": size, "mtime": mtime,
                "start": first_ts, "end": last_ts,
                "build": build, "channel": _channel_of(path),
            }
        new_cache[path] = rec

        if rec.get("start"):
            sess = _session_from_record(path, rec)
            if sess is not None:
                sessions.append(sess)

        if progress_cb:
            progress_cb(i + 1, total)

    _settings.save_cache(new_cache)
    sessions.sort(key=lambda s: s.start)
    return sessions


def apply_cap(sessions: Iterable[Session], cap_hours: float) -> list[Session]:
    """Return copies of *sessions* with durations clamped to *cap_hours*.

    Star Citizen left running overnight produces multi-day "sessions" that
    aren't really play time.  A non-zero cap trims each session's end so AFK
    time is excluded from totals and per-day/hour distributions alike.
    """
    if not cap_hours or cap_hours <= 0:
        return list(sessions)
    cap_s = cap_hours * 3600.0
    out: list[Session] = []
    for s in sessions:
        if s.duration_seconds > cap_s:
            from datetime import timedelta
            out.append(Session(
                path=s.path, channel=s.channel, build=s.build,
                start=s.start, end=s.start + timedelta(seconds=cap_s),
                duration_seconds=cap_s,
            ))
        else:
            out.append(s)
    return out
