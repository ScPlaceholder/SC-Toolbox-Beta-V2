"""Smoke + IoU tests for ``hud_tracker.anchors.chrome_lines``.

Run under production Python 3.14::

    "C:\\Users\\_user\\AppData\\Local\\SC_Toolbox\\current\\python\\python.exe" \\
        hud_tracker\\anchors\\test_chrome_lines.py

What this script does
---------------------
1. Loads the two labelled captures in
   ``training_data_panels/user_20260418_154408/region1`` that have
   ground-truth ``top_line`` / ``bot_line`` boxes
   (``cap_20260418_155705_329`` and ``cap_20260418_155707_070``).
2. Calls :func:``find_chrome_lines`` on each and reports the IoU
   between predicted and ground-truth boxes for ``top_line`` and
   ``bot_line`` separately.
3. Sanity-runs the detector on the first twenty unlabelled captures
   in ``training_data_panels/user_20260418_081525/region1`` and
   verifies for each that:
     - the call does not raise
     - if both lines are returned, ``top.y < bot.y``
     - all returned bboxes lie within the image bounds
4. Prints a summary including mean ms/frame across all calls.

Exit code is 0 on success (both labelled captures hit IoU >= 0.40
on each line, and the unlabelled sanity test passes), 1 on any
failure.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Allow running this file as a script from any cwd by inserting the
# Mining_Signals root onto sys.path BEFORE the package import.
_ANCHORS_DIR = Path(__file__).resolve().parent
_HUD_TRACKER_DIR = _ANCHORS_DIR.parent
_MINING_SIGNALS_DIR = _HUD_TRACKER_DIR.parent
if str(_MINING_SIGNALS_DIR) not in sys.path:
    sys.path.insert(0, str(_MINING_SIGNALS_DIR))

# Defer importing PIL / numpy until after sys.path is set so the
# stack trace is cleaner if production python is misconfigured.
from PIL import Image  # noqa: E402

from hud_tracker.anchors.chrome_lines import find_chrome_lines  # noqa: E402


# ─────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────
TRAINING_ROOT = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)
LABELLED_DIR = TRAINING_ROOT / "user_20260418_154408" / "region1"
UNLABELLED_DIR = TRAINING_ROOT / "user_20260418_081525" / "region1"

LABELLED_CAPTURES = [
    "cap_20260418_155705_329",
    "cap_20260418_155707_070",
]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def bbox_iou(a: dict, b: dict) -> float:
    """IoU between two ``{"x","y","w","h"}`` boxes. Returns 0.0 on degenerate."""
    if a is None or b is None:
        return 0.0
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = a["x"] + a["w"], a["y"] + a["h"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = b["x"] + b["w"], b["y"] + b["h"]
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
    area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0.0
    return inter / float(union)


def fmt_bbox(b: dict | None) -> str:
    if b is None:
        return "None"
    base = (
        f"x={b['x']:>4} y={b['y']:>4} w={b['w']:>4} h={b['h']:>3}"
    )
    if "score" in b:
        return f"{base} score={b['score']:.3f}"
    return base


# ─────────────────────────────────────────────────────────────
# Test 1: IoU on labelled captures
# ─────────────────────────────────────────────────────────────

def test_labelled() -> tuple[bool, list[float], list[tuple[str, float, float]]]:
    """Run the labelled-IoU test.

    Returns ``(all_loaded_ok, timings_ms, [(stem, iou_top, iou_bot), ...])``.

    ``all_loaded_ok`` only fails if a labelled file can't be loaded —
    IoU values are reported but do NOT gate the success flag, because
    this script's purpose is to characterise the detector's IoU on
    real captures rather than enforce a regression threshold (the
    legacy fill-ratio detector emits 1-3 px strokes while the GT
    boxes include the surrounding end-notch margins, so IoU is
    inherently capped by the height mismatch even when the stroke
    is centred correctly).
    """
    print("=" * 78)
    print("Test 1 — IoU on labelled captures")
    print("=" * 78)

    timings_ms: list[float] = []
    iou_results: list[tuple[str, float, float]] = []
    all_loaded_ok = True

    for stem in LABELLED_CAPTURES:
        img_path = LABELLED_DIR / f"{stem}.png"
        boxes_path = LABELLED_DIR / f"{stem}.boxes.json"
        if not img_path.is_file() or not boxes_path.is_file():
            print(f"  MISSING: {img_path} or {boxes_path}")
            all_loaded_ok = False
            continue

        with open(boxes_path, "r", encoding="utf-8") as f:
            gt = json.load(f).get("boxes", {})
        gt_top = gt.get("top_line")
        gt_bot = gt.get("bot_line")

        img = Image.open(img_path)
        t0 = time.perf_counter()
        result = find_chrome_lines(img)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        timings_ms.append(elapsed_ms)

        pred_top = result["top_line"]
        pred_bot = result["bot_line"]

        iou_top = bbox_iou(gt_top, pred_top)
        iou_bot = bbox_iou(gt_bot, pred_bot)
        iou_results.append((stem, iou_top, iou_bot))

        print(f"\n  {stem}  ({elapsed_ms:6.2f} ms)")
        print(f"    GT  top: {fmt_bbox(gt_top)}")
        print(f"    PR  top: {fmt_bbox(pred_top)}")
        print(f"    IoU top: {iou_top:.3f}")
        print(f"    GT  bot: {fmt_bbox(gt_bot)}")
        print(f"    PR  bot: {fmt_bbox(pred_bot)}")
        print(f"    IoU bot: {iou_bot:.3f}")

        # Centre-y delta is a more representative quality measure for
        # 1-3 px strokes vs. fat GT boxes — print it as a diagnostic
        # so callers can judge localisation accuracy independently
        # of the height mismatch.
        if pred_top is not None and gt_top is not None:
            dy = (pred_top["y"] + pred_top["h"] / 2) - (gt_top["y"] + gt_top["h"] / 2)
            print(f"    centre-y delta top: {dy:+.1f} px")
        if pred_bot is not None and gt_bot is not None:
            dy = (pred_bot["y"] + pred_bot["h"] / 2) - (gt_bot["y"] + gt_bot["h"] / 2)
            print(f"    centre-y delta bot: {dy:+.1f} px")

    return all_loaded_ok, timings_ms, iou_results


# ─────────────────────────────────────────────────────────────
# Test 2: sanity on unlabelled captures
# ─────────────────────────────────────────────────────────────

def test_unlabelled() -> tuple[bool, list[float]]:
    print()
    print("=" * 78)
    print("Test 2 — sanity on first 20 unlabelled captures")
    print("=" * 78)

    timings_ms: list[float] = []
    all_ok = True

    pngs = sorted(UNLABELLED_DIR.glob("cap_*.png"))[:20]
    if not pngs:
        print(f"  No PNGs found under {UNLABELLED_DIR}")
        return False, timings_ms

    for img_path in pngs:
        try:
            img = Image.open(img_path)
            iw, ih = img.size
        except Exception as exc:
            print(f"  {img_path.name}: open failed {exc!r}")
            all_ok = False
            continue

        try:
            t0 = time.perf_counter()
            result = find_chrome_lines(img)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            timings_ms.append(elapsed_ms)
        except Exception as exc:
            print(f"  {img_path.name}: detector RAISED {exc!r}")
            all_ok = False
            continue

        top, bot = result["top_line"], result["bot_line"]

        # Bounds check on every returned box.
        for label, box in (("top_line", top), ("bot_line", bot)):
            if box is None:
                continue
            if not (
                0 <= box["x"] < iw
                and 0 <= box["y"] < ih
                and box["w"] > 0
                and box["h"] > 0
                and box["x"] + box["w"] <= iw
                and box["y"] + box["h"] <= ih
            ):
                print(
                    f"  {img_path.name}: {label} out of bounds "
                    f"{box} for image {iw}x{ih}"
                )
                all_ok = False

        # Ordering check when both present.
        if top is not None and bot is not None:
            if top["y"] >= bot["y"]:
                print(
                    f"  {img_path.name}: top.y {top['y']} >= bot.y {bot['y']}"
                )
                all_ok = False

        top_str = fmt_bbox(top)
        bot_str = fmt_bbox(bot)
        print(
            f"  {img_path.name} ({elapsed_ms:5.2f} ms)  "
            f"top=[{top_str}]  bot=[{bot_str}]"
        )

    return all_ok, timings_ms


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main() -> int:
    ok1, t1, iou_rows = test_labelled()
    ok2, t2 = test_unlabelled()

    all_t = t1 + t2
    mean_ms = (sum(all_t) / len(all_t)) if all_t else 0.0

    # IoU gate: every labelled capture must hit ≥ 0.40 on BOTH lines.
    iou_threshold = 0.40
    iou_pass = bool(iou_rows) and all(
        (iou_top >= iou_threshold and iou_bot >= iou_threshold)
        for _, iou_top, iou_bot in iou_rows
    )

    print()
    print("=" * 78)
    print("Summary")
    print("=" * 78)
    print(f"  labelled file load: {'PASS' if ok1 else 'FAIL'}")
    for stem, iou_top, iou_bot in iou_rows:
        gate_top = "OK " if iou_top >= iou_threshold else "LOW"
        gate_bot = "OK " if iou_bot >= iou_threshold else "LOW"
        print(
            f"    {stem}  IoU top={iou_top:.3f} [{gate_top}]  "
            f"bot={iou_bot:.3f} [{gate_bot}]"
        )
    if iou_rows:
        avg_top = sum(r[1] for r in iou_rows) / len(iou_rows)
        avg_bot = sum(r[2] for r in iou_rows) / len(iou_rows)
        print(f"    mean IoU: top={avg_top:.3f}  bot={avg_bot:.3f}")
    print(f"  IoU >= {iou_threshold:.2f} gate: {'PASS' if iou_pass else 'FAIL'}")
    print(f"  unlabelled sanity:  {'PASS' if ok2 else 'FAIL'}")
    print(f"  mean ms/frame:      {mean_ms:.2f}  (n={len(all_t)})")

    return 0 if (ok1 and ok2 and iou_pass) else 1


if __name__ == "__main__":
    sys.exit(main())
