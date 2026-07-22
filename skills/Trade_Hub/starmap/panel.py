"""StarMapPanel — embeds the star map in Trade Hub and manages navigation,
the home system, route plotting, and the pop-out.

Scenes: galaxy -> system -> planet, with a Back/breadcrumb bar. The whole map
area (controls + scenes) reparents into a top-level SCWindow on "Pop Out", so
state carries over with zero reload; while popped out the tab shows a "Return"
placeholder. View state (camera, selection, home, route, pop-out, opacity)
persists atomically across launches.

Everything is defensive: if data/scene construction fails the panel shows an
inline message instead of letting a star-map error break Trade Hub.
"""
from __future__ import annotations

import threading
import time
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
    QStackedWidget, QVBoxLayout, QWidget,
)

from shared.qt.theme import P
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.fuzzy_combo import SCFuzzyCombo

from .data import (Galaxy, HOME_CHOICES, LOC_ALIASES, load_bodies, load_state,
                   norm_loc, save_state)
from .galaxy_view import GalaxyView
from .trade_overlay import TradeOverlay
from .planet_system_view import PlanetSystemView
from .planet_view import PlanetView
from .system_view import SystemView
from .terminal_panel import TerminalDialog
from .commodity_view import CommodityView
from .lore import LoreBubble, LoreFetcher
from .ui import make_close_button

_OPACITY_STEPS: List[float] = [1.0, 0.85, 0.7, 0.55]
_OVERLAY_LAYERS = (("flows", "Trade Flows"), ("top", "Top Routes"), ("activity", "Activity"),
                   ("career", "My Runs"))


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 5px 14px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.energy_cyan}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }}"
    )


class StarMapPanel(QWidget):
    _overlay_ready = Signal(object)        # worker thread -> UI: a freshly-built TradeOverlay

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._overlay = None
        self._overlay_flags = {"flows": False, "top": False, "activity": False, "career": False}
        self._overlay_ready.connect(self._apply_overlay)
        self._lore_bubble: Optional[LoreBubble] = None
        self._lore_fetcher = LoreFetcher()
        self._lore_fetcher.done.connect(self._on_lore_done)
        self._galaxy: Optional[GalaxyView] = None
        self._galaxy_data: Optional[Galaxy] = None
        self._bodies = {}
        self._trade_hub = None
        self._trade_route_pts = []
        self._maparea: Optional[QWidget] = None
        self._popout: Optional[SCWindow] = None
        self._restored = False
        self._popout_opacity = 1.0
        self._nav: List[Tuple[str, QWidget]] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        try:
            self._build_ui(root)
        except Exception as exc:   # never break Trade Hub over the star map
            msg = QLabel(f"Star Map unavailable:\n{exc}")
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet(f"color: {P.fg_dim}; font-size: 10pt; padding: 24px;")
            root.addWidget(msg, 1)

    # ── construction ────────────────────────────────────────────────────────
    def _build_ui(self, root: QVBoxLayout) -> None:
        st = load_state()
        self._popout_opacity = float(st.get("popout_opacity", 1.0))
        self._overlay_flags = {**self._overlay_flags, **(st.get("overlay_flags") or {})}

        self._galaxy_data = Galaxy.load()
        self._bodies = load_bodies()

        self._galaxy = GalaxyView(self._galaxy_data)
        self._galaxy.apply_state(st.get("galaxy"))
        self._galaxy.viewChanged.connect(self._save_soon)
        self._galaxy.systemEntered.connect(self._enter_system)
        self._galaxy.drillIn.connect(self._enter_system)
        self._galaxy.loreRequested.connect(self._show_lore)
        self._galaxy.routePlotted.connect(lambda *_: self._btn_route.setText("✕ Clear route"))

        # Map area (this whole widget is what reparents on pop-out).
        self._maparea = QWidget()
        ma = QVBoxLayout(self._maparea)
        ma.setContentsMargins(0, 0, 0, 0)
        ma.setSpacing(0)
        ma.addWidget(self._build_controls())
        ma.addWidget(self._build_navbar())
        self._stack = QStackedWidget()
        self._stack.addWidget(self._galaxy)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        row.addWidget(self._build_overlay_sidebar())
        row.addWidget(self._stack, 1)
        ma.addLayout(row, 1)
        self._galaxy.set_overlay(self._overlay, self._overlay_flags)
        self._nav = [("Galaxy", self._galaxy)]
        self._update_navbar()

        self._holder = QWidget()
        hl = QVBoxLayout(self._holder)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        hl.addWidget(self._maparea, 1)
        root.addWidget(self._holder, 1)

        self._placeholder = self._build_placeholder()
        self._placeholder.hide()
        root.addWidget(self._placeholder, 1)

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

        self._btn_popout = QPushButton("⧉ Pop Out")
        self._btn_popout.setCursor(Qt.PointingHandCursor)
        self._btn_popout.setStyleSheet(_btn_ss())
        self._btn_popout.clicked.connect(self.toggle_popout)

        self._search = SCFuzzyCombo(placeholder="Search system or location…",
                                    items=self._all_place_names())
        self._search.item_selected.connect(self.goto)

        lay.addWidget(self._btn_home)
        lay.addWidget(self._btn_route)
        lay.addWidget(self._search, 1)
        lay.addWidget(self._btn_popout)
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

    def _build_overlay_sidebar(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(120)
        w.setStyleSheet(f"background: {P.bg_secondary};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(7)
        hdr = QLabel("OVERLAYS")
        hdr.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 8pt; font-weight: bold;")
        lay.addWidget(hdr)
        self._ov_buttons = {}
        for key, label in _OVERLAY_LAYERS:
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(self._overlay_flags.get(key, False))
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(self._toggle_ss())
            b.toggled.connect(lambda on, k=key: self._toggle_overlay(k, on))
            lay.addWidget(b)
            self._ov_buttons[key] = b
        lay.addStretch(1)
        note = QLabel("community-reported\ntrade data")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {P.fg_disabled}; font-size: 7pt;")
        lay.addWidget(note)
        return w

    @staticmethod
    def _toggle_ss() -> str:
        return (
            f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; "
            f"border: 1px solid {P.border}; border-radius: 5px; padding: 7px 6px; "
            f"font-family: Consolas; font-size: 8.5pt; text-align: left; }} "
            f"QPushButton:hover {{ color: {P.fg_bright}; }} "
            f"QPushButton:checked {{ background: {P.tool_trade}; color: #1a1400; "
            f"border-color: {P.tool_trade}; font-weight: bold; }}"
        )

    # ── trade-activity overlay ────────────────────────────────────────────────
    def _toggle_overlay(self, key: str, on: bool) -> None:
        self._overlay_flags[key] = bool(on)
        self._push_overlay()
        self._save_soon()

    def _push_overlay(self) -> None:
        for _lbl, w in self._nav:
            if hasattr(w, "set_overlay"):
                w.set_overlay(self._overlay, self._overlay_flags)

    def _rebuild_overlay(self) -> None:
        """Recompute the overlay off-thread (it reads UEX prices for recency)."""
        routes = self._routes()
        career = getattr(self._trade_hub, "_career", None)
        def work(routes=routes, career=career) -> None:
            try:
                cmap = career.map_data(self._sys_code) if career else None
                ov = TradeOverlay(routes, self._sys_code, self._terminal_recency(), career=cmap)
            except Exception:
                ov = None
            self._overlay_ready.emit(ov)
        threading.Thread(target=work, daemon=True, name="TradeOverlayBuild").start()

    def _apply_overlay(self, ov) -> None:
        if ov is None:
            return
        self._overlay = ov
        self._push_overlay()

    def _terminal_recency(self) -> dict:
        from .trade_overlay import terminal_recency
        return terminal_recency()

    def _build_placeholder(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(14)
        lbl = QLabel("Star Map is running in its own window.")
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 11pt;")
        btn = QPushButton("⧉  Return to this tab")
        btn.setCursor(Qt.PointingHandCursor)
        btn.setStyleSheet(_btn_ss())
        btn.clicked.connect(self._close_popout)
        lay.addStretch(1)
        lay.addWidget(lbl, 0, Qt.AlignCenter)
        lay.addWidget(btn, 0, Qt.AlignCenter)
        lay.addStretch(1)
        return w

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
        view.terminalClicked.connect(self._open_terminal)
        view.jumpRequested.connect(self._jump_to_system)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        view.set_trade_route(self._route_pts_for(code.upper()))
        view.set_overlay(self._overlay, self._overlay_flags)
        self._push(f"{name.upper()} system", view)
        self._frame_route_in(view, code.upper())   # show/fit any active leg in THIS system

    def _on_body_activated(self, name: str, code: str) -> None:
        if name == "CIG Headquarters":
            self._show_cig_hq()

    def _show_cig_hq(self) -> None:
        """Easter egg: CIG HQ on Earth (Sol) pops a 'Buy more ships!' bubble."""
        dlg = QDialog(self)
        dlg.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        dlg.setAttribute(Qt.WA_TranslucentBackground)
        dlg.setModal(False)
        outer = QVBoxLayout(dlg); outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(f"background:{P.bg_card}; border:2px solid {P.tool_trade}; border-radius:12px;")
        outer.addWidget(card)
        lay = QVBoxLayout(card); lay.setContentsMargins(28, 14, 28, 22); lay.setSpacing(10)
        toprow = QHBoxLayout(); toprow.setContentsMargins(0, 0, 0, 0)
        toprow.addStretch(1)
        toprow.addWidget(make_close_button(dlg.close))
        lay.addLayout(toprow)
        title = QLabel("Buy more ships!")
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"color:{P.tool_trade}; font-family:Consolas; font-size:19pt; font-weight:bold;")
        lay.addWidget(title)
        sub = QLabel("CIG Headquarters · Earth, Sol")
        sub.setAlignment(Qt.AlignCenter)
        sub.setStyleSheet(f"color:{P.fg_dim}; font-size:8pt;")
        lay.addWidget(sub)
        link = QLabel('<a href="https://robertsspaceindustries.com/en/pledge" '
                      'style="color:#44ccff; text-decoration:none;">'
                      '🚀  robertsspaceindustries.com/en/pledge</a>')
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
        # Deferred: emitted from the system view's own click handler — navigating
        # now would tear that view down mid-event (Qt 'object deleted' crash).
        QTimer.singleShot(0, lambda d=dest_name: self._jump_to_system_now(d))

    def _jump_to_system_now(self, dest_name: str) -> None:
        """Lateral gateway jump: hop from the current system view straight to the
        connected system's view (via the galaxy stack, not shown)."""
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
        view.terminalClicked.connect(self._open_terminal)
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
        view.terminalClicked.connect(self._open_terminal)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        self._push(body_name.upper(), view)

    # ── terminals + trade hub (UEX overlay) ──────────────────────────────────
    def set_trade_hub(self, hub) -> None:
        self._trade_hub = hub
        # selecting a buy/sell location in the Trade Hub snaps the map to it too
        for attr in ("_buy_loc", "_sell_loc"):
            combo = getattr(hub, attr, None)
            sig = getattr(combo, "item_selected", None)
            if sig is not None:
                try:
                    sig.connect(self.goto)
                except Exception:
                    pass

    def _routes(self) -> list:
        hub = self._trade_hub
        return list(getattr(hub, "_all_routes", None) or []) if hub is not None else []

    def _terminal_names(self) -> set:
        """Normalised set of every location that hosts a commodity terminal, so
        map bodies whose name differs from the UEX route name ('Pyro Gateway' vs
        'Pyro Gateway (Stanton)', 'Grim HEX' vs 'Green Imperial Housing Exchange')
        still get a terminal badge + click."""
        names = set()
        for r in self._routes():
            for loc in (getattr(r, "buy_location", ""), getattr(r, "sell_location", "")):
                if loc:
                    n = norm_loc(loc)
                    names.add(LOC_ALIASES.get(n, n))
        return names

    def on_routes_loaded(self) -> None:
        """Called by Trade Hub when route data (re)loads — refresh terminal badges."""
        names = self._terminal_names()
        for _lbl, w in self._nav:
            if hasattr(w, "_terminals"):
                w._terminals = names
                w.update()
        self._rebuild_overlay()

    def _open_terminal(self, location: str, system: str) -> None:
        dlg = TerminalDialog(location, system, self._routes(), self._plot_route,
                             on_commodity=self._open_commodity, parent=self)
        self._terminal_dlg = dlg          # keep a reference (non-modal)
        dlg.show()

    def _open_commodity(self, name: str, location: str = "", system: str = "") -> None:
        view = CommodityView(
            name,
            on_routes=lambda n, loc=location, sys=system: self._routes_for_commodity(n, loc, sys),
            on_route=lambda dloc, dsys, n=name, oloc=location, osys=system: self._route_to(n, dloc, dsys, oloc, osys),
            parent=self)
        self._commodity_dlg = view        # keep a reference (non-modal)
        view.show()

    def _route_to(self, commodity: str, dest_loc: str, dest_sys: str,
                  origin_loc: str = "", origin_sys: str = "") -> None:
        # Auto-route this commodity from the origin terminal (if any) TO the chosen
        # destination — UEX-style: origin + destination + commodity.
        self._apply_filters(buy_loc=origin_loc, buy_sys=origin_sys,
                            sell_loc=dest_loc, sell_sys=dest_sys, commodity=commodity)

    def _routes_for_commodity(self, name: str, location: str = "", system: str = "") -> None:
        # Routes for this commodity, optionally FROM a specific terminal (UEX-style).
        self._apply_filters(buy_loc=location, buy_sys=system, commodity=name)

    # ── trade-route overlay (mirror a calculated Trade Hub route) ─────────────
    def show_route(self, waypoints) -> None:
        # Defer to the next event-loop tick: this is often called from inside a
        # click handler, and navigating scenes here would tear down widgets
        # mid-event (Qt "C++ object already deleted" crash).
        QTimer.singleShot(0, lambda wp=list(waypoints): self._show_route_now(wp))

    def _show_route_now(self, waypoints) -> None:
        """Draw a calculated route. waypoints = [(location, system, role), …].
        Intra-system: a buy→sell line in that system. Cross-system: the galaxy
        jump path PLUS a leg inside EACH endpoint system (buy→jump-gateway in the
        origin, jump-gateway→sell in the destination) so the route reads in both."""
        pts = []
        for loc, sysname, role in waypoints:
            code = self._sys_code(sysname)
            body = self._find_body(code, loc)
            if body is not None:
                pts.append((code, loc, body.x, body.y, body.z, role))
            else:
                pts.append((code, loc, None, None, None, role))
        uniq = list(dict.fromkeys(c for (c, _n, x, _y, _z, _r) in pts if c and x is not None))
        # DIAGNOSTIC (2026-07-21): a route silently vanishes when its waypoint LOCATION NAMES
        # can't be matched to a map body (_find_body). Log the unresolved names so the missing
        # LOC_ALIASES entries can be added — this is the "some routes don't show" bug.
        import logging as _lg
        _unres = [loc for (c, loc, x, _y, _z, _r) in pts if x is None]
        if _unres:
            _lg.getLogger("TradeHub.starmap").warning(
                "route select: %d/%d waypoints UNRESOLVED on map: %s", len(_unres), len(pts), " | ".join(_unres))
        if len(uniq) >= 2:
            pts = self._inject_gateways(pts)
        self._trade_route_pts = pts
        self._go_galaxy()
        if self._galaxy is None or not uniq:
            if not uniq:
                _lg.getLogger("TradeHub.starmap").warning(
                    "route NOT shown — NONE of its waypoints resolved to map bodies: %s",
                    " | ".join(loc for (_c, loc, *_rest) in pts))
            return
        if len(uniq) >= 2:
            self._galaxy.plot_route(uniq[0], uniq[-1])      # cross-system jump path
            buy_code = next((c for (c, _n, x, _y, _z, r) in pts
                             if r == "buy" and x is not None and c in self._bodies), None) or \
                next((c for (c, _n, x, _y, _z, _r) in pts if x is not None and c in self._bodies), None)
            if buy_code:
                self._enter_system(buy_code)                # _enter_system frames its leg
        elif uniq[0] in self._bodies:
            self._enter_system(uniq[0])                     # intra-system location line

    def _frame_route_in(self, view, code: str) -> None:
        """Frame a freshly-entered system view on its active trade leg: fit the
        whole line when the system holds ≥2 route points (an intra-system pair, or
        a cross-system endpoint + its jump gateway), else centre the lone point."""
        if not hasattr(view, "frame_trade_route"):
            return
        if view.frame_trade_route():
            return
        rpts = self._route_pts_for(code)
        if len(rpts) == 1 and hasattr(view, "center_on"):
            body = self._find_body(code, rpts[0][0])
            if body is not None:
                view.center_on(body, zoom=2.6)

    def _inject_gateways(self, pts: list) -> list:
        """Cross-system route: append each endpoint system's jump-gateway toward
        the OTHER endpoint, so every SystemView can draw its own leg. Gateways are
        named after the system they lead to ('Pyro Gateway' in Stanton ↔ 'Stanton
        Gateway' in Pyro)."""
        buy = next((p for p in pts if p[5] == "buy" and p[2] is not None), None)
        sell = next((p for p in pts if p[5] == "sell" and p[2] is not None), None)
        if not buy or not sell or buy[0] == sell[0]:
            return pts
        out = list(pts)
        for here, other in ((buy[0], sell[0]), (sell[0], buy[0])):
            gw = self._find_body(here, f"{self._sys_name(other)} Gateway")
            if gw is not None:
                out.append((here, gw.name, gw.x, gw.y, gw.z, "jump"))
        return out

    def _sys_name(self, code: str) -> str:
        if self._galaxy_data is not None:
            s = self._galaxy_data.get(code)
            if s:
                return s.name
        return code.title()

    def _route_pts_for(self, code: str) -> list:
        return [(n, x, y, z, role) for (c, n, x, y, z, role) in self._trade_route_pts
                if c == code and x is not None]

    def _sys_code(self, sysname: str) -> str:
        if not sysname:
            return ""
        if self._galaxy_data is not None:
            for s in self._galaxy_data.systems:
                if s.name.lower() == sysname.lower() or s.code.lower() == sysname.lower():
                    return s.code
        return sysname.upper()

    def _find_body(self, code: str, loc: str):
        """Resolve a UEX terminal/location name to a positioned body. UEX names
        diverge from the scunpacked body names ('Pyro Gateway (Stanton)' vs
        'Pyro Gateway', 'Area 18' vs 'Area18', 'Green Imperial Housing Exchange'
        vs 'Grim HEX'), so match in escalating leniency: exact → normalised-exact
        (+ alias) → guarded normalised substring."""
        bl = self._bodies.get(code, [])
        if not loc:
            return None
        raw = loc.lower()
        for b in bl:                                    # 1) exact (case-insensitive)
            if b.name.lower() == raw:
                return b
        n = norm_loc(loc)
        n = LOC_ALIASES.get(n, n)
        if not n:
            return None
        for b in bl:                                    # 2) normalised exact (+ alias)
            if norm_loc(b.name) == n:
                return b
        cands = []                                      # 3) guarded substring either way
        for b in bl:
            bn = norm_loc(b.name)
            if bn and len(n) >= 4 and (n in bn or bn in n):
                cands.append(b)
        if cands:
            return max(cands, key=lambda b: len(b.name))
        return None

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
        """Snap the map to a system or location by name — from the map search bar
        or the Trade Hub buy/sell-location pickers."""
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

    def _close_dialogs(self) -> None:
        for attr in ("_terminal_dlg", "_commodity_dlg"):
            d = getattr(self, attr, None)
            if d is not None:
                try:
                    d.close()
                except Exception:
                    pass

    def _apply_filters(self, buy_loc: str = "", buy_sys: str = "", commodity: str = "",
                       sell_loc: str = "", sell_sys: str = "") -> None:
        # Defer (see _show_route_now): switching view / rebuilding the route table /
        # closing dialogs must not happen inside the click handler that triggered it.
        QTimer.singleShot(0, lambda: self._apply_filters_now(
            buy_loc, buy_sys, commodity, sell_loc, sell_sys))

    def _apply_filters_now(self, buy_loc: str, buy_sys: str, commodity: str,
                           sell_loc: str, sell_sys: str) -> None:
        """Apply a CLEAN filter set on the Trade Hub (blanks the filters not
        passed, like UEX's terminal_origin/commodity URL params) and show ROUTES."""
        hub = self._trade_hub
        if hub is None:
            return
        try:
            hub._buy_loc.set_text(buy_loc)
            hub._buy_sys.set_text(buy_sys)
            hub._sell_loc.set_text(sell_loc)
            hub._sell_sys.set_text(sell_sys)
            hub._commodity_combo.set_text(commodity)
            hub._set_view_mode("ROUTES")
            hub._apply_search()
        except Exception:
            pass
        self._close_dialogs()

    def _plot_route(self, location: str, system: str) -> None:
        # All routes FROM this terminal.
        self._apply_filters(buy_loc=location, buy_sys=system)

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

    # ── home (M3) ───────────────────────────────────────────────────────────
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

    # ── routing (M4) ───────────────────────────────────────────────────────────
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

    def show_trade_route(self, start_code: str, end_code: str) -> None:
        """Public hook: highlight a Trade Hub route's start→end systems on the map."""
        if self._galaxy is None:
            return
        self._go_galaxy()
        self._galaxy.plot_route(start_code, end_code)
        self._btn_route.setText("✕ Clear route")

    # ── pop-out ─────────────────────────────────────────────────────────────
    def toggle_popout(self) -> None:
        if self._maparea is None:
            return
        if self._popout is None:
            self._open_popout()
        else:
            self._close_popout()

    def _open_popout(self, geom: Optional[List[int]] = None) -> None:
        if self._maparea is None or self._popout is not None:
            return
        win = SCWindow(title="Star Map", width=1100, height=760,
                       min_w=520, min_h=400, opacity=self._popout_opacity,
                       accent=P.energy_cyan)
        tb = SCTitleBar(win, title="STAR MAP", icon_text="✦",
                        accent_color=P.energy_cyan, show_minimize=True,
                        extra_buttons=[("◑", self._cycle_popout_opacity)])
        tb.minimize_clicked.connect(win.showMinimized)
        tb.close_clicked.connect(self._close_popout)
        win.content_layout.addWidget(tb)
        win.content_layout.addWidget(self._maparea, 1)   # reparent the live map area
        if geom and len(geom) == 4:
            win.setGeometry(*geom)
        win.set_opacity(self._popout_opacity)
        win.show()
        self._popout = win
        self._btn_popout.setText("⧉ Return to tab")
        self._show_popped(True)
        self._save_soon()

    def _close_popout(self) -> None:
        if self._popout is None:
            return
        win = self._popout
        self._popout = None
        if self._maparea is not None:
            self._holder.layout().addWidget(self._maparea, 1)
        self._btn_popout.setText("⧉ Pop Out")
        self._show_popped(False)
        win.close()
        win.deleteLater()
        self._save_soon()

    def _show_popped(self, popped: bool) -> None:
        self._holder.setVisible(not popped)
        self._placeholder.setVisible(popped)

    def _cycle_popout_opacity(self) -> None:
        try:
            i = _OPACITY_STEPS.index(round(self._popout_opacity, 2))
        except ValueError:
            i = 0
        self._popout_opacity = _OPACITY_STEPS[(i + 1) % len(_OPACITY_STEPS)]
        if self._popout is not None:
            self._popout.set_opacity(self._popout_opacity)
        self._save_soon()

    # ── persistence ─────────────────────────────────────────────────────────
    def _save_soon(self) -> None:
        if hasattr(self, "_save_timer"):
            self._save_timer.start()

    def save_state(self) -> None:
        if self._galaxy is None:
            return
        st = load_state()
        st["galaxy"] = self._galaxy.get_state()
        st["popout_open"] = self._popout is not None
        st["popout_opacity"] = self._popout_opacity
        st["overlay_flags"] = self._overlay_flags
        if self._popout is not None:
            g = self._popout.geometry()
            st["popout_geom"] = [g.x(), g.y(), g.width(), g.height()]
        save_state(st)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._restored or self._galaxy is None:
            return
        self._restored = True
        try:
            st = load_state()
            if st.get("popout_open"):
                self._open_popout(st.get("popout_geom"))
        except Exception:
            pass
        if self._galaxy.home is None:
            QTimer.singleShot(0, self._first_run_home)

    def _first_run_home(self) -> None:
        if self._galaxy is not None and self._galaxy.home is None:
            self._pick_home()

    def shutdown(self) -> None:
        """Called by Trade Hub on close: persist, then tear down any pop-out."""
        if self._lore_bubble is not None:
            self._lore_bubble.close()
            self._lore_bubble = None
        self.save_state()
        if self._popout is not None:
            win = self._popout
            self._popout = None
            if self._maparea is not None:
                self._holder.layout().addWidget(self._maparea, 1)
            win.close()
            win.deleteLater()


class HomePicker(QDialog):
    """First-launch / change-home chooser over the five eligible home systems."""

    def __init__(self, galaxy_data: Galaxy, parent=None) -> None:
        super().__init__(parent)
        self.choice: Optional[str] = None
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QWidget()
        card.setStyleSheet(
            f"background: {P.bg_card}; border: 1px solid {P.energy_cyan}; border-radius: 8px;")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(12)

        title = QLabel("Choose your home system")
        title.setStyleSheet(
            f"color: {P.tool_trade}; font-family: Consolas; font-size: 13pt; font-weight: bold;")
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
            accent = P.energy_cyan if (s and s.in_game) else P.border
            btn.setStyleSheet(
                f"QPushButton {{ background: {P.bg_input}; color: {P.fg}; "
                f"border: 1px solid {accent}; border-radius: 6px; padding: 6px 16px; "
                f"font-family: Consolas; font-size: 10pt; }} "
                f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {P.tool_trade}; }}")
            btn.clicked.connect(lambda _=False, c=code: self._choose(c))
            grid.addWidget(btn, i // 3, i % 3)
        lay.addLayout(grid)

    def _choose(self, code: str) -> None:
        self.choice = code
        self.accept()
