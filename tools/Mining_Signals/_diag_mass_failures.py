"""For each ANNOTATED panel that fails _cold_start, show:
  - what MASS *actually* matched (peak score + position across all scales)
  - what the ground-truth MASS position is (from .boxes.json)
  - the top 3 MASS candidates at the matched scale (peak + 2 runners-up)

This tells us whether MASS:
  (a) finds nothing (no peak above threshold)
  (b) finds the right position but at low score (template degradation)
  (c) finds the wrong position at high score (spurious match)
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


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    annotated = sorted(p for p in glob.glob(pat) if _ann(p))

    # Find ANNOTATED panels that fail _cold_start.
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

    print(f"Diagnosing {len(fails)} ANNOTATED _cold_start failures\n")

    templates = _load_templates()
    mass_t = templates["mass"]

    for img_path in fails:
        name = os.path.basename(img_path)
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception as e:
            print(f"{name}: load failed: {e}")
            continue
        W, H = img.size

        # Ground truth from annotation.
        with open(img_path[:-4] + ".boxes.json") as f:
            ann = json.load(f)
        truth = ann.get("boxes", {}).get("mass_row")

        # Replicate label_match's full-frame search.
        search_w = int(W * 0.65)
        crop = img.crop((0, 0, search_w, H))
        target = _canonicalize(np.asarray(crop.convert("L"), dtype=np.uint8))

        print(f"=== {name}  ({W}x{H}) ===")
        if truth:
            tx, ty, tw, th = truth["x"], truth["y"], truth["w"], truth["h"]
            print(f"  GT mass_row: x={tx} y={ty} w={tw} h={th} "
                  f"(left_edge={tx}, center=({tx+tw//2},{ty+th//2}))")

        # Search MASS at every scale.
        print(f"  MASS NCC peaks across scales:")
        best_overall = (-1.0, 0, 0, 1.0)
        for scale in _SCALE_FACTORS:
            sc = _resize_template(mass_t, scale)
            if sc.shape[0] > target.shape[0] or sc.shape[1] > target.shape[1]:
                continue
            score, x, y = _ncc_search(target, sc)
            marker = ""
            if score >= _MIN_MATCH_SCORE:
                marker = " >>> accepted"
            print(f"    scale={scale:.2f}  score={score:.3f}  pos=({x:>4},{y:>4})"
                  f"  template size={sc.shape[1]}x{sc.shape[0]}{marker}")
            if score > best_overall[0]:
                best_overall = (score, x, y, scale)

        bs, bx, by, bscale = best_overall
        print(f"  -> BEST overall: scale={bscale:.2f}  score={bs:.3f}  pos=({bx}, {by})")
        if truth:
            dx = bx - truth["x"]
            dy = by - truth["y"]
            verdict = ""
            if abs(dx) < 30 and abs(dy) < 30:
                verdict = "  OK on/near truth"
            else:
                verdict = f"  WRONG off truth by ({dx:+d}, {dy:+d}) px"
            print(f"  -> vs truth ({truth['x']}, {truth['y']}){verdict}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
