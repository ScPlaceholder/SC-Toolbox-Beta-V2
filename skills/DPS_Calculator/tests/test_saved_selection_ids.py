"""Saved loadouts from 2.3.1 must restore onto 3.0's scunpacked weapon slot ids.

2.3.1 keyed weapon selections "<parent label>:<itemPortName>"; 3.0 keys them
"sc:<HardpointName path>". Before services/saved_selections.py, every old
weapon selection was looked up under its old key, found nothing, and was
dropped without a word.

The fixture is REAL on both sides:
* SAVED_231 is the user's own ``Documents/SC Loadouts/Caterpillar.json``, saved by
  2.3.1 on 2026-03-31 (weapons section verbatim).
* CATERPILLAR_ROWS are the two ``sc_gun_slots`` rows of "Drake Caterpillar" in the
  scunpacked window index for build 4.10.1-LIVE.12660092 (only the fields the
  restore reads).

Each test was shown to fail with the mapping broken (``map_legacy_id`` returning
None, or matching the port without the label).
"""
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.saved_selections import (  # noqa: E402
    map_legacy_id, not_restored_message, resolve_selections,
)

TOP = "sc:hardpoint_turret_top/hardpoint_weapon_left/hardpoint_class_2"
BOTTOM = "sc:hardpoint_turret_bottom/hardpoint_weapon_left/hardpoint_class_2"

CATERPILLAR_ROWS = [
    {"id": BOTTOM, "sc_slots": [
        {"id": "hardpoint_turret_bottom/hardpoint_weapon_left/hardpoint_class_2"},
        {"id": "hardpoint_turret_bottom/hardpoint_weapon_right/hardpoint_class_2"}]},
    {"id": TOP, "sc_slots": [
        {"id": "hardpoint_turret_top/hardpoint_weapon_left/hardpoint_class_2"},
        {"id": "hardpoint_turret_top/hardpoint_weapon_right/hardpoint_class_2"}]},
]

SAVED_231 = {
    "weapons": {
        ":hardpoint_weapon_right": "M5A",
        ":hardpoint_weapon_left": "M5A",
        ":hardpoint_weapon_top": "CF-337 Panther",
        "Turret Top:hardpoint_weapon_left_3": "Condemnation",
        "Turret Top:hardpoint_weapon_right_4": "Condemnation",
        "Turret Bottom:hardpoint_weapon_left_5": "M8A",
        "Turret Bottom:hardpoint_weapon_right_6": "Attrition-6",
    },
    "missiles": {},
}


def test_old_payload_restores_onto_new_ids():
    plan, _ = resolve_selections(SAVED_231, {"weapons": CATERPILLAR_ROWS})
    assert plan.get("weapons", {}).get(TOP) == "Condemnation"


def test_label_disambiguates_identical_port_names():
    # both turrets have a hardpoint_weapon_left; only the label tells them apart
    assert map_legacy_id("Turret Top:hardpoint_weapon_left_3", CATERPILLAR_ROWS) == TOP
    assert map_legacy_id("Turret Bottom:hardpoint_weapon_left", CATERPILLAR_ROWS) == BOTTOM


def test_unmappable_ids_are_counted_not_misapplied():
    plan, unplaced = resolve_selections(SAVED_231, {"weapons": CATERPILLAR_ROWS})
    # The three top-level pilot hardpoints no longer exist on this ship; the bottom
    # turret's two guns are one row now and were saved with different guns.
    assert unplaced == 5
    assert BOTTOM not in plan.get("weapons", {})
    assert set(plan.get("weapons", {})) == {TOP}
    # ":hardpoint_weapon_left" names a port that also sits inside both turrets.
    assert map_legacy_id(":hardpoint_weapon_left", CATERPILLAR_ROWS) is None
    # ...and stays unmapped when only ONE turret carries that name: a top-level
    # pilot hardpoint that is gone is not the turret arm that shares its name.
    assert map_legacy_id(":hardpoint_weapon_left", CATERPILLAR_ROWS[1:]) is None


def test_current_ids_still_restore_and_cleared_slots_are_not_counted():
    plan, unplaced = resolve_selections(
        {"weapons": {TOP: "Attrition-4", BOTTOM: ""}}, {"weapons": CATERPILLAR_ROWS})
    assert plan == {"weapons": {TOP: "Attrition-4"}} and unplaced == 0


def test_message_is_silent_at_zero():
    assert not_restored_message(0) == ""
    assert not_restored_message(5) == "5 saved selections could not be restored"


def test_app_counts_slots_whose_component_is_not_offered():
    """The window path: a mapped id whose saved name is not in the slot's list
    (select_by_name -> False) is counted too, on top of the unmapped ones."""
    from dps_ui.app import DpsCalcApp

    class _Tbl:
        def __init__(self, names):
            self.names, self.picked = names, None

        def select_by_name(self, name):
            if name in self.names:
                self.picked = name
                return True
            return False

    top, bottom = _Tbl({"Attrition-4"}), _Tbl({"M8A"})

    class _Stub:
        _pending_sel = SAVED_231
        _slot_tables = {"weapons": [(CATERPILLAR_ROWS[0], bottom, None),
                                    (CATERPILLAR_ROWS[1], top, None)]}

        def _update_footer(self):
            pass

    n = DpsCalcApp._apply_pending_sel(_Stub())
    assert top.picked is None and bottom.picked is None
    assert n == 6     # 5 unmapped/conflicting + Condemnation not offered on the top row
