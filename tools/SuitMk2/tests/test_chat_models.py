"""The chat model drop-down, and the check that a model fits before it is ever loaded (J, 2026-10-05).

"We should also have a drop down that ... auto-detects local models to make that easy." And: someone runs a 27B
model beside Star Citizen on max graphics on a 1080 Ti with 16 GB of RAM; it must not crash the game or overload
the machine, and no setting may get past that.

Everything here runs against fakes: a dict standing in for Ollama's list of models, numbers standing in for the
memory that is free, and a request function that fails the test if the model is ever asked when it should not be.
No model is loaded or asked, and the one real request made is to a port nobody listens on.
"""
from __future__ import annotations

import json
import time

import pytest

import chat_models as cm
import chat_talker as ct
import conversation as conv
import hardware_guard as hg
import settings as st
from _eyes_helpers import make_core
from hardware_guard import GB, FreeMemory
from test_disable_companions import _Harness, app          # noqa: F401  (app is a fixture)

GEMMA = {"name": "gemma3:4b", "size": 3338801804}           # as Ollama lists it
BIG = {"name": "gemma3:27b", "size": 17 * GB}
QWEN = {"name": "qwen3:4b", "size": 2497293931}
LINE = {"name": "realizer-elah:latest", "size": 1646572675}
NOSIZE = {"name": "mystery:latest", "size": None}
MODELS = sorted([GEMMA, BIG, QWEN, LINE, NOSIZE], key=lambda m: m["name"])
ROOMY = FreeMemory(vram_free=10 * GB, ram_free=20 * GB, commit_free=30 * GB, game_running=False)
WITH_GAME = FreeMemory(vram_free=6 * GB, ram_free=20 * GB, commit_free=30 * GB, game_running=True)


def _tags(*models):
    return lambda path, timeout=0: {"models": [dict(m, model=m["name"]) for m in models]} if path == "/api/tags" else None


# ---------------------------------------------------------------------------------------------------------------
# H. the list
# ---------------------------------------------------------------------------------------------------------------
def test_the_installed_models_are_listed_with_their_size_on_disk():
    got = cm.installed(get=_tags(QWEN, GEMMA, NOSIZE))
    assert [m["name"] for m in got] == ["gemma3:4b", "mystery:latest", "qwen3:4b"]
    assert [m["size"] for m in got] == [3338801804, None, 2497293931]


@pytest.mark.parametrize("get", [lambda path, timeout=0: None, lambda path, timeout=0: [1, 2], lambda path, timeout=0: 1 / 0])
def test_ollama_not_answering_is_none_and_never_an_exception(get):
    assert cm.installed(get=get) is None and cm.loaded(get=get) is None


def test_with_nobody_listening_the_real_request_returns_quickly_and_quietly():
    t0 = time.time()
    assert cm.installed("http://127.0.0.1:9", timeout=0.5) is None       # a port nothing listens on
    assert time.time() - t0 < 5.0


def test_the_list_round_trips_through_the_existing_ollama_helper():
    import ollama_manager as om
    fake = om.FakeOllama(models={"gemma3:4b": {}, "qwen3:4b": {}})
    try:
        got = cm.installed(fake.url)
        assert [m["name"] for m in got] == ["gemma3:4b", "qwen3:4b"]
        assert all(r[0] == "GET" and r[1] == "/api/tags" for r in fake.requests)    # a list request, nothing else
    finally:
        fake.stop()


def test_only_gemma3_4b_is_marked_tested_and_nothing_is_hidden():
    assert cm.TESTED == ("gemma3:4b",)
    assert cm.entry_label("gemma3:4b") == "gemma3:4b (tested)"
    assert cm.entry_label("gemma3:27b") == "gemma3:27b (untested)" and cm.entry_label("qwen3:4b") == "qwen3:4b (untested)"
    assert "untested" in cm.entry_label("realizer-elah:latest")
    assert all(cm.entry_label(m["name"]).startswith(m["name"]) for m in MODELS)


# ---------------------------------------------------------------------------------------------------------------
# K. does it fit
# ---------------------------------------------------------------------------------------------------------------
def test_a_model_that_fits_is_accepted_with_the_numbers():
    ok, why = hg.fits(GEMMA["size"], ROOMY)
    assert ok and why == "needs about 3.7 GB; 10 GB of video memory and 20 GB of system memory are free."
    assert hg.need_bytes(GEMMA["size"]) == int(GEMMA["size"] * 1.2) and hg.NEED_FACTOR == 1.2


def test_a_model_too_big_for_the_free_video_memory_is_refused_in_plain_words():
    ok, why = hg.fits(BIG["size"], WITH_GAME)
    assert not ok
    assert why == ("this model needs about 20 GB of video memory; 6.0 GB is free with Star Citizen running, and "
                   "1.0 GB has to stay free for the game.")


def test_the_reserve_counts_a_model_that_would_only_just_fit_is_refused():
    exact = FreeMemory(vram_free=hg.need_bytes(GEMMA["size"]) + GB - 1, ram_free=20 * GB, commit_free=30 * GB)
    assert not hg.fits(GEMMA["size"], exact)[0]
    exact.vram_free += 1
    assert hg.fits(GEMMA["size"], exact)[0]


@pytest.mark.parametrize("free, said", [
    (FreeMemory(vram_free=24 * GB, ram_free=5 * GB, commit_free=40 * GB), "5.0 GB of system memory is free"),     # RAM
    (FreeMemory(vram_free=24 * GB, ram_free=30 * GB, commit_free=4 * GB), "4.0 GB of system memory is free"),     # commit
])
def test_a_model_that_fits_the_card_but_not_the_system_memory_is_refused(free, said):
    ok, why = hg.fits(8 * GB, free)
    assert not ok and said in why and "needs about 9.6 GB" in why


@pytest.mark.parametrize("free", [None, FreeMemory(), FreeMemory(vram_free=None, ram_free=64 * GB, commit_free=64 * GB),
                                  FreeMemory(vram_free=24 * GB)])
def test_unknown_free_memory_is_never_read_as_fits(free):
    ok, why = hg.fits(BIG["size"], free)
    assert not ok and "could not be read, so the check could not run" in why and "3.5 GB" in why
    ok, why = hg.fits(GEMMA["size"], free)                   # the one measured model is small enough to be allowed
    assert ok and "the check could not run" in why
    assert not hg.fits(int(3.6 * GB), free)[0]


@pytest.mark.parametrize("size", [None, 0, -5, "big", True])
def test_unknown_model_size_is_refused(size):
    ok, why = hg.fits(size, ROOMY)
    assert not ok and "does not say how big" in why


def test_free_memory_is_read_without_raising_whatever_the_monitor_does(monkeypatch):
    import sys

    class Sample:
        vram_free_bytes, ram_avail_bytes, sc_pid = 7 * GB, 11 * GB, 4242
    good = type("hw", (), {"sample_all": staticmethod(lambda interval=1.0: Sample()),
                           "read_commit_bytes": staticmethod(lambda: (64 * GB, 9 * GB, None))})
    monkeypatch.setitem(sys.modules, "hw_monitor", good)
    f = hg.read_free_memory()
    assert (f.vram_free, f.ram_free, f.commit_free, f.game_running, f.system_free()) == (7 * GB, 11 * GB, 9 * GB, True, 9 * GB)
    bad = type("hw", (), {"sample_all": staticmethod(lambda interval=1.0: 1 / 0),
                          "read_commit_bytes": staticmethod(lambda: 1 / 0)})
    monkeypatch.setitem(sys.modules, "hw_monitor", bad)
    f = hg.read_free_memory()
    assert (f.vram_free, f.ram_free, f.commit_free, f.system_free()) == (None, None, None, None)


def test_the_monitor_reads_commit_from_windows():
    import hw_monitor
    limit, avail, reason = hw_monitor.read_commit_bytes()    # one kernel call; reads, changes nothing
    assert reason is None and isinstance(limit, int) and isinstance(avail, int) and 0 < avail <= limit


# ---------------------------------------------------------------------------------------------------------------
# K. the pick: a refused model is not saved
# ---------------------------------------------------------------------------------------------------------------
def test_a_refused_model_is_not_written_to_the_settings():
    s = {"chat": True, "chat_model": "gemma3:4b"}
    ok, why = cm.choose(s, "gemma3:27b", MODELS, WITH_GAME)
    assert not ok and s == {"chat": True, "chat_model": "gemma3:4b"}
    assert why.startswith("gemma3:27b: this model needs about 20 GB of video memory; 6.0 GB is free with Star Citizen")
    assert why.endswith("Not chosen.")


def test_a_model_that_fits_is_written_and_nothing_else_is():
    s = {"chat": False, "chat_model": "", "muted": True}
    ok, why = cm.choose(s, "gemma3:4b", MODELS, ROOMY)
    assert ok and s == {"chat": False, "chat_model": "gemma3:4b", "muted": True} and "needs about 3.7 GB" in why


@pytest.mark.parametrize("name, models, said", [
    ("gemma3:27b", None, "Ollama is not reachable"), ("llama9:70b", MODELS, "is not installed"),
    ("mystery:latest", MODELS, "does not say how big"), ("gemma3:27b", MODELS, "needs about 20 GB"),
])
def test_what_cannot_be_checked_is_not_chosen(name, models, said):
    s = {"chat": True, "chat_model": "gemma3:4b"}
    ok, why = cm.choose(s, name, models, WITH_GAME)
    assert not ok and said in why and s["chat_model"] == "gemma3:4b"


def test_choosing_no_model_clears_it_and_turns_chat_off():
    s = {"chat": True, "chat_model": "gemma3:4b"}
    assert cm.choose(s, "", MODELS, None)[0] and s == {"chat": False, "chat_model": ""}


# ---------------------------------------------------------------------------------------------------------------
# K. the same check again when the talk path is about to use the model
# ---------------------------------------------------------------------------------------------------------------
def _use(model="gemma3:4b", free=None, held=None, models=MODELS, clock=None):
    box = {"free": free or ROOMY, "held": held, "models": models, "reads": 0}

    def read():
        box["reads"] += 1
        return box["free"]
    check = cm.UseCheck(model, now=(lambda: clock[0]) if clock else time.time, tags=lambda: box["models"],
                        ps=lambda: box["held"], free=read)
    return check, box


def test_a_model_that_fitted_when_picked_is_refused_once_the_game_has_taken_the_memory():
    clock = [0.0]
    check, box = _use(clock=clock)
    assert check()[0] is True
    box["free"] = FreeMemory(vram_free=2 * GB, ram_free=20 * GB, commit_free=30 * GB, game_running=True)   # the game started
    clock[0] += cm.FIT_RECHECK_S
    ok, why = check()
    assert ok is False and "2.0 GB is free with Star Citizen running" in why
    box["free"] = ROOMY
    clock[0] += cm.FIT_RECHECK_S
    assert check()[0] is True                                # and allowed again when the memory is back


def test_the_check_is_cheap_one_reading_is_kept_for_half_a_minute():
    clock = [0.0]
    check, box = _use(clock=clock)
    for _ in range(50):
        check()
        clock[0] += 0.5
    assert box["reads"] == 1 and check.checks == 1 and cm.FIT_RECHECK_S == 30.0
    clock[0] += 10.0
    check()
    assert box["reads"] == 2


def test_a_model_ollama_already_holds_is_not_measured_against_the_memory_it_is_using():
    tight = FreeMemory(vram_free=1 * GB, ram_free=20 * GB, commit_free=30 * GB)
    check, box = _use(free=tight, held={"gemma3:4b"})
    assert check() == (True, "already loaded") and box["reads"] == 0
    other, _ = _use(free=tight, held={"qwen3:4b"})           # some OTHER model being loaded says nothing about ours
    assert other()[0] is False


@pytest.mark.parametrize("kw", [{"models": None}, {"models": []}, {"model": "mystery:latest"}, {"model": "gone:1b"}])
def test_what_cannot_be_told_at_use_time_is_a_no(kw):
    check, _ = _use(**kw)
    assert check()[0] is False


def test_a_check_that_breaks_is_a_no():
    check = cm.UseCheck("gemma3:4b", tags=lambda: 1 / 0, ps=lambda: None, free=lambda: ROOMY)
    ok, why = check()
    assert ok is False and "could not run" in why


def _never_post(url, body, timeout):
    raise AssertionError("the chat model was asked")


def _talk_spec(core, sentence="Rough day."):
    return conv.ConversationLane().handle(sentence, core.lane_state(), {})


def test_the_talk_path_does_not_load_a_model_that_no_longer_fits():
    core = make_core(None, headroom=lambda: "ROOMY")
    notes = []
    talker = ct.Talker("gemma3:27b", post=_never_post, note=notes.append,
                       fit=lambda: (False, "this model needs about 20 GB of video memory; 6.0 GB is free"))
    core.talker = talker
    assert core._talk(_talk_spec(core), "Rough day.") is None           # answered as with chat off
    assert talker.stats["unavailable"] == 1 and talker.stats["asked"] == 0
    assert notes == ["talk: gemma3:27b is not asked (this model needs about 20 GB of video memory; 6.0 GB is free); "
                     "answered as with chat off"]


def test_a_fit_check_that_raises_is_a_no_and_one_that_passes_lets_the_model_be_asked():
    core = make_core(None, headroom=lambda: "ROOMY")
    spec = _talk_spec(core)
    broken = ct.Talker("gemma3:4b", post=_never_post, fit=lambda: 1 / 0)
    assert broken.answer(spec, "Rough day.", lambda: "OK") is None and broken.stats["asked"] == 0
    asked = []

    def post(url, body, timeout):
        asked.append(body["model"])
        return {"response": "Then fly."}
    fine = ct.Talker("gemma3:4b", post=post, fit=lambda: (True, "fits"))
    assert fine.answer(spec, "Rough day.", lambda: "OK") is not None and asked[0] == "gemma3:4b"


def test_every_talker_built_from_the_settings_checks_the_fit_before_use():
    talker = ct.from_settings({"chat": True, "chat_model": "gemma3:27b"})
    assert isinstance(talker._fit, cm.UseCheck) and talker._fit.model == "gemma3:27b"
    assert ct.from_settings({"chat": False, "chat_model": "gemma3:27b"}) is None
    # and no settings key reaches it: the only things from_settings reads are the two chat switches and the thread
    import inspect
    src = inspect.getsource(ct.from_settings)
    assert sorted(set(__import__("re").findall(r's(?:\.get\(|\[)"(\w+)"', src))) == [
        "chat_model", "chat_thread_ends_after_s", "chat_thread_exchanges"]


def test_no_setting_reaches_the_fit_check():
    import inspect
    for fn in (hg.fits, hg.read_free_memory, hg.need_bytes, cm.UseCheck.__call__, cm.UseCheck.__init__):
        src = inspect.getsource(fn)
        assert "settings" not in src and "st." not in src and ".get(\"" not in src, fn.__name__
    import ast
    tree = ast.parse(inspect.getsource(cm.choose))
    touched = [n for n in ast.walk(tree) if isinstance(n, ast.Subscript) and getattr(n.value, "id", "") == "s"]
    assert touched and all(isinstance(n.ctx, ast.Store) for n in touched)        # it writes the choice; it reads no switch
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and getattr(n.value, "id", "") == "s"]
    assert not [k for k in st.DEFAULTS if any(w in k for w in ("fit", "vram", "reserve", "need_factor", "memory_"))]


# ---------------------------------------------------------------------------------------------------------------
# I. the thread length and the silence timeout are settings, with the old values as defaults
# ---------------------------------------------------------------------------------------------------------------
def test_the_thread_settings_default_to_what_the_constants_were():
    assert st.DEFAULTS["chat_thread_exchanges"] == ct.THREAD_EXCHANGES == 6
    assert st.DEFAULTS["chat_thread_ends_after_s"] == ct.THREAD_ENDS_AFTER_S == 600.0
    plain = ct.Talker("gemma3:4b")
    assert (plain.thread_exchanges, plain.thread_ends_after_s) == (6, 600.0)
    t = ct.from_settings({"chat": True, "chat_model": "gemma3:4b"})
    assert (t.thread_exchanges, t.thread_ends_after_s) == (6, 600.0)


def test_a_pilot_may_keep_a_longer_conversation_for_longer():
    core = make_core(None, headroom=lambda: "ROOMY")
    clock, prompts = [0.0], []

    def post(url, body, timeout):
        prompts.append(body["prompt"])
        return {"response": "Then fly."}
    t = ct.from_settings({"chat": True, "chat_model": "gemma3:4b", "chat_thread_exchanges": 40,
                          "chat_thread_ends_after_s": 86400.0}, post=post, now=lambda: clock[0], fit=lambda: (True, ""))
    said = [f"The thruster rattled on run number {i}, I think." for i in range(12)]
    for line in said:
        clock[0] += 1200.0                                   # twenty minutes apart: past the old ten-minute end
        assert t.answer(_talk_spec(core, line), line, lambda: "OK") is not None
    assert all(line in prompts[-1] for line in said)         # all twelve still there; the old limit was six


@pytest.mark.parametrize("saved, loaded", [({"chat_thread_exchanges": 0}, 6), ({"chat_thread_exchanges": "many"}, 6),
                                           ({"chat_thread_exchanges": 500}, 500), ({}, 6)])
def test_the_saved_thread_length_is_a_number_of_at_least_one(tmp_path, monkeypatch, saved, loaded):
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({**saved, "chat_thread_ends_after_s": -1}), encoding="utf-8")
    s = st.load()
    assert s["chat_thread_exchanges"] == (loaded if saved.get("chat_thread_exchanges") != 0 else 1)
    assert s["chat_thread_ends_after_s"] == 600.0


# ---------------------------------------------------------------------------------------------------------------
# H. the window: the drop-down and the checkbox
# ---------------------------------------------------------------------------------------------------------------
def _panel(monkeypatch, tmp_path, **settings):
    h = _Harness(monkeypatch, tmp_path, **settings)
    return h, h.panel()


def _entries(w):
    return [(w._chat_model.itemText(i), w._chat_model.itemData(i)) for i in range(w._chat_model.count())]


def test_the_list_is_asked_for_off_the_qt_thread_when_the_panel_opens(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    assert "suitmk2_chat_models" in h.threads                # a worker; the harness does not run it
    assert _entries(w) == [("(no chat model)", "")] and w._chat_status.text() == ""
    assert not w._chat_on.isChecked()


def test_with_ollama_not_running_the_list_is_empty_with_a_plain_message(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    for nothing in (None, []):
        w._fill_chat_models(nothing)
        assert _entries(w) == [("(no chat model)", "")] and w._chat_status.text() == cm.NONE_FOUND
    assert h.saved == []


def test_every_installed_model_is_offered_and_the_untested_ones_say_so(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, chat_model="gemma3:4b")
    w._fill_chat_models(MODELS)
    assert _entries(w) == [("(no chat model)", "")] + [(cm.entry_label(m["name"]), m["name"]) for m in MODELS]
    assert w._chat_model.currentData() == "gemma3:4b" and w._chat_model.currentText() == "gemma3:4b (tested)"
    assert sum("(tested)" in text for text, _ in _entries(w)) == 1 and h.saved == []


def test_a_saved_model_ollama_no_longer_has_is_shown_as_such_and_the_setting_is_left_alone(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, chat=True, chat_model="llama9:70b")
    w._fill_chat_models([GEMMA])
    assert w._chat_model.currentText() == "llama9:70b (saved; not found in Ollama)"
    assert "is not installed in Ollama any more" in w._chat_status.text() and h.saved == []


def test_a_saved_model_is_not_called_missing_before_ollama_has_answered(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, chat_model="gemma3:4b")
    assert w._chat_model.currentText() == "gemma3:4b (saved)"            # the list has not come back yet
    w._fill_chat_models(None)                                            # Ollama did not answer
    assert w._chat_model.currentText() == "gemma3:4b (saved)" and w._chat_status.text() == cm.NONE_FOUND
    w._fill_chat_models([QWEN])                                          # it answered, and does not have it
    assert w._chat_model.currentText() == "gemma3:4b (saved; not found in Ollama)"


def test_picking_a_model_that_does_not_fit_is_refused_shown_and_not_saved(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, chat_model="gemma3:4b")
    w._fill_chat_models(MODELS)
    w._chat_model.setCurrentIndex(w._chat_model.findData("gemma3:27b"))
    w._apply_chat_pick("gemma3:27b", WITH_GAME)
    assert h.saved == [] and w.s["chat_model"] == "gemma3:4b"
    assert w._chat_model.currentData() == "gemma3:4b"        # the drop-down is back on what is saved
    assert "needs about 20 GB of video memory; 6.0 GB is free with Star Citizen running" in w._chat_status.text()


def test_picking_a_model_that_fits_saves_it(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    w._fill_chat_models(MODELS)
    w._apply_chat_pick("qwen3:4b", ROOMY)
    assert h.saved[-1]["chat_model"] == "qwen3:4b" and h.saved[-1]["chat"] is False
    assert "needs about 2.8 GB" in w._chat_status.text()


def test_the_memory_is_read_off_the_qt_thread_when_a_model_is_picked(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path)
    w._fill_chat_models(MODELS)
    monkeypatch.setattr(hg, "read_free_memory", lambda: 1 / 0)        # were it called here, the test would raise
    del h.threads[:]
    w._pick_chat_model(w._chat_model.findData("qwen3:4b"))
    assert h.threads == ["suitmk2_chat_fit"] and h.saved == []
    assert w._chat_status.text() == "checking that it fits..."


@pytest.mark.parametrize("settings, models, why", [
    ({}, MODELS, "Pick a chat model first."),
    ({"chat_model": "gemma3:4b"}, None, "Ollama is not reachable, so free talk cannot be turned on."),
    ({"chat_model": "llama9:70b"}, MODELS, "llama9:70b is not installed in Ollama any more. Pick another model."),
])
def test_the_checkbox_says_why_when_it_cannot_be_on(app, monkeypatch, tmp_path, settings, models, why):
    h, w = _panel(monkeypatch, tmp_path, **settings)
    w._fill_chat_models(models)
    w._chat_on.setChecked(True)
    assert not w._chat_on.isChecked() and w._chat_status.text() == why
    assert h.saved == []                                     # chat was not turned on


def test_the_checkbox_turns_chat_on_and_off_and_a_running_core_gets_its_talker_at_once(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, chat_model="gemma3:4b")
    w._fill_chat_models(MODELS)
    core = type("Core", (), {"talker": None, "_note": lambda self, m: None})()
    w.core = core
    w._chat_on.setChecked(True)
    assert h.saved[-1]["chat"] is True and h.saved[-1]["chat_model"] == "gemma3:4b"
    assert isinstance(core.talker, ct.Talker) and core.talker.model == "gemma3:4b"
    assert isinstance(core.talker._fit, cm.UseCheck)         # and that talker checks the fit before every use
    w._chat_on.setChecked(False)
    assert h.saved[-1]["chat"] is False and core.talker is None
    w.core = None


def test_choosing_no_model_unticks_the_box(app, monkeypatch, tmp_path):
    h, w = _panel(monkeypatch, tmp_path, chat=True, chat_model="gemma3:4b")
    w._fill_chat_models(MODELS)
    assert w._chat_on.isChecked()
    w._apply_chat_pick("", None)
    assert not w._chat_on.isChecked() and h.saved[-1]["chat"] is False and h.saved[-1]["chat_model"] == ""
