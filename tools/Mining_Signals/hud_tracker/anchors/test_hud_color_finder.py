"""End-to-end test for ``hud_color_finder.find_hud_panel``.

Runs the finder on:

  1. The 2 labeled calibration captures — measures IoU between the
     predicted HUD bbox and the union of the GT feature boxes (which
     is the ground-truth panel extent).
  2. 10 unlabeled captures from ``user_20260418_081525/region1`` —
     sanity check that detection succeeds, and reports detection rate
     and mean ms/frame.

Outputs a single text report to stdout. Exits with status 0 when the
labeled captures produce a detection (regardless of IoU quality —
this is a development tool, not a CI gate).
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

# Ensure the production tree is importable when invoked outside of it.
_THIS = Path(__file__).resolve()
_PROD_TREE = _THIS.parent.parent.parent
if str(_PROD_TREE) not in sys.path:
    sys.path.insert(0, str(_PROD_TREE))

import json

import numpy as np
from PIL import Image

from hud_tracker.anchors.hud_color_finder import (
    DEFAULT_CALIBRATION,
    find_hud_panel,
    load_calibration,
)


logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("test_hud_color_finder")


LABELED_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_154408\region1"
)

UNLABELED_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals\training_data_panels"
    r"\user_20260418_081525\region1"
)

CALIBRATION_CAPTURES = [
    "cap_20260418_155705_329",
    "cap_20260418_155707_070",
]


def _box_to_rect(b: dict) -> tuple[int, int, int, int]:
    return int(b["x"]), int(b["y"]), int(b["w"]), int(b["h"])


def _union_of_boxes(boxes: dict) -> tuple[int, int, int, int] | None:
    if not boxes:
        return None
    xs, ys, xe, ye = [], [], [], []
    for b in boxes.values():
        try:
            x, y, w, h = _box_to_rect(b)
        except Exception:
            continue
        xs.append(x); ys.append(y)
        xe.append(x + w); ye.append(y + h)
    if not xs:
        return None
    return min(xs), min(ys), max(xe) - min(xs), max(ye) - min(ys)


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix0 = max(ax, bx); iy0 = max(ay, by)
    ix1 = min(ax + aw, bx + bw); iy1 = min(ay + ah, by + bh)
    iw = max(0, ix1 - ix0); ih = max(0, iy1 - iy0)
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return float(inter) / float(union) if union > 0 else 0.0


# ─────────────────────────────────────────────────────────────────────


def _print_calibration(calib: dict) -> None:
    print("=== Calibration in use ===")
    print(f"  source: {calib.get('source', '?')}")
    print(f"  n_captures: {calib.get('n_captures', '?')}")
    print(f"  n_total_samples: {calib.get('n_total_samples', '?')}")
    cy = calib.get("cyan_band", {})
    gn = calib.get("green_band", {})
    print(
        f"  cyan_band:  H in [{cy.get('h_min','?')}, {cy.get('h_max','?')}]  "
        f"(PIL hue 0-255 ~= {round(int(cy.get('h_min',0))*360/255)}deg-"
        f"{round(int(cy.get('h_max',0))*360/255)}deg on the standard 0-360 wheel)"
    )
    print(
        f"  green_band: H in [{gn.get('h_min','?')}, {gn.get('h_max','?')}]  "
        f"(PIL hue 0-255 ~= {round(int(gn.get('h_min',0))*360/255)}deg-"
        f"{round(int(gn.get('h_max',0))*360/255)}deg on the standard 0-360 wheel)"
    )
    print(f"  sat_min: {calib.get('sat_min')}")
    print(f"  val_min: {calib.get('val_min')}")
    print(
        f"  morph: seed_iter={calib.get('morph_seed_iterations')}, "
        f"vert_close_px={calib.get('morph_vert_close_px')}, "
        f"horiz_close_px={calib.get('morph_horiz_close_px')}"
    )
    print(f"  geom: aspect in [{calib.get('min_bbox_aspect')}, {calib.get('max_bbox_aspect')}], "
          f"min_area={calib.get('min_area_px')} px, "
          f"min_extent={calib.get('min_extent')}")
    print()


def _run_labeled() -> list[dict]:
    print("=== Labeled captures (IoU vs. union-of-GT-boxes) ===")
    rows: list[dict] = []
    for stem in CALIBRATION_CAPTURES:
        png = LABELED_DIR / f"{stem}.png"
        bxj = LABELED_DIR / f"{stem}.boxes.json"
        if not png.is_file() or not bxj.is_file():
            print(f"  {stem}: missing files, skipped")
            continue
        img = Image.open(png).convert("RGB")
        gt = json.loads(bxj.read_text())
        gt_union = _union_of_boxes(gt.get("boxes", {}))
        t0 = time.perf_counter()
        result = find_hud_panel(img)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if result is None:
            print(f"  {stem}: NO DETECTION ({dt_ms:.1f} ms)")
            rows.append({"stem": stem, "iou": 0.0, "ms": dt_ms, "found": False})
            continue
        bbox = result["bbox"]
        iou = _iou(bbox, gt_union) if gt_union else 0.0
        print(
            f"  {stem}:"
            f"\n    predicted bbox: {bbox} (conf {result['confidence']:.2f})"
            f"\n    GT union bbox:  {gt_union}"
            f"\n    IoU: {iou:.3f}  |  chrome_px: {result['n_chrome_pixels']}"
            f"  |  ms: {dt_ms:.1f}"
        )
        rows.append({"stem": stem, "iou": iou, "ms": dt_ms, "found": True})
    print()
    return rows


def _run_unlabeled() -> list[dict]:
    print("=== Unlabeled sanity captures (10 samples) ===")
    if not UNLABELED_DIR.is_dir():
        print(f"  UNLABELED_DIR not found: {UNLABELED_DIR}")
        return []
    pngs = sorted(UNLABELED_DIR.glob("*.png"))[:10]
    rows: list[dict] = []
    for png in pngs:
        try:
            img = Image.open(png).convert("RGB")
        except Exception as exc:
            print(f"  {png.name}: open failed ({exc})")
            continue
        t0 = time.perf_counter()
        result = find_hud_panel(img)
        dt_ms = (time.perf_counter() - t0) * 1000.0
        if result is None:
            print(f"  {png.name}: NO DETECTION ({dt_ms:.1f} ms)")
            rows.append({"name": png.name, "found": False, "ms": dt_ms})
            continue
        rows.append({
            "name": png.name,
            "found": True,
            "ms": dt_ms,
            "bbox": result["bbox"],
            "conf": result["confidence"],
            "chrome_px": result["n_chrome_pixels"],
        })
        print(
            f"  {png.name}: bbox={result['bbox']} conf={result['confidence']:.2f} "
            f"chrome_px={result['n_chrome_pixels']} ms={dt_ms:.1f}"
        )
    if rows:
        n_found = sum(1 for r in rows if r["found"])
        mean_ms = sum(r["ms"] for r in rows) / len(rows)
        print(f"\n  Detection rate: {n_found}/{len(rows)}, mean {mean_ms:.1f} ms/frame")
    print()
    return rows


def _check_auto_annotator_imports() -> None:
    print("=== auto_template_annotator import check ===")
    auto = (
        Path(r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
             r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
             r"\scripts\auto_template_annotator.py")
    )
    if not auto.is_file():
        print(f"  auto_template_annotator not found: {auto}")
        return
    # Import without instantiating Qt — compile-check the module.
    import importlib.util
    spec = importlib.util.spec_from_file_location("_auto_template_annotator_test", auto)
    if spec is None or spec.loader is None:
        print("  could not build import spec")
        return
    try:
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        print("  imported OK")
    except Exception as exc:
        print(f"  FAILED: {exc!r}")
    print()


def main() -> int:
    calib = load_calibration()
    _print_calibration(calib)
    labeled = _run_labeled()
    unlabeled = _run_unlabeled()
    _check_auto_annotator_imports()

    # Summary block at the end so it's easy to grep.
    print("=== Summary ===")
    if labeled:
        ious = [r["iou"] for r in labeled if r.get("found")]
        if ious:
            print(f"  labeled IoU mean: {sum(ious)/len(ious):.3f}  "
                  f"(min {min(ious):.3f}, max {max(ious):.3f})")
        n_found = sum(1 for r in labeled if r.get("found"))
        print(f"  labeled found: {n_found}/{len(labeled)}")
    if unlabeled:
        n_found = sum(1 for r in unlabeled if r.get("found"))
        mean_ms = sum(r["ms"] for r in unlabeled) / max(1, len(unlabeled))
        print(f"  unlabeled found: {n_found}/{len(unlabeled)}, mean {mean_ms:.2f} ms/frame")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
