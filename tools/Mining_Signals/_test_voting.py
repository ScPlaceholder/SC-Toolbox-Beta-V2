"""Test pure VOTING between grayscale-NCC and RGB-NCC for MASS picking.

Strategy:
  1. Grayscale dual-polarity sweep -> top-K candidates.
  2. RGB sweep -> top-K candidates (independent search).
  3. For each grayscale candidate, find nearest RGB candidate.
     - If within 15 px: AGREEMENT (high confidence)
     - If > 15 px: DISAGREEMENT (lower confidence)
  4. Pick winner by:
     - voting_score = gray_score + rgb_score + AGREEMENT_BONUS (if agree)
                    = gray_score - DISAGREEMENT_PENALTY (if disagree)

This is the pattern from comma_finder's polarity voter -- two independent
detectors confirm each other geometrically.

Reports lock-rate vs the production (RGB-rescue) baseline.
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
    _load_rgb_mass_template, _resize_rgb_template,
    reset_template_cache,
)

WEIGHTS = (0.20, 0.50, 0.30)
AGREEMENT_PX = 15
AGREEMENT_BONUS = 0.20
DISAGREEMENT_PENALTY = 0.10


def _norm(arr_f32):
    m = float(arr_f32.mean())
    s = float(arr_f32.std())
    return (arr_f32 - m) / s if s >= 1e-3 else (arr_f32 - m)


def _ncc_search_full(target, template):
    """Return (best_score, best_x, best_y)."""
    from scipy.signal import fftconvolve  # type: ignore
    H, W = target.shape
    h, w = template.shape
    if h > H or w > W:
        return -1.0, 0, 0
    n = float(h * w)
    t_zm = template - template.mean()
    c = fftconvolve(target, t_zm[::-1, ::-1], mode="valid") / n
    idx = int(np.argmax(c))
    by, bx = divmod(idx, c.shape[1])
    return float(c[by, bx]), int(bx), int(by)


def _grayscale_top_k(target_pos, target_neg, mass_t, k=8):
    """Top-K grayscale MASS candidates across (scale, polarity)."""
    candidates = []
    for polarity, t in (("pos", target_pos), ("neg", target_neg)):
        for scale in _SCALE_FACTORS:
            sc = _resize_template(mass_t, scale)
            if sc.shape[0] > t.shape[0] or sc.shape[1] > t.shape[1]:
                continue
            score, x, y = _ncc_search_full(t, sc)
            if score < _MIN_MATCH_SCORE:
                continue
            candidates.append({
                "x": x, "y": y, "score": score, "scale": scale,
                "polarity": polarity, "w": sc.shape[1], "h": sc.shape[0],
            })
    candidates.sort(key=lambda c: -c["score"])
    return candidates[:k]


def _rgb_top_k(rgb_target, mass_rgb, k=8):
    """Top-K RGB MASS candidates across scales."""
    candidates = []
    for scale in _SCALE_FACTORS:
        t = _resize_rgb_template(mass_rgb, scale)
        if t.shape[0] > rgb_target.shape[0] or t.shape[1] > rgb_target.shape[1]:
            continue
        sR = _ncc_one_channel(rgb_target[..., 0], t[..., 0])
        sG = _ncc_one_channel(rgb_target[..., 1], t[..., 1])
        sB = _ncc_one_channel(rgb_target[..., 2], t[..., 2])
        if sR.size == 0:
            continue
        combined = WEIGHTS[0] * sR + WEIGHTS[1] * sG + WEIGHTS[2] * sB
        idx = int(np.argmax(combined))
        py, px = divmod(idx, combined.shape[1])
        candidates.append({
            "x": int(px), "y": int(py), "score": float(combined[py, px]),
            "scale": scale, "w": t.shape[1], "h": t.shape[0],
        })
    candidates.sort(key=lambda c: -c["score"])
    return candidates[:k]


def _vote(gray_cands, rgb_cands):
    """Pick winner by agreement-aware voting score."""
    best = None
    for g in gray_cands:
        # Nearest RGB candidate to this grayscale candidate.
        if rgb_cands:
            nearest = min(
                rgb_cands,
                key=lambda r: (r["x"] - g["x"]) ** 2 + (r["y"] - g["y"]) ** 2,
            )
            dist = ((nearest["x"] - g["x"]) ** 2
                    + (nearest["y"] - g["y"]) ** 2) ** 0.5
            if dist <= AGREEMENT_PX:
                vote_score = g["score"] + nearest["score"] + AGREEMENT_BONUS
                agree = True
            else:
                vote_score = g["score"] - DISAGREEMENT_PENALTY
                agree = False
        else:
            vote_score = g["score"]
            agree = False
        if best is None or vote_score > best["vote_score"]:
            best = {**g, "vote_score": vote_score, "agreement": agree}
    return best


def main() -> int:
    rgb_mass = _load_rgb_mass_template()
    if rgb_mass is None:
        print("RGB template missing -- abort")
        return 1
    templates = _load_templates()
    mass_t = templates["mass"]

    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat)
                       if os.path.exists(p[:-4] + ".boxes.json"))

    # For each panel, run voting and check if winner is geometrically correct.
    correct = 0
    wrong = 0
    n = 0
    by_agreement = Counter()
    correctness_by_agreement = Counter()

    for img_path in annotated:
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue
        W, H = img.size
        with open(img_path[:-4] + ".boxes.json") as f:
            ann = json.load(f)
        truth = ann.get("boxes", {}).get("mass_row")
        if truth is None:
            continue
        gt_x, gt_y = int(truth["x"]), int(truth["y"])

        search_w = int(W * 0.65)
        gray = np.asarray(img.crop((0, 0, search_w, H)).convert("L"),
                          dtype=np.uint8)
        target_pos = _norm(gray.astype(np.float32))
        target_neg = _norm((255 - gray).astype(np.float32))
        rgb = np.asarray(img.crop((0, 0, search_w, H)), dtype=np.float32)

        g_cands = _grayscale_top_k(target_pos, target_neg, mass_t, k=8)
        r_cands = _rgb_top_k(rgb, rgb_mass, k=8)
        if not g_cands:
            continue
        winner = _vote(g_cands, r_cands)
        n += 1
        agree = winner["agreement"]
        by_agreement[agree] += 1
        is_correct = (abs(winner["x"] - gt_x) < 30
                      and abs(winner["y"] - gt_y) < 50)
        if is_correct:
            correct += 1
            correctness_by_agreement[(agree, "correct")] += 1
        else:
            wrong += 1
            correctness_by_agreement[(agree, "wrong")] += 1

    print(f"Voting result on {n} ANNOTATED panels:")
    print(f"  correct localization: {correct}/{n} ({100*correct/n:.1f}%)")
    print(f"  wrong localization:   {wrong}/{n} ({100*wrong/n:.1f}%)")
    print(f"\n  By agreement (gray and rgb top within {AGREEMENT_PX}px):")
    for agree, cnt in by_agreement.most_common():
        label = "AGREE" if agree else "DISAGREE"
        cc = correctness_by_agreement.get((agree, "correct"), 0)
        cw = correctness_by_agreement.get((agree, "wrong"), 0)
        print(f"    {label}: {cnt}  (correct={cc}, wrong={cw})")
    print("\n  Compare to current production (grayscale dual-polarity + RGB rescue):")
    print(f"    -> 68/72 ANNOTATED locks observed in end-to-end test")
    return 0


if __name__ == "__main__":
    sys.exit(main())
