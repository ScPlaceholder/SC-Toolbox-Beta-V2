"""VoiceControlBar — the interactive voice strip for the Starmap panel.

The one place the user talks to the map:

  * Ears            — arm / disarm the voice ears (right-click: whisper
                      model + command help)
  * Mic mode        — Always on / Push-to-talk / Toggle (exclusive)
  * Set Mic Keybind — press-anything capture (keyboard, mouse, joystick,
                      gamepad)
  * Calibrate Star Map — the set_route 3-click in-game calibration
  * Voice Replies   — spoken confirmations through the Mouth TTS
  * status line     — what was heard and what the engine is doing
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QButtonGroup, QHBoxLayout, QLabel, QPushButton, QWidget,
)

from shared.qt.theme import P

ACCENT = P.energy_cyan


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 4px 12px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }} "
        f"QPushButton:checked {{ color: {ACCENT}; border-color: {ACCENT}; }}"
    )


class VoiceControlBar(QWidget):
    """Calibrate / keybind / mic-mode / voice-reply controls + status."""

    # J 2026-09-26: ears always on; the player picks push-to-talk or always on.
    _MODES = (("Push-to-talk", "push"), ("Always on", "always"))

    def __init__(self, panel) -> None:
        super().__init__(panel)
        self._panel = panel
        self.setStyleSheet(f"background: {P.bg_secondary};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 5, 8, 5)
        lay.setSpacing(8)

        # ── ears armed state (never shown: the ears are always on) ──────────
        self._btn_ears = QPushButton("Ears", self)
        self._btn_ears.setCheckable(True)
        self._btn_ears.setVisible(False)
        self._btn_ears.toggled.connect(panel._arm_ears)
        panel._btn_ears = self._btn_ears       # the panel's arm / tooltip code uses it
        # the old right-click menu (trigger, whisper model, help) moves to the bar
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(panel._ears_menu)

        # ── mic mode (exclusive) ───────────────────────────────────────────
        mic_lbl = QLabel("Mic:")
        mic_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
        lay.addWidget(mic_lbl)
        self._mode_btns = {}
        self._mode_group = QButtonGroup(self)
        self._mode_group.setExclusive(True)
        for label, value in self._MODES:
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setCheckable(True)
            b.setStyleSheet(_btn_ss())
            self._mode_group.addButton(b)
            self._mode_btns[value] = b
            b.toggled.connect(lambda on, v=value: on and panel._set_mode(v))
            lay.addWidget(b)

        # ── keybind + calibration + spoken replies ─────────────────────────
        self._btn_keybind = QPushButton("Set Mic Keybind")
        self._btn_keybind.setCursor(Qt.PointingHandCursor)
        self._btn_keybind.setStyleSheet(_btn_ss())
        self._btn_keybind.setToolTip("Press any key, mouse button, joystick or gamepad button to trigger the ears")
        self._btn_keybind.clicked.connect(panel._pick_binding)
        lay.addWidget(self._btn_keybind)

        self._btn_calibrate = QPushButton("Calibrate Star Map")
        self._btn_calibrate.setCursor(Qt.PointingHandCursor)
        self._btn_calibrate.setStyleSheet(_btn_ss())
        self._btn_calibrate.setToolTip(
            "3-click calibration of the in-game route setter (opens the game starmap steps)")
        self._btn_calibrate.clicked.connect(panel._calibrate)
        lay.addWidget(self._btn_calibrate)

        self._btn_replies = QPushButton("Voice Replies")
        self._btn_replies.setCursor(Qt.PointingHandCursor)
        self._btn_replies.setCheckable(True)
        self._btn_replies.setStyleSheet(_btn_ss())
        self._btn_replies.setToolTip("Speak confirmations aloud (Navigate to ...)")
        self._btn_replies.toggled.connect(panel._set_voice_replies)
        lay.addWidget(self._btn_replies)

        # ── status line ────────────────────────────────────────────────────
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
        lay.addWidget(self._status, 1)

    # ── API used by the panel ──────────────────────────────────────────────
    def set_status(self, msg: str) -> None:
        self._status.setText(msg or "")

    def sync_mode(self, mode: str) -> None:
        btn = self._mode_btns.get(mode)
        if btn is not None and not btn.isChecked():
            btn.blockSignals(True)
            btn.setChecked(True)
            btn.blockSignals(False)

    def set_replies(self, on: bool) -> None:
        if self._btn_replies.isChecked() != bool(on):
            self._btn_replies.blockSignals(True)
            self._btn_replies.setChecked(bool(on))
            self._btn_replies.blockSignals(False)

    def mode(self) -> str:
        for value, btn in self._mode_btns.items():
            if btn.isChecked():
                return value
        return "toggle"

    def replies_enabled(self) -> bool:
        return self._btn_replies.isChecked()
