"""Shared map-label rendering.

Draws a body/location name as a centred *name plate*: the text sits on a
translucent dark pill so names stay legible over the starfield, planets and
orbit rings — even at a distance / when small. Every map scene (system,
planet-neighbourhood, globe) uses this so labels look and behave the same.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QPointF, QRectF
from PySide6.QtGui import QColor, QFont, QPainter

from shared.qt.theme import P


def recency_color(t: float) -> QColor:
    """Report-freshness colour: green (just reported) → amber → red (stale).
    ``t`` in 0..1 where 1 = freshest."""
    t = max(0.0, min(1.0, t))
    return QColor(int(230 * (1.0 - t)), int(190 * t + 40), 70)


def career_color(net: float, vmax: float) -> QColor:
    """Personal-profit heat: green for net gain, red for net loss, brightness by
    magnitude vs ``vmax``. Neutral grey at zero / no scale."""
    if vmax <= 0 or net == 0:
        return QColor("#6a7686")
    t = min(1.0, abs(net) / vmax)
    if net > 0:
        return QColor.fromHsvF(0.34, 0.45 + 0.45 * t, 0.55 + 0.40 * t)
    return QColor.fromHsvF(0.01, 0.50 + 0.45 * t, 0.55 + 0.40 * t)


def draw_label(p: QPainter, sx: float, sy: float, r: float, name: str,
               color: QColor, occupied: List[QRectF], *,
               sub: Optional[str] = None, force: bool = False,
               big: bool = False) -> bool:
    """Draw a centred name plate under ``(sx, sy)`` for a body of icon radius ``r``.

    A translucent dark pill behind the text keeps it readable over any
    background; an optional ``sub`` line (e.g. ``"PLANET"``) sits beneath the
    name. Returns ``False`` without drawing when the plate would overlap an
    already-placed label and ``force`` is not set; otherwise records its box in
    ``occupied`` and returns ``True``.
    """
    f = QFont(); f.setPointSizeF(11.5 if big else 10.0); f.setBold(True)
    p.setFont(f)
    fm = p.fontMetrics()
    tw = fm.horizontalAdvance(name)
    asc, desc = fm.ascent(), fm.descent()
    x = sx - tw / 2.0
    y = sy + r + asc + 6.0
    pad_x, pad_y = 5.0, 2.5
    plate = QRectF(x - pad_x, y - asc - pad_y, tw + 2 * pad_x, asc + desc + 2 * pad_y)
    box = QRectF(plate)
    if sub:                                   # reserve the sub-label row for collisions
        box.setHeight(box.height() + 13.0)
    if not force:
        for o in occupied:
            if box.intersects(o):
                return False
    occupied.append(box)

    bg = QColor(6, 8, 12); bg.setAlpha(170)   # dark pill keeps text legible over anything
    p.setPen(Qt.NoPen); p.setBrush(bg)
    p.drawRoundedRect(plate, 4.0, 4.0)
    p.setPen(color)
    p.drawText(QPointF(x, y), name)
    if sub:
        fs = QFont(); fs.setPointSizeF(7.5); fs.setBold(True); p.setFont(fs)
        sw = p.fontMetrics().horizontalAdvance(sub)
        p.setPen(QColor(P.fg_dim))
        p.drawText(QPointF(sx - sw / 2.0, y + 12.0), sub)
    return True
