"""Categorize the remaining failures across all tiers.

For each NOT_LOCKED panel: count colons in the panel as a proxy for
"does this image even contain a HUD."  Colons are tiny stable glyphs
that appear ~3+ times in any visible mining HUD; <2 colons total
strongly suggests no HUD content at all.

Goal: find out how many failures are RECOVERABLE (have HUD content
but we failed to lock) vs UNRECOVERABLE (no HUD content).
"""
from __future__ import annotations

import glob
import logging
import os
import sys
from collections import Counter
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

logging.getLogger().setLevel(logging.CRITICAL)

from ocr.sc_ocr.colon_anchor import find_colons
from ocr.sc_ocr.hud_panel_tracker import DEFAULT_OFFSETS, HudPanelTracker


def _tier(p: str) -> str:
    base = p[:-4]
    if os.path.exists(base + ".skip"):
        return "SKIP"
    if os.path.exists(base + ".boxes.json"):
        return "ANNOTATED"
    return "UNREVIEWED"


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    panels = sorted(glob.glob(pat))

    # Find NOT_LOCKED panels and categorize by colon count.
    fails_by_tier_colons: dict[str, Counter] = {
        "ANNOTATED": Counter(),
        "UNREVIEWED": Counter(),
        "SKIP": Counter(),
    }
    examples: dict[str, list] = {
        "no_hud_likely": [],     # 0-1 colons
        "partial_hud": [],       # 2-4 colons
        "full_hud_likely": [],   # 5+ colons
    }

    for p in panels:
        tier = _tier(p)
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            r = t._cold_start(img)
        except Exception:
            r = None
        if r is not None:
            continue  # locked, not a failure

        try:
            colons = find_colons(
                img, y_band=(0, img.height),
                x_range=(0, int(img.width * 0.75)),
            )
            n_colons = len(colons)
        except Exception:
            n_colons = -1

        if n_colons <= 1:
            bucket = "no_hud_likely"
        elif n_colons <= 4:
            bucket = "partial_hud"
        else:
            bucket = "full_hud_likely"
        fails_by_tier_colons[tier][bucket] += 1
        if len(examples[bucket]) < 5:
            examples[bucket].append((tier, os.path.basename(p), n_colons))

    print("Failure categorization by visible HUD content (colon count):\n")
    for tier in ("ANNOTATED", "UNREVIEWED", "SKIP"):
        total = sum(fails_by_tier_colons[tier].values())
        print(f"  {tier} (n={total} failures):")
        for bucket in ("no_hud_likely", "partial_hud", "full_hud_likely"):
            cnt = fails_by_tier_colons[tier].get(bucket, 0)
            print(f"    {bucket:25s} {cnt}")

    print("\nExample failures per bucket:")
    for bucket, items in examples.items():
        if not items:
            continue
        print(f"  {bucket}:")
        for tier, name, n in items:
            print(f"    [{tier:>10s}]  {n:>2} colons  {name}")

    # Total potentially recoverable:
    partial = sum(c.get("partial_hud", 0) for c in fails_by_tier_colons.values())
    full = sum(c.get("full_hud_likely", 0) for c in fails_by_tier_colons.values())
    nohud = sum(c.get("no_hud_likely", 0) for c in fails_by_tier_colons.values())
    print(f"\nSUMMARY across all 241 panels:")
    print(f"  Likely no HUD content   (0-1 colons): {nohud}  <- unrecoverable")
    print(f"  Partial HUD content     (2-4 colons): {partial}  <- maybe recoverable")
    print(f"  Full HUD content        (5+ colons):  {full}  <- LIKELY RECOVERABLE")
    print(f"\n  If we rescue all full-HUD failures: +{full} panels")
    print(f"  -> would push total from 203 to {203 + full} / 241 = "
          f"{100*(203+full)/241:.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
