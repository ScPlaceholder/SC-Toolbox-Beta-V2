"""For every ANNOTATED panel, find the BEST RGB NCC match for MASS
(searching all scales) and report:
  - score
  - position
  - whether it lands on the truth from .boxes.json

Goal: see what RGB-NCC threshold reliably catches real MASS without
admitting spurious matches.
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

_SCALE_FACTORS = (0.6, 0.75, 0.9, 1.0, 1.15, 1.35, 1.6, 2.0)
# Best weights from the previous test: equal-ish but de-emphasise R.
WEIGHTS = (0.20, 0.50, 0.30)


def _resize_rgb(t: np.ndarray, scale: float) -> np.ndarray:
    h, w = t.shape[:2]
    new_h = max(4, int(round(h * scale)))
    new_w = max(4, int(round(w * scale)))
    if new_h == h and new_w == w:
        return t
    pil = Image.fromarray(np.clip(t, 0, 255).astype(np.uint8))
    pil = pil.resize((new_w, new_h), Image.BILINEAR)
    return np.asarray(pil, dtype=np.float32)


def _rgb_ncc_best(target_rgb: np.ndarray, template_rgb: np.ndarray):
    sR = _ncc_one_channel(target_rgb[..., 0], template_rgb[..., 0])
    sG = _ncc_one_channel(target_rgb[..., 1], template_rgb[..., 1])
    sB = _ncc_one_channel(target_rgb[..., 2], template_rgb[..., 2])
    if sR.size == 0:
        return -1.0, 0, 0
    combined = WEIGHTS[0] * sR + WEIGHTS[1] * sG + WEIGHTS[2] * sB
    idx = int(np.argmax(combined))
    y, x = divmod(idx, combined.shape[1])
    return float(combined[y, x]), int(x), int(y)


def main() -> int:
    rgb_templates = np.load(ROOT / "ocr" / "sc_templates" / "labels_rgb.npz")
    mass_t = rgb_templates["mass"]

    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat)
                       if os.path.exists(p[:-4] + ".boxes.json"))
    print(f"Testing RGB NCC MASS on {len(annotated)} ANNOTATED panels\n")

    correct_scores: list[float] = []
    wrong_scores: list[float] = []
    no_truth = 0
    by_bucket = Counter()  # bucket: (above_thr, on_truth)

    rows: list[tuple[float, str, bool]] = []  # (score, name, is_correct)

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
        if truth is None:
            no_truth += 1
            continue
        gt_x, gt_y = int(truth["x"]), int(truth["y"])

        search_w = int(W * 0.65)
        rgb = np.asarray(img.crop((0, 0, search_w, H)), dtype=np.float32)

        best = (-2.0, 0, 0, 1.0)
        for scale in _SCALE_FACTORS:
            t_scaled = _resize_rgb(mass_t, scale)
            if (t_scaled.shape[0] > rgb.shape[0]
                    or t_scaled.shape[1] > rgb.shape[1]):
                continue
            score, x, y = _rgb_ncc_best(rgb, t_scaled)
            if score > best[0]:
                best = (score, x, y, scale)
        bs, bx, by, bscale = best
        is_correct = abs(bx - gt_x) < 30 and abs(by - gt_y) < 50
        if is_correct:
            correct_scores.append(bs)
        else:
            wrong_scores.append(bs)
        rows.append((bs, name, is_correct))

    print(f"Scanned {len(rows)} ANNOTATED panels with truth\n")
    print(f"Correct localization (within 30px x, 50px y of truth):")
    print(f"  count: {len(correct_scores)} / {len(rows)} "
          f"({100*len(correct_scores)/len(rows):.1f}%)")
    if correct_scores:
        cs = np.array(correct_scores)
        print(f"  score range: [{cs.min():.3f}, {cs.max():.3f}]  "
              f"median={np.median(cs):.3f}  mean={cs.mean():.3f}")
    print(f"\nWrong localization:")
    print(f"  count: {len(wrong_scores)}")
    if wrong_scores:
        ws = np.array(wrong_scores)
        print(f"  score range: [{ws.min():.3f}, {ws.max():.3f}]  "
              f"median={np.median(ws):.3f}  mean={ws.mean():.3f}")

    # Find the threshold that gives us best separation.
    print("\n=== Threshold-vs-keep-rate sweep ===")
    print("threshold  correct_kept  wrong_kept  net_useful")
    for thr in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70):
        c_kept = sum(1 for s in correct_scores if s >= thr)
        w_kept = sum(1 for s in wrong_scores if s >= thr)
        net = c_kept - w_kept
        print(f"  {thr:.2f}        {c_kept:>3}/{len(correct_scores)}"
              f"          {w_kept:>3}/{len(wrong_scores)}"
              f"        {net:+d}")

    # Print outliers.
    print("\n=== Wrong matches that nonetheless score high (candidate FP) ===")
    rows.sort(key=lambda r: -r[0])
    for bs, name, is_correct in rows:
        if not is_correct and bs >= 0.50:
            print(f"  {bs:.3f}  {name}")

    print("\n=== Correct matches that score LOW (candidate FN) ===")
    rows.sort(key=lambda r: r[0])
    for bs, name, is_correct in rows:
        if is_correct and bs < 0.55:
            print(f"  {bs:.3f}  {name}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
