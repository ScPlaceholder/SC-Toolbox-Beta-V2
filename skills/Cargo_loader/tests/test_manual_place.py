"""Tests for cargo_engine.manual_place — drag-and-drop snapping and validity."""

import os
import sys
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from cargo_engine.manual_place import (  # noqa: E402
    snap_position, rotate_yaw, move_box, PlacementContext,
    OK, R_OUTSIDE, R_OVERLAP, R_TOO_TALL, R_UNSUPPORTED, R_TOO_BIG_FOR_GRID,
)
from cargo_engine.rendering import iso_project, iso_unproject  # noqa: E402


def grid(x=0, z=0, w=10, h=4, l=10, mx=None, mn=None):
    return {"x": x, "y0": 0, "z": z, "w": w, "h": h, "l": l,
            "maxSize": mx, "minSize": mn}


B8 = (2, 2, 2, 8)      # 8 SCU cube (w, h, l, size)
B1 = (1, 1, 1, 1)


class TestGridSnap:
    def test_fractional_target_rounds_to_whole_cells(self):
        pos, ok, why = snap_position(B1, (3.3, 5.2), [grid()], [])
        assert (pos, ok, why) == ((3, 0, 5), True, OK)

    def test_rounds_up_past_half(self):
        pos, ok, _ = snap_position(B1, (3.6, 4.51), [grid()], [])
        assert pos == (4, 0, 5) and ok


class TestFlushSnap:
    def test_pulls_flush_against_neighbour(self):
        placed = [(0, 0, 0, 2, 2, 2, 8)]
        # plain rounding of 2.7 would leave a 1-cell gap at x=3
        pos, ok, _ = snap_position(B8, (2.7, 0.0), [grid()], placed)
        assert pos[0] == 2 and ok

    def test_far_from_neighbour_just_rounds(self):
        placed = [(0, 0, 0, 2, 2, 2, 8)]
        pos, ok, _ = snap_position(B8, (3.8, 0.0), [grid()], placed)
        assert pos[0] == 4 and ok

    def test_pulls_flush_against_grid_wall(self):
        # box max face would be at 9.3 -> 0.7 from the wall at 10
        pos, ok, _ = snap_position(B8, (7.3, 3.0), [grid()], [])
        assert pos == (8, 0, 3) and ok

    def test_neighbour_not_lined_up_does_not_attract(self):
        placed = [(0, 0, 6, 2, 2, 2, 8)]           # far away in Z
        pos, _, _ = snap_position(B8, (2.7, 0.0), [grid()], placed)
        assert pos[0] == 3


class TestInsideGrid:
    def test_centre_outside_every_grid_is_invalid(self):
        pos, ok, why = snap_position(B8, (14.0, 2.0), [grid()], [])
        assert not ok and why == R_OUTSIDE

    def test_overhang_is_clamped_into_the_grid_under_the_centre(self):
        pos, ok, _ = snap_position(B8, (8.9, -0.9), [grid()], [])  # centre (9.9, 0.1) inside
        assert ok and pos == (8, 0, 0)

    def test_must_fit_one_grid_not_straddle_two(self):
        grids = [grid(0, 0, 3, 4, 3), grid(3, 0, 3, 4, 3)]
        box = (4, 1, 2, 4)
        pos, ok, why = snap_position(box, (1.0, 0.0), grids, [])
        assert not ok and why == R_TOO_BIG_FOR_GRID

    def test_second_grid_is_usable(self):
        grids = [grid(0, 0, 4, 4, 4), grid(10, 0, 4, 4, 4)]
        pos, ok, _ = snap_position(B8, (11.0, 1.0), grids, [])
        assert ok and pos == (11, 0, 1)


class TestOverlap:
    def test_explicit_height_into_another_box_is_invalid(self):
        placed = [(2, 0, 2, 2, 2, 2, 8)]
        pos, ok, why = snap_position(B8, (3.0, 0.0, 3.0), [grid()], placed)
        assert not ok and why == R_OVERLAP

    def test_side_by_side_is_fine(self):
        placed = [(2, 0, 2, 2, 2, 2, 8)]
        pos, ok, _ = snap_position(B8, (4.0, 0.0, 2.0), [grid()], placed)
        assert ok and pos == (4, 0, 2)


class TestSizeRules:
    def test_max_size(self):
        box16 = (2, 2, 4, 16)
        pos, ok, why = snap_position(box16, (1.0, 1.0), [grid(mx=8)], [])
        assert not ok and "up to 8 SCU" in why

    def test_min_size(self):
        pos, ok, why = snap_position(B1, (1.0, 1.0), [grid(mn=2)], [])
        assert not ok and "2 SCU or more" in why

    def test_within_limits(self):
        pos, ok, _ = snap_position(B8, (1.0, 1.0), [grid(mn=1, mx=32)], [])
        assert ok


class TestSupport:
    def test_drops_onto_box_below(self):
        placed = [(2, 0, 2, 2, 2, 2, 8)]
        pos, ok, _ = snap_position(B8, (2.0, 2.0), [grid()], placed)
        assert ok and pos == (2, 2, 2)

    def test_overhang_is_unsupported(self):
        placed = [(2, 0, 2, 1, 1, 1, 1)]           # 1-cell pillar
        pos, ok, why = snap_position(B8, (2.0, 2.0), [grid()], placed)
        assert pos[1] == 1 and not ok and why == R_UNSUPPORTED

    def test_explicit_floating_box_is_unsupported(self):
        pos, ok, why = snap_position(B8, (4.0, 1.0, 4.0), [grid()], [])
        assert not ok and why == R_UNSUPPORTED

    def test_stack_taller_than_grid_is_invalid(self):
        placed = [(2, 0, 2, 2, 2, 2, 8)]
        pos, ok, why = snap_position(B8, (2.0, 2.0), [grid(h=3)], placed)
        assert not ok and why == R_TOO_TALL


class TestUnionMode:
    """Hand-made layouts: any cell of any placement volume is fair game."""

    def test_spanning_two_adjacent_volumes_is_ok(self):
        vols = [grid(0, 0, 2, 2, 2), grid(2, 0, 2, 2, 2)]
        pos, ok, _ = snap_position((4, 1, 2, 8), (0.0, 0.0), vols, [], union=True)
        assert ok and pos == (0, 0, 0)

    def test_outside_every_volume_is_invalid(self):
        vols = [grid(0, 0, 2, 2, 2)]
        pos, ok, why = snap_position(B8, (5.0, 5.0), vols, [], union=True)
        assert not ok and why == R_OUTSIDE


class TestHelpers:
    def test_rotate_yaw_keeps_height(self):
        assert rotate_yaw((2, 1, 4)) == (4, 1, 2)
        assert rotate_yaw(rotate_yaw((2, 1, 4))) == (2, 1, 4)

    def test_move_box_keeps_size_and_copies(self):
        boxes = [(0, 0, 0, 2, 2, 2, 8), (4, 0, 0, 1, 1, 1, 1)]
        out = move_box(boxes, 0, (6, 0, 6), (2, 2, 2))
        assert out[0] == (6, 0, 6, 2, 2, 2, 8) and boxes[0] == (0, 0, 0, 2, 2, 2, 8)

    def test_context_is_reusable_across_moves(self):
        ctx = PlacementContext([grid()], [(0, 0, 0, 2, 2, 2, 8)])
        assert ctx.snap(B8, (5.0, 5.0))[1]
        assert ctx.snap(B8, (0.0, 0.0))[0][1] == 2      # lands on top

    @pytest.mark.parametrize("rot", [0, 1, 2, 3])
    def test_unproject_inverts_project(self, rot):
        for wx, wz, wy in [(0.0, 0.0, 0.0), (3.5, 7.25, 2.0), (12.0, 1.0, 1.0)]:
            sx, sy = iso_project(wx, wy, wz, 20.0, 400.0, 100.0,
                                 rotation=rot, total_gw=14, total_gl=9)
            ux, uz = iso_unproject(sx, sy, wy, 20.0, 400.0, 100.0,
                                   rotation=rot, total_gw=14, total_gl=9)
            # iso_project rounds to int pixels: allow 1px / 20px-cell error
            assert abs(ux - wx) < 0.1 and abs(uz - wz) < 0.1
