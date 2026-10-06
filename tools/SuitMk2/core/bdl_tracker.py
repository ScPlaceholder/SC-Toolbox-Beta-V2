"""bdl_tracker.py - a SIMULATED blood drug level from med pen use (April spec section 2).

The game has a BDL (the med bed's own notice says so: "Medical Bed: The bed has restored your health and reset your
BDL", 3,211 times in real logs) but never writes its value. This module ESTIMATES it from what the log does show, so
Elah can say "that's a lot of stims" before the pilot overdoses. It is directionally right at best:

    EVERY NUMBER BELOW IS AN ESTIMATE from the April 2026 design spec ("based on community testing ... not a precise
    medical readout"). Nothing here is measured from the game. They are tunables, named as such. The spoken warning
    therefore NEVER states a level or a threshold: it names only what the log did show (how many pens in the last few
    minutes) and a hedged, qualitative load ("a lot of stims"). A precise number the game never showed would be an
    invented fact with a confident voice.

CONSUMPTION vs HOLSTER (verified on 1,112 real Game.log backups):
    <AttachmentReceived> Player[..] Attachment[crlf_consumable_healing_01_<id>, crlf_consumable_healing_01, <id>] ...
        Port[weapon_attach_hand_right]      the pen is in the hand (drawn)
  Of 2,888 draws: 1,611 times ANOTHER item took the hand and the pen's entity id never appeared again (consumed);
  1,058 times the same id came back to a pen slot or the hand within 120 s (holstered, median 2.9 s); 61 times
  nothing else happened (usually a death or a logout). A consumed pen is never logged as such; it is the ABSENCE of
  its id that says so, which is why confirmation waits SWAP_CONFIRM_S after the swap.
  THE ABSENCE IS NOT PROOF. Holstering is normally logged (the pen lands on medPen_attach_N, a knife on
  utility_attach_2, a rifle on wep_stocked_2), but of the pens swapped away without such a line, 823 of 2,800 (29%)
  were drawn AGAIN later (median 20 s, 90th percentile 267 s): the game stowed them without a log line. So a dose is
  counted when the swap is confirmed (a warning that waits minutes is useless) and RETRACTED if that pen's id ever
  appears again. A warning already spoken cannot be unsaid, which is one more reason it is worded as an estimate.
  Known blind spot: a pen DROPPED on the floor also vanishes, and reads as a dose.
  The med gun is not tracked: its shots are not logged (only the gun and its vials attaching), so the spec's
  "+10 per med gun shot" has no signal to count.

RESETS: the med bed notice above (the game's own words, builds to May 2026; current builds log only
"<MED BED HEAL> ... Perform surgery event Success", which the core's med_bed_heal event resets on) and a death or
respawn (the core calls reset()).

GROUND TRUTH, RARE: the game itself sometimes shows a BDL hint ("Blood Drug Level (BDL): An elevated Blood Drug
Level ..." 8 times in real logs, "Overdose: A high Blood Drug Level ..." twice, Mar-May 2026). Both are taken as a
floor under the estimate. The one session with both (30 Mar 2026) is the selftest's fixture: 16 pens in about two
minutes, the game's "elevated" at 06:38:07, "Overdose" at 06:38:41, and the pilot down at 06:38:58. With the April
constants the estimate reaches caution 4 s after the game's "elevated" and critical 20 s BEFORE its "Overdose".
Over 1,003 recorded sessions the estimate crossed a band in only a handful: the warning is rare by construction.

    python bdl_tracker.py --selftest
"""
from __future__ import annotations

import re
import sys
from collections import deque
from datetime import datetime, timezone
from typing import Optional

# ---- TUNABLES: April-spec ESTIMATES, not game data. Change freely after play-testing. -----------------------------
BDL_PER_MEDPEN = 20.0          # estimate: one CureLife med pen (crlf_consumable_healing_01)
BDL_DECAY_PER_S = 1.0          # estimate: passive reduction per second
BDL_CAUTION = 60.0             # estimate: "a lot of stims"
BDL_DANGER = 80.0              # estimate: "close to too many"
BDL_CRITICAL = 100.0           # estimate: overdose territory
BDL_CLEAR_BELOW = 60.0         # announce "coming back down" once after being at or above CAUTION
CONSUME_WINDOW_S = 30.0        # a drawn pen whose id never reappears within this long counts as used
SWAP_CONFIRM_S = 3.0           # ...or this long after another item takes the hand (holsters took a median 2.9 s)
RECENT_DOSES_S = 300.0         # "N pens in the last few minutes": the one number a warning may say (it was logged)
BAND_REPEAT_S = 90.0           # a band already warned about is not warned again this soon (a band RISE always is)

PEN_CLASS = "crlf_consumable_healing"
HAND = "weapon_attach_hand_right"
_ATTACH = re.compile(r"<AttachmentReceived> Player\[([^\]]+)\] Attachment\[[^,\]]+, ([^,\]]+), (\d+)\].*?Port\[([^\]]+)\]"
                     r"(?:\s*Elapsed\[([\d.]+)\])?")
# ~0 s elapsed is a whole loadout attaching at spawn or at a terminal, not a hand movement (the same rule the core
# applies to holsters; measured on real logs: 0.00004-0.0006 s). A "draw" at Elapsed 0.00003 made a phantom burst.
MIN_ELAPSED_S = 1.0
_TS = re.compile(r"^<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z?>")

# Bands, lowest first: (name, threshold, the qualitative load a line may say). Words, never numbers.
BANDS = (("caution", BDL_CAUTION, "a lot of stims"),
         ("danger", BDL_DANGER, "too many stims, close together"),
         ("critical", BDL_CRITICAL, "far too many stims"))


def _ts(line: str) -> Optional[float]:
    m = _TS.match(line)
    if not m:
        return None
    s = m.group(1)
    if "." in s:
        base, frac = s.split(".")
        s = f"{base}.{frac[:6].ljust(6, '0')}"
    try:
        return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


class BdlTracker:
    def __init__(self):
        self.level = 0.0
        self._t: Optional[float] = None          # log time the level refers to
        self._pending: dict[str, dict] = {}      # entity id -> {"t": drawn at, "other_t": another item in hand at}
        self._doses: deque = deque(maxlen=32)
        self._elevated = False
        self._band_said: Optional[str] = None
        self._used: dict[str, float] = {}        # pen id -> when it was counted as a dose (for retraction)
        self._band_t = -1e18
        self.retracted = 0

    # -- time -------------------------------------------------------------------------------------------------------
    def _advance(self, t: float) -> None:
        if self._t is not None and t > self._t:
            self.level = max(0.0, self.level - BDL_DECAY_PER_S * (t - self._t))
        self._t = t if self._t is None else max(self._t, t)

    def band(self) -> Optional[tuple]:
        hit = None
        for b in BANDS:
            if self.level >= b[1]:
                hit = b
        return hit

    # -- inputs -----------------------------------------------------------------------------------------------------
    def reset(self, why: str = "") -> None:
        self.level, self._pending, self._elevated, self._band_said = 0.0, {}, False, None
        self._doses.clear()
        self._used.clear()

    def on_line(self, line: str, local_name: Optional[str] = None) -> list[dict]:
        """One raw log line -> events: {"type": "dose", ...} when a pen is confirmed used and the load is in a band,
        {"type": "clear"} once when it falls back below BDL_CLEAR_BELOW. Most lines return [] after a substring test."""
        if "Medical Bed:" in line and "reset your BDL" in line:
            self.reset("med bed")
            return []
        floor = None
        if "SHUDEvent_OnNotification" in line:
            if '"Blood Drug Level (BDL): An elevated' in line:
                floor = BDL_CAUTION
            elif '"Overdose: A high Blood Drug Level' in line:
                floor = BDL_CRITICAL
        if floor is not None:
            t = _ts(line)
            if t is None:
                return []
            out = self._tick(t)
            self.level = max(self.level, floor)
            return out + self._band_event(t, source="game")
        attach = "<AttachmentReceived>" in line
        if not attach and not self._pending and not self._elevated:
            return []                                  # nothing in flight: no need to even read the clock
        t = _ts(line)
        if t is None:
            return []
        out = self._tick(t)
        if attach:
            m = _ATTACH.search(line)
            if m and (local_name is None or m.group(1) == local_name):
                if m.group(5) is not None and float(m.group(5)) < MIN_ELAPSED_S:
                    return out                             # a loadout attaching, not a hand movement
                out += self._attach(m.group(3), m.group(2), m.group(4), t)
        return out

    def _attach(self, eid: str, cls: str, port: str, t: float) -> list[dict]:
        if eid in self._used:                          # a pen counted as used has come back: it was stowed, not used
            used_t = self._used.pop(eid)
            self._advance(t)
            self.level = max(0.0, self.level - max(0.0, BDL_PER_MEDPEN - BDL_DECAY_PER_S * (t - used_t)))
            if used_t in self._doses:
                self._doses.remove(used_t)
            self.retracted += 1
        if eid in self._pending:                       # it came back: holstered (or drawn again)
            self._pending.pop(eid)
            if port == HAND and PEN_CLASS in cls:
                self._pending[eid] = {"t": t, "other_t": None}
            return []
        if port == HAND:
            for p in self._pending.values():               # ANOTHER entity took the hand, a second pen included
                p["other_t"] = p["other_t"] or t
            if PEN_CLASS in cls:
                self._pending[eid] = {"t": t, "other_t": None}
        return []

    def _tick(self, t: float) -> list[dict]:
        out = []
        for eid, p in sorted(self._pending.items(), key=lambda kv: kv[1]["t"]):
            swapped = p["other_t"] is not None and t - p["other_t"] >= SWAP_CONFIRM_S
            if swapped or t - p["t"] >= CONSUME_WINDOW_S:
                self._pending.pop(eid)
                used_t = p["other_t"] if swapped else p["t"] + CONSUME_WINDOW_S
                self._used[eid] = used_t
                out += self._dose(used_t)
        self._advance(t)
        if self._elevated and self.level < BDL_CLEAR_BELOW:
            self._elevated, self._band_said = False, None
            out.append({"type": "clear", "recent": self.recent(t)})
        return out

    def _dose(self, t: float) -> list[dict]:
        self._advance(t)
        self.level += BDL_PER_MEDPEN
        self._doses.append(t)
        return self._band_event(t)

    def _band_event(self, t: float, source: str = "estimate") -> list[dict]:
        """A warning when the load reaches a band: always on a RISE, a repeat of the same band only after
        BAND_REPEAT_S. source "game" = the game's own hint set the floor."""
        b = self.band()
        if b is None:
            return []
        self._elevated = True
        names = [x[0] for x in BANDS]
        rise = self._band_said is None or names.index(b[0]) > names.index(self._band_said)
        if not rise and t - self._band_t < BAND_REPEAT_S:
            return []
        self._band_said, self._band_t = b[0], t
        return [{"type": "dose", "band": b[0], "load": b[2], "recent": self.recent(t), "source": source}]

    def recent(self, t: Optional[float] = None) -> int:
        t = self._t if t is None else t
        return sum(1 for d in self._doses if t is not None and t - d <= RECENT_DOSES_S)


# Verbatim from a real log "Game Build(10007308) 03 Aug 25 (15 28 29).log": a pen drawn, then the sniper back in hand
# 3 s later, and the pen's id never seen again (a real dose).
FIXTURE_DRAW = ("<2025-08-03T21:08:41.272Z> [Notice] <AttachmentReceived> Player[ProjectGegnome] Attachment["
                "crlf_consumable_healing_01_5285776306102, crlf_consumable_healing_01, 5285776306102] Status[persistent] "
                "Port[weapon_attach_hand_right] Elapsed[88.299026] [Team_CoreGameplayFeatures][Inventory]")
FIXTURE_SWAP = ("<2025-08-03T21:08:44.407Z> [Notice] <AttachmentReceived> Player[ProjectGegnome] Attachment["
                "ksar_sniper_ballistic_01_5018848033179, ksar_sniper_ballistic_01, 5018848033179] Status[persistent] "
                "Port[weapon_attach_hand_right] Elapsed[91.433708] [Team_CoreGameplayFeatures][Inventory]")
FIXTURE_LATER = ("<2025-08-03T21:08:51.670Z> [Notice] <Actor Death> CActor::Kill: "
                 "'PU_Human_Enemy_GroundCombat_NPC_ASD_techie_5186307686114' [5186307686114] in zone 'pyro1' killed by "
                 "'ProjectGegnome' [201926433820]")
FIXTURE_MED_BED = ('<2026-04-03T05:00:00.000Z> [Notice] <SHUDEvent_OnNotification> Added notification "Medical Bed: The '
                   'bed has restored your health and reset your BDL. Use the Treatment tab to heal injuries (depending '
                   'on tier), and the Medication tab for dosage." [4] to queue.')     # text verbatim; timestamp made up


# A real log "Game Build(11518367) 30 Mar 26 (02 28 39).log", 06:37:33-06:38:39 UTC: every pen attachment and hand
# change, fields verbatim (time, class, entity id, port, Elapsed), line text rebuilt around them by _line().
MAR30 = [
    ('06:37:33.172', 'crlf_consumable_healing_01', '9741297568195', 'medPen_attach_1', '0.406509'),
    ('06:37:34.519', 'crlf_consumable_healing_01', '9741297568195', 'weapon_attach_hand_right', '1.752574'),
    ('06:37:34.929', 'crlf_consumable_healing_01', '9766015537152', 'oxyPen_attach_1', '2.162953'),
    ('06:37:36.372', 'crlf_consumable_healing_01', '9766015537676', 'oxyPen_attach_2', '3.606005'),
    ('06:37:39.378', 'crlf_consumable_healing_01', '9741297568462', 'weapon_attach_hand_right', '6.612183'),
    ('06:37:42.623', 'crlf_consumable_healing_01', '9766015537152', 'weapon_attach_hand_right', '9.856925'),
    ('06:37:44.819', 'crlf_consumable_healing_01', '9741297568462', 'weapon_attach_hand_right', '12.052831'),
    ('06:37:45.479', 'crlf_consumable_healing_01', '9766015537676', 'weapon_attach_hand_right', '12.712909'),
    ('06:37:46.199', 'crlf_consumable_healing_01', '9766015537152', 'weapon_attach_hand_right', '13.432304'),
    ('06:37:48.474', 'crlf_consumable_healing_01', '9766015537676', 'oxyPen_attach_2', '15.707057'),
    ('06:37:49.015', 'crlf_consumable_healing_01', '9766015537676', 'weapon_attach_hand_right', '16.248337'),
    ('06:38:00.504', 'Default', '5080', 'weapon_attach_hand_right', '1.178635'),
    ('06:38:00.646', 'crlf_consumable_healing_01', '9766015542917', 'medPen_attach_1', '1.321440'),
    ('06:38:01.378', 'crlf_consumable_healing_01', '9766015543024', 'medPen_attach_2', '2.052330'),
    ('06:38:02.028', 'crlf_consumable_healing_01', '9766015542917', 'weapon_attach_hand_right', '2.702165'),
    ('06:38:02.663', 'crlf_consumable_healing_01', '9766015543119', 'oxyPen_attach_1', '3.337289'),
    ('06:38:04.973', 'crlf_consumable_healing_01', '9766015543158', 'oxyPen_attach_2', '5.648391'),
    ('06:38:05.076', 'crlf_consumable_healing_01', '9766015543024', 'weapon_attach_hand_right', '5.750649'),
    ('06:38:07.877', 'crlf_consumable_healing_01', '9766015543024', 'weapon_attach_hand_right', '8.551274'),
    ('06:38:08.064', 'crlf_consumable_healing_01', '9766015543119', 'weapon_attach_hand_right', '8.738421'),
    ('06:38:11.073', 'crlf_consumable_healing_01', '9766015543119', 'oxyPen_attach_1', '11.748145'),
    ('06:38:12.109', 'crlf_consumable_healing_01', '9766015543119', 'weapon_attach_hand_right', '12.783645'),
    ('06:38:16.900', 'crlf_consumable_healing_01', '9766015543158', 'weapon_attach_hand_right', '17.574511'),
    ('06:38:21.688', 'crlf_consumable_healing_01', '9766015543158', 'weapon_attach_hand_right', '22.362921'),
    ('06:38:31.909', 'crlf_consumable_healing_01', '9766015552350', 'medPen_attach_1', '1.018242'),
    ('06:38:32.830', 'crlf_consumable_healing_01', '9766015552350', 'weapon_attach_hand_right', '1.938797'),
    ('06:38:33.401', 'crlf_consumable_healing_01', '9766015553564', 'medPen_attach_2', '2.509534'),
    ('06:38:34.976', 'crlf_consumable_healing_01', '9766015553744', 'oxyPen_attach_1', '4.084795'),
    ('06:38:35.595', 'crlf_consumable_healing_01', '9766015553564', 'weapon_attach_hand_right', '4.703789'),
    ('06:38:36.528', 'crlf_consumable_healing_01', '9766015553823', 'oxyPen_attach_2', '5.637296'),
    ('06:38:39.209', 'crlf_consumable_healing_01', '9766015553744', 'weapon_attach_hand_right', '8.316910'),
]
MAR30_GAME_ELEVATED = ('<2026-03-30T06:38:07.458Z> [Notice] <SHUDEvent_OnNotification> Added notification "Blood Drug '
                       'Level (BDL): An elevated Blood Drug Level can cause feelings of intoxication, making getting '
                       'around or piloting a vehicle difficult and potentially dangerous." [11]')
MAR30_GAME_OVERDOSE_T = "06:38:41.121"      # "Overdose: A high Blood Drug Level (BDL) ..." ; down at 06:38:58


def _line(row: tuple) -> str:
    t, cls, eid, port, el = row
    return (f"<2026-03-30T{t}Z> [Notice] <AttachmentReceived> Player[ProjectGegnome] Attachment[{cls}_{eid}, {cls}, "
            f"{eid}] Status[persistent] Port[{port}] Elapsed[{el}] [Team_CoreGameplayFeatures][Inventory]")


def mar30_lines(with_game_notice: bool = False) -> list:
    out = [_line(r) for r in MAR30]
    if with_game_notice:
        out.append(MAR30_GAME_ELEVATED)
        out.sort(key=lambda ln: ln[1:25])
    return out + [f"<2026-03-30T{MAR30_GAME_OVERDOSE_T}Z> [Notice] tick"]


def burst(n: int, start: str = "2025-08-03T21:10:00", gap_s: float = 6.0, first_id: int = 5285776306200) -> list[str]:
    """n doses in the real line shapes above (draw, then the sniper back in hand 3 s later), gap_s apart. The SHAPES
    are real; the ids and timestamps are generated, since real logs never hold a burst fast enough to cross a band."""
    t0 = datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp()
    out = []
    for i in range(n):
        for dt, tmpl, eid in ((0.0, FIXTURE_DRAW, str(first_id + i)), (3.0, FIXTURE_SWAP, None)):
            ts = datetime.fromtimestamp(t0 + i * gap_s + dt, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
            line = "<" + ts + ">" + tmpl.split(">", 1)[1]
            out.append(line.replace("5285776306102", eid) if eid else line)
    ts = datetime.fromtimestamp(t0 + n * gap_s + 10, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out.append("<" + ts + "> [Notice] tick")
    return out


def _selftest() -> int:
    res = []

    def case(name, cond):
        res.append((name, bool(cond)))
    b = BdlTracker()
    evs = [e for ln in (FIXTURE_DRAW, FIXTURE_SWAP, FIXTURE_LATER) for e in b.on_line(ln, "ProjectGegnome")]
    case("J's real draw-then-swap is ONE dose", len(b._doses) == 1 and b.recent() == 1)
    case("...and one pen is below every band: silent", evs == [])
    case("the level decays with log time", 0 < b.level < BDL_PER_MEDPEN)
    m = BdlTracker()
    got = []
    for ln in mar30_lines():
        for e in m.on_line(ln, "ProjectGegnome"):
            got.append((ln[12:24], e))
    first = next((t for t, e in got if e["type"] == "dose"), None)
    crit = next((t for t, e in got if e["type"] == "dose" and e["band"] == "critical"), None)
    case("J's real 30 Mar overdose: the estimate warns within 10 s of the game's own 'elevated' hint (06:38:07)",
         first is not None and "06:38:00" <= first <= "06:38:17")
    case("...and reaches critical BEFORE the game's 'Overdose' notice (06:38:41)", crit is not None and crit < "06:38:41")
    case("...without repeating one band over and over", len([1 for _, e in got if e["type"] == "dose"]) <= 4)
    m2 = BdlTracker()
    got2 = [e for ln in mar30_lines(True) for e in m2.on_line(ln, "ProjectGegnome")]
    case("the game's own 'elevated' hint lifts the estimate to caution at once",
         got2 and got2[0]["source"] == "game" and got2[0]["band"] == "caution")
    g = BdlTracker()
    lines = burst(1)
    redraw = FIXTURE_DRAW.replace("21:08:41.272", "21:10:40.000").replace("5285776306102", "5285776306200")
    for ln in lines + [redraw]:
        g.on_line(ln, "ProjectGegnome")
    case("a pen counted as used that is drawn AGAIN is retracted (29% of swaps in J's logs)",
         g.retracted == 1 and g.recent() == 0)
    z = BdlTracker()
    z.on_line(FIXTURE_DRAW.replace("Elapsed[88.299026]", "Elapsed[0.000033]"), "ProjectGegnome")
    case("a pen landing in the hand at Elapsed ~0 is a loadout attaching, not a draw", not z._pending)
    h = BdlTracker()
    holster = FIXTURE_DRAW.replace("Port[weapon_attach_hand_right] Elapsed[88.299026]", "Port[medPen_attach_1] Elapsed[90.1]")
    holster = holster.replace("21:08:41.272", "21:08:43.100")
    for ln in (FIXTURE_DRAW, holster, FIXTURE_SWAP, FIXTURE_LATER):
        h.on_line(ln, "ProjectGegnome")
    case("drawn, then back in its slot: holstered, not a dose", len(h._doses) == 0)
    o = BdlTracker()
    o.on_line(FIXTURE_DRAW.replace("ProjectGegnome", "SomeoneElse"), "ProjectGegnome")
    case("another player's pen is not the pilot's dose", not o._pending)
    # With the April constants (+20 a pen, -1 a second) a band needs pens about as fast as a hand can cycle them: the
    # shortest real gaps between doses in real logs are 2.4-3.2 s. Over 1,003 recorded sessions the tracker crossed
    # a band 7 times (measured), so these warnings are rare by design of the estimates, not by a bug.
    w = BdlTracker()
    evs = [e for ln in burst(3, gap_s=6.0) for e in w.on_line(ln, "ProjectGegnome")]
    case("three pens six seconds apart stay below every band (estimate)", not [e for e in evs if e["type"] == "dose"])
    w = BdlTracker()
    evs = [e for ln in burst(6, gap_s=3.5) for e in w.on_line(ln, "ProjectGegnome")]
    doses = [e for e in evs if e["type"] == "dose"]
    case("pens 3.5 s apart: the 4th reaches caution (estimate)", doses and doses[0]["band"] == "caution"
         and doses[0]["recent"] == 4)
    case("...the 5th danger, the 6th critical", [e["band"] for e in doses] == ["caution", "danger", "critical"])
    case("a warning's load is words, never a number", all(not re.search(r"\d", e["load"]) for e in doses))
    later = "<2025-08-03T21:15:00.000Z> [Notice] <AttachmentReceived> Player[ProjectGegnome] Attachment[x_1, x, 1] Port[x]"
    evs = w.on_line(later, "ProjectGegnome")
    case("minutes later it falls back: ONE 'coming down' event", [e["type"] for e in evs] == ["clear"])
    case("...and not again", w.on_line(later.replace("15:00", "15:30"), "ProjectGegnome") == [])
    r = BdlTracker()
    for ln in burst(4, gap_s=3.5):
        r.on_line(ln, "ProjectGegnome")
    r.on_line(FIXTURE_MED_BED.replace("2026-04-03T05:00:00", "2025-08-03T21:10:40"), "ProjectGegnome")
    case("the med bed's own 'reset your BDL' notice resets it", r.level == 0 and r.recent() == 0)
    bad = [n for n, ok in res if not ok]
    for n in bad:
        print("  FAIL  " + n)
    print(f"bdl_tracker selftest: {len(res) - len(bad)}/{len(res)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
