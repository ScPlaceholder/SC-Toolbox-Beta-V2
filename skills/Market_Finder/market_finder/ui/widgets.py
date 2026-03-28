"""Reusable UI widgets: SearchBubble, ItemDetailBubble — PySide6 version."""

from __future__ import annotations

import threading
import webbrowser
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal, QPoint, QObject
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame, QScrollArea,
    QSizePolicy,
)

import shared.path_setup  # noqa: E402  # centralised path config
from shared.qt.theme import P

from ..config import (
    CAT_COLORS, SEARCH_BUBBLE_MAX, SEARCH_BUBBLE_PER_TAB,
    PRICE_DISPLAY_MAX, PURCHASE_DISPLAY_MAX, RENTAL_DISPLAY_MAX,
    item_tab,
)


class SearchBubble(QWidget):
    """Popup displaying search results grouped by category tab."""

    item_selected = Signal(dict)

    def __init__(
        self,
        parent: QWidget,
        items: list[dict],
        on_select: Callable[[dict], None],
    ) -> None:
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setWindowFlags(Qt.Popup | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self._on_select = on_select

        self.setStyleSheet(f"""
            QWidget {{
                background-color: {P.bg_secondary};
                border: 1px solid {P.border};
            }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(0)

        groups: dict[str, list[dict]] = {}
        for item in items[:SEARCH_BUBBLE_MAX]:
            tab = item_tab(item)
            groups.setdefault(tab, []).append(item)

        for tab_name, tab_items in groups.items():
            color = CAT_COLORS.get(tab_name, P.fg_dim)
            hdr = QLabel(tab_name.upper())
            hdr.setStyleSheet(f"""
                font-family: Consolas;
                font-size: 8pt;
                font-weight: bold;
                color: {color};
                background: transparent;
                padding: 4px 6px 1px 6px;
            """)
            layout.addWidget(hdr)

            for it in tab_items[:SEARCH_BUBBLE_PER_TAB]:
                lbl = _ClickableLabel(
                    f"  {it.get('name', '')}  \u2014  {it.get('category', '')}",
                    it,
                )
                lbl.setStyleSheet(f"""
                    QLabel {{
                        font-family: Consolas;
                        font-size: 9pt;
                        color: {P.fg};
                        background: transparent;
                        padding: 1px 6px;
                    }}
                    QLabel:hover {{
                        background-color: {P.selection};
                    }}
                """)
                lbl.setCursor(Qt.PointingHandCursor)
                lbl.clicked.connect(lambda item=it: self._select(item))
                layout.addWidget(lbl)

    def _select(self, item: dict) -> None:
        self._on_select(item)
        self.close()

    def position_below(self, widget: QWidget) -> None:
        """Position this popup directly below *widget*."""
        pos = widget.mapToGlobal(widget.rect().bottomLeft())
        w = max(widget.width(), 400)
        self.setFixedWidth(w)
        self.adjustSize()
        h = min(self.sizeHint().height(), 500)
        self.setGeometry(pos.x(), pos.y(), w, h)
        self.show()


class _ClickableLabel(QLabel):
    """QLabel that emits clicked signal on mouse press."""
    clicked = Signal()

    def __init__(self, text: str, item: dict, parent=None):
        super().__init__(text, parent)
        self._item = item

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Thread-safe price signal for bubbles
# ---------------------------------------------------------------------------

class _BubblePriceSignal(QObject):
    prices_ready = Signal(list, dict, int)   # prices, item, gen
    prices_error = Signal(str, int)          # error, gen


# ---------------------------------------------------------------------------
# Draggable / pinnable detail bubble
# ---------------------------------------------------------------------------

class ItemDetailBubble(QWidget):
    """Floating, draggable, pinnable window showing full item/ship details.

    Created on double-click of a table row.  Pinned bubbles stay open;
    unpinned ones close when the user clicks outside.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WA_DeleteOnClose)
        self._pinned = False
        self._drag_pos: QPoint | None = None
        self._price_gen = 0
        self._price_lock = threading.Lock()
        self._price_signal = _BubblePriceSignal(self)
        self._price_signal.prices_ready.connect(self._render_item_prices)
        self._price_signal.prices_error.connect(self._render_price_error)

        self.setMinimumSize(320, 200)
        self.setMaximumSize(500, 700)

        self.setStyleSheet(f"""
            ItemDetailBubble {{
                background-color: {P.bg_secondary};
                border: 2px solid {P.accent};
                border-radius: 6px;
            }}
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Title bar
        self._title_bar = QWidget()
        self._title_bar.setFixedHeight(26)
        self._title_bar.setStyleSheet(f"""
            background-color: {P.bg_header};
            border-top-left-radius: 6px;
            border-top-right-radius: 6px;
        """)
        tb_lay = QHBoxLayout(self._title_bar)
        tb_lay.setContentsMargins(8, 2, 4, 2)
        tb_lay.setSpacing(4)

        self._title_lbl = QLabel("")
        self._title_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {P.accent};
            background: transparent;
        """)
        tb_lay.addWidget(self._title_lbl, 1)

        self._pin_btn = QLabel("PIN")
        self._pin_btn.setAlignment(Qt.AlignCenter)
        self._pin_btn.setCursor(Qt.PointingHandCursor)
        self._pin_btn.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 7pt;
            font-weight: bold;
            color: {P.fg_dim};
            background: transparent;
            padding: 1px 4px;
        """)
        self._pin_btn.mousePressEvent = lambda _: self._toggle_pin()
        tb_lay.addWidget(self._pin_btn)

        close_btn = QLabel("\u2715")
        close_btn.setFixedSize(20, 20)
        close_btn.setAlignment(Qt.AlignCenter)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            font-size: 10pt;
            color: {P.fg_dim};
            background: transparent;
        """)
        close_btn.mousePressEvent = lambda _: self.close()
        tb_lay.addWidget(close_btn)

        outer.addWidget(self._title_bar)

        # Scroll area for content
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet(f"""
            QScrollArea {{
                background-color: {P.bg_secondary};
                border: none;
            }}
        """)
        outer.addWidget(scroll, 1)

        self._content = QWidget()
        self._content.setStyleSheet(f"background-color: {P.bg_secondary};")
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(0)
        self._content_layout.addStretch(1)
        scroll.setWidget(self._content)

    # -- Pin / drag ----------------------------------------------------------

    def _toggle_pin(self) -> None:
        self._pinned = not self._pinned
        if self._pinned:
            self._pin_btn.setText("UNPIN")
            self._pin_btn.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt; font-weight: bold;
                color: {P.accent}; background: transparent; padding: 1px 4px;
            """)
        else:
            self._pin_btn.setText("PIN")
            self._pin_btn.setStyleSheet(f"""
                font-family: Consolas; font-size: 7pt; font-weight: bold;
                color: {P.fg_dim}; background: transparent; padding: 1px 4px;
            """)

    @property
    def is_pinned(self) -> bool:
        return self._pinned

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_pos = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._drag_pos = None

    # -- Content helpers (mirror DetailPanel) --------------------------------

    def _clear_content(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._content_layout.addStretch(1)

    def _add_separator(self) -> None:
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background-color: {P.border};")
        self._content_layout.insertWidget(self._content_layout.count() - 1, line)

    def _add_kv_row(self, label: str, value: str) -> None:
        row = QWidget()
        row.setStyleSheet("background: transparent;")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(8, 1, 8, 1)
        row_lay.setSpacing(4)

        k = QLabel(f"{label}:")
        k.setFixedWidth(100)
        k.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent;")
        row_lay.addWidget(k)

        v = QLabel(value)
        v.setWordWrap(True)
        v.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg}; background: transparent;")
        row_lay.addWidget(v)
        row_lay.addStretch(1)
        self._content_layout.insertWidget(self._content_layout.count() - 1, row)

    def _add_section_header(self, text: str, color: str) -> None:
        hdr = QWidget()
        hdr.setStyleSheet(f"background-color: {P.bg_primary};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(6, 6, 6, 2)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt; font-weight: bold;
            color: {color}; background: transparent;
        """)
        hdr_lay.addWidget(lbl)
        self._content_layout.insertWidget(self._content_layout.count() - 1, hdr)

    def _add_price_row(self, tname: str, loc: str, price_str: str, color: str, idx: int) -> None:
        bg = P.bg_card if idx % 2 == 0 else P.bg_input
        row = QWidget()
        row.setStyleSheet(f"background-color: {bg};")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 2, 4, 2)
        row_lay.setSpacing(4)

        left = QWidget()
        left.setStyleSheet(f"background-color: {bg};")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(4, 0, 0, 0)
        left_lay.setSpacing(0)

        t_lbl = QLabel(tname)
        t_lbl.setWordWrap(True)
        t_lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg}; background: transparent;")
        left_lay.addWidget(t_lbl)
        if loc:
            l_lbl = QLabel(loc)
            l_lbl.setWordWrap(True)
            l_lbl.setStyleSheet(f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim}; background: transparent;")
            left_lay.addWidget(l_lbl)
        row_lay.addWidget(left, 1)

        p_lbl = QLabel(price_str)
        p_lbl.setMinimumWidth(90)
        p_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt; font-weight: bold;
            color: {color}; background: transparent; padding-right: 6px;
        """)
        p_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_lay.addWidget(p_lbl)

        self._content_layout.insertWidget(self._content_layout.count() - 1, row)

    @staticmethod
    def _fmt(val: object) -> str:
        if not val:
            return "\u2014"
        try:
            n = float(val)
            if n >= 1_000_000:
                return f"{n / 1_000_000:.1f}M"
            if n >= 1000:
                return f"{n:,.0f}"
            return f"{n:.0f}"
        except (ValueError, TypeError):
            return str(val)

    # -- Show item -----------------------------------------------------------

    def show_item(self, item: dict, data_service) -> None:
        """Populate bubble with item details and load prices asynchronously."""
        self._clear_content()
        self._data = data_service

        name = item.get("name", "Unknown")
        self._title_lbl.setText(name)

        meta = [
            ("Category", item.get("category", "\u2014")),
            ("Section", item.get("section", "\u2014")),
            ("Manufacturer", item.get("company_name", "\u2014")),
            ("Size", item.get("size", "\u2014")),
        ]
        for label, val in meta:
            if val and val != "\u2014":
                self._add_kv_row(label, str(val))

        self._add_separator()

        loading = QLabel("Loading prices...")
        loading.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt;
            color: {P.fg_dim}; background: transparent; padding: 4px 8px;
        """)
        self._content_layout.insertWidget(self._content_layout.count() - 1, loading)

        item_id = item.get("id")
        threading.Thread(
            target=self._load_prices, args=(item_id, item), daemon=True,
        ).start()

        self.adjustSize()
        self.show()

    def _load_prices(self, item_id: int, item: dict) -> None:
        import time
        with self._price_lock:
            self._price_gen += 1
            gen = self._price_gen
        # Retry a few times if another thread is already fetching this item
        for attempt in range(6):
            result = self._data.fetch_item_prices(item_id)
            if result.ok or (not result.ok and result.error_type != "in_progress"):
                break
            time.sleep(0.5)
        try:
            if result.ok:
                self._price_signal.prices_ready.emit(result.data, item, gen)
            else:
                self._price_signal.prices_error.emit(result.error, gen)
        except RuntimeError:
            pass

    def _render_price_error(self, error: str, gen: int) -> None:
        with self._price_lock:
            if self._price_gen != gen:
                return
        self._remove_loading_label()
        lbl = QLabel(f"Failed to load prices: {error}")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.red}; background: transparent; padding: 4px 8px;")
        self._content_layout.insertWidget(self._content_layout.count() - 1, lbl)

    def _render_item_prices(self, prices: list[dict], item: dict, gen: int) -> None:
        with self._price_lock:
            if self._price_gen != gen:
                return
        self._remove_loading_label()

        buy_prices = [p for p in prices if p.get("price_buy") and p["price_buy"] > 0]
        sell_prices = [p for p in prices if p.get("price_sell") and p["price_sell"] > 0]

        if not buy_prices and not sell_prices:
            lbl = QLabel("No market data available")
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent; padding: 4px 8px;")
            self._content_layout.insertWidget(self._content_layout.count() - 1, lbl)
            return

        if buy_prices:
            buy_prices.sort(key=lambda p: p.get("price_buy", 0))
            self._add_section_header("WHERE TO BUY", P.green)
            for i, p in enumerate(buy_prices[:PRICE_DISPLAY_MAX]):
                tname = p.get("terminal_name", "Unknown")
                loc_parts = []
                for field in ("star_system_name", "planet_name", "moon_name", "city_name", "space_station_name"):
                    val = p.get(field)
                    if val:
                        loc_parts.append(val)
                loc = " > ".join(loc_parts)
                price_val = p.get("price_buy", 0)
                price_str = f"{price_val:,.0f}" if price_val else "\u2014"
                self._add_price_row(tname, loc, price_str, P.green, i)

        if sell_prices:
            sell_prices.sort(key=lambda p: p.get("price_sell", 0), reverse=True)
            self._add_section_header("WHERE TO SELL", P.orange)
            for i, p in enumerate(sell_prices[:PRICE_DISPLAY_MAX]):
                tname = p.get("terminal_name", "Unknown")
                loc_parts = []
                for field in ("star_system_name", "planet_name", "moon_name", "city_name", "space_station_name"):
                    val = p.get(field)
                    if val:
                        loc_parts.append(val)
                loc = " > ".join(loc_parts)
                price_val = p.get("price_sell", 0)
                price_str = f"{price_val:,.0f}" if price_val else "\u2014"
                self._add_price_row(tname, loc, price_str, P.orange, i)

    def _remove_loading_label(self) -> None:
        for i in range(self._content_layout.count()):
            item = self._content_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if isinstance(w, QLabel) and "Loading" in w.text():
                    w.deleteLater()
                    self._content_layout.removeItem(item)
                    break

    # -- Show ship -----------------------------------------------------------

    def show_ship(self, vehicle: dict, data_service) -> None:
        """Populate bubble with ship/vehicle details."""
        self._clear_content()
        self._data = data_service

        name = vehicle.get("name_full") or vehicle.get("name", "?")
        self._title_lbl.setText(name)

        mfr = vehicle.get("company_name", "")
        if mfr:
            self._add_kv_row("Manufacturer", mfr)

        self._add_separator()

        icon = "\U0001f680" if vehicle.get("is_spaceship") else "\U0001f699"
        vtype = "Spaceship" if vehicle.get("is_spaceship") else "Ground Vehicle"
        specs = [
            ("Type", f"{icon} {vtype}"),
            ("Pad Size", vehicle.get("pad_type", "\u2014")),
            ("Crew", vehicle.get("crew", "\u2014")),
            ("Cargo (SCU)", vehicle.get("scu", "\u2014")),
            ("H2 Fuel", self._fmt(vehicle.get("fuel_hydrogen"))),
            ("QT Fuel", self._fmt(vehicle.get("fuel_quantum"))),
            ("Mass", f"{self._fmt(vehicle.get('mass'))} kg"),
            ("Length", f"{vehicle.get('length', chr(0x2014))} m"),
            ("Width", f"{vehicle.get('width', chr(0x2014))} m"),
            ("Height", f"{vehicle.get('height', chr(0x2014))} m"),
        ]
        for label, val in specs:
            if val and val != "\u2014" and val != "\u2014 kg" and val != "\u2014 m":
                self._add_kv_row(label, str(val))

        # Role tags
        self._add_separator()
        roles_widget = QWidget()
        roles_widget.setStyleSheet("background: transparent;")
        roles_lay = QHBoxLayout(roles_widget)
        roles_lay.setContentsMargins(8, 2, 8, 2)
        roles_lay.setSpacing(4)

        role_keys = [
            ("is_cargo", "Cargo", P.energy_cyan), ("is_mining", "Mining", P.yellow),
            ("is_salvage", "Salvage", P.orange), ("is_medical", "Medical", P.green),
            ("is_exploration", "Explorer", P.accent), ("is_military", "Military", P.red),
            ("is_racing", "Racing", P.yellow), ("is_stealth", "Stealth", P.fg_dim),
            ("is_passenger", "Passenger", P.green), ("is_refuel", "Refuel", P.energy_cyan),
            ("is_repair", "Repair", P.orange), ("is_bomber", "Bomber", P.red),
            ("is_carrier", "Carrier", P.accent), ("is_starter", "Starter", P.green),
        ]
        has_roles = False
        for key, label, color in role_keys:
            if vehicle.get(key):
                has_roles = True
                tag = QLabel(label)
                tag.setStyleSheet(f"""
                    font-family: Consolas; font-size: 7pt; font-weight: bold;
                    color: {color}; background-color: {P.bg_card};
                    padding: 1px 4px; border: 1px solid {P.border};
                """)
                roles_lay.addWidget(tag)
        roles_lay.addStretch(1)
        if has_roles:
            self._content_layout.insertWidget(self._content_layout.count() - 1, roles_widget)
        else:
            roles_widget.deleteLater()

        # Purchase locations
        vid = vehicle.get("id")
        purchases = data_service.purchase_by_vehicle.get(vid, [])
        if purchases:
            self._add_separator()
            self._add_section_header(f"WHERE TO BUY ({len(purchases)})", P.accent)
            purchases_sorted = sorted(purchases, key=lambda p: p.get("price_buy", 0))
            for i, pur in enumerate(purchases_sorted[:PURCHASE_DISPLAY_MAX]):
                tname = pur.get("terminal_name", "?")
                price = pur.get("price_buy")
                price_str = f"{price:,.0f} aUEC" if price else "\u2014"
                best_str = " BEST" if i == 0 else ""
                color = P.green if i == 0 else P.accent

                tid = pur.get("id_terminal")
                loc_parts: list[str] = []
                if tid and tid in data_service.terminals:
                    term = data_service.terminals[tid]
                    for fld in ("star_system_name", "planet_name", "city_name", "space_station_name"):
                        v = term.get(fld)
                        if v:
                            loc_parts.append(v)
                loc = " > ".join(loc_parts)
                self._add_price_row(tname, loc, price_str + best_str, color, i)
        else:
            self._add_separator()
            lbl = QLabel("Not available for in-game purchase")
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent; padding: 4px 8px;")
            self._content_layout.insertWidget(self._content_layout.count() - 1, lbl)

        # Rental info
        rentals = data_service.rental_by_vehicle.get(vid, [])
        if rentals:
            self._add_separator()
            self._add_section_header(f"RENTAL LOCATIONS ({len(rentals)})", P.green)
            rentals_sorted = sorted(rentals, key=lambda r: r.get("price_rent", 0))
            for i, rent in enumerate(rentals_sorted[:RENTAL_DISPLAY_MAX]):
                tname = rent.get("terminal_name", "?")
                price = rent.get("price_rent")
                price_str = f"{price:,.0f} aUEC/day" if price else "\u2014"

                tid = rent.get("id_terminal")
                loc_parts = []
                if tid and tid in data_service.terminals:
                    term = data_service.terminals[tid]
                    for fld in ("star_system_name", "planet_name", "city_name"):
                        v = term.get(fld)
                        if v:
                            loc_parts.append(v)
                loc = " > ".join(loc_parts)
                self._add_price_row(tname, loc, price_str, P.green, i)

        # Store links
        urls: list[tuple[str, str]] = []
        if vehicle.get("url_store"):
            urls.append(("RSI Store", vehicle["url_store"]))
        if vehicle.get("url_brochure"):
            urls.append(("Brochure", vehicle["url_brochure"]))
        if urls:
            self._add_separator()
            for label, url in urls:
                lnk = QLabel(f"\U0001f517 {label}")
                lnk.setStyleSheet(f"""
                    font-family: Consolas; font-size: 8pt;
                    color: {P.accent}; background: transparent; padding: 1px 8px;
                """)
                lnk.setCursor(Qt.PointingHandCursor)
                lnk.mousePressEvent = lambda _, u=url: webbrowser.open(u)
                self._content_layout.insertWidget(self._content_layout.count() - 1, lnk)

        self.adjustSize()
        self.show()

    # -- Position near cursor ------------------------------------------------

    def show_near_cursor(self, global_pos: QPoint) -> None:
        """Position the bubble near the given global position."""
        self.move(global_pos.x() + 20, global_pos.y() - 40)
        self.show()
