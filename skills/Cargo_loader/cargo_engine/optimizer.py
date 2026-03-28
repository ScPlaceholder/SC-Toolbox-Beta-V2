"""
Greedy optimizer and slot assignment logic.

Pure functions, no UI dependencies.
"""

from cargo_engine.schema import CONTAINER_SIZES
from cargo_engine.placement import max_containers_in_slot


def greedy_optimize_3d(slots: list[dict]) -> dict[int, int]:
    """Greedy optimiser: fill slots with largest containers first."""
    counts = {s: 0 for s in CONTAINER_SIZES}
    for slot in slots:
        w, h, l = slot["w"], slot["h"], slot["l"]
        max_size = slot.get("maxSize")
        min_size = slot.get("minSize") or 1
        remaining = slot["capacity"]
        allowed = sorted(
            [s for s in CONTAINER_SIZES
             if (max_size is None or s <= max_size)
             and s >= min_size
             and max_containers_in_slot(s, w, h, l) > 0],
            reverse=True,
        )
        for size in allowed:
            if remaining <= 0:
                break
            n = min(max_containers_in_slot(size, w, h, l), remaining // size)
            counts[size] += n
            remaining -= n * size
        if remaining > 0 and 1 >= min_size and max_containers_in_slot(1, w, h, l) > 0:
            placed_scu = slot["capacity"] - remaining
            max_1scu = max_containers_in_slot(1, w, h, l)
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
        max_size = slot.get("maxSize")
        min_size = slot.get("minSize") or 1
        slot_remain = slot["capacity"]
        slot_asgn: dict[int, int] = {}
        for size in sorted(CONTAINER_SIZES, reverse=True):
            if remaining.get(size, 0) <= 0 or slot_remain <= 0:
                continue
            if max_size is not None and size > max_size:
                continue
            if size < min_size:
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
