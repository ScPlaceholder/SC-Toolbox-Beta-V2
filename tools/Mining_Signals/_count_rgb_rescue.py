"""Count how many times the RGB rescue path fires across 241 panels."""
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

# Capture WARN-level "RGB rescued" log messages.
_log_msgs: list[str] = []


class _CaptureHandler(logging.Handler):
    def emit(self, record):
        _log_msgs.append(record.getMessage())


logging.getLogger().setLevel(logging.INFO)
lg = logging.getLogger("ocr.sc_ocr.label_match")
lg.setLevel(logging.INFO)
lg.addHandler(_CaptureHandler())
lg.propagate = False

from ocr.sc_ocr.hud_panel_tracker import DEFAULT_OFFSETS, HudPanelTracker


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    panels = sorted(glob.glob(pat))
    print(f"Scanning {len(panels)} panels for RGB rescue activity...\n")

    counters = Counter()
    for p in panels:
        _log_msgs.clear()
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            t._cold_start(img)
        except Exception:
            continue
        for m in _log_msgs:
            if "RGB rescued" in m:
                counters["row_rgb_rescue"] += 1
            elif "AGREE" in m and "MASS" in m:
                counters["mass_agree"] += 1
            elif "DISAGREES" in m and "MASS" in m:
                counters["mass_disagree"] += 1
            elif "swapping to" in m:
                counters["mass_swap"] += 1
            elif "and RGB" in m and "AGREE" in m:
                counters["row_agree"] += 1

    print("Voting/rescue event counts across 241 panels:")
    for k, v in counters.most_common():
        print(f"  {k:30s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
