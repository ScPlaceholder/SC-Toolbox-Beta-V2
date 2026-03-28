"""Blueprint detail modal — crafting recipe viewer (PySide6)."""
from __future__ import annotations
import logging

from PySide6.QtCore import Qt

log = logging.getLogger(__name__)
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QScrollArea, QSlider,
)

from shared.qt.theme import P
from ui.modals.base import ModalBase


class BlueprintDetailModal(ModalBase):
    """Popup showing full crafting recipe for a blueprint."""

    def __init__(self, parent, bp: dict, data_mgr):
        self._bp = bp
        self._data = data_mgr
        name = data_mgr.get_blueprint_product_name(bp)
        super().__init__(parent, title=f"Blueprint: {name}", width=600, height=520)
        try:
            self._build_ui()
        except Exception:
            log.exception("[BlueprintDetail] _build_ui crashed for %s", name)
        self.show()

    def _build_ui(self):
        bp = self._bp
        name = self._data.get_blueprint_product_name(bp)
        product = self._data.get_blueprint_product(bp)
        bp_type = bp.get("type", "?")
        bp_sub = bp.get("subtype", "").replace("_", " ").title()
        tiers = bp.get("tiers", [])
        dismantle = self._data.crafting_dismantle

        TYPE_COLORS = {"weapons": P.orange, "armour": P.accent, "ammo": P.yellow}
        type_color = TYPE_COLORS.get(bp_type, P.fg)

        layout = self.body_layout

        # Close button at top right
        close_row = QHBoxLayout()
        close_row.setContentsMargins(0, 4, 4, 0)
        close_row.addStretch(1)
        close_btn = QPushButton("x")
        close_btn.setObjectName("detailClose")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton#detailClose {{ background: transparent; color: {P.fg_dim}; border: none;
                          font-family: Consolas; font-size: 13pt; font-weight: bold;
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

        def _lbl(text, color=P.fg, size="10pt", bold=False, wrap=False):
            l = QLabel(text)
            w = "bold" if bold else "normal"
            l.setStyleSheet(f"font-family: Consolas; font-size: {size}; font-weight: {w}; color: {color}; background: transparent;")
            if wrap:
                l.setWordWrap(True)
            lay.addWidget(l)
            return l

        def _sep():
            s = QFrame()
            s.setFrameShape(QFrame.HLine)
            s.setStyleSheet(f"color: {P.border}; background: {P.border};")
            s.setFixedHeight(1)
            lay.addWidget(s)

        # Header
        _lbl(name, P.fg, "15pt", True)

        # Type + subtype
        tag_w = QWidget()
        tag_w.setStyleSheet(f"background: transparent;")
        tag_l = QHBoxLayout(tag_w)
        tag_l.setContentsMargins(0, 0, 0, 0)
        tag_l.setSpacing(6)
        type_fg = "white" if bp_type != "ammo" else P.bg_primary
        t_lbl = QLabel(bp_type.title())
        t_lbl.setStyleSheet(f"background-color: {type_color}; color: {type_fg}; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 0 6px;")
        tag_l.addWidget(t_lbl)
        if bp_sub:
            s_lbl = QLabel(bp_sub)
            s_lbl.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; background: transparent;")
            tag_l.addWidget(s_lbl)
        tag_l.addStretch(1)
        lay.addWidget(tag_w)

        tag = bp.get("tag", "")
        if tag:
            _lbl(tag, P.fg_disabled, "7pt")

        # Product stats
        if product:
            _sep()
            _lbl("PRODUCT STATS", P.fg_dim, "10pt", True)
            mfr = product.get("manufacturer", "")
            mfr_code = product.get("manufacturerCode", "")
            if mfr:
                mfr_display = f"{mfr_code} \u2014 {mfr}" if mfr_code else mfr
                _lbl(f"  Manufacturer: {mfr_display}", P.fg, "9pt")
            for key, label in [("size", "Size"), ("grade", "Grade"), ("mass", "Mass")]:
                val = product.get(key)
                if val is not None:
                    _lbl(f"  {label}: {val}", P.fg, "9pt")

        # Crafting tiers
        for ti, tier in enumerate(tiers):
            _sep()
            craft_time = tier.get("craftTimeSeconds", 0)
            mins = craft_time // 60
            secs = craft_time % 60
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"
            tier_label = "CRAFTING RECIPE" if len(tiers) == 1 else f"TIER {ti + 1}"

            tier_hdr = QWidget()
            tier_hdr.setStyleSheet("background: transparent;")
            th_l = QHBoxLayout(tier_hdr)
            th_l.setContentsMargins(0, 0, 0, 0)
            tl = QLabel(tier_label)
            tl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {type_color}; background: transparent;")
            th_l.addWidget(tl)
            th_l.addStretch(1)
            tt = QLabel(f"\u23f1 {time_str}")
            tt.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
            th_l.addWidget(tt)
            lay.addWidget(tier_hdr)

            for slot in tier.get("slots", []):
                slot_name = slot.get("name", "?")
                options = slot.get("options", [])
                modifiers = slot.get("modifiers", [])

                slot_w = QWidget()
                slot_w.setStyleSheet(f"background-color: {P.bg_card}; border: 1px solid {P.border};")
                sl = QVBoxLayout(slot_w)
                sl.setContentsMargins(10, 8, 10, 8)
                sl.setSpacing(2)

                sn = QLabel(slot_name)
                sn.setStyleSheet(f"font-family: Consolas; font-size: 11pt; font-weight: bold; color: {P.accent}; background: transparent;")
                sl.addWidget(sn)

                for opt in options:
                    opt_type = opt.get("type", "")
                    qty = opt.get("quantity", 0)
                    min_q = opt.get("minQuality", 0)

                    row = QWidget()
                    row.setStyleSheet("background: transparent;")
                    rl = QHBoxLayout(row)
                    rl.setContentsMargins(0, 2, 0, 0)

                    if opt_type == "resource":
                        res_name = opt.get("resourceName", "?")
                        icon = QLabel("\u26cf")
                        icon.setStyleSheet(f"color: {P.orange}; font-size: 10pt; background: transparent;")
                        rl.addWidget(icon)
                        nl = QLabel(f"  {res_name}")
                        nl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.fg}; background: transparent;")
                        rl.addWidget(nl, 1)
                    elif opt_type == "item":
                        item_name = opt.get("itemName", "?")
                        icon = QLabel("\U0001f48e")
                        icon.setStyleSheet(f"color: {P.purple}; font-size: 10pt; background: transparent;")
                        rl.addWidget(icon)
                        nl = QLabel(f"  {item_name}")
                        nl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.fg}; background: transparent;")
                        rl.addWidget(nl, 1)
                    else:
                        rl.addStretch(1)

                    ql = QLabel(f"(min {min_q})")
                    ql.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
                    rl.addWidget(ql)
                    al = QLabel(f"{qty} SCU")
                    al.setStyleSheet(f"font-family: Consolas; font-size: 10pt; color: {P.fg}; background: transparent;")
                    rl.addWidget(al)
                    sl.addWidget(row)

                # Quality slider + modifiers
                if modifiers:
                    msep = QFrame()
                    msep.setFrameShape(QFrame.HLine)
                    msep.setStyleSheet(f"color: {P.border}; background: {P.border};")
                    msep.setFixedHeight(1)
                    sl.addWidget(msep)

                    slider_row = QWidget()
                    slider_row.setStyleSheet("background: transparent;")
                    sr_l = QHBoxLayout(slider_row)
                    sr_l.setContentsMargins(0, 4, 0, 0)
                    qlbl = QLabel("QUALITY")
                    qlbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; font-weight: bold; color: {P.fg_dim}; background: transparent;")
                    sr_l.addWidget(qlbl)
                    slider = QSlider(Qt.Horizontal)
                    slider.setRange(0, 1000)
                    slider.setValue(750)
                    sr_l.addWidget(slider, 1)
                    q_display = QLabel("750")
                    q_display.setFixedWidth(40)
                    q_display.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.fg}; background: {P.bg_input}; padding: 2px;")
                    q_display.setAlignment(Qt.AlignCenter)
                    sr_l.addWidget(q_display)
                    sl.addWidget(slider_row)

                    mod_labels = []
                    for mod in modifiers:
                        prop = mod.get("propertyName", mod.get("propertyKey", "?"))
                        mr = QWidget()
                        mr.setStyleSheet("background: transparent;")
                        mrl = QHBoxLayout(mr)
                        mrl.setContentsMargins(0, 0, 0, 0)
                        name_l = QLabel(prop)
                        name_l.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg}; background: transparent;")
                        mrl.addWidget(name_l, 1)
                        factor_l = QLabel("")
                        factor_l.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
                        mrl.addWidget(factor_l)
                        pct_l = QLabel("")
                        pct_l.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {P.green}; background: transparent;")
                        mrl.addWidget(pct_l)
                        sl.addWidget(mr)
                        mod_labels.append((name_l, factor_l, pct_l))

                    def _make_updater(sl_widget, disp, mods, labels):
                        def _update(val):
                            disp.setText(str(val))
                            for i, mod in enumerate(mods):
                                if i >= len(labels):
                                    break
                                start_q = mod.get("startQuality", 0)
                                end_q = mod.get("endQuality", 1000)
                                mod_start = mod.get("modifierAtStart", 1)
                                mod_end = mod.get("modifierAtEnd", 1)
                                if end_q != start_q:
                                    t = max(0, min(1, (val - start_q) / (end_q - start_q)))
                                else:
                                    t = 1
                                factor = mod_start + (mod_end - mod_start) * t
                                pct = (factor - 1) * 100
                                pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
                                pct_color = P.green if pct >= 0 else P.red
                                _, factor_lbl, pct_lbl = labels[i]
                                factor_lbl.setText(f"\u00d7{factor:.3f}")
                                pct_lbl.setText(pct_str)
                                pct_lbl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {pct_color}; background: transparent;")
                        return _update

                    updater = _make_updater(slider, q_display, modifiers, mod_labels)
                    slider.valueChanged.connect(updater)
                    updater(750)

                lay.addWidget(slot_w)

        # Dismantle
        if dismantle:
            _sep()
            _lbl("DISMANTLE", P.red, "10pt", True)
            eff = dismantle.get("efficiency", 0.5)
            dt = dismantle.get("dismantleTimeSeconds", 15)
            _lbl(f"  Efficiency: {eff * 100:.0f}%  \u2022  Time: {dt}s", P.fg_dim, "9pt")

        # Missions that reward this blueprint
        bp_pool_ids = set()
        for pool_id, pool in self._data.blueprint_pools.items():
            for bp_item in pool.get("blueprints", []):
                bp_name_check = bp_item.get("name", "") if isinstance(bp_item, dict) else ""
                if bp_name_check == name:
                    bp_pool_ids.add(pool_id)
                    break

        if bp_pool_ids:
            missions = []
            for contract in self._data.contracts:
                for reward_entry in (contract.get("blueprintRewards") or []):
                    if reward_entry.get("blueprintPool") in bp_pool_ids:
                        title = contract.get("title", "?")
                        if title.startswith("@"):
                            title = contract.get("debugName", title)
                        faction = self._data.get_faction(contract.get("factionGuid", ""))
                        fname = faction.get("name", "?")
                        chance = reward_entry.get("chance", 1)
                        missions.append((title, fname, chance))
                        break

            if missions:
                _sep()
                _lbl(f"MISSIONS THAT REWARD THIS ({len(missions)})", P.green, "10pt", True)
                for title, fname, chance in missions[:15]:
                    chance_str = f" ({int(chance * 100)}%)" if chance < 1 else ""
                    _lbl(f"  \u2022 {title}{chance_str}", P.fg, "9pt", wrap=True)
                    _lbl(f"    {fname}", P.fg_dim, "8pt")

        lay.addStretch(1)
