"""_draw_ground draws a floor under every slot, at that slot's own height.

WHY THIS FILE EXISTS. On 2026-09-26 the hand-layout branch of _draw_ground drew one
polygon with y hardcoded to 0, so on the 29 layouts with slots above the deck every
upper-deck box was painted with nothing beneath it. The branch was deleted in favour
of the per-slot loop that the other 113 ships already used.

The suite did not notice -- 489 passed before the change and 489 after. So I mutated
_draw_ground to `return` immediately, drawing NO floor at all on any ship, and the
suite still reported 489 passed. Nothing in this repo tested the floor renderer, and
"my change didn't break anything" was a statement about coverage, not correctness.

These call _draw_ground against a recording stub rather than a live QGraphicsScene:
the function's entire job is choosing geometry, and the Qt item it hands that geometry
to is not the part that was wrong.
"""
import importlib
import sys
import types

import pytest

cargo_app = importlib.import_module("cargo_app")
CargoRenderer = cargo_app.CargoRenderer


class Recorder:
    """Stands in for the renderer, capturing what would have been drawn."""

    def __init__(self):
        self.polygons = []
        self.lines = []

    def _add_polygon(self, points, fill_color, outline_color):
        self.polygons.append(list(points))
        return None

    def _add_line(self, p1, p2, color):
        self.lines.append((p1, p2))


def _pt(x, y, z):
    """Identity projection: keep the 3-D coordinate so the test can assert on y."""
    return (x, y, z)


def _draw(slots, cell=12):
    r = Recorder()
    CargoRenderer._draw_ground(r, slots, (0, 0), True, {"name": "Test Ship"},
                               _pt, cell, 10, 10)
    return r


def _slot(x, z, w, l, y0):
    return {"x": x, "z": z, "w": w, "l": l, "y0": y0}


def test_a_slot_on_an_upper_deck_gets_its_floor_at_its_own_height():
    """The original defect: Starfarer reaches y=4 and was drawn on the floor."""
    r = _draw([_slot(0, 0, 2, 2, 0), _slot(0, 0, 2, 2, 4)])

    heights = sorted({p[1] for poly in r.polygons for p in poly})
    assert heights == [0, 4], (
        "expected a floor at y=0 and another at y=4, got %r -- a hardcoded 0 here "
        "is what left upper decks floating" % heights)


def test_every_slot_gets_exactly_one_floor():
    """Drawn and legal must be the same set.

    The deleted branch painted the layout's full gridW x gridZ rectangle while the
    placement rules bound to the slot volumes, so a visibly solid deck refused
    cargo: only 20.6% of the Idris-P's drawn floor would accept a box.
    """
    slots = [_slot(0, 0, 2, 2, 0), _slot(4, 0, 2, 2, 0), _slot(0, 6, 3, 3, 2)]
    r = _draw(slots)
    assert len(r.polygons) == len(slots)


def test_the_floor_corners_sit_on_the_slot_footprint():
    r = _draw([_slot(3, 5, 2, 4, 1)])
    assert len(r.polygons) == 1
    assert sorted(r.polygons[0]) == sorted([(3, 1, 5), (5, 1, 5), (5, 1, 9), (3, 1, 9)])


def test_gridlines_follow_the_floor_up():
    """A gridline left at y=0 under a raised floor is the same bug, less visible."""
    r = _draw([_slot(0, 0, 2, 2, 3)])
    assert r.lines, "no gridlines drawn at all at cell=12"
    ys = {p[1] for pair in r.lines for p in pair}
    assert ys == {3}, "gridlines drawn at %r, expected all at the slot's y0=3" % ys


def test_a_missing_y0_still_draws_at_the_deck():
    """Slots from the older path carry no y0; they must not vanish or float."""
    r = _draw([{"x": 0, "z": 0, "w": 2, "l": 2}])
    assert len(r.polygons) == 1
    assert {p[1] for p in r.polygons[0]} == {0}


def test_no_slots_draws_no_floor():
    """The empty case must not fall back to painting a phantom deck."""
    assert _draw([]).polygons == []
