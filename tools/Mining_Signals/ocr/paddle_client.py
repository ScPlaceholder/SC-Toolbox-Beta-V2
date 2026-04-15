"""Client for the PaddleOCR sidecar daemon.

Runs under the main Python 3.14 environment (where paddlepaddle
cannot be installed) and manages a subprocess running
``paddle_daemon.py`` under a Python 3.13 embed that has the paddle
stack pre-installed.

See ``paddle_daemon.py`` for the wire protocol.

Public API:

    paddle_client.is_available() -> bool
        True if the py313 embed is present and the daemon is alive
        (or can be started). Cheap check, no subprocess spawn.

    paddle_client.recognize(img: PIL.Image.Image) -> list[dict] | None
        Send a PIL image to the daemon and return the list of
        recognized text regions. Each region is a dict with
        keys ``text``, ``conf``, ``y_mid``. Returns None on any
        failure (daemon unavailable, timeout, protocol error).
        Caller should fall back to another engine.

    paddle_client.shutdown() -> None
        Cleanly terminate the daemon (called at app exit).
"""

from __future__ import annotations

import io
import json
import logging
import os
import struct
import subprocess
import sys
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────

# Default location of the py313 embed (set by the install-time script).
# Can be overridden by setting MINING_SIGNALS_PADDLE_PYTHON environment
# variable to the absolute path of a python.exe that has paddlepaddle
# + paddleocr installed.
_DEFAULT_EMBED_PYTHON = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "SC_Toolbox",
    "py313_paddleocr",
    "python.exe",
)

_DAEMON_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "paddle_daemon.py")

# Timeouts (seconds)
_STARTUP_TIMEOUT = 60.0   # model loading + first inference is slow
_REQUEST_TIMEOUT = 30.0   # per-request upper bound
_BACKOFF_AFTER_FAIL = 30.0  # wait this long before retrying after a crash

# ─────────────────────────────────────────────────────────────
# Daemon state (module-level singleton)
# ─────────────────────────────────────────────────────────────

_proc: Optional[subprocess.Popen] = None
_lock = threading.Lock()
_last_fail_ts: float = 0.0
_stderr_thread: Optional[threading.Thread] = None


def _get_embed_python() -> Optional[str]:
    """Return path to the Python 3.13 embed's python.exe, or None."""
    override = os.environ.get("MINING_SIGNALS_PADDLE_PYTHON")
    if override and os.path.isfile(override):
        return override
    if os.path.isfile(_DEFAULT_EMBED_PYTHON):
        return _DEFAULT_EMBED_PYTHON
    return None


def _drain_stderr(proc: subprocess.Popen) -> None:
    """Background thread that forwards daemon stderr to Python logging."""
    try:
        for line in proc.stderr:
            try:
                decoded = line.decode("utf-8", errors="replace").rstrip()
            except Exception:
                continue
            if decoded:
                log.debug("paddle_daemon: %s", decoded)
    except Exception:
        pass


def _start_daemon() -> Optional[subprocess.Popen]:
    """Spawn the daemon and wait for its ready signal. Returns the Popen or None."""
    py = _get_embed_python()
    if py is None:
        log.info("paddle_client: py313 embed not found at %s", _DEFAULT_EMBED_PYTHON)
        return None
    if not os.path.isfile(_DAEMON_SCRIPT):
        log.error("paddle_client: daemon script missing at %s", _DAEMON_SCRIPT)
        return None

    log.info("paddle_client: starting daemon with %s", py)
    try:
        proc = subprocess.Popen(
            [py, _DAEMON_SCRIPT],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Run with no shell and no window on Windows
            creationflags=0x08000000 if sys.platform == "win32" else 0,  # CREATE_NO_WINDOW
        )
    except Exception as exc:
        log.error("paddle_client: failed to spawn daemon: %s", exc)
        return None

    # Start stderr drain thread
    global _stderr_thread
    _stderr_thread = threading.Thread(target=_drain_stderr, args=(proc,), daemon=True)
    _stderr_thread.start()

    # Wait for ready signal
    try:
        ready = _read_response(proc, timeout=_STARTUP_TIMEOUT)
    except Exception as exc:
        log.error("paddle_client: daemon failed during startup: %s", exc)
        try:
            proc.kill()
        except Exception:
            pass
        return None

    if not ready or not ready.get("ok") or ready.get("status") != "ready":
        log.error("paddle_client: daemon did not send ready signal: %r", ready)
        try:
            proc.kill()
        except Exception:
            pass
        return None

    log.info("paddle_client: daemon ready")
    return proc


def _read_exact(proc: subprocess.Popen, n: int, deadline: float) -> Optional[bytes]:
    """Read exactly n bytes from proc.stdout, respecting a deadline."""
    buf = bytearray()
    while len(buf) < n:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        # Raw read; subprocess pipes on Windows don't support select().
        # We do a blocking read but check process liveness occasionally.
        chunk = proc.stdout.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def _read_response(proc: subprocess.Popen, timeout: float) -> Optional[dict]:
    """Read one length-prefixed JSON response from the daemon."""
    deadline = time.monotonic() + timeout
    header = _read_exact(proc, 4, deadline)
    if not header:
        raise IOError("daemon closed stdout")
    (length,) = struct.unpack(">I", header)
    if length == 0:
        return {}
    payload = _read_exact(proc, length, deadline)
    if payload is None:
        raise IOError("timeout reading daemon response body")
    try:
        return json.loads(payload.decode("utf-8"))
    except Exception as exc:
        raise IOError(f"malformed daemon response: {exc}")


def _send_request(proc: subprocess.Popen, png_bytes: bytes) -> None:
    """Write a length-prefixed PNG blob to the daemon."""
    proc.stdin.write(struct.pack(">I", len(png_bytes)))
    proc.stdin.write(png_bytes)
    proc.stdin.flush()


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────


def is_available() -> bool:
    """Cheap check: returns True if the py313 embed exists on disk."""
    return _get_embed_python() is not None


def _ensure_daemon() -> Optional[subprocess.Popen]:
    """Get a running daemon, starting one if necessary. Respects backoff."""
    global _proc, _last_fail_ts

    # Short-circuit if we're in backoff
    if _proc is None and (time.time() - _last_fail_ts) < _BACKOFF_AFTER_FAIL:
        return None

    if _proc is not None:
        # Check it's still alive
        if _proc.poll() is not None:
            log.warning("paddle_client: daemon died (rc=%s), will restart", _proc.returncode)
            _proc = None

    if _proc is None:
        _proc = _start_daemon()
        if _proc is None:
            _last_fail_ts = time.time()
            return None

    return _proc


def recognize(img) -> Optional[list]:
    """Send a PIL Image to PaddleOCR and return recognized text regions.

    Returns a list of dicts (``text``, ``conf``, ``y_mid``) on success,
    or None if the daemon is unavailable or the request failed.
    The caller should fall back to another engine on None.
    """
    global _proc, _last_fail_ts

    with _lock:
        proc = _ensure_daemon()
        if proc is None:
            return None

        # Serialize the image to PNG bytes
        try:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            png_bytes = buf.getvalue()
        except Exception as exc:
            log.error("paddle_client: PNG encode failed: %s", exc)
            return None

        try:
            _send_request(proc, png_bytes)
            response = _read_response(proc, timeout=_REQUEST_TIMEOUT)
        except Exception as exc:
            log.error("paddle_client: request failed: %s — killing daemon", exc)
            try:
                proc.kill()
            except Exception:
                pass
            _proc = None
            _last_fail_ts = time.time()
            return None

        if not response or not response.get("ok"):
            err = (response or {}).get("error", "unknown")
            log.warning("paddle_client: daemon reported failure: %s", err)
            return None

        return response.get("texts", [])


def shutdown() -> None:
    """Terminate the daemon cleanly. Safe to call even if not started."""
    global _proc
    with _lock:
        if _proc is None:
            return
        try:
            _proc.stdin.close()
        except Exception:
            pass
        try:
            _proc.wait(timeout=2.0)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
        _proc = None
