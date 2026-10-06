"""Set route belongs to the Assistant: it sets a route with the Star Map closed.

J, 2026-10-04: "Can we move the set route functionality from the starmap to the
ai assistant?" Until then "navigate to Area 18" was relayed to the Star Map,
whose process ran the in-game macro, so it only worked with the map open.

NOTHING HERE TOUCHES THE GAME, THE MOUSE OR THE KEYBOARD. The input layer is
faked at one of two depths, stated per test:

  * a fake route setter handed to RouteService (records what it was asked to
    plot; proves who called it and when), or
  * fake ``pynput`` modules under the REAL InGameRouteSetter (records every key
    and click the real macro would have sent; proves the macro still runs from
    its new home, and that with the switch off it sends none of them).

There is no Star Map in any of these tests: the IPC bus says nothing is running
and fails the test if anything is sent to it, except where a test is about the
open-map case and says so.
"""
import json
import os
import subprocess
import sys
import threading
import types

import pytest

from assistant import agent as agent_mod
from assistant import answers, builtin_tools, ipc_bus, starmap_bridge
from assistant.builtin_tools import build_default_registry
from assistant.router import Router
from assistant.set_route import gate, phrases
from assistant.set_route.service import RouteService
from assistant.tools import ToolContext, ToolError

A_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
TOOLBOX = os.path.normpath(os.path.join(A_ROOT, "..", ".."))
PACKAGED_DATA = os.path.join(A_ROOT, "assistant", "data", "set_route")


# ── fakes ────────────────────────────────────────────────────────────────────

class _Gate:
    """The In-Game switch, in memory."""

    def __init__(self, on):
        self.on = on

    def in_game_enabled(self, legacy=None):
        return self.on


class _Setter:
    """Stands in for InGameRouteSetter: records, sends nothing."""

    def __init__(self, has_pynput=True, calibrated=True):
        self.plotted = []
        self._has_pynput = has_pynput
        self._calibrated = calibrated
        self.is_busy = False

    def available(self):
        return self._has_pynput

    def calibrated(self):
        return self._calibrated

    def busy(self):
        return self.is_busy

    def set_route(self, destination, status_cb=None, done_cb=None):
        self.plotted.append(destination)
        if status_cb:
            status_cb("opening the in-game map (F2)...")
        if done_cb:
            done_cb("route to %s plotted in game" % destination)
        return True


def _engine():
    """The real phonetic engine on the destination list packaged with the Assistant."""
    from assistant.set_route.destination_engine import DestinationPhoneticEngine
    return DestinationPhoneticEngine(PACKAGED_DATA)


class _Catalog:
    """Enough of the router's name catalogue: two star systems, nothing else."""
    alias = {"system": {"pyro": "Pyro", "stanton": "Stanton"}}

    def find(self, text):
        return []


@pytest.fixture
def no_star_map(monkeypatch):
    """No Star Map and no Everything Finder is running; sending to one is a failure."""
    sent = []
    monkeypatch.setattr(ipc_bus, "is_running", lambda sid: False)

    def _send(sid, cmd):
        sent.append((sid, cmd))
        raise AssertionError("something was sent to %s with no Star Map open: %r" % (sid, cmd))
    monkeypatch.setattr(ipc_bus, "send", _send)
    return sent


def _agent(in_game=True, setter=None, mode="router"):
    setter = setter if setter is not None else _Setter()
    spoken, status = [], []
    svc = RouteService(engine_factory=_engine, setter_factory=lambda: setter, gate=_Gate(in_game))
    ctx = ToolContext(base_dir=TOOLBOX, speak=spoken.append, status=status.append,
                      extra={"set_route": svc})
    reg = build_default_registry()
    a = agent_mod.AssistantAgent(reg, ctx, mode=mode)
    a.router = Router(reg, catalog=_Catalog())
    a.spoken, a.status, a.setter, a.svc = spoken, status, setter, svc
    return a


# ── which utterances are a route in the game ─────────────────────────────────

@pytest.mark.parametrize("said, dest", [
    ("Navigate to Area 18.", "area 18"),
    ("Set route to Port Tressler", "port tressler"),
    ("set the route to Port Tressler", "port tressler"),
    ("Set a route to Lorville", "lorville"),
    ("set course to Lorville", "lorville"),
    ("plot a course to Everus Harbor", "everus harbor"),
    ("please navigate to Area 18", "area 18"),
    ("Route to Area 18", "area 18"),
    # with the map's name in front it is still a route in the game, so still ours
    ("Star map, navigate to Area 18", "area 18"),
    ("tell the star map to set route to Baijini Point", "baijini point"),
])
def test_set_route_phrases_are_recognised(said, dest):
    assert phrases.destination(said, ["Pyro", "Stanton"]) == dest


@pytest.mark.parametrize("said", [
    "",
    "Route to Pyro",                                      # a jump route ON the map: the map's
    "route to stanton",
    "star map, route to Pyro",
    "What is the best trade route to Pyro for my Caterpillar?",
    "How many jumps from Stanton to Pyro",
    "clear route",                                        # only clears what the map drew
    "zoom in",
    "navigate",                                           # no destination
    "can you navigate to Area 18 later",                  # not at the start
])
def test_everything_else_is_not_a_set_route_request(said):
    assert phrases.destination(said, ["Pyro", "Stanton"]) == ""


def test_the_router_keeps_set_route_and_leaves_the_map_its_own_commands():
    r = Router(build_default_registry(), catalog=_Catalog())
    d = r.decide("Navigate to Area 18")
    assert (d.kind, d.tool, d.args) == ("call", "set_route", {"destination": "area 18"})
    # said to the map by name, it is still not relayed
    assert r.decide("Star map, set route to Port Tressler").tool == "set_route"
    # an open question from the turn before cannot swallow it
    d = r.decide("navigate to Lorville", pending={"tool": "ship_info", "args": {}, "missing": "ship"})
    assert d.tool == "set_route" and d.args == {"destination": "lorville"}
    # the map's own: a jump route across its galaxy view, and its view commands
    for said, cmd in (("Route to Pyro", "route to pyro"), ("clear route", "clear route"),
                      ("zoom in", "zoom in"), ("star map, show Hurston", "show hurston")):
        d = r.decide(said)
        assert (d.tool, d.args) == ("starmap_command", {"command": cmd}), said
    # "route to <a place that is not a system>" is a route in the game
    assert r.decide("route to Area 18").tool == "set_route"


def test_the_relay_no_longer_carries_set_route_phrases():
    for said in ("navigate to Area 18", "set route to Port Tressler", "set course to Lorville",
                 "plot a course to Everus Harbor"):
        assert starmap_bridge.command_text(said) == "", said
    assert starmap_bridge.command_text("zoom in") == "zoom in"


# ── the point: a route is set with no Star Map open ──────────────────────────

def test_the_assistant_sets_a_route_with_no_star_map(no_star_map):
    """Fake route setter. "Navigate to Area 18", then "yes": the Assistant's own
    code plots it. Nothing is relayed; no Star Map exists to relay to."""
    a = _agent(in_game=True)
    reply = a.handle_user_text("Navigate to Area 18")
    assert "Area18" in reply and "Say yes or no" in reply
    assert a.setter.plotted == [], "the game was touched before the pilot said yes"
    reply = a.handle_user_text("yes")
    assert a.setter.plotted == ["area18"]
    assert "isn't open" not in reply and reply.strip()
    # the macro's narration is said by the Assistant's own mouth
    assert "opening the in-game map (F2)..." in a.spoken
    assert "route to area18 plotted in game" in a.spoken
    assert no_star_map == []
    assert [t["tool"] for t in a.trace] == ["set_route"]


def test_a_spoken_alias_resolves_through_the_real_destination_list(no_star_map):
    a = _agent(in_game=True)
    a.handle_user_text("set route to area eighteen")
    a.handle_user_text("yes")
    assert a.setter.plotted == ["area18"]


def test_no_means_no(no_star_map):
    a = _agent(in_game=True)
    a.handle_user_text("Navigate to Area 18")
    reply = a.handle_user_text("no")
    assert a.setter.plotted == []
    assert reply == "Okay, I won't set the route to area18 in the game."


def test_moving_on_instead_of_answering_does_not_plot(no_star_map):
    a = _agent(in_game=True)
    a.handle_user_text("Navigate to Area 18")
    a.handle_user_text("clear route")            # neither yes nor no: a new request
    assert a.setter.plotted == []
    a.handle_user_text("yes")                    # there is nothing left to say yes to
    assert a.setter.plotted == []


def test_an_unknown_destination_and_an_ambiguous_one_ask_nothing_and_plot_nothing(no_star_map):
    a = _agent(in_game=True)
    reply = a.handle_user_text("navigate to flibbertigibbet zz")
    assert "yes or no" not in reply.lower() and a._pending_confirm is None
    assert a.setter.plotted == []

    class _Two:
        def find_destination(self, phrase):
            return None, ["port tressler", "port olisar"]
    a = _agent(in_game=True)
    a.svc._engine_factory = _Two
    reply = a.handle_user_text("navigate to port")
    assert "port tressler" in reply.lower() and "port olisar" in reply.lower()
    assert a._pending_confirm is None and a.setter.plotted == []


def test_what_it_says_when_it_cannot(no_star_map):
    a = _agent(in_game=True, setter=_Setter(has_pynput=False))
    reply = a.handle_user_text("Navigate to Area 18")
    assert "pynput" in reply and a._pending_confirm is None and a.setter.plotted == []

    busy = _Setter()
    busy.is_busy = True
    a = _agent(in_game=True, setter=busy)
    reply = a.handle_user_text("Navigate to Area 18")
    assert "already setting a route" in reply and a.setter.plotted == []

    def _broken():
        raise OSError("destinations.json is gone")
    a = _agent(in_game=True)
    a.svc._engine_factory = _broken
    reply = a.handle_user_text("Navigate to Area 18")
    assert "destination list could not be loaded" in reply and a.setter.plotted == []


def test_an_open_star_map_is_asked_to_show_the_place_and_nothing_more(monkeypatch):
    """The open-map case: the route is still set HERE; the map only follows along."""
    sent = []
    monkeypatch.setattr(ipc_bus, "is_running", lambda sid: sid == "starmap")
    monkeypatch.setattr(ipc_bus, "send", lambda sid, cmd: sent.append((sid, cmd)) or True)
    a = _agent(in_game=True)
    a.handle_user_text("Navigate to Area 18")
    assert sent == [("starmap", {"type": "map_goto", "name": "area18"})]
    a.handle_user_text("yes")
    assert a.setter.plotted == ["area18"]
    assert len(sent) == 1, "the route was relayed to the map as well as set here"


# ── the In-Game switch ───────────────────────────────────────────────────────

def test_in_game_off_nothing_is_asked_and_nothing_is_sent(no_star_map):
    a = _agent(in_game=False)
    reply = a.handle_user_text("Navigate to Area 18")
    assert "In-game plotting is off" in reply
    assert "yes or no" not in reply.lower() and a._pending_confirm is None
    a.handle_user_text("yes")
    assert a.setter.plotted == []


def test_in_game_off_the_plot_tool_itself_refuses():
    """What a model in "llm" mode could do: call plot_route_in_game directly. The
    yes/no is the agent's; the switch is checked again inside the service."""
    a = _agent(in_game=False)
    plot = a.registry.get("plot_route_in_game")
    assert plot.confirm is True
    out = plot.run(a.ctx, {"destination": "area18"})
    assert out["started"] is False and "In-game plotting is off" in out["reply"]
    assert a.setter.plotted == []


def test_in_game_off_the_route_setter_is_never_even_built():
    built = []                       # a flag, not a raise: setter() swallows a factory error

    def _factory():
        built.append(1)
        return _Setter()
    svc = RouteService(engine_factory=_engine, setter_factory=_factory, gate=_Gate(False))
    started, msg = svc.plot("area18")
    assert started is False and "toggle 'In-Game'" in msg
    assert built == [], "the in-game route setter was built with In-Game off"


def test_the_switch_is_read_at_plot_time_not_when_the_question_was_asked(no_star_map):
    """Turned off between "navigate to ..." and "yes": the yes plots nothing."""
    a = _agent(in_game=True)
    a.handle_user_text("Navigate to Area 18")
    a.svc._gate.on = False
    reply = a.handle_user_text("yes")
    assert a.setter.plotted == []
    assert "In-game plotting is off" in reply


def test_the_plot_tool_types_only_destinations_the_list_knows():
    a = _agent(in_game=True)
    plot = a.registry.get("plot_route_in_game")
    with pytest.raises(ToolError):
        plot.run(a.ctx, {"destination": "flibbertigibbet zz; rm -rf"})
    assert a.setter.plotted == []
    out = plot.run(a.ctx, {"destination": "area eighteen"})     # resolved, then plotted
    assert out["started"] is True and a.setter.plotted == ["area18"]


def test_set_route_is_asked_about_and_never_rephrased_by_a_model():
    reg = build_default_registry()
    assert reg.get("set_route").confirm is False            # it only looks a name up
    assert reg.get("plot_route_in_game").confirm is True    # this one touches the game
    assert reg.get("plot_route_in_game").describe_action({"destination": "area18"}) == \
        "set the route to area18 in the game"
    assert answers.plain_answer("set_route", {}, {"reply": "Area18."}) == "Area18."
    assert answers.plain_answer("plot_route_in_game", {}, {"reply": "Plotting area18"}) == "Plotting area18."


# ── the switch on disk (gate.py) ─────────────────────────────────────────────

@pytest.fixture
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert gate.settings_path().startswith(str(tmp_path)), "the test would write J's real file"
    return tmp_path


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj))


def test_the_switch_is_off_until_someone_turns_it_on(home):
    assert gate.in_game_enabled() is False
    assert gate.set_in_game(True) is True
    assert gate.in_game_enabled() is True
    assert gate.set_in_game(False) is True
    assert gate.in_game_enabled() is False


def test_the_star_maps_saved_choice_carries_over_until_the_switch_is_set_here(home):
    _write(gate.legacy_path(), {"game_route": True, "galaxy": {}})
    assert gate.in_game_enabled() is True               # J had In-Game on in the Star Map
    gate.set_in_game(False)
    assert gate.in_game_enabled() is False              # his newer choice wins from now on
    assert gate.in_game_enabled(legacy=True) is False
    with open(gate.legacy_path(), encoding="utf-8") as fh:
        assert json.load(fh) == {"game_route": True, "galaxy": {}}, "the Star Map's file was written"


def test_a_switch_file_that_cannot_be_read_means_off(home):
    _write(gate.legacy_path(), {"game_route": True})
    _write(gate.settings_path(), "{ not json")
    assert gate.in_game_enabled() is False
    assert gate.in_game_enabled(legacy=True) is False
    _write(gate.settings_path(), '["in_game", true]')
    assert gate.in_game_enabled() is False
    _write(gate.settings_path(), {"in_game": "yes"})    # only a real true turns it on
    assert gate.in_game_enabled() is False


# ── the real macro, from its new home, on a fake input layer ─────────────────

class _FakeInput:
    """Fake ``pynput``: every key and click the macro sends lands in ``events``."""

    def __init__(self):
        self.events = []
        ev = self.events

        class _KeyboardController:
            def press(self, key):
                ev.append(("press", key))

            def release(self, key):
                ev.append(("release", key))

            def pressed(self, key):
                class _Held:
                    def __enter__(self_inner):
                        ev.append(("hold", key))

                    def __exit__(self_inner, *exc):
                        ev.append(("unhold", key))
                return _Held()

        class _MouseController:
            position = (0, 0)

            def click(self, button, count):
                ev.append(("click", tuple(self.position)))

            def scroll(self, dx, dy):
                ev.append(("scroll", dy))

        kb, ms = types.ModuleType("pynput.keyboard"), types.ModuleType("pynput.mouse")
        kb.Controller, kb.Key = _KeyboardController, types.SimpleNamespace(f2="F2", ctrl="CTRL")
        ms.Controller, ms.Button = _MouseController, types.SimpleNamespace(left="LEFT")
        pp = types.ModuleType("pynput")
        pp.keyboard, pp.mouse = kb, ms
        self.modules = {"pynput": pp, "pynput.keyboard": kb, "pynput.mouse": ms}

    def install(self, monkeypatch):
        for name, mod in self.modules.items():
            monkeypatch.setitem(sys.modules, name, mod)
        return self


@pytest.fixture
def fake_input(monkeypatch):
    pytest.importorskip("PySide6.QtWidgets")          # route_setter imports it for its dialog
    from assistant.set_route import route_setter
    fake = _FakeInput().install(monkeypatch)
    fake.clipboard = []
    monkeypatch.setattr(route_setter.time, "sleep", lambda _s: None)
    monkeypatch.setattr(route_setter, "_set_clipboard", fake.clipboard.append)
    monkeypatch.setattr(route_setter, "load_calibration", lambda: {
        "search_bar": (11, 12), "destination": (21, 22), "map_center": (31, 32)})
    return fake


def _plot_and_wait(svc, dest):
    done, said = threading.Event(), []

    def _done(msg):
        said.append(msg)
        done.set()
    started, msg = svc.plot(dest, status_cb=said.append, done_cb=_done)
    if started:
        assert done.wait(5), "the macro thread did not finish"
    return started, msg, said


def test_the_real_macro_runs_from_the_assistant_on_a_fake_input_layer(fake_input):
    """Fake pynput + fake clipboard under the real InGameRouteSetter."""
    svc = RouteService(engine_factory=_engine, gate=_Gate(True))
    started, msg, said = _plot_and_wait(svc, "area18")
    assert started and msg == "setting route to area18 in game"
    ev = fake_input.events
    assert ev[0] == ("press", "F2") and ev[-1] == ("release", "F2")
    assert [e for e in ev if e[0] == "click"] == [
        ("click", (31, 32)), ("click", (11, 12)), ("click", (21, 22)), ("click", (31, 32))]
    assert fake_input.clipboard == ["area18"]
    assert ("hold", "CTRL") in ev and ("press", "v") in ev
    assert ev.count(("press", "r")) == 6
    assert said[-1] == "route to area18 plotted in game"
    assert type(svc.setter()).__module__ == "assistant.set_route.route_setter"


def test_with_in_game_off_the_real_macro_sends_no_input_at_all(fake_input):
    svc = RouteService(engine_factory=_engine, gate=_Gate(False))
    started, _msg, _said = _plot_and_wait(svc, "area18")
    assert started is False
    assert fake_input.events == [] and fake_input.clipboard == []


def test_the_default_service_uses_the_switch_on_disk(fake_input, home):
    """No injected gate: the tools' own RouteService, the saved switch, the real macro."""
    svc = RouteService(engine_factory=_engine)
    assert _plot_and_wait(svc, "area18")[0] is False and fake_input.events == []
    gate.set_in_game(True)
    assert _plot_and_wait(svc, "area18")[0] is True and fake_input.events


def test_the_tools_build_their_own_service_when_the_host_gives_none(monkeypatch):
    monkeypatch.setattr(builtin_tools, "_default_route_service", None)
    svc = builtin_tools._routes(ToolContext(base_dir=TOOLBOX))
    assert isinstance(svc, RouteService)
    assert builtin_tools._routes(ToolContext(base_dir=TOOLBOX)) is svc


# ── one copy ─────────────────────────────────────────────────────────────────

#: J's original WingmanAI skill. It runs inside WingmanAI, not the toolbox, and is
#: the upstream this code was ported from; the toolbox shares its data folder.
_WINGMAN_SKILL = "tools/set_route_ai/"
_HOME = "tools/Assistant/assistant/set_route/"


def _tracked_python():
    """Every .py git knows or would add (tracked + untracked, not ignored): a new
    second copy must be seen before it is ever committed."""
    try:
        out = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard",
                              "--", "*.py"], cwd=TOOLBOX,
                             capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pytest.skip("git is not available: cannot list the toolbox's files")
    if out.returncode != 0:
        pytest.skip("not a git checkout: cannot list the toolbox's files")
    files = [f for f in out.stdout.decode("utf-8").split("\0") if f]
    assert len(files) > 100, "git listed almost nothing; this check would pass on an empty list"
    return [f for f in files if os.path.isfile(os.path.join(TOOLBOX, f))]


def test_there_is_one_set_route_implementation_and_everything_imports_it():
    import re
    defines = re.compile(r"^class (InGameRouteSetter|DestinationPhoneticEngine|RouteCalibrationDialog)\b",
                         re.MULTILINE)
    imports = re.compile(r"^\s*(?:from\s+\S*\b(?:route_setter|destination_engine)\b\s+import|"
                         r"import\s+\S*\b(?:route_setter|destination_engine)\b|"
                         r"from\s+\S+\s+import\s+[^\n]*\b(?:route_setter|destination_engine)\b)",
                         re.MULTILINE)
    where, importers = {}, []
    for rel in _tracked_python():
        if rel.startswith(_WINGMAN_SKILL):
            continue
        with open(os.path.join(TOOLBOX, rel), encoding="utf-8", errors="replace") as fh:
            src = fh.read()
        for name in defines.findall(src):
            where.setdefault(name, []).append(rel)
        if imports.search(src) and not rel.startswith(_HOME):
            importers.append(rel)
    assert where == {
        "InGameRouteSetter": [_HOME + "route_setter.py"],
        "DestinationPhoneticEngine": [_HOME + "destination_engine.py"],
        "RouteCalibrationDialog": [_HOME + "route_setter.py"],
    }, where
    # outside the package, only the Assistant's own window (its Calibrate button), tests, and the installer
    # build's check of the staged copy (it proves that nothing is clicked with no calibration)
    allowed = {"tools/Assistant/assistant/panel.py", "build/check_assistant_stage.py"}
    stray = [f for f in importers if f not in allowed and "/tests/" not in f]
    assert stray == [], "set-route code is imported around the service / the link: %s" % stray
    assert not os.path.exists(os.path.join(TOOLBOX, "skills", "Starmap", "starmap", "set_route"))
    assert not os.path.exists(os.path.join(TOOLBOX, "skills", "Starmap", "starmap", "data", "set_route"))


def test_only_the_service_starts_the_macro():
    """InGameRouteSetter().set_route is the one call that sends input. It appears in
    exactly one place outside its own file: RouteService.plot, behind the switch."""
    import re
    builds = re.compile(r"\bInGameRouteSetter\s*\(")
    users = []
    for rel in _tracked_python():
        if rel.startswith(_WINGMAN_SKILL) or "/tests/" in rel:
            continue
        with open(os.path.join(TOOLBOX, rel), encoding="utf-8", errors="replace") as fh:
            if builds.search(fh.read()):
                users.append(rel)
    assert users == [_HOME + "service.py"], users


def test_the_packaged_data_moved_with_the_code():
    for name in ("destinations.json", "phonetic_learning.json", "blacklist.json"):
        assert os.path.isfile(os.path.join(PACKAGED_DATA, name)), name
    from assistant.set_route import destination_engine, route_setter
    # five folders up from the module is the toolbox root, as its data lookups assume
    assert os.path.normpath(destination_engine._TOOLBOX_ROOT) == TOOLBOX
    assert os.path.normpath(route_setter._TOOLBOX_ROOT) == TOOLBOX
    live = os.path.join(TOOLBOX, "tools", "set_route_ai", "data")
    expect = live if os.path.isdir(live) else PACKAGED_DATA
    assert os.path.normpath(destination_engine.default_data_dir()) == os.path.normpath(expect)


# ── calibration: the pilot's own file, and nothing is clicked without one ────

@pytest.fixture
def new_install(monkeypatch, home, tmp_path):
    """An empty home folder and no WingmanAI skill beside the toolbox: what an installed copy starts with.
    The real macro on a fake input layer; load_calibration is the real one."""
    pytest.importorskip("PySide6.QtWidgets")
    from assistant.set_route import route_setter
    monkeypatch.setattr(route_setter, "shared_calibration_path",
                        lambda: str(tmp_path / "no_wingman_skill" / "mouse_calibration.json"))
    fake = _FakeInput().install(monkeypatch)
    fake.clipboard = []
    monkeypatch.setattr(route_setter.time, "sleep", lambda _s: None)
    monkeypatch.setattr(route_setter, "_set_clipboard", fake.clipboard.append)
    fake.route_setter = route_setter
    fake.own = os.path.join(str(home), ".sctoolbox", "set_route", "mouse_calibration.json")
    return fake


def test_with_no_calibration_the_service_refuses_and_nothing_is_sent(new_install):
    from assistant.set_route import service
    rs = new_install.route_setter
    assert rs.load_calibration() is None
    svc = RouteService(engine_factory=_engine, gate=_Gate(True))
    assert svc.why_not("area18") == service.NOT_CALIBRATED
    started, msg, _said = _plot_and_wait(svc, "area18")
    assert started is False and msg == service.NOT_CALIBRATED
    assert new_install.events == [] and new_install.clipboard == []


def test_with_no_calibration_the_macro_itself_refuses(new_install):
    """Past the service: the function that moves the mouse does not run without the pilot's positions."""
    rs = new_install.route_setter
    done, said = threading.Event(), []

    def _done(msg):
        said.append(msg)
        done.set()
    assert rs.InGameRouteSetter().set_route("area18", status_cb=said.append, done_cb=_done) is True
    assert done.wait(5), "the macro thread did not finish"
    assert said == ["error: " + rs.NOT_CALIBRATED]
    assert new_install.events == [] and new_install.clipboard == []


def test_with_no_calibration_the_pilot_is_told_to_calibrate_and_not_asked_yes_or_no(no_star_map):
    a = _agent(in_game=True, setter=_Setter(calibrated=False))
    reply = a.handle_user_text("Navigate to Area 18")
    assert "Calibrate Route" in reply and "Nothing was sent to the game" in reply
    assert "yes or no" not in reply.lower()
    a.handle_user_text("yes")
    out = a.registry.get("plot_route_in_game").run(a.ctx, {"destination": "area18"})
    assert out["started"] is False and "Calibrate Route" in out["reply"]
    assert a.setter.plotted == []
    # the line is spoken with the microphone possibly open: it must not be the phrase that starts a calibration
    assert not phrases.is_calibrate(builtin_tools.NOT_CALIBRATED)


def test_a_calibration_is_saved_in_the_pilots_folder_and_then_the_macro_runs(new_install):
    rs = new_install.route_setter
    before = sorted(os.listdir(PACKAGED_DATA))
    rs.save_calibration((11, 12), (21, 22), (31, 32))
    assert os.path.isfile(new_install.own)
    assert os.path.normpath(rs.calibration_path()) == os.path.normpath(new_install.own)
    assert rs.load_calibration() == {"search_bar": (11, 12), "destination": (21, 22), "map_center": (31, 32)}
    assert sorted(os.listdir(PACKAGED_DATA)) == before, "the calibration was written beside the code"
    svc = RouteService(engine_factory=_engine, gate=_Gate(True))
    started, _msg, said = _plot_and_wait(svc, "area18")
    assert started and said[-1] == "route to area18 plotted in game"
    assert [e for e in new_install.events if e[0] == "click"] == [
        ("click", (31, 32)), ("click", (11, 12)), ("click", (21, 22)), ("click", (31, 32))]


@pytest.mark.parametrize("content", [
    "not json",
    {"version": 2, "starmap": {"search_bar": {"x": 1, "y": 2}, "destination": {"x": 3, "y": 4},
                               "map_center": {"x": 5, "y": 6}}},
    {"version": 1, "starmap": {"search_bar": {"x": 1, "y": 2}, "destination": {"x": 3, "y": 4}}},
    {"version": 1, "starmap": {"search_bar": {"x": 1, "y": 2}, "destination": {"x": 3},
                               "map_center": {"x": 5, "y": 6}}},
    {"version": 1, "starmap": {"search_bar": {"x": "left", "y": 2}, "destination": {"x": 3, "y": 4},
                               "map_center": {"x": 5, "y": 6}}},
    {"version": 1},
    [1, 2, 3],
])
def test_a_calibration_that_is_not_whole_is_no_calibration(new_install, content):
    """No position is ever filled in from a default: a file missing one is the same as no file."""
    _write(new_install.own, content)
    assert new_install.route_setter.load_calibration() is None


def test_the_wingman_skills_calibration_is_read_until_the_pilot_has_their_own(new_install, monkeypatch, tmp_path):
    rs = new_install.route_setter
    shared = str(tmp_path / "wingman" / "mouse_calibration.json")
    _write(shared, {"version": 1, "starmap": {"search_bar": {"x": 1, "y": 2}, "destination": {"x": 3, "y": 4},
                                              "map_center": {"x": 5, "y": 6}}})
    monkeypatch.setattr(rs, "shared_calibration_path", lambda: shared)
    with open(shared, "rb") as fh:
        before = fh.read()
    assert rs.load_calibration() == {"search_bar": (1, 2), "destination": (3, 4), "map_center": (5, 6)}
    rs.save_calibration((7, 8), (9, 10), (11, 12))
    assert rs.load_calibration() == {"search_bar": (7, 8), "destination": (9, 10), "map_center": (11, 12)}
    with open(shared, "rb") as fh:
        assert fh.read() == before, "the WingmanAI skill's file was written"


def test_no_calibration_is_packaged_with_the_assistant():
    """A calibration is one screen's click positions. None travels with the code."""
    assert not os.path.exists(os.path.join(PACKAGED_DATA, "mouse_calibration.json"))
    from assistant.set_route import route_setter
    assert not os.path.normcase(os.path.abspath(route_setter.user_calibration_path())).startswith(
        os.path.normcase(TOOLBOX) + os.sep)
