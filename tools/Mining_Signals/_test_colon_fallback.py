"""TEMPORARY test harness for v2.2.13 colon-fallback validation.

Runs HudPanelTracker._cold_start against every captured user HUD
panel and tallies lock success vs failure modes. Reports how many
panels needed the new colon fallback to reach 3 anchors, how many
fell short anyway (colon found 0), and how many would have locked
even without the fallback.

NOT committed -- run locally, read output, delete.
"""
from __future__ import annotations

import glob
import logging
import os
import sys
import time
from collections import Counter
from pathlib import Path

# Make production imports work (mirror skill_bootstrap behaviour).
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from ocr.sc_ocr.hud_panel_tracker import (  # noqa: E402
    DEFAULT_OFFSETS,
    HudPanelTracker,
)

# Capture every WARNING+ log from the tracker for tallying.
_log_msgs: list[str] = []


class _CaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _log_msgs.append(record.getMessage())


logging.getLogger().setLevel(logging.WARNING)
for ln in (
    "ocr.sc_ocr.hud_panel_tracker",
    "ocr.sc_ocr.colon_anchor",
    "ocr.sc_ocr.scan_results_match",
    "ocr.sc_ocr.label_match",
):
    lg = logging.getLogger(ln)
    lg.setLevel(logging.WARNING)
    lg.addHandler(_CaptureHandler())
    lg.propagate = False


def _scan_dirs() -> list[str]:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    return sorted(glob.glob(pat))


def main() -> int:
    images = _scan_dirs()
    print(f"found {len(images)} captures under training_data_panels/user_*/region1/")
    if not images:
        return 1

    def _annotation_status(p: str) -> str:
        base = p[:-4]  # strip .png
        if os.path.exists(base + ".skip"):
            return "SKIP"
        if os.path.exists(base + ".boxes.json"):
            return "ANNOTATED"
        return "UNREVIEWED"

    # tier -> counter of outcomes
    by_tier: dict[str, Counter[str]] = {
        "ANNOTATED": Counter(),
        "UNREVIEWED": Counter(),
        "SKIP": Counter(),
    }
    t0 = time.time()
    for i, img_path in enumerate(images):
        _log_msgs.clear()
        tier = _annotation_status(img_path)
        c = by_tier[tier]
        c["total"] += 1
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            c["load_failed"] += 1
            continue
        tracker = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            result = tracker._cold_start(img)
        except Exception as exc:
            c["EXCEPTION"] += 1
            if c["EXCEPTION"] <= 1:
                print(f"  EXC ({tier}) {os.path.basename(img_path)}: {exc}")
            continue
        if result is not None:
            c["LOCKED"] += 1
        else:
            c["NOT_LOCKED"] += 1
        saw_synth = False
        saw_zero = False
        for m in _log_msgs:
            if "colon fallback synthesized" in m:
                saw_synth = True
            elif "colon fallback ran but found 0" in m:
                saw_zero = True
        if saw_synth:
            c["colon_synthesized"] += 1
            if result is not None:
                c["colon_synth_AND_locked"] += 1
        if saw_zero:
            c["colon_zero_found"] += 1
        if (i + 1) % 50 == 0:
            elapsed = time.time() - t0
            ann_lk = by_tier['ANNOTATED']['LOCKED']
            ann_tot = by_tier['ANNOTATED']['total']
            print(f"  ... {i+1}/{len(images)} in {elapsed:.1f}s (ANNOTATED locked={ann_lk}/{ann_tot})")

    print()
    for tier in ("ANNOTATED", "UNREVIEWED", "SKIP"):
        c = by_tier[tier]
        if not c:
            continue
        tot = c["total"]
        lk = c["LOCKED"]
        nl = c["NOT_LOCKED"]
        rate = (lk * 100.0 / tot) if tot else 0.0
        print(f"=== {tier} ({tot} panels)  lock rate: {lk}/{tot} ({rate:.1f}%) ===")
        for k, v in c.most_common():
            if k in ("total",):
                continue
            print(f"  {k:30s} {v}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
