"""Baseline measurement harness for the SCAN RESULTS title detector.

Runs ``ocr.sc_ocr.scan_results_match.find_scan_results_anchor`` over every
``(.png, .boxes.json)`` pair in the labeled training capture directory and
reports localization accuracy. The numbers it produces are the bar a new
HUD tracker has to beat — without this baseline we can't tell whether a
new detector is actually an improvement.

The detector returns the title bbox (x, y, w, h, score) or None. The
ground-truth ``scan_results`` entry in each ``.boxes.json`` sidecar is
also a title bbox, so we compare title-vs-title — fine for a baseline.

Usage (from the Mining_Signals directory or with sys.path including it)::

    python hud_tracker/harness.py

Outputs ``hud_tracker/baseline_report.json``.
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from typing import Optional

# Make ``ocr`` importable when this is run directly from any cwd.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_TOOL_DIR = os.path.normpath(os.path.join(_THIS_DIR, ".."))
if _TOOL_DIR not in sys.path:
    sys.path.insert(0, _TOOL_DIR)

from PIL import Image  # noqa: E402

from ocr.sc_ocr.scan_results_match import find_scan_results_anchor  # noqa: E402


LABELED_DIR = (
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region1"
)

REPORT_PATH = os.path.join(_THIS_DIR, "baseline_report.json")

# Score threshold above which a "wrong" prediction (low IoU) is dangerous —
# the detector returned a confident match that isn't the title. These are
# the failures that cause downstream parsers to lock onto the wrong row
# (e.g. snapping the SCAN RESULTS anchor onto the MASS row).
CONFIDENT_SCORE = 0.5
WRONG_IOU_MAX = 0.3
CORRECT_IOU_MIN = 0.5


def iou(a: dict, b: dict) -> float:
    """Standard axis-aligned IoU on dicts of the form {x, y, w, h}.

    Returns 0.0 if either rect has zero area or they don't overlap.
    """
    ax1, ay1 = a["x"], a["y"]
    ax2, ay2 = ax1 + a["w"], ay1 + a["h"]
    bx1, by1 = b["x"], b["y"]
    bx2, by2 = bx1 + b["w"], by1 + b["h"]

    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(0, a["w"]) * max(0, a["h"])
    b_area = max(0, b["w"]) * max(0, b["h"])
    union = a_area + b_area - inter
    if union <= 0:
        return 0.0
    return float(inter) / float(union)


def percentile(values: list[float], p: float) -> Optional[float]:
    """Linear-interpolated percentile. ``p`` in [0, 100]."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return float(s[0])
    k = (len(s) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return float(s[f] + (s[c] - s[f]) * (k - f))


def load_pairs(directory: str) -> list[tuple[str, str, dict]]:
    """Return ``(png_path, boxes_path, gt_bbox)`` for every labeled capture.

    Only captures whose ``.boxes.json`` contains a non-empty
    ``boxes.scan_results`` block are returned — boxes files with empty or
    missing ``scan_results`` are silently skipped (nothing to evaluate).

    User-flagged ``.skip``-marker files are also honored: a sibling file
    of the form ``<capture-stem>.skip`` (e.g. ``cap_20260418_160145_257.skip``
    next to ``cap_20260418_160145_257.png``) excludes that capture from
    evaluation entirely. Lets the user mark bad captures (motion blur,
    occlusion, mislabeled GT) without having to delete the underlying
    PNG / JSON pair, so the same skip decisions persist across
    reruns. Without this, the harness's accuracy numbers got diluted
    with captures the user already declared unrepresentative.
    """
    pairs: list[tuple[str, str, dict]] = []
    if not os.path.isdir(directory):
        return pairs
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".boxes.json"):
            continue
        boxes_path = os.path.join(directory, name)
        try:
            with open(boxes_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            continue
        boxes = meta.get("boxes") or {}
        gt = boxes.get("scan_results")
        if not gt or not all(k in gt for k in ("x", "y", "w", "h")):
            continue
        png_name = meta.get("image") or name.replace(".boxes.json", ".png")
        png_path = os.path.join(directory, png_name)
        if not os.path.isfile(png_path):
            continue
        # Skip-marker check: a sibling ``<stem>.skip`` file means the
        # user has flagged this capture for exclusion. Cheap stat-only
        # probe — no read of the marker file's contents needed.
        skip_path = os.path.splitext(png_path)[0] + ".skip"
        if os.path.exists(skip_path):
            continue
        pairs.append((png_path, boxes_path, gt))
    return pairs


def run() -> dict:
    pairs = load_pairs(LABELED_DIR)

    per_capture: list[dict] = []
    timings_ms: list[float] = []
    ious_for_detected: list[float] = []
    detections = 0
    correct = 0
    confident_wrong = 0

    # Warm up the template + scale variant cache so per-frame timings
    # reflect steady-state detector cost, not one-off template load.
    # Without this, the first frame is dominated by ``np.load`` + the
    # PIL resize sweep that builds 8 scale variants — irrelevant to the
    # baseline a real-time tracker has to beat.
    if pairs:
        try:
            warm_img = Image.open(pairs[0][0])
            warm_img.load()
            find_scan_results_anchor(warm_img)
            warm_img.close()
        except Exception:
            pass

    for png_path, boxes_path, gt in pairs:
        try:
            img = Image.open(png_path)
            img.load()
        except Exception as exc:
            per_capture.append({
                "capture": os.path.basename(png_path),
                "error": f"open_failed: {exc}",
                "gt": gt,
                "pred": None,
                "iou": None,
                "ms": None,
            })
            continue

        # Time only the detector call so the baseline reflects detector
        # cost, not file I/O. Use perf_counter for sub-ms resolution.
        t0 = time.perf_counter()
        try:
            anchor = find_scan_results_anchor(img)
        except Exception as exc:
            per_capture.append({
                "capture": os.path.basename(png_path),
                "error": f"detect_failed: {exc}",
                "gt": gt,
                "pred": None,
                "iou": None,
                "ms": (time.perf_counter() - t0) * 1000.0,
            })
            continue
        ms = (time.perf_counter() - t0) * 1000.0
        timings_ms.append(ms)

        if anchor is None:
            per_capture.append({
                "capture": os.path.basename(png_path),
                "gt": gt,
                "pred": None,
                "iou": 0.0,
                "score": None,
                "ms": ms,
            })
            continue

        detections += 1
        pred = {
            "x": int(anchor["title_x"]),
            "y": int(anchor["title_y"]),
            "w": int(anchor["title_w"]),
            "h": int(anchor["title_h"]),
        }
        score = float(anchor.get("score", 0.0))
        i = iou(pred, gt)
        ious_for_detected.append(i)
        if i >= CORRECT_IOU_MIN:
            correct += 1
        if score > CONFIDENT_SCORE and i < WRONG_IOU_MAX:
            confident_wrong += 1

        per_capture.append({
            "capture": os.path.basename(png_path),
            "gt": gt,
            "pred": pred,
            "score": score,
            "iou": i,
            "ms": ms,
        })

    total = len(pairs)
    detected_with_iou = [c for c in per_capture if c.get("pred") is not None]
    # Worst-by-IoU ranking is over DETECTED captures — non-detections
    # already show up as detection-rate misses; ranking them as "worst"
    # would flood the list and obscure the dangerous false-positive cases.
    worst20 = sorted(
        detected_with_iou,
        key=lambda c: (c.get("iou") if c.get("iou") is not None else 0.0),
    )[:20]

    summary: dict = {
        "labeled_dir": LABELED_DIR,
        "total_captures": total,
        "detection_rate": (detections / total) if total else None,
        "correct_localization_rate": (correct / total) if total else None,
        "median_iou_overall": percentile(
            [c.get("iou") or 0.0 for c in per_capture], 50.0
        ),
        "p90_iou_overall": percentile(
            [c.get("iou") or 0.0 for c in per_capture], 90.0
        ),
        "median_iou_detected": percentile(ious_for_detected, 50.0),
        "p90_iou_detected": percentile(ious_for_detected, 90.0),
        "mean_ms_per_frame": (
            statistics.fmean(timings_ms) if timings_ms else None
        ),
        "median_ms_per_frame": (
            statistics.median(timings_ms) if timings_ms else None
        ),
        "confident_but_wrong_count": confident_wrong,
        "thresholds": {
            "correct_iou_min": CORRECT_IOU_MIN,
            "wrong_iou_max": WRONG_IOU_MAX,
            "confident_score_min": CONFIDENT_SCORE,
        },
        "worst20": worst20,
        "per_capture": per_capture,
    }

    return summary


def main() -> int:
    report = run()
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    # Console summary so you can eyeball the result without opening JSON.
    print(f"Labeled dir: {report['labeled_dir']}")
    print(f"Total captures: {report['total_captures']}")
    if report["total_captures"]:
        det = report["detection_rate"] or 0.0
        cor = report["correct_localization_rate"] or 0.0
        print(f"Detection rate: {det:.3f}")
        print(f"Correct-localization rate (IoU>{CORRECT_IOU_MIN}): {cor:.3f}")
        print(f"Median IoU (overall): {report['median_iou_overall']}")
        print(f"p90 IoU (overall): {report['p90_iou_overall']}")
        print(f"Median IoU (detected): {report['median_iou_detected']}")
        print(f"p90 IoU (detected): {report['p90_iou_detected']}")
        print(f"Mean ms/frame: {report['mean_ms_per_frame']}")
        print(
            f"Confident-but-wrong (score>{CONFIDENT_SCORE}, IoU<{WRONG_IOU_MAX}): "
            f"{report['confident_but_wrong_count']}"
        )
        print("Worst 5:")
        for c in report["worst20"][:5]:
            print(
                f"  {c['capture']}  iou={c['iou']:.3f}  "
                f"pred={c.get('pred')}  gt={c.get('gt')}"
            )
    print(f"Report written to: {REPORT_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
