"""speak_gate.py - "know when to shut up" (ARCHITECTURE.md roadmap #1, principle 6: "presence is
mostly silence. Speak rarely; prove attention when you do.").

This module does NOT decide what to say or whether it is worth saying content-wise (that is the
character engine / rhetoric planner upstream). It decides, given a candidate utterance already
planned and a snapshot of the situation, whether NOW is a moment to say anything at all:
ALLOW / DEFER (with retry_after_s) / DROP, each with a human-readable reason for auditing.

Deterministic, side-effect free: no audio, no network, no threads, no wall-clock reads baked in.
The caller supplies `now()` (a zero-arg callable returning a float, e.g. `time.monotonic`) so tests
can run on a fake clock with no sleeping.

    from speak_gate import SpeakGate, SpeakState, Candidate, Priority
    gate = SpeakGate(now=time.monotonic)
    decision = gate.evaluate(state, candidate)
    if decision.verdict == Verdict.ALLOW:
        speak(candidate)
        gate.record_spoken(state, candidate)

    python speak_gate.py --selftest
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Dict, List, Optional


# ---------------------------------------------------------------------------
# Priorities
# ---------------------------------------------------------------------------

class Priority(Enum):
    URGENT = "URGENT"       # collision, hull critical, death imminent - always heard
    PRACTICAL = "PRACTICAL"  # refinery done, fuel vs jump need, cheaper elsewhere
    EVENT = "EVENT"          # noteworthy but not urgent (new system, mission complete)
    AMBIENT = "AMBIENT"      # flavor commentary tied to current state
    BANTER = "BANTER"        # character-to-character or idle chatter, lowest value


# Priorities ordered low -> high, used for combat/party-comms "only X and above" rules.
_PRIORITY_RANK = {
    Priority.BANTER: 0,
    Priority.AMBIENT: 1,
    Priority.EVENT: 2,
    Priority.PRACTICAL: 3,
    Priority.URGENT: 4,
}


class Verdict(Enum):
    ALLOW = "ALLOW"
    DEFER = "DEFER"
    DROP = "DROP"


# ---------------------------------------------------------------------------
# Tunable thresholds (all in one place on purpose - see report for rationale)
# ---------------------------------------------------------------------------

# --- cooldowns (seconds) ---
GLOBAL_MIN_GAP_S = 4.0          # minimum gap between ANY two spoken lines
PRIORITY_COOLDOWN_S: Dict[Priority, float] = {
    Priority.URGENT: 0.0,       # never rate-limited by its own cooldown
    Priority.PRACTICAL: 20.0,
    Priority.EVENT: 45.0,
    Priority.AMBIENT: 180.0,    # 3 min - ambient flavor is cheap to skip
    Priority.BANTER: 240.0,     # 4 min - lowest value, longest gap
}

# --- pilot speaking / barge-in ---
PILOT_SPEAKING_RETRY_S = 2.0    # re-check shortly after pilot stops talking

# --- party comms ---
PARTY_COMMS_ALLOWED = {Priority.URGENT, Priority.PRACTICAL}
PARTY_COMMS_RETRY_S = 15.0

# --- combat ---
COMBAT_INTENSITY_THRESHOLD = 0.5   # above this, "hot" combat gates speech hard
COMBAT_HOT_ALLOWED = {Priority.URGENT}
COMBAT_EVENT_RETRY_S = 10.0        # EVENT deferred until intensity drops, re-check cadence

# --- staleness ---
EVENT_STALE_AFTER_S = 30.0         # a late EVENT reaction is worse than none

# --- quiet budget (rolling window) ---
QUIET_BUDGET_WINDOW_S = 600.0      # 10 minutes
QUIET_BUDGET_MAX_LINES = 4         # at most K AMBIENT+BANTER lines per window
QUIET_BUDGET_PRIORITIES = {Priority.AMBIENT, Priority.BANTER}
QUIET_BUDGET_RETRY_S = 60.0        # re-check cadence once budget is exhausted


def default_retry(seconds: float) -> float:
    """Clamp a retry-after to a sane non-negative floor."""
    return max(0.0, seconds)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Candidate:
    """A candidate utterance offered to the gate. Content/meaning is decided upstream; this is
    only the metadata the gate needs to judge TIMING."""
    priority: Priority
    speaker: str                 # "elah" | "montaigne" | ...
    text_len_words: int
    created_at: float            # when the utterance was planned/authorized (same clock as now())


@dataclass
class SpeakState:
    """Mutable situation snapshot the caller keeps updated. The gate reads it; `record_spoken`
    (and `on_pilot_started_speaking`) are the only mutators the gate itself calls."""
    # pilot mic
    pilot_speaking: bool = False
    pilot_last_spoke_at: Optional[float] = None

    # in-game comms
    party_comms_active: bool = False

    # combat
    in_combat: bool = False
    combat_intensity: float = 0.0

    # companion's own speech
    companion_speaking: bool = False
    companion_started_at: Optional[float] = None

    # user controls
    muted: bool = False
    stream_safe: bool = False

    # history for cooldowns / budget
    last_spoken_at: Optional[float] = None
    last_spoken_at_by_priority: Dict[Priority, float] = field(default_factory=dict)
    # timestamps of every AMBIENT/BANTER line spoken, for the rolling quiet budget.
    quiet_budget_log: List[float] = field(default_factory=list)


@dataclass
class Decision:
    verdict: Verdict
    reason: str
    retry_after_s: Optional[float] = None   # only meaningful for DEFER
    barge_in: bool = False                  # True -> caller should stop companion's current line


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

class SpeakGate:
    """Stateless decision function plus a couple of bookkeeping helpers. All time comes from
    `now()`, injected so tests never sleep."""

    def __init__(self, now: Callable[[], float]):
        self._now = now

    # -- public API ---------------------------------------------------

    def evaluate(self, state: SpeakState, candidate: Candidate) -> Decision:
        now = self._now()

        # Rule: muted is absolute and instant. Nothing survives it, not even URGENT.
        if state.muted:
            return Decision(Verdict.DROP, self._audit("muted: hard mute active", state))

        # Rule: pilot is speaking -> defer everything except URGENT. If the companion is
        # mid-line, also signal barge-in so the caller stops it (handled by
        # on_pilot_started_speaking for the moment speech starts; here we still cover the
        # steady-state "pilot is speaking and a NEW candidate showed up" case).
        if state.pilot_speaking and candidate.priority != Priority.URGENT:
            barge_in = state.companion_speaking
            return Decision(
                Verdict.DEFER,
                self._audit("pilot is speaking: deferring non-URGENT", state),
                retry_after_s=PILOT_SPEAKING_RETRY_S,
                barge_in=barge_in,
            )

        # Rule: party comms active -> only URGENT/PRACTICAL allowed, everything else DROPs
        # (an ambient/banter line about to be stale-by-the-time-comms-free is not worth queuing).
        if state.party_comms_active and candidate.priority not in PARTY_COMMS_ALLOWED:
            if candidate.priority in (Priority.AMBIENT, Priority.BANTER):
                return Decision(Verdict.DROP, self._audit("party comms active: AMBIENT/BANTER dropped", state))
            # EVENT: worth a retry, comms are often short.
            return Decision(
                Verdict.DEFER,
                self._audit("party comms active: EVENT deferred", state),
                retry_after_s=PARTY_COMMS_RETRY_S,
            )

        # Rule: hot combat -> only URGENT. EVENT deferred until intensity drops.
        # AMBIENT/BANTER dropped outright.
        if state.in_combat and state.combat_intensity > COMBAT_INTENSITY_THRESHOLD:
            if candidate.priority not in COMBAT_HOT_ALLOWED:
                if candidate.priority in (Priority.AMBIENT, Priority.BANTER):
                    return Decision(Verdict.DROP, self._audit("hot combat: AMBIENT/BANTER dropped", state))
                if candidate.priority == Priority.EVENT:
                    return Decision(
                        Verdict.DEFER,
                        self._audit("hot combat: EVENT deferred until intensity drops", state),
                        retry_after_s=COMBAT_EVENT_RETRY_S,
                    )
                # PRACTICAL falls here too under strict "only URGENT" combat gating.
                return Decision(
                    Verdict.DEFER,
                    self._audit("hot combat: deferred, only URGENT allowed", state),
                    retry_after_s=COMBAT_EVENT_RETRY_S,
                )

        # Rule: staleness. An EVENT older than the threshold is DROPPED outright - a late
        # reaction is worse than none. (Checked before cooldowns/budget: an already-stale
        # event should never occupy a cooldown slot or budget slot.)
        if candidate.priority == Priority.EVENT:
            age = now - candidate.created_at
            if age > EVENT_STALE_AFTER_S:
                return Decision(Verdict.DROP, self._audit(f"EVENT stale ({age:.1f}s old): dropped", state))

        # Rule: cooldowns - global minimum gap, plus per-priority gap. URGENT is exempt from
        # both (no cooldown at all).
        if candidate.priority != Priority.URGENT:
            if state.last_spoken_at is not None:
                gap = now - state.last_spoken_at
                if gap < GLOBAL_MIN_GAP_S:
                    return Decision(
                        Verdict.DEFER,
                        self._audit(f"global cooldown: {gap:.1f}s < {GLOBAL_MIN_GAP_S}s since last line", state),
                        retry_after_s=default_retry(GLOBAL_MIN_GAP_S - gap),
                    )
            per_prio_last = state.last_spoken_at_by_priority.get(candidate.priority)
            prio_gap_required = PRIORITY_COOLDOWN_S[candidate.priority]
            if per_prio_last is not None and prio_gap_required > 0.0:
                gap = now - per_prio_last
                if gap < prio_gap_required:
                    return Decision(
                        Verdict.DEFER,
                        self._audit(
                            f"{candidate.priority.value} cooldown: {gap:.1f}s < {prio_gap_required}s",
                            state,
                        ),
                        retry_after_s=default_retry(prio_gap_required - gap),
                    )

        # Rule: rolling quiet budget for AMBIENT+BANTER.
        if candidate.priority in QUIET_BUDGET_PRIORITIES:
            recent = [t for t in state.quiet_budget_log if now - t < QUIET_BUDGET_WINDOW_S]
            if len(recent) >= QUIET_BUDGET_MAX_LINES:
                return Decision(
                    Verdict.DEFER,
                    self._audit(
                        f"quiet budget exhausted: {len(recent)}/{QUIET_BUDGET_MAX_LINES} "
                        f"AMBIENT+BANTER lines in last {QUIET_BUDGET_WINDOW_S:.0f}s",
                        state,
                    ),
                    retry_after_s=QUIET_BUDGET_RETRY_S,
                )

        return Decision(Verdict.ALLOW, self._audit("clear to speak", state))

    def on_pilot_started_speaking(self, state: SpeakState) -> bool:
        """Call the moment mic VAD flips to "pilot speaking". Returns whether the caller should
        stop the companion's current speech mid-line (barge-in)."""
        return state.companion_speaking

    def record_spoken(self, state: SpeakState, candidate: Candidate) -> None:
        """Caller invokes this after actually speaking `candidate` (i.e. after an ALLOW was
        acted on) so future cooldown/budget checks see it."""
        now = self._now()
        state.last_spoken_at = now
        state.last_spoken_at_by_priority[candidate.priority] = now
        if candidate.priority in QUIET_BUDGET_PRIORITIES:
            state.quiet_budget_log.append(now)
            # trim anything outside the window so the log doesn't grow unbounded
            state.quiet_budget_log = [
                t for t in state.quiet_budget_log if now - t < QUIET_BUDGET_WINDOW_S
            ]

    # -- internal -------------------------------------------------------

    @staticmethod
    def _audit(reason: str, state: SpeakState) -> str:
        # stream_safe never changes timing, but per the spec it must be recorded in the reason
        # for auditing.
        return f"{reason} [stream_safe={state.stream_safe}]"


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def _selftest() -> bool:
    results: List[bool] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        results.append(cond)
        status = "PASS" if cond else "FAIL"
        print(f"[{status}] {name}" + (f" - {detail}" if detail and not cond else ""))

    # Fake clock: a mutable box so tests can advance it without sleeping.
    clock = {"t": 1000.0}

    def now() -> float:
        return clock["t"]

    def advance(dt: float) -> None:
        clock["t"] += dt

    def fresh_state(**overrides) -> SpeakState:
        s = SpeakState()
        for k, v in overrides.items():
            setattr(s, k, v)
        return s

    def cand(priority: Priority, age_s: float = 0.0, speaker: str = "elah", words: int = 8) -> Candidate:
        return Candidate(priority=priority, speaker=speaker, text_len_words=words, created_at=now() - age_s)

    gate = SpeakGate(now=now)

    # --- mute: absolute, drops everything including URGENT ---
    clock["t"] = 1000.0
    s = fresh_state(muted=True)
    d = gate.evaluate(s, cand(Priority.URGENT))
    check("mute drops URGENT", d.verdict == Verdict.DROP, d.reason)
    d = gate.evaluate(s, cand(Priority.AMBIENT))
    check("mute drops AMBIENT", d.verdict == Verdict.DROP, d.reason)

    # --- pilot speaking: defers non-URGENT, allows URGENT, barge-in when companion speaking ---
    s = fresh_state(pilot_speaking=True, companion_speaking=True)
    d = gate.evaluate(s, cand(Priority.PRACTICAL))
    check("pilot speaking defers PRACTICAL", d.verdict == Verdict.DEFER and d.retry_after_s == PILOT_SPEAKING_RETRY_S, d.reason)
    check("pilot speaking + companion speaking -> barge_in True", d.barge_in is True)
    d = gate.evaluate(s, cand(Priority.URGENT))
    check("pilot speaking still allows URGENT", d.verdict == Verdict.ALLOW, d.reason)
    barge = gate.on_pilot_started_speaking(fresh_state(companion_speaking=True))
    check("on_pilot_started_speaking signals stop when companion mid-line", barge is True)
    barge2 = gate.on_pilot_started_speaking(fresh_state(companion_speaking=False))
    check("on_pilot_started_speaking no-op when companion silent", barge2 is False)

    # --- party comms: only URGENT/PRACTICAL allowed ---
    s = fresh_state(party_comms_active=True)
    d = gate.evaluate(s, cand(Priority.PRACTICAL))
    check("party comms allows PRACTICAL", d.verdict == Verdict.ALLOW, d.reason)
    d = gate.evaluate(s, cand(Priority.AMBIENT))
    check("party comms drops AMBIENT", d.verdict == Verdict.DROP, d.reason)
    d = gate.evaluate(s, cand(Priority.BANTER))
    check("party comms drops BANTER", d.verdict == Verdict.DROP, d.reason)
    d = gate.evaluate(s, cand(Priority.EVENT))
    check("party comms defers EVENT", d.verdict == Verdict.DEFER and d.retry_after_s == PARTY_COMMS_RETRY_S, d.reason)

    # --- combat: hot combat only URGENT; EVENT deferred; AMBIENT/BANTER dropped ---
    s = fresh_state(in_combat=True, combat_intensity=0.9)
    d = gate.evaluate(s, cand(Priority.URGENT))
    check("hot combat allows URGENT", d.verdict == Verdict.ALLOW, d.reason)
    d = gate.evaluate(s, cand(Priority.EVENT))
    check("hot combat defers EVENT", d.verdict == Verdict.DEFER and d.retry_after_s == COMBAT_EVENT_RETRY_S, d.reason)
    d = gate.evaluate(s, cand(Priority.AMBIENT))
    check("hot combat drops AMBIENT", d.verdict == Verdict.DROP, d.reason)
    d = gate.evaluate(s, cand(Priority.PRACTICAL))
    check("hot combat defers PRACTICAL (not in allow-set)", d.verdict == Verdict.DEFER, d.reason)
    # combat below threshold does not gate at all
    s = fresh_state(in_combat=True, combat_intensity=0.2)
    d = gate.evaluate(s, cand(Priority.AMBIENT))
    check("sub-threshold combat does not gate AMBIENT", d.verdict == Verdict.ALLOW, d.reason)

    # --- staleness: EVENT older than threshold is dropped ---
    s = fresh_state()
    d = gate.evaluate(s, cand(Priority.EVENT, age_s=EVENT_STALE_AFTER_S + 1))
    check("stale EVENT dropped", d.verdict == Verdict.DROP, d.reason)
    d = gate.evaluate(s, cand(Priority.EVENT, age_s=EVENT_STALE_AFTER_S - 1))
    check("fresh EVENT not dropped by staleness", d.verdict == Verdict.ALLOW, d.reason)

    # --- cooldowns: global gap ---
    s = fresh_state()
    s.last_spoken_at = now() - 1.0  # 1s ago, global min gap is GLOBAL_MIN_GAP_S
    d = gate.evaluate(s, cand(Priority.EVENT))
    check("global cooldown defers too-soon EVENT", d.verdict == Verdict.DEFER, d.reason)
    check(
        "global cooldown retry_after matches remaining gap",
        d.retry_after_s is not None and abs(d.retry_after_s - (GLOBAL_MIN_GAP_S - 1.0)) < 1e-9,
        str(d.retry_after_s),
    )
    # cooldown expiry: advance the clock past the gap, same state, now allowed
    advance(GLOBAL_MIN_GAP_S)
    d = gate.evaluate(s, cand(Priority.EVENT))
    check("global cooldown expires after enough time passes", d.verdict == Verdict.ALLOW, d.reason)

    # --- per-priority cooldown (AMBIENT long gap) ---
    s = fresh_state()
    s.last_spoken_at = now() - 500.0  # clears global gap
    s.last_spoken_at_by_priority[Priority.AMBIENT] = now() - 5.0
    d = gate.evaluate(s, cand(Priority.AMBIENT))
    check("AMBIENT per-priority cooldown defers", d.verdict == Verdict.DEFER, d.reason)
    s.last_spoken_at_by_priority[Priority.AMBIENT] = now() - (PRIORITY_COOLDOWN_S[Priority.AMBIENT] + 1)
    d = gate.evaluate(s, cand(Priority.AMBIENT))
    check("AMBIENT per-priority cooldown allows once expired", d.verdict == Verdict.ALLOW, d.reason)

    # --- URGENT bypasses all cooldowns ---
    s = fresh_state()
    s.last_spoken_at = now()  # just spoke this instant
    s.last_spoken_at_by_priority[Priority.URGENT] = now()
    d = gate.evaluate(s, cand(Priority.URGENT))
    check("URGENT bypasses global+own cooldown", d.verdict == Verdict.ALLOW, d.reason)

    # --- rolling quiet budget ---
    s = fresh_state()
    s.last_spoken_at = now() - 500.0
    # fill the budget with MAX_LINES recent AMBIENT/BANTER timestamps
    for i in range(QUIET_BUDGET_MAX_LINES):
        s.quiet_budget_log.append(now() - (i + 1) * 10)
    d = gate.evaluate(s, cand(Priority.BANTER))
    check("quiet budget exhausted defers BANTER", d.verdict == Verdict.DEFER and d.retry_after_s == QUIET_BUDGET_RETRY_S, d.reason)
    # a line outside the rolling window doesn't count against the budget
    s2 = fresh_state()
    s2.last_spoken_at = now() - 500.0
    for i in range(QUIET_BUDGET_MAX_LINES):
        s2.quiet_budget_log.append(now() - QUIET_BUDGET_WINDOW_S - (i + 1))
    d = gate.evaluate(s2, cand(Priority.AMBIENT))
    check("quiet budget ignores entries outside the rolling window", d.verdict == Verdict.ALLOW, d.reason)
    # URGENT/PRACTICAL/EVENT are not subject to the quiet budget at all
    s3 = fresh_state()
    s3.last_spoken_at = now() - 500.0
    for i in range(QUIET_BUDGET_MAX_LINES + 2):
        s3.quiet_budget_log.append(now() - (i + 1))
    d = gate.evaluate(s3, cand(Priority.PRACTICAL))
    check("quiet budget does not apply to PRACTICAL", d.verdict == Verdict.ALLOW, d.reason)

    # --- record_spoken updates cooldown + budget bookkeeping ---
    s = fresh_state()
    before = len(s.quiet_budget_log)
    gate.record_spoken(s, cand(Priority.AMBIENT))
    check("record_spoken sets last_spoken_at", s.last_spoken_at == now())
    check("record_spoken sets per-priority last spoken", s.last_spoken_at_by_priority[Priority.AMBIENT] == now())
    check("record_spoken appends to quiet budget log for AMBIENT", len(s.quiet_budget_log) == before + 1)
    s2 = fresh_state()
    gate.record_spoken(s2, cand(Priority.URGENT))
    check("record_spoken does not budget-log URGENT", len(s2.quiet_budget_log) == 0)

    # --- stream_safe recorded in the reason, timing unaffected ---
    s_safe = fresh_state(stream_safe=True)
    s_unsafe = fresh_state(stream_safe=False)
    d_safe = gate.evaluate(s_safe, cand(Priority.EVENT))
    d_unsafe = gate.evaluate(s_unsafe, cand(Priority.EVENT))
    check("stream_safe does not change verdict", d_safe.verdict == d_unsafe.verdict == Verdict.ALLOW)
    check("stream_safe=True recorded in reason", "stream_safe=True" in d_safe.reason, d_safe.reason)
    check("stream_safe=False recorded in reason", "stream_safe=False" in d_unsafe.reason, d_unsafe.reason)

    # --- default clear state: plain ALLOW ---
    d = gate.evaluate(fresh_state(), cand(Priority.AMBIENT))
    check("quiet clear state allows AMBIENT", d.verdict == Verdict.ALLOW, d.reason)

    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return passed == total


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        ok = _selftest()
        sys.exit(0 if ok else 1)
    print(__doc__)
