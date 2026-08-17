"""Test runner for ``find_icon_by_geometry``.

Reports detection rate per source, mean IoU on ground-truth-labeled
region2 captures, mean ms/frame, and a check-failure breakdown.

Run under production python:
  "C:/Users/_user/AppData/Local/SC_Toolbox/current/python/python.exe" \
      hud_tracker/anchors/test_icon_geometry.py
"""

from __future__ import annotations

import glob
import json
import os
import random
import statistics
import sys
import time
from collections import Counter
from typing import Any

import numpy as np
from PIL import Image

# Make hud_tracker.anchors importable when invoked as a script
HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hud_tracker.anchors.icon_geometry import find_icon_by_geometry  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ICON_DIR = (
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_pending_review_signal\icon"
)
BAD_CROP_PATH = (
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals"
    r"\training_data_blacklist\bad crop.png"
)
# `user_20260418_154408/region2/` lives in roaming on this machine.
REGION2_DIR = (
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region2"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ax1, ay1 = ax0 + aw, ay0 + ah
    bx1, by1 = bx0 + bw, by0 + bh
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _open_rgb(path: str) -> np.ndarray | None:
    try:
        return np.asarray(Image.open(path).convert("RGB"))
    except Exception:
        return None


def _detect(image: np.ndarray, hud_bbox: tuple[int, int, int, int] | None = None) -> tuple[dict | None, float]:
    t0 = time.perf_counter()
    res = find_icon_by_geometry(image, hud_bbox=hud_bbox)
    return res, (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------


def test_real_icons() -> dict[str, Any]:
    """Detect on the 6 already-extracted icon crops (small isolated tiles)."""
    paths = sorted(glob.glob(os.path.join(ICON_DIR, "pending_*_rgb.png")))
    if not paths:
        return {"count": 0, "detections": 0, "fail_paths": []}

    detections = 0
    times: list[float] = []
    fail_checks: Counter = Counter()
    fail_paths: list[str] = []
    for p in paths:
        rgb = _open_rgb(p)
        if rgb is None:
            fail_paths.append(p)
            continue
        res, ms = _detect(rgb)
        times.append(ms)
        if res is not None:
            detections += 1
        else:
            fail_paths.append(p)
            # Run again to capture which checks would have failed if we
            # forced a return — easier: re-evaluate at low threshold.
            # (icon_geometry.py doesn't expose internals at sub-threshold,
            # so we just record the path.)
    return {
        "count": len(paths),
        "detections": detections,
        "fail_paths": fail_paths,
        "ms_mean": (statistics.mean(times) if times else 0.0),
        "ms_max": (max(times) if times else 0.0),
        "fail_checks": dict(fail_checks),
    }


def test_bad_crop() -> dict[str, Any]:
    rgb = _open_rgb(BAD_CROP_PATH)
    if rgb is None:
        return {"available": False}
    res, ms = _detect(rgb)
    return {
        "available": True,
        "detected": res is not None,
        "ms": ms,
        "result": res,
    }


def test_region2_sample(n: int = 20, seed: int = 0, force_include_labeled: bool = True) -> dict[str, Any]:
    """Run on a random sample of region2 panels; compute IoU where labeled.

    When ``force_include_labeled`` is True, every PNG with a sibling
    ``*.boxes.json`` is included automatically and the random sample fills
    out the remaining slots — that way the IoU statistic always covers
    the labeled set.
    """
    paths = sorted(glob.glob(os.path.join(REGION2_DIR, "*.png")))
    if not paths:
        return {"available": False}

    labeled_paths = [p for p in paths if os.path.exists(os.path.splitext(p)[0] + ".boxes.json")]
    other_paths = [p for p in paths if p not in labeled_paths]

    rng = random.Random(seed)
    if force_include_labeled:
        chosen = list(labeled_paths)
        remaining = max(0, n - len(chosen))
        if remaining and other_paths:
            chosen += rng.sample(other_paths, min(remaining, len(other_paths)))
        sample = chosen
    else:
        sample = rng.sample(paths, min(n, len(paths)))

    times: list[float] = []
    detections = 0
    crashes = 0
    ious: list[float] = []
    labeled = 0
    fail_check_counter: Counter = Counter()
    pass_check_counter: Counter = Counter()
    per_image: list[dict[str, Any]] = []

    for p in sample:
        rgb = _open_rgb(p)
        if rgb is None:
            crashes += 1
            continue
        boxes_path = os.path.splitext(p)[0] + ".boxes.json"
        gt_icon: tuple[int, int, int, int] | None = None
        hud_bbox = None
        if os.path.exists(boxes_path):
            try:
                with open(boxes_path, "r", encoding="utf-8") as f:
                    bj = json.load(f)
                boxes = bj.get("boxes", {})
                ic = boxes.get("icon")
                if ic and {"x", "y", "w", "h"} <= ic.keys():
                    gt_icon = (int(ic["x"]), int(ic["y"]), int(ic["w"]), int(ic["h"]))
                    labeled += 1
                hb = bj.get("hud_bbox")
                if hb and {"x", "y", "w", "h"} <= hb.keys():
                    hud_bbox = (int(hb["x"]), int(hb["y"]), int(hb["w"]), int(hb["h"]))
            except Exception:
                pass
        try:
            res, ms = _detect(rgb, hud_bbox=hud_bbox)
        except Exception as exc:
            crashes += 1
            per_image.append({"path": os.path.basename(p), "crash": repr(exc)})
            continue

        times.append(ms)
        record: dict[str, Any] = {"path": os.path.basename(p), "ms": round(ms, 2)}
        if res is None:
            record["detected"] = False
        else:
            detections += 1
            record["detected"] = True
            record["bbox"] = res["bbox"]
            record["confidence"] = round(res["confidence"], 2)
            checks = res.get("details", {}).get("checks", {})
            for cname, ok in checks.items():
                if ok:
                    pass_check_counter[cname] += 1
                else:
                    fail_check_counter[cname] += 1
            if gt_icon is not None:
                iou = _iou(res["bbox"], gt_icon)
                ious.append(iou)
                record["iou"] = round(iou, 3)
                record["gt"] = gt_icon
        per_image.append(record)

    return {
        "available": True,
        "sampled": len(sample),
        "detections": detections,
        "crashes": crashes,
        "labeled": labeled,
        "iou_mean": (statistics.mean(ious) if ious else 0.0),
        "iou_n": len(ious),
        "ms_mean": (statistics.mean(times) if times else 0.0),
        "ms_max": (max(times) if times else 0.0),
        "fail_checks": dict(fail_check_counter),
        "pass_checks": dict(pass_check_counter),
        "per_image": per_image,
    }


def test_defensive() -> dict[str, Any]:
    cases: list[tuple[str, Any]] = [
        ("None", None),
        ("empty_array", np.zeros((0, 0, 3), dtype=np.uint8)),
        ("tiny_array", np.zeros((2, 2, 3), dtype=np.uint8)),
        ("solid_black", np.zeros((50, 50, 3), dtype=np.uint8)),
        ("solid_cyan", np.tile(np.array([[0, 255, 255]], dtype=np.uint8), (50, 50, 1))),
    ]
    out: dict[str, Any] = {}
    for name, x in cases:
        try:
            r = find_icon_by_geometry(x)
            out[name] = "None" if r is None else "Detected"
        except Exception as exc:
            out[name] = f"RAISED: {exc!r}"
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_section(title: str) -> None:
    bar = "=" * len(title)
    print()
    print(title)
    print(bar)


def main() -> None:
    print("find_icon_by_geometry test report")
    print("=================================")

    _print_section("[1] Defensive behavior")
    for name, status in test_defensive().items():
        print(f"  {name:14s}: {status}")

    _print_section("[2] Real icons (training_data_pending_review_signal/icon)")
    r1 = test_real_icons()
    n = r1["count"]
    d = r1["detections"]
    print(f"  Detected: {d}/{n}  ({d/max(1,n):.0%})")
    print(f"  ms/frame: mean {r1['ms_mean']:.2f}, max {r1['ms_max']:.2f}")
    if r1["fail_paths"]:
        print("  Failures:")
        for p in r1["fail_paths"]:
            print(f"    - {os.path.basename(p)}")

    _print_section("[3] Blacklist source (training_data_blacklist/bad crop.png)")
    r2 = test_bad_crop()
    if not r2.get("available"):
        print("  bad crop.png not available")
    else:
        verdict = "DETECTED (icon present in source)" if r2["detected"] else "rejected"
        print(f"  {verdict}; {r2['ms']:.2f} ms")
        if r2["detected"]:
            res = r2["result"]
            print(f"    bbox={res['bbox']}  conf={res['confidence']:.2f}  "
                  f"score={res['details']['score']}/6  "
                  f"checks={res['details']['checks']}")

    _print_section("[4] region2 random sample (n=20)")
    r3 = test_region2_sample(20, seed=0)
    if not r3.get("available"):
        print("  region2 directory not available")
    else:
        print(f"  Sampled: {r3['sampled']}, detections: {r3['detections']}, crashes: {r3['crashes']}")
        print(f"  Labeled: {r3['labeled']}, IoU n={r3['iou_n']}, mean IoU={r3['iou_mean']:.3f}")
        print(f"  ms/frame: mean {r3['ms_mean']:.2f}, max {r3['ms_max']:.2f}")
        print(f"  Pass-check counts:  {r3['pass_checks']}")
        print(f"  Fail-check counts:  {r3['fail_checks']}")
        print("  Per-image:")
        for rec in r3["per_image"]:
            line = f"    {rec.get('path'):<32s}"
            if "crash" in rec:
                line += f" CRASH {rec['crash']}"
            elif rec.get("detected"):
                line += f" det conf={rec.get('confidence', 0):.2f}"
                if "iou" in rec:
                    line += f" iou={rec['iou']:.2f} gt={rec['gt']} pred={rec['bbox']}"
                else:
                    line += f" pred={rec['bbox']}"
            else:
                line += " no-detect"
            line += f" ({rec.get('ms', 0):.1f} ms)"
            print(line)

    _print_section("Summary")
    real_pct = r1["detections"] / max(1, r1["count"])
    print(f"  Real icons:        {r1['detections']}/{r1['count']} ({real_pct:.0%})")
    if r3.get("available"):
        print(f"  region2 detect:    {r3['detections']}/{r3['sampled']} (mean IoU {r3['iou_mean']:.3f} on {r3['iou_n']} labeled)")
        print(f"  region2 ms mean:   {r3['ms_mean']:.2f}")
    print(f"  bad-crop detected: {r2.get('detected', 'n/a')}")


if __name__ == "__main__":
    main()
