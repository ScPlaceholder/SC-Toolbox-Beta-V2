"""Owned Blueprints page — folder-based organization with breadcrumb nav."""
from __future__ import annotations

import logging
import threading

from PySide6.QtCore import Qt, QTimer, QObject, Signal
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QFrame,
    QFileDialog, QMessageBox, QInputDialog, QMenu,
)

from shared.qt.theme import P
from shared.qt.search_bar import SCSearchBar
from ui.components.virtual_grid import VirtualScrollGrid, FabCard
from ui.components.breadcrumb import BreadcrumbBar
from data.models import text_field
from services.inventory import blueprint_key
from services import sc_log_scanner

log = logging.getLogger(__name__)

# Sentinel key added to folder dicts when passed through the grid
_IS_FOLDER = "_is_folder"
_FOLDER_PATH = "_folder_path"
_FOLDER_NAME = "_folder_name"
_FOLDER_BP_COUNT = "_folder_bp_count"
_FOLDER_SUB_COUNT = "_folder_sub_count"


class _ScanSignals(QObject):
    done = Signal(object, bool)
    live_name = Signal(str)


class OwnedBlueprintsPage(QWidget):
    """Grid of owned blueprints organized into folders with breadcrumb nav."""

    def __init__(self, parent, data_mgr, inventory, on_open_detail=None):
        super().__init__(parent)
        self._data = data_mgr
        self._inventory = inventory
        self._on_open_detail = on_open_detail
        self._current_folder: str = "/"
        self._grid_items: list[dict] = []
        self._auto_scanned = False
        self._scan_in_progress = False
        self._scan_signals = _ScanSignals(self)
        self._scan_signals.done.connect(self._on_scan_done)
        self._scan_signals.live_name.connect(self._on_live_blueprint)
        self._watcher = None
        self._watcher_folder = None
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── Header bar ──
        hdr = QWidget()
        hdr.setFixedHeight(44)
        hdr.setStyleSheet(f"background: {P.bg_primary};")
        hl = QHBoxLayout(hdr)
        hl.setContentsMargins(12, 6, 12, 6)
        hl.setSpacing(10)

        title = QLabel("\U0001f4be OWNED BLUEPRINTS")
        title.setStyleSheet(
            f"font-family: Consolas; font-size: 11pt; font-weight: bold;"
            f" color: {P.accent}; background: transparent;"
        )
        hl.addWidget(title)

        self._search = SCSearchBar(placeholder="Search owned blueprints...", debounce_ms=300)
        self._search.search_changed.connect(lambda _t: self._apply_filter())
        hl.addWidget(self._search, 1)

        tool_btn_qss = (
            f"QPushButton {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.accent}; border-radius: 3px;"
            f" padding: 3px 10px; font-family: Consolas; font-size: 8pt; font-weight: bold; }}"
            f"QPushButton:hover {{ color: {P.accent}; border-color: {P.fg}; }}"
        )

        scan_btn = QPushButton("\U0001f504 Scan Game Log")
        scan_btn.setCursor(Qt.PointingHandCursor)
        scan_btn.setStyleSheet(tool_btn_qss)
        scan_btn.clicked.connect(lambda: self.scan_game_log(show_message=True))
        hl.addWidget(scan_btn)

        folder_btn = QPushButton("\U0001f4c2 SC Folder")
        folder_btn.setCursor(Qt.PointingHandCursor)
        folder_btn.setStyleSheet(tool_btn_qss)
        folder_btn.clicked.connect(self._pick_sc_folder)
        hl.addWidget(folder_btn)

        clear_btn = QPushButton("Clear All")
        clear_btn.setCursor(Qt.PointingHandCursor)
        clear_btn.setStyleSheet(
            f"QPushButton {{ background: {P.bg_card}; color: {P.fg_dim};"
            f" border: 1px solid {P.border}; border-radius: 3px;"
            f" padding: 3px 10px; font-family: Consolas; font-size: 8pt; }}"
            f"QPushButton:hover {{ color: #ff6666; border-color: #ff6666; }}"
        )
        clear_btn.clicked.connect(self._clear_all)
        hl.addWidget(clear_btn)
        root.addWidget(hdr)

        sep = QFrame()
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"color: {P.border}; background: {P.border};")
        sep.setFixedHeight(1)
        root.addWidget(sep)

        # ── Breadcrumb bar ──
        crumb_row = QWidget()
        crumb_row.setFixedHeight(32)
        crumb_row.setStyleSheet(f"background: {P.bg_primary};")
        cr_lay = QHBoxLayout(crumb_row)
        cr_lay.setContentsMargins(0, 0, 12, 0)
        cr_lay.setSpacing(8)

        self._breadcrumb = BreadcrumbBar()
        self._breadcrumb.folder_clicked.connect(self._navigate_to)
        self._breadcrumb.item_dropped_on_folder.connect(self._on_drop_on_folder)
        cr_lay.addWidget(self._breadcrumb, 1)

        new_folder_btn = QPushButton("+ New Folder")
        new_folder_btn.setCursor(Qt.PointingHandCursor)
        new_folder_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {P.accent};"
            f" border: 1px solid {P.accent}; border-radius: 3px;"
            f" padding: 2px 8px; font-family: Consolas; font-size: 8pt; }}"
            f"QPushButton:hover {{ background: #1a3030; }}"
        )
        new_folder_btn.clicked.connect(self._create_folder_here)
        cr_lay.addWidget(new_folder_btn)

        self._count_label = QLabel("0 items")
        self._count_label.setStyleSheet(
            f"font-family: Consolas; font-size: 9pt; color: {P.fg_dim};"
            f" background: transparent;"
        )
        cr_lay.addWidget(self._count_label)
        root.addWidget(crumb_row)

        # ── Grid ──
        self._grid = VirtualScrollGrid(
            card_width=300, row_height=120,
            fill_fn=self._fill_card,
            on_click_fn=self._on_card_click,
            card_class=FabCard,
        )
        self._grid.card_dropped.connect(self._on_card_dropped)
        root.addWidget(self._grid, 1)

    # ── Navigation ──

    def _navigate_to(self, folder_path: str):
        if not self._inventory.folder_exists(folder_path):
            folder_path = "/"
        self._current_folder = folder_path
        self._breadcrumb.set_path(folder_path)
        self._apply_filter()

    def _create_folder_here(self):
        name, ok = QInputDialog.getText(
            self.window(), "New Folder", "Folder name:", text="New Folder")
        if not ok or not name or not name.strip():
            return
        try:
            self._inventory.create_folder(self._current_folder, name.strip())
            self._apply_filter()
        except ValueError as e:
            self._show_info("Error", str(e))

    # ── Public ──

    def refresh(self):
        self._apply_filter()

    def maybe_auto_scan(self):
        if self._auto_scanned:
            return
        if not getattr(self._data, "crafting_loaded", False):
            return
        self._auto_scanned = True
        QTimer.singleShot(0, lambda: self.scan_game_log(show_message=False))

    # ── SC folder + log scan ──

    def _pick_sc_folder(self):
        current = sc_log_scanner.get_sc_folder() or "C:/"
        folder = QFileDialog.getExistingDirectory(
            self.window(), "Select Star Citizen channel folder (e.g. LIVE)",
            current,
        )
        if not folder:
            return
        try:
            sc_log_scanner.set_sc_folder(folder)
        except Exception:
            log.exception("[OwnedBP] set_sc_folder failed")
        QTimer.singleShot(0, lambda: self.scan_game_log(show_message=True))

    def scan_game_log(self, show_message: bool = False) -> None:
        if self._inventory is None or self._scan_in_progress:
            return

        sc_folder = sc_log_scanner.get_sc_folder()
        if not sc_folder:
            if show_message:
                self._show_info(
                    "Star Citizen folder not found",
                    "Could not auto-detect your Star Citizen install.\n"
                    "Click the SC Folder button to pick the channel folder "
                    "(e.g. ...\\StarCitizen\\LIVE) manually.",
                )
            return

        if not getattr(self._data, "crafting_loaded", False) or not self._data.crafting_blueprints:
            if show_message:
                self._show_info(
                    "Crafting data not loaded",
                    "Open the Fabricator tab first so crafting blueprint data "
                    "can be fetched, then run the scan again.",
                )
            return

        self._scan_in_progress = True

        def _worker():
            try:
                names = sc_log_scanner.scan_blueprint_names(sc_folder)
            except Exception:
                log.exception("[OwnedBP] scan worker failed")
                names = set()
            try:
                self._scan_signals.done.emit(names, show_message)
            except Exception:
                log.exception("[OwnedBP] scan signal emit failed")

        threading.Thread(target=_worker, daemon=True).start()

    # ── Live log watcher ──

    def _ensure_watcher(self, sc_folder: str):
        if not sc_folder:
            return
        if self._watcher is not None and self._watcher_folder == sc_folder:
            return
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                log.exception("[OwnedBP] watcher stop failed")
            self._watcher = None

        def _on_line_name(name: str):
            try:
                self._scan_signals.live_name.emit(name)
            except Exception:
                log.exception("[OwnedBP] live signal emit failed")

        try:
            self._watcher = sc_log_scanner.BlueprintLogWatcher(sc_folder, _on_line_name)
            self._watcher.start()
            self._watcher_folder = sc_folder
            log.info("[OwnedBP] live log watcher started: %s", sc_folder)
        except Exception:
            log.exception("[OwnedBP] failed to start log watcher")
            self._watcher = None

    def stop_watcher(self):
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:
                log.exception("[OwnedBP] watcher stop failed")
            self._watcher = None
            self._watcher_folder = None

    def _on_live_blueprint(self, name: str):
        if not name or self._inventory is None:
            return
        if not getattr(self._data, "crafting_loaded", False) or not self._data.crafting_blueprints:
            return
        try:
            matches = sc_log_scanner.match_blueprints(
                [name], self._data.crafting_blueprints, data_mgr=self._data)
        except Exception:
            log.exception("[OwnedBP] live match failed")
            return
        added = 0
        for bp in matches:
            try:
                bp_id = blueprint_key(bp)
                if bp_id and not self._inventory.is_owned(bp_id):
                    self._inventory.add(bp_id, bp)
                    added += 1
            except Exception:
                log.exception("[OwnedBP] live inventory add failed")
        if added > 0:
            log.info("[OwnedBP] live scan added %d blueprint(s) for '%s'", added, name)
            try:
                self.refresh()
            except Exception:
                log.exception("[OwnedBP] live refresh failed")

    def _on_scan_done(self, names, show_message: bool):
        self._scan_in_progress = False
        try:
            sc_folder = sc_log_scanner.get_sc_folder() or ""
            if sc_folder:
                self._ensure_watcher(sc_folder)

            if not names:
                if show_message:
                    self._show_info(
                        "No blueprints in log",
                        f"Scanned logs under:\n{sc_folder}\n\n"
                        "No 'Received Blueprint' entries were found.",
                    )
                return

            try:
                matches = sc_log_scanner.match_blueprints(
                    names, self._data.crafting_blueprints, data_mgr=self._data)
            except Exception:
                log.exception("[OwnedBP] match_blueprints failed")
                matches = []

            added = 0
            for bp in matches:
                try:
                    bp_id = blueprint_key(bp)
                    if bp_id and not self._inventory.is_owned(bp_id):
                        self._inventory.add(bp_id, bp)
                        added += 1
                except Exception:
                    log.exception("[OwnedBP] inventory add failed")

            try:
                self.refresh()
            except Exception:
                log.exception("[OwnedBP] refresh failed")

            if show_message:
                unmatched = len(names) - len(matches)
                msg = (
                    f"Folder: {sc_folder}\n\n"
                    f"Found {len(names)} unique blueprint name(s) in logs.\n"
                    f"Matched to {len(matches)} fabricator blueprint(s).\n"
                    f"Newly marked as owned: {added}\n"
                )
                if unmatched > 0:
                    msg += f"Unmatched names (not in current crafting data): {unmatched}"
                QTimer.singleShot(0, lambda: self._show_info("Game Log Scan", msg))
        except Exception:
            log.exception("[OwnedBP] _on_scan_done failed")

    # ── Grid contents ──

    def _apply_filter(self):
        if self._inventory is None:
            return
        subfolders, bps = self._inventory.get_folder_contents(self._current_folder)
        query = (self._search.text() or "").strip().lower()

        # Build mixed items list: folders first, then blueprints
        items: list[dict] = []

        for child_name in subfolders:
            child_path = self._inventory.child_folder_path(
                self._current_folder, child_name)
            bp_count = self._inventory.folder_blueprint_count(child_path, recursive=True)
            sub_count = len(
                (self._inventory.get_folder_tree().get(child_path, {})
                 .get("children", [])))
            # If searching, skip folders that don't match
            if query and query not in child_name.lower():
                continue
            items.append({
                _IS_FOLDER: True,
                _FOLDER_PATH: child_path,
                _FOLDER_NAME: child_name,
                _FOLDER_BP_COUNT: bp_count,
                _FOLDER_SUB_COUNT: sub_count,
            })

        if query:
            bps = [bp for bp in bps
                   if query in self._data.get_blueprint_product_name(bp).lower()
                   or query in (bp.get("tag") or "").lower()]

        items.extend(bps)

        self._grid_items = items
        folder_count = sum(1 for i in items if isinstance(i, dict) and i.get(_IS_FOLDER))
        bp_shown = len(items) - folder_count
        parts = []
        if folder_count:
            parts.append(f"{folder_count} folder{'s' if folder_count != 1 else ''}")
        parts.append(f"{bp_shown} blueprint{'s' if bp_shown != 1 else ''}")
        self._count_label.setText("  \u2022  ".join(parts))
        self._grid.set_data(items)

    def _fill_card(self, card, item, idx):
        if isinstance(item, dict) and item.get(_IS_FOLDER):
            card.set_folder_data(
                item[_FOLDER_NAME],
                item[_FOLDER_BP_COUNT],
                item[_FOLDER_SUB_COUNT],
            )
            return

        bp = item
        TYPE_COLORS = {"weapons": P.orange, "armour": P.accent, "ammo": P.yellow}
        name = self._data.get_blueprint_product_name(bp)
        bp_type = text_field(bp, "type", "?")
        bp_sub = text_field(bp, "subtype").replace("_", " ").title()
        tiers = bp.get("tiers") or []
        type_color = TYPE_COLORS.get(bp_type, P.fg)
        type_fg = "white" if bp_type != "ammo" else P.bg_primary

        res_text = ""
        time_text = ""
        if tiers:
            tier = tiers[0]
            craft_time = tier.get("craftTimeSeconds", 0)
            resources = []
            for s in tier.get("slots", []):
                for opt in s.get("options", []):
                    rn = opt.get("resourceName", "")
                    qty = opt.get("quantity", 0)
                    if rn:
                        resources.append(f"{rn} x{qty}")
            if resources:
                res_text = "  |  ".join(resources[:3])
                if len(resources) > 3:
                    res_text += f"  +{len(resources) - 3}"
            mins = craft_time // 60
            secs = craft_time % 60
            time_text = f"  {mins}m {secs}s" if mins else f"  {secs}s"

        card.set_data(name, bp_type.title(), type_color, type_fg, bp_sub, res_text, time_text)

    def _on_card_click(self, item, idx):
        if isinstance(item, dict) and item.get(_IS_FOLDER):
            self._navigate_to(item[_FOLDER_PATH])
            return
        bp = item
        if self._on_open_detail is not None:
            self._on_open_detail(bp)
            return
        from ui.modals.blueprint_detail import BlueprintDetailModal
        BlueprintDetailModal(self.window(), bp, self._data,
                             inventory=self._inventory,
                             on_inventory_changed=self.refresh)

    # ── Drag-and-drop handlers ──

    def _on_card_dropped(self, dragged_item, target_item):
        """A card was dropped onto another card in the grid."""
        if isinstance(target_item, dict) and target_item.get(_IS_FOLDER):
            target_folder = target_item[_FOLDER_PATH]
            self._handle_drop_into_folder(dragged_item, target_folder)

    def _on_drop_on_folder(self, dragged_item, folder_path: str):
        """A card was dropped onto a breadcrumb segment."""
        self._handle_drop_into_folder(dragged_item, folder_path)

    def _handle_drop_into_folder(self, dragged_item, target_folder: str):
        """Move the dragged blueprint into the target folder."""
        if self._inventory is None:
            return
        # Only blueprints can be moved (not folder cards)
        if isinstance(dragged_item, dict) and dragged_item.get(_IS_FOLDER):
            return
        bp = dragged_item
        bp_id = blueprint_key(bp)
        if not bp_id or not self._inventory.is_owned(bp_id):
            return
        try:
            self._inventory.move_blueprint(bp_id, target_folder)
            self._apply_filter()
        except Exception:
            log.exception("[OwnedBP] drag-drop move failed for %s -> %s",
                          bp_id, target_folder)

    # ── Context menus (right-click) ──

    def contextMenuEvent(self, event):
        """Show context menu on right-click within the grid."""
        # Find which card was clicked by mapping position to grid item
        pos = event.pos()
        grid_pos = self._grid.mapFrom(self, pos)
        # Try to resolve the item at this position
        item = self._resolve_item_at(grid_pos)
        if item is None:
            return

        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.border}; font-family: Consolas; font-size: 9pt; }}"
            f"QMenu::item:selected {{ background: #1a3030; color: {P.accent}; }}"
        )

        if isinstance(item, dict) and item.get(_IS_FOLDER):
            folder_path = item[_FOLDER_PATH]
            folder_name = item[_FOLDER_NAME]
            rename_act = menu.addAction(f"Rename '{folder_name}'")
            delete_act = menu.addAction(f"Delete '{folder_name}'")
            chosen = menu.exec(event.globalPos())
            if chosen == rename_act:
                self._rename_folder(folder_path)
            elif chosen == delete_act:
                self._delete_folder(folder_path, folder_name)
        else:
            bp = item
            bp_id = blueprint_key(bp)
            move_act = menu.addAction("Move to folder...")
            remove_act = menu.addAction("Remove from owned")
            chosen = menu.exec(event.globalPos())
            if chosen == move_act:
                self._move_bp_to_folder(bp_id)
            elif chosen == remove_act:
                self._remove_bp(bp_id)

    def _resolve_item_at(self, grid_pos):
        """Try to figure out which grid item is under grid_pos."""
        # Walk visible cards in the virtual grid
        for row_cards in self._grid._visible_cards.values():
            for card in row_cards:
                if card.isVisible() and card.geometry().contains(
                        card.mapFrom(self._grid, grid_pos)):
                    if card._data is not None:
                        return card._data
        return None

    def _rename_folder(self, folder_path: str):
        old_name = folder_path.rsplit("/", 1)[-1] if folder_path != "/" else "Root"
        new_name, ok = QInputDialog.getText(
            self.window(), "Rename Folder", "New name:", text=old_name)
        if not ok or not new_name or not new_name.strip():
            return
        try:
            self._inventory.rename_folder(folder_path, new_name.strip())
            self._apply_filter()
        except ValueError as e:
            self._show_info("Error", str(e))

    def _delete_folder(self, folder_path: str, folder_name: str):
        msg = QMessageBox(self.window())
        msg.setWindowTitle("Delete Folder")
        msg.setText(
            f"Delete folder '{folder_name}'?\n\n"
            "Blueprints and sub-folders inside will be moved to the parent folder."
        )
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        msg.setStyleSheet(
            f"QMessageBox {{ background: {P.bg_primary}; color: {P.fg}; font-family: Consolas; }}"
            f"QLabel {{ color: {P.fg}; }}"
            f"QPushButton {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.border}; padding: 4px 10px; }}"
        )
        if msg.exec() != QMessageBox.Yes:
            return
        try:
            self._inventory.delete_folder(folder_path)
            self._apply_filter()
        except ValueError as e:
            self._show_info("Error", str(e))

    def _move_bp_to_folder(self, bp_id: str):
        from ui.modals.folder_picker import FolderPickerModal
        modal = FolderPickerModal(self.window(), self._inventory)
        modal.folder_selected.connect(
            lambda target: self._do_move_bp(bp_id, target))

    def _do_move_bp(self, bp_id: str, target: str):
        try:
            self._inventory.move_blueprint(bp_id, target)
            self._apply_filter()
        except ValueError as e:
            self._show_info("Error", str(e))

    def _remove_bp(self, bp_id: str):
        self._inventory.remove(bp_id)
        self._apply_filter()

    # ── Clear / info helpers ──

    def _clear_all(self):
        if not self._inventory:
            return
        msg = QMessageBox(self.window())
        msg.setWindowTitle("Clear Owned Blueprints")
        msg.setText("Remove ALL owned blueprints from your inventory?")
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.No)
        msg.setStyleSheet(
            f"QMessageBox {{ background: {P.bg_primary}; color: {P.fg}; font-family: Consolas; }}"
            f"QPushButton {{ background: {P.bg_card}; color: {P.fg};"
            f" border: 1px solid {P.border}; padding: 4px 10px; }}"
        )
        if msg.exec() != QMessageBox.Yes:
            return
        for bp_id in list(self._inventory.owned_ids()):
            self._inventory.remove(bp_id)
        self.refresh()

    def _show_info(self, title: str, text: str):
        try:
            msg = QMessageBox(self.window())
            msg.setWindowTitle(title)
            msg.setText(text)
            msg.setStandardButtons(QMessageBox.Ok)
            msg.setStyleSheet(
                f"QMessageBox {{ background: {P.bg_primary}; color: {P.fg};"
                f" font-family: Consolas; }}"
                f"QLabel {{ color: {P.fg}; }}"
                f"QPushButton {{ background: {P.bg_card}; color: {P.fg};"
                f" border: 1px solid {P.border}; padding: 4px 12px;"
                f" font-family: Consolas; }}"
                f"QPushButton:hover {{ border-color: {P.accent}; }}"
            )
            msg.exec()
        except Exception:
            log.exception("[OwnedBP] _show_info failed (%s)", title)
