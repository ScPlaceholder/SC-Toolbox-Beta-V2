"""StarmapPanel — the merged standalone star map.

Combines the Trade Hub star map (galaxy -> system -> planet globe scenes,
home system, lore bubbles, route plotting) with the Market Finder star
map (terminal -> items browsing, item pop-outs, grocery list, multi-stop
shopping routes), and a command router ("navigate to Area 18", "zoom in")
fed by the command bar and by the AI Assistant.

The Star Map has NO microphone (J, 2026-10-04). It used to carry its own
voice ears (starmap/voice/), and with the saved mic mode "Always on" simply
opening the map - standalone, or as a tab of the Everything Finder - armed
the mic. Voice-to-text now lives in one place, the Assistant, which relays
what it hears as an IPC ``map_command`` (:meth:`StarmapPanel.handle_map_command`).

Terminal clicks open the combined :class:`LocationDialog` (commodities
+ items tabs). The grocery list docks on the right; its "Plot shopping
route" draws the multi-stop jump route on the galaxy view.

Everything is defensive: if data/scene construction fails the panel
shows an inline message instead of dying.

Everything Finder port (2026-10-03) - the Trade Hub star map's features that
this panel lacked, so the one Star Map carries the union of all three copies:

  * trade OVERLAYS (Trade Flows / Top Routes / Activity / My Runs), from
    Trade_Hub/starmap/panel.py + trade_overlay.py. Hidden until a host calls
    :meth:`set_routes_provider`, because this tool has no route engine.
  * :meth:`show_route` - draw one calculated Trade Hub route (buy -> sell, with
    the jump gateways injected so each endpoint system draws its own leg).
  * Trade Hub link-through (:meth:`set_trade_hub`): "Trade routes from here" on
    a location card, and the commodity page's routes buttons, filter the live
    Trade Hub's routes table exactly as the Trade Hub star map does.
  * the location card's Items tab refills when the items index lands (from the
    Item Finder star map), instead of staying on "no item data".
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

import threading

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton,
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
from .items_index import ItemsIndexLoader, index_counts, items_at
from .item_popout import ItemPopOut
from .location_dialog import LocationDialog
from .grocery import GroceryPanel
from .lore import LoreBubble, LoreFetcher
from .ui import make_close_button
from .commands import CommandRouter, safe_reply_file
from .command_bar import CommandBar
from .commodities_view import CommoditiesView
from .market_view import MarketView

ACCENT = P.energy_cyan

# Trade Hub star map overlay layers (Trade_Hub/starmap/panel.py _OVERLAY_LAYERS).
_OVERLAY_LAYERS = (("flows", "Trade Flows"), ("top", "Top Routes"), ("activity", "Activity"),
                   ("career", "My Runs"))


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 5px 14px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }}"
    )


class StarmapPanel(QWidget):
    """Star map + location browser + grocery list + command router (no microphone)."""

    _overlay_ready = Signal(object)        # worker thread -> UI: a freshly-built TradeOverlay
    # Asks the host to bring its Trade Hub forward (the Everything Finder switches tab).
    tradeHubRequested = Signal()

    def __init__(self, parent: Optional[QWidget] = None,
                 cmd_file: str = "") -> None:
        super().__init__(parent)
        # Trade Hub star map state (ported). All inert until a host attaches routes.
        self._overlay = None
        self._overlay_flags = {"flows": False, "top": False, "activity": False, "career": False}
        try:
            self._overlay_flags.update({k: bool(v) for k, v in
                                        (load_state().get("overlay_flags") or {}).items()
                                        if k in self._overlay_flags})
        except (AttributeError, TypeError):
            pass
        self._overlay_ready.connect(self._apply_overlay)
        self._routes_provider: Optional[Callable[[], list]] = None
        self._career_provider: Optional[Callable[[], object]] = None
        self._trade_hub = None
        self._trade_route_pts: list = []
        self._overlay_sidebar: Optional[QWidget] = None
        self._ov_buttons: Dict[str, QPushButton] = {}
        self._lore_bubble: Optional[LoreBubble] = None
        self._lore_fetcher = LoreFetcher()
        self._lore_fetcher.done.connect(self._on_lore_done)
        self._galaxy: Optional[GalaxyView] = None
        self._galaxy_data: Optional[Galaxy] = None
        self._bodies = {}
        self._items_index: Dict[tuple, list] = {}
        self._popouts: List[ItemPopOut] = []
        self._location_dlg: Optional[LocationDialog] = None
        self._restored = False
        self._nav: List[Tuple[str, QWidget]] = []
        self._shop_stops: List[dict] = []     # the grocery route on the map, in visit order
        self._ipc = None
        self._dest_engine = None
        self._setter_obj = None
        self._voicebar = None          # the CommandBar (name kept: voice_status() feeds it)
        self._router = None
        self._said = None              # lines "spoken" while a command runs (see speak())
        self._reply_file = ""          # where the Assistant that sent the last command listens
        self._root_layout = None
        self._side_panels: Dict[str, QWidget] = {}
        self._side_buttons: Dict[str, QPushButton] = {}
        self._comm_views: List[QWidget] = []

        # Terminal -> items index, built off-thread from UEX items_prices_all.
        self._index_loader = ItemsIndexLoader(self)
        self._index_loader.done.connect(self._on_index_done)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self._root_layout = root
        try:
            self._build_ui(root)
            self._build_command_bar()
        except Exception as exc:   # never die over the star map
            msg = QLabel(f"Star Map unavailable:\n{exc}")
            msg.setAlignment(Qt.AlignCenter)
            msg.setStyleSheet(f"color: {P.fg_dim}; font-size: 10pt; padding: 24px;")
            root.addWidget(msg, 1)

        if cmd_file:
            try:
                from shared.qt.ipc_thread import IPCWatcher
                self._ipc = IPCWatcher(cmd_file, poll_ms=500, parent=self)
                self._ipc.command_received.connect(self._on_ipc)
                self._ipc.start()
            except Exception:
                self._ipc = None

    # construction
    def _build_ui(self, root: QVBoxLayout) -> None:
        self._galaxy_data = Galaxy.load()
        self._bodies = load_bodies()

        self._galaxy = GalaxyView(self._galaxy_data)
        self._galaxy.apply_state(load_state().get("galaxy"))
        self._galaxy.viewChanged.connect(self._save_soon)
        self._galaxy.systemEntered.connect(self._enter_system)
        self._galaxy.drillIn.connect(self._enter_system)
        self._galaxy.loreRequested.connect(self._show_lore)
        self._galaxy.routePlotted.connect(
            lambda *_: self._btn_route.setText("Clear route"))

        self._stack = QStackedWidget()
        self._stack.addWidget(self._galaxy)
        # The grocery panel must exist BEFORE _build_controls(): the Grocery button wires to
        # self._grocery.setVisible there. Built after it, the map died on "no attribute '_grocery'".
        self._grocery = GroceryPanel()
        self._grocery.plotRequested.connect(self._plot_grocery_route)
        self._grocery.changed.connect(self._grocery_changed)
        self._grocery.setVisible(False)
        # Side views, cloned from their origin tools: the Market Finder
        # terminal/items browser and the Trade Hub commodities grid. They
        # dock where the grocery list does; the view buttons pick which
        # one (at most one) is visible.
        self._market = MarketView(self)
        self._market.setVisible(False)
        self._commodities = CommoditiesView()
        self._commodities.openRequested.connect(self._open_commodity)
        self._commodities.setVisible(False)
        self._side_panels = {"grocery": self._grocery, "market": self._market,
                             "commodities": self._commodities}
        root.addWidget(self._build_controls())
        root.addWidget(self._build_navbar())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        self._overlay_sidebar = self._build_overlay_sidebar()
        self._overlay_sidebar.setVisible(False)       # shown by set_routes_provider()
        body.addWidget(self._overlay_sidebar)
        body.addWidget(self._stack, 1)
        body.addWidget(self._grocery)
        body.addWidget(self._market)
        body.addWidget(self._commodities)
        root.addLayout(body, 1)

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

        self._btn_home = QPushButton("Home")
        self._btn_home.setCursor(Qt.PointingHandCursor)
        self._btn_home.setStyleSheet(_btn_ss())
        self._btn_home.setToolTip("Left-click: snap to home - Right-click: change home")
        self._btn_home.clicked.connect(self._home_clicked)
        self._btn_home.setContextMenuPolicy(Qt.CustomContextMenu)
        self._btn_home.customContextMenuRequested.connect(self._home_menu)

        self._btn_route = QPushButton("Route")
        self._btn_route.setCursor(Qt.PointingHandCursor)
        self._btn_route.setStyleSheet(_btn_ss())
        self._btn_route.setToolTip("Plot a jump route: click a start system, then a destination")
        self._btn_route.clicked.connect(self._route_clicked)
        self._btn_route.setContextMenuPolicy(Qt.CustomContextMenu)
        self._btn_route.customContextMenuRequested.connect(self._route_menu)

        self._btn_game = QPushButton("In-Game")
        self._btn_game.setCursor(Qt.PointingHandCursor)
        self._btn_game.setCheckable(True)
        self._btn_game.setStyleSheet(_btn_ss())
        self._btn_game.setToolTip("Also plot 'navigate to ...' routes inside Star Citizen "
                                  "(right-click Route to calibrate)")
        self._btn_game.toggled.connect(lambda _on: self._save_soon())
        try:
            self._btn_game.setChecked(bool(load_state().get("game_route")))
        except (AttributeError, TypeError, RuntimeError) as e:
            # load_state() already absorbs a missing or unparseable file and returns {}, so what is left is a state
            # file holding a JSON list instead of an object (AttributeError on .get) or a dead C++ object behind the
            # button. The button then silently comes up UNCHECKED, which reads as "the pilot never enabled in-game
            # plotting" - so voice routes stop being drawn inside Star Citizen and their saved preference is simply
            # ignored, with the UI showing a perfectly ordinary off switch.
            log.warning("Starmap: could not restore the In-Game route toggle (%s: %s); it defaults to off",
                        type(e).__name__, e)

        self._btn_grocery = QPushButton("Grocery")
        self._btn_grocery.setCursor(Qt.PointingHandCursor)
        self._btn_grocery.setCheckable(True)
        self._btn_grocery.setStyleSheet(_btn_ss())
        self._btn_grocery.setToolTip("Show / hide the grocery list")
        self._btn_grocery.toggled.connect(
            lambda on, k="grocery": self._side_toggled(k, on))

        self._btn_market = QPushButton("Market")
        self._btn_market.setCursor(Qt.PointingHandCursor)
        self._btn_market.setCheckable(True)
        self._btn_market.setStyleSheet(_btn_ss())
        self._btn_market.setToolTip("Show / hide the Item Finder view (terminal items)")
        self._btn_market.toggled.connect(
            lambda on, k="market": self._side_toggled(k, on))

        self._btn_comm = QPushButton("Commodities")
        self._btn_comm.setCursor(Qt.PointingHandCursor)
        self._btn_comm.setCheckable(True)
        self._btn_comm.setStyleSheet(_btn_ss())
        self._btn_comm.setToolTip("Show / hide the commodities view (UEX prices)")
        self._btn_comm.toggled.connect(
            lambda on, k="commodities": self._side_toggled(k, on))
        self._side_buttons = {"grocery": self._btn_grocery,
                              "market": self._btn_market,
                              "commodities": self._btn_comm}

        self._search = SCFuzzyCombo(placeholder="Search system or location...",
                                    items=self._all_place_names())
        self._search.item_selected.connect(self.goto)

        lay.addWidget(self._btn_home)
        lay.addWidget(self._btn_route)
        lay.addWidget(self._btn_game)
        lay.addWidget(self._btn_grocery)
        lay.addWidget(self._btn_market)
        lay.addWidget(self._btn_comm)
        lay.addWidget(self._search, 1)
        return w

    def _build_navbar(self) -> QWidget:
        self._navbar = QWidget()
        self._navbar.setStyleSheet(f"background: {P.bg_secondary};")
        lay = QHBoxLayout(self._navbar)
        lay.setContentsMargins(8, 3, 8, 3)
        lay.setSpacing(8)
        self._btn_back = QPushButton("< Back")
        self._btn_back.setCursor(Qt.PointingHandCursor)
        self._btn_back.setStyleSheet(_btn_ss())
        self._btn_back.clicked.connect(self._go_back)
        self._crumb = QLabel("")
        self._crumb.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 9pt; background: transparent;")
        lay.addWidget(self._btn_back)
        lay.addWidget(self._crumb)
        lay.addStretch(1)
        return self._navbar

    # command bar (typed commands + commands relayed by the Assistant; no microphone)
    def _build_command_bar(self) -> None:
        """Build the command router and its bar.

        Nothing here opens, arms or even imports audio capture. That is the
        point of the method: its predecessor (_build_ears) restored the saved
        mic mode and armed the ears one tick after construction."""
        self._router = CommandRouter(self, parent=self)
        self._router.unrecognized.connect(
            lambda t: self.voice_status("did not understand: '%s'" % t))
        self._voicebar = CommandBar(self)
        if self._root_layout is not None:
            self._root_layout.insertWidget(2, self._voicebar)

    def run_command(self, text: str) -> Tuple[bool, str]:
        """Run one map command (typed, or relayed by the Assistant).

        Returns ``(understood, reply)``. *reply* is what the pilot should be
        told: the lines the command "spoke" (see :meth:`speak`) if it spoke
        any, else its status message."""
        if self._router is None:
            return False, "map unavailable"
        self._said = []
        try:
            ok, msg = self._router.run(text)
        finally:
            said, self._said = self._said, None
        if msg:
            self.voice_status(msg)
        return ok, (" ".join(s for s in said if s) or msg)

    def handle_map_command(self, cmd: dict) -> None:
        """IPC ``map_command``: the Assistant heard something meant for the map.

        ``{"type": "map_command", "text": "zoom in", "id": "...", "reply_file": "..."}``.
        The reply (``{"id", "ok", "reply"}``) is appended to *reply_file*, so the
        Assistant can say what happened in its own voice; later narration from
        the in-game route macro goes to the same file as ``{"say": "..."}``."""
        try:
            text = str((cmd or {}).get("text") or "").strip()
            self._reply_file = safe_reply_file((cmd or {}).get("reply_file"))
            ident = (cmd or {}).get("id")
        except AttributeError:
            return
        if not text:
            self._send_reply({"id": ident, "ok": False, "reply": "that was an empty command"})
            return
        ok, reply = self.run_command(text)
        self._send_reply({"id": ident, "ok": bool(ok), "reply": reply})

    def voice_status(self, msg: str) -> None:
        vb = getattr(self, "_voicebar", None)
        if vb is not None:
            vb.set_status(msg)

    # command handlers (CommandRouter target)
    def cmd_help(self) -> str:
        lines = self._router.help_lines()
        return "commands: " + " | ".join(lines)

    def cmd_route_to(self, name: str) -> str:
        if self._galaxy is None:
            return "map unavailable"
        code = self._sys_code(name)
        if not code or self._galaxy_data is None or self._galaxy_data.get(code) is None:
            return "unknown system: %s" % name
        start = self._galaxy.home or getattr(self._galaxy, "_selected", None)
        self._go_galaxy()
        if start and start != code:
            self._galaxy.plot_route(start, code)
            self._btn_route.setText("Clear route")
            return "routing %s to %s" % (start, code)
        self._galaxy.center_on(code)
        return "showing %s (set a home system to route)" % code

    def cmd_clear_route(self) -> str:
        if self._shop_stops:
            self.clear_shopping_route()
            return "route cleared"
        if self._galaxy is not None and self._galaxy.route_active:
            self._galaxy.clear_route()
            self._btn_route.setText("Route")
            return "route cleared"
        return "no route to clear"

    def cmd_home(self) -> str:
        self._home_clicked()
        return "home"

    def cmd_galaxy(self) -> str:
        self._go_galaxy()
        return "galaxy"

    def cmd_back(self) -> str:
        self._go_back()
        return "back"

    def cmd_zoom(self, zoom_in: bool) -> str:
        if self._galaxy is None:
            return "map unavailable"
        cam = getattr(self._galaxy, "_cam", None)
        if cam is None:
            return "zoom unavailable"
        cam.zoom = min(11.0, max(0.16, cam.zoom * (1.25 if zoom_in else 0.8)))
        self._galaxy.update()
        return "zoomed %s" % ("in" if zoom_in else "out")

    def cmd_toggle_grocery(self) -> str:
        self._btn_grocery.setChecked(not self._btn_grocery.isChecked())
        return "grocery %s" % ("shown" if self._btn_grocery.isChecked() else "hidden")

    def cmd_goto(self, name: str) -> str:
        before = self._crumb.text()
        self.goto(name)
        after = self._crumb.text()
        if after != before or (self._galaxy is not None and self._galaxy._selected):
            return "going to %s" % name
        return "not found: %s" % name


    # ── scene navigation ──────────────────────────────────────────────────
    def _enter_system(self, code: str) -> None:
        s = self._galaxy_data.get(code) if self._galaxy_data else None
        name = s.name if s else code
        try:
            view = SystemView(code, name, self._bodies.get(code.upper(), []),
                              terminal_names=self._terminal_names())
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).warning("SystemView failed to build; staying on the current view",
                                            exc_info=True)
            return
        view.planetEntered.connect(lambda pn, c=code: self._enter_neighborhood(c, pn))
        view.drillIn.connect(lambda pn, c=code: self._enter_neighborhood(c, pn))
        view.drillOut.connect(self._go_back)
        view.terminalClicked.connect(self._open_location)
        view.jumpRequested.connect(self._jump_to_system)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        self._apply_shop_route(view, code)
        if not self._shop_stops and self._trade_route_pts and hasattr(view, "set_trade_route"):
            view.set_trade_route(self._route_pts_for(code.upper()))
        if hasattr(view, "set_overlay"):
            view.set_overlay(self._overlay, self._overlay_flags)
        self._push(f"{name.upper()} system", view)
        if not self._shop_stops and self._trade_route_pts:
            self._frame_route_in(view, code.upper())   # show/fit an active Trade Hub leg here

    def _enter_neighborhood(self, code: str, planet_name: str) -> None:
        try:
            view = PlanetSystemView(code, planet_name, self._bodies.get(code.upper(), []),
                                    terminal_names=self._terminal_names())
        except Exception:
            import logging as _lg
            _lg.getLogger(__name__).warning("PlanetSystemView failed to build; staying on the current view",
                                            exc_info=True)
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
            import logging as _lg
            _lg.getLogger(__name__).warning("PlanetView failed to build; staying on the current view",
                                            exc_info=True)
            return
        view.drillOut.connect(self._go_back)
        view.terminalClicked.connect(self._open_location)
        view.bodyActivated.connect(self._on_body_activated)
        view.loreRequested.connect(self._show_lore)
        self._push(body_name.upper(), view)

    def _jump_to_system(self, dest_name: str) -> None:
        # Deferred: emitted from the system view's own click handler —
        # navigating now would tear that view down mid-event.
        QTimer.singleShot(0, lambda d=dest_name: self._jump_to_system_now(d))

    def _jump_to_system_now(self, dest_name: str) -> None:
        code = self._sys_code(dest_name)
        if not code or self._galaxy_data is None or self._galaxy_data.get(code) is None:
            return
        self._go_galaxy()
        self._enter_system(code)

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
                      '🚀  robertsspaceindustries.com/en/pledge</a>')
        link.setTextFormat(Qt.RichText); link.setOpenExternalLinks(True)
        link.setAlignment(Qt.AlignCenter); link.setCursor(Qt.PointingHandCursor)
        link.setStyleSheet("font-family:Consolas; font-size:11pt; padding:4px;")
        lay.addWidget(link)
        self._cig_dlg = dlg               # keep a ref (non-modal)
        dlg.show(); dlg.adjustSize()
        c = self.mapToGlobal(self.rect().center())
        dlg.move(c.x() - dlg.width() // 2, c.y() - dlg.height() // 2)

    # ── location lore (right-click) ───────────────────────────────────────
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

    # ── terminals: combined commodities + items dialog ────────────────────
    def ensure_index(self) -> None:
        """Kick off the (once) terminal->items index build. Safe to call often."""
        try:
            self._index_loader.start()
        except (RuntimeError, OSError, AttributeError) as e:
            # start() only sets a flag and spawns a daemon thread, so the ways out are the OS refusing a new thread
            # (RuntimeError / OSError) or the loader being absent. Nothing retries: _on_index_done never fires, the
            # panel never says "items index empty" either, and every terminal shows no item prices for the rest of
            # the session with no explanation anywhere.
            log.warning("Starmap: the items index build did not start (%s: %s); no item prices this session",
                        type(e).__name__, e, exc_info=True)

    def _on_index_done(self, index: dict, source: str) -> None:
        self._items_index = index or {}
        try:
            self._market.set_index(self._items_index)
        except (RuntimeError, AttributeError, TypeError, KeyError) as e:
            # set_index refills three Qt widget models, so a deleted C++ object (RuntimeError) or an unexpected row
            # shape is what reaches here.
            # The consequence is a report that contradicts itself: this method carries on and calls voice_status
            # with "items index ready (live): N items at M places" a few lines below, so the panel ANNOUNCES a
            # loaded index while the Market view is still holding the old, empty one. A silent handler here does not
            # just lose the failure, it produces a false success message.
            log.warning("Starmap: the Market view rejected the new items index (%s: %s); it keeps the previous one "
                        "even though the status line will say the index is ready", type(e).__name__, e, exc_info=True)
        names = self._terminal_names()
        for _lbl, w in self._nav:
            if hasattr(w, "_terminals"):
                w._terminals = names
                w.update()
        # An open location card rendered "no item data" while the index was
        # still building: give its Items tab a second chance (Item Finder port).
        dlg = getattr(self, "_location_dlg", None)
        items_dlg = getattr(dlg, "_items_dlg", None) if dlg is not None else None
        if items_dlg is not None and hasattr(items_dlg, "refill"):
            try:
                items_dlg.refill()
            except RuntimeError:
                pass                      # the card was closed and its C++ side is gone
        src = {"live": "UEX live", "cache": "UEX cache",
               "offline": "offline"}.get(source, "")
        n_items, n_places = index_counts(self._items_index)
        if not n_places:
            self.voice_status("items index empty (%s): no item prices to show" % (src or "no data"))
        else:
            self.voice_status("items index ready (%s): %d items at %d places"
                              % (src, n_items, n_places))

    def _terminal_names(self) -> set:
        """Normalised names of every UEX terminal known to the items index,
        so map bodies that host one get a terminal badge + click (UEX names
        diverge from map body names, hence the alias pass).

        With a routes provider attached (ported from the Trade Hub star map),
        every commodity buy / sell location of those routes counts too."""
        names = set()
        for (_sys_n, term_n) in (self._items_index or {}).keys():
            if term_n:
                names.add(LOC_ALIASES.get(term_n, term_n))
        for r in self._routes():
            for loc in (getattr(r, "buy_location", ""), getattr(r, "sell_location", "")):
                if loc:
                    n = norm_loc(loc)
                    names.add(LOC_ALIASES.get(n, n))
        return names

    def _open_location(self, location: str, system: str) -> None:
        """Double-click a terminal body: combined commodities + items card."""
        self.ensure_index()
        hub = self._trade_hub
        dlg = LocationDialog(
            location, system,
            items_provider=lambda loc=location, sys=system: self._rows_for(loc, sys),
            on_popout=self._popout_item,
            parent=self,
            on_trade_routes=(None if hub is None else
                             (lambda loc=location, sys=system: self._plot_trade_routes(loc, sys))),
            on_commodity_routes=(None if hub is None else
                                 (lambda n, loc=location, sys=system:
                                  self._apply_trade_filters(buy_loc=loc, buy_sys=sys, commodity=n))),
            on_commodity_route=(None if hub is None else
                                (lambda n, dloc, dsys, loc=location, sys=system:
                                 self._apply_trade_filters(buy_loc=loc, buy_sys=sys, commodity=n,
                                                           sell_loc=dloc, sell_sys=dsys))))
        self._location_dlg = dlg          # keep a reference (non-modal)
        dlg.show()
        # Centre over the map so it doesn't hug the screen corner.
        tl = self.mapToGlobal(self.rect().topLeft())
        dlg.move(tl.x() + max(20, (self.width() - dlg.width()) // 2), tl.y() + 70)
        dlg.raise_()

    def _rows_for(self, location: str, system_code: str) -> List[dict]:
        """Resolve a map body to its sold-items rows (item dict + price)."""
        sysname = ""
        if self._galaxy_data is not None:
            s = self._galaxy_data.get(system_code)
            sysname = s.name if s else ""
        rows = []
        for e in items_at(self._items_index, location, sysname):
            item = {"id": e.get("item_id"),
                    "name": e.get("name") or "Item #%s" % e.get("item_id"),
                    "category": e.get("category") or "",
                    "price": e.get("price_buy") or 0,
                    "location": location,
                    "system": sysname}
            rows.append({"item": item, "price": item["price"]})
        return rows

    def _popout_item(self, item: dict) -> None:
        """Pop out a full detail bubble the user can drag onto the grocery list."""
        self._popouts = [p for p in self._popouts
                         if p.isVisible()]          # prune closed
        bub = ItemPopOut(item, on_add_to_grocery=self._grocery.add_item)
        bub.show()
        # Centre on the map window so it does not cover the row that spawned it.
        tl = self.mapToGlobal(self.rect().topLeft())
        bub.move(tl.x() + max(20, (self.width() - bub.width()) // 2),
                 tl.y() + 70)
        bub.raise_()
        self._popouts.append(bub)

    def _plot_grocery_route(self) -> None:
        """Grocery List's 'Plot shopping route'.

        Every item is pinned to the location it was added from, so the stops
        are fixed; they are ORDERED for the shortest trip
        (:func:`.route_planner.order_stops` over
        :func:`.distances.site_distance`, exact for up to 10 locations) and
        drawn in that order: the jump route across systems on the galaxy,
        numbered stops (via the gateways) inside each system.  A route that
        stays in one system opens straight into that system's view.
        """
        from .distances import site_distance
        from .route_planner import order_stops
        if self._galaxy is None:
            return
        stops = []
        for it in self._grocery.items():
            system = str(it.get("system") or "").strip()
            loc = str(it.get("location") or "").strip()
            if not system:
                continue
            stops.append({"item_id": it.get("id"), "name": it.get("name") or "",
                          "terminal": loc, "terminal_id": 0, "system": system,
                          "location": loc, "places": [loc] if loc else [],
                          "price": it.get("price") or 0})
        if not stops:
            self.clear_shopping_route()
            return
        self.plot_shopping_route(order_stops(stops, site_distance))

    def _grocery_changed(self) -> None:
        """Keep a plotted grocery route in step with the list."""
        if self._shop_stops:
            QTimer.singleShot(0, self._plot_grocery_route)

    def plot_shopping_route(self, stops: List[dict]) -> None:
        """Draw an ordered stop list (see :meth:`_plot_grocery_route`)."""
        if self._galaxy is None:
            return
        self._shop_stops = list(stops or [])
        if not self._shop_stops:
            self.clear_shopping_route()
            return
        seq = self._shop_system_seq()
        self._go_galaxy()
        if len(seq) >= 2:
            self._galaxy.plot_multi_route(seq)
        else:
            self._galaxy.clear_route()
            if seq:
                self._enter_system(seq[0])
                view = self._nav[-1][1]
                if hasattr(view, "frame_trade_route"):
                    view.frame_trade_route()
        self._btn_route.setText("Clear route")

    def has_shopping_route(self) -> bool:
        return bool(self._shop_stops)

    def clear_shopping_route(self) -> None:
        self._shop_stops = []
        if self._galaxy is not None:
            self._galaxy.clear_route()
        for _lbl, w in self._nav:
            if hasattr(w, "set_trade_route"):
                w.set_trade_route([])
        self._btn_route.setText("Route")

    def _shop_visits(self) -> List[dict]:
        from .route_planner import visits
        return visits(self._shop_stops)

    def _shop_system_seq(self) -> List[str]:
        """System codes in visit order, consecutive repeats collapsed."""
        seq: List[str] = []
        for v in self._shop_visits():
            c = self._sys_code(v["system"])
            if c and (not seq or seq[-1] != c):
                seq.append(c)
        return seq

    def shopping_route_points(self, code: str) -> list:
        """The in-system leg of the shopping route for system *code*:
        ``[(name, x, y, z, role)]`` in visit order, role ``"stop:<n>"`` for
        stop n (1 = first stop of the whole route) or ``"jump"`` for the
        gateway where the route arrives from / leaves for another system."""
        from .distances import jump_path, resolve_site
        vs = self._shop_visits()
        codes = [self._sys_code(v["system"]) for v in vs]
        gal = self._galaxy_data
        bodies = {b.name: b for b in self._bodies.get(code.upper(), [])}

        def gateway_to(other: str):
            path = jump_path(code, other) or [code, other]
            nxt = gal.get(path[1]) if gal is not None and len(path) > 1 else None
            b = bodies.get(f"{nxt.name} Gateway") if nxt is not None else None
            return (b.name, b.x, b.y, b.z, "jump") if b is not None else None

        pts: list = []
        for i, v in enumerate(vs):
            if codes[i] != code:
                continue
            if i > 0 and codes[i - 1] != code:
                g = gateway_to(codes[i - 1])
                if g is not None:
                    pts.append(g)
            _c, body = resolve_site(v["system"], v["places"])
            label = (v["places"] or [v.get("location") or "?"])[0]
            if body is not None:
                pts.append((label, body.x, body.y, body.z, f"stop:{i + 1}"))
            if i + 1 < len(vs) and codes[i + 1] != code:
                g = gateway_to(codes[i + 1])
                if g is not None:
                    pts.append(g)
        return pts

    def _apply_shop_route(self, view, code: str) -> None:
        if self._shop_stops and hasattr(view, "set_trade_route"):
            view.set_trade_route(self.shopping_route_points(code))

    # ── search / snap-to-destination ──────────────────────────────────────
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
        """Snap the map to a system or location by name (search bar, voice)."""
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

    def _sys_code(self, sysname: str) -> str:
        if not sysname:
            return ""
        if self._galaxy_data is not None:
            for s in self._galaxy_data.systems:
                if s.name.lower() == sysname.lower() or s.code.lower() == sysname.lower():
                    return s.code
        return sysname.upper()

    # ── nav stack ─────────────────────────────────────────────────────────
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
        self._crumb.setText("   >   ".join(lbl for lbl, _ in self._nav))
        if hasattr(self, "_btn_route"):
            self._btn_route.setEnabled(not deep)

    # ── home ──────────────────────────────────────────────────────────────
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
        menu.addAction("Change home system...", self._pick_home)
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

    # ── routing ───────────────────────────────────────────────────────────
    def _route_clicked(self) -> None:
        g = self._galaxy
        if g is None:
            return
        if self._shop_stops:
            self.clear_shopping_route()
            return
        if self._trade_route_pts:
            self.clear_trade_route()
            return
        if g.route_active:
            g.clear_route()
            self._btn_route.setText("Route")
        else:
            g.start_route()
            self._btn_route.setText("Cancel")

    # ── replies (said by the Assistant, never by the map) ─────────────────
    @staticmethod
    def _cline(key: str, default: str, **fields) -> str:
        """A fixed spoken line in the chosen character's words (shared/character_voice.LINES); the plain
        wording if the shared module is unavailable."""
        try:
            from shared.character_voice import line
            return line(key, **fields)
        except ImportError as e:
            # The documented case: "the plain wording if the shared module is unavailable". Expected when the skill
            # runs outside the toolbox, so debug.
            log.debug("Starmap: shared.character_voice unavailable (%s); using the plain wording for %r", e, key)
            return default
        except (KeyError, IndexError, ValueError, TypeError) as e:
            # The module IS there and could not render the line: a key it has no entry for, or a template whose
            # format fields do not match what this call site passes. The fallback is the plain English wording, which
            # is a perfectly normal-sounding sentence - so the pilot's chosen character quietly stops being the voice
            # of the star map and nothing at all indicates that a line is missing or a template is wrong.
            log.warning("Starmap: character line %r failed to render (%s: %s); falling back to the plain wording",
                        key, type(e).__name__, e)
            return default

    def speak(self, text: str) -> None:
        """Hand a spoken line to whoever is talking to the map. The map itself is mute.

        The Star Map had its own TTS mouth while it had its own ears. Both are
        the Assistant's now, and the map must not talk on its own: with the
        Assistant's mic open, a line said by the map comes straight back in as
        the pilot's next utterance ("Navigate to Area 18" would re-run itself).
        The Assistant marks its OWN speech so its ears skip it; it cannot do
        that for a second voice in another process.

          * while a command is running, the line becomes that command's reply;
          * afterwards (the in-game route macro narrates its steps later) it is
            sent to the Assistant that issued the last command, which says it;
          * with nobody listening it is dropped - callers put the same text on
            the status line themselves.
        """
        if not text:
            return
        if self._said is not None:
            self._said.append(text)
            return
        self._send_reply({"say": text})

    def _send_reply(self, payload: dict) -> bool:
        """Append *payload* to the Assistant's reply file, if it gave one."""
        path = self._reply_file
        if not path:
            return False
        try:
            from shared.ipc import ipc_write
            return bool(ipc_write(path, payload))
        except Exception as e:
            # LEFT BROAD on purpose: this runs on the map's command path and on the route
            # macro's worker thread, and a reply that cannot be written (the Assistant
            # closed and its temp file went with it, a lock timeout) must not become
            # "the map command failed". It is reported, because the Assistant is then
            # waiting for an answer that will never arrive and will say so itself.
            log.warning("Starmap: could not answer the Assistant at %s (%s: %s)",
                        path, type(e).__name__, e)
            return False

    def _route_status(self, msg: str) -> None:
        """Macro step narration: show it on the status line and say it."""
        self.voice_status(msg)
        self.speak(msg)

    def _route_done(self, msg: str) -> None:
        self.voice_status(msg)
        spoken = msg[len("error:"):].strip() if msg.startswith("error:") else msg
        self.speak(spoken)

    # ── side views (grocery / market finder / commodities) ────────────────
    def _side_toggled(self, key: str, on: bool) -> None:
        """At most one side view is visible; a checked button shows its own."""
        panels = getattr(self, "_side_panels", {})
        buttons = getattr(self, "_side_buttons", {})
        if on:
            for k, w in panels.items():
                w.setVisible(k == key)
            for k, b in buttons.items():
                if k != key and b.isChecked():
                    b.blockSignals(True)
                    b.setChecked(False)
                    b.blockSignals(False)
        elif key in panels:
            panels[key].setVisible(False)

    def _show_side(self, key: str) -> str:
        """Voice-friendly toggle for the named side view."""
        btn = getattr(self, "_side_buttons", {}).get(key)
        if btn is None:
            return "view unavailable"
        btn.setChecked(not btn.isChecked())
        return "%s %s" % (key, "shown" if btn.isChecked() else "hidden")

    def cmd_toggle_market(self) -> str:
        return self._show_side("market")

    def cmd_toggle_commodities(self) -> str:
        return self._show_side("commodities")

    # ── commodity pages (Trade Hub clone) ─────────────────────────────────
    def _open_commodity(self, name: str) -> None:
        """Open the UEX-style commodity page from the commodities grid."""
        try:
            from .commodity_view import CommodityView
        except Exception as exc:
            self.voice_status("commodity page unavailable: %s" % exc)
            return
        self._comm_views = [v for v in self._comm_views if v.isVisible()]
        view = CommodityView(str(name), parent=self)
        self._comm_views.append(view)
        view.show()
        tl = self.mapToGlobal(self.rect().topLeft())
        view.move(tl.x() + max(20, (self.width() - view.width()) // 2),
                  tl.y() + 60)
        view.raise_()

    # ── IPC (toolbox hotkey re-show / hide / quit) ────────────────────────
    def _on_ipc(self, cmd: dict) -> None:
        try:
            kind = (cmd or {}).get("type", "")
        except AttributeError:
            return
        app = QApplication.instance()
        if kind == "show":
            win = self.window()
            if win is not None:
                win.show()
                win.raise_()
                win.activateWindow()
        elif kind == "hide":
            win = self.window()
            if win is not None:
                win.hide()
        elif kind == "quit" and app is not None:
            app.quit()
        elif kind == "map_command":
            self.handle_map_command(cmd)

    # ── persistence ───────────────────────────────────────────────────────
    def _save_soon(self) -> None:
        if hasattr(self, "_save_timer"):
            self._save_timer.start()

    def save_state(self) -> None:
        if self._galaxy is None:
            return
        st = load_state()
        st["galaxy"] = self._galaxy.get_state()
        if hasattr(self, "_btn_game"):
            st["game_route"] = self._btn_game.isChecked()
        st["overlay_flags"] = dict(self._overlay_flags)
        # "ears" and "voice" in the state file are the mic settings from when the
        # map had its own ears. They are NOT written any more and deliberately not
        # removed either: st is load_state() with this panel's keys laid over it,
        # so they ride along untouched, and the Assistant reads them once to carry
        # the pilot's choices over (assistant/starmap_bridge.py).
        save_state(st)

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._restored or self._galaxy is None:
            return
        self._restored = True
        self.ensure_index()               # build the items index off-thread
        if self._galaxy.home is None:
            QTimer.singleShot(0, self._first_run_home)

    def _first_run_home(self) -> None:
        if self._galaxy is not None and self._galaxy.home is None:
            self._pick_home()

    def shutdown(self) -> None:
        """Persist state and tear down bubbles / watchers."""
        # THE HANDLER IN THIS METHOD IS LEFT BROAD, for a reason that belongs to the method: the LAST statement is
        # self.save_state(). An exception escaping from the IPC watcher skips it, and the pilot loses the home
        # system and the panel toggles - a data loss caused by a failure to shut down a background thread. What was
        # missing was any record that a thread outlived the panel.
        # (The voice mouth and ears that used to be stopped here are gone: the map has neither any more.)
        self._reply_file = ""
        if self._lore_bubble is not None:
            self._lore_bubble.close()
            self._lore_bubble = None
        for p in getattr(self, "_popouts", []):
            p.close()
        self._popouts.clear()
        if getattr(self, "_location_dlg", None) is not None:
            self._location_dlg.close()
            self._location_dlg = None
        if self._ipc is not None:
            try:
                self._ipc.stop()
            except Exception as e:
                log.warning("Starmap shutdown: the IPC watcher did not stop (%s: %s); its poll thread may still be "
                            "reading the command file", type(e).__name__, e, exc_info=True)
        self.save_state()


    # ══ Trade Hub star map features (ported 2026-10-03 for the Everything Finder) ══
    # Source: Trade_Hub/starmap/panel.py. Kept behaviourally identical; the only
    # change is how the routes arrive - through a provider callable instead of a
    # hard reference to the Trade Hub window - so the standalone tool, which has
    # no route engine, is untouched until a host attaches one.

    def set_routes_provider(self, provider: Optional[Callable[[], list]],
                            career_provider: Optional[Callable[[], object]] = None) -> None:
        """Attach the Trade Hub's routes (and optionally its career ledger).

        Shows the OVERLAYS sidebar and rebuilds the overlay; passing None hides
        it again. Call :meth:`on_routes_loaded` whenever the routes change."""
        self._routes_provider = provider
        self._career_provider = career_provider
        if self._overlay_sidebar is not None:
            self._overlay_sidebar.setVisible(provider is not None)
        if provider is None:
            self._overlay = None
            self._push_overlay()
        else:
            self.on_routes_loaded()

    def set_trade_hub(self, hub) -> None:
        """Attach a live Trade Hub window: link-through to its routes table, and
        snap the map when its buy / sell location pickers change (as the Trade
        Hub star map does). Also attaches its routes as the overlay source."""
        self._trade_hub = hub
        for attr in ("_buy_loc", "_sell_loc"):
            combo = getattr(hub, attr, None)
            sig = getattr(combo, "item_selected", None)
            if sig is not None:
                try:
                    sig.connect(self.goto)
                except (RuntimeError, TypeError):
                    pass
        self.set_routes_provider(
            lambda h=hub: list(getattr(h, "_all_routes", None) or []),
            career_provider=lambda h=hub: getattr(h, "_career", None))

    def _routes(self) -> list:
        prov = self._routes_provider
        if prov is None:
            return []
        try:
            return list(prov() or [])
        except Exception:          # a host's provider must never break the map
            log.warning("Starmap: the routes provider raised; treating it as no routes", exc_info=True)
            return []

    def on_routes_loaded(self) -> None:
        """Route data (re)loaded - refresh terminal badges and rebuild the overlay."""
        names = self._terminal_names()
        for _lbl, w in self._nav:
            if hasattr(w, "_terminals"):
                w._terminals = names
                w.update()
        self._rebuild_overlay()

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
        if self._routes_provider is None:
            return
        routes = self._routes()
        career = None
        if self._career_provider is not None:
            try:
                career = self._career_provider()
            except Exception:
                career = None

        def work(routes=routes, career=career) -> None:
            try:
                from .trade_overlay import TradeOverlay, terminal_recency
                cmap = career.map_data(self._sys_code) if career else None
                ov = TradeOverlay(routes, self._sys_code, terminal_recency(), career=cmap)
            except Exception:
                log.warning("Starmap: the trade overlay did not build; overlays stay as they were",
                            exc_info=True)
                ov = None
            self._overlay_ready.emit(ov)
        threading.Thread(target=work, daemon=True, name="TradeOverlayBuild").start()

    def _apply_overlay(self, ov) -> None:
        if ov is None:
            return
        self._overlay = ov
        self._push_overlay()

    # trade-route overlay (mirror a calculated Trade Hub route)
    def show_route(self, waypoints) -> None:
        """Draw one calculated route. ``waypoints = [(location, system, role), ...]``
        with role "buy" / "sell". Deferred one tick: this is often called from
        inside a click handler, and navigating scenes there tears widgets down
        mid-event."""
        QTimer.singleShot(0, lambda wp=list(waypoints): self._show_route_now(wp))

    def _show_route_now(self, waypoints) -> None:
        """Intra-system: a buy->sell line in that system. Cross-system: the galaxy
        jump path PLUS a leg inside EACH endpoint system (buy->jump-gateway in the
        origin, jump-gateway->sell in the destination)."""
        if self._shop_stops:
            self.clear_shopping_route()
        pts = []
        for loc, sysname, role in waypoints:
            code = self._sys_code(sysname)
            body = self._find_body(code, loc)
            if body is not None:
                pts.append((code, loc, body.x, body.y, body.z, role))
            else:
                pts.append((code, loc, None, None, None, role))
        uniq = list(dict.fromkeys(c for (c, _n, x, _y, _z, _r) in pts if c and x is not None))
        unres = [loc for (c, loc, x, _y, _z, _r) in pts if x is None]
        if unres:
            log.warning("Starmap route: %d/%d waypoints UNRESOLVED on map: %s",
                        len(unres), len(pts), " | ".join(unres))
        if len(uniq) >= 2:
            pts = self._inject_gateways(pts)
        self._trade_route_pts = pts
        self._go_galaxy()
        if self._galaxy is None or not uniq:
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
        self._btn_route.setText("Clear route")

    def has_trade_route(self) -> bool:
        return bool(self._trade_route_pts)

    def clear_trade_route(self) -> None:
        self._trade_route_pts = []
        if self._galaxy is not None:
            self._galaxy.clear_route()
        for _lbl, w in self._nav:
            if hasattr(w, "set_trade_route"):
                w.set_trade_route([])
        self._btn_route.setText("Route")

    def _frame_route_in(self, view, code: str) -> None:
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
        the OTHER endpoint ('Pyro Gateway' in Stanton <-> 'Stanton Gateway' in Pyro)."""
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

    def _find_body(self, code: str, loc: str):
        """Resolve a UEX terminal/location name to a positioned body, in escalating
        leniency: exact -> normalised-exact (+ alias) -> guarded normalised substring."""
        bl = self._bodies.get(code, [])
        if not loc:
            return None
        raw = loc.lower()
        for b in bl:
            if b.name.lower() == raw:
                return b
        n = norm_loc(loc)
        n = LOC_ALIASES.get(n, n)
        if not n:
            return None
        for b in bl:
            if norm_loc(b.name) == n:
                return b
        cands = []
        for b in bl:
            bn = norm_loc(b.name)
            if bn and len(n) >= 4 and (n in bn or bn in n):
                cands.append(b)
        if cands:
            return max(cands, key=lambda b: len(b.name))
        return None

    # link-through to the live Trade Hub routes table
    def _plot_trade_routes(self, location: str, system: str) -> None:
        """All Trade Hub routes FROM this terminal."""
        self._apply_trade_filters(buy_loc=location, buy_sys=system)

    def _apply_trade_filters(self, buy_loc: str = "", buy_sys: str = "", commodity: str = "",
                             sell_loc: str = "", sell_sys: str = "") -> None:
        QTimer.singleShot(0, lambda: self._apply_trade_filters_now(
            buy_loc, buy_sys, commodity, sell_loc, sell_sys))

    def _apply_trade_filters_now(self, buy_loc: str, buy_sys: str, commodity: str,
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
        except (AttributeError, RuntimeError, TypeError):
            log.warning("Starmap: could not apply the Trade Hub route filters", exc_info=True)
            return
        self.tradeHubRequested.emit()

    # ── repurposed set_route_ai: spoken destinations + in-game plotting ───
    def _engine(self):
        """Lazy DestinationPhoneticEngine (set_route_ai port)."""
        if self._dest_engine is None:
            try:
                from .set_route.destination_engine import DestinationPhoneticEngine
                self._dest_engine = DestinationPhoneticEngine()
            except Exception:
                self._dest_engine = None
        return self._dest_engine

    def _setter(self):
        if self._setter_obj is None:
            try:
                from .set_route.route_setter import InGameRouteSetter
                self._setter_obj = InGameRouteSetter()
            except Exception:
                self._setter_obj = None
        return self._setter_obj

    def cmd_set_route(self, name: str) -> str:
        """'set route to X' / 'navigate to X' — resolve X with the ported
        set_route phonetic engine, mirror it on the map, speak the
        confirmation ("Navigate to X"), and (when In-Game is toggled on)
        drive the in-game starmap macro like the Wingman skill did,
        narrating each step and the result."""
        engine = self._engine()
        if engine is None:
            return "destination engine unavailable"
        dest, alts = engine.find_destination(name)
        if alts:
            self.speak(self._cline("which", "Which one? " + ", ".join(alts[:3]), options=", ".join(alts[:3])))
            return "which one? " + ", ".join(alts[:6])
        if not dest:
            self.speak(self._cline("unknown", "Unknown destination: %s" % name, name=name))
            return "unknown destination: %s" % name
        # Mirror on our own map when the destination matches a known place.
        try:
            self.goto(dest.title())
        except (RuntimeError, AttributeError, TypeError, KeyError, ValueError) as e:
            # goto() switches the stacked view and may build a system/planet scene, so a dead C++ object
            # (RuntimeError) or an unknown place name is what reaches here. It is deliberately non-fatal: the
            # in-game plotting below is the actual job and must still run when our own map cannot follow along.
            # But the very next line says "Navigate to <dest>" out loud and the method goes on to report success, so
            # swallowing this means the panel announces a navigation whose map never moved.
            log.warning("Starmap: could not mirror %r on the panel's own map (%s: %s); the spoken confirmation and "
                        "the in-game plot go ahead anyway", dest, type(e).__name__, e, exc_info=True)
        self.speak(self._cline("navigate", "Navigate to %s" % dest, dest=dest))
        if getattr(self, "_btn_game", None) is not None and self._btn_game.isChecked():
            setter = self._setter()
            if setter is None:
                return "route setter unavailable"
            if not setter.available():
                return "in-game plotting needs pynput (pip install pynput)"
            if setter.busy():
                return "already setting a route"
            setter.set_route(dest, status_cb=self._route_status,
                             done_cb=self._route_done)
            return "setting route to %s in game" % dest
        return "route to %s (toggle 'In-Game' on to plot it in the game)" % dest

    def _route_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Calibrate in-game route setter...", self._calibrate)
        menu.exec(self._btn_route.mapToGlobal(pos))

    def _calibrate(self) -> None:
        try:
            from .set_route.route_setter import RouteCalibrationDialog
        except Exception as exc:
            self.voice_status("calibration unavailable: %s" % exc)
            return
        dlg = RouteCalibrationDialog(self)
        if dlg.exec() and dlg.result_ready:
            self.voice_status("route setter calibrated")
        else:
            self.voice_status("calibration cancelled")

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
            f"color: {ACCENT}; font-family: Consolas; font-size: 13pt; font-weight: bold;")
        sub = QLabel("Snap-to-home returns here. You can change it later (right-click Home).")
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
                f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }}")
            btn.clicked.connect(lambda _=False, c=code: self._choose(c))
            grid.addWidget(btn, i // 3, i % 3)
        lay.addLayout(grid)

    def _choose(self, code: str) -> None:
        self.choice = code
        self.accept()
