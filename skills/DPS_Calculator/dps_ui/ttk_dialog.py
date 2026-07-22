"""Time-To-Kill dialog — your current loadout's sustained DPS vs a target ship.

Isolated popup (cannot disturb the main layout). Pick a target ship; it shows the
target's shield + hull HP, the time to kill it, and — the thing erkul never tells
you — whether the kill is even possible (sustained DPS must exceed shield regen).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox, QPushButton,
)

try:
    from dps_ui.constants import BG, BG2, BG3, BORDER, FG, FG_DIM, ACCENT, GREEN, YELLOW
except Exception:  # pragma: no cover
    BG = "#111"; BG2 = "#1a1a1a"; BG3 = "#222"; BORDER = "#333"; FG = "#ddd"
    FG_DIM = "#888"; ACCENT = "#4af"; GREEN = "#6c6"; YELLOW = "#cc6"

from services.ttk import target_defense, time_to_kill, format_ttk
from services.slot_extractor import extract_slots_by_type


class TTKDialog(QDialog):
    def __init__(self, parent, ship_names, get_ship_data, find_shield, attacker_dps):
        super().__init__(parent)
        self.setWindowTitle("Time To Kill")
        self.setModal(False)
        self._get_ship_data = get_ship_data
        self._find_shield = find_shield
        self._attacker_dps = float(attacker_dps or 0)

        self.setStyleSheet(
            f"QDialog {{ background: {BG}; border: 1px solid {BORDER}; }}"
            f"QLabel {{ color: {FG}; font-family: Consolas; font-size: 9pt; background: transparent; }}"
            f"QComboBox {{ color: {FG}; background: {BG2}; border: 1px solid {BORDER}; padding: 3px; }}"
            f"QPushButton {{ color: {FG}; background: {BG2}; border: 1px solid {BORDER}; padding: 4px 12px; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QLabel("Time To Kill — your loadout vs a target")
        title.setStyleSheet(f"color: {ACCENT}; font-weight: bold; font-size: 10pt;")
        root.addWidget(title)

        atk = QHBoxLayout()
        atk.addWidget(QLabel("Your sustained DPS:"))
        self._atk_lbl = QLabel(f"{self._attacker_dps:,.0f}")
        self._atk_lbl.setStyleSheet(f"color: {YELLOW}; font-weight: bold;")
        atk.addWidget(self._atk_lbl)
        atk.addStretch(1)
        root.addLayout(atk)

        tgt = QHBoxLayout()
        tgt.addWidget(QLabel("Target ship:"))
        self._combo = QComboBox()
        self._combo.setEditable(True)
        self._combo.addItems(sorted(ship_names or []))
        self._combo.setFixedWidth(260)
        self._combo.currentTextChanged.connect(self._recompute)
        tgt.addWidget(self._combo)
        tgt.addStretch(1)
        root.addLayout(tgt)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self._out = {}
        for r, (key, label) in enumerate([
            ("shield", "Target shield HP"),
            ("hull", "Target hull HP"),
            ("eff", "Effective HP"),
            ("regen", "Shield regen /s"),
            ("ttk", "TIME TO KILL"),
        ]):
            lk = QLabel(label + ":")
            lk.setStyleSheet(f"color: {FG_DIM};")
            lv = QLabel("—")
            if key == "ttk":
                lv.setStyleSheet(f"color: {GREEN}; font-weight: bold; font-size: 11pt;")
            grid.addWidget(lk, r, 0, Qt.AlignRight)
            grid.addWidget(lv, r, 1, Qt.AlignLeft)
            self._out[key] = lv
        root.addLayout(grid)

        self._verdict = QLabel("")
        self._verdict.setWordWrap(True)
        root.addWidget(self._verdict)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(close)
        root.addLayout(row)

        self.resize(380, 300)
        if self._combo.count():
            self._recompute(self._combo.currentText())

    def _recompute(self, name):
        try:
            ship = self._get_ship_data(name)
        except Exception:
            ship = None
        if not ship:
            for k in self._out:
                self._out[k].setText("—")
            self._verdict.setText("")
            return
        dfn = target_defense(ship, self._find_shield, extract_slots_by_type)
        res = time_to_kill(self._attacker_dps, dfn)
        self._out["shield"].setText(f"{dfn['shield_hp']:,.0f}")
        self._out["hull"].setText(f"{dfn['hull_hp']:,.0f}")
        self._out["eff"].setText(f"{dfn['effective_hp']:,.0f}")
        self._out["regen"].setText(f"{dfn['shield_regen']:,.0f}")
        if res.get("possible"):
            self._out["ttk"].setText(format_ttk(res))
            self._out["ttk"].setStyleSheet(f"color: {GREEN}; font-weight: bold; font-size: 11pt;")
            self._verdict.setText("")
        else:
            self._out["ttk"].setText("—")
            self._out["ttk"].setStyleSheet(f"color: #c66; font-weight: bold; font-size: 11pt;")
            self._verdict.setText("⚠ " + res.get("reason", "Unwinnable"))
            self._verdict.setStyleSheet("color: #c66;")
