"""Pure data-indexing functions — no UI or class dependencies.

Each function receives raw data and returns indexed lookup structures.
"""

from __future__ import annotations

from collections import defaultdict

from config import HIDDEN_LOCATIONS


def index_contracts(data: dict) -> dict:
    """Index all mission data for fast filtering.

    Parameters
    ----------
    data : dict
        Raw JSON payload from the scmdb API (or cache).

    Returns
    -------
    dict with keys:
        contracts, legacy_contracts, factions, location_pools, ship_pools,
        blueprint_pools, scopes, availability_pools, faction_rewards_pools,
        partial_reward_pools, faction_by_guid, all_categories, all_systems,
        all_mission_types, all_faction_names, min_reward, max_reward.
    """
    contracts = data.get("contracts", [])
    if isinstance(contracts, dict):
        contracts = list(contracts.values())
    legacy = data.get("legacyContracts", [])
    if isinstance(legacy, dict):
        legacy = list(legacy.values())
    # Mark legacy contracts so we can distinguish them in the UI
    for c in legacy:
        c["_legacy"] = True
    # Merge both lists -- scmdb.net shows both in the same view
    all_contracts = contracts + legacy
    legacy_contracts = legacy

    factions = data.get("factions", {})
    location_pools = data.get("locationPools", {})
    ship_pools = data.get("shipPools", {})
    blueprint_pools = data.get("blueprintPools", {})
    scopes = data.get("scopes", {})
    availability_pools = data.get("availabilityPools", [])
    faction_rewards_pools = data.get("factionRewardsPools", [])
    partial_reward_pools = data.get("partialRewardPayoutPools", [])

    # Build faction GUID lookup
    faction_by_guid: dict = {}
    if isinstance(factions, dict):
        for guid, f in factions.items():
            faction_by_guid[guid] = f

    # Collect unique values for filters
    cats: set[str] = set()
    systems: set[str] = set()
    types: set[str] = set()
    fnames: set[str] = set()
    rewards: list[int] = []

    for c in all_contracts:
        cat = c.get("category", "")
        if cat:
            cats.add(cat)
        for s in (c.get("systems") or []):
            if s:
                systems.add(s)
        mt = c.get("missionType", "")
        if mt:
            types.add(mt)
        fg = c.get("factionGuid", "")
        if fg and fg in faction_by_guid:
            fnames.add(faction_by_guid[fg].get("name", ""))
        r = c.get("rewardUEC")
        if r is not None and isinstance(r, (int, float)):
            rewards.append(int(r))

    return {
        "contracts": all_contracts,
        "legacy_contracts": legacy_contracts,
        "factions": factions,
        "location_pools": location_pools,
        "ship_pools": ship_pools,
        "blueprint_pools": blueprint_pools,
        "scopes": scopes,
        "availability_pools": availability_pools,
        "faction_rewards_pools": faction_rewards_pools,
        "partial_reward_pools": partial_reward_pools,
        "faction_by_guid": faction_by_guid,
        "all_categories": sorted(cats),
        "all_systems": sorted(systems),
        "all_mission_types": sorted(types),
        "all_faction_names": sorted(fnames),
        "min_reward": min(rewards) if rewards else 0,
        "max_reward": max(rewards) if rewards else 0,
    }


def index_mining(
    locations: list,
    compositions: dict,
    hidden_locations: frozenset[str] = HIDDEN_LOCATIONS,
) -> dict:
    """Build resource-to-location and location-to-resource lookups.

    Parameters
    ----------
    locations : list
        Mining location dicts from the API.
    compositions : dict
        Composition GUID -> composition dict mapping.
    hidden_locations : frozenset
        Location names to exclude (defaults to ``config.HIDDEN_LOCATIONS``).

    Returns
    -------
    dict with keys:
        resource_to_locations, location_to_resources, all_resource_names,
        all_location_types, all_mining_systems, resource_categories.
    """
    r2l: dict[str, list] = defaultdict(list)   # resource -> locations
    l2r: dict[str, list] = defaultdict(list)   # location -> resources
    res_cats: dict[str, set] = defaultdict(set) # category_label -> {resource_names}
    all_types: set[str] = set()
    all_systems: set[str] = set()

    cat_labels = {
        "SpaceShip_Mineables": "Ores",
        "SpaceShip_Mineables_Rare": "Ores",
        "GroundVehicle_Mineables": "Vehicle Mining",
        "FPS_Mineables": "FPS Mining",
        "Harvestables": "Plants",
    }

    for loc in locations:
        loc_name = loc.get("locationName", "")
        if loc_name in hidden_locations:
            continue

        loc_type = loc.get("locationType", "")
        system = loc.get("system", "")
        all_types.add(loc_type)
        all_systems.add(system)

        for group in loc.get("groups", []):
            grp_name = group.get("groupName", "")
            deposits = group.get("deposits", [])
            total_prob = sum(d.get("relativeProbability", 0) for d in deposits)

            for dep in deposits:
                comp_guid = dep.get("compositionGuid", "")
                comp = compositions.get(comp_guid, {})
                dep_prob = dep.get("relativeProbability", 0) / total_prob if total_prob else 0

                # Harvestables use presetName instead of compositions
                preset_name = dep.get("presetName", "")
                if preset_name and not comp.get("parts"):
                    entry = {
                        "location": loc_name,
                        "system": system,
                        "type": loc_type,
                        "group": grp_name,
                        "min_pct": 0,
                        "max_pct": dep_prob * 100,
                        "probability": dep_prob,
                    }
                    r2l[preset_name].append(entry)
                    l2r[loc_name].append({
                        "resource": preset_name,
                        "group": grp_name,
                        "min_pct": 0,
                        "max_pct": dep_prob * 100,
                    })
                    cat = cat_labels.get(grp_name, "")
                    if cat:
                        res_cats[cat].add(preset_name)
                    continue

                for part in comp.get("parts", []):
                    elem_name = part.get("elementName", "")
                    if not elem_name:
                        continue
                    min_pct = part.get("minPercent", 0)
                    max_pct = part.get("maxPercent", 0)

                    entry = {
                        "location": loc_name,
                        "system": system,
                        "type": loc_type,
                        "group": grp_name,
                        "min_pct": min_pct,
                        "max_pct": max_pct,
                        "probability": dep_prob,
                    }
                    r2l[elem_name].append(entry)
                    l2r[loc_name].append({
                        "resource": elem_name,
                        "group": grp_name,
                        "min_pct": min_pct,
                        "max_pct": max_pct,
                    })

                    # Categorize resource by group type
                    cat = cat_labels.get(grp_name, "")
                    if cat:
                        res_cats[cat].add(elem_name)

    return {
        "resource_to_locations": dict(r2l),
        "location_to_resources": dict(l2r),
        "all_resource_names": sorted(r2l.keys()),
        "all_location_types": sorted(all_types),
        "all_mining_systems": sorted(all_systems),
        "resource_categories": {k: sorted(v) for k, v in res_cats.items()},
    }


def get_location_resources(location_to_resources: dict, loc_name: str) -> list:
    """Get deduplicated resources for a location, sorted by max_pct desc.

    Parameters
    ----------
    location_to_resources : dict
        The ``location_to_resources`` mapping from :func:`index_mining`.
    loc_name : str
        Location name to look up.

    Returns
    -------
    list[dict]
        Deduplicated resource entries sorted by ``max_pct`` descending.
    """
    entries = location_to_resources.get(loc_name, [])
    # Deduplicate by resource name, keeping max percentages
    best: dict[str, dict] = {}
    for e in entries:
        name = e["resource"]
        if name not in best or e["max_pct"] > best[name]["max_pct"]:
            best[name] = e
    return sorted(best.values(), key=lambda x: -x["max_pct"])
