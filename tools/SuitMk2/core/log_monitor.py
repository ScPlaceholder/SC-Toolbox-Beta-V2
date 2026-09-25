"""
SuitMk2 - Log Monitor

Tails the Star Citizen Game.log file continuously, reading only new
appended lines. Emits raw line events to subscribers.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


class LogMonitor:
    """
    Monitors Star Citizen Game.log using a tail-f approach.

    Only processes new lines appended after monitoring starts.
    Handles file truncation/rotation gracefully.
    """

    def __init__(self, log_path: Path, poll_interval: float = 0.1) -> None:
        self._log_path = log_path
        self._poll_interval = poll_interval
        self._stop_event = threading.Event()
        self._running = False
        self._thread: threading.Thread | None = None
        self._file_position = 0
        self._subscribers: list[Callable[[str], None]] = []
        self._sub_lock = threading.Lock()

    def start(self) -> None:
        """Start monitoring from the current end of file."""
        if self._running:
            return

        if not self._log_path.exists():
            raise FileNotFoundError(f"Log file not found: {self._log_path}")

        self._running = True
        self._stop_event.clear()
        self._file_position = self._log_path.stat().st_size
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()
        logger.info("LogMonitor started: %s", self._log_path)

    def stop(self) -> None:
        """Stop monitoring."""
        self._running = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        logger.info("LogMonitor stopped")

    def is_running(self) -> bool:
        return self._running

    def subscribe(self, callback: Callable[[str], None]) -> None:
        """Subscribe to raw log lines."""
        with self._sub_lock:
            self._subscribers = [*self._subscribers, callback]

    def _monitor_loop(self) -> None:
        """Main polling loop."""
        while self._running:
            try:
                self._read_new_lines()
            except Exception:
                logger.exception("Error reading log file")
            self._stop_event.wait(self._poll_interval)

    def _read_new_lines(self) -> None:
        """Read any new content since last position."""
        if not self._log_path.exists():
            return

        current_size = self._log_path.stat().st_size

        # File truncated/rotated
        if current_size < self._file_position:
            self._file_position = 0

        if current_size == self._file_position:
            return

        try:
            with open(self._log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._file_position)
                new_content = f.read()
                self._file_position = f.tell()
        except Exception:
            logger.exception("Failed to read log file")
            return

        for line in new_content.splitlines():
            stripped = line.strip()
            if stripped:
                self._emit_line(stripped)

    def _emit_line(self, line: str) -> None:
        """Send a line to all subscribers."""
        for callback in self._subscribers:  # snapshot via copy-on-write in subscribe()
            try:
                callback(line)
            except Exception:
                logger.exception("Error in line subscriber callback")

    def scan_backlog(self, minutes: float = 10.0) -> list[str]:
        """Scan recent log lines from the last N minutes.

        Reads backwards from the end of the file to find lines with
        timestamps within the window.  Returns them in chronological
        order so the event pipeline can replay them to seed state.

        Does NOT move the file position — ``start()`` still begins
        from the current end-of-file.

        Args:
            minutes: How far back to scan (default 10 minutes).

        Returns:
            List of raw log lines within the time window (oldest first).
        """
        if not self._log_path.exists():
            return []

        import re
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        ts_re = re.compile(r"<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z?>")

        # Read the tail of the file (last 500KB should cover 10 min easily)
        try:
            file_size = self._log_path.stat().st_size
            read_size = min(file_size, 512_000)
            with open(self._log_path, "r", encoding="utf-8", errors="ignore") as f:
                f.seek(max(0, file_size - read_size))
                if file_size > read_size:
                    f.readline()  # skip partial first line
                tail_lines = f.readlines()
        except Exception:
            logger.exception("Failed to read log backlog")
            return []

        # Filter to lines within the time window
        result: list[str] = []
        for line in tail_lines:
            match = ts_re.search(line)
            if not match:
                continue
            try:
                ts_str = match.group(1)
                if "." in ts_str:
                    base, frac = ts_str.split(".")
                    frac = frac[:6].ljust(6, "0")
                    ts_str = f"{base}.{frac}"
                line_time = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                if line_time >= cutoff:
                    stripped = line.strip()
                    if stripped:
                        result.append(stripped)
            except (ValueError, IndexError):
                continue

        logger.info(
            "LogMonitor: backlog scan found %d lines in last %.0f minutes",
            len(result), minutes,
        )
        return result

    def scan_for_session(self) -> str | None:
        """Scan existing log for the last session_start timestamp.

        Returns ISO timestamp string or None.
        """
        if not self._log_path.exists():
            return None

        import re

        ts_re = re.compile(r"<(\d{4}-\d{2}-\d{2}T[\d:.]+)Z?>")
        last_ts = None

        try:
            with open(self._log_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if "AccountLoginCharacterStatus_Character" in line:
                        match = ts_re.search(line)
                        if match:
                            last_ts = match.group(1)
        except Exception:
            logger.exception("Failed to scan log for session")

        return last_ts
