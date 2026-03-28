"""
Centralized logging configuration for the SC_Toolbox ecosystem.

Call ``setup_logging()`` once at process start to configure the root logger
with both console and (optionally) file output.  Subprocess log files are
written into a ``logs/`` directory next to the skill directory.
"""
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler
from typing import Optional

from shared.constants import (
    LOG_MAX_BYTES,
    LOG_BACKUP_COUNT,
    LOG_FORMAT,
    LOG_DATE_FORMAT,
)

_LOG_DIR: Optional[str] = None
_CONFIGURED = False
_INIT_LOCK = threading.Lock()

# Re-export for backward compatibility
DEFAULT_LEVEL = logging.INFO
MAX_LOG_BYTES = LOG_MAX_BYTES
BACKUP_COUNT = LOG_BACKUP_COUNT


def _ensure_log_dir(base_dir: str) -> str:
    """Create and return the logs/ directory under *base_dir*."""
    global _LOG_DIR
    if _LOG_DIR:
        return _LOG_DIR
    _LOG_DIR = os.path.join(base_dir, "logs")
    os.makedirs(_LOG_DIR, exist_ok=True)
    return _LOG_DIR


def setup_logging(
    *,
    name: str = "sc_toolbox",
    base_dir: Optional[str] = None,
    level: int = DEFAULT_LEVEL,
    log_to_file: bool = True,
    log_to_console: bool = True,
) -> logging.Logger:
    """Configure the root logger and return a named child logger.

    Parameters
    ----------
    name:
        Logger name (used as the log-file stem, e.g. ``sc_toolbox.log``).
    base_dir:
        Directory that will contain the ``logs/`` subfolder.
        Defaults to the SC_Toolbox skill directory.
    level:
        Logging level for the root logger.
    log_to_file:
        Whether to add a rotating file handler.
    log_to_console:
        Whether to add a stderr stream handler.
    """
    global _CONFIGURED
    with _INIT_LOCK:
        if _CONFIGURED:
            return logging.getLogger(name)

        if base_dir is None:
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

        root = logging.getLogger()
        root.setLevel(level)

        formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE_FORMAT)

        if log_to_console:
            sh = logging.StreamHandler(sys.stderr)
            sh.setFormatter(formatter)
            sh.setLevel(level)
            root.addHandler(sh)

        if log_to_file:
            log_dir = _ensure_log_dir(base_dir)
            log_path = os.path.join(log_dir, f"{name}.log")
            fh = RotatingFileHandler(
                log_path,
                maxBytes=MAX_LOG_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            fh.setLevel(level)
            root.addHandler(fh)

        _CONFIGURED = True
        return logging.getLogger(name)


def get_subprocess_log_path(skill_id: str, base_dir: Optional[str] = None) -> str:
    """Return a log-file path for a skill subprocess.

    The file can be passed to ``subprocess.Popen(stderr=open(...))`` so that
    child output is captured instead of discarded.
    """
    if base_dir is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    log_dir = _ensure_log_dir(base_dir)
    return os.path.join(log_dir, f"{skill_id}.log")
