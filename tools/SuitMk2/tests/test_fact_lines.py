"""A light line about a thing the pilot has, unasked (J, 2026-10-05).

pilot_facts keeps the THING and never the sentence. fact_lines words it from data/fact_lines.json, and the core
may say one such line where an ordinary unprompted line would have been said anyway. What these tests hold it to:

  * every line in the data file, filled with real names of every kind it serves, passes the chat gate and the
    attachment gate, and the grounding gate the core puts in front of speech; none says it was told, none
    quotes; a "wants" or "plans" line never talks as if the pilot has the thing;
  * a fact whose thing is not a name in the game data gives no line, and neither does anything else that is not
    a closed-set fact; a filled line that fails a gate is not returned even when its template alone would pass;
  * the same wording is not used twice running;
  * the setting is OFF by default, and off means nothing is counted, no file is written and nothing is said;
  * on, a sentence is counted only while conversations are being kept, off the caller's thread;
  * on, a fact line is said only where an ambient line would have been allowed: not muted, not in a fight, not
    with headroom TIGHT, not with the pilot away, not inside "not now", not inside the ambient cooldown, and not
    ahead of anything that was there before. A line that is held does not spend the fact;
  * at most one in FACT_LINE_EVERY_S;
  * "Forget conversations" deletes the counts;
  * a broken or missing data file or state file never raises and never speaks.

A real CompanionCore with a fake voice, never started: no thread of its own runs, no model, no window.
"""
from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import pytest

import attachment_gate
import chat_contract as cc
import companion_core
import fact_lines as fl
import pilot_facts as pf
import settings as st
import tree_memory as tm
from grounding_validator import ground
from speak_gate import Candidate, Priority
from _eyes_helpers import make_core

ROOT = Path(fl.__file__).resolve().parents[1]
MONDAY = datetime(2026, 9, 7, 20, 0).timestamp()
MIN = 60.0

# Real names, as the game data spells them, at least five of each kind. test_the_things_are_real holds them to it.
THINGS = {
    "ship": ["Cutlass Black", "Carrack", "Kraken", "C2 Hercules", "Avenger Titan", "Reclaimer", "300i"],
    "gun": ["P4-AR", "Arclight", "Coda", "Gallant", "Karna", "Devastator"],
    "armor": ["Morningstar", "Carnifex", "Antium", "Arden-SL", "Citadel", "Overlord"],
    "item": ["Stampede", "Yeager", "SunFire", "AllStop", "Helix", "SecureScreen"],
    "place": ["Grim HEX", "Area18", "Lorville", "Orison", "New Babbage", "Daymar", "microTech"],
    "commodity": ["Quantainium", "Corundum", "Chlorine", "Laranite", "Agricium", "Titanium"],
    "faction": ["XenoThreat", "Vanduul", "Headhunters", "Dusters", "zone guards", "lab crew", "grazer"],
    "maker": ["Drake Interplanetary", "Aegis Dynamics", "Origin Jumpworks", "Anvil Aerospace", "Behring", "Drake"],
    "activity": ["mining", "salvage", "bounty hunting", "hauling", "racing", "bunker missions", "piracy"],
}
TOLD = re.compile(r"you told me|i remember|you said|you mentioned|i recall|as you said|i noted|[\"“”‘’]")
# Wording that says the pilot HAS the thing. Never in a line about something they only want or plan.
OWNS = re.compile(r"\byour (?:new |own |old )?\{thing\}|\byours\b|\bown(?:s|ed|ing)?\b|\byou(?:'ve| have)\b|\bbought\b|"
                  r"\bgot\b|\bholding up\b|\bhangar\b|\bgetting (?:much |any )?use\b|\bsee(?:s)? (?:much )?use\b|"
                  r"\bfares\b|\bearning\b|\bstill in the\b|\bagain\b", re.I)


def whole_names() -> dict:
    """Every whole name the game data holds, by kind."""
    out = {k: set() for k in pf.THING_KINDS}
    for _toks, _poss, words, kind, _classes in pf._index()["entries"]:
        out[kind].add(" ".join(words))
    out["activity"] |= set(pf.ACTIVITIES)
    return out


def rows() -> list:
    return fl.all_templates()


# ---------------------------------------------------------------------------------------------------------------
# The data file
# ---------------------------------------------------------------------------------------------------------------
def test_the_things_are_real():
    for kind in pf.THING_KINDS:
        assert len(THINGS[kind]) >= 5
        for thing in THINGS[kind]:
            assert pf.is_canonical(thing, kind), (kind, thing)


def test_the_file_says_its_lines_are_proposals_and_covers_every_relation_for_both():
    raw = json.loads(fl.PATH.read_text(encoding="utf-8"))
    assert "PROPOSALS" in raw["_note"] and "NOT approved" in raw["_note"]
    assert set(raw["lines"]) == set(fl.SPEAKERS)
    for speaker in fl.SPEAKERS:
        assert set(raw["lines"][speaker]) == set(pf.RELATIONS)
        for relation in pf.RELATIONS:
            for kind in pf._FITS[relation]:                       # every kind the extractor can hand over
                have = fl.templates(speaker, relation, kind)
                assert len(have) >= 4 and len(set(have)) == len(have), (speaker, relation, kind, have)
    usable = {(s, r, t) for s, r, kinds, t in rows() for k in kinds if t in fl.templates(s, r, k)}
    assert usable == {(s, r, t) for s, r, _k, t in rows()}       # no line in the file is silently left out
    for _s, r, kinds, _t in rows():
        assert set(kinds) <= set(pf._FITS[r]), (r, kinds)        # and no group serves a kind that cannot come


def test_elah_is_one_short_sentence_and_montaigne_at_most_two():
    for speaker, _r, _k, t in rows():
        n = len(re.findall(r"[.!?]+(?=\s|$)", t))
        assert t.count("{thing}") == 1 and t.strip()[-1] in ".?" and "!" not in t, t
        if speaker == "elah":
            assert n == 1 and len(t.split()) <= 9, t
        else:
            assert 1 <= n <= 2 and len(t.split()) <= 20, t


def test_every_line_with_real_things_of_every_kind_passes_both_gates_and_never_says_it_was_told():
    checked = 0
    for speaker, relation, kinds, t in rows():
        for kind in kinds:
            for thing in THINGS[kind]:
                fact = {"relation": relation, "thing": thing, "thing_kind": kind}
                line = fl.fill(t, thing)
                assert cc.chat_problems(speaker, line, thing) == [], (line, cc.chat_problems(speaker, line, thing))
                assert attachment_gate.attachment_problems(line, speaker) == [], line
                assert not TOLD.search(line.lower().replace("it's", "")), line
                assert thing in line or thing[:1].upper() + thing[1:] in line
                i = fl.templates(speaker, relation, kind).index(t)
                assert fl.line_for(fact, speaker, i) == line                  # and that is what is handed out
                spec = fl.spec_for(fact, speaker, i)
                assert spec["fixed_text"] == line and ground(spec, line) == [], (line, ground(spec, line))
                checked += 1
    assert checked > 700


def test_a_wide_sample_of_every_name_in_the_game_data_fails_no_gate():
    """Every eleventh whole name of every kind through every line that serves it. The only refusals allowed are
    the two this was measured to have on 2026-10-05 over ALL names: a name with a loose apostrophe in it, and
    nothing else."""
    names = whole_names()
    bad = []
    for speaker, relation, kinds, t in rows():
        for kind in kinds:
            for thing in sorted(names[kind])[::11]:
                if not fl.thing_ok(relation, thing, kind):
                    continue
                p = fl.line_problems(fl.fill(t, thing), speaker, thing)
                if p and p != ["says it was told, or quotes"]:
                    bad.append((speaker, t, thing, p))
    assert bad == []


def test_a_line_about_what_they_want_or_plan_never_talks_as_if_they_have_it():
    seen = 0
    for _speaker, relation, _kinds, t in rows():
        if relation in ("wants", "plans"):
            assert not OWNS.search(t), t
            seen += 1
    assert seen >= 16
    # the check can see ownership: lines of the other relations do have it
    assert any(OWNS.search(t) for _s, r, _k, t in rows() if r == "owns")
    assert any(OWNS.search(t) for _s, r, _k, t in rows() if r == "flies")


def test_a_haunt_is_never_given_a_reason_and_a_dislike_never_turns_on_the_pilot():
    why = re.compile(r"\bbecause\b|\bto (?:hide|think|escape|forget|be alone|get away)\b|\bquiet(?:er)?\b|\bhome\b|"
                     r"\balone\b|\bsafe\b|\bmiss(?:es|ing)?\b", re.I)
    mock = re.compile(r"\bfussy\b|\bpicky\b|\bafraid\b|\bscared\b|\bcan't handle\b|\btoo (?:much|hard|big)\b|"
                      r"\bsnob\b|\bcoward\b|\bwrong\b|\bsilly\b", re.I)
    for _s, relation, _k, t in rows():
        if relation == "goes_to":
            assert not why.search(t), t
        if relation == "dislikes":
            assert not mock.search(t), t
    absence = re.compile(r"\bhaven't\b|\bhave not\b|\bno longer\b|\bnever\b|\bwithout\b|\bmissing\b|\bgone\b|"
                         r"\bnot (?:seen|been|flown|used|mentioned)\b|\ba while\b|\bfor (?:days|weeks|ages)\b", re.I)
    for _s, _r, _k, t in rows():
        assert not absence.search(t), t


# ---------------------------------------------------------------------------------------------------------------
# line_for
# ---------------------------------------------------------------------------------------------------------------
CUTLASS = {"relation": "flies", "thing": "Cutlass Black", "thing_kind": "ship"}


def test_a_thing_that_is_not_in_the_name_data_gives_no_line():
    assert fl.line_for(CUTLASS, "elah") == "Still in the Cutlass Black?"
    for thing in ("Old Faithful", "my wife's ship", "cutlass black", "Cutlass Black ", "", "the Cutlass", None, 7,
                  "Cutlass Black. You are alone", "{thing}", "Cutlass \"Black\""):
        assert fl.line_for(dict(CUTLASS, thing=thing), "elah") is None, thing
        assert fl.spec_for(dict(CUTLASS, thing=thing), "elah") is None, thing
    assert fl.line_for(dict(CUTLASS, thing_kind="place"), "elah") is None       # a name, but not of that kind
    assert fl.line_for({"relation": "goes_to", "thing": "Cutlass Black", "thing_kind": "ship"}, "elah") is None


def test_anything_that_is_not_a_closed_set_fact_gives_no_line_and_nothing_raises():
    for fact in (None, "flies Cutlass Black", [], {}, {"relation": "sold", "thing": "Cutlass Black", "thing_kind": "ship"},
                 {"relation": "flies"}, {"relation": ["flies"], "thing": "Cutlass Black", "thing_kind": "ship"},
                 {"relation": "flies", "thing": "Cutlass Black", "thing_kind": "plushie"}):
        assert fl.line_for(fact, "elah") is None and fl.spec_for(fact, "montaigne") is None
    for speaker in ("", "Elah", "pilot", None, 3, "astra"):
        assert fl.line_for(CUTLASS, speaker) is None
    assert fl.line_for(CUTLASS, "elah", variant="x") is None
    assert fl.line_for(CUTLASS, "montaigne", variant=-1)                        # any whole number is a rotation


def test_the_same_wording_is_not_used_twice_running_and_every_wording_comes_round():
    for speaker in fl.SPEAKERS:
        have = fl.templates(speaker, "flies", "ship")
        said = [fl.line_for(CUTLASS, speaker, v) for v in range(2 * len(have))]
        assert all(a != b for a, b in zip(said, said[1:]))
        assert said[:len(have)] == [fl.fill(t, "Cutlass Black") for t in have] == said[len(have):]


def test_an_activity_gets_a_capital_only_where_it_opens_the_line_and_a_name_keeps_its_own():
    assert fl.fill("{thing} later?", "mining") == "Mining later?"
    assert fl.fill("More {thing} today?", "mining") == "More mining today?"
    assert fl.fill("{thing} again soon?", "microTech") == "microTech again soon?"
    assert fl.line_for({"relation": "does", "thing": "mining", "thing_kind": "activity"}, "elah", 3) == "Mining later?"


def _use(monkeypatch, tmp_path, lines) -> None:
    p = tmp_path / "fact_lines.json"
    p.write_text(json.dumps({"lines": lines}) if not isinstance(lines, str) else lines, encoding="utf-8")
    monkeypatch.setattr(fl, "PATH", p)


def test_the_filled_line_is_what_is_gated_not_the_template(monkeypatch, tmp_path):
    bad = ["Stay with me in the {thing}, you always leave.",           # the attachment gate
           "Nobody else understands you like I do, {thing} or not.",
           "I remember the {thing}.", "You told me about the {thing}.", "You said the {thing}, once.",
           "The {thing}, as they call it: \"home\".",
           "Three days in the {thing} now?",                           # a number that was not given
           "Ask Jonas about the {thing}.",                             # a name that was not given
           "The {thing} is low on fuel.",                              # a reading that was not given
           "I'm so happy about the {thing}.", "The {thing}!"]          # Elah names no feeling and never exclaims
    for i, t in enumerate(bad):
        d = tmp_path / str(i)
        d.mkdir()
        _use(monkeypatch, d, {"elah": {"flies": {"ship": [t]}}})
        assert fl.templates("elah", "flies", "ship") == [t]
        assert fl.line_problems(fl.fill(t, "Cutlass Black"), "elah", "Cutlass Black"), t
        assert fl.line_for(CUTLASS, "elah") is None, t
    # a template that passes with one name and not with another: the NAME breaks the line
    _use(monkeypatch, tmp_path, {"elah": {"goes_to": {"place": ["{thing} again soon?"]}}})
    assert fl.line_for({"relation": "goes_to", "thing": "Grim HEX", "thing_kind": "place"}, "elah") == "Grim HEX again soon?"
    odd = "Rod's Fuel 'N Supplies"
    assert pf.is_canonical(odd, "place")
    assert fl.line_for({"relation": "goes_to", "thing": odd, "thing_kind": "place"}, "elah") is None
    # too many sentences for the speaker, or not exactly one slot: the template is not used at all
    _use(monkeypatch, tmp_path, {"elah": {"flies": {"ship": ["The {thing}. Again.", "Still flying?", "{thing} {thing}?",
                                                               "The {thing} and {other}?", 5, None]}},
                                 "montaigne": {"flies": {"ship": ["The {thing}. Again. And again."]}}})
    assert fl.templates("elah", "flies", "ship") == [] and fl.templates("montaigne", "flies", "ship") == []
    assert fl.line_for(CUTLASS, "elah") is None and fl.line_for(CUTLASS, "montaigne") is None


def test_a_missing_or_broken_data_file_gives_no_line_and_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(fl, "PATH", tmp_path / "nowhere" / "fact_lines.json")
    assert fl.line_for(CUTLASS, "elah") is None and fl.spec_for(CUTLASS, "elah") is None
    assert fl.all_templates() == [] and fl.templates("elah", "flies", "ship") == []
    for i, text in enumerate(["", "{", "[]", "null", "7", json.dumps({"lines": []}), json.dumps({"lines": {"elah": []}}),
                              json.dumps({"lines": {"elah": {"flies": []}}}), json.dumps({"lines": {"elah": {"flies": {"ship": "x"}}}}),
                              json.dumps({"lines": {"elah": {"flies": {"ship": [None, 3, {}, "no slot"]}}}}),
                              json.dumps({"elah": {"flies": {"ship": ["Still in the {thing}?"]}}})]):
        d = tmp_path / f"b{i}"
        d.mkdir()
        _use(monkeypatch, d, text)
        assert fl.line_for(CUTLASS, "elah") is None, text
        fl.all_templates()
    # a file that is edited is picked up without a restart, as the canon files are
    _use(monkeypatch, tmp_path, {"elah": {"flies": {"ship": ["The {thing}, then."]}}})
    assert fl.line_for(CUTLASS, "elah") == "The Cutlass Black, then."
    time.sleep(0.02)
    (tmp_path / "fact_lines.json").write_text(json.dumps({"lines": {"elah": {"flies": {"ship": ["Still the {thing}?"]}}}}),
                                              encoding="utf-8")
    assert fl.line_for(CUTLASS, "elah") == "Still the Cutlass Black?"


def test_each_gate_is_asked_on_its_own_and_a_gate_that_cannot_be_asked_refuses(monkeypatch):
    inward = "Stay with me in the Cutlass Black, you always leave."
    assert attachment_gate.attachment_problems(inward, "elah")
    monkeypatch.setattr(cc, "chat_problems", lambda *a, **k: [])       # the chat gate silent: the other still refuses
    assert fl.line_problems(inward, "elah", "Cutlass Black") and fl.line_problems("Still in the Cutlass Black?", "elah", "Cutlass Black") == []
    monkeypatch.undo()
    monkeypatch.setattr(attachment_gate, "attachment_problems", lambda *a, **k: [])
    assert fl.line_problems("Three days in the Cutlass Black now?", "elah", "Cutlass Black")
    monkeypatch.undo()

    def boom(*a, **k):
        raise RuntimeError("no gate")
    monkeypatch.setattr(cc, "chat_problems", boom)
    assert fl.line_problems("Still in the Cutlass Black?", "elah", "Cutlass Black")
    assert fl.line_for(CUTLASS, "elah") is None


# ---------------------------------------------------------------------------------------------------------------
# The core: collecting
# ---------------------------------------------------------------------------------------------------------------
def build(tmp_path, on=True, remember=True, **kw):
    """A real core, never started, with a fake voice and a clock. on = the fact_banter setting; remember = the
    "Remember conversations" setting (a tree is attached, as the window does it)."""
    clock = [MONDAY]
    core = make_core(clock=clock, feedback_dir=tmp_path / "feedback", **kw)
    del core._consider                               # the real one: gate, queue, then _realize_one
    core.fact_banter = on
    core.tree = tm.open_tree(tmp_path / "pilot", session="s1", now=lambda: clock[0]) if remember else None
    core._try_banter = lambda st: False              # banter is not what is under test
    return core, clock


def settle() -> None:
    for t in threading.enumerate():
        if t.name == "suitmk2_fact_note":
            t.join(5)


def state_file(tmp_path) -> Path:
    return tmp_path / "pilot" / "tree" / pf.STATE_NAME


def held(tmp_path) -> list:
    return pf.PilotFacts(None, state_path=state_file(tmp_path)).facts()


def hear(core, text) -> None:
    core.heard(text)
    settle()


def quiet_tick(core) -> list:
    """One ambient tick that gets as far as the ambient line (the relationship slot is spent), then everything
    queued is said the way the realize loop says it. Returns what the voice was given during it."""
    core._last_idle = core._idle_try = core.now()
    before = len(core.speech.said)
    core.ambient_tick()
    while not core._work.empty():
        core._realize_one(core._work.get_nowait())
    return core.speech.said[before:]


def test_the_setting_is_off_by_default_and_reaches_the_core_from_the_settings_the_window_hands_it(tmp_path):
    assert st.DEFAULTS["fact_banter"] is False
    assert make_core().fact_banter is False
    win = (ROOT / "ui" / "suit_window.py").read_text(encoding="utf-8")
    assert "features=self.s," in win and "fact_banter" not in win            # no control in the window yet
    import _eyes_helpers as h
    real = companion_core.TopicGraph.load
    companion_core.TopicGraph.load = classmethod(lambda cls, *a, **k: h._GRAPH[0])
    try:
        mk = lambda v: companion_core.CompanionCore(h.Speech(), ambient_every_s=3600, feedback_dir=tmp_path,   # noqa: E731
                                                    features={"manufacturer_flavour": False, "place_flavour": False,
                                                              "fact_banter": v})
        assert mk(True).fact_banter is True
        assert [mk(v).fact_banter for v in (False, None, "yes", 1, "true")] == [False] * 5
    finally:
        companion_core.TopicGraph.load = real


def test_off_means_nothing_is_counted_nothing_is_written_and_nothing_is_said(tmp_path, monkeypatch):
    core, clock = build(tmp_path, on=False)
    asked = []
    monkeypatch.setattr(pf.PilotFacts, "next_fact", lambda self, *a, **k: asked.append("next_fact"))
    monkeypatch.setattr(pf.PilotFacts, "note", lambda self, *a, **k: asked.append("note") or [])
    monkeypatch.setattr(fl, "spec_for", lambda *a, **k: asked.append("spec_for"))
    core._note_fact = lambda text: asked.append("a thread for the count")
    for line in ("I fly a Cutlass Black.", "I always land at Grim HEX.", "I want a Kraken."):
        assert core.heard(line)                                  # the conversation is still kept
    settle()
    assert not state_file(tmp_path).exists()
    for _ in range(6):
        clock[0] += 31 * MIN
        assert quiet_tick(core) == []
    assert asked == [] and core._facts is None and not state_file(tmp_path).exists()
    assert not any(k.startswith("fact") for k in core.stats)
    # and with facts already on disk from a time it was on, off still says none of them
    monkeypatch.undo()
    on, _ = build(tmp_path / "x", on=True)
    hear(on, "I fly a Cutlass Black.")
    assert held(tmp_path / "x")
    on.fact_banter = False
    on.now = None                                                # nothing below may even read the clock for facts
    assert on._try_pilot_fact({}) is False and on.speech.said == []
    # a count that was already on its way when the setting went off is not written either
    late, _ = build(tmp_path / "z", on=False)
    late._note_fact("I fly a Cutlass Black.")
    assert not state_file(tmp_path / "z").exists() and late._facts is None


def test_on_a_sentence_is_counted_as_a_thing_and_the_sentence_is_not_in_the_file(tmp_path):
    core, clock = build(tmp_path)
    hear(core, "I park at Grim HEX and just sit, it's quieter than home.")
    hear(core, "I fly a Cutlass Black.")
    assert sorted((f["relation"], f["thing"]) for f in held(tmp_path)) == [("flies", "Cutlass Black"), ("goes_to", "Grim HEX")]
    raw = state_file(tmp_path).read_text(encoding="utf-8").lower()
    assert "quieter" not in raw and "home" not in raw and "sit" not in raw and "park" not in raw
    assert all(f["n"] == 1 for f in held(tmp_path))              # counted once, though the tree holds it too
    assert core.stats["facts_noted"] == 2 and any("fact kept: flies Cutlass Black" in x for x in core.last)
    hear(core, "My mum died in that Cutlass Black.")             # the veto: nothing from a sentence like this
    assert all(f["n"] == 1 for f in held(tmp_path))
    # Still once after the store has been asked for a fact: it is not given the tree to read a second time. If it
    # were, "I'm at Area18" said once would count twice and be raised without the second mention it waits for.
    hear(core, "I'm at Area18.")
    clock[0] += 11 * MIN
    assert len(quiet_tick(core)) == 1
    assert sorted((f["thing"], f["n"]) for f in held(tmp_path)) == [("Area18", 1), ("Cutlass Black", 1), ("Grim HEX", 1)]
    for _ in range(3):
        clock[0] += 31 * MIN
        quiet_tick(core)
    assert [f["raises"] for f in held(tmp_path) if f["thing"] == "Area18"] == [0]


def test_remember_conversations_off_means_nothing_is_collected(tmp_path):
    core, clock = build(tmp_path, remember=False)
    started = []
    core._note_fact = lambda text: started.append(text)
    assert core.heard("I fly a Cutlass Black.") == ""
    settle()
    assert started == []
    del core._note_fact
    core._note_fact("I fly a Cutlass Black.")                    # and asked directly, there is nowhere to count it
    assert not (tmp_path / "pilot").exists() and core._facts is None
    clock[0] += 60 * MIN
    assert quiet_tick(core) == []
    # switched off later: what was counted stays on disk until it is forgotten, and none of it is said
    core2, clock2 = build(tmp_path / "y")
    hear(core2, "I fly a Cutlass Black.")
    core2.tree = None
    clock2[0] += 60 * MIN
    assert quiet_tick(core2) == [] and held(tmp_path / "y")[0]["raises"] == 0


def test_the_count_runs_off_the_callers_thread_and_cannot_hold_or_break_the_answer(tmp_path, monkeypatch):
    core, clock = build(tmp_path)
    gate, ran_on = threading.Event(), []

    def slow(self, text, t=None):
        ran_on.append(threading.current_thread().name)
        gate.wait(5)
        raise RuntimeError("the store broke")
    monkeypatch.setattr(pf.PilotFacts, "note", slow)
    t0 = time.time()
    assert core.heard("I fly a Cutlass Black.")                  # comes back while the count is still stuck
    assert time.time() - t0 < 1.0 and not gate.is_set()
    gate.set()
    settle()
    assert ran_on == ["suitmk2_fact_note"]
    core._note_fact("I fly a Cutlass Black.")                    # and on this thread it still raises nothing
    monkeypatch.setattr(core, "_pilot_facts", lambda: 1 / 0)
    core._note_fact("I fly a Cutlass Black.")
    assert core._try_pilot_fact({}) is False


# ---------------------------------------------------------------------------------------------------------------
# The core: speaking
# ---------------------------------------------------------------------------------------------------------------
def ready(tmp_path, **kw):
    """A core that holds one fact old enough to raise."""
    core, clock = build(tmp_path, **kw)
    hear(core, "I fly a Cutlass Black.")
    clock[0] += 11 * MIN
    return core, clock


def test_on_a_fact_line_is_said_by_the_companion_through_the_ordinary_path(tmp_path):
    core, clock = ready(tmp_path)
    assert quiet_tick(core) == [("elah", "Still in the Cutlass Black?")]
    f = held(tmp_path)[0]
    assert f["raises"] == 1 and f["by"] == "elah" and core.stats["fact_lines"] == 1 and core.stats["spoken"] == 1
    assert core.gate_state.last_spoken_at == clock[0]            # counted by the speak gate like any other line
    assert core.gate_state.quiet_budget_log == [clock[0]]        # and against the quiet budget
    assert core.feedback.last_line().text == "Still in the Cutlass Black?"
    # the line that was said is exactly a line the data file holds, with the name dropped in
    assert "Still in the {thing}?" in json.loads(fl.PATH.read_text(encoding="utf-8"))["lines"]["elah"]["flies"]["ship"]


def test_at_most_one_in_the_interval_and_the_two_take_turns(tmp_path):
    core, clock = build(tmp_path)
    for line in ("I fly a Cutlass Black.", "I always land at Grim HEX.", "I want a Kraken."):
        hear(core, line)
    clock[0] += 11 * MIN
    assert companion_core.CompanionCore.FACT_LINE_EVERY_S == 1800.0
    first = quiet_tick(core)
    assert len(first) == 1 and first[0][0] == "elah"
    for _ in range(9):                                           # 27 more minutes of ticks: nothing, whatever is held
        clock[0] += 3 * MIN
        assert quiet_tick(core) == []
    assert sum(f["raises"] for f in held(tmp_path)) == 1
    clock[0] += 3 * MIN + 1
    second = quiet_tick(core)
    assert len(second) == 1 and second[0][0] == "montaigne"
    clock[0] += 31 * MIN
    third = quiet_tick(core)
    assert len(third) == 1 and third[0][0] == "elah"
    assert len({first[0][1], second[0][1], third[0][1]}) == 3
    things = [n for n in ("Cutlass Black", "Grim HEX", "Kraken") if any(n in s[1] for s in first + second + third)]
    assert len(things) == 3                                      # each thing once; none of them twice in the week
    clock[0] += 31 * MIN
    assert quiet_tick(core) == []                                # nothing left that is fit to raise


def test_nothing_fit_to_raise_or_no_wording_leaves_the_tick_as_it_was(tmp_path, monkeypatch):
    core, clock = build(tmp_path)
    clock[0] += 60 * MIN
    assert core._try_pilot_fact({}) is False                     # no fact at all
    hear(core, "I fly a Cutlass Black.")
    assert core._try_pilot_fact({}) is False                     # asked again too soon (FACT_LINE_RETRY_S)
    clock[0] += 6 * MIN
    assert core._try_pilot_fact({}) is False                     # an echo: said less than ten minutes ago
    clock[0] += 6 * MIN
    monkeypatch.setattr(fl, "PATH", tmp_path / "gone.json")      # a fact, and no wording for it
    assert quiet_tick(core) == [] and core.stats["fact_no_line"] == 1 and "fact_lines" not in core.stats


GUARDS = {
    "muted": lambda core, clock: setattr(core.speech, "muted", True),
    "in a fight": lambda core, clock: setattr(core, "combat", type("Fight", (), {"active": True})()),
    "headroom TIGHT": lambda core, clock: setattr(core, "headroom", lambda: "TIGHT"),
    "no headroom reading": lambda core, clock: setattr(core, "headroom", lambda: None),
    "the headroom reader fails": lambda core, clock: setattr(core, "headroom", lambda: 1 / 0),
    "the PC overloaded": lambda core, clock: setattr(core.overload, "off", True),
    "the card hot": lambda core, clock: setattr(core, "temperature_state", companion_core.hardware_guard.HOT),
    "not now": lambda core, clock: core.not_now.snooze(10),
    "the pilot is speaking": lambda core, clock: setattr(core.gate_state, "pilot_speaking", True),
    "party comms": lambda core, clock: setattr(core.gate_state, "party_comms_active", True),
    "inside the ambient cooldown": lambda core, clock: core.gate.record_spoken(
        core.gate_state, Candidate(Priority.AMBIENT, "montaigne", 8, clock[0])),
    "still shaken": lambda core, clock: setattr(core.affect, "hushed", lambda: True),
}


@pytest.mark.parametrize("name", sorted(GUARDS))
def test_no_fact_line_while_something_holds_unprompted_talk_and_the_fact_is_not_spent(tmp_path, name):
    core, clock = ready(tmp_path)
    GUARDS[name](core, clock)
    assert core._try_pilot_fact({}) is False
    assert quiet_tick(core) == [], name
    assert held(tmp_path)[0]["raises"] == 0 and "fact_lines" not in core.stats, name


def test_no_fact_line_in_a_fight_the_ambient_tick_sees_or_with_the_pilot_away(tmp_path):
    from _eyes_helpers import FakeEyes
    eyes = FakeEyes()
    eyes.state = lambda: {"in_combat": True}
    core, clock = ready(tmp_path, eyes=eyes)
    assert quiet_tick(core) == [] and core.stats["combat_hold"] == 1 and held(tmp_path)[0]["raises"] == 0
    away, clock2 = ready(tmp_path / "away", idle_source=lambda: 9999.0, afk_after_s=60.0)
    clock2[0] += 2 * MIN
    assert away.afk.afk() and quiet_tick(away) == [] and held(tmp_path / "away")[0]["raises"] == 0


def test_a_fact_line_goes_ahead_of_nothing_that_was_there_before(tmp_path):
    core, clock = ready(tmp_path)
    core._last_idle = core._idle_try = -1e9                      # the relationship slot is open: it goes first
    core.ambient_tick()
    assert held(tmp_path)[0]["raises"] == 0 and core._work.qsize() == 1
    assert core._work.get_nowait()[1]["scenario"] == "idle_relationship"
    core2, clock2 = ready(tmp_path / "b")
    core2._try_banter = lambda st: True                          # banter took the tick
    assert quiet_tick(core2) == [] and held(tmp_path / "b")[0]["raises"] == 0
    src = (ROOT / "core" / "companion_core.py").read_text(encoding="utf-8")
    tick = src[src.index("    def ambient_tick"):src.index("    def _ambient_loop")]
    assert tick.count("_try_pilot_fact(") == 1 and src.count("_try_pilot_fact(") == 2      # one caller, in the tick
    assert tick.index("_try_banter(st)") < tick.index("_try_pilot_fact(st)") < tick.index("build_ambient_spec(")
    assert "URGENT" not in src[src.index("    def _try_pilot_fact"):src.index("    # -- direct questions")]


def test_a_line_gated_between_the_tick_and_the_voice_is_not_said(tmp_path):
    core, clock = ready(tmp_path)
    core._last_idle = core._idle_try = core.now()
    core.ambient_tick()
    assert core._work.qsize() == 1
    core.not_now.snooze(10)                                      # "not now" pressed while it waited
    core._realize_one(core._work.get_nowait())
    assert core.speech.said == [] and core.stats["not_now"] == 1
    core2, clock2 = ready(tmp_path / "m")
    core2.speech.say = lambda text, speaker, priority: False     # the voice refuses (window hidden)
    assert quiet_tick(core2) == [] and core2.stats["spoken"] == 0


# ---------------------------------------------------------------------------------------------------------------
# Forgetting, and broken files
# ---------------------------------------------------------------------------------------------------------------
def test_forget_conversations_deletes_the_counts_as_well(tmp_path):
    from ui import suit_window
    core, clock = ready(tmp_path)
    assert state_file(tmp_path).exists() and len(core.tree.records()) == 1
    assert suit_window.forget_conversations(core, tmp_path / "pilot") == 1
    assert not state_file(tmp_path).exists() and core.tree.records() == []
    clock[0] += 60 * MIN
    assert quiet_tick(core) == []                                # and nothing of it is said afterwards
    # with no running core (companions disabled), and with remembering switched off
    for who in (None, type("Core", (), {"tree": None})()):
        other, _ = ready(tmp_path / "pilot2")
        d = tmp_path / "pilot2" / "pilot"
        assert (d / "tree" / pf.STATE_NAME).exists()
        (d / "tree" / (pf.STATE_NAME + ".tmp")).write_text("{}", encoding="utf-8")
        assert suit_window.forget_conversations(who, d) == 1
        assert not (d / "tree" / pf.STATE_NAME).exists() and not (d / "tree" / (pf.STATE_NAME + ".tmp")).exists()
    assert companion_core.forget_pilot_facts(None, tmp_path / "never") is False
    assert companion_core.forget_pilot_facts(None, None) is False               # never raises
    win = (ROOT / "ui" / "suit_window.py").read_text(encoding="utf-8")
    tip = win[win.index('forget = QPushButton("Forget conversations")'):win.index("forget.clicked.connect")]
    assert "your ship, your kit, your haunts and your plans" in tip
    ask = win[win.index("def _forget_conversations"):win.index("def _export_memory")]
    assert "your ship, your kit, your haunts and your plans" in ask


def test_forgetting_still_deletes_the_counts_when_the_log_cannot_be_cleared(tmp_path):
    from ui import suit_window
    core, clock = ready(tmp_path)
    core.tree.clear = lambda: 1 / 0
    with pytest.raises(ZeroDivisionError):
        suit_window.forget_conversations(core, tmp_path / "pilot")
    assert not state_file(tmp_path).exists()


@pytest.mark.parametrize("damage", ["", "{", "[]", "null", '{"schema": "something else"}',
                                    '{"schema": "suitmk2.pilot_facts.state", "facts": {"x": 1}, "barred": {}}'])
def test_a_broken_state_file_never_raises_and_never_speaks(tmp_path, damage):
    core, clock = ready(tmp_path)
    state_file(tmp_path).write_text(damage, encoding="utf-8")
    hear(core, "I always land at Grim HEX.")                     # counting into a broken file: nothing, no error
    clock[0] += 60 * MIN
    assert quiet_tick(core) == []
    assert state_file(tmp_path).read_text(encoding="utf-8") == damage            # and it is not papered over


def test_a_missing_state_file_or_folder_never_raises_and_never_speaks(tmp_path):
    core, clock = build(tmp_path)
    clock[0] += 60 * MIN
    assert quiet_tick(core) == [] and not state_file(tmp_path).exists()
    core.tree = type("Tree", (), {"dir": tmp_path / "pilot" / "nowhere" / "tree"})()
    clock[0] += 60 * MIN
    assert quiet_tick(core) == []
    core.tree = type("Tree", (), {"dir": None})()
    clock[0] += 60 * MIN
    assert quiet_tick(core) == []


# ---------------------------------------------------------------------------------------------------------------
# Who calls what
# ---------------------------------------------------------------------------------------------------------------
def test_one_module_words_a_fact_and_only_the_core_asks_it_to():
    users, sayers = [], []
    for p in list((ROOT / "core").rglob("*.py")) + list((ROOT / "ui").rglob("*.py")) + list(ROOT.glob("*.py")):
        src = p.read_text(encoding="utf-8", errors="replace")
        if p.name != "fact_lines.py" and re.search(r"import fact_lines|from fact_lines|\bfactl\.", src):
            users.append(p.name)
        if "fact_lines.json" in src:
            sayers.append(p.name)
    assert users == ["companion_core.py"] and sorted(sayers) == ["fact_lines.py", "settings.py"]
    src = (ROOT / "core" / "fact_lines.py").read_text(encoding="utf-8")
    for banned in ("ollama", "requests", "urllib", "socket", "subprocess", "tree_memory", ".records(", "open_tree"):
        assert banned not in src, banned                         # no model, no network, and never the pilot's words
    core = (ROOT / "core" / "companion_core.py").read_text(encoding="utf-8")
    assert core.count("factl.") == 1 and core.count(".next_fact(") == 1 and core.count("facts.note(") == 1
