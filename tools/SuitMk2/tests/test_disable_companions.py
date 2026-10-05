"""Disable companions: not running, which is more than muted (J, 2026-10-05).

"We also need a disable companions checkbox which keeps them from running for people who don't want them or have
potato computers."

The REAL Suit panel is built here (hidden, Qt's offscreen platform), with the settings file replaced by a dict and
every constructor that would start something replaced by one that records being called: the voices, the ducking
monitor, the model service, the companion core, the feedback keys, the microphone, and every thread the window
starts. With the companions disabled none of them may be called. With them enabled the same harness sees them
called, which is what makes the first result mean something.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import pytest
from PySide6.QtWidgets import QApplication

import settings as st


def _suit_module():
    """ui/suit_window.py, loaded by path under a private name (the toolbox root has a `ui` package of its own;
    tests/test_shutdown_once.py says how that goes wrong)."""
    name = "_suitmk2_ui_suit_window_under_test"
    if name in sys.modules:
        return sys.modules[name]
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui", "suit_window.py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class _Harness:
    """Everything the window could start, as fakes that write down that they were asked."""

    def __init__(self, monkeypatch, tmp_path, **settings):
        self.mod = mod = _suit_module()
        self.made, self.threads, self.saved, self.stopped = [], [], [], []
        h = self
        s = dict(st.DEFAULTS)
        s.update({"talk_key": dict(st.DEFAULT_TALK_KEY), "sound_classifier": False, "voices_dir": str(tmp_path),
                  "good_key": {"kind": "keyboard", "code": 120, "label": "F9", "joy_index": 0}})
        s.update(settings)
        self.s = s
        monkeypatch.setattr(st, "load", lambda: dict(h.s))
        monkeypatch.setattr(st, "save", lambda new: h.saved.append(dict(new)))
        monkeypatch.setattr(st, "DIR", tmp_path)

        class Speech:
            ducker = None

            def __init__(self, *a, **k):
                h.made.append("voices")

            def set_level(self, *a):
                pass

            def mute(self, on):
                pass

            def allow_addressed(self, on):
                pass

            def preload(self):
                h.made.append("voices loaded")

            def voice_source(self, who):
                return "fake"

            def close(self):
                h.stopped.append("voices")

        class Sidecar:
            status, backend = "fake", "none"
            during_ensure = None

            def __init__(self, *a, **k):
                h.made.append("model service")

            def ensure(self):
                if Sidecar.during_ensure:
                    Sidecar.during_ensure()
                return False

            def stop(self):
                h.stopped.append("model service")

        class Core:
            def __init__(self, speech, **k):
                h.made.append("core")
                self.kw, self.started = k, False
                self.eyes, self.talker, self.tree, self.session_id = k.get("eyes"), None, None, "s"
                self.affect = type("A", (), {"stance_text": False})()
                self.combat = type("C", (), {"feed": lambda *a: None})()

            def _note(self, msg):
                pass

            def start(self, game_log):
                h.made.append("core started")
                self.started = True

            def stop(self):
                h.stopped.append("core")

        class Monitor:
            def __init__(self, *a):
                self.triggered = type("Sig", (), {"connect": lambda *a: None})()

            def start(self, binding):
                h.made.append("feedback key")

            def stop(self):
                h.stopped.append("feedback key")

        class Thread:
            def __init__(self, target=None, name="", daemon=None, args=()):
                self.target, self.name, self.args = target, name, args

            def start(self):
                h.threads.append(self.name)
                if self.name == "suitmk2_disable":           # the teardown worker: run it, so its effect is seen
                    self.target(*self.args)

        class Ducking:
            def __init__(self, *a, **k):
                h.made.append("ducking monitor")

            def start(self):
                return self

        self.Sidecar = Sidecar
        monkeypatch.setattr(mod, "Speech", Speech)
        monkeypatch.setattr(mod, "Sidecar", Sidecar)
        monkeypatch.setattr(mod, "CompanionCore", Core)
        monkeypatch.setattr(mod, "HotkeyMonitor", Monitor)
        monkeypatch.setattr(mod, "find_game_log", lambda saved=None: None)
        monkeypatch.setattr(mod, "threading", type("T", (), {"Thread": Thread}))
        # a stand-in for the whole module: the real one needs scipy, and must not be what this test starts
        monkeypatch.setitem(sys.modules, "voice_fx", type("voice_fx", (), {"DuckingMonitor": Ducking}))
        monkeypatch.setattr(mod._Ears, "arm", lambda self_: h.made.append("microphone armed"))
        monkeypatch.setattr(mod._SuitBody, "_attach_tree", lambda self_: None)

    def panel(self):
        w = self.mod.SuitPanel(None, cmd_file=None)
        w._timer.stop()
        return w


@pytest.fixture
def app():
    yield QApplication.instance() or QApplication([])


def test_disabled_the_window_builds_and_boot_starts_nothing(app, monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, companions_enabled=False)
    w = h.panel()
    w._boot()                                                # even called directly, it starts nothing
    w._arm_ears()                                            # the talk key's 1.5 s timer would do this
    assert h.made == [] and h.threads == []
    assert w.core is None and w.sidecar is None and isinstance(w.speech, h.mod._NoSpeech)
    assert w._sidecar_ready is False                         # so first-run setup never checks or installs anything
    assert w._disable.isChecked() and w._off_lbl.text() == h.mod.COMPANIONS_OFF_NOTICE
    w._refresh()
    assert {v.text() for v in w._rows.values()} == {"off"}
    w._test_voices()                                         # the button is still there; it says nothing
    assert h.made == [] and h.saved == []


def test_enabled_the_same_harness_sees_everything_start(app, monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, companions_enabled=True)
    w = h.panel()
    assert h.made == ["ducking monitor", "voices", "feedback key"]
    assert h.threads == ["suitmk2_voice_preload", "suitmk2_boot"] and not w._disable.isChecked()
    w._boot()
    assert h.made[3:] == ["model service", "core", "core started"] and w.core.started
    assert w._off_lbl.text() == ""
    w._arm_ears()
    assert h.made[-1] == "microphone armed"


def test_an_old_settings_file_has_them_enabled(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"muted": True}), encoding="utf-8")
    assert st.DEFAULTS["companions_enabled"] is True and st.load()["companions_enabled"] is True
    (tmp_path / "settings.json").write_text(json.dumps({"companions_enabled": False}), encoding="utf-8")
    assert st.load()["companions_enabled"] is False


def test_ticking_the_box_stops_what_runs_and_unticking_starts_it_again_without_a_restart(app, monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, companions_enabled=True)
    w = h.panel()
    w._boot()
    core = w.core
    del h.made[:], h.threads[:]
    w._disable.setChecked(True)                              # the checkbox itself
    assert h.saved[-1]["companions_enabled"] is False
    assert sorted(h.stopped) == ["core", "feedback key", "feedback key", "model service", "voices"]
    assert w.core is None and w.sidecar is None and isinstance(w.speech, h.mod._NoSpeech) and core is not None
    assert h.made == [] and w._off_lbl.text() == h.mod.COMPANIONS_OFF_NOTICE
    w._disable.setChecked(False)
    assert h.saved[-1]["companions_enabled"] is True
    assert h.made == ["ducking monitor", "voices", "feedback key", "microphone armed"]
    assert h.threads == ["suitmk2_disable", "suitmk2_voice_preload", "suitmk2_boot"] and w._off_lbl.text() == ""


def test_disabled_while_the_model_service_was_still_starting_builds_no_core(app, monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, companions_enabled=True)
    w = h.panel()
    h.Sidecar.during_ensure = lambda: w._disable.setChecked(True)     # he ticks the box mid-boot
    w._boot()
    assert "core" not in h.made and w.core is None and w.sidecar is None
    assert h.stopped.count("model service") == 1             # the service this boot started is stopped again


def test_a_disabled_suit_does_not_speak_or_listen_through_the_stand_in(app, monkeypatch, tmp_path):
    h = _Harness(monkeypatch, tmp_path, companions_enabled=False)
    w = h.panel()
    assert w.speech.say("Suit online.", "elah", 1) is False and w.speech.pending() == 0
    w._on_transcript("where are we")                         # no core: nothing answers, nothing raises
    assert h.made == []
