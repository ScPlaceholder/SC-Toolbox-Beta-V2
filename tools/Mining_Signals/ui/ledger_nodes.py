"""
QGraphicsItem subclasses for the Mining Ledger hierarchy canvas.

Node types:
  ForemanNode  — root of the hierarchy (top of chart)
  TeamNode     — one per mining team / leader
  ShipNode     — a ship assigned to a team
  PlayerBadge  — small tag on a ShipNode for each crew member
  ConnectionLine — bezier line linking parent → child
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Optional

from PySide6.QtCore import (
    Qt, QRectF, QPointF, Signal, QMimeData,
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QPainterPath,
    QLinearGradient, QDrag, QFontMetrics,
)
from PySide6.QtWidgets import (
    QGraphicsObject, QGraphicsPathItem, QGraphicsSceneMouseEvent,
    QGraphicsItem, QStyleOptionGraphicsItem, QWidget,
    QGraphicsSceneContextMenuEvent, QMenu,
    QGraphicsTextItem, QLineEdit,
)

from shared.qt.theme import P

log = logging.getLogger(__name__)

# Ship type → accent colour
SHIP_TYPE_COLORS: dict[str, str] = {
    "Prospector": P.green,
    "MOLE": P.yellow,
    "Golem": P.orange,
    "Hauling": P.energy_cyan,
    "Repair": P.accent,
    "Refuel": P.purple,
    "Escort": P.red,
    "Mothership": P.fg_bright,
    "Medical": P.green,
    "Salvage": P.sc_cyan,
}

_FONT_FAMILY = "Consolas, monospace"
_HEADER_FONT_FAMILY = "Electrolize, Consolas, monospace"

# Node dimensions
_FOREMAN_W, _FOREMAN_H = 200, 60
_TEAM_W, _TEAM_H = 240, 56
_SHIP_W, _SHIP_H = 200, 50
_BADGE_W, _BADGE_H = 110, 22
_PORT_RADIUS = 4


# ---------------------------------------------------------------------------
# Helper: rounded-rect painter
# ---------------------------------------------------------------------------

def _paint_node_rect(
    painter: QPainter,
    rect: QRectF,
    fill: str,
    border_color: str,
    radius: float = 6.0,
    border_width: float = 1.5,
    accent_left: str | None = None,
) -> None:
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor(border_color), border_width))
    painter.setBrush(QBrush(QColor(fill)))
    painter.drawRoundedRect(rect, radius, radius)
    if accent_left:
        bar = QRectF(rect.x(), rect.y() + radius, 3, rect.height() - 2 * radius)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(accent_left)))
        painter.drawRect(bar)


# ---------------------------------------------------------------------------
# Base node
# ---------------------------------------------------------------------------

class LedgerNodeBase(QGraphicsObject):
    """Base for all draggable ledger nodes."""

    position_changed = Signal()

    def __init__(self, width: float, height: float, parent=None) -> None:
        super().__init__(parent)
        self._w = width
        self._h = height
        self.setFlags(
            QGraphicsItem.ItemIsMovable
            | QGraphicsItem.ItemIsSelectable
            | QGraphicsItem.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self._hovered = False

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def output_port(self) -> QPointF:
        return self.mapToScene(QPointF(self._w / 2, self._h))

    def input_port(self) -> QPointF:
        return self.mapToScene(QPointF(self._w / 2, 0))

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            self.position_changed.emit()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):
        self._hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hovered = False
        self.update()
        super().hoverLeaveEvent(event)


# ---------------------------------------------------------------------------
# Foreman node
# ---------------------------------------------------------------------------

class ForemanNode(LedgerNodeBase):
    """Root node — the foreman's team. Accepts ship drops like TeamNode."""

    name_changed = Signal(str)
    ship_dropped = Signal(object)  # emits dict with ship info

    def __init__(self, name: str = "Foreman", parent=None) -> None:
        super().__init__(_FOREMAN_W, _FOREMAN_H, parent)
        self.node_name = name
        # TeamNode-compatible attributes so the scene can treat it uniformly
        self.team_name = name
        self.leader_name = name
        self._editing = False
        self._edit_proxy = None
        self._drop_highlight = False
        self._drag_origin: QPointF | None = None
        self._drag_children_starts: list = []
        self.setAcceptDrops(True)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None) -> None:
        rect = self.boundingRect().adjusted(1, 1, -1, -1)
        if self._drop_highlight:
            border = P.green
        elif self._hovered:
            border = P.green
        else:
            border = P.border_card
        _paint_node_rect(painter, rect, P.bg_card, border, radius=8, border_width=2.0)

        # Glow on top edge
        grad = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + 12))
        grad.setColorAt(0, QColor(P.green))
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width(), 12), 8, 8)

        # Title
        painter.setPen(QPen(QColor(P.green)))
        font = QFont(_HEADER_FONT_FAMILY, 11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(12, 6, -12, -20), Qt.AlignCenter | Qt.AlignTop, self.node_name)

        # Subtitle
        painter.setPen(QPen(QColor(P.fg_dim)))
        sub_font = QFont(_FONT_FAMILY, 7)
        painter.setFont(sub_font)
        painter.drawText(rect.adjusted(12, 0, -12, -6), Qt.AlignCenter | Qt.AlignBottom, "FOREMAN")

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            self._drop_highlight = True
            self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_highlight = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drop_highlight = False
        self.update()
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-ship")).decode())
            self.ship_dropped.emit(data)
            event.acceptProposedAction()
        else:
            event.ignore()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin = self.pos()
            self._drag_children_starts = []
            scene = self.scene()
            if scene is not None and hasattr(scene, "get_team_descendants"):
                for child in scene.get_team_descendants(self):
                    self._drag_children_starts.append((child, child.pos()))
        super().mousePressEvent(event)

    def itemChange(self, change, value):
        if (change == QGraphicsItem.ItemPositionHasChanged
                and self._drag_origin is not None
                and self._drag_children_starts):
            delta = self.pos() - self._drag_origin
            for child, start in self._drag_children_starts:
                try:
                    child.setPos(start + delta)
                except RuntimeError:
                    pass
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._drag_origin = None
        self._drag_children_starts = []
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._start_edit()

    def _start_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        scene = self.scene()
        if not scene:
            self._editing = False
            return
        self._edit_proxy = scene.addWidget(self._make_editor(scene))
        self._edit_proxy.setPos(self.scenePos() + QPointF(10, 8))
        self._edit_proxy.widget().setFocus()

    def _make_editor(self, scene) -> QLineEdit:
        ed = QLineEdit(self.node_name)
        ed.setFixedWidth(int(self._w - 24))
        ed.setStyleSheet(
            f"background: {P.bg_input}; color: {P.fg_bright}; border: 1px solid {P.green}; "
            f"font-family: {_HEADER_FONT_FAMILY}; font-size: 11pt; padding: 2px 4px;"
        )
        ed.selectAll()

        def _finish():
            if not self._editing:
                return
            text = ed.text().strip()
            if text:
                self.node_name = text
                self.team_name = text
                self.name_changed.emit(text)
            self._editing = False
            self.update()
            try:
                if self._edit_proxy and self.scene():
                    self.scene().removeItem(self._edit_proxy)
            except RuntimeError:
                pass
            self._edit_proxy = None

        ed.returnPressed.connect(_finish)
        return ed


# ---------------------------------------------------------------------------
# Team node
# ---------------------------------------------------------------------------

class TeamNode(LedgerNodeBase):
    """Team name header bubble — drop ships onto this to assign them."""

    name_changed = Signal(str)
    ship_dropped = Signal(object)  # emits dict with ship info
    drag_released = Signal(object)  # emits self — for snap-to-parent-team
    cluster_changed = Signal(str)  # emits new cluster letter

    def __init__(self, team_name: str, leader_name: str, accent: str = P.accent, parent=None) -> None:
        super().__init__(_TEAM_W, _TEAM_H, parent)
        self.team_name = team_name
        self.leader_name = leader_name
        self._accent = accent
        self.cluster: str = ""
        self._editing = False
        self._edit_proxy = None
        self._drop_highlight = False
        self._drag_origin: QPointF | None = None
        self._drag_children_starts: list = []
        self.setAcceptDrops(True)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None) -> None:
        rect = self.boundingRect().adjusted(1, 1, -1, -1)

        # Highlight when a ship is being dragged over
        if self._drop_highlight:
            border_color = P.green
            border_w = 2.5
        elif self._hovered:
            border_color = self._accent
            border_w = 2.0
        else:
            border_color = P.border_card
            border_w = 1.5

        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(border_color), border_w))
        painter.setBrush(QBrush(QColor(P.bg_card)))
        painter.drawRoundedRect(rect, 8, 8)

        # Accent glow on top
        grad = QLinearGradient(rect.topLeft(), QPointF(rect.left(), rect.top() + 10))
        grad.setColorAt(0, QColor(self._accent))
        grad.setColorAt(1, QColor(0, 0, 0, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(grad))
        painter.drawRoundedRect(QRectF(rect.x(), rect.y(), rect.width(), 10), 8, 8)

        # Team name (centered, prominent)
        painter.setPen(QPen(QColor(P.fg_bright)))
        font = QFont(_HEADER_FONT_FAMILY, 11)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(12, 4, -12, -18), Qt.AlignCenter | Qt.AlignTop, self.team_name)

        # Leader subtitle (left) + cluster label (right)
        sub_font = QFont(_FONT_FAMILY, 7)
        painter.setFont(sub_font)
        painter.setPen(QPen(QColor(self._accent)))
        painter.drawText(rect.adjusted(12, 0, -12, -6), Qt.AlignLeft | Qt.AlignBottom, f"Leader: {self.leader_name}")
        if self.cluster:
            painter.setPen(QPen(QColor(P.fg_dim)))
            cluster_font = QFont(_FONT_FAMILY, 7)
            cluster_font.setItalic(True)
            painter.setFont(cluster_font)
            painter.drawText(rect.adjusted(12, 0, -12, -6), Qt.AlignRight | Qt.AlignBottom, f"Cluster {self.cluster}")

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            self._drop_highlight = True
            self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_highlight = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drop_highlight = False
        self.update()
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-ship")).decode())
            self.ship_dropped.emit(data)
            event.acceptProposedAction()
        else:
            event.ignore()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin = self.pos()
            self._drag_children_starts = []
            scene = self.scene()
            if scene is not None and hasattr(scene, "get_team_descendants"):
                for child in scene.get_team_descendants(self):
                    self._drag_children_starts.append((child, child.pos()))
        super().mousePressEvent(event)

    def itemChange(self, change, value):
        if (change == QGraphicsItem.ItemPositionHasChanged
                and self._drag_origin is not None
                and self._drag_children_starts):
            delta = self.pos() - self._drag_origin
            for child, start in self._drag_children_starts:
                try:
                    child.setPos(start + delta)
                except RuntimeError:
                    pass
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._drag_origin = None
        self._drag_children_starts = []
        super().mouseReleaseEvent(event)
        self.drag_released.emit(self)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu()
        menu.setStyleSheet(
            f"QMenu {{ background: {P.bg_card}; color: {P.fg}; border: 1px solid {P.border}; "
            f"font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QMenu::item:selected {{ background: {P.bg_input}; }}"
            f"QMenu::separator {{ background: {P.border}; height: 1px; margin: 4px 8px; }}"
        )
        cluster_menu = menu.addMenu("Assign to Cluster")
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            prefix = "●  " if letter == self.cluster else "    "
            a = cluster_menu.addAction(f"{prefix}{letter}")
            a.setData(("cluster", letter))
        if self.cluster:
            menu.addSeparator()
            a = menu.addAction(f"✕  Remove from Cluster {self.cluster}")
            a.setData(("uncluster", None))
        chosen = menu.exec(event.screenPos())
        if not chosen:
            return
        action_type, payload = chosen.data()
        if action_type == "cluster":
            self.cluster = payload
            self.cluster_changed.emit(payload)
            self.update()
        elif action_type == "uncluster":
            self.cluster = ""
            self.cluster_changed.emit("")
            self.update()

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._start_edit()

    def _start_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        scene = self.scene()
        if not scene:
            self._editing = False
            return
        self._edit_proxy = scene.addWidget(self._make_editor(scene))
        self._edit_proxy.setPos(self.scenePos() + QPointF(12, 6))
        self._edit_proxy.widget().setFocus()

    def _make_editor(self, scene) -> QLineEdit:
        ed = QLineEdit(self.team_name)
        ed.setFixedWidth(int(self._w - 30))
        ed.setStyleSheet(
            f"background: {P.bg_input}; color: {P.fg_bright}; border: 1px solid {self._accent}; "
            f"font-family: {_HEADER_FONT_FAMILY}; font-size: 10pt; padding: 2px 4px;"
        )
        ed.selectAll()

        def _finish():
            if not self._editing:
                return
            text = ed.text().strip()
            if text:
                self.team_name = text
                self.name_changed.emit(text)
            self._editing = False
            self.update()
            try:
                if self._edit_proxy and self.scene():
                    self.scene().removeItem(self._edit_proxy)
            except RuntimeError:
                pass
            self._edit_proxy = None

        ed.returnPressed.connect(_finish)
        return ed


# ---------------------------------------------------------------------------
# Ship node
# ---------------------------------------------------------------------------

class ShipNode(LedgerNodeBase):
    """A ship assigned to a team on the canvas."""

    player_dropped = Signal(str)  # player name
    request_delete = Signal(object)  # emits self
    request_snap = Signal(object, object)  # emits (self, team_node)
    drag_released = Signal(object)  # emits self — fired on mouse release after drag
    ship_dropped_on_me = Signal(object)  # emits ship info dict (mothership only)
    request_add_strike_group = Signal(object)  # emits self (mothership only)

    def __init__(
        self,
        ship_name: str,
        ship_type: str,
        loadout_path: str = "",
        crew: list[str] | None = None,
        model_crew: int = 0,
        unique_id: str = "",
        mothership_id: str = "",
        strike_group: str = "",
        parent=None,
    ) -> None:
        super().__init__(_SHIP_W, _SHIP_H, parent)
        self.ship_name = ship_name
        self.ship_type = ship_type
        self.loadout_path = loadout_path
        self.crew: list[str] = crew or []
        self.model_crew = model_crew  # from ship DB; 0 = use heuristic
        self.unique_id = unique_id or uuid.uuid4().hex[:8]
        self.mothership_id = mothership_id
        self.strike_group = strike_group
        self._accent = SHIP_TYPE_COLORS.get(ship_type, P.fg_dim)
        self.setAcceptDrops(True)

    _FALLBACK_CREW = {
        "MOLE": 4, "Prospector": 1, "Golem": 1,
        "Hauling": 2, "Repair": 2, "Refuel": 2,
        "Escort": 2, "Mothership": 8, "Medical": 2,
    }

    @property
    def max_crew(self) -> int:
        if self.model_crew > 0:
            return self.model_crew
        return self._FALLBACK_CREW.get(self.ship_type, 2)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None) -> None:
        rect = self.boundingRect().adjusted(1, 1, -1, -1)
        border = self._accent if self._hovered else P.border_card
        _paint_node_rect(painter, rect, P.bg_card, border, accent_left=self._accent)

        # Ship name
        painter.setPen(QPen(QColor(P.fg_bright)))
        font = QFont(_FONT_FAMILY, 9)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(rect.adjusted(14, 6, -50, 0), Qt.AlignLeft | Qt.AlignVCenter, self.ship_name)

        # Type badge
        painter.setPen(QPen(QColor(self._accent)))
        type_font = QFont(_FONT_FAMILY, 7)
        painter.setFont(type_font)
        fm = QFontMetrics(type_font)
        type_text = self.ship_type.upper()
        tw = fm.horizontalAdvance(type_text)
        badge_rect = QRectF(rect.right() - tw - 20, rect.y() + 6, tw + 12, 18)
        painter.setBrush(QBrush(QColor(P.bg_secondary)))
        painter.drawRoundedRect(badge_rect, 4, 4)
        painter.drawText(badge_rect, Qt.AlignCenter, type_text)

        # Crew count
        painter.setPen(QPen(QColor(P.fg_dim)))
        crew_font = QFont(_FONT_FAMILY, 7)
        painter.setFont(crew_font)
        crew_text = f"{len(self.crew)}/{self.max_crew} crew"
        painter.drawText(rect.adjusted(14, 0, -12, -4), Qt.AlignLeft | Qt.AlignBottom, crew_text)

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-player"):
            event.acceptProposedAction()
        elif (self.ship_type == "Mothership"
              and event.mimeData().hasFormat("application/x-sc-ledger-ship")):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-player"):
            event.acceptProposedAction()
        elif (self.ship_type == "Mothership"
              and event.mimeData().hasFormat("application/x-sc-ledger-ship")):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-player"):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-player")).decode())
            name = data.get("name", "")
            if name and name not in self.crew:
                self.player_dropped.emit(name)
            event.acceptProposedAction()
        elif (self.ship_type == "Mothership"
              and event.mimeData().hasFormat("application/x-sc-ledger-ship")):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-ship")).decode())
            self.ship_dropped_on_me.emit(data)
            event.acceptProposedAction()
        else:
            event.ignore()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        """On release, check if we're near a team node and snap to it."""
        super().mouseReleaseEvent(event)
        self.drag_released.emit(self)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu()
        menu.setStyleSheet(
            f"QMenu {{ background: {P.bg_card}; color: {P.fg}; border: 1px solid {P.border}; "
            f"font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QMenu::item:selected {{ background: {P.bg_input}; }}"
            f"QMenu::separator {{ background: {P.border}; height: 1px; margin: 4px 8px; }}"
        )

        # Delete option
        delete_action = menu.addAction(f"🗑  Delete {self.ship_name}")
        delete_action.setData(("delete", None))

        # Assign-to-team options (includes foreman as a team)
        scene = self.scene()
        if scene:
            current_team = None
            for t, ships in getattr(scene, "_team_ships", {}).items():
                if self in ships:
                    current_team = t
                    break

            # Build list of all assignable targets: foreman + all teams
            all_targets = []
            foreman = getattr(scene, "_foreman", None)
            if foreman and foreman is not current_team:
                all_targets.append(foreman)
            for t in getattr(scene, "_teams", []):
                if t is not current_team:
                    all_targets.append(t)

            if all_targets:
                menu.addSeparator()
                for t in all_targets:
                    label = getattr(t, "team_name", "Team")
                    a = menu.addAction(f"→  Assign to {label}")
                    a.setData(("assign", t))

            # Unassign option if currently in a team
            if current_team:
                menu.addSeparator()
                label = getattr(current_team, "team_name", "Team")
                unassign_action = menu.addAction(f"↩  Unassign from {label}")
                unassign_action.setData(("unassign", current_team))

        # Mothership: add strike group option
        if self.ship_type == "Mothership":
            menu.addSeparator()
            a = menu.addAction("⚔  Add Strike Group")
            a.setData(("add_strike_group", None))

        # Crew unassign options
        if self.crew:
            menu.addSeparator()
            for name in self.crew:
                a = menu.addAction(f"✕  Unassign {name}")
                a.setData(("unassign_crew", name))

        chosen = menu.exec(event.screenPos())
        if not chosen:
            return

        action_type, payload = chosen.data()
        if action_type == "delete":
            self.request_delete.emit(self)
        elif action_type == "assign":
            self.request_snap.emit(self, payload)
        elif action_type == "unassign":
            # Move to unassigned
            self.request_snap.emit(self, None)
        elif action_type == "add_strike_group":
            self.request_add_strike_group.emit(self)
        elif action_type == "unassign_crew":
            if payload in self.crew:
                self.crew.remove(payload)
                self.update()
                self.position_changed.emit()


# ---------------------------------------------------------------------------
# Player badge
# ---------------------------------------------------------------------------

class PlayerBadge(QGraphicsObject):
    """Small pill attached to a ShipNode showing assigned crew member."""

    def __init__(
        self,
        name: str,
        accent: str = P.fg_dim,
        profession_icon: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.player_name = name
        self._accent = accent
        self.profession_icon = profession_icon
        self._w = _BADGE_W
        self._h = _BADGE_H

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self._w, self._h)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None) -> None:
        rect = self.boundingRect().adjusted(1, 1, -1, -1)
        painter.setRenderHint(QPainter.Antialiasing)

        # Tinted background using the ship accent color (~18% alpha)
        accent = QColor(self._accent)
        bg = QColor(accent)
        bg.setAlpha(46)
        painter.setPen(QPen(accent, 1.2))
        painter.setBrush(QBrush(bg))
        painter.drawRoundedRect(rect, 4, 4)

        # Prominent left accent bar matching the ship type
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(accent))
        painter.drawRect(QRectF(rect.x() + 1, rect.y() + 2, 3, rect.height() - 4))

        # Profession icon (if set)
        text_x = 10
        if self.profession_icon:
            icon_font = QFont("Segoe UI Emoji, Segoe UI Symbol", 8)
            painter.setFont(icon_font)
            painter.setPen(QPen(QColor(P.fg_bright)))
            icon_rect = QRectF(rect.x() + 8, rect.y(), 14, rect.height())
            painter.drawText(icon_rect, Qt.AlignLeft | Qt.AlignVCenter, self.profession_icon)
            text_x = 22

        # Player name
        painter.setPen(QPen(QColor(P.fg_bright)))
        font = QFont(_FONT_FAMILY, 7)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            rect.adjusted(text_x, 0, -6, 0),
            Qt.AlignLeft | Qt.AlignVCenter,
            self.player_name,
        )


# ---------------------------------------------------------------------------
# Strike group node
# ---------------------------------------------------------------------------

_SG_W, _SG_H = 180, 34


class StrikeGroupNode(LedgerNodeBase):
    """A nameable group of ships attached to a mothership."""

    name_changed = Signal(str)
    ship_dropped = Signal(object)  # emits ship info dict
    request_delete = Signal(object)  # emits self

    def __init__(self, name: str, mothership_id: str, leader: str = "", parent=None) -> None:
        super().__init__(_SG_W, _SG_H, parent)
        self.name_text = name
        self.mothership_id = mothership_id
        self.leader = leader
        self._editing = False
        self._edit_proxy = None
        self._drag_origin: QPointF | None = None
        self._drag_children_starts: list = []
        self._drop_highlight = False
        self.setAcceptDrops(True)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: QWidget = None) -> None:
        rect = self.boundingRect().adjusted(1, 1, -1, -1)
        if self._drop_highlight:
            border_color = P.green
            border_w = 2.0
        elif self._hovered:
            border_color = P.purple
            border_w = 1.8
        else:
            border_color = P.border_card
            border_w = 1.2
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(border_color), border_w))
        painter.setBrush(QBrush(QColor(P.bg_card)))
        painter.drawRoundedRect(rect, 5, 5)

        # Purple accent bar on left
        painter.setPen(Qt.NoPen)
        painter.setBrush(QBrush(QColor(P.purple)))
        painter.drawRect(QRectF(rect.x(), rect.y() + 4, 3, rect.height() - 8))

        # Name
        painter.setPen(QPen(QColor(P.fg_bright)))
        font = QFont(_FONT_FAMILY, 8)
        font.setBold(True)
        painter.setFont(font)
        title_rect = rect.adjusted(10, 2, -6, -14 if self.leader else -2)
        painter.drawText(title_rect, Qt.AlignLeft | Qt.AlignVCenter, self.name_text)

        # Leader subtitle
        if self.leader:
            painter.setPen(QPen(QColor(P.purple)))
            sub_font = QFont(_FONT_FAMILY, 6)
            painter.setFont(sub_font)
            painter.drawText(
                rect.adjusted(10, 0, -6, -2),
                Qt.AlignLeft | Qt.AlignBottom,
                f"SGL: {self.leader}",
            )

    def dragEnterEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            self._drop_highlight = True
            self.update()
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._drop_highlight = False
        self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event):
        self._drop_highlight = False
        self.update()
        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-ship")).decode())
            self.ship_dropped.emit(data)
            event.acceptProposedAction()
        else:
            event.ignore()

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        menu = QMenu()
        menu.setStyleSheet(
            f"QMenu {{ background: {P.bg_card}; color: {P.fg}; border: 1px solid {P.border}; "
            f"font-family: Consolas, monospace; font-size: 8pt; }}"
            f"QMenu::item:selected {{ background: {P.bg_input}; }}"
        )
        a = menu.addAction(f"🗑  Delete {self.name_text}")
        a.setData(("delete", None))
        chosen = menu.exec(event.screenPos())
        if chosen and chosen.data()[0] == "delete":
            self.request_delete.emit(self)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_origin = self.pos()
            self._drag_children_starts = []
            scene = self.scene()
            if scene is not None and hasattr(scene, "get_strike_group_ships"):
                for child in scene.get_strike_group_ships(self):
                    self._drag_children_starts.append((child, child.pos()))
        super().mousePressEvent(event)

    def itemChange(self, change, value):
        if (change == QGraphicsItem.ItemPositionHasChanged
                and self._drag_origin is not None
                and self._drag_children_starts):
            delta = self.pos() - self._drag_origin
            for child, start in self._drag_children_starts:
                try:
                    child.setPos(start + delta)
                except RuntimeError:
                    pass
        return super().itemChange(change, value)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._drag_origin = None
        self._drag_children_starts = []
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._start_edit()

    def _start_edit(self) -> None:
        if self._editing:
            return
        self._editing = True
        scene = self.scene()
        if not scene:
            self._editing = False
            return
        self._edit_proxy = scene.addWidget(self._make_editor())
        self._edit_proxy.setPos(self.scenePos() + QPointF(8, 4))
        self._edit_proxy.widget().setFocus()

    def _make_editor(self) -> QLineEdit:
        ed = QLineEdit(self.name_text)
        ed.setFixedWidth(int(self._w - 16))
        ed.setStyleSheet(
            f"background: {P.bg_input}; color: {P.fg_bright}; border: 1px solid {P.purple}; "
            f"font-family: {_FONT_FAMILY}; font-size: 8pt; padding: 2px 4px;"
        )
        ed.selectAll()

        def _finish():
            if not self._editing:
                return
            text = ed.text().strip()
            if text:
                self.name_text = text
                self.name_changed.emit(text)
            self._editing = False
            self.update()
            try:
                if self._edit_proxy and self.scene():
                    self.scene().removeItem(self._edit_proxy)
            except RuntimeError:
                pass
            self._edit_proxy = None

        ed.returnPressed.connect(_finish)
        return ed


# ---------------------------------------------------------------------------
# Connection line
# ---------------------------------------------------------------------------

class ConnectionLine(QGraphicsPathItem):
    """Curved bezier line connecting two nodes."""

    def __init__(self, source: LedgerNodeBase, target: LedgerNodeBase | QGraphicsObject, parent=None) -> None:
        super().__init__(parent)
        self.source = source
        self.target = target
        self.setPen(QPen(QColor(P.fg_dim), 1.5, Qt.SolidLine))
        self.setZValue(-1)
        self.update_path()

    def update_path(self) -> None:
        start = self.source.output_port()
        end = self.target.input_port()
        dy = abs(end.y() - start.y()) * 0.5
        path = QPainterPath(start)
        path.cubicTo(
            QPointF(start.x(), start.y() + dy),
            QPointF(end.x(), end.y() - dy),
            end,
        )
        self.setPath(path)
