"""Tests for cargo_engine.collision — occupancy grid."""

import os
import sys
# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargo_engine.collision import OccupancyGrid, check_bounds


class TestOccupancyGrid:
    def test_empty_grid_not_blocked(self):
        grid = OccupancyGrid()
        assert not grid.is_blocked(0, 0, 0, 2, 2, 2)

    def test_set_and_check(self):
        grid = OccupancyGrid()
        grid.set_region(0, 0, 0, 2, 2, 2, owner="box1")
        assert grid.is_blocked(0, 0, 0, 1, 1, 1)
        assert grid.is_blocked(1, 1, 1, 1, 1, 1)

    def test_adjacent_not_blocked(self):
        grid = OccupancyGrid()
        grid.set_region(0, 0, 0, 2, 2, 2, owner="box1")
        assert not grid.is_blocked(2, 0, 0, 1, 1, 1)

    def test_skip_owner(self):
        grid = OccupancyGrid()
        grid.set_region(0, 0, 0, 2, 2, 2, owner="box1")
        assert not grid.is_blocked(0, 0, 0, 2, 2, 2, skip_owner="box1")
        assert grid.is_blocked(0, 0, 0, 2, 2, 2, skip_owner="box2")

    def test_clear_region(self):
        grid = OccupancyGrid()
        grid.set_region(0, 0, 0, 2, 2, 2, owner="box1")
        grid.clear_region(0, 0, 0, 2, 2, 2)
        assert not grid.is_blocked(0, 0, 0, 2, 2, 2)

    def test_owner_at(self):
        grid = OccupancyGrid()
        grid.set_region(5, 3, 7, 1, 1, 1, owner="mybox")
        assert grid.owner_at(5, 3, 7) == "mybox"
        assert grid.owner_at(0, 0, 0) is None

    def test_clear_all(self):
        grid = OccupancyGrid()
        grid.set_region(0, 0, 0, 10, 10, 10, owner=True)
        assert len(grid) == 1000
        grid.clear()
        assert len(grid) == 0

    def test_overlap_detection(self):
        grid = OccupancyGrid()
        grid.set_region(0, 0, 0, 4, 2, 4, owner="box1")
        # Partially overlapping
        assert grid.is_blocked(2, 0, 2, 4, 2, 4)
        # Fully outside
        assert not grid.is_blocked(4, 0, 4, 2, 2, 2)


class TestCheckBounds:
    def test_within_bounds(self):
        assert check_bounds(0, 0, 0, 2, 2, 8, 10, 10, 4)

    def test_at_edge(self):
        assert check_bounds(8, 0, 2, 2, 2, 8, 10, 10, 4)

    def test_exceeds_width(self):
        assert not check_bounds(9, 0, 0, 2, 2, 2, 10, 10, 4)

    def test_exceeds_height(self):
        assert not check_bounds(0, 3, 0, 2, 2, 2, 10, 10, 4)

    def test_negative_position(self):
        assert not check_bounds(-1, 0, 0, 1, 1, 1, 10, 10, 4)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
