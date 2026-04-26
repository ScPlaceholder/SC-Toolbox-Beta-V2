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

def _find_panel_lines(
    gray: np.ndarray,
    min_width_frac: float = 0.18,
    max_thickness: int = 3,
) -> list[tuple[int, int, int]]:
    """Detect horizontal HUD separator lines.

    The SC scan-results panel is bounded by thin horizontal HUD lines:
    one under the SCAN RESULTS header (above the mineral name) and
    one below the difficulty bar (above COMPOSITION). These lines are:
      - 1-2 px tall (much thinner than text rows, which are 14+ px)
      - span most of the panel width
      - high-contrast vs the panel background
      - rendered HUD chrome → present in EVERY scan, regardless of
        ship variant or HUD color

    Detection is polarity-independent (uses the existing edge mask)
    so light, dark, and noisy backgrounds all work the same.

    Returns a list of ``(y_center, x_left, x_right)`` tuples, sorted
    top-to-bottom. Each tuple gives the line's middle row and its
    horizontal endpoints.

    Notes:
      - Multiple consecutive bright rows are coalesced into one line.
      - Lines shorter than ``min_width_frac`` of image width are
        discarded (filters out cluster bars and short underlines).
      - Lines thicker than ``max_thickness`` are discarded (those are
        actual text rows, not HUD chrome).
    """
    h, w = gray.shape
    if h == 0 or w == 0:
        return []

    mask = _build_text_mask(gray)
    row_density = mask.sum(axis=1)
    min_width = int(w * min_width_frac)

    # Find consecutive runs of high-density rows.
    in_run = False
    run_start = 0
    runs: list[tuple[int, int]] = []
    for y in range(h + 1):
        d = row_density[y] if y < h else 0
        is_hot = d >= min_width
        if is_hot and not in_run:
            in_run = True
            run_start = y
        elif not is_hot and in_run:
            in_run = False
            runs.append((run_start, y))

    lines: list[tuple[int, int, int]] = []
    for y_start, y_end in runs:
        thickness = y_end - y_start
        if thickness == 0 or thickness > max_thickness:
            continue
        # Endpoints: leftmost and rightmost True column anywhere in
        # the line's vertical extent. Use ``any`` so a single broken
        # pixel doesn't truncate the line.
        line_mask = mask[y_start:y_end, :].any(axis=0)
        xs = np.where(line_mask)[0]
        if xs.size == 0:
            continue
        x_left = int(xs[0])
        x_right = int(xs[-1]) + 1
        span = x_right - x_left
        if span < min_width:
            continue
        # ── Continuity check ──
        # Span alone doesn't distinguish a real HUD separator (near-
        # solid, ≥95% of columns lit) from a 1-3 px text slice where
        # letter caps/baselines happen to span wide (e.g. "SCAN RESULTS"
        # at the top of the panel: wide, but ~50-70% lit because of
        # inter-letter gaps). Without this filter, text rows get
        # promoted to HUD lines and the panel-finder anchors the whole
        # geometry at the wrong y, compressing MASS/RESIST/INSTAB
        # boxes onto the header. Require ≥ 80% fill within the span.
        fill = int(line_mask[x_left:x_right].sum())
        if fill < int(span * 0.80):
            continue
        y_center = (y_start + y_end) // 2
        lines.append((y_center, x_left, x_right))
    return lines


# Per-scan cache for _find_panel_lines results. Keyed by id(gray) +
# shape so that the three-way row call (mass, resist, instab) inside
# a single scan only pays the detection cost once.
_panel_lines_cache: tuple[int, tuple[int, int], list[tuple[int, int, int]]] | None = None


def _get_panel_lines_cached(gray: np.ndarray) -> list[tuple[int, int, int]]:
    """Return _find_panel_lines(gray), cached per gray-array identity.

    Three rows in a single scan share the same gray; cache hits keep
    repeated calls free.
    """
    global _panel_lines_cache
    key = (id(gray), gray.shape)
    if _panel_lines_cache is not None:
        cid, cshape, clines = _panel_lines_cache
        if cid == key[0] and cshape == key[1]:
            return clines
    lines = _find_panel_lines(gray)
    _panel_lines_cache = (key[0], key[1], lines)
    return lines


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
    img: "Image.Image",
    gray: "np.ndarray",
    y1: int,
    y2: int,
    x_min: int = 0,
) -> "Optional[Image.Image]":
    """Crop the value sub-region of a row.

    SIMPLE PROVEN RECIPE (matches scripts/test_sc_ocr_on_annotations.py
    which reads the digits at 99-100% confidence):

      1. Crop the row strip [y1:y2, :].
      2. Polarity-canonicalize so text is BRIGHT (CNN training convention).
      3. Otsu threshold -> binary.
      4. Project to columns, find contiguous spans (>=2 px wide).
      5. Find the LARGEST gap between consecutive spans -- that's the
         label-to-value separator.
      6. Take all spans to the RIGHT of the largest gap as the value.
      7. Crop with ~4 px margin on each side.

    The previous implementation accumulated ~250 lines of cluster
    width filters, geometric fallbacks, line-mid clamping and
    multi-pass cluster acceptance; none of it improved on the simple
    recipe above for typical HUD content. Fewer moving parts,
    cleaner failure modes, easier to debug.

    Returns None only if the row is degenerate or no spans are found.
    """
    if y2 <= y1 or (y2 - y1) < 4:
        return None
    H, W = gray.shape
    y1 = max(0, y1)
    y2 = min(H, y2)
    if y2 - y1 < 4:
        return None

    # ── User's mental model (matches the annotated panel) ──
    #   GREEN line  = x_min  (just past INSTABILITY: colon)
    #   PURPLE line = W      (panel right edge)
    #   The VALUE LIVES BETWEEN green and purple. There can be NO label
    #   text in this region (we already moved past the colons).
    #
    # Algorithm:
    #   1. Crop the value column [x_min : W] from the row strip.
    #   2. Use MAX-OF-CHANNELS for text detection (so red/green/yellow
    #      text registers as bright — luminance grayscale loses red).
    #   3. Two-tier vertical-density mask (strict for strokes,
    #      permissive for dots, joined by adjacency dilation).
    #   4. Find contiguous text spans.
    #   5. Crop tight around all spans (they're ALL value text since
    #      labels are excluded by x_min).
    x_lo = max(0, x_min) if x_min > 0 else 0
    x_hi = W
    if x_hi - x_lo < 8:
        return None

    # Slice once to the value-column region (saves a copy and bounds
    # all subsequent indices).
    try:
        rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
        detect_region = rgb[y1:y2, x_lo:x_hi].max(axis=2)
    except Exception:
        detect_region = gray[y1:y2, x_lo:x_hi]

    # Polarity-canonicalize so text is the BRIGHT class.
    thr_d = _otsu(detect_region)
    bright_count = int((detect_region > thr_d).sum())
    dark_count = detect_region.size - bright_count
    if dark_count < bright_count:
        detect_canon = (255 - detect_region).astype(np.uint8)
    else:
        detect_canon = detect_region.astype(np.uint8)

    # Binary text mask via Otsu on the canonicalized region.
    thr2 = _otsu(detect_canon)
    binary = (detect_canon > thr2).astype(np.uint8)

    # Two-tier density: strict floor catches text strokes, permissive
    # floor catches dots, joined by adjacency dilation.
    row_h = y2 - y1
    region_w = x_hi - x_lo
    strict_floor = max(4, int(row_h * 0.25))
    permissive_floor = 3
    proj = binary.sum(axis=0)
    strict_hot = proj >= strict_floor
    permissive_hot = proj >= permissive_floor

    if strict_hot.any():
        dilate_radius = 6
        kernel = np.ones(2 * dilate_radius + 1, dtype=np.int32)
        dilated = np.convolve(
            strict_hot.astype(np.int32), kernel, mode="same",
        ) > 0
        hot = dilated & permissive_hot
    else:
        hot = strict_hot

    # Find contiguous text spans (right-to-left scan per the user's
    # spec — purple back toward green — so we naturally identify the
    # rightmost text cluster first).
    spans_rtl: list[tuple[int, int]] = []
    in_run = False
    end = 0
    for i in range(region_w - 1, -1, -1):
        if hot[i] and not in_run:
            in_run = True
            end = i + 1
        elif not hot[i] and in_run:
            in_run = False
            if end - (i + 1) >= 2:
                spans_rtl.append((i + 1, end))
    if in_run and end >= 2:
        spans_rtl.append((0, end))

    if not spans_rtl:
        return None

    # spans_rtl is right-to-left ordered. Convert to left-to-right
    # for cropping bounds.
    spans = list(reversed(spans_rtl))

    # Crop from the GREEN LINE (x_lo) to the rightmost text + margin.
    # Per user spec: "start reading rows right of MASS: green line,
    # slide to purple line, scan back to green to find numerical
    # values". So the LEFT edge stays at x_lo (the green line) — we
    # don't trim inward to where the digits visually start. The
    # RIGHT edge is the rightmost text + small margin (so we don't
    # waste pipeline cycles on empty pixels past the value).
    v_right_local = min(region_w, spans[-1][1] + 4)
    if v_right_local < 4:
        return None

    # LEFT edge = 0 (which maps to x_lo in image coords) — preserves
    # the user's "start at green line" anchor.
    return img.crop((x_lo, y1, x_lo + v_right_local, y2))




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

    # Vertical projection segmentation. Min span width 2 (was 3) so
    # narrow chars like '1' and '.' aren't dropped — matches the
    # offline extractor that produced the high-accuracy training data.
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
            if x - start >= 2:
                spans.append((start, x))

    # Right-anchored span filter:
    # SC HUD values are read right-to-left and the LEFT edge is where
    # label-text intrusion shows up (e.g. trailing colon of
    # "RESISTANCE:" leaking in front of "0%"). Find the largest gap
    # between adjacent spans; if it's much wider than the typical
    # gap, discard everything LEFT of it (those are label artifacts).
    if len(spans) >= 2:
        gaps = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
        largest_gap, gap_idx = max(gaps, key=lambda g: g[0])
        if gaps:
            sorted_gaps = sorted(g for g, _ in gaps)
            median_gap = sorted_gaps[len(sorted_gaps) // 2]
        else:
            median_gap = 0
        # Heuristic: a gap that's >2× the median or >18 px absolute
        # marks the boundary between label text and the value cluster.
        if largest_gap >= max(18, median_gap * 2 + 4):
            spans = spans[gap_idx + 1:]

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

    # Confidence-filtered predictions. Glyphs that the CNN can't classify
    # confidently (typically: chunks of label text accidentally captured
    # in the value crop) are dropped rather than mislabeled.
    _MIN_CONFIDENCE = 0.40
    results: list[tuple[str, float]] = []
    for i in range(len(char_images)):
        probs = np.exp(logits[i] - np.max(logits[i]))
        probs /= probs.sum()
        idx = int(np.argmax(probs))
        conf = float(probs[idx])
        if conf < _MIN_CONFIDENCE:
            continue
        results.append((_char_classes[idx], conf))
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

    # Engine A: Otsu threshold on grayscale (white/cyan text).
    # Now feeds the GRAY channel to the model — matches training data.
    thr_a = _otsu(gray)
    binary_a = (gray > thr_a).astype(np.uint8) * 255
    results_a = _segment_and_infer(gray, binary_a)

    # Engine C: Otsu threshold on max-of-channels for red text detection,
    # BUT feed the GRAY pixels to the model — the custom CNN was trained
    # on luminance crops; max-of-channels has a different intensity
    # profile that systematically biases predictions (e.g. 0->9, 1->7).
    thr_c = _otsu(max_ch)
    binary_c = (max_ch > thr_c).astype(np.uint8) * 255
    results_c = _segment_and_infer(gray, binary_c)

    # Engine B (fixed threshold 140 on grayscale) removed — its high
    # per-character confidence on wrong characters was poisoning the
    # vote on red text (e.g. "569" for "96" where it voted '5' at
    # position 0 with 0.46 conf over Engine A's '9' at 0.36).

    engines = [r for r in (results_a, results_c) if r]
    if not engines:
        return ""

    # Engine selection: pick the engine with the highest MEAN
    # confidence per character. Previously we picked the engine with
    # the most characters, which let phantom leading digits (label-
    # text intrusion bleeding into the value crop) outvote the truth
    # — e.g. "20%" beating "0%" on length even though the leading
    # "2" was a low-confidence garbage prediction.
    def _mean_conf(rs: list[tuple[str, float]]) -> float:
        return sum(c for _, c in rs) / len(rs) if rs else 0.0

    # If all engines agree on length, do per-position confidence vote.
    if len(set(len(r) for r in engines)) == 1:
        n = len(engines[0])
        text = ""
        for i in range(n):
            options = [e[i] for e in engines]
            ch, _ = max(options, key=lambda x: x[1])
            text += ch
        return text

    # Otherwise, pick the engine with the highest mean confidence.
    best = max(engines, key=_mean_conf)
    return "".join(ch for ch, _ in best)


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


# Cache of SCAN RESULTS title position per region key. Once we know
# where the title is in the captured region, the entire panel layout
# follows by FIXED PROPORTIONAL OFFSETS — no per-frame guessing.
# Cleared when the panel disappears (handled by callers via
# _label_cache.clear() / _scan_results_anchor_cache.clear()).
_scan_results_anchor_cache: dict[tuple[int, int], tuple[float, dict]] = {}


def _find_scan_results_anchor(img: Image.Image) -> Optional[dict]:
    """Find the SCAN RESULTS title via Tesseract and return geometry.

    The SC mining HUD has a FIXED layout. Once we locate the SCAN
    RESULTS title — large bold static text, easy for Tesseract to
    read reliably across light/dark/noisy backgrounds — every other
    row position is a known proportional offset.

    Returns a dict::

        {
            "title_x": int,   # left edge of "SCAN" word
            "title_y": int,   # top of title text
            "title_h": int,   # title text height
            "title_w": int,   # extent across "SCAN RESULTS"
        }

    or None if Tesseract can't find the title (panel not visible,
    occluded, or PaddleOCR-only path).

    Tesseract is run with a UPPERCASE-letters whitelist (the title
    is always rendered in caps) and PSM 11 (sparse text), which is
    fast and tolerant of HUD backgrounds.
    """
    if not _check_tesseract():
        return None
    try:
        import pytesseract
    except ImportError:
        return None

    # Search the top half of the image — title is always near the top.
    w_img, h_img = img.size
    top = img.crop((0, 0, w_img, min(h_img, max(120, h_img // 2))))
    gray = np.array(top.convert("L"), dtype=np.uint8)

    # Run two polarity variants (dark- and light-background HUDs)
    thr = _otsu(gray)
    variants = [
        np.where(gray > thr, 0, 255).astype(np.uint8),
        np.where(gray < thr, 0, 255).astype(np.uint8),
    ]
    best: Optional[tuple[int, int, int, int]] = None
    for binary in variants:
        binary_pil = Image.fromarray(binary)
        try:
            data = pytesseract.image_to_data(
                binary_pil,
                config=(
                    "--psm 11 -c tessedit_char_whitelist="
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                ),
                output_type=pytesseract.Output.DICT,
            )
        except Exception:
            continue
        n = len(data.get("text", []))
        # Collect all "SCAN" and "RESULTS" hits with their bboxes
        scan_hits: list[tuple[int, int, int, int]] = []
        result_hits: list[tuple[int, int, int, int]] = []
        for i in range(n):
            txt = (data["text"][i] or "").strip().upper()
            if not txt or len(txt) < 3:
                continue
            x = int(data["left"][i])
            y = int(data["top"][i])
            ww = int(data["width"][i])
            hh = int(data["height"][i])
            if "SCAN" in txt:
                scan_hits.append((x, y, ww, hh))
            elif "RESULT" in txt:
                result_hits.append((x, y, ww, hh))
        # Find a SCAN+RESULTS pair on roughly the same line
        for sx, sy, sw, sh in scan_hits:
            for rx, ry, rw, rh in result_hits:
                if abs(sy - ry) > max(sh, rh):
                    continue  # not on the same line
                if rx <= sx:
                    continue  # RESULTS should be to the right of SCAN
                title_x = sx
                title_y = min(sy, ry)
                title_h = max(sh, rh)
                title_w = (rx + rw) - sx
                if best is None or title_h > best[3]:
                    best = (title_x, title_y, title_w, title_h)
    if best is None:
        return None
    title_x, title_y, title_w, title_h = best
    return {
        "title_x": title_x,
        "title_y": title_y,
        "title_h": title_h,
        "title_w": title_w,
    }


# ── Fixed proportional offsets from SCAN RESULTS title to each row ──
# These are measured against the title HEIGHT (which scales with
# panel scale automatically). Center y of each row is computed as:
#   row_y_center = title_y + title_h * MULTIPLIER
# Multipliers measured from the 397-px reference panel:
#   title bottom is at title_y + title_h
#   mineral row center ≈ title bottom + 1.6 * title_h
#   mass row center    ≈ title bottom + 3.0 * title_h
#   resist row center  ≈ title bottom + 4.4 * title_h
#   instab row center  ≈ title bottom + 5.8 * title_h
#   outcome bar center ≈ title bottom + 7.4 * title_h
_ROW_OFFSET_MULTS = {
    "_mineral_row": 1.6,
    "mass":         3.0,
    "resistance":   4.4,
    "instability":  5.8,
}
_ROW_HEIGHT_MULT = 0.9   # half-height = 0.9 * title_h
# Label-right (value-column-left anchor) as a fraction of img.width.
# Measured from the reference panel: ~52%.
_VALUE_COL_LEFT_FRAC = 0.52


def _label_rows_from_anchor(
    img: Image.Image, anchor: dict,
) -> dict[str, tuple[int, int, int]]:
    """Compute label rows + mineral row from the SCAN RESULTS anchor.

    Pure geometry — no projection, no peak detection, no per-frame
    guessing. Once the anchor is correct, every row IS where it
    structurally must be.
    """
    title_y = anchor["title_y"]
    title_h = anchor["title_h"]
    title_bottom = title_y + title_h
    half_h = max(8, int(title_h * _ROW_HEIGHT_MULT * 0.5))
    label_right = int(img.width * _VALUE_COL_LEFT_FRAC)
    result: dict[str, tuple[int, int, int]] = {}
    for key, mult in _ROW_OFFSET_MULTS.items():
        center_y = title_bottom + int(title_h * (mult - 1.0))
        y1 = max(0, center_y - half_h)
        y2 = min(img.height, center_y + half_h)
        result[key] = (y1, y2, label_right)
    return result


def _find_label_rows_by_hud_grid(
    img: Image.Image,
) -> dict[str, tuple[int, int, int]]:
    """HUD-grid label-row finder — pure geometry, no text detection.

    The SCAN RESULTS panel is a fixed UI grid bounded by two HUD
    chrome separator lines:

        ─── TOP LINE ───  (under "SCAN RESULTS" title)
            Resource (mineral name)
            Mass:        <value>
            Resistance:  <value>
            Instability: <value>
            ( DIFFICULTY )
        ─── BOT LINE ───  (above "COMPOSITION")

    Every row sits at a FIXED FRACTION of the span between these
    two lines. No per-frame band detection, no Tesseract anchor —
    just pick the correct line pair and apply the constants.

    Returns the same dict shape as ``_find_label_rows`` plus the
    special ``_mineral_row`` key. Returns ``{}`` when fewer than
    2 HUD lines are detected (panel not visible / partial capture).
    """
    gray = np.array(img.convert("L"), dtype=np.uint8)
    lines = _get_panel_lines_cached(gray)
    if len(lines) < 2:
        return {}

    # Choose the bracketing line pair. The SCAN RESULTS data area
    # has a span typically 100-400 px depending on capture scale.
    # We pick the FIRST line pair (i, j) where the gap is in that
    # range and i is as high as possible.
    top_y: Optional[int] = None
    bot_y: Optional[int] = None
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            gap = lines[j][0] - lines[i][0]
            if 100 <= gap <= 450:
                top_y = lines[i][0]
                bot_y = lines[j][0]
                break
        if top_y is not None:
            break

    # If no plausible pair, fall back to (lines[0], lines[1]) if
    # they're at least 80 px apart, else give up.
    if top_y is None or bot_y is None:
        if len(lines) >= 2 and (lines[1][0] - lines[0][0]) >= 80:
            top_y = lines[0][0]
            bot_y = lines[1][0]
        else:
            return {}

    span = bot_y - top_y

    # ── Fixed fractions (calibrated from real game panels) ──
    # The data area between the two HUD lines holds 5 rows in
    # roughly even spacing:
    #
    #   ═══ TOP LINE ═══         frac = 0.00
    #   Resource (mineral)       frac = 0.13   ← centered
    #   Mass: <value>            frac = 0.31
    #   Resistance: <value>      frac = 0.49
    #   Instability: <value>     frac = 0.67
    #   ( DIFFICULTY )           frac = 0.86
    #   ═══ BOT LINE ═══         frac = 1.00
    #
    # These are FIXED — the SCAN RESULTS panel is a static UI
    # element. No per-frame guessing.
    _ROW_FRACTIONS = {
        "_mineral_row": 0.13,
        "mass":         0.31,
        "resistance":   0.49,
        "instability":  0.67,
    }

    # Half-row-height: 4.5% of span on each side of center, giving
    # 9% total row height. Row pitch is ~18% of span, so the gap
    # between adjacent rows is ~9% — comfortable margin so the row
    # bands NEVER overlap, which would cause _find_value_crop's
    # y-tightening to grab the wrong row's content.
    half_h = max(8, int(span * 0.045))

    # Value-column-left anchor: a fixed fraction of image width
    # past the longest label. Calibrated to ~52%.
    label_right = int(img.width * 0.52)

    result: dict[str, tuple[int, int, int]] = {}
    for key, frac in _ROW_FRACTIONS.items():
        cy = top_y + int(span * frac)
        result[key] = (
            max(0, cy - half_h),
            min(img.height, cy + half_h),
            label_right,
        )

    log.debug(
        "onnx_hud_reader: HUD grid OK (top=%d, bot=%d, span=%d, "
        "half_h=%d, lr=%d)",
        top_y, bot_y, span, half_h, label_right,
    )

    # Push telemetry to debug overlay.
    try:
        from .sc_ocr import debug_overlay as _dbg
        _dbg.set_hud_lines(lines)
        _dbg.set_panel_finder(
            top_y=top_y,
            mineral_y_top=result["_mineral_row"][0],
            mineral_y_bot=result["_mineral_row"][1],
            mineral_center=(result["_mineral_row"][0] + result["_mineral_row"][1]) // 2,
            pitch=int(span * 0.18),
            bot_line_y=bot_y,
            source="hud_grid",
        )
    except Exception:
        pass

    return result


def _find_label_rows_by_position(
    img: Image.Image,
) -> dict[str, tuple[int, int, int]]:
    """Position-based label-row finder — NO TESSERACT.

    Uses HUD geometry instead of OCR:
      1. Detect the two horizontal HUD separator lines that bracket
         the SCAN RESULTS data area (``_find_panel_lines``).
      2. Run horizontal-projection text-band detection inside the
         band between those lines.
      3. The band always contains exactly 5 text rows in a fixed
         order: [mineral_name, MASS, RESISTANCE, INSTABILITY,
         difficulty_bar]. Assign roles by ORDINAL POSITION — no need
         to read the labels.
      4. For each row, compute the label's right edge (colon
         position) via column-density scan in the left half of the
         row.

    This eliminates Tesseract from the critical row-positioning
    path. Tesseract was the source of "MASS detected at RESISTANCE's
    y" bugs because its LSTM is trained on printed documents and
    misbehaves on bright-sky / colored / anti-aliased HUD text.
    Position-based assignment is structurally immune to that
    failure mode: if 5 bands exist between the lines, they ARE
    [mineral, mass, resist, instab, difficulty].

    Returns the same shape as ``_find_label_rows`` so callers don't
    care which engine produced it. Returns ``{}`` when:
      - Fewer than 2 HUD lines detected (panel not visible)
      - Fewer than 4 text bands between the lines (panel too small
        or sky bleed corrupted the projection)
    """
    gray = np.array(img.convert("L"), dtype=np.uint8)
    lines = _get_panel_lines_cached(gray)
    if not lines:
        return {}

    # ── Multi-anchor row detection ──
    # Single-peak detection was unstable: text rows have ascender +
    # x-height sub-peaks that get counted as separate rows on dark
    # backgrounds, AND merged bands on light backgrounds caused
    # rejections. Multi-anchor approach uses 3 independent anchors:
    #   ANCHOR 1: top HUD line (above mineral name)
    #   ANCHOR 2: mineral-name BAND (first text band below top line)
    #   ANCHOR 3: row pitch (panel-scaled, refined from line pair if
    #             both top and bottom lines are detected)
    # Then MASS/RESIST/INSTAB y-positions are EXTRAPOLATED from the
    # mineral-name anchor using fixed pitch. Only one anchor needs
    # to be correct for the whole geometry to fall out — and the
    # mineral name is the easiest to detect because it's always the
    # FIRST text band right below the top HUD line.
    top_y = lines[0][0]

    # Search starts a few px BELOW top_y to skip the HUD line itself.
    # The HUD line is bright enough (yellow ~180+ gray) to register
    # as text in the polarity mask. Without this offset the very
    # first detected band IS the line, which then becomes "Resource"
    # and shifts every subsequent row assignment by one slot.
    _LINE_SKIP = 5
    search_origin = top_y + _LINE_SKIP
    search_h = min(img.height - search_origin, 250)
    if search_h < 80:
        return {}
    band = gray[search_origin:search_origin + search_h, :]
    text_mask = _build_text_mask(band)
    proj = text_mask.sum(axis=1).astype(np.float32)
    if proj.size < 20 or float(proj.max()) <= 0:
        return {}

    # Heavy smoothing (7-px box) to merge ascender+x-height sub-peaks
    # within one row into a single peak.
    if proj.size >= 9:
        kernel = np.ones(7, dtype=np.float32) / 7.0
        proj = np.convolve(proj, kernel, mode="same")

    # ── DIRECT 5-band detection (no extrapolation) ──
    # The SCAN RESULTS panel between the top and bottom HUD lines
    # contains EXACTLY 5 text bands in a fixed order:
    #   index 0: Resource (mineral name, e.g. "BERYL (RAW)")
    #   index 1: Mass row
    #   index 2: Resistance row
    #   index 3: Instability row
    #   index 4: Outcome bar (EASY / MEDIUM / HARD / EXTREME / IMPOSSIBLE)
    #
    # Previously we tried to extrapolate from the mineral row using a
    # single pitch value. That meant any error in mineral detection
    # cascaded — and on this panel the SCAN RESULTS title's HUD
    # underline kept being picked up as the "first band" instead of
    # the actual mineral name, which shifted every downstream row by
    # one slot.
    #
    # Direct assignment by ordinal position is structurally immune to
    # that whole class of bug: detect all bands, assign by index.
    band_thr = max(8.0, float(proj.max()) * 0.12)
    bands: list[tuple[int, int, float]] = []  # (y_start, y_end, peak)
    in_band = False
    bs = 0
    for y in range(proj.size):
        v = float(proj[y])
        if v >= band_thr and not in_band:
            in_band = True
            bs = y
        elif v < band_thr and in_band:
            in_band = False
            bands.append((bs, y, float(proj[bs:y].max())))
    if in_band:
        bands.append((bs, int(proj.size), float(proj[bs:].max())))

    if not bands:
        log.debug(
            "onnx_hud_reader: position-based — no bands found "
            "below top_line=%d (max_proj=%.1f, threshold=%.1f)",
            top_y, float(proj.max()), band_thr,
        )
        return {}

    # Filter by reasonable height for a single text row.
    # Outcome bar (rendering trick) can be slightly taller (~35 px),
    # so we use a 60 px ceiling. Floor at 4 px to drop any 1-px
    # divider artifacts.
    bands = [b for b in bands if 4 <= (b[1] - b[0]) <= 60]

    if len(bands) < 4:
        log.debug(
            "onnx_hud_reader: position-based — only %d bands found "
            "(need at least 4 for mineral+mass+resist+instab)",
            len(bands),
        )
        return {}

    # If the panel finder over-detected (sometimes happens when
    # a thin separator line between resource and mass survives the
    # height filter), drop bands that are very close to a stronger
    # neighbour: any pair with center-to-center distance < 12 px,
    # keep the taller one.
    bands.sort(key=lambda b: b[0])
    deduped: list[tuple[int, int, float]] = []
    for b in bands:
        if deduped:
            prev = deduped[-1]
            prev_center = (prev[0] + prev[1]) // 2
            this_center = (b[0] + b[1]) // 2
            if (this_center - prev_center) < 12:
                # Merge — keep whichever has the higher peak
                if b[2] > prev[2]:
                    deduped[-1] = b
                continue
        deduped.append(b)
    bands = deduped

    # ── Skip the SCAN RESULTS title if it accidentally got into bands ──
    # When the line detector picks up a decorative element above the
    # SCAN RESULTS title (a known false positive on some captures),
    # top_y lands above the title. The first text band found is then
    # the title itself, NOT the mineral name. The reliable signature
    # of this failure mode: a HUD line sits BETWEEN bands[0] and
    # bands[1] in absolute image coordinates (the line under SCAN
    # RESULTS that separates title from data area).
    #
    # Detection: walk the line list; if any line's y is strictly
    # between bands[0].y_end and bands[1].y_start (in absolute
    # coords), bands[0] is the title — drop it.
    if len(bands) >= 2 and lines:
        b0_end_abs = search_origin + bands[0][1]
        b1_start_abs = search_origin + bands[1][0]
        for ly, _, _ in lines:
            if b0_end_abs < ly < b1_start_abs:
                log.debug(
                    "onnx_hud_reader: dropping bands[0] (SCAN RESULTS "
                    "title) — HUD line found at y=%d between band[0] "
                    "(ends y=%d) and band[1] (starts y=%d)",
                    ly, b0_end_abs, b1_start_abs,
                )
                bands = bands[1:]
                break

    # Take the first 5 bands — Resource, Mass, Resistance, Instability,
    # Outcome (in that order). If fewer than 5, the outcome bar is
    # presumed missing (rare); we still assign indices 0..3.
    bands = bands[:5]

    # Anchor outputs:
    #   mineral row     = bands[0]
    #   mass / resist / instab rows = bands[1] / bands[2] / bands[3]
    mineral_y_rel, mineral_y_end_rel, _ = bands[0]
    mineral_center_rel = (mineral_y_rel + mineral_y_end_rel) // 2
    mineral_y_abs = search_origin + mineral_center_rel

    # ── DIRECT band assignment (no extrapolation) ──
    # bands[0] = Resource (mineral), bands[1] = Mass,
    # bands[2] = Resistance, bands[3] = Instability,
    # bands[4] = Outcome (if present, only used for telemetry).
    # If we have only 4 bands (outcome bar undetected on a noisy
    # frame), still take indices 1..3 for the value rows.
    label_keys = ["mass", "resistance", "instability"]
    if len(bands) < 4:
        log.debug(
            "onnx_hud_reader: position-based — only %d bands after "
            "dedupe (need 4+ for mass/resist/instab)", len(bands),
        )
        return {}

    # Convert all 5 bands to absolute image coordinates.
    abs_band_rows = [(search_origin + s, search_origin + e) for s, e, _ in bands]

    # Pick mass/resist/instab in order. Add small ±_PAD so character
    # ascenders/descenders aren't clipped (the existing _PAD constant
    # below is used for the same purpose at output time, but we want
    # the box to be a bit wider here so OCR has breathing room).
    target_rows = [
        abs_band_rows[1],   # mass
        abs_band_rows[2],   # resistance
        abs_band_rows[3],   # instability
    ]

    # Compute pitch from the actual measured spacing between detected
    # bands (used by downstream consumers / debug overlay).
    spacings = [
        abs_band_rows[i + 1][0] - abs_band_rows[i][0]
        for i in range(min(3, len(abs_band_rows) - 1))
    ]
    if spacings:
        pitch = int(round(sum(spacings) / len(spacings)))
    else:
        pitch = 30  # fallback

    # Find the bottom HUD line for telemetry only (not used for pitch
    # anymore). Range is generous because some captures stretch the
    # panel vertically.
    bot_line_y: Optional[int] = None
    for ly, _, _ in lines[1:]:
        if 100 <= ly - top_y <= 600:
            bot_line_y = ly
            break

    # Compute label-right (colon position) per row via column-density
    # scan in the left half. Pure NumPy, no OCR.
    text_mask = _build_text_mask(gray, deviation=30)
    half_w = img.width // 2
    _PAD = 3
    _GAP_THRESHOLD = 14
    # Per-key fallback right-edge fractions (used only when the
    # column scan fails to find any label pixels).
    _FALLBACK_RIGHT_FRAC = {"mass": 0.18, "resistance": 0.34, "instability": 0.36}

    # First pass: scan each row's label-right (colon position).
    per_row_label_right: dict[str, int] = {}
    for key, (y1, y2) in zip(label_keys, target_rows):
        col_hot = text_mask[y1:y2, :].sum(axis=0) >= 2
        hot_idxs = np.where(col_hot[:half_w])[0]
        if hot_idxs.size == 0:
            per_row_label_right[key] = int(img.width * _FALLBACK_RIGHT_FRAC[key])
            continue
        x_start = int(hot_idxs[0])
        scanned_right = x_start
        gap_run = 0
        x = x_start
        while x < col_hot.shape[0]:
            if col_hot[x]:
                scanned_right = x + 1
                gap_run = 0
            else:
                gap_run += 1
                if gap_run >= _GAP_THRESHOLD:
                    break
            x += 1
        per_row_label_right[key] = min(scanned_right, half_w)

    # ── Shared value-column anchor ──
    # The HUD left-aligns ALL three values to a SINGLE column whose
    # left edge is past the LONGEST label (INSTABILITY:). MASS,
    # RESISTANCE, and INSTABILITY values therefore all start at the
    # same x. Use the MAX label-right across rows as the shared
    # value-column-left anchor — every row's value crop uses this
    # same x_min downstream.
    shared_label_right = max(per_row_label_right.values())

    result: dict[str, tuple[int, int, int]] = {}
    for key, (y1, y2) in zip(label_keys, target_rows):
        result[key] = (
            max(0, y1 - _PAD),
            min(img.height, y2 + _PAD),
            shared_label_right,
        )
    # ── Surface the mineral-name row too ──
    # Stored under a leading-underscore key so callers iterating the
    # dict for label rows (mass/resistance/instability) won't pick it
    # up by accident. scan_hud_onnx uses this to OCR the mineral
    # name with the alphabet model (or Tesseract as placeholder)
    # and add it to the scan result.
    result["_mineral_row"] = (
        max(0, search_origin + mineral_y_rel - _PAD),
        min(img.height, search_origin + mineral_y_end_rel + _PAD),
        shared_label_right,
    )
    log.debug(
        "onnx_hud_reader: label_rows_by_position OK "
        "(top_line=%d, search_origin=%d, mineral_y=%d, pitch=%d, "
        "bot_line=%s, bands=%d, shared_label_right=%d, mass_y=%d-%d)",
        top_y, search_origin, mineral_y_abs, pitch, bot_line_y,
        len(bands), shared_label_right,
        result["mass"][0], result["mass"][1],
    )
    # Stash telemetry for the debug overlay viewer.
    try:
        from .sc_ocr import debug_overlay as _dbg
        _dbg.set_hud_lines(lines)
        _dbg.set_panel_finder(
            top_y=top_y,
            mineral_y_top=search_origin + (mineral_y_rel or 0),
            mineral_y_bot=search_origin + (mineral_y_end_rel or 0),
            mineral_center=mineral_y_abs,
            pitch=pitch,
            bot_line_y=bot_line_y,
            source="by_position",
        )
    except Exception:
        pass
    return result


def _find_label_rows_by_ncc(
    img: Image.Image,
) -> dict[str, tuple[int, int, int]]:
    """NCC-template-match adapter — wraps ``label_match.find_label_positions``
    into the standard ``_find_label_rows`` return shape.

    Returns the standard dict with keys mass/resistance/instability/
    _mineral_row, where:
      * Each row's y1, y2 = match.y - small_pad, match.y + match.h + small_pad
      * shared label_right = max of (match.x + match.w) across the 3
        matched labels — that's the rightmost colon position, used as
        the value-column-left anchor for ALL rows (HUD values are
        left-aligned to a single column).
      * mineral row is synthesized from MASS row position minus one
        pitch (computed from observed row spacing).

    Returns ``{}`` on no/insufficient matches → caller falls back.
    """
    try:
        from .sc_ocr import label_match
    except Exception as exc:
        log.debug("label_match import failed: %s", exc)
        return {}

    matches = label_match.find_label_positions(img)
    # MASS is the anchor. If we don't have MASS, we have nothing
    # reliable to anchor on — fall back. (Other rows can be missing;
    # we synthesize them from MASS via fixed pitch.)
    if "mass" not in matches:
        log.debug(
            "label_match: MASS not matched — falling back "
            "(matches=%s)", list(matches.keys()),
        )
        return {}

    _PAD_Y = 4

    # Compute shared_label_right = rightmost ACTUAL text column across
    # all matched label regions. Don't trust the template's reported
    # width — bootstrap templates were extracted with padding AND
    # Tesseract often over-estimated the bbox width, so match.x +
    # match.w lands ~30-50 px past the colon (in the value area).
    #
    # For each matched label, scan the matched x-range for the
    # rightmost column with significant text density. That column is
    # the colon. Take the max across labels.
    try:
        gray_full = np.array(img.convert("L"), dtype=np.uint8)
        rgb_full = np.array(img.convert("RGB"), dtype=np.uint8)
        detect_full = rgb_full.max(axis=2).astype(np.uint8)
    except Exception:
        gray_full = None
        detect_full = None

    def _scan_actual_label_right(m: dict) -> int:
        if detect_full is None:
            return m["x"] + m["w"]
        x1 = max(0, m["x"])
        x2 = min(detect_full.shape[1], m["x"] + m["w"])
        y1 = max(0, m["y"])
        y2 = min(detect_full.shape[0], m["y"] + m["h"])
        if x2 <= x1 or y2 <= y1:
            return m["x"] + m["w"]
        region = detect_full[y1:y2, x1:x2]
        # Polarity-canonicalize so text is BRIGHT
        thr = _otsu(region)
        bright = int((region > thr).sum())
        if (region.size - bright) < bright:
            region_canon = (255 - region).astype(np.uint8)
        else:
            region_canon = region
        thr2 = _otsu(region_canon)
        col_density = (region_canon > thr2).sum(axis=0)
        # Rightmost column with at least 25% of region height as text
        floor = max(2, int((y2 - y1) * 0.25))
        idxs = np.where(col_density >= floor)[0]
        if idxs.size == 0:
            return m["x"] + m["w"]
        # Convert local idx to image-coord x and add small margin (the
        # colon's right edge halo).
        return x1 + int(idxs[-1]) + 2

    # ── MASS is the anchor ──
    # MASS_y, MASS_h define the entire panel geometry. Other rows are
    # at fixed proportional offsets from MASS. Other matches (RESIST,
    # INSTAB) are CROSS-CHECKS: if they line up with MASS-derived
    # predictions, great; if not, we trust MASS and synthesize the
    # others. This prevents one bad NCC false-positive (e.g. RESIST
    # matched to asteroid noise far below the real panel) from
    # dragging the whole row stack off-panel.
    mass_match = matches["mass"]
    mass_cy = mass_match["y"] + mass_match["h"] // 2
    mass_h = mass_match["h"]

    # ── Pitch (vertical distance between adjacent rows) ──
    # Three sources, in order of preference:
    #
    # 1. HUD line pair: top_line under SCAN RESULTS + bot_line above
    #    COMPOSITION bracket the data area, which holds 5 rows
    #    (mineral + mass + resist + instab + outcome). pitch =
    #    line_gap / 5. This is MEASURED, not guessed — most reliable.
    #
    # 2. Observed MASS→RESISTANCE distance: if RESISTANCE was also
    #    matched at a plausible position below MASS, take that delta.
    #
    # 3. Fallback: mass_h × 1.0 (template height). Used only when
    #    neither HUD lines nor RESISTANCE match are available.
    pitch: Optional[int] = None
    pitch_source = "fallback"

    # Source 1: HUD line pair
    try:
        gray_for_lines = (
            gray_full if gray_full is not None
            else np.array(img.convert("L"), dtype=np.uint8)
        )
        lines = _get_panel_lines_cached(gray_for_lines)
        if len(lines) >= 2:
            # Find a line pair where the top line is above MASS and
            # the bottom line is below MASS by at least 2 pitches.
            ys = sorted({ly for ly, _, _ in lines})
            top_candidates = [y for y in ys if y < mass_cy]
            bot_candidates = [y for y in ys if y > mass_cy]
            if top_candidates and bot_candidates:
                top_y_line = max(top_candidates)
                bot_y_line = min(bot_candidates)
                # Search for the line pair that brackets the largest
                # plausible data area below mass. The default-and-
                # nearest pair is the safest first guess.
                line_gap = bot_y_line - top_y_line
                # If gap is in plausible range, derive pitch.
                if 80 <= line_gap <= 600:
                    candidate_pitch = int(round(line_gap / 5.0))
                    if 15 <= candidate_pitch <= 90:
                        pitch = candidate_pitch
                        pitch_source = (
                            f"line_pair (top={top_y_line}, "
                            f"bot={bot_y_line}, gap={line_gap})"
                        )
    except Exception as _exc:
        log.debug("label_match: line-pair pitch failed: %s", _exc)

    # Source 2: observed MASS→RESISTANCE distance
    if "resistance" in matches:
        rmass_y = matches["resistance"]["y"] + matches["resistance"]["h"] // 2
        observed_pitch = rmass_y - mass_cy
        # Only trust if plausible AND consistent with line-pair pitch.
        if 15 <= observed_pitch <= 90:
            if pitch is None:
                pitch = observed_pitch
                pitch_source = f"resist_match (observed={observed_pitch})"
            elif abs(observed_pitch - pitch) < pitch * 0.20:
                # Cross-check: observed agrees with line-pair within 20%.
                # Use observed (more direct measurement).
                pitch = observed_pitch
                pitch_source = (
                    f"resist_match (observed={observed_pitch}, "
                    f"line_pair_agreed)"
                )

    # Source 3: fallback to template height
    if pitch is None:
        pitch = max(15, int(round(mass_h * 1.0)))
        pitch_source = f"fallback (mass_h={mass_h})"

    log.debug("label_match: pitch=%d (%s)", pitch, pitch_source)

    # Row half-height MUST be smaller than pitch/2 so adjacent rows
    # don't overlap (which would let RESISTANCE crop pick up the
    # INSTABILITY row below it, INSTABILITY pick up the EASY bar,
    # etc.). Use 40% of pitch — leaves a 20% gap between rows.
    half_h = max(8, int(pitch * 0.40))

    # ── Compute shared_label_right by direct per-row scan ──
    # Values are LEFT-ALIGNED to the column just past the LONGEST
    # label (INSTABILITY:). Don't infer from templates — JUST SCAN
    # EACH ROW for where its label ends in actual pixels.
    #
    # Per row:
    #   1. Crop the row strip
    #   2. Polarity-canonicalize, Otsu threshold
    #   3. Find the FIRST contiguous bright text cluster from the left
    #      (that's the label)
    #   4. The rightmost column of that cluster = the colon
    # shared_label_right = max across all rows
    def _row_label_end(row_y1: int, row_y2: int) -> Optional[int]:
        if detect_full is None:
            return None
        ry1 = max(0, row_y1)
        ry2 = min(detect_full.shape[0], row_y2)
        if ry2 - ry1 < 4:
            return None
        # Use only the LEFT 60% of the image — the value column lives
        # in the right 40% and we don't want to scan into it for the
        # label end.
        rx2 = int(detect_full.shape[1] * 0.60)
        region = detect_full[ry1:ry2, :rx2]
        if region.size == 0:
            return None
        # Polarity-canonicalize so text is BRIGHT
        thr = _otsu(region)
        bright = int((region > thr).sum())
        if (region.size - bright) < bright:
            region_canon = (255 - region).astype(np.uint8)
        else:
            region_canon = region
        thr2 = _otsu(region_canon)
        col_d = (region_canon > thr2).sum(axis=0)
        floor = max(3, int((ry2 - ry1) * 0.25))
        hot = col_d >= floor
        if not hot.any():
            return None
        # Find FIRST contiguous bright run (the label).
        idxs = np.where(hot)[0]
        first_hot = int(idxs[0])
        # Walk right from first_hot, allowing small inter-letter gaps
        # (≤ 8 px) but breaking on the BIG gap between label and value
        # (≥ 14 px expected, but anything > 10 should suffice).
        last_hot = first_hot
        gap = 0
        i = first_hot
        while i < hot.size:
            if hot[i]:
                last_hot = i
                gap = 0
            else:
                gap += 1
                if gap >= 12:
                    break
            i += 1
        return int(last_hot) + 2  # small margin for anti-alias halo

    # Predict approximate row positions from MASS anchor + pitch
    # (not yet finalized — that's done in the row_offsets loop below,
    # but we need rough y-bounds NOW to scan for label ends).
    rough_pitch = pitch
    rough_half_h = mass_h // 2 + _PAD_Y
    label_ends: list[int] = []
    for mult in (0, 1, 2):  # mass, resistance, instability rows
        cy = mass_cy + mult * rough_pitch
        end = _row_label_end(cy - rough_half_h, cy + rough_half_h)
        if end is not None:
            label_ends.append(end)

    if label_ends:
        shared_label_right = max(label_ends)
    else:
        # Fallback: MASS template's actual colon scan
        shared_label_right = _scan_actual_label_right(mass_match)

    # ── Synthesize all 4 rows from MASS anchor + fixed offsets ──
    #   _mineral_row = MASS_y - pitch
    #   mass         = MASS_y                (the anchor)
    #   resistance   = MASS_y + pitch
    #   instability  = MASS_y + 2 × pitch
    result: dict[str, tuple[int, int, int]] = {}
    row_offsets = [
        ("_mineral_row", -1),
        ("mass",          0),
        ("resistance",    1),
        ("instability",   2),
    ]
    for key, mult in row_offsets:
        cy = mass_cy + mult * pitch
        y1 = max(0, cy - half_h)
        y2 = min(img.height, cy + half_h)
        if y2 - y1 < 6:
            continue
        result[key] = (y1, y2, shared_label_right)

    log.debug(
        "label_match: rows OK (mass_y=%d-%d, resist_y=%d-%d, "
        "instab_y=%d-%d, shared_lr=%d)",
        result["mass"][0], result["mass"][1],
        result["resistance"][0], result["resistance"][1],
        result["instability"][0], result["instability"][1],
        shared_label_right,
    )

    # Telemetry for debug overlay.
    try:
        from .sc_ocr import debug_overlay as _dbg
        _dbg.set_panel_finder(
            top_y=None,
            mineral_y_top=result["_mineral_row"][0],
            mineral_y_bot=result["_mineral_row"][1],
            mineral_center=(result["_mineral_row"][0] + result["_mineral_row"][1]) // 2,
            pitch=pitch,
            bot_line_y=None,
            source="ncc_label_match",
        )
    except Exception:
        pass

    return result


# Module-level "current region" stash. The OCR pipeline (api.py
# scan_hud_onnx) sets this before calling _find_label_rows so we can
# look up persistent calibration without changing _find_label_rows'
# signature (which has dozens of callers across legacy code paths).
_current_region: Optional[dict] = None


def _set_current_region(region: Optional[dict]) -> None:
    global _current_region
    _current_region = region


# Rate-limit calibration-state logging so we don't spam the log on every
# scan. Re-log only when the region changes or the load result flips.
_calibration_log_state: dict = {"key": None, "loaded": None}


def _log_calibration_state(region: dict, cal_result) -> None:
    key = (
        int(region.get("x", 0)), int(region.get("y", 0)),
        int(region.get("w", 0)), int(region.get("h", 0)),
    )
    loaded = bool(cal_result)
    if (
        _calibration_log_state["key"] == key
        and _calibration_log_state["loaded"] == loaded
    ):
        return
    _calibration_log_state["key"] = key
    _calibration_log_state["loaded"] = loaded
    if loaded:
        log.info(
            "calibration: USING saved rows for region=%s fields=%s "
            "(detection skipped)",
            key, sorted(cal_result.keys()),
        )
    else:
        # Inspect the file to give an actionable reason.
        try:
            from .sc_ocr import calibration as _cal
            cal = _cal.load({"x": key[0], "y": key[1], "w": key[2], "h": key[3]})
            if cal is None:
                reason = "no entry for this region key"
            else:
                rows = cal.get("rows") or {}
                missing = [
                    f for f in ("mass", "resistance", "instability")
                    if f not in rows
                ]
                if not rows:
                    reason = "entry exists but rows={} (no rows locked)"
                elif missing:
                    reason = f"missing rows: {missing}"
                else:
                    reason = "rows present but to_label_rows returned None"
        except Exception as exc:
            reason = f"load failed: {exc}"
        log.warning(
            "calibration: NOT applied for region=%s — %s — falling back "
            "to live detection (boxes will move scan-to-scan)",
            key, reason,
        )


def _find_label_rows(img: Image.Image) -> dict[str, tuple[int, int, int]]:
    """Find MASS / RESIST / INSTAB rows.

    Strategy:
      0. CALIBRATION: if the user has saved per-row calibration via
         the Calibration Dialog, return those coordinates DIRECTLY.
         No detection, no drift. This is the steady-state path
         after first-time setup.
      1. NCC label template matching (auto-detect)
      2. Position-based 5-band scan
      3. HUD-grid fractional fallback
      4. Tesseract per-label search (deepest fallback)
    """
    # ── ZEROTH: persistent calibration ──
    # If the user has saved calibration for the current region, use it
    # and skip ALL detection.
    if _current_region is not None:
        try:
            from .sc_ocr import calibration as _cal
            cal_result = _cal.to_label_rows(
                _current_region, img.width, img.height, img=img,
            )
            # One-shot diagnostic: log whether calibration is loaded for
            # this region so the user can verify in the log file. The
            # rate-limiter guards against per-scan log spam.
            try:
                _log_calibration_state(_current_region, cal_result)
            except Exception:
                pass
            if cal_result:
                # Push telemetry to debug overlay
                try:
                    from .sc_ocr import debug_overlay as _dbg
                    _dbg.set_panel_finder(
                        top_y=None,
                        mineral_y_top=cal_result.get("_mineral_row", (0, 0, 0))[0],
                        mineral_y_bot=cal_result.get("_mineral_row", (0, 0, 0))[1],
                        mineral_center=None,
                        pitch=None,
                        bot_line_y=None,
                        source="calibration",
                    )
                except Exception:
                    pass
                log.debug(
                    "label_rows from calibration: %s",
                    {k: v for k, v in cal_result.items()},
                )
                return cal_result
        except Exception as exc:
            log.debug("calibration lookup failed: %s", exc)

    # ── PRIMARY: NCC label template matching ──
    # Concrete per-row pixel positions from matching the rendered
    # MASS:/RESISTANCE:/INSTABILITY: label templates against the
    # panel image. No geometry inference, no Tesseract subprocess —
    # just NumPy correlation against canonicalized templates.
    # Cached per region in the caller.
    ncc_result = _find_label_rows_by_ncc(img)
    if ncc_result:
        return ncc_result

    # ── SECONDARY: position-based finder (5-band scan from actual text) ──
    pos_result = _find_label_rows_by_position(img)
    if pos_result:
        return pos_result

    # ── TERTIARY: HUD-line-bracketed grid + fixed fractions ──
    grid_result = _find_label_rows_by_hud_grid(img)
    if grid_result:
        return grid_result

    # ── Tesseract fallback (deepest) ──
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

    # 4-character prefix matching. Shorter needles tolerate Tesseract
    # mis-reads in the label tail (e.g. 'RESI5TANCE' still matches
    # 'resi'; 'INSTABITY' still matches 'inst'). Also resolution-
    # robust — smaller render sizes lose trailing characters first,
    # but the 4-char stem ('MASS', 'RESI', 'INST') survives at any
    # panel scale where labels are even partially legible.
    targets = {
        "mass":        "mass",
        "resistance":  "resi",
        "instability": "inst",
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
            # Drop whitespace and punctuation for prefix matching — a
            # small text like 'MASS:' should hit 'mass' even though
            # the strict-lowered form is 'mass:'.
            stripped = "".join(c for c in text if c.isalpha())
            if len(stripped) < 4:
                continue
            text = stripped
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

    # ─── Anchor-based row reconciliation ───
    # The SC HUD panel has a FIXED vertical layout: MASS, then
    # RESISTANCE one row below, then INSTABILITY one row below that.
    # Per-row Tesseract searches are unreliable because:
    #   - Tesseract sometimes misreads "RESISTANCE" as containing
    #     the "mass" stem (or vice versa)
    #   - Same y-position can match multiple stems
    #   - Frame averaging across HUD jiggle blurs the row boundaries
    # Once we have ANY reliable row anchor, we can compute the others
    # from known relative pixel offsets (panel-scaled).
    #
    # Strategy:
    #   1. Pick the highest-confidence detected row as the anchor.
    #   2. Estimate row spacing from observed inter-row deltas.
    #   3. Override any detected row whose y is wildly off the
    #      expected anchor + N*row_spacing position.
    #
    # We use MASS as the preferred anchor when present (it's the
    # topmost row and most distinctive), else fall back to whichever
    # row scored highest.
    _ROW_ORDER = ["mass", "resistance", "instability"]

    # ─── Multi-anchor row reconciliation ───
    # When multiple rows are detected, use them to MEASURE the actual
    # row spacing (not assume a panel-scaled constant) and to detect
    # outlier detections that don't fit the line through the others.
    # Then clamp final positions against the HUD's top/bottom
    # separator lines (rows can never live outside the data band).

    # Step 1: Estimate row height from whatever was detected (used
    # later for crop padding).
    _heights = [best[k][1] - best[k][0] for k in best]
    raw_row_height = max(8, max(_heights) if _heights else 8)
    row_height = int(raw_row_height * 1.6) + 4

    # Step 2: Compute expected_spacing. If 2+ rows detected, use the
    # MEASURED spacing — this absorbs any panel-scale or HUD-resize
    # variation automatically. Falls back to the panel-scaled
    # constant only when a single row is all we have.
    #
    # IMPORTANT: when 3 rows are detected, use the LONGEST BASELINE
    # (idx 0 to idx 2) divided by 2, NOT the average of adjacent
    # deltas. Averaging adjacent deltas is unstable: if Tesseract
    # confuses two adjacent rows (e.g. detects MASS at RESISTANCE's
    # y), one delta collapses to ~0 and the average halves, which
    # then poisons outlier rejection. The longest-baseline spacing
    # is far less sensitive to a single noisy detection because the
    # bad row contributes only one error to a much larger interval.
    _REF_PANEL_W = 397
    panel_scale = max(0.5, float(img.width) / _REF_PANEL_W)
    _const_spacing = max(raw_row_height + 8, int(30 * panel_scale))

    detected_pairs = sorted(
        (_ROW_ORDER.index(k), best[k][0])
        for k in best
        if k in _ROW_ORDER
    )  # [(idx, y), ...] sorted by row index

    if len(detected_pairs) >= 2:
        # Use the longest baseline for the most robust spacing.
        first_idx, first_y = detected_pairs[0]
        last_idx, last_y = detected_pairs[-1]
        idx_span = max(1, last_idx - first_idx)
        measured_spacing = int(round((last_y - first_y) / idx_span))
        # Sanity-bound against the panel-scaled constant.
        if 0.4 * _const_spacing <= measured_spacing <= 2.0 * _const_spacing:
            expected_spacing = measured_spacing
        else:
            expected_spacing = _const_spacing
    else:
        expected_spacing = _const_spacing

    # Step 3: Outlier rejection. Use the longest-baseline spacing
    # (computed above) and the FIRST AND LAST detected rows as the
    # reference line, then drop any middle row that doesn't fit.
    # This is more robust than median-pivot because the endpoints
    # define the longest baseline; a noisy middle row can't poison
    # the line.
    if len(detected_pairs) == 3:
        first_idx, first_y = detected_pairs[0]
        last_idx, last_y = detected_pairs[-1]
        for idx, y in detected_pairs:
            if idx in (first_idx, last_idx):
                continue
            predicted = first_y + (idx - first_idx) * expected_spacing
            if abs(y - predicted) > expected_spacing * 0.5:
                _outlier_key = _ROW_ORDER[idx]
                log.debug(
                    "onnx_hud_reader: dropping outlier middle row %s (y=%d, "
                    "predicted=%d, spacing=%d)",
                    _outlier_key, y, predicted, expected_spacing,
                )
                best.pop(_outlier_key, None)
        # Also check: if the FIRST and LAST themselves are
        # implausibly close (idx_span * spacing collapsed because
        # one of them was misdetected at the same y as the other),
        # reject the smaller-scoring of the pair.
        if abs(last_y - first_y) < expected_spacing * (last_idx - first_idx) * 0.5:
            _f_score = best[_ROW_ORDER[first_idx]][3] if _ROW_ORDER[first_idx] in best else 0
            _l_score = best[_ROW_ORDER[last_idx]][3] if _ROW_ORDER[last_idx] in best else 0
            _drop_idx = first_idx if _f_score < _l_score else last_idx
            _drop_key = _ROW_ORDER[_drop_idx]
            log.debug(
                "onnx_hud_reader: endpoints collapsed (first_y=%d last_y=%d "
                "span=%d, expected≈%d); dropping lower-score endpoint %s",
                first_y, last_y, last_idx - first_idx,
                (last_idx - first_idx) * expected_spacing, _drop_key,
            )
            best.pop(_drop_key, None)

    # Step 4: Pick the anchor (prefer MASS, else highest-score row).
    if "mass" in best:
        anchor_key = "mass"
    elif best:
        anchor_key = max(best, key=lambda k: best[k][3])
    else:
        # All rows were rejected as outliers — return empty so the
        # caller falls back to mineral-row offset estimation.
        log.debug("onnx_hud_reader: all rows rejected as outliers")
        return {}

    anchor_y, anchor_y2, anchor_left, _ = best[anchor_key]
    anchor_idx = _ROW_ORDER.index(anchor_key)

    # Step 5: HUD-line Y bounds. The two horizontal HUD separator
    # lines bracket the data area. Any row Y outside that band is
    # provably wrong; clamp the anchor before we propagate it.
    _lines = _get_panel_lines_cached(np.array(img.convert("L"), dtype=np.uint8))
    _y_min_bound = 0
    _y_max_bound = img.height
    if len(_lines) >= 2:
        # Top line = first line above the anchor; bottom line = first
        # line below the anchor's last expected row.
        _last_expected_y = anchor_y + (len(_ROW_ORDER) - 1 - anchor_idx) * expected_spacing
        _above = [ly for ly, _, _ in _lines if ly < anchor_y]
        _below = [ly for ly, _, _ in _lines if ly > _last_expected_y]
        if _above:
            _y_min_bound = max(_above)  # closest line above anchor
        if _below:
            _y_max_bound = min(_below)  # closest line below last row

    _Y_PAD_TOP = 4
    for idx, key in enumerate(_ROW_ORDER):
        expected_y = anchor_y + (idx - anchor_idx) * expected_spacing
        # Clamp expected_y inside the HUD-line band (with padding for
        # row height — the row's TOP must be far enough below the top
        # line that the row's BOTTOM doesn't push past the bottom line).
        if expected_y < _y_min_bound:
            expected_y = _y_min_bound + _Y_PAD_TOP
        if expected_y + row_height > _y_max_bound:
            expected_y = _y_max_bound - row_height
        if 0 <= expected_y < img.height - row_height:
            y_top = max(0, expected_y - _Y_PAD_TOP)
            y_bot = expected_y + row_height
            if key in best:
                detected_y = best[key][0]
                if abs(detected_y - expected_y) > expected_spacing * 0.6:
                    best[key] = (
                        y_top,
                        y_bot,
                        best[key][2],
                        best[key][3],
                    )
            else:
                best[key] = (
                    y_top,
                    y_bot,
                    anchor_left,
                    1,
                )

    # Compute real label right edges via column-density on the
    # polarity-independent text mask of the full image. If the mask
    # is too noisy (e.g. asteroid leak), fall back to a fixed
    # right-edge estimate based on label length.
    full_gray = np.array(img.convert("L"), dtype=np.uint8)
    text_mask = _build_text_mask(full_gray, deviation=30)

    result: dict[str, tuple[int, int, int]] = {}
    _PAD = 3
    # Walk-right gap tolerance: inter-letter gaps in SC's HUD font can
    # exceed 5 px (especially at small panel scales), causing the scan
    # to terminate mid-label. Bumped 5 -> 14 so the scan bridges
    # intra-label gaps but still detects the much larger 30-50 px gap
    # between the label's trailing colon and the value's first digit.
    _GAP_THRESHOLD = 14
    # Fixed fallback right edges — from known panel geometry
    _FALLBACK_RIGHTS = {"mass": 110, "resistance": 200, "instability": 205}

    # Panel width heuristic — the hardcoded fallback rights were
    # measured on a 397px-wide reference panel. Scale them if the
    # current panel is wider/narrower. ``left`` was cropped at 55% of
    # img.width so the label column is always in the left half.
    _REF_PANEL_W = 397
    panel_scale = max(0.5, float(img.width) / _REF_PANEL_W)

    for key, (y1, y2, lbl_left, _score) in best.items():
        # Scan hot columns in this row to find the label right edge.
        # The label is darkest immediately after ``lbl_left`` and
        # fades into the gap between label and value. Walk rightward
        # tolerating small gaps inside the label glyphs.
        col_hot = text_mask[y1:y2, :].sum(axis=0) >= 2
        scanned_right = lbl_left
        gap_run = 0
        x = lbl_left
        while x < col_hot.shape[0]:
            if col_hot[x]:
                scanned_right = x + 1
                gap_run = 0
            else:
                gap_run += 1
                if gap_run >= _GAP_THRESHOLD:
                    break
            x += 1

        # Use the scanned edge when it's plausibly past the label —
        # require at least 20 px of label extent. Reject the scan if
        # it ran clear across the row (text_mask bleed from asteroid
        # scene), which we detect by comparing against a scaled cap.
        fallback_right = int(_FALLBACK_RIGHTS[key] * panel_scale)
        scan_extent = scanned_right - lbl_left
        max_plausible = int(min(img.width * 0.45, fallback_right * 1.8))
        if 20 <= scan_extent and scanned_right <= max_plausible:
            lbl_right = scanned_right
        else:
            lbl_right = fallback_right
            log.debug(
                "sc_ocr: label_rows key=%s scan_extent=%d out of "
                "bounds, using scaled fallback=%d (panel_scale=%.2f)",
                key, scan_extent, fallback_right, panel_scale,
            )

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

    # ── SC-OCR ENGINE (primary, legacy disabled) ──
    try:
        from .sc_ocr.api import scan_hud_onnx as _sc_ocr_scan
        sc_result = _sc_ocr_scan(region)
        elapsed = (time.time() - t0) * 1000
        log.info(
            "sc_ocr: mass=%s resistance=%s instability=%s in %.0fms",
            sc_result.get("mass"), sc_result.get("resistance"),
            sc_result.get("instability"), elapsed,
        )
        return sc_result
    except Exception as exc:
        # Include the full traceback so we can locate the actual line
        # that's raising — the bare ``%s`` was masking 1000+ identical
        # ``KeyError: 'instability'`` failures with no line info.
        log.error("sc_ocr failed: %s", exc, exc_info=True)
        return result

    # ── LEGACY PIPELINE (disabled — kept for reference) ──
    # To re-enable: remove the 'return' above and uncomment below.
    if False:
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
    # Bumped 6 -> 14 so the trailing colon and any anti-alias halo of
    # "RESISTANCE:"/"INSTABILITY:" don't leak into the value crop and
    # produce phantom leading digits in front of "0%" etc.
    _LABEL_GAP = 14

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
