"""Tutorial popup for Battle Buddy — matches the DPS Calculator format."""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QScrollArea, QWidget,
)

from ui.theme import (
    BG, BG2, BG3, BORDER, FG, FG_DIM, ACCENT, HEADER_BG,
    FONT_TITLE, FONT_BODY,
)

_SECTION = f"""
    font-family: {FONT_TITLE};
    font-size: 10pt; font-weight: bold;
    color: {ACCENT}; background: transparent;
    margin-top: 10px; margin-bottom: 4px;
"""

_BODY = f"""
    font-family: {FONT_BODY};
    font-size: 9pt; color: {FG};
    background: transparent;
    line-height: 1.5;
"""

_HINT = f"""
    font-family: {FONT_BODY};
    font-size: 8pt; color: {FG_DIM};
    background: transparent; font-style: italic;
"""


def _section(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_SECTION)
    lbl.setWordWrap(True)
    return lbl


def _body(text: str) -> QLabel:
    """A paragraph. *text* is rich text: names shown on the bar or in the options go in <b>."""
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(_BODY)
    lbl.setWordWrap(True)
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setTextFormat(Qt.RichText)
    lbl.setStyleSheet(_HINT)
    lbl.setWordWrap(True)
    return lbl


def _bullets(*lines: str) -> str:
    """Lines as a bulleted list inside a _body()."""
    return "<br>".join("•&nbsp; " + line for line in lines)


def _steps(*lines: str) -> str:
    """Lines as numbered steps inside a _body()."""
    return "<br>".join("%d.&nbsp; %s" % (i, line) for i, line in enumerate(lines, 1))


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_battle_buddy", "<shift>+8")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+8"


# Tab title -> the method that builds it, in the order shown.
_TAB_BUILDERS = {
    "Overview": "_tab_overview",
    "Weapons": "_tab_weapons",
    "Consumables": "_tab_consumables",
    "Options": "_tab_options",
}


def tutorial_text() -> str:
    """All the tutorial's text as one piece of markup (what the tests read). Needs a QApplication."""
    pages = [getattr(TutorialPopup, name)(None) for name in _TAB_BUILDERS.values()]
    return "<br>".join(lbl.text() for page in pages for lbl in page.findChildren(QLabel))


def _build_tab(widgets: list[QWidget]) -> QScrollArea:
    page = QWidget()
    page.setStyleSheet(f"background-color: {BG};")
    lay = QVBoxLayout(page)
    lay.setContentsMargins(14, 10, 14, 10)
    lay.setSpacing(2)
    for w in widgets:
        lay.addWidget(w)
    lay.addStretch(1)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setStyleSheet(f"""
        QScrollArea {{ background-color: {BG}; border: none; }}
        QScrollArea > QWidget > QWidget {{ background-color: {BG}; }}
    """)
    scroll.setWidget(page)
    return scroll


class TutorialPopup(QDialog):
    """Multi-tab tutorial popup for Battle Buddy.  Draggable."""

    _instance: "TutorialPopup | None" = None

    @classmethod
    def show_tutorial(cls, parent=None) -> "TutorialPopup":
        if cls._instance is None or not cls._instance.isVisible():
            cls._instance = cls(parent)
        cls._instance.show()
        cls._instance.raise_()
        cls._instance.activateWindow()
        return cls._instance

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedSize(520, 440)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {BG};
                border: 1px solid {ACCENT};
                border-radius: 4px;
            }}
        """)
        self._drag_pos = QPoint()
        self._dragging = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # Header (drag handle)
        hdr = QWidget(self)
        hdr.setFixedHeight(34)
        hdr.setCursor(QCursor(Qt.OpenHandCursor))
        hdr.setStyleSheet(f"background-color: {HEADER_BG}; border-bottom: 1px solid {BORDER};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(12, 0, 6, 0)

        title = QLabel("\U0001f4e1  Battle Buddy — Tutorial", hdr)
        title.setStyleSheet(f"""
            font-family: {FONT_TITLE}; font-size: 10pt;
            font-weight: bold; color: {ACCENT}; background: transparent;
        """)
        hdr_lay.addWidget(title)
        hdr_lay.addStretch(1)

        btn_close = QPushButton("\u2715", hdr)
        btn_close.setFixedSize(26, 22)
        btn_close.setCursor(QCursor(Qt.PointingHandCursor))
        btn_close.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {FG_DIM}; border: none; font-size: 11pt; }}
            QPushButton:hover {{ color: #ff5533; }}
        """)
        btn_close.clicked.connect(self.close)
        hdr_lay.addWidget(btn_close)
        self._hdr = hdr
        root.addWidget(hdr)

        # Tabs
        tabs = QTabWidget(self)
        tabs.setStyleSheet(f"""
            QTabBar::tab {{
                background-color: {BG2}; color: {FG_DIM};
                border: none; border-bottom: 2px solid transparent;
                padding: 5px 10px;
                font-family: {FONT_BODY}; font-size: 8pt; font-weight: bold;
            }}
            QTabBar::tab:hover {{ color: {FG}; background-color: {BG3}; }}
            QTabBar::tab:selected {{
                color: {ACCENT}; border-bottom-color: {ACCENT};
                background-color: {BG};
            }}
            QTabWidget::pane {{ background-color: {BG}; border: none; }}
        """)

        for title, builder in _TAB_BUILDERS.items():
            tabs.addTab(getattr(self, builder)(), title)
        tabs.setDocumentMode(False)
        root.addWidget(tabs, 1)

        # Force dark background on tab content area
        self.setStyleSheet(self.styleSheet() + f"""
            QScrollArea {{ background-color: {BG}; }}
            QScrollArea > QWidget > QWidget {{ background-color: {BG}; }}
            QTabWidget::pane {{ background-color: {BG}; border: none; }}
        """)

    # ── Tab content ──────────────────────────────────────────────────────────

    def _tab_overview(self) -> QScrollArea:
        return _build_tab([
            _section("What it is for"),
            _body(
                "Battle Buddy is a small bar that shows what you are carrying "
                "on foot: your weapons and spare magazines, your medical pens "
                "and your grenades. It works them out from Star Citizen's "
                "Game.log file. It does not read or change the game itself."
            ),

            _section("Getting started"),
            _body(_steps(
                "Open Battle Buddy. The bar appears straight away.",
                "Start Star Citizen and load into the universe. The bar fills "
                "in as the game puts your gear on you.",
                "If the bar stays empty, open the options with the ⚙ "
                "button and check the Game.log path.",
            )),
            _hint("Each time you join the universe the bar starts again from nothing and "
                  "rebuilds from the log."),

            _section("The bar"),
            _body(_bullets(
                "Drag the title, <b>BATTLE BUDDY</b>, to move the bar. It "
                "remembers where you leave it.",
                "The slider sets how see-through the bar is, from 20% to 100%.",
                "<b>? Tutorial</b> opens this window. ⚙ opens the options.",
                "✕ hides the bar. Battle Buddy keeps running and keeps "
                "following the log.",
            )),

            _section("Clicking through it"),
            _body(
                "Tick <b>Ignore mouse hovering on Battle Buddy</b> and your "
                "clicks go through the bar to the game. The tick box itself "
                "stays clickable, so you can turn it off again."
            ),

            _section("Hotkey"),
            _body(
                f"The launcher's hotkey for this tool is {_hotkey()}. It shows "
                "and hides the bar."
            ),
        ])

    def _tab_weapons(self) -> QScrollArea:
        return _build_tab([
            _section("The five cards"),
            _body(
                "<b>PRIMARY 1</b>, <b>PRIMARY 2</b>, <b>SIDEARM</b>, "
                "<b>UTILITY</b> and <b>MELEE</b>. A slot with nothing in it "
                "reads <b>— Empty —</b>."
            ),

            _section("What a card shows"),
            _body(_bullets(
                "The weapon's name and its type, such as rifle or pistol.",
                "Its ammo type, in colour: energy is cyan, ballistic is amber, "
                "distortion is purple.",
                "One pip for each spare magazine, up to eight, and the count "
                "beside them.",
            )),
            _hint("The type comes from the weapon's name in the log. One the tool does not "
                  "recognise shows as Weapon."),

            _section("Reloading"),
            _body(
                "When you load a spare magazine into the weapon, the count "
                "goes down by one."
            ),

            _section("Utility"),
            _body(
                "For a multitool, the <b>UTILITY</b> card shows the "
                "attachment that is fitted, such as Tractor Beam, Mining or "
                "Salvage, in place of an ammo type."
            ),
        ])

    def _tab_consumables(self) -> QScrollArea:
        return _build_tab([
            _section("Pens"),
            _body(
                "Your pens are listed by name under <b>MED</b>, <b>OXY</b>, "
                "<b>STIM</b>, <b>DETOX</b> and <b>OTHER</b>, with a count when "
                "you carry more than one of a kind. A group is shown only "
                "while you carry something in it."
            ),

            _section("Grenades"),
            _body(
                "Grenades are listed under <b>GREN</b> by type, with a count."
            ),

            _section("Using one"),
            _body(
                "A pen or a grenade comes off the list when you take it in "
                "your hand."
            ),

            _section("Changing armour"),
            _body(
                "Taking armour off removes its pens together. When three or "
                "more pens go within a few seconds, the bar clears all of them "
                "at once."
            ),
        ])

    def _tab_options(self) -> QScrollArea:
        return _build_tab([
            _section("Opening the options"),
            _body(
                "Press ⚙ on the bar. <b>Save</b> applies your changes at "
                "once. <b>Cancel</b> closes without changing anything."
            ),

            _section("Game.log path"),
            _body(
                "Under <b>GAME.LOG PATH</b> is the file Battle Buddy follows. "
                "It finds your Star Citizen folder by itself: first the one "
                "the launcher knows, then a search of your drives. If it has "
                "the wrong file, press <b>Browse…</b> and pick Game.log "
                "in your LIVE folder."
            ),

            _section("Layout"),
            _body(
                "Under <b>HUD ORIENTATION</b>, choose "
                "<b>Horizontal (wide bar)</b> or "
                "<b>Vertical (narrow column)</b>."
            ),

            _section("Opacity"),
            _body(
                "Opacity is not in the options. Use the slider on the bar."
            ),
        ])


    # ── Drag-to-move ─────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self._hdr.geometry().contains(event.position().toPoint()):
            self._dragging = True
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            self._hdr.setCursor(QCursor(Qt.ClosedHandCursor))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._dragging and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._dragging:
            self._dragging = False
            self._hdr.setCursor(QCursor(Qt.OpenHandCursor))
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def show_relative_to(self, widget: QWidget) -> None:
        pos = widget.mapToGlobal(QPoint(0, widget.height() + 4))
        x = max(0, pos.x() - self.width() + widget.width())
        self.move(x, pos.y())
        self.show()
        self.raise_()
        self.activateWindow()
