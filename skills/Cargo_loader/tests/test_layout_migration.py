"""Tests for the layouts/ -> unified groups/grids migration.

The claim under test is "converting a hand-made layout into the unified cache
format loses nothing". These tests are written so that claim is MEASURED, not
asserted: each one names the quantity that would move if the converter were
wrong, and every one of them has been shown to go red under a deliberate
mutation of the converter (see test_mutation_guard_* at the bottom, which
mutate in-process rather than asking the reader to take it on trust).
"""

import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_DIR = os.path.dirname(HERE)
for p in (SKILL_DIR, os.path.join(SKILL_DIR, "datamine")):
    if p not in sys.path:
        sys.path.insert(0, p)

import layouts_to_grids as L2G                                    # noqa: E402
from cargo_engine.packing import build_slots, place_containers_3d  # noqa: E402
from cargo_engine.optimizer import (                               # noqa: E402
    assign_slots_from_counts, greedy_optimize_3d,
)


def _layouts():
    return [L for L in L2G.load_layouts()
            if (L.get("ship") or "").lower() not in L2G.SCRATCH_SHIPS
            and L.get("ship")]


ALL_LAYOUTS = _layouts()
IDS = [L["ship"] for L in ALL_LAYOUTS]


def _synthetic(ship, placements, gw=8, gz=8, gh=8):
    return {"schemaVersion": 1, "ship": ship, "gridW": gw, "gridZ": gz,
            "gridH": gh,
            "totalUsed": sum(p["dims"]["w"] * p["dims"]["h"] * p["dims"]["l"]
                             for p in placements),
            "totalCapacity": sum(p["scu"] for p in placements),
            "containers": {}, "placements": placements}


def _p(scu, w, h, l, x, y, z):
    return {"scu": scu, "dims": {"w": w, "h": h, "l": l},
            "pos": {"x": x, "y": y, "z": z}, "rotation": 0}


# Shapes the 32 real layouts DO NOT produce, declared explicitly rather than
# inferred from the data I happen to have. Measured 2026-09-26: no real layout
# contains two same-footprint boxes separated by exactly one empty deck, so
# _merge_up's "do these touch in Y" test has no producer in real data and a
# mutation of it cannot be caught by a real-data suite. The first version of
# this file ran exactly that mutation, stayed green, and I read it as the
# mutation being harmless.
GAPPED_STACK = _synthetic("SynthGappedStack", [
    _p(2, 1, 1, 2, 0, 0, 0),   # deck 0
    _p(2, 1, 1, 2, 0, 2, 0),   # deck 2, SAME footprint, one empty deck between
])
FLOATING_ONLY = _synthetic("SynthFloatingOnly", [
    _p(1, 1, 1, 1, 3, 4, 3),   # nothing at all on the ground
])
SPARSE_L = _synthetic("SynthSparseL", [
    _p(1, 1, 1, 1, 0, 0, 0), _p(1, 1, 1, 1, 1, 0, 0), _p(1, 1, 1, 1, 0, 0, 1),
])
SYNTHETIC = [GAPPED_STACK, FLOATING_ONLY, SPARSE_L]
SYNTH_IDS = [s["ship"] for s in SYNTHETIC]


def _fill(entry):
    """Run a converted ship entry through the real auto path.

    Returns (capacity, scu the optimiser asks for, scu that geometrically fits).
    """
    slots, _ = build_slots(entry)
    cap = sum(s["capacity"] for s in slots)
    counts = greedy_optimize_3d(slots)
    asgn = assign_slots_from_counts(slots, counts)
    fitted = sum(b[6] for i, s in enumerate(slots)
                 for b in place_containers_3d(s, asgn[i]))
    return cap, sum(k * v for k, v in counts.items()), fitted


# ── the conversion itself ────────────────────────────────────────────────────

class TestCellExactness:
    """A converted ship must cover the same cells - no gaps, no invention."""

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    @pytest.mark.parametrize("mode", sorted(L2G.MODES))
    def test_cells_identical(self, layout, mode):
        entry = L2G.convert_layout(layout, mode)
        want = L2G.layout_cells(layout)
        got = L2G.grid_cells(entry["groups"][0]["grids"])
        assert got == want, (
            "%s/%s: %d cells missing, %d invented"
            % (layout["ship"], mode, len(want - got), len(got - want)))

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    @pytest.mark.parametrize("mode", sorted(L2G.MODES))
    def test_no_grid_overlap(self, layout, mode):
        grids = L2G.convert_layout(layout, mode)["groups"][0]["grids"]
        vol = sum(g["width"] * g["height"] * g["length"] for g in grids)
        assert vol == len(L2G.grid_cells(grids))

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    @pytest.mark.parametrize("mode", sorted(L2G.MODES))
    def test_capacity_equals_layout_used(self, layout, mode):
        # totalUsed, not totalCapacity: three files disagree with themselves
        # (Custom 0, Freelancer 66 vs 68, Mercury Star Runner 14 vs 114) and
        # the placements are the primary record.
        entry = L2G.convert_layout(layout, mode)
        assert entry["capacity"] == layout["totalUsed"]


class TestVolumesModeIsLossless:
    """`volumes` is the mode that may be switched on without losing fill."""

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    def test_reproduces_the_container_histogram(self, layout):
        entry = L2G.convert_layout(layout, "volumes")
        slots, _ = build_slots(entry)
        counts = {k: v for k, v in greedy_optimize_3d(slots).items() if v}
        want = {int(k): v for k, v in layout.get("containers", {}).items() if v}
        assert counts == want, layout["ship"]

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    def test_every_scu_still_fits(self, layout):
        entry = L2G.convert_layout(layout, "volumes")
        cap, asked, fitted = _fill(entry)
        assert cap == layout["totalUsed"]
        assert asked == cap, "%s: optimiser asked for %d of %d" % (
            layout["ship"], asked, cap)
        assert fitted == cap, "%s: only %d of %d SCU fitted" % (
            layout["ship"], fitted, cap)

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    def test_boxes_land_where_the_layout_drew_them(self, layout):
        """Same box set, same world positions, via the unified code path.

        Compared against the layout's own coordinates FLOORED, because the
        unified format has no sub-cell positions; the eight placements that
        needed flooring are pinned down by test_fractional_inventory below, so
        this tolerance cannot widen unnoticed.
        """
        entry = L2G.convert_layout(layout, "volumes")
        slots, _ = build_slots(entry)
        asgn = assign_slots_from_counts(slots, greedy_optimize_3d(slots))
        got = sorted(
            (s["x"] + b[0], s["y0"] + b[1], s["z"] + b[2], b[6])
            for i, s in enumerate(slots)
            for b in place_containers_3d(s, asgn[i]))
        want = sorted(
            (L2G._snap(p["pos"]["x"]), L2G._snap(p["pos"]["y"]),
             L2G._snap(p["pos"]["z"]), p["scu"])
            for p in layout["placements"])
        assert got == want, layout["ship"]

    def test_fractional_inventory(self):
        """Exactly which placements are NOT bit-exact, and no others.

        Idris-M and Idris-P each put four 2 SCU containers on a half cell
        (x=29.5, x=31.5). Those eight are the entire tolerance in the test
        above. A new fractional placement anywhere - or a change to these -
        fails here rather than silently widening what "lossless" covers.
        """
        found = {L["ship"]: sorted(L2G.fractional_placements(L))
                 for L in ALL_LAYOUTS if L2G.fractional_placements(L)}
        assert sorted(found) == ["Idris-M", "Idris-P"]
        for ship, frac in found.items():
            assert [f for _i, f, _v in frac] == ["pos.x"] * 4, ship
            assert sorted(v for _i, _f, v in frac) == [29.5, 29.5, 31.5, 31.5]

    @pytest.mark.parametrize("layout", ALL_LAYOUTS, ids=IDS)
    def test_provenance_records_every_snap(self, layout):
        entry = L2G.convert_layout(layout, "volumes")
        assert (len(entry["provenance"]["snappedFromFractional"])
                == len(L2G.fractional_placements(layout)))


class TestMergedModeIsNotLossless:
    """Recorded so the difference between the modes cannot go quiet.

    `merged` carries the true grid shape - which is what makes the floor and
    the painted deck correct - but hands the fill back to the greedy optimiser,
    which cannot solve the packing a human solved by hand. This test asserts
    the SHORTFALL EXISTS, so if someone later improves the optimiser enough to
    close it, this test fails and the migration decision gets revisited
    instead of silently becoming free.
    """

    def test_some_ship_loses_fill_under_merged(self):
        lossy = []
        for layout in ALL_LAYOUTS:
            cap, _asked, fitted = _fill(L2G.convert_layout(layout, "merged"))
            if fitted < cap:
                lossy.append((layout["ship"], cap, fitted))
        assert lossy, (
            "merged mode now fills every ship - the volumes/merged trade-off "
            "this migration was blocked on may be gone; re-measure and decide")


class TestShapesRealDataDoesNotProduce:
    """Cover the branches no real layout reaches, so a mutation of them bites.

    Without these every mutation of _merge_up's adjacency test passes: I ran
    one (== becomes <= +1, which stacks boxes across an empty deck and invents
    a whole layer of cells) against the 32 real layouts and the suite stayed
    green in 0.40s. Not because the check was sound - because nothing in the
    corpus had the shape.
    """

    @pytest.mark.parametrize("layout", SYNTHETIC, ids=SYNTH_IDS)
    @pytest.mark.parametrize("mode", sorted(L2G.MODES))
    def test_cells_identical(self, layout, mode):
        # `columns` REFUSES a gapped stack instead of converting it (a floating
        # deck is the one shape that cannot be covered by one grid per column).
        # Asserting the refusal here rather than skipping keeps the fixture
        # load-bearing for all three modes: a mode that silently started
        # accepting it would fail this test, not quietly pass a narrower one.
        if mode == "columns" and L2G.gapped_columns(layout):
            with pytest.raises(L2G.ConversionError, match="gap"):
                L2G.convert_layout(layout, mode)
            return
        entry = L2G.convert_layout(layout, mode)
        assert (L2G.grid_cells(entry["groups"][0]["grids"])
                == L2G.layout_cells(layout))

    def test_only_the_gapped_fixture_is_refused_by_columns(self):
        """The refusal must be narrow. If it widened, say which shape moved."""
        refused = []
        for layout in SYNTHETIC + ALL_LAYOUTS:
            try:
                L2G.convert_layout(layout, "columns")
            except L2G.ConversionError:
                refused.append(layout["ship"])
        assert refused == ["SynthGappedStack"], refused

    def test_gapped_stack_stays_two_grids(self):
        """The empty deck between them must survive the merge."""
        grids = L2G.convert_layout(GAPPED_STACK, "merged")["groups"][0]["grids"]
        assert len(grids) == 2, grids
        assert sorted(g["y"] for g in grids) == [0, 2]
        assert all(g["height"] == 1 for g in grids)
        assert (0, 1, 0) not in L2G.grid_cells(grids)

    def test_floating_only_keeps_its_height(self):
        entry = L2G.convert_layout(FLOATING_ONLY, "volumes")
        slots, _ = build_slots(entry)
        assert [s["y0"] for s in slots] == [4]

    def test_no_real_layout_has_a_gapped_stack(self):
        """Why the fixture above exists. If this ever fails, a real ship has
        grown the shape and the fixture stops being the only cover."""
        for layout in ALL_LAYOUTS:
            planes = {}
            for (x, y, z) in L2G.layout_cells(layout):
                planes.setdefault(y, set()).add((x, z))
            foot = {}
            for y in sorted(planes):
                for r in L2G._max_rects(planes[y]):
                    foot.setdefault(r, []).append(y)
            for ys in foot.values():
                ys = sorted(ys)
                assert all(b - a != 2 for a, b in zip(ys, ys[1:])), layout["ship"]


# ── the enabling change in build_slots ───────────────────────────────────────

class TestBuildSlotsHonorsGridY:
    """Before 2026-09-26 build_slots hardcoded y0=0 and dropped grid["y"]."""

    def test_grid_y_becomes_slot_y0(self):
        ship = {"groups": [{"x": 0, "z": 0, "grids": [
            {"x": 0, "y": 0, "z": 0, "width": 2, "height": 1, "length": 2},
            {"x": 0, "y": 3, "z": 0, "width": 2, "height": 1, "length": 2},
        ]}]}
        slots, _ = build_slots(ship)
        assert [s["y0"] for s in slots] == [0, 3]

    def test_group_y_offsets_its_grids(self):
        ship = {"groups": [{"x": 0, "y": 2, "z": 0, "grids": [
            {"x": 0, "y": 1, "z": 0, "width": 1, "height": 1, "length": 1},
        ]}]}
        assert build_slots(ship)[0][0]["y0"] == 3

    def test_missing_and_negative_y_clamp_to_zero(self):
        ship = {"groups": [{"x": 0, "z": 0, "grids": [
            {"x": 0, "z": 0, "width": 1, "height": 1, "length": 1},
            {"x": 1, "y": -5, "z": 0, "width": 1, "height": 1, "length": 1},
            {"x": 2, "y": None, "z": 0, "width": 1, "height": 1, "length": 1},
        ]}]}
        assert [s["y0"] for s in build_slots(ship)[0]] == [0, 0, 0]

    def test_hand_layout_provenance_honors_size_tags(self):
        ship = {"provenance": {"source": "hand-layout"},
                "groups": [{"x": 0, "z": 0, "grids": [
                    {"x": 0, "y": 0, "z": 0, "width": 2, "height": 2,
                     "length": 6, "minSize": 24, "maxSize": 24}]}]}
        slots, _ = build_slots(ship)
        assert (slots[0]["minSize"], slots[0]["maxSize"]) == (24, 24)
        counts = {k: v for k, v in greedy_optimize_3d(slots).items() if v}
        assert counts == {24: 1}


# ── proof the suite can fail ─────────────────────────────────────────────────
#
# A pass against a broken baseline proves nothing, so each guard below breaks
# the converter in a specific, plausible way and asserts the corresponding
# check goes red. These are the mutations a careless edit would actually make.

class TestMutationGuards:

    def test_mutation_dropping_a_placement_is_caught(self, monkeypatch):
        real = L2G.grids_from_volumes
        monkeypatch.setattr(L2G, "grids_from_volumes",
                            lambda lay: real(lay)[:-1])
        monkeypatch.setitem(L2G.MODES, "volumes", L2G.grids_from_volumes)
        with pytest.raises(L2G.ConversionError) as exc:
            L2G.convert_layout(ALL_LAYOUTS[0], "volumes")
        assert "missing" in str(exc.value)

    def test_mutation_flattening_y_is_caught(self, monkeypatch):
        """The exact bug the old renderer had: forget the deck height."""
        multi = next(L for L in ALL_LAYOUTS
                     if max(p["pos"]["y"] for p in L["placements"]) > 0)
        real = L2G.grids_from_volumes

        def flat(lay):
            out = [dict(g, y=0) for g in real(lay)]
            return out
        monkeypatch.setattr(L2G, "grids_from_volumes", flat)
        monkeypatch.setitem(L2G.MODES, "volumes", flat)
        with pytest.raises(L2G.ConversionError):
            L2G.convert_layout(multi, "volumes")

    def test_mutation_growing_a_grid_is_caught(self, monkeypatch):
        real = L2G.grids_from_merged

        def fat(lay):
            out = real(lay)
            out[0] = dict(out[0], width=out[0]["width"] + 1)
            return out
        monkeypatch.setattr(L2G, "grids_from_merged", fat)
        monkeypatch.setitem(L2G.MODES, "merged", fat)
        with pytest.raises(L2G.ConversionError) as exc:
            L2G.convert_layout(ALL_LAYOUTS[0], "merged")
        assert "invented" in str(exc.value) or "overlap" in str(exc.value)

    def test_unpinning_sizes_changes_nothing_today(self):
        """The pins in `volumes` mode are inert, and this records that.

        I expected losing minSize/maxSize to be the subtle mutation that keeps
        the cells right and moves the fill. It is not: unpinning changes the
        mix on 0 of 32 ships, because a hole exactly one container's size
        admits nothing larger, so greedy-largest-first already returns the
        layout's own histogram. So for `volumes` mode cell-exactness IS the
        complete test, and the pins are protection against a future optimiser
        rather than something load-bearing now.

        Written as an assertion rather than a comment so that if an optimiser
        change ever makes the pins matter, this fails and says so.
        """
        moved = []
        for layout in ALL_LAYOUTS:
            grids = L2G.grids_from_volumes(layout)
            unpinned = {"provenance": {"source": "hand-layout"},
                        "groups": [{"x": 0, "z": 0, "grids": [
                            dict(g, minSize=None, maxSize=None) for g in grids]}]}
            slots, _ = build_slots(unpinned)
            counts = {k: v for k, v in greedy_optimize_3d(slots).items() if v}
            want = {int(k): v for k, v in layout.get("containers", {}).items() if v}
            if counts != want:
                moved.append(layout["ship"])
        assert moved == [], (
            "unpinning now changes the mix on %s - the pins have become "
            "load-bearing, so cell-exactness alone no longer proves the "
            "conversion lossless" % moved)

    def test_mutation_build_slots_ignoring_y_is_caught(self, monkeypatch):
        import cargo_engine.packing as pk
        multi = next(L for L in ALL_LAYOUTS
                     if max(p["pos"]["y"] for p in L["placements"]) > 0)
        entry = L2G.convert_layout(multi, "volumes")
        good = {s["y0"] for s in build_slots(entry)[0]}
        assert good != {0}, "fixture must have an upper deck"

        real_build = pk.build_slots

        def flat_build(ship):
            slots, bounds = real_build(ship)
            return [dict(s, y0=0) for s in slots], bounds
        monkeypatch.setattr(pk, "build_slots", flat_build)
        assert {s["y0"] for s in pk.build_slots(entry)[0]} == {0}
