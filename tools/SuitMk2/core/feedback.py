"""feedback.py - tie a pilot's "Good one" / "Shut up" press to the line it was about (SuitMk2).

Two bindable keys, one question: WHICH line was that about? The answer is the MOST RECENT spoken line, and only if it
was spoken within WINDOW_S (15 s). A press outside the window is logged as ignored and attributed to nothing: a
reaction pinned to the wrong line is worse than no reaction, because move_lifecycle promotes and retires rhetorical
moves on exactly this evidence.

    good_one -> move_lifecycle kind "reaction_positive"
    shut_up  -> move_lifecycle kind "mute"   AND a NotNow snooze (pacing.py, default 10 min) AND hush() if given
                (hush = cut the current line; the snooze happens even when the press is too late to attribute,
                because "be quiet" is a request about NOW, not a review of the last line)

At most one reaction of each kind per line: mashing Good one five times is one data point, not five.

Every press appends one row to <log_dir>/feedback_<session>.jsonl (presses only, so the file stays small).
Forwarding is best-effort: no lifecycle, a lifecycle that raises, or a line with no rhetoric move all degrade to
"logged, not forwarded", never to an exception on the hotkey thread.

    python feedback.py --selftest
    python feedback.py --mutation-check
"""
from __future__ import annotations

import json
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

WINDOW_S = 15.0
EVIDENCE_KIND = {"good_one": "reaction_positive", "shut_up": "mute"}
SNOOZE_MIN = 10.0


@dataclass
class SpokenLine:
    spec_id: str
    speaker: str
    move: Optional[str]          # rhetoric move, e.g. "DRY_UNDERSTATEMENT"
    move_id: Optional[str]       # what move_lifecycle keys on: spec["move_id"] if present, else "<speaker>.<move>"
    t: float
    priority: Optional[str] = None
    text: str = ""
    reacted: set = field(default_factory=set)
    # The FULL spec the line was realized from: a liked line becomes a training pair (spec -> text) for
    # the next retrain, so the pilot's taste reaches the weights. Kept out of meta(); written only on an attributed press.
    spec: Optional[dict] = None

    def meta(self) -> dict:
        d = asdict(self)
        d.pop("reacted")
        d.pop("spec", None)
        return d


def move_id_for(spec: dict) -> tuple[Optional[str], Optional[str]]:
    """(move, move_id). A learned move carries its own id (move_lifecycle.realizer_spec); a trained one is
    '<owner>.<MOVE>', mirroring move_lifecycle.trained_id (not imported: this module must load without it)."""
    rh = spec.get("rhetoric") or []
    move = rh[0] if rh else None
    mid = spec.get("move_id") or (f"{spec.get('speaker')}.{move}" if move and spec.get("speaker") else None)
    return move, mid


# Spoken feedback (the keys can be verbal as well). SHORT utterances only, so "shut up and let
# me think about the route" is a question for the lane, not a snooze. Returns 'good_one', 'shut_up' or None.
import re as _re
_GOOD = _re.compile(r"^(?:(?:elah|montaigne|ship|suit)[, ]+)?(?:good one|nice one|nice|(?:ha)+h?|that was (?:good|great|funny)|"
                    r"love (?:it|that)|good line|great line|lol)[.!]*$")
_QUIET = _re.compile(r"^(?:(?:elah|montaigne|ship|suit)[, ]+)?(?:shut up|quiet|be quiet|hush|enough|stop talking|"
                     r"not now|pipe down|zip it)[.!]*$")


def verbal_reaction(text: str) -> Optional[str]:
    t = " ".join(str(text or "").lower().replace(",", ", ").split()).strip(" .!?")
    if not t or len(t.split()) > 5:
        return None
    if _GOOD.match(t):
        return "good_one"
    if _QUIET.match(t):
        return "shut_up"
    return None


def _in_window(dt: float, window_s: float) -> bool:
    """The attribution guard (a module function so --mutation-check can break it)."""
    return 0.0 <= dt <= window_s


class FeedbackRecorder:
    def __init__(self, log_dir, now: Callable[[], float] = time.time, lifecycle=None, not_now=None,
                 hush: Optional[Callable[[], None]] = None, window_s: float = WINDOW_S,
                 snooze_min: float = SNOOZE_MIN, session: Optional[str] = None):
        self.now, self.lifecycle, self.not_now, self.hush = now, lifecycle, not_now, hush
        self.window_s, self.snooze_min = float(window_s), float(snooze_min)
        self.session = session or time.strftime("%Y%m%d_%H%M%S", time.localtime(now()))
        self.log_dir = Path(log_dir)
        self.log_path = self.log_dir / f"feedback_{self.session}.jsonl"
        self._last: Optional[SpokenLine] = None
        self._lock = threading.Lock()
        self.counts = {"good_one": 0, "shut_up": 0, "attributed": 0, "ignored": 0, "forwarded": 0}

    # -- the spoken side (call from the realize worker when speech.say() accepted the line) -----------------------
    def note_spoken(self, spec: dict, text: str = "", priority=None, t: Optional[float] = None) -> SpokenLine:
        move, mid = move_id_for(spec)
        line = SpokenLine(spec_id=str(spec.get("id", "")), speaker=str(spec.get("speaker", "")), move=move,
                          move_id=mid, t=self.now() if t is None else float(t),
                          priority=getattr(priority, "name", None if priority is None else str(priority)),
                          text=(text or ""),            # FULL text: it may become a training target
                          spec=dict(spec) if isinstance(spec, dict) else None)
        with self._lock:
            self._last = line
        return line

    def last_line(self) -> Optional[SpokenLine]:
        with self._lock:
            return self._last

    # -- the pilot side (call from the hotkey handler) ------------------------------------------------------------
    def press(self, reaction: str) -> dict:
        if reaction not in EVIDENCE_KIND:
            raise ValueError(f"unknown reaction {reaction!r}; one of {sorted(EVIDENCE_KIND)}")
        t = self.now()
        kind = EVIDENCE_KIND[reaction]
        with self._lock:
            line = self._last
            dt = None if line is None else t - line.t
            if line is None:
                attributed, why = False, "no line spoken yet"
            elif not _in_window(dt, self.window_s):
                attributed, why = False, f"stale: last line {dt:.1f}s ago (> {self.window_s:.0f}s window)"
            elif reaction in line.reacted:
                attributed, why = False, f"already recorded {reaction} for this line"
            else:
                attributed, why = True, "most recent line, in window"
                line.reacted.add(reaction)
        self.counts[reaction] += 1
        self.counts["attributed" if attributed else "ignored"] += 1

        snoozed_until = None
        if reaction == "shut_up":
            if self.not_now is not None:
                try:
                    snoozed_until = self.not_now.snooze(self.snooze_min)
                except Exception as e:                       # never raise on the hotkey thread
                    why += f"; snooze failed {e!r}"
            if self.hush is not None:
                try:
                    self.hush()
                except Exception as e:
                    why += f"; hush failed {e!r}"

        forwarded = None
        if attributed and self.lifecycle is not None and line.move_id:
            try:
                r = self.lifecycle.record_evidence(line.move_id, kind)
                forwarded = {"weight": (r or {}).get("weight"), "why": (r or {}).get("why")}
                self.counts["forwarded"] += 1
            except Exception as e:
                forwarded = {"error": f"{type(e).__name__}: {e}"}
        elif attributed and not line.move_id:
            why += "; line has no rhetoric move, nothing to forward"

        row = {"t": t, "session": self.session, "reaction": reaction, "evidence_kind": kind,
               "attributed": attributed, "why": why, "dt": None if dt is None else round(dt, 3),
               "line": line.meta() if attributed else None, "forwarded": forwarded, "snoozed_until": snoozed_until,
               "spec": line.spec if attributed else None}
        self._append(row)
        return row

    def _append(self, row: dict) -> None:
        try:
            self.log_dir.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
        except Exception:
            pass                                             # logging must never break the key press


# ---------------------------------------------------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------------------------------------------------
def _selftest(verbose: bool = True) -> int:
    import shutil
    import tempfile
    results = []

    def check(name, cond, detail=""):
        results.append(bool(cond))
        if verbose:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))

    tmp = Path(tempfile.mkdtemp(prefix="suitmk2_feedback_st_"))
    try:
        clock = {"t": 50_000.0}
        now = lambda: clock["t"]                                      # noqa: E731

        class FakeLifecycle:
            def __init__(self):
                self.calls = []

            def record_evidence(self, move_id, kind):
                self.calls.append((move_id, kind))
                return {"weight": 1.0, "why": "attributed"}

        from pacing import NotNow
        lc, nn, hushed = FakeLifecycle(), NotNow(now), []
        fb = FeedbackRecorder(tmp, now=now, lifecycle=lc, not_now=nn, hush=lambda: hushed.append(now()),
                              session="S1")
        spec_a = {"id": "amb_lorville_v0", "speaker": "elah", "rhetoric": ["DRY_UNDERSTATEMENT"]}
        spec_b = {"id": "ban_x_p0_t2", "speaker": "montaigne", "rhetoric": ["ESSAY_DIGRESSION"]}

        r = fb.press("good_one")
        check("press before any line: ignored", not r["attributed"] and r["line"] is None and not lc.calls)

        fb.note_spoken(spec_a, "Lorville. Charming, in the way rust is charming.")
        clock["t"] += 3
        r = fb.press("good_one")
        check("good_one 3 s after line A: attributed to A", r["attributed"] and r["line"]["spec_id"] == spec_a["id"])
        check("forwarded as reaction_positive on elah.DRY_UNDERSTATEMENT",
              lc.calls == [("elah.DRY_UNDERSTATEMENT", "reaction_positive")], str(lc.calls))
        r = fb.press("good_one")
        check("second good_one on the same line: not double-counted", not r["attributed"] and len(lc.calls) == 1)

        clock["t"] += 2
        fb.note_spoken(spec_b, "One is reminded, as ever, of Seneca.")
        clock["t"] += 3
        r = fb.press("shut_up")
        check("shut_up after line B: attributed to B, not A",
              r["attributed"] and r["line"]["spec_id"] == spec_b["id"] and r["line"]["speaker"] == "montaigne")
        check("forwarded as mute on montaigne.ESSAY_DIGRESSION", lc.calls[-1] == ("montaigne.ESSAY_DIGRESSION", "mute"))
        check("shut_up snoozes NotNow for 10 min", nn.active() and abs(nn.remaining_s() - 600) < 1e-6)
        check("shut_up hushes the current line", len(hushed) == 1)

        # window boundary: exactly 15 s is in, just past is out
        nn.cancel()
        fb.note_spoken(spec_a, "x")
        clock["t"] += 15.0
        check("press at exactly 15.0 s: in window", fb.press("good_one")["attributed"])
        fb.note_spoken(spec_a, "y")
        clock["t"] += 15.01
        n_calls = len(lc.calls)
        r = fb.press("good_one")
        check("press at 15.01 s: stale, ignored, not forwarded",
              not r["attributed"] and r["line"] is None and "stale" in r["why"] and len(lc.calls) == n_calls)
        r = fb.press("shut_up")
        check("stale shut_up: still snoozes (a request about now), not forwarded",
              not r["attributed"] and nn.active() and len(lc.calls) == n_calls)

        # a line with no rhetoric move, and a lifecycle that raises
        fb.note_spoken({"id": "evt_x", "speaker": "elah"}, "z")
        r = fb.press("good_one")
        check("line with no move: attributed, nothing forwarded", r["attributed"] and r["forwarded"] is None)

        class Boom:
            def record_evidence(self, *a):
                raise RuntimeError("store locked")
        fb2 = FeedbackRecorder(tmp, now=now, lifecycle=Boom(), session="S2")
        fb2.note_spoken(spec_a)
        r = fb2.press("good_one")
        check("a raising lifecycle degrades to a logged error, no exception", "error" in (r["forwarded"] or {}))
        check("learned move id wins over the trained one",
              move_id_for({"speaker": "elah", "rhetoric": ["X"], "move_id": "elah.learned_7"})[1] == "elah.learned_7")
        try:
            fb.press("meh")
            check("unknown reaction refused", False)
        except ValueError:
            check("unknown reaction refused", True)

        rows = [json.loads(x) for x in (tmp / "feedback_S1.jsonl").read_text(encoding="utf-8").splitlines()]
        check("one JSONL row per valid press (8; the refused one is not logged), per session file", len(rows) == 8 and (tmp / "feedback_S2.jsonl").exists(),
              str(len(rows)))
        check("ignored rows carry no line (nothing misattributed in the log)",
              all(r["line"] is None for r in rows if not r["attributed"]))

        # pacing integration: shut_up through a Pacer's NotNow actually silences the gate
        import speak_gate as sg
        from pacing import apply
        g = sg.SpeakGate(now=now)
        pacer = apply(2, g)
        fb3 = FeedbackRecorder(tmp, now=now, not_now=pacer.not_now, session="S3")
        fb3.press("shut_up")
        ev = sg.Candidate(sg.Priority.EVENT, "elah", 8, now())
        d = g.evaluate(sg.SpeakState(), ev)
        check("shut_up -> gate drops EVENT", d.verdict is sg.Verdict.DROP and "not now" in d.reason, d.reason)
        ur = sg.Candidate(sg.Priority.URGENT, "elah", 8, now())
        check("shut_up -> URGENT still allowed", g.evaluate(sg.SpeakState(), ur).verdict is sg.Verdict.ALLOW)

        # real move_lifecycle, if it imports: its FAST_WINDOW_S (5 s) is narrower than ours (15 s)
        try:
            import memory_store as ms
            from move_lifecycle import MoveLifecycle
            from ambient_spec import _ELAH_MOVES
        except Exception as e:                                        # an optional module; absence is not a FAIL
            if verbose:
                print(f"  [SKIP] real move_lifecycle integration ({type(e).__name__}: {e})")
        else:
            store = ms.open_store(tmp / "mem", "p1")
            mv = sorted(_ELAH_MOVES)[0]
            real = MoveLifecycle(store, now=now)
            fb4 = FeedbackRecorder(tmp, now=now, lifecycle=real, session="S4")
            spec = {"id": "amb_t", "speaker": "elah", "rhetoric": [mv]}
            real.record_use(f"elah.{mv}", "S4")
            fb4.note_spoken(spec)
            clock["t"] += 3
            r = fb4.press("shut_up")
            check("real lifecycle: shut_up at 3 s weighs -1", r["forwarded"] and r["forwarded"]["weight"] == -1.0,
                  str(r["forwarded"]))
            real.record_use(f"elah.{mv}", "S4")
            fb4.note_spoken(spec)
            clock["t"] += 10
            r = fb4.press("shut_up")
            check("real lifecycle: shut_up at 10 s counts in BOTH (windows aligned at 15 s, 2026-09-23)",
                  r["attributed"] and r["forwarded"]["weight"] == -1.0, str(r["forwarded"]))
            real.record_use(f"elah.{mv}", "S4")
            fb4.note_spoken(spec)
            clock["t"] += 20
            r = fb4.press("shut_up")
            check("real lifecycle: shut_up at 20 s is attributed by neither", not r["attributed"], str(r))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    bad = results.count(False)
    if verbose:
        print(f"feedback selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


def _mutation_check() -> int:
    global _in_window
    real = _in_window
    mutants = {"window guard removed (any age attributes)": lambda dt, w: dt >= 0.0,
               "window off by one (strict <)": lambda dt, w: 0.0 <= dt < w}
    base = _selftest(verbose=False)
    print(f"  unmutated baseline: {'PASS' if base == 0 else 'FAIL'}")
    caught = 0
    for name, fn in mutants.items():
        _in_window = fn
        try:
            rc = _selftest(verbose=False)
        finally:
            _in_window = real
        print(f"  mutant '{name}': {'CAUGHT' if rc else 'SURVIVED'}")
        caught += bool(rc)
    print(f"feedback mutation check: {caught}/{len(mutants)} caught")
    return 0 if caught == len(mutants) and base == 0 else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--mutation-check" in sys.argv:
        sys.exit(_mutation_check())
    print(__doc__)
