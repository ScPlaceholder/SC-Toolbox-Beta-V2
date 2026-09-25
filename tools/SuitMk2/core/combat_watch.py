"""combat_watch.py - is the pilot in a fight? (J's design, 2026-09-23)

J: "Can we also intercept game audio? We could have combat weapon equipped -> turn on game ears and eyes -> gun
shot + muzzle flash = combat state."

What the evidence allows (measured on his logs the same night):
  - The log records weapons going INTO a slot (AttachmentReceived ... Port[wep_sidearm|wep_stocked_N]) but never
    a weapon being DRAWN. So the log can say "weapon put away" (a combat-OFF hint), not "weapon out".
  - voice_fx.DuckingMonitor already reads StarCitizen.exe's own output PEAK at ~20 Hz, per process: it never hears
    Elah or Montaigne. Gunfire is a train of sharp transients; engines and music are a sustained level. So the
    cheapest "game ears" are transient counting on a meter we already poll: no capture, no model, no GPU.
  - The eyes can confirm (their in_combat flag, or a muzzle flash) when asked, so they only spend a glance when
    the ears say "maybe".

State machine, cheapest signal first:
    QUIET --(>= MAYBE_SPIKES spikes in WINDOW_S)--> MAYBE --(eyes confirm)---------------> ON  "Contact."
                                                     MAYBE --(>= SURE_SPIKES, no eyes)---> ON
    ON --(no spike for CLEAR_S)--> QUIET "Clear."        ON --(weapon holstered)--> QUIET "Clear."
A spike: level >= SPIKE_MIN and >= SPIKE_RATIO x the median of the previous BASELINE_S (a jump, not a loud room),
with a REFRACTORY_S gap so one shot's decay is not counted twice.

Thresholds are GUESSES until tuned on a real session's gunfire; every change of state is logged with the numbers
that caused it, so a tuning pass reads the log instead of guessing again.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from statistics import median
from typing import Callable, Optional

log = logging.getLogger("suitmk2.combat")

SPIKE_MIN = 0.30          # absolute peak a shot must reach (0..1 session peak)
SPIKE_RATIO = 2.5         # ...and this many times the recent median
BASELINE_S = 1.5
REFRACTORY_S = 0.08
ONSET_RATIO = 1.5         # a shot RISES: this many times the previous reading...
ONSET_MIN = 0.15          # ...and by at least this much in absolute terms
WINDOW_S = 2.0
MAYBE_SPIKES = 3
SURE_SPIKES = 5           # audio alone, when there are no eyes to ask
CLEAR_S = 20.0
CONFIRM_TIMEOUT_S = 4.0   # a MAYBE the eyes do not confirm in this long falls back to QUIET


class CombatWatch:
    def __init__(self, on_change: Callable[[str, str], None], confirm: Optional[Callable[[], bool]] = None,
                 now: Callable[[], float] = time.time):
        """on_change(state, reason): state is "on" or "off". confirm(): True = a second sense confirms (the sound
        classifier heard gunfire, or the eyes see combat), False = it looked and says no, None = no second opinion
        (audio alone must then reach SURE_SPIKES). confirm itself may be None: same as always answering None."""
        self.on_change, self.confirm, self.now = on_change, confirm, now
        self.state = "quiet"               # quiet | maybe | on
        self._hist: deque = deque()        # (t, level) over BASELINE_S
        self._spikes: deque = deque()      # spike times over WINDOW_S
        self._last_spike = -1e9
        self._maybe_at = 0.0
        self._lock = threading.Lock()
        self.stats = {"spikes": 0, "maybe": 0, "on": 0, "off": 0, "unconfirmed": 0}

    # -- input --------------------------------------------------------------------------------------------------
    def feed(self, level: float, t: Optional[float] = None) -> None:
        """One meter reading (DuckingMonitor calls this ~20 times a second)."""
        t = self.now() if t is None else t
        fire = None
        with self._lock:
            while self._hist and t - self._hist[0][0] > BASELINE_S:
                self._hist.popleft()
            base = median(v for _, v in self._hist) if self._hist else 0.0
            self._hist.append((t, float(level)))
            # An ONSET, not merely a loud frame: without the rise test a single shot's decay (0.8, 0.7, 0.6...)
            # stayed above SPIKE_RATIO x the bed for several frames and counted as three shots (selftest caught it).
            prev = self._hist[-2][1] if len(self._hist) >= 2 else 0.0
            rising = level >= prev * ONSET_RATIO and level - prev >= ONSET_MIN
            if (rising and level >= SPIKE_MIN and level >= SPIKE_RATIO * max(base, 0.02)
                    and t - self._last_spike >= REFRACTORY_S):
                self._last_spike = t
                self._spikes.append(t)
                self.stats["spikes"] += 1
            while self._spikes and t - self._spikes[0] > WINDOW_S:
                self._spikes.popleft()
            fire = self._step(t)
        if fire:
            self._emit(*fire)

    def holstered(self) -> None:
        """The log saw a weapon go back into a slot: a strong hint the fight is over."""
        fire = None
        with self._lock:
            if self.state == "on":
                self.state = "quiet"
                self.stats["off"] += 1
                fire = ("off", "weapon holstered")
        if fire:
            self._emit(*fire)

    # -- state machine ------------------------------------------------------------------------------------------
    def _step(self, t: float):
        n = len(self._spikes)
        if self.state == "quiet" and n >= MAYBE_SPIKES:
            self.state, self._maybe_at = "maybe", t
            self.stats["maybe"] += 1
        if self.state == "maybe":
            seen = self._confirm()
            if seen is True or (seen is None and n >= SURE_SPIKES):
                self.state = "on"
                self.stats["on"] += 1
                why = "eyes confirmed" if seen else f"{n} shots in {WINDOW_S:.0f}s, no eyes"
                return ("on", f"{why} (spikes={n})")
            if t - self._maybe_at > CONFIRM_TIMEOUT_S:
                self.state = "quiet"
                self.stats["unconfirmed"] += 1
        if self.state == "on" and t - self._last_spike > CLEAR_S:
            self.state = "quiet"
            self.stats["off"] += 1
            return ("off", f"no shots for {CLEAR_S:.0f}s")
        return None

    def _confirm(self) -> Optional[bool]:
        if self.confirm is None:
            return None
        try:
            v = self.confirm()
        except Exception:
            return None
        # None must survive: bool(None) is False, which silently turned "no opinion" into "the eyes said no" and
        # disabled the audio-only SURE_SPIKES path for any confirm that can abstain (the sound classifier can).
        return None if v is None else bool(v)

    def _emit(self, state: str, reason: str) -> None:
        log.info("combat %s: %s", state, reason)
        try:
            self.on_change(state, reason)
        except Exception:
            log.exception("combat on_change")

    @property
    def active(self) -> bool:
        return self.state == "on"


def _selftest() -> int:
    ok = total = 0

    def case(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({detail})" if detail and not cond else ""))

    def run(levels, confirm=None, dt=0.05, start=1000.0, holster_at=None):
        events, clk = [], [start]
        w = CombatWatch(lambda s, r: events.append((round(clk[0] - start, 2), s, r)), confirm, now=lambda: clk[0])
        for i, lv in enumerate(levels):
            clk[0] = start + i * dt
            if holster_at is not None and i == holster_at:
                w.holstered()
            w.feed(lv, clk[0])
        return w, events

    engine = [0.15] * 200                                   # 10 s of loud but steady engine: never combat
    w, ev = run(engine, confirm=None)
    case("a loud steady engine is not combat", not ev and w.stats["spikes"] == 0, str(ev))

    def gunfire(shots, gap_frames=4, bed=0.12):
        out = [bed] * 40
        for _ in range(shots):
            out += [0.8] + [bed] * gap_frames
        return out

    w, ev = run(gunfire(6) + [0.12] * 40, confirm=None)
    case("six shots with no eyes: combat on from audio alone", ev and ev[0][1] == "on", str(ev))
    w, ev = run(gunfire(3) + [0.12] * 100, confirm=None)
    case("three shots with no eyes: maybe only, never on", not ev and w.stats["maybe"] == 1, str(ev))
    w, ev = run(gunfire(3) + [0.12] * 10, confirm=lambda: True)
    case("three shots + eyes confirm: combat on", ev and ev[0][1] == "on" and "eyes" in ev[0][2], str(ev))
    w, ev = run(gunfire(4) + [0.12] * 200, confirm=lambda: False)
    case("eyes say no: the maybe expires, no combat", not ev and w.stats["unconfirmed"] == 1, str(ev))
    w, ev = run(gunfire(6) + [0.12] * int((CLEAR_S + 2) / 0.05), confirm=None)
    case("combat clears after the quiet period", [e[1] for e in ev] == ["on", "off"], str(ev))
    w, ev = run(gunfire(6) + [0.12] * 60, confirm=None, holster_at=len(gunfire(6)) + 20)
    case("holstering ends combat early", [e[1] for e in ev] == ["on", "off"] and "holster" in ev[1][2], str(ev))
    w, ev = run(gunfire(6) + [0.12] * 40, confirm=lambda: None)
    case("a confirm that abstains (None) still lets audio alone decide",
         ev and ev[0][1] == "on" and "no eyes" in ev[0][2], str(ev))
    one = [0.12] * 40 + [0.8, 0.7, 0.6, 0.5, 0.4] + [0.12] * 40
    w, ev = run(one, confirm=None)
    case("one shot's decay counts once (refractory)", w.stats["spikes"] == 1, str(w.stats))
    print(f"combat_watch selftest: {ok}/{total} passed")
    return 0 if ok == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
