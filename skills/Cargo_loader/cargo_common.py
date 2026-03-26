"""
Shared constants and algorithms for Cargo Loader tools.

Both cargo_app.py and generate_layout.py import from here to avoid duplication.
"""
import itertools
import json
import logging
import os

log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
DIR = os.path.dirname(os.path.abspath(__file__))

# ── Container constants ───────────────────────────────────────────────────────
CONTAINER_SIZES = [1, 2, 4, 8, 16, 24, 32]

# Container dimensions: (length, width, height) per user spec L x W x H
CONTAINER_DIMS = {
    1: (1, 1, 1), 2: (1, 2, 1), 4: (2, 2, 1),
    8: (2, 2, 2), 16: (2, 4, 2), 24: (2, 6, 2), 32: (2, 8, 2),
}

# 4 SCU boxes are flat crates -- they cannot be rotated to stand on their end.
# Any container listed here is restricted to ch <= the given value.
CONTAINER_MAX_CH: dict[int, int] = {4: 1}

# Editor's BASE_DIMS (w, h, l) -- long axis defaults to Z (l).
# Used to determine rotation value for the editor.
EDITOR_BASE_DIMS = {
    1:  (1, 1, 1),
    2:  (1, 1, 2),
    4:  (2, 1, 2),
    8:  (2, 2, 2),
    16: (2, 2, 4),
    24: (2, 2, 6),
    32: (2, 2, 8),
}


# ── Reference loadouts ───────────────────────────────────────────────────────
_LOADOUTS_FILE = os.path.join(DIR, "reference_loadouts.json")


def load_reference_loadouts(script_dir: str | None = None) -> dict[str, dict[int, int]]:
    """Load reference loadouts from JSON. *script_dir* overrides the default directory."""
    path = os.path.join(script_dir, "reference_loadouts.json") if script_dir else _LOADOUTS_FILE
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
        return {k: {int(sz): cnt for sz, cnt in v.items()} for k, v in raw.items()}
    except Exception as e:
        log.warning("Failed to load reference loadouts from %s: %s", path, e)
        return {}


def find_reference_loadout(
    ship_name: str, loadouts: dict[str, dict[int, int]]
) -> dict[int, int] | None:
    """Return the reference loadout for *ship_name*, or None if not found.

    Matching strategy (case-insensitive):
    1. Exact match after lowercasing.
    2. Longest key that is a substring of the ship name.
    3. Longest ship-name fragment that is a substring of any key.
    """
    needle = ship_name.lower().strip()
    # 1. Exact
    if needle in loadouts:
        return loadouts[needle]
    # 2. Longest key contained in needle
    best_key: str | None = None
    best_len = 0
    for key in loadouts:
        if key in needle and len(key) > best_len:
            best_key = key
            best_len = len(key)
    if best_key:
        return loadouts[best_key]
    # 3. Longest needle fragment contained in any key
    best_key = None
    best_len = 0
    for key in loadouts:
        if needle in key and len(key) > best_len:
            best_key = key
            best_len = len(key)
    if best_key:
        return loadouts[best_key]
    return None


# ── 3-D packing helpers ──────────────────────────────────────────────────────

def best_rotation(
    dims: tuple[int, int, int],
    sw: int, sh: int, sl: int,
    max_ch: int | None = None,
) -> tuple[int, int, int] | None:
    """Pick the rotation of *dims* that fits in slot (sw x sh x sl) and
    minimises the Y height (ch), then maximises the X x Z floor area.
    """
    best: tuple | None = None
    best_ch = 9_999
    best_area = -1
    seen: set[tuple] = set()
    for perm in itertools.permutations(dims):
        if perm in seen:
            continue
        seen.add(perm)
        cw, ch, cl = perm
        if max_ch is not None and ch > max_ch:
            continue
        if cw <= sw and ch <= sh and cl <= sl:
            if ch < best_ch or (ch == best_ch and cw * cl > best_area):
                best_ch = ch
                best_area = cw * cl
                best = perm
    return best


def max_containers_in_slot(size: int, w: int, h: int, l: int) -> int:
    """Max containers of *size* that physically fit in a w x h x l slot."""
    dims = CONTAINER_DIMS[size]
    max_ch = CONTAINER_MAX_CH.get(size)
    result = 0
    seen: set[tuple] = set()
    for perm in itertools.permutations(dims):
        if perm in seen:
            continue
        seen.add(perm)
        cd_x, cd_y, cd_z = perm
        if max_ch is not None and cd_y > max_ch:
            continue
        if cd_x <= w and cd_y <= h and cd_z <= l:
            result = max(result, (w // cd_x) * (h // cd_y) * (l // cd_z))
    return result


def place_containers_3d(
    slot: dict, assignment: dict[int, int]
) -> list[tuple[int, int, int, int, int, int, int]]:
    """3-D bin-pack containers into slot W x H x L.

    Returns list of (lx, ly, lz, cw, ch, cl, size)
    -- positions are relative to the slot's own origin.
    """
    sw, sh, sl = slot["w"], slot["h"], slot["l"]
    occupied = [[[False] * sl for _ in range(sh)] for _ in range(sw)]
    result: list[tuple] = []

    for size in sorted(assignment.keys(), reverse=True):
        rot = best_rotation(CONTAINER_DIMS[size], sw, sh, sl,
                            max_ch=CONTAINER_MAX_CH.get(size))
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


def greedy_optimize_3d(slots: list[dict]) -> dict[int, int]:
    """Greedy optimiser: fill slots with largest containers first."""
    counts = {s: 0 for s in CONTAINER_SIZES}
    for slot in slots:
        w, h, l = slot["w"], slot["h"], slot["l"]
        maxSize = slot.get("maxSize")
        minSize = slot.get("minSize") or 1
        remaining = slot["capacity"]
        allowed = sorted(
            [s for s in CONTAINER_SIZES
             if (maxSize is None or s <= maxSize)
             and s >= minSize
             and max_containers_in_slot(s, w, h, l) > 0],
            reverse=True,
        )
        for size in allowed:
            if remaining <= 0:
                break
            n = min(max_containers_in_slot(size, w, h, l), remaining // size)
            counts[size] += n
            remaining -= n * size
        if remaining > 0 and 1 >= minSize and max_containers_in_slot(1, w, h, l) > 0:
            # Don't blindly add remaining; check how many 1-SCU physically fit
            # after accounting for space used by larger containers in this slot.
            placed_scu = slot["capacity"] - remaining
            max_1scu = max_containers_in_slot(1, w, h, l)
            # Conservatively: total physical 1-SCU capacity minus SCU already placed
            n = min(remaining, max(0, max_1scu - placed_scu))
            if n > 0:
                counts[1] += n
    return counts


def assign_slots_from_counts(
    slots: list[dict], counts: dict[int, int]
) -> list[dict[int, int]]:
    """Assign containers from *counts* to *slots* greedily."""
    remaining = dict(counts)
    result: list[dict] = []
    for slot in slots:
        w, h, l = slot["w"], slot["h"], slot["l"]
        maxSize = slot.get("maxSize")
        minSize = slot.get("minSize") or 1
        slot_remain = slot["capacity"]
        slot_asgn: dict[int, int] = {}
        for size in sorted(CONTAINER_SIZES, reverse=True):
            if remaining.get(size, 0) <= 0 or slot_remain <= 0:
                continue
            if maxSize is not None and size > maxSize:
                continue
            if size < minSize:
                continue
            max_phys = max_containers_in_slot(size, w, h, l)
            if max_phys <= 0:
                continue
            n = min(remaining[size], max_phys, slot_remain // size)
            if n > 0:
                slot_asgn[size] = n
                remaining[size] -= n
                slot_remain -= n * size
        result.append(slot_asgn)
    return result
