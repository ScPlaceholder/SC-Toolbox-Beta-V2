"""The real Assistant window, started on J's saved files, after the Star Map's voice moved in.

The pure rule is tested in test_starmap_bridge.py. This is the window actually
doing it: a clean subprocess, HOME redirected to a folder holding J's two state
files as they were on 2026-10-04 (Star Map: mic "Always on"; Assistant:
push-to-talk on Z), and the audio stack replaced by recording fakes so that
"would the mic have opened?" has an answer on an interpreter with no sounddevice.

What must hold:
  * the Assistant stays on push-to-talk - J's "Always on" was the Star Map's
    setting and must not open THIS window's mic on launch;
  * it is not silently dropped either: it is recorded in the Assistant's state
    and the window says so, once;
  * the Star Map's file is not written;
  * a second launch says nothing.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

pytest.importorskip("PySide6.QtWidgets")

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
A_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

J_STARMAP = {
    "galaxy": {"selected": "STANTON", "home": "STANTON"},
    "game_route": True,
    "ears": {"binding": None, "mode": "always", "model": "small.en"},
    "voice": {"replies": True},
}
J_ASSISTANT = {
    "binding": {"kind": "key", "code": "z"},
    "geom": [100, 100, 520, 341],
    "voice_replies": True,
    "mic_mode": "push",
}

_PROBE = textwrap.dedent(r'''
    import json, os, sys, time, types
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["SC_ASSISTANT_MODE"] = "router"          # no model, no network
    for p in (A_ROOT, ROOT):
        sys.path.insert(0, p)

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

    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication
    app = QApplication([])
    from assistant.panel import AssistantWindow

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    def launch():
        for k in rec: rec[k] = 0
        w = AssistantWindow(ROOT)
        w.show()
        pump(0.6)
        out = {"mode": w._ears.mode(), "armed": w._ears.armed(),
               "model": w._ears._model_name, "audio": dict(rec),
               "notice": w._lbl_notice.text(), "notice_shown": not w._lbl_notice.isHidden(),
               "binding": (w._ears.binding().describe() if w._ears.binding() else None)}
        w._ears.shutdown()
        w.hide()
        w.deleteLater()
        pump(0.1)
        return out

    out = {"first": launch(), "second": launch()}
    print("PROBE " + json.dumps(out), flush=True)
    os._exit(0)
''')


def test_the_assistant_starts_on_js_files_without_opening_the_mic(tmp_path):
    sdir = tmp_path / ".sctoolbox"
    (sdir / "starmap").mkdir(parents=True)
    sm_file = sdir / "starmap" / "starmap_state.json"
    sm_file.write_text(json.dumps(J_STARMAP), encoding="utf-8")
    a_file = sdir / "assistant_panel.json"
    a_file.write_text(json.dumps(J_ASSISTANT), encoding="utf-8")
    sm_before = sm_file.read_bytes()

    code = f"ROOT = {ROOT!r}\nA_ROOT = {A_ROOT!r}\n" + _PROBE
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env, encoding="utf-8", errors="replace")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line is not None, f"probe printed nothing (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    got = json.loads(line[6:])
    first, second = got["first"], got["second"]

    # push-to-talk on Z, as J had set THIS window; the hook is watched, the mic is shut
    assert first["mode"] == "push", "the Star Map's Always on was applied to the Assistant"
    assert first["binding"] == "z" and first["armed"] is True
    assert first["audio"]["streams"] == 0, "the Assistant opened the mic on launch"
    assert first["audio"]["listeners"] == 1
    assert first["model"] == "small.en"

    # said, once
    assert first["notice_shown"] is True
    assert "Always on" in first["notice"] and "Push-to-talk" in first["notice"]
    assert second["notice_shown"] is False and second["notice"] == ""
    assert second["mode"] == "push" and second["audio"]["streams"] == 0

    # on record in the Assistant's own file; the Star Map's file untouched
    saved = json.loads(a_file.read_text(encoding="utf-8"))
    assert saved["mic_mode"] == "push"
    assert saved["starmap_voice_migrated"]["mode"] == "always"
    assert saved["starmap_voice_migrated"]["mode_applied"] is False
    assert sm_file.read_bytes() == sm_before
