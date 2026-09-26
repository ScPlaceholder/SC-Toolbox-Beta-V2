"""Per-scan structured records (the pipeline's sensing layer).

One JSON line per completed scan into ``debug_glyphs/scan_records.jsonl``:
timestamps, duration, the four published values, the pose state, and
which fields currently have gate bypasses revoked by the consistency
reflex. Every diagnostic question of the form "how often does X
happen / when did Y start" becomes a one-line query over this file
instead of log archaeology — and the same records feed the warm-pass
tooling and future reflexes.

Best-effort by design: any failure here is swallowed; recording must
never affect a scan. The file self-rotates at ~2 MB keeping the most
recent half.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger(__name__)

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "debug_glyphs", "scan_records.jsonl",
)
_MAX_BYTES = 2_000_000
_scan_counter = 0


def write(values: dict, ms: float) -> None:
    """Append one scan record. ``values`` carries the published
    mass/resistance/instability/mineral_name."""
    global _scan_counter
    try:
        _scan_counter += 1
        rec = {
            "n": _scan_counter,
            "ts": round(time.time(), 3),
            "ms": round(float(ms), 1),
            "mass": values.get("mass"),
            "resistance": values.get("resistance"),
            "instability": values.get("instability"),
            "mineral": values.get("mineral_name"),
        }
        try:
            from . import panel_pose as _pp
            with _pp._lock:
                rec["pose_holds"] = int(_pp._state.get("holds") or 0)
                rec["pose_set"] = _pp._state.get("rows") is not None
        except Exception:
            pass
        try:
            from . import panel_solve as _ps
            _pp = _ps.last_pose()
            if _pp:
                rec["pose"] = {
                    "x": round(_pp["x"], 1), "y": round(_pp["y"], 1),
                    "scale": round(_pp["scale"], 1),
                    "anchors": _pp.get("anchors"),
                    "rejected": _pp.get("rejected"),
                    # stab = temporal-hold verdict (hold/ema/accept/fresh):
                    # a long run of "hold" means the registration is
                    # frozen steady; "fresh" every scan means the hold is
                    # NOT engaging (e.g. heartbeat stale) — the live tell.
                    "stab": _pp.get("stab"),
                    # locked = the LOCKED bone-length scale (or None). Its
                    # presence proves the running app loaded the scale-lock
                    # build; a steady value across scans = size is rigid.
                    "locked": _ps.locked_scale(),
                }
        except Exception:
            pass
        try:
            from . import api as _api
            _rev = [
                f for f in ("mass", "resistance", "instability")
                if _api._bypass_revoked(f)
            ]
            if _rev:
                rec["bypass_revoked"] = _rev
        except Exception:
            pass
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        try:
            if os.path.getsize(_PATH) > _MAX_BYTES:
                with open(_PATH, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                with open(_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines) // 2:])
        except OSError:
            pass
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass
