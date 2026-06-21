"""Pseudo-3D camera for the galaxy view.

Orthographic projection with yaw + pitch rotation; the post-rotation depth
axis drives node size, brightness and draw order so the flat plane reads as
3D. No perspective divide (stable, no singularities); perspective can be
layered on later without touching callers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple


@dataclass
class Camera:
    yaw: float = 0.6        # radians, rotation about the vertical (Y) axis
    pitch: float = 0.5      # radians, tilt toward/away from the viewer
    zoom: float = 1.0       # user zoom multiplier (applied to base scale)
    pan_x: float = 0.0      # screen-space pan, pixels
    pan_y: float = 0.0
    fx: float = 0.0         # focus point (3D) kept at screen centre
    fy: float = 0.0
    fz: float = 0.0

    def rotate(self, d_yaw: float, d_pitch: float) -> None:
        self.yaw += d_yaw
        # clamp pitch just short of the poles to avoid flipping
        self.pitch = max(-1.45, min(1.45, self.pitch + d_pitch))

    def zoom_at(self, factor: float, mx: float, my: float,
                cx: float, cy: float, zmin: float, zmax: float) -> float:
        """Multiply zoom by *factor* (clamped to [zmin, zmax]) while keeping the
        world point under (mx, my) fixed on screen — i.e. zoom toward the cursor."""
        z0 = self.zoom or 1.0
        z1 = max(zmin, min(zmax, z0 * factor))
        ratio = z1 / z0
        self.pan_x -= (mx - cx - self.pan_x) * (ratio - 1.0)
        self.pan_y -= (my - cy - self.pan_y) * (ratio - 1.0)
        self.zoom = z1
        return z1

    def project(self, x: float, y: float, z: float,
                cx: float, cy: float, scale: float) -> Tuple[float, float, float]:
        """Project galactic point ``(x, y, z)`` to screen space.

        Returns ``(screen_x, screen_y, depth)``; larger ``depth`` = nearer the
        camera (use it for size / brightness / painter's-order).
        """
        x -= self.fx
        y -= self.fy
        z -= self.fz

        cyaw, syaw = math.cos(self.yaw), math.sin(self.yaw)
        x1 = x * cyaw + z * syaw
        z1 = -x * syaw + z * cyaw
        y1 = y

        cp, sp = math.cos(self.pitch), math.sin(self.pitch)
        y2 = y1 * cp - z1 * sp
        z2 = y1 * sp + z1 * cp
        x2 = x1

        s = scale * self.zoom
        sx = cx + x2 * s + self.pan_x
        sy = cy - y2 * s + self.pan_y     # screen Y grows downward; negate
        return sx, sy, z2
