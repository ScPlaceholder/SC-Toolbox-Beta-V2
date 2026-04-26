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
import os
import time
from typing import Optional

import numpy as np
from PIL import Image

from . import capture, fallback, preprocess, validate

log = logging.getLogger(__name__)

# One-shot debug save per field per process session — first successful
# value crop for each of mass/resistance/instability gets saved to
# debug_value_<field>_crop.png at the repo root so we can see what the
# OCR is actually receiving when reads go wrong.
_SAVED_DEBUG: dict[str, bool] = {}

# Consensus buffer: last 5 raw reads per field. Scans are ~1 Hz so
# 5 entries cover ~5 seconds. The display layer uses 2-of-3 in the
# rolling window to suppress single-frame noise; the LOCK layer
# (further down) requires stricter agreement across the full
# window to avoid locking in garbage.
from collections import deque as _deque
_LOCK_WINDOW = 5  # frames considered for lock decision
_RECENT_READS: dict[str, _deque] = {
    "mass": _deque(maxlen=_LOCK_WINDOW),
    "resistance": _deque(maxlen=_LOCK_WINDOW),
    "instability": _deque(maxlen=_LOCK_WINDOW),
}
# Parallel buffer of downsampled crop images per field. Used by the
# pre-lock verifier to confirm that the CROP itself is stable
# (i.e. the row isn't jumping between a digit row and an unrelated
# UI element like the difficulty progress bar). If the OCR text is
# consistent but the underlying pixels are wildly different, that's
# almost certainly a "trash crop" coincidence and we refuse to lock.
_RECENT_CROPS: dict[str, _deque] = {
    "mass": _deque(maxlen=_LOCK_WINDOW),
    "resistance": _deque(maxlen=_LOCK_WINDOW),
    "instability": _deque(maxlen=_LOCK_WINDOW),
}
# Crop fingerprint size — downsample each crop to this resolution
# before storing, so pairwise comparison is O(N²×64) per field
# instead of O(N²×W×H). 8×24 keeps enough horizontal detail to
# distinguish 4-digit vs 1-digit reads, color stripes, etc.
_CROP_FP_W = 24
_CROP_FP_H = 8
# Required agreement across the full window before locking:
#   * all _LOCK_WINDOW reads must produce the same value
#   * mean pairwise crop similarity (NCC) ≥ this threshold
_LOCK_VALUE_AGREEMENT = _LOCK_WINDOW   # all-of-N (strictest)
_LOCK_CROP_NCC_MIN = 0.85
# Last displayed (stabilized) value per field. When a scan produces
# a one-off outlier we stick with this until the buffer confirms a
# new value.
_STABLE_VALUE: dict[str, Optional[float]] = {
    "mass": None, "resistance": None, "instability": None,
}

# ──────────────────────────────────────────
# Signal (signature scanner) consensus
# ──────────────────────────────────────────
# Mining HUD jitter (~1-3 px subpixel animation at ~3 Hz) makes per-frame
# Tesseract reads on the signal cluster swing between adjacent values,
# e.g. 17,020 ↔ 17,011 across consecutive frames even though the rock's
# true signature is constant. We dampen this with a small rolling buffer
# of the last N raw reads and require K-of-N agreement before swapping
# the displayed value.
_SIGNAL_BUFFER_LEN = 5            # remember last 5 raw reads
_SIGNAL_AGREEMENT_REQ = 2         # require 2 agreeing reads before swap
_RECENT_SIGNAL_READS: _deque = _deque(maxlen=_SIGNAL_BUFFER_LEN)
_STABLE_SIGNAL: Optional[int] = None

# Known-signature value set, populated from the mining chart data via
# ``set_known_signal_values()``. Used as a tie-breaker AND as a sanity
# floor in the variant voter: if ANY variant's read exact-matches a
# known signature value, we strongly prefer it over arbitrary in-range
# numbers. Empty set = no preference applied (fail-open behaviour).
_KNOWN_SIGNAL_VALUES: set[int] = set()


def set_known_signal_values(values) -> None:
    """Register the set of all valid signature values from the mining
    chart. Called from ``ui/app.py:_on_data_loaded`` after the
    chart rows are loaded.

    The voter uses this set as a tie-breaker: if among the 6 PSM ×
    scale Tesseract variants two produce ``17020`` (a known Silicon
    × 4-rocks value) and one produces ``17011`` (not in any known
    table), the voter returns ``17020`` even before majority is
    reached. This kills the dominant flicker pattern outright.
    """
    global _KNOWN_SIGNAL_VALUES
    _KNOWN_SIGNAL_VALUES = {int(v) for v in values if v}


def _reset_signal_consensus() -> None:
    """Clear the signal consensus buffer. Call when the user changes
    rocks or the signature panel disappears."""
    global _STABLE_SIGNAL
    _RECENT_SIGNAL_READS.clear()
    _STABLE_SIGNAL = None


def _reset_consensus_buffers() -> None:
    """Clear all consensus buffers. Called when the panel disappears
    (user stopped looking at a scan result) so the next rock's reads
    aren't contaminated by the previous rock's values."""
    for b in _RECENT_READS.values():
        b.clear()
    for b in _RECENT_CROPS.values():
        b.clear()
    for k in _STABLE_VALUE:
        _STABLE_VALUE[k] = None
    _field_lock_cache.clear()
    # Also drop the signal consensus — same lifecycle. If the user
    # looked away from the rock, the next rock starts fresh.
    _reset_signal_consensus()


def _crop_fingerprint(value_crop: "Image.Image") -> Optional[np.ndarray]:
    """Downsample a value crop to a fixed (_CROP_FP_H × _CROP_FP_W)
    grayscale fingerprint for pairwise similarity comparison.

    Returns a zero-mean unit-variance float32 array (NCC-ready), or
    None if the input is degenerate.
    """
    try:
        gray = value_crop.convert("L").resize(
            (_CROP_FP_W, _CROP_FP_H), Image.BILINEAR,
        )
        arr = np.asarray(gray, dtype=np.float32).ravel()
        std = float(arr.std())
        if std < 1e-3:
            return None
        return (arr - float(arr.mean())) / std
    except Exception:
        return None


def _crop_buffer_consistent(field: str) -> tuple[bool, float]:
    """Return (is_consistent, mean_pairwise_NCC) for the field's crop buffer.

    A buffer is consistent when its frames all look like the same
    underlying scene — i.e. the row crop has been STABLE across the
    window. If the row was jumping (digits one frame, progress bar
    the next), pairwise NCC will be low and we refuse to lock.
    """
    fps = [fp for fp in _RECENT_CROPS[field] if fp is not None]
    if len(fps) < _LOCK_WINDOW:
        return False, 0.0
    n = len(fps[0])
    sims = []
    for i in range(len(fps)):
        for j in range(i + 1, len(fps)):
            sims.append(float(np.dot(fps[i], fps[j]) / n))
    if not sims:
        return False, 0.0
    mean_sim = sum(sims) / len(sims)
    return mean_sim >= _LOCK_CROP_NCC_MIN, mean_sim


def _value_buffer_unanimous(field: str) -> Optional[float]:
    """Return the unanimous value if the buffer is full and all frames
    agree, else None. Stricter than _consensus_value's 2-of-3 rule —
    used as the lock gate."""
    buf = _RECENT_READS.get(field)
    if buf is None or len(buf) < _LOCK_WINDOW:
        return None
    vals = [round(float(v), 4) for v in buf if v is not None]
    if len(vals) < _LOCK_WINDOW:
        return None
    if len(set(vals)) == 1:
        return vals[0]
    return None


# ── Field-value lock cache ─────────────────────────────────────────
#
# Once a field reads a value that PASSES VALIDATION (and has high
# enough CNN confidence), we lock it for the duration the panel
# remains visible. Subsequent scans for the same panel skip the OCR
# work entirely for that field and return the locked value. The
# cache is cleared the moment the panel disappears (mineral row
# undetectable) — i.e. when the user looks away from the rock.
#
# Why locking is necessary even with consensus: the consensus buffer
# requires 2-of-3 agreement to display a value, which means a single
# good frame surrounded by misreads will still show garbage. Locking
# treats the FIRST validated read as truth, and only re-evaluates
# when the panel goes away (rock changed).
#
# Keyed by region (x, y, w, h) so multiple scan regions don't share
# cache state (e.g. two Mining Signals instances).
#
# Each entry stores BOTH the locked value AND the crop fingerprint
# that was in effect when the lock fired. On every subsequent scan
# we compare the current frame's crop fingerprint against the
# stored one; a significant divergence drops the lock and resumes
# OCR. This prevents a wrong locked value from persisting silently
# if the row geometry drifts after locking (e.g. ship moves and the
# panel re-anchors slightly differently).
_field_lock_cache: dict[
    tuple[int, int, int, int],
    dict[str, tuple[float, np.ndarray]],
] = {}
# Threshold: if current crop NCC vs stored fingerprint < this, the
# lock is invalidated. Lower than the lock-acquisition threshold
# (0.85) so transient noise doesn't immediately drop a good lock.
_LOCK_INVALIDATE_NCC = 0.65


def _region_key(region: dict) -> tuple[int, int, int, int]:
    return (
        int(region.get("x", 0)),
        int(region.get("y", 0)),
        int(region.get("w", 0)),
        int(region.get("h", 0)),
    )


def _consensus_value(field: str, new_value: Optional[float]) -> Optional[float]:
    """Sticky consensus: return last stable value unless a new value
    appears 2+ times in the rolling 3-read buffer.

    Behaviour:
      * None input → return last stable (don't corrupt buffer).
      * New value that matches an existing buffer entry → counts go up.
      * If most-frequent value has >= 2 occurrences → that becomes the
        new stable value and is returned.
      * Otherwise → return the previously-displayed stable value
        (outlier suppressed).

    First-ever non-None read: return it immediately (no history to
    stick to).
    """
    buf = _RECENT_READS.get(field)
    if buf is None:
        return new_value
    if new_value is None:
        return _STABLE_VALUE.get(field)

    buf.append(new_value)
    counts: dict[float, int] = {}
    for v in buf:
        if v is None:
            continue
        key = round(float(v), 4)
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return new_value

    best_key, best_n = max(counts.items(), key=lambda kv: kv[1])
    if best_n >= 2:
        # Confirmed — update stable value and return it
        _STABLE_VALUE[field] = best_key
        return best_key

    # No 2-agreement yet — prefer the last stable value if we have one,
    # else accept the new read as provisional
    last = _STABLE_VALUE.get(field)
    if last is not None:
        return last
    _STABLE_VALUE[field] = new_value
    return new_value

# Reuse proven legacy helpers that are pure NumPy (no Tesseract dep)
# Now that _build_text_mask is polarity-aware, _find_mineral_row and
# _find_value_crop both work on light AND dark backgrounds.
from ..onnx_hud_reader import (  # noqa: E402
    _find_mineral_row,
    _find_value_crop,
    _otsu,
)


def _canonicalize_polarity(gray: np.ndarray) -> np.ndarray:
    """Force the image to bright-text-on-dark-background (model's training polarity).

    The HUD ships values in many colors (white, yellow, cyan, green,
    red) over many backgrounds (black space, bright sky, dim cloud
    gradient, snowy asteroid). Background-median heuristics like
    ``median > 140 → invert`` get fooled by:
      - Bright sky backgrounds where the sky pixels dominate the
        median even though the text is BRIGHTER than the sky.
      - Mixed crops where a UI element occupies more area than the
        text itself.

    Minority-class rule (background-agnostic):
      1. Run Otsu on the grayscale image — splits pixels into the
         two cleanest groups.
      2. Whichever group has FEWER pixels is the foreground (text).
         Text is always a small fraction of any reasonable crop;
         backgrounds always dominate by area.
      3. If the minority group is DARK, invert so the text ends up
         BRIGHT (on dark background). The model was trained on
         bright-text-on-dark-bg crops surrounded by white padding;
         we preserve that convention here so downstream segmentation
         and the CNN see what they expect.

    Returns the polarity-normalized grayscale (uint8).
    """
    if gray.size == 0:
        return gray
    thr = _otsu(gray)
    bright_count = int((gray > thr).sum())
    dark_count = int((gray <= thr).sum())
    # Minority class = text. We want text BRIGHT (matches training
    # data). If the minority class is already bright, leave alone.
    # If the minority class is dark, invert to bring it bright.
    if dark_count < bright_count:
        return (255 - gray).astype(np.uint8)
    return gray


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

# HUD geometry ratios (fraction of panel HEIGHT from mineral-row center
# to each value row). Measured from the 397x541 test fixture and
# verified to scale proportionally across panel sizes.
_ROW_HEIGHT_HALF_RATIO = 0.028  # ±15/541
_OFFSET_RATIOS = {"mass": 0.079, "resistance": 0.152, "instability": 0.222}
# Label right-edge ratios (fraction of panel WIDTH)
_LABEL_RIGHT_RATIOS = {"mass": 0.277, "resistance": 0.504, "instability": 0.516}


# ── Glyph extraction + ONNX classification ────────────────────────

def _segment_glyphs(gray: np.ndarray, binary: np.ndarray) -> list[np.ndarray]:
    """Segment individual glyphs and return 28x28 float32 crops in [0,1].

    Replicates the EXACT preprocessing the ONNX model was trained on:
      glyph from grayscale → pad with 255 → resize 28x28 → / 255.

    Right-anchored: drops spans before the largest gap between
    consecutive characters. The largest gap usually marks where label-
    text intrusion (e.g. the trailing colon of "RESISTANCE:") ends and
    the actual value begins.
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
            # Min span width 2 (was 3) so narrow chars like '1' and '.'
            # aren't dropped — matches the offline extractor that
            # produced the high-accuracy training data.
            if x - start >= 2:
                spans.append((start, x))

    # Right-anchored span filter: SC HUD values are read right-to-left
    # and the LEFT edge is where label-text intrusion shows up
    # (e.g. trailing colon of "RESISTANCE:" leaking in front of "0%").

    # Helper: a leading "narrow" span looks like a real digit `1` if
    # it's TALL relative to its width. The SC font's `1` glyph has
    # aspect ratio (height/width) of roughly 2.5-3.5, while halo
    # dots / chromatic aberration / colon residue are roughly square
    # (ratio ~1.0-1.3) or wider. Without this guard, the width-based
    # "leading-narrow drop" eats real `1`s every time the value
    # starts with one — turning 14156 into 4156, 11565 into 1565,
    # etc.
    def _looks_like_one(span_idx: int) -> bool:
        if not (0 <= span_idx < len(spans)):
            return False
        s, e = spans[span_idx]
        w_px = max(1, e - s)
        ys = np.where(np.any(binary[:, s:e] > 0, axis=1))[0]
        if ys.size == 0:
            return False
        h_px = int(ys[-1] - ys[0] + 1)
        return (h_px / w_px) >= 2.0

    if len(spans) >= 2:
        # (1) Drop ANY leading span whose width OR HEIGHT is small
        # relative to the median real digit. Catches colons, halo,
        # chromatic-aberration dots — but NOT a real leading `1`,
        # protected by the aspect-ratio guard.
        if len(spans) >= 3:
            widths = sorted(e - s for s, e in spans)
            median_w = widths[len(widths) // 2]
            # Width threshold raised 70%->80% to catch wider artifacts.
            min_real_width = max(4, int(median_w * 0.80))
            while spans and (spans[0][1] - spans[0][0]) < min_real_width:
                if _looks_like_one(0):
                    break  # real `1`, leave alone
                spans.pop(0)
        elif len(spans) == 2:
            w1 = spans[0][1] - spans[0][0]
            w2 = spans[1][1] - spans[1][0]
            # Drop leading if it's noticeably narrower than the next
            # AND not tall enough to be a `1`.
            if w1 < w2 * 0.6 and not _looks_like_one(0):
                spans = spans[1:]

        # (2) Find the largest gap between adjacent spans; discard
        # everything LEFT of it (label-to-value boundary).
        # Tightened so even modestly-larger gaps trigger the cut —
        # inter-digit gaps in SC HUD font are very uniform.
        if len(spans) >= 2:
            gaps = [(spans[i + 1][0] - spans[i][1], i) for i in range(len(spans) - 1)]
            largest_gap, gap_idx = max(gaps, key=lambda g: g[0])
            sorted_gaps = sorted(g for g, _ in gaps)
            median_gap = sorted_gaps[len(sorted_gaps) // 2]
            # gap >1.4× median (was 1.6) OR >8px absolute (was 12)
            if largest_gap >= max(8, int(median_gap * 1.4 + 1)):
                spans = spans[gap_idx + 1:]

        # (3) After the gap-cut, if the new leading span is STILL
        # disproportionately narrow vs the rest, drop it once more —
        # but again only if it doesn't look like a real `1`.
        if len(spans) >= 3:
            widths = [e - s for s, e in spans[1:]]
            avg_real = sum(widths) / len(widths)
            if (spans[0][1] - spans[0][0]) < avg_real * 0.65 and not _looks_like_one(0):
                spans = spans[1:]

    crops: list[np.ndarray] = []
    for x1, x2 in spans:
        ys = np.where(np.any(binary[:, x1:x2] > 0, axis=1))[0]
        # Min 1 active row keeps narrow `.` glyphs (height 1-2 px after
        # binarization) from being silently dropped. Dropping the dot
        # turns "2.78" into "278" and downstream decimal-recovery can
        # then place the dot at the wrong position (e.g. "27.80").
        if len(ys) < 1:
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


def _crnn_decode(
    session, classes: str, blank: int,
    value_crop: Image.Image, h_target: int,
) -> Optional[tuple[str, list[float]]]:
    """Run a single CRNN at a single scale and greedy-decode.

    Shared body for both the primary and optional v2 CRNN; caller
    passes the session + vocabulary + blank index to use.
    """
    gray = np.array(value_crop.convert("L"), dtype=np.uint8)
    if gray.size == 0:
        return None

    if float(np.median(gray)) > 140:
        gray = 255 - gray

    H, W = gray.shape
    w_new = max(16, int(round(W * h_target / max(1, H))))
    resized = np.array(
        Image.fromarray(gray).resize((w_new, h_target), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0

    inp = resized.reshape(1, 1, h_target, w_new).astype(np.float32)
    try:
        inp_name = session.get_inputs()[0].name
        logits = session.run(None, {inp_name: inp})[0]
    except Exception as exc:
        log.debug("sc_ocr: CRNN inference failed at h=%d: %s", h_target, exc)
        return None

    if logits.ndim == 3:
        logits_tc = logits[:, 0, :]
    elif logits.ndim == 2:
        logits_tc = logits
    else:
        return None

    shifted = logits_tc - logits_tc.max(axis=-1, keepdims=True)
    probs = np.exp(shifted)
    probs /= probs.sum(axis=-1, keepdims=True)
    preds = probs.argmax(axis=-1)
    confs = probs.max(axis=-1)

    text_chars: list[str] = []
    per_char: list[float] = []
    prev = -1
    for t in range(len(preds)):
        p = int(preds[t])
        if p != prev and p != blank and 0 <= p < len(classes):
            text_chars.append(classes[p])
            per_char.append(float(confs[t]))
        prev = p
    return "".join(text_chars), per_char


def _crnn_recognize_single(
    value_crop: Image.Image, h_target: int,
) -> Optional[tuple[str, list[float]]]:
    """Primary-CRNN single-scale pass. Kept as a thin wrapper over
    ``_crnn_decode`` so callers that only want the primary model
    don't need to know about the ensemble partner."""
    if not fallback._ensure_crnn_model():
        return None
    return _crnn_decode(
        fallback._crnn_session,
        fallback._crnn_classes,
        fallback._crnn_blank_idx,
        value_crop, h_target,
    )


def _crnn_recognize(value_crop: Image.Image) -> Optional[tuple[str, list[float]]]:
    """End-to-end CRNN read with multi-scale + multi-model ensembling.

    Runs inference at 3 different input heights (base−8, base, base+16)
    on EACH available CRNN (primary + optional v2 partner) and picks
    the read with the highest mean confidence across all candidates.

    Two-model ensemble is the default when ``model_crnn_v2.onnx``
    exists on disk; otherwise falls back to single-model multi-scale.
    The v2 partner is expected to have been trained with different
    init / augmentation than the primary so their errors decorrelate.
    """
    if not fallback._ensure_crnn_model():
        return None

    base_h = int(fallback._crnn_input_height)
    # The shipped ONNX models were exported with height FIXED (only
    # batch + width are dynamic axes), so off-scale probes at
    # base_h-8 / base_h+16 fail with INVALID_ARGUMENT and are silently
    # dropped. Restrict to base_h to stop the wasted inference calls
    # and log noise. A future retrain with dynamic height could re-
    # introduce the multi-scale ensemble; guard via model metadata.
    scales = [base_h]

    # Assemble the list of (session, classes, blank, tag) pairs. Each
    # is a complete recognizer; we probe each at each scale.
    recognizers: list[tuple[object, str, int, str]] = [
        (fallback._crnn_session, fallback._crnn_classes,
         fallback._crnn_blank_idx, "v1"),
    ]
    if fallback._ensure_crnn2_model():
        recognizers.append((
            fallback._crnn2_session, fallback._crnn2_classes,
            fallback._crnn2_blank_idx, "v2",
        ))

    candidates: list[tuple[str, list[float], str, int]] = []  # +tag, +scale
    for sess, classes, blank, tag in recognizers:
        for h in scales:
            r = _crnn_decode(sess, classes, blank, value_crop, h)
            if r is not None and r[0]:
                candidates.append((r[0], r[1], tag, h))

    if not candidates:
        # Every probe returned empty — fall back to a single primary
        # pass at base height (may still return empty; caller handles).
        r = _crnn_recognize_single(value_crop, base_h)
        return r

    # Rank by mean confidence, ties broken by length (more preserved
    # chars ⇒ CTC collapsed correctly at that scale).
    def _score(item):
        _text, confs, _tag, _h = item
        mean = sum(confs) / len(confs) if confs else 0.0
        return (mean, len(_text))
    candidates.sort(key=_score, reverse=True)
    winner = candidates[0]
    # Audit logging only when >1 recognizer actually ran, to keep the
    # single-model case quiet.
    if len(recognizers) > 1:
        _wtxt, _wconfs, _wtag, _wh = winner
        _wmean = sum(_wconfs) / len(_wconfs) if _wconfs else 0.0
        log.debug(
            "sc_ocr: crnn-ensemble winner=%s@h%d text=%r mean=%.2f "
            "(ncand=%d)", _wtag, _wh, _wtxt, _wmean, len(candidates),
        )
    return winner[0], winner[1]


def _crnn_recognize_old_UNUSED(value_crop: Image.Image) -> Optional[tuple[str, list[float]]]:
    """Unused — kept temporarily so the file still parses during
    refactor. The active entry point is ``_crnn_recognize`` above.
    This function is dead code; the body below is left intact but
    unreachable to avoid accidental git-hunk mixups with the
    refactor. Delete in a follow-up commit.
    """
    if False:
        pass
    if not fallback._ensure_crnn_model():
        return None

    gray = np.array(value_crop.convert("L"), dtype=np.uint8)
    if gray.size == 0:
        return None

    if float(np.median(gray)) > 140:
        gray = 255 - gray

    H, W = gray.shape
    h_target = int(fallback._crnn_input_height)
    w_new = max(16, int(round(W * h_target / max(1, H))))
    resized = np.array(
        Image.fromarray(gray).resize((w_new, h_target), Image.BILINEAR),
        dtype=np.float32,
    ) / 255.0

    inp = resized.reshape(1, 1, h_target, w_new).astype(np.float32)
    try:
        session = fallback._crnn_session
        inp_name = session.get_inputs()[0].name
        logits = session.run(None, {inp_name: inp})[0]
    except Exception as exc:
        log.debug("sc_ocr: CRNN inference failed: %s", exc)
        return None

    if logits.ndim == 3:
        logits_tc = logits[:, 0, :]
    elif logits.ndim == 2:
        logits_tc = logits
    else:
        log.debug("sc_ocr: unexpected CRNN logits shape %s", logits.shape)
        return None

    shifted = logits_tc - logits_tc.max(axis=-1, keepdims=True)
    probs = np.exp(shifted)
    probs /= probs.sum(axis=-1, keepdims=True)
    preds = probs.argmax(axis=-1)
    confs = probs.max(axis=-1)

    classes = fallback._crnn_classes
    blank = fallback._crnn_blank_idx
    text_chars: list[str] = []
    per_char: list[float] = []
    prev = -1
    for t in range(len(preds)):
        p = int(preds[t])
        if p != prev and p != blank and 0 <= p < len(classes):
            text_chars.append(classes[p])
            per_char.append(float(confs[t]))
        prev = p
    return "".join(text_chars), per_char


def _try_tesseract_eng_sc(value_crop: Image.Image) -> str:
    """Primary Tesseract read using eng_sc + 3x+ upscale.

    Extracted from the main body of ``_ocr_value_crop`` so it can be
    called ahead of CRNN for digit-only fields. Returns "" on any
    failure (eng_sc model missing, pytesseract not installed, crop
    dimensions invalid, etc.).
    """
    try:
        import pytesseract
        from ..screen_reader import _check_tesseract
        _check_tesseract()
    except Exception:
        return ""

    _tessdata_local = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "ocr", "tessdata",
    )
    _have_sc = os.path.isfile(os.path.join(_tessdata_local, "eng_sc.traineddata"))
    if not _have_sc:
        return ""

    W, H = value_crop.size
    shortest = min(W, H)
    if shortest < 80:
        scale = max(3, 100 // max(1, shortest))
        tess_input = value_crop.resize((W * scale, H * scale), Image.LANCZOS)
    else:
        tess_input = value_crop

    # Pad with a polarity-appropriate border. Tesseract's PSM 7/8 both
    # need "quiet space" around the text to lock onto a baseline; a
    # tightly-cropped number sometimes returns empty when the text
    # touches the image edge. Use the image's corner pixel as the fill
    # color to keep polarity consistent with the crop.
    _pad = max(16, tess_input.height // 4)
    _corner = tess_input.getpixel((0, 0))
    if isinstance(_corner, tuple):
        _bg = _corner
    else:
        _bg = _corner  # grayscale int
    try:
        from PIL import ImageOps as _ImageOps
        tess_input = _ImageOps.expand(tess_input, border=_pad, fill=_bg)
    except Exception:
        pass

    prev_env = os.environ.get("TESSDATA_PREFIX")
    os.environ["TESSDATA_PREFIX"] = _tessdata_local

    def _run(psm: int) -> str:
        try:
            return pytesseract.image_to_string(
                tess_input,
                config=(
                    f"-l eng_sc --psm {psm} "
                    "-c tessedit_char_whitelist=0123456789.%"
                ),
            ).strip()
        except Exception:
            return ""

    try:
        # PSM 7 first (single text line, best when the crop is already
        # line-shaped). Fall back to PSM 8 (single word) which is more
        # forgiving when PSM 7 can't find a baseline.
        text = _run(7)
        if not text:
            text = _run(8)
    finally:
        if prev_env is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = prev_env
    return text


def _parallel_vote(
    field: str,
    crnn_text: str,
    crnn_confs: list[float],
    tess_text: str,
) -> Optional[tuple[str, list[float]]]:
    """Field-aware voter between a CRNN read and a Tesseract read.

    Both engines run every scan (not a cascade) and their outputs are
    reconciled here. Returns the winning (text, confs) pair or None
    to indicate no confident agreement (caller should fall through to
    the ONNX segmenter).

    Decision rules (in order):
      1. Both empty → None (caller falls through).
      2. Only one produced text → use that one.
      3. Texts are identical after stripping non-digit/./% → return
         that text with CRNN's confidences (or fabricated 0.95 if
         only Tesseract spoke).
      4. Disagree: apply field-specific validity filters. For
         percentages anything > 100 is invalid; for instability,
         > 200 is suspicious. The read that passes its field's
         sanity check wins. If both pass (or neither passes), prefer
         CRNN when mean conf ≥ 0.80 else prefer Tesseract (eng_sc
         is generally more stable on digit-only HUD text at small
         sizes).
    """
    def _digits_only(s: str) -> str:
        return "".join(c for c in s if c in "0123456789.%")

    c_norm = _digits_only(crnn_text or "")
    t_norm = _digits_only(tess_text or "")

    if not c_norm and not t_norm:
        return None
    if c_norm and not t_norm:
        return c_norm, crnn_confs or [0.85] * len(c_norm)
    if t_norm and not c_norm:
        return t_norm, [0.9] * len(t_norm)
    if c_norm == t_norm:
        return c_norm, crnn_confs or [0.95] * len(c_norm)

    # Disagreement — apply field sanity checks.
    def _field_ok(s: str) -> bool:
        try:
            if field == "resistance":
                v = float(s.replace("%", "")) if s else -1.0
                return 0.0 <= v <= 100.0
            if field == "instability":
                v = float(s) if s and "%" not in s else -1.0
                return 0.0 <= v <= 10000.0
            if field == "mass":
                v = float(s) if s and "%" not in s else -1.0
                return 0.1 <= v <= 10_000_000.0
        except ValueError:
            return False
        return True

    c_ok = _field_ok(c_norm)
    t_ok = _field_ok(t_norm)
    if c_ok and not t_ok:
        return c_norm, crnn_confs or [0.85] * len(c_norm)
    if t_ok and not c_ok:
        return t_norm, [0.9] * len(t_norm)

    # Both pass (or both fail) — prefer Tesseract eng_sc. The shipped
    # CRNN (47% val snapshot) is poorly calibrated: it frequently hits
    # 0.85–0.90 mean confidence on wrong reads. eng_sc is SC-Datarunner
    # trained on the actual HUD font and reads live crops reliably.
    # Only override Tesseract when CRNN is VERY confident (≥0.95) AND
    # the CRNN read has more digits (usually indicates Tesseract chopped
    # a leading digit). This keeps the ensemble benefit without letting
    # overconfident CRNN hallucinations dominate. Retraining the CRNN
    # re-calibrates its confidences — this threshold can drop to 0.85
    # once the model hits >80% val and its confidence distribution
    # becomes reliable.
    mean_c = (sum(crnn_confs) / len(crnn_confs)) if crnn_confs else 0.0
    c_longer = len(c_norm) > len(t_norm)
    crnn_wins = mean_c >= 0.95 and c_longer
    log.info(
        "sc_ocr: vote-disagree field=%s crnn=%r(%.2f,%d) tess=%r(%d) -> %s",
        field, c_norm, mean_c, len(c_norm),
        t_norm, len(t_norm),
        "crnn" if crnn_wins else "tess",
    )
    if crnn_wins:
        return c_norm, crnn_confs
    return t_norm, [0.9] * len(t_norm)


_FULL_ROW_DEBUG_SAVED: dict[str, bool] = {}


def _ocr_full_row(
    img: Image.Image, y1: int, y2: int, field: str,
) -> tuple[str, list[float]]:
    """OCR the full row (label + value) and extract the trailing number.

    Robust against label-right-edge mis-detection because we don't
    need to know WHERE the value starts — we let the label itself
    serve as a baseline anchor for Tesseract, then regex out the
    trailing numeric token after decode.

    Pipeline:
      1. Crop ``img[y1_pad:y2_pad, 0:W]`` — full panel width.
      2. Polarity-correct + upscale if small + border-pad.
      3. Run Tesseract eng_sc with NO char whitelist (letters must
         decode properly so "MASS:", "RESISTANCE:", "INSTABILITY:"
         anchor the line).
      4. Regex: find all ``\\d[\\d.,]*%?`` tokens, return rightmost.

    Returns (text, confidences) or ("", []) on any failure. Caller
    falls through to ``_ocr_value_crop`` for a second opinion.
    """
    try:
        import pytesseract
        from ..screen_reader import _check_tesseract
        _check_tesseract()
    except Exception:
        return "", []

    _tessdata = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "ocr", "tessdata",
    )
    if not os.path.isfile(os.path.join(_tessdata, "eng_sc.traineddata")):
        return "", []

    # Full-width row crop with small vertical padding.
    y1_p = max(0, y1 - 2)
    y2_p = min(img.height, y2 + 2)
    row = img.crop((0, y1_p, img.width, y2_p))

    # Polarity correction: Tesseract prefers dark-text-on-light.
    gray = np.array(row.convert("L"), dtype=np.uint8)
    if float(np.median(gray)) < 130:
        row = Image.fromarray(255 - gray)

    # Contrast stretch. Pull min/max to full [0, 255] range so
    # thin-stroke digits on dim backgrounds get Tesseract's full
    # signal. No-op when the row already uses full range.
    try:
        _arr = np.array(row.convert("L"), dtype=np.float32)
        _mn, _mx = float(_arr.min()), float(_arr.max())
        if _mx - _mn > 10:
            _arr = (_arr - _mn) * (255.0 / (_mx - _mn))
            row = Image.fromarray(np.clip(_arr, 0, 255).astype(np.uint8))
    except Exception:
        pass

    # Upscale small rows to ~120 px tall so Tesseract's LSTM has
    # plenty of room even on extraction-mode tiny panels (rows as
    # small as 20 px get a full 6× upscale → 120 px). Bumped from
    # the previous 80-px target because 80 was marginal on thin
    # digits rendered at small native size.
    W, H = row.size
    if H < 100:
        scale = max(3, 120 // max(1, H))
        row = row.resize((W * scale, H * scale), Image.LANCZOS)

    # Unsharp-mask after upscale to restore stroke sharpness lost
    # during interpolation. Modest strength — heavier values hurt
    # more than help because they exaggerate anti-aliasing artifacts.
    try:
        from PIL import ImageFilter as _ImageFilter
        row = row.filter(_ImageFilter.UnsharpMask(radius=1.2, percent=130, threshold=2))
    except Exception:
        pass

    # Border pad so PSM 7 can lock onto a baseline.
    try:
        from PIL import ImageOps as _ImageOps
        _pad = max(20, row.height // 4)
        _corner = row.getpixel((0, 0))
        row = _ImageOps.expand(row, border=_pad, fill=_corner)
    except Exception:
        pass

    # Save one debug sample per field per session so we can inspect
    # what Tesseract actually received.
    try:
        if not _FULL_ROW_DEBUG_SAVED.get(field):
            row.save(f"debug_fullrow_{field}.png")
            _FULL_ROW_DEBUG_SAVED[field] = True
    except Exception:
        pass

    prev_env = os.environ.get("TESSDATA_PREFIX")
    os.environ["TESSDATA_PREFIX"] = _tessdata
    try:
        # image_to_data so we get per-word bounding boxes; lets us
        # crop the right-of-label pixel region for targeted reads
        # even when Tesseract fails on the value digits in its own
        # full-row pass. No char whitelist so labels decode cleanly.
        data = pytesseract.image_to_data(
            row, config="-l eng_sc --psm 7",
            output_type=pytesseract.Output.DICT,
        )
        # Reassemble the text in Tesseract's reading order for the
        # regex-extraction fast path below.
        words = [w for w in data.get("text", []) if (w or "").strip()]
        text = " ".join(words)
    except Exception:
        data = {}
        text = ""
    finally:
        if prev_env is None:
            os.environ.pop("TESSDATA_PREFIX", None)
        else:
            os.environ["TESSDATA_PREFIX"] = prev_env

    import re

    # Fast path: did Tesseract see a numeric token in the full-row read?
    if text:
        matches = re.findall(r"\d[\d.,]*%?", text)
        if matches:
            value = matches[-1].replace(",", "")
            log.info(
                "sc_ocr: row-ocr field=%s decoded=%r -> %r (fast-path)",
                field, text, value,
            )
            return value, [0.95] * len(value)

    # Slow path: Tesseract saw the LABEL but couldn't decode the
    # value digits (small extraction-mode panels hit this). Find the
    # label word's bounding box, crop the pixel region to its right,
    # upscale aggressively, and run CRNN + Tesseract on the value-
    # only crop. This uses Tesseract as a row anchor but relies on
    # CRNN for actual digit recognition where it's often stronger.
    if not data:
        return "", []
    label_prefix = {"mass": "mass", "resistance": "resi", "instability": "inst"}.get(field, "")
    if not label_prefix:
        log.debug("sc_ocr: row-ocr field=%s decoded=%r no_numeric_token", field, text)
        return "", []

    # Walk words in reading order; find the last one whose alpha-only
    # form contains the 4-char prefix (e.g. 'MASS:' → 'mass' → OK).
    anchor = None
    n_words = len(data.get("text", []))
    for i in range(n_words):
        w = (data["text"][i] or "").strip()
        if not w:
            continue
        alpha = "".join(c for c in w if c.isalpha()).lower()
        if label_prefix in alpha:
            anchor = (
                int(data["left"][i]),
                int(data["top"][i]),
                int(data["width"][i]),
                int(data["height"][i]),
            )
    if anchor is None:
        log.debug(
            "sc_ocr: row-ocr field=%s decoded=%r no_label_anchor",
            field, text,
        )
        return "", []

    lx, ly, lw, lh = anchor
    row_W, row_H = row.size
    # Leave a tiny gap after the colon
    value_x0 = min(row_W - 4, lx + lw + max(4, lh // 2))
    value_x1 = row_W
    # Vertically pad so descenders/ascenders survive
    value_y0 = max(0, ly - max(4, lh // 4))
    value_y1 = min(row_H, ly + lh + max(4, lh // 4))
    value_only = row.crop((value_x0, value_y0, value_x1, value_y1))

    # Aggressive upscale to ~200 px tall — only the small value crop
    # gets this treatment, not the whole row, so per-digit pixel
    # count rises far beyond what Tesseract sees in its full-row pass.
    vw, vh = value_only.size
    if vh > 0 and vh < 200:
        vscale = max(2, 220 // vh)
        value_only = value_only.resize(
            (vw * vscale, vh * vscale), Image.LANCZOS,
        )

    # Re-contrast + unsharp — the slow path earns heavier treatment
    try:
        from PIL import ImageFilter as _IF
        _va = np.array(value_only.convert("L"), dtype=np.float32)
        _mn, _mx = float(_va.min()), float(_va.max())
        if _mx - _mn > 8:
            _va = (_va - _mn) * (255.0 / (_mx - _mn))
            value_only = Image.fromarray(np.clip(_va, 0, 255).astype(np.uint8))
        value_only = value_only.filter(
            _IF.UnsharpMask(radius=1.5, percent=180, threshold=2),
        )
    except Exception:
        pass

    try:
        if not _FULL_ROW_DEBUG_SAVED.get(field + "_value"):
            value_only.save(f"debug_value_slowpath_{field}.png")
            _FULL_ROW_DEBUG_SAVED[field + "_value"] = True
    except Exception:
        pass

    # Run both engines on the targeted value crop.
    crnn_result = _crnn_recognize(value_only)
    crnn_text = ""
    crnn_confs: list[float] = []
    if crnn_result is not None:
        _ct, _cc = crnn_result
        # Same digit-mapping as in the main _ocr_value_crop path.
        _mapped = (_ct.replace("I", "1").replace("l", "1")
                     .replace("O", "0").replace("o", "0")
                     .replace("S", "5").replace("s", "5")
                     .replace("B", "8").replace("Z", "2")
                     .replace("G", "6").replace("q", "9"))
        crnn_text = "".join(c for c in _mapped if c in "0123456789.%")
        crnn_confs = _cc

    tess_text = _try_tesseract_eng_sc(value_only)

    log.info(
        "sc_ocr: row-ocr field=%s slow-path decoded=%r anchor=%r "
        "crnn=%r tess=%r",
        field, text, anchor, crnn_text, tess_text,
    )

    voted = _parallel_vote(field, crnn_text, crnn_confs, tess_text)
    if voted is None:
        return "", []
    return voted


def _ocr_value_crop(value_crop: Image.Image, field: str = "") -> tuple[str, list[float]]:
    """OCR a tight value crop → (text, per_char_confidences).

    Parallel CRNN + Tesseract voting for digit-only fields — both
    engines run every scan and their outputs are reconciled by
    ``_parallel_vote``. Falls through to the 28×28 ONNX segmenter only
    when both engines produce nothing agreeable.
    """
    # ── CRNN (primary) ──
    # Acceptance gate is deliberately strict. The initial CRNN was
    # trained on sc_templates-derived synthetic crops which don't
    # perfectly match the real SC HUD rendering — so its confidence
    # on real crops is lower than on synth. Requiring length >= 2 AND
    # mean confidence > 0.95 keeps the CRNN out of the way for
    # typical runs while still letting a future retrain (with
    # real-crop training data) take over once accuracy improves.
    # Gate tuning (two-tier):
    # * High-confidence CRNN read → use directly
    # * Low-confidence CRNN read → fall through to eng_sc Tesseract
    #   (which is rock-solid on digit-only HUD values via SC-Datarunner
    #   trained model). eng_sc can't do letters as well, so we still
    #   accept the CRNN for letter-containing text at a lower bar.
    # HUD values (mass/resistance/instability) must be numeric-only.
    # For those fields, reject any CRNN output containing letters —
    # letters are always hallucinations on numeric fields (the
    # infamous 'I' → '1' confusion, or trailing letter noise). For
    # other fields (explicitly labelled as text), allow the letter
    # gate.
    _digit_only_field = field in ("mass", "resistance", "instability")

    # ─── CUSTOM ONNX MODEL (priority for digit fields) ───
    # The user-trained 28x28 CNN classifier (in fallback._session) is
    # ~99% accurate on real SC HUD glyphs — beats both CRNN
    # (synth-trained) and Tesseract (general-purpose) on this domain.
    # Run it FIRST for digit-only fields. If it returns confident
    # output, use it directly and skip CRNN+Tesseract entirely.
    if _digit_only_field:
        try:
            _rgb_pri = np.array(value_crop.convert("RGB"), dtype=np.uint8)
            _gray_pri = np.array(value_crop.convert("L"), dtype=np.uint8)
            # Background-agnostic polarity normalization (handles
            # bright-sky panels where median-based inversion fails).
            # After canonicalization text is BRIGHT (matches CNN's
            # training convention: bright glyphs on dark bg, padded
            # with white in _segment_glyphs).
            _gray_pri = _canonicalize_polarity(_gray_pri)
            _thr_pri = _otsu(_gray_pri)
            _bin_pri = (_gray_pri > _thr_pri).astype(np.uint8) * 255
            _crops_pri = _segment_glyphs(_gray_pri, _bin_pri)
            # Diagnostic: log how many glyphs the segmenter found.
            # Distinguishes "segmenter dropped digits" (pipeline issue)
            # from "classifier misread clean digits" (training issue).
            log.info(
                "sc_ocr.diag: field=%s segment(primary)=%d glyphs",
                field, len(_crops_pri) if _crops_pri else 0,
            )
            if _crops_pri:
                _results_pri = _classify_crops(_crops_pri)
                _txt_pri = "".join(ch for ch, _ in _results_pri)
                _confs_pri = [c for _, c in _results_pri]
                # Diagnostic: log per-glyph classifier output with
                # confidence regardless of confidence-gate. Lets us
                # see whether ONNX is e.g. reading a `1`-shaped crop
                # as `7` with 0.80 confidence (classifier problem) or
                # whether segmentation only ever fed it 1 crop
                # (pipeline problem).
                if _results_pri:
                    _per_glyph = " ".join(
                        f"({ch},{conf:.2f})"
                        for ch, conf in _results_pri
                    )
                    log.info(
                        "sc_ocr.diag: field=%s classify(primary)=%r "
                        "per-glyph=%s",
                        field, _txt_pri, _per_glyph,
                    )
                # Strict: only use if every char hit ≥ 0.85 confidence
                # AND output is purely numeric/./%
                if (_txt_pri
                        and all(c in "0123456789.%" for c in _txt_pri)
                        and _confs_pri
                        and min(_confs_pri) >= 0.85):
                    _mean = sum(_confs_pri) / len(_confs_pri)
                    log.info(
                        "sc_ocr: PRIMARY field=%s text=%r mean=%.2f (custom CNN)",
                        field, _txt_pri, _mean,
                    )
                    return _txt_pri, _confs_pri
        except Exception as _exc:
            log.debug("sc_ocr: primary ONNX path failed: %s", _exc)

    # ── Parallel CRNN + Tesseract vote ──
    # Both engines run every scan (not a cascade) and _parallel_vote
    # reconciles their outputs using field-aware sanity checks. The
    # ONNX segmenter below is reached only when both engines agree
    # on nothing (rare — usually one or both returns something).
    if _digit_only_field:
        _crnn_raw = _crnn_recognize(value_crop)
        _crnn_text, _crnn_confs = ("", [])
        if _crnn_raw is not None:
            _ctxt, _cconfs = _crnn_raw
            # Digit-mapping on CRNN output so the voter compares apples
            # to apples against Tesseract (which is char-whitelisted to
            # digits/./%).
            if _ctxt:
                _mapped = (_ctxt.replace("I", "1").replace("l", "1")
                                .replace("O", "0").replace("o", "0")
                                .replace("S", "5").replace("s", "5")
                                .replace("B", "8").replace("Z", "2")
                                .replace("G", "6").replace("q", "9"))
                _crnn_text = "".join(c for c in _mapped if c in "0123456789.%")
                _crnn_confs = _cconfs

        _tess_text = _try_tesseract_eng_sc(value_crop)
        voted = _parallel_vote(field, _crnn_text, _crnn_confs, _tess_text)
        if voted is not None:
            _vt, _vc = voted
            _vmean = (sum(_vc) / len(_vc)) if _vc else 0.0
            log.info(
                "sc_ocr: vote field=%s text=%r mean=%.2f (crnn=%r tess=%r)",
                field, _vt, _vmean, _crnn_text, _tess_text,
            )
            return _vt, _vc
        log.debug(
            "sc_ocr: parallel vote produced nothing for field=%s "
            "(crnn=%r tess=%r); falling through to ONNX segmenter",
            field, _crnn_text, _tess_text,
        )

    # Non-digit field: keep the original CRNN-first flow (letter text
    # can't be voted against the digit-only eng_sc model anyway).
    if not _digit_only_field:
        crnn_result = _crnn_recognize(value_crop)
        if crnn_result is not None:
            text, confs = crnn_result
            mean_conf = (sum(confs) / len(confs)) if confs else 0.0
            if text and mean_conf > 0.75:
                log.info(
                    "sc_ocr: crnn(text-field) text=%r mean=%.2f field=%s",
                    text, mean_conf, field,
                )
                return text, confs

    import pytesseract
    # Ensure Tesseract binary path is configured
    from ..screen_reader import _check_tesseract
    _check_tesseract()

    W, H = value_crop.size

    # Auto-upscale small crops — keep the original upscale here so the
    # ONNX segmenter sees reasonable text sizes. Tesseract gets its own
    # more aggressive upscale below (see _tess_input).
    if H < 25:
        scale_up = max(2, 28 // max(1, H))
        value_crop = value_crop.resize(
            (W * scale_up, H * scale_up), Image.LANCZOS,
        )

    rgb = np.array(value_crop.convert("RGB"), dtype=np.uint8)
    gray = np.array(value_crop.convert("L"), dtype=np.uint8)
    max_ch = rgb.max(axis=2).astype(np.uint8)
    median = float(np.median(gray))

    # Polarity correction
    if median > 140:
        gray = 255 - gray
        max_ch = 255 - max_ch

    # ── Tesseract (primary) ──
    # Feed the raw value crop DIRECTLY — Tesseract's internal
    # preprocessing (adaptive threshold, upscaling) handles the
    # SC HUD font better than our manual pipeline. Verified:
    # raw crop → "499" correct; 4x+Otsu+flip → empty or wrong.
    #
    # Uses the SC-specific Tesseract LSTM (``eng_sc.traineddata``)
    # from the SC-Datarunner-UEX project when available. That model
    # is fine-tuned on SC HUD renderings and is dramatically more
    # robust than default ``eng`` at scale — default eng hallucinates
    # characters (e.g. '499' → '43%' at 4× upscale) while eng_sc
    # reads '499' stably. Our local tessdata dir ships the SC model
    # alongside a copy of eng for fallback.
    import os as _os
    _tessdata_local = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
        "ocr", "tessdata",
    )
    _have_sc = _os.path.isfile(_os.path.join(_tessdata_local, "eng_sc.traineddata"))
    _tess_env = _os.environ.copy()
    if _have_sc:
        _tess_env["TESSDATA_PREFIX"] = _tessdata_local
    _tess_lang = "eng_sc" if _have_sc else "eng"
    _tess_cfg = f"-l {_tess_lang} --psm 7 -c tessedit_char_whitelist=0123456789.%"
    _prev_env = {}
    if _have_sc:
        _prev_env["TESSDATA_PREFIX"] = _os.environ.get("TESSDATA_PREFIX")
        _os.environ["TESSDATA_PREFIX"] = _tessdata_local
    # Tesseract does best when text height is ~80-120 px. If the
    # current crop is smaller, upscale JUST for Tesseract (don't
    # touch the pipeline-shared ``value_crop`` which ONNX needs).
    # Empirically, x2 hits a Tesseract failure mode with the SC font
    # (clean '499' reads empty at x2 but correctly at x1/x3/x4). So
    # we jump straight to x3+ when upscaling is needed.
    _tW, _tH = value_crop.size
    _t_short = min(_tW, _tH)
    if _t_short < 80:
        _t_scale = max(3, 100 // max(1, _t_short))
        _tess_input = value_crop.resize(
            (_tW * _t_scale, _tH * _t_scale), Image.LANCZOS,
        )
    else:
        _tess_input = value_crop

    tess_text = ""
    try:
        tess_text = pytesseract.image_to_string(
            _tess_input,
            config=_tess_cfg,
        ).strip()
    except Exception:
        pass
    finally:
        if _have_sc:
            if _prev_env["TESSDATA_PREFIX"] is None:
                _os.environ.pop("TESSDATA_PREFIX", None)
            else:
                _os.environ["TESSDATA_PREFIX"] = _prev_env["TESSDATA_PREFIX"]

    # ── ONNX (secondary voter) ──
    thr_a = _otsu(gray)
    bin_a = (gray > thr_a).astype(np.uint8) * 255
    crops = _segment_glyphs(gray, bin_a)
    log.info(
        "sc_ocr.diag: field=%s segment(secondary)=%d glyphs",
        field, len(crops) if crops else 0,
    )
    onnx_text = ""
    onnx_confs: list[float] = []
    if crops:
        results = _classify_crops(crops)
        onnx_text = "".join(ch for ch, _ in results)
        onnx_confs = [c for _, c in results]
        # Diagnostic: per-glyph classifier output for the secondary
        # voter path. Same purpose as the primary diag log — separates
        # classifier errors from segmentation errors.
        if results:
            _per_glyph = " ".join(
                f"({ch},{conf:.2f})" for ch, conf in results
            )
            log.info(
                "sc_ocr.diag: field=%s classify(secondary)=%r per-glyph=%s",
                field, onnx_text, _per_glyph,
            )

    # ── Consensus → collect for CRNN retraining ──
    # When Tesseract AND ONNX agree on the exact same text, that's
    # ground truth we can trust. Save the original value crop with
    # this label so a future CRNN retrain has domain-matched data.
    if tess_text and onnx_text and tess_text == onnx_text:
        try:
            from ..training_collector import collect_crnn_value_sample
            collect_crnn_value_sample(value_crop, tess_text, source="live_consensus")
        except Exception as exc:
            log.debug("sc_ocr: CRNN sample save failed: %s", exc)

    # ── Vote ──
    # Prefer Tesseract (more accurate LSTM) unless it returned empty,
    # in which case fall back to ONNX.
    if tess_text:
        # Use Tesseract result with dummy confidences
        return tess_text, [0.9] * len(tess_text)
    elif onnx_text:
        return onnx_text, onnx_confs
    else:
        return "", []


# ── Public API ─────────────────────────────────────────────────────

def scan_region(region: dict) -> Optional[int]:
    """Read a signal-number region → int in [1000, 35000].

    Same architectural pattern as ``scan_hud_onnx``:

      1. Capture the region.
      2. **Anchor**: locate the location-pin icon via blacklist
         pHash matching. The icon's right edge is the rigid
         coordinate from which the digit cluster starts at a known
         offset — this is the signal scanner's equivalent of the
         HUD's mineral-row template anchor.
      3. **Crop**: snip from (icon_right + 4 px) to right edge,
         then row-isolate to the dominant text band.
      4. **Multi-engine OCR**: run the trained per-region CNN
         (``model_signal_cnn.onnx``) AND Tesseract on the same
         clean crop. Vote when they agree; fall back to None
         (caller falls back to legacy 3-engine vote) when they
         don't, mirroring the HUD's lock-gate consensus.
      5. **Validate**: result must be in [1000, 35000].

    Returns the recognized integer or None on any failure (anchor
    miss, OCR disagreement, out-of-range value).
    """
    img = capture.grab(region)
    if img is None:
        return None
    return _signal_recognize_pil(img)


def _signal_recognize_pil(img) -> Optional[int]:
    """Same pipeline as ``scan_region`` but takes an in-memory PIL
    image — used by ``screen_reader.scan_region`` to avoid a second
    capture pass after it's already grabbed the frame.

    Pipeline (mirrors HUD's ``scan_hud_onnx`` architecture):

      1. **Anchor** via NCC against the location-pin icon template
         (signal_anchor.find_digit_crop_box). The icon is a fixed-
         shape UI element that NEVER changes across rocks/sessions
         — same role as the HUD's mineral-row template.
      2. **Crop** to just the digit cluster (right of icon).
      3. **Row-isolate** to the dominant text band (drops UNKNOWN
         caption, distance text, etc. that are below the number).
      4. **Multi-PSM/scale Tesseract** OCR of the crop. The icon is
         already excluded so Tesseract only sees the digits — much
         higher accuracy than the legacy 3-engine vote.
      5. **Range validate** in [1000, 35000].
    """
    try:
        from PIL import Image as _PILImage
        if not isinstance(img, _PILImage.Image):
            img = _PILImage.fromarray(np.asarray(img))
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
    except Exception as exc:
        log.debug("sc_ocr.signal: bad input: %s", exc)
        return None
    if gray.ndim != 2 or gray.shape[0] < 8 or gray.shape[1] < 12:
        return None

    try:
        import sys
        from pathlib import Path as _Path
        _scripts = _Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        import extract_labeled_glyphs as _xlg  # type: ignore
    except Exception as exc:
        log.debug("sc_ocr.signal: extract_labeled_glyphs unavailable: %s", exc)
        return None

    # ── Anchor via NCC against the icon template ──
    # Same approach the HUD uses for label rows. The icon NEVER
    # changes shape; only its color varies, which polarity-
    # canonicalization eliminates. Result is pixel-precise.
    try:
        from . import signal_anchor as _sa
        crop_box = _sa.find_digit_crop_box(gray)
    except Exception as exc:
        log.debug("sc_ocr.signal: anchor failed: %s", exc)
        crop_box = None

    if crop_box is not None:
        # Use the anchor-derived crop. This is the happy path.
        x1, y1, x2, y2 = crop_box
        work = gray[y1:y2, x1:x2]
    else:
        # Anchor missed (no icon in image, or NCC below threshold).
        # Fall back to the heuristic icon mask + row isolate, which
        # is less reliable but still better than nothing.
        bg = int(np.median(gray))
        work = gray.copy()
        icon_right = _xlg._locate_icon_via_blacklist_match(work)
        floor_mask = int(work.shape[1] * 0.30)
        mask_w = max(floor_mask, icon_right + 4 if icon_right > 0 else 0)
        if 0 < mask_w < work.shape[1]:
            work[:, :mask_w] = bg

    # ── Row isolation: keep only the dominant text band ──
    work = _xlg._isolate_main_row(work)
    if work.shape[0] < 6 or work.shape[1] < 12:
        return None

    # ── Tesseract ensemble (multi-PSM/scale) — VOTE across variants ──
    # Previous behaviour was "first 4-5-digit variant wins", which made
    # the read brittle to per-frame HUD jitter: one PSM/scale could
    # mis-segment a `2` as `1`, and we'd ship the bad read even when
    # the other 5 variants agreed on the correct value. Now we collect
    # ALL plausible variant reads and pick the most-supported one,
    # with the known-signature table as a tie-breaker.
    from PIL import Image as _PILImage
    base = _PILImage.fromarray(work, mode="L")
    variants = [
        (base, "1x", 1),
        (base.resize((base.width * 2, base.height * 2), _PILImage.LANCZOS), "2x", 2),
        (base.resize((base.width * 3, base.height * 3), _PILImage.LANCZOS), "3x", 3),
    ]
    # Each entry: (value, text, boxes, tag, scale)
    candidates: list[tuple[int, str, list, str, int]] = []
    for psm in ("7", "13", "8"):
        for img_v, tag, scale in variants:
            try:
                boxes = _xlg._tesseract_char_boxes(
                    img_v, whitelist="0123456789.", psm=psm,
                )
            except Exception:
                continue
            if not boxes:
                continue
            text = "".join(b[0] for b in boxes if b[0].isdigit())
            if not (4 <= len(text) <= 5):
                continue
            try:
                v = int(text)
            except ValueError:
                continue
            if not (1000 <= v <= 35000):
                continue
            digit_boxes = [b for b in boxes if b[0].isdigit()]
            candidates.append((v, text, digit_boxes, f"{tag}/psm{psm}", scale))

    if not candidates:
        log.debug("sc_ocr.signal: no Tesseract variant produced 4-5 digits in range")
        return None

    # Vote. Two-tier scoring:
    #   1. Among variants whose value EXACT-matches a known signature
    #      table entry, take the most common. Known-table hits are
    #      strong evidence the read is correct (the table is finite
    #      and the truth IS one of those values).
    #   2. If no variant matches the table (table empty, or rock value
    #      outside the chart), fall back to plain majority vote across
    #      ALL in-range variants.
    from collections import Counter
    table_hits = [c for c in candidates if c[0] in _KNOWN_SIGNAL_VALUES]
    if table_hits:
        counts = Counter(c[0] for c in table_hits)
        winner_val, winner_count = counts.most_common(1)[0]
        winner = next(c for c in table_hits if c[0] == winner_val)
        vote_strength = f"{winner_count}/{len(candidates)} table-match"
    else:
        counts = Counter(c[0] for c in candidates)
        winner_val, winner_count = counts.most_common(1)[0]
        winner = next(c for c in candidates if c[0] == winner_val)
        vote_strength = f"{winner_count}/{len(candidates)} majority"

    tess_val = winner[0]
    tess_text = winner[1]
    tess_boxes_used = winner[2]
    tess_tag = winner[3]
    tess_scale = winner[4]

    # ── Tesseract is the primary classifier ──
    # The trained CNN doesn't help inference: at training time
    # crops were CURATED (Glyph Forge), so the CNN learned the
    # clean-input distribution. At inference Tesseract's bboxes
    # often include icon edges or neighboring-digit pixels — shapes
    # the CNN never saw. CNN predictions on those go wrong while
    # Tesseract handles the noise gracefully.
    #
    # We still keep the CNN around as an OPTIONAL tie-breaker /
    # sanity check via _signal_cnn_at_tess_boxes(), but only INFORM
    # the log — the Tesseract read is what we return. This mirrors
    # how the HUD's per-digit CNN is one validator among many, not
    # the sole authority.
    try:
        cnn_text = _signal_cnn_at_tess_boxes(
            work, tess_boxes_used, tess_scale,
        )
        if cnn_text is not None and cnn_text != tess_text:
            log.debug(
                "sc_ocr.signal: CNN disagrees (cnn=%r tess=%r) — "
                "trusting Tesseract", cnn_text, tess_text,
            )
    except Exception:
        pass

    # ── Display-level stabilisation ─────────────────────────────
    # Even after voting, consecutive frames can swing between two
    # plausible-looking readings on heavy HUD jitter (e.g. when the
    # icon anchor briefly slides ±1 px and re-cuts the leading digit).
    # We hold the LAST DISPLAYED value steady until the buffer shows
    # _SIGNAL_AGREEMENT_REQ consecutive reads of a NEW value. This
    # turns a 17,020 → 17,011 → 17,020 single-frame blip into a
    # silent no-op at the display layer.
    global _STABLE_SIGNAL
    _RECENT_SIGNAL_READS.append(tess_val)
    if _STABLE_SIGNAL is None:
        # First read of a fresh buffer — show it immediately.
        _STABLE_SIGNAL = tess_val
    elif tess_val != _STABLE_SIGNAL:
        # Candidate new value. Only swap if the last
        # _SIGNAL_AGREEMENT_REQ reads ALL agree on the new value.
        recent = list(_RECENT_SIGNAL_READS)[-_SIGNAL_AGREEMENT_REQ:]
        if (
            len(recent) >= _SIGNAL_AGREEMENT_REQ
            and all(r == tess_val for r in recent)
        ):
            log.info(
                "sc_ocr.signal: stable swap %d → %d (consensus %d-of-%d)",
                _STABLE_SIGNAL, tess_val,
                _SIGNAL_AGREEMENT_REQ, _SIGNAL_BUFFER_LEN,
            )
            _STABLE_SIGNAL = tess_val
        else:
            log.debug(
                "sc_ocr.signal: outlier %d (stable=%d, vote=%s) — "
                "holding stable value",
                tess_val, _STABLE_SIGNAL, vote_strength,
            )

    log.info(
        "sc_ocr.signal: vote %d via %s (%s) → display %d",
        tess_val, tess_tag, vote_strength, _STABLE_SIGNAL,
    )
    return _STABLE_SIGNAL


# ── Lazy CNN session for the signal-region model ──
_signal_session = None
_signal_session_path = ""
_signal_classes = "0123456789"


def _signal_cnn_at_tess_boxes(
    gray_work: np.ndarray,
    tess_boxes: list,
    scale: int,
) -> Optional[str]:
    """Run the trained signal CNN on the per-character bounding boxes
    Tesseract reported. Tesseract finds the spatial positions; the
    CNN does the digit identification. If they round-trip to the
    same string, both engines agree.

    Returns the predicted string or None on failure."""
    try:
        import onnxruntime as _ort
        from . import training_registry as _tr  # type: ignore
    except Exception:
        try:
            from .. import training_registry as _tr  # type: ignore
        except Exception:
            return None
    try:
        from ocr import training_registry as _tr  # type: ignore
    except Exception:
        pass
    model_path = _tr.get_model_path("signal")
    if not model_path.is_file():
        return None
    global _signal_session, _signal_session_path, _signal_classes
    try:
        if _signal_session is None or _signal_session_path != str(model_path):
            _signal_session = _ort.InferenceSession(
                str(model_path),
                providers=["CPUExecutionProvider"],
            )
            _signal_session_path = str(model_path)
            try:
                import json as _json
                meta = _json.loads(
                    model_path.with_suffix(".json").read_text(encoding="utf-8")
                )
                _signal_classes = meta.get("charClasses", "0123456789")
            except Exception:
                _signal_classes = "0123456789"
    except Exception as exc:
        log.debug("sc_ocr.signal: CNN session load failed: %s", exc)
        return None

    # Reuse the offline extractor's glyph-rendering helper so the CNN
    # sees inputs shaped EXACTLY like its training data.
    try:
        import sys
        from pathlib import Path as _Path
        _scripts = _Path(__file__).resolve().parent.parent.parent / "scripts"
        if str(_scripts) not in sys.path:
            sys.path.insert(0, str(_scripts))
        import extract_labeled_glyphs as _xlg  # type: ignore
    except Exception:
        return None

    # Convert Tesseract's bounding boxes back to original `gray_work`
    # coordinates and resolve overlapping boxes via the midpoint trick
    # (same as the training pipeline).
    raw_spans = []
    for b in tess_boxes:
        if not b[0].isdigit():
            continue
        x1 = b[1] // scale
        x2 = b[3] // scale
        if x2 > x1:
            raw_spans.append([x1, x2])
    for i in range(len(raw_spans)):
        if i + 1 < len(raw_spans):
            cur_x1, cur_x2 = raw_spans[i]
            nxt_x1, nxt_x2 = raw_spans[i+1]
            if nxt_x1 < cur_x2:
                cur_c = (cur_x1 + cur_x2) / 2.0
                nxt_c = (nxt_x1 + nxt_x2) / 2.0
                if nxt_c > cur_c:
                    boundary = int((cur_c + nxt_c) / 2.0)
                    raw_spans[i][1] = boundary
                    raw_spans[i+1][0] = boundary
    digits: list[str] = []
    for x1, x2 in raw_spans:
        if x2 - x1 < 3:
            return None
        g = _xlg._glyph_to_28x28(gray_work, x1, x2)
        if g is None:
            return None
        x = (g.astype(np.float32) / 255.0)[None, None, :, :]
        try:
            out = _signal_session.run(None, {"input": x})[0]
        except Exception:
            return None
        idx = int(np.argmax(out, axis=1)[0])
        if 0 <= idx < len(_signal_classes):
            digits.append(_signal_classes[idx])
        else:
            return None
    return "".join(digits) if digits else None


def _ocr_mineral_name(
    img: "Image.Image",
    y1: int,
    y2: int,
    x_min: int,
) -> Optional[str]:
    """Extract the mineral name (e.g. 'Beryl', 'Quantanium') from the
    mineral row crop.

    Pipeline (placeholder until the SC alphabet model is trained):
      1. Crop the mineral row from the panel image.
      2. Polarity-canonicalize via the same minority-class rule used
         for digit fields, so dark- and light-background panels both
         look the same to the OCR.
      3. Upscale to ~60 px tall (Tesseract performs best on text in
         that height range).
      4. Run Tesseract with a letters/space/parens whitelist
         (numbers excluded — mineral names never contain digits).
      5. Strip any parenthesized suffix like ``(RAW)`` before
         vocabulary match (those modifiers are noise, not part of
         the mineral identity).
      6. Snap to the closest known mineral via
         ``refinery_reader._fuzzy_mineral`` (uses difflib +
         the curated alias map).

    Returns the canonical mineral name on success or None on any
    failure. Tesseract is the placeholder OCR engine here; when the
    SC alphabet CNN is trained, swap it in by replacing the
    Tesseract block — the fuzzy-match snap stays as the final
    safety net regardless of upstream OCR quality.
    """
    try:
        import re
        import pytesseract
        from ..screen_reader import _check_tesseract
        if not _check_tesseract():
            return None
    except Exception:
        return None
    if y2 <= y1 or (y2 - y1) < 6:
        return None
    crop_x_left = max(0, x_min - 20)
    if crop_x_left >= img.width - 4:
        return None
    crop = img.crop((crop_x_left, y1, img.width, y2))
    if crop.width < 20 or crop.height < 8:
        return None
    gray = np.array(crop.convert("L"), dtype=np.uint8)
    gray = _canonicalize_polarity(gray)
    H, W = gray.shape
    # Tesseract works best at ~60-100 px text height
    if H < 60:
        scale = max(2, 60 // max(1, H))
        new_w = max(1, W * scale)
        new_h = max(1, H * scale)
        try:
            tess_input = Image.fromarray(gray).resize(
                (new_w, new_h), Image.LANCZOS,
            )
        except Exception:
            return None
    else:
        tess_input = Image.fromarray(gray)
    try:
        text = pytesseract.image_to_string(
            tess_input,
            config=(
                "--psm 7 -c tessedit_char_whitelist="
                "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz () "
            ),
        ).strip()
    except Exception as exc:
        log.debug("mineral name OCR failed: %s", exc)
        return None
    if not text:
        return None
    # Strip parenthesized suffix like "(RAW)"
    base = re.sub(r"\s*\(.*?\)\s*", "", text).strip()
    if not base:
        base = text  # fallback if the whole read was inside parens
    try:
        from ..refinery_reader import _fuzzy_mineral
        canonical = _fuzzy_mineral(base)
        if canonical:
            log.info("sc_ocr: mineral_name raw=%r → %r", text, canonical)
            return canonical
    except Exception as exc:
        log.debug("mineral fuzzy match failed: %s", exc)
    # No fuzzy match: refuse to return unvalidated raw OCR. Single-letter
    # or nonsense Tesseract output (e.g. "i" on a crop it couldn't read)
    # would otherwise surface as the resource name in the break bubble.
    # Caller falls back to the signal-scanner match when this returns None.
    log.info("sc_ocr: mineral_name raw=%r (no fuzzy match — dropping)", text)
    return None


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
        "mineral_name": None,
        "panel_visible": False,
    }
    t0 = time.time()
    # ── Profile-aware dispatch (scaffolding) ──
    # Load the profile that scopes this scan (mining HUD = digit-only,
    # uses model_cnn.onnx). Subsequent steps will route classification
    # and validation through this profile so:
    #   1. The mining HUD's digit model is reserved for mining HUD only
    #      (other panels use the SC alphabet model when it's trained).
    #   2. Per-field char whitelists are enforced post-classification
    #      (a digit-only field can never produce a letter even if the
    #      classifier is confused).
    # For now the profile is loaded but not yet enforced — that's a
    # follow-up wiring change so we can verify nothing regresses first.
    try:
        from . import profile_loader as _pl
        _profile = _pl.get_profile("mining_hud")
    except Exception as _pexc:
        log.warning("profile_loader: get_profile('mining_hud') failed: %s", _pexc)
        _profile = None
    # Reset diagnostic overlay for this scan.
    try:
        from . import debug_overlay as _dbg
        _dbg.reset()
    except Exception:
        _dbg = None
    # Single-frame capture. The previous 12-frame averaging blurred
    # text rendering enough to confuse OCR on tight glyphs, and the
    # anchor-based row reconciliation in _find_label_rows handles
    # jiggle-related row mis-identification structurally instead.
    img = capture.grab(region)
    if img is None:
        img = capture.grab(region)
    if img is None:
        return empty
    if _dbg is not None:
        _dbg.set_image(img)
        # Write IMMEDIATELY so the viewer reflects the latest capture
        # even if any downstream step crashes before reaching the
        # end-of-scan write. End-of-scan write overwrites with the
        # fully-annotated version.
        try:
            _dbg.write()
        except Exception as _wexc:
            log.warning("debug_overlay early write failed: %s", _wexc)

    # Upscale to reference size if the capture is smaller.
    # The ONNX model was trained on digit crops from a 397x541
    # panel where text rows are ~24px tall. Smaller panels produce
    # text too small for accurate classification (e.g. 400x403
    # produces 22px rows with 10px-wide digits — ONNX can't read
    # those). Upscaling to the reference height ensures consistent
    # glyph size regardless of the user's HUD region dimensions.
    REF_H = 541
    W_img, H_img = img.size
    if H_img < REF_H * 0.95:  # only upscale if meaningfully smaller
        scale_up = REF_H / H_img
        img = img.resize(
            (int(W_img * scale_up), REF_H), Image.LANCZOS,
        )

    gray = np.asarray(img.convert("L"), dtype=np.uint8)

    median_gray = float(np.median(gray))
    if median_gray > 130:
        gray = 255 - gray

    mineral_row = _find_mineral_row(img)
    # Anchor-based drift correction. The naive `_find_mineral_row` Y
    # is too noisy to trust as an absolute anchor (it sometimes picks
    # the SCAN RESULTS header on bright backgrounds). Instead, ask
    # ``label_match.find_label_positions`` for the NCC-validated
    # position of the MASS label — that's the most reliable anchor in
    # the panel because:
    #   * MASS is always present when scan results are visible
    #   * Its glyph shape is fixed (only color/polarity changes)
    #   * NCC pierces through asteroid noise that defeats projection-
    #     based row finders
    #
    # Critical guard: validate the NCC match against the CALIBRATED
    # mass position. A MASS NCC match more than ~25 px from where we
    # expect it is almost certainly a false positive (e.g. matching
    # against text in the COMPOSITION section). When that happens we
    # decline to drift-correct and let the locks land at saved coords.
    _cal_drift_y = 0
    # Tier-2 fallback state: if NCC anchor found MASS but it's far
    # from calibration, the locks are stale. Switch to NCC-derived
    # row positions for THIS scan (the user can either keep using
    # this until the panel settles, or recalibrate).
    _ncc_label_positions: dict = {}
    try:
        from . import calibration as _cal_drift_mod
        _saved_cal = _cal_drift_mod.load(region)
        _saved_mass = (_saved_cal or {}).get("rows", {}).get("mass") if _saved_cal else None
        from . import label_match as _lm_drift
        _ncc_label_positions = _lm_drift.find_label_positions(img)
        _mass_match = _ncc_label_positions.get("mass")
        if (
            _saved_mass is not None
            and _mass_match is not None
            and _mass_match.get("score", 0) >= 0.50
        ):
            _cur_mass_y = int(_mass_match["y"])
            _cal_mass_y = int(_saved_mass["y"])
            _proposed_drift = _cur_mass_y - _cal_mass_y
            if abs(_proposed_drift) <= 25:
                # Tier 1: small drift → drift-correct and use locks.
                _cal_drift_y = _proposed_drift
                if _cal_drift_y != 0:
                    log.info(
                        "sc_ocr: MASS-anchor drift %+d px "
                        "(cur=%d, cal=%d, ncc=%.2f) — drift-"
                        "correcting locked crops",
                        _cal_drift_y, _cur_mass_y, _cal_mass_y,
                        _mass_match["score"],
                    )
            else:
                # Tier 2: large drift → locks are stale, panel has
                # moved significantly. The NCC anchor is solid (high
                # score), so derive row positions from it directly
                # and skip the locks for this scan. Caller threads
                # ``_ncc_label_positions`` into the value-crop block
                # which uses NCC-MASS y instead of the saved lock y.
                log.info(
                    "sc_ocr: MASS-anchor %+d px from saved (cur=%d, "
                    "cal=%d, ncc=%.2f) — locks are stale, using "
                    "NCC-derived row positions this scan",
                    _proposed_drift, _cur_mass_y, _cal_mass_y,
                    _mass_match["score"],
                )
                # Leave _cal_drift_y = 0 so the lock-apply block
                # checks _ncc_label_positions and prefers it.
        elif _mass_match is not None and _mass_match.get("score", 0) >= 0.50:
            log.debug(
                "sc_ocr: MASS NCC found at y=%d (no calibration to "
                "compare against)",
                _mass_match["y"],
            )
        else:
            log.debug(
                "sc_ocr: MASS NCC anchor not confident enough "
                "(score=%.2f) — no drift correction this scan",
                (_mass_match or {}).get("score", 0),
            )
    except Exception as _drift_exc:
        log.debug("anchor-drift compute failed: %s", _drift_exc)
        _cal_drift_y = 0
        _ncc_label_positions = {}

    if mineral_row is None:
        # No panel visible — reset consensus buffers AND drop any
        # locked field values for this region. The user looked away
        # from the rock; next rock starts fresh.
        _reset_consensus_buffers()
        _field_lock_cache.pop(_region_key(region), None)
        # Also drop the SCAN RESULTS anchor cache — next rock might
        # have a slightly different panel position if the user moved.
        try:
            from ..onnx_hud_reader import _scan_results_anchor_cache
            _scan_results_anchor_cache.clear()
        except Exception:
            pass
        # Write a (mostly-empty) overlay so the viewer reflects
        # "no panel detected" instead of stale data.
        if _dbg is not None:
            try:
                _dbg.write()
            except Exception:
                pass
        return empty

    result = dict(empty)
    result["panel_visible"] = True

    # ── Field-value lock fast-path ──
    # If all three fields are already locked from previous scans,
    # short-circuit the entire OCR pipeline. This is the steady-state
    # behavior once the user has stopped on a rock — we read each
    # field once, validate it, and then just return the locked values
    # until the panel disappears OR until any field's crop drifts
    # from its stored fingerprint (drop the lock, re-OCR).
    _rk = _region_key(region)
    _locks = _field_lock_cache.get(_rk, {})
    if (
        "mass" in _locks
        and "resistance" in _locks
        and "instability" in _locks
    ):
        # All-locked steady state. We STILL run label-row detection
        # and per-field crop save + NCC self-invalidation, otherwise
        # locks set under wrong row geometry can never recover (and
        # the live debug viewer goes stale). What we skip is the
        # CNN classification + Tesseract fallback per field — that's
        # where the real cost is.
        try:
            from ..onnx_hud_reader import _find_label_rows
            _label_rows_for_validation = _find_label_rows(img)
        except Exception as _exc:
            log.debug("label_rows in lock-validation failed: %s", _exc)
            _label_rows_for_validation = {}

        # Telemetry: push the row geometry into the debug overlay
        # even on the locked fast path.
        if _dbg is not None:
            _dbg.set_label_rows(_label_rows_for_validation)

        # Per-field crop save + NCC drift check.
        for _field in ("mass", "resistance", "instability"):
            _entry = _label_rows_for_validation.get(_field)
            if _entry is None:
                # No row geometry — touch the crop file with a
                # placeholder so the live viewer's mtime advances
                # (otherwise stale crops mask the failure).
                try:
                    _placeholder = Image.new(
                        "RGB", (200, 30), (40, 20, 20),
                    )
                    _placeholder.save(f"debug_value_{_field}_crop.png")
                except Exception:
                    pass
                if _field == "mass":
                    result["mass"] = _locks["mass"][0]
                elif _field == "resistance":
                    result["resistance"] = _locks["resistance"][0]
                elif _field == "instability":
                    result["instability"] = _locks["instability"][0]
                if _dbg is not None:
                    _dbg.set_lock(_field, _locks[_field][0])
                continue
            _y1, _y2, _lr = _entry
            try:
                _vc = _find_value_crop(
                    img, gray, _y1, _y2,
                    x_min=max(0, _lr + 6),
                )
            except Exception:
                _vc = None
            _locked_val, _locked_fp = _locks[_field]
            if _vc is None:
                # Value-column extraction failed — but we STILL save
                # a placeholder crop (the full row strip) so the
                # live viewer reflects what's currently on screen
                # instead of going stale. Otherwise the user can't
                # tell if the panel finder is misaligned.
                try:
                    _placeholder = img.crop((0, _y1, img.width, _y2))
                    _placeholder.save(f"debug_value_{_field}_crop.png")
                except Exception:
                    pass
                # Preserve the locked value (we can't re-OCR without a
                # value crop).
                if _field == "mass":
                    result["mass"] = _locked_val
                elif _field == "resistance":
                    result["resistance"] = _locked_val
                elif _field == "instability":
                    result["instability"] = _locked_val
                if _dbg is not None:
                    _dbg.set_lock(_field, _locked_val)
                continue
            # Save current crop for the live viewer.
            try:
                _vc.save(f"debug_value_{_field}_crop.png")
            except Exception:
                pass
            # Push value-crop box into telemetry.
            if _dbg is not None:
                try:
                    _vc_w, _vc_h = _vc.size
                    _ax_left = max(0, _lr + 6)
                    _ax_right = min(img.width, _ax_left + _vc_w)
                    _dbg.set_value_crop(
                        _field,
                        (_ax_left, _y1, _ax_right, _y1 + _vc_h),
                    )
                except Exception:
                    pass
            # NCC drift check vs stored fingerprint.
            _current_fp = _crop_fingerprint(_vc)
            _drift_ncc = 1.0
            if _current_fp is not None and _locked_fp is not None:
                _drift_ncc = float(np.dot(_current_fp, _locked_fp) / len(_current_fp))
            if _drift_ncc < _LOCK_INVALIDATE_NCC:
                log.info(
                    "sc_ocr: LOCK INVALIDATED field=%s in fast-path "
                    "(crop drifted, NCC=%.2f < %.2f)",
                    _field, _drift_ncc, _LOCK_INVALIDATE_NCC,
                )
                if _dbg is not None:
                    _dbg.set_lock(_field, None, invalidated=True)
                # Drop the lock and the field's value (becomes None).
                # The next scan will go through the full per-field
                # OCR loop below, since not-all-locked anymore.
                del _field_lock_cache[_rk][_field]
                _RECENT_READS[_field].clear()
                _RECENT_CROPS[_field].clear()
                # Force fall-through to the full OCR path
                _locks = _field_lock_cache.get(_rk, {})
                break
            # Lock holds — return locked value.
            if _field == "mass":
                result["mass"] = _locked_val
            elif _field == "resistance":
                result["resistance"] = _locked_val
            elif _field == "instability":
                result["instability"] = _locked_val
            if _dbg is not None:
                _dbg.set_lock(_field, _locked_val)
        else:
            # No invalidation — all locks hold. Cache mineral name
            # (one-shot OCR) and return.
            _cached_mineral = _locks.get("_mineral_name")
            if _cached_mineral is not None:
                result["mineral_name"] = _cached_mineral[0]
            else:
                _mineral_entry = _label_rows_for_validation.get("_mineral_row")
                if _mineral_entry is not None:
                    try:
                        _my1, _my2, _mlr = _mineral_entry
                        _mname = _ocr_mineral_name(img, _my1, _my2, _mlr)
                        if _mname:
                            _locks["_mineral_name"] = (_mname, None)
                            result["mineral_name"] = _mname
                            if _dbg is not None:
                                _dbg.set_ocr_text("mineral", _mname, [1.0])
                    except Exception as _exc:
                        log.debug("mineral OCR fast-path failed: %s", _exc)
            elapsed_ms = (time.time() - t0) * 1000
            log.info(
                "sc_ocr: ALL LOCKED mineral=%s mass=%s resistance=%s instability=%s in %.0fms",
                result.get("mineral_name"),
                result["mass"], result["resistance"], result["instability"],
                elapsed_ms,
            )
            if _dbg is not None:
                try:
                    _dbg.write()
                except Exception:
                    pass
            return result
        # If we reach here, a lock was invalidated — fall through to
        # the full per-field OCR loop below to re-establish reads.
        return result

    H, W = gray.shape

    # Use Tesseract label detection to find the EXACT positions of
    # MASS/RESISTANCE/INSTABILITY labels. This handles ANY rock type
    # (different mineral names shift the layout). 3 Tesseract calls
    # for label detection + 3 for values = ~300ms total, vs legacy's
    # 12-15 calls at 600ms+.
    from ..onnx_hud_reader import _find_label_rows, _set_current_region
    # Stash region so _find_label_rows can do persistent calibration lookup
    _set_current_region(region)
    label_rows = _find_label_rows(img)
    if _dbg is not None:
        _dbg.set_image(img)
        _dbg.set_label_rows(label_rows)
        # Write the overlay immediately after label-row detection so
        # the viewer always reflects what the panel finder produced,
        # even if downstream OCR raises an exception. End-of-scan
        # write() below will overwrite with the fully-populated
        # version including OCR text + lock state.
        try:
            _dbg.write()
        except Exception:
            pass

    # ── Detect difficulty label once per scan ──
    # The EASY / MEDIUM / HARD / EXTREME / IMPOSSIBLE label is rendered
    # as a single large word below the instability row. Reading it
    # gives us a game-logic prior that bounds instability's valid
    # range — a correctly-placed 'EASY' tag means instability ≈ 0-25.
    # Reuses the full-panel Tesseract pass from _find_label_rows
    # (re-runs because that function doesn't expose the raw text).
    _difficulty: Optional[str] = None
    try:
        from . import priors
        import pytesseract as _pt
        from ..screen_reader import _check_tesseract
        if _check_tesseract():
            # Try multiple Tesseract configurations — the difficulty
            # label is inside a colored progress bar (EASY = green,
            # HARD = red) that Tesseract doesn't always see at PSM 11.
            # PSM 6 (uniform block) + both polarities + two crop regions
            # catches more cases. First hit wins.
            _left = img.crop((0, 0, int(img.width * 0.55), img.height))
            _left_gray = np.array(_left.convert("L"), dtype=np.uint8)
            _rgb = np.array(_left.convert("RGB"), dtype=np.uint8)
            _max_ch = _rgb.max(axis=2).astype(np.uint8)  # catches colored labels
            _thr = _otsu(_left_gray)
            _thr_c = _otsu(_max_ch)

            _variants = [
                ("gray_bright_psm11", np.where(_left_gray > _thr, 0, 255).astype(np.uint8), "--psm 11"),
                ("gray_bright_psm6",  np.where(_left_gray > _thr, 0, 255).astype(np.uint8), "--psm 6"),
                ("max_bright_psm6",   np.where(_max_ch > _thr_c, 0, 255).astype(np.uint8), "--psm 6"),
                ("gray_dark_psm11",   np.where(_left_gray < _thr, 0, 255).astype(np.uint8), "--psm 11"),
            ]
            for _name, _bw, _cfg in _variants:
                try:
                    _t = _pt.image_to_string(Image.fromarray(_bw), config=_cfg)
                except Exception:
                    continue
                _d = priors.detect_difficulty(_t)
                if _d:
                    _difficulty = _d
                    log.info(
                        "sc_ocr: difficulty detected=%r (via %s)",
                        _difficulty, _name,
                    )
                    break
            if _difficulty is None:
                log.debug("sc_ocr: difficulty not detected (tried 4 variants)")
    except Exception as _exc:
        log.debug("sc_ocr: difficulty detection failed: %s", _exc)

    # Fallback to fixed offsets from mineral row if label detection fails
    if not label_rows:
        mr_center = (mineral_row[0] + mineral_row[1]) // 2
        scale = H / 541
        _ROW_H = int(15 * scale)
        for field, off, lr in [("mass",43,110),("resistance",82,200),("instability",120,205)]:
            c = mr_center + int(off * scale)
            label_rows[field] = (max(0,c-_ROW_H), min(H,c+_ROW_H), int(lr*scale))

    fields = ["mass", "resistance", "instability"]

    for field in fields:
        # Locked-field fast path with self-invalidation.
        # We always compute the current value crop (cheap), save it
        # for the live viewer, and compare it against the stored
        # fingerprint that was in effect when the lock fired. If
        # the fingerprint similarity drops below
        # _LOCK_INVALIDATE_NCC, the panel content has changed under
        # us — drop the lock and fall through to full OCR.
        if field in _locks:
            _locked_val, _locked_fp = _locks[field]
            try:
                _entry = label_rows.get(field)
                _current_vc = None
                if _entry is not None:
                    _y1, _y2, _lr = _entry
                    if _y2 > _y1 and (_y2 - _y1) >= 6:
                        _current_vc = _find_value_crop(
                            img, gray, _y1, _y2,
                            x_min=max(0, _lr + 6),
                        )
                if _current_vc is not None:
                    _current_vc.save(f"debug_value_{field}_crop.png")
                    _current_fp = _crop_fingerprint(_current_vc)
                    if _current_fp is not None and _locked_fp is not None:
                        _sim = float(np.dot(_current_fp, _locked_fp) / len(_current_fp))
                        if _sim < _LOCK_INVALIDATE_NCC:
                            log.info(
                                "sc_ocr: LOCK INVALIDATED field=%s "
                                "(crop drifted, NCC=%.2f < %.2f) — re-OCR",
                                field, _sim, _LOCK_INVALIDATE_NCC,
                            )
                            if _dbg is not None:
                                _dbg.set_lock(field, None, invalidated=True)
                            del _field_lock_cache[_rk][field]
                            # Also flush per-field consensus + crop
                            # buffers so a stale value doesn't lock
                            # back in immediately.
                            _RECENT_READS[field].clear()
                            _RECENT_CROPS[field].clear()
                            # Fall through to full OCR below
                            _locks = _field_lock_cache.get(_rk, {})
                        else:
                            # Lock is still valid — use it.
                            if field == "mass":
                                result["mass"] = _locked_val
                            elif field == "resistance":
                                result["resistance"] = _locked_val
                            elif field == "instability":
                                result["instability"] = _locked_val
                            if _dbg is not None:
                                _dbg.set_lock(field, _locked_val)
                                # Reconstruct crop box for overlay
                                try:
                                    _vc_w, _vc_h = _current_vc.size
                                    _ax_left = max(0, _lr + 6)
                                    _ax_right = min(img.width, _ax_left + _vc_w)
                                    _dbg.set_value_crop(
                                        field,
                                        (_ax_left, _y1, _ax_right, _y1 + _vc_h),
                                    )
                                except Exception:
                                    pass
                            continue
                else:
                    # Couldn't compute a current crop — save a
                    # placeholder so the live viewer's mtime
                    # advances even when value-crop extraction
                    # fails. Without this, a locked field whose
                    # row geometry can't produce a clean crop
                    # goes stale for the duration of the lock.
                    try:
                        if _entry is not None:
                            _y1, _y2, _lr = _entry
                            if _y2 > _y1 and (_y2 - _y1) >= 4:
                                _placeholder = img.crop(
                                    (0, _y1, img.width, _y2),
                                )
                            else:
                                _placeholder = Image.new(
                                    "RGB", (200, 30), (40, 20, 20),
                                )
                        else:
                            _placeholder = Image.new(
                                "RGB", (200, 30), (40, 20, 20),
                            )
                        _placeholder.save(f"debug_value_{field}_crop.png")
                    except Exception:
                        pass
                    if field == "mass":
                        result["mass"] = _locked_val
                    elif field == "resistance":
                        result["resistance"] = _locked_val
                    elif field == "instability":
                        result["instability"] = _locked_val
                    continue
            except Exception as _exc:
                log.debug(
                    "sc_ocr: lock-validation failed for %s: %s — "
                    "keeping lock", field, _exc,
                )
                # Even on exception, touch the file so we know the
                # path is being reached.
                try:
                    _ph = Image.new("RGB", (200, 30), (60, 20, 20))
                    _ph.save(f"debug_value_{field}_crop.png")
                except Exception:
                    pass
                if field == "mass":
                    result["mass"] = _locked_val
                elif field == "resistance":
                    result["resistance"] = _locked_val
                elif field == "instability":
                    result["instability"] = _locked_val
                continue

        entry = label_rows.get(field)
        if entry is None:
            log.info("sc_ocr: field=%s MISSING from label_rows (panel layout?)", field)
            continue
        y1, y2, lr = entry

        # Sanity-check the row geometry. In fracture/extraction mode
        # the panel is positioned differently than ship-scan mode, and
        # fixed offsets from the mineral row can shoot past the image
        # bottom — returning (y1=547, y2=541) when image is only 541
        # tall. The full-row OCR path would then crop an empty strip
        # and Tesseract would hallucinate garbage like '¤- ¤8'.
        # Skip the field outright if the geometry is inverted or
        # degenerate (< 6 px tall).
        if y1 >= y2 or (y2 - y1) < 6 or y2 > img.height or y1 < 0:
            log.info(
                "sc_ocr: field=%s row geometry invalid y=%d-%d "
                "(img_h=%d) — skipping", field, y1, y2, img.height,
            )
            continue

        # Full-row OCR runs before _find_value_crop; the full-row path
        # has its own sanity checks and can still succeed when the
        # tight crop would fail. Compute value_crop for the slow-path
        # fallback but don't bail if it's None — row OCR may carry.
        #
        # CALIBRATION OVERRIDE: if the user locked a box for this
        # field, crop THAT box directly. Auto-detection via
        # _find_value_crop uses `lr + 6` as x_min, where `lr` is the
        # shared value_column_left across rows — this frequently
        # overshoots past the leading digit when the user's locked
        # box starts slightly to the left of the widest label's
        # colon position (e.g. instability "2.22" starting at x=193
        # when lr=196 → x_min=202 cuts off "2."). Respect the lock
        # verbatim when present; it's exactly what the calibration
        # dialog previewed.
        value_crop = None
        try:
            from . import calibration as _cal_mod
            _locked_box = _cal_mod.get_row(region, field, dy=_cal_drift_y)
            # Tier-2 override: if the anchor revealed locks are stale
            # (large drift) but found this row's label via NCC, use
            # the NCC-derived y for the value crop. Keep the saved
            # x/w/h since the X column doesn't drift in normal play.
            if (
                _cal_drift_y == 0  # no drift applied (tier 1 didn't trigger)
                and _ncc_label_positions  # but we DID find labels via NCC
                and field in _ncc_label_positions
                and _locked_box is not None
            ):
                _ncc_match = _ncc_label_positions[field]
                _saved_y = int(_locked_box["y"])
                _ncc_y = int(_ncc_match["y"])
                if abs(_ncc_y - _saved_y) > 25:
                    log.info(
                        "sc_ocr: tier-2 override for %s: lock y=%d "
                        "→ NCC y=%d", field, _saved_y, _ncc_y,
                    )
                    _locked_box = {
                        "x": int(_locked_box["x"]),
                        "y": _ncc_y,
                        "w": int(_locked_box["w"]),
                        "h": int(_locked_box["h"]),
                    }
        except Exception:
            _locked_box = None
        if _locked_box is not None:
            try:
                _bx = int(_locked_box["x"])
                _by = int(_locked_box["y"])
                _bw = int(_locked_box["w"])
                _bh = int(_locked_box["h"])
                _x0 = max(0, _bx)
                _y0 = max(0, _by)
                _x1 = min(img.width, _bx + _bw)
                _y1 = min(img.height, _by + _bh)
                if _x1 - _x0 >= 4 and _y1 - _y0 >= 6:
                    _candidate = img.crop((_x0, _y0, _x1, _y1))
                    # Sanity-check the lock against the actual pixels.
                    # Calibrated boxes can drift off-target when the
                    # panel slides inside the captured region. We
                    # require BOTH:
                    #
                    #   (a) the crop has digit-like ink density
                    #       (5-45 % of pixels above text threshold —
                    #       below 5 % means empty background, above
                    #       45 % means a solid block like the
                    #       difficulty bar);
                    #   (b) the bright pixels form ≥1 distinct
                    #       vertical column cluster (real digit
                    #       crops have at least 1 column-group of
                    #       ink; a row band with no digits has no
                    #       structured columns).
                    #
                    # If either fails, drop the lock for THIS scan
                    # only and let auto-detect run.
                    try:
                        _gc = np.asarray(_candidate.convert("L"), dtype=np.uint8)
                        if float(np.median(_gc)) > 130:
                            _gc = 255 - _gc
                        _bin = (_gc > 80).astype(np.uint8)
                        _area = max(1, _bin.size)
                        _density = float(_bin.sum()) / float(_area)
                        if not (0.05 <= _density <= 0.45):
                            log.info(
                                "sc_ocr: locked crop for %s has out-of-"
                                "range ink density %.3f — falling back "
                                "to auto-detect this scan",
                                field, _density,
                            )
                        else:
                            # Column-cluster check. Project bright
                            # pixels onto x-axis; count runs of
                            # ink-bearing columns. ≥1 run = at least
                            # one digit-shaped vertical band.
                            _col_proj = _bin.sum(axis=0) > 1
                            _runs = int(np.sum(
                                np.diff(_col_proj.astype(np.int8)) == 1
                            )) + (1 if _col_proj[0] else 0)
                            if _runs >= 1:
                                value_crop = _candidate
                            else:
                                log.info(
                                    "sc_ocr: locked crop for %s has no "
                                    "vertical column structure — "
                                    "falling back to auto-detect",
                                    field,
                                )
                    except Exception:
                        # On any sanity-check failure, accept the
                        # lock anyway — better than dropping a real
                        # crop on a transient numpy hiccup.
                        value_crop = _candidate
            except Exception:
                value_crop = None
        if value_crop is None:
            value_crop = _find_value_crop(img, gray, y1, y2, x_min=max(0, lr + 6))
        # Telemetry: record the value crop box for the debug overlay.
        if _dbg is not None and value_crop is not None:
            try:
                _vc_w, _vc_h = value_crop.size
                # value_crop is cropped from img; we need its position
                # in img coords. Reconstruct via the bounds used
                # inside _find_value_crop (x_min + small offset, y1).
                # For overlay purposes, the right-anchored crop ends
                # near img.width and starts at img.width - _vc_w.
                # Use a heuristic: pick centered around shared lr.
                _approx_x_left = max(0, lr + 6)
                _approx_x_right = min(img.width, _approx_x_left + _vc_w)
                _dbg.set_value_crop(
                    field, (_approx_x_left, y1, _approx_x_right, y1 + _vc_h),
                )
            except Exception:
                pass
        if value_crop is None:
            # _find_value_crop failed (often happens when the value
            # is a single thin digit like "1" that doesn't meet the
            # _MIN_VALUE_WIDTH cluster filter). Save the full row
            # strip to BOTH the diagnostic file AND the live-viewer
            # crop file so the viewer reflects current geometry
            # instead of going stale.
            try:
                _debug_row = img.crop((0, max(0, y1 - 2), img.width, min(img.height, y2 + 2)))
                _debug_row.save(f"debug_row_{field}_failed.png")
                # Crop the value column area (right of label) so the
                # live viewer shows what the OCR was looking at.
                _vc_left = max(0, lr + 6)
                _vc_right = min(img.width, _vc_left + int(img.width * 0.30))
                if _vc_right > _vc_left:
                    _row_crop = img.crop(
                        (_vc_left, max(0, y1 - 2), _vc_right, min(img.height, y2 + 2)),
                    )
                    _row_crop.save(f"debug_value_{field}_crop.png")
            except Exception:
                pass
            log.info(
                "sc_ocr: field=%s value_crop is None "
                "(y=%d-%d x_lr=%d saved debug_row_%s_failed.png)",
                field, y1, y2, lr, field,
            )
            continue

        # Save successful crops on EVERY scan so the live viewer
        # (scripts/live_crop_viewer.py) can show what the OCR sees in
        # real time. Old behavior was once-per-process; live viewer
        # needs the file to update on each scan.
        try:
            value_crop.save(f"debug_value_{field}_crop.png")
        except Exception:
            pass

        # Capture every value crop to a pending/ buffer for later manual
        # labeling + retraining. Rate-limited internally to ~5 s per
        # field, so the hot path stays cheap.
        try:
            from ..training_collector import save_pending_crop
            save_pending_crop(value_crop, field)
        except Exception:
            pass

        # ── Value-crop OCR (PRIMARY — custom CNN inside) ──
        # Run the value-crop path FIRST. The user-trained 28×28 CNN
        # has 99% val_acc on real SC HUD glyphs and is wired as the
        # top voter inside _ocr_value_crop. Only fall back to the
        # full-row Tesseract path when the value-crop OCR returns
        # nothing or doesn't validate.
        text, confs = _ocr_value_crop(value_crop, field=field)
        _valid_primary = None
        if text:
            if field == "mass":
                _valid_primary = validate.validate_mass(text)
            elif field == "resistance":
                _valid_primary = validate.validate_pct(text)
            elif field == "instability":
                _valid_primary = validate.validate_instability(text, confidences=confs)
        if _valid_primary is None:
            # Primary failed — try full-row Tesseract as fallback.
            row_text, row_confs = _ocr_full_row(img, y1, y2, field)
            if row_text:
                _valid_row = None
                if field == "mass":
                    _valid_row = validate.validate_mass(row_text)
                elif field == "resistance":
                    _valid_row = validate.validate_pct(row_text)
                elif field == "instability":
                    _valid_row = validate.validate_instability(row_text, confidences=row_confs)
                if _valid_row is not None:
                    log.debug(
                        "sc_ocr: PRIMARY failed for %s, using row-fallback %r",
                        field, row_text,
                    )
                    text, confs = row_text, row_confs
        if not text:
            log.info("sc_ocr: field=%s ocr returned empty text", field)
            continue

        log.info("sc_ocr raw %s: text=%r confs=%s", field, text,
                 [f"{c:.2f}" for c in confs[:8]])
        if _dbg is not None:
            _dbg.set_ocr_text(field, text, confs)
        if field == "mass":
            raw_val = validate.validate_mass(text)
        elif field == "resistance":
            raw_val = validate.validate_pct(text)
        elif field == "instability":
            raw_val = validate.validate_instability(text, confidences=confs)
        else:
            raw_val = None

        # ── Game-logic priors + NCC template fallback ──
        # If the voted value contradicts game knowledge (e.g. EASY
        # difficulty but instability=278), try the NCC template voter
        # as a fourth opinion. Templates are deterministic — 100%
        # accurate when the font matches — so they're the right
        # tiebreaker when all three neural/heuristic engines disagree
        # with the rock's observed difficulty.
        try:
            from . import priors as _priors
            _ctx = {"difficulty": _difficulty} if _difficulty else {}

            # ── Proactive decimal recovery for instability ──
            # Most mineable rocks have instability in the 0-30 range,
            # with rare edge cases up to ~200. A raw read ≥ 30 that
            # contains NO decimal point almost certainly lost one —
            # e.g. `4.65` → `465`, `12.10` → `1210`. We try this BEFORE
            # the plausibility check so we don't depend on difficulty
            # detection (which misses when the EASY/MEDIUM bar has
            # non-standard polarity, e.g. white text on green).
            if (field == "instability"
                    and raw_val is not None
                    and float(raw_val) >= 30.0
                    and "." not in (text or "")):
                _recovered = _priors.try_decimal_recovery(field, text, _ctx)
                if _recovered is not None and 0.0 <= _recovered <= 200.0:
                    log.info(
                        "sc_ocr: proactive-decimal-recover field=%s "
                        "raw=%r orig_val=%s -> %s",
                        field, text, raw_val, _recovered,
                    )
                    raw_val = _recovered

            _ok = True
            if raw_val is not None:
                _ok, _reason = _priors.is_plausible(field, float(raw_val), _ctx)
                if not _ok:
                    # Second-chance decimal recovery when priors reject
                    # (e.g. difficulty IS detected and bounds say value
                    # is out-of-range).
                    _recovered = _priors.try_decimal_recovery(field, text, _ctx)
                    if _recovered is not None:
                        log.info(
                            "sc_ocr: prior-decimal-recover field=%s "
                            "raw=%r rejected_val=%s -> %s",
                            field, text, raw_val, _recovered,
                        )
                        raw_val = _recovered
                        _ok = True
                    else:
                        log.info(
                            "sc_ocr: prior-reject field=%s val=%s (%s) — "
                            "trying NCC templates",
                            field, raw_val, _reason,
                        )
            if (raw_val is None) or (not _ok):
                # Template-voter fallback
                try:
                    from .. import templates_furore as _tf
                    _ttext, _tconfs = _tf.match_value_crop(value_crop)
                    if _ttext:
                        _mean = sum(_tconfs) / len(_tconfs) if _tconfs else 0.0
                        if field == "mass":
                            _tv = validate.validate_mass(_ttext)
                        elif field == "resistance":
                            _tv = validate.validate_pct(_ttext)
                        elif field == "instability":
                            _tv = validate.validate_instability(_ttext, confidences=_tconfs)
                        else:
                            _tv = None
                        _t_ok = False
                        if _tv is not None:
                            _t_ok, _ = _priors.is_plausible(field, float(_tv), _ctx)
                        log.info(
                            "sc_ocr: templates field=%s text=%r val=%s "
                            "mean=%.2f plausible=%s",
                            field, _ttext, _tv, _mean, _t_ok,
                        )
                        if _tv is not None and _t_ok and _mean >= 0.55:
                            raw_val = _tv
                except Exception as _texc:
                    log.debug("sc_ocr: template voter failed: %s", _texc)
        except Exception as _pexc:
            log.debug("sc_ocr: priors check failed: %s", _pexc)

        if field == "mass":
            result["mass"] = _consensus_value("mass", raw_val)
        elif field == "resistance":
            result["resistance"] = _consensus_value("resistance", raw_val)
        elif field == "instability":
            result["instability"] = _consensus_value("instability", raw_val)

        # ── Push crop fingerprint into the per-field buffer ──
        # Used by the pre-lock verifier to confirm the underlying
        # crop pixels are stable across the lock window (catches the
        # "row jumped to a progress bar" case where OCR text might
        # coincidentally match but the crop content is unrelated).
        _RECENT_CROPS[field].append(_crop_fingerprint(value_crop))

        # ── Strict lock gate ──
        # Two independent checks must both pass:
        #   (1) ALL N reads in the window agreed on the same value
        #       (much stricter than _consensus_value's 2-of-3 — a
        #       coincidental misread can satisfy 2-of-3 but rarely
        #       satisfies all-of-N).
        #   (2) Mean pairwise NCC of the last N CROP IMAGES is ≥
        #       _LOCK_CROP_NCC_MIN — the row crop has been visually
        #       stable, not jumping between targets.
        # If either fails, no lock this frame; we keep evaluating
        # next scan with the rolling window.
        if field not in _locks:
            _unanimous = _value_buffer_unanimous(field)
            _crop_ok, _crop_sim = _crop_buffer_consistent(field)
            _displayed = result.get(field)
            if (
                _unanimous is not None
                and _displayed is not None
                and float(_unanimous) == float(_displayed)
                and _crop_ok
            ):
                # Store the most recent crop fingerprint alongside
                # the value; lock self-invalidation compares against
                # this on subsequent scans to detect drift.
                _fp = _crop_fingerprint(value_crop)
                _locks_for_region = _field_lock_cache.setdefault(_rk, {})
                _locks_for_region[field] = (float(_unanimous), _fp)
                log.info(
                    "sc_ocr: LOCKED field=%s value=%s "
                    "(unanimous %d/%d frames, crop-NCC=%.2f)",
                    field, _unanimous, _LOCK_WINDOW, _LOCK_WINDOW, _crop_sim,
                )
                if _dbg is not None:
                    _dbg.set_lock(field, float(_unanimous))
            else:
                log.debug(
                    "sc_ocr: lock-gate field=%s unanimous=%s "
                    "crop_ok=%s crop_sim=%.2f (need %.2f)",
                    field, _unanimous, _crop_ok, _crop_sim,
                    _LOCK_CROP_NCC_MIN,
                )

    # ── Mineral name (placeholder Tesseract → snap to KNOWN_MINERALS) ──
    # The mineral row is surfaced under the "_mineral_row" key by
    # _find_label_rows_by_position. Tesseract is the placeholder OCR
    # here; when the SC alphabet CNN is trained, we swap it inside
    # _ocr_mineral_name. The fuzzy snap to KNOWN_MINERALS keeps the
    # final read clean regardless of OCR quality.
    _mineral_entry = label_rows.get("_mineral_row")
    if _mineral_entry is not None:
        try:
            _my1, _my2, _mlr = _mineral_entry
            # CALIBRATION OVERRIDE for mineral row: prefer the user's
            # locked box when present. The shared value_column_left
            # `_mlr` is derived from the NUMERIC rows (mass/resistance/
            # instability label ends), which sit ~180 px into the panel
            # — way to the right of where the mineral name text starts
            # (usually x=11). Passing that lr into _ocr_mineral_name
            # crops from x=_mlr-20, which chops off the entire mineral
            # name and leaves only the trailing ")" or "(ORE)" — hence
            # garbage reads like 'Elo' or 'Eg fT'. When the user has
            # locked the mineral row, its box['x'] is the real start.
            try:
                from . import calibration as _cal_mod
                _mineral_box = _cal_mod.get_row(region, "_mineral_row")
            except Exception:
                _mineral_box = None
            if _mineral_box is not None:
                try:
                    _mlr = max(0, int(_mineral_box["x"]) + 20)
                except Exception:
                    pass
            _mineral_name = _ocr_mineral_name(img, _my1, _my2, _mlr)
            result["mineral_name"] = _mineral_name
            if _dbg is not None and _mineral_name:
                _dbg.set_ocr_text("mineral", _mineral_name, [1.0])
            # Save the mineral row crop so the calibration dialog can
            # display it as a live preview.
            #
            # The mineral name (e.g. "ALUMINUM (ORE)") sits on the
            # LEFT side of the row — NOT in the value column. So we
            # crop the FULL row width starting from the panel's left
            # margin (matches where MASS row content begins). This
            # ensures the entire mineral name is visible in the
            # preview, not just the trailing parenthesis.
            try:
                if _my2 > _my1 and img.width > 0:
                    _mineral_crop = img.crop((0, _my1, img.width, _my2))
                    _mineral_crop.save("debug_value__mineral_row_crop.png")
            except Exception:
                pass
        except Exception as _mexc:
            log.debug("sc_ocr: mineral name read failed: %s", _mexc)

    elapsed_ms = (time.time() - t0) * 1000
    log.info(
        "sc_ocr: mineral=%s mass=%s resistance=%s instability=%s in %.0fms",
        result.get("mineral_name"),
        result["mass"], result["resistance"], result["instability"],
        elapsed_ms,
    )
    if _dbg is not None:
        try:
            _dbg.write()
        except Exception:
            pass
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
