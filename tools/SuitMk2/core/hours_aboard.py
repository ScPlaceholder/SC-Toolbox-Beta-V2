"""hours_aboard.py - how long this pilot has had the companions with them, in hours of use.

One number, kept in one small file beside the pilot's memory, that survives restarts. It was added for the rare
line (rare_line.py), which is said about once in a hundred hours of use and so needs to know what an hour of use
is. The plan that the characters may develop along authored stages keyed to hours aboard will
want the same number; nothing of that is built here, and nothing here stands in its way: any code may read
`hours`.

WHAT COUNTS AS AN HOUR OF USE. Time in which an unprompted line could have been heard: the Suit running, its
window open and not muted, and the pilot at the controls (not AFK). CompanionCore adds it on every ambient tick.
A fight counts (they are with the pilot in it); the window hidden does not (the toolbox preloads the Suit hidden
at startup and it can sit there all day), nor does a muted Suit, nor an empty chair.

WHY NOT THE COUNTER THAT ALREADY EXISTED. relationship.json has `minutes_together`, added per session by the
dream queue's `counters` job. It was read on a real machine and could not be used for this:
  - it had not moved for eight days: 16 of 38 sessions' counters jobs were still waiting in the queue;
  - it counts a session from its first event to its last, hidden window and empty chair included;
  - it is added only at the next launch, so it cannot say how far into this session the pilot is.
That counter is left exactly as it is.

The file: {"seconds": <float>, "updated": <unix time>}. Unreadable or missing reads as zero and says so in the
log; a write that fails is logged and tried again at the next save.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("suitmk2.hours")
NAME = "hours_aboard.json"
SAVE_EVERY_S = 300.0          # written at most this often while it grows, and once more when the Suit stops


class HoursAboard:
    """path None: counts in memory only and nothing is written (tests, a Suit with no memory)."""

    def __init__(self, path: Optional[Path] = None, now: Callable[[], float] = time.time):
        self.path = Path(path) if path else None
        self.now = now
        self._lock = threading.Lock()
        self.seconds = 0.0
        self._saved_seconds = 0.0
        self._saved_t = now()
        if self.path is not None and self.path.exists():
            try:
                self.seconds = max(0.0, float(json.loads(self.path.read_text(encoding="utf-8")).get("seconds") or 0.0))
            except (OSError, ValueError, TypeError, AttributeError) as e:
                log.warning("hours aboard: %s could not be read (%s: %s); counting from zero", self.path,
                            type(e).__name__, e)
            self._saved_seconds = self.seconds

    @property
    def hours(self) -> float:
        return self.seconds / 3600.0

    def add(self, seconds: float) -> None:
        """Count this much more use. Nothing for a zero, a negative or a nonsense amount."""
        try:
            seconds = float(seconds)
        except (TypeError, ValueError):
            return
        if not 0.0 < seconds < 86400.0:
            return
        with self._lock:
            self.seconds += seconds
            due = self.now() - self._saved_t >= SAVE_EVERY_S
        if due:
            self.save()

    def save(self) -> bool:
        """Write the number now, whole (a temporary file, then a rename). True when it is on disk."""
        if self.path is None:
            return False
        with self._lock:
            seconds, self._saved_t = self.seconds, self.now()
            if seconds == self._saved_seconds:
                return True
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp.write_text(json.dumps({"seconds": round(seconds, 1), "updated": round(self.now(), 1)}), encoding="utf-8")
                os.replace(tmp, self.path)
                self._saved_seconds = seconds
                return True
            except OSError as e:
                log.warning("hours aboard: could not write %s (%s: %s)", self.path, type(e).__name__, e)
                return False
