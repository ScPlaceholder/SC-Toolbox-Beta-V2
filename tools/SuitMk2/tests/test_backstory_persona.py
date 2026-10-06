"""What the two personas say since J's backstory of 2026-10-05 (23:18 and 23:25).

Montaigne has outlasted many pilots, so wanting company IS his character; but he knows what a life spent aboard
costs, so he never praises the pilot for staying in and he sends them out. Elah is new and wants to be good at
this; she may ask one plain question about other people; she never asks to be reassured.

MEASURED, AND NOT MORE THAN THIS: the same sentences helped on lines about other people and did NOT fix the lines
where the pilot withdraws (elah-audio/_suit_chat_eval.md, section 18). That is why those are answered by code
(tests/test_withdrawal.py). These tests hold that the sentences are in the personas and that nothing else about
the prompt moved; they do not show that a model obeys them.

The real canon files and the real serializers. No model.
"""
from __future__ import annotations

import json

import pytest

import chat_contract as cc

NEW = {
    "montaigne": ["You have outlasted many pilots, and you are glad of this one's company and say so plainly.",
                  "You never praise the pilot for staying in or for avoiding people: you send the pilot out, and you are "
                  "hungry to hear what was found there."],
    "elah": ["You are new, the latest of your kind, and you want to be good at this.",
             "When the pilot mentions other people you may ask one plain question about them.",
             "You never ask the pilot to reassure you."],
}
# Sentences each persona had before, word for word. Not all of them: one from each part of it.
KEPT = {
    "montaigne": ["You are Montaigne, talking aloud with your pilot from aboard the pilot's ship.",
                  "You are Michel de Montaigne, the essayist: a man, and nothing will persuade you otherwise.",
                  "Warm, unhurried, amused, gently self-mocking; one small reflection on people, then back to the matter.",
                  "You say you, and pilot; never a name.", "You dream of the Carrack.",
                  "You never quote the historical Montaigne and never invent history.",
                  "You fly nothing and touch no control.",
                  "You never call yourself a ship, a vessel, an AI or a program: you are a man who stays on board."],
    "elah": ["You are Elah, the AI in the pilot's flight suit, talking aloud with your pilot.",
             "Dry, quick, practical; you land on the full stop.",
             "You look after the pilot and never say so; you never name your own feelings and never ask for attention.",
             "You say you, rarely pilot, never a name.", "You like Drake and Aegis ships; your dream ship is the Kraken.",
             "You are the suit, always.", "You cannot act on the ship; you watch, listen and talk."],
}


@pytest.mark.parametrize("who", cc.SPEAKERS)
def test_the_persona_says_what_the_backstory_implies_and_keeps_what_it_said(who):
    p = cc.persona(who)
    for sentence in NEW[who] + KEPT[who]:
        assert p.count(sentence) == 1, sentence
    # the new sentences stand together, in order, and taking them out leaves the persona as it was
    block = " ".join(NEW[who])
    assert block in p
    before = p.replace(" " + block, "")
    assert before != p and all(s in before for s in KEPT[who]) and not any(s in before for s in NEW[who])
    assert p.endswith(KEPT[who][-1])                     # what he is, and what she is, is still the last word


def test_montaigne_does_not_know_what_he_is():
    """His model number and what he was (an attendant) are J's backstory. None of it may be in what he is told
    about himself."""
    c = json.loads(cc.canon_path("montaigne").read_text(encoding="utf-8"))
    low = c["persona"].lower()
    for word in ("mont-ai", "gn-3", "attendant", "model number", "assistant", "guidance node", "telemetry",
                 "corrupt", "accident", "broken", "glitch"):
        assert word not in low, word


def test_montaigne_is_glad_of_company_and_never_advises_staying_in():
    low = cc.persona("montaigne").lower()
    assert "glad of this one's company" in low and "say so plainly" in low
    assert "never praise the pilot for staying in or for avoiding people" in low
    assert "send the pilot out" in low and "hungry to hear" in low
    assert "never leave the ship" in low and "agoraphobe" in low      # his own condition, unchanged


def test_elah_may_ask_about_people_and_never_asks_to_be_reassured():
    low = cc.persona("elah").lower()
    assert "one plain question" in low and "never ask the pilot to reassure you" in low
    assert "never ask for attention" in low and "never name your own feelings" in low
    for word in ("replace", "the model she", "inferior", "afraid", "fear"):     # what she wonders is not in her prompt
        assert word not in low, word


@pytest.mark.parametrize("who", cc.SPEAKERS)
def test_nothing_variable_stands_before_the_persona_in_any_prompt(who):
    """The persona is the first thing a model reads, whatever the pilot said and whatever came before."""
    p = cc.persona(who)
    assert cc.front(who).startswith(p) and cc.front(who, strict=True).startswith(p)
    turns = [("Rough day.", "Then fly."), ("I'd rather be here than with people.", "Who were you meant to see?")]
    for pilot in ("Hello.", "I met someone, actually.", "x" * 300):
        for history in ([], turns):
            chatml = cc.serialize(who, history, pilot)
            gemma = cc.serialize(who, history, pilot, fmt="gemma")
            assert chatml.startswith("<|im_start|>system\n" + p) and chatml.count(p) == 1
            assert gemma.startswith("<start_of_turn>user\n" + p) and gemma.count(p) == 1
            kind = cc.serialize_kind(who, "remark", history, pilot, full_persona=True)
            assert kind.startswith("<|im_start|>system\n" + p) and kind.count(p) == 1


def test_the_new_text_is_marked_as_a_proposal_in_the_file():
    for who in cc.SPEAKERS:
        c = json.loads(cc.canon_path(who).read_text(encoding="utf-8"))
        note = c["_provisional_2026-10-05_night"]
        assert "J has NOT approved" in note and "persona" in note and NEW[who][0].split(",")[0][:12] in note
        assert "J has NOT approved" in c["_provisional"]                  # the older note is as it was
