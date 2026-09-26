"""Regression tests for issue #24 -- Fabricator search froze the window.

scmdb.net ships blueprint/item keys that are PRESENT with a null value.
``dict.get(key, default)`` only defaults a MISSING key, so the old
``prod.get("name", ...)`` returned ``None`` and the search branch's
``.lower()`` raised ``AttributeError``.  Because the crash hook ends in an
application-modal, always-on-top dialog and the search is debounced, every
keystroke stacked another dialog -- so it read as a freeze, not a traceback.

The fixtures below mirror the real 4.10.1 payload: ``productName: null``
(5 of 1,607 blueprints), ``name: null`` on the crafting item (4 of 1,605),
``subtype: null`` and ``type: null`` (15 and 4 blueprints).
"""

import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.manager import MissionDataManager  # noqa: E402
from data.models import FabFilterState, text_field  # noqa: E402
from services.filtering import filter_blueprints  # noqa: E402
from services.inventory import blueprint_key  # noqa: E402


# ── Fixtures modelled on the real live payload ───────────────────────────────

# BP_CRAFT_COOL_AEGS_S04_Javelin_SCItem: item exists, its name is null.
BP_NULL_ITEM_NAME = {
    "tag": "BP_CRAFT_COOL_AEGS_S04_Javelin_SCItem",
    "productEntityClass": "1f5be5de-b5ea-42c7-879f-289c4bf64f19",
    "productName": "Javelin Cooler",
    "type": "weapons",
    "subtype": "rifle",
}

# BP_CRAFT_COOL_S04_CNOU_Pioneer: null item name AND null productName.
BP_NULL_BOTH_NAMES = {
    "tag": "BP_CRAFT_COOL_S04_CNOU_Pioneer",
    "productEntityClass": "no-such-item",
    "productName": None,
    "type": "weapons",
    "subtype": "rifle",
}

# BP_CRAFT_Carryable_2H_CY_CollectorMaterial_001: null subtype/type/manufacturer.
BP_NULL_SUBTYPE = {
    "tag": "BP_CRAFT_Carryable_2H_CY_CollectorMaterial_001",
    "productEntityClass": "carryable-001",
    "productName": "Collector Material",
    "type": None,
    "subtype": None,
    "manufacturer": None,
}

BP_HEALTHY = {
    "tag": "BP_CRAFT_WEAPON_DEMECO",
    "productEntityClass": "demeco",
    "productName": "Demeco",
    "type": "weapons",
    "subtype": "lmg",
}

ITEMS = [
    {"entityClass": "1f5be5de-b5ea-42c7-879f-289c4bf64f19", "name": None},
    {"entityClass": "carryable-001", "name": "Collector Material"},
    {"entityClass": "demeco", "name": "Demeco LMG"},
]


def _mgr():
    m = MissionDataManager()
    m.crafting_blueprints = [
        BP_NULL_ITEM_NAME, BP_NULL_BOTH_NAMES, BP_NULL_SUBTYPE, BP_HEALTHY,
    ]
    m.crafting_items = ITEMS
    m.crafting_items_map = {i["entityClass"]: i for i in ITEMS}
    m.crafting_loaded = True
    return m


# ── text_field ───────────────────────────────────────────────────────────────

class TestTextField:
    def test_missing_key_uses_default(self):
        assert text_field({}, "name", "?") == "?"

    def test_present_null_uses_default(self):
        """The whole point: .get() would have returned None here."""
        assert {"name": None}.get("name", "?") is None
        assert text_field({"name": None}, "name", "?") == "?"

    def test_none_dict_uses_default(self):
        assert text_field(None, "name", "?") == "?"

    def test_real_value_passes_through(self):
        assert text_field({"name": "Demeco"}, "name") == "Demeco"

    def test_non_string_is_stringified(self):
        assert text_field({"size": 4}, "size") == "4"

    def test_empty_string_is_preserved_not_defaulted(self):
        assert text_field({"name": ""}, "name", "?") == ""


# ── The getter that produced the None ────────────────────────────────────────

class TestProductName:
    def test_null_item_name_falls_back_to_product_name(self):
        assert _mgr().get_blueprint_product_name(BP_NULL_ITEM_NAME) == "Javelin Cooler"

    def test_null_item_name_and_null_product_name_falls_back_to_tag(self):
        name = _mgr().get_blueprint_product_name(BP_NULL_BOTH_NAMES)
        assert name == "BP_CRAFT_COOL_S04_CNOU_Pioneer"

    def test_always_returns_a_str(self):
        m = _mgr()
        for bp in m.crafting_blueprints + [{}, {"productName": None, "tag": None}]:
            assert isinstance(m.get_blueprint_product_name(bp), str)

    def test_no_name_anywhere_returns_question_mark(self):
        assert _mgr().get_blueprint_product_name({}) == "?"

    def test_lower_never_raises(self):
        """The exact call in the search branch that used to crash."""
        m = _mgr()
        for bp in m.crafting_blueprints:
            m.get_blueprint_product_name(bp).lower()


# ── The search branch itself ─────────────────────────────────────────────────

class TestSearchWithNullNames:
    def test_single_character_search_does_not_raise(self):
        """#24's reproduction: any character, not a particular query."""
        m = _mgr()
        for ch in "abcdefghijklmnopqrstuvwxyz0123456789":
            filter_blueprints(m.crafting_blueprints, FabFilterState(search=ch),
                              m.get_blueprint_product, m.get_blueprint_product_name)

    def test_search_still_matches(self):
        m = _mgr()
        res = filter_blueprints(m.crafting_blueprints, FabFilterState(search="demeco"),
                                m.get_blueprint_product, m.get_blueprint_product_name)
        assert res == [BP_HEALTHY]

    def test_null_named_row_is_still_findable_by_tag(self):
        m = _mgr()
        res = filter_blueprints(m.crafting_blueprints, FabFilterState(search="pioneer"),
                                m.get_blueprint_product, m.get_blueprint_product_name)
        assert BP_NULL_BOTH_NAMES in res

    def test_pre_fix_getter_would_have_crashed(self):
        """Control: without the fix this fixture DOES raise, so the tests above
        are exercising the defect rather than passing for free."""
        m = _mgr()

        def old_get_name(bp):  # verbatim pre-fix implementation
            prod = m.get_blueprint_product(bp)
            if prod:
                return prod.get("name", bp.get("productName", bp.get("tag", "?")))
            return bp.get("productName", bp.get("tag", "?"))

        try:
            filter_blueprints(m.crafting_blueprints, FabFilterState(search="a"),
                              m.get_blueprint_product, old_get_name)
        except AttributeError:
            return
        raise AssertionError("fixture no longer reproduces #24 -- fix the fixture")


# ── Null subtype / type (the non-fatal sibling, logged by the grid) ──────────

class TestNullSubtypeAndType:
    def test_subtype_filter_skips_null_subtype_rows(self):
        m = _mgr()
        res = filter_blueprints(m.crafting_blueprints,
                                FabFilterState(subtypes={"lmg"}),
                                m.get_blueprint_product, m.get_blueprint_product_name)
        assert res == [BP_HEALTHY]

    def test_type_filter_skips_null_type_rows(self):
        m = _mgr()
        res = filter_blueprints(m.crafting_blueprints,
                                FabFilterState(types={"weapons"}),
                                m.get_blueprint_product, m.get_blueprint_product_name)
        assert BP_NULL_SUBTYPE not in res
        assert BP_HEALTHY in res

    def test_card_label_formatting_does_not_raise(self):
        """What ui/pages/fabricator.py::_fill_fab_card does with these fields."""
        for bp in _mgr().crafting_blueprints:
            text_field(bp, "subtype").replace("_", " ").title()
            assert isinstance(text_field(bp, "type", "?"), str)

    def test_getters_return_str(self):
        m = _mgr()
        assert m.get_blueprint_subtype(BP_NULL_SUBTYPE) == ""
        assert m.get_blueprint_type(BP_NULL_SUBTYPE) == ""
        assert m.get_blueprint_subtype(BP_HEALTHY) == "lmg"


class TestBlueprintKey:
    def test_null_product_name_does_not_produce_a_none_key(self):
        assert blueprint_key({"productName": None}) == ""

    def test_normal_key_unchanged(self):
        assert blueprint_key(BP_HEALTHY) == "BP_CRAFT_WEAPON_DEMECO|demeco"
