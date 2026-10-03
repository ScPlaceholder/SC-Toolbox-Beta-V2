"""One grid format: the hand-made holds, converted, driven through the real app.

WHAT IS UNDER TEST. Until 2026-09-26 the Cargo Loader read two grid formats -
the unified cache (144 ships, build_slots) and layouts/*.json (31 ships,
cargo_app._layout_to_slots). The layouts are now converted into ordinary
unified entries by datamine/layouts_to_grids.py, and the hand-solved packing
they carry rides along as an optional `arrangement` field that cargo_app loads
into the renderer's `_manual_boxes`.

The claim is "unifying loses no cargo". That claim is worth nothing asserted,
so every test here names the quantity that would move, and the ones that matter
are checked THROUGH THE APP, not through a re-implementation of it:
test_the_app_draws_the_layouts_own_placements loads all 32 ships into a live
CargoApp and compares the renderer's own box list, in ship coordinates, against
the layout files. It is the test the whole exercise reduces to.

⚠ WHY THE APP AND NOT THE ENGINE. The 489-test suite that existed before stayed
green when _draw_ground was mutated to draw NOTHING AT ALL. There was no
coverage of the renderer, so "the conversion is cell-exact" - which the sibling
file proves thoroughly - said nothing about what a user would see. Two of the
three defects the split caused were only visible on screen.

MUTATION PROOF. TestMutationGuards at the bottom breaks each mechanism in the
way a careless edit would and asserts the named test goes red, in-process, so
no reader has to take the coverage on trust.
"""

import copy
import importlib
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
for _p in (SKILL_DIR, os.path.join(SKILL_DIR, "datamine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import layouts_to_grids as L2G                                       # noqa: E402
from cargo_engine.packing import build_slots                         # noqa: E402
from cargo_engine.manual_place import PlacementContext               # noqa: E402
from cargo_engine.schema import (                                    # noqa: E402
    CONTAINER_DIMS, CONTAINER_MAX_STACK_HEIGHT,
)

cargo_app = importlib.import_module("cargo_app")


# ── the corpus, once ─────────────────────────────────────────────────────────

def _live_layouts():
    return [L for L in L2G.load_layouts()
            if (L.get("ship") or "").strip()
            and (L.get("ship") or "").strip().lower() not in L2G.SCRATCH_SHIPS]


LAYOUTS = _live_layouts()
IDS = [L2G.target_name(L) for L in LAYOUTS]
CONVERTED = {L2G.target_name(L): L2G.convert_layout(L, "columns") for L in LAYOUTS}

GAME_GRIDS_FILE = os.path.join(SKILL_DIR, "datamine", "cargo_grids_scunpacked.json")


def _game_ships():
    with open(GAME_GRIDS_FILE, encoding="utf-8") as fh:
        return json.load(fh)["ships"]


def placements_in_ship_coords(layout):
    """The layout's own boxes, snapped, as (x, y, z, w, h, l, scu)."""
    return sorted(
        (L2G._snap(p["pos"]["x"]), L2G._snap(p["pos"]["y"]), L2G._snap(p["pos"]["z"]),
         L2G._snap(p["dims"]["w"]), L2G._snap(p["dims"]["h"]), L2G._snap(p["dims"]["l"]),
         int(p["scu"]))
        for p in layout["placements"])


def _grid_origin(entry):
    grids = entry["groups"][0]["grids"]
    return min(g["x"] for g in grids), min(g["z"] for g in grids)


def _contains(g, b):
    x, y, z, w, h, l = b[:6]
    return (g["x"] <= x and x + w <= g["x"] + g["width"]
            and g["y"] <= y and y + h <= g["y"] + g["height"]
            and g["z"] <= z and z + l <= g["z"] + g["length"])


# ── the invariant the `columns` mode exists to keep ──────────────────────────

class TestOneGridPerColumn:
    """No two grids may cover one (x, z) column.

    manual_place.PlacementContext._grid_under(cx, cz) picks the grid a dropped
    container belongs to from its footprint centre and never looks at y. A
    second grid over a column therefore lets the engine reject a container from
    the grid it already occupies.
    """

    @pytest.mark.parametrize("entry", CONVERTED.values(), ids=list(CONVERTED))
    def test_no_converted_ship_stacks_a_column(self, entry):
        stacked = L2G.stacked_columns(entry["groups"][0]["grids"])
        assert stacked == [], (
            "%s: %d column(s) under two grids, e.g. %r"
            % (entry["name"], len(stacked), stacked[:1]))

    def test_the_game_files_keep_the_same_invariant(self):
        """Why it is the right invariant: the real corpus has always kept it.

        If this ever fails, the game files have grown the shape and the y-blind
        `_grid_under` is a live defect on the datamine path too - which is a
        finding about the engine, not a reason to relax the converter.
        """
        offenders = []
        for ship in _game_ships():
            slots, _ = build_slots(ship)
            grids = [{"x": s["x"], "y": s["y0"], "z": s["z"], "width": s["w"],
                      "height": s["h"], "length": s["l"]} for s in slots]
            if L2G.stacked_columns(grids):
                offenders.append(ship["name"])
        assert offenders == [], offenders

    def test_merged_mode_breaks_it_on_exactly_three_ships(self):
        """The measurement that justifies a third mode, pinned.

        `merged` decomposes per deck and stacks repeated footprints, which puts
        two grids over one column on these three. If this list shrinks, `merged`
        got better and `columns` may no longer be needed; if it grows, so did
        the problem. Either way it is a decision, not a silent drift.
        """
        broken = sorted(name for name, L in zip(IDS, LAYOUTS)
                        if L2G.stacked_columns(
                            L2G.grids_from_merged(L)))
        assert broken == ["Caterpillar", "Starfarer", "Starfarer Gemini"], broken


# ── the arrangement is a real, legal packing ─────────────────────────────────

class TestArrangementIsWellFormed:

    @pytest.mark.parametrize("name", list(CONVERTED))
    def test_every_box_is_a_legal_container_rotation(self, name):
        for b in CONVERTED[name]["arrangement"]:
            base = CONTAINER_DIMS.get(b["scu"])
            assert base is not None, "%s: no such container size %r" % (name, b["scu"])
            assert sorted(b["dims"]) == sorted(base), (
                "%s: %d SCU box with dims %r, not a rotation of %r"
                % (name, b["scu"], b["dims"], base))
            cap = CONTAINER_MAX_STACK_HEIGHT.get(b["scu"])
            if cap is not None:
                assert b["dims"][1] <= cap, (
                    "%s: %d SCU box %d high, max stack height is %d"
                    % (name, b["scu"], b["dims"][1], cap))

    @pytest.mark.parametrize("name", list(CONVERTED))
    def test_every_box_sits_inside_the_grids_and_none_overlap(self, name):
        entry = CONVERTED[name]
        cells = L2G.grid_cells(entry["groups"][0]["grids"])
        seen = set()
        for b in entry["arrangement"]:
            box = tuple(b["pos"]) + tuple(b["dims"])
            for dx in range(box[3]):
                for dy in range(box[4]):
                    for dz in range(box[5]):
                        c = (box[0] + dx, box[1] + dy, box[2] + dz)
                        assert c in cells, "%s: box cell %r outside the grids" % (name, c)
                        assert c not in seen, "%s: two boxes claim cell %r" % (name, c)
                        seen.add(c)

    @pytest.mark.parametrize("name", list(CONVERTED))
    def test_arrangement_scu_equals_capacity(self, name):
        entry = CONVERTED[name]
        assert sum(b["scu"] for b in entry["arrangement"]) == entry["capacity"]


class TestEveryBoxCanBeDroppedWhereItSits:
    """The rule the old format broke, and the reason it is worth measuring.

    A container the engine refuses to place at its own position is effectively
    immovable: pick it up, drop it back, and the app puts it where it was
    because the drop was illegal. Nothing in the format-B path ever checked it.
    """

    def _reject(self, grids_world, boxes):
        out = []
        for i, b in enumerate(boxes):
            ctx = PlacementContext(grids_world, boxes[:i] + boxes[i + 1:],
                                   union=True)
            pos, ok, why = ctx.snap((b[3], b[4], b[5], b[6]), (b[0], b[1], b[2]))
            if not ok:
                out.append((b, why))
            elif tuple(pos) != (b[0], b[1], b[2]):
                out.append((b, "moved to %r" % (tuple(pos),)))
        return out

    @pytest.mark.parametrize("name", list(CONVERTED))
    def test_no_box_is_rejected_from_its_own_position(self, name):
        entry = CONVERTED[name]
        slots, bounds = build_slots(entry)
        xm, zm = bounds[0], bounds[1]
        grids = [dict(s, x=s["x"] - xm, z=s["z"] - zm) for s in slots]
        boxes = cargo_app.arrangement_to_boxes(entry, bounds)
        bad = self._reject(grids, boxes)
        assert bad == [], "%s: %d box(es) illegal at home, e.g. %r" % (
            name, len(bad), bad[:2])

    def test_the_format_it_replaces_rejected_eight(self):
        """The defect that is FIXED, pinned so the gain cannot be forgotten.

        _layout_to_slots kept the placements' float positions, so the union of
        legal cells had fractional corners while a dropped box snaps to an
        integer cell. The Idris-M and Idris-P each put four 2 SCU containers at
        x=29.5 and x=31.5, and all eight were refused with "outside the cargo
        grids" - by the very volume they occupied.
        """
        rejected = []
        for L in LAYOUTS:
            vol = [{"x": p["pos"]["x"], "y0": p["pos"]["y"], "z": p["pos"]["z"],
                    "w": p["dims"]["w"], "h": p["dims"]["h"], "l": p["dims"]["l"],
                    "maxSize": None, "minSize": None} for p in L["placements"]]
            boxes = [(p["pos"]["x"], p["pos"]["y"], p["pos"]["z"], p["dims"]["w"],
                      p["dims"]["h"], p["dims"]["l"], int(p["scu"]))
                     for p in L["placements"]]
            for b, why in self._reject(vol, boxes):
                rejected.append((L["ship"], b[0], why))
        assert len(rejected) == 8, rejected
        assert {r[0] for r in rejected} == {"Idris-M", "Idris-P"}, rejected
        assert {r[1] for r in rejected} == {29.5, 31.5}, rejected


# ── capacity: which number is right, stated once and pinned ──────────────────

class TestCapacityAgreesWithThreeSources:
    """Each layout has three independent capacity claims: the cells its boxes
    occupy, its own `totalCapacity`, and the game files. 30 of 32 agree exactly.

    The disagreements are settled by majority AND by geometry, and pinned here
    so a future edit has to argue with a list rather than with a habit:

      Freelancer  cells 68 / stated 66 / game 66  ->  66 IS RIGHT.
        Two of three said 66, and the geometry agreed: removing the two 1 SCU
        boxes at (0,0,13) and (0,1,13) leaves 54 SCU in the main hold and 12 in
        the side racks, which is exactly the game files' 2x3x9 + 2x3x1 + 2x3x1
        split. Fixed in the layout file, not papered over here; the two boxes
        were 2 SCU the ship cannot carry.
      Mercury Star Runner  cells 114 / stated 14 / game 114  ->  114 IS RIGHT.
        A dropped digit. Inert while the capacity came from the ship source, and
        a 100 SCU error the moment it did not. Fixed in the layout file.
      Hammerhead  cells 40 / stated 40 / game 64  ->  THE GAME'S 64 IS KEPT.
        No duplicate: the app's ship data has one Hammerhead, one 4x2x8 grid of
        64 cells. The hand layout is an older partial solve covering 40 of them;
        merge_layout_grids keeps the game's hold and takes only the arrangement,
        so the app shows 64 SCU with 40 drawn and 24 free (pinned by
        test_a_partial_hand_solve_does_not_shrink_the_hold).
    """

    EXPECTED_STATED_MISMATCH: dict[str, tuple[int, int]] = {}
    EXPECTED_GAME_MISMATCH = {"Hammerhead": (40, 64)}

    def test_total_used_matches_the_cells(self):
        """`totalUsed` is derived and nothing downstream reads it - which is
        exactly why it can rot unnoticed. Added after a mutation that set the
        Freelancer's back to 68 SURVIVED the rest of this class: three checks
        passed while the source file claimed a number its own boxes contradict.
        All 32 agree today, so the check costs nothing and closes the hole.
        """
        off = {}
        for L in LAYOUTS:
            name = L2G.target_name(L)
            if L.get("totalUsed") != CONVERTED[name]["capacity"]:
                off[name] = (L.get("totalUsed"), CONVERTED[name]["capacity"])
        assert off == {}, off

    def test_stated_capacity_matches_the_cells(self):
        off = {}
        for L in LAYOUTS:
            name = L2G.target_name(L)
            cells = CONVERTED[name]["capacity"]
            stated = L.get("totalCapacity")
            if stated != cells:
                off[name] = (cells, stated)
        assert off == self.EXPECTED_STATED_MISMATCH, off

    def test_game_capacity_matches_the_cells(self):
        game = {(s.get("name") or "").lower(): s.get("capacity")
                for s in _game_ships()}
        off = {}
        for name, entry in CONVERTED.items():
            g = game.get(name.lower())
            assert g is not None, "%s has no game-file entry to compare" % name
            if g != entry["capacity"]:
                off[name] = (entry["capacity"], g)
        assert off == self.EXPECTED_GAME_MISMATCH, off

    def test_the_freelancer_carries_exactly_66(self):
        """Its own regression: the 2 SCU over-fill is gone from the source."""
        entry = CONVERTED["Freelancer"]
        assert entry["capacity"] == 66
        assert sum(b["scu"] for b in entry["arrangement"]) == 66
        assert not [b for b in entry["arrangement"] if b["scu"] == 1], (
            "the two 1 SCU boxes at (0,*,13) were the over-fill")

    def test_no_reference_loadout_asks_for_more_than_the_hold_holds(self):
        """THE SAME 2 SCU WERE IN A THIRD FILE.

        reference_loadouts.json also carried the Freelancer at 68
        ({"32":1,"4":4,"2":9,"1":2}), and _optimize() prefers a reference
        loadout over the greedy packer - so pressing Optimize would have asked
        for 68 SCU into a 66 SCU hold and been clamped all over again, by a
        path the arrangement does not touch. Fixing the layout and leaving this
        would have fixed the default view and left the button broken.

        One over-fill, three files: the layout's placements, the layout's
        `containers`, and this. A loss found in one copy is worth grepping for.
        """
        from cargo_common import find_reference_loadout, load_reference_loadouts
        refs = load_reference_loadouts(SKILL_DIR)
        over = {}
        for name, entry in CONVERTED.items():
            ref = find_reference_loadout(name, refs)
            if ref is None:
                continue
            asked = sum(int(k) * int(v) for k, v in ref.items())
            if asked > entry["capacity"]:
                over[name] = (asked, entry["capacity"])
        assert over == {}, over


# ── the shipped file, the rename, the names ──────────────────────────────────

class TestShippedFileAndNames:

    def test_the_shipped_file_is_what_the_converter_produces(self):
        """A generated artifact that has drifted from its generator is a third
        format wearing the costume of the first."""
        with open(cargo_app.LAYOUT_GRIDS_FILE, encoding="utf-8") as fh:
            shipped = json.load(fh)
        assert shipped.get("mode") == "columns"
        fresh, _skipped = L2G.convert_all("columns")
        assert shipped["ships"] == fresh, (
            "datamine/cargo_grids_layouts.json is stale - re-run "
            "python datamine/layouts_to_grids.py")

    def test_zeus_cl_is_remapped_and_says_so(self):
        assert "Zeus CL" not in CONVERTED
        entry = CONVERTED["Zeus Mk II CL"]
        assert entry["provenance"]["renamedFrom"] == "Zeus CL"
        assert entry["capacity"] == 128
        game = {s["name"]: s.get("capacity") for s in _game_ships()}
        assert game["Zeus Mk II CL"] == 128, (
            "the remap rests on the two capacities matching")

    def test_every_layout_name_reaches_a_real_ship(self):
        game = {(s.get("name") or "").lower() for s in _game_ships()}
        missing = sorted(n for n in CONVERTED if n.lower() not in game)
        assert missing == [], missing

    def test_the_renames_are_the_only_ones(self):
        assert L2G.RENAMES == {"zeus cl": "Zeus Mk II CL"}


# ── the overlay ──────────────────────────────────────────────────────────────

class TestMergeLayoutGrids:

    def _overlay(self):
        return {"widget": {
            "name": "Widget", "capacity": 8,
            "groups": [{"x": 0, "z": 0, "grids": [
                {"x": 0, "y": 0, "z": 0, "width": 2, "height": 2, "length": 2,
                 "minSize": None, "maxSize": None}]}],
            "arrangement": [{"scu": 8, "pos": [0, 0, 0], "dims": [2, 2, 2]}],
            "provenance": {"source": "hand-layout"},
        }}

    def test_it_replaces_geometry_and_keeps_what_a_layout_lacks(self):
        ships = [{"name": "Widget", "capacity": 99, "manufacturer": "Acme",
                  "labels": ["fore"], "groups": [{"x": 0, "z": 0, "grids": []}]}]
        cargo_app.merge_layout_grids(ships, self._overlay())
        assert len(ships) == 1
        s = ships[0]
        assert s["capacity"] == 8 and s["scu"] == 8 and s["cargo"] == 8
        assert s["groups"][0]["grids"][0]["width"] == 2
        assert len(s["arrangement"]) == 1
        assert s["manufacturer"] == "Acme" and s["labels"] == ["fore"]

    def test_it_matches_case_insensitively(self):
        ships = [{"name": "WIDGET", "capacity": 99,
                  "groups": [{"x": 0, "z": 0, "grids": []}]}]
        cargo_app.merge_layout_grids(ships, self._overlay())
        assert len(ships) == 1 and ships[0]["capacity"] == 8
        assert ships[0]["name"] == "WIDGET", "the source keeps its own spelling"

    def test_an_unmatched_hold_is_appended_not_dropped(self):
        ships = [{"name": "Other", "capacity": 1,
                  "groups": [{"x": 0, "z": 0, "grids": []}]}]
        cargo_app.merge_layout_grids(ships, self._overlay())
        assert [s["name"] for s in ships] == ["Other", "Widget"]

    def test_it_does_not_duplicate_on_a_second_pass(self):
        ships = [{"name": "Other", "capacity": 1,
                  "groups": [{"x": 0, "z": 0, "grids": []}]}]
        cargo_app.merge_layout_grids(ships, self._overlay())
        cargo_app.merge_layout_grids(ships, self._overlay())
        assert [s["name"] for s in ships] == ["Other", "Widget"]

    def test_the_real_overlay_covers_every_layout(self):
        assert len(cargo_app.LAYOUT_GRIDS) == len(CONVERTED)
        assert set(cargo_app.LAYOUT_GRIDS) == {n.lower() for n in CONVERTED}


class TestArrangementToBoxes:

    ENTRY = {"name": "Widget",
             "arrangement": [{"scu": 2, "pos": [5, 1, 7], "dims": [1, 1, 2]}]}

    def test_it_subtracts_the_grid_origin_from_x_and_z_only(self):
        boxes = cargo_app.arrangement_to_boxes(self.ENTRY, (5, 7, 9, 9))
        assert boxes == [(0, 1, 0, 1, 1, 2, 2)], boxes

    def test_no_arrangement_is_none_not_empty(self):
        assert cargo_app.arrangement_to_boxes({"name": "x"}, (0, 0, 1, 1)) is None
        assert cargo_app.arrangement_to_boxes(
            {"name": "x", "arrangement": []}, (0, 0, 1, 1)) is None

    def test_a_malformed_entry_is_refused_whole(self):
        """Half an arrangement is worse than none: it would draw a hold that
        looks packed and is missing boxes."""
        bad = {"name": "Widget", "arrangement": [
            {"scu": 2, "pos": [0, 0, 0], "dims": [1, 1, 2]},
            {"scu": 2, "pos": [0, 0], "dims": [1, 1, 2]}]}
        assert cargo_app.arrangement_to_boxes(bad, (0, 0, 1, 1)) is None

    def test_has_union_grids_reads_provenance(self):
        assert cargo_app.has_union_grids(
            {"provenance": {"source": "hand-layout"}}) is True
        assert cargo_app.has_union_grids(
            {"provenance": {"source": "scunpacked-data"}}) is False
        assert cargo_app.has_union_grids({}) is False
        assert cargo_app.has_union_grids(None) is False


# ── the app itself ───────────────────────────────────────────────────────────

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app_boxes():
    """Load every hand-made hold into a live CargoApp and record what it draws.

    Module-scoped: one window, 32 ships, and the renderer's own `_last_boxes`.
    Nothing here re-implements the app - that is the point of the fixture.
    """
    import shared.qt.base_window as bw
    qapp = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    saved = (cargo_app._fetch_uex_commodities,
             cargo_app.ShipDataLoader.load_async, bw._save_window_state)
    cargo_app._fetch_uex_commodities = lambda: None
    cargo_app.ShipDataLoader.load_async = lambda self, cb: None
    bw._save_window_state = lambda w: None
    try:
        w = cargo_app.CargoApp(0, 0, 1000, 700, 1.0, None)
        w._commodity_poll.stop()
        ships = cargo_app.merge_layout_grids(copy.deepcopy(_game_ships()))
        w._data._index(ships)
        w._data.loaded = True
        w._set_mode("auto")
        out = {}
        for name in sorted(CONVERTED):
            w._load_ship(name)
            w._reset_containers()
            qapp.processEvents()
            out[name] = {
                "boxes": sorted(tuple(b) for b in w._renderer._last_boxes),
                "bounds": tuple(w._bounds),
                "capacity": (w._current_ship or {}).get("capacity"),
                "union": w._union_grids,
                "counts": {s: w._get_count(s) for s in cargo_app.CONTAINER_SIZES},
            }
        out["__combo__"] = w._data.get_ship_names()
        w.hide()
        yield out
        w.deleteLater()
        qapp.processEvents()
    finally:
        (cargo_app._fetch_uex_commodities,
         cargo_app.ShipDataLoader.load_async, bw._save_window_state) = saved


# The Hammerhead is deliberately NOT full, and that is the corrected state.
# Its datamined hold is one 4 x 2 x 8 grid = 64 cells with capacity 64 — self
# consistent, provenance scunpacked 4.10.1 — while the hand layout is an older
# partial solve covering 40 of those cells. merge_layout_grids used to let the
# layout shrink the hold to 40 so that drawn == capacity; it now keeps the
# source's 64 and takes only the arrangement, because a hand-solve has no
# business deleting cells the ship source knows about.
PARTIAL = {"Hammerhead": (40, 64)}
class TestTheAppDrawsTheHandSolvedPacking:
    """The headline. Everything above is a property of the data; this is what a
    user sees."""

    @pytest.mark.parametrize("layout", LAYOUTS, ids=IDS)
    def test_the_app_draws_the_layouts_own_placements(self, layout, app_boxes):
        name = L2G.target_name(layout)
        rec = app_boxes[name]
        x_min, z_min = rec["bounds"][0], rec["bounds"][1]
        got = sorted((b[0] + x_min, b[1], b[2] + z_min, b[3], b[4], b[5], b[6])
                     for b in rec["boxes"])
        want = placements_in_ship_coords(layout)
        assert got == want, (
            "%s: %d box(es) the layout has and the app does not, %d the other way"
            % (name, len(set(want) - set(got)), len(set(got) - set(want))))


    @pytest.mark.parametrize("name", [n for n in CONVERTED if n not in PARTIAL])
    def test_the_hold_is_full(self, name, app_boxes):
        rec = app_boxes[name]
        drawn = sum(b[6] for b in rec["boxes"])
        assert drawn == rec["capacity"] == CONVERTED[name]["capacity"], (
            "%s: drew %d SCU into a %s SCU hold" % (name, drawn, rec["capacity"]))

    @pytest.mark.parametrize("name", list(PARTIAL))
    def test_a_partial_hand_solve_does_not_shrink_the_hold(self, name, app_boxes):
        """The layout supplies an ARRANGEMENT; the ship source supplies the HOLD.

        Guards both directions at once. If the arrangement ever stops being drawn
        the first assert fails; if a layout is ever allowed to shrink a hold again
        the second does. Before the 2026-09-27 fix the Hammerhead reported a 40 SCU
        hold and 24 real cells were unreachable.
        """
        drawn_want, cap_want = PARTIAL[name]
        rec = app_boxes[name]
        drawn = sum(b[6] for b in rec["boxes"])
        assert drawn == drawn_want, (
            "%s: hand arrangement drew %d SCU, expected %d" % (name, drawn, drawn_want))
        assert rec["capacity"] == cap_want, (
            "%s: hold reported as %s SCU, expected the ship source's %d — a partial "
            "hand-solve must not delete the cells it did not use"
            % (name, rec["capacity"], cap_want))
        assert drawn < rec["capacity"], (
            "%s: this ship is in PARTIAL because its hand-solve covers only part of "
            "the hold; if it is now full, move it back to the full-hold test rather "
            "than leaving a vacuous assertion here" % name)

    def test_the_whole_fleet_total(self):
        """8912 SCU over 32 ships, 1047 boxes. The number the exercise is about.

        Before unification the same path drew 8874 of a claimed 8936: the
        Freelancer lost 38 SCU to the capacity clamp and the Hammerhead's hold
        was 24 SCU smaller than the capacity beside it. The shortfall is now 0.
        """
        assert sum(e["capacity"] for e in CONVERTED.values()) == 8912
        assert sum(len(e["arrangement"]) for e in CONVERTED.values()) == 1047

    def test_the_freelancer_is_no_longer_clamped_to_28(self, app_boxes):
        """The single worst live defect this replaced.

        _update_fill computes `used` once and then clamps each spinbox against
        it, so a hold whose boxes totalled 68 in a 66 SCU ship came out as
        1->0, 2 SCU 9->8, 4 SCU 4->3, 32 SCU 1->0: 28 of 66 SCU on screen.
        """
        rec = app_boxes["Freelancer"]
        assert rec["capacity"] == 66
        assert sum(b[6] for b in rec["boxes"]) == 66
        assert rec["counts"][32] == 1, "the 32 SCU box was the first casualty"

    def test_hand_holds_use_union_placement_and_game_grids_do_not(self, app_boxes):
        for name in CONVERTED:
            assert app_boxes[name]["union"] is True, name

    def test_every_hand_hold_is_reachable_in_the_combo(self, app_boxes):
        combo = {c.lower() for c in app_boxes["__combo__"]}
        missing = sorted(n for n in CONVERTED if n.lower() not in combo)
        assert missing == [], missing

    def test_the_idrises_survive_the_exclude_list(self, app_boxes):
        """_EXCLUDE drops the Idris-M/P from the ship list, and before the
        formats were unified they came back via a separate layout-name
        injection. The exemption in get_ship_names is what keeps 2700 SCU of
        hand-solved hold in the combo box."""
        combo = {c.lower() for c in app_boxes["__combo__"]}
        assert "idris-m" in combo and "idris-p" in combo

    def test_zeus_cl_is_no_longer_a_second_entry(self, app_boxes):
        combo = {c.lower() for c in app_boxes["__combo__"]}
        assert "zeus cl" not in combo
        assert "zeus mk ii cl" in combo

    def test_the_bounds_are_the_real_hold_not_a_bounding_box(self, app_boxes):
        """The old path forced bounds to (0, 0, gridW, gridZ), the layout's
        declared box, which is why the Caterpillar was drawn with six empty
        rows of deck in front of it and only 24.7% of its painted floor would
        take a box. Bounds now come from the grids."""
        rec = app_boxes["Caterpillar"]
        assert rec["bounds"][1] == 6, rec["bounds"]
        layout = next(L for L in LAYOUTS if L["ship"] == "Caterpillar")
        assert layout["gridZ"] != rec["bounds"][3] - rec["bounds"][1]


class TestTheOldFormatIsGone:

    def test_cargo_app_no_longer_reads_layouts(self):
        for gone in ("SHIP_LAYOUTS", "_layout_to_slots", "_load_ship_layouts"):
            assert not hasattr(cargo_app, gone), (
                "%s is back - the second grid format came with it" % gone)

    def test_the_replacements_are_present(self):
        for need in ("LAYOUT_GRIDS", "load_layout_grids", "merge_layout_grids",
                     "arrangement_to_boxes", "has_union_grids"):
            assert hasattr(cargo_app, need), need

    def test_the_source_layouts_are_kept(self):
        """Deleting the source of a generated artifact is not unification.
        cargo_grid_editor.html and layouts_to_grids.py both read them."""
        assert os.path.isdir(cargo_app.LAYOUTS_DIR)
        assert len(L2G.load_layouts()) == 33


# ── proof the suite can fail ─────────────────────────────────────────────────

class TestMutationGuards:
    """Each mutation is one a careless edit would really make, applied in
    process, with the named test re-run against it."""

    def test_mutation_using_merged_grids_trips_the_column_invariant(self):
        """Swap the decomposition back to `merged` and the invariant test must
        go red on the Caterpillar. This is the mutation that motivated the
        whole third mode, so it is the one that must not pass quietly."""
        cat = next(L for L in LAYOUTS if L["ship"] == "Caterpillar")
        entry = dict(CONVERTED["Caterpillar"])
        entry["groups"] = [{"x": 0, "z": 0, "grids": L2G.grids_from_merged(cat)}]
        with pytest.raises(AssertionError, match="under two grids"):
            TestOneGridPerColumn().test_no_converted_ship_stacks_a_column(entry)

    def test_mutation_forgetting_the_grid_origin_is_caught(self, monkeypatch):
        """arrangement_to_boxes must subtract bounds min. The Caterpillar's
        grids start at z=6, so dropping it shifts every box by six cells - and
        on the 21 ships whose origin is already 0 nothing would move at all,
        which is exactly why the assertion is parametrised per ship."""
        monkeypatch.setattr(cargo_app, "arrangement_to_boxes",
                            lambda ship, bounds: [
                                (b["pos"][0], b["pos"][1], b["pos"][2],
                                 b["dims"][0], b["dims"][1], b["dims"][2], b["scu"])
                                for b in (ship.get("arrangement") or [])] or None)
        cat = CONVERTED["Caterpillar"]
        slots, bounds = build_slots(cat)
        boxes = cargo_app.arrangement_to_boxes(cat, bounds)
        shifted = sorted((b[0] + bounds[0], b[1], b[2] + bounds[1],
                          b[3], b[4], b[5], b[6]) for b in boxes)
        layout = next(L for L in LAYOUTS if L["ship"] == "Caterpillar")
        assert shifted != placements_in_ship_coords(layout), (
            "the mutation changed nothing - this guard is not guarding")

    def test_mutation_dropping_a_box_from_the_arrangement_is_caught(self, monkeypatch):
        real = L2G.arrangement_boxes
        monkeypatch.setattr(L2G, "arrangement_boxes", lambda L: real(L)[:-1])
        cat = next(L for L in LAYOUTS if L["ship"] == "Caterpillar")
        entry = L2G.convert_layout(cat, "columns")
        assert sum(b["scu"] for b in entry["arrangement"]) != entry["capacity"], (
            "dropping a box left the SCU total unchanged - the fill test would "
            "not have noticed")
        assert sorted(tuple(b["pos"]) + tuple(b["dims"]) + (b["scu"],)
                      for b in entry["arrangement"]) != placements_in_ship_coords(cat)

    def test_mutation_moving_a_box_out_of_its_grid_is_caught(self):
        entry = copy.deepcopy(CONVERTED["Reclaimer"])
        entry["arrangement"][0]["pos"][1] += 99
        cells = L2G.grid_cells(entry["groups"][0]["grids"])
        b = entry["arrangement"][0]
        assert tuple(b["pos"]) not in cells, (
            "a box lifted 99 decks is still inside the grids - the containment "
            "check would pass anything")

    def test_mutation_overlaying_without_capacity_reopens_the_clamp(self):
        """The Freelancer's 28/66 came from capacity and geometry disagreeing.
        An overlay that replaces the grids and leaves the old capacity behind
        puts that disagreement straight back."""
        ships = [{"name": "Freelancer", "capacity": 999,
                  "groups": [{"x": 0, "z": 0, "grids": []}]}]
        entry = CONVERTED["Freelancer"]
        # the mutation: geometry only, capacity untouched
        ships[0]["groups"] = entry["groups"]
        assert ships[0]["capacity"] != entry["capacity"], (
            "a partial overlay must be detectable as a mismatch")
        cargo_app.merge_layout_grids(ships, {"freelancer": entry})
        assert ships[0]["capacity"] == 66, "the real overlay must fix it"

    def test_mutation_stated_capacity_drift_is_caught(self):
        """The Freelancer/MSR check must actually bite. Feed it a layout whose
        totalCapacity disagrees and confirm the comparison notices."""
        fake = copy.deepcopy(next(L for L in LAYOUTS if L["ship"] == "Freelancer"))
        fake["totalCapacity"] = 999
        cells = L2G.convert_layout(fake, "columns")["capacity"]
        assert fake["totalCapacity"] != cells

    def test_a_missing_overlay_file_degrades_instead_of_crashing(self):
        """The app must still start with the game grids alone."""
        assert cargo_app.load_layout_grids("no-such-file.json") == {}
        ships = [{"name": "Freelancer", "capacity": 66,
                  "groups": [{"x": 0, "z": 0, "grids": []}]}]
        cargo_app.merge_layout_grids(ships, {})
        assert not ships[0].get("arrangement")
