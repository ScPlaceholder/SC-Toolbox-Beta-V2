"""
Robust IPC via JSONL temp files with proper lock-file mutual exclusion.

The original code used msvcrt.locking on 1 byte at offset 0 of the DATA file,
which provides no real mutual exclusion.  This module locks a separate .lock
file (entire-file lock) so readers and writers are properly serialised.
"""
import json
import logging
import msvcrt
import os
import time

log = logging.getLogger(__name__)

_LOCK_SIZE = 1  # single byte at offset 0 — standard msvcrt pattern
_LOCK_TIMEOUT = 2.0      # seconds
_LOCK_RETRY_DELAY = 0.02  # seconds


def _acquire_lock(fh, timeout: float = _LOCK_TIMEOUT) -> None:
    """Lock the first _LOCK_SIZE bytes of *fh* (opened in binary mode)."""
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


def _open_lock(lock_path: str):
    """Open a lock file, ensuring at least 1 byte exists for msvcrt.locking."""
    lf = open(lock_path, "a+b")
    lf.seek(0, 2)
    if lf.tell() == 0:
        lf.write(b'\x00')
        lf.flush()
    return lf


def ipc_write(cmd_file: str, data: dict) -> bool:
    """Append a single JSON command to *cmd_file* under lock. Returns True on success."""
    lock_path = cmd_file + ".lock"
    lf = None
    try:
        lf = _open_lock(lock_path)
        _acquire_lock(lf)
        try:
            with open(cmd_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
        finally:
            _release_lock(lf)
        return True
    except TimeoutError:
        log.warning("ipc_write: lock timeout for %s", cmd_file)
        return False
    finally:
        if lf:
            lf.close()


def ipc_read_and_clear(cmd_file: str) -> list:
    """Read all commands from *cmd_file*, truncate it, and return parsed dicts.

    Malformed lines are logged and skipped.
    """
    lines = []
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

    commands = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            commands.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("ipc_read_and_clear: skipping malformed line: %r", line[:120])
    return commands


def ipc_read_incremental(cmd_file: str, offset: int) -> tuple:
    """Read commands from *offset* onwards without truncating.

    Returns (commands, new_offset).
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

    commands = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            commands.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("ipc_read_incremental: skipping malformed line: %r", line[:120])
    return commands, new_offset
