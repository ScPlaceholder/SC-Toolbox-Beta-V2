"""Run backend calls off the GUI thread.

Every backend call from the Dev Mode UI goes through ``TaskRunner.start``.
The call runs on a private ``QThreadPool``; its progress callback and its
result are marshalled back to the GUI thread through Qt's queued signals,
so page code only ever touches widgets from the GUI thread.

Lifetime safety: a job's signals are connected to *bound methods of the
TaskRunner* (a QObject parented to its page). If the page is destroyed
while a job is still running, Qt drops the connection and the finished
job's result is simply discarded, instead of calling into a deleted widget.
"""

from __future__ import annotations

import inspect
import itertools
import logging
import time
import traceback
import weakref
from dataclasses import dataclass
from typing import Any, Callable, Optional

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QApplication

log = logging.getLogger(__name__)

_POOL: Optional[QThreadPool] = None
_ids = itertools.count(1)


def pool() -> QThreadPool:
    global _POOL
    if _POOL is None:
        _POOL = QThreadPool()
        _POOL.setMaxThreadCount(4)
    return _POOL


def drain(timeout_s: float = 10.0) -> bool:
    """Block (processing events) until every job has finished AND its
    queued result has been delivered. For tests and screenshot rendering;
    the UI itself never calls this. Returns False on timeout."""
    app = QApplication.instance()
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if pool().activeThreadCount() == 0 and not TaskRunner.any_pending():
            app.processEvents()
            return True
        pool().waitForDone(20)
    return False


class _JobSignals(QObject):
    progress = Signal(int, float, str)
    done = Signal(int, object)
    failed = Signal(int, str, str)


class _Job(QRunnable):
    def __init__(self, job_id: int, fn: Callable, args, kwargs, with_progress: bool):
        super().__init__()
        self.setAutoDelete(True)
        self.job_id = job_id
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.with_progress = with_progress
        self.signals = _JobSignals()

    @staticmethod
    def _emit(sig, *args) -> None:
        """Emit, unless the window that owned the signals has already closed. Closing Dev Mode while a
        job runs deletes them, and the old code then raised "Signal source has been deleted" - from
        _progress that aborted the job itself mid-run. Only that exact case is swallowed."""
        try:
            sig.emit(*args)
        except RuntimeError as e:
            if "has been deleted" not in str(e):
                raise
            log.debug("Dev Mode job result dropped: its window closed first")

    def _progress(self, frac: float, msg: str = "") -> None:
        try:
            f = float(frac)
        except (TypeError, ValueError):
            f = 0.0
        self._emit(self.signals.progress, self.job_id, max(0.0, min(1.0, f)), str(msg or ""))

    def run(self) -> None:
        kwargs = dict(self.kwargs)
        if self.with_progress:
            kwargs["progress"] = self._progress
        try:
            result = self.fn(*self.args, **kwargs)
        except Exception as exc:  # forwarded to the UI and logged, never swallowed
            log.exception("Dev Mode job %s failed", getattr(self.fn, "__name__", self.fn))
            self._emit(self.signals.failed, self.job_id, f"{type(exc).__name__}: {exc}",
                       traceback.format_exc())
            return
        self._emit(self.signals.done, self.job_id, result)


@dataclass
class _Pending:
    name: str
    on_done: Optional[Callable[[Any], None]]
    on_error: Optional[Callable[[str], None]]
    on_progress: Optional[Callable[[float, str], None]]
    # Keep the signal carrier alive until its queued results are delivered;
    # the QRunnable itself is auto-deleted by the pool when run() returns.
    signals: _JobSignals


def _accepts_progress(fn: Callable) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    return "progress" in sig.parameters or any(
        p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )


class TaskRunner(QObject):
    """Owns the background jobs started by one page."""

    busy_changed = Signal(bool)
    activity = Signal(str)          # human-readable status for the window footer
    error = Signal(str, str)        # (job name, message) for jobs without on_error

    _live: "weakref.WeakSet[TaskRunner]" = weakref.WeakSet()

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._pending: dict[int, _Pending] = {}
        TaskRunner._live.add(self)
        # A destroyed runner can never receive its jobs' results, so it
        # must stop counting as busy (otherwise drain() would wait forever).
        self.destroyed.connect(self._pending.clear)

    @classmethod
    def any_pending(cls) -> bool:
        return any(r._pending for r in list(cls._live))

    @property
    def busy(self) -> bool:
        return bool(self._pending)

    def is_running(self, name: str) -> bool:
        return any(p.name == name for p in self._pending.values())

    def start(self, name: str, fn: Callable, *args,
              on_done: Optional[Callable[[Any], None]] = None,
              on_error: Optional[Callable[[str], None]] = None,
              on_progress: Optional[Callable[[float, str], None]] = None,
              **kwargs) -> int:
        """Run ``fn(*args, **kwargs)`` on the pool. If ``fn`` takes a
        ``progress`` parameter it is supplied automatically."""
        job_id = next(_ids)
        job = _Job(job_id, fn, args, kwargs, _accepts_progress(fn))
        job.signals.progress.connect(self._on_progress)
        job.signals.done.connect(self._on_done)
        job.signals.failed.connect(self._on_failed)
        was_busy = self.busy
        self._pending[job_id] = _Pending(name, on_done, on_error, on_progress, job.signals)
        if not was_busy:
            self.busy_changed.emit(True)
        self.activity.emit(f"{name}…")
        pool().start(job)
        return job_id

    def _finish(self, job_id: int) -> Optional[_Pending]:
        p = self._pending.pop(job_id, None)
        if not self._pending:
            self.busy_changed.emit(False)
        return p

    @Slot(int, float, str)
    def _on_progress(self, job_id: int, frac: float, msg: str) -> None:
        p = self._pending.get(job_id)
        if p is None:
            return
        if p.on_progress:
            p.on_progress(frac, msg)
        self.activity.emit(f"{p.name}: {msg or ''} {frac * 100:.0f}%".replace(":  ", ": "))

    @Slot(int, object)
    def _on_done(self, job_id: int, result: object) -> None:
        p = self._finish(job_id)
        if p is None:
            return
        self.activity.emit(f"{p.name}: done")
        if p.on_done:
            p.on_done(result)

    @Slot(int, str, str)
    def _on_failed(self, job_id: int, message: str, tb: str) -> None:
        p = self._finish(job_id)
        if p is None:
            return
        self.activity.emit(f"{p.name} failed: {message}")
        if p.on_error:
            p.on_error(message)
        else:
            self.error.emit(p.name, message)
