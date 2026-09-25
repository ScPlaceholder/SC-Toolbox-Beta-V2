"""Location detail modal — resource breakdown for mining locations (PySide6)."""
from __future__ import annotations
import logging

from PySide6.QtCore import Qt

log = logging.getLogger(__name__)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QScrollArea, QProgressBar,
)

from shared.qt.theme import P
from config import MINING_GROUP_TYPES
from ui.modals.base import ModalBase


class LocationDetailModal(ModalBase):
    """Popup showing full resource breakdown for a mining location."""

    def __init__(self, parent, loc: dict, data_mgr):
        self._loc = loc
        self._data = data_mgr
        name = loc.get("locationName", "?")
        super().__init__(parent, title=f"Location: {name}", width=550, height=500)
        try:
            self._build_ui()
        except Exception:
            log.exception("[LocationDetail] _build_ui crashed for %s", name)
        self.show()

    def _build_ui(self):
        loc = self._loc
        loc_name = loc.get("locationName", "?")
        system = loc.get("system", "")
        loc_type = loc.get("locationType", "")

        sys_colors = {"Stanton": P.accent, "Pyro": P.orange, "Nyx": P.purple}
        sys_color = sys_colors.get(system, P.fg)

        layout = self.body_layout

        # Close button
        close_row = QHBoxLayout()
        close_row.setContentsMargins(0, 4, 4, 0)
        close_row.addStretch(1)
        close_btn = QPushButton("x")
        close_btn.setObjectName("detailClose")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton#detailClose {{ background: transparent; color: {P.fg_dim}; border: none;
                          font-family: Consolas; font-size: 14pt; font-weight: bold;
                          padding: 0px; min-height: 0px; }}
            QPushButton#detailClose:hover {{ color: {P.red}; }}
        """)
        close_btn.clicked.connect(self.close)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        # Scrollable body
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {P.bg_primary}; }}")
        inner = QWidget()
        inner.setStyleSheet(f"background: {P.bg_primary};")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 0, 16, 16)
        lay.setSpacing(4)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        def _lbl(text, color=P.fg, size="10pt", bold=False):
            l = QLabel(text)
            w = "bold" if bold else "normal"
            l.setStyleSheet(f"font-family: Consolas; font-size: {size}; font-weight: {w}; color: {color}; background: transparent;")
            lay.addWidget(l)

        def _sep():
            s = QFrame()
            s.setFrameShape(QFrame.HLine)
            s.setStyleSheet(f"color: {P.border}; background: {P.border};")
            s.setFixedHeight(1)
            lay.addWidget(s)

        # Header
        _lbl(loc_name, P.fg, "15pt", True)

        # Badges
        badge_w = QWidget()
        badge_w.setStyleSheet("background: transparent;")
        bl = QHBoxLayout(badge_w)
        bl.setContentsMargins(0, 4, 0, 0)
        bl.setSpacing(4)
        sys_badge = QLabel(f" {system} ")
        sys_badge.setStyleSheet(f"background: {P.bg_card}; color: {sys_color}; font-family: Consolas; font-size: 9pt; font-weight: bold;")
        bl.addWidget(sys_badge)
        type_badge = QLabel(f" {loc_type.title()} ")
        type_badge.setStyleSheet(f"background: {P.bg_card}; color: {P.fg_dim}; font-family: Consolas; font-size: 9pt;")
        bl.addWidget(type_badge)

        groups = loc.get("groups", [])
        for g in groups:
            gn = g.get("groupName", "")
            gt_info = MINING_GROUP_TYPES.get(gn, {})
            if gt_info:
                gb = QLabel(f" {gt_info['icon']} {gt_info['short']} ")
                gb.setStyleSheet(f"background: {P.bg_card}; color: {P.fg_dim}; font-family: Consolas; font-size: 9pt;")
                bl.addWidget(gb)
        bl.addStretch(1)
        lay.addWidget(badge_w)

        _sep()

        # Sort buttons
        self._sort_key = "pct"
        self._sort_asc = False
        sort_w = QWidget()
        sort_w.setStyleSheet("background: transparent;")
        sl = QHBoxLayout(sort_w)
        sl.setContentsMargins(0, 4, 0, 4)
        sl.setSpacing(4)
        sort_lbl = QLabel("SORT BY")
        sort_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {P.fg_dim}; background: transparent;")
        sl.addWidget(sort_lbl)
        self._sort_btns: dict[str, QPushButton] = {}
        for key, text in [("pct", "% Desc"), ("name", "Name"), ("type", "Type")]:
            btn = QPushButton(text)
            btn.setCursor(Qt.PointingHandCursor)
            btn.clicked.connect(lambda checked=False, k=key: self._set_sort(k))
            sl.addWidget(btn)
            self._sort_btns[key] = btn
        sl.addStretch(1)
        self._update_sort_btn_style()
        lay.addWidget(sort_w)

        # Resources table header
        hdr_w = QWidget()
        hdr_w.setStyleSheet("background: transparent;")
        hl = QHBoxLayout(hdr_w)
        hl.setContentsMargins(0, 0, 0, 0)
        for text, w in [("RESOURCE", 200), ("TYPE", 100), ("MAX %", 70)]:
            lbl = QLabel(text)
            lbl.setFixedWidth(w)
            align = Qt.AlignRight if text == "MAX %" else Qt.AlignLeft
            lbl.setAlignment(align | Qt.AlignVCenter)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; font-weight: bold; color: {P.fg_dim}; background: transparent;")
            hl.addWidget(lbl)
        lay.addWidget(hdr_w)

        # Resource rows container
        self._rows_container = QWidget()
        self._rows_container.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(0)
        lay.addWidget(self._rows_container)

        self._resources = self._data.get_location_resources(loc_name)

        if not self._resources:
            _lbl("  No resources found at this location.", P.fg_dim)
        else:
            self._rebuild_rows()

        _sep()

        n_groups = len(groups)
        n_res = len(self._resources or [])
        _lbl(f"{n_res} resources  \u00b7  {n_groups} deposit groups", P.fg_dim, "9pt")

        lay.addStretch(1)

    # ── Sort logic ────────────────────────────────────────────────────────

    _GRP_FG = {
        "SpaceShip_Mineables": P.accent,
        "SpaceShip_Mineables_Rare": P.yellow,
        "FPS_Mineables": P.purple,
        "GroundVehicle_Mineables": P.orange,
        "Harvestables": P.green,
        "Salvage_FreshDerelicts": P.red,
        "Salvage_BrokenShips_Poor": "#cc6644",
        "Salvage_BrokenShips_Normal": "#cc6644",
        "Salvage_BrokenShips_Elite": "#cc6644",
    }

    def _set_sort(self, key: str):
        if self._sort_key == key:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_key = key
            self._sort_asc = key == "name"  # name defaults ascending, others descending
        self._update_sort_btn_style()
        self._rebuild_rows()

    def _update_sort_btn_style(self):
        for key, btn in self._sort_btns.items():
            active = key == self._sort_key
            arrow = " \u25b2" if self._sort_asc else " \u25bc" if active else ""
            label = {"pct": "%", "name": "Name", "type": "Type"}[key]
            btn.setText(f"{label}{arrow}")
            btn.setStyleSheet(
                f"QPushButton {{ background: {'#1a3030' if active else P.bg_card}; "
                f"color: {P.accent if active else P.fg_dim}; border: none; "
                f"font-family: Consolas; font-size: 8pt; font-weight: bold; padding: 2px 8px; }}"
                f"QPushButton:hover {{ color: {P.fg}; }}"
            )

    def _rebuild_rows(self):
        # Clear existing rows
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget()
            if w:
                w.deleteLater()

        # Sort resources
        resources = list(self._resources)
        if self._sort_key == "pct":
            resources.sort(key=lambda r: r.get("max_pct", 0), reverse=not self._sort_asc)
        elif self._sort_key == "name":
            resources.sort(key=lambda r: r.get("resource", "").lower(), reverse=not self._sort_asc)
        elif self._sort_key == "type":
            resources.sort(key=lambda r: (
                MINING_GROUP_TYPES.get(r.get("group", ""), {}).get("short", ""),
                -r.get("max_pct", 0),
            ), reverse=not self._sort_asc)

        for i, r in enumerate(resources):
            row_bg = P.bg_card if i % 2 == 0 else P.bg_primary
            max_pct = r.get("max_pct", 0)
            group = r.get("group", "")

            if max_pct >= 40:
                pct_color = P.green
            elif max_pct >= 15:
                pct_color = P.yellow
            elif max_pct >= 5:
                pct_color = P.orange
            else:
                pct_color = P.fg_dim

            display_name = r["resource"]
            for suffix in [" (Ore)", " (Raw)", " (Gem)"]:
                display_name = display_name.replace(suffix, "")

            gt_info = MINING_GROUP_TYPES.get(group, {})
            group_short = gt_info.get("short", group[:8] if group else "\u2014")
            grp_fg = self._GRP_FG.get(group, P.fg_dim)

            row_w = QWidget()
            row_w.setStyleSheet(f"background-color: {row_bg};")
            rl = QHBoxLayout(row_w)
            rl.setContentsMargins(4, 3, 4, 3)

            nl = QLabel(display_name)
            nl.setFixedWidth(200)
            nl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; color: {P.fg}; background: transparent;")
            rl.addWidget(nl)

            gl = QLabel(group_short)
            gl.setFixedWidth(100)
            gl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; font-weight: bold; color: {grp_fg}; background: transparent;")
            rl.addWidget(gl)

            pl = QLabel(f"{max_pct:.0f}%")
            pl.setFixedWidth(70)
            pl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            pl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {pct_color}; background: transparent;")
            rl.addWidget(pl)

            self._rows_layout.addWidget(row_w)
