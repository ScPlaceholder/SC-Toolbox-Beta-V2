"""Missions page -- sidebar filters + mission card grid (PySide6)."""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QScrollArea, QLineEdit, QSlider,
)

from shared.qt.theme import P
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.qt.search_bar import SCSearchBar
from ui.theme import tag_colors, faction_initials, fmt_uec
from ui.components.virtual_grid import VirtualScrollGrid, MissionCard


class MissionsPage(QWidget):
    """Missions page with sidebar filters and card grid."""

    def __init__(self, parent, data_mgr):
        super().__init__(parent)
        self._data = data_mgr
        self._all_results = []
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(300)
        self._filter_timer.timeout.connect(self.on_filter_change)

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
        self._sb_layout = QVBoxLayout(sb)
        self._sb_layout.setContentsMargins(0, 0, 0, 0)
        self._sb_layout.setSpacing(0)
        sidebar_scroll.setWidget(sb)
        main.addWidget(sidebar_scroll)

        self._build_sidebar(self._sb_layout)

        # Separator
        sep = QFrame()
        sep.setFrameShape(QFrame.VLine)
        sep.setStyleSheet(f"color: {P.border};")
        sep.setFixedWidth(1)
        main.addWidget(sep)

        # ── Cards area ──
        cards_area = QVBoxLayout()
        cards_area.setContentsMargins(0, 0, 0, 0)
        cards_area.setSpacing(0)

        self._count_label = QLabel("Loading...")
        self._count_label.setFixedHeight(28)
        self._count_label.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; padding-left: 10px; background: {P.bg_primary};")
        cards_area.addWidget(self._count_label)

        self._vgrid = VirtualScrollGrid(
            card_width=320, row_height=130,
            fill_fn=self._fill_mission_card,
            on_click_fn=self._on_mission_click,
            card_class=MissionCard,
        )
        cards_area.addWidget(self._vgrid, 1)
        main.addLayout(cards_area, 1)

    def _build_sidebar(self, sb):
        def section(text):
            lbl = QLabel(text)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {P.accent}; background: transparent; padding: 8px 8px 2px 8px;")
            sb.addWidget(lbl)

        # Header
        hdr = QWidget()
        hdr.setStyleSheet(f"background: {P.bg_secondary};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(8, 8, 8, 4)
        fl = QLabel("FILTERS")
        fl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.fg}; background: transparent;")
        hl.addWidget(fl)
        hl.addStretch(1)
        clear_btn = QPushButton("Clear all")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {P.red}; border: none; font-family: Consolas; font-size: 8pt; }}
            QPushButton:hover {{ color: {P.fg_bright}; }}
        """)
        clear_btn.clicked.connect(self.clear_all_filters)
        hl.addWidget(clear_btn)
        sb.addWidget(hdr)

        # Search
        section("SEARCH")
        self._search_bar = SCSearchBar(placeholder="Search missions...", debounce_ms=300)
        self._search_bar.search_changed.connect(lambda _: self.on_filter_change())
        sb.addWidget(self._search_bar)

        # Category
        section("CATEGORY")
        self._cat_btns = {}
        cat_row = QHBoxLayout()
        cat_row.setContentsMargins(8, 0, 8, 2)
        for cat in ["career", "story"]:
            btn = self._make_toggle_btn(cat.title(), "category", cat)
            cat_row.addWidget(btn)
        cat_row.addStretch(1)
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setLayout(cat_row)
        sb.addWidget(w)

        # System
        section("STAR SYSTEM")
        self._sys_btns = {}
        sys_row = QHBoxLayout()
        sys_row.setContentsMargins(8, 0, 8, 2)
        for s in ["Multi", "Nyx", "Pyro", "Stanton"]:
            btn = self._make_toggle_btn(s, "system", s)
            sys_row.addWidget(btn)
        sys_row.addStretch(1)
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setLayout(sys_row)
        sb.addWidget(w)

        # Mission type
        section("MISSION TYPE")
        self._type_combo = SCFuzzyCombo(placeholder="All types")
        self._type_combo.addItem("All types")
        self._type_combo.currentIndexChanged.connect(lambda _: self.on_filter_change())
        sb.addWidget(self._type_combo)

        # Faction
        section("FACTION")
        self._faction_combo = SCFuzzyCombo(placeholder="All factions")
        self._faction_combo.addItem("All factions")
        self._faction_combo.currentIndexChanged.connect(lambda _: self.on_filter_change())
        sb.addWidget(self._faction_combo)

        # Legality
        section("LEGALITY")
        self._legality = "all"
        leg_row = QHBoxLayout()
        leg_row.setContentsMargins(8, 0, 8, 2)
        self._leg_btns = {}
        for val, text in [("all", "All"), ("legal", "Legal"), ("illegal", "Illegal")]:
            btn = self._make_radio_btn(text, "legality", val)
            leg_row.addWidget(btn)
        leg_row.addStretch(1)
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setLayout(leg_row)
        sb.addWidget(w)

        # Sharing
        section("SHARING")
        self._sharing = "all"
        shr_row = QHBoxLayout()
        shr_row.setContentsMargins(8, 0, 8, 2)
        self._shr_btns = {}
        for val, text in [("all", "All"), ("sharable", "Sharable"), ("solo", "Solo")]:
            btn = self._make_radio_btn(text, "sharing", val)
            shr_row.addWidget(btn)
        shr_row.addStretch(1)
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setLayout(shr_row)
        sb.addWidget(w)

        # Availability
        section("AVAILABILITY")
        self._avail = "all"
        av_row = QHBoxLayout()
        av_row.setContentsMargins(8, 0, 8, 2)
        self._avail_btns = {}
        for val, text in [("all", "All"), ("unique", "Unique"), ("repeatable", "Repeatable")]:
            btn = self._make_radio_btn(text, "availability", val)
            av_row.addWidget(btn)
        av_row.addStretch(1)
        w = QWidget()
        w.setStyleSheet("background: transparent;")
        w.setLayout(av_row)
        sb.addWidget(w)

        # Rank
        section("RANK INDEX")
        self._rank_slider = QSlider(Qt.Horizontal)
        self._rank_slider.setRange(0, 6)
        self._rank_slider.setValue(6)
        self._rank_slider.valueChanged.connect(lambda _: self.on_filter_change())
        sb.addWidget(self._rank_slider)

        # Reward range
        section("REWARD UEC")
        rew_w = QWidget()
        rew_w.setStyleSheet("background: transparent;")
        rl = QHBoxLayout(rew_w)
        rl.setContentsMargins(8, 0, 8, 2)
        rl.addWidget(QLabel("Min"))
        self._reward_min = QLineEdit("0")
        self._reward_min.setFixedWidth(70)
        self._reward_min.textChanged.connect(lambda _: self._filter_timer.start())
        rl.addWidget(self._reward_min)
        rl.addWidget(QLabel("\u2013"))
        rl.addWidget(QLabel("Max"))
        self._reward_max = QLineEdit("9999999")
        self._reward_max.setFixedWidth(70)
        self._reward_max.textChanged.connect(lambda _: self._filter_timer.start())
        rl.addWidget(self._reward_max)
        sb.addWidget(rew_w)

        sb.addStretch(1)

    # ── Toggle / radio button factories ──

    def _make_toggle_btn(self, text, group, value):
        btn = QPushButton(text)
        btn.setCheckable(True)
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(f"""
            QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                          font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
            QPushButton:checked {{ background: #1a3030; color: {P.accent}; }}
            QPushButton:hover {{ color: {P.fg}; }}
        """)
        btn.toggled.connect(lambda _: self.on_filter_change())
        if group == "category":
            self._cat_btns[value] = btn
        elif group == "system":
            self._sys_btns[value] = btn
        return btn

    def _make_radio_btn(self, text, group, value):
        btn = QPushButton(text)
        btn.setCursor(Qt.PointingHandCursor)
        is_default = value == "all"
        btn.setStyleSheet(f"""
            QPushButton {{ background: {"#1a3030" if is_default else P.bg_card};
                          color: {P.accent if is_default else P.fg_dim}; border: none;
                          font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
            QPushButton:hover {{ color: {P.fg}; }}
        """)

        def _select():
            setattr(self, f"_{group}", value)
            btns = getattr(self, f"_{group[:3]}_btns", {})
            for v, b in btns.items():
                active = v == value
                b.setStyleSheet(f"""
                    QPushButton {{ background: {"#1a3030" if active else P.bg_card};
                                  color: {P.accent if active else P.fg_dim}; border: none;
                                  font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
                    QPushButton:hover {{ color: {P.fg}; }}
                """)
            self.on_filter_change()

        btn.clicked.connect(_select)
        if group == "legality":
            self._leg_btns[value] = btn
        elif group == "sharing":
            self._shr_btns[value] = btn
        elif group == "availability":
            self._avail_btns[value] = btn
        return btn

    # ── Public API ──

    def populate_dropdowns(self):
        self._type_combo.clear()
        self._type_combo.addItem("All types")
        for t in self._data.all_mission_types:
            self._type_combo.addItem(t)

        self._faction_combo.clear()
        self._faction_combo.addItem("All factions")
        for f in self._data.all_faction_names:
            self._faction_combo.addItem(f)

        if self._data.min_reward:
            self._reward_min.setText(str(self._data.min_reward))
        if self._data.max_reward:
            self._reward_max.setText(str(self._data.max_reward))

    def rebuild_cards(self):
        self._vgrid.set_data(self._all_results)

    def rebuild_cards_with(self, items):
        self._all_results = items
        self._vgrid.set_data(items)

    def set_search(self, query: str):
        self._search_bar.setText(query)

    def apply_ipc_filter(self, cmd: dict):
        if "category" in cmd:
            cat = cmd["category"].lower()
            if cat in self._cat_btns:
                self._cat_btns[cat].setChecked(True)
        if "system" in cmd:
            s = cmd["system"]
            if s in self._sys_btns:
                self._sys_btns[s].setChecked(True)
        if "mission_type" in cmd:
            idx = self._type_combo.findText(cmd["mission_type"])
            if idx >= 0:
                self._type_combo.setCurrentIndex(idx)
        self.on_filter_change()

    # ── Filter logic ──

    def on_filter_change(self):
        if not self._data.loaded:
            return

        from data.models import FilterState
        from services.filtering import filter_contracts

        mt = self._type_combo.currentText()
        if mt in ("All types", ""):
            mt = ""

        fn = self._faction_combo.currentText()
        if fn in ("All factions", ""):
            factions = set()
        else:
            factions = {fn}

        try:
            reward_min = int(self._reward_min.text() or 0)
        except ValueError:
            reward_min = 0
        try:
            reward_max = int(self._reward_max.text() or 9999999)
        except ValueError:
            reward_max = 9999999

        filters = FilterState(
            search=self._search_bar.text().strip(),
            categories={cat for cat, btn in self._cat_btns.items() if btn.isChecked()},
            systems={s for s, btn in self._sys_btns.items() if btn.isChecked()},
            mission_type=mt,
            factions=factions,
            legality="" if self._legality == "all" else self._legality,
            sharing="" if self._sharing == "all" else self._sharing,
            availability="" if self._avail == "all" else self._avail,
            rank_max=self._rank_slider.value(),
            reward_min=reward_min,
            reward_max=reward_max,
        )

        self._all_results = filter_contracts(
            self._data.contracts, filters,
            self._data.faction_by_guid,
            self._data.availability_pools,
            self._data.scopes,
            self._data.blueprint_pools,
        )
        total = len(self._data.contracts)
        shown = len(self._all_results)
        self._count_label.setText(f"{shown} of {total}")
        self.rebuild_cards()

    def clear_all_filters(self):
        self._search_bar.clear()
        for btn in self._cat_btns.values():
            btn.setChecked(False)
        for btn in self._sys_btns.values():
            btn.setChecked(False)
        self._type_combo.setCurrentIndex(0)
        self._faction_combo.setCurrentIndex(0)
        self._legality = "all"
        self._sharing = "all"
        self._avail = "all"
        # Reset radio button styles
        for group_name, btns in [("leg", self._leg_btns), ("shr", self._shr_btns), ("avail", self._avail_btns)]:
            for v, b in btns.items():
                active = v == "all"
                b.setStyleSheet(f"""
                    QPushButton {{ background: {"#1a3030" if active else P.bg_card};
                                  color: {P.accent if active else P.fg_dim}; border: none;
                                  font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
                    QPushButton:hover {{ color: {P.fg}; }}
                """)
        self._rank_slider.setValue(6)
        self._reward_min.setText("0")
        self._reward_max.setText("9999999")
        self.on_filter_change()

    # ── Card fill + click ──

    def _fill_mission_card(self, card, contract, idx):
        c = contract
        faction = self._data.get_faction(c.get("factionGuid", ""))
        fname = faction.get("name", "Unknown")
        initials = faction_initials(fname)

        title = c.get("title", "Unknown Mission")
        if title.startswith("@"):
            title = c.get("debugName", title)
        if len(title) > 50:
            title = title[:47] + "..."

        tags = []
        for s in (c.get("systems") or [])[:2]:
            bg_c, fg_c = tag_colors(s)
            tags.append((s, bg_c, fg_c, False))
        mt_val = c.get("missionType", "")
        if mt_val:
            bg_c, fg_c = tag_colors(mt_val)
            tags.append((mt_val, bg_c, fg_c, True))
        prereqs = c.get("prerequisites") or {}
        if prereqs.get("completedContractTags"):
            bg_c, fg_c = tag_colors("CHAIN")
            tags.append(("CHAIN", bg_c, fg_c, True))

        reward = c.get("rewardUEC")
        reward_text = fmt_uec(reward) if reward else "\u2014"
        reward_color = P.yellow if reward else P.fg_dim

        card.set_data(title, initials, fname, tags, reward_text, reward_color)

    def _on_mission_click(self, contract, idx):
        from ui.modals.mission_detail import MissionDetailModal
        MissionDetailModal(self.window(), contract, self._data)
