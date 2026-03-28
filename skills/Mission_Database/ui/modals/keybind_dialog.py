"""Keybind capture dialog (PySide6)."""
from __future__ import annotations
from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QWidget,
)
from PySide6.QtGui import QKeyEvent

from shared.qt.theme import P


class KeybindDialog(QDialog):
    """Modal dialog to capture a keyboard shortcut."""

    def __init__(self, parent, current_keybind: str, on_save: Callable, on_clear: Callable):
        super().__init__(parent)
        self._on_save = on_save
        self._on_clear = on_clear
        self._captured = ""

        self.setWindowTitle("Set Hotkey")
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setFixedSize(300, 150)
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {P.bg_primary};
                border: 1px solid {P.border};
            }}
        """)

        # Center on parent
        if parent:
            pg = parent.geometry()
            x = pg.x() + (pg.width() - 300) // 2
            y = pg.y() + (pg.height() - 150) // 2
            self.move(max(0, x), max(0, y))

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(6)

        title = QLabel("Press any key combination...")
        title.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.fg}; background: transparent;")
        title.setAlignment(Qt.AlignCenter)
        layout.addWidget(title)

        current = current_keybind or "None"
        cur_lbl = QLabel(f"Current: {current}")
        cur_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        cur_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(cur_lbl)

        self._capture_lbl = QLabel("Waiting for keypress...")
        self._capture_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt; color: {P.accent};
            background-color: {P.bg_card}; padding: 8px;
        """)
        self._capture_lbl.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._capture_lbl)

        # Buttons
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)

        save_btn = QPushButton("Save")
        save_btn.setCursor(Qt.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{ background-color: #1a3020; color: {P.green}; border: none;
                          font-family: Consolas; font-size: 8pt; font-weight: bold; padding: 3px 12px; }}
            QPushButton:hover {{ background-color: #1a4030; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {P.bg_card}; color: {P.fg_dim}; border: none;
                          font-family: Consolas; font-size: 8pt; padding: 3px 12px; }}
            QPushButton:hover {{ background-color: {P.bg_input}; }}
        """)
        clear_btn.clicked.connect(self._clear)
        btn_row.addWidget(clear_btn)

        btn_row.addStretch(1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.setCursor(Qt.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{ background-color: {P.bg_card}; color: {P.fg_dim}; border: none;
                          font-family: Consolas; font-size: 8pt; padding: 3px 12px; }}
            QPushButton:hover {{ background-color: {P.bg_input}; }}
        """)
        cancel_btn.clicked.connect(self.close)
        btn_row.addWidget(cancel_btn)

        layout.addLayout(btn_row)

    def keyPressEvent(self, event: QKeyEvent):
        parts = []
        mods = event.modifiers()
        if mods & Qt.ControlModifier:
            parts.append("Control")
        if mods & Qt.ShiftModifier:
            parts.append("Shift")
        if mods & Qt.AltModifier:
            parts.append("Alt")

        key = event.key()
        # Ignore pure modifier keys
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return

        key_text = event.text().upper() if event.text() and event.text().isalnum() else ""
        if not key_text:
            # Try to get key name from Qt
            key_name = {
                Qt.Key_F1: "F1", Qt.Key_F2: "F2", Qt.Key_F3: "F3", Qt.Key_F4: "F4",
                Qt.Key_F5: "F5", Qt.Key_F6: "F6", Qt.Key_F7: "F7", Qt.Key_F8: "F8",
                Qt.Key_F9: "F9", Qt.Key_F10: "F10", Qt.Key_F11: "F11", Qt.Key_F12: "F12",
                Qt.Key_Space: "space", Qt.Key_Tab: "Tab",
                Qt.Key_Home: "Home", Qt.Key_End: "End",
                Qt.Key_Insert: "Insert", Qt.Key_Delete: "Delete",
            }.get(key, "")
            if key_name:
                parts.append(key_name)
        else:
            parts.append(key_text.lower())

        if parts:
            combo = "-".join(parts)
            self._captured = combo
            self._capture_lbl.setText(combo)

    def _save(self):
        if self._captured:
            self._on_save(self._captured)
        self.close()

    def _clear(self):
        self._on_clear()
        self.close()


def show_keybind_dialog(parent, current_keybind: str, on_save: Callable, on_clear: Callable):
    """Open a modal to capture a new keybind."""
    dlg = KeybindDialog(parent, current_keybind, on_save, on_clear)
    dlg.exec()
