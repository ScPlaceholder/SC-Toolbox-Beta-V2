"""Galaxy scene — the top-level star map view.

Custom-painted (QPainter) pseudo-3D galaxy: ~90 systems as glowing nodes with
jump-point links, drag-to-rotate / wheel-to-zoom / right-drag-to-pan.

Features: home marker + snap-to-home, an out-of-bounds warning for systems that
aren't in-game yet, jump-point route plotting, and double-click-to-enter (which
opens the system-detail view for in-game systems).

Render-on-demand: there is NO timer. ``update()`` is only called in response to
user input, so the widget paints at ~0% CPU when idle — the <1% target.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import Qt, QPointF, Signal
from PySide6.QtGui import (
    QBrush, QColor, QFont, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from shared.qt.theme import P

from .camera import Camera
from .data import Galaxy, System
from .labels import career_color, recency_color

# Per-affiliation node colour (falls back to a neutral blue-grey).
_AFFIL: Dict[str, str] = {
    "UEE": "#3a7bd5",
    "Vanduul": "#ff5533",
    "Xi'an": "#9a6bff",
    "Banu": "#ffb13b",
    "Unclaimed": "#6b7794",
    "Developing": "#33dd88",
}


class GalaxyView(QWidget):
    """Top-level galaxy scene."""

    systemSelected = Signal(str)      # single-click select
    systemEntered = Signal(str)       # double-click an in-game system -> enter it
    drillIn = Signal(str)             # zoom past threshold over a system -> enter it
    setHomeRequested = Signal(str)    # user asked to make a system their home
    routePlotted = Signal(str, str, int, float)   # start, end, jumps, light-years
    viewChanged = Signal()            # any camera/selection/route change to persist
    loreRequested = Signal(str, str)  # right-click a system -> (lore-title, code) lore lookup

    def __init__(self, galaxy: Galaxy, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._galaxy = galaxy
        self._edges: List[Tuple[System, System]] = galaxy.jump_edges()

        self._cam = Camera(yaw=0.6, pitch=0.5, zoom=1.0)
        focus = galaxy.get("STANTON") or (galaxy.systems[0] if galaxy.systems else None)
        if focus is not None:
            self._cam.fx, self._cam.fy, self._cam.fz = focus.x, focus.y, focus.z
        self._fit_radius = self._compute_fit_radius()

        self._home: Optional[str] = None
        self._hovered: Optional[str] = None
        self._selected: Optional[str] = None

        # Trade-activity overlay (set by the panel; only drawn for enabled layers).
        self._overlay = None
        self._ov_flags: Dict[str, bool] = {}

        # Route state (M4).
        self._route_mode = False
        self._route_start: Optional[str] = None
        self._route_end: Optional[str] = None
        self._route: Optional[List[str]] = None

        # Interaction state.
        self._mode: Optional[str] = None      # "rotate" | "pan" | None
        self._last: Optional[QPointF] = None
        self._moved: float = 0.0

        # Static, seeded starfield (generated once; never animated).
        rng = random.Random(20151123)
        self._stars = [(rng.random(), rng.random(), rng.random()) for _ in range(240)]

        self.setMinimumSize(360, 280)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.StrongFocus)

    # ── geometry helpers ────────────────────────────────────────────────────
    def _compute_fit_radius(self) -> float:
        r = 1.0
        for s in self._galaxy.systems:
            d = math.dist((s.x, s.y, s.z), (self._cam.fx, self._cam.fy, self._cam.fz))
            r = max(r, d)
        return r

    def _base_scale(self, w: int, h: int) -> float:
        return (min(w, h) * 0.44) / self._fit_radius

    def _node_radius(self, df: float) -> float:
        z = self._cam.zoom
        return (3.4 + 6.0 * df) * (0.95 + 0.18 * min(z, 3.0))

    def _affil_color(self, s: System) -> str:
        return _AFFIL.get(s.affiliation, "#6b7794")

    def _hit_test(self, pt: QPointF, thresh: float = 13.0) -> Optional[str]:
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        base = self._base_scale(w, h)
        best: Optional[str] = None
        best_d = thresh
        for s in self._galaxy.systems:
            sx, sy, _ = self._cam.project(s.x, s.y, s.z, cx, cy, base)
            d = math.hypot(sx - pt.x(), sy - pt.y())
            if d < best_d:
                best_d = d
                best = s.code
        return best

    def _focal_system(self, pt: Optional[QPointF] = None) -> Optional[str]:
        """The system nearest the cursor (or centre) — the zoom-drill target.
        Any system, in-game or lore (lore ones just open a sparse view)."""
        w, h = self.width(), self.height()
        cx, cy = w / 2.0, h / 2.0
        tx, ty = (pt.x(), pt.y()) if pt is not None else (cx, cy)
        base = self._base_scale(w, h)
        best = None
        bd = float(max(w, h))
        for s in self._galaxy.systems:
            sx, sy, _ = self._cam.project(s.x, s.y, s.z, cx, cy, base)
            d = math.hypot(sx - tx, sy - ty)
            if d < bd:
                bd = d
                best = s.code
        return best

    # ── painting ────────────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:
        w, h = self.width(), self.height()
        if w <= 0 or h <= 0:
            return
        cx, cy = w / 2.0, h / 2.0
        base = self._base_scale(w, h)

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_deepest))
        self._draw_starfield(p, w, h)

        proj = [
            (s, *self._cam.project(s.x, s.y, s.z, cx, cy, base))
            for s in self._galaxy.systems
        ]
        depths = [d for (_s, _x, _y, d) in proj]
        dmin = min(depths)
        drange = (max(depths) - dmin) or 1.0
        pos = {s.code: (sx, sy, d) for (s, sx, sy, d) in proj}

        self._draw_edges(p, pos, dmin, drange)
        self._draw_flow_lines(p, pos)        # trade-activity overlay (under nodes)
        self._draw_route(p, pos)
        for (s, sx, sy, d) in sorted(proj, key=lambda t: t[3]):   # far -> near
            self._draw_node(p, s, sx, sy, (d - dmin) / drange)
        self._draw_labels(p, proj, dmin, drange)
        self._draw_hud(p, w, h)
        p.end()

    def _draw_starfield(self, p: QPainter, w: int, h: int) -> None:
        base = QColor(P.fg)
        for fx, fy, b in self._stars:
            base.setAlpha(int(16 + 46 * b))
            p.setPen(base)
            p.drawPoint(int(fx * w), int(fy * h))

    def _draw_edges(self, p: QPainter, pos: Dict[str, Tuple[float, float, float]],
                    dmin: float, drange: float) -> None:
        pen = QPen()
        for a, b in self._edges:
            pa = pos.get(a.code)
            pb = pos.get(b.code)
            if pa is None or pb is None:
                continue
            df = (((pa[2] + pb[2]) / 2.0) - dmin) / drange
            col = QColor(P.energy_cyan)
            col.setAlpha(int(16 + 40 * df))
            pen.setColor(col)
            pen.setWidthF(0.7 + 0.8 * df)
            p.setPen(pen)
            p.drawLine(QPointF(pa[0], pa[1]), QPointF(pb[0], pb[1]))

    def _draw_route(self, p: QPainter, pos: Dict[str, Tuple[float, float, float]]) -> None:
        if not self._route or len(self._route) < 2:
            return
        pts = [QPointF(pos[c][0], pos[c][1]) for c in self._route if c in pos]
        if len(pts) < 2:
            return
        # Glow underlay then bright core.
        glow = QPen(QColor(P.tool_trade)); glow.setWidthF(7.0)
        gc = QColor(P.tool_trade); gc.setAlpha(60); glow.setColor(gc)
        glow.setCapStyle(Qt.RoundCap); glow.setJoinStyle(Qt.RoundJoin)
        p.setPen(glow)
        p.drawPolyline(QPolygonF(pts))
        core = QPen(QColor(P.tool_trade)); core.setWidthF(2.2)
        core.setCapStyle(Qt.RoundCap); core.setJoinStyle(Qt.RoundJoin)
        p.setPen(core)
        p.drawPolyline(QPolygonF(pts))

    # ── trade-activity overlay ────────────────────────────────────────────────
    def set_overlay(self, overlay, flags: Dict[str, bool]) -> None:
        self._overlay = overlay
        self._ov_flags = dict(flags or {})
        self.update()

    def _code_for(self, name: str) -> Optional[str]:
        if not name:
            return None
        nl = name.lower()
        for s in self._galaxy.systems:
            if s.name.lower() == nl or s.code.lower() == nl:
                return s.code
        return name.upper()

    @staticmethod
    def _arc_path(pa, pb) -> QPainterPath:
        """A bowed quadratic between two node positions, so links between systems
        that sit close together on the galaxy are still visible as arcs."""
        x1, y1, x2, y2 = pa[0], pa[1], pb[0], pb[1]
        dx, dy = x2 - x1, y2 - y1
        ln = math.hypot(dx, dy) or 1.0
        bow = max(24.0, ln * 0.16)
        cx, cy = (x1 + x2) / 2.0 - dy / ln * bow, (y1 + y2) / 2.0 + dx / ln * bow
        path = QPainterPath(QPointF(x1, y1))
        path.quadTo(QPointF(cx, cy), QPointF(x2, y2))
        return path

    def _draw_flow_lines(self, p: QPainter, pos) -> None:
        ov = self._overlay
        if ov is None:
            return
        p.setBrush(Qt.NoBrush)
        if self._ov_flags.get("flows"):
            maxf = max(1, ov.max_flow)
            f = QFont(); f.setPointSizeF(8.0); f.setBold(True)
            for a, b, count, _profit in ov.system_flows:
                pa, pb = pos.get(a), pos.get(b)
                if pa is None or pb is None:
                    continue
                t = count / maxf
                col = QColor(P.tool_trade); col.setAlpha(int(60 + 160 * t))
                pen = QPen(col); pen.setWidthF(1.6 + 5.5 * t); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                path = self._arc_path(pa, pb)
                p.drawPath(path)
                ap = path.pointAtPercent(0.5)
                p.setFont(f); p.setPen(QColor(P.tool_trade))
                p.drawText(QPointF(ap.x() + 3, ap.y() - 2), f"{count}")
        if self._ov_flags.get("top"):
            seen = set()
            for r in ov.top_cross_system():
                a = self._code_for(getattr(r, "buy_system", ""))
                b = self._code_for(getattr(r, "sell_system", ""))
                pa, pb = pos.get(a), pos.get(b)
                if pa is None or pb is None or (a, b) in seen:
                    continue
                seen.add((a, b)); seen.add((b, a))
                pen = QPen(QColor(P.accent)); pen.setWidthF(2.4); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen)
                p.drawPath(self._arc_path(pa, pb))
        if self._ov_flags.get("career") and ov.career_galaxy_lines:
            vmax, mc = ov.career_line_vmax, max(1, ov.career_max_count)
            for a, b, net, count in ov.career_galaxy_lines:
                pa, pb = pos.get(a), pos.get(b)
                if pa is None or pb is None:
                    continue
                pen = QPen(career_color(net, vmax)); pen.setWidthF(2.0 + 3.0 * (count / mc))
                pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                p.drawPath(self._arc_path(pa, pb))

    def _draw_node(self, p: QPainter, s: System, sx: float, sy: float, df: float) -> None:
        r = self._node_radius(df)
        col = QColor(self._affil_color(s))
        c = QPointF(sx, sy)

        # Soft glow bloom.
        glow_r = r * 4.2
        grad = QRadialGradient(c, glow_r)
        g0 = QColor(col); g0.setAlpha(int(60 + 70 * df)); grad.setColorAt(0.0, g0)
        g1 = QColor(col); g1.setAlpha(int(18 * df)); grad.setColorAt(0.4, g1)
        g2 = QColor(col); g2.setAlpha(0); grad.setColorAt(1.0, g2)
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(grad))
        p.drawEllipse(c, glow_r, glow_r)

        # Bright core.
        core = QColor(col).lighter(140)
        core.setAlpha(int(180 + 75 * df))
        p.setBrush(core)
        p.drawEllipse(c, r, r)

        # In-game emphasis ring.
        if s.in_game:
            pen = QPen(QColor(P.energy_cyan)); pen.setWidthF(1.6)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r + 3.5, r + 3.5)

        # Home marker.
        if s.code == self._home:
            pen = QPen(QColor(P.green)); pen.setWidthF(1.8)
            p.setPen(pen); p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r + 8.5, r + 8.5)
            fh = QFont(); fh.setPointSizeF(8.5); p.setFont(fh)
            p.setPen(QColor(P.green))
            p.drawText(QPointF(sx - 4.0, sy - r - 10.0), "⌂")   # ⌂

        # Route endpoints.
        if s.code == self._route_start:
            self._ring(p, c, r + 6.0, P.green, 2.0)
        if s.code == self._route_end:
            self._ring(p, c, r + 6.0, P.tool_trade, 2.0)

        # Selection / hover ring.
        if s.code == self._selected:
            self._ring(p, c, r + 6.0, P.tool_trade, 2.0)
        elif s.code == self._hovered:
            self._ring(p, c, r + 5.0, P.fg_bright, 1.2)

        # Activity overlay: report-recency ring (green fresh -> red stale).
        if self._ov_flags.get("activity") and self._overlay is not None:
            t = self._overlay.system_recency.get(s.code)
            if t is not None:
                pen = QPen(recency_color(t)); pen.setWidthF(2.6)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, r + 2.5, r + 2.5)

        # My Runs overlay: personal-profit heat ring (green gain -> red loss).
        if self._ov_flags.get("career") and self._overlay is not None and self._overlay.career_system:
            net = self._overlay.career_system.get(s.code)
            if net is not None:
                pen = QPen(career_color(net, self._overlay.career_vmax)); pen.setWidthF(3.2)
                p.setPen(pen); p.setBrush(Qt.NoBrush)
                p.drawEllipse(c, r + 6.0, r + 6.0)

    @staticmethod
    def _ring(p: QPainter, c: QPointF, rad: float, color: str, width: float) -> None:
        pen = QPen(QColor(color)); pen.setWidthF(width)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, rad, rad)

    def _draw_labels(self, p: QPainter, proj, dmin: float, drange: float) -> None:
        f = QFont(); f.setPointSizeF(10.5); f.setBold(True); p.setFont(f)
        fm = p.fontMetrics()
        for (s, sx, sy, d) in proj:
            shown = s.in_game or s.code in (
                self._hovered, self._selected, self._home,
                self._route_start, self._route_end)
            if not shown:
                continue
            df = (d - dmin) / drange
            col = QColor(P.fg_bright if s.in_game else P.fg)
            col.setAlpha(int(160 + 95 * df))
            p.setPen(col)
            tw = fm.horizontalAdvance(s.name)
            p.drawText(QPointF(sx - tw / 2.0, sy + self._node_radius(df) + 17.0), s.name)

    def _draw_hud(self, p: QPainter, w: int, h: int) -> None:
        f = QFont(); f.setPointSizeF(8.0); p.setFont(f)
        p.setPen(QColor(P.fg_dim))
        hint = ("click route points" if self._route_mode
                else "drag rotate · wheel zoom · right-drag pan · dbl-click enter · "
                     "right-click: lore")
        p.drawText(12, h - 13, hint)

        # Selected-system info + out-of-bounds warning.
        s = self._galaxy.get(self._selected)
        if s is not None:
            ft = QFont(); ft.setPointSizeF(11.0); ft.setBold(True); p.setFont(ft)
            p.setPen(QColor(P.tool_trade))
            p.drawText(14, 26, s.name)
            fs = QFont(); fs.setPointSizeF(8.0); p.setFont(fs)
            p.setPen(QColor(P.fg_dim))
            tag = "in-game — double-click to enter" if s.in_game else "lore system — double-click to look around"
            p.drawText(14, 43, f"{s.affiliation}   ·   {tag}   ·   right-click to see lore"
                               f"   ·   jumps: {len(s.jump_points)}")
            if not s.in_game:
                self._draw_warning(p, w, f"ⓘ  {s.name.upper()} — lore system · limited map detail")

        # Route summary.
        if self._route_start and self._route_end:
            fr = QFont(); fr.setPointSizeF(9.0); fr.setBold(True); p.setFont(fr)
            a = self._galaxy.get(self._route_start)
            b = self._galaxy.get(self._route_end)
            an = a.name if a else self._route_start
            bn = b.name if b else self._route_end
            if self._route and len(self._route) >= 2:
                jumps = len(self._route) - 1
                ly = self._galaxy.path_distance(self._route)
                txt = f"⤳  {an} → {bn}   ·   {jumps} jump{'s' if jumps != 1 else ''}   ·   {ly:.1f} ly"
                p.setPen(QColor(P.tool_trade))
            else:
                txt = f"⤳  {an} → {bn}   ·   no jump route"
                p.setPen(QColor(P.red))
            p.drawText(14, h - 34, txt)

    def _draw_warning(self, p: QPainter, w: int, text: str) -> None:
        fw = QFont(); fw.setPointSizeF(9.5); fw.setBold(True); p.setFont(fw)
        fm = p.fontMetrics()
        tw = fm.horizontalAdvance(text)
        bx = (w - tw) / 2.0 - 14
        bw = tw + 28
        p.setPen(Qt.NoPen)
        bg = QColor(P.yellow); bg.setAlpha(34)
        p.setBrush(bg)
        p.drawRoundedRect(int(bx), 10, int(bw), 26, 6, 6)
        p.setPen(QColor(P.yellow))
        p.drawText(int(bx + 14), 28, text)

    # ── interaction (drives render-on-demand) ───────────────────────────────
    def mousePressEvent(self, ev) -> None:
        self._last = ev.position()
        self._moved = 0.0
        if ev.button() == Qt.RightButton:
            self._mode = "pan"
        elif ev.button() == Qt.LeftButton:
            self._mode = "rotate"

    def mouseMoveEvent(self, ev) -> None:
        pt = ev.position()
        if self._mode and self._last is not None:
            dx = pt.x() - self._last.x()
            dy = pt.y() - self._last.y()
            self._last = pt
            self._moved += abs(dx) + abs(dy)
            if self._mode == "rotate":
                self._cam.rotate(dx * 0.008, -dy * 0.008)
            else:
                self._cam.pan_x += dx
                self._cam.pan_y += dy
            self.update()
            return
        code = self._hit_test(pt)
        if code != self._hovered:
            self._hovered = code
            if not self._route_mode:
                self.setCursor(Qt.PointingHandCursor if code else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, ev) -> None:
        was_click = self._mode == "rotate" and self._moved < 4.0
        was_right_click = self._mode == "pan" and self._moved < 4.0
        self._mode = None
        self._last = None
        if was_click:
            code = self._hit_test(ev.position())
            if code:
                if self._route_mode:
                    self._route_click(code)
                else:
                    self.select(code)
        elif was_right_click:                       # right-click a system -> lore
            code = self._hit_test(ev.position())
            s = self._galaxy.get(code) if code else None
            if s is not None:
                self.loreRequested.emit(f"{s.name} system", code)
        self.viewChanged.emit()

    def mouseDoubleClickEvent(self, ev) -> None:
        code = self._hit_test(ev.position())
        if not code:
            return
        self.select(code)
        # Enter ANY system: in-game ones have full body data; lore systems open a
        # sparse star-only view you can still pan/zoom/rotate to look around.
        self.systemEntered.emit(code)

    def wheelEvent(self, ev) -> None:
        d = ev.angleDelta().y()
        if not d:
            return
        pt = ev.position()
        w, h = self.width(), self.height()
        self._cam.zoom_at(1.0015 ** d, pt.x(), pt.y(), w / 2.0, h / 2.0, 0.15, 14.0)
        self.update()
        self.viewChanged.emit()
        if d > 0 and self._cam.zoom >= 5.5:        # zoom-in past threshold -> enter system
            code = self._focal_system(pt)
            if code:
                self._cam.zoom = 3.6               # land here when zooming back out
                self.drillIn.emit(code)

    # ── routing (M4) ─────────────────────────────────────────────────────────
    def start_route(self) -> None:
        self._route_mode = True
        self._route_start = None
        self._route_end = None
        self._route = None
        self.setCursor(Qt.CrossCursor)
        self.update()

    def clear_route(self) -> None:
        self._route_mode = False
        self._route_start = self._route_end = self._route = None
        self.setCursor(Qt.ArrowCursor)
        self.update()
        self.viewChanged.emit()

    def _route_click(self, code: str) -> None:
        if self._route_start is None:
            self._route_start = code
            self._route = None
            self.update()
        else:
            self.plot_route(self._route_start, code)
            self._route_mode = False
            self.setCursor(Qt.ArrowCursor)

    def plot_route(self, start: str, end: str) -> None:
        self._route_start = start
        self._route_end = end
        self._route = self._galaxy.shortest_path(start, end)
        self.update()
        self.viewChanged.emit()
        if self._route and len(self._route) >= 2:
            self.routePlotted.emit(start, end, len(self._route) - 1,
                                   self._galaxy.path_distance(self._route))

    # ── home (M3) ─────────────────────────────────────────────────────────────
    def set_home(self, code: Optional[str]) -> None:
        self._home = code
        self.update()
        self.viewChanged.emit()

    @property
    def home(self) -> Optional[str]:
        return self._home

    @property
    def route_active(self) -> bool:
        return bool(self._route or self._route_mode or self._route_start)

    def snap_to_home(self) -> None:
        s = self._galaxy.get(self._home)
        if s is None:
            return
        self._cam.fx, self._cam.fy, self._cam.fz = s.x, s.y, s.z
        self._cam.pan_x = self._cam.pan_y = 0.0
        self._cam.zoom = 1.0
        self._cam.yaw, self._cam.pitch = 0.6, 0.5
        self._fit_radius = self._compute_fit_radius()
        self.select(self._home)
        self.update()
        self.viewChanged.emit()

    def center_on(self, code: str) -> None:
        """Snap the camera onto a system (used by the search/snap-to feature)."""
        s = self._galaxy.get(code)
        if s is None:
            return
        self._cam.fx, self._cam.fy, self._cam.fz = s.x, s.y, s.z
        self._cam.pan_x = self._cam.pan_y = 0.0
        self._cam.zoom = max(self._cam.zoom, 2.4)
        self._fit_radius = self._compute_fit_radius()
        self.select(code)
        self.update()
        self.viewChanged.emit()

    # ── public API ──────────────────────────────────────────────────────────
    def select(self, code: Optional[str]) -> None:
        if code != self._selected:
            self._selected = code
            self.update()
            self.viewChanged.emit()
            if code:
                self.systemSelected.emit(code)

    def get_state(self) -> dict:
        c = self._cam
        return {
            "yaw": c.yaw, "pitch": c.pitch, "zoom": c.zoom,
            "pan_x": c.pan_x, "pan_y": c.pan_y,
            "focus": [c.fx, c.fy, c.fz],
            "selected": self._selected,
            "home": self._home,
            # The plotted jump route is intentionally NOT persisted — it's a
            # transient visualization, so the galaxy starts clean each launch
            # (a stale gold line greeting the user "by default" is confusing).
        }

    def apply_state(self, st: Optional[dict]) -> None:
        if not st:
            return
        c = self._cam
        c.yaw = float(st.get("yaw", c.yaw))
        c.pitch = float(st.get("pitch", c.pitch))
        c.zoom = float(st.get("zoom", c.zoom))
        c.pan_x = float(st.get("pan_x", 0.0))
        c.pan_y = float(st.get("pan_y", 0.0))
        f = st.get("focus")
        if isinstance(f, (list, tuple)) and len(f) == 3:
            c.fx, c.fy, c.fz = (float(v) for v in f)
            self._fit_radius = self._compute_fit_radius()
        self._selected = st.get("selected")
        self._home = st.get("home")
        # Do NOT restore a plotted route — start clean each launch (see get_state).
        self.update()
