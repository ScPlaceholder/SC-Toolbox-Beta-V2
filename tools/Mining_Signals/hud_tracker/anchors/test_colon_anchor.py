"""Smoke test for ``ocr.sc_ocr.colon_anchor.find_colons``.

Loads a handful of labelled HUD captures and verifies that
``find_colons`` returns 3 colons (one per value row) whose Y-centers
align (within tolerance) with the labelled ``mass_row`` /
``resistance_row`` / ``instability_row`` Y-midlines.

Run under production Python 3.14::

    "C:\\Users\\_user\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe" \\
        hud_tracker\\anchors\\test_colon_anchor.py

Exits 0 if at least 80% of captures produce 3 matching colons.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ANCHORS_DIR = Path(__file__).resolve().parent
_HUD_TRACKER_DIR = _ANCHORS_DIR.parent
_MINING_SIGNALS_DIR = _HUD_TRACKER_DIR.parent
if str(_MINING_SIGNALS_DIR) not in sys.path:
    sys.path.insert(0, str(_MINING_SIGNALS_DIR))

from PIL import Image  # noqa: E402

from ocr.sc_ocr.colon_anchor import find_colons, reset_cache  # noqa: E402

# Tolerance: a colon's Y-center may be a few pixels off the
# row-bbox midline due to the row-bbox padding plus colon-template
# half-height alignment.
_Y_TOLERANCE_PX = 12


def main() -> int:
    panel_root = Path(
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
        r"\training_data_panels\user_20260418_154408\region1"
    )
    if not panel_root.is_dir():
        print(f"FAIL: panel root not found: {panel_root}")
        return 1
    captures = sorted(panel_root.glob("*.boxes.json"))[:20]
    if not captures:
        print("FAIL: no labelled captures found")
        return 1
    print(f"Loaded {len(captures)} labelled captures from {panel_root}")
    passes = 0
    for boxes_path in captures:
        with open(boxes_path) as f:
            boxes = json.load(f)["boxes"]
        if "mass_row" not in boxes or "resistance_row" not in boxes \
                or "instability_row" not in boxes:
            continue
        png_path = boxes_path.with_suffix("").with_suffix(".png")
        if not png_path.is_file():
            continue
        img = Image.open(png_path).convert("RGB")
        reset_cache()
        mr = boxes["mass_row"]
        rr = boxes["resistance_row"]
        ir = boxes["instability_row"]
        expected = {
            "mass": mr["y"] + mr["h"] // 2,
            "resistance": rr["y"] + rr["h"] // 2,
            "instability": ir["y"] + ir["h"] // 2,
        }
        # Slightly wider Y band than the labelled rows to mimic the
        # production caller (which uses search_origin..+200).
        y_top = max(0, mr["y"] - 15)
        y_bot = ir["y"] + ir["h"] + 15
        colons = find_colons(
            img, y_band=(y_top, y_bot), x_range=(80, img.size[0] - 120),
        )
        # For each expected row, find the closest detected colon.
        hits = 0
        per_row: dict[str, tuple[int, float]] = {}
        for name, ey in expected.items():
            best: tuple[int, float, int] = (10**9, 0.0, 0)  # (delta, score, n)
            for c in colons:
                cy = c["y"] + c["h"] // 2
                d = abs(cy - ey)
                if d < best[0]:
                    best = (d, float(c["score"]), cy)
            per_row[name] = best
            if best[0] <= _Y_TOLERANCE_PX:
                hits += 1
        ok = (hits == 3)
        if ok:
            passes += 1
        status = "PASS" if ok else "FAIL"
        print(
            f"  {status} {png_path.name} - found {len(colons)} colons, "
            f"3/3 expected match? {hits}/3"
        )
        for name in ("mass", "resistance", "instability"):
            d, sc, cy = per_row[name]
            print(
                f"    {name}: expected y={expected[name]}, "
                f"nearest cy={cy} (delta={d}, score={sc:.3f})"
            )
    total = passes + (len(captures) - passes)
    rate = passes / max(1, len(captures))
    print(f"\n3-colon pass rate: {passes}/{len(captures)} ({rate:.1%})")
    if rate < 0.80:
        print(f"FAIL: pass rate below 80%")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
