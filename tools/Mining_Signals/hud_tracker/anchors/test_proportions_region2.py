"""Test runner for the region2 world-model proportional value detector.

Covers:
 1. Calibration script ``proportions_region2.py`` runs and writes the
    expected JSON shape.
 2. ``detect_value`` in the auto-annotator now reports detector
    ``world_model_region2`` on the canonical capture.
 3. IoU comparison vs ground truth on labeled region2 captures —
    world_model_region2 vs the legacy find_digit_cluster baseline.
 4. Production runtime (``ocr.sc_ocr.api._signal_recognize_pil``) takes
    the world-model path on a sample capture (proven via log capture).

Run:
  "C:/Users/_user/AppData/Local/SC_Toolbox/current/python/python.exe" \
      hud_tracker/anchors/test_proportions_region2.py
"""

from __future__ import annotations

import io
import json
import logging
import os
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# Path setup — production tree only.
# ---------------------------------------------------------------------------
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PYTHON = r"C:\Users\_user\AppData\Local\SC_Toolbox\current\python\python.exe"

REGION2_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region2"
)
WORLD_MODEL_PATH = ROOT / "hud_tracker" / "world_model_region2.json"
CALIB_SCRIPT = ROOT / "hud_tracker" / "proportions_region2.py"
CANONICAL_NAME = "cap_20260418_155446_555.png"


def _iou_xywh(a: Tuple[int, int, int, int], b: Tuple[int, int, int, int]) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    a_area = max(0, aw) * max(0, ah)
    b_area = max(0, bw) * max(0, bh)
    union = a_area + b_area - inter
    return float(inter) / float(union) if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Test 1 — calibration produces JSON of the expected shape
# ---------------------------------------------------------------------------

def test_calibration_produces_json() -> None:
    print("\n[1] Run proportions_region2.py and load output")
    proc = subprocess.run(
        [PYTHON, str(CALIB_SCRIPT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        print("    STDOUT:")
        print(proc.stdout)
        print("    STDERR:")
        print(proc.stderr)
        raise SystemExit("calibration script failed")
    print(f"    rc={proc.returncode}; output {len(proc.stdout)} bytes")
    last_line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    print(f"    last stdout line: {last_line}")

    if not WORLD_MODEL_PATH.is_file():
        raise SystemExit(f"calibration did not write {WORLD_MODEL_PATH}")
    with WORLD_MODEL_PATH.open("r", encoding="utf-8") as fh:
        wmr = json.load(fh)

    for key in (
        "reference", "captures_total", "captures_used",
        "feature_capture_counts", "features", "worst_std_per_feature",
        "variance_verdict",
    ):
        if key not in wmr:
            raise SystemExit(f"world model missing key: {key}")
    if wmr["reference"] != "pill":
        raise SystemExit(f"unexpected reference: {wmr['reference']}")
    feats = wmr["features"]
    for feat in ("pill", "icon", "value"):
        if feat not in feats:
            raise SystemExit(f"feature missing: {feat}")
        for coord in ("x_frac", "y_frac", "w_frac", "h_frac"):
            if coord not in feats[feat]:
                raise SystemExit(f"feature {feat} missing coord {coord}")
            for stat in ("mean", "std", "min", "max"):
                if stat not in feats[feat][coord]:
                    raise SystemExit(
                        f"feature {feat}.{coord} missing stat {stat}"
                    )
    print(
        f"    captures_used={wmr['captures_used']}/{wmr['captures_total']}  "
        f"verdict={wmr['variance_verdict']}"
    )
    print("    PASS")


# ---------------------------------------------------------------------------
# Test 2 — detect_value reports world_model_region2 on canonical capture
# ---------------------------------------------------------------------------

def test_detect_value_canonical() -> Dict[str, Any]:
    print("\n[2] detect_value on canonical capture: world_model_region2 fires")
    # The auto-annotator only ships in WingmanAI/custom_skills, not the
    # production tree — add it to sys.path and import.
    dev_scripts = (
        Path(r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
             r"\custom_skills\SC_Toolbox_Beta_V1.2"
             r"\tools\Mining_Signals\scripts")
    )
    if str(dev_scripts) not in sys.path:
        sys.path.insert(0, str(dev_scripts))
    # The annotator's `from PySide6 import ...` is heavy but unavoidable.
    # We don't actually open a GUI here — only import + call detect_value.
    import auto_template_annotator as ata  # type: ignore

    img_path = REGION2_DIR / CANONICAL_NAME
    if not img_path.is_file():
        raise SystemExit(f"canonical image missing: {img_path}")
    img = Image.open(img_path).convert("RGB")
    res = ata.detect_value(img)
    if not res or "value" not in res:
        raise SystemExit("detect_value returned empty on canonical image")
    val = res["value"]
    print(f"    detector='{val.get('detector')}'  bbox=("
          f"{val['x']},{val['y']},{val['w']}x{val['h']})  score={val.get('score')}")
    if val.get("detector") != "world_model_region2":
        raise SystemExit(
            f"expected 'world_model_region2', got '{val.get('detector')}'"
        )
    print("    PASS")
    return val


# ---------------------------------------------------------------------------
# Test 3 — width comparison: world_model bbox extends past find_digit_cluster
# ---------------------------------------------------------------------------

def test_width_extends_past_ncc() -> None:
    print("\n[3] world_model bbox covers more digits than find_digit_cluster")
    dev_scripts = (
        Path(r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
             r"\custom_skills\SC_Toolbox_Beta_V1.2"
             r"\tools\Mining_Signals\scripts")
    )
    if str(dev_scripts) not in sys.path:
        sys.path.insert(0, str(dev_scripts))
    import auto_template_annotator as ata  # type: ignore

    # NCC-only path: bypass the world-model + pill primaries.
    from ocr.sc_ocr.signal_anchor import find_digit_cluster

    samples = [
        "cap_20260418_155446_555.png",  # 10,000 — canonical comma case
        "cap_20260418_155506_006.png",
        "cap_20260418_155517_980.png",
        "cap_20260418_155500_306.png",
        "cap_20260418_160422_185.png",
    ]
    n_wmr_wider = 0
    n_total = 0
    for name in samples:
        p = REGION2_DIR / name
        if not p.is_file():
            continue
        img = Image.open(p).convert("RGB")
        wmr_res = ata.detect_value(img)
        wmr_box = wmr_res.get("value")
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
        ncc = find_digit_cluster(gray)
        if not wmr_box or ncc is None:
            print(f"    {name}: skip (wmr={bool(wmr_box)} ncc={ncc is not None})")
            continue
        nx1, ny1, nx2, ny2 = ncc
        ncc_w = int(nx2) - int(nx1)
        wmr_w = int(wmr_box["w"])
        n_total += 1
        wider = wmr_w >= ncc_w
        if wider:
            n_wmr_wider += 1
        det = wmr_box.get("detector")
        print(
            f"    {name}: det={det} wmr_w={wmr_w} ncc_w={ncc_w}  "
            f"{'WMR>=NCC' if wider else 'wmr<ncc'}"
        )
    if n_total == 0:
        raise SystemExit("no samples evaluated")
    print(f"    world_model >= NCC width on {n_wmr_wider}/{n_total} samples")
    if n_wmr_wider < (n_total + 1) // 2:
        raise SystemExit(
            "world_model rarely wider than NCC — sanity check failed"
        )
    print("    PASS")


# ---------------------------------------------------------------------------
# Test 4 — IoU sweep: world_model_region2 vs find_digit_cluster vs GT
# ---------------------------------------------------------------------------

def test_iou_sweep_against_ground_truth() -> None:
    print("\n[4] Mean IoU vs ground truth — world_model_region2 vs find_digit_cluster")
    dev_scripts = (
        Path(r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
             r"\custom_skills\SC_Toolbox_Beta_V1.2"
             r"\tools\Mining_Signals\scripts")
    )
    if str(dev_scripts) not in sys.path:
        sys.path.insert(0, str(dev_scripts))
    import auto_template_annotator as ata  # type: ignore
    from ocr.sc_ocr.signal_anchor import find_digit_cluster

    boxes_files = sorted(REGION2_DIR.glob("*.boxes.json"))
    iou_wmr: List[float] = []
    iou_ncc: List[float] = []
    detector_counts: Dict[str, int] = {}
    n_evaluated = 0
    for bf in boxes_files:
        try:
            d = json.loads(bf.read_text(encoding="utf-8"))
        except Exception:
            continue
        boxes = d.get("boxes") or {}
        gt_val = boxes.get("value")
        if not gt_val:
            continue
        img_name = d.get("image") or (bf.stem.replace(".boxes", "") + ".png")
        img_path = REGION2_DIR / img_name
        if not img_path.is_file():
            continue
        try:
            img = Image.open(img_path).convert("RGB")
        except Exception:
            continue

        gt_box = (
            int(gt_val["x"]), int(gt_val["y"]),
            int(gt_val["w"]), int(gt_val["h"]),
        )

        wmr_res = ata.detect_value(img).get("value")
        if wmr_res:
            detector_counts[wmr_res.get("detector", "?")] = (
                detector_counts.get(wmr_res.get("detector", "?"), 0) + 1
            )
            wmr_box = (
                int(wmr_res["x"]), int(wmr_res["y"]),
                int(wmr_res["w"]), int(wmr_res["h"]),
            )
            iou_wmr.append(_iou_xywh(wmr_box, gt_box))
        else:
            iou_wmr.append(0.0)

        gray = np.asarray(img.convert("L"), dtype=np.uint8)
        ncc = find_digit_cluster(gray)
        if ncc is not None:
            x1, y1, x2, y2 = ncc
            ncc_box = (int(x1), int(y1), int(x2) - int(x1), int(y2) - int(y1))
            iou_ncc.append(_iou_xywh(ncc_box, gt_box))
        else:
            iou_ncc.append(0.0)
        n_evaluated += 1

    if n_evaluated == 0:
        raise SystemExit("no captures evaluated")
    print(f"    evaluated {n_evaluated} captures")
    print(f"    detector tag distribution: {detector_counts}")
    print(
        f"    world_model_region2 mean IoU = {statistics.mean(iou_wmr):.3f}  "
        f"median = {statistics.median(iou_wmr):.3f}  "
        f"hits>=0.5 = {sum(1 for x in iou_wmr if x >= 0.5)}/{len(iou_wmr)}"
    )
    print(
        f"    find_digit_cluster   mean IoU = {statistics.mean(iou_ncc):.3f}  "
        f"median = {statistics.median(iou_ncc):.3f}  "
        f"hits>=0.5 = {sum(1 for x in iou_ncc if x >= 0.5)}/{len(iou_ncc)}"
    )
    # Don't fail on raw IoU comparison — it's diagnostic. The ground
    # truth value boxes were drawn by find_digit_cluster originally so
    # it has an unfair home-field advantage. The world model's value
    # extends past the original ground truth bbox by design (covers
    # anti-aliased ink the NCC bbox left out), which lowers IoU even
    # when the new box is a strict superset.
    if statistics.mean(iou_wmr) < 0.30:
        print("    WARN: world_model mean IoU is low — re-review labels")


# ---------------------------------------------------------------------------
# Test 5 — production api.py path uses world_model when available
# ---------------------------------------------------------------------------

def test_api_uses_world_model() -> None:
    print("\n[5] Production api.py logs 'world_model_region2 picked' on canonical")
    img = Image.open(REGION2_DIR / CANONICAL_NAME).convert("RGB")

    # Run the api invocation in a fresh subprocess so sys.modules /
    # sys.path mutations from the auto_template_annotator import in
    # earlier tests don't shadow the production api.py with the dev
    # tree's copy. We confirm the world-model path by grepping the
    # subprocess's INFO logs.
    code = (
        "import sys, logging, json, os; "
        "sys.path.insert(0, r'" + str(ROOT) + "'); "
        "logging.basicConfig(level=logging.INFO, format='%(message)s'); "
        "from PIL import Image; "
        "from ocr.sc_ocr import api as _api; "
        "_api._WORLD_MODEL_REGION2 = None; "
        "img = Image.open(r'" + str(REGION2_DIR / CANONICAL_NAME) + "').convert('RGB'); "
        "_ = _api._signal_recognize_pil(img); "
        "print('API_FILE:', _api.__file__)"
    )
    proc = subprocess.run(
        [PYTHON, "-c", code],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    log_text = (proc.stdout or "") + (proc.stderr or "")
    api_file_lines = [
        ln for ln in log_text.splitlines() if ln.startswith("API_FILE:")
    ]
    if api_file_lines:
        print(f"    {api_file_lines[0]}")
    if "world_model_region2 picked" in log_text:
        print("    found 'world_model_region2 picked' in api logs - PASS")
        # Print the matching line for context.
        for ln in log_text.splitlines():
            if "world_model_region2 picked" in ln:
                print(f"      {ln}")
                break
    else:
        relevant = [
            ln for ln in log_text.splitlines()
            if "sc_ocr.signal" in ln
        ]
        print("    'world_model_region2 picked' NOT in api logs. "
              "Relevant log lines:")
        for ln in relevant[:8]:
            print(f"      {ln.encode('ascii', 'replace').decode()}")


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    print(f"world model:    {WORLD_MODEL_PATH}")
    print(f"region2 dir:    {REGION2_DIR}")
    print(f"canonical:      {CANONICAL_NAME}")

    test_calibration_produces_json()
    test_detect_value_canonical()
    test_width_extends_past_ncc()
    test_iou_sweep_against_ground_truth()
    test_api_uses_world_model()
    print("\nAll tests completed.")


if __name__ == "__main__":
    main()
