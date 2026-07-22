"""Persistent calibration storage for the SC mining HUD OCR.

Stores user-confirmed crop coordinates per HUD region so the runtime
can skip detection entirely (faster + zero drift). Lives at::

    %LOCALAPPDATA%\\SC_Toolbox\\sc_ocr\\calibration.json

Schema (versioned for forward compatibility)::

    {
      "version": 1,
      "calibrations": {
        "<region_x>,<region_y>,<region_w>,<region_h>": {
          "saved_at": "2026-04-19T17:12:00",
          "image_size": [width, height],   # captured image size after upscale
          "rows": {
            "_mineral_row":  {"x": ..., "y": ..., "w": ..., "h": ...},
            "mass":          {...},
            "resistance":    {...},
            "instability":   {...}
          },
          "value_column_left": <int>      # x-coord where value crops start
        }
      }
    }

Each row has both a stored bounding box AND a "locked" flag in the
caller's UI state — the file only stores LOCKED rows. A row that
isn't locked yet falls back to runtime detection for that field.

Thread-safety: the runtime is single-threaded for OCR; the dialog
runs in the same process. No locking needed.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger(__name__)

# Storage location — matches the pattern used by other tools
# (per-user-profile data under %LOCALAPPDATA%\SC_Toolbox).
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")
CALIBRATION_DIR = Path(_LOCALAPPDATA) / "SC_Toolbox" / "sc_ocr"
CALIBRATION_PATH = CALIBRATION_DIR / "calibration.json"

SCHEMA_VERSION = 1

# Field names recognized by the calibration system. Order matters for
# UI display; "_mineral_row" is optional and prefixed underscore so
# downstream code that iterates label_rows for value OCR skips it.
#
# "signature" is the signature-scanner digit cluster (4-5 digit signal
# value). It lives in a SEPARATE on-screen region from the HUD label
# rows (the dialog stores it under the ocr_region key, not the
# hud_region key), so it never collides with HUD iteration even when
# the user has both regions calibrated. HUD-OCR loops in
# ``onnx_hud_reader`` / ``sc_ocr.api`` / ``debug_overlay`` are
# hardcoded to ``("mass","resistance","instability")`` rather than
# iterating FIELD_NAMES, so adding it here doesn't leak into HUD code.
FIELD_NAMES: tuple[str, ...] = (
    "_mineral_row",
    "mass",
    "resistance",
    "instability",
    "signature",
    # ``needle`` calibrates the EASY/MEDIUM/HARD/EXTREME/IMPOSSIBLE
    # difficulty bar at the bottom of the SCAN RESULTS panel. Stored
    # alongside HUD rows under the HUD region key. ``to_label_rows``
    # below skips it (HUD value-OCR loop only iterates mass/resistance/
    # instability — same reason it skips ``signature``); the difficulty
    # detector in ``api.scan_hud_onnx`` reads it via ``get_row`` directly.
    "needle",
)

# Field names that are user-facing (drop the underscore prefix).
DISPLAY_NAMES: dict[str, str] = {
    "_mineral_row":  "Resource (Mineral)",
    "mass":          "Mass",
    "resistance":    "Resistance",
    "instability":   "Instability",
    "signature":     "Signature / Signal Value",
    "needle":        "Needle (difficulty)",
}


def _region_key(region: dict) -> str:
    """Deterministic string key for a region dict."""
    return (
        f"{int(region.get('x', 0))},"
        f"{int(region.get('y', 0))},"
        f"{int(region.get('w', 0))},"
        f"{int(region.get('h', 0))}"
    )


# ── mtime-keyed cache ──
# Public callers (``_find_label_rows`` runs ``to_label_rows`` once per
# scan, plus ``get_row`` for each locked field — typically 4-5 calls per
# scan tick) used to re-parse calibration.json on every invocation.
# That's ~1-5 ms of wasted JSON work per scan. Cache the parsed dict
# keyed on the file's mtime so the first call per scan parses, and the
# remaining calls hit memory.
#
# Invalidation is automatic: every ``save_row`` / ``remove_row`` /
# ``clear_region`` writes the file via ``_save_all``, which bumps mtime,
# which the next ``_load_all`` notices. This means a Lock-click in the
# Calibration Dialog is observed by the very next scan tick — no need
# to manually clear the cache from the save path. mtime granularity on
# Windows NTFS is 100 ns; on most filesystems it's >= 1 ms — both far
# finer than typical scan-tick spacing (100 ms+), so we won't miss a
# write even if save and load happen back-to-back.
_CACHED_DATA: Optional[dict] = None
_CACHED_MTIME_NS: Optional[int] = None


def _file_mtime_ns() -> Optional[int]:
    """Return current mtime_ns of the calibration file, or None if it
    doesn't exist (or stat fails for any reason)."""
    try:
        return CALIBRATION_PATH.stat().st_mtime_ns
    except (OSError, FileNotFoundError):
        return None


def _load_all() -> dict:
    """Read the whole calibration file. Returns an empty schema if
    the file doesn't exist or is corrupt.

    mtime-keyed cache: returns the previously-parsed dict when the
    file's mtime hasn't changed since last read. The cached dict is
    returned by reference, so callers MUST NOT mutate it in place —
    use ``copy.deepcopy`` if mutation is needed (the only mutating
    callers are ``save_row`` / ``remove_row`` / ``clear_region``,
    which each call ``_save_all`` to rewrite the file, and that
    naturally invalidates the cache for everyone else).
    """
    global _CACHED_DATA, _CACHED_MTIME_NS

    cur_mtime = _file_mtime_ns()

    # Cache miss case 1: file doesn't exist (or stat failed).
    if cur_mtime is None:
        # If it disappeared since the last cached read, drop the cache.
        if _CACHED_MTIME_NS is not None:
            log.debug(
                "calibration: file gone (was mtime_ns=%s) — clearing cache",
                _CACHED_MTIME_NS,
            )
            _CACHED_DATA = None
            _CACHED_MTIME_NS = None
        return {"version": SCHEMA_VERSION, "calibrations": {}}

    # Cache hit: same mtime as the in-memory snapshot.
    if (
        _CACHED_DATA is not None
        and _CACHED_MTIME_NS is not None
        and cur_mtime == _CACHED_MTIME_NS
    ):
        return _CACHED_DATA

    # Cache miss case 2: file exists but mtime has advanced (or this
    # is the first read). Parse and cache.
    try:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("calibrations", {})
        _CACHED_DATA = data
        _CACHED_MTIME_NS = cur_mtime
        # One-line debug breadcrumb so users / devs can see in logs
        # whether a freshly-saved Lock landed on disk and was picked
        # up. Sample (logged once per write):
        #   calibration: re-loaded mtime_ns=1733... regions=2 keys=['1920,...']
        try:
            _regions = sorted((data.get("calibrations") or {}).keys())
            log.debug(
                "calibration: re-loaded mtime_ns=%s regions=%d keys=%s",
                cur_mtime, len(_regions), _regions,
            )
        except Exception:
            pass
        return data
    except Exception as exc:
        log.warning("calibration.json load failed: %s — using empty schema", exc)
        # Don't poison the cache with the empty fallback — leave the
        # previous good snapshot in place if we have one. Only clear
        # if we've never read successfully.
        if _CACHED_DATA is None:
            return {"version": SCHEMA_VERSION, "calibrations": {}}
        return _CACHED_DATA


def _save_all(data: dict) -> None:
    """Atomic write of the calibration file (tmp + rename).

    On success, refresh the cache directly so the very next ``_load_all``
    call sees this exact dict without re-reading from disk. mtime-based
    invalidation alone would also work (the rename bumps mtime, the next
    read parses the file again), but populating the cache here avoids
    one redundant parse and removes any race with mtime granularity on
    exotic filesystems where mtime updates lag the rename.
    """
    global _CACHED_DATA, _CACHED_MTIME_NS
    try:
        CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CALIBRATION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, CALIBRATION_PATH)
        # Refresh cache with the just-written dict + its new mtime.
        try:
            _CACHED_DATA = data
            _CACHED_MTIME_NS = _file_mtime_ns()
        except Exception:
            # If stat fails after a successful write, just clear the
            # cache so the next read does a fresh parse.
            _CACHED_DATA = None
            _CACHED_MTIME_NS = None
    except Exception as exc:
        log.warning("calibration.json write failed: %s", exc)


# ── Public API ──


def load(region: dict) -> Optional[dict]:
    """Return the calibration entry for this region, or None.

    Returned dict has the schema::
        {
            "rows": {field_name: {"x": int, "y": int, "w": int, "h": int}},
            "value_column_left": int,
            "image_size": [w, h],
            "saved_at": str,
        }
    """
    data = _load_all()
    return data["calibrations"].get(_region_key(region))


def get_row(
    region: dict,
    field: str,
    dy: int = 0,
    dx: int = 0,
) -> Optional[dict]:
    """Return a single row's calibration box, or None if not set.

    ``dy`` / ``dx`` apply a runtime offset to the saved coordinates
    so callers can drift-correct against the panel's actual current
    position (see :func:`compute_drift_y`). The saved coordinates are
    not mutated; this is purely an output transform.
    """
    cal = load(region)
    if not cal:
        return None
    box = cal.get("rows", {}).get(field)
    if box is None:
        return None
    if dy == 0 and dx == 0:
        return box
    return {
        "x": int(box["x"]) + int(dx),
        "y": int(box["y"]) + int(dy),
        "w": int(box["w"]),
        "h": int(box["h"]),
    }


def compute_drift_y(region: dict, current_mineral_y: int) -> int:
    """Return the vertical offset between the calibrated mineral-row
    position and the panel's CURRENT mineral-row position.

    Why this exists: the SC mining HUD panel slides up/down inside
    the captured region as the player adjusts pitch toward different
    rocks. Calibrated value-row crops are pinned to ABSOLUTE pixel
    positions, so a small panel shift makes them point at empty
    space (or — worse — the wrong row entirely). Anchoring on the
    mineral row lets us slide the locked crops along with the panel.

    Returns the delta as ``current_mineral_y - calibrated_y``. The
    caller passes this as ``dy`` to ``get_row`` to drift-correct
    every locked field in one consistent shift.

    Returns ``0`` if no calibration exists, no ``_mineral_row`` lock
    is saved, or the proposed drift is implausibly large (>40% of
    the captured image height — that would mean the panel jumped to
    a totally different position, in which case the user should
    recalibrate, not have the toolbox guess).
    """
    cal = load(region)
    if not cal:
        return 0
    mineral_box = cal.get("rows", {}).get("_mineral_row")
    if not mineral_box:
        return 0
    try:
        calibrated_y = int(mineral_box["y"])
    except (TypeError, ValueError, KeyError):
        return 0
    delta = int(current_mineral_y) - calibrated_y
    # Clamp: a large jump indicates a region-resize event, a totally
    # different panel layout, or detection noise — not a smooth pan.
    img_size = cal.get("image_size") or [None, None]
    img_h = int(img_size[1]) if img_size[1] else int(region.get("h", 0))
    if img_h > 0 and abs(delta) > img_h * 0.4:
        log.info(
            "calibration.compute_drift_y: |delta|=%d > 40%% of %dpx — "
            "treating as too large to drift-correct (likely region "
            "resize or different panel)", abs(delta), img_h,
        )
        return 0
    return delta


def is_complete(region: dict) -> bool:
    """True when MASS, RESISTANCE, and INSTABILITY are all locked.
    (_mineral_row is optional — calibration is "complete" without it.)"""
    cal = load(region)
    if not cal:
        return False
    rows = cal.get("rows", {})
    return all(field in rows for field in ("mass", "resistance", "instability"))


def save_row(
    region: dict, field: str, box: dict[str, int],
    image_size: Optional[tuple[int, int]] = None,
    value_column_left: Optional[int] = None,
) -> None:
    """Save one row's calibration box. Called when the user clicks
    'Lock' on a row in the calibration dialog."""
    if field not in FIELD_NAMES:
        log.warning("save_row: unknown field %r", field)
        return
    data = _load_all()
    key = _region_key(region)
    entry = data["calibrations"].get(key)
    if entry is None:
        entry = {
            "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            "rows": {},
        }
        data["calibrations"][key] = entry
    # Defensive: an older/corrupt entry might be missing "rows"
    if "rows" not in entry or not isinstance(entry.get("rows"), dict):
        entry["rows"] = {}
    entry["rows"][field] = {
        "x": int(box["x"]),
        "y": int(box["y"]),
        "w": int(box["w"]),
        "h": int(box["h"]),
    }
    # ── Save-time collision guard ──
    # Warn loudly when locking a row whose y collides with an existing
    # row's y (within 4 px). Real panel rows are at least one font
    # height apart (~30-50 px); identical y means a degraded panel
    # finder fed every "Lock" click the same crop coordinates. Logging
    # alone instead of refusing the save: the user might be deliberately
    # nudging rows close together for an unusual HUD scale, and the
    # load-time validator (``to_label_rows``) is the real safety net.
    _other_ys: dict[str, int] = {
        f: int(b.get("y", -1))
        for f, b in entry["rows"].items()
        if f != field and f != "_mineral_row" and isinstance(b, dict)
    }
    _new_y = int(box["y"])
    for _other_field, _other_y in _other_ys.items():
        if abs(_other_y - _new_y) <= 4:
            log.warning(
                "calibration.save_row: SUSPECT region=%s field=%s y=%d "
                "is within 4 px of field=%s y=%d. Saved anyway, but the "
                "panel finder likely returned identical crops for "
                "multiple rows — verify in the dialog before relying "
                "on this calibration.",
                key, field, _new_y, _other_field, _other_y,
            )
    if image_size is not None:
        entry["image_size"] = [int(image_size[0]), int(image_size[1])]
    if value_column_left is not None:
        entry["value_column_left"] = int(value_column_left)
    entry["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _save_all(data)
    # Verify-after-write: re-read the file and confirm the row landed.
    # This catches silent failures from atomic-rename hiccups, antivirus
    # interference, or schema drift that cleared rows under us.
    try:
        verify = _load_all()
        v_entry = verify.get("calibrations", {}).get(key, {})
        v_rows = v_entry.get("rows", {})
        if field in v_rows:
            log.info(
                "calibration.save_row: persisted region=%s field=%s box=%s "
                "(rows now=%s)",
                key, field, entry["rows"][field], sorted(v_rows.keys()),
            )
        else:
            log.error(
                "calibration.save_row: WROTE field=%s but read-back shows "
                "rows=%s — persistence failed for region=%s",
                field, sorted(v_rows.keys()), key,
            )
    except Exception as _vexc:
        log.warning("calibration.save_row: verify-after-write failed: %s", _vexc)


def remove_row(region: dict, field: str) -> None:
    """Unlock (remove) a single row's calibration."""
    data = _load_all()
    key = _region_key(region)
    if key in data["calibrations"]:
        data["calibrations"][key].get("rows", {}).pop(field, None)
        _save_all(data)


def clear_region(region: dict) -> None:
    """Drop the entire calibration for a region."""
    data = _load_all()
    data["calibrations"].pop(_region_key(region), None)
    _save_all(data)


# ── Reserved per-region keys (v2.2.6.1) ──
# Inside ``data["calibrations"][region_key]`` we add three new keys
# under a ``$`` prefix so the existing ``FIELD_NAMES`` iteration in
# ``to_label_rows`` skips them naturally (FIELD_NAMES is a hardcoded
# tuple — anything not in it is invisible to the value-OCR loop):
#
#   $column_x_offset      : int  — global x-shift applied to every
#                                  HUD row's x_value_start. Lets the
#                                  user nudge ALL three rows (mass /
#                                  resistance / instability) at once
#                                  because they share a column. Default 0.
#   $manual_override_mode : bool — when True, the pipeline bypasses
#                                  ALL auto-detection (label_match,
#                                  scan_results_anchor, NCC, …) and
#                                  uses ONLY the user-drawn boxes
#                                  stored in $manual_overrides.
#                                  Default False.
#   $manual_overrides     : dict — field name → ``{x, y, w, h}`` dict
#                                  in HUD-region-relative coords.
#                                  Used only when
#                                  $manual_override_mode is True.
#                                  Default {}.
#
# These coexist alongside the existing per-region keys (``rows``,
# ``saved_at``, ``image_size``, ``value_column_left``) without
# collision because ``$`` is reserved here and never appears in
# FIELD_NAMES.

_KEY_COLUMN_X_OFFSET = "$column_x_offset"
_KEY_MANUAL_OVERRIDE_MODE = "$manual_override_mode"
_KEY_MANUAL_OVERRIDES = "$manual_overrides"


def _ensure_entry(data: dict, region: dict) -> dict:
    """Return the per-region entry, creating it if missing.
    Mutates ``data`` in place and returns the entry dict so the
    caller can set keys on it before calling ``_save_all``.
    """
    key = _region_key(region)
    entry = data["calibrations"].get(key)
    if entry is None:
        entry = {
            "saved_at": datetime.utcnow().isoformat(timespec="seconds"),
            "rows": {},
        }
        data["calibrations"][key] = entry
    if "rows" not in entry or not isinstance(entry.get("rows"), dict):
        entry["rows"] = {}
    return entry


def get_column_x_offset(region: dict) -> int:
    """Return the column x offset for this region (default 0).

    Applied to every HUD row's ``x_value_start`` at scan time. Use
    this to shift mass / resistance / instability value-crops left or
    right together (they share a vertical column on the SC HUD, so a
    single global delta keeps them aligned).
    """
    cal = load(region)
    if not cal:
        return 0
    try:
        return int(cal.get(_KEY_COLUMN_X_OFFSET, 0) or 0)
    except (TypeError, ValueError):
        return 0


def set_column_x_offset(region: dict, val: int) -> None:
    """Persist the column x offset for this region.

    See ``get_column_x_offset`` for semantics.
    """
    data = _load_all()
    # _load_all may return the cached dict by reference — copy before
    # mutating so we don't poison the cache for parallel readers.
    import copy as _copy
    data = _copy.deepcopy(data)
    entry = _ensure_entry(data, region)
    try:
        entry[_KEY_COLUMN_X_OFFSET] = int(val)
    except (TypeError, ValueError):
        log.warning("set_column_x_offset: bad value %r — ignored", val)
        return
    entry["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _save_all(data)
    log.info(
        "calibration: column_x_offset=%d region=%s",
        int(val), _region_key(region),
    )


def get_manual_override_mode(region: dict) -> bool:
    """Return whether this region is in manual-override mode (default False).

    When True, the OCR pipeline skips ALL auto-detection (NCC label
    matching, scan-results anchor, signal-icon anchor) and uses only
    the user's manually-drawn boxes from ``$manual_overrides``.
    """
    cal = load(region)
    if not cal:
        return False
    return bool(cal.get(_KEY_MANUAL_OVERRIDE_MODE, False))


def set_manual_override_mode(region: dict, val: bool) -> None:
    """Toggle manual-override mode for this region.

    See ``get_manual_override_mode`` for semantics.
    """
    data = _load_all()
    import copy as _copy
    data = _copy.deepcopy(data)
    entry = _ensure_entry(data, region)
    entry[_KEY_MANUAL_OVERRIDE_MODE] = bool(val)
    entry["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _save_all(data)
    log.info(
        "calibration: manual_override_mode=%s region=%s",
        bool(val), _region_key(region),
    )


def get_manual_override_box(region: dict, field: str) -> Optional[dict]:
    """Return the manual rectangle for ``field`` in override mode, or None.

    Box shape is ``{"x": int, "y": int, "w": int, "h": int}`` in
    HUD-region-relative coords (same convention as the saved
    ``rows`` boxes). Returns None when the field has no manual
    override stored.
    """
    cal = load(region)
    if not cal:
        return None
    overrides = cal.get(_KEY_MANUAL_OVERRIDES) or {}
    if not isinstance(overrides, dict):
        return None
    box = overrides.get(field)
    if not isinstance(box, dict):
        return None
    # Defensive copy so callers can't mutate the cached dict.
    try:
        return {
            "x": int(box["x"]),
            "y": int(box["y"]),
            "w": int(box["w"]),
            "h": int(box["h"]),
        }
    except (KeyError, TypeError, ValueError):
        return None


def set_manual_override_box(
    region: dict, field: str, box: dict,
) -> None:
    """Store a manual rectangle for ``field``.

    ``box`` is ``{"x": int, "y": int, "w": int, "h": int}`` in
    HUD-region-relative coords. Used only when
    ``$manual_override_mode`` is True for this region.

    Unlike ``save_row``, this DOES NOT validate ``field`` against
    ``FIELD_NAMES`` — manual overrides intentionally accept any
    string the UI passes, including the HUD's
    ``mass`` / ``resistance`` / ``instability`` and the signal-region's
    ``signature``. The pipeline only consumes the field names it
    knows about.
    """
    data = _load_all()
    import copy as _copy
    data = _copy.deepcopy(data)
    entry = _ensure_entry(data, region)
    overrides = entry.get(_KEY_MANUAL_OVERRIDES)
    if not isinstance(overrides, dict):
        overrides = {}
        entry[_KEY_MANUAL_OVERRIDES] = overrides
    try:
        overrides[field] = {
            "x": int(box["x"]),
            "y": int(box["y"]),
            "w": int(box["w"]),
            "h": int(box["h"]),
        }
    except (KeyError, TypeError, ValueError) as exc:
        log.warning(
            "set_manual_override_box: bad box %r for field=%s (%s) — ignored",
            box, field, exc,
        )
        return
    entry["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _save_all(data)
    log.info(
        "calibration: manual_override_box field=%s box=%s region=%s",
        field, overrides[field], _region_key(region),
    )


def clear_manual_overrides(region: dict) -> None:
    """Wipe all manual overrides for this region (returns to auto-detect).

    Removes BOTH the per-field box dict and the override-mode flag
    so the pipeline reverts cleanly to its auto-detect behaviour.
    """
    data = _load_all()
    import copy as _copy
    data = _copy.deepcopy(data)
    key = _region_key(region)
    entry = data["calibrations"].get(key)
    if entry is None:
        return
    entry.pop(_KEY_MANUAL_OVERRIDES, None)
    entry.pop(_KEY_MANUAL_OVERRIDE_MODE, None)
    entry["saved_at"] = datetime.utcnow().isoformat(timespec="seconds")
    _save_all(data)
    log.info(
        "calibration: cleared manual overrides region=%s", key,
    )


def to_label_rows(
    region: dict, image_w: int, image_h: int,
    img: "Optional[object]" = None,
) -> Optional[dict[str, tuple[int, int, int]]]:
    """Convert saved calibration into the standard ``_find_label_rows``
    return shape. Returns None if calibration is incomplete.

    The standard shape is::
        {field: (y1, y2, value_column_left)}

    IMPORTANT: only the y bounds from the saved boxes matter —
    they pin down which row each field is. The x bounds (label_right
    / value_column_left) are AUTO-DETECTED at runtime by scanning the
    row's actual pixel content for the label/value separator. This
    way the user only has to get the row Y position right; the
    horizontal x position works regardless of how they happened to
    drag the box.

    If ``img`` is provided we scan it for the colon position. If not,
    we fall back to the saved value_column_left or a heuristic.
    """
    cal = load(region)
    if not cal:
        return None
    rows = cal.get("rows", {})
    if not all(field in rows for field in ("mass", "resistance", "instability")):
        return None

    # ── Sanity-check the saved layout ──
    # Calibration files written by older builds (or by Lock-clicks
    # against a degraded panel finder) sometimes contain garbage where
    # every row got the same y, or where the rows are out of order
    # (mass below resistance, etc.). Returning that data poisons
    # ``_find_label_rows`` because it short-circuits all detection.
    # Reject obviously-broken layouts so the runtime falls through to
    # SCAN RESULTS anchor + NCC instead. The user can re-lock once
    # those produce sensible row bands.
    try:
        _y_mass = int(rows["mass"]["y"])
        _y_res  = int(rows["resistance"]["y"])
        _y_inst = int(rows["instability"]["y"])
        # Pitch must be positive AND non-trivial (≥6 px) between every
        # adjacent pair. A pitch of 0 means rows collide; a negative
        # pitch means the file claims instability is above mass.
        _pitch_mr = _y_res - _y_mass
        _pitch_ri = _y_inst - _y_res
        if _pitch_mr < 6 or _pitch_ri < 6:
            log.warning(
                "calibration.to_label_rows: rejecting corrupt layout for "
                "region=%s — y_mass=%d y_resistance=%d y_instability=%d "
                "(pitch_mr=%d pitch_ri=%d). Falling through to detection. "
                "Re-lock rows in the Calibration dialog to repair.",
                _region_key(region), _y_mass, _y_res, _y_inst,
                _pitch_mr, _pitch_ri,
            )
            return None
    except (KeyError, TypeError, ValueError) as _exc:
        log.warning(
            "calibration.to_label_rows: malformed rows for region=%s "
            "(%s) — falling through to detection.",
            _region_key(region), _exc,
        )
        return None

    # Auto-detect value_column_left by scanning rows for the
    # rightmost label-text column (just past the colon). This is
    # MUCH more robust than trusting the user's box x-coords —
    # BUT only when the gap walk stops cleanly at the colon. On
    # tight HUD layouts where the post-colon gap is < _INTRA_LABEL_GAP
    # the walk bridges into the value's leading digit, placing
    # ``value_column_left`` AFTER the leading "1". Clamp the auto-
    # detect result so it can refine LEFT of the user's box (good)
    # but never lands RIGHT of it (bad). The user told us where the
    # value lives by drawing those boxes — that's the hard cap.
    value_column_left: Optional[int] = None
    if img is not None:
        try:
            value_column_left = _auto_detect_value_column_left(
                img, rows, image_w,
            )
        except Exception:
            pass
    user_min_x: Optional[int] = None
    try:
        user_min_x = min(
            int(rows[f]["x"])
            for f in ("mass", "resistance", "instability")
            if f in rows
        )
    except (KeyError, TypeError, ValueError):
        user_min_x = None
    # Subtract _CALLER_MARGIN so that callers' ``x_min = lr + 6`` lands
    # EXACTLY at the user's box left edge — not 6 px inside it. The
    # user dragged the box to where the value starts; the OCR crop
    # should start there too, not past the leading digit.
    _CALLER_MARGIN = 6
    user_lr_clamp: Optional[int] = (
        max(0, user_min_x - _CALLER_MARGIN) if user_min_x is not None else None
    )
    if user_lr_clamp is not None:
        # Trust the user's calibration. The auto-detect was a
        # historical refinement attempt, but in practice it has TWO
        # failure modes that both produce visibly broken crops:
        #   * auto > user_lr_clamp → gap-walk over-shot into the
        #     value's leading digit (chops "1" off "16007").
        #   * auto < user_lr_clamp → cluster scan coalesced label
        #     and value, so the rightmost cluster's left edge is
        #     the LABEL's left edge (sweeps the colon into the
        #     value crop, the OCR misclassifies it as a digit).
        # The user drew that box specifically to mark where the
        # value lives. There's no win from refining away from it.
        value_column_left = user_lr_clamp
    if value_column_left is None:
        # Auto-detect failed — prefer the user's calibrated box X
        # (guaranteed before the leading digit by definition: it's
        # what the user dragged in the Calibration dialog) over the
        # legacy saved value_column_left, which was written by older
        # algorithms and may itself be past the leading digit.
        if user_lr_clamp is not None:
            value_column_left = user_lr_clamp
        else:
            value_column_left = cal.get("value_column_left")
    if value_column_left is None:
        # Last resort: use right edge of widest label box
        value_column_left = max(
            rows[f]["x"] + rows[f]["w"]
            for f in ("mass", "resistance", "instability")
            if f in rows
        )

    result: dict[str, tuple[int, int, int]] = {}
    for field in FIELD_NAMES:
        # ``signature`` is the signal-scanner digit-cluster crop, NOT a
        # HUD label row. It's stored under the ocr_region key (not the
        # hud_region key), but defend against the degenerate case where
        # both regions point at the same coordinates: silently dropping
        # it here keeps the HUD value-OCR loop from ever seeing it.
        if field == "signature":
            continue
        # ``needle`` (difficulty bar) is HUD-region-keyed but NOT a
        # value-OCR row — the difficulty detector in
        # ``api.scan_hud_onnx`` reads ``get_row(region, "needle")``
        # directly. Drop it here so the HUD value-OCR loop never sees it.
        if field == "needle":
            continue
        if field not in rows:
            continue
        b = rows[field]
        y1 = max(0, int(b["y"]))
        y2 = min(image_h, int(b["y"] + b["h"]))
        if y2 - y1 < 4:
            continue
        result[field] = (y1, y2, int(value_column_left))
    return result


def _auto_detect_value_column_left(
    img, rows: dict, image_w: int,
) -> Optional[int]:
    """Find the value column's left edge by scanning each row's
    RIGHTMOST text cluster.

    SC HUD invariant: in every value row, the value digits are the
    RIGHTMOST text. Labels are always to the LEFT of values, never
    to the right. Anchoring on the right side of the row sidesteps
    every fragile aspect of the previous label-end detection
    (gap-walk threshold, density floors at the colon, intra-label
    letter spacing) — we don't need to know where the label ENDS,
    only where the rightmost text cluster BEGINS.

    Per-row algorithm:
      1. Build a column-density mask over the row strip with Otsu
         + 25 %-of-row-height density floor.
      2. Find every contiguous run of "hot" columns.
      3. Coalesce runs separated by ≤ ``_COALESCE_GAP`` (bridges
         intra-digit and decimal-point gaps in multi-character
         values without bridging the larger label-to-value gap).
      4. Rightmost coalesced run is the value cluster.
      5. Subtract ``_VALUE_MARGIN`` so the caller's ``lr + 6`` lands
         exactly at the cluster's leftmost detected pixel.

    Returns ``min(per_row_lefts)`` so the shared ``value_column_left``
    is safe for whichever row's value sits leftmost. Returns None
    only when no row produces a cluster (typically a fully-empty
    panel).

    On TIGHT HUDs where the post-colon gap is < ``_COALESCE_GAP``,
    the label and value coalesce into one cluster and the result
    is too far left. ``to_label_rows`` clamps against the
    user-calibrated box X to catch that case.
    """
    try:
        import numpy as _np
    except ImportError:
        return None
    try:
        from . import frame_context as _fc
        # Max-of-channels so colored text registers as bright on both
        # light- and dark-background HUDs — the shared per-frame percept.
        detect = _fc.max_channel(img)
    except Exception:
        return None

    # Coalesce runs separated by ≤ this many px. Bridges intra-digit
    # gaps in multi-character values (3-6 px between SC digits, plus
    # decimal points up to ~10 px) without bridging the typical
    # 12-15 px label-to-value gap. Matches the same constant in
    # ``onnx_hud_reader._value_left_for_row``. Tight HUDs where the
    # post-colon gap is ≤ 12 px land in the clamp branch of
    # ``to_label_rows``.
    _COALESCE_GAP = 12
    # Caller adds +6 to land at x_min, so subtracting 6 here makes
    # x_min = (cluster_left - 6) + 6 = cluster_left exactly.
    _VALUE_MARGIN = 6

    value_lefts: list[int] = []
    for field in ("mass", "resistance", "instability"):
        if field not in rows:
            continue
        b = rows[field]
        y1 = max(0, int(b["y"]))
        y2 = min(detect.shape[0], int(b["y"] + b["h"]))
        if y2 - y1 < 4:
            continue
        # Scan the FULL row width — the value can sit anywhere up
        # to the right edge.
        region = detect[y1:y2, :]
        if region.size == 0:
            continue
        thr = _otsu_uint8(region)
        bright = int((region > thr).sum())
        if (region.size - bright) < bright:
            region = (255 - region).astype(_np.uint8)
        thr2 = _otsu_uint8(region)
        col_d = (region > thr2).sum(axis=0)
        floor = max(3, int((y2 - y1) * 0.25))
        hot = col_d >= floor
        if not hot.any():
            continue
        # Find every contiguous run of hot columns.
        runs: list[tuple[int, int]] = []
        in_run = False
        rs = 0
        W_row = hot.size
        for x in range(W_row):
            if hot[x] and not in_run:
                in_run = True
                rs = x
            elif not hot[x] and in_run:
                in_run = False
                runs.append((rs, x))
        if in_run:
            runs.append((rs, W_row))
        if not runs:
            continue
        # Coalesce nearby runs into glyph clusters.
        coalesced: list[tuple[int, int]] = [runs[0]]
        for cur_rs, cur_re in runs[1:]:
            prev_rs, prev_re = coalesced[-1]
            if (cur_rs - prev_re) <= _COALESCE_GAP:
                coalesced[-1] = (prev_rs, cur_re)
            else:
                coalesced.append((cur_rs, cur_re))
        # Rightmost cluster = value (HUD invariant).
        value_left = int(coalesced[-1][0])
        value_lefts.append(max(0, value_left - _VALUE_MARGIN))
    if not value_lefts:
        return None
    # MIN across rows so the leftmost value's leading digit is safe.
    return min(value_lefts)


def _otsu_uint8(arr) -> int:
    import numpy as _np
    hist, _ = _np.histogram(arr.flatten(), bins=256, range=(0, 256))
    total = arr.size
    sum_total = _np.sum(_np.arange(256) * hist)
    sum_bg, w_bg = 0.0, 0
    max_var, threshold = 0.0, 127
    for t in range(256):
        w_bg += int(hist[t])
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * int(hist[t])
        m_bg = sum_bg / w_bg
        m_fg = (sum_total - sum_bg) / w_fg
        var = w_bg * w_fg * (m_bg - m_fg) ** 2
        if var > max_var:
            max_var = var
            threshold = t
    return int(threshold)


# ── First-launch tracking ──
# Separate from per-region calibration since it's a global "do you
# want the welcome popup?" preference.

_FIRSTLAUNCH_PATH = CALIBRATION_DIR / "_calibration_prompt_dismissed.flag"


def is_first_launch_prompt_dismissed() -> bool:
    return _FIRSTLAUNCH_PATH.is_file()


def dismiss_first_launch_prompt() -> None:
    try:
        CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        _FIRSTLAUNCH_PATH.write_text("dismissed", encoding="utf-8")
    except Exception as exc:
        log.warning("could not write first-launch flag: %s", exc)


def reset_first_launch_prompt() -> None:
    """For testing: re-enable the popup."""
    try:
        _FIRSTLAUNCH_PATH.unlink(missing_ok=True)
    except Exception:
        pass
