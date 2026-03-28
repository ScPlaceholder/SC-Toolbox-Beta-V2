"""UI constants for Craft Database."""

from shared.qt.theme import P

TOOL_COLOR = "#44ccbb"
TOOL_NAME = "Craft Database"
TOOL_ID = "craft_db"

# Card colors
CARD_BG = P.bg_card
CARD_BORDER = P.border_card
CARD_HOVER_BORDER = TOOL_COLOR

# Ingredient tag colors
TAG_COLORS = {
    "Taranite": "#aa66ff",
    "Hephaestanite": "#aa66ff",
    "Ouratite": "#44aaff",
    "Aslarite": "#44aaff",
    "Stileron": "#33dd88",
    "Tungsten": "#ffaa22",
    "Iron": "#ff7733",
    "Copper": "#ff7733",
    "Hadanite": "#ff5533",
    "Laranite": "#aa66ff",
    "Agricium": "#33ccdd",
    "Lindinium": "#33ccdd",
    "default": "#5a6480",
}

# Stat effect colors
STAT_POSITIVE = P.green
STAT_NEGATIVE = P.red
STAT_NEUTRAL = P.fg_dim

# Mission lawfulness
LAWFUL_COLOR = P.green
UNLAWFUL_COLOR = P.red

# Category type colors
CATEGORY_COLORS = {
    "Armour": "#44aaff",
    "Weapons": "#ff7733",
    "Ammo": "#ffaa22",
}

POLL_MS = 150
PAGE_SIZE = 50
