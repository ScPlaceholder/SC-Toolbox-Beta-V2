"""Public API for SC-OCR.

Signature-compatible replacements for the legacy three-engine
call sites:

    scan_region(region)     →  Optional[int]
    scan_hud_onnx(region)   →  dict[str, Optional[float]]
    scan_refinery(region)   →  Optional[list[dict]]

Architecture:
  capture → polarity-correct → Otsu binarize → find mineral row
  (pure NumPy) → fixed offsets to value rows → _find_value_crop
  (NumPy column-density) → segment glyphs → ONNX batch classify
  → validate

23 ms per scan. No Tesseract. No PaddleOCR. No subprocesses.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
from PIL import Image

from . import capture, fallback, preprocess, validate

log = logging.getLogger(__name__)

# Reuse proven legacy helpers that are pure NumPy (no Tesseract dep)
# Now that _build_text_mask is polarity-aware, _find_mineral_row and
# _find_value_crop both work on light AND dark backgrounds.
from ..onnx_hud_reader import (  # noqa: E402
    _find_mineral_row,
    _find_value_crop,
    _otsu,
)


def _find_mineral_row_universal(img: Image.Image) -> Optional[tuple[int, int]]:
    """Find the mineral-name row on ANY background via local contrast.

    Uses a high-pass filter (|pixel - gaussian_blur|) to detect text
    edges regardless of polarity. Text has sharp edges that differ
    from their local neighborhood; smooth backgrounds (bright sky,
    dark space, asteroid rock) have low local contrast. This works
    identically on dark-on-light AND light-on-dark text.

    Then runs the same header → mineral-row detection logic as the
    legacy pipeline.
    """
    from PIL import ImageFilter

    gray = np.array(img.convert("L"), dtype=np.float32)
    H, W = gray.shape
    median = float(np.median(gray))

    if median < 130:
        # Dark background: proven brightness-based approach
        # (matches legacy _find_mineral_row exactly)
        text_mask = gray > 150
    else:
        # Light background: local-contrast approach
        # Detects text edges regardless of polarity
        blurred = np.asarray(
            Image.fromarray(gray.astype(np.uint8)).filter(
                ImageFilter.GaussianBlur(radius=5)
            ),
            dtype=np.float32,
        )
        local_contrast = np.abs(gray - blurred)
        text_mask = local_contrast > 15
    row_counts = text_mask.sum(axis=1)

    # Build row spans
    MIN_ROW_HEIGHT = 12
    rows: list[tuple[int, int, int]] = []
    in_row = False
    start = 0
    peak = 0
    for y in range(H + 1):
        val = int(row_counts[y]) if y < H else 0
        if val >= 5 and not in_row:
            in_row = True
            start = y
            peak = val
        elif val >= 5 and in_row:
            peak = max(peak, val)
        elif val < 5 and in_row:
            in_row = False
            if y - start >= MIN_ROW_HEIGHT:
                rows.append((start, y, peak))

    if len(rows) < 2:
        return None

    # Find the header ("SCAN RESULTS"): first band with decent peak
    header_idx = None
    for i, (y1, y2, pk) in enumerate(rows):
        if pk >= 40 and (y2 - y1) <= 40:
            header_idx = i
            break
    if header_idx is None:
        for i, (y1, y2, pk) in enumerate(rows):
            if pk >= 20 and (y2 - y1) <= 40:
                header_idx = i
                break
    if header_idx is None:
        return None

    # Mineral name = next qualifying band after header
    for y1, y2, pk in rows[header_idx + 1:]:
        if pk >= 20 and (y2 - y1) <= 40:
            return (y1, y2)

    return None

# Fixed HUD geometry offsets (pixel distance from mineral-row center
# to each value row). Constant across all scans at a given HUD scale.
_ROW_HEIGHT_HALF = 15
_OFFSETS = {"mass": 43, "resistance": 82, "instability": 120}
_LABEL_RIGHTS = {"mass": 110, "resistance": 200, "instability": 205}


# ── Glyph extraction + ONNX classification ────────────────────────

def _segment_glyphs(gray: np.ndarray, binary: np.ndarray) -> list[np.ndarray]:
    """Segment individual glyphs and return 28x28 float32 crops in [0,1].

    Replicates the EXACT preprocessing the ONNX model was trained on:
      glyph from grayscale → pad with 255 → resize 28x28 → / 255.
    """
    h, w = gray.shape
    proj = np.sum(binary > 0, axis=0)
    spans: list[tuple[int, int]] = []
    in_char = False
    start = 0
    for x in range(w + 1):
        val = proj[x] if x < w else 0
        if val > 0 and not in_char:
            in_char = True; start = x
        elif val == 0 and in_char:
            in_char = False
            if x - start >= 3:
                spans.append((start, x))

    crops: list[np.ndarray] = []
    for x1, x2 in spans:
        ys = np.where(np.any(binary[:, x1:x2] > 0, axis=1))[0]
        if len(ys) < 3:
            continue
        y1, y2 = ys[0], ys[-1] + 1
        crop = gray[y1:y2, x1:x2].astype(np.float32)
        pad = 2
        padded = np.full(
            (crop.shape[0] + pad * 2, crop.shape[1] + pad * 2),
            255.0, dtype=np.float32,
        )
        padded[pad:pad + crop.shape[0], pad:pad + crop.shape[1]] = crop
        pil = Image.fromarray(padded.astype(np.uint8)).resize(
            (28, 28), Image.BILINEAR,
        )
        crops.append(np.array(pil, dtype=np.float32) / 255.0)
    return crops


def _classify_crops(crops: list[np.ndarray]) -> list[tuple[str, float]]:
    """Batch-classify 28x28 crops via the ONNX CNN."""
    if not crops or not fallback._ensure_model():
        return []
    session = fallback._session
    char_classes = fallback._char_classes
    inp_name = session.get_inputs()[0].name
    batch = np.array(crops, dtype=np.float32).reshape(-1, 1, 28, 28)
    try:
        logits = session.run(None, {inp_name: batch})[0]
    except Exception as exc:
        log.debug("sc_ocr: ONNX inference failed: %s", exc)
        return []
    results = []
    for i in range(len(crops)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        idx = int(np.argmax(probs))
        results.append((char_classes[idx], float(probs[idx])))
    return results


def _ocr_value_crop(value_crop: Image.Image) -> tuple[str, list[float]]:
    """OCR a tight value crop → (text, per_char_confidences)."""
    from PIL import ImageFilter

    gray = np.array(value_crop.convert("L"), dtype=np.uint8)
    median = float(np.median(gray))

    if median < 140:
        # Dark background: standard Otsu (bright text on dark bg)
        thr = _otsu(gray)
        binary = (gray > thr).astype(np.uint8) * 255
    else:
        # Light background: local-contrast binarization.
        # Simple inversion + Otsu fails because inverted text and
        # background end up at similar brightness. Local contrast
        # detects the text EDGES regardless of absolute brightness.
        blurred = np.asarray(
            Image.fromarray(gray).filter(ImageFilter.GaussianBlur(radius=3)),
            dtype=np.float32,
        )
        local_contrast = np.abs(gray.astype(np.float32) - blurred)
        binary = (local_contrast > 10).astype(np.uint8) * 255
        # Invert gray for glyph extraction (ONNX expects bright text)
        gray = 255 - gray

    crops = _segment_glyphs(gray, binary)
    results = _classify_crops(crops)
    text = "".join(ch for ch, _ in results)
    confs = [c for _, c in results]
    return text, confs


# ── Public API ─────────────────────────────────────────────────────

def scan_region(region: dict) -> Optional[int]:
    """Read a signal-number region → int in [1000, 35000]."""
    img = capture.grab(region)
    if img is None:
        return None
    rgb = np.asarray(img, dtype=np.uint8)
    gray = np.asarray(img.convert("L"), dtype=np.uint8)
    # Polarity correction
    if np.median(gray) > 140:
        gray = 255 - gray
    thr = _otsu(gray)
    binary = (gray > thr).astype(np.uint8) * 255
    crops = _segment_glyphs(gray, binary)
    results = _classify_crops(crops)
    text = "".join(ch for ch, _ in results)
    return validate.validate_signal(text)


def scan_hud_onnx(region: dict) -> dict:
    """Read the mining HUD panel → {mass, resistance, instability, panel_visible}.

    Uses pure-NumPy mineral-row detection + fixed pixel offsets to
    locate value crops, then ONNX batch classification. No Tesseract,
    no PaddleOCR, no subprocesses. ~23 ms per scan.
    """
    empty = {
        "mass": None,
        "resistance": None,
        "instability": None,
        "panel_visible": False,
    }
    t0 = time.time()
    img = capture.grab(region)
    if img is None:
        return empty

    gray = np.asarray(img.convert("L"), dtype=np.uint8)

    # Polarity dispatch: light-background panels need inversion
    # before mineral-row detection and value OCR can work.
    median_gray = float(np.median(gray))
    if median_gray > 130:
        gray = 255 - gray

    # Find the mineral-name row (e.g. "SAVRILIUM (ORE)") via pure
    # NumPy horizontal-projection heuristics. This is the anchor
    # for the fixed-offset value row positions.
    #
    # On light backgrounds, _find_mineral_row needs the image with
    # inverted polarity so its text-mask heuristic sees text (now
    # dark) as bright. We create an inverted PIL image for this.
    # Use the legacy _find_mineral_row which now works on BOTH
    # backgrounds thanks to the polarity-aware _build_text_mask.
    mineral_row = _find_mineral_row(img)
    if mineral_row is None:
        return empty

    mr_center = (mineral_row[0] + mineral_row[1]) // 2
    result = dict(empty)
    result["panel_visible"] = True

    # For _find_value_crop: on light backgrounds, invert the
    # grayscale so the legacy's `gray > 150` text mask sees
    # the dark text as bright. Also create an inverted PIL image
    # for the crop extraction.
    if median_gray > 130:
        gray_for_crop = 255 - gray
        img_for_crop = Image.fromarray(
            255 - np.asarray(img, dtype=np.uint8), mode="RGB",
        )
    else:
        gray_for_crop = gray
        img_for_crop = img

    for field in ("mass", "resistance", "instability"):
        center = mr_center + _OFFSETS[field]
        y1 = max(0, center - _ROW_HEIGHT_HALF)
        y2 = min(img.height, center + _ROW_HEIGHT_HALF)
        lbl_right = _LABEL_RIGHTS[field]

        if median_gray < 130:
            # Dark bg: legacy column-density crop (proven)
            value_crop = _find_value_crop(
                img, gray, y1, y2,
                x_min=max(0, lbl_right + 6),
            )
        else:
            # Light bg: _find_value_crop's column-density clustering
            # fails on edge-based masks (local contrast produces
            # fragmented clusters). Just take the full right portion
            # of the row from the label-right edge — simpler and
            # more robust. The segmenter will isolate individual
            # glyphs within this wider strip.
            x_start = max(0, lbl_right + 6)
            if x_start < img.size[0] - 10:
                value_crop = img.crop((x_start, y1, img.size[0], y2))
            else:
                value_crop = None
        if value_crop is None:
            continue

        text, confs = _ocr_value_crop(value_crop)
        if not text:
            continue

        if field == "mass":
            result["mass"] = validate.validate_mass(text)
        elif field == "resistance":
            result["resistance"] = validate.validate_pct(text)
        elif field == "instability":
            result["instability"] = validate.validate_instability(
                text, confidences=confs,
            )

    elapsed_ms = (time.time() - t0) * 1000
    log.info(
        "sc_ocr: mass=%s resistance=%s instability=%s in %.0fms",
        result["mass"], result["resistance"], result["instability"],
        elapsed_ms,
    )
    return result


def scan_refinery(region: dict, station: str = "") -> Optional[list[dict]]:
    """Read a refinery terminal region → list of order dicts.

    v1: delegates to the legacy refinery_reader since it needs
    full-alphabet recognition which the 13-class ONNX model can't do.
    """
    try:
        from ..refinery_reader import scan_refinery as legacy_scan
        return legacy_scan(region, station)
    except Exception as exc:
        log.debug("sc_ocr.scan_refinery: legacy fallback: %s", exc)
        return None
