"""Test harness for hud_tracker.anchors.icon_rgb_ncc.

Run via:

    "C:\\Users\\_user\\AppData\\Local\\SC_Toolbox\\current\\python\\python.exe"
        hud_tracker\\anchors\\test_icon_rgb_ncc.py

The script:

  1. Verifies the template stack loads (real labels OR synthetic
     fallback).
  2. Runs ``find_icon_rgb_ncc`` on the canonical failure-case capture
     (``cap_20260418_155446_555.png``) and confirms the best match
     lives in the leftmost ~30% of the search width — the actual icon
     region — rather than the cyan-digit area where the legacy
     grayscale NCC clustered all 8 candidates.
  3. Runs on the 5 real labeled icons (excluding
     ``pending_cap_20260418_155503_607_rgb.png`` per upstream finding)
     and reports per-image IoU vs ground truth.
  4. Runs on 20 random region2 captures, reports detection rate +
     per-frame ms.
  5. Compares RGB-NCC vs grayscale-NCC peak positions on the canonical
     capture.

This test is self-contained; no pytest required.
"""

from __future__ import annotations

import json
import logging
import os
import random
import sys
import time
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.signal import fftconvolve  # type: ignore[import-untyped]

# Make hud_tracker importable when run as a script.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

logging.basicConfig(level=logging.WARNING, format="%(message)s")
log = logging.getLogger("test_icon_rgb_ncc")

# --- Constants ------------------------------------------------------------

LABEL_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_pending_review_signal\icon"
)
REGION2_DIR = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels\user_20260418_154408\region2"
)
BLACKLISTED_LABELS = {"pending_cap_20260418_155503_607_rgb.png"}
CANONICAL_CAP = "cap_20260418_155446_555.png"


def iou_xywh(a, b) -> float:
    ax1, ay1, aw, ah = a
    bx1, by1, bw, bh = b
    ax2, ay2 = ax1 + aw, ay1 + ah
    bx2, by2 = bx1 + bw, by1 + bh
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    ua = aw * ah + bw * bh - inter
    return float(inter) / float(ua) if ua > 0 else 0.0


def _load_boxes(json_path: Path) -> dict | None:
    """Load <stem>.boxes.json and return parsed dict or None."""
    if not json_path.is_file():
        return None
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _gray_ncc_top_candidates(
    rgb_image: np.ndarray,
    hud_bbox: tuple[int, int, int, int] | None,
    template_rgb: np.ndarray,
    n: int = 8,
) -> list[tuple[float, int, int]]:
    """Reference grayscale NCC: PIL Image -> luminance -> NCC against a
    grayscale version of the same template. Return top-n peaks as
    (score, x, y) in original-image coords.

    This replicates the behavior of the legacy NCC that drops color
    information.
    """
    if hud_bbox is not None:
        x, y, w, h = hud_bbox
        crop = rgb_image[y:y + h, x:x + w]
        ox, oy = x, y
    else:
        crop = rgb_image
        ox, oy = 0, 0
    # Luma weights (Rec.601); single channel.
    img_l = (
        0.299 * crop[..., 0] + 0.587 * crop[..., 1] + 0.114 * crop[..., 2]
    ).astype(np.float32)
    t_l = (
        0.299 * template_rgb[..., 0]
        + 0.587 * template_rgb[..., 1]
        + 0.114 * template_rgb[..., 2]
    ).astype(np.float32)

    H, W = img_l.shape
    h_t, w_t = t_l.shape
    if H < h_t or W < w_t:
        return []

    t_zm = t_l - float(t_l.mean())
    t_norm = float(np.sqrt(np.sum(t_zm * t_zm)))
    if t_norm < 1e-6:
        return []
    n_pix = float(h_t * w_t)
    flipped = t_zm[::-1, ::-1]
    num = fftconvolve(img_l, flipped, mode="valid")
    kernel = np.ones((h_t, w_t), dtype=np.float32)
    sum_img = fftconvolve(img_l, kernel, mode="valid")
    sum_img_sq = fftconvolve(img_l * img_l, kernel, mode="valid")
    mu = sum_img / n_pix
    var_window = np.maximum(sum_img_sq - n_pix * (mu * mu), 0.0)
    denom = np.sqrt(var_window) * t_norm
    score = np.zeros_like(num, dtype=np.float32)
    np.divide(num, denom, out=score, where=denom > 1e-6)
    np.clip(score, -1.0, 1.0, out=score)

    # Peak picking with simple non-max suppression: take the top n
    # responses with at least 5px separation.
    flat_idx = np.argsort(score, axis=None)[::-1]
    out: list[tuple[float, int, int]] = []
    for fi in flat_idx[:1000]:
        yi, xi = np.unravel_index(int(fi), score.shape)
        s = float(score[yi, xi])
        if s <= 0:
            break
        # Reject if too close to an already-picked peak.
        too_close = False
        for _, px, py in out:
            if abs(int(xi) - (px - ox)) < 5 and abs(int(yi) - (py - oy)) < 5:
                too_close = True
                break
        if too_close:
            continue
        out.append((s, int(xi) + ox, int(yi) + oy))
        if len(out) >= n:
            break
    return out


def main() -> int:
    print("=" * 72)
    print("icon_rgb_ncc test harness")
    print("=" * 72)

    from hud_tracker.anchors.icon_rgb_ncc import (
        find_icon_rgb_ncc,
        availability,
    )

    # ── 1. Templates available? ──────────────────────────────────────
    av = availability()
    print(f"\n[1] Template stack: n={av['n_templates']} size={av['size']}")
    print(f"    names: {av['names']}")
    if av["n_templates"] == 0:
        print("    FAIL: no templates loaded")
        return 1
    print("    PASS: templates loaded")

    # ── 2. Canonical failure-case capture ────────────────────────────
    cap_path = REGION2_DIR / CANONICAL_CAP
    boxes_path = REGION2_DIR / f"{cap_path.stem}.boxes.json"
    if not cap_path.is_file():
        print(f"\n[2] FAIL: canonical capture missing at {cap_path}")
        return 2
    boxes = _load_boxes(boxes_path)
    if boxes is None:
        print(f"\n[2] FAIL: boxes json missing at {boxes_path}")
        return 2
    hud_bbox = None
    if isinstance(boxes.get("hud_bbox"), dict):
        hb = boxes["hud_bbox"]
        hud_bbox = (int(hb["x"]), int(hb["y"]), int(hb["w"]), int(hb["h"]))
    gt_icon = boxes.get("boxes", {}).get("icon")
    gt_bbox = (
        int(gt_icon["x"]), int(gt_icon["y"]),
        int(gt_icon["w"]), int(gt_icon["h"]),
    ) if gt_icon else None

    im = Image.open(cap_path).convert("RGB")
    t0 = time.time()
    res = find_icon_rgb_ncc(im, hud_bbox=hud_bbox)
    dt_ms = (time.time() - t0) * 1000.0

    print(f"\n[2] Canonical capture {CANONICAL_CAP}")
    print(f"    hud_bbox: {hud_bbox}")
    print(f"    GT icon:  {gt_bbox}")
    print(f"    RGB NCC: {res['bbox'] if res else None} score={res['score'] if res else None:.3f}"
          if res else "    RGB NCC: None")
    print(f"    elapsed: {dt_ms:.1f} ms")
    if res is None:
        print("    FAIL: no detection returned for canonical capture")
        return 2
    cand_x = res["bbox"][0]
    crop_w = hud_bbox[2] if hud_bbox is not None else im.size[0]
    crop_x = cand_x - (hud_bbox[0] if hud_bbox else 0)
    x_frac = crop_x / max(1, crop_w)
    print(f"    candidate x_frac in crop: {x_frac:.2f} "
          f"(must be < 0.30 to be on the actual icon, not digits)")
    if x_frac >= 0.30:
        print("    FAIL: candidate is in the digit area, not on the icon")
        return 2
    canonical_iou = iou_xywh(res["bbox"], gt_bbox) if gt_bbox else 0.0
    print(f"    IoU vs GT: {canonical_iou:.3f}")
    print("    PASS: candidate landed in leftmost 30% of search (icon region)")

    # ── 3. Five real labeled icons ──────────────────────────────────
    print("\n[3] Detection on 5 labeled icons (excluding mislabeled)")
    real_caps = [n for n in sorted(os.listdir(str(LABEL_DIR)))
                 if n.endswith("_rgb.png")
                 and n.startswith("pending_")
                 and n not in BLACKLISTED_LABELS]
    n_hits = 0
    ious = []
    for label_name in real_caps:
        # The label filename is pending_<cap_stem>_rgb.png — strip to find
        # the originating capture.
        cap_stem = label_name.removeprefix("pending_").removesuffix("_rgb.png")
        cap_p = REGION2_DIR / f"{cap_stem}.png"
        bx_p = REGION2_DIR / f"{cap_stem}.boxes.json"
        if not cap_p.is_file() or not bx_p.is_file():
            print(f"    {label_name:50s} SKIP (capture/boxes missing)")
            continue
        gt = _load_boxes(bx_p)
        if not gt or "boxes" not in gt or "icon" not in gt["boxes"]:
            print(f"    {label_name:50s} SKIP (no GT)")
            continue
        gt_icon = gt["boxes"]["icon"]
        gt_box = (int(gt_icon["x"]), int(gt_icon["y"]),
                  int(gt_icon["w"]), int(gt_icon["h"]))
        hb = gt.get("hud_bbox")
        hud_b = None
        if isinstance(hb, dict):
            hud_b = (int(hb["x"]), int(hb["y"]),
                     int(hb["w"]), int(hb["h"]))
        im = Image.open(cap_p).convert("RGB")
        r = find_icon_rgb_ncc(im, hud_bbox=hud_b)
        if r is None:
            print(f"    {label_name:50s} MISS  (no detection)")
            ious.append(0.0)
            continue
        det_iou = iou_xywh(r["bbox"], gt_box)
        ious.append(det_iou)
        if det_iou >= 0.30:
            n_hits += 1
            verdict = "HIT"
        else:
            verdict = "MISS"
        print(f"    {label_name:50s} {verdict}  bbox={r['bbox']} "
              f"score={r['score']:.2f} IoU={det_iou:.2f}")
    n_total = len(real_caps)
    print(f"\n    Detection rate: {n_hits}/{n_total} hits "
          f"(threshold IoU >= 0.30, mean IoU = "
          f"{(sum(ious)/len(ious) if ious else 0.0):.3f})")
    detection_pass = n_hits >= 4
    print("    " + ("PASS" if detection_pass else "FAIL")
          + ": >=4/5 required to beat grayscale NCC")

    # ── 4. 20 random region2 captures ───────────────────────────────
    print("\n[4] Performance + detection on 20 random region2 captures")
    all_caps = [n for n in sorted(os.listdir(str(REGION2_DIR)))
                if n.endswith(".png")]
    rng = random.Random(42)
    if len(all_caps) > 20:
        sample = rng.sample(all_caps, 20)
    else:
        sample = all_caps
    times = []
    n_detected = 0
    for n_name in sample:
        cap_p = REGION2_DIR / n_name
        bx_p = REGION2_DIR / f"{cap_p.stem}.boxes.json"
        gt = _load_boxes(bx_p)
        hud_b = None
        if isinstance(gt, dict) and isinstance(gt.get("hud_bbox"), dict):
            hb = gt["hud_bbox"]
            hud_b = (int(hb["x"]), int(hb["y"]),
                     int(hb["w"]), int(hb["h"]))
        try:
            im = Image.open(cap_p).convert("RGB")
        except Exception:
            continue
        t0 = time.time()
        r = find_icon_rgb_ncc(im, hud_bbox=hud_b)
        dt = (time.time() - t0) * 1000.0
        times.append(dt)
        if r is not None:
            n_detected += 1
    if times:
        ms_mean = sum(times) / len(times)
        ms_min, ms_max = min(times), max(times)
    else:
        ms_mean = ms_min = ms_max = 0.0
    print(f"    detected on {n_detected}/{len(sample)} captures")
    print(f"    ms/frame: mean={ms_mean:.1f}  min={ms_min:.1f}  max={ms_max:.1f}")

    # ── 5. RGB vs grayscale NCC peak comparison on canonical capture ──
    print(f"\n[5] RGB NCC vs Grayscale NCC peak positions on {CANONICAL_CAP}")
    canonical_im = np.asarray(Image.open(REGION2_DIR / CANONICAL_CAP).convert("RGB"),
                              dtype=np.float32)
    cap_boxes = _load_boxes(REGION2_DIR / f"cap_20260418_155446_555.boxes.json")
    hud_b = None
    if cap_boxes and isinstance(cap_boxes.get("hud_bbox"), dict):
        hb = cap_boxes["hud_bbox"]
        hud_b = (int(hb["x"]), int(hb["y"]), int(hb["w"]), int(hb["h"]))

    # Use template[0] (real labeled icon, scale 1.0) for the gray comparison.
    t_im = Image.open(LABEL_DIR / "pending_cap_20260418_155446_555_rgb.png").convert("RGB")
    t_arr = np.asarray(t_im, dtype=np.float32)
    gray_peaks = _gray_ncc_top_candidates(canonical_im, hud_b, t_arr, n=8)
    print("    Grayscale NCC top 8 peaks (score, x, y):")
    for s, x, y in gray_peaks:
        # Mark whether this is in the digit area or icon area.
        crop_x = x - (hud_b[0] if hud_b else 0)
        x_frac = crop_x / max(1, hud_b[2] if hud_b else 1)
        zone = "ICON" if x_frac < 0.30 else "DIGIT" if x_frac > 0.30 else "MID"
        print(f"      ({s:.3f}, {x:>3d}, {y:>3d}) [{zone}]  x_frac={x_frac:.2f}")

    # RGB NCC top peak.
    rgb_res = find_icon_rgb_ncc(Image.open(REGION2_DIR / CANONICAL_CAP).convert("RGB"),
                                hud_bbox=hud_b)
    if rgb_res is not None:
        x, y, w, h = rgb_res["bbox"]
        crop_x = x - (hud_b[0] if hud_b else 0)
        x_frac = crop_x / max(1, hud_b[2] if hud_b else 1)
        zone = "ICON" if x_frac < 0.30 else "DIGIT" if x_frac > 0.30 else "MID"
        print(f"    RGB NCC top peak: ({rgb_res['score']:.3f}, {x}, {y}) "
              f"[{zone}]  x_frac={x_frac:.2f}")

    n_gray_in_icon = sum(1 for s, x, _ in gray_peaks
                         if (x - (hud_b[0] if hud_b else 0))
                            / max(1, hud_b[2] if hud_b else 1) < 0.30)
    n_gray_in_digit = sum(1 for s, x, _ in gray_peaks
                          if (x - (hud_b[0] if hud_b else 0))
                             / max(1, hud_b[2] if hud_b else 1) >= 0.30)
    print(f"    Grayscale NCC: {n_gray_in_icon} peaks in icon area, "
          f"{n_gray_in_digit} peaks in digit area")
    print("    RGB NCC: 1 peak in icon area, 0 peaks in digit area"
          if rgb_res is not None
          and (rgb_res["bbox"][0] - (hud_b[0] if hud_b else 0))
              / max(1, hud_b[2] if hud_b else 1) < 0.30
          else "    RGB NCC: peak NOT in icon area (regression!)")

    # ── Summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)
    print(f"  templates                : {av['n_templates']} (real labels)")
    print(f"  canonical capture x_frac : {x_frac:.2f}  "
          f"(<0.30 means landed on ICON, not digits)")
    print(f"  canonical IoU vs GT      : {canonical_iou:.3f}")
    print(f"  labeled detection rate   : {n_hits}/{n_total}")
    print(f"  mean labeled IoU         : "
          f"{(sum(ious)/len(ious) if ious else 0.0):.3f}")
    print(f"  20-cap perf              : mean {ms_mean:.1f} ms / detect "
          f"{n_detected}/{len(sample)}")
    return 0 if detection_pass and rgb_res is not None else 3


if __name__ == "__main__":
    raise SystemExit(main())
