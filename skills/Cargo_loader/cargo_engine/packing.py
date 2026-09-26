"""
3D bin-packing — places containers into slot volumes.

Pure logic, no UI. Deterministic results for identical inputs.
"""

import itertools

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


def place_containers_3d(
    slot: dict, assignment: dict[int, int]
) -> list[tuple[int, int, int, int, int, int, int]]:
    """3-D bin-pack containers into slot W x H x L.

    Returns list of (lx, ly, lz, cw, ch, cl, size)
    — positions are relative to the slot's own origin.

    Each container placement tries all valid rotations so that containers can
    fill irregular remaining space (e.g. after a large container takes the front
    portion of a slot, smaller containers rotate to fit the back portion).
    """
    sw, sh, sl = slot["w"], slot["h"], slot["l"]
    occupied = [[[False] * sl for _ in range(sh)] for _ in range(sw)]
    result: list[tuple] = []

    for size in sorted(assignment.keys(), reverse=True):
        rots = _valid_rotations(
            CONTAINER_DIMS[size], sw, sh, sl,
            max_ch=CONTAINER_MAX_STACK_HEIGHT.get(size),
        )
        if not rots:
            continue

        for _ in range(assignment[size]):
            placed = False
            for cw, ch, cl in rots:
                if placed:
                    break
                for ly in range(sh - ch + 1):
                    if placed:
                        break
                    for lz in range(sl - cl + 1):
                        if placed:
                            break
                        for lx in range(sw - cw + 1):
                            if all(
                                not occupied[lx + dx][ly + dy][lz + dz]
                                for dx in range(cw)
                                for dy in range(ch)
                                for dz in range(cl)
                            ):
                                for dx in range(cw):
                                    for dy in range(ch):
                                        for dz in range(cl):
                                            occupied[lx + dx][ly + dy][lz + dz] = True
                                result.append((lx, ly, lz, cw, ch, cl, size))
                                placed = True
                                break
    return result


def build_slots(ship: dict) -> tuple[list[dict], tuple]:
    """Build slot list from ship group/grid data. Returns (slots, bounds)."""
    slots: list[dict] = []
    # minSize/maxSize are honored only for scunpacked-derived ship data, where
    # they were converted from the game files' per-grid MinSize/MaxSize extents
    # (see datamine/rebuild_grids.js). The old sc-cargo.space cache carries
    # unreliable tags (e.g. Vulture's 2x2x3 bay tagged maxSize=1) and keeps
    # the historical unconstrained behavior.
    honor_sizes = (ship.get("provenance") or {}).get("source") == "scunpacked-data"
    for group in ship.get("groups", []):
        gx = group.get("x", 0)
        gz = group.get("z", 0)
        for grid in group.get("grids", []):
            x = gx + grid.get("x", 0)
            z = gz + grid.get("z", 0)
            w = max(1, grid.get("width", 1))
            h = max(1, grid.get("height", 1))
            l = max(1, grid.get("length", 1))
            slots.append({
                "x": x, "y0": 0, "z": z,
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
