"""Opening the Star Map must not arm a microphone. Ever.

J, 2026-10-04: the Star Map had its own voice ears, and his saved mic mode there
was "Always on". So opening the map - standalone, or as a tab of the Everything
Finder - opened the mic. Voice-to-text now lives in one place, the AI Assistant,
and the map only takes commands (typed, or relayed by the Assistant as the IPC
``map_command``).

How these tests can fail. The interpreter the suite runs on has no sounddevice
or faster-whisper, and with those missing even the OLD map would have refused
to arm - so a test that merely opened the panel would pass for the wrong
reason. Here the audio stack is replaced by recording fakes that make every
dependency look installed: the old panel, given this exact saved state, opens
the fake input stream (verified against the pre-change tree), and nothing real
is ever touched either way.

No network (urlopen blocked), no real audio, state files redirected to tmp.
"""
import json
import os
import re
import sys
import tempfile
import time
import types

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                 '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
from PySide6.QtCore import QCoreApplication, QEvent  # noqa: E402

PKG_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "starmap"))

#: J's Star Map state file as it was on 2026-10-04 (galaxy view trimmed): the
#: saved mic mode that used to arm the ears on open.
SAVED_STATE = {
    "galaxy": {"selected": "STANTON", "home": "STANTON"},
    "game_route": True,
    "ears": {"binding": None, "mode": "always", "model": "small.en"},
    "voice": {"replies": True},
}


class _Audio:
    """Recording stand-ins for sounddevice, faster-whisper and pynput."""

    def __init__(self):
        self.streams = []        # every input stream anything opened
        self.listeners = []      # every keyboard / mouse hook anything started
        self.models = []         # every whisper model anything loaded
        self.spoken = []         # every TTS line anything queued

    def install(self, monkeypatch):
        rec = self

        class InputStream:
            def __init__(self, *a, **k):
                rec.streams.append(k or a)

            def start(self): pass
            def stop(self): pass
            def close(self): pass

        sd = types.ModuleType("sounddevice")
        sd.InputStream = InputStream
        sd.query_devices = lambda *a, **k: []
        sd.default = types.SimpleNamespace(device=(0, 0))

        class WhisperModel:
            def __init__(self, *a, **k):
                rec.models.append(a)

            def transcribe(self, *a, **k):
                return [], None

        fw = types.ModuleType("faster_whisper")
        fw.WhisperModel = WhisperModel

        class Listener:
            def __init__(self, **kw):
                self.running = False

            def start(self):
                self.running = True
                rec.listeners.append(self)

            def stop(self):
                self.running = False

            def is_alive(self): return self.running
            def join(self, _t=None): pass
            name = "FakeListener"

        kb, ms = types.ModuleType("pynput.keyboard"), types.ModuleType("pynput.mouse")
        kb.Listener, ms.Listener = Listener, Listener
        pp = types.ModuleType("pynput")
        pp.keyboard, pp.mouse = kb, ms
        for name, mod in (("sounddevice", sd), ("faster_whisper", fw), ("pynput", pp),
                          ("pynput.keyboard", kb), ("pynput.mouse", ms)):
            monkeypatch.setitem(sys.modules, name, mod)

        # The old panel spoke through shared.character_voice.CharacterMouth.
        try:
            import shared.character_voice as cv
            monkeypatch.setattr(cv.CharacterMouth, "speak",
                                lambda _self, text, *a, **k: rec.spoken.append(text), raising=False)
        except (ImportError, AttributeError):
            pass                                # no shared voice here: nothing to intercept
        return self


def _pump(seconds=0.4):
    end = time.time() + seconds
    while time.time() < end:
        QCoreApplication.processEvents()
        time.sleep(0.01)


@pytest.fixture
def audio(monkeypatch):
    return _Audio().install(monkeypatch)


@pytest.fixture
def panel(monkeypatch, tmp_path, audio):
    QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    # HOME for this test only (not at import: the other Star Map test files each pin
    # their own HOME at import and one of them asserts on it).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    import urllib.request

    def _no_network(*_a, **_k):
        raise OSError("network disabled in tests")
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)

    from starmap import data, distances, lore
    from starmap import panel as panel_mod
    for mod in (data, distances, lore):
        for attr in ("_STATE_DIR", "_CACHE_DIR"):
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, str(tmp_path))
    state_path = tmp_path / "starmap_state.json"
    state_path.write_text(json.dumps(SAVED_STATE), encoding="utf-8")
    monkeypatch.setattr(data, "_STATE_PATH", str(state_path))
    monkeypatch.setattr(distances, "_CACHE_PATH", str(tmp_path / "distance_cache.json"))
    monkeypatch.setattr(distances, "_cache", {})
    monkeypatch.setattr(lore, "_CACHE_PATH", str(tmp_path / "lore_cache.json"))
    # the shared shopping list the map docks: this test's own file, never ~/.sctoolbox
    from shared.shopping import shopping_list as shop_mod
    monkeypatch.setattr(shop_mod, "_shared", shop_mod.ShoppingList(path=str(tmp_path / "shopping.json")))

    p = panel_mod.StarmapPanel()
    assert p._galaxy is not None, "star map failed to build"
    p.resize(1100, 700)
    p.show()
    _pump()
    yield p
    p.hide()
    p.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QCoreApplication.processEvents()


# ── the bug ──────────────────────────────────────────────────────────────────

def test_opening_the_star_map_with_always_on_saved_opens_no_microphone(panel, audio):
    """The report, exactly: saved mode "always", open the map, the mic arms."""
    from starmap.data import load_state
    assert load_state()["ears"]["mode"] == "always", "the fixture is not J's saved state"
    _pump(0.6)                      # the old panel armed one event-loop tick after building
    assert audio.streams == [], "opening the Star Map opened an audio input stream"
    assert audio.listeners == [], "opening the Star Map installed a keyboard / mouse hook"
    assert audio.models == [], "opening the Star Map loaded a speech-to-text model"
    assert not hasattr(panel, "_ears"), "the Star Map still owns a voice ears controller"


def test_the_star_map_has_no_mic_controls(panel):
    """No button, in any state, that could put the mic back."""
    texts = [b.text() for b in panel.findChildren(QtWidgets.QPushButton)]
    for gone in ("Always on", "Push-to-talk", "Set Mic Keybind", "Voice Replies", "Ears"):
        assert gone not in texts, f"the Star Map still has a {gone!r} control"
    assert "Calibrate Star Map" in texts          # the route-setter calibration stays


def test_no_star_map_module_can_capture_audio():
    """Source-level: nothing in the package imports an audio-capture or STT library,
    and the voice package is gone (the in-game route macro's pynput use is input
    SENDING, in set_route/, and is not a microphone)."""
    assert not os.path.exists(os.path.join(PKG_DIR, "voice")), "starmap/voice/ is back"
    banned = re.compile(r"^\s*(?:import|from)\s+(sounddevice|faster_whisper|pyaudio|speech_recognition)\b"
                        r"|\bInputStream\s*\(|import_module\(\s*[\"'](sounddevice|faster_whisper)",
                        re.MULTILINE)
    offenders = []
    for root, _dirs, files in os.walk(PKG_DIR):
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, encoding="utf-8") as fh:
                    if banned.search(fh.read()):
                        offenders.append(os.path.relpath(path, PKG_DIR))
    assert offenders == [], f"audio capture is back in the Star Map: {offenders}"


def test_saving_state_leaves_the_old_mic_settings_for_the_assistant_to_read(panel):
    """The map no longer writes "ears" / "voice", and must not erase them either:
    the Assistant reads them once to carry J's choices over."""
    from starmap.data import load_state
    panel.save_state()
    st = load_state()
    assert st["ears"] == SAVED_STATE["ears"]
    assert st["voice"] == SAVED_STATE["voice"]
    assert st["game_route"] is True


# ── commands still work, without ears ────────────────────────────────────────

def test_a_typed_command_runs_through_the_router(panel):
    cam = panel._galaxy._cam
    before = cam.zoom
    ok, reply = panel.run_command("zoom in")
    assert ok and reply == "zoomed in"
    assert cam.zoom > before
    assert panel._voicebar.status() == "zoomed in"
    ok, reply = panel.run_command("flibbertigibbet")
    assert not ok and "did not understand" in reply


def _reply_path(name="a1"):
    from starmap.commands import REPLY_PREFIX, REPLY_SUFFIX
    return os.path.join(tempfile.gettempdir(), f"{REPLY_PREFIX}test_{os.getpid()}_{name}{REPLY_SUFFIX}")


def _read_lines(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(ln) for ln in fh.read().splitlines() if ln.strip()]


def test_a_command_from_the_assistant_is_run_and_answered_in_its_reply_file(panel):
    path = _reply_path("ok")
    open(path, "w").close()
    try:
        before = panel._galaxy._cam.zoom
        panel._on_ipc({"type": "map_command", "text": "zoom out", "id": "abc", "reply_file": path})
        assert panel._galaxy._cam.zoom < before
        assert _read_lines(path) == [{"id": "abc", "ok": True, "reply": "zoomed out"}]
        panel._on_ipc({"type": "map_command", "text": "wibble", "id": "def", "reply_file": path})
        last = _read_lines(path)[-1]
        assert last["id"] == "def" and last["ok"] is False and "did not understand" in last["reply"]
    finally:
        for p in (path, path + ".lock"):
            if os.path.exists(p):
                os.remove(p)


def test_the_map_never_speaks_its_lines_go_to_the_assistant(panel, audio, monkeypatch):
    """A line the map used to SAY becomes the command's reply (during the command)
    or a "say" line for the Assistant (after it). With the Assistant's mic open, a
    second voice in another process would come back in as the pilot's next words."""
    class _Engine:
        def find_destination(self, name):
            return "Area 18", []
    monkeypatch.setattr(panel, "_engine", lambda: _Engine())
    panel._btn_game.setChecked(False)
    path = _reply_path("say")
    open(path, "w").close()
    try:
        panel.handle_map_command({"text": "navigate to area 18", "id": "n1", "reply_file": path})
        first = _read_lines(path)[0]
        assert first["id"] == "n1" and first["ok"] is True
        # the reply is the line the map would have SPOKEN, not the longer status text
        assert "area 18" in first["reply"].lower()
        assert "toggle" not in first["reply"].lower(), first["reply"]
        assert "toggle 'In-Game'" in panel._voicebar.status()
        panel._route_done("Route set to Area 18")           # the macro narrating, later
        assert _read_lines(path)[-1] == {"say": "Route set to Area 18"}
        assert audio.spoken == [], "the Star Map spoke through its own mouth"
        assert not hasattr(panel, "_mouth") or panel._mouth is None
    finally:
        for p in (path, path + ".lock"):
            if os.path.exists(p):
                os.remove(p)


def test_a_reply_file_outside_the_temp_folder_is_not_written(panel, tmp_path):
    """The command file is writable by any local process, so the path it names is
    only honoured when it is a reply file in the temp folder."""
    from starmap.commands import safe_reply_file
    evil = tmp_path / "sc_toolbox_reply_x.jsonl"           # right name, wrong folder
    wrong_name = os.path.join(tempfile.gettempdir(), "notes.txt")
    assert safe_reply_file(str(evil)) == ""
    assert safe_reply_file(wrong_name) == ""
    assert safe_reply_file(None) == "" and safe_reply_file(123) == ""
    assert safe_reply_file(_reply_path("good")) == _reply_path("good")
    panel.handle_map_command({"text": "zoom in", "id": "z", "reply_file": str(evil)})
    assert not evil.exists()
    assert panel._reply_file == ""
