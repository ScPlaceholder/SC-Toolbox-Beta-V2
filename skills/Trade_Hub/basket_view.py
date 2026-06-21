"""Basket view — multi-commodity pickup planner UI.

A view-mode widget that slots into the existing Trade Hub right-hand
pane. The user picks a starting location, checks several commodities,
and clicks "Plan Route". The planner clusters commodities that share a
seller terminal and orders the remaining stops by real UEX Gm distance
from the previous stop.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, List, Optional

from PySide6.QtCore import Qt, Signal, QObject
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QFrame, QScrollArea,
)

from shared.qt.theme import P
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.qt.fuzzy_multi_check import SCFuzzyMultiCheck
from shared.i18n import s_ as _

from trade_hub_data import Route, DistanceCache, fmt_distance, get_unique_commodities
from basket_engine import (
    BasketPlan, BasketStop, Offer,
    build_sellers_index, build_buyers_index,
    plan_variants, plan_sell_variants,
    distance_pairs_needed,
)

log = logging.getLogger("TradeHub.basket")


class _PlanSignals(QObject):
    done = Signal(object)   # list[BasketPlan]
    failed = Signal(str)
    progress = Signal(str)


class BasketPlanCard(QFrame):
    """Summary card for one basket plan variant. Clickable → opens detail dialog."""

    clicked = Signal(object)  # BasketPlan

    def __init__(self, index: int, plan: BasketPlan, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._plan = plan
        self.setObjectName("basket_plan_card")
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet(
            f"QFrame#basket_plan_card {{ background: {P.bg_card}; "
            f"border: 1px solid {P.border}; border-radius: 4px; }}"
            f"QFrame#basket_plan_card:hover {{ border-color: {P.accent}; }}"
        )

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(8)

        step = QLabel(f"#{index + 1}")
        step.setStyleSheet(
            f"font-family: Consolas; font-size: 11pt; font-weight: bold; "
            f"color: {P.accent}; background: transparent;"
        )
        header.addWidget(step)

        title = QLabel(plan.label or _("BASKET ROUTE"))
        title.setStyleSheet(
            f"font-family: Consolas; font-size: 11pt; font-weight: bold; "
            f"color: {P.tool_trade}; background: transparent;"
        )
        header.addWidget(title, 1)

        dist_lbl = QLabel(fmt_distance(plan.total_distance_gm))
        dist_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; "
            f"color: {P.energy_cyan}; background: transparent;"
        )
        header.addWidget(dist_lbl)

        lay.addLayout(header)

        stops_line = _("%(n)d stop(s)") % {"n": len(plan.stops)}
        commodities = []
        for stop in plan.stops:
            for o in stop.picks:
                if o.commodity not in commodities:
                    commodities.append(o.commodity)
        summary = QLabel(f"{stops_line} · " + " · ".join(commodities))
        summary.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; "
            f"color: {P.fg}; background: transparent;"
        )
        summary.setWordWrap(True)
        lay.addWidget(summary)

        terminals = " → ".join(s.terminal.terminal_name or "?" for s in plan.stops)
        route_lbl = QLabel(terminals)
        route_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        route_lbl.setWordWrap(True)
        lay.addWidget(route_lbl)

        if plan.unresolved:
            unres = QLabel(_("unresolved: ") + ", ".join(plan.unresolved))
            unres.setStyleSheet(
                f"font-family: Consolas; font-size: 8pt; "
                f"color: {P.red}; background: transparent;"
            )
            unres.setWordWrap(True)
            lay.addWidget(unres)

        hint = QLabel(_("Click to view details · Pin to keep open"))
        hint.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; font-style: italic; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        lay.addWidget(hint)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self.clicked.emit(self._plan)
        super().mousePressEvent(event)


class BasketView(QWidget):
    """Multi-commodity pickup planner.

    Parent should call :meth:`refresh_data` after routes are loaded so
    the commodity list and starting-location dropdown are populated.
    Connect to :attr:`plan_clicked` to open a detail dialog per plan.
    """

    plan_clicked = Signal(object)  # BasketPlan

    def __init__(
        self,
        routes_getter: Callable[[], List[Route]],
        dist_cache: DistanceCache,
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self._routes_getter = routes_getter
        self._dist_cache = dist_cache
        self._mode = "buy"  # "buy" | "sell"
        self._signals = _PlanSignals()
        self._signals.done.connect(self._on_plan_done)
        self._signals.failed.connect(self._on_plan_failed)
        self._signals.progress.connect(self._set_status)
        self._build_ui()

    # ── Construction ──

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        # Mode toggle row — BUY (pickup planner) vs SELL (sale planner).
        # Swaps the label, button wording, and the index / ranking used
        # when the user clicks PLAN ROUTE.
        mode_row = QHBoxLayout()
        mode_row.setSpacing(6)
        mode_lbl = QLabel(_("MODE:"))
        mode_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; "
            f"color: {P.tool_trade}; background: transparent;"
        )
        mode_row.addWidget(mode_lbl)
        self._btn_buy = QPushButton(_("BUY"))
        self._btn_buy.setCursor(Qt.PointingHandCursor)
        self._btn_buy.clicked.connect(lambda: self._set_mode("buy"))
        mode_row.addWidget(self._btn_buy)
        self._btn_sell = QPushButton(_("SELL"))
        self._btn_sell.setCursor(Qt.PointingHandCursor)
        self._btn_sell.clicked.connect(lambda: self._set_mode("sell"))
        mode_row.addWidget(self._btn_sell)
        mode_row.addStretch(1)
        root.addLayout(mode_row)

        # Controls row
        controls = QHBoxLayout()
        controls.setSpacing(10)

        start_col = QVBoxLayout()
        start_col.setSpacing(2)
        start_lbl = QLabel(_("START LOCATION:"))
        start_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; "
            f"color: {P.tool_trade}; background: transparent;"
        )
        start_col.addWidget(start_lbl)
        self._start_combo = SCFuzzyCombo(placeholder=_("Starting terminal..."))
        start_col.addWidget(self._start_combo)
        controls.addLayout(start_col, 2)

        comm_col = QVBoxLayout()
        comm_col.setSpacing(2)
        self._comm_lbl = QLabel(_("COMMODITIES TO ACQUIRE:"))
        self._comm_lbl.setStyleSheet(
            f"font-family: Consolas; font-size: 8pt; "
            f"color: {P.tool_trade}; background: transparent;"
        )
        comm_col.addWidget(self._comm_lbl)
        self._commodity_picker = SCFuzzyMultiCheck(label=_("Select commodities..."))
        comm_col.addWidget(self._commodity_picker)
        controls.addLayout(comm_col, 3)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(2)
        btn_col.addSpacing(14)
        self._plan_btn = QPushButton(_("PLAN ROUTE"))
        self._plan_btn.setCursor(Qt.PointingHandCursor)
        self._plan_btn.setStyleSheet(
            f"QPushButton {{ background: {P.accent}; color: #ffffff; "
            f"border: none; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; padding: 6px 14px; }}"
            f"QPushButton:hover {{ background: {P.energy_cyan}; }}"
            f"QPushButton:disabled {{ background: {P.bg_card}; color: {P.fg_dim}; }}"
        )
        self._plan_btn.clicked.connect(self._on_plan_clicked)
        btn_col.addWidget(self._plan_btn)
        controls.addLayout(btn_col, 0)

        root.addLayout(controls)

        # Status / summary line
        self._status = QLabel("")
        self._status.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; "
            f"color: {P.fg_dim}; background: transparent;"
        )
        root.addWidget(self._status)

        # Scrollable card list
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.NoFrame)
        self._scroll.setStyleSheet(
            f"QScrollArea {{ background: {P.bg_secondary}; border: none; }}"
        )
        self._cards_host = QWidget()
        self._cards_layout = QVBoxLayout(self._cards_host)
        self._cards_layout.setContentsMargins(2, 2, 2, 2)
        self._cards_layout.setSpacing(8)
        self._cards_layout.addStretch(1)
        self._scroll.setWidget(self._cards_host)
        root.addWidget(self._scroll, 1)

        self._update_mode_btns()

    # ── Mode toggle ──

    def _update_mode_btns(self) -> None:
        active = (
            f"QPushButton {{ background: {P.accent}; color: #ffffff; "
            f"border: none; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; padding: 3px 10px; }}"
        )
        inactive = (
            f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; "
            f"border: none; font-family: Consolas; font-size: 9pt; "
            f"font-weight: bold; padding: 3px 10px; }}"
            f"QPushButton:hover {{ color: {P.fg}; }}"
        )
        self._btn_buy.setStyleSheet(active if self._mode == "buy" else inactive)
        self._btn_sell.setStyleSheet(active if self._mode == "sell" else inactive)

        if self._mode == "sell":
            self._comm_lbl.setText(_("COMMODITIES TO SELL:"))
            self._plan_btn.setText(_("PLAN SALE"))
        else:
            self._comm_lbl.setText(_("COMMODITIES TO ACQUIRE:"))
            self._plan_btn.setText(_("PLAN ROUTE"))

    def _set_mode(self, mode: str) -> None:
        if mode not in ("buy", "sell") or mode == self._mode:
            return
        self._mode = mode
        self._update_mode_btns()
        self.refresh_data()
        self._clear_cards()

    # ── Public API ──

    def refresh_data(self) -> None:
        """Reload commodities + starting-location list from current routes.

        Start-location candidates differ by mode: BUY mode lists
        pickup (buy) terminals, SELL mode lists dropoff (sell)
        terminals. The user may already be sitting at a station that
        only buys — exposing those in SELL mode lets them plan the
        shortest sale loop from where they are.
        """
        routes = self._routes_getter() or []
        commodities = get_unique_commodities(routes)
        self._commodity_picker.set_items(commodities)

        starts: dict[str, int] = {}
        if self._mode == "sell":
            for r in routes:
                if r.id_terminal_sell and r.sell_terminal:
                    starts.setdefault(r.sell_terminal, r.id_terminal_sell)
        else:
            for r in routes:
                if r.id_terminal_buy and r.buy_terminal:
                    starts.setdefault(r.buy_terminal, r.id_terminal_buy)
        sorted_names = sorted(starts.keys(), key=str.casefold)
        self._start_combo.set_items(sorted_names)
        self._start_term_map = starts

        if not sorted_names:
            self._set_status(_("No routes loaded yet — wait for data fetch."))
        elif self._mode == "sell":
            self._set_status(
                _("Pick a starting terminal, check the commodities to sell, "
                  "then click PLAN SALE.")
            )
        else:
            self._set_status(
                _("Pick a starting terminal, check the commodities to acquire, "
                  "then click PLAN ROUTE.")
            )

    # ── Plan flow ──

    def _on_plan_clicked(self) -> None:
        selected = set(self._commodity_picker.get_selected())
        if not selected:
            self._set_status(_("Select at least one commodity."))
            return

        start_name = (self._start_combo.current_text() or "").strip()
        start_id = self._start_term_map.get(start_name) if start_name else None

        routes = self._routes_getter() or []
        mode = self._mode
        if mode == "sell":
            index = build_buyers_index(routes, selected)
            empty_msg = _("No active buyers found for the selected commodities.")
        else:
            index = build_sellers_index(routes, selected)
            empty_msg = _("No in-stock sellers found for the selected commodities.")

        if not index:
            self._set_status(empty_msg)
            self._clear_cards()
            return

        self._plan_btn.setEnabled(False)
        self._set_status(_("Fetching distances..."))

        pairs = distance_pairs_needed(index, start_id)

        def _worker() -> None:
            try:
                if pairs:
                    self._dist_cache.fetch_missing(pairs)
                if mode == "sell":
                    plans = plan_sell_variants(
                        index, selected, start_id, self._dist_cache, max_variants=5,
                    )
                else:
                    plans = plan_variants(
                        index, selected, start_id, self._dist_cache, max_variants=5,
                    )
                self._signals.done.emit(plans)
            except Exception as exc:  # top-level background-thread guard
                log.exception("Basket plan failed")
                self._signals.failed.emit(str(exc))

        threading.Thread(target=_worker, daemon=True, name="BasketPlan").start()

    def _on_plan_done(self, plans: List[BasketPlan]) -> None:
        self._plan_btn.setEnabled(True)
        self._render_plans(plans)

    def _on_plan_failed(self, msg: str) -> None:
        self._plan_btn.setEnabled(True)
        self._set_status(_("Plan failed: ") + msg)

    # ── Rendering ──

    def _render_plans(self, plans: List[BasketPlan]) -> None:
        self._clear_cards()

        if not plans:
            self._set_status(_("No route could be built."))
            return

        for i, plan in enumerate(plans):
            card = BasketPlanCard(i, plan, self._cards_host)
            card.clicked.connect(self.plan_clicked.emit)
            self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)

        self._set_status(
            _("%(n)d plan option(s) — click a card to open details, pin to keep up to 5 open.")
            % {"n": len(plans)}
        )

    def _clear_cards(self) -> None:
        # Remove all widgets but keep the trailing stretch.
        while self._cards_layout.count() > 1:
            item = self._cards_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()

    def _set_status(self, text: str) -> None:
        self._status.setText(text)
