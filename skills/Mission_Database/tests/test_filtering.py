"""Tests for services.filtering — contract and location filtering."""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.models import FilterState, ResourceFilterState
from services.filtering import (
    filter_contracts, filter_locations,
    is_ace, is_asd, is_wikelo, is_blueprint, matches_pseudo_category,
)


# ── Pseudo-category detection tests ─────────────────────────────────────────

class TestIsAce:
    def test_ace_detected(self):
        c = {
            "debugName": "BountyHunt_Normal",
            "shipEncounters": {
                "spawnConfig": {
                    "groups": [
                        {"role": "AcePilot", "spawnChance": 50},
                    ],
                },
            },
        }
        assert is_ace(c) is True

    def test_ambush_not_ace(self):
        c = {
            "debugName": "ShipAmbush_Normal",
            "shipEncounters": {
                "spawnConfig": {
                    "groups": [
                        {"role": "AcePilot", "spawnChance": 50},
                    ],
                },
            },
        }
        assert is_ace(c) is False

    def test_no_ship_encounters(self):
        assert is_ace({}) is False

    def test_zero_spawn_chance(self):
        c = {
            "debugName": "Test",
            "shipEncounters": {
                "spawnConfig": {
                    "groups": [{"role": "AcePilot", "spawnChance": 0}],
                },
            },
        }
        assert is_ace(c) is False


class TestIsAsd:
    def test_facility_delve(self):
        assert is_asd({"debugName": "Hockrow_FacilityDelve_Alpha"}) is True

    def test_asd_prefix(self):
        assert is_asd({"debugName": "Hockrow_ASD_Bravo"}) is True

    def test_non_matching(self):
        assert is_asd({"debugName": "Regular_Mission"}) is False


class TestIsWikelo:
    def test_wikelo_detected(self):
        c = {"factionGuid": "guid-w"}
        factions = {"guid-w": {"name": "Wikelo Emporium"}}
        assert is_wikelo(c, factions) is True

    def test_non_wikelo(self):
        c = {"factionGuid": "guid-other"}
        factions = {"guid-other": {"name": "Crusader Security"}}
        assert is_wikelo(c, factions) is False


class TestIsBlueprint:
    def test_has_blueprint_reward(self):
        c = {"blueprintRewards": [{"blueprintPool": "pool-1"}]}
        pools = {"pool-1": {"items": []}}
        assert is_blueprint(c, pools) is True

    def test_no_reward(self):
        assert is_blueprint({}, {}) is False

    def test_pool_not_resolved(self):
        c = {"blueprintRewards": [{"blueprintPool": "pool-missing"}]}
        assert is_blueprint(c, {}) is False


# ── filter_contracts tests ───────────────────────────────────────────────────

class TestFilterContracts:
    def _contracts(self):
        return [
            {
                "title": "Delivery Run",
                "description": "Deliver cargo",
                "debugName": "Delivery_01",
                "category": "career",
                "systems": ["Stanton"],
                "missionType": "Delivery",
                "factionGuid": "guid-cs",
                "illegal": False,
                "canBeShared": True,
                "rewardUEC": 5000,
            },
            {
                "title": "Bounty Hunt",
                "description": "Eliminate target",
                "debugName": "Bounty_01",
                "category": "career",
                "systems": ["Pyro"],
                "missionType": "Combat",
                "factionGuid": "guid-pyro",
                "illegal": True,
                "canBeShared": False,
                "rewardUEC": 15000,
            },
            {
                "title": "Story Mission",
                "description": "A story",
                "debugName": "Story_01",
                "category": "story",
                "systems": ["Stanton", "Pyro"],
                "missionType": "Investigation",
                "factionGuid": "guid-cs",
                "illegal": False,
                "canBeShared": True,
                "rewardUEC": 25000,
            },
        ]

    def _factions(self):
        return {
            "guid-cs": {"name": "Crusader Security"},
            "guid-pyro": {"name": "Pyro Mercs"},
        }

    def test_no_filters(self):
        result = filter_contracts(
            self._contracts(), FilterState(),
            self._factions(), [], {}, {},
        )
        assert len(result) == 3

    def test_filter_by_search(self):
        result = filter_contracts(
            self._contracts(), FilterState(search="bounty"),
            self._factions(), [], {}, {},
        )
        assert len(result) == 1
        assert result[0]["title"] == "Bounty Hunt"

    def test_filter_by_category(self):
        result = filter_contracts(
            self._contracts(), FilterState(categories={"story"}),
            self._factions(), [], {}, {},
        )
        assert len(result) == 1
        assert result[0]["category"] == "story"

    def test_filter_by_system(self):
        result = filter_contracts(
            self._contracts(), FilterState(systems={"Pyro"}),
            self._factions(), [], {}, {},
        )
        # "Bounty Hunt" is Pyro-only, "Story Mission" is in both
        assert len(result) == 2

    def test_filter_by_mission_type(self):
        result = filter_contracts(
            self._contracts(), FilterState(mission_type="Combat"),
            self._factions(), [], {}, {},
        )
        assert len(result) == 1

    def test_filter_by_faction(self):
        result = filter_contracts(
            self._contracts(), FilterState(factions={"Crusader Security"}),
            self._factions(), [], {}, {},
        )
        assert len(result) == 2  # Delivery + Story

    def test_filter_legal_only(self):
        result = filter_contracts(
            self._contracts(), FilterState(legality="legal"),
            self._factions(), [], {}, {},
        )
        assert all(not c.get("illegal") for c in result)
        assert len(result) == 2

    def test_filter_illegal_only(self):
        result = filter_contracts(
            self._contracts(), FilterState(legality="illegal"),
            self._factions(), [], {}, {},
        )
        assert len(result) == 1
        assert result[0]["illegal"] is True

    def test_filter_sharable(self):
        result = filter_contracts(
            self._contracts(), FilterState(sharing="sharable"),
            self._factions(), [], {}, {},
        )
        assert all(c.get("canBeShared") for c in result)

    def test_filter_solo(self):
        result = filter_contracts(
            self._contracts(), FilterState(sharing="solo"),
            self._factions(), [], {}, {},
        )
        assert all(not c.get("canBeShared") for c in result)

    def test_filter_by_reward_range(self):
        result = filter_contracts(
            self._contracts(), FilterState(reward_min=10000, reward_max=20000),
            self._factions(), [], {}, {},
        )
        assert len(result) == 1
        assert result[0]["rewardUEC"] == 15000


# ── filter_locations tests ───────────────────────────────────────────────────

class TestFilterLocations:
    def _locations(self):
        return [
            {
                "locationName": "Aberdeen",
                "locationType": "Moon",
                "system": "Stanton",
                "groups": [{"groupName": "SpaceShip_Mineables", "deposits": []}],
            },
            {
                "locationName": "Hurston",
                "locationType": "Planet",
                "system": "Stanton",
                "groups": [{"groupName": "FPS_Mineables", "deposits": []}],
            },
            {
                "locationName": "Pyro IV",
                "locationType": "Planet",
                "system": "Pyro",
                "groups": [{"groupName": "SpaceShip_Mineables", "deposits": []}],
            },
        ]

    def _get_resources(self, loc_name):
        resources = {
            "Aberdeen": [{"resource": "Gold"}, {"resource": "Copper"}],
            "Hurston": [{"resource": "Hadanite"}],
            "Pyro IV": [{"resource": "Quantainium"}],
        }
        return resources.get(loc_name, [])

    def test_no_filters(self):
        result = filter_locations(
            self._locations(), ResourceFilterState(),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 3

    def test_filter_by_system(self):
        result = filter_locations(
            self._locations(), ResourceFilterState(systems={"Stanton"}),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 2

    def test_filter_by_location_type(self):
        result = filter_locations(
            self._locations(), ResourceFilterState(location_types={"Moon"}),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 1
        assert result[0]["locationName"] == "Aberdeen"

    def test_filter_by_deposit_type(self):
        result = filter_locations(
            self._locations(), ResourceFilterState(deposit_types={"FPS_Mineables"}),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 1
        assert result[0]["locationName"] == "Hurston"

    def test_filter_by_resource_any(self):
        result = filter_locations(
            self._locations(),
            ResourceFilterState(resources={"Gold", "Hadanite"}, match_mode="any"),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 2  # Aberdeen and Hurston

    def test_filter_by_resource_all(self):
        result = filter_locations(
            self._locations(),
            ResourceFilterState(resources={"Gold", "Copper"}, match_mode="all"),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 1  # Only Aberdeen has both

    def test_hidden_locations_excluded(self):
        result = filter_locations(
            self._locations(), ResourceFilterState(),
            self._get_resources, {}, frozenset({"Aberdeen"}),
        )
        assert len(result) == 2
        assert all(loc["locationName"] != "Aberdeen" for loc in result)

    def test_filter_by_search(self):
        result = filter_locations(
            self._locations(), ResourceFilterState(search="hurston"),
            self._get_resources, {}, frozenset(),
        )
        assert len(result) == 1
        assert result[0]["locationName"] == "Hurston"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
