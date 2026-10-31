"""Manual override dialog — user draws crop boxes by hand on a live HUD shot.

Opened from the EMERGENCY OVERRIDE button in the calibration dialog. The
user picks a field, click-drags a rectangle on a scaled-up screenshot of
the live HUD region, and saves. Saving:

  * persists each drawn box via
    ``calibration.set_manual_override_box(region, field, box)``
  * flips ``calibration.set_manual_override_mode(region, True)`` so the
    OCR pipeline reads from the user's manual rectangles instead of any
    auto-detection / locked-row data.

Coordinates the user draws are in HUD-region-relative pixels (same
origin as ``calibration.json``'s row entries) — the screenshot we show
IS the HUD-region capture, so no transformation is needed beyond the
display-scale factor we apply for visibility.
"""
from __future__ import annotations

import logging
from typing import Optional

from PIL import Image
from PIL.ImageQt import ImageQt
from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QSizePolicy, QStatusBar, QVBoxLayout, QWidget,
)

from ocr.sc_ocr import calibration

from .theme import ACCENT

log = logging.getLogger(__name__)


# Colors per field — match the FIELD_COLORS palette from calibration_dialog
# where possible. We deliberately re-define rather than import to keep
# this module standalone (per the "don't import from _RowControl" rule).
# The mineral row uses the calibration field name "_mineral_row" but we
# expose it to the user as "Mineral".
FIELD_COLORS_RGB: dict[str, tuple[int, int, int]] = {
    "_mineral_row":   (0, 230, 100),     # cyan-green for mineral
    "mass":           (0, 200, 255),     # cyan
    "resistance":     (200, 100, 255),   # magenta
    "instability":    (255, 100, 200),   # pink/red
}

# User-facing label for each field in the override dialog. Distinct from
# calibration.DISPLAY_NAMES because we want short labels here (selector
# buttons fit on one row).
FIELD_LABELS: dict[str, str] = {
    "_mineral_row":   "Mineral",
    "mass":           "Mass",
    "resistance":     "Resistance",
    "instability":    "Instability",
}

# Fields the manual override dialog supports. Mass / Resistance /
# Instability are required; Mineral is optional but allowed (matches the
# scope: "Mineral, Mass, Resistance, Instability" per the task brief).
OVERRIDE_FIELDS: tuple[str, ...] = (
    "_mineral_row", "mass", "resistance", "instability",
)

# Minimum drawn-rectangle size in HUD-region pixels. Anything smaller
# than this is considered an accidental click+release without a real
# drag — we discard it rather than save a 1×1 box that would crash
# downstream cropping.
MIN_BOX_W = 4
MIN_BOX_H = 4

# Display scale: the HUD region is typically a few hundred pixels wide
# and ~150 px tall — too small to draw on accurately. We scale up by
# this factor for the on-screen draw widget. User-drawn rectangles are
# divided back by this factor when stored so they remain HUD-region-
# relative pixels (matching what `calibration.set_manual_override_box`
# expects).
DISPLAY_SCALE = 2

# Minimum scaled draw widget size — even tiny HUD regions get a
# usable canvas. Combined with DISPLAY_SCALE: a typical 400×200 HUD
# scales to 800×400. A small 200×100 HUD scales to 400×200, then the
# floor pushes it to MIN_DRAW_W × MIN_DRAW_H if it's still smaller.
MIN_DRAW_W = 480
MIN_DRAW_H = 240


class _DrawArea(QLabel):
    """QLabel that shows the HUD screenshot and lets the user draw rects.

    One rectangle per field at any given time — drawing a new rect for
    a field that already has one replaces the previous one. The active
    field is whatever the parent dialog last selected (set via
    :meth:`set_active_field`).

    Coordinates exposed to the parent are HUD-region-relative (i.e. the
    same coordinate system as ``calibration.json`` row entries). The
    DISPLAY_SCALE factor is applied internally and only affects
    rendering, never the saved values.
    """

    box_drawn = Signal(str, dict)  # (field, {"x","y","w","h"} in HUD coords)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet(
            "background: #0a0a0a; border: 1px solid #333;"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._pixmap_unscaled: Optional[QPixmap] = None
        self._scale = DISPLAY_SCALE
        # Map field → rect in WIDGET-display coordinates (already
        # multiplied by self._scale). The HUD-region rect is just
        # widget_rect / self._scale, computed on demand.
        self._boxes: dict[str, QRect] = {}
        # Currently selected field — None means clicks/drags are ignored
        # so the user can't accidentally draw before picking a field.
        self._active_field: Optional[str] = None
        # In-progress drag origin / live rect, in widget coordinates.
        self._drag_origin: Optional[QPoint] = None
        self._drag_rect: Optional[QRect] = None

    def set_image(self, pil: Optional[Image.Image]) -> None:
        """Display ``pil`` as the background. Pass ``None`` to clear.

        We pre-render the scaled QPixmap so paintEvent is just a blit;
        scaling a PIL.Image every paint would burn CPU as the user drags.
        """
        if pil is None:
            self._pixmap_unscaled = None
            self.setText("(no HUD capture available)")
            self.setMinimumSize(MIN_DRAW_W, MIN_DRAW_H)
            return
        try:
            qimg = ImageQt(pil.convert("RGB"))
            self._pixmap_unscaled = QPixmap.fromImage(QImage(qimg))
            scaled_w = max(MIN_DRAW_W, pil.width * self._scale)
            scaled_h = max(MIN_DRAW_H, pil.height * self._scale)
            # Fix the widget size so the user knows where the canvas
            # ends — letting the QLabel auto-size against an empty area
            # makes click coords ambiguous when they fall outside the
            # pixmap.
            self.setFixedSize(scaled_w, scaled_h)
            self.update()
        except Exception as exc:
            log.warning("manual_override: set_image failed: %s", exc)
            self._pixmap_unscaled = None
            self.setText(f"(image render failed: {exc})")

    def set_active_field(self, field: Optional[str]) -> None:
        self._active_field = field
        # Clear any in-progress drag so switching fields mid-drag
        # doesn't leave a phantom rectangle on the previous field.
        self._drag_origin = None
        self._drag_rect = None
        self.update()

    def set_box_for_field(self, field: str, hud_box: dict) -> None:
        """Programmatically place a rectangle (used by 'Load from current
        locks'). ``hud_box`` is HUD-region-relative."""
        widget_rect = QRect(
            int(hud_box.get("x", 0)) * self._scale,
            int(hud_box.get("y", 0)) * self._scale,
            int(hud_box.get("w", 0)) * self._scale,
            int(hud_box.get("h", 0)) * self._scale,
        )
        self._boxes[field] = widget_rect
        self.update()

    def clear_field(self, field: str) -> None:
        if field in self._boxes:
            del self._boxes[field]
            self.update()

    def clear_all(self) -> None:
        self._boxes.clear()
        self.update()

    def get_hud_box(self, field: str) -> Optional[dict]:
        rect = self._boxes.get(field)
        if rect is None or rect.width() <= 0 or rect.height() <= 0:
            return None
        return {
            "x": int(rect.x() / self._scale),
            "y": int(rect.y() / self._scale),
            "w": int(rect.width() / self._scale),
            "h": int(rect.height() / self._scale),
        }

    # ── Painting ──

    def paintEvent(self, event):  # noqa: D401 - Qt override
        painter = QPainter(self)
        if self._pixmap_unscaled is None:
            super().paintEvent(event)
            painter.end()
            return
        # Draw the scaled HUD pixmap as the background.
        target = QRect(
            0, 0,
            self._pixmap_unscaled.width() * self._scale,
            self._pixmap_unscaled.height() * self._scale,
        )
        painter.drawPixmap(target, self._pixmap_unscaled)

        # Draw all committed rectangles.
        for field, rect in self._boxes.items():
            color = FIELD_COLORS_RGB.get(field, (255, 255, 0))
            pen = QPen(QColor(*color), 2)
            painter.setPen(pen)
            painter.drawRect(rect)
            # Field label inside the rect.
            painter.fillRect(
                QRect(rect.x(), rect.y(), 90, 16),
                QColor(0, 0, 0, 160),
            )
            painter.setPen(QColor(*color))
            painter.drawText(
                rect.x() + 4, rect.y() + 12,
                FIELD_LABELS.get(field, field),
            )

        # In-progress drag rectangle (dashed).
        if self._drag_rect is not None and self._active_field is not None:
            color = FIELD_COLORS_RGB.get(self._active_field, (255, 255, 0))
            pen = QPen(QColor(*color), 2, Qt.DashLine)
            painter.setPen(pen)
            painter.drawRect(self._drag_rect)
        painter.end()

    # ── Mouse handling ──

    def mousePressEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton or self._active_field is None:
            return super().mousePressEvent(event)
        if self._pixmap_unscaled is None:
            return
        self._drag_origin = event.position().toPoint()
        self._drag_rect = QRect(self._drag_origin, self._drag_origin)
        self.update()

    def mouseMoveEvent(self, event: QMouseEvent):
        if self._drag_origin is None:
            return super().mouseMoveEvent(event)
        cur = event.position().toPoint()
        # Clamp to the pixmap area so the user can't draw a box that
        # extends past the actual HUD capture (saving a coord outside
        # the screenshot would point into garbage on the live HUD).
        if self._pixmap_unscaled is not None:
            max_x = self._pixmap_unscaled.width() * self._scale - 1
            max_y = self._pixmap_unscaled.height() * self._scale - 1
            cx = max(0, min(cur.x(), max_x))
            cy = max(0, min(cur.y(), max_y))
            cur = QPoint(cx, cy)
        self._drag_rect = QRect(self._drag_origin, cur).normalized()
        self.update()

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() != Qt.LeftButton or self._drag_origin is None:
            return super().mouseReleaseEvent(event)
        rect = self._drag_rect
        self._drag_origin = None
        self._drag_rect = None
        if rect is None or self._active_field is None:
            self.update()
            return
        # Reject tiny accidental clicks. Convert min sizes to widget
        # coords first since the drag rect is in widget coordinates.
        if (rect.width() < MIN_BOX_W * self._scale
                or rect.height() < MIN_BOX_H * self._scale):
            self.update()
            return
        self._boxes[self._active_field] = rect
        self.update()
        hud_box = self.get_hud_box(self._active_field)
        if hud_box is not None:
            self.box_drawn.emit(self._active_field, hud_box)


class ManualOverrideDialog(QDialog):
    """Modal dialog: live HUD screenshot + per-field draw + save.

    Opened from the EMERGENCY OVERRIDE button. On accept, persists each
    drawn box to ``calibration.set_manual_override_box`` and flips
    ``calibration.set_manual_override_mode(region, True)`` so the OCR
    pipeline reads from the user's rectangles instead of auto-detection.

    If ``region`` is None (the user hasn't set their HUD region yet) the
    dialog still opens but disables Save and shows a clear message —
    that way the user gets feedback rather than a silent no-op.
    """

    overrides_saved = Signal(dict)  # {field: hud_box_dict, ...}

    def __init__(
        self,
        region: Optional[dict],
        hud_pil: Optional[Image.Image],
        parent=None,
    ):
        super().__init__(parent)
        self._region = region
        self._hud_pil = hud_pil
        self.setWindowTitle("Manual Override — Draw Crop Boxes")
        self.setMinimumSize(720, 560)
        self.resize(900, 680)
        self.setModal(True)

        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        v.setSpacing(6)

        # ── Big red warning banner ──
        banner = QLabel(
            "<b>MANUAL OVERRIDE MODE</b><br>"
            "Auto-detection disabled. Draw a box for each field you "
            "want OCR to read. Saving locks these positions until you "
            "disable manual mode."
        )
        banner.setWordWrap(True)
        banner.setStyleSheet(
            "background: #aa1a1a; color: #fff; padding: 10px; "
            "border-radius: 4px; font-family: Electrolize, Consolas; "
            "font-size: 11pt;"
        )
        v.addWidget(banner)

        # If we have no region the rest of the UI is read-only.
        if self._region is None:
            note = QLabel(
                "No HUD region set. Use 'Set HUD scanning region' in "
                "the toolbox first, then re-open this dialog. Save is "
                "disabled until a region exists."
            )
            note.setWordWrap(True)
            note.setStyleSheet(
                "background: #5a3a1a; color: #ffd28a; padding: 8px; "
                "border-radius: 4px;"
            )
            v.addWidget(note)

        # ── Field selector row ──
        field_bar = QHBoxLayout()
        field_bar.setSpacing(4)
        field_bar.addWidget(QLabel("Field to draw:"))
        self._field_buttons: dict[str, QPushButton] = {}
        for field in OVERRIDE_FIELDS:
            btn = QPushButton(FIELD_LABELS.get(field, field))
            btn.setCheckable(True)
            r, g, b = FIELD_COLORS_RGB.get(field, (180, 180, 180))
            btn.setStyleSheet(
                f"QPushButton {{ background: #2a2a2a; color: rgb({r},{g},{b}); "
                "padding: 6px 12px; border: 2px solid #444; }}"
                f"QPushButton:checked {{ background: rgb({r//4},{g//4},{b//4}); "
                f"border: 2px solid rgb({r},{g},{b}); color: white; "
                "font-weight: bold; }}"
                "QPushButton:hover { border-color: #888; }"
            )
            btn.clicked.connect(
                lambda _checked, f=field: self._on_field_picked(f)
            )
            field_bar.addWidget(btn)
            self._field_buttons[field] = btn
        field_bar.addStretch(1)
        v.addLayout(field_bar)

        # ── Main area: draw widget + sidebar ──
        body = QHBoxLayout()
        body.setSpacing(8)

        # Draw area in a frame so the canvas edges are visible. The
        # canvas itself sets a fixed size driven by the HUD capture's
        # dimensions (× DISPLAY_SCALE), so wrapping in a frame keeps
        # the layout from collapsing on small HUD captures.
        draw_frame = QFrame()
        draw_frame.setFrameShape(QFrame.StyledPanel)
        draw_frame.setStyleSheet(
            "QFrame { background: #050505; border: 1px solid #333; }"
        )
        df_layout = QVBoxLayout(draw_frame)
        df_layout.setContentsMargins(4, 4, 4, 4)
        self._draw = _DrawArea()
        self._draw.set_image(self._hud_pil)
        self._draw.box_drawn.connect(self._on_box_drawn)
        df_layout.addWidget(self._draw)
        df_layout.addStretch(1)
        body.addWidget(draw_frame, 1)

        # Sidebar: per-field current coords + clear buttons.
        sidebar = QVBoxLayout()
        sidebar.setSpacing(4)
        sidebar_label = QLabel("Current boxes")
        sidebar_label.setStyleSheet(
            f"color: {ACCENT}; font-weight: bold; padding: 2px;"
        )
        sidebar.addWidget(sidebar_label)
        self._box_list = QListWidget()
        self._box_list.setStyleSheet(
            "QListWidget { background: #181818; color: #cccccc; "
            "font-family: Consolas; font-size: 9pt; border: 1px solid #333; }"
        )
        self._box_list.setMinimumWidth(220)
        sidebar.addWidget(self._box_list, 1)

        clear_one_btn = QPushButton("Clear selected")
        clear_one_btn.setStyleSheet(
            "QPushButton { background: #553333; color: #ffd0d0; "
            "padding: 4px; border: none; }"
            "QPushButton:hover { background: #774444; }"
        )
        clear_one_btn.clicked.connect(self._on_clear_selected)
        sidebar.addWidget(clear_one_btn)

        clear_all_btn = QPushButton("Clear all boxes")
        clear_all_btn.setStyleSheet(
            "QPushButton { background: #663333; color: #ffd0d0; "
            "padding: 4px; border: none; }"
            "QPushButton:hover { background: #884444; }"
        )
        clear_all_btn.clicked.connect(self._on_clear_all)
        sidebar.addWidget(clear_all_btn)

        load_btn = QPushButton("Load from current locks")
        load_btn.setToolTip(
            "Pre-populate the boxes with whatever is currently saved as "
            "calibration locks for this region. You can then nudge / "
            "redraw individual fields."
        )
        load_btn.setStyleSheet(
            "QPushButton { background: #335577; color: #cce0ff; "
            "padding: 4px; border: none; }"
            "QPushButton:hover { background: #4477aa; }"
        )
        load_btn.clicked.connect(self._on_load_from_locks)
        sidebar.addWidget(load_btn)

        body.addLayout(sidebar, 0)
        v.addLayout(body, 1)

        # ── Save / Cancel row ──
        actions = QHBoxLayout()
        actions.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setStyleSheet(
            "QPushButton { background: #444; color: #ddd; "
            "padding: 8px 18px; border: none; }"
            "QPushButton:hover { background: #666; }"
        )
        cancel_btn.clicked.connect(self.reject)
        actions.addWidget(cancel_btn)

        self._save_btn = QPushButton("💾 Save Manual Override")
        self._save_btn.setStyleSheet(
            "QPushButton { background: #aa1a1a; color: white; "
            "padding: 8px 18px; font-weight: bold; border: none; }"
            "QPushButton:hover { background: #cc2222; }"
            "QPushButton:disabled { background: #553333; color: #888; }"
        )
        self._save_btn.clicked.connect(self._on_save)
        if self._region is None:
            self._save_btn.setEnabled(False)
        actions.addWidget(self._save_btn)

        v.addLayout(actions)

        # ── Status bar ──
        self._status = QStatusBar()
        v.addWidget(self._status)
        if self._hud_pil is None:
            self._status.showMessage(
                "No HUD screenshot available — start the toolbox's main "
                "scan once so a frame is captured, then re-open.", 0,
            )
        else:
            self._status.showMessage(
                "Pick a field above, then click and drag on the "
                "screenshot to draw its box.", 0,
            )

        # Default to the first field selected so the user can start
        # drawing immediately. They can switch with the buttons.
        if OVERRIDE_FIELDS:
            self._on_field_picked(OVERRIDE_FIELDS[1] if len(OVERRIDE_FIELDS) > 1
                                  else OVERRIDE_FIELDS[0])

    # ── Field selection ──

    def _on_field_picked(self, field: str) -> None:
        self._draw.set_active_field(field)
        for f, btn in self._field_buttons.items():
            btn.setChecked(f == field)
        self._status.showMessage(
            f"Drawing: {FIELD_LABELS.get(field, field)} — "
            "click and drag on the screenshot.", 0,
        )

    # ── Drawing callbacks ──

    def _on_box_drawn(self, field: str, hud_box: dict) -> None:
        self._refresh_box_list()
        self._status.showMessage(
            f"{FIELD_LABELS.get(field, field)} → "
            f"x={hud_box['x']} y={hud_box['y']} "
            f"w={hud_box['w']} h={hud_box['h']}", 4000,
        )

    def _refresh_box_list(self) -> None:
        self._box_list.clear()
        for field in OVERRIDE_FIELDS:
            box = self._draw.get_hud_box(field)
            label = FIELD_LABELS.get(field, field)
            if box is None:
                text = f"{label}: (not drawn)"
            else:
                text = (
                    f"{label}: x={box['x']} y={box['y']} "
                    f"w={box['w']} h={box['h']}"
                )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, field)
            r, g, b = FIELD_COLORS_RGB.get(field, (200, 200, 200))
            item.setForeground(QColor(r, g, b))
            self._box_list.addItem(item)

    # ── Sidebar buttons ──

    def _on_clear_selected(self) -> None:
        items = self._box_list.selectedItems()
        if not items:
            self._status.showMessage(
                "Select an entry in the list first.", 3000,
            )
            return
        for item in items:
            field = item.data(Qt.UserRole)
            if field:
                self._draw.clear_field(field)
        self._refresh_box_list()

    def _on_clear_all(self) -> None:
        self._draw.clear_all()
        self._refresh_box_list()
        self._status.showMessage("All boxes cleared.", 3000)

    def _on_load_from_locks(self) -> None:
        """Pre-populate boxes from existing per-row calibration locks
        for this region. Useful when the user wants to start from
        auto-detected positions and only adjust one or two fields.

        Reads through ``calibration.get_row(region, field)`` so we get
        whatever the caller's calibration source-of-truth is. Anything
        that returns None for a given field is just skipped (the user
        can still draw it from scratch)."""
        if self._region is None:
            self._status.showMessage(
                "No region set — nothing to load.", 3000,
            )
            return
        # Saved locks are in the reader's RESIZED frame space
        # (calibration.json "image_size", e.g. 907x670); the draw canvas
        # and $manual_overrides are raw HUD-capture pixels (region w x
        # h, e.g. 436x322). Convert, or boxes saved from here get
        # double-scaled by the apply-time scaling in onnx_hud_reader.
        try:
            _entry = calibration.load(self._region) or {}
            _iw, _ih = (_entry.get("image_size") or [0, 0])[:2]
            _fx = (int(self._region.get("w", 0)) / _iw) if _iw else 1.0
            _fy = (int(self._region.get("h", 0)) / _ih) if _ih else 1.0
        except Exception:
            _fx = _fy = 1.0
        loaded = 0
        for field in OVERRIDE_FIELDS:
            try:
                box = calibration.get_row(self._region, field)
            except Exception as exc:
                log.debug("get_row(%s) failed: %s", field, exc)
                box = None
            if box is None:
                continue
            self._draw.set_box_for_field(field, {
                "x": int(round(int(box["x"]) * _fx)),
                "y": int(round(int(box["y"]) * _fy)),
                "w": int(round(int(box["w"]) * _fx)),
                "h": int(round(int(box["h"]) * _fy)),
            })
            loaded += 1
        self._refresh_box_list()
        if loaded == 0:
            self._status.showMessage(
                "No saved locks found for this region.", 4000,
            )
        else:
            self._status.showMessage(
                f"Loaded {loaded} box(es) from calibration. Adjust as "
                "needed and Save when ready.", 4000,
            )

    # ── Save ──

    def _on_save(self) -> None:
        if self._region is None:
            QMessageBox.warning(
                self, "Cannot save",
                "No HUD region is set yet. Use 'Set HUD scanning region' "
                "in the toolbox first.",
            )
            return
        # Collect every drawn box, persist each, then flip the region's
        # override flag. We persist BEFORE flipping the flag so a partial
        # failure doesn't leave the pipeline pointed at empty manual data.
        saved: dict[str, dict] = {}
        for field in OVERRIDE_FIELDS:
            box = self._draw.get_hud_box(field)
            if box is None:
                continue
            try:
                calibration.set_manual_override_box(
                    self._region, field, box,
                )
                saved[field] = box
            except Exception as exc:
                log.error(
                    "set_manual_override_box(%s) failed: %s",
                    field, exc, exc_info=True,
                )
                QMessageBox.critical(
                    self, "Save failed",
                    f"Could not persist override for "
                    f"{FIELD_LABELS.get(field, field)}: {exc}",
                )
                return

        if not saved:
            QMessageBox.warning(
                self, "No boxes drawn",
                "Draw at least one box before saving. Use Cancel to "
                "exit without enabling manual override.",
            )
            return

        try:
            calibration.set_manual_override_mode(self._region, True)
        except Exception as exc:
            log.error(
                "set_manual_override_mode(True) failed: %s",
                exc, exc_info=True,
            )
            QMessageBox.critical(
                self, "Save failed",
                f"Boxes were saved but the override flag could not be "
                f"set: {exc}\n\n"
                "Run the OCR pipeline once to verify; if the override "
                "is not active, re-open this dialog and Save again.",
            )
            return

        self.overrides_saved.emit(saved)
        self.accept()
