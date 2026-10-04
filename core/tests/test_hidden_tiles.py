"""A tool declared ``hidden`` loses its launcher TILE and nothing else.

J, 2026-10-04: "can you hide trade hub/ item finder and starmap since those are now taken care of
in a single tool" (the Everything Finder, which has them as tabs) and "also hide craft database".

Hidden is a declared property of the tool (SkillConfig.hidden, set in core/skill_registry.py's
built-in list or in the tool's skill.json).  It is NOT the Settings "Enabled / Disabled" switch
(LauncherSettings.disabled_skills), which also takes away the hotkey and the preload.  These tests
pin the difference from every side that reaches a tool:

    the launcher grid          no tile for the four, a tile for every other tool
    the registry               still returns all four, with a script on disk
    the launcher's processes   still registered, so IPC launch_skill opens them
    global hotkeys             still bound (hiding a tile must not silently kill a shortcut)
    the Assistant              still resolves "open the star map" and can still spawn it
    the Everything Finder      builds its tabs by importing the tools, never through the registry

Every assertion here was watched failing against a deliberately broken copy of the code before it
was trusted (see the commit message for which break kills which test).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

REPO = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
sys.path.insert(0, REPO)

from core.skill_registry import discover_skills, resolve_script_path, tile_skills  # noqa: E402
from shared.config_models import LauncherSettings, SkillConfig  # noqa: E402

PROBE = os.path.join(REPO, "core", "tests", "_launcher_grid_probe.py")

# id -> the name on the tile J asked to be hidden
HIDDEN = {"market": "Item Finder", "trade": "Trade Hub", "starmap": "Starmap", "craft_db": "Craft Database"}
# Tiles that were on J's screenshot and must still be there.  "missions" is Mission/Craft DB: a
# different tool from Craft Database, and deliberately left alone.
STILL_TILES = {"dps", "cargo", "missions", "mining", "everything_finder", "battle_buddy",
               "mouse_blocker", "dev_history", "mining_signals", "playtime", "assistant"}
# Hidden for a different reason, later the same day: SuitMk2 became a tab of the Toolbox Assistant window
# (SkillConfig.tab_of).  It has no tile AND no process of its own, so the rules below that say "hidden
# changes nothing but the tile" are not about it; core/tests/test_tab_tools.py is.
TAB_TOOLS = {"suitmk2"}


def _skill(sid: str, **kw) -> SkillConfig:
    return SkillConfig(id=sid, name=sid.title(), icon="*", color="#fff", folder=sid, script="app.py", **kw)


def _probe(*args: str) -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("QT_SCALE_FACTOR", None)
    proc = subprocess.run([sys.executable, PROBE, *args], cwd=REPO, env=env,
                          capture_output=True, text=True, encoding="utf-8", timeout=180)
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("{")]
    assert proc.returncode == 0 and lines, (
        f"probe failed (rc={proc.returncode})\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr[-3000:]}")
    return json.loads(lines[-1])


# ── the property itself ──────────────────────────────────────────────────────

def test_hidden_defaults_to_false_and_round_trips():
    plain = SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py"})
    assert plain.hidden is False
    assert "hidden" not in plain.to_dict(), "an ordinary tool's metadata must not grow a hidden key"

    hid = SkillConfig.from_dict({"id": "a", "name": "A", "script": "a.py", "hidden": True})
    assert hid.hidden is True
    assert hid.to_dict()["hidden"] is True
    assert SkillConfig.from_dict(hid.to_dict()).hidden is True


def test_tile_skills_drops_only_the_hidden_ones_and_keeps_order():
    skills = [_skill("a"), _skill("b", hidden=True), _skill("c"), _skill("d", hidden=True)]
    assert [s.id for s in tile_skills(skills)] == ["a", "c"]
    assert [s.id for s in skills] == ["a", "b", "c", "d"], "the registry list itself must not be filtered"


def test_the_user_can_ask_for_a_hidden_tile_back():
    skills = [_skill("a"), _skill("b", hidden=True), _skill("c", hidden=True)]
    assert [s.id for s in tile_skills(skills, ["c"])] == ["a", "c"]
    # and that choice survives a settings load / save
    settings = LauncherSettings.from_dict({"show_hidden_tiles": ["c"]}, skills)
    assert settings.show_hidden_tiles == ["c"]
    assert settings.to_dict()["show_hidden_tiles"] == ["c"]
    fresh = LauncherSettings.from_dict({}, skills)
    assert fresh.show_hidden_tiles == []
    # set after loading (to_dict starts from the raw file, which would hide a missing write-back)
    fresh.show_hidden_tiles = ["b"]
    assert fresh.to_dict()["show_hidden_tiles"] == ["b"]


# ── the real registry ────────────────────────────────────────────────────────

def test_the_four_are_declared_hidden_and_still_registered():
    by_id = {s.id: s for s in discover_skills(REPO)}
    for sid, name in HIDDEN.items():
        assert sid in by_id, f"{name} ({sid}) is no longer returned by the registry: hidden is not removed"
        assert by_id[sid].hidden is True, f"{name} ({sid}) is not declared hidden"
        assert resolve_script_path(by_id[sid], REPO), f"{name} ({sid}) has no entry script on disk"
    assert {sid for sid, s in by_id.items() if s.hidden} == set(HIDDEN) | TAB_TOOLS, "something else became hidden"


def test_mission_craft_db_is_not_the_craft_database_and_keeps_its_tile():
    by_id = {s.id: s for s in discover_skills(REPO)}
    assert str(by_id["missions"].name) == "Mission/Craft DB"
    assert by_id["missions"].hidden is False


# ── the launcher grid, built for real ────────────────────────────────────────

def test_the_launcher_grid_has_no_tile_for_the_four_and_one_for_everything_else():
    out = _probe()
    tile_ids = [t["id"] for t in out["tiles"]]
    assert set(HIDDEN) <= set(out["registry"]), "the probe's registry lost a hidden tool"
    assert not set(HIDDEN) & set(tile_ids), f"hidden tools still have tiles: {set(HIDDEN) & set(tile_ids)}"
    assert STILL_TILES <= set(tile_ids), f"tiles that must stay are gone: {STILL_TILES - set(tile_ids)}"
    assert set(tile_ids) == set(out["registry"]) - set(HIDDEN) - TAB_TOOLS, "a tool with no tile that is not hidden"


def test_the_grid_closes_up_with_no_hole_where_a_hidden_tile_was():
    out = _probe()
    cells = [(t["row"], t["col"]) for t in out["tiles"]]
    assert cells == [(i // 2, i % 2) for i in range(len(cells))], f"gaps in the grid: {cells}"


def test_show_hidden_tiles_brings_one_tile_back_in_the_real_window():
    out = _probe("--show-hidden", "starmap")
    tile_ids = {t["id"] for t in out["tiles"]}
    assert "starmap" in tile_ids
    assert not {"market", "trade", "craft_db"} & tile_ids


# ── the launcher's processes and hotkeys ─────────────────────────────────────

class _RecordingPM:
    def __init__(self):
        self.registered: dict[str, dict] = {}
        self.shown: list[str] = []

    def register(self, skill_id, python_exe, script, cwd, args, base_dir, env=None):
        self.registered[skill_id] = {"script": script, "cwd": cwd, "args": list(args)}

    def get(self, skill_id):
        if skill_id not in self.registered:
            return None
        pm = self

        class _MP:
            running = True
            visible = True

            def set_on_exit(self, cb):
                pass

            def show(self):
                pm.shown.append(skill_id)

        return _MP()


class _NoWindow:
    def update_tile(self, *a):
        pass


def _bare_launcher(settings: dict):
    """skill_launcher.SCToolboxApp with only the state the two methods under test read.

    The constructor is not run: it kills orphan tool processes, starts a global hotkey listener
    and pre-spawns tools, none of which a test may do on a machine where the toolbox is in use."""
    import skill_launcher

    app = skill_launcher.SCToolboxApp.__new__(skill_launcher.SCToolboxApp)
    app._skills = discover_skills(REPO)
    app._settings = LauncherSettings.from_dict(settings, app._skills)
    app._launcher_hotkey = "<ctrl>+0"
    app._python = sys.executable
    app._availability = {}
    app._pm = _RecordingPM()
    app._window = _NoWindow()
    app._hotkey_conflicts = []
    app.toggled = []
    app._enqueue = lambda fn: fn()
    app._toggle_skill = app.toggled.append
    app._auto_hide_check = lambda: None
    return app


def test_hidden_tools_are_registered_with_the_process_manager_and_open_over_ipc():
    app = _bare_launcher({"disabled_skills": [], "keybinds_disabled": []})
    app._register_skills({})
    for sid, name in HIDDEN.items():
        assert sid in app._pm.registered, f"{name} ({sid}) was not registered: IPC and hotkey cannot open it"
        assert os.path.isfile(app._pm.registered[sid]["script"])
    # what WingmanAI's skill and the Assistant send to a launcher that is listening
    app._dispatch({"type": "launch_skill", "skill_id": "starmap"})
    assert app._pm.shown == ["starmap"]


def test_a_hidden_tools_hotkey_still_works():
    """Decision (2026-10-04): hiding a tile does not take the shortcut away."""
    app = _bare_launcher({"disabled_skills": [], "keybinds_disabled": []})
    by_id = {s.id: s for s in app._skills}
    bindings = app._build_hotkey_bindings()
    for sid, name in HIDDEN.items():
        hk = by_id[sid].hotkey
        assert hk, f"{name} has no default hotkey to keep"
        assert hk in bindings, f"{name}'s hotkey {hk} is no longer bound; conflicts: {app._hotkey_conflicts}"
        bindings[hk]()
        assert app.toggled[-1] == sid, f"{hk} toggles {app.toggled[-1]}, not {sid}"


def test_the_users_own_switches_still_turn_a_hidden_tools_hotkey_off():
    """Hidden adds nothing to the hotkey rules and takes nothing from them: the per-tool keybind
    switch and the Enabled/Disabled switch in Settings work on a hidden tool as on any other."""
    app = _bare_launcher({"disabled_skills": ["trade"], "keybinds_disabled": ["starmap"]})
    by_id = {s.id: s for s in app._skills}
    bindings = app._build_hotkey_bindings()
    assert by_id["trade"].hotkey not in bindings
    assert by_id["starmap"].hotkey not in bindings
    assert by_id["market"].hotkey in bindings


# ── the Assistant ────────────────────────────────────────────────────────────

@pytest.fixture()
def assistant_modules():
    """tools/Assistant's package, importable the way its own entry script makes it."""
    adir = os.path.join(REPO, "tools", "Assistant")
    added = adir not in sys.path
    if added:
        sys.path.insert(0, adir)
    try:
        from assistant import headless, ipc_bus
        yield headless, ipc_bus
    finally:
        if added:
            sys.path.remove(adir)


@pytest.mark.parametrize("spoken, sid", [
    ("star map", "starmap"), ("starmap", "starmap"), ("trade hub", "trade"),
    ("item finder", "market"), ("craft database", "craft_db"),
])
def test_the_assistant_still_resolves_and_can_still_spawn_a_hidden_tool(assistant_modules, spoken, sid):
    headless, ipc_bus = assistant_modules
    assert headless.resolve_skill(REPO, spoken)["id"] == sid
    # spawn_plan is what ipc_bus.spawn_skill() would run, without running it
    plan = ipc_bus.spawn_plan(REPO, sid, cmd_file="<cmd_file>")
    assert plan is not None, f"the Assistant can no longer build a launch plan for {sid}"
    assert os.path.isfile(plan["script"])
    assert plan["argv"][-1] == "<cmd_file>"


# ── the Everything Finder ────────────────────────────────────────────────────

def test_the_everything_finder_builds_its_tabs_without_the_registry():
    """Its three tabs are built by importing the tools' own modules (tool_loader.py).  If it ever
    starts asking the registry for them instead, hidden becomes something that can break its tabs,
    and this test is the tripwire that says so."""
    pkg = os.path.join(REPO, "skills", "Everything_Finder", "everything_finder")
    for name in sorted(os.listdir(pkg)):
        if not name.endswith(".py"):
            continue
        with open(os.path.join(pkg, name), encoding="utf-8") as fh:
            src = fh.read()
        for needle in ("skill_registry", "discover_skills", "tile_skills", "launch_skill"):
            assert needle not in src, f"everything_finder/{name} now uses {needle}"

    loader = os.path.join(pkg, "tool_loader.py")
    with open(loader, encoding="utf-8") as fh:
        src = fh.read()
    for folder, entry in (("Market_Finder", "market_finder"), ("Trade_Hub", "trade_hub_app.py"),
                          ("Starmap", "starmap")):
        assert f'"{folder}"' in src, f"tool_loader.py no longer points at skills/{folder}"
        assert os.path.exists(os.path.join(REPO, "skills", folder, entry)), f"skills/{folder}/{entry} is gone"
