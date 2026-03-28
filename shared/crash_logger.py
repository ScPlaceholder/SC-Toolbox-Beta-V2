"""
Comprehensive crash logger for SC_Toolbox skill subprocesses.

Call ``init_crash_logging(skill_name)`` once at the very top of each
skill's ``main()`` to get:

* Flush-on-write rotating log file (``logs/<skill>.crash.log``)
* ``sys.excepthook`` for unhandled main-thread exceptions
* ``threading.excepthook`` for unhandled background-thread exceptions
* Qt message handler routing (debug/warning/critical/fatal)
* Startup banner (PID, Python version, argv, cwd)
* atexit shutdown marker (distinguishes clean exit from crash)
* stderr echo for WARNING+ (so process_manager captures critical messages)
"""
from __future__ import annotations

import atexit
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

from shared.constants import (
    CRASH_LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
    CRASH_LOG_FORMAT,
    LOG_DATE_FORMAT,
)

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOG_DIR = os.path.join(_BASE_DIR, "logs")

MAX_LOG_BYTES = CRASH_LOG_MAX_BYTES
BACKUP_COUNT = LOG_BACKUP_COUNT
LOG_FORMAT = CRASH_LOG_FORMAT

_initialised = False
_init_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Flush-safe handler — flushes after every emit to prevent log loss on crash
# ---------------------------------------------------------------------------
class _FlushRotatingHandler(RotatingFileHandler):
    """RotatingFileHandler that flushes after every emit."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            super().emit(record)
            self.flush()
        except Exception:
            pass  # never crash the process because of logging


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def init_crash_logging(skill_name: str) -> logging.Logger:
    """Initialise crash-aware logging for a skill subprocess.

    Returns a named logger the caller can use.  Safe to call more than once
    (subsequent calls are no-ops that return the existing logger).
    """
    global _initialised
    with _init_lock:
        if _initialised:
            return logging.getLogger(skill_name)
        _initialised = True

    os.makedirs(_LOG_DIR, exist_ok=True)

    log_path = os.path.join(_LOG_DIR, f"{skill_name}.crash.log")
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

    # ── Root logger setup ─────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler — flush on every write
    fh = _FlushRotatingHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    root.addHandler(fh)

    # stderr handler — WARNING+ so process_manager's log file captures them
    sh = logging.StreamHandler(sys.stderr)
    sh.setLevel(logging.WARNING)
    sh.setFormatter(formatter)
    root.addHandler(sh)

    log = logging.getLogger(skill_name)

    # ── Startup banner ────────────────────────────────────────────────────
    log.info("=" * 72)
    log.info("%s starting  (PID %d)", skill_name, os.getpid())
    log.info("Python %s", sys.version)
    log.info("argv:  %s", sys.argv)
    log.info("cwd:   %s", os.getcwd())
    log.info("=" * 72)

    # ── Unhandled exception hooks ─────────────────────────────────────────
    _prev_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, (SystemExit, KeyboardInterrupt)):
            _prev_excepthook(exc_type, exc_value, exc_tb)
            return
        log.critical(
            "UNHANDLED EXCEPTION on main thread",
            exc_info=(exc_type, exc_value, exc_tb),
        )

    sys.excepthook = _excepthook

    _prev_threading_hook = getattr(threading, "excepthook", None)

    def _threading_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        log.critical(
            "UNHANDLED EXCEPTION on thread %r",
            args.thread.name if args.thread else "<unknown>",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        if _prev_threading_hook:
            _prev_threading_hook(args)

    threading.excepthook = _threading_excepthook

    # ── Qt message handler ────────────────────────────────────────────────
    _install_qt_handler(log)

    # ── atexit shutdown marker ────────────────────────────────────────────
    def _on_exit():
        log.info("%s shutting down (PID %d)", skill_name, os.getpid())
        log.info("-" * 72)

    atexit.register(_on_exit)

    return log


def _install_qt_handler(log: logging.Logger) -> None:
    """Install a Qt message handler if PySide6 is available."""
    try:
        from PySide6.QtCore import QtMsgType, qInstallMessageHandler
    except ImportError:
        return  # Qt not available — nothing to hook

    def _qt_msg_handler(msg_type, context, message):
        if msg_type == QtMsgType.QtDebugMsg:
            log.debug("[Qt] %s", message)
        elif msg_type == QtMsgType.QtWarningMsg:
            log.warning("[Qt] %s", message)
        elif msg_type == QtMsgType.QtCriticalMsg:
            log.error("[Qt CRITICAL] %s", message)
        elif msg_type == QtMsgType.QtFatalMsg:
            log.critical("[Qt FATAL] %s", message)
        else:
            log.info("[Qt] %s", message)

    qInstallMessageHandler(_qt_msg_handler)
