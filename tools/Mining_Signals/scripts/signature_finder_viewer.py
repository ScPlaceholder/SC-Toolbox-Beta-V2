"""Live diagnostic for the signature scanner.

Polls the configured signal-scan region every ~500 ms, runs the
sc_ocr.api icon-anchored pipeline on it, and displays:

  * the captured panel image, scaled up
  * RED box around the location-pin icon (NCC anchor)
  * GREEN box around the digit cluster (everything to the right of icon)
  * Tesseract's reading + range-validation result
  * History of the last 10 reads with timestamps

Use this to verify the signature scanner has the icon AND the digits
inside its scan region. If the icon NCC anchor jumps around scan-to-
scan (or fails to match), you'll see it immediately. If the typed
value the OCR returns drifts, you'll see when and why.

Reads the same scan region the live runtime uses (from
mining_signals_config.json's "ocr_region"), so you don't need to
calibrate twice.

Run with:
    python scripts/signature_finder_viewer.py
or double-click LAUNCH_SignatureFinderViewer.bat in training_data_panels/.
"""
from __future__ import annotations

import json
import logging
import queue
import sys
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageFont

THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
sys.path.insert(0, str(TOOL))
sys.path.insert(0, str(TOOL / "scripts"))

# Config path resolution is shared with ui/app.py via
# mining_shared/paths.py — both processes now compute identical
# paths from one source of truth instead of duplicating the logic
# here with a hardcoded persistent path.  Pre-refactor, the viewer
# hardcoded the persistent path WITHOUT the app's makedirs-fallback,
# so on locked-down machines (Controlled Folder Access etc.) the
# app would silently fall back to the in-tool dir while the viewer
# kept reading the unreachable persistent path — a silent drift
# class that's now impossible because both go through paths.py.
from mining_shared.paths import resolve_config_path as _resolve_config_path
CONFIG_PATH = _resolve_config_path()
POLL_MS = 500
HISTORY_LEN = 10

# Theme matches the rest of the toolbox tools.
ACCENT = "#33dd88"
RED = "#ff4444"
DIM = "#888888"
BG = "#1e1e1e"
FG = "#e0e0e0"

# Mirrors LOCK_GREEN / LOCK_GRAY in ui/calibration_dialog.py — keeps the
# locked-state styling visually consistent across the two windows even
# though we deliberately don't import from that module (this script
# can run as a standalone process via SingleInstance("signature_finder")).
LOCK_GREEN = "#2a8"
LOCK_GREEN_BRIGHT = "#5fff9c"   # the "lime" overlay color when locked
LOCK_GRAY = "#555"

# Debug-mode overlay colours. Match the annotator-debugger convention so
# users moving between the two windows recognise the boxes immediately:
#   PILL    = cyan   (find_hud_panel result)
#   CLUSTER = lime   (find_digit_cluster result, distinct from the
#                    final digit_box overlay so users can tell whether
#                    the cluster matched and was promoted to digit_box,
#                    or matched but was overridden by the icon-anchor
#                    path's combo decision)
#   GLYPH   = magenta (per-glyph segmenter output)
DEBUG_PILL_COLOR = "#00d4ff"
DEBUG_CLUSTER_COLOR = "#a8ff60"
DEBUG_GLYPH_COLOR = "#ff66dd"

# Default placeholder used when the user clicks ✏ Set on a MISS.  This is
# the same coordinate as ``_RowControl._PLACEHOLDER_BOXES["signature"]``
# in ui/calibration_dialog.py — keep these in sync if either side ever
# changes.  Coordinates are signal-region-relative.
_SIGNATURE_PLACEHOLDER_BOX: dict = {"x": 200, "y": 10, "w": 120, "h": 30}


def _load_scan_region() -> Optional[dict]:
    """Read ocr_region from the active config. Returns None if the file
    or key is missing.

    Resolves the config path on every call so the persistent file is
    picked up the moment the user saves a region in the main app
    (avoids the stale CONFIG_PATH that froze at module-load time).
    """
    path = _resolve_config_path()
    if not path.is_file():
        return None
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    region = cfg.get("ocr_region")
    if not region or not all(k in region for k in ("x", "y", "w", "h")):
        return None
    return region


def _capture(region: dict) -> Optional[Image.Image]:
    """mss screen capture of the region. Falls back to PIL.ImageGrab
    if mss is unavailable (matches screen_reader.capture_region's
    fallback chain)."""
    try:
        from PIL import ImageGrab
        bbox = (
            int(region["x"]), int(region["y"]),
            int(region["x"]) + int(region["w"]),
            int(region["y"]) + int(region["h"]),
        )
        img = ImageGrab.grab(bbox=bbox, all_screens=True)
        if img.mode != "RGB":
            img = img.convert("RGB")
        return img
    except Exception:
        pass
    try:
        import mss
        with mss.mss() as sct:
            grab = sct.grab({
                "left": int(region["x"]), "top": int(region["y"]),
                "width": int(region["w"]), "height": int(region["h"]),
            })
            return Image.frombytes(
                "RGB", grab.size, grab.bgra, "raw", "BGRX",
            )
    except Exception:
        return None


def _annotate(
    img: Image.Image,
    icon_box: Optional[tuple[int, int, int, int]],
    digit_box: Optional[tuple[int, int, int, int]],
    digit_color: str = ACCENT,
    debug_pill_box: Optional[tuple[int, int, int, int]] = None,
    debug_cluster_box: Optional[tuple[int, int, int, int]] = None,
    debug_glyph_boxes: Optional[list[tuple[int, int, int, int]]] = None,
    debug_labels: Optional[list[tuple[int, int, str, str]]] = None,
) -> Image.Image:
    """Draw colored overlays on a copy of `img`.

    ``digit_color`` overrides the green digit-cluster outline — used by
    the viewer to render the locked-state box in a brighter lime so the
    user can tell at a glance the rectangle is persisted to
    calibration.json.

    The ``debug_*`` arguments are populated only when the popout's
    Debug Mode toggle is on. They mirror what the standalone
    Signature Annotator Debugger draws:
      * ``debug_pill_box``: cyan rectangle from ``find_hud_panel``
      * ``debug_cluster_box``: lime rectangle from ``find_digit_cluster``
        BEFORE the combo decision picks between icon-anchor and
        cluster-anchor paths
      * ``debug_glyph_boxes``: magenta per-glyph rectangles from
        ``_segment_glyphs``, in source-image coordinates
      * ``debug_labels``: list of (x, y, text, color) tuples drawn at
        their absolute positions; used for "icon NCC=0.78" style
        annotations next to each box
    """
    out = img.copy()
    draw = ImageDraw.Draw(out)
    # Draw debug overlays first so primary RED/GREEN boxes paint on top
    # — the user's eye should still go to the chosen icon + digit_box
    # outlines. Debug overlays are context, not the primary signal.
    if debug_pill_box is not None:
        x1, y1, x2, y2 = debug_pill_box
        draw.rectangle(
            [x1, y1, x2 - 1, y2 - 1],
            outline=DEBUG_PILL_COLOR, width=1,
        )
    if debug_cluster_box is not None:
        x1, y1, x2, y2 = debug_cluster_box
        draw.rectangle(
            [x1, y1, x2 - 1, y2 - 1],
            outline=DEBUG_CLUSTER_COLOR, width=1,
        )
    if debug_glyph_boxes:
        for gx1, gy1, gx2, gy2 in debug_glyph_boxes:
            draw.rectangle(
                [gx1, gy1, gx2 - 1, gy2 - 1],
                outline=DEBUG_GLYPH_COLOR, width=1,
            )
    if icon_box is not None:
        x1, y1, x2, y2 = icon_box
        draw.rectangle([x1, y1, x2 - 1, y2 - 1], outline=RED, width=2)
    if digit_box is not None:
        x1, y1, x2, y2 = digit_box
        draw.rectangle([x1, y1, x2 - 1, y2 - 1], outline=digit_color, width=2)
    if debug_labels:
        # Tiny text labels — kept terse so they don't clutter the
        # capture preview. Font fallback chain mirrors PIL's default
        # behaviour when no system font is available.
        try:
            font = ImageFont.load_default()
        except Exception:
            font = None
        for lx, ly, ltext, lcolor in debug_labels:
            try:
                draw.text((int(lx), int(ly)), ltext, fill=lcolor, font=font)
            except Exception:
                pass
    return out


# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────

from PySide6.QtCore import Qt, QMimeData, QObject, QRunnable, QThreadPool, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPalette, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)
from PIL.ImageQt import ImageQt  # noqa: E402


# Loggers captured by the signature-finder Debug Mode panel. Different
# from panel_finder_popout's set — this viewer covers region2 (signature
# scanner) diagnostics, which run through signal_anchor and the icon-*
# matchers rather than the HUD/chrome-line/mineral-name pipeline.
_DEBUG_LOGGERS = (
    "ocr.sc_ocr.api",
    "ocr.sc_ocr.signal_anchor",
    "ocr.sc_ocr.signal_solve",
    "ocr.sc_ocr.signal_record",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.icon_contour",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.comma_finder",
)

_DEBUG_LOG_MAX_LINES = 500

# ── Signature pipeline flow tree (Debug Mode) ──
# A compact, signature-only version of the panel_finder_popout dual-route
# flowchart. Stages light up idle → active → done as ``_sig_flow_mark``
# advances them from the captured log stream (first matching rule wins), so a
# non-expert can watch the Route-2 signature scan run end-to-end.
_SIG_STAGES = [
    ("capture", "① capture region"),
    ("pill", "② find pill / icon"),
    ("crop", "③ locate the number"),
    ("comma", "④ comma + extend crop"),
    ("digits", "⑤ read digits (CNN)"),
    ("crnn", "⑥ CRNN cross-check"),
    ("flap", "⑦ consistency → value"),
]
_SIG_FLOW_RULES = [
    ("icon_voter", "pill"),
    ("icon_rgb_ncc", "pill"),
    ("icon_geometry", "pill"),
    ("signal_solve", "pill"),
    ("world_model", "pill"),
    ("localize_icon", "pill"),
    ("comma_finder", "comma"),
    ("signal_anchor", "crop"),
    ("signal_record", "flap"),
    ("scanning timeout", "_timeout"),
    ("crnn", "crnn"),
    ("segment", "digits"),
    ("glyph", "digits"),
    ("comma", "comma"),
    ("pill", "pill"),
    ("flap", "flap"),
]
_SIG_IDLE = ("QLabel{background:#222;color:#7a7a7a;border:1px solid #363636;"
             "border-radius:5px;padding:2px 6px;font-family:Consolas;"
             "font-size:8pt;}")
_SIG_ACTIVE = ("QLabel{background:#16385c;color:#e6f3ff;border:1px solid "
               "#5ab0ff;border-radius:5px;padding:2px 6px;font-family:Consolas;"
               "font-size:8pt;font-weight:bold;}")
_SIG_DONE = ("QLabel{background:#173318;color:#bfe9bf;border:1px solid #3a7a3a;"
             "border-radius:5px;padding:2px 6px;font-family:Consolas;"
             "font-size:8pt;}")


class _QueueLogHandler(logging.Handler):
    """Thread-safe ``logging.Handler`` that pushes each formatted record
    into a ``queue.Queue``. The scan worker thread emits log records
    from off-thread; the main-thread poll timer drains the queue.
    """

    def __init__(self, sink: queue.Queue):
        super().__init__()
        self._sink = sink

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
            line = f"{ts} {record.name}  {record.levelname}  {record.getMessage()}"
            self._sink.put_nowait(line)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# Worker plumbing — moves the heavy scan work off the UI thread.
# ─────────────────────────────────────────────────────────────


class _SigScanResult:
    """Plain payload object passed from worker → UI via queued signal."""
    __slots__ = (
        "annotated", "icon_found", "digit_box", "value", "dt_ms",
        "anchor_error", "ocr_error",
        # Crop-selector telemetry — the UI uses these to drive the
        # status line and the manual-override state machine.
        "auto_digit_box",   # auto-anchor result regardless of override
        "drawn_box",        # what actually went into the green overlay
        "crop_state",       # "AUTO" / "MANUAL" / "LOCKED"
        # Debug-mode overlay payload — only populated when the popout's
        # Debug Mode checkbox is on. Each is None / [] otherwise so the
        # _annotate path is a no-op for these in normal operation.
        "debug_pill",       # (x1,y1,x2,y2,score) or None
        "debug_cluster",    # (x1,y1,x2,y2) or None
        "debug_glyphs",     # list[(x1,y1,x2,y2)]
        "debug_status",     # short status line for the debug panel
    )

    def __init__(self):
        self.annotated: Optional[Image.Image] = None
        self.icon_found = None  # tuple(x1,y1,x2,y2,score) or None
        self.digit_box = None
        self.value = None
        self.dt_ms = 0.0
        self.anchor_error: Optional[str] = None
        self.ocr_error: Optional[str] = None
        self.auto_digit_box = None
        self.drawn_box = None
        self.crop_state = "AUTO"
        self.debug_pill = None
        self.debug_cluster = None
        self.debug_glyphs = []
        self.debug_status = ""


class _SigScanSignaler(QObject):
    """Lives on the UI thread; emits ``done`` queued from a worker thread."""
    done = Signal(object)


class _SigScanWorker(QRunnable):
    """One scan tick. All heavy work (capture, NCC anchor, OCR, annotate,
    resize) runs in ``run`` on a pool thread. Pure-Python / numpy / PIL
    only — no widget access."""

    def __init__(
        self,
        region: dict,
        anchor_mod,
        api_mod,
        signaler: _SigScanSignaler,
        target_w: int,
        target_h: int,
        manual_box: Optional[dict] = None,
        locked: bool = False,
        debug_mode: bool = False,
    ):
        super().__init__()
        self._region = region
        self._anchor = anchor_mod
        self._api = api_mod
        self._signaler = signaler
        # Snapshot of label size at submit time — used for the resize so
        # the worker doesn't have to touch the widget.
        self._target_w = target_w
        self._target_h = target_h
        # Snapshot of crop-selector state at submit time. Worker stays
        # off the UI thread; whatever the user nudges between worker
        # submissions reaches the next scan via these snapshots.
        self._manual_box = (
            None if manual_box is None else dict(manual_box)
        )
        self._locked = bool(locked)
        # When True, the worker additionally calls the supplementary
        # detectors (find_hud_panel, find_digit_cluster as a separate
        # call, _segment_glyphs on the chosen digit_box) and returns
        # those bboxes for the UI to overlay. Off by default so the
        # normal scan path doesn't pay for diagnostic overhead.
        self._debug_mode = bool(debug_mode)

    def run(self):
        result = _SigScanResult()
        t0 = time.monotonic()
        try:
            img = _capture(self._region)
            if img is None:
                result.anchor_error = "capture failed"
                result.dt_ms = (time.monotonic() - t0) * 1000.0
                self._signaler.done.emit(result)
                return

            gray = np.asarray(img.convert("L"), dtype=np.uint8)
            # Cache the RGB array so signal_anchor's geometric structural
            # validator can run on each CNN-validated candidate. The
            # validator inspects HSV/color (warm-pixel teardrop body
            # with hole + oval base with notch), which is meaningless on
            # the gray array alone. Without RGB the validator silently
            # skips and behavior matches the pre-validator pipeline.
            rgb = np.asarray(img.convert("RGB"), dtype=np.uint8)
            try:
                # Use min_score=0.40 (matches find_digit_crop_box's
                # default) instead of find_icon's library default of
                # 0.55. The 0.55 threshold rejects the icon roughly
                # half the time on chromatically-aberrated SC HUDs
                # where the icon's NCC score wobbles 0.40-0.70 frame-
                # to-frame; when it dips below 0.55, only digit-
                # cluster candidates remain in the list, and the
                # leftmost-only filter (signal_anchor.py:160) is
                # forced to pick a digit cluster as the "icon". Keep
                # the threshold permissive and let the CNN re-rank +
                # leftmost-only filter handle precision instead.
                result.icon_found = self._anchor.find_icon(
                    gray, min_score=0.40, rgb_image=rgb,
                )
            except Exception as e:
                result.anchor_error = f"anchor error: {e}"
            try:
                result.digit_box = self._anchor.find_digit_crop_box(
                    gray, rgb_image=rgb,
                )
            except Exception:
                result.digit_box = None
            # Preserve the auto-anchor result for the UI — even when we
            # override the green overlay with the manual rectangle, the
            # status line still tells the user what the auto-anchor saw.
            result.auto_digit_box = result.digit_box
            try:
                result.value = self._api._signal_recognize_pil(img)
            except Exception as e:
                result.ocr_error = str(e)

            # Manual override: when the user has nudged or locked, draw
            # THEIR rectangle in green instead of the auto-anchor result.
            # This is what makes nudges visible in real time even when
            # the auto-anchor disagrees.
            digit_color = ACCENT
            if self._manual_box is not None:
                mb = self._manual_box
                x1 = int(mb["x"])
                y1 = int(mb["y"])
                x2 = x1 + int(mb["w"])
                y2 = y1 + int(mb["h"])
                result.digit_box = (x1, y1, x2, y2)
                result.crop_state = "LOCKED" if self._locked else "MANUAL"
                if self._locked:
                    digit_color = LOCK_GREEN_BRIGHT
            else:
                result.crop_state = "AUTO"

            icon_xyxy = None
            if result.icon_found is not None:
                x1, y1, x2, y2, _score = result.icon_found
                icon_xyxy = (x1, y1, x2, y2)
            result.drawn_box = result.digit_box

            # ── Debug overlays ──
            # Run the supplementary detectors only when the popout's
            # Debug Mode is on. Each one is wrapped individually so a
            # single detector failure (missing module, malformed input)
            # doesn't take out the rest of the debug payload — the user
            # sees whichever overlays succeeded.
            debug_labels: list[tuple[int, int, str, str]] = []
            if self._debug_mode:
                # 1. Pill — find_hud_panel on RGB. Returns a dict with
                #    bbox in (x, y, w, h) and a confidence score.
                try:
                    from hud_tracker.anchors.hud_color_finder import (
                        find_hud_panel as _find_hud_panel,
                    )
                    pill_res = _find_hud_panel(img)
                    if pill_res is not None and "bbox" in pill_res:
                        px, py, pw, ph = pill_res["bbox"]
                        result.debug_pill = (
                            int(px), int(py),
                            int(px + pw), int(py + ph),
                            float(pill_res.get("confidence", 0.0)),
                        )
                        debug_labels.append((
                            int(px), max(0, int(py) - 10),
                            f"pill conf={pill_res.get('confidence', 0):.2f}",
                            DEBUG_PILL_COLOR,
                        ))
                except Exception as exc:
                    log.debug("debug pill detect failed: %s", exc)

                # 2. Digit cluster — find_digit_cluster called directly,
                #    independent of the icon-anchor combo decision. This
                #    is the same detector but exposed BEFORE
                #    find_digit_crop_box's combo logic possibly overrides
                #    it with the icon-anchor path.
                try:
                    cluster = self._anchor.find_digit_cluster(gray)
                    if cluster is not None:
                        result.debug_cluster = (
                            int(cluster[0]), int(cluster[1]),
                            int(cluster[2]), int(cluster[3]),
                        )
                        debug_labels.append((
                            int(cluster[0]),
                            min(img.height - 10, int(cluster[3]) + 2),
                            "digit_cluster",
                            DEBUG_CLUSTER_COLOR,
                        ))
                except Exception as exc:
                    log.debug("debug digit_cluster failed: %s", exc)

                # 3. Per-glyph segmenter — re-binarize the chosen
                #    digit_box and run _segment_glyphs to surface what
                #    the runtime would feed to the per-digit CNN. The
                #    runtime uses _adaptive_binarize_multi; here we use
                #    a simple percentile threshold which is close
                #    enough for visual debug. Boxes returned are in
                #    cropped-image coords; translate back to source.
                if result.digit_box is not None:
                    try:
                        bx1, by1, bx2, by2 = result.digit_box
                        bx1 = max(0, int(bx1)); by1 = max(0, int(by1))
                        bx2 = min(img.width, int(bx2))
                        by2 = min(img.height, int(by2))
                        if bx2 > bx1 and by2 > by1:
                            crop = img.crop((bx1, by1, bx2, by2))
                            crop_gray = np.asarray(
                                crop.convert("L"), dtype=np.uint8,
                            )
                            # Percentile-80 threshold — matches one of
                            # the recipes the runtime's adaptive
                            # selector picks most often per the user's
                            # logs. Good enough for box visualisation.
                            thr = int(np.percentile(crop_gray, 80))
                            crop_bin = (crop_gray > thr).astype(np.uint8) * 255
                            from ocr.sc_ocr.api import (
                                _segment_glyphs as _seg,
                            )
                            _crops, gboxes = _seg(crop_gray, crop_bin)
                            for gx, gy, gw, gh in gboxes:
                                result.debug_glyphs.append((
                                    bx1 + int(gx), by1 + int(gy),
                                    bx1 + int(gx) + int(gw),
                                    by1 + int(gy) + int(gh),
                                ))
                    except Exception as exc:
                        log.debug("debug glyph segmentation failed: %s", exc)

                # 4. Status line summarising what fired this frame —
                #    surfaces a one-line "what got detected" beneath
                #    the OCR status, useful when the boxes overlap.
                parts = []
                if result.debug_pill is not None:
                    parts.append(f"pill✓({result.debug_pill[4]:.2f})")
                else:
                    parts.append("pill✗")
                if result.icon_found is not None:
                    parts.append(f"icon✓({result.icon_found[4]:.2f})")
                else:
                    parts.append("icon✗")
                if result.debug_cluster is not None:
                    parts.append("cluster✓")
                else:
                    parts.append("cluster✗")
                parts.append(f"glyphs={len(result.debug_glyphs)}")
                result.debug_status = " · ".join(parts)

            annotated = _annotate(
                img, icon_xyxy, result.digit_box,
                digit_color=digit_color,
                debug_pill_box=(
                    result.debug_pill[:4]
                    if result.debug_pill is not None else None
                ),
                debug_cluster_box=result.debug_cluster,
                debug_glyph_boxes=(
                    result.debug_glyphs if result.debug_glyphs else None
                ),
                debug_labels=debug_labels if debug_labels else None,
            )

            max_w = max(200, self._target_w - 16)
            max_h = max(100, self._target_h - 16)
            ratio = min(max_w / annotated.width, max_h / annotated.height)
            if ratio > 1:
                annotated = annotated.resize(
                    (int(annotated.width * ratio), int(annotated.height * ratio)),
                    Image.NEAREST,
                )
            elif ratio < 1:
                annotated = annotated.resize(
                    (int(annotated.width * ratio), int(annotated.height * ratio)),
                    Image.LANCZOS,
                )
            result.annotated = annotated
            result.dt_ms = (time.monotonic() - t0) * 1000.0
        except Exception as e:
            # Safety net — never let an exception kill the worker without
            # delivering a result, or _scan_in_progress would stay True
            # forever.
            result.anchor_error = f"worker crashed: {e}"
            result.dt_ms = (time.monotonic() - t0) * 1000.0
        self._signaler.done.emit(result)


class SignatureFinderViewer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Signature Finder — live anchor + OCR diagnostic")
        self.setMinimumSize(720, 560)
        self.setStyleSheet(f"background: {BG}; color: {FG};")

        self._region: Optional[dict] = _load_scan_region()
        self._history: deque = deque(maxlen=HISTORY_LEN)

        # Debug Mode state. Lazy-built; the queue + handler only exist
        # while the checkbox is on. ``_last_annotated_pil`` keeps the
        # most recent annotated frame in memory so the Copy button can
        # ship it as the clipboard image (the worker thread writes
        # this; the UI thread reads it).
        self._debug_log_queue: queue.Queue = queue.Queue()
        self._debug_log_handler: Optional[_QueueLogHandler] = None
        self._debug_log_lines: list[str] = []
        self._last_annotated_pil: Optional[Image.Image] = None
        # Saved logger levels captured on Debug Mode toggle-ON so we
        # can restore each logger to its pre-toggle state on toggle-
        # OFF / window close. Without this, ``_on_debug_toggled``'s
        # ``setLevel(DEBUG)`` line permanently mutates the shared
        # logger registry — every scan after the first toggle pays
        # DEBUG-level overhead and any other attached handler (file
        # logger, console root) sees verbose chatter forever.
        self._saved_logger_levels: dict[str, int] = {}
        # Mirrors the Debug Mode checkbox state. The poll loop reads
        # this when submitting a scan worker so debug overlays are
        # only drawn when the panel is open. Lives outside
        # ``_on_debug_toggled``'s scope because the checkbox handler
        # runs on the UI thread but the worker submission runs in a
        # different stack frame and needs a stable read.
        self._debug_mode_on: bool = False

        # ── Crop-selector state (mirrors ui/calibration_dialog._RowControl) ──
        # _manual_box: signal-region-relative {x,y,w,h} the user is
        #   currently nudging.  None = use auto-anchor.
        # _is_locked:  True after the user clicks Lock; the box has been
        #   persisted via calibration.save_row().  The signal pipeline
        #   already honours this on its own (api._signal_recognize_pil),
        #   we just track it here to drive the UI state.
        # _last_auto_box: the most recent auto-anchor result, kept so we
        #   can fall back to a sensible rectangle on Lock-with-no-manual.
        self._manual_box: Optional[dict] = None
        self._is_locked: bool = False
        self._last_auto_box: Optional[dict] = None

        self._build_ui()

        # Hydrate from disk: if the user already locked this region in
        # the calibration dialog, surface that immediately so the
        # initial frame draws the lime overlay rather than showing
        # "AUTO" until the next save.
        self._hydrate_lock_from_disk()

        # Don't import the OCR pipeline at module load — defer until
        # actually used so this script can launch even if numpy/scipy
        # take a moment.
        self._api = None
        self._anchor = None

        # Pause-on-move: the OCR pipeline runs on the main Qt thread
        # and takes 100-300 ms per poll (screen grab + NCC anchor +
        # 6 Tesseract calls). When the user grabs the title bar to
        # drag the window, Qt's QMoveEvent queues behind that work
        # and the drag stutters. We set ``_move_pause_until`` from
        # ``moveEvent`` and short-circuit ``_poll`` while the window
        # is being repositioned; polling resumes ~400 ms after the
        # last move event, which feels instant to the user.
        self._move_pause_until = 0.0
        self._move_pause_seconds = 0.4

        # Off-thread scan pipeline. Pool size = 1 so we never run two
        # scans concurrently; combined with ``_scan_in_progress`` the
        # worst case is exactly one queued scan at a time.
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)
        self._signaler = _SigScanSignaler()
        # Default cross-thread connection is QueuedConnection — the
        # slot will run on the UI thread (this object's thread).
        self._signaler.done.connect(self._on_scan_result)
        self._scan_in_progress = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._timer.start(POLL_MS)

    def moveEvent(self, event):
        """Refresh the pause-until window on every move tick. Qt
        fires moveEvent at ~30-60 Hz during a title-bar drag on
        Windows."""
        super().moveEvent(event)
        self._move_pause_until = time.monotonic() + self._move_pause_seconds

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(8)

        # Header — title on the left, Debug Mode toggle on the right.
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(8)
        title = QLabel("SIGNATURE FINDER", self)
        tf = QFont("Consolas")
        tf.setPointSize(13); tf.setBold(True)
        title.setFont(tf)
        title.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        header_row.addWidget(title)
        header_row.addStretch(1)
        self._debug_checkbox = QCheckBox("Debug Mode", self)
        self._debug_checkbox.setStyleSheet(
            f"QCheckBox {{ color: {FG}; font-family: Consolas; "
            f"font-size: 9pt; background: transparent; }}"
        )
        self._debug_checkbox.toggled.connect(self._on_debug_toggled)
        header_row.addWidget(self._debug_checkbox)
        root.addLayout(header_row)

        sub = QLabel(
            "Polls the configured scan region every 500 ms.  "
            "RED = icon anchor (NCC).  GREEN = digit crop.",
            self,
        )
        sub.setStyleSheet(f"color: {DIM}; font-size: 9pt; background: transparent;")
        sub.setWordWrap(True)
        root.addWidget(sub)

        # Region info line
        self._region_lbl = QLabel("", self)
        self._region_lbl.setStyleSheet(
            f"color: {FG}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        root.addWidget(self._region_lbl)

        # Annotated image preview
        self._image_lbl = QLabel("(no scan yet)", self)
        self._image_lbl.setAlignment(Qt.AlignCenter)
        self._image_lbl.setMinimumSize(680, 200)
        self._image_lbl.setStyleSheet(
            f"background: #181818; border: 1px solid #333; padding: 6px;"
        )
        self._image_lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        root.addWidget(self._image_lbl, 1)

        # Big result line
        self._result_lbl = QLabel("—", self)
        rf = QFont("Consolas")
        rf.setPointSize(28); rf.setBold(True)
        self._result_lbl.setFont(rf)
        self._result_lbl.setAlignment(Qt.AlignCenter)
        self._result_lbl.setStyleSheet(
            f"color: {ACCENT}; background: transparent;"
        )
        root.addWidget(self._result_lbl)

        # Crop-selector controls — mirrors the Signature row in
        # ui/calibration_dialog._RowControl. Lets the user fine-tune the
        # signature crop rectangle while watching it work live.
        self._build_crop_selector(root)

        # Status line (anchor score, Tesseract variant, latency)
        self._status_lbl = QLabel("", self)
        self._status_lbl.setAlignment(Qt.AlignCenter)
        self._status_lbl.setStyleSheet(
            f"color: {DIM}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        root.addWidget(self._status_lbl)

        # History panel
        sep = QFrame(self)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {DIM}; background: {DIM};")
        root.addWidget(sep)

        self._history_lbl = QLabel("", self)
        self._history_lbl.setStyleSheet(
            f"color: {FG}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        root.addWidget(self._history_lbl)

        # Refresh region button (in case user changes scan region in
        # the toolbox while this viewer is open)
        btn_row = QHBoxLayout()
        reload_btn = QPushButton("Reload region from config", self)
        reload_btn.setStyleSheet(
            f"background: #444; color: {FG}; padding: 4px 12px; "
            f"border: 1px solid #555; border-radius: 3px;"
        )
        reload_btn.clicked.connect(self._reload_region)
        btn_row.addWidget(reload_btn)
        btn_row.addStretch(1)
        root.addLayout(btn_row)

        # ── Debug panel (hidden until Debug Mode toggled on) ──
        # Sits below the existing history panel rather than replacing
        # it; the user can still see the last 10 reads while the log
        # panel scrolls underneath.
        self._debug_panel = QWidget(self)
        dp_v = QVBoxLayout(self._debug_panel)
        dp_v.setContentsMargins(0, 4, 0, 0)
        dp_v.setSpacing(4)

        dp_btns = QHBoxLayout()
        dp_btns.setContentsMargins(0, 0, 0, 0)
        dp_btns.setSpacing(6)

        self._copy_button = QPushButton("Copy log + image", self)
        self._copy_button.setToolTip(
            "Copy the captured log text AND the current overlay image to "
            "the clipboard. Paste into chat/email/Word for diagnostic reports."
        )
        self._copy_button.setStyleSheet(
            "QPushButton { background: #335577; color: #cce0ff; "
            "padding: 3px 10px; border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #4477aa; }"
        )
        self._copy_button.clicked.connect(self._on_copy_clicked)
        dp_btns.addWidget(self._copy_button)

        self._clear_button = QPushButton("Clear", self)
        self._clear_button.setToolTip("Empty the captured log buffer.")
        self._clear_button.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; padding: 3px 10px; "
            "border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #666; }"
        )
        self._clear_button.clicked.connect(self._on_clear_clicked)
        dp_btns.addWidget(self._clear_button)

        self._debug_status_lbl = QLabel("", self)
        self._debug_status_lbl.setStyleSheet(
            "color: #5fff9c; font-family: Consolas; font-size: 9pt; "
            "background: transparent;"
        )
        dp_btns.addWidget(self._debug_status_lbl, 1)
        dp_v.addLayout(dp_btns)

        # Live signature pipeline tree — its OWN floating window (not crammed
        # into the debug log panel). Shown/hidden with Debug Mode.
        self._sig_flow = self._build_sig_flowchart()

        self._debug_log_widget = QPlainTextEdit(self)
        self._debug_log_widget.setReadOnly(True)
        self._debug_log_widget.setMaximumBlockCount(_DEBUG_LOG_MAX_LINES)
        self._debug_log_widget.setStyleSheet(
            "QPlainTextEdit { background: #181818; color: #cccccc; "
            "border: 1px solid #333; font-family: Consolas; font-size: 9pt; }"
        )
        self._debug_log_widget.setMinimumHeight(160)
        dp_v.addWidget(self._debug_log_widget)

        self._debug_panel.setVisible(False)
        root.addWidget(self._debug_panel, 1)

        self._debug_status_timer = QTimer(self)
        self._debug_status_timer.setSingleShot(True)
        self._debug_status_timer.timeout.connect(
            lambda: self._debug_status_lbl.setText("")
        )

        self._refresh_region_label()

    def _refresh_region_label(self):
        if self._region is None:
            self._region_lbl.setText(
                "⚠ No ocr_region configured — set scan area in Mining Signals."
            )
        else:
            r = self._region
            self._region_lbl.setText(
                f"Region: x={r['x']} y={r['y']} w={r['w']} h={r['h']}  "
                f"|  config: {CONFIG_PATH.name}"
            )

    def _reload_region(self):
        prev_region = self._region
        self._region = _load_scan_region()
        self._refresh_region_label()
        # Region key changed: a previously-locked region's persistence
        # doesn't apply any more. Drop in-memory locked/manual state and
        # try to hydrate from the new region's saved calibration.
        if self._region != prev_region:
            self._manual_box = None
            self._is_locked = False
            self._last_auto_box = None
            self._hydrate_lock_from_disk()
            self._refresh_crop_selector_state()

    # ─────────────────────────────────────────────────────────────
    # Crop-selector controls — mirrors calibration_dialog._RowControl
    # without importing it (this script can run as its own process via
    # SingleInstance("signature_finder")).
    # ─────────────────────────────────────────────────────────────

    def _build_crop_selector(self, root: QVBoxLayout) -> None:
        """Build the nudge / resize / Set / Auto / Lock row + its
        status line. Inserted between the big result label and the
        existing OCR status line."""
        # Row 1: arrows + W/H + ✏ Set + ↻ Auto + 🔒 Lock
        row = QHBoxLayout()
        row.setSpacing(4)

        row.addWidget(self._make_nudge_label("MOVE:"))
        self._btn_left = self._make_nudge_btn(
            "←", -1, 0, 0, 0, "Move crop LEFT 1 px (Shift+click = 5 px)",
        )
        self._btn_up = self._make_nudge_btn(
            "↑", 0, -1, 0, 0, "Move crop UP 1 px (Shift+click = 5 px)",
        )
        self._btn_down = self._make_nudge_btn(
            "↓", 0, +1, 0, 0, "Move crop DOWN 1 px (Shift+click = 5 px)",
        )
        self._btn_right = self._make_nudge_btn(
            "→", +1, 0, 0, 0, "Move crop RIGHT 1 px (Shift+click = 5 px)",
        )
        for b in (self._btn_left, self._btn_up, self._btn_down, self._btn_right):
            row.addWidget(b)

        row.addSpacing(10)
        row.addWidget(self._make_nudge_label("RESIZE:"))
        self._btn_wider = self._make_nudge_btn(
            "W+", 0, 0, +1, 0, "Make crop WIDER (1 px, Shift = 5 px)",
        )
        self._btn_narrow = self._make_nudge_btn(
            "W−", 0, 0, -1, 0, "Make crop NARROWER (1 px, Shift = 5 px)",
        )
        self._btn_taller = self._make_nudge_btn(
            "H+", 0, 0, 0, +1, "Make crop TALLER (1 px, Shift = 5 px)",
        )
        self._btn_shorter = self._make_nudge_btn(
            "H−", 0, 0, 0, -1, "Make crop SHORTER (1 px, Shift = 5 px)",
        )
        for b in (self._btn_wider, self._btn_narrow, self._btn_taller, self._btn_shorter):
            row.addWidget(b)

        row.addStretch(1)

        self._btn_seed_manual = QPushButton("✏ Set")
        self._btn_seed_manual.setToolTip(
            "Drop a starting crop rectangle when auto-anchor MISSes. "
            "Then use ← ↑ ↓ → and W± H± to position, click Lock to save."
        )
        self._btn_seed_manual.setStyleSheet(
            "QPushButton { background: #335577; color: #cce0ff; "
            "padding: 2px 6px; border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #4477aa; }"
        )
        self._btn_seed_manual.clicked.connect(self._on_seed_manual)
        row.addWidget(self._btn_seed_manual)

        self._btn_reset_auto = QPushButton("↻ Auto")
        self._btn_reset_auto.setToolTip(
            "Discard the manual override and revert to live auto-anchor"
        )
        self._btn_reset_auto.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; padding: 2px 6px; "
            "border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #666; }"
        )
        self._btn_reset_auto.clicked.connect(self._on_reset_to_auto)
        row.addWidget(self._btn_reset_auto)

        self._lock_btn = QPushButton("🔒 Lock")
        self._lock_btn.setCursor(Qt.PointingHandCursor)
        self._lock_btn.setMinimumWidth(110)
        self._lock_btn.clicked.connect(self._on_lock_toggle)
        row.addWidget(self._lock_btn)

        root.addLayout(row)

        # Row 2: state-aware status line for the crop selector
        # (separate from the OCR-pipeline status line below).
        self._crop_status_lbl = QLabel("", self)
        self._crop_status_lbl.setStyleSheet(
            f"color: {DIM}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        self._crop_status_lbl.setAlignment(Qt.AlignCenter)
        root.addWidget(self._crop_status_lbl)

        self._apply_lock_style(False)
        self._refresh_crop_selector_state()

    def _make_nudge_label(self, text: str) -> QLabel:
        lbl = QLabel(text, self)
        lbl.setStyleSheet(
            f"color: {DIM}; font-family: Consolas; font-size: 8pt; "
            f"background: transparent;"
        )
        return lbl

    def _make_nudge_btn(
        self, text: str, dx: int, dy: int, dw: int, dh: int, tooltip: str,
    ) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setFixedSize(32, 26)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
            "border: none; font-family: Consolas; font-size: 10pt; "
            "font-weight: bold; }}"
            "QPushButton:hover { background: #777; }"
            "QPushButton:pressed { background: #444; }"
            "QPushButton:disabled { background: #333; color: #666; }"
        )
        btn.setAutoRepeat(True)
        btn.setAutoRepeatInterval(60)
        btn.setAutoRepeatDelay(350)
        btn.clicked.connect(
            lambda _checked=False, dx=dx, dy=dy, dw=dw, dh=dh:
                self._nudge(dx, dy, dw, dh)
        )
        return btn

    # ── State helpers ────────────────────────────────────────────

    def _hydrate_lock_from_disk(self) -> None:
        """Read the persisted signature row for the current region and
        restore it as a locked manual box if present. Called at startup
        and whenever the region changes."""
        if self._region is None:
            return
        try:
            from ocr.sc_ocr import calibration as _cal
            saved = _cal.get_row(self._region, "signature")
        except Exception:
            saved = None
        if saved is not None:
            self._manual_box = {
                "x": int(saved["x"]), "y": int(saved["y"]),
                "w": int(saved["w"]), "h": int(saved["h"]),
            }
            self._is_locked = True

    def _refresh_crop_selector_state(self) -> None:
        """Update button enable-state + the crop-selector status line
        based on current _region / _manual_box / _is_locked."""
        has_region = self._region is not None
        nudge_btns = (
            self._btn_left, self._btn_up, self._btn_down, self._btn_right,
            self._btn_wider, self._btn_narrow, self._btn_taller, self._btn_shorter,
        )
        # Nudges only meaningful when there's a manual box AND the row
        # is not locked (locking freezes the box on disk; the user must
        # unlock to nudge again, mirroring calibration_dialog's UX).
        nudge_enabled = (
            has_region and self._manual_box is not None and not self._is_locked
        )
        for b in nudge_btns:
            b.setEnabled(nudge_enabled)
        # ✏ Set drops the placeholder — only useful when not already
        # locked (and pointless without a region).
        self._btn_seed_manual.setEnabled(has_region and not self._is_locked)
        # ↻ Auto reverts to auto-anchor — only meaningful when there's
        # something to revert from.
        self._btn_reset_auto.setEnabled(
            has_region and (self._manual_box is not None or self._is_locked)
        )
        # Lock requires a region. With no region, the calibration key
        # would be undefined — refuse to save.
        self._lock_btn.setEnabled(has_region)

        if not has_region:
            self._crop_status_lbl.setText(
                "Set scanning region first — open Mining Signals and define ocr_region."
            )
            self._crop_status_lbl.setStyleSheet(
                f"color: {RED}; font-family: Consolas; font-size: 9pt; "
                f"background: transparent;"
            )
            return

        self._crop_status_lbl.setStyleSheet(
            f"color: {DIM}; font-family: Consolas; font-size: 9pt; "
            f"background: transparent;"
        )
        if self._is_locked and self._manual_box is not None:
            mb = self._manual_box
            self._crop_status_lbl.setText(
                f"LOCKED  x={mb['x']} y={mb['y']} w={mb['w']} h={mb['h']}"
            )
        elif self._manual_box is not None:
            mb = self._manual_box
            self._crop_status_lbl.setText(
                f"MANUAL (unsaved)  x={mb['x']} y={mb['y']} "
                f"w={mb['w']} h={mb['h']}  — click Lock to save"
            )
        else:
            self._crop_status_lbl.setText(
                "AUTO  — using live icon-anchor result"
            )

    def _apply_lock_style(self, locked: bool) -> None:
        if locked:
            self._lock_btn.setText("🔓 Unlock")
            self._lock_btn.setStyleSheet(
                f"QPushButton {{ background: {LOCK_GREEN}; color: white; "
                "font-weight: bold; padding: 4px 10px; border: none; }}"
                "QPushButton:hover { background: #3b9; }"
                "QPushButton:disabled { background: #2a8a; color: #cccccc88; }"
            )
        else:
            self._lock_btn.setText("🔒 Lock")
            self._lock_btn.setStyleSheet(
                f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
                "padding: 4px 10px; border: none; }}"
                "QPushButton:hover { background: #777; }"
                "QPushButton:disabled { background: #333; color: #666; }"
            )

    # ── Button handlers ──────────────────────────────────────────

    def _nudge(self, dx: int, dy: int, dw: int, dh: int) -> None:
        if self._is_locked:
            return
        if self._manual_box is None:
            self._crop_status_lbl.setText(
                "Click ✏ Set first to drop a starting rectangle, then nudge."
            )
            return
        try:
            mods = QApplication.keyboardModifiers()
            if mods & Qt.ShiftModifier:
                dx, dy, dw, dh = dx * 5, dy * 5, dw * 5, dh * 5
        except Exception:
            pass
        box = dict(self._manual_box)
        box["x"] = max(0, int(box["x"]) + dx)
        box["y"] = max(0, int(box["y"]) + dy)
        box["w"] = max(4, int(box["w"]) + dw)
        box["h"] = max(4, int(box["h"]) + dh)
        self._manual_box = box
        self._refresh_crop_selector_state()

    def _on_seed_manual(self) -> None:
        if self._is_locked:
            self._crop_status_lbl.setText(
                "(unlock first to seed a manual crop)"
            )
            return
        # Prefer the most recent auto-anchor result if we have one — it's
        # already a near-correct starting point the user only needs to
        # fine-tune. Otherwise fall back to the placeholder rectangle
        # (mirrors _RowControl._PLACEHOLDER_BOXES["signature"]).
        if self._last_auto_box is not None:
            self._manual_box = dict(self._last_auto_box)
        else:
            self._manual_box = dict(_SIGNATURE_PLACEHOLDER_BOX)
        self._refresh_crop_selector_state()

    def _on_reset_to_auto(self) -> None:
        # When locked, ↻ Auto must also unlock + delete the saved row,
        # otherwise the signal pipeline (which checks calibration.json
        # on every scan) would keep using the persisted box even though
        # the UI says AUTO. Mirrors the unlock half of calibration_dialog
        # _RowControl._on_lock_toggle.
        if self._is_locked and self._region is not None:
            try:
                from ocr.sc_ocr import calibration as _cal
                _cal.remove_row(self._region, "signature")
            except Exception:
                pass
        self._manual_box = None
        self._is_locked = False
        self._apply_lock_style(False)
        self._refresh_crop_selector_state()

    def _on_lock_toggle(self) -> None:
        if self._region is None:
            self._crop_status_lbl.setText(
                "Cannot lock — no ocr_region configured."
            )
            return
        if self._is_locked:
            # Unlock: drop the persisted row but keep the in-memory
            # manual box so the user can keep nudging from where they
            # left off without losing their position.
            try:
                from ocr.sc_ocr import calibration as _cal
                _cal.remove_row(self._region, "signature")
            except Exception:
                pass
            self._is_locked = False
            self._apply_lock_style(False)
            self._refresh_crop_selector_state()
            return
        # Lock path: prefer manual box; if none, fall back to the most
        # recent auto-anchor box; if STILL none, ask sc_ocr.api for its
        # last seen crop (recovery path mirrors _RowControl._recover_box).
        box = self._manual_box
        if box is None and self._last_auto_box is not None:
            box = dict(self._last_auto_box)
        if box is None:
            try:
                from ocr.sc_ocr import api as _api
                recovered = _api.get_last_signal_crop_box()
                if recovered is not None:
                    box = dict(recovered)
            except Exception:
                box = None
        if box is None:
            self._crop_status_lbl.setText(
                "Cannot lock — no crop yet. Wait for a scan or click ✏ Set."
            )
            return
        try:
            from ocr.sc_ocr import calibration as _cal
            _cal.save_row(self._region, "signature", {
                "x": int(box["x"]), "y": int(box["y"]),
                "w": int(box["w"]), "h": int(box["h"]),
            })
        except Exception as e:
            self._crop_status_lbl.setText(f"Save failed: {e}")
            return
        self._manual_box = {
            "x": int(box["x"]), "y": int(box["y"]),
            "w": int(box["w"]), "h": int(box["h"]),
        }
        self._is_locked = True
        self._apply_lock_style(True)
        self._refresh_crop_selector_state()

    def _ensure_imports(self):
        if self._api is None:
            try:
                import ocr.sc_ocr.api as _api
                from ocr.sc_ocr import signal_anchor as _sa
                self._api = _api
                self._anchor = _sa
            except Exception as e:
                self._status_lbl.setText(f"Pipeline import failed: {e}")
                return False
        return True

    def _poll(self):
        # Drain captured log lines into the debug panel BEFORE the
        # move-pause check — log lines accumulate independently of
        # whether the user is dragging. Only touches widgets when
        # Debug Mode is on.
        if self._debug_log_handler is not None:
            self._drain_debug_log_queue()
        # ── UI-thread only: gating, heartbeat, work submission ──
        # Skip while the user is dragging/repositioning the window
        # (see __init__ comment on _move_pause_until). The next
        # tick after the pause window expires will catch up.
        if time.monotonic() < self._move_pause_until:
            return
        # Heartbeat so the OCR pipeline knows we're watching and
        # keeps producing diagnostic dumps. Without this the dumps
        # are gated off and we'd see stale frames. (Kept on UI thread
        # per refactor brief — separate cleanup planned.)
        try:
            from ocr.sc_ocr import debug_overlay as _dbg
            _dbg.viewer_heartbeat()
        except Exception:
            pass
        if self._region is None:
            self._reload_region()
            if self._region is None:
                return
        if not self._ensure_imports():
            return

        # If the previous scan is still running, skip this tick to
        # avoid backlog. Pool is size-1 anyway, but the flag also
        # prevents the queue from growing past one entry.
        if self._scan_in_progress:
            return

        # Snapshot label size on UI thread; pass into worker so the
        # worker never touches widgets.
        target_w = self._image_lbl.width()
        target_h = self._image_lbl.height()

        # Snapshot the crop-selector state. The worker copies these
        # internally — so even if the user clicks a nudge button while
        # the worker is running, the in-flight scan keeps a coherent
        # view; the next submission picks up the updated values.
        worker = _SigScanWorker(
            self._region, self._anchor, self._api, self._signaler,
            target_w, target_h,
            manual_box=self._manual_box,
            locked=self._is_locked,
            debug_mode=self._debug_mode_on,
        )
        self._scan_in_progress = True
        self._pool.start(worker)

    def _on_scan_result(self, result: "_SigScanResult"):
        """Queued slot — runs on UI thread. Applies all widget updates
        from the worker's result payload."""
        # Always clear the in-flight flag first so a slot exception
        # can't wedge the timer permanently.
        self._scan_in_progress = False

        # Capture failure: the worker bails before producing an
        # annotated image. Mirror the previous behaviour: replace the
        # pixmap area with a text placeholder and stop.
        if result.annotated is None:
            self._image_lbl.setText("(capture failed)")
            if result.anchor_error:
                self._status_lbl.setText(result.anchor_error)
            return

        if result.anchor_error:
            self._status_lbl.setText(result.anchor_error)

        # Cache the most recent annotated PIL image. The Copy button
        # uses this to ship the same image the viewer is currently
        # showing onto the clipboard.
        self._last_annotated_pil = result.annotated

        # Pixmap (ImageQt must run on UI thread — it produces a QImage
        # which the QPixmap factory expects to be used from the GUI
        # thread).
        qim = ImageQt(result.annotated)
        self._image_lbl.setPixmap(QPixmap.fromImage(qim))

        # Big result line
        value = result.value
        if value is not None:
            self._result_lbl.setText(f"{value:,}")
            self._result_lbl.setStyleSheet(
                f"color: {ACCENT}; background: transparent;"
            )
        else:
            self._result_lbl.setText("—")
            self._result_lbl.setStyleSheet(
                f"color: {RED}; background: transparent;"
            )

        # Track the most recent auto-anchor box so ✏ Set can seed from
        # it (preferred over the static placeholder) when the user
        # eventually decides to take manual control.
        if result.auto_digit_box is not None:
            x1, y1, x2, y2 = result.auto_digit_box
            self._last_auto_box = {
                "x": int(x1), "y": int(y1),
                "w": int(x2 - x1), "h": int(y2 - y1),
            }

        icon_found = result.icon_found
        # Anchor status — with the manual override the auto-anchor
        # result is informational, not load-bearing. Still show it so
        # the user can tell at a glance whether their box agrees with
        # what the auto-detect would have picked.
        if result.crop_state == "LOCKED":
            anchor_status = "anchor: LOCKED"
        elif icon_found is not None:
            anchor_status = (
                f"anchor: x={icon_found[0]}..{icon_found[2]} "
                f"score={icon_found[4]:.2f}"
            )
        else:
            anchor_status = "anchor: MISS"

        # Crop status — when the user has overridden, show the actual
        # rectangle they chose (W×H) instead of the auto-anchor's
        # x-range, since "their" box is the load-bearing one.
        drawn = result.drawn_box
        if result.crop_state in ("LOCKED", "MANUAL") and drawn is not None:
            x1, y1, x2, y2 = drawn
            crop_status = f"crop: {x2 - x1}×{y2 - y1}"
        elif drawn is not None:
            x1, _y1, x2, _y2 = drawn
            crop_status = f"crop: x={x1}..{x2}"
        else:
            crop_status = "crop: —"
        status_text = (
            f"{anchor_status}  |  {crop_status}  |  {result.dt_ms:.0f} ms"
        )
        # In Debug Mode, append the per-frame detector summary so the
        # user can see at a glance which detectors fired without
        # cross-referencing the box colours.
        if result.debug_status:
            status_text += f"\n[debug] {result.debug_status}"
        self._status_lbl.setText(status_text)

        # Refresh the crop-selector status line in case anything changed
        # (e.g. the auto-anchor produced a box for the first time, which
        # might enable the seed/lock buttons even with no manual box yet).
        self._refresh_crop_selector_state()

        # History
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {value!s:>7}  ({result.dt_ms:.0f} ms)"
        self._history.append(line)
        self._history_lbl.setText("\n".join(self._history))

    # ─────────────────────────────────────────────────────────────
    # Debug Mode: log capture + clipboard payload
    # ─────────────────────────────────────────────────────────────

    def _on_debug_toggled(self, on: bool) -> None:
        """Show/hide the debug panel and attach/detach the queue-based
        log handler on every logger in ``_DEBUG_LOGGERS``. Tearing the
        handler down on toggle-off prevents handler leaks across
        repeated toggles."""
        if on:
            self._debug_mode_on = True
            self._debug_log_queue = queue.Queue()
            self._debug_log_lines = []
            self._debug_log_widget.clear()
            self._debug_log_handler = _QueueLogHandler(self._debug_log_queue)
            # Snapshot each logger's pre-toggle level BEFORE we mutate
            # anything, so toggle-off can restore the user's prior
            # configuration verbatim. Captures even NOTSET (0) so the
            # restore path knows to put it back to NOTSET rather than
            # leave it at DEBUG.
            self._saved_logger_levels = {}
            for name in _DEBUG_LOGGERS:
                lg = logging.getLogger(name)
                self._saved_logger_levels[name] = lg.level
                lg.addHandler(self._debug_log_handler)
                if lg.level == logging.NOTSET or lg.level > logging.DEBUG:
                    lg.setLevel(logging.DEBUG)
            self._sig_flow_reset()       # blank the tree for the new session
            # Pop the signature flow tree out as its OWN window.
            _g = self.frameGeometry()
            self._sig_flow.move(_g.right() + 8, _g.top())
            self._sig_flow.show()
            self._sig_flow.raise_()
            self._debug_panel.setVisible(True)
        else:
            self._debug_mode_on = False
            if self._debug_log_handler is not None:
                for name in _DEBUG_LOGGERS:
                    try:
                        logging.getLogger(name).removeHandler(
                            self._debug_log_handler,
                        )
                    except Exception:
                        pass
                self._debug_log_handler = None
            # Restore each logger to its pre-toggle level. Without
            # this, every logger we lifted to DEBUG above stays at
            # DEBUG forever — production scans pay verbose-logging
            # overhead and other handlers (file, root) see chatter
            # they shouldn't. Iterating ``_saved_logger_levels``
            # rather than ``_DEBUG_LOGGERS`` means a future addition
            # to the constant won't accidentally restore a logger we
            # never touched.
            for name, prior_level in self._saved_logger_levels.items():
                try:
                    logging.getLogger(name).setLevel(prior_level)
                except Exception:
                    pass
            self._saved_logger_levels = {}
            self._debug_log_queue = queue.Queue()
            self._debug_log_lines = []
            self._debug_log_widget.clear()
            self._sig_flow.hide()
            self._debug_panel.setVisible(False)

    def _build_sig_flowchart(self) -> QWidget:
        """Compact, signature-only pipeline tree shown in the Debug panel.
        Stages advance idle → active → done as ``_sig_flow_mark`` parses the
        captured log stream, so the Route-2 scan is legible at a glance."""
        self._sig_nodes: dict[str, QLabel] = {}
        self._sig_order = [k for k, _ in _SIG_STAGES]
        self._sig_active = -1
        fc = QWidget(self, Qt.Window | Qt.WindowStaysOnTopHint)
        fc.setWindowTitle("SC — Signature Pipeline Flow")
        fc.resize(300, 360)
        v = QVBoxLayout(fc)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(1)
        self._sig_flow_caption = QLabel(
            "Signature pipeline — start a scan to watch it run.", self)
        self._sig_flow_caption.setStyleSheet(
            "color:#9ccfff;background:transparent;font-family:Consolas;"
            "font-size:8pt;")
        self._sig_flow_caption.setWordWrap(True)
        v.addWidget(self._sig_flow_caption)
        for i, (key, label) in enumerate(_SIG_STAGES):
            box = QLabel(label, self)
            box.setAlignment(Qt.AlignCenter)
            box.setWordWrap(True)
            box.setStyleSheet(_SIG_IDLE)
            v.addWidget(box)
            self._sig_nodes[key] = box
            if i < len(_SIG_STAGES) - 1:
                ar = QLabel("↓", self)
                ar.setAlignment(Qt.AlignCenter)
                ar.setStyleSheet(
                    "color:#555;background:transparent;font-size:8pt;")
                v.addWidget(ar)
        return fc

    def _sig_flow_reset(self) -> None:
        if not hasattr(self, "_sig_nodes"):
            return
        self._sig_active = -1
        for box in self._sig_nodes.values():
            box.setStyleSheet(_SIG_IDLE)
        self._sig_flow_caption.setText(
            "Signature pipeline — start a scan to watch it run.")

    def _sig_flow_set(self, stage: str) -> None:
        if not hasattr(self, "_sig_nodes") or stage not in self._sig_order:
            return
        idx = self._sig_order.index(stage)
        self._sig_active = idx
        for i, key in enumerate(self._sig_order):
            box = self._sig_nodes[key]
            box.setStyleSheet(
                _SIG_DONE if i < idx
                else _SIG_ACTIVE if i == idx
                else _SIG_IDLE
            )
        label = dict(_SIG_STAGES)[stage]
        clean = label.split(" ", 1)[1] if " " in label else label
        self._sig_flow_caption.setText("Signature → " + clean)

    def _sig_flow_mark(self, line: str) -> None:
        """Advance the signature tree from one captured log line — first
        matching rule wins; ``_timeout`` re-blanks for a fresh search."""
        if not hasattr(self, "_sig_nodes"):
            return
        low = line.lower()
        for substr, stage in _SIG_FLOW_RULES:
            if substr in low:
                if stage == "_timeout":
                    self._sig_flow_reset()
                    self._sig_flow_caption.setText(
                        "Signature → scanning… (panel not found yet)")
                else:
                    self._sig_flow_set(stage)
                return

    def _drain_debug_log_queue(self) -> None:
        if self._debug_log_handler is None:
            return
        appended = 0
        while True:
            try:
                line = self._debug_log_queue.get_nowait()
            except queue.Empty:
                break
            self._debug_log_lines.append(line)
            self._debug_log_widget.appendPlainText(line)
            self._sig_flow_mark(line)        # advance the signature tree
            appended += 1
            if appended > 1000:
                break
        if appended:
            if len(self._debug_log_lines) > _DEBUG_LOG_MAX_LINES:
                drop = len(self._debug_log_lines) - _DEBUG_LOG_MAX_LINES
                self._debug_log_lines = self._debug_log_lines[drop:]
            sb = self._debug_log_widget.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())

    def _on_clear_clicked(self) -> None:
        self._debug_log_lines = []
        self._debug_log_widget.clear()
        try:
            while True:
                self._debug_log_queue.get_nowait()
        except queue.Empty:
            pass

    def _on_copy_clicked(self) -> None:
        """Place captured log text + the current overlay image onto the
        clipboard in a single QMimeData payload."""
        log_text = "\n".join(self._debug_log_lines)
        line_count = len(self._debug_log_lines)

        mime = QMimeData()
        mime.setText(log_text)

        image_attached = False
        try:
            if self._last_annotated_pil is not None:
                qim = QImage(ImageQt(self._last_annotated_pil))
                if not qim.isNull():
                    mime.setImageData(qim)
                    image_attached = True
        except Exception:
            image_attached = False

        try:
            cb = QApplication.clipboard()
            cb.setMimeData(mime)
        except Exception as exc:
            self._debug_status_lbl.setText(f"copy failed: {exc}")
            self._debug_status_timer.start(2500)
            return

        if image_attached:
            self._debug_status_lbl.setText(
                f"Copied {line_count} lines + image to clipboard"
            )
        else:
            self._debug_status_lbl.setText(
                f"Copied {line_count} lines to clipboard (no image yet)"
            )
        self._debug_status_timer.start(2500)

    def closeEvent(self, event):
        try:
            self._sig_flow.close()       # close the pop-out flow window too
        except Exception:
            pass
        # Tear down log handlers so closing the window doesn't leak
        # them onto the global logger registry. Also restore each
        # logger's pre-toggle level — same rationale as the toggle-
        # off branch above (otherwise closing the window with Debug
        # Mode still on leaves DEBUG-level chatter active for the
        # rest of the host process's lifetime).
        try:
            if self._debug_log_handler is not None:
                for name in _DEBUG_LOGGERS:
                    try:
                        logging.getLogger(name).removeHandler(
                            self._debug_log_handler,
                        )
                    except Exception:
                        pass
                self._debug_log_handler = None
            for name, prior_level in self._saved_logger_levels.items():
                try:
                    logging.getLogger(name).setLevel(prior_level)
                except Exception:
                    pass
            self._saved_logger_levels = {}
        except Exception:
            pass
        super().closeEvent(event)


def main():
    app = QApplication.instance() or QApplication(sys.argv)
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor(BG))
    palette.setColor(QPalette.WindowText, QColor(FG))
    palette.setColor(QPalette.Base, QColor("#2a2a2a"))
    palette.setColor(QPalette.Text, QColor(FG))
    palette.setColor(QPalette.Button, QColor("#444"))
    palette.setColor(QPalette.ButtonText, QColor(FG))
    app.setPalette(palette)

    win = SignatureFinderViewer()

    # Cross-process single-instance enforcement. If the popout opened
    # from inside the calibration dialog (or another standalone copy)
    # is already running, hand control to that holder and exit.
    # Note: package is named ``mining_shared`` to avoid collision with
    # the SC_Toolbox-wide ``shared/`` package one directory up.
    from mining_shared.single_instance import SingleInstance
    guard = SingleInstance("signature_finder", win)
    if not guard.acquire():
        # Holder already poked. Don't show our own window.
        return 0
    # Pin guard onto the window so it lives as long as the window.
    win._single_instance = guard

    win.show()
    win.raise_()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
