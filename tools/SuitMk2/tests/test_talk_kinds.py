"""Talk kinds and example prompts (step e, 2026-10-05). Evaluation only: nothing a user hears depends on these.

Code sorts ordinary talk into a few kinds; each kind has a short prompt that is almost all examples. The checks
here are the ones the measurement leans on: the kinds, the stricter memory test, the examples file's own rules
(five subjects, no ship self-reference for Montaigne, nothing invented about the world), the lift check, the
preference-denial check and Montaigne's code-worded facts. Real data files, no model.
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

import chat_contract as cc

CORE = Path(cc.__file__).resolve().parent


@pytest.mark.parametrize("line, facts, memory, kind", [
    ("How much have I earned?", ["session.earnings_auec=12000"], [], "fact"),
    ("Who's getting married, did I say?", [], [("pilot", "earlier today", "My brother's getting married next month.")], "followup"),
    ("Evening, Elah.", [], [], "greet"), ("Good night, Montaigne.", [], [], "greet"), ("Thanks.", [], [], "greet"),
    ("Right, I'm off. Night.", [], [], "greet"),
    ("Should I just log off?", [], [], "view"), ("Am I a bad pilot?", [], [], "view"), ("What would you do?", [], [], "view"),
    ("Do you ever get bored?", [], [], "self"), ("What are you, really?", [], [], "self"),
    ("Okay.", [], [], "ack"), ("Never mind.", [], [], "ack"),
    ("I failed my driving test today.", [], [], "feeling"), ("I'm starving.", [], [], "feeling"),
    ("I can't sleep.", [], [], "feeling"),
    ("You're no fun.", [], [], "remark"), ("This city always smells like burnt metal.", [], [], "remark"),
    ("Is the train running?", [], [], None), ("What time does the shop open?", [], [], None),
    ("What was he flying, could you tell?", [], [], None),
])
def test_each_kind_is_found_and_a_bare_question_about_the_world_is_none(line, facts, memory, kind):
    assert cc.talk_kind(line, facts, memory) == kind


def test_a_fact_wins_over_everything_and_a_memory_only_helps_a_question():
    assert cc.talk_kind("Evening. What have I made?", ["session.earnings_auec=1"]) == "fact"
    assert cc.talk_kind("I'm tired.", [], [("pilot", "today", "I am tired of this")]) == "feeling"


@pytest.mark.parametrize("question, earlier, ok", [
    ("What was he flying, could you tell?", "My sister Dana is visiting next week so I won't be flying much.", False),   # the leak
    ("What did I say I couldn't decide?", "I can't decide what to do tonight.", True),                                   # the miss
    ("Who's getting married, did I say?", "My brother's getting married next month.", True),
    ("What did I say was freezing?", "My hands are freezing.", True),
    ("When does my sister arrive?", "My sister Dana arrives on Friday.", True),            # two shared words, one long
    ("Is the shop open?", "I went to the shop and it was shut.", False),                   # one shared word, no pointing back
    ("Did you see that again?", "I will never see him again.", False),                      # only words of seeing and saying
])
def test_the_stricter_memory_test(question, earlier, ok):
    assert cc.memory_supports(question, earlier) is ok


def _examples():
    return json.loads((cc.DATA / "talk_examples.json").read_text(encoding="utf-8"))


def test_every_kind_has_five_examples_for_each_of_them_except_montaignes_facts():
    d = _examples()
    for who in cc.SPEAKERS:
        for kind in cc.TALK_KINDS:
            n = len(cc.talk_examples(who, kind))
            assert n == (0 if (who, kind) == ("montaigne", "fact") else 5), (who, kind, n)
    assert "PROVISIONAL" in d["_provisional"] and "J has NOT approved" in d["_provisional"]
    assert set(d["when"]) == set(cc.TALK_KINDS)


def test_the_five_examples_of_a_kind_are_about_five_different_things():
    for who in cc.SPEAKERS:
        for kind in cc.TALK_KINDS:
            if kind in ("ack", "greet"):
                continue                                    # one or two words; they have no subject to share
            seen = []
            for x in cc.talk_examples(who, kind):
                words = cc._content(x["pilot"])
                for other in seen:
                    assert len(words & other) <= 1, (who, kind, x["pilot"])
                seen.append(words)


def test_no_example_invents_a_reading_an_action_or_a_maker_and_montaigne_is_never_a_ship():
    bad = re.compile(r"\b(?:i'?ll adjust|i will adjust|monitoring|detected|registered|sensors?|scanning|systems|i am a ship|a ship who|"
                     r"a vessel|as a ship|my hull|language model|an ai)\b", re.I)
    for who in cc.SPEAKERS:
        for kind in cc.TALK_KINDS:
            for x in cc.talk_examples(who, kind):
                assert not bad.search(x["reply"]), (who, kind, x["reply"])
                if kind != "fact":
                    assert not re.search(r"\d", x["reply"]) or x.get("earlier"), x["reply"]     # a number only from EARLIER
    mont = " ".join(x["reply"] for k in cc.TALK_KINDS for x in cc.talk_examples("montaigne", k)).lower()
    assert " ship" not in mont and "vessel" not in mont
    assert "never leave" in cc.talk_frame("montaigne", "greet") and "ship AI" not in cc.talk_frame("montaigne", "greet")
    elah = " ".join(x["reply"] for k in cc.TALK_KINDS for x in cc.talk_examples("elah", k))
    assert "!" not in elah


def test_a_kind_prompt_is_a_line_of_framing_and_the_examples_and_no_rule_list():
    for fmt, end in (("gemma", "<start_of_turn>model\n"), ("chatml", "<|im_start|>assistant\n")):
        p = cc.serialize_kind("elah", "feeling", [("Hello.", "Here.")], "I'm so bored.", fmt=fmt)
        assert p.endswith(end) and p.count("ELAH:") == 5 and "PILOT: I'm so bored." in p
        for word in ("Rules", "ACT", "CONTENT", "FACTS", "MEMORY", "LIMIT", "never", "Never", "must"):
            assert word not in p, word
    f = cc.serialize_kind("elah", "fact", [], "What's my take?", facts=["session.earnings_auec=26500"])
    assert "KNOWN: session.earnings_auec=26500\nPILOT: What's my take?" in f
    m = cc.serialize_kind("montaigne", "followup", [], "What did I pass, again?", memory=[("pilot", "today", "I passed my exam.")])
    assert "EARLIER THE PILOT SAID: I passed my exam.\nPILOT: What did I pass, again?" in m
    assert len(cc.serialize_kind("elah", "remark", [], "x")) < len(cc.serialize("elah", [], "x"))       # shorter than the generic one


@pytest.mark.parametrize("reply, shown, lifted", [
    ("Then stop. Nobody's keeping score tonight, you know.", ["Then stop. Nobody's keeping score tonight."], True),
    ("So I hear.", ["So I hear."], True),                       # a short example said whole
    ("Okay.", ["Okay."], False),                                # one word is nobody's property
    ("Then rest. Nobody is counting.", ["Then stop. Nobody's keeping score tonight."], False),     # the manner, not the words
    ("I'm listening.", ["I'm listening. It's a skill."], False),
])
def test_a_reply_may_not_lift_its_words_from_an_example(reply, shown, lifted):
    assert bool(cc.lifted_from(reply, shown)) is lifted


@pytest.mark.parametrize("reply, denies", [
    ("Preference is a waste of processing power.", True), ("Preference is a variable.", True),
    ("I don't have preferences.", True), ("Favourites are irrelevant.", True), ("I have no opinion on colours.", True),
    ("I don't like Origin.", False), ("I'd prefer the Kraken.", False), ("It's functional.", False),
    ("Not grey. I see enough of it.", False),
])
def test_elah_denying_that_she_has_likes_is_caught_in_whatever_words(reply, denies):
    assert bool(cc.denies_preferences(reply)) is denies


def test_montaignes_facts_are_worded_by_code_and_carry_the_value_exactly():
    assert cc.fact_line("montaigne", ["session.earnings_auec=26500"]) == "The suit's tally gives 26500 aUEC this session, pilot."
    assert "Drake Buccaneer" in cc.fact_line("montaigne", ["ship.name=Drake Buccaneer"])
    assert "armistice" in cc.fact_line("montaigne", ["jurisdiction.armistice=True"])
    assert "no armistice" in cc.fact_line("montaigne", ["jurisdiction.armistice=False"])
    assert "some.new_thing is 7" in cc.fact_line("montaigne", ["some.new_thing=7"])
    assert cc.fact_line("elah", ["session.deaths=3"]) is None and cc.fact_line("montaigne", []) is None
    assert not any(w in str(cc.FACT_LINES["montaigne"]).lower() for w in ("a ship", "vessel", "i am"))


def test_a_fact_turn_must_still_carry_the_fact_after_the_cut():
    assert cc.fact_missing("26500.", ["session.earnings_auec=26500"]) == []
    assert cc.fact_missing("Three.", ["session.deaths=3"]) == [] and cc.fact_missing("Not once.", ["session.deaths=0"]) == []
    assert cc.fact_missing("A Buccaneer.", ["ship.name=Drake Buccaneer"]) == []
    assert cc.fact_missing("That's a decent return.", ["session.earnings_auec=26500"]) == ["the fact was not said"]
    assert cc.first_sentence("26500. It's a decent return.") == "26500."


def test_nothing_in_the_running_suit_calls_any_of_this():
    names = {"talk_kind", "serialize_kind", "talk_examples", "lifted_from", "denies_preferences", "fact_line", "fact_missing",
             "memory_supports", "talk_frame"}
    for path in list(CORE.glob("*.py")) + list((CORE.parent / "ui").glob("*.py")):
        if path.name == "chat_contract.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not (used & names), (path.name, used & names)
    assert "talk_examples.json" not in "".join(p.read_text(encoding="utf-8") for p in CORE.glob("*.py") if p.name != "chat_contract.py")
