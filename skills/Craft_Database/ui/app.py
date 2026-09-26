"""Craft Database — main application window."""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from shared.qt.base_window import SCWindow
from shared.qt.title_bar import SCTitleBar
from shared.qt.search_bar import SCSearchBar
from shared.qt.theme import P
from shared.qt.ipc_thread import IPCWatcher

from data.repository import BlueprintQuery, CraftRepository
from domain.models import Blueprint
from services.inventory import InventoryService
from ui.constants import TOOL_COLOR, TOOL_NAME, POLL_MS, PAGE_SIZE
from ui.widgets import BlueprintGrid, PaginationBar
from ui.filter_panel import FilterPanel
from ui.detail_panel import BlueprintPopup

log = logging.getLogger(__name__)


def _hotkey(settings_key: str, default: str, fallback: str) -> str:
    """This tool's real hotkey for its title bar (follows a launcher rebind)."""
    try:
        from shared.hotkey_label import hotkey_label
        return hotkey_label(settings_key, default)
    except ImportError:
        return fallback


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
        self._inventory = InventoryService()
        self._current_page = 1
        self._search_text = ""
        self._inventory_mode = False
        self._hints_set = False

        self._build_ui()
        self._data_ready.connect(self._on_data_ready)
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
            hotkey_text=_hotkey("hotkey_craft_db", "<shift>+7", "Shift+7"),
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

        # Inventory toggle button
        self._inv_btn = QPushButton(f"INVENTORY (0)")
        self._inv_btn.setCursor(Qt.PointingHandCursor)
        self._update_inv_btn_style(active=False)
        self._inv_btn.clicked.connect(self._toggle_inventory)
        stats_lay.addWidget(self._inv_btn)

        # Which game build the data is from (pinned datamine, not a live feed)
        self._version_lbl = QLabel(self._repo.game_label())
        self._version_lbl.setToolTip(
            "Crafting blueprints datamined from the game files "
            "(StarCitizenWiki/scunpacked-data, pinned build)")
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
            placeholder="Search by name, category, resource...",
            debounce_ms=400,
        )
        self._search_bar.search_changed.connect(self._on_search)
        center_lay.addWidget(self._search_bar)

        # Result count
        self._result_lbl = QLabel("")
        self._result_lbl.setStyleSheet(f"color: {P.fg_dim}; font-size: 8pt;")
        center_lay.addWidget(self._result_lbl)

        # No data yet: one line + Download (hidden once data is loaded)
        self._nodata = QWidget()
        nd_lay = QHBoxLayout(self._nodata)
        nd_lay.setContentsMargins(0, 4, 0, 4)
        nd_lay.setSpacing(8)
        self._nodata_lbl = QLabel("")
        self._nodata_lbl.setWordWrap(True)
        self._nodata_lbl.setStyleSheet(f"color: {P.fg}; font-size: 9pt;")
        nd_lay.addWidget(self._nodata_lbl, 1)
        self._download_btn = QPushButton("Download")
        self._download_btn.setCursor(Qt.PointingHandCursor)
        self._download_btn.setToolTip(
            "Download the crafting blueprints (about 4 MB, pinned game build) "
            "from GitHub. Needed once; after that the tool works offline.")
        self._download_btn.setStyleSheet(
            f"QPushButton {{ color: {TOOL_COLOR}; background: transparent;"
            f"border: 1px solid {TOOL_COLOR}; border-radius: 3px;"
            f"padding: 3px 12px; font-size: 8pt; font-weight: bold; }}"
            f"QPushButton:hover {{ background: {P.bg_input}; }}"
        )
        self._download_btn.clicked.connect(self._start_download)
        nd_lay.addWidget(self._download_btn)
        self._nodata.hide()
        center_lay.addWidget(self._nodata)

        # Blueprint grid
        self._grid = BlueprintGrid()
        self._grid.card_clicked.connect(self._on_card_clicked)
        self._grid.card_expand.connect(self._on_card_expand)
        self._grid.card_ownership.connect(self._on_ownership_toggled)
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

        self._update_inv_count()

    # ── Inventory button styling ─────────────────────────────────────────

    def _update_inv_btn_style(self, active: bool):
        if active:
            self._inv_btn.setStyleSheet(
                f"QPushButton {{ color: {P.bg_primary}; background: {TOOL_COLOR};"
                f"border: 1px solid {TOOL_COLOR}; border-radius: 3px;"
                f"padding: 3px 12px; font-size: 8pt; font-weight: bold;"
                f"letter-spacing: 1px; }}"
                f"QPushButton:hover {{ background: #55ddcc; border-color: #55ddcc; }}"
            )
        else:
            self._inv_btn.setStyleSheet(
                f"QPushButton {{ color: {TOOL_COLOR}; background: transparent;"
                f"border: 1px solid {TOOL_COLOR}; border-radius: 3px;"
                f"padding: 3px 12px; font-size: 8pt; font-weight: bold;"
                f"letter-spacing: 1px; }}"
                f"QPushButton:hover {{ background: {P.bg_input}; }}"
            )

    def _update_inv_count(self):
        count = self._inventory.owned_count()
        self._inv_btn.setText(f"INVENTORY ({count})")

    # ── Loading ──────────────────────────────────────────────────────────

    def _start_loading(self):
        self._loading_lbl.show()
        self._result_lbl.setText("Loading blueprints...")
        self._repo.load_async(on_done=lambda: self._data_ready.emit())

    def _start_download(self):
        self._download_btn.setEnabled(False)
        self._nodata_lbl.setText("Downloading crafting data...")
        self._repo.download_async(on_done=lambda: self._data_ready.emit())

    def _show_nodata(self, text: str):
        self._nodata_lbl.setText(text)
        self._download_btn.setEnabled(True)
        self._nodata.show()

    def _on_data_ready(self):
        self._loading_lbl.hide()

        if not self._repo.is_loaded():
            err = self._repo.get_error() or ""
            if self._repo.is_missing() or err.startswith("Download failed"):
                msg = (f"Crafting data for {self._repo.game_label()[len('Game data: '):]}"
                       " is not downloaded yet.")
                if err.startswith("Download failed"):
                    msg = "Download failed. Check the connection and try again."
                self._show_nodata(msg)
                self._result_lbl.setText("")
            else:
                self._show_nodata(f"Crafting data could not be read: {err}")
                self._result_lbl.setText("")
            if self._inventory_mode:
                self._refresh_inventory_grid()
            return

        self._nodata.hide()
        stats = self._repo.get_stats()
        if stats:
            self._bp_count_lbl.setText(f"{stats.total_blueprints:,}")
            self._ing_count_lbl.setText(f"{stats.unique_ingredients}")

        hints = self._repo.get_hints()
        if hints and not self._hints_set:
            self._filter_panel.set_hints(hints)
            self._hints_set = True

        self._refresh_grid()

    # ── Grid refresh ─────────────────────────────────────────────────────

    def _refresh_grid(self):
        if self._inventory_mode:
            self._refresh_inventory_grid()
            return

        blueprints = self._repo.get_blueprints()
        pag = self._repo.get_pagination()
        owned_ids = self._inventory.owned_ids()

        self._grid.set_blueprints(blueprints, owned_ids=owned_ids, inventory_mode=False)
        self._pagination.set_pagination(pag.page, pag.pages)
        self._result_lbl.setText(f"{pag.total:,} results")

    def _get_filter_values(self) -> dict[str, str]:
        """Return sanitised filter values from the sidebar."""
        filters = self._filter_panel.get_filters()
        return {
            "category": filters.get("category", "").strip(),
            "resource": filters.get("resource", "").strip(),
            "mission_type": filters.get("mission_type", "").strip(),
            "location": filters.get("location", "").strip(),
            "contractor": filters.get("contractor", "").strip(),
            "ownable": filters.get("ownable", ""),
        }

    @staticmethod
    def _apply_local_filters(blueprints: list[Blueprint], fv: dict[str, str]) -> list[Blueprint]:
        """Apply sidebar filters in-memory (used for inventory mode)."""
        cat = fv["category"]
        res = fv["resource"]
        mt = fv["mission_type"]
        loc = fv["location"]
        con = fv["contractor"]

        if cat:
            blueprints = [bp for bp in blueprints if cat.lower() in bp.category.lower()]
        if res:
            blueprints = [bp for bp in blueprints if res.lower() in [n.lower() for n in bp.ingredient_names]]
        if mt:
            blueprints = [bp for bp in blueprints if any(mt.lower() in m.mission_type.lower() for m in bp.missions)]
        if loc:
            blueprints = [bp for bp in blueprints if any(loc.lower() in m.locations.lower() for m in bp.missions)]
        if con:
            blueprints = [bp for bp in blueprints if any(con.lower() in m.contractor.lower() for m in bp.missions)]
        return blueprints

    def _refresh_inventory_grid(self):
        all_raw = self._inventory.get_all()
        blueprints = [Blueprint.from_dict(d) for d in all_raw]

        # Apply search filter
        query = self._search_text.strip().lower()
        if query:
            blueprints = [
                bp for bp in blueprints
                if query in bp.name.lower()
                or query in bp.category.lower()
                or any(query in n.lower() for n in bp.ingredient_names)
            ]

        # Apply sidebar filters locally
        fv = self._get_filter_values()
        blueprints = self._apply_local_filters(blueprints, fv)

        owned_ids = self._inventory.owned_ids()
        self._grid.set_blueprints(blueprints, owned_ids=owned_ids, inventory_mode=True)
        self._pagination.set_pagination(1, 1)
        self._result_lbl.setText(f"{len(blueprints)} owned blueprints")

    def _fetch_with_filters(self, page: int = 1):
        if self._inventory_mode:
            self._refresh_inventory_grid()
            return
        if not self._repo.is_loaded():
            return

        fv = self._get_filter_values()
        self._current_page = page
        self._result_lbl.setText("Loading...")

        query = BlueprintQuery(
            page=page,
            limit=PAGE_SIZE,
            search=self._search_text,
            ownable=True if fv["ownable"] else None,
            resource=fv["resource"],
            mission_type=fv["mission_type"],
            location=fv["location"],
            contractor=fv["contractor"],
            category=fv["category"],
        )
        self._repo.fetch_blueprints(
            query=query,
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

    def _on_ownership_toggled(self, bp: Blueprint, now_owned: bool):
        if now_owned:
            self._inventory.add(bp.blueprint_id, bp.raw_dict)
        else:
            self._inventory.remove(bp.blueprint_id)

        self._update_inv_count()
        self._refresh_grid()

    def _toggle_inventory(self):
        self._inventory_mode = not self._inventory_mode
        self._update_inv_btn_style(self._inventory_mode)

        if self._inventory_mode:
            self._refresh_inventory_grid()
        else:
            self._fetch_with_filters(page=self._current_page)

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
            if self._repo.is_loaded():
                self._fetch_with_filters(page=self._current_page)
            else:
                self._start_loading()
        else:
            self.handle_ipc_command(cmd)

