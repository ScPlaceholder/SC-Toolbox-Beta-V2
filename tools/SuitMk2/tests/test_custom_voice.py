"""A voice of the player's own for Elah and Montaigne: the settings, the check and the window's voice picker.

Per character the Suit window has a drop-down: Built-in (the default), each voice file in the player's voices
folder, and Browse. A file that cannot be used must never be silence: the character speaks with the built-in
voice and the window says which file and what was wrong.

Nothing here loads a model, plays a sound or downloads anything. The settings file and the voices folder are the
test's own temporary folder (settings.PATH and settings.DIR are replaced), the voice files are a few bytes written
by speech.write_fake_voice, and loading one is a function of the test's. The window is the REAL Suit panel, built
hidden with the harness of test_disable_companions.py.
"""
from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QFileDialog

import settings as st
import speech as sp
from test_disable_companions import _Harness, app          # noqa: F401  (app is a fixture)

KEYS = ("voice_file_elah", "voice_file_montaigne")


# ── the check, the fallback and the reload (speech.py's own selftest cases) ───────────────────────────────────

def test_the_check_the_fallback_and_the_reload(tmp_path):
    results = []
    sp._selftest_voices(lambda name, ok: results.append((name, bool(ok))), tmp_path)
    assert [name for name, ok in results if not ok] == []
    assert len(results) == 19


# ── settings ──────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def settings_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(st, "DIR", tmp_path / "suitmk2")
    monkeypatch.setattr(st, "PATH", tmp_path / "suitmk2" / "settings.json")
    return tmp_path / "suitmk2"


def test_the_default_is_the_built_in_voice_for_both(settings_dir):
    assert [st.DEFAULTS[k] for k in KEYS] == ["", ""]
    assert [st.load()[k] for k in KEYS] == ["", ""]            # no settings file at all
    assert not settings_dir.exists()                           # and reading the settings made no folder


def test_an_old_settings_file_loads_as_it_did_with_the_built_in_voices(settings_dir):
    settings_dir.mkdir()
    old = {"muted": True, "volume_elah": 0.7, "chattiness": 4, "voices_dir": "D:/somewhere/voices"}
    st.PATH.write_text(json.dumps(old), encoding="utf-8")
    s = st.load()
    assert [s[k] for k in KEYS] == ["", ""]
    assert {k: s[k] for k in old} == old                       # what the file said is what is loaded
    # every other key is its default, exactly as before the two new keys existed
    defaults = dict(st.DEFAULTS, talk_key=dict(st.DEFAULT_TALK_KEY))
    assert {k: v for k, v in s.items() if k not in old} == {k: v for k, v in defaults.items() if k not in old}
    assert json.loads(st.PATH.read_text(encoding="utf-8")) == old      # loading does not rewrite the file


def test_a_chosen_voice_is_saved_and_read_back(settings_dir, tmp_path):
    voice = str(sp.write_fake_voice(tmp_path / "mine", "deep"))
    s = st.load()
    s["voice_file_montaigne"] = voice
    st.save(s)
    assert json.loads(st.PATH.read_text(encoding="utf-8"))["voice_file_montaigne"] == voice
    again = st.load()
    assert again["voice_file_montaigne"] == voice and again["voice_file_elah"] == ""
    assert again == s


@pytest.mark.parametrize("written", [None, 5, ["a.onnx"], {"path": "a.onnx"}, True])
def test_a_choice_that_is_not_a_path_is_the_built_in_voice(settings_dir, written):
    settings_dir.mkdir()
    st.PATH.write_text(json.dumps({"voice_file_elah": written, "voice_file_montaigne": "  C:/v/x.onnx  "}),
                       encoding="utf-8")
    s = st.load()
    assert s["voice_file_elah"] == "" and s["voice_file_montaigne"] == "C:/v/x.onnx"


def test_the_players_voices_folder_is_beside_the_settings_file(settings_dir):
    assert st.player_voices_dir() == settings_dir / "voices"
    assert not st.player_voices_dir().exists()                 # asking where it is does not create it


# ── the Assistant, when it speaks as a companion ────────────────────────────────────────────────────────────────

def test_the_assistant_looks_for_the_choice_where_the_suit_keeps_it():
    """shared/character_voice.py cannot import this tool's settings, so it repeats the path and the key."""
    from shared import character_voice as cv
    assert cv.SUIT_SETTINGS == st.PATH
    assert tuple(cv.CUSTOM_KEY.format(speaker=who) for who in ("elah", "montaigne")) == KEYS
    assert all(k in st.DEFAULTS for k in KEYS)


def test_a_choice_the_suit_saves_is_the_one_the_assistant_reads(settings_dir, tmp_path, monkeypatch):
    from shared import character_voice as cv
    monkeypatch.setattr(cv, "SUIT_SETTINGS", st.PATH)          # the test's own settings file, as for the Suit
    voice = str(sp.write_fake_voice(tmp_path / "mine", "deep"))
    assert cv.custom_voice_file("montaigne") == ""
    s = st.load()
    s["voice_file_montaigne"] = voice
    st.save(s)
    assert cv.custom_voice_file("montaigne") == voice and cv.custom_voice_file("elah") == ""
    # the two checks agree on what the Suit window would report as unusable
    for bad in (str(tmp_path / "mine" / "gone.onnx"), str(sp.write_fake_voice(tmp_path / "mine", "bare", meta=None))):
        s["voice_file_montaigne"] = bad
        st.save(s)
        assert sp.check_voice_file(bad)[0] is False and cv.custom_voice_file("montaigne") == ""


# ── the window ──────────────────────────────────────────────────────────────────────────────────────────────────

class _Voices:
    """Stands where speech.Speech stands in the window and writes down what it was asked."""

    ducker = None
    made = []

    def __init__(self, voices_dir, **k):
        self.custom = dict(k.get("custom_voices") or {})
        self.calls, self.problems = [], {}
        _Voices.made.append(self)

    def set_level(self, *a):
        pass

    def mute(self, on):
        pass

    def allow_addressed(self, on):
        pass

    def preload(self):
        self.calls.append("preload")

    def voice_source(self, who):
        return "custom" if self.custom.get(who) and not self.problems.get(who) else "trained"

    def set_custom_voice(self, who, path):
        self.calls.append(("set", who, path))
        self.custom[who] = path

    def reload(self):
        self.calls.append("reload")

    def voice_problem(self, who):
        return self.problems.get(who, "")

    def close(self):
        pass


def _window(monkeypatch, tmp_path, enabled=True, **settings):
    h = _Harness(monkeypatch, tmp_path / "home", companions_enabled=enabled, **settings)
    _Voices.made = []
    monkeypatch.setattr(h.mod, "Speech", _Voices)
    return h, h.panel()


def _rows(box):
    return [box.itemText(i) for i in range(box.count())]


def _choose(w, who, text):
    """What picking a row of the drop-down does: Qt selects it, then says which one was activated."""
    box = w._voice_box[who]
    i = box.findText(text)
    assert i >= 0, f"{text!r} is not in {_rows(box)}"
    box.setCurrentIndex(i)
    box.activated.emit(i)


def _browse_gives(monkeypatch, path):
    asked = []

    def get_open(parent, title, folder, kinds):
        asked.append((title, folder, kinds))
        return (str(path).replace("\\", "/") if path else "", "")     # Qt gives forward slashes

    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(get_open))
    return asked


def test_a_window_with_nothing_chosen_shows_built_in_and_writes_nothing(app, monkeypatch, tmp_path):
    h, w = _window(monkeypatch, tmp_path)
    assert list(w._voice_box) == ["elah", "montaigne"]
    for who in ("elah", "montaigne"):
        assert _rows(w._voice_box[who]) == ["Built-in", "Browse…"]
        assert w._voice_box[who].currentText() == "Built-in"
    assert w._voice_note.text() == ""
    assert _Voices.made[0].custom == {"elah": "", "montaigne": ""} and _Voices.made[0].calls == []
    assert h.saved == [] and not (tmp_path / "home" / "voices").exists()


def test_the_voices_in_the_players_folder_are_listed_and_a_broken_one_is_not(app, monkeypatch, tmp_path):
    folder = tmp_path / "home" / "voices"                      # the harness makes tmp_path/home the settings folder
    sp.write_fake_voice(folder, "Zed")
    sp.write_fake_voice(folder, "alpha")
    sp.write_fake_voice(folder, "no_json", meta=None)
    h, w = _window(monkeypatch, tmp_path)
    assert _rows(w._voice_box["elah"]) == ["Built-in", "alpha", "Zed", "Browse…"]
    assert _rows(w._voice_box["montaigne"]) == ["Built-in", "alpha", "Zed", "Browse…"]


def test_choosing_a_listed_voice_saves_it_and_applies_it_without_a_restart(app, monkeypatch, tmp_path):
    voice = str(sp.write_fake_voice(tmp_path / "home" / "voices", "alpha"))
    h, w = _window(monkeypatch, tmp_path)
    voices, threads = w.speech, len(h.threads)
    _choose(w, "montaigne", "alpha")
    assert h.saved[-1]["voice_file_montaigne"] == voice and h.saved[-1]["voice_file_elah"] == ""
    assert voices.calls == [("set", "montaigne", voice), "reload"]     # the same voices, told and reloaded
    assert w.speech is voices and h.threads[threads:] == ["suitmk2_voice_preload"]
    assert w._voice_box["montaigne"].currentText() == "alpha" and w._voice_box["elah"].currentText() == "Built-in"
    assert w._voice_note.text() == ""
    w._refresh()
    assert w._rows["Montaigne voice"].text() == "custom | loaded: custom"
    assert w._rows["Elah voice"].text() == "stock | loaded: trained"

    _choose(w, "montaigne", "Built-in")
    assert h.saved[-1]["voice_file_montaigne"] == ""
    assert voices.calls[2:] == [("set", "montaigne", ""), "reload"]
    assert w._voice_box["montaigne"].currentText() == "Built-in"


def test_browse_takes_a_file_from_anywhere_and_cancel_changes_nothing(app, monkeypatch, tmp_path):
    voice = sp.write_fake_voice(tmp_path / "downloads", "narrator")
    h, w = _window(monkeypatch, tmp_path)
    asked = _browse_gives(monkeypatch, voice)
    _choose(w, "elah", "Browse…")
    assert asked == [("Choose a voice for Elah", str(Path.home()), "Piper voice (*.onnx)")]
    assert h.saved[-1]["voice_file_elah"] == str(voice)
    assert _rows(w._voice_box["elah"]) == ["Built-in", "narrator.onnx", "Browse…"]
    assert w._voice_box["elah"].currentText() == "narrator.onnx" and w._voice_note.text() == ""
    assert _rows(w._voice_box["montaigne"]) == ["Built-in", "Browse…"]

    saved, calls = len(h.saved), list(w.speech.calls)
    _browse_gives(monkeypatch, None)                           # the player closes the file dialog
    _choose(w, "elah", "Browse…")
    assert len(h.saved) == saved and w.speech.calls == calls
    assert w._voice_box["elah"].currentText() == "narrator.onnx"


def test_browse_opens_in_the_players_voices_folder_when_there_is_one(app, monkeypatch, tmp_path):
    folder = tmp_path / "home" / "voices"
    folder.mkdir(parents=True)
    h, w = _window(monkeypatch, tmp_path)
    asked = _browse_gives(monkeypatch, None)
    _choose(w, "montaigne", "Browse…")
    assert asked == [("Choose a voice for Montaigne", str(folder), "Piper voice (*.onnx)")]


@pytest.mark.parametrize("name, meta, said", [
    ("no_json", None, "no_json.onnx.json was not found beside no_json.onnx"),
    ("bad_json", "{ nope", "bad_json.onnx.json could not be read (JSONDecodeError)"),
    ("other", {"a": 1}, "other.onnx.json is not the settings file of a Piper voice"),
])
def test_a_file_that_cannot_be_used_is_said_in_the_window_and_is_not_silence(app, monkeypatch, tmp_path,
                                                                             name, meta, said):
    voice = sp.write_fake_voice(tmp_path / "downloads", name, meta=meta)
    h, w = _window(monkeypatch, tmp_path)
    _browse_gives(monkeypatch, voice)
    _choose(w, "elah", "Browse…")
    assert w._voice_note.text() == f"Elah is using the built-in voice: {said}."
    assert h.saved[-1]["voice_file_elah"] == str(voice)        # the choice is kept, and shown as the choice
    assert w._voice_box["elah"].currentText() == f"{name}.onnx"
    w._refresh()
    assert w._voice_note.text() == f"Elah is using the built-in voice: {said}."
    _choose(w, "elah", "Built-in")
    assert w._voice_note.text() == ""


def test_a_saved_voice_opens_as_saved_and_one_that_has_gone_is_said(app, monkeypatch, tmp_path):
    kept = str(sp.write_fake_voice(tmp_path / "downloads", "narrator"))
    gone = str(tmp_path / "downloads" / "deleted.onnx")
    h, w = _window(monkeypatch, tmp_path, voice_file_elah=kept, voice_file_montaigne=gone)
    assert _Voices.made[0].custom == {"elah": kept, "montaigne": gone}
    assert w._voice_box["elah"].currentText() == "narrator.onnx"
    assert w._voice_box["montaigne"].currentText() == "deleted.onnx"
    assert w._voice_note.text() == f"Montaigne is using the built-in voice: {gone} was not found."
    assert h.saved == []


def test_what_the_voices_say_went_wrong_on_loading_reaches_the_window(app, monkeypatch, tmp_path):
    """The file passes the check and then will not load. Only the voices know; the window asks them each second."""
    voice = str(sp.write_fake_voice(tmp_path / "downloads", "narrator"))
    h, w = _window(monkeypatch, tmp_path, voice_file_elah=voice, voice_file_montaigne=voice)
    assert w._voice_note.text() == ""
    w.speech.problems["montaigne"] = "narrator.onnx could not be loaded as a voice"
    w._refresh()
    assert w._voice_note.text() == ("Montaigne is using the built-in voice: narrator.onnx could not be loaded "
                                    "as a voice.")
    w.speech.problems["elah"] = "narrator.onnx could not be loaded as a voice"
    w._refresh()
    assert w._voice_note.text().splitlines() == [
        "Elah is using the built-in voice: narrator.onnx could not be loaded as a voice.",
        "Montaigne is using the built-in voice: narrator.onnx could not be loaded as a voice."]


def test_the_real_voices_behind_the_window_fall_back_and_say_why(app, monkeypatch, tmp_path):
    """The window with speech.Speech itself (not a stand-in). Loading a file is the test's; nothing is played."""
    builtin = tmp_path / "builtin"
    for who in ("elah", "montaigne"):
        sp.write_fake_voice(builtin, who)
    good = str(sp.write_fake_voice(tmp_path / "downloads", "narrator"))
    broken = str(sp.write_fake_voice(tmp_path / "downloads", "broken"))
    loaded = []

    def load(path):
        loaded.append(str(path))
        if Path(path).name == "broken.onnx":
            raise RuntimeError("not a model")
        return object()

    h = _Harness(monkeypatch, tmp_path / "home", companions_enabled=True, voices_dir=str(builtin),
                 voice_file_elah=good, voice_file_montaigne=broken)
    monkeypatch.setattr(h.mod, "Speech", functools.partial(sp.Speech, cache_dir=tmp_path / "cache", load=load,
                                                           play=lambda audio, sr: None))
    w = h.panel()
    try:
        w.speech.preload()                                     # the harness does not run the window's threads
        w._refresh()
        assert loaded == [good, broken, str(builtin / "montaigne.onnx")]
        assert w.speech.voice_source("elah") == "custom" and w.speech.voice_source("montaigne") == "trained"
        assert w._rows["Elah voice"].text() == "custom | loaded: custom"
        assert w._rows["Montaigne voice"].text() == "custom | loaded: trained"
        assert w._voice_note.text() == ("Montaigne is using the built-in voice: broken.onnx could not be loaded "
                                        "as a voice.")
        del loaded[:]
        _browse_gives(monkeypatch, good)
        _choose(w, "montaigne", "Browse…")                     # a good file, with no restart
        w.speech.preload()
        w._refresh()
        assert loaded == [good, good] and w.speech.voice_source("montaigne") == "custom"
        assert w._voice_note.text() == ""
    finally:
        w.speech.close()


def test_with_the_companions_disabled_a_choice_is_saved_and_nothing_starts(app, monkeypatch, tmp_path):
    voice = str(sp.write_fake_voice(tmp_path / "home" / "voices", "alpha"))
    h, w = _window(monkeypatch, tmp_path, enabled=False)
    _choose(w, "elah", "alpha")
    assert h.saved[-1]["voice_file_elah"] == voice and w._voice_box["elah"].currentText() == "alpha"
    assert h.made == [] and h.threads == [] and _Voices.made == []
    w._refresh()
    assert w._voice_note.text() == ""


def test_the_mouse_wheel_never_changes_a_voice(app, monkeypatch, tmp_path):
    sp.write_fake_voice(tmp_path / "home" / "voices", "alpha")
    h, w = _window(monkeypatch, tmp_path)

    class Wheel:
        ignored = False

        def ignore(self):
            self.ignored = True

    ev = Wheel()
    w._voice_box["elah"].wheelEvent(ev)
    assert ev.ignored and w._voice_box["elah"].currentText() == "Built-in" and h.saved == []
