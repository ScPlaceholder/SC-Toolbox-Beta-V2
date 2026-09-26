"""Data models and type aliases."""
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

# Type aliases for documentation — contracts/blueprints stay as dicts from JSON
Contract = dict
Blueprint = dict
Location = dict
Faction = dict


def text_field(d: Optional[dict], key: str, default: str = "") -> str:
    """Read ``key`` from ``d`` as a string, treating JSON ``null`` as absent.

    ``dict.get(key, default)`` only supplies the default when the key is
    MISSING.  scmdb.net ships keys that are PRESENT with a null value -- on
    4.10.1 that is 5 blueprints with ``productName: null``, 4 crafting items
    with ``name: null``, 15 blueprints with ``subtype: null`` and
    ``manufacturer: null``, and 4 with ``type: null`` -- so ``.get`` handed
    ``None`` on to callers that immediately did ``.lower()``, ``.replace()``
    or ``.title()``.  That was issue #24: typing one character into the
    Fabricator search raised ``AttributeError: 'NoneType' object has no
    attribute 'lower'`` on the first null-named blueprint (index 1026).

    Use this for any upstream text field about to be treated as a str.
    """
    if not d:
        return default
    value = d.get(key)
    if value is None:
        return default
    return value if isinstance(value, str) else str(value)


_AVAILABILITY_FIELDS = (
    "onceOnly",
    "canReacceptAfterAbandoning",
    "canReacceptAfterFailing",
    "availableInPrison",
    "hasPersonalCooldown",
    "personalCooldownTime",
    "abandonedCooldownTime",
)


def contract_availability(contract: Optional[dict],
                          availability_pools=None) -> dict:
    """Availability facts for one contract, read from the CONTRACT (issue #22).

    ``availabilityPools`` on scmdb.net 4.10.1 is the single-element list
    ``[{}]``, so ``availability_pools[contract["availabilityIndex"]]`` returns
    an empty dict for every one of the 1,533 contracts.  Four boolean flags in
    the Requirements tab were therefore hardcoded "No", and the COOLDOWN
    section never rendered -- while the same facts sat on the contract record
    itself and disagreed in 897 of 6,132 flag cells.

    The record wins where it carries the field; the pool is merged underneath
    it so this keeps working if scmdb.net ever populates the pools again.
    """
    merged: dict = {}
    if availability_pools is not None and contract is not None:
        try:
            pool = availability_pools[contract.get("availabilityIndex")]
        except (IndexError, KeyError, TypeError):
            pool = None
        if isinstance(pool, dict):
            merged.update(pool)
    if contract:
        for key in _AVAILABILITY_FIELDS:
            if contract.get(key) is not None:
                merged[key] = contract[key]
    return merged


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
    rank_min: int = 0
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
    # Issue #21: show only blueprints a mission reward pool hands out.
    # Defaults to False so the dataclass keeps its old behaviour for every
    # existing caller and test; the Fabricator UI ships it CHECKED, matching
    # the sibling Craft Database's "Obtainable" checkbox.
    obtainable_only: bool = False


@dataclass
class ResourceFilterState:
    """Snapshot of resource page filter values."""
    search: str = ""
    systems: Set[str] = field(default_factory=set)
    location_types: Set[str] = field(default_factory=set)
    deposit_types: Set[str] = field(default_factory=set)
    resources: Set[str] = field(default_factory=set)
    match_mode: str = "any"


@dataclass
class TierStep:
    """One rank-to-rank transition in a rank path plan."""
    from_rank_name: str
    to_rank_name: str
    from_rank_index: int
    to_rank_index: int
    rep_needed: int
    best_repeatable: dict = field(default_factory=dict)
    best_rep_per_run: int = 0
    repeats_needed: int = 0
    one_time_missions: List[Tuple[dict, int]] = field(default_factory=list)
    all_repeatables: List[Tuple[dict, int]] = field(default_factory=list)


@dataclass
class RankPathResult:
    """Complete rank path computation result."""
    steps: List[TierStep] = field(default_factory=list)
    total_repeatable_runs: int = 0
    total_one_time_missions: int = 0
    scope_name: str = ""
    faction_name: str = ""
