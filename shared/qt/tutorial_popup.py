"""A tabbed tutorial popup, for tools that have no tutorial window of their own yet.

It is the Craft Database / Star Map tutorial popup with the tool's name, colour
and text passed in: a frameless, draggable, always-on-top dialog with corner
brackets and one scrolling rich-text page per tab. The tools that already had a
tutorial keep their own windows; this one exists so that the tools that got
their first tutorial on 2026-10-05 (Everything Finder, Mouse Blocker, Toolbox
Assistant / Suit Mk2) did not each need another copy of the same 150 lines.

    from shared.qt.tutorial_popup import TutorialPopup, Tab
    TutorialPopup.open("mouse_blocker", parent, title="MOUSE BLOCKER", accent="#ff3355",
                       tabs=[Tab("Getting Started", html), ...])

One window per *key*: opening it again while it is showing brings that window
forward instead of making a second one. A Tab may carry one button, drawn under
its text (the Everything Finder uses it to open the Star Map's own tutorial).

Writing rule for the text, which tests rely on: a name the user can read in the
tool (a button, a tab, a heading) goes in <b>...</b>. See shared/tutorial_guard.py.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

SIZE = (620, 500)
MIN_SIZE = (480, 360)
BRACKET_LEN = 14
_CLOSE_BG = "rgba(255, 60, 60, 0.15)"
_CLOSE_FG = "#cc6666"
_CLOSE_HOVER_BG = "rgba(220, 50, 50, 0.85)"

_FONT = "font-family: Electrolize, Consolas;"
BODY = f"font-family: Consolas; color: {P.fg}; font-size: 9pt; line-height: 1.5;"
DIM = f"color: {P.fg_dim};"
YELLOW = f"color: {P.yellow};"
GREEN = f"color: {P.green};"


def h3(text: str, color: str) -> str:
    return f'<h3 style="{_FONT} color: {color};">{text}</h3>'


def h4(text: str, color: str) -> str:
    return f'<h4 style="{_FONT} color: {color};">{text}</h4>'


def page(body: str) -> str:
    """Wrap a tab's markup in the body font."""
    return f'<div style="{BODY}">{body}</div>'


class Tab:
    """One tab: its title, its markup, and optionally one button under the text."""

    def __init__(self, title: str, html: str, button: str = "",
                 on_button: Optional[Callable[[], None]] = None) -> None:
        self.title = title
        self.html = html
        self.button = button
        self.on_button = on_button


class _CloseBtn(QPushButton):
    def __init__(self, parent=None):
        super().__init__("x", parent)
        self.setObjectName("tutClose")
        self.setFixedSize(32, 28)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton#tutClose {{
                background: {_CLOSE_BG};
                color: {_CLOSE_FG};
                border: none;
                border-radius: 3px;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }}
            QPushButton#tutClose:hover {{
                background-color: {_CLOSE_HOVER_BG};
                color: #ffffff;
            }}
        """)


class TutorialPopup(QDialog):
    """See the module docstring. Build it with TutorialPopup.open(...)."""

    _open: Dict[str, "TutorialPopup"] = {}

    @classmethod
    def open(cls, key: str, parent: Optional[QWidget], title: str, accent: str,
             tabs: Sequence[Tab], show: bool = True) -> "TutorialPopup":
        """The popup for *key*, made if it is not showing, and brought to the front."""
        popup = cls._open.get(key)
        if popup is not None:
            try:
                if popup.isVisible():
                    popup.raise_()
                    popup.activateWindow()
                    return popup
            except RuntimeError:            # its window was destroyed with its parent
                pass
        popup = cls(key, parent, title, accent, tabs)
        cls._open[key] = popup
        if show:
            popup.show()
            popup.raise_()
        return popup

    def __init__(self, key: str, parent: Optional[QWidget], title: str, accent: str,
                 tabs: Sequence[Tab]) -> None:
        super().__init__(parent)
        self._key = key
        self._accent = accent
        self._drag_pos: Optional[QPoint] = None
        self.tab_widget: Optional[QTabWidget] = None

        self.setWindowTitle(title.title() + " — Tutorial")
        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.resize(*SIZE)
        self.setMinimumSize(*MIN_SIZE)
        if parent is not None:
            pg = parent.geometry()
            self.move(max(0, pg.x() + (pg.width() - SIZE[0]) // 2),
                      max(0, pg.y() + (pg.height() - SIZE[1]) // 2))
        self._build(title, list(tabs))

    # ── build ────────────────────────────────────────────────────────────
    def _build(self, title: str, tabs: List[Tab]) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        frame = QWidget(self)
        frame.setStyleSheet("background-color: rgba(11, 14, 20, 230);")
        frame_lay = QVBoxLayout(frame)
        frame_lay.setContentsMargins(0, 0, 0, 0)
        frame_lay.setSpacing(0)

        bar = QWidget(frame)
        bar.setFixedHeight(34)
        bar.setStyleSheet(f"background-color: {P.bg_header};")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(12, 0, 4, 0)
        bar_lay.setSpacing(8)
        title_lbl = QLabel(title.upper() + "  —  TUTORIAL", bar)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace;"
            f"font-size: 11pt; font-weight: bold;"
            f"color: {self._accent}; letter-spacing: 2px; background: transparent;")
        bar_lay.addWidget(title_lbl)
        bar_lay.addStretch(1)
        close_btn = _CloseBtn(bar)
        close_btn.clicked.connect(self.close)
        bar_lay.addWidget(close_btn)
        frame_lay.addWidget(bar)

        self.tab_widget = QTabWidget()
        self.tab_widget.setStyleSheet(f"""
            QTabWidget::pane {{
                border: none;
                background: transparent;
            }}
            QTabBar::tab {{
                background: {P.bg_secondary};
                color: {P.fg_dim};
                border: none;
                padding: 6px 14px;
                font-family: Consolas;
                font-size: 9pt;
                font-weight: bold;
            }}
            QTabBar::tab:selected {{
                background: #0e1e26;
                color: {self._accent};
            }}
            QTabBar::tab:hover:!selected {{
                color: {P.fg};
            }}
        """)
        for tab in tabs:
            # A lone & in a tab title is Qt's shortcut mark and is not drawn.
            self.tab_widget.addTab(self._make_tab(tab), tab.title.replace("&", "&&"))
        frame_lay.addWidget(self.tab_widget, 1)
        outer.addWidget(frame)

    def _make_tab(self, tab: Tab) -> QScrollArea:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {P.scrollbar_bg}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {P.scrollbar_handle}; min-height: 20px; border-radius: 3px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        """)
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(body)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lbl = QLabel(tab.html)
        lbl.setObjectName("tutText")
        lbl.setWordWrap(True)
        lbl.setTextFormat(Qt.RichText)
        lbl.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lbl.setStyleSheet(f"background: transparent; padding: 16px; color: {P.fg};")
        lbl.setOpenExternalLinks(True)
        lay.addWidget(lbl)
        if tab.button:
            btn = QPushButton(tab.button)
            btn.setObjectName("tutAction")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton#tutAction {{
                    font-family: Consolas; font-size: 9pt; font-weight: bold;
                    color: {self._accent}; background: transparent;
                    border: 1px solid {self._accent}; border-radius: 3px;
                    padding: 4px 12px; margin: 0px 16px 16px 16px;
                }}
                QPushButton#tutAction:hover {{ background: rgba(255, 255, 255, 0.08); }}
            """)
            if tab.on_button is not None:
                btn.clicked.connect(lambda _checked=False, fn=tab.on_button: fn())
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(btn)
            row.addStretch(1)
            lay.addLayout(row)
        lay.addStretch(1)
        scroll.setWidget(body)
        return scroll

    def tab_text(self, index: int) -> str:
        """The markup of tab *index* (for tests)."""
        return self.tab_widget.widget(index).findChild(QLabel, "tutText").text()

    # ── border and corner brackets ───────────────────────────────────────
    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        edge = QColor(self._accent)
        edge.setAlpha(100)
        painter.setPen(QPen(edge, 1))
        painter.drawRect(0, 0, w - 1, h - 1)
        bl = BRACKET_LEN
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

    # ── drag anywhere to move ────────────────────────────────────────────
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

    def closeEvent(self, event):
        if TutorialPopup._open.get(self._key) is self:
            del TutorialPopup._open[self._key]
        super().closeEvent(event)
