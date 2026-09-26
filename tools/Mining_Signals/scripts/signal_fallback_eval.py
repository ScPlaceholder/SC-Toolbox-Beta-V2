"""Evaluate the region2 signature value-crop sources across EVERY
annotated signature panel.

For each panel we run the LIVE detectors and score the derived value crop
against the hand-annotated value box (IoU):

  WORLD-MODEL  the live primary (pill + learned world_model fractions)
  SIGNAL_SOLVE the JSON-free pill fallback we just wired (pill +
               signal_solve fixed constants) — what fires when the world
               model is absent
  ICON         the OLD fallback (localize_icon → digits at icon_right +
               6.5*icon_w) — what SIGNAL_SOLVE replaces

This answers: when the world model is gone, is the pill-anchored fallback
actually better than dropping to the icon path? Run:
    python scripts/signal_fallback_eval.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import platform  # noqa: E402
platform._wmi = None

import numpy as np  # noqa: E402
from PIL import Image  # noqa: E402

from scripts.anchor_geometry_eval import load_annotated  # noqa: E402
from ocr.sc_ocr import api  # noqa: E402
from ocr.sc_ocr import signal_solve as ss  # noqa: E402


def iou(a, b):
    if a is None or b is None:
        return 0.0
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    x1, y1 = max(ax, bx), max(ay, by)
    x2, y2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    u = aw * ah + bw * bh - inter
    return inter / u if u > 0 else 0.0


def world_model_box(rgb, pill):
    wmr = api._load_region2_world_model_for_api()
    if wmr is None or pill is None:
        return None
    vf = (wmr.get("features") or {}).get("value")
    if not vf:
        return None
    px, py, pw, ph = pill
    return (int(round(px + vf["x_frac"]["mean"] * pw)),
            int(round(py + vf["y_frac"]["mean"] * ph)),
            int(round(vf["w_frac"]["mean"] * pw)),
            int(round(vf["h_frac"]["mean"] * ph)))


def icon_box(rgb):
    try:
        from hud_tracker.anchors.icon_voter import localize_icon
        loc = localize_icon(rgb)
        if loc is None:
            return None
        ix, iy, iw, ih = loc["bbox"]
        gap = max(2, int(iw * 0.15))
        x1 = ix + iw + gap
        return (x1, iy, max(80, int(iw * 6.5)), ih)
    except Exception:
        return None


def main():
    panels = load_annotated("region2")
    rows = {"world": [], "signal_solve": [], "icon": []}
    no_pill = 0
    n = 0
    for png, gt in panels:
        if "value" not in gt:
            continue
        v = gt["value"]
        gtb = (v["x"], v["y"], v["w"], v["h"])
        try:
            rgb = np.asarray(Image.open(png).convert("RGB"), dtype=np.uint8)
        except Exception:
            continue
        n += 1
        try:
            pill = api._find_pill_for_signal(rgb)
        except Exception:
            pill = None
        if pill is None:
            no_pill += 1
        rows["world"].append(iou(world_model_box(rgb, pill), gtb))
        pose = ss.solve(pill) if pill else None
        rows["signal_solve"].append(
            iou(pose["value_box"] if pose else None, gtb))
        rows["icon"].append(iou(icon_box(rgb), gtb))

    print("Signature value-crop accuracy across %d annotated panels "
          "(live detectors; %d had no pill):\n" % (n, no_pill))
    print("  %-14s %8s  %8s  %8s  %8s" % (
        "source", "hit@.5", "hit@.7", "mean", "median"))
    for name in ("world", "signal_solve", "icon"):
        a = sorted(rows[name])
        if not a:
            continue
        h5 = sum(1 for i in a if i >= 0.5)
        h7 = sum(1 for i in a if i >= 0.7)
        print("  %-14s %4d/%d %2d%%  %4d/%d %2d%%  %.3f    %.3f" % (
            name, h5, len(a), round(100 * h5 / len(a)),
            h7, len(a), round(100 * h7 / len(a)),
            sum(a) / len(a), a[len(a) // 2]))
    print("\nWORLD = live primary; SIGNAL_SOLVE = JSON-free pill fallback "
          "(fires when world-model absent); ICON = the fallback it "
          "replaces.")


if __name__ == "__main__":
    main()
