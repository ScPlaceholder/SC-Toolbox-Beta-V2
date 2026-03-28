"""Tests for cargo_engine.placement — rotation selection and capacity."""

import os
import sys
# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargo_engine.placement import best_rotation, max_containers_in_slot, packed_to_rotation
from cargo_engine.schema import CONTAINER_DIMS, CONTAINER_MAX_STACK_HEIGHT


class TestBestRotation:
    def test_1scu_fits_1x1x1(self):
        result = best_rotation((1, 1, 1), 1, 1, 1)
        assert result == (1, 1, 1)

    def test_2scu_fits_in_2x1x1_slot(self):
        dims = CONTAINER_DIMS[2]  # (1, 1, 2)
        result = best_rotation(dims, 2, 1, 1)
        assert result is not None
        cw, ch, cl = result
        assert cw <= 2 and ch <= 1 and cl <= 1

    def test_4scu_respects_max_height(self):
        """4 SCU boxes are flat (h=1) and cannot stand on their end."""
        dims = CONTAINER_DIMS[4]  # (2, 1, 2)
        max_ch = CONTAINER_MAX_STACK_HEIGHT.get(4)
        assert max_ch == 1
        result = best_rotation(dims, 2, 2, 2, max_ch=max_ch)
        assert result is not None
        _, ch, _ = result
        assert ch <= 1, "4 SCU must have height <= 1"

    def test_no_fit_returns_none(self):
        result = best_rotation((2, 2, 8), 1, 1, 1)
        assert result is None

    def test_minimizes_height(self):
        """Should prefer flat orientation over tall."""
        dims = (2, 1, 4)  # e.g. 16 SCU
        result = best_rotation(dims, 4, 4, 4)
        assert result is not None
        _, ch, _ = result
        assert ch == 1, "Should minimize height"

    def test_maximizes_floor_area_at_same_height(self):
        dims = (2, 1, 4)
        result = best_rotation(dims, 4, 4, 4)
        assert result is not None
        cw, ch, cl = result
        assert ch == 1
        assert cw * cl >= 8  # 2*4 or 4*2


class TestMaxContainersInSlot:
    def test_1scu_in_2x2x2(self):
        assert max_containers_in_slot(1, 2, 2, 2) == 8

    def test_8scu_in_2x2x2(self):
        assert max_containers_in_slot(8, 2, 2, 2) == 1

    def test_32scu_in_2x2x8(self):
        assert max_containers_in_slot(32, 2, 2, 8) == 1

    def test_32scu_doesnt_fit_1x1x1(self):
        assert max_containers_in_slot(32, 1, 1, 1) == 0

    def test_4scu_height_constraint(self):
        """4 SCU should fit flat (h=1) even in tall slots."""
        n = max_containers_in_slot(4, 2, 4, 2)
        assert n > 0
        # With h=1, 4 layers of height 1 in a slot of h=4
        assert n == 4  # (2/2)*(4/1)*(2/2)


class TestPackedToRotation:
    def test_base_dims_return_0(self):
        for scu in [1, 2, 4, 8, 16, 24, 32]:
            bw, bh, bl = CONTAINER_DIMS[scu]
            assert packed_to_rotation(scu, bw, bh, bl) == 0

    def test_swapped_dims_return_90(self):
        for scu in [2, 16, 24, 32]:  # asymmetric containers
            bw, bh, bl = CONTAINER_DIMS[scu]
            if bw != bl:  # only if w != l
                assert packed_to_rotation(scu, bl, bh, bw) == 90


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
