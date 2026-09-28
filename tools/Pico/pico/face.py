"""pico/face.py — THE FACE CHOOSER. Which face is on the snap points right now.

J's shape for the engine (2026-09-28): body and limbs, mouth and visor, props, clothes,
where "the mouth and visor are snap points that a different pipeline streams to... we just
need to animate the penguin with no face and everything else gets streamed in on top."

The rig already holds up its half. `Skeleton` declares `visor`, `beak`, `eye_L`, `eye_R`
as slots carrying only (name, bone, z) and no artwork, and `Rig.slot_transforms(pose,
attachments)` takes `{slot: [art_id, ...]}` as a SKIN that "CANNOT influence any matrix".
Not one .anim clip in pico/clips names a face. So the streaming separation is enforced,
not merely intended.

What did not exist is the CHOOSER: the thing that decides which face is on the point at a
given moment. This is it. It is the only piece here that makes a judgement, which is why
it is written by hand rather than delegated.

──────────────────────────────────────────────────────────────────────────────────────────
WHAT THIS MODULE REFUSES TO DO, AND WHY EACH REFUSAL IS LOAD-BEARING
──────────────────────────────────────────────────────────────────────────────────────────

1. ⛔ IT NEVER FALLS BACK TO "CONTENT". The recorded requirement is blunt: *UNKNOWN needs
   its OWN face, or a mascot defaulting to content is a dashboard that lies.* A face is a
   status display. Showing a happy penguin because the state is unreadable is not a
   cosmetic default, it is a false report — the same defect as a checker that prints CLEAR
   when it could not read the thing it was checking.
   ⚠ AND THE ART HAS NO "UNKNOWN" FACE, which I found by reading the inventory rather than
     assuming one. Twenty blue expressions, none of them blank. So UNKNOWN is bound to
     `neutral` (visor_04, "flat dashes") DELIBERATELY: flat dashes read as no-information,
     where arcs-down reads as contentment. The two are different claims and the art can
     tell them apart. `UNKNOWN_VISOR` must never be pointed at content_*.

2. ⛔ IT REFUSES AN UNAUDITED TIER rather than substituting. `animate.py` records
   `TIER_MAPPED = ("red",)` — "tiers with an audited blue->tier table. Grows only by eye."
   Measured 2026-09-28: orange and yellow show NO confident evidence against sharing red's
   slot layout, but that is a population statement about a matcher, not an eye. An expression
   meaning "alert" rendering as "content" is a mascot that lies and nothing downstream can
   catch it.

3. ⛔ IT REFUSES A NON-CONFIDENT MAPPING. visor_06 and visor_07 are a mirror pair recorded
   `confident: false` in visor_tier_map.json, because shape alone cannot order a mirror.
   A 50/50 asserted as fact is worse than a named gap, so those two are excluded from every
   pool and raise if requested under a tier.

4. ⛔ IT EMITS SEMANTIC NAMES AND MAKES THE BINDER REFUSE. Astra's own caveat on the idles
   it wrote: *"names below are semantic targets from your description, not verified
   filenames or beak indices. Bind them to actual assets before building."* `idle_import`
   already works this way — it refuses anything that does not bind rather than substituting
   a default face. Same contract here: `choose()` returns meaning, `bind()` returns art, and
   an unbound name raises.

──────────────────────────────────────────────────────────────────────────────────────────
WHY THE POOL HOLDS PAIRS AND NOT TWO INDEPENDENT SLOTS
──────────────────────────────────────────────────────────────────────────────────────────

Asked what the right relationship between mouth and eyes is, Astra answered:

    "Give both channels the same underlying intention, but let the action determine their
     timing. Neither channel should always lead."
    "Independent timing is useful; INDEPENDENTLY RANDOMIZED EMOTIONS ARE NOT. Select
     compatible expression pairs for each intention."

My first sketch rotated the visor and the beak separately, which is precisely the mistake
that names. So a mood's pool is a tuple of (visor, beak) PAIRS chosen to go together, and
rotation picks a pair. The timing may differ between the two channels; the CHOICE may not.

Also from that answer, and honoured here: the beak stays closed through quiet idling ("It
does not need to accompany every eye change"), and swaps use "a few readable stages, not a
tour through the mouth library" — hence pools of two or three, never the whole inventory.

──────────────────────────────────────────────────────────────────────────────────────────
WHAT IT DOES NOT KNOW
──────────────────────────────────────────────────────────────────────────────────────────
Nothing here reads Game.log. The contract assigns that to an EVENT MAPPER at
`pico/events.py`, which does not exist. So this module takes a mood from its caller and has
no opinion about where the mood came from. It is the half that can be built and tested
without inventing the other half. `set_mood` is the seam they will meet at.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

__all__ = [
    "FaceError",
    "Mood",
    "FaceChooser",
    "DEFAULT_MOODS",
    "BASE_VISOR_ART",
    "UNKNOWN_VISOR",
    "UNKNOWN_PAIR",
    "FACE_SLOTS",
    "EMOTION_TO_MOOD",
    "COMPANION_MOOD_FLOOR",
    "mood_for",
    "Temperament",
    "PERSON",
    "CREATURE",
]


class FaceError(Exception):
    """Raised instead of guessing. Every raise site is a refusal, not a bug."""


# Semantic visor name -> art id in the BLUE (base) set, read off visor_tier_map.json's
# glyph column rather than inferred from the file numbering. The numbering is NOT the
# ordering: blue_08 is "static, both sparks" and maps to red_18, not red_08.
BASE_VISOR_ART: Mapping[str, str] = {
    "content": "visor_01",        # arcs down
    "content_alt": "visor_02",    # arcs up
    "squint": "visor_03",         # ><
    "neutral": "visor_04",        # flat dashes  <- also UNKNOWN. See refusal 1.
    "neutral_alt": "visor_05",    # flat dashes v2
    "static": "visor_08",
    "wide": "visor_09",           # two circles
    "focus": "visor_10",          # concentric
    "woozy": "visor_11",          # tildes
    "angry": "visor_12",
    "downcast": "visor_13",       # L brackets
    "love": "visor_14",           # hearts
    "starstruck": "visor_15",     # asterisks
    "locked_on": "visor_16",      # bullseyes
    "ko": "visor_17",             # X
    "alert": "visor_18",          # !!
    "sparks": "visor_19",
    "crackle": "visor_20",
    # visor_06 / visor_07 are ABSENT ON PURPOSE: the mirror pair recorded
    # confident:false. Excluded from the vocabulary so no pool can reach them.
}

# ⛔ The unknown face. Asserted below to be distinct from every content_* name, because
#    this single binding is what stops the mascot from lying when it knows nothing.
UNKNOWN_VISOR = "neutral"
UNKNOWN_BEAK = "closed"
UNKNOWN_PAIR = (UNKNOWN_VISOR, UNKNOWN_BEAK)

# Slots this module is allowed to write. Anything outside is somebody else's layer.
FACE_SLOTS = ("visor", "beak")


@dataclass(frozen=True)
class Mood:
    """A named intention, a small pool of compatible (visor, beak) pairs, and a deadline.

    `ttl_s` is not decoration. A mood with no expiry is a mood that outlives the thing that
    justified it, and a face still showing ALERT an hour after the alert cleared is the same
    false report as the content-fallback. Decay is how this module forgets.
    """

    name: str
    pool: tuple[tuple[str, str], ...]
    ttl_s: float

    def __post_init__(self) -> None:
        if not self.pool:
            raise FaceError("mood %r has an empty pool — it could never show anything"
                            % self.name)
        if not (self.ttl_s > 0):
            raise FaceError(
                "mood %r has ttl_s=%r; a non-positive TTL either never expires or expires "
                "instantly, and both defeat decay" % (self.name, self.ttl_s)
            )
        for pair in self.pool:
            if len(pair) != 2:
                raise FaceError(
                    "mood %r pool entry %r is not a (visor, beak) PAIR. Pools hold pairs "
                    "because independently randomised emotions produce incompatible "
                    "combinations." % (self.name, pair)
                )
            if pair[0] not in BASE_VISOR_ART:
                raise FaceError(
                    "mood %r names visor %r, which is not in the vocabulary. (visor_06 and "
                    "visor_07 are excluded on purpose: confident:false mirror pair.)"
                    % (self.name, pair[0])
                )


DEFAULT_MOODS: Mapping[str, Mood] = {
    # Pools of two or three, per "a few readable stages, not a tour through the library".
    # The beak stays closed while idling; it opens only where the intention needs it.
    "calm": Mood("calm", (("content", "closed"),
                          ("content_alt", "closed"),
                          ("neutral", "closed")), ttl_s=300.0),
    "alert": Mood("alert", (("alert", "ajar"),
                            ("locked_on", "closed"),
                            ("focus", "closed")), ttl_s=60.0),
    "hurt": Mood("hurt", (("downcast", "closed"),
                          ("woozy", "ajar"),
                          ("squint", "closed")), ttl_s=180.0),
    "happy": Mood("happy", (("love", "open"),
                            ("starstruck", "open"),
                            ("content", "closed")), ttl_s=120.0),
    "startled": Mood("startled", (("wide", "open"),
                                  ("static", "ajar")), ttl_s=20.0),
    # emotion.py carries `irritation` with a 900s decay and I had no face family for it,
    # which would have quietly routed irritation to `hurt`. Different feeling, different face.
    "irritated": Mood("irritated", (("angry", "closed"),
                                    ("squint", "closed")), ttl_s=240.0),
}


class FaceChooser:
    """Decides the (visor, beak) pair now, and binds it to art on request.

    Two calls on purpose. `choose()` answers "what does he mean", `bind()` answers "which
    files", and keeping them apart is what lets the tier be a MODIFIER over one expression
    set rather than a second set of expressions.
    """

    def __init__(
        self,
        *,
        moods: Mapping[str, Mood] = DEFAULT_MOODS,
        tier_map: Optional[Mapping[str, Mapping[str, object]]] = None,
        audited_tiers: Iterable[str] = ("red",),
        rotate_s: float = 2.5,
        slots: Sequence[str] = FACE_SLOTS,
        temperament: "Temperament" = None,   # type: ignore[assignment]
    ) -> None:
        if UNKNOWN_VISOR.startswith("content"):
            raise FaceError(
                "UNKNOWN_VISOR is a content face. That is the lying-dashboard defect this "
                "module exists to prevent."
            )
        if UNKNOWN_VISOR not in BASE_VISOR_ART:
            raise FaceError("UNKNOWN_VISOR %r is not bindable" % UNKNOWN_VISOR)
        if not (rotate_s > 0):
            raise FaceError("rotate_s must be positive; %r would divide by zero or never "
                            "advance" % rotate_s)
        self.moods = dict(moods)
        self.tier_map = dict(tier_map or {})
        self.audited_tiers = tuple(audited_tiers)
        self.rotate_s = float(rotate_s)
        self.slots = tuple(slots)
        self.temperament = temperament if temperament is not None else PERSON
        self._mood: Optional[str] = None
        self._since: float = 0.0

    # ── state in ────────────────────────────────────────────────────────────────────────
    def set_mood(self, name: str, *, at: float) -> None:
        """The seam the event mapper will meet. Unknown mood names RAISE."""
        if name not in self.moods:
            raise FaceError(
                "no mood %r. Known: %s. Refusing rather than picking a neighbour, because "
                "a wrong mood shows a wrong face and nothing downstream can tell."
                % (name, ", ".join(sorted(self.moods)))
            )
        self._mood = name
        self._since = float(at)

    def forget(self) -> None:
        self._mood = None

    # ── the decision ────────────────────────────────────────────────────────────────────
    def current_mood(self, *, at: float) -> Optional[str]:
        """The live mood, or None once it has decayed. None is a RESULT, not a gap."""
        if self._mood is None:
            return None
        m = self.moods[self._mood]
        # A creature's feelings pass faster. ttl_scale is the only place the temperament
        # touches decay, because the underlying emotion decay lives upstream in
        # CompanionAffect and is not mine to speed up.
        if float(at) - self._since >= m.ttl_s * self.temperament.ttl_scale:
            return None
        return self._mood

    def choose(self, *, at: float) -> tuple[str, str]:
        """The (visor, beak) semantic pair for this instant.

        Returns UNKNOWN_PAIR when no mood is live — never the last one seen, and never a
        content face. Rotation is a pure function of (mood, elapsed) so the same clock
        always gives the same face, which is the only reason this is testable.
        """
        name = self.current_mood(at=at)
        if name is None:
            return UNKNOWN_PAIR
        m = self.moods[name]
        elapsed = float(at) - self._since
        step = int(math.floor(elapsed / self.rotate_s))
        return m.pool[step % len(m.pool)]

    # ── meaning -> files ────────────────────────────────────────────────────────────────
    def bind(self, pair: tuple[str, str], *, tier: Optional[str] = None,
             beak_art: Optional[Mapping[str, str]] = None) -> dict[str, tuple[str, ...]]:
        """Resolve a semantic pair to an attachments map for `Rig.slot_transforms`.

        `tier` is a MODIFIER over the same expression set, not a different set — red means
        injured wearing the very same expression. Composition, e.g. dead = red + ko.
        """
        visor, beak = pair
        if visor not in BASE_VISOR_ART:
            raise FaceError("visor %r does not bind to any art id" % visor)
        art = BASE_VISOR_ART[visor]

        if tier is not None:
            if tier not in self.audited_tiers:
                raise FaceError(
                    "tier %r is NOT AUDITED (audited: %s). The slot layout may transfer — "
                    "measured 2026-09-28, no confident evidence against it — but that is a "
                    "statement about a shape matcher, not an eye. Refusing: an expression "
                    "meaning 'alert' rendering as 'content' is a mascot that lies."
                    % (tier, ", ".join(self.audited_tiers) or "none")
                )
            row = self.tier_map.get(art)
            if row is None:
                raise FaceError(
                    "no %s mapping for %s (%s). Refusing rather than reusing the blue art, "
                    "which would silently drop the severity colour." % (tier, art, visor)
                )
            if not row.get("confident", False):
                raise FaceError(
                    "the %s mapping for %s (%s) is recorded confident:false — a mirror pair "
                    "shape cannot order. A 50/50 asserted as fact is worse than a gap."
                    % (tier, art, visor)
                )
            mapped = row.get(tier)
            if not mapped:
                raise FaceError("%s row for %s has no %r field" % (tier, art, tier))
            art = str(mapped)

        out: dict[str, tuple[str, ...]] = {}
        if "visor" in self.slots:
            out["visor"] = (art,)
        if "beak" in self.slots:
            table = dict(beak_art or {})
            if table:
                if beak not in table:
                    raise FaceError(
                        "beak %r does not bind. Astra flagged its own beak names as "
                        "semantic targets, not verified indices — bind or refuse, never "
                        "substitute." % beak
                    )
                out["beak"] = (table[beak],)
            else:
                # No beak table supplied: pass the SEMANTIC name through unresolved rather
                # than inventing an index. The caller's binder must reject it if it cannot
                # resolve it, exactly as idle_import does.
                out["beak"] = (beak,)
        return out

    def face_now(self, *, at: float, tier: Optional[str] = None,
                 beak_art: Optional[Mapping[str, str]] = None
                 ) -> dict[str, tuple[str, ...]]:
        """choose() then bind(). The one call a renderer needs."""
        return self.bind(self.choose(at=at), tier=tier, beak_art=beak_art)



# ══════════════════════════════════════════════════════════════════════════════════════════
# TEMPERAMENT: the same event, felt harder
# ══════════════════════════════════════════════════════════════════════════════════════════
# J, 2026-09-28: "Pico as a creature. Think Chibi. The bigger and more exaggerated emotions
# the more charming something is when built in those visual proportions."
#
# ⚠ THAT IS NOT IN TENSION WITH THE RIG'S "SMALL ANGLES" RULE, though it reads like it. The
#   angle limits are about ROTATION — 8 degrees already reads as a big movement on a chibi
#   body. This is about EMOTIONAL amplitude: which face, how readable, how soon. Bigger
#   feelings, not bigger arcs.
#
# ★ WHY THIS LIVES HERE AND NOT AS A THIRD SPEAKER IN SuitMk2. The obvious implementation is
#   to add "pico" to emotion.py's SPEAKERS with its own hook amounts. It is also a trap I
#   found before writing any of it: `hushed()` is `any(... for s in SPEAKERS)`, and hushed
#   means idle talk waits — so a frightened penguin would SILENCE ME. I would go quiet and
#   the reason would be a mascot.
#   ⇒ A temperament applied on Pico's side needs no new speaker, touches nothing upstream,
#     and cannot gate anybody's voice, because Pico is not a speaker at all. Same events,
#     bigger reaction. It sidesteps the trap instead of working around it.


@dataclass(frozen=True)
class Temperament:
    """How hard a character feels the same event, and how fast it passes.

    `gain` multiplies the incoming level before banding, so a creature reaches the big
    readable faces on events a person would shrug at. `ttl_scale` shortens how long a mood
    holds, because a creature's feelings spike and pass — a dog forgets.

    ⛔ GAIN IS NOT FREE, AND THE TEST IS WHAT KEEPS IT HONEST. Push it high enough and every
      level lands in the top band, at which point the face is permanently startled and
      carries exactly as much information as one that is permanently content. Both ends
      destroy the signal; the floor is just the more obvious one. `test_exaggeration_does_
      not_collapse_the_range` sweeps the level range and demands more than one mood remain
      reachable, so raising this knob to something useless fails loudly instead of quietly.
    """

    name: str
    gain: float = 1.0
    ttl_scale: float = 1.0

    def __post_init__(self) -> None:
        if not (self.gain > 0):
            raise FaceError("temperament %r gain=%r: a non-positive gain mutes every event"
                            % (self.name, self.gain))
        if not (self.ttl_scale > 0):
            raise FaceError("temperament %r ttl_scale=%r would expire moods instantly or "
                            "never" % (self.name, self.ttl_scale))

    def felt(self, level: float) -> float:
        """The level as THIS character experiences it, capped at 1.0 like emotion.py's CAP."""
        return min(1.0, float(level) * self.gain)


#: A person: feels the event as reported. What the companion's own speakers use.
PERSON = Temperament("person", gain=1.0, ttl_scale=1.0)

#: A creature: chibi proportions, exaggerated feelings, short memory. gain=2.0 is a FIRST
#: GUESS chosen so a fear of 0.3 crosses the 0.55 startled band; it is not measured against
#: anyone's eye yet, and the range test above is the only thing currently defending it.
CREATURE = Temperament("creature", gain=2.0, ttl_scale=0.4)

# ══════════════════════════════════════════════════════════════════════════════════════════
# THE BRIDGE FROM THE COMPANION'S AFFECT MODEL
# ══════════════════════════════════════════════════════════════════════════════════════════
# J, 2026-09-28: "Can't we basically port the event and emotional mapper from the ai
# companion and use it?" Mostly yes. SuitMk2's core/emotion.py has `CompanionAffect` with
# nine emotions, each with its OWN decay constant (fear 300s, grief 1800s, boredom 1200s),
# twenty-four event hooks, and `dominant(speaker) -> (emotion|None, level)`. That is a
# better decay model than the flat per-mood TTL above and it already exists.
#
# ⚠ NO IMPORT OF SuitMk2 HERE, DELIBERATELY. This takes (emotion, level) as plain values.
#   Pico must not depend on another tool's internals — two implementations of one concept
#   drifting unwatched is the defect I found in this very project this morning, where
#   animate.py and pico/ both hold idle_breathe and nothing checks they agree. Whoever wires
#   this passes what `dominant()` returned; the mapping does not reach for it.
#
# ⛔⛔ AND `dominant()` COLLAPSES TWO STATES INTO ONE RETURN VALUE. Read it:
#
#       if not lv: return None, 0.0
#       e = max(lv, key=lv.get)
#       return (e, lv[e]) if lv[e] >= MOOD_FLOOR else (None, lv[e])
#
#   `(None, 0.0)` means EITHER "there is no affect record for this speaker" OR "every
#   emotion has decayed to exactly zero". Those are *unknown* and *calm*, and for a face
#   that is the difference between flat dashes and a content smile — the lying-dashboard
#   distinction this whole module is built around. The caller cannot tell them apart from
#   the return value.
#
# ⇒ So `feed_live` is keyword-only WITH NO DEFAULT. The caller must answer "is there an
#   affect feed at all?" separately, and cannot answer it by accident. A default would let
#   the ambiguity through silently, which is exactly how it got here.

#: emotion -> ((min_level, mood), ...) tried in order, highest threshold first. Intensity
#: selects BETWEEN moods rather than within a pool: the pool rotates on TIME to give
#: variety, so an index driven by intensity would fight it and one of the two would lose.
EMOTION_TO_MOOD: Mapping[str, tuple[tuple[float, str], ...]] = {
    "fear":       ((0.55, "startled"), (0.0, "hurt")),
    "grief":      ((0.0, "hurt"),),
    "irritation": ((0.0, "irritated"),),
    "relief":     ((0.0, "calm"),),
    "boredom":    ((0.0, "calm"),),
    "warmth":     ((0.0, "happy"),),
    "joy":        ((0.0, "happy"),),
    "pride":      ((0.0, "happy"),),
    "curiosity":  ((0.0, "alert"),),
}

#: emotion.py's own floor, restated rather than imported. If it moves there this goes stale,
#: which is a real risk — but importing it would couple the trees, and the comment above
#: explains why that is worse. Checked against the source on 2026-09-28: MOOD_FLOOR = 0.25.
COMPANION_MOOD_FLOOR = 0.25


def mood_for(emotion: Optional[str], level: float, *, feed_live: bool,
             temperament: "Temperament" = PERSON) -> Optional[str]:
    """Map the companion's (emotion, level) onto a mood name, or None for UNKNOWN.

    Three outcomes, kept apart on purpose:

      feed_live=False           -> None. Nothing is known. The chooser shows the UNKNOWN
                                   face. This is the case `dominant()` cannot distinguish.
      feed_live, no emotion     -> "calm". A feeling exists below the floor, or none does.
                                   KNOWN and unremarkable, which is not the same as unknown.
      feed_live, emotion named  -> the banded mood.

    Raises on an emotion this table has never heard of, rather than defaulting to calm — a
    new emotion added upstream must surface as a failure here, not as a penguin looking
    content about something nobody mapped.
    """
    if not feed_live:
        return None
    if emotion is None:
        return "calm"
    bands = EMOTION_TO_MOOD.get(emotion)
    if bands is None:
        raise FaceError(
            "no face mapping for emotion %r. Known: %s. Refusing rather than defaulting to "
            "calm, because an unmapped feeling must show up as a fault and not as a "
            "contented face." % (emotion, ", ".join(sorted(EMOTION_TO_MOOD)))
        )
    lv = temperament.felt(level)
    for threshold, mood in bands:
        if lv >= threshold:
            return mood
    # Unreachable while every table ends at 0.0; asserted rather than assumed.
    raise FaceError("emotion %r level %r fell through its bands" % (emotion, level))
