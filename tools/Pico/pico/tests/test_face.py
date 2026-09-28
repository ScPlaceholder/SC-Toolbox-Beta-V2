"""THE CONTROL for the face chooser: the checks must FAIL on wrong implementations.

Same standard test_control.py sets, and for the reason it gives: "A differential test with
a broken control scores a perfect 100%, because every mutant dies regardless of what it
does." So this asserts BOTH halves —

    BASELINE   the real chooser passes all ten checks
    CONTROL    each of the seven wrong implementations is caught by >= 1 check

— and then asserts per-mutation WHICH check catches it. That last part is what earns its
keep. A check that catches everything is usually catching nothing in particular, and a
check that catches nothing is invisible in a green run.

The seven mutants are not invented for the exercise. Each is a thing I would plausibly
have written: a content fallback (the default that lies), a mood that never expires, a
decay that keeps showing the last face, a pool that never rotates, a pool rotated per-slot
instead of per-pair (the exact mistake Astra's "independently randomized emotions are not
[useful]" names), a tier that passes through unaudited, and one that trusts a
confident:false row.

Nothing here writes to disk, renders, or touches a queue.
"""

from __future__ import annotations

from typing import Optional

import pytest

from pico.face import (
    BASE_VISOR_ART,
    DEFAULT_MOODS,
    FACE_SLOTS,
    FaceChooser,
    FaceError,
    UNKNOWN_PAIR,
    UNKNOWN_VISOR,
)

# A tier map shaped like the real visor_tier_map.json "map" block: keyed on the BLUE art
# id, carrying the tier's art id and a confidence flag.
TIER_MAP = {
    "visor_01": {"red": "visor_01", "confident": True},
    "visor_04": {"red": "visor_04", "confident": True},
    "visor_18": {"red": "visor_16", "confident": True},   # alert: 18 -> 16, NOT identity
    "visor_09": {"red": "visor_08", "confident": True},
    "visor_13": {"red": "visor_12", "confident": True},
    "visor_11": {"red": "visor_10", "confident": True},
    "visor_03": {"red": "visor_03", "confident": True},
    "visor_16": {"red": "visor_21", "confident": True},
    "visor_10": {"red": "visor_09", "confident": True},
    "visor_08": {"red": "visor_18", "confident": True},
    # the mirror pair, recorded as unorderable by shape
    "visor_06": {"red": "visor_06", "confident": False},
}


def build(cls=FaceChooser, **kw) -> FaceChooser:
    kw.setdefault("tier_map", TIER_MAP)
    kw.setdefault("rotate_s", 2.0)
    return cls(**kw)


# ── the checks, each named, each returning None on pass or a reason on fail ─────────────

def c_unknown_when_no_mood(f: FaceChooser) -> Optional[str]:
    got = f.choose(at=100.0)
    if got != UNKNOWN_PAIR:
        return "no mood set but got %r instead of UNKNOWN_PAIR" % (got,)
    return None


def c_unknown_is_not_content(f: FaceChooser) -> Optional[str]:
    if UNKNOWN_VISOR.startswith("content"):
        return "UNKNOWN_VISOR is a content face"
    if BASE_VISOR_ART[UNKNOWN_VISOR] == BASE_VISOR_ART["content"]:
        return "UNKNOWN binds to the same art as content, so they are indistinguishable"
    return None


def c_decays_to_unknown(f: FaceChooser) -> Optional[str]:
    f.set_mood("alert", at=0.0)
    ttl = f.moods["alert"].ttl_s
    live = f.choose(at=1.0)
    if live == UNKNOWN_PAIR:
        return "a freshly set mood already reads as UNKNOWN"
    after = f.choose(at=ttl + 1.0)
    if after != UNKNOWN_PAIR:
        return "mood outlived its %.0fs ttl: got %r" % (ttl, (after,))
    return None


def c_pool_rotates(f: FaceChooser) -> Optional[str]:
    f.set_mood("calm", at=0.0)
    n = len(f.moods["calm"].pool)
    seen = {f.choose(at=i * f.rotate_s) for i in range(n)}
    if len(seen) < 2:
        return "a pool of %d produced %d distinct pair(s) — that is a pool of one" % (
            n, len(seen))
    return None


def c_rotation_deterministic(f: FaceChooser) -> Optional[str]:
    f.set_mood("calm", at=0.0)
    if f.choose(at=5.0) != f.choose(at=5.0):
        return "two calls at the same instant disagreed"
    return None


def c_pairs_stay_paired(f: FaceChooser) -> Optional[str]:
    f.set_mood("happy", at=0.0)
    pool = set(f.moods["happy"].pool)
    for i in range(12):
        got = f.choose(at=i * 1.0)
        if got not in pool:
            return ("returned %r, which is not a pool entry — visor and beak came from "
                    "different pairs" % (got,))
    return None


def c_unaudited_tier_refuses(f: FaceChooser) -> Optional[str]:
    try:
        f.bind(("alert", "ajar"), tier="orange")
    except FaceError:
        return None
    return "an unaudited tier was accepted"


def c_nonconfident_refuses(f: FaceChooser) -> Optional[str]:
    """visor_06 is excluded from the vocabulary, so flag a REACHABLE row instead.

    ⚠ THE FIRST VERSION OF THIS CHECK BUILT ITS OWN CHOOSER AND IGNORED `f`, so the
      mutant under test never ran and `trust_nonconfident` passed all ten checks. Caught on
      the first run by the per-mutation assertion, which is the whole reason that assertion
      exists. A check must exercise the OBJECT IT WAS HANDED; constructing a fresh one tests
      the baseline forever, whatever it was called with.
    """
    f.tier_map = {**TIER_MAP, "visor_01": {"red": "visor_01", "confident": False}}
    try:
        f.bind(("content", "closed"), tier="red")
    except FaceError:
        return None
    return "a confident:false mapping was used as if it were known"


def c_tier_is_modifier(f: FaceChooser) -> Optional[str]:
    f.set_mood("alert", at=0.0)
    pair = f.choose(at=0.0)
    blue = f.bind(pair)
    red = f.bind(pair, tier="red")
    if f.choose(at=0.0) != pair:
        return "asking for a tier changed the semantic pair"
    if blue["visor"] == red["visor"]:
        return "tier produced the same art id, so the colour was silently dropped"
    return None


def c_only_face_slots(f: FaceChooser) -> Optional[str]:
    f.set_mood("calm", at=0.0)
    out = f.face_now(at=0.0)
    extra = sorted(set(out) - set(FACE_SLOTS))
    if extra:
        return "wrote slots outside the face: %s" % extra
    return None


CHECKS = {
    "unknown_when_no_mood": c_unknown_when_no_mood,
    "unknown_is_not_content": c_unknown_is_not_content,
    "decays_to_unknown": c_decays_to_unknown,
    "pool_rotates": c_pool_rotates,
    "rotation_deterministic": c_rotation_deterministic,
    "pairs_stay_paired": c_pairs_stay_paired,
    "unaudited_tier_refuses": c_unaudited_tier_refuses,
    "nonconfident_refuses": c_nonconfident_refuses,
    "tier_is_modifier": c_tier_is_modifier,
    "only_face_slots": c_only_face_slots,
}


def run_all(cls=FaceChooser, **kw) -> dict[str, Optional[str]]:
    out = {}
    for name, fn in CHECKS.items():
        f = build(cls, **kw)          # a FRESH chooser per check: shared state between
        try:                          # checks is how one test's mood leaks into another's
            out[name] = fn(f)         # answer. [[my-test-carried-state-and-invented-a-bug]]
        except FaceError as exc:
            out[name] = "raised: %s" % exc
    return out


# ── BASELINE ───────────────────────────────────────────────────────────────────────────

def test_baseline_passes_every_check():
    res = run_all()
    bad = {k: v for k, v in res.items() if v is not None}
    assert not bad, "the real chooser failed its own checks: %r" % bad


# ── CONTROL: seven wrong implementations ───────────────────────────────────────────────

class M_ContentFallback(FaceChooser):
    """The default that lies: no mood -> look happy."""
    def choose(self, *, at: float):
        name = self.current_mood(at=at)
        if name is None:
            return ("content", "closed")
        return super().choose(at=at)


class M_NoDecay(FaceChooser):
    """A mood that never expires."""
    def current_mood(self, *, at: float):
        return self._mood


class M_StickyLast(FaceChooser):
    """On decay, keep showing the last face instead of admitting ignorance."""
    def choose(self, *, at: float):
        name = self.current_mood(at=at)
        if name is None and self._mood is not None:
            m = self.moods[self._mood]
            return m.pool[0]
        return super().choose(at=at)


class M_FrozenPool(FaceChooser):
    """A pool of three that always shows the first entry."""
    def choose(self, *, at: float):
        name = self.current_mood(at=at)
        if name is None:
            return UNKNOWN_PAIR
        return self.moods[name].pool[0]


class M_IndependentRotation(FaceChooser):
    """Rotate visor and beak separately — the exact mistake Astra warned about."""
    def choose(self, *, at: float):
        name = self.current_mood(at=at)
        if name is None:
            return UNKNOWN_PAIR
        pool = self.moods[name].pool
        el = float(at) - self._since
        i = int(el // self.rotate_s) % len(pool)
        j = int(el // (self.rotate_s * 2) + 1) % len(pool)
        return (pool[i][0], pool[j][1])


class M_TierPassthrough(FaceChooser):
    """Accept any tier; fall back to blue art when it is not audited."""
    def bind(self, pair, *, tier=None, beak_art=None):
        if tier is not None and tier not in self.audited_tiers:
            return super().bind(pair, tier=None, beak_art=beak_art)
        return super().bind(pair, tier=tier, beak_art=beak_art)


class M_TrustNonConfident(FaceChooser):
    """Use a confident:false mapping as though it were known."""
    def bind(self, pair, *, tier=None, beak_art=None):
        if tier is not None:
            art = BASE_VISOR_ART[pair[0]]
            row = dict(self.tier_map.get(art) or {})
            if row and not row.get("confident", False):
                row["confident"] = True
                patched = dict(self.tier_map)
                patched[art] = row
                saved, self.tier_map = self.tier_map, patched
                try:
                    return super().bind(pair, tier=tier, beak_art=beak_art)
                finally:
                    self.tier_map = saved
        return super().bind(pair, tier=tier, beak_art=beak_art)


# Each mutant, and the check(s) that MUST catch it. Naming the expected catcher is what
# exposes an inert check: if a mutant dies to a different check than the one written for
# it, the written one is not doing the work it claims.
MUTANTS = {
    "content_fallback": (M_ContentFallback, "unknown_when_no_mood"),
    "no_decay": (M_NoDecay, "decays_to_unknown"),
    "sticky_last": (M_StickyLast, "decays_to_unknown"),
    "frozen_pool": (M_FrozenPool, "pool_rotates"),
    "independent_rotation": (M_IndependentRotation, "pairs_stay_paired"),
    "tier_passthrough": (M_TierPassthrough, "unaudited_tier_refuses"),
    "trust_nonconfident": (M_TrustNonConfident, "nonconfident_refuses"),
}


@pytest.mark.parametrize("label", sorted(MUTANTS))
def test_each_mutant_is_caught(label):
    cls, _expected = MUTANTS[label]
    res = run_all(cls)
    caught = [k for k, v in res.items() if v is not None]
    assert caught, (
        "mutant %r passed EVERY check. Either the mutation is inert or the checks are."
        % label
    )


@pytest.mark.parametrize("label", sorted(MUTANTS))
def test_the_named_check_is_the_one_that_catches_it(label):
    cls, expected = MUTANTS[label]
    res = run_all(cls)
    assert res.get(expected) is not None, (
        "mutant %r was NOT caught by %r, the check written for it. It died to %r instead, "
        "which means %r is not doing the work it claims."
        % (label, expected, [k for k, v in res.items() if v is not None], expected)
    )


def test_mood_validation_refuses_bad_pools():
    from pico.face import Mood
    with pytest.raises(FaceError):
        Mood("empty", (), ttl_s=10.0)
    with pytest.raises(FaceError):
        Mood("forever", (("content", "closed"),), ttl_s=0.0)
    with pytest.raises(FaceError):
        Mood("notapair", (("content",),), ttl_s=10.0)          # type: ignore[arg-type]
    with pytest.raises(FaceError):
        Mood("mirror", (("visor_06", "closed"),), ttl_s=10.0)  # excluded vocabulary


def test_unknown_mood_name_raises_rather_than_guessing():
    f = build()
    with pytest.raises(FaceError):
        f.set_mood("clam", at=0.0)          # a typo for "calm"


def test_every_default_mood_is_constructible_and_bindable():
    f = build()
    for name, mood in DEFAULT_MOODS.items():
        f.set_mood(name, at=0.0)
        for i in range(len(mood.pool)):
            pair = f.choose(at=i * f.rotate_s)
            out = f.bind(pair)
            assert set(out) <= set(FACE_SLOTS)
            assert out["visor"][0] in BASE_VISOR_ART.values()


# ══════════════════════════════════════════════════════════════════════════════════════════
# THE AFFECT BRIDGE: three states, and the two that look alike must not collapse
# ══════════════════════════════════════════════════════════════════════════════════════════
# SuitMk2's CompanionAffect.dominant() returns (None, 0.0) for BOTH "no record for this
# speaker" AND "every emotion decayed to zero" — unknown and calm, one return value. For a
# face that is flat dashes versus a smile, so mood_for() takes `feed_live` keyword-only with
# NO DEFAULT: the caller must answer it and cannot answer it by accident.

from pico.face import COMPANION_MOOD_FLOOR, EMOTION_TO_MOOD, mood_for  # noqa: E402


def test_no_feed_is_unknown_not_calm():
    """The case dominant() cannot express. None here means the UNKNOWN face."""
    assert mood_for(None, 0.0, feed_live=False) is None
    assert mood_for("fear", 0.9, feed_live=False) is None, (
        "a dead feed must win over whatever stale emotion was last seen"
    )


def test_live_feed_with_no_emotion_is_calm_not_unknown():
    """Known and unremarkable. Distinct from the test above, which is the entire point."""
    assert mood_for(None, 0.0, feed_live=True) == "calm"
    assert mood_for(None, COMPANION_MOOD_FLOOR - 0.01, feed_live=True) == "calm"


def test_the_two_indistinguishable_states_give_different_faces():
    """The regression that matters: if these ever agree, the collapse has been reintroduced."""
    unknown = mood_for(None, 0.0, feed_live=False)
    calm = mood_for(None, 0.0, feed_live=True)
    assert unknown != calm, (
        "no-feed and nothing-felt produced the same answer from IDENTICAL arguments — "
        "that is dominant()'s ambiguity leaking through the bridge it was written to stop"
    )


def test_feed_live_has_no_default():
    """It must be impossible to forget the question. A default would let it through."""
    with pytest.raises(TypeError):
        mood_for(None, 0.0)          # type: ignore[call-arg]


def test_intensity_bands_between_moods():
    hi = mood_for("fear", 0.9, feed_live=True)
    lo = mood_for("fear", 0.1, feed_live=True)
    assert hi == "startled" and lo == "hurt", (hi, lo)
    assert hi != lo, "banding did nothing — intensity is not selecting"


def test_unmapped_emotion_raises_rather_than_defaulting_to_calm():
    with pytest.raises(FaceError):
        mood_for("smugness", 0.9, feed_live=True)


def test_every_mapped_mood_actually_exists():
    """A table naming a mood the chooser lacks fails at runtime, on a real event, later."""
    f = build()
    for emotion, bands in EMOTION_TO_MOOD.items():
        for _thr, mood in bands:
            assert mood in f.moods, "%s -> %r, which is not a mood" % (emotion, mood)


def test_every_companion_emotion_is_mapped():
    """The nine emotions in SuitMk2's EMOTIONS. Restated, not imported — Pico must not
    depend on another tool's internals. If upstream adds a tenth, mood_for RAISES on it
    (tested above) rather than showing a contented face, and this list going stale is the
    known cost of not coupling the trees."""
    upstream = {"fear", "relief", "pride", "joy", "curiosity",
                "boredom", "irritation", "warmth", "grief"}
    missing = sorted(upstream - set(EMOTION_TO_MOOD))
    assert not missing, "unmapped companion emotion(s): %s" % missing


# ══════════════════════════════════════════════════════════════════════════════════════════
# TEMPERAMENT: exaggeration has to stay informative
# ══════════════════════════════════════════════════════════════════════════════════════════

from pico.face import CREATURE, PERSON, Temperament  # noqa: E402


def test_creature_feels_the_same_event_harder():
    """If this ever agrees, the knob does nothing and the green means nothing."""
    ev = 0.3
    assert mood_for("fear", ev, feed_live=True, temperament=PERSON) == "hurt"
    assert mood_for("fear", ev, feed_live=True, temperament=CREATURE) == "startled"


#: No single mood may claim more than this share of the input range, for an emotion that has
#: more than one band. A JUDGEMENT, not a measurement: 0.85 passes gain=2.0 (the top band
#: takes ~71% of the range) and fails gain=20.0 (~95%). If someone tunes the gain for real
#: against an actual eye, this number is the thing to revisit, not to delete.
MAX_SHARE_OF_RANGE = 0.85


def test_exaggeration_does_not_collapse_the_range():
    """⛔ THE ONE THAT DEFENDS THE GAIN, and its FIRST VERSION COULD NOT FAIL.

    A creature permanently startled carries exactly as much information as one permanently
    content — both ends destroy the signal and the floor is only the more obvious end.

    ⚠ Version one asserted merely that more than ONE mood was reachable across the sweep.
      That passes for ANY gain, because level 0.0 multiplied by anything is still 0.0 and
      lands in the bottom band. I set CREATURE.gain to 20.0 to check the test bit, and it
      stayed green: 1 of 21 sample points in `hurt`, 20 in `startled`, two distinct moods,
      assertion satisfied. The face would be startled for 95% of possible inputs and the
      test called that a range.
    ⇒ So measure the SHARE, not the variety. Second test today that passed for a reason
      unrelated to what it was checking, both found by mutating rather than by re-reading.
      [[prove-the-test-can-fail-before-trusting-it-passes]]
    """
    for emotion, bands in EMOTION_TO_MOOD.items():
        if len(bands) < 2:
            continue
        got = [mood_for(emotion, lv / 40.0, feed_live=True, temperament=CREATURE)
               for lv in range(41)]
        reached = set(got)
        assert len(reached) > 1, (
            "under CREATURE, %r reaches only %r across 0..1 — the gain of %.2f flattened a "
            "banded emotion into a constant face" % (emotion, reached, CREATURE.gain)
        )
        top = max(reached, key=got.count)
        share = got.count(top) / len(got)
        assert share <= MAX_SHARE_OF_RANGE, (
            "under CREATURE, %r spends %.0f%% of the 0..1 range in %r (cap %.0f%%). More than "
            "one mood is technically reachable, but the face is effectively constant — a gain "
            "of %.2f has spent the signal it was meant to amplify."
            % (emotion, share * 100, top, MAX_SHARE_OF_RANGE * 100, CREATURE.gain)
        )


def test_gain_is_capped_so_it_cannot_exceed_the_upstream_ceiling():
    """emotion.py caps a level at 1.0; amplifying must not invent a level above it."""
    assert CREATURE.felt(0.9) <= 1.0
    assert Temperament("wild", gain=99.0).felt(1.0) == 1.0


def test_creature_moods_pass_faster():
    slow = build(temperament=PERSON)
    fast = build(temperament=CREATURE)
    for f in (slow, fast):
        f.set_mood("alert", at=0.0)
    ttl = slow.moods["alert"].ttl_s
    mid = ttl * 0.5
    assert slow.choose(at=mid) != UNKNOWN_PAIR, "the person forgot too early"
    assert fast.choose(at=mid) == UNKNOWN_PAIR, (
        "the creature still remembers at half the person's TTL — ttl_scale did nothing"
    )


def test_temperament_validation():
    with pytest.raises(FaceError):
        Temperament("mute", gain=0.0)
    with pytest.raises(FaceError):
        Temperament("frozen", ttl_scale=0.0)


def test_default_temperament_is_person_so_existing_callers_are_unchanged():
    assert build().temperament is PERSON
    assert mood_for("fear", 0.3, feed_live=True) == "hurt"
