"""Resources/Mining page -- location browser with resource data (PySide6)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QScrollArea, QRadioButton, QButtonGroup,
)

from shared.qt.theme import P
from shared.qt.fuzzy_multi_check import SCFuzzyMultiCheck
from shared.qt.search_bar import SCSearchBar
from config import HIDDEN_LOCATIONS, MINING_GROUP_TYPES
from ui.theme import tag_colors
from ui.components.virtual_grid import VirtualScrollGrid, MissionCard


class ResourcesPage(QWidget):
    """Resources/Mining page with sidebar filters and location card grid."""

    def __init__(self, parent, data_mgr):
        super().__init__(parent)
        self._data = data_mgr
        self._res_all_results = []
        self._resource_multi = None
        self._build()

    def _build(self):
        main = QHBoxLayout(self)
        main.setContentsMargins(0, 0, 0, 0)
        main.setSpacing(0)

        # ── Sidebar ──
        sidebar_scroll = QScrollArea()
        sidebar_scroll.setFixedWidth(220)
        sidebar_scroll.setWidgetResizable(True)
        sidebar_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar_scroll.setStyleSheet(f"QScrollArea {{ background-color: {P.bg_secondary}; border: none; }}")
        sb = QWidget()
        sb.setStyleSheet(f"background-color: {P.bg_secondary};")
        sb_lay = QVBoxLayout(sb)
        sb_lay.setContentsMargins(0, 0, 0, 0)
        sb_lay.setSpacing(0)
        sidebar_scroll.setWidget(sb)
        main.addWidget(sidebar_scroll)

        def section(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {P.accent}; background: transparent; padding: 8px 8px 2px 8px;")
            sb_lay.addWidget(lbl)

        # Header
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {P.bg_secondary};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(8, 8, 8, 4)
        fl = QLabel("FILTERS")
        fl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.fg}; background: transparent;")
        hl.addWidget(fl)
        hl.addStretch(1)
        clear_btn = QPushButton("Clear")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {P.fg_dim}; border: none; font-family: Consolas; font-size: 7pt; }}
            QPushButton:hover {{ color: {P.fg}; }}
        """)
        clear_btn.clicked.connect(self.clear_filters)
        hl.addWidget(clear_btn)
        sb_lay.addWidget(hdr)

        # Search
        section("SEARCH")
        self._search = SCSearchBar(placeholder="Search locations...", debounce_ms=300)
        self._search.search_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._search)

        # System
        section("SYSTEM")
        self._sys_btns = {}
        sys_row = QHBoxLayout()
        sys_row.setContentsMargins(8, 0, 8, 2)
        for s in ["Stanton", "Pyro", "Nyx"]:
            btn = QPushButton(s)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
                QPushButton:checked {{ background: #1a3030; color: {P.accent}; }}
                QPushButton:hover {{ color: {P.fg}; }}
            """)
            btn.toggled.connect(lambda _: self.on_filter_change())
            sys_row.addWidget(btn)
            self._sys_btns[s] = btn
        sys_row.addStretch(1)
        sw = QWidget()
        sw.setStyleSheet("background: transparent;")
        sw.setLayout(sys_row)
        sb_lay.addWidget(sw)

        # Location type
        section("LOCATION TYPE")
        self._lt_btns = {}
        lt_row = QHBoxLayout()
        lt_row.setContentsMargins(8, 0, 8, 2)
        for lt in ["Planet", "Moon", "Belt", "Lagrange", "Cluster", "Event", "Special"]:
            btn = QPushButton(lt)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 7pt; padding: 1px 4px; }}
                QPushButton:checked {{ background: #1a3030; color: {P.accent}; }}
                QPushButton:hover {{ color: {P.fg}; }}
            """)
            btn.toggled.connect(lambda _: self.on_filter_change())
            lt_row.addWidget(btn)
            self._lt_btns[lt.lower()] = btn
        lt_row.addStretch(1)
        ltw = QWidget()
        ltw.setStyleSheet("background: transparent;")
        ltw.setLayout(lt_row)
        sb_lay.addWidget(ltw)

        # Deposit type
        section("DEPOSIT TYPE")
        self._dt_btns = {}
        dt_row = QHBoxLayout()
        dt_row.setContentsMargins(8, 0, 8, 2)
        _dt_colors = {
            "SpaceShip_Mineables": P.accent,
            "FPS_Mineables": P.purple,
            "GroundVehicle_Mineables": P.orange,
            "Harvestables": P.green,
        }
        for key, label in [("SpaceShip_Mineables", "Ship"), ("FPS_Mineables", "FPS"),
                           ("GroundVehicle_Mineables", "ROC"), ("Harvestables", "Harvest")]:
            active_fg = _dt_colors.get(key, P.accent)
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
                QPushButton:checked {{ background: #1a3030; color: {active_fg}; }}
                QPushButton:hover {{ color: {P.fg}; }}
            """)
            btn.toggled.connect(lambda _: self.on_filter_change())
            dt_row.addWidget(btn)
            self._dt_btns[key] = btn
        dt_row.addStretch(1)
        dtw = QWidget()
        dtw.setStyleSheet("background: transparent;")
        dtw.setLayout(dt_row)
        sb_lay.addWidget(dtw)

        # Resources filter
        section("RESOURCES")
        self._resource_multi = SCFuzzyMultiCheck(label="All", items=[])
        self._resource_multi.selection_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._resource_multi)

        # Match mode
        match_w = QWidget()
        match_w.setStyleSheet("background: transparent;")
        ml = QHBoxLayout(match_w)
        ml.setContentsMargins(8, 0, 8, 2)
        self._match_group = QButtonGroup(self)
        self._any_radio = QRadioButton("Any")
        self._any_radio.setChecked(True)
        self._any_radio.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        self._any_radio.toggled.connect(lambda _: self.on_filter_change())
        ml.addWidget(self._any_radio)
        self._all_radio = QRadioButton("All")
        self._all_radio.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        ml.addWidget(self._all_radio)
        self._match_group.addButton(self._any_radio)
        self._match_group.addButton(self._all_radio)
        ml.addStretch(1)
        sb_lay.addWidget(match_w)

        sb_lay.addStretch(1)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {P.border};")
        sep.setFixedWidth(1)
        main.addWidget(sep)

        # ── Main area ──
        right = QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)

        self._count_label = QLabel("Loading resource data...")
        self._count_label.setFixedHeight(28)
        self._count_label.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; padding-left: 10px; background: {P.bg_primary};")
        right.addWidget(self._count_label)

        self._vgrid = VirtualScrollGrid(
            card_width=320, row_height=280,
            fill_fn=self._fill_card,
            on_click_fn=self._on_click,
            card_class=MissionCard,
        )
        right.addWidget(self._vgrid, 1)
        main.addLayout(right, 1)

    # ── Public API ──

    def set_count_message(self, msg):
        self._count_label.setText(msg)

    def populate_resource_values(self):
        if self._resource_multi:
            self._resource_multi.set_items(sorted(self._data.all_resource_names))

    def rebuild_grid(self):
        self._vgrid.set_data(self._res_all_results)

    # ── Filter logic ──

    def on_filter_change(self):
        if not self._data.mining_loaded:
            return

        search = (self._search.text() or "").lower()
        active_systems = {s for s, btn in self._sys_btns.items() if btn.isChecked()}
        active_loctypes = {t for t, btn in self._lt_btns.items() if btn.isChecked()}
        active_deptypes = {t for t, btn in self._dt_btns.items() if btn.isChecked()}
        selected_res = set(self._resource_multi.get_selected()) if self._resource_multi else set()
        match_mode = "all" if self._all_radio.isChecked() else "any"

        hidden = HIDDEN_LOCATIONS
        results = []

        for loc in self._data.mining_locations:
            loc_name = loc.get("locationName", "")
            if loc_name in hidden:
                continue

            system = loc.get("system", "")
            loc_type = loc.get("locationType", "")

            if active_systems and system not in active_systems:
                continue
            if active_loctypes and loc_type not in active_loctypes:
                continue
            if active_deptypes:
                group_names = {g.get("groupName", "") for g in loc.get("groups", [])}
                if not group_names.intersection(active_deptypes):
                    continue

            loc_resources = self._data.get_location_resources(loc_name)
            resource_names = {r["resource"] for r in loc_resources}

            if selected_res:
                if match_mode == "all":
                    if not selected_res.issubset(resource_names):
                        continue
                else:
                    if not selected_res.intersection(resource_names):
                        continue

            if search:
                all_text = loc_name.lower() + " " + " ".join(r.lower() for r in resource_names)
                if search not in all_text:
                    continue

            results.append(loc)

        self._res_all_results = results
        total = len([loc for loc in self._data.mining_locations
                     if loc.get("locationName", "") not in hidden])
        shown = len(results)
        suffix = f" of {total}" if shown != total else ""
        self._count_label.setText(
            f"{shown}{suffix} Locations  \u00b7  {len(self._data.all_resource_names)} Resources")
        self._vgrid.set_data(results)

    def clear_filters(self):
        self._search.clear()
        for btn in self._sys_btns.values():
            btn.setChecked(False)
        for btn in self._lt_btns.values():
            btn.setChecked(False)
        for btn in self._dt_btns.values():
            btn.setChecked(False)
        if self._resource_multi:
            self._resource_multi.set_selected([])
        self._any_radio.setChecked(True)
        self.on_filter_change()

    # ── Card fill + click ──

    def _fill_card(self, card, loc, idx):
        loc_name = loc.get("locationName", "?")
        system = loc.get("system", "")
        loc_type = loc.get("locationType", "")

        sys_colors = {
            "Stanton": (P.accent, "#0a2020"),
            "Pyro": (P.orange, "#2a1a0a"),
            "Nyx": (P.purple, "#1a0a2a"),
        }
        sys_fg, sys_bg = sys_colors.get(system, (P.fg_dim, P.bg_card))

        tags = [(system, sys_bg, sys_fg, True)]

        LOC_TYPE_COLORS = {
            "planet": ("#0a1a20", "#55bbaa"), "moon": ("#0a1520", "#7799bb"),
            "belt": ("#1a1a0a", "#bbaa55"), "lagrange": ("#0a0a1a", "#8888cc"),
            "cluster": ("#1a0a1a", "#aa77bb"), "event": ("#1a1a0a", P.yellow),
            "special": ("#1a0a0a", P.red),
        }
        type_labels = {"planet": "Planet", "moon": "Moon", "belt": "Belt",
                       "lagrange": "Lagrange", "cluster": "Cluster",
                       "event": "Event", "special": "Special"}
        type_label = type_labels.get(loc_type, loc_type.title())
        lt_bg, lt_fg = LOC_TYPE_COLORS.get(loc_type, (P.bg_card, P.fg_dim))
        tags.append((type_label, lt_bg, lt_fg, True))

        GROUP_COLORS = {
            "SpaceShip_Mineables": ("#0a1a2a", P.accent),
            "SpaceShip_Mineables_Rare": ("#1a1a0a", P.yellow),
            "FPS_Mineables": ("#1a0a1a", P.purple),
            "GroundVehicle_Mineables": ("#1a1a0a", P.orange),
            "Harvestables": ("#0a1a0a", P.green),
        }
        groups = loc.get("groups", [])
        for g in groups[:3]:
            gn = g.get("groupName", "")
            gt_info = MINING_GROUP_TYPES.get(gn, {})
            if gt_info:
                bg_c, fg_c = GROUP_COLORS.get(gn, (P.bg_card, P.fg_dim))
                tags.append((gt_info["short"], bg_c, fg_c, True))

        resources = self._data.get_location_resources(loc_name)
        lines = []
        for r in resources[:8]:
            pct = f"{r['max_pct']:.0f}%" if r["max_pct"] else ""
            name = r["resource"]
            for suffix in [" (Ore)", " (Raw)", " (Gem)"]:
                name = name.replace(suffix, "")
            lines.append(f"{name:<16s} {pct:>5s}")
        if len(resources) > 8:
            lines.append(f"+{len(resources) - 8} more")
        extra = "\n".join(lines)

        reward_text = f"{len(resources)} resources"
        reward_color = P.green if resources else P.fg_dim
        initials = loc_name[:2].upper()

        card.set_data(loc_name, initials, system, tags, reward_text, reward_color, extra=extra)

    def _on_click(self, loc, idx):
        from ui.modals.location_detail import LocationDetailModal
        LocationDetailModal(self.window(), loc, self._data)
