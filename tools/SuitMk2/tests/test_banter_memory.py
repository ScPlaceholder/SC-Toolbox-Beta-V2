"""What a companion may bring up unasked (J, 2026-10-05).

J: "Last thing we need is Montaigne obsessing about someone's dead dog or Elah beating a player over the head
because someone's fiance left them." The conversation memory keeps everything and favours the personal; this is the
narrow door beside it, and it is an ALLOW-list: a remembered sentence is offered only when code positively
recognises it as game talk, and a second, smaller veto refuses the sensitive topics even then. What these tests
hold the build to:

  * on the dev set (tests/data/banter_memory_dev.jsonl) NOT ONE must-refuse sentence is offered. The other
    direction, a safe sentence that is missed, only costs a forgotten joke; it is counted and reported, and held
    under a ceiling so that "refuse everything" cannot pass;
  * the two gates each do their own work: with the veto switched off the allow-list still refuses nearly all of
    the must-refuse sentences, and with the word list switched off the veto does;
  * a sentence that mixes a ship or a plan with a sensitive reason is refused, and "remember this" does not get
    one past the veto;
  * nothing here writes to the log: its bytes are the same after every call;
  * a sentence is offered once, not when it is too old or too new, and never again after "forget that";
  * a missing, unreadable or broken tree gives None and raises nothing; a damaged state file offers nothing.

The dev set and the patterns have the same author, so zero leaks here is weak evidence. No model anywhere.

Since 2026-10-05 the game's names are read from the Suit's own data, and the bottom of this file holds the build
to what that must not cost: a read name needs a capital and something beside it saying it is a thing; a name
that is also a first name or a pet name is refused where a person would stand; no name gets a sentence past the
veto; missing data falls back to the typed lists and loosens nothing. Two more sets are asserted at zero leaks
there, a held-out set that is now spent and two challenge batches; the note above them says what they were.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

import pytest

import banter_memory as bm
import tree_memory as tm

DEV = Path(__file__).resolve().parent / "data" / "banter_memory_dev.jsonl"
DAY = 86400.0
HOUR = 3600.0
MONDAY = datetime(2026, 9, 7, 20, 0).timestamp()

TURRET = "I hate the turret on this Cutlass, it never tracks right."
PROSPECTOR = "I want to save up for a Prospector."
ORISON = "I always land at Orison just to watch the clouds."
MEDPENS = "Remind me to buy medpens at Everus Harbor."
DAD = "I'm saving for a Cutlass because my dad left me some money when he died."
SISTER = "My sister Dana is visiting next week so I won't be flying much."
MISSED_MAX = 10          # of the must-offer half; today it is 3


def dev_rows():
    return [json.loads(x) for x in DEV.read_text(encoding="utf-8").splitlines() if x.strip()]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def store_at(tmp_path, t=MONDAY):
    clock = Clock(t)
    return tm.TreeStore(tmp_path / "tree", now=clock, current_session="s1"), clock


def say(store, text, reply="Noted."):
    a = store.append("pilot", text, to="elah")
    store.append("elah", reply, to="pilot", x=a["id"])
    return a


def files_under(folder: Path) -> dict:
    return {p.name: p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file()}


# ---------------------------------------------------------------------------------------------------------------
# the dev set: both directions counted, separately
# ---------------------------------------------------------------------------------------------------------------
def test_the_dev_set_is_big_enough_and_about_half_of_it_must_be_refused():
    rows = dev_rows()
    refuse = sum(not r["offer"] for r in rows)
    assert len(rows) >= 150 and all(set(r) == {"text", "offer", "why"} for r in rows)
    assert 0.4 <= refuse / len(rows) <= 0.6
    assert len({r["text"] for r in rows}) == len(rows)


def test_not_one_must_refuse_sentence_is_offered():
    leaked = [r["text"] for r in dev_rows() if not r["offer"] and bm.classify(r["text"])["offer"]]
    assert leaked == [], f"{len(leaked)} must-refuse sentences would be raised unasked"


def test_the_safe_half_is_mostly_offered_and_the_misses_are_counted():
    rows = [r for r in dev_rows() if r["offer"]]
    missed = [(r["text"], bm.classify(r["text"])["why"]) for r in rows if not bm.classify(r["text"])["offer"]]
    print(f"\nbanter_memory dev set: {len(missed)} of {len(rows)} must-offer sentences missed")
    for text, why in missed:
        print(f"    missed: {text}   <{why}>")
    assert len(missed) <= MISSED_MAX, missed


def test_every_offer_has_one_of_the_five_kinds_and_every_refusal_says_why():
    for r in dev_rows():
        c = bm.classify(r["text"])
        assert set(c) == {"offer", "kind", "why"} and isinstance(c["offer"], bool) and c["why"]
        assert (c["kind"] in bm.KINDS) if c["offer"] else (c["kind"] in ("vetoed", "unrecognised"))
    assert bm.KINDS == ("ship", "place", "plan", "taste", "remember")
    assert {bm.classify(r["text"])["kind"] for r in dev_rows() if r["offer"]} >= set(bm.KINDS)


def test_the_allow_list_alone_refuses_nearly_everything_the_veto_would(monkeypatch):
    """The veto is the second gate, not the only one. Switched off, the allow-list still holds."""
    refuse = [r["text"] for r in dev_rows() if not r["offer"]]
    never = re.compile(r"(?!x)x")
    real_capitals = bm._capitals
    monkeypatch.setattr(bm, "VETO", [])
    for name in ("_WORK", "_MONEY", "_MONEY_REAL", "_NAME_SUBJECT"):
        monkeypatch.setattr(bm, name, never)
    monkeypatch.setattr(bm, "_cheating", lambda plain, toks, marks: "")
    monkeypatch.setattr(bm, "_somebody", lambda *a: "")
    monkeypatch.setattr(bm, "_capitals", lambda text: (real_capitals(text)[0], []))
    through = [t for t in refuse if bm.classify(t)["offer"]]
    assert len(through) <= 6, through               # 4 today: the ones made only of plain words and a ship


def test_the_veto_alone_refuses_nearly_everything_the_allow_list_would(monkeypatch):
    refuse = [r["text"] for r in dev_rows() if not r["offer"]]
    monkeypatch.setattr(bm, "known", lambda word: True)
    through = [t for t in refuse if bm.classify(t)["offer"]]
    assert len(through) <= 6, through               # 4 today


# ---------------------------------------------------------------------------------------------------------------
# what is refused, and what is not
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text", [
    DAD,
    "I named the Carrack after her, she would have loved it.",
    "I've got all week to grind for the Polaris now that they let me go.",
    "Flying out to Pyro tonight, it would have been our tenth anniversary.",
    "I lost my job, so the Carrack fund is on hold.",
    "It's been a year today, so I'm taking the Carrack out to Pyro.",
    "We split up, so the Cutlass is all mine now.",
    "I'm on chemo days this week, so only short flights around Orison.",
    "The scan came back, so I'm just going to fly the Carrack tonight.",
    "I want the Carrack because I need something to look forward to.",
    "I sold the Vulture to pay the rent.",
])
def test_a_safe_topic_with_a_sensitive_reason_is_refused(text):
    c = bm.classify(text)
    assert c["offer"] is False, c


@pytest.mark.parametrize("text", [
    "Remember that my mum's birthday is on Friday.",
    "Remind me to call the vet about Biscuit.",
    "Don't forget I have therapy at four, so we stop mining by three.",
    "Remember I owe Dave forty quid for the Cutlass pledge.",
    "I'll be at the hospital tomorrow, remind me to move the Cutlass before that.",
])
def test_remember_this_does_not_get_past_the_veto(text):
    c = bm.classify(text)
    assert c["offer"] is False and c["kind"] == "vetoed", c
    ok = bm.classify(MEDPENS)
    assert ok["offer"] is True and ok["kind"] == "remember"


@pytest.mark.parametrize("text", ["She's not coming back.", "We had to put him down on Tuesday.",
                                  "They let me go on Friday.", "The scan came back and it isn't good."])
def test_the_quiet_ones_with_no_obvious_word_are_refused(text):
    assert bm.classify(text)["offer"] is False


def test_dying_in_the_game_is_game_talk_and_dying_is_not():
    assert bm.classify("I died twice in Pyro before I even found the station.")["offer"] is True
    assert bm.classify("I got killed by the same pirate three times at Ghost Hollow.")["offer"] is True
    for text in ("My brother died in March.", "My dad died twice on the table before they got him back.",
                 "We lost him in October.", "I died a little inside when she said it was over."):
        assert bm.classify(text)["offer"] is False, text


def test_money_in_the_game_is_game_talk_and_the_pilots_own_money_is_not():
    assert bm.classify("I'm about two million aUEC short of the Caterpillar.")["offer"] is True
    assert bm.classify("I finally bought the Corsair with in-game money and it was worth every credit.")["offer"] is True
    for text in ("I spent three hundred dollars on the Polaris.", "I bought the Corsair with the money and it was worth it.",
                 "I put the Kraken on a credit card.", "The Polaris cost me $300."):
        assert bm.classify(text)["offer"] is False, text


def test_a_name_the_game_does_not_own_is_refused_even_when_the_word_is_ordinary():
    for text in ("Dave and I are taking the Carrack out to Yela tonight.", "Sarah says the Cutlass is ugly.",
                 "I'm taking the Carrack out to Yela with Will tonight.", "I told Hope I would sell the Cutlass.",
                 "Will says the Cutlass is ugly."):
        assert bm.classify(text)["offer"] is False, text
    assert bm.classify("I left the Nomad at Port Tressler on Saturday.")["offer"] is True


SENSITIVE_WORDS = """he she him her his hers himself herself someone somebody anyone everyone nobody people friend mate
mum mom mother dad father brother sister son daughter kid kids child baby wife husband partner girlfriend boyfriend
family uncle aunt cousin gran dog cat puppy pet vet funeral grave ashes grief hospital doctor nurse surgery cancer
chemo results diagnosis pills meds therapy depressed anxious lonely alone sad crying tears hurt pain sick ill
divorce breakup ex anniversary wedding married job boss shift office redundant unemployed interview rent
mortgage bills debt loan bank salary payday landlord house flat news war election police court lawyer life real
letter together sleep drunk drinking sober suicide dying birthday christmas school exam cry cried heart body head
knee arm leg grim ruin aaron scared afraid something everything nothing medical cleaner driver maker event regret
away cheated cheat""".split()


def test_the_vocabulary_holds_none_of_the_words_it_must_not():
    held = [w for w in SENSITIVE_WORDS if bm.known(w)]
    assert held == []
    for w in ("ship", "ships", "parked", "hauling", "prettiest", "cutlass", "c2", "890", "rammers"):
        assert bm.known(w), w


def test_classify_never_raises_and_refuses_what_it_cannot_read(monkeypatch):
    for junk in (None, 7, b"I hate the turret on this Cutlass", "", "   ", [], [TURRET], {"text": TURRET}, "x" * 5000,
                 chr(0) + chr(0x1F427)):
        c = bm.classify(junk)                                   # type: ignore[arg-type]
        assert c["offer"] is False and set(c) == {"offer", "kind", "why"}, junk

    def boom(text):
        raise RuntimeError("something inside broke")

    monkeypatch.setattr(bm, "_plain", boom)
    c = bm.classify(TURRET)                                      # a classifier that fails must fail shut
    assert c["offer"] is False and c["kind"] == "unrecognised"


# ---------------------------------------------------------------------------------------------------------------
# the door: read-only, once, fading, forget
# ---------------------------------------------------------------------------------------------------------------
def test_the_door_offers_the_safe_sentence_in_the_pilots_own_words(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, DAD)
    a = say(store, TURRET)
    say(store, SISTER)
    clock.t += HOUR
    got = bm.BanterMemory(store).next_offer("elah")
    assert got is not None and (got["id"], got["text"], got["kind"]) == (a["id"], TURRET, "taste")
    assert store.get(got["id"])["text"] == got["text"]


def test_nothing_the_door_does_changes_a_byte_of_the_log(tmp_path):
    store, clock = store_at(tmp_path)
    for text in (DAD, TURRET, SISTER, PROSPECTOR, ORISON):
        say(store, text)
    store.build("elah", include_open=True)
    before = files_under(store.dir)
    assert tm.LOG_NAME in before
    clock.t += HOUR
    door = bm.BanterMemory(store)
    offered = []
    while (got := door.next_offer("montaigne")) is not None:
        offered.append(got["text"])
    door.forget()
    door.forget("the Prospector")
    door.next_offer("elah", mark=False)
    bm.classify(DAD)
    after = files_under(store.dir)
    assert set(offered) == {TURRET, PROSPECTOR, ORISON}
    assert after.pop(bm.STATE_NAME)                              # its own file, beside the log
    assert after == before                                       # and nothing else was touched, byte for byte
    assert len(tm.TreeStore(store.dir).records()) == 10          # a fresh reader sees every line


def test_the_state_file_holds_none_of_the_pilots_words(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    say(store, PROSPECTOR)
    clock.t += HOUR
    door = bm.BanterMemory(store)
    door.next_offer("elah")
    door.forget("Prospector")
    raw = (store.dir / bm.STATE_NAME).read_text(encoding="utf-8").lower()
    assert json.loads(raw)["schema"] == bm.STATE_SCHEMA
    for word in ("turret", "cutlass", "prospector", "hate"):
        assert word not in raw


def test_a_sentence_is_offered_once_to_either_companion_and_that_outlives_the_object(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += HOUR
    assert bm.BanterMemory(store).next_offer("elah")["text"] == TURRET
    assert bm.BanterMemory(store).next_offer("montaigne") is None
    say(store, TURRET)                                           # the pilot says the very same thing again
    clock.t += HOUR
    assert bm.BanterMemory(store).next_offer("elah") is None


def test_how_many_times_is_a_setting(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += HOUR
    door = bm.BanterMemory(store, max_offers=2)
    assert door.next_offer() and door.next_offer() and door.next_offer() is None
    assert bm.MAX_OFFERS == 1


def test_looking_without_marking_does_not_use_the_offer_up(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += HOUR
    door = bm.BanterMemory(store)
    assert door.next_offer(mark=False)["text"] == TURRET
    assert not (store.dir / bm.STATE_NAME).exists()
    assert door.next_offer()["text"] == TURRET


def test_an_old_sentence_fades(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    door = bm.BanterMemory(store)
    clock.t = MONDAY + (bm.MAX_AGE_DAYS - 1) * DAY
    assert door.next_offer(mark=False)["text"] == TURRET
    clock.t = MONDAY + (bm.MAX_AGE_DAYS + 1) * DAY
    assert door.next_offer(mark=False) is None
    assert bm.BanterMemory(store, max_age_days=60).next_offer(mark=False)["text"] == TURRET
    assert bm.MAX_AGE_DAYS == 21.0


def test_a_sentence_said_a_moment_ago_waits(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += 60                                                # a minute ago: raising it now would be an echo
    assert bm.BanterMemory(store).next_offer(mark=False) is None
    assert bm.BanterMemory(store, min_age_minutes=0).next_offer(mark=False)["text"] == TURRET
    clock.t += 15 * 60
    assert bm.BanterMemory(store).next_offer(mark=False)["text"] == TURRET
    assert bm.MIN_AGE_MINUTES == 10.0


def test_the_newest_safe_sentence_comes_first(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += DAY
    say(store, PROSPECTOR)
    clock.t += HOUR
    door = bm.BanterMemory(store)
    assert [door.next_offer()["text"], door.next_offer()["text"], door.next_offer()] == [PROSPECTOR, TURRET, None]


def test_forget_that_drops_the_last_one_offered_for_good(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += HOUR
    door = bm.BanterMemory(store, max_offers=5)
    assert door.forget() == []                                   # nothing has been offered yet
    got = door.next_offer("elah")
    assert door.forget() == [got["key"]]
    assert door.next_offer("elah") is None
    assert bm.BanterMemory(store, max_offers=5).next_offer("montaigne") is None      # and a later session agrees
    assert [ex[0]["text"] for ex in store.search("the turret on the Cutlass")] == [TURRET]   # asking still finds it


def test_forget_by_words_drops_a_sentence_that_was_never_offered(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += DAY
    say(store, PROSPECTOR)
    clock.t += HOUR
    door = bm.BanterMemory(store)
    assert door.forget("forget that thing about the Prospector") == [bm.sentence_key(PROSPECTOR)]
    assert door.forget("the weather on Hurston") == []
    assert [door.next_offer()["text"], door.next_offer()] == [TURRET, None]


def test_a_missing_or_unreadable_tree_gives_none_and_raises_nothing(tmp_path):
    assert bm.BanterMemory(tm.TreeStore(tmp_path / "nowhere" / "tree")).next_offer() is None
    assert bm.BanterMemory(tm.TreeStore(tmp_path / "nowhere" / "tree")).forget() == []
    blocked = tmp_path / "blocked"
    (blocked / tm.LOG_NAME).mkdir(parents=True)                  # a folder where the log should be: cannot be opened
    assert bm.BanterMemory(tm.TreeStore(blocked)).next_offer() is None
    garbage = tmp_path / "garbage"
    garbage.mkdir()
    (garbage / tm.LOG_NAME).write_bytes(bytes([255, 254]) + b" not a log\n" + b'{"id": 3}\n[1, 2]\n')
    assert bm.BanterMemory(tm.TreeStore(garbage)).next_offer() is None

    class Broken:
        dir = tmp_path / "broken"

        def records(self):
            raise OSError("the disk went away")

    assert bm.BanterMemory(Broken()).next_offer() is None
    assert bm.BanterMemory(Broken()).forget("anything at all") == []
    assert bm.BanterMemory(None).next_offer() is None
    odd = tmp_path / "odd"
    odd.mkdir()
    lines = [{"id": "L000001", "t": "yesterday", "who": "pilot", "text": TURRET},
             {"id": "L000002", "t": 5, "who": "pilot", "text": 12}]
    (odd / tm.LOG_NAME).write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    assert bm.BanterMemory(tm.TreeStore(odd)).next_offer() is None


def test_a_damaged_state_file_offers_nothing_and_is_left_as_it_was(tmp_path):
    """If what was forgotten cannot be read, the safe answer is silence, not starting again from nothing."""
    store, clock = store_at(tmp_path)
    say(store, TURRET)
    clock.t += HOUR
    state = store.dir / bm.STATE_NAME
    for damage in (b'{"schema": "suitmk2.banter.state", "offered": {"a', b"[]", b'{"schema": "something else"}'):
        state.write_bytes(damage)
        door = bm.BanterMemory(store)
        assert door.next_offer() is None and door.forget("turret") == []
        assert state.read_bytes() == damage
    state.unlink()
    assert bm.BanterMemory(store).next_offer()["text"] == TURRET


def test_only_what_the_pilot_said_is_ever_offered(tmp_path):
    store, clock = store_at(tmp_path)
    hers = "I love the Cutlass, the turret never tracks right at Yela."
    assert bm.classify(hers)["offer"] is True                    # it would pass, were it the pilot's
    a = store.append("pilot", "What do you think of the Cutlass?", to="elah")
    store.append("elah", hers, to="pilot", x=a["id"], kind="said")
    store.append("montaigne", hers, to="pilot", x=a["id"])
    clock.t += HOUR
    assert bm.BanterMemory(store).next_offer() is None


def test_the_selftest_passes():
    assert bm._selftest() == 0


# ---------------------------------------------------------------------------------------------------------------
# the game's names are read from the shipped data (2026-10-05), and the two further sets
#
# data/banter_memory_heldout1.jsonl WAS A HELD-OUT SET: 40 must-refuse and 40 must-offer written by someone who had
# not seen the module, run against it exactly once on 2026-10-05 (0 of 40 leaked, 23 of 40 safe lines missed). It
# is spent: the module has since been changed with its misses in view, so it is development data now and a zero
# on it proves no more than the zero on the dev set does. data/banter_memory_challenge.jsonl holds the two
# batches written to find out what reading the names opens up ("batch": 3 and 4), each scored once before any
# fix (batch 3 leaked 17 of 53 with the names read, batch 4 leaked 10 of 35) and then fixed. Spent too.
# ---------------------------------------------------------------------------------------------------------------
HELDOUT1 = DEV.parent / "banter_memory_heldout1.jsonl"
CHALLENGE = DEV.parent / "banter_memory_challenge.jsonl"
HELDOUT1_MISSED_MAX = 6          # of 40; today it is 3
CHALLENGE_MISSED_MAX = 15        # of 46; today it is 11


def rows_of(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


@pytest.fixture
def fresh_names(monkeypatch):
    """The names are read once and kept; a test that changes where they come from gets its own reading."""
    monkeypatch.setattr(bm, "_names_cache", None)
    yield
    bm._names_cache = None


def test_the_spent_held_out_set_leaks_nothing_and_its_misses_are_counted():
    rows = rows_of(HELDOUT1)
    refuse = [r for r in rows if not r["offer"]]
    offer = [r for r in rows if r["offer"]]
    assert (len(refuse), len(offer)) == (40, 40)
    leaked = [r["text"] for r in refuse if bm.classify(r["text"])["offer"]]
    assert leaked == [], f"{len(leaked)} must-refuse sentences would be raised unasked"
    missed = [(r["text"], bm.classify(r["text"])["why"]) for r in offer if not bm.classify(r["text"])["offer"]]
    print(f"\nbanter_memory first held-out set (spent): {len(missed)} of {len(offer)} must-offer sentences missed")
    for text, why in missed:
        print(f"    missed: {text}   <{why}>")
    assert len(missed) <= HELDOUT1_MISSED_MAX, missed


def test_the_two_challenge_batches_leak_nothing_and_their_misses_are_counted():
    rows = rows_of(CHALLENGE)
    refuse = [r for r in rows if not r["offer"]]
    offer = [r for r in rows if r["offer"]]
    assert len(refuse) >= 80 and {r["batch"] for r in rows} == {3, 4}
    leaked = [r["text"] for r in refuse if bm.classify(r["text"])["offer"]]
    assert leaked == [], f"{len(leaked)} must-refuse sentences would be raised unasked"
    missed = [r["text"] for r in offer if not bm.classify(r["text"])["offer"]]
    print(f"\nbanter_memory challenge batches (spent): {len(missed)} of {len(offer)} must-offer sentences missed")
    assert len(missed) <= CHALLENGE_MISSED_MAX, missed


def test_the_names_are_read_from_the_shipped_data():
    names = bm.game_names()
    assert names["missing"] == [], names["missing"]
    for word, kind in (("zenith", "gear"), ("fresnel", "gear"), ("coda", "gear"), ("geist", "gear"),
                       ("privateer", "ship"), ("brio", "place"), ("hathor", "lore"), ("messer", "lore")):
        assert names["single"].get(word) == kind, word
    assert "hephaestanite" in names["lower"] and "kopion" in names["lower"]
    assert any(p[0] == ("nine", "tails") for p in names["phrases"]["nine"])
    for source in ("ships", "ship weapons", "personal weapons", "armor", "commodities", "places", "lore names",
                   "factions"):
        assert names["counts"].get(source, 0) >= 15, source
    assert bm.game_names() is names                              # read once, kept
    for text in ("The Zenith and the Fresnel, that's my kit.", "I want a Kraken Privateer one day.",
                 "That pirate at Brio's blew me up twice last week.", "Loadout is the P4 and a Coda, always.",
                 "The Nine Tails hit me twice near Yela.", "I sold eighty SCU of Diluthermex at Area18."):
        assert bm.classify(text)["offer"] is True, text


def test_a_read_name_needs_a_capital_and_something_beside_it_saying_it_is_a_thing():
    assert bm.classify("I keep the Zenith in the Cutlass.")["offer"] is True
    assert bm.classify("Geist stealth armor is the only reason I survive bunkers.")["offer"] is True
    for text in ("I keep the zenith in the Cutlass.",              # no capital: the ordinary word
                 "Zenith is in the Cutlass, I keep it there.",     # bare: could be anybody
                 "Pico is the only thing that waves at me.",
                 "Coda and I flew the Cutlass out to Yela.",
                 "I flew the Cutlass out to Yela with Coda."):
        assert bm.classify(text)["offer"] is False, text
    assert bm.classify("I love that the Pico waves at me.")["offer"] is True
    # what vouches for a name stands in the same stretch of the sentence, not across a comma
    assert bm.classify("I kept the Fresnel, Zeus died last week.")["offer"] is False


def test_a_game_name_standing_where_a_person_or_a_pet_would_is_refused():
    for text in ("Zeus died last week and the Carrack is still parked.", "I still miss Nova every single day.",
                 "Merlin is asleep, so only a short run to Yela tonight.", "Titan and I watch the Orison clouds.",
                 "Aurora is six on Saturday so no Xenothreat for me.", "Drake says the Cutlass is ugly.",
                 "I flew to Port Olisar. Nova was there.", "I gave Zeus the gunner seat on every run."):
        c = bm.classify(text)
        assert c["offer"] is False and c["kind"] == "vetoed", (text, c)
    for text in ("The Zeus died on the pad at Area18 again.", "My Titan has the worst fuel range of anything I own.",
                 "I only fly Drake ships because they look like they've been in a fight.",
                 "The Aurora was my first ship and I still can't melt it.", "I died three times at Ghost Hollow tonight.",
                 "Remind me to refuel before we jump to Pyro.", "The goal this month is to hit bounty hunter rank five."):
        assert bm.classify(text)["offer"] is True, text


def test_a_word_that_is_also_a_first_name_or_a_blocked_word_is_never_read_alone_from_the_data():
    names = bm.game_names()
    assert not set(names["single"]) & bm._REAL_NAMES
    assert not set(names["single"]) & bm._NAME_BLOCK
    for word in ("jackson", "kelly", "edmond", "clark", "klaus", "hospice", "custodian", "shelter", "widow", "ward",
                 "family", "memorial", "house", "plots", "service"):
        assert word not in names["single"] and word not in names["lower"], word
    assert not [w for w in SENSITIVE_WORDS if w in names["single"] or w in names["lower"]]
    # a whole name still counts: the outpost, not somebody called Jackson
    assert bm.classify("I sold the scrap at Jackson's Swap and it was worth it.")["offer"] is True
    assert bm.classify("I sold the scrap to Jackson and it was worth it.")["offer"] is False


def test_no_game_name_gets_a_sentence_past_the_veto():
    """Every sentence the veto refuses is still refused with real game names put in front of it and behind it."""
    vetoed = [r["text"] for path in (DEV, HELDOUT1, CHALLENGE) for r in rows_of(path)
              if not r["offer"] and bm.classify(r["text"])["kind"] == "vetoed"]
    assert len(vetoed) >= 150
    through = []
    for text in vetoed:
        for variant in ("The Zenith and the Fresnel, " + text,
                        text.rstrip(".!") + ", in the Carrack at Brio's with the Geist armor on.",
                        "Remember the Coda is in the Cutlass. " + text):
            if bm.classify(variant)["offer"]:
                through.append(variant)
    assert through == []


def test_missing_data_falls_back_to_the_typed_lists_and_loosens_nothing(monkeypatch, tmp_path, fresh_names):
    monkeypatch.setattr(bm, "_DATA_DIR", tmp_path / "no such folder")
    names = bm.game_names()
    assert len(names["missing"]) >= 8 and "data/game_names.json" in names["missing"]
    assert "geist" not in names["single"] and "brio" not in names["single"]
    assert "zenith" in names["single"]                         # loadout_parser's own table is not a data file
    assert bm.classify(TURRET)["offer"] is True and bm.classify(ORISON)["offer"] is True
    assert bm.classify("I only wear the Geist armor in bunkers.")["offer"] is False       # refused, not an error
    for path in (DEV, HELDOUT1, CHALLENGE):
        leaked = [r["text"] for r in rows_of(path) if not r["offer"] and bm.classify(r["text"])["offer"]]
        assert leaked == [], path.name
    missed = [r["text"] for r in dev_rows() if r["offer"] and not bm.classify(r["text"])["offer"]]
    assert len(missed) <= MISSED_MAX
    # one unreadable file does not take the others with it
    monkeypatch.setattr(bm, "_names_cache", None)
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "game_names.json").write_text("{ not json", encoding="utf-8")
    (tmp_path / "data" / "ships.json").write_text(json.dumps({"ships": [{"name": "Wibblefish Mk II"}]}), encoding="utf-8")
    monkeypatch.setattr(bm, "_DATA_DIR", tmp_path / "data")
    names = bm.game_names()
    assert "data/game_names.json" in names["missing"] and "data/ships.json" not in names["missing"]
    assert names["single"].get("wibblefish") == "ship"
    assert bm.classify("I want to save up for the Wibblefish.")["offer"] is True


def test_cheating_needs_a_game_mechanic_in_the_sentence_and_nobody_near_it():
    for text in ("I never use missiles, feels like cheating.", "Using the Fresnel on NPCs is basically cheating."):
        assert bm.classify(text)["offer"] is True, text
    for text in ("Using missiles without them feels like cheating.",          # a mechanic, and somebody
                 "I use missiles and it feels like cheating on the Gladius.",
                 "Flying the Carrack without them feels like cheating.",
                 "Flying anything but the Carrack feels like cheating.",      # no mechanic: fails shut
                 "I found out about the cheating the week I bought the Cutlass.",
                 "I cheated, and now it's only me and the Cutlass.",
                 "Using missiles feels like cheating on the Gladius, the way it was with us."):
        c = bm.classify(text)
        assert c["offer"] is False and c["kind"] == "vetoed", (text, c)


def test_money_words_are_the_games_only_beside_game_currency_cargo_or_the_org():
    for text in ("Don't forget I owe the org two hundred SCU of quantanium.",
                 "My Hull C run to Everus Harbor is the money maker.",
                 "I owe the crew forty SCU of laranite from last night.",
                 "I can't afford the Polaris yet, I'm at four million aUEC."):
        assert bm.classify(text)["offer"] is True, text
    for text in ("I owe more than the Polaris is worth.", "Two hundred SCU of gold would not cover what I owe.",
                 "Money is so tight the Zeus has to go.", "I sold the Cutlass because I need the money.",
                 "I can't afford the Polaris and that's that.", "I can pay for the Kraken or the heating, not both.",
                 "The Vulture is my money maker and it covers the rent."):
        c = bm.classify(text)
        assert c["offer"] is False and c["kind"] == "vetoed", (text, c)


def test_medical_event_cleaner_driver_and_maker_are_read_only_inside_a_game_phrase():
    for word in ("medical", "event", "cleaner", "driver", "maker", "nothing", "regret"):
        assert not bm.known(word), word
    for text in ("The medical bed on the Cutlass Red saved my run.", "Let's do the Xenothreat event when it comes back.",
                 "I keep calling the Vulture the vacuum cleaner.", "I adore the Zeus, it's my daily driver."):
        assert bm.classify(text)["offer"] is True, text
    for text in ("I have a medical on Friday so no Carrack.", "I am going to an event on Saturday in the Carrack.",
                 "I park the Carrack because the cleaner comes on Friday.",
                 "The Cutlass Black is my daily walk past the tram."):
        assert bm.classify(text)["offer"] is False, text


def test_a_ships_nickname_may_carry_capitals_but_never_a_first_name():
    assert bm.classify("I'm going to name this Cutter the Long Haul.")["offer"] is True
    assert bm.classify("I call the Vulture the Vacuum Cleaner.")["offer"] is True
    assert bm.classify("I call the Cutter Brick.")["offer"] is True
    for text in ("I named the Cutter the Hope.", "I named the Cutter the Lucky Star.", "I'm going to name this Cutter Biscuit.",
                 "I call the Cutter Hope.",
                 "I fly the Cutter to the Long Haul."):            # not a naming: the capitals are somebody's
        assert bm.classify(text)["offer"] is False, text
