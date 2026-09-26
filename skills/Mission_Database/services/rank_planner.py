"""Pure rank-path-planner logic — no UI dependencies.

Given a faction, scope, rank range, and optional system filter, compute the
optimal mission-repeat plan to advance through each rank tier.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Optional

from data.models import RankPathResult, TierStep, contract_availability


def clean_rank_name(name: str) -> str:
    """Strip localisation-key prefixes (``@...``) from rank names."""
    if name.startswith("@"):
        return name.split("_")[-1] if "_" in name else name
    return name


def get_faction_scope(
    faction_guid: str,
    contracts: list,
    faction_rewards_pools: list,
) -> str:
    """Return the primary scope GUID used by *faction_guid*.

    Scans the faction's contracts, looks up each one's reward-pool entry,
    and returns the most-common ``scopeGuid``.  Returns ``""`` if none found.
    """
    scope_counts: Counter = Counter()
    for c in contracts:
        if c.get("factionGuid") != faction_guid:
            continue
        fri = c.get("factionRewardsIndex")
        if fri is None:
            continue
        try:
            pool = faction_rewards_pools[fri]
        except (IndexError, TypeError):
            continue
        if isinstance(pool, list):
            for entry in pool:
                sg = entry.get("scopeGuid", "") if isinstance(entry, dict) else ""
                if sg:
                    scope_counts[sg] += 1
    if not scope_counts:
        return ""
    return scope_counts.most_common(1)[0][0]


def get_faction_systems(faction_guid: str, contracts: list) -> list[str]:
    """Return sorted unique system names for contracts belonging to *faction_guid*."""
    systems: set[str] = set()
    for c in contracts:
        if c.get("factionGuid") != faction_guid:
            continue
        for s in c.get("systems") or []:
            if s:
                systems.add(s)
    return sorted(systems)


def get_rep_for_contract(
    contract: dict,
    faction_rewards_pools: list,
    scope_guid: str,
) -> int:
    """Return the XP reward for *contract* within *scope_guid*, or ``0``."""
    fri = contract.get("factionRewardsIndex")
    if fri is None:
        return 0
    try:
        pool = faction_rewards_pools[fri]
    except (IndexError, TypeError):
        return 0
    if not isinstance(pool, list):
        return 0
    for entry in pool:
        if not isinstance(entry, dict):
            continue
        if entry.get("scopeGuid") == scope_guid:
            return entry.get("amount", 0) or 0
    # Fallback: if only one entry, use it regardless of scope match
    if len(pool) == 1 and isinstance(pool[0], dict):
        return pool[0].get("amount", 0) or 0
    return 0


def compute_rank_path(
    faction_guid: str,
    scope_guid: str,
    from_rank_index: int,
    to_rank_index: int,
    system_filter: Optional[str],
    contracts: list,
    faction_rewards_pools: list,
    availability_pools: list,
    scopes: dict,
) -> RankPathResult:
    """Compute the full rank-progression plan.

    Parameters
    ----------
    faction_guid : str
        GUID of the faction to plan for.
    scope_guid : str
        GUID of the reputation scope (from :func:`get_faction_scope`).
    from_rank_index, to_rank_index : int
        0-based rank indices (inclusive start, exclusive end handled internally).
    system_filter : str or None
        If set, only consider missions available in this star system.
    contracts, faction_rewards_pools, availability_pools, scopes
        Data snapshots from :class:`MissionDataManager`.

    Returns
    -------
    RankPathResult
    """
    scope_obj = scopes.get(scope_guid, {})
    scope_name = scope_obj.get("scopeName", "")
    ranks = sorted(scope_obj.get("ranks", []), key=lambda r: r.get("rankIndex", 0))

    if not ranks or from_rank_index >= to_rank_index:
        return RankPathResult(scope_name=scope_name)

    # Pre-filter: faction contracts (optionally by system)
    faction_contracts = [c for c in contracts if c.get("factionGuid") == faction_guid]
    if system_filter:
        faction_contracts = [
            c for c in faction_contracts if system_filter in (c.get("systems") or [])
        ]

    # Build rank lookup
    rank_by_idx = {r.get("rankIndex", 0): r for r in ranks}

    steps: list[TierStep] = []

    for rank_idx in range(from_rank_index, to_rank_index):
        rank = rank_by_idx.get(rank_idx, {})
        next_rank = rank_by_idx.get(rank_idx + 1, {})
        rep_needed = rank.get("rangeXP", 0) or 0

        from_name = clean_rank_name(rank.get("name", "?"))
        to_name = clean_rank_name(next_rank.get("name", "?"))

        # Missions accessible at this rank
        accessible = [
            c for c in faction_contracts
            if ((c.get("minStanding") or {}).get("rankIndex", 0) or 0) <= rank_idx
        ]

        repeatables: list[tuple[dict, int]] = []
        one_times: list[tuple[dict, int]] = []

        for c in accessible:
            rep = get_rep_for_contract(c, faction_rewards_pools, scope_guid)
            if rep <= 0:
                continue

            # Determine if one-time.  Record-first: availabilityPools is [{}]
            # upstream, so the pool read made every once-only mission look
            # repeatable and inflated the plan (issue #22b).
            is_once = bool(contract_availability(c, availability_pools)
                           .get("onceOnly", False))

            if is_once:
                one_times.append((c, rep))
            else:
                repeatables.append((c, rep))

        # Sort repeatables by rep descending
        repeatables.sort(key=lambda x: -x[1])

        best_contract = repeatables[0][0] if repeatables else {}
        best_rep = repeatables[0][1] if repeatables else 0
        runs = math.ceil(rep_needed / best_rep) if best_rep > 0 else 0

        steps.append(TierStep(
            from_rank_name=from_name,
            to_rank_name=to_name,
            from_rank_index=rank_idx,
            to_rank_index=rank_idx + 1,
            rep_needed=rep_needed,
            best_repeatable=best_contract,
            best_rep_per_run=best_rep,
            repeats_needed=runs,
            one_time_missions=one_times,
            all_repeatables=repeatables,
        ))

    total_runs = sum(s.repeats_needed for s in steps)
    total_once = sum(len(s.one_time_missions) for s in steps)

    return RankPathResult(
        steps=steps,
        total_repeatable_runs=total_runs,
        total_one_time_missions=total_once,
        scope_name=scope_name,
    )
