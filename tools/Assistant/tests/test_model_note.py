"""The Assistant says so when it is answering without its small local model (J, 2026-10-06).

Nothing in the Assistant downloads qwen2.5:0.5b; the first-run setup on the Suit Mk2 tab does. Until it is on the PC
the Assistant answers in router mode, which used to be silent: the status line read "ready - router+llm /
qwen2.5:0.5b" either way. No request leaves these tests: the answer of Ollama's tag list is handed in.
"""
from __future__ import annotations

import types

import pytest

from assistant import panel
from assistant.config import LLMConfig, NOTE_MISSING, NOTE_UNREACHABLE, local_model_state, model_note


def _cfg(**kw) -> LLMConfig:
    cfg = LLMConfig()
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _tags(*names):
    asked = []

    def get(root, timeout):
        asked.append(root)
        return {"models": [{"name": n} for n in names]}
    get.asked = asked
    return get


def test_the_default_model_is_the_one_the_setup_fetches():
    assert LLMConfig().model == "qwen2.5:0.5b" and LLMConfig().mode == "router+llm"


def test_with_the_model_on_this_pc_nothing_is_said():
    assert model_note(_cfg(), _tags("qwen2.5:0.5b", "gemma3:4b")) == ""
    assert model_note(_cfg(model="mine"), _tags("mine:latest")) == ""


def test_without_it_the_status_line_says_simpler_mode_and_where_to_get_it():
    get = _tags("qwen2.5:1.5b", "suitmk2-elah:latest")
    note = model_note(_cfg(), get)
    assert local_model_state(_cfg(), get) == "missing" and note == NOTE_MISSING.format(model="qwen2.5:0.5b")
    assert "simpler mode" in note and "is not on this PC yet" in note and "Suit Mk2 tab" in note
    assert get.asked == ["http://127.0.0.1:11434"] * 2


def test_ollama_not_answering_is_said_too():
    assert local_model_state(_cfg(), lambda root, timeout: None) == "unreachable"
    assert model_note(_cfg(), lambda root, timeout: None) == NOTE_UNREACHABLE and "simpler mode" in NOTE_UNREACHABLE


@pytest.mark.parametrize("kw", [
    {"mode": "router"},                                      # it asks no model at all
    {"provider": "anthropic"},
    {"base_url": "https://api.openai.com/v1"},               # the player's own service: not ours to check
    {"base_url": "http://127.0.0.1:1234/v1"},
    {"model": ""},
])
def test_any_other_setup_is_not_asked_about_and_nothing_is_said(kw):
    def never(root, timeout):
        raise AssertionError("no request may be made for this config")
    assert model_note(_cfg(**kw), never) == ""


def test_with_nobody_listening_the_real_request_is_quick_and_quiet():
    from assistant import config
    assert config._ollama_tags("http://127.0.0.1:9", 1.0) is None


class _Label:
    def __init__(self):
        self.text = ""

    def setText(self, t):
        self.text = t

    def setToolTip(self, t):
        pass


def _stub_panel():
    """Just what _on_reply and _check_model touch, with no window and no thread."""
    p = types.SimpleNamespace(_lbl_reply=_Label(), _lbl_status=_Label(), _ptt_turn=False, checks=[])
    p._set_status = p._lbl_status.setText
    p._check_model = lambda: p.checks.append(1)
    p._sync_in_game = lambda: None
    return p


def test_after_a_turn_the_status_line_goes_back_to_the_note_and_not_to_a_bare_ready():
    p = _stub_panel()
    p._ready_text = NOTE_MISSING.format(model="qwen2.5:0.5b")
    panel._AssistantBody._on_reply(p, "Forty SCU.")
    assert p._lbl_status.text == p._ready_text and "simpler mode" in p._lbl_status.text
    assert p.checks == [1]                                   # and it looks again: the setup may have fetched it


def test_with_the_model_there_the_status_line_is_ready_and_nothing_is_asked_again():
    p = _stub_panel()
    p._ready_text = "ready"
    panel._AssistantBody._on_reply(p, "Forty SCU.")
    assert p._lbl_status.text == "ready" and p.checks == []
    q = _stub_panel()                                        # a panel built before the note existed
    panel._AssistantBody._on_reply(q, "Forty SCU.")
    assert q._lbl_status.text == "ready" and q.checks == []


def test_the_check_runs_off_the_gui_thread_and_puts_the_note_on_the_status_line(monkeypatch):
    started, said = [], []

    class Thread:
        def __init__(self, target=None, name="", daemon=None):
            self.target, self.name = target, name

        def start(self):
            started.append(self.name)
            self.target()                                    # run it here, so its effect is seen

    monkeypatch.setattr(panel, "threading", types.SimpleNamespace(Thread=Thread))
    monkeypatch.setattr(panel.LLMConfig, "load", classmethod(lambda cls: LLMConfig()))
    monkeypatch.setattr(panel, "model_note", lambda cfg: NOTE_MISSING.format(model=cfg.model))
    p = types.SimpleNamespace(statusRequested=types.SimpleNamespace(emit=said.append), _ready_text="ready")
    panel._AssistantBody._check_model(p)
    assert started == ["assistant_model_check"] and said == [NOTE_MISSING.format(model="qwen2.5:0.5b")]
    assert p._ready_text == said[0]
    monkeypatch.setattr(panel, "model_note", lambda cfg: "")
    panel._AssistantBody._check_model(p)
    assert p._ready_text == "ready" and len(said) == 1       # it is there now: nothing more is said
