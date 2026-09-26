"""ENSEMBLE: grayscale-dual-polarity finds candidate MASS; RGB at that
position verifies / rejects.

For every ANNOTATED panel:
  1. Run production _cold_start (current with all our v2.2.13 fixes).
  2. If it locked, query the grayscale MASS position from label_match's
     last full-frame result.
  3. Compute RGB NCC score at that exact position.
  4. Decide: would the ensemble (gray + RGB > 0.55) keep / drop this lock?

Goal: see whether ensemble would
  - Drop any panels we currently LOCK on (regression risk)
  - Save panels we currently DON'T LOCK on (the win)
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
from ocr.sc_ocr.label_match import (
    _load_templates, _resize_template, _SCALE_FACTORS, _MIN_MATCH_SCORE,
    find_label_positions, get_last_full_frame_matches, reset_template_cache,
)

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


def _rgb_score_at(rgb_full: np.ndarray, template_rgb: np.ndarray,
                  x: int, y: int) -> float:
    """RGB NCC score of `template_rgb` against the patch at (x, y) in rgb_full."""
    h, w = template_rgb.shape[:2]
    H, W = rgb_full.shape[:2]
    if x < 0 or y < 0 or x + w > W or y + h > H:
        return -2.0
    patch = rgb_full[y:y + h, x:x + w]
    # Per-channel NCC at this single location: cosine-similarity of zero-mean patches.
    score = 0.0
    for ch, weight in enumerate(WEIGHTS):
        p = patch[..., ch].astype(np.float32)
        t = template_rgb[..., ch].astype(np.float32)
        p_zm = p - p.mean()
        t_zm = t - t.mean()
        p_norm = float(np.sqrt(np.sum(p_zm * p_zm)))
        t_norm = float(np.sqrt(np.sum(t_zm * t_zm)))
        if p_norm < 1e-6 or t_norm < 1e-6:
            continue
        ch_score = float(np.sum(p_zm * t_zm) / (p_norm * t_norm))
        score += weight * ch_score
    return score


def main() -> int:
    rgb_templates = np.load(ROOT / "ocr" / "sc_templates" / "labels_rgb.npz")
    mass_rgb_t = rgb_templates["mass"]

    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat)
                       if os.path.exists(p[:-4] + ".boxes.json"))

    print(f"Ensemble test on {len(annotated)} ANNOTATED panels\n")

    # For each panel:
    # - Run cold_start to see if it currently locks.
    # - Get the GRAYSCALE MASS position from last_full_frame_matches.
    # - Score RGB at that position with the matching scale.
    # - Compare against ground truth from boxes.json.

    rgb_at_correct: list[float] = []
    rgb_at_wrong: list[float] = []
    rgb_at_lock_correct: list[float] = []  # locked AND grayscale was right
    rgb_at_lock_wrong: list[float] = []    # locked BUT grayscale was wrong (impossible to lock if wrong... but check)
    rgb_at_nolock: list[float] = []

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

        reset_template_cache()  # ensure fresh state per panel
        # First: cold start.
        tracker = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            cold_result = tracker._cold_start(img)
        except Exception:
            cold_result = None
        # Then: get the grayscale full-frame matches (just MASS for now).
        # find_label_positions caches per-image; if cold_start already
        # called it the cache hits.
        try:
            matches = find_label_positions(img)
        except Exception:
            matches = {}
        mass_match = matches.get("mass")
        if mass_match is None:
            # Grayscale couldn't find MASS at all -- ensemble doesn't change
            # this case.
            continue

        mx = int(mass_match["x"])
        my = int(mass_match["y"])
        mscale = float(mass_match.get("scale", 1.0))

        # Crop the search window (same as label_match: left 65%).
        rgb_full = np.asarray(img.crop((0, 0, int(W * 0.65), H)),
                              dtype=np.float32)
        # Score RGB at the grayscale MASS position, at MASS's scale.
        rgb_template_scaled = _resize_rgb(mass_rgb_t, mscale)
        rgb_score = _rgb_score_at(rgb_full, rgb_template_scaled, mx, my)

        # Was grayscale right?
        if truth:
            gt_x, gt_y = int(truth["x"]), int(truth["y"])
            gray_correct = abs(mx - gt_x) < 30 and abs(my - gt_y) < 50
        else:
            gray_correct = True  # benefit of doubt

        if cold_result is not None:
            # Locked.
            if gray_correct:
                rgb_at_lock_correct.append(rgb_score)
            else:
                rgb_at_lock_wrong.append(rgb_score)
        else:
            rgb_at_nolock.append(rgb_score)

        if gray_correct:
            rgb_at_correct.append(rgb_score)
        else:
            rgb_at_wrong.append(rgb_score)

    def _stats(label, arr):
        if not arr:
            print(f"  {label}: (empty)")
            return
        a = np.array(arr)
        print(f"  {label} (n={len(arr)}): "
              f"min={a.min():.3f} median={np.median(a):.3f} "
              f"mean={a.mean():.3f} max={a.max():.3f}")

    print("RGB score at the grayscale-found MASS position:")
    _stats("when grayscale was CORRECT", rgb_at_correct)
    _stats("when grayscale was WRONG  ", rgb_at_wrong)

    print("\nBy cold_start outcome:")
    _stats("LOCKED + gray correct", rgb_at_lock_correct)
    _stats("LOCKED + gray wrong  ", rgb_at_lock_wrong)
    _stats("NOT LOCKED           ", rgb_at_nolock)

    print("\n=== Ensemble threshold sweep (RGB-at-graypos >= thr to ACCEPT) ===")
    print("thr      lock_kept   lock_drop   nolock_unchanged")
    n_locked = len(rgb_at_lock_correct) + len(rgb_at_lock_wrong)
    for thr in (0.40, 0.45, 0.50, 0.55, 0.60, 0.65):
        # If we apply the ensemble: panels with RGB < thr at the grayscale
        # MASS position would have MASS rejected.
        # For panels that currently LOCK: if RGB rejects MASS, they lose
        # the lock (regression).
        # For panels that currently DON'T LOCK: ensemble doesn't help unless
        # MASS rejection triggers an alternative anchor mechanism.
        keep_lock_c = sum(1 for s in rgb_at_lock_correct if s >= thr)
        keep_lock_w = sum(1 for s in rgb_at_lock_wrong if s >= thr)
        dropped_locks = (len(rgb_at_lock_correct) + len(rgb_at_lock_wrong)
                         - keep_lock_c - keep_lock_w)
        print(f"  {thr:.2f}     {keep_lock_c+keep_lock_w}/{n_locked}        "
              f"{dropped_locks:>2}            (potentially)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
