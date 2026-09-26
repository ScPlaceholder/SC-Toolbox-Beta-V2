"""Single rigid-pose authority for the SIGNATURE panel (region2 skeleton).

The sibling of ``panel_solve``, but region2 has different physics, so the
design is different (per the project owner, 2026-06-14):

  * region2 does NOT jitter much — temporal hold is secondary here.
  * what matters is that everything stays SNAPPED TOGETHER.
  * the PILL finder + the COMMA are enough to snap it consistently.

So the icon is NOT the anchor (its NCC latches onto digit circle-on-stick
silhouettes — the known failure mode). Instead:

  PILL  = the panel FRAME. ``_find_pill_for_signal`` returns the big
          stable badge box (px, py, pw, ph). The value sits at a RIGID
          offset inside it. Calibrated on the 60 annotated region2 panels
          (origin = pill top-left; dx in pill_w, dy in pill_h units):

            value dx 0.420 (spread .09)   value dy 0.263 (spread .028!)
            value w  0.527 (spread .14)   value h  0.556 (spread .11)

          dy is rock-tight, so the pill pins the value ROW precisely. The
          only loose axis is value WIDTH — and that varies only with how
          many digits the number has, which is exactly what the comma
          pins.

  COMMA = the digit-grid landmark INSIDE the value. SC signatures are
          thousands-grouped ("12,810" / "7,080" / "21,200"), so the comma
          is always 3 digits from the right. ``find_comma_voted`` locates
          it; it anchors the digit segmentation so the read aligns to the
          real grid regardless of leading-digit count.

Together: PILL snaps WHERE the value is, COMMA snaps HOW the digits align.
The value crop and the digit grid are welded to these two anchors and
cannot drift independently.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Value box offsets from the PILL top-left. dx/w are in pill-WIDTH units,
# dy/h in pill-HEIGHT units (calibrated on the annotated region2 set).
VALUE_DX = 0.420
VALUE_DY = 0.263
VALUE_W = 0.527
VALUE_H = 0.556

# Plausible pill size band (px). The pill width STRETCHES with digit count
# (annotated 36..165), so width alone is not a scale; the HEIGHT is the
# stable badge dimension and is the better scale reference.
_PILL_H_MIN = 14.0
_PILL_H_MAX = 90.0

_LAST_POSE: "Optional[dict]" = None


def last_pose() -> "Optional[dict]":
    """Most recent solved signature pose, or None."""
    return _LAST_POSE


def value_box(pose: dict) -> "tuple[int, int, int, int]":
    """Derive the signature VALUE crop (x, y, w, h) rigidly from the pill.

    Every consumer gets the digit crop from here, welded to the pill, so
    it cannot drift on its own. The comma (``pose['comma_x']``, if known)
    refines the digit-grid alignment WITHIN this crop downstream — it does
    not move the crop."""
    px, py = pose["x"], pose["y"]
    pw, ph = pose["pill_w"], pose["pill_h"]
    return (int(round(px + VALUE_DX * pw)),
            int(round(py + VALUE_DY * ph)),
            int(round(VALUE_W * pw)),
            int(round(VALUE_H * ph)))


def solve(pill: "Optional[tuple]",
          comma_x: "Optional[float]" = None) -> "Optional[dict]":
    """Solve the signature pose from the PILL (+ optional comma).

    ``pill``    = ``_find_pill_for_signal`` result ``(px, py, pw, ph)`` or
                  None.
    ``comma_x`` = the voted comma x (absolute px) from ``find_comma_voted``
                  if available — the digit-grid anchor.

    Returns ``{x, y, pill_w, pill_h, comma_x, value_box, stab}`` or None
    when the pill is missing / implausibly sized. The pill PINS the value
    crop; the comma PINS the digit alignment inside it.
    """
    if pill is None:
        return None
    px, py, pw, ph = (float(pill[0]), float(pill[1]),
                      float(pill[2]), float(pill[3]))
    if not (_PILL_H_MIN <= ph <= _PILL_H_MAX) or pw <= 0:
        log.debug("signal_solve: implausible pill h=%.1f w=%.1f — reject",
                  ph, pw)
        return None

    global _LAST_POSE
    pose = {
        "x": px, "y": py, "pill_w": pw, "pill_h": ph,
        "comma_x": (float(comma_x) if comma_x is not None else None),
        "stab": "fresh",
    }
    pose["value_box"] = value_box(pose)
    _LAST_POSE = pose
    return dict(pose)
