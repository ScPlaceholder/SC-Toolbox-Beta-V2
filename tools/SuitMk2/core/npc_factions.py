"""npc_factions.py - NPC entity names in Game.log -> the name a companion SAYS (J 2026-09-25, April spec section 1).

The log names NPCs by their entity class ("PU_Human_Enemy_GroundCombat_NPC_Ninetails_grunt_7766016...") and a
companion must never read that aloud. This table turns one into "Nine Tails pirates". Same shape as
location_names.py: a pattern table plus a resolver, no model, no network.

EVERY ROW IS BACKED BY A REAL LOG LINE. Measured 2026-09-25 over all 1,112 of J's Game.log backups (LIVE + HOTFIX,
May 2024 .. Sep 2026): each pattern below matched real entity names, and LINE_FIXTURES at the bottom holds verbatim
lines (tails trimmed) with the file each came from. The April spec's six rows were the starting point; two did not
survive the check as written:
  * "ASD_soldier / ASD_grunt -> abandoned station hostiles": the entities are real (733 + 314 kills in the logs), but
    ASD is an organisation, not "an abandoned station" (in-game ship text, ARGO_ATLS_IKTI_ARGOS: "ASD has made further
    adjustments to the power suit"; bounties read "at Onyx Facility S3A10"). Spoken as "ASD troops" instead.
  * "Criminal-Pilot -> hostile pilot": kept, spoken as "criminal pilot", the log's own word.
name_inferred=True: the PATTERN is verified in the logs, the SPOKEN NAME is a reading of a class code the log never
spells out (vlk_ -> valakkar; topics_lore.json has the Valakkar as a Pyro sandworm, the logs never write the word).
No row at all for codenames with no public name (NPC_Archetypes-...-Cheesecake_*, AIModule_Unmanned_* turrets): an
unknown codename is better unsaid than guessed.

WHERE THE LOG STILL NAMES NPCs (this shrank build by build):
  <Actor Death> ... killed by '...'                       kills and deaths, up to build 10591185 (Nov 2025)
  <Debug Hostility Events> Fake hit FROM x TO y           hits, up to Jul 2025
  <Connection Flow> ... communication partner <name>      hails from NPC ships, still present Jul 2026
  any other line naming an NPC entity                     presence only ("Linked entity initializing"), Aug-Sep 2026
So in current builds most sightings are PRESENCE ("they are here"), not "they shot you". The role is kept with each
sighting so a line never says more than the log did.

    python npc_factions.py --selftest
"""
from __future__ import annotations

import re
import sys
import time
from collections import deque
from typing import Callable, Optional

# (id, pattern over the RAW entity name, spoken name, kind, name_inferred). ORDER MATTERS: first match wins, so a
# creature row comes before the faction whose pet it is ("Kopion_Headhunter_pet" is a kopion, not a Headhunter).
FACTIONS: list[tuple[str, str, str, str, bool]] = [
    ("kopion", r"^Kopion_", "kopion", "creature", False),
    ("valakkar", r"^vlk_", "valakkar", "creature", True),
    ("quasigrazer", r"^Quasigrazer_", "quasi grazer", "creature", False),
    ("nine_tails", r"(?i)ninetails", "Nine Tails pirates", "hostile", False),
    ("headhunters", r"(?i)headhunter", "Headhunters", "hostile", False),
    ("xenothreat", r"(?i)xenothreat", "XenoThreat", "hostile", False),
    ("pyro_outlaws", r"(?i)pyro_outlaw", "Pyro outlaws", "hostile", False),
    ("asd", r"_NPC_ASD_", "ASD troops", "hostile", False),
    ("cfp", r"(?i)CitizensforProsperity", "Citizens for Prosperity", "hostile", False),
    ("frontier_fighters", r"FrontierFighters", "Frontier Fighters", "hostile", False),
    ("dusters", r"-Dusters-", "Dusters", "hostile", False),
    ("shipjackers", r"^Shipjacker_", "shipjackers", "hostile", False),
    ("drug_lab", r"-Drug_(?:Chief|Cook)_", "drug lab crew", "hostile", False),
    ("contested_zone", r"(?i)_NPC_Contestedzones_", "contested zone guards", "hostile", False),
    ("distribution_centre", r"-distributioncentre_", "distribution centre guards", "hostile", False),
    ("criminal_gunner", r"Criminal-Gunner", "criminal gunner", "hostile", False),
    ("criminal_pilot", r"Criminal-Pilot", "criminal pilot", "hostile", False),
    ("vanduul", r"^Vanduul_", "Vanduul", "hostile", False),
]
_COMPILED = [(fid, re.compile(p), spoken, kind, inf) for fid, p, spoken, kind, inf in FACTIONS]

# A cheap substring gate before any regex: feed_line runs on EVERY log line (270k in one session).
_GATE = ("PU_", "NPC_", "Kopion_", "vlk_", "Quasigrazer_", "Shipjacker_", "Vanduul_")
_ENTITY = re.compile(r"\b((?:PU_|NPC_|Kopion_|vlk_|Quasigrazer_|Shipjacker_|Vanduul_)[A-Za-z0-9_\-]+)")
_KILL = re.compile(r"CActor::Kill:\s*'([^']+)'.*?killed by '([^']+)'")
_HIT = re.compile(r"Fake hit FROM (\S+) TO (\S+)")
_COMMS = re.compile(r"communication partner (\S+)")

# Roles, strongest first. A line never says more than its role: "present" is only "they are around".
ROLES = ("killed_you", "you_killed", "hit", "hailed", "present")


def resolve(entity: str) -> Optional[dict]:
    """A raw entity/class name -> {id, spoken, kind, name_inferred}, or None (a player, a ship, an unknown codename)."""
    name = str(entity or "").strip()
    if not name:
        return None
    for fid, rx, spoken, kind, inf in _COMPILED:
        if rx.search(name):
            return {"id": fid, "spoken": spoken, "kind": kind, "name_inferred": inf}
    return None


def scan_line(line: str, local_name: Optional[str] = None) -> list[dict]:
    """Every NPC the line names, with the strongest role the line supports. [] for the ~all lines naming none."""
    if not any(g in line for g in _GATE):
        return []
    out: list[dict] = []
    m = _KILL.search(line) if "<Actor Death>" in line else None
    if m:
        victim, killer = m.group(1), m.group(2)
        fk, fv = resolve(killer), resolve(victim)
        if fk and local_name and victim == local_name:
            out.append(dict(fk, role="killed_you", entity=killer))
        elif fv and local_name and killer == local_name:
            out.append(dict(fv, role="you_killed", entity=victim))
        else:
            out += [dict(f, role="present", entity=e) for f, e in ((fk, killer), (fv, victim)) if f]
        return out
    m = _HIT.search(line) if "Debug Hostility" in line else None
    if m:
        f = resolve(m.group(1))
        return [dict(f, role="hit", entity=m.group(1))] if f else []
    m = _COMMS.search(line) if "communication partner" in line else None
    if m:
        f = resolve(m.group(1))
        return [dict(f, role="hailed", entity=m.group(1))] if f else []
    seen = set()
    for e in _ENTITY.findall(line):
        f = resolve(e)
        if f and f["id"] not in seen:
            seen.add(f["id"])
            out.append(dict(f, role="present", entity=e))
    return out


class ThreatLog:
    """Recent NPC sightings. The core asks it who is around when a fight starts or the pilot goes down.

    A sighting is fresh for a window that depends on how much the log said: a kill, a hit or a hail counts for
    ACTIVE_WINDOW_S, mere presence for PRESENT_WINDOW_S. A creature that is merely PRESENT never counts as the
    opponent: a kopion wandering past a firefight is not who the pilot is fighting."""
    ACTIVE_WINDOW_S = 180.0
    PRESENT_WINDOW_S = 300.0

    def __init__(self, now: Callable[[], float] = time.time, maxlen: int = 64):
        self.now = now
        self._s: deque = deque(maxlen=maxlen)

    def note(self, sighting: dict) -> None:
        self._s.append(dict(sighting, t=self.now()))

    def clear(self) -> None:
        self._s.clear()

    def current(self) -> Optional[dict]:
        """The best fresh sighting: strongest role, then newest. None when nothing fresh names anyone."""
        now, best = self.now(), None
        for s in self._s:
            active = s["role"] != "present"
            if now - s["t"] > (self.ACTIVE_WINDOW_S if active else self.PRESENT_WINDOW_S):
                continue
            if s["kind"] == "creature" and not active:
                continue
            key = (-ROLES.index(s["role"]), s["t"])
            if best is None or key >= best[0]:
                best = (key, s)
        return best[1] if best else None


# Verbatim lines from J's logs (tails trimmed), each with the backup file it came from.
LINE_FIXTURES = [
    ("kopion", "you_killed", "Game Build(10007308) 09 Aug 25 (19 06 37).log",
     "<2025-08-10T00:17:08.737Z> [Notice] <Actor Death> CActor::Kill: 'Kopion_Irradiated_5403148872314' [5403148872314] "
     "in zone 'asd_labresearch_int_01a' killed by 'ProjectGegnome' [201926433820] using "
     "'ksar_pistol_ballistic_01_iae2023_5403148902899' [Class ksar_pistol_ballistic_01_iae2023] with damage type 'Bullet'"),
    ("asd", "killed_you", "Game Build(10098575) 21 Aug 25 (18 27 53).log",
     "<2025-08-21T22:54:08.386Z> [Notice] <Actor Death> CActor::Kill: 'ProjectGegnome' [201926433820] in zone "
     "'layout_int_lab_004' killed by 'PU_Human_Enemy_GroundCombat_NPC_ASD_grunt_5655822673257' [5655822673257] using "
     "'ksar_smg_energy_01_5655822673328' [Class ksar_smg_energy_01] with damage type 'Bullet'"),
    ("criminal_pilot", "hit", "Game Build(10310799) 28 Sep 25 (09 54 11).log",
     "<2025-09-28T21:06:23.906Z> [Notice] <Debug Hostility Events> [OnHandleHit] Fake hit FROM "
     "PU_Pilots-Human-Criminal-Pilot_Light_6351301758404 TO DRAK_Cutlass_Black_PU_AI_HeadHunters_6351301757582. Being "
     "sent to child PU_Human_Enemy_GroundCombat_NPC_Headhunters_Pilot_6351301757618 [Team_MissionFeatures][HitInfo]"),
    ("vanduul", "hailed", "Game Build(10766222) 26 Nov 25 (18 58 30).log",
     "<2025-11-27T02:45:20.134Z> [Notice] <Connection Flow> CSCCommsComponent::DoEstablishCommunicationCommon: Update "
     "bubble created for communication connection '552628486' on channel '0' for ProjectGegnome [201926433820] to track "
     "their communication partner Vanduul_Pilot_01_7766016471684 [7766016471684] [Team_CoreGameplayFeatures][Comms]"),
    ("criminal_pilot", "hailed", "Game Build(12122953) 01 Jul 26 (20 05 49).log",
     "<2026-07-02T00:41:40.604Z> [Notice] <Connection Flow> CSCCommsComponent::DoEstablishCommunicationCommon: Update "
     "bubble created for communication connection '2460408435' on channel '0' for ProjectGegnome [204715025323] to track "
     "their communication partner PU_Pilots-Human-Criminal-Pilot_Light_635844493485 [635844493485]"),
    ("asd", "present", "Game Build(12344265) 02 Aug 26 (11 00 29).log",
     "<2026-08-02T19:29:25.614Z> [Error] <Linked entity initializing!> CSCActorResultStateUsable::SetLinkedEntity: "
     "'PU_Human_Enemy_GroundCombat_NPC_ASD_sniper_Elite_749878206720' cannot change link from 'null' (bindType 0) to "
     "usable: 'GuardSpot-088' slotted: 'GuardSpot-088' (logical). [Team_ActorTech][Usable]"),
    ("kopion", "present", "Game Build(12519617) 26 Aug 26 (21 59 15).log",
     "<2026-09-07T23:10:59.737Z> CAudioProxy::SetOffset() : Invalid offset set on proxy Kopion_CombatPet_NineTails-006"),
    ("quasigrazer", "present", "Game Build(10007308) 08 Aug 25 (21 30 50).log",
     "<2025-08-09T02:53:40.410Z> CAudioProxy::SetOffset() : Invalid offset set on proxy Quasigrazer_5409205508935"),
    ("dusters", "present", "Game Build(9179031) 25 May 24 (20 18 55).log",
     "<2024-05-26T00:52:28.899Z> [Notice] <Actor Position Divergence> [PU_Human-Dusters-Engineer-Male_01_4065964670998] "
     "[AI 1] diverging 2501.100342m (threshold 4.000000m) for over 10.004625s (threshold 10.000000s)"),
]
# Entity CLASS SHAPES counted in <Actor Death> lines of J's logs (scan 2026-09-25; the numeric id suffix replaced),
# one per row that has no line fixture above, plus names that must resolve to nobody.
RESOLVE_FIXTURES = {
    "PU_Human_Enemy_GroundCombat_NPC_Ninetails_grunt_1": "nine_tails",             # 54 kills
    "PU_Human-NineTails-Grunt-Male-Medium_1_1": "nine_tails",                      # 118
    "PU_Human_Enemy_GroundCombat_NPC_Headhunters_cqc_1": "headhunters",            # 10
    "PU_Human-Headhunter-Worker-Male-Pyro_1_1": "headhunters",                     # 4
    "PU_Human_Enemy_GroundCombat_NPC_pyro_outlaw_cqc_1": "pyro_outlaws",           # 9
    "PU_Human_Enemy_GroundCombat_NPC_xenothreat_techie_1": "xenothreat",           # 6
    "PU_Human-Xenothreat-Gunner-Male-Light_1_1": "xenothreat",                     # 6
    "PU_Human_Enemy_GroundCombat_NPC_ASD_grunt_scav_1": "asd",                     # 296
    "PU_Human_Enemy_GroundCombat_NPC_CitizensforProsperity_sniper_1": "cfp",       # 5
    "NPC_Archetypes-Male-Human-FrontierFighters_juggernaut_1": "frontier_fighters",  # 18
    "Shipjacker_HUB_Medium_1_1": "shipjackers",                                    # 84
    "NPC_Archetypes-Male-Human-Civilians-Utilitarian-Drug_Cook_Utilitarian_1_1": "drug_lab",  # 5
    "PU_Human_Enemy_GroundCombat_NPC_contestedzones_cqc_1": "contested_zone",      # 16
    "NPC_Archetypes-Male-Human-distributioncentre_soldier_1": "distribution_centre",  # 9
    "PU_Pilots-Human-Criminal-Gunner_Light_1": "criminal_gunner",                  # 193
    "vlk_juvenile_irradiated_1": "valakkar",                                       # 131
    "Kopion_Headhunter_pet_1": "kopion",                                           # 5: a pet is the animal
    "ProjectGegnome": None, "unknown": None,
    "NPC_Archetypes-Male-Human-Cheesecake_soldier_1": None,                       # 62: codename, no public name
    "AIModule_Unmanned_PU_PDC_1": None, "RSI_Polaris_5166856068459": None,
}


def _selftest() -> int:
    res = []

    def case(name, cond):
        res.append((name, bool(cond)))
    for raw, want in RESOLVE_FIXTURES.items():
        case(f"resolve {raw[:48]} -> {want}", (resolve(raw) or {}).get("id") == want)
    case("every table row is backed by a fixture",
         {fid for fid, *_ in FACTIONS} <= set(RESOLVE_FIXTURES.values()) | {f for f, *_ in LINE_FIXTURES})
    for fid, role, src, line in LINE_FIXTURES:
        s = scan_line(line, "ProjectGegnome")
        case(f"real line ({src[:26]}) -> {fid} as {role}", s and s[0]["id"] == fid and s[0]["role"] == role)
    case("local pilot unknown -> a death is presence only, never 'killed you'",
         scan_line(LINE_FIXTURES[1][3], None)[0]["role"] == "present")
    case("a line naming no NPC -> nothing", scan_line(
        '<2026-09-24T00:05:03.875Z> [Notice] <SHUDEvent_OnNotification> Added notification "Contract Complete: '
        'Claim #30040: Crusader M2 Hercules Starlifter Salvage Rights: " [18]') == [])
    case("a player-vs-player kill names no faction", scan_line(
        "<t> <Actor Death> CActor::Kill: 'SomePlayer' [1] in zone 'x' killed by 'ProjectGegnome' [2] using 'y'",
        "ProjectGegnome") == [])
    clock = [1000.0]
    tl = ThreatLog(now=lambda: clock[0])
    tl.note(scan_line(LINE_FIXTURES[5][3])[0])                       # ASD present
    case("fresh presence is current", (tl.current() or {}).get("id") == "asd")
    tl.note(scan_line(LINE_FIXTURES[2][3])[0])                       # criminal pilot HIT
    case("a hit outranks presence", (tl.current() or {}).get("id") == "criminal_pilot")
    clock[0] += ThreatLog.ACTIVE_WINDOW_S + 1
    case("a stale hit gives way to fresher presence", (tl.current() or {}).get("id") == "asd")
    clock[0] += ThreatLog.PRESENT_WINDOW_S
    case("everything stale -> None", tl.current() is None)
    tl.note(scan_line(LINE_FIXTURES[6][3])[0])                       # a kopion wandering past
    case("a creature merely present is never 'the opponent'", tl.current() is None)
    bad = [n for n, ok in res if not ok]
    for n in bad:
        print("  FAIL  " + n)
    print(f"npc_factions selftest: {len(res) - len(bad)}/{len(res)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
