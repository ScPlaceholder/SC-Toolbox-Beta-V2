# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""The Shopping List pop-out: add items and commodities, get routes.

A frameless, always-on-top window opened from the button beside the tabs.
Everything that can touch the network runs on a worker thread and reports
back through Qt signals; the UI thread only renders.

Route options ("choice parameters"):
  * Start at  - optional starting terminal (distance of the first leg)
  * Prefer    - which of the planner's strategies to list first
  * Terminals per entry - how many of each entry's cheapest terminals the
                planner may choose between (more = better routes, more
                distance look-ups)
  * Auto      - re-plan automatically whenever the list or an option changes

The planning itself is Trade Hub's basket planner; see shopping_list.py.
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import QObject, QPoint, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSpinBox, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.fuzzy_combo import SCFuzzyCombo

from .shopping_list import (
    KINDS, PER_ENTRY_DEFAULT, PER_ENTRY_MAX, STRATEGIES, ShoppingList,
    plan_routes, plan_summary, split_label,
)
from .shopping_source import ShoppingSource

log = logging.getLogger(__name__)

ACCENT = "#55ddaa"
_KIND_LABEL = {"item": "ITEM", "commodity": "CMDTY"}


def _btn_ss(color: str = ACCENT) -> str:
    return (f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; border: 1px solid {P.border}; "
            f"padding: 4px 10px; font-family: Consolas; font-size: 9pt; }} "
            f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {color}; }} "
            f"QPushButton:checked {{ background: {color}; color: #04140d; border-color: {color}; "
            f"font-weight: bold; }} "
            f"QPushButton:disabled {{ color: {P.fg_disabled}; }}")


class _Signals(QObject):
    names_ready = Signal(str, object)      # kind, {name: id-or-None}
    plan_done = Signal(object, object, int)  # plans, PlanInput, request id
    plan_failed = Signal(str, int)
    status = Signal(str)


class _EntryRow(QWidget):
    def __init__(self, kind: str, name: str, qty: int, on_remove: Callable[[], None],
                 parent: QWidget) -> None:
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(8, 3, 6, 3)
        lay.setSpacing(8)
        tag = QLabel(_KIND_LABEL.get(kind, kind.upper()))
        tag.setFixedWidth(46)
        color = P.tool_market if kind == "item" else P.tool_trade
        tag.setStyleSheet(f"color: {color}; font-family: Consolas; font-size: 7.5pt; "
                          f"font-weight: bold; background: transparent;")
        lay.addWidget(tag)
        nm = QLabel(name)
        nm.setStyleSheet(f"color: {P.fg}; font-family: Consolas; font-size: 9pt; background: transparent;")
        lay.addWidget(nm, 1)
        q = QLabel(f"x{qty}" + (" SCU" if kind == "commodity" else ""))
        q.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
        lay.addWidget(q)
        rm = QPushButton("x")
        rm.setFixedWidth(22)
        rm.setCursor(Qt.PointingHandCursor)
        rm.setToolTip("Remove from the list")
        rm.setStyleSheet(f"QPushButton {{ color: {P.fg_dim}; background: transparent; border: none; "
                         f"font-family: Consolas; font-weight: bold; }} "
                         f"QPushButton:hover {{ color: #ff5566; }}")
        rm.clicked.connect(on_remove)
        lay.addWidget(rm)


class _DragBar(QWidget):
    def __init__(self, window: QWidget, on_close: Callable[[], None]) -> None:
        super().__init__(window)
        self._window = window
        self._drag: Optional[QPoint] = None
        self.setFixedHeight(30)
        self.setStyleSheet(f"background: {P.bg_header};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 0, 6, 0)
        t = QLabel("\U0001f6d2  SHOPPING LIST")
        t.setStyleSheet(f"color: {ACCENT}; font-family: Electrolize, Consolas; font-size: 10pt; "
                        f"font-weight: bold; letter-spacing: 2px; background: transparent;")
        lay.addWidget(t)
        lay.addStretch(1)
        x = QPushButton("x")
        x.setFixedWidth(24)
        x.setCursor(Qt.PointingHandCursor)
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


class ShoppingPopout(QWidget):
    """The shared shopping list window."""

    def __init__(self, shopping: ShoppingList, source: ShoppingSource,
                 on_show_on_map: Optional[Callable[[object], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent, Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
                         | Qt.Tool)
        self.setWindowTitle("Everything Finder - Shopping List")
        self.resize(520, 640)
        self._list = shopping
        self._source = source
        self._on_show_on_map = on_show_on_map
        self._names: Dict[str, Dict[str, Optional[int]]] = {"item": {}, "commodity": {}}
        self._names_loading: set = set()
        self._kind = "item"
        self._req = 0
        self._plans: List[object] = []
        self._sig = _Signals(self)
        self._sig.names_ready.connect(self._on_names)
        self._sig.plan_done.connect(self._on_plan_done)
        self._sig.plan_failed.connect(self._on_plan_failed)
        self._sig.status.connect(self._set_status)

        self._auto_timer = QTimer(self)
        self._auto_timer.setSingleShot(True)
        self._auto_timer.setInterval(800)
        self._auto_timer.timeout.connect(self.plan_now)

        self._build()
        self._list.subscribe(self._on_list_changed)
        self._render_entries()

    # construction
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        frame = QFrame()
        frame.setObjectName("efShopFrame")
        frame.setStyleSheet(f"#efShopFrame {{ background: {P.bg_primary}; border: 1px solid {ACCENT}; }}")
        outer.addWidget(frame)
        root = QVBoxLayout(frame)
        root.setContentsMargins(1, 1, 1, 1)
        root.setSpacing(0)
        root.addWidget(_DragBar(self, self.close))

        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(10, 8, 10, 10)
        bl.setSpacing(7)
        root.addWidget(body, 1)

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
        self._btn_add.setStyleSheet(_btn_ss())
        self._btn_add.clicked.connect(self._add_clicked)
        add.addWidget(self._btn_add)
        bl.addLayout(add)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt;")
        bl.addWidget(self._status)

        # entries
        self._entries_host = QWidget()
        self._entries_lay = QVBoxLayout(self._entries_host)
        self._entries_lay.setContentsMargins(0, 0, 0, 0)
        self._entries_lay.setSpacing(0)
        sc = QScrollArea()
        sc.setWidgetResizable(True)
        sc.setWidget(self._entries_host)
        sc.setMinimumHeight(140)
        sc.setStyleSheet(f"QScrollArea {{ background: {P.bg_secondary}; border: 1px solid {P.border}; }}")
        bl.addWidget(sc, 2)

        clear_row = QHBoxLayout()
        self._count = QLabel("")
        self._count.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt;")
        clear_row.addWidget(self._count, 1)
        btn_clear = QPushButton("Clear list")
        btn_clear.setCursor(Qt.PointingHandCursor)
        btn_clear.setStyleSheet(_btn_ss())
        btn_clear.clicked.connect(self._list.clear)
        clear_row.addWidget(btn_clear)
        bl.addLayout(clear_row)

        # route options
        hdr = QLabel("ROUTE  -  Trade Hub basket planner")
        hdr.setStyleSheet(f"color: {ACCENT}; font-family: Consolas; font-size: 8.5pt; font-weight: bold;")
        bl.addWidget(hdr)
        opt = QHBoxLayout()
        opt.setSpacing(6)
        opt.addWidget(self._small("Start"))
        self._start = SCFuzzyCombo(placeholder="(anywhere)", items=[])
        self._start.item_selected.connect(lambda _t: self._schedule_auto())
        opt.addWidget(self._start, 1)
        opt.addWidget(self._small("Prefer"))
        self._prefer = QComboBox()
        self._prefer.addItems(list(STRATEGIES))
        self._prefer.currentIndexChanged.connect(lambda _i: self._schedule_auto())
        opt.addWidget(self._prefer)
        bl.addLayout(opt)
        opt2 = QHBoxLayout()
        opt2.setSpacing(6)
        opt2.addWidget(self._small("Terminals per entry"))
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
        self._btn_plan.setStyleSheet(_btn_ss())
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

    @staticmethod
    def _small(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt;")
        return lbl

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
        self._load_names(kind)

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
                log.warning("Everything Finder: loading %s names failed", kind, exc_info=True)
                names = {}
                self._sig.status.emit(f"Could not load {kind} names: {exc}")
            self._sig.names_ready.emit(kind, names)
        threading.Thread(target=work, daemon=True, name=f"EFNames-{kind}").start()

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
        """Add one entry (also the API the tests drive). Unknown names are allowed
        once the catalogue for that kind has loaded only if they are in it."""
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

    def _add_clicked(self) -> None:
        if self.add_entry(self._kind, self._name_box.current_text(), self._qty.value()):
            self._name_box.set_text("")
            self._qty.setValue(1)

    def _on_list_changed(self) -> None:
        self._render_entries()
        self._schedule_auto()

    def _render_entries(self) -> None:
        while self._entries_lay.count():
            it = self._entries_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()
        entries = self._list.entries()
        for e in entries:
            self._entries_lay.addWidget(_EntryRow(
                e.kind, e.name, e.qty,
                on_remove=lambda _c=False, k=e.kind, n=e.name: self._list.remove(k, n),
                parent=self._entries_host))
        if not entries:
            hint = QLabel("Empty. Pick Item or Commodity, choose a name, then Add.")
            hint.setAlignment(Qt.AlignCenter)
            hint.setStyleSheet(f"color: {P.fg_disabled}; font-family: Consolas; font-size: 8.5pt; padding: 18px;")
            self._entries_lay.addWidget(hint)
        self._entries_lay.addStretch(1)
        n_i = len(self._list.of_kind("item"))
        n_c = len(self._list.of_kind("commodity"))
        self._count.setText(f"{n_i} item(s), {n_c} commodity(ies)")

    # planning
    def _schedule_auto(self) -> None:
        if self._auto.isChecked() and len(self._list):
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
        start_id = getattr(self, "_start_map", {}).get(start_name) if start_name else None
        prefer = self._prefer.currentText()
        per = self._per.value()
        self._btn_plan.setEnabled(False)
        self._set_status("Planning: loading prices...")

        def work() -> None:
            try:
                need_c = any(e.kind == "commodity" for e in entries)
                routes = self._source.routes() if need_c else []
                prices = self._source.item_prices(entries)
                self._sig.status.emit("Planning: fetching terminal distances...")
                plans, pi = plan_routes(
                    entries, routes, prices, self._source.dist_cache(),
                    start_terminal_id=start_id, per_entry_limit=per, prefer=prefer,
                    on_progress=lambda d, t: self._sig.status.emit(
                        f"Planning: distances {d}/{t}..."))
                self._sig.plan_done.emit(plans, pi, req)
            except Exception as exc:
                log.exception("Everything Finder: planning failed")
                self._sig.plan_failed.emit(f"{type(exc).__name__}: {exc}", req)
        threading.Thread(target=work, daemon=True, name="EFPlan").start()

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
        self.render_plans(self._plans, pi)

    def _clear_results(self) -> None:
        while self._results_lay.count() > 1:
            it = self._results_lay.takeAt(0)
            if it.widget():
                it.widget().hide()
                it.widget().deleteLater()

    def render_plans(self, plans, pi) -> None:
        self._clear_results()
        missing = [split_label(lbl)[1] + f" ({split_label(lbl)[0]})" for lbl in (pi.no_offers if pi else [])]
        if not plans:
            msg = "No route could be built."
            if missing:
                msg += " Nothing sells: " + ", ".join(missing)
            self._set_status(msg)
            return
        for i, plan in enumerate(plans):
            self._results_lay.insertWidget(self._results_lay.count() - 1, self._plan_card(i, plan))
        msg = f"{len(plans)} route option(s), planned with Trade Hub's basket planner."
        if missing:
            msg += " No terminal sells: " + ", ".join(missing)
        self._set_status(msg)

    def _plan_card(self, idx: int, plan) -> QWidget:
        card = QFrame()
        card.setObjectName("efPlanCard")
        card.setStyleSheet(f"#efPlanCard {{ background: {P.bg_card}; border: 1px solid "
                           f"{ACCENT if idx == 0 else P.border}; border-radius: 4px; }}")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(10, 6, 10, 8)
        lay.setSpacing(3)
        top = QHBoxLayout()
        dist = plan.total_distance_gm
        head = QLabel(f"{plan.label or 'ROUTE'}   {len(plan.stops)} stop(s)   "
                      f"{dist:,.1f} Gm" if dist else f"{plan.label or 'ROUTE'}   {len(plan.stops)} stop(s)")
        head.setWordWrap(True)
        head.setStyleSheet(f"color: {ACCENT}; font-family: Consolas; font-size: 9pt; font-weight: bold; "
                           f"background: transparent;")
        top.addWidget(head, 1)
        if self._on_show_on_map is not None:
            b = QPushButton("Show on Star Map")
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(_btn_ss())
            b.clicked.connect(lambda _c=False, p=plan: self._on_show_on_map(p))
            top.addWidget(b)
        lay.addLayout(top)
        cost = sum(o.price_buy for s in plan.stops for o in s.picks)
        for line in plan_summary(plan):
            lbl = QLabel(line)
            lbl.setWordWrap(True)
            lbl.setStyleSheet(f"color: {P.fg}; font-family: Consolas; font-size: 8.5pt; background: transparent;")
            lay.addWidget(lbl)
        foot = f"unit prices total {cost:,.0f} aUEC"
        if plan.unresolved:
            foot += "   not covered: " + ", ".join(split_label(u)[1] for u in plan.unresolved)
        f = QLabel(foot)
        f.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; background: transparent;")
        lay.addWidget(f)
        return card

    def plans(self) -> List[object]:
        return list(self._plans)

    def _set_status(self, text: str) -> None:
        self._status.setText(text)

    def position_beside(self, anchor: QWidget) -> None:
        g = anchor.frameGeometry()
        self.move(g.right() - self.width() - 12, g.top() + 74)
