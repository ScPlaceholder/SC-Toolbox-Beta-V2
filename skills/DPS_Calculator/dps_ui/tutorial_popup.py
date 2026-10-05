"""Tutorial popup for the DPS Calculator."""
from __future__ import annotations

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QTabWidget, QScrollArea, QWidget,
)

from dps_ui.constants import (
    BG, BG2, BG3, BG4, BORDER, FG, FG_DIM, FG_DIMMER, ACCENT,
    GREEN, YELLOW, ORANGE, CYAN, HEADER_BG,
)


_SECTION = f"""
    font-family: Electrolize, Consolas, monospace;
    font-size: 10pt; font-weight: bold;
    color: {ACCENT}; background: transparent;
    margin-top: 10px; margin-bottom: 4px;
"""

_BODY = f"""
    font-family: Consolas, monospace;
    font-size: 9pt; color: {FG};
    background: transparent;
    line-height: 1.5;
"""

_HINT = f"""
    font-family: Consolas, monospace;
    font-size: 8pt; color: {FG_DIM};
    background: transparent; font-style: italic;
"""


def _section(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_SECTION)
    lbl.setWordWrap(True)
    return lbl


def _body(text: str) -> QLabel:
    """A paragraph. *text* is rich text: names of buttons, tabs and columns go in <b>."""
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
    return "".join("<br>•&nbsp; " + line for line in lines)


def _hotkey() -> str:
    """This tool's hotkey as the launcher has it now (follows a rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label("hotkey_dps", "<shift>+1")
    except Exception:                       # noqa: BLE001 - a tutorial is not worth a failed open
        return "Shift+1"


def tutorial_text() -> str:
    """All the tutorial's text as one piece of markup (what the tests read). Needs a QApplication."""
    pages = [getattr(TutorialPopup, name)(None) for name in _TAB_BUILDERS.values()]
    return "<br>".join(lbl.text() for page in pages for lbl in page.findChildren(QLabel))


def _build_tab(widgets: list[QWidget]) -> QScrollArea:
    """Wrap a list of widgets in a scrollable tab page."""
    page = QWidget()
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
        QScrollArea {{ background: {BG}; border: none; }}
    """)
    scroll.setWidget(page)
    return scroll


# Tab title -> the method that builds it, in the order shown.
_TAB_BUILDERS = {
    "Getting Started": "_tab_getting_started",
    "Weapons": "_tab_weapons",
    "Systems": "_tab_defenses",
    "Power & Sigs": "_tab_power",
    "Tools": "_tab_tools",
}


class TutorialPopup(QDialog):
    """Multi-tab tutorial popup for the DPS Calculator.  Draggable.

    Singleton: a second call just raises the existing window.
    """

    _instance: "TutorialPopup | None" = None

    def __new__(cls, parent=None):
        if cls._instance is not None and cls._instance.isVisible():
            cls._instance.raise_()
            cls._instance.activateWindow()
            return cls._instance
        instance = super().__new__(cls)
        cls._instance = instance
        return instance

    def __init__(self, parent=None):
        if getattr(self, "_initialised", False):
            return
        self._initialised = True
        super().__init__(parent)
        self.setWindowFlags(
            Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
        )
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setFixedSize(520, 420)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {BG};
                border: 1px solid {ACCENT};
                border-radius: 6px;
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
        title = QLabel("\u2694  DPS Calculator Tutorial", hdr)
        title.setStyleSheet(f"""
            font-family: Electrolize, Consolas; font-size: 10pt;
            font-weight: bold; color: {ACCENT}; background: transparent;
        """)
        hdr_lay.addWidget(title)
        hdr_lay.addStretch(1)

        btn_close = QPushButton("\u2715", hdr)
        btn_close.setFixedSize(26, 22)
        btn_close.setCursor(QCursor(Qt.PointingHandCursor))
        btn_close.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {FG_DIM};
                border: none; font-size: 11pt;
            }}
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
                font-family: Consolas; font-size: 8pt; font-weight: bold;
            }}
            QTabBar::tab:hover {{ color: {FG}; background-color: {BG3}; }}
            QTabBar::tab:selected {{
                color: {ACCENT}; border-bottom-color: {ACCENT};
                background-color: {BG};
            }}
            QTabWidget::pane {{ background-color: {BG}; border: none; }}
        """)

        for title, builder in _TAB_BUILDERS.items():
            tabs.addTab(getattr(self, builder)(), title.replace("&", "&&"))    # a lone & is Qt's shortcut mark
        root.addWidget(tabs, 1)

    # ── Tab content ──────────────────────────────────────────────────────

    def _tab_getting_started(self) -> QScrollArea:
        return _build_tab([
            _section("What it is for"),
            _body(
                "Use the DPS Calculator to see what a ship's loadout does before "
                "you buy the parts: damage, shields, power and signatures, with "
                "any weapon or component swapped in."
            ),

            _section("Pick a ship"),
            _body(
                "Type part of a name in the <b>Ship</b> box at the top "
                "(for example glad for Gladius) and click a result. The ship "
                "loads with the parts it comes with."
            ),

            _section("The three panels"),
            _body(_bullets(
                "Left: the ship's weapons, turrets and missile racks, one row per slot.",
                "Center: two tabs, <b>Defenses / Systems</b> and <b>Power &amp; Propulsion</b>.",
                "Right: the power columns, the totals and the signatures.",
            )),
            _hint("Drag the dividers between panels to resize them."),

            _section("Change a part"),
            _body(
                "Click a row to open the picker for that slot. It lists the "
                "parts that fit there. Type in <b>Filter</b> to narrow the list "
                "by name, click a column header to sort, then click a part to "
                "fit it. <b>leave empty</b> clears the slot."
            ),
            _hint("Hover a column header or a number to read what it means."),

            _section("Back to stock"),
            _body(
                "<b>RESET</b>, on the <b>POWER PLANTS</b>, <b>COOLERS</b> and "
                "<b>RADARS</b> headers, puts the whole ship back to the parts it "
                "comes with, not only that section."
            ),

            _section("Data and hotkey"),
            _body(
                "<b>⟳ Refresh</b> checks StarCitizenWiki/scunpacked-data for a "
                "newer game-data build and switches to it. The data is kept on "
                "this PC between sessions.<br><br>"
                f"The launcher's hotkey for this tool is {_hotkey()}. It shows "
                "and hides the window."
            ),
        ])

    def _tab_weapons(self) -> QScrollArea:
        return _build_tab([
            _section("Weapon rows"),
            _body(
                "The left panel groups the slots under headers such as "
                "<b>WEAPONS</b>, <b>TURRETS</b> and <b>MISSILE &amp; BOMB RACKS</b>. "
                "Only the groups this ship has are shown. A weapon row reads, "
                "left to right:" + _bullets(
                    "<b>DPS↓</b>: sustained damage per second, the figure to compare loadouts on.",
                    "<b>Raw</b>: burst damage per second, before heat or ammo limits it.",
                    "<b>Effic</b>: burst damage for the power the gun draws. Higher is better.",
                    "<b>Alpha</b>: damage of one shot.",
                    "<b>RPS</b>: shots per second.",
                    "Then <b>Speed</b>, <b>Range</b>, <b>Spread</b>, <b>Power</b>, "
                    "<b>Ammo</b>, <b>Pen</b> and <b>HP</b>.",
                )
            ),
            _hint("A green ×N badge on a turret row means N identical guns; its DPS counts all of them."),

            _section("Not in totals"),
            _body(
                "Guns listed under <b>NOT IN TOTALS</b> are left out of the ship's "
                "totals. Each row says why."
            ),

            _section("Missiles"),
            _body(
                "Under <b>MISSILE &amp; BOMB RACKS</b>, each rack is followed by "
                "the missiles on it. A missile row shows <b>Track</b>, "
                "<b>Dmg↓</b>, <b>Speed</b>, <b>Range</b> and <b>Lock</b>."
            ),

            _section("Where to buy a part"),
            _body(
                "Press the \U0001f6d2 button on a filled row, or in the picker, to "
                "look the part up on UEX without fitting it. The list shows "
                "<b>Location</b>, <b>Terminal</b>, <b>Buy aUEC</b>, "
                "<b>Sell aUEC</b> and <b>Updated</b>. Press <b>Pin</b> to keep a "
                "list open. Up to 5 can be open at once."
            ),
        ])

    def _tab_defenses(self) -> QScrollArea:
        return _build_tab([
            _section("Defenses / Systems tab"),
            _body(_bullets(
                "<b>SHIELDS</b>: <b>HP↓</b> is the shield's hit points, "
                "<b>Reg/s</b> how fast it comes back, and <b>Phys</b>, "
                "<b>Enrg</b> and <b>Dist</b> its resistance to physical, energy "
                "and distortion damage.",
                "<b>COOLERS</b>: <b>Cool↓</b> is the cooling rate.",
                "<b>RADARS</b>: one row per radar. Click it to swap the radar.",
            )),

            _section("Power & Propulsion tab"),
            _body(_bullets(
                "<b>POWER PLANTS</b>: <b>Output</b> is the power each plant supplies.",
                "<b>QUANTUM DRIVES</b>: <b>Speed km/s</b>, <b>Max Dist Gm</b>, "
                "<b>Spool s</b>, <b>Cooldown s</b> and <b>Fuel/Mm</b>.",
                "<b>MAIN THRUSTERS</b>, <b>RETRO THRUSTERS</b> and "
                "<b>MANEUVERING</b> are shown for reference and cannot be swapped.",
            )),
            _hint("Sections appear only for the kinds of slot the ship has."),
        ])

    def _tab_power(self) -> QScrollArea:
        return _build_tab([
            _section("Power columns"),
            _body(
                "The top of the right panel has one column of pips for each "
                "thing that draws power: <b>WPN</b>, <b>THR</b>, <b>SHD</b>, "
                "<b>RDR</b>, <b>LSP</b>, <b>CLR</b>, <b>QDR</b> and <b>UTL</b>. "
                "Each cooler has a column of its own." + _bullets(
                    "Left-click a pip to set that column to that level.",
                    "Right-click a column, or click the icon under it, to switch it off. Do it again to switch it back on.",
                    "Green pips are the normal level, orange pips are above it, grey means switched off.",
                )
            ),
            _hint("The DPS↓ column and the totals at the bottom follow the power you give the weapons."),

            _section("Too much draw"),
            _body(
                "The bar at the top of the power panel turns yellow, then red, as the draw "
                "nears what the power plants supply. Past that it reads "
                "<b>OVER CAPACITY</b> with the amount you are over."
            ),

            _section("Flight modes"),
            _body(
                "<b>SCM</b> is combat flight and <b>NAV</b> is cruise. Switch "
                "between them to see the power and signatures of each. In "
                "<b>NAV</b> the shields are not powered."
            ),

            _section("Signatures"),
            _body(
                "The right panel lists IR, EM and CS under <b>SIGNATURES</b>. "
                "Lower is harder to detect. If a part has no power data the "
                "panel says <b>Signatures not computed</b> and names the part."
            ),

            _section("Totals at the bottom"),
            _body(
                "The bar along the bottom adds the ship up: <b>Burst:</b>, "
                "<b>DPS:</b>, <b>Alpha T+P:</b>, <b>Shield:</b>, <b>Hull:</b> "
                "and <b>Cooling:</b>. Hover any of them to read what it counts."
            ),
        ])

    def _tab_tools(self) -> QScrollArea:
        return _build_tab([
            _section("Save and load a loadout"),
            _body(
                "<b>⬇ Save Loadout</b> writes the ship and every part you "
                "picked to a file. <b>⬆ Load Loadout</b> opens one, switches "
                "to its ship and fits the parts again. If a saved part cannot "
                "be fitted any more, the status line says how many."
            ),

            _section("Crafting"),
            _body(
                "<b>\U0001f527 Crafting</b> sets the crafting quality of the "
                "weapons you have fitted. Quality 500 is a store-bought weapon "
                "and changes nothing. Drag <b>ALL slots</b> to move every part "
                "at once, or set each part on its own. "
                "<b>Reset to store-bought</b> puts everything back to 500."
            ),
            _hint("Only weapons that can be crafted are listed."),

            _section("Time to kill"),
            _body(
                "<b>⚔ TTK</b> asks for a <b>Target ship:</b> and shows how "
                "long your sustained DPS takes to get through its shield and "
                "hull, as <b>TIME TO KILL</b>. It uses the DPS you had when you "
                "opened it, so close and reopen it after changing the loadout."
            ),

            _section("Optimize"),
            _body(
                "<b>\U0001f3af Optimize</b> lists the best weapon for each "
                "hardpoint. Choose what to <b>Optimize for:</b> "
                "<b>Sustained DPS</b>, <b>Burst DPS</b> or <b>Alpha</b>. It is a "
                "recommendation only and does not change your loadout."
            ),
        ])


    # ── Drag-to-move (header only) ─────────────────────────────────────

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

    def closeEvent(self, event):
        TutorialPopup._instance = None
        self._initialised = False
        super().closeEvent(event)

    def show_relative_to(self, widget: QWidget) -> None:
        """Position the popup near *widget*, then show (non-modal)."""
        pos = widget.mapToGlobal(QPoint(0, widget.height() + 4))
        x = max(0, pos.x() - self.width() + widget.width())
        self.move(x, pos.y())
        self.show()
        self.raise_()
        self.activateWindow()
