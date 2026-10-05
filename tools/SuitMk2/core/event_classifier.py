"""
SuitMk2 - Event Classifier

Categorizes parsed events into high-level gameplay event types and
updates the central state store accordingly.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

import importlib.util
import os

from event_parser import LogEvent, _is_planetary_body, _get_body_info
from state_store import StateStore

# Path-based import to avoid collision with sc_log_reader's location_names
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    _spec = importlib.util.spec_from_file_location(
        "suitMk2_location_names",
        os.path.join(_HERE, "location_names.py"),
    )
    _loc_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_loc_mod)
    get_location_type = _loc_mod.get_location_type
    get_location_body = _loc_mod.get_location_body
    location_is_named = _loc_mod.is_named
except Exception:
    logging.getLogger(__name__).warning("Failed to load location_names.py; using fallback stub.")
    def get_location_type(raw: str) -> str:
        return "unknown"

    def get_location_body(raw: str) -> str:
        return ""

    def location_is_named(raw: str) -> bool:
        return True

logger = logging.getLogger(__name__)


# High-level event categories
LOCATION_EVENT = "LOCATION"
ACTOR_STATE_EVENT = "ACTOR_STATE"
SHIP_EVENT = "SHIP"
COMBAT_EVENT = "COMBAT"
EQUIPMENT_EVENT = "EQUIPMENT"
NPC_EVENT = "NPC"
CARGO_EVENT = "CARGO"
FUEL_EVENT = "FUEL"
QUANTUM_EVENT = "QUANTUM"
DEATH_EVENT = "DEATH"
MISSION_EVENT = "MISSION"
ECONOMY_EVENT = "ECONOMY"
SOCIAL_EVENT = "SOCIAL"
SESSION_EVENT = "SESSION"
ZONE_EVENT = "ZONE"


# Location flavor — classifies environment feel for dialogue
# "rough"   = derelict, pirate, lawless, damaged infrastructure
# "urban"   = major landing zone / city
# "orbital" = clean orbital station or Lagrange rest stop
# "wild"    = moon or planet surface (wilderness, no atmosphere, harsh)
# "transit" = jump point or quantum travel
# "unknown" = fallback
_ROUGH_LOCATIONS = {
    "grim hex", "grimhex", "ruin station", "levski",
}
_URBAN_LOCATIONS = {
    "lorville", "area 18", "orison", "new babbage",
}


def _classify_location_flavor(name: str, raw: str) -> str:
    """Determine environmental flavor of a location for dialogue tone."""
    name_lower = name.lower()
    if name_lower in _ROUGH_LOCATIONS:
        return "rough"
    if name_lower in _URBAN_LOCATIONS:
        return "urban"
    loc_type = get_location_type(raw)
    if loc_type == "jump_point":
        return "transit"
    if loc_type == "city":
        return "urban"
    if loc_type in ("moon", "planet"):
        return "wild"
    if loc_type == "outpost":       # what an outpost was before it had a type of its own: Pyro rough, the rest wild
        return "rough" if raw.upper().startswith("PYRO") else "wild"
    if loc_type == "station":
        return "orbital"
    # Pyro system is generally rough
    if raw.upper().startswith("PYRO") or raw.upper().startswith("RR_PYRO"):
        return "rough"
    return "unknown"


class EventClassifier:
    """
    Processes LogEvents, updates the state store, and emits classified events.

    Acts as the bridge between raw log parsing and the analytical modules.
    """

    def __init__(self, state_store: StateStore, volatile_context: Any = None) -> None:
        self._state = state_store
        self._subscribers: list[Callable[[LogEvent], None]] = []
        self._last_arrived_location: str | None = None
        self._volatile = volatile_context  # VolatileContext for location stickiness

    def subscribe(self, callback: Callable[[LogEvent], None]) -> None:
        """Subscribe to classified events."""
        self._subscribers.append(callback)

    def on_event(self, event: LogEvent) -> None:
        """Process a parsed event: update state and classify."""
        self._update_state(event)
        self._emit(event)

    def _update_state(self, event: LogEvent) -> None:
        """Update the state store based on the event."""
        data = event.data
        et = event.event_type

        if et == "session_start":
            if "player_name" in data:
                self._state.set("player_name", data["player_name"])
            if "player_geid" in data:
                self._state.set("player_geid", data["player_geid"])
            self._state.set("session_deaths", 0)
            self._state.set("session_contracts_completed", 0)

        elif et == "join_pu":
            if "shard" in data:
                self._state.set("server", data["shard"])

        elif et == "location_change":
            raw = data.get("location_raw", "")
            self._state.set("location_raw", raw)
            loc_name = data.get("location_name")
            self._state.set("star_system", data.get("star_system"))

            # Classify location type and environmental flavor
            loc_type = get_location_type(raw)
            loc_flavor = _classify_location_flavor(loc_name or "", raw)
            self._state.set("location_type", loc_type)
            self._state.set("location_flavor", loc_flavor)

            if loc_name and loc_name != "INVALID LOCATION ID":
                # Use VolatileContext for stickiness if available
                if self._volatile:
                    disposition = self._volatile.update_location(
                        loc_name, loc_type, loc_flavor, raw,
                    )
                    event.data["location_disposition"] = disposition
                    if disposition not in ("same", "zone_change"):
                        self._state.set("location_name", loc_name)
                        self._last_arrived_location = loc_name
                        self._set_place_facts(raw)
                else:
                    # Fallback: simple dedup by name
                    if loc_name != self._last_arrived_location:
                        self._last_arrived_location = loc_name
                        self._state.set("location_name", loc_name)
                        self._set_place_facts(raw)

        elif et == "channel_change":
            if data.get("is_ship"):
                if data.get("action") == "joined":
                    ship_type = data.get("channel", "")
                    self._state.set("ship", ship_type)
                    # Enrich with location context so the LLM knows WHERE
                    # the player is boarding and can comment on it
                    event.event_type = "ship_channel_joined"
                    event.data = {
                        **event.data,
                        "ship_type": ship_type,
                        "location": self._state.get("location_name", ""),
                        "location_type": self._state.get("location_type", ""),
                        "star_system": self._state.get("star_system", ""),
                        "jurisdiction": self._state.get("jurisdiction", ""),
                    }
                elif data.get("action") == "left":
                    # Enrich BEFORE clearing ship state
                    event.data["ship_type"] = self._state.get("ship", "")
                    event.data["location"] = self._state.get("location_name", "")
                    event.data["location_type"] = self._state.get("location_type", "")
                    event.data["star_system"] = self._state.get("star_system", "")
                    self._state.set("ship", None)
                    event.event_type = "ship_channel_left"

        elif et in ("qt_arrived", "qt_error"):
            # Enrich quantum events with ship + location context
            event.data["ship"] = self._state.get("ship", "")
            event.data["location"] = self._state.get("location_name", "")
            event.data["star_system"] = self._state.get("star_system", "")

        elif et == "qt_route_calculated":
            # Enrich with ship context
            event.data["ship"] = self._state.get("ship", "")
            event.data["star_system"] = self._state.get("star_system", "")

            # Detect planetary body change from "Projected Start Location"
            projected = data.get("projected_body", "")
            if projected and _is_planetary_body(projected):
                body_info = _get_body_info(projected)
                if body_info and self._volatile:
                    friendly_name, body_type, parent = body_info
                    disposition = self._volatile.update_planetary_body(friendly_name)
                    event.data["body_disposition"] = disposition

                    if disposition in ("new_body", "returning"):
                        # Emit a synthetic planetary_body_entered event
                        self._state.set("planetary_body", friendly_name)
                        self._state.set("planetary_body_type", body_type)
                        self._state.set("planetary_body_parent", parent)

                        body_event = LogEvent(
                            event_type="planetary_body_entered",
                            timestamp=event.timestamp,
                            raw_line=event.raw_line,
                            category="SHIP",
                            data={
                                "body_name": friendly_name,
                                "body_type": body_type,
                                "parent_body": parent,
                                "disposition": disposition,
                                "ship": self._state.get("ship", ""),
                                "star_system": self._state.get("star_system", ""),
                            },
                        )
                        self._emit(body_event)
                elif not body_info:
                    # System-level location (e.g., "Stanton") — update state
                    # but don't emit a body_entered event
                    self._state.set("planetary_body", None)

        elif et == "qt_target_selected":
            event.data["ship"] = self._state.get("ship", "")
            event.data["star_system"] = self._state.get("star_system", "")

        elif et in ("hangar_ready", "hangar_queue", "docking_ready", "docking_detached"):
            # Enrich with ship + location context for dialogue
            event.data["ship"] = self._state.get("ship", "")
            event.data["location"] = self._state.get("location_name", "")
            event.data["location_type"] = self._state.get("location_type", "")
            event.data["star_system"] = self._state.get("star_system", "")

        elif et == "jurisdiction_change":
            jurisdiction = data.get("jurisdiction")
            if jurisdiction:
                self._state.set("jurisdiction", jurisdiction)

        elif et in ("entered_monitored_space", "exited_monitored_space",
                     "monitored_space_down", "monitored_space_restored"):
            self._state.set("in_monitored_space", data.get("monitored", False))

        elif et == "armistice_zone":
            action = data.get("action")
            self._state.set("in_armistice", action == "entered")

        elif et == "restricted_area":
            self._state.set("in_restricted_area", data.get("action") == "entered")

        elif et == "injury":
            part = data.get("body_part", "").lower().replace(" ", "_")
            severity = data.get("severity")
            if part and severity:
                self._state.set(f"injury_{part}", severity)

        elif et == "med_bed_heal":
            for part in data.get("healed_parts", {}):
                self._state.set(f"injury_{part}", None)
            # Enrich with ship context — if aboard a ship, the med bed
            # is the ship's med bay (e.g., Cutlass Red, Carrack, 890J)
            current_ship = self._state.get("ship")
            if current_ship:
                event.data["on_ship"] = True
                event.data["ship_type"] = current_ship
            current_location = self._state.get("location_name")
            if current_location:
                event.data["location"] = current_location

        elif et == "contract_accepted":
            self._state.set("last_contract_accepted", data.get("mission_name"))

        elif et == "objective_new":
            # 2026-09-24: kept so the pilot can ask "what's my objective" (conversation.py, topic "mission").
            self._state.set("last_objective", data.get("objective"))
        elif et == "contract_complete":
            self._state.set("last_contract_completed", data.get("mission_name"))
            count = self._state.get("session_contracts_completed", 0) or 0
            self._state.set("session_contracts_completed", count + 1)

        elif et == "contract_failed":
            self._state.set("last_contract_failed", data.get("mission_name"))

        elif et == "reward_earned":
            amount = data.get("amount", 0)
            current = self._state.get("session_earnings", 0)
            self._state.set("session_earnings", current + amount)

        # Combat signal extraction — track weapon equips and incapacitation
        elif et == "attachment_received":
            item_class = data.get("item_class", "")
            port = data.get("port", "")
            # Weapon in hand = potential combat
            if port == "weapon_attach_hand_right" and item_class:
                self._state.set("equipped_weapon", item_class)
                # BDL port classification — med pen detection
                if "consumable_healing" in item_class or "medgun" in item_class:
                    self._state.set("last_healing_item", item_class)

        elif et == "incapacitated":
            deaths = self._state.get("session_deaths", 0) or 0
            self._state.set("session_deaths", deaths + 1)

        elif et == "player_respawned":
            # Current SC builds usually log the respawn but not the death itself; a respawn
            # whose death was never seen still counts as one death this session.
            if not data.get("death_observed"):
                deaths = self._state.get("session_deaths", 0) or 0
                self._state.set("session_deaths", deaths + 1)

        # Companion presence — track party members currently in session
        elif et == "party_connected":
            name = data.get("name", "")
            if name:
                companions = self._state.get("active_companions", []) or []
                if name not in companions:
                    companions.append(name)
                    self._state.set("active_companions", companions)

        elif et == "party_disconnected":
            name = data.get("name", "")
            if name:
                companions = self._state.get("active_companions", []) or []
                if name in companions:
                    companions.remove(name)
                    self._state.set("active_companions", companions)

    def _set_place_facts(self, raw: str) -> None:
        """What the place tables say about the place location_name now names (2026-10-05), set together with
        the name so the two cannot disagree: the body it is on or around ("" when nothing says), and whether the
        name is a real one or only the log's code with its underscores removed. The conversation lane reads both."""
        try:
            self._state.set("location_body", get_location_body(raw) or None)
            self._state.set("location_named", bool(location_is_named(raw)))
        except Exception:
            logger.exception("place facts for %r", raw)

    def _emit(self, event: LogEvent) -> None:
        """Emit classified event to subscribers."""
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception:
                logger.exception("Error in classified event subscriber")
