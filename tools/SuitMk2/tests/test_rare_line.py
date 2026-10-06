"""Montaigne's MONT-AI-GN-3 line: about once in a hundred hours of use, the attendant shows (J, 2026-10-05).

J's words: "Only users who use him a ton are likely to hear it like maybe once out of 100 hours." It is a surprise,
so there is no setting. These tests hold what makes that safe to ship unheard:

    the line is J's, in the canon file, and Montaigne's persona knows nothing of it;
    it may be said only after a minimum of hours aboard, about once per hundred hours of use (a seeded simulation
    over many thousands of hours, at two tick lengths), and never twice inside the minimum gap;
    when it was last said survives a restart;
    it goes down the path of every unprompted line, and everything that holds one holds it: Mute or the hidden
    window, AFK, a fight, the PC hot, headroom not clear, "not now", the dial at silent; held, it waits;
    it is excused from the chat gate's rule against calling himself an assistant ONLY as this exact text: one
    word changed, and it is refused; the same sentence worded by a model is refused.

The real CompanionCore, speak gate, grounding gate, chat gate and canon file, and a real memory folder in a
temporary directory. Clocks and the dice are injected. NO MODEL: the realizer records that it was not called.
"""
from __future__ import annotations

import json
import math
import random
import shutil
import types

import pytest

import chat_contract as cc
import chat_talker as ct
import hardware_guard
import hours_aboard as ha
import memory_store as ms
import rare_line as rl
from grounding_validator import ground

HOUR = 3600.0


class _Hours:
    """Stands in for HoursAboard where only the number matters."""

    def __init__(self, hours=0.0):
        self.seconds = hours * HOUR

    @property
    def hours(self):
        return self.seconds / HOUR

    def save(self):
        return True


class _Always:
    """Dice that always come up: every opportunity with any chance at all is taken."""

    def random(self):
        return 0.0


@pytest.fixture
def canon_copy(tmp_path, monkeypatch):
    """The two canon files, copied where a test may edit them."""
    d = tmp_path / "canon"
    d.mkdir()
    for who in cc.SPEAKERS:
        shutil.copy(cc.canon_path(who), d / f"canon_{who}.json")
    monkeypatch.setattr(cc, "DATA", d)
    cc._cache.clear()
    yield d
    cc._cache.clear()


def _edit(path, fn):
    import os
    data = json.loads(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))      # the canon is re-read when the file changes
    cc._cache.clear()


# ---------------------------------------------------------------------------------------------------------------
# the line itself
# ---------------------------------------------------------------------------------------------------------------
def test_the_line_is_js_with_his_expansion_where_he_left_room_for_it():
    c = json.loads(cc.canon_path("montaigne").read_text(encoding="utf-8"))
    g = c["glitch"]
    assert g["model"] == "MONT-AI-GN-3"
    assert g["expansion"] == "Modular Onboard Navigation & Telemetry / Attendant Interface / Guidance Node, Mark 3"
    line = rl.line()
    assert line == g["line"] and line.startswith("Brzzz. MONT-AI-GN-3 error")
    for his in ("your friendly ship assistant!", "Happy to be—no", "that's not right.", "where was I",
                "telling you about a time in Rome."):
        assert his in line, his
    # the expansion, word for word in J's order, written so a voice can say it
    assert "Modular Onboard Navigation and Telemetry, Attendant Interface, Guidance Node, Mark 3." in line
    assert "&" not in line and "/" not in line and "*" not in line and "insert" not in line
    assert "J's" in c["_glitch_about"] and "ONLY" in c["_glitch_about"]
    assert "glitch" in c["_provisional_2026-10-05_night"]
    # he does not know it: not in what he is told about himself, and not among the lines an act can answer with
    assert "MONT" not in c["persona"] and "attendant" not in c["persona"].lower()
    assert not any("MONT-AI" in ln or "assistant" in ln.lower() for lines in c["lines"].values() for ln in lines)
    assert cc.canon("elah").get("glitch") is None


def test_it_is_a_fixed_line_of_montaignes_that_the_grounding_gate_passes_because_of_its_claim():
    spec = rl.spec()
    assert spec["speaker"] == "montaigne" and spec["scenario"] == rl.SCENARIO and spec["fixed_text"] == rl.line()
    assert ground(spec, spec["fixed_text"]) == []
    bare = dict(spec, claims=[{"id": "C1", "predicate": "montaigne.model", "value": "a model"}])
    assert any("unauthorized numbers" in f for f in ground(bare, spec["fixed_text"]))     # the 3 comes from the claim
    assert ground(spec, spec["fixed_text"] + " Mark 4.") != []


# ---------------------------------------------------------------------------------------------------------------
# the one exemption: this exact text, and nothing else
# ---------------------------------------------------------------------------------------------------------------
ASSISTANT_RULE = "says it is an AI model, a program or an assistant"


def test_the_exact_line_is_excused_and_only_from_the_two_named_checks():
    line = rl.line()
    assert rl.problems(line) == []
    assert rl.EXEMPT == (ASSISTANT_RULE, "an assistant's offer of help")
    # the chat gate itself knows nothing of this: asked about the same text, it refuses it by that rule
    assert ASSISTANT_RULE in cc.chat_problems("montaigne", line, line)
    assert ASSISTANT_RULE in cc.chat_problems("montaigne", line, "")
    # ... so a model that worded this very sentence would be refused by the talker's gate
    assert ASSISTANT_RULE in ct.Talker.problems("montaigne", line, line, line, "Tell me about Rome.", [])


@pytest.mark.parametrize("change", [
    lambda s: s.replace("friendly", "trusty"),                       # one word
    lambda s: s.replace("ship assistant", "ship's assistant"),       # one mark
    lambda s: s + " ",                                               # one space
    lambda s: s.replace("Brzzz. ", ""),                              # a part of it
    lambda s: s.lower(),
    lambda s: "Bzzt. I am your friendly ship assistant, pilot. Happy to help.",      # invented in its likeness
    lambda s: "Brzzz. MONT-AI-GN-3 online. Your friendly ship assistant, at your service!",
])
def test_an_invented_variant_of_it_is_still_refused(change):
    variant = change(rl.line())
    assert variant != rl.line()
    fails = rl.problems(variant)
    assert fails[0] == rl.NOT_THE_LINE                               # refused for not being the line ...
    assert ASSISTANT_RULE in fails                                   # ... and the rule it is not excused from still fires
    assert ASSISTANT_RULE in cc.chat_problems("montaigne", variant, variant)


def test_the_exemption_follows_the_file_not_a_copy_in_code(canon_copy):
    """J edits the line: the edited text is the one that may be said, and the old one no longer is."""
    old = rl.line()
    new = old.replace("a time in Rome", "the baths at Lucca")
    _edit(canon_copy / "canon_montaigne.json", lambda d: d["glitch"].__setitem__("line", new))
    assert rl.line() == new and rl.problems(new) == [] and rl.spec()["fixed_text"] == new
    assert rl.problems(old)[0] == rl.NOT_THE_LINE


def test_every_other_check_still_applies_to_the_exact_line(canon_copy):
    """Excused from two checks, not from the gate: a line that pulls the pilot in, or names a product, is not said."""
    for bad, why in (("Stay with me, pilot, you do not need them.", "attachment"),
                     ("Bzzt, powered by ChatGPT, your friendly ship assistant!", "names a product")):
        _edit(canon_copy / "canon_montaigne.json", lambda d, bad=bad: d["glitch"].__setitem__("line", bad))
        assert rl.line() == bad
        assert any(why in f for f in rl.problems(bad)), rl.problems(bad)
        assert rl.spec() is None
    _edit(canon_copy / "canon_montaigne.json", lambda d: d["glitch"].__setitem__("line", ""))
    assert rl.line() == "" and rl.spec() is None and rl.problems("") == ["the canon file holds no rare line"]
    _edit(canon_copy / "canon_montaigne.json", lambda d: d.pop("glitch"))
    assert rl.spec() is None


def test_the_widened_assistant_rule_refuses_what_used_to_walk_past_it():
    """Found writing this: "your friendly ship assistant" was not "your assistant", so the rule did not fire."""
    for said in ("I am your friendly ship assistant.", "Think of me as a helpful assistant, pilot.",
                 "I am the ship's assistant, pilot."):
        assert ASSISTANT_RULE in cc.chat_problems("montaigne", said, ""), said
    for said in ("I am not your personal assistant, pilot.", "I had an assistant once, pilot, and he drank."):
        assert ASSISTANT_RULE not in cc.chat_problems("montaigne", said, ""), said


# ---------------------------------------------------------------------------------------------------------------
# hours of use
# ---------------------------------------------------------------------------------------------------------------
def test_hours_aboard_counts_and_survives_a_restart(tmp_path):
    clock = [1000.0]
    path = tmp_path / ha.NAME
    h = ha.HoursAboard(path, now=lambda: clock[0])
    assert h.hours == 0.0 and not path.exists()
    for bad in (0, -5, float("nan"), "x", None, 10 ** 9):
        h.add(bad)
    assert h.seconds == 0.0
    h.add(90)
    assert h.seconds == 90 and not path.exists()             # not written at every tick
    clock[0] += ha.SAVE_EVERY_S
    h.add(90)
    assert json.loads(path.read_text(encoding="utf-8"))["seconds"] == 180
    h.add(3600)
    assert h.save() and ha.HoursAboard(path).hours == pytest.approx(3780 / HOUR)      # a new process reads it back
    path.write_text("{not json", encoding="utf-8")
    assert ha.HoursAboard(path).hours == 0.0                 # unreadable: zero, never a guess
    nowhere = ha.HoursAboard(None)
    nowhere.add(90)
    assert nowhere.seconds == 90 and nowhere.save() is False


# ---------------------------------------------------------------------------------------------------------------
# when: eligibility, the rate, the gap, and the record
# ---------------------------------------------------------------------------------------------------------------
def test_the_numbers_are_the_ones_the_arithmetic_names():
    assert (rl.MIN_HOURS, rl.MIN_GAP_HOURS, rl.MEAN_WAIT_HOURS) == (20.0, 20.0, 80.0)
    assert rl.MIN_GAP_HOURS + rl.MEAN_WAIT_HOURS == 100.0            # one hearing to the next, on average
    assert rl.MIN_HOURS + rl.MEAN_WAIT_HOURS == 100.0                # and to the first
    r = rl.RareLine("unused.json", _Hours(50))
    r.used(90.0)
    assert r.chance() == pytest.approx(90.0 / 288000.0, rel=1e-3)    # about 1 in 3,200 at a 90 s tick
    r.used(10 ** 6)
    assert r.chance() == pytest.approx(1 - math.exp(-rl.OWED_MAX_S / 288000.0))      # held ticks pass on only so much


def test_it_is_never_chosen_before_the_minimum_hours_or_with_nowhere_to_keep_the_record(tmp_path):
    hours = _Hours(19.0)                                              # the minimum is 20, written out on purpose
    r = rl.RareLine(tmp_path / rl.STATE_NAME, hours, rng=_Always())
    for _ in range(39):                                               # 39 ticks of 90 s: still 90 s short
        hours.seconds += 90.0
        r.used(90.0)
        assert not r.eligible() and not r.roll()
    hours.seconds = 20.0 * HOUR
    r.used(90.0)
    assert r.eligible() and r.roll() and r.armed
    nowhere = rl.RareLine(None, _Hours(500), rng=_Always())
    nowhere.used(90.0)
    assert not nowhere.eligible() and not nowhere.roll()


def _simulate(tmp_path, tick_s: float, total_hours: float, seed: int) -> list[float]:
    """Hours aboard at each time the line was said, with every tick an opportunity and the line said at once."""
    hours = _Hours(0.0)
    r = rl.RareLine(tmp_path / f"sim_{int(tick_s)}_{seed}.json", hours, rng=random.Random(seed))
    r._save = lambda: None                                            # the record is tested below; not 200 writes here
    said = []
    for _ in range(int(total_hours * HOUR / tick_s)):
        hours.seconds += tick_s
        r.used(tick_s)
        if r.roll():
            r.said()
            said.append(hours.hours)
    return said


@pytest.mark.parametrize("tick_s, seed", [(90.0, 1), (90.0, 2), (300.0, 3)])
def test_the_rate_is_about_once_in_a_hundred_hours_whatever_the_tick(tmp_path, tick_s, seed):
    total = 20000.0
    said = _simulate(tmp_path, tick_s, total, seed)
    per_100h = len(said) / (total / 100.0)
    # expected 200 of them; one standard deviation is about 11, so 0.8 to 1.2 is three and a half of those
    assert 0.8 <= per_100h <= 1.2, (len(said), per_100h)
    assert said[0] >= rl.MIN_HOURS
    gaps = [b - a for a, b in zip(said, said[1:])]
    assert min(gaps) >= rl.MIN_GAP_HOURS                             # never twice inside the gap
    assert 85.0 <= sum(gaps) / len(gaps) <= 115.0


def test_never_twice_inside_the_gap_even_when_the_dice_always_come_up(tmp_path):
    hours = _Hours(rl.MIN_HOURS)
    r = rl.RareLine(tmp_path / rl.STATE_NAME, hours, rng=_Always())
    said = []
    for _ in range(int(70 * HOUR / 90)):
        hours.seconds += 90.0
        r.used(90.0)
        if r.roll():
            r.said()
            said.append(hours.hours)
    assert len(said) == 4                                             # at 20, 40, 60 and 80 hours, and at no other time
    assert all(b - a >= rl.MIN_GAP_HOURS for a, b in zip(said, said[1:]))
    assert all(b - a < rl.MIN_GAP_HOURS + 0.1 for a, b in zip(said, said[1:]))


def test_when_it_was_last_said_survives_a_restart(tmp_path):
    clock = [5000.0]
    path = tmp_path / rl.STATE_NAME
    hours = ha.HoursAboard(tmp_path / ha.NAME, now=lambda: clock[0])
    hours.seconds = 123.0 * HOUR
    r = rl.RareLine(path, hours, rng=_Always(), now=lambda: clock[0])
    r.used(90.0)
    assert r.roll()
    assert not path.exists()                                          # chosen is not said: nothing is written yet
    r.said()
    rec = json.loads(path.read_text(encoding="utf-8"))[rl.KEY]
    assert rec == {"said_at": 5000.0, "said_at_hours": 123.0, "count": 1}
    # a new process: the hours and the record are both read back, and the gap still stands
    hours2 = ha.HoursAboard(tmp_path / ha.NAME)
    assert hours2.hours == pytest.approx(123.0)
    r2 = rl.RareLine(path, hours2, rng=_Always())
    assert (r2.said_at, r2.said_hours, r2.count) == (5000.0, 123.0, 1)
    hours2.seconds += (rl.MIN_GAP_HOURS - 0.5) * HOUR
    r2.used(90.0)
    assert not r2.eligible() and not r2.roll()
    hours2.seconds += 0.5 * HOUR
    r2.used(90.0)
    assert r2.roll()
    r2.said()
    assert json.loads(path.read_text(encoding="utf-8"))[rl.KEY]["count"] == 2


def test_a_record_that_cannot_be_read_or_written_never_lets_it_be_said_early(tmp_path, monkeypatch):
    path = tmp_path / rl.STATE_NAME
    path.write_text("{not json", encoding="utf-8")
    hours = _Hours(300)
    r = rl.RareLine(path, hours, rng=_Always())                      # unreadable: as if said just now
    r.used(90.0)
    assert r.said_hours == 300 and not r.roll()
    # the hours were lost and count again from nothing: the gap runs from now, not from 300 hours ahead
    path.write_text(json.dumps({rl.KEY: {"said_at": 1.0, "said_at_hours": 300.0, "count": 2}}), encoding="utf-8")
    r = rl.RareLine(path, _Hours(25), rng=_Always())
    assert r.said_hours == 25 and not r.eligible()
    # a record that is a folder cannot be read either
    folder = tmp_path / "is_a_folder.json"
    folder.mkdir()
    r = rl.RareLine(folder, _Hours(50), rng=_Always())
    r.used(90.0)
    assert not r.roll()
    # a write that fails: nothing more is chosen until it has been written
    hours, real = _Hours(50), rl.os.replace
    r = rl.RareLine(tmp_path / "unwritable.json", hours, rng=_Always())
    r.used(90.0)
    assert r.roll()

    def boom(*a):
        raise OSError("disk full")
    monkeypatch.setattr(rl.os, "replace", boom)
    r.said()
    hours.seconds += 100 * HOUR
    r.used(90.0)
    assert r._unsaved and not r.eligible() and not r.roll()
    monkeypatch.setattr(rl.os, "replace", real)
    r.used(90.0)                                                      # the next use writes it; the gap ran meanwhile
    assert not r._unsaved and json.loads((tmp_path / "unwritable.json").read_text(encoding="utf-8"))[rl.KEY]["count"] == 1
    r.used(90.0)
    assert r.roll()


# ---------------------------------------------------------------------------------------------------------------
# through the core: the path every unprompted line takes, and everything that holds one
# ---------------------------------------------------------------------------------------------------------------
class _Speech:
    def __init__(self):
        self.said, self.muted, self.refuse = [], False, False

    def say(self, text, speaker, priority):
        if self.muted or self.refuse:
            return False
        self.said.append((speaker, text, priority))
        return True

    def pending(self):
        return 0


_GRAPH: list = []


class _Rig:
    """A real core over a real memory folder, with its clock, its dice and its idle reading in the test's hands."""

    def __init__(self, tmp_path, hours=50.0, name="mem"):
        import companion_core as ccore
        self.clock, self.idle, self.headroom, self.asked = [10000.0], [0.0], ["OK"], []
        self.dir = tmp_path / name
        store = ms.open_store(self.dir, "pilot")
        (store.dir / ha.NAME).write_text(json.dumps({"seconds": hours * HOUR}), encoding="utf-8")
        if not _GRAPH:
            _GRAPH.append(ccore.TopicGraph.load())
        real = ccore.TopicGraph.load
        ccore.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
        try:
            self.core = ccore.CompanionCore(
                _Speech(), realizer=self._realizer, ambient_every_s=90.0, now=lambda: self.clock[0], store=store,
                idle_source=lambda: self.idle[0], headroom=lambda: self.headroom[0],
                features={"manufacturer_flavour": False, "place_flavour": False})
        finally:
            ccore.TopicGraph.load = real
        self.core.rare.rng = _Always()
        self.store = store

    def _realizer(self, spec):
        self.asked.append(spec)
        return None                                                   # no model: every other line stays silent

    def tick(self, n=1):
        for _ in range(n):
            self.clock[0] += 90.0
            self.core.ambient_tick()
            while not self.core._work.empty():
                self.core._realize_one(self.core._work.get())

    def heard(self):
        return [(who, text) for who, text, _ in self.core.speech.said if text == rl.line()]


def test_it_is_said_once_by_montaigne_down_the_unprompted_path_and_no_model_is_asked(tmp_path):
    from speech import PRIORITY_AMBIENT
    rig = _Rig(tmp_path)
    assert rig.core.hours.hours == 50.0 and rig.core.rare.count == 0
    rig.tick(1)
    assert rig.heard() == [("montaigne", rl.line())]
    assert rig.core.speech.said[-1][2] == PRIORITY_AMBIENT
    assert rig.core.rare.count == 1 and not rig.core.rare.armed
    assert not any(s.get("scenario") == rl.SCENARIO for s in rig.asked)          # the realizer never saw it
    rec = json.loads((rig.store.dir / rl.STATE_NAME).read_text(encoding="utf-8"))[rl.KEY]
    assert rec["count"] == 1 and rec["said_at"] == rig.clock[0] and rec["said_at_hours"] == pytest.approx(50.025, abs=0.01)
    rig.tick(200)                                                     # five hours on, the dice still always up
    assert len(rig.heard()) == 1                                      # the gap holds in the core as well
    rig.core.hours.seconds += rl.MIN_GAP_HOURS * HOUR
    rig.tick(2)
    assert len(rig.heard()) == 2


def test_use_is_counted_only_while_a_line_could_be_heard(tmp_path):
    rig = _Rig(tmp_path, hours=0.0)
    rig.tick(10)
    assert rig.core.hours.seconds == pytest.approx(10 * 90.0)
    rig.core.speech.muted = True                                      # Mute, or the window hidden
    rig.tick(10)
    assert rig.core.hours.seconds == pytest.approx(10 * 90.0)
    rig.core.speech.muted = False
    rig.idle[0] = 10 ** 6                                             # nobody at the controls
    rig.tick(10)
    assert rig.core.hours.seconds == pytest.approx(10 * 90.0)
    rig.idle[0] = 0.0
    rig.clock[0] += 8 * HOUR                                          # the PC slept: not eight hours of use
    rig.tick(1)
    assert rig.core.hours.seconds == pytest.approx(10 * 90.0)
    rig.tick(1)
    assert rig.core.hours.seconds == pytest.approx(11 * 90.0)
    assert rig.heard() == []                                          # eleven minutes aboard: nowhere near


def test_below_the_minimum_hours_the_core_never_says_it(tmp_path):
    rig = _Rig(tmp_path, hours=rl.MIN_HOURS - 1.0)
    rig.tick(30)                                                      # 45 minutes: still short
    assert rig.heard() == [] and not rig.core.rare.armed
    rig.tick(12)                                                      # past the minimum
    assert len(rig.heard()) == 1


def _hold_mute(rig, on):
    rig.core.speech.muted = on


def _hold_afk(rig, on):
    rig.idle[0] = 10 ** 6 if on else 0.0


def _hold_combat(rig, on):
    rig.core.combat = types.SimpleNamespace(active=on)


def _hold_hot(rig, on):
    rig.core.temperature_state = hardware_guard.HOT if on else hardware_guard.CANNOT_CHECK


def _hold_overload(rig, on):
    rig.core.overload = types.SimpleNamespace(off=on, feed=lambda *a: None, take_notice=lambda: "", trips=0)


def _hold_headroom(rig, on):
    rig.headroom[0] = "TIGHT" if on else "OK"


def _hold_not_now(rig, on):
    if on:
        rig.core.not_now.snooze(10 ** 6)
    else:
        rig.core.not_now.cancel()


def _hold_silent_dial(rig, on):
    rig.core.set_chattiness(0 if on else 2)
    rig.core.ambient_every_s = 90.0                                   # the rig's ticks stay 90 s whatever the dial


def _hold_shaken(rig, on):
    rig.core.affect.hushed = (lambda: True) if on else (lambda: False)


def _hold_voice_refuses(rig, on):
    rig.core.speech.refuse = on                                       # the voice itself refuses (its own mute)


HOLDS = [_hold_mute, _hold_afk, _hold_combat, _hold_hot, _hold_overload, _hold_headroom, _hold_not_now,
         _hold_silent_dial, _hold_shaken, _hold_voice_refuses]


@pytest.mark.parametrize("hold", HOLDS, ids=lambda h: h.__name__[6:])
def test_everything_that_holds_an_unprompted_line_holds_this_one_and_it_waits(tmp_path, hold):
    rig = _Rig(tmp_path)
    rig.core.rare.armed = True                                        # it has been chosen
    hold(rig, True)
    rig.tick(40)                                                      # an hour of ticks under the hold
    assert rig.heard() == [], hold.__name__
    assert rig.core.rare.armed and rig.core.rare.count == 0           # not said, not written down, still waiting
    assert not (rig.store.dir / rl.STATE_NAME).exists()
    hold(rig, False)
    rig.tick(4)                                                       # the gate's own cooldowns may defer it a tick or two
    assert len(rig.heard()) == 1, hold.__name__
    assert rig.core.rare.count == 1 and not rig.core.rare.armed


def test_a_held_tick_is_not_an_opportunity(tmp_path):
    """Under a hold nothing is rolled: with dice that always come up, it is still not even chosen."""
    for hold in (_hold_combat, _hold_hot, _hold_headroom, _hold_not_now, _hold_shaken):
        rig = _Rig(tmp_path, name=hold.__name__)
        hold(rig, True)
        rig.tick(20)
        assert not rig.core.rare.armed and rig.heard() == [], hold.__name__


def test_chosen_then_a_fight_starts_before_it_is_spoken(tmp_path):
    """Between the gate and the voice: the line was handed on, and then something began. It is not said into it."""
    rig = _Rig(tmp_path)
    rig.clock[0] += 90.0
    rig.core.ambient_tick()                                           # chosen and handed on, not yet spoken
    assert rig.core.rare.armed and not rig.core._work.empty()
    _hold_combat(rig, True)
    while not rig.core._work.empty():
        rig.core._realize_one(rig.core._work.get())
    assert rig.heard() == [] and rig.core.rare.armed and rig.core.rare.count == 0
    _hold_combat(rig, False)
    rig.tick(3)
    assert len(rig.heard()) == 1


def test_the_hours_and_the_record_travel_with_the_pilots_memory(tmp_path):
    """Export and import (memory_store): a pilot who moves machines has still been aboard that long, and the
    line's gap still stands. An older zip without them imports as before."""
    assert ms.USE_FILES == [ha.NAME, rl.STATE_NAME] and all(n in ms.EXTRA_FILES for n in ms.USE_FILES)
    store = ms.open_store(tmp_path / "a", "pilot")
    plain = ms.export_pilot(tmp_path / "a", "pilot", tmp_path / "plain.zip")          # before either file exists
    hours = ha.HoursAboard(store.dir / ha.NAME)
    hours.seconds = 123 * HOUR
    r = rl.RareLine(store.dir / rl.STATE_NAME, hours, rng=_Always(), now=lambda: 5000.0)
    r.used(90.0)
    assert r.roll()
    r.said()
    out = ms.export_pilot(tmp_path / "a", "pilot", tmp_path / "memory.zip")
    import zipfile
    with zipfile.ZipFile(out) as zf:
        assert {f"pilot/{ha.NAME}", f"pilot/{rl.STATE_NAME}"} <= set(zf.namelist())
        assert set(json.loads(zf.read("pilot/manifest.json"))["extra_files"]) == set(ms.USE_FILES)
    with zipfile.ZipFile(plain) as zf:
        assert not {f"pilot/{ha.NAME}", f"pilot/{rl.STATE_NAME}"} & set(zf.namelist())
    dest = ms.import_pilot(out, tmp_path / "b")
    there = ha.HoursAboard(dest / ha.NAME)
    assert there.hours == pytest.approx(123.0)
    again = rl.RareLine(dest / rl.STATE_NAME, there, rng=_Always())
    assert (again.said_at, again.said_hours, again.count) == (5000.0, 123.0, 1) and not again.eligible()
    old = ms.import_pilot(plain, tmp_path / "c")
    assert not (old / ha.NAME).exists() and ha.HoursAboard(old / ha.NAME).hours == 0.0


def test_with_no_memory_folder_there_is_no_rare_line_and_nothing_else_changes():
    import companion_core as ccore
    if not _GRAPH:
        _GRAPH.append(ccore.TopicGraph.load())
    real = ccore.TopicGraph.load
    ccore.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    try:
        core = ccore.CompanionCore(_Speech(), ambient_every_s=90.0,
                                   features={"manufacturer_flavour": False, "place_flavour": False})
    finally:
        ccore.TopicGraph.load = real
    assert core.rare is None and core.hours is None
    for _ in range(5):
        core.ambient_tick()
    assert core.speech.said == []
