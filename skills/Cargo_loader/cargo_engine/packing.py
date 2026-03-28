"""
3D bin-packing — places containers into slot volumes.

Pure logic, no UI. Deterministic results for identical inputs.
"""

from cargo_engine.schema import CONTAINER_DIMS, CONTAINER_MAX_STACK_HEIGHT
from cargo_engine.placement import best_rotation


def place_containers_3d(
    slot: dict, assignment: dict[int, int]
) -> list[tuple[int, int, int, int, int, int, int]]:
    """3-D bin-pack containers into slot W x H x L.

    Returns list of (lx, ly, lz, cw, ch, cl, size)
    — positions are relative to the slot's own origin.
    """
    sw, sh, sl = slot["w"], slot["h"], slot["l"]
    occupied = [[[False] * sl for _ in range(sh)] for _ in range(sw)]
    result: list[tuple] = []

    for size in sorted(assignment.keys(), reverse=True):
        rot = best_rotation(
            CONTAINER_DIMS[size], sw, sh, sl,
            max_ch=CONTAINER_MAX_STACK_HEIGHT.get(size),
        )
        if rot is None:
            continue
        cw, ch, cl = rot

        for _ in range(assignment[size]):
            placed = False
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
                "maxSize": grid.get("maxSize"),
                "minSize": grid.get("minSize"),
                "capacity": w * h * l,
            })
    if not slots:
        return [], (0, 0, 1, 1)
    x_min = min(s["x"] for s in slots)
    z_min = min(s["z"] for s in slots)
    x_max = max(s["x"] + s["w"] for s in slots)
    z_max = max(s["z"] + s["l"] for s in slots)
    return slots, (x_min, z_min, x_max, z_max)
