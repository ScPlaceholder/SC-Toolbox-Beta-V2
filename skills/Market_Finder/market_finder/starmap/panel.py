"""MarketMapPanel — embeds the star map in Market Finder.

Adapted from the Trade Hub's StarMapPanel: same galaxy -> system -> planet
scene navigation, home system, lore bubbles on right-click, and route
plotting, but the click-through answers Market Finder's question — *what
items sell at this location?* — via the terminal->items index, and item
rows pop out draggable detail bubbles that feed the Grocery List.

Styling uses Market Finder's accent (P.tool_market) rather than Trade Hub's.

Everything is defensive: if data/scene construction fails the panel shows
an inline message instead of letting a star-map error break Market Finder.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.fuzzy_combo import SCFuzzyCombo

from .data import (Galaxy, HOME_CHOICES, LOC_ALIASES, load_bodies, load_state,
                   norm_loc, save_state)
from .galaxy_view import GalaxyView
from .planet_system_view import PlanetSystemView
from .planet_view import PlanetView
from .system_view import SystemView
from .items_index import ItemsIndexLoader, items_at
from .items_dialog import ItemsDialog
from .item_popout import ItemPopOut
from .lore import LoreBubble, LoreFetcher
from .ui import make_close_button

ACCENT = P.tool_market


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 5px 14px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }}"
    )


class MarketMapPanel(QWidget):
    """Star map + items-at-location browsing for Market Finder."""

    def __init__(self, data_service,
                 on_add_to_grocery: Optional[Callable[[dict], None]] = None,
                 parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._data = data_service
        self._on_add_to_grocery = on_add_to_grocery
        self._lore_bubble: Optional[LoreBubble] = None
        self._lore_fetcher = LoreFetcher()
        self._lore_fetcher.done.connect(self._on_lore_done)
        self._galaxy: Optional[GalaxyView] = None
        self._galaxy_data: Optional[Galaxy] = None
        self._bodies = {}
        self._items_index: Dict[tuple, list] = {}
        self._items_by_id: Dict[int, dict] = {}
        self._items_dlg: Optional[ItemsDialog] = None
        self._popouts: List[ItemPopOut] = []
        self._restored = False
        self._nav: List[Tuple[str, QWidget]] = []

        # Terminal -> items index, built off-thread from UEX items_prices_all.
        self._index_loader = ItemsIndexLoader(getattr(data_service, "_api", None))
        self._index_loader.done.connect(self._on_index_done)
        QTimer.singleShot(1000, self._poll_data_ready)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        try:
            self._build_ui(root)
        except Exception as exc:   # never break Market Finder over the star map
            msg = QLabel(f"Star Map unavailable:\n{exc}")
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet(f"color: {P.fg_dim}; font-size: 10pt; padding: 24px;")
            root.addWidget(msg, 1)

    # ── construction ────────────────────────────────────────────────────────
    def _build_ui(self, root: QVBoxLayout) -> None:
        self._galaxy_data = Galaxy.load()
        self._bodies = load_bodies()

        self._galaxy = GalaxyView(self._galaxy_data)
        self._galaxy.apply_state(load_state().get("galaxy"))
        self._galaxy.viewChanged.connect(self._save_soon)
        self._galaxy.systemEntered.connect(self._enter_system)
        self._galaxy.drillIn.connect(self._enter_system)
        self._galaxy.loreRequested.connect(self._show_lore)
        self._galaxy.routePlotted.connect(lambda *_: self._btn_route.setText("Clear route"))

        self._stack = QStackedWidget()
        self._stack.addWidget(self._galaxy)
        root.addWidget(self._build_controls())
        root.addWidget(self._build_navbar())
        root.addWidget(self._stack, 1)
        self._nav = [("Galaxy", self._galaxy)]
        self._update_navbar()

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(600)
        self._save_timer.timeout.connect(self.save_state)

    def _build_controls(self) -> QWidget:
        w = QWidget()
        w.setStyleSheet(f"background: {P.bg_header};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(8)

        self._btn_home = QPushButton("⌂ Home")
        self._btn_home.setCursor(Qt.PointingHandCursor)
        self._btn_home.setStyleSheet(_btn_ss())
        self._btn_home.setToolTip("Left-click: snap to home · Right-click: change home")
        self._btn_home.clicked.connect(self._home_clicked)
        self._btn_home.setContextMenuPolicy(Qt.CustomContextMenu)
        self._btn_home.customContextMenuRequested.connect(self._home_menu)

        self._btn_route = QPushButton("⤳ Route")
        self._btn_route.setCursor(Qt.PointingHandCursor)
        self._btn_route.setStyleSheet(_btn_ss())
        self._btn_route.setToolTip("Plot a jump route: click a start system, then a destination")
        self._btn_route.clicked.connect(self._route_clicked)

        self._search = SCFuzzyCombo(placeholder="Search system or location…",
                                    items=self._all_place_names())
        self._search.item_selected.connect(self.goto)

        self._src_lbl = QLabel("")
        self._src_lbl.setStyleSheet(
            f"color: {P.fg_disabled}; font-family: Consolas; font-size: 7pt;")
        self._src_lbl.setToolTip("Source of the items-sold-here data")

        lay.addWidget(self._btn_home)
        lay.addWidget(self._btn_route)
        lay.addWidget(self._search, 1)
        lay.addWidget(self._src_lbl)
        return w

    def _build_navbar(self) -> QWidget:
        self._navbar = QWidget()
        self._navbar.setStyleSheet(f"background: {P.bg_secondary};")
        lay = QHBoxLayout(self._navbar)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(8)
        self._btn_back = QPushButton("‹ Back")
        self._btn_back.setCursor(Qt.PointingHandCursor)
        self._btn_back.setStyleSheet(_btn_ss())
        self._btn_back.clicked.connect(self._go_back)
        self._crumb = QLabel("")
        self._crumb.setStyleSheet(f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt;")
        lay.addWidget(self._btn_back)
        lay.addWidget(self._crumb)
        lay.addStretch(1)
        return self._navbar

    # ── items index ───────────────────────────────────────────────────────────

    def _poll_data_ready(self) -> None:
        """Re-sync item metadata once the DataService finishes loading.

        The items index often completes BEFORE DataService.items is
        populated (one bulk request vs. the full multi-fetch load); the
        refresh_items() inside _on_index_done then caches an empty
        id->item map and every location dialog comes up empty forever.
        Poll until is_loaded() flips, then refresh + refill the dialog.
        """
        try:
            loaded = bool(getattr(self._data, "is_loaded", lambda: True)())
        except Exception:
            loaded = True
        if loaded:
            self.refresh_items()
            dlg = self._items_dlg
            if dlg is not None:
                try:
                    if dlg.isVisible():
                        dlg.refill()
                except RuntimeError:
                    self._items_dlg = None
            return
        QTimer.singleShot(1000, self._poll_data_ready)

    def ensure_index(self) -> None:
        """Kick off the (once) terminal->items index build. Safe to call often."""
        try:
            self._index_loader.start()
        except Exception:
            pass

    def _on_index_done(self, index: dict, source: str) -> None:
        self._items_index = index or {}
        self.refresh_items()
        self._src_lbl.setText({"live": "UEX live", "cache": "UEX cache",
                               "session": "session data"}.get(source, ""))
        # The index can finish after an ItemsDialog was already opened (the
        # dialog then rendered "no item data"); give it a fresh resolve.
        dlg = self._items_dlg
        if dlg is not None:
            try:
                if dlg.isVisible():
                    dlg.refill()
            except RuntimeError:
                self._items_dlg = None

    def refresh_items(self) -> None:
        """Data (re)loaded — refresh item names and terminal badges."""
        self._items_by_id = {it.get("id"): it for it in (getattr(self._data, "items", None) or [])
                             if it.get("id") is not None}
        names = self._terminal_names()
        for _lbl, w in self._nav:
            if hasattr(w, "_terminals"):
                w._terminals = names
                w.update()

    def _terminal_names(self) -> set:
        """Normalised names of every UEX terminal, so map bodies that host one
        get a terminal badge + click (names diverge: Grim HEX vs the UEX
        Green Imperial Housing Exchange, etc.)."""
        names = set()
        for t in (getattr(self._data, "terminals", None) or {}).values():
            for f in ("city_name", "space_station_name", "moon_name", "planet_name",
                      "nickname", "name"):
                v = t.get(f)
                if v:
                    n = norm_loc(str(v))
                    if n:
                        names.add(LOC_ALIASES.get(n, n))
        return names

    def _rows_for(self, location: str, system_code: str) -> List[dict]:
        """Resolve a map body to its sold-items rows (item dict + price)."""
        sysname = ""
        if self._galaxy_data is not None:
            s = self._galaxy_data.get(system_code)
            sysname = s.name if s else ""
        entries = items_at(self._items_index, location, sysname)
        if not entries:
            # Lenient retry: UEX terminal names sometimes differ from the map
            # body names beyond formatting, so also accept guarded substring
            # matches of the normalised names within this system.
            body_n = norm_loc(location)
            for (sys_n, body_key), rows in self._items_index.items():
                if sys_n != norm_loc(sysname) or not body_n or not body_key:
                    continue
                if body_n in body_key or (len(body_key) >= 4 and body_key in body_n):
                    merged = {e["item_id"]: e for e in entries}
                    for e in rows:
                        cur = merged.get(e["item_id"])
                        if cur is None or (0 < e["price_buy"] < (cur["price_buy"] or 1e18)):
                            merged[e["item_id"]] = dict(e)
                    entries = list(merged.values())
        rows = []
        for e in entries:
            item = self._items_by_id.get(e["item_id"])
            if item is not None:
                rows.append({"item": item, "price": e["price_buy"]})
        rows.sort(key=lambda r: ((r["price"] or 0) <= 0, r["price"] or 1e18,
                                 str(r["item"].get("name") or "")))
        return rows

    # ── scene navigation ──────────────────────────────────────────────────────
    def _enter_system(self, code: str) -> None:
        s = self._galaxy_data.get(code) if self._galaxy_data else None
        name = s.name if s else code
        try:
            view = SystemView(code, name, self._bodies.get(code.upper(), []),
                              terminal_names=self._terminal_names())
        except Exception:
            return
        view.planetEntered.connect(lambda pn, c=code: self._enter_neighborhood(c, pn))
        view.drillIn.connect(lambda pn, c=code: self._enter_neighborhood(c, pn))
        view.drillOut.connect(self._go_back)
        view.terminalClicked.connect(self._open_location)
        view.jumpRequested.connect(self._jump_to_system)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        self._push(f"{name.upper()} system", view)

    def _on_body_activated(self, name: str, code: str) -> None:
        if name == "CIG Headquarters":
            self._show_cig_hq()

    def _show_cig_hq(self) -> None:
        """Easter egg: CIG HQ on Earth (Sol) pops a Buy more ships bubble."""
        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        dlg.setAttribute(Qt.WA_TranslucentBackground)
        dlg.setModal(False)
        outer = QVBoxLayout(dlg); outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(f"background:{P.bg_card}; border:2px solid {ACCENT}; border-radius:12px;")
        outer.addWidget(card)
        lay = QVBoxLayout(card); lay.setContentsMargins(28, 14, 28, 22); lay.setSpacing(10)
        toprow = QHBoxLayout(); toprow.setContentsMargins(0, 0, 0, 0)
        toprow.addStretch(1)
        toprow.addWidget(make_close_button(dlg.close))
        lay.addLayout(toprow)
        title = QLabel("Buy more ships!")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color:{ACCENT}; font-family:Consolas; font-size:19pt; font-weight:bold;")
        lay.addWidget(title)
        sub = QLabel("CIG Headquarters · Earth, Sol")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        lay.addWidget(sub)
        link = QLabel('<a href="https://robertsspaceindustries.com/en/pledge" '
                      'style="color:#44ccff; text-decoration:none;">'
                      'robertsspaceindustries.com/en/pledge</a>')
        link.setTextFormat(Qt.RichText); link.setOpenExternalLinks(True)
        link.setAlignment(Qt.AlignCenter); link.setCursor(Qt.PointingHandCursor)
        link.setStyleSheet("font-family:Consolas; font-size:11pt; padding:4px;")
        lay.addWidget(link)
        self._cig_dlg = dlg               # keep a ref (non-modal)
        dlg.show(); dlg.adjustSize()
        c = self.mapToGlobal(self.rect().center())
        dlg.move(c.x() - dlg.width() // 2, c.y() - dlg.height() // 2)

    # ── location lore (right-click) ──────────────────────────────────────────
    def _show_lore(self, name: str, _system: str = "") -> None:
        """Pop a single lore bubble for a body/location. Opening a new one
        replaces the previous; the bubble streams its wiki picture + text."""
        name = (name or "").strip()
        if not name:
            return
        old = self._lore_bubble
        if old is not None:
            old.close()                  # -> _on_bubble_closed clears + deletes it
        bub = LoreBubble(name, name, self)
        self._lore_bubble = bub
        bub.closed.connect(self._on_bubble_closed)
        bub.show()
        self._place_lore_bubble(bub)
        self._lore_fetcher.fetch(name)

    def _on_lore_done(self, name: str, info) -> None:
        bub = self._lore_bubble
        if bub is not None and bub._req_name == name:
            bub.apply(info)
            self._place_lore_bubble(bub)

    def _on_bubble_closed(self, bub) -> None:
        if self._lore_bubble is bub:
            self._lore_bubble = None
        bub.deleteLater()

    def _place_lore_bubble(self, bub) -> None:
        """Anchor near the top-centre of the map area so it grows downward."""
        bub.adjustSize()
        tl = self.mapToGlobal(self.rect().topLeft())
        x = tl.x() + max(12, (self.width() - bub.width()) // 2)
        bub.move(x, tl.y() + 56)

    def _jump_to_system(self, dest_name: str) -> None:
        # Deferred: emitted from the system view own click handler — navigating
        # now would tear that view down mid-event (Qt object deleted crash).
        QTimer.singleShot(0, lambda d=dest_name: self._jump_to_system_now(d))

    def _jump_to_system_now(self, dest_name: str) -> None:
        """Lateral gateway jump: hop from the current system view straight to
        the connected system view (via the galaxy stack, not shown)."""
        code = self._sys_code(dest_name)
        if not code or self._galaxy_data is None or self._galaxy_data.get(code) is None:
            return
        self._go_galaxy()
        self._enter_system(code)

    def _enter_neighborhood(self, code: str, planet_name: str) -> None:
        try:
            view = PlanetSystemView(code, planet_name, self._bodies.get(code.upper(), []),
                                    terminal_names=self._terminal_names())
        except Exception:
            return
        view.bodyEntered.connect(lambda bn, c=code: self._enter_globe(c, bn))
        view.drillIn.connect(lambda bn, c=code: self._enter_globe(c, bn))
        view.drillOut.connect(self._go_back)
        view.terminalClicked.connect(self._open_location)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        self._push(f"{planet_name} & moons", view)

    def _enter_globe(self, code: str, body_name: str) -> None:
        try:
            view = PlanetView(code, body_name, self._bodies.get(code.upper(), []),
                              terminal_names=self._terminal_names())
        except Exception:
            return
        view.drillOut.connect(self._go_back)
        view.terminalClicked.connect(self._open_location)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        self._push(body_name.upper(), view)

    # ── items at location ────────────────────────────────────────────────────
    def _open_location(self, location: str, system: str) -> None:
        """Double-click a terminal body: show what items sell there."""
        self.ensure_index()
        dlg = ItemsDialog(
            location, system,
            rows_provider=lambda loc=location, sys=system: self._rows_for(loc, sys),
            on_popout=self._popout_item,
            parent=self)
        self._items_dlg = dlg               # keep a reference (non-modal)
        dlg.show()

    def _popout_item(self, item: dict) -> None:
        """Pop out a full detail bubble the user can drag onto the grocery list."""
        self._popouts = [p for p in self._popouts
                         if p.isVisible()]          # prune closed
        bub = ItemPopOut(item, on_add_to_grocery=self._on_add_to_grocery)
        bub.show_item(item, self._data)
        bub.show()
        # Centre on the map window so it does not cover the row that spawned it.
        tl = self.mapToGlobal(self.rect().topLeft())
        bub.move(tl.x() + max(20, (self.width() - bub.width()) // 2),
                 tl.y() + 70)
        bub.raise_()
        self._popouts.append(bub)

    # ── search / snap-to-destination ──────────────────────────────────────────
    def _all_place_names(self) -> list:
        names = set()
        if self._galaxy_data is not None:
            for s in self._galaxy_data.systems:
                names.add(s.name)
        for blist in self._bodies.values():
            for b in blist:
                names.add(b.name)
        return sorted(names)

    def goto(self, name: str) -> None:
        """Snap the map to a system or location by name."""
        name = (name or "").strip()
        if not name or self._galaxy is None:
            return
        nl = name.lower()
        if self._galaxy_data is not None:
            for s in self._galaxy_data.systems:
                if s.name.lower() == nl or s.code.lower() == nl:
                    self._go_galaxy()
                    self._galaxy.center_on(s.code)
                    return
        for match in (lambda b: b.name.lower() == nl, lambda b: nl in b.name.lower()):
            for code, blist in self._bodies.items():
                for b in blist:
                    if match(b):
                        self._go_galaxy()
                        self._enter_system(code)
                        view = self._nav[-1][1]
                        if hasattr(view, "center_on"):
                            view.center_on(b)
                        return

    def _push(self, label: str, widget: QWidget) -> None:
        self._stack.addWidget(widget)
        self._stack.setCurrentWidget(widget)
        self._nav.append((label, widget))
        self._update_navbar()

    def _go_back(self) -> None:
        if len(self._nav) <= 1:
            return
        _label, widget = self._nav.pop()
        self._stack.setCurrentWidget(self._nav[-1][1])
        self._stack.removeWidget(widget)
        widget.deleteLater()
        self._update_navbar()

    def _go_galaxy(self) -> None:
        while len(self._nav) > 1:
            self._go_back()

    def _update_navbar(self) -> None:
        deep = len(self._nav) > 1
        self._navbar.setVisible(deep)
        self._crumb.setText("   ›   ".join(lbl for lbl, _ in self._nav))
        if hasattr(self, "_btn_route"):
            self._btn_route.setEnabled(not deep)

    # ── helpers ──────────────────────────────────────────────────────────
    def _sys_code(self, sysname: str) -> str:
        if not sysname:
            return ""
        if self._galaxy_data is not None:
            for s in self._galaxy_data.systems:
                if s.name.lower() == sysname.lower() or s.code.lower() == sysname.lower():
                    return s.code
        return sysname.upper()

    # ── home ─────────────────────────────────────────────────────────────
    def _home_clicked(self) -> None:
        self._go_galaxy()
        if self._galaxy is None:
            return
        if self._galaxy.home is None:
            self._pick_home()
        else:
            self._galaxy.snap_to_home()

    def _home_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Change home system…", self._pick_home)
        menu.exec(self._btn_home.mapToGlobal(pos))

    def _pick_home(self) -> None:
        if self._galaxy_data is None or self._galaxy is None:
            return
        dlg = HomePicker(self._galaxy_data, self)
        if dlg.exec() and dlg.choice:
            self._galaxy.set_home(dlg.choice)
            self._go_galaxy()
            self._galaxy.snap_to_home()
            self._save_soon()

    # ── routing ──────────────────────────────────────────────────────────
    def _route_clicked(self) -> None:
        g = self._galaxy
        if g is None:
            return
        if g.route_active:
            g.clear_route()
            self._btn_route.setText("⤳ Route")
        else:
            g.start_route()
            self._btn_route.setText("✕ Cancel")

    def plot_shopping_route(self, stops: List[dict]) -> None:
        """Public hook for the Grocery List's "Plot Optimal Route" button.

        *stops* is the ordered list of shopping stops from
        :mod:`market_finder.route_planner` (each with a ``system`` name).
        The map pops back to the galaxy and draws the multi-stop jump route
        through each stop's system.
        """
        if self._galaxy is None:
            return
        codes = [self._sys_code(s.get("system") or "") for s in stops]
        self._go_galaxy()
        self._galaxy.plot_multi_route(codes)
        self._btn_route.setText("Clear route")

    # ── persistence ──────────────────────────────────────────────────────
    def _save_soon(self) -> None:
        if hasattr(self, "_save_timer"):
            self._save_timer.start()

    def save_state(self) -> None:
        if self._galaxy is None:
            return
        st = load_state()
        st["galaxy"] = self._galaxy.get_state()
        save_state(st)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._restored or self._galaxy is None:
            return
        self._restored = True
        if self._galaxy.home is None:
            QTimer.singleShot(0, self._first_run_home)

    def _first_run_home(self) -> None:
        if self._galaxy is not None and self._galaxy.home is None:
            self._pick_home()

    def shutdown(self) -> None:
        """Called by Market Finder on close: persist state, close bubbles."""
        if self._lore_bubble is not None:
            self._lore_bubble.close()
            self._lore_bubble = None
        for p in self._popouts:
            p.close()
        self._popouts.clear()
        if self._items_dlg is not None:
            self._items_dlg.close()
            self._items_dlg = None
        self.save_state()


class HomePicker(QDialog):
    """First-launch / change-home chooser over the five eligible home systems."""

    def __init__(self, galaxy_data: Galaxy, parent=None) -> None:
        super().__init__(parent)
        self.choice: Optional[str] = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(
            f"background: {P.bg_card}; border: 1px solid {ACCENT}; border-radius: 8px;")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        title = QLabel("Choose your home system")
        title.setStyleSheet(
            f"color: {P.tool_market}; font-family: Consolas; font-size: 13pt; font-weight: bold;")
        sub = QLabel("Snap-to-home returns here. You can change it later (right-click ⌂ Home).")
        sub.setStyleSheet(f"color: {P.fg_dim}; font-size: 9pt;")
        lay.addWidget(title)
        lay.addWidget(sub)

        grid = QGridLayout()
        grid.setSpacing(8)
        for i, code in enumerate(HOME_CHOICES):
            s = galaxy_data.get(code)
            name = s.name if s else code
            tag = "in-game" if (s and s.in_game) else "not in-game yet"
            btn = QPushButton(f"{name}\n{tag}")
            btn.setCursor(Qt.PointingHandCursor)
            btn.setMinimumHeight(52)
            accent = ACCENT if (s and s.in_game) else P.border
            btn.setStyleSheet(
                f"QPushButton {{ background: {P.bg_input}; color: {P.fg}; "
                f"border: 1px solid {accent}; border-radius: 6px; padding: 6px 16px; "
                f"font-family: Consolas; font-size: 10pt; }} "
                f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.tool_market}; }}")
            btn.clicked.connect(lambda _=False, c=code: self._choose(c))
            grid.addWidget(btn, i // 3, i % 3)
        lay.addLayout(grid)

    def _choose(self, code: str) -> None:
        self.choice = code
        self.accept()
