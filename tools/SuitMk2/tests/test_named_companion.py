"""The Suit decides who answers, unless the pilot says which one (J, 2026-10-05).

"Oh have it decide unless the user specifically says an ai". SuitMk2 has one talk key and two companions. The
conversation lane (core/conversation.py) picks the speaker from the kind of question, and a companion ADDRESSED
by name answers instead. A name that is only mentioned decides nothing.

These drive the real route() and the real ConversationLane; nothing is replaced.
"""
from __future__ import annotations

import pytest

import conversation as conv


def speaker(sentence: str) -> str:
    spec = conv.ConversationLane().handle(sentence, dict(conv.FULL_STATE), dict(conv.FULL_HIST))
    assert spec is not None, sentence
    assert spec["speaker"] == spec["route"]["addressee"] == conv.route(sentence)[0]
    return spec["speaker"]


# The lane's own choice for each kind of question, with nobody named. "where are we" is Elah's by default and
# "have we been here before" is Montaigne's, so each name below is shown overriding a choice that was the other's.
ELAHS = "where are we"
MONTAIGNES = "have we been here before"


def test_with_nobody_named_the_lane_chooses_as_it_always_has():
    assert speaker(ELAHS) == "elah"
    assert speaker(MONTAIGNES) == "montaigne"
    assert speaker("what do you think of this place") == "montaigne"
    assert speaker("how are you") == "elah"
    assert conv._DEFAULT_ADDRESSEE == {"factual": "elah", "memory": "montaigne", "opinion": "montaigne",
                                       "social": "elah", "action": "elah", "unknown": "elah", "noise": "elah"}


@pytest.mark.parametrize("sentence", [
    "Montaigne, where are we?",              # first word
    "Montaigne where are we",                # first word, no comma heard
    "Hey Montaigne, where are we?",          # after a greeting
    "ok so Montaigne where are we",
    "Where are we, Montaigne?",              # last word
    "where are we montaigne",
    "Tell me, Montaigne, where are we?",     # set off by commas in the middle
])
def test_montaigne_addressed_by_name_answers_what_would_have_been_elahs(sentence):
    assert speaker(sentence) == "montaigne"
    assert "montaigne" not in conv.route(sentence)[2]["text"], "the address was left in the question"


@pytest.mark.parametrize("sentence", [
    "Elah, have we been here before?",
    "Elah have we been here before",
    "hey Elah have we been here before",
    "Have we been here before, Elah?",
    "So, Elah, have we been here before?",
])
def test_elah_addressed_by_name_answers_what_would_have_been_montaignes(sentence):
    assert speaker(sentence) == "elah"
    assert "elah" not in conv.route(sentence)[2]["text"]


@pytest.mark.parametrize("heard", ["Ella", "Ela", "Ellah", "Eila", "Aila", "Ayla", "Eli", "Eller"])
def test_elah_as_speech_to_text_writes_her_name(heard):
    assert speaker("%s, have we been here before?" % heard) == "elah"
    assert speaker("have we been here before %s" % heard.lower()) == "elah"


@pytest.mark.parametrize("heard", ["Montagne", "Montane", "Montaine", "Montain", "Montange", "Michel", "Mountain"])
def test_montaigne_as_speech_to_text_writes_his_name(heard):
    assert speaker("%s, where are we?" % heard) == "montaigne"


def test_the_names_heard_are_an_explicit_list_and_ordinary_words_are_not_on_it():
    assert set(conv._NAMES) == {"elah", "montaigne"}
    assert conv._NAMES["elah"] == ("elah", "ela", "ella", "ellah", "eila", "aila", "ayla", "eli", "eller")
    assert conv._NAMES["montaigne"] == ("montaigne", "montagne", "montane", "montaine", "montain", "montange",
                                        "michel")
    # close to a name, and not a name: nothing fuzzy is going on
    for word in ("else", "elk", "ally", "alley", "mountains", "montana", "month", "main", "mention", "ellipse"):
        assert speaker("%s have we been here before" % word) == "montaigne", word
        assert speaker("%s where are we" % word) == "elah", word
    # "mountain" is an ordinary word: it calls Montaigne only as the first or the last word
    assert speaker("where are we on this mountain range") == "elah"
    assert speaker("is that a mountain") == "elah"


@pytest.mark.parametrize("sentence, lanes_choice", [
    ("what did Montaigne say about where we are", "elah"),            # "where we are": Elah's question
    ("did Montaigne tell you where we are", "elah"),
    ("does Elah know if we have been here before", "montaigne"),      # Montaigne's question
    ("I asked Elah and have we been here before", "montaigne"),
])
def test_a_name_only_mentioned_does_not_decide_who_answers(sentence, lanes_choice):
    assert speaker(sentence) == lanes_choice
    name = "montaigne" if "Montaigne" in sentence else "elah"
    assert name in conv.route(sentence)[2]["text"], "a mentioned name was cut out of the sentence"


def test_with_both_named_the_first_one_addressed_answers():
    assert speaker("Elah, ask Montaigne where we are") == "elah"                  # he is only mentioned
    assert speaker("Montaigne, did Elah say where we are") == "montaigne"
    assert speaker("Montaigne, Elah, where are we") == "montaigne"                # both addressed: the first
    assert speaker("Elah, Montaigne, have we been here before") == "elah"
    assert speaker("what did Montaigne say about where we are, Elah?") == "elah"  # mentioned first, she is addressed
    assert speaker("did Elah say we have been here before, Montaigne?") == "montaigne"


def test_being_called_by_name_and_nothing_else_is_a_greeting_to_that_one():
    for sentence, who in (("Elah?", "elah"), ("Montaigne.", "montaigne"), ("hey Montaigne", "montaigne")):
        assert conv.route(sentence)[:2] == (who, "social"), sentence


def test_the_lanes_own_cases_still_pass():
    assert conv._run_cases(dict(conv.FULL_STATE), dict(conv.FULL_HIST), True) == []
