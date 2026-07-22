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
import threading
import time
from typing import Optional

import numpy as np
from PIL import Image

from .screen_reader import _check_tesseract

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

# Lazy-loaded
_session = None
_char_classes: str = "0123456789.-%"

# Label-row cache: maps (x, y, w, h) region key → (timestamp, rows).
# Tesseract label OCR is expensive (~3 subprocess spawns, ~500 ms)
# but labels don't move within a rock — cache and reuse. Cache is
# cleared when the panel disappears or after TTL expires.
_label_cache: dict[tuple[int, int, int, int], tuple[float, dict]] = {}
_LABEL_CACHE_TTL_SEC = 60.0  # safe upper bound; rocks scan for <60s


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

        # CRITICAL: cap ONNX threading. Default behaviour spawns one
        # worker per CPU core, which on multi-core systems pegs every
        # core during inference and starves the Qt GUI thread → all
        # app windows go "Not Responding" while a scan is in flight.
        # 1 intra + 1 inter is plenty for these tiny (28×28 / 32×128)
        # crops; the cost saved on thread coordination outweighs any
        # parallelism gain at this input size.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        _session = ort.InferenceSession(
            model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
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
        # Same thread-cap rationale as the initial session above —
        # don't let the hot-swapped session run unbounded.
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        new_session = ort.InferenceSession(
            new_model_path,
            sess_options=opts,
            providers=["CPUExecutionProvider"],
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
# Startup pre-warm
# ─────────────────────────────────────────────────────────────

# Guard against double pre-warm (e.g. _on_data_loaded firing twice
# on a forced refresh). The first call does the work, every later
# call short-circuits.
_prewarm_started: bool = False
_prewarm_lock = threading.Lock()


def prewarm_models() -> None:
    """Eagerly load every ONNX session used by the HUD scan path.

    Cold-starting ``onnxruntime.InferenceSession`` plus the first
    inference takes 10-20 s on Windows for the CRNN models, so doing
    this lazily on the user's first "Start Scan" click meant the
    initial scan stalled for ~20 s before producing a result.

    Pre-warm is BEST-EFFORT: any failure is logged and swallowed so
    the existing lazy-load fallbacks in ``_ensure_*`` still run on
    the first real scan as before. Safe to call from a background
    daemon thread; concurrent scan-path calls of ``_ensure_*`` are
    fine because each function gates on its module-global ``is None``
    check and Python's GIL makes the assignment atomic — at worst we
    create a session twice and discard one.

    Calling this function more than once is a no-op (gated by
    ``_prewarm_started``), so the caller doesn't need to worry about
    re-firing it from ``_on_data_loaded`` on a manual refresh.
    """
    global _prewarm_started
    with _prewarm_lock:
        if _prewarm_started:
            return
        _prewarm_started = True

    log.info("prewarm: starting ONNX session pre-warm")
    t_total = time.time()

    # 1) Legacy 28x28 HUD CNN in this module.
    t0 = time.time()
    try:
        loaded = _ensure_model()
        elapsed = time.time() - t0
        if loaded and _session is not None:
            log.info("prewarm: model_cnn (legacy) loaded in %.1fs", elapsed)
            # Dummy inference: shape (1, 1, 28, 28) float32 in [0, 1].
            try:
                dummy = np.zeros((1, 1, 28, 28), dtype=np.float32)
                inp_name = _session.get_inputs()[0].name
                _session.run(None, {inp_name: dummy})
                log.info(
                    "prewarm: model_cnn (legacy) first-inference warmed in %.1fs",
                    time.time() - t0,
                )
            except Exception as exc:
                log.warning("prewarm: model_cnn dummy inference failed: %s", exc)
        else:
            log.info(
                "prewarm: model_cnn (legacy) unavailable (skipped, %.1fs)",
                elapsed,
            )
    except Exception as exc:
        log.warning("prewarm: model_cnn load failed: %s", exc)

    # 2) sc_ocr fallback models — primary CNN, inverted CNN, CRNN v1, CRNN v2.
    try:
        from .sc_ocr import fallback as _fb
    except Exception as exc:
        log.warning("prewarm: could not import sc_ocr.fallback: %s", exc)
        _fb = None

    if _fb is not None:
        # Primary 28x28 CNN
        t0 = time.time()
        try:
            if _fb._ensure_model() and _fb._session is not None:
                log.info(
                    "prewarm: sc_ocr CNN loaded in %.1fs", time.time() - t0,
                )
                try:
                    dummy = np.zeros((1, 1, 28, 28), dtype=np.float32)
                    inp_name = _fb._session.get_inputs()[0].name
                    _fb._session.run(None, {inp_name: dummy})
                    log.info(
                        "prewarm: sc_ocr CNN first-inference warmed in %.1fs",
                        time.time() - t0,
                    )
                except Exception as exc:
                    log.warning(
                        "prewarm: sc_ocr CNN dummy inference failed: %s", exc,
                    )
            else:
                log.debug("prewarm: sc_ocr CNN unavailable (skipped)")
        except Exception as exc:
            log.warning("prewarm: sc_ocr CNN load failed: %s", exc)

        # Inverted 28x28 CNN (optional)
        t0 = time.time()
        try:
            if _fb._ensure_model_inv() and _fb._session_inv is not None:
                log.info(
                    "prewarm: sc_ocr CNN-inv loaded in %.1fs",
                    time.time() - t0,
                )
                try:
                    dummy = np.zeros((1, 1, 28, 28), dtype=np.float32)
                    inp_name = _fb._session_inv.get_inputs()[0].name
                    _fb._session_inv.run(None, {inp_name: dummy})
                    log.info(
                        "prewarm: sc_ocr CNN-inv first-inference warmed in %.1fs",
                        time.time() - t0,
                    )
                except Exception as exc:
                    log.warning(
                        "prewarm: sc_ocr CNN-inv dummy inference failed: %s",
                        exc,
                    )
            else:
                log.debug("prewarm: sc_ocr CNN-inv unavailable (skipped)")
        except Exception as exc:
            log.warning("prewarm: sc_ocr CNN-inv load failed: %s", exc)

        # CRNN v1 (the big one — ~10–20 s on first load)
        t0 = time.time()
        try:
            if _fb._ensure_crnn_model() and _fb._crnn_session is not None:
                log.info(
                    "prewarm: sc_ocr CRNN v1 loaded in %.1fs",
                    time.time() - t0,
                )
                try:
                    h = int(_fb._crnn_input_height)
                    # CRNN expects (N, 1, H, W) with W dynamic; use a
                    # reasonable warm-up width so the kernels JIT.
                    dummy = np.zeros((1, 1, h, 64), dtype=np.float32)
                    inp_name = _fb._crnn_session.get_inputs()[0].name
                    _fb._crnn_session.run(None, {inp_name: dummy})
                    log.info(
                        "prewarm: sc_ocr CRNN v1 first-inference warmed in %.1fs",
                        time.time() - t0,
                    )
                except Exception as exc:
                    log.warning(
                        "prewarm: sc_ocr CRNN v1 dummy inference failed: %s",
                        exc,
                    )
            else:
                log.debug("prewarm: sc_ocr CRNN v1 unavailable (skipped)")
        except Exception as exc:
            log.warning("prewarm: sc_ocr CRNN v1 load failed: %s", exc)

        # CRNN v2 (optional ensemble partner)
        t0 = time.time()
        try:
            if _fb._ensure_crnn2_model() and _fb._crnn2_session is not None:
                log.info(
                    "prewarm: sc_ocr CRNN v2 loaded in %.1fs",
                    time.time() - t0,
                )
                try:
                    h = int(_fb._crnn2_input_height)
                    dummy = np.zeros((1, 1, h, 64), dtype=np.float32)
                    inp_name = _fb._crnn2_session.get_inputs()[0].name
                    _fb._crnn2_session.run(None, {inp_name: dummy})
                    log.info(
                        "prewarm: sc_ocr CRNN v2 first-inference warmed in %.1fs",
                        time.time() - t0,
                    )
                except Exception as exc:
                    log.warning(
                        "prewarm: sc_ocr CRNN v2 dummy inference failed: %s",
                        exc,
                    )
            else:
                log.debug("prewarm: sc_ocr CRNN v2 unavailable (skipped)")
        except Exception as exc:
            log.warning("prewarm: sc_ocr CRNN v2 load failed: %s", exc)

    log.info(
        "prewarm: all ONNX sessions ready in %.1fs total",
        time.time() - t_total,
    )


# ─────────────────────────────────────────────────────────────
# Row detection
# ─────────────────────────────────────────────────────────────

def _find_panel_lines(
    gray: np.ndarray,
    min_width_frac: float = 0.18,
    max_thickness: int = 3,
    *,
    y_range: Optional[tuple[int, int]] = None,
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
    horizontal endpoints. Y values are always IMAGE-ABSOLUTE,
    regardless of whether ``y_range`` was used.

    Local search mode
    -----------------
    If ``y_range=(y_lo, y_hi)`` is provided, only rows in
    ``[y_lo, y_hi)`` are scanned. This lets the rigid-body tracker
    say "I know the lines live in this band — don't bother scanning
    the rest". Returned y-centers are still image-absolute. ``y_lo``
    and ``y_hi`` are clamped to ``[0, h]``; if the resulting range is
    empty, the function returns ``[]``.

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

    # Clamp the search band to the image. Default is full-image.
    if y_range is not None:
        y_lo = max(0, int(y_range[0]))
        y_hi = min(h, int(y_range[1]))
    else:
        y_lo, y_hi = 0, h
    if y_hi <= y_lo:
        return []

    mask = _build_text_mask(gray)
    row_density = mask.sum(axis=1)
    min_width = int(w * min_width_frac)

    # Find consecutive runs of high-density rows within [y_lo, y_hi).
    # We iterate one row past the end so a run that touches y_hi-1
    # gets closed off correctly.
    in_run = False
    run_start = 0
    runs: list[tuple[int, int]] = []
    for y in range(y_lo, y_hi + 1):
        d = row_density[y] if y < y_hi else 0
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
# shape + y_range so that the three-way row call (mass, resist,
# instab) inside a single scan only pays the detection cost once,
# AND local-search and full-frame requests for the same gray do not
# stomp each other's cache entries.
_panel_lines_cache: tuple[
    int,                                          # id(gray)
    tuple[int, int],                              # gray.shape
    Optional[tuple[int, int]],                    # y_range
    list[tuple[int, int, int]],                   # lines
] | None = None


def _get_panel_lines_cached(
    gray: np.ndarray,
    *,
    y_range: Optional[tuple[int, int]] = None,
) -> list[tuple[int, int, int]]:
    """Return panel lines for ``gray``, cached per gray-array identity.

    Three rows in a single scan share the same gray; cache hits keep
    repeated calls free.

    Detection hierarchy:
      PRIMARY  — ``hud_tracker.anchors.chrome_lines.find_chrome_lines``,
                 the structural detector with vertical-isolation +
                 inter-candidate gap discriminators that reject the
                 COMPOSITION-pair lines (the legacy detector's known
                 failure mode). Returns top_line / bot_line dicts; we
                 fold them into the legacy ``(y_center, x_left,
                 x_right)`` tuple shape.
      FALLBACK — legacy ``_find_panel_lines`` for any case where the
                 chrome_lines detector returns nothing (defensive — we
                 never want to return zero lines just because the
                 primary went home empty).

    Local search mode
    -----------------
    When ``y_range=(y_lo, y_hi)`` is provided, results are restricted
    to lines whose y-center lies within that band. The primary
    ``find_chrome_lines`` detector is run full-frame as today and
    then post-filtered (it doesn't expose a y-range parameter
    itself, but it's fast and runs through its own internal caches).
    The fallback ``_find_panel_lines`` receives ``y_range`` directly
    and skips scanning outside the band entirely.

    Returned y-centers are always image-absolute. Cache key is
    extended with ``y_range`` so a local-search call cannot return a
    stale full-frame cached list (or vice-versa).
    """
    global _panel_lines_cache
    yr_norm: Optional[tuple[int, int]] = (
        (int(y_range[0]), int(y_range[1])) if y_range is not None else None
    )
    key = (id(gray), gray.shape, yr_norm)
    if _panel_lines_cache is not None:
        cid, cshape, cyr, clines = _panel_lines_cache
        if cid == key[0] and cshape == key[1] and cyr == key[2]:
            return clines

    lines: list[tuple[int, int, int]] = []
    try:
        from hud_tracker.anchors.chrome_lines import find_chrome_lines
        cl = find_chrome_lines(gray)
        for key_name in ("top_line", "bot_line"):
            entry = cl.get(key_name)
            if entry is None:
                continue
            y_center = int(entry["y"]) + int(entry["h"]) // 2
            x_left = int(entry["x"])
            x_right = int(entry["x"]) + int(entry["w"])
            lines.append((y_center, x_left, x_right))
        # Make sure they're top-to-bottom (caller relies on this).
        lines.sort(key=lambda t: t[0])
    except Exception as exc:
        log.debug(
            "_get_panel_lines_cached: find_chrome_lines failed (%s); "
            "falling back to legacy _find_panel_lines", exc,
        )
        lines = []

    if not lines:
        # Legacy fallback. Keep this path intact so any case the
        # primary missed (and there are several known: very dim
        # captures, partial occlusion) still produces SOMETHING.
        # Pass y_range through so the legacy detector skips work
        # outside the requested band in local-search mode.
        lines = _find_panel_lines(gray, y_range=yr_norm)

    # Final y_range filter (covers the primary-detector path too,
    # since find_chrome_lines doesn't accept y_range directly).
    if yr_norm is not None:
        y_lo, y_hi = yr_norm
        lines = [t for t in lines if y_lo <= t[0] < y_hi]

    _panel_lines_cache = (key[0], key[1], key[2], lines)
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


def _find_hud_digit_cluster(
    gray: "np.ndarray",
    y1: int,
    y2: int,
    x_min: int = 0,
    x_max: Optional[int] = None,
) -> "Optional[tuple[int, int, int, int]]":
    """Locate the digit cluster on a single HUD row by structural pattern.

    HUD counterpart of ``signal_anchor.find_digit_cluster``. Where the
    signature panel uses a two-anchor COMBO-AGREE (location-pin icon +
    digit-cluster), the HUD currently only anchors on label templates
    (MASS: / RESISTANCE: / INSTABILITY:). When any of those label
    matches fails (score below threshold) the row's value-column
    bounds fall back to whichever row DID match — producing oversized
    value crops of mostly background that the CRNN reads as "0".

    This function is the content-based fallback: ignore labels, find
    where the digits actually are. Runs the same row-projection +
    column-segmentation + digit-shape-filter + adjacency-clustering
    pipeline as the signature finder, but constrained to a single
    pre-computed row band and restricted to the right of ``x_min``
    (past the row's label, when known). Returns a tight bbox around
    the digit cluster in original-image coords, or None when no
    valid cluster is detected.

    Used by ``_find_label_rows`` to:
      * VERIFY a label-anchored row found real digits (COMBO-AGREE).
      * RECOVER a value crop when the label match failed but digits
        are still rendered (most common: instability/resistance
        labels mis-detected on bright-sand-background panels).
      * REJECT degenerate row bands that contain no digits at all
        (the 0/0/0 flicker failure mode).
    """
    if gray.size == 0:
        return None
    H, W = gray.shape
    y1 = max(0, int(y1))
    y2 = min(H, int(y2))
    if y2 - y1 < 6:
        return None
    x_lo = max(0, int(x_min))
    x_hi = W if x_max is None else min(W, int(x_max))
    if x_hi - x_lo < 8:
        return None

    try:
        from .sc_ocr.api import _canonicalize_polarity, _adaptive_binarize
    except Exception as exc:
        log.debug("_find_hud_digit_cluster: import failed: %s", exc)
        return None
    band = gray[y1:y2, x_lo:x_hi]
    try:
        canon = _canonicalize_polarity(band.astype(np.uint8))
        binary = _adaptive_binarize(canon)
    except Exception as exc:
        log.debug("_find_hud_digit_cluster: canon/binarize failed: %s", exc)
        return None
    if binary.size == 0 or int(binary.max()) == 0:
        return None

    # Column projection of the band — find segments where columns have ink.
    col_proj = np.sum(binary > 0, axis=0).astype(np.int32)
    if int(col_proj.max()) == 0:
        return None
    col_active = col_proj > 0
    spans: list[tuple[int, int]] = []
    in_span = False
    span_start = 0
    bw = col_active.shape[0]
    for x in range(bw + 1):
        is_active = bool(col_active[x]) if x < bw else False
        if is_active and not in_span:
            in_span = True
            span_start = x
        elif (not is_active) and in_span:
            in_span = False
            if x - span_start >= 1:
                spans.append((span_start, x))
    if not spans:
        return None

    # Compute each span's actual bbox.
    span_bboxes: list[tuple[int, int, int, int]] = []
    for sx1, sx2 in spans:
        col_slice = binary[:, sx1:sx2]
        if int(col_slice.max()) == 0:
            continue
        rows_with_ink = np.where(col_slice.sum(axis=1) > 0)[0]
        if rows_with_ink.size == 0:
            continue
        sy1 = int(rows_with_ink[0])
        sy2 = int(rows_with_ink[-1] + 1)
        # Lift to original-image coords.
        span_bboxes.append(
            (x_lo + sx1, y1 + sy1, x_lo + sx2, y1 + sy2)
        )

    # Filter to digit-typical dimensions. HUD values render at the same
    # font height as the labels (~16-22 px) at native scale; widths run
    # 2-18 px (narrow "1" / "." through wide "0" / "8"). Bounds are
    # deliberately wider than the signature version's 8-25 × 2-16 so
    # we catch both small "." in instability and the leading "1" that
    # the signature finder's stricter width filter sometimes drops.
    digit_spans = []
    for x1c, y1c, x2c, y2c in span_bboxes:
        sw = x2c - x1c
        sh = y2c - y1c
        if 6 <= sh <= 32 and 1 <= sw <= 22:
            digit_spans.append((x1c, y1c, x2c, y2c))
    if not digit_spans:
        return None
    digit_spans.sort(key=lambda b: b[0])

    # Cluster by horizontal adjacency. HUD row values are 1-6 digits.
    median_h = int(np.median([b[3] - b[1] for b in digit_spans]))
    max_gap = max(6, median_h)
    clusters: list[list[tuple[int, int, int, int]]] = [[digit_spans[0]]]
    for i in range(1, len(digit_spans)):
        prev_x2 = clusters[-1][-1][2]
        cur_x1 = digit_spans[i][0]
        if cur_x1 - prev_x2 <= max_gap:
            clusters[-1].append(digit_spans[i])
        else:
            clusters.append([digit_spans[i]])

    # Pick the leftmost cluster of 1-7 members. "1" is valid (e.g.
    # resistance=0 or just-spawned mass=0). 7 is the upper bound for
    # 6-digit mass values with decimal (e.g. 100,000.5).
    for cluster in clusters:
        n = len(cluster)
        if 1 <= n <= 7:
            cx1 = min(s[0] for s in cluster)
            cy1 = min(s[1] for s in cluster)
            cx2 = max(s[2] for s in cluster)
            cy2 = max(s[3] for s in cluster)
            return (cx1, cy1, cx2, cy2)
    return None


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
    #
    # NOTE: A signature-style content-based digit-cluster refinement
    # using ``_find_hud_digit_cluster`` was attempted here. It works
    # cleanly on signature panels (small fixed crop, dark backgrounds,
    # well-separated digits) but on HUD value rows the binarization
    # frequently fuses adjacent digits into a single wide mega-span
    # that gets filtered out, leaving only the LEADING isolated digit
    # as the "cluster" — so a value like mass=27265 would crop to
    # just "2", dropping benchmark accuracy ~4 pp. The signature pipe
    # gets around this with ``_strip_pill_outline_bridges`` and
    # comma-masking preprocessing that breaks fused regions; that
    # preprocessing assumes the signature's pill-shaped numeric
    # capsule and doesn't transfer cleanly. The cluster function is
    # kept for future use (e.g. as a SECOND signal in a COMBO-AGREE
    # check that only refines when ALL clusters across rows agree),
    # but isn't wired into the value-crop path until that two-signal
    # design is fleshed out.
    return img.crop((x_lo, y1, x_lo + v_right_local, y2))


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


# Rigid-body HUD panel tracker — one per region. The tracker keeps the
# last accepted panel pose (panel_x, panel_y, scale) and, on subsequent
# frames, predicts each anchor's position then verifies with local-
# search detectors. Result: cross-frame stability that the prior
# full-image-search detector pipeline could not provide. See
# ocr/sc_ocr/hud_panel_tracker.py for the state machine.
#
# Keyed on the same region-tuple that the rest of this module uses,
# so each capture region gets its own independent tracker. Module-
# level mutable dict is OK here because each entry is itself a
# HudPanelTracker whose methods are thread-safe (the only state is
# last_pose / rejection_count, both updated under the GIL during a
# single track() call).
_hud_trackers: dict[str, "object"] = {}


def _get_or_create_tracker():
    """Return the HudPanelTracker singleton for the current region.

    Lazily imports the tracker class + offset table so a startup that
    happens before the package is fully wired up still succeeds. If
    Agent A's ``DEFAULT_OFFSETS`` import isn't available yet, fall
    back to the tracker module's own local default offsets table —
    the tracker still functions end-to-end during the integration
    window.
    """
    region = _get_current_region()
    if region is None:
        key = "default"
    else:
        try:
            key = (
                f"{int(region.get('x', 0))}_{int(region.get('y', 0))}_"
                f"{int(region.get('w', 0))}_{int(region.get('h', 0))}"
            )
        except Exception:
            key = "default"

    tracker = _hud_trackers.get(key)
    if tracker is not None:
        return tracker

    try:
        from .sc_ocr.hud_panel_tracker import (
            HudPanelTracker,
            DEFAULT_OFFSETS as _LOCAL_OFFSETS,
        )
    except Exception as exc:
        log.debug("HudPanelTracker import failed: %s", exc)
        return None

    # NOTE: ``hud_tracker.rigid_body.DEFAULT_OFFSETS`` is the canonical
    # offset table (in title-height units), but its label_* and colon_*
    # entries have ``None`` X-offsets because the existing codebase only
    # stores per-row bbox geometry — there's no per-glyph X calibration
    # anywhere yet. Feeding ``None`` offsets to the solver makes it skip
    # those anchors, leaving the tracker with too few measurements to
    # lock. The local table here uses pixel-unit offsets with X=0 (the
    # title's left edge) for the label rows, which lets the tracker use
    # them as Y-only constraints — enough to lock. When per-glyph X
    # calibration lands, the canonical table will become usable and we
    # can drop the local one.
    tracker = HudPanelTracker(offsets=_LOCAL_OFFSETS)
    _hud_trackers[key] = tracker
    return tracker


# ── HudPanelStabilizer singleton ───────────────────────────────────
# Same per-region keying as the tracker: one stabilizer per calibrated
# region so different capture configs don't share lock state.
_hud_stabilizers: dict[str, "object"] = {}


def _get_or_create_stabilizer():
    """Return the HudPanelStabilizer singleton for the current region.

    The stabilizer uses phase correlation between consecutive frames'
    panel regions for sub-millisecond inter-frame tracking, and falls
    back to the rigid-body tracker for cold-start and periodic
    re-anchor.
    """
    region = _get_current_region()
    if region is None:
        key = "default"
    else:
        try:
            key = (
                f"{int(region.get('x', 0))}_{int(region.get('y', 0))}_"
                f"{int(region.get('w', 0))}_{int(region.get('h', 0))}"
            )
        except Exception:
            key = "default"

    stab = _hud_stabilizers.get(key)
    if stab is not None:
        return stab

    try:
        from .sc_ocr.hud_panel_stabilizer import HudPanelStabilizer
    except Exception as exc:
        log.debug("HudPanelStabilizer import failed: %s", exc)
        return None

    # Tracker factory: the stabilizer needs access to the (already-
    # singleton'd) HudPanelTracker for its region. Wrap the existing
    # accessor so each cold-start / re-anchor uses the same tracker
    # instance (preserves the tracker's own lock state in case the
    # caller flips back to the tracker tier later).
    def _tracker_factory():
        t = _get_or_create_tracker()
        if t is None:
            raise RuntimeError("HudPanelTracker unavailable")
        return t

    stab = HudPanelStabilizer(tracker_factory=_tracker_factory)
    _hud_stabilizers[key] = stab
    return stab


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
    # Tesseract subprocess timeout: bound the worst-case hang at 8 s.
    # pytesseract supports timeout= since v0.3.6 and raises
    # RuntimeError if exceeded. Without this, a hung Tesseract child
    # could block the calling thread indefinitely (we've seen logs
    # where _find_label_rows reported 47 s elapsed for ~340 ms of
    # actual visible work — likely candidates include this exact call).
    _TESS_ANCHOR_TIMEOUT_S = 8
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
                timeout=_TESS_ANCHOR_TIMEOUT_S,
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
# Could be promoted to use ``hud_tracker/world_model.json`` (region1
# proportions) when that file has been calibrated against ≥ 30 captures.
# As of 2026-05, world_model.json has only 2 captures' worth of data so
# the static multipliers below — measured against the 397x541 reference
# fixture — remain authoritative for Tier C. See
# ``ocr/sc_ocr/api.py::_load_region2_world_model_for_api`` for how the
# region2 equivalent (``world_model_region2.json``) is loaded; an
# analogous lookup function could be added for region1 once enough
# captures have been labeled.
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
    """Compute label rows from the SCAN RESULTS anchor.

    Three-tier strategy, first one to succeed wins:

      A. PURE NCC (preferred) — crop the image to "below the title",
         run NCC label-template matching against MASS / RESISTANCE /
         INSTABILITY. Each match's pixel position IS that row's
         position. The title acts as a y-gate that prevents NCC from
         drifting into COMPOSITION / commodity rows below the data
         area. Most robust against tilted text and arbitrary panel
         scale because each row gets its own deterministic NCC anchor.

      B. MEASURED BANDS (fallback) — horizontal-projection band
         detection seeded from the title position. Works when NCC
         label templates don't match (e.g. unusual rendering /
         missing template) but bands are still distinguishable.

      C. FIXED MULTIPLIERS (deepest fallback) — title_h × static
         offsets from a reference panel. Only fires when bands also
         fail. Less robust but gives SOMETHING to work with.

    Tier A landed because measured bands kept finding the wrong bands
    when the captured region extended through IMPOSSIBLE / COMPOSITION
    / commodity rows (the projection signal under the title spans
    those rows AND the data rows, and band detection couldn't always
    pick the right 4). Per-row NCC is structurally immune to that —
    each row has its own template match.
    """
    title_y = anchor["title_y"]
    title_h = anchor["title_h"]
    title_bottom = title_y + title_h
    eff_title_h = min(int(title_h), 50)
    search_origin = min(img.height, title_y + eff_title_h + 4)

    # ── Tier A: pure NCC for each row (constrained to below title) ──
    # Crop to below the title, run NCC label-template matching, take
    # each match's pixel position as that row's location. Compute
    # label_right per row directly from the matched bbox's right edge
    # (``m["x"] + m["w"]``).
    #
    # Why the bbox right edge and not a column-density / gap-detection
    # scan? The previous gap-detection / value-column-find code was
    # brittle in two distinct ways that both caused per-row
    # ``label_right`` values to drift wildly across scans of the same
    # panel:
    #
    #   1. Gap detection (used for ``per_match_lr`` diagnostic) relied
    #      on a fixed ``_INTRA_LABEL_GAP=5 px`` threshold to merge
    #      inter-letter runs. At larger NCC scales (e.g. scale=2.00),
    #      the rendered intra-letter spacing in the SC HUD font crosses
    #      that threshold, so the "first coalesced run" terminated
    #      mid-label — yielding ``mass: 88`` when the actual label
    #      ``MASS:`` ended near x=168.
    #   2. Value-column-find (used for ``shared_lr``) depended on Otsu
    #      thresholds and a 50% row-height density floor that varied
    #      with image lighting, panel polarity, and NCC scale; the
    #      candidate often disagreed with the MASS-bbox fallback by
    #      enough to trigger the fallback, but the fallback itself
    #      was a heuristic (``mass.w * 0.93``) only correct when the
    #      MASS template was scale-matched.
    #
    # The matched bbox's right edge is THE deterministic, well-defined
    # right edge of where the label was found. ``_find_value_crop``
    # downstream does its OWN sophisticated value-finding from
    # ``x_min = lr + 6`` rightward (binary mask, two-tier density,
    # adjacency dilation, RTL span scan), so we don't need ``lr`` to
    # land precisely on the colon — we just need it past the label
    # and not so far right that we miss the value. ``match.x + match.w``
    # satisfies both constraints.
    #
    # Shared anchor: the HUD left-aligns all three values to a single
    # column whose left edge is past the LONGEST label (typically
    # INSTABILITY:). Taking ``max(per_match_lr.values())`` gives the
    # rightmost label-end across rows, which is the right anchor for
    # all rows' value crops. Matches the documented rule in
    # ``_find_label_rows_by_ncc`` (line ~1800).
    try:
        from .sc_ocr import label_match as _lm_rows
        if (img.height - search_origin) >= 60:
            # y_min keeps the search below the title; coordinates come
            # back IMAGE-ABSOLUTE (label_match owns the conversion).
            matches = _lm_rows.find_label_positions(
                img, y_min=search_origin,
            )
            matches = _repair_label_match_xs(img, matches)
            if matches and "mass" in matches:
                # ── Auto-calibration hook ──
                # Feed the observed (title_y, title_h, label_y_top,
                # label_h) tuple into the tracker's calibration learner.
                # The learner accumulates per-field label-top-to-title
                # ratios and, once stable, publishes them as the
                # tracker's offset/row-mult tables — overriding the
                # hardcoded defaults that don't match this user's HUD.
                #
                # matches['y'] is already image-absolute (label_match
                # applied y_min internally) — same frame as title_y.
                try:
                    from .sc_ocr import hud_panel_tracker as _hpt
                    _abs_matches: dict[str, dict] = {}
                    for _fld in ("mass", "resistance", "instability"):
                        _m = matches.get(_fld)
                        if not _m:
                            continue
                        _abs_matches[_fld] = {
                            "x": int(_m["x"]),
                            "y": int(_m["y"]),
                            "w": int(_m["w"]),
                            "h": int(_m["h"]),
                        }
                    _hpt.observe_calibration_sample(
                        title_y=anchor["title_y"],
                        title_h=anchor["title_h"],
                        label_matches=_abs_matches,
                    )
                except Exception as _cal_exc:  # pragma: no cover
                    log.debug(
                        "auto-calibration observe failed: %s", _cal_exc,
                    )

                # Per-row label_right = matched label bbox's right edge
                # (coordinates are image-absolute; label_match owns the
                # y_min restriction internally).
                per_match_lr = {
                    k: int(m["x"]) + int(m["w"]) for k, m in matches.items()
                }
                # ── Tier-D: colon-anchor fallback for unmatched rows ──
                # When Tier A matched < 3 rows, the rows that DIDN'T match
                # would otherwise fall back to a synthesized Y position
                # AND share label_right with the matched rows. The shared
                # label_right is whichever match's right edge was widest —
                # if only MASS matched, that anchor is far too short for
                # the (longer) RESISTANCE/INSTABILITY labels, so the
                # downstream value crop bleeds into the label area of the
                # unmatched rows and the CRNN reads spurious "0"s.
                #
                # Run a SECOND, INDEPENDENT anchor over the same y-band:
                # NCC against the canonical colon glyph (mirrors the
                # signature pipeline's icon+content two-anchor pattern).
                # For each unmatched row we use the nearest colon's Y
                # for that row's position and its (x + w) as its
                # label_right. Then the shared label_right is the max
                # across BOTH match-derived and colon-derived right edges.
                colon_lr_by_row: dict[str, int] = {}
                colon_y_by_row: dict[str, int] = {}
                colon_dets: list[dict] = []
                needed = {"mass", "resistance", "instability"}
                missing = needed - set(matches.keys())
                if missing:
                    try:
                        from .sc_ocr import colon_anchor as _ca
                        # Y-band: from search_origin downward enough to
                        # cover all three value rows (~200 px is generous
                        # at all observed HUD scales).
                        ca_y_top = search_origin
                        ca_y_bot = min(img.height, search_origin + 200)
                        # X-range: past the leftmost matched-label start
                        # (so we ignore mineral-row punctuation and any
                        # chrome on the left) but well clear of the value
                        # column (so we don't false-fire on decimal dots
                        # in values like "8.01"). The matched MASS bbox
                        # gives us a solid leftmost-start; we widen by
                        # 20 px in case a missing label's start is
                        # slightly left of MASS's.
                        ca_x_left = max(0, int(matches["mass"]["x"]) - 20)
                        ca_x_right = min(
                            img.width, int(img.width * 0.65),
                        )
                        colon_dets = _ca.find_colons(
                            img,
                            y_band=(ca_y_top, ca_y_bot),
                            x_range=(ca_x_left, ca_x_right),
                        )
                        # Predict each missing row's center Y from the
                        # matched MASS row using template-height-based
                        # pitch (same heuristic as the synthesized-row
                        # path below), then snap to the nearest detected
                        # colon within tolerance.
                        #
                        # Tolerance (±20 px) accounts for the inherent
                        # error in the mass*1.4 pitch heuristic
                        # (empirically the true pitch is mass*1.21-1.40
                        # across the labelled set; for the 2nd row down
                        # the prediction can be 10-15 px off). The
                        # all-or-nothing gate below is what actually
                        # defends against snapping to non-colon glyphs.
                        mass_y_abs = int(matches["mass"]["y"])
                        mass_h_pred = int(matches["mass"]["h"])
                        pitch_pred = max(20, int(round(mass_h_pred * 1.4)))
                        row_offsets = {
                            "mass": 0,
                            "resistance": pitch_pred,
                            "instability": 2 * pitch_pred,
                        }
                        mass_cy_img = (
                            mass_y_abs + mass_h_pred // 2
                        )
                        _SNAP_TOL = 20
                        used_colon_idxs: set[int] = set()
                        for row in missing:
                            target_cy = mass_cy_img + row_offsets[row]
                            best_idx = -1
                            best_d = 10 ** 9
                            for idx, c in enumerate(colon_dets):
                                if idx in used_colon_idxs:
                                    continue
                                cy = int(c["y"]) + int(c["h"]) // 2
                                d = abs(cy - target_cy)
                                if d < best_d:
                                    best_d = d
                                    best_idx = idx
                            if best_idx >= 0 and best_d <= _SNAP_TOL:
                                used_colon_idxs.add(best_idx)
                                c = colon_dets[best_idx]
                                colon_lr_by_row[row] = (
                                    int(c["x"]) + int(c["w"])
                                )
                                colon_y_by_row[row] = (
                                    int(c["y"]) + int(c["h"]) // 2
                                )
                        # All-or-nothing gate: only trust Tier-D when
                        # every missing row got a tight-snap colon.
                        # Partial Tier-D (some rows filled, others not)
                        # is unreliable — typically means the underlying
                        # MASS-anchor is degenerate (incorrect pitch
                        # prediction), so the synthesized fallback is
                        # likely BETTER than the half-corrected colon
                        # positions. Empirically: on the labeled
                        # benchmark, partial Tier-D introduced one
                        # regression (capture _160347_045) while
                        # contributing zero recoveries.
                        if colon_lr_by_row and len(colon_lr_by_row) < len(missing):
                            log.info(
                                "label_rows_from_anchor: TIER-D "
                                "partial fill (%d/%d rows) discarded — "
                                "filled=%s missing=%s (avoids snapping "
                                "to non-colon glyphs)",
                                len(colon_lr_by_row), len(missing),
                                sorted(colon_lr_by_row.keys()),
                                sorted(missing),
                            )
                            colon_lr_by_row.clear()
                            colon_y_by_row.clear()
                        elif colon_lr_by_row:
                            log.info(
                                "label_rows_from_anchor: TIER-D colon "
                                "fallback filled rows=%s colons=%s "
                                "(matched_rows=%s)",
                                sorted(colon_lr_by_row.keys()),
                                [
                                    (int(c["x"]), int(c["y"]))
                                    for c in colon_dets
                                ],
                                sorted(matches.keys()),
                            )
                    except Exception as ca_exc:
                        log.debug(
                            "label_rows_from_anchor: TIER-D colon "
                            "fallback failed (%s) — proceeding with "
                            "tier-A matches only", ca_exc,
                        )

                # Shared anchor across rows = rightmost label end across
                # both Tier-A matches and Tier-D colon detections.
                # The HUD aligns all values to a single column past the
                # longest label, so max-of-right-edges is the correct
                # value-column-left anchor.
                combined_rights = list(per_match_lr.values())
                combined_rights.extend(colon_lr_by_row.values())
                label_right = max(combined_rights)
                # Sanity floor: never let label_right exceed 75% of image
                # width — values column always has ≥25% of the panel.
                label_right = min(label_right, int(img.width * 0.75))

                # Build the result dict. Use NCC matches for Y, synthesize
                # missing rows from MASS using template-height-based pitch.
                #
                # Asymmetric padding: _PAD_Y_TOP > _PAD_Y_BOT. The NCC
                # bbox is tight to the label letters, but value digits
                # render slightly taller than the label letters in
                # the SC HUD font — the digits' tops extend a few
                # pixels above the label baseline. Extending the top
                # by more than the bottom clears those digit tops
                # without bleeding into the next row.
                #
                # Constraint: TOP + BOT must stay below (pitch - m.h)
                # to avoid overlap between adjacent rows' y-bands.
                # For typical m.h=32, pitch=45 → gap=13 → TOP+BOT<13.
                # 8+4=12 leaves 1 px buffer.
                _PAD_Y_TOP = 8
                _PAD_Y_BOT = 4
                mass_match = matches["mass"]
                mass_y_full = int(mass_match["y"])
                mass_h = int(mass_match["h"])
                pitch = max(20, int(round(mass_h * 1.4)))

                def _row_box(
                    m: Optional[dict],
                    offset_pitch: int,
                    colon_cy: Optional[int] = None,
                ) -> tuple[int, int]:
                    """Compute Y-box for a row.

                    Preference order:
                      1. NCC label match (Tier A) — use its bbox.
                      2. Tier-D colon Y — center the row on the colon
                         and use the MASS template height for the box.
                      3. Synthesize from MASS using pitch.
                    """
                    if m is not None:
                        y1 = max(0, int(m["y"]) - _PAD_Y_TOP)
                        y2 = min(
                            img.height,
                            int(m["y"]) + int(m["h"]) + _PAD_Y_BOT,
                        )
                    elif colon_cy is not None:
                        y1 = max(0, colon_cy - mass_h // 2 - _PAD_Y_TOP)
                        y2 = min(
                            img.height,
                            colon_cy + mass_h // 2 + _PAD_Y_BOT,
                        )
                    else:
                        cy = mass_y_full + (mass_h // 2) + offset_pitch
                        y1 = max(0, cy - mass_h // 2 - _PAD_Y_TOP)
                        y2 = min(img.height, cy + mass_h // 2 + _PAD_Y_BOT)
                    return y1, y2

                result: dict[str, tuple[int, int, int]] = {}
                y1, y2 = _row_box(mass_match, 0)
                result["mass"] = (y1, y2, label_right)
                y1, y2 = _row_box(
                    matches.get("resistance"), pitch,
                    colon_cy=colon_y_by_row.get("resistance"),
                )
                result["resistance"] = (y1, y2, label_right)
                y1, y2 = _row_box(
                    matches.get("instability"), 2 * pitch,
                    colon_cy=colon_y_by_row.get("instability"),
                )
                result["instability"] = (y1, y2, label_right)
                y1, y2 = _row_box(None, -pitch)  # mineral synthesized
                # Don't let mineral row land above the title bottom.
                if y1 < search_origin - 8:
                    y1 = max(0, search_origin)
                    y2 = min(img.height, search_origin + mass_h)
                result["_mineral_row"] = (y1, y2, label_right)

                log.debug(
                    "label_rows_from_anchor: NCC tier (search_origin=%d, "
                    "matched=%s, per_match_lr=%s, colon_lr=%s, shared_lr=%d)",
                    search_origin,
                    sorted(matches.keys()),
                    per_match_lr, colon_lr_by_row, label_right,
                )
                return result
    except Exception as exc:
        log.debug(
            "label_rows_from_anchor: NCC tier failed (%s) — falling "
            "back to measured bands", exc,
        )

    # ── Tier B: measured bands ──
    try:
        gray = np.array(img.convert("L"), dtype=np.uint8)
        search_h = min(img.height - search_origin, 400)
        if search_h >= 80:
            band_strip = gray[search_origin:search_origin + search_h, :]
            text_mask = _build_text_mask(band_strip)
            proj = text_mask.sum(axis=1).astype(np.float32)
            if proj.size >= 9:
                kernel = np.ones(7, dtype=np.float32) / 7.0
                proj = np.convolve(proj, kernel, mode="same")
            if proj.size >= 20 and float(proj.max()) > 0:
                band_thr = max(8.0, float(proj.max()) * 0.12)
                bands_rel: list[tuple[int, int]] = []
                in_band = False
                bs = 0
                for y in range(proj.size):
                    v = float(proj[y])
                    if v >= band_thr and not in_band:
                        in_band = True
                        bs = y
                    elif v < band_thr and in_band:
                        in_band = False
                        bands_rel.append((bs, y))
                if in_band:
                    bands_rel.append((bs, int(proj.size)))
                # Filter to plausible single-text-row heights
                bands_rel = [
                    b for b in bands_rel if 4 <= (b[1] - b[0]) <= 60
                ]
                # Dedupe close-together bands (ascender/x-height splits)
                bands_rel.sort()
                deduped: list[tuple[int, int]] = []
                for b in bands_rel:
                    if deduped:
                        prev = deduped[-1]
                        if ((b[0] + b[1]) // 2 - (prev[0] + prev[1]) // 2) < 12:
                            if (b[1] - b[0]) > (prev[1] - prev[0]):
                                deduped[-1] = b
                            continue
                    deduped.append(b)
                bands_rel = deduped
                # Need at least 4: mineral, mass, resist, instab.
                if len(bands_rel) >= 4:
                    abs_bands = [
                        (search_origin + s, search_origin + e)
                        for s, e in bands_rel[:4]
                    ]
                    # Compute label_right per row by scanning the
                    # leftmost-text run of each band, then take the max
                    # (HUD left-aligns all values to one column).
                    text_mask_full = _build_text_mask(gray)
                    half_w = img.width // 2
                    _GAP = 14
                    per_row_lr: list[int] = []
                    for y1, y2 in abs_bands[1:]:  # skip mineral row
                        col_hot = (
                            text_mask_full[y1:y2, :].sum(axis=0) >= 2
                        )
                        hot = np.where(col_hot[:half_w])[0]
                        if hot.size == 0:
                            continue
                        x_start = int(hot[0])
                        scanned_right = x_start
                        gap_run = 0
                        x = x_start
                        while x < col_hot.shape[0]:
                            if col_hot[x]:
                                scanned_right = x + 1
                                gap_run = 0
                            else:
                                gap_run += 1
                                if gap_run >= _GAP:
                                    break
                            x += 1
                        per_row_lr.append(min(scanned_right, half_w))
                    if per_row_lr:
                        label_right = max(per_row_lr)
                    else:
                        label_right = int(img.width * _VALUE_COL_LEFT_FRAC)

                    keys = ["_mineral_row", "mass", "resistance", "instability"]
                    _PAD = 3
                    result: dict[str, tuple[int, int, int]] = {}
                    for key, (y1, y2) in zip(keys, abs_bands):
                        result[key] = (
                            max(0, y1 - _PAD),
                            min(img.height, y2 + _PAD),
                            label_right,
                        )
                    log.debug(
                        "label_rows_from_anchor: measured bands "
                        "(title_y=%d, title_h=%d, search_origin=%d, "
                        "bands=%d, label_right=%d)",
                        title_y, title_h, search_origin,
                        len(bands_rel), label_right,
                    )
                    return result
    except Exception as exc:
        log.debug(
            "label_rows_from_anchor: measured-bands path failed (%s) "
            "— falling back to fixed multipliers", exc,
        )

    # ── Fallback: fixed proportional offsets ──
    # Used when band detection fails (e.g. capture region too small,
    # tilt corrupts projection too badly to find 4 bands). Less robust
    # than measurement, but at least produces SOMETHING when measurement
    # can't.
    half_h = max(8, int(title_h * _ROW_HEIGHT_MULT * 0.5))
    label_right = int(img.width * _VALUE_COL_LEFT_FRAC)
    result = {}
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
            title_box=_get_cached_title_box(),
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
    # the title itself, NOT the mineral name. Each row assignment
    # below shifts down by one slot: mass→mineral, resist→mass, etc.
    #
    # We use THREE detectors layered top-to-bottom; the first one to
    # fire drops bands[0] and the rest are skipped. Each is
    # independent so a single failure mode (e.g. tilted underline that
    # _find_panel_lines rejects) doesn't disable all three.
    #
    #   Signal A — HUD-LINE-BETWEEN: a HUD line sits strictly between
    #     bands[0] and bands[1]. Most reliable when the underline is
    #     extracted as a line (axis-aligned, full-width).
    #
    #   Signal B — OUTCOME-HEIGHT: with 5 bands, the last one should
    #     be the outcome progress bar (EASY/MEDIUM/HARD/...) which is
    #     ~1.4-2× taller than text rows. If bands[4] is no taller
    #     than the median of bands[1..3], bands[4] is just another
    #     text row — meaning we're seeing [title, mineral, mass,
    #     resist, instab] and the outcome bar fell outside the search
    #     window. Drop bands[0].
    #
    #   Signal C — PITCH-OUTLIER: data rows sit at uniform pitch.
    #     If bands[0]→bands[1] pitch is meaningfully larger than the
    #     median pitch of bands[1..3] consecutive pairs, bands[0] is
    #     the title separated by underline+padding. Backup for cases
    #     where Signals A and B both fail.
    title_dropped = False
    if len(bands) >= 2 and lines:
        b0_end_abs = search_origin + bands[0][1]
        b1_start_abs = search_origin + bands[1][0]
        for ly, _, _ in lines:
            if b0_end_abs < ly < b1_start_abs:
                log.debug(
                    "onnx_hud_reader: dropping bands[0] (SCAN RESULTS "
                    "title) — Signal A: HUD line at y=%d between "
                    "band[0] (ends y=%d) and band[1] (starts y=%d)",
                    ly, b0_end_abs, b1_start_abs,
                )
                bands = bands[1:]
                title_dropped = True
                break

    if not title_dropped and len(bands) >= 5:
        # Signal B: outcome-bar height check.
        band_heights = [b[1] - b[0] for b in bands]
        # Median height of presumed text rows under the assumption
        # that bands[0] is the title (so bands[1..3] are mineral,
        # mass, resist — all text rows of similar height).
        sorted_inner_h = sorted(band_heights[1:4])
        inner_median_h = sorted_inner_h[len(sorted_inner_h) // 2]
        outcome_h = band_heights[4]
        if inner_median_h > 0 and outcome_h <= inner_median_h * 1.15:
            log.debug(
                "onnx_hud_reader: dropping bands[0] (SCAN RESULTS "
                "title) — Signal B: bands[4] height %d <= 1.15 × "
                "median data-row height %d (outcome bar absent — "
                "5 text rows means [title, mineral, mass, resist, "
                "instab])",
                outcome_h, inner_median_h,
            )
            bands = bands[1:]
            title_dropped = True

    if not title_dropped and len(bands) >= 4:
        # Signal C: pitch-outlier check.
        # Use pitches between bands[1..3] consecutive pairs as the
        # reference. With 4+ bands we have at least 2 inner pairs;
        # take their median.
        inner_pitches = [
            bands[i + 1][0] - bands[i][0]
            for i in range(1, min(4, len(bands) - 1))
        ]
        if inner_pitches:
            sorted_inner_p = sorted(inner_pitches)
            median_inner_p = sorted_inner_p[len(sorted_inner_p) // 2]
            pitch_0_to_1 = bands[1][0] - bands[0][0]
            if median_inner_p > 0 and pitch_0_to_1 > median_inner_p * 1.4:
                log.debug(
                    "onnx_hud_reader: dropping bands[0] (SCAN RESULTS "
                    "title) — Signal C: bands[0]→bands[1] pitch %d "
                    "> 1.4 × median inner pitch %d",
                    pitch_0_to_1, median_inner_p,
                )
                bands = bands[1:]
                title_dropped = True

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
            title_box=_get_cached_title_box(),
        )
    except Exception:
        pass
    return result


def _row_ink_profile(
    detect_full: Optional[np.ndarray],
    row_y1: int,
    row_y2: int,
    x_limit: Optional[int],
) -> tuple[Optional[np.ndarray], int]:
    """Polarity-canonicalised per-column ink density for a row strip.

    Returns ``(col_density, band_h)`` where ``col_density[x]`` is the
    count of bright (text) pixels in column ``x`` of the strip, or
    ``(None, 0)`` if the strip is unusable. ``x_limit`` caps the scan
    width (``None`` → 60 % of image width). Shared by the label-end
    and value-column scanners below.
    """
    if detect_full is None or detect_full.size == 0:
        return None, 0
    ry1 = max(0, int(row_y1))
    ry2 = min(detect_full.shape[0], int(row_y2))
    band_h = ry2 - ry1
    if band_h < 4:
        return None, 0
    if x_limit is None:
        rx2 = int(detect_full.shape[1] * 0.60)
    else:
        rx2 = max(8, min(int(x_limit), int(detect_full.shape[1])))
    region = detect_full[ry1:ry2, :rx2]
    if region.size == 0:
        return None, 0
    # Polarity-canonicalize so text reads BRIGHT regardless of source.
    thr = _otsu(region)
    bright = int((region > thr).sum())
    if (region.size - bright) < bright:
        region_canon = (255 - region).astype(np.uint8)
    else:
        region_canon = region
    thr2 = _otsu(region_canon)
    col_d = (region_canon > thr2).sum(axis=0)
    return col_d, band_h


def _scan_label_end_x(
    detect_full: Optional[np.ndarray],
    row_y1: int,
    row_y2: int,
    *,
    x_limit: Optional[int] = None,
    return_start: bool = False,
) -> "Optional[object]":
    """Scan a row strip for the image-X where the LABEL text ends.

    The SC mining HUD renders ``LABEL:`` on the left of each row, a
    wide background gap, then the value. This locates the rightmost
    column of the FIRST contiguous bright text cluster from the left —
    i.e. the colon / label end, where the value column begins.

    Why a pixel scan rather than ``label_x + label_w`` from an NCC
    match: the label templates carry baked-in padding, so ``x + w``
    already overshoots the real colon by 30-50 px even for a clean
    match. Worse, ``RESISTANCE:`` / ``INSTABILITY:`` are often
    *synthesized* on skewed panels (their wide templates fail NCC) —
    a synthesized label's ``w`` is the full padded template width at
    the MASS scale, so a template-width-derived ``label_right`` lands
    the value crop in empty space PAST the digits and OCR reads
    nothing. Reading the actual ink is immune to both problems and
    works regardless of perspective skew (a rotated label still has
    its leftmost ink cluster = the label, with a clean gap before the
    value).

    Returns the image-absolute X of the label end, or ``None`` if no
    label ink is found in the strip.
    """
    col_d, band_h = _row_ink_profile(detect_full, row_y1, row_y2, x_limit)
    if col_d is None:
        return None
    # ── Ink floor ──
    # A column carries "ink" if at least a thin sliver of it is bright.
    # Keep this LOW: the HUD font's 'M' has a narrow centre V-notch
    # where the column density drops to ~30% of band height, and the
    # colon ':' is only two dots (~15-25% of band height). The
    # historical 0.25*band_h floor classed both as background, so the
    # scan false-broke INSIDE the first letter ('M' of 'MASS:').
    # 8% of band height keeps glyph internals and the colon as ink
    # while still rejecting 1-2 px dust.
    ink_floor = max(3, int(band_h * 0.08))
    ink = col_d >= ink_floor
    if not ink.any():
        return None
    # ── Gap threshold ──
    # The label→value gap is the widest horizontal gap in the row by
    # far. Inter-letter gaps are 1-3 px and a glyph's internal notch is
    # narrower still. 18% of band height sits comfortably between the
    # two and tracks capture resolution.
    gap_min = max(6, int(band_h * 0.18))
    # The label is a long contiguous ink run; stray dust specks are a
    # few px. An accepted run must clear this width so a leading speck
    # can't be mistaken for the label.
    min_label_w = max(12, int(band_h * 0.45))
    n_cols = int(ink.size)
    run_start: Optional[int] = None
    last_ink = -1
    gap = 0
    x = 0
    while x < n_cols:
        if ink[x]:
            if run_start is None:
                run_start = x
            last_ink = x
            gap = 0
        elif run_start is not None:
            gap += 1
            if gap >= gap_min:
                # Run ended at a wide gap. Accept it as the label only
                # if it is wide enough; otherwise it was speckle —
                # discard and keep scanning for the real label.
                if last_ink - run_start + 1 >= min_label_w:
                    if return_start:
                        return (int(run_start), int(last_ink) + 2)
                    return int(last_ink) + 2  # +2 px anti-alias halo
                run_start = None
                last_ink = -1
                gap = 0
        x += 1
    # Reached the scan limit without a trailing wide gap — the value
    # column is past x_limit. The current run, if substantial, is the
    # label.
    if (
        run_start is not None
        and last_ink - run_start + 1 >= min_label_w
    ):
        if return_start:
            return (int(run_start), int(last_ink) + 2)
        return int(last_ink) + 2
    return None


def _repair_label_match_xs(
    img: Image.Image,
    matches: dict,
    slack: int = 10,
) -> dict:
    """Re-anchor label matches that landed in the VALUE column.

    The label templates can false-match the value digits (live
    2026-06-12: MASS matched at x=416 on "12334" while the row's ink
    label-end was 82; resistance/instability were then synthesized
    from that wrong x, mis-placing value boxes and poisoning the
    auto-calibration and row-consensus K_x evidence). The row's
    leftmost substantial ink cluster is ground truth for where the
    label text lives: when a match's x lies beyond the ink-derived
    label END, the match is on the value column — re-anchor its x to
    the ink cluster START. The y/h are kept (they were correct even
    in the observed failure). Pixel work runs only when matches
    exist (~1 ms for the max-channel build + per-row scans)."""
    if not matches:
        return matches
    detect = None
    out = dict(matches)
    for fld, m in matches.items():
        try:
            x = int(m["x"])
            y1 = int(m["y"])
            y2 = y1 + int(m["h"])
            if detect is None:
                from .sc_ocr import frame_context as _fc
                detect = _fc.max_channel(img)  # shared per-frame percept
            se = _scan_label_end_x(
                detect, y1, y2, return_start=True,
            )
            if se is None:
                continue
            start, end = se
            if x > int(end) + slack:
                mm = dict(m)
                mm["x"] = int(start)
                out[fld] = mm
                log.info(
                    "label-x repaired field=%s x=%d -> %d (match was "
                    "in the value column; ink label span %d..%d)",
                    fld, x, int(start), int(start), int(end),
                )
        except Exception:
            continue
    return out


def _scan_value_column_x(
    detect_full: Optional[np.ndarray],
    row_y1: int,
    row_y2: int,
    *,
    x_limit: Optional[int] = None,
) -> Optional[int]:
    """Find the image-X where the VALUE column begins in a row strip.

    A HUD row is ``[label]  …wide gap…  [value]``. This returns the X
    of the first column of the *value* cluster — what a value crop
    should start at.

    Why this exists alongside ``_scan_label_end_x``: when the row band
    is shorter than the rendered glyphs (large-scale / zoomed-in
    captures), the band slices through the *tops* of the letters and
    each glyph fragments into disconnected pieces. A left-to-right
    "first contiguous run" scan then stops after the first letter and
    reports a label end ~1 glyph in — far short of the colon — so the
    value crop starts deep inside the label and drags the (long)
    label tail into the digits, fusing them.

    This scanner is fragmentation-proof: it first applies a horizontal
    morphological *closing* that bridges every inter-letter / inter-
    digit gap (those scale with glyph size) but NOT the much wider
    label→value gap. After closing the row collapses to two blobs —
    label, value — and the value's left edge is returned.

    Returns the image-absolute X, or ``None`` when no clean two-blob
    (label + value) structure is found (caller falls back).
    """
    if x_limit is None and detect_full is not None:
        # The value column sits around mid-panel; scan wide enough to
        # include it but not the far-right chrome.
        x_limit = int(detect_full.shape[1] * 0.80)
    col_d, band_h = _row_ink_profile(detect_full, row_y1, row_y2, x_limit)
    if col_d is None:
        return None
    ink_floor = max(3, int(band_h * 0.08))
    ink = col_d >= ink_floor
    idxs = np.where(ink)[0]
    if idxs.size == 0:
        return None
    # ── Closing radius ──
    # Bridge inter-letter / inter-digit gaps (which scale with glyph
    # size, ~0.2-0.4 * band height) so each word collapses to a single
    # blob, but stay well under the label→value gap so that gap
    # survives. ``max(24, band_h)`` clears inter-glyph gaps at every
    # capture scale; the HUD's label→value gap is always far larger.
    close_r = max(24, band_h)
    closed = ink.copy()
    for k in range(idxs.size - 1):
        gap = int(idxs[k + 1] - idxs[k] - 1)
        if 0 < gap <= close_r:
            closed[idxs[k] + 1: idxs[k + 1]] = True
    # Maximal True runs in the closed mask.
    runs: list[tuple[int, int]] = []
    run_start: Optional[int] = None
    for x in range(int(closed.size)):
        if closed[x]:
            if run_start is None:
                run_start = x
        elif run_start is not None:
            runs.append((run_start, x - 1))
            run_start = None
    if run_start is not None:
        runs.append((run_start, int(closed.size) - 1))
    # The label is the first blob at least ~2× the band height wide
    # (a single fragmented glyph is only ~1× — this excludes the case
    # where closing failed to merge the label). The value is the next
    # blob wide enough to be at least one digit.
    min_label_w = max(20, int(band_h * 2.0))
    min_value_w = max(4, int(band_h * 0.25))
    label_idx: Optional[int] = None
    for i, (s, e) in enumerate(runs):
        if e - s + 1 >= min_label_w:
            label_idx = i
            break
    if label_idx is None:
        return None
    _label_end_x = int(runs[label_idx][1])
    for (s, e) in runs[label_idx + 1:]:
        if e - s + 1 >= min_value_w:
            # Back the value-column X off the value blob's first ink
            # column by a small margin. The leading digit's left edge
            # is often AA-thinned / skew-degraded below the ink floor,
            # so the raw blob start lands a few px INSIDE the glyph —
            # the crop boundary then clips it and a left-clipped digit
            # mis-classifies (a "6" with its loop's left side cut off
            # reads as "5"). The margin restores those columns. It is
            # clamped to stay clear of the label blob, so the crop
            # never bleeds back into label text.
            _margin = max(8, int(band_h * 0.20))
            return max(_label_end_x + 4, int(s) - _margin)
    return None


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

    # Asymmetric padding: see the matching comment ~line 996 above.
    # NCC bbox is tight to label letters; value digits render slightly
    # above the label baseline so the top needs more room than the
    # bottom. TOP+BOT must stay below (pitch - m.h) to avoid adjacent-
    # row overlap; 8+4=12 fits in the typical 13 px gap.
    _PAD_Y_TOP = 8
    _PAD_Y_BOT = 4
    _PAD_Y = _PAD_Y_BOT  # legacy symmetric var, kept for callers that
                         # still reference _PAD_Y (rough_half_h below)

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
        from .sc_ocr import frame_context as _fc
        gray_full = np.array(img.convert("L"), dtype=np.uint8)
        detect_full = _fc.max_channel(img)  # shared per-frame percept
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
        # Thin wrapper over the module-level pixel scan (shared with
        # the EARLY-DIRECT path in _find_label_rows, which derives its
        # value-column X the same way). Default x_limit = 60% of image
        # width: the value column lives in the right ~40% and must not
        # be scanned for the label end.
        return _scan_label_end_x(detect_full, row_y1, row_y2)

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
            title_box=_get_cached_title_box(),
        )
    except Exception:
        pass

    return result


# Per-thread "current region" stash. The OCR pipeline (api.py
# scan_hud_onnx) sets this before calling _find_label_rows so we can
# look up persistent calibration without changing _find_label_rows'
# signature (which has dozens of callers across legacy code paths).
# threading.local() ensures each scan-pool worker sees its own region
# value, preventing cross-contamination when up to 64 scans run in
# parallel.
_thread_local = threading.local()


def _set_current_region(region: Optional[dict]) -> None:
    _thread_local.current_region = region


def _get_current_region() -> Optional[dict]:
    return getattr(_thread_local, "current_region", None)


# Per-thread cached SCAN RESULTS title bounding box. _find_label_rows
# detects the title once at the top of the function and stashes the
# (x, y, w, h) tuple here so EVERY downstream _dbg.set_panel_finder
# call (including those inside _find_label_rows_by_position,
# _find_label_rows_by_ncc, _find_label_rows_by_hud_grid) can pass it
# through to the overlay — the gold "SCAN RESULTS" box should always
# be drawn when the anchor is found, regardless of which path
# eventually produced the row positions. ThreadPoolExecutor(64) means
# we MUST keep this per-thread; module-level mutable state would
# cross-contaminate scans.
def _set_cached_title_box(box: Optional[tuple[int, int, int, int]]) -> None:
    _thread_local.cached_title_box = box


def _get_cached_title_box() -> Optional[tuple[int, int, int, int]]:
    return getattr(_thread_local, "cached_title_box", None)


def _emit_anchor_only_overlay() -> None:
    """Push a final ``set_panel_finder`` with the cached title_box so the
    overlay viewer STILL shows the gold SCAN RESULTS box even when every
    detection path failed.

    This is the diagnostic "we found the title but couldn't lay out the
    rows" state — without this hook, every failed scan would leave the
    overlay completely blank and the user couldn't tell whether the
    anchor matched at all. Best-effort: any exception inside is swallowed.
    """
    try:
        _box = _get_cached_title_box()
        if _box is None:
            return
        from .sc_ocr import debug_overlay as _dbg
        _dbg.set_panel_finder(
            top_y=_box[1],
            mineral_y_top=None,
            mineral_y_bot=None,
            mineral_center=None,
            pitch=None,
            bot_line_y=None,
            source="anchor_only",
            title_box=_box,
        )
    except Exception:
        pass


def _emit_label_rows_overlay(result: Optional[dict]) -> None:
    """Push ``set_label_rows`` from any path that successfully produced
    row positions, so the overlay state has the cyan row-band boxes
    regardless of which caller invoked ``_find_label_rows``.

    Without this hook, only ``scan_hud_onnx`` calls ``_dbg.set_label_rows``
    after the function returns — the calibration dialog's
    ``cal_live_refresh`` worker calls ``_find_label_rows`` directly,
    finds rows, but never publishes them. So the overlay would show
    only the title box and no row bands even on a successful detection.

    ``result`` is the dict returned by a detection path; keys we
    publish are ``mass`` / ``resistance`` / ``instability``, each a
    ``(y1, y2, label_right)`` tuple. Other keys (``_mineral_row``,
    ``mineral``, etc.) are ignored — ``set_label_rows`` only expects
    the three numeric-value rows.

    Best-effort: any exception inside is swallowed; this MUST never
    affect OCR correctness.
    """
    try:
        if not result or not isinstance(result, dict):
            return
        rows = {
            k: v for k, v in result.items()
            if k in ("mass", "resistance", "instability") and v is not None
        }
        if not rows:
            return
        from .sc_ocr import debug_overlay as _dbg
        # INFO log so the user can grep this and confirm what rows are
        # actually being pushed to the overlay state per-call. If the
        # overlay shows only mass-cyan but this log shows all three keys,
        # the merge in set_label_rows or the TTL gate at render time is
        # the next place to look. Demote to DEBUG once stable.
        try:
            log.info(
                "_emit_label_rows_overlay: pushing fields=%s",
                sorted(rows.keys()),
            )
        except Exception:
            pass
        _dbg.set_label_rows(rows)
    except Exception:
        pass


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


def _build_manual_override_label_rows(
    region: dict, img_w: int, img_h: int,
) -> dict[str, tuple[int, int, int]]:
    """Assemble a ``{field: (y_s, y_e, x_v_start)}`` dict directly
    from the user's manual-override boxes.

    Used when ``calibration.get_manual_override_mode(region)`` is True.
    Skips ``_find_label_rows`` / ``label_match`` / scan_results_anchor
    entirely — the user's drawn rectangles ARE the ground truth.

    Defensive: if a field has no manual box stored, it's silently
    skipped (the row stays uncalibrated, downstream OCR returns None
    for it). This keeps the pipeline alive even when the user
    enables manual mode without populating every field.
    """
    from .sc_ocr import calibration as _cal_local
    out: dict[str, tuple[int, int, int]] = {}
    for _field in ("_mineral_row", "mass", "resistance", "instability"):
        box = _cal_local.get_manual_override_box(region, _field)
        if box is None:
            continue
        try:
            _x = max(0, int(box["x"]))
            _y = max(0, int(box["y"]))
            _w = max(1, int(box["w"]))
            _h = max(1, int(box["h"]))
        except (KeyError, TypeError, ValueError):
            continue
        y_s = min(img_h, _y)
        y_e = min(img_h, _y + _h)
        if y_e - y_s < 4:
            continue
        # x_v_start is the x where the value crop begins. The manual
        # box is the user-defined value rectangle, so its left edge
        # IS the value column.
        x_v_start = min(img_w, _x)
        out[_field] = (y_s, y_e, x_v_start)
    return out


def _refine_value_band_to_ink(
    gray: np.ndarray,
    y1: int,
    y2: int,
    x_left: int,
    x_right: int,
) -> tuple[int, int]:
    """Snap a label-derived value band onto the ACTUAL digit-ink rows.

    The HUD value crop is derived from the LABEL row geometry: each
    field's band is centred on its label and the value digits are
    assumed to sit at the label's vertical position. In practice the
    value renders at a slightly different vertical offset from its label
    baseline, and that offset varies with HUD resolution — so a label-
    centred band clips digit tops or bottoms (the instability
    "19.50"->19.99 / "1.43"->1.41 family of bugs). Every prior fix was a
    per-field fixed shift/extend constant overfit to one capture.

    This is the self-correcting replacement. Within a generous vertical
    window around the coarse band, project per-row ink on the VALUE
    column, find the contiguous ink run that best OVERLAPS the coarse
    band (so an adjacent row's ink cannot hijack it), and return a band
    snapped to that run plus a small margin. Falls back to the input band
    whenever no clear, overlapping ink run is found — so it can only ever
    tighten onto real digits, never strand a field with an empty crop.

    Row profile uses a high per-row PERCENTILE (not mean) so it is
    immune to the value's horizontal position/width: a row crossing any
    digit stroke scores high regardless of how few columns the value
    occupies, while a single hot pixel can't fake a row.

    gray: max-channel grayscale of the (upscaled) HUD region.
    [y1,y2]: coarse label-derived band. [x_left,x_right]: value column.
    Returns a (y1, y2) band in the same coordinates.
    """
    try:
        H, W = int(gray.shape[0]), int(gray.shape[1])
        y1i, y2i = int(y1), int(y2)
        bh = max(1, y2i - y1i)
        xl = max(0, int(x_left))
        xr = min(int(x_right), W)
        if xr - xl < 6:
            return y1, y2
        # Window: ~0.55 band-height beyond each edge — enough to recover a
        # value rendered above OR below its label baseline, short of the
        # adjacent row. The overlap test below is the real guard against
        # grabbing a neighbour's ink, so the window can be generous.
        pad = max(3, int(bh * 0.55))
        wy1 = max(0, y1i - pad)
        wy2 = min(H, y2i + pad)
        if wy2 - wy1 < 6:
            return y1, y2
        col = gray[wy1:wy2, xl:xr].astype(np.float32)
        # Canonical polarity: ink bright (bright background -> invert).
        if float(np.median(col)) > 140.0:
            col = 255.0 - col
        prof = np.percentile(col, 90, axis=1)
        lo, hi = float(prof.min()), float(prof.max())
        if hi - lo < 12.0:
            return y1, y2  # flat strip, no resolvable ink
        thr = lo + 0.33 * (hi - lo)
        mask = prof > thr
        # Enumerate contiguous ink runs (window-relative row indices).
        runs: list[tuple[int, int]] = []
        _s: Optional[int] = None
        for _i, _v in enumerate(mask):
            if _v and _s is None:
                _s = _i
            elif (not _v) and _s is not None:
                runs.append((_s, _i))
                _s = None
        if _s is not None:
            runs.append((_s, len(mask)))
        if not runs:
            return y1, y2

        # Pick the run that overlaps the coarse band the most (absolute
        # coords); ties break toward the longer run.
        def _overlap(rs: int, re: int) -> int:
            a0, a1 = wy1 + rs, wy1 + re
            return max(0, min(a1, y2i) - max(a0, y1i))

        best = max(runs, key=lambda r: (_overlap(r[0], r[1]), r[1] - r[0]))
        if _overlap(best[0], best[1]) < 2 or (best[1] - best[0]) < 4:
            return y1, y2  # nothing meaningfully overlaps -> keep label band
        rs, re = best
        margin = max(2, int((re - rs) * 0.18))
        ny1 = max(0, wy1 + rs - margin)
        ny2 = min(H, wy1 + re + margin)
        if ny2 - ny1 < 6:
            return y1, y2
        return ny1, ny2
    except Exception:
        return y1, y2


def _find_label_rows(img: Image.Image) -> dict[str, tuple[int, int, int]]:
    """Pose-hold wrapper around the full row detection.

    When the pixels under every previously-accepted anchor band are
    unchanged (fingerprint-verified per call — see
    ``sc_ocr.panel_pose``), the previous rows are returned WITHOUT any
    template sweeps: the steady state verifies instead of re-searching,
    which removes both the per-frame mis-detection opportunity and the
    sweep cost. Any fingerprint break, image-size change, or the
    periodic forced re-verify falls through to ``_find_label_rows_impl``
    and re-fingerprints its result. ``SC_POSE_HOLD=0`` disables holding.
    """
    _pose_on = os.environ.get("SC_POSE_HOLD") != "0"
    if _pose_on:
        try:
            from .sc_ocr import panel_pose as _pose_mod
            _held = _pose_mod.observe(img)
            if _held is not None:
                _pose_health("HELD", _held)
                return _held
        except Exception as _ph_exc:
            log.debug("pose-hold observe failed: %s", _ph_exc)
    result = _find_label_rows_impl(img)
    if _pose_on:
        try:
            from .sc_ocr import panel_pose as _pose_mod
            _pose_mod.store(img, result)
        except Exception as _ph_exc:
            log.debug("pose-hold store failed: %s", _ph_exc)
    _pose_health("DETECTED", result)
    return result


def _pose_health(state: str, rows: dict) -> None:
    """One greppable line per frame into filter_events.log: the pose
    state (HELD / DETECTED) and the row geometry it produced. Loss
    events and flaps become data instead of anecdotes -- grep POSE
    and diff the y's. Best-effort: never affects the scan."""
    try:
        from .sc_ocr import api as _api_mod
        _m = rows.get("mass") if rows else None
        _i = rows.get("instability") if rows else None
        _api_mod._filter_event_log(
            "POSE %s mass=%s instab=%s rows=%d"
            % (
                state,
                ("%d-%d" % (_m[0], _m[1])) if _m else "-",
                ("%d-%d" % (_i[0], _i[1])) if _i else "-",
                len(rows) if rows else 0,
            )
        )
    except Exception:
        pass


def _find_label_rows_impl(img: Image.Image) -> dict[str, tuple[int, int, int]]:
    # Per-scan percept lifecycle: drop any cached normalization from a
    # PRIOR frame before this call reads the shared frame_context. The
    # cache keys on (id,size,mode); Python reuses id() after GC and
    # region1 panels often share size+mode, so without this reset a
    # collected prior frame's id-reuse would alias its stale max-channel
    # onto this frame (harness 2026-06-12: instability 28->25). Within
    # this call ``img`` is a live parameter, so its id is stable and the
    # in-call cache hits are valid.
    try:
        from .sc_ocr import frame_context as _fc0
        _fc0.reset()
    except Exception:
        pass
    return _find_label_rows_impl_body(img)


def _find_label_rows_impl_body(img: Image.Image) -> dict[str, tuple[int, int, int]]:
    """Find MASS / RESIST / INSTAB rows.

    Strategy:
      -1. MANUAL OVERRIDE MODE: if the user has flipped the override
          flag for this region, build label_rows from their drawn
          boxes ONLY — no detection, no anchor, no NCC.
      0. CALIBRATION: if the user has saved per-row calibration via
         the Calibration Dialog, return those coordinates DIRECTLY.
         No detection, no drift. This is the steady-state path
         after first-time setup.
      1. NCC label template matching (auto-detect)
      2. Position-based 5-band scan
      3. HUD-grid fractional fallback
      4. Tesseract per-label search (deepest fallback)

    Budget caps (each path is wall-clock bounded so a single slow
    detector can't stall the scan for minutes — the user previously saw
    275 s scans because a no-result NCC pass kept invoking the next
    fallback, each of which spawned its own Tesseract subprocess).
    """
    # Per-path wall-clock budgets (ms). When a path runs over, log a
    # WARNING (so the user sees it in the normal log file, not just at
    # DEBUG level) and ONLY use the budget to decide whether to ATTEMPT
    # the next fallback — never to throw away a result a path already
    # produced. The result-first ordering rule is enforced below.
    #
    # Generous values: the previous tight budgets (600/2500 ms) were
    # killing legit slow scans. Real successful scans on cold caches
    # have been observed to take ~50 s end-to-end yet still produce a
    # complete result with all three rows matched. The old code threw
    # those away. Now we only block the NEXT path if budget is gone.
    _BUDGET_PRIMARY_MS = 10000
    _BUDGET_SECONDARY_MS = 5000
    _BUDGET_TERTIARY_MS = 3000
    _BUDGET_QUATERNARY_MS = 2000
    _BUDGET_TESS_PER_LABEL_MS = 5000
    _BUDGET_TOTAL_MS = 30000

    # Master clock for the whole _find_label_rows call. Used to enforce
    # the total budget cap (only as a fallback gate — never to discard
    # successful results).
    _t_total_start = time.monotonic()

    def _total_elapsed_ms() -> float:
        return (time.monotonic() - _t_total_start) * 1000.0

    def _total_budget_exhausted() -> bool:
        return _total_elapsed_ms() > _BUDGET_TOTAL_MS

    # Helper: a result is "usable" (worth keeping) when it has at least
    # one row position OR a _mineral_row anchor. Empty dicts and dicts
    # with no useful keys are treated as "no result" so we fall through.
    def _result_is_usable(r: Optional[dict]) -> bool:
        if not r:
            return False
        return bool(
            "_mineral_row" in r
            or "mass" in r
            or "resistance" in r
            or "instability" in r
        )

    # Reset the per-thread cached title_box at function entry so a
    # previous scan's value (different image, possibly on a re-used
    # ThreadPoolExecutor worker) can't leak into this one. Populated
    # below when the SCAN RESULTS anchor is detected. Eager reset (not
    # lazy) is required because the 64-worker pool reuses threads.
    _set_cached_title_box(None)

    # Diagnostic: log function entry with image dimensions and any
    # available region info. INFO level so it's visible without DEBUG.
    try:
        _entry_w, _entry_h = img.size
    except Exception:
        _entry_w = _entry_h = -1
    _entry_region = _get_current_region()
    log.info(
        "_find_label_rows: entry img.size=(%d,%d) region=%s",
        _entry_w, _entry_h,
        _entry_region if _entry_region else "<none>",
    )

    # ── Title-box detection (before any sub-path runs) ──
    # The SCAN RESULTS title is the most-stable feature of the panel.
    # We try to find it ONCE, up front, and cache the bounding box in
    # the thread-local stash. EVERY downstream _dbg.set_panel_finder
    # call (in this function and in callees within this file) reads
    # the cache and passes it through, so the gold "SCAN RESULTS" box
    # is drawn on the overlay regardless of which detection path
    # eventually produces row positions — even if all of them fail.
    #
    # Wrapped in budget protection because the NCC anchor itself has
    # a fast/slow path: NCC template (~5 ms) usually hits, but the
    # Tesseract fallback can spawn a subprocess if the template path
    # fails. We give the whole anchor-find operation 250 ms — well
    # under the PRIMARY budget so even if it eats the full slice,
    # PRIMARY still has 350 ms to compute rows.
    try:
        log.debug(
            "_find_label_rows: PRE-ANCHOR starting at %.0fms",
            _total_elapsed_ms(),
        )
        _t_anchor_pre = time.monotonic()
        try:
            from .sc_ocr import scan_results_match as _srm_pre
            _t_srm_import = time.monotonic()
            # LOCAL-FIRST: when the cross-frame tracker has a fresh
            # smoothed pose, search a ±60 px window around it instead
            # of sweeping the full frame. The full sweep re-discovered
            # a constant small-scale false positive every cycle (the
            # title underline + end hook at 0.6x scale, score ~0.9,
            # logged as "tracker REJECTED outlier dist=395.7px") —
            # cosmetically harmless but ~26 ms/scan of waste and log
            # spam. A local window never contains that blob. A local
            # MISS escalates to the full-frame sweep, so a real panel
            # move is still re-acquired exactly as before.
            _trk_center = None
            try:
                _trk_center = _srm_pre.get_tracked_anchor_center()
            except Exception:
                _trk_center = None
            _pre_anchor = None
            _pre_mode = "full"
            if _trk_center is not None:
                _pre_anchor = _srm_pre.find_scan_results_anchor(
                    img, search_center=_trk_center, search_radius=60,
                )
                if _pre_anchor is not None:
                    _pre_mode = "local"
                else:
                    _pre_mode = "full-escalated"
            if _pre_anchor is None:
                _pre_anchor = _srm_pre.find_scan_results_anchor(img)
            log.debug(
                "_find_label_rows: PRE-ANCHOR srm.find_scan_results_anchor "
                "took %.0fms (result=%s, mode=%s)",
                (time.monotonic() - _t_srm_import) * 1000.0,
                "OK" if _pre_anchor is not None else "None",
                _pre_mode,
            )
        except Exception as _pre_inner_exc:
            _pre_anchor = None
            log.debug(
                "_find_label_rows: PRE-ANCHOR srm import/call failed: %s",
                _pre_inner_exc,
            )
        _pre_anchor_elapsed = (time.monotonic() - _t_anchor_pre) * 1000.0
        log.debug(
            "_find_label_rows: PRE-ANCHOR completed in %.0fms (result=%s)",
            _pre_anchor_elapsed,
            "OK" if _pre_anchor is not None else "empty",
        )
        if _pre_anchor_elapsed > 250:
            log.warning(
                "_find_label_rows: pre-anchor exceeded 250ms (took %.0fms) "
                "— title_box may be unavailable for overlay",
                _pre_anchor_elapsed,
            )
        if _pre_anchor is not None:
            _set_cached_title_box((
                int(_pre_anchor["title_x"]),
                int(_pre_anchor["title_y"]),
                int(_pre_anchor["title_w"]),
                int(_pre_anchor["title_h"]),
            ))
            # Push the title_box and the predicted row centers IMMEDIATELY
            # so the overlay viewer can render the gold SCAN RESULTS box
            # even if nothing else succeeds. _ROW_OFFSET_MULTS encodes the
            # known proportional offsets from title height to each row's
            # center.
            try:
                from .sc_ocr import debug_overlay as _dbg_pre
                _t_y = int(_pre_anchor["title_y"])
                _t_h = int(_pre_anchor["title_h"])
                _t_bot = _t_y + _t_h
                _expected_centers: dict[str, int] = {}
                for _key, _mult in _ROW_OFFSET_MULTS.items():
                    if _key.startswith("_"):
                        continue
                    _expected_centers[_key] = _t_bot + int(_t_h * (_mult - 1.0))
                _dbg_pre.set_expected_rows(
                    _expected_centers,
                    half_h=max(8, int(_t_h * _ROW_HEIGHT_MULT * 0.6)),
                )
                _dbg_pre.set_panel_finder(
                    top_y=_t_y,
                    mineral_y_top=None,
                    mineral_y_bot=None,
                    mineral_center=None,
                    pitch=int(_t_h * 1.4),
                    bot_line_y=None,
                    source="anchor_only",
                    title_box=_get_cached_title_box(),
                )
            except Exception:
                pass

            # ── EARLY-DIRECT row finder ──
            # Run label_match against a "below the title" crop. If it
            # finds all three numeric labels (mass / resistance /
            # instability), build label_rows directly from those
            # observed positions and RETURN EARLY — bypassing
            # STABILIZER, TRACKER, and PRIMARY entirely.
            #
            # The label_match results are DIRECT OBSERVATIONS of where
            # the rows actually are. Going through STABILIZER's phase
            # correlation (which carries forward a possibly-stale
            # cold-start pose) or the calibration learner (which
            # learns multipliers and applies them with potentially
            # wrong scale) just adds opportunities for drift on top of
            # this clean signal. Earlier scans showed bands ending up
            # at default-multiplier positions even though label_match
            # had cleanly identified all three rows — the architecture
            # was discarding the answer it already had.
            #
            # Why crop below the title?
            #   Full-frame label_match has been observed to false-match
            #   the MASS template against pieces of "SCAN RESULTS" or
            #   the mineral-name row ("IRON (ORE)"). Cropping to
            #   ``[title_bottom+4, img_height]`` eliminates those.
            #
            # We also feed the observations into the calibration
            # learner. Calibration is still useful for the fallback
            # tiers (STABILIZER's _pose_to_label_rows, TRACKER's solver)
            # when label_match itself fails — e.g. if the panel scrolls
            # off-screen briefly and only the title is detectable.
            #
            # Cost: ~80-150ms for the multi-scale NCC sweep. When the
            # EARLY-DIRECT path returns, the downstream tiers skip
            # entirely so the total scan time goes DOWN despite this
            # added work.
            try:
                from .sc_ocr import (
                    hud_panel_tracker as _hpt_cal,
                    label_match as _lm_cal,
                )
                _title_y_int = int(_pre_anchor["title_y"])
                _title_h_int = int(_pre_anchor["title_h"])
                _search_origin_cal = min(
                    img.height, _title_y_int + _title_h_int + 4,
                )
                _early_direct_result: Optional[dict] = None
                if (img.height - _search_origin_cal) >= 60:
                    # y_min keeps the search below the title;
                    # coordinates come back IMAGE-ABSOLUTE.
                    _cal_matches = _lm_cal.find_label_positions(
                        img, y_min=_search_origin_cal,
                    )
                    _cal_matches = _repair_label_match_xs(
                        img, _cal_matches,
                    )
                    if _cal_matches:
                        _abs_matches: dict[str, dict] = {}
                        for _fld in ("mass", "resistance", "instability"):
                            _m = _cal_matches.get(_fld)
                            if not _m:
                                continue
                            # y is already image-absolute (label_match
                            # applied y_min internally).
                            _abs_matches[_fld] = {
                                "x": int(_m["x"]),
                                "y": int(_m["y"]),
                                "w": int(_m["w"]),
                                "h": int(_m["h"]),
                            }
                        # Always feed calibration; it serves the
                        # fallback tiers when label_match fails on a
                        # future scan.
                        if len(_abs_matches) >= 2:
                            _hpt_cal.observe_calibration_sample(
                                title_y=_pre_anchor["title_y"],
                                title_h=_pre_anchor["title_h"],
                                label_matches=_abs_matches,
                            )
                            log.debug(
                                "PRE-ANCHOR auto-cal: observed "
                                "title=(y=%d,h=%d) labels=%s",
                                _title_y_int, _title_h_int,
                                {
                                    f: (
                                        _abs_matches[f]["y"],
                                        _abs_matches[f]["h"],
                                    )
                                    for f in _abs_matches
                                },
                            )
                        # Sanity-gate the (title, labels) geometry
                        # before trusting it. ``_pre_anchor`` and the
                        # NCC label sweep can independently latch onto
                        # DIFFERENT scales of the same panel — most
                        # commonly the title matches the short "SCAN"
                        # substring (h≈17, 1× scale of the run-in word)
                        # while the label sweep matches at the proper
                        # 2× scale (h≈56). The label-to-title ratio
                        # explodes (e.g. 10.8 for mass on a 2.0-7.5
                        # bound) and the geometry is internally
                        # inconsistent — every row band built from it
                        # would be the wrong size.
                        #
                        # The calibration learner above already rejects
                        # each field silently on this mismatch (visible
                        # as ``calibration sample REJECT field=… ratio=…
                        # outside bounds`` at DEBUG). We now consult the
                        # SAME plausibility predicate to decide whether
                        # EARLY-DIRECT can proceed at all. When it can't,
                        # we fall through to STABILIZER/TRACKER/PRIMARY
                        # — those tiers run their own pose validation
                        # and can recover from a partial title detect.
                        _cal_ok, _cal_reason = (
                            _hpt_cal.check_calibration_consistency(
                                title_y=_pre_anchor["title_y"],
                                title_h=_pre_anchor["title_h"],
                                label_matches=_abs_matches,
                            )
                        )
                        # Build label_rows directly when ALL THREE
                        # numeric labels matched — that's the
                        # high-confidence case where we can trust the
                        # observation. If only 2 matched we fall
                        # through to STABILIZER/PRIMARY which can
                        # synthesize the missing row via pitch or
                        # colon anchors.
                        _required = ("mass", "resistance", "instability")
                        _have_all_labels = all(
                            f in _abs_matches for f in _required
                        )
                        # ── Geometry-source selection ──
                        # Two paths can produce a usable ``_half_h_direct``:
                        #
                        #   A. ``_cal_ok == True``: title-label ratios are
                        #      in bounds. Use ``title_h * 0.5`` for the
                        #      half-band — this is the original behavior
                        #      and produces a band ≈ title_h tall (well
                        #      under the 50-px oversized-crop threshold
                        #      for typical title_h≈45 captures).
                        #
                        #   B. ``_cal_ok == False`` BUT label heights /
                        #      gaps are internally consistent: title
                        #      detector matched the wrong instance (e.g.
                        #      the small "SCAN RESULTS" button at the
                        #      top-right corner at 1× scale, h=17, while
                        #      the actual labels matched at 2× scale,
                        #      h=56). Fall back to label-derived geometry
                        #      using ``label_h * 0.4`` (≈ what
                        #      ``title_h * 0.5`` would give us if title
                        #      had matched at the correct scale, since
                        #      ``title_h ≈ label_h * 0.8`` on good
                        #      captures).
                        #
                        # Path B unblocks the failure mode where STABILIZER
                        # otherwise returns a stale-pose lock at low
                        # phase-corr response (~0.13), positioning row
                        # bands ~75-100 px above the real labels and
                        # producing garbage live reads (mass='5555555',
                        # instability='Faese', etc.) while label_match
                        # had clean direct observations all along.
                        _half_h_direct: Optional[int] = None
                        _geometry_source = "none"
                        if _have_all_labels:
                            _label_h_list = [
                                int(_abs_matches[f]["h"]) for f in _required
                            ]
                            _label_h_max = max(_label_h_list)
                            _label_h_min = min(_label_h_list)
                            if _cal_ok:
                                _half_h_direct = max(
                                    8, int(_title_h_int * 0.5),
                                )
                                _geometry_source = "title"
                            else:
                                # Independent label-consistency gate. We
                                # require height-uniformity (within 15%),
                                # gap-uniformity (within 30% of each
                                # other), and gap-plausibility (≥ 0.7×
                                # label_h, well below the typical ~1.3×
                                # label_h HUD pitch but enough to reject
                                # samples where two labels accidentally
                                # collided at the same Y).
                                _label_y_sorted = sorted(
                                    int(_abs_matches[f]["y"])
                                    for f in _required
                                )
                                _gaps = [
                                    _label_y_sorted[i + 1]
                                    - _label_y_sorted[i]
                                    for i in range(2)
                                ]
                                _gap_max = max(_gaps)
                                _gap_min = min(_gaps)
                                _h_uniform = (
                                    _label_h_max > 0
                                    and (
                                        _label_h_max - _label_h_min
                                    ) / _label_h_max <= 0.15
                                )
                                _gap_uniform = (
                                    _gap_max > 0
                                    and (
                                        _gap_max - _gap_min
                                    ) / _gap_max <= 0.30
                                )
                                _gap_plausible = (
                                    _gap_min >= int(_label_h_max * 0.7)
                                )
                                if (
                                    _h_uniform
                                    and _gap_uniform
                                    and _gap_plausible
                                ):
                                    _half_h_direct = max(
                                        8, int(_label_h_max * 0.4),
                                    )
                                    _geometry_source = "label-only"
                                    log.info(
                                        "_find_label_rows: EARLY-DIRECT "
                                        "label-only fallback (title: %s; "
                                        "labels are independently "
                                        "consistent h_max=%d gaps=%s) — "
                                        "using label_h-derived half_h=%d",
                                        _cal_reason, _label_h_max,
                                        _gaps, _half_h_direct,
                                    )
                                else:
                                    log.info(
                                        "_find_label_rows: EARLY-DIRECT "
                                        "refusing geometry (title: %s; "
                                        "label-only fallback also "
                                        "failed: h_uniform=%s "
                                        "gap_uniform=%s "
                                        "gap_plausible=%s) — falling "
                                        "through to STABILIZER/TRACKER/"
                                        "PRIMARY",
                                        _cal_reason, _h_uniform,
                                        _gap_uniform, _gap_plausible,
                                    )
                        elif not _cal_ok:
                            log.info(
                                "_find_label_rows: EARLY-DIRECT "
                                "refusing inconsistent geometry "
                                "(%s, title=(y=%d,h=%d)) — falling "
                                "through to STABILIZER/TRACKER/"
                                "PRIMARY",
                                _cal_reason,
                                _title_y_int, _title_h_int,
                            )
                        if _have_all_labels and _half_h_direct is not None:
                            # ── Row Y-bands from observed label
                            # centers. MASS is NCC-matched; RESISTANCE
                            # / INSTABILITY may be synthesized from the
                            # rigid 3-row geometry. ──
                            _row_bands: dict[
                                str, tuple[int, int]
                            ] = {}
                            # ── RIGID-POSE AUTHORITY ──
                            # Solve ONE panel pose from the title + label
                            # observations and derive every row CENTER
                            # from it, so the rows are a single rigid
                            # body that cannot independently disagree —
                            # and an anchor that doesn't fit (a dust
                            # title, a stray label) is rejected as an
                            # outlier and the pose re-solved from the
                            # rest. Proven on 66 annotated panels: pose
                            # row-centers land within ~1px median.
                            # The band THICKNESS keeps the tuned
                            # ``_half_h_direct`` (pose validated centers,
                            # not band height). Falls back to per-label
                            # centers only when the pose is
                            # under-determined (<2 anchors).
                            _panel_pose = None
                            try:
                                from .sc_ocr import panel_solve as _psolve
                                _lbls_solve = {
                                    _f: _abs_matches[_f]
                                    for _f in _required
                                    if _f in _abs_matches
                                }
                                _panel_pose = _psolve.solve(
                                    _pre_anchor, _lbls_solve,
                                )
                            except Exception as _pexc:
                                log.debug(
                                    "EARLY-DIRECT pose solve failed: %s",
                                    _pexc,
                                )
                                _panel_pose = None
                            # Independent per-label band is the starting
                            # point (the value reader is tuned to it).
                            # ``_actual_cy`` keeps each row's OWN detected
                            # center BEFORE the pose snaps strays, so the
                            # spine overlay can show seated-vs-wandered.
                            _actual_cy: dict[str, int] = {}
                            for _fld in _required:
                                _m_abs = _abs_matches[_fld]
                                _lc = (
                                    int(_m_abs["y"])
                                    + int(_m_abs["h"]) // 2
                                )
                                _actual_cy[_fld] = _lc
                                _y1 = max(0, _lc - _half_h_direct)
                                _y2 = min(img.height, _lc + _half_h_direct)
                                if _y2 - _y1 >= 4:
                                    _row_bands[_fld] = (_y1, _y2)
                            # The rigid pose now CORRECTS strays, it does
                            # not override good detail: a row whose
                            # independent center disagrees with the
                            # pose-predicted center by more than
                            # _POSE_SNAP_PX is the part trying to wander
                            # (a dust/jump detection) — snap it back onto
                            # the body. A row that already agrees is left
                            # exactly as the tuned value reader expects,
                            # so clean stills are unchanged. This is the
                            # skeleton holding each part in place without
                            # forcibly moving the ones already seated.
                            _POSE_SNAP_PX = 12
                            if _panel_pose is not None:
                                _snapped = []
                                for _fld in _required:
                                    _pcy = int(round(
                                        _panel_pose["y"]
                                        + _panel_pose["scale"]
                                        * _psolve.ROW_CENTER_MULTS[_fld]
                                    ))
                                    _cur = _row_bands.get(_fld)
                                    _cur_cy = (
                                        (_cur[0] + _cur[1]) // 2
                                        if _cur else None
                                    )
                                    if (_cur_cy is None
                                            or abs(_cur_cy - _pcy)
                                            > _POSE_SNAP_PX):
                                        _y1 = max(0, _pcy - _half_h_direct)
                                        _y2 = min(
                                            img.height, _pcy + _half_h_direct,
                                        )
                                        if _y2 - _y1 >= 4:
                                            _row_bands[_fld] = (_y1, _y2)
                                            _snapped.append(_fld)
                                log.info(
                                    "_find_label_rows: RIGID POSE x=%.0f "
                                    "y=%.0f scale=%.1f anchors=%s "
                                    "rejected=%s snapped=%s",
                                    _panel_pose["x"], _panel_pose["y"],
                                    _panel_pose["scale"],
                                    _panel_pose["anchors"],
                                    _panel_pose["rejected"], _snapped,
                                )
                                # Overlay: draw the title from the POSE
                                # so the gold box stops hopping between
                                # the raw detection and the frozen
                                # snapshot (the ~14px jump the user
                                # recorded) — one rigid source.
                                try:
                                    _set_cached_title_box(
                                        _psolve.title_box(_panel_pose)
                                    )
                                except Exception:
                                    pass
                                # Spine overlay: feed the ONE solved pose
                                # + each part's own detection so the
                                # reviewer can SEE the body held together
                                # (every node strung on one backbone) or a
                                # part wandering off it.
                                try:
                                    from .sc_ocr import (
                                        debug_overlay as _dbg_spine
                                    )
                                    _px = _panel_pose["x"]
                                    _py = _panel_pose["y"]
                                    _sc = _panel_pose["scale"]
                                    _ttl_act = (
                                        int(_pre_anchor["title_y"])
                                        if _pre_anchor is not None
                                        and "title_y" in _pre_anchor
                                        else None
                                    )
                                    _spine_nodes = [(
                                        "title", int(_px), int(_py),
                                        _ttl_act,
                                    ), (
                                        "_mineral_row", int(_px),
                                        int(round(
                                            _py + _sc * _psolve
                                            .ROW_CENTER_MULTS["_mineral_row"]
                                        )), None,
                                    )]
                                    for _fld in (
                                        "mass", "resistance", "instability",
                                    ):
                                        _spine_nodes.append((
                                            _fld, int(_px),
                                            int(round(
                                                _py + _sc * _psolve
                                                .ROW_CENTER_MULTS[_fld]
                                            )),
                                            _actual_cy.get(_fld),
                                        ))
                                    _dbg_spine.set_panel_pose(
                                        (int(_px), int(_py)), float(_sc),
                                        _spine_nodes,
                                        _panel_pose.get("rejected"),
                                        _panel_pose.get("stab"),
                                    )
                                except Exception:
                                    pass
                            # ── Value-column X (label_right) ──
                            # Every field's value crop starts at this
                            # X. Derive it from where the VALUE INK
                            # begins — never from ``label_x + label_w``
                            # (a synthesized label's ``w`` is a full
                            # padded template at a possibly-wrong MASS
                            # scale; ``max(x+w)`` overshoots the digits
                            # and the crop comes back empty).
                            #
                            # _scan_value_column_x locates the label→
                            # value gap directly and is immune to the
                            # row band slicing tall glyphs into
                            # fragments. The three values are left-
                            # aligned to one column, so scan every row
                            # and take the MEDIAN — robust if one row's
                            # label→value gap is too tight to resolve
                            # (e.g. INSTABILITY:, the longest label,
                            # leaves the smallest gap). _scan_label_end
                            # _x is the fallback when no row yields a
                            # clean label+value two-blob structure.
                            _label_right: Optional[int] = None
                            _value_starts: list[int] = []
                            _label_ends: list[int] = []
                            try:
                                from .sc_ocr import frame_context as _fc
                                _detect_ed = _fc.max_channel(img)
                                for _fld in _required:
                                    _bnd = _row_bands.get(_fld)
                                    if _bnd is None:
                                        continue
                                    _vs = _scan_value_column_x(
                                        _detect_ed, _bnd[0], _bnd[1],
                                    )
                                    if _vs is not None:
                                        _value_starts.append(_vs)
                                    _le = _scan_label_end_x(
                                        _detect_ed, _bnd[0], _bnd[1],
                                    )
                                    if _le is not None:
                                        _label_ends.append(_le)
                                if _value_starts:
                                    # Left-aligned column — median is
                                    # robust to one mis-resolved row.
                                    _value_starts.sort()
                                    _label_right = _value_starts[
                                        len(_value_starts) // 2
                                    ]
                                elif _label_ends:
                                    # No clean two-blob row; fall back
                                    # to the rightmost label end.
                                    _label_right = max(_label_ends)
                            except Exception as _lr_exc:
                                log.debug(
                                    "EARLY-DIRECT value-column scan "
                                    "failed: %s", _lr_exc,
                                )
                            if _label_right is None:
                                # Nothing resolved — fall back to the
                                # template-width estimate so the path
                                # still produces a result.
                                _label_right = max(
                                    int(_abs_matches[f]["x"])
                                    + int(_abs_matches[f]["w"])
                                    for f in _required
                                )
                                log.debug(
                                    "EARLY-DIRECT value-column scan "
                                    "empty — template-width "
                                    "fallback=%d", _label_right,
                                )
                            # Clamp: the value column never starts left
                            # of 20% of the width (labels are always at
                            # least that long) nor right of 75% (the
                            # HUD reserves the right ~25% for values).
                            _label_right = max(
                                int(img.width * 0.20),
                                min(
                                    int(_label_right),
                                    int(img.width * 0.75),
                                ),
                            )
                            log.info(
                                "_find_label_rows: EARLY-DIRECT "
                                "value-column X=%d (value-starts=%s "
                                "label-ends=%s img.width=%d)",
                                _label_right,
                                sorted(_value_starts),
                                sorted(_label_ends),
                                img.width,
                            )
                            # Max-channel grayscale for the ink-projection
                            # band refiner (same recipe as _detect_ed; built
                            # here so the refiner doesn't depend on the
                            # value-column scan's try-block scope).
                            try:
                                from .sc_ocr import frame_context as _fc
                                _gray_ink = _fc.max_channel(img)
                            except Exception:
                                _gray_ink = None
                            _direct: dict[str, tuple[int, int, int]] = {}
                            for _fld in _required:
                                _bnd = _row_bands.get(_fld)
                                if _bnd is None:
                                    continue
                                _y1d, _y2d = int(_bnd[0]), int(_bnd[1])
                                # Snap the label-derived band onto the actual
                                # digit ink. SCOPED TO INSTABILITY: across all
                                # 130 annotated region panels (full pipeline,
                                # not pre-cropped) the instability label band
                                # is only ~6% correct — its value renders well
                                # off its label baseline and clips badly — and
                                # the ink refiner ~triples it (recovering
                                # clipped leading digits on values like 282.02
                                # / 126.02 / 19.08 that the label band read as
                                # 47.42 / 12.02 / 1.96). Mass and resistance,
                                # by contrast, read fine from the label band
                                # and the refiner REGRESSES them (it snapped
                                # onto wrong/noise ink: resistance 0%->55,
                                # mass 15683->5883), so they keep the label
                                # band. The refiner still self-corrects across
                                # resolutions and falls back to the label band
                                # when no clear overlapping ink run is found.
                                # See _refine_value_band_to_ink + the
                                # full-panel A/B in task notes.
                                if _gray_ink is not None and _fld == "instability":
                                    _ry1, _ry2 = _refine_value_band_to_ink(
                                        _gray_ink, _y1d, _y2d,
                                        _label_right, img.width,
                                    )
                                    # Height sanity: digits are label-
                                    # height-ish. A refined band much
                                    # shorter than the label band is
                                    # PARTIAL ink (clipped digits), not
                                    # a better band — live 2026-06-12 a
                                    # 16px sliver of a 44px band read
                                    # 1.43 as '4.%3' -> 4.3 and the
                                    # frozen layer locked it.
                                    if (_ry2 - _ry1) < 0.5 * (_y2d - _y1d):
                                        log.info(
                                            "_find_label_rows: ink-refine "
                                            "REJECTED (h=%d < 50%% of "
                                            "label band h=%d) — keeping "
                                            "label band",
                                            _ry2 - _ry1, _y2d - _y1d,
                                        )
                                        _ry1, _ry2 = _y1d, _y2d
                                    # ── POSE LEASH ──────────────────────
                                    # Instability is the worst-anchored row
                                    # (its value renders well off its own
                                    # label baseline) and the ink refiner
                                    # searches a window that can snap onto a
                                    # panel border / adjacent ink and run
                                    # the crop clean off the row — the organ
                                    # leaving the skeleton. The held pose
                                    # places instability within ~0.3px on
                                    # stills, so if the refined (or label)
                                    # band's center has strayed more than
                                    # ~0.6 row-heights from the pose-
                                    # predicted instability center, it has
                                    # escaped the body: snap it back onto the
                                    # pose band. This is a one-way LEASH —
                                    # it never moves a crop that already
                                    # agrees with the pose, so clean reads
                                    # (and the gate) are untouched; it only
                                    # catches the runaway.
                                    if _panel_pose is not None:
                                        try:
                                            _pbnd = _psolve.row_band(
                                                _panel_pose, "instability",
                                                img.height, _label_right,
                                            )
                                        except Exception:
                                            _pbnd = None
                                        if _pbnd is not None:
                                            _pcy = (_pbnd[0] + _pbnd[1]) / 2.0
                                            _rcy = (_ry1 + _ry2) / 2.0
                                            _leash = max(
                                                12.0,
                                                0.6 * _panel_pose["scale"],
                                            )
                                            if abs(_rcy - _pcy) > _leash:
                                                log.info(
                                                    "_find_label_rows: "
                                                    "instability crop ESCAPED "
                                                    "pose leash (band cy=%.0f "
                                                    "vs pose cy=%.0f, "
                                                    "leash=%.0f) — clamped to "
                                                    "pose band (%d,%d)",
                                                    _rcy, _pcy, _leash,
                                                    _pbnd[0], _pbnd[1],
                                                )
                                                _ry1, _ry2 = (
                                                    int(_pbnd[0]),
                                                    int(_pbnd[1]),
                                                )
                                    if (_ry1, _ry2) != (_y1d, _y2d):
                                        log.info(
                                            "_find_label_rows: EARLY-DIRECT "
                                            "ink-refine field=%s label-band="
                                            "(%d,%d) -> ink-band=(%d,%d)",
                                            _fld, _y1d, _y2d, _ry1, _ry2,
                                        )
                                        _y1d, _y2d = _ry1, _ry2
                                _direct[_fld] = (_y1d, _y2d, _label_right)
                            # Mineral row sits between the title and
                            # the mass label. In ``title`` mode we use
                            # the detected title bottom; in ``label-
                            # only`` mode the title position is also
                            # suspect (it was the source of the
                            # inconsistency), so we synthesize the
                            # mineral-row top by stepping one label-
                            # pitch UP from mass — same spacing the
                            # HUD itself uses between rows.
                            _mass_top = int(_abs_matches["mass"]["y"])
                            # Mineral row from the SAME rigid pose when
                            # available — one body, so the name band
                            # can't drift relative to the rows. Its
                            # center is a fixed offset above MASS;
                            # bottom is pinned just above the mass label.
                            if _panel_pose is not None:
                                _min_cy = int(round(
                                    _panel_pose["y"]
                                    + _panel_pose["scale"]
                                    * _psolve.ROW_CENTER_MULTS["_mineral_row"]
                                ))
                                _mineral_y1 = max(
                                    0,
                                    _min_cy - int(_panel_pose["scale"] * 0.7),
                                )
                            elif _geometry_source == "title":
                                _mineral_y1 = max(
                                    0,
                                    _title_y_int + _title_h_int + 4,
                                )
                            else:
                                _label_pitch = (
                                    int(
                                        (
                                            int(_abs_matches[
                                                "instability"
                                            ]["y"])
                                            - int(_abs_matches[
                                                "mass"
                                            ]["y"])
                                        ) / 2
                                    )
                                )
                                _mineral_y1 = max(
                                    0, _mass_top - _label_pitch,
                                )
                            _mineral_y2 = min(img.height, _mass_top - 4)
                            if _mineral_y2 - _mineral_y1 >= 8:
                                _direct["_mineral_row"] = (
                                    _mineral_y1, _mineral_y2, _label_right,
                                )
                            if all(f in _direct for f in _required):
                                _early_direct_result = _direct

                if (
                    _early_direct_result is not None
                    and _result_is_usable(_early_direct_result)
                ):
                    log.info(
                        "_find_label_rows: EARLY-DIRECT returning result "
                        "(title y=%d h=%d, used 3 label_match observations, "
                        "skipping STABILIZER/TRACKER/PRIMARY)",
                        _title_y_int, _title_h_int,
                    )
                    try:
                        from .sc_ocr import debug_overlay as _dbg_early
                        _mineral_band = _early_direct_result.get(
                            "_mineral_row", (0, 0, 0),
                        )
                        # Overlay honesty: the synthesized _mineral_row
                        # is the whole title→MASS gap (a generous SEARCH
                        # strip, kept as-is for downstream consumers).
                        # The band OCR actually READS is the structural
                        # refine (bottom-line-above-MASS + paren anchor)
                        # — draw THAT, so the green MINERAL box matches
                        # reality instead of swallowing the panel.
                        try:
                            from .sc_ocr import api as _api_band
                            _rb_ov = (
                                _api_band
                                ._refine_mineral_band_above_mass(
                                    img,
                                    int(_abs_matches["mass"]["y"]),
                                )
                            )
                            if _rb_ov is not None:
                                _mineral_band = (
                                    _rb_ov[0], _rb_ov[1],
                                    _mineral_band[2],
                                )
                        except Exception:
                            pass
                        _dbg_early.set_panel_finder(
                            top_y=_title_y_int,
                            mineral_y_top=_mineral_band[0] or None,
                            mineral_y_bot=_mineral_band[1] or None,
                            mineral_center=(
                                (_mineral_band[0] + _mineral_band[1]) // 2
                                if _mineral_band[0] and _mineral_band[1]
                                else None
                            ),
                            pitch=int(_title_h_int * 1.4),
                            bot_line_y=None,
                            source="early_direct_label_match",
                            title_box=_get_cached_title_box(),
                        )
                    except Exception:
                        pass
                    _emit_label_rows_overlay(_early_direct_result)
                    return _early_direct_result
            except Exception as _cal_pre_exc:  # pragma: no cover
                log.debug(
                    "PRE-ANCHOR EARLY-DIRECT / auto-calibration failed: %s",
                    _cal_pre_exc,
                )
    except Exception as _pre_exc:
        log.debug("pre-anchor detection failed: %s", _pre_exc)

    # ── MANUAL OVERRIDE MODE ──
    # When the user flips manual-override on for this region, every
    # auto-detect path is bypassed. The user's drawn rectangles ARE
    # the answer; we just translate them into the standard label_rows
    # tuple shape so downstream OCR consumes them like any other source.
    _region = _get_current_region()
    if _region is not None:
        try:
            from .sc_ocr import calibration as _cal_mo
            if _cal_mo.get_manual_override_mode(_region):
                log.info(
                    "hud: manual override mode active — bypassing auto-detect"
                )
                manual_rows = _build_manual_override_label_rows(
                    _region, img.width, img.height,
                )
                # Telemetry: push the user's boxes into the debug overlay
                # so the live viewer shows them just like any other
                # detection path.
                try:
                    from .sc_ocr import debug_overlay as _dbg
                    _mineral = manual_rows.get("_mineral_row")
                    if _mineral:
                        _dbg.set_panel_finder(
                            top_y=None,
                            mineral_y_top=_mineral[0],
                            mineral_y_bot=_mineral[1],
                            mineral_center=(_mineral[0] + _mineral[1]) // 2,
                            pitch=None,
                            bot_line_y=None,
                            source="manual_override",
                            title_box=_get_cached_title_box(),
                        )
                except Exception:
                    pass
                return manual_rows
        except Exception as _mo_exc:
            log.debug("manual override lookup failed: %s", _mo_exc)

    # ── ZEROTH: persistent calibration ──
    # If the user has saved calibration for the current region, use it
    # and skip ALL detection.
    if _region is not None:
        log.debug(
            "_find_label_rows: ZEROTH (calibration) starting at %.0fms",
            _total_elapsed_ms(),
        )
        _t_zeroth = time.monotonic()
        try:
            from .sc_ocr import calibration as _cal
            cal_result = _cal.to_label_rows(
                _region, img.width, img.height, img=img,
            )
            # One-shot diagnostic: log whether calibration is loaded for
            # this region so the user can verify in the log file. The
            # rate-limiter guards against per-scan log spam.
            try:
                _log_calibration_state(_region, cal_result)
            except Exception:
                pass
            log.debug(
                "_find_label_rows: ZEROTH completed in %.0fms (result=%s)",
                (time.monotonic() - _t_zeroth) * 1000.0,
                "OK" if cal_result else "empty",
            )
            if cal_result:
                # Push telemetry to debug overlay. The title_box was
                # already detected at the top of this function and
                # stashed in the per-thread cache; reuse it instead of
                # re-running find_scan_results_anchor (which would
                # double the anchor-detection cost on the calibration
                # short-circuit path).
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
                        title_box=_get_cached_title_box(),
                    )
                except Exception:
                    pass
                log.debug(
                    "label_rows from calibration: %s",
                    {k: v for k, v in cal_result.items()},
                )
                # Publish rows to debug overlay so any caller (cal_live_refresh
                # worker, scan_hud_onnx, etc.) gets the cyan band boxes painted.
                _emit_label_rows_overlay(cal_result)
                return cal_result
        except Exception as exc:
            log.debug("calibration lookup failed: %s", exc)

    # ── STABILIZER: phase-correlation panel tracking ──
    # Once an absolute panel position has been established (via the
    # rigid-body TRACKER tier below), the stabilizer caches a 256x64
    # patch of the panel region and computes frame-to-frame motion
    # via phase correlation of consecutive frames' patches. This is
    # sub-millisecond per frame, structurally immune to the
    # template-NCC false-match problem (correlation between two
    # consecutive frames has a unique peak at the true motion vector
    # — the panel can't be in two places at once), and accumulates
    # smooth motion trajectories naturally.
    #
    # The stabilizer falls back to the rigid-body TRACKER for cold
    # start and periodic re-anchor (every 30 frames) to correct any
    # sub-pixel drift. A failed stabilize() call falls through to
    # TRACKER unchanged, so this tier is strictly an optimization —
    # never makes the system worse than the legacy behaviour.
    log.debug(
        "_find_label_rows: STABILIZER (phase correlation) starting at %.0fms",
        _total_elapsed_ms(),
    )
    _t_stab_start = time.monotonic()
    try:
        stabilizer = _get_or_create_stabilizer()
        if stabilizer is not None:
            stab_result = stabilizer.stabilize(img)
            _stab_elapsed = (
                time.monotonic() - _t_stab_start
            ) * 1000.0
            _stab_usable = _result_is_usable(stab_result)
            log.debug(
                "_find_label_rows: STABILIZER completed in %.0fms "
                "(locked=%s pose=%s result=%s)",
                _stab_elapsed,
                stabilizer.is_locked,
                stabilizer.pose,
                "OK" if _stab_usable else "empty",
            )
            if _stab_usable:
                log.info(
                    "_find_label_rows: STABILIZER returned lock "
                    "(pose=%s total elapsed=%.0fms)",
                    stabilizer.pose, _total_elapsed_ms(),
                )
                _emit_label_rows_overlay(stab_result)
                return stab_result
            # Surface the failure reason at WARNING level. This logger
            # (onnx_hud_reader) is already in the debug-viewer whitelist
            # so the user sees it regardless of how hud_panel_stabilizer
            # is configured. Critical for diagnosing why the lock isn't
            # holding.
            _stab_reason = getattr(stabilizer, "last_failure_reason", None)
            if _stab_reason:
                log.warning(
                    "_find_label_rows: STABILIZER did not lock — %s",
                    _stab_reason,
                )
    except Exception as _stab_exc:
        log.warning(
            "HudPanelStabilizer tier raised exception: %s",
            _stab_exc, exc_info=True,
        )

    # ── TRACKER: rigid-body panel pose ──
    # The HUD panel is a rigid body that moves predictably (≤30 px
    # between frames in normal gameplay). A frame-by-frame tracker
    # solves for (panel_x, panel_y, scale) from a pool of anchor
    # measurements and re-uses the previous frame's pose to do local
    # search in the next, eliminating the cross-frame jitter that the
    # prior full-image-search pipeline produced.
    #
    # On cold start (no prior pose) the tracker falls back to full-
    # frame anchor search internally. If it can't establish a lock —
    # too few anchors, residuals too high, or a velocity violation —
    # it returns None and the existing PRIMARY/SECONDARY/.../TESSERACT
    # fallback tiers below run unchanged. This is a strict opt-in
    # layer: a failed tracker call never makes the system worse than
    # the legacy behaviour.
    #
    # Note: when STABILIZER above succeeds, this tier is skipped
    # (early return). When STABILIZER fails (cold-start, lost lock),
    # it internally invokes this TRACKER to re-acquire — so the
    # tracker tier here mainly serves as a backup when STABILIZER
    # itself is unavailable / disabled.
    log.debug(
        "_find_label_rows: TRACKER (rigid-body pose) starting at %.0fms",
        _total_elapsed_ms(),
    )
    _t_tracker_start = time.monotonic()
    try:
        tracker = _get_or_create_tracker()
        if tracker is not None:
            tracker_result = tracker.track(img)
            _tracker_elapsed = (
                time.monotonic() - _t_tracker_start
            ) * 1000.0
            _tracker_usable = _result_is_usable(tracker_result)
            log.debug(
                "_find_label_rows: TRACKER completed in %.0fms "
                "(locked=%s pose=%s result=%s)",
                _tracker_elapsed,
                tracker.is_locked,
                tracker.last_pose,
                "OK" if _tracker_usable else "empty",
            )
            if _tracker_usable:
                log.info(
                    "_find_label_rows: TRACKER returned lock "
                    "(pose=%s total elapsed=%.0fms)",
                    tracker.last_pose, _total_elapsed_ms(),
                )
                _emit_label_rows_overlay(tracker_result)
                return tracker_result
            # Surface the failure reason at WARNING level so the user
            # can see WHY the lock isn't holding regardless of any
            # debug-viewer logger whitelist filtering.
            _tracker_reason = getattr(tracker, "last_failure_reason", None)
            if _tracker_reason:
                log.warning(
                    "_find_label_rows: TRACKER did not lock — %s",
                    _tracker_reason,
                )
    except Exception as _tracker_exc:
        log.warning(
            "HudPanelTracker tier raised exception: %s",
            _tracker_exc, exc_info=True,
        )

    # ── PRIMARY: SCAN RESULTS title anchor ──
    # The "SCAN RESULTS" title is the most stable feature of the rock-
    # scan panel: large bold static text, always at the top, identical
    # across every rock, every panel scale, every HUD color. Once we
    # locate it, every other row's position is a known proportional
    # offset from the title — no per-frame guessing, no risk of NCC
    # false positives in the COMPOSITION rows below the panel data.
    #
    # This runs BEFORE label-template NCC because NCC can be fooled by
    # COMPOSITION rows when the user's capture region extends past the
    # SCAN RESULTS panel (e.g. tall regions that include "RAW SILICON
    # 245" / "HEPHAESTANITE (RAW) 96" — those texts contain glyphs
    # that NCC-correlate against MASS/RESIST/INSTAB templates and
    # produce convincing-but-wrong row positions).
    #
    # Cache the anchor for 1 s per (image-size) — shortened from 5 s.
    # The original 5-s TTL was sized for the Tesseract anchor path
    # (200-500 ms per call); the NCC path is ~5 ms so cross-scan
    # caching saves little. More importantly, the longer TTL FROZE the
    # anchor through cross-frame jitter, and the cache-expiry boundary
    # then surfaced as a 10-25 px JUMP every 5 seconds in production
    # logs (see scan_results_match._smooth_anchor). With a 1-s TTL the
    # anchor tracker in scan_results_match sees nearly every scan and
    # smooths frame-to-frame jitter EMA-style instead of hiding it
    # behind a stale-cache freeze. The tracker also handles the slow-
    # drift / panel-disappear cases that the long TTL used to absorb.
    log.debug(
        "_find_label_rows: PRIMARY (SCAN RESULTS anchor) starting at %.0fms",
        _total_elapsed_ms(),
    )
    _t_primary_start = time.monotonic()
    try:
        import time as _t_anchor
        _anchor_key = (img.width, img.height)
        _now = _t_anchor.monotonic()
        _cached = _scan_results_anchor_cache.get(_anchor_key)
        anchor: Optional[dict] = None
        if _cached is not None and (_now - _cached[0]) < 1.0:
            anchor = _cached[1]
            log.debug(
                "_find_label_rows: PRIMARY cache HIT for key=%s",
                _anchor_key,
            )
        else:
            # Try NCC anchor first (template-based, ~5 ms, tilt-tolerant).
            # Fall through to Tesseract if the NCC template is missing
            # or no scale crosses the confidence threshold.
            try:
                _t_ncc_anchor = time.monotonic()
                from .sc_ocr import scan_results_match as _srm
                anchor = _srm.find_scan_results_anchor(img)
                log.debug(
                    "_find_label_rows: PRIMARY NCC anchor took %.0fms "
                    "(result=%s)",
                    (time.monotonic() - _t_ncc_anchor) * 1000.0,
                    "OK" if anchor is not None else "None",
                )
            except Exception as _srm_exc:
                log.debug(
                    "scan_results_match unavailable, falling back to "
                    "Tesseract anchor: %s", _srm_exc,
                )
                anchor = None
            # Skip the slow Tesseract anchor fallback when we're
            # already over budget — the next path will pick up.
            if anchor is None and (
                (time.monotonic() - _t_primary_start) * 1000.0
                < _BUDGET_PRIMARY_MS - 1000
            ):
                _t_tess_anchor = time.monotonic()
                anchor = _find_scan_results_anchor(img)
                log.debug(
                    "_find_label_rows: PRIMARY Tesseract anchor took %.0fms "
                    "(result=%s)",
                    (time.monotonic() - _t_tess_anchor) * 1000.0,
                    "OK" if anchor is not None else "None",
                )
            if anchor is not None:
                _scan_results_anchor_cache[_anchor_key] = (_now, anchor)
        if anchor is not None:
            # Refresh the cached title_box if the anchor we just found
            # disagrees with what the pre-anchor pass produced (different
            # detection variant).
            _set_cached_title_box((
                int(anchor["title_x"]),
                int(anchor["title_y"]),
                int(anchor["title_w"]),
                int(anchor["title_h"]),
            ))
            # ────────────────────────────────────────────────────────────
            # RESULT-FIRST ORDERING (the key fix):
            # Run the anchor-driven row finder, and if it produces a
            # usable result, RETURN IT regardless of how long the work
            # took. Budget exhaustion only blocks the NEXT path, never
            # discards a completed dict. The previous code did the
            # opposite — and the user's logs showed legit ~50 s scans
            # that produced all 3 rows getting thrown away because the
            # budget had elapsed by the time the result was ready.
            # ────────────────────────────────────────────────────────────
            anchor_result = _label_rows_from_anchor(img, anchor)
            _primary_path_elapsed = (
                time.monotonic() - _t_primary_start
            ) * 1000.0
            log.debug(
                "_find_label_rows: PRIMARY completed in %.0fms (result=%s)",
                _primary_path_elapsed,
                "OK" if _result_is_usable(anchor_result) else "empty",
            )
            if _primary_path_elapsed > _BUDGET_PRIMARY_MS:
                log.warning(
                    "_find_label_rows: PRIMARY exceeded %dms budget "
                    "(took %.0fms) — but result will still be returned "
                    "if usable",
                    _BUDGET_PRIMARY_MS, _primary_path_elapsed,
                )
            if _result_is_usable(anchor_result):
                # Push telemetry to the debug overlay so the panel
                # finder shows where SCAN RESULTS was located.
                try:
                    from .sc_ocr import debug_overlay as _dbg
                    _mineral = anchor_result.get(
                        "_mineral_row", (0, 0, 0)
                    )
                    # Measured pitch from the actual row geometry —
                    # more accurate than title_h * 1.4 (which is wrong
                    # whenever Tesseract's bbox on a tilted title
                    # inflates title_h).
                    _mass_row = anchor_result.get("mass")
                    if _mineral and _mass_row:
                        _measured_pitch = (
                            (_mass_row[0] + _mass_row[1]) // 2
                            - (_mineral[0] + _mineral[1]) // 2
                        )
                    else:
                        _measured_pitch = int(anchor["title_h"] * 1.4)
                    _dbg.set_panel_finder(
                        top_y=anchor["title_y"],
                        mineral_y_top=_mineral[0],
                        mineral_y_bot=_mineral[1],
                        mineral_center=(_mineral[0] + _mineral[1]) // 2,
                        pitch=_measured_pitch,
                        bot_line_y=None,
                        source="scan_results_anchor",
                        title_box=_get_cached_title_box(),
                    )
                except Exception:
                    pass
                log.info(
                    "_find_label_rows: PRIMARY returning result "
                    "(title @ x=%d y=%d w=%d h=%d, total elapsed=%.0fms)",
                    anchor["title_x"], anchor["title_y"],
                    anchor["title_w"], anchor["title_h"],
                    _total_elapsed_ms(),
                )
                # Publish rows to debug overlay so any caller (cal_live_refresh
                # worker, scan_hud_onnx, etc.) gets the cyan band boxes painted.
                _emit_label_rows_overlay(anchor_result)
                return anchor_result
    except Exception as exc:
        log.debug("SCAN RESULTS anchor path failed: %s", exc)

    # Budget gate: only blocks the NEXT path. If PRIMARY produced a
    # result we already returned above — this gate never throws away
    # successful work.
    if _total_budget_exhausted():
        log.warning(
            "_find_label_rows: total %dms budget exhausted after PRIMARY — "
            "skipping remaining fallbacks (elapsed=%.0fms)",
            _BUDGET_TOTAL_MS, _total_elapsed_ms(),
        )
        _emit_anchor_only_overlay()
        return {}

    # ── SECONDARY: NCC label template matching ──
    # Concrete per-row pixel positions from matching the rendered
    # MASS:/RESISTANCE:/INSTABILITY: label templates against the
    # panel image. No geometry inference, no Tesseract subprocess —
    # just NumPy correlation against canonicalized templates.
    # Cached per region in the caller.
    log.debug(
        "_find_label_rows: SECONDARY (NCC labels) starting at %.0fms",
        _total_elapsed_ms(),
    )
    _t_secondary_start = time.monotonic()
    ncc_result = _find_label_rows_by_ncc(img)
    _secondary_elapsed = (time.monotonic() - _t_secondary_start) * 1000.0
    log.debug(
        "_find_label_rows: SECONDARY completed in %.0fms (result=%s)",
        _secondary_elapsed,
        "OK" if _result_is_usable(ncc_result) else "empty",
    )
    if _secondary_elapsed > _BUDGET_SECONDARY_MS:
        log.warning(
            "_find_label_rows: SECONDARY exceeded %dms budget "
            "(took %.0fms) — but result will still be returned if usable",
            _BUDGET_SECONDARY_MS, _secondary_elapsed,
        )
    # Result-first: a usable SECONDARY result is returned regardless of
    # budget. The budget only governs the next-path gate below.
    if _result_is_usable(ncc_result):
        # Publish rows to debug overlay so any caller (cal_live_refresh
        # worker, scan_hud_onnx, etc.) gets the cyan band boxes painted.
        _emit_label_rows_overlay(ncc_result)
        return ncc_result

    if _total_budget_exhausted():
        log.warning(
            "_find_label_rows: total %dms budget exhausted after SECONDARY — "
            "skipping remaining fallbacks (elapsed=%.0fms)",
            _BUDGET_TOTAL_MS, _total_elapsed_ms(),
        )
        _emit_anchor_only_overlay()
        return {}

    # ── TERTIARY: position-based finder (5-band scan from actual text) ──
    log.debug(
        "_find_label_rows: TERTIARY (position bands) starting at %.0fms",
        _total_elapsed_ms(),
    )
    _t_tertiary_start = time.monotonic()
    pos_result = _find_label_rows_by_position(img)
    _tertiary_elapsed = (time.monotonic() - _t_tertiary_start) * 1000.0
    log.debug(
        "_find_label_rows: TERTIARY completed in %.0fms (result=%s)",
        _tertiary_elapsed,
        "OK" if _result_is_usable(pos_result) else "empty",
    )
    if _tertiary_elapsed > _BUDGET_TERTIARY_MS:
        log.warning(
            "_find_label_rows: TERTIARY exceeded %dms budget "
            "(took %.0fms) — but result will still be returned if usable",
            _BUDGET_TERTIARY_MS, _tertiary_elapsed,
        )
    if _result_is_usable(pos_result):
        # Publish rows to debug overlay so any caller (cal_live_refresh
        # worker, scan_hud_onnx, etc.) gets the cyan band boxes painted.
        _emit_label_rows_overlay(pos_result)
        return pos_result

    if _total_budget_exhausted():
        log.warning(
            "_find_label_rows: total %dms budget exhausted after TERTIARY — "
            "skipping remaining fallbacks (elapsed=%.0fms)",
            _BUDGET_TOTAL_MS, _total_elapsed_ms(),
        )
        _emit_anchor_only_overlay()
        return {}

    # ── QUATERNARY: HUD-line-bracketed grid + fixed fractions ──
    log.debug(
        "_find_label_rows: QUATERNARY (HUD grid) starting at %.0fms",
        _total_elapsed_ms(),
    )
    _t_quaternary_start = time.monotonic()
    grid_result = _find_label_rows_by_hud_grid(img)
    _quaternary_elapsed = (time.monotonic() - _t_quaternary_start) * 1000.0
    log.debug(
        "_find_label_rows: QUATERNARY completed in %.0fms (result=%s)",
        _quaternary_elapsed,
        "OK" if _result_is_usable(grid_result) else "empty",
    )
    if _quaternary_elapsed > _BUDGET_QUATERNARY_MS:
        log.warning(
            "_find_label_rows: QUATERNARY exceeded %dms budget "
            "(took %.0fms) — but result will still be returned if usable",
            _BUDGET_QUATERNARY_MS, _quaternary_elapsed,
        )
    if _result_is_usable(grid_result):
        # Publish rows to debug overlay so any caller (cal_live_refresh
        # worker, scan_hud_onnx, etc.) gets the cyan band boxes painted.
        _emit_label_rows_overlay(grid_result)
        return grid_result

    if _total_budget_exhausted():
        log.warning(
            "_find_label_rows: total %dms budget exhausted after QUATERNARY — "
            "skipping Tesseract fallback (elapsed=%.0fms)",
            _BUDGET_TOTAL_MS, _total_elapsed_ms(),
        )
        _emit_anchor_only_overlay()
        return {}

    # ── Tesseract per-label fallback (deepest) ──
    log.debug(
        "_find_label_rows: TESSERACT (per-label fallback) starting at %.0fms",
        _total_elapsed_ms(),
    )
    if not _check_tesseract():
        _emit_anchor_only_overlay()
        return {}
    try:
        import pytesseract
    except ImportError:
        _emit_anchor_only_overlay()
        return {}

    _t_tess_start = time.monotonic()
    # pytesseract's timeout= kwarg is enforced via SIGTERM on the
    # tesseract child process. Pass our per-label budget (in seconds)
    # so a hung subprocess can't block the caller for tens of seconds.
    # Add a 1-second safety margin so the python-side timeout fires
    # AFTER any pytesseract-internal timeout, not before.
    _TESS_LABEL_TIMEOUT_S = max(1, _BUDGET_TESS_PER_LABEL_MS // 1000)

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
        # Per-label budget cap: each Tesseract invocation can spawn a
        # subprocess that occasionally hangs for tens of seconds. If we
        # blow the per-label budget OR the total budget while inside
        # this loop, abandon any remaining variants and use whatever
        # we already collected.
        if (time.monotonic() - _t_tess_start) * 1000.0 > _BUDGET_TESS_PER_LABEL_MS:
            log.warning(
                "_find_label_rows: TESSERACT exceeded %dms per-label budget "
                "— moving on (variants tried before abort)",
                _BUDGET_TESS_PER_LABEL_MS,
            )
            break
        if _total_budget_exhausted():
            log.warning(
                "_find_label_rows: total %dms budget exhausted inside "
                "Tesseract loop — aborting (elapsed=%.0fms)",
                _BUDGET_TOTAL_MS, _total_elapsed_ms(),
            )
            break
        binary_pil = Image.fromarray(binary)
        try:
            data = pytesseract.image_to_data(
                binary_pil,
                config=(
                    "--psm 11 -c tessedit_char_whitelist="
                    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz:"
                ),
                output_type=pytesseract.Output.DICT,
                timeout=_TESS_LABEL_TIMEOUT_S,
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
        _emit_anchor_only_overlay()
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
        _emit_anchor_only_overlay()
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
        try:
            from .sc_ocr import scan_record as _srec
            _srec.write(sc_result if isinstance(sc_result, dict) else {}, elapsed)
        except Exception:
            pass
        return sc_result
    except Exception as exc:
        # Include the full traceback so we can locate the actual line
        # that's raising — the bare ``%s`` was masking 1000+ identical
        # ``KeyError: 'instability'`` failures with no line info.
        log.error("sc_ocr failed: %s", exc, exc_info=True)
        return result
