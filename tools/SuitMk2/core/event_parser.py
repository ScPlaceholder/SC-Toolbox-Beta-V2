"""
SuitMk2 - Event Parser

Parses raw Star Citizen log lines into structured LogEvent objects.
Handles timestamp extraction, event classification, and data extraction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

import importlib.util
import os

# Path-based import to avoid collision with sc_log_reader's location_names
_HERE = os.path.dirname(os.path.abspath(__file__))
try:
    _spec = importlib.util.spec_from_file_location(
        "suitMk2_location_names",
        os.path.join(_HERE, "location_names.py"),
    )
    _loc_mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_loc_mod)
    get_location_name = _loc_mod.get_location_name
    get_location_system = _loc_mod.get_location_system
except Exception:
    logging.getLogger(__name__).warning("Failed to load location_names.py; using fallback stubs.")
    def get_location_name(raw: str) -> str:
        return raw
    def get_location_system(raw: str) -> str:
        return "Unknown"

logger = logging.getLogger(__name__)


@dataclass
class LogEvent:
    """Structured representation of a parsed log event."""

    event_type: str
    timestamp: datetime
    raw_line: str
    category: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_type": self.event_type,
            "category": self.category,
            "timestamp": self.timestamp.isoformat(),
            "data": self.data,
        }


# Ship manufacturer prefixes for channel name detection (code format)
_SHIP_PREFIXES = [
    "ORIG_", "ANVL_", "AEGS_", "CRUS_", "DRAK_", "MISC_", "RSI_",
    "ARGO_", "BANU_", "CNOU_", "ESPR_", "GAMA_", "KRIG_", "TMBL_",
    "VNCL_", "XIAN_", "XNAA_",
]

# Ship manufacturer full names (for cleaned channel names like "Drake Cutlass Blue")
_SHIP_MANUFACTURERS = [
    "Aegis", "Anvil", "Argo", "Banu", "Consolidated Outland", "Crusader",
    "Drake", "Esperia", "Gatac", "Greycat", "Kruger", "MISC",
    "Origin", "RSI", "Tumbril", "Vanduul",
]

# Event category mapping
_CATEGORY_MAP = {
    "session_start": "SESSION",
    "session_end": "SESSION",
    "session_crash": "SESSION",
    "join_pu": "SESSION",
    "location_change": "LOCATION",
    "channel_change": "SHIP",
    "contract_accepted": "MISSION",
    "contract_complete": "MISSION",
    "contract_failed": "MISSION",
    "contract_shared": "MISSION",
    "contract_available": "MISSION",
    "objective_new": "MISSION",
    "objective_complete": "MISSION",
    "objective_withdrawn": "MISSION",
    "injury": "ACTOR_STATE",
    "med_bed_heal": "ACTOR_STATE",
    "emergency_services": "ACTOR_STATE",
    "incapacitated": "ACTOR_STATE",
    "player_respawned": "ACTOR_STATE",
    "jurisdiction_change": "LOCATION",
    "armistice_zone": "LOCATION",
    "restricted_area": "LOCATION",
    "entered_monitored_space": "LOCATION",
    "exited_monitored_space": "LOCATION",
    "monitored_space_down": "LOCATION",
    "monitored_space_restored": "LOCATION",
    "hangar_ready": "SHIP",
    "hangar_queue": "SHIP",
    "docking_ready": "SHIP",
    "docking_detached": "SHIP",
    "qt_arrived": "SHIP",
    "qt_error": "SHIP",
    "qt_target_selected": "SHIP",
    "qt_route_calculated": "SHIP",
    "planetary_body_entered": "SHIP",
    "party_invite": "SOCIAL",
    "incoming_call": "SOCIAL",
    "reward_earned": "ECONOMY",
    "blueprint_received": "ECONOMY",
    "weapon_holstered": "COMBAT",
    "refinery_complete": "ECONOMY",
    "platform_moving": "SHIP",
    "journal_entry": "LORE",
}


# Local-player death / respawn detection (2026-09-23, from J's own Game.log history, 1069 logs).
# SC has stripped its death logging patch by patch; each shape below is a real, verbatim line and
# the last build it was seen in. None of the death lines fires unless it names the LOCAL player.
#   <Actor Death> CActor::Kill: '<victim>' ... killed by '<killer>' ... damage type '<cause>'   <= 10591185 (Nov 2025)
#   Logged an incap.! nickname: <name>, causes: [<Cause> (<n> damage), ...]                      <= 11218823 (Feb 2026)
#   <[ActorState] Dead> ... Actor '<name>' ... ejected from zone ... destroyed vehicle            <= 12122953 (Jul 2026)
#   "Standby, Local Emergency Services Are En Route"  (HUD notice, local only)                   still present Sep 2026
#   <Recv Unbind Batch Add Player> ... playerGEID=<local geid>   = respawn (old body unbound)    11825000 .. Sep 2026
# In current builds the moment of death is NOT logged unless the emergency notice fires; the respawn is.
_INCAP_DEBOUNCE_S = 120.0   # one incapacitation per incident: SC repeats the same one on several lines


class EventParser:
    """
    Parses raw log lines into structured LogEvent objects.

    Subscribes to LogMonitor for raw lines and emits parsed events.
    Also updates a StateStore with atomic state changes.
    """

    def __init__(self) -> None:
        self._event_subscribers: list[Callable[[LogEvent], None]] = []
        self._last_session_geid: str | None = None
        self._diagnostic_callback: Callable[[str], None] | None = None
        # Local player identity: from the login line, or (if we attached mid-session) from
        # <AttachmentReceived> Player[...], which in every one of J's logs names only the local player.
        self._local_name: str | None = None
        self._local_geid: str | None = None
        self._last_incap_at: datetime | None = None
        self._incap_since_respawn = False

    def subscribe(self, callback: Callable[[LogEvent], None]) -> None:
        """Subscribe to parsed events."""
        self._event_subscribers.append(callback)

    def set_diagnostic_callback(self, callback: Callable[[str], None]) -> None:
        """Set callback for lines that contain health keywords but didn't match.

        Used for debugging when death/injury events aren't being detected.
        """
        self._diagnostic_callback = callback

    def on_raw_line(self, line: str) -> None:
        """Process a raw log line. Called by LogMonitor."""
        if self._local_name is None and "<AttachmentReceived> Player[" in line:
            m = re.search(r"<AttachmentReceived> Player\[([^\]]+)\]", line)
            if m:
                self._local_name = m.group(1)

        event = self._parse_line(line)
        if event is None:
            return

        # Deduplicate repeated session_start during loading screens
        if event.event_type == "session_start":
            self._local_name = event.data.get("player_name") or self._local_name
            self._local_geid = event.data.get("player_geid") or self._local_geid
            geid = event.data.get("player_geid")
            if geid and geid == self._last_session_geid:
                return
            self._last_session_geid = geid

        if event.event_type == "incapacitated" and not self._accept_incap(event):
            return
        if event.event_type == "player_respawned":
            event.data["death_observed"] = self._incap_since_respawn
            self._incap_since_respawn = False
            self._last_incap_at = None

        self._emit_event(event)

        # The emergency notice is the only death-moment line current builds still write for the
        # local player. emergency_services is emitted unchanged; incapacitated is derived from it.
        if event.event_type == "emergency_services" and "en route" in line.lower():
            derived = LogEvent(
                event_type="incapacitated",
                timestamp=event.timestamp,
                raw_line=line,
                category=_CATEGORY_MAP["incapacitated"],
                data={"state": "downed", "source": "emergency_services"},
            )
            if self._accept_incap(derived):
                self._emit_event(derived)

    def _accept_incap(self, event: LogEvent) -> bool:
        """One incapacitated per incident: SC repeats the same death on several lines."""
        last = self._last_incap_at
        if last is not None and abs((event.timestamp - last).total_seconds()) < _INCAP_DEBOUNCE_S:
            return False
        self._last_incap_at = event.timestamp
        self._incap_since_respawn = True
        return True

    def _names_local_player(self, line: str) -> bool:
        """True only if a death line's victim is the local player (never guess when unknown)."""
        name = self._local_name
        if not name:
            return False
        if "<Actor Death>" in line:
            m = re.search(r"CActor::Kill:\s*'([^']+)'", line)
            return bool(m) and m.group(1) == name
        if "Logged an incap.!" in line:
            m = re.search(r"nickname:\s*([^,]+),", line)
            return bool(m) and m.group(1).strip() == name
        if "<[ActorState] Dead>" in line:
            m = re.search(r"Actor\s+'([^']+)'", line)
            return bool(m) and m.group(1) == name
        return False

    def _parse_line(self, line: str) -> LogEvent | None:
        """Parse a log line into a LogEvent."""
        event_type = self._classify_line(line)
        if event_type is None:
            return None

        timestamp = self._extract_timestamp(line)
        data = self._extract_data(line, event_type)
        category = _CATEGORY_MAP.get(event_type, "UNKNOWN")

        return LogEvent(
            event_type=event_type,
            timestamp=timestamp,
            raw_line=line,
            category=category,
            data=data,
        )

    def _extract_timestamp(self, line: str) -> datetime:
        """Extract ISO timestamp from log line."""
        match = re.search(r"<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d+)Z?>", line)
        if match:
            try:
                ts_str = match.group(1)
                if "." in ts_str:
                    base, frac = ts_str.split(".")
                    frac = frac[:6].ljust(6, "0")
                    ts_str = f"{base}.{frac}"
                return datetime.fromisoformat(ts_str)
            except ValueError:
                pass
        return datetime.now()

    def _classify_line(self, line: str) -> str | None:
        """Determine event type from a raw log line."""
        # Skip duplicate notification lines
        if "UpdateNotificationItem" in line:
            return None

        # Session events
        if "AccountLoginCharacterStatus_Character" in line:
            return "session_start"
        if "{Join PU}" in line:
            return "join_pu"
        if "SystemQuit" in line:
            return "session_end"
        # 30k crash: Network Error with SC disconnect codes 30010/30016
        if "Network Error 30010" in line or "Network Error 30016" in line:
            return "session_crash"
        if "Disconnecting from Stanton" in line or "Disconnecting from Pyro" in line:
            return "session_end"

        # Hangar ship elevators and freight elevators (2026-09-24, from the old skill's interior_parser). J's log:
        # a freight run is ClosingFrontGate -> LoweringPlatform -> (~40 s) RaisingPlatform -> OpeningFrontGate, and
        # the many OpenIdle/ClosedIdle lines are the hangar STREAMING IN, not anyone using it. Only a real move counts.
        # (Personal lifts are NOT logged; these are the cargo and ship platforms.)
        if "OnLoadingPlatformStateChanged" in line and ("LoweringPlatform" in line or "RaisingPlatform" in line):
            return "platform_moving"

        # Health events — top-level checks (no SHUDEvent wrapper required)
        if "<MED BED HEAL>" in line and "Perform surgery event Success" in line:
            return "med_bed_heal"
        if "Medical Bed:" in line and "restored your health" in line:
            return "med_bed_heal"
        if "Injury Detected" in line:
            return "injury"
        if "Emergency Services" in line and "en route" in line.lower():
            return "emergency_services"

        # Local player death / incapacitation (legacy shapes; see the note above EventParser)
        if ("<Actor Death>" in line or "Logged an incap.!" in line
                or "<[ActorState] Dead>" in line):
            if self._names_local_player(line):
                return "incapacitated"
        # Local player respawn: the old body's player entity is unbound, then re-bound
        if "<Recv Unbind Batch Add Player>" in line:
            m = re.search(r"playerGEID=(\d+)", line)
            if self._local_geid is None or (m and m.group(1) == self._local_geid):
                return "player_respawned"

        # SHUDEvent-based events
        if "SHUDEvent" in line:
            return self._classify_shud_event(line)

        # Location events
        if "RequestLocationInventory" in line:
            return "location_change"

        # Quantum travel events
        if "[QuantumTravel]" in line:
            if "Quantum Drive Arrived" in line or "Arrived at Final Destination" in line:
                return "qt_arrived"
            if "Failed to get starmap route data" in line:
                return "qt_error"
            if "Player Selected Quantum Target" in line:
                return "qt_target_selected"
            if "Projected Start Location is" in line:
                return "qt_route_calculated"
        if "SHUDEvent" in line and "Quantum Travel:" in line:
            if "obstructed" in line:
                return "qt_error"

        # Docking tube state changes (landing approach / departure)
        if "CDockingAnimatorComponent::OnSetCurrentState" in line:
            if "eES_Ready previous state eES_Readying" in line:
                return "docking_ready"
            if "eES_Unready previous state eES_Unreadying" in line:
                return "docking_detached"

        # Channel changes (ship enter/exit) — outside SHUDEvent
        line_lower = line.lower()
        if ("joined channel" in line_lower or
                "left channel" in line_lower or
                "left the channel" in line_lower):
            return "channel_change"

        # A weapon going INTO a weapon slot (holster / back). Not a SHUDEvent, so it must be classified HERE: first
        # placed in _classify_shud_event, where no AttachmentReceived line ever arrives (caught on a real log).
        # The log never records a DRAW, so this is only "put away" (or loadout at spawn): a combat-OFF hint.
        if "<AttachmentReceived>" in line and "Port[wep_" in line:
            return "weapon_holstered"

        # Diagnostic: log lines with health/combat keywords that we didn't match
        if self._diagnostic_callback:
            _HEALTH_KEYWORDS = (
                "injury", "death", "killed", "died", "emergency",
                "bleeding", "incapacitated", "unconscious", "flatline",
                "damage", "wound", "fatal", "respawn", "downed",
            )
            if any(kw in line_lower for kw in _HEALTH_KEYWORDS):
                self._diagnostic_callback(line)

        return None

    def _classify_shud_event(self, line: str) -> str | None:
        """Classify SHUDEvent notification lines."""
        line_lower = line.lower()

        # Contracts
        if "Contract Accepted:" in line:
            return "contract_accepted"
        if "Contract Complete:" in line:
            return "contract_complete"
        if "Contract Failed:" in line:
            return "contract_failed"
        if "Contract Shared:" in line:
            return "contract_shared"
        if "Contract Available:" in line:
            return "contract_available"

        # Objectives
        if "New Objective:" in line:
            return "objective_new"
        if "Objective Complete:" in line:
            return "objective_complete"
        if "Objective Withdrawn:" in line:
            return "objective_withdrawn"

        # Monitored space
        if "Entered Monitored Space" in line:
            return "entered_monitored_space"
        if "Exited Monitored Space" in line:
            return "exited_monitored_space"
        if "Monitored Space Down" in line:
            return "monitored_space_down"
        if "Monitored Space Restored" in line:
            return "monitored_space_restored"

        # Journal entries (before jurisdiction to avoid false match)
        if "Journal Entry Added:" in line:
            return "journal_entry"

        # Zones and jurisdiction
        if "Jurisdiction" in line:
            return "jurisdiction_change"
        if "Armistice Zone" in line:
            return "armistice_zone"
        if "Restricted Area" in line:
            return "restricted_area"

        # Ships and hangars
        if "Hangar Request Completed" in line:
            return "hangar_ready"
        if "Joined hangar queue" in line:
            return "hangar_queue"

        # Social
        if "Party Invite Received" in line:
            return "party_invite"
        if "Incoming call:" in line:
            return "incoming_call"

        # Economy
        if "You've earned:" in line:
            return "reward_earned"
        if "Received Blueprint:" in line:
            return "blueprint_received"
        if "Refinery Work Order" in line:
            return "refinery_complete"

        # Medical
        if "Emergency Services Are En Route" in line:
            return "emergency_services"

        # Channel changes from SHUDEvent
        if ("joined channel" in line_lower or
                "left channel" in line_lower or
                "left the channel" in line_lower):
            return "channel_change"

        return None

    def _extract_data(self, line: str, event_type: str) -> dict[str, Any]:
        """Extract structured data from a log line based on event type."""
        data: dict[str, Any] = {}

        if event_type == "session_start":
            match = re.search(r"name\s+(\S+)", line)
            if match:
                data["player_name"] = match.group(1)
            match = re.search(r"geid\s+(\S+)", line)
            if match:
                data["player_geid"] = match.group(1)

        elif event_type == "join_pu":
            match = re.search(r"\{Join PU\}\s*\[([^\]]+)\]", line)
            if match:
                data["shard"] = match.group(1)

        elif event_type in ("contract_accepted", "contract_complete", "contract_failed"):
            type_labels = {
                "contract_accepted": "Contract Accepted",
                "contract_complete": "Contract Complete",
                "contract_failed": "Contract Failed",
            }
            pattern = rf'"{type_labels[event_type]}:\s*(.+?):\s*"'
            name_match = re.search(pattern, line)
            if name_match:
                data["mission_name"] = name_match.group(1).strip()
            id_match = re.search(r"MissionId:\s*\[([^\]]+)\]", line)
            if id_match:
                data["mission_id"] = id_match.group(1)

        elif event_type in ("contract_shared", "contract_available"):
            type_labels = {
                "contract_shared": "Contract Shared",
                "contract_available": "Contract Available",
            }
            pattern = rf'"{type_labels[event_type]}:\s*(.+?):\s*"'
            name_match = re.search(pattern, line)
            if name_match:
                data["mission_name"] = name_match.group(1).strip()
            id_match = re.search(r"MissionId:\s*\[([^\]]+)\]", line)
            if id_match:
                data["mission_id"] = id_match.group(1)

        elif event_type == "objective_new":
            obj_match = re.search(r'"New Objective:\s*(.+?):\s*"', line)
            if obj_match:
                data["objective"] = obj_match.group(1).strip()
            id_match = re.search(r"MissionId:\s*\[([^\]]+)\]", line)
            if id_match:
                data["mission_id"] = id_match.group(1)

        elif event_type == "objective_complete":
            obj_match = re.search(r'"Objective Complete:\s*(.+?)(?::\s*"|\s*")', line)
            if obj_match:
                data["objective"] = obj_match.group(1).strip()
            id_match = re.search(r"MissionId:\s*\[([^\]]+)\]", line)
            if id_match:
                data["mission_id"] = id_match.group(1)

        elif event_type == "objective_withdrawn":
            obj_match = re.search(r'"Objective Withdrawn:\s*(.+?):\s*"', line)
            if obj_match:
                data["objective"] = obj_match.group(1).strip()
            id_match = re.search(r"MissionId:\s*\[([^\]]+)\]", line)
            if id_match:
                data["mission_id"] = id_match.group(1)

        elif event_type == "injury":
            sev_match = re.search(r"(\w+)\s+Injury Detected", line)
            if sev_match:
                data["severity"] = sev_match.group(1)
            part_match = re.search(r"Injury Detected\s*-\s*([^-]+)\s*-", line)
            if part_match:
                data["body_part"] = part_match.group(1).strip()
            tier_match = re.search(r"Tier\s*(\d+)", line)
            if tier_match:
                data["tier"] = int(tier_match.group(1))

        elif event_type == "incapacitated":
            if "<Actor Death>" in line:
                data["state"] = "dead"
                data["source"] = "actor_death"
                m = re.search(
                    r"in zone '([^']*)' killed by '([^']*)'.*?using '([^']*)' \[Class ([^\]]*)\]"
                    r".*?damage type '([^']*)'", line)
                if m:
                    data["zone"] = m.group(1)
                    data["cause"] = m.group(5)
                    killer = m.group(2)
                    if killer and killer not in ("unknown", self._local_name):
                        data["killer"] = killer
                    if m.group(4) and m.group(4) not in ("unknown", "Player"):
                        data["weapon_class"] = m.group(4)
            elif "Logged an incap.!" in line:
                data["state"] = "downed"
                data["source"] = "incap_log"
                causes = re.findall(r"(\w+) \([\d.]+ damage\)", line)
                if causes:
                    data["causes"] = causes
                    data["cause"] = causes[0]
            elif "<[ActorState] Dead>" in line:
                data["state"] = "dead"
                data["source"] = "actor_state_dead"
                data["cause"] = "vehicle_destroyed"
                m = re.search(r"ejected from zone '([^']*)'.*?to zone '([^']*)'", line)
                if m:
                    data["zone"] = m.group(1)
                    data["location_raw"] = m.group(2)

        elif event_type == "player_respawned":
            m = re.search(r"playerGEID=(\d+)", line)
            if m:
                data["player_geid"] = m.group(1)

        elif event_type == "med_bed_heal":
            healed = {}
            for part in ["head", "torso", "leftArm", "rightArm", "leftLeg", "rightLeg"]:
                if f"{part}: true" in line:
                    healed[part] = True
            data["healed_parts"] = healed

        elif event_type == "location_change":
            loc_match = re.search(r"Location\[([^\]]+)\]", line)
            if loc_match:
                raw = loc_match.group(1)
                data["location_raw"] = raw
                data["location_name"] = get_location_name(raw)
                data["star_system"] = get_location_system(raw)

        elif event_type == "channel_change":
            line_lower = line.lower()
            # ONLY the pilot's own lines are boarding/leaving. Measured on J's logs 2026-09-24: the pilot's read "You have
            # joined channel" / "You have left the channel"; other players' read "<name> has joined/left the channel".
            # The old test matched any "left the channel", so 1,298 crewmates leaving across his backups each cleared
            # the PILOT's current ship in the classifier.
            if "you have joined channel" in line_lower:
                data["action"] = "joined"
            elif "you have left channel" in line_lower or "you have left the channel" in line_lower:
                data["action"] = "left"
            elif "has joined" in line_lower:
                data["action"] = "other_joined"
            elif "has left" in line_lower:
                data["action"] = "other_left"
            ch_match = re.search(
                r"(?:joined|left(?:\s+the)?)\s+channel\s+'([^']+)'",
                line, re.IGNORECASE,
            )
            if ch_match:
                raw_channel = ch_match.group(1)
                data["channel_raw"] = raw_channel
                data["channel"] = _clean_ship_name(raw_channel)
                data["is_ship"] = _is_ship_channel(raw_channel)

        elif event_type == "jurisdiction_change":
            jur_match = re.search(r'"Entered\s+(.+?)\s+Jurisdiction', line)
            if jur_match:
                data["jurisdiction"] = jur_match.group(1).strip()

        elif event_type in (
            "entered_monitored_space", "exited_monitored_space",
            "monitored_space_down", "monitored_space_restored",
        ):
            data["monitored"] = event_type in (
                "entered_monitored_space", "monitored_space_restored",
            )

        elif event_type == "armistice_zone":
            if "Entering" in line or "entered" in line.lower():
                data["action"] = "entered"
            elif "Leaving" in line or "exited" in line.lower():
                data["action"] = "exited"

        elif event_type == "restricted_area":
            data["action"] = "exited" if "Leaving" in line else "entered"

        elif event_type == "hangar_ready":
            data["action"] = "ready"

        elif event_type == "hangar_queue":
            data["action"] = "queued"

        elif event_type == "party_invite":
            invite_match = re.search(r'"([^"\\]+?)\\nParty Invite Received', line)
            if invite_match:
                data["from_player"] = invite_match.group(1).strip()

        elif event_type == "journal_entry":
            journal_match = re.search(r'"Journal Entry Added:\s*(.+?):\s*"', line)
            if journal_match:
                data["subject"] = journal_match.group(1).strip()

        elif event_type == "incoming_call":
            call_match = re.search(r'"Incoming call:\s*([^"]*)"', line)
            if call_match:
                caller = call_match.group(1).strip()
                if caller:
                    data["caller"] = caller

        elif event_type == "platform_moving":
            m = re.search(r"Manager \[([^\]]+)\] Platform state changed to (\w+)", line)
            if m:
                name, state = m.group(1), m.group(2)
                data["platform"] = name
                data["kind"] = ("ship" if "ShipElevator" in name else
                                "freight" if "FreightElevator" in name else "platform")
                data["direction"] = "down" if state == "LoweringPlatform" else "up"

        elif event_type == "refinery_complete":
            ref_match = re.search(r"Refinery Work Order.*?at\s+([^:\"]+)", line)
            if ref_match:
                data["location"] = ref_match.group(1).strip()

        elif event_type == "reward_earned":
            # Measured 2026-09-23 across 80 logs back to June: "You've earned:" names ITEMS ("Falston Jumpsuit
            # 'People's Alliance Edition'", "People's Alliance Hat"). aUEC payouts have no log line at all, so the
            # amount branch has never matched a real line in these builds. Kept in case a build logs money again.
            amount_match = re.search(r"You've earned:\s*([\d,]+)\b(?!\s*[A-Za-z])", line)
            if amount_match:
                data["amount"] = int(amount_match.group(1).replace(",", ""))
            else:
                item_match = re.search(r"You've earned:\s*(.+?)\s*(?:\"\s*\[\d+\]|\[\d+\]|$)", line)
                if item_match:
                    data["item"] = " ".join(item_match.group(1).replace('"', " ").split())   # quotes are refused on speech

        elif event_type == "weapon_holstered":
            m = re.search(r"Attachment\[[^,\]]+,\s*([^,\]]+),.*?Port\[(wep_[^\]]+)\]", line)
            if m:
                data["weapon"], data["port"] = m.group(1).strip(), m.group(2)
            e = re.search(r"Elapsed\[([\d.]+)\]", line)
            if e:
                data["elapsed_s"] = float(e.group(1))     # ~0 = a whole loadout attaching at spawn, not a holster

        elif event_type == "blueprint_received":
            bp = re.search(r"Received Blueprint:\s*(.+?)\s*:?\s*\"?\s*(?:\[\d+\]|$)", line)
            if bp:
                data["blueprint"] = bp.group(1).strip().rstrip(":").strip()

        elif event_type == "qt_target_selected":
            # Extract destination from: "Player has selected point <dest> as their destination"
            dest_match = re.search(
                r"selected point\s+(.+?)\s+as their destination", line,
            )
            if dest_match:
                raw_dest = dest_match.group(1).strip()
                data["destination_raw"] = raw_dest
                data["destination"] = _resolve_planetary_body(raw_dest)
            # Extract ship from the line (e.g., DRAK_Caterpillar_Pirate_...)
            ship_match = re.search(
                r"\|\s*(?:ORIG|ANVL|AEGS|CRUS|DRAK|MISC|RSI|ARGO|BANU|CNOU|ESPR|GAMA|KRIG|TMBL|VNCL|XIAN|XNAA)_(\S+?)_\d+\[",
                line,
            )
            if ship_match:
                data["ship_code"] = ship_match.group(0).split("|")[-1].strip().split("[")[0].strip()

        elif event_type == "qt_route_calculated":
            # Extract projected start location: "Projected Start Location is <body>"
            body_match = re.search(
                r"Projected Start Location is\s+(.+?)\s+for route to destination\s+(.+?)(?:\s|$)",
                line,
            )
            if body_match:
                raw_body = body_match.group(1).strip()
                raw_dest = body_match.group(2).strip()
                data["projected_body"] = raw_body
                data["projected_body_friendly"] = _resolve_planetary_body_name(raw_body)
                data["destination_raw"] = raw_dest
                data["destination"] = _resolve_planetary_body(raw_dest)

        return data

    def _emit_event(self, event: LogEvent) -> None:
        """Send event to all subscribers."""
        for callback in self._event_subscribers:
            try:
                callback(event)
            except Exception:
                logger.exception("Error in event subscriber")


# ---------------------------------------------------------------------------
# Planetary body name resolution
# ---------------------------------------------------------------------------
# The "Projected Start Location" field uses friendly names like "Hurston",
# "Aberdeen", "Stanton", "Nyx", etc.  We also get OOC codes like
# "OOC_Stanton_1_Hurston" in destination fields.  These helpers normalise
# both forms to a consistent friendly name.

# Map of OOC object-container codes to (friendly_name, body_type, parent_body)
_PLANETARY_BODIES: dict[str, tuple[str, str, str]] = {
    # Stanton planets
    "OOC_Stanton_1_Hurston":     ("Hurston",   "planet", "Stanton"),
    "OOC_Stanton_2_Crusader":    ("Crusader",  "planet", "Stanton"),
    "OOC_Stanton_3_ArcCorp":     ("ArcCorp",   "planet", "Stanton"),
    "OOC_Stanton_4_Microtech":   ("microTech", "planet", "Stanton"),
    # Hurston moons
    "OOC_Stanton_1a_Ariel":      ("Arial",     "moon", "Hurston"),
    "OOC_Stanton_1a_Arial":      ("Arial",     "moon", "Hurston"),
    "OOC_Stanton_1b_Aberdeen":   ("Aberdeen",  "moon", "Hurston"),
    "OOC_Stanton_1c_Magda":      ("Magda",     "moon", "Hurston"),
    "OOC_Stanton_1d_Ita":        ("Ita",       "moon", "Hurston"),
    # Crusader moons
    "OOC_Stanton_2a_Cellin":     ("Cellin",    "moon", "Crusader"),
    "OOC_Stanton_2b_Daymar":     ("Daymar",    "moon", "Crusader"),
    "OOC_Stanton_2c_Yela":       ("Yela",      "moon", "Crusader"),
    # ArcCorp moons
    "OOC_Stanton_3a_Lyria":      ("Lyria",     "moon", "ArcCorp"),
    "OOC_Stanton_3b_Wala":       ("Wala",      "moon", "ArcCorp"),
    # microTech moons
    "OOC_Stanton_4a_Calliope":   ("Calliope",  "moon", "microTech"),
    "OOC_Stanton_4b_Clio":       ("Clio",      "moon", "microTech"),
    "OOC_Stanton_4c_Euterpe":    ("Euterpe",   "moon", "microTech"),
}

# Friendly names used directly in "Projected Start Location is <name>"
_FRIENDLY_BODY_NAMES: dict[str, tuple[str, str, str]] = {
    # (normalised_name, body_type, parent_or_system)
    "hurston":   ("Hurston",   "planet", "Stanton"),
    "crusader":  ("Crusader",  "planet", "Stanton"),
    "arccorp":   ("ArcCorp",   "planet", "Stanton"),
    "microtech": ("microTech", "planet", "Stanton"),
    "arial":     ("Arial",     "moon", "Hurston"),
    "aberdeen":  ("Aberdeen",  "moon", "Hurston"),
    "magda":     ("Magda",     "moon", "Hurston"),
    "ita":       ("Ita",       "moon", "Hurston"),
    "cellin":    ("Cellin",    "moon", "Crusader"),
    "daymar":    ("Daymar",    "moon", "Crusader"),
    "yela":      ("Yela",      "moon", "Crusader"),
    "lyria":     ("Lyria",     "moon", "ArcCorp"),
    "wala":      ("Wala",      "moon", "ArcCorp"),
    "calliope":  ("Calliope",  "moon", "microTech"),
    "clio":      ("Clio",      "moon", "microTech"),
    "euterpe":   ("Euterpe",   "moon", "microTech"),
    # System-level (not a planet/moon — used as fallback)
    "stanton":   ("Stanton",   "system", ""),
    "nyx":       ("Nyx",       "system", ""),
    "pyro":      ("Pyro",      "system", ""),
}


def _resolve_planetary_body(raw: str) -> str:
    """Resolve an OOC code or raw destination to a friendly name."""
    if raw in _PLANETARY_BODIES:
        return _PLANETARY_BODIES[raw][0]
    # Try friendly lookup
    lower = raw.lower().strip()
    if lower in _FRIENDLY_BODY_NAMES:
        return _FRIENDLY_BODY_NAMES[lower][0]
    return raw


def _resolve_planetary_body_name(name: str) -> str:
    """Resolve a 'Projected Start Location' name to a canonical form."""
    lower = name.lower().strip()
    info = _FRIENDLY_BODY_NAMES.get(lower)
    if info:
        return info[0]
    return name


def _is_planetary_body(name: str) -> bool:
    """Return True if the name is a known planet or moon (not a system/station)."""
    lower = name.lower().strip()
    info = _FRIENDLY_BODY_NAMES.get(lower)
    if info:
        return info[1] in ("planet", "moon")
    return False


def _get_body_info(name: str) -> tuple[str, str, str] | None:
    """Return (friendly_name, body_type, parent) or None."""
    lower = name.lower().strip()
    return _FRIENDLY_BODY_NAMES.get(lower)


def _is_ship_channel(channel: str) -> bool:
    """Detect if a channel name represents a ship.

    Handles both coded names (DRAK_Cutlass) and cleaned names (Drake Cutlass Blue).
    """
    channel_upper = channel.upper()
    # Check prefix codes (e.g., DRAK_, AEGS_)
    if any(prefix in channel_upper for prefix in _SHIP_PREFIXES):
        return True
    # Check full manufacturer names (e.g., "Drake Cutlass Blue : PlayerName")
    name = channel.split(" : ")[0] if " : " in channel else channel
    name_lower = name.lower()
    return any(mfr.lower() in name_lower for mfr in _SHIP_MANUFACTURERS)


def _clean_ship_name(raw_channel: str) -> str:
    """Clean raw channel name to readable ship name.

    Handles both coded (DRAK_Cutlass) and cleaned (Drake Cutlass Blue) formats.
    """
    name = raw_channel
    if " : " in name:
        name = name.split(" : ")[0]
    if name.startswith("@vehicle_Name"):
        name = name[13:]
    # Strip code prefixes (e.g., DRAK_)
    for prefix in _SHIP_PREFIXES:
        if name.upper().startswith(prefix):
            name = name[len(prefix):]
            break
    return name.replace("_", " ").strip()
