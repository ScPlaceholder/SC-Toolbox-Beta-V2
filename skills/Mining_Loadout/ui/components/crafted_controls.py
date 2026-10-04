"""Crafted-laser controls for one turret: a tick box and one quality per part.

The parts (which ingredient groups change Laser Power, their names, materials
and quality ranges) come from the blueprint data through
``shared.mining_crafting``; nothing about them is hardcoded here.
"""
from typing import Dict, List, Optional

import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QSpinBox,
)

from shared.qt.theme import P
from shared import mining_crafting as mc


class CraftedControls(QWidget):
    """Tick box + per-part quality inputs + the resulting power percentage."""

    changed = Signal()

    def __init__(self, index: Optional[Dict[str, dict]] = None, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._index: Dict[str, dict] = index or {}
        self._laser: str = ""
        self._groups: List[dict] = []
        self._spins: Dict[str, QSpinBox] = {}

        self.setStyleSheet("background: transparent;")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(2)

        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(6)
        self._check = QCheckBox(_("CRAFTED"))
        self._check.setStyleSheet(f"""
            QCheckBox {{
                font-family: Consolas;
                font-size: 7pt;
                color: {P.fg_dim};
                background: transparent;
            }}
            QCheckBox:disabled {{
                color: {P.border};
            }}
        """)
        self._check.setCursor(Qt.PointingHandCursor)
        self._check.toggled.connect(self._on_toggled)
        top.addWidget(self._check)
        top.addStretch(1)
        self._pct = QLabel("")
        self._pct.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            font-weight: bold;
            color: {P.tool_mining};
            background: transparent;
        """)
        top.addWidget(self._pct)
        lay.addLayout(top)

        self._rows = QWidget()
        self._rows.setStyleSheet("background: transparent;")
        self._rows_lay = QVBoxLayout(self._rows)
        self._rows_lay.setContentsMargins(0, 0, 0, 0)
        self._rows_lay.setSpacing(2)
        lay.addWidget(self._rows)
        self._rows.setVisible(False)

        if not self._index:
            # No blueprint data on this machine: the feature is simply not offered.
            self.setVisible(False)
        self._apply_enabled()

    # ── public ───────────────────────────────────────────────────────────────

    def set_laser(self, name: str) -> None:
        """Point the controls at a laser. A different laser clears the tick."""
        if name == self._laser:
            return
        self._laser = name
        entry = self._index.get(name)
        self._groups = list(entry["power"]) if entry else []
        self._check.blockSignals(True)
        self._check.setChecked(False)
        self._check.blockSignals(False)
        self._rebuild_rows()
        self._apply_enabled()

    def is_craftable(self) -> bool:
        return bool(self._groups)

    def state(self) -> Optional[Dict[str, int]]:
        """Qualities per part key when ticked, else None."""
        if not self._groups or not self._check.isChecked():
            return None
        return {k: sp.value() for k, sp in self._spins.items()}

    def set_state(self, qualities: Optional[dict]) -> None:
        """Tick/untick and fill the qualities without emitting ``changed``."""
        on = isinstance(qualities, dict) and bool(self._groups)
        if on:
            for g in self._groups:
                sp = self._spins.get(g["key"])
                if sp is None:
                    continue
                sp.blockSignals(True)
                sp.setValue(int(round(mc.clamp_quality(g, qualities.get(g["key"])))))
                sp.blockSignals(False)
        self._check.blockSignals(True)
        self._check.setChecked(on)
        self._check.blockSignals(False)
        self._rows.setVisible(on)

    def set_result(self, factor: Optional[float]) -> None:
        """Show what the crafted parts do to laser power (None = not crafted)."""
        if factor is None:
            self._pct.setText("")
            return
        self._pct.setText(f"{_('Laser power')} {(factor - 1.0) * 100.0:+.1f}%")

    # ── internals ────────────────────────────────────────────────────────────

    def _apply_enabled(self) -> None:
        ok = bool(self._groups)
        self._check.setEnabled(ok)
        if ok:
            self._check.setToolTip(_(
                "Tick if this laser was crafted, then enter the quality (0-1000) of the "
                "material used for each part. Mining laser power is scaled before modules."
            ))
        elif not self._index:
            self._check.setToolTip(_(
                "Crafting blueprint data was not found on this PC "
                "(the Craft Database tool downloads it)."
            ))
        else:
            self._check.setToolTip(_("The game data has no crafting blueprint for this laser."))
        self._rows.setVisible(ok and self._check.isChecked())

    def _rebuild_rows(self) -> None:
        while self._rows_lay.count():
            item = self._rows_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._spins = {}
        for g in self._groups:
            row = QWidget()
            row.setStyleSheet("background: transparent;")
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(4)
            text = g["name"]
            if g["material"]:
                text += f" · {g['material']}"
            lbl = QLabel(text)
            lbl.setStyleSheet(f"""
                font-family: Consolas;
                font-size: 8pt;
                color: {P.fg};
                background: transparent;
            """)
            rl.addWidget(lbl, 1)
            sp = QSpinBox()
            sp.setRange(int(g["q_min"]), int(g["q_max"]))
            sp.setSingleStep(10)
            sp.setValue(mc.default_quality(g))
            sp.setFixedWidth(64)
            sp.setStyleSheet(
                f"QSpinBox {{ font-family: Consolas; font-size: 8pt; color: {P.tool_mining};"
                f" background: {P.bg_input}; border: 1px solid {P.border}; padding: 1px; }}"
            )
            lo = (g["at_min"] - 1.0) * 100.0
            hi = (g["at_max"] - 1.0) * 100.0
            sp.setToolTip(
                f"{g['label']}: {lo:+.0f}% "
                + _("at quality") + f" {g['q_min']:.0f}, {hi:+.0f}% "
                + _("at quality") + f" {g['q_max']:.0f}"
            )
            sp.valueChanged.connect(lambda _v: self.changed.emit())
            rl.addWidget(sp)
            self._rows_lay.addWidget(row)
            self._spins[g["key"]] = sp

    def _on_toggled(self, on: bool) -> None:
        self._rows.setVisible(on and bool(self._groups))
        self.changed.emit()
