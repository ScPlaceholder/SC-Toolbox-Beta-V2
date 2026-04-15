"""Resource detail popup — floating window with pin/close.

Matches the Craft Database / Market Finder pin+close bubble pattern.
Up to 5 popups can be open at once; oldest unpinned is auto-evicted.
Shows resource name, rarity, and signal values for 1-6 rocks.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen, QGuiApplication
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget, QGridLayout,
)

from shared.qt.theme import P

TOOL_COLOR = "#33dd88"
POPUP_BRACKET_LEN = 18
MAX_OPEN_POPUPS = 5

RARITY_COLORS: dict[str, str] = {
    "Common":    "#8cc63f",
    "Uncommon":  "#00bcd4",
    "Rare":      "#ffc107",
    "Epic":      "#aa66ff",
    "Legendary": "#ff9800",
    "ROC":       "#33ccdd",
    "FPS":       "#44aaff",
    "Salvage":   "#66ccff",
}


def _pin_btn_qss(pinned: bool, accent: str) -> str:
    if pinned:
        return f"""
            QPushButton#modalPin {{
                background-color: rgba(51, 221, 136, 120);
                color: {P.bg_primary};
                border: 1px solid {accent};
                border-radius: 3px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                padding: 3px 12px; min-height: 0px;
            }}
            QPushButton#modalPin:hover {{
                background-color: rgba(51, 221, 136, 50);
                color: {accent};
                border-color: {accent};
            }}
        """
    return f"""
        QPushButton#modalPin {{
            background-color: transparent;
            color: {accent};
            border: 1px solid rgba(51, 221, 136, 60);
            border-radius: 3px;
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 3px 12px; min-height: 0px;
        }}
        QPushButton#modalPin:hover {{
            background-color: rgba(51, 221, 136, 60);
            color: {P.fg_bright};
            border-color: {accent};
        }}
    """


class _ModalCloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setObjectName("modalClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton#modalClose {{
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666;
                border: none;
                border-radius: 3px;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }}
            QPushButton#modalClose:hover {{
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }}
        """)


class ResourcePopup(QDialog):
    """Floating detail popup for a mining resource with pin/close and drag.

    Up to MAX_OPEN_POPUPS can be open at once. Oldest unpinned popup is
    auto-evicted when the limit is hit.
    """

    _open_dialogs: list["ResourcePopup"] = []
    _pinned_dialogs: list["ResourcePopup"] = []

    def __new__(cls, resource: dict, parent: Optional[QWidget] = None):
        """Return the existing popup for this resource if one is already open.

        Prevents the same resource being displayed in multiple bubbles.
        If a matching popup exists, it's raised to the top and returned
        instead of constructing a new instance.
        """
        name = (resource or {}).get("name", "")
        if name:
            for existing in list(cls._open_dialogs):
                if not existing.isVisible():
                    continue
                if existing._resource.get("name") == name:
                    existing.raise_()
                    existing.activateWindow()
                    # Mark as already-initialised so __init__ is a no-op
                    existing._skip_init = True
                    return existing
        instance = super().__new__(cls)
        instance._skip_init = False
        return instance

    def __init__(self, resource: dict, parent: Optional[QWidget] = None):
        # If __new__ returned an existing instance, skip re-initialising it
        if getattr(self, "_skip_init", False):
            return

        super().__init__(parent)
        self._resource = resource
        # Accept either a plain string rarity or a (sort_order, name) tuple
        raw_rarity = resource.get("_rarity_name") or resource.get("rarity", "")
        if isinstance(raw_rarity, tuple) and len(raw_rarity) >= 2:
            raw_rarity = raw_rarity[1]
        self._rarity = str(raw_rarity)
        self._accent = RARITY_COLORS.get(self._rarity, TOOL_COLOR)
        self._drag_pos: QPoint | None = None
        self._pinned = False

        self.setWindowTitle(resource.get("name", "Resource"))
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(320, 260)
        self.setMinimumSize(280, 220)

        # Enforce max popups before adding ourselves
        self._evict_oldest()
        ResourcePopup._open_dialogs.append(self)

        # Position: cascade slightly from parent center
        if parent:
            pg = parent.geometry()
            idx = len(ResourcePopup._open_dialogs) - 1
            cx = pg.x() + (pg.width() - self.width()) // 2 + idx * 26
            cy = pg.y() + (pg.height() - self.height()) // 2 + idx * 30

            screen = QGuiApplication.primaryScreen()
            if screen:
                sr = screen.availableGeometry()
                cx = max(sr.x(), min(cx, sr.right() - self.width()))
                cy = max(sr.y(), min(cy, sr.bottom() - self.height()))

            self.move(max(0, cx), max(0, cy))

        self._build()
        self.show()

    # ── Build ────────────────────────────────────────────────────────────

    def _build(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        frame = QWidget(self)
        frame.setStyleSheet("background-color: rgba(11, 14, 20, 230);")
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(0, 0, 0, 0)
        frame_lay.setSpacing(0)

        # ── Title bar
        title_bar = QWidget(frame)
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(f"background-color: {P.bg_header};")
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(12, 0, 4, 0)
        tb_lay.setSpacing(8)

        name = self._resource.get("name", "").upper()
        title_lbl = QLabel(name, title_bar)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace;"
            f"font-size: 11pt; font-weight: bold;"
            f"color: {self._accent}; letter-spacing: 2px; background: transparent;"
        )
        tb_lay.addWidget(title_lbl)
        tb_lay.addStretch(1)

        # Pin button
        self._pin_btn = QPushButton("Pin")
        self._pin_btn.setObjectName("modalPin")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setStyleSheet(_pin_btn_qss(False, self._accent))
        self._pin_btn.clicked.connect(self._toggle_pin)
        tb_lay.addWidget(self._pin_btn)

        # Close button
        close_btn = _ModalCloseBtn(title_bar)
        close_btn.clicked.connect(self.close)
        tb_lay.addWidget(close_btn)

        frame_lay.addWidget(title_bar)

        # ── Body
        body = QWidget(frame)
        body.setStyleSheet("background: transparent;")
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(16, 14, 16, 16)
        body_lay.setSpacing(10)

        # Rarity badge
        rarity_lbl = QLabel(self._rarity.upper(), body)
        rarity_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 9pt; font-weight: bold;
            color: {self._accent}; background: {P.bg_input};
            border: 1px solid {self._accent}; border-radius: 10px;
            padding: 3px 10px; letter-spacing: 2px;
        """)
        rarity_lbl.setAlignment(Qt.AlignCenter)
        rarity_lbl.setMaximumWidth(120)
        body_lay.addWidget(rarity_lbl, alignment=Qt.AlignLeft)

        # Section header
        sec_hdr = QLabel("SIGNAL VALUES", body)
        sec_hdr.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 9pt; font-weight: bold;
            color: {P.fg_dim}; background: transparent;
            letter-spacing: 2px; padding-top: 4px;
        """)
        body_lay.addWidget(sec_hdr)

        # Values grid — 3 columns, 2 rows for 1-6 rocks
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(6)

        for i, rocks in enumerate(range(1, 7)):
            val = self._resource.get(str(rocks), 0)
            if not val:
                continue

            row = i // 3
            col = i % 3

            cell = QWidget(body)
            cell.setStyleSheet(f"""
                background: {P.bg_input};
                border: 1px solid {P.border};
                border-radius: 3px;
            """)
            cell_lay = QVBoxLayout(cell)
            cell_lay.setContentsMargins(8, 4, 8, 6)
            cell_lay.setSpacing(2)

            rock_word = "ROCK" if rocks == 1 else "ROCKS"
            rock_lbl = QLabel(f"{rocks} {rock_word}", cell)
            rock_lbl.setStyleSheet(f"""
                font-family: Consolas, monospace;
                font-size: 7pt; font-weight: bold;
                color: {P.fg_dim}; background: transparent;
                border: none; letter-spacing: 1px;
            """)
            cell_lay.addWidget(rock_lbl)

            val_lbl = QLabel(f"{val:,}", cell)
            val_lbl.setStyleSheet(f"""
                font-family: Electrolize, Consolas, monospace;
                font-size: 12pt; font-weight: bold;
                color: {self._accent}; background: transparent;
                border: none;
            """)
            cell_lay.addWidget(val_lbl)

            grid.addWidget(cell, row, col)

        body_lay.addLayout(grid)
        body_lay.addStretch(1)

        frame_lay.addWidget(body, 1)
        outer.addWidget(frame)

    # ── Pin toggle ───────────────────────────────────────────────────────

    def _toggle_pin(self):
        if self._pinned:
            self._pinned = False
            self._pin_btn.setText("Pin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(False, self._accent))
            if self in ResourcePopup._pinned_dialogs:
                ResourcePopup._pinned_dialogs.remove(self)
        else:
            # Enforce max 5 pinned
            active_pins = [d for d in ResourcePopup._pinned_dialogs if d.isVisible()]
            if len(active_pins) >= MAX_OPEN_POPUPS:
                # Can't pin more — flash the button briefly
                self._pin_btn.setText("Max 5")
                from PySide6.QtCore import QTimer
                QTimer.singleShot(
                    1000,
                    lambda: self._pin_btn.setText("Pin") if not self._pinned else None,
                )
                return
            self._pinned = True
            self._pin_btn.setText("Unpin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(True, self._accent))
            ResourcePopup._pinned_dialogs.append(self)

    # ── Max popup enforcement ────────────────────────────────────────────

    @classmethod
    def _evict_oldest(cls):
        cls._open_dialogs = [d for d in cls._open_dialogs if d.isVisible()]
        cls._pinned_dialogs = [d for d in cls._pinned_dialogs if d.isVisible()]

        while len(cls._open_dialogs) >= MAX_OPEN_POPUPS:
            victim = None
            for d in cls._open_dialogs:
                if not d._pinned:
                    victim = d
                    break
            if victim is None:
                # All pinned — refuse to open more
                return
            victim.close()

    # ── Lifecycle ────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self in ResourcePopup._open_dialogs:
            ResourcePopup._open_dialogs.remove(self)
        if self in ResourcePopup._pinned_dialogs:
            ResourcePopup._pinned_dialogs.remove(self)
        super().closeEvent(event)

    # ── Paint: border + corner brackets ─────────────────────────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        edge = QColor(self._accent)
        edge.setAlpha(100)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        bl = POPUP_BRACKET_LEN
        bracket = QColor(self._accent)
        bracket.setAlpha(200)
        painter.setPen(QPen(bracket, 2))
        painter.drawLine(0, 0, bl, 0)
        painter.drawLine(0, 0, 0, bl)
        painter.drawLine(w - 1, 0, w - 1 - bl, 0)
        painter.drawLine(w - 1, 0, w - 1, bl)
        painter.drawLine(0, h - 1, bl, h - 1)
        painter.drawLine(0, h - 1, 0, h - 1 - bl)
        painter.drawLine(w - 1, h - 1, w - 1 - bl, h - 1)
        painter.drawLine(w - 1, h - 1, w - 1, h - 1 - bl)
        painter.end()

    # ── Drag support ─────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)

    @classmethod
    def close_all_unpinned(cls):
        """Close every popup that isn't pinned."""
        for d in list(cls._open_dialogs):
            if not d._pinned:
                d.close()
