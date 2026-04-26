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
from datetime import datetime
from pathlib import Path
from typing import Optional

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget,
)

log = logging.getLogger(__name__)

# Default to the same overlay path the OCR pipeline writes to
_THIS_DIR = Path(__file__).resolve().parent
TOOL_DIR = _THIS_DIR.parent
DEFAULT_OVERLAY_PATH = TOOL_DIR / "debug_panel_overlay.png"

POLL_MS = 400


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

        # ── Centered image area ──
        # Use a wrapper widget with stretch-flanked layout for true
        # centering regardless of QLabel sizing quirks.
        wrap = QWidget()
        wrap.setStyleSheet("background: #111;")
        wrap.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        wrap_v = QVBoxLayout(wrap)
        wrap_v.setContentsMargins(0, 0, 0, 0)
        wrap_v.setSpacing(0)
        wrap_v.addStretch(1)

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
        wrap_v.addLayout(wrap_h)
        wrap_v.addStretch(1)
        v.addWidget(wrap, 1)

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
        # Skip while the window is being dragged.
        if self._time.monotonic() < self._move_pause_until:
            return
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

    def closeEvent(self, event):
        try:
            self._timer.stop()
        except Exception:
            pass
        super().closeEvent(event)
