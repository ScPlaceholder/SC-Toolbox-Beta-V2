"""
Mining Ledger tab — three-panel layout for fleet hierarchy management.

Left:   Player Roster (add / remove / promote / import / export)
Center: Hierarchy Canvas (interactive node graph)
Right:  Ship Fleet (categorized ships, draggable onto canvas)
"""

from __future__ import annotations

import json
import logging
import os
from functools import partial
from typing import Callable, Optional

from PySide6.QtCore import Qt, QMimeData, QPoint, QByteArray, QTimer
from PySide6.QtGui import QDrag, QColor, QFont, QMouseEvent
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QLineEdit, QCheckBox, QScrollArea, QFrame, QSplitter,
    QFileDialog, QSizePolicy, QToolTip, QListWidget, QListWidgetItem,
    QDialog, QDialogButtonBox, QMenu, QMessageBox, QTabWidget,
)

from shared.qt.theme import P
from services.ledger_store import (
    LedgerData, PlayerEntry, FleetSupportShip, MiningTeam,
    save_ledger, load_ledger, export_player_roster, import_player_roster,
)
from services.loadout_loader import LoadoutSnapshot
from services.ship_db import load_ship_db, fuzzy_match, crew_for_model, ShipModel
from services.professions import (
    PROFESSIONS, icon_for as profession_icon,
    fuzzy_match as profession_fuzzy_match,
)
from .ledger_canvas import LedgerScene, LedgerView
from .ledger_nodes import SHIP_TYPE_COLORS

log = logging.getLogger(__name__)

_ACCENT = "#33dd88"  # Mining Signals green

_BTN_STYLE = f"""
    QPushButton {{
        background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border};
        border-radius: 4px; padding: 4px 10px;
        font-family: Consolas, monospace; font-size: 8pt;
    }}
    QPushButton:hover {{
        background: {P.bg_secondary}; border-color: {_ACCENT};
    }}
"""

_HEADER_STYLE = (
    f"font-family: Electrolize, Consolas, monospace; font-size: 10pt; "
    f"font-weight: bold; color: {_ACCENT}; background: transparent; padding: 4px 0;"
)

_CATEGORY_STYLE = (
    f"font-family: Electrolize, Consolas, monospace; font-size: 9pt; "
    f"font-weight: bold; color: {P.fg_bright}; background: transparent; padding: 6px 0 2px 0;"
)

_INPUT_STYLE = (
    f"background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border}; "
    f"border-radius: 4px; padding: 4px 8px; font-family: Consolas, monospace; font-size: 8pt;"
)


# ---------------------------------------------------------------------------
# Collapsible section widget
# ---------------------------------------------------------------------------

class _CollapsibleSection(QWidget):
    """A header label that toggles visibility of child content."""

    def __init__(self, title: str, accent: str = P.fg_bright, parent: QWidget = None) -> None:
        super().__init__(parent)
        self._expanded = True
        self._title = title

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self._header = QPushButton(f"▼  {title}", self)
        self._header.setStyleSheet(
            f"QPushButton {{ background: {P.bg_secondary}; color: {accent}; border: none; "
            f"font-family: Electrolize, Consolas, monospace; font-size: 8pt; font-weight: bold; "
            f"text-align: left; padding: 5px 8px; border-radius: 3px; }}"
            f"QPushButton:hover {{ background: {P.bg_input}; }}"
        )
        self._header.setCursor(Qt.PointingHandCursor)
        self._header.clicked.connect(self.toggle)
        outer.addWidget(self._header)

        self._content = QWidget(self)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 2, 0, 0)
        self._content_layout.setSpacing(3)
        outer.addWidget(self._content)

    @property
    def content_layout(self) -> QVBoxLayout:
        return self._content_layout

    def toggle(self) -> None:
        self._expanded = not self._expanded
        self._content.setVisible(self._expanded)
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow}  {self._title}")

    def set_title(self, title: str) -> None:
        self._title = title
        arrow = "▼" if self._expanded else "▶"
        self._header.setText(f"{arrow}  {self._title}")


# ---------------------------------------------------------------------------
# Draggable row widgets
# ---------------------------------------------------------------------------

class _DraggableProfessionRow(QWidget):
    """A row in the Key tab that can be dragged onto a player."""

    def __init__(
        self,
        name: str,
        icon: str,
        description: str,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._icon = icon
        self._drag_start: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(6)

        icon_lbl = QLabel(icon, self)
        icon_lbl.setStyleSheet(
            f"font-family: 'Segoe UI Emoji', 'Segoe UI Symbol'; "
            f"font-size: 11pt; background: transparent;"
        )
        icon_lbl.setFixedWidth(22)
        icon_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(icon_lbl)

        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(1)
        name_lbl = QLabel(name, self)
        name_lbl.setStyleSheet(
            f"color: {P.fg_bright}; font-family: Consolas, monospace; "
            f"font-size: 8pt; font-weight: bold; background: transparent;"
        )
        name_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        text_col.addWidget(name_lbl)
        desc_lbl = QLabel(description, self)
        desc_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas, monospace; "
            f"font-size: 7pt; background: transparent;"
        )
        desc_lbl.setWordWrap(True)
        desc_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        text_col.addWidget(desc_lbl)
        layout.addLayout(text_col, 1)

        self.setStyleSheet(
            f"QWidget {{ background: {P.bg_card}; border-radius: 4px; }}"
            f"QWidget:hover {{ background: {P.bg_input}; }}"
        )
        self.setCursor(Qt.OpenHandCursor)
        self.setToolTip(f"Drag onto a player to assign: {name}")

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            return
        if (event.position().toPoint() - self._drag_start).manhattanLength() < 10:
            return
        drag = QDrag(self)
        mime = QMimeData()
        payload = json.dumps({"profession": self._name}).encode()
        mime.setData("application/x-sc-ledger-profession", QByteArray(payload))
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
        self._drag_start = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)


class _DraggableShipRow(QWidget):
    """A row in the fleet panel that can be dragged onto the canvas."""

    def __init__(
        self,
        ship_name: str,
        ship_type: str,
        loadout_path: str = "",
        tooltip_text: str = "",
        parent: QWidget = None,
        model_crew: int = 0,
    ) -> None:
        super().__init__(parent)
        self._ship_name = ship_name
        self._ship_type = ship_type
        self._loadout_path = loadout_path
        self._model_crew = model_crew
        self._drag_start: QPoint | None = None

        accent = SHIP_TYPE_COLORS.get(ship_type, P.fg_dim)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 3, 8, 3)
        layout.setSpacing(6)

        # Color bar
        bar = QFrame(self)
        bar.setFixedSize(3, 18)
        bar.setStyleSheet(f"background: {accent}; border: none;")
        bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(bar)

        lbl = QLabel(ship_name, self)
        lbl.setStyleSheet(
            f"color: {P.fg}; font-family: Consolas, monospace; font-size: 8pt; background: transparent;"
        )
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        layout.addWidget(lbl, 1)

        self.setStyleSheet(
            f"QWidget {{ background: {P.bg_card}; border-radius: 4px; }}"
            f"QWidget:hover {{ background: {P.bg_input}; }}"
        )
        self.setCursor(Qt.OpenHandCursor)
        if tooltip_text:
            self.setToolTip(tooltip_text)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            return
        if (event.position().toPoint() - self._drag_start).manhattanLength() < 10:
            return
        drag = QDrag(self)
        mime = QMimeData()
        payload = json.dumps({
            "ship_name": self._ship_name,
            "ship_type": self._ship_type,
            "loadout_path": self._loadout_path,
            "model_crew": self._model_crew,
        }).encode()
        mime.setData("application/x-sc-ledger-ship", QByteArray(payload))
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
        self._drag_start = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)


class _DraggablePlayerRow(QWidget):
    """A row in the player panel that can be dragged onto ships."""

    def __init__(
        self,
        name: str,
        is_leader: bool,
        is_foreman: bool,
        is_assigned_user: bool,
        on_toggle_leader: Callable[[bool], None],
        on_toggle_foreman: Callable[[bool], None],
        on_toggle_assign: Callable[[bool], None],
        on_remove: Callable[[], None],
        parent: QWidget = None,
        strike_group_leader_of: str = "",
        can_promote_strike_leader: bool = False,
        on_toggle_strike_leader: Callable[[bool], None] | None = None,
        profession: str = "",
        on_assign_profession: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._name = name
        self._is_leader = is_leader
        self._is_foreman = is_foreman
        self._is_assigned_user = is_assigned_user
        self._strike_group_leader_of = strike_group_leader_of
        self._can_promote_strike_leader = can_promote_strike_leader
        self._profession = profession
        self._on_toggle_leader = on_toggle_leader
        self._on_toggle_foreman = on_toggle_foreman
        self._on_toggle_assign = on_toggle_assign
        self._on_toggle_strike_leader = on_toggle_strike_leader
        self._on_assign_profession = on_assign_profession
        self._on_remove = on_remove
        self._drag_start: QPoint | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 3, 4, 3)
        outer.setSpacing(1)

        # Row 1: name (with profession icon prefix if assigned)
        icon_str = profession_icon(profession)
        display_name = f"{icon_str}  {name}" if icon_str else name
        lbl = QLabel(display_name, self)
        name_color = _ACCENT if is_assigned_user else (P.green if is_foreman else P.fg)
        lbl.setStyleSheet(
            f"color: {name_color}; font-family: Consolas, monospace; "
            f"font-size: 8pt; font-weight: bold; background: transparent;"
        )
        lbl.setToolTip(f"Profession: {profession}" if profession else "")
        lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        outer.addWidget(lbl)

        # Row 2: role tags (only shown if player has a role)
        tags: list[str] = []
        if is_assigned_user:
            tags.append(f'<span style="color:{_ACCENT};">User</span>')
        if is_foreman:
            tags.append(f'<span style="color:{P.green};">Foreman</span>')
        if is_leader:
            tags.append(f'<span style="color:{P.yellow};">Leader</span>')
        if strike_group_leader_of:
            tags.append(
                f'<span style="color:{P.purple};">SG Leader {strike_group_leader_of}</span>'
            )
        if tags:
            role_lbl = QLabel("  ".join(tags), self)
            role_lbl.setStyleSheet(
                f"font-family: Consolas, monospace; font-size: 7pt; background: transparent;"
            )
            role_lbl.setAttribute(Qt.WA_TransparentForMouseEvents, True)
            role_lbl.setTextFormat(Qt.RichText)
            outer.addWidget(role_lbl)

        self.setStyleSheet(
            f"QWidget {{ background: {P.bg_card}; border-radius: 4px; }}"
            f"QWidget:hover {{ background: {P.bg_input}; }}"
        )
        self.setCursor(Qt.OpenHandCursor)
        self.setAcceptDrops(True)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_start = event.position().toPoint()
        super().mousePressEvent(event)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-sc-ledger-profession"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasFormat("application/x-sc-ledger-profession"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        if not event.mimeData().hasFormat("application/x-sc-ledger-profession"):
            event.ignore()
            return
        try:
            data = json.loads(
                bytes(event.mimeData().data("application/x-sc-ledger-profession")).decode()
            )
        except (ValueError, UnicodeDecodeError):
            event.ignore()
            return
        name = data.get("profession", "")
        if self._on_assign_profession is not None:
            self._on_assign_profession(name)
        event.acceptProposedAction()

    def contextMenuEvent(self, event) -> None:
        menu = QMenu(self)
        menu.setStyleSheet(
            f"QMenu {{ background: {P.bg_card}; color: {P.fg}; border: 1px solid {P.border}; "
            f"font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QMenu::item:selected {{ background: {P.bg_input}; }}"
            f"QMenu::separator {{ background: {P.border}; height: 1px; margin: 4px 8px; }}"
        )

        # Assign User toggle
        if self._is_assigned_user:
            a = menu.addAction("✕  Unset as User")
            a.setData(("assign", False))
        else:
            a = menu.addAction("👤  Set as User")
            a.setData(("assign", True))

        # Foreman toggle
        if self._is_foreman:
            a = menu.addAction("✕  Remove Foreman")
            a.setData(("foreman", False))
        else:
            a = menu.addAction("⭐  Set as Foreman")
            a.setData(("foreman", True))

        # Leader toggle
        if self._is_leader:
            a = menu.addAction("✕  Remove Leader")
            a.setData(("leader", False))
        else:
            a = menu.addAction("🔰  Promote to Leader")
            a.setData(("leader", True))

        # Strike Group Leader (only if player is in a strike group)
        if self._can_promote_strike_leader and self._on_toggle_strike_leader is not None:
            if self._strike_group_leader_of:
                a = menu.addAction("✕  Remove Strike Group Leader")
                a.setData(("strike_leader", False))
            else:
                a = menu.addAction("⚔  Promote to Strike Group Leader")
                a.setData(("strike_leader", True))

        # Profession submenu
        if self._on_assign_profession is not None:
            menu.addSeparator()
            prof_menu = menu.addMenu("\U0001F4CB  Assign Profession")
            # "No Profession" clear option
            a = prof_menu.addAction("—  No Profession")
            a.setData(("profession", ""))
            prof_menu.addSeparator()
            for pname, picon, pdesc in PROFESSIONS:
                marker = "●  " if pname == self._profession else "    "
                a = prof_menu.addAction(f"{marker}{picon}  {pname}")
                a.setToolTip(pdesc)
                a.setData(("profession", pname))

        menu.addSeparator()
        a = menu.addAction("🗑  Remove Player")
        a.setData(("remove", None))

        chosen = menu.exec(event.globalPos())
        if not chosen:
            return
        action_type, value = chosen.data()
        if action_type == "assign":
            self._on_toggle_assign(value)
        elif action_type == "foreman":
            self._on_toggle_foreman(value)
        elif action_type == "leader":
            self._on_toggle_leader(value)
        elif action_type == "strike_leader":
            if self._on_toggle_strike_leader is not None:
                self._on_toggle_strike_leader(value)
        elif action_type == "profession":
            if self._on_assign_profession is not None:
                self._on_assign_profession(value)
        elif action_type == "remove":
            self._on_remove()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._drag_start is None:
            return
        if (event.position().toPoint() - self._drag_start).manhattanLength() < 10:
            return
        drag = QDrag(self)
        mime = QMimeData()
        payload = json.dumps({"name": self._name}).encode()
        mime.setData("application/x-sc-ledger-player", QByteArray(payload))
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)
        self._drag_start = None

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        self._drag_start = None
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Ship model fuzzy search dialog
# ---------------------------------------------------------------------------

class _ShipModelPicker(QDialog):
    """Modal dialog with a fuzzy search bar to pick a Star Citizen ship model."""

    def __init__(self, parent: QWidget = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Select Ship Model")
        self.setFixedSize(340, 400)
        self.setStyleSheet(
            f"QDialog {{ background: {P.bg_primary}; color: {P.fg}; "
            f"border: 1px solid {P.border}; }}"
        )
        self.selected_model: ShipModel | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        lbl = QLabel("Search ship model:", self)
        lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas, monospace; font-size: 8pt; background: transparent;"
        )
        layout.addWidget(lbl)

        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Type to search (e.g. Carrack, Caterpillar)...")
        self._search.setStyleSheet(_INPUT_STYLE)
        self._search.textChanged.connect(self._on_search)
        layout.addWidget(self._search)

        self._list = QListWidget(self)
        self._list.setStyleSheet(
            f"QListWidget {{ background: {P.bg_input}; color: {P.fg}; border: 1px solid {P.border}; "
            f"font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QListWidget::item {{ padding: 4px 8px; }}"
            f"QListWidget::item:selected {{ background: {P.bg_secondary}; color: {P.fg_bright}; }}"
        )
        self._list.itemDoubleClicked.connect(self._on_pick)
        layout.addWidget(self._list, 1)

        btn_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        btn_box.setStyleSheet(
            f"QPushButton {{ {_BTN_STYLE.split('{')[1].split('}')[0]} }}"
        )
        btn_box.accepted.connect(self._on_accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        # Initial populate
        self._on_search("")
        self._search.setFocus()

    def _on_search(self, text: str) -> None:
        self._list.clear()
        results = fuzzy_match(text, limit=30)
        for ship in results:
            item = QListWidgetItem(f"{ship.name}  (crew: {ship.crew_max})")
            item.setData(Qt.UserRole, ship)
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _on_pick(self, item: QListWidgetItem) -> None:
        self.selected_model = item.data(Qt.UserRole)
        self.accept()

    def _on_accept(self) -> None:
        item = self._list.currentItem()
        if item:
            self.selected_model = item.data(Qt.UserRole)
            self.accept()


# ---------------------------------------------------------------------------
# Main tab widget
# ---------------------------------------------------------------------------

class MiningLedgerTab(QWidget):
    """Three-panel Mining Ledger tab: players | canvas | fleet."""

    def __init__(
        self,
        config: dict,
        save_config_fn: Callable[[dict], None],
        fleet_snapshots: list[LoadoutSnapshot],
        ship_snapshots: dict[str, LoadoutSnapshot | None] | None = None,
        salvage_snapshots: list | None = None,
        parent: QWidget = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._save_config_fn = save_config_fn
        self._fleet_snapshots = fleet_snapshots
        self._ship_snapshots = ship_snapshots or {}
        self._salvage_snapshots = salvage_snapshots if salvage_snapshots is not None else []

        self._ledger_path = config.get(
            "ledger_file",
            os.path.join(
                os.path.expanduser("~"), "Documents", "SC Loadouts", "mining_roster.json",
            ),
        )

        # Auto-migrate: if the new path doesn't exist but an old in-folder
        # mining_ledger.json does, copy it over so we don't lose data on update
        if not os.path.isfile(self._ledger_path):
            old_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)), "mining_ledger.json",
            )
            if os.path.isfile(old_path):
                import shutil
                os.makedirs(os.path.dirname(self._ledger_path), exist_ok=True)
                shutil.copy2(old_path, self._ledger_path)
                log.info(
                    "Migrated roster from %s → %s", old_path, self._ledger_path,
                )

        self._data = load_ledger(self._ledger_path)

        # Debounce save timer — must exist before anything emits hierarchy_changed
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(500)
        self._save_timer.timeout.connect(self._do_save)

        self._scene = LedgerScene()
        self._build_ui()
        self._scene.set_ledger_data(self._data)

        # Push profession lookup to scene for badge rendering
        self._push_profession_lookup()

        # Re-render player panel now that the scene has teams and crew data
        self._refresh_player_panel()

        # Center view after first paint (needs geometry to be ready)
        # If a user is assigned, center on their team; otherwise center on foreman
        def _initial_center():
            if self._data.assigned_user:
                self._center_on_assigned_user()
            else:
                self._view.center_on_content()
        QTimer.singleShot(50, _initial_center)

        # Connect after initial load so the load itself doesn't trigger a save
        self._scene.hierarchy_changed.connect(self._on_hierarchy_changed)
        self._scene.profession_assigned.connect(self._on_assign_profession)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        splitter = QSplitter(Qt.Horizontal, self)
        splitter.setStyleSheet(
            f"QSplitter::handle {{ background: {P.border}; width: 2px; }}"
        )

        # Left panel — Player Roster
        left = self._build_player_panel()
        left.setMinimumWidth(200)
        left.setMaximumWidth(300)
        splitter.addWidget(left)

        # Center — Canvas with fullscreen button overlay
        canvas_container = QWidget(self)
        canvas_layout = QVBoxLayout(canvas_container)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)

        # Toolbar row above canvas
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(4, 4, 4, 0)
        toolbar.addStretch()

        _toolbar_btn_style = (
            f"QPushButton {{ background: {P.bg_input}; color: {P.fg_dim}; "
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 2px 8px; "
            f"font-family: Consolas, monospace; font-size: 7pt; }}"
            f"QPushButton:hover {{ color: {P.fg_bright}; border-color: {_ACCENT}; }}"
        )

        btn_export = QPushButton("💾 Export", self)
        btn_export.setFixedHeight(22)
        btn_export.setStyleSheet(_toolbar_btn_style)
        btn_export.setToolTip("Export the entire roster to a shareable JSON file")
        btn_export.clicked.connect(self._on_export_roster_file)
        toolbar.addWidget(btn_export)

        btn_load = QPushButton("📂 Load", self)
        btn_load.setFixedHeight(22)
        btn_load.setStyleSheet(_toolbar_btn_style)
        btn_load.setToolTip("Load a roster JSON file (replaces current roster)")
        btn_load.clicked.connect(self._on_load_roster_file)
        toolbar.addWidget(btn_load)

        _clear_btn_style = (
            f"QPushButton {{ background: {P.bg_input}; color: {P.fg_dim}; "
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 2px 8px; "
            f"font-family: Consolas, monospace; font-size: 7pt; }}"
            f"QPushButton:hover {{ color: {P.red}; border-color: {P.red}; }}"
        )

        btn_clear_players = QPushButton("🗑 Players", self)
        btn_clear_players.setFixedHeight(22)
        btn_clear_players.setStyleSheet(_clear_btn_style)
        btn_clear_players.setToolTip("Remove all players from the roster (keeps ships and teams)")
        btn_clear_players.clicked.connect(self._on_clear_players)
        toolbar.addWidget(btn_clear_players)

        btn_clear_ships = QPushButton("🗑 Ships", self)
        btn_clear_ships.setFixedHeight(22)
        btn_clear_ships.setStyleSheet(_clear_btn_style)
        btn_clear_ships.setToolTip("Remove all ships from the canvas (keeps players and teams)")
        btn_clear_ships.clicked.connect(self._on_clear_ships)
        toolbar.addWidget(btn_clear_ships)

        btn_clear_all = QPushButton("🗑 All", self)
        btn_clear_all.setFixedHeight(22)
        btn_clear_all.setStyleSheet(_clear_btn_style)
        btn_clear_all.setToolTip("Remove all teams, ships, and strike groups from the canvas")
        btn_clear_all.clicked.connect(self._on_clear_ledger)
        toolbar.addWidget(btn_clear_all)

        btn_fullscreen = QPushButton("⛶", self)
        btn_fullscreen.setFixedHeight(22)
        btn_fullscreen.setFixedWidth(26)
        btn_fullscreen.setStyleSheet(_toolbar_btn_style)
        btn_fullscreen.setToolTip("Toggle fullscreen")
        btn_fullscreen.clicked.connect(self._toggle_fullscreen)
        toolbar.addWidget(btn_fullscreen)
        canvas_layout.addLayout(toolbar)

        # Cluster filter bar (hidden until clusters exist)
        self._cluster_bar = QWidget(canvas_container)
        self._cluster_bar.setStyleSheet(f"background: {P.bg_secondary};")
        cluster_bar_layout = QHBoxLayout(self._cluster_bar)
        cluster_bar_layout.setContentsMargins(8, 2, 8, 2)
        cluster_bar_layout.setSpacing(4)

        cl_lbl = QLabel("Clusters:", self._cluster_bar)
        cl_lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas, monospace; font-size: 7pt; background: transparent;"
        )
        cluster_bar_layout.addWidget(cl_lbl)

        _small_btn = (
            f"QPushButton {{ background: {P.bg_input}; color: {P.fg_dim}; "
            f"border: 1px solid {P.border}; border-radius: 2px; padding: 1px 6px; "
            f"font-family: Consolas, monospace; font-size: 7pt; }}"
            f"QPushButton:hover {{ color: {P.fg_bright}; }}"
        )
        btn_all = QPushButton("All", self._cluster_bar)
        btn_all.setFixedHeight(18)
        btn_all.setStyleSheet(_small_btn)
        btn_all.clicked.connect(self._check_all_clusters)
        cluster_bar_layout.addWidget(btn_all)

        btn_none = QPushButton("None", self._cluster_bar)
        btn_none.setFixedHeight(18)
        btn_none.setStyleSheet(_small_btn)
        btn_none.clicked.connect(self._uncheck_all_clusters)
        cluster_bar_layout.addWidget(btn_none)

        self._cluster_cb_area = QHBoxLayout()
        self._cluster_cb_area.setSpacing(4)
        cluster_bar_layout.addLayout(self._cluster_cb_area)
        cluster_bar_layout.addStretch()

        self._cluster_bar.hide()
        self._cluster_checkboxes: dict[str, QCheckBox] = {}
        canvas_layout.addWidget(self._cluster_bar)

        self._view = LedgerView(self._scene, canvas_container)
        canvas_layout.addWidget(self._view, 1)
        splitter.addWidget(canvas_container)

        # Right panel — Ship Fleet
        right = self._build_fleet_panel()
        right.setMinimumWidth(200)
        right.setMaximumWidth(300)
        splitter.addWidget(right)

        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setSizes([220, 600, 220])

        outer.addWidget(splitter)

    # ------------------------------------------------------------------
    # Left panel — Player Roster
    # ------------------------------------------------------------------

    def _build_player_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setStyleSheet(f"background: {P.bg_primary};")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header = QLabel("Player Roster", panel)
        header.setStyleSheet(_HEADER_STYLE)
        layout.addWidget(header)

        # Add row
        add_row = QHBoxLayout()
        self._player_input = QLineEdit(panel)
        self._player_input.setPlaceholderText("Player name...")
        self._player_input.setStyleSheet(_INPUT_STYLE)
        self._player_input.returnPressed.connect(self._on_add_player)
        add_row.addWidget(self._player_input, 1)

        btn_add = QPushButton("Add Player", panel)
        btn_add.setStyleSheet(_BTN_STYLE)
        btn_add.clicked.connect(self._on_add_player)
        add_row.addWidget(btn_add)
        layout.addLayout(add_row)

        # Import / Export row
        ie_row = QHBoxLayout()
        btn_import = QPushButton("Import", panel)
        btn_import.setStyleSheet(_BTN_STYLE)
        btn_import.clicked.connect(self._on_import_roster)
        ie_row.addWidget(btn_import)

        btn_export = QPushButton("Export", panel)
        btn_export.setStyleSheet(_BTN_STYLE)
        btn_export.clicked.connect(self._on_export_roster)
        ie_row.addWidget(btn_export)
        layout.addLayout(ie_row)

        # Separator
        sep1 = QFrame(panel)
        sep1.setFrameShape(QFrame.HLine)
        sep1.setStyleSheet(f"background: {P.border}; max-height: 1px;")
        layout.addWidget(sep1)

        # Search / filter box
        self._player_search = QLineEdit(panel)
        self._player_search.setPlaceholderText("Search players...")
        self._player_search.setStyleSheet(_INPUT_STYLE)
        self._player_search.textChanged.connect(self._on_player_search)
        layout.addWidget(self._player_search)

        # Tabs: Players list / Profession Key
        tabs = QTabWidget(panel)
        tabs.setDocumentMode(True)
        tabs.setStyleSheet(
            f"QTabBar::tab {{ background: {P.bg_input}; color: {P.fg_dim}; "
            f"font-family: Consolas, monospace; font-size: 7pt; "
            f"padding: 4px 10px; border: 1px solid {P.border}; }}"
            f"QTabBar::tab:selected {{ background: {P.bg_secondary}; color: {_ACCENT}; }}"
            f"QTabWidget::pane {{ border: 1px solid {P.border}; background: transparent; }}"
        )

        # ── Players tab ──
        players_scroll = QScrollArea(tabs)
        players_scroll.setWidgetResizable(True)
        players_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
        )
        self._player_list_widget = QWidget(players_scroll)
        self._player_list_layout = QVBoxLayout(self._player_list_widget)
        self._player_list_layout.setContentsMargins(0, 0, 0, 0)
        self._player_list_layout.setSpacing(4)
        self._player_list_layout.addStretch()
        players_scroll.setWidget(self._player_list_widget)
        tabs.addTab(players_scroll, "Players")

        # ── Key tab ──
        key_container = QWidget(tabs)
        key_outer = QVBoxLayout(key_container)
        key_outer.setContentsMargins(4, 4, 4, 4)
        key_outer.setSpacing(4)

        self._profession_search = QLineEdit(key_container)
        self._profession_search.setPlaceholderText("Search professions...")
        self._profession_search.setStyleSheet(_INPUT_STYLE)
        self._profession_search.textChanged.connect(self._on_profession_search)
        key_outer.addWidget(self._profession_search)

        key_scroll = QScrollArea(key_container)
        key_scroll.setWidgetResizable(True)
        key_scroll.setStyleSheet(
            f"QScrollArea {{ background: transparent; border: none; }}"
        )
        self._profession_list_widget = QWidget(key_scroll)
        self._profession_list_layout = QVBoxLayout(self._profession_list_widget)
        self._profession_list_layout.setContentsMargins(0, 0, 0, 0)
        self._profession_list_layout.setSpacing(4)
        self._profession_list_layout.addStretch()
        key_scroll.setWidget(self._profession_list_widget)
        key_outer.addWidget(key_scroll, 1)

        self._refresh_profession_key()
        tabs.addTab(key_container, "Key")

        layout.addWidget(tabs, 1)

        self._refresh_player_panel()
        return panel

    def _refresh_player_panel(self) -> None:
        # Clear existing rows (keep the stretch at end)
        while self._player_list_layout.count() > 1:
            item = self._player_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Get search filter
        search_text = ""
        if hasattr(self, "_player_search"):
            search_text = self._player_search.text().strip().lower()

        # Build maps: player → team_name, player → (mothership_id, strike_group_name)
        player_team_map: dict[str, str] = {}
        player_sg_map: dict[str, tuple[str, str]] = {}
        for ship in self._scene._ships:
            team = self._scene.get_ship_team(ship)
            team_label = getattr(team, "team_name", "") if team else ""
            for crew_name in ship.crew:
                player_team_map[crew_name] = team_label
                if ship.strike_group and ship.mothership_id:
                    player_sg_map[crew_name] = (ship.mothership_id, ship.strike_group)

        for t in self._scene._teams:
            player_team_map.setdefault(t.leader_name, t.team_name)

        # Strike group leader lookup (player_name → strike group name they lead)
        sg_leader_map: dict[str, str] = {}
        for g in self._scene._strike_groups:
            if g.leader:
                sg_leader_map[g.leader] = g.name_text

        # Group players by team
        from collections import OrderedDict
        groups: OrderedDict[str, list] = OrderedDict()
        unassigned: list = []

        team_order = []
        if self._scene._foreman:
            team_order.append(self._scene._foreman.team_name)
        for t in self._scene._teams:
            if t.team_name not in team_order:
                team_order.append(t.team_name)
        for tn in team_order:
            groups[tn] = []

        for player in self._data.players:
            if search_text and search_text not in player.name.lower():
                continue
            team_name = player_team_map.get(player.name, "")
            if team_name and team_name in groups:
                groups[team_name].append(player)
            else:
                unassigned.append(player)

        idx = 0
        for team_name, members in groups.items():
            if not members:
                continue
            section = _CollapsibleSection(
                f"{team_name}  ({len(members)})", _ACCENT, self._player_list_widget,
            )
            # Split members: non-strike-group players + per-strike-group sub-sections
            team_node = None
            if self._scene._foreman and self._scene._foreman.team_name == team_name:
                team_node = self._scene._foreman
            else:
                for t in self._scene._teams:
                    if t.team_name == team_name:
                        team_node = t
                        break

            # Determine which strike groups belong to motherships in this team
            sg_in_team: OrderedDict[tuple[str, str], list] = OrderedDict()
            regular_members = []
            if team_node is not None:
                team_ships = self._scene._team_ships.get(team_node, [])
                mothership_ids = {
                    s.unique_id for s in team_ships if s.ship_type == "Mothership"
                }
                for player in members:
                    sg_key = player_sg_map.get(player.name)
                    if sg_key and sg_key[0] in mothership_ids:
                        sg_in_team.setdefault(sg_key, []).append(player)
                    else:
                        regular_members.append(player)
            else:
                regular_members = members

            # Render regular team members first
            for player in regular_members:
                row = self._make_player_row(player, section, sg_leader_map)
                section.content_layout.addWidget(row)

            # Render each strike group as a nested sub-section
            for (mother_id, sg_name), sg_members in sg_in_team.items():
                sg_section = _CollapsibleSection(
                    f"⚔ {sg_name}  ({len(sg_members)})",
                    P.purple,
                    section,
                )
                for player in sg_members:
                    row = self._make_player_row(
                        player, sg_section, sg_leader_map, in_strike_group=True,
                    )
                    sg_section.content_layout.addWidget(row)
                section.content_layout.addWidget(sg_section)

            self._player_list_layout.insertWidget(idx, section)
            idx += 1

        # Unassigned section
        if unassigned:
            section = _CollapsibleSection(
                f"Unassigned  ({len(unassigned)})", P.fg_dim, self._player_list_widget,
            )
            for player in unassigned:
                row = self._make_player_row(player, section, sg_leader_map)
                section.content_layout.addWidget(row)
            self._player_list_layout.insertWidget(idx, section)

    def _make_player_row(
        self, player, parent_widget,
        sg_leader_map: dict[str, str] | None = None,
        in_strike_group: bool = False,
    ) -> _DraggablePlayerRow:
        sg_leader_of = ""
        if sg_leader_map:
            sg_leader_of = sg_leader_map.get(player.name, "")
        return _DraggablePlayerRow(
            name=player.name,
            is_leader=player.is_leader,
            is_foreman=(player.name == self._data.foreman_name),
            is_assigned_user=(player.name == self._data.assigned_user),
            strike_group_leader_of=sg_leader_of,
            can_promote_strike_leader=in_strike_group,
            profession=getattr(player, "profession", ""),
            on_assign_profession=partial(self._on_assign_profession, player.name),
            on_toggle_leader=partial(self._on_toggle_leader, player.name),
            on_toggle_foreman=partial(self._on_toggle_foreman, player.name),
            on_toggle_assign=partial(self._on_toggle_assign_user, player.name),
            on_toggle_strike_leader=partial(self._on_toggle_strike_leader, player.name),
            on_remove=partial(self._on_remove_player, player.name),
            parent=parent_widget,
        )

    # ------------------------------------------------------------------
    # Right panel — Ship Fleet
    # ------------------------------------------------------------------

    def _build_fleet_panel(self) -> QWidget:
        panel = QWidget(self)
        panel.setStyleSheet(f"background: {P.bg_primary};")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        header = QLabel("Ship Fleet", panel)
        header.setStyleSheet(_HEADER_STYLE)
        layout.addWidget(header)

        # Scrollable ship list
        scroll = QScrollArea(panel)
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(f"QScrollArea {{ background: transparent; border: none; }}")
        self._fleet_list_widget = QWidget(scroll)
        self._fleet_list_layout = QVBoxLayout(self._fleet_list_widget)
        self._fleet_list_layout.setContentsMargins(0, 0, 0, 0)
        self._fleet_list_layout.setSpacing(4)
        self._fleet_list_layout.addStretch()
        scroll.setWidget(self._fleet_list_widget)
        layout.addWidget(scroll, 1)

        # Fleet Support add buttons
        sep = QFrame(panel)
        sep.setFrameShape(QFrame.HLine)
        sep.setStyleSheet(f"background: {P.border}; max-height: 1px;")
        layout.addWidget(sep)

        support_lbl = QLabel("Add Fleet Support", panel)
        support_lbl.setStyleSheet(
            f"font-family: Consolas, monospace; font-size: 7pt; color: {P.fg_dim}; background: transparent;"
        )
        layout.addWidget(support_lbl)

        support_types = ("Hauling", "Repair", "Refuel", "Escort", "Mothership", "Medical")
        from PySide6.QtWidgets import QGridLayout
        support_grid = QGridLayout()
        support_grid.setSpacing(4)
        for i, stype in enumerate(support_types):
            btn = QPushButton(stype, panel)
            btn.setStyleSheet(_BTN_STYLE)
            btn.clicked.connect(partial(self._on_add_support_ship, stype))
            support_grid.addWidget(btn, i // 2, i % 2)
        layout.addLayout(support_grid)

        self._refresh_fleet_panel()
        return panel

    def refresh_fleet_panel(self) -> None:
        """Public method — called by app.py when fleet changes."""
        self._refresh_fleet_panel()

    def _refresh_fleet_panel(self) -> None:
        # Clear existing widgets (keep stretch at end)
        while self._fleet_list_layout.count() > 1:
            item = self._fleet_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        # Merge individual ship slots and fleet snapshots, deduplicate by path
        all_snaps: list[LoadoutSnapshot] = []
        seen_paths: set[str] = set()
        for snap in self._ship_snapshots.values():
            if snap is not None:
                norm = os.path.normpath(snap.source_path)
                if norm not in seen_paths:
                    all_snaps.append(snap)
                    seen_paths.add(norm)
        for snap in self._fleet_snapshots:
            norm = os.path.normpath(snap.source_path)
            if norm not in seen_paths:
                all_snaps.append(snap)
                seen_paths.add(norm)

        # Group mining ships by type
        mining_cats: dict[str, list[tuple[str, str, str, str]]] = {
            "Prospector": [], "MOLE": [], "Golem": [],
        }
        for snap in all_snaps:
            ship_type = snap.ship
            if ship_type not in mining_cats:
                mining_cats[ship_type] = []
            name = os.path.splitext(os.path.basename(snap.source_path))[0]
            tooltip = self._format_loadout_tooltip(snap)
            mining_cats[ship_type].append((name, ship_type, snap.source_path, tooltip))

        idx = 0
        # Mining ship categories — collapsible
        for cat_name in ("Prospector", "MOLE", "Golem"):
            ships = mining_cats.get(cat_name, [])
            if not ships:
                continue
            accent = SHIP_TYPE_COLORS.get(cat_name, P.fg_bright)
            section = _CollapsibleSection(
                f"{cat_name}  ({len(ships)})", accent, self._fleet_list_widget,
            )
            for name, stype, path, tooltip in ships:
                row = _DraggableShipRow(name, stype, path, tooltip, section)
                section.content_layout.addWidget(row)
            self._fleet_list_layout.insertWidget(idx, section)
            idx += 1

        # Salvage ships — loaded from DPS Calculator
        if self._salvage_snapshots:
            accent = SHIP_TYPE_COLORS.get("Salvage", P.sc_cyan)
            section = _CollapsibleSection(
                f"Salvage  ({len(self._salvage_snapshots)})",
                accent, self._fleet_list_widget,
            )
            for snap in self._salvage_snapshots:
                display_name = os.path.splitext(os.path.basename(snap.source_path))[0]
                tooltip_lines = [f"Ship: {snap.ship}"]
                if getattr(snap, "salvage_heads", None):
                    tooltip_lines.append("Salvage heads:")
                    for h in snap.salvage_heads:
                        tooltip_lines.append(f"  + {h}")
                tooltip = "\n".join(tooltip_lines)
                row = _DraggableShipRow(
                    f"{display_name} ({snap.ship})",
                    "Salvage", snap.source_path, tooltip, section,
                )
                section.content_layout.addWidget(row)
            self._fleet_list_layout.insertWidget(idx, section)
            idx += 1

        # Fleet Support — group by support_type sub-category, each collapsible
        support_types = ("Hauling", "Repair", "Refuel", "Escort", "Mothership", "Medical")
        support_by_type: dict[str, list] = {st: [] for st in support_types}
        for fs in self._data.fleet_support_ships:
            support_by_type.setdefault(fs.support_type, []).append(fs)

        has_any_support = any(support_by_type[st] for st in support_types)
        if has_any_support:
            sep = QFrame(self._fleet_list_widget)
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet(f"background: {P.border}; max-height: 1px;")
            self._fleet_list_layout.insertWidget(idx, sep)
            idx += 1

            for st in support_types:
                ships_list = support_by_type.get(st, [])
                if not ships_list:
                    continue
                accent = SHIP_TYPE_COLORS.get(st, P.fg_dim)
                section = _CollapsibleSection(
                    f"{st}  ({len(ships_list)})", accent, self._fleet_list_widget,
                )
                for fs in ships_list:
                    tooltip = f"Role: {fs.support_type}\nModel: {fs.ship_model or '—'}\nMax crew: {fs.model_crew}"
                    row = _DraggableShipRow(
                        fs.name, fs.support_type, "", tooltip, section,
                        model_crew=fs.model_crew,
                    )
                    section.content_layout.addWidget(row)
                self._fleet_list_layout.insertWidget(idx, section)
                idx += 1

    @staticmethod
    def _format_loadout_tooltip(snap: LoadoutSnapshot) -> str:
        lines = [f"Ship: {snap.ship}"]
        for i, t in enumerate(snap.turrets):
            lines.append(f"  Turret {i+1}: {t.laser}")
            for m in t.modules:
                if m and "No Module" not in m:
                    lines.append(f"    + {m}")
        if snap.gadget and "No Gadget" not in snap.gadget:
            lines.append(f"  Gadget: {snap.gadget}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Player actions
    # ------------------------------------------------------------------

    def _on_player_search(self, _text: str) -> None:
        """Re-filter the player list when search text changes."""
        self._refresh_player_panel()

    def _on_profession_search(self, _text: str) -> None:
        self._refresh_profession_key()

    def _refresh_profession_key(self) -> None:
        """Rebuild the draggable profession list based on search query."""
        # Clear existing rows (keep the stretch at the end)
        while self._profession_list_layout.count() > 1:
            item = self._profession_list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        query = ""
        if hasattr(self, "_profession_search"):
            query = self._profession_search.text().strip()
        entries = profession_fuzzy_match(query)
        for pname, picon, pdesc in entries:
            row = _DraggableProfessionRow(pname, picon, pdesc, self._profession_list_widget)
            self._profession_list_layout.insertWidget(
                self._profession_list_layout.count() - 1, row,
            )

    def _on_toggle_assign_user(self, name: str, checked: bool) -> None:
        """Set or clear the assigned user, then center view on their team."""
        if checked:
            self._data.assigned_user = name
        else:
            if self._data.assigned_user == name:
                self._data.assigned_user = ""
        self._refresh_player_panel()
        self._schedule_save()
        if checked:
            self._center_on_assigned_user()

    def _center_on_assigned_user(self) -> None:
        """Center the canvas on the assigned user's ship or team."""
        name = self._data.assigned_user
        if not name:
            return
        # First try: find the ship the player is on
        ship = self._scene.find_ship_for_player(name)
        if ship:
            self._view.centerOn(ship)
            return
        # Second try: if player is a team leader, center on their team
        team = self._scene.find_team_for_player(name)
        if team:
            self._view.centerOn(team)

    def _on_add_player(self) -> None:
        name = self._player_input.text().strip()
        if not name:
            return
        if any(p.name == name for p in self._data.players):
            return
        self._data.players.append(PlayerEntry(name=name))
        self._player_input.clear()
        self._refresh_player_panel()
        self._schedule_save()

    def _on_remove_player(self, name: str) -> None:
        self._data.players = [p for p in self._data.players if p.name != name]
        # Also unassign from any ships and remove team if was leader
        was_leader = False
        for p in self._data.players:
            if p.name == name and p.is_leader:
                was_leader = True
                break
        self._scene.unassign_player_everywhere(name)
        if was_leader:
            self._scene.remove_team(name)
        self._refresh_player_panel()
        self._schedule_save()

    def _on_toggle_leader(self, name: str, checked: bool) -> None:
        for p in self._data.players:
            if p.name == name:
                p.is_leader = checked
                break

        if checked:
            t_node = self._scene.add_team(f"{name}'s Team", name)
            self._view.centerOn(t_node)
        else:
            self._scene.remove_team(name)

        self._refresh_player_panel()
        self._schedule_save()

    def _on_toggle_foreman(self, name: str, checked: bool) -> None:
        """Assign or unassign a player as the foreman."""
        if checked:
            self._data.foreman_name = name
            if self._scene._foreman:
                self._scene._foreman.node_name = name
                self._scene._foreman.team_name = name
                self._scene._foreman.update()
        else:
            # Unchecking: revert to default "Foreman"
            if self._data.foreman_name == name:
                self._data.foreman_name = "Foreman"
                if self._scene._foreman:
                    self._scene._foreman.node_name = "Foreman"
                    self._scene._foreman.team_name = "Foreman"
                    self._scene._foreman.update()
        self._refresh_player_panel()
        self._schedule_save()

    def _on_toggle_strike_leader(self, name: str, checked: bool) -> None:
        """Promote / demote a player as strike group leader of their group."""
        # Find which strike group this player is in (via their ship)
        target_group = None
        for ship in self._scene._ships:
            if name in ship.crew and ship.strike_group and ship.mothership_id:
                target_group = self._scene._find_strike_group(
                    ship.mothership_id, ship.strike_group,
                )
                break
        if target_group is None:
            return
        if checked:
            target_group.leader = name
        else:
            if target_group.leader == name:
                target_group.leader = ""
        target_group.update()
        self._refresh_player_panel()
        self._schedule_save()

    def _on_assign_profession(self, name: str, profession: str) -> None:
        """Set (or clear) a player's profession."""
        for p in self._data.players:
            if p.name == name:
                p.profession = profession
                break
        # Push profession lookup to scene so canvas badges update
        self._push_profession_lookup()
        # Redraw all badges so the icon appears/disappears on canvas
        for ship in self._scene._ships:
            if name in ship.crew:
                self._scene._add_crew_badges(ship)
        self._refresh_player_panel()
        self._schedule_save()

    def _push_profession_lookup(self) -> None:
        """Push {player_name: icon} map to the scene for badge rendering."""
        lookup = {
            p.name: profession_icon(p.profession)
            for p in self._data.players if p.profession
        }
        if hasattr(self._scene, "set_profession_lookup"):
            self._scene.set_profession_lookup(lookup)

    def _on_import_roster(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Player Roster", "", "JSON Files (*.json)"
        )
        if not path:
            return
        imported = import_player_roster(path)
        if imported:
            existing_names = {p.name for p in self._data.players}
            for p in imported:
                if p.name not in existing_names:
                    self._data.players.append(p)
                    existing_names.add(p.name)
                    if p.is_leader:
                        self._scene.add_team(f"{p.name}'s Team", p.name)
            self._refresh_player_panel()
            self._schedule_save()

    def _on_export_roster(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Player Roster", "player_roster.json", "JSON Files (*.json)"
        )
        if path:
            export_player_roster(self._data.players, path)

    # ------------------------------------------------------------------
    # Fleet Support actions
    # ------------------------------------------------------------------

    def _on_add_support_ship(self, support_type: str) -> None:
        picker = _ShipModelPicker(self)
        if picker.exec() != QDialog.Accepted or picker.selected_model is None:
            return
        model = picker.selected_model
        self._data.fleet_support_ships.append(FleetSupportShip(
            name=model.name,
            support_type=support_type,
            ship_model=model.name,
            model_crew=model.crew_max,
        ))
        self._refresh_fleet_panel()
        self._schedule_save()

    # ------------------------------------------------------------------
    # Fullscreen
    # ------------------------------------------------------------------

    def _toggle_fullscreen(self) -> None:
        """Toggle the parent SCWindow between maximized and normal."""
        window = self.window()
        if window:
            if window.isMaximized():
                window.showNormal()
            else:
                window.showMaximized()

    def _on_export_roster_file(self) -> None:
        """Export the entire roster to a user-chosen JSON file."""
        default_dir = os.path.join(
            os.path.expanduser("~"), "Documents", "SC Loadouts",
        )
        os.makedirs(default_dir, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Mining Roster",
            os.path.join(default_dir, "mining_roster_export.json"),
            "JSON Files (*.json)",
        )
        if not path:
            return
        # Force a save-to-data pass first
        self._do_save()
        from services.ledger_store import save_ledger
        save_ledger(self._data, path)

    def _on_load_roster_file(self) -> None:
        """Load a roster JSON file — replaces the current roster entirely."""
        default_dir = os.path.join(
            os.path.expanduser("~"), "Documents", "SC Loadouts",
        )
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Mining Roster", default_dir,
            "JSON Files (*.json)",
        )
        if not path:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Load Roster")
        box.setText("Replace the current roster with the loaded file?")
        box.setInformativeText(
            "All current teams, ships, and player assignments will be replaced. "
            "Consider exporting first if you want to keep a backup."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.setStyleSheet(
            f"QMessageBox {{ background: {P.bg_primary}; color: {P.fg}; }}"
            f"QLabel {{ color: {P.fg}; font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QPushButton {{ background: {P.bg_input}; color: {P.fg}; "
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 4px 12px; "
            f"min-width: 60px; font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QPushButton:hover {{ border-color: {_ACCENT}; }}"
        )
        if box.exec() != QMessageBox.Yes:
            return
        from services.ledger_store import load_ledger
        loaded = load_ledger(path)
        self._data = loaded
        self._scene.set_ledger_data(self._data)
        self._push_profession_lookup()
        self._refresh_player_panel()
        self._refresh_fleet_panel()
        self._refresh_cluster_filters()
        self._schedule_save()
        QTimer.singleShot(50, self._view.center_on_content)

    def _on_clear_players(self) -> None:
        """Remove all players from the roster. Ships and teams remain."""
        if not self._data.players:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Clear Players")
        box.setText(f"Remove all {len(self._data.players)} players?")
        box.setInformativeText("Ships and teams will remain. This cannot be undone.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        # Unassign all crew from all ships
        for ship in self._scene._ships:
            ship.crew.clear()
            self._scene._add_crew_badges(ship)
        # Clear strike group leaders
        for g in self._scene._strike_groups:
            g.leader = ""
            g.update()
        self._data.players.clear()
        self._data.assigned_user = ""
        self._data.foreman_name = "Foreman"
        if self._scene._foreman:
            self._scene._foreman.node_name = "Foreman"
            self._scene._foreman.team_name = "Foreman"
            self._scene._foreman.update()
        self._refresh_player_panel()
        self._schedule_save()

    def _on_clear_ships(self) -> None:
        """Remove all ships from the canvas. Players and teams remain."""
        if not self._scene._ships:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Clear Ships")
        box.setText(f"Remove all {len(self._scene._ships)} ships from the canvas?")
        box.setInformativeText("Players and teams will remain. This cannot be undone.")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        if box.exec() != QMessageBox.Yes:
            return
        # Remove all strike groups first
        for g in list(self._scene._strike_groups):
            self._scene.remove_strike_group(g)
        # Remove all ships (from teams, foreman, unassigned)
        for ship in list(self._scene._ships):
            self._scene.remove_ship(ship)
        self._refresh_player_panel()
        self._schedule_save()

    def _on_clear_ledger(self) -> None:
        """Wipe all teams, ships, strike groups, and player assignments.
        Players themselves are kept in the roster — demoted to Unassigned."""
        box = QMessageBox(self)
        box.setWindowTitle("Clear Roster")
        box.setText("Remove all teams, ships, and strike groups?")
        box.setInformativeText(
            "Players will remain in the roster but all team and ship "
            "assignments will be cleared. This cannot be undone."
        )
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.setDefaultButton(QMessageBox.No)
        box.setStyleSheet(
            f"QMessageBox {{ background: {P.bg_primary}; color: {P.fg}; }}"
            f"QLabel {{ color: {P.fg}; font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QPushButton {{ background: {P.bg_input}; color: {P.fg}; "
            f"border: 1px solid {P.border}; border-radius: 3px; padding: 4px 12px; "
            f"min-width: 60px; font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QPushButton:hover {{ border-color: {_ACCENT}; }}"
        )
        if box.exec() != QMessageBox.Yes:
            return

        # Wipe ledger data but preserve players, support ships, assigned_user, foreman name
        preserved_players = list(self._data.players)
        preserved_support = list(self._data.fleet_support_ships)
        preserved_user = self._data.assigned_user
        preserved_foreman = self._data.foreman_name
        # Demote all players (leader flag cleared)
        for p in preserved_players:
            p.is_leader = False

        from services.ledger_store import LedgerData
        self._data = LedgerData(
            foreman_name=preserved_foreman,
            players=preserved_players,
            fleet_support_ships=preserved_support,
            assigned_user=preserved_user,
        )

        # Rebuild scene from the cleared data
        self._scene.set_ledger_data(self._data)
        self._refresh_player_panel()
        self._refresh_fleet_panel()
        self._refresh_cluster_filters()
        self._schedule_save()
        QTimer.singleShot(50, self._view.center_on_content)

    # ------------------------------------------------------------------
    # Cluster filtering
    # ------------------------------------------------------------------

    def _refresh_cluster_filters(self) -> None:
        clusters = self._scene.all_clusters()
        if not clusters:
            self._cluster_bar.hide()
            return
        self._cluster_bar.show()
        # Remove old checkboxes
        for cb in self._cluster_checkboxes.values():
            self._cluster_cb_area.removeWidget(cb)
            cb.deleteLater()
        self._cluster_checkboxes.clear()

        # Find assigned user's cluster for highlighting
        user_cluster = ""
        if self._data.assigned_user:
            team = self._scene.find_team_for_player(self._data.assigned_user)
            if team:
                user_cluster = self._scene.cluster_for_team(team)

        for letter in clusters:
            cb = QCheckBox(letter, self._cluster_bar)
            cb.setChecked(True)
            is_user = (letter == user_cluster and user_cluster)
            color = _ACCENT if is_user else P.fg_dim
            cb.setStyleSheet(
                f"QCheckBox {{ color: {color}; font-family: Consolas, monospace; "
                f"font-size: 7pt; font-weight: {'bold' if is_user else 'normal'}; background: transparent; }}"
                f"QCheckBox::indicator {{ width: 12px; height: 12px; }}"
            )
            cb.stateChanged.connect(self._on_cluster_filter_changed)
            self._cluster_checkboxes[letter] = cb
            self._cluster_cb_area.addWidget(cb)

    def _on_cluster_filter_changed(self) -> None:
        visible = {letter for letter, cb in self._cluster_checkboxes.items() if cb.isChecked()}
        self._scene.set_cluster_visibility(visible)

    def _check_all_clusters(self) -> None:
        for cb in self._cluster_checkboxes.values():
            cb.setChecked(True)

    def _uncheck_all_clusters(self) -> None:
        for cb in self._cluster_checkboxes.values():
            cb.setChecked(False)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _on_hierarchy_changed(self) -> None:
        self._schedule_save()
        self._schedule_player_refresh()
        self._refresh_cluster_filters()

    def _schedule_player_refresh(self) -> None:
        """Debounced refresh of the player panel after canvas changes."""
        if not hasattr(self, "_player_refresh_timer"):
            self._player_refresh_timer = QTimer(self)
            self._player_refresh_timer.setSingleShot(True)
            self._player_refresh_timer.setInterval(300)
            self._player_refresh_timer.timeout.connect(self._refresh_player_panel)
        self._player_refresh_timer.start()

    def _schedule_save(self) -> None:
        self._save_timer.start()

    def _do_save(self) -> None:
        assigned = self._data.assigned_user
        self._data = self._scene.to_ledger_data(
            self._data.players, self._data.fleet_support_ships,
        )
        self._data.assigned_user = assigned
        save_ledger(self._data, self._ledger_path)
