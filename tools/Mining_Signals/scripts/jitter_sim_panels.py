"""Run EVERY annotated region1 panel through the jitter simulator.

For each hand-annotated panel we read its true anchor geometry (the
SCAN RESULTS title box + the three label/value row boxes), solve the
panel's true pose once, then drive panel_solve.solve() with that real
geometry under jitter + garbage + dropout + motion — and measure how
well the stabilized output stays on the panel's own true pose.

This stresses the stabilizer across the REAL distribution of panel
scales and positions (small upscaled-region panels through large ones),
not the single synthetic panel in jitter_sim.py. The stabilizer uses
some ABSOLUTE constants (9px deadband, +-2px NCC noise, 0.45px/frame
stationary threshold), so a panel's scale changes how those land —
which is exactly what per-panel testing exposes.

Run:  python scripts/jitter_sim_panels.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import platform  # noqa: E402
platform._wmi = None

import time as _time  # noqa: E402
from ocr.sc_ocr import panel_solve as ps  # noqa: E402
from scripts.anchor_geometry_eval import load_annotated  # noqa: E402

_S = [0]


def _rng():
    _S[0] = (1103515245 * _S[0] + 12345) & 0x7FFFFFFF
    return _S[0] / 0x7FFFFFFF


def _noise(amp):
    return (_rng() - 0.5) * 2.0 * amp


OFF = {"mass": 3.33, "resistance": 4.98, "instability": 6.64}
ROWS = {"mass": "mass_row", "resistance": "resistance_row",
        "instability": "instability_row"}


REF_H = 541   # the live pipeline upscales captures to this height


def base_anchors(gt, up=1.0):
    """Real title + label anchor positions from a panel's GT boxes,
    scaled by ``up`` (the live REF_H upscale factor)."""
    if "scan_results" not in gt:
        return None
    if any(r not in gt for r in ROWS.values()):
        return None
    sr = gt["scan_results"]
    title = {"title_x": float(sr["x"]) * up, "title_y": float(sr["y"]) * up,
             "title_w": float(sr["w"]) * up, "title_h": float(sr["h"]) * up}
    labels = {}
    for f, key in ROWS.items():
        b = gt[key]
        labels[f] = {"x": float(b["x"]) * up, "y": float(b["y"]) * up,
                     "w": float(b["w"]) * up, "h": float(b["h"]) * up,
                     "score": 0.9}
    return title, labels


def reset():
    _S[0] = 7
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


def true_pose(title, labels):
    """Clean solve (no heartbeat) = the panel's true pose."""
    reset()
    return ps.solve(dict(title), {k: dict(v) for k, v in labels.items()})


def frame(i, title, labels, ox, oy, garbage_rate, dropout_rate):
    if dropout_rate and (i % dropout_rate) == (dropout_rate - 2):
        return None, None
    NCC = 2.0
    t = {**title,
         "title_x": title["title_x"] + ox + _noise(NCC),
         "title_y": title["title_y"] + oy + _noise(NCC)}
    if garbage_rate and (i % garbage_rate) == (garbage_rate - 1):
        # labels collapse onto a wrong-sized spacing (the tumble trigger)
        ty = title["title_y"] + oy
        sc = float(title["title_h"]) * 0.45 or 12.0
        lab = {f: {"x": title["title_x"] + ox + _noise(NCC),
                   "y": ty + OFF[f] * sc + _noise(NCC),
                   "w": 80, "h": 14, "score": 0.9} for f in OFF}
    else:
        lab = {f: {**labels[f],
                   "x": labels[f]["x"] + ox + _noise(NCC),
                   "y": labels[f]["y"] + oy + _noise(NCC)} for f in OFF}
    return t, lab


def run_panel(title, labels, tp, drift=0.0, garbage_rate=6, dropout_rate=11,
              n=40):
    """Drive the stabilizer; return (on_target_frac, scale_spread,
    median_err) measured vs the true pose tp."""
    reset()
    clock = [1000.0]
    realm = _time.monotonic
    _time.monotonic = lambda: clock[0]
    try:
        errs, scales, ont = [], [], 0
        tol = max(6.0, 0.10 * tp["scale"])
        for i in range(n):
            clock[0] += 3.5
            ps.note_live_frame()
            clock[0] += 1.0
            ox, oy = drift * i, drift * 0.4 * i
            t, lab = frame(i, title, labels, ox, oy,
                           garbage_rate, dropout_rate)
            if t is None:
                continue
            p = ps.solve(t, lab)
            if not p:
                continue
            # true position THIS frame (true pose + the applied motion)
            bx, by = tp["x"] + ox, tp["y"] + oy
            e = ((p["x"] - bx) ** 2 + (p["y"] - by) ** 2) ** 0.5
            errs.append(e)
            scales.append(p["scale"])
            if e <= tol and abs(p["scale"] - tp["scale"]) <= tol:
                ont += 1
        if not errs:
            return None
        return (ont / len(errs),
                max(scales) - min(scales),
                sorted(errs)[len(errs) // 2])
    finally:
        _time.monotonic = realm


SCENARIOS = [("stationary", 0.0), ("slow drift", 2.0), ("fast drift", 8.0)]


LIVE_SCALE = 55.0   # the pose-scale the live upscaled pipeline operates at


def _load_usable(upscale_to_live):
    panels = load_annotated("region1")
    usable, skipped = [], 0
    for png, gt in panels:
        ba = base_anchors(gt, up=1.0)
        if ba is None:
            skipped += 1
            continue
        title, labels = ba
        tp = true_pose(title, labels)
        if not tp or not (4 <= tp["scale"] <= 400):
            skipped += 1
            continue
        if upscale_to_live and tp["scale"] > 0:
            # Re-scale the panel geometry so its pose-scale matches the
            # live REF_H-upscaled regime (~55), while noise stays +-2px
            # absolute — exactly the live condition (noise is a smaller
            # fraction of a bigger panel).
            up = LIVE_SCALE / tp["scale"]
            ba = base_anchors(gt, up=up)
            title, labels = ba
            tp = true_pose(title, labels)
            if not tp:
                skipped += 1
                continue
        res = tp.get("residuals") or {}
        med_res = (sorted(res.values())[len(res) // 2] if res else 0.0)
        rel_res = med_res / tp["scale"] if tp["scale"] else 9.9
        usable.append((os.path.basename(png), title, labels, tp, rel_res))
    return usable, skipped


RES_OUTLIER = 0.25  # clean-solve median residual / scale above this = bad
#                     geometry (the rigid template doesn't fit the boxes)


def _report(usable, skipped):
    scales = sorted(t[3]["scale"] for t in usable)
    clean = [u for u in usable if u[4] <= RES_OUTLIER]
    bad = [u for u in usable if u[4] > RES_OUTLIER]
    print("  %d usable (%d skipped); pose-scale %.0f..%.0f (median %.0f); "
          "%d well-formed + %d geometry-outlier (clean-solve residual >%.0f%% "
          "of scale, same panels the geom harness flags)" % (
              len(usable), skipped, scales[0], scales[-1],
              scales[len(scales) // 2], len(clean), len(bad),
              RES_OUTLIER * 100))
    print("  %-12s %18s   %18s" % ("scenario",
                                   "WELL-FORMED panels", "geometry-outliers"))
    for sname, drift in SCENARIOS:
        def measure(group):
            onts = []
            for name, title, labels, tp, _mr in group:
                r = run_panel(title, labels, tp, drift=drift)
                if r:
                    onts.append(r[0])
            if not onts:
                return "n/a"
            perfect = sum(1 for o in onts if o >= 0.999)
            return "%5.1f%% (%d/%d perfect)" % (
                100 * sum(onts) / len(onts), perfect, len(onts))
        print("  %-12s %18s   %18s" % (
            sname, measure(clean), measure(bad)))


def main():
    print("Every annotated region1 panel -> jitter sim (+/-2px NCC noise + "
          "1/6 garbage + 1/11 dropout).")
    print("on-tgt = mean over panels of frames within max(6px, 10%% scale) "
          "of that panel's TRUE pose.\n")
    print("[LIVE REGIME] geometry upscaled to REF_H=541 like the real "
          "pipeline (what actually runs):")
    u, s = _load_usable(upscale_to_live=True)
    _report(u, s)
    print("\n[NATIVE] raw annotation scale (smaller = harsher: +/-2px noise "
          "is a bigger fraction):")
    u, s = _load_usable(upscale_to_live=False)
    _report(u, s)


if __name__ == "__main__":
    main()
