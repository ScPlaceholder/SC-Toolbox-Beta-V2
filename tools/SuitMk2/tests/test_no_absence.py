"""Never comment on an absence (J, 2026-10-05).

"If nothing is being salvaged do not comment on it. Depending on the salvage approach 3 ships could've been munched
since the last picture and being like 'yeah slim pickings today' just breaks the immersion. If something does happen
or there's a ship to salvage the engine should choose to comment or not."

A picture is only ever a reason to speak about something that is in it. What turns a picture into a line is
activity_mode.build_look_spec (the eyes' own looks) and CompanionCore._look_for (the pilot's "look at that"); both
ask activity_mode.names_something_there first. The real core and the real conversation lane; the eyes are a fake
that returns a description. No screen is captured and no model runs.
"""
from __future__ import annotations

import pytest

import activity_mode as am
import conversation as conv
import pacing
import picture_pace as pp
from _eyes_helpers import FakeEyes, make_core, tick

ABSENCES = [
    "", "   ", "nothing", "Nothing.", "Nothing notable.", "nothing of note here", "no ships", "No ships in sight.",
    "quiet", "Quiet.", "all quiet", "A quiet hangar.", "empty", "An empty hangar.", "Empty space, no ships in sight",
    "slim pickings", "Slim pickings today.", "not much going on", "there isn't much here", "Nobody around.",
    "no one here", "none", "N/A", "a deserted landing pad", "the pad is vacant", "a barren plain without features",
    "an uneventful stretch of space", "nothing to salvage", "no wrecks to be seen", "the salvage field is bare",
    "a hangar, no ships", "a ship is missing from the pad", "lacking any sign of life", "the void",
    "a blank screen", "can't see anything", "cannot make anything out", "a silent corridor",
    # names nothing at all
    "just the usual", "something", "the same view", "this and that", "more of the same", "it looks like a scene",
]
PRESENT = [
    "A wrecked hull drifting in the dark.", "a ship, or maybe a crate", "A tall lattice tower on a ridge under a yellow sky.",
    "three haulers queued at the pad", "a creature, or maybe a body", "smoke rising from a crashed fighter",
    "a giant plant in front of a distant vent", "an abandoned outpost half buried in sand", "a few ships docked",
    "a lone figure at the far end of the corridor", "the Reclaimer's claw closing on a hull",
]


@pytest.mark.parametrize("notable", ABSENCES)
def test_a_picture_with_nothing_in_it_makes_no_spec(notable):
    assert am.names_something_there(notable) is False
    for variant in range(len(am.LOOK_VOICES)):
        assert am.build_look_spec(notable, "interval", variant) is None


@pytest.mark.parametrize("notable", PRESENT)
def test_a_picture_with_a_thing_in_it_may_become_a_line_and_carries_the_eyes_own_words(notable):
    assert am.names_something_there(notable) is True
    spec = am.build_look_spec(notable, "interval", 0)
    assert spec is not None and spec["scenario"] == "scene_look"
    assert [c["value"] for c in spec["claims"]] == [notable.strip()]


def test_the_rule_is_positive_a_description_must_name_something():
    """Not a block-list alone: words that are on no list of absences still make no line when they name nothing."""
    for nameless in ("just the usual", "something", "the same view", "more of the same"):
        assert not am.ABSENCE.search(nameless)               # the refusal list does not catch these
        assert am.names_something_there(nameless) is False   # the positive rule does


@pytest.mark.parametrize("notable", ABSENCES)
def test_the_core_considers_no_line_for_a_picture_with_nothing_in_it_at_any_chattiness(notable):
    clock = [1000.0]
    for level in range(5):
        eyes = FakeEyes(saw=notable)
        core = make_core(eyes, clock, eye_chattiness=level)
        core.pace.configure({pp.every_key("sandbox"): pp.MIN_EVERY_S})
        for _ in range(3):
            tick(core)
            clock[0] += 5.0
        assert len(eyes.looks) == 3                          # the pictures were taken
        assert core.considered == [], (level, notable)       # and nothing was even offered to the gate
    assert pacing.eye_talk_gap_s(4) == 0.0                   # at the chattiest setting there is no wait to hide behind


def test_the_same_core_does_consider_a_line_when_something_is_there():
    clock, eyes = [1000.0], FakeEyes(saw=PRESENT[0])
    core = make_core(eyes, clock)
    tick(core)
    assert [s["claims"][0]["value"] for s in core.considered] == [PRESENT[0]]


def test_an_arrival_or_a_mining_capture_is_no_exception():
    clock, eyes = [1000.0], FakeEyes(saw="No ships to salvage here.")
    core = make_core(eyes, clock)
    core._last_look_t = clock[0] - 1000.0

    class Ev:
        event_type, data = "location_change", {"location_name": "Area18"}
    core.on_event(Ev())
    tick(core)
    clock[0] += 200.0
    assert core.mining_capture() is True
    tick(core)
    assert eyes.looks == ["arrival", "mining_capture"]
    assert [s for s in core.considered if s["scenario"] == "scene_look"] == []      # the arrival has its own line
    assert core.stats["look_nothing_there"] == 2


# ---------------------------------------------------------------------------------------------------------------
# the pilot's own "look at that": an absence is not said there either
# ---------------------------------------------------------------------------------------------------------------
VIVERE = (("location_name", "Vivere OLP"), ("location_body", "Aberdeen"), ("location_type", "outpost"),
          ("location_named", True))


def _ask(core, sentence="wow look at that!"):
    from speak_gate import Candidate, Priority
    spec = conv.ConversationLane(core.place_knowledge()).handle(sentence, core.lane_state(), {})
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    before = len(core.speech.said)
    core._answer_worker(spec, cand, sentence)
    return [t for _, t in core.speech.said[before:]]


def _asking_core(saw):
    eyes = FakeEyes(saw=saw)
    core = make_core(eyes)
    core._answer_muted = lambda: False
    for k, v in VIVERE:
        core.state.set(k, v)
    return core, eyes


@pytest.mark.parametrize("saw", ["Nothing notable.", "No ships in sight.", "An empty landing pad.", "quiet"])
def test_look_at_that_with_nothing_there_is_answered_as_if_the_eyes_had_said_nothing(saw):
    core, eyes = _asking_core(saw)
    said = _ask(core)
    blind, _ = _asking_core(None)
    assert eyes.looks == ["pilot_asked"]
    assert said == _ask(blind) and "What I see" not in said[0]
    assert not any(w in said[0].lower() for w in ("nothing notable", "no ships", "empty", "quiet"))


def test_look_at_that_with_something_there_still_starts_from_it():
    core, _ = _asking_core("A tall lattice tower on a ridge under a yellow sky.")
    assert _ask(core)[0].startswith("What I see: A tall lattice tower on a ridge under a yellow sky.")
