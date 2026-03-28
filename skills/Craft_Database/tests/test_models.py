"""Unit tests for domain models."""

import sys
import os

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

import pytest
from domain.models import (
    Blueprint,
    CraftStats,
    DamageResistance,
    FilterHints,
    FireMode,
    IngredientOption,
    IngredientSlot,
    ItemStats,
    Mission,
    MissionDifficulty,
    Pagination,
    QualityEffect,
    SpreadInfo,
    TemperatureResistance,
)


# ── IngredientOption ─────────────────────────────────────────────────────


class TestIngredientOption:
    def test_from_dict_full(self):
        d = {
            "guid": "abc-123",
            "name": "Tungsten",
            "quantity_scu": 0.06,
            "min_quality": 100,
            "unit": "scu",
            "loc_key": "items_commodities_tungsten",
        }
        opt = IngredientOption.from_dict(d)
        assert opt.guid == "abc-123"
        assert opt.name == "Tungsten"
        assert opt.quantity_scu == pytest.approx(0.06)
        assert opt.min_quality == 100
        assert opt.unit == "scu"
        assert opt.loc_key == "items_commodities_tungsten"

    def test_from_dict_defaults(self):
        opt = IngredientOption.from_dict({})
        assert opt.guid == ""
        assert opt.name == ""
        assert opt.quantity_scu == 0.0
        assert opt.min_quality == 0
        assert opt.unit == "scu"


# ── QualityEffect ────────────────────────────────────────────────────────


class TestQualityEffect:
    def test_from_dict(self):
        d = {
            "stat": "Recoil Smoothness",
            "quality_min": 0,
            "quality_max": 1000,
            "modifier_at_min": 1.2,
            "modifier_at_max": 0.8,
        }
        qe = QualityEffect.from_dict(d)
        assert qe.stat == "Recoil Smoothness"
        assert qe.modifier_at_min == pytest.approx(1.2)
        assert qe.modifier_at_max == pytest.approx(0.8)

    def test_modifier_at_min_boundary(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.modifier_at(0) == pytest.approx(1.2)

    def test_modifier_at_max_boundary(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.modifier_at(1000) == pytest.approx(0.8)

    def test_modifier_at_midpoint(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.modifier_at(500) == pytest.approx(1.0)

    def test_modifier_at_clamped_below(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.modifier_at(-100) == pytest.approx(1.2)

    def test_modifier_at_clamped_above(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.modifier_at(2000) == pytest.approx(0.8)

    def test_modifier_at_equal_bounds(self):
        qe = QualityEffect(stat="Test", quality_min=500, quality_max=500,
                           modifier_at_min=1.5, modifier_at_max=0.5)
        assert qe.modifier_at(500) == pytest.approx(0.5)

    def test_pct_at_midpoint(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.pct_at(500) == pytest.approx(0.0)

    def test_pct_at_min(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.pct_at(0) == pytest.approx(20.0)

    def test_pct_at_max(self):
        qe = QualityEffect(stat="Test", modifier_at_min=1.2, modifier_at_max=0.8)
        assert qe.pct_at(1000) == pytest.approx(-20.0)


# ── IngredientSlot ───────────────────────────────────────────────────────


class TestIngredientSlot:
    def test_from_dict(self):
        d = {
            "slot": "FRAME",
            "options": [{"guid": "x", "name": "Iron", "quantity_scu": 0.5}],
            "quality_effects": [{"stat": "Recoil", "modifier_at_min": 1.1, "modifier_at_max": 0.9}],
            "name": "Iron",
            "quantity_scu": 0.5,
        }
        slot = IngredientSlot.from_dict(d)
        assert slot.slot == "FRAME"
        assert len(slot.options) == 1
        assert slot.options[0].name == "Iron"
        assert len(slot.quality_effects) == 1
        assert slot.name == "Iron"
        assert slot.quantity_scu == pytest.approx(0.5)

    def test_from_dict_empty(self):
        slot = IngredientSlot.from_dict({})
        assert slot.slot == ""
        assert slot.options == []
        assert slot.quality_effects == []


# ── Mission ──────────────────────────────────────────────────────────────


class TestMission:
    def test_from_dict(self):
        d = {
            "name": "Test Mission",
            "contractor": "BHG",
            "mission_type": "Bounty Hunter",
            "lawful": 1,
            "drop_chance": "0.5000",
            "locations": "Pyro",
            "difficulty": {"mechanicalSkill": "Easy", "timeCommitment": "Short"},
        }
        m = Mission.from_dict(d)
        assert m.name == "Test Mission"
        assert m.contractor == "BHG"
        assert m.lawful == 1
        assert m.drop_chance == pytest.approx(0.5)
        assert m.difficulty.mechanical_skill == "Easy"

    def test_drop_pct(self):
        m = Mission(name="T", drop_chance=0.75)
        assert m.drop_pct == "75%"

    def test_drop_pct_full(self):
        m = Mission(name="T", drop_chance=1.0)
        assert m.drop_pct == "100%"

    def test_from_dict_no_difficulty(self):
        m = Mission.from_dict({"name": "T", "difficulty": None})
        assert m.difficulty.mechanical_skill == ""


# ── ItemStats ────────────────────────────────────────────────────────────


class TestItemStats:
    def test_weapon_from_dict(self):
        d = {
            "type": "weapon",
            "fire_modes": [{"name": "Single", "fire_rate": 225}],
            "mass_kg": 6,
            "overheat_temperature": 450,
        }
        stats = ItemStats.from_dict(d)
        assert stats.type == "weapon"
        assert len(stats.fire_modes) == 1
        assert stats.fire_modes[0].fire_rate == pytest.approx(225)
        assert stats.mass_kg == pytest.approx(6)

    def test_armor_from_dict(self):
        d = {
            "type": "armor",
            "damage_resistance": {"physical": 0.6, "energy": 0.6, "profile": "Heavy"},
            "temperature_resistance": {"min": -75, "max": 105},
        }
        stats = ItemStats.from_dict(d)
        assert stats.type == "armor"
        assert stats.damage_resistance.physical == pytest.approx(0.6)
        assert stats.damage_resistance.profile == "Heavy"
        assert stats.temperature_resistance.min_temp == pytest.approx(-75)
        assert stats.temperature_resistance.max_temp == pytest.approx(105)

    def test_from_dict_no_optional(self):
        stats = ItemStats.from_dict({})
        assert stats.damage_resistance is None
        assert stats.temperature_resistance is None
        assert stats.fire_modes == []


# ── Blueprint ────────────────────────────────────────────────────────────


class TestBlueprint:
    @pytest.fixture
    def sample_bp_dict(self):
        return {
            "id": 1017,
            "blueprint_id": "bp_test",
            "name": "A03 Sniper Rifle",
            "category": "Weapons / Sniper",
            "craft_time_seconds": 180,
            "tiers": 1,
            "default_owned": 0,
            "version": "LIVE-4.7.0",
            "ingredients": [
                {"slot": "FRAME", "options": [{"guid": "x", "name": "Taranite", "quantity_scu": 0.06}],
                 "quality_effects": [], "name": "Taranite", "quantity_scu": 0.06},
                {"slot": "STOCK", "options": [], "quality_effects": [], "name": "Iron", "quantity_scu": 0.03},
            ],
            "missions": [
                {"name": "Take Stronghold", "contractor": "CfP", "mission_type": "Mercenary",
                 "lawful": 1, "locations": "Pyro"},
            ],
        }

    def test_from_dict(self, sample_bp_dict):
        bp = Blueprint.from_dict(sample_bp_dict)
        assert bp.id == 1017
        assert bp.name == "A03 Sniper Rifle"
        assert bp.category == "Weapons / Sniper"
        assert bp.craft_time_seconds == 180
        assert len(bp.ingredients) == 2
        assert len(bp.missions) == 1

    def test_craft_time_display_seconds(self):
        bp = Blueprint(id=1, blueprint_id="t", name="T", craft_time_seconds=45)
        assert bp.craft_time_display == "45s"

    def test_craft_time_display_minutes(self):
        bp = Blueprint(id=1, blueprint_id="t", name="T", craft_time_seconds=180)
        assert bp.craft_time_display == "3m"

    def test_craft_time_display_hours_minutes(self):
        bp = Blueprint(id=1, blueprint_id="t", name="T", craft_time_seconds=3660)
        assert bp.craft_time_display == "1h 1m"

    def test_craft_time_display_hours_minutes_seconds(self):
        bp = Blueprint(id=1, blueprint_id="t", name="T", craft_time_seconds=3661)
        assert bp.craft_time_display == "1h 1m 1s"

    def test_category_type(self, sample_bp_dict):
        bp = Blueprint.from_dict(sample_bp_dict)
        assert bp.category_type == "Weapons"

    def test_category_subtype(self, sample_bp_dict):
        bp = Blueprint.from_dict(sample_bp_dict)
        assert bp.category_subtype == "Sniper"

    def test_category_type_empty(self):
        bp = Blueprint(id=1, blueprint_id="t", name="T", category="")
        assert bp.category_type == ""
        assert bp.category_subtype == ""

    def test_ingredient_names(self, sample_bp_dict):
        bp = Blueprint.from_dict(sample_bp_dict)
        assert bp.ingredient_names == ["Taranite", "Iron"]

    def test_mission_count(self, sample_bp_dict):
        bp = Blueprint.from_dict(sample_bp_dict)
        assert bp.mission_count == 1

    def test_from_dict_minimal(self):
        bp = Blueprint.from_dict({"id": 1, "blueprint_id": "x", "name": "Minimal"})
        assert bp.name == "Minimal"
        assert bp.ingredients == []
        assert bp.missions == []
        assert bp.tiers == 1


# ── FilterHints ──────────────────────────────────────────────────────────


class TestFilterHints:
    def test_from_dict(self):
        d = {
            "location": ["Pyro", "Stanton"],
            "mission_type": ["Bounty Hunter"],
            "contractor": ["BHG"],
            "resource": ["Tungsten"],
            "category": ["Weapons / Sniper"],
        }
        hints = FilterHints.from_dict(d)
        assert hints.locations == ["Pyro", "Stanton"]
        assert hints.mission_types == ["Bounty Hunter"]
        assert hints.contractors == ["BHG"]
        assert hints.resources == ["Tungsten"]
        assert hints.categories == ["Weapons / Sniper"]

    def test_from_dict_empty(self):
        hints = FilterHints.from_dict({})
        assert hints.locations == []
        assert hints.resources == []


# ── CraftStats ───────────────────────────────────────────────────────────


class TestCraftStats:
    def test_from_dict(self):
        d = {"totalBlueprints": 1040, "uniqueIngredients": 178, "version": "LIVE-4.7.0"}
        stats = CraftStats.from_dict(d)
        assert stats.total_blueprints == 1040
        assert stats.unique_ingredients == 178
        assert stats.version == "LIVE-4.7.0"

    def test_from_dict_defaults(self):
        stats = CraftStats.from_dict({})
        assert stats.total_blueprints == 0
        assert stats.version == ""


# ── Pagination ───────────────────────────────────────────────────────────


class TestPagination:
    def test_from_dict(self):
        d = {"page": 2, "limit": 50, "total": 364, "pages": 8}
        pag = Pagination.from_dict(d)
        assert pag.page == 2
        assert pag.limit == 50
        assert pag.total == 364
        assert pag.pages == 8

    def test_from_dict_defaults(self):
        pag = Pagination.from_dict({})
        assert pag.page == 1
        assert pag.pages == 1
