"""Mission detail modal — 4-tab popup showing full mission info (PySide6)."""
from __future__ import annotations
import logging
import math

log = logging.getLogger(__name__)

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QPushButton,
    QTabWidget, QScrollArea, QGridLayout,
)

from shared.qt.theme import P
from ui.theme import tag_colors, faction_initials, strip_html, fmt_uec, fmt_time
from ui.modals.base import ModalBase


class MissionDetailModal(ModalBase):
    """Popup modal showing full mission details with 4 tabs."""

    def __init__(self, parent, contract: dict, data_mgr):
        self._contract = contract
        self._data = data_mgr
        super().__init__(parent, title="Mission Details", width=650, height=550)
        try:
            self._build_ui()
        except Exception:
            log.exception("[MissionDetail] _build_ui crashed for %s",
                          contract.get("title", contract.get("debugName", "?")))
        self.show()

    def _build_ui(self):
        c = self._contract
        faction = self._data.get_faction(c.get("factionGuid", ""))
        fname = faction.get("name", "Unknown")
        initials = faction_initials(fname)

        layout = self.body_layout

        # ── Header ──
        hdr = QWidget()
        hdr.setStyleSheet(f"background-color: {P.bg_secondary};")
        hdr_layout = QHBoxLayout(hdr)
        hdr_layout.setContentsMargins(12, 10, 12, 10)

        badge = QLabel(initials)
        badge.setFixedSize(40, 40)
        badge.setAlignment(Qt.AlignCenter)
        badge.setStyleSheet(f"""
            background-color: #1a2538; color: {P.accent};
            font-family: Consolas; font-size: 11pt; font-weight: bold;
        """)
        hdr_layout.addWidget(badge)

        title_col = QVBoxLayout()
        title_col.setSpacing(0)
        title_text = c.get("title", "Unknown Mission")
        if title_text.startswith("@"):
            title_text = c.get("debugName", title_text)
        title_lbl = QLabel(title_text)
        title_lbl.setWordWrap(True)
        title_lbl.setStyleSheet(f"font-family: Consolas; font-size: 12pt; font-weight: bold; color: {P.fg}; background: transparent;")
        title_col.addWidget(title_lbl)
        faction_lbl = QLabel(fname)
        faction_lbl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; color: {P.fg_dim}; background: transparent;")
        title_col.addWidget(faction_lbl)
        hdr_layout.addLayout(title_col, 1)

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
        hdr_layout.addWidget(close_btn)
        layout.addWidget(hdr)

        # ── Tags row ──
        tags_w = QWidget()
        tags_w.setStyleSheet(f"background-color: {P.bg_primary};")
        tags_layout = QHBoxLayout(tags_w)
        tags_layout.setContentsMargins(12, 4, 12, 4)
        tags_layout.setSpacing(4)

        tags = []
        for s in (c.get("systems") or []):
            tags.append(("system", s))
        mt = c.get("missionType", "")
        if mt:
            tags.append(("type", mt))
        if not c.get("illegal"):
            tags.append(("legal", "LEGAL"))
        else:
            tags.append(("illegal", "ILLEGAL"))
        prereqs = c.get("prerequisites") or {}
        if prereqs.get("completedContractTags"):
            tags.append(("chain", "CHAIN"))
        cat = c.get("category", "")
        if cat:
            tags.append(("cat", cat.title()))

        for tag_type, tag_text in tags:
            if tag_type in ("system", "type"):
                bg_c, fg_c = tag_colors(tag_text)
            elif tag_type in ("legal", "illegal"):
                bg_c, fg_c = tag_colors(tag_text.upper())
            elif tag_type == "chain":
                bg_c, fg_c = tag_colors("CHAIN")
            else:
                bg_c, fg_c = tag_colors(tag_text.lower())
            lbl = QLabel(f" {tag_text} ")
            lbl.setStyleSheet(f"background-color: {bg_c}; color: {fg_c}; font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 1px 4px;")
            tags_layout.addWidget(lbl)
        tags_layout.addStretch(1)
        layout.addWidget(tags_w)

        # ── Tabs ──
        tabs = QTabWidget()
        tabs.setStyleSheet(f"""
            QTabWidget::pane {{ border: none; background: {P.bg_primary}; }}
        """)
        layout.addWidget(tabs, 1)

        for builder, label in [
            (self._build_overview, "OVERVIEW"),
            (self._build_requirements, "REQUIREMENTS"),
            (self._build_calculator, "CALCULATOR"),
            (self._build_community, "COMMUNITY"),
        ]:
            try:
                tabs.addTab(builder(), label)
            except Exception:
                log.exception("[MissionDetail] %s tab crashed", label)
                fallback, flay = self._make_scroll_page()
                self._info(flay, f"Error loading {label} tab.", P.red)
                tabs.addTab(fallback, label)

    def _make_scroll_page(self) -> tuple:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"QScrollArea {{ border: none; background: {P.bg_primary}; }}")
        inner = QWidget()
        inner.setStyleSheet(f"background: {P.bg_primary};")
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(12, 6, 12, 12)
        lay.setSpacing(4)
        scroll.setWidget(inner)
        return scroll, lay

    def _section(self, lay, text, color=None):
        lbl = QLabel(text)
        c = color or P.fg_dim
        lbl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; font-weight: bold; color: {c}; background: transparent;")
        lay.addWidget(lbl)

    def _info(self, lay, text, color=None):
        lbl = QLabel(text)
        c = color or P.fg
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; color: {c}; background: transparent;")
        lay.addWidget(lbl)

    def _sep(self, lay):
        s = QFrame()
        s.setFrameShape(QFrame.HLine)
        s.setStyleSheet(f"color: {P.border}; background: {P.border};")
        s.setFixedHeight(1)
        lay.addWidget(s)

    def _build_overview(self) -> QScrollArea:
        scroll, lay = self._make_scroll_page()
        c = self._contract
        faction = self._data.get_faction(c.get("factionGuid", ""))

        self._info(lay, faction.get("name", "Unknown"), P.fg)

        desc = strip_html(c.get("description", ""))
        if desc and not desc.startswith("@"):
            self._info(lay, desc)

        reward = c.get("rewardUEC")
        if reward is not None:
            self._section(lay, "REWARD")
            self._info(lay, fmt_uec(reward), P.yellow)

        buyin = c.get("buyIn")
        if buyin:
            self._section(lay, "BUY-IN")
            self._info(lay, fmt_uec(buyin), P.red)

        hauling = c.get("haulingOrders")
        if hauling and isinstance(hauling, list):
            self._section(lay, "HAULING ORDERS")
            for ho in hauling:
                if not isinstance(ho, dict):
                    continue
                res_obj = ho.get("resource")
                res = res_obj.get("name", "Unknown cargo") if isinstance(res_obj, dict) else str(res_obj or "Unknown cargo")
                mn = ho.get("minSCU", 0)
                mx = ho.get("maxSCU", 0)
                self._info(lay, f"  {res}: {mn}\u2013{mx} SCU", P.orange)

        bp_rewards = c.get("blueprintRewards")
        if bp_rewards and isinstance(bp_rewards, list):
            self._section(lay, "BLUEPRINT REWARDS")
            for reward_entry in bp_rewards:
                if not isinstance(reward_entry, dict):
                    continue
                pool_id = reward_entry.get("blueprintPool", "")
                pool_name = reward_entry.get("poolName", "")
                chance = reward_entry.get("chance", 1)
                pool = self._data.blueprint_pools.get(pool_id, {})
                pool_display = pool.get("name", pool_name) or pool_name
                blueprints = pool.get("blueprints", [])
                chance_str = f" ({int(chance * 100)}%)" if chance < 1 else ""
                self._info(lay, f"  \U0001f527 {pool_display}{chance_str}", P.accent)
                if blueprints:
                    for bp_item in blueprints[:8]:
                        bp_name = bp_item.get("name", "?") if isinstance(bp_item, dict) else str(bp_item)
                        self._info(lay, f"     \u2198 {bp_name}", P.green)

        lay.addStretch(1)
        return scroll

    def _build_requirements(self) -> QScrollArea:
        scroll, lay = self._make_scroll_page()
        c = self._contract
        prereqs = c.get("prerequisites") or {}

        ct = prereqs.get("completedContractTags")
        if ct and isinstance(ct, dict):
            self._section(lay, "MISSION CHAIN", P.fg)
            req_tags = ct.get("tags", [])
            if req_tags:
                self._info(lay, "REQUIRES COMPLETION OF:")
                for tag in req_tags:
                    lbl = QLabel(f" {tag} ")
                    lbl.setStyleSheet(f"background-color: #1a2538; color: {P.accent}; font-family: Consolas; font-size: 9pt; padding: 2px 4px;")
                    lay.addWidget(lbl)

        intros = c.get("linkedIntros")
        if intros and isinstance(intros, list):
            self._section(lay, "CHAIN STARTS WITH:")
            for intro in intros:
                if not isinstance(intro, dict):
                    continue
                name = intro.get("title", intro.get("debugName", "?"))
                if name.startswith("@"):
                    name = intro.get("debugName", name)
                self._info(lay, f" {name} ", P.green)

        self._sep(lay)

        avail = self._data.get_availability(c.get("availabilityIndex"))
        flags = [
            ("SHAREABLE", c.get("canBeShared", False)),
            ("ILLEGAL", c.get("illegal", False)),
            ("ONCE ONLY", avail.get("onceOnly", False)),
            ("RE-ACCEPT AFTER ABANDON", avail.get("canReacceptAfterAbandoning", False)),
            ("RE-ACCEPT AFTER FAIL", avail.get("canReacceptAfterFailing", False)),
            ("AVAILABLE IN PRISON", avail.get("availableInPrison", False)),
        ]

        grid_w = QWidget()
        grid_w.setStyleSheet(f"background: {P.bg_primary};")
        grid = QGridLayout(grid_w)
        grid.setSpacing(2)
        for i, (label, val) in enumerate(flags):
            row, col = i // 2, i % 2
            cell = QWidget()
            cell.setStyleSheet(f"background-color: {P.bg_card}; padding: 6px;")
            cl = QVBoxLayout(cell)
            cl.setContentsMargins(10, 6, 10, 6)
            lbl = QLabel(label)
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
            cl.addWidget(lbl)
            color = P.green if val else P.red
            vlbl = QLabel("Yes" if val else "No")
            vlbl.setStyleSheet(f"font-family: Consolas; font-size: 11pt; font-weight: bold; color: {color}; background: transparent;")
            cl.addWidget(vlbl)
            grid.addWidget(cell, row, col)
        lay.addWidget(grid_w)

        cd = avail.get("personalCooldownTime", 0)
        if cd:
            self._section(lay, "COOLDOWN")
            self._info(lay, fmt_time(cd))

        lay.addStretch(1)
        return scroll

    def _build_calculator(self) -> QScrollArea:
        scroll, lay = self._make_scroll_page()
        c = self._contract
        faction = self._data.get_faction(c.get("factionGuid", ""))
        fname = faction.get("name", "Unknown")

        self._section(lay, "REWARDS", P.yellow)

        reward = c.get("rewardUEC")
        rep_xp = 0
        scope_name = ""
        scope_guid = ""
        fri = c.get("factionRewardsIndex")
        if fri is not None:
            try:
                fr_pool = self._data.faction_rewards_pools[fri]
                if isinstance(fr_pool, list) and fr_pool:
                    for entry in fr_pool:
                        if isinstance(entry, dict):
                            rep_xp = entry.get("amount", 0) or 0
                            scope_guid = entry.get("scopeGuid", "")
                            break
            except (IndexError, TypeError):
                pass

        if scope_guid and scope_guid in self._data.scopes:
            scope_obj = self._data.scopes[scope_guid]
            scope_name = scope_obj.get("scopeName", "")

        parts = []
        if reward:
            try:
                parts.append(f"UEC: {int(reward):,}")
            except (ValueError, TypeError):
                parts.append(f"UEC: {reward}")
        if rep_xp:
            parts.append(f"REP/MISSION: {rep_xp} XP")
        parts.append(f"FACTION: {fname}")
        if scope_name:
            parts.append(f"SCOPE: {scope_name}")
        self._info(lay, "  ".join(parts))

        # Rank table
        ranks = []
        if scope_guid and scope_guid in self._data.scopes:
            scope_obj = self._data.scopes[scope_guid]
            ranks = scope_obj.get("ranks", [])

        ms = c.get("minStanding") or {}
        min_rank_idx = 0
        min_rank_name = ""
        if isinstance(ms, dict):
            min_rank_name = ms.get("name", "")
            min_rank_idx = ms.get("rankIndex", 0) or 0

        if ranks:
            self._sep(lay)
            # Header
            hdr_w = QWidget()
            hdr_w.setStyleSheet(f"background: transparent;")
            hdr_l = QHBoxLayout(hdr_w)
            hdr_l.setContentsMargins(0, 0, 0, 0)
            for text, w in [("RANK", 200), ("XP TO FILL", 100), ("MISSIONS", 80)]:
                lbl = QLabel(text)
                lbl.setFixedWidth(w)
                lbl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; font-weight: bold; color: {P.fg_dim}; background: transparent;")
                hdr_l.addWidget(lbl)
            lay.addWidget(hdr_w)

            for rank in sorted(ranks, key=lambda r: r.get("rankIndex", 0)):
                r_idx = rank.get("rankIndex", 0)
                r_name = rank.get("name", "?")
                if r_name.startswith("@"):
                    r_name = r_name.split("_")[-1] if "_" in r_name else r_name
                r_xp = rank.get("rangeXP", 0) or 0
                missions_to_fill = math.ceil(r_xp / rep_xp) if rep_xp > 0 else 0
                is_min = (r_idx == min_rank_idx and min_rank_name)
                is_max = (r_idx == len(ranks) - 1)
                is_below_min = (r_idx < min_rank_idx)

                row_bg = P.bg_card if r_idx % 2 == 0 else P.bg_primary
                if is_min:
                    row_bg = "#1a2a1a"
                fg_color = P.fg_disabled if is_below_min else P.fg

                row_w = QWidget()
                row_w.setStyleSheet(f"background-color: {row_bg};")
                row_l = QHBoxLayout(row_w)
                row_l.setContentsMargins(6, 3, 6, 3)

                n_lbl = QLabel(r_name)
                n_lbl.setFixedWidth(200)
                weight = "bold" if is_min else "normal"
                n_lbl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: {weight}; color: {fg_color}; background: transparent;")
                row_l.addWidget(n_lbl)

                xp_text = f"{r_xp:,}" if r_xp and not is_max else "\u2014"
                xp_lbl = QLabel(xp_text)
                xp_lbl.setFixedWidth(100)
                xp_lbl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; color: {fg_color}; background: transparent;")
                row_l.addWidget(xp_lbl)

                m_text = str(missions_to_fill) if missions_to_fill and not is_max else "\u2014"
                m_color = P.accent if is_min else (P.fg_disabled if is_below_min else P.fg)
                m_lbl = QLabel(m_text)
                m_lbl.setFixedWidth(80)
                m_lbl.setStyleSheet(f"font-family: Consolas; font-size: 10pt; font-weight: bold; color: {m_color}; background: transparent;")
                row_l.addWidget(m_lbl)

                lay.addWidget(row_w)

        lay.addStretch(1)
        return scroll

    def _build_community(self) -> QScrollArea:
        scroll, lay = self._make_scroll_page()
        self._info(lay, "Community data coming soon.", P.fg_dim)
        self._info(lay, "(Requires Supabase integration for time entries,\n difficulty ratings, and satisfaction scores.)", P.fg_disabled)
        lay.addStretch(1)
        return scroll
