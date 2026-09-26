"""Planet globe scene — a flat-coloured sphere you can spin, with surface
locations pinned at their true relative positions.

Orthographic sphere projection: each location's offset from the planet centre is
normalised to a direction, rotated by the view, and projected; back-facing pins
are hidden. Bigger pins with labels that reveal as you zoom in (the "location"
level). Zoom out to drill back to the system. Flat colours only (no textures).
Render-on-demand.
"""
from __future__ import annotations

import math
from typing import List, Optional, Tuple

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import QWidget

from shared.qt.theme import P

from .data import Body, load_bodies, norm_loc
from .labels import draw_label
from .system_view import _body_color, draw_terminal_badge

_DRILL_OUT_ZOOM = 0.62      # zoom out below this -> back to the system
_LABEL_REVEAL_ZOOM = 1.5    # location labels appear past this zoom


class PlanetView(QWidget):
    drillOut = Signal()         # zoom out -> back to system
    terminalClicked = Signal(str, str)   # double-click a surface terminal -> (name, system)
    bodyActivated = Signal(str, str)     # double-click any other surface location -> (name, system)
    loreRequested = Signal(str, str)     # right-click a location / the planet -> (name, system) lore

    def __init__(self, system_code: str, planet_name: str,
                 bodies: Optional[List[Body]] = None, terminal_names=None,
                 parent=None) -> None:
        super().__init__(parent)
        self._system = system_code.upper()
        self._planet_name = planet_name
        self._terminals = set(terminal_names or ())
        if bodies is None:
            bodies = load_bodies().get(self._system, [])
        self._planet = next((b for b in bodies if b.name == planet_name), None)

        # Surface locations = children of this planet (outposts / landing / stations).
        self._locs: List[Body] = []
        self._dirs: List[Tuple[float, float, float]] = []
        if self._planet is not None:
            for b in bodies:
                if b.parent == planet_name and b.kind in ("outpost", "landing", "station"):
                    dx, dy, dz = b.x - self._planet.x, b.y - self._planet.y, b.z - self._planet.z
                    m = math.sqrt(dx * dx + dy * dy + dz * dz) or 1.0
                    self._locs.append(b)
                    self._dirs.append((dx / m, dy / m, dz / m))

        self._color = _body_color(self._planet) if self._planet else QColor(P.fg_dim)
        self._yaw = 0.4
        self._pitch = 0.3
        self._zoom = 1.0
        self._hovered: Optional[Body] = None
        self._last: Optional[QPointF] = None
        self._dragging = False
        self._moved = 0.0

        self.setMinimumSize(360, 280)
        self.setMouseTracking(True)

    def _radius(self, w: int, h: int) -> float:
        return min(w, h) * 0.32 * self._zoom

    def _rot(self, v: Tuple[float, float, float]) -> Tuple[float, float, float]:
        x, y, z = v
        cy, sy = math.cos(self._yaw), math.sin(self._yaw)
        x1 = x * cy + z * sy
        z1 = -x * sy + z * cy
        y1 = y
        cp, sp = math.cos(self._pitch), math.sin(self._pitch)
        y2 = y1 * cp - z1 * sp
        z2 = y1 * sp + z1 * cp
        return (x1, y2, z2)

    # ── painting ──────────────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        cx, cy = w / 2.0, h / 2.0
        R = self._radius(w, h)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_deepest))

        if self._planet is None:
            p.setPen(QColor(P.fg_dim))
            p.drawText(self.rect(), Qt.AlignCenter, "No data for this planet")
            p.end()
            return

        c = QPointF(cx, cy)
        base = QColor(self._color)
        grad = QRadialGradient(QPointF(cx - R * 0.3, cy - R * 0.35), R * 1.35)
        grad.setColorAt(0.0, QColor(base).lighter(135))
        grad.setColorAt(0.65, base)
        grad.setColorAt(1.0, QColor(base).darker(170))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(grad)); p.drawEllipse(c, R, R)
        rim = QPen(QColor(base).lighter(160)); rim.setWidthF(1.6)
        p.setPen(rim); p.setBrush(Qt.NoBrush); p.drawEllipse(c, R, R)

        self._draw_graticule(p, cx, cy, R)
        self._draw_pins(p, cx, cy, R)
        self._draw_hud(p, w, h)
        p.end()

    def _draw_graticule(self, p: QPainter, cx, cy, R) -> None:
        col = QColor(P.energy_cyan); col.setAlpha(42)
        pen = QPen(col); pen.setWidthF(0.9)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        for lat in range(-60, 61, 30):
            self._arc(p, cx, cy, R, lat_deg=lat)
        for lon in range(0, 180, 30):
            self._arc(p, cx, cy, R, lon_deg=lon)

    def _arc(self, p: QPainter, cx, cy, R, lat_deg=None, lon_deg=None) -> None:
        pts: List[QPointF] = []
        if lat_deg is not None:
            lat = math.radians(lat_deg)
            samples = [(math.cos(lat) * math.cos(t), math.sin(lat), math.cos(lat) * math.sin(t))
                       for t in [i / 48.0 * 2 * math.pi for i in range(49)]]
        else:
            lon = math.radians(lon_deg)
            samples = [(math.cos(a) * math.cos(lon), math.sin(a), math.cos(a) * math.sin(lon))
                       for a in [-math.pi / 2 + i / 48.0 * math.pi for i in range(49)]]
        for v in samples:
            x, y, z = self._rot(v)
            if z >= -0.02:
                pts.append(QPointF(cx + x * R, cy - y * R))
            else:
                if len(pts) >= 2:
                    p.drawPolyline(QPolygonF(pts))
                pts = []
        if len(pts) >= 2:
            p.drawPolyline(QPolygonF(pts))

    def _draw_pins(self, p: QPainter, cx, cy, R) -> None:
        pin_r = 4.5 + 1.8 * min(self._zoom, 3.0)
        front = []
        for b, d in zip(self._locs, self._dirs):
            x, y, z = self._rot(d)
            px, py = cx + x * R, cy - y * R
            col = _body_color(b)
            if z >= 0:
                p.setPen(Qt.NoPen); p.setBrush(col)
                p.drawEllipse(QPointF(px, py), pin_r, pin_r)
                if norm_loc(b.name) in self._terminals:
                    draw_terminal_badge(p, px, py, pin_r)
                front.append((b, px, py))
            else:
                c2 = QColor(col); c2.setAlpha(55)
                p.setPen(Qt.NoPen); p.setBrush(c2)
                p.drawEllipse(QPointF(px, py), pin_r * 0.6, pin_r * 0.6)
        # Labels: hovered always; the rest fill in as space allows (collision-avoided).
        reveal = self._zoom >= _LABEL_REVEAL_ZOOM
        occupied: List[QRectF] = []
        for (b, px, py) in sorted(front, key=lambda t: 0 if t[0] is self._hovered else 1):
            hov = b is self._hovered
            if not (reveal or hov):
                continue
            draw_label(p, px, py, pin_r, b.name,
                       QColor(P.fg_bright if hov else P.fg), occupied, force=hov)

    def _draw_hud(self, p: QPainter, w: int, h: int) -> None:
        f = QFont(); f.setPointSizeF(8); p.setFont(f); p.setPen(QColor(P.fg_dim))
        p.drawText(12, h - 13,
                   f"drag to spin · wheel zoom (out: system) · right-click: lore · "
                   f"{len(self._locs)} surface locations")
        ft = QFont(); ft.setPointSizeF(13); ft.setBold(True); p.setFont(ft)
        p.setPen(QColor(P.tool_trade)); p.drawText(14, 28, self._planet_name.upper())

    # ── interaction ────────────────────────────────────────────────────────────
    def _hit(self, pt: QPointF, cx, cy, R) -> Optional[Body]:
        best = None
        bd = 11.0
        for b, d in zip(self._locs, self._dirs):
            x, y, z = self._rot(d)
            if z < 0:
                continue
            px, py = cx + x * R, cy - y * R
            dd = math.hypot(px - pt.x(), py - pt.y())
            if dd < bd:
                bd = dd
                best = b
        return best

    def mousePressEvent(self, ev) -> None:
        self._last = ev.position()
        self._dragging = True
        self._moved = 0.0

    def mouseMoveEvent(self, ev) -> None:
        pt = ev.position()
        if self._dragging and self._last is not None:
            dx = pt.x() - self._last.x(); dy = pt.y() - self._last.y(); self._last = pt
            self._moved += abs(dx) + abs(dy)
            self._yaw += dx * 0.01
            self._pitch = max(-1.45, min(1.45, self._pitch + dy * 0.01))
            self.update()
            return
        w, h = self.width(), self.height()
        b = self._hit(pt, w / 2.0, h / 2.0, self._radius(w, h))
        if b is not self._hovered:
            self._hovered = b
            self.setCursor(Qt.PointingHandCursor if b else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        was_click = self._moved < 4.0
        self._dragging = False
        if was_click and ev.button() == Qt.RightButton:   # right-click -> lore
            w, h = self.width(), self.height()
            b = self._hit(ev.position(), w / 2.0, h / 2.0, self._radius(w, h))
            name = b.name if b is not None else self._planet_name
            if name:
                self.loreRequested.emit(name, self._system)

    def mouseDoubleClickEvent(self, ev) -> None:
        w, h = self.width(), self.height()
        b = self._hit(ev.position(), w / 2.0, h / 2.0, self._radius(w, h))
        if b is None:
            return
        if norm_loc(b.name) in self._terminals:
            self.terminalClicked.emit(b.name, self._system)
        else:
            self.bodyActivated.emit(b.name, self._system)

    def wheelEvent(self, ev) -> None:
        d = ev.angleDelta().y()
        if not d:
            return
        self._zoom = max(0.45, min(5.0, self._zoom * (1.0015 ** d)))
        self.update()
        if d < 0 and self._zoom <= _DRILL_OUT_ZOOM:
            self.drillOut.emit()
