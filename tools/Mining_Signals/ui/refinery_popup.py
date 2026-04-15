"""Refinery order detail popup — floating window with pin/close + live countdown.

Follows the ResourcePopup pattern: frameless, always-on-top, draggable,
max 5 open, auto-evict oldest unpinned. Adds a live countdown timer and
editable order name.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

from PySide6.QtCore import Qt, QPoint, QTimer, Signal
from PySide6.QtGui import QPainter, QColor, QPen, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QVBoxLayout, QWidget, QGridLayout,
)

from shared.qt.theme import P

if TYPE_CHECKING:
    from services.refinery_orders import RefineryOrder, RefineryOrderStore

ACCENT = "#33dd88"
MAX_OPEN_POPUPS = 5
BRACKET_LEN = 18


def _pin_qss(pinned: bool) -> str:
    if pinned:
        return f"""
            QPushButton#refPin {{
                background-color: rgba(51, 221, 136, 120);
                color: {P.bg_primary};
                border: 1px solid {ACCENT};
                border-radius: 3px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                padding: 3px 12px; min-height: 0px;
            }}
            QPushButton#refPin:hover {{
                background-color: rgba(51, 221, 136, 50);
                color: {ACCENT};
            }}
        """
    return f"""
        QPushButton#refPin {{
            background-color: transparent;
            color: {ACCENT};
            border: 1px solid rgba(51, 221, 136, 60);
            border-radius: 3px;
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 3px 12px; min-height: 0px;
        }}
        QPushButton#refPin:hover {{
            background-color: rgba(51, 221, 136, 60);
            color: {P.fg_bright};
        }}
    """


class _CloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666; border: none; border-radius: 3px;
                font-family: Consolas; font-size: 13pt; font-weight: bold;
                padding: 0px; margin: 2px; min-height: 0px;
            }}
            QPushButton:hover {{
                background-color: rgba(220, 50, 50, 0.85); color: #ffffff;
            }}
        """)


class RefineryOrderPopup(QDialog):
    """Floating detail popup for a refinery order with live countdown."""

    _open_dialogs: list["RefineryOrderPopup"] = []
    _pinned_dialogs: list["RefineryOrderPopup"] = []

    order_changed = Signal()  # emitted when name edited

    def __new__(
        cls,
        order: "RefineryOrder",
        store: "RefineryOrderStore",
        parent: Optional[QWidget] = None,
    ):
        oid = order.id
        for existing in list(cls._open_dialogs):
            if not existing.isVisible():
                continue
            if existing._order.id == oid:
                existing.raise_()
                existing.activateWindow()
                existing._skip_init = True
                return existing
        instance = super().__new__(cls)
        instance._skip_init = False
        return instance

    def __init__(
        self,
        order: "RefineryOrder",
        store: "RefineryOrderStore",
        parent: Optional[QWidget] = None,
    ):
        if getattr(self, "_skip_init", False):
            return

        super().__init__(parent)
        self._order = order
        self._store = store
        self._drag_pos: QPoint | None = None
        self._pinned = False

        self.setWindowTitle(order.name)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(340, 320)
        self.setMinimumSize(300, 280)

        self._evict_oldest()
        RefineryOrderPopup._open_dialogs.append(self)

        if parent:
            pg = parent.geometry()
            idx = len(RefineryOrderPopup._open_dialogs) - 1
            cx = pg.x() + (pg.width() - self.width()) // 2 + idx * 26
            cy = pg.y() + (pg.height() - self.height()) // 2 + idx * 30
            screen = QGuiApplication.primaryScreen()
            if screen:
                sr = screen.availableGeometry()
                cx = max(sr.x(), min(cx, sr.right() - self.width()))
                cy = max(sr.y(), min(cy, sr.bottom() - self.height()))
            self.move(max(0, cx), max(0, cy))

        self._build()

        # Live countdown timer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._update_countdown)
        self._timer.start(1000)

        self.show()

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        frame = QWidget(self)
        frame.setObjectName("refFrame")
        frame.setStyleSheet(f"""
            QWidget#refFrame {{
                background-color: rgba(11, 14, 20, 230);
                border: 1px solid rgba(51, 221, 136, 100);
                border-radius: 4px;
            }}
        """)
        outer.addWidget(frame)

        main = QVBoxLayout(frame)
        main.setContentsMargins(12, 8, 12, 12)
        main.setSpacing(6)

        # ── Title bar ──
        title_row = QHBoxLayout()
        title_row.setSpacing(4)

        self._name_label = QLabel(self._order.name, frame)
        self._name_label.setStyleSheet(
            f"font-family: Consolas; font-size: 10pt; font-weight: bold; "
            f"color: {ACCENT}; background: transparent;"
        )
        self._name_label.setCursor(Qt.PointingHandCursor)
        self._name_label.setToolTip("Double-click to rename")
        self._name_label.mouseDoubleClickEvent = lambda e: self._start_rename()
        title_row.addWidget(self._name_label, 1)

        # Name edit (hidden by default)
        self._name_edit = QLineEdit(self._order.name, frame)
        self._name_edit.setStyleSheet(
            f"font-family: Consolas; font-size: 10pt; color: {ACCENT}; "
            f"background: {P.bg_card}; border: 1px solid {ACCENT}; "
            f"border-radius: 2px; padding: 1px 4px;"
        )
        self._name_edit.setVisible(False)
        self._name_edit.returnPressed.connect(self._finish_rename)
        self._name_edit.editingFinished.connect(self._finish_rename)
        title_row.addWidget(self._name_edit, 1)

        self._pin_btn = QPushButton("Pin", frame)
        self._pin_btn.setObjectName("refPin")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setStyleSheet(_pin_qss(False))
        self._pin_btn.clicked.connect(self._toggle_pin)
        title_row.addWidget(self._pin_btn)

        close_btn = _CloseBtn(frame)
        close_btn.clicked.connect(self.close)
        title_row.addWidget(close_btn)

        main.addLayout(title_row)

        _hdr = (
            f"font-family: Consolas; font-size: 7pt; font-weight: bold; "
            f"color: {P.fg_dim}; background: transparent; padding-top: 4px;"
        )
        _val = (
            f"font-family: Consolas; font-size: 9pt; "
            f"color: {P.fg}; background: transparent;"
        )

        # ── Status + Station ──
        status_row = QHBoxLayout()
        is_complete = self._order.status == "complete"
        status_text = "COMPLETE" if is_complete else "IN PROCESS"
        status_color = P.fg if is_complete else ACCENT
        status_lbl = QLabel(status_text, frame)
        status_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; font-weight: bold; "
            f"color: {status_color}; background: transparent;"
        )
        status_row.addWidget(status_lbl)
        status_row.addStretch(1)
        station_lbl = QLabel(self._order.station or "—", frame)
        station_lbl.setStyleSheet(_val)
        status_row.addWidget(station_lbl)
        main.addLayout(status_row)

        # ── Countdown ──
        lbl = QLabel("TIME REMAINING", frame)
        lbl.setStyleSheet(_hdr)
        main.addWidget(lbl)

        self._countdown_label = QLabel(self._order.time_remaining_str(), frame)
        self._countdown_label.setStyleSheet(
            f"font-family: Consolas; font-size: 16pt; font-weight: bold; "
            f"color: {ACCENT}; background: transparent;"
        )
        main.addWidget(self._countdown_label)

        # ── Commodities ──
        if self._order.commodities:
            lbl2 = QLabel("COMMODITIES", frame)
            lbl2.setStyleSheet(_hdr)
            main.addWidget(lbl2)

            # Column headers
            col_hdr_style = (
                f"font-family: Consolas; font-size: 7pt; font-weight: bold; "
                f"color: {P.fg_dim}; background: transparent;"
            )
            grid = QGridLayout()
            grid.setSpacing(2)
            for ci, header_text in enumerate(["Material", "Quality", "Qty", "Yield"]):
                h = QLabel(header_text, frame)
                h.setStyleSheet(col_hdr_style)
                if ci >= 1:
                    h.setAlignment(Qt.AlignRight)
                grid.addWidget(h, 0, ci)

            for i, c in enumerate(self._order.commodities):
                row = i + 1
                name_lbl = QLabel(c.get("name", "?"), frame)
                name_lbl.setStyleSheet(_val)
                grid.addWidget(name_lbl, row, 0)

                qual_lbl = QLabel(str(c.get("quality", "—")), frame)
                qual_lbl.setStyleSheet(_val)
                qual_lbl.setAlignment(Qt.AlignRight)
                grid.addWidget(qual_lbl, row, 1)

                qty_lbl = QLabel(str(c.get("qty", "—")), frame)
                qty_lbl.setStyleSheet(_val)
                qty_lbl.setAlignment(Qt.AlignRight)
                grid.addWidget(qty_lbl, row, 2)

                scu_lbl = QLabel(f"{c.get('scu', 0)} cSCU", frame)
                scu_lbl.setStyleSheet(
                    f"font-family: Consolas; font-size: 9pt; "
                    f"color: {ACCENT}; background: transparent;"
                )
                scu_lbl.setAlignment(Qt.AlignRight)
                grid.addWidget(scu_lbl, row, 3)
            main.addLayout(grid)

        # ── Details ──
        lbl3 = QLabel("DETAILS", frame)
        lbl3.setStyleSheet(_hdr)
        main.addWidget(lbl3)

        details = []
        if self._order.method:
            details.append(f"Method: {self._order.method}")
        if self._order.cost:
            details.append(f"Cost: {self._order.cost:,.0f} aUEC")
        if self._order.submitted_at:
            sub = self._order.submitted_at.replace("T", " ")[:16]
            details.append(f"Submitted: {sub}")
        if self._order.expected_completion:
            exp = self._order.expected_completion.replace("T", " ")[:16]
            details.append(f"Expected: {exp}")
        if self._order.completed_at:
            comp = self._order.completed_at.replace("T", " ")[:16]
            details.append(f"Completed: {comp}")

        for d in details:
            dl = QLabel(d, frame)
            dl.setStyleSheet(_val)
            main.addWidget(dl)

        main.addStretch(1)

    def _start_rename(self):
        self._name_edit.setText(self._order.name)
        self._name_label.setVisible(False)
        self._name_edit.setVisible(True)
        self._name_edit.setFocus()
        self._name_edit.selectAll()

    def _finish_rename(self):
        if not self._name_edit.isVisible():
            return
        new_name = self._name_edit.text().strip()
        self._name_edit.setVisible(False)
        self._name_label.setVisible(True)
        if new_name and new_name != self._order.name:
            self._store.rename_order(self._order.id, new_name)
            self._name_label.setText(new_name)
            self.order_changed.emit()

    def _toggle_pin(self):
        self._pinned = not self._pinned
        self._pin_btn.setText("Unpin" if self._pinned else "Pin")
        self._pin_btn.setStyleSheet(_pin_qss(self._pinned))
        if self._pinned:
            if self not in RefineryOrderPopup._pinned_dialogs:
                RefineryOrderPopup._pinned_dialogs.append(self)
        else:
            if self in RefineryOrderPopup._pinned_dialogs:
                RefineryOrderPopup._pinned_dialogs.remove(self)

    def _update_countdown(self):
        text = self._order.time_remaining_str()
        self._countdown_label.setText(text)
        if self._order.status == "complete":
            self._countdown_label.setStyleSheet(
                f"font-family: Consolas; font-size: 16pt; font-weight: bold; "
                f"color: {P.fg}; background: transparent;"
            )
        elif self._order.time_remaining_seconds() <= 0:
            self._countdown_label.setStyleSheet(
                f"font-family: Consolas; font-size: 16pt; font-weight: bold; "
                f"color: #ffc107; background: transparent;"
            )

    @classmethod
    def _evict_oldest(cls):
        cls._open_dialogs = [d for d in cls._open_dialogs if d.isVisible()]
        while len(cls._open_dialogs) >= MAX_OPEN_POPUPS:
            for d in cls._open_dialogs:
                if d not in cls._pinned_dialogs:
                    d.close()
                    cls._open_dialogs.remove(d)
                    break
            else:
                break

    def closeEvent(self, event):
        if self._timer:
            self._timer.stop()
        if self in RefineryOrderPopup._open_dialogs:
            RefineryOrderPopup._open_dialogs.remove(self)
        if self in RefineryOrderPopup._pinned_dialogs:
            RefineryOrderPopup._pinned_dialogs.remove(self)
        super().closeEvent(event)

    # ── Drag ──

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    # ── Paint ──

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        pen = QPen(QColor(ACCENT))
        pen.setWidth(1)

        # Corner brackets
        bpen = QPen(QColor(51, 221, 136, 200))
        bpen.setWidth(2)
        painter.setPen(bpen)
        bl = BRACKET_LEN
        # Top-left
        painter.drawLine(r.left(), r.top(), r.left() + bl, r.top())
        painter.drawLine(r.left(), r.top(), r.left(), r.top() + bl)
        # Top-right
        painter.drawLine(r.right(), r.top(), r.right() - bl, r.top())
        painter.drawLine(r.right(), r.top(), r.right(), r.top() + bl)
        # Bottom-left
        painter.drawLine(r.left(), r.bottom(), r.left() + bl, r.bottom())
        painter.drawLine(r.left(), r.bottom(), r.left(), r.bottom() - bl)
        # Bottom-right
        painter.drawLine(r.right(), r.bottom(), r.right() - bl, r.bottom())
        painter.drawLine(r.right(), r.bottom(), r.right(), r.bottom() - bl)

        painter.end()
