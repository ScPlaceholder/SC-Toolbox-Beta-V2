"""topic_ledger.py - acknowledge a subject a couple of times, then talk about something else (2026-09-23).

J, mid dry-run: "Events ended up being covered obsessively rather than acknowledging an event or location a
couple times then talking about other things." That was the original MK2, and ours had the same shape:
build_ambient_spec() returned the FIRST matching situation, so while the pilot sat in one jurisdiction or at one
landing zone every ambient tick was about that same thing. The rotating `variant` changed the wording, never the
subject. Time cooldowns (speak_gate / pacing) limit how OFTEN they talk, not WHAT about.

The ledger counts lines actually SPOKEN per subject. A subject is the scenario plus its first non-numeric claim
value ("jurisdiction|Crusader Security", "extended_stay|Orison"): numbers are excluded on purpose, because
minutes-at-location and visit counts change every tick and would make every line a "new" subject. After
`max_mentions` lines inside `window_s`, the subject is spent: ambient falls through to the next situation (or
silence), and a non-urgent event about it is dropped. Mentions age out of the window, so coming back to a place
an hour later can be acknowledged again.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

MAX_MENTIONS = 2
WINDOW_S = 1800.0


def subject_key(spec: dict) -> str:
    scenario = str(spec.get("scenario", "?"))
    for c in spec.get("claims") or []:
        v = c.get("value")
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, (int, float)):
            continue
        s = str(v).strip()
        if not s or s.replace(",", "").replace(".", "", 1).lstrip("-").isdigit():
            continue
        # Key on WHAT the claim is about (its predicate root: "jurisdiction.zone" -> "jurisdiction"), not on which
        # channel produced the line. J, 2026-09-26: "still obsessed about the most recent event". The event said
        # "event_jurisdiction_change|crusader industries" and the ambient said "jurisdiction|crusader industries",
        # so one subject got two budgets, and banter a third: jurisdiction came up five times in eight minutes.
        pred = str(c.get("predicate") or "")
        root = pred.split(".", 1)[0] if "." in pred else ""
        return f"{root or scenario}|{s.lower()}"
    return scenario


class TopicLedger:
    def __init__(self, max_mentions: int = MAX_MENTIONS, window_s: float = WINDOW_S,
                 now: Callable[[], float] = time.time):
        self.max_mentions, self.window_s, self.now = max_mentions, window_s, now
        self._said: dict[str, list[float]] = {}
        self._lock = threading.Lock()
        self.skipped = 0

    def _recent(self, key: str) -> list[float]:
        cutoff = self.now() - self.window_s
        times = [t for t in self._said.get(key, []) if t >= cutoff]
        if times:
            self._said[key] = times
        else:
            self._said.pop(key, None)
        return times

    def spent(self, spec: dict) -> bool:
        with self._lock:
            hit = len(self._recent(subject_key(spec))) >= self.max_mentions
        if hit:
            self.skipped += 1
        return hit

    def record(self, spec: dict) -> None:
        with self._lock:
            self._said.setdefault(subject_key(spec), []).append(self.now())

    def status(self) -> dict:
        with self._lock:
            return {k: len(self._recent(k)) for k in list(self._said)}


def _selftest() -> int:
    ok = 0
    total = 0

    def case(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        ok += bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name + (f"  ({detail})" if detail and not cond else ""))

    def spec(scn, *vals):
        return {"scenario": scn, "claims": [{"id": f"C{i+1}", "value": v} for i, v in enumerate(vals)]}

    case("key uses the first non-numeric value", subject_key(spec("extended_stay", "Orison", 22))
         == "extended_stay|orison")
    case("key ignores changing minutes", subject_key(spec("extended_stay", "Orison", 23))
         == subject_key(spec("extended_stay", "Orison", 40)))
    case("numeric-only spec keys on scenario", subject_key(spec("busy_session", 4)) == "busy_session")
    case("numeric strings are numbers too", subject_key(spec("reward", "15,000")) == "reward")
    case("bools are skipped", subject_key(spec("jurisdiction", True, "Crusader")) == "jurisdiction|crusader")

    clk = [1000.0]
    led = TopicLedger(now=lambda: clk[0])
    a = spec("jurisdiction", "Crusader Security")
    case("fresh subject is not spent", not led.spent(a))
    led.record(a)
    case("one mention: not spent", not led.spent(a))
    led.record(a)
    case("two mentions: spent", led.spent(a))
    case("a different subject is untouched", not led.spent(spec("jurisdiction", "Hurston Security")))
    clk[0] += WINDOW_S + 1
    case("mentions age out of the window", not led.spent(a))
    case("skips are counted", led.skipped == 1, str(led.skipped))
    print(f"topic_ledger selftest: {ok}/{total} passed")
    return 0 if ok == total else 1


if __name__ == "__main__":
    import sys
    sys.exit(_selftest() if "--selftest" in sys.argv else 0)
