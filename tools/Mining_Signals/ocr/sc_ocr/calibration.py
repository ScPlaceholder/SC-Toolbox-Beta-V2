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
FIELD_NAMES: tuple[str, ...] = (
    "_mineral_row",
    "mass",
    "resistance",
    "instability",
)

# Field names that are user-facing (drop the underscore prefix).
DISPLAY_NAMES: dict[str, str] = {
    "_mineral_row":  "Resource (Mineral)",
    "mass":          "Mass",
    "resistance":    "Resistance",
    "instability":   "Instability",
}


def _region_key(region: dict) -> str:
    """Deterministic string key for a region dict."""
    return (
        f"{int(region.get('x', 0))},"
        f"{int(region.get('y', 0))},"
        f"{int(region.get('w', 0))},"
        f"{int(region.get('h', 0))}"
    )


def _load_all() -> dict:
    """Read the whole calibration file. Returns an empty schema if
    the file doesn't exist or is corrupt."""
    if not CALIBRATION_PATH.is_file():
        return {"version": SCHEMA_VERSION, "calibrations": {}}
    try:
        data = json.loads(CALIBRATION_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not a dict")
        data.setdefault("version", SCHEMA_VERSION)
        data.setdefault("calibrations", {})
        return data
    except Exception as exc:
        log.warning("calibration.json load failed: %s — using empty schema", exc)
        return {"version": SCHEMA_VERSION, "calibrations": {}}


def _save_all(data: dict) -> None:
    """Atomic write of the calibration file (tmp + rename)."""
    try:
        CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CALIBRATION_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, CALIBRATION_PATH)
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

    # Auto-detect value_column_left by scanning rows for the
    # rightmost label-text column (just past the colon). This is
    # MUCH more robust than trusting the user's box x-coords.
    value_column_left: Optional[int] = None
    if img is not None:
        try:
            value_column_left = _auto_detect_value_column_left(
                img, rows, image_w,
            )
        except Exception:
            pass
    if value_column_left is None:
        # Fall back to saved value_column_left if it exists
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
    """For each value row in the calibration, scan its strip for the
    rightmost label-text column (the colon). Return the MAX across
    rows (= the longest label's colon position = where values
    LEFT-align in the panel).

    Returns None if scan fails for all rows.
    """
    try:
        import numpy as _np
        from PIL import Image as _PILImage
    except ImportError:
        return None
    try:
        rgb = _np.asarray(img.convert("RGB"), dtype=_np.uint8)
        # Use max-of-channels so colored text registers as bright
        detect = rgb.max(axis=2).astype(_np.uint8)
    except Exception:
        return None

    # Restrict label-text scan to the LEFT 60% of the image
    # (the value column lives in the right 40%; we don't want to
    # accidentally scan into it for the label end).
    half_w = max(1, int(image_w * 0.60))
    label_ends: list[int] = []
    for field in ("mass", "resistance", "instability"):
        if field not in rows:
            continue
        b = rows[field]
        y1 = max(0, int(b["y"]))
        y2 = min(detect.shape[0], int(b["y"] + b["h"]))
        if y2 - y1 < 4:
            continue
        region = detect[y1:y2, :half_w]
        if region.size == 0:
            continue
        # Otsu threshold
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
        idxs = _np.where(hot)[0]
        first_hot = int(idxs[0])
        last_hot = first_hot
        gap = 0
        i = first_hot
        while i < hot.size:
            if hot[i]:
                last_hot = i
                gap = 0
            else:
                gap += 1
                if gap >= 12:
                    break
            i += 1
        label_ends.append(int(last_hot) + 4)
    if not label_ends:
        return None
    return max(label_ends)


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
