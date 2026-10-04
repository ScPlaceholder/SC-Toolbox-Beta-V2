"""CommandBar - the Star Map's command strip.

The Star Map does not listen. Voice-to-text lives in ONE place, the AI
Assistant (J, 2026-10-04); what the Assistant hears for the map arrives here
as text and runs through the same :class:`~.commands.CommandRouter` as a line
typed into this bar.

  * command box        - type what you would say: "navigate to Area 18",
                         "zoom in", "clear route", "help"
  * Calibrate Star Map - the set_route 3-click in-game calibration
  * status line        - what the map was told and what it did

Until 2026-10-04 this file was voice_control.py and carried the mic controls
(push-to-talk / always on, mic keybind, voice replies). Those are the
Assistant's now; a Star Map that owned its own always-on microphone was
arming it every time the map was opened, including as a tab of the
Everything Finder.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton, QWidget

from shared.qt.theme import P

ACCENT = P.energy_cyan

PLACEHOLDER = ("Type a command: navigate to Area 18, zoom in, clear route, help"
               "   -   or say it to the Assistant")


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 4px 12px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }}"
    )


class CommandBar(QWidget):
    """Typed commands + calibration + the status line. No microphone."""

    def __init__(self, panel) -> None:
        super().__init__(panel)
        self._panel = panel
        self.setStyleSheet(f"background: {P.bg_secondary};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(8)

        self._input = QLineEdit()
        self._input.setPlaceholderText(PLACEHOLDER)
        self._input.setMinimumWidth(260)
        self._input.setToolTip(
            "Runs a map command. The Assistant sends the same commands when you say them:\n"
            "voice input lives in the Assistant, not in the Star Map.")
        self._input.setStyleSheet(
            f"QLineEdit {{ background: {P.bg_card}; color: {P.fg_bright}; "
            f"border: 1px solid {P.border}; padding: 3px 8px; "
            f"font-family: Consolas; font-size: 9pt; }} "
            f"QLineEdit:focus {{ border-color: {ACCENT}; }}")
        self._input.returnPressed.connect(self._submit)
        lay.addWidget(self._input, 2)

        self._btn_calibrate = QPushButton("Calibrate Star Map")
        self._btn_calibrate.setCursor(Qt.PointingHandCursor)
        self._btn_calibrate.setStyleSheet(_btn_ss())
        self._btn_calibrate.setToolTip(
            "3-click calibration of the in-game route setter (opens the game starmap steps)")
        self._btn_calibrate.clicked.connect(panel._calibrate)
        lay.addWidget(self._btn_calibrate)

        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
        lay.addWidget(self._status, 3)

    def _submit(self) -> None:
        text = self._input.text().strip()
        if not text:
            return
        self._input.clear()
        self._panel.run_command(text)

    # API used by the panel
    def set_status(self, msg: str) -> None:
        self._status.setText(msg or "")

    def status(self) -> str:
        return self._status.text()
