"""
Collision detection — occupancy grid for 3D voxel-based overlap checks.

Provides both the old 3D list approach (for packing) and a hash-map
approach (for the editor) in a unified interface.
"""


class OccupancyGrid:
    """Sparse voxel occupancy grid using a dict for O(1) lookups.

    Each cell maps to an arbitrary owner ID (or True for anonymous).
    Supports spatial queries for collision detection and hit testing.
    """

    __slots__ = ("_cells",)

    def __init__(self):
        self._cells: dict[tuple[int, int, int], object] = {}

    def set_region(self, x: int, y: int, z: int,
                   w: int, h: int, l: int, owner: object = True):
        """Mark all cells in the (x,y,z)→(x+w, y+h, z+l) box as occupied."""
        for dx in range(w):
            for dy in range(h):
                for dz in range(l):
                    self._cells[(x + dx, y + dy, z + dz)] = owner

    def clear_region(self, x: int, y: int, z: int,
                     w: int, h: int, l: int):
        """Free all cells in the given box."""
        for dx in range(w):
            for dy in range(h):
                for dz in range(l):
                    self._cells.pop((x + dx, y + dy, z + dz), None)

    def is_blocked(self, x: int, y: int, z: int,
                   w: int, h: int, l: int,
                   skip_owner: object = None) -> bool:
        """Check if any cell in the box is occupied (optionally ignoring one owner)."""
        for dx in range(w):
            for dy in range(h):
                for dz in range(l):
                    owner = self._cells.get((x + dx, y + dy, z + dz))
                    if owner is not None and owner != skip_owner:
                        return True
        return False

    def owner_at(self, x: int, y: int, z: int) -> object | None:
        """Return the owner at a single cell, or None."""
        return self._cells.get((x, y, z))

    def clear(self):
        """Remove all occupancy data."""
        self._cells.clear()

    def __len__(self) -> int:
        return len(self._cells)

    def __contains__(self, key: tuple[int, int, int]) -> bool:
        return key in self._cells


def check_bounds(x: int, y: int, z: int,
                 w: int, h: int, l: int,
                 grid_w: int, grid_z: int, grid_h: int) -> bool:
    """Return True if the box fits within the grid bounds."""
    return (x >= 0 and y >= 0 and z >= 0
            and x + w <= grid_w
            and y + h <= grid_h
            and z + l <= grid_z)
