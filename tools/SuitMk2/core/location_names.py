"""
SuitMk2 - Location Name Mapping

Maps Star Citizen game log location codes to human-readable names
and star system identifiers.

TWO SOURCES, in this order:
  1. LOCATION_MAP below: the hand-written table. It wins, so every name it has given so far is unchanged.
  2. THE CATALOGUE: data/brochures.jsonl, 513 places from the starmap catalogue. Each row's source_url ends in
     the log's own code, lower-cased with hyphens ("outpost-olp-stanton1b-vivere" is the log's
     Outpost_OLP_Stanton1b_Vivere), and the row carries the display name, the type and the body. So a place
     the table never listed gets its real name instead of the code with its underscores removed. Measured on
     1,116 real Game.logs: 218 distinct codes, 131 named by the catalogue (all 16 Hathor sites, every Pyro
     outpost and station the pilot has been to, the HDMS and Rayari outposts); the table alone named 37.
  3. Neither: the old fallback (the code with its prefix and underscores removed). is_named() says False, so the
     conversation lane can say it is reading the log's label and has no proper name.

Still unnamed after this, because no data in the toolbox names them: the ASD Delve facilities, the junk
sites (Brio's, Samson and Son's, Delvin), the Collector asteroids, the Astro Armada plants, Kaboos and the Nyx
extraction stations.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from typing import NamedTuple, Optional


class LocationInfo(NamedTuple):
    name: str
    system: str


LOCATION_MAP: dict[str, LocationInfo] = {
    # Stanton - Orbital Stations
    "RR_MIC_LEO": LocationInfo("Port Tressler", "Stanton"),
    "RR_HUR_LEO": LocationInfo("Everus Harbor", "Stanton"),
    "RR_CRU_LEO": LocationInfo("Seraphim Station", "Stanton"),
    "RR_ARC_LEO": LocationInfo("Baijini Point", "Stanton"),
    # Stanton - Lagrange Stations
    "RR_HUR_L1": LocationInfo("HUR-L1", "Stanton"),
    "RR_HUR_L2": LocationInfo("HUR-L2", "Stanton"),
    "RR_HUR_L3": LocationInfo("HUR-L3", "Stanton"),
    "RR_HUR_L4": LocationInfo("HUR-L4", "Stanton"),
    "RR_HUR_L5": LocationInfo("HUR-L5", "Stanton"),
    "RR_CRU_L1": LocationInfo("CRU-L1", "Stanton"),
    "RR_CRU_L2": LocationInfo("CRU-L2", "Stanton"),
    "RR_CRU_L3": LocationInfo("CRU-L3", "Stanton"),
    "RR_CRU_L4": LocationInfo("CRU-L4", "Stanton"),
    "RR_CRU_L5": LocationInfo("CRU-L5", "Stanton"),
    "RR_ARC_L1": LocationInfo("ARC-L1", "Stanton"),
    "RR_ARC_L2": LocationInfo("ARC-L2", "Stanton"),
    "RR_ARC_L3": LocationInfo("ARC-L3", "Stanton"),
    "RR_ARC_L4": LocationInfo("ARC-L4", "Stanton"),
    "RR_ARC_L5": LocationInfo("ARC-L5", "Stanton"),
    "RR_MIC_L1": LocationInfo("MIC-L1", "Stanton"),
    "RR_MIC_L2": LocationInfo("MIC-L2", "Stanton"),
    "RR_MIC_L3": LocationInfo("MIC-L3", "Stanton"),
    "RR_MIC_L4": LocationInfo("MIC-L4", "Stanton"),
    "RR_MIC_L5": LocationInfo("MIC-L5", "Stanton"),
    # Stanton - Cities
    "Stanton1_Lorville": LocationInfo("Lorville", "Stanton"),
    "HUR_Lorville": LocationInfo("Lorville", "Stanton"),
    # Stanton2 is Crusader and Stanton3 is ArcCorp. The two keys here used to be the other way round
    # ("Stanton2_Area18", "Stanton3_Orison"), so neither ever matched: real logs say Stanton2_Orison 599 times and
    # Stanton3_Area18 303 times, and both fell to the fallback and were read out as "2 Orison" and "3 Area18".
    "Stanton3_Area18": LocationInfo("Area 18", "Stanton"),
    "ARC_Area18": LocationInfo("Area 18", "Stanton"),
    "Stanton2_Orison": LocationInfo("Orison", "Stanton"),
    "CRU_Orison": LocationInfo("Orison", "Stanton"),
    "Stanton4_NewBabbage": LocationInfo("New Babbage", "Stanton"),
    "MIC_NewBabbage": LocationInfo("New Babbage", "Stanton"),
    # Stanton - Moons
    "Stanton1a": LocationInfo("Arial", "Stanton"),
    "Stanton1b": LocationInfo("Aberdeen", "Stanton"),
    "Stanton1c": LocationInfo("Magda", "Stanton"),
    "Stanton1d": LocationInfo("Ita", "Stanton"),
    # Crusader's moons are Stanton2a-c and ArcCorp's are Stanton3a-b (the same swap as the cities above). The
    # catalogue agrees: Stanton2a_RayariHydro_HickesResearch is on Cellin, Stanton2b_ShubinMining_SCD1 on Daymar,
    # Stanton3a_Shubin_SAL2 on Lyria, Stanton3b_ArcCorp_Area045 on Wala.
    "Stanton2a": LocationInfo("Cellin", "Stanton"),
    "Stanton2b": LocationInfo("Daymar", "Stanton"),
    "Stanton2c": LocationInfo("Yela", "Stanton"),
    "Stanton3a": LocationInfo("Lyria", "Stanton"),
    "Stanton3b": LocationInfo("Wala", "Stanton"),
    "Stanton4a": LocationInfo("Calliope", "Stanton"),
    "Stanton4b": LocationInfo("Clio", "Stanton"),
    "Stanton4c": LocationInfo("Euterpe", "Stanton"),
    # Stanton - Other
    "GrimHex": LocationInfo("Grim HEX", "Stanton"),
    "GH_GrimHex": LocationInfo("Grim HEX", "Stanton"),
    "RR_Grim_Hex": LocationInfo("Grim HEX", "Stanton"),
    # Nyx System
    "Nyx_Levski": LocationInfo("Levski", "Nyx"),
    "NYX_Levski": LocationInfo("Levski", "Nyx"),
    # Jump Points
    "RR_JP_NyxCastra": LocationInfo("Nyx-Castra Jump Point", "Jump Point"),
    "RR_JP_StantonPyro": LocationInfo("Stanton-Pyro Jump Point", "Jump Point"),
    "JP_StantonPyro": LocationInfo("Stanton-Pyro Jump Point", "Jump Point"),
    "RR_JP_StantonMagnus": LocationInfo("Stanton-Nyx Jump Point", "Jump Point"),
    "JP_StantonMagnus": LocationInfo("Stanton-Nyx Jump Point", "Jump Point"),
    # Pyro System
    "Pyro_Ruin": LocationInfo("Ruin Station", "Pyro"),
    "PYRO_Ruin": LocationInfo("Ruin Station", "Pyro"),
}

_SYSTEM_PREFIX_MAP: list[tuple[str, str]] = [
    ("Stanton", "Stanton"),
    ("RR_HUR", "Stanton"),
    ("RR_CRU", "Stanton"),
    ("RR_ARC", "Stanton"),
    ("RR_MIC", "Stanton"),
    ("RR_Grim", "Stanton"),
    ("HUR_", "Stanton"),
    ("ARC_", "Stanton"),
    ("CRU_", "Stanton"),
    ("MIC_", "Stanton"),
    ("GH_", "Stanton"),
    ("GrimHex", "Stanton"),
    ("Pyro", "Pyro"),
    ("PYRO", "Pyro"),
    ("Nyx", "Nyx"),
    ("NYX", "Nyx"),
    ("RR_JP_", "Jump Point"),
    ("JP_", "Jump Point"),
]

# Location type classification based on code patterns
_LOCATION_TYPE_MAP: list[tuple[str, str]] = [
    ("RR_", "station"),
    ("_Lorville", "city"),
    ("_Area18", "city"),
    ("_Orison", "city"),
    ("_NewBabbage", "city"),
    ("_Levski", "city"),
    ("_Ruin", "station"),
    ("GrimHex", "station"),
    ("GH_", "station"),
    ("JP_", "jump_point"),
]


# ---------------------------------------------------------------------------------------------------------------
# The catalogue (data/brochures.jsonl)
# ---------------------------------------------------------------------------------------------------------------
CATALOGUE_PATH = Path(__file__).resolve().parent.parent / "data" / "brochures.jsonl"
_CATALOGUE_TYPES = {"Outpost": "outpost", "Station": "station", "LandingZone": "city", "Moon": "moon",
                    "Planet": "planet"}
_BODY_TOKEN = re.compile(r"^(?:stanton|pyro|nyx)\d[a-z]?$")
_catalogue: Optional[dict] = None          # the log's code as a slug -> the catalogue row
_bodies: dict = {}                         # "stanton1b" -> "Aberdeen", by majority of the rows carrying that token


class PlaceInfo(NamedTuple):
    name: str
    system: str
    type: str          # city | station | outpost | moon | planet | jump_point | unknown
    body: str          # the planet or moon it is on or around; "" when nothing says
    source: str        # "table" | "catalogue"


def _slug(code: str) -> str:
    return str(code or "").strip().lower().replace("_", "-")


def _clean(v) -> str:
    return str(v or "").replace("\u00a0", " ").strip()        # five rows say "Magda" with a no-break space after it


def load_catalogue(path: Optional[Path] = None) -> dict:
    """Read the catalogue once. A missing or broken file is an empty catalogue: every code then resolves exactly as
    it did before the catalogue existed."""
    global _catalogue, _bodies
    rows: dict = {}
    votes: dict = {}
    try:
        with open(path or CATALOGUE_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                slug = str(r.get("source_url") or "").rstrip("/").rsplit("/", 1)[-1].lower()
                name = _clean(r.get("name"))
                if not slug or not name:
                    continue
                rows[slug] = {"name": name, "type": _clean(r.get("type")), "parent": _clean(r.get("parent")),
                              "system": _clean(r.get("system"))}
                parent = rows[slug]["parent"]
                if parent and parent != rows[slug]["system"]:
                    for tok in slug.split("-"):
                        if _BODY_TOKEN.match(tok):
                            votes.setdefault(tok, Counter())[parent] += 1
                            break
    except OSError:
        rows = {}
    _catalogue = rows
    _bodies = {tok: c.most_common(1)[0][0] for tok, c in votes.items()}
    return rows


def catalogue_entry(code: str) -> Optional[dict]:
    if _catalogue is None:
        load_catalogue()
    return (_catalogue or {}).get(_slug(code))


def _table_entry(code: str) -> Optional[LocationInfo]:
    if code in LOCATION_MAP:
        return LOCATION_MAP[code]
    code_upper = code.upper()
    for key, info in LOCATION_MAP.items():
        if key.upper() == code_upper:
            return info
    return None


def get_location_body(code: str) -> str:
    """The planet or moon a code is on or around, or "".

    The catalogue row's own parent, when the code has a row. A code with no row (an ASD Delve facility, a junk
    site) still carries a body token, "Stanton1a", and the body is the one most catalogue rows with that token
    name. One correction: a row whose parent is the body the catalogue gives to a DIFFERENT token is overruled
    by its own code. That is Stanton2c_DrugLab_Jumptown, whose row says Daymar; Daymar is Stanton2b, and the
    other rows carrying Stanton2c all say Yela."""
    if not code:
        return ""
    if _catalogue is None:
        load_catalogue()
    voted = ""
    for tok in _slug(code).split("-"):
        if _BODY_TOKEN.match(tok):
            voted = _bodies.get(tok, "")
            break
    row = catalogue_entry(code)
    parent = row["parent"] if row and row["parent"] != row["system"] else ""
    if parent and voted and parent != voted and parent in _bodies.values():
        return voted
    return parent or voted


def resolve(code: str) -> Optional[PlaceInfo]:
    """The place a log code names, or None when neither the table nor the catalogue knows it."""
    if not code:
        return None
    t = _table_entry(code)
    row = catalogue_entry(code)
    if t is None and row is None:
        return None
    return PlaceInfo(t.name if t else row["name"], t.system if t else (row["system"] or get_location_system(code)),
                     get_location_type(code), get_location_body(code), "table" if t else "catalogue")


def is_named(code: str) -> bool:
    """True when get_location_name(code) is a real name, False when it is the code with its underscores removed."""
    return resolve(code) is not None


def get_location_name(code: str) -> str:
    """Get human-readable name for a location code."""
    if not code:
        return "Unknown"
    t = _table_entry(code)
    if t is not None:
        return t.name
    row = catalogue_entry(code)
    if row is not None:
        return row["name"]
    cleaned = code
    for prefix in ["RR_", "Stanton", "HUR_", "ARC_", "CRU_", "MIC_"]:
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned.replace("_", " ")


def get_location_system(code: str) -> str:
    """Get star system for a location code."""
    if not code:
        return "Unknown"
    if code in LOCATION_MAP:
        return LOCATION_MAP[code].system
    code_upper = code.upper()
    for key, info in LOCATION_MAP.items():
        if key.upper() == code_upper:
            return info.system
    row = catalogue_entry(code)
    if row is not None and row["system"]:
        return row["system"]
    for prefix, system in _SYSTEM_PREFIX_MAP:
        if code.startswith(prefix):
            return system
    return "Unknown"


def get_location_type(code: str) -> str:
    """Classify location type: station, city, outpost, moon, planet, jump_point, unknown."""
    if not code:
        return "unknown"
    for pattern, loc_type in _LOCATION_TYPE_MAP:
        if pattern in code:
            return loc_type
    row = catalogue_entry(code)
    if row is not None and row["type"] in _CATALOGUE_TYPES:
        return _CATALOGUE_TYPES[row["type"]]
    # A moon is Stanton[1-4][a-d] and nothing after it; a planet is Stanton[1-4]. These two tests used to
    # look only at the LAST character, so Stanton1_HurdynMining_HDMSEdmond (an outpost) was a "moon" and
    # Stanton4_Shubin_SM0_10 (another) was a "planet".
    if re.fullmatch(r"Stanton\d[a-z]", code):
        return "moon"
    if re.fullmatch(r"Stanton\d", code):
        return "planet"
    return "unknown"
