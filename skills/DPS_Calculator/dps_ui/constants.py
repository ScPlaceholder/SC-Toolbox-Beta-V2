import os
import re

from shared.i18n import s_ as _
from shared.api_config import (
    ERKUL_BASE_URL, ERKUL_HEADERS, ERKUL_TIMEOUT,
    FLEETYARDS_BASE_URL, FLEETYARDS_HEADERS,
    CACHE_TTL_ERKUL, CACHE_TTL_CARGO,
)

# ── API ───────────────────────────────────────────────────────────────────────
API_BASE    = ERKUL_BASE_URL
API_HEADERS = ERKUL_HEADERS
CACHE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".erkul_cache.json")
CACHE_TTL     = CACHE_TTL_ERKUL
CACHE_VERSION = 5

# ── Fleetyards API
FY_BASE    = FLEETYARDS_BASE_URL
FY_HEADERS = FLEETYARDS_HEADERS
FY_HP_CACHE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", ".fy_hardpoints_cache.json"
)
FY_HP_TTL = CACHE_TTL_CARGO

# ── Palette — import canonical colours from shared theme ─────────────────────
from shared.qt.theme import P

BG           = P.bg_primary
BG2          = P.bg_secondary
BG3          = P.bg_card
BG4          = P.bg_input
BORDER       = P.border
FG           = P.fg
FG_DIM       = P.fg_dim
FG_DIMMER    = P.fg_disabled
ACCENT       = P.accent
GREEN        = P.green
YELLOW       = P.yellow
RED          = P.red
ORANGE       = P.orange
CYAN         = P.energy_cyan
PURPLE       = P.purple
PHYS_COL     = "#99aabb"
ENERGY_COL   = P.energy_cyan
DIST_COL     = "#bb88ff"
THERM_COL    = P.orange
HEADER_BG    = P.bg_header
SECT_HDR_BG  = "#131928"
CARD_EVEN    = P.bg_card
CARD_ODD     = P.bg_input
CARD_BORDER  = P.border_card
ROW_EVEN     = CARD_EVEN
ROW_ODD      = CARD_ODD
SIZE_COLORS  = {1: "#2a5580", 2: "#2a6677", 3: "#256655", 4: "#446622"}
TRACK_COLORS = {"IR": THERM_COL, "EM": ENERGY_COL, "CrossSection": PHYS_COL}
TYPE_STRIPE  = {
    "WeaponGun":      ENERGY_COL,
    "MissileLauncher": RED,
    "Shield":         DIST_COL,
    "Cooler":         CYAN,
    "Radar":          FG_DIM,
    "PowerPlant":     ORANGE,
    "QuantumDrive":   ACCENT,
    "Thruster":       YELLOW,
}

# ── Label helpers ─────────────────────────────────────────────────────────────
_LABEL_STRIP = re.compile(
    r"(^hardpoint_|_weapon$|_gun$|^hardpoint_class_\d+$|^weapon_)",
    re.IGNORECASE,
)
_TURRET_HOUSING_SUBTYPES = {
    "TopTurret", "MannedTurret", "BallTurret", "NoseTurret",
    "RemoteTurret", "UpperTurret", "LowerTurret",
}
_GROUP_SHORT = {
    "laser repeater":        "LR", "laser cannon":          "LC",
    "laser gatling":         "LG", "laser scattergun":      "LS",
    "laser beam":            "LB", "ballistic repeater":    "BR",
    "ballistic cannon":      "BC", "ballistic gatling":     "BG",
    "ballistic scattergun":  "BS", "distortion cannon":     "DC",
    "distortion repeater":   "DR", "distortion scattergun": "DS",
    "plasma cannon":         "PC", "tachyon cannon":        "TC",
    "neutron cannon":        "NC", "rocket pod":            "RP",
}
_DOM_COL = {
    "damagePhysical":    PHYS_COL,
    "damageEnergy":      ENERGY_COL,
    "damageDistortion":  DIST_COL,
    "damageThermal":     THERM_COL,
}
_TRACK_COL = {"IR": THERM_COL, "EM": ENERGY_COL, "CrossSection": PHYS_COL}

# Voice-command tab -> data-access + UI-change mapping
_TAB_FIND = {
    "weapons":  "find_weapon",
    "missiles": "find_missile",
    "defenses": "find_shield",
}
_TAB_CHANGE = {
    "weapons":  "_weapon_on_change",
    "missiles": "_missile_on_change",
    "defenses": "_shield_on_change",
}

_FY_SIZE_MAP = {
    "small": 1, "s": 1, "one": 1, "1": 1,
    "medium": 2, "m": 2, "two": 2, "2": 2,
    "large": 3, "l": 3, "three": 3, "3": 3,
    "capital": 4, "xl": 4, "four": 4, "4": 4,
}

_INF = float('inf')


# ── Small helper functions (defined here to avoid circular imports) ───────────
def group_short(g: str) -> str:
    """Abbreviate a weapon group name to a 2-3 char code."""
    return _GROUP_SHORT.get(g.lower(), g[:3].upper() if g else "\u2014")


def pct(v: float) -> str:
    """Format a 0-1 fraction as a signed percentage string."""
    return f"{v*100:+.0f}%" if v is not None else "0%"


# ── Column specs (erkul-style table rows) ─────────────────────────────────────
# Format: (header, key, char_width, fg_color, fmt_fn)

QD_COLS = [
    (_("Name"),       "name",       16, FG,         lambda v, it: it["name"]),
    (_("Class"),      "class",       9, FG_DIM,     lambda v, it: str(v) if v else "\u2014"),
    (_("Grade"),      "grade",       5, FG_DIM,     lambda v, it: str(v) if v else "\u2014"),
    (_("Speed km/s"), "speed",      10, GREEN,      lambda v, it: f"{v/1000:,.0f}" if v else "\u2014"),
    (_("Max Dist Gm"),"jump_range", 10, FG,         lambda v, it: "\u221e" if v >= _INF else (f"{v/1e9:.1f}" if v else "\u2014")),
    (_("Spool s"),    "spool",       7, YELLOW,     lambda v, it: f"{v:.1f}" if v else "\u2014"),
    (_("Cooldown s"), "cooldown",    9, FG_DIM,     lambda v, it: f"{v:.1f}" if v else "\u2014"),
    (_("Fuel/Mm"),    "fuel_rate",   8, ENERGY_COL, lambda v, it: f"{v:.2f}" if v else "\u2014"),
    (_("Power kW"),   "power_draw",  8, ORANGE,     lambda v, it: f"{v/1000:.1f}" if v else "\u2014"),
    (_("EM"),         "em_max",      8, YELLOW,     lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("HP"),         "hp",          6, PHYS_COL,   lambda v, it: f"{v:.0f}" if v else "\u2014"),
]

PP_COLS = [
    (_("Name"),       "name",       20, FG,     lambda v, it: it["name"]),
    (_("Class"),      "class",      10, FG_DIM, lambda v, it: str(v) if v else "\u2014"),
    (_("Grade"),      "grade",       5, FG_DIM, lambda v, it: str(v) if v else "\u2014"),
    (_("Output"),     "output",     10, ORANGE, lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("IR"),         "ir_max",      8, THERM_COL, lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("EM"),         "em_max",      8, YELLOW, lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("HP"),         "hp",          6, PHYS_COL,   lambda v, it: f"{v:.0f}" if v else "\u2014"),
]

WEAPON_TABLE_COLS = [
    (_("Name"),    "name",    12, FG,      lambda v, it: it["name"]),
    (_("Type"),    "group",    3, FG_DIM,  lambda v, it: group_short(it.get("group", ""))),
    (_("DPS\u2193"),   "dps_sus",  7, GREEN,   lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("Raw"),     "dps_raw",  6, YELLOW,  lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("Alpha"),   "alpha",    6, ACCENT,  lambda v, it: f"{v:.1f}" if v else "\u2014"),
    (_("RPS"),     "rps",      5, FG_DIM,  lambda v, it: f"{v:.2f}" if v else "\u2014"),
    (_("Ammo"),    "ammo",     5, FG,      lambda v, it: f"{int(v)}" if v else "\u2014"),
]

MISSILE_TABLE_COLS = [
    (_("Name"),    "name",       12, FG,      lambda v, it: it["name"]),
    (_("Track"),   "tracking",    3, FG_DIM,  lambda v, it: str(v)[:2].upper() if v else "\u2014"),
    (_("Dmg\u2193"),   "total_dmg",   7, RED,     lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("Speed"),   "speed",       6, FG_DIM,  lambda v, it: f"{v:.0f}" if v else "\u2014"),
    (_("Range"),   "lock_range",  6, YELLOW,  lambda v, it: f"{v/1000:.1f}k" if v else "\u2014"),
    (_("Lock"),    "lock_time",   5, FG_DIM,  lambda v, it: f"{v:.1f}s" if v else "\u2014"),
]

SHIELD_TABLE_COLS = [
    (_("Name"),    "name",           12, FG,         lambda v, it: it["name"]),
    (_("Class"),   "class",           5, FG_DIM,     lambda v, it: str(v) if v else "\u2014"),
    (_("HP\u2193"),    "hp",               7, PURPLE,     lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("Reg/s"),   "regen",           6, GREEN,      lambda v, it: f"{v:.1f}" if v else "\u2014"),
    (_("Phys"),    "res_phys_max",    5, PHYS_COL,   lambda v, it: pct(v)),
    (_("Enrg"),    "res_energy_max",  5, ENERGY_COL, lambda v, it: pct(v)),
    (_("Dist"),    "res_dist_max",    5, DIST_COL,   lambda v, it: pct(v)),
    (_("Power"),   "power_draw",      6, ORANGE,     lambda v, it: f"{v/1000:.1f}" if v else "\u2014"),
    (_("EM"),      "em_max",          5, YELLOW,     lambda v, it: f"{v:,.0f}" if v else "\u2014"),
]

COOLER_TABLE_COLS = [
    (_("Name"),    "name",         12, FG,       lambda v, it: it["name"]),
    (_("Class"),   "class",         5, FG_DIM,   lambda v, it: str(v) if v else "\u2014"),
    (_("Cool\u2193"),  "cooling_rate",   7, GREEN,    lambda v, it: f"{v:,.0f}" if v else "\u2014"),
    (_("Pwr"),     "power_draw",    6, ORANGE,   lambda v, it: f"{v/1000:.1f}" if v else "\u2014"),
    (_("IR"),      "ir_max",        5, THERM_COL, lambda v, it: f"{v:.0f}" if v else "\u2014"),
    (_("EM"),      "em_max",        5, YELLOW,    lambda v, it: f"{v:.0f}" if v else "\u2014"),
    (_("HP"),      "hp",            5, PHYS_COL,  lambda v, it: f"{v:.0f}" if v else "\u2014"),
]

RADAR_TABLE_COLS = [
    (_("Name"),    "name",          12, FG,       lambda v, it: it["name"]),
    (_("Class"),   "class",          5, FG_DIM,   lambda v, it: str(v) if v else "\u2014"),
    (_("Det\u2193"),   "detection_min",   6, GREEN,    lambda v, it: f"{v:.0f}" if v else "\u2014"),
    (_("Max"),     "detection_max",   6, FG,       lambda v, it: f"{v:.0f}" if v else "\u2014"),
    (_("Power"),   "power_draw",      6, ORANGE,   lambda v, it: f"{v/1000:.1f}" if v else "\u2014"),
    (_("EM"),      "em_max",          5, YELLOW,   lambda v, it: f"{v:.0f}" if v else "\u2014"),
    (_("HP"),      "hp",              5, PHYS_COL, lambda v, it: f"{v:.0f}" if v else "\u2014"),
]
