"""Centralized constants for the SC_Toolbox ecosystem.

Magic numbers that were previously scattered across multiple modules live
here.  API-specific timeouts and cache TTLs remain in ``api_config.py``.
"""

# ---------------------------------------------------------------------------
# HTTP client defaults
# ---------------------------------------------------------------------------
DEFAULT_MAX_RETRIES: int = 3
DEFAULT_BACKOFF_BASE: float = 1.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
LOG_MAX_BYTES: int = 2 * 1024 * 1024       # 2 MB per normal log file
CRASH_LOG_MAX_BYTES: int = 4 * 1024 * 1024  # 4 MB per crash log file
LOG_BACKUP_COUNT: int = 3

LOG_FORMAT: str = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

CRASH_LOG_FORMAT: str = (
    "%(asctime)s.%(msecs)03d [%(levelname)-8s] %(name)s "
    "(PID %(process)d | %(threadName)s) "
    "%(filename)s:%(lineno)d %(funcName)s: %(message)s"
)

# ---------------------------------------------------------------------------
# IPC
# ---------------------------------------------------------------------------
IPC_LOCK_TIMEOUT: float = 2.0
IPC_LOCK_RETRY_DELAY: float = 0.02
IPC_MAX_FILE_BYTES: int = 1 * 1024 * 1024  # 1 MB

# ---------------------------------------------------------------------------
# Process management
# ---------------------------------------------------------------------------
PROCESS_START_COOLDOWN: float = 5.0   # base min seconds between launches
PROCESS_MAX_COOLDOWN: float = 60.0    # max backoff cap
PROCESS_SHUTDOWN_TIMEOUT: float = 2.0  # graceful shutdown grace period
