"""contract_history.py - what kind of work the pilot does, remembered across sessions (J 2026-09-25, April spec 5).

The April design fed contract types into personality SUBCLASSES. SuitMk2 has no subclasses (its feelings decay and
are fed by events, emotion.py), so this keeps the history and lets it touch two things, both small and testable:
  * COMMENTARY: a finished contract's line carries its type ("bounty") and, at a milestone, the count: the first of a
    new kind, every 10th of one kind, and the 10th/25th/50th/100th overall (the April milestone table).
  * FEELING: finishing the pilot's SPECIALTY (their most-completed kind, SPECIALTY_MIN done and SPECIALTY_SHARE of all)
    scales Elah's pride and Montaigne's joy by SPECIALTY_AFFECT_SCALE; the first job of a new kind scales it by
    NEW_KIND_AFFECT_SCALE (novelty). Nothing else changes.

WHAT THE LOG GIVES (measured 2026-09-25 over all of J's Game.log backups): NO contract type field. Only the HUD title
("Contract Accepted: Verified Bounty: Harry Batey | ...", "Contract Complete: Claim #30040: Crusader M2 Hercules
Starlifter Salvage Rights") and a MissionId. So the type is a keyword table over the TITLE, ordered so "Thwart
Cargo-jacking" is combat, not cargo. Measured on J's logs: 234 of his 241 distinct accepted/completed/failed titles,
98.4% of 2,130 such notices, get a type; the other 7 ("Hot Shot", "Snow Snipe", "Vanduul-Tech Smugglers", ...) stay
"other" rather than guessed.
Outcomes: complete and failed come from the HUD lines; ABANDONED from
    <EndMission> Ending mission for player. MissionId[..] ... CompletionType[Abandon] Reason[Mission Ended]
joined to the accepted title by MissionId. CompletionType[Abandon] Reason[Player left] (150 of J's) is leaving the
server with a contract open, not abandoning it, and is not counted.

History is stored as raw events in the pilot's memory store (callbacks, kind "contract"), like ship_feelings, and
tallied at load.
    python contract_history.py --selftest
"""
from __future__ import annotations

import logging
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
log = logging.getLogger("suitmk2.contracts")

KIND = "contract"
SPECIALTY_MIN = 5              # a specialty needs at least this many of one kind completed ...
SPECIALTY_SHARE = 0.4          # ... and this share of every completed contract
SPECIALTY_AFFECT_SCALE = 1.3   # finishing the specialty: a little more pride (Elah) and joy (Montaigne)
NEW_KIND_AFFECT_SCALE = 1.2    # the first of a new kind
TYPE_MILESTONE_EVERY = 10      # every 10th of one kind
TOTAL_MILESTONES = (10, 25, 50, 100)

# (type, spoken, title keywords). ORDER MATTERS: first match wins.
TYPES: list[tuple[str, str, tuple]] = [
    ("salvage", "salvage", ("salvage", "clean up", "salvager")),
    ("bounty", "bounty", ("bounty", "neutralize", "neutralized", "terrorist", "preemptive strike", "precision strike",
                          "track down", "defense contract", "elimination", "tracker")),
    ("mining", "mining", ("mined ore", "mined materials", "purchase order")),
    ("rescue", "rescue", ("rescue", "ship in trouble", "ship in distress", "operative extraction")),
    # before combat: "Jorrit Dossier: Updated Security Data" is a data run, not a fight ("security" is a combat word)
    ("investigation", "investigation", ("dossier", "intel", "blackbox", "missing person")),
    ("combat", "combat", ("xenothreat", "hauler hunters", "thwart", "raiders", "supply thief", "cargo heist",
                          "cargo theft", "protection detail", "security", "escort", "patrol", "retake", "defend",
                          "under attack", "boarding action", "combat gauntlet", "sweep and clear", "expel", "ambush",
                          "protect", "call to arms", "cull", "overpopulation", "eradication")),
    ("delivery", "delivery", ("courier", "delivery")),
    ("cargo", "cargo hauling", ("cargo", "haul", "shipment", "supply of", "materials order")),
]
SPOKEN = {t: s for t, s, _ in TYPES} | {"other": "other"}
_END = re.compile(r"<EndMission> Ending mission for player\. MissionId\[([^\]]+)\].*?CompletionType\[(\w+)\] "
                  r"Reason\[([^\]]*)\]")


def classify(title: str) -> str:
    low = str(title or "").lower()
    if not low or "~mission(" in low:
        return "other"
    for t, _, words in TYPES:
        if any(w in low for w in words):
            return t
    return "other"


class ContractHistory:
    def __init__(self, store=None, now=None):
        import time
        self.store, self.now = store, (now or time.time)
        self.done: Counter = Counter()           # type -> completed
        self.failed: Counter = Counter()
        self.abandoned: Counter = Counter()
        self._open: dict[str, str] = {}          # mission id -> title (this session)
        self._closed: dict[str, dict] = {}       # mission id -> its completion result (a repeat never counts twice)
        self._load()

    # -- persistence ------------------------------------------------------------------------------------------------
    def _load(self) -> None:
        if self.store is None:
            return
        try:
            import memory_store as ms
            for cb in ms.recent_callbacks(self.store, n=50000):
                m = cb.get("meta") or {}
                if cb.get("kind") == KIND and m.get("type"):
                    self._count(m["type"], m.get("outcome"))
        except Exception:
            log.exception("contract history load")

    def _count(self, t: str, outcome: Optional[str]) -> None:
        {"complete": self.done, "failed": self.failed, "abandoned": self.abandoned}.get(outcome, Counter())[t] += 1

    def _record(self, t: str, outcome: str, title: str) -> None:
        self._count(t, outcome)
        if self.store is None:
            return
        try:
            import memory_store as ms
            ms.record_callback(self.store, f"contract {outcome}: {title}", kind=KIND,
                               meta={"type": t, "outcome": outcome, "title": title},
                               created=datetime.fromtimestamp(self.now(), timezone.utc).isoformat())
        except Exception:
            log.exception("contract history record")

    # -- queries ------------------------------------------------------------------------------------------------------
    def total(self) -> int:
        return sum(self.done.values())

    def specialty(self) -> Optional[str]:
        """The pilot's most-completed kind, if it is clearly theirs (never "other")."""
        ranked = [(n, t) for t, n in self.done.items() if t != "other"]
        if not ranked:
            return None
        n, t = max(ranked)
        return t if n >= SPECIALTY_MIN and n >= SPECIALTY_SHARE * self.total() else None

    # -- inputs -------------------------------------------------------------------------------------------------------
    def accepted(self, mission_id: Optional[str], title: str) -> str:
        t = classify(title)
        if mission_id:
            self._open[mission_id] = title
        return t

    def completed(self, mission_id: Optional[str], title: str) -> dict:
        """-> {type, spoken, of_type, total, milestone, specialty, affect_scale}. milestone is None or one of
        "first_of_type", "type_count", "total"."""
        if mission_id and mission_id in self._closed:
            return dict(self._closed[mission_id], milestone=None)
        title = title or self._open.get(mission_id or "", "")
        self._open.pop(mission_id or "", None)
        t = classify(title)
        spec_before = self.specialty()
        new_kind = t != "other" and self.done[t] == 0 and self.total() > 0
        self._record(t, "complete", title)
        n, total = self.done[t], self.total()
        milestone = None
        if t != "other" and n == 1 and total > 1:
            milestone = "first_of_type"
        elif t != "other" and n % TYPE_MILESTONE_EVERY == 0:
            milestone = "type_count"
        elif total in TOTAL_MILESTONES:
            milestone = "total"
        scale = SPECIALTY_AFFECT_SCALE if t == (spec_before or self.specialty()) and t != "other" else \
            (NEW_KIND_AFFECT_SCALE if new_kind else 1.0)
        out = {"type": t, "spoken": SPOKEN[t], "of_type": n, "total": total, "milestone": milestone,
               "specialty": self.specialty(), "affect_scale": scale}
        if mission_id:
            self._closed[mission_id] = out
        return out

    def failure(self, mission_id: Optional[str], title: str) -> str:
        title = title or self._open.pop(mission_id or "", "")
        t = classify(title)
        self._record(t, "failed", title)
        return t

    def on_line(self, line: str) -> Optional[str]:
        """An <EndMission> Abandon (Reason Mission Ended) for a contract accepted this session -> its type, recorded."""
        if "<EndMission> Ending mission" not in line:
            return None
        m = _END.search(line)
        if not m or m.group(2) != "Abandon" or m.group(3) != "Mission Ended" or m.group(1) not in self._open:
            return None
        title = self._open.pop(m.group(1))
        t = classify(title)
        self._record(t, "abandoned", title)
        return t


# Real titles from J's HUD lines (scan of all his backups, 2026-09-25), with the type each must get.
TITLE_FIXTURES = {
    "Verified Bounty: Harry Batey | Extreme-Risk Target (Sub-Capital Class Vessel, Heavy Support)": "bounty",
    "Claim #30040: Crusader M2 Hercules Starlifter Salvage Rights": "salvage",
    "RSI Constellation Andromeda clean up": "salvage",
    "Alliance Aid: Thwart Cargo-jacking": "combat",
    "Alliance Aid: Hauler Hunters": "combat",
    "Eliminate XenoThreat Enforcer": "combat",
    "Retake Platforms From Nine Tails": "combat",
    "Rookie Rank - Direct Extra Small Cargo Haul": "cargo",
    "Orison Relief: Medium Materials Order": "cargo",
    "Alliance Aid: Small Supply of Research Resource": "cargo",
    "Bulk Covalex Shipment Needs Recovering": "cargo",
    "XS Purchase Order: Ship Mined Ore": "mining",
    "Small Purchase Order: Hand Mined Materials": "mining",
    "Alliance Aid: Ship in Trouble": "rescue",
    "Operative Extraction": "rescue",
    "Jorrit Dossier: Project Hyperion": "investigation",
    "Retrieve Additional Smuggler Intel": "investigation",
    "Locate Missing Person: Kevin Chalk": "investigation",
    "Jorrit Dossier: Updated Security Data": "investigation",
    "COURIER NEEDED FOR RETRIEVAL - EXP: U": "delivery",
    "Luminalia Present Delivery": "delivery",
    "Neutralize UEE Threat: Gundeep": "bounty",
    "Tracker License Certification": "bounty",
    "Vanduul-Tech Smugglers": "other",
    "Verified Bounty: ~mission(TargetName) | ~mission(Danger)": "other",
}
# J's log "Game Build(10967244) 18 Dec 25 (20 11 47).log" and "Game Build(10989003) 28 Dec 25 (18 20 33).log".
FIXTURE_BOUNTY_DONE = ('<2025-12-19T02:27:15.983Z> [Notice] <SHUDEvent_OnNotification> Added notification "Contract '
                       'Complete: Verified Bounty: Harry Batey | Extreme-Risk Target (Sub-Capital Class Vessel, Heavy '
                       'Support): " [116] to queue. New queue size: 2, MissionId: [17c8e765-b229-4fe6-a68a-9f272ceb6588], '
                       'ObjectiveId: [] [Team_CoreGameplayFeatures][Missions][Comms]')
FIXTURE_ACCEPT = ('<2025-12-29T03:40:21.231Z> [Notice] <SHUDEvent_OnNotification> Added notification "Contract Accepted:  '
                  'Luminalia Present Delivery: " [190] to queue. New queue size: 1, MissionId: '
                  '[2122035b-385e-446e-982e-6f589a6ff0ee], ObjectiveId: [] [Team_CoreGameplayFeatures][Missions][Comms]')
FIXTURE_ABANDON = ('<2025-12-29T03:41:29.113Z> [Notice] <EndMission> Ending mission for player. '
                   'MissionId[2122035b-385e-446e-982e-6f589a6ff0ee] Player[ProjectGegnome] PlayerId[201926433820] '
                   'CompletionType[Abandon] Reason[Mission Ended] [Team_MissionFeatures][Missions]')


def _selftest() -> int:
    import tempfile
    res = []

    def case(name, cond, detail=""):
        res.append((name + (f"  [{detail}]" if detail and not cond else ""), bool(cond)))
    for title, want in TITLE_FIXTURES.items():
        case(f"classify {title[:44]!r} -> {want}", classify(title) == want, classify(title))
    from event_parser import EventParser
    evs = []
    p = EventParser()
    p.subscribe(evs.append)
    for ln in (FIXTURE_BOUNTY_DONE, FIXTURE_ACCEPT):
        p.on_raw_line(ln)
    case("the real HUD lines parse with their title and MissionId",
         [e.event_type for e in evs] == ["contract_complete", "contract_accepted"]
         and evs[0].data.get("mission_id") == "17c8e765-b229-4fe6-a68a-9f272ceb6588"
         and classify(evs[0].data.get("mission_name")) == "bounty")
    with tempfile.TemporaryDirectory() as d:
        import memory_store as ms
        store = ms.open_store(d, "pilot")
        h = ContractHistory(store)
        r = h.completed("a", "Rookie Rank - Direct Small Cargo Haul")
        case("the very first contract: no 'first of a kind' fuss", r["milestone"] is None and r["total"] == 1)
        r = h.completed("b", "Verified Bounty: Someone")
        case("the first bounty after other work: first_of_type, a novelty lift",
             r["milestone"] == "first_of_type" and r["affect_scale"] == NEW_KIND_AFFECT_SCALE)
        for i in range(8):
            r = h.completed(f"c{i}", "Verified Bounty: Someone")
        case("nine bounties of ten contracts: bounty is the specialty", h.specialty() == "bounty")
        case("finishing the specialty lifts the feeling", r["affect_scale"] == SPECIALTY_AFFECT_SCALE)
        case("the tenth contract overall is a milestone", r["total"] == 10 and r["milestone"] == "total")
        r = h.completed("d", "Verified Bounty: Someone")
        case("the tenth BOUNTY is a milestone of its own", r["of_type"] == 10 and r["milestone"] == "type_count")
        case("the same MissionId completing twice counts once", h.completed("d", "Verified Bounty: Someone")["of_type"] == 10
             and h.done["bounty"] == 10)
        h.accepted("2122035b-385e-446e-982e-6f589a6ff0ee", "Luminalia Present Delivery")
        case("a real abandon (Reason Mission Ended) of an accepted contract is counted",
             h.on_line(FIXTURE_ABANDON) == "delivery" and h.abandoned["delivery"] == 1)
        h.accepted("x", "Luminalia Present Delivery")
        left = FIXTURE_ABANDON.replace("2122035b-385e-446e-982e-6f589a6ff0ee", "x").replace("Mission Ended", "Player left")
        case("leaving the server with a contract open is NOT an abandon", h.on_line(left) is None)
        h2 = ContractHistory(store)
        case("the history survives a restart (memory store)",
             h2.done["bounty"] == 10 and h2.done["cargo"] == 1 and h2.abandoned["delivery"] == 1)
    bad = [n for n, ok in res if not ok]
    for n in bad:
        print("  FAIL  " + n)
    print(f"contract_history selftest: {len(res) - len(bad)}/{len(res)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
