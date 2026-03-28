"""Data models for mining equipment and ship configurations."""
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ── Attribute name constants (UEX API) ────────────────────────────────────────
ATTR_MINING_POWER = "Mining Laser Power"
ATTR_EXT_POWER = "Extraction Laser Power"
ATTR_OPT_RANGE = "Optimal Range"
ATTR_MAX_RANGE = "Maximum Range"
ATTR_RESISTANCE = "Resistance"
ATTR_INSTABILITY = "Laser Instability"
ATTR_INERT = "Inert Material Level"
ATTR_CHARGE_WINDOW = "Optimal Charge Window Size"
ATTR_CHARGE_RATE = "Optimal Charge Window Rate"
ATTR_CHARGE_RATE_MODULE = "Optimal Charge Rate"
ATTR_MODULE_SLOTS = "Module Slots"
ATTR_OVERCHARGE = "Catastrophic Charge Rate"
ATTR_SHATTER = "Shatter Damage"
ATTR_ITEM_TYPE = "Item Type"
ATTR_USES = "Uses"
ATTR_DURATION = "Duration"
ATTR_SIZE = "Size"
ATTR_CLUSTER = "Cluster Modifier"

# UEX API category IDs
CATEGORY_LASERS = 29
CATEGORY_MODULES = 30
CATEGORY_GADGETS = 28


@dataclass
class LaserItem:
    """A mining laser head."""
    id: int
    name: str
    size: int                       # 1 or 2 (0 = unspecified)
    company: str
    min_power: float                # aUEC
    max_power: float                # aUEC
    ext_power: Optional[float]      # aUEC (extraction laser power)
    opt_range: Optional[float]      # m
    max_range: Optional[float]      # m
    resistance: Optional[float]     # % modifier
    instability: Optional[float]    # % modifier
    inert: Optional[float]          # % modifier
    charge_window: Optional[float]  # % modifier
    charge_rate: Optional[float]    # % modifier
    module_slots: int               # default 2
    price: float = 0.0              # min buy price aUEC


@dataclass
class ModuleItem:
    """A mining laser module (passive or active)."""
    id: int
    name: str
    item_type: str                  # "Active" or "Passive"
    power_pct: Optional[float]      # multiplier x100 (e.g. 135 = +35%)
    ext_power_pct: Optional[float]  # multiplier x100
    resistance: Optional[float]
    instability: Optional[float]
    inert: Optional[float]
    charge_rate: Optional[float]
    charge_window: Optional[float]
    overcharge: Optional[float]
    shatter: Optional[float]
    uses: int
    duration: Optional[float]       # seconds (active only)
    price: float = 0.0


@dataclass
class GadgetItem:
    """A mining gadget (consumable)."""
    id: int
    name: str
    charge_window: Optional[float]
    charge_rate: Optional[float]
    instability: Optional[float]
    resistance: Optional[float]
    cluster: Optional[float]
    price: float = 0.0


@dataclass
class ShipConfig:
    """Configuration for a mining ship type."""
    name: str
    turrets: int
    laser_size: int
    module_slots: int
    turret_names: List[str] = field(default_factory=list)
    stock_laser: str = ""


# ── Ship definitions ──────────────────────────────────────────────────────────
SHIPS: Dict[str, ShipConfig] = {
    "Prospector": ShipConfig(
        name="Prospector",
        turrets=1,
        laser_size=1,
        module_slots=2,
        turret_names=["Main Turret"],
        stock_laser="Arbor MH1 Mining Laser",
    ),
    "MOLE": ShipConfig(
        name="MOLE",
        turrets=3,
        laser_size=2,
        module_slots=2,
        turret_names=["Front Turret", "Port Turret", "Starboard Turret"],
        stock_laser="Arbor MH2 Mining Laser",
    ),
    "Golem": ShipConfig(
        name="Golem",
        turrets=1,
        laser_size=1,
        module_slots=2,
        turret_names=["Main Turret"],
        stock_laser="Pitman Mining Laser",
    ),
}

# ── Placeholder strings ──────────────────────────────────────────────────────
NONE_LASER = "— No Laser —"
NONE_MODULE = "— No Module —"
NONE_GADGET = "— No Gadget —"
