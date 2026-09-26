"""Diagnostic: capture residuals on the 11 'all-anchors-present-but-fail' panels."""
from __future__ import annotations

import glob
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from ocr.sc_ocr.hud_panel_tracker import (  # noqa: E402
    DEFAULT_OFFSETS,
    HudPanelTracker,
)

logging.getLogger().setLevel(logging.CRITICAL)


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    images = sorted(p for p in glob.glob(pat)
                    if os.path.exists(p[:-4] + ".boxes.json"))
    print(f"checking {len(images)} ANNOTATED panels for residuals")

    tracker = HudPanelTracker(offsets=DEFAULT_OFFSETS)
    print(f"max_residual_px threshold = {tracker.max_residual_px}")
    print(f"min_anchors_for_lock      = {tracker._min_anchors_for_lock}\n")

    print("=== 4-anchor panels that FAILED the residual check ===")
    n_fail4 = 0
    for img_path in images:
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            measurements = t._collect_anchors(img, search_centers=None)
        except Exception:
            continue
        if len(measurements) < 4:
            continue
        pose, residuals = t._solve(measurements)
        ok = t._residuals_ok(residuals)
        if ok:
            continue
        n_fail4 += 1
        max_r = max(residuals.values())
        worst = max(residuals.items(), key=lambda kv: kv[1])
        print(f"\n  {os.path.basename(img_path)}")
        print(f"     pose=({pose[0]:.1f}, {pose[1]:.1f}, scale={pose[2]:.3f})")
        print(f"     max residual {max_r:.2f}px (worst: {worst[0]})")
        for k, v in sorted(residuals.items(), key=lambda kv: -kv[1]):
            mark = "<<" if v > t.max_residual_px else ""
            print(f"       {k:35s} {v:6.2f}px  {mark}")
        # Also show the raw anchor positions vs offsets
        print("     raw measurements:")
        for k, (x, y) in sorted(measurements.items()):
            ox, oy = DEFAULT_OFFSETS.get(k, (float('nan'), float('nan')))
            print(f"       {k:35s} pos=({x:7.1f}, {y:7.1f})  offset=({ox:6.1f}, {oy:6.1f})")

    print(f"\n  total all-anchors-but-fail panels: {n_fail4}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
