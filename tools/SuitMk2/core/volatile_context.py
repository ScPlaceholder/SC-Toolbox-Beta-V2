"""
SuitMk2 - Volatile Context (RAM)

Short-term temporal memory that sits between raw events and dialogue
decisions.  Acts like RAM — fast, non-persistent, auto-expiring.

Provides:
  - Per-event-type dedup with configurable windows
  - Location stickiness (cities/stations don't re-trigger on sub-zone walks)
  - Ship stickiness (brief exit+reboard = repositioning, not new boarding)
  - EVA inference (exiting ship in deep space)
  - Recent activity summary for LLM prompt injection
  - Context snapshot for ambient dialogue awareness
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class VolatileEvent:
    """A timestamped event record in short-term memory."""

    event_type: str
    timestamp: float
    data: dict[str, Any]
    # Denormalized for fast lookup
    location_name: str = ""
    ship_type: str = ""


@dataclass
class LocationContext:
    """Sticky location tracking — prevents re-announcements."""

    name: str
    location_type: str  # city, station, moon, planet, jump_point, unknown
    location_flavor: str  # urban, orbital, rough, wild, transit, unknown
    raw: str = ""
    arrived_at: float = 0.0
    last_seen_at: float = 0.0
    zone_changes: int = 0  # sub-zone transitions within this sticky location


@dataclass
class ShipContext:
    """Sticky ship tracking — detects repositioning vs. real exits."""

    ship_type: str
    boarded_at: float = 0.0
    exited_at: float | None = None
    exit_location_type: str = ""
    exit_location_name: str = ""
    reboard_count: int = 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MAX_EVENTS = 100
EVENT_RELEVANCE_WINDOW = 600.0       # 10 minutes
LOCATION_STICKY_WINDOW = 1800.0      # 30 minutes — cities "stick" this long
SHIP_REPOSITION_WINDOW = 120.0       # 2 minutes — exit→reboard = reposition
PLANETARY_BODY_COOLDOWN = 300.0      # 5 minutes — same body won't re-announce

STICKY_LOCATION_TYPES = {"city", "station"}

# Per-event-type dedup rules: event_type → (key_field, window_seconds)
# If key_field is empty string, any event of that type within the window dedupes.
_DEDUP_RULES: dict[str, tuple[str, float]] = {
    "location_change":      ("location_name",  60.0),
    "ship_channel_joined":  ("ship_type",      30.0),
    "ship_channel_left":    ("ship_type",      30.0),
    "docking_ready":        ("",               30.0),
    "docking_detached":     ("",               30.0),
    "qt_arrived":           ("",               30.0),
    "qt_error":             ("",               15.0),
    "hangar_ready":         ("",               30.0),
    "med_bed_heal":         ("",               60.0),
    "jurisdiction_change":  ("jurisdiction",   45.0),
    "armistice_zone":       ("action",         20.0),
    "reward_earned":        ("",               10.0),
    "qt_target_selected":   ("destination",    15.0),
    "qt_route_calculated":  ("projected_body", 10.0),
    "planetary_body_entered": ("body_name",   300.0),  # 5-min cooldown per body
}

# Human-readable event summaries for LLM prompt injection
_EVENT_SUMMARIES: dict[str, str] = {
    "location_change":       "Arrived at {location_name} ({location_type})",
    "ship_channel_joined":   "Boarded {channel}",
    "ship_channel_left":     "Exited ship",
    "contract_accepted":     "Accepted contract \"{mission_name}\"",
    "contract_complete":     "Completed contract \"{mission_name}\"",
    "contract_failed":       "Failed contract \"{mission_name}\"",
    "objective_new":         "New objective: {objective}",
    "objective_complete":    "Objective complete: {objective}",
    "injury":               "{severity} injury — {body_part}",
    "med_bed_heal":         "Med bed heal",
    "incapacitated":        "Incapacitated (death)",
    "reward_earned":        "Earned {amount} aUEC",
    "jurisdiction_change":  "Entered {jurisdiction} jurisdiction",
    "armistice_zone":       "{action} armistice zone",
    "entered_monitored_space": "Entered monitored space",
    "exited_monitored_space":  "Exited monitored space",
    "hangar_ready":         "Ship ready at hangar",
    "docking_ready":        "Docking tube extended — landing approach",
    "docking_detached":     "Docking tube retracted — departure",
    "qt_arrived":           "Quantum travel complete — arrived at destination",
    "qt_error":             "Quantum travel error — route obstructed or failed",
    "qt_target_selected":   "Quantum target selected: {destination}",
    "qt_route_calculated":  "Plotting route from {projected_body_friendly}",
    "planetary_body_entered": "Entered {body_name} gravity well ({body_type})",
    "refinery_complete":    "Refinery order complete at {location}",
    "session_start":        "Session started",
    "party_invite":         "Party invite from {from_player}",
}


# ---------------------------------------------------------------------------
# VolatileContext
# ---------------------------------------------------------------------------

class VolatileContext:
    """RAM-like short-term memory for the suit AI.

    Thread-safe.  All public methods acquire ``_lock``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._events: deque[VolatileEvent] = deque(maxlen=MAX_EVENTS)

        # Sticky state
        self._current_location: LocationContext | None = None
        self._previous_locations: deque[LocationContext] = deque(maxlen=10)
        self._current_ship: ShipContext | None = None

        # Planetary body cooldown — tracks last announcement time per body
        self._planetary_body_last_announced: dict[str, float] = {}
        self._current_planetary_body: str | None = None

        # Session-wide medical counters — reset on session_start
        self._session_heal_count: int = 0
        self._session_injury_count: int = 0
        self._session_death_count: int = 0
        self._last_heal_at: float | None = None
        self._last_death_at: float | None = None

    # ------------------------------------------------------------------
    # Event recording & dedup
    # ------------------------------------------------------------------

    def record_event(self, event_type: str, data: dict[str, Any]) -> None:
        """Append an event to the ring buffer."""
        now = time.time()
        evt = VolatileEvent(
            event_type=event_type,
            timestamp=now,
            data=dict(data),  # shallow copy
            location_name=data.get("location_name", ""),
            ship_type=data.get("channel", data.get("ship_type", "")),
        )
        with self._lock:
            self._events.append(evt)
            # Session-wide medical counters
            if event_type == "med_bed_heal":
                self._session_heal_count += 1
                self._last_heal_at = now
            elif event_type == "injury":
                self._session_injury_count += 1
            elif event_type == "incapacitated":
                self._session_death_count += 1
                self._last_death_at = now

    def get_medical_context(self) -> dict[str, Any]:
        """Return session-wide medical counters and recent timing.

        Used by ContextBuilder to assemble rich medical prompt context.
        """
        now = time.time()
        with self._lock:
            heal_count = self._session_heal_count
            injury_count = self._session_injury_count
            death_count = self._session_death_count
            last_heal = self._last_heal_at
            last_death = self._last_death_at
            # Collect recent injuries and deaths from event ring buffer
            recent_injuries = []
            recent_death_times = []
            for evt in self._events:
                if evt.event_type == "injury":
                    part = evt.data.get("body_part", "unknown")
                    sev = evt.data.get("severity", "")
                    tier = evt.data.get("tier", "")
                    recent_injuries.append({
                        "body_part": part,
                        "severity": sev,
                        "tier": tier,
                        "age_seconds": round(now - evt.timestamp),
                    })
                elif evt.event_type == "incapacitated":
                    recent_death_times.append(round(now - evt.timestamp))
        return {
            "session_heal_count": heal_count,
            "session_injury_count": injury_count,
            "session_death_count": death_count,
            "seconds_since_last_heal": round(now - last_heal) if last_heal else None,
            "seconds_since_last_death": round(now - last_death) if last_death else None,
            "recent_injuries": recent_injuries[-5:],  # last 5
            "recent_death_ages_seconds": recent_death_times[-3:],  # last 3
        }

    def is_duplicate(self, event_type: str, data: dict[str, Any]) -> bool:
        """Check if this event should be suppressed per dedup rules."""
        rule = _DEDUP_RULES.get(event_type)
        if rule is None:
            return False

        key_field, window = rule
        key_value = data.get(key_field, "") if key_field else ""
        cutoff = time.time() - window

        with self._lock:
            for evt in reversed(self._events):
                if evt.timestamp < cutoff:
                    break  # older than window — stop searching
                if evt.event_type != event_type:
                    continue
                if not key_field:
                    return True  # any match within window
                if (evt.data.get(key_field, "") == key_value) and key_value:
                    return True

        return False

    # ------------------------------------------------------------------
    # Location stickiness
    # ------------------------------------------------------------------

    def update_location(
        self,
        name: str,
        location_type: str,
        location_flavor: str,
        raw: str,
    ) -> str:
        """Process a location change and return a disposition.

        Returns:
            ``"new_location"`` — genuinely new place, AI should react.
            ``"zone_change"`` — sub-zone of current sticky location, suppress.
            ``"return"`` — returned to a recently-visited location.
            ``"same"`` — exact same location name, suppress entirely.
        """
        now = time.time()

        with self._lock:
            cur = self._current_location

            # --- Exact same location ---
            if cur and cur.name == name:
                cur.last_seen_at = now
                return "same"

            # --- Sticky location logic ---
            # If we're at a city/station and the incoming event is a sub-zone
            # or unknown type within the sticky window, treat as zone_change.
            if cur and cur.location_type in STICKY_LOCATION_TYPES:
                elapsed = now - cur.arrived_at
                if elapsed < LOCATION_STICKY_WINDOW:
                    # Still within the sticky window for this city/station.
                    # Heuristic: if the new location is "unknown" type or
                    # same type as the sticky location, it's a sub-zone walk.
                    if location_type in ("unknown", cur.location_type):
                        cur.zone_changes += 1
                        cur.last_seen_at = now
                        return "zone_change"

            # --- Check if returning to a recent location ---
            for prev in self._previous_locations:
                if prev.name == name:
                    # Returning — move previous to current
                    if cur:
                        self._previous_locations.append(cur)
                    self._current_location = LocationContext(
                        name=name,
                        location_type=location_type,
                        location_flavor=location_flavor,
                        raw=raw,
                        arrived_at=now,
                        last_seen_at=now,
                    )
                    return "return"

            # --- Genuinely new location ---
            if cur:
                self._previous_locations.append(cur)
            self._current_location = LocationContext(
                name=name,
                location_type=location_type,
                location_flavor=location_flavor,
                raw=raw,
                arrived_at=now,
                last_seen_at=now,
            )
            return "new_location"

    # ------------------------------------------------------------------
    # Ship stickiness
    # ------------------------------------------------------------------

    def on_ship_entered(self, ship_type: str) -> str:
        """Process a ship boarding event.

        Returns:
            ``"new_ship"`` — first time boarding this ship.
            ``"reboard"`` — same ship within reposition window (suppress dialogue).
            ``"ship_switch"`` — switching from one ship to another.
        """
        now = time.time()

        with self._lock:
            cur = self._current_ship

            if cur and cur.ship_type == ship_type:
                if cur.exited_at is None:
                    # Still aboard the same ship — duplicate event, suppress
                    return "reboard"
                # Exited and re-entering — check if within reposition window
                if (now - cur.exited_at) < SHIP_REPOSITION_WINDOW:
                    cur.reboard_count += 1
                    cur.exited_at = None
                    return "reboard"
                # Reboarding after longer absence
                cur.exited_at = None
                return "reboard"

            # Different ship or no prior ship
            disposition = "ship_switch" if cur else "new_ship"
            self._current_ship = ShipContext(
                ship_type=ship_type,
                boarded_at=now,
            )
            return disposition

    def on_ship_exited(
        self,
        ship_type: str,
        location_type: str,
        location_name: str,
    ) -> str:
        """Process a ship exit event.

        Returns:
            ``"disembark"`` — at a city/station (normal exit).
            ``"eva"`` — no location or deep space (EVA inference).
            ``"reposition"`` — on a surface/moon (surface ops or repositioning).
        """
        now = time.time()

        with self._lock:
            if self._current_ship:
                self._current_ship.exited_at = now
                self._current_ship.exit_location_type = location_type
                self._current_ship.exit_location_name = location_name

        # Infer exit context from location.
        # EVA is only inferred when the player left a ship AND there's
        # no ground-accessible location — NOT just because location is unknown.
        if location_type in ("city", "station"):
            return "disembark"
        if location_type in ("moon", "planet"):
            return "surface_ops"
        # No known ground location — likely EVA in space
        if not location_name or location_name in ("Unknown", ""):
            return "eva"
        return "disembark"  # default: assume normal exit at a named location

    # ------------------------------------------------------------------
    # Planetary body cooldown
    # ------------------------------------------------------------------

    def update_planetary_body(self, body_name: str) -> str:
        """Process a change in the player's projected planetary body.

        Called when the ``qt_route_calculated`` event reports a new
        "Projected Start Location" that is a planet or moon (not a
        system like "Stanton").

        The cooldown prevents re-announcing the same body when the
        player is quantum-hopping between points within a single
        planet/moon's gravity well (e.g., mining clusters on Aberdeen).

        Returns:
            ``"new_body"`` — first time entering this body, announce it.
            ``"same_body"`` — already here, suppress.
            ``"cooldown"`` — recently announced, suppress.
            ``"returning"`` — returning after cooldown expired, announce.
        """
        now = time.time()

        with self._lock:
            # Already at this body — suppress
            if self._current_planetary_body == body_name:
                return "same_body"

            # Check cooldown for this specific body
            last = self._planetary_body_last_announced.get(body_name)
            if last is not None and (now - last) < PLANETARY_BODY_COOLDOWN:
                # Still update current body (player IS here) but suppress announcement
                self._current_planetary_body = body_name
                return "cooldown"

            # Accept the new body and mark announced
            previous = self._current_planetary_body
            self._current_planetary_body = body_name
            self._planetary_body_last_announced[body_name] = now

            # Prune stale entries (older than 30 min) to prevent unbounded growth
            stale_cutoff = now - 1800.0
            stale_keys = [
                k for k, v in self._planetary_body_last_announced.items()
                if v < stale_cutoff
            ]
            for k in stale_keys:
                del self._planetary_body_last_announced[k]

            if previous is None:
                return "new_body"
            return "returning"

    def get_current_planetary_body(self) -> str | None:
        """Return the current planetary body name, or None."""
        with self._lock:
            return self._current_planetary_body

    # ------------------------------------------------------------------
    # Query APIs for prompt injection & ambient dialogue
    # ------------------------------------------------------------------

    def get_recent_summary(
        self,
        window_seconds: float = 300.0,
        max_events: int = 8,
    ) -> str:
        """Build a formatted string of recent activity for LLM injection.

        Returns a block like:
            [Recent Activity — last 5 minutes]
              3m ago: Arrived at Lorville (city)
              2m ago: Entered armistice zone
              1m ago: Accepted contract "Missing Crew"
        """
        now = time.time()
        cutoff = now - window_seconds

        with self._lock:
            recent = [
                e for e in self._events
                if e.timestamp >= cutoff
            ]

        if not recent:
            return ""

        # Deduplicate: max 2 entries per event type to prevent
        # obsessive fixation on one topic (e.g., 10 med_bed_heals)
        type_counts: dict[str, int] = {}
        deduped: list[VolatileEvent] = []
        for evt in reversed(recent):  # most recent first
            count = type_counts.get(evt.event_type, 0)
            if count < 2:
                deduped.append(evt)
                type_counts[evt.event_type] = count + 1
        deduped.reverse()  # back to chronological

        # Take the most recent N, oldest first
        recent = deduped[-max_events:]

        lines = []
        for evt in recent:
            ago = now - evt.timestamp
            if ago < 60:
                time_str = f"{int(ago)}s ago"
            else:
                time_str = f"{int(ago / 60)}m ago"

            summary = self._summarize_event(evt)
            if summary:
                lines.append(f"  {time_str}: {summary}")

        if not lines:
            return ""

        window_min = int(window_seconds / 60)
        header = f"[Recent Activity \u2014 last {window_min} minutes]"
        return header + "\n" + "\n".join(lines)

    def get_context_snapshot(self) -> dict[str, Any]:
        """Return a dict of volatile state for ambient dialogue."""
        now = time.time()
        cutoff = now - EVENT_RELEVANCE_WINDOW  # 10 min

        with self._lock:
            recent = [e for e in self._events if e.timestamp >= cutoff]
            cur_loc = self._current_location
            cur_ship = self._current_ship
            prev_locs = list(self._previous_locations)

        # Extract typed recent events
        injuries = []
        deaths = 0
        contracts = []
        rewards = 0
        locations_visited = []

        for evt in recent:
            if evt.event_type == "injury":
                part = evt.data.get("body_part", "unknown")
                sev = evt.data.get("severity", "")
                injuries.append(f"{sev} {part}".strip())
            elif evt.event_type == "incapacitated":
                deaths += 1
            elif evt.event_type in ("contract_accepted", "contract_complete"):
                name = evt.data.get("mission_name", "")
                if name:
                    contracts.append(name)
            elif evt.event_type == "reward_earned":
                rewards += evt.data.get("amount", 0)
            elif evt.event_type == "location_change" and evt.location_name:
                if evt.location_name not in locations_visited:
                    locations_visited.append(evt.location_name)

        # Ship status
        ship_status = "none"
        ship_type = ""
        if cur_ship:
            ship_type = cur_ship.ship_type
            if cur_ship.exited_at is None:
                ship_status = "aboard"
            else:
                ship_status = "away"

        planetary_body = self._current_planetary_body

        return {
            "location_name": cur_loc.name if cur_loc else "",
            "location_type": cur_loc.location_type if cur_loc else "",
            "minutes_at_location": round((now - cur_loc.arrived_at) / 60, 1) if cur_loc else 0,
            "ship_type": ship_type,
            "ship_status": ship_status,
            "planetary_body": planetary_body or "",
            "recent_injuries": injuries,
            "recent_deaths": deaths,
            "recent_contracts": contracts,
            "recent_rewards_auec": rewards,
            "recent_locations_visited": locations_visited,
            "previous_locations": [p.name for p in prev_locs[-5:]],
        }

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    def on_session_start(self) -> None:
        """Clear all volatile state for a new session."""
        with self._lock:
            self._events.clear()
            self._current_location = None
            self._previous_locations.clear()
            self._current_ship = None
            self._planetary_body_last_announced.clear()
            self._current_planetary_body = None
            self._session_heal_count = 0
            self._session_injury_count = 0
            self._session_death_count = 0
            self._last_heal_at = None
            self._last_death_at = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _summarize_event(evt: VolatileEvent) -> str:
        """Map an event to a short human-readable summary."""
        template = _EVENT_SUMMARIES.get(evt.event_type)
        if not template:
            return ""
        try:
            # Merge event data with fallbacks for missing keys
            params = {
                "location_name": evt.location_name or "unknown",
                "location_type": evt.data.get("location_type", ""),
                "channel": evt.data.get("channel", evt.ship_type or "ship"),
                "ship_type": evt.ship_type or "ship",
                "mission_name": evt.data.get("mission_name", "unknown"),
                "objective": evt.data.get("objective", "unknown"),
                "severity": evt.data.get("severity", ""),
                "body_part": evt.data.get("body_part", "unknown"),
                "amount": evt.data.get("amount", 0),
                "jurisdiction": evt.data.get("jurisdiction", "unknown"),
                "action": evt.data.get("action", "entered"),
                "location": evt.data.get("location", "unknown"),
                "from_player": evt.data.get("from_player", "unknown"),
                "destination": evt.data.get("destination", "unknown"),
                "projected_body_friendly": evt.data.get("projected_body_friendly", "unknown"),
                "body_name": evt.data.get("body_name", "unknown"),
                "body_type": evt.data.get("body_type", ""),
            }
            return template.format(**params)
        except (KeyError, ValueError):
            return evt.event_type.replace("_", " ")
