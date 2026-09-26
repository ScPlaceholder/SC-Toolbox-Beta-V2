"""Diagnostic: WHY does colon fallback not save more panels?

For every ANNOTATED panel that fails to lock, dump:
  - which anchors WERE collected (so we know what's missing)
  - run find_colons blindly across the whole panel
  - report how many colons it sees and at what y's
"""
from __future__ import annotations

import glob
import logging
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from ocr.sc_ocr.colon_anchor import find_colons  # noqa: E402
from ocr.sc_ocr.hud_panel_tracker import (  # noqa: E402
    DEFAULT_OFFSETS,
    HudPanelTracker,
)

logging.getLogger().setLevel(logging.CRITICAL)  # silence


def _annotated(p: str) -> bool:
    return os.path.exists(p[:-4] + ".boxes.json")


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    images = sorted(p for p in glob.glob(pat) if _annotated(p))
    print(f"checking {len(images)} ANNOTATED panels")

    locked_count = 0
    miss_patterns: Counter[str] = Counter()
    miss_details: list[tuple[str, list[str], int]] = []  # (path, anchors, n_colons)

    for img_path in images:
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue
        tracker = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        # Replicate cold start's anchor collection to see the raw measurements.
        try:
            measurements = tracker._collect_anchors(img, search_centers=None)
        except Exception as exc:
            print(f"  EXC: {os.path.basename(img_path)}: {exc}")
            continue
        # Did it lock?
        try:
            result = tracker._cold_start(img)
        except Exception:
            result = None
        if result is not None:
            locked_count += 1
            continue

        # Filter to label_* anchors for clarity.
        label_keys = sorted(k for k in measurements.keys() if k.startswith("label_"))
        has_scan = "scan_results_template" in measurements or "scan_results" in measurements
        all_keys = sorted(measurements.keys())

        # Run find_colons on the WHOLE panel (no anchoring on label_mass).
        try:
            colons = find_colons(
                img,
                y_band=(0, img.height),
                x_range=(0, int(img.width * 0.75)),
            )
        except Exception as exc:
            colons = []
            print(f"  colon EXC: {os.path.basename(img_path)}: {exc}")

        # Bucket by missing-label pattern.
        missing_labels = []
        for k in ("label_mass", "label_resistance", "label_instability"):
            if k not in measurements:
                missing_labels.append(k.replace("label_", ""))
        pattern = "+".join(missing_labels) if missing_labels else "(none)"
        if not has_scan:
            pattern = "NO_SCAN+" + pattern
        miss_patterns[pattern] += 1

        miss_details.append((os.path.basename(img_path), all_keys, len(colons)))

    print(f"\nLOCKED: {locked_count} / {len(images)}")
    print(f"NOT_LOCKED: {len(miss_details)}\n")

    print("=== Missing-label patterns on NOT_LOCKED ===")
    for pat, n in miss_patterns.most_common():
        print(f"  {pat:50s} {n}")

    print("\n=== Per-panel detail (NOT_LOCKED) ===")
    for path, keys, ncolons in miss_details:
        kshort = [k.replace("label_", "L:").replace("scan_results", "SR")
                  .replace("_template", "") for k in keys]
        print(f"  {path}")
        print(f"     anchors: {kshort}")
        print(f"     colons found (whole panel): {ncolons}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
