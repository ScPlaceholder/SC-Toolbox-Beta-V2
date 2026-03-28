"""
IPCWatcher – QThread-based IPC command reader.

Replaces the threading.Thread + root.after() pattern used in every tool.
Polls the JSONL command file and emits a signal for each command.
"""

from __future__ import annotations
import json
import logging
import time
from typing import Any, Dict

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication

from shared.ipc import ipc_read_and_clear

log = logging.getLogger(__name__)


class IPCWatcher(QThread):
    """Background thread that polls an IPC JSONL file.

    Usage:
        watcher = IPCWatcher(cmd_file_path)
        watcher.command_received.connect(self._dispatch)
        watcher.start()

    The thread auto-stops on ``QApplication.aboutToQuit`` so it never
    outlives Qt's event loop (which would cause a fatal
    "QThread: Destroyed while thread is still running" crash).
    """

    command_received = Signal(dict)

    def __init__(self, cmd_file: str, poll_ms: int = 300, parent=None):
        super().__init__(parent)
        self._cmd_file = cmd_file
        self._poll_s = poll_ms / 1000.0
        self._running = True

        # Auto-stop before Qt teardown to prevent fatal QThread crash
        app = QApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.stop)

    def run(self) -> None:
        import os
        _missing_count = 0
        while self._running:
            try:
                if not os.path.exists(self._cmd_file):
                    _missing_count += 1
                    if _missing_count == 1:
                        log.warning("IPCWatcher: file deleted: %s", self._cmd_file)
                    if _missing_count > 3:
                        # File gone for >3 polls — parent launcher has shut down
                        # or restarted.  Tell the app to exit so we don't become
                        # a zombie process.
                        log.info("IPCWatcher: file missing, sending quit")
                        self.command_received.emit({"type": "quit"})
                        break
                    time.sleep(self._poll_s)
                    continue
                _missing_count = 0
                commands = ipc_read_and_clear(self._cmd_file)
                for cmd in commands:
                    self.command_received.emit(cmd)
            except (OSError, json.JSONDecodeError, ValueError):
                _missing_count += 1
                if _missing_count <= 1:
                    log.warning("IPCWatcher error reading %s", self._cmd_file)
                if _missing_count > 3:
                    log.info("IPCWatcher: persistent errors, sending quit")
                    self.command_received.emit({"type": "quit"})
                    break
            time.sleep(self._poll_s)

    def stop(self) -> None:
        self._running = False
        if not self.wait(2000):
            self.terminate()
