"""refinery_tracker.py - refinery orders that outlast a session (J 2026-09-25, April spec section 6).

WHAT THE LOG ACTUALLY RECORDS (measured 2026-09-25 over all 1,112 of J's Game.log backups, before designing):
  * NO SUBMISSION WITH A DURATION. The April spec keyed its timer on OnRefineryRequest; in 1,112 logs that line appears
    4 times, in 2 sessions (Sep 2025), always "request[] currencyType[UEC]" with an EMPTY request and followed by
    "Commodity Refinery Response Error ... result[InvalidQuantityError] type[Selling]". No ore, no duration, no
    finish time, and it was a sale. A countdown timer has nothing to count.
  * ONE REAL SIGNAL: the HUD notice
        "A Refinery Work Order has been Completed at HUR-L2 Faithful Dream Station: "
    (parsed as refinery_complete since 2026-09-24). 46 of them in the backups: 4 fired mid-session, hours after a
    login (a job finishing while the pilot played), and 42 fired 34-98 s after a {Join PU} (one at 498 s). The game
    RE-ANNOUNCES a finished, uncollected order at every login: HUR-L2 was announced on four consecutive logins,
    3-4 Apr 2026.
  * NO PICKUP LINE. Collecting the order is not logged.

SO THE DESIGN IS BUILT ON WHAT EXISTS, NOT ON A TIMER:
  * an order is "ready at <station>" from its first notice, persisted through the pilot's memory store (callbacks,
    kind "refinery": raw events, like ship_feelings), so it survives a restart and travels with an export;
  * a notice for a station that already has an open order is the game's re-announcement: said as "still waiting",
    with how long, not as news;
  * INFERRED PICKUP: a login that passes REANNOUNCE_GRACE_S with no re-announcement for an open order closes it. This
    is an inference from the 42 re-announcements, never a logged fact, so it is silent and only stops the reminders;
    if the station is announced again later, the order simply reopens;
  * THE REMINDER ("when a job is due" = when it can be collected): arriving at a station with an open order says so,
    once per session per station. If the reminder was just given, the re-announcement a few seconds later is not
    said again (the pilot usually logs in AT the station: location line, then the notice 8 s later).

    python refinery_tracker.py --selftest
"""
from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
log = logging.getLogger("suitmk2.refinery")

KIND = "refinery"
# The latest login re-announcement measured was 98 s after {Join PU} (one outlier at 498 s, likely a real completion).
REANNOUNCE_GRACE_S = 180.0
REMINDER_SUPPRESSES_NOTICE_S = 600.0     # a notice this soon after the pickup reminder for the same station: not said


def _key(station: str) -> str:
    return " ".join(str(station or "").lower().replace(" ", " ").split())


def same_place(a: str, b: str) -> bool:
    """Arrival names and notice names differ in length: location_change says "HUR-L2", the notice says "HUR-L2 Faithful
    Dream Station". Equal, or one contains the other and the shorter is at least 4 characters."""
    a, b = _key(a), _key(b)
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 4 and (long_.startswith(short + " ") or f" {short} " in f" {long_} ")


def _epoch(iso: str) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


class RefineryTracker:
    def __init__(self, store=None, now: Callable[[], float] = time.time):
        self.store, self.now = store, now
        self.orders: dict[str, dict] = {}          # key -> {station, first, last, count, open}
        self._joined_t: Optional[float] = None
        self._seen_since_join: set = set()
        self._reminded: dict[str, float] = {}      # key -> when the pickup reminder was given (this session)
        self._load()

    # -- persistence: raw events in the pilot's memory store, folded at load --------------------------------------
    def _record(self, station: str, what: str) -> None:
        if self.store is None:
            return
        try:
            import memory_store as ms
            created = datetime.fromtimestamp(self.now(), timezone.utc).isoformat()
            ms.record_callback(self.store, f"refinery {what} at {station}", kind=KIND,
                               meta={"station": station, "what": what}, created=created)
        except Exception:
            log.exception("refinery record")

    def _load(self) -> None:
        if self.store is None:
            return
        try:
            import memory_store as ms
            rows = [cb for cb in ms.recent_callbacks(self.store, n=20000) if cb.get("kind") == KIND]
        except Exception:
            log.exception("refinery load")
            return
        for cb in sorted(rows, key=lambda r: r.get("created", "")):
            m, t = cb.get("meta") or {}, _epoch(cb.get("created", ""))
            if t is None or not m.get("station"):
                continue
            self._apply(m["station"], m.get("what"), t)

    def _apply(self, station: str, what: str, t: float) -> dict:
        k = _key(station)
        o = self.orders.get(k)
        if what == "ready":
            if o is None or not o["open"]:
                o = self.orders[k] = {"station": station, "first": t, "last": t, "count": 0, "open": True}
            o["last"], o["count"] = t, o["count"] + 1
        elif what == "gone" and o is not None:
            o["open"] = False
        return o or {}

    # -- inputs ------------------------------------------------------------------------------------------------------
    def joined(self) -> None:
        """{Join PU}: a login (or a server change). Re-announcements are expected within REANNOUNCE_GRACE_S."""
        self.sweep()
        self._joined_t, self._seen_since_join = self.now(), set()

    def notice(self, station: str) -> Optional[dict]:
        """The HUD notice. -> {station, known, days_waiting, at_login, suppressed}, or None for no station."""
        if not str(station or "").strip():
            return None
        k, now = _key(station), self.now()
        before = self.orders.get(k)
        known = bool(before and before["open"])
        first = before["first"] if known else now
        self._apply(station, "ready", now)
        self._record(station, "ready")
        self._seen_since_join.add(k)
        at_login = self._joined_t is not None and now - self._joined_t <= REANNOUNCE_GRACE_S
        rem = self._reminded.get(k)
        return {"station": station, "known": known, "days_waiting": int((now - first) // 86400),
                "at_login": at_login, "suppressed": rem is not None and now - rem < REMINDER_SUPPRESSES_NOTICE_S}

    def sweep(self) -> list[str]:
        """Close every open order a finished login grace window did NOT re-announce (inferred pickup). -> stations."""
        if self._joined_t is None or self.now() - self._joined_t <= REANNOUNCE_GRACE_S:
            return []
        closed = []
        for k, o in self.orders.items():
            if o["open"] and k not in self._seen_since_join and o["last"] < self._joined_t:
                o["open"] = False
                self._record(o["station"], "gone")
                closed.append(o["station"])
        self._joined_t = None                        # judged once per login
        return closed

    def arrived(self, place: str) -> Optional[dict]:
        """Arriving somewhere. -> the open order waiting HERE (once per session per station), else None."""
        if not str(place or "").strip():
            return None
        for k, o in self.orders.items():
            if o["open"] and k not in self._reminded and same_place(place, o["station"]):
                self._reminded[k] = self.now()
                return {"station": o["station"], "days_waiting": int((self.now() - o["first"]) // 86400)}
        return None

    def ready(self) -> list[str]:
        return [o["station"] for o in self.orders.values() if o["open"]]


# Verbatim lines, J's log "Game Build(11545720) 03 Apr 26 (00 47 48).log" (tails trimmed).
FIXTURE_JOIN = "<2026-04-03T04:48:23.527Z> [+] [CIG] {Join PU} [0] id[c1391066-71ab-4a55-b763-1eb0c052792a] status[1] port[64344]"
FIXTURE_AT_STATION = ("<2026-04-03T04:49:13.182Z> [Notice] <RequestLocationInventory> Player[ProjectGegnome] requested "
                      "inventory for Location[RR_HUR_L2] [Team_CoreGameplayFeatures][Inventory]")
FIXTURE_NOTICE = ('<2026-04-03T04:49:21.009Z> [Notice] <SHUDEvent_OnNotification> Added notification "A Refinery Work '
                  'Order has been Completed at HUR-L2 Faithful Dream Station: " [3] to queue. New queue size: 3, '
                  'MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
                  '[Team_CoreGameplayFeatures][Missions][Comms]')


def _selftest() -> int:
    import tempfile
    res = []

    def case(name, cond):
        res.append((name, bool(cond)))
    case("HUR-L2 is the station in the notice", same_place("HUR-L2", "HUR-L2 Faithful Dream Station"))
    case("Levski matches Levski", same_place("Levski", "levski"))
    case("HUR-L1 is NOT HUR-L2", not same_place("HUR-L1", "HUR-L2 Faithful Dream Station"))
    case("a short fragment never matches", not same_place("L2", "HUR-L2 Faithful Dream Station"))
    from event_parser import EventParser
    evs = []
    p = EventParser()
    p.subscribe(evs.append)
    p.on_raw_line(FIXTURE_NOTICE)
    case("the real notice parses to refinery_complete at the station",
         evs and evs[0].event_type == "refinery_complete" and evs[0].data.get("location") == "HUR-L2 Faithful Dream Station")

    clock = [1_000_000.0]
    with tempfile.TemporaryDirectory() as d:
        import memory_store as ms
        store = ms.open_store(d, "pilot")
        t = RefineryTracker(store, now=lambda: clock[0])
        n1 = t.notice("HUR-L2 Faithful Dream Station")
        case("first notice: news, not known", n1 and not n1["known"])
        clock[0] += 86400 * 2 + 60
        t2 = RefineryTracker(store, now=lambda: clock[0])                 # a NEW session, same pilot memory
        case("next session remembers the open order", t2.ready() == ["HUR-L2 Faithful Dream Station"])
        t2.joined()
        clock[0] += 50
        r = t2.arrived("HUR-L2")
        case("arriving at the station: reminder, with days waiting", r and r["days_waiting"] == 2)
        case("...only once per session", t2.arrived("HUR-L2") is None)
        clock[0] += 8
        n2 = t2.notice("HUR-L2 Faithful Dream Station")
        case("login re-announcement: known, at login, 2 days", n2["known"] and n2["at_login"] and n2["days_waiting"] == 2)
        case("...and suppressed: the reminder was just said", n2["suppressed"])
        clock[0] += 86400
        t3 = RefineryTracker(store, now=lambda: clock[0])
        t3.joined()
        clock[0] += REANNOUNCE_GRACE_S + 1
        case("a login with no re-announcement closes it (inferred pickup)",
             t3.sweep() == ["HUR-L2 Faithful Dream Station"] and t3.ready() == [])
        t4 = RefineryTracker(store, now=lambda: clock[0])
        case("...and the closure persists", t4.ready() == [])
        n3 = t4.notice("HUR-L2 Faithful Dream Station")
        case("a later notice reopens it as news", n3 and not n3["known"] and t4.ready())
    t5 = RefineryTracker(None, now=lambda: clock[0])
    t5.joined()
    clock[0] += REANNOUNCE_GRACE_S - 30
    t5.notice("Levski")
    clock[0] += 60
    case("an order announced inside the window is not closed", t5.sweep() == [] and t5.ready() == ["Levski"])
    from event_spec import build_event_spec
    from grounding_validator import ground
    s = build_event_spec("refinery_complete", {"location": "Levski", "refinery_known": True, "refinery_days_waiting": 2})
    preds = {c["predicate"]: c["value"] for c in (s or {}).get("claims", [])}
    case("a re-announced order is a 'still waiting' line with its days",
         preds.get("refinery.still_waiting") is True and preds.get("refinery.days_waiting") == 2)
    case("grounding keeps the day count honest", not ground(s, "Your order at Levski is still waiting, two days now.")
         and any("unauthorized" in f or "missing" in f for f in ground(s, "Your order at Levski has waited three days.")))
    s2 = build_event_spec("refinery_pickup", {"location": "HUR-L2 Faithful Dream Station", "days_waiting": 0})
    case("a same-day pickup reminder carries no day count", s2 and "refinery.days_waiting" not in
         {c["predicate"] for c in s2["claims"]})
    bad = [n for n, ok in res if not ok]
    for n in bad:
        print("  FAIL  " + n)
    print(f"refinery_tracker selftest: {len(res) - len(bad)}/{len(res)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
