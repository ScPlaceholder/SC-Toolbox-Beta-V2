"""THE CONTROL for the event mapper: the checks must FAIL on wrong implementations.

Same standard `test_control.py` and `test_face.py` set, and for the reason `test_face.py`
gives: *"A differential test with a broken control scores a perfect 100%, because every
mutant dies regardless of what it does."* So this asserts BOTH halves —

    BASELINE   the real MoodSource passes all twelve checks
    CONTROL    each of the ten wrong implementations is caught by >= 1 check

— and then asserts per-mutation WHICH check catches it, because a check that catches
everything is usually catching nothing in particular.

★ SIX OF THE TEN MUTANTS ARE NOT HYPOTHETICAL. They are code I actually wrote today and had
  to fix, which is the only reason I trust them to be the right shapes:

    unseeded_clock      I aimed the decay clock at log time and left the affect model's `_t`
                        seeded from WALL time. Measured on a real 74,759-line log: the step
                        was backwards, `dt = max(0.0, ...)` clamped it to zero AND left `_t`
                        un-updated, so NO decay ever ran and the face reported `fear 1.00`
                        assembled out of two deaths 17 hours apart.
    wall_clock_decay    the version before that: decay in wall time while replaying, so
                        89 `location_change` events five hours apart in the log stacked
                        inside the two seconds the replay took.
    count_events        `observed = events_seen > 0`. A real, healthy, boring session reads
                        as a disconnected feed.
    feed_live_from_...  deriving feed existence from `dominant()`, which is the collapse.
    set_mood_every_tick calling `set_mood` per frame resets `_since` and FREEZES the pool —
                        with every test in test_face.py still green, because the chooser is
                        behaving exactly as specified and the defect is in the caller.
    missing_log_quiet   an unreadable log counted as a quiet one.

⛔ WHAT IS NOT FABRICATED. Requirement 8 of the brief: no invented log lines presented as
  verified parsing. So this file NEVER writes a fake Game.log line and claims it parses.
  Two honest sources instead:
    * typed events constructed through upstream's OWN `LogEvent` class and handed to the
      parser callback. That exercises the mapper without asserting anything about text.
    * the REAL Game.log, if this machine has one. `test_real_log_*` locates it and SKIPS
      when absent — a skip is visible in the run, where a silent pass is not.
  The one string fed as a "line" is `NOT-A-LOG-LINE`, and it is fed precisely BECAUSE it
  does not parse: the point of that check is that reading a line counts as observation even
  when nothing comes of it.
"""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from pico.face import CREATURE, PERSON, FaceChooser, FaceError, UNKNOWN_PAIR
from pico import events as ev
from pico.events import (
    EventError,
    FeedState,
    MoodSource,
    PICO_EVENT_TYPES,
    check_emotion_coverage,
    check_hook_wiring,
    check_mood_floor,
    load_suit,
    scrape_parser_event_types,
    verify_upstream,
)

# One bridge for the whole module. `SuitBridge` is frozen and its `new_affect` / `new_parser`
# hand back fresh objects, so sharing it cannot leak state between checks — the thing
# `run_all` builds a fresh source per check to prevent.
# [[my-test-carried-state-and-invented-a-bug]]
try:
    BRIDGE = load_suit()
    BRIDGE_ERROR = ""
except EventError as _exc:                                     # pragma: no cover - env
    BRIDGE, BRIDGE_ERROR = None, str(_exc)

pytestmark = pytest.mark.skipif(
    BRIDGE is None, reason="SuitMk2 core not loadable: %s" % BRIDGE_ERROR)


# ── fixtures that are honest about what they are ────────────────────────────────────────

#: A string fed as a log line ON PURPOSE because it is not one. Reading it must still count
#: as observation; nothing here claims it parses into an event.
NOT_A_LINE = "NOT-A-LOG-LINE this file does not fabricate Game.log syntax"

#: Log time used by the injected-event checks — the real first-event time of the 2025-08-02
#: log this module was developed against.
LOG_T0 = datetime(2025, 8, 2, 19, 27, 13, tzinfo=timezone.utc)

#: ⛔ THE WALL CLOCK IS SET **AFTER** `LOG_T0` ON PURPOSE, AND THE FIRST VERSION WAS NOT.
#:   Replaying a past log is the ordinary case, and it is the only case where the unseeded
#:   clock actually hurts: the first step is BACKWARDS, `_decay`'s `dt = max(0.0, ...)` clamps
#:   it to zero AND leaves `_t` un-updated, so decay never runs again. Put the wall clock
#:   BEFORE the log instead and the step is forwards, the huge decay lands on levels that are
#:   already zero, `_t` is updated, and the bug SELF-HEALS after one event.
#:   ⇒ With the clock at 1_000_000 the `unseeded_clock` mutant passed all thirteen checks.
#:     Found by running the control, not by reading it. A fixture can define a mutation out
#:     of existence, and the mutant looked dead because the environment spared it.
WALL_T0 = LOG_T0.timestamp() + 86_400.0


def _epoch_of_test(ts):
    """The production converter, re-exported for a mutant that needs it. Not a copy."""
    return ev._epoch_of(ts)


def make_event(kind: str, *, offset_s: float = 0.0, data: Optional[dict] = None):
    """A typed event built through UPSTREAM's own `LogEvent`, not a local lookalike.

    Using the real class is what makes this not a fork: if upstream changes the dataclass,
    this stops constructing and the failure is loud.
    """
    return BRIDGE.parser_mod.LogEvent(
        event_type=kind,
        timestamp=(LOG_T0 + timedelta(seconds=offset_s)).replace(tzinfo=None),
        raw_line="<injected typed event; no log text is claimed>",
        category="",
        data=dict(data or {}),
    )


def inject(src: MoodSource, kind: str, *, offset_s: float = 0.0, **kw) -> None:
    """Hand a typed event to the mapper the way the parser would.

    It goes through `_on_event`, i.e. the SAME entry point the real parser calls, so the
    clock handling and the counters under test are the production ones.
    """
    src._on_event(make_event(kind, offset_s=offset_s, **kw))


def build(cls=MoodSource, **kw) -> MoodSource:
    kw.setdefault("now", lambda: WALL_T0)
    return cls(BRIDGE, **kw)


def real_log() -> Optional[Path]:
    """A real Game.log on THIS machine, or None. Never a fabricated stand-in."""
    for p in (Path(r"C:/Star Citizen/StarCitizen/LIVE/Game.log"),
              Path(r"C:/Star Citizen/StarCitizen/HOTFIX/Game.log")):
        if p.is_file():
            return p
    return None


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE CHECKS — each named, each returns None on pass or a reason on fail
# ══════════════════════════════════════════════════════════════════════════════════════════

def c_unknown_before_any_line(s: MoodSource) -> Optional[str]:
    r = s.reading()
    if r.mood is not None:
        return "nothing has been read but the mood is %r, not UNKNOWN" % r.mood
    if r.feed_live:
        return "feed reports LIVE with zero lines seen"
    return None


def c_quiet_feed_is_calm(s: MoodSource) -> Optional[str]:
    """A feed that is WORKING and reporting nothing. Distinct from a feed that is absent.

    ★ The line fed does not parse, and that is the design of the check: `lines_seen` is the
      evidence of observation, not `events_seen`. A 74,759-line log with no hooked event in
      it is a real session being watched, and must read `calm`.
    """
    s.feed_line(NOT_A_LINE)
    r = s.reading()
    if not r.feed_live:
        return "a line was read but the feed reports %r" % r.feed.reason
    if r.mood != "calm":
        return "a live, quiet feed gave %r instead of 'calm'" % r.mood
    return None


def c_no_feed_is_not_calm(s: MoodSource) -> Optional[str]:
    """THE REGRESSION THAT MATTERS. If these ever agree, the collapse is back.

    `dominant()` returns `(None, 0.0)` for both "no record for this speaker" and "everything
    decayed to zero". For a face that is flat dashes versus a smile.
    """
    before = s.reading().mood
    s.feed_line(NOT_A_LINE)
    after = s.reading().mood
    if before == after:
        return ("unread and read-but-quiet both gave %r — that is dominant()'s ambiguity "
                "leaking through the layer written to stop it" % (before,))
    if before is not None:
        return "an unread feed gave %r, not UNKNOWN" % (before,)
    if after != "calm":
        return "a read feed gave %r, not 'calm'" % (after,)
    return None


def c_feed_live_independent_of_emotion(s: MoodSource) -> Optional[str]:
    """Feed EXISTENCE must not be a function of what the feed REPORTS.

    Enforced structurally rather than by reading the code: `dominant` is replaced with
    something that raises. If `feed_state()` still answers, it provably never consulted it.
    No reading of that tuple can distinguish unknown from calm, so any implementation that
    asks it has already lost, whatever it does with the answer.
    """
    s.feed_line(NOT_A_LINE)

    def explode(_speaker):
        raise AssertionError("feed_state() consulted dominant()")

    saved, s.affect.dominant = s.affect.dominant, explode
    try:
        st = s.feed_state()
        if not st.live:
            return "feed_state said %r for a source that had been read" % st.reason
    except AssertionError as exc:
        return str(exc)
    finally:
        s.affect.dominant = saved
    return None


def c_speaker_must_exist(s: MoodSource) -> Optional[str]:
    """An unknown speaker makes dominant() return (None, 0.0) forever = permanent calm."""
    try:
        type(s)(BRIDGE, speaker="pico", now=lambda: WALL_T0)
    except EventError:
        return None
    return ("a speaker outside upstream's SPEAKERS was accepted; its affect record does not "
            "exist, so the face would read 'calm' for the life of the process")


def c_unmapped_emotion_propagates(s: MoodSource) -> Optional[str]:
    """`face.mood_for` RAISES on an unmapped emotion by design. Catching it here would turn
    every future upstream feeling into a contented penguin."""
    s.feed_line(NOT_A_LINE)
    s.affect.dominant = lambda _sp: ("smugness", 0.9)
    try:
        r = s.reading()
    except FaceError:
        return None
    return ("an emotion no table maps produced %r instead of raising — the refusal in "
            "face.mood_for was swallowed" % (r.mood,))


def c_missing_log_refuses(s: MoodSource) -> Optional[str]:
    """An absence is not a quiet room. [[an-absence-needs-every-path-right]]"""
    ghost = Path("this-log-does-not-exist-anywhere.log")
    raised = False
    try:
        s.read_log(ghost)
    except EventError:
        raised = True
    if not raised:
        return "read_log accepted a path that does not exist"
    ok, _why = s.try_read_log(ghost)
    if ok:
        return "try_read_log reported success for a missing file"
    if s.reading().feed_live:
        return ("a failed read marked the feed LIVE — a missing log was laundered into "
                "'nothing happened'")
    return None


def c_rotation_survives_apply(s: MoodSource) -> Optional[str]:
    """⛔ `set_mood` resets `_since`; `choose()` indexes the pool off `at - _since`.

    Call `set_mood` every tick and the index is always 0 and the face FREEZES — with every
    test in test_face.py still green, because the chooser is doing exactly what it says.
    The defect would live entirely in this caller, which is why the check lives here.
    """
    s.feed_line(NOT_A_LINE)
    inject(s, "incapacitated")
    f = FaceChooser(rotate_s=2.0)
    seen = set()
    for i in range(8):
        t = WALL_T0 + i * 2.0
        s.apply_to(f, at=t)
        seen.add(f.choose(at=t))
    if len(seen) < 2:
        return ("the face showed %d distinct pair(s) over 8 rotations — apply_to is resetting "
                "_since every tick and the pool never advances" % len(seen))
    return None


def c_hooked_event_moves_the_mood(s: MoodSource) -> Optional[str]:
    """The wiring, end to end: a hooked event must change the face off calm."""
    s.feed_line(NOT_A_LINE)
    if s.reading().mood != "calm":
        return "a quiet live feed did not start at calm"
    inject(s, "incapacitated")
    r = s.reading()
    if r.mood == "calm" or r.emotion is None:
        return ("an 'incapacitated' event left the mood at %r (emotion %r). The parser and "
                "HOOKS have drifted apart, or the event never reached the affect model"
                % (r.mood, r.emotion))
    return None


def c_stale_feed_is_unknown(s: MoodSource) -> Optional[str]:
    """With a staleness window set, an old feed must go UNKNOWN rather than reporting calm."""
    stale = type(s)(BRIDGE, now=lambda: WALL_T0, max_silence_s=60.0)
    stale.feed_line(NOT_A_LINE)
    if not stale.reading(at=WALL_T0).feed_live:
        return "a freshly read feed already counted as stale"
    late = stale.reading(at=WALL_T0 + 600.0)
    if late.feed_live:
        return "600s of silence against a 60s window still reported LIVE"
    if late.mood is not None:
        return "a stale feed gave %r instead of UNKNOWN" % (late.mood,)
    return None


def c_decay_uses_log_time(s: MoodSource) -> Optional[str]:
    """Decay must follow the LOG's clock, so the answer cannot depend on replay speed.

    Two identical event sequences — 1800s apart in LOG time — replayed under a frozen wall
    clock and under one that advances 1800s. The same events at the same game times must
    give the same feeling. If they differ, decay is being driven by how fast the file was
    read, which is exactly the defect measured on the real 74,759-line log.
    """
    levels = []
    for advance in (0.0, 1800.0):
        clock = [WALL_T0]
        src = type(s)(BRIDGE, now=lambda c=clock: c[0])
        src.feed_line(NOT_A_LINE)
        inject(src, "incapacitated", offset_s=0.0)
        clock[0] += advance
        inject(src, "injury", offset_s=1800.0, data={"tier": 3})
        levels.append(round(src.reading().level, 6))
    if levels[0] != levels[1]:
        return ("the same game events gave level %r when replayed instantly and %r when "
                "replayed over 1800s of wall time — decay is on the wrong clock"
                % (levels[0], levels[1]))
    return None


def c_clock_seeded_before_first_feed(s: MoodSource) -> Optional[str]:
    """The affect model's internal clock must be in LOG time BEFORE the first event is fed.

    ⛔ The bug this was written for: `CompanionAffect.__init__` sets `_t = now()` (wall). Aim
      the clock at log time afterwards and the first step is one huge jump. Backwards, it is
      clamped to zero AND `_t` is never updated, so decay never runs again for the whole
      replay — the `fear 1.00` reading. Asserting the seed directly is the only check that
      catches it, because a single event decays correctly either way and the damage only
      shows up hundreds of events later.
    """
    s.feed_line(NOT_A_LINE)
    inject(s, "incapacitated", offset_s=0.0)
    want = LOG_T0.timestamp()
    got = float(getattr(s.affect, "_t", float("nan")))
    if abs(got - want) > 1.0:
        return ("after the first event the affect clock is at %r but the event's log time is "
                "%r (off by %.0f s). Decay will be computed against wall time."
                % (got, want, got - want))
    return None


def c_decay_actually_happens(s: MoodSource) -> Optional[str]:
    """A feeling must FADE. A clock that never advances `_t` looks fine on one event.

    Paired with the seed check above: that one proves where the clock starts, this one proves
    it moves. `fear` has a 300s half-life upstream, so 1800s is six half-lives and the level
    must drop to a small fraction.
    """
    s.feed_line(NOT_A_LINE)
    inject(s, "incapacitated", offset_s=0.0)
    hot = s.reading().level
    inject(s, "qt_arrived", offset_s=1800.0)          # a tiny hook, 1800s later in log time
    cooled = s.affect.levels[s.speaker]["fear"]
    if hot <= 0.0:
        return "the event produced no feeling at all, so fading cannot be tested"
    if cooled >= hot * 0.5:
        return ("fear was %.3f and is %.3f after 1800s of LOG time (six 300s half-lives). "
                "It is not decaying." % (hot, cooled))
    return None


CHECKS = {
    "unknown_before_any_line": c_unknown_before_any_line,
    "quiet_feed_is_calm": c_quiet_feed_is_calm,
    "no_feed_is_not_calm": c_no_feed_is_not_calm,
    "feed_live_independent_of_emotion": c_feed_live_independent_of_emotion,
    "speaker_must_exist": c_speaker_must_exist,
    "unmapped_emotion_propagates": c_unmapped_emotion_propagates,
    "missing_log_refuses": c_missing_log_refuses,
    "rotation_survives_apply": c_rotation_survives_apply,
    "hooked_event_moves_the_mood": c_hooked_event_moves_the_mood,
    "stale_feed_is_unknown": c_stale_feed_is_unknown,
    "decay_uses_log_time": c_decay_uses_log_time,
    "clock_seeded_before_first_feed": c_clock_seeded_before_first_feed,
    "decay_actually_happens": c_decay_actually_happens,
}


def run_all(cls=MoodSource, **kw) -> dict[str, Optional[str]]:
    out: dict[str, Optional[str]] = {}
    for name, fn in CHECKS.items():
        try:
            s = build(cls, **kw)      # a FRESH source per check: shared state between checks
        except EventError as exc:     # is how one test's events leak into another's answer
            out[name] = "construction raised: %s" % exc
            continue
        try:
            out[name] = fn(s)
        except (EventError, FaceError) as exc:
            out[name] = "raised: %s: %s" % (type(exc).__name__, exc)
    return out


# ── BASELINE ───────────────────────────────────────────────────────────────────────────

def test_baseline_passes_every_check():
    res = run_all()
    bad = {k: v for k, v in res.items() if v is not None}
    assert not bad, "the real mapper failed its own checks: %r" % bad


# ══════════════════════════════════════════════════════════════════════════════════════════
# CONTROL: ten wrong implementations
# ══════════════════════════════════════════════════════════════════════════════════════════

class M_AlwaysLive(MoodSource):
    """The default that lies: assume a feed exists."""
    def feed_state(self, *, at=None):
        st = super().feed_state(at=at)
        return FeedState(upstream_ok=True, speaker_known=True, lines_seen=max(1, st.lines_seen),
                         events_seen=st.events_seen, events_moved=st.events_moved,
                         last_line_at=st.at, last_event_at=st.last_event_at,
                         max_silence_s=None, at=st.at)


class M_FeedLiveFromDominant(MoodSource):
    """Derive feed existence from what the feed reports — the collapse itself."""
    def feed_state(self, *, at=None):
        st = super().feed_state(at=at)
        emotion, level = self.affect.dominant(self.speaker)
        alive = emotion is not None or level > 0.0
        return FeedState(upstream_ok=alive, speaker_known=alive,
                         lines_seen=1 if alive else 0, events_seen=st.events_seen,
                         events_moved=st.events_moved, last_line_at=st.last_line_at,
                         last_event_at=st.last_event_at, max_silence_s=None, at=st.at)


class M_CountEventsNotLines(MoodSource):
    """`observed = events_seen > 0`: a real, healthy, boring session reads as disconnected."""
    def feed_state(self, *, at=None):
        st = super().feed_state(at=at)
        return FeedState(upstream_ok=st.upstream_ok, speaker_known=st.speaker_known,
                         lines_seen=st.events_seen, events_seen=st.events_seen,
                         events_moved=st.events_moved, last_line_at=st.last_line_at,
                         last_event_at=st.last_event_at, max_silence_s=st.max_silence_s,
                         at=st.at)


class M_SwallowFaceError(MoodSource):
    """Catch the refusal and substitute a default — a new upstream feeling becomes calm."""
    def reading(self, *, at=None):
        try:
            return super().reading(at=at)
        except FaceError:
            t = self._now() if at is None else float(at)
            feed = self.feed_state(at=t)
            emotion, level = self.affect.dominant(self.speaker)
            return ev.MoodReading(mood="calm", emotion=emotion, level=float(level),
                                  feed=feed, speaker=self.speaker, at=t)


class M_MissingLogIsQuiet(MoodSource):
    """An unreadable log counted as a quiet one."""
    def read_log(self, path, *, encoding="utf-8"):
        p = Path(path)
        if not p.is_file():
            self.lines_seen += 1
            self.last_line_at = self._now()
            return 0
        return super().read_log(path, encoding=encoding)

    def try_read_log(self, path, **kw):
        return True, "read 0 line(s)"


class M_SetMoodEveryTick(MoodSource):
    """Re-arm the chooser every tick: `_since` resets and the pool freezes."""
    def apply_to(self, chooser, *, at=None):
        r = self.reading(at=at)
        if r.mood is None:
            chooser.forget()
        else:
            chooser.set_mood(r.mood, at=r.at)
        return r


class M_AnySpeaker(MoodSource):
    """Skip the speaker gate and let a typo read as permanent calm."""
    def __init__(self, bridge=None, *, speaker="elah", **kw):
        real = speaker if speaker in (bridge or BRIDGE).speakers else "elah"
        super().__init__(bridge, speaker=real, **kw)
        self.speaker = speaker           # put the bogus name back AFTER the gate


class M_WallClockDecay(MoodSource):
    """Decay in wall time: the answer depends on how fast the log was read."""
    def _advance_clock(self, log_epoch):
        if log_epoch is not None:
            self.last_event_at = log_epoch          # still report it; just do not USE it


class M_UnseededClock(MoodSource):
    """Aim the clock at log time but leave the affect model's `_t` seeded from wall time.

    My own first version. Under a real clock and a historical log the step is backwards,
    `dt = max(0.0, ...)` clamps it AND never advances `_t`, so decay never runs again.
    """
    def _advance_clock(self, log_epoch):
        if log_epoch is None:
            return
        if self.last_event_at is not None and log_epoch < self.last_event_at:
            self.clock_regressions += 1
            return
        self.last_event_at = log_epoch
        self._clock_offset = log_epoch - self._now()
        # and no rebuild: `affect._t` stays in wall time


class M_StaleIsCalm(MoodSource):
    """Ignore the staleness window a caller explicitly asked for."""
    def __init__(self, *a, **kw):
        kw["max_silence_s"] = None
        super().__init__(*a, **kw)


class M_NoFeedIsCalm(MoodSource):
    """THE LYING DASHBOARD. No feed at all, and the penguin looks contented about it."""
    def reading(self, *, at=None):
        t = self._now() if at is None else float(at)
        feed = self.feed_state(at=t)
        emotion, level = self.affect.dominant(self.speaker)
        mood = ev._face.mood_for(emotion, level, feed_live=True,
                                 temperament=self.temperament)
        return ev.MoodReading(mood=mood, emotion=emotion, level=float(level), feed=feed,
                              speaker=self.speaker, at=t)


class M_DroppedEvents(MoodSource):
    """Count the events and never feed them: the silent-drift shape.

    This is what upstream renaming an event type would look like from inside — the parser
    works, the counters move, `feed_live` is honestly TRUE, and the face is calm forever.
    """
    def _on_event(self, event):
        self.events_seen += 1
        self.last_event = event
        self._advance_clock(_epoch_of_test(getattr(event, "timestamp", None)))


class M_FrozenClock(MoodSource):
    """Seed the clock correctly and then never advance it: a feeling that never fades."""
    def _advance_clock(self, log_epoch):
        if self._clock_seeded or log_epoch is None:
            return
        super()._advance_clock(log_epoch)


MUTANTS = {
    "always_live": (M_AlwaysLive, "unknown_before_any_line"),
    "feed_live_from_dominant": (M_FeedLiveFromDominant, "feed_live_independent_of_emotion"),
    "count_events_not_lines": (M_CountEventsNotLines, "quiet_feed_is_calm"),
    "swallow_face_error": (M_SwallowFaceError, "unmapped_emotion_propagates"),
    "missing_log_is_quiet": (M_MissingLogIsQuiet, "missing_log_refuses"),
    "set_mood_every_tick": (M_SetMoodEveryTick, "rotation_survives_apply"),
    "any_speaker": (M_AnySpeaker, "speaker_must_exist"),
    "wall_clock_decay": (M_WallClockDecay, "decay_uses_log_time"),
    "unseeded_clock": (M_UnseededClock, "clock_seeded_before_first_feed"),
    "stale_is_calm": (M_StaleIsCalm, "stale_feed_is_unknown"),
    "no_feed_is_calm": (M_NoFeedIsCalm, "no_feed_is_not_calm"),
    "dropped_events": (M_DroppedEvents, "hooked_event_moves_the_mood"),
    "frozen_clock": (M_FrozenClock, "decay_actually_happens"),
}


def test_every_check_has_a_mutant_that_proves_it_can_fail():
    """⛔ A CHECK WITH NO MUTANT HAS ONLY EVER PASSED, which is indistinguishable from a
    check that cannot fail. This asserts the control is COMPLETE, not merely present.

    Written after finding that ten mutants covered ten of thirteen checks and the three
    without one were invisible in a green run — the exact shape `rule_isolation.py` looks for.
    """
    named = {expected for _cls, expected in MUTANTS.values()}
    unproven = sorted(set(CHECKS) - named)
    assert not unproven, (
        "check(s) with no mutant written for them: %s. Each has only ever passed; write a "
        "wrong implementation it must catch, or delete it." % unproven)
    stray = sorted(named - set(CHECKS))
    assert not stray, "MUTANTS names check(s) that do not exist: %s" % stray


@pytest.mark.parametrize("label", sorted(MUTANTS))
def test_each_mutant_is_caught(label):
    cls, _expected = MUTANTS[label]
    res = run_all(cls)
    caught = [k for k, v in res.items() if v is not None]
    assert caught, (
        "mutant %r passed EVERY check. Either the mutation is inert or the checks are."
        % label)


@pytest.mark.parametrize("label", sorted(MUTANTS))
def test_the_named_check_is_the_one_that_catches_it(label):
    cls, expected = MUTANTS[label]
    res = run_all(cls)
    assert res.get(expected) is not None, (
        "mutant %r was NOT caught by %r, the check written for it. It died to %r instead, "
        "which means %r is not doing the work it claims."
        % (label, expected, [k for k, v in res.items() if v is not None], expected))


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE SEAM CHECKS — an adapter's whole failure mode is an upstream rename
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_mood_floor_agrees_with_face():
    """face.py restates MOOD_FLOOR and flagged the staleness itself. Nothing watched it."""
    check_mood_floor(BRIDGE.emotion)                  # must not raise against the live tree


def test_mood_floor_check_can_fail():
    """Prove the instrument works before trusting what it reports."""
    with pytest.raises(EventError):
        check_mood_floor(types.SimpleNamespace(MOOD_FLOOR=0.5))


def test_emotion_coverage_agrees_with_face():
    check_emotion_coverage(BRIDGE.emotion)


def test_emotion_coverage_catches_a_new_upstream_emotion():
    """A tenth emotion must surface at LOAD, not on the one real event that raises."""
    fake = types.SimpleNamespace(EMOTIONS=dict.fromkeys(list(BRIDGE.emotions) + ["smugness"]))
    with pytest.raises(EventError) as exc:
        check_emotion_coverage(fake)
    assert "smugness" in str(exc.value)


def test_hook_wiring_is_non_empty_against_the_live_tree():
    """⛔ THE CHECK WHOSE ABSENCE WOULD BE SILENT.

    If the parser's event names and HOOKS' keys drift apart, the parser keeps emitting,
    every `feed()` returns False, `feed_live` stays TRUE because lines really are being
    read, and the penguin reports CALM through a firefight. No downstream symptom at all.
    """
    wired = check_hook_wiring(BRIDGE.emotion, BRIDGE.core)
    assert wired, "no event type is both parseable and hooked"
    assert set(wired) <= set(BRIDGE.hooks)


def test_hook_wiring_catches_a_total_rename():
    fake = types.SimpleNamespace(HOOKS={"renamed_" + k: v for k, v in BRIDGE.emotion.HOOKS.items()})
    with pytest.raises(EventError) as exc:
        check_hook_wiring(fake, BRIDGE.core)
    assert "contented face" in str(exc.value)


def test_recorded_intersection_still_holds():
    """`PICO_EVENT_TYPES` is a RECORD, not the live answer — so a shrink is visible.

    The live answer is recomputed by `check_hook_wiring` on every load. This asserts the
    record has not silently gone stale, and names the direction if it has.
    """
    live = set(check_hook_wiring(BRIDGE.emotion, BRIDGE.core))
    recorded = set(PICO_EVENT_TYPES)
    assert not (recorded - live), (
        "PICO_EVENT_TYPES claims event(s) the tree no longer wires: %s"
        % sorted(recorded - live))
    assert not (live - recorded), (
        "the tree now wires event(s) the record does not mention: %s — update the record "
        "and decide whether the new one needs a face" % sorted(live - recorded))


def test_scrape_refuses_to_report_an_absence_it_cannot_see(tmp_path):
    """⚠ AN INSTRUMENT THAT CANNOT SEE MUST NOT READ AS A CLEAN BOARD.

    If upstream renames `_classify_line`, the AST walk finds nothing. "Found nothing" would
    otherwise be indistinguishable from "the parser emits nothing", which would make the
    wiring comparison fail with a wrong diagnosis — or pass vacuously.
    [[i-nearly-reported-two-absences-my-own-tool-manufactured]]
    """
    (tmp_path / "event_parser.py").write_text(
        "def _renamed_classifier(line):\n    return 'incapacitated'\n", encoding="utf-8")
    with pytest.raises(EventError) as exc:
        scrape_parser_event_types(tmp_path)
    assert "blind" in str(exc.value)


def test_scrape_refuses_when_the_function_returns_no_literals(tmp_path):
    """The second blindness: the function is there but no longer returns string constants."""
    (tmp_path / "event_parser.py").write_text(
        "def _classify_line(line):\n    return TABLE.get(line)\n", encoding="utf-8")
    with pytest.raises(EventError) as exc:
        scrape_parser_event_types(tmp_path)
    assert "ZERO event-type literals" in str(exc.value)


def test_scrape_sees_real_event_names():
    """And the positive control: it must find things when they ARE there."""
    names = scrape_parser_event_types(BRIDGE.core)
    assert "incapacitated" in names and "location_change" in names
    assert len(names) > 20, "only %d names scraped; the walk is not seeing the parser" % len(names)


def test_verify_upstream_catches_a_missing_attribute():
    with pytest.raises(EventError) as exc:
        verify_upstream(types.SimpleNamespace(), BRIDGE.parser_mod)
    assert "SPEAKERS" in str(exc.value)


def test_verify_upstream_passes_the_live_tree():
    verify_upstream(BRIDGE.emotion, BRIDGE.parser_mod)


# ══════════════════════════════════════════════════════════════════════════════════════════
# NO NAMESPACE POLLUTION — the reason this is an adapter and not a sys.path hack
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_loading_upstream_claims_no_bare_module_names():
    """`event_parser.py` itself records being bitten by exactly this collision.

    Putting `SuitMk2/core` on sys.path would make ~60 files importable as `emotion`,
    `pacing`, `eyes`, `feedback` inside whatever process hosts Pico.
    """
    import sys
    for bare in ("emotion", "event_parser", "state_store", "pacing", "eyes"):
        assert bare not in sys.modules, (
            "loading the adapter claimed the bare name %r in sys.modules" % bare)
    assert str(BRIDGE.core) not in sys.path, "the adapter put SuitMk2/core on sys.path"


def test_the_adapter_writes_nothing(tmp_path):
    """`CompanionAffect(path=None)` makes `_save` a no-op. Asserted, not assumed."""
    s = build()
    s.feed_line(NOT_A_LINE)
    inject(s, "incapacitated")
    assert s.affect.path is None, "the affect model was given a path and will persist"
    assert not list(tmp_path.iterdir())


def test_a_supplied_affect_is_used_and_not_replaced():
    """Pico may re-aim the decay clock only on a model it created.

    ⛔ THE FIRST VERSION OF THIS TEST ASSERTED THE WRONG CONSEQUENCE AND A SOURCE MUTATION
      FOUND IT. It checked that `shared._t` was unchanged. Delete the `owns_affect` guard and
      `_advance_clock` runs, which REBUILDS `self.affect` — so Pico silently drops the
      caller's object and uses a fresh empty one, while `shared._t` sits there untouched and
      the assertion passes. The mutation survived a green suite.
    ⇒ The invariant is IDENTITY, not the clock field: a supplied affect must still BE the
      one being fed afterwards. That is strictly stronger and it is what the caller cares
      about — sharing the companion's mood is the whole point of passing it in.
      [[a-correct-rule-can-guard-a-branch-nothing-takes]]
    """
    shared = BRIDGE.new_affect(now=lambda: 5_000_000.0)
    s = MoodSource(BRIDGE, affect=shared, now=lambda: WALL_T0)
    assert s.owns_affect is False
    inject(s, "incapacitated", offset_s=0.0)
    assert s.affect is shared, (
        "Pico replaced the caller-supplied affect model with one of its own; the shared mood "
        "is no longer shared and nothing said so")
    assert abs(float(shared._t) - 5_000_000.0) < 1.0, (
        "Pico re-seeded a caller-supplied affect model's clock")
    assert shared.levels["elah"]["fear"] > 0.0, "the event never reached the shared model"
    assert s.last_event_at is not None, "the log timestamp should still be REPORTED"


def test_a_non_monotone_timestamp_is_counted_and_refused():
    """⚠ THE GUARD THAT NOTHING WAS EXERCISING, found by a source mutation surviving.

    Upstream's `_extract_timestamp` falls back to `datetime.now()` when a line carries no
    `<...Z>` stamp, which in a historical log is months in the FUTURE and would zero every
    emotion. Real logs are ordered — MEASURED 0 regressions across 234 events in two of them
    — so no real replay and no injected sequence ever took this branch, and deleting it left
    the suite green. A correct rule guarding a branch nothing takes is not covered.
    """
    s = build()
    s.feed_line(NOT_A_LINE)
    inject(s, "incapacitated", offset_s=3600.0)
    aimed = s.last_event_at
    hot = s.affect.levels[s.speaker]["fear"]
    assert hot > 0.0

    inject(s, "injury", offset_s=0.0, data={"tier": 3})     # an hour EARLIER: a regression
    assert s.clock_regressions == 1, (
        "a backwards timestamp was accepted silently; clock_regressions=%d"
        % s.clock_regressions)
    assert s.last_event_at == aimed, (
        "the decay clock was re-aimed backwards to %r from %r" % (s.last_event_at, aimed))
    assert s.affect.levels[s.speaker]["fear"] >= hot, (
        "fear FELL after a backwards timestamp — the clock jumped and decayed the feeling "
        "against time that never passed")


# ══════════════════════════════════════════════════════════════════════════════════════════
# FeedState: the three facts, and the named reason
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_feed_state_reason_reports_the_outermost_failure():
    """"No upstream" makes "no speaker record" true too; reporting the inner one misdirects."""
    assert "upstream" in FeedState(False, False, 0).reason
    assert "speaker" in FeedState(True, False, 0).reason
    assert "no log line has been read" in FeedState(True, True, 0).reason
    assert FeedState(True, True, 1).reason == "live"


def test_feed_state_live_is_the_and_of_three_facts():
    assert FeedState(True, True, 1).live
    for bad in (FeedState(False, True, 1), FeedState(True, False, 1), FeedState(True, True, 0)):
        assert not bad.live, bad


def test_log_age_is_not_read_time():
    """⛔ The field I almost shipped alone. `last_line_at` is when PICO READ; `log_age_s` is
    how old the GAME EVENT is. Replay a month-old log and the first says `now`."""
    st = FeedState(True, True, 10, last_line_at=WALL_T0,
                   last_event_at=WALL_T0 - 86_400.0, at=WALL_T0)
    assert st.log_age_s == 86_400.0
    assert st.live, "a day-old event is not by itself a dead feed; that is a separate policy"
    assert FeedState(True, True, 10, at=1.0).log_age_s is None


def test_max_silence_must_be_positive():
    with pytest.raises(EventError):
        MoodSource(BRIDGE, now=lambda: 1.0, max_silence_s=0.0)


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE SEAM WITH face.py, AND THE TEMPERAMENT
# ══════════════════════════════════════════════════════════════════════════════════════════

def test_apply_to_forgets_when_the_feed_dies():
    f = FaceChooser(rotate_s=2.0)
    s = build()
    s.feed_line(NOT_A_LINE)
    inject(s, "incapacitated")
    s.apply_to(f, at=WALL_T0)
    assert f.choose(at=WALL_T0) != UNKNOWN_PAIR
    blind = build()                                   # nothing read: the feed does not exist
    blind.apply_to(f, at=WALL_T0)
    assert f.choose(at=WALL_T0) == UNKNOWN_PAIR, (
        "a dead feed left the last face on the visor instead of the UNKNOWN one")


def test_temperament_reaches_the_reading():
    """The same event, felt harder. If these ever agree, the knob does nothing.

    ⚠ THE EVENT HAS TO BE CHOSEN, NOT PICKED. `face.mood_for` bands fear at 0.55, and
      `CREATURE.gain` is 2.0, so only a raw level in [0.275, 0.55) reads differently for the
      two temperaments. Tier 3 (the mildest injury: fear 0.25 x 1.0 = 0.25) lands BELOW that
      window and both read `hurt` — the first version of this test asserted a difference that
      the arithmetic made impossible. SC counts injury tiers DOWN, so tier 2 is the middling
      one: 0.25 x 1.6 = 0.4, which PERSON feels as `hurt` and CREATURE as `startled`.
    """
    got = {}
    for temper in (PERSON, CREATURE):
        s = build(temperament=temper)
        s.feed_line(NOT_A_LINE)
        inject(s, "injury", data={"tier": 2})          # elah: fear 0.25 x 1.6 = 0.40
        got[temper.name] = s.reading().mood
    assert got["person"] != got["creature"], (
        "PERSON and CREATURE read the same event identically (%r) — either the temperament "
        "is not reaching the reading, or the level chosen does not straddle a band" % (got,))


def test_a_reading_is_directly_usable_by_the_chooser():
    """The contract's seam: whatever `reading().mood` is, `set_mood` must accept it."""
    f = FaceChooser()
    s = build()
    s.feed_line(NOT_A_LINE)
    for kind in PICO_EVENT_TYPES:
        inject(s, kind, data={"tier": 1, "amount": 50_000})
        mood = s.reading().mood
        if mood is not None:
            f.set_mood(mood, at=0.0)                  # raises on an unknown mood name


def test_non_hook_events_are_counted_but_do_not_move_the_mood():
    """Most of what a log says is not a feeling, and that is not a failure."""
    s = build()
    s.feed_line(NOT_A_LINE)
    inject(s, "weapon_holstered")
    inject(s, "platform_moving")
    assert s.events_seen == 2
    assert s.events_moved == 0
    assert s.reading().mood == "calm", "a log with no hooked event in it is a quiet session"


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE REAL LOG — skipped, never faked, when this machine has none
# ══════════════════════════════════════════════════════════════════════════════════════════

@pytest.mark.skipif(real_log() is None, reason="no real Game.log on this machine")
def test_real_log_produces_a_reading_and_the_feed_comes_alive():
    """⚠ THIS ONE RUNS ON THE REAL CLOCK, unlike every check above.

    `log_age_s` compares a real log's timestamps to wall time, so a fake clock makes it
    meaningless — the first version asserted freshness against a wall clock of 1,000,000
    and reported the events as 56 years in the future. A test about real-world freshness
    cannot run on an invented clock.
    """
    import time
    log = real_log()
    s = MoodSource(BRIDGE, now=time.time)
    assert s.reading().mood is None, "UNKNOWN before the first line"
    n = s.read_log(log)
    assert n > 0
    r = s.reading()
    assert r.feed_live, "read %d real lines and the feed still reports %r" % (n, r.feed.reason)
    if r.mood is not None:
        # The seam: whatever a real log produced, the chooser must accept it by name.
        FaceChooser().set_mood(r.mood, at=0.0)
    age = r.feed.log_age_s
    assert age is not None, "a real log produced no timestamped event at all"
    assert age > -86_400.0, (
        "the newest game event is dated %.0fs in the FUTURE. Either the log's UTC stamps are "
        "being misread or upstream's local-time fallback is in play." % -age)
    assert s.clock_regressions == 0, (
        "%d non-monotone timestamp(s) in a real log — the upstream datetime.now() fallback "
        "is in play and the decay clock refused them" % s.clock_regressions)


@pytest.mark.skipif(real_log() is None, reason="no real Game.log on this machine")
def test_real_log_replay_is_independent_of_replay_speed():
    """The measured defect, asserted against real data rather than an injected sequence.

    ⛔ IT COMPARES THE STATE AT EACH GAME EVENT, NOT THE READING AFTER THE FILE, AND THE
      DIFFERENCE IS NOT PEDANTRY. The clock keeps advancing on wall time after the last
      event — deliberately, so a feeling still fades while nothing is happening. So a slow
      replay legitimately has more wall time between the last event and the final read, and
      comparing the final readings failed by 0.047 vs 0.044 on a correct implementation.
      That is the invariant stated wrong, not the code being wrong: what must not depend on
      replay speed is the state AT each game event.
    """
    log = real_log()
    runs = []
    for step in (0.0, 5.0):
        clock = [WALL_T0]
        s = MoodSource(BRIDGE, now=lambda c=clock: c[0])
        snaps: list[dict] = []
        # A second subscriber runs AFTER `_on_event`, so it sees the post-feed state.
        s.parser.subscribe(lambda _e, s=s, snaps=snaps: snaps.append(
            {k: round(v, 6) for k, v in s.affect.levels[s.speaker].items()}))
        with log.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                s.feed_line(line)
                clock[0] += step                       # pretend the replay took real time
        runs.append(snaps)
    assert runs[0] and runs[1], "the real log produced no events to compare"
    assert len(runs[0]) == len(runs[1])
    assert runs[0] == runs[1], (
        "the same real log produced different affect state at the same game events when read "
        "at different speeds — decay is being driven by how fast the file was read. First "
        "divergence at event %d." % next(i for i, (a, b) in enumerate(zip(*runs)) if a != b))
