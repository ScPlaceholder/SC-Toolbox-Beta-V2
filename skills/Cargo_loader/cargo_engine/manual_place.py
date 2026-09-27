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

Items (snap_item) keep rule 1 and rule 2, replace rule 3 with a bound onto the
UNION of the grid floors (_bound_item — an item may lie across a seam between
two grids, which a container may not), and turn rules 4-7 into warnings. The
comment above _bound_item is the argument for that split.
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


def _clamp_span(v: int, size: int, lo: int, span: int) -> int:
    """Keep the half-open run [v, v + size) inside [lo, lo + span).

    When size > span the run cannot be contained at all; it is pinned to *lo*
    so the overflow hangs off the FAR face. The alternative, min(lo + span -
    size, ...), is below lo and would push an oversized item off the NEAR face
    instead — i.e. backwards out of the bay, away from the grid the player
    aimed at. Either way the overflowing columns still report a warning, so
    this choice only decides which end the excess sticks out of.
    """
    return int(min(max(v, lo), max(lo, lo + span - size)))


def _whole_cells(grid: dict, axis: str, span: str) -> tuple[int, int]:
    """One axis of a grid as WHOLE cells: (first cell, how many).

    ⚠ ceil() on both ends, not int(origin) and the raw span, because a grid
      origin is not always a whole cell — 8 of the 1,069 placements in the
      hand-made layouts are on half cells (Idris_M/Idris_P, x = 29.5 and
      31.5). Items only ever sit on whole cells, so the cells a half-cell grid
      actually covers are ceil(o) .. ceil(o + span) - 1: for 29.5 + 2 that is
      30 and 31, and NOT 29, which int(29.5) would have offered as the first
      legal position on a grid that does not reach it.
    """
    o = _g(grid, axis)
    lo = math.ceil(o)
    return lo, math.ceil(o + grid[span]) - lo


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
        self._floor_cells: set[tuple[int, int]] | None = None
        self._anchor_cache: dict[tuple[int, int], list[tuple[int, int]]] = {}
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

    # ⛔ 2026-09-26 (J: "make sure the ship items stay within the maximum
    #   confines of the optimal layout"). snap_item used to return the snapped
    #   target unchanged, so an item followed the mouse anywhere on the floor
    #   plane: a click off the bay landed one at (20, 0, 20) with the grid at
    #   x 0..6 / z 0..8, and the pointer is not bounded, so neither was the
    #   item. The container path (snap) has always clamped — `x = min(max(x,
    #   gx), gx + grid["w"] - w)` — and the item path simply never grew the
    #   same line. Worst case was rotation: a 1x1x4 gun fits a 6-wide grid at
    #   x=3 in every orientation EXCEPT yawed, where its footprint becomes
    #   4x1 and runs to x=7, one cell outside. That is invisible to any
    #   fixture written unrotated.
    # ★ THE VOID IS A WALL AND THE FIT IS STILL A WARNING, which is not a
    #   compromise — the two are different claims. J, same day: items are
    #   "sized ROUGHLY", "if they don't match their cargo bay that's user
    #   error not engine error", and impossible stacks are realistic. So
    #   whether an item is too tall for the bay, overlaps its neighbour, or
    #   overhangs its support stays amber and places: those are judgements
    #   about the ARRANGEMENT, and the player is the quartermaster. Whether
    #   the item is anchored to the ship at all is not a judgement, and that
    #   is the only thing made impossible here. Read the precise claim off
    #   _bound_item's four cases, not off this sentence: an item CAN still
    #   finish with columns off the grid, but only from a deliberate aim into
    #   a bay too small for it, never from the mouse drifting into empty
    #   space, and always flagged.
    # ★★ ONLY X AND Z ARE BOUNDED, and the asymmetry is the argument, not an
    #   omission. X/Z come from the mouse and are unbounded — the defect. Y
    #   comes from _item_rest (the floor, or the top of what is underneath) or
    #   from the box the player clicked, so it is already bounded by the stack
    #   below it, and containers refuse to breach the ceiling (R_TOO_TALL).
    #   An item poking out of the top is therefore always a bounded overshoot
    #   of a real stack, and clamping it would silently drop items INTO the
    #   stack they were dropped onto. W_TOO_TALL keeps reporting it.
    # ⛔⛔ THE BOUND IS THE UNION OF THE GRID FLOORS, NOT ONE GRID, and the
    #   first version of this fix got that wrong in a way every test I had
    #   written still passed. Copying snap()'s per-grid clamp — `x =
    #   min(max(x, gx), gx + grid["w"] - w)` — looks like the obvious move and
    #   is correct for CONTAINERS, because a container must live in a single
    #   grid (R_TOO_BIG_FOR_GRID). An ITEM must not: item_warnings checks each
    #   footprint COLUMN against every grid, so an item lying across the seam
    #   between two abutting grids is clean by design, and per-grid clamping
    #   dragged it off the seam into one side. Measured over all 144 ships and
    #   all 33 hand-made layouts: of 68,935 already-clean item placements,
    #   per-grid clamping MOVED 4,056 — and on the Aurora it moved them and
    #   then flagged them W_OUTSIDE, so the fix for "items escape the grid"
    #   would have put clean items outside it. Every one of the 4,056 spanned
    #   more than one grid.
    # ★★★ So the bound and the warning now ask ONE question — _footprint_
    #   covered and item_warnings' W_OUTSIDE test are the same predicate over
    #   the same cells — and that is the property to keep. Two checks derived
    #   from one rule cannot drift into a bound that forbids what the warning
    #   permits, which is precisely what the per-grid version did.
    #   ⚠ What the shared predicate does NOT buy is the absence of W_OUTSIDE.
    #     After _bound_item an item can still carry it, from case 2: a
    #     deliberate aim into a bay too small for it. The honest statement is
    #     that W_OUTSIDE now means "the player put it there", never "the
    #     pointer wandered".
    # ⚠ Saved plans are deliberately NOT bounded on load (_items_from_payload
    #   keeps the stored pos verbatim). Moving someone's saved work on open,
    #   without them touching anything, is worse than showing it amber — and
    #   item_warnings flags exactly those items, which is what the amber tint
    #   is for.

    def _floor(self) -> set[tuple[int, int]]:
        """Every WHOLE (x, z) cell a grid covers. Built once per context.

        ⛔ Built through _whole_cells, so it agrees with item_warnings' own
          `gx <= cx < gx + w` on a half-cell grid. The first version of this
          added dx to a .5 origin and produced a set of .5 cells that no
          integer item position could ever be a member of; it reported 26
          already-clean placements on the two Idris layouts as off the grids
          and moved them. A bound that disagrees with the warning it is
          supposed to enforce is worse than no bound, because it moves things
          and then flags them.
        """
        if self._floor_cells is None:
            cells: set[tuple[int, int]] = set()
            for g in self.grids:
                x0, nx = _whole_cells(g, "x", "w")
                z0, nz = _whole_cells(g, "z", "l")
                for cx in range(x0, x0 + nx):
                    for cz in range(z0, z0 + nz):
                        cells.add((cx, cz))
            self._floor_cells = cells
        return self._floor_cells

    def _footprint_covered(self, x: int, z: int, w: int, l: int) -> bool:
        cells = self._floor()
        return all((x + dx, z + dz) in cells
                   for dx in range(w) for dz in range(l))

    def _anchors(self, w: int, l: int) -> list[tuple[int, int]]:
        """Every min corner where a w x l footprint is fully over the grids.

        Cached per footprint, because a drag asks on every mouse move and the
        two orientations of one item are two footprints. Candidates are the
        floor cells themselves — a covered footprint's own min corner is
        always one — so this costs O(cells x w x l) once, not a scan of the
        bounding box, which for a ship with two distant bays is mostly void.
        """
        key = (w, l)
        got = self._anchor_cache.get(key)
        if got is None:
            got = sorted(c for c in self._floor()
                         if self._footprint_covered(c[0], c[1], w, l))
            self._anchor_cache[key] = got
        return got

    def _nearest_grid(self, cx: float, cz: float) -> dict | None:
        """The grid whose floor rectangle is nearest (cx, cz); ties by list
        order, so the answer never depends on dict iteration.

        Returns None only when there are no grids at all. That is the one case
        the caller must not bound in: an absent grid list is missing data, not
        a zero-sized bay, and treating it as one would collapse every item
        onto the origin and call that "in bounds".
        """
        best = None
        best_key = None
        for i, g in enumerate(self.grids):
            gx, gz = _g(g, "x"), _g(g, "z")
            dx = max(gx - cx, 0.0, cx - (gx + g["w"]))
            dz = max(gz - cz, 0.0, cz - (gz + g["l"]))
            key = (dx * dx + dz * dz, i)
            if best_key is None or key < best_key:
                best, best_key = g, key
        return best

    def _pin_into(self, grid: dict, x: int, z: int,
                  w: int, l: int) -> tuple[int, int]:
        """Clamp a footprint into one grid's whole cells, min corner first."""
        x0, nx = _whole_cells(grid, "x", "w")
        z0, nz = _whole_cells(grid, "z", "l")
        return _clamp_span(x, w, x0, nx), _clamp_span(z, l, z0, nz)

    def _bound_item(self, x: int, z: int, w: int, l: int) -> tuple[int, int]:
        """Pull a snapped item footprint back onto the grids.

        Four cases, in order, and the ORDER is the whole design:

        1. Already fully over the grids -> untouched. This must cost nothing,
           so it is a w x l set membership test and no search, and it is what
           leaves the 4,056 seam placements exactly where they were.
        2. The player pointed AT a bay (the footprint's centre, or failing that
           its min corner, stands on one) -> clamped into that bay and nowhere
           else. If the item fits, the clamp pulls it fully in; if it is too
           big for that bay it is pinned there, overflowing, and flagged.
        3. The aim is off the grids entirely -> the nearest min corner that can
           hold the footprint whole, by squared distance then (x, z) so the
           answer is deterministic.
        4. Off the grids and nothing anywhere can hold it -> pinned to the
           nearest grid. Still on the ship; item_warnings reports the overflow.

        ⛔ CASE 2 EXISTS BECAUSE CASE 3 ALONE TELEPORTS. Measured over all 144
          ships and 33 layouts, aiming at every cell of every grid with nine
          item footprints: 57,721 aims were not already covered, and while 65%
          of them moved two cells or less, the tail ran to 86 — an 8x2x2 item
          aimed at the nose of the Idris-P reappearing at the tail, because the
          only bay that can hold it whole is there. A click is a statement
          about WHERE; answering it by moving the item the length of the ship
          is not a bound, it is a different placement. With case 2 in front,
          that aim pins in the bay the player chose and the tail is gone.
        ★ So the item can still end up with columns off the grid — but only
          ever by the player's own aim, never by the mouse wandering into the
          void, and always amber. That is the line J drew: the region is the
          engine's business, the fit is the quartermaster's. A rule that also
          refused case 2 would be turning his warning into a wall.
        ⚠ Case 2 tries the centre first and the min corner second because one
          test is not enough for both ends of the size range. A 1x1x1's centre
          is always inside the bay its corner is in; a long item's centre can
          fall past the far wall while it is plainly being aimed into the bay
          (a 1x1x4 gun at z=0 of a 2-deep bay has its centre at z=2, outside).
          Corner-only fails the mirror case. Neither alone is the aim.
        """
        if not self.grids:
            return x, z
        if self._footprint_covered(x, z, w, l):
            return x, z
        aimed = (self._grid_under(x + w / 2.0, z + l / 2.0)
                 or self._grid_under(x + 0.5, z + 0.5))
        if aimed is not None:
            return self._pin_into(aimed, x, z, w, l)
        anchors = self._anchors(w, l)
        if anchors:
            return min(anchors, key=lambda c: ((c[0] - x) ** 2 + (c[1] - z) ** 2,
                                               c[0], c[1]))
        return self._pin_into(self._nearest_grid(x + w / 2.0, z + l / 2.0),
                              x, z, w, l)

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
        cx, cz = tx + w / 2.0, tz + l / 2.0
        grid = self._grid_under(cx, cz)
        z0 = _round(tz)
        x = _snap_axis(tx, w, self._wall_edges(grid, "x")
                       + self._neighbour_edges("x", z0, l), self.thr)
        z = _snap_axis(tz, l, self._wall_edges(grid, "z")
                       + self._neighbour_edges("z", x, w), self.thr)
        # The ORIENTED footprint is bounded onto the grids — w and l are
        # post-rotation, because the caller yaws the box before asking, and a
        # bound applied to the nominal size would pass every unrotated fixture
        # while letting a yawed item hang off the edge. The magnet above is
        # deliberately left reading every grid's walls: it may pull the item
        # onto a neighbouring grid's face, which is legal for an item, and
        # _bound_item only intervenes when the footprint leaves the grids.
        x, z = self._bound_item(x, z, w, l)
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
