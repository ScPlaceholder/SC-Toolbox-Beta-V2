"""
Settings popup bubble — PySide6 MobiGlas implementation.

Floating popup with three tabs: Tools (enable/disable + keybinds),
Grid Layout (customizable NxM grid), and Language selection.
"""
import logging
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QPoint, QTimer, Signal, QEvent
from PySide6.QtGui import QIntValidator, QWheelEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QFrame,
    QSizePolicy, QComboBox, QPushButton, QCheckBox, QGridLayout,
    QScrollArea, QSpinBox, QSlider,
)

from shared.config_models import SkillConfig
from shared.i18n import _ as _t
from shared.qt import screen_fit
from shared.qt.theme import P
from shared.qt.animated_button import SCButton

log = logging.getLogger(__name__)

# Display names for language codes
_LANG_NAMES: dict[str, str] = {
    "en": "English",
    "de": "Deutsch",
    "fr": "Fran\u00e7ais",
    "es": "Espa\u00f1ol",
    "pt": "Portugu\u00eas",
    "it": "Italiano",
    "nl": "Nederlands",
    "pl": "Polski",
    "ru": "\u0420\u0443\u0441\u0441\u043a\u0438\u0439",
    "zh": "\u4e2d\u6587",
    "ja": "\u65e5\u672c\u8a9e",
    "ko": "\ud55c\uad6d\uc5b4",
}


def _lang_display(code: str) -> str:
    return _LANG_NAMES.get(code, code.upper())


# ── Shared QSS helpers ────────────────────────────────────────────────────────

_TOGGLE_QSS = f"""
    QCheckBox {{
        spacing: 6px;
        color: {P.fg};
        background: transparent;
        font-family: Consolas; font-size: 9pt;
    }}
    QCheckBox::indicator {{
        width: 36px; height: 18px;
        border-radius: 9px;
        border: 1px solid {P.border};
        background-color: {P.bg_input};
    }}
    QCheckBox::indicator:checked {{
        background-color: {P.green};
        border-color: {P.green};
    }}
"""

_SPIN_QSS = f"""
    QSpinBox {{
        background-color: {P.bg_input};
        color: {P.fg};
        border: 1px solid {P.border};
        font-family: Consolas; font-size: 9pt;
        padding: 2px 6px;
        min-width: 50px;
    }}
    QSpinBox::up-button, QSpinBox::down-button {{
        background-color: {P.bg_card};
        border: 1px solid {P.border};
        width: 16px;
    }}
    QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
        background-color: {P.bg_input};
    }}
"""

_COMBO_QSS = f"""
    QComboBox {{
        background-color: {P.bg_input};
        color: {P.fg};
        border: 1px solid {P.border};
        font-family: Consolas; font-size: 8pt;
        padding: 2px 4px;
    }}
    QComboBox:hover {{
        border-color: {P.accent};
    }}
    QComboBox QAbstractItemView {{
        background-color: {P.bg_card};
        color: {P.fg};
        border: 1px solid {P.border};
        selection-background-color: {P.bg_input};
        selection-color: {P.fg_bright};
    }}
"""

_LINE_EDIT_QSS = f"""
    QLineEdit {{
        background-color: {P.bg_input};
        color: {P.fg};
        border: 1px solid {P.border};
        font-family: Consolas; font-size: 9pt;
        padding: 2px 6px;
    }}
    QLineEdit:focus {{
        border-color: {P.accent};
    }}
"""


def _btn_qss(bg: str, bg_hover: str, color: str, color_hover: str = P.fg_bright,
             font_size: str = "9pt", padding: str = "8px 12px") -> str:
    return f"""
        QPushButton {{
            background-color: {bg};
            color: {color};
            border: none;
            font-family: Consolas; font-size: {font_size}; font-weight: bold;
            padding: {padding};
        }}
        QPushButton:hover {{
            background-color: {bg_hover};
            color: {color_hover};
        }}
    """


# ── Scroll-on-hover helper ─────────────────────────────────────────────────────

from PySide6.QtCore import QObject as _QObject


class _HoverWheelFilter(_QObject):
    """App-level event filter that intercepts wheel events and redirects
    them to QSpinBox / QSlider widgets under the cursor, even when those
    widgets don't have focus.

    Installed on ``QApplication.instance()`` so it sees every event before
    any widget.  Only active when ``_enabled`` is True and the cursor is
    inside the owning popup.
    """

    def __init__(self, popup: QWidget):
        super().__init__(popup)
        self._enabled = False
        self._popup = popup
        self._processing = False  # guard against re-entrant sendEvent

    def set_enabled(self, on: bool):
        self._enabled = on

    def install(self):
        """Install on the running QApplication."""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app:
            app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if not self._enabled or self._processing:
            return False
        if event.type() != QEvent.Wheel:
            return False

        # Only act when the cursor is inside our popup
        if not self._popup.isVisible():
            return False

        from PySide6.QtWidgets import QApplication
        global_pos = event.globalPosition().toPoint()
        widget = QApplication.widgetAt(global_pos)
        if not widget:
            return False

        # Check the widget itself and its ancestors for a spinbox or slider
        target = widget
        while target is not None:
            if isinstance(target, (QSpinBox, QSlider)):
                # Found one — give it focus and forward the wheel event
                self._processing = True
                target.setFocus(Qt.OtherFocusReason)
                # Build a new wheel event in the target's local coords
                local_pos = target.mapFromGlobal(global_pos)
                redirected = QWheelEvent(
                    local_pos,
                    global_pos,
                    event.pixelDelta(),
                    event.angleDelta(),
                    event.buttons(),
                    event.modifiers(),
                    event.phase(),
                    event.inverted(),
                )
                QApplication.sendEvent(target, redirected)
                self._processing = False
                return True  # consume original
            if target is self._popup:
                break  # don't walk past the popup
            target = target.parentWidget()

        return False


# ══════════════════════════════════════════════════════════════════════════════
# Settings Popup
# ══════════════════════════════════════════════════════════════════════════════

class SettingsPopup(QWidget):
    """Floating settings popup bubble with tabbed content."""

    applied = Signal(dict)  # emits full settings dict on Apply

    def __init__(
        self,
        parent_window: QWidget,
        skills: List[SkillConfig],
        launcher_hotkey: str,
        disabled_skills: List[str],
        keybinds_disabled: List[str],
        grid_rows: int,
        grid_cols: int,
        grid_layout: Dict[str, str],
        current_language: str = "en",
        available_languages: Optional[List[str]] = None,
        on_apply: Optional[Callable[[dict], None]] = None,
        scroll_on_hover: bool = False,
        ui_scale: float = 1.0,
        hide_on_tool_active: bool = False,
        opacity: float = 0.95,
    ) -> None:
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("settingsPopup")
        self.setWindowOpacity(opacity)
        self.setStyleSheet(f"QWidget#settingsPopup {{ background-color: {P.bg_header}; }}")

        # ── Size: the design size, shrunk to what this screen can actually show ──
        # UI scale is applied with QT_SCALE_FACTOR, and both widget sizes and availableGeometry()
        # are in device-independent pixels — so raising the scale does not grow this number, it
        # shrinks the screen's.  At 2.5x a 1920x1080 monitor reports 768x432, and the unclamped
        # 560x560 popup opened at y=-65: 65px above the top edge and 63px below the bottom one,
        # taking the Apply button and the title bar (the drag handle and the [x]) with it, measured
        # in shared/tests/test_settings_popup_fit.py.  The user then cannot reach the
        # control that would undo the scale: the setting makes itself unreachable.  Both tab bodies
        # are QScrollAreas, so a shorter popup scrolls rather than hiding rows.
        self._avail = screen_fit.available_rect(parent_window.frameGeometry().center()
                                                if parent_window is not None else None)
        self.setFixedSize(*screen_fit.clamp_size(560, 560, self._avail))

        self._parent_window = parent_window
        self._skills = skills
        self._on_apply = on_apply
        self._available_langs = available_languages or ["en"]
        self._current_tab = 0
        self._tab_btns: list[QPushButton] = []

        # ── Working copies of settings ──
        self._launcher_hotkey = launcher_hotkey
        self._skill_hotkeys: Dict[str, str] = {s.id: s.hotkey for s in skills}
        self._disabled: set[str] = set(disabled_skills)
        self._keybinds_disabled: set[str] = set(keybinds_disabled)
        self._grid_rows = grid_rows
        self._grid_cols = grid_cols
        self._grid_layout: Dict[str, str] = dict(grid_layout)
        self._scroll_on_hover = scroll_on_hover
        self._ui_scale = ui_scale
        self._hide_on_tool_active = hide_on_tool_active
        self._language = current_language

        # Scroll-on-hover event filter (app-level)
        self._wheel_filter = _HoverWheelFilter(self)
        self._wheel_filter.set_enabled(scroll_on_hover)

        # Save-on-close bookkeeping: closing the popup saves settings (same as
        # Apply); only Cancel discards.  These flags guard against double-saving.
        self._committed = False
        self._cancelled = False

        self._build_ui()

        # Install on QApplication so it intercepts all wheel events
        self._wheel_filter.install()

        self._select_tab(0)
        self._position_near_parent()

        # Snapshot the initial state so closeEvent can skip a needless save +
        # UI rebuild when the popup is closed without any changes.
        self._baseline = self._collect_and_validate()

    # ── Positioning ───────────────────────────────────────────────────────

    def _position_near_parent(self):
        pw = self._parent_window
        pos = pw.pos()
        size = pw.size()
        cx = pos.x() + (size.width() - self.width()) // 2
        cy = pos.y() + (size.height() - self.height()) // 2
        # Centring on the launcher can push the popup past an edge on its own — the launcher may
        # sit near one, or be bigger than the screen at a high UI scale.  Clamp after the size
        # clamp above, never before: clamping the position of a window that does not fit only
        # chooses which edge loses, which is how the SCWindow rescue (c9046c3) still ends up
        # pinning an oversized window to the top-left with its bottom bar off-screen.
        cx, cy = screen_fit.clamp_pos(cx, cy, self.width(), self.height(), self._avail)
        self.move(cx, cy)

    # ── Build UI ──────────────────────────────────────────────────────────

    def _build_ui(self):
        main_lay = QVBoxLayout(self)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # ── Title bar (draggable) ─────────────────────────────────────────
        bar = QWidget()
        bar.setObjectName("settingsBar")
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"QWidget#settingsBar {{ background-color: {P.bg_header}; }}")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 0, 6, 0)
        bar_lay.setSpacing(4)

        bar._drag_pos = QPoint()

        def drag_press(e):
            if e.button() == Qt.LeftButton:
                bar._drag_pos = e.globalPosition().toPoint() - self.pos()
                e.accept()

        def drag_move(e):
            if e.buttons() & Qt.LeftButton:
                self.move(e.globalPosition().toPoint() - bar._drag_pos)
                e.accept()

        bar.mousePressEvent = drag_press
        bar.mouseMoveEvent = drag_move

        title_lbl = QLabel("\u2699  " + _t("SETTINGS"))
        title_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 10pt; font-weight: bold;
            color: {P.accent}; background: transparent;
            letter-spacing: 2px;
        """)
        bar_lay.addWidget(title_lbl)
        bar_lay.addStretch(1)

        close_btn = QPushButton("x")
        close_btn.setObjectName("settingsClose")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton#settingsClose {{
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666;
                border: none;
                font-family: Consolas; font-size: 13pt; font-weight: bold;
                border-radius: 3px; padding: 0px; margin: 2px; min-height: 0px;
            }}
            QPushButton#settingsClose:hover {{
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }}
        """)
        close_btn.clicked.connect(self.close)
        bar_lay.addWidget(close_btn)

        main_lay.addWidget(bar)

        # ── Accent line ──────────────────────────────────────────────────
        accent = QFrame()
        accent.setFixedHeight(1)
        accent.setStyleSheet(f"background-color: {P.accent};")
        main_lay.addWidget(accent)

        # ── Tab bar ──────────────────────────────────────────────────────
        tab_bar = QWidget()
        tab_bar.setObjectName("settingsTabBar")
        tab_bar.setStyleSheet(f"QWidget#settingsTabBar {{ background-color: {P.bg_secondary}; }}")
        tab_lay = QHBoxLayout(tab_bar)
        tab_lay.setContentsMargins(6, 4, 6, 4)
        tab_lay.setSpacing(4)

        for i, label in enumerate([_t("Tools"), _t("Grid Layout"), _t("Language")]):
            btn = QPushButton(label)
            btn.setObjectName(f"settingsTab_{i}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(26)
            btn.clicked.connect(lambda checked=False, idx=i: self._select_tab(idx))
            tab_lay.addWidget(btn)
            self._tab_btns.append(btn)

        tab_lay.addStretch(1)
        main_lay.addWidget(tab_bar)

        # ── Content stack ────────────────────────────────────────────────
        self._tab_pages: list[QWidget] = []

        self._tools_page = self._build_tools_tab()
        self._tab_pages.append(self._tools_page)

        self._grid_page = self._build_grid_tab()
        self._tab_pages.append(self._grid_page)

        self._lang_page = self._build_language_tab()
        self._tab_pages.append(self._lang_page)

        for page in self._tab_pages:
            main_lay.addWidget(page)

        # ── Bottom bar ───────────────────────────────────────────────────
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        main_lay.addWidget(sep)

        bottom = QWidget()
        bottom.setObjectName("settingsBottom")
        bottom.setFixedHeight(44)
        bottom.setStyleSheet(f"QWidget#settingsBottom {{ background-color: {P.bg_secondary}; }}")
        b_lay = QHBoxLayout(bottom)
        b_lay.setContentsMargins(10, 6, 10, 6)
        b_lay.setSpacing(8)

        self._status_label = QLabel("")
        self._status_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {P.fg_dim}; background: transparent;
        """)
        b_lay.addWidget(self._status_label, stretch=1)

        cancel_btn = SCButton(_t("Cancel"), bottom, glow_color=P.red)
        cancel_btn.setStyleSheet(_btn_qss(
            "#2a1a18", "#3a2a28", P.fg_dim, font_size="8pt", padding="5px 14px",
        ))
        cancel_btn.clicked.connect(self._on_cancel_clicked)
        b_lay.addWidget(cancel_btn)

        apply_btn = SCButton(_t("Apply"), bottom, glow_color=P.green)
        apply_btn.setStyleSheet(_btn_qss(
            "#1a3020", "#1f3a28", P.green, font_size="8pt", padding="5px 14px",
        ))
        apply_btn.clicked.connect(self._on_apply_clicked)
        b_lay.addWidget(apply_btn)
        # Kept on the instance so a test can assert where it landed: it is the only control that
        # can undo a UI scale, so "is Apply on the screen" is the check that matters.
        self._apply_btn = apply_btn

        main_lay.addWidget(bottom)

    # ── Tab switching ─────────────────────────────────────────────────────

    def _select_tab(self, idx: int):
        self._current_tab = idx
        for i, btn in enumerate(self._tab_btns):
            obj = f"settingsTab_{i}"
            if i == idx:
                btn.setStyleSheet(f"""
                    QPushButton#{obj} {{
                        background-color: {P.bg_input};
                        color: {P.accent};
                        border: 1px solid {P.accent};
                        border-radius: 3px;
                        font-family: Consolas; font-size: 8pt; font-weight: bold;
                        padding: 2px 8px;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton#{obj} {{
                        background-color: {P.bg_card};
                        color: {P.fg_dim};
                        border: 1px solid {P.border};
                        border-radius: 3px;
                        font-family: Consolas; font-size: 8pt;
                        padding: 2px 8px;
                    }}
                    QPushButton#{obj}:hover {{
                        background-color: {P.bg_input};
                        color: {P.fg_bright};
                        border-color: {P.fg_dim};
                    }}
                """)
        for i, page in enumerate(self._tab_pages):
            page.setVisible(i == idx)

    # ── Tab 1: Tools ──────────────────────────────────────────────────────

    def _refresh_sc_path(self) -> None:
        try:
            from shared.sc_install import get_sc_root
            root = get_sc_root()
        except Exception:  # noqa: BLE001 - a status label must not break Settings
            root = None
        self._sc_path_desc.setText(root or _t("not linked \u2014 each tool finds it on its own"))
        self._sc_path_desc.setToolTip(root or "")

    def _change_sc_path(self) -> None:
        """Reopen the first-launch popup so the shared folder can be changed."""
        from shared import sc_install
        from shared.qt.sc_path_dialog import ScPathDialog
        dlg = ScPathDialog(self, detected=sc_install.get_or_detect_sc_root())
        dlg.exec()
        self._refresh_sc_path()

    def _build_tools_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("toolsPage")
        page.setStyleSheet(f"QWidget#toolsPage {{ background-color: {P.bg_secondary}; }}")

        scroll = QScrollArea(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {P.bg_primary}; width: 8px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {P.border}; min-height: 20px; border-radius: 4px;
            }}
        """)

        content = QWidget()
        content.setStyleSheet("background: transparent;")
        c_lay = QVBoxLayout(content)
        c_lay.setContentsMargins(12, 8, 12, 8)
        c_lay.setSpacing(2)

        # Hint
        hint = QLabel(_t("Format: <shift>+1  <ctrl>+F2  <alt>+q  F5"))
        hint.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        c_lay.addWidget(hint)

        # Column hint — clarifies the two toggles on each row
        col_hint = QLabel(_t("Left toggle = tool on/off    Right toggle = keybind on/off"))
        col_hint.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        c_lay.addWidget(col_hint)

        # Launcher hotkey row
        self._hotkey_entries: Dict[str, QLineEdit] = {}
        self._toggle_checks: Dict[str, QCheckBox] = {}
        self._keybind_checks: Dict[str, QCheckBox] = {}

        launcher_row = self._make_tool_row(c_lay, "launcher", "SC_Toolbox", self._launcher_hotkey, show_toggle=False)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        c_lay.addWidget(sep)

        # Skill rows
        for skill in self._skills:
            label = f"{skill.icon} {skill.name}"
            enabled = skill.id not in self._disabled
            self._make_tool_row(c_lay, skill.id, label, self._skill_hotkeys.get(skill.id, skill.hotkey), show_toggle=True, enabled=enabled)

        # ── Scroll on hover toggle ────────────────────────────────────────
        scroll_sep = QFrame()
        scroll_sep.setFixedHeight(1)
        scroll_sep.setStyleSheet(f"background-color: {P.border};")
        c_lay.addWidget(scroll_sep)

        scroll_row = QWidget()
        scroll_row.setFixedHeight(32)
        scroll_row.setStyleSheet("background: transparent;")
        scr_lay = QHBoxLayout(scroll_row)
        scr_lay.setSpacing(8)
        scr_lay.setContentsMargins(0, 0, 0, 0)

        scroll_lbl = QLabel(_t("Scroll on hover"))
        scroll_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        scr_lay.addWidget(scroll_lbl)

        scroll_desc = QLabel(_t("Mouse wheel adjusts sliders and spinboxes on hover"))
        scroll_desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        scr_lay.addWidget(scroll_desc, stretch=1)

        self._scroll_hover_check = QCheckBox()
        self._scroll_hover_check.setChecked(self._scroll_on_hover)
        self._scroll_hover_check.setStyleSheet(_TOGGLE_QSS)
        self._scroll_hover_check.toggled.connect(self._wheel_filter.set_enabled)
        scr_lay.addWidget(self._scroll_hover_check)

        c_lay.addWidget(scroll_row)

        # ── UI Scale ─────────────────────────────────────────────────────
        scale_sep = QFrame()
        scale_sep.setFixedHeight(1)
        scale_sep.setStyleSheet(f"background-color: {P.border};")
        c_lay.addWidget(scale_sep)

        scale_row = QWidget()
        scale_row.setFixedHeight(32)
        scale_row.setStyleSheet("background: transparent;")
        sc_lay = QHBoxLayout(scale_row)
        sc_lay.setSpacing(8)
        sc_lay.setContentsMargins(0, 0, 0, 0)

        scale_lbl = QLabel(_t("UI Scale"))
        scale_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        sc_lay.addWidget(scale_lbl)

        scale_desc = QLabel(_t("Scale all UI elements for high-res monitors"))
        scale_desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        sc_lay.addWidget(scale_desc, stretch=1)

        self._scale_combo = QComboBox()
        self._scale_combo.setStyleSheet(_COMBO_QSS)
        self._scale_combo.setFixedWidth(80)
        self._scale_combo.setFixedHeight(24)
        _scale_values = [0.75, 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
        # Only offer scales this monitor can still display Settings at.  The clamp in __init__
        # already guarantees Apply stays reachable, so this is not what rescues a stuck user — it
        # stops the choice being offered at all on a screen where the popup would be squeezed below
        # MIN_POPUP (380x420 device-independent px).  1080p tops out at 2x; 1440p and above keep
        # the full list.  The scale in force and the smallest choice are always kept: a combo that
        # hid the current value would misreport the state, and there must always be a way down.
        _offer = screen_fit.usable_ui_scales(
            _scale_values, self._avail, self._ui_scale,
            keep=(self._ui_scale, min(_scale_values)),
        )
        for val in _offer:
            self._scale_combo.addItem(f"{val:.2g}x", val)
        # The tip names the way out as well as the cap: closing this window with its [x] saves,
        # exactly like Apply, so a scale is undoable even from a build where Apply is off-screen.
        _tip = _t("Applies to every window. Closing Settings with [x] saves it too, like Apply.")
        if len(_offer) < len(_scale_values):
            _tip += "  " + _t("Larger scales are hidden: this screen cannot show Settings at them.")
            _tip += f"  ({max(_offer):.2g}x max)"
        self._scale_combo.setToolTip(_tip)
        idx = self._scale_combo.findData(self._ui_scale)
        if idx < 0:
            # Fall back to 1x by VALUE, not by position: the list above can be shorter than the
            # full eight on a small screen, and index 1 was only ever 1.0x by coincidence.
            idx = self._scale_combo.findData(1.0)
        self._scale_combo.setCurrentIndex(max(idx, 0))
        sc_lay.addWidget(self._scale_combo)

        c_lay.addWidget(scale_row)

        # ── Star Citizen folder (shared by every tool that reads Game.log) ──
        sc_sep = QFrame()
        sc_sep.setFixedHeight(1)
        sc_sep.setStyleSheet(f"background-color: {P.border};")
        c_lay.addWidget(sc_sep)

        sc_row = QWidget()
        sc_row.setFixedHeight(32)
        sc_row.setStyleSheet("background: transparent;")
        scp_lay = QHBoxLayout(sc_row)
        scp_lay.setSpacing(8)
        scp_lay.setContentsMargins(0, 0, 0, 0)

        scp_lbl = QLabel(_t("Star Citizen folder"))
        scp_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        scp_lay.addWidget(scp_lbl)

        self._sc_path_desc = QLabel()
        self._sc_path_desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        scp_lay.addWidget(self._sc_path_desc, stretch=1)

        sc_btn = QPushButton(_t("Change\u2026"))
        sc_btn.setCursor(Qt.PointingHandCursor)
        sc_btn.setFixedHeight(24)
        sc_btn.setStyleSheet(f"""
            QPushButton {{ background: {P.bg_input}; color: {P.fg};
                border: 1px solid {P.border}; border-radius: 3px;
                font-family: Consolas; font-size: 8pt; padding: 0 10px; }}
            QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.energy_cyan}; }}
        """)
        sc_btn.clicked.connect(self._change_sc_path)
        scp_lay.addWidget(sc_btn)
        c_lay.addWidget(sc_row)
        self._refresh_sc_path()

        # ── Tool voice: who speaks for the Starmap and the Assistant (J, 2026-09-26: "Elah as default but
        #    allow players to pick"). Shared setting, read on every spoken line, so it applies at once. ──
        tv_row = QWidget()
        tv_row.setFixedHeight(32)
        tv_row.setStyleSheet("background: transparent;")
        tv_lay = QHBoxLayout(tv_row)
        tv_lay.setSpacing(8)
        tv_lay.setContentsMargins(0, 0, 0, 0)
        tv_lbl = QLabel(_t("Tool voice"))
        tv_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        tv_lay.addWidget(tv_lbl)
        tv_desc = QLabel(_t("Who speaks for the Star Map and the Assistant"))
        tv_desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        tv_lay.addWidget(tv_desc, stretch=1)
        self._voice_combo = QComboBox()
        self._voice_combo.setStyleSheet(_COMBO_QSS)
        self._voice_combo.setFixedWidth(140)
        self._voice_combo.setFixedHeight(24)
        try:
            from shared import character_voice as _cv
            for key in _cv.VOICES:
                self._voice_combo.addItem(_t(_cv.LABELS[key]), key)
            i = self._voice_combo.findData(_cv.get_voice())
            self._voice_combo.setCurrentIndex(i if i >= 0 else 0)
            self._voice_combo.currentIndexChanged.connect(
                lambda _i: _cv.set_voice(self._voice_combo.currentData()))
        except Exception:
            log.exception("tool voice setting unavailable")
            self._voice_combo.setEnabled(False)
        tv_lay.addWidget(self._voice_combo)
        c_lay.addWidget(tv_row)

        # ── Microphone: which mic the ears of the Star Map, Assistant and SuitMk2 listen to (J, 2026-09-26:
        #    "nothing could hear me", headset mic). Stored by name; "Windows default" follows Windows. ──
        mic_row = QWidget()
        mic_row.setFixedHeight(32)
        mic_row.setStyleSheet("background: transparent;")
        mic_lay = QHBoxLayout(mic_row)
        mic_lay.setSpacing(8)
        mic_lay.setContentsMargins(0, 0, 0, 0)
        mic_lbl = QLabel(_t("Microphone"))
        mic_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        mic_lay.addWidget(mic_lbl)
        mic_desc = QLabel(_t("The mic the tools listen to"))
        mic_desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        mic_lay.addWidget(mic_desc, stretch=1)
        self._mic_combo = QComboBox()
        self._mic_combo.setStyleSheet(_COMBO_QSS)
        self._mic_combo.setFixedWidth(220)
        self._mic_combo.setFixedHeight(24)
        try:
            from shared import mic as _mic
            self._mic_combo.addItem(_t(_mic.DEFAULT_LABEL), "")
            for name in _mic.list_inputs(refresh=True):
                self._mic_combo.addItem(name, name)
            chosen = _mic.get_choice()
            if chosen and self._mic_combo.findData(chosen) < 0:
                self._mic_combo.addItem(chosen + "  " + _t("(not connected)"), chosen)
            i = self._mic_combo.findData(chosen)
            self._mic_combo.setCurrentIndex(i if i >= 0 else 0)
            self._mic_combo.currentIndexChanged.connect(
                lambda _i: _mic.set_choice(self._mic_combo.currentData() or ""))
        except Exception:
            log.exception("microphone setting unavailable")
            self._mic_combo.setEnabled(False)
        mic_lay.addWidget(self._mic_combo)
        c_lay.addWidget(mic_row)

        # ── Auto-hide launcher ────────────────────────────────────────────
        hide_sep = QFrame()
        hide_sep.setFixedHeight(1)
        hide_sep.setStyleSheet(f"background-color: {P.border};")
        c_lay.addWidget(hide_sep)

        hide_row = QWidget()
        hide_row.setFixedHeight(32)
        hide_row.setStyleSheet("background: transparent;")
        hide_lay = QHBoxLayout(hide_row)
        hide_lay.setSpacing(8)
        hide_lay.setContentsMargins(0, 0, 0, 0)

        hide_lbl = QLabel(_t("Hide launcher when tool is active"))
        hide_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        hide_lay.addWidget(hide_lbl)

        hide_desc = QLabel(_t("Auto-hide while any tool window is open"))
        hide_desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        hide_lay.addWidget(hide_desc, stretch=1)

        self._hide_on_tool_check = QCheckBox()
        self._hide_on_tool_check.setChecked(self._hide_on_tool_active)
        self._hide_on_tool_check.setStyleSheet(_TOGGLE_QSS)
        hide_lay.addWidget(self._hide_on_tool_check)

        c_lay.addWidget(hide_row)

        c_lay.addStretch(1)
        scroll.setWidget(content)

        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)
        return page

    def _make_tool_row(self, layout: QVBoxLayout, key: str, label: str, hotkey: str,
                       show_toggle: bool = True, enabled: bool = True) -> None:
        row_widget = QWidget()
        row_widget.setFixedHeight(36)
        row_widget.setStyleSheet("background: transparent;")
        row = QHBoxLayout(row_widget)
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)

        # Tool name
        lbl = QLabel(label)
        lbl.setMinimumWidth(130)
        lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        row.addWidget(lbl)

        # Toggle
        if show_toggle:
            toggle = QCheckBox()
            toggle.setChecked(enabled)
            toggle.setStyleSheet(_TOGGLE_QSS)
            toggle.setToolTip(_t("Enable") if enabled else _t("Disable"))
            row.addWidget(toggle)
            self._toggle_checks[key] = toggle

            # Enabled/Disabled label
            state_lbl = QLabel(_t("Enabled") if enabled else _t("Disabled"))
            state_lbl.setFixedWidth(54)
            state_lbl.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt;
                color: {P.green if enabled else P.red}; background: transparent;
            """)
            def _on_toggle(checked, sl=state_lbl):
                sl.setText(_t("Enabled") if checked else _t("Disabled"))
                color = P.green if checked else P.red
                sl.setStyleSheet(f"""
                    font-family: Consolas; font-size: 7pt;
                    color: {color}; background: transparent;
                """)
            toggle.toggled.connect(_on_toggle)
            row.addWidget(state_lbl)
        else:
            row.addStretch(0)

        # Keybind
        hk_label = QLabel(_t("Hotkey:"))
        hk_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {P.fg_dim}; background: transparent;
        """)
        row.addWidget(hk_label)

        entry = QLineEdit(hotkey)
        entry.setMinimumWidth(96)
        entry.setFixedHeight(24)
        entry.setStyleSheet(_LINE_EDIT_QSS)
        row.addWidget(entry, 1)

        # Keybind on/off toggle — disables the global hotkey without disabling
        # the tool itself.  Greys the entry and blanks the tile badge when off.
        kb_on = key not in self._keybinds_disabled
        entry.setEnabled(kb_on)

        kb_toggle = QCheckBox()
        kb_toggle.setChecked(kb_on)
        kb_toggle.setStyleSheet(_TOGGLE_QSS)
        kb_toggle.setToolTip(_t("Turn this keybind on or off"))
        row.addWidget(kb_toggle)

        kb_state = QLabel(_t("Key On") if kb_on else _t("Key Off"))
        kb_state.setFixedWidth(50)
        kb_state.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.green if kb_on else P.red}; background: transparent;
        """)

        def _on_kb_toggle(checked, sl=kb_state, e=entry):
            e.setEnabled(checked)
            sl.setText(_t("Key On") if checked else _t("Key Off"))
            color = P.green if checked else P.red
            sl.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt;
                color: {color}; background: transparent;
            """)
        kb_toggle.toggled.connect(_on_kb_toggle)
        row.addWidget(kb_state)

        layout.addWidget(row_widget)
        self._hotkey_entries[key] = entry
        self._keybind_checks[key] = kb_toggle

    # ── Tab 2: Grid Layout ────────────────────────────────────────────────

    def _build_grid_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("gridPage")
        page.setStyleSheet(f"QWidget#gridPage {{ background-color: {P.bg_secondary}; }}")

        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 8, 12, 8)
        outer.setSpacing(8)

        # Row/column controls
        ctrl_row = QHBoxLayout()
        ctrl_row.setSpacing(12)

        rows_lbl = QLabel(_t("Number of rows"))
        rows_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        ctrl_row.addWidget(rows_lbl)

        self._rows_spin = QSpinBox()
        self._rows_spin.setRange(1, 10)
        self._rows_spin.setValue(self._grid_rows)
        self._rows_spin.setStyleSheet(_SPIN_QSS)
        self._rows_spin.valueChanged.connect(self._rebuild_grid_preview)
        ctrl_row.addWidget(self._rows_spin)

        ctrl_row.addSpacing(16)

        cols_lbl = QLabel(_t("Number of columns"))
        cols_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        ctrl_row.addWidget(cols_lbl)

        self._cols_spin = QSpinBox()
        self._cols_spin.setRange(1, 10)
        self._cols_spin.setValue(self._grid_cols)
        self._cols_spin.setStyleSheet(_SPIN_QSS)
        self._cols_spin.valueChanged.connect(self._rebuild_grid_preview)
        ctrl_row.addWidget(self._cols_spin)

        ctrl_row.addStretch(1)
        outer.addLayout(ctrl_row)

        # Grid preview area (scrollable)
        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        outer.addWidget(sep)

        grid_hint = QLabel(_t("Click a cell to assign a tool to that grid position."))
        grid_hint.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        outer.addWidget(grid_hint)

        self._grid_scroll = QScrollArea()
        self._grid_scroll.setWidgetResizable(True)
        self._grid_scroll.setFrameShape(QFrame.NoFrame)
        self._grid_scroll.setStyleSheet(f"""
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {P.bg_primary}; width: 8px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {P.border}; min-height: 20px; border-radius: 4px;
            }}
            QScrollBar:horizontal {{
                background: {P.bg_primary}; height: 8px; border: none;
            }}
            QScrollBar::handle:horizontal {{
                background: {P.border}; min-width: 20px; border-radius: 4px;
            }}
        """)
        outer.addWidget(self._grid_scroll, stretch=1)

        self._grid_combos: Dict[str, QComboBox] = {}
        self._rebuild_grid_preview()
        return page

    def _rebuild_grid_preview(self):
        rows = self._rows_spin.value()
        cols = self._cols_spin.value()

        container = QWidget()
        container.setStyleSheet("background: transparent;")
        grid = QGridLayout(container)
        grid.setSpacing(4)

        self._grid_combos.clear()

        # Build skill choices: "(Empty)" + each skill
        skill_choices = [("", _t("(Empty)"))]
        for s in self._skills:
            skill_choices.append((s.id, f"{s.icon} {s.name}"))

        for r in range(rows):
            for c in range(cols):
                cell_key = f"{r},{c}"
                combo = QComboBox()
                combo.setStyleSheet(_COMBO_QSS)
                combo.setFixedHeight(28)
                combo.setMinimumWidth(60)

                for sid, display in skill_choices:
                    combo.addItem(display, sid)

                # Restore saved assignment
                saved_sid = self._grid_layout.get(cell_key, "")
                if saved_sid:
                    idx = combo.findData(saved_sid)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)

                grid.addWidget(combo, r, c)
                self._grid_combos[cell_key] = combo

        # Equal stretch
        for c in range(cols):
            grid.setColumnStretch(c, 1)
        for r in range(rows):
            grid.setRowStretch(r, 0)

        self._grid_scroll.setWidget(container)

    # ── Tab 3: Language ───────────────────────────────────────────────────

    def _build_language_tab(self) -> QWidget:
        page = QWidget()
        page.setObjectName("langPage")
        page.setStyleSheet(f"QWidget#langPage {{ background-color: {P.bg_secondary}; }}")

        outer = QVBoxLayout(page)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(8)

        lang_hdr = QLabel(_t("LANGUAGE"))
        lang_hdr.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 9pt; font-weight: bold;
            color: {P.accent}; background: transparent;
            letter-spacing: 2px;
        """)
        outer.addWidget(lang_hdr)

        desc = QLabel(_t("Select the display language for all tools."))
        desc.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {P.fg_dim}; background: transparent;
        """)
        outer.addWidget(desc)

        lang_row = QHBoxLayout()
        lang_row.setSpacing(8)

        self._lang_combo = QComboBox()
        self._lang_combo.setStyleSheet(_COMBO_QSS)
        self._lang_combo.setMinimumWidth(180)
        self._lang_combo.setFixedHeight(28)
        for code in self._available_langs:
            self._lang_combo.addItem(_lang_display(code), code)
        idx = self._lang_combo.findData(self._language)
        if idx >= 0:
            self._lang_combo.setCurrentIndex(idx)
        lang_row.addWidget(self._lang_combo)
        lang_row.addStretch(1)

        outer.addLayout(lang_row)
        outer.addStretch(1)
        return page

    # ── Apply ─────────────────────────────────────────────────────────────

    def _collect_and_validate(self) -> Optional[dict]:
        """Gather every control's value into a settings dict and validate the
        hotkeys.  Returns the dict, or ``None`` if a hotkey is invalid (in which
        case a status message is shown)."""
        result: dict = {}

        # Launcher hotkey
        result["hotkey_launcher"] = self._hotkey_entries.get("launcher", QLineEdit()).text().strip()

        # Skill hotkeys
        skill_hotkeys: Dict[str, str] = {}
        for skill in self._skills:
            entry = self._hotkey_entries.get(skill.id)
            if entry:
                skill_hotkeys[skill.id] = entry.text().strip()
        result["skill_hotkeys"] = skill_hotkeys

        # Disabled skills
        disabled: list[str] = []
        for skill in self._skills:
            toggle = self._toggle_checks.get(skill.id)
            if toggle and not toggle.isChecked():
                disabled.append(skill.id)
        result["disabled_skills"] = disabled

        # Disabled keybinds (per-tool hotkey on/off; keys include "launcher")
        keybinds_disabled: list[str] = []
        for kb_key, chk in self._keybind_checks.items():
            if not chk.isChecked():
                keybinds_disabled.append(kb_key)
        result["keybinds_disabled"] = keybinds_disabled

        # Grid settings
        result["grid_rows"] = self._rows_spin.value()
        result["grid_cols"] = self._cols_spin.value()

        grid_layout: Dict[str, str] = {}
        for cell_key, combo in self._grid_combos.items():
            sid = combo.currentData()
            if sid:
                grid_layout[cell_key] = sid
        result["grid_layout"] = grid_layout

        # Scroll on hover
        result["scroll_on_hover"] = self._scroll_hover_check.isChecked()

        # Language
        result["language"] = self._lang_combo.currentData() or "en"

        # UI Scale
        result["ui_scale"] = self._scale_combo.currentData() or 1.0

        # Auto-hide launcher
        result["hide_on_tool_active"] = self._hide_on_tool_check.isChecked()

        # Validate hotkeys
        for key, val in [("launcher", result["hotkey_launcher"])] + [(k, v) for k, v in skill_hotkeys.items()]:
            if val and not any(c.isalnum() or c in "`~!@#$%^&*" for c in val):
                self._show_status(f"\u2717 {_t('Invalid hotkey')}: {val}", P.red)
                return None

        return result

    def _on_apply_clicked(self):
        result = self._collect_and_validate()
        if result is None:
            return
        # Mark committed so closeEvent doesn't try to save a second time.
        self._committed = True
        # Close popup before relaunch so it doesn't float orphaned after the
        # parent launcher window has already closed.
        self.close()
        if self._on_apply:
            self._on_apply(result)

    def _on_cancel_clicked(self):
        """Discard changes and close without saving."""
        self._cancelled = True
        self.close()

    def _remove_wheel_filter(self):
        """Detach the app-level wheel filter so it doesn't outlive the popup."""
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app:
                app.removeEventFilter(self._wheel_filter)
        except Exception:
            pass

    def closeEvent(self, event):
        """Save settings when the popup closes \u2014 closing via the [x] button (or
        the window manager) commits the current values, exactly like Apply.
        Only the Cancel button discards.  If a hotkey is invalid the close is
        blocked so the user can correct it (or hit Cancel to abandon).

        During application shutdown we skip the save: applying would rebuild the
        launcher window and resurrect the UI as the app is trying to quit."""
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        shutting_down = bool(app and app.closingDown())

        if not self._committed and not self._cancelled and not shutting_down:
            result = self._collect_and_validate()
            if result is None:
                event.ignore()  # keep open until the invalid hotkey is fixed
                return
            if self._baseline is not None and result == self._baseline:
                # Nothing changed — close without a needless save + UI rebuild.
                self._remove_wheel_filter()
                super().closeEvent(event)
                return
            self._committed = True
            self._remove_wheel_filter()
            if self._on_apply:
                self._on_apply(result)
            event.accept()
            return
        self._remove_wheel_filter()
        super().closeEvent(event)

    def _show_status(self, msg: str, color: str):
        self._status_label.setText(msg)
        self._status_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {color}; background: transparent;
        """)
