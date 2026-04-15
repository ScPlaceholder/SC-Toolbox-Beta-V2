"""
Mining Signals — Refinery Order Data Model + Persistence + Matching

Tracks refinery work orders through their lifecycle:
  OCR capture → in_process → log completion match → complete

Persists to mining_signals_config.json under "refinery_orders".
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)


@dataclass
class RefineryOrder:
    """A single refinery work order."""

    id: str
    name: str  # user-editable, default: "{primary_commodity} @ {station}"
    station: str
    commodities: list[dict] = field(default_factory=list)  # [{name, scu}, ...]
    method: str = ""
    cost: float = 0.0
    processing_seconds: int = 0
    submitted_at: str = ""  # ISO 8601
    expected_completion: str = ""  # ISO 8601
    status: str = "in_process"  # "in_process" | "complete" | "picked_up"
    completed_at: str | None = None
    picked_up_at: str | None = None
    log_event_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> RefineryOrder:
        data.setdefault("commodities", [])
        data.setdefault("method", "")
        data.setdefault("cost", 0.0)
        data.setdefault("processing_seconds", 0)
        data.setdefault("submitted_at", "")
        data.setdefault("expected_completion", "")
        data.setdefault("status", "in_process")
        data.setdefault("completed_at", None)
        data.setdefault("picked_up_at", None)
        data.setdefault("log_event_id", None)
        return cls(**data)

    def time_remaining_seconds(self) -> int:
        """Seconds remaining until expected completion. Clamped to 0."""
        if not self.expected_completion:
            return 0
        try:
            expected = datetime.fromisoformat(self.expected_completion)
            if expected.tzinfo is None:
                expected = expected.replace(tzinfo=timezone.utc)
            now = datetime.now(tz=timezone.utc)
            delta = (expected - now).total_seconds()
            return max(0, int(delta))
        except (ValueError, TypeError):
            return 0

    def time_remaining_str(self) -> str:
        """Human-readable time remaining."""
        secs = self.time_remaining_seconds()
        if self.status == "complete":
            return "Complete"
        if secs <= 0:
            return "Expected"
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        if h > 0:
            return f"{h}h {m:02d}m {s:02d}s"
        return f"{m}m {s:02d}s"

    def commodities_summary(self) -> str:
        """Short string: 'Corundum Q597 24 cSCU, Aluminum Q443 38 cSCU'."""
        parts = []
        for c in self.commodities:
            name = c.get("name", "?")
            scu = c.get("scu", 0)
            quality = c.get("quality", 0)
            if quality:
                parts.append(f"{name} Q{quality} {scu} cSCU")
            else:
                parts.append(f"{name} {scu} cSCU")
        return ", ".join(parts) if parts else "—"


def _make_order_id() -> str:
    return uuid.uuid4().hex[:12]


def _default_name(station: str, commodities: list[dict]) -> str:
    """Generate default order name from primary commodity + station."""
    if commodities:
        primary = commodities[0].get("name", "Unknown")
    else:
        primary = "Order"
    short_station = station.split(" ")[-1] if station else "?"
    return f"{primary} @ {short_station}"


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class RefineryOrderStore:
    """In-memory store for refinery orders with JSON config persistence."""

    def __init__(self, config: dict) -> None:
        self._orders: dict[str, RefineryOrder] = {}
        raw = config.get("refinery_orders", [])
        for entry in raw:
            try:
                order = RefineryOrder.from_dict(entry)
                self._orders[order.id] = order
            except (TypeError, KeyError) as exc:
                log.warning("Skipping malformed refinery order: %s", exc)

    def add_order(
        self,
        station: str,
        commodities: list[dict],
        method: str = "",
        cost: float = 0.0,
        processing_seconds: int = 0,
        name: str = "",
    ) -> RefineryOrder:
        """Create and store a new order. Returns the order.

        Dedup: if an order with the same station + commodities + method
        was added within the last 5 minutes, skip (return existing).
        Rejects orders where all commodities have zero quantities.
        """
        # Reject all-zero orders — false positives from OCR matching
        # mineral names in non-refinery UI text.
        if commodities and all(
            c.get("qty", 0) == 0 and c.get("scu", 0) == 0
            for c in commodities
        ):
            log.debug("refinery_orders: rejected all-zero order for %s", station)
            return None

        # Dedup check — prevents auto-scan from creating duplicates.
        # Matches on commodity fingerprint (names + qualities) which
        # uniquely identifies an order even when multiple orders share
        # the same mineral name but different quality levels.
        now = datetime.now(tz=timezone.utc)
        new_fingerprint = _commodities_fingerprint(commodities)

        for existing in self._orders.values():
            if existing.status != "in_process":
                continue

            existing_fingerprint = _commodities_fingerprint(existing.commodities)

            # Exact commodity fingerprint match (same minerals at same qualities)
            # This catches both auto-scan re-reads AND the player switching
            # between refining methods (commodities stay the same, but method,
            # cost, yields, and timer change).
            if new_fingerprint and new_fingerprint == existing_fingerprint:
                try:
                    sub = datetime.fromisoformat(existing.submitted_at)
                    if sub.tzinfo is None:
                        sub = sub.replace(tzinfo=timezone.utc)
                    if abs((now - sub).total_seconds()) < 7200:  # 2 hours
                        log.info("Dedup: same commodities — updating method/cost/timer")
                        if station:
                            existing.station = station
                        # Always update method, cost, commodities (yields change
                        # when the player picks a different refining method)
                        if method:
                            existing.method = method
                        if cost > 0:
                            existing.cost = cost
                        if commodities:
                            existing.commodities = commodities
                        if processing_seconds > 0:
                            new_expected = (now + timedelta(seconds=processing_seconds)).isoformat()
                            existing.expected_completion = new_expected
                            existing.processing_seconds = processing_seconds
                        return existing
                except (ValueError, TypeError):
                    pass

        submitted = _now_iso()
        if processing_seconds > 0:
            expected = (now + timedelta(seconds=processing_seconds)).isoformat()
        else:
            expected = ""

        order = RefineryOrder(
            id=_make_order_id(),
            name=name or _default_name(station, commodities),
            station=station,
            commodities=commodities,
            method=method,
            cost=cost,
            processing_seconds=processing_seconds,
            submitted_at=submitted,
            expected_completion=expected,
            status="in_process",
        )
        self._orders[order.id] = order
        return order

    def rename_order(self, order_id: str, new_name: str) -> bool:
        order = self._orders.get(order_id)
        if order and new_name.strip():
            order.name = new_name.strip()
            return True
        return False

    def complete_order(
        self, order_id: str, completed_at: str, log_event_id: str | None = None
    ) -> bool:
        order = self._orders.get(order_id)
        if not order:
            return False
        order.status = "complete"
        order.completed_at = completed_at
        order.log_event_id = log_event_id
        return True

    def pickup_order(self, order_id: str) -> bool:
        """Move a complete order to picked_up status."""
        order = self._orders.get(order_id)
        if not order or order.status != "complete":
            return False
        order.status = "picked_up"
        order.picked_up_at = _now_iso()
        return True

    def delete_order(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    def get_order(self, order_id: str) -> RefineryOrder | None:
        return self._orders.get(order_id)

    def get_in_process(self) -> list[RefineryOrder]:
        orders = [o for o in self._orders.values() if o.status == "in_process"]
        orders.sort(key=lambda o: o.expected_completion or o.submitted_at)
        return orders

    def get_complete(self) -> list[RefineryOrder]:
        orders = [o for o in self._orders.values() if o.status == "complete"]
        orders.sort(
            key=lambda o: o.completed_at or o.submitted_at, reverse=True
        )
        return orders

    def get_picked_up(self) -> list[RefineryOrder]:
        orders = [o for o in self._orders.values() if o.status == "picked_up"]
        orders.sort(
            key=lambda o: o.picked_up_at or o.completed_at or "", reverse=True
        )
        return orders

    def to_config_list(self) -> list[dict]:
        """Serialize all orders for JSON persistence."""
        return [o.to_dict() for o in self._orders.values()]

    def add_log_only_completion(
        self, log_event: dict
    ) -> RefineryOrder:
        """Create a complete order from a log event with no OCR data."""
        order = RefineryOrder(
            id=log_event["id"],
            name=f"Order @ {log_event['location']}",
            station=log_event["location"],
            status="complete",
            completed_at=log_event["timestamp"],
            log_event_id=log_event["id"],
        )
        self._orders[order.id] = order
        return order


def _commodities_fingerprint(commodities: list[dict]) -> str:
    """Create a fingerprint string from commodities including qualities.

    Two orders with the same minerals at different quality levels will
    produce different fingerprints.
    Example: "corundum:597,corundum:726,aluminum:443"
    """
    parts = []
    for c in commodities:
        name = c.get("name", "").lower()
        quality = c.get("quality", 0)
        parts.append(f"{name}:{quality}")
    return ",".join(sorted(parts))


def _commodities_match(a: list[dict], b: list[dict]) -> bool:
    """Check if two commodity lists refer to the same order.

    Compares mineral names AND quality levels to distinguish orders
    of the same commodity at different qualities.
    """
    return _commodities_fingerprint(a) == _commodities_fingerprint(b)


def match_log_completion(
    store: RefineryOrderStore, log_event: dict
) -> list[str]:
    """Match a log completion event to in-process orders.

    The game log only provides station + timestamp + count. It does NOT
    include commodity details. So matching relies on:
      1. Station name match
      2. Expected completion time proximity (30-min window)
      3. When multiple orders match, prefer the one whose expected
         completion is closest to the log timestamp

    Returns list of matched order IDs (may be empty).
    """
    location = log_event.get("location", "").strip().lower()
    count = log_event.get("count", 1)
    log_ts_str = log_event.get("timestamp", "")

    try:
        log_ts = datetime.fromisoformat(log_ts_str)
        if log_ts.tzinfo is None:
            log_ts = log_ts.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return []

    # Find candidates at the same station
    candidates = []
    for order in store.get_in_process():
        order_station = order.station.strip().lower()
        # Match station — try exact and partial (log may use short name)
        if order_station != location and location not in order_station:
            continue
        if not order.expected_completion:
            candidates.append((3600, order))
            continue
        try:
            expected = datetime.fromisoformat(order.expected_completion)
            if expected.tzinfo is None:
                expected = expected.replace(tzinfo=timezone.utc)
            delta = abs((log_ts - expected).total_seconds())
            if delta < 1800:  # 30-minute window
                candidates.append((delta, order))
        except (ValueError, TypeError):
            candidates.append((3600, order))

    # Sort by closest expected completion time
    candidates.sort(key=lambda c: c[0])

    # Match up to `count` orders
    matched_ids = []
    for _, order in candidates[:count]:
        matched_ids.append(order.id)

    return matched_ids
