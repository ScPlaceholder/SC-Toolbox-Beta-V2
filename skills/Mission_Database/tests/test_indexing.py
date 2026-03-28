"""Tests for services.indexing — contract and mining index building."""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.indexing import index_contracts, index_mining, get_location_resources


# ── index_contracts tests ────────────────────────────────────────────────────

class TestIndexContracts:
    def _make_data(self, contracts=None, legacy=None, factions=None):
        return {
            "contracts": contracts or [],
            "legacyContracts": legacy or [],
            "factions": factions or {},
            "locationPools": {},
            "shipPools": {},
            "blueprintPools": {},
            "scopes": {},
            "availabilityPools": [],
            "factionRewardsPools": [],
            "partialRewardPayoutPools": [],
        }

    def test_empty_data(self):
        idx = index_contracts(self._make_data())
        assert idx["contracts"] == []
        assert idx["all_categories"] == []
        assert idx["min_reward"] == 0
        assert idx["max_reward"] == 0

    def test_contracts_merged_with_legacy(self):
        data = self._make_data(
            contracts=[{"title": "Delivery A"}],
            legacy=[{"title": "Old Mission"}],
        )
        idx = index_contracts(data)
        assert len(idx["contracts"]) == 2
        assert len(idx["legacy_contracts"]) == 1
        assert idx["legacy_contracts"][0].get("_legacy") is True

    def test_categories_extracted(self):
        data = self._make_data(contracts=[
            {"category": "career"},
            {"category": "story"},
            {"category": "career"},  # duplicate
        ])
        idx = index_contracts(data)
        assert idx["all_categories"] == ["career", "story"]

    def test_systems_extracted(self):
        data = self._make_data(contracts=[
            {"systems": ["Stanton", "Pyro"]},
            {"systems": ["Stanton"]},
        ])
        idx = index_contracts(data)
        assert "Stanton" in idx["all_systems"]
        assert "Pyro" in idx["all_systems"]

    def test_mission_types_extracted(self):
        data = self._make_data(contracts=[
            {"missionType": "Delivery"},
            {"missionType": "Combat"},
        ])
        idx = index_contracts(data)
        assert "Combat" in idx["all_mission_types"]
        assert "Delivery" in idx["all_mission_types"]

    def test_reward_range(self):
        data = self._make_data(contracts=[
            {"rewardUEC": 1000},
            {"rewardUEC": 5000},
            {"rewardUEC": 250},
        ])
        idx = index_contracts(data)
        assert idx["min_reward"] == 250
        assert idx["max_reward"] == 5000

    def test_faction_names_extracted(self):
        data = self._make_data(
            contracts=[{"factionGuid": "guid-1"}],
            factions={"guid-1": {"name": "Crusader Security"}},
        )
        idx = index_contracts(data)
        assert "Crusader Security" in idx["all_faction_names"]
        assert idx["faction_by_guid"]["guid-1"]["name"] == "Crusader Security"

    def test_contracts_as_dict_converted(self):
        """If contracts is a dict (keyed by GUID), it should be flattened to a list."""
        data = self._make_data()
        data["contracts"] = {"guid-a": {"title": "A"}, "guid-b": {"title": "B"}}
        idx = index_contracts(data)
        assert len(idx["contracts"]) == 2

    def test_pools_passed_through(self):
        data = self._make_data()
        data["locationPools"] = {"pool-1": {"name": "Stanton"}}
        data["blueprintPools"] = {"bp-1": {"items": []}}
        idx = index_contracts(data)
        assert "pool-1" in idx["location_pools"]
        assert "bp-1" in idx["blueprint_pools"]


# ── index_mining tests ───────────────────────────────────────────────────────

class TestIndexMining:
    def test_empty_data(self):
        idx = index_mining([], {})
        assert idx["all_resource_names"] == []
        assert idx["resource_to_locations"] == {}

    def test_basic_mining_index(self):
        locations = [
            {
                "locationName": "Aberdeen",
                "locationType": "Moon",
                "system": "Stanton",
                "groups": [
                    {
                        "groupName": "SpaceShip_Mineables",
                        "deposits": [
                            {
                                "compositionGuid": "comp-1",
                                "relativeProbability": 100,
                            },
                        ],
                    },
                ],
            },
        ]
        compositions = {
            "comp-1": {
                "parts": [
                    {"elementName": "Gold", "minPercent": 5, "maxPercent": 15},
                    {"elementName": "Copper", "minPercent": 10, "maxPercent": 25},
                ],
            },
        }
        idx = index_mining(locations, compositions, hidden_locations=frozenset())
        assert "Gold" in idx["all_resource_names"]
        assert "Copper" in idx["all_resource_names"]
        assert "Aberdeen" in idx["location_to_resources"]
        assert len(idx["resource_to_locations"]["Gold"]) == 1
        assert idx["resource_to_locations"]["Gold"][0]["location"] == "Aberdeen"
        assert "Moon" in idx["all_location_types"]
        assert "Stanton" in idx["all_mining_systems"]

    def test_hidden_locations_excluded(self):
        locations = [
            {
                "locationName": "Ship Graveyard",
                "locationType": "POI",
                "system": "Stanton",
                "groups": [],
            },
        ]
        idx = index_mining(locations, {}, hidden_locations=frozenset({"Ship Graveyard"}))
        assert "Ship Graveyard" not in idx["location_to_resources"]

    def test_harvestable_preset(self):
        """Harvestables use presetName instead of composition parts."""
        locations = [
            {
                "locationName": "Hurston",
                "locationType": "Planet",
                "system": "Stanton",
                "groups": [
                    {
                        "groupName": "Harvestables",
                        "deposits": [
                            {"presetName": "Blue Bilva", "relativeProbability": 50},
                            {"presetName": "Golden Medmon", "relativeProbability": 50},
                        ],
                    },
                ],
            },
        ]
        idx = index_mining(locations, {}, hidden_locations=frozenset())
        assert "Blue Bilva" in idx["all_resource_names"]
        assert "Golden Medmon" in idx["all_resource_names"]

    def test_resource_categories_built(self):
        locations = [
            {
                "locationName": "Aberdeen",
                "locationType": "Moon",
                "system": "Stanton",
                "groups": [
                    {
                        "groupName": "FPS_Mineables",
                        "deposits": [
                            {"compositionGuid": "c1", "relativeProbability": 100},
                        ],
                    },
                ],
            },
        ]
        compositions = {
            "c1": {"parts": [{"elementName": "Hadanite", "minPercent": 1, "maxPercent": 5}]},
        }
        idx = index_mining(locations, compositions, hidden_locations=frozenset())
        assert "FPS Mining" in idx["resource_categories"]
        assert "Hadanite" in idx["resource_categories"]["FPS Mining"]


# ── get_location_resources tests ─────────────────────────────────────────────

class TestGetLocationResources:
    def test_basic(self):
        l2r = {
            "Aberdeen": [
                {"resource": "Gold", "group": "Ship", "min_pct": 5, "max_pct": 15},
                {"resource": "Copper", "group": "Ship", "min_pct": 10, "max_pct": 25},
            ],
        }
        result = get_location_resources(l2r, "Aberdeen")
        assert len(result) == 2
        # Sorted by max_pct descending
        assert result[0]["resource"] == "Copper"

    def test_deduplicates_keeps_highest(self):
        l2r = {
            "Aberdeen": [
                {"resource": "Gold", "group": "Ship", "min_pct": 1, "max_pct": 5},
                {"resource": "Gold", "group": "Rare", "min_pct": 3, "max_pct": 10},
            ],
        }
        result = get_location_resources(l2r, "Aberdeen")
        assert len(result) == 1
        assert result[0]["max_pct"] == 10

    def test_missing_location(self):
        assert get_location_resources({}, "Nowhere") == []


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
