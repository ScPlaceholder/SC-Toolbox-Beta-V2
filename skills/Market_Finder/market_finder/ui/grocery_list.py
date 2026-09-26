"""Grocery List bubble — a drag-and-drop shopping list for Market Finder.

The user opens this floating, draggable bubble from the title bar, then
drags items out of the item table onto it.  Each dropped item becomes a
card listing where it can be bought and for how much (cheapest first), so
the player can see at a glance where to shop.

Each card's buy locations collapse to a single best-price row and expand
back to the full list via a per-item toggle (and by clicking any row).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, Signal, QObject, QPoint, QTimer
from PySide6.QtWidgets import (
    QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QCheckBox, QSpinBox, QSizePolicy,
)

import shared.path_setup  # noqa: E402  # centralised path config
from shared.i18n import s_ as _
from shared.qt.theme import P
from shared.qt.data_table import SC_ITEM_MIME

from ..config import GROCERY_BUY_DISPLAY_MAX
from ..grocery import buy_locations, format_price


# ── Exact tooltip strings required by the toggle affordance ──
# (wrapped with _() so they remain translation-ready; English falls back
#  to the source text).
def _tooltip_hide() -> str:
    return _("Hide Other Locations/Prices")


def _tooltip_show() -> str:
    return _("Show Other Locations/Prices")


# ---------------------------------------------------------------------------
# Route option: "Shorter trip over cheapest price"
# ---------------------------------------------------------------------------

#: Key of the route option in Item Finder's saved state
#: (``~/.sctoolbox/market_finder/starmap_state.json``, shared with the Star
#: Map's view state; each writer read-modify-writes only its own key).
ROUTE_STATE_KEY = "grocery_route"


def load_route_options() -> dict:
    """The saved route option: ``{"shorter": bool, "premium_pct": int}``.

    Off, at :data:`~market_finder.route_planner.PREMIUM_PCT_DEFAULT` %, on
    first run or when the saved value is unusable.
    """
    from ..route_planner import PREMIUM_PCT_DEFAULT, PREMIUM_PCT_MAX
    from ..starmap import data as sm_data
    saved = sm_data.load_state().get(ROUTE_STATE_KEY)
    saved = saved if isinstance(saved, dict) else {}
    try:
        pct = int(saved.get("premium_pct", PREMIUM_PCT_DEFAULT))
    except (TypeError, ValueError):
        pct = PREMIUM_PCT_DEFAULT
    return {"shorter": bool(saved.get("shorter", False)),
            "premium_pct": max(0, min(PREMIUM_PCT_MAX, pct))}


def save_route_options(shorter: bool, premium_pct: int) -> None:
    from ..starmap import data as sm_data
    st = sm_data.load_state()
    st[ROUTE_STATE_KEY] = {"shorter": bool(shorter), "premium_pct": int(premium_pct)}
    sm_data.save_state(st)


def route_summary_text(totals: dict, cheapest: "Optional[dict]" = None) -> str:
    """One short line: the route's total aUEC and travel, plus -- when the
    shorter-trip option planned it -- the difference to the cheapest-price
    route, e.g. ``15,120 aUEC · 1,877 Gm  (+1,240 aUEC, -38 Gm)``."""
    gm = totals.get("gm")
    text = "{auec} aUEC · {gm} Gm".format(
        auec=f"{int(round(totals.get('auec') or 0)):,}",
        gm=f"{int(round(gm)):,}" if gm is not None else "?")
    if cheapest is None:
        return text
    d_auec = int(round((totals.get("auec") or 0) - (cheapest.get("auec") or 0)))
    base_gm = cheapest.get("gm")
    d_gm = (int(round(gm - base_gm)) if gm is not None and base_gm is not None else None)
    if d_auec == 0 and d_gm == 0:
        return text + "  " + _("(cheapest is shortest)")
    return text + "  ({a} aUEC, {g} Gm)".format(
        a=f"{d_auec:+,}", g=f"{d_gm:+,}" if d_gm is not None else "?")


# ---------------------------------------------------------------------------
# Thread-safe price delivery for a single card
# ---------------------------------------------------------------------------

class _CardSignal(QObject):
    ready = Signal(list)   # normalized price payload
    error = Signal(str)


class _RouteSignal(QObject):
    """Delivers background telemetry-fetch progress and the planned route."""
    progress = Signal(int, int)   # fetched, total
    done = Signal(object)         # route_planner.plan_shopping() result



# ---------------------------------------------------------------------------
# Clickable row / toggle
# ---------------------------------------------------------------------------

class _ClickRow(QWidget):
    """A row that runs a callback when left-clicked."""

    def __init__(self, on_click: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self._on_click:
            self._on_click()
            event.accept()
            return
        super().mousePressEvent(event)


# ---------------------------------------------------------------------------
# Item card
# ---------------------------------------------------------------------------

class GroceryItemCard(QFrame):
    """One grocery item: its name and a collapsible list of buy locations."""

    def __init__(
        self,
        item: dict,
        data_service,
        on_remove: Callable[["GroceryItemCard"], None],
        parent: QWidget | None = None,
        on_loaded: Callable[["GroceryItemCard"], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._item = item
        self._data = data_service
        self._on_remove = on_remove
        self._on_loaded = on_loaded
        self._expanded: bool = True          # default: show all locations
        self._buy_rows: list[dict] = []
        self._loaded: bool = False

        self._apply_border(P.border)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ── Header: name + category + remove ──
        header = QWidget()
        header.setStyleSheet(f"""
            background-color: {P.bg_header};
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        """)
        h_lay = QHBoxLayout(header)
        h_lay.setContentsMargins(8, 4, 6, 4)
        h_lay.setSpacing(6)

        name = item.get("name") or item.get("name_full") or _("Unknown")
        name_lbl = QLabel(str(name))
        name_lbl.setWordWrap(True)
        name_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {P.tool_market};
            background: transparent;
        """)
        h_lay.addWidget(name_lbl, 1)

        category = item.get("category")
        if category:
            cat_lbl = QLabel(str(category))
            cat_lbl.setStyleSheet(
                f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim}; background: transparent;"
            )
            h_lay.addWidget(cat_lbl)

        remove = QLabel("✕")
        remove.setToolTip(_("Remove from grocery list"))
        remove.setCursor(Qt.PointingHandCursor)
        remove.setFixedSize(18, 18)
        remove.setAlignment(Qt.AlignCenter)
        remove.setStyleSheet(
            f"font-size: 10pt; color: {P.fg_dim}; background: transparent;"
        )
        remove.mousePressEvent = lambda _e: self._remove()
        h_lay.addWidget(remove)

        outer.addWidget(header)

        # ── Rows container (locations / prices) ──
        self._rows_box = QWidget()
        self._rows_box.setStyleSheet("background: transparent;")
        self._rows_layout = QVBoxLayout(self._rows_box)
        self._rows_layout.setContentsMargins(0, 0, 0, 4)
        self._rows_layout.setSpacing(0)
        outer.addWidget(self._rows_box)

        self._status = QLabel(_("Loading prices..."))
        self._status.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; "
            f"background: transparent; padding: 4px 8px;"
        )
        self._rows_layout.addWidget(self._status)

        # ── Background price fetch ──
        self._signal = _CardSignal(self)
        self._signal.ready.connect(self._on_prices_ready)
        self._signal.error.connect(self._on_prices_error)
        threading.Thread(target=self._load, daemon=True).start()

    # -- public ----------------------------------------------------------

    def item_id(self):
        return self._item.get("id")

    def flash(self) -> None:
        """Briefly highlight the card (used when a duplicate is dropped)."""
        self._apply_border(P.green)
        # Context object = self: Qt drops the timer if the card is deleted
        # first (removed / list cleared within 0.7 s), instead of calling into
        # a dead widget from the event loop.
        QTimer.singleShot(700, self, lambda: self._apply_border(P.border))

    # -- styling ---------------------------------------------------------

    def _apply_border(self, color: str) -> None:
        self.setStyleSheet(f"""
            GroceryItemCard {{
                background-color: {P.bg_card};
                border: 1px solid {color};
                border-radius: 4px;
            }}
        """)

    # -- price loading ---------------------------------------------------

    def _load(self) -> None:
        item_id = self._item.get("id")
        result = None
        # Retry while another widget holds the in-progress lock for this id.
        for _attempt in range(6):
            result = self._data.fetch_item_prices(item_id)
            if result.ok or result.error_type != "in_progress":
                break
            time.sleep(0.5)
        try:
            if result is not None and result.ok:
                self._signal.ready.emit(result.data)
            else:
                msg = result.error if result is not None else _("Unknown error")
                self._signal.error.emit(msg or _("Unknown error"))
        except RuntimeError:
            pass  # card already destroyed

    def _clear_rows(self) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def _on_prices_error(self, error: str) -> None:
        self._clear_rows()
        lbl = QLabel(_("Could not load prices"))
        lbl.setToolTip(str(error))
        lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.red}; "
            f"background: transparent; padding: 4px 8px;"
        )
        self._rows_layout.addWidget(lbl)

    def _on_prices_ready(self, prices: list) -> None:
        self._loaded = True
        self._buy_rows = buy_locations(prices)
        self._rebuild_rows()
        if self._on_loaded is not None:
            self._on_loaded(self)

    # -- rendering -------------------------------------------------------

    def _rebuild_rows(self) -> None:
        self._clear_rows()

        if not self._buy_rows:
            lbl = QLabel(_("No buy locations found"))
            lbl.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; "
                f"background: transparent; padding: 4px 8px;"
            )
            self._rows_layout.addWidget(lbl)
            return

        total = len(self._buy_rows)
        visible = self._buy_rows if self._expanded else self._buy_rows[:1]
        for idx, row in enumerate(visible[:GROCERY_BUY_DISPLAY_MAX]):
            self._rows_layout.addWidget(self._make_row(row, idx))

        if total > 1:
            self._rows_layout.addWidget(self._make_toggle(total))

    def _make_row(self, row: dict, idx: int) -> QWidget:
        is_best = idx == 0
        bg = P.bg_card if idx % 2 == 0 else P.bg_input

        w = _ClickRow(self._toggle)
        w.setStyleSheet(f"background-color: {bg};")
        # Tooltip reflects what clicking will do in the CURRENT state:
        #   showing all → "Hide Other..."; showing one → "Show Other..."
        w.setToolTip(_tooltip_hide() if self._expanded else _tooltip_show())

        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 2, 6, 2)
        lay.setSpacing(4)

        left = QWidget()
        left.setStyleSheet("background: transparent;")
        left_lay = QVBoxLayout(left)
        left_lay.setContentsMargins(0, 0, 0, 0)
        left_lay.setSpacing(0)

        t_lbl = QLabel(str(row["terminal"]))
        t_lbl.setWordWrap(True)
        t_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg}; background: transparent;"
        )
        left_lay.addWidget(t_lbl)
        if row["location"]:
            l_lbl = QLabel(str(row["location"]))
            l_lbl.setWordWrap(True)
            l_lbl.setStyleSheet(
                f"font-family: Consolas; font-size: 7pt; color: {P.fg_dim}; background: transparent;"
            )
            left_lay.addWidget(l_lbl)
        lay.addWidget(left, 1)

        price_text = format_price(row["price"])
        if is_best:
            price_text += "  ★"  # ★ marks the cheapest
        p_lbl = QLabel(price_text)
        p_lbl.setMinimumWidth(110)
        p_lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        p_lbl.setStyleSheet(f"""
            font-family: Consolas; font-size: 9pt; font-weight: bold;
            color: {P.green if is_best else P.fg}; background: transparent;
            padding-right: 4px;
        """)
        if is_best:
            p_lbl.setToolTip(_("Cheapest place to buy"))
        lay.addWidget(p_lbl)

        return w

    def _make_toggle(self, total: int) -> QWidget:
        w = _ClickRow(self._toggle)
        w.setStyleSheet("background: transparent;")
        w.setToolTip(_tooltip_hide() if self._expanded else _tooltip_show())

        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 1, 6, 3)
        lay.setSpacing(0)

        if self._expanded:
            text = _("▾  Hide other locations/prices")
        else:
            text = _("▸  Show {n} other locations/prices").format(n=total - 1)
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.accent}; background: transparent;"
        )
        lay.addWidget(lbl)
        lay.addStretch(1)
        return w

    def _toggle(self) -> None:
        if len(self._buy_rows) <= 1:
            return
        self._expanded = not self._expanded
        self._rebuild_rows()

    def _remove(self) -> None:
        if self._on_remove:
            self._on_remove(self)


# ---------------------------------------------------------------------------
# Title bar that drags the window
# ---------------------------------------------------------------------------

class _DragBar(QWidget):
    """Title bar whose drag moves the parent top-level window."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._press: QPoint | None = None

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._press = (
                event.globalPosition().toPoint()
                - self.window().frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self._press is not None and event.buttons() & Qt.LeftButton:
            self.window().move(event.globalPosition().toPoint() - self._press)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self._press = None


# ---------------------------------------------------------------------------
# Grocery List bubble
# ---------------------------------------------------------------------------

class GroceryListBubble(QWidget):
    """Floating, draggable shopping list that accepts dropped items."""

    def __init__(self, data_service, on_plot_route: Callable | None = None, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint,
        )
        self._data = data_service
        self._on_plot_route = on_plot_route
        self._cards: dict[object, GroceryItemCard] = {}

        self.setAcceptDrops(True)
        self.setMinimumSize(320, 260)
        self.resize(380, 520)
        self._apply_border(P.tool_market)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(2, 2, 2, 2)
        outer.setSpacing(0)

        # ── Title bar ──
        title_bar = _DragBar()
        title_bar.setFixedHeight(28)
        title_bar.setStyleSheet(f"background-color: {P.bg_header};")
        tb_lay = QHBoxLayout(title_bar)
        tb_lay.setContentsMargins(8, 2, 6, 2)
        tb_lay.setSpacing(6)

        title_lbl = QLabel("\U0001f6d2  " + _("GROCERY LIST"))
        title_lbl.setStyleSheet(f"""
            font-family: Electrolize, Consolas;
            font-size: 9pt;
            font-weight: bold;
            color: {P.tool_market};
            background: transparent;
        """)
        tb_lay.addWidget(title_lbl)

        self._count_lbl = QLabel("(0)")
        self._count_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;"
        )
        tb_lay.addWidget(self._count_lbl)

        self._route_status = QLabel("")
        self._route_status.setStyleSheet(
            f"font-family: Consolas; font-size: 7pt; color: {P.accent}; background: transparent;"
        )
        self._route_status.setVisible(False)
        tb_lay.addWidget(self._route_status)

        self._route_busy = False
        # Once a route is on the map it follows the list: adding, removing or
        # clearing items (or prices arriving) re-plans it after a short pause.
        self._route_live = False
        self._route_pending = False
        self._route_requested = False     # clicked before any price had loaded
        self._replot_timer = QTimer(self)
        self._replot_timer.setSingleShot(True)
        self._replot_timer.setInterval(350)
        self._replot_timer.timeout.connect(lambda: self._plot_route(auto=True))
        tb_lay.addStretch(1)

        clear_btn = QLabel(_("Clear"))
        clear_btn.setToolTip(_("Remove all items from the grocery list"))
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            color: {P.fg_dim}; background: transparent;
            border: 1px solid {P.border}; border-radius: 3px; padding: 1px 8px;
        """)
        clear_btn.mousePressEvent = lambda _e: self.clear()
        tb_lay.addWidget(clear_btn)


        plot_btn = QLabel(_("⤳ Plot Route"))
        plot_btn.setToolTip(_("Plot the shortest shopping route for every item "
                              "on this list on the Star Map (UEX distance telemetry). "
                              "The route follows the list as you change it."))
        plot_btn.setCursor(Qt.PointingHandCursor)
        plot_btn.setStyleSheet(f"""
            font-family: Consolas; font-size: 8pt; font-weight: bold;
            color: {P.tool_market}; background: transparent;
            border: 1px solid {P.tool_market}; border-radius: 3px; padding: 1px 8px;
        """)
        plot_btn.mousePressEvent = lambda _e: self._plot_route()
        tb_lay.addWidget(plot_btn)
        close_btn = QLabel("✕")
        close_btn.setToolTip(_("Close (your list is kept)"))
        close_btn.setFixedSize(20, 20)
        close_btn.setAlignment(Qt.AlignCenter)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.setStyleSheet(
            f"font-size: 11pt; color: {P.fg_dim}; background: transparent;"
        )
        close_btn.mousePressEvent = lambda _e: self.hide()
        tb_lay.addWidget(close_btn)

        outer.addWidget(title_bar)
        outer.addWidget(self._build_route_options())

        # ── Drop area (gets the highlight border during a drag) ──
        self._drop_frame = QFrame()
        self._drop_frame.setObjectName("dropFrame")
        self._set_drop_highlight(False)
        drop_lay = QVBoxLayout(self._drop_frame)
        drop_lay.setContentsMargins(0, 0, 0, 0)
        drop_lay.setSpacing(0)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background-color: {P.bg_primary}; border: none; }}"
        )

        self._inner = QWidget()
        self._inner.setStyleSheet(f"background-color: {P.bg_primary};")
        self._inner_layout = QVBoxLayout(self._inner)
        self._inner_layout.setContentsMargins(6, 6, 6, 6)
        self._inner_layout.setSpacing(6)

        self._empty_lbl = QLabel(
            _("Your grocery list is empty.\n\n"
              "Drag an item from the list on the left onto this window "
              "to add it. Each item shows where to buy it and how much "
              "it costs.")
        )
        self._empty_lbl.setWordWrap(True)
        self._empty_lbl.setAlignment(Qt.AlignCenter)
        self._empty_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim}; "
            f"background: transparent; padding: 30px 16px;"
        )
        self._inner_layout.addWidget(self._empty_lbl)
        self._inner_layout.addStretch(1)

        self._scroll.setWidget(self._inner)
        drop_lay.addWidget(self._scroll)
        outer.addWidget(self._drop_frame, 1)

    # -- styling ---------------------------------------------------------

    def _apply_border(self, color: str) -> None:
        self.setStyleSheet(f"""
            GroceryListBubble {{
                background-color: {P.bg_secondary};
                border: 2px solid {color};
                border-radius: 6px;
            }}
        """)

    def _set_drop_highlight(self, on: bool) -> None:
        color = P.green if on else "transparent"
        # objectName selector keeps the border on the frame only.
        self._drop_frame.setStyleSheet(
            f"#dropFrame {{ border: 2px dashed {color}; border-radius: 4px; }}"
        )

    # -- list management -------------------------------------------------

    def add_item(self, item: dict) -> None:
        item_id = item.get("id")
        if item_id is None:
            return
        existing = self._cards.get(item_id)
        if existing is not None:
            existing.flash()
            return

        self._empty_lbl.hide()
        card = GroceryItemCard(item, self._data, self._remove_card, parent=self._inner,
                               on_loaded=lambda _c: self._list_changed())
        self._cards[item_id] = card
        # Insert before the trailing stretch.
        self._inner_layout.insertWidget(self._inner_layout.count() - 1, card)
        self._update_count()
        self._list_changed()

    def _remove_card(self, card: GroceryItemCard) -> None:
        self._cards.pop(card.item_id(), None)
        card.setParent(None)
        card.deleteLater()
        self._update_count()
        if not self._cards:
            self._empty_lbl.show()
        self._list_changed()

    def clear(self) -> None:
        for card in list(self._cards.values()):
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self._empty_lbl.show()
        self._update_count()
        self._list_changed()

    def _list_changed(self) -> None:
        """Re-plan the shown route (debounced) whenever the list changes."""
        if self._route_live:
            self._replot_timer.start()

    def _update_count(self) -> None:
        self._count_lbl.setText(f"({len(self._cards)})")

    # -- route option ----------------------------------------------------

    def _build_route_options(self) -> QWidget:
        """The "Shorter trip over cheapest price" toggle + its percentage,
        and the one-line summary of the last planned route under them."""
        from ..route_planner import PREMIUM_PCT_MAX

        box = QWidget()
        box.setStyleSheet(f"background-color: {P.bg_secondary};")
        v = QVBoxLayout(box)
        v.setContentsMargins(8, 3, 6, 3)
        v.setSpacing(1)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        tip = _("Let the route pay up to this much more than an item's cheapest "
                "price when that makes the trip shorter. Off = always the "
                "cheapest price (equal prices are still used to shorten it).")
        self._shorter_cb = QCheckBox(_("Shorter trip over cheapest price"))
        self._shorter_cb.setToolTip(tip)
        self._shorter_cb.setCursor(Qt.PointingHandCursor)
        self._shorter_cb.setStyleSheet(
            f"QCheckBox {{ font-family: Consolas; font-size: 8pt; color: {P.fg}; "
            f"background: transparent; }}")
        row.addWidget(self._shorter_cb)

        self._premium_spin = QSpinBox()
        self._premium_spin.setRange(0, PREMIUM_PCT_MAX)
        self._premium_spin.setPrefix("+")
        self._premium_spin.setSuffix(" %")
        self._premium_spin.setToolTip(tip)
        self._premium_spin.setFixedWidth(74)
        self._premium_spin.setStyleSheet(
            f"QSpinBox {{ font-family: Consolas; font-size: 8pt; color: {P.fg}; "
            f"background-color: {P.bg_input}; border: 1px solid {P.border}; "
            f"border-radius: 3px; padding: 0px 2px; }} "
            f"QSpinBox:disabled {{ color: {P.fg_dim}; }}")
        row.addWidget(self._premium_spin)
        row.addStretch(1)
        v.addLayout(row)

        # One fixed line, no word wrap; it never widens the bubble (Ignored
        # horizontal policy) -- the tooltip carries the detail.
        self._route_summary = QLabel("")
        self._route_summary.setWordWrap(False)
        self._route_summary.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._route_summary.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; color: {P.green}; background: transparent;")
        self._route_summary.setVisible(False)
        v.addWidget(self._route_summary)

        opts = load_route_options()
        self._shorter_cb.setChecked(opts["shorter"])
        self._premium_spin.setValue(opts["premium_pct"])
        self._premium_spin.setEnabled(opts["shorter"])
        self._shorter_cb.toggled.connect(self._route_option_changed)
        self._premium_spin.valueChanged.connect(self._route_option_changed)
        return box

    def route_premium(self) -> float:
        """The price premium the planner may use (0.0 when the option is off)."""
        if not self._shorter_cb.isChecked():
            return 0.0
        return self._premium_spin.value() / 100.0

    def _route_option_changed(self, *_args) -> None:
        """Save the option and re-plan the shown route with it right away."""
        on = self._shorter_cb.isChecked()
        self._premium_spin.setEnabled(on)
        save_route_options(on, self._premium_spin.value())
        if self._route_live:
            self._plot_route(auto=True)

    def _show_route_summary(self, plan: "Optional[dict]") -> None:
        totals = (plan or {}).get("totals")
        if not (plan or {}).get("stops") or not totals:
            self._route_summary.setText("")
            self._route_summary.setVisible(False)
            return
        cheapest = plan.get("cheapest")
        self._route_summary.setText(route_summary_text(totals, cheapest))
        tip = _("This route: {auec} for {n} item(s), {stops} stop(s), {gm}.").format(
            auec=format_price(totals["auec"]), n=len(plan["stops"]), stops=totals["stops"],
            gm=(f"{totals['gm']:,.0f} Gm" if totals["gm"] is not None else _("travel unknown")))
        if cheapest is not None:
            tip += "\n" + _("Cheapest-price route: {auec}, {stops} stop(s), {gm}.").format(
                auec=format_price(cheapest["auec"]), stops=cheapest["stops"],
                gm=(f"{cheapest['gm']:,.0f} Gm" if cheapest["gm"] is not None
                    else _("travel unknown")))
        self._route_summary.setToolTip(tip)
        self._route_summary.setVisible(True)

    # -- optimal route ---------------------------------------------------

    def _set_route_status(self, text: "Optional[str]") -> None:
        """Show/hide the small telemetry-fetch status in the title bar."""
        self._route_status.setText(text or "")
        self._route_status.setVisible(bool(text))

    def _plot_route(self, auto: bool = False) -> None:
        """Plan the shopping route and ask the Star Map to draw it.

        :func:`market_finder.route_planner.plan_route` picks the terminal for
        every item and the visit order that minimise travel (Gm, see
        :func:`market_finder.starmap.distances.site_distance`).  Missing UEX
        distance telemetry is fetched first and planning runs on the same
        worker thread, so the UI never blocks; the title bar shows progress.

        *auto* = re-plan after the list changed (the map is only updated if
        it is still showing a shopping route).
        """
        from ..route_planner import (
            candidate_sites, collect_wants, missing_price_cards, plan_shopping,
        )
        from ..starmap import distances as uex_dist

        if self._route_busy:
            self._route_pending = True        # re-plan once the current one lands
            return
        cards = list(self._cards.values())
        if not auto:
            self._route_live = True
            for card in missing_price_cards(cards):
                card.flash()
        wants = collect_wants(cards)
        if not wants:
            if not auto:
                self._route_requested = True           # plot as soon as prices arrive
            else:
                self._show_route_summary(None)
                if self._on_plot_route is not None:
                    self._on_plot_route([], auto=True)  # list emptied: clear the map
            return
        if auto and self._route_requested:
            auto = False                               # the click that had nothing to plot yet
        self._route_requested = False

        # Read on the UI thread; the worker plans with this value.  The
        # premium's candidate sites are a superset of the cheapest-price
        # ones, so one telemetry fetch serves both plans.
        premium = self.route_premium()
        sites, _eligible = candidate_sites(wants, premium)
        pairs = uex_dist.telemetry_pairs(sites)

        self._route_busy = True
        if pairs:
            self._set_route_status(_("Fetching UEX distance telemetry…"))

        sig = _RouteSignal(self)
        sig.progress.connect(
            lambda done, total: self._set_route_status(
                _("Fetching UEX distance telemetry… {done}/{total}").format(
                    done=done, total=total)))
        sig.done.connect(lambda plan: self._route_ready(plan, auto))

        def _work() -> None:
            plan: dict = {}
            try:
                if pairs:
                    uex_dist.fetch_missing(
                        pairs, on_progress=lambda d, t: sig.progress.emit(d, t))
                plan = plan_shopping(wants, uex_dist.site_distance,
                                     max_price_premium=premium)
            except Exception:
                plan = {}
            finally:
                try:
                    sig.done.emit(plan)
                except RuntimeError:
                    pass  # bubble already destroyed

        threading.Thread(target=_work, daemon=True).start()

    def _route_ready(self, plan: dict, auto: bool) -> None:
        self._route_busy = False
        self._set_route_status(None)
        ordered = list((plan or {}).get("stops") or [])
        self._show_route_summary(plan)
        if self._on_plot_route is not None and (ordered or auto):
            self._on_plot_route(ordered, auto=auto)
        if self._route_pending:
            self._route_pending = False
            self._replot_timer.start()

    # -- drag & drop -----------------------------------------------------

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat(SC_ITEM_MIME):
            event.setDropAction(Qt.CopyAction)
            event.acceptProposedAction()
            self._set_drop_highlight(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat(SC_ITEM_MIME):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_drop_highlight(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_drop_highlight(False)
        md = event.mimeData()
        if not md.hasFormat(SC_ITEM_MIME):
            event.ignore()
            return
        try:
            raw = bytes(md.data(SC_ITEM_MIME))
            item = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        if isinstance(item, dict) and item.get("id") is not None:
            self.add_item(item)
            event.acceptProposedAction()
        else:
            event.ignore()

    # -- positioning -----------------------------------------------------

    def position_beside(self, anchor: QWidget) -> None:
        """Place the bubble just to the right of *anchor* (or left if no room)."""
        from PySide6.QtGui import QGuiApplication

        geo = anchor.frameGeometry()
        x = geo.right() + 12
        y = geo.top()
        screen = QGuiApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            if x + self.width() > avail.right():
                x = geo.left() - self.width() - 12
            x = max(avail.left() + 8, min(x, avail.right() - self.width() - 8))
            y = max(avail.top() + 8, min(y, avail.bottom() - self.height() - 8))
        self.move(x, y)
