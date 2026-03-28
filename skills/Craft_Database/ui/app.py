"""Craft Database — main application window."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.search_bar import SCSearchBar
from shared.qt.theme import P
from shared.qt.ipc_thread import IPCWatcher

from data.repository import CraftRepository
from domain.models import Blueprint
from ui.constants import TOOL_COLOR, TOOL_NAME, POLL_MS, PAGE_SIZE
from ui.widgets import BlueprintGrid, PaginationBar
from ui.filter_panel import FilterPanel
from ui.detail_panel import BlueprintPopup

log = logging.getLogger(__name__)


class CraftDatabaseApp(SCWindow):
    """Main window for the Craft Database skill."""

    _data_ready = Signal()

    def __init__(self, x=100, y=100, w=1300, h=800, opacity=0.95, cmd_file=""):
        super().__init__(
            title=TOOL_NAME,
            width=w,
            height=h,
            min_w=900,
            min_h=500,
            opacity=opacity,
            always_on_top=True,
            accent=TOOL_COLOR,
        )
        self.move(x, y)
        self._cmd_file = cmd_file
        self._repo = CraftRepository()
        self._current_page = 1
        self._search_text = ""

        self._data_ready.connect(self._on_data_ready)

        self._build_ui()
        self._start_loading()

        if cmd_file:
            self._ipc = IPCWatcher(cmd_file)
            self._ipc.command_received.connect(self._handle_command)
            self._ipc.start()

    # ── UI construction ──────────────────────────────────────────────────

    def _build_ui(self):
        layout = self.content_layout
        layout.setSpacing(0)
        layout.setContentsMargins(0, 0, 0, 0)

        # Title bar
        self._title_bar = SCTitleBar(
            window=self,
            title=TOOL_NAME.upper(),
            icon_text="\U0001f3ed",
            accent_color=TOOL_COLOR,
            hotkey_text="Shift+7",
            extra_buttons=[("? Tutorial", self._show_tutorial)],
        )
        self._title_bar.close_clicked.connect(self.close)
        self._title_bar.minimize_clicked.connect(self.showMinimized)
        layout.addWidget(self._title_bar)

        # ── Stats bar
        self._stats_bar = QWidget()
        self._stats_bar.setStyleSheet(f"background: {P.bg_primary};")
        stats_lay = QHBoxLayout(self._stats_bar)
        stats_lay.setContentsMargins(12, 4, 12, 4)
        stats_lay.setSpacing(16)

        self._bp_count_lbl = QLabel("---")
        self._bp_count_lbl.setStyleSheet(
            f"color: {TOOL_COLOR}; font-size: 14pt; font-weight: bold;"
        )
        stats_lay.addWidget(self._bp_count_lbl)

        bp_desc = QLabel("BLUEPRINTS")
        bp_desc.setStyleSheet(f"color: {P.fg_dim}; font-size: 7pt; letter-spacing: 1px;")
        stats_lay.addWidget(bp_desc)

        self._ing_count_lbl = QLabel("---")
        self._ing_count_lbl.setStyleSheet(
            f"color: {TOOL_COLOR}; font-size: 14pt; font-weight: bold;"
        )
        stats_lay.addWidget(self._ing_count_lbl)

        ing_desc = QLabel("INGREDIENTS")
        ing_desc.setStyleSheet(f"color: {P.fg_dim}; font-size: 7pt; letter-spacing: 1px;")
        stats_lay.addWidget(ing_desc)

        stats_lay.addStretch()

        self._version_lbl = QLabel("")
        self._version_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt;")
        stats_lay.addWidget(self._version_lbl)

        layout.addWidget(self._stats_bar)

        # ── Main body (filter panel | content)
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_lay = QHBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)

        # Filter panel (left)
        self._filter_panel = FilterPanel()
        self._filter_panel.filters_changed.connect(self._on_filters_changed)
        body_lay.addWidget(self._filter_panel)

        # Center content
        center = QWidget()
        center.setStyleSheet("background: transparent;")
        center_lay = QVBoxLayout(center)
        center_lay.setContentsMargins(8, 8, 8, 8)
        center_lay.setSpacing(6)

        # Search bar
        self._search_bar = SCSearchBar(
            placeholder="Search by name, resource, contractor...",
            debounce_ms=400,
        )
        self._search_bar.search_changed.connect(self._on_search)
        center_lay.addWidget(self._search_bar)

        # Result count
        self._result_lbl = QLabel("")
        self._result_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt;")
        center_lay.addWidget(self._result_lbl)

        # Blueprint grid
        self._grid = BlueprintGrid()
        self._grid.card_clicked.connect(self._on_card_clicked)
        self._grid.card_expand.connect(self._on_card_expand)
        center_lay.addWidget(self._grid, 1)

        # Pagination
        self._pagination = PaginationBar()
        self._pagination.page_changed.connect(self._on_page_changed)
        center_lay.addWidget(self._pagination)

        body_lay.addWidget(center, 1)

        layout.addWidget(body, 1)

        # Loading overlay
        self._loading_lbl = QLabel("Loading blueprints...")
        self._loading_lbl.setAlignment(Qt.AlignCenter)
        self._loading_lbl.setStyleSheet(
            f"color: {TOOL_COLOR}; font-size: 12pt; font-weight: bold;"
        )

    # ── Loading ──────────────────────────────────────────────────────────

    def _start_loading(self):
        self._loading_lbl.show()
        self._repo.load_async(on_done=lambda: self._data_ready.emit())

    def _on_data_ready(self):
        self._loading_lbl.hide()

        stats = self._repo.get_stats()
        if stats:
            self._bp_count_lbl.setText(f"{stats.total_blueprints:,}")
            self._ing_count_lbl.setText(f"{stats.unique_ingredients}")
            self._version_lbl.setText(str(stats.version))

        hints = self._repo.get_hints()
        if hints:
            self._filter_panel.set_hints(hints)

        self._refresh_grid()

    # ── Grid refresh ─────────────────────────────────────────────────────

    def _refresh_grid(self):
        blueprints = self._repo.get_blueprints()
        pag = self._repo.get_pagination()

        self._grid.set_blueprints(blueprints)
        self._pagination.set_pagination(pag.page, pag.pages)
        self._result_lbl.setText(f"{pag.total} results")

    def _fetch_with_filters(self, page: int = 1):
        filters = self._filter_panel.get_filters()
        self._current_page = page
        self._result_lbl.setText("Loading...")

        self._repo.fetch_blueprints(
            page=page,
            limit=PAGE_SIZE,
            search=self._search_text,
            ownable=True if filters.get("ownable") else None,
            resource=filters.get("resource", ""),
            mission_type=filters.get("mission_type", ""),
            location=filters.get("location", ""),
            contractor=filters.get("contractor", ""),
            category=filters.get("category", ""),
            on_done=lambda: self._data_ready.emit(),
        )

    # ── Event handlers ───────────────────────────────────────────────────

    def _on_search(self, text: str):
        self._search_text = text
        self._fetch_with_filters(page=1)

    def _on_filters_changed(self, _filters: dict):
        self._fetch_with_filters(page=1)

    def _on_page_changed(self, page: int):
        self._fetch_with_filters(page=page)

    def _on_card_clicked(self, bp: Blueprint):
        BlueprintPopup(bp, parent=self, accent=TOOL_COLOR)

    def _on_card_expand(self, bp: Blueprint):
        BlueprintPopup(bp, parent=self, accent=TOOL_COLOR)

    # ── Tutorial ─────────────────────────────────────────────────────────

    def _show_tutorial(self):
        from ui.tutorial_popup import TutorialPopup
        TutorialPopup(self)

    # ── IPC ───────────────────────────────────────────────────────────────

    def _handle_command(self, cmd: dict):
        action = cmd.get("type", cmd.get("action", ""))
        if action == "show":
            self.show()
            self.raise_()
            self.activateWindow()
        elif action == "hide":
            self.hide()
        elif action == "quit":
            QApplication.instance().quit()
        elif action == "refresh":
            self._fetch_with_filters(page=self._current_page)

    def handle_ipc_command(self, cmd: dict):
        self._handle_command(cmd)
