"""Live viewer for the panel-finder diagnostic overlay.

The OCR pipeline writes ``debug_panel_overlay.png`` every scan with
the captured panel image plus colored annotations showing every
decision the panel finder made:

  YELLOW thick lines  → detected HUD separator lines (chrome)
  ORANGE thin lines   → top/bottom anchor lines used by finder
  GREEN box           → detected mineral-name band (anchor 2)
  CYAN boxes          → MASS/RESISTANCE/INSTABILITY row bands
  RED short verticals → shared label-right (value-column-left anchor)
  MAGENTA boxes       → actual value crops sent to the OCR
  YELLOW text         → per-row OCR text + min confidence + lock state
  WHITE text          → finder source ("by_position" or fallback) + pitch

This viewer polls the PNG every 400 ms and shows the latest version
scaled to fit. Watch it while the toolbox is scanning to see in
real time exactly where the panel finder is grabbing each crop and
whether locks are firing or invalidating.

Run with:
    python scripts/live_panel_finder_viewer.py
or double-click LAUNCH_PanelFinderViewer.bat in training_data_panels/.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)


THIS = Path(__file__).resolve()
TOOL = THIS.parent.parent
OVERLAY_PATH = TOOL / "debug_panel_overlay.png"

POLL_MS = 400


class Viewer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SC-OCR Panel Finder Viewer")
        self.setMinimumSize(640, 720)

        self._last_mtime = 0.0
        self._cached_pil = None  # latest loaded overlay PNG

        v = QVBoxLayout(self)
        v.setSpacing(4)

        legend = QLabel(
            "<b>Legend</b> &nbsp; "
            "<span style='color:#ffdc00'>━ HUD line</span> &nbsp; "
            "<span style='color:#ff8c00'>━ TOP/BOT anchor</span> &nbsp; "
            "<span style='color:#00e664'>▢ MINERAL band</span> &nbsp; "
            "<span style='color:#00c8ff'>▢ row</span> &nbsp; "
            "<span style='color:#ff64ff'>▢ value crop</span> &nbsp; "
            "<span style='color:#ff6464'>┃ label_right</span>"
        )
        legend.setStyleSheet(
            "padding: 6px; background: #1d2530; color: #cbd; "
            "font-size: 11px;"
        )
        legend.setWordWrap(True)
        v.addWidget(legend)

        self._meta = QLabel("(waiting for first scan…)")
        self._meta.setStyleSheet("color: #888; font-size: 10px; padding: 2px 6px;")
        v.addWidget(self._meta)

        self._img = QLabel("(no overlay yet)")
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setMinimumHeight(500)
        self._img.setStyleSheet(
            "background: #111; color: #666; border: 1px solid #333;"
        )
        v.addWidget(self._img, 1)

        bottom = QHBoxLayout()
        self._status = QLabel(f"Auto-refreshing every {POLL_MS} ms…")
        self._status.setStyleSheet("color: #888; font-size: 10px;")
        bottom.addWidget(self._status, 1)
        refresh_btn = QPushButton("Refresh now")
        refresh_btn.clicked.connect(self._tick)
        bottom.addWidget(refresh_btn)
        v.addLayout(bottom)

        # Pause-on-move: skip the polling tick while the user is
        # dragging the window so the title-bar drag doesn't fight
        # for the main thread with the file-load + LANCZOS-resize
        # work. Polling resumes ~350 ms after the last move event.
        import time as _time
        self._time = _time
        self._move_pause_until = 0.0
        self._move_pause_seconds = 0.35

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._move_pause_until = self._time.monotonic() + self._move_pause_seconds

    def _tick(self):
        if self._time.monotonic() < self._move_pause_until:
            return
        if not OVERLAY_PATH.is_file():
            self._meta.setText(f"(file not found: {OVERLAY_PATH.name})")
            self._img.setText("Waiting for the OCR pipeline to write the overlay…")
            return
        mtime = OVERLAY_PATH.stat().st_mtime
        size = OVERLAY_PATH.stat().st_size
        ts = datetime.fromtimestamp(mtime).strftime("%H:%M:%S")
        delta = max(0, int(datetime.now().timestamp() - mtime))
        self._meta.setText(f"{OVERLAY_PATH.name}   {ts}  ({delta}s ago)  {size:,} B")

        if mtime == self._last_mtime:
            return
        self._last_mtime = mtime

        try:
            pil = Image.open(OVERLAY_PATH).convert("RGB")
        except Exception as exc:
            self._img.setText(f"open failed: {exc}")
            return
        self._cached_pil = pil
        self._render()

    def _render(self):
        """Scale the cached PIL image to fit the QLabel's CURRENT
        available size (preserving aspect ratio) and center it."""
        pil = getattr(self, "_cached_pil", None)
        if pil is None:
            return
        # Use the QLabel's actual current size (updated on resize).
        avail_w = max(40, self._img.width() - 8)
        avail_h = max(40, self._img.height() - 8)
        # Fit to whichever dimension is more constraining.
        ratio = min(avail_w / pil.width, avail_h / pil.height)
        # Allow scaling UP small images too — user wants the captured
        # region fully visible regardless of original size.
        new_w = max(20, int(pil.width * ratio))
        new_h = max(20, int(pil.height * ratio))
        scaled = pil.resize((new_w, new_h), Image.LANCZOS)
        self._img.setPixmap(QPixmap.fromImage(ImageQt(scaled)))

    def resizeEvent(self, event):
        """Re-render on window resize so the image keeps fitting."""
        super().resizeEvent(event)
        self._render()


def main():
    app = QApplication(sys.argv)
    win = Viewer()
    primary = app.primaryScreen().availableGeometry()
    win.move(primary.left() + 50, primary.top() + 50)

    # Cross-process single-instance enforcement. Shares the slot with
    # the popout opened from the calibration dialog so they can't
    # both be visible at the same time.
    # Note: package is named ``mining_shared`` to avoid collision with
    # the SC_Toolbox-wide ``shared/`` package one directory up.
    if str(TOOL) not in sys.path:
        sys.path.insert(0, str(TOOL))
    from mining_shared.single_instance import SingleInstance
    guard = SingleInstance("panel_finder", win)
    if not guard.acquire():
        # Holder already poked. Exit without showing our own copy.
        return 0
    win._single_instance = guard

    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
