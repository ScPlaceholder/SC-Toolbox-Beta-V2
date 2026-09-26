"""Jitter / motion simulator for the panel pose.

Drives the REAL panel_solve.solve() with a HUD panel whose anchors jitter
the way live recordings showed — small per-anchor NCC noise, occasional
gross mis-matches (garbage scale), brief dropouts — under a range of
MOTION profiles. The panel's true position each frame is known, so we can
score how well the output pose stays on target.

Two modes per scenario:
  BASELINE   - no live heartbeat  -> stateless free solve (tracker as it
               behaved before stabilization)
  STABILIZED - live heartbeat     -> scale lock + predictive hold (now)

Run:  python scripts/jitter_sim.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import platform  # noqa: E402
platform._wmi = None

import time as _time  # noqa: E402
from ocr.sc_ocr import panel_solve as ps  # noqa: E402

_S = [0]


def _rng():
    _S[0] = (1103515245 * _S[0] + 12345) & 0x7FFFFFFF
    return _S[0] / 0x7FFFFFFF


def _noise(amp):
    return (_rng() - 0.5) * 2.0 * amp


TRUE_X, TRUE_Y, TRUE_SCALE = 58.0, 68.0, 62.0
OFF = {"mass": 3.33, "resistance": 4.98, "instability": 6.64}


# ── motion profiles: frame index -> (x_offset, y_offset) from the origin ──
def m_still(i):
    return 0.0, 0.0


def m_slow(i):
    return 2.0 * i, 0.0


def m_fast(i):
    return 8.0 * i, 1.5 * i


def m_accel(i):
    return 0.12 * i * i, 0.0          # 0 -> speeds up to ~11px/frame


def m_stop(i):
    return (4.0 * min(i, 20), 0.0)    # drift 20 frames then FREEZE


def m_reverse(i):
    return (4.0 * i, 0.0) if i <= 16 else (4.0 * 16 - 4.0 * (i - 16), 0.0)


def m_teleport(i):
    return (0.0, 0.0) if i < 24 else (130.0, -40.0)   # jump mid-run


def reset():
    _S[0] = 12345
    ps._LAST_POSE = None
    ps._LAST_POSE_TS = 0.0
    ps._LIVE_TS = 0.0
    ps._LOCKED_SCALE = None
    ps._LOCKED_SCALE_TS = 0.0
    ps._CAND_SCALE = None
    ps._REJECT_RUN = 0
    ps._VEL_X = 0.0
    ps._VEL_Y = 0.0
    ps._PREV_MX = None
    ps._PREV_MY = None


def make_frame(i, motion, garbage_rate, dropout_rate):
    ox, oy = motion(i)
    bx, by = TRUE_X + ox, TRUE_Y + oy
    if dropout_rate and (i % dropout_rate) == (dropout_rate - 2):
        return None, None, (bx, by)
    garbage = garbage_rate and (i % garbage_rate) == (garbage_rate - 1)
    NCC = 2.0
    tx = bx + _noise(NCC)
    ty = by + _noise(NCC)
    if garbage:
        gs = TRUE_SCALE * 0.45
        labels = {f: {"x": tx + _noise(NCC), "y": ty + OFF[f] * gs,
                      "w": 80, "h": 14, "score": 0.9} for f in OFF}
    else:
        labels = {f: {"x": tx + _noise(NCC),
                      "y": by + OFF[f] * TRUE_SCALE + _noise(NCC),
                      "w": 80, "h": 14, "score": 0.9} for f in OFF}
    title = {"title_x": tx, "title_y": ty,
             "title_w": int(5.64 * TRUE_SCALE), "title_h": int(TRUE_SCALE)}
    return title, labels, (bx, by)


def run(mode, motion, n=44, garbage_rate=6, dropout_rate=11):
    reset()
    clock = [1000.0]
    real = _time.monotonic
    _time.monotonic = lambda: clock[0]
    try:
        rows = []
        for i in range(n):
            clock[0] += 3.5
            if mode == "stabilized":
                ps.note_live_frame()
            clock[0] += 1.0
            title, labels, tgt = make_frame(
                i, motion, garbage_rate, dropout_rate)
            if title is None:
                rows.append((tgt, None))
                continue
            rows.append((tgt, ps.solve(title, labels)))
        return rows
    finally:
        _time.monotonic = real


def score(rows):
    seen = [(t, p) for (t, p) in rows if p]
    errs = [((p["x"] - t[0]) ** 2 + (p["y"] - t[1]) ** 2) ** 0.5
            for t, p in seen]
    ss = [p["scale"] for _, p in seen]
    ont = sum(1 for e in errs if e <= 6)
    return {
        "n": len(seen),
        "med": sorted(errs)[len(errs) // 2] if errs else 0.0,
        "max": max(errs) if errs else 0.0,
        "sspread": (max(ss) - min(ss)) if ss else 0.0,
        "ont": round(100 * ont / max(1, len(errs))),
    }


SCENARIOS = [
    ("stationary", m_still, 6, 11),
    ("clean (no garbage/drop)", m_still, 0, 0),
    ("slow drift 2px/fr", m_slow, 6, 11),
    ("fast drift 8px/fr", m_fast, 6, 11),
    ("accelerating", m_accel, 6, 11),
    ("move-then-STOP", m_stop, 6, 11),
    ("direction REVERSE", m_reverse, 6, 11),
    ("TELEPORT mid-run", m_teleport, 6, 11),
    ("heavy garbage (1/3)", m_still, 3, 11),
    ("heavy dropout (1/4)", m_still, 6, 4),
]

if __name__ == "__main__":
    print("Jitter/motion sim — real panel_solve, ±2px NCC noise.\n"
          "on-target = output within 6px of the TRUE panel that frame.\n")
    print("%-26s %-26s  %s" % ("scenario", "BASELINE (old)",
                               "STABILIZED (now)"))
    print("-" * 86)
    for name, motion, gr, dr in SCENARIOS:
        b = score(run("baseline", motion, garbage_rate=gr, dropout_rate=dr))
        s = score(run("stabilized", motion, garbage_rate=gr, dropout_rate=dr))

        def fmt(d):
            return "%3d%% on-tgt  err%4.1f/%4.1f sc%4.1f" % (
                d["ont"], d["med"], d["max"], d["sspread"])
        print("%-26s %-26s  %s" % (name, fmt(b), fmt(s)))
    print("-" * 86)
    print("err = median/max px off target | sc = scale spread (0 = bone "
          "length rigid). Higher on-tgt + lower err/sc = better.")
