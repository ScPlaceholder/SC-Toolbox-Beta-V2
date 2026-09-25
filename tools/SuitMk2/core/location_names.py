"""
SuitMk2 - Location Name Mapping

Maps Star Citizen game log location codes to human-readable names
and star system identifiers.
"""

from __future__ import annotations

from typing import NamedTuple


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
    "Stanton2_Area18": LocationInfo("Area 18", "Stanton"),
    "ARC_Area18": LocationInfo("Area 18", "Stanton"),
    "Stanton3_Orison": LocationInfo("Orison", "Stanton"),
    "CRU_Orison": LocationInfo("Orison", "Stanton"),
    "Stanton4_NewBabbage": LocationInfo("New Babbage", "Stanton"),
    "MIC_NewBabbage": LocationInfo("New Babbage", "Stanton"),
    # Stanton - Moons
    "Stanton1a": LocationInfo("Arial", "Stanton"),
    "Stanton1b": LocationInfo("Aberdeen", "Stanton"),
    "Stanton1c": LocationInfo("Magda", "Stanton"),
    "Stanton1d": LocationInfo("Ita", "Stanton"),
    "Stanton2a": LocationInfo("Lyria", "Stanton"),
    "Stanton2b": LocationInfo("Wala", "Stanton"),
    "Stanton3a": LocationInfo("Cellin", "Stanton"),
    "Stanton3b": LocationInfo("Daymar", "Stanton"),
    "Stanton3c": LocationInfo("Yela", "Stanton"),
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


def get_location_name(code: str) -> str:
    """Get human-readable name for a location code."""
    if not code:
        return "Unknown"
    if code in LOCATION_MAP:
        return LOCATION_MAP[code].name
    code_upper = code.upper()
    for key, info in LOCATION_MAP.items():
        if key.upper() == code_upper:
            return info.name
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
    for prefix, system in _SYSTEM_PREFIX_MAP:
        if code.startswith(prefix):
            return system
    return "Unknown"


def get_location_type(code: str) -> str:
    """Classify location type: station, city, moon, planet, jump_point, unknown."""
    if not code:
        return "unknown"
    for pattern, loc_type in _LOCATION_TYPE_MAP:
        if pattern in code:
            return loc_type
    # Moon detection: Stanton[1-4][a-d]
    if len(code) >= 9 and code.startswith("Stanton") and code[-1].isalpha():
        return "moon"
    # Planet: Stanton[1-4] without moon suffix
    if code.startswith("Stanton") and code[-1].isdigit():
        return "planet"
    return "unknown"
