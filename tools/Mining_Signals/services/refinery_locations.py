"""Static database of Star Citizen refinery locations.

Groups every refinery by ``system`` and ``parent`` body so the
Mining Signals → Refinery → Locations tab can present a sensible
tree and rank entries by proximity to the player.

The distance calculation is deliberately coarse: scmdb.net / the
game log don't expose coordinates, so we use a tiered score based on
system → parent body → location class → identity matches.  Small
numbers = closer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ── Location types ──────────────────────────────────────────────
TYPE_LAGRANGE = "Lagrange Station"
TYPE_ORBITAL  = "Orbital Station"
TYPE_CITY     = "Planetary City"
TYPE_STATION  = "Deep Space Station"
TYPE_GATEWAY  = "Jump Gateway Station"
TYPE_MOON     = "Moon"
TYPE_PLANET   = "Planet"


@dataclass(frozen=True)
class RefineryLocation:
    """A single in-game refinery."""
    name: str                  # Human-readable name (also the clipboard value)
    system: str                # "Stanton" / "Pyro" / "Nyx"
    parent: str                # Planet or "(system)" if free-floating
    loc_type: str              # One of the TYPE_* constants
    aliases: tuple[str, ...] = field(default_factory=tuple)  # Log-match aliases


# ── Static location database ────────────────────────────────────

# Canonical list of in-game refineries, cross-checked against
# uexcorp.space/mining/refineries and starcitizen.tools.  Locations
# that are commonly mistaken for refineries (HUR-L3/L4/L5, CRU-L4/L5,
# ARC-L3/L5, MIC-L4, Everus Harbor, Seraphim Station, Baijini Point,
# Port Tressler, and the planetary cities) are intentionally excluded
# — they do NOT have a refinery deck.  They are still registered in
# ``_PLAYER_LOC_INDEX`` below so "Near me" can compute proximity when
# a player is standing at one of them.
# Cross-checked against the in-game MobiGlas refinery terminal (4.7.1).
# HUR-L2 was previously listed here but does NOT have a refinery in-game.
# The Nyx/Pyro gateway pair was removed — the reference only shows the
# Stanton-facing gateways plus the Stanton Gateway on each side.
REFINERIES: tuple[RefineryLocation, ...] = (
    # ── Stanton / Hurston ──
    RefineryLocation("HUR-L1 Green Glade Station",   "Stanton", "Hurston",   TYPE_LAGRANGE, ("HUR-L1", "HUR L1")),

    # ── Stanton / Crusader ──
    RefineryLocation("CRU-L1 Ambitious Dream Station","Stanton","Crusader",  TYPE_LAGRANGE, ("CRU-L1", "CRU L1")),

    # ── Stanton / ArcCorp ──
    RefineryLocation("ARC-L1 Wide Forest Station",   "Stanton", "ArcCorp",   TYPE_LAGRANGE, ("ARC-L1", "ARC L1")),
    RefineryLocation("ARC-L2 Lively Pathway Station","Stanton", "ArcCorp",   TYPE_LAGRANGE, ("ARC-L2", "ARC L2")),
    RefineryLocation("ARC-L4 Faint Glen Station",    "Stanton", "ArcCorp",   TYPE_LAGRANGE, ("ARC-L4", "ARC L4")),

    # ── Stanton / MicroTech ──
    RefineryLocation("MIC-L1 Shallow Frontier Station","Stanton","MicroTech",TYPE_LAGRANGE, ("MIC-L1", "MIC L1")),
    RefineryLocation("MIC-L2 Long Forest Station",   "Stanton", "MicroTech", TYPE_LAGRANGE, ("MIC-L2", "MIC L2")),
    RefineryLocation("MIC-L5 Modern Icarus Station", "Stanton", "MicroTech", TYPE_LAGRANGE, ("MIC-L5", "MIC L5")),

    # ── Stanton jump gateways ──
    RefineryLocation("Magnus Gateway",               "Stanton", "Jump Gateways", TYPE_GATEWAY, ("ST-MAG",)),
    RefineryLocation("Pyro Gateway",                 "Stanton", "Jump Gateways", TYPE_GATEWAY, ("ST-PYR", "Pyro Gateway (Stanton)")),
    RefineryLocation("Terra Gateway",                "Stanton", "Jump Gateways", TYPE_GATEWAY, ("ST-TER",)),

    # ── Pyro ──
    RefineryLocation("Ruin Station",                 "Pyro",    "(system)", TYPE_STATION,  ()),
    RefineryLocation("Checkmate Station",            "Pyro",    "(system)", TYPE_STATION,  ("Checkmate",)),
    RefineryLocation("Orbituary Station",             "Pyro",    "Pyro III", TYPE_ORBITAL,  ("Orbituary", "Orbituary (Pyro III)")),
    RefineryLocation("Stanton Gateway (Pyro)",       "Pyro",    "Jump Gateways", TYPE_GATEWAY, ("PYR-ST", "Stanton Gateway")),

    # ── Nyx ──
    RefineryLocation("Levski Station",               "Nyx",     "Delamar",  TYPE_CITY,    ("Levski",)),
    RefineryLocation("Stanton Gateway (Nyx)",        "Nyx",     "Jump Gateways", TYPE_GATEWAY, ("NYX-ST",)),
)


# ── Player location → refinery proximity ───────────────────────


def _normalise(s: str) -> str:
    """Lowercase + strip non-alphanumerics (for tolerant matches)."""
    return "".join(c for c in (s or "").lower() if c.isalnum())


def find_refinery_by_name(name: str) -> Optional[RefineryLocation]:
    """Return the RefineryLocation that matches ``name`` (or ``None``).

    Checks the canonical ``name`` field first, then every alias.
    Uses normalised comparison so 'HUR-L1' also matches 'HUR L1' and
    'hur-l1 green glade station' also matches the full canonical name.
    """
    if not name:
        return None
    needle = _normalise(name)
    for r in REFINERIES:
        if _normalise(r.name) == needle:
            return r
        for alias in r.aliases:
            if _normalise(alias) == needle:
                return r
    # Looser containment match so "hur" → first Hurston refinery.
    for r in REFINERIES:
        if needle in _normalise(r.name):
            return r
        for alias in r.aliases:
            if needle in _normalise(alias):
                return r
    return None


# Map a human-readable player location (as produced by the log scanner)
# onto a virtual (system, parent, type) tuple so we can compute tiered
# distances to every refinery.  Entries live in ``_PLAYER_LOC_INDEX``.


def _build_player_location_index() -> dict[str, tuple[str, str, str]]:
    idx: dict[str, tuple[str, str, str]] = {}
    for r in REFINERIES:
        for key in (r.name, *r.aliases):
            idx[_normalise(key)] = (r.system, r.parent, r.loc_type)

    # Non-refinery locations the log scanner may still report — Lagrange
    # rest stops without a refinery, orbital stations, planetary cities,
    # planets, and moons.  They need to be in the lookup index so
    # "Near me" can compute proximity when the player is standing at
    # one of them (e.g. at Lorville → Hurston refineries are tier 1).
    #
    # Tuple schema: (name, system, parent, loc_type)
    extras: tuple[tuple[str, str, str, str], ...] = (
        # Hurston Lagrange (non-refinery rest stops — HUR-L2 was removed
        # from the refinery list as it does NOT have a refinery in-game)
        ("HUR-L2",                              "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L2 Faithful Dream Station",       "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L3",                              "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L3 Thundering Express Station",   "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L4",                              "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L4 Melodic Fields Station",       "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L5",                              "Stanton", "Hurston",   TYPE_LAGRANGE),
        ("HUR-L5 High Course Station",          "Stanton", "Hurston",   TYPE_LAGRANGE),
        # Hurston orbital + city
        ("Everus Harbor",                       "Stanton", "Hurston",   TYPE_ORBITAL),
        ("Lorville",                            "Stanton", "Hurston",   TYPE_CITY),
        # Crusader Lagrange (non-refinery)
        ("CRU-L4",                              "Stanton", "Crusader",  TYPE_LAGRANGE),
        ("CRU-L4 Shallow Fields Station",       "Stanton", "Crusader",  TYPE_LAGRANGE),
        ("CRU-L5",                              "Stanton", "Crusader",  TYPE_LAGRANGE),
        ("CRU-L5 Beautiful Glen Station",       "Stanton", "Crusader",  TYPE_LAGRANGE),
        # Crusader orbital + city
        ("Seraphim Station",                    "Stanton", "Crusader",  TYPE_ORBITAL),
        ("Orison",                              "Stanton", "Crusader",  TYPE_CITY),
        # ArcCorp Lagrange (non-refinery)
        ("ARC-L3",                              "Stanton", "ArcCorp",   TYPE_LAGRANGE),
        ("ARC-L3 Modern Express Station",       "Stanton", "ArcCorp",   TYPE_LAGRANGE),
        ("ARC-L5",                              "Stanton", "ArcCorp",   TYPE_LAGRANGE),
        ("ARC-L5 Yellow Core Station",          "Stanton", "ArcCorp",   TYPE_LAGRANGE),
        # ArcCorp orbital + city
        ("Baijini Point",                       "Stanton", "ArcCorp",   TYPE_ORBITAL),
        ("Area 18",                             "Stanton", "ArcCorp",   TYPE_CITY),
        ("Area18",                              "Stanton", "ArcCorp",   TYPE_CITY),
        # microTech Lagrange (non-refinery)
        ("MIC-L4",                              "Stanton", "MicroTech", TYPE_LAGRANGE),
        ("MIC-L4 Red Crossroads Station",       "Stanton", "MicroTech", TYPE_LAGRANGE),
        # microTech orbital + city
        ("Port Tressler",                       "Stanton", "MicroTech", TYPE_ORBITAL),
        ("New Babbage",                         "Stanton", "MicroTech", TYPE_CITY),
        # Stanton planets
        ("Hurston",                             "Stanton", "Hurston",   TYPE_PLANET),
        ("Crusader",                            "Stanton", "Crusader",  TYPE_PLANET),
        ("ArcCorp",                             "Stanton", "ArcCorp",   TYPE_PLANET),
        ("microTech",                           "Stanton", "MicroTech", TYPE_PLANET),
        ("MicroTech",                           "Stanton", "MicroTech", TYPE_PLANET),
        # Stanton moons — grouped under their parent planet so mining
        # on Aberdeen still ranks all Hurston refineries at tier 1.
        ("Arial",                               "Stanton", "Hurston",   TYPE_MOON),
        ("Aberdeen",                            "Stanton", "Hurston",   TYPE_MOON),
        ("Magda",                               "Stanton", "Hurston",   TYPE_MOON),
        ("Ita",                                 "Stanton", "Hurston",   TYPE_MOON),
        ("Daymar",                              "Stanton", "Crusader",  TYPE_MOON),
        ("Yela",                                "Stanton", "Crusader",  TYPE_MOON),
        ("Cellin",                              "Stanton", "Crusader",  TYPE_MOON),
        ("Lyria",                               "Stanton", "ArcCorp",   TYPE_MOON),
        ("Wala",                                "Stanton", "ArcCorp",   TYPE_MOON),
        ("Calliope",                            "Stanton", "MicroTech", TYPE_MOON),
        ("Clio",                                "Stanton", "MicroTech", TYPE_MOON),
        ("Euterpe",                             "Stanton", "MicroTech", TYPE_MOON),
        # Pyro planets + moons
        ("Pyro I",                              "Pyro",    "(system)",  TYPE_PLANET),
        ("Monox",                               "Pyro",    "(system)",  TYPE_PLANET),
        ("Pyro III",                            "Pyro",    "Pyro III",  TYPE_PLANET),
        ("Pyro IV",                             "Pyro",    "(system)",  TYPE_PLANET),
        ("Pyro V",                              "Pyro",    "(system)",  TYPE_PLANET),
        ("Bloom",                               "Pyro",    "(system)",  TYPE_PLANET),
        # Nyx
        ("Delamar",                             "Nyx",     "Delamar",   TYPE_PLANET),
    )
    for name, system, parent, loc_type in extras:
        idx[_normalise(name)] = (system, parent, loc_type)
    return idx


_PLAYER_LOC_INDEX: dict[str, tuple[str, str, str]] = _build_player_location_index()


def lookup_player_location(name: str) -> Optional[tuple[str, str, str]]:
    """Return ``(system, parent, type)`` for a player-reported location."""
    if not name:
        return None
    n = _normalise(name)
    if n in _PLAYER_LOC_INDEX:
        return _PLAYER_LOC_INDEX[n]
    # Tolerate substring matches (e.g. "HUR-L1 Green Glade" → HUR-L1).
    for key, val in _PLAYER_LOC_INDEX.items():
        if key and (key in n or n in key):
            return val
    return None


def distance_score(player_name: str, refinery: RefineryLocation) -> int:
    """Return a small integer proximity score (0 = closest).

    Tiers are coarse because we don't have real coordinates:
      0     same refinery
      1     same parent body (moon shares no refineries, so in practice
            "on Hurston" → Lorville / Everus Harbor cluster)
      10    same system, different parent
      100   different system
      +500  unknown player location (put all equal at the end)
    """
    player = lookup_player_location(player_name)
    if player is None:
        return 500
    p_sys, p_parent, _p_type = player
    # If the player is already standing at (or has aliased into) the
    # same refinery, tier 0.  ``find_refinery_by_name`` handles aliases
    # and loose substring matches so "HUR-L1" → "HUR-L1 Green Glade".
    same = find_refinery_by_name(player_name)
    if same is not None and same.name == refinery.name:
        return 0
    if p_parent == refinery.parent and p_sys == refinery.system:
        return 1
    if p_sys == refinery.system:
        return 10
    return 100


def rank_by_proximity(player_name: str) -> list[tuple[int, RefineryLocation]]:
    """Return every refinery paired with its proximity score, sorted."""
    scored = [(distance_score(player_name, r), r) for r in REFINERIES]
    scored.sort(key=lambda t: (t[0], t[1].system, t[1].parent, t[1].name))
    return scored


def group_by_system_parent(
    entries: list[RefineryLocation] | None = None,
) -> dict[str, dict[str, list[RefineryLocation]]]:
    """Build a nested dict ``{system: {parent: [refineries]}}``.

    Pass a filtered list from the caller (e.g. a search-filtered view).
    ``None`` returns the full database.  Insertion order matches
    :data:`REFINERIES`, which is already grouped sensibly.
    """
    data = entries if entries is not None else list(REFINERIES)
    out: dict[str, dict[str, list[RefineryLocation]]] = {}
    for r in data:
        out.setdefault(r.system, {}).setdefault(r.parent, []).append(r)
    return out
