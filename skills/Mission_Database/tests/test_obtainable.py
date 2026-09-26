"""Regression tests for issue #21 -- unobtainable blueprints filled the Fabricator.

875 of 1,607 4.10.1 blueprints are handed out by no mission reward pool (867 if
you also count the 8 the datamine marks "known by default", which is the number
the sibling Craft Database reports).  The Fabricator showed them all, so a full
recipe like Demeco LMG sat in the grid with no way to obtain it.

The fix ports Craft Database's "Obtainable" checkbox: the join lives in
``services.indexing.index_reward_pool_blueprints`` and
``MissionDataManager.is_blueprint_obtainable``, which answers True / False /
None, where None is NOT KNOWN YET and must never be filtered out.
"""

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.manager import MissionDataManager  # noqa: E402
from data.models import FabFilterState  # noqa: E402
from services.filtering import filter_blueprints  # noqa: E402
from services.indexing import index_reward_pool_blueprints  # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────

# Demeco LMG: a full recipe that no reward pool hands out (the reported case).
BP_UNOBTAINABLE = {
    "tag": "BP_CRAFT_klwe_lmg_energy_01",
    "guid": "8dca7c5e-b5ca-42e2-83cb-7b7d5f937d89",
    "productEntityClass": "ec-demeco",
    "productName": "Demeco LMG",
    "type": "weapons",
    "subtype": "lmg",
}

# The camo variant, which a pool DOES hand out.
BP_BY_GUID = {
    "tag": "BP_CRAFT_klwe_lmg_energy_01_mr01",
    "guid": "7022945b-fa64-4478-ac6f-5575cf1ee34f",
    "productEntityClass": "ec-demeco-camo",
    "productName": 'Demeco "Purgatory Camo" LMG',
    "type": "weapons",
    "subtype": "lmg",
}

# Reachable only by entityClass — the pool entry lost its blueprintRecord.
BP_BY_ENTITY_CLASS = {
    "tag": "BP_CRAFT_EC_ONLY",
    "guid": "guid-not-in-any-pool",
    "productEntityClass": "ec-scraper",
    "productName": "Trawler Scraper Module",
}

# Reachable only by display name.
BP_BY_NAME = {
    "tag": "BP_CRAFT_NAME_ONLY",
    "guid": "also-not-in-a-pool",
    "productEntityClass": "ec-not-in-a-pool",
    "productName": "Hart Scraper Module",
}

POOLS = {
    "pool-a": {
        "name": "BP_REWARDS_Test",
        "blueprints": [
            {"blueprintRecord": "7022945b-fa64-4478-ac6f-5575cf1ee34f",
             "entityClass": "ec-demeco-camo",
             "name": 'Demeco "Purgatory Camo" LMG'},
            {"entityClass": "ec-scraper", "name": "Trawler Scraper Module"},
            {"name": "Hart Scraper Module"},
            "a bare string, not a dict",
        ],
    },
    "pool-b": {"name": "BP_REWARDS_Empty"},
    "pool-c": "not a dict at all",
}

ALL_BPS = [BP_UNOBTAINABLE, BP_BY_GUID, BP_BY_ENTITY_CLASS, BP_BY_NAME]


def _mgr(with_pools: bool = True):
    m = MissionDataManager()
    m.crafting_blueprints = list(ALL_BPS)
    m.crafting_items_map = {}
    m.crafting_loaded = True
    if with_pools:
        m.blueprint_pools = POOLS
        m.reward_pool_blueprints = index_reward_pool_blueprints(POOLS)
    return m


# ── The index ────────────────────────────────────────────────────────────

class TestRewardPoolIndex:
    def test_indexes_all_three_keys(self):
        idx = index_reward_pool_blueprints(POOLS)
        assert "7022945b-fa64-4478-ac6f-5575cf1ee34f" in idx["guids"]
        assert "ec-scraper" in idx["entity_classes"]
        assert "Hart Scraper Module" in idx["names"]

    def test_survives_junk_entries(self):
        idx = index_reward_pool_blueprints(POOLS)
        assert len(idx["guids"]) == 1

    def test_empty_input_gives_empty_sets(self):
        idx = index_reward_pool_blueprints({})
        assert idx == {"guids": set(), "entity_classes": set(), "names": set()}

    def test_none_input_does_not_raise(self):
        assert index_reward_pool_blueprints(None)["guids"] == set()


# ── The verdict ──────────────────────────────────────────────────────────

class TestObtainability:
    def test_unknown_before_mission_data_loads(self):
        """None, not False. The Fabricator and the mission cache load apart."""
        m = _mgr(with_pools=False)
        assert m.blueprint_obtainability_known() is False
        for bp in ALL_BPS:
            assert m.is_blueprint_obtainable(bp) is None

    def test_known_once_pools_are_indexed(self):
        assert _mgr().blueprint_obtainability_known() is True

    def test_demeco_lmg_is_unobtainable(self):
        """The case from the issue: full recipe, no source."""
        assert _mgr().is_blueprint_obtainable(BP_UNOBTAINABLE) is False

    def test_match_by_guid(self):
        assert _mgr().is_blueprint_obtainable(BP_BY_GUID) is True

    def test_match_by_entity_class(self):
        assert _mgr().is_blueprint_obtainable(BP_BY_ENTITY_CLASS) is True

    def test_match_by_name(self):
        assert _mgr().is_blueprint_obtainable(BP_BY_NAME) is True

    def test_null_fields_do_not_raise(self):
        m = _mgr()
        assert m.is_blueprint_obtainable(
            {"guid": None, "productEntityClass": None, "productName": None}) is False


# ── The filter ───────────────────────────────────────────────────────────

class TestObtainableFilter:
    def _run(self, m, **kw):
        return filter_blueprints(m.crafting_blueprints, FabFilterState(**kw),
                                 m.get_blueprint_product,
                                 m.get_blueprint_product_name,
                                 m.is_blueprint_obtainable)

    def test_off_shows_everything(self):
        assert len(self._run(_mgr())) == len(ALL_BPS)

    def test_on_hides_only_the_unobtainable(self):
        kept = self._run(_mgr(), obtainable_only=True)
        assert BP_UNOBTAINABLE not in kept
        assert len(kept) == len(ALL_BPS) - 1

    def test_unknown_rows_are_kept_not_hidden(self):
        """The dangerous failure: filtering on None would hide all 1,607 rows."""
        kept = self._run(_mgr(with_pools=False), obtainable_only=True)
        assert len(kept) == len(ALL_BPS)

    def test_no_callable_makes_the_flag_a_no_op(self):
        """Existing callers that pass four arguments must not change meaning."""
        m = _mgr()
        kept = filter_blueprints(m.crafting_blueprints,
                                 FabFilterState(obtainable_only=True),
                                 m.get_blueprint_product,
                                 m.get_blueprint_product_name)
        assert len(kept) == len(ALL_BPS)

    def test_default_state_does_not_filter(self):
        """FabFilterState() must behave exactly as it did before #21."""
        assert FabFilterState().obtainable_only is False

    def test_combines_with_search(self):
        kept = self._run(_mgr(), obtainable_only=True, search="demeco")
        assert kept == [BP_BY_GUID]
