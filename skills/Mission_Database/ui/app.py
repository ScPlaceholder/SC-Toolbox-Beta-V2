"""Main application window (PySide6) — delegates page content to page modules."""
from __future__ import annotations
import json
import logging
import os
import sys
import threading
import webbrowser

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QStackedWidget, QFrame,
)

from shared.i18n import s_ as _
from shared.qt.theme import P
from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.ipc_thread import IPCWatcher
from data.manager import MissionDataManager
from data import api, cache
from services.inventory import InventoryService
from ui.pages.missions import MissionsPage
from ui.pages.fabricator import FabricatorPage
from ui.pages.resources import ResourcesPage
from ui.pages.owned_blueprints import OwnedBlueprintsPage
from ui.modals.keybind_dialog import show_keybind_dialog

log = logging.getLogger(__name__)


class _ThreadSignal(QObject):
    """Thread-safe signal bridge: emit from any thread, slot runs on main thread."""
    fire = Signal()
    fire_str = Signal(str)
    fire_object = Signal(object)


class MissionDBApp(SCWindow):
    """Main application window — scmdb.net visual clone (PySide6)."""

    def __init__(self, x, y, w, h, opacity, cmd_file) -> None:
        super().__init__(
            title="SC SCMDB // Mission/Crafting Database",
            width=w, height=h, min_w=800, min_h=500,
            opacity=opacity, always_on_top=True,
        )
        self.restore_geometry_from_args(x, y, w, h, opacity)
        self._cmd_file = cmd_file
        self._data = MissionDataManager()
        self._inventory = InventoryService()
        self._active_channel = "live"

        # Thread-safe signals for cross-thread UI updates
        self._sig_data_loaded = _ThreadSignal(self)
        self._sig_data_loaded.fire.connect(self._on_data_loaded)
        self._sig_crafting_loaded = _ThreadSignal(self)
        self._sig_crafting_loaded.fire.connect(self._on_crafting_loaded)
        self._sig_mining_loaded = _ThreadSignal(self)
        self._sig_mining_loaded.fire.connect(self._on_mining_loaded)
        self._sig_status = _ThreadSignal(self)
        self._sig_status.fire_str.connect(self._status_label_set)
        self._sig_version = _ThreadSignal(self)
        self._sig_version.fire_str.connect(self._version_label_set)
        self._sig_apply = _ThreadSignal(self)
        self._sig_apply.fire_object.connect(self._run_on_main)

        self._build_ui()
        self._start_ipc()
        self._data.load(on_done=lambda: self._sig_data_loaded.fire.emit())

    def _build_ui(self):
        layout = self.content_layout

        # ── Title bar ──
        self._title_bar = SCTitleBar(
            self, title=_("SCMDB // MISSION DATABASE"),
            icon_text="SC", accent_color=P.tool_mission,
            show_minimize=False,
            extra_buttons=[("? Tutorial", self._show_tutorial)],
        )
        self._title_bar.close_clicked.connect(self.close)
        layout.addWidget(self._title_bar)

        # ── Header bar ──
        header = QWidget()
        header.setFixedHeight(36)
        header.setStyleSheet(f"background-color: {P.bg_header};")
        hdr_layout = QHBoxLayout(header)
        hdr_layout.setContentsMargins(10, 0, 10, 0)
        hdr_layout.setSpacing(8)

        # Discord link
        discord_btn = QPushButton(_("Discord: SCMDB"))
        discord_btn.setCursor(Qt.PointingHandCursor)
        discord_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: #7289da; border: none;
                          font-family: Consolas; font-size: 8pt; }}
            QPushButton:hover {{ color: #99aaee; }}
        """)
        discord_btn.clicked.connect(lambda: webbrowser.open("https://discord.gg/qbDQBvSzPN"))
        hdr_layout.addWidget(discord_btn)

        hdr_layout.addStretch(1)

        # Status
        self._status_label = QLabel(_("Loading data..."))
        self._status_label.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim}; background: transparent;")
        hdr_layout.addWidget(self._status_label)

        # Keybind button
        self._keybind_file = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            ".keybind.json")
        self._keybind = self._load_keybind()
        keybind_text = self._keybind or _("Set hotkey")
        self._keybind_btn = QPushButton(f"\u2328 {keybind_text}")
        self._keybind_btn.setCursor(Qt.PointingHandCursor)
        self._keybind_btn.setStyleSheet(f"""
            QPushButton {{ background: transparent; color: {P.fg_disabled}; border: none;
                          font-family: Consolas; font-size: 7pt; }}
            QPushButton:hover {{ color: {P.fg_dim}; }}
        """)
        self._keybind_btn.clicked.connect(self._set_keybind_dialog)
        hdr_layout.addWidget(self._keybind_btn)

        # LIVE/PTU toggle
        self._live_btn = QPushButton(_("LIVE"))
        self._live_btn.setCursor(Qt.PointingHandCursor)
        self._live_btn.clicked.connect(lambda: self._switch_version("live"))
        hdr_layout.addWidget(self._live_btn)

        self._ptu_btn = QPushButton(_("PTU"))
        self._ptu_btn.setCursor(Qt.PointingHandCursor)
        self._ptu_btn.clicked.connect(lambda: self._switch_version("ptu"))
        hdr_layout.addWidget(self._ptu_btn)

        self._update_ver_btn_style()

        # Version label
        self._version_label = QLabel(_("Loading..."))
        self._version_label.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_disabled}; background: transparent;")
        hdr_layout.addWidget(self._version_label)

        layout.addWidget(header)

        # ── Page navigation bar ──
        nav = QWidget()
        nav.setFixedHeight(34)
        nav.setStyleSheet(f"background-color: {P.bg_secondary};")
        nav_layout = QHBoxLayout(nav)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)

        self._page_btns = {}
        self._current_page = "missions"
        for page_key, page_label in [("missions", "\U0001f4cb " + _("Missions")),
                                      ("fabricator", "\U0001f527 " + _("Fabricator")),
                                      ("resources", "\u26cf " + _("Resources")),
                                      ("owned", "\U0001f4be " + _("Owned Blueprints"))]:
            btn = QPushButton(page_label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_secondary}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 4px 14px; }}
                QPushButton:hover {{ color: {P.fg}; }}
            """)
            btn.clicked.connect(lambda checked=False, pk=page_key: self._switch_page(pk))
            nav_layout.addWidget(btn)
            self._page_btns[page_key] = btn
        nav_layout.addStretch(1)

        planner_btn = QPushButton("\u2191 " + _("Rank Planner"))
        planner_btn.setCursor(Qt.PointingHandCursor)
        planner_btn.setStyleSheet(f"""
            QPushButton {{ background: {P.bg_card}; color: {P.accent}; border: none;
                          font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 4px 12px; }}
            QPushButton:hover {{ color: {P.fg_bright}; background: #1a2a30; }}
        """)
        planner_btn.clicked.connect(self._open_rank_planner)
        nav_layout.addWidget(planner_btn)

        layout.addWidget(nav)
        self._update_page_btn_style()

        # ── Stacked pages ──
        self._stack = QStackedWidget()
        layout.addWidget(self._stack, 1)

        self._missions = MissionsPage(self._stack, self._data)
        self._stack.addWidget(self._missions)

        self._fabricator = None
        self._fab_idx = -1
        self._resources = None
        self._res_idx = -1
        self._owned_page = None
        self._owned_idx = -1

    # ── Page navigation ──

    def _update_page_btn_style(self):
        for key, btn in self._page_btns.items():
            if key == self._current_page:
                btn.setStyleSheet(f"""
                    QPushButton {{ background: #1a2a30; color: {P.tool_mission}; border: none;
                                  font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 4px 14px; }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{ background: {P.bg_secondary}; color: {P.fg_dim}; border: none;
                                  font-family: Consolas; font-size: 9pt; font-weight: bold; padding: 4px 14px; }}
                    QPushButton:hover {{ color: {P.fg}; }}
                """)

    def _switch_page(self, page_key):
        if page_key == self._current_page:
            return
        self._current_page = page_key
        self._update_page_btn_style()

        if page_key == "missions":
            self._stack.setCurrentWidget(self._missions)

        elif page_key == "fabricator":
            if self._fabricator is None:
                self._fabricator = FabricatorPage(
                    self._stack, self._data,
                    on_open_detail=self._open_blueprint_detail_q)
                self._fab_idx = self._stack.addWidget(self._fabricator)
            self._stack.setCurrentIndex(self._fab_idx)
            if not self._data.is_crafting_loaded() and self._data.is_data_loaded():
                self._status_label.setText(_("Loading crafting data..."))
                self._data.load_crafting(
                    on_done=lambda: self._sig_crafting_loaded.fire.emit())

        elif page_key == "resources":
            if self._resources is None:
                self._resources = ResourcesPage(self._stack, self._data)
                self._res_idx = self._stack.addWidget(self._resources)
            self._stack.setCurrentIndex(self._res_idx)
            if not self._data.is_mining_loaded() and self._data.is_data_loaded():
                self._status_label.setText(_("Loading mining/resource data..."))
                self._data.load_mining(
                    on_done=lambda: self._sig_mining_loaded.fire.emit())

        elif page_key == "owned":
            if self._owned_page is None:
                self._owned_page = OwnedBlueprintsPage(
                    self._stack, self._data, self._inventory,
                    on_open_detail=self._open_blueprint_detail)
                self._owned_idx = self._stack.addWidget(self._owned_page)
            self._stack.setCurrentIndex(self._owned_idx)
            self._owned_page.refresh()
            if not self._data.is_crafting_loaded() and self._data.is_data_loaded():
                self._status_label.setText(_("Loading crafting data..."))
                self._data.load_crafting(
                    on_done=lambda: self._sig_crafting_loaded.fire.emit())

    def _open_blueprint_detail(self, bp: dict):
        self._open_blueprint_detail_q(bp, 750)

    def _open_blueprint_detail_q(self, bp: dict, quality: int):
        from ui.modals.blueprint_detail import BlueprintDetailModal
        BlueprintDetailModal(self.window(), bp, self._data,
                             inventory=self._inventory,
                             on_inventory_changed=self._on_inventory_changed,
                             initial_quality=quality)

    def _on_inventory_changed(self):
        if self._owned_page is not None:
            self._owned_page.refresh()

    # ── Data callbacks ──

    def _on_data_loaded(self):
        if self._data.error:
            self._status_label.setText(f"Error: {self._data.error}")
            return

        self._status_label.setText(_("Ready"))
        self._version_label.setText(self._data.version)

        ver_lower = self._data.version.lower()
        self._active_channel = "ptu" if "ptu" in ver_lower else "live"
        self._update_ver_btn_style()

        self._missions.populate_dropdowns()
        self._missions.on_filter_change()

        if self._current_page == "fabricator":
            self._status_label.setText(_("Loading crafting data..."))
            if self._fabricator:
                self._fabricator.set_count_message(_("Loading crafting data..."))
            self._data.load_crafting(
                on_done=lambda: self._sig_crafting_loaded.fire.emit())

        self._schedule_auto_refresh()

    def _on_crafting_loaded(self):
        if not self._data.crafting_loaded or not self._data.crafting_blueprints:
            ver = self._data.version or "?"
            is_live = "live" in ver.lower()
            if is_live:
                has_ptu = any("ptu" in v.get("version", "").lower()
                              for v in self._data.available_versions)
                if has_ptu:
                    self._status_label.setText(_("Fabricator requires PTU -- switching..."))
                    self._switch_version("ptu")
                    return
                msg = _("Fabricator has no data on LIVE -- switch to PTU for crafting blueprints")
            else:
                msg = _("No crafting data available for this version")
            self._status_label.setText(msg)
            if self._fabricator:
                self._fabricator.set_count_message(msg)
            return

        self._status_label.setText(_("Ready"))
        if self._fabricator:
            self._fabricator.on_filter_change()
        if self._owned_page is not None:
            self._owned_page.maybe_auto_scan()
            self._owned_page.refresh()

    def _on_mining_loaded(self):
        if not self._data.mining_loaded:
            self._status_label.setText(_("Mining data not available for this version"))
            if self._resources:
                self._resources.set_count_message(_("No mining data for this version"))
            return

        self._status_label.setText(_("Ready"))
        if self._resources:
            self._resources.populate_resource_values()
            self._resources.on_filter_change()

    # ── Thread-safe slot helpers ──

    def _status_label_set(self, text: str):
        self._status_label.setText(text)

    def _version_label_set(self, text: str):
        self._version_label.setText(text)

    def _run_on_main(self, fn):
        """Execute an arbitrary callable on the main thread (via signal)."""
        fn()

    # ── Auto refresh ──

    def _schedule_auto_refresh(self):
        QTimer.singleShot(30 * 60 * 1000, self._check_auto_refresh)

    def _check_auto_refresh(self):
        if not self._data.is_data_loaded() or self._data.is_data_loading():
            self._schedule_auto_refresh()
            return

        def _do_check():
            fresh = api.fetch_versions()
            if not fresh:
                return
            channel = self._active_channel
            new_ver = None
            for v in fresh:
                if channel in v.get("version", "").lower():
                    new_ver = v.get("version", "")
                    break
            if new_ver and new_ver != self._data.version:
                def _apply():
                    self._data.available_versions = fresh
                    self._status_label.setText(f"Update found: {new_ver} -- refreshing...")
                self._sig_apply.fire_object.emit(_apply)
                self._data.load_version(
                    new_ver,
                    on_done=lambda: self._sig_data_loaded.fire.emit())

        threading.Thread(target=_do_check, daemon=True).start()
        self._schedule_auto_refresh()

    # ── Version switching ──

    def _update_ver_btn_style(self):
        if self._active_channel == "live":
            self._live_btn.setStyleSheet(f"""
                QPushButton {{ background: #1a3020; color: {P.green}; border: none;
                              font-family: Consolas; font-size: 8pt; font-weight: bold; padding: 1px 8px; }}
            """)
            self._ptu_btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 8pt; font-weight: bold; padding: 1px 8px; }}
            """)
        else:
            self._live_btn.setStyleSheet(f"""
                QPushButton {{ background: {P.bg_card}; color: {P.fg_dim}; border: none;
                              font-family: Consolas; font-size: 8pt; font-weight: bold; padding: 1px 8px; }}
            """)
            self._ptu_btn.setStyleSheet(f"""
                QPushButton {{ background: #1a2040; color: {P.yellow}; border: none;
                              font-family: Consolas; font-size: 8pt; font-weight: bold; padding: 1px 8px; }}
            """)

    def _show_tutorial(self):
        from ui.modals.tutorial import TutorialModal
        if hasattr(self, "_tutorial_modal") and self._tutorial_modal is not None:
            try:
                if self._tutorial_modal.isVisible():
                    self._tutorial_modal.raise_()
                    self._tutorial_modal.activateWindow()
                    return
            except RuntimeError:
                pass
        self._tutorial_modal = TutorialModal(self)

    def _open_rank_planner(self):
        if not self._data.is_data_loaded():
            self._status_label.setText("Data still loading...")
            return
        from ui.modals.rank_planner import RankPathPlannerModal
        RankPathPlannerModal(self, self._data)

    def _switch_version(self, channel: str):
        if channel == self._active_channel or self._data.loading:
            return
        self._active_channel = channel
        self._update_ver_btn_style()
        self._status_label.setText(f"Loading {channel.upper()} data...")
        self._missions.rebuild_cards_with([])
        if self._fabricator:
            self._fabricator.clear_grid()
            self._fabricator.set_count_message(_("Loading crafting data..."))
            self._data.set_crafting_loaded(False)

        def _do_switch():
            fresh = api.fetch_versions()
            if fresh:
                self._sig_apply.fire_object.emit(
                    lambda: setattr(self._data, "available_versions", fresh))
            target_ver = None
            versions = fresh if fresh is not None else self._data.available_versions
            for v in versions:
                ver = v.get("version", "")
                if channel.lower() in ver.lower():
                    target_ver = ver
                    break
            if not target_ver:
                self._sig_status.fire_str.emit(
                    f"No {channel.upper()} version available")
                return
            self._sig_version.fire_str.emit(f"\u2192 {target_ver}")
            self._data.load_version(
                target_ver,
                on_done=lambda: self._sig_data_loaded.fire.emit())

        threading.Thread(target=_do_switch, daemon=True).start()

    # ── Keybind ──

    def _load_keybind(self) -> str:
        try:
            if os.path.isfile(self._keybind_file):
                with open(self._keybind_file, encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("keybind", "")
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            log.warning("Failed to load keybind: %s", e)
        return ""

    def _save_keybind(self, keybind: str):
        try:
            with open(self._keybind_file, "w", encoding="utf-8") as f:
                json.dump({"keybind": keybind}, f)
        except (OSError, TypeError, ValueError) as e:
            log.warning("Failed to save keybind: %s", e)

    def _set_keybind_dialog(self):
        def _on_save(key):
            self._keybind = key
            self._save_keybind(key)
            self._keybind_btn.setText(f"\u2328 {key}")

        def _on_clear():
            self._keybind = ""
            self._save_keybind("")
            self._keybind_btn.setText("\u2328 " + _("Set hotkey"))

        show_keybind_dialog(self, self._keybind, _on_save, _on_clear)

    # ── IPC ──

    def _start_ipc(self):
        if not self._cmd_file or self._cmd_file == os.devnull:
            return
        self._ipc = IPCWatcher(self._cmd_file)
        self._ipc.command_received.connect(self._dispatch)
        self._ipc.start()

    def _dispatch(self, cmd: dict):
        t = cmd.get("type", "")
        if t == "quit":
            self.close()
            import sys
            sys.exit(0)
        elif t == "show":
            self.show()
            self.raise_()
        elif t == "hide":
            self.hide()
        elif t == "refresh":
            self._status_label.setText("Refreshing...")
            try:
                os.remove(cache.default_cache_path())
            except OSError:
                pass
            self._data.set_loaded(False)
            self._data.load(
                on_done=lambda: self._sig_data_loaded.fire.emit())
        elif t == "search":
            query = cmd.get("query", "")
            self._missions.set_search(query)
        elif t == "filter":
            self._missions.apply_ipc_filter(cmd)

    def closeEvent(self, event) -> None:
        if hasattr(self, '_ipc'):
            self._ipc.stop()
        if self._owned_page is not None:
            try:
                self._owned_page.stop_watcher()
            except Exception:
                log.exception("failed to stop owned-blueprints watcher")
        super().closeEvent(event)
