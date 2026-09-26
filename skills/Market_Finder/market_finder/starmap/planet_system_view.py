"""Planet-neighbourhood scene — a planet with its moons (and orbiting stations).

Centred on the planet; moons sit on projected orbit rings around it. This is the
step between the system view and a body's surface globe: from here you pick the
terrestrial body (the planet or one of its moons) to zoom into.

Double-click or zoom into a body -> its surface globe. Zoom out -> back to the
system. Render-on-demand (no timers)."""
from __future__ import annotations

import math
from typing import List, Optional

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPen, QPolygonF, QRadialGradient
from PySide6.QtWidgets import QWidget

from shared.qt.theme import P

from .camera import Camera
from .data import Body, load_bodies, norm_loc
from .labels import draw_label
from .system_view import _body_color, draw_terminal_badge

_DRILL_OUT_ZOOM = 0.45      # zoom out -> system
_DRILL_IN_ZOOM = 6.0        # zoom in on a body -> its surface globe
_LABEL_REVEAL_ZOOM = 1.3


class PlanetSystemView(QWidget):
    bodyEntered = Signal(str)   # double-click / zoom into the planet or a moon -> globe
    drillIn = Signal(str)
    drillOut = Signal()
    terminalClicked = Signal(str, str)   # double-click a station with terminals -> (name, system)
    bodyActivated = Signal(str, str)     # double-click any other body (e.g. CIG HQ) -> (name, system)
    loreRequested = Signal(str, str)     # right-click a body -> (name, system) lore lookup

    def __init__(self, system_code: str, planet_name: str,
                 bodies: Optional[List[Body]] = None, terminal_names=None,
                 parent=None) -> None:
        super().__init__(parent)
        self._system = system_code.upper()
        self._planet_name = planet_name
        self._terminals = set(terminal_names or ())
        if bodies is None:
            bodies = load_bodies().get(self._system, [])
        self._planet = next((b for b in bodies
                             if b.name == planet_name and b.kind == "planet"), None)
        self._moons = [b for b in bodies if b.parent == planet_name and b.kind == "moon"]
        self._stations = [b for b in bodies if b.parent == planet_name and b.kind == "station"]
        # Terrestrial bodies you can drop into: the planet + its moons.
        self._terra: List[Body] = ([self._planet] if self._planet else []) + self._moons

        cx, cy, cz = (self._planet.x, self._planet.y, self._planet.z) if self._planet else (0, 0, 0)
        self._pc = (cx, cy, cz)
        self._cam = Camera(yaw=0.35, pitch=1.0, zoom=1.0, fx=cx, fy=cy, fz=cz)
        self._fit = self._compute_fit()

        self._hovered: Optional[Body] = None
        self._mode: Optional[str] = None
        self._last: Optional[QPointF] = None
        self._moved = 0.0

        self.setMinimumSize(360, 280)
        self.setMouseTracking(True)

    def _compute_fit(self) -> float:
        r = 1.0
        for b in self._moons + self._stations:
            r = max(r, math.dist((b.x, b.y, b.z), self._pc))
        if r <= 1.0:           # lone planet (no moons/stations) — sensible default scale
            r = 1.2e9
        return r

    def _scale(self, w: int, h: int) -> float:
        return (min(w, h) * 0.40) / self._fit

    def _planet_radius(self) -> float:
        return 22.0 + 6.0 * min(self._cam.zoom, 3.0)

    def _moon_radius(self) -> float:
        return 7.0 + 2.0 * min(self._cam.zoom, 3.0)

    # ── painting ──────────────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        cx, cy = w / 2.0, h / 2.0
        scale = self._scale(w, h)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_deepest))

        self._draw_orbits(p, cx, cy, scale)
        occupied: List[QRectF] = []
        if self._planet is not None:
            self._draw_planet(p, cx, cy, scale, occupied)
        self._draw_satellites(p, cx, cy, scale, occupied)
        self._draw_hud(p, w, h)
        p.end()

    def _draw_orbits(self, p: QPainter, cx, cy, scale) -> None:
        col = QColor(P.accent); col.setAlpha(55)
        pen = QPen(col); pen.setWidthF(1.0)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        px, py, pz = self._pc
        for b in self._moons:
            rad = math.hypot(b.x - px, b.y - py)
            pts = []
            for i in range(0, 73):
                t = i / 72.0 * 2 * math.pi
                sx, sy, _ = self._cam.project(px + rad * math.cos(t), py + rad * math.sin(t),
                                              pz, cx, cy, scale)
                pts.append(QPointF(sx, sy))
            p.drawPolyline(QPolygonF(pts))

    def _draw_planet(self, p: QPainter, cx, cy, scale, occupied) -> None:
        sx, sy, _ = self._cam.project(*self._pc, cx, cy, scale)
        c = QPointF(sx, sy)
        col = _body_color(self._planet)
        R = self._planet_radius()
        grad = QRadialGradient(QPointF(sx - R * 0.3, sy - R * 0.3), R * 1.3)
        grad.setColorAt(0.0, QColor(col).lighter(135))
        grad.setColorAt(0.7, col)
        grad.setColorAt(1.0, QColor(col).darker(165))
        p.setPen(Qt.NoPen); p.setBrush(QBrush(grad)); p.drawEllipse(c, R, R)
        rim = QPen(QColor(col).lighter(160)); rim.setWidthF(1.3)
        p.setPen(rim); p.setBrush(Qt.NoBrush); p.drawEllipse(c, R, R)
        if self._planet.name in self._terminals:
            draw_terminal_badge(p, sx, sy, R)
        self._put_label(p, sx, sy, R, self._planet.name, "PLANET",
                        QColor(P.fg_bright), occupied, force=True)

    def _draw_satellites(self, p: QPainter, cx, cy, scale, occupied) -> None:
        reveal = self._cam.zoom >= _LABEL_REVEAL_ZOOM
        mr = self._moon_radius()
        items = []
        for b in self._moons + self._stations:
            sx, sy, d = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            items.append((b, sx, sy, d))
        for (b, sx, sy, _d) in sorted(items, key=lambda t: t[3]):
            col = _body_color(b)
            c = QPointF(sx, sy)
            if b.kind == "moon":
                grad = QRadialGradient(QPointF(sx - mr * 0.3, sy - mr * 0.3), mr * 1.3)
                grad.setColorAt(0.0, QColor(col).lighter(130))
                grad.setColorAt(1.0, QColor(col).darker(150))
                p.setPen(Qt.NoPen); p.setBrush(QBrush(grad)); p.drawEllipse(c, mr, mr)
            else:
                pen = QPen(col); pen.setWidthF(1.6); p.setPen(pen); p.setBrush(Qt.NoBrush)
                p.drawRect(int(sx - 4), int(sy - 4), 8, 8)
            if norm_loc(b.name) in self._terminals:
                draw_terminal_badge(p, sx, sy, mr if b.kind == "moon" else 5.0)
        # labels (moons always; stations as space allows / hover)
        for (b, sx, sy, _d) in sorted(items, key=lambda t: 0 if t[0].kind == "moon" else 1):
            must = b.kind == "moon" or b is self._hovered
            if not (must or reveal):
                continue
            r = mr if b.kind == "moon" else 5.0
            sub = "MOON" if b.kind == "moon" else None
            col = QColor(P.fg_bright) if (b.kind == "moon" or b is self._hovered) \
                else _body_color(b).lighter(125)
            self._put_label(p, sx, sy, r, b.name, sub, col, occupied, force=must)

    def _put_label(self, p: QPainter, sx, sy, r, name, sub, color, occupied, force=False) -> None:
        draw_label(p, sx, sy, r, name, color, occupied,
                   sub=sub, force=force, big=sub == "PLANET")

    def _draw_hud(self, p: QPainter, w: int, h: int) -> None:
        f = QFont(); f.setPointSizeF(8); p.setFont(f); p.setPen(QColor(P.fg_dim))
        p.drawText(12, h - 13,
                   "drag rotate · wheel zoom (in: surface · out: system) · "
                   "double-click a body to land · right-click: lore")
        ft = QFont(); ft.setPointSizeF(13); ft.setBold(True); p.setFont(ft)
        p.setPen(QColor(P.tool_trade))
        p.drawText(14, 28, f"{self._planet_name.upper()} & MOONS")
        fc = QFont(); fc.setPointSizeF(8); p.setFont(fc); p.setPen(QColor(P.fg_dim))
        p.drawText(14, 44, f"{len(self._moons)} moons · {len(self._stations)} stations")

    # ── interaction ────────────────────────────────────────────────────────────
    def _hit(self, pt: QPointF) -> Optional[Body]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        scale = self._scale(w, h)
        best = None
        bd = 18.0
        for b in self._terra + self._stations:
            sx, sy, _ = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            d = math.hypot(sx - pt.x(), sy - pt.y())
            if d < bd:
                bd = d
                best = b
        return best

    def _focal_body(self, pt=None) -> Optional[str]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        tx, ty = (pt.x(), pt.y()) if pt is not None else (cx, cy)
        scale = self._scale(w, h)
        best = None
        bd = float(max(w, h))   # nearest terrestrial body to the cursor
        for b in self._terra:
            sx, sy, _ = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            d = math.hypot(sx - tx, sy - ty)
            if d < bd:
                bd = d
                best = b.name
        return best

    def mousePressEvent(self, ev) -> None:
        self._last = ev.position()
        self._moved = 0.0
        self._mode = "pan" if ev.button() == Qt.RightButton else "rotate"

    def mouseMoveEvent(self, ev) -> None:
        pt = ev.position()
        if self._mode and self._last is not None:
            dx = pt.x() - self._last.x(); dy = pt.y() - self._last.y(); self._last = pt
            self._moved += abs(dx) + abs(dy)
            if self._mode == "rotate":
                self._cam.rotate(dx * 0.008, -dy * 0.008)
            else:
                self._cam.pan_x += dx; self._cam.pan_y += dy
            self.update()
            return
        b = self._hit(pt)
        if b is not self._hovered:
            self._hovered = b
            enterable = b in self._terra if b else False
            self.setCursor(Qt.PointingHandCursor if enterable else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        was_click = self._moved < 4.0
        btn = ev.button()
        self._mode = None
        self._last = None
        if was_click and btn == Qt.RightButton:     # right-click -> lore for that body
            b = self._hit(ev.position())
            name = b.name if b is not None else (self._planet_name
                                                 if self._planet is not None else None)
            if name:
                self.loreRequested.emit(name, self._system)

    def mouseDoubleClickEvent(self, ev) -> None:
        b = self._hit(ev.position())
        if b is None:
            return
        if b in self._terra:
            self.bodyEntered.emit(b.name)
        elif norm_loc(b.name) in self._terminals:
            self.terminalClicked.emit(b.name, self._system)
        else:
            self.bodyActivated.emit(b.name, self._system)

    def wheelEvent(self, ev) -> None:
        d = ev.angleDelta().y()
        if not d:
            return
        pt = ev.position()
        w, h = self.width(), self.height()
        self._cam.zoom_at(1.0015 ** d, pt.x(), pt.y(), w / 2.0, h / 2.0, 0.2, 40.0)
        self.update()
        if d < 0 and self._cam.zoom <= _DRILL_OUT_ZOOM:
            self.drillOut.emit()
        elif d > 0 and self._cam.zoom >= _DRILL_IN_ZOOM:
            bn = self._focal_body(pt)
            if bn:
                self._cam.zoom = 3.0
                self.drillIn.emit(bn)
