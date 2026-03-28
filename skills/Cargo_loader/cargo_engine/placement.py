"""
Placement logic — rotation selection and capacity calculations.

Pure functions, no state, no UI dependencies.
"""

import itertools

from cargo_engine.schema import CONTAINER_DIMS, CONTAINER_MAX_STACK_HEIGHT


def best_rotation(
    dims: tuple[int, int, int],
    sw: int, sh: int, sl: int,
    max_ch: int | None = None,
) -> tuple[int, int, int] | None:
    """Pick the rotation of *dims* that fits in slot (sw x sh x sl) and
    minimises the Y height (ch), then maximises the X x Z floor area.

    Returns (cw, ch, cl) or None if no orientation fits.
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
    max_ch = CONTAINER_MAX_STACK_HEIGHT.get(size)
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


def packed_to_rotation(size: int, cw: int, ch: int, cl: int) -> int:
    """Determine editor rotation (0 or 90) from packed dims.

    Compares against the base dims from the schema to determine
    if the container was rotated.
    """
    bw, bh, bl = CONTAINER_DIMS[size]
    if cw == bw and cl == bl:
        return 0
    if cw == bl and cl == bw:
        return 90
    return 0  # fallback
