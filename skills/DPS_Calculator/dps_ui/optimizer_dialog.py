"""Optimizer dialog — the best weapon per hardpoint for a chosen goal (read-only).

Isolated popup. Shows, for each weapon hardpoint, the highest-scoring weapon that fits
it, plus how far your CURRENT fit trails that upper bound. Read-only by design — it
recommends, it never touches your loadout (v0 is greedy per-slot, no power-pool model,
so the total is a target, not a guaranteed sustainable fit).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QComboBox, QPushButton,
    QScrollArea, QWidget,
)

try:
    from dps_ui.constants import BG, BG2, BG3, BORDER, FG, FG_DIM, ACCENT, GREEN, YELLOW
except Exception:  # pragma: no cover
    BG = "#111"; BG2 = "#1a1a1a"; BG3 = "#222"; BORDER = "#333"; FG = "#ddd"
    FG_DIM = "#888"; ACCENT = "#4af"; GREEN = "#6c6"; YELLOW = "#cc6"

from services.optimizer import optimize_weapons, compare_to_current

_GOALS = [("Sustained DPS", "dps_sus"), ("Burst DPS", "dps_raw"), ("Alpha", "alpha")]


class OptimizerDialog(QDialog):
    def __init__(self, parent, slots, weapon_candidates, current_dps_by_goal):
        """current_dps_by_goal: {'dps_sus': x, 'dps_raw': y, 'alpha': z} for the live fit."""
        super().__init__(parent)
        self.setWindowTitle("Loadout Optimizer")
        self.setModal(False)
        self._slots = slots or []
        self._cands = weapon_candidates
        self._current = current_dps_by_goal or {}

        self.setStyleSheet(
            f"QDialog {{ background: {BG}; border: 1px solid {BORDER}; }}"
            f"QLabel {{ color: {FG}; font-family: Consolas; font-size: 9pt; background: transparent; }}"
            f"QComboBox {{ color: {FG}; background: {BG2}; border: 1px solid {BORDER}; padding: 3px; }}"
            f"QPushButton {{ color: {FG}; background: {BG2}; border: 1px solid {BORDER}; padding: 4px 12px; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(8)

        title = QLabel("Best weapon per hardpoint (recommendation only)")
        title.setStyleSheet(f"color: {ACCENT}; font-weight: bold; font-size: 10pt;")
        root.addWidget(title)

        goal = QHBoxLayout()
        goal.addWidget(QLabel("Optimize for:"))
        self._goal = QComboBox()
        for lbl, _k in _GOALS:
            self._goal.addItem(lbl)
        self._goal.currentIndexChanged.connect(self._recompute)
        goal.addWidget(self._goal)
        goal.addStretch(1)
        root.addLayout(goal)

        self._summary = QLabel("")
        self._summary.setStyleSheet("font-size: 10pt;")
        root.addWidget(self._summary)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: 1px solid {BORDER}; background: {BG}; }}")
        self._inner = QWidget()
        self._col = QVBoxLayout(self._inner)
        self._col.setContentsMargins(4, 4, 4, 4)
        self._col.setSpacing(2)
        scroll.setWidget(self._inner)
        root.addWidget(scroll, 1)

        note = QLabel("v0: greedy per-slot, no shared-power-pool model — treat the total as an upper-bound target.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {FG_DIM}; font-size: 8pt;")
        root.addWidget(note)

        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout(); row.addStretch(1); row.addWidget(close)
        root.addLayout(row)

        self.resize(440, 420)
        self._recompute()

    def _clear_rows(self):
        while self._col.count():
            item = self._col.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _recompute(self, *_a):
        key = _GOALS[self._goal.currentIndex()][1]
        result = optimize_weapons(self._slots, self._cands, key)
        cmp = compare_to_current(result["total"], self._current.get(key, 0))

        opt = cmp["optimized"]; cur = cmp["current"]; pct = cmp["pct_below"]
        if pct is None:
            self._summary.setText(f"Optimized: <b style='color:{GREEN}'>{opt:,.0f}</b>")
        else:
            self._summary.setText(
                f"Optimized: <b style='color:{GREEN}'>{opt:,.0f}</b>   "
                f"Current: <b style='color:{YELLOW}'>{cur:,.0f}</b>   "
                f"(current is {pct:.0f}% below the max)"
            )

        self._clear_rows()
        if not self._slots:
            self._col.addWidget(QLabel("No weapon hardpoints on this ship."))
            return
        for p in result["picks"]:
            slot = p["slot"]; w = p["weapon"]
            label = slot.get("label", slot.get("id", "?"))
            msz = slot.get("max_size", "?")
            name = (w.get("name") or w.get("local_name") or "?") if w else "— none fits —"
            row = QHBoxLayout()
            lk = QLabel(f"{label} (S{msz})")
            lk.setFixedWidth(200)
            lk.setStyleSheet(f"color: {FG_DIM};")
            lv = QLabel(name)
            lv.setStyleSheet(f"color: {ACCENT};")
            sc = QLabel(f"{p['score']:,.0f}")
            sc.setFixedWidth(70)
            sc.setStyleSheet(f"color: {GREEN};")
            row.addWidget(lk); row.addWidget(lv); row.addStretch(1); row.addWidget(sc)
            holder = QWidget(); holder.setLayout(row)
            self._col.addWidget(holder)
