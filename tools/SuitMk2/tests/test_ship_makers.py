"""Who makes a ship is looked up, never remembered (J, 2026-10-05), and the flipped default.

"I love this ship." was answered "It is a Drake." when the ship was an Aegis. The maker comes from the Suit's own
ship list; a maker's name in a model's reply that the turn did not supply is refused by the gate.
Real data files, no model.
"""
from __future__ import annotations

import pytest

import chat_contract as cc
import manufacturers as mf
import ship_makers as sm


def test_every_ship_in_the_list_gets_its_own_maker_and_none_gets_a_wrong_one():
    c = sm.coverage()
    assert c["ships"] >= 240 and c["maker_of_right"] == c["ships"] and c["wrong"] == []
    assert c["found_in_a_sentence"] >= c["ships"] - 1, c["not_found"]            # "X1" is too short to find in talk


@pytest.mark.parametrize("sentence, expect", [
    ("My mate flies a Cutlass Red.", {"Cutlass Red": "Drake Interplanetary"}),
    ("the cutlass is fine", {"Cutlass": "Drake Interplanetary"}),                            # any variant: same maker
    ("I want a Kraken Privateer one day.", {"Kraken Privateer": "Drake Interplanetary"}),    # the longest name wins, once
    ("I used to have a Connie.", {"connie": "Roberts Space Industries"}),
    ("The MSR is the prettiest ship in the game.", {"msr": "Crusader Industries"}),
    ("Thinking of trading the Titan for a Vulture.", {"titan": "Aegis Dynamics", "Vulture": "Drake Interplanetary"}),
    ("So you wouldn't want a 300i?", {"300i": "Origin Jumpworks"}),
    ("Is the Hornet any good?", {"Hornet": "Anvil Aerospace"}),
    ("two hornets and a gladius", {"Hornet": "Anvil Aerospace", "Gladius": "Aegis Dynamics"}),
])
def test_ships_named_in_a_sentence_come_back_with_the_right_maker(sentence, expect):
    assert {s["said"]: s["maker"] for s in sm.ships_in(sentence)} == expect


@pytest.mark.parametrize("sentence", ["what a storm, it cost me a fortune", "the cat sat on the mat", "I love this ship",
                                      "my spirit is broken", "that was a real hurricane of a day", "the titan of industry"])
def test_ordinary_words_are_not_ships(sentence):
    assert sm.ships_in(sentence) == []


def test_a_word_that_is_also_english_counts_only_with_its_capital():
    assert [s["maker"] for s in sm.ships_in("I bought a Spirit")] == ["Crusader Industries"]
    assert sm.ships_in("i bought a spirit") == []
    assert sm.ships_in("Spirit is what I lack") == []                # the first word of a sentence always has one


def test_the_ship_the_suit_knows_resolves_however_the_log_names_it():
    assert sm.maker_of("Aegis Avenger Titan") == "Aegis Dynamics"
    assert sm.maker_of("Drake Cutlass Blue") == "Drake Interplanetary"
    assert sm.maker_of("@vehicle_NameDRAK_Golem_OX") == "Drake Interplanetary"
    assert sm.maker_of("") is None and sm.maker_of("a thing nobody makes") is None


def test_every_nickname_points_at_a_ship_in_the_list_and_never_at_a_maker():
    makers = {e["name"] for e in mf.load()}
    for nick, target in sm.NICKNAMES.items():
        assert target not in makers and sm.maker_of(target), nick


def test_the_gate_refuses_a_maker_the_turn_did_not_supply():
    words = sm.maker_words()
    assert cc.maker_problems("It is a Drake.", "I love this ship. ship.maker=Aegis Dynamics", words) == ["a maker that was not supplied ['Drake']"]
    assert cc.maker_problems("An Aegis. Good.", "I love this ship. ship.maker=Aegis Dynamics", words) == []
    assert cc.maker_problems("Drake built it.", "My mate flies a Cutlass. ship.maker[Cutlass]=Drake Interplanetary", words) == []
    assert cc.maker_problems("The origin of the fault is unknown.", "", words) == []          # the word, not the maker
    assert cc.maker_problems("Origin. All polish.", "", words) != []


@pytest.mark.parametrize("line, asked, talk", [
    ("Is the hangar heated?", True, False), ("What time does the shop open?", True, False),
    ("Who's on comms?", True, False), ("anyone following us", True, False),
    ("What was he flying, could you tell?", True, False),            # the companion as a witness, not as a subject
    ("Do you know what he flies?", True, False), ("Did you see that?", True, False),
    ("Do you ever get bored?", True, True), ("What are you, really?", True, True), ("What would you do?", True, True),
    ("Should I buy a bigger ship?", True, True), ("Am I a bad pilot?", True, True), ("Is that good?", True, True),
    ("That's nothing, isn't it?", True, True), ("Hey. Miss me?", True, True),
    ("My hands are freezing.", False, False), ("I lost a fight I should have won.", False, False),
    ("Good to hear your voice.", False, True),
])
def test_what_counts_as_a_question_and_what_counts_as_talk(line, asked, talk):
    assert cc.is_question(line) is asked and cc.is_talk_question(line) is talk


def test_the_flipped_default_says_i_do_not_know_only_to_a_bare_question_about_the_world():
    assert cc.default_is_unknown("Is the train running?")
    assert not cc.default_is_unknown("Is the train running?", facts=["transit.running=True"])        # there is a fact
    assert not cc.default_is_unknown("Who's getting married, did I say?", memory=[("pilot", "today", "My brother")])
    assert not cc.default_is_unknown("I'm starving.")                                                 # a remark
    assert not cc.default_is_unknown("Do you get lonely when I'm gone?")                              # about the companion
