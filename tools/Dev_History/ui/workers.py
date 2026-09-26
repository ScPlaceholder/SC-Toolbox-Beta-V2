"""Background workers — nothing that can touch the network runs on the UI thread."""
from __future__ import annotations

import gc
import logging
from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, QTimer, Signal

from core.engine import describe_error

log = logging.getLogger(__name__)


class Task(QThread):
    """Run ``fn(task)`` off the UI thread.

    ``fn`` receives the task so it can emit ``progress``.  Exactly one of
    ``succeeded(result)`` / ``failed(message)`` is emitted.
    """

    succeeded = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable[["Task"], Any], ref: str = "", parent=None) -> None:
        super().__init__(parent)
        self._fn = fn
        self._ref = ref

    def run(self) -> None:
        try:
            result = self._fn(self)
        except Exception as exc:  # network, disk, bad gzip — all reported, none fatal
            log.warning("dev_history: background task failed: %r", exc)
            self.failed.emit(describe_error(exc, self._ref))
            return
        self.succeeded.emit(result)


class MainThreadGC(QObject):
    """Run Python's cyclic garbage collector on the GUI thread only.

    Measured, not theoretical: with automatic GC on, parsing the ~20 MB index
    JSON in a ``Task`` triggered a collection ON THE WORKER THREAD while the GUI
    thread was inside ``SCWindow.paintEvent`` (GIL released in a QPainter call),
    and the process died with an access violation — 2 crashes in 3 runs of the
    offscreen smoke test.  Disabling automatic collection and collecting from a
    GUI-thread timer removes that interleaving entirely.  Reference counting
    still frees almost everything immediately; only cycles wait for the timer.
    """

    def __init__(self, parent=None, interval_ms: int = 1000) -> None:
        super().__init__(parent)
        self._ticks = 0
        gc.disable()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._collect)
        self._timer.start()

    def _collect(self) -> None:
        self._ticks += 1
        if self._ticks % 60 == 0:
            gc.collect()
        elif self._ticks % 10 == 0:
            gc.collect(1)
        else:
            gc.collect(0)
