"""Painter's order for the iso view (J, 2026-09-26: "some of the visual stacking is wrong").

For every pair of boxes whose iso outlines overlap on screen and whose volumes do not intersect, the box
on the far side of a separating plane must be painted first. The camera looks from +X, +Z and above, so
"far" means smaller x, smaller z, or lower y. Boxes that genuinely intersect (items may overlap by design)
have no correct order and are skipped.
"""
import itertools
import os
import random
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")))

from cargo_engine.rendering import topological_sort_boxes, _rotate_coords  # noqa: E402


def _bounds(b):
    x, y, z, w, h, l = b[:6]
    return x, y, z, x + w, y + h, z + l


def _screen_ranges(b):
    # iso_project: sx ~ x - z, sy ~ (x + z)/2 - y. A box's outline is the hull of its corners.
    x0, y0, z0, x1, y1, z1 = _bounds(b)
    corners = [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]
    u = [x - z for x, y, z in corners]
    v = [(x + z) / 2 - y for x, y, z in corners]
    w = [x - y for x, y, z in corners]       # third hexagon axis
    t = [z - y for x, y, z in corners]
    return (min(u), max(u)), (min(v), max(v)), (min(w), max(w)), (min(t), max(t))


def _screen_overlap(a, b):
    return all(p0 < q1 - 1e-9 and q0 < p1 - 1e-9
               for (p0, p1), (q0, q1) in zip(_screen_ranges(a), _screen_ranges(b)))


def _intersect(a, b):
    a, b = _bounds(a), _bounds(b)
    return all(a[i] < b[i + 3] and b[i] < a[i + 3] for i in range(3))


def _behind(a, b):
    """True if a is on the far side of some separating plane from b."""
    ax0, ay0, az0, ax1, ay1, az1 = _bounds(a)
    bx0, by0, bz0, bx1, by1, bz1 = _bounds(b)
    return ax1 <= bx0 or az1 <= bz0 or ay1 <= by0


def violations(order):
    pos = {id(b): i for i, b in enumerate(order)}
    bad = []
    for a, b in itertools.combinations(order, 2):
        if _intersect(a, b) or not _screen_overlap(a, b):
            continue
        ab, ba = _behind(a, b), _behind(b, a)
        if ab == ba:
            continue                       # no strict relation (shouldn't happen for disjoint overlaps)
        back, front = (a, b) if ab else (b, a)
        if pos[id(back)] > pos[id(front)]:
            bad.append((back, front))
    return bad


def test_diagonal_neighbours_order():
    # Behind on BOTH x and z: overlaps on screen, but overlaps in neither other axis.
    back = (0, 0, 0, 2, 2, 2, 8)
    front = (2, 0, 2, 2, 2, 2, 8)
    for order in ([back, front], [front, back]):
        got = topological_sort_boxes(list(order))
        assert not violations(got), got


def test_screenshot_scene_has_no_order_errors():
    # The containers from J's Items-tab screenshot plus item boxes around them.
    scene = [(0, 0, 0, 2, 2, 8, 32), (0, 0, 8, 2, 2, 4, 16), (2, 0, 0, 2, 2, 2, 8), (2, 2, 0, 2, 2, 2, 8),
             (4, 0, 0, 2, 2, 2, "pod"), (6, 0, 0, 2, 2, 2, "pod"), (8, 0, 0, 2, 2, 4, "pod"),
             (4, 0, 4, 2, 2, 6, "bomb"), (7, 0, 5, 1, 1, 3, "msl"), (8, 0, 5, 1, 1, 3, "msl"),
             (2, 0, 2, 1, 1, 1, "clr"), (3, 0, 2, 1, 1, 1, "clr"), (2, 4, 0, 2, 1, 1, "shd")]
    for seed in range(20):
        s = list(scene)
        random.Random(seed).shuffle(s)
        got = topological_sort_boxes(s)
        assert not violations(got), (seed, violations(got)[:3])


def test_random_disjoint_stacks_all_rotations():
    rng = random.Random(7)
    checked = cyclic = 0
    for trial in range(60):
        occ, boxes = set(), []
        for _ in range(14):
            w, h, l = rng.choice([(1, 1, 1), (2, 2, 2), (2, 2, 4), (1, 1, 3), (2, 1, 2)])
            x, z = rng.randrange(0, 10), rng.randrange(0, 10)
            y = rng.randrange(0, 3)
            cells = {(x + i, y + j, z + k) for i in range(w) for j in range(h) for k in range(l)}
            if cells & occ:
                continue
            occ |= cells
            boxes.append((x, y, z, w, h, l, len(boxes)))
        for rot in range(4):
            got = topological_sort_boxes(list(boxes), rotation=rot, total_gw=12, total_gl=12)
            # judge in the rotated (camera) frame, where "far" is smaller x / z
            rot_of = {id(b): _rotated(b, rot, 12, 12) for b in got}
            framed = [rot_of[id(b)] for b in got]
            if _has_cycle(framed):
                cyclic += 1                # a true occlusion cycle: no painter order exists
                continue
            checked += 1
            bad = violations(framed)
            assert not bad, (trial, rot, bad[:2])
    assert checked > 150 and cyclic < checked / 10, (checked, cyclic)


def _has_cycle(boxes):
    """A genuine cyclic overlap (A hides B hides C hides A): no painter's order can be right."""
    edges = {i: [] for i in range(len(boxes))}
    for i, j in itertools.permutations(range(len(boxes)), 2):
        a, b = boxes[i], boxes[j]
        if not _intersect(a, b) and _screen_overlap(a, b) and _behind(a, b) and not _behind(b, a):
            edges[i].append(j)
    state = {}

    def visit(u):
        state[u] = 1
        for v in edges[u]:
            if state.get(v) == 1 or (v not in state and visit(v)):
                return True
        state[u] = 2
        return False
    return any(u not in state and visit(u) for u in edges)


def _rotated(b, rot, gw, gl):
    x, y, z, w, h, l = b[:6]
    ax, az = _rotate_coords(x, z, rot, gw, gl)
    bx, bz = _rotate_coords(x + w, z + l, rot, gw, gl)
    return (min(ax, bx), y, min(az, bz), abs(bx - ax), h, abs(bz - az), b[6])
