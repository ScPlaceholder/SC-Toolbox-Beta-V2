"""Weapon-crafting panel — master slider + per-weapon + per-SLOT sub-sliders.

An isolated popup so it can never disturb the main window layout. Each craftable
weapon shows its individual crafting slots as sub-sliders underneath it (true
"ludicrous mode" — each gun has ~2 damage slots that stack). A master slider drives
everything at once. Emits a live {className: {slot: quality}} config via on_change so
the DPS footer updates as you drag. Any slot left at store-bought (500) is omitted,
so an empty config means "no crafting" (a clean no-op — parity preserved).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSlider,
    QScrollArea, QWidget, QFrame,
)

try:
    from dps_ui.constants import BG, BG2, BG3, BORDER, FG, FG_DIM, ACCENT, GREEN, YELLOW
except Exception:  # pragma: no cover - defensive fallback
    BG = "#111"; BG2 = "#1a1a1a"; BG3 = "#222"; BORDER = "#333"; FG = "#ddd"
    FG_DIM = "#888"; ACCENT = "#4af"; GREEN = "#6c6"; YELLOW = "#cc6"

STORE_Q = 500


def _slot_pct(slot, q):
    """Single-slot damage-modifier as a percentage delta at quality q."""
    q0, q1 = slot.get("q_start", 0), slot.get("q_end", 1000)
    qq = max(q0, min(q1, q))
    frac = (qq - q0) / (q1 - q0) if q1 > q0 else 0.0
    mod = slot.get("mod_start", 1.0) + (slot.get("mod_end", 1.0) - slot.get("mod_start", 1.0)) * frac
    return (mod - 1.0) * 100.0


def _weapon_pct(className, qmap):
    """Whole-weapon damage delta % from its per-slot quality map (uses the real backend)."""
    try:
        from services.crafting import weapon_damage_mult
        return (weapon_damage_mult(className, qmap) - 1.0) * 100.0
    except Exception:
        return 0.0


def _pct_html(pct):
    color = GREEN if pct > 0.05 else (FG_DIM if abs(pct) <= 0.05 else "#c66")
    sign = "+" if pct >= 0 else ""
    return f'<span style="color:{color}">{sign}{pct:.1f}%</span>'


class CraftingDialog(QDialog):
    """weapons: list of (className, display_name, [slot_dict, ...]). current: {cn: {slot: q}} or None."""

    def __init__(self, parent, weapons, current, on_change):
        super().__init__(parent)
        self.setWindowTitle("Weapon Crafting — per-slot quality")
        self.setModal(False)
        self._on_change = on_change
        self._weapons = list(weapons)
        cur = current if isinstance(current, dict) else {}
        # state: {className: {slot_name: quality}}
        self._q = {}
        for cn, _name, slots in self._weapons:
            wcur = cur.get(cn, {}) if isinstance(cur.get(cn), dict) else {}
            self._q[cn] = {s["slot"]: int(wcur.get(s["slot"], STORE_Q)) for s in slots}
        self._slot_rows = {}   # (cn, slot) -> (slider, val_lbl, eff_lbl)
        self._wpn_eff = {}     # cn -> weapon total effect label
        self._wpn_slots = {cn: slots for cn, _n, slots in self._weapons}

        self.setStyleSheet(
            f"QDialog {{ background: {BG}; border: 1px solid {BORDER}; }}"
            f"QLabel {{ color: {FG}; font-family: Consolas; font-size: 9pt; background: transparent; }}"
            f"QPushButton {{ color: {FG}; background: {BG2}; border: 1px solid {BORDER}; padding: 4px 12px; font-family: Consolas; }}"
            f"QPushButton:hover {{ background: {BG3}; }}"
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(6)

        title = QLabel("Craft each weapon slot independently. 500 = store-bought (no effect).")
        title.setStyleSheet(f"color: {ACCENT}; font-weight: bold; font-size: 10pt;")
        root.addWidget(title)

        # Master row — drives every slot of every weapon at once.
        master = QHBoxLayout()
        m_lbl = QLabel("ALL slots")
        m_lbl.setFixedWidth(150)
        m_lbl.setStyleSheet(f"color: {YELLOW}; font-weight: bold;")
        self._master = QSlider(Qt.Horizontal)
        self._master.setMinimum(0); self._master.setMaximum(1000); self._master.setValue(STORE_Q)
        self._master.valueChanged.connect(self._on_master)
        self._master_val = QLabel(f"Q{STORE_Q}")
        self._master_val.setFixedWidth(56)
        master.addWidget(m_lbl); master.addWidget(self._master); master.addWidget(self._master_val)
        root.addLayout(master)

        line = QFrame(); line.setFrameShape(QFrame.HLine); line.setStyleSheet(f"color: {BORDER};")
        root.addWidget(line)

        # Scrollable per-weapon sections, each with per-slot sub-sliders.
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ border: 1px solid {BORDER}; background: {BG}; }}")
        inner = QWidget()
        col = QVBoxLayout(inner)
        col.setContentsMargins(4, 4, 4, 4)
        col.setSpacing(2)
        if not self._weapons:
            col.addWidget(QLabel("No craftable weapons on this loadout."))
        for cn, name, slots in self._weapons:
            self._add_weapon_section(col, cn, name, slots)
        col.addStretch(1)
        scroll.setWidget(inner)
        root.addWidget(scroll, 1)

        btns = QHBoxLayout()
        btns.addStretch(1)
        reset = QPushButton("Reset to store-bought")
        reset.clicked.connect(self._reset)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        btns.addWidget(reset); btns.addWidget(close)
        root.addLayout(btns)

        self.resize(480, 460)

    def _add_weapon_section(self, col, cn, name, slots):
        # Weapon header: name + rolled-up effect.
        hdr = QHBoxLayout()
        wlbl = QLabel(name or cn)
        wlbl.setToolTip(cn)
        wlbl.setStyleSheet(f"color: {ACCENT}; font-weight: bold;")
        eff = QLabel(_pct_html(_weapon_pct(cn, self._q.get(cn, {}))))
        eff.setFixedWidth(60)
        hdr.addWidget(wlbl); hdr.addStretch(1); hdr.addWidget(eff)
        col.addLayout(hdr)
        self._wpn_eff[cn] = eff
        # Per-slot sub-sliders (indented).
        for s in slots:
            col.addLayout(self._slot_row(cn, s))

    def _slot_row(self, cn, slot):
        sname = slot["slot"]
        row = QHBoxLayout()
        row.addSpacing(18)  # indent under the weapon
        lbl = QLabel(sname)
        lbl.setFixedWidth(150)
        lbl.setStyleSheet(f"color: {FG_DIM};")
        sld = QSlider(Qt.Horizontal)
        sld.setMinimum(0); sld.setMaximum(1000); sld.setValue(self._q[cn].get(sname, STORE_Q))
        sld.valueChanged.connect(lambda v, c=cn, sn=sname: self._on_slot(c, sn, v))
        val = QLabel(f"Q{self._q[cn].get(sname, STORE_Q)}")
        val.setFixedWidth(48)
        eff = QLabel(_pct_html(_slot_pct(slot, self._q[cn].get(sname, STORE_Q))))
        eff.setFixedWidth(54)
        row.addWidget(lbl); row.addWidget(sld); row.addWidget(val); row.addWidget(eff)
        self._slot_rows[(cn, sname)] = (sld, val, eff, slot)
        return row

    def _refresh_weapon_eff(self, cn):
        lbl = self._wpn_eff.get(cn)
        if lbl is not None:
            lbl.setText(_pct_html(_weapon_pct(cn, self._q.get(cn, {}))))

    def _on_slot(self, cn, sname, v):
        self._q[cn][sname] = v
        sld, val, eff, slot = self._slot_rows[(cn, sname)]
        val.setText(f"Q{v}")
        eff.setText(_pct_html(_slot_pct(slot, v)))
        self._refresh_weapon_eff(cn)
        self._emit()

    def _on_master(self, v):
        self._master_val.setText(f"Q{v}")
        for (cn, sname), (sld, val, eff, slot) in self._slot_rows.items():
            self._q[cn][sname] = v
            sld.blockSignals(True); sld.setValue(v); sld.blockSignals(False)
            val.setText(f"Q{v}")
            eff.setText(_pct_html(_slot_pct(slot, v)))
        for cn in self._q:
            self._refresh_weapon_eff(cn)
        self._emit()

    def _reset(self):
        self._master.blockSignals(True); self._master.setValue(STORE_Q); self._master.blockSignals(False)
        self._master_val.setText(f"Q{STORE_Q}")
        self._on_master(STORE_Q)

    def result_config(self):
        """{className: {slot: quality}} for non-store-bought slots only (empty -> no-op)."""
        out = {}
        for cn, slots in self._q.items():
            moved = {sn: q for sn, q in slots.items() if q != STORE_Q}
            if moved:
                out[cn] = moved
        return out

    def _emit(self):
        if self._on_change:
            self._on_change(self.result_config())
