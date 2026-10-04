"""Pico Pals as a launcher tile.

J, 2026-10-04: "I also don't see pico pals as a tool".  There was no tools/Pico/skill.json, so the
registry never saw him; and sprite_pal.py's own command line rejects what the launcher passes to
every tool, so a tile pointed straight at it would have died on start.  tools/Pico/skill.json and
pico_pals_app.py (the adapter) are the fix, and these tests hold both ends:

    the registry        Pico Pals is discovered, has a tile, and its hotkey collides with nothing
    the argument list   the launcher's own code builds it and the launcher's own process class
                        starts it: not argparse's exit 2, and show / hide / quit all work
    no art              no loops on disk -> exit 3 and one plain sentence in the log the launcher
                        shows, never a tile that silently does nothing

These live in tools/Pico/tests, NOT tools/Pico/pico/tests: the `pico` package is the pure-logic
layer and its selftest (check_no_qt) fails if any file under it so much as names Qt, which the
probe beside this file has to.

NOTHING HERE TOUCHES THE USER'S FILES OR DESKTOP.  Every child runs on Qt's offscreen platform (no
window can appear), with USERPROFILE / HOME / APPDATA pointed into tmp_path: Pico's settings
(%APPDATA%/PicoPal), the toolbox's shared settings (~/.sctoolbox), the loops (~/BrAi/...) and the
Game.log are all fakes built under tmp_path, and the launcher's per-tool log goes there too.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1]            # tools/Pico
REPO = TOOL.parents[1]
for p in (str(REPO), str(TOOL)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pico_pals_app  # noqa: E402
from core.process_manager import ManagedProcess  # noqa: E402
from core.skill_registry import discover_skills, resolve_script_path, tile_skills  # noqa: E402
from shared.config_models import LauncherSettings  # noqa: E402
from pico import sprites  # noqa: E402

ADAPTER = TOOL / "pico_pals_app.py"
PROBE = Path(__file__).resolve().parent / "_pico_pals_probe.py"

# The smallest valid GIF: one 1x1 frame.  Enough for Catalog.scan and QMovie; no art is needed to
# prove the window starts and obeys the launcher.
TINY_GIF = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01\x00\x00\x00\x00"
            b",\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;")


# ── fixtures ────────────────────────────────────────────────────────────────

class _RecordingPM:
    """Stands in for ProcessManager while the launcher's own registration code runs, to capture the
    argument list it would pass.  Nothing is started."""

    def __init__(self):
        self.registered: dict[str, dict] = {}

    def register(self, skill_id, python_exe, script, cwd, args, base_dir, env=None):
        self.registered[skill_id] = {"script": script, "cwd": cwd, "args": list(args)}

    def get(self, skill_id):
        return None


def launcher_registration(custom_args: list[str] | None = None) -> dict:
    """What skill_launcher.py registers for Pico: {"script", "cwd", "args"}.

    Produced by the launcher's real _register_skills(), not rebuilt here, so if the launcher ever
    changes what it passes, this test starts Pico with the new list and finds out."""
    import skill_launcher

    app = skill_launcher.SCToolboxApp.__new__(skill_launcher.SCToolboxApp)
    app._skills = [s for s in discover_skills(str(REPO)) if s.id == "pico"]
    assert app._skills, "the registry has no skill with id 'pico'"
    if custom_args is not None:
        app._skills[0].custom_args = list(custom_args)
    app._settings = LauncherSettings.from_dict({}, app._skills)
    app._python = sys.executable
    app._availability = {}
    app._pm = _RecordingPM()
    app._enqueue = lambda fn: None
    app._auto_hide_check = lambda: None
    app._register_skills({})
    return app._pm.registered["pico"]


@pytest.fixture(autouse=True)
def logs_under_tmp(tmp_path, monkeypatch):
    """shared.logging_config caches the first logs/ folder it is asked for, for the whole process.
    Pin it to this test's tmp_path, so a tool's captured output can never land in the repo's
    logs/pico.log (or in another test's folder) whatever ran earlier in the session."""
    from shared import logging_config

    (tmp_path / "logs").mkdir()
    monkeypatch.setattr(logging_config, "_LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture()
def fake_home(tmp_path):
    """A home folder holding everything Pico reads, all fake: loops, the toolbox's saved game
    folder with a Game.log in it, and an empty APPDATA."""
    home = tmp_path / "home"
    loops = home / "BrAi" / "_forJ" / "VNCCS" / "pico_anim_sequences"
    loops.mkdir(parents=True)
    for pool in sprites.MOOD_LOOPS.values():
        for name in pool:
            if sprites.SNAP_SEP not in name:
                (loops / (name + ".gif")).write_bytes(TINY_GIF)
    game = home / "FakeGame" / "StarCitizen"
    (game / "LIVE").mkdir(parents=True)
    (game / "LIVE" / "Game.log").write_text("<2026-10-04T00:00:00.000Z> fixture line\n", encoding="utf-8")
    (home / ".sctoolbox").mkdir()
    (home / ".sctoolbox" / "shared_settings.json").write_text(
        json.dumps({"sc_root": str(game).replace("\\", "/")}), encoding="utf-8")
    (home / "AppData").mkdir()
    return home


def child_env(home: Path, state: Path | None = None) -> dict:
    env = {
        "QT_QPA_PLATFORM": "offscreen",          # no window can reach the desktop
        "QT_SCALE_FACTOR": "",                   # "" = unset, in ManagedProcess's env convention
        "USERPROFILE": str(home), "HOME": str(home), "APPDATA": str(home / "AppData"),
        "HOMEDRIVE": "", "HOMEPATH": "",
        "PYTHONIOENCODING": "utf-8",
    }
    if state is not None:
        env["PICO_PROBE_STATE"] = str(state)
    return env


def managed(script: Path, args: list[str], base: Path, env: dict) -> ManagedProcess:
    """The launcher's own process wrapper, exactly as ProcessManager.register builds it, except
    that the per-tool log (base_dir/logs/pico.log) lands under tmp_path."""
    return ManagedProcess(skill_id="pico", python_exe=sys.executable, script=str(script),
                          cwd=str(TOOL), args=list(args), base_dir=str(base), env=env)


def wait_for(cond, timeout: float = 30.0, what: str = "condition"):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(0.05)
    raise AssertionError("timed out after %.0fs waiting for %s" % (timeout, what))


def read_state(state: Path) -> dict | None:
    try:
        return json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def log_text(base: Path) -> str:
    try:
        return (base / "logs" / "pico.log").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def kill(mp: ManagedProcess) -> None:
    proc = mp._proc
    if proc is not None and proc.poll() is None:
        proc.kill()
        proc.wait(timeout=10)
    if mp._log_file not in (None, subprocess.DEVNULL):
        mp._log_file.close()


# ── the registry ─────────────────────────────────────────────────────────────

def test_pico_pals_is_discovered_and_gets_a_tile():
    skills = discover_skills(str(REPO))
    by_id = {s.id: s for s in skills}
    assert "pico" in by_id, "no Pico tile: tools/Pico/skill.json is missing or invalid"
    pico = by_id["pico"]
    assert str(pico.name) == "Pico Pals"
    assert pico.hidden is False
    assert pico.id in [s.id for s in tile_skills(skills)]
    assert pico.script == "pico_pals_app.py", (
        "the tile must run the adapter; sprite_pal.py rejects the launcher's arguments")
    assert resolve_script_path(pico, str(REPO)) == str(ADAPTER)


def test_pico_pals_hotkey_collides_with_no_other_tool_or_the_launcher():
    from pynput.keyboard import HotKey

    skills = discover_skills(str(REPO))
    combos: dict[frozenset, str] = {frozenset(str(k) for k in HotKey.parse("<ctrl>+0")): "the launcher"}
    for s in skills:
        if not s.hotkey:
            continue
        combo = frozenset(str(k) for k in HotKey.parse(s.hotkey))
        assert combo not in combos, f"{s.name} and {combos[combo]} both default to {s.hotkey}"
        combos[combo] = str(s.name)
    assert {s.id: s for s in skills}["pico"].hotkey, "Pico Pals was given a hotkey; it is gone"


# ── the argument list ────────────────────────────────────────────────────────

def test_what_the_launcher_passes_is_geometry_opacity_and_nothing_pico_can_use():
    args = launcher_registration()["args"]
    assert len(args) == 5, args                       # x y w h opacity; the command file is appended at start
    assert [int(a) for a in args[:4]] and float(args[4]) > 0


@pytest.mark.parametrize("argv, want", [
    (["100", "100", "1300", "800", "0.95", "C:/t/cmd.jsonl"], ([], "C:/t/cmd.jsonl")),
    (["100", "100", "1300", "800", "--demo", "0.95", "C:/t/cmd.jsonl"], (["--demo"], "C:/t/cmd.jsonl")),
    (["-5", "0", "1300", "800", "--loops", "D:/x", "1.0", "c.jsonl"], (["--loops", "D:/x"], "c.jsonl")),
    (["100", "100", "1300", "800", "0.95"], ([], None)),
    (["--demo"], (["--demo"], None)),
    (["--mood", "happy"], (["--mood", "happy"], None)),
    ([], ([], None)),
])
def test_split_launcher_argv(argv, want):
    assert pico_pals_app.split_launcher_argv(argv) == want


def test_sprite_pal_alone_dies_on_the_launchers_arguments(fake_home, tmp_path):
    """The control: this is the failure the adapter exists to prevent.  If sprite_pal.py ever
    learns to take these arguments itself, this test says the adapter can go."""
    reg = launcher_registration()
    mp = managed(TOOL / "sprite_pal.py", reg["args"], tmp_path, child_env(fake_home))
    try:
        assert mp.start()
        proc = mp._proc
        assert proc.wait(timeout=60) == 2, log_text(tmp_path)
        assert "unrecognized arguments" in log_text(tmp_path)
    finally:
        kill(mp)


def test_the_tile_starts_pico_and_show_hide_quit_work(fake_home, tmp_path):
    """The launcher's exact argument list, started by the launcher's own ManagedProcess."""
    reg = launcher_registration()
    assert Path(reg["script"]) == ADAPTER and Path(reg["cwd"]) == TOOL
    state = tmp_path / "state.json"
    mp = managed(PROBE, reg["args"], tmp_path, child_env(fake_home, state))
    try:
        assert mp.start()
        proc = mp._proc
        first = wait_for(lambda: read_state(state) or (proc.poll() is not None and {"dead": True}),
                         what="Pico's window")
        assert "dead" not in first, "Pico exited on start (rc=%s):\n%s" % (proc.returncode, log_text(tmp_path))
        assert first["visible"] is True
        # the adapter passed sprite_pal.py the Game.log of the install the toolbox knows about
        assert first["pal_args"] == ["--log", str(fake_home / "FakeGame" / "StarCitizen" / "LIVE"
                                                 / "Game.log").replace("\\", "/")]

        # Customise opens 0.3s after every start (PICO_CONTRACT.md) and is modal. The launcher's
        # commands must still get through while it is open, so wait for it before sending any.
        wait_for(lambda: (read_state(state) or {}).get("modal") is True, what="the Customise box")

        mp.hide()                                    # what a second click on the tile sends
        wait_for(lambda: (read_state(state) or {}).get("visible") is False, what="hide")
        assert proc.poll() is None, "hide must not end the process"
        mp.show()
        wait_for(lambda: (read_state(state) or {}).get("visible") is True, what="show")

        mp.stop(timeout=15)                          # launcher shutdown: IPC quit, then terminate
        assert proc.returncode == 0, "Pico did not quit when asked (rc=%s), it had to be killed:\n%s" % (
            proc.returncode, log_text(tmp_path))
        assert "unrecognized arguments" not in log_text(tmp_path)
    finally:
        kill(mp)


def test_custom_args_reach_sprite_pal(fake_home, tmp_path):
    """skill.json custom_args sit between the geometry and the opacity; the adapter hands them on."""
    reg = launcher_registration(custom_args=["--demo"])
    assert reg["args"][4] == "--demo"
    state = tmp_path / "state.json"
    mp = managed(PROBE, reg["args"], tmp_path, child_env(fake_home, state))
    try:
        assert mp.start()
        proc = mp._proc
        first = wait_for(lambda: read_state(state) or (proc.poll() is not None and {"dead": True}),
                         what="Pico's window")
        assert "dead" not in first, log_text(tmp_path)
        assert first["pal_args"] == ["--demo"], "a demo run must not be given a Game.log"
        mp.stop(timeout=15)
        assert proc.returncode == 0, log_text(tmp_path)
    finally:
        kill(mp)


# ── no art ───────────────────────────────────────────────────────────────────

def test_no_loops_on_disk_fails_loudly_not_silently(tmp_path):
    """A machine without Pico's art (it is not in the repo): exit 3 and a sentence in the log.
    A non-zero exit is what makes the launcher open that log for the user (unexpectedly_died)."""
    empty_home = tmp_path / "empty_home"
    (empty_home / "AppData").mkdir(parents=True)
    reg = launcher_registration()
    mp = managed(ADAPTER, reg["args"], tmp_path, child_env(empty_home))
    try:
        assert mp.start()
        proc = mp._proc
        assert proc.wait(timeout=60) == pico_pals_app.EXIT_NO_ART, log_text(tmp_path)
        assert mp.unexpectedly_died, "the launcher would not show the reason"
        text = log_text(tmp_path)
        assert "Pico Pals cannot start" in text and "animation loops" in text, text
        assert "Traceback" not in text and "unrecognized arguments" not in text, text
    finally:
        kill(mp)
