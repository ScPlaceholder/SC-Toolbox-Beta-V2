"""Calibrate per-slot kerning offsets from labeled signature captures.

Walks every labeled region2 capture (training_data_panels/user_*/region2/
*.png + *.json), runs the production pipeline (pill → crop_box → comma
detection → blob segmenter), and for each capture whose blob count
matches the GT digit count records the per-digit offset from the comma
center, normalized by row height. Aggregates across all kept captures
and writes a per-slot model to ``kerning_model.json`` (sibling to this
script).

The output is consumed at inference time by a kerning-locked slot
proposer that replaces blob-based digit positioning. The proposer
reads the kerning model, projects 5 candidate slot centers from the
detected comma + row height, and asks the CNN about each slot — no
blob-counting, no leading-narrow rescue heuristics, no n_digits
ambiguity. Stage 6 (wrong n_digits) and Stage 7 (digit position) in
the failure profile both collapse to slot-occupancy questions.

Slot indexing convention (relative to the comma):
    slot[0]  = leftmost digit, only present when n_digits == 5
    slot[1]  = digit immediately left of the comma (always present)
    slot[2]  = digit immediately right of the comma (always present)
    slot[3]  = next-right digit (always present)
    slot[4]  = rightmost digit (always present)

Usage::

    python hud_tracker/anchors/calibrate_kerning.py

Outputs ``hud_tracker/anchors/kerning_model.json``.
"""
from __future__ import annotations

import json
import logging
import statistics
import sys
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

# Make the SC_Toolbox tool tree importable when this script is run
# directly (``python calibrate_kerning.py``). Mirrors the path setup in
# determinism_check.py.
_THIS_DIR = Path(__file__).resolve().parent
_TOOL_DIR = _THIS_DIR.parent.parent
if str(_TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR))
if str(_TOOL_DIR / "scripts") not in sys.path:
    sys.path.insert(0, str(_TOOL_DIR / "scripts"))

from ocr.sc_ocr import api as _api  # noqa: E402
from hud_tracker.anchors.icon_voter import localize_icon  # noqa: E402
from hud_tracker.anchors.comma_finder import find_comma_voted  # noqa: E402

# Suppress chatter from production loggers — calibration generates a lot
# of pipeline calls and we don't want every per-capture diagnostic to
# flood stdout. We still see ERROR/CRITICAL.
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
for name in (
    "ocr.sc_ocr.api",
    "ocr.sc_ocr.signal_anchor",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.icon_contour",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.comma_finder",
):
    logging.getLogger(name).setLevel(logging.ERROR)

PANEL_ROOT = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI"
    r"\custom_skills\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
    r"\training_data_panels"
)

OUTPUT_PATH = _THIS_DIR / "kerning_model.json"


def _normalise_row_via_pipeline(png_path: Path) -> Optional[dict]:
    """Run the production pipeline up to and including comma detection.

    Returns a dict with the work-crop arrays + comma center + row height
    when the capture passes through pill / icon / crop_box / row-isolate
    cleanly. Returns ``None`` on any failure (capture is unusable for
    kerning measurement). Matches the determinism_check.py path so the
    calibration data lives in the same coordinate space the inference
    pipeline operates in.
    """
    img = Image.open(str(png_path)).convert("RGB")
    rgb = np.asarray(img, dtype=np.uint8)
    gray = rgb.max(axis=2).astype(np.uint8)

    wmr = _api._load_region2_world_model_for_api()
    if wmr is None:
        return None
    vfrac = (wmr.get("features") or {}).get("value")
    if vfrac is None:
        return None

    pill = _api._find_pill_for_signal(rgb)
    if pill is None:
        return None
    px, py, pw, ph = pill

    vx = int(round(px + float(vfrac["x_frac"]["mean"]) * pw))
    vy = int(round(py + float(vfrac["y_frac"]["mean"]) * ph))
    vw = int(round(float(vfrac["w_frac"]["mean"]) * pw))
    vh = int(round(float(vfrac["h_frac"]["mean"]) * ph))

    icon_loc = localize_icon(rgb)
    if icon_loc is None:
        return None
    ix, iy, iw, ih = icon_loc["bbox"]
    icon_anchor = ix + iw + max(2, int(pw * 0.03))
    delta = vx - icon_anchor
    vx = icon_anchor
    vw = vw + delta

    rhs_ceiling = px + pw - max(2, int(pw * 0.05))
    digits_x2 = min(vx + vw, rhs_ceiling, gray.shape[1])
    digits_x1 = max(0, vx)
    digits_y1 = max(0, vy)
    digits_y2 = min(vy + vh, gray.shape[0])
    if digits_x2 <= digits_x1 or digits_y2 <= digits_y1:
        return None

    work = gray[digits_y1:digits_y2, digits_x1:digits_x2].copy()
    work_rgb = rgb[digits_y1:digits_y2, digits_x1:digits_x2].copy()

    # Row-isolate (mirrors determinism_check.py).
    try:
        import extract_labeled_glyphs as xlg  # type: ignore
        band = xlg._find_main_row_bounds(work) if hasattr(
            xlg, "_find_main_row_bounds"
        ) else None
    except Exception:
        band = None
    if band is not None:
        by1, by2 = band
        work = work[by1:by2, :]
        work_rgb = work_rgb[by1:by2, :]

    # Stretch dynamic range (same as production).
    w_arr = work.astype(np.float32)
    mn, mx = float(w_arr.min()), float(w_arr.max())
    if mx - mn > 8:
        w_arr = (w_arr - mn) * (255.0 / (mx - mn))
        work = np.clip(w_arr, 0, 255).astype(np.uint8)

    # Upscale tiny rows (production rule).
    h_pre = work.shape[0]
    if h_pre < 28:
        scale_up = max(2, 32 // max(1, h_pre))
        work = np.asarray(
            Image.fromarray(work, mode="L").resize(
                (work.shape[1] * scale_up, h_pre * scale_up),
                Image.LANCZOS,
            ),
            dtype=np.uint8,
        )
        work_rgb = np.asarray(
            Image.fromarray(work_rgb, mode="RGB").resize(
                (work_rgb.shape[1] * scale_up,
                 work_rgb.shape[0] * scale_up),
                Image.LANCZOS,
            ),
            dtype=np.uint8,
        )

    # Polarity-canonicalize (same path the segmenter uses).
    work_canon = _api._canonicalize_polarity(work)

    # Comma detection. ``find_comma_voted`` takes the RGB array (it
    # internally polarity-canonicalizes); the result is a dict with
    # ``bbox`` (x, y, w, h) and ``x_center`` (float).
    comma = find_comma_voted(work_rgb)
    if comma is None:
        return None
    comma_center = float(comma.get("x_center", 0.0))
    bb = comma.get("bbox") or [0, 0, 0, 0]
    comma_x_lo = int(bb[0])
    comma_x_hi = int(bb[0]) + int(bb[2])

    return {
        "work_canon": work_canon,
        "work_rgb": work_rgb,
        "row_height": float(work_canon.shape[0]),
        "comma_center": comma_center,
        "comma_extent": (comma_x_lo, comma_x_hi),
    }


def _segment_for_kerning(
    work_canon: np.ndarray, expected_count: int,
) -> Optional[list[tuple[int, int, int, int]]]:
    """Run the production segmenter on a work crop and return the
    digit bboxes if the count matches ``expected_count``.

    Returns ``None`` when the segmenter produces a different count —
    that capture is rejected for kerning measurement (we can't trust
    its slot assignment). This is the calibration-time filter that
    makes per-slot offset distributions clean.
    """
    hud_bin = _api._adaptive_binarize_multi(work_canon, expected_count=expected_count)
    hud_bin = _api._strip_pill_outline_bridges(hud_bin)
    hud_bin = _api._mask_commas_in_signature_band(hud_bin)
    crops, boxes = _api._segment_glyphs(
        work_canon, hud_bin, disable_gap_cut=True,
    )
    crops, boxes = _api._trim_comma_fused_into_signature_boxes(
        crops, boxes, work_canon, hud_bin,
    )
    crops, boxes = _api._drop_blacklisted_signature_glyphs(crops, boxes)
    crops, boxes = _api._enforce_comma_signature_structure(crops, boxes)
    crops, boxes = _api._split_wide_signature_spans(
        work_canon, hud_bin, crops, boxes, expected_count=expected_count,
    )
    if len(boxes) != expected_count:
        return None
    return list(boxes)


def _measure_one(
    png_path: Path,
    gt_value: str,
    *,
    require_cnn_match: bool = True,
    width_uniformity: float = 1.5,
) -> Optional[dict]:
    """Process one capture; return per-slot offset measurements.

    Filters applied (so the kerning model trains on clean data):

    * ``digit_count == GT`` (segmenter agrees with the GT length).
    * ``width_uniformity``: every digit bbox width must be within
      ``width_uniformity`` × the row's median digit width — both
      directions. Filters out comma-fused / icon-stub-fused bboxes
      where one slot ends up dramatically wider than its peers.
    * ``require_cnn_match``: the gray PRIMARY CNN's classification at
      each slot matches the GT digit. The segmenter occasionally hits
      the right count by luck while shifting the slot positions; this
      filter rejects those by demanding the CNN sees the right digit
      in each emitted bbox.
    """
    digits_only = "".join(c for c in gt_value if c.isdigit())
    n_digits = len(digits_only)
    if n_digits not in (4, 5):
        return None

    norm = _normalise_row_via_pipeline(png_path)
    if norm is None:
        return None
    boxes = _segment_for_kerning(norm["work_canon"], expected_count=n_digits)
    if boxes is None:
        return None

    boxes = sorted(boxes, key=lambda b: b[0])
    row_h = norm["row_height"]
    if row_h <= 0:
        return None

    # Width-uniformity filter: reject captures with one outlier-wide
    # bbox (typically a digit+comma or icon+digit fusion).
    widths = [b[2] for b in boxes]
    sorted_widths = sorted(widths)
    median_w = float(sorted_widths[len(sorted_widths) // 2])
    if median_w <= 0:
        return None
    for w in widths:
        ratio_hi = w / median_w
        ratio_lo = median_w / max(1.0, w)
        if max(ratio_hi, ratio_lo) > width_uniformity:
            return None

    # CNN-match filter: re-extract crops from the canonical gray, run
    # the gray PRIMARY signal CNN, and verify each emitted slot reads
    # back to the GT digit at that position. Mirrors the production
    # crop preprocessing exactly so the calibration numbers reflect
    # what the inference path will see.
    if require_cnn_match:
        work_canon = norm["work_canon"]
        crops = []
        for bb in boxes:
            bx, by, bw, bh = bb
            if bx + bw > work_canon.shape[1] or by + bh > work_canon.shape[0]:
                return None
            g = work_canon[by:by + bh, bx:bx + bw].astype(np.float32)
            pad = 2
            padded = np.full(
                (g.shape[0] + pad * 2, g.shape[1] + pad * 2),
                255.0, dtype=np.float32,
            )
            padded[pad:pad + g.shape[0], pad:pad + g.shape[1]] = g
            pil = Image.fromarray(padded.astype(np.uint8)).resize(
                (28, 28), Image.BILINEAR,
            )
            crops.append(np.array(pil, dtype=np.float32) / 255.0)
        try:
            results = _api._classify_crops_signal(crops)
        except Exception:
            return None
        if not results or len(results) != n_digits:
            return None
        # Sequential-by-x match against GT.
        for (ch, _conf), gt_ch in zip(results, digits_only):
            if str(ch) != gt_ch:
                return None

    slot_offsets: dict[str, float] = {}
    digit_widths: dict[str, float] = {}
    base_slot = 0 if n_digits == 5 else 1
    for k, (bx, _by, bw, _bh) in enumerate(boxes):
        slot_idx = base_slot + k
        center = bx + bw * 0.5
        offset = (center - norm["comma_center"]) / row_h
        slot_offsets[str(slot_idx)] = float(offset)
        digit_widths[str(slot_idx)] = float(bw) / row_h

    return {
        "stem": png_path.stem,
        "n_digits": n_digits,
        "row_height": float(row_h),
        "comma_center": float(norm["comma_center"]),
        "slot_offsets": slot_offsets,
        "digit_widths": digit_widths,
    }


def main() -> int:
    samples: list[dict] = []
    n_captures_seen = 0
    n_captures_kept = 0
    n_skipped_no_pipeline = 0
    n_skipped_wrong_count = 0
    n_skipped_no_gt = 0

    for png_path in sorted(PANEL_ROOT.glob("user_*/region2/*.png")):
        # Honor `.skip` markers (matches the harness skip-marker policy).
        if png_path.with_suffix(".skip").exists():
            continue
        json_path = png_path.with_suffix(".json")
        if not json_path.exists():
            continue
        try:
            meta = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        gt = str(meta.get("value", "")).strip()
        if not gt:
            n_skipped_no_gt += 1
            continue

        n_captures_seen += 1
        try:
            # CNN-match too tight in current production state (only 1
            # of 232 captures classified clean across-the-board). Use
            # width-uniformity alone, which is the comma-fusion /
            # icon-stub-fusion guard — the actual pollution source we
            # need to reject. CNN agreement can be added back later
            # when classifier accuracy improves.
            measurement = _measure_one(
                png_path, gt, require_cnn_match=False, width_uniformity=1.5,
            )
        except Exception as exc:
            print(f"  ERROR {png_path.name}: {exc}")
            continue
        if measurement is None:
            # Distinguish "pipeline failed" from "wrong count" — same
            # rejection in practice, but useful for debug. We don't
            # currently differentiate inside _measure_one (both return
            # None); count both under wrong_count for now.
            n_skipped_wrong_count += 1
            continue
        n_captures_kept += 1
        samples.append(measurement)

    print(f"\nCalibration walked {n_captures_seen} captures.")
    print(f"  Kept (digit_count match):  {n_captures_kept}")
    print(f"  Rejected (pipeline/count): {n_skipped_wrong_count}")
    print(f"  Skipped (no gt):           {n_skipped_no_gt}")

    if not samples:
        print("No usable samples. Aborting.")
        return 1

    # Aggregate per-slot offsets + widths.
    per_slot_offsets: dict[str, list[float]] = {
        str(i): [] for i in range(5)
    }
    per_slot_widths: dict[str, list[float]] = {
        str(i): [] for i in range(5)
    }
    n_digits_hist: dict[int, int] = {4: 0, 5: 0}
    for s in samples:
        n_digits_hist[s["n_digits"]] = (
            n_digits_hist.get(s["n_digits"], 0) + 1
        )
        for k, v in s["slot_offsets"].items():
            per_slot_offsets[k].append(v)
        for k, v in s["digit_widths"].items():
            per_slot_widths[k].append(v)

    print("\nPer-slot sample counts:")
    for k in sorted(per_slot_offsets.keys()):
        print(
            f"  slot[{k}]: n={len(per_slot_offsets[k])} "
            f"digit_widths_n={len(per_slot_widths[k])}"
        )

    print("\nDigit-count histogram:")
    for k, v in sorted(n_digits_hist.items()):
        print(f"  n_digits={k}: {v} captures")

    slots_out: dict[str, dict[str, float]] = {}
    for k in sorted(per_slot_offsets.keys()):
        offs = per_slot_offsets[k]
        widths = per_slot_widths[k]
        if not offs:
            continue
        slots_out[k] = {
            "center_offset_mean": float(statistics.fmean(offs)),
            "center_offset_median": float(statistics.median(offs)),
            "center_offset_sigma": (
                float(statistics.stdev(offs)) if len(offs) >= 2 else 0.0
            ),
            "digit_w_mean": float(statistics.fmean(widths)) if widths else 0.0,
            "digit_w_median": (
                float(statistics.median(widths)) if widths else 0.0
            ),
            "n": len(offs),
        }

    out_doc = {
        "schema": "kerning_v1",
        "units": "fraction-of-row-height",
        "n_captures_walked": n_captures_seen,
        "n_captures_kept": n_captures_kept,
        "n_digits_histogram": n_digits_hist,
        "slots": slots_out,
        "comment": (
            "center_offset = (digit_center_x - comma_center_x) / row_height. "
            "Slot 0 is leftmost digit (only present in 5-digit values). "
            "Slot 1 is the digit immediately left of the comma. Slots "
            "2/3/4 are right of the comma. Slots 1..4 are always present; "
            "slot 0 is empty when n_digits == 4."
        ),
    }

    OUTPUT_PATH.write_text(
        json.dumps(out_doc, indent=2),
        encoding="utf-8",
    )
    print(f"\nWrote kerning model: {OUTPUT_PATH}")

    # Console summary so you can sanity-check spread + monotonicity.
    print("\nPer-slot summary (offset units = fraction of row height):")
    for k in sorted(slots_out.keys()):
        info = slots_out[k]
        print(
            f"  slot[{k}]: mean={info['center_offset_mean']:+.3f} "
            f"median={info['center_offset_median']:+.3f} "
            f"sigma={info['center_offset_sigma']:.3f} "
            f"digit_w_mean={info['digit_w_mean']:.3f} "
            f"n={info['n']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
