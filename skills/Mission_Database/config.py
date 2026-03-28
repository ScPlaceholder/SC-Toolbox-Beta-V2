"""Centralized constants for the Mission Database app."""

from shared.qt.theme import P
from shared.api_config import SCMDB_BASE_URL, SCMDB_HEADERS, CACHE_TTL_ERKUL

# ── API ──────────────────────────────────────────────────────────────────────
SCMDB_BASE = SCMDB_BASE_URL
API_HEADERS = SCMDB_HEADERS
CACHE_TTL     = CACHE_TTL_ERKUL
CACHE_VERSION = 1

# ── Tag colours (background, foreground) ─────────────────────────────────────
TAG_COLORS = {
    "Delivery":       ("#1a3322", "#33cc88"),
    "Combat":         ("#331a1a", "#ff5533"),
    "Salvage":        ("#332a1a", "#ffaa22"),
    "Investigation":  ("#221133", "#aa66ff"),
    "Bounty Hunt":    ("#331a1a", "#ff5533"),
    "Rescue":         ("#1a2233", "#44aaff"),
    "Escort":         ("#1a2233", "#44aaff"),
    "Mercenary":      ("#331a1a", "#ff5533"),
    "Mining":         ("#332a1a", "#ffaa22"),
    "Racing":         ("#1a3322", "#33cc88"),
    "career":         ("#1a2233", "#44aaff"),
    "story":          ("#222228", "#888899"),
    "LEGAL":          ("#1a3322", "#33dd88"),
    "ILLEGAL":        ("#331a1a", "#ff5533"),
    "CHAIN":          ("#332a11", "#ffaa22"),
    "ONCE":           ("#332a11", "#ffaa22"),
    "Stanton":        ("#0a2218", "#33cc88"),
    "Pyro":           ("#331a0a", "#ff7733"),
    "Nyx":            ("#1a1a33", "#7777cc"),
    "Multi":          ("#222228", "#888899"),
}

# ── Hidden locations (excluded from UI lists) ───────────────────────────────
HIDDEN_LOCATIONS = frozenset({
    "Akiro Cluster", "Pyro Belt (Cool 1)", "Pyro Belt (Cool 2)",
    "Pyro Belt (Warm 1)", "Pyro Belt (Warm 2)", "Lagrange G",
    "Lagrange (Occupied)", "Asteroid Cluster (Low Yield)",
    "Asteroid Cluster (Medium Yield)", "Ship Graveyard", "Space Derelict",
})

# ── Mining / salvage group type metadata ────────────────────────────────────
MINING_GROUP_TYPES = {
    "SpaceShip_Mineables":       {"label": "Ship Mining",       "short": "Ship",       "icon": "\u26cf", "category": "mining"},
    "SpaceShip_Mineables_Rare":  {"label": "Ship Mining (Rare)","short": "Ship (Rare)","icon": "\u2b50", "category": "mining"},
    "FPS_Mineables":             {"label": "FPS Mining",        "short": "FPS",        "icon": "\u26cf", "category": "mining"},
    "GroundVehicle_Mineables":   {"label": "ROC Mining",        "short": "ROC",        "icon": "\U0001f69c","category": "mining"},
    "Harvestables":              {"label": "Harvesting",        "short": "Harvest",    "icon": "\U0001f33f","category": "mining"},
    "Salvage_FreshDerelicts":    {"label": "Derelict Salvage",  "short": "Wrecks",     "icon": "\U0001f6f8","category": "salvage"},
    "Salvage_BrokenShips_Poor":  {"label": "Debris (Small)",    "short": "S Debris",   "icon": "\u2699",  "category": "salvage"},
    "Salvage_BrokenShips_Normal":{"label": "Debris (Medium)",   "short": "M Debris",   "icon": "\u2699",  "category": "salvage"},
    "Salvage_BrokenShips_Elite": {"label": "Debris (Large)",    "short": "L Debris",   "icon": "\u2699",  "category": "salvage"},
}
