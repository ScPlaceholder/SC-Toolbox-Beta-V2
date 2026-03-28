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
    lbl = QLabel(text)
    lbl.setStyleSheet(_BODY)
    lbl.setWordWrap(True)
    return lbl


def _hint(text: str) -> QLabel:
    lbl = QLabel(text)
    lbl.setStyleSheet(_HINT)
    lbl.setWordWrap(True)
    return lbl


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


class TutorialPopup(QDialog):
    """Multi-tab tutorial popup for the DPS Calculator.  Draggable."""

    def __init__(self, parent=None):
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

        tabs.addTab(self._tab_getting_started(), "Getting Started")
        tabs.addTab(self._tab_weapons(), "Weapons & DPS")
        tabs.addTab(self._tab_defenses(), "Defenses")
        tabs.addTab(self._tab_power(), "Power & Sigs")
        root.addWidget(tabs, 1)

    # ── Tab content ──────────────────────────────────────────────────────

    def _tab_getting_started(self) -> QScrollArea:
        return _build_tab([
            _section("Ship Selector"),
            _body(
                "Use the search box at the top to find a ship. "
                "Type any part of the name (e.g. \"glad\" for Gladius) "
                "and click a result to load it."
            ),
            _hint("The selector uses fuzzy matching \u2014 you can type partial names."),

            _section("Three-Panel Layout"),
            _body(
                "\u2022  Left panel \u2014 Weapons & Missiles with DPS stats\n"
                "\u2022  Center panel \u2014 Defenses/Systems and Power/Propulsion tabs\n"
                "\u2022  Right panel \u2014 Summary overview and signatures"
            ),
            _hint("Drag the dividers between panels to resize them."),

            _section("Swapping Components"),
            _body(
                "Click any component name (weapon, shield, cooler, etc.) "
                "to open a picker popup. The picker shows all compatible "
                "components for that slot size and lets you search/sort."
            ),

            _section("Refresh"),
            _body(
                "The \u27f3 Refresh button re-fetches data from erkul.games. "
                "Data is cached for 2 hours and updates automatically."
            ),
        ])

    def _tab_weapons(self) -> QScrollArea:
        return _build_tab([
            _section("Weapon Table"),
            _body(
                "The left panel shows every weapon hardpoint on the ship. "
                "Each row displays:\n"
                "\u2022  Name and size (S1\u2013S7)\n"
                "\u2022  DPS (damage per second, sustained)\n"
                "\u2022  Alpha (damage per shot)\n"
                "\u2022  RPS (rounds per second)\n"
                "\u2022  Range and ammo count"
            ),

            _section("Damage Types"),
            _body(
                "Damage is broken down by type:\n"
                f"\u2022  Physical \u2014 ballistic/projectile\n"
                f"\u2022  Energy \u2014 laser/plasma\n"
                f"\u2022  Distortion \u2014 disables components\n"
                f"\u2022  Thermal \u2014 heat damage"
            ),
            _hint("The color-coded bars show the damage split at a glance."),

            _section("Missiles"),
            _body(
                "Missile racks appear below weapons. Stats include total "
                "damage, tracking type (IR/EM/CS), lock time, and speed."
            ),
        ])

    def _tab_defenses(self) -> QScrollArea:
        return _build_tab([
            _section("Shields"),
            _body(
                "The Defenses tab shows shield generators with:\n"
                "\u2022  HP (total hit points)\n"
                "\u2022  Regen (HP/s regeneration rate)\n"
                "\u2022  Resistances (Physical / Energy / Distortion / Thermal)"
            ),

            _section("Coolers"),
            _body(
                "Coolers manage heat dissipation. Higher cooling rate = "
                "more sustained fire before overheating."
            ),

            _section("Radars"),
            _body(
                "Radar stats show detection ranges for different signature "
                "types. Larger radars detect at greater distances."
            ),

            _section("Power Plants & Quantum Drives"),
            _body(
                "Found under the Power & Propulsion tab:\n"
                "\u2022  Power plants \u2014 total output, EM signature\n"
                "\u2022  Quantum drives \u2014 speed, spool time, fuel rate, range"
            ),
        ])

    def _tab_power(self) -> QScrollArea:
        return _build_tab([
            _section("Power Allocation"),
            _body(
                "The right panel shows the power triangle. Adjust power "
                "distribution between weapons, shields, and thrusters to "
                "see how it affects performance and signatures."
            ),

            _section("Signatures (EM / IR / CS)"),
            _body(
                "\u2022  EM (Electromagnetic) \u2014 affected by power plants & shields\n"
                "\u2022  IR (Infrared) \u2014 affected by thrusters & heat\n"
                "\u2022  CS (Cross-Section) \u2014 based on ship size"
            ),
            _hint("Lower signatures make you harder to detect on radar."),

            _section("Flight Modes"),
            _body(
                "\u2022  SCM (Space Combat Maneuvering) \u2014 combat flight mode\n"
                "\u2022  NAV (Navigation) \u2014 cruise mode, higher top speed\n\n"
                "Toggle between them to see how signatures and "
                "thruster performance change."
            ),

            _section("Footer Totals"),
            _body(
                "The bar at the bottom shows aggregate stats: "
                "total DPS, shield HP, missile damage, and hull HP."
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

    def show_relative_to(self, widget: QWidget) -> None:
        """Position the popup near *widget*, then show (non-modal)."""
        pos = widget.mapToGlobal(QPoint(0, widget.height() + 4))
        x = max(0, pos.x() - self.width() + widget.width())
        self.move(x, pos.y())
        self.show()
        self.raise_()
        self.activateWindow()
