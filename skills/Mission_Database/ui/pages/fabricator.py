"""Fabricator page -- crafting blueprint browser (PySide6)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QCheckBox, QScrollArea, QSlider, QSpinBox,
)

from shared.qt.theme import P
from shared.qt.fuzzy_multi_check import SCFuzzyMultiCheck
from shared.qt.search_bar import SCSearchBar
from data.models import text_field
from ui.components.virtual_grid import VirtualScrollGrid, FabCard


_SUBTYPES_BY_TYPE = {
    "weapons": {"lmg", "pistol", "rifle", "shotgun", "smg", "sniper"},
    "armour":  {"combat", "cosmonaut", "engineer", "environment", "explorer",
                "flightsuit", "hunter", "medic", "miner", "racer",
                "radiation", "salvager", "stealth", "undersuit"},
    "ammo":    {"ballistic", "electron", "laser", "plasma", "shotgun"},
}
_ALL_SUBTYPES = sorted(set().union(*_SUBTYPES_BY_TYPE.values()))


class FabricatorPage(QWidget):
    """Fabricator page with sidebar filters and blueprint card grid."""

    def __init__(self, parent, data_mgr, on_open_detail=None):
        super().__init__(parent)
        self._data = data_mgr
        self._fab_all_results = []
        self._on_open_detail = on_open_detail
        self._global_quality = 750
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

        # Obtainable (issue #21) — ported from the sibling Craft Database's
        # ui/filter_panel.py, same label, same tooltip wording, ON by default.
        # 875 of 1,607 4.10.1 blueprints are handed out by no mission reward
        # pool, so an unfiltered Fabricator is mostly recipes nobody can get.
        self._obtainable_cb = QCheckBox("Obtainable")
        self._obtainable_cb.setChecked(True)
        self._obtainable_cb.setStyleSheet(
            f"QCheckBox {{ color: {P.fg}; background: transparent; "
            f"font-family: Consolas; font-size: 8pt; padding: 2px 8px 4px 8px; }}")
        self._obtainable_cb.toggled.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._obtainable_cb)
        self._sync_obtainable_enabled()

        # Search
        section("SEARCH")
        self._search = SCSearchBar(placeholder="Search blueprints...", debounce_ms=300)
        self._search.search_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._search)

        # Type
        section("TYPE")
        self._type_btns = {}
        type_row = QHBoxLayout()
        type_row.setContentsMargins(8, 0, 8, 2)
        for t in ["weapons", "armour", "ammo"]:
            btn = QPushButton(t.title())
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
                QPushButton:checked {{ background: #1a3030; color: {P.accent}; }}
                QPushButton:hover {{ color: {P.fg}; }}
            """)
            btn.toggled.connect(lambda _: self.on_filter_change())
            type_row.addWidget(btn)
            self._type_btns[t] = btn
        type_row.addStretch(1)
        tw = QWidget()
        tw.setStyleSheet("background: transparent;")
        tw.setLayout(type_row)
        sb_lay.addWidget(tw)

        # Armor class
        section("ARMOR CLASS")
        self._ac_btns = {}
        ac_row = QHBoxLayout()
        ac_row.setContentsMargins(8, 0, 8, 2)
        for val in ["Light", "Medium", "Heavy"]:
            btn = QPushButton(val)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 8pt; padding: 2px 6px; }}
                QPushButton:checked {{ background: #1a3030; color: {P.accent}; }}
                QPushButton:hover {{ color: {P.fg}; }}
            """)
            btn.toggled.connect(lambda _: self.on_filter_change())
            ac_row.addWidget(btn)
            self._ac_btns[val] = btn
        ac_row.addStretch(1)
        acw = QWidget()
        acw.setStyleSheet("background: transparent;")
        acw.setLayout(ac_row)
        sb_lay.addWidget(acw)

        # Armor slot
        section("ARMOR SLOT")
        self._armor_slot_multi = SCFuzzyMultiCheck(label="All", items=["Helmet", "Torso", "Arms", "Legs", "Backpack", "Undersuit"])
        self._armor_slot_multi.selection_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._armor_slot_multi)

        # Subtype
        section("SUBTYPE")
        self._subtype_multi = SCFuzzyMultiCheck(label="All", items=[s.title() for s in _ALL_SUBTYPES])
        self._subtype_multi.selection_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._subtype_multi)

        # Manufacturer
        section("MANUFACTURER")
        self._mfr_multi = SCFuzzyMultiCheck(label="All", items=[
            "BEH", "CCC", "CDS", "CLDA", "DOOM", "GEM", "GRIN",
            "GYS", "HDGW", "HDTC", "KAP", "KLA", "KSAR", "MIS",
            "RRS", "RSI", "SYFB", "THP", "UNKN", "VGL"])
        self._mfr_multi.selection_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._mfr_multi)

        # Material
        section("MATERIAL")
        self._material_multi = SCFuzzyMultiCheck(label="All", items=[
            "Agricium", "Aluminum", "Aphorite", "Aslarite",
            "Beradom", "Beryl", "Carinite", "Copper", "Corundum",
            "Dolivine", "Gold", "Hadanite", "Hephaestanite",
            "Iron", "Janalite", "Laranite", "Lindinium", "Ouratite",
            "Quartz", "Riccite", "Sadaryx", "Saldynium (Ore)",
            "Savrilium", "Silicon", "Stileron", "Taranite",
            "Tin", "Titanium", "Torite", "Tungsten"])
        self._material_multi.selection_changed.connect(lambda _: self.on_filter_change())
        sb_lay.addWidget(self._material_multi)

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

        # Global quality control bar
        qbar = QWidget()
        qbar.setFixedHeight(32)
        qbar.setStyleSheet(f"background: {P.bg_primary};")
        qbar_l = QHBoxLayout(qbar)
        qbar_l.setContentsMargins(10, 4, 10, 4)
        qbar_l.setSpacing(8)
        qlbl = QLabel("GLOBAL QUALITY")
        qlbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {P.accent}; background: transparent;")
        qbar_l.addWidget(qlbl)
        self._q_slider = QSlider(Qt.Horizontal)
        self._q_slider.setRange(0, 1000)
        self._q_slider.setValue(self._global_quality)
        self._q_slider.setFixedWidth(240)
        self._q_slider.setStyleSheet(
            f"QSlider::groove:horizontal {{ background: {P.bg_input}; height: 4px; border-radius: 2px; }}"
            f"QSlider::handle:horizontal {{ background: {P.accent}; width: 12px; margin: -4px 0; border-radius: 6px; }}"
        )
        self._q_slider.valueChanged.connect(self._on_global_quality_changed)
        qbar_l.addWidget(self._q_slider)
        self._q_spin = QSpinBox()
        self._q_spin.setRange(0, 1000)
        self._q_spin.setValue(self._global_quality)
        self._q_spin.setFixedWidth(64)
        self._q_spin.setStyleSheet(
            f"QSpinBox {{ background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border};"
            f" font-family: Consolas; font-size: 9pt; padding: 1px 3px; }}"
        )
        self._q_spin.valueChanged.connect(self._on_global_quality_spin)
        qbar_l.addWidget(self._q_spin)
        qbar_l.addStretch(1)
        right.addWidget(qbar)

        self._count_label = QLabel("Loading crafting data...")
        self._count_label.setFixedHeight(24)
        self._count_label.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; padding-left: 10px; background: {P.bg_primary};")
        right.addWidget(self._count_label)

        self._vgrid = VirtualScrollGrid(
            card_width=300, row_height=120,
            fill_fn=self._fill_fab_card,
            on_click_fn=self._on_fab_click,
            card_class=FabCard,
        )
        right.addWidget(self._vgrid, 1)
        main.addLayout(right, 1)

    # ── Public API ──

    def set_count_message(self, msg):
        self._count_label.setText(msg)

    def rebuild_grid(self):
        self._vgrid.set_data(self._fab_all_results)

    def clear_grid(self):
        self._fab_all_results = []
        self._vgrid.set_data([])

    # ── Filter logic ──

    def _sync_obtainable_enabled(self):
        """Grey the Obtainable box out while the verdict is UNKNOWN (#21).

        Obtainability comes from the MISSION reward pools, which load on their
        own thread.  Leaving the box live and checked before they arrive would
        hide every blueprint and look like the data failed to load.
        """
        known = self._data.blueprint_obtainability_known()
        self._obtainable_cb.setEnabled(known)
        if known:
            self._obtainable_cb.setToolTip(
                "Only blueprints a player can get: handed out by at least one "
                "mission reward pool.\nUncheck to see every recipe in the game "
                "files.")
        else:
            self._obtainable_cb.setToolTip(
                "Waiting for mission data — until the reward pools load, there "
                "is nothing to judge obtainability against, so every blueprint "
                "is shown.")

    def on_filter_change(self):
        if not self._data.crafting_loaded:
            return

        self._sync_obtainable_enabled()
        search = (self._search.text() or "").lower()
        active_types = {t for t, btn in self._type_btns.items() if btn.isChecked()}
        active_ac = {k for k, btn in self._ac_btns.items() if btn.isChecked()}
        active_subtypes = {s.lower() for s in self._subtype_multi.get_selected()}
        active_armor_slot = set(self._armor_slot_multi.get_selected())
        active_mfr = set(self._mfr_multi.get_selected())
        active_material = set(self._material_multi.get_selected())
        obtainable_only = (self._obtainable_cb.isChecked()
                           and self._obtainable_cb.isEnabled())

        results = []
        for bp in self._data.crafting_blueprints:
            prod = self._data.get_blueprint_product(bp)

            # Issue #21 — `is False`, not `not ...`: an UNKNOWN verdict is kept.
            if obtainable_only and self._data.is_blueprint_obtainable(bp) is False:
                continue

            if search:
                name = self._data.get_blueprint_product_name(bp).lower()
                tag = (bp.get("tag") or "").lower()
                if search not in name and search not in tag:
                    continue

            if active_types and text_field(bp, "type") not in active_types:
                continue

            if active_subtypes and text_field(bp, "subtype") not in active_subtypes:
                continue

            if active_ac and prod:
                ast = (prod.get("attachSubType", "") or "").title()
                tags = (prod.get("tags", "") or "").lower()
                item_classes = set()
                if ast in ("Light", "Lightarmor"):
                    item_classes.add("Light")
                elif ast == "Medium":
                    item_classes.add("Medium")
                elif ast == "Heavy":
                    item_classes.add("Heavy")
                if "light" in tags or "kap_light" in tags:
                    item_classes.add("Light")
                if "medium" in tags:
                    item_classes.add("Medium")
                if "heavy" in tags:
                    item_classes.add("Heavy")
                if not item_classes & active_ac:
                    continue
            elif active_ac and not prod:
                continue

            if active_armor_slot:
                name_lower = self._data.get_blueprint_product_name(bp).lower()
                item_slot = ""
                if "helmet" in name_lower or "helm" in name_lower:
                    item_slot = "Helmet"
                elif "arms" in name_lower:
                    item_slot = "Arms"
                elif "legs" in name_lower:
                    item_slot = "Legs"
                elif "core" in name_lower or "torso" in name_lower:
                    item_slot = "Torso"
                elif "backpack" in name_lower:
                    item_slot = "Backpack"
                elif "undersuit" in name_lower:
                    item_slot = "Undersuit"
                if item_slot not in active_armor_slot:
                    continue

            if active_mfr and prod:
                if prod.get("manufacturerCode", "") not in active_mfr:
                    continue
            elif active_mfr and not prod:
                continue

            if active_material:
                tiers = bp.get("tiers", [])
                bp_materials = set()
                for tier in tiers:
                    for slot in tier.get("slots", []):
                        for opt in slot.get("options", []):
                            rn = opt.get("resourceName", "")
                            if rn:
                                bp_materials.add(rn)
                            itn = opt.get("itemName", "")
                            if itn:
                                bp_materials.add(itn)
                if not bp_materials & active_material:
                    continue

            results.append(bp)

        self._fab_all_results = results
        total = len(self._data.crafting_blueprints)
        shown = len(results)
        suffix = f" of {total}" if shown != total else ""
        self._count_label.setText(f"{shown}{suffix} Blueprints")
        self.rebuild_grid()

    def clear_filters(self):
        self._search.clear()
        for btn in self._type_btns.values():
            btn.setChecked(False)
        for btn in self._ac_btns.values():
            btn.setChecked(False)
        self._armor_slot_multi.set_selected([])
        self._subtype_multi.set_selected([])
        self._mfr_multi.set_selected([])
        self._material_multi.set_selected([])
        # Back to the DEFAULT, which is checked — matching Craft Database's
        # reset. "Clear" restores the default view, it does not unhide the
        # 875 unobtainable rows.
        self._obtainable_cb.setChecked(True)
        self.on_filter_change()

    # ── Card fill + click ──

    def _fill_fab_card(self, card, bp, idx):
        TYPE_COLORS = {"weapons": P.orange, "armour": P.accent, "ammo": P.yellow}

        name = self._data.get_blueprint_product_name(bp)
        bp_type = text_field(bp, "type", "?")
        bp_sub = text_field(bp, "subtype").replace("_", " ").title()
        tiers = bp.get("tiers") or []
        type_color = TYPE_COLORS.get(bp_type, P.fg)
        type_fg = "white" if bp_type != "ammo" else P.bg_primary

        res_text = ""
        time_text = ""
        if tiers:
            tier = tiers[0]
            craft_time = tier.get("craftTimeSeconds", 0)
            resources = []
            for s in tier.get("slots", []):
                for opt in s.get("options", []):
                    rn = opt.get("resourceName", "")
                    qty = opt.get("quantity", 0)
                    if rn:
                        resources.append(f"{rn} x{qty}")
            if resources:
                res_text = "  |  ".join(resources[:3])
                if len(resources) > 3:
                    res_text += f"  +{len(resources) - 3}"
            mins = craft_time // 60
            secs = craft_time % 60
            time_text = f"  {mins}m {secs}s" if mins else f"  {secs}s"

        card.set_data(name, bp_type.title(), type_color, type_fg, bp_sub, res_text, time_text)

    def _on_global_quality_changed(self, val: int):
        self._global_quality = val
        if self._q_spin.value() != val:
            self._q_spin.blockSignals(True)
            self._q_spin.setValue(val)
            self._q_spin.blockSignals(False)

    def _on_global_quality_spin(self, val: int):
        self._global_quality = val
        if self._q_slider.value() != val:
            self._q_slider.blockSignals(True)
            self._q_slider.setValue(val)
            self._q_slider.blockSignals(False)

    def _on_fab_click(self, bp, idx):
        if self._on_open_detail is not None:
            self._on_open_detail(bp, self._global_quality)
            return
        from ui.modals.blueprint_detail import BlueprintDetailModal
        BlueprintDetailModal(self.window(), bp, self._data,
                             initial_quality=self._global_quality)
