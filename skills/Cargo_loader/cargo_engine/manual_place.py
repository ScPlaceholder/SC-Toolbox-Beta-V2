"""
Manual placement — snapping and validity rules for drag-and-drop box moves.

Pure logic, no UI. The Cargo Loader's drag handler converts the mouse to a
continuous world (x, z) target and asks this module where the box would land
and whether that landing is legal.

Coordinates are world cells (1 cell = 1 SCU edge). A box is described by its
min corner (x, y, z) and its extents (w, h, l) along X, Y (up) and Z.

Rules (in the order they are checked):
  1. Grid snap     every coordinate is a whole cell.
  2. Flush snap    if the target is within FLUSH_THRESHOLD cells of a position
                   where the box would sit flush against a neighbour's face or a
                   grid wall, it jumps there (a small "magnet").
  3. Inside a grid the box must lie fully inside ONE cargo grid (the grid under
                   the box centre; the footprint is clamped into it). In union
                   mode (hand-made layouts, whose "grids" are the placement
                   volumes) every cell of the box must lie inside some volume.
  4. Size rules    the grid's minSize / maxSize, when the data carries them.
  5. Height        the box must not poke out of the top of the grid.
  6. Overlap       no cell shared with another box (collision.OccupancyGrid).
  7. Support       a box above the floor needs a box under EVERY footprint cell
                   (no overhangs, no floating). Without an explicit y the box
                   drops onto the highest box under its footprint.
"""

from __future__ import annotations

import math

from cargo_engine.collision import OccupancyGrid

FLUSH_THRESHOLD = 0.75

# Reasons (stable strings; the UI shows them, tests compare them)
OK = "ok"
R_OUTSIDE = "outside the cargo grids"
R_TOO_BIG_FOR_GRID = "does not fit in this grid"
R_MAX_SIZE = "grid only takes containers up to {n} SCU"
R_MIN_SIZE = "grid only takes containers of {n} SCU or more"
R_TOO_TALL = "sticks out of the top of the grid"
R_OVERLAP = "overlaps another container"
R_UNSUPPORTED = "not supported underneath (would float or overhang)"

# Item warnings (items place anyway; the UI tints them amber and says why)
W_OUTSIDE = "outside the cargo grids"
W_TOO_TALL = "sticks out of the top of the grid"
W_OVERLAP = "overlaps another box"
W_FLOATING = "floating or overhanging"


def is_item(box: tuple) -> bool:
    """Items carry their catalogue key (a str) where containers carry SCU."""
    return isinstance(box[6], str)


def _aabb_overlap(a: tuple, b: tuple) -> bool:
    return (a[0] < b[0] + b[3] and b[0] < a[0] + a[3]
            and a[1] < b[1] + b[4] and b[1] < a[1] + a[4]
            and a[2] < b[2] + b[5] and b[2] < a[2] + a[5])


def rotate_yaw(dims: tuple[int, int, int]) -> tuple[int, int, int]:
    """Rotate a box 90 degrees about the vertical axis: swap w and l.

    Height is untouched, so a flat-only container (2/4 SCU, maxStackHeight 1)
    stays flat — the same rule the auto-packer applies.
    """
    w, h, l = dims
    return (l, h, w)


def _round(v: float) -> int:
    return int(math.floor(v + 0.5))


def _snap_axis(t: float, size: int, edges: list[int], thr: float) -> int:
    """Snap one axis: nearest flush candidate within *thr*, else nearest cell."""
    best = None
    best_d = None
    for e in edges:
        for c in (e, e - size):          # our min face on e, or our max face on e
            d = abs(t - c)
            if best_d is None or d < best_d:
                best, best_d = c, d
    if best is not None and best_d <= thr:
        return int(best)
    return _round(t)


def _g(grid: dict, key: str, default=0):
    v = grid.get(key, default)
    return default if v is None else v


class PlacementContext:
    """Pre-built state for one drag (grids + the other boxes).

    Build once when the drag starts; call snap() on every mouse move.
    """

    def __init__(self, grids: list[dict], placed: list[tuple], *,
                 union: bool = False, flush_threshold: float = FLUSH_THRESHOLD):
        self.grids = list(grids)
        self.placed = [tuple(b) for b in placed]
        self.union = union
        self.thr = flush_threshold
        self.occ = OccupancyGrid()
        for i, (x, y, z, w, h, l, _s) in enumerate(self.placed):
            self.occ.set_region(x, y, z, w, h, l, owner=i)
        self._union_cells: set[tuple[int, int, int]] | None = None
        if union:
            cells = set()
            for g in self.grids:
                gx, gy, gz = _g(g, "x"), _g(g, "y0"), _g(g, "z")
                for dx in range(g["w"]):
                    for dy in range(g["h"]):
                        for dz in range(g["l"]):
                            cells.add((gx + dx, gy + dy, gz + dz))
            self._union_cells = cells

    # -- helpers -------------------------------------------------------------

    def _grid_under(self, cx: float, cz: float) -> dict | None:
        for g in self.grids:
            gx, gz = _g(g, "x"), _g(g, "z")
            if gx <= cx < gx + g["w"] and gz <= cz < gz + g["l"]:
                return g
        return None

    def _wall_edges(self, grid: dict | None, axis: str) -> list[int]:
        src = [grid] if grid is not None else self.grids
        span = "w" if axis == "x" else "l"
        out = []
        for g in src:
            o = _g(g, axis)
            out += [o, o + g[span]]
        return out

    def _neighbour_edges(self, axis: str, other_lo: int, other_len: int) -> list[int]:
        """Faces of boxes that line up with us on the other floor axis."""
        out = []
        for (x, y, z, w, h, l, _s) in self.placed:
            if axis == "x":
                if z < other_lo + other_len and other_lo < z + l:
                    out += [x, x + w]
            else:
                if x < other_lo + other_len and other_lo < x + w:
                    out += [z, z + l]
        return out

    def _drop_height(self, x: int, z: int, w: int, l: int, floor: int) -> int:
        top = floor
        for (bx, by, bz, bw, bh, bl, _s) in self.placed:
            if bx < x + w and x < bx + bw and bz < z + l and z < bz + bl:
                top = max(top, by + bh)
        return top

    def _supported(self, x: int, y: int, z: int, w: int, l: int, floor: int) -> bool:
        if y <= floor:
            return True
        for dx in range(w):
            for dz in range(l):
                if self.occ.owner_at(x + dx, y - 1, z + dz) is None:
                    return False
        return True

    # -- main entry ----------------------------------------------------------

    def snap(self, box: tuple, target: tuple) -> tuple[tuple[int, int, int], bool, str]:
        """Where does *box* land if dropped at *target*, and is that legal?

        box    = (w, h, l, size)   extents in its CURRENT rotation
        target = (x, z) or (x, y, z) — desired min corner, may be fractional;
                 without y the box drops onto whatever is below it.
        Returns ((x, y, z), valid, reason).
        """
        w, h, l, size = box
        tx, tz = float(target[0]), float(target[-1])
        ty = target[1] if len(target) == 3 else None

        grid = None if self.union else self._grid_under(tx + w / 2.0, tz + l / 2.0)

        # 1+2: grid snap with flush magnet. Z first (rounded) so X can pick
        # neighbours that line up in Z, then X, then Z again against X.
        z0 = _round(tz)
        x = _snap_axis(tx, w, self._wall_edges(grid, "x")
                       + self._neighbour_edges("x", z0, l), self.thr)
        z = _snap_axis(tz, l, self._wall_edges(grid, "z")
                       + self._neighbour_edges("z", x, w), self.thr)

        if self.union:
            floor = min((_g(g, "y0") for g in self.grids
                         if _g(g, "x") < x + w and x < _g(g, "x") + g["w"]
                         and _g(g, "z") < z + l and z < _g(g, "z") + g["l"]),
                        default=0)
        else:
            if grid is None:
                return (x, 0, z), False, R_OUTSIDE
            gx, gz = _g(grid, "x"), _g(grid, "z")
            if w > grid["w"] or l > grid["l"]:
                return (x, _g(grid, "y0"), z), False, R_TOO_BIG_FOR_GRID
            # 3: clamp the footprint into the ghost of this grid
            x = min(max(x, gx), gx + grid["w"] - w)
            z = min(max(z, gz), gz + grid["l"] - l)
            floor = _g(grid, "y0")

        y = self._drop_height(x, z, w, l, floor) if ty is None else _round(ty)
        pos = (x, y, z)

        if self.union:
            cells = self._union_cells or set()
            for dx in range(w):
                for dy in range(h):
                    for dz in range(l):
                        if (x + dx, y + dy, z + dz) not in cells:
                            above = y + dy >= floor
                            return pos, False, (R_TOO_TALL if above and all(
                                (x + ex, floor, z + ez) in cells
                                for ex in range(w) for ez in range(l))
                                else R_OUTSIDE)
        else:
            # 4: size rules
            mx, mn = grid.get("maxSize"), grid.get("minSize")
            if mx is not None and size > mx:
                return pos, False, R_MAX_SIZE.format(n=mx)
            if mn is not None and size < mn:
                return pos, False, R_MIN_SIZE.format(n=mn)
            # 5: height
            if y < floor:
                return pos, False, R_OUTSIDE
            if y + h > floor + grid["h"]:
                return pos, False, R_TOO_TALL

        # 6: overlap
        if self.occ.is_blocked(x, y, z, w, h, l):
            return pos, False, R_OVERLAP
        # 7: support
        if not self._supported(x, y, z, w, l, floor):
            return pos, False, R_UNSUPPORTED
        return pos, True, OK

    # -- items: warnings, not walls -------------------------------------------
    #
    # J, 2026-09-26: the quartermaster snaps items down; if they do not match
    # the bay "that's user error not engine error", and impossible stacks are
    # realistic. So an item always places. It snaps to whole cells and to the
    # same flush magnet as containers (grid walls, containers AND other
    # items), and every rule a container must obey becomes a warning. No
    # per-item game snapping is modelled, on purpose.

    def _item_floor(self, x: int, z: int, w: int, l: int) -> int:
        ys = [_g(g, "y0") for g in self.grids
              if _g(g, "x") < x + w and x < _g(g, "x") + g["w"]
              and _g(g, "z") < z + l and z < _g(g, "z") + g["l"]]
        return min(ys) if ys else 0

    def _item_rest(self, x: int, z: int, w: int, l: int, floor: int) -> int:
        """Rest on the tallest box under the item's MIDDLE cells (one or two
        per axis). What the rest of the footprint touches is the player's
        call and shows up as a warning."""
        xs = (x + (w - 1) // 2, x + w // 2)
        zs = (z + (l - 1) // 2, z + l // 2)
        top = floor
        for (bx, by, bz, bw, bh, bl, _s) in self.placed:
            if (any(bx <= cx < bx + bw for cx in xs)
                    and any(bz <= cz < bz + bl for cz in zs)):
                top = max(top, by + bh)
        return top

    def item_warnings(self, pos: tuple, dims: tuple, *, skip: int | None = None) -> list[str]:
        """Every container rule this item breaks, as warnings (empty = clean).

        skip: index in self.placed of the item itself, when it is in there.
        """
        x, y, z = pos
        w, h, l = dims
        out: list[str] = []
        # grids: every column of the footprint under some grid, and the item
        # between that grid's floor and ceiling
        where = None
        for dx in range(w):
            for dz in range(l):
                cx, cz = x + dx, z + dz
                gs = [g for g in self.grids
                      if _g(g, "x") <= cx < _g(g, "x") + g["w"]
                      and _g(g, "z") <= cz < _g(g, "z") + g["l"]]
                if not gs:
                    where = W_OUTSIDE
                    break
                if not any(_g(g, "y0") <= y and y + h <= _g(g, "y0") + g["h"] for g in gs):
                    if any(_g(g, "y0") <= y for g in gs):
                        where = where or W_TOO_TALL
                    else:
                        where = W_OUTSIDE
                        break
            if where == W_OUTSIDE:
                break
        if where:
            out.append(where)
        me = (x, y, z, w, h, l)
        if any(i != skip and _aabb_overlap(me, b) for i, b in enumerate(self.placed)):
            out.append(W_OVERLAP)
        if not self._supported(x, y, z, w, l, self._item_floor(x, z, w, l)):
            out.append(W_FLOATING)
        return out

    def snap_item(self, box: tuple, target: tuple,
                  y: int | None = None) -> tuple[tuple[int, int, int], list[str]]:
        """Where an item lands: ((x, y, z), warnings). It always lands.

        box    = (w, h, l, key) in its current rotation
        target = (x, z) desired min corner, may be fractional
        y      = explicit height (stacking on a box the player pointed at);
                 None rests it on whatever is under its middle.
        """
        w, h, l = box[0], box[1], box[2]
        tx, tz = float(target[0]), float(target[-1])
        grid = self._grid_under(tx + w / 2.0, tz + l / 2.0)
        z0 = _round(tz)
        x = _snap_axis(tx, w, self._wall_edges(grid, "x")
                       + self._neighbour_edges("x", z0, l), self.thr)
        z = _snap_axis(tz, l, self._wall_edges(grid, "z")
                       + self._neighbour_edges("z", x, w), self.thr)
        if y is None:
            y = self._item_rest(x, z, w, l, self._item_floor(x, z, w, l))
        pos = (x, int(y), z)
        return pos, self.item_warnings(pos, (w, h, l))


def snap_position(box: tuple, target: tuple, grids: list[dict],
                  placed: list[tuple], *, union: bool = False,
                  flush_threshold: float = FLUSH_THRESHOLD):
    """One-shot form of PlacementContext(...).snap(box, target)."""
    return PlacementContext(grids, placed, union=union,
                            flush_threshold=flush_threshold).snap(box, target)


def move_box(boxes: list[tuple], index: int, new_pos: tuple[int, int, int],
             new_dims: tuple[int, int, int]) -> list[tuple]:
    """Return a copy of *boxes* with box *index* moved/rotated."""
    out = list(boxes)
    size = out[index][6]
    out[index] = (new_pos[0], new_pos[1], new_pos[2],
                  new_dims[0], new_dims[1], new_dims[2], size)
    return out
