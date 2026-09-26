"""Injury stats — mined from the HUD notifications Star Citizen writes to Game.log.

Verified against real logs (builds from Dec 2025 onward).  Each injury the
player takes raises one HUD notification, logged ONCE as it is queued::

    <2026-09-08T01:17:05.579Z> [Notice] <SHUDEvent_OnNotification> Added notification
        "Severe Injury Detected - Head - Tier 1 Treatment Required : " [51] to queue. ...

The same notification then echoes several more times — the queue dump lines
(``<ts>    "Severe Injury Detected - ..." [51]``) and ``<UpdateNotificationItem>``
Next / StartFade / Remove lines — so only the ``Added notification`` line is
counted.  Counting every line containing "Injury Detected" inflates the total
roughly five-fold.  Some builds prefix the tag with ``[SPAM <n>]``.

Tiers count DOWN in severity: Tier 1 = Severe, Tier 2 = Moderate, Tier 3 = Minor.
Body parts seen in the logs: Head, Torso, Left arm, Right arm, Left leg, Right leg.

Med bed surgeries are a separate, older event (present since 2024 builds)::

    <MED BED HEAL> ... Perform surgery event Success, med bed name: ..., head: false
        torso: true leftArm: false rightArm: false leftLeg: false rightLeg: false

This module is pure Python (no Qt) so it can be unit-tested and reused by the
full-content scan in :mod:`core.fun_stats`.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

# Display order for the six body parts the game reports.
PARTS = ("head", "torso", "left_arm", "right_arm", "left_leg", "right_leg")
PART_LABELS = {
    "head": "Head", "torso": "Torso",
    "left_arm": "Left Arm", "right_arm": "Right Arm",
    "left_leg": "Left Leg", "right_leg": "Right Leg",
}
TIERS = (1, 2, 3)
TIER_LABELS = {1: "Severe", 2: "Moderate", 3: "Minor"}

_INJURY_RE = re.compile(
    r"<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z>[^\n]*?"
    r"<SHUDEvent_OnNotification> Added notification \""
    r"(\w+) Injury Detected - ([^\"\n]+?) - Tier (\d+)")

_SURGERY_RE = re.compile(
    r"<(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?)Z>[^\n]*?"
    r"<MED BED HEAL>[^\n]*?Perform surgery event Success([^\n]*)")
# Log key in the surgery line -> our part key.
_SURGERY_PARTS = (("head", "head"), ("torso", "torso"),
                  ("leftArm", "left_arm"), ("rightArm", "right_arm"),
                  ("leftLeg", "left_leg"), ("rightLeg", "right_leg"))


def normalise_part(raw: str) -> str:
    """'Left arm' -> 'left_arm'.  Unknown parts are normalised the same way."""
    return re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")


def part_label(key: str) -> str:
    return PART_LABELS.get(key, key.replace("_", " ").title())


def parse_injury_line(line: str) -> Optional[tuple[str, str, int]]:
    """Return (utc_iso_ts, part_key, tier) for an injury ``Added notification``
    line, else None.  Echo lines (queue dumps, UpdateNotificationItem) → None."""
    m = _INJURY_RE.search(line)
    if not m:
        return None
    return m.group(1), normalise_part(m.group(3)), int(m.group(4))


def parse_surgery_line(line: str) -> Optional[tuple[str, list[str]]]:
    """Return (utc_iso_ts, [healed part keys]) for a successful med bed surgery."""
    m = _SURGERY_RE.search(line)
    if not m:
        return None
    tail = m.group(2)
    healed = [key for log_key, key in _SURGERY_PARTS if f"{log_key}: true" in tail]
    return m.group(1), healed


def scan_text(text: str) -> dict:
    """Extract injuries + surgeries from a whole log file's text.

    Returns a JSON-safe dict for the per-file cache::

        {"inj": [[ts, part, tier], ...], "surg": [[ts, [parts...]], ...]}
    """
    inj: list[list] = []
    surg: list[list] = []
    if "Injury Detected" in text:
        for m in _INJURY_RE.finditer(text):
            inj.append([m.group(1), normalise_part(m.group(3)), int(m.group(4))])
    if "<MED BED HEAL>" in text:
        for m in _SURGERY_RE.finditer(text):
            tail = m.group(2)
            surg.append([m.group(1),
                         [key for lk, key in _SURGERY_PARTS if f"{lk}: true" in tail]])
    return {"inj": inj, "surg": surg}


def _local(ts: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc).astimezone()
    except ValueError:
        return None


@dataclass
class InjuryStats:
    total: int = 0
    by_part: Counter = field(default_factory=Counter)                 # part -> count
    by_tier: Counter = field(default_factory=Counter)                 # tier -> count
    by_part_tier: dict[str, Counter] = field(default_factory=dict)    # part -> tier -> count
    by_week: dict[date, int] = field(default_factory=dict)            # Monday (local) -> count
    sessions_with_injuries: int = 0
    first: Optional[datetime] = None                                  # local time
    last: Optional[datetime] = None
    surgeries: int = 0
    parts_healed: Counter = field(default_factory=Counter)

    @property
    def is_empty(self) -> bool:
        return self.total == 0

    @property
    def most_hit(self) -> Optional[tuple[str, int]]:
        if not self.by_part:
            return None
        # Ties break by display order so the result is deterministic.
        order = {p: i for i, p in enumerate(PARTS)}
        return min(self.by_part.items(), key=lambda kv: (-kv[1], order.get(kv[0], 99), kv[0]))

    @property
    def severe(self) -> int:
        return self.by_tier.get(1, 0)

    def week_series(self) -> list[tuple[date, int]]:
        """Every week from the first to the last injury, zero weeks included,
        so the time axis is honest about quiet stretches."""
        if not self.by_week:
            return []
        start, end = min(self.by_week), max(self.by_week)
        out = []
        d = start
        while d <= end:
            out.append((d, self.by_week.get(d, 0)))
            d += timedelta(days=7)
        return out

    def parts_in_order(self) -> list[str]:
        """Known parts in display order, then any unexpected ones the log used."""
        extra = sorted(p for p in self.by_part if p not in PARTS)
        return list(PARTS) + extra


def aggregate(records: Iterable[dict]) -> InjuryStats:
    """Combine per-file ``scan_text`` results into one :class:`InjuryStats`."""
    s = InjuryStats()
    for rec in records:
        inj = (rec or {}).get("inj") or []
        if inj:
            s.sessions_with_injuries += 1
        for ts, part, tier in inj:
            s.total += 1
            s.by_part[part] += 1
            s.by_tier[int(tier)] += 1
            s.by_part_tier.setdefault(part, Counter())[int(tier)] += 1
            dt = _local(ts)
            if dt is None:
                continue
            monday = dt.date() - timedelta(days=dt.weekday())
            s.by_week[monday] = s.by_week.get(monday, 0) + 1
            if s.first is None or dt < s.first:
                s.first = dt
            if s.last is None or dt > s.last:
                s.last = dt
        for _ts, parts in (rec or {}).get("surg") or []:
            s.surgeries += 1
            for p in parts:
                s.parts_healed[p] += 1
    return s


def injuries_per_hour(stats: InjuryStats, sessions: Iterable) -> Optional[float]:
    """Injuries per hour of play, counted only over sessions that started on or
    after the first logged injury — older builds did not log injuries at all, so
    including their hours would understate the rate.  None when unknowable."""
    if stats.first is None:
        return None
    since = stats.first.date()
    secs = 0.0
    for sess in sessions:
        start = getattr(sess, "start_local", None)
        if start is not None and start.date() >= since:
            secs += float(getattr(sess, "duration_seconds", 0.0) or 0.0)
    if secs <= 0:
        return None
    return stats.total / (secs / 3600.0)
