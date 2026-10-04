"""Keeps the owned-blueprint list in step with the game logs, by itself.

WHY THIS EXISTS.  Blueprints used to appear only after a sequence of steps:
the scan lived inside the Owned Blueprints page, so it did not exist until
that tab had been opened; it started only from the "crafting data loaded"
callback, which never fired again if the Fabricator tab had been opened
first; and each scan re-read every log backup (70 s for 4.5 GB on the machine
this was written on) with nothing on screen to say so.  Only once that had
finished did anything follow the live log.

Now the window owns one of these from the moment it is created:

* a timer on the UI thread asks for a poll every few seconds;
* the poll runs on a worker thread (never more than one) and reads only what
  is new -- see ``services.sc_log_scanner.IncrementalLogScanner``;
* names found come back over a Qt signal, so the inventory is changed and the
  page refreshed on the UI thread;
* names are matched against the blueprint list whenever either side changes,
  so it does not matter which of "logs read" and "blueprint data loaded"
  happens first.

Nothing here shows a dialog.  The state is one plain line of text, shown by
the Owned Blueprints page.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from PySide6.QtCore import QObject, QTimer, Signal

from services import sc_log_scanner

log = logging.getLogger(__name__)

POLL_INTERVAL_MS = 5000

NO_FOLDER_TEXT = (
    "Star Citizen folder not found, so new blueprints cannot be added "
    "automatically. The ones already saved are still here. "
    "Use SC Folder to point at the game.")


class OwnedBlueprintSync(QObject):
    """Polls the game logs in the background and marks blueprints owned."""

    status_changed = Signal(str)        # plain text for the Owned page
    manual_done = Signal(object)        # dict, after a "Scan Game Log" click

    _sig_names = Signal(object)         # worker -> UI: list of new names
    _sig_progress = Signal(int, int)    # worker -> UI: backups read, to read
    _sig_polled = Signal(object, bool)  # worker -> UI: PollResult, manual

    def __init__(self, parent, data_mgr, inventory,
                 on_changed: Optional[Callable[[], None]] = None,
                 scanner=None, interval_ms: int = POLL_INTERVAL_MS) -> None:
        super().__init__(parent)
        self._data = data_mgr
        self._inventory = inventory
        self._on_changed = on_changed
        self._scanner = scanner or sc_log_scanner.IncrementalLogScanner()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._want_full = False         # a manual scan is waiting its turn
        self._need_match = True         # names or blueprint data changed
        self._folder_found: Optional[bool] = None
        self._progress: Optional[tuple] = None
        self._last = sc_log_scanner.ApplyResult()
        self._status = "Checking your game logs for blueprints..."

        self._sig_names.connect(self._on_names)
        self._sig_progress.connect(self._on_progress)
        self._sig_polled.connect(self._on_polled)

        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._kick)

    # ── lifecycle ──

    def start(self) -> None:
        """Begin polling (first poll straight away, off the UI thread)."""
        self._stop.clear()
        if not self._timer.isActive():
            self._timer.start()
        QTimer.singleShot(0, self._kick)

    def stop(self) -> None:
        """Stop polling and save what has been read.  Safe to call twice."""
        self._timer.stop()
        self._stop.set()
        worker, self._worker = self._worker, None
        if worker is not None and worker.is_alive():
            worker.join(timeout=3.0)
        try:
            self._scanner.flush()
        except Exception:
            log.exception("[OwnedSync] saving scan state failed")

    # ── requests from the UI ──

    def rescan(self) -> None:
        """Manual fallback: read every log again, then report."""
        self._want_full = True
        self._set_status("Reading all game logs again...")
        self._kick()

    def folder_changed(self) -> None:
        """The player picked another Star Citizen folder."""
        self._scanner.forget_folder()
        self._kick()

    def on_crafting_changed(self) -> None:
        """Blueprint data was (re)loaded: names may match now."""
        self._need_match = True
        self._match()

    def restore_removed(self) -> int:
        """Put back blueprints the player removed that the logs show.
        Returns how many were added."""
        return len(self._match(restore_removed=True).added)

    def status_text(self) -> str:
        return self._status

    # ── polling ──

    def _kick(self) -> None:
        if self._stop.is_set():
            return
        if self._worker is not None and self._worker.is_alive():
            return                      # one poll at a time; next tick retries
        full, self._want_full = self._want_full, False
        self._worker = threading.Thread(
            target=self._work, args=(full,), daemon=True,
            name="OwnedBlueprintSync")
        self._worker.start()

    def _work(self, full: bool) -> None:
        """Worker thread.  Touches no widget and no inventory."""
        try:
            res = self._scanner.poll(
                on_names=lambda names: self._sig_names.emit(list(names)),
                on_progress=self._sig_progress.emit,
                stop=self._stop, full=full)
        except Exception:
            log.exception("[OwnedSync] poll failed")
            res = None
        if res is None or self._stop.is_set():
            if full and res is None:
                self._want_full = True  # lost the race with a tick: try again
            return
        try:
            self._sig_polled.emit(res, full)
        except RuntimeError:
            pass                        # window already destroyed

    # ── UI-thread slots ──

    def _on_names(self, names) -> None:
        self._need_match = True
        self._match()

    def _on_progress(self, done: int, total: int) -> None:
        self._progress = (done, total) if done < total else None
        self._refresh_status()

    def _on_polled(self, res, manual: bool) -> None:
        self._progress = None
        self._folder_found = bool(res.folder)
        applied = self._match(force=manual)
        self._refresh_status()
        if manual:
            self.manual_done.emit({
                "folder": res.folder,
                "names": res.names_total,
                "matched": applied.matched,
                "added": len(applied.added),
                "held_back": len(applied.held_back),
                "unmatched": list(applied.unmatched),
                "crafting_loaded": self._crafting_ready(),
            })

    # ── matching names to blueprints (UI thread) ──

    def _crafting_ready(self) -> bool:
        return bool(getattr(self._data, "crafting_loaded", False)
                    and getattr(self._data, "crafting_blueprints", None))

    def _match(self, force: bool = False, restore_removed: bool = False):
        """Mark what the logs name as owned.  Cheap; skipped when neither the
        names nor the blueprint data changed since the last time."""
        if not (self._need_match or force or restore_removed):
            return self._last
        if self._inventory is None or not self._crafting_ready():
            return self._last           # stays pending until the data loads
        names = self._scanner.names()
        try:
            res = sc_log_scanner.apply_log_names(
                names, self._data.crafting_blueprints, self._inventory,
                data_mgr=self._data, restore_removed=restore_removed)
        except Exception:
            log.exception("[OwnedSync] matching log names failed")
            return self._last
        self._need_match = False
        self._last = res
        if res.added:
            log.info("[OwnedSync] %d blueprint(s) newly marked owned", len(res.added))
            if self._on_changed is not None:
                try:
                    self._on_changed()
                except Exception:
                    log.exception("[OwnedSync] refresh callback failed")
        self._refresh_status()
        return res

    # ── status line ──

    def _set_status(self, text: str) -> None:
        if text != self._status:
            self._status = text
            self.status_changed.emit(text)

    def _refresh_status(self) -> None:
        if self._folder_found is False:
            self._set_status(NO_FOLDER_TEXT)
            return
        if self._progress is not None:
            done, total = self._progress
            self._set_status(
                f"Reading older game logs: {done:,} of {total:,} "
                "(only needed once)...")
            return
        if self._folder_found is None:
            return                      # first poll still running
        n = len(self._scanner.names())
        text = (f"Updating automatically from your game log. "
                f"{n} blueprint{'s' if n != 1 else ''} found in logs")
        if not self._crafting_ready():
            text += "; waiting for blueprint data to load."
        else:
            text += "."
            if self._last.unmatched:
                k = len(self._last.unmatched)
                text += (f" {k} not in the current blueprint list: "
                         + ", ".join(self._last.unmatched[:3])
                         + ("..." if k > 3 else "") + ".")
            if self._last.held_back:
                k = len(self._last.held_back)
                text += f" {k} left off because you removed {'it' if k == 1 else 'them'}."
        self._set_status(text)
