"""Which missions drop a blueprint — recovered by joining the datamine to the Mission Database.

``datamine.py`` says in its own docstring that the drop list is "not in the datamine
(sc-craft.tools had them)". That is true of ``blueprints.json`` alone: a blueprint there
only names its reward POOLS. But the pools are identified by the same UUIDs the Mission
Database's scmdb cache uses, so the mission, the contractor, the locations and the drop
chance are all still reachable. Nothing was lost; it is a join.

THE CHAIN, measured 2026-09-26 against the pinned 4.10.1-LIVE.12660092 datamine and this
repo's ``skills/Mission_Database/.scmdb_cache.json``::

    blueprintPools[pool_uuid].blueprints[].blueprintRecord   == datamine record ``UUID``
        687 of 687 exact  (name matching lost 6 to renames between 4.7 and 4.10)
    contracts[].blueprintRewards[].blueprintPool             == pool_uuid
        826 links, 116 of the datamine's 154 pools
    contracts[].factionGuid  -> factions[guid].name          (the contractor)
    contracts[].locations[]  -> locationPools[id].name       (where it is offered)

    drop chance = blueprintRewards[].chance * weight / sum(weights in that pool)

Both factors are real fields. Neither is invented, and neither is normalised.

⚠ FOUR THINGS THAT LOOK LIKE POLISH AND ARE ACTUALLY CORRECTNESS:

1. THE CACHE IS CHOSEN BY CONTENT, NEVER BY FILENAME. ``pick_cache`` ranks by how many
   blueprintPools a file holds. The obvious "current" name is not reliably the richest —
   in another checkout of this project ``.scmdb_cache.json`` is 4.6.0 with an EMPTY
   blueprintPools, which would make this module silently return nothing while looking
   like it worked. A filename is a claim; a pool count is evidence.

2. JOIN ON ``blueprintRecord`` (a UUID), NOT ON THE ITEM NAME. Names drift between game
   versions: the UUID join is 687/687 where the name join orphans 6. ``entityClass``
   matches the datamine's ``Output.UUID`` and is kept as a secondary key for callers that
   only have the output item.

3. COVERAGE IS PARTIAL AND MUST STAY VISIBLE. Measured end to end against this repo:
   **645 of 1,607 blueprints get drop data.** A blueprint with no entry returns ``[]``,
   which means NO DATA — not "does not drop". The caller must render those differently;
   conflating them tells the user something false.
   ⚠ And the shortfall has TWO causes, which I only separated by running it: 38 of the
   datamine's 154 pools have no scmdb entry at all, AND of the 116 pools that do, only
   **87 are actually awarded by a contract**. A pool can list blueprints and still be
   reachable from no mission, so "the pool joined" does not imply "a mission gives it".
   I predicted 666 from pool membership alone and the real answer is 645; the gap is
   precisely those unawarded pools.

4. AN UNNAMED REWARD SLOT KEEPS ITS WEIGHT. Some pools contain a slot with a weight and no
   blueprint. It consumes probability, so it stays in the denominator and the named chances
   in that pool sum to LESS than 1 on purpose. Normalising it away would inflate every
   named blueprint's chance to cover an outcome that is not a blueprint at all.

⚠ VERSION SKEW is real and is reported rather than hidden: the blueprints are 4.10.1-LIVE
and the drop data is whatever ``source_version`` says. Show it in the UI instead of
implying the drop list is current.

⛔⛔ THE CACHE THIS READS IS **NOT SHIPPED**, AND ON A FRESH INSTALL THERE IS NONE.
  Found 2026-09-26, immediately after wiring this up and testing it green on a dev box —
  which is the whole problem. ``build_installer.bat`` deletes ``.scmdb_cache*.json`` from
  staging (correctly: it is a downloaded cache, not source), and
  ``Mission_Database/data/cache.py`` names even its crafting variant
  ``.scmdb_cache_crafting_*`` explicitly so the same rule covers it.
  ⇒ So on a user's machine this returns None until **Mission Database** has run and
    fetched its cache. Craft Database therefore has a SILENT CROSS-SKILL DEPENDENCY: the
    drop list is complete here and absent there, and nothing on screen explains why.
  ★ This is the failure this file spends four notes warning about, arriving one level up:
    every guard here distinguishes "no data" from "does not drop" *per blueprint*, and
    none of them noticed that on a fresh install the answer is "no data" for ALL of them
    for a reason the user can do something about. A correct per-item verdict is not a
    correct product.
  ⇒ ``build_index`` records ``dropData.source = None`` when this returns None, so the
    absence is already carried in the index and the UI can say WHY rather than showing an
    empty section. Wiring that message is the open piece; J's call whether the fix is that
    message, Craft Database triggering the fetch, or shipping a seed cache.
"""
from __future__ import annotations

import glob
import json
import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# How many location names to show before summarising. Contracts routinely list dozens of
# spawn points; printing all of them buries the mission name.
LOCATION_CAP = 6


def _mission_db_dir() -> str:
    """``skills/Mission_Database`` — a sibling of this skill."""
    here = os.path.dirname(os.path.abspath(__file__))          # .../Craft_Database/data
    skills = os.path.dirname(os.path.dirname(here))            # .../skills
    return os.path.join(skills, "Mission_Database")


def pick_cache(directory: Optional[str] = None) -> tuple[Optional[str], Optional[dict]]:
    """The scmdb cache with the MOST blueprintPools, plus its parsed contents.

    Ranked by (pools, named entries, version) so the choice is a property of the data and
    not of how the filenames happen to sort — several caches can tie on pool count.
    Returns ``(None, None)`` when there is nothing usable, and logs why. A missing or
    empty cache is a normal, survivable state: the Craft Database simply shows no drops.
    """
    directory = directory or _mission_db_dir()
    best: tuple = (-1, -1, "")
    best_path = best_doc = None
    for path in sorted(glob.glob(os.path.join(directory, ".scmdb_cache*.json"))):
        try:
            with open(path, encoding="utf-8") as fh:
                doc = json.load(fh)
        except Exception as exc:                       # a corrupt cache must not be fatal
            log.warning("mission_drops: cannot read %s (%s)", os.path.basename(path), exc)
            continue
        pools = doc.get("blueprintPools") or {}
        if not isinstance(pools, dict):
            continue
        named = sum(1 for p in pools.values()
                    for e in (p.get("blueprints") or []) if e.get("blueprintRecord"))
        key = (len(pools), named, str(doc.get("version") or ""))
        if key > best:
            best, best_path, best_doc = key, path, doc
    if not best_path or best[0] <= 0:
        log.info("mission_drops: no scmdb cache with reward pools under %s", directory)
        return None, None
    log.info("mission_drops: using %s (%d pools)", os.path.basename(best_path), best[0])
    return best_path, best_doc


class MissionDrops:
    """blueprint UUID -> the missions that award it.

    Built once, then queried per blueprint while the index is generated.
    """

    def __init__(self, doc: dict, path: str = "") -> None:
        self.path = path
        self.source_version = str(doc.get("version") or "unknown")
        pools: dict = doc.get("blueprintPools") or {}
        factions: dict = doc.get("factions") or {}
        locpools: dict = doc.get("locationPools") or {}

        # pool_uuid -> (total weight incl. unnamed slots, unattributed share)
        self._pool_weight: dict[str, float] = {}
        self._pool_unattributed: dict[str, float] = {}
        # blueprint UUID -> [(pool_uuid, weight)]
        self._by_record: dict[str, list[tuple[str, float]]] = {}
        # output item UUID -> blueprint UUID, a secondary key (see note 2)
        self._by_entity: dict[str, str] = {}

        for pool_uuid, pool in pools.items():
            entries = pool.get("blueprints") or []
            total = float(sum(float(e.get("weight") or 0) for e in entries))
            unnamed = float(sum(float(e.get("weight") or 0) for e in entries
                                if not e.get("blueprintRecord")))
            self._pool_weight[pool_uuid] = total
            self._pool_unattributed[pool_uuid] = (unnamed / total) if total else 0.0
            for e in entries:
                rec = e.get("blueprintRecord")
                if not rec:
                    continue                    # an unnamed slot: weight already counted above
                self._by_record.setdefault(rec, []).append((pool_uuid, float(e.get("weight") or 0)))
                ent = e.get("entityClass")
                if ent:
                    self._by_entity.setdefault(ent, rec)

        # pool_uuid -> [mission dicts], built once so a blueprint lookup is a dict hit
        self._pool_missions: dict[str, list[dict]] = {}
        for contract in doc.get("contracts") or []:
            rewards = contract.get("blueprintRewards") or []
            if not rewards:
                continue
            base = self._contract_fields(contract, factions, locpools)
            for reward in rewards:
                pool_uuid = reward.get("blueprintPool")
                if not pool_uuid:
                    continue
                chance = reward.get("chance")
                self._pool_missions.setdefault(pool_uuid, []).append(
                    dict(base, _chance=(float(chance) if chance is not None else None)))

    # ── one contract -> the fields domain.models.Mission.from_dict reads ──────────

    @staticmethod
    def _contract_fields(contract: dict, factions: dict, locpools: dict) -> dict:
        names: list[str] = []
        for loc_id in contract.get("locations") or []:
            name = (locpools.get(loc_id) or {}).get("name")
            # ⚠ 9 of the 911 location pools carry an UNRESOLVED localisation key as their
            #   name ('@generic_locations_blank', '@RR_P3_L2_Clinic', ...). Rendering one
            #   puts a raw key in front of the user, which reads as a bug and tells them
            #   nothing. Drop them: an omitted location is honest, a '@' string is not.
            if name and not name.startswith("@") and name not in names:
                names.append(name)
        shown, extra = names[:LOCATION_CAP], max(0, len(names) - LOCATION_CAP)
        locations = ", ".join(shown)
        if extra:
            # Say it was truncated. Silently showing six of forty implies the mission is
            # only offered in six places.
            locations += " (+%d more)" % extra

        faction = factions.get(contract.get("factionGuid") or "") or {}
        return {
            "name": contract.get("title") or contract.get("debugName") or "",
            "contractor": faction.get("name") or "",
            "mission_type": contract.get("missionType") or "",
            "category": contract.get("category") or "",
            # `illegal` is a bool here; the model wants `lawful`.
            "lawful": 0 if contract.get("illegal") else 1,
            "not_for_release": 0,
            "locations": locations,
            "description": contract.get("description") or "",
            "time_to_complete_minutes": contract.get("timeToComplete") or 0,
        }

    # ── queries ──────────────────────────────────────────────────────────────────

    def for_blueprint(self, blueprint_uuid: Optional[str],
                      output_uuid: Optional[str] = None) -> list[dict]:
        """Missions that award this blueprint, newest-shape dicts ready for Mission.from_dict.

        An empty list means THIS JOIN HAS NO DATA for that blueprint, which is not the same
        as "it does not drop". 38 of the datamine's pools have no scmdb entry at all.
        """
        memberships = self._by_record.get(blueprint_uuid or "")
        if not memberships and output_uuid:
            rec = self._by_entity.get(output_uuid)
            if rec:
                memberships = self._by_record.get(rec)
        if not memberships:
            return []

        out: list[dict] = []
        for pool_uuid, weight in memberships:
            total = self._pool_weight.get(pool_uuid) or 0.0
            # Guard the denominator rather than emitting a tidy 0.0 — a zero-weight pool is
            # a data question, and 0.0 would read as "cannot drop".
            share = (weight / total) if total else None
            for mission in self._pool_missions.get(pool_uuid) or []:
                chance = mission.get("_chance")
                if chance is None or share is None:
                    continue        # cannot state a probability; omit rather than invent one
                row = {k: v for k, v in mission.items() if k != "_chance"}
                row["drop_chance"] = chance * share
                out.append(row)
        # Best chance first: the answer to "where do I get this" is the likeliest source.
        out.sort(key=lambda m: (-m["drop_chance"], m["name"]))
        return out

    def unattributed_share(self, pool_uuid: str) -> float:
        """Fraction of a pool held by unnamed slots (see note 4). Usually 0.0."""
        return self._pool_unattributed.get(pool_uuid, 0.0)

    @property
    def pool_count(self) -> int:
        return len(self._pool_weight)

    @property
    def linked_pool_count(self) -> int:
        """Pools that at least one contract awards. A pool nothing awards yields no missions."""
        return len(self._pool_missions)

    @property
    def blueprint_count(self) -> int:
        return len(self._by_record)


def load(directory: Optional[str] = None) -> Optional[MissionDrops]:
    """``MissionDrops`` from the best available cache, or ``None`` if there is none.

    Never raises on missing or corrupt data: the Craft Database must open and work with no
    drop information, showing it as unknown.
    """
    path, doc = pick_cache(directory)
    if not doc:
        return None
    try:
        return MissionDrops(doc, path or "")
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("mission_drops: join failed (%s) — continuing with no drop data", exc)
        return None
