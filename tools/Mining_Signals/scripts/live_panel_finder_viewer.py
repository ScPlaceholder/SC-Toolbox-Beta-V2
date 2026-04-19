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
MAX_VIEW_W = 900   # scale down if overlay is wider than this


class Viewer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("SC-OCR Panel Finder Viewer")
        self.setMinimumSize(640, 720)

        self._last_mtime = 0.0

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

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

    def _tick(self):
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
        # Scale to fit width
        if pil.width > MAX_VIEW_W:
            ratio = MAX_VIEW_W / pil.width
            pil = pil.resize(
                (MAX_VIEW_W, int(pil.height * ratio)),
                Image.LANCZOS,
            )
        self._img.setPixmap(QPixmap.fromImage(ImageQt(pil)))


def main():
    app = QApplication(sys.argv)
    win = Viewer()
    primary = app.primaryScreen().availableGeometry()
    win.move(primary.left() + 50, primary.top() + 50)
    win.show()
    win.raise_()
    win.activateWindow()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
