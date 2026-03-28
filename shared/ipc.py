"""
Robust IPC via JSONL temp files with proper lock-file mutual exclusion.

Features:
- File-based locking via msvcrt on a separate .lock file
- File size limits with automatic rotation
- Debug logging for all operations
- Designed to be replaced by socket-based IPC in future
"""
from __future__ import annotations

import json
import logging
import msvcrt
import os
import time
from typing import Any

from shared.constants import IPC_LOCK_TIMEOUT, IPC_LOCK_RETRY_DELAY, IPC_MAX_FILE_BYTES

log = logging.getLogger(__name__)

_LOCK_SIZE = 1
_LOCK_TIMEOUT = IPC_LOCK_TIMEOUT
_LOCK_RETRY_DELAY = IPC_LOCK_RETRY_DELAY

# File size limits — when the data file exceeds this, it is truncated
# on the next read-and-clear.  Incremental readers are unaffected because
# they only consume new data.
MAX_FILE_BYTES = IPC_MAX_FILE_BYTES

# If a lock file hasn't been modified in this many seconds, assume the
# holder crashed and recreate the lock file to unblock other processes.
_STALE_LOCK_SECONDS = 10.0


# ── Low-level locking ────────────────────────────────────────────────────────

def _acquire_lock(fh, timeout: float = _LOCK_TIMEOUT) -> None:
    """Lock the first byte of *fh* (opened in binary mode)."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, _LOCK_SIZE)
            return
        except (OSError, IOError):
            if time.monotonic() >= deadline:
                raise TimeoutError("IPC lock acquisition timed out")
            time.sleep(_LOCK_RETRY_DELAY)


def _release_lock(fh) -> None:
    """Unlock the region previously locked by _acquire_lock."""
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, _LOCK_SIZE)
    except (OSError, IOError) as e:
        log.warning("_release_lock failed: %s", e)


def _recover_stale_lock(lock_path: str) -> None:
    """Delete a lock file that appears to be held by a dead process.

    If the lock file's mtime is older than ``_STALE_LOCK_SECONDS``, it's
    almost certainly orphaned — the holding process crashed without
    releasing.  Deleting it lets a fresh ``_open_lock`` recreate cleanly.
    """
    try:
        age = time.time() - os.path.getmtime(lock_path)
        if age > _STALE_LOCK_SECONDS:
            os.remove(lock_path)
            log.info("ipc: removed stale lock file %s (age %.0fs)", lock_path, age)
    except OSError:
        pass  # file already gone or inaccessible — fine


def _open_lock(lock_path: str):
    """Open a lock file, ensuring at least 1 byte exists for msvcrt.locking.

    Recovers stale lock files left behind by crashed processes.
    """
    _recover_stale_lock(lock_path)
    lf = open(lock_path, "a+b")
    lf.seek(0, 2)
    if lf.tell() == 0:
        lf.write(b'\x00')
        lf.flush()
    return lf


# ── Public API ───────────────────────────────────────────────────────────────

def ipc_write(cmd_file: str, data: dict[str, Any]) -> bool:
    """Append a single JSON command to *cmd_file* under lock.

    Returns True on success.  If the file exceeds MAX_FILE_BYTES a warning
    is logged but the write still proceeds (the reader will truncate).
    """
    lock_path = cmd_file + ".lock"
    lf = None
    try:
        lf = _open_lock(lock_path)
        _acquire_lock(lf)
        try:
            with open(cmd_file, "a", encoding="utf-8") as f:
                payload = json.dumps(data, ensure_ascii=False)
                f.write(payload + "\n")
                f.flush()
                os.fsync(f.fileno())
            log.debug("ipc_write: %s -> %s", data.get("type", "?"), cmd_file)

            # Warn on oversized file
            try:
                size = os.path.getsize(cmd_file)
                if size > MAX_FILE_BYTES:
                    log.warning(
                        "ipc_write: %s is %d bytes (limit %d) — will be "
                        "truncated on next read_and_clear",
                        cmd_file, size, MAX_FILE_BYTES)
            except OSError:
                pass
        finally:
            _release_lock(lf)
        return True
    except TimeoutError:
        log.warning("ipc_write: lock timeout for %s", cmd_file)
        return False
    except (OSError, IOError) as exc:
        log.error("ipc_write: file error for %s: %s", cmd_file, exc)
        return False
    finally:
        if lf:
            lf.close()


def ipc_read_and_clear(cmd_file: str) -> list[dict[str, Any]]:
    """Read all commands from *cmd_file*, truncate it, and return parsed dicts.

    Malformed lines are logged and skipped.  If the file exceeds
    MAX_FILE_BYTES it is truncated even if no commands are found (rotation).
    """
    lines: list[str] = []
    lock_path = cmd_file + ".lock"
    lf = None
    try:
        lf = _open_lock(lock_path)
        _acquire_lock(lf)
        try:
            with open(cmd_file, "r+", encoding="utf-8") as f:
                lines = f.readlines()
                if lines:
                    f.seek(0)
                    f.truncate()
                else:
                    # Rotation: truncate oversized empty-command files
                    try:
                        if os.path.getsize(cmd_file) > MAX_FILE_BYTES:
                            f.seek(0)
                            f.truncate()
                            log.info("ipc_read_and_clear: rotated oversized %s", cmd_file)
                    except OSError:
                        pass
        finally:
            _release_lock(lf)
    except TimeoutError:
        log.warning("ipc_read_and_clear: lock timeout for %s", cmd_file)
        return []
    except (OSError, IOError) as exc:
        log.warning("ipc_read_and_clear: file error: %s", exc)
        return []
    finally:
        if lf:
            lf.close()

    commands: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            commands.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("ipc_read_and_clear: skipping malformed line: %r", line[:120])

    if commands:
        log.debug("ipc_read_and_clear: read %d command(s) from %s", len(commands), cmd_file)
    return commands


def ipc_read_incremental(cmd_file: str, offset: int) -> tuple[list[dict[str, Any]], int]:
    """Read commands from *offset* onwards without truncating.

    Returns ``(commands, new_offset)``.
    """
    lock_path = cmd_file + ".lock"
    lf = None
    try:
        lf = _open_lock(lock_path)
        _acquire_lock(lf)
        try:
            with open(cmd_file, "rb") as f:
                f.seek(offset)
                raw = f.read().decode("utf-8", errors="replace")
                new_offset = f.tell()
        finally:
            _release_lock(lf)
    except TimeoutError:
        log.warning("ipc_read_incremental: lock timeout for %s", cmd_file)
        return [], offset
    except (OSError, IOError) as exc:
        log.warning("ipc_read_incremental: file error: %s", exc)
        return [], offset
    finally:
        if lf:
            lf.close()

    commands: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            commands.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("ipc_read_incremental: skipping malformed line: %r", line[:120])

    if commands:
        log.debug("ipc_read_incremental: read %d command(s) from %s (offset %d->%d)",
                   len(commands), cmd_file, offset, new_offset)
    return commands, new_offset
