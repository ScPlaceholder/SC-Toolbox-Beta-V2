"""Base class for modal dialogs — PySide6 QDialog with title bar, pin, and close."""
from __future__ import annotations
from typing import Optional

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QPainter, QColor, QPen
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget, QLabel, QPushButton,
)

from shared.qt.theme import P

MAX_OPEN_POPUPS = 5


def _pin_btn_qss(pinned: bool, accent: str = "") -> str:
    """Stylesheet for the pin/unpin button (matches Trade Hub pattern)."""
    c = accent or P.tool_mission
    if pinned:
        return f"""
            QPushButton#modalPin {{
                background-color: rgba(51, 221, 136, 80);
                color: {P.bg_primary};
                border: 1px solid {c};
                border-radius: 3px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                padding: 3px 12px; min-height: 0px;
            }}
            QPushButton#modalPin:hover {{
                background-color: rgba(51, 221, 136, 50);
                color: {c};
                border-color: {c};
            }}
        """
    return f"""
        QPushButton#modalPin {{
            background-color: rgba(51, 221, 136, 30);
            color: {c};
            border: 1px solid rgba(51, 221, 136, 60);
            border-radius: 3px;
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 3px 12px; min-height: 0px;
        }}
        QPushButton#modalPin:hover {{
            background-color: rgba(51, 221, 136, 60);
            color: {P.fg_bright};
            border-color: {c};
        }}
    """


class _ModalCloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setObjectName("modalClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("""
            QPushButton#modalClose {
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
            }
            QPushButton#modalClose:hover {
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }
        """)


class ModalBase(QDialog):
    """Base modal with title bar, pin/unpin, close button, and scrollable body.

    - Maximum of MAX_OPEN_POPUPS popups at once; opening a new one closes
      the oldest *unpinned* popup.  Pinned popups are never auto-closed.
    - Pin button keeps the popup alive even when another card is clicked.
    """

    _open_dialogs: list = []     # all open (oldest first)
    _pinned_dialogs: list = []   # only pinned

    def __init__(
        self,
        parent: Optional[QWidget],
        title: str,
        width: int = 650,
        height: int = 550,
        accent: str = "",
    ):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setWindowFlags(
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(width, height)
        self.setMinimumSize(400, 300)

        self._accent = accent or P.tool_mission
        self._drag_pos = None
        self._pinned = False

        # ── Enforce max popups ──
        self._evict_oldest()
        ModalBase._open_dialogs.append(self)

        # Position near parent
        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - width) // 2
            y = pg.y() + (pg.height() - height) // 2
            self.move(max(0, x), max(0, y))

        # Main layout
        self._outer_layout = QVBoxLayout(self)
        self._outer_layout.setContentsMargins(1, 1, 1, 1)
        self._outer_layout.setSpacing(0)

        # Container frame
        self._frame = QWidget(self)
        self._frame.setStyleSheet("background-color: rgba(11, 14, 20, 230);")
        frame_layout = QVBoxLayout(self._frame)
        frame_layout.setContentsMargins(0, 0, 0, 0)
        frame_layout.setSpacing(0)

        # ── Title bar ──
        title_bar = QWidget(self._frame)
        title_bar.setFixedHeight(34)
        title_bar.setStyleSheet(f"background-color: {P.bg_header};")
        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(12, 0, 4, 0)
        tb_layout.setSpacing(8)

        title_lbl = QLabel(title.upper(), title_bar)
        title_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 11pt; font-weight: bold;
            color: {self._accent};
            letter-spacing: 2px;
            background: transparent;
        """)
        tb_layout.addWidget(title_lbl)
        tb_layout.addStretch(1)

        # Pin button
        self._pin_btn = QPushButton("Pin")
        self._pin_btn.setObjectName("modalPin")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setStyleSheet(_pin_btn_qss(False, self._accent))
        self._pin_btn.clicked.connect(self._toggle_pin)
        tb_layout.addWidget(self._pin_btn)

        # Close button
        close_btn = _ModalCloseBtn(title_bar)
        close_btn.clicked.connect(self.close)
        tb_layout.addWidget(close_btn)

        frame_layout.addWidget(title_bar)

        # Body area for subclasses
        self._body_layout = QVBoxLayout()
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        frame_layout.addLayout(self._body_layout, 1)

        self._outer_layout.addWidget(self._frame)

    # ── Pin / Unpin ──

    def _toggle_pin(self):
        if self._pinned:
            self._pinned = False
            self._pin_btn.setText("Pin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(False, self._accent))
            if self in ModalBase._pinned_dialogs:
                ModalBase._pinned_dialogs.remove(self)
        else:
            self._pinned = True
            self._pin_btn.setText("Unpin")
            self._pin_btn.setStyleSheet(_pin_btn_qss(True, self._accent))
            ModalBase._pinned_dialogs.append(self)

    # ── Max popup enforcement ──

    @classmethod
    def _evict_oldest(cls):
        """Close the oldest unpinned popup if we're at the limit."""
        # Clean up any already-closed dialogs first
        cls._open_dialogs = [d for d in cls._open_dialogs if d.isVisible()]
        cls._pinned_dialogs = [d for d in cls._pinned_dialogs if d.isVisible()]

        while len(cls._open_dialogs) >= MAX_OPEN_POPUPS:
            # Find oldest unpinned
            victim = None
            for d in cls._open_dialogs:
                if not d._pinned:
                    victim = d
                    break
            if victim is None:
                # All are pinned — close the oldest pinned one
                victim = cls._open_dialogs[0]
            victim.close()

    # ── Public API ──

    @property
    def body_layout(self) -> QVBoxLayout:
        """Subclasses add their content here."""
        return self._body_layout

    def make_scrollable_body(self) -> tuple:
        """Create a scrollable body area.

        Returns (scroll_area, inner_widget, inner_layout).
        """
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("""
            QScrollArea {
                background: transparent;
                border: none;
            }
        """)

        inner = QWidget()
        inner.setStyleSheet("background: transparent;")
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(16, 12, 16, 16)
        inner_layout.setSpacing(4)
        scroll.setWidget(inner)

        self._body_layout.addWidget(scroll, 1)
        return scroll, inner, inner_layout

    # ── Lifecycle ──

    def closeEvent(self, event):
        if self in ModalBase._open_dialogs:
            ModalBase._open_dialogs.remove(self)
        if self in ModalBase._pinned_dialogs:
            ModalBase._pinned_dialogs.remove(self)
        super().closeEvent(event)

    # ── Paint ──

    def paintEvent(self, event):
        """Draw border glow."""
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        # Border
        edge = QColor(self._accent)
        edge.setAlpha(100)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        # Corner brackets
        bl = 14
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

    # ── Drag support ──

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
