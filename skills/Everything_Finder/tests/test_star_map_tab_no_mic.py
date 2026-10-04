"""Opening the Star Map TAB of the Everything Finder must not arm a microphone.

This is the bug as J hit it (2026-10-04): his saved Star Map mic mode was
"Always on", the Everything Finder's Star Map tab is the standalone Star Map's
panel, so selecting the tab opened the mic. The Star Map has no ears any more;
voice-to-text lives in the AI Assistant, which relays map commands as the IPC
``map_command`` (also exercised here, through the window's own command handler).

Run in a clean subprocess with HOME redirected, like the lazy-import probe: the
real EverythingFinderWindow, the real Star Map panel, J's saved state on disk.
The audio stack is replaced by recording fakes that make every voice dependency
look installed - without them the OLD panel would also have stayed quiet on this
interpreter (no sounddevice here), and the test would pass for the wrong reason.
Against the pre-change tree this probe reports one opened input stream.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
EF_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

pytest.importorskip("PySide6.QtWidgets")

_PROBE = textwrap.dedent(r'''
    import json, os, sys, time, types
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, ROOT)

    # J's saved Star Map state: the mic mode that armed the ears on open.
    sdir = os.path.join(TMP, ".sctoolbox", "starmap")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "starmap_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"galaxy": {"selected": "STANTON", "home": "STANTON"},
                   "ears": {"binding": None, "mode": "always", "model": "small.en"},
                   "voice": {"replies": True}}, fh)

    rec = {"streams": 0, "listeners": 0, "models": 0}

    class InputStream:
        def __init__(self, *a, **k): rec["streams"] += 1
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    sd = types.ModuleType("sounddevice")
    sd.InputStream = InputStream
    sd.query_devices = lambda *a, **k: []
    sd.default = types.SimpleNamespace(device=(0, 0))

    class WhisperModel:
        def __init__(self, *a, **k): rec["models"] += 1
        def transcribe(self, *a, **k): return [], None
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = WhisperModel

    class Listener:
        name = "FakeListener"
        def __init__(self, **kw): self.running = False
        def start(self):
            self.running = True
            rec["listeners"] += 1
        def stop(self): self.running = False
        def is_alive(self): return self.running
        def join(self, _t=None): pass
    kb, ms = types.ModuleType("pynput.keyboard"), types.ModuleType("pynput.mouse")
    kb.Listener, ms.Listener = Listener, Listener
    pp = types.ModuleType("pynput")
    pp.keyboard, pp.mouse = kb, ms
    sys.modules.update({"sounddevice": sd, "faster_whisper": fw, "pynput": pp,
                        "pynput.keyboard": kb, "pynput.mouse": ms})

    import urllib.request
    def _no_network(*a, **k): raise OSError("network disabled in tests")
    urllib.request.urlopen = _no_network

    from shared.app_bootstrap import bootstrap_skill
    bootstrap_skill(os.path.join(EF_DIR, "everything_finder_app.py"))
    from PySide6.QtWidgets import QApplication, QPushButton
    from PySide6.QtCore import QCoreApplication
    app = QApplication([])
    from everything_finder import window as wmod
    wmod.EverythingFinderWindow._state_path = staticmethod(lambda: os.path.join(TMP, "w.json"))

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    out = {}
    w = wmod.EverythingFinderWindow(initial_tab=wmod.TAB_MAP)
    w.show()
    pump(1.0)                                   # builds the Star Map tab, then one tick more
    panel = w.inner(wmod.TAB_MAP)
    out["panel"] = type(panel).__name__
    out["has_ears"] = hasattr(panel, "_ears")
    out["mic_buttons"] = sorted(b.text() for b in panel.findChildren(QPushButton)
                                if b.text() in ("Always on", "Push-to-talk", "Set Mic Keybind",
                                                "Voice Replies"))
    out["after_open"] = dict(rec)

    # A command relayed by the Assistant reaches the tab through the window.
    reply = os.path.join(TMPDIR, "sc_toolbox_reply_eftest_%d.jsonl" % os.getpid())
    open(reply, "w").close()
    handler = getattr(w, "_handle_command")
    handler({"type": "map_command", "text": "zoom in", "id": "q1", "reply_file": reply})
    pump(0.3)
    try:
        with open(reply, encoding="utf-8") as fh:
            out["reply"] = [json.loads(ln) for ln in fh.read().splitlines() if ln.strip()]
    except OSError as exc:
        out["reply"] = "unreadable: %s" % exc
    for p in (reply, reply + ".lock"):
        try: os.remove(p)
        except OSError: pass
    out["after_command"] = dict(rec)
    out["tab"] = w.tabs.current_key()
    print("PROBE " + json.dumps(out), flush=True)
    os._exit(0)
''')


def _run_probe(tmp_path):
    import tempfile
    code = (f"ROOT = {ROOT!r}\nEF_DIR = {EF_DIR!r}\nTMP = {str(tmp_path)!r}\n"
            f"TMPDIR = {tempfile.gettempdir()!r}\n" + _PROBE)
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env, encoding="utf-8", errors="replace")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line is not None, f"probe printed nothing (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    return json.loads(line[6:])


def test_opening_the_star_map_tab_arms_no_microphone_and_still_takes_commands(tmp_path):
    got = _run_probe(tmp_path)
    assert got["panel"] == "StarmapPanel", got
    quiet = {"streams": 0, "listeners": 0, "models": 0}
    assert got["after_open"] == quiet, \
        f"opening the Star Map tab touched the audio stack: {got['after_open']}"
    assert got["has_ears"] is False, "the Star Map tab still owns a voice ears controller"
    assert got["mic_buttons"] == [], f"the Star Map tab still has mic controls: {got['mic_buttons']}"
    # ... and the Assistant can still drive it, with no mic on this side.
    assert got["reply"] == [{"id": "q1", "ok": True, "reply": "zoomed in"}], got["reply"]
    assert got["after_command"] == quiet, got["after_command"]
    assert got["tab"] == "star_map"
