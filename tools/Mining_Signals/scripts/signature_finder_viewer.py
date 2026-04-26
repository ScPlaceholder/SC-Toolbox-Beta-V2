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

CONFIG_PATH = TOOL / "mining_signals_config.json"
POLL_MS = 500
HISTORY_LEN = 10

# Theme matches the rest of the toolbox tools.
ACCENT = "#33dd88"
RED = "#ff4444"
DIM = "#888888"
BG = "#1e1e1e"
FG = "#e0e0e0"


def _load_scan_region() -> Optional[dict]:
    """Read ocr_region from mining_signals_config.json. Returns None
    if the file or key is missing."""
    if not CONFIG_PATH.is_file():
        return None
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
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
) -> Image.Image:
    """Draw colored overlays on a copy of `img`."""
    out = img.copy()
    draw = ImageDraw.Draw(out)
    if icon_box is not None:
        x1, y1, x2, y2 = icon_box
        draw.rectangle([x1, y1, x2 - 1, y2 - 1], outline=RED, width=2)
    if digit_box is not None:
        x1, y1, x2, y2 = digit_box
        draw.rectangle([x1, y1, x2 - 1, y2 - 1], outline=ACCENT, width=2)
    return out


# ─────────────────────────────────────────────────────────────
# UI
# ─────────────────────────────────────────────────────────────

from PySide6.QtCore import Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QFont, QPalette, QPixmap  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy,
    QVBoxLayout, QWidget,
)
from PIL.ImageQt import ImageQt  # noqa: E402


class SignatureFinderViewer(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Signature Finder — live anchor + OCR diagnostic")
        self.setMinimumSize(720, 560)
        self.setStyleSheet(f"background: {BG}; color: {FG};")

        self._region: Optional[dict] = _load_scan_region()
        self._history: deque = deque(maxlen=HISTORY_LEN)
        self._build_ui()

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

        # Header
        title = QLabel("SIGNATURE FINDER", self)
        tf = QFont("Consolas")
        tf.setPointSize(13); tf.setBold(True)
        title.setFont(tf)
        title.setStyleSheet(f"color: {ACCENT}; background: transparent;")
        root.addWidget(title)

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
        self._region = _load_scan_region()
        self._refresh_region_label()

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
        # Skip while the user is dragging/repositioning the window
        # (see __init__ comment on _move_pause_until). The next
        # tick after the pause window expires will catch up.
        if time.monotonic() < self._move_pause_until:
            return
        if self._region is None:
            self._reload_region()
            if self._region is None:
                return
        if not self._ensure_imports():
            return

        t0 = time.monotonic()
        img = _capture(self._region)
        if img is None:
            self._image_lbl.setText("(capture failed)")
            return

        # Run anchor + recognize
        gray = np.asarray(img.convert("L"), dtype=np.uint8)
        try:
            icon_found = self._anchor.find_icon(gray)
        except Exception as e:
            icon_found = None
            self._status_lbl.setText(f"anchor error: {e}")
        try:
            digit_box = self._anchor.find_digit_crop_box(gray)
        except Exception:
            digit_box = None
        try:
            value = self._api._signal_recognize_pil(img)
        except Exception as e:
            value = None

        dt_ms = (time.monotonic() - t0) * 1000.0

        # Annotate the captured image
        icon_xyxy = None
        if icon_found is not None:
            x1, y1, x2, y2, _score = icon_found
            icon_xyxy = (x1, y1, x2, y2)
        annotated = _annotate(img, icon_xyxy, digit_box)
        # Scale to fit the label's current size while keeping aspect.
        max_w = max(200, self._image_lbl.width() - 16)
        max_h = max(100, self._image_lbl.height() - 16)
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
        qim = ImageQt(annotated)
        self._image_lbl.setPixmap(QPixmap.fromImage(qim))

        # Big result + status line
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

        anchor_status = (
            f"anchor: x={icon_found[0]}..{icon_found[2]} score={icon_found[4]:.2f}"
            if icon_found is not None else "anchor: MISS"
        )
        crop_status = (
            f"crop: x={digit_box[0]}..{digit_box[2]}"
            if digit_box is not None else "crop: —"
        )
        self._status_lbl.setText(
            f"{anchor_status}  |  {crop_status}  |  {dt_ms:.0f} ms"
        )

        # History
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"{ts}  {value!s:>7}  ({dt_ms:.0f} ms)"
        self._history.append(line)
        self._history_lbl.setText("\n".join(self._history))


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
