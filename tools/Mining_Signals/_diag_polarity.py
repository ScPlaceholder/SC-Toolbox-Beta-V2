"""Does MASS detection improve if we try BOTH polarities and pick
whichever scores higher?

For each ANNOTATED failure: run the multi-scale MASS NCC against
- the Otsu-canonicalized target (current production behavior), AND
- the explicitly-inverted target.

Report the best score + position for each polarity, and verify against
ground truth from .boxes.json.
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

from ocr.sc_ocr.hud_panel_tracker import DEFAULT_OFFSETS, HudPanelTracker
from ocr.sc_ocr.label_match import (
    _load_templates, _canonicalize, _resize_template, _ncc_search,
    _SCALE_FACTORS, _MIN_MATCH_SCORE,
)


def _ann(p: str) -> bool:
    return os.path.exists(p[:-4] + ".boxes.json")


def _multiscale_best(target: np.ndarray, template: np.ndarray):
    best = (-2.0, 0, 0, 1.0)
    for scale in _SCALE_FACTORS:
        sc = _resize_template(template, scale)
        if sc.shape[0] > target.shape[0] or sc.shape[1] > target.shape[1]:
            continue
        score, x, y = _ncc_search(target, sc)
        if score > best[0]:
            best = (score, x, y, scale)
    return best


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat) if _ann(p))

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
    print(f"Testing dual-polarity MASS NCC on {len(fails)} ANNOTATED failures\n")

    templates = _load_templates()
    mass_t = templates["mass"]
    res_t = templates["resistance"]
    inst_t = templates["instability"]

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

        search_w = int(W * 0.65)
        crop = img.crop((0, 0, search_w, H))
        gray = np.asarray(crop.convert("L"), dtype=np.uint8)
        # Current production polarity (Otsu heuristic):
        prod = _canonicalize(gray)
        # The other polarity: bypass Otsu, force the OPPOSITE choice.
        # Easiest way: flip gray and run canonicalize again -- but Otsu
        # may flip back. So do it the hard way: explicitly normalize
        # without the polarity flip.
        gray_inv = (255 - gray).astype(np.float32)
        mean = float(gray_inv.mean())
        std = float(gray_inv.std())
        if std >= 1e-3:
            inv = (gray_inv - mean) / std
        else:
            inv = gray_inv - mean
        # Also build the non-inverted reference, normalized only:
        ref = gray.astype(np.float32)
        rmean = float(ref.mean()); rstd = float(ref.std())
        if rstd >= 1e-3:
            ref = (ref - rmean) / rstd
        else:
            ref = ref - rmean

        print(f"=== {name}  ({W}x{H}) ===")
        if truth:
            print(f"  GT mass left=({gt_x}, {gt_y})")

        for label, template in (("MASS", mass_t), ("RESIST", res_t), ("INSTA", inst_t)):
            # Production polarity:
            ps, px, py, psc = _multiscale_best(prod, template)
            # Both raw polarities (uncanonicalized):
            rs, rx, ry, rsc = _multiscale_best(ref, template)
            is_, ix, iy, isc = _multiscale_best(inv, template)
            print(f"  {label}:")
            print(f"    PRODUCTION (Otsu canonical):  score={ps:.3f} @ ({px},{py}) scale={psc:.2f}")
            print(f"    RAW non-inverted:             score={rs:.3f} @ ({rx},{ry}) scale={rsc:.2f}")
            print(f"    RAW inverted (other polarity):score={is_:.3f} @ ({ix},{iy}) scale={isc:.2f}")
            best_polarity = max(("prod", ps), ("ref", rs), ("inv", is_), key=lambda x: x[1])
            verdict = ""
            if truth and label == "MASS":
                best_pos = {"prod": (px, py), "ref": (rx, ry), "inv": (ix, iy)}[best_polarity[0]]
                dx = best_pos[0] - gt_x
                dy = best_pos[1] - gt_y
                if abs(dx) < 30 and abs(dy) < 50:
                    verdict = f"  -> {best_polarity[0]} wins, OK on truth"
                else:
                    verdict = f"  -> {best_polarity[0]} wins, off ({dx:+},{dy:+})"
            print(f"    best polarity: {best_polarity[0]} ({best_polarity[1]:.3f}){verdict}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
