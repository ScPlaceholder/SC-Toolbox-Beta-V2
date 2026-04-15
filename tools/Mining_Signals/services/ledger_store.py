"""
Mining Ledger data model and persistence.

Stores the fleet hierarchy: foreman → teams → ships → crew assignments.
Teams can be nested under other teams.
Persisted as ``mining_ledger.json`` alongside the Mining Signals config.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PlayerEntry:
    name: str
    is_leader: bool = False
    profession: str = ""   # profession name (see services.professions)


@dataclass
class FleetSupportShip:
    name: str
    support_type: str       # Hauling | Repair | Refuel | Escort | Mothership | Medical
    ship_model: str = ""    # e.g. "Caterpillar", "Carrack" — from UEX vehicle DB
    model_crew: int = 2     # max crew from the ship model lookup


@dataclass
class CrewAssignment:
    loadout_path: str          # path to loadout JSON (empty for support ships)
    ship_name: str             # user-facing display name
    ship_type: str             # Prospector | MOLE | Golem | Hauling | Repair | Refuel
    crew: list[str] = field(default_factory=list)
    _pos: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    model_crew: int = 0        # max crew from ship DB; 0 = use heuristic
    unique_id: str = ""        # stable id for mothership references
    mothership_id: str = ""    # unique_id of parent mothership if in a strike group
    strike_group: str = ""     # strike group name ("" = not in a strike group)


@dataclass
class StrikeGroupData:
    """A named group of ships attached to a mothership."""
    name: str                      # e.g. "Strike Group 1"
    mothership_id: str             # unique_id of parent mothership
    leader: str = ""               # player name of strike group leader
    _pos: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})


@dataclass
class MiningTeam:
    name: str
    leader: str
    ships: list[CrewAssignment] = field(default_factory=list)
    _pos: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    parent_leader: str = ""    # empty = child of foreman
    cluster: str = ""          # single letter A-Z, or "" for unassigned


@dataclass
class LedgerData:
    foreman_name: str = "Foreman"
    foreman_pos: dict[str, float] = field(default_factory=lambda: {"x": 0.0, "y": 0.0})
    foreman_ships: list[CrewAssignment] = field(default_factory=list)
    teams: list[MiningTeam] = field(default_factory=list)
    players: list[PlayerEntry] = field(default_factory=list)
    fleet_support_ships: list[FleetSupportShip] = field(default_factory=list)
    unassigned_ships: list[CrewAssignment] = field(default_factory=list)
    assigned_user: str = ""    # the "you are here" player name
    strike_groups: list[StrikeGroupData] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def _crew_to_dict(c: CrewAssignment) -> dict[str, Any]:
    return {
        "loadout_path": c.loadout_path,
        "ship_name": c.ship_name,
        "ship_type": c.ship_type,
        "crew": list(c.crew),
        "_pos": dict(c._pos),
        "model_crew": c.model_crew,
        "unique_id": c.unique_id,
        "mothership_id": c.mothership_id,
        "strike_group": c.strike_group,
    }


def _crew_from_dict(raw: dict[str, Any]) -> CrewAssignment:
    return CrewAssignment(
        loadout_path=raw.get("loadout_path", ""),
        ship_name=raw.get("ship_name", "Unknown"),
        ship_type=raw.get("ship_type", "Prospector"),
        crew=list(raw.get("crew", [])),
        _pos=dict(raw.get("_pos", {"x": 0.0, "y": 0.0})),
        model_crew=raw.get("model_crew", 0),
        unique_id=raw.get("unique_id", ""),
        mothership_id=raw.get("mothership_id", ""),
        strike_group=raw.get("strike_group", ""),
    )


def ledger_to_dict(data: LedgerData) -> dict[str, Any]:
    return {
        "foreman_name": data.foreman_name,
        "foreman_pos": dict(data.foreman_pos),
        "foreman_ships": [_crew_to_dict(s) for s in data.foreman_ships],
        "teams": [
            {
                "name": t.name,
                "leader": t.leader,
                "ships": [_crew_to_dict(s) for s in t.ships],
                "_pos": dict(t._pos),
                "parent_leader": t.parent_leader,
                "cluster": t.cluster,
            }
            for t in data.teams
        ],
        "players": [
            {
                "name": p.name,
                "is_leader": p.is_leader,
                "profession": p.profession,
            }
            for p in data.players
        ],
        "fleet_support_ships": [
            {
                "name": s.name,
                "support_type": s.support_type,
                "ship_model": s.ship_model,
                "model_crew": s.model_crew,
            }
            for s in data.fleet_support_ships
        ],
        "unassigned_ships": [_crew_to_dict(s) for s in data.unassigned_ships],
        "assigned_user": data.assigned_user,
        "strike_groups": [
            {
                "name": sg.name,
                "mothership_id": sg.mothership_id,
                "leader": sg.leader,
                "_pos": dict(sg._pos),
            }
            for sg in data.strike_groups
        ],
    }


def ledger_from_dict(raw: dict[str, Any]) -> LedgerData:
    teams: list[MiningTeam] = []
    for t in raw.get("teams", []):
        teams.append(MiningTeam(
            name=t.get("name", "Team"),
            leader=t.get("leader", ""),
            ships=[_crew_from_dict(s) for s in t.get("ships", [])],
            _pos=dict(t.get("_pos", {"x": 0.0, "y": 0.0})),
            parent_leader=t.get("parent_leader", ""),
            cluster=t.get("cluster", ""),
        ))

    players = [
        PlayerEntry(
            name=p.get("name", ""),
            is_leader=p.get("is_leader", False),
            profession=p.get("profession", ""),
        )
        for p in raw.get("players", [])
    ]

    support = [
        FleetSupportShip(
            name=s.get("name", ""),
            support_type=s.get("support_type", "Hauling"),
            ship_model=s.get("ship_model", ""),
            model_crew=s.get("model_crew", 2),
        )
        for s in raw.get("fleet_support_ships", [])
    ]

    unassigned = [
        _crew_from_dict(s) for s in raw.get("unassigned_ships", [])
    ]

    foreman_ships = [
        _crew_from_dict(s) for s in raw.get("foreman_ships", [])
    ]

    strike_groups = [
        StrikeGroupData(
            name=sg.get("name", "Strike Group"),
            mothership_id=sg.get("mothership_id", ""),
            leader=sg.get("leader", ""),
            _pos=dict(sg.get("_pos", {"x": 0.0, "y": 0.0})),
        )
        for sg in raw.get("strike_groups", [])
    ]

    return LedgerData(
        foreman_name=raw.get("foreman_name", "Foreman"),
        foreman_pos=dict(raw.get("foreman_pos", {"x": 0.0, "y": 0.0})),
        foreman_ships=foreman_ships,
        teams=teams,
        players=players,
        fleet_support_ships=support,
        unassigned_ships=unassigned,
        assigned_user=raw.get("assigned_user", ""),
        strike_groups=strike_groups,
    )


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def save_ledger(data: LedgerData, path: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(ledger_to_dict(data), f, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Failed to save ledger: %s", exc)


def load_ledger(path: str) -> LedgerData:
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if isinstance(raw, dict):
                return ledger_from_dict(raw)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to load ledger: %s", exc)
    return LedgerData()


# ---------------------------------------------------------------------------
# Player roster import / export
# ---------------------------------------------------------------------------

def export_player_roster(players: list[PlayerEntry], path: str) -> None:
    payload = [
        {
            "name": p.name,
            "is_leader": p.is_leader,
            "profession": p.profession,
        }
        for p in players
    ]
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp, path)
    except OSError as exc:
        log.warning("Failed to export roster: %s", exc)


def import_player_roster(path: str) -> list[PlayerEntry]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            return [
                PlayerEntry(
                    name=p.get("name", ""),
                    is_leader=p.get("is_leader", False),
                    profession=p.get("profession", ""),
                )
                for p in raw if isinstance(p, dict) and p.get("name")
            ]
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to import roster: %s", exc)
    return []
