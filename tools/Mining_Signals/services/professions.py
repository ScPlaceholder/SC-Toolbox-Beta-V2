"""
Profession definitions for the Mining Roster.

Each profession has a display name, an emoji icon, and a short
description that appears in the profession key view.
"""

from __future__ import annotations

# (name, icon, description) — kept in alphabetical order at module load
_PROFESSIONS_RAW: list[tuple[str, str, str]] = [
    ("Combat Pilot", "\U0001F3AF",
     "Dogfighting, bounty hunting contracts, and PvP/PvE combat in fighters and heavy ships."),
    ("Cargo Hauler", "\U0001F4E6",
     "Running trade routes, buying low and selling high, optimizing SCU loads across systems."),
    ("Copilot", "\U0001F9D1\u200D\u2708\uFE0F",
     "Managing shields, power, quantum drives, and navigation as the second seat on multi-crew ships."),
    ("Miner", "\u26CF\uFE0F",
     "Prospecting, scanning, and extracting ore from asteroids or planetary deposits."),
    ("Salvager", "\U0001F527",
     "Stripping wrecks for components, hull materials, and valuable parts."),
    ("Bounty Hunter", "\U0001F3F9",
     "Tracking and capturing (or eliminating) player and NPC targets for contract payouts."),
    ("Pirate / Raider", "\u2620\uFE0F",
     "Interdicting cargo haulers, hijacking ships, and operating outside the law for profit."),
    ("Medical Responder", "\U0001F691",
     "Reviving downed players in the field, running hospital ships, triage in combat zones."),
    ("Explorer", "\U0001F9ED",
     "Charting jump points, scanning unvisited POIs, discovering derelicts, and mapping the unknown."),
    ("Mercenary", "\U0001F4B0",
     "Hired muscle for org operations, bunker clearing, FPS combat, and escort missions."),
    ("Racing Pilot", "\U0001F3C1",
     "Competing in ship races, tuning racing builds, and running time trials."),
    ("Refinery Operator", "\U0001F3ED",
     "Processing raw ore into refined materials, managing refinery jobs and logistics chains."),
    ("Fuel Runner", "\u26FD",
     "Operating Starfarer-class ships to harvest and deliver fuel to remote locations or fleets."),
    ("Smuggler", "\U0001F575\uFE0F",
     "Moving illicit cargo past security scans, navigating restricted zones, dealing in contraband."),
    ("Org Logistics Coordinator", "\U0001F4CB",
     "Managing fleet movements, supply lines, and resource allocation for large organizations."),
    ("Ship Mechanic / Engineer", "\U0001F529",
     "Repairing, maintaining, and tuning ship components; multi-crew engineering roles."),
    ("Data Runner", "\U0001F4BE",
     "Transporting encrypted data between locations, prioritizing speed and stealth over firepower."),
    ("Reconnaissance Scout", "\U0001F441\uFE0F",
     "Gathering intel on enemy fleet positions, scanning areas ahead of org operations."),
    ("Farming / Biome Specialist", "\U0001F33E",
     "Growing and harvesting crops or biological resources on planetary homesteads."),
    ("Arms Dealer / Merchant", "\U0001F4BC",
     "Buying, transporting, and selling weapons, armor, and ship components across systems."),
    ("Fleet Commander / Strategist", "\U0001F396\uFE0F",
     "Leading multi-ship org combat operations and coordinating capital ship engagements."),
    ("Gunner / Turret Operator", "\U0001F52B",
     "Manning remote and manned turrets on multi-crew ships, defending against fighters, and tracking enemy capital targets."),
    ("Box Monkey", "\U0001F412",
     "LOVES moving boxes. They are literally obsessed with moving boxes and cannot get enough."),
]

# Sorted alphabetically by name (case-insensitive)
PROFESSIONS: list[tuple[str, str, str]] = sorted(
    _PROFESSIONS_RAW, key=lambda entry: entry[0].lower()
)


ICON_BY_NAME: dict[str, str] = {name: icon for name, icon, _ in PROFESSIONS}
DESC_BY_NAME: dict[str, str] = {name: desc for name, _, desc in PROFESSIONS}


def icon_for(name: str) -> str:
    """Return the emoji icon for a profession name, or '' if unknown."""
    return ICON_BY_NAME.get(name, "")


def all_profession_names() -> list[str]:
    return [name for name, _, _ in PROFESSIONS]


def fuzzy_match(query: str) -> list[tuple[str, str, str]]:
    """Return professions matching a query (case-insensitive substring
    against name or description). Prefix matches come first, then
    other substring matches. Returns all if query is empty.
    """
    if not query:
        return list(PROFESSIONS)
    q = query.lower().strip()
    prefix: list[tuple[str, str, str]] = []
    other: list[tuple[str, str, str]] = []
    for entry in PROFESSIONS:
        name_l = entry[0].lower()
        desc_l = entry[2].lower()
        if name_l.startswith(q):
            prefix.append(entry)
        elif q in name_l or q in desc_l:
            other.append(entry)
    return prefix + other
