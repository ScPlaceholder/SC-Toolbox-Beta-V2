"""For each ANNOTATED panel:
  1. Find MASS via grayscale dual-polarity (current production).
  2. RGB-search a small window (±15 px) AROUND that position.
  3. Report the RGB peak score in that window + delta from grayscale.

Goal: confirm RGB peak in window scores high on currently-locked panels
and low on currently-failed panels — i.e., that windowed RGB is a
useful discriminator.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sys
from collections import Counter
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logging.getLogger().setLevel(logging.CRITICAL)

from hud_tracker.anchors.icon_rgb_ncc import _ncc_one_channel
from ocr.sc_ocr.hud_panel_tracker import DEFAULT_OFFSETS, HudPanelTracker
from ocr.sc_ocr.label_match import find_label_positions, reset_template_cache

WEIGHTS = (0.20, 0.50, 0.30)
WINDOW_PX = 30
# Try a few scales around the grayscale-found scale to compensate for
# the fact that RGB's best scale may differ slightly from grayscale's.
SCALE_VARIATIONS = (0.85, 1.0, 1.15)


def _resize_rgb(t: np.ndarray, scale: float) -> np.ndarray:
    h, w = t.shape[:2]
    new_h = max(4, int(round(h * scale)))
    new_w = max(4, int(round(w * scale)))
    if new_h == h and new_w == w:
        return t
    pil = Image.fromarray(np.clip(t, 0, 255).astype(np.uint8))
    pil = pil.resize((new_w, new_h), Image.BILINEAR)
    return np.asarray(pil, dtype=np.float32)


def _rgb_ncc_in_window(target_rgb: np.ndarray, template_rgb: np.ndarray,
                       gx: int, gy: int, window: int):
    """Peak RGB NCC score within ±window pixels of (gx, gy)."""
    h, w = template_rgb.shape[:2]
    H, W = target_rgb.shape[:2]
    # Crop a larger region to do the search inside.
    x1 = max(0, gx - window)
    y1 = max(0, gy - window)
    x2 = min(W, gx + window + w)
    y2 = min(H, gy + window + h)
    if x2 - x1 < w or y2 - y1 < h:
        return -1.0, gx, gy
    crop = target_rgb[y1:y2, x1:x2]
    sR = _ncc_one_channel(crop[..., 0], template_rgb[..., 0])
    sG = _ncc_one_channel(crop[..., 1], template_rgb[..., 1])
    sB = _ncc_one_channel(crop[..., 2], template_rgb[..., 2])
    if sR.size == 0:
        return -1.0, gx, gy
    combined = WEIGHTS[0] * sR + WEIGHTS[1] * sG + WEIGHTS[2] * sB
    idx = int(np.argmax(combined))
    py, px = divmod(idx, combined.shape[1])
    return float(combined[py, px]), int(px + x1), int(py + y1)


def main() -> int:
    rgb_templates = np.load(ROOT / "ocr" / "sc_templates" / "labels_rgb_sharp.npz")
    mass_rgb = rgb_templates["mass"]
    print("Using SHARP synthetic RGB templates")

    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat)
                       if os.path.exists(p[:-4] + ".boxes.json"))

    locked_correct: list[float] = []   # locked + grayscale right + windowed RGB
    locked_wrong:   list[float] = []   # locked but grayscale wrong
    failed_w_mass:  list[float] = []   # cold_start failed but grayscale found mass
    failed_no_mass: int = 0            # cold_start failed AND no mass at all

    for img_path in annotated:
        name = os.path.basename(img_path)
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue
        W, H = img.size
        with open(img_path[:-4] + ".boxes.json") as f:
            ann = json.load(f)
        truth = ann.get("boxes", {}).get("mass_row")

        reset_template_cache()
        tracker = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            cold = tracker._cold_start(img)
        except Exception:
            cold = None
        try:
            matches = find_label_positions(img)
        except Exception:
            matches = {}
        mass = matches.get("mass")
        if mass is None:
            if cold is None:
                failed_no_mass += 1
            continue

        mx, my = int(mass["x"]), int(mass["y"])
        mscale = float(mass.get("scale", 1.0))
        rgb_full = np.asarray(img.crop((0, 0, int(W * 0.65), H)),
                              dtype=np.float32)
        # Try several scales around grayscale's scale, take the best RGB.
        rgb_score = -2.0
        rx, ry = mx, my
        for sv in SCALE_VARIATIONS:
            t_scaled = _resize_rgb(mass_rgb, mscale * sv)
            sc, sx, sy = _rgb_ncc_in_window(
                rgb_full, t_scaled, mx, my, WINDOW_PX,
            )
            if sc > rgb_score:
                rgb_score, rx, ry = sc, sx, sy

        gray_correct = False
        if truth:
            gt_x, gt_y = int(truth["x"]), int(truth["y"])
            gray_correct = abs(mx - gt_x) < 30 and abs(my - gt_y) < 50

        if cold is not None:
            if gray_correct:
                locked_correct.append(rgb_score)
            else:
                locked_wrong.append(rgb_score)
        else:
            failed_w_mass.append(rgb_score)

    def _stats(label, arr):
        if not arr:
            print(f"  {label}: (none)")
            return
        a = np.array(arr)
        print(f"  {label} (n={len(arr)}): "
              f"min={a.min():.3f}  median={np.median(a):.3f}  "
              f"mean={a.mean():.3f}  max={a.max():.3f}")

    print(f"Windowed RGB MASS score (±{WINDOW_PX} px around grayscale candidate)\n")
    _stats("LOCKED + gray correct ", locked_correct)
    _stats("LOCKED + gray WRONG   ", locked_wrong)
    _stats("FAILED + mass found   ", failed_w_mass)
    print(f"\n  FAILED + no MASS found at all: {failed_no_mass}")

    print(f"\n=== Threshold sweep ===")
    print(f"thr     keep_lock_correct  keep_lock_wrong  keep_fail")
    total_correct = len(locked_correct)
    total_wrong = len(locked_wrong)
    total_fail = len(failed_w_mass)
    for thr in (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
        kc = sum(1 for s in locked_correct if s >= thr)
        kw = sum(1 for s in locked_wrong if s >= thr)
        kf = sum(1 for s in failed_w_mass if s >= thr)
        print(f"  {thr:.2f}    {kc}/{total_correct}              "
              f"{kw}/{total_wrong}            {kf}/{total_fail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
