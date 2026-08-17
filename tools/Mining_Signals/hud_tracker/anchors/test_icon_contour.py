"""Test runner for ``find_icon_by_contour``.

Reports detection rate per source, mean IoU on ground-truth-labeled
region2 captures, mean ms/frame, and (most importantly) the
agreement rate / decorrelation analysis vs. ``find_icon_by_geometry``.

Run under production python:
  "C:/Users/_user/AppData/Local/SC_Toolbox/current/python/python.exe" \
      hud_tracker/anchors/test_icon_contour.py
"""

from __future__ import annotations

import glob
import json
import os
import random
import statistics
import sys
import time
from typing import Any

import numpy as np
from PIL import Image

# Make hud_tracker.anchors importable when invoked as a script
HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hud_tracker.anchors.icon_contour import find_icon_by_contour  # noqa: E402
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


def _detect_contour(image, hud_bbox=None):
    t0 = time.perf_counter()
    res = find_icon_by_contour(image, hud_bbox=hud_bbox)
    return res, (time.perf_counter() - t0) * 1000.0


def _detect_geom(image, hud_bbox=None):
    t0 = time.perf_counter()
    res = find_icon_by_geometry(image, hud_bbox=hud_bbox)
    return res, (time.perf_counter() - t0) * 1000.0


# ---------------------------------------------------------------------------
# Test sections
# ---------------------------------------------------------------------------


def test_real_icons() -> dict[str, Any]:
    paths = sorted(glob.glob(os.path.join(ICON_DIR, "pending_*_rgb.png")))
    if not paths:
        return {"count": 0, "detections": 0, "fail_paths": []}

    detections = 0
    times: list[float] = []
    fail_paths: list[str] = []
    confs: list[float] = []
    for p in paths:
        rgb = _open_rgb(p)
        if rgb is None:
            fail_paths.append(p)
            continue
        res, ms = _detect_contour(rgb)
        times.append(ms)
        if res is not None:
            detections += 1
            confs.append(res["confidence"])
        else:
            fail_paths.append(p)
    return {
        "count": len(paths),
        "detections": detections,
        "fail_paths": fail_paths,
        "ms_mean": (statistics.mean(times) if times else 0.0),
        "ms_max": (max(times) if times else 0.0),
        "conf_mean": (statistics.mean(confs) if confs else 0.0),
    }


def test_bad_crop() -> dict[str, Any]:
    rgb = _open_rgb(BAD_CROP_PATH)
    if rgb is None:
        return {"available": False}
    res, ms = _detect_contour(rgb)
    return {
        "available": True,
        "detected": res is not None,
        "ms": ms,
        "result": res,
    }


def _select_sample(n: int, seed: int) -> list[str]:
    paths = sorted(glob.glob(os.path.join(REGION2_DIR, "*.png")))
    if not paths:
        return []
    labeled = [
        p for p in paths if os.path.exists(os.path.splitext(p)[0] + ".boxes.json")
    ]
    other = [p for p in paths if p not in labeled]
    rng = random.Random(seed)
    chosen = list(labeled)
    remaining = max(0, n - len(chosen))
    if remaining and other:
        chosen += rng.sample(other, min(remaining, len(other)))
    return chosen


def test_region2_sample(n: int = 20, seed: int = 0) -> dict[str, Any]:
    """Run BOTH detectors on the same sample. Compute IoU vs GT and the
    full agreement / decorrelation table.
    """
    sample = _select_sample(n, seed)
    if not sample:
        return {"available": False}

    times_c: list[float] = []
    times_g: list[float] = []
    detections_c = 0
    detections_g = 0
    crashes = 0

    ious_c: list[float] = []
    ious_g: list[float] = []

    # Agreement table
    agree_pos = 0  # both YES, IoU(c, g) >= 0.5
    agree_neg = 0  # both NO
    only_contour = 0  # contour YES, geometry NO
    only_geometry = 0  # geometry YES, contour NO
    both_yes_disagree = 0  # both YES but bboxes don't overlap
    both_yes_iou_03 = 0  # both YES with C-G IoU >= 0.3 (looser)
    cg_ious: list[float] = []
    cg_center_dists: list[float] = []

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
                    gt_icon = (
                        int(ic["x"]),
                        int(ic["y"]),
                        int(ic["w"]),
                        int(ic["h"]),
                    )
                hb = bj.get("hud_bbox")
                if hb and {"x", "y", "w", "h"} <= hb.keys():
                    hud_bbox = (
                        int(hb["x"]),
                        int(hb["y"]),
                        int(hb["w"]),
                        int(hb["h"]),
                    )
            except Exception:
                pass

        try:
            res_c, ms_c = _detect_contour(rgb, hud_bbox=hud_bbox)
            res_g, ms_g = _detect_geom(rgb, hud_bbox=hud_bbox)
        except Exception as exc:
            crashes += 1
            per_image.append({"path": os.path.basename(p), "crash": repr(exc)})
            continue

        times_c.append(ms_c)
        times_g.append(ms_g)
        if res_c is not None:
            detections_c += 1
        if res_g is not None:
            detections_g += 1

        rec: dict[str, Any] = {
            "path": os.path.basename(p),
            "ms_c": round(ms_c, 2),
            "ms_g": round(ms_g, 2),
        }
        if res_c is not None:
            rec["contour_bbox"] = res_c["bbox"]
            rec["contour_conf"] = round(res_c["confidence"], 2)
            if gt_icon is not None:
                ic_iou = _iou(res_c["bbox"], gt_icon)
                ious_c.append(ic_iou)
                rec["contour_iou"] = round(ic_iou, 3)
        if res_g is not None:
            rec["geom_bbox"] = res_g["bbox"]
            rec["geom_conf"] = round(res_g["confidence"], 2)
            if gt_icon is not None:
                ig_iou = _iou(res_g["bbox"], gt_icon)
                ious_g.append(ig_iou)
                rec["geom_iou"] = round(ig_iou, 3)
        if gt_icon is not None:
            rec["gt"] = gt_icon

        # Agreement
        if res_c is None and res_g is None:
            agree_neg += 1
            rec["agreement"] = "agree-negative"
        elif res_c is not None and res_g is not None:
            cg_iou = _iou(res_c["bbox"], res_g["bbox"])
            rec["cg_iou"] = round(cg_iou, 3)
            cg_ious.append(cg_iou)
            cx_c = res_c["bbox"][0] + res_c["bbox"][2] / 2.0
            cy_c = res_c["bbox"][1] + res_c["bbox"][3] / 2.0
            cx_g = res_g["bbox"][0] + res_g["bbox"][2] / 2.0
            cy_g = res_g["bbox"][1] + res_g["bbox"][3] / 2.0
            dist = ((cx_c - cx_g) ** 2 + (cy_c - cy_g) ** 2) ** 0.5
            cg_center_dists.append(dist)
            rec["cg_center_dist"] = round(dist, 1)
            if cg_iou >= 0.5:
                agree_pos += 1
                rec["agreement"] = "agree-positive"
            else:
                both_yes_disagree += 1
                rec["agreement"] = "both-yes-but-disagree"
            if cg_iou >= 0.3:
                both_yes_iou_03 += 1
        elif res_c is not None and res_g is None:
            only_contour += 1
            rec["agreement"] = "only-contour"
        else:
            only_geometry += 1
            rec["agreement"] = "only-geometry"

        per_image.append(rec)

    n_total = len(sample) - crashes
    agree_total = agree_pos + agree_neg
    agree_total_loose = both_yes_iou_03 + agree_neg
    return {
        "available": True,
        "sampled": len(sample),
        "crashes": crashes,
        "detections_contour": detections_c,
        "detections_geometry": detections_g,
        "labeled": sum(1 for r in per_image if "gt" in r),
        "iou_n_c": len(ious_c),
        "iou_mean_c": (statistics.mean(ious_c) if ious_c else 0.0),
        "iou_n_g": len(ious_g),
        "iou_mean_g": (statistics.mean(ious_g) if ious_g else 0.0),
        "ms_mean_c": (statistics.mean(times_c) if times_c else 0.0),
        "ms_max_c": (max(times_c) if times_c else 0.0),
        "ms_mean_g": (statistics.mean(times_g) if times_g else 0.0),
        "agree_positive": agree_pos,
        "agree_negative": agree_neg,
        "only_contour": only_contour,
        "only_geometry": only_geometry,
        "both_yes_disagree": both_yes_disagree,
        "both_yes_iou_03": both_yes_iou_03,
        "agree_rate": (agree_total / n_total if n_total else 0.0),
        "agree_rate_loose": (agree_total_loose / n_total if n_total else 0.0),
        "cg_iou_mean": (statistics.mean(cg_ious) if cg_ious else 0.0),
        "cg_center_dist_mean": (
            statistics.mean(cg_center_dists) if cg_center_dists else 0.0
        ),
        "per_image": per_image,
    }


def test_defensive() -> dict[str, Any]:
    cases: list[tuple[str, Any]] = [
        ("None", None),
        ("empty_array", np.zeros((0, 0, 3), dtype=np.uint8)),
        ("tiny_array", np.zeros((2, 2, 3), dtype=np.uint8)),
        ("solid_black", np.zeros((50, 50, 3), dtype=np.uint8)),
        ("solid_cyan", np.tile(np.array([[0, 255, 255]], dtype=np.uint8), (50, 50, 1))),
        ("luma_2d", np.zeros((50, 50), dtype=np.uint8)),
        ("rgba_4ch", np.zeros((50, 50, 4), dtype=np.uint8)),
    ]
    out: dict[str, Any] = {}
    for name, x in cases:
        try:
            r = find_icon_by_contour(x)
            out[name] = "None" if r is None else "Detected"
        except Exception as exc:
            out[name] = f"RAISED: {exc!r}"
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _print_section(title: str) -> None:
    print()
    print(title)
    print("=" * len(title))


def main() -> None:
    print("find_icon_by_contour test report")
    print("================================")

    _print_section("[1] Defensive behavior")
    for name, status in test_defensive().items():
        print(f"  {name:14s}: {status}")

    _print_section("[2] Real icons (training_data_pending_review_signal/icon)")
    r1 = test_real_icons()
    n = r1["count"]
    d = r1["detections"]
    print(f"  Detected: {d}/{n}  ({d/max(1,n):.0%})")
    print(f"  Mean confidence: {r1['conf_mean']:.3f}")
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
        verdict = "DETECTED" if r2["detected"] else "rejected"
        print(f"  {verdict}; {r2['ms']:.2f} ms")
        if r2["detected"]:
            res = r2["result"]
            print(
                f"    bbox={res['bbox']}  conf={res['confidence']:.2f}  "
                f"chamfer={res['details']['contour_match_score']:.2f}  "
                f"perim={res['details']['best_contour_perimeter']}"
            )

    _print_section("[4] region2 random sample (n=20) — both detectors")
    r3 = test_region2_sample(20, seed=0)
    if not r3.get("available"):
        print("  region2 directory not available")
    else:
        print(
            f"  Sampled: {r3['sampled']}, crashes: {r3['crashes']}, labeled: {r3['labeled']}"
        )
        print(
            f"  Detections — contour: {r3['detections_contour']}, geometry: {r3['detections_geometry']}"
        )
        print(
            f"  IoU vs GT — contour: mean {r3['iou_mean_c']:.3f} on n={r3['iou_n_c']} | "
            f"geometry: mean {r3['iou_mean_g']:.3f} on n={r3['iou_n_g']}"
        )
        print(
            f"  ms/frame — contour: mean {r3['ms_mean_c']:.2f}, max {r3['ms_max_c']:.2f} | "
            f"geometry: mean {r3['ms_mean_g']:.2f}"
        )
        print()
        print("  Agreement table:")
        print(f"    agree-positive (both YES, IoU>=0.5): {r3['agree_positive']}")
        print(f"    agree-negative (both NO):            {r3['agree_negative']}")
        print(f"    only contour YES:                    {r3['only_contour']}")
        print(f"    only geometry YES:                   {r3['only_geometry']}")
        print(f"    both YES but bboxes disagree:        {r3['both_yes_disagree']}")
        print(f"    --> agreement rate (positive+negative): {r3['agree_rate']:.0%}")
        print(f"    --> loose agreement (IoU>=0.3 + agree-neg): {r3['agree_rate_loose']:.0%}")
        print(
            f"    Mean C-G IoU: {r3['cg_iou_mean']:.3f}  "
            f"Mean C-G center dist: {r3['cg_center_dist_mean']:.1f} px"
        )
        print()
        print("  Per-image:")
        for rec in r3["per_image"]:
            line = f"    {rec.get('path'):<32s} [{rec.get('agreement', '?'):<22s}]"
            if "crash" in rec:
                line += f" CRASH {rec['crash']}"
            else:
                if "contour_bbox" in rec:
                    line += f" C={rec['contour_bbox']} conf={rec.get('contour_conf', 0):.2f}"
                    if "contour_iou" in rec:
                        line += f" iou={rec['contour_iou']:.2f}"
                else:
                    line += " C=NONE"
                if "geom_bbox" in rec:
                    line += f" | G={rec['geom_bbox']} conf={rec.get('geom_conf', 0):.2f}"
                    if "geom_iou" in rec:
                        line += f" iou={rec['geom_iou']:.2f}"
                else:
                    line += " | G=NONE"
                if "gt" in rec:
                    line += f" gt={rec['gt']}"
            print(line)

    _print_section("Summary")
    real_pct = r1["detections"] / max(1, r1["count"])
    print(f"  Real icons:        {r1['detections']}/{r1['count']} ({real_pct:.0%})")
    if r3.get("available"):
        print(
            f"  region2 detect:    contour {r3['detections_contour']}/{r3['sampled']} "
            f"(mean IoU {r3['iou_mean_c']:.3f} on {r3['iou_n_c']} labeled)"
        )
        print(f"  Agreement rate:    {r3['agree_rate']:.0%}")
        print(f"  contour ms mean:   {r3['ms_mean_c']:.2f}")
    print(f"  bad-crop detected: {r2.get('detected', 'n/a')}")


if __name__ == "__main__":
    main()
