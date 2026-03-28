"""Tests for cargo_engine.packing and optimizer — 3D bin-packing."""

import os
import sys
# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargo_engine.packing import place_containers_3d, build_slots
from cargo_engine.optimizer import greedy_optimize_3d, assign_slots_from_counts
from cargo_engine.schema import CONTAINER_SIZES


class TestPlaceContainers3D:
    def test_single_32scu_in_exact_slot(self):
        slot = {"w": 2, "h": 2, "l": 8}
        result = place_containers_3d(slot, {32: 1})
        assert len(result) == 1
        lx, ly, lz, cw, ch, cl, size = result[0]
        assert size == 32
        assert cw * ch * cl == 32  # volume matches

    def test_multiple_1scu_fill_slot(self):
        slot = {"w": 2, "h": 2, "l": 2}
        result = place_containers_3d(slot, {1: 8})
        assert len(result) == 8
        # Verify no overlaps
        positions = set()
        for lx, ly, lz, cw, ch, cl, sz in result:
            for dx in range(cw):
                for dy in range(ch):
                    for dz in range(cl):
                        key = (lx+dx, ly+dy, lz+dz)
                        assert key not in positions, f"Overlap at {key}"
                        positions.add(key)

    def test_mixed_sizes(self):
        slot = {"w": 4, "h": 2, "l": 4}
        result = place_containers_3d(slot, {8: 2, 1: 4})
        total_scu = sum(sz for _, _, _, _, _, _, sz in result)
        assert total_scu == 20  # 2*8 + 4*1

    def test_overfill_drops_excess(self):
        """More containers requested than fit should silently drop extras."""
        slot = {"w": 2, "h": 2, "l": 2}
        result = place_containers_3d(slot, {8: 5})  # only 1 can fit
        assert len(result) == 1

    def test_empty_assignment(self):
        slot = {"w": 4, "h": 4, "l": 4}
        result = place_containers_3d(slot, {})
        assert result == []


class TestBuildSlots:
    def test_simple_ship(self):
        ship = {
            "groups": [{
                "x": 0, "z": 0,
                "grids": [
                    {"x": 0, "z": 0, "width": 4, "height": 2, "length": 8},
                    {"x": 4, "z": 0, "width": 4, "height": 2, "length": 8},
                ]
            }]
        }
        slots, bounds = build_slots(ship)
        assert len(slots) == 2
        x_min, z_min, x_max, z_max = bounds
        assert x_min == 0
        assert x_max == 8
        assert z_max == 8

    def test_empty_ship(self):
        slots, bounds = build_slots({"groups": []})
        assert slots == []
        assert bounds == (0, 0, 1, 1)


class TestGreedyOptimize:
    def test_fills_with_largest(self):
        slots = [{"w": 2, "h": 2, "l": 8, "capacity": 32}]
        counts = greedy_optimize_3d(slots)
        assert counts[32] >= 1

    def test_respects_max_size(self):
        slots = [{"w": 2, "h": 2, "l": 4, "capacity": 16, "maxSize": 8}]
        counts = greedy_optimize_3d(slots)
        assert counts.get(16, 0) == 0
        assert counts.get(32, 0) == 0

    def test_respects_min_size(self):
        slots = [{"w": 2, "h": 2, "l": 2, "capacity": 8, "minSize": 4}]
        counts = greedy_optimize_3d(slots)
        assert counts.get(1, 0) == 0
        assert counts.get(2, 0) == 0


class TestAssignSlotsFromCounts:
    def test_assigns_to_single_slot(self):
        # 32 SCU needs 2x2x8 — slot must be large enough
        slots = [{"w": 4, "h": 4, "l": 8, "capacity": 128}]
        counts = {32: 1, 8: 2}
        result = assign_slots_from_counts(slots, counts)
        assert len(result) == 1
        assert result[0].get(32, 0) == 1
        assert result[0].get(8, 0) == 2

    def test_distributes_across_slots(self):
        slots = [
            {"w": 2, "h": 2, "l": 8, "capacity": 32},
            {"w": 2, "h": 2, "l": 8, "capacity": 32},
        ]
        counts = {32: 2}
        result = assign_slots_from_counts(slots, counts)
        total = sum(a.get(32, 0) for a in result)
        assert total == 2


class TestValidation:
    def test_valid_layout(self):
        from cargo_engine.validation import validate_layout
        data = {
            "schemaVersion": 1,
            "ship": "Test Ship",
            "gridW": 10, "gridZ": 10, "gridH": 4,
            "placements": [{
                "scu": 8,
                "dims": {"w": 2, "h": 2, "l": 2},
                "pos": {"x": 0, "y": 0, "z": 0},
                "rotation": 0,
            }]
        }
        errors = validate_layout(data)
        assert errors == []

    def test_invalid_scu(self):
        from cargo_engine.validation import validate_layout
        data = {
            "schemaVersion": 1,
            "ship": "Test",
            "gridW": 10, "gridZ": 10, "gridH": 4,
            "placements": [{"scu": 99, "dims": {"w": 1, "h": 1, "l": 1}, "pos": {"x": 0, "y": 0, "z": 0}}]
        }
        errors = validate_layout(data)
        assert any("invalid scu" in e.lower() or "Invalid scu" in e for e in errors)

    def test_out_of_bounds(self):
        from cargo_engine.validation import validate_layout
        data = {
            "schemaVersion": 1,
            "ship": "Test",
            "gridW": 4, "gridZ": 4, "gridH": 4,
            "placements": [{"scu": 32, "dims": {"w": 2, "h": 2, "l": 8}, "pos": {"x": 0, "y": 0, "z": 0}}]
        }
        errors = validate_layout(data)
        assert any("Exceeds" in e for e in errors)


class TestRendering:
    def test_topological_sort_simple(self):
        from cargo_engine.rendering import topological_sort_boxes
        # Two non-overlapping boxes: one behind the other
        boxes = [
            (0, 0, 0, 2, 2, 2, 8),  # near origin
            (4, 0, 0, 2, 2, 2, 8),  # further right (+X)
        ]
        result = topological_sort_boxes(boxes)
        assert len(result) == 2
        # Box at x=0 should be drawn first (it's behind x=4)
        assert result[0][0] == 0
        assert result[1][0] == 4

    def test_topological_sort_stacked(self):
        from cargo_engine.rendering import topological_sort_boxes
        boxes = [
            (0, 2, 0, 2, 2, 2, 8),  # on top
            (0, 0, 0, 2, 2, 2, 8),  # on bottom
        ]
        result = topological_sort_boxes(boxes)
        assert len(result) == 2
        # Bottom box drawn first
        assert result[0][1] == 0
        assert result[1][1] == 2

    def test_iso_project(self):
        from cargo_engine.rendering import iso_project
        # Origin point
        sx, sy = iso_project(0, 0, 0, 16, 200, 200)
        assert sx == 200
        assert sy == 200

    def test_shade(self):
        from cargo_engine.rendering import shade
        assert shade("#ffffff", 0.5) == "#7f7f7f"
        assert shade("#000000", 1.0) == "#000000"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
