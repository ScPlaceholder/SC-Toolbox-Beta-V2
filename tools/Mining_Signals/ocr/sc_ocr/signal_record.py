"""Per-scan structured records for the SIGNATURE panel (region2 nervous
system) — the sibling of ``scan_record``.

One JSON line per accepted signature read into
``debug_glyphs/signature_records.jsonl``: the value, which tier/gate
accepted it (``source``), the pill-anchored crop box, the voted comma x
(the digit-grid anchor), and the cross-frame CONSISTENCY-REFLEX verdict
(is the value flapping?). Region2 barely jitters, so the point of this
layer is not motion telemetry — it is making signature MISREADS
diagnosable: which crop / comma / tier produced a wrong number, and
whether the value is oscillating.

Best-effort by design: any failure here is swallowed; recording must
never affect a read. Self-rotates at ~2 MB keeping the most recent half.
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque
from typing import Optional

log = logging.getLogger(__name__)

_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "debug_glyphs", "signature_records.jsonl",
)
_MAX_BYTES = 2_000_000
_sig_counter = 0

# ── Consistency reflex state ──────────────────────────────────────────
# A real rock signature is FIXED while it is on screen, so a value that
# oscillates (A→B→A) or churns through several distinct numbers in a
# short window is a misread, not a new reading. We surface that as a
# ``flap`` flag on the record (and a one-time warning) so it is greppable
# without log archaeology.
_recent: "deque[tuple[float, int]]" = deque(maxlen=8)
_FLAP_WINDOW_S = 12.0


def _flap_verdict(value: int, now: float) -> Optional[str]:
    """Classify the current value against recent history. Returns a short
    reason string when the signature is flapping, else None."""
    prev = list(_recent)
    _recent.append((now, value))
    recent = [v for (t, v) in prev if now - t <= _FLAP_WINDOW_S]
    if len(recent) >= 2:
        # A→B→A oscillation: current equals the one before last, but the
        # immediately-previous read differed.
        if recent[-1] != value and value in recent[:-1]:
            return "oscillate(%s<->%s)" % (recent[-1], value)
    distinct = set(recent + [value])
    if len(distinct) >= 3:
        return "churn(%d distinct/%ds)" % (len(distinct), int(_FLAP_WINDOW_S))
    return None


def write(value: "Optional[int]", source: str,
          crop_box: "Optional[dict]" = None,
          comma_x: "Optional[float]" = None,
          pose: "Optional[dict]" = None) -> None:
    """Append one signature record + run the consistency reflex.

    ``value``    the accepted signature integer (or None on a miss)
    ``source``   the tier/gate label that produced it
    ``crop_box`` ``{x,y,w,h}`` value crop in signal-region coords
    ``comma_x``  voted comma x (digit-grid anchor) or None
    ``pose``     optional signal_solve pill pose (for crop-vs-pose drift)
    """
    global _sig_counter
    try:
        now = time.time()
        flap = _flap_verdict(int(value), now) if value is not None else None
        _sig_counter += 1
        rec = {
            "n": _sig_counter,
            "ts": round(now, 3),
            "value": value,
            "source": source,
            "crop": crop_box,
            "comma_x": (round(float(comma_x), 1)
                        if comma_x is not None else None),
            "flap": flap,
        }
        if pose is not None:
            rec["pose"] = {
                "pill": [int(pose.get("x", 0)), int(pose.get("y", 0)),
                         int(pose.get("pill_w", 0)), int(pose.get("pill_h", 0))],
                "value_box": pose.get("value_box"),
            }
        if flap:
            log.warning("signal_record: signature FLAP value=%s (%s) — "
                        "likely misread", value, flap)
        try:
            if (os.path.exists(_PATH)
                    and os.path.getsize(_PATH) > _MAX_BYTES):
                with open(_PATH, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                with open(_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[len(lines) // 2:])
        except Exception:
            pass
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        with open(_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as exc:
        log.debug("signal_record.write swallowed: %s", exc)
