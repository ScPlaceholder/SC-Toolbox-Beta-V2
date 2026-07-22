"""Panel Finder popout window — embedded version of the standalone
live_panel_finder_viewer.py script.

Same job: poll ``debug_panel_overlay.png`` every 400 ms and display it
with proper centering + aspect-fit. Lives as a separate top-level
window opened from the calibration dialog so the user can:

  * Drag it freely around the screen
  * Resize from very small (~ 200 x 200) up to fullscreen
  * Close it without affecting the calibration dialog
  * Keep it open as a persistent reference while calibrating

The image is ALWAYS centered both horizontally and vertically inside
the viewer using a stretch-flanked QLabel layout, regardless of
window size or image aspect ratio.
"""
from __future__ import annotations

import logging
import queue
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, QTimer, QMimeData
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QSizePolicy, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# Default to the same overlay path the OCR pipeline writes to
_THIS_DIR = Path(__file__).resolve().parent
TOOL_DIR = _THIS_DIR.parent
DEFAULT_OVERLAY_PATH = TOOL_DIR / "debug_panel_overlay.png"

POLL_MS = 400

# Extra window height added when Debug Mode is switched ON. The debug
# panel otherwise just steals half of a fixed-size window from the
# overlay image — an easy change to miss, and on a small window the
# log widget gets squeezed below its 120 px minimum. Growing the
# window makes the toggle an obvious, visible action and guarantees
# the panel room. Removed again on OFF.
_DEBUG_PANEL_EXTRA_H = 240   # log panel only; the flowchart is now its own window

# Loggers captured by the panel-finder Debug Mode panel. Keep this list
# in sync with the equivalent set in scripts/signature_finder_viewer.py
# (both viewers ship their own list because the two GUIs cover different
# pipelines).
_DEBUG_LOGGERS = (
    # Route 1 — HUD / composition panel
    "ocr.sc_ocr.api",
    "ocr.sc_ocr.scan_results_match",
    "ocr.sc_ocr.label_match",
    "ocr.sc_ocr.hud_panel_tracker",
    "ocr.sc_ocr.hud_panel_stabilizer",
    "ocr.onnx_hud_reader",
    "hud_tracker.anchors.chrome_lines",
    "hud_tracker.anchors.hud_color_finder",
    "hud_tracker.anchors.mineral_name_color",
    # Route 2 — signature value (added so the live flowchart can light up
    # the signature pipeline's stages too, not just the HUD route)
    "ocr.sc_ocr.signal_anchor",
    "ocr.sc_ocr.signal_solve",
    "ocr.sc_ocr.signal_record",
    "hud_tracker.anchors.icon_voter",
    "hud_tracker.anchors.icon_rgb_ncc",
    "hud_tracker.anchors.icon_geometry",
    "hud_tracker.anchors.comma_finder",
)

# Cap the captured-log buffer at this many lines. Older lines drop off
# the top so we never let a leak-prone diagnostic pile up unbounded.
_DEBUG_LOG_MAX_LINES = 500

# ── Live pipeline flowchart ──────────────────────────────────────────
# Ordered stages per route: (stage-key, plain-language label). Shown as a
# column of boxes that light up live as the scan runs, so even a non-expert
# can watch what the scanner is doing on both routes.
_R1_STAGES = [
    ("capture", "① capture screen"),
    ("panel", "② find the panel"),
    ("rows", "③ find mineral rows"),
    ("values", "④ read MASS / RES / INST"),
    ("mineral", "⑤ read mineral name"),
    ("lock", "⑥ lock + confirm → composition"),
]
_R2_STAGES = [
    ("capture", "① capture screen"),
    ("pill", "② find pill / icon"),
    ("crop", "③ locate the number"),
    ("comma", "④ find comma + extend"),
    ("digits", "⑤ read digits (CNN)"),
    ("crnn", "⑥ CRNN cross-check"),
    ("flap", "⑦ consistency → value"),
]
# First matching rule wins: (substring searched in the lowercased log line,
# route, stage-key). Logger-name substrings are strong route signals; message
# keywords catch the shared "ocr.sc_ocr.api" lines that serve both routes.
_FLOW_RULES = [
    ("hud_color_finder", "r1", "panel"),
    ("scan_results_match", "r1", "panel"),
    ("mineral_name_color", "r1", "rows"),
    ("label_match", "r1", "values"),
    ("onnx_hud_reader", "r1", "values"),
    ("icon_voter", "r2", "pill"),
    ("icon_rgb_ncc", "r2", "pill"),
    ("icon_geometry", "r2", "pill"),
    ("signal_solve", "r2", "pill"),
    ("comma_finder", "r2", "comma"),
    ("signal_anchor", "r2", "crop"),
    ("signal_record", "r2", "flap"),
    ("scanning timeout", "r2", "_timeout"),
    ("all locked", "r1", "lock"),
    ("world_model", "r2", "pill"),
    ("localize_icon", "r2", "pill"),
    ("crnn", "r2", "crnn"),
    ("segment", "r2", "digits"),
    ("glyph", "r2", "digits"),
    ("comma", "r2", "comma"),
    ("pill", "r2", "pill"),
    ("flap", "r2", "flap"),
    ("lock", "r1", "lock"),
    ("mineral", "r1", "rows"),
    ("resistance", "r1", "values"),
    ("instability", "r1", "values"),
]
_STAGE_IDLE = ("QLabel{background:#222;color:#7a7a7a;border:1px solid #363636;"
               "border-radius:5px;padding:2px 5px;font-family:Consolas;"
               "font-size:8pt;}")
_STAGE_ACTIVE = ("QLabel{background:#16385c;color:#e6f3ff;border:1px solid "
                 "#5ab0ff;border-radius:5px;padding:2px 5px;font-family:Consolas;"
                 "font-size:8pt;font-weight:bold;}")
_STAGE_DONE = ("QLabel{background:#173318;color:#bfe9bf;border:1px solid "
               "#3a7a3a;border-radius:5px;padding:2px 5px;font-family:Consolas;"
               "font-size:8pt;}")


class _QueueLogHandler(logging.Handler):
    """Thread-safe ``logging.Handler`` that pushes each formatted record
    into a ``queue.Queue``. Loggers may emit from worker threads; the
    GUI's poll timer drains the queue on the main thread, so the only
    cross-thread contract is the queue itself (which is already
    thread-safe).
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
            # Never let a logging path raise — that would propagate back
            # into whichever worker emitted the record.
            pass


class PanelFinderPopout(QWidget):
    """Standalone window showing the live panel finder overlay."""

    def __init__(self, overlay_path: Optional[Path] = None, parent=None):
        # Top-level window, NOT a child of parent (so it doesn't get
        # locked to the calibration dialog's z-order). Keep parent
        # only for clean shutdown.
        super().__init__(parent, Qt.Window)
        self.setWindowTitle("SC-OCR Panel Finder")
        self._overlay_path = overlay_path or DEFAULT_OVERLAY_PATH
        self._cached_pil: Optional[Image.Image] = None
        self._last_mtime = 0.0

        # Start SMALL — user requested. They can resize up.
        self.resize(360, 360)
        self.setMinimumSize(180, 180)

        # ── Layout ──
        v = QVBoxLayout(self)
        v.setContentsMargins(4, 4, 4, 4)
        v.setSpacing(4)

        # Compact header strip
        header = QWidget()
        hl = QHBoxLayout(header)
        hl.setContentsMargins(4, 2, 4, 2)
        hl.setSpacing(6)
        self._meta = QLabel("waiting…")
        self._meta.setStyleSheet(
            "color: #888; font-family: Consolas; font-size: 8pt;"
        )
        hl.addWidget(self._meta, 1)
        # Debug Mode checkbox — toggles the log-capture panel below the
        # image. Default OFF so existing users see no behavior change.
        self._debug_checkbox = QCheckBox("Debug Mode")
        self._debug_checkbox.setStyleSheet(
            "QCheckBox { color: #ccc; font-family: Consolas; font-size: 9pt; }"
        )
        self._debug_checkbox.toggled.connect(self._on_debug_toggled)
        hl.addWidget(self._debug_checkbox)
        refresh_btn = QPushButton("⟳")
        refresh_btn.setFixedSize(22, 22)
        refresh_btn.setToolTip("Force refresh now")
        refresh_btn.clicked.connect(self._tick_force)
        refresh_btn.setStyleSheet(
            "QPushButton { background: #333; color: white; border: none; "
            "font-size: 11pt; }"
            "QPushButton:hover { background: #555; }"
        )
        hl.addWidget(refresh_btn)
        v.addWidget(header)

        # ── Side-by-side image area ──
        # Left: frozen reference snapshot (only visible when a freeze
        # is active). Right: live OCR overlay. Two stretch-flanked
        # columns inside a single dark-background wrapper.
        wrap = QWidget()
        wrap.setStyleSheet("background: #111;")
        wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        wrap_h_root = QHBoxLayout(wrap)
        wrap_h_root.setContentsMargins(0, 0, 0, 0)
        wrap_h_root.setSpacing(0)

        # ── Frozen pane (left) ──
        # Hidden when no frozen reference exists. Shows the snapshot
        # image plus a small status strip with the locked values and
        # age. Stays visible until the OCR pipeline clears its frozen
        # reference (icon-absent-3s).
        frozen_col = QWidget()
        frozen_col.setStyleSheet("background: #0c1a0c;")  # subtle green tint
        frozen_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        frozen_v = QVBoxLayout(frozen_col)
        frozen_v.setContentsMargins(2, 2, 2, 2)
        frozen_v.setSpacing(2)

        frozen_header = QLabel("FROZEN")
        frozen_header.setAlignment(Qt.AlignCenter)
        frozen_header.setStyleSheet(
            "color: #6f6; background: transparent; "
            "font-family: Consolas; font-size: 8pt; font-weight: bold;"
        )
        frozen_v.addWidget(frozen_header)

        # Stretch-flanked image — same centering technique as the
        # live pane below.
        frozen_img_wrap = QWidget()
        frozen_img_wrap.setSizePolicy(
            QSizePolicy.Expanding, QSizePolicy.Expanding,
        )
        frozen_img_wrap_v = QVBoxLayout(frozen_img_wrap)
        frozen_img_wrap_v.setContentsMargins(0, 0, 0, 0)
        frozen_img_wrap_v.setSpacing(0)
        frozen_img_wrap_v.addStretch(1)
        frozen_img_wrap_h = QHBoxLayout()
        frozen_img_wrap_h.setContentsMargins(0, 0, 0, 0)
        frozen_img_wrap_h.setSpacing(0)
        frozen_img_wrap_h.addStretch(1)
        self._frozen_img = QLabel("(no freeze)")
        self._frozen_img.setAlignment(Qt.AlignCenter)
        self._frozen_img.setStyleSheet(
            "background: transparent; color: #444; "
            "font-family: Consolas; font-size: 8pt;"
        )
        self._frozen_img.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        frozen_img_wrap_h.addWidget(self._frozen_img)
        frozen_img_wrap_h.addStretch(1)
        frozen_img_wrap_v.addLayout(frozen_img_wrap_h)
        frozen_img_wrap_v.addStretch(1)
        frozen_v.addWidget(frozen_img_wrap, 1)

        # Status strip — mass / resistance / instability / mineral
        # + age in seconds. Updated by _tick.
        self._frozen_status = QLabel("")
        self._frozen_status.setAlignment(Qt.AlignCenter)
        self._frozen_status.setWordWrap(True)
        self._frozen_status.setStyleSheet(
            "color: #afa; background: transparent; "
            "font-family: Consolas; font-size: 8pt;"
        )
        frozen_v.addWidget(self._frozen_status)

        # Wire the column into the root layout. Start it hidden;
        # _tick will show it when a freeze becomes active.
        self._frozen_col = frozen_col
        self._frozen_col.setVisible(False)
        wrap_h_root.addWidget(frozen_col, 1)

        # ── Live pane (right) ──
        # Existing layout, just wrapped in its own column so both panes
        # get equal width when frozen pane is visible.
        live_col = QWidget()
        live_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        live_v = QVBoxLayout(live_col)
        live_v.setContentsMargins(0, 0, 0, 0)
        live_v.setSpacing(0)
        live_v.addStretch(1)
        wrap_h = QHBoxLayout()
        wrap_h.setContentsMargins(0, 0, 0, 0)
        wrap_h.setSpacing(0)
        wrap_h.addStretch(1)
        self._img = QLabel("(no overlay yet)")
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setStyleSheet(
            "background: transparent; color: #555; "
            "font-family: Consolas; font-size: 9pt;"
        )
        # Critical: SizePolicy must NOT expand, so the surrounding
        # stretches actually push the label to the center.
        self._img.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        wrap_h.addWidget(self._img)
        wrap_h.addStretch(1)
        live_v.addLayout(wrap_h)
        live_v.addStretch(1)
        wrap_h_root.addWidget(live_col, 1)

        v.addWidget(wrap, 1)

        # Cached frozen state for change detection. Avoids re-rendering
        # the (potentially large) frozen image every 400 ms when
        # nothing has changed about the freeze.
        self._cached_frozen_id: Optional[int] = None
        self._cached_frozen_pix: Optional[QPixmap] = None

        # ── Debug Mode panel (hidden until checkbox toggled on) ──
        # State: a queue.Queue between the (possibly worker-thread) log
        # handler and the main-thread poll-timer drain. The handler is
        # only attached to loggers while Debug Mode is on; toggling off
        # tears it down completely so we never leak handlers across
        # toggles.
        self._debug_log_queue: queue.Queue = queue.Queue()
        self._debug_log_handler: Optional[_QueueLogHandler] = None
        self._debug_log_lines: list[str] = []
        # Saved per-logger levels captured on toggle-ON so toggle-OFF
        # / closeEvent can restore the prior configuration. Without
        # this, ``_on_debug_toggled``'s ``setLevel(DEBUG)`` line
        # permanently mutates the shared logger registry — every
        # scan after the first toggle pays DEBUG-level overhead and
        # any other attached handler (file logger, console root)
        # sees verbose chatter forever.
        self._saved_logger_levels: dict[str, int] = {}
        # Window height captured the moment Debug Mode is switched ON
        # so toggling it OFF can restore the prior size exactly.
        self._pre_debug_height: Optional[int] = None

        self._debug_panel = QWidget()
        dp_v = QVBoxLayout(self._debug_panel)
        dp_v.setContentsMargins(4, 2, 4, 2)
        dp_v.setSpacing(3)

        # Buttons row above the log (Copy + Clear + status notification)
        dp_btns = QHBoxLayout()
        dp_btns.setContentsMargins(0, 0, 0, 0)
        dp_btns.setSpacing(6)
        self._copy_button = QPushButton("Copy log + image")
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

        self._clear_button = QPushButton("Clear")
        self._clear_button.setToolTip("Empty the captured log buffer.")
        self._clear_button.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; padding: 3px 10px; "
            "border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #666; }"
        )
        self._clear_button.clicked.connect(self._on_clear_clicked)
        dp_btns.addWidget(self._clear_button)

        # Record N seconds of the live overlay frames (exactly what this
        # window shows) into panel_finder_recording\popout_<ts>\ so the
        # captured finder behavior can be sent for analysis / replayed.
        self._record_button = QPushButton("● Record 20s")
        self._record_button.setToolTip(
            "Capture ~20 s of the live debug-overlay frames into "
            "panel_finder_recording\\popout_<timestamp>\\ (the folder opens "
            "when done). Click again to stop early."
        )
        self._record_button.setStyleSheet(
            "QPushButton { background: #663333; color: #ffcccc; "
            "padding: 3px 10px; border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #aa4444; }"
        )
        self._record_button.clicked.connect(self._on_record_clicked)
        dp_btns.addWidget(self._record_button)
        self._rec_active = False
        self._rec_secs = 20.0

        # Inline status notification (transient — cleared after ~2.5 s)
        self._debug_status_lbl = QLabel("")
        self._debug_status_lbl.setStyleSheet(
            "color: #5fff9c; font-family: Consolas; font-size: 9pt;"
        )
        dp_btns.addWidget(self._debug_status_lbl, 1)
        dp_v.addLayout(dp_btns)

        # Live two-route pipeline flowchart — its OWN floating window (not
        # crammed into the debug log panel). Shown/hidden with Debug Mode.
        self._flowchart = self._build_flowchart()

        self._debug_log_widget = QPlainTextEdit()
        self._debug_log_widget.setReadOnly(True)
        self._debug_log_widget.setMaximumBlockCount(_DEBUG_LOG_MAX_LINES)
        self._debug_log_widget.setStyleSheet(
            "QPlainTextEdit { background: #181818; color: #cccccc; "
            "border: 1px solid #333; font-family: Consolas; font-size: 9pt; }"
        )
        self._debug_log_widget.setMinimumHeight(120)
        dp_v.addWidget(self._debug_log_widget)

        self._debug_panel.setVisible(False)
        v.addWidget(self._debug_panel, 1)

        # Timer used to clear the transient "copied" status notification
        self._debug_status_timer = QTimer(self)
        self._debug_status_timer.setSingleShot(True)
        self._debug_status_timer.timeout.connect(
            lambda: self._debug_status_lbl.setText("")
        )

        # Pause-on-move: see SignatureFinderViewer for full rationale.
        # During a title-bar drag, Qt's QMoveEvent and our polling
        # tick queue on the same main thread; the tick blocks the
        # drag until it finishes. Skipping ticks while the window is
        # actively moving makes the drag feel instant.
        self._move_pause_until = 0.0
        self._move_pause_seconds = 0.35
        import time as _time
        self._time = _time

        # Polling timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

        self._tick()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._move_pause_until = self._time.monotonic() + self._move_pause_seconds

    # ──────────────────────────────────────────
    # Polling + render
    # ──────────────────────────────────────────

    def _tick_force(self) -> None:
        self._last_mtime = 0.0
        self._tick()

    def _tick(self) -> None:
        # Drain captured log lines into the debug panel BEFORE the
        # move-pause check — log lines accumulate in the queue
        # regardless of whether the user is dragging, and the cap on
        # _DEBUG_LOG_MAX_LINES means we never grow unbounded. We only
        # touch widgets when Debug Mode is on.
        if self._debug_log_handler is not None:
            self._drain_debug_log_queue()
        # Update the frozen-reference pane on every tick (regardless
        # of the move-pause check below — we want the frozen pane to
        # appear/disappear instantly when the OCR pipeline freezes or
        # clears, not delayed by dragging).
        self._update_frozen_pane()
        # Skip while the window is being dragged.
        if self._time.monotonic() < self._move_pause_until:
            return
        # Heartbeat so the OCR pipeline keeps writing
        # debug_panel_overlay.png. If no viewer touches this file
        # within HEARTBEAT_TTL_SEC the pipeline skips the write
        # entirely.
        try:
            from ocr.sc_ocr import debug_overlay as _dbg
            _dbg.viewer_heartbeat_tag("overlay")
        except Exception:
            pass
        # Stop an in-progress recording on time even if no new frame
        # has arrived (pipeline stalled) — _tick runs every 400 ms.
        if self._rec_active:
            import time as _t
            if _t.monotonic() >= self._rec_end:
                self._finish_recording()

        if not self._overlay_path.is_file():
            self._meta.setText(f"(missing: {self._overlay_path.name})")
            self._img.setText("Waiting for OCR pipeline…")
            return
        mtime = self._overlay_path.stat().st_mtime
        size = self._overlay_path.stat().st_size
        ts = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        delta = max(0, int(datetime.now().timestamp() - mtime))
        self._meta.setText(f"{ts}  ({delta}s ago)  {size:,} B")

        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime
        try:
            pil = Image.open(self._overlay_path).convert("RGB")
        except Exception as exc:
            self._img.setText(f"open failed: {exc}")
            return
        self._cached_pil = pil
        self._render()

        # Recording: save this freshly-arrived overlay frame verbatim.
        if self._rec_active:
            self._record_overlay_frame()

    def _render(self) -> None:
        pil = self._cached_pil
        if pil is None:
            return
        # Use the wrapper's available area, NOT the QLabel's (the
        # QLabel sizes to its content under our layout).
        wrap = self._img.parent()
        avail_w = max(40, wrap.width() - 8) if wrap else 360
        avail_h = max(40, wrap.height() - 8) if wrap else 360
        ratio = min(avail_w / pil.width, avail_h / pil.height)
        new_w = max(20, int(pil.width * ratio))
        new_h = max(20, int(pil.height * ratio))
        if new_w == pil.width and new_h == pil.height:
            scaled = pil
        else:
            scaled = pil.resize((new_w, new_h), Image.LANCZOS)
        self._img.setPixmap(QPixmap.fromImage(ImageQt(scaled)))
        # Force the QLabel to size to the new pixmap so the
        # surrounding stretch flanks center it correctly.
        self._img.setFixedSize(new_w, new_h)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render()

    # ──────────────────────────────────────────
    # Frozen-reference pane
    # ──────────────────────────────────────────

    def _update_frozen_pane(self) -> None:
        """Show or hide the left-side frozen reference pane based on
        whether the OCR pipeline currently has a freeze active.

        Runs every tick (400 ms). Cheap when nothing changes — uses
        ``id()`` of the cached image to detect new freezes without
        re-rendering.
        """
        try:
            from ocr.sc_ocr.frozen_panel import get_active_frozen
            frozen = get_active_frozen()
        except Exception:
            frozen = None

        if frozen is None or not frozen.is_frozen:
            # Hide the pane if there's no active freeze.
            if self._frozen_col.isVisible():
                self._frozen_col.setVisible(False)
                self._cached_frozen_id = None
                self._cached_frozen_pix = None
            return

        # Pane visible.
        if not self._frozen_col.isVisible():
            self._frozen_col.setVisible(True)

        pil_img = frozen.panel_image
        if pil_img is None:
            return

        # Render the frozen image only when the underlying object has
        # changed (cheap identity check). The image itself is immutable
        # under our usage so id() is a reliable change signal.
        img_id = id(pil_img)
        if img_id != self._cached_frozen_id:
            try:
                # Resize to fit the available width while preserving
                # aspect ratio. Cap at the column's current width
                # (post-layout) to avoid horizontal overflow.
                target_w = max(120, min(640, self._frozen_col.width() - 8))
                w, h = pil_img.size
                if w > 0:
                    scale = target_w / float(w)
                    new_w = int(w * scale)
                    new_h = int(h * scale)
                    rendered = pil_img.resize(
                        (max(1, new_w), max(1, new_h)),
                        Image.LANCZOS,
                    )
                else:
                    rendered = pil_img
                qim = ImageQt(rendered.convert("RGBA"))
                pix = QPixmap.fromImage(QImage(qim))
                self._frozen_img.setPixmap(pix)
                self._frozen_img.setFixedSize(pix.size())
                self._cached_frozen_pix = pix
                self._cached_frozen_id = img_id
            except Exception as exc:
                log.debug("frozen-pane render failed: %s", exc)
                self._frozen_img.setText("(render error)")

        # Status strip always updates (age changes every tick).
        vals = frozen.values
        try:
            mass = vals.get("mass")
            resist = vals.get("resistance")
            instab = vals.get("instability")
            mineral = vals.get("mineral_name")
            age_s = frozen.age_seconds()
            since_title = frozen.time_since_title_seen()
            self._frozen_status.setText(
                f"mass={mass}  resist={resist}%  instab={instab}\n"
                f"mineral={mineral or '?'}\n"
                f"age={age_s:.1f}s  title_seen={since_title:.1f}s ago"
            )
        except Exception as exc:
            log.debug("frozen-pane status update failed: %s", exc)
            self._frozen_status.setText("(status error)")

    # ──────────────────────────────────────────
    # Debug Mode: log capture + clipboard payload
    # ──────────────────────────────────────────

    def _on_debug_toggled(self, on: bool) -> None:
        """Show/hide the debug panel. When toggling ON we attach a
        single ``_QueueLogHandler`` to every logger in
        ``_DEBUG_LOGGERS``; when toggling OFF we remove the handler
        from each one so we don't leak handlers across toggles. The
        panel widget itself is kept alive (lazy hide) so subsequent
        toggles are instantaneous."""
        if on:
            # Wire up a fresh queue + handler each time. Re-toggling
            # always starts with an empty buffer.
            self._debug_log_queue = queue.Queue()
            self._debug_log_lines = []
            self._debug_log_widget.clear()
            self._debug_log_handler = _QueueLogHandler(self._debug_log_queue)
            # Snapshot each logger's pre-toggle level BEFORE we mutate
            # anything, so the toggle-off branch can restore the
            # prior configuration verbatim. Captures even NOTSET (0).
            self._saved_logger_levels = {}
            for name in _DEBUG_LOGGERS:
                lg = logging.getLogger(name)
                self._saved_logger_levels[name] = lg.level
                lg.addHandler(self._debug_log_handler)
                # Make sure DEBUG-level records actually reach the
                # handler — many of these loggers default to WARNING.
                # We don't lower the propagation level on the root
                # logger though; only the per-source loggers we care
                # about.
                if lg.level == logging.NOTSET or lg.level > logging.DEBUG:
                    lg.setLevel(logging.DEBUG)
            self._debug_panel.setVisible(True)
            # Seed the log view with a confirmation line. Without this
            # the panel stays blank until the next scan emits a record
            # — which, combined with the panel quietly appearing in a
            # fixed-size window, reads as "the toggle did nothing".
            seed = (
                f"[Debug Mode ON — capturing {len(_DEBUG_LOGGERS)} "
                f"pipeline loggers at DEBUG. Pipeline log lines appear "
                f"here while a scan is running.]"
            )
            self._debug_log_lines.append(seed)
            self._debug_log_widget.appendPlainText(seed)
            self._flow_reset()      # blank the flowchart for the new session
            # Pop the flow tree out as its OWN window, just right of this one.
            _g = self.frameGeometry()
            self._flowchart.move(_g.right() + 8, _g.top())
            self._flowchart.show()
            self._flowchart.raise_()
            # Grow the window so switching Debug Mode on is an obvious,
            # visible change and the log widget gets its full room.
            self._grow_window_for_debug()
        else:
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
            # Drop the captured buffer + queue so toggling off truly
            # releases memory.
            self._debug_log_queue = queue.Queue()
            self._debug_log_lines = []
            self._debug_log_widget.clear()
            self._flowchart.hide()
            self._debug_panel.setVisible(False)
            self._restore_window_after_debug()

    def _grow_window_for_debug(self) -> None:
        """Add height for the Debug Mode panel so toggling it on is an
        obvious, visible change. Skipped when maximised / fullscreen
        (the panel just shares the existing space then). Clamped to the
        screen's available height."""
        if self.isMaximized() or self.isFullScreen():
            self._pre_debug_height = None
            return
        self._pre_debug_height = self.height()
        target_h = self.height() + _DEBUG_PANEL_EXTRA_H
        try:
            scr = self.screen()
            if scr is not None:
                target_h = min(target_h, scr.availableGeometry().height())
        except Exception:
            pass
        self.resize(self.width(), target_h)

    def _restore_window_after_debug(self) -> None:
        """Shrink the window back to its pre-Debug-Mode height."""
        if self.isMaximized() or self.isFullScreen():
            self._pre_debug_height = None
            return
        prior = self._pre_debug_height
        if prior is not None:
            self.resize(self.width(), prior)
        self._pre_debug_height = None

    def _drain_debug_log_queue(self) -> None:
        """Move every queued log line into the panel widget. Runs on
        the main thread from ``_tick``."""
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
            self._flow_mark(line)        # advance the live flowchart
            appended += 1
            # Defensive: if the queue is being flooded by a runaway
            # logger we don't want to block the GUI for too long. Cap
            # per-tick at a generous batch.
            if appended > 1000:
                break
        if appended:
            # Keep the in-memory list bounded too (the QPlainTextEdit's
            # ``maximumBlockCount`` already trims the visible widget).
            if len(self._debug_log_lines) > _DEBUG_LOG_MAX_LINES:
                drop = len(self._debug_log_lines) - _DEBUG_LOG_MAX_LINES
                self._debug_log_lines = self._debug_log_lines[drop:]
            # Scroll to bottom so newest lines stay visible.
            sb = self._debug_log_widget.verticalScrollBar()
            if sb is not None:
                sb.setValue(sb.maximum())

    def _on_record_clicked(self) -> None:
        """Start (or stop) a ~20 s capture of the live overlay frames."""
        import time as _t
        from datetime import datetime as _dt
        if self._rec_active:
            self._finish_recording()
            return
        try:
            stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
            self._rec_dir = TOOL_DIR / "panel_finder_recording" / (
                "popout_" + stamp)
            self._rec_dir.mkdir(parents=True, exist_ok=True)
            self._rec_active = True
            self._rec_count = 0
            self._rec_end = _t.monotonic() + self._rec_secs
            self._record_button.setText("● Recording…")
            self._debug_status_lbl.setText(
                "Recording %ds of overlay frames…" % int(self._rec_secs))
        except Exception as exc:
            self._rec_active = False
            self._debug_status_lbl.setText("record start failed: %s" % exc)

    def _record_overlay_frame(self) -> None:
        """Save the just-loaded overlay frame verbatim into the rec dir."""
        import time as _t
        import shutil
        try:
            self._rec_count += 1
            dest = self._rec_dir / ("frame_%03d.png" % self._rec_count)
            shutil.copyfile(str(self._overlay_path), str(dest))
            remaining = int(max(0, self._rec_end - _t.monotonic()))
            self._record_button.setText("● Recording %ds…" % remaining)
        except Exception:
            pass

    def _finish_recording(self) -> None:
        """Stop recording, report the folder, and open it."""
        self._rec_active = False
        self._record_button.setText("● Record 20s")
        try:
            n = getattr(self, "_rec_count", 0)
            d = str(getattr(self, "_rec_dir", ""))
            self._debug_status_lbl.setText(
                "Saved %d frames → %s" % (n, d))
            import os
            if d and os.path.isdir(d):
                try:
                    os.startfile(d)  # noqa: windows-only — opens the folder
                except Exception:
                    pass
        except Exception:
            pass

    def _on_clear_clicked(self) -> None:
        self._debug_log_lines = []
        self._debug_log_widget.clear()
        # Drain anything that piled up between user clicking Clear and
        # this slot running.
        try:
            while True:
                self._debug_log_queue.get_nowait()
        except queue.Empty:
            pass

    def _on_copy_clicked(self) -> None:
        """Place captured log text + the current overlay image onto the
        clipboard. Most apps paste either text OR image depending on
        what they support; rich-content destinations (Word, email)
        get both at once."""
        log_text = "\n".join(self._debug_log_lines)
        line_count = len(self._debug_log_lines)

        mime = QMimeData()
        mime.setText(log_text)

        image_attached = False
        try:
            if self._cached_pil is not None:
                qim = QImage(ImageQt(self._cached_pil))
                if not qim.isNull():
                    mime.setImageData(qim)
                    image_attached = True
            elif self._overlay_path.is_file():
                qim = QImage(str(self._overlay_path))
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

    # ──────────────────────────────────────────
    # Live pipeline flowchart
    # ──────────────────────────────────────────

    def _build_flowchart(self) -> QWidget:
        """Two-column live flowchart of both scan routes + a plain-text
        log-copy button. Stage boxes are stored in ``self._flow_nodes`` so
        ``_flow_mark`` can light them up as the captured logs stream in."""
        self._flow_nodes = {"r1": {}, "r2": {}}
        self._flow_active = {"r1": -1, "r2": -1}
        self._flow_order = {"r1": [k for k, _ in _R1_STAGES],
                            "r2": [k for k, _ in _R2_STAGES]}
        fc = QWidget(self, Qt.Window | Qt.WindowStaysOnTopHint)
        fc.setWindowTitle("SC — Scan Pipeline Flow (both routes)")
        fc.setStyleSheet("background:#141414;")
        fc.resize(440, 400)
        outer = QVBoxLayout(fc)
        outer.setContentsMargins(4, 3, 4, 3)
        outer.setSpacing(3)
        top = QHBoxLayout()
        top.setSpacing(6)
        self._flow_caption = QLabel(
            "Pipeline idle — start a scan to watch both routes run.")
        self._flow_caption.setWordWrap(True)
        self._flow_caption.setStyleSheet(
            "color:#9ccfff; background:transparent; font-family:Consolas; "
            "font-size:8pt;")
        top.addWidget(self._flow_caption, 1)
        self._copy_log_btn = QPushButton("Copy debug log (text)")
        self._copy_log_btn.setToolTip(
            "Copy the captured pipeline log as plain text (no image), so it "
            "can be saved as a text file or pasted into chat/notes.")
        self._copy_log_btn.setStyleSheet(
            "QPushButton{background:#3a5a3a;color:#d6f5d6;padding:3px 10px;"
            "border:none;font-size:9pt;}"
            "QPushButton:hover{background:#4c774c;}")
        self._copy_log_btn.clicked.connect(self._on_copy_log_text_clicked)
        top.addWidget(self._copy_log_btn)
        outer.addLayout(top)
        cols = QHBoxLayout()
        cols.setSpacing(10)
        cols.addWidget(
            self._build_route_column("r1", "ROUTE 1 · HUD panel", _R1_STAGES), 1)
        cols.addWidget(
            self._build_route_column(
                "r2", "ROUTE 2 · signature value", _R2_STAGES), 1)
        outer.addLayout(cols)
        return fc

    def _build_route_column(self, route, title, stages) -> QWidget:
        col = QWidget()
        cv = QVBoxLayout(col)
        cv.setContentsMargins(2, 2, 2, 2)
        cv.setSpacing(1)
        hdr = QLabel(title)
        hdr.setAlignment(Qt.AlignCenter)
        hdr.setStyleSheet(
            "color:#dddddd; background:transparent; font-family:Consolas; "
            "font-size:8pt; font-weight:bold;")
        cv.addWidget(hdr)
        for i, (key, label) in enumerate(stages):
            box = QLabel(label)
            box.setAlignment(Qt.AlignCenter)
            box.setWordWrap(True)
            box.setStyleSheet(_STAGE_IDLE)
            cv.addWidget(box)
            self._flow_nodes[route][key] = box
            if i < len(stages) - 1:
                ar = QLabel("↓")
                ar.setAlignment(Qt.AlignCenter)
                ar.setStyleSheet(
                    "color:#555; background:transparent; font-size:8pt;")
                cv.addWidget(ar)
        cv.addStretch(1)
        return col

    def _flow_reset(self) -> None:
        if not hasattr(self, "_flow_nodes"):
            return
        for route in ("r1", "r2"):
            self._flow_active[route] = -1
            for box in self._flow_nodes[route].values():
                box.setStyleSheet(_STAGE_IDLE)
        self._flow_caption.setText(
            "Pipeline idle — start a scan to watch both routes run.")

    def _flow_set(self, route, stage) -> None:
        order = self._flow_order.get(route, [])
        if stage not in order:
            return
        idx = order.index(stage)
        self._flow_active[route] = idx
        for i, key in enumerate(order):
            box = self._flow_nodes[route][key]
            if i < idx:
                box.setStyleSheet(_STAGE_DONE)
            elif i == idx:
                box.setStyleSheet(_STAGE_ACTIVE)
            else:
                box.setStyleSheet(_STAGE_IDLE)
        name = "HUD panel" if route == "r1" else "Signature"
        label = dict(_R1_STAGES if route == "r1" else _R2_STAGES)[stage]
        clean = label.split(" ", 1)[1] if " " in label else label
        self._flow_caption.setText(f"{name} → {clean}")

    def _flow_mark(self, line: str) -> None:
        """Advance the flowchart from a single captured log line (first
        matching rule wins; logger-name hits beat shared-api keywords)."""
        if not hasattr(self, "_flow_nodes"):
            return
        low = line.lower()
        for substr, route, stage in _FLOW_RULES:
            if substr in low:
                if stage == "_timeout":
                    self._flow_reset()
                    self._flow_caption.setText(
                        "Signature → scanning… (panel not found yet)")
                else:
                    self._flow_set(route, stage)
                return

    def _on_copy_log_text_clicked(self) -> None:
        """Copy the captured pipeline log to the clipboard as PLAIN TEXT."""
        text = "\n".join(self._debug_log_lines)
        n = len(self._debug_log_lines)
        try:
            QApplication.clipboard().setText(text)
        except Exception as exc:
            self._debug_status_lbl.setText(f"copy failed: {exc}")
            self._debug_status_timer.start(2500)
            return
        self._debug_status_lbl.setText(f"Copied {n} log lines as text")
        self._debug_status_timer.start(2500)

    def closeEvent(self, event):
        try:
            self._flowchart.close()      # close the pop-out flow window too
        except Exception:
            pass
        try:
            self._timer.stop()
        except Exception:
            pass
        # Tear down any attached log handlers so closing the window
        # doesn't leak them onto the global logger registry. Also
        # restore each logger's pre-toggle level — same rationale as
        # the toggle-off branch above (otherwise closing the window
        # with Debug Mode still on leaves DEBUG-level chatter active
        # for the rest of the host process's lifetime).
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
