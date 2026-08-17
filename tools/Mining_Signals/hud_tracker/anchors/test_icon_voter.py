"""Test runner for the multi-voter icon detector.

Covers:
 1. Synthetic decision-tree branches (mocked detector outputs).
 2. The 6 real icons in
    training_data_pending_review_signal/icon/pending_*_rgb.png.
 3. 20 region2 captures with optional ground-truth boxes.
 4. End-to-end production sanity: ``find_icon`` from ``signal_anchor``
    on the same 20 captures, plus an auto-annotator import smoke test.

Run under production python:
  "C:/Users/_user/AppData/Local/SC_Toolbox/current/python/python.exe" \
      hud_tracker/anchors/test_icon_voter.py
"""

from __future__ import annotations

import glob
import json
import logging
import os
import random
import statistics
import sys
import time
from collections import Counter
from typing import Any
from unittest.mock import patch

import numpy as np
from PIL import Image

# Make hud_tracker.anchors and ocr.sc_ocr importable when invoked as
# a script.
HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from hud_tracker.anchors import icon_voter  # noqa: E402
from hud_tracker.anchors.icon_voter import (  # noqa: E402
    availability,
    vote_on_icon_candidate,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ICON_DIR = (
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_pending_review_signal\icon"
)
REGION2_DIR = (
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region2"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_rgb(path: str) -> np.ndarray | None:
    try:
        return np.asarray(Image.open(path).convert("RGB"))
    except Exception:
        return None


def _print_section(title: str) -> None:
    bar = "=" * len(title)
    print()
    print(title)
    print(bar)


def _box_to_xyxy(box: dict[str, int]) -> tuple[int, int, int, int]:
    return (int(box["x"]), int(box["y"]), int(box["x"]) + int(box["w"]),
            int(box["y"]) + int(box["h"]))


def _iou_xyxy(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    iw, ih = max(0, ix1 - ix0), max(0, iy1 - iy0)
    inter = iw * ih
    aw, ah = max(0, ax1 - ax0), max(0, ay1 - ay0)
    bw, bh = max(0, bx1 - bx0), max(0, by1 - by0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# 1. Synthetic decision-tree branches
# ---------------------------------------------------------------------------


def _make_dummy_rgb(w: int = 60, h: int = 60) -> Image.Image:
    """A bland gray image — voters all called via mocks anyway."""
    arr = np.full((h, w, 3), 96, dtype=np.uint8)
    return Image.fromarray(arr).convert("RGB")


def test_synthetic_branches() -> dict[str, Any]:
    """Mock each voter and verify the decision tree."""
    bbox = (5, 5, 55, 55)
    img = _make_dummy_rgb()
    cases: list[dict[str, Any]] = []

    # 1) both primaries yes -> accept (no CNN consulted)
    with patch.object(icon_voter, "_vote_geometry", return_value="yes"), \
         patch.object(icon_voter, "_vote_contour", return_value="yes"), \
         patch.object(icon_voter, "_vote_rgb_cnn", return_value=("unavailable", None)), \
         patch.object(icon_voter, "_vote_gray_cnn", return_value=("no", 0.0)):
        v = vote_on_icon_candidate(img, bbox)
    cases.append({
        "branch": "both primaries yes",
        "expected_accept": True,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": v["accepted"] and "primaries_agree_yes" in v["decision_path"],
    })

    # 2) both primaries no -> reject (no CNN consulted)
    with patch.object(icon_voter, "_vote_geometry", return_value="no"), \
         patch.object(icon_voter, "_vote_contour", return_value="no"), \
         patch.object(icon_voter, "_vote_rgb_cnn", return_value=("yes", 0.99)), \
         patch.object(icon_voter, "_vote_gray_cnn", return_value=("yes", 0.9)):
        v = vote_on_icon_candidate(img, bbox)
    cases.append({
        "branch": "both primaries no",
        "expected_accept": False,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": (not v["accepted"]) and "primaries_agree_no" in v["decision_path"],
    })

    # 3) primaries disagree, rgb_cnn says yes -> accept
    with patch.object(icon_voter, "_vote_geometry", return_value="yes"), \
         patch.object(icon_voter, "_vote_contour", return_value="no"), \
         patch.object(icon_voter, "_vote_rgb_cnn", return_value=("yes", 0.85)), \
         patch.object(icon_voter, "_vote_gray_cnn", return_value=("no", 0.1)):
        v = vote_on_icon_candidate(img, bbox)
    cases.append({
        "branch": "primaries disagree, rgb_cnn yes",
        "expected_accept": True,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": v["accepted"] and "rgb_cnn=accept" in v["decision_path"],
    })

    # 4) primaries disagree, rgb_cnn unsure (abstain), gray_cnn yes -> accept
    with patch.object(icon_voter, "_vote_geometry", return_value="yes"), \
         patch.object(icon_voter, "_vote_contour", return_value="no"), \
         patch.object(icon_voter, "_vote_rgb_cnn", return_value=("abstain", 0.5)), \
         patch.object(icon_voter, "_vote_gray_cnn", return_value=("yes", 0.7)):
        v = vote_on_icon_candidate(img, bbox)
    cases.append({
        "branch": "primaries disagree, rgb_cnn abstain, gray_cnn yes",
        "expected_accept": True,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": v["accepted"] and "gray_cnn=accept" in v["decision_path"],
    })

    # 5) all abstain / unavailable -> reject (gray says abstain too)
    with patch.object(icon_voter, "_vote_geometry", return_value="no"), \
         patch.object(icon_voter, "_vote_contour", return_value="yes"), \
         patch.object(icon_voter, "_vote_rgb_cnn", return_value=("abstain", 0.5)), \
         patch.object(icon_voter, "_vote_gray_cnn", return_value=("abstain", None)):
        v = vote_on_icon_candidate(img, bbox)
    cases.append({
        "branch": "primaries disagree, all CNNs abstain",
        "expected_accept": False,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": (not v["accepted"]),
    })

    # 6) gray-only mode (rgb_image=None) — only the gray CNN votes.
    with patch.object(icon_voter, "_vote_gray_cnn", return_value=("yes", 0.65)):
        v = vote_on_icon_candidate(None, bbox)
    cases.append({
        "branch": "gray-only mode (rgb_image None), gray_cnn yes",
        "expected_accept": True,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": v["accepted"],
    })

    # 7) primaries disagree, rgb_cnn says NO with prob<=0.3 -> reject
    with patch.object(icon_voter, "_vote_geometry", return_value="yes"), \
         patch.object(icon_voter, "_vote_contour", return_value="no"), \
         patch.object(icon_voter, "_vote_rgb_cnn", return_value=("no", 0.1)), \
         patch.object(icon_voter, "_vote_gray_cnn", return_value=("yes", 0.99)):
        v = vote_on_icon_candidate(img, bbox)
    cases.append({
        "branch": "primaries disagree, rgb_cnn no",
        "expected_accept": False,
        "got_accept": bool(v["accepted"]),
        "decision_path": v["decision_path"],
        "ok": (not v["accepted"]) and "rgb_cnn=reject" in v["decision_path"],
    })

    n_pass = sum(1 for c in cases if c["ok"])
    return {"n": len(cases), "n_pass": n_pass, "cases": cases}


# ---------------------------------------------------------------------------
# 2. Real icons
# ---------------------------------------------------------------------------


def test_real_icons() -> dict[str, Any]:
    paths = sorted(glob.glob(os.path.join(ICON_DIR, "pending_*_rgb.png")))
    if not paths:
        return {"available": False}
    detections = 0
    times: list[float] = []
    fail_paths: list[str] = []
    for p in paths:
        rgb = _open_rgb(p)
        if rgb is None:
            fail_paths.append(p)
            continue
        H, W = rgb.shape[:2]
        bbox = (0, 0, W, H)
        t0 = time.perf_counter()
        v = vote_on_icon_candidate(rgb, bbox)
        times.append((time.perf_counter() - t0) * 1000.0)
        if v["accepted"]:
            detections += 1
        else:
            fail_paths.append(p)
    return {
        "available": True,
        "count": len(paths),
        "detections": detections,
        "fail_paths": [os.path.basename(p) for p in fail_paths],
        "ms_mean": statistics.mean(times) if times else 0.0,
        "ms_max": max(times) if times else 0.0,
    }


# ---------------------------------------------------------------------------
# 3. Region2 captures
# ---------------------------------------------------------------------------


def test_region2_voter(n: int = 20, seed: int = 0) -> dict[str, Any]:
    paths = sorted(glob.glob(os.path.join(REGION2_DIR, "*.png")))
    if not paths:
        return {"available": False}
    rng = random.Random(seed)
    labeled = [p for p in paths if os.path.exists(os.path.splitext(p)[0] + ".boxes.json")]
    other = [p for p in paths if p not in labeled]
    chosen = list(labeled[:n])
    if len(chosen) < n and other:
        chosen += rng.sample(other, min(n - len(chosen), len(other)))
    sample = chosen[:n]

    accepts = 0
    rejects = 0
    crashes = 0
    decision_path_counter: Counter = Counter()
    per_image: list[dict[str, Any]] = []

    for p in sample:
        rgb = _open_rgb(p)
        if rgb is None:
            crashes += 1
            continue
        boxes_path = os.path.splitext(p)[0] + ".boxes.json"
        gt_icon = None
        if os.path.exists(boxes_path):
            try:
                with open(boxes_path, "r", encoding="utf-8") as f:
                    bj = json.load(f)
                ic = bj.get("boxes", {}).get("icon")
                if ic and {"x", "y", "w", "h"} <= ic.keys():
                    gt_icon = (
                        int(ic["x"]),
                        int(ic["y"]),
                        int(ic["x"]) + int(ic["w"]),
                        int(ic["y"]) + int(ic["h"]),
                    )
            except Exception:
                gt_icon = None

        # Use the GT bbox when available; otherwise scan the leftmost
        # region (icon is always there). When no GT is available, give
        # a generous left-third box so the voter has something to work
        # with.
        if gt_icon is not None:
            bbox = gt_icon
        else:
            H, W = rgb.shape[:2]
            bbox = (0, 0, max(40, W // 4), H)

        try:
            v = vote_on_icon_candidate(rgb, bbox)
        except Exception as exc:
            crashes += 1
            per_image.append({"path": os.path.basename(p), "crash": repr(exc)})
            continue

        if v["accepted"]:
            accepts += 1
        else:
            rejects += 1
        decision_path_counter[v["decision_path"]] += 1
        per_image.append({
            "path": os.path.basename(p),
            "accepted": v["accepted"],
            "decision_path": v["decision_path"],
            "votes": v["votes"],
            "had_gt": gt_icon is not None,
        })

    return {
        "available": True,
        "sampled": len(sample),
        "accepts": accepts,
        "rejects": rejects,
        "crashes": crashes,
        "decision_paths": dict(decision_path_counter),
        "per_image": per_image,
    }


# ---------------------------------------------------------------------------
# 4. End-to-end production pipeline
# ---------------------------------------------------------------------------


def test_production_pipeline(n: int = 20, seed: int = 0) -> dict[str, Any]:
    """Run ``signal_anchor.find_icon`` on the same 20 captures.

    Verifies the [VOTE] log lines fire and that anchors come back.
    """
    paths = sorted(glob.glob(os.path.join(REGION2_DIR, "*.png")))
    if not paths:
        return {"available": False}
    rng = random.Random(seed)
    labeled = [p for p in paths if os.path.exists(os.path.splitext(p)[0] + ".boxes.json")]
    other = [p for p in paths if p not in labeled]
    chosen = list(labeled[:n])
    if len(chosen) < n and other:
        chosen += rng.sample(other, min(n - len(chosen), len(other)))
    sample = chosen[:n]

    try:
        from ocr.sc_ocr.signal_anchor import find_icon, reset_anchor_cache, reset_cache  # noqa
    except Exception as exc:
        return {"available": False, "error": repr(exc)}

    # Capture [VOTE] log records.
    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            try:
                msg = self.format(record)
            except Exception:
                msg = record.getMessage()
            if "[VOTE]" in msg or "[ANCHOR-DIAG]" in msg:
                records.append(msg)

    handler = _Capture()
    handler.setLevel(logging.INFO)
    fmt = logging.Formatter("%(name)s %(levelname)s %(message)s")
    handler.setFormatter(fmt)
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        reset_cache()
    except Exception:
        pass

    detected = 0
    crashes = 0
    iou_n = 0
    iou_sum = 0.0
    per_image: list[dict[str, Any]] = []

    for p in sample:
        try:
            reset_anchor_cache()
        except Exception:
            pass
        rgb = _open_rgb(p)
        if rgb is None:
            crashes += 1
            continue
        gray = np.asarray(Image.fromarray(rgb).convert("L"), dtype=np.uint8)
        try:
            res = find_icon(gray, rgb_image=rgb)
        except Exception as exc:
            crashes += 1
            per_image.append({"path": os.path.basename(p), "crash": repr(exc)})
            continue
        gt = None
        bp = os.path.splitext(p)[0] + ".boxes.json"
        if os.path.exists(bp):
            try:
                with open(bp, "r", encoding="utf-8") as f:
                    bj = json.load(f)
                ic = bj.get("boxes", {}).get("icon")
                if ic:
                    gt = (
                        int(ic["x"]),
                        int(ic["y"]),
                        int(ic["x"]) + int(ic["w"]),
                        int(ic["y"]) + int(ic["h"]),
                    )
            except Exception:
                gt = None
        if res is not None:
            detected += 1
            x1, y1, x2, y2, score = res
            iou = None
            if gt is not None:
                iou = _iou_xyxy((x1, y1, x2, y2), gt)
                iou_n += 1
                iou_sum += iou
            per_image.append({
                "path": os.path.basename(p),
                "detected": True,
                "bbox": (x1, y1, x2, y2),
                "score": round(score, 3),
                "iou": (round(iou, 3) if iou is not None else None),
            })
        else:
            per_image.append({
                "path": os.path.basename(p),
                "detected": False,
            })

    root.removeHandler(handler)
    root.setLevel(prev_level)

    n_vote_accept = sum(1 for r in records if "[VOTE]" in r and " ACCEPTED via " in r)
    n_vote_reject = sum(1 for r in records if "[VOTE]" in r and " REJECTED votes=" in r)
    decision_path_counter: Counter = Counter()
    for r in records:
        if "[VOTE]" in r and " ACCEPTED via " in r:
            try:
                # message looks like:
                #   [VOTE] cand (x1,y1,x2,y2) ACCEPTED via <path> votes=...
                tail = r.split(" ACCEPTED via ", 1)[1]
                path = tail.split(" votes=", 1)[0]
                decision_path_counter[path] += 1
            except Exception:
                continue

    return {
        "available": True,
        "sampled": len(sample),
        "detected": detected,
        "crashes": crashes,
        "iou_mean": (iou_sum / iou_n) if iou_n else 0.0,
        "iou_n": iou_n,
        "n_vote_accept": n_vote_accept,
        "n_vote_reject": n_vote_reject,
        "decision_paths": dict(decision_path_counter),
        "per_image": per_image,
    }


# ---------------------------------------------------------------------------
# 5. Auto-annotator smoke test
# ---------------------------------------------------------------------------


def test_auto_annotator_import() -> dict[str, Any]:
    """The auto-annotator passes only ``gray`` to ``find_icon`` — verify
    that path still works (the voter must degrade to gray-only mode)."""
    out: dict[str, Any] = {}

    # Try the production scripts location (Roaming), which is where
    # ``scripts.auto_template_annotator`` actually lives. Fall back to
    # the local checkout if for some reason it shows up there.
    roaming_root = (
        r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
        r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    )
    inserted = []
    for root in (ROOT, roaming_root):
        if root and os.path.isdir(root) and root not in sys.path:
            sys.path.insert(0, root)
            inserted.append(root)
    try:
        try:
            import scripts.auto_template_annotator as ata  # type: ignore  # noqa
            out["import_ok"] = True
            out["has_detect_icon"] = hasattr(ata, "detect_icon")
        except Exception as exc:
            out["import_ok"] = False
            out["import_error"] = repr(exc)
            out["has_detect_icon"] = False

        # Direct exercise of the gray-only path without importing the
        # script — we just call detect_icon ourselves via the find_icon
        # signature and confirm voter doesn't choke when rgb_image=None.
        try:
            from ocr.sc_ocr.signal_anchor import find_icon, reset_cache  # noqa
            try:
                reset_cache()
            except Exception:
                pass
            sample_path = None
            for p in sorted(glob.glob(os.path.join(REGION2_DIR, "*.png"))):
                sample_path = p
                break
            if sample_path:
                rgb = _open_rgb(sample_path)
                if rgb is not None:
                    gray = np.asarray(Image.fromarray(rgb).convert("L"), dtype=np.uint8)
                    res = find_icon(gray)  # NO rgb_image — gray-only
                    out["gray_only_call_ok"] = True
                    out["gray_only_result"] = (
                        None if res is None else
                        [int(x) for x in res[:4]] + [round(float(res[4]), 3)]
                    )
                else:
                    out["gray_only_call_ok"] = False
                    out["gray_only_error"] = "rgb load failed"
            else:
                out["gray_only_call_ok"] = False
                out["gray_only_error"] = "no sample available"
        except Exception as exc:
            out["gray_only_call_ok"] = False
            out["gray_only_error"] = repr(exc)
    finally:
        for p in inserted:
            try:
                sys.path.remove(p)
            except ValueError:
                pass
    return out


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def main() -> None:
    print("icon_voter test report")
    print("======================")
    print(f"Voter availability: {availability()}")

    _print_section("[1] Synthetic decision-tree branches")
    s1 = test_synthetic_branches()
    print(f"  passed: {s1['n_pass']}/{s1['n']}")
    for c in s1["cases"]:
        verdict = "OK" if c["ok"] else "FAIL"
        print(
            f"  {verdict:4s} {c['branch']!s:<55s} "
            f"path={c['decision_path']}"
        )

    _print_section("[2] Real icons")
    s2 = test_real_icons()
    if not s2.get("available"):
        print("  icon dir not present")
    else:
        n = s2["count"]
        d = s2["detections"]
        print(f"  Accepted: {d}/{n} ({d/max(1,n):.0%})  "
              f"ms mean {s2['ms_mean']:.2f}  max {s2['ms_max']:.2f}")
        if s2["fail_paths"]:
            print("  Failures:")
            for p in s2["fail_paths"]:
                print(f"    - {p}")

    _print_section("[3] Region2 captures (voter direct)")
    s3 = test_region2_voter(20, seed=0)
    if not s3.get("available"):
        print("  region2 dir not present")
    else:
        print(f"  sampled={s3['sampled']}  accept={s3['accepts']}  "
              f"reject={s3['rejects']}  crashes={s3['crashes']}")
        print("  decision-path breakdown:")
        for path, count in sorted(s3["decision_paths"].items(),
                                  key=lambda kv: -kv[1]):
            print(f"    {count:4d}  {path}")

    _print_section("[4] Production find_icon pipeline")
    s4 = test_production_pipeline(20, seed=0)
    if not s4.get("available"):
        print(f"  pipeline unavailable: {s4.get('error')}")
    else:
        print(f"  sampled={s4['sampled']}  detected={s4['detected']}  "
              f"crashes={s4['crashes']}")
        print(f"  IoU mean={s4['iou_mean']:.3f} (n={s4['iou_n']})")
        print(f"  [VOTE] accept lines: {s4['n_vote_accept']}  "
              f"reject lines: {s4['n_vote_reject']}")
        print("  per-decision-path acceptance:")
        for path, count in sorted(s4["decision_paths"].items(),
                                  key=lambda kv: -kv[1]):
            print(f"    {count:4d}  {path}")
        print("  per-image:")
        for rec in s4["per_image"][:25]:
            line = f"    {rec.get('path'):<32s}"
            if "crash" in rec:
                line += f" CRASH {rec['crash']}"
            elif rec.get("detected"):
                line += f" det score={rec.get('score')} "
                if rec.get("iou") is not None:
                    line += f"iou={rec.get('iou')} "
                line += f"bbox={rec.get('bbox')}"
            else:
                line += " no-detect"
            print(line)

    _print_section("[5] Auto-annotator (gray-only) sanity")
    s5 = test_auto_annotator_import()
    print(f"  import_ok: {s5.get('import_ok')}  "
          f"has_detect_icon: {s5.get('has_detect_icon')}")
    if "import_error" in s5:
        print(f"  import_error: {s5['import_error']}")
    print(f"  gray_only_call_ok: {s5.get('gray_only_call_ok')}  "
          f"result: {s5.get('gray_only_result')}")
    if "gray_only_error" in s5:
        print(f"  gray_only_error: {s5['gray_only_error']}")

    _print_section("Summary")
    print(f"  synthetic: {s1['n_pass']}/{s1['n']}")
    if s2.get("available"):
        print(f"  real icons: {s2['detections']}/{s2['count']}")
    if s3.get("available"):
        print(f"  region2 voter accept rate: "
              f"{s3['accepts']}/{s3['sampled']}")
    if s4.get("available"):
        print(f"  production pipeline detected: "
              f"{s4['detected']}/{s4['sampled']} (IoU mean "
              f"{s4['iou_mean']:.3f} on {s4['iou_n']} labeled)")
        print(f"  vote-line counts: accept={s4['n_vote_accept']} "
              f"reject={s4['n_vote_reject']}")


if __name__ == "__main__":
    main()
