"""pacing.py - one chattiness dial (0..4) and a "not now" snooze for the SuitMk2 companion.

The pilot gets ONE knob instead of fifteen thresholds:

    0 silent       only URGENT (collision, death) and direct answers to a question the pilot asked
    1 quiet
    2 normal       == speak_gate.py's shipped defaults, exactly (upgrading changes nothing until the dial moves)
    3 chatty
    4 very chatty

Each level maps to concrete SpeakGate parameters (per-priority cooldowns, global gap, quiet-budget lines per window),
plus two things that live outside the gate: the core's ambient tick interval and the BanterPolicy exchange cap.
URGENT is never throttled by the dial (its cooldown is 0 at every level; it is not in the quiet budget), and
direct answers go through CompanionCore.answer() as URGENT, so they are never throttled either. Hard mute still
beats everything, as before: pacing only ever ADDS refusals ahead of the gate; it never removes one.

HOW IT REACHES A LIVE GATE WITHOUT EDITING speak_gate.py
speak_gate's thresholds are MODULE CONSTANTS read as globals inside evaluate()/record_spoken(). Changing them
module-wide would re-tune every gate in the process (and every selftest). So apply() gives ONE gate instance its
own view: it rebinds that instance's evaluate/record_spoken to copies of the same functions whose globals are an
overlay dict (speak_gate's namespace + this level's values). Same code, same audit strings, per-instance numbers.
Re-applying mutates that overlay in place, so a slider move takes effect on the very next evaluate().
The overlay is a bridge; PROPOSED_SPEAK_GATE_PATCH below is the small change that retires it, and apply() already
prefers a gate that exposes `set_params` (the patched API) over the overlay.

NotNow: snooze every non-URGENT line for N minutes (default 10), cancellable, clock injected. Installed by apply()
as a pre-check, so it also covers banter starts and ambient ticks; lines ALREADY queued for realization and
in-flight banter turns 2-3 need the companion_core patch (see report) to be caught.

    python pacing.py --selftest
    python pacing.py --mutation-check
    python pacing.py --table
"""
from __future__ import annotations

import dataclasses
import sys
import threading
import time
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import speak_gate as sg                                   # noqa: E402
from speak_gate import Decision, Priority, Verdict        # noqa: E402

LEVEL_NAMES = ("silent", "quiet", "normal", "chatty", "very chatty")
DEFAULT_LEVEL = 2
NOT_NOW_DEFAULT_MIN = 10.0


@dataclass(frozen=True)
class PacingParams:
    level: int
    global_min_gap_s: float
    cooldown_s: Dict[Priority, float] = field(hash=False)
    quiet_budget_max_lines: int            # AMBIENT+BANTER lines per QUIET_BUDGET_WINDOW_S (600 s, unchanged)
    ambient_every_s: float                 # CompanionCore.ambient_every_s
    banter_gap_min: float                  # BanterPolicy.min_gap_s / 60
    allowed: frozenset                     # priorities the dial lets through to the gate at all


_ALL = frozenset(Priority)
_P = Priority
# Level 0's numbers are deliberately finite: the silent pre-check DROPs non-URGENT before these are ever read, and a
# finite value keeps retry_after_s sane if anything bypasses it. Level 2 must equal speak_gate's constants.
_TABLE = {
    #   gap   PRACTICAL  EVENT  AMBIENT  BANTER  budget  ambient_tick  banter_min  allowed
    0: (8.0, 3600.0, 3600.0, 3600.0, 3600.0, 0, 300.0, 1440.0, frozenset({_P.URGENT})),
    # banter_min halved after a dry run (banter needs to fire off more): 40/20/12/8 -> 25/10/6/4.
    1: (6.0, 30.0, 90.0, 420.0, 600.0, 2, 180.0, 25.0, _ALL),
    2: (4.0, 20.0, 45.0, 180.0, 240.0, 4, 90.0, 10.0, _ALL),
    3: (3.0, 15.0, 30.0, 120.0, 150.0, 6, 60.0, 6.0, _ALL),
    4: (2.5, 10.0, 20.0, 75.0, 90.0, 9, 40.0, 4.0, _ALL),
}


def clamp_level(level) -> int:
    try:
        return max(0, min(4, int(level)))
    except (TypeError, ValueError):
        return DEFAULT_LEVEL


# TALK ABOUT WHAT THE EYES SAW: a second dial, same five names (cooldown periods
# for chatting about what it sees, with a chattiness slider of their own). How often the eyes take a picture is
# one setting (picture_pace.py); how often a picture may become a spoken line is this one. Seconds that must pass
# after a line about something the eyes saw before the next one; None = no such lines at all.
# Level 2 is 240 s, which is what it was before there was a dial (an unprompted look every 240 s at most). Level 4 is
# no wait of its own. Like the main dial it only ever ADDS a refusal ahead of the gate: the gate, the main dial, Mute,
# AFK, a fight and the hardware limits all still apply to a line this lets through. An answer to "look at that" is an
# answer to the pilot, not a line of the eyes' own, and is not held by this.
EYE_TALK_GAP_S = {0: None, 1: 600.0, 2: 240.0, 3: 90.0, 4: 0.0}


def eye_talk_gap_s(level) -> Optional[float]:
    return EYE_TALK_GAP_S[clamp_level(level)]


def params_for(level: int) -> PacingParams:
    level = clamp_level(level)
    gap, pr, ev, am, ba, budget, tick, banter, allowed = _TABLE[level]
    return PacingParams(level=level, global_min_gap_s=gap,
                        cooldown_s={_P.URGENT: 0.0, _P.PRACTICAL: pr, _P.EVENT: ev, _P.AMBIENT: am, _P.BANTER: ba},
                        quiet_budget_max_lines=budget, ambient_every_s=tick, banter_gap_min=banter, allowed=allowed)


# ---------------------------------------------------------------------------------------------------------------------
# NotNow
# ---------------------------------------------------------------------------------------------------------------------
class NotNow:
    """Snooze all non-URGENT speech until a deadline. Thread-safe (the hotkey fires off the UI thread)."""

    def __init__(self, now: Callable[[], float] = time.time):
        self.now = now
        self._until: Optional[float] = None
        self._lock = threading.Lock()

    def snooze(self, minutes: float = NOT_NOW_DEFAULT_MIN) -> float:
        """Start (or extend to) a snooze of `minutes` from now. Never SHORTENS an active one. Returns the deadline."""
        with self._lock:
            end = self.now() + max(0.0, float(minutes)) * 60.0
            if self._until is None or end > self._until:
                self._until = end
            return self._until

    def cancel(self) -> None:
        with self._lock:
            self._until = None

    def remaining_s(self) -> float:
        with self._lock:
            if self._until is None:
                return 0.0
            left = self._until - self.now()
            if left <= 0:
                self._until = None
                return 0.0
            return left

    def active(self) -> bool:
        return self.remaining_s() > 0


# ---------------------------------------------------------------------------------------------------------------------
# The pre-check (a module function so --mutation-check can swap it)
# ---------------------------------------------------------------------------------------------------------------------
def _pre_check(params: PacingParams, not_now: Optional[NotNow], priority: Priority) -> Optional[str]:
    """A reason to DROP before the gate runs, or None. URGENT is never refused here."""
    if priority == Priority.URGENT:
        return None
    if priority not in params.allowed:
        return f"pacing: {LEVEL_NAMES[params.level]} (level {params.level}) allows only URGENT + direct answers"
    if not_now is not None:
        left = not_now.remaining_s()
        if left > 0:
            return f"pacing: not now ({left:.0f}s left)"
    return None


def _overlay_values(p: PacingParams) -> dict:
    return {"GLOBAL_MIN_GAP_S": p.global_min_gap_s,
            "PRIORITY_COOLDOWN_S": dict(p.cooldown_s),
            "QUIET_BUDGET_MAX_LINES": p.quiet_budget_max_lines}


class Pacer:
    """What apply() installs on one gate. Keep the reference to call set_level() / not_now later."""

    def __init__(self, gate, core=None, not_now: Optional[NotNow] = None, level: int = DEFAULT_LEVEL):
        self.gate, self.core = gate, core
        self.not_now = not_now if not_now is not None else NotNow(getattr(gate, "_now", time.time))
        self.params = params_for(level)
        self._native = hasattr(gate, "set_params")          # the patched speak_gate API, if it has landed
        self._overlay: Optional[dict] = None
        self._install()
        self.set_level(level)

    def _install(self) -> None:
        g = self.gate
        if self._native:
            self._inner_eval = g.evaluate
        else:
            self._overlay = dict(vars(sg))                    # speak_gate's namespace; its classes are the SAME objects
            ev, rs = sg.SpeakGate.evaluate, sg.SpeakGate.record_spoken
            ev2 = types.FunctionType(ev.__code__, self._overlay, ev.__name__, ev.__defaults__, ev.__closure__)
            rs2 = types.FunctionType(rs.__code__, self._overlay, rs.__name__, rs.__defaults__, rs.__closure__)
            self._inner_eval = types.MethodType(ev2, g)
            g.record_spoken = types.MethodType(rs2, g)
        pacer = self

        def evaluate(state, candidate):
            why = _pre_check(pacer.params, pacer.not_now, candidate.priority)
            if why is not None:
                return Decision(Verdict.DROP, g._audit(why, state))
            return pacer._inner_eval(state, candidate)

        g.evaluate = evaluate
        g._pacer = self

    def set_level(self, level: int) -> PacingParams:
        self.params = p = params_for(level)
        if self._native:
            self.gate.set_params(**_overlay_values(p))
        else:
            self._overlay.update(_overlay_values(p))
        c = self.core
        if c is not None:
            c.ambient_every_s = p.ambient_every_s                 # read at the top of each ambient wait
            if getattr(c, "banter", None) is not None:
                c.banter.min_gap_s = p.banter_gap_min * 60.0
        return p


def apply(level: int, gate, core_or_none=None, not_now: Optional[NotNow] = None) -> Pacer:
    """Set chattiness `level` on a live SpeakGate (and the core's ambient tick + banter cap, if given).
    Idempotent: calling again on the same gate re-levels it in place (no stacked wrappers) and keeps its NotNow
    unless a new one is passed."""
    prev = getattr(gate, "_pacer", None)
    if prev is not None:
        if not_now is not None:
            prev.not_now = not_now
        if core_or_none is not None:
            prev.core = core_or_none
        prev.set_level(level)
        return prev
    return Pacer(gate, core_or_none, not_now, level)


# ---------------------------------------------------------------------------------------------------------------------
# Proposed minimal speak_gate.py change (text only; this module does not apply it)
# ---------------------------------------------------------------------------------------------------------------------
PROPOSED_SPEAK_GATE_PATCH = r'''
--- speak_gate.py
+++ speak_gate.py
@@ class SpeakGate:
     def __init__(self, now: Callable[[], float]):
         self._now = now
+        # Per-instance tunables (pacing.py). Default to the module constants, so behaviour is unchanged.
+        self.global_min_gap_s = GLOBAL_MIN_GAP_S
+        self.priority_cooldown_s = dict(PRIORITY_COOLDOWN_S)
+        self.quiet_budget_max_lines = QUIET_BUDGET_MAX_LINES
+
+    def set_params(self, GLOBAL_MIN_GAP_S=None, PRIORITY_COOLDOWN_S=None, QUIET_BUDGET_MAX_LINES=None):
+        if GLOBAL_MIN_GAP_S is not None:
+            self.global_min_gap_s = float(GLOBAL_MIN_GAP_S)
+        if PRIORITY_COOLDOWN_S is not None:
+            self.priority_cooldown_s = dict(PRIORITY_COOLDOWN_S)
+        if QUIET_BUDGET_MAX_LINES is not None:
+            self.quiet_budget_max_lines = int(QUIET_BUDGET_MAX_LINES)
@@ def evaluate(self, state, candidate):
-                if gap < GLOBAL_MIN_GAP_S:
+                if gap < self.global_min_gap_s:
                     return Decision(
                         Verdict.DEFER,
-                        self._audit(f"global cooldown: {gap:.1f}s < {GLOBAL_MIN_GAP_S}s since last line", state),
-                        retry_after_s=default_retry(GLOBAL_MIN_GAP_S - gap),
+                        self._audit(f"global cooldown: {gap:.1f}s < {self.global_min_gap_s}s since last line", state),
+                        retry_after_s=default_retry(self.global_min_gap_s - gap),
                     )
             per_prio_last = state.last_spoken_at_by_priority.get(candidate.priority)
-            prio_gap_required = PRIORITY_COOLDOWN_S[candidate.priority]
+            prio_gap_required = self.priority_cooldown_s[candidate.priority]
@@
-            if len(recent) >= QUIET_BUDGET_MAX_LINES:
+            if len(recent) >= self.quiet_budget_max_lines:
                 return Decision(
                     Verdict.DEFER,
                     self._audit(
-                        f"quiet budget exhausted: {len(recent)}/{QUIET_BUDGET_MAX_LINES} "
+                        f"quiet budget exhausted: {len(recent)}/{self.quiet_budget_max_lines} "
'''


# ---------------------------------------------------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------------------------------------------------
def _selftest(verbose: bool = True) -> int:
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        if verbose:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))

    clock = {"t": 10_000.0}
    now = lambda: clock["t"]                                          # noqa: E731

    def cand(p, age=0.0):
        return sg.Candidate(priority=p, speaker="elah", text_len_words=8, created_at=now() - age)

    # 1. the table is monotonic: more chatty => shorter cooldowns, bigger budget, shorter tick and banter cap
    ps = [params_for(i) for i in range(5)]
    for a, b in zip(ps, ps[1:]):
        tag = f"{a.level}->{b.level}"
        check(f"global gap non-increasing {tag}", b.global_min_gap_s <= a.global_min_gap_s)
        for pr in (_P.PRACTICAL, _P.EVENT, _P.AMBIENT, _P.BANTER):
            check(f"{pr.value} cooldown strictly shorter {tag}", b.cooldown_s[pr] < a.cooldown_s[pr],
                  f"{a.cooldown_s[pr]} -> {b.cooldown_s[pr]}")
        check(f"quiet budget strictly bigger {tag}", b.quiet_budget_max_lines > a.quiet_budget_max_lines)
        check(f"ambient tick strictly shorter {tag}", b.ambient_every_s < a.ambient_every_s)
        check(f"banter cap strictly shorter {tag}", b.banter_gap_min < a.banter_gap_min)
        check(f"allowed set never shrinks {tag}", a.allowed <= b.allowed)
    check("URGENT cooldown is 0 at every level", all(p.cooldown_s[_P.URGENT] == 0.0 for p in ps))
    check("URGENT allowed at every level", all(_P.URGENT in p.allowed for p in ps))
    n = params_for(2)
    check("level 2 == speak_gate shipped constants",
          n.global_min_gap_s == sg.GLOBAL_MIN_GAP_S and n.cooldown_s == sg.PRIORITY_COOLDOWN_S
          and n.quiet_budget_max_lines == sg.QUIET_BUDGET_MAX_LINES)
    check("out-of-range levels clamp", params_for(-3).level == 0 and params_for(99).level == 4
          and params_for("x").level == DEFAULT_LEVEL)
    gaps = [eye_talk_gap_s(i) for i in range(5)]
    check("eye talk: silent is never, then each level waits less, very chatty does not wait",
          gaps[0] is None and gaps[1] > gaps[2] > gaps[3] > gaps[4] == 0.0 and eye_talk_gap_s("x") == gaps[2])

    # 2. level 0: ambient/banter/event/practical dropped, URGENT allowed, mute still beats URGENT
    g = sg.SpeakGate(now=now)
    pacer = apply(0, g)
    s = sg.SpeakState()
    for pr in (_P.AMBIENT, _P.BANTER, _P.EVENT, _P.PRACTICAL):
        d = g.evaluate(s, cand(pr))
        check(f"level 0 drops {pr.value}", d.verdict is Verdict.DROP and "pacing" in d.reason, d.reason)
    d = g.evaluate(s, cand(_P.URGENT))
    check("level 0 allows URGENT (direct answers are URGENT)", d.verdict is Verdict.ALLOW, d.reason)
    d = g.evaluate(sg.SpeakState(muted=True), cand(_P.URGENT))
    check("hard mute still drops URGENT under pacing", d.verdict is Verdict.DROP and "muted" in d.reason)

    # 3. the overlay is per-instance and live
    apply(4, g)
    check("re-apply returns the same Pacer (no stacked wrappers)", apply(4, g) is pacer)
    other = sg.SpeakGate(now=now)
    s = sg.SpeakState(last_spoken_at=now() - 500)
    s.last_spoken_at_by_priority[_P.AMBIENT] = now() - 100          # 100 s: level-4 cooldown 75, default 180
    check("level 4: AMBIENT allowed 100 s after the last one",
          g.evaluate(s, cand(_P.AMBIENT)).verdict is Verdict.ALLOW)
    check("an untouched gate still uses the 180 s default",
          other.evaluate(s, cand(_P.AMBIENT)).verdict is Verdict.DEFER)
    check("module constants were not mutated", sg.PRIORITY_COOLDOWN_S[_P.AMBIENT] == 180.0
          and sg.QUIET_BUDGET_MAX_LINES == 4 and sg.GLOBAL_MIN_GAP_S == 4.0)
    apply(1, g)
    check("slider back to 1: same state now DEFERs (420 s)",
          g.evaluate(s, cand(_P.AMBIENT)).verdict is Verdict.DEFER)
    s = sg.SpeakState(last_spoken_at=now() - 500)
    s.quiet_budget_log = [now() - 30 * (i + 1) for i in range(5)]   # 5 recent AMBIENT/BANTER lines
    apply(4, g)
    check("level 4 budget (9) allows a 6th AMBIENT line", g.evaluate(s, cand(_P.AMBIENT)).verdict is Verdict.ALLOW)
    apply(2, g)
    d = g.evaluate(s, cand(_P.AMBIENT))
    check("level 2 budget (4) defers it", d.verdict is Verdict.DEFER and "quiet budget" in d.reason, d.reason)
    g.record_spoken(s, cand(_P.AMBIENT))
    check("record_spoken still books the line", s.last_spoken_at == now() and len(s.quiet_budget_log) == 6)

    # 4. core knobs: ambient tick and banter cap
    class Banter:
        min_gap_s = 1200.0

    class Core:
        ambient_every_s = 90.0
        banter = Banter()
    core = Core()
    apply(3, g, core)
    check("core ambient tick set (level 3 -> 60 s)", core.ambient_every_s == 60.0)
    check("banter cap set (level 3 -> 6 min)", core.banter.min_gap_s == 360.0)
    apply(0, g, core)
    check("level 0 banter cap is a day", core.banter.min_gap_s == 1440 * 60.0)

    # 5. NotNow: blocks non-URGENT, lets URGENT through, expires, cancels, never shortens
    apply(2, g)
    nn = pacer.not_now
    free = sg.SpeakState(last_spoken_at=now() - 1000)
    check("before snooze EVENT allowed", g.evaluate(free, cand(_P.EVENT)).verdict is Verdict.ALLOW)
    nn.snooze()                                                      # default 10 min
    d = g.evaluate(free, cand(_P.EVENT))
    check("snoozed: EVENT dropped", d.verdict is Verdict.DROP and "not now" in d.reason, d.reason)
    check("snoozed: BANTER dropped", g.evaluate(free, cand(_P.BANTER)).verdict is Verdict.DROP)
    check("snoozed: URGENT allowed", g.evaluate(free, cand(_P.URGENT)).verdict is Verdict.ALLOW)
    clock["t"] += 599
    check("snoozed at 9m59s: still dropped", g.evaluate(free, cand(_P.PRACTICAL)).verdict is Verdict.DROP)
    clock["t"] += 2
    check("expired at 10m01s", not nn.active())
    check("expired: EVENT allowed again", g.evaluate(free, cand(_P.EVENT)).verdict is Verdict.ALLOW)
    nn.snooze(10)
    nn.cancel()
    check("cancel lifts it immediately", g.evaluate(free, cand(_P.EVENT)).verdict is Verdict.ALLOW)
    nn2 = NotNow(now)
    nn2.snooze(10)
    clock["t"] += 300
    nn2.snooze(1)
    check("snooze(1) at 5 min does not cut a 10-min snooze to 1", abs(nn2.remaining_s() - 300) < 1e-6,
          str(nn2.remaining_s()))

    # 6. native path: a gate with the patched set_params() API is driven through it, never via the overlay
    class Patched(sg.SpeakGate):
        def set_params(self, **kw):
            self.got = kw
    pg = Patched(now=now)
    apply(3, pg)
    check("patched gate: set_params receives the level's values",
          pg.got["GLOBAL_MIN_GAP_S"] == 3.0 and pg.got["QUIET_BUDGET_MAX_LINES"] == 6
          and pg.got["PRIORITY_COOLDOWN_S"][_P.AMBIENT] == 120.0)
    check("patched gate: record_spoken is NOT rebound", "record_spoken" not in vars(pg))
    check("patched gate: pre-check still installed", apply(0, pg).gate.evaluate(
        sg.SpeakState(), cand(_P.AMBIENT)).verdict is Verdict.DROP)

    bad = results.count(False)
    if verbose:
        print(f"pacing selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


def _mutation_check() -> int:
    """Break one guard at a time; the selftest must FAIL each time, and the unmutated baseline must PASS
    (a broken baseline would make every mutant look caught)."""
    global _pre_check
    real = _pre_check
    mutants = {
        "level-0 allow-set ignored": lambda p, nn, pr: real(dataclasses.replace(p, allowed=_ALL), nn, pr),
        "NotNow ignored": lambda p, nn, pr: real(p, None, pr),
        "URGENT refused under NotNow": lambda p, nn, pr: ("pacing: not now" if nn and nn.active()
                                                          else real(p, nn, pr)),
    }
    base = _selftest(verbose=False)
    print(f"  unmutated baseline: {'PASS' if base == 0 else 'FAIL'}")
    caught = 0
    for name, fn in mutants.items():
        _pre_check = fn
        try:
            rc = _selftest(verbose=False)
        finally:
            _pre_check = real
        print(f"  mutant '{name}': {'CAUGHT' if rc else 'SURVIVED'}")
        caught += bool(rc)
    print(f"pacing mutation check: {caught}/{len(mutants)} caught")
    return 0 if caught == len(mutants) and base == 0 else 1


def table() -> str:
    rows = ["level name         gap  PRACT  EVENT  AMBIENT  BANTER  budget/10min  ambient_tick  banter_cap  allowed"]
    for i in range(5):
        p = params_for(i)
        c = p.cooldown_s
        rows.append(f"{i:>5} {LEVEL_NAMES[i]:<11} {p.global_min_gap_s:>4}  {c[_P.PRACTICAL]:>5.0f}  {c[_P.EVENT]:>5.0f}"
                    f"  {c[_P.AMBIENT]:>7.0f}  {c[_P.BANTER]:>6.0f}  {p.quiet_budget_max_lines:>12}"
                    f"  {p.ambient_every_s:>11.0f}s  {p.banter_gap_min:>9.0f}m  "
                    f"{'URGENT only' if p.allowed != _ALL else 'all'}")
    return "\n".join(rows)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--mutation-check" in sys.argv:
        sys.exit(_mutation_check())
    if "--table" in sys.argv:
        print(table())
        sys.exit(0)
    print(__doc__)
