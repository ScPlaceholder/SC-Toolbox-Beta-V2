"""Gemma is offered, never assumed; and the one setup click also fetches the Assistant's small brain (J, 2026-10-06).

"I'd add a pop up option for Gemma and explain that it's a heavier model and use if you plan to talk to the
companions." And: Gemma can be installed when the two small Qwen models get installed, the 0.5b one being the
Toolbox Assistant's.

Nothing here downloads anything or talks to a real Ollama: the server is ollama_manager.FakeOllama, which writes
down every request, and the free memory is a number handed in. Three layers are tested:

  A. chat_models: the words of the offer, the download, the choice, the removal.
  B. ui/setup_panel.py, the real panel on Qt's offscreen platform against the fake server: what one click fetches.
  C. ui/suit_window.py, the real Suit panel in the harness of test_disable_companions.py: the button beside the
     free-talk controls, and what a yes or a no does to the settings.
"""
from __future__ import annotations

import json
import time

import pytest

import chat_models as cm
import hardware_guard as hg
import model_provision as mp
import ollama_manager as om
from gguf_stitch import GGUFFile, write_test_gguf
from hardware_guard import GB, FreeMemory
from test_disable_companions import _Harness, app          # noqa: F401  (app is a fixture)
from ui import chat_offer
from ui import setup_panel as sp

GEMMA = {"name": "gemma3:4b", "size": 3338801804}           # as Ollama lists it
QWEN = {"name": "qwen3:4b", "size": 2497293931}
ROOMY = FreeMemory(vram_free=10 * GB, ram_free=20 * GB, commit_free=30 * GB, game_running=False)
TIGHT = FreeMemory(vram_free=3 * GB, ram_free=20 * GB, commit_free=30 * GB, game_running=True)
JARGON = ("ollama", "gguf", "pull", "model")


def _pulls(fo) -> list:
    return [q[2].get("model") for q in fo.requests if len(q) > 2 and q[0] == "POST" and q[1] == "/api/pull"]


@pytest.fixture
def fake():
    made = []

    def make(*names):
        fo = om.FakeOllama(models={n: {"from_blob": "sha256:" + "c" * 64} for n in names})
        made.append(fo)
        return fo
    yield make
    for fo in made:
        fo.stop()


# ---------------------------------------------------------------------------------------------------------------
# A. the words, the download, the choice
# ---------------------------------------------------------------------------------------------------------------
def test_the_offer_says_the_size_that_it_is_heavier_and_that_it_is_only_for_talking():
    o = cm.offer([QWEN], ROOMY)
    assert o.kind == "download" and o.yes == "Download Gemma (about 3.3 GB)" and o.no == "Not now"
    assert "about 3.3 GB" in o.text and "heavier model" in o.text
    assert "only worth getting if you plan to talk with the companions" in o.text
    assert "normal comments work without it" in o.text
    assert "add it later" in o.text and "remove it again" in o.text


def test_the_numbers_in_the_offer_are_the_codes_own():
    o = cm.offer([QWEN], ROOMY)
    need = hg.need_bytes(cm.OFFER_DOWNLOAD_BYTES)
    assert cm.OFFER_DOWNLOAD_BYTES == GEMMA["size"] and cm.download_text() == "3.3 GB"
    assert f"about {hg._gb(need)} while it is in use" in o.text                       # 3.7 GB, as fits() says it
    assert f"when about {hg._gb(need + hg.VRAM_RESERVE_GB * GB)} is free" in o.text   # 4.7 GB
    assert f"{hg._gb(hg.VRAM_RESERVE_GB * GB)} stays free for the game" in o.text


def test_the_chat_model_and_the_vision_model_are_one_download_and_the_offer_says_so():
    assert cm.OFFER == cm.TESTED[0] == mp.VISION_TAG == "gemma3:4b" and cm.serves_eyes()
    assert "one download that serves both" in cm.offer([], ROOMY).text


def test_a_pc_with_no_room_is_told_so_and_is_offered_nothing():
    o = cm.offer([QWEN], TIGHT)
    assert o.kind == "tight" and o.yes == "" and o.no == "Close"
    assert "does not have the room for it" in o.text and "3.0 GB is free with Star Citizen running" in o.text
    assert "Nothing was downloaded" in o.text and "normal comments work without it" in o.text
    assert cm.offer([GEMMA], TIGHT).kind == "tight"          # on disk already, and still not offered for use


def test_ollama_not_answering_withholds_the_offer_from_the_tab_but_not_from_the_setup_that_installs_it():
    o = cm.offer(None, ROOMY)
    assert o.kind == "unreachable" and o.yes == "" and "Ollama is not running" in o.text
    assert cm.offer(None, ROOMY, setup=True).kind == "download"


def test_already_on_this_pc_it_is_only_offered_for_use():
    o = cm.offer([GEMMA, QWEN], ROOMY)
    assert o.kind == "select" and o.yes == "Use Gemma for talking"
    assert "already on this PC, so there is nothing to download" in o.text
    assert "only worth using if you plan to talk with the companions" in o.text


def test_a_yes_asks_ollama_for_gemma3_4b_and_nothing_else_exactly_once(fake):
    fo = fake("qwen2.5:1.5b")
    seen = []
    assert cm.fetch(fo.url, progress=lambda *a: seen.append(a)) == (True, "pulled")
    assert _pulls(fo) == ["gemma3:4b"] and "gemma3:4b" in fo.models
    assert seen and seen[-1][3] == "Downloading Gemma: done"


def test_already_installed_nothing_is_downloaded(fake):
    fo = fake("gemma3:4b")
    assert cm.offer(cm.installed(fo.url), ROOMY).kind == "select"
    assert cm.fetch(fo.url) == (True, "present") and _pulls(fo) == []


def test_a_failed_download_is_one_plain_sentence_and_no_exception(fake):
    fo = fake()
    fo.pull_script["gemma3:4b"] = [{"status": "pulling manifest"}, {"error": "max retries exceeded"}]
    ok, said = cm.fetch(fo.url)
    assert not ok and said.startswith("Gemma could not be downloaded (") and "max retries exceeded" in said
    assert "Nothing was changed" in said and "normal comments work without it" in said
    assert "Error" not in said and "Traceback" not in said and "gemma3:4b" not in fo.models
    ok, said = cm.fetch("http://127.0.0.1:9")                # nobody listening
    assert not ok and said.startswith("Gemma could not be downloaded (")


def test_taking_it_chooses_the_model_and_leaves_the_free_talk_switch_alone():
    s = {"chat": False, "chat_model": "", "muted": True}
    ok, said = cm.take(s, [GEMMA], ROOMY)
    assert ok and s == {"chat": False, "chat_model": "gemma3:4b", "muted": True}
    assert said == 'Gemma is chosen for talking. Free talk is still off: tick "Free talk (chat model)" to start talking.'
    on = {"chat": True, "chat_model": "qwen3:4b"}
    assert cm.take(on, [GEMMA, QWEN], ROOMY) == (True, "Gemma is now the model for talking, and free talk is on.")
    assert on == {"chat": True, "chat_model": "gemma3:4b"}


def test_taking_it_goes_through_the_same_fit_check_and_a_refusal_changes_nothing():
    s = {"chat": False, "chat_model": "qwen3:4b"}
    ok, said = cm.take(s, [GEMMA], TIGHT)
    assert not ok and s == {"chat": False, "chat_model": "qwen3:4b"}
    assert said.startswith("Gemma is on this PC but was not chosen for talking.") and "3.0 GB is free" in said


def test_removing_it_deletes_that_one_model_and_turns_free_talk_off(fake):
    fo = fake("gemma3:4b", "qwen3:4b")
    s = {"chat": True, "chat_model": "gemma3:4b"}
    assert cm.remove(s, fo.url) == (True, "Gemma was removed from this PC. Free talk is off.")
    assert set(fo.models) == {"qwen3:4b"} and s == {"chat": False, "chat_model": ""}
    other = {"chat": True, "chat_model": "qwen3:4b"}
    ok, said = cm.remove(other, fo.url)                      # it is no longer there
    assert not ok and said.startswith("Gemma could not be removed (") and other["chat_model"] == "qwen3:4b"
    assert "3.3 GB" in cm.remove_question() and "closer look at the screen" in cm.remove_question()


# ---- the pop-up itself ---------------------------------------------------------------------------------------------
def test_the_pop_up_shows_the_words_and_its_default_is_no(app):
    o = cm.offer([QWEN], ROOMY)
    box = chat_offer.build(None, o)
    assert box.windowTitle() == "Talk with Elah and Montaigne?"
    assert box.text() == o.headline and box.informativeText() == o.body
    assert box.defaultButton() is box.no_button and box.escapeButton() is box.no_button
    assert box.no_button.text() == "Not now" and box.yes_button.text() == "Download Gemma (about 3.3 GB)"
    assert not chat_offer.said_yes(box)                      # nothing pressed is not a yes
    box.no_button.click()
    assert not chat_offer.said_yes(box)
    box2 = chat_offer.build(None, o)
    box2.yes_button.click()
    assert chat_offer.said_yes(box2)


def test_with_nothing_to_offer_the_pop_up_has_one_button_and_cannot_say_yes(app):
    box = chat_offer.build(None, cm.offer([], TIGHT))
    assert box.yes_button is None and [b.text() for b in box.buttons()] == ["Close"]
    box.no_button.click()
    assert not chat_offer.said_yes(box)


# ---------------------------------------------------------------------------------------------------------------
# B. the first-run panel: what one click fetches
# ---------------------------------------------------------------------------------------------------------------
class _World:
    """A fake Ollama that already has the base model, and the two character deltas on disk, as the panel's own
    selftest builds them."""

    def __init__(self, tmp_path, monkeypatch, *extra):
        a = bytes(range(256)) * 2
        self.base = tmp_path / ("sha256-" + "b" * 64)
        write_test_gguf(self.base, {"general.architecture": "qwen2"},
                        [("tok.weight", (128,), 0, a), ("blk.0.attn_q.weight", (256, 2), 12, b"\x11" * 288)])
        self.models = tmp_path / "models"
        self.models.mkdir()
        for spk in mp.SPEAKERS:
            write_test_gguf(self.models / f"{spk}.delta.gguf",
                            {"suitmk2.delta.speaker": spk,
                             "suitmk2.delta.base_fingerprint": GGUFFile(self.base).fingerprint()},
                            [("blk.0.attn_q.weight", (256, 2), 8, bytes([len(spk)]) * 544)])
        have = {mp.BASE_TAG: {"from_blob": "sha256:" + "b" * 64, "from_path": str(self.base)}}
        have.update({n: {"from_blob": "sha256:" + "c" * 64} for n in extra})
        self.fo = om.FakeOllama(models=have)
        self.kw = {"models_dir": self.models, "state_path": tmp_path / "state.json"}
        self.asked, self.ready, self.failed, self.assistant_failed, self.chat = [], [], [], [], []
        monkeypatch.setattr(hg, "read_free_memory", lambda *a, **k: ROOMY)

    def panel(self, answer=None, **kw):
        def ask(offer):
            self.asked.append(offer)
            return answer
        kw.setdefault("assistant_tag", mp.ASSISTANT_TAG)
        p = sp.SetupPanel(manager=om.OllamaManager(self.fo.url), provisioner_kw=self.kw,
                          ask_chat=None if answer is None else ask, **kw)
        p.ready.connect(lambda: self.ready.append(1))
        p.failed.connect(self.failed.append)
        p.assistant_failed.connect(self.assistant_failed.append)
        p.chat_fetched.connect(lambda ok, said: self.chat.append((ok, said)))
        p.show()
        return p


def _pump(app, cond, secs=30.0):                             # noqa: F811
    t0 = time.time()
    while time.time() - t0 < secs and not cond():
        app.processEvents()
        time.sleep(0.02)
    return cond()


def _visible_text(p) -> str:
    return (p.status_lbl.text() + p.detail.text() + p.button.text()).lower()


@pytest.fixture
def world(tmp_path, monkeypatch):
    made = []

    def make(*extra):
        w = _World(tmp_path, monkeypatch, *extra)
        made.append(w)
        return w
    yield make
    for w in made:
        w.fo.stop()


def test_the_default_is_no_a_click_and_a_no_download_no_gemma(app, world):
    w = world()
    p = w.panel(answer=False)
    assert _pump(app, p.button.isEnabled) and p.button.text() == "Set up Elah and Montaigne (about 2.3 GB)"
    assert p.chat_wanted is False
    p.button.click()
    assert _pump(app, lambda: bool(w.ready))
    assert [o.kind for o in w.asked] == ["download"]         # asked once, at the click
    assert "gemma3:4b" not in _pulls(w.fo) and "gemma3:4b" not in w.fo.models and w.chat == []
    assert {"suitmk2-elah", "suitmk2-montaigne"} <= set(w.fo.models)


def test_setup_with_no_click_asks_nobody_and_downloads_no_gemma(app, world):
    w = world()
    p = w.panel(answer=True, auto_start=True)                # even an asker that would say yes is not asked
    assert _pump(app, lambda: bool(w.ready)) and not p.isVisible()
    assert w.asked == [] and "gemma3:4b" not in _pulls(w.fo) and w.chat == []


def test_a_yes_downloads_gemma_exactly_once_after_the_setup_and_reports_it(app, world):
    w = world()
    p = w.panel(answer=True)
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready) and bool(w.chat))
    assert _pulls(w.fo).count("gemma3:4b") == 1 and w.chat == [(True, "pulled")]
    assert _pulls(w.fo) == [mp.ASSISTANT_TAG, "gemma3:4b"]   # the Assistant's brain with the setup; Gemma after it
    assert p.chat_wanted is False and w.failed == []


def test_anything_but_a_clear_yes_from_the_pop_up_is_a_no(app, world):
    w = world()
    p = w.panel(answer="yes please")                         # truthy, and not True
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready)) and "gemma3:4b" not in _pulls(w.fo)


def test_gemma_already_here_is_offered_for_use_and_a_yes_downloads_nothing(app, world):
    w = world("gemma3:4b")
    p = w.panel(answer=True)
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready) and bool(w.chat))
    assert [o.kind for o in w.asked] == ["select"] and "gemma3:4b" not in _pulls(w.fo)
    assert w.chat == [(True, "present")]


def test_a_failed_gemma_download_is_not_a_failed_setup(app, world):
    w = world()
    w.fo.pull_script["gemma3:4b"] = [{"status": "pulling manifest"}, {"error": "max retries exceeded"}]
    p = w.panel(answer=True)
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready) and bool(w.chat))
    ok, said = w.chat[0]
    assert not ok and said.startswith("Gemma could not be downloaded (") and w.failed == []
    assert {"suitmk2-elah", "suitmk2-montaigne"} <= set(w.fo.models) and not p.isVisible()


def test_one_click_fetches_the_assistants_small_brain_exactly_once_when_it_is_absent(app, world):
    w = world()
    p = w.panel()
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready)) and not p.isVisible()
    assert _pulls(w.fo) == ["qwen2.5:0.5b"] and "qwen2.5:0.5b" in w.fo.models
    assert p._result["assistant"] == "pulled" and w.assistant_failed == []


def test_the_assistants_small_brain_is_not_asked_for_when_it_is_already_here(app, world):
    w = world("qwen2.5:0.5b")
    p = w.panel()
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready)) and not p.isVisible()
    assert _pulls(w.fo) == [] and p._result["assistant"] == "present"


def test_without_a_tag_the_setup_fetches_nothing_for_the_assistant(app, world):
    w = world()
    p = w.panel(assistant_tag=None)
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready)) and _pulls(w.fo) == [] and "assistant" not in p._result


def test_a_failed_fetch_of_the_small_brain_leaves_the_companions_set_up_and_says_so_plainly(app, world):
    w = world()
    w.fo.pull_script["qwen2.5:0.5b"] = [{"status": "pulling manifest"}, {"error": "max retries exceeded"}]
    p = w.panel()
    _pump(app, p.button.isEnabled)
    p.button.click()
    assert _pump(app, lambda: bool(w.ready) and bool(w.assistant_failed))
    assert {"suitmk2-elah", "suitmk2-montaigne"} <= set(w.fo.models) and w.failed == []
    assert w.assistant_failed == [sp.ASSISTANT_FAILED]
    assert _pump(app, lambda: p.status_lbl.text() == sp.ASSISTANT_FAILED)
    assert "answers in its simpler mode until it is fetched" in p.status_lbl.text()
    assert "Elah and Montaigne are set up" in p.status_lbl.text()
    assert p.isVisible() and p.button.isEnabled() and p.button.text() == "Get the Assistant's brain (about 0.4 GB)"
    assert "max retries exceeded" in p.detail.text()         # the raw reason, small, as a failed setup shows its own
    assert not any(j in (p.status_lbl.text() + p.button.text()).lower() for j in JARGON)
    # the button then fetches that one thing and nothing else
    del w.fo.pull_script["qwen2.5:0.5b"]
    created = len(w.fo.created)
    p.button.click()
    assert _pump(app, lambda: not p.isVisible())
    assert _pulls(w.fo) == ["qwen2.5:0.5b", "qwen2.5:0.5b"] and len(w.fo.created) == created
    assert "qwen2.5:0.5b" in w.fo.models


def test_companions_set_up_before_this_existed_get_a_way_to_fetch_the_small_brain_and_nothing_else_is_built(
        app, tmp_path, monkeypatch):
    fr = om.FakeOllama(models={"realizer-elah": {"from_blob": "sha256:" + "d" * 64},
                               "realizer-montaigne": {"from_blob": "sha256:" + "e" * 64}})
    try:
        asked, ready = [], []
        p = sp.SetupPanel(manager=om.OllamaManager(fr.url), provisioner_kw={"state_path": tmp_path / "s.json"},
                          ready_prefixes=("suitmk2-", "realizer-"), assistant_tag=mp.ASSISTANT_TAG,
                          ask_chat=lambda o: asked.append(o) or True)
        p.ready.connect(lambda: ready.append(1))
        p.show()
        assert _pump(app, lambda: bool(ready))               # the companions ARE ready, and the window is told so
        assert p.isVisible() and p.assistant_only and p.status_lbl.text() == sp.ASSISTANT_NEEDED
        assert "about 0.4 GB" in p.status_lbl.text() and "simpler mode" in p.status_lbl.text()
        assert not any(j in _visible_text(p) for j in JARGON)
        p.button.click()
        assert _pump(app, lambda: not p.isVisible())
        assert _pulls(fr) == ["qwen2.5:0.5b"] and fr.created == [] and asked == []   # no Gemma question here
        assert not any(n.startswith("suitmk2-") for n in fr.models)
    finally:
        fr.stop()


def test_the_size_on_the_button_is_the_old_total_plus_the_small_brain():
    assert mp.ASSISTANT_TAG == "qwen2.5:0.5b" and mp.ASSISTANT_BYTES == 397_821_319
    assert sp.ASSISTANT_SIZE == "0.4 GB" and round(1.9 + mp.ASSISTANT_BYTES / 1e9, 1) == 2.3
    assert sp.BUTTON_TEXT == "Set up Elah and Montaigne (about 2.3 GB)"


@pytest.mark.parametrize("cfg,want", [
    (None, "qwen2.5:0.5b"),                                                     # no file: the Assistant's defaults
    ({}, "qwen2.5:0.5b"),
    ({"model": "qwen2.5:0.5b", "base_url": "http://localhost:11434/v1", "mode": "router+llm"}, "qwen2.5:0.5b"),
    ({"mode": "router"}, None),                                                 # it asks no model at all
    ({"model": "qwen2.5:3b"}, None),                                            # the player's own choice
    ({"provider": "anthropic", "model": "qwen2.5:0.5b"}, None),
    ({"base_url": "https://api.openai.com/v1"}, None),
    ({"base_url": "http://127.0.0.1:1234/v1"}, None),                           # another local service
])
def test_the_small_brain_is_only_fetched_when_the_assistant_would_ask_this_pc_for_it(tmp_path, cfg, want):
    path = tmp_path / "assistant_llm.json"
    if cfg is not None:
        path.write_text(json.dumps(cfg), encoding="utf-8")
    assert mp.assistant_tag_wanted(path) == want
    path.write_text("not json", encoding="utf-8")
    assert mp.assistant_tag_wanted(path) == "qwen2.5:0.5b"


# ---------------------------------------------------------------------------------------------------------------
# C. the Suit Mk2 tab: the button beside the free-talk controls
# ---------------------------------------------------------------------------------------------------------------
def _tab(monkeypatch, tmp_path, answer, **settings):
    monkeypatch.setattr(mp, "ASSISTANT_CONFIG", tmp_path / "no_such_assistant_llm.json")
    h = _Harness(monkeypatch, tmp_path, **settings)
    w = h.panel()
    asked = []
    monkeypatch.setattr(h.mod.chat_offer, "ask", lambda parent, offer: asked.append(offer) or answer)
    del h.threads[:]
    return h, w, asked


def test_the_tab_hands_the_setup_panel_the_question_and_the_assistants_model(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True)
    assert w.setup is not None and w.setup.assistant_tag == "qwen2.5:0.5b" and w.setup.chat_wanted is False
    assert w.setup.ask_chat(cm.offer([], ROOMY, setup=True)) is True and [o.kind for o in asked] == ["download"]
    assert w._chat_on.text() == cm.SWITCH_LABEL              # the sentence that names the checkbox names this one


def test_the_button_is_hidden_until_ollama_answers_and_then_says_what_it_would_do(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, False)
    assert w._gemma_btn.isHidden()
    w._fill_chat_models(None)
    assert w._gemma_btn.isHidden()
    w._fill_chat_models([QWEN])
    assert not w._gemma_btn.isHidden() and w._gemma_btn.text() == "Get Gemma for talking..."
    w._fill_chat_models([GEMMA, QWEN])
    assert w._gemma_btn.text() == "Use Gemma for talking..."
    w.s["chat_model"] = "gemma3:4b"
    w._fill_chat_models([GEMMA, QWEN])
    assert w._gemma_btn.text() == "Remove Gemma..."
    w._gemma_btn.click()
    assert h.threads == ["suitmk2_gemma_probe"] and h.saved == []      # the PC is read off the Qt thread first


def test_a_no_in_the_tab_downloads_nothing_and_changes_nothing(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, False)
    w._gemma_decide([QWEN], ROOMY)
    assert [o.kind for o in asked] == ["download"]
    assert h.threads == [] and h.saved == [] and w.s["chat_model"] == "" and w._gemma_cancel is None


def test_a_yes_in_the_tab_starts_the_download_and_chooses_nothing_until_it_is_here(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True)
    w._gemma_decide([QWEN], ROOMY)
    assert h.threads == ["suitmk2_gemma_fetch"] and h.saved == [] and w.s["chat_model"] == ""
    assert w._gemma_btn.text() == "Stop the download" and w._chat_status.text() == "Downloading Gemma: 0%"
    w._gemma_btn.click()                                     # the same button stops it
    assert w._gemma_cancel.is_set() and h.threads == ["suitmk2_gemma_fetch"]


def test_a_failed_download_says_its_sentence_and_leaves_the_settings_as_they_were(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True, chat_model="qwen3:4b")
    w._fill_chat_models([QWEN])
    w._gemma_decide([QWEN], ROOMY)
    del h.threads[:]
    before = dict(w.s)
    said = "Gemma could not be downloaded (gemma3:4b: max retries exceeded). Nothing was changed."
    w._gemma_fetched(False, said)
    assert w._chat_status.text() == said and h.saved == [] and w.s == before and h.threads == []
    assert w._chat_model.currentData() == "qwen3:4b" and not w._chat_on.isChecked()
    assert w._gemma_cancel is None and w._gemma_btn.text() == "Get Gemma for talking..."


def test_once_it_is_here_it_is_chosen_saved_and_free_talk_is_left_off(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True)
    w._gemma_fetched(True, "pulled")
    assert h.threads == ["suitmk2_gemma_take"] and h.saved == []       # the PC is read again before choosing
    w._gemma_take([GEMMA, QWEN], ROOMY)
    assert h.saved[-1]["chat_model"] == "gemma3:4b" and h.saved[-1]["chat"] is False
    assert not w._chat_on.isChecked() and w._chat_model.currentData() == "gemma3:4b"
    assert w._chat_status.text() == ('Gemma is chosen for talking. Free talk is still off: tick "Free talk (chat '
                                     'model)" to start talking.')
    assert w._gemma_btn.text() == "Remove Gemma..."


def test_already_installed_a_yes_in_the_tab_only_chooses_it(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True)
    w._gemma_decide([GEMMA, QWEN], ROOMY)
    assert [o.kind for o in asked] == ["select"] and h.threads == []   # no download was started
    assert h.saved[-1]["chat_model"] == "gemma3:4b" and h.saved[-1]["chat"] is False and not w._chat_on.isChecked()


def test_a_pc_with_no_room_is_told_and_nothing_follows_even_from_a_yes(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True)          # an asker that says yes to a box with no yes button
    w._gemma_decide([QWEN], TIGHT)
    assert [o.kind for o in asked] == ["tight"] and h.threads == [] and h.saved == []
    w._gemma_decide([GEMMA], TIGHT)
    assert [o.kind for o in asked] == ["tight", "tight"] and h.threads == [] and h.saved == []


def test_gemma_arriving_on_a_pc_that_has_no_room_by_then_is_not_chosen(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True)
    w._gemma_take([GEMMA], TIGHT)
    assert h.saved == [] and w.s["chat_model"] == ""
    assert w._chat_status.text().startswith("Gemma is on this PC but was not chosen for talking.")


def test_removing_it_clears_the_choice_and_unticks_free_talk(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True, chat=True, chat_model="gemma3:4b")
    w._fill_chat_models([GEMMA, QWEN])
    assert w._chat_on.isChecked()
    w._gemma_removed(False, "Gemma could not be removed (x). Nothing was changed.")
    assert h.saved == [] and w._chat_on.isChecked() and w.s["chat_model"] == "gemma3:4b"
    w._gemma_removed(True, "Gemma was removed from this PC. Free talk is off.")
    assert h.saved[-1]["chat_model"] == "" and h.saved[-1]["chat"] is False and not w._chat_on.isChecked()
    assert w._gemma_btn.text() == "Get Gemma for talking..." and w._chat_model.findData("gemma3:4b") < 0


def test_removing_asks_first_and_a_no_removes_nothing(app, monkeypatch, tmp_path):
    h, w, asked = _tab(monkeypatch, tmp_path, True, chat_model="gemma3:4b")
    said = []
    monkeypatch.setattr(h.mod.chat_offer, "confirm", lambda parent, title, text, yes: said.append((title, yes)) or False)
    w._gemma_decide([GEMMA, QWEN], ROOMY)                   # installed and chosen: the button is "Remove Gemma..."
    assert said == [("Remove Gemma?", "Remove Gemma")] and asked == []
    assert h.threads == [] and h.saved == [] and w.s["chat_model"] == "gemma3:4b"
    monkeypatch.setattr(h.mod.chat_offer, "confirm", lambda parent, title, text, yes: True)
    w._gemma_decide([GEMMA, QWEN], ROOMY)
    assert h.threads == ["suitmk2_gemma_remove"] and h.saved == []     # the settings change when it is gone


def test_the_pop_up_is_readable_under_the_toolbox_theme(app):
    box = chat_offer.build(None, cm.offer([QWEN], ROOMY))
    assert "QMessageBox { background:" in box.styleSheet() and "QMessageBox QLabel { color:" in box.styleSheet()
