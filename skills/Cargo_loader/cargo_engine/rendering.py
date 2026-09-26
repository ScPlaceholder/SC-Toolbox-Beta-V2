"""
Rendering utilities — isometric projection and painter's algorithm.

Pure functions for coordinate transforms and draw-order computation.
No dependency on any GUI framework (Tkinter, Canvas, etc.).

Supports 4 camera rotations (0°, 90°, 180°, 270° clockwise).
"""

from cargo_engine.schema import SHADING


# ── Isometric projection ─────────────────────────────────────────────────
#
#  Camera sits at (+∞ X, +∞ Y, +∞ Z) looking toward origin.
#  Visible faces per box: TOP (y=max), RIGHT (x=max), LEFT/FRONT (z=max).
#
#  Screen axes:
#    +X world  →  (+cell,  +cell*0.5)  screen   (right and down)
#    +Z world  →  (-cell,  +cell*0.5)  screen   (left  and down)
#    +Y world  →  (0,      -cell    )  screen   (straight up)


def _rotate_coords(wx: float, wz: float, rotation: int,
                   total_gw: float, total_gl: float) -> tuple[float, float]:
    """Rotate world XZ coordinates for the given camera rotation.

    rotation 0: default (camera at +X, +Z)
    rotation 1: 90° CW  (camera at +Z, -X)
    rotation 2: 180°    (camera at -X, -Z)
    rotation 3: 270° CW (camera at -Z, +X)

    total_gw / total_gl are the grid dimensions used for re-centering
    after rotation so the scene stays at positive coordinates.
    """
    if rotation == 1:
        wx, wz = wz, total_gw - wx
    elif rotation == 2:
        wx, wz = total_gw - wx, total_gl - wz
    elif rotation == 3:
        wx, wz = total_gl - wz, wx
    return wx, wz


def iso_project(wx: float, wy: float, wz: float,
                cell: float, ox: float, oy: float,
                rotation: int = 0,
                total_gw: float = 0.0, total_gl: float = 0.0) -> tuple[int, int]:
    """World (X, Y, Z) → integer screen (sx, sy).

    If rotation != 0, world coordinates are rotated before projection.
    total_gw and total_gl are the unrotated grid extents, needed for
    proper re-centering after rotation.
    """
    if rotation:
        wx, wz = _rotate_coords(wx, wz, rotation, total_gw, total_gl)
    sx = ox + (wx - wz) * cell
    sy = oy + (wx + wz) * cell * 0.5 - wy * cell
    return int(sx), int(sy)


def iso_unproject(sx: float, sy: float, wy: float,
                  cell: float, ox: float, oy: float,
                  rotation: int = 0,
                  total_gw: float = 0.0, total_gl: float = 0.0) -> tuple[float, float]:
    """Inverse of iso_project on the horizontal plane at height *wy*.

    Screen (sx, sy) -> continuous world (wx, wz). Used by drag-and-drop to
    turn the mouse position into a floor position.
    """
    a = (sx - ox) / cell                       # rx - rz
    b = (sy - oy + wy * cell) / (cell * 0.5)   # rx + rz
    rx, rz = (a + b) / 2.0, (b - a) / 2.0
    if rotation == 1:
        return total_gw - rz, rx
    if rotation == 2:
        return total_gw - rx, total_gl - rz
    if rotation == 3:
        return rz, total_gl - rx
    return rx, rz


def _effective_dims(gw: float, gl: float, rotation: int) -> tuple[float, float]:
    """Return (effective_gw, effective_gl) after rotation.

    For 90°/270° the width and length swap because the camera
    is looking from a perpendicular direction.
    """
    if rotation in (1, 3):
        return gl, gw
    return gw, gl


def compute_scene_extents(gw: float, gl: float, max_h: float, cell: float,
                          rotation: int = 0):
    """Compute the screen-space bounding box for a scene.

    Returns (scene_left, scene_right, scene_top, scene_bottom).
    """
    ew, el = _effective_dims(gw, gl, rotation)
    scene_left = -el * cell
    scene_right = ew * cell
    scene_top = -max_h * cell
    scene_bottom = (ew + el) * 0.5 * cell
    return scene_left, scene_right, scene_top, scene_bottom


def auto_fit_cell(gw: float, gl: float, max_h: float,
                  canvas_w: int, canvas_h: int, pad: int = 48,
                  min_cell: float = 8.0, max_cell: float = 42.0,
                  rotation: int = 0) -> float:
    """Compute the cell size that fits the scene in the canvas."""
    ew, el = _effective_dims(gw, gl, rotation)
    span_w = ew + el
    span_h = max_h + (ew + el) * 0.5
    return max(min_cell, min(
        (canvas_w - pad * 2) / max(span_w, 1),
        (canvas_h - pad * 2) / max(span_h, 1),
        max_cell,
    ))


def center_origin(gw: float, gl: float, max_h: float,
                   cell: float, canvas_w: int, canvas_h: int,
                   pad: int = 48, rotation: int = 0) -> tuple[float, float]:
    """Compute (ox, oy) that centres the scene in the canvas."""
    sl, sr, st, sb = compute_scene_extents(gw, gl, max_h, cell, rotation)
    scene_pw = sr - sl
    scene_ph = sb - st
    ox = (canvas_w - scene_pw) / 2.0 - sl
    oy = (canvas_h - scene_ph) / 2.0 - st + pad * 0.25
    return ox, oy


# ── Topological sort (painter's algorithm) ────────────────────────────────
#
# Replaces the naive sort-by-(x+z, y, x) approach used in the old Python
# renderer. Uses the same Kahn's algorithm as the JS editor for correctness.


def _range_overlap(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def topological_sort_boxes(
    boxes: list[tuple],
    rotation: int = 0,
    total_gw: float = 0.0,
    total_gl: float = 0.0,
) -> list[tuple]:
    """Sort boxes for correct isometric painter's algorithm rendering.

    Each box is a tuple: (wx, wy, wz, dw, dh, dl, size)

    Uses topological sort with occlusion detection. Only pairs whose iso
    outlines actually overlap on screen are ordered (exact test on the three
    hexagon axes x-z, x-y, z-y). For such a pair, A is behind B if a plane
    separates them with A on the far side: A.x1 <= B.x0, A.z1 <= B.z0 or
    A.y1 <= B.y0 (the camera looks from +X, +Z, above).

    The old rule also demanded overlap in the other two axes, so diagonal
    neighbours (behind on both x and z) got no constraint at all and were
    painted in list order: J, 2026-09-26, "some of the visual stacking is
    wrong". Pairs that genuinely intersect (items may overlap by design) and
    any cycle fall back to depth order (centre x + y + z), not list order.

    When rotation != 0, the world coordinates are rotated before
    computing the behind-relationship so that the painter's algorithm
    matches the rotated camera direction.
    """
    n = len(boxes)
    if n <= 1:
        return list(boxes)

    # Precompute bounds in *rotated* space: (rx, ry, rz, rx+rdw, ry+dh, rz+rdl)
    bounds = []
    for b in boxes:
        wx, wy, wz, dw, dh, dl = b[0], b[1], b[2], b[3], b[4], b[5]

        if rotation:
            # Rotate both the origin and the far corner, then derive new dims
            rx0, rz0 = _rotate_coords(wx, wz, rotation, total_gw, total_gl)
            rx1, rz1 = _rotate_coords(wx + dw, wz + dl, rotation, total_gw, total_gl)
            # After rotation the min/max may swap
            rx_min = min(rx0, rx1)
            rx_max = max(rx0, rx1)
            rz_min = min(rz0, rz1)
            rz_max = max(rz0, rz1)
            bounds.append((rx_min, wy, rz_min, rx_max, wy + dh, rz_max))
        else:
            bounds.append((wx, wy, wz, wx + dw, wy + dh, wz + dl))

    # Build adjacency: adj[i] contains indices j where box i must be drawn before box j
    in_deg = [0] * n
    adj: list[list[int]] = [[] for _ in range(n)]

    def _hex(bd):
        # Screen outline of the box on the three hexagon axes (exact separating-axis test).
        x0, y0, z0, x1, y1, z1 = bd
        return ((x0 - z1, x1 - z0), (x0 - y1, x1 - y0), (z0 - y1, z1 - y0))

    hexes = [_hex(bd) for bd in bounds]

    for i in range(n):
        ax0, ay0, az0, ax1, ay1, az1 = bounds[i]
        for j in range(i + 1, n):
            if not all(_range_overlap(p0, p1, q0, q1)
                       for (p0, p1), (q0, q1) in zip(hexes[i], hexes[j])):
                continue                     # outlines don't overlap: order is irrelevant
            bx0, by0, bz0, bx1, by1, bz1 = bounds[j]

            # A behind B (A drawn first)?
            a_before_b = ax1 <= bx0 or az1 <= bz0 or ay1 <= by0
            # B behind A (B drawn first)?
            b_before_a = bx1 <= ax0 or bz1 <= az0 or by1 <= ay0

            if a_before_b and not b_before_a:
                adj[i].append(j)
                in_deg[j] += 1
            elif b_before_a and not a_before_b:
                adj[j].append(i)
                in_deg[i] += 1

    # Kahn's algorithm; among ready boxes the farthest (smallest centre depth)
    # goes first, so unconstrained pairs - intersecting items - still look right.
    import heapq

    def depth(i):
        x0, y0, z0, x1, y1, z1 = bounds[i]
        return (x0 + x1 + y0 + y1 + z0 + z1) / 2.0

    heap = [(depth(i), i) for i in range(n) if in_deg[i] == 0]
    heapq.heapify(heap)
    result, done = [], [False] * n
    while heap:
        _, u = heapq.heappop(heap)
        result.append(boxes[u])
        done[u] = True
        for v in adj[u]:
            in_deg[v] -= 1
            if in_deg[v] == 0:
                heapq.heappush(heap, (depth(v), v))

    # Cycle fallback: the rest by depth, not list order
    if len(result) < n:
        result.extend(boxes[i] for i in sorted((i for i in range(n) if not done[i]), key=depth))

    return result


# ── Color helpers ─────────────────────────────────────────────────────────

def hex_to_rgb(h: str) -> tuple[int, int, int]:
    """Convert '#rrggbb' to (r, g, b)."""
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)


def rgb_to_hex(r: int, g: int, b: int) -> str:
    """Convert (r, g, b) to '#rrggbb'."""
    return (
        f"#{max(0, min(255, r)):02x}"
        f"{max(0, min(255, g)):02x}"
        f"{max(0, min(255, b)):02x}"
    )


def shade(hex_col: str, factor: float) -> str:
    """Apply brightness factor to a hex color."""
    r, g, b = hex_to_rgb(hex_col)
    return rgb_to_hex(int(r * factor), int(g * factor), int(b * factor))


def label_color(hex_col: str) -> str:
    """White label on dark base, dark label on light base."""
    r, g, b = hex_to_rgb(hex_col)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if lum > 140 else "#ffffff"
