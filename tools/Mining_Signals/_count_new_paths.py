"""Count: (a) panel-presence pre-filter activations, (b) colon-anchor
fallback in scan_results-anchored mode."""
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

_msgs: list[str] = []


class _Capture(logging.Handler):
    def emit(self, record):
        _msgs.append(record.getMessage())


logging.getLogger().setLevel(logging.DEBUG)
for n in ("ocr.sc_ocr.hud_panel_tracker", "ocr.sc_ocr.label_match"):
    lg = logging.getLogger(n)
    lg.setLevel(logging.DEBUG)
    lg.addHandler(_Capture())
    lg.propagate = False

from ocr.sc_ocr.hud_panel_tracker import DEFAULT_OFFSETS, HudPanelTracker


def main() -> int:
    pat = str(ROOT / "training_data_panels" / "user_*" / "region1" / "*.png")
    panels = sorted(glob.glob(pat))

    counts = Counter()
    for p in panels:
        _msgs.clear()
        try:
            img = Image.open(p).convert("RGB")
        except Exception:
            continue
        t = HudPanelTracker(offsets=DEFAULT_OFFSETS)
        try:
            r = t._cold_start(img)
        except Exception:
            continue
        for m in _msgs:
            if "panel-presence check" in m:
                counts["pre_filter_rejected"] += 1
            elif "colon fallback (scan_results-anchored)" in m and "synthesized" in m:
                counts["colon_sr_anchored_synth"] += 1
            elif "colon fallback (scan_results-anchored)" in m and "found 0" in m:
                counts["colon_sr_anchored_zero"] += 1
            elif "colon fallback (label_mass-anchored)" in m and "synthesized" in m:
                counts["colon_mass_anchored_synth"] += 1

    print(f"Across 241 panels:")
    for k, v in counts.most_common():
        print(f"  {k:35s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
