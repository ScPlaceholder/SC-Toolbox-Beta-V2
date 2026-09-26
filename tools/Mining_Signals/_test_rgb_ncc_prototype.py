"""Prototype: RGB NCC for label matching, tested on the 5 remaining
ANNOTATED failures.

Reuses the per-channel NCC machinery from icon_rgb_ncc.py and adds
weighted combination across R/G/B. Goal: see whether RGB NCC finds
MASS at the correct position on panels where grayscale NCC found it
at a spurious position (e.g., panel 155912_020 where MASS was 0.86
NCC at wrong position on grayscale).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logging.getLogger().setLevel(logging.CRITICAL)

from hud_tracker.anchors.icon_rgb_ncc import _ncc_one_channel
from ocr.sc_ocr.hud_panel_tracker import DEFAULT_OFFSETS, HudPanelTracker

# RGB scale factors -- match label_match.py's grayscale sweep so we
# search the same coverage.
_SCALE_FACTORS = (0.6, 0.75, 0.9, 1.0, 1.15, 1.35, 1.6, 2.0)


def _resize_rgb(t: np.ndarray, scale: float) -> np.ndarray:
    """Resize an (H, W, 3) float32 [0..255] template by scale."""
    h, w = t.shape[:2]
    new_h = max(4, int(round(h * scale)))
    new_w = max(4, int(round(w * scale)))
    if new_h == h and new_w == w:
        return t
    pil = Image.fromarray(np.clip(t, 0, 255).astype(np.uint8))
    pil = pil.resize((new_w, new_h), Image.BILINEAR)
    return np.asarray(pil, dtype=np.float32)


def _rgb_ncc(target_rgb: np.ndarray, template_rgb: np.ndarray,
             weights: tuple[float, float, float]):
    """Returns (combined_score, x, y) at best position."""
    sR = _ncc_one_channel(target_rgb[..., 0], template_rgb[..., 0])
    sG = _ncc_one_channel(target_rgb[..., 1], template_rgb[..., 1])
    sB = _ncc_one_channel(target_rgb[..., 2], template_rgb[..., 2])
    if sR.size == 0:
        return -1.0, 0, 0
    wR, wG, wB = weights
    combined = wR * sR + wG * sG + wB * sB
    idx = int(np.argmax(combined))
    y, x = divmod(idx, combined.shape[1])
    return float(combined[y, x]), int(x), int(y)


def main() -> int:
    rgb_templates = np.load(ROOT / "ocr" / "sc_templates" / "labels_rgb.npz")
    print(f"Loaded RGB templates: {[k for k in rgb_templates.files if k != 'height']}")

    # Find current ANNOTATED failures (post all v2.2.13 fixes).
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat)
                       if os.path.exists(p[:-4] + ".boxes.json"))
    fails: list[str] = []
    for p in annotated:
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            r = t._cold_start(img)
        except Exception:
            r = None
        if r is None:
            fails.append(p)

    # Try a few weight schemes.
    weight_schemes = [
        ("equal",            (1/3, 1/3, 1/3)),
        ("G-emphasis",       (0.20, 0.50, 0.30)),
        ("G+B vs R",         (0.10, 0.45, 0.45)),
        ("R-anti-correlate", (-0.30, 0.65, 0.65)),  # actively penalize R
    ]
    mass_t = rgb_templates["mass"]
    print(f"\nTesting RGB NCC on {len(fails)} ANNOTATED failures\n")

    for img_path in fails:
        name = os.path.basename(img_path)
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue
        W, H = img.size
        with open(img_path[:-4] + ".boxes.json") as f:
            ann = json.load(f)
        truth = ann.get("boxes", {}).get("mass_row")
        gt_x = truth["x"] if truth else None
        gt_y = truth["y"] if truth else None

        # Search left 65% (same window as grayscale matcher).
        search_w = int(W * 0.65)
        rgb = np.asarray(img.crop((0, 0, search_w, H)), dtype=np.float32)

        print(f"=== {name}  ({W}x{H})  gt=({gt_x}, {gt_y}) ===")
        for scheme_name, weights in weight_schemes:
            best = (-2.0, 0, 0, 1.0)
            for scale in _SCALE_FACTORS:
                t_scaled = _resize_rgb(mass_t, scale)
                if (t_scaled.shape[0] > rgb.shape[0]
                        or t_scaled.shape[1] > rgb.shape[1]):
                    continue
                score, x, y = _rgb_ncc(rgb, t_scaled, weights)
                if score > best[0]:
                    best = (score, x, y, scale)
            bs, bx, by, bscale = best
            verdict = "(no truth)" if gt_x is None else (
                f"OK ({bx-gt_x:+d},{by-gt_y:+d})"
                if (abs(bx - gt_x) < 30 and abs(by - gt_y) < 30)
                else f"WRONG ({bx-gt_x:+d},{by-gt_y:+d})"
            )
            print(f"  {scheme_name:20s}  score={bs:.3f} @ ({bx:>4},{by:>4}) "
                  f"scale={bscale:.2f}  {verdict}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
