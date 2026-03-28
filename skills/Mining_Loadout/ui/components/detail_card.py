"""Pop-out detail card component — PySide6 version."""
import logging
from typing import Any, Dict, List, Optional, Tuple

import shared.path_setup  # noqa: E402  # centralised path config

from PySide6.QtCore import Qt, QPoint
from PySide6.QtGui import QFont, QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QTextEdit,
    QCheckBox, QFrame, QPushButton, QSizePolicy,
)

from shared.qt.theme import P
from models.items import GadgetItem, LaserItem, ModuleItem
from ui.constants import MAX_PINNED_CARDS

log = logging.getLogger("MiningLoadout.ui.card")


class DetailCardManager:
    """Manages pinned detail cards for mining items."""

    def __init__(self, parent_window: QWidget, opacity: float):
        self._parent = parent_window
        self._opacity = opacity
        self._pinned_cards: List[Dict] = []

    @property
    def pinned_cards(self) -> List[Dict]:
        return self._pinned_cards

    def close_card(self, key: str) -> None:
        card = next((c for c in self._pinned_cards if c["key"] == key), None)
        if card:
            try:
                card["popup"].close()
                card["popup"].deleteLater()
            except RuntimeError:
                log.debug("Failed to destroy card popup for key=%s", key)
            self._pinned_cards = [c for c in self._pinned_cards if c["key"] != key]

    def _evict_oldest_unlocked(self) -> bool:
        for card in self._pinned_cards:
            lock_cb = card.get("lock_cb")
            if not (lock_cb and lock_cb.isChecked()):
                self.close_card(card["key"])
                return True
        return False

    def _card_position(self, idx: int) -> Tuple[int, int]:
        pos = self._parent.pos()
        size = self._parent.size()
        card_w, card_h = 430, 460
        base_x = pos.x() + (size.width() - card_w) // 2
        base_y = pos.y() + (size.height() - card_h) // 2
        return base_x + idx * 26, base_y + idx * 30

    def pin_item(self, kind: str, item: Any) -> None:
        if not item:
            return

        if kind == "laser":
            key = f"laser_{item.id}"
            title, subtitle = "LASER DETAIL", item.name
        elif kind == "module":
            key = f"module_{item.id}"
            title, subtitle = "MODULE DETAIL", item.name
        elif kind == "gadget":
            key = f"gadget_{item.id}"
            title, subtitle = "GADGET DETAIL", item.name
        else:
            return

        existing = next((c for c in self._pinned_cards if c["key"] == key), None)
        if existing:
            try:
                existing["popup"].raise_()
            except RuntimeError:
                log.debug("Failed to raise popup for key=%s", key)
            return

        if len(self._pinned_cards) >= MAX_PINNED_CARDS:
            if not self._evict_oldest_unlocked():
                return

        popup, text_edit, lock_cb = self._make_card(key, title, subtitle)
        _fill_item_card(text_edit, kind, item)
        self._pinned_cards.append({
            "key": key, "popup": popup, "text": text_edit,
            "type": kind, "data": item, "lock_cb": lock_cb,
        })

    def _make_card(self, key: str, title: str, subtitle: str) -> Tuple[QWidget, QTextEdit, QCheckBox]:
        popup = QWidget(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        popup.setObjectName("cardPopup")
        popup.setWindowOpacity(self._opacity)
        popup.setStyleSheet(f"QWidget#cardPopup {{ background-color: {P.bg_header}; }}")

        idx = len(self._pinned_cards)
        cx, cy = self._card_position(idx)
        popup.setGeometry(cx, cy, 430, 460)

        main_lay = QVBoxLayout(popup)
        main_lay.setContentsMargins(0, 0, 0, 0)
        main_lay.setSpacing(0)

        # Title bar
        bar = QWidget()
        bar.setObjectName("cardBar")
        bar.setFixedHeight(36)
        bar.setStyleSheet(f"QWidget#cardBar {{ background-color: {P.bg_header}; }}")
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(8, 0, 6, 0)
        bar_lay.setSpacing(4)

        # Drag support
        bar._drag_pos = QPoint()

        def drag_press(e):
            if e.button() == Qt.LeftButton:
                bar._drag_pos = e.globalPosition().toPoint() - popup.pos()
                e.accept()

        def drag_move(e):
            if e.buttons() & Qt.LeftButton:
                popup.move(e.globalPosition().toPoint() - bar._drag_pos)
                e.accept()

        bar.mousePressEvent = drag_press
        bar.mouseMoveEvent = drag_move

        title_lbl = QLabel(f"  {title}")
        title_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 10pt;
            font-weight: bold;
            color: {P.tool_mining};
            background: transparent;
        """)
        bar_lay.addWidget(title_lbl)

        sub_lbl = QLabel(subtitle)
        sub_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 7pt;
            color: {P.fg_dim};
            background: transparent;
        """)
        bar_lay.addWidget(sub_lbl)
        bar_lay.addStretch(1)

        lock_cb = QCheckBox("\u26b2")
        lock_cb.setStyleSheet(f"""
            QCheckBox {{
                font-family: Consolas;
                font-size: 13pt;
                color: {P.fg_dim};
                background: transparent;
                spacing: 0px;
            }}
            QCheckBox::indicator {{
                width: 0px;
                height: 0px;
            }}
            QCheckBox:checked {{
                color: {P.green};
            }}
        """)
        lock_cb.setCursor(Qt.PointingHandCursor)
        bar_lay.addWidget(lock_cb)

        close_btn = QPushButton("x")
        close_btn.setObjectName("cardClose")
        close_btn.setFixedSize(28, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton#cardClose {
                background: rgba(255, 60, 60, 0.15);
                color: #cc6666;
                border: none;
                font-family: Consolas;
                font-size: 13pt;
                font-weight: bold;
                border-radius: 3px;
                padding: 0px;
                margin: 2px;
                min-height: 0px;
            }
            QPushButton#cardClose:hover {
                background-color: rgba(220, 50, 50, 0.85);
                color: #ffffff;
            }
        """)
        close_btn.clicked.connect(lambda: self.close_card(key))
        bar_lay.addWidget(close_btn)

        main_lay.addWidget(bar)

        # Accent line
        accent_line = QFrame()
        accent_line.setFixedHeight(1)
        accent_line.setStyleSheet(f"background-color: {P.tool_mining};")
        main_lay.addWidget(accent_line)

        # Text content
        dt = QTextEdit()
        dt.setReadOnly(True)
        dt.setStyleSheet(f"""
            QTextEdit {{
                background-color: {P.bg_secondary};
                color: {P.fg};
                border: none;
                font-family: Consolas;
                font-size: 9pt;
                padding: 14px 10px;
                selection-background-color: {P.selection};
            }}
        """)
        main_lay.addWidget(dt, 1)

        popup.show()
        return popup, dt, lock_cb


# ── Card content rendering ────────────────────────────────────────────────────

def _fmt_pct(v: float) -> str:
    return f"{v:+.1f}%" if v != 0 else "0%"


def _pct_tag_good(v: float) -> str:
    return "positive" if v > 0 else ("negative" if v < 0 else "neutral")


def _pct_tag_bad(v: float) -> str:
    return "negative" if v > 0 else ("positive" if v < 0 else "neutral")


_TAG_COLORS = {
    "heading": P.tool_mining,
    "label": P.fg_dim,
    "value": P.fg_bright,
    "positive": P.green,
    "negative": P.red,
    "neutral": P.yellow,
    "divider": P.separator,
    "section": P.accent,
}


def _make_format(tag: str, bold: bool = False) -> QTextCharFormat:
    fmt = QTextCharFormat()
    color = _TAG_COLORS.get(tag, P.fg)
    fmt.setForeground(QColor(color))
    font = QFont("Consolas", 9)
    if bold or tag in ("heading", "value", "positive", "negative", "section"):
        font.setBold(True)
    if tag == "heading":
        font.setPointSize(10)
    fmt.setFont(font)
    return fmt


def _fill_item_card(dt: QTextEdit, kind: str, item: Any) -> None:
    """Fill a detail card text widget with item stats."""
    dt.clear()
    cursor = dt.textCursor()

    def w(text, tag=None):
        if tag:
            cursor.insertText(text, _make_format(tag))
        else:
            cursor.insertText(text)

    if kind == "laser":
        l = item
        w(f"\u25c8  {l.name}\n", "heading")
        w("\u2500" * 50 + "\n", "divider")
        w("  Company:        ", "label"); w(f"{l.company}\n", "value")
        w("  Size:           ", "label"); w(f"Size {l.size}\n", "value")
        w("\n")
        w("  POWER\n", "section")
        w("  Min Power:      ", "label"); w(f"{l.min_power:,.1f} aUEC\n", "value")
        w("  Max Power:      ", "label"); w(f"{l.max_power:,.1f} aUEC\n", "value")
        if l.ext_power:
            w("  Ext Power:      ", "label"); w(f"{l.ext_power:,.1f} aUEC\n", "value")
        w("\n")
        w("  RANGE\n", "section")
        if l.opt_range is not None:
            w("  Opt Range:      ", "label"); w(f"{l.opt_range:.0f} m\n", "value")
        if l.max_range is not None:
            w("  Max Range:      ", "label"); w(f"{l.max_range:.0f} m\n", "value")
        w("\n")
        w("  MODIFIERS\n", "section")
        if l.resistance is not None:
            v = l.resistance
            w("  Resistance:     ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if l.instability is not None:
            v = l.instability
            w("  Instability:    ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_bad(v))
        if l.inert is not None:
            v = l.inert
            w("  Inert Mat.:     ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_bad(v))
        if l.charge_window is not None:
            v = l.charge_window
            w("  Chrg Window:    ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if l.charge_rate is not None:
            v = l.charge_rate
            w("  Chrg Rate:      ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        w(f"\n  Module Slots:   ", "label"); w(f"{l.module_slots}\n", "value")
        w("\n")
        if l.price > 0:
            w("  Buy Price:      ", "label"); w(f"{l.price:,.0f} aUEC\n", "value")
        else:
            w("  Buy Price:      ", "label"); w("Stock / Free\n", "positive")

    elif kind == "module":
        m = item
        w(f"\u25c8  {m.name}\n", "heading")
        w("\u2500" * 50 + "\n", "divider")
        type_color = "neutral" if m.item_type.lower() == "active" else "value"
        w("  Type:           ", "label"); w(f"{m.item_type}\n", type_color)
        w("\n")
        w("  MODIFIERS\n", "section")
        if m.power_pct is not None:
            v = m.power_pct - 100.0
            color = _pct_tag_good(v) if v >= 0 else "negative"
            w("  Laser Power:    ", "label"); w(f"{_fmt_pct(v)}\n", color)
        if m.ext_power_pct is not None:
            v = m.ext_power_pct - 100.0
            color = _pct_tag_good(v) if v >= 0 else "negative"
            w("  Ext Power:      ", "label"); w(f"{_fmt_pct(v)}\n", color)
        if m.resistance is not None:
            v = m.resistance
            w("  Resistance:     ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if m.instability is not None:
            v = m.instability
            w("  Instability:    ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_bad(v))
        if m.inert is not None:
            v = m.inert
            w("  Inert Mat.:     ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_bad(v))
        if m.charge_window is not None:
            v = m.charge_window
            w("  Chrg Window:    ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if m.charge_rate is not None:
            v = m.charge_rate
            w("  Chrg Rate:      ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if m.overcharge is not None:
            v = m.overcharge
            w("  Overcharge:     ", "label"); w(f"{_fmt_pct(v)}\n", "negative")
        if m.shatter is not None:
            v = m.shatter
            w("  Shatter:        ", "label"); w(f"{_fmt_pct(v)}\n", "negative")
        if m.item_type.lower() == "active" and (m.uses or m.duration):
            w("\n")
            w("  ACTIVE USE\n", "section")
            if m.uses:
                w("  Uses:           ", "label"); w(f"{m.uses}\n", "value")
            if m.duration:
                w("  Duration:       ", "label"); w(f"{m.duration:.0f} s\n", "value")
        w("\n")
        if m.price > 0:
            w("  Buy Price:      ", "label"); w(f"{m.price:,.0f} aUEC\n", "value")

    elif kind == "gadget":
        g = item
        w(f"\u25c8  {g.name}\n", "heading")
        w("\u2500" * 50 + "\n", "divider")
        w("  Type:           ", "label"); w("Gadget\n", "value")
        w("\n")
        w("  MODIFIERS\n", "section")
        if g.resistance is not None:
            v = g.resistance
            w("  Resistance:     ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if g.instability is not None:
            v = g.instability
            w("  Instability:    ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_bad(v))
        if g.charge_window is not None:
            v = g.charge_window
            w("  Chrg Window:    ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if g.charge_rate is not None:
            v = g.charge_rate
            w("  Chrg Rate:      ", "label"); w(f"{_fmt_pct(v)}\n", _pct_tag_good(v))
        if g.cluster is not None:
            v = g.cluster
            w("  Cluster:        ", "label"); w(f"{_fmt_pct(v)}\n", "neutral")
        w("\n")
        if g.price > 0:
            w("  Buy Price:      ", "label"); w(f"{g.price:,.0f} aUEC\n", "value")

    dt.setTextCursor(cursor)
    dt.moveCursor(QTextCursor.Start)
