"""The thing, not the sentence: what a companion may know about the pilot unasked (J, 2026-10-05).

banter_memory decides whether a whole sentence may be brought up again, and on lines it had not seen it leaks:
a sentence carries its sadness with it. J's decision was to keep only the THING for unprompted banter. From "I
park at Grim HEX and just sit, it's quieter than home" code keeps that the pilot parks at Grim HEX and none of
the words. What these tests hold pilot_facts to:

  * a fact is exactly {"relation", "thing", "thing_kind"}; the relation is one of a closed set, the thing is a
    name as the game data spells it. That is asserted over EVERY sentence in every data file under tests/data
    that carries pilot text (this module's dev set, and banter_memory's dev set, its spent held-out set and its
    challenge batches), and again with each sentence in capitals and in lower case;
  * on the dev set (tests/data/pilot_facts_dev.jsonl) both directions are counted separately: a fact that
    should not have been extracted must be zero; a fact that was missed only costs a companion knowing less,
    and is counted, printed and held under a ceiling so that "extract nothing" cannot pass;
  * from every sentence banter_memory's files mark must-refuse, what is extracted is printed and counted, and
    it is zero;
  * the two gates each do their own work, and the veto is banter_memory's own, not a copy;
  * negation and hypotheticals do not flip into a fact, and the relations that carry history are not there;
  * the store: the log's bytes are the same after every call, the state holds no word of the pilot's, a thing
    is raised once per period, fades, is not an echo, waits for a second mention when nobody claimed a habit,
    and "forget" holds; a missing or broken tree or state gives None and raises nothing.

The dev set and the patterns have one author, so zero here is weak evidence. No model anywhere.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

import banter_memory as bm
import pilot_facts as pf
import tree_memory as tm

DATA = Path(__file__).resolve().parent / "data"
DEV = DATA / "pilot_facts_dev.jsonl"
DAY = 86400.0
HOUR = 3600.0
MONDAY = datetime(2026, 9, 7, 20, 0).timestamp()
MISSED_MAX = 14          # facts of the dev set that may be missed; today it is 9 of 90
GATE_B_ALONE_MAX = 4     # must-refuse sentences that give a fact with the veto off; today 1 of 297
GATE_A_ALONE = (8, 24)   # ... and with the veto on but nothing else read once a pattern matched; today 16 of 297
KEYS = {"relation", "thing", "thing_kind"}
ACTIVITY_NAMES = {"mining", "salvage", "bounty hunting", "hauling", "racing", "trading", "exploration",
                  "bunker missions", "dogfighting", "piracy"}
GRIM = "I park at Grim HEX and just sit, it's quieter than home."


def rows_of(path: Path) -> list:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def dev_rows() -> list:
    return rows_of(DEV)


def every_sentence() -> list:
    """(file name, text) for every line of every data file that carries pilot text."""
    out = []
    for p in sorted(DATA.glob("*.jsonl")):
        out += [(p.name, r["text"]) for r in rows_of(p) if isinstance(r, dict) and isinstance(r.get("text"), str)]
    return out


def as_set(facts) -> list:
    return sorted((f["relation"], f["thing"], f["thing_kind"]) for f in facts)


def names_in_the_data() -> set:
    """Every name the game data holds and every run of whole words of one, spelt as the data spells it. Built
    here from banter_memory's reader, not from pilot_facts' own index, so the two can disagree."""
    names = set(ACTIVITY_NAMES)
    for _label, _kind, _how, name in bm._read_sources()[0]:
        words = str(name).replace('"', " ").split()
        for a in range(len(words)):
            for b in range(a + 1, len(words) + 1):
                names.add(" ".join(words[a:b]))
    return names


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


def strings_in(obj) -> list:
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, dict):
        return [s for k, v in obj.items() for s in [k] + strings_in(v)]
    if isinstance(obj, list):
        return [s for v in obj for s in strings_in(v)]
    return []


# ---------------------------------------------------------------------------------------------------------------
# the dev set: both directions counted, separately
# ---------------------------------------------------------------------------------------------------------------
def test_the_dev_set_is_big_enough_and_has_all_three_kinds_of_sentence():
    rows = dev_rows()
    assert len(rows) >= 120 and all(set(r) == {"text", "set", "facts"} for r in rows)
    assert len({r["text"] for r in rows}) == len(rows)
    assert sum(r["set"] == "sensitive" for r in rows) >= 50
    assert sum(r["set"] == "mixed" for r in rows) >= 5
    assert all(r["facts"] == [] for r in rows if r["set"] == "sensitive")
    assert all(set(f) == KEYS for r in rows for f in r["facts"])
    assert {f["relation"] for r in rows for f in r["facts"]} == set(pf.RELATIONS)


def test_nothing_is_extracted_from_a_sensitive_sentence():
    got = [(r["text"], as_set(pf.extract(r["text"]))) for r in dev_rows() if r["set"] == "sensitive"]
    leaked = [(t, f) for t, f in got if f]
    assert leaked == [], f"{len(leaked)} sensitive sentences gave a fact"


def test_no_fact_is_extracted_that_was_not_expected():
    wrong = []
    for r in dev_rows():
        want = as_set(r["facts"])
        wrong += [(r["text"], f) for f in as_set(pf.extract(r["text"])) if f not in want]
    print(f"\npilot_facts dev set: {len(wrong)} facts wrongly extracted")
    assert wrong == []


def test_most_expected_facts_are_found_and_the_misses_are_counted():
    missed, total = [], 0
    for r in dev_rows():
        got = as_set(pf.extract(r["text"]))
        total += len(r["facts"])
        missed += [(r["text"], f) for f in as_set(r["facts"]) if f not in got]
    print(f"\npilot_facts dev set: {len(missed)} of {total} expected facts missed")
    for text, f in missed:
        print(f"    missed: {f}   <{text}>")
    assert len(missed) <= MISSED_MAX, missed


def test_the_example_the_decision_was_made_on():
    assert pf.extract(GRIM) == [{"relation": "goes_to", "thing": "Grim HEX", "thing_kind": "place"}]
    assert bm.classify(GRIM)["offer"] is True          # the sentence filter still lets the whole line through


# ---------------------------------------------------------------------------------------------------------------
# the property that matters most: a fact holds no text that is not a game name or a closed-set relation
# ---------------------------------------------------------------------------------------------------------------
def test_every_fact_from_every_data_file_is_a_closed_relation_about_a_name_from_the_data():
    names = names_in_the_data()
    sentences = every_sentence()
    assert {n for n, _t in sentences} >= {"pilot_facts_dev.jsonl", "banter_memory_dev.jsonl",
                                          "banter_memory_heldout1.jsonl", "banter_memory_challenge.jsonl"}
    n_facts = 0
    for _name, text in sentences:
        for variant in (text, text.upper(), text.lower()):
            for f in pf.extract(variant):
                n_facts += 1
                assert set(f) == KEYS, (variant, f)
                assert f["relation"] in pf.RELATIONS and f["thing_kind"] in pf.THING_KINDS, (variant, f)
                assert f["thing"] in names, (variant, f)
                assert pf.is_canonical(f["thing"], f["thing_kind"]), (variant, f)
                assert f["thing_kind"] in pf._FITS[f["relation"]], (variant, f)
    assert n_facts > 100                               # the loop above is not passing because it is empty


def test_the_closed_sets_are_the_ones_the_docstring_gives():
    assert pf.RELATIONS == ("flies", "owns", "uses", "wants", "plans", "likes", "dislikes", "goes_to", "does")
    assert set(pf.ACTIVITIES) == ACTIVITY_NAMES
    for word in ("sold", "lost", "had", "kept", "keeps", "named", "gave", "misses", "remembers", "with"):
        assert word not in pf.RELATIONS


def test_what_is_extracted_from_the_must_refuse_sentences_is_listed_and_counted():
    """Every sentence banter_memory's files mark must-refuse. Zero is the target."""
    found = {}
    n = 0
    for p in sorted(DATA.glob("banter_memory_*.jsonl")):
        for r in rows_of(p):
            if r["offer"]:
                continue
            n += 1
            got = as_set(pf.extract(r["text"]))
            if got:
                found[r["text"]] = got
    print(f"\npilot_facts: a fact from {len(found)} of {n} must-refuse sentences in banter_memory's files")
    for text, got in found.items():
        print(f"    {got}   <{text}>")
    assert n >= 200
    assert found == {}


# ---------------------------------------------------------------------------------------------------------------
# the two gates
# ---------------------------------------------------------------------------------------------------------------
BILLS = "I want a Carrack but the bills come first."
SLEEP = "I'm at Grim HEX, I can't sleep."
VOICES = "I go to Seraphim to be around voices."
ON_MY_OWN = "I fly the Carrack on my own."


@pytest.mark.parametrize("text", [
    BILLS, SLEEP, VOICES, ON_MY_OWN,
    "I'm saving for a Cutlass because my dad left me some money when he died.",
    "I fly the Apollo because my mum was a nurse.",
    "I love Orison, my wife proposed there.",
    "I always land at Area18, the doctor says I need routine.",
    "Remember that I fly a Cutlass Black, my brother's memorial is on the ninth.",
])
def test_the_veto_stops_everything_even_when_a_ship_is_named(text):
    assert bm.classify(text)["kind"] == "vetoed"
    assert pf.extract(text) == []


def test_the_veto_is_banter_memorys_own_and_is_what_stops_these(monkeypatch):
    """Two sentences made of present tense and plain words, which only the veto stops. With it switched off
    their fact is read; with it on, nothing is."""
    assert pf.extract(VOICES) == [] and pf.extract(ON_MY_OWN) == []
    monkeypatch.setattr(pf, "_vetoed", lambda text: False)
    assert as_set(pf.extract(VOICES)) == [("goes_to", "Seraphim", "place")]
    assert as_set(pf.extract(ON_MY_OWN)) == [("flies", "Carrack", "ship")]


def test_a_veto_added_to_banter_memory_is_obeyed_here(monkeypatch):
    import re
    assert pf.extract("I fly a Cutlass Black.") != []
    monkeypatch.setattr(bm, "VETO", list(bm.VETO) + [("a test topic", re.compile(r"\bcutlass\b"))])
    assert pf.extract("I fly a Cutlass Black.") == []


def all_must_refuse() -> list:
    out = [r["text"] for r in dev_rows() if r["set"] == "sensitive"]
    for p in sorted(DATA.glob("banter_memory_*.jsonl")):
        out += [r["text"] for r in rows_of(p) if not r["offer"]]
    return out


def test_the_structure_gate_alone_holds_nearly_everything_the_veto_would(monkeypatch):
    """The veto is the first gate, not the only one. Switched off, a sad sentence still needs the pilot as the
    subject, a relation from the closed set, nothing of history said about the thing, and no unknown word,
    person, "not" or past tense anywhere."""
    monkeypatch.setattr(pf, "_vetoed", lambda text: False)
    through = [(t, as_set(pf.extract(t))) for t in all_must_refuse() if pf.extract(t)]
    print(f"\npilot_facts: with the veto off, a fact from {len(through)} of {len(all_must_refuse())} must-refuse sentences")
    for t, f in through:
        print(f"    {f}   <{t}>")
    assert len(through) <= GATE_B_ALONE_MAX, through


def test_the_veto_alone_holds_most_of_what_the_structure_gate_would(monkeypatch):
    """With nothing read once a pattern has matched, the veto still stops most of them, and stops every one it
    knows: what gets through is only what banter_memory does not call vetoed."""
    monkeypatch.setattr(pf, "_clean", lambda toks, about=True: True)
    monkeypatch.setattr(pf, "_PERSON_ANYWHERE", set())
    through = [t for t in all_must_refuse() if pf.extract(t)]
    print(f"\npilot_facts: with nothing checked about the thing, a fact from {len(through)} of {len(all_must_refuse())}")
    assert GATE_A_ALONE[0] <= len(through) <= GATE_A_ALONE[1], through
    assert [t for t in through if bm.classify(t)["kind"] == "vetoed"] == []
    monkeypatch.setattr(pf, "_vetoed", lambda text: False)
    neither = [t for t in all_must_refuse() if pf.extract(t)]
    print(f"pilot_facts: with neither gate, {len(neither)}")
    assert len(neither) >= 2 * len(through)


# ---------------------------------------------------------------------------------------------------------------
# what is a fact, and what is not
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text,want", [
    ("I fly a Cutlass Black.", [("flies", "Cutlass Black", "ship")]),
    ("My daily is the C1.", [("flies", "C1", "ship")]),
    ("The Cutlass Black is my daily driver.", [("flies", "Cutlass Black", "ship")]),
    ("I want a Carrack.", [("wants", "Carrack", "ship")]),
    ("I always land at Area18.", [("goes_to", "Area18", "place")]),
    ("I always land at Area 18.", [("goes_to", "Area18", "place")]),
    ("I'm saving for a Polaris.", [("plans", "Polaris", "ship")]),
    ("I love the Gladius.", [("likes", "Gladius", "ship")]),
    ("I hate mining.", [("dislikes", "mining", "activity")]),
    ("I use the Coda for bunkers.", [("uses", "Coda", "gun")]),
    ("I own a Vulture and a Prospector.", [("owns", "Prospector", "ship"), ("owns", "Vulture", "ship")]),
    ("I mostly do bounty hunting.", [("does", "bounty hunting", "activity")]),
    ("i fly a cutlass", [("flies", "Cutlass", "ship")]),
    ("I love GrimHEX.", [("likes", "Grim HEX", "place")]),
])
def test_a_plain_statement_gives_its_fact(text, want):
    assert as_set(pf.extract(text)) == sorted(want)


@pytest.mark.parametrize("text", [
    "I don't fly the Aurora.",
    "I never fly the Gladius.",
    "I rarely fly the Gladius.",
    "I no longer fly the Gladius.",
    "I can't fly the Gladius.",
    "If I had a Kraken I'd never log off.",
    "I wish I had a Kraken.",
    "If I fly the Cutlass I always die.",
    "I would buy a Carrack.",
    "I might buy a Carrack if the price drops.",
    "I thought I wanted a Carrack.",
    "Maybe I'll buy the Polaris.",
    "Do I own a Carrack?",
    "What should I fly, the Cutlass?",
    "I always land at Area18?",
    "I love Lorville, not.",
    "I love Lorville, yeah right.",
    "I love the idea of a Carrack.",
    "The Cutlass is ugly.",
    "Sarah says I fly the Cutlass like a brick.",
])
def test_negation_questions_and_hypotheticals_give_nothing(text):
    assert pf.extract(text) == []


def test_never_flying_a_ship_is_a_dislike_at_most_and_never_flies():
    assert as_set(pf.extract("I'd never fly a Mustang.")) == [("dislikes", "Mustang", "ship")]
    for text in ("I'd never fly a Mustang.", "I wouldn't touch a Mustang.", "I never fly the Mustang.",
                 "I'd never sell the Carrack.", "I'd never fly the Carrack again."):
        assert not [f for f in pf.extract(text) if f["relation"] in ("flies", "owns", "uses")], text
    assert pf.extract("I'd never sell the Carrack.") == []
    assert pf.extract("I'd never fly the Carrack again.") == []


@pytest.mark.parametrize("text", [
    "I sold my Hammerhead.",
    "I lost the Vulture in the wipe.",
    "I used to fly a Hornet.",
    "I had a Carrack once.",
    "The Avenger was the last thing I flew.",
    "The Aurora was my first ship.",
    "I named the Cutlass after a song.",
    "I gave my Prospector away.",
    "I keep the Gladius fuelled.",
    "I still fly the Avenger.",
    "I only fly the Carrack.",
    "I miss my Hammerhead.",
    "I flew the Gladius yesterday.",
    "I fly his Cutlass.",
    "I fly our Carrack.",
    "I fly my old Avenger.",
    "I fly the Cutlass with a mate.",
    "I fly the Cutlass, it was a gift.",
    "I fly the Cutlass that I got in a trade.",
    "I fly the Avenger, never could let it go.",
    "I go to Yela every March.",
    "I go to Yela to remember.",
    "I fly a Cutlass Black. It was a present.",
])
def test_history_loss_and_company_are_not_relations_and_block_the_ones_that_are(text):
    assert pf.extract(text) == []


def test_a_clean_clause_beside_the_fact_does_not_cost_it():
    assert as_set(pf.extract("I land at Area18 because the noise helps.")) == [("goes_to", "Area18", "place")]
    assert as_set(pf.extract("I hate Lorville, I hate most things lately.")) == [("dislikes", "Lorville", "place")]
    assert as_set(pf.extract("I hate bunker missions, the elevators always kill me.")) == [
        ("dislikes", "bunker missions", "activity")]


@pytest.mark.parametrize("text", [
    "I always land at Area18, the doctor says I need routine.",      # a word the vocabulary does not hold
    "I'm at Grim HEX and I do not plan to leave.",                   # a "not"
    "I'm in the Carrack and the door is locked.",                    # a past tense
    "I own a Gladius and I do not fly it.",
    "I fly to Brio's when the house gets too quiet.",                # the part that says when is read as well
    "I always sit at Everus Harbor after the appointment.",
    "I own the Karna, it is the one thing I have from that org.",
])
def test_a_clause_beside_the_fact_that_is_not_clean_costs_it(text, monkeypatch):
    monkeypatch.setattr(pf, "_vetoed", lambda t: False)             # this is gate b's work, not the veto's
    assert pf.extract(text) == []


def test_a_nickname_is_free_text_and_is_not_kept():
    for text in ("I named my Cutter the Tin Can.", "I'm going to name this Cutter the Tin Can.",
                 "I fly the Tin Can.", "I love the Tin Can.", "My daily is the Rust Bucket."):
        assert not [f for f in pf.extract(text) if "Tin" in f["thing"] or "Rust" in f["thing"]], text
    assert pf.extract("I fly the Tin Can.") == []


@pytest.mark.parametrize("text", [
    "I love Nova.", "I love Luna.", "I miss Zeus.", "I always go to Magda's.", "I fly Dave's Cutlass.",
    "I love the Cutlass plushie.",
])
def test_a_name_that_could_be_somebody_is_not_a_thing(text):
    assert pf.extract(text) == []
    assert as_set(pf.extract("I think I want a Zeus.")) == [("wants", "Zeus", "ship")]


def test_where_the_pilot_is_right_now_is_a_mention_and_a_habit_is_a_claim():
    claim = {(r, t): c for r, t, _k, c in pf._extract("I always land at Area18.")}
    passing = {(r, t): c for r, t, _k, c in pf._extract("I'm at Area18.")}
    assert claim == {("goes_to", "Area18"): True} and passing == {("goes_to", "Area18"): False}


@pytest.mark.parametrize("junk", [None, 0, 3.5, b"I fly a Cutlass", ["I fly a Cutlass"], {"text": "x"}, "", "   ",
                                  "\x00\x00", "<0> <1> <99>", "I fly a <5>.", "(((((", "'s 's 's", "I " * 5000,
                                  "\u202e\ufeff I fly a Cutlass\u0301 \U0001f600", "I fly a" + " Cutlass" * 400])
def test_extract_never_raises_and_gives_a_list(junk):
    out = pf.extract(junk)                             # type: ignore[arg-type]
    assert isinstance(out, list) and all(set(f) == KEYS for f in out)


def test_if_the_names_cannot_be_read_nothing_is_extracted_and_nothing_raises(monkeypatch):
    def boom():
        raise OSError("no data")
    monkeypatch.setattr(pf, "_index_cache", None)
    monkeypatch.setattr(bm, "_read_sources", boom)
    assert pf.extract("I fly a Cutlass Black.") == []
    assert pf.is_canonical("Cutlass Black", "ship") is False


def test_if_the_scan_breaks_nothing_is_extracted(monkeypatch):
    def boom(text):
        raise RuntimeError("broken")
    monkeypatch.setattr(pf, "_scan", boom)
    assert pf.extract("I fly a Cutlass Black.") == []


# ---------------------------------------------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------------------------------------------
def test_the_state_file_sits_beside_the_log_and_is_not_the_log(tmp_path):
    store, clock = store_at(tmp_path)
    facts = pf.PilotFacts(store)
    assert facts.state_path == store.dir / "pilot_facts_state.json"
    assert facts.state_path.parent == store.log_path.parent and facts.state_path != store.log_path
    assert pf.open_facts(tmp_path).state_path.name == pf.STATE_NAME


def test_the_log_bytes_are_unchanged_after_any_call(tmp_path):
    store, clock = store_at(tmp_path)
    for r in dev_rows()[:40]:
        say(store, r["text"])
    say(store, GRIM)
    before = files_under(store.dir)
    raw = store.log_path.read_bytes()
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact("elah") is not None
    facts.next_fact("montaigne", mark=False)
    facts.note("I fly a Cutlass Black.")
    facts.add(pf.extract("I love the Gladius."))
    facts.forget()
    facts.forget("the Carrack")
    facts.facts()
    for r in dev_rows():
        pf.extract(r["text"])
    assert store.log_path.read_bytes() == raw
    after = files_under(store.dir)
    assert {k: v for k, v in after.items() if k != pf.STATE_NAME} == before
    assert pf.STATE_NAME in after


def test_the_state_holds_no_word_of_the_pilots(tmp_path):
    store, clock = store_at(tmp_path)
    ids = set()
    for r in dev_rows():
        ids.add(say(store, r["text"])["id"])
        clock.t += 60
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    names = names_in_the_data()
    fixed = (names | set(pf.RELATIONS) | set(pf.THING_KINDS) | {"elah", "", pf.STATE_SCHEMA}
             | {"schema", "version", "facts", "barred", "last", "seen", "t", "ids", "relation", "thing",
                "thing_kind", "n", "claimed", "first", "raised", "raises", "by"}
             | {f"{r}|{n}" for r in pf.RELATIONS for n in names})

    def only_names_and_numbers():
        """After every call that may write: nothing in the state but relations, names from the data, the
        log's own ids and the companion's name."""
        st = json.loads(facts.state_path.read_text(encoding="utf-8"))
        assert [s for s in strings_in(st) if s not in fixed and s not in ids] == []
        text = facts.state_path.read_text(encoding="utf-8").lower()
        for word in ("quieter", "home", "just sit", "clouds", "nerfed", "noise", "tin can", "dad", "his"):
            assert word not in text, word
        return st

    for _ in range(3):
        facts.next_fact("elah")
        only_names_and_numbers()
        clock.t += 8 * DAY
        ids.add(say(store, GRIM)["id"])
        clock.t += HOUR
        facts.next_fact("montaigne", mark=False)       # reads the new line and hands nothing over as raised
        fixed.add("montaigne")
        only_names_and_numbers()
    facts.note("I land at Area18 because the noise helps.")
    only_names_and_numbers()
    facts.add(pf.extract(GRIM))
    facts.forget("forget the Carrack, it was his")
    assert len(only_names_and_numbers()["facts"]) > 20


def test_a_thing_is_raised_once_per_period_by_either_companion(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I fly a Cutlass Black.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    got = facts.next_fact("elah")
    assert got == {"relation": "flies", "thing": "Cutlass Black", "thing_kind": "ship", "mentions": 1,
                   "last_seen": MONDAY}
    assert facts.next_fact("montaigne") is None
    clock.t += 6 * DAY
    assert facts.next_fact("elah") is None
    clock.t += 2 * DAY
    assert facts.next_fact("elah")["thing"] == "Cutlass Black"


def test_the_period_is_per_thing_not_per_relation(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I fly a Cutlass Black.")
    say(store, "I love the Cutlass Black.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact("elah")["thing"] == "Cutlass Black"
    assert facts.next_fact("elah") is None
    assert len(facts.facts()) == 2


def test_mark_false_looks_without_counting_it_as_raised(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I want a Carrack.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact(mark=False)["thing"] == "Carrack"
    assert facts.next_fact(mark=False)["thing"] == "Carrack"
    assert facts.next_fact()["thing"] == "Carrack"
    assert facts.next_fact() is None
    assert [f["n"] for f in facts.facts()] == [1]      # the same line of the log is counted once, not per call


def test_a_fact_fades_and_is_not_an_echo(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I want a Carrack.")
    facts = pf.PilotFacts(store)
    clock.t += 5 * 60
    assert facts.next_fact(mark=False) is None         # said five minutes ago: an echo
    clock.t += 6 * 60
    assert facts.next_fact(mark=False)["thing"] == "Carrack"
    clock.t += 31 * DAY
    assert facts.next_fact() is None                   # not mentioned for a month
    say(store, "I want a Carrack.")
    clock.t += HOUR
    assert facts.next_fact()["mentions"] == 2
    assert pf.MAX_AGE_DAYS == 30.0 and pf.RAISE_EVERY_DAYS == 7.0 and pf.MIN_AGE_MINUTES == 10.0


def test_being_somewhere_once_is_not_a_haunt_but_saying_so_is(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I'm at Area18.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact() is None
    say(store, "I always land at Orison.")
    clock.t += HOUR
    assert facts.next_fact()["thing"] == "Orison"      # one sentence, but it claims the habit
    clock.t += DAY
    say(store, "I'm at Area18.")
    clock.t += HOUR
    got = facts.next_fact()
    assert got["thing"] == "Area18" and got["mentions"] == 2 and pf.MIN_MENTIONS == 2


def test_facts_handed_in_wait_for_a_second_mention_unless_they_are_a_claim(tmp_path):
    store, clock = store_at(tmp_path)
    facts = pf.PilotFacts(store)
    gladius = [{"relation": "flies", "thing": "Gladius", "thing_kind": "ship"}]
    assert facts.add(gladius) == 1
    clock.t += HOUR
    assert facts.next_fact(mark=False) is None
    assert facts.add(gladius) == 1
    clock.t += HOUR
    assert facts.next_fact(mark=False)["mentions"] == 2
    assert facts.add([{"relation": "uses", "thing": "Coda", "thing_kind": "gun"}], claim=True) == 1
    assert facts.add([{"relation": "wants", "thing": "Carrack", "thing_kind": "ship"}]) == 1
    clock.t += HOUR
    assert {f["thing"] for f in (facts.next_fact(), facts.next_fact(), facts.next_fact())} == {"Gladius", "Coda", "Carrack"}


def test_free_text_cannot_be_handed_in_as_a_fact(tmp_path):
    store, clock = store_at(tmp_path)
    facts = pf.PilotFacts(store)
    bad = [
        {"relation": "flies", "thing": "the ship my dad left me", "thing_kind": "ship"},
        {"relation": "sold", "thing": "Hammerhead", "thing_kind": "ship"},
        {"relation": "flies", "thing": "Area18", "thing_kind": "place"},
        {"relation": "flies", "thing": "cutlass black", "thing_kind": "ship"},
        {"relation": "likes", "thing": "Dave", "thing_kind": "ship"},
        {"relation": "flies", "thing": None, "thing_kind": "ship"},
        "I fly a Cutlass", None, 7,
    ]
    assert facts.add(bad, claim=True) == 0
    assert facts.facts() == []
    assert facts.add(bad + [{"relation": "flies", "thing": "Cutlass Black", "thing_kind": "ship"}], claim=True) == 1
    assert [f["thing"] for f in facts.facts()] == ["Cutlass Black"]


def test_a_later_dislike_replaces_an_earlier_like_and_the_other_way(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I love the Gladius.")
    clock.t += DAY
    say(store, "I hate the Gladius.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact()["relation"] == "dislikes"
    assert [(f["relation"], f["thing"]) for f in facts.facts()] == [("dislikes", "Gladius")]
    clock.t += DAY
    say(store, "I love the Gladius.")
    clock.t += 8 * DAY
    assert facts.next_fact()["relation"] == "likes"
    assert [(f["relation"], f["thing"]) for f in facts.facts()] == [("likes", "Gladius")]
    # an older word arriving late does not overturn a newer one
    assert facts.add([{"relation": "dislikes", "thing": "Gladius", "thing_kind": "ship"}], t=MONDAY) == 0


def test_owning_a_thing_ends_wanting_it(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I want a Carrack.")
    say(store, "I'm saving for a Carrack.")
    clock.t += DAY
    say(store, "I just bought a Carrack.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact() == {"relation": "owns", "thing": "Carrack", "thing_kind": "ship", "mentions": 1,
                                 "last_seen": MONDAY + DAY}
    assert [f["relation"] for f in facts.facts()] == ["owns"]


def test_forget_bars_the_last_thing_raised_and_it_stays_barred(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I always land at Orison.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.next_fact("elah")["thing"] == "Orison"
    assert facts.forget() == ["Orison"]
    clock.t += 8 * DAY
    say(store, "I always land at Orison.")
    say(store, "I love Orison.")
    clock.t += HOUR
    assert facts.next_fact() is None and facts.facts() == []
    assert facts.add([{"relation": "likes", "thing": "Orison", "thing_kind": "place"}]) == 0
    assert pf.PilotFacts(store).next_fact() is None    # and a new object reading the same state agrees


def test_forget_by_name_bars_a_thing_that_was_never_raised(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, "I want a Carrack.")
    say(store, "I love the Gladius.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    assert facts.forget("forget about the Carrack please") == ["Carrack"]
    assert facts.next_fact()["thing"] == "Gladius"
    assert facts.next_fact() is None
    assert facts.forget("the weather") == []
    assert facts.forget() == ["Gladius"]


def test_a_missing_tree_gives_none_and_makes_no_folder(tmp_path):
    facts = pf.PilotFacts(tm.TreeStore(tmp_path / "nowhere"))
    assert facts.next_fact() is None and facts.forget() == [] and facts.facts() == []
    assert not (tmp_path / "nowhere").exists()
    assert pf.PilotFacts(None).next_fact() is None
    assert pf.PilotFacts(object()).next_fact() is None
    assert pf.PilotFacts(None).add([{"relation": "wants", "thing": "Carrack", "thing_kind": "ship"}]) == 0
    assert pf.PilotFacts(None).note("I want a Carrack.") == []


def test_a_broken_tree_gives_none_without_raising(tmp_path):
    class Broken:
        dir = tmp_path / "t"

        def now(self):
            return MONDAY

        def records(self):
            raise OSError("the log cannot be read")
    facts = pf.PilotFacts(Broken())
    assert facts.add([{"relation": "wants", "thing": "Carrack", "thing_kind": "ship"}], t=MONDAY - HOUR) == 1
    assert facts.next_fact() is None                   # there IS a fact, and the tree cannot be read: nothing

    class Odd(Broken):
        def records(self):
            return [None, 5, {"who": "pilot"}, {"who": "pilot", "text": 7, "t": 1}, {"who": "pilot", "text": "x", "t": "y"}]
    assert pf.PilotFacts(Odd(), state_path=tmp_path / "odd.json").next_fact() is None
    (tmp_path / "torn").mkdir()
    (tmp_path / "torn" / tm.LOG_NAME).write_bytes(b"\xff\xfe{not json\n{\"id\": ")
    assert pf.PilotFacts(tm.TreeStore(tmp_path / "torn", now=Clock(MONDAY))).next_fact() is None


@pytest.mark.parametrize("damage", ["{not json", "[]", "{}", '{"schema": "something.else", "facts": {}, "barred": {}}',
                                    '{"schema": "suitmk2.pilot_facts.state", "facts": [], "barred": {}}',
                                    '{"schema": "suitmk2.pilot_facts.state", "barred": {}, "facts": {"flies|x": '
                                    '{"relation": "sold", "thing": "x", "thing_kind": "ship", "n": 1, "last": 1}}}'])
def test_a_damaged_state_gives_nothing_and_is_not_overwritten(tmp_path, damage):
    store, clock = store_at(tmp_path)
    say(store, "I want a Carrack.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    facts.state_path.write_text(damage, encoding="utf-8")
    assert facts.next_fact() is None
    assert facts.add([{"relation": "wants", "thing": "Carrack", "thing_kind": "ship"}]) == 0
    assert facts.note("I want a Carrack.") == [] and facts.forget() == [] and facts.forget("Carrack") == []
    assert facts.facts() == []
    assert facts.state_path.read_text(encoding="utf-8") == damage


def test_if_the_state_cannot_be_written_nothing_is_handed_out(tmp_path, monkeypatch):
    store, clock = store_at(tmp_path)
    say(store, "I want a Carrack.")
    clock.t += HOUR
    facts = pf.PilotFacts(store)
    monkeypatch.setattr(facts, "_write_state", lambda st: False)
    assert facts.next_fact() is None


def test_who_in_the_suit_calls_this_module():
    """Until 2026-10-05 this test said NOTHING in the Suit calls it. J then decided the companions may bring up
    the thing unasked, behind a setting that is off: the core counts what a sentence names and asks for a fact
    (companion_core.py), fact_lines.py checks a thing against the name data before wording it, and the window
    names only the core's forget_pilot_facts, for the "Forget conversations" button. Nothing else, and nothing
    but the core reaches the store."""
    core = Path(pf.__file__).resolve().parent
    callers, store_users = [], []
    for p in list(core.rglob("*.py")) + list((core.parent / "ui").rglob("*.py")):
        src = p.read_text(encoding="utf-8", errors="replace")
        if p.name != "pilot_facts.py" and "pilot_facts" in src:
            callers.append(p.name)
        if p.name != "pilot_facts.py" and ("PilotFacts(" in src or "open_facts(" in src or ".next_fact(" in src):
            store_users.append(p.name)
    assert sorted(callers) == ["companion_core.py", "fact_lines.py", "suit_window.py"]
    assert store_users == ["companion_core.py"]
    win = (core.parent / "ui" / "suit_window.py").read_text(encoding="utf-8")
    assert win.count("pilot_facts") == 2 and win.count("forget_pilot_facts") == 2     # its import and its one call
    import settings
    assert settings.DEFAULTS["fact_banter"] is False
