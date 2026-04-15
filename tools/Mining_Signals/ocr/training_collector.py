"""Training data collector for ONNX model fine-tuning.

Runs passively alongside the Tesseract signal scanner. When Tesseract
produces a confident result (both engines agree), the collector:

1. Takes the raw captured image
2. Binarizes and segments it into individual character bounding boxes
3. Crops, pads, and resizes each character to 28×28 (matching the ONNX
   model's input format)
4. Saves each character image as a .png, labeled by the digit from
   Tesseract's agreed-upon result

Over time this builds a dataset of real character images from the
user's exact screen, resolution, brightness, and HUD overlay conditions.
This dataset can then fine-tune the ONNX model to solve issues like
the 0/8 and 9/8 confusion seen at specific font sizes.

Training data is saved to: tools/Mining_Signals/training_data/{digit}/
Each file: {timestamp}_{variant}_{index}.png (28×28 grayscale)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_TRAINING_DIR = os.path.join(os.path.dirname(_MODULE_DIR), "training_data")

# Track how many samples we've saved to avoid filling disk
_session_count = 0
_MAX_PER_SESSION = 2000  # stop collecting after this many per app session
_MIN_CONFIDENCE_VARIANTS = 2  # require at least N Tesseract variants to agree


def _ensure_dirs():
    """Create digit subdirectories 0-9 if they don't exist."""
    for digit in "0123456789":
        d = os.path.join(_TRAINING_DIR, digit)
        os.makedirs(d, exist_ok=True)


def _otsu(gray: np.ndarray) -> int:
    """Compute Otsu threshold."""
    hist, _ = np.histogram(gray.flatten(), bins=256, range=(0, 256))
    total = gray.size
    sum_total = np.sum(np.arange(256) * hist)
    sum_bg, w_bg = 0.0, 0
    max_var, threshold = 0.0, 0
    for t in range(256):
        w_bg += hist[t]
        if w_bg == 0:
            continue
        w_fg = total - w_bg
        if w_fg == 0:
            break
        sum_bg += t * hist[t]
        var = w_bg * w_fg * (sum_bg / w_bg - (sum_total - sum_bg) / w_fg) ** 2
        if var > max_var:
            max_var = var
            threshold = t
    return threshold


def _segment_characters(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Segment a grayscale image into character bounding boxes.

    Returns list of (x1, y1, x2, y2) bounding boxes, left to right.
    Filters out oversized segments (icons, labels) that can't be digits.
    """
    h, w = gray.shape
    thr = _otsu(gray)
    binary = (gray > thr).astype(np.uint8) * 255

    # Vertical projection
    proj = np.sum(binary > 0, axis=0)
    spans: list[tuple[int, int]] = []
    in_char = False
    start = 0
    for x in range(w + 1):
        val = proj[x] if x < w else 0
        if val > 0 and not in_char:
            in_char = True
            start = x
        elif val == 0 and in_char:
            in_char = False
            if x - start >= 3:
                spans.append((start, x))

    # Filter by aspect ratio — digits are roughly 0.4x–1.0x as wide as tall.
    # This rejects wide icons, horizontal bars, and letter runs.
    boxes: list[tuple[int, int, int, int]] = []
    for x1, x2 in spans:
        col = binary[:, x1:x2]
        ys = np.where(np.any(col > 0, axis=1))[0]
        if len(ys) < 3:
            continue
        y1, y2 = ys[0], ys[-1] + 1
        box_w = x2 - x1
        box_h = y2 - y1
        if box_h < 5:
            continue
        aspect = box_w / box_h
        # Digits: typically 0.3-1.1 wide-to-tall ratio. Reject wider things.
        if aspect > 1.3:
            continue
        boxes.append((x1, y1, x2, y2))

    return boxes


def _crop_to_28x28(gray: np.ndarray, box: tuple[int, int, int, int]) -> np.ndarray:
    """Crop a character, pad with white, resize to 28×28.

    Matches the ONNX model's expected input format exactly.
    """
    x1, y1, x2, y2 = box
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
    return np.array(pil, dtype=np.uint8)


def collect_training_sample(
    image: Image.Image,
    tesseract_result: int,
    confidence: str = "agreed",
) -> int:
    """Extract and save labeled character images from a scan.

    Returns the number of characters saved.

    Parameters
    ----------
    image : PIL.Image
        The raw captured scan region (RGB).
    tesseract_result : int
        The signal number read by Tesseract (e.g. 12345).
    confidence : str
        Label quality tier, controls which results get saved:
        - "agreed"    — both engines matched (highest quality)
        - "consensus" — app-level consensus (two consecutive scans agreed)
        - "vote"      — multiple Tesseract variants voted the same result
        - "single"    — single engine only (lowest quality, not saved)
    """
    global _session_count

    # Skip lowest-quality tier
    if confidence == "single":
        return 0

    if _session_count >= _MAX_PER_SESSION:
        return 0

    digits = str(tesseract_result)
    if len(digits) < 3 or len(digits) > 6:
        return 0  # implausible signal length

    try:
        _ensure_dirs()

        # Convert to grayscale and segment
        gray = np.array(image.convert("L"), dtype=np.uint8)
        boxes = _segment_characters(gray)

        # The number of segmented digit-shaped characters should match
        # the Tesseract result length. If we have MORE boxes, take the
        # rightmost N (signal numbers are right-aligned after icons).
        # If we have FEWER, reject — we can't label reliably.
        if len(boxes) > len(digits):
            boxes = boxes[-len(digits):]
        elif len(boxes) < len(digits):
            log.debug(
                "training: segment count %d < digit count %d for %s, skipping",
                len(boxes), len(digits), tesseract_result,
            )
            return 0

        ts = int(time.time() * 1000) % 1_000_000_000
        saved = 0

        for i, (box, digit) in enumerate(zip(boxes, digits)):
            char_img = _crop_to_28x28(gray, box)
            digit_dir = os.path.join(_TRAINING_DIR, digit)
            filename = f"{ts}_{confidence}_{i}.png"
            filepath = os.path.join(digit_dir, filename)
            Image.fromarray(char_img).save(filepath)
            saved += 1

        _session_count += saved
        if saved > 0:
            log.debug(
                "training: saved %d chars for '%s' (%s, session total: %d)",
                saved, digits, confidence, _session_count,
            )
        return saved

    except Exception as exc:
        log.debug("training: collection failed: %s", exc)
        return 0


def get_training_stats() -> dict[str, int]:
    """Return per-digit sample counts from the training directory."""
    stats: dict[str, int] = {}
    if not os.path.isdir(_TRAINING_DIR):
        return stats
    for digit in "0123456789":
        d = os.path.join(_TRAINING_DIR, digit)
        if os.path.isdir(d):
            stats[digit] = len([f for f in os.listdir(d) if f.endswith(".png")])
        else:
            stats[digit] = 0
    return stats


def get_total_samples() -> int:
    """Return total number of training samples collected."""
    return sum(get_training_stats().values())
