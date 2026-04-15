"""Permanent break-calculator side panel for the Scanner tab.

Uses the same "Advanced Breakability Assistance" sectioned layout
as the floating BreakBubble, with collapsible sections and the
charge simulation display.

The panel is stateless: the main app computes a :class:`BreakResult`
via ``compute_with_gadgets`` (same call the bubble uses) and pushes
everything into :meth:`BreakPanel.update_state`.
"""

from __future__ import annotations

from typing import Iterable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QSizePolicy,
)

from shared.qt.theme import P

ACCENT = "#33dd88"
RED = "#ff4444"
YELLOW = "#ffc107"
DIM = P.fg_dim

_LABEL_W = 110


# ─────────────────────────────────────────────────────────────
# Row helpers (matching break_bubble.py style)
# ─────────────────────────────────────────────────────────────

def _build_rows(
    parent: QWidget,
    rows: list[tuple[str, str, str]],
    label_width: int = _LABEL_W,
) -> list[QWidget]:
    """Build row widgets from (label, value, color) tuples."""
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
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        row_layout.addWidget(val, 1)

        widgets.append(row_widget)
    return widgets


def _build_separator(parent: QWidget) -> QFrame:
    line = QFrame(parent)
    line.setFrameShape(QFrame.HLine)
    line.setStyleSheet(f"color: {DIM}; background: {DIM};")
    line.setFixedHeight(1)
    return line


# ─────────────────────────────────────────────────────────────
# Collapsible section (same pattern as break_bubble.py)
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

        # Header row
        header = QWidget(self)
        header.setCursor(Qt.PointingHandCursor)
        header.setStyleSheet("background: transparent;")
        h_layout = QHBoxLayout(header)
        h_layout.setContentsMargins(0, 4, 0, 4)
        h_layout.setSpacing(6)

        self._arrow = QLabel("\u25bc", header)
        self._arrow.setStyleSheet(
            f"font-size: 8pt; color: {accent_color}; background: transparent;"
        )
        self._arrow.setFixedWidth(12)
        h_layout.addWidget(self._arrow)

        if icon:
            icon_lbl = QLabel(icon, header)
            icon_lbl.setStyleSheet(
                f"font-size: 10pt; color: {accent_color}; background: transparent;"
            )
            icon_lbl.setFixedWidth(18)
            h_layout.addWidget(icon_lbl)

        title_lbl = QLabel(title, header)
        title_lbl.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {accent_color}; background: transparent;"
        )
        h_layout.addWidget(title_lbl)

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

        # Content area
        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(18, 2, 0, 6)
        self._content_layout.setSpacing(2)
        layout.addWidget(self._content)

    def add_rows(self, rows: list[tuple[str, str, str]], label_width: int = _LABEL_W) -> None:
        for w in _build_rows(self._content, rows, label_width):
            self._content_layout.addWidget(w)

    def add_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        self._arrow.setText("\u25bc" if self._expanded else "\u25b6")


# ─────────────────────────────────────────────────────────────
# Main panel widget
# ─────────────────────────────────────────────────────────────

class BreakPanel(QWidget):
    """Live break-calculator sidebar using the sectioned layout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(280)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.setStyleSheet(f"background: {P.bg_primary};")

        self._root = QVBoxLayout(self)
        self._root.setContentsMargins(10, 6, 10, 8)
        self._root.setSpacing(2)

        self._children: list[QWidget] = []
        self._ship_label = ""
        self._build_idle()

    def _clear(self) -> None:
        for w in self._children:
            self._root.removeWidget(w)
            w.deleteLater()
        self._children.clear()

    def _build_idle(self) -> None:
        """Show the idle / waiting state."""
        self._clear()

        title = QLabel("ADVANCED BREAKABILITY ASSISTANCE", self)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent;"
        )
        self._root.addWidget(title)
        self._children.append(title)

        sep = _build_separator(self)
        self._root.addWidget(sep)
        self._children.append(sep)

        msg = QLabel("Waiting for rock data...", self)
        msg.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"color: {DIM}; background: transparent; padding: 12px 4px;"
        )
        msg.setWordWrap(True)
        self._root.addWidget(msg)
        self._children.append(msg)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._root.addWidget(spacer)
        self._children.append(spacer)

    # ── public API ──

    def clear(self) -> None:
        self._build_idle()

    def set_ship_label(self, text: str) -> None:
        self._ship_label = text or ""

    def update_state(
        self,
        *,
        mass: float | None,
        resistance: float | None,
        instability: float | None,
        ship_label: str = "",
        result=None,
        no_ship: bool = False,
    ) -> None:
        """Push a new snapshot of rock stats + break result into the panel."""
        if ship_label:
            self._ship_label = ship_label

        if no_ship:
            self._clear()
            self._build_idle_msg("Select a mining ship",
                                 "Open the Mining Ships tab to load a loadout.")
            return

        if mass is None or resistance is None:
            self._build_idle()
            return

        if result is None:
            self._build_idle()
            return

        # ── Rebuild the sectioned layout ──
        self._clear()

        can_break = not getattr(result, "insufficient", False)
        unbreakable = getattr(result, "unbreakable", False)
        accent = ACCENT if (can_break and not unbreakable) else RED

        # Title
        title = QLabel("ADVANCED BREAKABILITY ASSISTANCE", self)
        title.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {accent}; background: transparent;"
        )
        self._root.addWidget(title)
        self._children.append(title)

        sep = _build_separator(self)
        self._root.addWidget(sep)
        self._children.append(sep)

        # ── YOU section ──
        you_section = _CollapsibleSection(
            "YOU", "Active Miner", icon="\U0001f9d1",
            accent_color=accent, parent=self,
        )
        you_rows: list[tuple[str, str, str]] = []

        if self._ship_label:
            you_rows.append(("Ship:", self._ship_label, P.fg))
        if mass is not None:
            you_rows.append(("Mass:", f"{mass:,.0f} kg", P.fg))
        if resistance is not None:
            you_rows.append(("Resist:", f"{resistance:.0f}%", P.fg))
        if instability is not None:
            you_rows.append(("Instab:", f"{instability:.2f}", P.fg))

        # Verdict
        if unbreakable:
            you_rows.append(("Status:", "UNBREAKABLE", RED))
        elif not can_break:
            missing = getattr(result, "missing_power", 0.0) or 0.0
            status = (
                f"CANNOT BREAK (+{missing:,.0f} MW)"
                if missing > 0 else "CANNOT BREAK"
            )
            you_rows.append(("Status:", status, RED))
            gadget = getattr(result, "gadget_used", None)
            if gadget:
                you_rows.append(("Gadget:", gadget, YELLOW))
        else:
            pct = getattr(result, "percentage", None)
            pct_str = f"{pct:.0f}%" if pct is not None else "?"
            you_rows.append(("Power:", pct_str, ACCENT))

        if getattr(result, "active_modules_needed", 0):
            you_rows.append(
                ("Active Mods:", f"{result.active_modules_needed} activation(s)", YELLOW),
            )

        # Charge simulation
        cp = getattr(result, "charge_profile", None)
        if cp is not None and can_break and not unbreakable:
            throttle_color = YELLOW if cp.min_throttle_pct > 50 else ACCENT
            you_rows.append(("Min Throttle:", f"{cp.min_throttle_pct:.0f}%", throttle_color))
            if cp.est_total_time_sec < float("inf"):
                time_color = YELLOW if cp.est_total_time_sec > 60 else ACCENT
                you_rows.append(("Est. Time:", f"~{cp.est_total_time_sec:.0f}s", time_color))

        # Resistance match
        if can_break and not unbreakable:
            you_rows.append(("Resistance:", "\u2713 Can Break", ACCENT))
        elif not unbreakable:
            you_rows.append(("Resistance:", "\u2717 Needs Help", RED))

        # Laser list
        used_lasers = getattr(result, "used_lasers", []) or []
        for name in used_lasers:
            short = name.split(" > ", 1)[1] if " > " in name else name
            you_rows.append(("  Laser:", short, DIM))

        you_section.add_rows(you_rows)
        self._root.addWidget(you_section)
        self._children.append(you_section)

        # Spacer
        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._root.addWidget(spacer)
        self._children.append(spacer)

    def _build_idle_msg(self, title: str, detail: str) -> None:
        """Show a simple message state."""
        self._clear()

        hdr = QLabel("ADVANCED BREAKABILITY ASSISTANCE", self)
        hdr.setStyleSheet(
            f"font-family: Electrolize, Consolas, monospace; font-size: 9pt; "
            f"font-weight: bold; color: {ACCENT}; background: transparent;"
        )
        self._root.addWidget(hdr)
        self._children.append(hdr)

        sep = _build_separator(self)
        self._root.addWidget(sep)
        self._children.append(sep)

        msg = QLabel(f"{title}\n{detail}", self)
        msg.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 9pt; "
            f"color: {DIM}; background: transparent; padding: 12px 4px;"
        )
        msg.setWordWrap(True)
        self._root.addWidget(msg)
        self._children.append(msg)

        spacer = QWidget(self)
        spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self._root.addWidget(spacer)
        self._children.append(spacer)
