"""
Mining Signals — Live Refinery Log Monitor + Backfill Scanner

Live-tails Game.log for refinery completion notifications (like Battle Buddy).
Also scans LogBackups on startup for historical data.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.25  # seconds between read attempts
_REOPEN_DELAY = 2.0    # seconds to wait before reopening after rotation

# Matches RequestLocationInventory lines: "Location[RR_HUR_L2]"
_LOCATION_RE = re.compile(r"RequestLocationInventory.*Location\[([^\]]+)\]")

# Matches the SHUDEvent_OnNotification line for refinery completions.
# Group 1: timestamp, Group 2: optional count prefix ("2"), Group 3: location
_REFINERY_RE = re.compile(
    r"<(\d{4}-\d{2}-\d{2}T[\d:.]+)Z>"
    r".*SHUDEvent_OnNotification.*"
    r'"(?:(?:A|(\d+)) )?Refinery Work Orders? (?:has|have) been Completed at ([^:]+):'
)


def _make_id(timestamp: str, location: str) -> str:
    """Deterministic ID from timestamp + location for deduplication."""
    raw = f"{timestamp}|{location}"
    return hashlib.md5(raw.encode()).hexdigest()[:12]


def _parse_refinery_line(line: str) -> dict | None:
    """Parse a single log line. Returns dict or None."""
    m = _REFINERY_RE.search(line)
    if not m:
        return None
    ts = m.group(1)
    count = int(m.group(2)) if m.group(2) else 1
    location = m.group(3).strip()
    return {
        "id": _make_id(ts, location),
        "timestamp": ts,
        "location": location,
        "count": count,
    }


# ---------------------------------------------------------------------------
# Player location tracking from log
# ---------------------------------------------------------------------------

# Subset of location codes that have refineries
_LOCATION_NAMES: dict[str, str] = {
    "RR_HUR_L1": "HUR-L1", "RR_HUR_L2": "HUR-L2",
    "RR_HUR_L3": "HUR-L3", "RR_HUR_L4": "HUR-L4", "RR_HUR_L5": "HUR-L5",
    "RR_CRU_L1": "CRU-L1", "RR_CRU_L2": "CRU-L2",
    "RR_CRU_L3": "CRU-L3", "RR_CRU_L4": "CRU-L4", "RR_CRU_L5": "CRU-L5",
    "RR_ARC_L1": "ARC-L1", "RR_ARC_L2": "ARC-L2",
    "RR_ARC_L3": "ARC-L3", "RR_ARC_L4": "ARC-L4", "RR_ARC_L5": "ARC-L5",
    "RR_MIC_L1": "MIC-L1", "RR_MIC_L2": "MIC-L2",
    "RR_MIC_L3": "MIC-L3", "RR_MIC_L4": "MIC-L4", "RR_MIC_L5": "MIC-L5",
    "RR_HUR_LEO": "Everus Harbor", "RR_CRU_LEO": "Seraphim Station",
    "RR_ARC_LEO": "Baijini Point", "RR_MIC_LEO": "Port Tressler",
    "Stanton1_Lorville": "Lorville", "Stanton1_NewBab": "New Babbage",
    "Stanton1_Orison": "Orison", "Stanton1_AreaEighteen": "Area 18",
    "Levski": "Levski",
    "Pyro_Ruin_Station": "Ruin Station",
    "RestStop_Pyro_Rundown_RR_P3_LEO_Orb": "Orbituary (Pyro III)",
    "RestStop_Pyro_Checkmate": "Checkmate Station",
}


def _resolve_location(code: str) -> str:
    """Convert a game location code to a human-readable name."""
    if code in _LOCATION_NAMES:
        return _LOCATION_NAMES[code]
    # Try partial match (location codes can have suffixes)
    for key, name in _LOCATION_NAMES.items():
        if code.startswith(key) or key in code:
            return name
    return code  # Return raw code if unknown


def _update_location(monitor: "RefineryMonitor", line: str) -> None:
    """Check a log line for location changes and update the monitor."""
    m = _LOCATION_RE.search(line)
    if m:
        raw_code = m.group(1)
        name = _resolve_location(raw_code)
        if name != monitor._current_location:
            monitor._current_location = name
            log.debug("Player location: %s (%s)", name, raw_code)


# ---------------------------------------------------------------------------
# Live monitor — tails Game.log in real-time
# ---------------------------------------------------------------------------

class RefineryMonitor:
    """Tails Game.log and fires callbacks for refinery completion events.

    Also does a one-time backfill of Game.log + LogBackups on start.
    """

    def __init__(self, game_dir: str) -> None:
        self._game_dir = game_dir
        self._log_path = os.path.join(game_dir, "Game.log")
        self._callbacks: list[Callable[[list[dict]], None]] = []
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._pos: int = 0
        self._seen_ids: set[str] = set()
        self._current_location: str = ""  # human-readable player location

    def subscribe(self, callback: Callable[[list[dict]], None]) -> None:
        """Register a callback. Called with the full deduped results list."""
        self._callbacks.append(callback)

    def start(self) -> None:
        """Start backfill + live monitoring in a background thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="RefineryMonitor",
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop monitoring."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3.0)
            self._thread = None

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def current_location(self) -> str:
        """Human-readable player location from the most recent log."""
        return self._current_location

    def _run(self) -> None:
        # Phase 1: backfill from LogBackups + full Game.log
        results = self._backfill()
        self._seen_ids = {e["id"] for e in results}
        if results:
            self._dispatch(results)

        # Phase 2: live-tail Game.log from current end
        self._pos = self._get_file_size()
        fh = None

        while not self._stop_event.is_set():
            try:
                if fh is None:
                    if not os.path.exists(self._log_path):
                        time.sleep(_REOPEN_DELAY)
                        continue
                    fh = open(
                        self._log_path, "r",
                        encoding="utf-8", errors="replace",
                    )
                    fh.seek(self._pos)

                # Detect log rotation (new game session)
                current_size = self._get_file_size()
                if current_size < self._pos:
                    log.info("RefineryMonitor: log rotated, seeking to 0")
                    self._pos = 0
                    fh.seek(0)

                line = fh.readline()
                if line:
                    self._pos = fh.tell()
                    stripped = line.rstrip("\n")
                    # Track player location changes
                    _update_location(self, stripped)
                    entry = _parse_refinery_line(stripped)
                    if entry and entry["id"] not in self._seen_ids:
                        self._seen_ids.add(entry["id"])
                        # Re-dispatch full sorted list with new entry
                        results.append(entry)
                        results.sort(
                            key=lambda e: e["timestamp"], reverse=True,
                        )
                        self._dispatch(list(results))
                else:
                    time.sleep(_POLL_INTERVAL)

            except OSError as exc:
                log.warning("RefineryMonitor read error: %s — retrying", exc)
                if fh:
                    try:
                        fh.close()
                    except OSError:
                        pass
                    fh = None
                time.sleep(_REOPEN_DELAY)

        if fh:
            try:
                fh.close()
            except OSError:
                pass

    def _backfill(self) -> list[dict]:
        """Scan the 10 most recent LogBackups + Game.log for completions."""
        all_entries: dict[str, dict] = {}
        game_dir = Path(self._game_dir)

        # LogBackups — only the 10 most recent by modification time
        backups_dir = game_dir / "LogBackups"
        if backups_dir.is_dir():
            backup_files = sorted(
                backups_dir.glob("*.log"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for log_file in backup_files[:10]:
                if self._stop_event.is_set():
                    break
                for entry in _scan_file(log_file):
                    all_entries[entry["id"]] = entry

        # Live Game.log (full scan for backfill + find last location)
        live_log = game_dir / "Game.log"
        if live_log.is_file():
            try:
                with open(live_log, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        entry = _parse_refinery_line(line)
                        if entry:
                            all_entries[entry["id"]] = entry
                        # Track last location
                        m = _LOCATION_RE.search(line)
                        if m:
                            self._current_location = _resolve_location(m.group(1))
            except OSError as exc:
                log.warning("Could not read Game.log: %s", exc)

        return sorted(
            all_entries.values(), key=lambda e: e["timestamp"], reverse=True,
        )

    def _get_file_size(self) -> int:
        try:
            return os.path.getsize(self._log_path)
        except OSError:
            return 0

    def _dispatch(self, results: list[dict]) -> None:
        for cb in self._callbacks:
            try:
                cb(results)
            except Exception:
                log.exception("RefineryMonitor callback error")


def _scan_file(path: Path) -> list[dict]:
    """Scan a single log file for refinery completion events."""
    results: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                entry = _parse_refinery_line(line)
                if entry:
                    results.append(entry)
    except OSError as exc:
        log.warning("Could not read log file %s: %s", path, exc)
    return results
