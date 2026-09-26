"""Engine-mode toggle for the Mining Signals OCR pipeline.

Selects between the two OCR stacks at runtime:

  ``fast``   (default) — the SC-OCR surgical pipeline
             (``ocr/sc_ocr/api.py``): deterministic NumPy stages +
             ONNX glyph classifiers, ~23 ms per scan, no
             subprocesses.

  ``legacy`` — the original three-engine cross-validation stack:
             Tesseract A + Tesseract B + PaddleOCR sidecar voting
             (``ocr/screen_reader.extract_number``), ~700 ms with
             Tesseract only / ~1.2 s with the Paddle sidecar warm.

Resolution order (first match wins):

  1. Environment variable ``MINING_SIGNALS_OCR_MODE``
     (``fast`` / ``legacy`` — case-insensitive). Lets Elah force a
     mode for a single launch without touching any file, and beats
     the config so a stale config value can never lock debugging
     out.
  2. Config key ``ocr_engine_mode`` in the persistent
     ``mining_signals/config.json``. Survives Velopack upgrades.
  3. Default ``fast``.

The config read is mtime-cached, so flipping the config key takes
effect on the next scan without an app restart.  Any value other
than ``legacy`` (typos, empty string, null) resolves to ``fast`` —
the fast path is the fail-safe.
"""

from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

_ENV_VAR = "MINING_SIGNALS_OCR_MODE"
_CONFIG_KEY = "ocr_engine_mode"

_MODES = ("fast", "legacy")

_cache_lock = threading.Lock()
_cache_mode: str | None = None
_cache_mtime: float | None = None
_cache_path: str | None = None


def _config_path() -> str:
    """Persistent config path via the shared resolver, with a
    fallback to the in-tool legacy location if the shared module
    can't be imported (e.g. OCR package used standalone)."""
    try:  # pragma: no cover - import wiring
        from mining_shared.paths import resolve_config_path
        return str(resolve_config_path())
    except Exception:
        tool_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        local = os.path.join(tool_dir, "mining_signals_config.json")
        return local if os.path.isfile(local) else os.path.join(
            os.environ.get("LOCALAPPDATA", ""),
            "SC_Toolbox", "mining_signals", "config.json",
        )


def _read_config_mode() -> str | None:
    """Read ``ocr_engine_mode`` from the config file, or None."""
    path = _config_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    global _cache_mode, _cache_mtime, _cache_path
    with _cache_lock:
        if _cache_mode is not None and _cache_path == path and _cache_mtime == mtime:
            return _cache_mode
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        mode = cfg.get(_CONFIG_KEY)
    except (OSError, json.JSONDecodeError, AttributeError):
        mode = None
    if not isinstance(mode, str):
        mode = None
    mode = mode.strip().lower()
    if mode not in _MODES:
        mode = None
    with _cache_lock:
        _cache_mode = mode
        _cache_mtime = mtime
        _cache_path = path
    return mode


def get_engine_mode() -> str:
    """Current engine mode: ``"fast"`` or ``"legacy"``.

    Env var wins over config; config wins over the ``fast``
    default. Never raises — any failure lands on ``fast``.
    """
    env = os.environ.get(_ENV_VAR, "").strip().lower()
    if env in _MODES:
        return env
    try:
        mode = _read_config_mode()
    except Exception:  # pragma: no cover - defensive
        mode = None
    return mode if mode in _MODES else "fast"


def is_legacy() -> bool:
    return get_engine_mode() == "legacy"


def set_engine_mode(mode: str, cfg: dict | None = None) -> dict:
    """Set the mode in a config dict (does NOT touch disk).

    Returns the (possibly newly created) config dict so callers can
    pass it straight to their existing atomic-save helper — config
    writing conventions (tmp-file + os.replace, persistent path)
    live in ``ui/app.py`` and must not be duplicated here.

    Raises ValueError on an unknown mode so a typo can't silently
    write garbage that the reader then maps to ``fast``.
    """
    mode = (mode or "").strip().lower()
    if mode not in _MODES:
        raise ValueError(f"unknown OCR engine mode {mode!r} (expected one of {_MODES})")
    if cfg is None:
        cfg = {}
    cfg[_CONFIG_KEY] = mode
    return cfg
