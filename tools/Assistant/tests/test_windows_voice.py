"""The Assistant's Settings let the player choose which installed Windows voice speaks.

The row is "Windows voice": "Windows default" (what it always was) and the voices Windows has on the PC. The choice
is kept in the panel's own state file with Voice Replies, and a state file from before has none.

Nothing here speaks or asks Windows anything: the list of voices is handed in, the mouth is an object that holds
what it was told, and the state file and the LLM settings file are in the test's temporary folder.
"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QPushButton, QWidget

from assistant import config as config_mod
from assistant import panel as panel_mod
from assistant.config import LLMConfig

DAVID, ZIRA = "Microsoft David Desktop", "Microsoft Zira Desktop"


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def _rows(dlg):
    box = dlg.windows_voice
    return [box.itemText(i) for i in range(box.count())]


def _dialog(saved="", voices=(DAVID, ZIRA), listed=True):
    """The real dialog, never shown. listed: wait for the list of voices and put it in, as the dialog's timer does."""
    def list_voices():
        if isinstance(voices, Exception):
            raise voices
        return list(voices)

    dlg = panel_mod._SettingsDialog(LLMConfig(), None, windows_voice=saved, list_voices=list_voices)
    dlg._voice_thread.join(5)
    if listed:
        dlg._poll_windows_voices()
    return dlg


# ── the dialog ────────────────────────────────────────────────────────────────────────────────────────────────

def test_the_row_is_there_and_opens_on_windows_default(app):
    dlg = _dialog(listed=False)
    assert _rows(dlg) == ["Windows default"] and dlg.result_windows_voice() == ""     # before Windows has answered
    assert dlg._voice_poll.isActive()
    dlg._poll_windows_voices()
    assert _rows(dlg) == ["Windows default", DAVID, ZIRA]
    assert dlg.windows_voice.currentText() == "Windows default" and dlg.result_windows_voice() == ""
    assert not dlg._voice_poll.isActive()                      # asked once, not again
    label = dlg.layout().labelForField(dlg.windows_voice)
    assert label.text() == "Windows voice" and "Tool voice" in dlg.windows_voice.toolTip()


def test_choosing_a_voice_is_what_the_dialog_hands_back(app):
    dlg = _dialog()
    dlg.windows_voice.setCurrentIndex(dlg.windows_voice.findText(ZIRA))
    assert dlg.result_windows_voice() == ZIRA
    dlg.windows_voice.setCurrentIndex(0)
    assert dlg.result_windows_voice() == ""


def test_a_saved_voice_is_the_one_shown_before_and_after_the_list_arrives(app):
    dlg = _dialog(saved=ZIRA, listed=False)
    assert _rows(dlg) == ["Windows default", ZIRA] and dlg.windows_voice.currentText() == ZIRA
    dlg._poll_windows_voices()
    assert _rows(dlg) == ["Windows default", DAVID, ZIRA]
    assert dlg.windows_voice.currentText() == ZIRA and dlg.result_windows_voice() == ZIRA


def test_a_saved_voice_windows_no_longer_has_is_kept_and_said(app):
    dlg = _dialog(saved="Microsoft Hazel Desktop")
    assert _rows(dlg) == ["Windows default", DAVID, ZIRA, "Microsoft Hazel Desktop (not on this PC)"]
    assert dlg.windows_voice.currentText() == "Microsoft Hazel Desktop (not on this PC)"
    assert dlg.result_windows_voice() == "Microsoft Hazel Desktop"     # saving does not change it by itself


def test_a_voice_picked_before_the_list_arrives_stays_picked(app):
    dlg = _dialog(saved=ZIRA, listed=False)
    dlg.windows_voice.setCurrentIndex(0)                       # the player goes back to Windows default
    dlg._poll_windows_voices()
    assert dlg.windows_voice.currentText() == "Windows default" and dlg.result_windows_voice() == ""


def test_when_windows_cannot_list_its_voices_the_dialog_still_works(app):
    dlg = _dialog(saved=ZIRA, voices=OSError("no powershell"))
    assert _rows(dlg) == ["Windows default", ZIRA]            # nothing was listed, so nothing is said to be missing
    assert dlg.windows_voice.currentText() == ZIRA and dlg.result_windows_voice() == ZIRA
    assert dlg._voices_found == [] and not dlg._voice_poll.isActive()      # it ended; it is not asked for ever
    dlg = _dialog(voices=())
    assert _rows(dlg) == ["Windows default"] and dlg.result_windows_voice() == ""


def test_the_other_settings_are_handed_back_as_before(app):
    dlg = _dialog(saved=ZIRA)
    cfg, fresh = dlg.result_config(), LLMConfig()
    assert (cfg.provider, cfg.base_url, cfg.model, cfg.api_key, cfg.mode, cfg.max_tokens) == \
        (fresh.provider, fresh.base_url, fresh.model, fresh.api_key, fresh.mode, fresh.max_tokens)
    assert not hasattr(cfg, "windows_voice")                   # the voice is not an LLM setting


# ── the panel: saved, applied at once, read back at the next start ─────────────────────────────────────────────

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(panel_mod, "_STATE_PATH", str(tmp_path / "assistant_panel.json"))
    monkeypatch.setattr(config_mod, "_config_path", lambda: str(tmp_path / "assistant_llm.json"))
    for name in ("SC_LLM_PROVIDER", "SC_LLM_BASE_URL", "SC_LLM_API_KEY", "SC_LLM_MODEL", "SC_ASSISTANT_MODE",
                 "SC_LLM_MAX_TOKENS", "SC_LLM_TEMPERATURE"):
        monkeypatch.delenv(name, raising=False)
    import shared.character_voice as cv
    monkeypatch.setattr(cv, "installed_windows_voices", lambda *a, **k: [DAVID, ZIRA])
    return tmp_path


def _panel():
    """The real AssistantPanel's settings code, with a mouth that only holds what it is told."""
    cls = panel_mod.AssistantPanel
    p = cls.__new__(cls)
    QWidget.__init__(p)
    p._state = dict(p._load_state())
    p._mouth = types.SimpleNamespace(windows_voice="never told", stop=lambda: None)
    p._btn_replies = QPushButton("Voice Replies", p)
    p._btn_replies.setCheckable(True)
    p._btn_replies.setChecked(bool(p._state.get("voice_replies", True)))
    p._lbl_status = QLabel("", p)
    p.configured = []
    p._agent = types.SimpleNamespace(configure=p.configured.append)
    p._check_model = lambda: None                              # the real one asks the local model service
    p._apply_windows_voice()                                   # as _build does once the mouth is made
    return p


def _answer(monkeypatch, pick, accept=True):
    """Settings… is opened and the player picks a row (by its text) and presses Save, or Cancel."""
    seen = []

    def exec_(dlg):
        dlg._voice_thread.join(5)
        dlg._poll_windows_voices()
        seen.append((_rows(dlg), dlg.windows_voice.currentText()))
        i = dlg.windows_voice.findText(pick)
        assert i >= 0, f"{pick!r} is not in {_rows(dlg)}"
        dlg.windows_voice.setCurrentIndex(i)
        return QDialog.Accepted if accept else QDialog.Rejected

    monkeypatch.setattr(panel_mod._SettingsDialog, "exec", exec_)
    return seen


def _state_file():
    return json.loads(Path(panel_mod._STATE_PATH).read_text(encoding="utf-8"))


def test_with_nothing_saved_the_mouth_is_on_windows_default(app, home):
    p = _panel()
    assert p._mouth.windows_voice == "" and not Path(panel_mod._STATE_PATH).exists()


def test_a_state_file_from_before_loads_as_it_did_and_is_windows_default(app, home):
    old = {"voice_replies": False, "mic_mode": "always", "binding": {"kind": "key", "code": "f9"}}
    Path(panel_mod._STATE_PATH).write_text(json.dumps(old), encoding="utf-8")
    p = _panel()
    assert p._state == old and p._mouth.windows_voice == ""
    assert not p._btn_replies.isChecked()
    assert _state_file() == old                                # reading it does not rewrite it


@pytest.mark.parametrize("written", [None, 5, ["Zira"], {"name": "Zira"}])
def test_a_saved_voice_that_is_not_a_name_is_windows_default(app, home, written):
    Path(panel_mod._STATE_PATH).write_text(json.dumps({"windows_voice": written}), encoding="utf-8")
    assert _panel()._mouth.windows_voice == ""


def test_saving_a_voice_applies_it_at_once_and_it_is_still_the_voice_after_a_restart(app, home, monkeypatch):
    p = _panel()
    seen = _answer(monkeypatch, ZIRA)
    p._edit_settings()
    assert seen == [(["Windows default", DAVID, ZIRA], "Windows default")]
    assert p._mouth.windows_voice == ZIRA                      # the same mouth, told now
    assert _state_file()["windows_voice"] == ZIRA and _state_file()["voice_replies"] is True
    assert len(p.configured) == 1 and p._lbl_status.text().startswith("LLM set")    # the rest of Save still happens

    again = _panel()                                           # a new start reads the file
    assert again._mouth.windows_voice == ZIRA
    seen = _answer(monkeypatch, "Windows default")
    again._edit_settings()
    assert seen[0][1] == ZIRA                                  # the dialog opened on the saved voice
    assert again._mouth.windows_voice == "" and _state_file()["windows_voice"] == ""
    assert _panel()._mouth.windows_voice == ""


def test_cancel_changes_nothing(app, home, monkeypatch):
    Path(panel_mod._STATE_PATH).write_text(json.dumps({"windows_voice": DAVID}), encoding="utf-8")
    p = _panel()
    _answer(monkeypatch, ZIRA, accept=False)
    p._edit_settings()
    assert p._mouth.windows_voice == DAVID and _state_file() == {"windows_voice": DAVID}
    assert p.configured == [] and not (home / "assistant_llm.json").exists()
