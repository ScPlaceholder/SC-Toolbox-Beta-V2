"""pico/events.py — THE EVENT MAPPER. Game.log -> typed events -> a mood for the face.

Layer C of PICO_CONTRACT.md: *"Game.log -> triggers -> state machine. No Qt. Owns
pico/events.py"*. It is the half `face.py` declared it did not have:

    "Nothing here reads Game.log. The contract assigns that to an EVENT MAPPER at
     pico/events.py, which does not exist. So this module takes a mood from its caller and
     has no opinion about where the mood came from. `set_mood` is the seam they will meet at."

This is that caller. `face.py` is NOT modified and nothing here reaches inside it.

──────────────────────────────────────────────────────────────────────────────────────────
IT IS AN ADAPTER, NOT A PORT, AND THE IMPORT WAS TESTED RATHER THAN REASONED ABOUT
──────────────────────────────────────────────────────────────────────────────────────────
Measured 2026-09-28 by running it:

    import SuitMk2.core.emotion            with tools/ on sys.path   -> OK
    import SuitMk2.core.event_parser       with tools/ on sys.path   -> OK
    import SuitMk2.core.event_classifier   with tools/ on sys.path   -> ModuleNotFoundError:
                                                        No module named 'event_parser'

So the sibling tree imports, with one caveat that decides the mechanism: there is no
`__init__.py` anywhere under `SuitMk2/`, and `event_classifier.py` line 16 reads
`from event_parser import LogEvent, ...` — a FLAT import. It therefore needs
`SuitMk2/core` ITSELF on `sys.path`, not `tools/`.

⛔ AND THAT IS THE ROUTE THIS MODULE REFUSES TO TAKE. Putting `SuitMk2/core` on `sys.path`
  makes ~60 files importable under generic top-level names — `emotion`, `pacing`, `eyes`,
  `feedback`, `state_store` — inside whatever process hosts Pico. `event_parser.py` itself
  documents having been bitten by exactly that, at line 19:

      # Path-based import to avoid collision with sc_log_reader's location_names

  ⇒ So this module uses upstream's own remedy: `spec_from_file_location` under a PREFIXED
    alias. Verified 2026-09-28 — `sys.path` is untouched, neither `emotion` nor
    `event_parser` appears in `sys.modules` under its bare name, and both load.

⇒ `emotion.py` and `event_parser.py` are the only two files needed. `event_classifier.py`
  is NOT used and its flat-import problem is therefore moot: `LogEvent` already arrives
  carrying `.category` from the parser's own `_CATEGORY_MAP`, so importing the classifier
  would buy a taxonomy nothing here consults, at the cost of the one dependency that
  cannot be loaded without polluting the namespace.

⇒ NOTHING IS DUPLICATED. No hook table, no decay curve, no line patterns. The alternative
  was a 1,200-line fork, and this tree already demonstrates where that ends: `animate.py`
  and `pico/` both hold `idle_breathe` and nothing checks they agree.

The drift risk moves rather than vanishing — an adapter breaks when the upstream API is
renamed — so `verify_upstream()` and the three `check_*` functions below assert the seam
mechanically at load time, and `PICO_EVENT_TYPES` is re-derived from upstream's own source
by AST rather than trusted. See THE FOUR SEAM CHECKS.

──────────────────────────────────────────────────────────────────────────────────────────
REQUIREMENT ONE: `feed_live` IS DERIVED FROM FACTS THAT ARE NOT THE EMOTION
──────────────────────────────────────────────────────────────────────────────────────────
`CompanionAffect.dominant()` returns `(None, 0.0)` for BOTH "no record for this speaker"
AND "everything decayed to zero". Verified by running it, 2026-09-28:

    a = CompanionAffect(path=None)
    a.dominant("pico")   -> (None, 0.0)     # no such speaker, nothing is known
    a.dominant("elah")   -> (None, 0.0)     # a real speaker, genuinely at rest

Identical return values; *unknown* and *calm*; flat dashes versus a smile.

⇒ THE FIX IS NOT A BETTER READING OF THAT TUPLE — no reading of it can work, because the
  information is not in it. `FeedState` answers "does a feed exist?" from three facts, none
  of which is an emotion or a level:

      upstream_ok     did the SuitMk2 modules load at all
      speaker_known   is there a record for this speaker  (`affect.levels.get(speaker)`,
                      which is `None` for "pico" and a dict of nine zeros for "elah" —
                      the distinction dominant() throws away)
      observed        has a log source actually been read (lines_seen > 0)

  `feed_live` is the AND of those. It is computed before `dominant()` is called and is not
  a function of its result, which is the only construction that cannot collapse the two.

★ AND THE SPEAKER IS VALIDATED AT CONSTRUCTION, WHICH IS THE SHARPER HALF. A typo'd or
  invented speaker name makes `dominant()` return `(None, 0.0)` FOREVER. Fed to a naive
  bridge that reads only the tuple, a permanently-wrong speaker renders as a permanently
  CONTENTED penguin: a mascot cheerfully reporting a channel it was never connected to.
  So `MoodSource.__init__` raises on a speaker outside upstream's `SPEAKERS`.

──────────────────────────────────────────────────────────────────────────────────────────
WHY PICO IS NOT A SPEAKER, AND WHOSE AFFECT IT READS
──────────────────────────────────────────────────────────────────────────────────────────
`face.py` already found the trap and it is honoured here rather than rediscovered:
`emotion.hushed()` is `any(... for s in SPEAKERS)` and hushing means idle talk waits, so
adding "pico" to `SPEAKERS` would let a frightened penguin SILENCE ELAH.

⇒ Pico owns its OWN `CompanionAffect` instance, constructed with `path=None`, and reads an
  EXISTING speaker from it. `path=None` makes `_save()` and `_load()` no-ops — verified in
  the source — so this module performs no writes whatsoever, which `test_hygiene.py`
  independently enforces over the package tree.
⇒ Upstream `SPEAKERS` is untouched, nothing upstream can be gated by Pico's mood, and the
  decay model is REUSED rather than reimplemented. A caller that wants Pico to share the
  live companion's mood instead passes that object as `affect=`.

──────────────────────────────────────────────────────────────────────────────────────────
WHAT IT WILL NOT DO
──────────────────────────────────────────────────────────────────────────────────────────
1. ⛔ It never invents an emotion name and never catches `FaceError`. `face.mood_for`
   raises on an unmapped emotion BY DESIGN — that is how a tenth emotion added upstream
   surfaces as a fault instead of as a contented face. Swallowing it here would convert
   every future upstream addition into a silent lie. `reading()` lets it propagate.
2. ⛔ It does not default `feed_live`. `FeedState.live` is computed, and `MoodReading`
   carries the whole `FeedState` so a caller can say WHY the face is blank.
3. ⛔ It does not treat a missing log as a quiet one. `read_log` on an unreadable path
   RAISES; `try_read_log` returns False and records a named reason. An absence is never
   laundered into "nothing happened".
4. ⛔ It writes nothing, renders nothing, speaks nothing, and imports no Qt.

──────────────────────────────────────────────────────────────────────────────────────────
THE FOUR SEAM CHECKS  (all four run inside `load_suit`; none of them needs a model)
──────────────────────────────────────────────────────────────────────────────────────────
  verify_upstream        every attribute this adapter touches still exists. A rename fails
                         HERE, at load, rather than as an AttributeError mid-frame.
  check_mood_floor       upstream `MOOD_FLOOR` vs `face.COMPANION_MOOD_FLOOR`. face.py
                         restates that constant and flags the staleness itself: *"If it
                         moves there this goes stale, which is a real risk."* This closes
                         it mechanically without editing face.py.
  check_emotion_coverage upstream `EMOTIONS` vs `face.EMOTION_TO_MOOD`. A tenth emotion is
                         caught at load, not on the one real event that happens to raise it.
  check_hook_wiring      THE ONE THAT MATTERS, because its failure mode is silent. If the
                         parser's event names and `HOOKS`' keys drift apart, the parser keeps
                         emitting, the affect model ignores every one, `feed_live` stays
                         TRUE because lines really are being read, and the face reports CALM
                         forever. Nothing downstream can tell. So the event names are scraped
                         from upstream's own `_classify_line` by AST and compared to `HOOKS`.
                         ⚠ The scrape guards against its own blindness: zero names scraped
                           raises `EventError` rather than reporting everything missing. An
                           instrument that cannot see must not be read as a clean board.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from pico import face as _face
from pico.face import FaceChooser, FaceError, Temperament, PERSON

__all__ = [
    "EventError",
    # Re-exported DELIBERATELY, not left over from an edit: `MoodSource.reading()` lets
    # `FaceError` propagate by design (an unmapped emotion must surface as a fault), so a
    # caller has to be able to catch it without knowing that the mood came from face.py.
    "FaceError",
    "SuitBridge",
    "FeedState",
    "MoodReading",
    "MoodSource",
    "PICO_EVENT_TYPES",
    "default_core_dir",
    "load_suit",
    "verify_upstream",
    "check_mood_floor",
    "check_emotion_coverage",
    "check_hook_wiring",
    "scrape_parser_event_types",
    "mood_from_log",
]


class EventError(Exception):
    """Raised instead of guessing. Every raise site is a refusal, not a bug."""


# ══════════════════════════════════════════════════════════════════════════════════════════
# LOADING THE SIBLING TREE
# ══════════════════════════════════════════════════════════════════════════════════════════

#: Alias prefix for path-loaded upstream modules. Prefixed so `sys.modules` never gains a
#: bare `emotion` / `event_parser`, which is the collision `event_parser.py` itself records
#: having been bitten by.
_ALIAS = "_pico_suit_"

#: The two files this adapter needs. `event_classifier.py` is deliberately absent — see the
#: module docstring. Adding a third entry here is a decision, not a detail.
_NEEDED = ("emotion.py", "event_parser.py")


def default_core_dir() -> Path:
    """`tools/SuitMk2/core`, located RELATIVE TO THIS FILE, not to the cwd.

    A cwd-relative default would work from `tools/Pico` and fail everywhere else, which is
    the kind of path bug that only shows up once something else launches Pico.
    """
    return (Path(__file__).resolve().parents[2] / "SuitMk2" / "core").resolve()


def _load_module(core: Path, filename: str) -> Any:
    alias = _ALIAS + Path(filename).stem
    path = core / filename
    if not path.is_file():
        raise EventError(
            "upstream file %s is missing. Looked in %s. This adapter has no fallback "
            "implementation on purpose: a stub here would report a mood computed from "
            "nothing." % (filename, core)
        )
    spec = importlib.util.spec_from_file_location(alias, path)
    if spec is None or spec.loader is None:
        raise EventError("cannot build an import spec for %s" % path)
    mod = importlib.util.module_from_spec(spec)
    # Registered under the PREFIXED alias before exec so a dataclass or pickle that looks
    # itself up by __module__ resolves. The bare name is never claimed.
    sys.modules[alias] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception as exc:                                    # pragma: no cover - env
        sys.modules.pop(alias, None)
        raise EventError("upstream %s failed to import: %s: %s"
                         % (filename, type(exc).__name__, exc)) from exc
    return mod


@dataclass(frozen=True)
class SuitBridge:
    """The loaded upstream surface, plus the facts the seam checks established.

    Frozen because it is a record of what was verified at a moment, not a live handle to be
    edited afterwards.
    """

    core: Path
    emotion: Any
    parser_mod: Any
    speakers: tuple[str, ...]
    hooks: tuple[str, ...]
    emotions: tuple[str, ...]
    mood_floor: float
    wired_events: tuple[str, ...]

    def new_affect(self, *, now: Callable[[], float]) -> Any:
        """A fresh in-memory `CompanionAffect`. `path=None` -> it never writes."""
        return self.emotion.CompanionAffect(path=None, now=now)

    def new_parser(self) -> Any:
        return self.parser_mod.EventParser()


# ── the four seam checks ────────────────────────────────────────────────────────────────

#: attribute name -> why this adapter needs it. The reasons are load-bearing: a future
#: reader deciding whether a rename can be absorbed needs to know what each one is FOR.
_REQUIRED_EMOTION_ATTRS = {
    "CompanionAffect": "the affect model Pico instantiates instead of reimplementing",
    "SPEAKERS": "validating the speaker, so a typo cannot read as permanent calm",
    "HOOKS": "event_type -> emotion; the wiring check compares its keys to the parser's",
    "EMOTIONS": "the emotion vocabulary face.EMOTION_TO_MOOD must cover",
    "MOOD_FLOOR": "the floor face.COMPANION_MOOD_FLOOR restates and this checks",
    "CAP": "the 1.0 ceiling Temperament.felt must not exceed",
}
_REQUIRED_PARSER_ATTRS = {
    "EventParser": "turns raw lines into typed events",
    "LogEvent": "the typed event; carries .event_type, .data and .category",
}
_REQUIRED_AFFECT_METHODS = {
    "feed": "one game event in; returns True if it moved anything",
    "dominant": "the (emotion, level) read — the ambiguous one this module fences off",
    "levels": "per-speaker record; its PRESENCE is how feed existence is established",
    "drift": "boredom creep while nothing happens",
}


def verify_upstream(bridge_emotion: Any, bridge_parser: Any) -> None:
    """Every attribute this adapter touches, asserted to still exist.

    An adapter's whole failure mode is an upstream rename. Without this the rename shows up
    as an `AttributeError` at whatever moment the face happens to be asked for, which is
    both later and harder to read than a refusal at load.
    """
    missing = []
    for attr, why in _REQUIRED_EMOTION_ATTRS.items():
        if not hasattr(bridge_emotion, attr):
            missing.append("emotion.%s (%s)" % (attr, why))
    for attr, why in _REQUIRED_PARSER_ATTRS.items():
        if not hasattr(bridge_parser, attr):
            missing.append("event_parser.%s (%s)" % (attr, why))
    affect_cls = getattr(bridge_emotion, "CompanionAffect", None)
    if affect_cls is not None:
        for attr, why in _REQUIRED_AFFECT_METHODS.items():
            if not hasattr(affect_cls, attr) and attr not in getattr(
                    affect_cls, "__annotations__", {}):
                # `levels` is set in __init__, so probe an instance rather than the class.
                try:
                    probe = affect_cls(path=None, now=lambda: 0.0)
                    if not hasattr(probe, attr):
                        missing.append("CompanionAffect.%s (%s)" % (attr, why))
                except Exception:                              # pragma: no cover - env
                    missing.append("CompanionAffect.%s (%s) — and an instance could not be "
                                   "built to check" % (attr, why))
    if missing:
        raise EventError(
            "the upstream API this adapter is built on has moved. Missing: %s. Refusing to "
            "load: an adapter that silently loses a capability reports a mood computed from "
            "less than it claims." % "; ".join(missing)
        )


def check_mood_floor(bridge_emotion: Any) -> None:
    """`face.COMPANION_MOOD_FLOOR` vs upstream `MOOD_FLOOR`.

    face.py restates that constant deliberately, to avoid coupling the trees, and names the
    cost in its own comment: *"If it moves there this goes stale, which is a real risk."*
    Nothing was watching it. Now something is, and it needed no edit to face.py.
    """
    up = float(getattr(bridge_emotion, "MOOD_FLOOR"))
    mine = float(_face.COMPANION_MOOD_FLOOR)
    if abs(up - mine) > 1e-9:
        raise EventError(
            "MOOD_FLOOR drift: upstream emotion.py says %r, face.COMPANION_MOOD_FLOOR says "
            "%r. face.py restates that number rather than importing it and flagged this "
            "exact staleness risk. Reconcile them; do not paper over it here, because the "
            "floor decides whether a real feeling reads as calm." % (up, mine)
        )


def check_emotion_coverage(bridge_emotion: Any) -> None:
    """Upstream's emotion vocabulary vs `face.EMOTION_TO_MOOD`.

    `face.mood_for` raises on an unmapped emotion, which is correct and is the LAST line of
    defence — it fires on the first real event carrying the new feeling, possibly weeks in.
    This moves the same finding to load time, where it is cheap.
    """
    up = set(getattr(bridge_emotion, "EMOTIONS"))
    mapped = set(_face.EMOTION_TO_MOOD)
    missing = sorted(up - mapped)
    if missing:
        raise EventError(
            "upstream emotion(s) with no face mapping: %s. face.mood_for would raise on the "
            "first event carrying one; this says so now instead. Add a band to "
            "face.EMOTION_TO_MOOD — and pick a face for it, rather than letting it route to "
            "an existing mood that means something else." % ", ".join(missing)
        )
    # The reverse is NOT an error: face.py may map a feeling upstream has retired, and a
    # dead row in a lookup table is inert. Reported, never raised.


def scrape_parser_event_types(core: Path) -> frozenset[str]:
    """Event-type names upstream's parser can return, read off its AST.

    Structural, not textual: it walks `_classify_line` / `_classify_shud_event` and collects
    `return "<literal>"`. A textual grep would match the same strings in comments and
    docstrings, which is the difference between "the parser can emit this" and "the word
    appears in the file".

    ⚠ IT GUARDS AGAINST ITS OWN BLINDNESS. If upstream renames those functions the walk
      finds nothing, and "found nothing" would otherwise read as "the parser emits nothing",
      which would make every wiring comparison fail with a wrong diagnosis — or, worse, make
      a set-difference check pass vacuously. Zero names raises.
      [[i-nearly-reported-two-absences-my-own-tool-manufactured]]
    """
    src = (core / "event_parser.py").read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(src)
    wanted = {"_classify_line", "_classify_shud_event"}
    found_fn: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in wanted:
            found_fn.add(node.name)
            for inner in ast.walk(node):
                if (isinstance(inner, ast.Return)
                        and isinstance(inner.value, ast.Constant)
                        and isinstance(inner.value.value, str)):
                    names.add(inner.value.value)
    if not found_fn:
        raise EventError(
            "scraped event_parser.py and found NONE of %s. The classifier functions have "
            "been renamed or restructured, so this instrument is blind — and a blind "
            "instrument reporting an empty set looks exactly like a parser that emits "
            "nothing. Refusing to return a result." % ", ".join(sorted(wanted))
        )
    if not names:
        raise EventError(
            "found %s but scraped ZERO event-type literals from it. The classifier no longer "
            "returns string constants directly; this scrape cannot see its output and must "
            "not be read as a clean board." % ", ".join(sorted(found_fn))
        )
    return frozenset(names)


def check_hook_wiring(bridge_emotion: Any, core: Path) -> tuple[str, ...]:
    """The intersection of "the parser can emit it" and "the affect model reacts to it".

    ⛔ THIS IS THE CHECK WHOSE ABSENCE WOULD BE SILENT. Every other drift here produces an
      exception somewhere. This one does not: if the two name-spaces drift apart, the parser
      goes on emitting, `affect.feed()` returns False for every single event, `feed_live`
      stays TRUE because lines genuinely are being read, and the penguin reports CALM
      through a firefight. There is no downstream symptom at all.

    Returns the live intersection so a caller can see what Pico can actually react to —
    which is smaller than `HOOKS` and always will be, because several hooks are fed by
    `combat_watch.py` and `bdl_tracker.py` rather than by the log parser.
    """
    emitted = scrape_parser_event_types(core)
    hooks = set(getattr(bridge_emotion, "HOOKS"))
    wired = sorted(emitted & hooks)
    if not wired:
        raise EventError(
            "NO event type is both emitted by event_parser and present in emotion.HOOKS. "
            "The parser knows %d types and HOOKS knows %d, and they share none. Pico would "
            "read the log, parse it correctly, move no emotion, and show a contented face "
            "forever. Refusing to load." % (len(emitted), len(hooks))
        )
    return tuple(wired)


#: The intersection MEASURED 2026-09-28 against the shipped upstream, kept as a RECORD so a
#: shrinking intersection is visible. Not used as the live answer — `check_hook_wiring`
#: recomputes that every load. A frozen list used as the answer is how a fork starts.
#:
#: The 9 `HOOKS` keys absent from it are not defects: `combat_on`/`combat_off` come from
#: `combat_watch.py`, `bdl_warning`/`bdl_clear` from `bdl_tracker.py`, `pilot_spoke` from the
#: voice path, `boarded_ship`/`left_ship` from `move_lifecycle.py`, and `item_earned` /
#: `scene_notable` from elsewhere again. Pico sees the log only, so it sees 14 of 23.
PICO_EVENT_TYPES: tuple[str, ...] = (
    "blueprint_received", "contract_accepted", "contract_complete",
    "entered_monitored_space", "exited_monitored_space", "incapacitated", "injury",
    "location_change", "med_bed_heal", "objective_new", "player_respawned", "qt_arrived",
    "refinery_complete", "reward_earned",
)


def load_suit(core_dir: Optional[Path] = None) -> SuitBridge:
    """Load the sibling tree and run all four seam checks. Raises `EventError` on any.

    Deliberately NOT called at import time. `pico.events` must import in a tree that has no
    `SuitMk2/` beside it — otherwise a missing sibling breaks the whole Pico package rather
    than producing the honest reading it should produce, which is "no feed, UNKNOWN face".
    """
    core = Path(core_dir).resolve() if core_dir is not None else default_core_dir()
    if not core.is_dir():
        raise EventError(
            "no SuitMk2 core at %s. Pico and SuitMk2 are siblings under tools/; if the "
            "layout moved, pass core_dir explicitly rather than letting this guess." % core
        )
    mods = {Path(f).stem: _load_module(core, f) for f in _NEEDED}
    em, ep = mods["emotion"], mods["event_parser"]
    verify_upstream(em, ep)
    check_mood_floor(em)
    check_emotion_coverage(em)
    wired = check_hook_wiring(em, core)
    return SuitBridge(
        core=core,
        emotion=em,
        parser_mod=ep,
        speakers=tuple(em.SPEAKERS),
        hooks=tuple(sorted(em.HOOKS)),
        emotions=tuple(sorted(em.EMOTIONS)),
        mood_floor=float(em.MOOD_FLOOR),
        wired_events=wired,
    )


# ══════════════════════════════════════════════════════════════════════════════════════════
# FEED EXISTENCE — the requirement `dominant()` cannot satisfy
# ══════════════════════════════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class FeedState:
    """Whether a feed EXISTS, established independently of what it reports.

    Every field here is a fact about the PLUMBING. Not one of them is an emotion or a level,
    which is the only reason `live` cannot collapse into `dominant()`'s ambiguity.

    ⚠ `stale` IS AN OPEN QUESTION AND IS OFF BY DEFAULT. A feed that read 50,000 lines two
      hours ago and nothing since is, for a face, arguably not live — the game may have
      closed. But I have no basis for a number, and inventing one would put a silent policy
      where a documented gap belongs. `MoodSource(max_silence_s=...)` turns it on; with it
      unset, "observed once" counts as live forever and the mood's own TTL in face.py is
      what expires. Flagged rather than decided.
    """

    upstream_ok: bool
    speaker_known: bool
    lines_seen: int
    events_seen: int = 0
    events_moved: int = 0
    last_line_at: Optional[float] = None
    last_event_at: Optional[float] = None
    max_silence_s: Optional[float] = None
    at: Optional[float] = None

    @property
    def observed(self) -> bool:
        """Has a source actually been read. Zero lines is NOT a quiet feed, it is no feed."""
        return self.lines_seen > 0

    @property
    def log_age_s(self) -> Optional[float]:
        """How old the newest GAME event is, in wall-clock seconds. `None` if none yet.

        ⛔ THIS IS A DIFFERENT NUMBER FROM `last_line_at` AND I ALMOST SHIPPED ONLY ONE.
          `last_line_at` is when PICO READ a line; this is when the GAME WROTE the newest
          event. Replay a month-old log and the first is `now` while the second is a month
          ago. A feed judged on the first alone reports a month-old mood as current — a
          lying dashboard produced by the honest-looking field.

        ⚠ ACCURACY CAVEAT, inherited and not fixable from here: upstream's
          `_extract_timestamp` returns a NAIVE datetime — parsed from the log's `<...Z>`
          (UTC) when the pattern matches, and `datetime.now()` (LOCAL) when it does not. This
          property reads them as UTC, so a fallback timestamp is wrong by the local UTC
          offset. Good to hours, not to seconds, and stated rather than implied.
          ⇒ The DECAY clock below is immune, because it uses only DIFFERENCES between log
            timestamps and a constant offset cancels.
        """
        if self.last_event_at is None or self.at is None:
            return None
        return self.at - self.last_event_at

    @property
    def stale(self) -> bool:
        if self.max_silence_s is None or self.last_line_at is None or self.at is None:
            return False
        return (self.at - self.last_line_at) > self.max_silence_s

    @property
    def live(self) -> bool:
        return (self.upstream_ok and self.speaker_known and self.observed
                and not self.stale)

    @property
    def reason(self) -> str:
        """Why the feed is not live — named, so a blank face can be explained.

        Order matters: it reports the OUTERMOST failure, because "no upstream" makes
        "no speaker record" true as well and reporting the inner one would misdirect.
        """
        if self.live:
            return "live"
        if not self.upstream_ok:
            return "upstream SuitMk2 not loaded"
        if not self.speaker_known:
            return "no affect record for this speaker"
        if not self.observed:
            return "no log line has been read yet"
        return "no log line for %.0fs (max_silence_s=%.0f)" % (
            (self.at or 0.0) - (self.last_line_at or 0.0), self.max_silence_s or 0.0)


@dataclass(frozen=True)
class MoodReading:
    """What the face should show, and everything needed to justify it.

    `mood` is `None` for UNKNOWN — the same `None` `face.mood_for` returns, so it feeds
    `FaceChooser` directly. The `feed` record travels with it because a blank face with no
    stated reason is the thing that starts a debugging session.
    """

    mood: Optional[str]
    emotion: Optional[str]
    level: float
    feed: FeedState
    speaker: str
    at: float

    @property
    def feed_live(self) -> bool:
        return self.feed.live

    @property
    def unknown(self) -> bool:
        return self.mood is None

    def __str__(self) -> str:
        age = self.feed.log_age_s
        return "mood=%s emotion=%s level=%.2f feed=%s log_age=%s" % (
            self.mood or "UNKNOWN", self.emotion or "-", self.level, self.feed.reason,
            "-" if age is None else "%.0fs" % age)


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE STATE MACHINE
# ══════════════════════════════════════════════════════════════════════════════════════════

def _epoch_of(ts: Any) -> Optional[float]:
    """A parser `LogEvent.timestamp` as an epoch float, or `None` if it is not a datetime.

    Upstream hands back a NAIVE datetime (see `FeedState.log_age_s`). Read as UTC, because
    that is what the log's own `<...Z>` says; the fallback branch's local-time value is then
    off by the UTC offset, which the decay clock does not care about and `log_age_s` does.
    """
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.timestamp()


class MoodSource:
    """Game.log lines in, `MoodReading` out. The state machine layer C owes layer B.

    Clock is injected and shared with the affect model, so decay is deterministic under test
    — the same reason `FaceChooser.choose` takes `at` rather than calling `time.time()`.

    ──────────────────────────────────────────────────────────────────────────────────────
    TWO CLOCKS, EACH IN ITS OWN DOMAIN, AND THE SECOND ONE IS A BUG FIX
    ──────────────────────────────────────────────────────────────────────────────────────
    * The FACE side (mood TTL, pool rotation) runs on WALL time. `reading(at=...)`.
    * The AFFECT side (emotion half-lives) runs on LOG time: `_affect_now()` is
      `wall_now() + offset`, where `offset` is set from the newest log timestamp each time
      an event arrives.

    ⛔ THE SECOND ONE IS NOT A REFINEMENT, IT IS A MEASURED DEFECT REPAIRED. Replaying the
      real 74,759-line log of 2025-08-02 with the affect model on wall time gave
      `curiosity 1.00` — 89 `location_change` events, ~5 hours apart in the log, stacked in
      the ~2 seconds the replay actually took, so `_decay()` never ran between them and
      nothing ever faded. The mascot reported a maxed-out feeling assembled out of an entire
      afternoon. With log time, the same replay reports the level the session ACTUALLY ended
      on.
    ⇒ The offset form is correct in BOTH regimes rather than being a replay special case.
      Tailing a live log, log time tracks wall time and the offset is ~constant. During a
      fast replay, wall time barely moves and the offset carries the advance. After the log
      goes quiet, the clock keeps advancing on wall time from where the log left off, so a
      feeling still fades while nothing is happening — which a plain "use the last log
      timestamp" clock would freeze.
    ⚠ A backwards jump (upstream's `datetime.now()` fallback landing before a real UTC
      stamp) cannot explode the decay: `CompanionAffect._decay` is `dt = max(0.0, t - self._t)`,
      so it clamps to no decay. Verified in the source, not assumed.
    ⚠ AND IT IS SKIPPED ENTIRELY WHEN THE CALLER SUPPLIES `affect=`. A shared companion
      affect object owns its own clock; re-timing somebody else's model from Pico's log
      cursor would reach into another tool's state, which is the one thing this adapter is
      built not to do.
    """

    def __init__(
        self,
        bridge: Optional[SuitBridge] = None,
        *,
        speaker: str = "elah",
        temperament: Temperament = PERSON,
        affect: Any = None,
        now: Callable[[], float] = time.time,
        max_silence_s: Optional[float] = None,
        core_dir: Optional[Path] = None,
    ) -> None:
        self._now = now
        # Set BEFORE the affect model is built, because `_affect_now` closes over it.
        self._clock_offset = 0.0
        self._clock_seeded = False
        self.last_event_at: Optional[float] = None
        self.clock_regressions = 0
        self.temperament = temperament
        self.max_silence_s = None if max_silence_s is None else float(max_silence_s)
        if self.max_silence_s is not None and not (self.max_silence_s > 0):
            raise EventError(
                "max_silence_s=%r: a non-positive staleness window marks a feed dead the "
                "instant it is read" % max_silence_s)

        self.bridge = bridge if bridge is not None else load_suit(core_dir)

        # ⛔ THE SPEAKER GATE. An unknown speaker makes dominant() return (None, 0.0) for
        #    ever, which a bridge reading only that tuple renders as permanent CALM: a
        #    penguin looking content about a channel it was never connected to. Refusing is
        #    the only answer that cannot be mistaken for information.
        if speaker not in self.bridge.speakers:
            raise EventError(
                "speaker %r is not one of upstream's SPEAKERS (%s). Refusing: an unknown "
                "speaker has no affect record, dominant() returns (None, 0.0) for it "
                "permanently, and that is indistinguishable from perfect calm. A typo here "
                "would be a face that lies for the life of the process."
                % (speaker, ", ".join(self.bridge.speakers)))
        self.speaker = speaker

        # `owns_affect` decides whether Pico may re-time the decay clock. It may re-time only
        # a model it created. See the class docstring's last caveat.
        self.owns_affect = affect is None
        self.affect = (self.bridge.new_affect(now=self._affect_now) if self.owns_affect
                       else affect)
        self.parser = self.bridge.new_parser()
        self.parser.subscribe(self._on_event)

        self.lines_seen = 0
        self.events_seen = 0
        self.events_moved = 0
        self.last_line_at: Optional[float] = None
        self.last_event: Optional[Any] = None
        self._sources: list[str] = []

    # ── clocks ─────────────────────────────────────────────────────────────────────────
    def _affect_now(self) -> float:
        """Wall time plus the log-time offset. See the class docstring. Offset 0 -> wall."""
        return self._now() + self._clock_offset

    # ── in ─────────────────────────────────────────────────────────────────────────────
    def _on_event(self, event: Any) -> None:
        """Parser callback. One typed event -> the affect model.

        `feed()` returns False for an event with no hook — `weapon_holstered`,
        `platform_moving`, and 27 others. That is NORMAL and is counted separately from
        `events_seen`, never treated as a failure: most of what a log says is not a feeling.
        """
        self.events_seen += 1
        self.last_event = event
        if self.owns_affect:
            self._advance_clock(_epoch_of(getattr(event, "timestamp", None)))
        else:
            ts = _epoch_of(getattr(event, "timestamp", None))
            if ts is not None:
                self.last_event_at = ts
        if self.affect.feed(event.event_type, dict(getattr(event, "data", None) or {})):
            self.events_moved += 1

    def _advance_clock(self, log_epoch: Optional[float]) -> None:
        """Re-aim the decay clock at log time, BEFORE the event is fed.

        ⛔ THE SEED IS THE WHOLE PROBLEM AND MY FIRST VERSION GOT IT WRONG IN BOTH
          DIRECTIONS. `CompanionAffect.__init__` sets `self._t = now()`, i.e. WALL time. Aim
          the clock at log time afterwards and the first event is one enormous step:
            * replaying a 2025 log under the real clock -> the step is BACKWARDS, and
              `_decay`'s `dt = max(0.0, ...)` clamps it to zero AND never updates `_t`, so
              NO decay is ever applied again. Measured: `fear 1.00` off two deaths 17 hours
              apart in the log. The mascot reported a maxed feeling built from an afternoon.
            * the same replay under a fake clock at t=1000 -> the step is FORWARDS by ~55
              years and zeroes every emotion at the first event. Measured: `calm 0.00` for a
              log that ends minutes after a death.
          Two opposite wrong answers from one missing seed, and the first looked plausible.
        ⇒ So on the FIRST log timestamp the affect model is REBUILT, which makes its `_t`
          seed from `_affect_now()` — log time. Nothing is lost: no event has been fed yet.
          It uses only the public constructor; no upstream private state is touched.

        ⚠ MONOTONE ONLY. Upstream's `_extract_timestamp` falls back to `datetime.now()`
          (local, naive) when a line carries no `<...Z>` stamp, which in a historical log is
          a jump of months into the future and would zero everything. A log's real stamps are
          ordered, so a regression is that fallback and is REFUSED for the clock — counted in
          `clock_regressions` rather than skipped silently, because a silent skip makes a
          broken log look like a clean one.
          MEASURED 2026-09-28 across 234 events in two real logs: 0 regressions. The guard is
          for the case that has not happened yet, and the counter is how I would find out.
        """
        if log_epoch is None:                                  # pragma: no cover - upstream
            return                                            # always returns a datetime
        if self.last_event_at is not None and log_epoch < self.last_event_at:
            self.clock_regressions += 1
            return
        self.last_event_at = log_epoch
        self._clock_offset = log_epoch - self._now()
        if not self._clock_seeded:
            self._clock_seeded = True
            self.affect = self.bridge.new_affect(now=self._affect_now)

    def feed_line(self, line: str) -> None:
        """One raw log line. Counts as OBSERVATION whether or not it parses.

        ★ THE COUNT IS OF LINES, NOT EVENTS, AND THAT IS THE POINT. A 74,759-line log with
          no hooked event in it is a feed that is WORKING and reporting a quiet session —
          `calm`. Counting events instead would make a real, healthy, boring feed
          indistinguishable from a disconnected one, which is the exact collapse this
          module exists to prevent, reintroduced one layer down.
        """
        self.lines_seen += 1
        self.last_line_at = self._now()
        self.parser.on_raw_line(line.rstrip("\n"))

    def feed_lines(self, lines: Iterable[str]) -> int:
        n = 0
        for line in lines:
            self.feed_line(line)
            n += 1
        return n

    def read_log(self, path: Path | str, *, encoding: str = "utf-8") -> int:
        """Read a whole log. RAISES on an unreadable path — an absence is not a quiet room.

        `errors="replace"`: SC logs carry stray bytes, and dropping the whole file over one
        of them would turn a cosmetic encoding fault into a dead feed.
        """
        p = Path(path)
        if not p.is_file():
            raise EventError(
                "no log at %s. Refusing to return a reading: a missing log and a silent one "
                "are different claims, and only one of them means 'nothing happened'." % p)
        try:
            with p.open("r", encoding=encoding, errors="replace") as fh:
                n = self.feed_lines(fh)
        except OSError as exc:
            raise EventError("cannot read %s: %s: %s" % (p, type(exc).__name__, exc)) from exc
        self._sources.append(str(p))
        return n

    def try_read_log(self, path: Path | str, **kw: Any) -> tuple[bool, str]:
        """`read_log` for a poll loop where the log may legitimately not exist yet.

        Returns `(ok, reason)`. It does NOT convert the failure into an observation, so the
        feed stays dead and the face stays UNKNOWN — which is the honest reading for "the
        game is not running".
        """
        try:
            n = self.read_log(path, **kw)
        except EventError as exc:
            return False, str(exc)
        return True, "read %d line(s)" % n

    def drift(self, minutes: float, *, looping: bool) -> None:
        """Pass-through to upstream boredom creep. Not a synonym for observation.

        ⚠ It deliberately does NOT touch `lines_seen`. Drifting is something Pico does to
          itself; calling it could otherwise manufacture a live feed out of a closed game.
        """
        self.affect.drift(minutes, looping)

    # ── out ────────────────────────────────────────────────────────────────────────────
    def feed_state(self, *, at: Optional[float] = None) -> FeedState:
        """Feed existence, computed WITHOUT consulting `dominant()`.

        Note what is absent: no emotion, no level, no call into the affect model's read
        path. That independence is the requirement, not a stylistic preference.
        """
        return FeedState(
            upstream_ok=self.bridge is not None,
            # The distinction dominant() throws away: `None` for a speaker with no record,
            # a dict of nine zeros for a real speaker genuinely at rest.
            speaker_known=bool(self.affect.levels.get(self.speaker)),
            lines_seen=self.lines_seen,
            events_seen=self.events_seen,
            events_moved=self.events_moved,
            last_line_at=self.last_line_at,
            last_event_at=self.last_event_at,
            max_silence_s=self.max_silence_s,
            at=self._now() if at is None else float(at),
        )

    def reading(self, *, at: Optional[float] = None) -> MoodReading:
        """The mood now. `FaceError` from `face.mood_for` is NOT caught.

        An unmapped emotion must surface as a fault. Catching it here and substituting a
        default is precisely how a new upstream feeling becomes a contented penguin, and
        `face.mood_for`'s own docstring says so.
        """
        t = self._now() if at is None else float(at)
        feed = self.feed_state(at=t)
        emotion, level = self.affect.dominant(self.speaker)
        mood = _face.mood_for(emotion, level, feed_live=feed.live,
                              temperament=self.temperament)
        return MoodReading(mood=mood, emotion=emotion, level=float(level), feed=feed,
                           speaker=self.speaker, at=t)

    def apply_to(self, chooser: FaceChooser, *, at: Optional[float] = None) -> MoodReading:
        """Drive a `FaceChooser` from the current reading. The seam face.py named.

        ⛔ IT ONLY CALLS `set_mood` WHEN THE MOOD NAME CHANGES, and that is a bug fix, not
          an optimisation. `set_mood` resets `_since`, and `choose()` derives its pool index
          from `floor((at - _since) / rotate_s)`. Call it every tick and `_since` is always
          `now`, the index is always 0, and the pool NEVER ROTATES — a frozen face, with
          every test in test_face.py still green because the chooser is behaving exactly as
          specified. The defect would live entirely in this caller.
        ⇒ When the name is unchanged, `_since` is left alone so rotation continues. When it
          has decayed to None, the same name is re-armed.
        """
        r = self.reading(at=at)
        if r.mood is None:
            chooser.forget()
            return r
        if chooser.current_mood(at=r.at) != r.mood:
            chooser.set_mood(r.mood, at=r.at)
        return r

    # ── diagnostics ────────────────────────────────────────────────────────────────────
    def diagnostics(self) -> dict[str, Any]:
        """Plain facts for a status line. No verdict — the reading is the verdict."""
        return {
            "speaker": self.speaker,
            "temperament": self.temperament.name,
            "lines_seen": self.lines_seen,
            "events_seen": self.events_seen,
            "events_moved": self.events_moved,
            "sources": tuple(self._sources),
            "wired_events": self.bridge.wired_events,
            "upstream_core": str(self.bridge.core),
            "owns_affect": self.owns_affect,
            "clock_offset_s": round(self._clock_offset, 1),
            "clock_regressions": self.clock_regressions,
            "log_age_s": self.feed_state().log_age_s,
            "affect": self.affect.state(),
        }


def mood_from_log(
    path: Path | str,
    *,
    speaker: str = "elah",
    temperament: Temperament = PERSON,
    bridge: Optional[SuitBridge] = None,
    now: Callable[[], float] = time.time,
    core_dir: Optional[Path] = None,
) -> MoodReading:
    """One-shot: read a whole log, return the mood. Raises `EventError` if it cannot read it.

    The convenience entry point. A live mascot uses `MoodSource` and tails the file instead,
    because this rereads from the top every call.
    """
    src = MoodSource(bridge, speaker=speaker, temperament=temperament, now=now,
                     core_dir=core_dir)
    src.read_log(path)
    return src.reading()


# ══════════════════════════════════════════════════════════════════════════════════════════
def _main(argv: list[str]) -> int:                             # pragma: no cover - CLI
    """`py -3.13 -m pico.events [<Game.log>]` — the seam checks, then a real reading."""
    try:
        bridge = load_suit()
    except EventError as exc:
        print("REFUSED: %s" % exc)
        return 2
    print("upstream   : %s" % bridge.core)
    print("speakers   : %s" % ", ".join(bridge.speakers))
    print("mood floor : %.2f (agrees with face.COMPANION_MOOD_FLOOR)" % bridge.mood_floor)
    print("wired      : %d of %d HOOKS reachable from the log parser"
          % (len(bridge.wired_events), len(bridge.hooks)))
    print("             %s" % ", ".join(bridge.wired_events))
    src = MoodSource(bridge)
    print("before any line: %s" % src.reading())
    if len(argv) > 1:
        ok, why = src.try_read_log(argv[1])
        print("read %s -> %s" % (argv[1], why if ok else "FAILED: " + why))
        if ok:
            print("after: %s" % src.reading())
            print("diag : lines=%d events=%d moved=%d"
                  % (src.lines_seen, src.events_seen, src.events_moved))
    return 0


if __name__ == "__main__":                                     # pragma: no cover - CLI
    raise SystemExit(_main(sys.argv))
