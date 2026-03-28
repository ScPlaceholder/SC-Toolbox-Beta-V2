"""Pure filtering functions — no UI or class dependencies.

Each function receives data snapshots and filter state, returns filtered lists.
"""

from __future__ import annotations

import re
from typing import Callable

from data.models import FabFilterState, FilterState, ResourceFilterState


# ── Pseudo-category detection (matches scmdb.net JS logic) ───────────────────


def is_ace(c: dict) -> bool:
    """ACE = shipEncounters has AcePilot group with spawnChance > 0, not ShipAmbush."""
    se = c.get("shipEncounters")
    if not se:
        return False
    sc = se.get("spawnConfig", {})
    groups = sc.get("groups", [])
    has_ace_pilot = any(
        g.get("role") == "AcePilot" and (g.get("spawnChance") or 0) > 0
        for g in groups
    )
    is_ambush = "ShipAmbush" in (c.get("debugName") or "")
    return has_ace_pilot and not is_ambush


def is_asd(c: dict) -> bool:
    """ASD = debugName matches /^Hockrow_(FacilityDelve|ASD)_/."""
    dn = c.get("debugName") or ""
    return bool(re.match(r"^Hockrow_(FacilityDelve|ASD)_", dn))


def is_wikelo(c: dict, faction_by_guid: dict) -> bool:
    """Wikelo = faction is 'Wikelo Emporium'."""
    fg = c.get("factionGuid", "")
    fn = faction_by_guid.get(fg, {}).get("name", "")
    return fn == "Wikelo Emporium"


def is_blueprint(c: dict, blueprint_pools: dict) -> bool:
    """Blueprint = has blueprintRewards with resolved pools."""
    bp = c.get("blueprintRewards") or c.get("itemRewards") or []
    if not bp:
        return False
    for reward in bp:
        if isinstance(reward, dict):
            pool_id = reward.get("blueprintPool", "")
            if pool_id and pool_id in blueprint_pools:
                return True
    return False


def matches_pseudo_category(
    c: dict, cat: str, faction_by_guid: dict, blueprint_pools: dict
) -> bool:
    """Check if contract matches a pseudo-category filter."""
    if cat == "ace":
        return is_ace(c)
    if cat == "asd":
        return is_asd(c)
    if cat == "wikelo":
        return is_wikelo(c, faction_by_guid)
    if cat == "blueprints":
        return is_blueprint(c, blueprint_pools)
    return False


# ── Contract filtering ───────────────────────────────────────────────────────


def filter_contracts(
    contracts: list,
    filters: FilterState,
    faction_by_guid: dict,
    availability_pools: list,
    scopes: dict,
    blueprint_pools: dict,
) -> list:
    """Apply all filters and return matching contracts.

    Parameters
    ----------
    contracts : list
        Flat list of contract dicts (both current and legacy).
    filters : FilterState
        Dataclass carrying every active filter value.
    faction_by_guid : dict
        GUID -> faction dict lookup.
    availability_pools : list
        Indexed availability pool entries.
    scopes : dict
        Scope data (used indirectly via rank logic).
    blueprint_pools : dict
        Blueprint pool lookup for pseudo-category detection.
    """
    results = []

    search = (filters.search or "").lower()
    categories = filters.categories
    systems = filters.systems
    mission_type = filters.mission_type
    factions = filters.factions
    legality = filters.legality          # "legal", "illegal", ""
    sharing = filters.sharing            # "sharable", "solo", ""
    availability = filters.availability  # "unique", "repeatable", ""
    rank_max = filters.rank_max
    reward_min = filters.reward_min
    reward_max = filters.reward_max

    for c in contracts:
        # Search
        if search:
            title = (c.get("title") or "").lower()
            desc = (c.get("description") or "").lower()
            debug = (c.get("debugName") or "").lower()
            if search not in title and search not in desc and search not in debug:
                continue

        # Category — real categories (career/story/event) + pseudo-categories
        if categories:
            PSEUDO = {"ace", "asd", "wikelo", "blueprints"}
            real_cats = categories - PSEUDO
            pseudo_cats = categories & PSEUDO
            matched = False
            # Check real categories
            if real_cats and c.get("category", "") in real_cats:
                matched = True
            # Check pseudo-categories (any match = include)
            if pseudo_cats:
                for pc in pseudo_cats:
                    if matches_pseudo_category(c, pc, faction_by_guid, blueprint_pools):
                        matched = True
                        break
            # If only real cats selected and no pseudo, or vice versa
            if not matched:
                continue

        # Systems ("Multi" = contracts with 2+ systems)
        if systems:
            c_sys = set(c.get("systems") or [])
            want = set(systems)
            if "Multi" in want:
                want.discard("Multi")
                # "Multi" matches contracts in 2+ systems
                if len(c_sys) >= 2:
                    pass  # multi match
                elif want and c_sys.intersection(want):
                    pass  # specific system match
                elif not want:
                    if len(c_sys) < 2:
                        continue
                else:
                    continue
            else:
                if not c_sys.intersection(want):
                    continue

        # Mission type
        if mission_type:
            if c.get("missionType", "") != mission_type:
                continue

        # Factions
        if factions:
            fg = c.get("factionGuid", "")
            fn = faction_by_guid.get(fg, {}).get("name", "")
            if fn not in factions:
                continue

        # Legality
        if legality == "legal" and c.get("illegal"):
            continue
        if legality == "illegal" and not c.get("illegal"):
            continue

        # Sharing
        if sharing == "sharable" and not c.get("canBeShared"):
            continue
        if sharing == "solo" and c.get("canBeShared"):
            continue

        # Availability
        if availability:
            try:
                avail = availability_pools[c.get("availabilityIndex")]
            except (IndexError, TypeError):
                avail = {}
            if availability == "unique" and not avail.get("onceOnly"):
                continue
            if availability == "repeatable" and avail.get("onceOnly"):
                continue

        # Rank
        ms = c.get("minStanding") or {}
        rank_idx = 0
        if isinstance(ms, dict):
            rank_idx = ms.get("rankIndex", 0) or 0
        if rank_idx > rank_max:
            continue

        # Reward
        reward = c.get("rewardUEC")
        if reward is not None:
            if isinstance(reward, (int, float)):
                if reward < reward_min or reward > reward_max:
                    continue

        results.append(c)

    return results


# ── Blueprint / Fabricator filtering ─────────────────────────────────────────


def filter_blueprints(
    blueprints: list,
    filters: FabFilterState,
    get_product_fn: Callable[[dict], dict | None],
    get_product_name_fn: Callable[[dict], str],
) -> list:
    """Filter crafting blueprints.

    Parameters
    ----------
    blueprints : list
        All crafting blueprint dicts.
    filters : FabFilterState
        Active filter state for the fabricator page.
    get_product_fn : callable
        ``(bp) -> product_dict | None`` — resolves a blueprint to its product.
    get_product_name_fn : callable
        ``(bp) -> str`` — returns the display name for a blueprint's product.
    """
    results = []

    search = (filters.search or "").lower()
    active_types = filters.types
    active_subtypes = filters.subtypes
    active_armor_class = filters.armor_classes
    active_armor_slot = filters.armor_slots
    active_mfr = filters.manufacturers
    active_material = filters.materials

    for bp in blueprints:
        prod = get_product_fn(bp)

        # Search
        if search:
            name = get_product_name_fn(bp).lower()
            tag = (bp.get("tag") or "").lower()
            if search not in name and search not in tag:
                continue

        # Type filter
        if active_types and bp.get("type", "") not in active_types:
            continue

        # Subtype filter
        if active_subtypes and bp.get("subtype", "") not in active_subtypes:
            continue

        # Armor class filter (Light/Medium/Heavy from item attachSubType)
        if active_armor_class and prod:
            ast = (prod.get("attachSubType", "") or "").title()
            tags = (prod.get("tags", "") or "").lower()
            item_classes: set[str] = set()
            if ast in ("Light", "Lightarmor"):
                item_classes.add("Light")
            elif ast == "Medium":
                item_classes.add("Medium")
            elif ast == "Heavy":
                item_classes.add("Heavy")
            # Fallback: check tags
            if "light" in tags or "kap_light" in tags:
                item_classes.add("Light")
            if "medium" in tags:
                item_classes.add("Medium")
            if "heavy" in tags:
                item_classes.add("Heavy")
            if not item_classes & active_armor_class:
                continue
        elif active_armor_class and not prod:
            continue

        # Armor slot filter (Helmet/Torso/Arms/Legs/Backpack/Undersuit)
        if active_armor_slot:
            name_lower = get_product_name_fn(bp).lower()
            item_slot = ""
            if "helmet" in name_lower or "helm" in name_lower:
                item_slot = "Helmet"
            elif "arms" in name_lower:
                item_slot = "Arms"
            elif "legs" in name_lower:
                item_slot = "Legs"
            elif "core" in name_lower or "torso" in name_lower:
                item_slot = "Torso"
            elif "backpack" in name_lower:
                item_slot = "Backpack"
            elif "undersuit" in name_lower:
                item_slot = "Undersuit"
            if item_slot not in active_armor_slot:
                continue

        # Manufacturer filter
        if active_mfr and prod:
            mfr_code = prod.get("manufacturerCode", "")
            if mfr_code not in active_mfr:
                continue
        elif active_mfr and not prod:
            continue

        # Material filter (resources + items used in recipe)
        if active_material:
            tiers = bp.get("tiers", [])
            bp_materials: set[str] = set()
            for tier in tiers:
                for slot in tier.get("slots", []):
                    for opt in slot.get("options", []):
                        rn = opt.get("resourceName", "")
                        if rn:
                            bp_materials.add(rn)
                        itn = opt.get("itemName", "")
                        if itn:
                            bp_materials.add(itn)
            if not bp_materials & active_material:
                continue

        results.append(bp)

    return results


# ── Location / Resource filtering ────────────────────────────────────────────


def filter_locations(
    locations: list,
    filters: ResourceFilterState,
    get_location_resources_fn: Callable[[str], list],
    mining_compositions: dict,
    hidden_locations: frozenset[str],
) -> list:
    """Filter mining/resource locations.

    Parameters
    ----------
    locations : list
        All mining location dicts.
    filters : ResourceFilterState
        Active filter state for the resource page.
    get_location_resources_fn : callable
        ``(loc_name) -> list[dict]`` — returns deduplicated resources for a location.
    mining_compositions : dict
        Composition data (unused directly but kept for interface consistency).
    hidden_locations : frozenset
        Location names to exclude from results.
    """
    results = []

    search = (filters.search or "").lower()
    active_systems = filters.systems
    active_loctypes = filters.location_types
    active_deptypes = filters.deposit_types
    selected_res = filters.resources
    match_mode = filters.match_mode

    for loc in locations:
        loc_name = loc.get("locationName", "")
        if loc_name in hidden_locations:
            continue

        system = loc.get("system", "")
        loc_type = loc.get("locationType", "")

        # System filter
        if active_systems and system not in active_systems:
            continue

        # Location type filter
        if active_loctypes and loc_type not in active_loctypes:
            continue

        # Deposit type filter
        if active_deptypes:
            group_names = {g.get("groupName", "") for g in loc.get("groups", [])}
            if not group_names.intersection(active_deptypes):
                continue

        # Get resources at this location
        loc_resources = get_location_resources_fn(loc_name)
        resource_names = {r["resource"] for r in loc_resources}

        # Resource filter
        if selected_res:
            if match_mode == "all":
                if not selected_res.issubset(resource_names):
                    continue
            else:  # any
                if not selected_res.intersection(resource_names):
                    continue

        # Search filter (match location name or resource names)
        if search:
            all_text = loc_name.lower() + " " + " ".join(r.lower() for r in resource_names)
            if search not in all_text:
                continue

        results.append(loc)

    return results
