"""Look-and-feel helpers for the Dev Mode window.

Everything visual that more than one step page uses lives here so the
seven pages stay about behaviour, not about colour codes. Colours come
from the shared toolbox palette ``P``; the accent is Mining Signals'
green so the pop-out reads as part of that tool.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

ACCENT = "#33dd88"          # Mining Signals' tool accent (tools/Mining_Signals/ui/theme.py)
ACCENT_RGB = "51, 221, 136"

# Region kinds, in the training registry's declaration order. The contract
# has no list_kinds(), and ocr.training_registry also registers an internal
# "pending_hud" staging kind that users must never train, so the six public
# kinds are spelled out here.
KINDS: tuple[str, ...] = (
    "signal", "signal_inv", "signal_rgb", "signal_rgb_inv", "hud", "hud_rgb",
)

KIND_LABELS = {
    "signal": "Signal (mono)",
    "signal_inv": "Signal (mono, inverted)",
    "signal_rgb": "Signal (colour)",
    "signal_rgb_inv": "Signal (colour, inverted)",
    "hud": "HUD (mono)",
    "hud_rgb": "HUD (colour)",
}

# Proposal sources -> (badge text, colour, how-much-to-trust sentence)
SOURCE_INFO = {
    "consensus": ("CONSENSUS", P.green,
                  "The OCR engines agree. Usually right, still glance at it."),
    "reader": ("READER", P.yellow,
               "One engine's reading, nothing to cross-check it. Check every digit."),
    "import": ("IMPORT", P.orange,
               "Taken from an old file name. About 1 in 5 of these were wrong."),
    None: ("NO PROPOSAL", P.fg_dim,
           "Nothing to go on. Type exactly what you see."),
}

STATUS_COLOURS = {
    "unlabeled": P.fg_dim,
    "proposed": P.yellow,
    "confirmed": P.green,
    "rejected": P.red,
}

MONO = "Consolas, monospace"
HEAD = "Electrolize, Consolas, monospace"


def fmt_bytes(n: Optional[int]) -> str:
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


def fmt_pct(x: Optional[float]) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.1f}%"


def heading(text: str, parent: Optional[QWidget] = None) -> QLabel:
    lbl = QLabel(text.upper(), parent)
    lbl.setStyleSheet(
        f"font-family: {HEAD}; font-size: 14pt; font-weight: bold; "
        f"color: {P.fg_bright}; letter-spacing: 2px; background: transparent;"
    )
    return lbl


def subtext(text: str, parent: Optional[QWidget] = None, colour: str = "") -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setWordWrap(True)
    lbl.setStyleSheet(
        f"font-family: {MONO}; font-size: 9pt; color: {colour or P.fg_dim}; "
        f"background: transparent;"
    )
    return lbl


def section_label(text: str, parent: Optional[QWidget] = None) -> QLabel:
    lbl = QLabel(text.upper(), parent)
    lbl.setStyleSheet(
        f"font-family: {HEAD}; font-size: 9pt; font-weight: bold; "
        f"color: {ACCENT}; letter-spacing: 1px; background: transparent;"
    )
    return lbl


class Card(QFrame):
    """Dark rounded panel used to group controls on a step page."""

    def __init__(self, parent: Optional[QWidget] = None, title: str = "",
                 margins: tuple[int, int, int, int] = (14, 12, 14, 12)):
        super().__init__(parent)
        self.setObjectName("dmCard")
        self.setStyleSheet(
            f"QFrame#dmCard {{ background-color: rgba(20, 26, 38, 200); "
            f"border: 1px solid {P.border_card}; border-radius: 4px; }}"
        )
        self.body = QVBoxLayout(self)
        self.body.setContentsMargins(*margins)
        self.body.setSpacing(8)
        if title:
            self.body.addWidget(section_label(title, self))


def button(text: str, parent: Optional[QWidget] = None, kind: str = "normal",
           tooltip: str = "") -> QPushButton:
    """A themed push button. kind: normal | primary | danger | ghost."""
    btn = QPushButton(text, parent)
    btn.setCursor(Qt.PointingHandCursor)
    if tooltip:
        btn.setToolTip(tooltip)
    if kind == "primary":
        btn.setStyleSheet(
            f"QPushButton {{ background: rgba({ACCENT_RGB}, 40); color: {ACCENT}; "
            f"border: 1px solid {ACCENT}; border-radius: 3px; padding: 7px 16px; "
            f"font-family: {MONO}; font-size: 9pt; font-weight: bold; }}"
            f"QPushButton:hover {{ background: rgba({ACCENT_RGB}, 90); color: {P.fg_bright}; }}"
            f"QPushButton:disabled {{ background: {P.bg_input}; color: {P.fg_disabled}; "
            f"border-color: {P.border}; }}"
        )
    elif kind == "danger":
        btn.setStyleSheet(
            f"QPushButton {{ background: rgba(255, 85, 51, 25); color: {P.red}; "
            f"border: 1px solid rgba(255, 85, 51, 120); border-radius: 3px; padding: 7px 16px; "
            f"font-family: {MONO}; font-size: 9pt; font-weight: bold; }}"
            f"QPushButton:hover {{ background: rgba(255, 85, 51, 80); color: {P.fg_bright}; }}"
            f"QPushButton:disabled {{ background: {P.bg_input}; color: {P.fg_disabled}; "
            f"border-color: {P.border}; }}"
        )
    else:
        btn.setStyleSheet(
            f"QPushButton {{ background: rgba(28, 34, 51, 200); color: {P.fg}; "
            f"border: 1px solid {P.border_card}; border-radius: 3px; padding: 7px 14px; "
            f"font-family: {MONO}; font-size: 9pt; font-weight: bold; }}"
            f"QPushButton:hover {{ border-color: {ACCENT}; color: {ACCENT}; }}"
            f"QPushButton:disabled {{ background: {P.bg_input}; color: {P.fg_disabled}; "
            f"border-color: {P.border}; }}"
        )
    return btn


PROGRESS_QSS = (
    f"QProgressBar {{ background: {P.bg_input}; border: 1px solid {P.border}; "
    f"border-radius: 2px; color: {P.fg}; font-family: {MONO}; font-size: 8pt; "
    f"text-align: center; min-height: 16px; max-height: 16px; }}"
    f"QProgressBar::chunk {{ background: rgba({ACCENT_RGB}, 170); }}"
)

COMBO_QSS = (
    f"QComboBox {{ background: rgba(28, 34, 51, 200); color: {P.fg}; "
    f"border: 1px solid {P.border_card}; border-radius: 3px; padding: 4px 8px; "
    f"font-family: {MONO}; font-size: 9pt; min-width: 150px; }}"
    f"QComboBox:hover {{ border-color: {ACCENT}; }}"
    f"QComboBox::drop-down {{ border: none; width: 18px; }}"
    f"QComboBox QAbstractItemView {{ background: {P.bg_secondary}; color: {P.fg}; "
    f"selection-background-color: {P.selection}; border: 1px solid {P.border}; }}"
)

TABLE_QSS = (
    f"QTableWidget {{ background: rgba(11, 14, 20, 200); alternate-background-color: "
    f"rgba(20, 26, 38, 200); color: {P.fg}; border: 1px solid {P.border}; "
    f"gridline-color: transparent; font-family: {MONO}; font-size: 9pt; }}"
    f"QTableWidget::item {{ padding: 3px 8px; }}"
    f"QTableWidget::item:selected {{ background: {P.selection}; color: {P.fg_bright}; }}"
    f"QHeaderView::section {{ background: {P.bg_header}; color: {ACCENT}; border: none; "
    f"border-bottom: 1px solid rgba({ACCENT_RGB}, 120); padding: 5px 8px; "
    f"font-family: {HEAD}; font-size: 8pt; font-weight: bold; }}"
)


class Chip(QLabel):
    """Small coloured count badge, e.g. 'CONFIRMED 42'."""

    def __init__(self, name: str, colour: str, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._name = name
        self._colour = colour
        self.set_value(0)

    def set_value(self, value) -> None:
        self.setText(f"{self._name}  <b>{value}</b>")
        self.setStyleSheet(
            f"QLabel {{ color: {self._colour}; background: rgba(20, 26, 38, 220); "
            f"border: 1px solid {self._colour}; border-radius: 9px; padding: 2px 10px; "
            f"font-family: {MONO}; font-size: 8pt; }}"
        )


class Banner(QFrame):
    """Full-width notice strip (info / warn / error) with optional action."""

    COLOURS = {"info": P.accent, "warn": P.yellow, "error": P.red, "ok": P.green}

    def __init__(self, parent: Optional[QWidget] = None, level: str = "info"):
        super().__init__(parent)
        self.setObjectName("dmBanner")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(10)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        lay.addWidget(self.label, 1)
        self.action: Optional[QPushButton] = None
        self._lay = lay
        self.set_level(level)

    def set_level(self, level: str) -> None:
        c = self.COLOURS.get(level, P.accent)
        self.setStyleSheet(
            f"QFrame#dmBanner {{ background: rgba(20, 26, 38, 230); "
            f"border: 1px solid {c}; border-left: 4px solid {c}; border-radius: 3px; }}"
        )
        self.label.setStyleSheet(
            f"color: {P.fg}; font-family: {MONO}; font-size: 9pt; background: transparent;"
        )

    def set_text(self, html: str, level: Optional[str] = None) -> None:
        if level:
            self.set_level(level)
        self.label.setText(html)

    def add_action(self, text: str, slot) -> QPushButton:
        self.action = button(text, self)
        self.action.clicked.connect(slot)
        self._lay.addWidget(self.action, 0, Qt.AlignVCenter)
        return self.action


def page_frame(parent: Optional[QWidget], title: str, blurb: str) -> tuple[QWidget, QVBoxLayout]:
    """Standard page scaffold: heading, one-line explanation, then content."""
    root = QVBoxLayout(parent)
    root.setContentsMargins(22, 16, 22, 16)
    root.setSpacing(12)
    root.addWidget(heading(title, parent))
    if blurb:
        root.addWidget(subtext(blurb, parent))
    return parent, root
