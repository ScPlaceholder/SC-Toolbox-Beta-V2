"""Mining HUD OCR calibration dialog.

User flow:
  1. Open dialog (from "Calibrate Mining Crops" button in scan bar)
  2. Tool starts streaming the live OCR-detected crops in real time
  3. For each row (Resource / Mass / Resistance / Instability):
       - User adjusts the bounding box if needed (drag to resize)
       - When the crop "looks right", user clicks "🔒 Lock <ROW>"
       - Lock button turns GREEN, that row is saved to disk immediately
  4. When all 3 required rows are locked, "CALIBRATION COMPLETE"
     banner appears in big text
  5. User closes dialog; runtime now uses the saved coords directly,
     skipping all detection

Tabs:
  • Calibrate — the live streaming + lock UI
  • Tutorial — how-to text + screenshot examples
  • (future) Voice — narrated walkthrough via Wingman TTS

The dialog is intentionally large + non-modal so the user can
continue to see the actual game HUD beside it for visual comparison.
"""
from __future__ import annotations

import logging
from typing import Optional

import numpy as np
from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import (
    QPoint, QPointF, QRect, QRectF, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QPainter, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFrame, QGraphicsPixmapItem,
    QGraphicsRectItem, QGraphicsScene, QGraphicsView, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QStatusBar, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from ocr.sc_ocr import calibration
from ocr.sc_ocr.calibration import DISPLAY_NAMES, FIELD_NAMES

log = logging.getLogger(__name__)

ACCENT = "#33dd88"
LOCK_GREEN = "#2a8"
LOCK_GRAY = "#555"
PANEL_BG = "#1d2530"
TEXT_PRIMARY = "#cbd"
TEXT_DIM = "#888"

# Polling interval for the live preview
POLL_MS = 400

# Field colors (matches debug_overlay)
FIELD_COLORS: dict[str, tuple[int, int, int]] = {
    "_mineral_row":   (0, 230, 100),
    "mass":           (0, 200, 255),
    "resistance":     (200, 100, 255),
    "instability":    (255, 100, 200),
}


class _CropPreview(QLabel):
    """Renders a single row's current value crop with a colored border."""

    def __init__(self, field: str, parent=None):
        super().__init__(parent)
        self._field = field
        self.setMinimumSize(360, 60)
        self.setMaximumHeight(80)
        self.setAlignment(Qt.AlignCenter)
        color = FIELD_COLORS.get(field, (200, 200, 200))
        self.setStyleSheet(
            f"background: #111; border: 2px solid rgb{color}; "
            "color: #666; font-family: Consolas; font-size: 9pt;"
        )
        self.setText("(no crop yet)")

    def update_crop(self, pil: Optional[Image.Image]) -> None:
        if pil is None:
            self.setText("(no crop yet)")
            return
        # Scale to fit our height, keep aspect
        avail_h = max(40, self.height() - 6)
        ratio = avail_h / max(1, pil.height)
        new_w = max(40, int(pil.width * ratio))
        scaled = pil.resize((new_w, avail_h), Image.NEAREST)
        try:
            self.setPixmap(QPixmap.fromImage(ImageQt(scaled.convert("RGB"))))
        except Exception:
            pass


class _RowControl(QGroupBox):
    """One row: live preview + nudge controls + lock button + status."""

    locked = Signal(str, dict)   # field_name, {"x":..,"y":..,"w":..,"h":..}
    unlocked = Signal(str)
    box_changed = Signal(str, dict)  # emitted when user nudges box (re-crop request)

    def __init__(self, field: str, parent=None):
        super().__init__(DISPLAY_NAMES.get(field, field), parent)
        self._field = field
        self._is_locked = False
        self._is_manual = False    # user has nudged → freeze live updates
        self._latest_box: Optional[dict] = None

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 18, 8, 8)
        v.setSpacing(4)

        self._preview = _CropPreview(field)
        v.addWidget(self._preview)

        # ── Nudge controls row ──
        nudge = QHBoxLayout()
        nudge.setSpacing(2)

        nudge.addWidget(self._make_nudge_label("MOVE:"))
        self._btn_left  = self._make_nudge_btn("←", -1,  0,  0,  0,
            "Move crop LEFT 1 px (Shift+click = 5 px)")
        self._btn_up    = self._make_nudge_btn("↑",  0, -1,  0,  0,
            "Move crop UP 1 px (Shift+click = 5 px)")
        self._btn_down  = self._make_nudge_btn("↓",  0, +1,  0,  0,
            "Move crop DOWN 1 px (Shift+click = 5 px)")
        self._btn_right = self._make_nudge_btn("→", +1,  0,  0,  0,
            "Move crop RIGHT 1 px (Shift+click = 5 px)")
        for b in (self._btn_left, self._btn_up, self._btn_down, self._btn_right):
            nudge.addWidget(b)

        nudge.addSpacing(10)
        nudge.addWidget(self._make_nudge_label("RESIZE:"))
        self._btn_wider   = self._make_nudge_btn("W+",  0,  0, +1,  0,
            "Make crop WIDER (extend right edge by 1 px)")
        self._btn_narrow  = self._make_nudge_btn("W−",  0,  0, -1,  0,
            "Make crop NARROWER")
        self._btn_taller  = self._make_nudge_btn("H+",  0,  0,  0, +1,
            "Make crop TALLER")
        self._btn_shorter = self._make_nudge_btn("H−",  0,  0,  0, -1,
            "Make crop SHORTER")
        for b in (self._btn_wider, self._btn_narrow, self._btn_taller, self._btn_shorter):
            nudge.addWidget(b)

        nudge.addStretch(1)

        self._btn_reset_live = QPushButton("↻ Auto")
        self._btn_reset_live.setToolTip(
            "Discard manual adjustments, return to live auto-detection"
        )
        self._btn_reset_live.setStyleSheet(
            "QPushButton { background: #444; color: #ccc; padding: 2px 6px; "
            "border: none; font-size: 9pt; }"
            "QPushButton:hover { background: #666; }"
        )
        self._btn_reset_live.clicked.connect(self._on_reset_to_live)
        nudge.addWidget(self._btn_reset_live)

        v.addLayout(nudge)

        # ── Status + lock row ──
        row = QHBoxLayout()
        self._status = QLabel("Waiting for crop…")
        self._status.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 9pt;"
        )
        row.addWidget(self._status, 1)

        self._lock_btn = QPushButton("🔒 Lock")
        self._lock_btn.setCursor(Qt.PointingHandCursor)
        self._lock_btn.setMinimumWidth(120)
        self._apply_lock_style(False)
        self._lock_btn.clicked.connect(self._on_lock_toggle)
        row.addWidget(self._lock_btn)

        v.addLayout(row)

    def _make_nudge_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 8pt;"
        )
        return lbl

    def _make_nudge_btn(
        self, text: str, dx: int, dy: int, dw: int, dh: int, tooltip: str,
    ) -> QPushButton:
        btn = QPushButton(text)
        btn.setFixedSize(32, 26)
        btn.setToolTip(tooltip)
        btn.setStyleSheet(
            f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
            "border: none; font-family: Consolas; font-size: 10pt; "
            "font-weight: bold; }}"
            "QPushButton:hover { background: #777; }"
            "QPushButton:pressed { background: #444; }"
        )
        btn.setAutoRepeat(True)
        btn.setAutoRepeatInterval(60)
        btn.setAutoRepeatDelay(350)
        btn.clicked.connect(
            lambda _checked, dx=dx, dy=dy, dw=dw, dh=dh:
                self._nudge(dx, dy, dw, dh)
        )
        return btn

    def update_live(self, pil: Optional[Image.Image], box: Optional[dict]) -> None:
        """Called every poll tick by the parent dialog."""
        if self._is_locked or self._is_manual:
            return  # don't overwrite locked / manually-nudged preview
        self._preview.update_crop(pil)
        self._latest_box = box
        if box is None:
            self._status.setText("(crop not detected — check HUD region)")
        else:
            self._status.setText(
                f"x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
            )

    def update_manual(self, pil: Optional[Image.Image], box: dict) -> None:
        """Called when the parent re-crops after a nudge (manual mode)."""
        self._latest_box = box
        self._preview.update_crop(pil)
        self._status.setText(
            f"MANUAL: x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
        )

    def _nudge(self, dx: int, dy: int, dw: int, dh: int) -> None:
        """User clicked an arrow / resize button. Adjust the box by
        the requested deltas and ask the parent to re-crop."""
        if self._is_locked:
            return
        if self._latest_box is None:
            self._status.setText("Cannot nudge: no crop detected yet")
            return
        # Honor Shift modifier for 5× steps
        try:
            mods = QApplication.keyboardModifiers()
            if mods & Qt.ShiftModifier:
                dx, dy, dw, dh = dx * 5, dy * 5, dw * 5, dh * 5
        except Exception:
            pass
        box = dict(self._latest_box)
        box["x"] = max(0, box["x"] + dx)
        box["y"] = max(0, box["y"] + dy)
        box["w"] = max(4, box["w"] + dw)
        box["h"] = max(4, box["h"] + dh)
        self._latest_box = box
        self._is_manual = True
        # Ask parent to re-crop the panel image with the new box
        self.box_changed.emit(self._field, dict(box))

    def _on_reset_to_live(self) -> None:
        """Discard manual adjustments, return to live auto-detection."""
        self._is_manual = False
        if self._is_locked:
            self._status.setText(
                "(unlock first to return to live detection)"
            )
            return
        self._status.setText("Returned to live detection — waiting for next scan")

    def display_locked(self, pil: Optional[Image.Image], box: dict) -> None:
        """Show the locked box visualization (called when load from disk)."""
        self._is_locked = True
        self._latest_box = box
        self._preview.update_crop(pil)
        self._status.setText(
            f"LOCKED: x={box['x']} y={box['y']} w={box['w']} h={box['h']}"
        )
        self._apply_lock_style(True)

    def is_locked(self) -> bool:
        return self._is_locked

    def reset(self) -> None:
        self._is_locked = False
        self._latest_box = None
        self._preview.setText("(no crop yet)")
        self._status.setText("Waiting for crop…")
        self._apply_lock_style(False)

    def _on_lock_toggle(self) -> None:
        if self._is_locked:
            self._is_locked = False
            self._status.setText("Unlocked — adjust HUD or wait for new crop")
            self._apply_lock_style(False)
            self.unlocked.emit(self._field)
            return
        # Recovery path: if _latest_box is stale-None (debug_overlay
        # state was cleared between our last update_live and this
        # click), try one more time to find a usable box from any
        # available source.
        if self._latest_box is None:
            recovered = self._recover_box()
            if recovered is not None:
                self._latest_box = recovered
                log.info(
                    "lock toggle: recovered box for %s from %s",
                    self._field, recovered.get("_source", "unknown"),
                )
        if self._latest_box is None:
            # LOUD failure — popup so the user actually notices.
            self._status.setText(
                "❌ Cannot lock: no crop detected yet. Make sure the "
                "toolbox's main scan is running ('Start Scan' button)."
            )
            try:
                from PySide6.QtWidgets import QMessageBox
                QMessageBox.warning(
                    self, "Cannot lock",
                    f"Cannot lock {self._field} — no crop detected.\n\n"
                    "Possible causes:\n"
                    "  • The toolbox's main scan loop isn't running. "
                    "Click 'Start Scan' in the scanner bar first.\n"
                    "  • The HUD region isn't pointed at a visible "
                    "SCAN RESULTS panel.\n"
                    "  • The OCR pipeline crashed. Check "
                    "logs/mining_signals.log for ERROR entries.",
                )
            except Exception:
                pass
            return
        self._is_locked = True
        self._status.setText(
            f"✓ LOCKED: x={self._latest_box['x']} y={self._latest_box['y']} "
            f"w={self._latest_box['w']} h={self._latest_box['h']}"
        )
        self._apply_lock_style(True)
        # Strip any internal _source key before emitting
        emit_box = {k: v for k, v in self._latest_box.items()
                    if not k.startswith("_")}
        self.locked.emit(self._field, dict(emit_box))

    def _recover_box(self) -> Optional[dict]:
        """Last-resort attempt to derive a box at lock-click time
        when _latest_box is stale-None. Tries 3 sources in order:
          1. debug_overlay's current in-memory state (may have been
             cleared by a recent scan reset; worth re-checking)
          2. The saved debug_value_<field>_crop.png file's pixel size
             paired with the most recent label_rows from disk
          3. None — caller will show a popup
        """
        # Source 1: in-memory debug_overlay state
        try:
            from ocr.sc_ocr import debug_overlay
            state = debug_overlay._state
            label_rows = state.get("label_rows", {})
            row = label_rows.get(self._field)
            crops = state.get("value_crops", {})
            crop = crops.get(self._field)
            if crop is not None:
                x1, y1, x2, y2 = crop
                box = {"x": int(x1), "y": int(y1),
                       "w": int(x2 - x1), "h": int(y2 - y1)}
                box["_source"] = "debug_overlay.value_crops"
                return box
            if row is not None:
                box = {
                    "x": int(row.get("label_right", 0)),
                    "y": int(row["y1"]),
                    "w": 200,
                    "h": int(row["y2"] - row["y1"]),
                }
                box["_source"] = "debug_overlay.label_rows"
                return box
        except Exception:
            pass
        # Source 2: derive from saved crop file size + last-known
        # state. We don't have its absolute coordinates so fall back
        # to a region-relative estimate.
        try:
            from pathlib import Path as _P
            tool_dir = _P(__file__).resolve().parent.parent
            crop_path = tool_dir / f"debug_value_{self._field}_crop.png"
            if crop_path.is_file():
                from PIL import Image as _Img
                pil = _Img.open(crop_path)
                w, h = pil.size
                # We don't know the absolute x/y from the cropped PNG
                # alone — give a placeholder positioned where the
                # value column typically is. User can nudge after.
                box = {"x": 200, "y": 100, "w": int(w), "h": int(h)}
                box["_source"] = "fallback_from_crop_file"
                return box
        except Exception:
            pass
        return None

    def _apply_lock_style(self, locked: bool) -> None:
        if locked:
            self._lock_btn.setText("🔓 Unlock")
            self._lock_btn.setStyleSheet(
                f"QPushButton {{ background: {LOCK_GREEN}; color: white; "
                "font-weight: bold; padding: 6px; border: none; }}"
                f"QPushButton:hover {{ background: #3b9; }}"
            )
            self.setStyleSheet(
                f"QGroupBox {{ border: 2px solid {LOCK_GREEN}; "
                "border-radius: 4px; margin-top: 6px; padding-top: 4px; }}"
                f"QGroupBox::title {{ color: {LOCK_GREEN}; "
                "font-weight: bold; }}"
            )
        else:
            self._lock_btn.setText("🔒 Lock")
            self._lock_btn.setStyleSheet(
                f"QPushButton {{ background: {LOCK_GRAY}; color: white; "
                "padding: 6px; border: none; }}"
                f"QPushButton:hover {{ background: #777; }}"
            )
            self.setStyleSheet(
                "QGroupBox { border: 1px solid #444; border-radius: 4px; "
                "margin-top: 6px; padding-top: 4px; }"
                f"QGroupBox::title {{ color: {TEXT_PRIMARY}; }}"
            )


class CalibrationDialog(QDialog):
    """Main calibration dialog — non-modal so user can see the game HUD
    beside it."""

    def __init__(self, region: dict, scan_callback, parent=None):
        """
        Parameters:
            region : the user's HUD region dict {"x", "y", "w", "h"}
            scan_callback : callable(region: dict) -> dict
                Should return a single OCR scan result. We use it to
                trigger the pipeline and read back what crops were
                used. The OCR pipeline already saves debug crops to
                disk; we read them from there for the live preview.
        """
        super().__init__(parent)
        self.setWindowTitle("Mining HUD OCR Calibration")
        self.setMinimumSize(720, 720)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, False)
        self._region = region
        self._scan_callback = scan_callback

        # ── Tabs ──
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # Top header — region info + completion banner
        self._header = QLabel("")
        self._header.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 12pt; "
            f"color: {ACCENT}; padding: 4px 8px;"
        )
        v.addWidget(self._header)

        self._completion_banner = QLabel("")
        self._completion_banner.setAlignment(Qt.AlignCenter)
        self._completion_banner.setStyleSheet(
            f"font-family: Electrolize, Consolas; font-size: 22pt; "
            f"font-weight: bold; color: {LOCK_GREEN}; padding: 10px; "
            "background: rgba(42, 136, 0, 0.12); border-radius: 6px;"
        )
        self._completion_banner.setVisible(False)
        v.addWidget(self._completion_banner)

        self._tabs = QTabWidget()
        v.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_calibrate_tab(), "Calibrate")
        self._tabs.addTab(self._build_tutorial_tab(), "Tutorial")
        # Pause OCR polling when the user is on the Tutorial tab
        # (otherwise every 400 ms we'd run a full OCR scan in the
        # background, locking up the UI as you read).
        self._tabs.currentChanged.connect(self._on_tab_changed)

        # Status bar
        self._status_bar = QStatusBar()
        v.addWidget(self._status_bar)

        # ── Polling timer for live crop updates ──
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)

        self._refresh_header()
        self._reload_locked_state_from_disk()

        # ── One-shot bootstrap scan ──
        # Do ONE scan on dialog open so the previews populate
        # immediately, even if the toolbox's main scan loop isn't
        # running yet. After this single bootstrap, the dialog only
        # READS the saved crop files (every POLL_MS) — it never runs
        # parallel OCR scans (which previously caused severe lag).
        if scan_callback is not None:
            try:
                scan_callback(region)
            except Exception as exc:
                log.debug("bootstrap scan failed: %s", exc)
        self._tick()

    # ──────────────────────────────────────────
    # Calibrate tab
    # ──────────────────────────────────────────

    def _build_calibrate_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(6)

        # ── Voice tutorial bar (front-and-center on the Calibrate tab) ──
        voice_bar = QWidget()
        voice_bar.setStyleSheet(f"background: {PANEL_BG}; padding: 6px;")
        vh = QHBoxLayout(voice_bar)
        vh.setContentsMargins(8, 6, 8, 6)
        vh.setSpacing(8)
        self._voice_btn = QPushButton("🔊 Play Voice Tutorial")
        self._voice_btn.setCursor(Qt.PointingHandCursor)
        self._voice_btn.setToolTip(
            "Audio walkthrough of how to calibrate the mining HUD crops"
        )
        self._voice_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: black; "
            "padding: 8px 18px; font-weight: bold; font-size: 10pt; "
            "border: none; }}"
            "QPushButton:hover { background: #5e8; }"
            "QPushButton:disabled { background: #444; color: #888; }"
        )
        self._voice_btn.clicked.connect(self._on_voice_play)
        vh.addWidget(self._voice_btn)

        self._voice_stop_btn = QPushButton("⏹ Stop")
        self._voice_stop_btn.setCursor(Qt.PointingHandCursor)
        self._voice_stop_btn.setStyleSheet(
            "QPushButton { background: #444; color: white; padding: 8px 14px; "
            "border: none; font-size: 10pt; }"
            "QPushButton:hover { background: #666; }"
            "QPushButton:disabled { background: #2a2a2a; color: #555; }"
        )
        self._voice_stop_btn.setEnabled(False)
        self._voice_stop_btn.clicked.connect(self._on_voice_stop)
        vh.addWidget(self._voice_stop_btn)

        self._voice_status = QLabel("")
        self._voice_status.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: Consolas; font-size: 9pt;"
        )
        vh.addWidget(self._voice_status, 1)

        # Panel Finder popout — opens a separate, resizable window
        # showing the live annotated panel image.
        self._panel_finder_btn = QPushButton("🖼 SC-OCR Panel Finder")
        self._panel_finder_btn.setCursor(Qt.PointingHandCursor)
        self._panel_finder_btn.setToolTip(
            "Open the SC-OCR Panel Finder in a separate window. "
            "Resizable from small to large; shows the live annotated "
            "panel as a visual reference while you calibrate."
        )
        self._panel_finder_btn.setStyleSheet(
            "QPushButton { background: #2a4a6a; color: white; "
            "padding: 8px 14px; font-weight: bold; font-size: 10pt; "
            "border: none; }"
            "QPushButton:hover { background: #3b5d7a; }"
        )
        self._panel_finder_btn.clicked.connect(self._on_open_panel_finder)
        vh.addWidget(self._panel_finder_btn)

        # Signature Finder popout — same pattern as Panel Finder, but
        # for the signal/signature scanner pipeline (icon-anchored
        # NCC + Tesseract diagnostic). Useful while calibrating to
        # confirm the signature scan region picks up the icon AND
        # all digits.
        self._signature_finder_btn = QPushButton("📈 Signature Finder")
        self._signature_finder_btn.setCursor(Qt.PointingHandCursor)
        self._signature_finder_btn.setToolTip(
            "Open the Signature Finder in a separate window. Live "
            "diagnostic for the signal scanner — shows the captured "
            "scan region, the NCC icon anchor (red box), the digit "
            "crop (green box), and the OCR result for every poll."
        )
        self._signature_finder_btn.setStyleSheet(
            "QPushButton { background: #2a6a4a; color: white; "
            "padding: 8px 14px; font-weight: bold; font-size: 10pt; "
            "border: none; }"
            "QPushButton:hover { background: #3b7a5d; }"
        )
        self._signature_finder_btn.clicked.connect(
            self._on_open_signature_finder
        )
        vh.addWidget(self._signature_finder_btn)

        v.addWidget(voice_bar)

        # Voice player held by the dialog so it survives playback.
        self._voice_player = None

        info = QLabel(
            "<b>How it works:</b> Each row shows the live crop being "
            "fed to the OCR pipeline. When a row's crop looks correct "
            "(value clearly visible, no label leakage), click "
            "<b style='color:#2a8'>🔒 Lock</b>. Locked rows are saved "
            "immediately and used at runtime instead of detection."
        )
        info.setWordWrap(True)
        info.setStyleSheet(
            f"background: {PANEL_BG}; color: {TEXT_PRIMARY}; "
            "padding: 8px; border-radius: 4px; font-size: 9pt;"
        )
        v.addWidget(info)

        self._row_controls: dict[str, _RowControl] = {}
        for field in FIELD_NAMES:
            ctrl = _RowControl(field)
            ctrl.locked.connect(self._on_row_locked)
            ctrl.unlocked.connect(self._on_row_unlocked)
            ctrl.box_changed.connect(self._on_row_box_changed)
            self._row_controls[field] = ctrl
            v.addWidget(ctrl)

        # Action row
        actions = QHBoxLayout()
        actions.addStretch(1)
        reset_btn = QPushButton("Reset all calibration")
        reset_btn.setStyleSheet(
            "QPushButton { background: #722; color: white; padding: 6px 14px; "
            "border: none; }"
            "QPushButton:hover { background: #944; }"
        )
        reset_btn.clicked.connect(self._on_reset_all)
        actions.addWidget(reset_btn)

        close_btn = QPushButton("Close")
        close_btn.setStyleSheet(
            f"QPushButton {{ background: {ACCENT}; color: black; "
            "padding: 6px 14px; font-weight: bold; border: none; }}"
            "QPushButton:hover { background: #5e8; }"
        )
        close_btn.clicked.connect(self.accept)
        actions.addWidget(close_btn)

        v.addLayout(actions)
        return page

    # ──────────────────────────────────────────
    # Tutorial tab
    # ──────────────────────────────────────────

    def _build_tutorial_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Tutorial text only — voice button lives on the Calibrate
        # tab now (front-and-center where users actually start).
        browser = QTextBrowser()
        browser.setStyleSheet(
            f"background: {PANEL_BG}; color: {TEXT_PRIMARY}; "
            "padding: 12px; font-family: Consolas; font-size: 10pt;"
        )
        browser.setOpenExternalLinks(False)
        browser.setHtml(self._tutorial_html())
        v.addWidget(browser, 1)

        return page

    def _tutorial_html(self) -> str:
        return """
<h2 style="color:#33dd88;">Why calibrate?</h2>
<p>The Mining Signals OCR engine reads the values in your in-game
SCAN RESULTS panel (MASS / RESISTANCE / INSTABILITY) so it can match
rocks to your fleet's breakability and surface them in the floating
HUD bubble.</p>

<p>Detecting <i>where</i> those values sit on screen is harder than
reading them. Backgrounds change (asteroid surface, deep space,
planet sky), HUD labels can be abbreviated, and panels render at
different positions depending on your resolution and HUD scale.</p>

<p>Calibration solves all of that with a one-time setup: you confirm
where each value sits, and the OCR uses your confirmed coordinates
forever after — no detection, no drift.</p>

<h2 style="color:#33dd88;">How to calibrate</h2>

<ol>
  <li><b>Open the SCAN RESULTS panel in-game.</b> Aim your mining
      laser at any rock until the panel appears.</li>
  <li><b>Watch the live crops.</b> Each row in the dialog shows the
      live OCR crop — the rectangle of pixels being fed to the
      digit recognizer. The crops update every ~0.4 seconds.</li>
  <li><b>For each row, when the crop looks right:</b>
      <ul>
          <li>The value digits should be FULLY VISIBLE
              (no label text on the left, no missing digits)</li>
          <li>The crop should NOT include parts of the row above
              or below it</li>
      </ul>
      Click <b style="color:#2a8;">🔒 Lock</b>. The row's border
      and lock button turn <b style="color:#2a8;">green</b>, and the
      crop coordinates are saved to disk immediately.</li>
  <li><b>Repeat for all three rows</b> (Mass, Resistance,
      Instability). The Resource (mineral name) row is optional.</li>
  <li>When all three are locked, the dialog displays
      <b style="color:#2a8;">"CALIBRATION COMPLETE"</b> in
      large text at the top. You can now close the dialog.</li>
</ol>

<h2 style="color:#33dd88;">When to recalibrate</h2>

<ul>
  <li>You change the screen resolution or HUD scale</li>
  <li>You switch between full-label and short-label HUD modes
      (e.g. helmet HUD vs. ship scanner panel)</li>
  <li>You move the user-defined HUD scan region</li>
</ul>

<p>To recalibrate, just open this dialog again. Click
<b style="color:#a44;">Reset all calibration</b> to start fresh, or
<b style="color:#888;">🔓 Unlock</b> a single row to redo just that
one.</p>

<h2 style="color:#33dd88;">Where is the calibration saved?</h2>

<p>Per-user file at:</p>
<p><code style="color:#fb0;">%LOCALAPPDATA%\\SC_Toolbox\\sc_ocr\\calibration.json</code></p>

<p>Calibration persists across toolbox restarts and updates. Multiple
HUD regions get separate calibrations (keyed by region geometry),
so you can switch between setups without losing your work.</p>
"""

    # ──────────────────────────────────────────
    # Live polling
    # ──────────────────────────────────────────

    def _tick(self) -> None:
        # IMPORTANT: We do NOT trigger our own OCR scan from the
        # dialog. The toolbox's main scan loop already runs OCR and
        # writes debug_value_*_crop.png files to disk. We just READ
        # those files here. Triggering parallel OCR scans from the
        # dialog (and the popout viewer) caused severe lag because
        # multiple OCR pipelines competed for CPU.
        #
        # If the user opens the dialog without "Start Scan" enabled,
        # the crop files will be stale. The status bar reflects this.

        # Read the latest debug crops from disk
        from ocr.sc_ocr import debug_overlay  # noqa: F401  (just for path)
        import os
        from pathlib import Path
        # debug_value_<field>_crop.png lives next to scan output
        # (tools/Mining_Signals/debug_value_*_crop.png)
        tool_dir = Path(__file__).resolve().parent.parent
        # Track latest crop file mtime to detect stale data
        newest_mtime = 0.0
        for field, ctrl in self._row_controls.items():
            # Both _mineral_row and the value rows now have
            # corresponding debug_value_<field>_crop.png files.
            crop_path = tool_dir / f"debug_value_{field}_crop.png"
            if crop_path.is_file():
                try:
                    mtime = crop_path.stat().st_mtime
                    newest_mtime = max(newest_mtime, mtime)
                    pil = Image.open(crop_path).convert("RGB")
                    if ctrl.is_locked():
                        # Refresh the locked preview with the current
                        # crop so user can see the rock's CURRENT
                        # content within their locked box bounds.
                        ctrl._preview.update_crop(pil)
                    else:
                        box = self._read_live_box(field)
                        ctrl.update_live(pil, box)
                except Exception as exc:
                    log.debug("preview load failed for %s: %s", field, exc)

        # Show data freshness in status bar so user knows if scanning
        # is live or stale (e.g., they haven't clicked Start Scan).
        if newest_mtime > 0:
            from datetime import datetime as _dt
            age = max(0, int(_dt.now().timestamp() - newest_mtime))
            if age <= 3:
                self._status_bar.showMessage(
                    f"✓ Live (last crop {age}s ago)", 2000,
                )
            else:
                self._status_bar.showMessage(
                    f"⚠ Crops are {age}s old — start the toolbox's "
                    "main scan ('Start Scan' button) to refresh", 0,
                )
        else:
            self._status_bar.showMessage(
                "⚠ No crop files yet — start the toolbox's main scan "
                "('Start Scan' button) to populate", 0,
            )

    def _crop_row_from_panel(self, field: str) -> Optional[Image.Image]:
        """Crop a row's strip from the latest panel image (for the
        mineral row, which doesn't get a separate value-crop file)."""
        try:
            from ocr.sc_ocr import debug_overlay
            img = debug_overlay._state.get("image")
            if img is None:
                return None
            label_rows = debug_overlay._state.get("label_rows", {})
            row = label_rows.get(field)
            if row is None:
                return None
            y1 = max(0, int(row["y1"]))
            y2 = min(img.height, int(row["y2"]))
            x1 = max(0, int(row.get("label_right", 0)))
            x2 = img.width
            if y2 <= y1 or x2 <= x1:
                return None
            return img.crop((x1, y1, x2, y2))
        except Exception:
            return None

    def _read_live_box(self, field: str) -> Optional[dict]:
        """Read the current detected bounding box from the debug overlay
        telemetry file (if it exists)."""
        # The runtime debug_overlay module has a label_rows dict that
        # we'd ideally read, but it's in-memory. Easiest is to read
        # the saved label_rows from the most recent scan via a small
        # JSON sidecar. For now, derive from crop size + region.
        try:
            from ocr.sc_ocr import debug_overlay
            state = debug_overlay._state
            label_rows = state.get("label_rows", {})
            row = label_rows.get(field)
            if row is None:
                return None
            # value-crop telemetry has the precise crop box
            crops = state.get("value_crops", {})
            crop = crops.get(field)
            if crop is not None:
                x1, y1, x2, y2 = crop
                return {"x": int(x1), "y": int(y1),
                        "w": int(x2 - x1), "h": int(y2 - y1)}
            # Fallback: row band + default x range
            return {
                "x": int(row.get("label_right", 0)),
                "y": int(row["y1"]),
                "w": 200,  # rough estimate
                "h": int(row["y2"] - row["y1"]),
            }
        except Exception:
            return None

    # ──────────────────────────────────────────
    # Lock / unlock handlers
    # ──────────────────────────────────────────

    def _on_row_box_changed(self, field: str, box: dict) -> None:
        """User nudged a row's box. Re-crop the panel image with the
        new coords and update the preview."""
        try:
            # Pull the latest panel image from the debug overlay state
            from ocr.sc_ocr import debug_overlay
            img = debug_overlay._state.get("image")
            if img is None:
                # Fall back: read the saved overlay PNG
                from pathlib import Path
                overlay_path = Path(debug_overlay.OUT_PATH)
                if overlay_path.is_file():
                    img = Image.open(overlay_path).convert("RGB")
            if img is None:
                self._status_bar.showMessage(
                    "Cannot re-crop: no panel image available "
                    "(start scanning so a panel is captured)", 5000,
                )
                return
            x, y, w, h = box["x"], box["y"], box["w"], box["h"]
            x2 = min(img.width, x + w)
            y2 = min(img.height, y + h)
            if x2 <= x or y2 <= y:
                return
            crop = img.crop((x, y, x2, y2))
            self._row_controls[field].update_manual(crop, box)
        except Exception as exc:
            log.debug("re-crop on nudge failed: %s", exc)

    def _on_row_locked(self, field: str, box: dict) -> None:
        # Determine value_column_left. If multiple rows are locked,
        # use the rightmost x (= longest label's colon position).
        # Otherwise use this row's x.
        all_xs = [box["x"]]
        for f, c in self._row_controls.items():
            if f == field or not c.is_locked():
                continue
            existing = calibration.get_row(self._region, f)
            if existing:
                all_xs.append(existing["x"])
        # Wait — value_column_left should be the X where values START
        # (the user-locked crop's LEFT edge IS that x). Pick max so
        # short-label rows (smaller x) don't override the longest
        # label's anchor.
        value_column_left = max(all_xs)

        # Try to capture image size from the debug overlay state
        image_size = None
        try:
            from ocr.sc_ocr import debug_overlay
            img = debug_overlay._state.get("image")
            if img is not None:
                image_size = img.size
        except Exception:
            pass

        calibration.save_row(
            self._region, field, box,
            image_size=image_size,
            value_column_left=value_column_left,
        )
        # Verify the row actually landed on disk. If the file was
        # written but the row didn't persist (e.g. AV interference,
        # permission issue, race), the user needs to know NOW rather
        # than discover it later when boxes jump during scanning.
        verify_box = calibration.get_row(self._region, field)
        if verify_box is None:
            self._status_bar.showMessage(
                f"⚠ FAILED to persist {DISPLAY_NAMES.get(field, field)} "
                f"to calibration.json — check log for details",
                10000,
            )
            log.error(
                "calibration_dialog: save_row(%s) reported success but "
                "get_row read-back returned None for region=%s",
                field, self._region,
            )
        else:
            self._status_bar.showMessage(
                f"Saved {DISPLAY_NAMES.get(field, field)} → "
                f"x={box['x']} y={box['y']} w={box['w']} h={box['h']}",
                5000,
            )
        self._refresh_completion_banner()

    def _on_row_unlocked(self, field: str) -> None:
        calibration.remove_row(self._region, field)
        self._status_bar.showMessage(
            f"Unlocked {DISPLAY_NAMES.get(field, field)}", 3000,
        )
        self._refresh_completion_banner()

    def _on_reset_all(self) -> None:
        calibration.clear_region(self._region)
        for ctrl in self._row_controls.values():
            ctrl.reset()
        self._refresh_completion_banner()
        self._status_bar.showMessage("All calibration cleared", 3000)

    def _refresh_completion_banner(self) -> None:
        complete = calibration.is_complete(self._region)
        if complete:
            self._completion_banner.setText("✅ CALIBRATION COMPLETE")
            self._completion_banner.setVisible(True)
        else:
            self._completion_banner.setVisible(False)

    def _refresh_header(self) -> None:
        r = self._region
        self._header.setText(
            f"HUD region: x={r.get('x')}, y={r.get('y')}, "
            f"w={r.get('w')}, h={r.get('h')}"
        )

    def _reload_locked_state_from_disk(self) -> None:
        """On open, read existing calibration and mark locked rows."""
        cal = calibration.load(self._region)
        if not cal:
            return
        rows = cal.get("rows", {})
        for field, box in rows.items():
            if field not in self._row_controls:
                continue
            # Try to load the corresponding crop file so the locked
            # row shows its actual content (instead of "no crop yet").
            from pathlib import Path as _P
            tool_dir = _P(__file__).resolve().parent.parent
            crop_path = tool_dir / f"debug_value_{field}_crop.png"
            pil = None
            if crop_path.is_file():
                try:
                    pil = Image.open(crop_path).convert("RGB")
                except Exception:
                    pil = None
            self._row_controls[field].display_locked(pil, box)
        self._refresh_completion_banner()

    # ──────────────────────────────────────────
    # Panel Finder popout
    # ──────────────────────────────────────────

    def _on_open_panel_finder(self) -> None:
        """Open (or raise) the standalone Panel Finder window.

        Single-instance guard:
          1. If we already created one in THIS process, raise it.
          2. Otherwise try to claim the cross-process slot. If
             another process holds it (e.g. the user double-clicked
             ``LAUNCH_PanelFinderViewer.bat``), the holder is poked
             to come to the front and we abort our own open.
        """
        try:
            from ui.panel_finder_popout import PanelFinderPopout
            # See mining_signals_app comment: ``mining_shared`` avoids
            # the name collision with SC_Toolbox's parent ``shared/``
            # package that the launcher pre-imports.
            from mining_shared.single_instance import SingleInstance

            existing = getattr(self, "_panel_finder_window", None)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return

            popout = PanelFinderPopout(parent=self)
            guard = SingleInstance("panel_finder", popout)
            if not guard.acquire():
                popout.deleteLater()
                self._status_bar.showMessage(
                    "Panel Finder is already open in another window — "
                    "brought to the front.", 5000,
                )
                return
            popout._single_instance = guard  # keep guard alive
            self._panel_finder_window = popout
            popout.show()
        except Exception as exc:
            log.error("panel finder popout failed: %s", exc, exc_info=True)
            self._status_bar.showMessage(
                f"Could not open Panel Finder: {exc}", 5000,
            )

    # ──────────────────────────────────────────
    # Signature Finder popout
    # ──────────────────────────────────────────

    def _on_open_signature_finder(self) -> None:
        """Open (or raise) the Signature Finder window.

        Mirrors :meth:`_on_open_panel_finder` exactly:
          1. Raise an existing in-process window if present.
          2. Otherwise try to claim the cross-process slot. If
             another process holds it (standalone .bat launch), poke
             the holder to come to the front and abort.
        """
        try:
            from scripts.signature_finder_viewer import SignatureFinderViewer
            # See mining_signals_app comment: ``mining_shared`` avoids
            # the name collision with SC_Toolbox's parent ``shared/``
            # package that the launcher pre-imports.
            from mining_shared.single_instance import SingleInstance

            existing = getattr(self, "_signature_finder_window", None)
            if existing is not None and existing.isVisible():
                existing.raise_()
                existing.activateWindow()
                return

            popout = SignatureFinderViewer()
            # Re-parent to the dialog so closing the calibration
            # window also tears down the viewer cleanly. Qt.Window
            # flag keeps it as its own top-level window.
            popout.setParent(self, Qt.Window)
            guard = SingleInstance("signature_finder", popout)
            if not guard.acquire():
                popout.deleteLater()
                self._status_bar.showMessage(
                    "Signature Finder is already open in another "
                    "window — brought to the front.", 5000,
                )
                return
            popout._single_instance = guard  # keep guard alive
            self._signature_finder_window = popout
            popout.show()
        except Exception as exc:
            log.error(
                "signature finder popout failed: %s", exc, exc_info=True,
            )
            self._status_bar.showMessage(
                f"Could not open Signature Finder: {exc}", 5000,
            )

    # ──────────────────────────────────────────
    # Voice tutorial
    # ──────────────────────────────────────────

    def _on_voice_play(self) -> None:
        """Play the calibration tutorial WAV.

        Fast path: cached WAV exists in the project → instant playback.
        First-time path: synthesize via Pocket TTS once, cache, then play.
        """
        from ui import voice_tutorial as _vt
        self._voice_btn.setEnabled(False)
        # Show synthesizing message ONLY if we actually need to generate
        # (avoid flashing the message for the cached path).
        if _vt._find_cached_tutorial() is None:
            self._voice_status.setText(
                "Generating tutorial audio (one-time, ~30 sec)…"
            )
            QApplication.processEvents()
        wav_path, source = _vt.get_tutorial_audio()
        if wav_path is None:
            self._voice_status.setText(
                "❌ No cached audio AND Pocket TTS unreachable on "
                "localhost:49112. Start Pocket TTS once to generate."
            )
            self._voice_btn.setEnabled(True)
            return
        if source == "generated":
            self._voice_status.setText(
                f"✓ Cached to {wav_path.name} — playing…"
            )
        # Lazy-init the player
        if self._voice_player is None:
            self._voice_player = _vt.VoicePlayer(
                on_state_change=self._on_voice_state,
            )
        ok = self._voice_player.play(wav_path)
        if not ok:
            self._voice_status.setText(
                "❌ Audio playback failed (Qt Multimedia issue)"
            )
            self._voice_btn.setEnabled(True)
            return
        if source == "cached":
            self._voice_status.setText("🔊 Playing…")
        self._voice_stop_btn.setEnabled(True)

    def _on_voice_stop(self) -> None:
        if self._voice_player is not None:
            self._voice_player.stop()
        self._voice_status.setText("Stopped")
        self._voice_btn.setEnabled(True)
        self._voice_stop_btn.setEnabled(False)

    def _on_voice_state(self, state: str) -> None:
        if state == "stopped":
            self._voice_status.setText("Done")
            self._voice_btn.setEnabled(True)
            self._voice_stop_btn.setEnabled(False)
        elif state == "playing":
            self._voice_status.setText("🔊 Playing…")
        elif state.startswith("error"):
            self._voice_status.setText(f"❌ {state}")
            self._voice_btn.setEnabled(True)
            self._voice_stop_btn.setEnabled(False)

    def _on_tab_changed(self, index: int) -> None:
        """Pause the OCR polling timer when leaving the Calibrate tab."""
        # Tab 0 = Calibrate, Tab 1 = Tutorial
        if index == 0:
            if not self._timer.isActive():
                self._timer.start(POLL_MS)
            self._status_bar.showMessage("Live polling resumed", 2000)
        else:
            if self._timer.isActive():
                self._timer.stop()
            self._status_bar.showMessage(
                "Live polling paused (not on Calibrate tab)", 0,
            )

    def closeEvent(self, event):
        try:
            self._timer.stop()
        except Exception:
            pass
        # Stop any in-flight voice playback so audio doesn't keep
        # narrating after the dialog is gone.
        try:
            if getattr(self, "_voice_player", None) is not None:
                self._voice_player.stop()
        except Exception:
            pass
        super().closeEvent(event)
