"""The voice of the Assistant: the Windows voice it is told to use, and a companion's voice file of the player's own.

Nothing here speaks, loads a model, downloads a voice or asks Windows anything. PowerShell is a function that
writes down the command it was given, loading a voice is a function of the test's, the voice files are a few bytes
in the test's temporary folder, and the Suit Mk2 settings file that is read is one in that folder too.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, REPO)

from shared import character_voice as cv  # noqa: E402

# The command as it was before a Windows voice could be chosen. With no choice it must stay exactly this.
OLD_COMMAND = ("Add-Type -AssemblyName System.Speech; "
               "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
               "$s.Rate = 2; $s.Volume = 90; "
               "$s.Speak('Route plotted'); $s.Dispose()")
VOICE_JSON = {"audio": {"sample_rate": 16000}, "espeak": {"voice": "en-us"}, "num_symbols": 256,
              "num_speakers": 1, "phoneme_id_map": {"_": [0]}}


# ── the Windows voice ─────────────────────────────────────────────────────────────────────────────────────────

def test_with_no_voice_chosen_the_command_is_what_it_always_was():
    assert cv.sapi_command("Route plotted") == OLD_COMMAND
    assert cv.sapi_command("Route plotted", "") == OLD_COMMAND
    assert cv.sapi_command("Route plotted", None) == OLD_COMMAND


def test_a_chosen_voice_is_selected_before_the_line_and_a_missing_one_is_not_an_error():
    cmd = cv.sapi_command("Route plotted", "Microsoft Zira Desktop")
    assert "try { $s.SelectVoice('Microsoft Zira Desktop') } catch { }; " in cmd
    assert cmd.index("SelectVoice") < cmd.index("$s.Speak('Route plotted')")
    assert cmd.replace("try { $s.SelectVoice('Microsoft Zira Desktop') } catch { }; ", "") == OLD_COMMAND


def test_a_name_cannot_put_anything_else_in_the_command():
    cmd = cv.sapi_command("hi", "It's'); Remove-Item x; ('")
    assert "SelectVoice('It''s''); Remove-Item x; (''')" in cmd      # every quote doubled: it stays one string
    for bad in ("two\nlines", "tab\there", "x" * 201, "   "):
        assert "SelectVoice" not in cv.sapi_command("hi", bad)
    assert cv.clean_voice_name("  Microsoft David Desktop \r\n") == "Microsoft David Desktop"
    assert cv.clean_voice_name(None) == "" and cv.clean_voice_name(5) == "5"


def test_the_installed_voices_are_read_from_what_windows_prints():
    asked = []

    def run(cmd):
        asked.append(cmd)
        return "Microsoft David Desktop\r\nMicrosoft Zira Desktop\r\n\r\nMicrosoft David Desktop\r\n"

    assert cv.installed_windows_voices(run) == ["Microsoft David Desktop", "Microsoft Zira Desktop"]
    assert len(asked) == 1 and "GetInstalledVoices" in asked[0] and "Speak(" not in asked[0]
    assert cv.installed_windows_voices(lambda cmd: "") == []
    assert cv.installed_windows_voices(lambda cmd: None) == []


def test_when_windows_cannot_be_asked_there_are_no_voices_and_no_error():
    def run(cmd):
        raise OSError("no powershell here")

    assert cv.installed_windows_voices(run) == []


class _Popen:
    """Stands where PowerShell stands: it writes down the command and speaks nothing."""

    commands = []

    def __init__(self, args, **k):
        _Popen.commands.append(args[-1])

    def wait(self, timeout=None):
        return 0

    def poll(self):
        return 0


@pytest.fixture
def mouth(monkeypatch):
    _Popen.commands = []
    if sys.platform != "win32":
        pytest.skip("the Windows voice is only spoken on Windows")
    monkeypatch.setattr(cv.subprocess, "Popen", _Popen)
    m = cv.CharacterMouth()
    yield m
    m.close()


def test_a_mouth_speaks_with_the_windows_default_until_it_is_told_a_voice(mouth):
    assert mouth.windows_voice == ""
    mouth._sapi("Route plotted")
    assert _Popen.commands == [OLD_COMMAND]
    mouth.windows_voice = "Microsoft Zira Desktop"
    mouth._sapi("Route plotted")
    assert _Popen.commands[1] == cv.sapi_command("Route plotted", "Microsoft Zira Desktop")
    assert "SelectVoice('Microsoft Zira Desktop')" in _Popen.commands[1]
    mouth.windows_voice = ""
    mouth._sapi("Route plotted")
    assert _Popen.commands[2] == OLD_COMMAND


# ── a companion's voice file of the player's own ────────────────────────────────────────────────────────────────

class _Voice:
    def __init__(self, path):
        self.path = str(path)

    def synthesize_wav(self, text, wf, syn_config=None):
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x10" * 160)


def _write_voice(folder: Path, name: str, meta=True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    onnx = folder / f"{name}.onnx"
    onnx.write_bytes(b"not a real model")
    if meta:
        Path(str(onnx) + ".json").write_text(json.dumps(VOICE_JSON), encoding="utf-8")
    return onnx


@pytest.fixture
def suit(tmp_path, monkeypatch):
    """The built-in voices and the Suit Mk2 settings file, both in the test's folder; loading is written down."""
    builtin = tmp_path / "builtin"
    for who in ("elah", "montaigne"):
        _write_voice(builtin, who)
    loaded = []

    def load(path):
        loaded.append(str(path))
        if Path(path).name == "wont_load.onnx":
            raise RuntimeError("not a model")
        return _Voice(path)

    def choose(**files):
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(json.dumps({"muted": False, **files}), encoding="utf-8")

    settings = tmp_path / "suitmk2" / "settings.json"
    monkeypatch.setattr(cv, "SUIT_SETTINGS", settings)
    monkeypatch.setattr(cv, "VOICES_DIR", builtin)
    monkeypatch.setattr(cv, "_load_piper", load)
    monkeypatch.setattr(cv, "_voices", {})

    def no_download(*a, **k):
        raise AssertionError("a test must never fetch a voice")

    monkeypatch.setattr(cv, "_stock_path", no_download)
    return type("Suit", (), {"builtin": builtin, "loaded": loaded, "choose": staticmethod(choose),
                             "settings": settings, "tmp": tmp_path})


def test_with_no_suit_settings_or_no_choice_the_built_in_voice_speaks(suit):
    assert cv.custom_voice_file("elah") == ""                  # there is no settings file at all
    assert Path(cv._piper_voice("elah").path) == suit.builtin / "elah.onnx"
    suit.choose()                                              # a settings file from before the choice existed
    assert cv.custom_voice_file("elah") == "" and cv.custom_voice_file("montaigne") == ""
    cv._piper_voice("elah")
    cv._piper_voice("montaigne")
    assert suit.loaded == [str(suit.builtin / "elah.onnx"), str(suit.builtin / "montaigne.onnx")]   # once each


def test_the_companions_own_file_is_the_voice_the_assistant_speaks_with(suit):
    mine = _write_voice(suit.tmp / "mine", "narrator")
    suit.choose(voice_file_elah=str(mine))
    assert cv.custom_voice_file("elah") == str(mine) and cv.custom_voice_file("montaigne") == ""
    assert cv._piper_voice("elah").path == str(mine)
    assert Path(cv._piper_voice("montaigne").path) == suit.builtin / "montaigne.onnx"
    audio, rate = cv.synthesize("Route plotted", "elah")       # a line, through the same voice
    assert (len(audio), rate) == (160, 16000) and suit.loaded.count(str(mine)) == 1


def test_a_new_choice_in_the_suit_window_applies_on_the_next_line_without_a_restart(suit):
    one, two = _write_voice(suit.tmp / "mine", "one"), _write_voice(suit.tmp / "mine", "two")
    assert Path(cv._piper_voice("elah").path) == suit.builtin / "elah.onnx"
    suit.choose(voice_file_elah=str(one))
    assert cv._piper_voice("elah").path == str(one)
    suit.choose(voice_file_elah=str(two))
    assert cv._piper_voice("elah").path == str(two)
    suit.choose(voice_file_elah="")
    assert Path(cv._piper_voice("elah").path) == suit.builtin / "elah.onnx"
    assert suit.loaded == [str(suit.builtin / "elah.onnx"), str(one), str(two), str(suit.builtin / "elah.onnx")]


@pytest.mark.parametrize("case", ["gone", "no_json", "wont_load"])
def test_a_file_that_cannot_be_used_never_costs_the_assistant_its_voice(suit, case):
    folder = suit.tmp / "mine"
    bad = {"gone": lambda: folder / "gone.onnx",
           "no_json": lambda: _write_voice(folder, "no_json", meta=False),
           "wont_load": lambda: _write_voice(folder, "wont_load")}[case]()
    suit.choose(voice_file_elah=str(bad))
    assert Path(cv._piper_voice("elah").path) == suit.builtin / "elah.onnx"
    audio, rate = cv.synthesize("Route plotted", "elah")
    assert len(audio) == 160
    cv._piper_voice("elah")
    assert suit.loaded.count(str(suit.builtin / "elah.onnx")) == 1     # and the built-in one is loaded once
    assert suit.loaded.count(str(bad)) == (1 if case == "wont_load" else 0)   # a file is not retried every line


@pytest.mark.parametrize("text", ["", "{ not json", "[1, 2]", '"just a string"', '{"voice_file_elah": 5}',
                                  '{"voice_file_elah": ["a.onnx"]}', '{"voice_file_elah": "   "}'])
def test_a_settings_file_that_cannot_be_read_is_no_choice(suit, text):
    suit.settings.parent.mkdir(parents=True, exist_ok=True)
    suit.settings.write_text(text, encoding="utf-8")
    assert cv.custom_voice_file("elah") == ""
    assert Path(cv._piper_voice("elah").path) == suit.builtin / "elah.onnx"
