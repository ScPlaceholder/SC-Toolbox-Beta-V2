"""Unit tests for filter_service."""

import sys
import os

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from domain.models import Blueprint, IngredientSlot, Mission
from services.filter_service import matches_search, filter_blueprints, group_categories


def _bp(name="Test", category="Weapons / Sniper", ingredients=None, missions=None):
    return Blueprint(
        id=1,
        blueprint_id="bp_test",
        name=name,
        category=category,
        ingredients=ingredients or [],
        missions=missions or [],
    )


def _slot(name="Tungsten", qty=0.06):
    return IngredientSlot(slot="FRAME", name=name, quantity_scu=qty)


def _mission(name="M", mission_type="Bounty Hunter", locations="Pyro",
             contractor="BHG", lawful=1):
    return Mission(name=name, mission_type=mission_type, locations=locations,
                   contractor=contractor, lawful=lawful)


# ── matches_search ───────────────────────────────────────────────────────


class TestMatchesSearch:
    def test_empty_query_matches_all(self):
        assert matches_search(_bp("Anything"), "") is True

    def test_name_match(self):
        assert matches_search(_bp("A03 Sniper Rifle"), "sniper") is True

    def test_name_no_match(self):
        assert matches_search(_bp("A03 Sniper Rifle"), "shotgun") is False

    def test_category_match(self):
        assert matches_search(_bp(category="Armour / Combat"), "armour") is True

    def test_ingredient_name_match(self):
        bp = _bp(ingredients=[_slot("Tungsten")])
        assert matches_search(bp, "tungsten") is True

    def test_ingredient_option_match(self):
        from domain.models import IngredientOption
        slot = IngredientSlot(
            slot="FRAME", name="Tungsten",
            options=[IngredientOption(guid="x", name="Hadanite", quantity_scu=0.1)],
        )
        bp = _bp(ingredients=[slot])
        assert matches_search(bp, "hadanite") is True

    def test_case_insensitive(self):
        assert matches_search(_bp("ADP Core"), "adp") is True


# ── filter_blueprints ────────────────────────────────────────────────────


class TestFilterBlueprints:
    @pytest.fixture
    def blueprints(self):
        return [
            _bp("Rifle", "Weapons / Sniper",
                 ingredients=[_slot("Tungsten")],
                 missions=[_mission(mission_type="Mercenary", locations="Pyro")]),
            _bp("Armor Core", "Armour / Combat / Heavy",
                 ingredients=[_slot("Ouratite")],
                 missions=[_mission(mission_type="Delivery", locations="Stanton",
                                    contractor="Shubin")]),
            _bp("Helmet", "Armour / Combat / Light",
                 ingredients=[_slot("Tungsten"), _slot("Aslarite")],
                 missions=[]),
        ]

    def test_no_filters(self, blueprints):
        result = filter_blueprints(blueprints)
        assert len(result) == 3

    def test_search_filter(self, blueprints):
        result = filter_blueprints(blueprints, search="rifle")
        assert len(result) == 1
        assert result[0].name == "Rifle"

    def test_category_type_filter(self, blueprints):
        result = filter_blueprints(blueprints, category_type="Armour")
        assert len(result) == 2

    def test_resource_filter(self, blueprints):
        result = filter_blueprints(blueprints, resource="Tungsten")
        assert len(result) == 2

    def test_mission_type_filter(self, blueprints):
        result = filter_blueprints(blueprints, mission_type="Mercenary")
        assert len(result) == 1
        assert result[0].name == "Rifle"

    def test_location_filter(self, blueprints):
        result = filter_blueprints(blueprints, location="Stanton")
        assert len(result) == 1
        assert result[0].name == "Armor Core"

    def test_contractor_filter(self, blueprints):
        result = filter_blueprints(blueprints, contractor="Shubin")
        assert len(result) == 1

    def test_combined_filters(self, blueprints):
        result = filter_blueprints(blueprints, category_type="Armour", resource="Tungsten")
        assert len(result) == 1
        assert result[0].name == "Helmet"

    def test_no_results(self, blueprints):
        result = filter_blueprints(blueprints, search="nonexistent")
        assert len(result) == 0


# ── group_categories ─────────────────────────────────────────────────────


class TestGroupCategories:
    def test_basic_grouping(self):
        cats = ["Weapons / Sniper", "Weapons / Rifle", "Armour / Light"]
        groups = group_categories(cats)
        assert "Weapons" in groups
        assert "Armour" in groups
        assert set(groups["Weapons"]) == {"Rifle", "Sniper"}
        assert groups["Armour"] == ["Light"]

    def test_no_subcategory(self):
        groups = group_categories(["Ammo"])
        assert groups["Ammo"] == []

    def test_empty(self):
        assert group_categories([]) == {}

    def test_deep_categories(self):
        cats = ["Armour / Combat / Heavy", "Armour / Combat / Light"]
        groups = group_categories(cats)
        assert "Armour" in groups
        assert "Combat / Heavy" in groups["Armour"]
        assert "Combat / Light" in groups["Armour"]

    def test_no_duplicates(self):
        cats = ["Weapons / Sniper", "Weapons / Sniper"]
        groups = group_categories(cats)
        assert groups["Weapons"] == ["Sniper"]
