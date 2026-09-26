"""Single rigid-pose authority for the HUD panel (the welded skeleton).

The HUD is a RIGID template: SCAN RESULTS, the mineral name, and the
MASS / RESISTANCE / INSTABILITY rows all sit at FIXED offsets from one
panel origin, scaled by one factor. Knowing where any two anchors are
determines where everything is. Yet historically each detector
searched for itself independently, so a dust-matched title, a wandering
mineral band, or a jumping value crop could each be wrong on its own —
the heart ending up in the foot.

This module welds them. Each scan, ALL anchor observations (title +
the three labels) feed ONE least-squares pose solve
(``hud_tracker.rigid_body.solve_panel_pose``); a high-residual anchor
(the dust title, the off mineral) is an OUTLIER and is rejected, then
the pose is re-solved from the survivors; and EVERY box is then derived
as ``origin + scale * template_offset``. The disagreements become
structurally impossible: you cannot have the title right and the
mineral wrong when the mineral is a fixed offset below the title.

Proven on 66 hand-annotated panels (2026-06-13): one pose places every
row CENTER within ~1px median / <=5px on 92-94% of panels — tighter
and far more robust than the independent detectors it replaces. The
constants below are calibrated against those annotations.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Anchor template offsets (dy in title-height units from the panel
# origin = SCAN RESULTS top-left). dx is ~0 (the HUD left-aligns).
# These are the label TOP-LEFT offsets the solver fits against.
ANCHOR_OFFSETS: dict[str, tuple[float, float]] = {
    "scan_results":      (0.0, 0.0),
    "label_mass":        (0.0, 3.33),
    "label_resistance":  (0.0, 4.98),
    "label_instability": (0.0, 6.64),
}

# Row-CENTER multipliers (title-h units, origin->row center), CALIBRATED
# on the annotated set (med row-center error <=0.3px after these).
ROW_CENTER_MULTS: dict[str, float] = {
    "_mineral_row": 2.60,   # estimate (between title and mass; no clean GT)
    "mass":         4.18,
    "resistance":   5.62,
    "instability":  7.06,
}

# Row band half-height as a fraction of scale (title height). The HUD
# value text is ~0.55x the title height; a generous half-band.
_ROW_HALF_H_MULT = 0.62

# Outlier rejection: an anchor whose residual exceeds max(_RES_FLOOR,
# _RES_MULT x median) does not fit the rigid body and is dropped.
_RES_FLOOR = 8.0
_RES_MULT = 3.0

# ── Temporal stabilization (the spine IN TIME) ──────────────────────
# A fresh least-squares solve every frame jitters as much as its inputs:
# all four anchors share x-offset 0, so the pose origin is the MEAN of
# four independent NCC matches, each with a few px of noise — on a
# perfectly stationary HUD the origin still danced ~19px frame-to-frame
# (measured 2026-06-13: x 48..67). The skeleton was rigid within a
# frame but had no memory across frames. So we HOLD the pose: when a new
# raw solve lands within a deadband of the last, the panel is stationary
# and the difference is pure noise — freeze the registration exactly
# (zero jitter). A medium move drifts via EMA; a large jump (panel moved
# / re-acquired) is hard-accepted. This is gated on a LIVE-FRAME
# heartbeat (``note_live_frame``) emitted only by the continuous capture
# loop, NEVER by the offline harness — so back-to-back unrelated stills
# can't alias one another's pose (the gates stay byte-identical).
_HOLD_DEADBAND = 9.0    # px: within this of the held pose ⇒ freeze
_HOLD_EMA = 0.4         # blend factor for medium (drift) moves
_HOLD_TELEPORT = 60.0   # px: beyond this ⇒ re-acquire (hard accept)
# Rigidity guard (bounded velocity, plausible scale): the HUD panel does
# NOT resize frame-to-frame. A new solve whose scale is outside this band
# relative to the HELD scale is a misdetection (labels latched onto
# wrong-sized text), NOT the panel moving — accepting it collapses the
# skeleton to a garbage body. Reject and hold (live 2026-06-13: a
# scale-24 @ x=217 solve teleport-accepted over a held scale-61 panel and
# the whole body fell apart). Band is generous enough for real scale
# noise (~±6%) but rejects 1.5×/2×/0.4× garbage.
_SCALE_LO = 0.65
_SCALE_HI = 1.55
# Scale LOCK (the bone length). Scale is set by label spacing; the title
# (offset 0,0) only anchors the origin, so mis-matched labels can resize
# the whole skeleton (live 2026-06-13: it tumbled 61→41→27→10 — a laundry
# machine, not a skeleton). So once two consecutive live solves AGREE on a
# scale, LOCK it and pin every later solve to that size (translation-only,
# via solve_panel_pose(fixed_scale=...)). Garbage frames then can't resize
# the body — they only jostle position (absorbed by the hold) and, if
# their anchors don't fit the locked size, are rejected outright. The lock
# persists across brief occlusions; sustained agreement on a NEW size
# (resolution change) re-locks. Live-only — the harness never locks.
_SCALE_LOCK_TOL = 0.10   # two scales within 10% ⇒ consistent
_SCALE_LOCK_TTL = 60.0   # lock survives this long without a confirming fit
_SCALE_FIT_FRAC = 0.35   # locked solve fits if median residual <=
#                          max(_RES_FLOOR, this × locked_scale). Generous
#                          enough to absorb the ~5% frame-to-frame jitter
#                          in label spacing (which is ~20px of misfit on
#                          the bottom row) so a stationary panel HOLDS
#                          instead of rejecting every noisy frame; still
#                          rejects gross wrong-scale garbage (>~15% off).
_SCALE_RELOCK_MIN = 0.20  # only re-lock to a NEW size if it is at least
#                           this fraction away from the current lock — a
#                           real resize (resolution change), not the few-%
#                           jitter that was spuriously re-locking 61→64.
_LOCKED_SCALE: "Optional[float]" = None
_LOCKED_SCALE_TS: float = 0.0
_CAND_SCALE: "Optional[float]" = None
# A rejected frame (anchors don't fit the locked size) is usually a single
# noisy/occluded frame on a panel that is STILL THERE — so hold position
# through it and KEEP the hold alive (refresh the timer), up to this many
# in a row. Without this, each reject let the hold timer lapse and the
# body re-acquired from scratch every other frame (the fresh/reject churn
# that reads as "flying into pieces"). After the cap the panel is treated
# as genuinely gone and the position re-acquires.
_MAX_HELD_REJECTS = 6
_REJECT_RUN: int = 0
# ── Predictive (aimbot) position tracking ──────────────────────────
# A pure deadband freeze is rigid on a STATIONARY panel but LAGS a moving
# one (the panel slides out from under the frozen box — measured 8-12px
# off, 18% on-target at 2px/frame). So estimate the panel's velocity from
# the smoothed measurement trend and PREDICT where it is each frame: a
# steadily-moving panel is followed (no lag), while pure zero-mean NCC
# jitter averages to ~0 velocity and is frozen (rigid). This is the
# "predict where it's going and snap there" the user asked for.
_VEL_DECAY = 0.45       # velocity EMA: higher = smoother but slower to turn
_AB_STATIONARY = 0.45   # |vel| below this (px/frame) ⇒ treat as still, freeze
_AB_TRACK = 0.75        # while MOVING, correction gain toward the
#                         measurement (on top of the velocity prediction)
#                         so the box catches up instead of lagging behind.
_VEL_X: float = 0.0
_VEL_Y: float = 0.0
_PREV_MX: "Optional[float]" = None
_PREV_MY: "Optional[float]" = None
# Heartbeat-recency window. CRITICAL SIZING (cost a live recording to
# learn, 2026-06-13): the beat fires at the TOP of api.scan_hud_onnx but
# the pose solve runs deep inside the same scan — and a full scan takes
# ~4 s (Tesseract + models), so the beat is already >2 s old by the time
# solve() checks it. A 2 s window therefore marked EVERY live solve
# "stale" and the hold never engaged (output followed raw NCC noise,
# exactly the jitter the user kept seeing). Must comfortably exceed one
# scan's duration. The harness is excluded by `_LIVE_TS > 0` regardless,
# so a generous window is free.
_LIVE_GATE_SEC = 12.0
# Last-pose recency must BRIDGE the live scan cadence (~3-5 s/scan, plus
# the occasional slow scan or brief freeze) so consecutive live scans of
# an unmoved panel hold instead of looking "stale". Too long risks
# holding across a genuinely vanished panel — but a panel re-acquired
# elsewhere trips the teleport branch anyway, so 15 s is safe.
_HOLD_TTL_SEC = 15.0

# Last solved pose — the nervous system (scan records) reads this so the
# body's single rigid-body decision is visible per scan.
_LAST_POSE: "Optional[dict]" = None
_LAST_POSE_TS: float = 0.0
_LIVE_TS: float = 0.0


def note_live_frame() -> None:
    """Heartbeat from the LIVE capture loop — call once per real frame.

    Temporal pose holding only engages within ``_LIVE_GATE_SEC`` of this
    call. The offline harness drives ``solve`` directly and never beats
    this heartbeat, so it always gets a fresh stateless solve (no
    cross-still pose aliasing); only the continuous live stream holds.
    """
    global _LIVE_TS
    import time
    _LIVE_TS = time.monotonic()


def last_pose() -> "Optional[dict]":
    """Most recent solved pose, or None."""
    return _LAST_POSE


def locked_scale() -> "Optional[float]":
    """The currently LOCKED bone-length scale, or None if not locked.

    Exposed so scan records / overlays can show whether the scale lock is
    actually engaged live — its mere PRESENCE in a record also proves the
    running app loaded the scale-lock build (older builds lack this)."""
    return _LOCKED_SCALE


def solve(
    title: "Optional[dict]",
    labels: "Optional[dict]",
) -> "Optional[dict]":
    """Solve ONE panel pose from the available anchors.

    ``title``  = the scan-results anchor dict (title_x/title_y/...), or
                 None if the title finder produced nothing.
    ``labels`` = ``find_label_positions`` result: ``{field: {x,y,w,h,
                 score}}`` for mass/resistance/instability.

    Returns ``{"x", "y", "scale", "residuals", "anchors", "rejected"}``
    or None when fewer than two anchors are available (under-determined).
    """
    try:
        from hud_tracker.rigid_body import solve_panel_pose
    except Exception as exc:
        log.debug("panel_solve: rigid_body unavailable: %s", exc)
        return None

    meas: list[tuple[str, float, float]] = []
    if title is not None:
        meas.append((
            "scan_results",
            float(title["title_x"]), float(title["title_y"]),
        ))
    if labels:
        for fld, key in (("mass", "label_mass"),
                        ("resistance", "label_resistance"),
                        ("instability", "label_instability")):
            m = labels.get(fld)
            if m is not None:
                meas.append((key, float(m["x"]), float(m["y"])))
    if len(meas) < 2:
        return None

    res = solve_panel_pose(meas, ANCHOR_OFFSETS)
    if res is None:
        return None
    px, py, scale, residuals = res
    rejected: list[str] = []

    global _LAST_POSE, _LAST_POSE_TS, _REJECT_RUN
    global _VEL_X, _VEL_Y, _PREV_MX, _PREV_MY
    import time as _t
    _now = _t.monotonic()
    _live = _LIVE_TS > 0.0 and (_now - _LIVE_TS) <= _LIVE_GATE_SEC
    _held = (
        _LAST_POSE is not None and _live
        and (_now - _LAST_POSE_TS) <= _HOLD_TTL_SEC
    )

    # Outlier rejection: drop the anchor(s) that don't fit, re-solve.
    if residuals and len(meas) >= 3:
        rv = sorted(residuals.values())
        med = rv[len(rv) // 2]
        thr = max(_RES_FLOOR, _RES_MULT * med)
        bad = [k for k, v in residuals.items() if v > thr]
        if bad and (len(meas) - len(bad)) >= 2:
            meas2 = [m for m in meas if m[0] not in bad]
            res2 = solve_panel_pose(meas2, ANCHOR_OFFSETS)
            if res2 is not None:
                px, py, scale, residuals = res2
                rejected = bad
                log.info(
                    "panel_solve: rejected outlier anchor(s) %s "
                    "(residual>%.1f) — re-solved pose from %d anchors",
                    bad, thr, len(meas2),
                )

    if not (4.0 <= scale <= 200.0):
        return None

    # ── SCALE LOCK (the bone length) ── LIVE ONLY. Scale is set purely by
    # label spacing, so mis-matched labels resize the whole skeleton. Lock
    # the size once two live solves agree, then PIN every later solve to it
    # (translation-only) so the body can move but never tumble. The harness
    # (_live False) never locks ⇒ free scale ⇒ gates byte-identical.
    global _LOCKED_SCALE, _LOCKED_SCALE_TS, _CAND_SCALE
    _scale_rejected = False
    if _live:
        _locked = (_LOCKED_SCALE is not None
                   and (_now - _LOCKED_SCALE_TS) <= _SCALE_LOCK_TTL)
        if _locked:
            _rf = solve_panel_pose(
                meas, ANCHOR_OFFSETS, fixed_scale=_LOCKED_SCALE,
            )
            _fit_ok = False
            if _rf is not None:
                _rr = _rf[3] or {}
                _rmed = (sorted(_rr.values())[len(_rr) // 2]
                         if _rr else 0.0)
                if _rmed <= max(_RES_FLOOR,
                                _SCALE_FIT_FRAC * _LOCKED_SCALE):
                    px, py, scale = _rf[0], _rf[1], float(_LOCKED_SCALE)
                    _LOCKED_SCALE_TS = _now
                    _CAND_SCALE = None
                    _fit_ok = True
            if not _fit_ok:
                # Anchors don't fit the locked size ⇒ a noisy/occluded
                # frame, OR a genuine resize. Re-lock ONLY when two
                # consecutive frames agree on a size that is CLEARLY
                # different from the current lock (a real resolution
                # change) — never on the few-% jitter that was flipping
                # the lock 61↔64. Otherwise keep the body's size and
                # reject this frame's geometry.
                if (_CAND_SCALE is not None
                        and abs(scale - _CAND_SCALE)
                        <= _SCALE_LOCK_TOL * max(scale, _CAND_SCALE)
                        and abs(scale - _LOCKED_SCALE)
                        > _SCALE_RELOCK_MIN * _LOCKED_SCALE):
                    _LOCKED_SCALE = 0.5 * (scale + _CAND_SCALE)
                    _LOCKED_SCALE_TS = _now
                    _CAND_SCALE = None
                    scale = float(_LOCKED_SCALE)
                    log.warning("panel_solve: scale RE-LOCKED to %.1f",
                                _LOCKED_SCALE)
                else:
                    _CAND_SCALE = scale
                    scale = float(_LOCKED_SCALE)
                    _scale_rejected = True
        else:
            # Establishing: lock when two consecutive solves agree.
            if (_CAND_SCALE is not None
                    and abs(scale - _CAND_SCALE)
                    <= _SCALE_LOCK_TOL * max(scale, _CAND_SCALE)):
                _LOCKED_SCALE = 0.5 * (scale + _CAND_SCALE)
                _LOCKED_SCALE_TS = _now
                _CAND_SCALE = None
                scale = float(_LOCKED_SCALE)
                log.warning("panel_solve: scale LOCKED at %.1f",
                            _LOCKED_SCALE)
            else:
                _CAND_SCALE = scale

    # ── Temporal hold (TRANSLATION) ── freeze position on a stationary
    # panel so the body stops dancing on NCC noise. Scale is already rigid
    # (locked above); here we only hold x/y. Live-gated (see _held).
    stab = "fresh"
    if _held:
        _hx, _hy = _LAST_POSE["x"], _LAST_POSE["y"]
        if _scale_rejected:
            # The body's geometry didn't fit its locked size this frame —
            # don't move it onto garbage; hold position AND keep the hold
            # alive through a bounded run of noisy frames, so a stationary
            # panel stays rigid instead of re-acquiring every other frame.
            _REJECT_RUN += 1
            if _REJECT_RUN <= _MAX_HELD_REJECTS:
                _LAST_POSE_TS = _now
            _LAST_POSE = {**_LAST_POSE, "stab": "reject"}
            return dict(_LAST_POSE)
        _REJECT_RUN = 0
        # Velocity from the smoothed MEASUREMENT trend (not the frozen
        # output, or it could never learn it's moving).
        if _PREV_MX is not None:
            _VEL_X = _VEL_DECAY * _VEL_X + (1 - _VEL_DECAY) * (px - _PREV_MX)
            _VEL_Y = _VEL_DECAY * _VEL_Y + (1 - _VEL_DECAY) * (py - _PREV_MY)
        _PREV_MX, _PREV_MY = px, py
        _spd = (_VEL_X * _VEL_X + _VEL_Y * _VEL_Y) ** 0.5
        if _spd < _AB_STATIONARY:
            # STATIONARY: zero-mean jitter ⇒ freeze rigidly within the
            # deadband (the box does not move at all on a parked panel).
            _VEL_X = _VEL_Y = 0.0
            _dx, _dy = px - _hx, py - _hy
            _dist = (_dx * _dx + _dy * _dy) ** 0.5
            if _dist <= _HOLD_DEADBAND:
                px, py = _hx, _hy
                stab = "hold"
            elif _dist <= _HOLD_TELEPORT:
                px = _hx + _HOLD_EMA * _dx
                py = _hy + _HOLD_EMA * _dy
                stab = "ema"
            else:
                stab = "accept"
                _PREV_MX, _PREV_MY = px, py
        else:
            # MOVING: predict ahead from velocity, then CORRECT toward the
            # measurement (gain _AB_TRACK) so the box rides WITH the panel
            # and catches up any lag instead of trailing behind it.
            _predx, _predy = _hx + _VEL_X, _hy + _VEL_Y
            _dx, _dy = px - _predx, py - _predy
            _dist = (_dx * _dx + _dy * _dy) ** 0.5
            if _dist <= _HOLD_TELEPORT:
                px = _predx + _AB_TRACK * _dx
                py = _predy + _AB_TRACK * _dy
                stab = "track"
            else:
                stab = "accept"  # panel jumped / re-acquired
                _VEL_X = _VEL_Y = 0.0
                _PREV_MX, _PREV_MY = px, py

    _REJECT_RUN = 0
    if stab == "fresh":
        # re-acquired from scratch ⇒ no velocity history yet.
        _VEL_X = _VEL_Y = 0.0
        _PREV_MX, _PREV_MY = px, py
    _LAST_POSE = {
        "x": float(px), "y": float(py), "scale": float(scale),
        "residuals": dict(residuals or {}),
        "anchors": [m[0] for m in meas if m[0] not in rejected],
        "rejected": rejected,
        "stab": stab,
    }
    _LAST_POSE_TS = _now
    return dict(_LAST_POSE)


def row_band(pose: dict, field: str, img_h: int,
             label_right: int) -> "Optional[tuple[int, int, int]]":
    """Derive ONE row's ``(y1, y2, label_right)`` band purely from the
    pose. Every consumer that needs a row gets it from here, so they
    cannot disagree."""
    if field not in ROW_CENTER_MULTS:
        return None
    cy = pose["y"] + pose["scale"] * ROW_CENTER_MULTS[field]
    half = max(8.0, pose["scale"] * _ROW_HALF_H_MULT)
    y1 = max(0, int(round(cy - half)))
    y2 = min(int(img_h), int(round(cy + half)))
    if y2 - y1 < 6:
        return None
    return (y1, y2, int(label_right))


def title_box(pose: dict) -> "tuple[int, int, int, int]":
    """Derive the title box (x, y, w, h) from the pose. The title is the
    origin; its size is the scale (height) and the template aspect."""
    h = int(round(pose["scale"]))
    return (int(round(pose["x"])), int(round(pose["y"])),
            int(round(5.6429 * h)), h)
