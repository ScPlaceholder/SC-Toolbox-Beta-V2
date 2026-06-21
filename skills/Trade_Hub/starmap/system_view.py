"""System-detail scene — one star system's bodies.

Star at centre, planets on projected orbit rings, plus moons, stations and the
asteroid belt. Bigger icons with labels centred underneath. Planet labels always
show; moon/station labels fill in as space allows (collision-avoided) and on
hover — so names get more complete as you zoom in. Pseudo-3D top-down.

Zoom-driven level-of-detail: zoom out far enough -> back to the galaxy; zoom in
on a planet -> open its globe. Double-click a planet also enters it.
Render-on-demand (no timers)."""
from __future__ import annotations

import math
from typing import List, Optional

from PySide6.QtCore import Qt, QPointF, QRectF, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPen, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from shared.qt.theme import P

from .camera import Camera
from .data import Body, LOC_ALIASES, load_bodies, norm_loc
from .labels import career_color, draw_label, recency_color

_DRILL_OUT_ZOOM = 0.45     # zoom out below this -> galaxy
_DRILL_IN_ZOOM = 7.0       # zoom in above this over a planet -> planet globe
_LABEL_REVEAL_ZOOM = 1.5   # moon/station labels start filling in past this zoom


def _stable_hue(name: str) -> int:
    """Deterministic hue 0-359 from a name (no per-process hash randomisation)."""
    return (sum(ord(c) for c in name) * 2654435761) % 360


def _body_color(b: Body) -> QColor:
    k = b.kind
    if k == "star":
        return QColor("#ffd86b")
    if k == "planet":
        return QColor.fromHsv(_stable_hue(b.name), 115, 205)
    if k == "moon":
        return QColor("#9aa6b8")
    if k == "station":
        return QColor(P.energy_cyan)
    if k == "jumppoint":
        return QColor(P.energy_cyan)
    if k == "asteroid":
        return QColor("#7a8290")
    if k == "outpost":
        return QColor(P.green)
    if k == "landing":
        return QColor(P.tool_trade)
    return QColor(P.fg_dim)


def draw_terminal_badge(p: QPainter, sx: float, sy: float, r: float) -> None:
    """Small gold $ badge marking a body that hosts commodity terminals."""
    bx, by = sx + r * 0.85, sy - r * 0.85
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(P.tool_trade))
    p.drawEllipse(QPointF(bx, by), 5.2, 5.2)
    f = QFont(); f.setPointSizeF(7.0); f.setBold(True); p.setFont(f)
    p.setPen(QColor("#1a1400"))
    p.drawText(QRectF(bx - 5.2, by - 6.8, 10.4, 13.6), Qt.AlignCenter, "$")


class SystemView(QWidget):
    planetEntered = Signal(str)     # double-click a planet -> open its globe
    drillIn = Signal(str)           # zoom in on a planet -> open its globe
    drillOut = Signal()             # zoom out -> back to galaxy
    terminalClicked = Signal(str, str)   # double-click a terminal body -> (name, system)
    jumpRequested = Signal(str)     # click a gateway's "Jump to X" -> destination system name
    bodyActivated = Signal(str, str)     # double-click a non-planet/non-terminal body -> (name, system)

    def __init__(self, system_code: str, system_name: str = "",
                 bodies: Optional[List[Body]] = None, terminal_names=None,
                 parent=None) -> None:
        super().__init__(parent)
        self._code = system_code.upper()
        self._name = system_name or system_code.title()
        if bodies is None:
            bodies = load_bodies().get(self._code, [])
        self._bodies = bodies
        self._terminals = set(terminal_names or ())
        self._trade_route: list = []     # [(name, x, y, z, role)] legs in THIS system
        self._frame_pts = None           # pending camera-fit to a route (applied in paint)
        self._overlay = None             # trade-activity overlay (panel-set)
        self._ov_flags = {}
        self._star = next((b for b in bodies if b.kind == "star"), None)
        self._planets = [b for b in bodies if b.kind == "planet"]
        self._moons = [b for b in bodies if b.kind == "moon"]
        self._stations = [b for b in bodies if b.kind == "station"]
        self._asteroids = [b for b in bodies if b.kind == "asteroid"]
        self._extra_stars = [b for b in bodies if b.kind == "star"
                             and (b.x or b.y or b.z)]   # binary companions (primary is at origin)
        self._markers = [b for b in bodies if b.kind in ("landing", "jumppoint")
                         or "nav" in b.type.lower()]
        self._by_norm = {}               # normalised body name -> Body (for overlay resolve)
        for b in self._bodies:
            self._by_norm.setdefault(norm_loc(b.name), b)
        # Gateways and jump points double as "Jump to <system>" buttons. In-game
        # systems use named gateway stations ("Pyro Gateway"); lore systems use
        # jump-point bodies named after the destination system they lead to.
        self._gateways = []
        for b in bodies:
            if b.name.lower().endswith("gateway"):
                d = b.name.rsplit(" Gateway", 1)[0].strip()
                if d:
                    self._gateways.append((b, d))
            elif b.kind == "jumppoint":
                self._gateways.append((b, b.name))
        self._jump_labels: list = []     # [(QRectF, dest_name)] filled in paint

        self._cam = Camera(yaw=0.35, pitch=1.05, zoom=1.0, fx=0.0, fy=0.0, fz=0.0)
        self._fit = self._compute_fit()

        self._hovered: Optional[Body] = None
        self._mode: Optional[str] = None
        self._last: Optional[QPointF] = None
        self._moved = 0.0

        self.setMinimumSize(360, 280)
        self.setMouseTracking(True)

    def _compute_fit(self) -> float:
        r = 1.0
        # frame everything that isn't the central star — planets, jump points and
        # asteroid belts included — so nothing sits off-screen on entry.
        pool = [b for b in self._bodies if b.kind != "star"] or self._bodies
        for b in pool:
            r = max(r, math.hypot(b.x, b.y, b.z))
        return r

    def _scale(self, w: int, h: int) -> float:
        return (min(w, h) * 0.42) / self._fit

    def _icon_radius(self, b: Body) -> float:
        return {"planet": 10.0, "moon": 4.5, "station": 4.5,
                "jumppoint": 6.0}.get(b.kind, 4.0)

    # ── painting ──────────────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        if self._frame_pts:
            self._apply_frame(w, h)
        cx, cy = w / 2.0, h / 2.0
        scale = self._scale(w, h)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_deepest))

        self._draw_orbits(p, cx, cy, scale)
        self._draw_asteroids(p, cx, cy, scale)
        occupied: List[QRectF] = []
        self._draw_star(p, cx, cy, scale, occupied)   # every system has a star (synthesised if no body data)
        self._draw_extra_stars(p, cx, cy, scale, occupied)   # binary companions
        self._draw_bodies(p, cx, cy, scale, occupied)
        self._draw_overlay(p, cx, cy, scale)
        self._draw_trade_route(p, cx, cy, scale)
        self._draw_gateway_jumps(p, cx, cy, scale)
        self._draw_hud(p, w, h)
        p.end()

    def set_trade_route(self, points) -> None:
        self._trade_route = list(points or [])
        self.update()

    def center_on(self, body, zoom: float = 4.0) -> None:
        """Snap the camera onto a body (search / snap-to / route-start)."""
        self._frame_pts = None                       # an explicit centre cancels a pending fit
        self._cam.fx, self._cam.fy, self._cam.fz = body.x, body.y, body.z
        self._cam.pan_x = self._cam.pan_y = 0.0
        self._cam.zoom = zoom
        self._hovered = body
        self.update()

    def frame_trade_route(self) -> bool:
        """Frame the camera so the whole in-system trade route fits on screen.
        Returns False when there are fewer than two positioned points, so the
        caller can fall back to centring on a single endpoint."""
        pts = [(x, y, z) for (_n, x, y, z, _r) in self._trade_route]
        if len(pts) < 2:
            return False
        self._frame_pts = pts
        self.update()
        return True

    def _apply_frame(self, w: int, h: int) -> None:
        """Fit the camera to ``self._frame_pts``. Deferred to paint so the real
        widget size is known; sets focus to the route's midpoint and a zoom that
        leaves every endpoint comfortably inside the viewport."""
        pts, self._frame_pts = self._frame_pts, None
        xs = [q[0] for q in pts]; ys = [q[1] for q in pts]; zs = [q[2] for q in pts]
        self._cam.fx = (min(xs) + max(xs)) / 2.0
        self._cam.fy = (min(ys) + max(ys)) / 2.0
        self._cam.fz = (min(zs) + max(zs)) / 2.0
        self._cam.pan_x = self._cam.pan_y = 0.0
        cx, cy = w / 2.0, h / 2.0
        scale = self._scale(w, h)
        self._cam.zoom = 1.0
        maxoff = 1.0
        for (x, y, z) in pts:
            sx, sy, _ = self._cam.project(x, y, z, cx, cy, scale)
            maxoff = max(maxoff, math.hypot(sx - cx, sy - cy))
        self._cam.zoom = max(0.3, min(20.0, (0.40 * min(w, h)) / maxoff))

    # ── trade-activity overlay ────────────────────────────────────────────────
    def set_overlay(self, overlay, flags) -> None:
        self._overlay = overlay
        self._ov_flags = dict(flags or {})
        self.update()

    def _resolve(self, name: str):
        n = norm_loc(name)
        return self._by_norm.get(LOC_ALIASES.get(n, n))

    def _draw_overlay(self, p: QPainter, cx, cy, scale) -> None:
        ov = self._overlay
        if ov is None:
            return
        proj = lambda b: self._cam.project(b.x, b.y, b.z, cx, cy, scale)[:2]
        if self._ov_flags.get("flows"):
            maxf = max(1, ov.max_flow)
            for a, b, count, _profit in ov.terminal_flows.get(self._code, []):
                ba, bb = self._resolve(a), self._resolve(b)
                if ba is None or bb is None:
                    continue
                (ax, ay), (bx, by) = proj(ba), proj(bb)
                t = count / maxf
                col = QColor(P.tool_trade); col.setAlpha(int(38 + 150 * t))
                pen = QPen(col); pen.setWidthF(1.0 + 4.0 * t); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen); p.drawLine(QPointF(ax, ay), QPointF(bx, by))
        if self._ov_flags.get("top"):
            for nameA, nameB in ov.top_lines_for(self._code):
                ba, bb = self._resolve(nameA), self._resolve(nameB)
                if ba is None or bb is None:
                    continue
                (ax, ay), (bx, by) = proj(ba), proj(bb)
                pen = QPen(QColor(P.accent)); pen.setWidthF(2.2); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen); p.drawLine(QPointF(ax, ay), QPointF(bx, by))
        if self._ov_flags.get("activity"):
            for b in self._bodies:
                t = ov.body_recency.get((self._code, norm_loc(b.name)))
                if t is None:
                    continue
                sx, sy = proj(b)
                pen = QPen(recency_color(t)); pen.setWidthF(2.2)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                rr = self._icon_radius(b) + 2.5
                p.drawEllipse(QPointF(sx, sy), rr, rr)
        if self._ov_flags.get("career") and ov.career_system_lines:
            vmax, mc = ov.career_line_vmax, max(1, ov.career_max_count)
            for nameA, nameB, net, count in ov.career_system_lines.get(self._code, []):
                ba, bb = self._resolve(nameA), self._resolve(nameB)
                if ba is None or bb is None:
                    continue
                (ax, ay), (bx, by) = proj(ba), proj(bb)
                pen = QPen(career_color(net, vmax)); pen.setWidthF(1.6 + 2.6 * (count / mc))
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen); p.drawLine(QPointF(ax, ay), QPointF(bx, by))
        if self._ov_flags.get("career") and ov.career_body:
            for b in self._bodies:
                net = ov.career_body.get((self._code, norm_loc(b.name)))
                if net is None:
                    continue
                sx, sy = proj(b)
                pen = QPen(career_color(net, ov.career_vmax)); pen.setWidthF(2.8)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                rr = self._icon_radius(b) + 5.0
                p.drawEllipse(QPointF(sx, sy), rr, rr)

    def _draw_trade_route(self, p: QPainter, cx, cy, scale) -> None:
        if not self._trade_route:
            return
        proj = [(self._cam.project(x, y, z, cx, cy, scale)[:2], role)
                for (_n, x, y, z, role) in self._trade_route]
        qpts = [QPointF(sx, sy) for ((sx, sy), _r) in proj]
        if len(qpts) >= 2:
            glow = QPen(QColor(P.tool_trade)); glow.setWidthF(8.0)
            gc = QColor(P.tool_trade); gc.setAlpha(70); glow.setColor(gc)
            glow.setCapStyle(Qt.RoundCap); glow.setJoinStyle(Qt.RoundJoin)
            p.setPen(glow); p.drawPolyline(QPolygonF(qpts))
            core = QPen(QColor(P.tool_trade)); core.setWidthF(2.4)
            core.setCapStyle(Qt.RoundCap); core.setJoinStyle(Qt.RoundJoin)
            p.setPen(core); p.drawPolyline(QPolygonF(qpts))
        f = QFont(); f.setPointSizeF(9.5); f.setBold(True); p.setFont(f)
        for ((sx, sy), role) in proj:
            if role == "buy":
                col, tag = QColor(P.green), "BUY"
            elif role == "sell":
                col, tag = QColor(P.tool_trade), "SELL"
            elif role == "jump":
                col, tag = QColor(P.energy_cyan), "JUMP"
            else:
                col, tag = QColor(P.fg_bright), ""
            p.setPen(Qt.NoPen); p.setBrush(col)
            p.drawEllipse(QPointF(sx, sy), 6.5, 6.5)
            if tag:
                p.setPen(col)
                p.drawText(QPointF(sx + 9, sy - 6), tag)

    def _draw_gateway_jumps(self, p: QPainter, cx, cy, scale) -> None:
        """A clickable '⤴ Jump to <system>' chip under each gateway, so you can
        hop straight to the connected system's view without going via the galaxy."""
        self._jump_labels = []
        if not self._gateways:
            return
        f = QFont(); f.setPointSizeF(8.5); f.setBold(True); p.setFont(f)
        fm = p.fontMetrics()
        for b, dest in self._gateways:
            sx, sy, _ = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            txt = f"⤴ Jump to {dest}"
            tw = fm.horizontalAdvance(txt)
            x = sx - tw / 2.0
            y = sy + self._icon_radius(b) + 34.0
            rect = QRectF(x - 6, y - 13, tw + 12, 19)
            bg = QColor(P.energy_cyan); bg.setAlpha(48)
            p.setBrush(bg); p.setPen(QPen(QColor(P.energy_cyan), 1.0))
            p.drawRoundedRect(rect, 5, 5)
            p.setPen(QColor(P.energy_cyan)); p.setBrush(Qt.NoBrush)
            p.drawText(QPointF(x, y), txt)
            self._jump_labels.append((rect, dest))

    def _hit_jump(self, pt) -> Optional[str]:
        for rect, dest in self._jump_labels:
            if rect.contains(pt):
                return dest
        return None

    def _draw_orbits(self, p: QPainter, cx, cy, scale) -> None:
        col = QColor(P.accent); col.setAlpha(60)
        pen = QPen(col); pen.setWidthF(1.1)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        for b in self._planets:
            rad = math.hypot(b.x, b.y)
            pts = []
            for i in range(0, 73):
                t = i / 72.0 * 2 * math.pi
                sx, sy, _ = self._cam.project(rad * math.cos(t), rad * math.sin(t),
                                              0.0, cx, cy, scale)
                pts.append(QPointF(sx, sy))
            p.drawPolyline(QPolygonF(pts))

    def _draw_asteroids(self, p: QPainter, cx, cy, scale) -> None:
        col = QColor("#7a8290"); col.setAlpha(120)
        for b in self._asteroids:
            if "belt" in b.type.lower():        # belt -> dotted ring at its radius
                rad = math.hypot(b.x, b.y)
                pen = QPen(col); pen.setWidthF(1.3); pen.setStyle(Qt.DotLine)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                pts = []
                for i in range(0, 97):
                    t = i / 96.0 * 2 * math.pi
                    sx, sy, _ = self._cam.project(rad * math.cos(t), rad * math.sin(t),
                                                  0.0, cx, cy, scale)
                    pts.append(QPointF(sx, sy))
                p.drawPolyline(QPolygonF(pts))
            else:                               # lone rock -> a point
                sx, sy, _ = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
                p.setPen(col); p.drawPoint(int(sx), int(sy))

    def _draw_extra_stars(self, p: QPainter, cx, cy, scale, occupied) -> None:
        """Binary/companion stars (the primary is the central one at the origin)."""
        for b in self._extra_stars:
            sx, sy, _ = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            c = QPointF(sx, sy)
            col = QColor("#ffd86b")
            glow = QRadialGradient(c, 26)
            g0 = QColor(col); g0.setAlpha(140); glow.setColorAt(0, g0)
            g1 = QColor(col); g1.setAlpha(0); glow.setColorAt(1, g1)
            p.setPen(Qt.NoPen); p.setBrush(QBrush(glow)); p.drawEllipse(c, 26, 26)
            p.setBrush(QColor("#fff0c0")); p.drawEllipse(c, 7, 7)
            self._put_label(p, sx, sy, 7.0, b.name, "STAR", QColor(P.fg), occupied, force=True)

    def _draw_star(self, p: QPainter, cx, cy, scale, occupied) -> None:
        sx, sy, _ = self._cam.project(0, 0, 0, cx, cy, scale)
        c = QPointF(sx, sy)
        col = QColor("#ffd86b")
        glow = QRadialGradient(c, 44)
        g0 = QColor(col); g0.setAlpha(155); glow.setColorAt(0, g0)
        g1 = QColor(col); g1.setAlpha(0); glow.setColorAt(1, g1)
        p.setPen(Qt.NoPen); p.setBrush(QBrush(glow)); p.drawEllipse(c, 44, 44)
        p.setBrush(QColor("#fff0c0")); p.drawEllipse(c, 12, 12)
        self._put_label(p, sx, sy, 12.0, self._name, "STAR", QColor(P.fg), occupied, force=True)

    def _draw_bodies(self, p: QPainter, cx, cy, scale, occupied) -> None:
        items = []
        for b in self._planets + self._moons + self._stations + self._markers:
            sx, sy, d = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            items.append((b, sx, sy, d))
        items.sort(key=lambda t: t[3])
        for (b, sx, sy, _d) in items:           # icons first (depth order)
            self._draw_icon(p, b, sx, sy)

        reveal = self._cam.zoom >= _LABEL_REVEAL_ZOOM

        def prio(it):
            b = it[0]
            if b is self._hovered:
                return 0
            if b.kind == "planet":
                return 1
            if b.kind in ("landing", "station"):
                return 2
            return 3
        for (b, sx, sy, _d) in sorted(items, key=prio):
            if b.kind == "jumppoint" and b is not self._hovered:
                continue                     # the 'Jump to X' chip already labels it
            must = b.kind == "planet" or b is self._hovered
            if not (must or reveal):
                continue
            sub = "PLANET" if b.kind == "planet" else None
            color = QColor(P.fg_bright) if (b.kind == "planet" or b is self._hovered) \
                else _body_color(b).lighter(125)
            self._put_label(p, sx, sy, self._icon_radius(b), b.name, sub, color,
                            occupied, force=must)

    def _draw_icon(self, p: QPainter, b: Body, sx: float, sy: float) -> None:
        c = QPointF(sx, sy)
        col = _body_color(b)
        k = b.kind
        if k == "planet":
            r = 10.0
            glow = QRadialGradient(c, r * 3)
            g0 = QColor(col); g0.setAlpha(130); glow.setColorAt(0, g0)
            g1 = QColor(col); g1.setAlpha(0); glow.setColorAt(1, g1)
            p.setPen(Qt.NoPen); p.setBrush(QBrush(glow)); p.drawEllipse(c, r * 3, r * 3)
            p.setBrush(col); p.drawEllipse(c, r, r)
        elif k == "moon":
            p.setPen(Qt.NoPen); p.setBrush(col); p.drawEllipse(c, 4.5, 4.5)
        elif k == "station":
            pen = QPen(col); pen.setWidthF(1.6); p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawRect(int(sx - 4), int(sy - 4), 8, 8)
        elif k == "jumppoint":
            pen = QPen(QColor(P.energy_cyan)); pen.setWidthF(1.7)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            d = 5.5
            p.drawPolygon(QPolygonF([QPointF(sx, sy - d), QPointF(sx + d, sy),
                                     QPointF(sx, sy + d), QPointF(sx - d, sy)]))
        else:
            pen = QPen(col); pen.setWidthF(1.4); p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, 4, 4)
        if norm_loc(b.name) in self._terminals:
            draw_terminal_badge(p, sx, sy, self._icon_radius(b))

    def _put_label(self, p: QPainter, sx: float, sy: float, r: float, name: str,
                   sub: Optional[str], color: QColor, occupied: List[QRectF],
                   force: bool = False) -> None:
        draw_label(p, sx, sy, r, name, color, occupied,
                   sub=sub, force=force, big=sub in ("PLANET", "STAR"))

    def _draw_hud(self, p: QPainter, w: int, h: int) -> None:
        f = QFont(); f.setPointSizeF(8); p.setFont(f); p.setPen(QColor(P.fg_dim))
        p.drawText(12, h - 13,
                   "drag rotate · wheel zoom (in: planet · out: galaxy) · double-click a planet")
        ft = QFont(); ft.setPointSizeF(13); ft.setBold(True); p.setFont(ft)
        p.setPen(QColor(P.tool_trade)); p.drawText(14, 28, f"{self._name.upper()} SYSTEM")
        fc = QFont(); fc.setPointSizeF(8); p.setFont(fc); p.setPen(QColor(P.fg_dim))
        if self._bodies:
            p.drawText(14, 44, f"{len(self._planets)} planets · {len(self._moons)} moons · "
                               f"{len(self._stations)} stations  ·  hover or zoom for names")
        else:
            p.drawText(14, 44, "lore system · no detailed map data yet — drag / wheel to look around")

    # ── interaction ────────────────────────────────────────────────────────────
    def _hit(self, pt: QPointF) -> Optional[Body]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        scale = self._scale(w, h)
        best = None
        bd = 15.0
        for b in self._planets + self._moons + self._stations + self._markers:
            sx, sy, _ = self._cam.project(b.x, b.y, b.z, cx, cy, scale)
            d = math.hypot(sx - pt.x(), sy - pt.y())
            if d < bd:
                bd = d
                best = b
        return best

    def _focal_planet(self, pt=None) -> Optional[str]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        tx, ty = (pt.x(), pt.y()) if pt is not None else (cx, cy)
        scale = self._scale(w, h)
        best = None
        bd = float(max(w, h))   # nearest planet to the cursor
        for b in self._planets:
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
        jump = self._hit_jump(pt)
        self.setCursor(Qt.PointingHandCursor if (jump or (b and b.kind == "planet")) else Qt.ArrowCursor)
        if b is not self._hovered:
            self._hovered = b
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        was_click = self._moved < 4.0
        self._mode = None
        self._last = None
        if was_click:
            dest = self._hit_jump(ev.position())
            if dest:
                self.jumpRequested.emit(dest)

    def mouseDoubleClickEvent(self, ev) -> None:
        b = self._hit(ev.position())
        if b is None:
            return
        if b.kind == "planet":
            self.planetEntered.emit(b.name)
        elif norm_loc(b.name) in self._terminals:
            self.terminalClicked.emit(b.name, self._code)
        else:
            self.bodyActivated.emit(b.name, self._code)

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
            pn = self._focal_planet(pt)
            if pn:
                self._cam.zoom = 3.0
                self.drillIn.emit(pn)
