"""Detail panel showing item or ship information with prices — PySide6 version."""

from __future__ import annotations

import logging
import threading
import webbrowser

import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QScrollArea, QSizePolicy,
)

from shared.qt.theme import P
from shared.qt.hud_widgets import HUDPanel

from ..config import (
    PRICE_DISPLAY_MAX, PURCHASE_DISPLAY_MAX, RENTAL_DISPLAY_MAX,
)
from ..service import DataService

log = logging.getLogger(__name__)


class _PriceSignal(QObject):
    """Thread-safe bridge for price data from background thread to main thread."""
    prices_ready = Signal(list, dict, int)   # prices, item, gen
    prices_error = Signal(str, int)          # error, gen


class DetailPanel(QWidget):
    """Right-hand panel displaying item details and market prices."""

    def __init__(self, parent: QWidget, data: DataService) -> None:
        super().__init__(parent)
        self.data = data
        self._price_gen: int = 0
        self._price_lock = threading.Lock()

        # Thread-safe signal for price data
        self._price_signal = _PriceSignal(self)
        self._price_signal.prices_ready.connect(self._render_prices)
        self._price_signal.prices_error.connect(self._render_price_error)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(f"QScrollArea {{ background-color: {P.bg_primary}; border: none; }}")
        layout.addWidget(self._scroll)

        self._inner = QWidget()
        self._inner.setStyleSheet(f"background-color: {P.bg_primary};")
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(0, 0, 0, 0)
        self._inner_layout.setSpacing(0)
        self._inner_layout.addStretch(1)
        self._scroll.setWidget(self._inner)

        self._show_placeholder()

    def _clear_inner(self) -> None:
        """Remove all widgets from inner layout."""
        while self._inner_layout.count():
            item = self._inner_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _scroll_to_top(self) -> None:
        self._scroll.verticalScrollBar().setValue(0)

    # -- Placeholder ---------------------------------------------------------

    def _show_placeholder(self) -> None:
        self._clear_inner()
        lbl = QLabel(_("Select an item to view details"))
        lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 10pt;
            color: {P.fg_dim};
            background: transparent;
            padding: 40px 10px;
        """)
        lbl.setWordWrap(True)
        lbl.setAlignment(Qt.AlignCenter)
        self._inner_layout.addWidget(lbl)
        self._inner_layout.addStretch(1)

    # -- Item detail ---------------------------------------------------------

    def show_item(self, item: dict) -> None:
        self._clear_inner()
        self._scroll_to_top()

        name_lbl = QLabel(item.get("name", "Unknown"))
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 12pt;
            font-weight: bold;
            color: {P.accent};
            background: transparent;
            padding: 10px 8px 2px 8px;
        """)
        self._inner_layout.addWidget(name_lbl)

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

        loading = QLabel(_("Loading prices..."))
        loading.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            color: {P.fg_dim};
            background: transparent;
            padding: 4px 8px;
        """)
        self._inner_layout.addWidget(loading)
        self._inner_layout.addStretch(1)

        item_id = item.get("id")
        threading.Thread(
            target=self._load_prices, args=(item_id, item), daemon=True,
        ).start()

    # -- Ship / vehicle detail -----------------------------------------------

    def show_ship(self, vehicle: dict) -> None:
        self._clear_inner()
        self._scroll_to_top()

        name = vehicle.get("name_full") or vehicle.get("name", "?")
        name_lbl = QLabel(name)
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 12pt;
            font-weight: bold;
            color: {P.accent};
            background: transparent;
            padding: 10px 8px 2px 8px;
        """)
        self._inner_layout.addWidget(name_lbl)

        mfr = vehicle.get("company_name", "")
        if mfr:
            mfr_lbl = QLabel(mfr)
            mfr_lbl.setStyleSheet(f"""
                font-family: Consolas;
                font-size: 9pt;
                color: {P.fg_dim};
                background: transparent;
                padding: 0 8px;
            """)
            self._inner_layout.addWidget(mfr_lbl)

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
            ("Length", f"{vehicle.get('length', '\u2014')} m"),
            ("Width", f"{vehicle.get('width', '\u2014')} m"),
            ("Height", f"{vehicle.get('height', '\u2014')} m"),
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
        for key, label, color in role_keys:
            if vehicle.get(key):
                tag = QLabel(label)
                tag.setStyleSheet(f"""
                    font-family: Consolas;
                    font-size: 7pt;
                    font-weight: bold;
                    color: {color};
                    background-color: {P.bg_card};
                    padding: 1px 4px;
                    border: 1px solid {P.border};
                """)
                roles_lay.addWidget(tag)
        roles_lay.addStretch(1)
        self._inner_layout.addWidget(roles_widget)

        # Purchase locations
        vid = vehicle.get("id")
        purchases = self.data.purchase_by_vehicle.get(vid, [])
        if purchases:
            self._add_separator()
            self._add_section_header(f"WHERE TO BUY ({len(purchases)})", P.accent)
            purchases_sorted = sorted(purchases, key=lambda p: p.get("price_buy", 0))
            for i, pur in enumerate(purchases_sorted[:PURCHASE_DISPLAY_MAX]):
                self._add_location_row(pur, "price_buy", P.green if i == 0 else P.accent, i, best=(i == 0))
        else:
            self._add_separator()
            lbl = QLabel("Not available for in-game purchase")
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent; padding: 4px 8px;")
            self._inner_layout.addWidget(lbl)

        # Rental info
        rentals = self.data.rental_by_vehicle.get(vid, [])
        if rentals:
            self._add_separator()
            self._add_section_header(f"RENTAL LOCATIONS ({len(rentals)})", P.green)
            rentals_sorted = sorted(rentals, key=lambda r: r.get("price_rent", 0))
            for i, rent in enumerate(rentals_sorted[:RENTAL_DISPLAY_MAX]):
                bg = P.bg_card if i % 2 == 0 else P.bg_input
                self._add_rental_row(rent, bg)

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
                    font-family: Consolas;
                    font-size: 8pt;
                    color: {P.accent};
                    background: transparent;
                    padding: 1px 8px;
                """)
                lnk.setCursor(Qt.PointingHandCursor)
                lnk.mousePressEvent = lambda _, u=url: webbrowser.open(u)
                self._inner_layout.addWidget(lnk)

        self._inner_layout.addStretch(1)

    # -- Price helpers -------------------------------------------------------

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

    def _load_prices(self, item_id: int, item: dict) -> None:
        with self._price_lock:
            self._price_gen += 1
            gen = self._price_gen

        result = self.data.fetch_item_prices(item_id)
        try:
            if result.ok:
                self._price_signal.prices_ready.emit(result.data, item, gen)
            else:
                self._price_signal.prices_error.emit(result.error, gen)
        except RuntimeError:
            pass  # widget destroyed

    def _render_price_error(self, error: str, gen: int) -> None:
        with self._price_lock:
            if self._price_gen != gen:
                return
        self._remove_loading_label()
        lbl = QLabel(f"Failed to load prices: {error}")
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            color: {P.red};
            background: transparent;
            padding: 4px 8px;
        """)
        self._inner_layout.insertWidget(self._inner_layout.count() - 1, lbl)

    def _render_prices(self, prices: list[dict], item: dict, gen: int) -> None:
        with self._price_lock:
            if self._price_gen != gen:
                return
        self._remove_loading_label()

        buy_prices = [p for p in prices if p.get("price_buy") and p["price_buy"] > 0]
        sell_prices = [p for p in prices if p.get("price_sell") and p["price_sell"] > 0]

        if not buy_prices and not sell_prices:
            lbl = QLabel("No market data available")
            lbl.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; background: transparent; padding: 4px 8px;")
            self._inner_layout.insertWidget(self._inner_layout.count() - 1, lbl)
            return

        insert_pos = self._inner_layout.count() - 1  # before stretch

        if buy_prices:
            buy_prices.sort(key=lambda p: p.get("price_buy", 0))
            self._insert_section_header("WHERE TO BUY", P.green, insert_pos)
            insert_pos += 1
            for i, p in enumerate(buy_prices[:PRICE_DISPLAY_MAX]):
                self._insert_price_row(p, "buy", i, insert_pos)
                insert_pos += 1

        if sell_prices:
            sell_prices.sort(key=lambda p: p.get("price_sell", 0), reverse=True)
            self._insert_section_header("WHERE TO SELL", P.orange, insert_pos)
            insert_pos += 1
            for i, p in enumerate(sell_prices[:PRICE_DISPLAY_MAX]):
                self._insert_price_row(p, "sell", i, insert_pos)
                insert_pos += 1

    # -- Shared UI helpers ---------------------------------------------------

    def _remove_loading_label(self) -> None:
        for i in range(self._inner_layout.count()):
            item = self._inner_layout.itemAt(i)
            if item and item.widget():
                w = item.widget()
                if isinstance(w, QLabel) and "Loading" in w.text():
                    w.deleteLater()
                    self._inner_layout.removeItem(item)
                    break

    def _add_separator(self) -> None:
        line = QFrame()
        line.setFixedHeight(1)
        line.setStyleSheet(f"background-color: {P.border};")
        idx = self._inner_layout.count() - 1 if self._inner_layout.count() > 0 else 0
        self._inner_layout.insertWidget(idx, line)

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
        v.setStyleSheet(f"font-family: Consolas; font-size: 9pt; color: {P.fg}; background: transparent;")
        row_lay.addWidget(v)
        row_lay.addStretch(1)

        idx = self._inner_layout.count() - 1 if self._inner_layout.count() > 0 else 0
        self._inner_layout.insertWidget(idx, row)

    def _add_section_header(self, text: str, color: str) -> None:
        hdr = QWidget()
        hdr.setStyleSheet(f"background-color: {P.bg_secondary};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(6, 3, 6, 3)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {color};
            background: transparent;
        """)
        hdr_lay.addWidget(lbl)
        idx = self._inner_layout.count() - 1 if self._inner_layout.count() > 0 else 0
        self._inner_layout.insertWidget(idx, hdr)

    def _insert_section_header(self, text: str, color: str, pos: int) -> None:
        hdr = QWidget()
        hdr.setStyleSheet(f"background-color: {P.bg_secondary};")
        hdr_lay = QHBoxLayout(hdr)
        hdr_lay.setContentsMargins(6, 10, 6, 2)
        lbl = QLabel(text)
        lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {color};
            background: transparent;
        """)
        hdr_lay.addWidget(lbl)
        self._inner_layout.insertWidget(pos, hdr)

    def _insert_price_row(self, price_data: dict, mode: str, idx: int, pos: int) -> None:
        bg = P.bg_card if idx % 2 == 0 else P.bg_input
        row = QWidget()
        row.setStyleSheet(f"background-color: {bg};")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 2, 4, 2)
        row_lay.setSpacing(4)

        tname = price_data.get("terminal_name", "Unknown")
        loc_parts: list[str] = []
        for field in ("star_system_name", "planet_name", "moon_name", "city_name", "space_station_name"):
            val = price_data.get(field)
            if val:
                loc_parts.append(val)
        loc = " > ".join(loc_parts)

        if mode == "buy":
            price_val = price_data.get("price_buy", 0)
            color = P.green
        else:
            price_val = price_data.get("price_sell", 0)
            color = P.orange
        price_str = f"{price_val:,.0f}" if price_val else "\u2014"

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
            font-family: Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {color};
            background: transparent;
            padding-right: 6px;
        """)
        p_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_lay.addWidget(p_lbl)

        self._inner_layout.insertWidget(pos, row)

    def _add_location_row(self, data: dict, price_key: str, color: str, idx: int, best: bool = False) -> None:
        bg = P.bg_card if idx % 2 == 0 else P.bg_input
        row = QWidget()
        row.setStyleSheet(f"background-color: {bg};")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 2, 4, 2)
        row_lay.setSpacing(4)

        tname = data.get("terminal_name", "?")
        price = data.get(price_key)
        price_str = f"{price:,.0f} aUEC" if price else "\u2014"

        tid = data.get("id_terminal")
        loc_parts: list[str] = []
        if tid and tid in self.data.terminals:
            term = self.data.terminals[tid]
            for fld in ("star_system_name", "planet_name", "city_name", "space_station_name"):
                v = term.get(fld)
                if v:
                    loc_parts.append(v)
        loc = " > ".join(loc_parts)

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

        best_str = " BEST" if best else ""
        p_lbl = QLabel(price_str + best_str)
        p_lbl.setMinimumWidth(120)
        p_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {color};
            background: transparent;
            padding-right: 6px;
        """)
        p_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_lay.addWidget(p_lbl)

        pos = self._inner_layout.count() - 1 if self._inner_layout.count() > 0 else 0
        self._inner_layout.insertWidget(pos, row)

    def _add_rental_row(self, rent: dict, bg: str) -> None:
        row = QWidget()
        row.setStyleSheet(f"background-color: {bg};")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(4, 2, 4, 2)
        row_lay.setSpacing(4)

        tname = rent.get("terminal_name", "?")
        price = rent.get("price_rent")
        price_str = f"{price:,.0f} aUEC/day" if price else "\u2014"

        tid = rent.get("id_terminal")
        loc_parts: list[str] = []
        if tid and tid in self.data.terminals:
            term = self.data.terminals[tid]
            for fld in ("star_system_name", "planet_name", "city_name"):
                v = term.get(fld)
                if v:
                    loc_parts.append(v)
        loc = " > ".join(loc_parts)

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
        p_lbl.setMinimumWidth(120)
        p_lbl.setStyleSheet(f"""
            font-family: Consolas;
            font-size: 8pt;
            font-weight: bold;
            color: {P.green};
            background: transparent;
        """)
        p_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        row_lay.addWidget(p_lbl)

        pos = self._inner_layout.count() - 1 if self._inner_layout.count() > 0 else 0
        self._inner_layout.insertWidget(pos, row)
