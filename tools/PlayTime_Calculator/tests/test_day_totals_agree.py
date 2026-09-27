"""A day's per-channel line must add up to the same day's total.

⛔ THE DEFECT THIS PINS, and it is worth stating because it is not a double count even though
  it looks exactly like one. The calendar's day panel printed `by_day[d]` as the Total — split
  at hour boundaries, correct — beside a Channels line summed from whole `s.duration_seconds`
  for every session whose START date was `d`. A session that began at 22:00 and ran 47 hours
  contributed two hours to the total and all forty-seven to the channel row. In the reference
  data, 98 of 1113 sessions cross local midnight and the worst day showed roughly 4 h beside
  roughly 48 h.

★ WHY THE TEST IS AN EQUALITY AND NOT A THRESHOLD: the two figures are now produced by the
  same splitter (`iter_hour_segments`), so they do not merely come close, they are the same
  seconds counted twice by different keys. Asserting "within 5%" would pass for a rewrite that
  reintroduced whole-session sums on any day whose sessions happen not to cross midnight —
  which is most days, and is exactly how this survived.

⚠ The float tolerance is 1e-6 and is about floating-point addition order, not about the model.
"""
from datetime import datetime, timedelta, timezone

import pytest

from core.analytics import build_analytics, channel_seconds_by_day
from core.log_scanner import Session


def _sess(channel, start_local_naive, hours):
    """A Session pinned to the LOCAL zone, because every figure here is local-date based."""
    start = start_local_naive.astimezone()
    end = start + timedelta(hours=hours)
    return Session(path=f"{channel}-{start:%Y%m%d%H%M}.log", channel=channel, build="X",
                   start=start.astimezone(timezone.utc), end=end.astimezone(timezone.utc),
                   duration_seconds=(end - start).total_seconds())


#: The shape that broke it: begins late on day 1, ends on day 3.
OVERNIGHT = _sess("LIVE", datetime(2025, 10, 7, 22, 0), 47.0)
#: An ordinary same-day session on the day the overnight one STARTS.
SAME_DAY = _sess("PTU", datetime(2025, 10, 7, 9, 0), 2.0)


def test_channel_seconds_add_up_to_the_day_total():
    sessions = [OVERNIGHT, SAME_DAY]
    by_day = build_analytics(sessions).by_day
    chans = channel_seconds_by_day(sessions)
    assert by_day, "no days were produced — the fixture is not exercising anything"
    for d, total in by_day.items():
        got = sum(chans.get(d, {}).values())
        assert got == pytest.approx(total, abs=1e-6), (
            "%s: channels sum to %.1f but the day total is %.1f — the panel would print both"
            % (d, got, total))


def test_the_old_whole_session_sum_really_does_disagree():
    """The negative control. Without it, the test above could be passing for any reason.

    This reproduces the ORIGINAL computation — whole durations grouped by start date — and
    asserts it is wrong by a large, specific margin on the first day. If this ever stops
    failing, the fixture stopped containing a midnight-crossing session and the test above is
    no longer testing anything.
    """
    sessions = [OVERNIGHT, SAME_DAY]
    by_day = build_analytics(sessions).by_day
    day1 = OVERNIGHT.start_local.date()

    old = {}
    for s in sessions:
        if s.start_local.date() == day1:
            old[s.channel] = old.get(s.channel, 0.0) + s.duration_seconds

    old_total = sum(old.values())
    real_total = by_day[day1]
    assert old_total > real_total * 5, (
        "the fixture no longer reproduces the defect: old sum %.1f vs real %.1f. A session that "
        "crosses local midnight is required, or the fix above is being tested against nothing."
        % (old_total, real_total))


def test_a_midnight_crossing_session_appears_under_every_day_it_touches():
    """And its seconds are DIVIDED, not repeated — the thing that would be a real double count."""
    chans = channel_seconds_by_day([OVERNIGHT])
    days = sorted(chans)
    assert len(days) == 3, "a 47h session starting at 22:00 spans three local dates, got %s" % days
    total = sum(v for day in chans.values() for v in day.values())
    assert total == pytest.approx(OVERNIGHT.duration_seconds, abs=1e-6), (
        "the split changed the total: %.1f vs %.1f" % (total, OVERNIGHT.duration_seconds))
