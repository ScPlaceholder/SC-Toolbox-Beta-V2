"""Find and diagnose the ANNOTATED panels that fail _cold_start."""
from __future__ import annotations

import glob
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from ocr.sc_ocr.colon_anchor import find_colons  # noqa: E402
from ocr.sc_ocr.hud_panel_tracker import (  # noqa: E402
    DEFAULT_OFFSETS,
    HudPanelTracker,
)

logging.getLogger().setLevel(logging.CRITICAL)


def _annotation_status(p: str) -> str:
    base = p[:-4]
    if os.path.exists(base + ".skip"):
        return "SKIP"
    if os.path.exists(base + ".boxes.json"):
        return "ANNOTATED"
    return "UNREVIEWED"


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    paths = sorted(glob.glob(pat))
    ann = [p for p in paths if _annotation_status(p) == "ANNOTATED"]
    print(f"ANNOTATED: {len(ann)}")

    # Step 1: identify failures.
    fails: list[str] = []
    for p in ann:
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
    print(f"failures: {len(fails)}")
    for f in fails:
        print(f"  {os.path.basename(f)}")

    # Step 2: diagnose each.
    for img_path in fails:
        name = os.path.basename(img_path)
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception as e:
            print(f"\n{name}: load failed: {e}")
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            measurements = t._collect_anchors(img, search_centers=None)
        except Exception as exc:
            print(f"\n{name}: _collect_anchors raised {exc}")
            continue

        print(f"\n=== {name}  ({img.width}x{img.height})  anchors={len(measurements)} ===")
        for k, (x, y) in sorted(measurements.items()):
            print(f"  {k:35s} ({x:7.1f}, {y:7.1f})")
        if len(measurements) >= 3:
            pose, residuals = t._solve(measurements)
            print(f"  baseline solve: pose=({pose[0]:.1f}, {pose[1]:.1f}, s={pose[2]:.2f})")
            for k, v in sorted(residuals.items(), key=lambda kv: -kv[1]):
                mark = "<<" if v > t.max_residual_px else ""
                print(f"    resid {k:30s} {v:6.2f}px {mark}")
            if len(measurements) > t._min_anchors_for_lock:
                worst = max(residuals.items(), key=lambda kv: kv[1])[0]
                reduced = {k: v for k, v in measurements.items() if k != worst}
                pose2, residuals2 = t._solve(reduced)
                ok = t._residuals_ok(residuals2)
                print(f"  drop '{worst}' -> {'LOCK' if ok else 'still fails'}: pose=({pose2[0]:.1f},{pose2[1]:.1f},s={pose2[2]:.2f})")
                for k, v in sorted(residuals2.items(), key=lambda kv: -kv[1]):
                    mark = "<<" if v > t.max_residual_px else ""
                    print(f"    resid {k:30s} {v:6.2f}px {mark}")

        try:
            colons = find_colons(img, y_band=(0, img.height),
                                 x_range=(0, int(img.width * 0.75)))
        except Exception as e:
            colons = []
            print(f"  find_colons raised {e}")
        print(f"  find_colons sees {len(colons)} colons in (0..0.75w x 0..h)")
        for c in colons[:12]:
            print(f"    colon ({c['x']:4d}, {c['y']:4d}) score={c['score']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
