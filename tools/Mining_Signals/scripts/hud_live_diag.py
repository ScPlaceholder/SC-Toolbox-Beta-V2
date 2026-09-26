"""HUD finder stability diagnostic — live vs frozen, per frame.

Captures the HUD region (or replays a recording) for ~N seconds, runs
the real OCR pipeline on every frame, and records WHERE THE FINDER PUT
THE PANEL each frame (title anchor + the three row bands) alongside the
LIVE per-scan values and the FROZEN published values. The output is a
per-frame table plus a JUMP SUMMARY that quantifies how much the finder
moves frame-to-frame — so "the HUD finder jumps all over the place" is
a number and a picture, not a vibe.

Why this exists: geometry can be ~94% correct on still annotated panels
yet still visibly jump live, because live adds frame-to-frame anchor
re-detection, tracker accept/reject decisions, and live-vs-frozen
divergence that a single-still harness cannot see. This tool measures
exactly that gap.

USAGE
  Live capture (the real use — run while the game HUD is visible):
    python scripts/hud_live_diag.py --region X,Y,W,H [--seconds 10] [--hz 4]
  Read the region from your debug log line:
    "_find_label_rows: entry ... region={'x': 1311, 'y': 705, 'w': 507, 'h': 320}"
    -> --region 1311,705,507,320

  Replay a recording (reproducible; what this was tested with):
    python scripts/hud_live_diag.py --frames panel_finder_recording/20260612_190903

  Self-test (no capture, formatting only):
    python scripts/hud_live_diag.py --selftest

Writes the report to %TEMP%\\hud_live_diag_report.txt and prints it.
Paste that file back for analysis.
"""
import platform
platform._wmi = None  # noqa: E402 (Py3.14 WMI import-hang guard)

import argparse
import glob
import io
import contextlib
import os
import sys
import time

_TOOL = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(_TOOL)
sys.path.insert(0, _TOOL)
sys.path.append(os.path.join(_TOOL, "..", ".."))

import logging
logging.basicConfig(level=logging.CRITICAL)

# Output to a stable, findable location in the tool's own folder — not
# %TEMP% (which sent users hunting). Created if missing.
_OUTDIR = os.path.join(_TOOL, "debug_glyphs")
os.makedirs(_OUTDIR, exist_ok=True)
REPORT = os.path.join(_OUTDIR, "hud_live_diag_report.txt")
MOSAIC = os.path.join(_OUTDIR, "hud_live_diag_mosaic.png")
SCATTER = os.path.join(_OUTDIR, "hud_live_diag_scatter.png")


def _fmt(v):
    if v is None:
        return "-"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".") if v != int(v) else str(int(v))
    return str(v)


def _capture_frames_live(region, seconds, hz):
    """Yield PIL frames from the live screen region for `seconds`."""
    from PIL import ImageGrab
    bbox = (region["x"], region["y"],
            region["x"] + region["w"], region["y"] + region["h"])
    period = 1.0 / max(1, hz)
    t_end = time.monotonic() + seconds
    while time.monotonic() < t_end:
        t0 = time.monotonic()
        img = ImageGrab.grab(bbox=bbox, all_screens=True).convert("RGB")
        yield img
        dt = period - (time.monotonic() - t0)
        if dt > 0:
            time.sleep(dt)


def _capture_frames_replay(folder, seconds, hz):
    """Yield PIL frames by replaying a recording's frame_*.png in order."""
    from PIL import Image
    paths = sorted(glob.glob(os.path.join(folder, "frame_*.png")))
    if not paths:
        return
    period = 1.0 / max(1, hz)
    t_end = time.monotonic() + seconds
    i = 0
    while time.monotonic() < t_end and paths:
        img = Image.open(paths[i % len(paths)]).convert("RGB")
        yield img
        i += 1
        time.sleep(period)


def run(frames_iter, _unused=None, cold=True):
    """FINDER-ONLY per frame — fast, no value-OCR, no freeze. Records
    the RAW per-frame detector output (title anchor + label-row tops),
    which is the layer that actually moves in the live overlay (the
    full pipeline freezes on a locked rock and goes stable by design,
    hiding the jump).

    cold=True resets the cross-frame tracker + pose-hold before EACH
    frame, so every frame is an independent detection: the scatter then
    shows the raw detector's frame-to-frame disagreement — the jump in
    its purest form. cold=False keeps state (what smoothing actually
    delivers — should be tight)."""
    from ocr import onnx_hud_reader as ohr
    from ocr.sc_ocr import scan_results_match as srm
    try:
        from ocr.sc_ocr import label_match as lm
    except Exception:
        lm = None
    try:
        from ocr.sc_ocr import panel_pose as pp
    except Exception:
        pp = None

    rows = []
    for n, img in enumerate(frames_iter, 1):
        if cold:
            try:
                srm.reset_anchor_tracker(); srm._LAST_CALL_CACHE = None
            except Exception:
                pass
            try:
                if pp:
                    pp.reset()
            except Exception:
                pass
            try:
                if lm:
                    lm._LAST_CALL_CACHE = None
            except Exception:
                pass
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                a = srm.find_scan_results_anchor(img)  # full-frame, raw
            except Exception:
                a = None
            try:
                lr = ohr._find_label_rows(img)
            except Exception:
                lr = {}
        rows.append({
            "n": n,
            "t": round(time.monotonic(), 2),
            "img": img.copy(),
            "ax_raw": a["title_x"] if a else None,
            "ay_raw": a["title_y"] if a else None,
            "aw": a["title_w"] if a else None,
            "ah": a["title_h"] if a else None,
            "ax": a["title_x"] if a else None,
            "ay": a["title_y"] if a else None,
            "asc": a["score"] if a else None,
            "mass_y": lr.get("mass", (None,))[0] if lr.get("mass") else None,
            "res_y": lr.get("resistance", (None,))[0] if lr.get("resistance") else None,
            "ins_y": lr.get("instability", (None,))[0] if lr.get("instability") else None,
            "lv_m": None, "lv_r": None, "lv_i": None, "lv_min": None,
            "fz_m": None, "fz_r": None, "fz_i": None, "fz_min": None,
            "frozen": False,
        })
    return rows


def render_mosaic(rows, path, note=""):
    """Draw each frame with the finder's anchor box + row bands on it,
    arranged in a grid, so the jumping is VISIBLE: a stable finder
    stacks its boxes in one place across cells; a jumping finder
    scatters them. Cells are captioned with live/frozen values.
    Always writes SOMETHING — a placeholder when no frames — so there
    is always a file to look at."""
    from PIL import Image, ImageDraw
    frames = [r for r in rows if r.get("img") is not None]
    if not frames:
        ph = Image.new("RGB", (700, 90), (18, 18, 22))
        ImageDraw.Draw(ph).text(
            (12, 30),
            "0 frames captured. %s" % (note or "Check --region X,Y,W,H "
            "matches the HUD on screen, or use --frames <folder>."),
            fill=(255, 120, 120))
        ph.save(path)
        return path
    GOLD, CYAN, RED, GREEN = (255, 200, 0), (0, 220, 255), (255, 70, 70), (0, 230, 100)
    CAPH = 34
    fw, fh = frames[0]["img"].size
    sc = min(1.0, 320.0 / fw)
    cw, ch = int(fw * sc), int(fh * sc) + CAPH
    cols = min(4, len(frames))
    import math
    r_ = math.ceil(len(frames) / cols)
    sheet = Image.new("RGB", (cols * cw + (cols + 1) * 6,
                              r_ * ch + (r_ + 1) * 6), (18, 18, 22))
    dr = ImageDraw.Draw(sheet)
    for i, fr in enumerate(frames):
        im = fr["img"].resize((int(fw * sc), int(fh * sc)), Image.BILINEAR).convert("RGB")
        d = ImageDraw.Draw(im)
        # anchor box (gold)
        if fr["ax_raw"] is not None:
            x, y = fr["ax_raw"] * sc, fr["ay_raw"] * sc
            w, h = (fr["aw"] or 0) * sc, (fr["ah"] or 0) * sc
            d.rectangle([x, y, x + w, y + h], outline=GOLD, width=2)
        # row bands (horizontal lines across the frame)
        for ry, col in ((fr["mass_y"], CYAN), (fr["res_y"], GREEN), (fr["ins_y"], RED)):
            if ry is not None:
                yy = ry * sc
                d.line([(0, yy), (im.width, yy)], fill=col, width=1)
        cx = 6 + (i % cols) * (cw + 6)
        cy = 6 + (i // cols) * (ch + 6)
        sheet.paste(im, (cx, cy))
        cap = "#%d anc=%s,%s sc=%s  rows m/r/i=%s/%s/%s" % (
            fr["n"], _fmt(fr["ax_raw"]), _fmt(fr["ay_raw"]), _fmt(fr["asc"]),
            _fmt(fr["mass_y"]), _fmt(fr["res_y"]), _fmt(fr["ins_y"]))
        dr.text((cx + 2, cy + im.height + 2), cap, fill=(220, 220, 220))
    sheet.save(path)
    return path


def render_scatter(rows, path):
    """THE money shot: overlay every frame's raw anchor box + mass-row
    line on a single base frame, colored blue(early)->red(late). A
    stable finder draws them all on top of each other (one crisp box);
    a jumping finder smears them across the panel. One look answers it."""
    from PIL import Image, ImageDraw
    fr = [r for r in rows if r.get("img") is not None and r.get("ax_raw") is not None]
    if not fr:
        return None
    base = fr[0]["img"].convert("RGB")
    # dim the base so the colored boxes pop
    base = Image.eval(base, lambda p: int(p * 0.45))
    d = ImageDraw.Draw(base)
    N = len(fr)
    for i, r in enumerate(fr):
        t = i / max(1, N - 1)
        col = (int(255 * t), 50, int(255 * (1 - t)))
        x, y = r["ax_raw"], r["ay_raw"]
        w, h = r["aw"] or 0, r["ah"] or 0
        d.rectangle([x, y, x + w, y + h], outline=col, width=1)
        if r["mass_y"] is not None:
            d.line([(0, r["mass_y"]), (base.width, r["mass_y"])], fill=col, width=1)
    axs = [r["ax_raw"] for r in fr]
    ays = [r["ay_raw"] for r in fr]
    d.text((4, 4), "%d detections  blue=first red=last  "
           "anchor x:%d-%d (span %d)  y:%d-%d (span %d)"
           % (N, min(axs), max(axs), max(axs) - min(axs),
              min(ays), max(ays), max(ays) - min(ays)),
           fill=(255, 255, 255))
    base.save(path)
    return path


def _span(vals):
    vs = [v for v in vals if v is not None]
    if not vs:
        return None
    return (min(vs), max(vs), max(vs) - min(vs))


def _max_jump(vals):
    prev = None
    mx = 0
    cnt = 0
    for v in vals:
        if v is not None and prev is not None:
            d = abs(v - prev)
            mx = max(mx, d)
            if d > 10:
                cnt += 1
        if v is not None:
            prev = v
    return mx, cnt


def _flaps(vals):
    prev = None
    c = 0
    for v in vals:
        if v is not None and prev is not None and v != prev:
            c += 1
        if v is not None:
            prev = v
    return c


def report(rows, out):
    out.append("HUD LIVE FINDER DIAGNOSTIC")
    out.append("  frames captured: %d  (each scan ~1-4s; for a longer "
               "window raise --seconds)" % len(rows))
    if not rows:
        out.append("  (no frames — check --region or --frames)")
        return
    # ── per-frame table ──
    out.append("")
    out.append("PER-FRAME  (anchor = where the finder put the title; "
               "rows = band tops)")
    out.append("  %3s %-7s %-6s | %-12s | %-14s | %-14s | %s"
               % ("n", "anc(x,y)", "score", "rows m/r/i", "LIVE m/r/i",
                  "FROZEN m/r/i", "fz?"))
    for r in rows:
        anc = ("%s,%s" % (_fmt(r["ax"]), _fmt(r["ay"]))) if r["ax"] is not None else "-"
        out.append(
            "  %3d %-7s %-6s | %-12s | %-14s | %-14s | %s"
            % (r["n"], anc, _fmt(r["asc"]),
               "%s/%s/%s" % (_fmt(r["mass_y"]), _fmt(r["res_y"]), _fmt(r["ins_y"])),
               "%s/%s/%s" % (_fmt(r["lv_m"]), _fmt(r["lv_r"]), _fmt(r["lv_i"])),
               "%s/%s/%s" % (_fmt(r["fz_m"]), _fmt(r["fz_r"]), _fmt(r["fz_i"])),
               "Y" if r["frozen"] else "."))
    # ── jump summary ──
    out.append("")
    out.append("JUMP SUMMARY  (this is the 'finder jumps' measurement)")
    for label, key in (("anchor x", "ax"), ("anchor y", "ay"),
                       ("mass row y", "mass_y"), ("resistance row y", "res_y"),
                       ("instability row y", "ins_y")):
        sp = _span([r[key] for r in rows])
        mj, mc = _max_jump([r[key] for r in rows])
        if sp is None:
            out.append("  %-18s: no data" % label)
        else:
            out.append("  %-18s: range %d..%d (span %dpx)  "
                       "max frame-jump %dpx  frames>10px: %d"
                       % (label, sp[0], sp[1], sp[2], mj, mc))
    # ── value stability + live-vs-frozen ──
    out.append("")
    out.append("VALUE STABILITY")
    for label, lk, fk in (("mass", "lv_m", "fz_m"),
                         ("resistance", "lv_r", "fz_r"),
                         ("instability", "lv_i", "fz_i"),
                         ("mineral", "lv_min", "fz_min")):
        live_flaps = _flaps([r[lk] for r in rows])
        diverge = sum(
            1 for r in rows
            if r["frozen"] and r[lk] is not None and r[fk] is not None
            and r[lk] != r[fk]
        )
        distinct = len({r[lk] for r in rows if r[lk] is not None})
        out.append("  %-11s live flaps=%d  distinct live values=%d  "
                   "live≠frozen frames=%d"
                   % (label, live_flaps, distinct, diverge))
    frozen_frames = sum(1 for r in rows if r["frozen"])
    out.append("")
    out.append("  frozen active in %d/%d frames" % (frozen_frames, len(rows)))


def _selftest():
    # Synthetic rows: a jumpy anchor + a flapping live value vs steady frozen.
    rows = []
    xs = [86, 88, 476, 90, 87, 476, 85]   # two big jumps to 476 (the phantom)
    lv_r = [52.0, 52.0, 100.0, 52.0, 11.0, 52.0, 52.0]
    for i, (x, r) in enumerate(zip(xs, lv_r), 1):
        rows.append({
            "n": i, "t": float(i), "ax": x, "ay": 68, "asc": 1.2,
            "mass_y": 300 + (i % 3), "res_y": 377, "ins_y": 455,
            "lv_m": 12334.0, "lv_r": r, "lv_i": 45.05, "lv_min": "Laranite",
            "fz_m": 12334.0, "fz_r": 52.0, "fz_i": 45.05, "fz_min": "Laranite",
            "frozen": True,
        })
    out = []
    report(rows, out)
    text = "\n".join(out)
    checks = 0
    assert "anchor x" in text and "span 391px" in text; checks += 1
    assert "frames>10px: 4" in text; checks += 1  # 4 transitions to/from 476
    import re as _re
    assert _re.search(r"resistance\s+live flaps=4", text); checks += 1
    assert "live≠frozen frames=2" in text; checks += 1  # the 100 and 11
    assert "frozen active in 7/7 frames" in text; checks += 1
    print(text)
    print("\nSELFTEST OK (%d checks)" % checks)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--region", help="X,Y,W,H of the HUD on screen")
    ap.add_argument("--frames", help="recording folder to replay instead of live")
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--hz", type=int, default=6)
    ap.add_argument("--warm", action="store_true",
                    help="keep tracker/pose state (smoothed; should be "
                         "tight). Default is cold: reset per frame to "
                         "expose raw per-frame finder disagreement.")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return _selftest()

    if args.frames:
        frames = _capture_frames_replay(args.frames, args.seconds, args.hz)
        src = "replay:%s" % os.path.basename(args.frames.rstrip("/\\"))
    elif args.region:
        try:
            x, y, w, h = (int(v) for v in args.region.split(","))
        except Exception:
            print("--region must be X,Y,W,H (ints)")
            return 2
        frames = _capture_frames_live({"x": x, "y": y, "w": w, "h": h},
                                      args.seconds, args.hz)
        src = "live:%d,%d,%d,%d" % (x, y, w, h)
    else:
        print("need --region X,Y,W,H  or  --frames <folder>  (or --selftest)")
        return 2

    # Capture ALL frames fast first (decoupled from the slower finder),
    # so we get dense temporal sampling of the motion instead of being
    # bottlenecked by per-frame processing.
    print("capturing ~%.0fs @ %dHz from %s ..." % (args.seconds, args.hz, src))
    captured = list(frames)
    print("  %d frames grabbed; running finder (cold) on each ..."
          % len(captured))
    rows = run(captured, cold=not args.warm)
    out = ["source: %s  mode=%s" % (src, "warm" if args.warm else "cold")]
    report(rows, out)
    text = "\n".join(out)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(text + "\n")
    print(text)
    opened = False
    try:
        if render_scatter(rows, SCATTER):
            print("\nVISUAL scatter (the jump, all detections on one frame) "
                  "-> %s" % SCATTER)
            try:
                os.startfile(SCATTER)  # noqa: windows-only
                opened = True
            except Exception:
                pass
    except Exception as _e:
        print("\n(scatter render failed: %s)" % _e)
    try:
        if render_mosaic(rows, MOSAIC, note="source was %s" % src):
            print("VISUAL mosaic (per-frame) -> %s" % MOSAIC)
            if not opened:
                try:
                    os.startfile(MOSAIC)
                except Exception:
                    pass
    except Exception as _e:
        print("(mosaic render failed: %s)" % _e)
    print("report -> %s" % REPORT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
