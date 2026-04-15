"""Floating HUD panel: Advanced Breakability Assistance.

Unified sectioned layout for all modes (solo, team, fleet, cluster).
Each section (YOU, TEAM SUPPORT, CLUSTER SUPPORT) is collapsible
with a clickable header that toggles content visibility.

Replaces the three separate display methods with a single
``show_breakability()`` call that adapts to the data provided.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer, QPropertyAnimation, QEasingCurve
from PySide6.QtGui import QColor, QPainter, QPen, QLinearGradient, QFont
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTabWidget,
    QFrame, QPushButton, QSizePolicy,
)

from shared.qt.theme import P

log = logging.getLogger(__name__)

ACCENT = "#33dd88"
RED = "#ff4444"
YELLOW = "#ffc107"
DIM = P.fg_dim

_FADE_DURATION_MS = 8000
_ANIMATION_MS = 300
_WIDTH = 420                  # wider to fit sectioned layout
_WIDTH_TWO_COL = 700          # two-column player cards
_ROW_HEIGHT = 20
_PADDING = 24
_LEFT_LABEL_W = 110           # wider for "Min Throttle:" etc.
_RIGHT_LABEL_W = 60
_SECTION_ICON_SIZE = 14


# ─────────────────────────────────────────────────────────────
# Row helpers
# ─────────────────────────────────────────────────────────────

def _build_rows(
    parent: QWidget,
    rows: list[tuple[str, str, str]],
    label_width: int = _LEFT_LABEL_W,
) -> list[QWidget]:
    """Build a list of row widgets from (label, value, color) tuples."""
    widgets: list[QWidget] = []
    for label_text, value_text, color in rows:
        row_widget = QWidget(parent)
        row_layout = QHBoxLayout(row_widget)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)

        if label_text:
            lbl = QLabel(label_text, row_widget)
            lbl.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"color: {DIM}; background: transparent;"
            )
            lbl.setFixedWidth(label_width)
            row_layout.addWidget(lbl)
        else:
            row_layout.addSpacing(label_width + 6)

        val = QLabel(value_text, row_widget)
        val.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {color}; background: transparent;"
        )
        val.setWordWrap(True)
        row_layout.addWidget(val, 1)

        widgets.append(row_widget)
    return widgets


def _build_separator(parent: QWidget) -> QFrame:
    """Build a thin horizontal separator line."""
    line = QFrame(parent)
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color: {DIM}; background: {DIM};")
    line.setFixedHeight(1)
    return line


def _force_topmost(widget: QWidget) -> None:
    """Force a widget to stay on top (fights borderless fullscreen games)."""
    try:
        import ctypes
        hwnd = int(widget.winId())
        ctypes.windll.user32.SetWindowPos(
            hwnd, -1, 0, 0, 0, 0,
            0x0002 | 0x0001 | 0x0040 | 0x0010,
        )
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# Collapsible section widget
# ─────────────────────────────────────────────────────────────

class _CollapsibleSection(QWidget):
    """Section with a clickable header that toggles content visibility."""

    def __init__(
        self,
        title: str,
        subtitle: str = "",
        icon: str = "",
        accent_color: str = ACCENT,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._expanded = True

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Header row (clickable) ──
        header = QWidget(self)
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet("background: transparent;")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(0, 4, 0, 4)
        h_layout.setSpacing(6)

        # Arrow indicator
        self._arrow = QLabel("\u25bc", header)  # ▼
        self._arrow.setStyleSheet(
            f"font-size: 8pt; color: {accent_color}; background: transparent;"
        )
        self._arrow.setFixedWidth(12)
        h_layout.addWidget(self._arrow)

        # Icon (optional)
        if icon:
            icon_lbl = QLabel(icon, header)
            icon_lbl.setStyleSheet(
                f"font-size: 10pt; color: {accent_color}; background: transparent;"
            )
            icon_lbl.setFixedWidth(18)
            h_layout.addWidget(icon_lbl)

        # Title
        title_lbl = QLabel(title, header)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {accent_color}; background: transparent;"
        )
        h_layout.addWidget(title_lbl)

        # Subtitle
        if subtitle:
            sub_lbl = QLabel(f"({subtitle})", header)
            sub_lbl.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 8pt; "
                f"color: {DIM}; background: transparent;"
            )
            h_layout.addWidget(sub_lbl)

        h_layout.addStretch(1)
        header.mousePressEvent = lambda e: self._toggle()
        layout.addWidget(header)

        # ── Content area ──
        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(18, 2, 0, 6)
        self._content_layout.setSpacing(2)
        layout.addWidget(self._content)

    def add_rows(self, rows: list[tuple[str, str, str]], label_width: int = _LEFT_LABEL_W) -> None:
        """Add stat rows to the content area."""
        for w in _build_rows(self._content, rows, label_width):
            self._content_layout.addWidget(w)

    def add_widget(self, widget: QWidget) -> None:
        """Add a custom widget to the content area."""
        self._content_layout.addWidget(widget)

    def add_two_column_cards(
        self,
        left_rows: list[tuple[str, str, str]],
        right_rows: list[tuple[str, str, str]],
    ) -> None:
        """Add a two-column layout (for side-by-side player cards)."""
        wrapper = QWidget(self._content)
        h = QHBoxLayout(wrapper)
        h.setContentsMargins(0, 0, 0, 0)
        h.setSpacing(20)

        for col_rows in (left_rows, right_rows):
            if not col_rows:
                continue
            col = QWidget(wrapper)
            vl = QVBoxLayout(col)
            vl.setContentsMargins(0, 0, 0, 0)
            vl.setSpacing(2)
            for w in _build_rows(col, col_rows, label_width=_LEFT_LABEL_W):
                vl.addWidget(w)
            vl.addStretch(1)
            h.addWidget(col, 1)

        self._content_layout.addWidget(wrapper)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        self._arrow.setText("\u25bc" if self._expanded else "\u25b6")  # ▼ / ▶
        # Trigger parent resize
        parent = self.parentWidget()
        while parent:
            if isinstance(parent, BreakBubble):
                parent._recalc_size()
                break
            parent = parent.parentWidget()


# ─────────────────────────────────────────────────────────────
# Main bubble widget
# ─────────────────────────────────────────────────────────────

class BreakBubble(QWidget):
    """Floating HUD panel: Advanced Breakability Assistance."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(14, 10, 14, 10)
        self._layout.setSpacing(2)

        self._children: list[QWidget] = []
        self._can_break = True
        self._is_fleet_mode = False
        self._last_tab_index = 0

        self._fade_timer = QTimer(self)
        self._fade_timer.setSingleShot(True)
        self._fade_timer.timeout.connect(self._start_fade_out)

    def _clear(self) -> None:
        """Remove all dynamic child widgets."""
        for w in self._children:
            self._layout.removeWidget(w)
            w.deleteLater()
        self._children.clear()

    def _recalc_size(self) -> None:
        """Recalculate panel size after a section collapse/expand."""
        self.adjustSize()
        self.update()

    # ─────────────────────────────────────────────────────────
    # Unified display method
    # ─────────────────────────────────────────────────────────

    def show_breakability(
        self,
        anchor_x: int,
        anchor_y: int,
        *,
        # Rock stats
        resource_name: str = "",
        mass: float | None = None,
        resistance: float | None = None,
        instability: float | None = None,
        # Break result
        power_required: float | None = None,
        power_percentage: float | None = None,
        can_break: bool = True,
        unbreakable: bool = False,
        missing_power: float = 0.0,
        used_lasers: list[str] | None = None,
        active_modules_needed: int = 0,
        gadget_recommendation: str = "",
        # Charge simulation
        min_throttle: float | None = None,
        est_crack_time: float | None = None,
        # Ship info for "YOU" section
        ship_name: str = "",
        laser_name: str = "",
        # Team support data (optional)
        team_members: list[dict] | None = None,
        # Cluster support data (optional)
        cluster_teams: list[dict] | None = None,
        # Fleet substitution data (optional)
        substitutes: list[dict] | None = None,
        home_team: list[dict] | None = None,
        additional_crew: list[dict] | None = None,
        search_scope: str = "solo",
        # Legacy fleet substitution tab data
        num_ships_needed: int = 1,
        ship_names: list[str] | None = None,
        solo_missing_power: float = 0.0,
        lp_power_pct: float = 0.0,
        lp_players: int = 0,
        lp_ships: list[str] | None = None,
        lp_stability: float = 0.0,
        lp_gadget: str = "",
        ls_power_pct: float = 0.0,
        ls_ship_count: int = 0,
        ls_ships: list[str] | None = None,
        ls_stability: float = 0.0,
        ls_gadget: str = "",
    ) -> None:
        """Unified breakability display — adapts to available data."""
        self._clear()
        self._can_break = can_break and not unbreakable
        self._is_fleet_mode = False

        # ── Title bar ──
        title_bar = QWidget(self)
        tb_layout = QHBoxLayout(title_bar)
        tb_layout.setContentsMargins(0, 0, 0, 4)
        tb_layout.setSpacing(0)

        title_lbl = QLabel("ADVANCED BREAKABILITY ASSISTANCE", title_bar)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 10pt; "
            f"font-weight: bold; color: {ACCENT if self._can_break else RED}; "
            f"background: transparent;"
        )
        tb_layout.addWidget(title_lbl, 1)

        close_btn = QLabel("\u2715", title_bar)  # ✕
        close_btn.setStyleSheet(
            f"font-size: 12pt; color: {DIM}; background: transparent; "
            f"padding: 0 4px;"
        )
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.mousePressEvent = lambda e: self.hide()
        tb_layout.addWidget(close_btn)

        self._layout.addWidget(title_bar)
        self._children.append(title_bar)

        # ── Separator after title ──
        sep = _build_separator(self)
        self._layout.addWidget(sep)
        self._children.append(sep)

        # ── Section 1: YOU (Active Miner) ──
        you_section = _CollapsibleSection(
            "YOU", "Active Miner", icon="\U0001f9d1",
            accent_color=ACCENT if self._can_break else RED,
            parent=self,
        )
        you_rows: list[tuple[str, str, str]] = []

        if ship_name:
            you_rows.append(("Ship:", ship_name, P.fg))
        if laser_name:
            you_rows.append(("Laser:", laser_name, P.fg))
        if resource_name:
            you_rows.append(("Resource:", resource_name, P.fg))
        if mass is not None:
            you_rows.append(("Mass:", f"{mass:,.0f} kg", P.fg))
        if resistance is not None:
            you_rows.append(("Resist:", f"{resistance:.0f}%", P.fg))
        if instability is not None:
            you_rows.append(("Instab:", f"{instability:,.2f}", P.fg))

        # Power / status
        if unbreakable:
            you_rows.append(("Status:", "UNBREAKABLE", RED))
        elif not can_break:
            status = (
                f"CANNOT BREAK (+{missing_power:,.0f} MW needed)"
                if missing_power > 0 else "CANNOT BREAK"
            )
            you_rows.append(("Status:", status, RED))
            if gadget_recommendation:
                you_rows.append(("Gadget:", gadget_recommendation, YELLOW))
        else:
            pct_str = (
                f"{power_percentage:.0f}%"
                if power_percentage is not None else "?"
            )
            scope_labels = {
                "solo": "Solo", "team": "Team",
                "cluster": "Cluster", "fleet": "Fleet",
            }
            scope = scope_labels.get(search_scope, "")
            if scope and scope != "Solo":
                pct_str += f"  ({scope})"
            you_rows.append(("Power:", pct_str, ACCENT))

        if active_modules_needed > 0:
            you_rows.append(
                ("Active Mods:", f"{active_modules_needed} activation(s)", YELLOW),
            )

        # Charge simulation
        if min_throttle is not None and can_break and not unbreakable:
            throttle_color = YELLOW if min_throttle > 50 else ACCENT
            you_rows.append(("Min Throttle:", f"{min_throttle:.0f}%", throttle_color))
        if est_crack_time is not None and can_break and not unbreakable:
            if est_crack_time < float("inf"):
                time_color = YELLOW if est_crack_time > 60 else ACCENT
                you_rows.append(("Est. Time:", f"~{est_crack_time:.0f}s", time_color))

        # Resistance match indicator
        if can_break and not unbreakable:
            you_rows.append(("Resistance:", "\u2713 Can Break", ACCENT))
        elif not unbreakable:
            you_rows.append(("Resistance:", "\u2717 Needs Help", RED))

        # Laser list (compact, under YOU section)
        if used_lasers and not (team_members or cluster_teams or home_team):
            for name in used_lasers:
                short = name.split(" > ", 1)[1] if " > " in name else name
                you_rows.append(("  Laser:", short, DIM))

        you_section.add_rows(you_rows)
        self._layout.addWidget(you_section)
        self._children.append(you_section)

        # ── Section 2: TEAM SUPPORT (Same Party) ──
        if team_members:
            sep2 = _build_separator(self)
            self._layout.addWidget(sep2)
            self._children.append(sep2)

            team_section = _CollapsibleSection(
                "TEAM SUPPORT", "Same Party", icon="\U0001f465",
                accent_color=ACCENT, parent=self,
            )

            # Show team members in pairs (two-column cards)
            for i in range(0, len(team_members), 2):
                left_member = team_members[i]
                right_member = team_members[i + 1] if i + 1 < len(team_members) else None

                left_card = self._member_card_rows(left_member)
                right_card = self._member_card_rows(right_member) if right_member else []
                team_section.add_two_column_cards(left_card, right_card)

            self._layout.addWidget(team_section)
            self._children.append(team_section)

        # ── Section 3: CLUSTER SUPPORT (Other Teams) ──
        if cluster_teams:
            sep3 = _build_separator(self)
            self._layout.addWidget(sep3)
            self._children.append(sep3)

            cluster_section = _CollapsibleSection(
                "CLUSTER SUPPORT", "Other Teams", icon="\U0001f30d",
                accent_color=ACCENT, parent=self,
            )

            for team_data in cluster_teams:
                team_name = team_data.get("team_name", "")
                members = team_data.get("members", [])
                if not members:
                    continue

                # Team header
                team_lbl_rows = [("", f"Team {team_name}", ACCENT)]
                cluster_section.add_rows(team_lbl_rows, label_width=0)

                for i in range(0, len(members), 2):
                    left_m = members[i]
                    right_m = members[i + 1] if i + 1 < len(members) else None
                    left_card = self._member_card_rows(left_m)
                    right_card = self._member_card_rows(right_m) if right_m else []
                    cluster_section.add_two_column_cards(left_card, right_card)

            self._layout.addWidget(cluster_section)
            self._children.append(cluster_section)

        # ── Section 4: Your Team + Substitutes (team mode fallback) ──
        if home_team:
            sep4 = _build_separator(self)
            self._layout.addWidget(sep4)
            self._children.append(sep4)

            home_section = _CollapsibleSection(
                "YOUR TEAM", "", icon="\u2694",
                accent_color=ACCENT, parent=self,
            )
            ht_rows: list[tuple[str, str, str]] = []
            self._append_ship_breakdown(ht_rows, home_team, accent=ACCENT)
            home_section.add_rows(ht_rows, label_width=_RIGHT_LABEL_W)
            self._layout.addWidget(home_section)
            self._children.append(home_section)

        if substitutes:
            sep5 = _build_separator(self)
            self._layout.addWidget(sep5)
            self._children.append(sep5)

            sub_section = _CollapsibleSection(
                "SUBSTITUTES", "", icon="\U0001f504",
                accent_color=YELLOW, parent=self,
            )
            sub_rows: list[tuple[str, str, str]] = []
            self._append_ship_breakdown(sub_rows, substitutes, accent=YELLOW)
            sub_section.add_rows(sub_rows, label_width=_RIGHT_LABEL_W)
            self._layout.addWidget(sub_section)
            self._children.append(sub_section)

        if additional_crew:
            for ac in additional_crew:
                crew_rows = [
                    ("Need Crew:", f"{ac['player_name']} \u2192 {ac['from_ship']}", YELLOW),
                ]
                for w in _build_rows(self, crew_rows):
                    self._layout.addWidget(w)
                    self._children.append(w)

        # ── Size calculation ──
        has_multi_col = bool(team_members or cluster_teams)
        width = _WIDTH_TWO_COL if has_multi_col else _WIDTH
        self.setMinimumWidth(width)
        self.adjustSize()

        # Persistent — no auto-fade
        self._show_at(anchor_x, anchor_y, fade=False)

    # ─────────────────────────────────────────────────────────
    # Fleet substitution mode (legacy tabbed — kept for compat)
    # ─────────────────────────────────────────────────────────

    def show_fleet_substitution(
        self,
        anchor_x: int,
        anchor_y: int,
        *,
        mass: float,
        resistance: float,
        instability: float | None = None,
        solo_missing_power: float = 0.0,
        lp_power_pct: float = 0.0,
        lp_players: int = 0,
        lp_ships: list[str] | None = None,
        lp_stability: float = 0.0,
        lp_gadget: str = "",
        ls_power_pct: float = 0.0,
        ls_ship_count: int = 0,
        ls_ships: list[str] | None = None,
        ls_stability: float = 0.0,
        ls_gadget: str = "",
    ) -> None:
        """Fleet substitution display — delegates to unified method."""
        self.show_breakability(
            anchor_x, anchor_y,
            mass=mass,
            resistance=resistance,
            instability=instability,
            can_break=False,
            missing_power=solo_missing_power,
            solo_missing_power=solo_missing_power,
            lp_power_pct=lp_power_pct,
            lp_players=lp_players,
            lp_ships=lp_ships,
            lp_stability=lp_stability,
            lp_gadget=lp_gadget,
            ls_power_pct=ls_power_pct,
            ls_ship_count=ls_ship_count,
            ls_ships=ls_ships,
            ls_stability=ls_stability,
            ls_gadget=ls_gadget,
        )

    def show_team_breakability(
        self,
        anchor_x: int,
        anchor_y: int,
        *,
        mass: float = 0.0,
        resistance: float = 0.0,
        instability: float | None = None,
        search_scope: str = "",
        can_break: bool = False,
        power_percentage: float = 0.0,
        used_lasers: list[str] | None = None,
        active_modules_needed: int = 0,
        gadget_recommendation: str = "",
        substitutes: list[dict] | None = None,
        home_team: list[dict] | None = None,
        additional_crew: list[dict] | None = None,
    ) -> None:
        """Team-mode display — delegates to unified method."""
        self.show_breakability(
            anchor_x, anchor_y,
            mass=mass,
            resistance=resistance,
            instability=instability,
            search_scope=search_scope,
            can_break=can_break,
            power_percentage=power_percentage,
            used_lasers=used_lasers,
            active_modules_needed=active_modules_needed,
            gadget_recommendation=gadget_recommendation,
            substitutes=substitutes,
            home_team=home_team,
            additional_crew=additional_crew,
        )

    # ─────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _member_card_rows(member: dict | None) -> list[tuple[str, str, str]]:
        """Build rows for a single team member card."""
        if member is None:
            return []
        rows = []
        if member.get("player"):
            rows.append(("Player:", member["player"], P.fg))
        if member.get("ship"):
            rows.append(("Ship:", member["ship"], DIM))
        if member.get("laser"):
            rows.append(("Laser:", member["laser"], DIM))
        if member.get("contribution_pct") is not None:
            ctype = member.get("contribution_type", "Power")
            pct = member["contribution_pct"]
            rows.append(("", f"+{pct}% {ctype.title()} Contribution", ACCENT))
        if member.get("gadgets"):
            for gadget_name, qty in member["gadgets"]:
                rows.append(("", f"\U0001f48e {gadget_name} x{qty}", YELLOW))
        return rows

    @staticmethod
    def _append_ship_breakdown(
        rows: list[tuple[str, str, str]],
        ships: list[dict],
        *,
        accent: str,
    ) -> None:
        """Render a hierarchical cluster > team > ship > laser list."""
        from collections import defaultdict
        by_cluster: dict[str, dict[str, list[dict]]] = defaultdict(
            lambda: defaultdict(list),
        )
        for sh in ships:
            cl = sh.get("cluster") or "(no cluster)"
            tm = sh.get("team_name") or "(unknown team)"
            by_cluster[cl][tm].append(sh)

        for cl_name in sorted(by_cluster.keys()):
            cl_display = cl_name if cl_name != "(no cluster)" else "\u2014"
            rows.append(("Cluster:", cl_display, accent))
            for tm_name in sorted(by_cluster[cl_name].keys()):
                rows.append(("  Team:", tm_name, accent))
                for sh in by_cluster[cl_name][tm_name]:
                    players = sh.get("player_names") or []
                    players_str = ", ".join(players) if players else "(no crew)"
                    rows.append((
                        "   Ship:",
                        f"{sh['ship_display']}  [{players_str}]",
                        DIM,
                    ))
                    for turret_name in sh.get("used_turrets", []):
                        short = turret_name
                        if " > " in short:
                            short = short.split(" > ", 1)[1]
                        rows.append(("    \u2514", short, DIM))

    @staticmethod
    def _stability_label(score: float) -> str:
        if score >= 1.5:
            return "Excellent"
        if score >= 1.0:
            return "Good"
        if score >= 0.5:
            return "Fair"
        return "Tight"

    @staticmethod
    def _stability_color(score: float) -> str:
        if score >= 0.5:
            return ACCENT
        return RED if score < 0.5 else YELLOW

    # ─────────────────────────────────────────────────────────
    # Display infrastructure
    # ─────────────────────────────────────────────────────────

    def _on_tab_changed(self, index: int) -> None:
        self._last_tab_index = index

    def _show_at(self, x: int, y: int, fade: bool = False) -> None:
        anim = getattr(self, "_anim", None)
        if anim is not None:
            try:
                anim.stop()
            except Exception:
                pass

        self.move(x, y)
        self.setWindowOpacity(1.0)
        if not self.isVisible():
            self.show()
        self.raise_()
        _force_topmost(self)

        self._fade_timer.stop()
        if fade:
            self._fade_timer.start(_FADE_DURATION_MS)
        self.update()

    def _start_fade_out(self) -> None:
        self._anim = QPropertyAnimation(self, b"windowOpacity")
        self._anim.setDuration(_ANIMATION_MS)
        self._anim.setStartValue(1.0)
        self._anim.setEndValue(0.0)
        self._anim.setEasingCurve(QEasingCurve.OutQuad)
        self._anim.finished.connect(self.hide)
        self._anim.start()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()

        bg = QColor(P.bg_primary)
        bg.setAlpha(230)
        painter.fillRect(0, 0, w, h, bg)

        accent = QColor(ACCENT if self._can_break else RED)
        glow = QLinearGradient(0, 0, 0, 8)
        gc = QColor(accent)
        gc.setAlpha(60)
        glow.setColorAt(0.0, gc)
        gc2 = QColor(accent)
        gc2.setAlpha(0)
        glow.setColorAt(1.0, gc2)
        painter.fillRect(0, 0, w, 8, glow)

        border = QColor(accent)
        border.setAlpha(140)
        painter.setPen(QPen(border, 1))
        painter.drawRect(0, 0, w - 1, h - 1)

        bloom = QColor(accent)
        bloom.setAlpha(20)
        painter.setPen(QPen(bloom, 3))
        painter.drawRect(1, 1, w - 3, h - 3)

        painter.end()
        super().paintEvent(event)
