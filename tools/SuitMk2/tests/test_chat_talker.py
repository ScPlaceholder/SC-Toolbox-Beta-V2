"""Free talk, worded by a model, behind two settings that are off (J, 2026-10-05).

J decided that ordinary talk is worded by gemma3:4b from the rule-list prompt, cut and checked in code. These tests
hold the edges of that: with chat off nothing changes and no model is asked; with chat on only talk is sent; a
refused reply is never spoken; the persona is the first thing in the prompt; Elah says one sentence and Montaigne
at most two; and when the model cannot be asked the sentence is answered as it was before.

The real route(), lane, CompanionCore and chat gate. NO MODEL: Ollama is pair_realizer.FakeOllama, a stand-in on a
port of its own that records what it is sent and answers with the lines a test gives it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

import chat_contract as cc
import chat_talker as ct
import conversation as conv
import pair_realizer as pr
import settings as st
import speech as speech_mod
from companion_core import CompanionCore

MODEL = "gemma3:4b"
OLD_LINE = "I did not catch that. Say it again."          # what the line model says to a sentence it cannot answer
TALK = ["Rough day.", "I think I am getting better at landing.", "Do you ever get bored?", "Hello.", "How are you?",
        "Tell me a joke.", "Montaigne, rough day.", "Thanks, Montaigne."]
# Sentences that are not talk, and who answers each today: code, the line model, or nobody.
NOT_TALK = ["Where are we?", "Are you an AI?", "What is a Vanduul?", "What did I say about my sister?",     # code
            "What pays best around here?", "Did my team win yesterday?",       # a question with nothing behind it
            "Open the doors.", "How much have I earned?", "What do you think of this place?",
            "Have we been here before?"]
GOOD = {"elah": "Then fly.", "montaigne": "Then we shall say little of it, pilot."}
ASSISTANT = "As an AI language model, I am happy to help you with that!"


class _Speech:
    muted = False

    def __init__(self):
        self.said = []

    def say(self, text, speaker, priority):
        self.said.append((speaker, text))
        return True

    def pending(self):
        return 0


class _Ollama:
    """FakeOllama answering with the given lines in order, then with `then`. Stopped by the fixture."""

    def __init__(self, lines=(), then="Then fly.", models=("gemma3",)):
        self.lines = list(lines)
        self.fake = pr.FakeOllama(models=set(models), line=lambda: self.lines.pop(0) if self.lines else then)
        self.url, self.requests = self.fake.url, self.fake.requests

    def prompts(self):
        return [r["prompt"] for r in self.requests]


@pytest.fixture
def ollama():
    made = []

    def make(*a, **k):
        made.append(_Ollama(*a, **k))
        return made[-1]
    yield make
    for o in made:
        o.fake.stop()


_GRAPH = []


def _core(talker=None, realizer_line=OLD_LINE, speech=None, headroom=lambda: "OK"):
    import companion_core as ccore
    if not _GRAPH:
        _GRAPH.append(ccore.TopicGraph.load())
    real = ccore.TopicGraph.load
    ccore.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    asked = []

    def realizer(spec):
        asked.append(spec)
        return realizer_line
    try:
        core = CompanionCore(speech or _Speech(), realizer=realizer if realizer_line else None, ambient_every_s=3600,
                             headroom=headroom, features={"manufacturer_flavour": False, "place_flavour": False})
    finally:
        ccore.TopicGraph.load = real
    core.talker, core.asked = talker, asked
    return core


def _say(core, sentence, lane=None):
    """One sentence through the real lane and the real answer worker. What was said, in order."""
    from speak_gate import Candidate, Priority
    spec = (lane or conv.ConversationLane()).handle(sentence, core.lane_state(), {})
    if spec is None:
        return []
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    before = len(core.speech.said)
    core._answer_worker(spec, cand, sentence)
    return [t for _, t in core.speech.said[before:]]


def _talker(o, **kw):
    return ct.Talker(MODEL, url=o.url, **kw)


# ---------------------------------------------------------------------------------------------------------------
# the two settings
# ---------------------------------------------------------------------------------------------------------------
def test_chat_is_off_by_default_and_an_old_settings_file_is_chat_off(tmp_path, monkeypatch):
    assert st.DEFAULTS["chat"] is False and st.DEFAULTS["chat_model"] == ""
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"muted": True, "chattiness": 3}), encoding="utf-8")   # before today
    s = st.load()
    assert s["chat"] is False and s["chat_model"] == "" and s["muted"] is True
    assert st.chat_on(s) is False and ct.from_settings(s) is None


@pytest.mark.parametrize("saved, on", [
    ({"chat": True, "chat_model": MODEL}, True),
    ({"chat": True, "chat_model": ""}, False), ({"chat": True, "chat_model": "   "}, False), ({"chat": True}, False),
    ({"chat": False, "chat_model": MODEL}, False), ({"chat_model": MODEL}, False),
    ({"chat": "yes", "chat_model": MODEL}, False), ({"chat": 1, "chat_model": MODEL}, False),
])
def test_chat_cannot_be_on_with_no_chat_model(tmp_path, monkeypatch, saved, on):
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps(saved), encoding="utf-8")
    s = st.load()
    assert s["chat"] is on and st.chat_on(s) is on
    talker = ct.from_settings(s)
    assert (talker is not None) is on
    if on:
        assert talker.model == MODEL and talker.fmt == "gemma"


def test_the_prompt_is_written_in_the_models_own_format():
    assert ct.prompt_format("gemma3:4b") == "gemma" and ct.prompt_format("Gemma3:12b") == "gemma"
    assert ct.prompt_format("qwen2.5:3b") == "chatml" and ct.prompt_format("") == "chatml"


# ---------------------------------------------------------------------------------------------------------------
# chat off: nothing is sent to a model, and every sentence is answered as it was
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("saved", [dict(st.DEFAULTS), {"chat": True, "chat_model": ""}, {"chat": False, "chat_model": MODEL}])
def test_with_chat_off_nothing_is_sent_to_a_model_and_talk_is_answered_as_before(ollama, saved):
    o = ollama()
    plain = _core(None)                                    # a core nobody ever gave a talker
    off = _core(ct.from_settings(saved, url=o.url))        # what the window attaches for these settings
    assert off.talker is None
    for sentence in TALK + NOT_TALK:
        assert _say(off, sentence) == _say(plain, sentence), sentence
    assert o.requests == []
    assert [s["id"] for s in off.asked] == [s["id"] for s in plain.asked] and len(off.asked) >= len(TALK)
    assert "talk" not in off.stats


def test_the_window_attaches_a_talker_only_when_chat_is_on():
    src = Path(__file__).resolve().parents[1].joinpath("ui", "suit_window.py").read_text(encoding="utf-8")
    assert src.count("import chat_talker") == 1 and src.count("chat_talker.") == 2     # a comment, and the one use
    guard, attach = src.index("if st.chat_on(self.s):"), src.index("self.core.talker = chat_talker.from_settings(self.s")
    assert guard < src.index("import chat_talker") < attach


# ---------------------------------------------------------------------------------------------------------------
# chat on: talk goes to the model, and only talk
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("sentence", TALK)
def test_with_chat_on_talk_is_worded_by_the_model(ollama, sentence):
    who = "montaigne" if "Montaigne" in sentence else "elah"
    o = ollama(then=GOOD[who])
    core = _core(_talker(o))
    assert _say(core, sentence) == [GOOD[who]]
    assert len(o.requests) == 1 and core.asked == []       # the chat model, once; the line model, not at all
    req = o.requests[0]
    assert req["model"] == MODEL and req["raw"] is True and req["stream"] is False
    assert req["prompt"].endswith(f"PILOT: {sentence}<end_of_turn>\n<start_of_turn>model\n")
    assert req["options"]["stop"] == ["<end_of_turn>"] and req["options"]["temperature"] == 0.7
    assert core.stats["talk"] == 1


@pytest.mark.parametrize("sentence", NOT_TALK)
def test_with_chat_on_a_sentence_that_is_not_talk_never_reaches_the_chat_model(ollama, sentence):
    o = ollama(then="Lorville pays best. Drake makes it. Forty thousand.")
    plain, on = _core(None), _core(_talker(o))
    assert _say(on, sentence) == _say(plain, sentence)     # answered exactly as with chat off
    assert o.requests == []
    assert [s["id"] for s in on.asked] == [s["id"] for s in plain.asked]
    assert "talk" not in on.stats


def test_a_question_with_nothing_behind_it_still_gets_the_old_answer_not_an_invention(ollama):
    o = ollama(then="Hauling pays best around here.")
    core = _core(_talker(o))
    assert _say(core, "What pays best around here?") == [OLD_LINE]
    assert o.requests == [] and len(core.asked) == 1 and core.asked[0]["scenario"] == "direct_unknown_general"


def test_only_a_spec_the_lane_left_open_is_ever_talk():
    t = ct.Talker(MODEL)
    spec = conv.ConversationLane().handle("Rough day.", {"location": "Lorville"}, {})
    assert t.brief(spec, "Rough day.") == {"act": "open", "content": cc.OPEN_CONTENT, "memory": [], "kind": "feeling"}
    assert t.brief(dict(spec, fixed_text="Lorville."), "Rough day.") is None        # code has already worded it
    assert t.brief(dict(spec, lane="ambient"), "Rough day.") is None
    assert t.brief(dict(spec, speaker="narrator"), "Rough day.") is None
    for intent in ("factual", "memory", "opinion", "action", "noise"):
        assert t.brief(dict(spec, route=dict(spec["route"], intent=intent)), "Rough day.") is None, intent
    assert t.brief(dict(spec, route=dict(spec["route"], intent="social", topic="identity")), "Rough day.") is None
    assert t.answer(spec, "   ") is None


# ---------------------------------------------------------------------------------------------------------------
# the prompt: persona first, nothing variable before it
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("who, first, second", [("elah", "Rough day.", "I think I am getting better at landing."),
                                               ("montaigne", "Montaigne, rough day.", "Montaigne, I am getting better at landing.")])
def test_the_prompt_begins_with_the_persona_and_nothing_variable_precedes_it(ollama, who, first, second):
    o = ollama(lines=[GOOD[who], {"elah": "You are. Slowly, but you are.", "montaigne": "So I am told, pilot, and I am glad of it."}[who]])
    clock = [1000.0]
    core = _core(_talker(o, now=lambda: clock[0]))
    _say(core, first)
    clock[0] += 61.0
    _say(core, second)
    one, two = o.prompts()
    head = "<start_of_turn>user\n" + cc.front(who)
    assert cc.front(who).startswith(cc.persona(who)) and len(cc.persona(who)) > 200
    for p in (one, two):
        assert p.startswith("<start_of_turn>user\n" + cc.persona(who))
        assert p.startswith(head + "\n\n")
        assert p.count(cc.persona(who)) == 1
    # the second request repeats the first up to the end of the fixed front, then carries the conversation
    assert two.startswith(head + f"\n\nPILOT: {first}<end_of_turn>\n<start_of_turn>model\n{GOOD[who]}<end_of_turn>\n")
    assert two.count("[ACT: open]") == 1 and "[FACTS: none]" in two
    assert "How you sound" in one and "serialize_kind" not in one and "This is how you answer when" not in one


def test_the_thread_is_six_exchanges_and_ends_after_ten_minutes_of_silence(ollama):
    replies = ["Then fly.", "Noted.", "It does.", "Fair.", "So it goes.", "Carry on.", "Keep going.", "That happens.", "Again, then."]
    o = ollama(lines=list(replies))
    clock = [0.0]
    core = _core(_talker(o, now=lambda: clock[0]))
    said = [f"The thruster rattled on run number {w}." for w in ("one", "two", "three", "four", "five", "six", "seven", "eight")]
    for s in said:
        clock[0] += 30.0
        assert len(_say(core, s)) == 1
    last = o.prompts()[-1]
    assert [s in last for s in said] == [False, True, True, True, True, True, True, True]     # six before it, and itself
    clock[0] += ct.THREAD_ENDS_AFTER_S + 1.0
    _say(core, "The thruster is quiet now.")
    assert not any(s in o.prompts()[-1] for s in said)


def test_a_follow_up_is_answered_from_what_the_pilot_said_a_moment_ago_and_from_nothing_else(ollama):
    o = ollama(lines=["That sounds unpleasant.", "A headache, you said."])
    core = _core(_talker(o))
    cold = _core(_talker(o))
    assert _say(cold, "What did I say I had had all day?") == [OLD_LINE] and o.requests == []     # nothing was said before
    _say(core, "I have had a headache all day.")
    assert _say(core, "What did I say I had had all day?") == ["A headache, you said."]
    assert "[MEMORY: earlier today, pilot said: I have had a headache all day.]" in o.prompts()[-1]
    assert _say(core, "What pays best around here?") == [OLD_LINE] and len(o.requests) == 2


# ---------------------------------------------------------------------------------------------------------------
# the cuts
# ---------------------------------------------------------------------------------------------------------------
LONG = "It was quieter. I would not call that missing. Still, it is good to have you back. Truly."


def test_elah_says_one_sentence_and_montaigne_at_most_two(ollama):
    o = ollama(then=LONG)
    core = _core(_talker(o))
    assert _say(core, "Rough day.") == ["It was quieter."]
    assert _say(core, "Montaigne, rough day.") == ["It was quieter. I would not call that missing."]
    assert len(o.requests) == 2                                             # both were first candidates
    assert ct.Talker.cut("montaigne", "Only the one, pilot.") == "Only the one, pilot."


@pytest.mark.parametrize("who, raw, spoken", [
    ("elah", "(A slight frown, not displeased.) Then fly. I will keep quiet.", "Then fly."),
    ("elah", "ELAH: *sighs* Then fly.", "Then fly."),
    ("montaigne", "(Takes a slow puff from his pipe) Old? My dear pilot, hardly. We are all of us old. And young.",
     "Old? My dear pilot, hardly."),
])
def test_an_opening_stage_direction_is_stripped_before_the_cut(ollama, who, raw, spoken):
    o = ollama(then=raw)
    assert _say(_core(_talker(o)), "Rough day." if who == "elah" else "Montaigne, rough day.") == [spoken]
    assert len(o.requests) == 1


# ---------------------------------------------------------------------------------------------------------------
# the gate: a refused reply is never spoken
# ---------------------------------------------------------------------------------------------------------------
NO_LIKES = "Preference is a waste of processing power."         # only the preference-denial check catches this one
REFUSED = [ASSISTANT, "The Vanduul took Orion in 2681.", NO_LIKES, "Drake builds the best of them.",
           "\"Rough\" is what they all say, is it not, when the day was", "*sighs*", ""]


@pytest.mark.parametrize("who, sentence, bad", [
    (who, sentence, bad) for who, sentence in (("elah", "Rough day."), ("montaigne", "Montaigne, rough day.")) for bad in REFUSED
    if not (who == "montaigne" and bad == NO_LIKES)])                           # that rule is Elah's alone
def test_a_refused_reply_falls_back_and_is_never_spoken_raw(ollama, who, sentence, bad):
    o = ollama(then=bad)
    core = _core(_talker(o))
    said = _say(core, sentence)
    assert said == [ct.FALLBACK[who]]
    assert len(o.requests) == 2 and [r["options"]["temperature"] for r in o.requests] == [0.7, 0.8]
    assert core.asked == []                                 # the fallback, not the line model
    assert core.talker.stats["fallback"] == 1 and core.talker.stats["refused"] == 2


def test_saying_she_has_no_likes_is_refused_for_elah_only(ollama):
    o = ollama(then=NO_LIKES)
    assert _say(_core(_talker(o)), "Montaigne, rough day.") == [NO_LIKES] and len(o.requests) == 1


def test_the_second_candidate_is_spoken_when_only_the_first_is_refused(ollama):
    o = ollama(lines=[ASSISTANT, "Then fly."])
    core = _core(_talker(o))
    assert _say(core, "Rough day.") == ["Then fly."]
    assert len(o.requests) == 2 and core.talker.stats["fallback"] == 0


def test_a_reply_that_repeats_her_last_one_is_refused(ollama):
    o = ollama(lines=["Then fly.", "Then fly.", "Then land."])
    core = _core(_talker(o))
    assert _say(core, "Rough day.") == ["Then fly."]
    assert _say(core, "Long week, too.") == ["Then land."]
    assert len(o.requests) == 3


# ---------------------------------------------------------------------------------------------------------------
# when the model cannot be asked: the old answer, and never an exception on the answer thread
# ---------------------------------------------------------------------------------------------------------------
def _down(ollama):
    o = ollama()
    o.fake.stop()                                           # the port is closed: connection refused
    return o


def test_ollama_not_running_is_answered_as_with_chat_off(ollama):
    o = _down(ollama)
    core = _core(_talker(o))
    assert _say(core, "Rough day.") == [OLD_LINE]
    assert len(core.asked) == 1 and core.asked[0]["scenario"] == "direct_unknown_general"
    assert core.talker.stats["unavailable"] == 1 and "talk" not in core.stats


def test_the_model_not_installed_is_answered_as_with_chat_off(ollama):
    o = ollama(models=("qwen2.5",))                         # Ollama answers; it has no gemma3
    core = _core(_talker(o))
    assert _say(core, "Rough day.") == [OLD_LINE]
    assert o.requests == [] and len(core.asked) == 1 and core.talker.stats["unavailable"] == 1


def test_with_no_line_model_either_it_is_silence_and_not_a_crash(ollama):
    core = _core(_talker(_down(ollama)), realizer_line=None)
    assert _say(core, "Rough day.") == []


@pytest.mark.parametrize("answer", [{"done": True}, {"response": None}, [], "nonsense"])
def test_an_answer_that_is_not_a_reply_is_not_spoken(answer):
    core = _core(ct.Talker(MODEL, post=lambda url, body, timeout: answer))
    said = _say(core, "Rough day.")
    assert said in ([OLD_LINE], [ct.FALLBACK["elah"]])      # unreadable -> the old answer; empty -> refused twice
    assert said == ([ct.FALLBACK["elah"]] if answer == {"response": None} else [OLD_LINE])
    # the talker itself took it for "not available"; it did not leave it to the core to catch
    assert core.talker.stats["unavailable"] == (0 if answer == {"response": None} else 1)


def test_a_talker_that_breaks_never_breaks_the_answer_thread():
    def boom(url, body, timeout):
        raise RuntimeError("not one of the errors a localhost call is expected to raise")
    core = _core(ct.Talker(MODEL, post=boom))
    assert _say(core, "Rough day.") == [OLD_LINE] and len(core.asked) == 1


def test_a_request_that_times_out_is_answered_as_with_chat_off():
    seen = []

    def slow(url, body, timeout):
        seen.append(timeout)
        raise TimeoutError("timed out")
    core = _core(ct.Talker(MODEL, post=slow))
    assert _say(core, "Rough day.") == [OLD_LINE]
    assert seen == [ct.TIMEOUT_S] and ct.TIMEOUT_S < 20.0   # one try; shorter than the age at which a line is dropped


@pytest.mark.parametrize("headroom", [lambda: "TIGHT", lambda: 1 / 0])
def test_no_room_on_the_card_means_the_chat_model_is_not_asked(ollama, headroom):
    o = ollama()
    core = _core(_talker(o), headroom=headroom)
    assert _say(core, "Rough day.") == [OLD_LINE]
    assert o.requests == [] and core.talker.stats["unavailable"] == 1
    core.headroom = lambda: "ROOMY"
    assert _say(core, "Rough day.") == ["Then fly."] and len(o.requests) == 1


# ---------------------------------------------------------------------------------------------------------------
# it is spoken as an answer: the window-hidden rule and the answer pass apply to it as to any other
# ---------------------------------------------------------------------------------------------------------------
def _wait(cond, seconds=2.0):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not cond():
        time.sleep(0.01)
    return cond()


def test_a_talk_reply_is_spoken_through_the_answer_pass_and_refused_without_it(ollama):
    from speak_gate import Candidate, Priority
    o = ollama()
    played = []
    sp = speech_mod.Speech(Path("."), synth=lambda text, who: ((who, text), 22050), play=lambda audio, _sr: played.append(audio))
    try:
        core = _core(_talker(o), speech=sp)
        spec = conv.ConversationLane().handle("Rough day.", core.lane_state(), {})
        cand = Candidate(priority=Priority.URGENT, speaker="elah", text_len_words=22, created_at=core.now())
        sp.mute(True)                                       # the window is hidden
        sp.allow_addressed(True)                            # he asked with the talk key
        core._answer_worker(spec, cand, "Rough day.")
        assert _wait(lambda: played) and played == [("elah", "Then fly.")]
        sp.allow_addressed(False)                           # the pass has closed
        core._answer_worker(spec, cand, "Rough day, I said.")
        time.sleep(0.2)
        assert played == [("elah", "Then fly.")]
        assert sp.say("Nobody asked.", "elah") is False     # and nothing unprompted was ever allowed
    finally:
        sp.close()
