"""
3D bin-packing — places containers into slot volumes.

Pure logic, no UI. Deterministic results for identical inputs.
"""

import itertools
import math

from cargo_engine.schema import CONTAINER_DIMS, CONTAINER_MAX_STACK_HEIGHT


def _valid_rotations(
    dims: tuple[int, int, int],
    sw: int, sh: int, sl: int,
    max_ch: int | None,
) -> list[tuple[int, int, int]]:
    """All unique orientations of *dims* that fit in sw×sh×sl, sorted by
    (ascending ch, descending footprint area) so low-profile rotations come first.
    """
    seen: set[tuple] = set()
    result = []
    for perm in itertools.permutations(dims):
        if perm in seen:
            continue
        seen.add(perm)
        cw, ch, cl = perm
        if max_ch is not None and ch > max_ch:
            continue
        if cw <= sw and ch <= sh and cl <= sl:
            result.append(perm)
    result.sort(key=lambda p: (p[1], -(p[0] * p[2])))
    return result


def _fits_at(occupied, lx, ly, lz, cw, ch, cl) -> bool:
    return all(
        not occupied[lx + dx][ly + dy][lz + dz]
        for dx in range(cw)
        for dy in range(ch)
        for dz in range(cl)
    )


def _place_one(occupied, rots, sw, sh, sl) -> tuple | None:
    """First free position for one container, trying *rots* in order (the
    scan order the packer has always used: rotation, then y, z, x). Marks the
    cells and returns (lx, ly, lz, cw, ch, cl), or None if nothing fits."""
    for cw, ch, cl in rots:
        for ly in range(sh - ch + 1):
            for lz in range(sl - cl + 1):
                for lx in range(sw - cw + 1):
                    if _fits_at(occupied, lx, ly, lz, cw, ch, cl):
                        for dx in range(cw):
                            for dy in range(ch):
                                for dz in range(cl):
                                    occupied[lx + dx][ly + dy][lz + dz] = True
                        return (lx, ly, lz, cw, ch, cl)
    return None


def _occupancy(sw: int, sh: int, sl: int, blocked=None) -> list:
    occupied = [[[False] * sl for _ in range(sh)] for _ in range(sw)]
    for (bx, by, bz) in blocked or ():
        if 0 <= bx < sw and 0 <= by < sh and 0 <= bz < sl:
            occupied[bx][by][bz] = True
    return occupied


def place_containers_3d(
    slot: dict, assignment: dict[int, int], blocked=None,
) -> list[tuple[int, int, int, int, int, int, int]]:
    """3-D bin-pack containers into slot W x H x L.

    Returns list of (lx, ly, lz, cw, ch, cl, size)
    — positions are relative to the slot's own origin.

    Each container placement tries all valid rotations so that containers can
    fill irregular remaining space (e.g. after a large container takes the front
    portion of a slot, smaller containers rotate to fit the back portion).

    *blocked* (optional) is a set of slot-local (x, y, z) cells that are
    already taken — by items placed by hand (J, 2026-10-03: "should treat
    objects as objects"). A container never takes a blocked cell; one that
    then has nowhere to go is left out of the result, the same way a
    container that does not fit has always been left out, so callers must
    count the RESULT, not the assignment. With blocked=None the output is
    identical to the packer before this argument existed.
    """
    sw, sh, sl = slot["w"], slot["h"], slot["l"]
    occupied = _occupancy(sw, sh, sl, blocked)
    result: list[tuple] = []

    for size in sorted(assignment.keys(), reverse=True):
        rots = _valid_rotations(
            CONTAINER_DIMS[size], sw, sh, sl,
            max_ch=CONTAINER_MAX_STACK_HEIGHT.get(size),
        )
        if not rots:
            continue

        for _ in range(assignment[size]):
            got = _place_one(occupied, rots, sw, sh, sl)
            if got is not None:
                result.append((*got, size))
    return result


def slot_blocked_cells(x0: float, y0: float, z0: float, slot: dict,
                       boxes) -> set[tuple[int, int, int]]:
    """Slot-local cells of a slot at world origin (x0, y0, z0) that any of
    *boxes* — (x, y, z, w, h, l, ...) in the same world coordinates — overlaps.

    Overlap is tested on the continuous cell cubes, not by rounding, because a
    hand-layout slot can sit on a half cell (Idris x = 29.5) while items sit on
    whole cells; a cell the item covers by half is still not free.
    """
    sw, sh, sl = slot["w"], slot["h"], slot["l"]
    out: set[tuple[int, int, int]] = set()

    def span(o, n, b, bn):
        # local cells c in [0, n) whose cube [o + c, o + c + 1) meets [b, b + bn)
        lo = max(0, math.floor(b - o))
        hi = min(n, math.ceil(b + bn - o))
        return range(lo, hi)

    for b in boxes:
        bx, by, bz, bw, bh, bl = b[0], b[1], b[2], b[3], b[4], b[5]
        for cx in span(x0, sw, bx, bw):
            for cy in span(y0, sh, by, bh):
                for cz in span(z0, sl, bz, bl):
                    out.add((cx, cy, cz))
    return out


def fill_slot_around(slot: dict, blocked=None) -> dict[int, int]:
    """How many of each container ACTUALLY fit in *slot* around *blocked*
    cells, largest first: the per-slot count Optimize uses when the hold
    already holds items. Same size rules as greedy_optimize_3d (maxSize,
    minSize, the slot's capacity), but every container is really placed, so
    the count can never promise space an item is standing in. Costs capacity
    exactly where an item is in the way, which is the honest answer.
    """
    sw, sh, sl = slot["w"], slot["h"], slot["l"]
    max_size = slot.get("maxSize")
    min_size = slot.get("minSize") or 1
    remaining = slot.get("capacity", sw * sh * sl)
    occupied = _occupancy(sw, sh, sl, blocked)
    counts: dict[int, int] = {}
    for size in sorted(CONTAINER_DIMS, reverse=True):
        if (max_size is not None and size > max_size) or size < min_size:
            continue
        rots = _valid_rotations(CONTAINER_DIMS[size], sw, sh, sl,
                                max_ch=CONTAINER_MAX_STACK_HEIGHT.get(size))
        while rots and remaining >= size:
            if _place_one(occupied, rots, sw, sh, sl) is None:
                break
            counts[size] = counts.get(size, 0) + 1
            remaining -= size
    return counts


def build_slots(ship: dict) -> tuple[list[dict], tuple]:
    """Build slot list from ship group/grid data. Returns (slots, bounds)."""
    slots: list[dict] = []
    # minSize/maxSize are honored only for scunpacked-derived ship data, where
    # they were converted from the game files' per-grid MinSize/MaxSize extents
    # (see datamine/rebuild_grids.js). The old sc-cargo.space cache carries
    # unreliable tags (e.g. Vulture's 2x2x3 bay tagged maxSize=1) and keeps
    # the historical unconstrained behavior.
    honor_sizes = (ship.get("provenance") or {}).get("source") in (
        "scunpacked-data", "hand-layout")
    for group in ship.get("groups", []):
        gx = group.get("x", 0)
        gz = group.get("z", 0)
        gy = group.get("y", 0)
        for grid in group.get("grids", []):
            x = gx + grid.get("x", 0)
            z = gz + grid.get("z", 0)
            # A grid's own deck height. Every cache entry has carried a "y" key
            # since the sc-cargo.space days and this function used to hardcode
            # y0 to 0, so an upper-deck grid was silently flattened onto the
            # floor: .cargo_cache.json's Caterpillar has grids at y=2 and they
            # were drawn and packed at y=0. Honoring it is also what lets a
            # hand-made layout (whose volumes reach y=4 on the Starfarer) be
            # expressed in this one format instead of its own.
            y0 = max(0, int(grid.get("y", 0) or 0)) + max(0, int(gy or 0))
            w = max(1, grid.get("width", 1))
            h = max(1, grid.get("height", 1))
            l = max(1, grid.get("length", 1))
            slots.append({
                "x": x, "y0": y0, "z": z,
                "w": w, "h": h, "l": l,
                "maxSize": grid.get("maxSize") if honor_sizes else None,
                "minSize": grid.get("minSize") if honor_sizes else None,
                "capacity": w * h * l,
            })
    if not slots:
        return [], (0, 0, 1, 1)
    x_min = min(s["x"] for s in slots)
    z_min = min(s["z"] for s in slots)
    x_max = max(s["x"] + s["w"] for s in slots)
    z_max = max(s["z"] + s["l"] for s in slots)
    return slots, (x_min, z_min, x_max, z_max)
