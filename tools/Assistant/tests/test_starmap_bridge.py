"""Voice lives in the Assistant; the Star Map takes orders from it.

Covers assistant/starmap_bridge.py and its wiring:

  * which utterances are map commands (and which must NOT be);
  * relaying one to a Star Map and hearing its answer, with a fake IPC bus and
    a fake map on a thread - no real process, no real microphone;
  * the one-time move of the Star Map's saved mic settings, on J's actual files
    as they were on 2026-10-04.
"""
import json
import os
import threading
import time

import pytest

from assistant import answers, starmap_bridge as sb


# ── which utterances are for the map ─────────────────────────────────────────

@pytest.mark.parametrize("said, command", [
    ("Navigate to Area 18.", "navigate to area 18"),
    ("Set route to Port Tressler", "set route to port tressler"),
    ("set the route to Port Tressler", "set the route to port tressler"),
    ("plot a course to Everus Harbor", "plot a course to everus harbor"),
    ("Route to Pyro", "route to pyro"),
    ("Clear route.", "clear route"),
    ("clear the route", "clear the route"),
    ("Zoom in", "zoom in"),
    ("please zoom out", "zoom out"),
    ("Back to galaxy", "back to galaxy"),
    ("Take me home", "take me home"),
    ("open the shopping list", "open the shopping list"),
    ("Toggle the grocery list", "toggle the grocery list"),
    # "star map, <anything>" reaches every map command
    ("Star map, show Hurston", "show hurston"),
    ("starmap commodities", "commodities"),
    ("tell the star map to go back", "go back"),
    ("Star Map: help", "help"),
])
def test_map_commands_are_recognised(said, command):
    assert sb.command_text(said) == command


@pytest.mark.parametrize("said", [
    "",
    "What is the best trade route to Pyro for my Caterpillar?",   # "route to" not at the start
    "How many jumps from Stanton to Pyro",                        # jump_route's question
    "Open the star map",                                          # launch_tool's
    "star map",                                                   # nothing to do
    "show me the star map",
    "Where can I buy a medpen",
    "find a Pembroke helmet",                                     # the map's "find X" needs "star map,"
    "go home and sleep",
    "zoom in on the profit margins",
    "take me home to Orison please",
])
def test_everything_else_is_left_to_the_assistant(said):
    assert sb.command_text(said) == ""


# ── relaying a command ───────────────────────────────────────────────────────

class _Bus:
    """A stand-in for ipc_bus: one optional live 'skill', whose commands land in a list."""

    def __init__(self, running=(), answer=None, delay=0.0, narrate=()):
        self.running = set(running)
        self.sent = []
        self._answer, self._delay, self._narrate = answer, delay, tuple(narrate)

    def is_running(self, sid):
        return sid in self.running

    def send(self, sid, cmd):
        if sid not in self.running:
            return False
        self.sent.append((sid, cmd))
        if self._answer is not None:
            threading.Thread(target=self._map, args=(cmd,), daemon=True).start()
        return True

    def _map(self, cmd):
        """What the Star Map does: answer into the reply file, narrate later."""
        time.sleep(self._delay)
        ok, reply = self._answer(cmd["text"])
        with open(cmd["reply_file"], "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": cmd["id"], "ok": ok, "reply": reply}) + "\n")
        for line in self._narrate:
            time.sleep(0.35)      # well after send_command has returned: the follower's job
            with open(cmd["reply_file"], "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"say": line}) + "\n")


def _wait(cond, seconds=3.0):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_a_command_goes_to_the_standalone_star_map_and_its_answer_comes_back(tmp_path):
    bus = _Bus(running={"starmap", "everything_finder"}, answer=lambda t: (True, "zoomed in"))
    r = sb.send_command("zoom in", bus=bus, tmp_dir=str(tmp_path), follow_s=0)
    sid, cmd = bus.sent[0]
    assert sid == "starmap", "the standalone Star Map is preferred when both are open"
    assert cmd["type"] == "map_command" and cmd["text"] == "zoom in"
    name = os.path.basename(cmd["reply_file"])
    assert name.startswith(sb.REPLY_PREFIX) and name.endswith(sb.REPLY_SUFFIX)
    assert r == {"command": "zoom in", "target": "the Star Map", "understood": True,
                 "reply": "zoomed in"}
    assert os.listdir(tmp_path) == [], "the reply file was left behind"


def test_the_everything_finder_is_used_when_no_standalone_map_is_open(tmp_path):
    bus = _Bus(running={"everything_finder"}, answer=lambda t: (False, "did not understand: 'x'"))
    r = sb.send_command("x", bus=bus, tmp_dir=str(tmp_path), follow_s=0)
    assert bus.sent[0][0] == "everything_finder"
    assert r["target"] == "the Everything Finder"
    assert r["understood"] is False and "did not understand" in r["reply"]


def test_with_no_star_map_open_nothing_is_sent_and_the_pilot_is_told(tmp_path):
    bus = _Bus(running=set())
    r = sb.send_command("zoom in", bus=bus, tmp_dir=str(tmp_path))
    assert bus.sent == []
    assert r["not_running"] is True and r["reply"] == sb.NOT_OPEN
    assert answers.plain_answer("starmap_command", {}, r) == sb.NOT_OPEN
    assert os.listdir(tmp_path) == []


def test_a_map_that_does_not_answer_times_out_instead_of_hanging(tmp_path):
    bus = _Bus(running={"starmap"}, answer=None)
    t0 = time.time()
    r = sb.send_command("zoom in", bus=bus, tmp_dir=str(tmp_path), timeout=0.3, follow_s=0)
    assert time.time() - t0 < 2.0
    assert r.get("timed_out") is True and r["reply"] == ""
    assert "has not answered" in answers.plain_answer("starmap_command", {}, r)


def test_later_narration_from_the_map_is_spoken_by_the_assistant(tmp_path):
    """The in-game route macro reports its steps after the command has returned.
    The map is mute; those lines must come out of the Assistant's mouth."""
    said = []
    bus = _Bus(running={"starmap"}, answer=lambda t: (True, "setting route to Area 18 in game"),
               narrate=("Opening the star map", "Route set to Area 18"))
    r = sb.send_command("navigate to area 18", bus=bus, on_say=said.append,
                        tmp_dir=str(tmp_path), follow_s=2.5)
    assert r["reply"] == "setting route to Area 18 in game"
    assert said == [], "the narration had not been written yet; the follower must deliver it"
    assert _wait(lambda: said == ["Opening the star map", "Route set to Area 18"]), said
    assert _wait(lambda: os.listdir(tmp_path) == [], 4.0), "the follower left its file behind"


def test_a_late_answer_is_still_said(tmp_path):
    said = []
    bus = _Bus(running={"starmap"}, answer=lambda t: (True, "zoomed in"), delay=0.5)
    r = sb.send_command("zoom in", bus=bus, on_say=said.append, tmp_dir=str(tmp_path),
                        timeout=0.1, follow_s=2.0)
    assert r.get("timed_out") is True
    assert _wait(lambda: said == ["zoomed in"]), said


def test_answers_are_said_in_the_maps_own_words():
    f = lambda **r: answers.plain_answer("starmap_command", {}, r)          # noqa: E731
    assert f(reply="zoomed in", target="the Star Map") == "Zoomed in."
    assert f(reply="which one? Area 18, Area 04", target="the Star Map") == "Which one? Area 18, Area 04."
    assert f(reply="Which one? Area 18?", target="the Star Map") == "Which one? Area 18?"
    assert f(reply="", target="the Everything Finder") == "Sent to the Everything Finder."


# ── wiring: registry, router ─────────────────────────────────────────────────

def test_the_tool_is_registered_and_not_confirm_gated():
    from assistant.builtin_tools import build_default_registry
    tool = build_default_registry().get("starmap_command")
    assert tool is not None
    assert tool.confirm is False, "a yes/no before every 'zoom in' would make voice unusable"


class _NoCatalog:
    """The router must decide a map command before it needs the name catalogue."""

    def find(self, text):
        raise AssertionError("the catalogue was consulted for a map command")


def test_the_router_sends_map_phrases_to_the_map_and_nothing_else():
    from assistant.builtin_tools import build_default_registry
    from assistant.router import Router
    r = Router(build_default_registry(), catalog=_NoCatalog())
    d = r.decide("Navigate to Area 18")
    assert (d.kind, d.tool, d.args) == ("call", "starmap_command", {"command": "navigate to area 18"})
    d = r.decide("star map, zoom in", pending={"tool": "ship_info", "args": {}, "missing": "ship"})
    assert d.tool == "starmap_command" and d.args == {"command": "zoom in"}


def test_without_the_tool_the_router_does_not_invent_it():
    from assistant.router import Router
    from assistant.tools import ToolRegistry

    class _Empty:
        def find(self, text):
            return []
    d = Router(ToolRegistry(), catalog=_Empty()).decide("zoom in")
    assert d.tool != "starmap_command"


# ── the one-time move of the Star Map's mic settings ─────────────────────────

#: J's two files as they were on 2026-10-04.
J_STARMAP = {
    "galaxy": {"selected": "STANTON", "home": "STANTON"},
    "game_route": True,
    "ears": {"binding": None, "mode": "always", "model": "small.en"},
    "voice": {"replies": True},
}
J_ASSISTANT = {
    "binding": {"kind": "key", "code": "z"},
    "geom": [1435, -19, 520, 341],
    "voice_replies": True,
    "mic_mode": "push",
}


def test_js_always_on_is_not_dropped_and_does_not_override_his_assistant_setting():
    state = json.loads(json.dumps(J_ASSISTANT))
    notes = sb.migrate_starmap_voice(state, J_STARMAP, today="2026-10-04")
    # his own choice for this window stands ...
    assert state["mic_mode"] == "push"
    assert state["binding"] == {"kind": "key", "code": "z"}
    # ... the Star Map's value is kept on record, and he is TOLD, not left to find out
    rec = state[sb.MIGRATED_KEY]
    assert rec["mode"] == "always" and rec["mode_applied"] is False and rec["at"] == "2026-10-04"
    assert rec["model"] == "small.en" and rec["replies"] is True
    text = " ".join(notes)
    assert "Always on" in text and "Push-to-talk" in text
    assert "no longer opens the mic" in text
    assert state["whisper_model"] == "small.en"


def test_with_no_assistant_setting_the_star_map_mode_is_adopted():
    state = {}
    notes = sb.migrate_starmap_voice(state, J_STARMAP, today="2026-10-04")
    assert state["mic_mode"] == "always"
    assert state[sb.MIGRATED_KEY]["mode_applied"] is True
    assert state["voice_replies"] is True
    assert any("Always on" in n and "carried over" in n for n in notes)


def test_the_move_happens_once():
    state = json.loads(json.dumps(J_ASSISTANT))
    assert sb.migrate_starmap_voice(state, J_STARMAP, today="d")
    snapshot = json.dumps(state, sort_keys=True)
    assert sb.migrate_starmap_voice(state, J_STARMAP, today="later") == []
    assert json.dumps(state, sort_keys=True) == snapshot


def test_a_pilot_who_never_used_the_maps_voice_sees_nothing():
    state = {"mic_mode": "push"}
    assert sb.migrate_starmap_voice(state, {"galaxy": {}}, today="d") == []
    assert state[sb.MIGRATED_KEY]["found"] is False and state["mic_mode"] == "push"


def test_the_star_map_file_is_only_read(tmp_path):
    p = tmp_path / "starmap_state.json"
    p.write_text(json.dumps(J_STARMAP), encoding="utf-8")
    before = p.read_bytes()
    sm = sb.load_starmap_state(str(p))
    sb.migrate_starmap_voice({}, sm, today="d")
    assert p.read_bytes() == before
    assert sb.load_starmap_state(str(tmp_path / "missing.json")) == {}
    (tmp_path / "bad.json").write_text("[1, 2", encoding="utf-8")
    assert sb.load_starmap_state(str(tmp_path / "bad.json")) == {}


@pytest.mark.parametrize("starmap_binding, expect, told", [
    ({"kind": "keyboard", "code": 90, "label": "Z"}, {"kind": "key", "code": "z"}, "carried over"),
    ({"kind": "keyboard", "code": 53, "label": "5"}, {"kind": "key", "code": "5"}, "carried over"),
    ({"kind": "keyboard", "code": 113, "label": "F2"}, {"kind": "key", "code": "f2"}, "carried over"),
    ({"kind": "mouse", "code": "Button.x1", "label": "MOUSE X1"},
     {"kind": "mouse", "code": "Button.x1"}, "carried over"),
    # no certain equivalent: say so, do not guess
    ({"kind": "joystick", "code": 3, "label": "JOY 3", "joy_index": 0}, None, "cannot be used here"),
    ({"kind": "keyboard", "code": 192, "label": "`"}, None, "cannot be used here"),
    ({"kind": "mouse", "code": "Button.left", "label": "MOUSE LEFT"}, None, "cannot be used here"),
])
def test_a_star_map_mic_key_is_carried_over_only_when_the_mapping_is_certain(
        starmap_binding, expect, told):
    sm = {"ears": {"binding": starmap_binding, "mode": "push", "model": "base.en"}}
    state = {}
    notes = sb.migrate_starmap_voice(state, sm, today="d")
    assert state.get("binding") == expect
    assert any(told in n for n in notes), notes
    assert state["whisper_model"] == "base.en"


def test_stop_listening_is_the_assistants_command_now():
    """It was the Star Map's "ears off"; the mic is this window's, so the phrase is
    matched here and never sent to the agent or the map."""
    from assistant.panel import _STOP_LISTENING
    for said in ("Stop listening.", "ears off", "Ear off!", "please go to sleep", " stop listening "):
        assert _STOP_LISTENING.match(said), said
    for said in ("stop listening to the radio and find me a route", "what are ears", "zoom in"):
        assert not _STOP_LISTENING.match(said), said
    assert sb.command_text("stop listening") == ""
