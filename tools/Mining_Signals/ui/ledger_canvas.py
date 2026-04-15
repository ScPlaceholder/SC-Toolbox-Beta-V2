"""
QGraphicsScene + QGraphicsView for the Mining Ledger hierarchy canvas.

Manages the visual node graph: foreman → teams → ships, with
connection lines drawn between them.  Supports drag-and-drop from
the side panels, team-to-team nesting, and auto-layout.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from PySide6.QtCore import Qt, Signal, QRectF, QPointF, QLineF
from PySide6.QtGui import (
    QColor, QPainter, QPen, QBrush, QWheelEvent, QDragEnterEvent,
    QDragMoveEvent, QDropEvent,
)
from PySide6.QtWidgets import (
    QGraphicsScene, QGraphicsView, QWidget, QGraphicsLineItem,
)

from shared.qt.theme import P
from services.ledger_store import (
    LedgerData, MiningTeam, CrewAssignment, PlayerEntry, StrikeGroupData,
)
from .ledger_nodes import (
    ForemanNode, TeamNode, ShipNode, PlayerBadge, StrikeGroupNode,
    ConnectionLine, SHIP_TYPE_COLORS, LedgerNodeBase,
    _SG_W, _SG_H, _SHIP_H,
)

log = logging.getLogger(__name__)

_GRID_SPACING = 30
_SCENE_W = 4000
_SCENE_H = 3000


class LedgerScene(QGraphicsScene):
    """Owns all ledger nodes and manages their relationships."""

    hierarchy_changed = Signal()
    profession_assigned = Signal(str, str)  # (player_name, profession)

    def __init__(self, parent=None) -> None:
        super().__init__(0, 0, _SCENE_W, _SCENE_H, parent)
        self._foreman: ForemanNode | None = None
        self._teams: list[TeamNode] = []
        self._ships: list[ShipNode] = []
        self._unassigned_ships: list[ShipNode] = []
        self._badges: list[PlayerBadge] = []
        self._badge_connectors: list = []  # QGraphicsLineItems tagged with ship id
        self._profession_lookup: dict[str, str] = {}  # player name → emoji icon
        self._connections: list[ConnectionLine] = []
        # Ship ownership: key is ForemanNode or TeamNode → [ShipNode]
        self._team_ships: dict[ForemanNode | TeamNode, list[ShipNode]] = {}
        # Team parent: key is TeamNode → parent (ForemanNode or TeamNode)
        self._team_parent: dict[TeamNode, ForemanNode | TeamNode] = {}
        # Strike groups
        self._strike_groups: list[StrikeGroupNode] = []
        # mothership unique_id → list of strike group nodes
        self._mothership_groups: dict[str, list[StrikeGroupNode]] = {}

    # ------------------------------------------------------------------
    # Build from data
    # ------------------------------------------------------------------

    def set_ledger_data(self, data: LedgerData) -> None:
        self.clear()
        self._foreman = None
        self._teams.clear()
        self._ships.clear()
        self._unassigned_ships.clear()
        self._badges.clear()
        self._badge_connectors.clear()
        self._connections.clear()
        self._team_ships.clear()
        self._team_parent.clear()
        self._strike_groups.clear()
        self._mothership_groups.clear()

        # Foreman — default to centered near top if never positioned
        self._foreman = ForemanNode(data.foreman_name)
        self.addItem(self._foreman)
        fx = data.foreman_pos.get("x", 0.0)
        fy = data.foreman_pos.get("y", 0.0)
        if fx == 0.0 and fy == 0.0:
            fx, fy = 400.0, 40.0
        self._foreman.setPos(fx, fy)
        self._foreman.position_changed.connect(self._on_node_moved)
        self._foreman.name_changed.connect(lambda _: self.hierarchy_changed.emit())
        self._foreman.ship_dropped.connect(
            lambda info: self.assign_ship_to_team(self._foreman, info)
        )
        self._team_ships[self._foreman] = []

        # Foreman ships
        for j, ship in enumerate(data.foreman_ships):
            s_node = self._create_ship_node(
                ship.ship_name, ship.ship_type, ship.loadout_path, ship.crew,
                model_crew=ship.model_crew,
                unique_id=ship.unique_id,
                mothership_id=ship.mothership_id,
                strike_group=ship.strike_group,
            )
            sx, sy = ship._pos.get("x", 0), ship._pos.get("y", 0)
            if sx == 0 and sy == 0:
                sx, sy = fx, fy + 80 + j * 70
            s_node.setPos(sx, sy)
            self._team_ships[self._foreman].append(s_node)
            self._add_crew_badges(s_node)

        # Teams — first pass: create all nodes
        team_by_leader: dict[str, TeamNode] = {}
        for i, team in enumerate(data.teams):
            t_node = self._create_team_node(team.name, team.leader)
            t_node.cluster = team.cluster
            tx, ty = team._pos.get("x", 0), team._pos.get("y", 0)
            if tx == 0 and ty == 0:
                tx, ty = 100 + i * 280, 180
            t_node.setPos(tx, ty)
            team_by_leader[team.leader] = t_node

            for j, ship in enumerate(team.ships):
                s_node = self._create_ship_node(
                    ship.ship_name, ship.ship_type, ship.loadout_path, ship.crew,
                    model_crew=ship.model_crew,
                    unique_id=ship.unique_id,
                    mothership_id=ship.mothership_id,
                    strike_group=ship.strike_group,
                )
                sx, sy = ship._pos.get("x", 0), ship._pos.get("y", 0)
                if sx == 0 and sy == 0:
                    sx, sy = tx, ty + 80 + j * 70
                s_node.setPos(sx, sy)
                self._team_ships[t_node].append(s_node)
                self._add_crew_badges(s_node)

        # Second pass: set parent relationships
        for team in data.teams:
            t_node = team_by_leader.get(team.leader)
            if not t_node:
                continue
            if team.parent_leader and team.parent_leader in team_by_leader:
                self._team_parent[t_node] = team_by_leader[team.parent_leader]
            else:
                self._team_parent[t_node] = self._foreman

        # Unassigned ships
        for j, ship in enumerate(data.unassigned_ships):
            s_node = self._create_ship_node(
                ship.ship_name, ship.ship_type, ship.loadout_path, ship.crew,
                model_crew=ship.model_crew,
                unique_id=ship.unique_id,
                mothership_id=ship.mothership_id,
                strike_group=ship.strike_group,
            )
            sx, sy = ship._pos.get("x", 0), ship._pos.get("y", 0)
            if sx == 0 and sy == 0:
                sx, sy = 200 + j * 240, 500
            s_node.setPos(sx, sy)
            self._unassigned_ships.append(s_node)
            self._add_crew_badges(s_node)

        # Strike groups (requires ships to be loaded first)
        for sg in data.strike_groups:
            mothership = self._find_mothership_by_id(sg.mothership_id)
            if mothership is None:
                continue
            group_node = self._create_strike_group_node(sg.name, sg.mothership_id, sg.leader)
            gx, gy = sg._pos.get("x", 0), sg._pos.get("y", 0)
            if gx == 0 and gy == 0:
                idx = len(self._mothership_groups.get(sg.mothership_id, [])) - 1
                gx = mothership.x() + idx * (_SG_W + 20)
                gy = mothership.y() + mothership._h + 24
            group_node.setPos(gx, gy)

        self._rebuild_connections()

    # ------------------------------------------------------------------
    # Extract current state back to data
    # ------------------------------------------------------------------

    def to_ledger_data(self, players: list[PlayerEntry], support_ships=None) -> LedgerData:
        data = LedgerData()
        if self._foreman:
            data.foreman_name = self._foreman.node_name
            data.foreman_pos = {"x": self._foreman.x(), "y": self._foreman.y()}
            # Foreman's own ships
            for s_node in self._team_ships.get(self._foreman, []):
                data.foreman_ships.append(CrewAssignment(
                    loadout_path=s_node.loadout_path,
                    ship_name=s_node.ship_name,
                    ship_type=s_node.ship_type,
                    crew=list(s_node.crew),
                    _pos={"x": s_node.x(), "y": s_node.y()},
                    model_crew=s_node.model_crew,
                    unique_id=s_node.unique_id,
                    mothership_id=s_node.mothership_id,
                    strike_group=s_node.strike_group,
                ))

        for t_node in self._teams:
            ships_data = []
            for s_node in self._team_ships.get(t_node, []):
                ships_data.append(CrewAssignment(
                    loadout_path=s_node.loadout_path,
                    ship_name=s_node.ship_name,
                    ship_type=s_node.ship_type,
                    crew=list(s_node.crew),
                    _pos={"x": s_node.x(), "y": s_node.y()},
                    model_crew=s_node.model_crew,
                    unique_id=s_node.unique_id,
                    mothership_id=s_node.mothership_id,
                    strike_group=s_node.strike_group,
                ))
            parent = self._team_parent.get(t_node)
            parent_leader = ""
            if isinstance(parent, TeamNode):
                parent_leader = parent.leader_name
            data.teams.append(MiningTeam(
                name=t_node.team_name,
                leader=t_node.leader_name,
                ships=ships_data,
                _pos={"x": t_node.x(), "y": t_node.y()},
                parent_leader=parent_leader,
                cluster=t_node.cluster,
            ))

        for s_node in self._unassigned_ships:
            data.unassigned_ships.append(CrewAssignment(
                loadout_path=s_node.loadout_path,
                ship_name=s_node.ship_name,
                ship_type=s_node.ship_type,
                crew=list(s_node.crew),
                _pos={"x": s_node.x(), "y": s_node.y()},
                model_crew=s_node.model_crew,
                unique_id=s_node.unique_id,
                mothership_id=s_node.mothership_id,
                strike_group=s_node.strike_group,
            ))

        # Strike groups
        for g in self._strike_groups:
            data.strike_groups.append(StrikeGroupData(
                name=g.name_text,
                mothership_id=g.mothership_id,
                leader=g.leader,
                _pos={"x": g.x(), "y": g.y()},
            ))

        data.players = list(players)
        if support_ships is not None:
            data.fleet_support_ships = list(support_ships)
        return data

    # ------------------------------------------------------------------
    # Team operations
    # ------------------------------------------------------------------

    def add_team(self, name: str, leader: str) -> TeamNode:
        count = len(self._teams)
        t_node = self._create_team_node(name, leader)
        x, y = 100 + count * 280, 180
        if self._foreman:
            x = self._foreman.x() - (count * 140) + count * 280
            y = self._foreman.y() + 140
        t_node.setPos(x, y)
        self._team_parent[t_node] = self._foreman
        self._rebuild_connections()
        self.hierarchy_changed.emit()
        return t_node

    def remove_team(self, leader: str) -> None:
        to_remove = None
        for t in self._teams:
            if t.leader_name == leader:
                to_remove = t
                break
        if not to_remove:
            return
        # Move child teams to foreman
        for t in list(self._teams):
            if self._team_parent.get(t) is to_remove:
                self._team_parent[t] = self._foreman
        # Remove ships under this team
        for s in self._team_ships.get(to_remove, []):
            self._remove_ship_badges(s)
            self.removeItem(s)
            if s in self._ships:
                self._ships.remove(s)
        self._team_ships.pop(to_remove, None)
        self._team_parent.pop(to_remove, None)
        self._teams.remove(to_remove)
        self.removeItem(to_remove)
        self._rebuild_connections()
        self.hierarchy_changed.emit()

    def set_team_parent(self, team: TeamNode, parent: ForemanNode | TeamNode) -> None:
        """Reparent a team under another team or the foreman."""
        if parent is team:
            return
        # Prevent circular: walk up from parent, ensure we don't hit team
        node = parent
        while isinstance(node, TeamNode):
            if node is team:
                return  # would create a cycle
            node = self._team_parent.get(node)
        self._team_parent[team] = parent
        self._rebuild_connections()
        self.hierarchy_changed.emit()

    # ------------------------------------------------------------------
    # Ship operations
    # ------------------------------------------------------------------

    def assign_ship_to_team(self, team_node: ForemanNode | TeamNode, ship_info: dict) -> ShipNode:
        s_node = self._create_ship_node(
            ship_info.get("ship_name", "Ship"),
            ship_info.get("ship_type", "Prospector"),
            ship_info.get("loadout_path", ""),
            model_crew=ship_info.get("model_crew", 0),
        )
        idx = len(self._team_ships.get(team_node, []))
        s_node.setPos(team_node.x(), team_node.y() + 80 + idx * 70)
        self._team_ships.setdefault(team_node, []).append(s_node)
        self._rebuild_connections()
        self.hierarchy_changed.emit()
        return s_node

    def place_ship_at(self, ship_info: dict, x: float, y: float) -> ShipNode:
        s_node = self._create_ship_node(
            ship_info.get("ship_name", "Ship"),
            ship_info.get("ship_type", "Prospector"),
            ship_info.get("loadout_path", ""),
            model_crew=ship_info.get("model_crew", 0),
        )
        s_node.setPos(x, y)
        self._unassigned_ships.append(s_node)
        self.hierarchy_changed.emit()
        return s_node

    def snap_ship_to_team(self, ship_node: ShipNode, team_node: ForemanNode | TeamNode) -> None:
        self._detach_ship_from_team(ship_node)
        self._team_ships.setdefault(team_node, []).append(ship_node)
        idx = self._team_ships[team_node].index(ship_node)
        ship_node.setPos(team_node.x(), team_node.y() + 80 + idx * 70)
        self._rebuild_connections()
        self.hierarchy_changed.emit()

    def find_nearest_team(self, pos: QPointF, max_distance: float = 150.0) -> ForemanNode | TeamNode | None:
        """Find the closest team/foreman node within max_distance."""
        best = None
        best_dist = max_distance
        # Include foreman
        all_targets: list[ForemanNode | TeamNode] = []
        if self._foreman:
            all_targets.append(self._foreman)
        all_targets.extend(self._teams)
        for t in all_targets:
            center = QPointF(t.x() + t._w / 2, t.y() + t._h / 2)
            dx, dy = pos.x() - center.x(), pos.y() - center.y()
            dist = (dx * dx + dy * dy) ** 0.5
            if dist < best_dist:
                best_dist = dist
                best = t
        return best

    def get_ship_team(self, ship_node: ShipNode) -> ForemanNode | TeamNode | None:
        for t, ships in self._team_ships.items():
            if ship_node in ships:
                return t
        return None

    def remove_ship(self, ship_node: ShipNode) -> None:
        self._remove_ship_badges(ship_node)
        # If this is a mothership, remove all its strike groups
        if ship_node.ship_type == "Mothership":
            groups = list(self._mothership_groups.get(ship_node.unique_id, []))
            for g in groups:
                self.remove_strike_group(g)
        for t, ships in self._team_ships.items():
            if ship_node in ships:
                ships.remove(ship_node)
                break
        if ship_node in self._unassigned_ships:
            self._unassigned_ships.remove(ship_node)
        if ship_node in self._ships:
            self._ships.remove(ship_node)
        self.removeItem(ship_node)
        self._rebuild_connections()
        self.hierarchy_changed.emit()

    # ------------------------------------------------------------------
    # Player operations — single assignment enforced
    # ------------------------------------------------------------------

    def assign_player_to_ship(self, ship_node: ShipNode, player_name: str) -> None:
        # Enforce single assignment: remove from any other ship first
        for s in self._ships:
            if s is not ship_node and player_name in s.crew:
                s.crew.remove(player_name)
                self._add_crew_badges(s)
                s.update()
        if player_name not in ship_node.crew:
            ship_node.crew.append(player_name)
            self._add_crew_badges(ship_node)
            ship_node.update()
            self.hierarchy_changed.emit()

    def find_ship_for_player(self, player_name: str) -> ShipNode | None:
        """Find the ship a player is assigned to, or None."""
        for s in self._ships:
            if player_name in s.crew:
                return s
        return None

    def find_team_for_player(self, player_name: str) -> ForemanNode | TeamNode | None:
        """Find the team that contains the ship a player is on."""
        ship = self.find_ship_for_player(player_name)
        if ship:
            return self.get_ship_team(ship)
        # Also check if the player is a team leader
        for t in self._teams:
            if t.leader_name == player_name:
                return t
        return None

    def get_team_descendants(self, team_or_foreman) -> list:
        """Return all nodes nested under a team/foreman: ships, strike groups,
        child teams (recursively), and their descendants."""
        result: list = []
        ships = list(self._team_ships.get(team_or_foreman, []))
        for s in ships:
            result.append(s)
            if s.ship_type == "Mothership":
                for g in self._mothership_groups.get(s.unique_id, []):
                    result.append(g)
        for child_team, parent in self._team_parent.items():
            if parent is team_or_foreman:
                result.append(child_team)
                result.extend(self.get_team_descendants(child_team))
        return result

    def get_strike_group_ships(self, group: "StrikeGroupNode") -> list:
        """Return all ships that belong to a specific strike group."""
        return [
            s for s in self._ships
            if s.mothership_id == group.mothership_id
            and s.strike_group == group.name_text
        ]

    def unassign_player_everywhere(self, player_name: str) -> None:
        changed = False
        for s in self._ships:
            if player_name in s.crew:
                s.crew.remove(player_name)
                self._add_crew_badges(s)
                s.update()
                changed = True
        if changed:
            self.hierarchy_changed.emit()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _create_team_node(self, name: str, leader: str) -> TeamNode:
        t = TeamNode(name, leader)
        self.addItem(t)
        self._teams.append(t)
        self._team_ships[t] = []
        t.position_changed.connect(self._on_node_moved)
        t.name_changed.connect(lambda _: self.hierarchy_changed.emit())
        t.ship_dropped.connect(lambda info, tn=t: self.assign_ship_to_team(tn, info))
        t.drag_released.connect(self._try_snap_team)
        t.cluster_changed.connect(lambda _: self.hierarchy_changed.emit())
        return t

    def _create_ship_node(
        self, name: str, ship_type: str, loadout_path: str = "",
        crew: list[str] | None = None, model_crew: int = 0,
        unique_id: str = "", mothership_id: str = "", strike_group: str = "",
    ) -> ShipNode:
        s = ShipNode(
            name, ship_type, loadout_path, crew, model_crew=model_crew,
            unique_id=unique_id, mothership_id=mothership_id, strike_group=strike_group,
        )
        self.addItem(s)
        self._ships.append(s)
        s.position_changed.connect(self._on_node_moved)
        s.player_dropped.connect(lambda pname, sn=s: self.assign_player_to_ship(sn, pname))
        s.request_delete.connect(self.remove_ship)
        s.request_snap.connect(self._on_ship_snap_request)
        s.drag_released.connect(self._try_snap_ship)
        s.ship_dropped_on_me.connect(lambda info, sn=s: self.assign_ship_to_mothership(sn, info))
        s.request_add_strike_group.connect(self.add_strike_group)
        return s

    def _create_strike_group_node(
        self, name: str, mothership_id: str, leader: str = "",
    ) -> StrikeGroupNode:
        g = StrikeGroupNode(name, mothership_id, leader)
        self.addItem(g)
        self._strike_groups.append(g)
        self._mothership_groups.setdefault(mothership_id, []).append(g)
        g.position_changed.connect(self._on_node_moved)
        g.name_changed.connect(lambda _: self.hierarchy_changed.emit())
        g.ship_dropped.connect(
            lambda info, gn=g: self.assign_ship_to_strike_group(gn, info)
        )
        g.request_delete.connect(self.remove_strike_group)
        return g

    def _find_mothership_by_id(self, unique_id: str) -> ShipNode | None:
        for s in self._ships:
            if s.unique_id == unique_id:
                return s
        return None

    def _find_strike_group(self, mothership_id: str, name: str) -> StrikeGroupNode | None:
        for g in self._strike_groups:
            if g.mothership_id == mothership_id and g.name_text == name:
                return g
        return None

    def add_strike_group(self, mothership: ShipNode, name: str | None = None) -> StrikeGroupNode:
        existing = self._mothership_groups.get(mothership.unique_id, [])
        if name is None:
            name = f"Strike Group {len(existing) + 1}"
        group = self._create_strike_group_node(name, mothership.unique_id)
        idx = len(existing)
        group.setPos(
            mothership.x() + idx * (_SG_W + 20),
            mothership.y() + mothership._h + 24,
        )
        self._rebuild_connections()
        self.hierarchy_changed.emit()
        return group

    def remove_strike_group(self, group: StrikeGroupNode) -> None:
        # Detach ships from this group (but leave them in their team)
        for s in self._ships:
            if s.mothership_id == group.mothership_id and s.strike_group == group.name_text:
                s.mothership_id = ""
                s.strike_group = ""
        if group in self._strike_groups:
            self._strike_groups.remove(group)
        groups = self._mothership_groups.get(group.mothership_id, [])
        if group in groups:
            groups.remove(group)
        self.removeItem(group)
        self._rebuild_connections()
        self.hierarchy_changed.emit()

    def assign_ship_to_mothership(self, mothership: ShipNode, ship_info: dict) -> ShipNode | None:
        """Drop a ship onto a mothership — adds to its first (or newest) strike group."""
        groups = self._mothership_groups.get(mothership.unique_id, [])
        if not groups:
            group = self.add_strike_group(mothership)
        else:
            group = groups[-1]
        return self.assign_ship_to_strike_group(group, ship_info)

    def assign_ship_to_strike_group(
        self, group: StrikeGroupNode, ship_info: dict,
    ) -> ShipNode | None:
        mothership = self._find_mothership_by_id(group.mothership_id)
        if mothership is None:
            return None
        # Figure out which team the mothership belongs to — put new ship there
        owner = self.get_ship_team(mothership)
        if owner is None:
            owner = self._foreman

        s_node = self._create_ship_node(
            ship_info.get("ship_name", "Ship"),
            ship_info.get("ship_type", "Prospector"),
            ship_info.get("loadout_path", ""),
            model_crew=ship_info.get("model_crew", 0),
            mothership_id=group.mothership_id,
            strike_group=group.name_text,
        )
        self._team_ships.setdefault(owner, []).append(s_node)

        # Snap into a column below the strike group
        existing_in_group = [
            s for s in self._ships
            if s is not s_node
            and s.mothership_id == group.mothership_id
            and s.strike_group == group.name_text
        ]
        col_idx = len(existing_in_group)
        s_node.setPos(
            group.x(),
            group.y() + group._h + 14 + col_idx * (_SHIP_H + 10),
        )
        self._rebuild_connections()
        self.hierarchy_changed.emit()
        return s_node

    def set_strike_group_leader(self, mothership_id: str, group_name: str, player_name: str) -> None:
        group = self._find_strike_group(mothership_id, group_name)
        if group is not None:
            group.leader = player_name
            group.update()
            self.hierarchy_changed.emit()

    def set_profession_lookup(self, lookup: dict[str, str]) -> None:
        """Update the {player_name: profession_icon} map and refresh badges."""
        self._profession_lookup = dict(lookup)
        for badge in self._badges:
            icon = self._profession_lookup.get(badge.player_name, "")
            badge.profession_icon = icon
            badge.update()

    def _add_crew_badges(self, ship_node: ShipNode) -> None:
        self._remove_ship_badges(ship_node)
        accent_hex = SHIP_TYPE_COLORS.get(ship_node.ship_type, P.fg_dim)
        for i, name in enumerate(ship_node.crew):
            icon = self._profession_lookup.get(name, "")
            badge = PlayerBadge(name, accent=accent_hex, profession_icon=icon)
            self.addItem(badge)
            badge.setParentItem(None)
            self._badges.append(badge)
            badge.setData(0, id(ship_node))

            # Connector line from ship's right edge to badge's left edge
            line = QGraphicsLineItem()
            pen = QPen(QColor(accent_hex), 1.4)
            pen.setCapStyle(Qt.RoundCap)
            line.setPen(pen)
            line.setZValue(-0.5)
            self.addItem(line)
            line.setData(0, id(ship_node))
            self._badge_connectors.append(line)
        # Position everything with collision avoidance
        self._relayout_all_badges()

    def _relayout_all_badges(self) -> None:
        """Place all badges with collision avoidance — push down if overlapping."""
        placed: list[tuple[float, float, float, float]] = []
        # Process ships top-to-bottom so higher ships get priority
        sorted_ships = sorted(self._ships, key=lambda s: (s.y(), s.x()))
        for ship in sorted_ships:
            ship_id = id(ship)
            badges = [b for b in self._badges if b.data(0) == ship_id]
            lines = [c for c in self._badge_connectors if c.data(0) == ship_id]
            base_x = ship.x() + ship._w + 14
            for i, badge in enumerate(badges):
                target_y = ship.y() + 4 + i * 26
                # Iteratively push down until no collision with already-placed badges
                while True:
                    collided = False
                    for (px, py, pw, ph) in placed:
                        if (base_x < px + pw and px < base_x + badge._w
                                and target_y < py + ph and py < target_y + badge._h):
                            target_y = py + ph + 4
                            collided = True
                            break
                    if not collided:
                        break
                badge.setPos(base_x, target_y)
                placed.append((base_x, target_y, badge._w, badge._h))
            # Update connector lines to their (possibly pushed-down) badges
            for line, badge in zip(lines, badges):
                self._update_badge_connector(line, ship, badge)

    def _update_badge_connector(
        self, line: QGraphicsLineItem, ship_node: ShipNode, badge: PlayerBadge,
    ) -> None:
        x1 = ship_node.x() + ship_node._w
        y1 = ship_node.y() + ship_node._h / 2
        x2 = badge.x()
        y2 = badge.y() + badge._h / 2
        line.setLine(QLineF(x1, y1, x2, y2))

    def _remove_ship_badges(self, ship_node: ShipNode) -> None:
        ship_id = id(ship_node)
        to_remove = [b for b in self._badges if b.data(0) == ship_id]
        for b in to_remove:
            self._badges.remove(b)
            self.removeItem(b)
        # Also remove connector lines tagged with this ship
        lines_to_remove = [c for c in self._badge_connectors if c.data(0) == ship_id]
        for c in lines_to_remove:
            self._badge_connectors.remove(c)
            self.removeItem(c)
        # Reclaim space for remaining badges
        if self._badges:
            self._relayout_all_badges()

    def _rebuild_connections(self) -> None:
        for c in self._connections:
            self.removeItem(c)
        self._connections.clear()

        if not self._foreman:
            return

        # Team parent → team connections
        for t in self._teams:
            parent = self._team_parent.get(t, self._foreman)
            if parent:
                conn = ConnectionLine(parent, t)
                self.addItem(conn)
                self._connections.append(conn)

        # Team/Foreman → ship connections (skip ships that are in strike groups)
        for owner, ships in self._team_ships.items():
            for s in ships:
                if s.strike_group:
                    continue
                conn = ConnectionLine(owner, s)
                self.addItem(conn)
                self._connections.append(conn)

        # Mothership → strike group
        for mother_id, groups in self._mothership_groups.items():
            mothership = self._find_mothership_by_id(mother_id)
            if mothership is None:
                continue
            for g in groups:
                conn = ConnectionLine(mothership, g)
                self.addItem(conn)
                self._connections.append(conn)

        # Strike group → ship
        for s in self._ships:
            if not s.strike_group or not s.mothership_id:
                continue
            g = self._find_strike_group(s.mothership_id, s.strike_group)
            if g is not None:
                conn = ConnectionLine(g, s)
                self.addItem(conn)
                self._connections.append(conn)

    def _on_ship_snap_request(self, ship_node: ShipNode, team_node) -> None:
        if team_node is None:
            self._detach_ship_from_team(ship_node)
            self._unassigned_ships.append(ship_node)
            self._rebuild_connections()
            self.hierarchy_changed.emit()
        else:
            self.snap_ship_to_team(ship_node, team_node)

    def _detach_ship_from_team(self, ship_node: ShipNode) -> None:
        for t, ships in self._team_ships.items():
            if ship_node in ships:
                ships.remove(ship_node)
                return
        if ship_node in self._unassigned_ships:
            self._unassigned_ships.remove(ship_node)

    def _try_snap_ship(self, ship_node: ShipNode) -> bool:
        pos = QPointF(ship_node.x() + ship_node._w / 2, ship_node.y())
        nearest = self.find_nearest_team(pos, max_distance=120.0)
        if nearest is None:
            return False
        current = self.get_ship_team(ship_node)
        if nearest is current:
            return False
        self._detach_ship_from_team(ship_node)
        self._team_ships.setdefault(nearest, []).append(ship_node)
        idx = self._team_ships[nearest].index(ship_node)
        ship_node.setPos(nearest.x(), nearest.y() + 80 + idx * 70)
        self._rebuild_connections()
        self.hierarchy_changed.emit()
        return True

    def _try_snap_team(self, team_node: TeamNode) -> None:
        """When a team is released after drag, snap to nearest parent team/foreman."""
        pos = QPointF(team_node.x() + team_node._w / 2, team_node.y())
        best = None
        best_dist = 200.0
        # Check foreman
        if self._foreman:
            c = QPointF(self._foreman.x() + self._foreman._w / 2,
                        self._foreman.y() + self._foreman._h / 2)
            d = ((pos.x() - c.x())**2 + (pos.y() - c.y())**2) ** 0.5
            if d < best_dist:
                best_dist = d
                best = self._foreman
        # Check other teams
        for t in self._teams:
            if t is team_node:
                continue
            c = QPointF(t.x() + t._w / 2, t.y() + t._h / 2)
            d = ((pos.x() - c.x())**2 + (pos.y() - c.y())**2) ** 0.5
            if d < best_dist:
                best_dist = d
                best = t
        if best and best is not self._team_parent.get(team_node):
            self.set_team_parent(team_node, best)

    def _on_node_moved(self) -> None:
        self._relayout_all_badges()
        for c in self._connections:
            c.update_path()
        self.hierarchy_changed.emit()

    # ------------------------------------------------------------------
    # Cluster queries & filtering
    # ------------------------------------------------------------------

    def teams_in_cluster(self, cluster: str) -> list[TeamNode]:
        return sorted(
            [t for t in self._teams if t.cluster == cluster],
            key=lambda t: t.team_name,
        )

    def all_clusters(self) -> list[str]:
        return sorted({t.cluster for t in self._teams if t.cluster})

    def ships_in_team(self, team_node) -> list[ShipNode]:
        return list(self._team_ships.get(team_node, []))

    def cluster_for_team(self, team_node) -> str:
        return getattr(team_node, "cluster", "")

    def set_cluster_visibility(self, visible_clusters: set[str]) -> None:
        for t in self._teams:
            visible = not t.cluster or t.cluster in visible_clusters
            opacity = 1.0 if visible else 0.3
            t.setOpacity(opacity)
            for s in self._team_ships.get(t, []):
                s.setOpacity(opacity)
                ship_id = id(s)
                for b in self._badges:
                    if b.data(0) == ship_id:
                        b.setOpacity(opacity)
                for c in self._badge_connectors:
                    if c.data(0) == ship_id:
                        c.setOpacity(opacity)
        if self._foreman:
            self._foreman.setOpacity(1.0)
            for s in self._team_ships.get(self._foreman, []):
                s.setOpacity(1.0)


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------

class LedgerView(QGraphicsView):
    """Viewport for the ledger hierarchy with grid background and drop support."""

    def __init__(self, scene: LedgerScene, parent: QWidget = None) -> None:
        super().__init__(scene, parent)
        self.setRenderHints(
            QPainter.Antialiasing | QPainter.SmoothPixmapTransform
        )
        self.setDragMode(QGraphicsView.NoDrag)
        self.setAcceptDrops(True)
        self.setStyleSheet(f"background: {P.bg_deepest}; border: none;")
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self._panning = False
        self._pan_start = QPointF()
        self._left_panning = False  # left-click drag on empty canvas

    def center_on_content(self) -> None:
        scene: LedgerScene = self.scene()
        if scene._foreman:
            self.centerOn(scene._foreman)
        else:
            items_rect = scene.itemsBoundingRect()
            if not items_rect.isEmpty():
                self.fitInView(items_rect.adjusted(-50, -50, 50, 50), Qt.KeepAspectRatio)

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(P.bg_deepest))
        pen = QPen(QColor(P.border), 0.5)
        painter.setPen(pen)
        left = int(rect.left()) - (int(rect.left()) % _GRID_SPACING)
        top = int(rect.top()) - (int(rect.top()) % _GRID_SPACING)
        x = left
        while x < rect.right():
            painter.drawLine(x, int(rect.top()), x, int(rect.bottom()))
            x += _GRID_SPACING
        y = top
        while y < rect.bottom():
            painter.drawLine(int(rect.left()), y, int(rect.right()), y)
            y += _GRID_SPACING

    def wheelEvent(self, event: QWheelEvent) -> None:
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MiddleButton:
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.ClosedHandCursor)
            return
        if event.button() == Qt.LeftButton:
            # Check if clicking on empty canvas (no item under cursor)
            scene_pos = self.mapToScene(event.position().toPoint())
            item = self.scene().itemAt(scene_pos, self.transform())
            if item is None:
                self._left_panning = True
                self._pan_start = event.position()
                self.setCursor(Qt.ClosedHandCursor)
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._panning or self._left_panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MiddleButton and self._panning:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            return
        if event.button() == Qt.LeftButton and self._left_panning:
            self._left_panning = False
            self.setCursor(Qt.ArrowCursor)
            return
        super().mouseReleaseEvent(event)

    # -- Drop from side panels --

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if (event.mimeData().hasFormat("application/x-sc-ledger-ship")
                or event.mimeData().hasFormat("application/x-sc-ledger-player")
                or event.mimeData().hasFormat("application/x-sc-ledger-profession")):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        event.setDropAction(Qt.CopyAction)
        event.accept()

    def dropEvent(self, event: QDropEvent) -> None:
        scene: LedgerScene = self.scene()
        pos = self.mapToScene(event.position().toPoint())

        hit_rect = QRectF(pos.x() - 10, pos.y() - 10, 20, 20)
        all_at = scene.items(hit_rect)
        target_strike_group = self._find_type_in_list(all_at, (StrikeGroupNode,))
        target_team = self._find_type_in_list(all_at, (TeamNode, ForemanNode))
        target_ship = self._find_type_in_list(all_at, (ShipNode,))
        target_badge = self._find_type_in_list(all_at, (PlayerBadge,))

        if event.mimeData().hasFormat("application/x-sc-ledger-ship"):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-ship")).decode())
            if target_strike_group:
                scene.assign_ship_to_strike_group(target_strike_group, data)
            elif target_ship and target_ship.ship_type == "Mothership":
                scene.assign_ship_to_mothership(target_ship, data)
            elif target_team:
                scene.assign_ship_to_team(target_team, data)
            else:
                scene.place_ship_at(data, pos.x(), pos.y())
            event.acceptProposedAction()

        elif event.mimeData().hasFormat("application/x-sc-ledger-player"):
            data = json.loads(bytes(event.mimeData().data("application/x-sc-ledger-player")).decode())
            name = data.get("name", "")
            if target_ship and name:
                scene.assign_player_to_ship(target_ship, name)
            event.acceptProposedAction()

        elif event.mimeData().hasFormat("application/x-sc-ledger-profession"):
            try:
                data = json.loads(
                    bytes(event.mimeData().data("application/x-sc-ledger-profession")).decode()
                )
            except (ValueError, UnicodeDecodeError):
                event.ignore()
                return
            profession = data.get("profession", "")
            # Prefer dropping on a badge (a specific crew member)
            if target_badge:
                scene.profession_assigned.emit(target_badge.player_name, profession)
                event.acceptProposedAction()
                return
            # Otherwise, if dropped on a ship node, assign to its first crew member
            if target_ship and target_ship.crew:
                scene.profession_assigned.emit(target_ship.crew[0], profession)
                event.acceptProposedAction()
                return
            event.ignore()
        else:
            event.ignore()

    @staticmethod
    def _find_type_in_list(items, classes):
        for item in items:
            if isinstance(item, classes):
                return item
        return None
