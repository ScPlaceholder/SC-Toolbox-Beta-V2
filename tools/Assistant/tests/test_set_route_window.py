"""The real Assistant window sets a route, typed into its own text box, with no Star Map.

test_set_route.py covers the agent and the service. This is the window doing
it end to end in a clean subprocess: HOME redirected to an empty folder holding
a Star Map state with In-Game saved on (J's), the IPC bus reporting that no
Star Map and no Everything Finder is running, and the REAL in-game macro
running on a fake input layer:

  * ``pynput`` is replaced by recording fakes, so every key and click the macro
    would send is counted instead of sent;
  * the clipboard write, the macro's sleeps and the calibration file are
    replaced too;
  * voice replies are off and the mouth is a recorder, so nothing is said aloud.

Nothing here moves the mouse, presses a key, opens the mic or needs the game.
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

_PROBE = textwrap.dedent(r'''
    import json, os, sys, time, types
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ["SC_ASSISTANT_MODE"] = "router"          # no model, no network
    for p in (A_ROOT, ROOT):
        sys.path.insert(0, p)

    events = []          # every key / click the in-game macro sends

    class InputStream:
        def __init__(self, *a, **k): pass
        def start(self): pass
        def stop(self): pass
        def close(self): pass
    sd = types.ModuleType("sounddevice")
    sd.InputStream = InputStream
    sd.query_devices = lambda *a, **k: []
    sd.default = types.SimpleNamespace(device=(0, 0))

    class WhisperModel:
        def __init__(self, *a, **k): pass
        def transcribe(self, *a, **k): return [], None
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = WhisperModel

    class Listener:
        name = "FakeListener"
        def __init__(self, **kw): self.running = False
        def start(self): self.running = True
        def stop(self): self.running = False
        def is_alive(self): return self.running
        def join(self, _t=None): pass

    class KeyboardController:
        def press(self, key): events.append(["press", str(key)])
        def release(self, key): events.append(["release", str(key)])
        def pressed(self, key):
            class Held:
                def __enter__(s): events.append(["hold", str(key)])
                def __exit__(s, *exc): events.append(["unhold", str(key)])
            return Held()

    class MouseController:
        position = (0, 0)
        def click(self, button, count): events.append(["click", list(self.position)])
        def scroll(self, dx, dy): events.append(["scroll", dy])

    kb, ms = types.ModuleType("pynput.keyboard"), types.ModuleType("pynput.mouse")
    kb.Listener, ms.Listener = Listener, Listener
    kb.Controller, kb.Key = KeyboardController, types.SimpleNamespace(f2="F2", ctrl="CTRL")
    ms.Controller, ms.Button = MouseController, types.SimpleNamespace(left="LEFT")
    pp = types.ModuleType("pynput")
    pp.keyboard, pp.mouse = kb, ms
    sys.modules.update({"sounddevice": sd, "faster_whisper": fw, "pynput": pp,
                        "pynput.keyboard": kb, "pynput.mouse": ms})

    from PySide6.QtWidgets import QApplication, QPushButton
    from PySide6.QtCore import QCoreApplication
    app = QApplication([])

    # no Star Map, no Everything Finder: nothing is running, nothing may be sent
    from assistant import ipc_bus
    sent = []
    ipc_bus.is_running = lambda sid: False
    ipc_bus.send = lambda sid, cmd: sent.append([sid, cmd]) or False

    # the macro's own edges: no real clipboard, no real waiting, fixed click positions
    from assistant.set_route import route_setter, gate
    clipboard = []
    route_setter._set_clipboard = clipboard.append
    route_setter.time = types.SimpleNamespace(sleep=lambda _s: None)
    route_setter.load_calibration = lambda: {
        "search_bar": (11, 12), "destination": (21, 22), "map_center": (31, 32)}

    from assistant.panel import AssistantWindow

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    w = AssistantWindow(ROOT)
    spoken = []
    w._mouth = types.SimpleNamespace(speak=spoken.append, stop=lambda: None)
    w.show()
    pump(0.4)

    def say(text):
        """Type into the window's own text box and press Enter; wait for the reply."""
        w._txt_input.setText(text)
        w._txt_input.returnPressed.emit()
        end = time.time() + 20
        while time.time() < end:
            pump(0.05)
            if w._worker is not None and not w._worker.isRunning() and w._lbl_status.text() != "thinking…":
                break
        pump(0.5)                      # the macro thread and its queued narration
        return w._lbl_reply.text()

    out = {"buttons": [b.text() for b in w.findChildren(QPushButton)],
           "starts_on": w._btn_game.isChecked()}

    # Calibrate Route: the real button, a recording dialog (the real one hooks the
    # mouse and keyboard globally and waits for clicks in the game)
    dialogs = []
    class Dialog:
        result_ready = True
        def __init__(self, parent=None): dialogs.append(parent is w)
        def exec(self): return 1
    real_dialog = route_setter.RouteCalibrationDialog
    out["real_dialog"] = [real_dialog.__module__, real_dialog.__name__]
    route_setter.RouteCalibrationDialog = Dialog
    cal = [b for b in w.findChildren(QPushButton) if b.text() == "Calibrate Route"]
    out["calibrate_buttons"] = len(cal)
    out["calibrate_visible"] = bool(cal) and cal[0].isVisible() and cal[0].isEnabled()
    if cal:
        cal[0].click()
    out["calibrate_opened"] = dialogs
    out["calibrate_status"] = w._lbl_status.text()
    route_setter.RouteCalibrationDialog = real_dialog
    out["calibration_file"] = os.path.normpath(route_setter.calibration_path())

    w._btn_game.click()                                 # the pilot turns In-Game off
    out["after_click_off"] = {"button": w._btn_game.isChecked(), "saved": gate.in_game_enabled()}
    out["off_reply"] = say("navigate to Area 18")
    out["off_yes_reply"] = say("yes")
    out["off_events"] = list(events)

    w._btn_game.click()                                 # and back on
    out["after_click_on"] = {"button": w._btn_game.isChecked(), "saved": gate.in_game_enabled()}
    out["ask_reply"] = say("navigate to Area 18")
    out["events_before_yes"] = list(events)
    out["yes_reply"] = say("yes")
    out["events"] = list(events)
    out["clipboard"] = clipboard
    out["sent_to_other_tools"] = sent
    out["spoken_aloud"] = spoken
    out["status"] = w._lbl_status.text()

    w._ears.shutdown()
    w.hide()
    print("PROBE " + json.dumps(out), flush=True)
    os._exit(0)
''')


def test_the_window_sets_a_route_with_no_star_map_and_obeys_its_in_game_switch(tmp_path):
    sdir = tmp_path / ".sctoolbox"
    (sdir / "starmap").mkdir(parents=True)
    sm_file = sdir / "starmap" / "starmap_state.json"
    sm_file.write_text(json.dumps({"galaxy": {"home": "STANTON"}, "game_route": True}),
                       encoding="utf-8")
    (sdir / "assistant_panel.json").write_text(json.dumps({
        "binding": {"kind": "key", "code": "z"}, "geom": [100, 100, 520, 341],
        "voice_replies": False, "mic_mode": "push"}), encoding="utf-8")
    sm_before = sm_file.read_bytes()

    code = f"ROOT = {ROOT!r}\nA_ROOT = {A_ROOT!r}\n" + _PROBE
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env, encoding="utf-8", errors="replace")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line is not None, f"probe printed nothing (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    got = json.loads(line[6:])

    # the switch and its calibration are in the Assistant's own window
    assert "In-Game" in got["buttons"] and "Calibrate Route" in got["buttons"]
    # Calibrate is HERE (it was the Star Map's "Calibrate Star Map"): one visible
    # button, and pressing it opens the route setter's own calibration dialog
    assert got["calibrate_buttons"] == 1 and got["calibrate_visible"] is True
    assert got["real_dialog"] == ["assistant.set_route.route_setter", "RouteCalibrationDialog"]
    assert got["calibrate_opened"] == [True]
    assert got["calibrate_status"] == "route setter calibrated"
    # ... and it saves where calibration always was, so an existing one keeps working
    live = os.path.join(ROOT, "tools", "set_route_ai", "data", "mouse_calibration.json")
    packaged = os.path.join(A_ROOT, "assistant", "data", "set_route", "mouse_calibration.json")
    assert got["calibration_file"] == os.path.normpath(live if os.path.isfile(live) else packaged)
    # J had In-Game on in the Star Map: it comes up on here, from the map's saved state
    assert got["starts_on"] is True

    # off: said so, nothing asked, "yes" does nothing, not one key or click
    assert got["after_click_off"] == {"button": False, "saved": False}
    assert "In-game plotting is off" in got["off_reply"]
    assert "yes or no" not in got["off_reply"].lower()
    assert got["off_events"] == [], "input was sent to the game with In-Game off"

    # on: asks first, and sends nothing until the answer
    assert got["after_click_on"] == {"button": True, "saved": True}
    assert "Area18" in got["ask_reply"] and "Say yes or no" in got["ask_reply"]
    assert got["events_before_yes"] == [], "input was sent to the game before the pilot said yes"

    # yes: the real macro ran, here, with no Star Map anywhere
    ev = got["events"]
    assert ev and ev[0] == ["press", "F2"] and ev[-1] == ["release", "F2"]
    assert [e[1] for e in ev if e[0] == "click"] == [[31, 32], [11, 12], [21, 22], [31, 32]]
    assert ev.count(["press", "r"]) == 6
    assert got["clipboard"] == ["area18"]
    assert "isn't open" not in got["yes_reply"]
    assert got["sent_to_other_tools"] == [], "the route was relayed instead of set here"
    assert got["spoken_aloud"] == [], "the test spoke through the speakers"

    # the Star Map's file is only read
    assert sm_file.read_bytes() == sm_before
    saved = json.loads((sdir / "set_route" / "settings.json").read_text(encoding="utf-8"))
    assert saved == {"in_game": True}
