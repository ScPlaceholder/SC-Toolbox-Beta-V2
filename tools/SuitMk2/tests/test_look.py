"""'Wow look at that!' summons the eyes (J, 2026-10-05).

"'Wow look at that!' should summon the eyes on the word look." A sentence that points at something makes the Suit
take ONE look at the screen, and the answer starts from what the eyes report, followed by what is known of the
place. What the eyes report is an OBSERVATION, said word for word; it is never what the thing is for.

The real route() and lane, the real CompanionCore answer worker, and the real eyes.Eyes with its own rules, fed a
fake screen, a fake foreground window and a fake vision model. No screen is captured and no model runs.
"""
from __future__ import annotations

import threading
import time

import pytest

import conversation as conv
import eyes as eyes_mod
import place_knowledge as pk
from companion_core import CompanionCore

TOWER = "What's that big tower do?"
WOW = "wow look at that!"
VIVERE = {"location": "Vivere OLP", "location_body": "Aberdeen", "location_type": "outpost", "location_named": True}
SAW = "A tall lattice tower on a ridge under a yellow sky."
SAW_CLEAN = "A tall lattice tower on a ridge under a yellow sky"
FACT = "Each Hathor site pairs an orbital laser platform with a cluster of planetary alignment facilities."
NO_EYES = "I can't tell which big tower you mean from here."


class _Speech:
    muted = False

    def __init__(self):
        self.said = []

    def say(self, text, speaker, priority):
        self.said.append((speaker, text))
        return True

    def pending(self):
        return 0


class _Eyes:
    """Stands where the eyes stand in the core: look(reason) -> a description or None."""

    def __init__(self, saw=SAW, delay=0.0):
        self.saw, self.delay, self.calls = saw, delay, []

    def look(self, reason="curiosity"):
        self.calls.append(reason)
        if self.delay:
            time.sleep(self.delay)
        return self.saw


_GRAPH = []


def _core(eyes=None):
    import companion_core as cc
    if not _GRAPH:
        _GRAPH.append(cc.TopicGraph.load())
    real = cc.TopicGraph.load
    cc.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    try:
        core = CompanionCore(_Speech(), realizer=None, eyes=eyes, ambient_every_s=3600,
                             features={"manufacturer_flavour": False, "place_flavour": False})
    finally:
        cc.TopicGraph.load = real
    core._answer_muted = lambda: False
    for k, v in (("location_name", "Vivere OLP"), ("location_body", "Aberdeen"), ("location_type", "outpost"),
                 ("location_named", True)):
        core.state.set(k, v)
    return core


def _ask(core, sentence):
    from speak_gate import Candidate, Priority
    spec = conv.ConversationLane(core.place_knowledge()).handle(sentence, core.lane_state(), {})
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    before = len(core.speech.said)
    core._answer_worker(spec, cand, sentence)
    return [t for _, t in core.speech.said[before:]]


# ---------------------------------------------------------------------------------------------------------------
# which sentences summon the eyes
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [
    WOW, TOWER, "look at that", "look", "Look!", "look at that tower", "whoa, look at that", "check that out",
    "do you see that", "can you see that tower", "see that", "what's that", "what is that", "what are those",
    "what does that thing over there do", "what am I looking at", "what's over there", "wow, that's huge",
])
def test_these_summon_the_eyes(sentence):
    spec = conv.ConversationLane().handle(sentence, dict(VIVERE), {})
    assert spec["route"]["topic"] == "place_about" and spec["place"]["look"] is True


@pytest.mark.parametrize("sentence", [
    "what is this place?", "What's this place?", "where are we", "what's this ship", "what do you think of this place",
    "how are you", "look after yourself", "I look terrible today", "what did I say about my sister",
])
def test_these_do_not(sentence):
    spec = conv.ConversationLane().handle(sentence, dict(VIVERE), {})
    assert spec is None or not (spec.get("place") or {}).get("look")


# ---------------------------------------------------------------------------------------------------------------
# one look, and the answer starts from it
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", [WOW, TOWER])
def test_the_trigger_fires_one_look_and_the_answer_starts_from_what_was_seen(sentence):
    eyes = _Eyes()
    core = _core(eyes)
    said = _ask(core, sentence)
    assert eyes.calls == ["pilot_asked"]                         # once
    assert len(said) == 1 and said[0].startswith(f"What I see: {SAW_CLEAN}.")
    assert "This is Vivere OLP, on Aberdeen." in said[0] and "Hathor" in said[0]
    assert "can't" not in said[0]                                # she saw it: no "I can't tell which"


def test_no_trigger_no_look():
    eyes = _Eyes()
    core = _core(eyes)
    said = _ask(core, "what is this place?")
    assert eyes.calls == [] and said and "What I see" not in said[0]


def test_presence_off_means_no_eyes_and_the_answer_is_what_it_was():
    core = _core(eyes=None)                                      # the window passes no eyes when Presence is off
    assert _ask(core, TOWER) == [f"{NO_EYES} This is Vivere OLP, on Aberdeen. {FACT}"]


def test_with_no_vision_model_she_says_what_she_said_before():
    eyes = _Eyes(saw=None)                                       # eyes.look() returns None: no glance model
    core = _core(eyes)
    assert _ask(core, TOWER) == [f"{NO_EYES} This is Vivere OLP, on Aberdeen. {FACT}"]
    assert eyes.calls == ["pilot_asked"]


def test_montaigne_has_it_from_the_suits_eyes():
    eyes = _Eyes()
    core = _core(eyes)
    said = _ask(core, "Montaigne, what's that big tower do?")
    assert said[0].startswith(f"The suit's eyes report this: {SAW_CLEAN}. The suit's feed says this is Vivere OLP")


def test_what_was_seen_that_is_not_what_was_asked_about():
    core = _core(_Eyes("Dusty buildings and a landed ship, haze everywhere."))
    said = _ask(core, TOWER)
    assert said[0].startswith("What I see: Dusty buildings and a landed ship, haze everywhere. "
                              "I can't tell which big tower you mean in that.")


def test_a_dev_history_answer_does_not_look():
    eyes = _Eyes()
    core = _core(eyes)
    spec = conv.ConversationLane(core.place_knowledge()).handle(TOWER, core.lane_state(), {})
    assert core._look_for(dict(spec, aside="dev_fact"), lambda t: None)["place"]["saw"] == ""
    assert eyes.calls == []


# ---------------------------------------------------------------------------------------------------------------
# the eyes' own rules are the rules: the real Eyes class
# ---------------------------------------------------------------------------------------------------------------
def _real_eyes(foreground="StarCitizen.exe", glance="default", headroom="OK", presence="occasional", kept=None):
    from PIL import Image
    calls = []

    def model(jpeg):
        calls.append(len(jpeg))
        return {"scene": "on_foot", "confidence": 0.9, "notable": SAW}
    e = eyes_mod.Eyes(presence=presence, grab=lambda: Image.new("RGB", (320, 180), (90, 80, 40)),
                      foreground=lambda: foreground, glance=model if glance == "default" else glance,
                      headroom=lambda: headroom, on_glance=(lambda j, g: kept.append(g)) if kept is not None else None)
    return e, calls


def test_the_real_eyes_look_once_when_the_game_is_in_front():
    e, calls = _real_eyes()
    said = _ask(_core(e), WOW)
    assert len(calls) == 1 and said[0].startswith(f"What I see: {SAW_CLEAN}.")


def test_a_look_is_allowed_on_the_lowest_presence_setting():
    e, calls = _real_eyes(presence="occasional")
    _ask(_core(e), WOW)
    assert len(calls) == 1


@pytest.mark.parametrize("why, kwargs", [
    ("another window is in front", {"foreground": "chrome.exe"}),
    ("nothing is in front", {"foreground": None}),
    ("the game needs the card", {"headroom": "TIGHT"}),
    ("there is no vision model", {"glance": None}),
])
def test_the_real_eyes_refuse_and_the_answer_is_the_one_without_eyes(why, kwargs):
    e, calls = _real_eyes(**kwargs)
    said = _ask(_core(e), TOWER)
    assert calls == [], why
    assert said == [f"{NO_EYES} This is Vivere OLP, on Aberdeen. {FACT}"]


def test_the_screenshot_is_kept_only_by_the_eyes_own_opt_in():
    kept = []
    e, _ = _real_eyes(kept=kept)                                 # on_glance is training_shots, which keeps on opt-in
    _ask(_core(e), WOW)
    assert len(kept) == 1 and kept[0]["reason"] == "pilot_asked"
    e2, calls = _real_eyes()                                     # no on_glance: nothing is handed anywhere
    core = _core(e2)
    _ask(core, WOW)
    assert calls and not hasattr(core, "_last_shot")


def test_asking_twice_in_a_few_seconds_looks_once():
    e, calls = _real_eyes()
    core = _core(e)
    first, second = _ask(core, WOW), _ask(core, WOW)
    assert len(calls) == 1                                       # eyes.py will not look twice inside 20 s
    assert first[0].startswith("What I see:") and second[0].split(": ", 1)[1].startswith(SAW_CLEAN)


def test_what_was_seen_a_while_ago_is_not_passed_off_as_now():
    e, calls = _real_eyes(foreground="chrome.exe")              # the eyes will not look
    core = _core(e)
    core._last_saw = (core.now() - 60.0, SAW)                    # something they saw a minute ago
    assert _ask(core, TOWER) == [f"{NO_EYES} This is Vivere OLP, on Aberdeen. {FACT}"]


# ---------------------------------------------------------------------------------------------------------------
# what the pilot hears while the look runs
# ---------------------------------------------------------------------------------------------------------------
def test_a_slow_look_gets_looking_first_then_the_answer(monkeypatch):
    monkeypatch.setattr(CompanionCore, "LOOK_HOLD_AFTER_S", 0.05)
    monkeypatch.setattr(CompanionCore, "LOOK_GIVE_UP_S", 5.0)
    core = _core(_Eyes(delay=0.4))
    said = _ask(core, WOW)
    assert said[0] == "Looking." and said[1].startswith(f"Eyes on it: {SAW_CLEAN}.") or \
        said[0] == "Looking." and said[1].startswith(f"What I see: {SAW_CLEAN}.")
    assert len(said) == 2
    core = _core(_Eyes(delay=0.4))
    assert _ask(core, "Montaigne, look at that")[0] == "One moment, pilot; I am asking the suit's eyes."


def test_a_quick_look_says_nothing_first(monkeypatch):
    monkeypatch.setattr(CompanionCore, "LOOK_HOLD_AFTER_S", 1.0)
    said = _ask(_core(_Eyes(delay=0.0)), WOW)
    assert len(said) == 1 and said[0] != "Looking."


def test_a_look_that_never_comes_back_is_given_up_on(monkeypatch):
    monkeypatch.setattr(CompanionCore, "LOOK_HOLD_AFTER_S", 0.05)
    monkeypatch.setattr(CompanionCore, "LOOK_GIVE_UP_S", 0.3)
    release = threading.Event()

    class Stuck(_Eyes):
        def look(self, reason="curiosity"):
            self.calls.append(reason)
            release.wait(5)
            return SAW
    t0 = time.time()
    said = _ask(_core(Stuck()), TOWER)
    release.set()
    assert time.time() - t0 < 2.0
    assert said == ["Looking.", f"{NO_EYES} This is Vivere OLP, on Aberdeen. {FACT}"]


def test_looking_goes_out_by_the_answers_own_way_of_speaking():
    """So it is spoken through the answer pass and no other way: _say_answer keeps its three callers
    (tests/test_voice_gate.py counts them)."""
    src = open(__import__("companion_core").__file__, encoding="utf-8").read()
    body = src[src.index("    def _look_for"):src.index("    def _answer_worker")]
    assert "say_now(hold)" in body and "_say_answer(" not in body and "self.speech.say(" not in body


# ---------------------------------------------------------------------------------------------------------------
# an observation is not a fact
# ---------------------------------------------------------------------------------------------------------------
def _spec(sentence=TOWER, who=""):
    from topic_graph import TopicGraph
    if not _GRAPH:
        _GRAPH.append(TopicGraph.load())
    return conv.ConversationLane(pk.PlaceKnowledge(_GRAPH[0])).handle(who + sentence, dict(VIVERE), {})


def test_it_enters_as_an_observation_and_nothing_else_changes():
    spec = _spec()
    seen = conv.with_observation(spec, SAW)
    new = [c for c in seen["claims"] if c not in spec["claims"]]
    assert new == [{"id": new[0]["id"], "kind": "OBSERVED", "predicate": "eyes.saw", "value": SAW_CLEAN}]
    assert [c for c in seen["claims"] if c["predicate"].startswith(("topic.", "location."))] == \
           [c for c in spec["claims"] if c["predicate"].startswith(("topic.", "location."))]
    assert seen["fixed_text"].endswith(spec["fixed_text"].split("from here. ", 1)[1])     # place and fact, unchanged
    assert conv.ground_direct(seen, seen["fixed_text"]) == []


@pytest.mark.parametrize("saw, why", [
    ("Three towers on a ridge.", "a number word"),
    ("A tower with 4 dishes on it.", "a digit"),
    ("A tall tower near Lorville.", "a place the claims do not have"),
    ("The Drake Cutlass parked by a tower.", "a ship the claims do not have"),
    ("A tower on the left of the screen.", "a position on the screen"),
    ("A tower in the top right corner.", "a position on the screen"),
    ("A tower just above the crosshair.", "a position on the screen"),
    ('A sign reading "Keep Out" on a tower.', "a quotation"),
    ("A tall thin tower made of grey metal lattice standing alone on a long ridge of yellow rock under a hazy sky "
     "with dust.", "too long to be a glance"),
    ("", "nothing"),
])
def test_a_report_that_carries_a_number_a_name_or_a_position_is_left_out_whole(saw, why):
    spec = _spec()
    assert pk.clean_observation(saw, spec) is None, why
    assert conv.with_observation(spec, saw) is None
    core = _core(_Eyes(saw))
    assert _ask(core, TOWER) == [f"{NO_EYES} This is Vivere OLP, on Aberdeen. {FACT}"]


def test_a_name_or_number_the_claims_already_have_may_be_said():
    spec = _spec()                                               # the claims hold Vivere OLP, Aberdeen and Hathor
    assert pk.clean_observation("A Hathor platform above Aberdeen.", spec) == "A Hathor platform above Aberdeen"
    assert pk.clean_observation("A platform above Daymar.", spec) is None
    mont = _spec(who="Montaigne, ")                              # his claims have no Hathor
    assert pk.clean_observation("A Hathor platform above Aberdeen.", mont) is None


def test_what_was_seen_is_said_as_reported_and_the_purpose_still_cannot_be_invented():
    seen = conv.with_observation(_spec(), SAW)
    loose = dict(seen, length_words=[1, 80])
    line = seen["fixed_text"]
    assert conv.ground_direct(loose, line) == []
    for bad, why in (
            (line.replace(SAW_CLEAN, "A tall lattice tower on a ridge"), "does not say what the eyes reported"),
            (line + " That tower is the comms relay.", "says more than its claims"),
            (line + " The tower powers the laser platform.", "says something about the thing it cannot see"),
            (line.replace("What I see:", "The big tower is this:"), "says something about the thing it cannot see"),
    ):
        fails = conv.ground_direct(loose, bad)
        assert any(why in f for f in fails), (bad, fails)


def test_the_ban_on_positions_still_holds_in_eyes_py():
    """Nothing here added a field to what the eyes expose. Their schema has scene, confidence and a sentence."""
    assert set(eyes_mod.GLANCE_SCHEMA["properties"]) == {"scene", "confidence", "notable"}
    assert eyes_mod.GLANCE_SCHEMA["additionalProperties"] is False
    for word in ("left", "right", "top", "corner", "crosshair", "coordinates"):
        assert pk.SCREEN_POSITION.search(f"a tower on the {word}")
    for fine in ("a tower ahead on a ridge", "a ship in the distance", "a figure under a yellow sky"):
        assert not pk.SCREEN_POSITION.search(fine)
