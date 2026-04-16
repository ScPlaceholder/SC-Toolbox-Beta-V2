"""ONNX-based mining HUD OCR — fast mass + resistance extraction.

Uses Mort13's trained CNN model (3KB graph + 1.7MB weights, 13 char classes,
100% validation accuracy) with a row-detection pipeline that's resolution-
independent (no anchor templates needed):

1. Capture the user's configured HUD region
2. Find text rows by horizontal brightness profiling
3. Identify MASS row (row 3) and RESISTANCE row (row 4) by position
4. Crop the right portion of each row (value only, skip label text)
5. Otsu binarize → projection segment → ONNX batch inference

Total pipeline: ~30-80ms per frame including screen capture.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

import numpy as np
from PIL import Image

from .screen_reader import capture_region, capture_region_averaged, _check_tesseract

log = logging.getLogger(__name__)

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
_MODEL_PATH = os.path.join(_MODULE_DIR, "models", "model_cnn.onnx")
_META_PATH = os.path.join(_MODULE_DIR, "models", "model_cnn.json")

# Online-learned model lives in %LOCALAPPDATA% so the shipped model
# in the app directory is never modified (safe across updates).
try:
    from .online_learner import ONLINE_MODEL_PATH as _ONLINE_MODEL_PATH
except ImportError:
    from pathlib import Path as _Path
    _ONLINE_MODEL_PATH = _Path(os.environ.get("LOCALAPPDATA", "")) / "SC_Toolbox" / "model_cnn_online.onnx"

# Thread-local side channel: _segment_and_infer stashes the 28×28
# uint8 digit crops here so the harvest trigger can read them without
# changing the function's return type.
import threading as _thr
_harvest_tls = _thr.local()

# Lazy-loaded
_session = None
_char_classes: str = "0123456789.-%"

# Label-row cache: maps (x, y, w, h) region key → (timestamp, rows).
# Tesseract label OCR is expensive (~3 subprocess spawns, ~500 ms)
# but labels don't move within a rock — cache and reuse. Cache is
# cleared when the panel disappears or after TTL expires.
_label_cache: dict[tuple[int, int, int, int], tuple[float, dict]] = {}
_LABEL_CACHE_TTL_SEC = 60.0  # safe upper bound; rocks scan for <60s

# Row detection thresholds
_MIN_ROW_HEIGHT = 14   # filters thin separator bars (~9px)
_MIN_VALUE_WIDTH = 15  # filters noise/artifacts in value crops
_TALL_ROW_HEIGHT = 35  # mineral name row is taller than data rows


# ─────────────────────────────────────────────────────────────
# Debug image throttling
# ─────────────────────────────────────────────────────────────
# Debug crops are useful for offline analysis when the OCR misreads,
# but the PIL encode + disk write adds ~80-120ms per scan. Throttle
# to every _DEBUG_SAVE_INTERVAL_S seconds so the hot path stays fast.
_DEBUG_SAVE_INTERVAL_S = 5.0
_last_debug_save_ts: float = 0.0


def _should_save_debug() -> bool:
    """Return True if enough time has elapsed since the last debug save.

    The timestamp is NOT updated here — call ``_mark_debug_saved()``
    once per full scan after all the debug artifacts are written.
    This ensures the raw capture, rows overlay, and individual crops
    are either all saved or all skipped for a given scan, so offline
    analysis always has a consistent snapshot.
    """
    return (time.time() - _last_debug_save_ts) >= _DEBUG_SAVE_INTERVAL_S


def _mark_debug_saved() -> None:
    """Reset the debug save timer; call after a full debug burst."""
    global _last_debug_save_ts
    _last_debug_save_ts = time.time()


def _debug_save_raw(img: Image.Image, filename: str) -> None:
    """Save a pristine copy of the HUD capture for offline re-analysis."""
    try:
        out_dir = os.path.dirname(_MODULE_DIR)
        img.save(os.path.join(out_dir, filename))
    except Exception as exc:
        log.debug("debug_save_raw failed (%s): %s", filename, exc)


def _debug_save_hud_rows(img: Image.Image, rows: list[tuple[int, int]]) -> None:
    """Save the full HUD capture with each detected row drawn as a box.

    Rows are numbered so the logs (and a human) can tell which row
    index was treated as MASS / RESISTANCE / INSTABILITY.
    """
    try:
        from PIL import ImageDraw
        out_dir = os.path.dirname(_MODULE_DIR)
        copy = img.copy().convert("RGB")
        draw = ImageDraw.Draw(copy)
        w = copy.width
        for idx, (y1, y2) in enumerate(rows):
            # Box
            draw.rectangle([(0, y1), (w - 1, y2 - 1)], outline=(0, 255, 0), width=1)
            # Row index label
            draw.text((2, max(0, y1 - 2)), f"r{idx}", fill=(0, 255, 0))
        copy.save(os.path.join(out_dir, "debug_live_hud_rows.png"))
    except Exception as exc:
        log.debug("debug_save_hud_rows failed: %s", exc)


def _debug_save_crop(crop_img: Image.Image, filename: str) -> None:
    """Save a crop + its binarized version to the tool dir for inspection.

    Writes two files per scan: the raw color crop and a max-of-channels
    binarized version (what the OCR engines actually see). Silent on
    failure — never breaks the scan loop.
    """
    try:
        # _MODULE_DIR is .../Mining_Signals/ocr — the debug pngs live one level up
        out_dir = os.path.dirname(_MODULE_DIR)
        # Raw upscaled crop (4x LANCZOS) so the user can actually see it
        w, h = crop_img.size
        if w > 0 and h > 0:
            big = crop_img.resize((w * 4, h * 4), Image.LANCZOS)
            big.save(os.path.join(out_dir, filename))

        # Binarized max-of-channels version (what Tesseract sees)
        rgb = np.array(crop_img.convert("RGB"), dtype=np.uint8)
        if rgb.ndim == 3 and rgb.size > 0:
            max_ch = rgb.max(axis=2).astype(np.uint8)
            scaled = Image.fromarray(max_ch).resize(
                (max_ch.shape[1] * 4, max_ch.shape[0] * 4), Image.LANCZOS,
            )
            arr = np.array(scaled, dtype=np.uint8)
            thr = _otsu(arr)
            binary = (arr > thr).astype(np.uint8) * 255
            bin_name = filename.replace(".png", "_bin.png")
            Image.fromarray(binary).save(os.path.join(out_dir, bin_name))
    except Exception as exc:
        log.debug("debug_save_crop failed (%s): %s", filename, exc)


def _ensure_model() -> bool:
    global _session, _char_classes
    if _session is not None:
        return True

    # Prefer online-learned model if it exists, else shipped model.
    model_path = (
        str(_ONLINE_MODEL_PATH)
        if _ONLINE_MODEL_PATH.is_file()
        else _MODEL_PATH
    )

    if not os.path.isfile(model_path):
        log.warning("onnx_hud_reader: model not found at %s", model_path)
        return False

    try:
        import onnxruntime as ort
    except ImportError:
        log.warning("onnx_hud_reader: onnxruntime not installed")
        return False

    try:
        import json
        if os.path.isfile(_META_PATH):
            with open(_META_PATH) as f:
                meta = json.load(f)
                _char_classes = meta.get("charClasses", _char_classes)

        _session = ort.InferenceSession(
            model_path, providers=["CPUExecutionProvider"],
        )
        log.info("onnx_hud_reader: model loaded from %s (%d classes)",
                 os.path.basename(model_path), len(_char_classes))
        return True
    except Exception as exc:
        log.error("onnx_hud_reader: model load failed: %s", exc)
        return False


def hot_swap_model(new_model_path: str) -> bool:
    """Replace the live ONNX inference session with a new model.

    Called by ``online_learner`` after re-exporting updated weights.
    Thread-safe: Python's GIL makes the pointer swap atomic.
    """
    global _session
    try:
        import onnxruntime as ort
        new_session = ort.InferenceSession(
            new_model_path, providers=["CPUExecutionProvider"],
        )
        old = _session
        _session = new_session
        del old
        log.info("onnx_hud_reader: hot-swapped model from %s",
                 os.path.basename(new_model_path))
        return True
    except Exception as exc:
        log.error("onnx_hud_reader: hot-swap failed: %s", exc)
        return False


def is_available() -> bool:
    return _ensure_model()


# ─────────────────────────────────────────────────────────────
# Row detection
# ─────────────────────────────────────────────────────────────

def _build_text_mask(gray: np.ndarray, deviation: int = 35) -> np.ndarray:
    """Return a boolean mask where True means "likely text pixel".

    Auto-detects polarity so it works on BOTH dark and light backgrounds:
    - Dark bg (median < 130): text is BRIGHT → gray > 150
    - Light bg (median >= 130): text is DARK → gray < (median - 30)

    This single fix enables the entire downstream pipeline
    (_find_mineral_row, _find_value_crop, column-density scanning)
    to work on light backgrounds without PaddleOCR.
    """
    del deviation  # kept for API compatibility
    median = float(np.median(gray))
    if median < 130:
        return gray > 150
    else:
        # Light background: text is darker than surroundings.
        # Use local contrast via high-pass filter for robust detection.
        from PIL import Image as _Img, ImageFilter
        blurred = np.asarray(
            _Img.fromarray(gray).filter(ImageFilter.GaussianBlur(radius=5)),
            dtype=np.float32,
        )
        local_contrast = np.abs(gray.astype(np.float32) - blurred)
        return local_contrast > 15


def _find_value_crop(
    img: Image.Image,
    gray: np.ndarray,
    y1: int,
    y2: int,
    x_min: int = 0,
) -> Optional[Image.Image]:
    """Crop the rightmost text cluster in a row — the numeric value.

    Uses a single, uniform strategy that works for both white/cyan
    labels+values AND red warning text:

      1. Build a per-column count of "definitely-text" pixels, where
         "definitely-text" means max(R,G,B) > 180. Cyan text peaks at
         ~243, red text peaks at ~250, white label at ~240. Blue HUD
         background peaks at ~120 and is safely below. The asteroid
         scene leaking through has sparse >180 pixels but they're
         scattered (1 per column).
      2. Require per-column density ≥ 3 to mark a column as "hot" —
         filters out scattered scene noise while keeping character
         strokes (which stack 3+ text pixels vertically).
      3. Scan hot columns right-to-left and build spans, merging
         with gap ≤ 12 (bridges inter-digit gaps but stays well
         below the ~70-100 px gap between label and value).
      4. Return the rightmost span that's at least _MIN_VALUE_WIDTH
         wide. If multiple spans are found, the rightmost one is
         always the value because the label is to the left.

    Rejects sub-pixel red fringing from cyan text anti-aliasing
    (which broke the previous "red-first" strategy) because the
    fringes are never dense enough to beat the cyan text itself.
    """
    # Polarity-independent text mask: pixels that deviate from the
    # local background (~51 px neighborhood mean) by more than 35
    # brightness units are text. Works on both bright-on-dark and
    # dark-on-bright panels. The mask is built on the FULL gray
    # image so the local-bg estimate uses real neighbor pixels on
    # all sides — cropping first would corrupt pixels near the row
    # boundary by shrinking their neighborhood.
    full_mask = _build_text_mask(gray, deviation=30)
    text_mask = full_mask[y1:y2, :]
    h, w = text_mask.shape

    col_text = np.sum(text_mask, axis=0)

    # Zero out columns to the left of x_min so label pixels can't
    # contaminate the value cluster. Simple clamp (no blanking of
    # the source image, which would corrupt the text mask).
    if x_min > 0:
        col_text[:x_min] = 0

    # Density gate: a column is "hot" (part of real text) only if at
    # least 3 pixels in that column are text-mask pixels. Filters
    # scattered scene noise while keeping character strokes.
    hot = col_text >= 3

    # Find hot spans scanning from the right
    spans: list[tuple[int, int]] = []  # (start_x, end_x), right-to-left
    in_span = False
    end = 0
    for x in range(w - 1, -1, -1):
        if hot[x] and not in_span:
            in_span = True
            end = x + 1
        elif not hot[x] and in_span:
            in_span = False
            spans.append((x + 1, end))
    if in_span:
        spans.append((0, end))

    if not spans:
        return None

    # Filter out tiny noise spans (< 3px) — single-pixel artifacts
    spans = [(s, e) for s, e in spans if e - s >= 3]
    if not spans:
        return None

    # Build merged clusters from right to left: merge spans with gaps ≤ 12px.
    # A gap > 12px starts a new cluster. Collect all clusters, then pick
    # the rightmost one that's wide enough. 12 comfortably bridges
    # inter-digit gaps (2-6px) while staying well below the 50-100px
    # gap between a label and its value.
    clusters: list[tuple[int, int]] = []  # (start, end)
    c_start = spans[0][0]
    c_end = spans[0][1]
    for s_start, s_end in spans[1:]:
        if c_start - s_end <= 12:
            c_start = s_start  # extend cluster leftward
        else:
            clusters.append((c_start, c_end))
            c_start, c_end = s_start, s_end
    clusters.append((c_start, c_end))

    # Pick the LEFTMOST qualifying cluster (i.e. the first cluster
    # after any x_min). clusters were built by scanning right-to-
    # left, so the leftmost cluster is the LAST entry in the list.
    # Values are always the first text cluster right after a label,
    # so we want the leftmost viable cluster — noise pixels further
    # right (asteroid scene bleeding through the right edge of the
    # panel) should be ignored.
    for c_start, c_end in reversed(clusters):
        if c_end - c_start >= _MIN_VALUE_WIDTH and c_start >= x_min:
            vx_start = max(0, c_start - 4)
            return img.crop((vx_start, y1, c_end, y2))

    return None


def _find_text_rows(channel: np.ndarray, min_height: int = 8) -> list[tuple[int, int]]:
    """Find contiguous horizontal bands containing text pixels.

    Uses the polarity-independent text mask (pixels that deviate from
    their row's median brightness) so this works on both dark-
    background HUDs (bright text) and light-background HUDs (dark
    text on sunlit panels).
    """
    text_mask = _build_text_mask(channel, deviation=35)
    row_counts = text_mask.sum(axis=1)
    h = len(row_counts)

    rows: list[tuple[int, int]] = []
    in_row = False
    start = 0
    for y in range(h + 1):
        val = row_counts[y] if y < h else 0
        if val > 3 and not in_row:
            in_row = True
            start = y
        elif val <= 3 and in_row:
            in_row = False
            if y - start >= min_height:
                rows.append((start, y))
    return rows


# ─────────────────────────────────────────────────────────────
# ONNX inference pipeline
# ─────────────────────────────────────────────────────────────

def _otsu(gray: np.ndarray) -> int:
    """Compute Otsu's optimal binarization threshold."""
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


def _segment_and_infer(gray: np.ndarray, binary: np.ndarray) -> list[tuple[str, float]]:
    """Segment characters from a binarized image and run ONNX inference.

    Returns list of (character, confidence) per detected glyph.
    """
    if _session is None:
        return []

    h, w = gray.shape

    # Vertical projection segmentation
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

    # Crop, pad, resize each character to 28×28
    char_images: list[np.ndarray] = []
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
        char_images.append(np.array(pil, dtype=np.float32) / 255.0)

    # Side-channel: stash the uint8 28×28 crops for the harvest
    # trigger. Overwrites on every call — caller reads immediately
    # after _ocr_crop returns, while still on the same thread.
    _harvest_tls.last_crops = [
        (c * 255.0).clip(0, 255).astype(np.uint8) for c in char_images
    ]

    if not char_images:
        return []

    # Batch inference
    batch = np.array(char_images, dtype=np.float32).reshape(-1, 1, 28, 28)
    inp_name = _session.get_inputs()[0].name
    logits = _session.run(None, {inp_name: batch})[0]

    results: list[tuple[str, float]] = []
    for i in range(len(char_images)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        idx = int(np.argmax(probs))
        results.append((_char_classes[idx], float(probs[idx])))
    return results


def _ocr_crop(crop_img: Image.Image) -> str:
    """Triple-engine OCR with auto polarity detection.

    Detects whether the crop is bright-on-dark or dark-on-bright
    (based on median grayscale — high median = light background =
    dark text) and inverts before thresholding if needed. All
    engines then see bright-text-on-dark-background, which is what
    the trained model expects.

    Engine A: Otsu threshold on (polarity-corrected) grayscale
    Engine B: Fixed threshold 140 on polarity-corrected grayscale
    Engine C: Otsu threshold on polarity-corrected max-of-channels
        (recovers colored warning text)

    Total cost: ~120ms (three ONNX batches).
    """
    if _session is None:
        return ""

    rgb = np.array(crop_img.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return ""
    gray = np.array(crop_img.convert("L"), dtype=np.uint8)
    # Max-of-channels: pixel is as bright as its brightest channel,
    # so saturated-red text stays at full brightness instead of
    # being dimmed to 30% by the luminance weights.
    max_ch = rgb.max(axis=2).astype(np.uint8)

    h, w = gray.shape
    if h < 4 or w < 4:
        return ""

    # Auto-detect polarity: if the median grayscale is >140, we're
    # looking at dark text on a light background (common when the
    # scene behind the translucent HUD is sunlit). Invert both gray
    # and max-of-channels so the rest of the pipeline sees the
    # standard bright-on-dark it was tuned for.
    if np.median(gray) > 140:
        gray = 255 - gray
        max_ch = 255 - max_ch

    # Engine A: Otsu threshold on grayscale (white/cyan text)
    thr_a = _otsu(gray)
    binary_a = (gray > thr_a).astype(np.uint8) * 255
    results_a = _segment_and_infer(gray, binary_a)

    # Engine C: Otsu threshold on max-of-channels (red text)
    thr_c = _otsu(max_ch)
    binary_c = (max_ch > thr_c).astype(np.uint8) * 255
    results_c = _segment_and_infer(max_ch, binary_c)

    # Engine B (fixed threshold 140 on grayscale) removed — its high
    # per-character confidence on wrong characters was poisoning the
    # vote on red text (e.g. "569" for "96" where it voted '5' at
    # position 0 with 0.46 conf over Engine A's '9' at 0.36).

    engines = [r for r in (results_a, results_c) if r]
    if not engines:
        return ""

    # Find the maximum length across engines. Short reads are almost
    # always wrong (segmentation fused two glyphs into one or missed
    # a glyph entirely), so filter to engines that hit the max length.
    max_len = max(len(r) for r in engines)
    max_len_engines = [r for r in engines if len(r) == max_len]

    # Among engines that found the most characters, vote per-position
    # by highest per-character confidence.
    if len(max_len_engines) > 1:
        text = ""
        for i in range(max_len):
            options = [e[i] for e in max_len_engines]
            ch, _ = max(options, key=lambda x: x[1])
            text += ch
        return text

    return "".join(ch for ch, _ in max_len_engines[0])


def _find_mineral_row(img: Image.Image) -> Optional[tuple[int, int]]:
    """Find the mineral-name row (e.g. 'TORITE (ORE)') via text mask.

    The mineral name is always the topmost wide text row after the
    'SCAN RESULTS' header. Returns (y1, y2) of its brightness band,
    or None if not found.

    Why not a label ('MASS:', 'RESIST:')? Tesseract's label OCR is
    unreliable on bright-background panels where the sunlit asteroid
    bleeds through and corrupts the local background estimate. The
    mineral-name row is visually distinctive regardless of polarity
    because it's a wide, dense text cluster unlike any other row in
    the top half of the panel.
    """
    gray = np.array(img.convert("L"), dtype=np.uint8)
    text_mask = _build_text_mask(gray, deviation=30)
    # Row counts
    row_counts = text_mask.sum(axis=1)
    h = len(row_counts)

    # Build row spans. Min height scales with panel size: at 541px
    # height the threshold is 14px (2.6%); at 130px it's ~8px. This
    # ensures small-panel HUDs (user's native 125x130 crop) don't
    # have their rows filtered out.
    min_row_h = max(6, min(14, int(h * 0.026)))
    rows: list[tuple[int, int, int]] = []  # (y1, y2, peak_count)
    in_row = False
    start = 0
    peak = 0
    for y in range(h + 1):
        val = row_counts[y] if y < h else 0
        if val > 3 and not in_row:
            in_row = True
            start = y
            peak = val
        elif val > 3 and in_row:
            peak = max(peak, val)
        elif val <= 3 and in_row:
            in_row = False
            if y - start >= min_row_h:
                rows.append((start, y, peak))

    if len(rows) < 2:
        return None

    # Typical panel layout after row-detection:
    #   - first wide/dense row (peak >= 60) = "SCAN RESULTS" header
    #   - next wide/dense row (peak >= 60)  = mineral name "TORITE (ORE)"
    #   - then MASS, RESISTANCE, INSTABILITY rows
    #
    # Find the first row matching the header signature and return
    # the NEXT qualifying row as the mineral name.
    # Peak threshold scales with panel width. At 397 px (test fixture),
    # the header peaks at ~117 = 29% of width. At 125 px (user's small
    # panel), the same text peaks at ~36 = 29% of width. Using a
    # proportional threshold handles all panel sizes.
    W = gray.shape[1]
    # "SCAN RESULTS" text width doesn't scale linearly with panel
    # width (same string, different font sizes). At 397px panel it
    # peaks at 117 (29%); at 332px it peaks at 43 (13%). Use 10%
    # as the floor to catch both.
    header_peak_min = max(15, int(W * 0.10))
    mineral_peak_min = max(10, int(W * 0.06))

    header_idx = None
    for i, (y1, y2, peak_cnt) in enumerate(rows):
        if peak_cnt >= header_peak_min and (y2 - y1) <= 40:
            header_idx = i
            break

    if header_idx is None:
        return None

    for y1, y2, peak_cnt in rows[header_idx + 1:]:
        if peak_cnt >= mineral_peak_min and (y2 - y1) <= 40:
            return (y1, y2)
    return None


def _find_label_rows(img: Image.Image) -> dict[str, tuple[int, int, int]]:
    """Find MASS / RESIST / INSTAB rows via Tesseract with polarity retries.

    Strategy:
      1. Crop the left 55% of the panel (labels live in this band).
      2. Try up to three binary renderings and keep the best result:
         (a) Otsu on grayscale as-is
         (b) Otsu on inverted grayscale  (for light-background panels)
         (c) Otsu on max-of-channels (for colored label text)
      3. Parse Tesseract word-level output for "mass", "resistance",
         "instability" substring matches.
      4. Return rows whose label y-span was detected, along with the
         label's right edge computed via column-density scan.
    """
    if not _check_tesseract():
        return {}
    try:
        import pytesseract
    except ImportError:
        return {}

    w_img, h_img = img.size
    left = img.crop((0, 0, int(w_img * 0.55), h_img))
    gray = np.array(left.convert("L"), dtype=np.uint8)
    rgb = np.array(left.convert("RGB"), dtype=np.uint8)
    max_ch = rgb.max(axis=2).astype(np.uint8)

    # Three candidate binaries. Text is ALWAYS BLACK in the output —
    # Tesseract is trained on printed-document style (dark ink on
    # white paper) and performs best with that polarity.
    thr_gray = _otsu(gray)
    thr_max = _otsu(max_ch)

    candidates = [
        # (a) Gray Otsu — bright-on-dark HUD: text is above thr, we
        # render above-thr as BLACK so text comes out black.
        ("gray_bright", np.where(gray > thr_gray, 0, 255).astype(np.uint8)),
        # (b) Gray Otsu inverted — dark-on-bright HUD: text is below
        # thr, render below-thr as BLACK.
        ("gray_dark",   np.where(gray < thr_gray, 0, 255).astype(np.uint8)),
        # (c) Max-of-channels Otsu — colored text (red RESISTANCE):
        ("max_bright",  np.where(max_ch > thr_max, 0, 255).astype(np.uint8)),
    ]

    targets = {
        "mass":        "mass",
        "resistance":  "resist",
        "instability": "instab",
    }

    best: dict[str, tuple[int, int, int, int]] = {}  # key -> (y1,y2,lbl_left,score)
    for _name, binary in candidates:
        binary_pil = Image.fromarray(binary)
        try:
            data = pytesseract.image_to_data(
                binary_pil,
                config=(
                    "--psm 11 -c tessedit_char_whitelist="
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:"
                ),
                output_type=pytesseract.Output.DICT,
            )
        except Exception:
            continue
        n = len(data.get("text", []))
        for i in range(n):
            text = (data["text"][i] or "").strip().lower()
            if len(text) < 4:
                continue
            x = int(data["left"][i])
            y = int(data["top"][i])
            h_ = int(data["height"][i])
            for key, needle in targets.items():
                if needle in text:
                    score = len(text)
                    prev = best.get(key)
                    if prev is None or score > prev[3]:
                        best[key] = (y, y + h_, x, score)
                    break

    if not best:
        return {}

    # Compute real label right edges via column-density on the
    # polarity-independent text mask of the full image. If the mask
    # is too noisy (e.g. asteroid leak), fall back to a fixed
    # right-edge estimate based on label length.
    full_gray = np.array(img.convert("L"), dtype=np.uint8)
    text_mask = _build_text_mask(full_gray, deviation=30)

    result: dict[str, tuple[int, int, int]] = {}
    _PAD = 3
    _GAP_THRESHOLD = 5
    # Fixed fallback right edges — from known panel geometry
    _FALLBACK_RIGHTS = {"mass": 110, "resistance": 200, "instability": 205}

    for key, (y1, y2, lbl_left, _score) in best.items():
        # Scan hot columns in this row to find the label right edge
        col_hot = text_mask[y1:y2, :].sum(axis=0) >= 2
        lbl_right = lbl_left
        gap_run = 0
        x = lbl_left
        while x < col_hot.shape[0]:
            if col_hot[x]:
                lbl_right = x + 1
                gap_run = 0
            else:
                gap_run += 1
                if gap_run >= _GAP_THRESHOLD:
                    break
            x += 1

        # Sanity check — on dark panels the column-density scan is
        # unreliable when the text mask shape varies, and on light
        # panels it's corrupted by asteroid bleed. Always use the
        # fixed right-edge values measured from the panel geometry.
        # (Row spacing and label widths are constant across scans.)
        lbl_right = _FALLBACK_RIGHTS[key]

        result[key] = (
            max(0, y1 - _PAD),
            min(img.height, y2 + _PAD),
            lbl_right,
        )
    return result


def _find_label_rows_v1_disabled(img: Image.Image) -> dict[str, tuple[int, int, int]]:
    """[DISABLED] Find MASS / RESIST / INSTAB rows via Tesseract on labels.

    Crops the left ~55% of the HUD (where all labels live) and runs
    Tesseract in sparse-text mode with a letters+colon whitelist.
    Returns a dict like::

        {"mass": (y1, y2, label_right_x), ...}

    where ``label_right_x`` is the x-coordinate of the right edge of
    the matched label text. The value extractor crops the row to
    everything right of this x, eliminating the label+value fusion
    problem that plagued the old content-based walker.

    Any missing label is omitted. The y-ranges come from Tesseract's
    bounding-box output, padded by 3 px so the value row-cropper has
    breathing room for glyph ascenders/descenders.

    This replaces the content-based walker that tried to OCR every
    row's value crop to identify which was mass/resistance/instability.
    Labels are high-contrast white text in a predictable column and
    Tesseract reads them reliably in one shot.
    """
    if not _check_tesseract():
        return {}
    try:
        import pytesseract
    except ImportError:
        return {}

    try:
        w, h = img.size
        # Label column lives in the left ~55% of the panel; value
        # columns start around 45-50% width. Crop wider than needed
        # so we don't clip "INSTABILITY" (longest label).
        left = img.crop((0, 0, int(w * 0.55), h))

        # Feed Tesseract a CLEAN BINARY from the polarity-
        # independent text mask. Tesseract was trained on printed
        # documents (dark text on white paper), so we render
        # text pixels as BLACK and the background as WHITE — NOT
        # the other way around. Works identically on both dark-
        # and light-background HUDs because the text mask is
        # polarity-independent.
        left_gray = np.array(left.convert("L"), dtype=np.uint8)
        text_mask = _build_text_mask(left_gray, deviation=30)
        # Black text (0) where mask is True, white bg (255) elsewhere
        binary_label = np.where(text_mask, 0, 255).astype(np.uint8)
        left_pil = Image.fromarray(binary_label)

        # PSM 11 = sparse text, finds scattered label glyphs without
        # assuming line structure. Letters + colon whitelist keeps
        # Tesseract from hallucinating numbers into the labels.
        data = pytesseract.image_to_data(
            left_pil,
            config=(
                "--psm 11 -c tessedit_char_whitelist="
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:"
            ),
            output_type=pytesseract.Output.DICT,
        )
    except Exception as exc:
        log.debug("onnx_hud_reader: label OCR failed: %s", exc)
        return {}

    # Label keywords — match on FULL substring, not just 3-char
    # prefix. "RESUL" from "SCAN RESULTS" previously stole the
    # "resistance" slot because both start with "res". Require at
    # least 5 characters of the full word to be present.
    targets = {
        "mass":        ("mass",),
        "resistance":  ("resist",),  # "resistance" → "resist" prefix
        "instability": ("instab",),  # "instability" → "instab" prefix
    }

    # found[key] = (y1, y2, label_left_x, score)
    # We use the label's LEFT edge + a per-row column-density scan
    # below to locate where the label ends, because Tesseract's
    # word bbox often absorbs the value text into the label bbox
    # when they're close together.
    found: dict[str, tuple[int, int, int, int]] = {}
    n = len(data.get("text", []))
    for i in range(n):
        text = (data["text"][i] or "").strip().lower()
        if len(text) < 4:
            continue
        # Tesseract reports conf=0 on red text, so don't filter by
        # conf — we rely on the substring match plus a word-length
        # tiebreaker to pick the best candidate per label.
        x = int(data["left"][i])
        y = int(data["top"][i])
        h_ = int(data["height"][i])
        for key, needles in targets.items():
            if any(needle in text for needle in needles):
                # Score by text length — longer matches are more
                # reliable ("resistance" beats "res" substring hit).
                score = len(text)
                prev = found.get(key)
                if prev is None or score > prev[3]:
                    found[key] = (y, y + h_, x, score)
                break

    if not found:
        return {}

    # Compute the actual right edge of each label via column-density
    # scanning. Tesseract's word bbox frequently stretches to absorb
    # the value text (e.g. RESISTANCE bbox reaches into "25%"), so we
    # can't use it directly. Instead, starting at the label's left
    # edge, walk rightward on the row's brightness profile and find
    # the first sustained gap of low-density columns — that's where
    # the label text ends and the value text begins.
    #
    # Works because all HUD labels are uppercase letters with narrow
    # inter-letter gaps (≤ 2 px), while the gap between ":" and the
    # value is ≥ 6 px.
    full_gray = np.array(img.convert("L"), dtype=np.uint8)
    # Polarity-independent text mask — works on both light and dark
    # backgrounds (pixels that deviate from row median are text).
    full_text_mask = _build_text_mask(full_gray, deviation=35)

    result: dict[str, tuple[int, int, int]] = {}
    _PAD = 3
    _GAP_THRESHOLD = 5  # consecutive low-density columns that mark end of label
    for key, (y1, y2, lbl_left, _score) in found.items():
        # Restrict the column-density scan to the label's y-band
        col_hot = full_text_mask[y1:y2, :].sum(axis=0) >= 2

        # Scan right from the label's left edge. First, skip over any
        # initial bright columns (the label glyphs themselves), then
        # count consecutive cold columns until we hit _GAP_THRESHOLD.
        lbl_right = lbl_left
        x = lbl_left
        # Walk through label glyphs
        in_label = True
        gap_run = 0
        while x < full_text_mask.shape[1]:
            if in_label:
                if col_hot[x]:
                    lbl_right = x + 1
                    gap_run = 0
                else:
                    gap_run += 1
                    if gap_run >= _GAP_THRESHOLD:
                        # End of label
                        break
            x += 1

        result[key] = (
            max(0, y1 - _PAD),
            min(img.height, y2 + _PAD),
            lbl_right,
        )
    return result


def _ocr_crop_fast(crop_img: Image.Image) -> str:
    """Fast single-engine OCR for exploration.

    Used to classify rows during the MASS/RESIST/INSTAB discovery
    walk. The result only needs to be "good enough to parse as some
    number" — we don't trust this output for the final display, we
    just use it to pick which rows to re-OCR with the full
    cross-validation pipeline.

    Runs ONE ONNX engine on max-of-channels (which handles both
    white and red HUD text) instead of the three-engine vote used
    by ``_ocr_crop``. Typical cost: ~30-40ms vs ~120ms.

    Auto-detects polarity and inverts dark-on-light crops so the
    trained model always sees bright-text-on-dark.
    """
    if _session is None:
        return ""

    rgb = np.array(crop_img.convert("RGB"), dtype=np.uint8)
    if rgb.size == 0:
        return ""
    # Max-of-channels preserves saturated red text
    max_ch = rgb.max(axis=2).astype(np.uint8)
    h, w = max_ch.shape
    if h < 4 or w < 4:
        return ""

    # Auto-invert when the background is brighter than the text
    if np.median(max_ch) > 140:
        max_ch = 255 - max_ch

    thr = _otsu(max_ch)
    binary = (max_ch > thr).astype(np.uint8) * 255
    results = _segment_and_infer(max_ch, binary)
    if not results:
        return ""
    return "".join(ch for ch, _ in results)


def _tesseract_crop(crop_img: Image.Image, whitelist: str = "0123456789.") -> str:
    """Independent OCR using Tesseract — a second set of eyes.

    Runs in parallel with the ONNX model to cross-validate difficult
    reads (e.g. red-text resistance where ONNX's tiny char classifier
    collapses the top loop of '9' into '8'). Preprocesses the crop
    with max-of-channels + 4x upscale + Otsu so Tesseract sees the
    same bright binary digits regardless of the original text color.

    Returns the raw recognized text, or "" if Tesseract isn't
    available or the call fails.
    """
    if not _check_tesseract():
        return ""
    try:
        import pytesseract
    except ImportError:
        return ""

    try:
        rgb = np.array(crop_img.convert("RGB"), dtype=np.uint8)
        if rgb.size == 0:
            return ""
        # Max-of-channels preserves saturated colors
        max_ch = rgb.max(axis=2).astype(np.uint8)

        h, w = max_ch.shape
        if h < 4 or w < 4:
            return ""

        # Auto-invert dark-on-light crops so Tesseract sees bright
        # text on a dark background (which it's more accurate on).
        if np.median(max_ch) > 140:
            max_ch = 255 - max_ch

        # 4x upscale with LANCZOS before thresholding — Tesseract
        # prefers larger, smoother glyphs
        scaled = Image.fromarray(max_ch).resize(
            (w * 4, h * 4), Image.LANCZOS,
        )
        arr = np.array(scaled, dtype=np.uint8)
        thr = _otsu(arr)
        binary = (arr > thr).astype(np.uint8) * 255
        # CRITICAL: flip polarity so Tesseract sees BLACK text on a
        # WHITE background — the shape it was trained on. Without
        # this flip, PSM 6/7/8 modes return empty because their
        # document-type classifier rejects white-on-black input;
        # only PSM 13 (raw line) accepts it and we don't want to
        # depend on one PSM working. Empirically verified on the
        # "382.36" instability crop: pre-flip returns '' on PSM 6/7,
        # post-flip returns '382.36' on every PSM mode.
        binary = 255 - binary
        binary_pil = Image.fromarray(binary)

        # Single PSM 7 (single line) — previously we tried PSM 7/8/6/13
        # and kept the longest, but that's 4 subprocess spawns per
        # field × 3 fields = 12 spawns per scan, each ~50-100 ms on
        # Windows. With 1 Hz scanning that burned an entire CPU core
        # and caused game stuttering on users' machines. PSM 7 alone
        # works on 95%+ of crops; the edge cases fall back to ONNX or
        # Paddle in the 3-way reconciler.
        cfg = f"--psm 7 -c tessedit_char_whitelist={whitelist}"
        try:
            return pytesseract.image_to_string(
                binary_pil, config=cfg,
            ).strip()
        except Exception:
            return ""
    except Exception as exc:
        log.debug("onnx_hud_reader: Tesseract read failed: %s", exc)
        return ""


def _majority_match(
    vals: list[tuple[str, Optional[float]]],
    rel_tol: float,
    abs_tol: float = 1.0,
) -> Optional[tuple[float, list[str]]]:
    """Find a majority (>=2) of engines agreeing on a value.

    ``vals`` is a list of ``(engine_name, value)`` pairs. Returns
    ``(avg_of_agreeing, [engine_names])`` when two or more engines
    produced values within ``rel_tol`` of each other (also using
    ``abs_tol`` as a floor for small-number comparisons). Returns
    ``None`` if no majority exists.
    """
    present = [(n, v) for n, v in vals if v is not None]
    if len(present) < 2:
        return None
    # Try every pair; if three engines agree we'll pick them all up
    # on the first matching pair and then extend with the third.
    best: Optional[tuple[float, list[str]]] = None
    for i in range(len(present)):
        n_i, v_i = present[i]
        cluster_vals = [v_i]
        cluster_names = [n_i]
        for j in range(len(present)):
            if j == i:
                continue
            n_j, v_j = present[j]
            if abs(v_i - v_j) <= max(abs_tol, max(abs(v_i), abs(v_j)) * rel_tol):
                cluster_vals.append(v_j)
                cluster_names.append(n_j)
        if len(cluster_vals) >= 2:
            avg = sum(cluster_vals) / len(cluster_vals)
            if best is None or len(cluster_vals) > len(best[1]):
                best = (avg, cluster_names)
    return best


def _reconcile_mass(
    onnx_val: Optional[float],
    tess_val: Optional[float],
    onnx_raw: str,
    tess_raw: str,
    paddle_val: Optional[float] = None,
    paddle_raw: str = "",
) -> Optional[float]:
    """Combine ONNX, Tesseract and Paddle mass reads.

    Mass is an integer in the low-thousands to low-millions range.
    Tesseract is generally more reliable than the tiny ONNX CNN on
    5-6 digit sequences because its LSTM has wider context. ONNX
    has been observed dropping middle digits (e.g. 12748 → 1298)
    which is catastrophic for mass — it turns a 12,748 kg rock into
    a 1,298 kg rock, off by an order of magnitude. Paddle acts as
    a tie-breaking third voter: with three engines any single-
    engine failure is outvoted.

    Voting policy (in order):
      1. All None → None.
      2. Only one parsed → use it.
      3. Two or three engines agree within 1% → average of agreeing.
      4. Fall back to two-engine ONNX/Tesseract digit-length rule.
    """
    # Collect all available reads
    reads = [("onnx", onnx_val), ("tess", tess_val), ("paddle", paddle_val)]
    present = [(n, v) for n, v in reads if v is not None]
    if not present:
        return None
    if len(present) == 1:
        name, val = present[0]
        log.debug("mass: only %s parsed → %s", name, val)
        return val

    # Three-way majority check
    majority = _majority_match(reads, rel_tol=0.01)
    if majority is not None:
        avg, names = majority
        if len(names) >= 2 and len([v for _, v in present]) >= 2:
            log.debug(
                "mass: majority %s → %.2f (onnx=%r tess=%r paddle=%r)",
                "+".join(names), avg, onnx_raw, tess_raw, paddle_raw,
            )
            return avg

    # No majority — fall back to the legacy two-engine digit-length
    # rule between ONNX and Tesseract. Paddle is reported in the log
    # but doesn't participate in the tiebreak since it was the
    # outlier (no engine agreed with it).
    if onnx_val is not None and tess_val is not None:
        onnx_digits = len(str(int(onnx_val)))
        tess_digits = len(str(int(tess_val)))
        if tess_digits > onnx_digits:
            log.info(
                "mass: 3-way split — ONNX=%s (%r, %d) Tesseract=%s (%r, %d) Paddle=%s (%r), picking Tesseract (longer)",
                onnx_val, onnx_raw, onnx_digits, tess_val, tess_raw, tess_digits, paddle_val, paddle_raw,
            )
            return tess_val
        if onnx_digits > tess_digits:
            log.info(
                "mass: 3-way split — ONNX=%s (%r, %d) Tesseract=%s (%r, %d) Paddle=%s (%r), picking ONNX (longer)",
                onnx_val, onnx_raw, onnx_digits, tess_val, tess_raw, tess_digits, paddle_val, paddle_raw,
            )
            return onnx_val
        # Same length — prefer ONNX (known-good on dark high-contrast)
        log.info(
            "mass: 3-way split — ONNX=%s (%r) Tesseract=%s (%r) Paddle=%s (%r), same length, picking ONNX",
            onnx_val, onnx_raw, tess_val, tess_raw, paddle_val, paddle_raw,
        )
        return onnx_val

    # Only ONNX+Paddle or Tesseract+Paddle exist and they disagree.
    # Pick the LONGER read — same dropped-digit reasoning.
    a_name, a_val = present[0]
    b_name, b_val = present[1]
    a_digits = len(str(int(a_val)))
    b_digits = len(str(int(b_val)))
    if a_digits >= b_digits:
        log.info(
            "mass: disagree %s=%s (%d) vs %s=%s (%d), picking %s",
            a_name, a_val, a_digits, b_name, b_val, b_digits, a_name,
        )
        return a_val
    log.info(
        "mass: disagree %s=%s (%d) vs %s=%s (%d), picking %s",
        a_name, a_val, a_digits, b_name, b_val, b_digits, b_name,
    )
    return b_val


def _reconcile_resistance(
    onnx_val: Optional[float],
    tess_val: Optional[float],
    onnx_raw: str,
    tess_raw: str,
    paddle_val: Optional[float] = None,
    paddle_raw: str = "",
) -> Optional[float]:
    """Combine ONNX, Tesseract and Paddle resistance reads.

    Voting policy:
      1. All None → None.
      2. Only one engine parsed → use it.
      3. Two or three engines agree within 1% → average of agreeing.
      4. No majority → prefer Tesseract (known-good on colored digits),
         Paddle second, ONNX last (red-text 9→8 misread is its known
         failure mode).
    """
    reads = [("onnx", onnx_val), ("tess", tess_val), ("paddle", paddle_val)]
    present = [(n, v) for n, v in reads if v is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0][1]

    majority = _majority_match(reads, rel_tol=0.01)
    if majority is not None:
        avg, names = majority
        log.debug(
            "resistance: majority %s → %.2f (onnx=%r tess=%r paddle=%r)",
            "+".join(names), avg, onnx_raw, tess_raw, paddle_raw,
        )
        return avg

    # No majority — prefer in order: Tesseract → Paddle → ONNX
    log.info(
        "resistance: 3-way split — ONNX=%s (%r) Tesseract=%s (%r) Paddle=%s (%r)",
        onnx_val, onnx_raw, tess_val, tess_raw, paddle_val, paddle_raw,
    )
    if tess_val is not None:
        return tess_val
    if paddle_val is not None:
        return paddle_val
    return onnx_val


def _reconcile_instability(
    onnx_val: Optional[float],
    tess_val: Optional[float],
    onnx_raw: str,
    tess_raw: str,
    paddle_val: Optional[float] = None,
    paddle_raw: str = "",
) -> Optional[float]:
    """Combine ONNX, Tesseract and Paddle instability reads.

    Tesseract and Paddle are the reliable engines for instability
    because the value almost always contains a decimal (e.g. 731.84)
    and ONNX's tiny CNN frequently drops or misclassifies the '.',
    producing garbage integers like 73104 for 731.84.

    Voting policy:
      1. All None → None.
      2. Only one parsed → use it.
      3. Two or three engines agree within 5% → average of agreeing.
      4. No majority → prefer reads WITH a decimal dot, then Tesseract
         (known-good decimal handling), then Paddle, then ONNX.
    """
    reads = [("onnx", onnx_val), ("tess", tess_val), ("paddle", paddle_val)]
    present = [(n, v) for n, v in reads if v is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0][1]

    # Three-way majority (generous 5% tolerance)
    majority = _majority_match(reads, rel_tol=0.05)
    if majority is not None:
        avg, names = majority
        log.debug(
            "instability: majority %s → %.2f (onnx=%r tess=%r paddle=%r)",
            "+".join(names), avg, onnx_raw, tess_raw, paddle_raw,
        )
        return avg

    # Decimal-shape heuristic: reads that contain '.' are much more
    # likely correct on this field. Filter to dot-bearing reads first.
    dot_reads = [
        (n, v, r) for (n, v), r in zip(
            reads, (onnx_raw, tess_raw, paddle_raw),
        ) if v is not None and "." in r
    ]
    if dot_reads:
        # Prefer tess > paddle > onnx among dot-bearing reads
        order = {"tess": 0, "paddle": 1, "onnx": 2}
        dot_reads.sort(key=lambda x: order[x[0]])
        log.info(
            "instability: 3-way split, picking dot-bearing %s=%s — onnx=%r tess=%r paddle=%r",
            dot_reads[0][0], dot_reads[0][1], onnx_raw, tess_raw, paddle_raw,
        )
        return dot_reads[0][1]

    # No dots anywhere. Filter out suspiciously tiny values if we have
    # larger normal-range alternatives.
    normals = [(n, v) for n, v in present if v >= 1.0]
    pool = normals if normals else present
    order = {"tess": 0, "paddle": 1, "onnx": 2}
    pool.sort(key=lambda x: order[x[0]])
    log.info(
        "instability: 3-way split, picking %s=%s — onnx=%r tess=%r paddle=%r",
        pool[0][0], pool[0][1], onnx_raw, tess_raw, paddle_raw,
    )
    return pool[0][1]


# Mass cap. Large asteroids can exceed a million kg — the previous
# 100,000 cap was silently rejecting legit high-mass rocks and letting
# the last-known-good value persist stale in the break bubble.
MAX_MASS_VALUE = 10_000_000.0


def _parse_mass(raw: str) -> Optional[float]:
    """Extract mass value from raw OCR text (e.g. '5927' → 5927.0)."""
    nums = re.findall(r"[\d.]+", raw)
    if not nums:
        return None
    best = max(nums, key=len)
    try:
        val = float(best)
        return val if 0.1 <= val <= MAX_MASS_VALUE else None
    except ValueError:
        return None


def _parse_instability(raw: str) -> Optional[float]:
    """Extract instability value from raw OCR text.

    Instability can include a decimal (e.g. '731.84'). Bounds are
    generous since the scale varies per rock type.
    """
    # Keep digits and dots only
    cleaned = re.sub(r"[^\d.]", "", raw)
    if not cleaned:
        return None
    # Collapse accidental double-dots
    cleaned = re.sub(r"\.+", ".", cleaned).strip(".")
    if not cleaned:
        return None
    try:
        val = float(cleaned)
        return val if 0 <= val <= 100000 else None
    except ValueError:
        return None


def _parse_resistance(raw: str) -> Optional[float]:
    """Extract resistance % from raw OCR text.

    Handles the common case where the '%' symbol is misread as a digit
    (e.g. '586' where the '6' is actually '%', so the real value is 58).
    Tries the full string first, then progressively shorter prefixes,
    since resistance is always 0-100.
    """
    # Strip recognized % and - characters
    cleaned = re.sub(r"[%\-]", "", raw)
    digits = re.sub(r"[^\d.]", "", cleaned)
    if not digits:
        return None

    # Try full string, then drop one char from the right, etc.
    for end in range(len(digits), 0, -1):
        substr = digits[:end]
        try:
            val = float(substr)
            if 0 <= val <= 100:
                return val
        except ValueError:
            continue
    return None


# ─────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────
# Light-background pipeline (PaddleOCR via sidecar)
# ─────────────────────────────────────────────────────────────

# Module-level executor used by _scan_light to time-box Paddle calls.
# See the same pattern in screen_reader._get_extract_pool for the
# rationale — a `with ThreadPoolExecutor() as` block would hang the
# caller on shutdown(wait=True) even if we already got the result or
# hit a timeout.
_light_pool = None


def _get_light_pool():
    global _light_pool
    if _light_pool is None:
        from concurrent.futures import ThreadPoolExecutor
        # 3 workers: one for the synchronous light-path call, one
        # for the fire-and-forget dark-path background Paddle pass,
        # plus a spare so a queued bg call can't starve a light
        # scan that comes in while the bg worker is still in flight.
        _light_pool = ThreadPoolExecutor(
            max_workers=3, thread_name_prefix="scan_light"
        )
    return _light_pool

# Fixed y-bands per field, measured from known panel geometry.
# The scan panel is always 397×541 at 1080p; text rows never move.
# Bands are widened by ~5 px on each side compared to the raw text
# extents because PaddleOCR reports the y-midpoint of the bbox and
# the bbox center can drift slightly from the visual center based on
# glyph ascenders/descenders. Wider bands cost nothing (adjacent
# rows are ~40 px apart so there's no overlap) and catch every
# observed value position across our sample set.
_LIGHT_ROW_BANDS = {
    "mass":        (115, 160),
    "resistance":  (155, 195),
    "instability": (195, 235),
}


def _scan_light(img: Image.Image) -> Optional[dict[str, Optional[float]]]:
    """Light-background scan via PaddleOCR sidecar.

    Returns the same dict shape as ``scan_hud_onnx`` on success, or
    None if the sidecar is unavailable or produced no parseable
    result (caller should fall through to the dark pipeline or
    return empty).

    Uses fixed row y-bands since panel geometry is constant across
    captures. Per sample, PaddleOCR finds 15-22 text regions; we
    filter to the three we care about by y-band membership.
    """
    try:
        from . import paddle_client
    except Exception as exc:
        log.debug("paddle_client import failed: %s", exc)
        return None

    if not paddle_client.is_available():
        return None

    # Bound the wait on Paddle — the first call per session takes
    # ~15-20 s for daemon spawn + model load. The main scan loop in
    # app.py puts a 5 s timeout on the HUD future, so if we block
    # longer than that here, the whole scan thread crashes with
    # TimeoutError and the break bubble ends up showing stale
    # values. Short inner timeout → return None → caller falls
    # through to the dark pipeline (which returns panel_visible=
    # False for light panels, hiding the bubble — graceful).
    import concurrent.futures as _cf
    pool = _get_light_pool()
    fut = pool.submit(paddle_client.recognize, img)
    try:
        # Paddle warm inference on CPU is ~9 s per call. The old 4 s
        # timeout here caused _scan_light to ALWAYS time out, silently
        # disabling the light pipeline. 12 s covers a warm call with
        # headroom, and the app.py hud_future timeout is raised to
        # match.  Cold start (~25 s) still times out here — that's
        # acceptable because the daemon persists and subsequent scans
        # will be warm.
        regions = fut.result(timeout=12.0)
    except _cf.TimeoutError:
        log.debug("_scan_light: Paddle cold-start in progress, skipping this scan")
        return None
    except Exception as exc:
        log.debug("_scan_light: Paddle call failed: %s", exc)
        return None

    if regions is None:
        return None

    result: dict[str, Optional[float]] = {
        "mass": None,
        "resistance": None,
        "instability": None,
        "panel_visible": False,
    }

    # Check panel_visible: PaddleOCR finds "SCAN RESULTS" at the top
    # of the panel when it's on screen. On some samples paddle splits
    # that into two separate regions ("SCAN" and "RESULTS"), so we
    # accept either form — look for the two words anywhere in the
    # region list.
    found_scan = False
    found_results = False
    for r in regions:
        t = r["text"].lower()
        if "scan" in t:
            found_scan = True
        if "result" in t:
            found_results = True
    panel_visible = found_scan and found_results
    result["panel_visible"] = panel_visible

    if not panel_visible:
        return result

    def _pick_value(y1: int, y2: int) -> Optional[str]:
        """Pick the value text in a y-band, skipping label text."""
        best: Optional[tuple[float, str]] = None
        for r in regions:
            y = r.get("y_mid", -1)
            if not (y1 <= y <= y2):
                continue
            text = r["text"].strip()
            # Reject label-only reads ("MASS:", "RESISTANCE:", "INSTABILITY:")
            # by requiring at least one digit.
            if not any(ch.isdigit() for ch in text):
                continue
            conf = float(r.get("conf", 0))
            if best is None or conf > best[0]:
                best = (conf, text)
        return best[1] if best else None

    mass_text = _pick_value(*_LIGHT_ROW_BANDS["mass"])
    res_text = _pick_value(*_LIGHT_ROW_BANDS["resistance"])
    inst_text = _pick_value(*_LIGHT_ROW_BANDS["instability"])

    if mass_text:
        result["mass"] = _parse_mass(mass_text)
    if res_text:
        result["resistance"] = _parse_resistance(res_text)
    if inst_text:
        result["instability"] = _parse_instability(inst_text)

    return result


# ─────────────────────────────────────────────────────────────
# Dark-path Paddle: ASYNC third voter
# ─────────────────────────────────────────────────────────────
# Paddle on CPU takes ~9 seconds per warm inference — far longer
# than the 5 second scan budget set in app.py (hud_future.result
# timeout=5). We can't wait for Paddle synchronously without
# crashing the scan thread.
#
# Instead we run it fire-and-forget in a background executor and
# cache the most recent successful result. The next scan reads the
# cache and uses whatever Paddle produced on the previous frame as
# a third voter. This works because the break bubble stays up for
# many seconds on the same rock — as long as the player is hovering
# on the rock, the cached Paddle text matches the current HUD text,
# and the 3-way reconciler catches ONNX digit-drops that the 2-way
# rule might miss.
#
# Cache is invalidated automatically when the panel disappears
# (scan_hud_onnx clears it on panel_visible=False).

import threading as _thr_mod

# Mutex-protected cache state
_paddle_cache_lock = _thr_mod.Lock()
_paddle_cache: dict[str, tuple[Optional[float], str]] = {
    "mass": (None, ""),
    "resistance": (None, ""),
    "instability": (None, ""),
}
_paddle_cache_t: float = 0.0   # monotonic timestamp of last successful result
_paddle_bg_fut = None           # currently-running future, or None
_PADDLE_CACHE_MAX_AGE = 30.0    # drop cache older than this many seconds
_PADDLE_MIN_INTERVAL_SEC = 15.0  # minimum gap between successful Paddle dispatches


def _paddle_cache_get() -> dict[str, tuple[Optional[float], str]]:
    """Return the cached Paddle result if it is fresh, else empty."""
    empty = {
        "mass": (None, ""),
        "resistance": (None, ""),
        "instability": (None, ""),
    }
    with _paddle_cache_lock:
        age = time.monotonic() - _paddle_cache_t
        if _paddle_cache_t == 0.0 or age > _PADDLE_CACHE_MAX_AGE:
            return empty
        # Return a copy so callers can't mutate the cache
        return dict(_paddle_cache)


def _paddle_cache_clear() -> None:
    """Called when the HUD panel disappears — drop stale values."""
    global _paddle_cache_t, _paddle_cache
    with _paddle_cache_lock:
        _paddle_cache_t = 0.0
        _paddle_cache = {
            "mass": (None, ""),
            "resistance": (None, ""),
            "instability": (None, ""),
        }


def _paddle_dispatch_bg(
    img: Image.Image,
    mass_row: Optional[tuple[int, int]],
    res_row: Optional[tuple[int, int]],
    inst_row: Optional[tuple[int, int]],
) -> None:
    """Kick off a background Paddle call if one is not already running.

    Does not block. The worker writes results into ``_paddle_cache``
    when the call completes. Only one call is ever in flight at a
    time — if the previous call is still running, this call is a
    no-op (saves CPU and avoids starving the scan thread).
    """
    global _paddle_bg_fut

    try:
        from . import paddle_client
    except Exception:
        return
    if not paddle_client.is_available():
        return

    # Rate-limit dispatch. Paddle takes ~9 s per warm inference and
    # uses multi-threaded OpenBLAS/MKL — running back-to-back at the
    # 1 Hz scan rate was pegging user CPUs at 90%+ across all cores.
    # We only need Paddle as a tie-breaking third voter when the
    # cache is empty or very stale. Limit to one call per
    # _PADDLE_MIN_INTERVAL_SEC seconds and skip when the cache is
    # still fresh.
    with _paddle_cache_lock:
        if _paddle_bg_fut is not None and not _paddle_bg_fut.done():
            return
        # Skip if the last successful result is still fresh enough
        # to serve as a third voter — no need to redo work.
        if (_paddle_cache_t > 0.0
                and (time.monotonic() - _paddle_cache_t) < _PADDLE_MIN_INTERVAL_SEC):
            return

    # Snapshot the row coordinates so the worker doesn't race with
    # the main thread's closure variables.
    snap_img = img.copy()
    snap_mass = mass_row
    snap_res = res_row
    snap_inst = inst_row

    def _worker():
        global _paddle_cache_t, _paddle_cache
        try:
            regions = paddle_client.recognize(snap_img)
        except Exception as exc:
            log.debug("paddle bg: recognize failed: %s", exc)
            return
        if not regions:
            return

        def _pick(band):
            if band is None:
                return ""
            y1, y2 = band
            best = None
            for r in regions:
                y = r.get("y_mid", -1)
                if not (y1 <= y <= y2):
                    continue
                text = r["text"].strip()
                if not any(ch.isdigit() for ch in text):
                    continue
                conf = float(r.get("conf", 0))
                if best is None or conf > best[0]:
                    best = (conf, text)
            return best[1] if best else ""

        mass_raw = _pick(snap_mass)
        res_raw = _pick(snap_res)
        inst_raw = _pick(snap_inst)

        new_cache = {
            "mass": (_parse_mass(mass_raw) if mass_raw else None, mass_raw),
            "resistance": (_parse_resistance(res_raw) if res_raw else None, res_raw),
            "instability": (_parse_instability(inst_raw) if inst_raw else None, inst_raw),
        }
        with _paddle_cache_lock:
            _paddle_cache = new_cache
            _paddle_cache_t = time.monotonic()
        log.info(
            "paddle bg: cached mass=%r res=%r inst=%r",
            mass_raw, res_raw, inst_raw,
        )

    try:
        pool = _get_light_pool()
        with _paddle_cache_lock:
            _paddle_bg_fut = pool.submit(_worker)
    except Exception as exc:
        log.debug("paddle bg: submit failed: %s", exc)


# ─────────────────────────────────────────────────────────────
# Auto-harvest + online learning
# ─────────────────────────────────────────────────────────────

_reservoir = None
_learner = None


def _get_reservoir():
    global _reservoir
    if _reservoir is None:
        try:
            from .digit_reservoir import DigitReservoir
            _reservoir = DigitReservoir()
        except Exception as exc:
            log.debug("digit_reservoir init failed: %s", exc)
    return _reservoir


def _get_learner():
    global _learner
    if _learner is None:
        try:
            from .online_learner import OnlineLearner
            from pathlib import Path
            _learner = OnlineLearner(Path(_MODULE_DIR) / "models")
        except Exception as exc:
            log.debug("online_learner init failed: %s", exc)
    return _learner


def _vals_agree(*vals: Optional[float], rel_tol: float = 0.005) -> bool:
    """True if all non-None vals agree within rel_tol (0.5%)."""
    present = [v for v in vals if v is not None]
    if len(present) < 2:
        return False
    ref = present[0]
    for v in present[1:]:
        if abs(v - ref) > max(1.0, abs(ref) * rel_tol):
            return False
    return True


def _try_harvest_field(
    raw_string: str,
    crop_img: Optional[Image.Image],
) -> None:
    """Extract and store individual digit crops from a unanimous field.

    Re-segments the crop image using the same Otsu pipeline as the
    inference path, maps each glyph to its character from the agreed
    raw string, and pushes digit crops to the reservoir and learner.
    """
    if crop_img is None or not raw_string:
        return
    reservoir = _get_reservoir()
    learner = _get_learner()
    if reservoir is None and learner is None:
        return

    # Re-segment to get the 28×28 uint8 crops
    gray = np.array(crop_img.convert("L"), dtype=np.uint8)
    thr = _otsu(gray)
    binary = (gray > thr).astype(np.uint8) * 255

    # Build crops manually (mirrors _segment_and_infer preprocessing)
    h, w = gray.shape
    proj = np.sum(binary > 0, axis=0)
    spans = []
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

    crops_28 = []
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
        crops_28.append(np.array(pil, dtype=np.uint8))

    # Map crops to characters from the raw string.
    # Strip non-digit characters from the raw string to get labels,
    # then match by position. If counts don't match, skip.
    digit_chars = [ch for ch in raw_string if ch in "0123456789"]
    if len(digit_chars) != len(crops_28):
        return

    for ch, crop in zip(digit_chars, crops_28):
        if reservoir is not None:
            reservoir.add(ch, crop)
        if learner is not None:
            learner.submit(ch, crop)


def scan_hud_onnx(region: dict) -> dict[str, Optional[float]]:
    """Capture HUD region and extract mass + resistance + instability.

    Tries SC-OCR first (23ms, no subprocesses). If SC-OCR detects a
    light background (median gray > 130), falls back to the legacy
    Tesseract-based pipeline for label detection. This gives dark-bg
    scans the fast path (95% of gameplay) while keeping light-bg
    scans functional via Tesseract fallback.

    Parameters
    ----------
    region : dict
        Screen region {x, y, w, h} covering the mining scan panel.

    Returns
    -------
    dict with keys:
        - "mass" (float | None)
        - "resistance" (float | None)
        - "instability" (float | None)
        - "panel_visible" (bool): True when the scan panel's mineral-name
          row was located, regardless of whether numeric extraction
          succeeded. Callers use this to distinguish "no panel" (keep
          cached values) from "panel visible but value unreadable"
          (clear stale cache — the rock has changed).
    """
    result: dict[str, Optional[float]] = {
        "mass": None,
        "resistance": None,
        "instability": None,
        "panel_visible": False,
    }

    if not _ensure_model():
        return result

    t0 = time.time()

    # ── SC-OCR FAST PATH ──
    # Try SC-OCR first (23ms, no subprocesses). Only return its result
    # if it SUCCEEDS (panel_visible=True AND at least one value read).
    # Otherwise fall through SILENTLY to the legacy pipeline — no
    # panel_visible=False returned, so the scan loop won't clear
    # signal-scanner results.
    try:
        from .sc_ocr.api import scan_hud_onnx as _sc_ocr_scan
        sc_result = _sc_ocr_scan(region)
        # Only accept SC-OCR if it produced ALL THREE values —
        # partial reads (only mass, no resistance/instability) are
        # likely garbage from a misaligned mineral-row detection.
        # The legacy pipeline is more robust on unfamiliar panel
        # geometries, so fall through on any partial result.
        sc_has_data = (
            sc_result.get("panel_visible")
            and sc_result.get("mass") is not None
            and sc_result.get("resistance") is not None
            and sc_result.get("instability") is not None
        )
        if sc_has_data:
            elapsed = (time.time() - t0) * 1000
            log.info(
                "sc_ocr fast path: mass=%s resistance=%s instability=%s in %.0fms",
                sc_result.get("mass"), sc_result.get("resistance"),
                sc_result.get("instability"), elapsed,
            )
            return sc_result
        # SC-OCR didn't produce results — fall through to legacy.
        # Do NOT return panel_visible=False here; let legacy decide.
    except Exception as exc:
        log.debug("sc_ocr fast path error, using legacy: %s", exc)

    # ── LEGACY PIPELINE (light-bg fallback + original dark-bg) ──

    # Multi-frame averaging defeats the SC HUD's subpixel text
    # jitter animation that otherwise causes inconsistent OCR
    # reads and starves the consensus logic in _do_scan. Falls
    # back to a single capture if averaging is unavailable.
    img = capture_region_averaged(region)
    if img is None:
        img = capture_region(region)
    if img is None:
        return result

    # Debug: save the raw unmodified HUD capture so we can dry-run
    # the pipeline against the exact bytes the scanner sees.
    # Throttled to avoid disk-write overhead on the hot path.
    if _should_save_debug():
        _debug_save_raw(img, "debug_live_hud_raw.png")

    gray = np.array(img.convert("L"), dtype=np.uint8)

    # ── POLARITY DISPATCH ──
    # Light-background panels (sunlit asteroid / atmospheric scene
    # bleeding through the translucent HUD) have a median grayscale
    # well above the dark-background case (~55). When median > 130
    # the dark pipeline's fixed-threshold heuristics break down, so
    # we route the image through the PaddleOCR sidecar instead.
    #
    # Dark captures follow the tuned ONNX+Tesseract path below.
    if float(np.median(gray)) > 130:
        light_result = _scan_light(img)
        if light_result is not None:
            elapsed = (time.time() - t0) * 1000
            log.debug(
                "onnx_hud_reader[light]: mass=%s resistance=%s instability=%s in %.0fms",
                light_result["mass"], light_result["resistance"],
                light_result["instability"], elapsed,
            )
            return light_result
        # Sidecar unavailable — fall through to dark pipeline
        # as a best-effort; it won't work on light panels but at
        # least returns panel_visible=False so the bubble hides
        # instead of showing stale data.
        log.debug("paddle sidecar unavailable, falling back to dark pipeline")

    # Identify the MASS / RESISTANCE / INSTABILITY rows via Tesseract
    # label OCR with fallback to fixed pixel offsets.
    #
    # Label positions don't move within a rock — the HUD panel is
    # screen-locked and the labels are at fixed pixel offsets. Cache
    # the detected rows and reuse them across scans to skip the
    # expensive Tesseract label OCR (3+ subprocess spawns per call,
    # ~500 ms of pure subprocess overhead). Cache is keyed by region
    # geometry and invalidated when the panel disappears (handled by
    # the `panel_visible=False` path below which clears _label_cache).
    global _label_cache
    cache_key = (region["x"], region["y"], region["w"], region["h"])
    cached = _label_cache.get(cache_key)
    if cached is not None and (time.monotonic() - cached[0]) < _LABEL_CACHE_TTL_SEC:
        label_rows = cached[1]
    else:
        label_rows = _find_label_rows(img)
        if label_rows:
            _label_cache[cache_key] = (time.monotonic(), label_rows)
    mass_entry = label_rows.get("mass")
    res_entry = label_rows.get("resistance")
    inst_entry = label_rows.get("instability")

    # Fallback: use mineral-row anchor if any label is missing
    if mass_entry is None or res_entry is None or inst_entry is None:
        mineral_row = _find_mineral_row(img)
        if mineral_row is not None:
            mr_center = (mineral_row[0] + mineral_row[1]) // 2
            _ROW_HEIGHT_HALF = 15
            _OFFSETS = {"mass": 43, "resistance": 82, "instability": 120}
            _LABEL_RIGHTS = {"mass": 110, "resistance": 200, "instability": 205}

            def _fallback_row(field: str) -> tuple[int, int, int]:
                center = mr_center + _OFFSETS[field]
                y1 = max(0, center - _ROW_HEIGHT_HALF)
                y2 = min(img.height, center + _ROW_HEIGHT_HALF)
                return (y1, y2, _LABEL_RIGHTS[field])

            if mass_entry is None:
                mass_entry = _fallback_row("mass")
            if res_entry is None:
                res_entry = _fallback_row("resistance")
            if inst_entry is None:
                inst_entry = _fallback_row("instability")

    mass_row = mass_entry[:2] if mass_entry else None
    res_row = res_entry[:2] if res_entry else None
    inst_row = inst_entry[:2] if inst_entry else None

    # Debug: dump the full HUD capture with the detected label rows
    # boxed so we can verify offline.
    if _should_save_debug():
        overlay_rows = [r for r in (mass_row, res_row, inst_row) if r is not None]
        _debug_save_hud_rows(img, overlay_rows)

    # Panel is considered visible if we found at least a mass row —
    # that's the strongest indicator the scan panel is on screen and
    # showing real data. "Panel visible but mass unreadable" is
    # handled upstream by the stale-clear logic in app.py.
    if mass_row is not None:
        result["panel_visible"] = True

    if mass_row is None and res_row is None and inst_row is None:
        log.debug("onnx_hud_reader: no MASS/RESISTANCE labels found")
        # Panel not visible — clear caches so the next rock/panel
        # re-detects labels fresh and doesn't see stale values.
        _paddle_cache_clear()
        _label_cache.clear()
        return result

    mass_raw = res_raw = inst_raw = ""
    res_tess_raw = ""
    inst_tess_raw = ""

    # Use label_right as the x_min hint so the value crop ignores
    # anything left of the label, eliminating label+value fusion
    # without corrupting the text-mask local-background estimate.
    _LABEL_GAP = 6

    def _value_crop_for(entry: tuple[int, int, int] | None) -> Optional[Image.Image]:
        if entry is None:
            return None
        y1, y2, lbl_right = entry
        x_min = max(0, lbl_right + _LABEL_GAP)
        if x_min >= img.width - 4:
            return None
        return _find_value_crop(img, gray, y1, y2, x_min=x_min)

    mass_crop = _value_crop_for(mass_entry)
    res_crop = _value_crop_for(res_entry)
    inst_crop = _value_crop_for(inst_entry)

    # Throttled debug saves (out of the parallel hot path).
    # The raw HUD and rows overlay were already gated above with
    # the same _should_save_debug() check — match that decision here
    # and mark the save timestamp so the NEXT scan skips disk-write.
    if _should_save_debug():
        if mass_crop is not None:
            _debug_save_crop(mass_crop, "debug_live_mass_crop.png")
        if res_crop is not None:
            _debug_save_crop(res_crop, "debug_live_res_crop.png")
        if inst_crop is not None:
            _debug_save_crop(inst_crop, "debug_live_inst_crop.png")
        _mark_debug_saved()

    from concurrent.futures import ThreadPoolExecutor

    # Per-crop ONNX+Tesseract readers. These return the raw strings
    # from each engine along with the parsed value; final 3-way
    # reconciliation happens after the Paddle future also resolves.
    def _do_mass_ot() -> tuple[Optional[float], Optional[float], str, str]:
        if mass_crop is None:
            return None, None, "", ""
        onnx_raw = _ocr_crop(mass_crop)
        onnx_val = _parse_mass(onnx_raw)
        tess_raw_local = _tesseract_crop(mass_crop, whitelist="0123456789")
        tess_val = _parse_mass(tess_raw_local) if tess_raw_local else None
        return onnx_val, tess_val, onnx_raw, tess_raw_local

    def _do_res_ot() -> tuple[Optional[float], Optional[float], str, str]:
        if res_crop is None:
            return None, None, "", ""
        onnx_raw = _ocr_crop(res_crop)
        onnx_val = _parse_resistance(onnx_raw)
        tess_raw_local = _tesseract_crop(res_crop, whitelist="0123456789.%")
        tess_val = _parse_resistance(tess_raw_local) if tess_raw_local else None
        return onnx_val, tess_val, onnx_raw, tess_raw_local

    def _do_inst_ot() -> tuple[Optional[float], Optional[float], str, str]:
        if inst_crop is None:
            return None, None, "", ""
        onnx_raw = _ocr_crop(inst_crop)
        onnx_val = _parse_instability(onnx_raw)
        tess_raw_local = _tesseract_crop(inst_crop, whitelist="0123456789.")
        tess_val = _parse_instability(tess_raw_local) if tess_raw_local else None
        return onnx_val, tess_val, onnx_raw, tess_raw_local

    # Kick off an async Paddle pass on this HUD capture. Fire-and-
    # forget — we will not wait for it. The result will land in the
    # module-level cache and be available to the NEXT scan as a third
    # voter. Since the break bubble stays up for many seconds on the
    # same rock, the cached result is still valid for the text we
    # are reading right now.
    _paddle_dispatch_bg(img, mass_row, res_row, inst_row)

    # Run the three per-crop ONNX+Tesseract readers concurrently.
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_mass_ot = pool.submit(_do_mass_ot)
        fut_res_ot = pool.submit(_do_res_ot)
        fut_inst_ot = pool.submit(_do_inst_ot)

        onnx_mass, tess_mass, mass_raw, mass_tess_raw = fut_mass_ot.result()
        onnx_res, tess_res, res_raw, res_tess_raw = fut_res_ot.result()
        onnx_inst, tess_inst, inst_raw, inst_tess_raw = fut_inst_ot.result()

    # Read Paddle's most recent successful result from the cache.
    # Empty on the first scan of a new rock (typically one lag frame),
    # populated thereafter until the panel disappears.
    paddle_vals = _paddle_cache_get()
    pmass_val, pmass_raw = paddle_vals["mass"]
    pres_val, pres_raw = paddle_vals["resistance"]
    pinst_val, pinst_raw = paddle_vals["instability"]

    mass_val = _reconcile_mass(
        onnx_mass, tess_mass, mass_raw, mass_tess_raw,
        paddle_val=pmass_val, paddle_raw=pmass_raw,
    )
    res_val = _reconcile_resistance(
        onnx_res, tess_res, res_raw, res_tess_raw,
        paddle_val=pres_val, paddle_raw=pres_raw,
    )
    inst_val = _reconcile_instability(
        onnx_inst, tess_inst, inst_raw, inst_tess_raw,
        paddle_val=pinst_val, paddle_raw=pinst_raw,
    )

    result["mass"] = mass_val
    result["resistance"] = res_val
    result["instability"] = inst_val

    # ── AUTO-HARVEST ──
    # When all 3 engines agree on a field, the OCR label is
    # near-certainly correct. Harvest individual digit crops for
    # the reservoir and online learner. Cheap (~5ms per field)
    # and only fires on unanimous consensus, so it won't slow
    # down normal scanning.
    try:
        if _vals_agree(onnx_mass, tess_mass, pmass_val):
            _try_harvest_field(mass_raw, mass_crop)
        if _vals_agree(onnx_res, tess_res, pres_val):
            _try_harvest_field(res_raw, res_crop)
        if _vals_agree(onnx_inst, tess_inst, pinst_val):
            _try_harvest_field(inst_raw, inst_crop)
    except Exception as exc:
        log.debug("auto-harvest failed: %s", exc)

    elapsed = (time.time() - t0) * 1000
    # INFO level (not DEBUG) so the per-scan raw engine reads land in
    # mining_signals.log for live diagnosis. One line per scan at 1 Hz
    # is cheap and the rotating file handler caps total size.
    log.info(
        "onnx_hud_reader: mass=%s (onnx=%r tess=%r paddle=%r) "
        "resistance=%s (onnx=%r tess=%r paddle=%r) "
        "instability=%s (onnx=%r tess=%r paddle=%r) in %.0fms",
        result["mass"], mass_raw, mass_tess_raw, pmass_raw,
        result["resistance"], res_raw, res_tess_raw, pres_raw,
        result["instability"], inst_raw, inst_tess_raw, pinst_raw,
        elapsed,
    )
    return result
