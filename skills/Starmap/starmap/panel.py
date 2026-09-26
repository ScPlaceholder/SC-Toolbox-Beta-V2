"""StarmapPanel — the merged standalone star map.

Combines the Trade Hub star map (galaxy -> system -> planet globe scenes,
home system, lore bubbles, route plotting) with the Market Finder star
map (terminal -> items browsing, item pop-outs, grocery list, multi-stop
shopping routes), and adds voice-command ears (FastWhisper behind a
keyboard / mouse / joystick / gamepad trigger).

Terminal clicks open the combined :class:`LocationDialog` (commodities
+ items tabs). The grocery list docks on the right; its "Plot shopping
route" draws the multi-stop jump route on the galaxy view.

Everything is defensive: if data/scene construction fails the panel
shows an inline message instead of dying.
"""
from __future__ import annotations

from typing import Callable, Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
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
from .voice import missing_deps as voice_missing_deps
from .voice.ears import EarsController
from .voice.commands import CommandRouter
from .voice.input_devices import BindingCaptureDialog, InputBinding
from .voice.mouth import Mouth
from .voice_control import VoiceControlBar
from .commodities_view import CommoditiesView
from .market_view import MarketView

ACCENT = P.energy_cyan


def _btn_ss() -> str:
    return (
        f"QPushButton {{ background: {P.bg_card}; color: {P.fg}; "
        f"border: 1px solid {P.border}; padding: 5px 14px; "
        f"font-family: Consolas; font-size: 9pt; }} "
        f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {ACCENT}; }} "
        f"QPushButton:disabled {{ color: {P.fg_disabled}; border-color: {P.border}; }}"
    )


class StarmapPanel(QWidget):
    """Star map + location browser + grocery list + voice ears."""

    def __init__(self, parent: Optional[QWidget] = None,
                 cmd_file: str = "") -> None:
        super().__init__(parent)
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
        self._ipc = None
        self._dest_engine = None
        self._setter_obj = None
        self._mouth = None
        self._voice_replies = True
        self._voicebar = None
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
            self._build_ears()
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
        self._btn_game.setToolTip("Also plot voice routes inside Star Citizen (right-click Route to calibrate)")
        self._btn_game.toggled.connect(lambda _on: self._save_soon())
        try:
            self._btn_game.setChecked(bool(load_state().get("game_route")))
        except Exception:
            pass

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

    # ears (voice)
    def _build_ears(self) -> None:
        missing = voice_missing_deps()
        self._ears = EarsController(self)
        self._router = CommandRouter(self, parent=self)
        self._router.unrecognized.connect(
            lambda t: self.voice_status("did not understand: '%s'" % t))

        # restore config
        cfg = (load_state().get("ears") or {})
        b = cfg.get("binding")
        if isinstance(b, dict):
            self._ears.set_binding(InputBinding.from_dict(b))
        self._ears.set_mode(str(cfg.get("mode", "toggle")))
        self._ears.set_model(str(cfg.get("model", "small.en")))
        self._voice_replies = bool(
            (load_state().get("voice") or {}).get("replies", True))

        self._ears.statusChanged.connect(self.voice_status)
        self._ears.transcript.connect(self._on_transcript)
        self._ears.needsInstall.connect(self._on_ears_needs_install)

        # Speaks as the player's chosen character (Elah by default, set in the launcher's Settings); the old
        # Windows-voice Mouth stays as the fallback if the shared voice cannot load.
        try:
            from shared.character_voice import CharacterMouth
            self._mouth = CharacterMouth()
        except Exception:
            try:
                self._mouth = Mouth()
            except Exception:
                self._mouth = None

        # The interactive voice bar: ears toggle, mic mode (always on /
        # push-to-talk / toggle), mic keybind, star map calibration, spoken
        # replies and the status line.
        self._voicebar = VoiceControlBar(self)
        self._voicebar.set_replies(self._voice_replies)
        self._voicebar.sync_mode(self._ears.mode())
        if self._root_layout is not None:
            self._root_layout.insertWidget(2, self._voicebar)

        if missing:
            self._btn_ears.setEnabled(False)
            self._btn_ears.setToolTip(
                "Voice ears need: pip install " + " ".join(missing))
        else:
            self._refresh_ears_tooltip()

    def _refresh_ears_tooltip(self) -> None:
        b = self._ears.binding()
        self._btn_ears.setToolTip(
            "Toggle voice command ears (trigger: %s, %s) - right-click to configure"
            % (b.describe() if b else "not set", self._ears.mode()))

    def _arm_ears(self, on: bool) -> None:
        if on:
            if not self._ears.arm():
                self._btn_ears.setChecked(False)
        else:
            self._ears.disarm()

    def _ears_menu(self, pos) -> None:
        menu = QMenu(self)
        menu.addAction("Set trigger (press any key / button)...", self._pick_binding)
        mode_menu = menu.addMenu("Mode")
        for label, value in (("Toggle (press to talk, press to stop)", "toggle"),
                             ("Push-to-talk (hold)", "push")):
            act = mode_menu.addAction(label)
            act.setCheckable(True)
            act.setChecked(self._ears.mode() == value)
            act.triggered.connect(lambda _=False, v=value: self._set_mode(v))
        model_menu = menu.addMenu("Whisper model")
        for name in ("tiny.en", "base.en", "small.en", "medium.en"):
            act = model_menu.addAction(name)
            act.setCheckable(True)
            act.setChecked(self._ears_model_name() == name)
            act.triggered.connect(lambda _=False, n=name: self._set_model(n))
        menu.addAction("Voice commands help", self.cmd_voice_help)
        menu.exec(self._btn_ears.mapToGlobal(pos))

    def _ears_model_name(self) -> str:
        return self._ears._model_name

    def _set_mode(self, value: str) -> None:
        self._ears.set_mode(value)
        self._refresh_ears_tooltip()
        self._save_soon()

    def _set_model(self, name: str) -> None:
        self._ears.set_model(name)
        self._save_soon()

    def _pick_binding(self) -> None:
        dlg = BindingCaptureDialog(self)
        if dlg.exec() and dlg.result is not None:
            self._ears.set_binding(dlg.result)
            self._refresh_ears_tooltip()
            self.voice_status("ears trigger: %s" % dlg.result.describe())
            self._save_soon()

    def _on_ears_needs_install(self, missing: list) -> None:
        self._btn_ears.setChecked(False)
        self.voice_status("ears need: pip install " + " ".join(missing))

    def _on_transcript(self, text: str) -> None:
        self.voice_status('heard: "%s"' % text)
        self._router.dispatch(text)

    def voice_status(self, msg: str) -> None:
        vb = getattr(self, "_voicebar", None)
        if vb is not None:
            vb.set_status(msg)

    # command handlers (voice router target)
    def cmd_ears_off(self) -> str:
        self._btn_ears.setChecked(False)
        return "ears off"

    def cmd_voice_help(self) -> str:
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
        self._push(f"{name.upper()} system", view)

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
        except Exception:
            pass

    def _on_index_done(self, index: dict, source: str) -> None:
        self._items_index = index or {}
        try:
            self._market.set_index(self._items_index)
        except Exception:
            pass
        names = self._terminal_names()
        for _lbl, w in self._nav:
            if hasattr(w, "_terminals"):
                w._terminals = names
                w.update()
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
        diverge from map body names, hence the alias pass)."""
        names = set()
        for (_sys_n, term_n) in (self._items_index or {}).keys():
            if term_n:
                names.add(LOC_ALIASES.get(term_n, term_n))
        return names

    def _open_location(self, location: str, system: str) -> None:
        """Double-click a terminal body: combined commodities + items card."""
        self.ensure_index()
        dlg = LocationDialog(
            location, system,
            items_provider=lambda loc=location, sys=system: self._rows_for(loc, sys),
            on_popout=self._popout_item,
            parent=self)
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
        """Grocery List's 'Plot shopping route': multi-stop jump route through
        every system on the list."""
        if self._galaxy is None:
            return
        codes = [self._sys_code(s) for s in self._grocery.systems()]
        codes = [c for c in codes if c]
        if not codes:
            return
        self._go_galaxy()
        self._galaxy.plot_multi_route(codes)
        self._btn_route.setText("Clear route")

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
        if g.route_active:
            g.clear_route()
            self._btn_route.setText("Route")
        else:
            g.start_route()
            self._btn_route.setText("Cancel")

    # ── voice replies (TTS) ───────────────────────────────────────────────
    @staticmethod
    def _cline(key: str, default: str, **fields) -> str:
        """A fixed spoken line in the chosen character's words (shared/character_voice.LINES); the plain
        wording if the shared module is unavailable."""
        try:
            from shared.character_voice import line
            return line(key, **fields)
        except Exception:
            return default

    def speak(self, text: str) -> None:
        """Speak a confirmation when Voice Replies is on (never blocks)."""
        if getattr(self, "_voice_replies", True) and getattr(self, "_mouth", None):
            try:
                self._mouth.speak(text)
            except Exception:
                pass

    def _set_voice_replies(self, on: bool) -> None:
        self._voice_replies = bool(on)
        self._save_soon()

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
        st["voice"] = {"replies": bool(getattr(self, "_voice_replies", True))}
        if hasattr(self, "_ears"):
            b = self._ears.binding()
            st["ears"] = {
                "binding": b.to_dict() if b is not None else None,
                "mode": self._ears.mode(),
                "model": getattr(self._ears, "_model_name", "small.en"),
            }
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
        """Persist state and tear down bubbles / ears / watchers."""
        if getattr(self, "_mouth", None) is not None:
            try:
                self._mouth.stop()
            except Exception:
                pass
        if hasattr(self, "_ears"):
            try:
                self._ears.shutdown()
            except Exception:
                pass
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
            except Exception:
                pass
        self.save_state()


    # ── repurposed set_route_ai: voice destinations + in-game plotting ────
    def _engine(self):
        """Lazy DestinationPhoneticEngine (set_route_ai port)."""
        if self._dest_engine is None:
            try:
                from .voice.destination_engine import DestinationPhoneticEngine
                self._dest_engine = DestinationPhoneticEngine()
            except Exception:
                self._dest_engine = None
        return self._dest_engine

    def _setter(self):
        if self._setter_obj is None:
            try:
                from .voice.route_setter import InGameRouteSetter
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
        except Exception:
            pass
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
            from .voice.route_setter import RouteCalibrationDialog
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
