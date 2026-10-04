# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
# Moved from skills/Everything_Finder/everything_finder/shopping_popout.py to shared/shopping/ and
# extended on 2026-10-04, when the three shopping lists became one (subagent of session 47adec0d).
"""The one shopping-list widget, shown by Item Finder, the Star Map and the Everything Finder.

:class:`ShoppingListPanel` is the list itself - embeddable (the Star Map docks
it beside the map). :class:`ShoppingListWindow` is the same panel in a
frameless, always-on-top pop-out (Item Finder and the Everything Finder open
that). Both show the same :class:`~.shopping_list.ShoppingList`, so an entry
added in one tool is in the list of every other.

Everything that can touch the network runs on a worker thread and reports
back through Qt signals; the UI thread only renders. A panel that is not on
screen loads nothing and plans nothing (the Star Map builds its docked panel
hidden, and opening the map must not start fetching Item Finder's catalogue);
the one exception is a route it has drawn on the map, which it keeps current.

What it can do, and which of the three old lists each part came from:

  * add ITEMS and COMMODITIES by name, with a quantity      (Everything Finder)
  * take an item DROPPED on it - a row dragged out of Item Finder's table, or a
    Star Map item pop-out                               (Item Finder, Star Map)
  * keep an item PINNED to the place it was added from on the map, with a
    click to unpin it                                                (Star Map)
  * list, under each entry, WHERE it is sold and for how much, cheapest first,
    collapsed to the best price until clicked                     (Item Finder)
  * plan the route with Trade Hub's basket planner: start terminal, preferred
    strategy, terminals per entry, auto-calculate              (Everything Finder)
  * draw a plan on the star map, and KEEP the drawn route in step with the
    list as it changes                                  (Item Finder, Star Map)
  * survive a restart, and follow changes made in another tool's window

Not carried over: Item Finder's "Shorter trip over cheapest price +N%" option.
It was a parameter of Item Finder's own route planner, which is retired; Trade
Hub's planner offers SHORTEST TRIP and BEST PRICE as whole strategies instead,
and has no knob in between. Building one here would be a second route planner.

The planning itself is Trade Hub's basket planner; see shopping_list.py.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from shared.qt.data_table import SC_ITEM_MIME
from shared.qt.fuzzy_combo import SCFuzzyCombo
from shared.qt.theme import P

from .shopping_list import (
    KINDS, PER_ENTRY_DEFAULT, PER_ENTRY_MAX, STRATEGIES, ShoppingList, entry_label,
    plan_cost, plan_routes, plan_summary, split_label,
)

log = logging.getLogger(__name__)

ACCENT = "#55ddaa"
_KIND_LABEL = {"item": "ITEM", "commodity": "CMDTY"}

#: How often an open panel checks whether another tool changed the list.
REFRESH_MS = 1500
#: Offers listed under an expanded entry (the rest are a click away in Item Finder).
OFFERS_SHOWN_MAX = 12


def _btn_ss(color: str = ACCENT) -> str:
    return (f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; border: 1px solid {P.border}; "
            f"padding: 4px 10px; font-family: Consolas; font-size: 9pt; }} "
            f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {color}; }} "
            f"QPushButton:checked {{ background: {color}; color: #04140d; border-color: {color}; "
            f"font-weight: bold; }} "
            f"QPushButton:disabled {{ color: {P.fg_disabled}; }}")


def _lbl(text: str, color: str, size: float = 8.0, bold: bool = False) -> QLabel:
    w = QLabel(text)
    w.setStyleSheet(f"color: {color}; font-family: Consolas; font-size: {size}pt; "
                    f"{'font-weight: bold; ' if bold else ''}background: transparent;")
    return w


class _Signals(QObject):
    names_ready = Signal(str, object)      # kind, {name: id-or-None}
    plan_done = Signal(object, object, int)  # plans, PlanInput, request id
    plan_failed = Signal(str, int)
    status = Signal(str)


class _Click(QLabel):
    """A label that runs a callback when left-clicked."""

    def __init__(self, text: str, on_click: Callable[[], None], parent: Optional[QWidget] = None):
        super().__init__(text, parent)
        self._on_click = on_click
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._on_click()
            event.accept()
            return
        super().mousePressEvent(event)


class _EntryRow(QWidget):
    """One entry: kind, name, pin, quantity, and (expanded) where it is sold."""

    def __init__(self, entry, offers: Optional[list], expanded: bool,
                 on_remove: Callable[[], None], on_toggle: Callable[[], None],
                 on_unpin: Callable[[], None], parent: QWidget) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 3, 6, 3)
        outer.setSpacing(1)
        top = QHBoxLayout()
        top.setSpacing(8)
        tag = _lbl(_KIND_LABEL.get(entry.kind, entry.kind.upper()),
                   P.tool_market if entry.kind == "item" else P.tool_trade, 7.5, bold=True)
        tag.setFixedWidth(46)
        top.addWidget(tag)
        nm = _lbl(entry.name, P.fg, 9)
        nm.setWordWrap(True)
        top.addWidget(nm, 1)
        self.pin_label: Optional[QLabel] = None
        if entry.pin:
            where = entry.pin.get("location") or "?"
            self.pin_label = _Click(f"@ {where}  x", on_unpin)
            self.pin_label.setToolTip(
                f"Pinned: buy this at {where}"
                + (f" ({entry.pin['system']})" if entry.pin.get("system") else "")
                + ". Click to unpin and let the planner choose where.")
            self.pin_label.setStyleSheet(
                f"color: {P.energy_cyan}; font-family: Consolas; font-size: 7.5pt; "
                f"border: 1px solid {P.energy_cyan}; border-radius: 3px; padding: 0 4px; "
                f"background: transparent;")
            top.addWidget(self.pin_label)
        top.addWidget(_lbl(f"x{entry.qty}" + (" SCU" if entry.kind == "commodity" else ""),
                           P.fg_dim, 8))
        rm = QPushButton("x")
        rm.setFixedWidth(22)
        rm.setCursor(Qt.PointingHandCursor)
        rm.setToolTip("Remove from the list")
        rm.setStyleSheet(f"QPushButton {{ color: {P.fg_dim}; background: transparent; border: none; "
                         f"font-family: Consolas; font-weight: bold; }} "
                         f"QPushButton:hover {{ color: #ff5566; }}")
        rm.clicked.connect(on_remove)
        top.addWidget(rm)
        outer.addLayout(top)

        # Where it is sold (known once a plan has loaded prices). Collapsed: the
        # best price only. Click to see the rest, click again to hide them.
        self.offer_labels: List[QLabel] = []
        self.toggle_label: Optional[QLabel] = None
        if offers:
            shown = offers[:OFFERS_SHOWN_MAX] if expanded else offers[:1]
            for i, (tk, off) in enumerate(shown):
                loc = tk.location if tk.location and tk.location.casefold() \
                    not in tk.terminal_name.casefold() else ""
                where = ", ".join(x for x in (loc, tk.system) if x)
                text = f"      {tk.terminal_name}" + (f"  ({where})" if where else "")
                price = f"{off.price_buy:,.0f} aUEC" + ("  *" if i == 0 else "")
                line = QHBoxLayout()
                line.setSpacing(6)
                a = _lbl(text, P.fg if i == 0 else P.fg_dim, 8)
                a.setWordWrap(True)
                b = _lbl(price, P.green if i == 0 else P.fg_dim, 8, bold=(i == 0))
                if i == 0:
                    b.setToolTip("Cheapest place to buy")
                line.addWidget(a, 1)
                line.addWidget(b)
                outer.addLayout(line)
                self.offer_labels.append(a)
            if len(offers) > 1:
                n = len(offers) - 1
                text = ("      v  hide other places" if expanded
                        else f"      >  {n} other place{'s' if n != 1 else ''}")
                self.toggle_label = _Click(text, on_toggle)
                self.toggle_label.setStyleSheet(
                    f"color: {P.accent}; font-family: Consolas; font-size: 7.5pt; background: transparent;")
                outer.addWidget(self.toggle_label)


class ShoppingListPanel(QWidget):
    """The shared shopping list: entries, route options and planned routes."""

    def __init__(self, shopping: ShoppingList, source,
                 on_show_on_map: Optional[Callable[[object], None]] = None,
                 on_clear_map: Optional[Callable[[], None]] = None,
                 accent: str = ACCENT, show_title: bool = False,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._list = shopping
        self._source = source
        self._on_show_on_map = on_show_on_map
        self._on_clear_map = on_clear_map
        self._accent = accent
        self._names: Dict[str, Dict[str, Optional[int]]] = {"item": {}, "commodity": {}}
        self._names_loading: set = set()
        self._kind = "item"
        self._req = 0
        self._plans: List[object] = []
        self._offers: Dict[str, list] = {}       # label -> [(TerminalKey, Offer)], cheapest first
        self._expanded: set = set()              # labels whose offer list is open
        self._rows: Dict[str, _EntryRow] = {}
        self._map_live: Optional[str] = None     # strategy label of the plan drawn on the map
        self._stale = False                      # the list changed while this panel was hidden
        self._start_map: Dict[str, int] = {}
        self._sig = _Signals(self)
        self._sig.names_ready.connect(self._on_names)
        self._sig.plan_done.connect(self._on_plan_done)
        self._sig.plan_failed.connect(self._on_plan_failed)
        self._sig.status.connect(self._set_status)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(800)
        self._auto_timer.timeout.connect(self.plan_now)

        # Another tool (another process) may change the list: look at the file
        # while this panel is on screen.
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setInterval(REFRESH_MS)
        self._refresh_timer.timeout.connect(self._poll_file)

        self.setAcceptDrops(True)
        self.setMinimumWidth(320)
        self._build(show_title)

        def _changed() -> None:
            try:
                self._on_list_changed()
            except RuntimeError:                  # this panel's C++ side is gone
                shopping.unsubscribe(_changed)
        self._changed_cb = _changed
        self._list.subscribe(_changed)
        self._render_entries()

    # construction
    def _build(self, show_title: bool) -> None:
        bl = QVBoxLayout(self)
        bl.setContentsMargins(10, 8, 10, 10)
        bl.setSpacing(7)

        if show_title:
            t = _lbl("SHOPPING LIST", self._accent, 9, bold=True)
            bl.addWidget(t)

        # add row
        add = QHBoxLayout()
        add.setSpacing(6)
        self._kind_btns: Dict[str, QPushButton] = {}
        for kind, text in (("item", "Item"), ("commodity", "Commodity")):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_btn_ss(P.tool_market if kind == "item" else P.tool_trade))
            b.clicked.connect(lambda _c=False, k=kind: self.set_kind(k))
            add.addWidget(b)
            self._kind_btns[kind] = b
        self._name_box = SCFuzzyCombo(placeholder="Item name...", items=[])
        self._name_box.item_selected.connect(lambda _t: None)
        add.addWidget(self._name_box, 1)
        self._qty = QSpinBox()
        self._qty.setRange(1, 99999)
        self._qty.setValue(1)
        self._qty.setFixedWidth(70)
        self._qty.setToolTip("Units (items) or SCU (commodities)")
        add.addWidget(self._qty)
        self._btn_add = QPushButton("Add")
        self._btn_add.setCursor(Qt.PointingHandCursor)
        self._btn_add.setStyleSheet(_btn_ss(self._accent))
        self._btn_add.clicked.connect(self._add_clicked)
        add.addWidget(self._btn_add)
        bl.addLayout(add)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt;")
        bl.addWidget(self._status)

        # entries (also the drop target's visible frame)
        self._entries_host = QWidget()
        self._entries_lay = QVBoxLayout(self._entries_host)
        self._entries_lay.setContentsMargins(0, 0, 0, 0)
        self._entries_lay.setSpacing(0)
        self._entries_scroll = QScrollArea()
        self._entries_scroll.setWidgetResizable(True)
        self._entries_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._entries_scroll.setWidget(self._entries_host)
        self._entries_scroll.setMinimumHeight(140)
        self._set_drop_highlight(False)
        bl.addWidget(self._entries_scroll, 2)

        clear_row = QHBoxLayout()
        self._count = QLabel("")
        self._count.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt;")
        clear_row.addWidget(self._count, 1)
        btn_clear = QPushButton("Clear list")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.setStyleSheet(_btn_ss(self._accent))
        btn_clear.clicked.connect(self._list.clear)
        clear_row.addWidget(btn_clear)
        bl.addLayout(clear_row)

        # route options
        hdr = QLabel("ROUTE  -  Trade Hub basket planner")
        hdr.setStyleSheet(f"color: {self._accent}; font-family: Consolas; font-size: 8.5pt; font-weight: bold;")
        bl.addWidget(hdr)
        opt = QHBoxLayout()
        opt.setSpacing(6)
        opt.addWidget(_lbl("Start", P.fg_dim))
        self._start = SCFuzzyCombo(placeholder="(anywhere)", items=[])
        self._start.item_selected.connect(lambda _t: self._schedule_auto())
        opt.addWidget(self._start, 1)
        opt.addWidget(_lbl("Prefer", P.fg_dim))
        self._prefer = QComboBox()
        self._prefer.addItems(list(STRATEGIES))
        self._prefer.currentIndexChanged.connect(lambda _i: self._schedule_auto())
        opt.addWidget(self._prefer)
        bl.addLayout(opt)
        opt2 = QHBoxLayout()
        opt2.setSpacing(6)
        opt2.addWidget(_lbl("Terminals per entry", P.fg_dim))
        self._per = QSpinBox()
        self._per.setRange(1, PER_ENTRY_MAX)
        self._per.setValue(PER_ENTRY_DEFAULT)
        self._per.valueChanged.connect(lambda _v: self._schedule_auto())
        opt2.addWidget(self._per)
        self._auto = QCheckBox("Auto-calculate")
        self._auto.setChecked(True)
        self._auto.setStyleSheet(f"color: {P.fg}; font-family: Consolas; font-size: 8.5pt;")
        self._auto.toggled.connect(lambda on: on and self._schedule_auto())
        opt2.addWidget(self._auto)
        opt2.addStretch(1)
        self._btn_plan = QPushButton("Plan route")
        self._btn_plan.setCursor(Qt.PointingHandCursor)
        self._btn_plan.setStyleSheet(_btn_ss(self._accent))
        self._btn_plan.clicked.connect(self.plan_now)
        opt2.addWidget(self._btn_plan)
        bl.addLayout(opt2)

        # results
        self._results_host = QWidget()
        self._results_lay = QVBoxLayout(self._results_host)
        self._results_lay.setContentsMargins(0, 0, 0, 0)
        self._results_lay.setSpacing(6)
        self._results_lay.addStretch(1)
        rs = QScrollArea()
        rs.setWidgetResizable(True)
        rs.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        rs.setWidget(self._results_host)
        rs.setStyleSheet(f"QScrollArea {{ background: {P.bg_secondary}; border: 1px solid {P.border}; }}")
        bl.addWidget(rs, 3)

        self.set_kind("item")

    def _set_drop_highlight(self, on: bool) -> None:
        self._entries_scroll.setStyleSheet(
            f"QScrollArea {{ background: {P.bg_secondary}; border: "
            f"{'2px dashed ' + P.green if on else '1px solid ' + P.border}; }}")

    # kind + names
    def set_kind(self, kind: str) -> None:
        if kind not in KINDS:
            return
        self._kind = kind
        for k, b in self._kind_btns.items():
            b.setChecked(k == kind)
        self._name_box.set_items(sorted(self._names[kind]))
        self._name_box.set_text("")
        inp = getattr(self._name_box, "_input", None)
        if inp is not None:
            inp.setPlaceholderText("Item name..." if kind == "item" else "Commodity name...")
        self._qty.setSuffix(" SCU" if kind == "commodity" else "")
        if self.isVisible():
            self._load_names(kind)

    def _emit(self, signal, *args) -> None:
        """Emit from a worker thread; a panel closed meanwhile is not an error."""
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def load_names(self, kind: Optional[str] = None) -> None:
        """Load the name catalogue for *kind* (default: the selected kind) now,
        whether or not the panel is on screen."""
        self._load_names(kind or self._kind)

    def _load_names(self, kind: str) -> None:
        if self._names[kind] or kind in self._names_loading:
            return
        self._names_loading.add(kind)
        what = "item catalogue (Item Finder data)" if kind == "item" else \
            "commodities (Trade Hub data - first load can take a while)"
        self._set_status(f"Loading {what}...")

        def work() -> None:
            try:
                if kind == "item":
                    names: Dict[str, Optional[int]] = dict(self._source.item_catalog())
                else:
                    names = {n: None for n in self._source.commodity_names()}
            except Exception as exc:
                log.warning("shopping list: loading %s names failed", kind, exc_info=True)
                names = {}
                self._emit(self._sig.status, f"Could not load {kind} names: {exc}")
            self._emit(self._sig.names_ready, kind, names)
        threading.Thread(target=work, daemon=True, name=f"ShopNames-{kind}").start()

    def _on_names(self, kind: str, names) -> None:
        self._names_loading.discard(kind)
        self._names[kind] = dict(names or {})
        if kind == self._kind:
            self._name_box.set_items(sorted(self._names[kind]))
        self._refresh_starts()
        if self._names[kind]:
            self._set_status(f"{len(self._names[kind]):,} {kind} names loaded.")

    def _refresh_starts(self) -> None:
        try:
            self._start_map = self._source.start_terminals()
        except Exception:
            self._start_map = {}
        cur = self._start.current_text()
        self._start.set_items(sorted(self._start_map))
        if cur:
            self._start.set_text(cur)

    # list
    def add_entry(self, kind: str, name: str, qty: int = 1) -> bool:
        """Add one entry by name (also the API the tests drive). Once the catalogue
        for that kind has loaded, a name must be in it."""
        name = (name or "").strip()
        if not name:
            self._set_status("Type or pick a name first.")
            return False
        known = self._names.get(kind) or {}
        if known and name not in known:
            match = next((n for n in known if n.casefold() == name.casefold()), None)
            if match is None:
                self._set_status(f"'{name}' is not a known {kind} name.")
                return False
            name = match
        self._list.add(kind, name, qty, item_id=known.get(name) if kind == "item" else None)
        self._set_status(f"Added {name} ({kind}).")
        return True

    def add_item(self, item: dict) -> bool:
        """Add an item dict - a dragged Item Finder row or a Star Map item pop-out.
        One that names a location is pinned there. Already listed: nothing is added
        twice, and the status line says so."""
        from .shopping_list import item_to_entry_args
        args = item_to_entry_args(item)
        if args is None:
            self._set_status("That is not an item I can add.")
            return False
        already = self._list.find("item", args["name"]) is not None
        self._list.add_item(item)
        where = f", pinned to {args['pin']['location']}" if args["pin"] else ""
        self._set_status((f"{args['name']} is already on the list" if already
                          else f"Added {args['name']} (item)") + where + ".")
        return True

    def _add_clicked(self) -> None:
        if self.add_entry(self._kind, self._name_box.current_text(), self._qty.value()):
            self._name_box.set_text("")
            self._qty.setValue(1)

    def _poll_file(self) -> None:
        self._list.refresh()             # notifies (-> _on_list_changed) only if it changed

    def _on_list_changed(self) -> None:
        self._render_entries()
        if not len(self._list):
            self._plans = []
            self._offers = {}
            self._clear_results()
            self._drop_map_route()
            return
        self._schedule_auto()

    def _toggle_offers(self, label: str) -> None:
        self._expanded.symmetric_difference_update({label})
        self._render_entries()

    def _render_entries(self) -> None:
        while self._entries_lay.count():
            it = self._entries_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        self._rows = {}
        entries = self._list.entries()
        for e in entries:
            lbl = entry_label(e.kind, e.name)
            row = _EntryRow(
                e, self._offers.get(lbl), lbl in self._expanded,
                on_remove=lambda _c=False, k=e.kind, n=e.name: self._list.remove(k, n),
                on_toggle=lambda l=lbl: self._toggle_offers(l),
                on_unpin=lambda k=e.kind, n=e.name: self._list.set_pin(k, n, None),
                parent=self._entries_host)
            self._rows[lbl] = row
            self._entries_lay.addWidget(row)
        if not entries:
            hint = QLabel("Empty. Pick Item or Commodity, choose a name, then Add -\n"
                          "or drag an item here from Item Finder or the Star Map.")
            hint.setAlignment(Qt.AlignCenter)
            hint.setWordWrap(True)
            hint.setStyleSheet(f"color: {P.fg_disabled}; font-family: Consolas; font-size: 8.5pt; padding: 18px;")
            self._entries_lay.addWidget(hint)
        self._entries_lay.addStretch(1)
        n_i = len(self._list.of_kind("item"))
        n_c = len(self._list.of_kind("commodity"))
        self._count.setText(f"{n_i} item(s), {n_c} commodity(ies)")

    def row(self, kind: str, name: str) -> Optional[_EntryRow]:
        return self._rows.get(entry_label(kind, name))

    # planning
    def _schedule_auto(self) -> None:
        if not (self._auto.isChecked() and len(self._list)):
            return
        if not self.isVisible() and self._map_live is None:
            self._stale = True           # nobody is looking and nothing is drawn: plan on show
            return
        self._auto_timer.start()

    def plan_now(self) -> None:
        entries = self._list.entries()
        if not entries:
            self._clear_results()
            self._set_status("Add something to the list first.")
            return
        self._req += 1
        req = self._req
        start_name = (self._start.current_text() or "").strip()
        start_id = self._start_map.get(start_name) if start_name else None
        prefer = self._prefer.currentText()
        per = self._per.value()
        self._btn_plan.setEnabled(False)
        self._set_status("Planning: loading prices...")

        def work() -> None:
            try:
                need_c = any(e.kind == "commodity" for e in entries)
                routes = self._source.routes() if need_c else []
                prices = self._source.item_prices(entries)
                self._emit(self._sig.status, "Planning: fetching terminal distances...")
                plans, pi = plan_routes(
                    entries, routes, prices, self._source.dist_cache(),
                    start_terminal_id=start_id, per_entry_limit=per, prefer=prefer,
                    on_progress=lambda d, t: self._emit(
                        self._sig.status, f"Planning: distances {d}/{t}..."))
                self._emit(self._sig.plan_done, plans, pi, req)
            except Exception as exc:
                log.exception("shopping list: planning failed")
                self._emit(self._sig.plan_failed, f"{type(exc).__name__}: {exc}", req)
        threading.Thread(target=work, daemon=True, name="ShopPlan").start()

    def _on_plan_failed(self, msg: str, req: int) -> None:
        if req != self._req:
            return
        self._btn_plan.setEnabled(True)
        self._set_status("Plan failed: " + msg)

    def _on_plan_done(self, plans, pi, req: int) -> None:
        if req != self._req:
            return                       # a newer request superseded this one
        self._btn_plan.setEnabled(True)
        self._plans = list(plans or [])
        self._offers = dict(getattr(pi, "offers", None) or {})
        self._render_entries()           # the entries can now say where they are sold
        self.render_plans(self._plans, pi)
        self._follow_on_map()

    def _clear_results(self) -> None:
        while self._results_lay.count() > 1:
            it = self._results_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()

    def render_plans(self, plans, pi) -> None:
        self._clear_results()
        def names(labels):
            return ", ".join(split_label(lbl)[1] + f" ({split_label(lbl)[0]})" for lbl in labels)
        missing = names(pi.no_offers if pi else [])
        unpinned = names(getattr(pi, "pin_missed", None) or [])
        if not plans:
            msg = "No route could be built."
            if missing:
                msg += " Nothing sells: " + missing
            self._set_status(msg)
            return
        for i, plan in enumerate(plans):
            self._results_lay.insertWidget(self._results_lay.count() - 1, self._plan_card(i, plan))
        msg = f"{len(plans)} route option(s), planned with Trade Hub's basket planner."
        if missing:
            msg += " No terminal sells: " + missing + "."
        if unpinned:
            msg += " Not sold at its pinned place, so planned anywhere: " + unpinned + "."
        self._set_status(msg)

    def _plan_card(self, idx: int, plan) -> QWidget:
        card = QFrame()
        card.setObjectName("shopPlanCard")
        card.setStyleSheet(f"#shopPlanCard {{ background: {P.bg_card}; border: 1px solid "
                           f"{self._accent if idx == 0 else P.border}; border-radius: 4px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(3)
        top = QHBoxLayout()
        dist = plan.total_distance_gm
        head = QLabel(f"{plan.label or 'ROUTE'}   {len(plan.stops)} stop(s)   "
                      f"{dist:,.1f} Gm" if dist else f"{plan.label or 'ROUTE'}   {len(plan.stops)} stop(s)")
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {self._accent}; font-family: Consolas; font-size: 9pt; font-weight: bold; "
                           f"background: transparent;")
        top.addWidget(head, 1)
        if self._on_show_on_map is not None:
            b = QPushButton("Show on Star Map")
            b.setCursor(Qt.PointingHandCursor)
            b.setToolTip("Draw this route on the star map. It then follows the list as you change it.")
            b.setStyleSheet(_btn_ss(self._accent))
            b.clicked.connect(lambda _c=False, p=plan: self.show_on_map(p))
            top.addWidget(b)
        lay.addLayout(top)
        for line in plan_summary(plan):
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {P.fg}; font-family: Consolas; font-size: 8.5pt; background: transparent;")
            lay.addWidget(lbl)
        foot = f"unit prices total {plan_cost(plan):,.0f} aUEC"
        if plan.unresolved:
            foot += "   not covered: " + ", ".join(split_label(u)[1] for u in plan.unresolved)
        f = QLabel(foot)
        f.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
        lay.addWidget(f)
        return card

    def plans(self) -> List[object]:
        return list(self._plans)

    # the route on the map follows the list
    def show_on_map(self, plan) -> None:
        """Draw *plan* on the star map and keep it in step with the list from now on.

        The host's callback is ``on_show_on_map(plan, auto)``: *auto* is False for
        this click and True when a re-plan redraws a route already on the map (a
        host should then leave the map alone if the user has cleared that route,
        and say so through :meth:`map_route_cleared`)."""
        if self._on_show_on_map is None or plan is None:
            return
        self._map_live = getattr(plan, "label", "") or "ROUTE"
        self._on_show_on_map(plan, False)

    def _follow_on_map(self) -> None:
        """A re-plan landed: if a route is on the map, redraw it (same strategy if
        the planner still offers it, else the first plan)."""
        if self._map_live is None or self._on_show_on_map is None:
            return
        if not self._plans:
            self._drop_map_route()
            return
        plan = next((p for p in self._plans if getattr(p, "label", "") == self._map_live),
                    self._plans[0])
        self._on_show_on_map(plan, True)

    def _drop_map_route(self) -> None:
        if self._map_live is None:
            return
        self._map_live = None
        if self._on_clear_map is not None:
            self._on_clear_map()

    def map_route_cleared(self) -> None:
        """The host cleared the route on the map itself: stop following."""
        self._map_live = None

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def status(self) -> str:
        return self._status.text()

    # visibility: only watch the file while someone is looking
    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._list.refresh()
        self._refresh_timer.start()
        self._load_names(self._kind)
        if self._stale or (len(self._list) and not self._plans):
            self._stale = False
            self._schedule_auto()

    def hideEvent(self, event) -> None:
        self._refresh_timer.stop()
        super().hideEvent(event)

    # drag & drop (an Item Finder row, a Star Map item pop-out)
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
        if self.add_payload(bytes(md.data(SC_ITEM_MIME))):
            event.acceptProposedAction()
        else:
            event.ignore()

    def add_payload(self, raw: bytes) -> bool:
        """Add the item carried by an SC_ITEM_MIME drag payload."""
        try:
            item = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return False
        return isinstance(item, dict) and self.add_item(item)


class _DragBar(QWidget):
    def __init__(self, window: QWidget, on_close: Callable[[], None], accent: str) -> None:
        super().__init__(window)
        self._window = window
        self._drag: Optional[QPoint] = None
        self.setFixedHeight(30)
        self.setStyleSheet(f"background: {P.bg_header};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        t = QLabel("\U0001f6d2  SHOPPING LIST")
        t.setStyleSheet(f"color: {accent}; font-family: Electrolize, Consolas; font-size: 10pt; "
                        f"font-weight: bold; letter-spacing: 2px; background: transparent;")
        lay.addWidget(t)
        lay.addStretch(1)
        x = QPushButton("x")
        x.setFixedWidth(24)
        x.setCursor(Qt.PointingHandCursor)
        x.setToolTip("Close (your list is kept)")
        x.setStyleSheet(f"QPushButton {{ color: {P.fg_dim}; background: transparent; border: none; "
                        f"font-family: Consolas; font-size: 11pt; font-weight: bold; }} "
                        f"QPushButton:hover {{ color: #ff5566; }}")
        x.clicked.connect(on_close)
        lay.addWidget(x)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.LeftButton:
            self._drag = e.globalPosition().toPoint() - self._window.pos()
            e.accept()

    def mouseMoveEvent(self, e) -> None:
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self._window.move(e.globalPosition().toPoint() - self._drag)
            e.accept()

    def mouseReleaseEvent(self, e) -> None:
        self._drag = None


class ShoppingListWindow(QWidget):
    """The shared list as a frameless, always-on-top pop-out (``.panel`` is the list)."""

    def __init__(self, shopping: ShoppingList, source,
                 on_show_on_map: Optional[Callable[[object], None]] = None,
                 on_clear_map: Optional[Callable[[], None]] = None,
                 accent: str = ACCENT, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setWindowTitle("SC Toolbox - Shopping List")
        self.resize(520, 640)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("shopFrame")
        frame.setStyleSheet(f"#shopFrame {{ background: {P.bg_primary}; border: 1px solid {accent}; }}")
        outer.addWidget(frame)
        root = QVBoxLayout(frame)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        root.addWidget(_DragBar(self, self.hide, accent))
        self.panel = ShoppingListPanel(shopping, source, on_show_on_map=on_show_on_map,
                                       on_clear_map=on_clear_map, accent=accent, parent=frame)
        root.addWidget(self.panel, 1)

    def position_beside(self, anchor: QWidget) -> None:
        """Just right of *anchor* (left if there is no room), kept on screen."""
        from PySide6.QtGui import QGuiApplication
        geo = anchor.frameGeometry()
        x, y = geo.right() + 12, geo.top()
        screen = QGuiApplication.primaryScreen()
        if screen:
            avail = screen.availableGeometry()
            if x + self.width() > avail.right():
                x = geo.right() - self.width() - 12       # no room outside: sit inside its right edge
                y = geo.top() + 74
            x = max(avail.left() + 8, min(x, avail.right() - self.width() - 8))
            y = max(avail.top() + 8, min(y, avail.bottom() - self.height() - 8))
        self.move(x, y)
