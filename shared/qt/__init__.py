"""
SC Toolbox – shared PySide6 widget library.

Provides a Star Citizen MobiGlas-grade UI foundation:
frameless windows, HUD panels, glow effects, themed tables, and
a comprehensive QSS stylesheet derived from the project's actual palette.

Usage:
    from shared.qt import SCWindow, SCTitleBar, HUDPanel, SCButton, SCTable, Palette
    from shared.qt.theme import apply_theme
"""

from shared.qt.theme import Palette, generate_qss, apply_theme  # noqa: F401
from shared.qt.fonts import load_fonts  # noqa: F401
from shared.qt.base_window import SCWindow  # noqa: F401
from shared.qt.title_bar import SCTitleBar  # noqa: F401
from shared.qt.hud_widgets import HUDPanel, GlowEffect, ScanlineOverlay  # noqa: F401
from shared.qt.animated_button import SCButton  # noqa: F401
from shared.qt.data_table import SCTable, SCTableModel  # noqa: F401
from shared.qt.search_bar import SCSearchBar  # noqa: F401
from shared.qt.dropdown import SCComboBox, SCMultiCheck  # noqa: F401
from shared.qt.ipc_thread import IPCWatcher  # noqa: F401
from shared.qt.fuzzy_combo import SCFuzzyCombo  # noqa: F401
from shared.qt.fuzzy_multi_check import SCFuzzyMultiCheck  # noqa: F401
