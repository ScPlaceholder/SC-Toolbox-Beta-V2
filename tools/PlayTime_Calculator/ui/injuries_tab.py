"""Injuries tab — where you get hurt, and how badly, from the Game.log injury feed.

Shares the window's full-content scan with Fun Stats + Career (the per-file
parse lives in :mod:`core.injuries` and is cached with the rest), so opening
this tab costs nothing extra once either of those has run.

Layout: headline cards, a front-facing body diagram shaded by how often each
part was hit (hover for the per-tier split), a per-part chart stacked by tier,
and injuries per week.  Tiers count DOWN: Tier 1 is the most severe.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QRectF, QPointF
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QWidget, QLabel, QGridLayout, QHBoxLayout, QSizePolicy, QFrame, QVBoxLayout,
)

from shared.qt.theme import P
from core import injuries as inj
from core.injuries import InjuryStats, PARTS, TIERS, TIER_LABELS, part_label
from ui.charts import BarChart, Bar
from ui.fun_stats_tab import _ScanTab
from ui.stat_widgets import stat_card, section_label, facts_box, count_fmt, ACCENT, GOLD, GREEN

# Tier colours: severity reads hot → cool.  Frequency on the body uses the
# app's usual cyan ramp so red keeps meaning "severe" everywhere on the page.
TIER_COLORS = {1: P.red, 2: P.yellow, 3: ACCENT}


def _tier_name(t: int) -> str:
    return f"Tier {t} · {TIER_LABELS.get(t, '?')}"


def _ramp(t: float) -> QColor:
    """Dim slate → accent cyan by normalised frequency ``t`` in [0,1]."""
    lo, hi = QColor(P.bg_input), QColor(ACCENT)
    t = max(0.0, min(1.0, t))
    return QColor(int(lo.red() + (hi.red() - lo.red()) * t),
                  int(lo.green() + (hi.green() - lo.green()) * t),
                  int(lo.blue() + (hi.blue() - lo.blue()) * t))


def _paint_tip(p: QPainter, anchor: QPointF, bounds: QRectF, lines: list[tuple[str, str]]) -> None:
    """Draw a BarChart-style tooltip box: [(text, colour), ...], first line is the title."""
    p.setFont(QFont("Consolas", 8, QFont.Bold))
    fm = QFontMetrics(p.font())
    tw = max(fm.horizontalAdvance(t) for t, _ in lines) + 14
    th = len(lines) * fm.height() + 8
    x = min(max(bounds.left(), anchor.x() + 12), bounds.right() - tw)
    y = min(max(bounds.top(), anchor.y() - th / 2), bounds.bottom() - th)
    box = QRectF(x, y, tw, th)
    bg = QColor(P.bg_header)
    bg.setAlpha(245)
    p.fillRect(box, bg)
    p.setPen(QPen(QColor(P.accent), 1))
    p.drawRect(box)
    yy = box.top() + 4
    for text, col in lines:
        p.setPen(QColor(col))
        p.drawText(QRectF(box.left() + 7, yy, tw - 12, fm.height()),
                   Qt.AlignLeft | Qt.AlignVCenter, text)
        yy += fm.height()


# ══════════════════════════════════════════════════════════════════════════════
# Body diagram
# ══════════════════════════════════════════════════════════════════════════════

# Part shapes in a 200 × 400 design space, figure facing the viewer — so the
# player's LEFT side is drawn on the viewer's RIGHT.
_W, _H = 200.0, 400.0


def _part_paths() -> dict[str, QPainterPath]:
    def rr(x, y, w, h, r):
        path = QPainterPath()
        path.addRoundedRect(QRectF(x, y, w, h), r, r)
        return path

    head = QPainterPath()
    head.addEllipse(QRectF(77, 8, 46, 56))
    torso = QPainterPath()
    torso.moveTo(92, 66)            # neck
    torso.lineTo(108, 66)
    torso.lineTo(109, 76)
    torso.quadTo(132, 78, 134, 92)  # right shoulder (viewer)
    torso.lineTo(130, 150)
    torso.quadTo(128, 190, 132, 222)
    torso.lineTo(68, 222)
    torso.quadTo(72, 190, 70, 150)
    torso.lineTo(66, 92)
    torso.quadTo(68, 78, 91, 76)
    torso.closeSubpath()
    return {
        "head": head,
        "torso": torso,
        "right_arm": rr(42, 84, 22, 140, 10),   # viewer's left
        "left_arm": rr(136, 84, 22, 140, 10),   # viewer's right
        "right_leg": rr(70, 226, 28, 164, 11),
        "left_leg": rr(102, 226, 28, 164, 11),
    }


_PATHS = _part_paths()


class BodyDiagram(QWidget):
    """Front-facing silhouette, each part shaded by injury count."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._stats = InjuryStats()
        self._hover: Optional[str] = None
        self._hover_pos = QPointF()
        self.setMouseTracking(True)
        self.setMinimumSize(260, 360)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)

    def set_stats(self, stats: InjuryStats) -> None:
        self._stats = stats
        self._hover = None
        self.update()

    def set_hover(self, part: Optional[str]) -> None:
        """Programmatic hover (used for screenshots / tests)."""
        self._hover = part
        if part in _PATHS:
            c = _PATHS[part].boundingRect().center()
            self._hover_pos = self._to_widget(c)
        self.update()

    # ── geometry ──

    def _figure_rect(self) -> QRectF:
        pad_top, pad_bot = 22, 34
        avail_h = max(1.0, self.height() - pad_top - pad_bot)
        scale = min(avail_h / _H, max(1.0, self.width() - 20) / _W)
        w, h = _W * scale, _H * scale
        return QRectF((self.width() - w) / 2, pad_top, w, h)

    def _scale(self) -> float:
        return self._figure_rect().width() / _W

    def _to_widget(self, pt: QPointF) -> QPointF:
        fr = self._figure_rect()
        s = self._scale()
        return QPointF(fr.left() + pt.x() * s, fr.top() + pt.y() * s)

    def _part_at(self, pos: QPointF) -> Optional[str]:
        fr = self._figure_rect()
        s = self._scale()
        if s <= 0:
            return None
        local = QPointF((pos.x() - fr.left()) / s, (pos.y() - fr.top()) / s)
        for key, path in _PATHS.items():
            if path.contains(local):
                return key
        return None

    # ── interaction ──

    def mouseMoveEvent(self, ev):
        pos = ev.position()
        part = self._part_at(pos)
        if part != self._hover or part:
            self._hover = part
            self._hover_pos = pos
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(ev)

    # ── painting ──

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_primary))

        st = self._stats
        mx = max((st.by_part.get(k, 0) for k in PARTS), default=0)
        fr = self._figure_rect()
        s = self._scale()

        # Side markers (front view: player's right is on the viewer's left).
        p.setFont(QFont("Consolas", 7, QFont.Bold))
        p.setPen(QColor(P.fg_dim))
        p.drawText(QRectF(fr.left(), 2, 40, 16), Qt.AlignLeft | Qt.AlignVCenter, "R")
        p.drawText(QRectF(fr.right() - 40, 2, 40, 16), Qt.AlignRight | Qt.AlignVCenter, "L")
        p.drawText(QRectF(0, 2, self.width(), 16), Qt.AlignCenter, "FRONT VIEW")

        p.save()
        p.translate(fr.left(), fr.top())
        p.scale(s, s)
        for key, path in _PATHS.items():
            n = st.by_part.get(key, 0)
            fill = _ramp(n / mx) if (mx and n) else QColor(P.bg_card)
            if key == self._hover:
                fill = fill.lighter(135)
            p.setBrush(fill)
            pen = QPen(QColor(ACCENT if key == self._hover else P.border_card))
            pen.setWidthF(1.6 / max(s, 0.01))
            p.setPen(pen)
            p.drawPath(path)
        p.restore()

        # Counts on each part.
        font = QFont("Electrolize", max(8, int(11 * s)), QFont.Bold)
        p.setFont(font)
        for key, path in _PATHS.items():
            n = st.by_part.get(key, 0)
            c = self._to_widget(path.boundingRect().center())
            t = n / mx if mx else 0
            p.setPen(QColor(P.bg_deepest) if t > 0.55 else QColor(P.fg_bright if n else P.fg_dim))
            p.drawText(QRectF(c.x() - 30, c.y() - 12, 60, 24), Qt.AlignCenter, f"{n:,}")

        # Frequency legend.
        lg_w = min(160.0, self.width() - 40.0)
        lx = (self.width() - lg_w) / 2
        ly = self.height() - 22
        for i in range(int(lg_w)):
            p.setPen(_ramp(i / max(1.0, lg_w - 1)))
            p.drawLine(QPointF(lx + i, ly), QPointF(lx + i, ly + 6))
        p.setFont(QFont("Consolas", 7))
        p.setPen(QColor(P.fg_dim))
        p.drawText(QRectF(lx - 44, ly - 4, 40, 14), Qt.AlignRight | Qt.AlignVCenter, "fewer")
        p.drawText(QRectF(lx + lg_w + 4, ly - 4, 50, 14), Qt.AlignLeft | Qt.AlignVCenter,
                   f"more ({mx:,})" if mx else "more")

        if self._hover:
            self._paint_hover(p)
        p.end()

    def _paint_hover(self, p: QPainter) -> None:
        key = self._hover
        st = self._stats
        n = st.by_part.get(key, 0)
        pct = (n / st.total * 100.0) if st.total else 0.0
        lines = [(f"{part_label(key)} — {n:,} injur{'y' if n == 1 else 'ies'}", P.accent)]
        if st.total:
            lines.append((f"{pct:.0f}% of all injuries", P.fg_dim))
        tiers = st.by_part_tier.get(key, {})
        for t in TIERS:
            lines.append((f"{_tier_name(t)}: {tiers.get(t, 0):,}", TIER_COLORS[t]))
        healed = st.parts_healed.get(key, 0)
        if healed:
            lines.append((f"Med bed surgeries: {healed:,}", GREEN))
        _paint_tip(p, self._hover_pos, QRectF(self.rect()).adjusted(2, 2, -2, -2), lines)


# ══════════════════════════════════════════════════════════════════════════════
# Stacked bar chart: injuries per body part, split by tier
# ══════════════════════════════════════════════════════════════════════════════

class TierStackChart(QWidget):
    """One bar per body part, stacked Tier 1 (bottom) → Tier 3 (top)."""

    _PAD_L, _PAD_R, _PAD_T, _PAD_B = 40, 12, 26, 30

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._stats = InjuryStats()
        self._parts: list[str] = list(PARTS)
        self._hover = -1
        self._hover_pos = QPointF()
        self.setMouseTracking(True)
        self.setMinimumHeight(240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_stats(self, stats: InjuryStats) -> None:
        self._stats = stats
        self._parts = [k for k in stats.parts_in_order()]
        self._hover = -1
        self.update()

    def set_hover(self, idx: int) -> None:
        self._hover = idx
        r = self._bar_rect(idx) if idx >= 0 else None
        if r is not None:
            self._hover_pos = QPointF(r.center().x(), r.top())
        self.update()

    def _plot(self) -> QRectF:
        return QRectF(self._PAD_L, self._PAD_T,
                      max(1, self.width() - self._PAD_L - self._PAD_R),
                      max(1, self.height() - self._PAD_T - self._PAD_B))

    def _max(self) -> int:
        return max((self._stats.by_part.get(k, 0) for k in self._parts), default=0)

    def _bar_rect(self, i: int) -> Optional[QRectF]:
        mx = self._max()
        if not self._parts or mx <= 0 or not (0 <= i < len(self._parts)):
            return None
        pr = self._plot()
        slot = pr.width() / len(self._parts)
        bw = min(56.0, slot * 0.62)
        h = self._stats.by_part.get(self._parts[i], 0) / mx * pr.height()
        x = pr.left() + i * slot + (slot - bw) / 2
        return QRectF(x, pr.bottom() - h, bw, h)

    def mouseMoveEvent(self, ev):
        pr = self._plot()
        x = ev.position().x()
        idx = -1
        if self._parts and pr.left() <= x <= pr.right():
            idx = int((x - pr.left()) // (pr.width() / len(self._parts)))
            if not (0 <= idx < len(self._parts)):
                idx = -1
        if idx != self._hover:
            self._hover = idx
            self._hover_pos = ev.position()
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):
        if self._hover != -1:
            self._hover = -1
            self.update()
        super().leaveEvent(ev)

    def paintEvent(self, ev):
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing, True)
        p.fillRect(self.rect(), QColor(P.bg_primary))
        mx = self._max()
        pr = self._plot()
        if mx <= 0:
            p.setPen(QColor(P.fg_dim))
            p.setFont(QFont("Consolas", 10))
            p.drawText(self.rect(), Qt.AlignCenter, "No data for this view.")
            p.end()
            return

        # Legend (top-right).
        p.setFont(QFont("Consolas", 7, QFont.Bold))
        fm = QFontMetrics(p.font())
        x = pr.right()
        for t in reversed(TIERS):
            label = _tier_name(t)
            w = fm.horizontalAdvance(label)
            x -= w
            p.setPen(QColor(P.fg))
            p.drawText(QRectF(x, 4, w, 14), Qt.AlignLeft | Qt.AlignVCenter, label)
            x -= 12
            p.fillRect(QRectF(x, 7, 8, 8), QColor(TIER_COLORS[t]))
            x -= 14

        # Gridlines.
        p.setFont(QFont("Consolas", 7))
        for frac in (0.0, 0.25, 0.5, 0.75, 1.0):
            y = pr.bottom() - frac * pr.height()
            p.setPen(QPen(QColor(P.border), 1))
            p.drawLine(QPointF(pr.left(), y), QPointF(pr.right(), y))
            p.setPen(QColor(P.fg_dim))
            p.drawText(QRectF(0, y - 7, self._PAD_L - 6, 14),
                       Qt.AlignRight | Qt.AlignVCenter, count_fmt(mx * frac))

        # Stacked bars.
        slot = pr.width() / len(self._parts)
        for i, key in enumerate(self._parts):
            r = self._bar_rect(i)
            tiers = self._stats.by_part_tier.get(key, {})
            if r is not None and r.height() > 0:
                y = r.bottom()
                total = self._stats.by_part.get(key, 0)
                for t in TIERS:
                    n = tiers.get(t, 0)
                    if not n:
                        continue
                    h = r.height() * n / total
                    col = QColor(TIER_COLORS[t])
                    if i == self._hover:
                        col = col.lighter(130)
                    p.fillRect(QRectF(r.left(), y - h, r.width(), h), col)
                    y -= h
                p.setPen(QColor(P.fg_bright))
                p.setFont(QFont("Consolas", 8, QFont.Bold))
                p.drawText(QRectF(r.left() - 10, r.top() - 15, r.width() + 20, 14),
                           Qt.AlignHCenter | Qt.AlignBottom, f"{total:,}")
            else:
                p.fillRect(QRectF(pr.left() + i * slot + slot * 0.19, pr.bottom() - 1,
                                  slot * 0.62, 1), QColor(P.border))
            p.setPen(QColor(P.fg_dim))
            p.setFont(QFont("Consolas", 7))
            p.drawText(QRectF(pr.left() + i * slot, pr.bottom() + 4, slot, 14),
                       Qt.AlignHCenter | Qt.AlignTop, part_label(key))

        if 0 <= self._hover < len(self._parts):
            key = self._parts[self._hover]
            tiers = self._stats.by_part_tier.get(key, {})
            lines = [(f"{part_label(key)} — {self._stats.by_part.get(key, 0):,}", P.accent)]
            lines += [(f"{_tier_name(t)}: {tiers.get(t, 0):,}", TIER_COLORS[t]) for t in TIERS]
            _paint_tip(p, self._hover_pos, QRectF(self.rect()).adjusted(2, 2, -2, -2), lines)
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
# The tab
# ══════════════════════════════════════════════════════════════════════════════

def _card_frame() -> QFrame:
    f = QFrame()
    f.setStyleSheet(f"QFrame {{ background: {P.bg_primary}; border: 1px solid {P.border_card}; }}")
    return f


class InjuriesTab(_ScanTab):
    def __init__(self, on_request_scan, parent=None) -> None:
        super().__init__("Injuries", on_request_scan, parent)
        self._sessions: list = []
        self._have_stats = False
        self.body: Optional[BodyDiagram] = None
        self.stack: Optional[TierStackChart] = None

    def set_stats(self, fs) -> None:
        self._have_stats = True
        super().set_stats(fs)
        n = fs.injuries.sessions_with_injuries
        self._status.setText(f"{fs.sessions_scanned:,} sessions analyzed · "
                             f"injuries in {n:,}")

    def set_sessions(self, sessions: list) -> None:
        """Play sessions (for the per-hour rate).  Re-renders if stats are in."""
        self._sessions = list(sessions or [])
        if self._have_stats:
            self._render()

    def _render(self) -> None:
        self._clear()
        self.body = self.stack = None
        st: InjuryStats = self._fs.injuries
        if st.is_empty:
            lbl = QLabel("No injuries logged yet.")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(f"color: {P.fg_dim}; background: transparent;"
                              f" font-family: Consolas; font-size: 10pt;")
            self._cl.addWidget(lbl)
            note = QLabel("Injury notifications only appear in Game.logs from late-2025 "
                          "builds onward. Take a hit and they'll show up here.")
            note.setAlignment(Qt.AlignCenter)
            note.setWordWrap(True)
            note.setStyleSheet(f"color: {P.fg_dim}; background: transparent;"
                               f" font-family: Consolas; font-size: 8pt;")
            self._cl.addWidget(note)
            self._cl.addStretch(1)
            return

        # ── Headline cards ──
        top = st.most_hit
        rate = inj.injuries_per_hour(st, self._sessions)
        since = f"{st.first:%b %Y}" if st.first else ""
        grid = QGridLayout()
        grid.setSpacing(8)
        cards = [
            ("TOTAL INJURIES", f"{st.total:,}",
             f"across {st.sessions_with_injuries:,} sessions", ACCENT),
            ("MOST-HIT PART", part_label(top[0]) if top else "—",
             f"{top[1]:,} injuries · {top[1] / st.total * 100:.0f}%" if top else "", GOLD),
            ("SEVERE (TIER 1)", f"{st.severe:,}",
             f"{st.severe / st.total * 100:.0f}% of all injuries", P.red),
            ("PER HOUR PLAYED", f"{rate:.2f}" if rate is not None else "—",
             f"injuries/hr since {since}" if rate is not None
             else "play time not loaded yet", GREEN),
        ]
        for i, (t, v, s, c) in enumerate(cards):
            grid.addWidget(stat_card(t, v, s, c), 0, i)
            grid.setColumnStretch(i, 1)
        self._cl.addLayout(grid)

        # ── Body diagram + per-part tier chart ──
        self._cl.addWidget(section_label("Where You Get Hurt  ·  hover a part for the tier split"))
        split = QHBoxLayout()
        split.setSpacing(10)
        body_box = _card_frame()
        bl = QVBoxLayout(body_box)
        bl.setContentsMargins(6, 6, 6, 6)
        self.body = BodyDiagram()
        self.body.set_stats(st)
        self.body.setMinimumHeight(430)
        bl.addWidget(self.body)
        split.addWidget(body_box, 2)

        stack_box = _card_frame()
        sl = QVBoxLayout(stack_box)
        sl.setContentsMargins(6, 6, 6, 6)
        cap = QLabel("Injuries by body part  ·  stacked by tier (Tier 1 = most severe)")
        cap.setStyleSheet(f"font-family: Consolas; font-size: 8pt; color: {P.fg_dim};"
                          f" background: transparent; border: none;")
        sl.addWidget(cap)
        self.stack = TierStackChart()
        self.stack.set_stats(st)
        sl.addWidget(self.stack, 1)
        split.addWidget(stack_box, 3)
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        wrap.setLayout(split)
        wrap.setMinimumHeight(450)
        self._cl.addWidget(wrap)

        # ── Over time ──
        weeks = st.week_series()
        if weeks:
            self._cl.addWidget(section_label("Injuries per Week"))
            best = max(n for _, n in weeks)
            chart = BarChart(fit_width=True, value_fmt=lambda v: f"{int(v):,} injuries",
                             axis_fmt=count_fmt)
            chart.setMinimumHeight(150)
            chart.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            chart.set_bars([Bar(label=f"{d:%b %d}", value=float(n), key=d.isoformat(),
                                accent=(n == best and n > 0)) for d, n in weeks])
            self._cl.addWidget(chart)

        # ── Facts ──
        facts: list[str] = []
        tier_bits = " · ".join(
            f"<span style='color:{TIER_COLORS[t]}'>{_tier_name(t)}: <b>{st.by_tier.get(t, 0):,}</b></span>"
            for t in TIERS)
        facts.append(tier_bits)
        if st.surgeries:
            healed = st.parts_healed.most_common(1)
            extra = f" — most treated: <b>{part_label(healed[0][0])}</b>" if healed else ""
            facts.append(f"\U0001f6cf️ <b>{st.surgeries:,}</b> med bed surgeries{extra}")
        if st.first and st.last:
            facts.append(f"\U0001f4c5 First logged injury <b>{st.first:%d %b %Y}</b>, "
                         f"latest <b>{st.last:%d %b %Y}</b>")
        facts.append(f"<span style='color:{P.fg_dim}'>ℹ️ Counts one per HUD injury "
                     f"notification. The game only started logging these in late-2025 "
                     f"builds; med bed surgeries go back further.</span>")
        self._cl.addWidget(facts_box("Injury Report", facts, accent=GOLD))
        self._cl.addStretch(1)
