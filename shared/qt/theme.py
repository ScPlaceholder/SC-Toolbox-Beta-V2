"""
SC Toolbox unified colour palette and QSS stylesheet generator.

Every colour constant in this file is extracted from the actual codebase:
    - ui/main_window.py (launcher)
    - skills/DPS_Calculator/dps_ui/constants.py
    - skills/Mission_Database/config.py
    - skills/Mining_Loadout/ui/constants.py
    - skills/Market_Finder/market_finder/config.py
    - shared/theme.py (Trade Hub canonical palette)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict


@dataclass(frozen=True)
class Palette:
    """Canonical SC Toolbox colour tokens."""

    # ── Backgrounds (dark-to-light surface stack) ──
    bg_deepest:   str = "#06080d"
    bg_primary:   str = "#0b0e14"
    bg_secondary: str = "#111620"
    bg_card:      str = "#141a26"
    bg_input:     str = "#1c2233"
    bg_header:    str = "#0e1420"

    # ── Borders & separators ──
    border:       str = "#1e2738"
    border_card:  str = "#252e42"
    separator:    str = "#0d1824"

    # ── Text hierarchy ──
    fg:           str = "#c8d4e8"
    fg_bright:    str = "#e8f2ff"
    fg_dim:       str = "#5a6480"
    fg_disabled:  str = "#3a4460"

    # ── Brand accent ──
    accent:       str = "#44aaff"
    sc_cyan:      str = "#00e7ff"   # MobiGlas canonical (reference only)

    # ── Functional colours ──
    green:        str = "#33dd88"
    yellow:       str = "#ffaa22"
    red:          str = "#ff5533"
    orange:       str = "#ff7733"
    purple:       str = "#aa66ff"
    energy_cyan:  str = "#44ccff"

    # ── Per-tool accent colours ──
    tool_dps:     str = "#ff7733"
    tool_cargo:   str = "#33ccdd"
    tool_mission: str = "#33dd88"
    tool_mining:  str = "#ffaa22"
    tool_market:  str = "#aa66ff"
    tool_trade:   str = "#ffcc00"

    # ── Selection ──
    selection:    str = "#10203c"

    # ── Scrollbar ──
    scrollbar_bg: str = "#0b0e14"
    scrollbar_handle: str = "#1e2738"


# Singleton default palette
P = Palette()


def generate_qss(p: Palette | None = None) -> str:
    """Generate a complete QSS stylesheet for the SC Toolbox MobiGlas theme."""
    if p is None:
        p = P

    return f"""
/* ═══════════════════════════════════════════════════════════════════════════
   SC Toolbox – MobiGlas QSS Theme
   Generated from shared/qt/theme.py
   ═══════════════════════════════════════════════════════════════════════════ */

/* ── Base ── */
QWidget {{
    background-color: transparent;
    color: {p.fg};
    font-family: Consolas, monospace;
    font-size: 9pt;
    border: none;
}}

QMainWindow {{
    background-color: transparent;
}}

/* ── Labels ── */
QLabel {{
    background-color: transparent;
    padding: 0px;
    border: none;
}}

QLabel[heading="true"] {{
    font-family: Electrolize, Consolas, monospace;
    font-size: 13pt;
    font-weight: bold;
    color: {p.fg_bright};
    letter-spacing: 1px;
}}

QLabel[accent="true"] {{
    color: {p.accent};
}}

QLabel[dim="true"] {{
    color: {p.fg_dim};
}}

QLabel[bright="true"] {{
    color: {p.fg_bright};
}}

/* ── Buttons ── */
QPushButton {{
    background-color: rgba(20, 26, 38, 180);
    color: {p.fg};
    border: 1px solid rgba(68, 170, 255, 40);
    padding: 6px 14px;
    font-family: Consolas, monospace;
    font-size: 9pt;
    font-weight: bold;
    min-height: 18px;
}}

QPushButton:hover {{
    background-color: rgba(28, 34, 51, 200);
    border-color: {p.accent};
    color: {p.accent};
}}

QPushButton:pressed {{
    background-color: {p.selection};
    border-color: {p.sc_cyan};
}}

QPushButton:disabled {{
    background-color: {p.bg_input};
    color: {p.fg_disabled};
    border-color: {p.border};
}}

QPushButton[primary="true"] {{
    background-color: {p.accent};
    color: {p.bg_primary};
    border: none;
}}

QPushButton[primary="true"]:hover {{
    background-color: {p.sc_cyan};
    color: {p.bg_primary};
}}

QPushButton[destructive="true"] {{
    background-color: {p.red};
    color: #ffffff;
    border: none;
}}

QPushButton[success="true"] {{
    background-color: {p.green};
    color: {p.bg_primary};
    border: none;
}}

/* ── Line edits ── */
QLineEdit {{
    background-color: rgba(28, 34, 51, 160);
    color: {p.fg};
    border: 1px solid rgba(68, 170, 255, 30);
    border-bottom: 1px solid rgba(68, 170, 255, 60);
    padding: 5px 8px;
    font-family: Consolas, monospace;
    font-size: 9pt;
    selection-background-color: {p.accent};
    selection-color: {p.bg_primary};
}}

QLineEdit:focus {{
    border-color: {p.accent};
    border-bottom-color: {p.accent};
}}

QLineEdit:disabled {{
    background-color: {p.bg_card};
    color: {p.fg_disabled};
}}

/* ── Combo boxes ── */
QComboBox {{
    background-color: rgba(28, 34, 51, 160);
    color: {p.fg};
    border: 1px solid rgba(68, 170, 255, 30);
    border-bottom: 1px solid rgba(68, 170, 255, 60);
    padding: 5px 8px;
    font-family: Consolas, monospace;
    font-size: 9pt;
    min-height: 18px;
}}

QComboBox:hover {{
    border-color: {p.accent};
}}

QComboBox:focus {{
    border-color: {p.accent};
}}

QComboBox::drop-down {{
    border: none;
    width: 20px;
}}

QComboBox::down-arrow {{
    image: none;
    border-left: 4px solid transparent;
    border-right: 4px solid transparent;
    border-top: 5px solid {p.fg_dim};
    margin-right: 6px;
}}

QComboBox QAbstractItemView {{
    background-color: {p.bg_secondary};
    color: {p.fg};
    border: 1px solid {p.border};
    selection-background-color: {p.selection};
    selection-color: {p.fg_bright};
    outline: none;
}}

/* ── Tables ── */
QTableView {{
    background-color: {p.bg_primary};
    alternate-background-color: {p.bg_card};
    color: {p.fg};
    gridline-color: transparent;
    border: none;
    font-family: Consolas, monospace;
    font-size: 9pt;
    selection-background-color: {p.selection};
    selection-color: {p.fg_bright};
    outline: none;
}}

QTableView::item {{
    padding: 4px 8px;
    border: none;
}}

QTableView::item:selected {{
    background-color: {p.selection};
    color: {p.fg_bright};
}}

QTableView::item:hover {{
    background-color: {p.bg_input};
}}

QHeaderView::section {{
    background-color: {p.bg_header};
    color: {p.accent};
    border: none;
    border-bottom: 1px solid {p.accent};
    border-right: 1px solid {p.border};
    padding: 5px 8px;
    font-family: Electrolize, Consolas, monospace;
    font-size: 8pt;
    font-weight: bold;
    text-transform: uppercase;
}}

QHeaderView::section:hover {{
    background-color: {p.bg_card};
    color: {p.fg_bright};
}}

/* ── Scroll bars ── */
QScrollBar:vertical {{
    background-color: {p.scrollbar_bg};
    width: 8px;
    margin: 0;
    border: none;
}}

QScrollBar::handle:vertical {{
    background-color: {p.scrollbar_handle};
    min-height: 30px;
    border-radius: 4px;
}}

QScrollBar::handle:vertical:hover {{
    background-color: {p.fg_dim};
}}

QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}

QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: none;
}}

QScrollBar:horizontal {{
    background-color: {p.scrollbar_bg};
    height: 8px;
    margin: 0;
    border: none;
}}

QScrollBar::handle:horizontal {{
    background-color: {p.scrollbar_handle};
    min-width: 30px;
    border-radius: 4px;
}}

QScrollBar::handle:horizontal:hover {{
    background-color: {p.fg_dim};
}}

QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}

QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: none;
}}

/* ── Scroll area ── */
QScrollArea {{
    background-color: transparent;
    border: none;
}}

/* ── Tab bar ── */
QTabBar {{
    background-color: {p.bg_secondary};
    border: none;
}}

QTabBar::tab {{
    background-color: {p.bg_secondary};
    color: {p.fg_dim};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 8px 16px;
    font-family: Consolas, monospace;
    font-size: 9pt;
    font-weight: bold;
}}

QTabBar::tab:hover {{
    color: {p.fg};
    background-color: {p.bg_card};
}}

QTabBar::tab:selected {{
    color: {p.accent};
    border-bottom-color: {p.accent};
    background-color: {p.bg_primary};
}}

QTabWidget::pane {{
    background-color: {p.bg_primary};
    border: none;
}}

/* ── Splitter ── */
QSplitter::handle {{
    background-color: {p.border};
    width: 1px;
    height: 1px;
}}

QSplitter::handle:hover {{
    background-color: {p.accent};
}}

/* ── Tooltip ── */
QToolTip {{
    background-color: {p.bg_secondary};
    color: {p.fg};
    border: 1px solid {p.border};
    padding: 5px 8px;
    font-family: Consolas, monospace;
    font-size: 8pt;
}}

/* ── Menu ── */
QMenu {{
    background-color: {p.bg_secondary};
    color: {p.fg};
    border: 1px solid {p.border};
    padding: 4px 0px;
}}

QMenu::item {{
    padding: 6px 24px;
}}

QMenu::item:selected {{
    background-color: {p.selection};
    color: {p.fg_bright};
}}

QMenu::separator {{
    height: 1px;
    background-color: {p.border};
    margin: 4px 8px;
}}

/* ── Check box ── */
QCheckBox {{
    background-color: transparent;
    color: {p.fg};
    spacing: 6px;
    font-size: 9pt;
}}

QCheckBox::indicator {{
    width: 14px;
    height: 14px;
    border: 1px solid {p.border};
    background-color: {p.bg_input};
}}

QCheckBox::indicator:checked {{
    background-color: {p.accent};
    border-color: {p.accent};
}}

QCheckBox::indicator:hover {{
    border-color: {p.accent};
}}

/* ── Slider ── */
QSlider::groove:horizontal {{
    background-color: {p.bg_input};
    height: 4px;
    border-radius: 2px;
}}

QSlider::handle:horizontal {{
    background-color: {p.accent};
    width: 12px;
    height: 12px;
    margin: -4px 0;
    border-radius: 6px;
}}

QSlider::handle:horizontal:hover {{
    background-color: {p.sc_cyan};
}}

/* ── Progress bar ── */
QProgressBar {{
    background-color: {p.bg_input};
    border: 1px solid {p.border};
    text-align: center;
    color: {p.fg};
    font-size: 8pt;
    height: 16px;
}}

QProgressBar::chunk {{
    background-color: {p.accent};
}}

/* ── Group box ── */
QGroupBox {{
    border: 1px solid {p.border};
    margin-top: 8px;
    padding-top: 12px;
    font-family: Electrolize, Consolas, monospace;
    font-size: 9pt;
    font-weight: bold;
    color: {p.fg_dim};
}}

QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 4px;
    color: {p.fg_dim};
}}

/* ── Frame ── */
QFrame[hud="true"] {{
    border: none;
    background-color: {p.bg_card};
}}
"""


def apply_theme(app, palette: Palette | None = None) -> None:
    """Apply the MobiGlas QSS theme to a QApplication."""
    from shared.qt.fonts import load_fonts
    load_fonts()
    app.setStyleSheet(generate_qss(palette))
