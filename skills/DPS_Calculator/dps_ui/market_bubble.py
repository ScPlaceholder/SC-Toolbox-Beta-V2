"""Market price bubble — queries UEX for buy/sell prices of a component."""
from __future__ import annotations

import datetime
import json
import logging
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from PySide6.QtCore import Qt, QPoint, Signal, QObject
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from dps_ui.constants import (
    ACCENT, BG, BG2, BG3, BORDER, FG, FG_DIM, GREEN, HEADER_BG, ORANGE, YELLOW,
)
from shared.qt.theme import P

log = logging.getLogger(__name__)

_UEX_BASE = "https://api.uexcorp.space/2.0"
_UEX_HEADERS = {"User-Agent": "DPS_Calculator/1.0", "Accept": "application/json"}
_MAX_BUBBLES = 5

# UEX category IDs for each Erkul item type
_TYPE_CATS: dict[str, list[int]] = {
    "WeaponGun":       [32, 35],   # Guns, Turrets
    "MissileLauncher": [33, 34],   # Missile Racks, Missiles
    "Shield":          [23],       # Shield Generators
    "Cooler":          [19],       # Coolers
    "PowerPlant":      [21],       # Power Plants
    "QuantumDrive":    [22],       # Quantum Drives
    "Radar":           [83],       # Radar
}
# Fallback: try all ship-relevant categories
_ALL_SHIP_CATS = [19, 21, 22, 23, 32, 33, 34, 35, 83]

# Class-level category item cache (shared across all bubbles in session)
_cat_cache: dict[int, list] = {}
_cat_cache_lock = threading.Lock()


def _norm(name: str) -> str:
    """Strip quotes, lowercase, collapse whitespace for fuzzy matching."""
    return re.sub(r"\s+", " ", re.sub(r"['\"]", "", name)).strip().lower()


def _name_matches(erkul: str, uex: str) -> bool:
    e, u = _norm(erkul), _norm(uex)
    return e in u or u in e


def _pin_qss(pinned: bool) -> str:
    c = ACCENT
    if pinned:
        return f"""
            QPushButton#mktPin {{
                background-color: rgba(51,221,136,80); color: {P.bg_primary};
                border: 1px solid {c}; border-radius: 3px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
                padding: 3px 10px; min-height: 0px;
            }}
            QPushButton#mktPin:hover {{ background-color: rgba(51,221,136,50); color: {c}; }}
        """
    return f"""
        QPushButton#mktPin {{
            background-color: rgba(51,221,136,30); color: {c};
            border: 1px solid rgba(51,221,136,60); border-radius: 3px;
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            padding: 3px 10px; min-height: 0px;
        }}
        QPushButton#mktPin:hover {{ background-color: rgba(51,221,136,60); color: {P.fg_bright}; }}
    """


class _FetchSignals(QObject):
    done = Signal(list)
    error = Signal(str)


class MarketBubble(QDialog):
    """Draggable, pinnable UEX market price popup.

    Opens as a non-modal, always-on-top window. Up to _MAX_BUBBLES are kept
    open simultaneously; the oldest unpinned bubble is evicted when the limit
    is reached. Pin keeps a bubble open regardless.
    """

    _open: list[MarketBubble] = []
    _pinned: list[MarketBubble] = []

    def __init__(self, parent: Optional[QWidget], item: dict):
        super().__init__(parent)
        self._item = item
        self._name = item.get("name", "Unknown")
        self._is_pinned = False
        self._drag_pos: Optional[QPoint] = None
        self._sig = _FetchSignals()
        self._sig.done.connect(self._on_prices)
        self._sig.error.connect(self._on_error)

        self.setWindowFlags(Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFixedSize(580, 360)

        self._evict_oldest()
        MarketBubble._open.append(self)

        if parent:
            pg = parent.geometry()
            self.move(max(0, pg.x() + 50), max(0, pg.y() + 80))

        self._build_ui()
        threading.Thread(target=self._fetch, daemon=True).start()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)

        frame = QWidget(self)
        frame.setStyleSheet("background-color: rgba(11,14,20,230);")
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.setSpacing(0)

        # ── Header ──
        hdr = QWidget(frame)
        hdr.setFixedHeight(34)
        hdr.setStyleSheet(f"background-color: {HEADER_BG};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 0, 4, 0)
        hl.setSpacing(8)

        title = QLabel(f"\U0001f6d2  {self._name.upper()}", hdr)
        title.setStyleSheet(f"""
            font-family: Electrolize, Consolas, monospace;
            font-size: 10pt; font-weight: bold;
            color: {ACCENT}; letter-spacing: 1px; background: transparent;
        """)
        hl.addWidget(title)
        hl.addStretch(1)

        self._pin_btn = QPushButton("Pin", hdr)
        self._pin_btn.setObjectName("mktPin")
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setStyleSheet(_pin_qss(False))
        self._pin_btn.clicked.connect(self._toggle_pin)
        hl.addWidget(self._pin_btn)

        close_btn = QPushButton("x", hdr)
        close_btn.setObjectName("mktClose")
        close_btn.setFixedSize(32, 28)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton#mktClose {
                background: rgba(255,60,60,0.15); color: #cc6666;
                border: none; border-radius: 3px;
                font-family: Consolas; font-size: 13pt; font-weight: bold;
                padding: 0px; margin: 2px; min-height: 0px;
            }
            QPushButton#mktClose:hover {
                background-color: rgba(220,50,50,0.85); color: #ffffff;
            }
        """)
        close_btn.clicked.connect(self.close)
        hl.addWidget(close_btn)
        fl.addWidget(hdr)

        # ── Status bar ──
        self._status = QLabel("  Searching UEX marketplace...", frame)
        self._status.setStyleSheet(f"""
            color: {FG_DIM}; font-family: Consolas; font-size: 9pt;
            background: {BG2}; padding: 5px 12px;
            border-bottom: 1px solid {BORDER};
        """)
        fl.addWidget(self._status)

        # ── Price table ──
        self._table = QTableWidget(0, 5, frame)
        self._table.setHorizontalHeaderLabels(
            ["Location", "Terminal", "Buy aUEC", "Sell aUEC", "Updated"]
        )
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SingleSelection)
        self._table.setShowGrid(False)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setStretchLastSection(True)
        self._table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setStyleSheet(f"""
            QTableWidget {{
                background-color: {BG};
                alternate-background-color: {BG3};
                color: {FG}; border: none;
                font-family: Consolas; font-size: 9pt;
                selection-background-color: #1e2840;
                selection-color: {FG}; outline: none;
            }}
            QTableWidget::item {{ padding: 3px 6px; }}
            QHeaderView::section {{
                background-color: {HEADER_BG}; color: {FG_DIM};
                border: none; border-bottom: 1px solid {BORDER};
                padding: 4px 6px;
                font-family: Consolas; font-size: 8pt; font-weight: bold;
            }}
        """)
        fl.addWidget(self._table, 1)
        outer.addWidget(frame)

    # ── Fetch (background thread) ─────────────────────────────────────────────

    def _fetch(self):
        item_type = self._item.get("type", "")
        cats = _TYPE_CATS.get(item_type, _ALL_SHIP_CATS)
        try:
            item_id = self._find_id_in_categories(cats)
            if item_id is None and cats is not _ALL_SHIP_CATS:
                # Widen search to all ship categories
                item_id = self._find_id_in_categories(_ALL_SHIP_CATS)
            if item_id is None:
                self._sig.error.emit(f"'{self._name}' not found in UEX marketplace.")
                return

            prices = self._fetch_prices(item_id)
            if prices:
                self._sig.done.emit(prices)
            else:
                self._sig.error.emit(f"No active listings for '{self._name}' in UEX.")

        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            self._sig.error.emit(f"Network error: {exc}")
        except (json.JSONDecodeError, ValueError) as exc:
            self._sig.error.emit(f"Parse error: {exc}")

    def _find_id_in_categories(self, cat_ids: list[int]) -> int | None:
        for cid in cat_ids:
            items = self._get_category(cid)
            for it in items:
                if _name_matches(self._name, it.get("name", "")):
                    log.debug(
                        "Matched '%s' → UEX '%s' (id=%s, cat=%s)",
                        self._name, it.get("name"), it.get("id"), cid,
                    )
                    return it.get("id")
        return None

    @staticmethod
    def _get_category(cid: int) -> list:
        """Fetch items for a UEX category, using class-level cache."""
        with _cat_cache_lock:
            if cid in _cat_cache:
                return _cat_cache[cid]
        url = f"{_UEX_BASE}/items?id_category={cid}"
        req = urllib.request.Request(url, headers=_UEX_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        items: list = []
        if isinstance(body, dict) and body.get("status") == "ok":
            data = body.get("data") or []
            items = data if isinstance(data, list) else []
        with _cat_cache_lock:
            _cat_cache[cid] = items
        return items

    @staticmethod
    def _fetch_prices(item_id: int) -> list:
        url = f"{_UEX_BASE}/items_prices?id_item={item_id}"
        req = urllib.request.Request(url, headers=_UEX_HEADERS)
        with urllib.request.urlopen(req, timeout=10) as r:
            body = json.loads(r.read().decode())
        if isinstance(body, dict) and body.get("status") == "ok":
            data = body.get("data") or []
            return data if isinstance(data, list) else []
        return []

    # ── Slots (Qt main thread) ────────────────────────────────────────────────

    def _on_prices(self, prices: list):
        self._table.setRowCount(0)
        for entry in prices:
            row = self._table.rowCount()
            self._table.insertRow(row)

            planet = entry.get("planet_name") or entry.get("star_system_name") or "—"
            city = entry.get("city_name") or ""
            location = f"{planet} › {city}" if city else planet
            terminal = entry.get("terminal_name") or "—"
            buy = entry.get("price_buy") or 0
            sell = entry.get("price_sell") or 0
            ts = entry.get("date_modified") or entry.get("date_added") or 0
            updated = (
                datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "—"
            )

            def _cell(text, color=FG, align=Qt.AlignLeft | Qt.AlignVCenter):
                c = QTableWidgetItem(str(text))
                c.setForeground(QColor(color))
                c.setTextAlignment(align)
                c.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                return c

            R = Qt.AlignRight | Qt.AlignVCenter
            self._table.setItem(row, 0, _cell(location))
            self._table.setItem(row, 1, _cell(terminal))
            self._table.setItem(row, 2, _cell(f"{buy:,.0f}" if buy else "—", GREEN, R))
            self._table.setItem(row, 3, _cell(f"{sell:,.0f}" if sell else "—", YELLOW, R))
            self._table.setItem(row, 4, _cell(updated, FG_DIM))

        self._status.setText(f"  {len(prices)} listing(s) — UEX marketplace")
        self._status.setStyleSheet(f"""
            color: {FG_DIM}; font-family: Consolas; font-size: 9pt;
            background: {BG2}; padding: 5px 12px;
            border-bottom: 1px solid {BORDER};
        """)

    def _on_error(self, msg: str):
        self._status.setText(f"  {msg}")
        self._status.setStyleSheet(f"""
            color: {ORANGE}; font-family: Consolas; font-size: 9pt;
            background: {BG2}; padding: 5px 12px;
            border-bottom: 1px solid {BORDER};
        """)

    # ── Pin ───────────────────────────────────────────────────────────────────

    def _toggle_pin(self):
        self._is_pinned = not self._is_pinned
        self._pin_btn.setText("Unpin" if self._is_pinned else "Pin")
        self._pin_btn.setStyleSheet(_pin_qss(self._is_pinned))
        if self._is_pinned:
            if self not in MarketBubble._pinned:
                MarketBubble._pinned.append(self)
        else:
            if self in MarketBubble._pinned:
                MarketBubble._pinned.remove(self)

    # ── Eviction ──────────────────────────────────────────────────────────────

    @classmethod
    def _evict_oldest(cls):
        cls._open = [d for d in cls._open if d.isVisible()]
        cls._pinned = [d for d in cls._pinned if d.isVisible()]
        while len(cls._open) >= _MAX_BUBBLES:
            victim = next((d for d in cls._open if not d._is_pinned), None)
            if victim is None:
                victim = cls._open[0]
            victim.close()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def closeEvent(self, event):
        if self in MarketBubble._open:
            MarketBubble._open.remove(self)
        if self in MarketBubble._pinned:
            MarketBubble._pinned.remove(self)
        super().closeEvent(event)

    # ── Paint (border + corner brackets) ─────────────────────────────────────

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, False)
        w, h = self.width(), self.height()
        edge = QColor(ACCENT)
        edge.setAlpha(100)
        p.setPen(QPen(edge, 1))
        p.drawRect(0, 0, w - 1, h - 1)
        bl = 12
        brk = QColor(ACCENT)
        brk.setAlpha(200)
        p.setPen(QPen(brk, 2))
        p.drawLine(0, 0, bl, 0);      p.drawLine(0, 0, 0, bl)
        p.drawLine(w-1, 0, w-1-bl, 0); p.drawLine(w-1, 0, w-1, bl)
        p.drawLine(0, h-1, bl, h-1);   p.drawLine(0, h-1, 0, h-1-bl)
        p.drawLine(w-1, h-1, w-1-bl, h-1); p.drawLine(w-1, h-1, w-1, h-1-bl)
        p.end()

    # ── Drag ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()

    def mouseMoveEvent(self, event):
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        self._drag_pos = None
        super().mouseReleaseEvent(event)
