"""UI constants — re-exports from shared palette + layout constants."""
from typing import Dict

import shared.path_setup  # noqa: E402  # centralised path config
from shared.qt.theme import P
from shared.i18n import s_ as _

# ── Layout constants ──────────────────────────────────────────────────────────
SIDEBAR_WIDTH = 195
STATS_PANEL_WIDTH = 268
MAX_PINNED_CARDS = 5

# ── Stat display definitions ─────────────────────────────────────────────────
# (key, label, unit, good_direction: 1=positive is good, -1=negative is good, 0=neutral)
STATS_DISPLAY = [
    ("min_power",     _("Min Power"),     " aUEC", 0),
    ("max_power",     _("Max Power"),     " aUEC", 0),
    ("ext_power",     _("Ext Power"),     " aUEC", 0),
    None,  # separator
    ("opt_range",     _("Opt Range"),     " m",    0),
    ("max_range",     _("Max Range"),     " m",    0),
    None,
    ("resistance",    _("Resistance"),    "%",     1),
    ("instability",   _("Instability"),   "%",    -1),
    ("inert",         _("Inert Mat."),    "%",    -1),
    None,
    ("charge_window", _("Opt Chrg Wnd"),  "%",     1),
    ("charge_rate",   _("Opt Chrg Rate"), "%",     0),
    ("overcharge",    _("Overcharge"),    "%",    -1),
    ("cluster",       _("Cluster"),       "%",     0),
    ("shatter",       _("Shatter"),       "%",    -1),
]

# ── Stat label map (for clipboard copy) ──────────────────────────────────────
STAT_LABEL_MAP = {
    "min_power": _("Min Power"),
    "max_power": _("Max Power"),
    "ext_power": _("Ext Power"),
    "opt_range": _("Opt Range"),
    "max_range": _("Max Range"),
    "resistance": _("Resistance"),
    "instability": _("Instability"),
    "inert": _("Inert Mat."),
    "charge_window": _("Opt Chrg Wnd"),
    "charge_rate": _("Opt Chrg Rate"),
    "overcharge": _("Overcharge"),
    "cluster": _("Cluster"),
    "shatter": _("Shatter"),
}
