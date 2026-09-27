"""Aggregate a list of :class:`Session` objects into play-time statistics.

All time-of-day / per-day bucketing is done in **local time** (the player
cares when *they* were playing, not UTC), while the total duration is
timezone-independent.  Sessions that cross midnight are split at hour
boundaries so each day and each clock-hour gets exactly its share.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from .log_scanner import Session

WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTH_NAMES = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def iter_hour_segments(start: datetime, end: datetime):
    """Yield (segment_start, seconds) for each clock-hour *start*..*end* spans."""
    cur = start
    while cur < end:
        nxt = cur.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        seg_end = min(nxt, end)
        yield cur, (seg_end - cur).total_seconds()
        cur = seg_end


def channel_seconds_by_day(sessions) -> dict:
    """-> {local date: {channel: seconds}}, split at midnight exactly as `by_day` is.

    ⛔ 2026-09-26. The calendar's day panel showed `by_day[d]` as the day TOTAL — correctly
      split at hour boundaries — beside a per-channel line summed from whole
      `s.duration_seconds` for every session whose START date was `d`. Those are different
      questions, and on a long overnight session they disagree enormously: a day total of
      about 4 h could sit next to a channel line reading about 48 h, because a session that
      began at 22:00 and ran 47 h contributed two hours to the total and all forty-seven to
      the channel row. 98 of 1113 sessions in the reference data cross local midnight.
    ★ IT IS BUILT ON `iter_hour_segments`, THE SAME SPLITTER `build_analytics` USES, and that
      is the point rather than a convenience: a second implementation of "which day does this
      time belong to" would be free to drift from the total it is printed next to, and the
      only symptom would be two plausible numbers that no longer add up. Sharing the splitter
      makes agreement structural instead of tested.
    ⚠ WHAT IT IS NOT: a session count. A session spanning midnight appears under BOTH dates
      here, with its seconds divided between them. Summing the lengths of these lists would
      double-count it, which is exactly the error this function exists to undo.
    """
    out: dict = {}
    for s in sessions:
        for seg_start, secs in iter_hour_segments(s.start_local, s.end_local):
            day = out.setdefault(seg_start.date(), {})
            day[s.channel] = day.get(s.channel, 0.0) + secs
    return out


@dataclass
class Highlights:
    longest_session: Optional[Session] = None
    most_played_day: Optional[tuple[date, float]] = None
    most_played_week: Optional[tuple[date, float]] = None     # (week-start Monday, secs)
    most_played_month: Optional[tuple[tuple[int, int], float]] = None  # ((y,m), secs)
    most_played_year: Optional[tuple[int, float]] = None
    busiest_hour: Optional[tuple[int, float]] = None          # (0-23, secs)
    busiest_weekday: Optional[tuple[int, float]] = None       # (0-6, secs)
    first_session: Optional[datetime] = None
    last_session: Optional[datetime] = None
    active_days: int = 0
    avg_session: float = 0.0
    avg_per_active_day: float = 0.0
    longest_streak: int = 0
    current_streak: int = 0


@dataclass
class Analytics:
    total_seconds: float = 0.0
    session_count: int = 0
    by_day: dict[date, float] = field(default_factory=dict)
    by_hour: list[float] = field(default_factory=lambda: [0.0] * 24)
    by_weekday: list[float] = field(default_factory=lambda: [0.0] * 7)
    by_week: dict[date, float] = field(default_factory=dict)        # key = Monday
    by_month: dict[tuple[int, int], float] = field(default_factory=dict)
    by_year: dict[int, float] = field(default_factory=dict)
    by_channel: dict[str, float] = field(default_factory=dict)
    by_build: dict[str, float] = field(default_factory=dict)
    highlights: Highlights = field(default_factory=Highlights)

    @property
    def is_empty(self) -> bool:
        return self.session_count == 0

    # ── derived, ordered views for charts ──

    def day_series(self) -> list[tuple[date, float]]:
        return sorted(self.by_day.items())

    def week_series(self) -> list[tuple[date, float]]:
        return sorted(self.by_week.items())

    def month_series(self) -> list[tuple[tuple[int, int], float]]:
        return sorted(self.by_month.items())

    def year_series(self) -> list[tuple[int, float]]:
        return sorted(self.by_year.items())


def _longest_and_current_streak(days: Iterable[date]) -> tuple[int, int]:
    ds = sorted(set(days))
    if not ds:
        return 0, 0
    longest = run = 1
    for prev, cur in zip(ds, ds[1:]):
        if (cur - prev).days == 1:
            run += 1
        else:
            run = 1
        longest = max(longest, run)
    # Current streak = consecutive days ending at the most recent played day.
    current = 1
    i = len(ds) - 1
    while i > 0 and (ds[i] - ds[i - 1]).days == 1:
        current += 1
        i -= 1
    return longest, current


def build_analytics(sessions: list[Session]) -> Analytics:
    a = Analytics()
    if not sessions:
        return a

    a.session_count = len(sessions)
    for s in sessions:
        a.total_seconds += s.duration_seconds
        if s.channel:
            a.by_channel[s.channel] = a.by_channel.get(s.channel, 0.0) + s.duration_seconds
        if s.build:
            a.by_build[s.build] = a.by_build.get(s.build, 0.0) + s.duration_seconds

        # Split across hour boundaries in local time.
        for seg_start, secs in iter_hour_segments(s.start_local, s.end_local):
            a.by_hour[seg_start.hour] += secs
            d = seg_start.date()
            a.by_day[d] = a.by_day.get(d, 0.0) + secs

    # Roll per-day totals up into weekday / week / month / year.
    for d, secs in a.by_day.items():
        a.by_weekday[d.weekday()] += secs
        monday = d - timedelta(days=d.weekday())
        a.by_week[monday] = a.by_week.get(monday, 0.0) + secs
        ym = (d.year, d.month)
        a.by_month[ym] = a.by_month.get(ym, 0.0) + secs
        a.by_year[d.year] = a.by_year.get(d.year, 0.0) + secs

    # ── Highlights ──
    h = a.highlights
    h.longest_session = max(sessions, key=lambda s: s.duration_seconds)
    if a.by_day:
        h.most_played_day = max(a.by_day.items(), key=lambda kv: kv[1])
    if a.by_week:
        h.most_played_week = max(a.by_week.items(), key=lambda kv: kv[1])
    if a.by_month:
        h.most_played_month = max(a.by_month.items(), key=lambda kv: kv[1])
    if a.by_year:
        h.most_played_year = max(a.by_year.items(), key=lambda kv: kv[1])
    if any(a.by_hour):
        bh = max(range(24), key=lambda i: a.by_hour[i])
        h.busiest_hour = (bh, a.by_hour[bh])
    if any(a.by_weekday):
        bw = max(range(7), key=lambda i: a.by_weekday[i])
        h.busiest_weekday = (bw, a.by_weekday[bw])

    h.first_session = min(s.start_local for s in sessions)
    h.last_session = max(s.end_local for s in sessions)
    h.active_days = len(a.by_day)
    h.avg_session = a.total_seconds / a.session_count if a.session_count else 0.0
    h.avg_per_active_day = a.total_seconds / h.active_days if h.active_days else 0.0
    h.longest_streak, h.current_streak = _longest_and_current_streak(a.by_day.keys())

    return a


# ── Cross-process summary (for the launcher main menu) ───────────────────────

def build_summary(a: Analytics, time_format: str, sc_folder: str) -> dict:
    """Serialise the headline numbers the launcher needs."""
    from . import formatting as fmt
    h = a.highlights
    return {
        "total_seconds": a.total_seconds,
        "session_count": a.session_count,
        "active_days": h.active_days,
        "time_format": time_format,
        "badge": fmt.format_badge(a.total_seconds, time_format),
        "headline": fmt.format_total(a.total_seconds, time_format),
        "hours": fmt.fmt_hours(a.total_seconds),
        "calendar": fmt.fmt_calendar_long(a.total_seconds),
        "sc_folder": sc_folder,
        "last_played": h.last_session.isoformat() if h.last_session else "",
    }
