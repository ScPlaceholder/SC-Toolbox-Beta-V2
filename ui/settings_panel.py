"""
Collapsible settings panel — PySide6 MobiGlas implementation.

Keybind editing, language selection, with animated slide-in/out.
"""
import logging
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QFrame,
    QSizePolicy, QComboBox,
)

from shared.config_models import SkillConfig
from shared.i18n import _ as _t
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
    """Return a display name for a language code."""
    return _LANG_NAMES.get(code, code.upper())


def _btn_qss(bg: str, bg_hover: str, color: str, color_hover: str = P.fg_bright,
             font_size: str = "9pt", padding: str = "8px 12px") -> str:
    """Generate a QPushButton stylesheet with normal and hover states."""
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


class SettingsPanel(QWidget):
    """Collapsible settings panel with language selection and keybind editing."""

    def __init__(
        self,
        parent: QWidget,
        skills: List[SkillConfig],
        launcher_hotkey: str,
        on_apply: Callable[[str, Dict[str, str]], None],
        current_language: str = "en",
        available_languages: Optional[List[str]] = None,
        on_language_change: Optional[Callable[[str], None]] = None,
    ) -> None:
        super().__init__(parent)
        self._skills = skills
        self._on_apply = on_apply
        self._on_language_change = on_language_change
        self._visible = False
        self._available_langs = available_languages or ["en"]

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ── Content frame (hidden initially) ──
        self._content = QFrame(self)
        self._content.setStyleSheet(f"background-color: {P.bg_secondary};")
        self._content.setMaximumHeight(0)
        self._content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        c_layout = QVBoxLayout(self._content)
        c_layout.setContentsMargins(12, 8, 12, 8)
        c_layout.setSpacing(2)

        # ── Keybinds section ──
        hdr = QLabel(_t("KEYBINDS"), self._content)
        hdr.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 9pt; font-weight: bold;
            color: {P.accent}; background: transparent;
            letter-spacing: 2px;
        """)
        c_layout.addWidget(hdr)

        hint = QLabel(_t("Format: <shift>+1  <ctrl>+F2  <alt>+q  F5"), self._content)
        hint.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        c_layout.addWidget(hint)

        # Keybind entries
        self._entries: Dict[str, QLineEdit] = {}
        self._add_row(c_layout, "launcher", "SC_Toolbox", launcher_hotkey)
        for skill in skills:
            label = f"{skill.icon} {skill.name}"
            self._add_row(c_layout, skill.id, label, skill.hotkey)

        # Apply button + status
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._status_label = QLabel("", self._content)
        self._status_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {P.green}; background: transparent;
        """)
        btn_row.addWidget(self._status_label, stretch=1)

        apply_btn = SCButton(_t("Apply Hotkeys"), self._content, glow_color=P.accent)
        apply_btn.setStyleSheet(_btn_qss(
            "#1a3020", "#1f3a28", P.accent, font_size="8pt", padding="5px 12px",
        ))
        apply_btn.clicked.connect(self._on_apply_clicked)
        btn_row.addWidget(apply_btn)

        c_layout.addLayout(btn_row)

        # Separator
        sep = QFrame(self._content)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background-color: {P.border};")
        c_layout.addWidget(sep)

        note = QLabel(_t("Window positions are stored per-skill in settings."), self._content)
        note.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_disabled}; background: transparent;
        """)
        c_layout.addWidget(note)

        # ── Language section ──
        lang_sep = QFrame(self._content)
        lang_sep.setFixedHeight(1)
        lang_sep.setStyleSheet(f"background-color: {P.border};")
        c_layout.addWidget(lang_sep)

        lang_hdr = QLabel(_t("LANGUAGE"), self._content)
        lang_hdr.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 9pt; font-weight: bold;
            color: {P.accent}; background: transparent;
            letter-spacing: 2px;
        """)
        c_layout.addWidget(lang_hdr)

        lang_row = QWidget(self._content)
        lang_row.setFixedHeight(32)
        lang_row.setStyleSheet("background: transparent;")
        lr = QHBoxLayout(lang_row)
        lr.setSpacing(8)
        lr.setContentsMargins(0, 0, 0, 0)

        self._lang_combo = QComboBox(lang_row)
        for code in self._available_langs:
            self._lang_combo.addItem(_lang_display(code), code)
        idx = self._lang_combo.findData(current_language)
        if idx >= 0:
            self._lang_combo.setCurrentIndex(idx)
        self._lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        self._lang_combo.setMinimumWidth(160)
        self._lang_combo.setFixedHeight(24)
        lr.addWidget(self._lang_combo)

        self._lang_status = QLabel("", lang_row)
        self._lang_status.setStyleSheet(f"""
            font-family: Consolas; font-size: 7pt;
            color: {P.fg_dim}; background: transparent;
        """)
        lr.addWidget(self._lang_status, stretch=1)

        c_layout.addWidget(lang_row)

        main_layout.addWidget(self._content)

        # ── Toggle button ──
        self._toggle_btn = SCButton("\u2699 " + _t("Settings & Keybinds"), self, glow_color=P.accent)
        self._toggle_btn.setStyleSheet(_btn_qss("#1a2538", "#223050", P.accent))
        self._toggle_btn.clicked.connect(self.toggle)
        main_layout.addWidget(self._toggle_btn)

        # Animation
        self._anim = QPropertyAnimation(self._content, b"maximumHeight", self)
        self._anim.setDuration(200)
        self._anim.setEasingCurve(QEasingCurve.InOutQuad)

        # Calculate expanded height: lang header + lang row + sep + keybind header + hint + rows + button row + sep + note + padding
        num_rows = 1 + len(skills)  # launcher + each skill
        self._expanded_height = 40 + 38 + 10 + 40 + 20 + (num_rows * 38) + 40 + 10 + 20 + 30

    def _add_row(self, layout: QVBoxLayout, key: str, label: str, value: str) -> None:
        row_widget = QWidget(self._content)
        row_widget.setFixedHeight(32)
        row_widget.setStyleSheet("background: transparent;")
        row = QHBoxLayout(row_widget)
        row.setSpacing(8)
        row.setContentsMargins(0, 0, 0, 0)

        lbl = QLabel(label, row_widget)
        lbl.setMinimumWidth(160)
        lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg}; background: transparent;
        """)
        row.addWidget(lbl)

        hk_label = QLabel(_t("Hotkey:"), row_widget)
        hk_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {P.fg_dim}; background: transparent;
        """)
        row.addWidget(hk_label)

        entry = QLineEdit(value, row_widget)
        entry.setMinimumWidth(130)
        entry.setFixedHeight(24)
        row.addWidget(entry, 1)

        layout.addWidget(row_widget)
        self._entries[key] = entry
        layout.addLayout(row)

    def _on_lang_changed(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if code and self._on_language_change:
            self._on_language_change(code)
            self._lang_status.setText(_t("Restart tools to apply"))
            self._lang_status.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt;
                color: {P.orange}; background: transparent;
            """)
            QTimer.singleShot(5000, lambda: self._lang_status.setText(""))

    def _on_apply_clicked(self) -> None:
        new_launcher = self._entries["launcher"].text().strip()
        new_skills: Dict[str, str] = {}
        for skill in self._skills:
            new_skills[skill.id] = self._entries[skill.id].text().strip()

        # Validate
        for key, val in [("launcher", new_launcher)] + list(new_skills.items()):
            if val and not any(c.isalnum() or c in "`~!@#$%^&*" for c in val):
                self._show_status(f"\u2717 {_t('Invalid hotkey')}: {val}", P.red, 3000)
                return

        try:
            self._on_apply(new_launcher, new_skills)
            self._show_status(f"\u2713 {_t('Hotkeys applied')}", P.green, 2000)
        except Exception as exc:  # broad catch intentional: external callback may raise anything
            log.error("settings_panel: apply failed: %s", exc)
            self._show_status(f"\u2717 {_t('Error')}: {exc}", P.red, 3000)

    def _show_status(self, msg: str, color: str, clear_ms: int) -> None:
        self._status_label.setText(msg)
        self._status_label.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt;
            color: {color}; background: transparent;
        """)
        QTimer.singleShot(clear_ms, lambda: self._status_label.setText(""))

    def toggle(self) -> None:
        if self._visible:
            # Collapse
            self._anim.stop()
            self._anim.setStartValue(self._content.maximumHeight())
            self._anim.setEndValue(0)
            self._anim.start()
            self._visible = False
            self._toggle_btn.setText("\u2699 " + _t("Settings & Keybinds"))
            self._toggle_btn.setStyleSheet(_btn_qss("#1a2538", "#223050", P.accent))
        else:
            # Expand
            self._anim.stop()
            self._anim.setStartValue(0)
            self._anim.setEndValue(self._expanded_height)
            self._anim.start()
            self._visible = True
            self._toggle_btn.setText("\u25b2 " + _t("Close Settings"))
            self._toggle_btn.setStyleSheet(_btn_qss("#2a1a18", "#3a2a28", P.orange))

    def get_entries(self) -> Dict[str, str]:
        return {k: e.text().strip() for k, e in self._entries.items()}
