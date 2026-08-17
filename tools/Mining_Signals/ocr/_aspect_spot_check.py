"""Spot-check the aspect-matched 40x28 CNN against three known-confused
captures from the production 28x28 v3 CNN.

For each capture, extract the target digit at the same bbox using both
the 28x28 path and the 40x28 path, run both ONNX models, and report
the read.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import onnxruntime as ort
from PIL import Image

PROD = Path(
    r"C:\Users\_user\AppData\Local\SC_Toolbox\current\tools\Mining_Signals"
)
WINGMAN = Path(
    r"C:\Users\_user\AppData\Roaming\ShipBit\WingmanAI\custom_skills"
    r"\SC_Toolbox_Beta_V1.2\tools\Mining_Signals"
)

V3_PATH = PROD / "ocr" / "models" / "model_signal_rgb_cnn_v3.onnx"
ASPECT_PATH = PROD / "ocr" / "models" / "model_signal_rgb_aspect_40x28.onnx"

V3_CHARS = "0123456789@"
ASPECT_CHARS = "0123456789"

sys.path.insert(0, str(PROD))
from scripts.extract_labeled_glyphs import (  # type: ignore
    _otsu, _locate_icon_via_blacklist_match, _isolate_main_row,
    _segment_digits,
)


def _glyph_to_28x28(gray_crop, x1, x2):
    if np.median(gray_crop) > 140:
        work = 255 - gray_crop
    else:
        work = gray_crop
    thr = _otsu(work)
    binary = (work > thr).astype(np.uint8)
    glyph_col = binary[:, x1:x2]
    ys = np.where(np.any(glyph_col > 0, axis=1))[0]
    if len(ys) < 2:
        return None
    ya, yb = int(ys[0]), int(ys[-1]) + 1
    glyph = gray_crop[ya:yb, x1:x2].astype(np.float32)
    pad = 2
    padded = np.full(
        (glyph.shape[0] + pad * 2, glyph.shape[1] + pad * 2),
        255.0, dtype=np.float32,
    )
    padded[pad:pad + glyph.shape[0], pad:pad + glyph.shape[1]] = glyph
    pil = Image.fromarray(padded.astype(np.uint8)).resize((28, 28), Image.BILINEAR)
    return np.asarray(pil, dtype=np.uint8)


def _glyph_to_40x28(gray_crop, x1, x2):
    if np.median(gray_crop) > 140:
        work = 255 - gray_crop
    else:
        work = gray_crop
    thr = _otsu(work)
    binary = (work > thr).astype(np.uint8)
    glyph_col = binary[:, x1:x2]
    ys = np.where(np.any(glyph_col > 0, axis=1))[0]
    if len(ys) < 2:
        return None
    ya, yb = int(ys[0]), int(ys[-1]) + 1
    glyph = gray_crop[ya:yb, x1:x2].astype(np.float32)
    pad = 2
    padded = np.full(
        (glyph.shape[0] + pad * 2, glyph.shape[1] + pad * 2),
        255.0, dtype=np.float32,
    )
    padded[pad:pad + glyph.shape[0], pad:pad + glyph.shape[1]] = glyph
    pil = Image.fromarray(padded.astype(np.uint8)).resize((28, 40), Image.BILINEAR)
    return np.asarray(pil, dtype=np.uint8)


def _prep_gray_unmasked(img_path: Path) -> np.ndarray:
    """Return the raw grayscale (no masking, no row isolation). Used
    when bbox coordinates come from a glyphs.json (those positions
    are in the original capture's coord space)."""
    img = Image.open(img_path).convert("L")
    return np.asarray(img, dtype=np.uint8).copy()


def _prep_gray_for_segment(img_path: Path) -> np.ndarray:
    """Reproduce the icon-mask + row-isolate that the extractor uses,
    so the bbox coordinates align with the trained model's expected
    glyph extent."""
    img = Image.open(img_path).convert("L")
    gray = np.asarray(img, dtype=np.uint8).copy()
    img_w = gray.shape[1]
    bg = int(np.median(gray))
    icon_right = _locate_icon_via_blacklist_match(gray)
    floor_mask = int(img_w * 0.30)
    mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
    if 0 < mask_w < img_w:
        gray[:, :mask_w] = bg
    gray = _isolate_main_row(gray)
    return gray


def _classify(sess: ort.InferenceSession, glyph: np.ndarray, classes: str,
              size_hw: tuple) -> tuple:
    H, W = size_hw
    pil = Image.fromarray(glyph, mode="L").convert("RGB")
    if pil.size != (W, H):
        pil = pil.resize((W, H), Image.BILINEAR)
    arr = np.asarray(pil, dtype=np.float32) / 255.0
    x = arr.transpose(2, 0, 1)[None, ...].astype(np.float32)
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]
    logits = out[0]
    e = np.exp(logits - logits.max())
    p = e / e.sum()
    idx = int(np.argmax(p))
    return classes[idx], float(p[idx])


def main() -> int:
    if not V3_PATH.exists():
        print(f"v3 not found: {V3_PATH}")
        return 1
    if not ASPECT_PATH.exists():
        print(f"aspect not found: {ASPECT_PATH}")
        return 1

    v3 = ort.InferenceSession(str(V3_PATH), providers=["CPUExecutionProvider"])
    aspect = ort.InferenceSession(
        str(ASPECT_PATH), providers=["CPUExecutionProvider"],
    )

    # The three test cases. bbox positions come from sibling
    # glyphs.json (Glyph Forge debug output) when present — those are
    # the SAME positions the production reader uses. For the 978
    # capture we don't have a glyphs.json, so we fall back to
    # column-segmentation on the icon-masked + row-isolated gray.
    base = (WINGMAN / "training_data_panels" / "user_20260418_081525"
            / "region2")
    cases = [
        {
            "name": "11,520 -> 5 (between comma and 2)",
            "img": base / "cap_20260418_085431_378.png",
            "label": "11,520",
            "tile_index": 2,  # 0='1', 1='1', 2='5', 3='2', 4='0'
            "expected": "5",
            "v3_old_read": "3",
            "use_glyphs_json": True,
        },
        {
            "name": "21,200 -> trailing 0",
            "img": base / "cap_20260418_085436_978.png",
            "label": "21,200",
            "tile_index": 4,  # 0='2', 1='1', 2='2', 3='0', 4='0'
            "expected": "0",
            "v3_old_read": "7",
            "use_glyphs_json": False,
        },
        {
            "name": "17,020 -> second 0 (trailing)",
            "img": base / "cap_20260418_085400_739.png",
            "label": "17,020",
            "tile_index": 4,  # 0='1', 1='7', 2='0', 3='2', 4='0'
            "expected": "0",
            "v3_old_read": "8",
            "use_glyphs_json": True,
        },
    ]

    print("=== aspect 40x28 spot-check ===")
    print(f"v3 (28x28):   {V3_PATH.name}")
    print(f"aspect 40x28: {ASPECT_PATH.name}")
    print()

    new_correct = 0
    for case in cases:
        if not case["img"].exists():
            print(f"SKIP {case['name']}: image missing {case['img']}")
            continue

        chars = [c for c in case["label"].replace(",", "") if c.isdigit()]
        idx = case["tile_index"]

        if case.get("use_glyphs_json"):
            # Use Glyph Forge's recorded bboxes — those are the
            # production segmenter's output and are the actual bboxes
            # the production reader sees when it misclassifies.
            gj = case["img"].with_suffix("").parent / (
                case["img"].stem + ".glyphs.json"
            )
            try:
                tiles = json.loads(gj.read_text())["tiles"]
                tile = tiles[idx]
                x1, x2 = int(tile["x1"]), int(tile["x2"])
            except Exception as exc:
                print(f"SKIP {case['name']}: glyphs.json read failed: {exc}")
                continue
            # Use the unmasked gray since Glyph Forge bboxes are in
            # the source coord space.
            gray = _prep_gray_unmasked(case["img"])
        else:
            gray = _prep_gray_for_segment(case["img"])
            spans = _segment_digits(gray, expected_count=len(chars))
            if len(spans) > len(chars):
                spans = spans[-len(chars):]
            if len(spans) != len(chars):
                print(f"SKIP {case['name']}: span count {len(spans)} "
                      f"!= chars {len(chars)}")
                continue
            if idx >= len(spans):
                print(f"SKIP {case['name']}: tile_index {idx} >= spans "
                      f"{len(spans)}")
                continue
            x1, x2 = spans[idx]

        glyph_28 = _glyph_to_28x28(gray, x1, x2)
        glyph_40 = _glyph_to_40x28(gray, x1, x2)
        if glyph_28 is None or glyph_40 is None:
            print(f"SKIP {case['name']}: glyph extract returned None")
            continue

        ch_v3, p_v3 = _classify(v3, glyph_28, V3_CHARS, (28, 28))
        ch_a, p_a = _classify(aspect, glyph_40, ASPECT_CHARS, (40, 28))

        v3_ok = ch_v3 == case["expected"]
        a_ok = ch_a == case["expected"]
        if a_ok:
            new_correct += 1

        print(f"--- {case['name']} ---")
        print(f"  bbox: x1={x1} x2={x2}  expected={case['expected']!r}")
        print(f"  28x28 v3:    read {ch_v3!r} p={p_v3:.3f} "
              f"({'OK' if v3_ok else 'WRONG'})")
        print(f"  40x28 aspect: read {ch_a!r} p={p_a:.3f} "
              f"({'OK' if a_ok else 'WRONG'})")
        print(f"  brief noted v3 reads {case['v3_old_read']!r}; "
              f"observed v3 reads {ch_v3!r}")
        print()

    print(f"=== summary: 40x28 correct on {new_correct}/{len(cases)} "
          f"known-confused captures ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
