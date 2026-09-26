"""Dialog to view / override the Star Citizen game resolution.

The region detectors use the game's output resolution to scale their
finders. It's auto-detected from ``Game.log`` (or the desktop), but the
user can pin an exact value here — useful if the log isn't found, or
they capture a monitor that differs from where SC renders.
"""
from __future__ import annotations

import logging
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QLabel, QSpinBox, QVBoxLayout,
)

from .game_resolution import get_game_resolution

log = logging.getLogger(__name__)

# Common PC display resolutions offered as one-click presets.
_PRESETS: list[tuple[str, int, int]] = [
    ("1920 × 1080  (1080p)", 1920, 1080),
    ("2560 × 1440  (1440p)", 2560, 1440),
    ("3840 × 2160  (4K)", 3840, 2160),
    ("2560 × 1080  (UW 1080p)", 2560, 1080),
    ("3440 × 1440  (UW 1440p)", 3440, 1440),
    ("1280 × 720  (720p)", 1280, 720),
]


class GameResolutionDialog(QDialog):
    """View the detected resolution and optionally pin a manual value.

    On accept, :meth:`result_override` returns the value to store in
    ``config["game_resolution"]`` — a ``{"w","h"}`` dict for a manual
    pin, or ``None`` to mean "auto-detect".
    """

    def __init__(self, config: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Game Resolution")
        self.setMinimumWidth(360)
        self._config = config or {}

        # What auto-detection alone (ignoring any manual override) finds,
        # so the user can see what they'd get by leaving it on Auto.
        cfg_no_override = dict(self._config)
        cfg_no_override.pop("game_resolution", None)
        auto = get_game_resolution(cfg_no_override)
        self._auto = auto

        override = self._config.get("game_resolution")
        has_override = (
            isinstance(override, dict)
            and int(override.get("w") or 0) > 0
            and int(override.get("h") or 0) > 0
        )

        v = QVBoxLayout(self)
        v.setSpacing(10)

        src_label = {
            "manual": "manual override",
            "game.log": "Star Citizen Game.log",
            "desktop": "desktop resolution",
            "unknown": "could not detect",
        }.get(auto.get("source", "unknown"), auto.get("source", ""))
        if auto["w"] > 0:
            info = (
                f"Auto-detected: <b>{auto['w']} × {auto['h']}</b>"
                f"  <span style='color:#888;'>(from {src_label})</span>"
            )
        else:
            info = (
                "Auto-detect could not find a resolution "
                "<span style='color:#888;'>(no Game.log / desktop info)"
                "</span>"
            )
        lbl = QLabel(info)
        lbl.setTextFormat(Qt.RichText)
        lbl.setWordWrap(True)
        v.addWidget(lbl)

        self._auto_cb = QCheckBox("Auto-detect (use Game.log / desktop)")
        self._auto_cb.setChecked(not has_override)
        self._auto_cb.toggled.connect(self._on_auto_toggled)
        v.addWidget(self._auto_cb)

        form = QFormLayout()
        form.setSpacing(6)

        # Preset picker — fills the spin boxes; "Custom…" leaves them be.
        self._preset = QComboBox()
        self._preset.addItem("Custom…", None)
        for label, pw, ph in _PRESETS:
            self._preset.addItem(label, (pw, ph))
        self._preset.currentIndexChanged.connect(self._on_preset_picked)
        form.addRow("Preset:", self._preset)

        wh_row = QHBoxLayout()
        self._w = QSpinBox()
        self._w.setRange(640, 7680)
        self._w.setSingleStep(10)
        self._h = QSpinBox()
        self._h.setRange(480, 4320)
        self._h.setSingleStep(10)
        # Seed the spin boxes with the current effective resolution.
        seed_w = int(override["w"]) if has_override else (auto["w"] or 1920)
        seed_h = int(override["h"]) if has_override else (auto["h"] or 1080)
        self._w.setValue(seed_w)
        self._h.setValue(seed_h)
        wh_row.addWidget(self._w)
        wh_row.addWidget(QLabel("×"))
        wh_row.addWidget(self._h)
        wh_row.addStretch(1)
        form.addRow("Resolution:", wh_row)
        v.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        v.addWidget(buttons)

        # Reflect the initial enabled/disabled state.
        self._on_auto_toggled(self._auto_cb.isChecked())

    def _on_auto_toggled(self, auto_on: bool) -> None:
        for w in (self._preset, self._w, self._h):
            w.setEnabled(not auto_on)

    def _on_preset_picked(self, _idx: int) -> None:
        data = self._preset.currentData()
        if data is not None:
            self._w.setValue(int(data[0]))
            self._h.setValue(int(data[1]))

    def result_override(self) -> Optional[dict]:
        """Value to store in ``config["game_resolution"]``: a
        ``{"w","h"}`` manual pin, or ``None`` for auto-detect."""
        if self._auto_cb.isChecked():
            return None
        return {"w": int(self._w.value()), "h": int(self._h.value())}
