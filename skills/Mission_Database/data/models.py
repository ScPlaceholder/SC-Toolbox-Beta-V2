"""Data models and type aliases."""
from dataclasses import dataclass, field
from typing import Set

# Type aliases for documentation — contracts/blueprints stay as dicts from JSON
Contract = dict
Blueprint = dict
Location = dict
Faction = dict


@dataclass
class FilterState:
    """Immutable snapshot of all mission filter values."""
    search: str = ""
    categories: Set[str] = field(default_factory=set)
    systems: Set[str] = field(default_factory=set)
    mission_type: str = ""
    factions: Set[str] = field(default_factory=set)
    legality: str = ""
    sharing: str = ""
    availability: str = ""
    rank_max: int = 6
    reward_min: int = 0
    reward_max: int = 999999999


@dataclass
class FabFilterState:
    """Snapshot of fabricator filter values."""
    search: str = ""
    types: Set[str] = field(default_factory=set)
    subtypes: Set[str] = field(default_factory=set)
    armor_classes: Set[str] = field(default_factory=set)
    armor_slots: Set[str] = field(default_factory=set)
    manufacturers: Set[str] = field(default_factory=set)
    materials: Set[str] = field(default_factory=set)


@dataclass
class ResourceFilterState:
    """Snapshot of resource page filter values."""
    search: str = ""
    systems: Set[str] = field(default_factory=set)
    location_types: Set[str] = field(default_factory=set)
    deposit_types: Set[str] = field(default_factory=set)
    resources: Set[str] = field(default_factory=set)
    match_mode: str = "any"
