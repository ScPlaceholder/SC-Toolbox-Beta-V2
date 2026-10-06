"""Check the staged Everything Finder and Star Map before they are packaged, and that no tool was left out.

    staging\\python\\python.exe build\\check_finder_starmap_stage.py <staging_root> [<toolbox source root>]

build_installer.bat stages skills\\Everything_Finder and skills\\Starmap by file pattern. This fails the build
when that went wrong in either direction, or when a staged copy would not behave for somebody who has just
installed it:

  1. the staged files of either folder are not exactly its run-time files: something extra (tests, README,
     a backup, a cache), or something missing. With the source root given, the run-time files are worked out
     from the source folders and every staged file is compared with its source byte for byte;
  2. a staged .py or .json holds a home-folder path or a developer's working note;
  3. a tool the Everything Finder's tabs are made of is not staged (skills\\Market_Finder, skills\\Trade_Hub,
     skills\\Starmap, the shared shopping list), or the Assistant's route setter is not (the Star Map's
     "navigate to ..." is that code);
  4. either window does not work from the staged copy ALONE: a fresh interpreter with an empty APPDATA, an
     empty home folder and an empty temp folder and no network, started on the entry script with the
     launcher's own arguments, must
       - be found by the launcher's tool discovery: Everything Finder with a tile, the Star Map without one;
       - build its window: the Everything Finder with all three tabs (Item Finder, Trade Hub, Star Map),
         none replaced by an error page; the Star Map with its panel and the galaxy's systems;
       - answer typed map commands from the staged galaxy data (help, go to a place);
       - with no network, say so in a sentence on the map's status line and not stop;
       - with In-Game switched on and no calibration, tell the player to calibrate, and send no key, click
         or clipboard text;
       - open no microphone;
       - open its tutorial (and the Everything Finder the Star Map's tutorial and the shopping list);
       - obey the launcher's hide / show / quit and exit with code 0;
       - write nothing into the staged folder (the logs folder it makes is removed again, and so are
         Trade Hub's log and Item Finder's price cache, which those tools keep beside their code);
  5. with the source root given: a folder in skills\\ or tools\\ has a skill.json (the launcher lists it) and
     is not staged, and is not named in NOT_SHIPPED below with the reason. This is the check that would have
     caught Pico Pals, the Toolbox Assistant, the Everything Finder and the Star Map being left out.

The probe never touches the desktop: it runs offscreen, the keyboard and mouse controllers and the global
key listeners are replaced before the tool is imported, and child processes and network connections are
refused. --online lets the network through and adds a real price lookup; it is for a person to run, and
the build never passes it.

Exit 0 = good, 1 = at least one failure (each printed), 2 = bad invocation.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# folder under skills/ -> what of it runs. Measured 2026-10-06: Everything Finder 8 files, 47 KB; Star Map
# 34 files, 586 KB (the galaxy's two JSON files are 220 KB of it).
TOOLS = {
    "Everything_Finder": {
        "label": "Everything Finder", "script": "everything_finder_app.py", "max_mb": 1,
        "dirs": ("", "everything_finder"),
        "source_only_dirs": ("tests",), "source_only_files": ("README.md",),
    },
    "Starmap": {
        "label": "Star Map", "script": "starmap_app.py", "max_mb": 3,
        "dirs": ("", "starmap", "starmap/data"),
        "source_only_dirs": ("tests",), "source_only_files": ("README.md",),
    },
}
ALLOWED_SUFFIXES = {".py", ".json"}
# What the windows are made of besides their own folders. Each must be staged.
NEEDS = {
    "skills/Market_Finder/market_finder/service.py": "the Item Finder tab",
    "skills/Trade_Hub/trade_hub_app.py": "the Trade Hub tab",
    "skills/Starmap/starmap/panel.py": "the Star Map tab",
    "shared/shopping/panel.py": "the shared shopping list",
    "shared/qt/tutorial_popup.py": "the tutorial window",
    "tools/Assistant/assistant/set_route/service.py": "'navigate to ...' on the Star Map (the Assistant's route setter)",
}
# Folders in skills\ or tools\ that have a skill.json and are deliberately NOT in the installer:
#   "tools/Example": "why it must not ship",
# Empty on 2026-10-06: everything the launcher lists ships.
NOT_SHIPPED: dict = {}
HOME_PATH = re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]")
# A developer's working folders and note links, and comments that say who asked for something and when
# instead of what the rule is (the shipped files were reworded; this keeps them that way).
# Two of the Everything Finder's tabs keep a file beside their own code, here as when they run on their
# own: Trade Hub its log (trade_hub_app.py, _LOG_PATH) and Item Finder its price cache (the build prunes
# .uex_cache.json from the staged copy for that reason). They are the only files a start may leave in the
# staged folder, and they are removed again here.
KNOWN_WRITES = ("skills/Trade_Hub/trade_hub.log", "skills/Market_Finder/.uex_cache.json")
DEV_NOTE = re.compile(rb"elah-audio|BrAi|_forJ|\[\[[a-z0-9]+(?:-[a-z0-9]+)+\]\]"
                      rb"|\bJ,? 20\d\d-\d\d-\d\d|\bJ's\b|\bJ asked\b|\xe2\x9b\x94|\xe2\x98\x85"
                      rb"|[Ss]ubagent|claude-|session:[0-9a-f]{6}")

PROBE = r'''
import json, os, runpy, sys, time
stage, cmd_file, which, online = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
OUT = {"input": [], "refused": [], "connects": 0, "mic_opens": 0}

def hook(event, args):
    if event == "subprocess.Popen":
        line = " ".join(str(a) for a in (args[1] if isinstance(args[1], (list, tuple)) else [args[1]]))
        OUT["refused"].append(line[:120])
        raise PermissionError("staging check: child process refused")
    elif event in ("socket.connect", "socket.getaddrinfo"):
        OUT["connects"] += 1
        if not online:
            raise OSError(10051, "staging check: no network")
    elif event in ("os.startfile", "os.system"):
        OUT["refused"].append(event)
        raise PermissionError("staging check: refused")
sys.addaudithook(hook)

# Nothing reaches the desktop: no key or click is sent and no global listener is installed.
from pynput import keyboard as _kb, mouse as _ms
class _Keys:
    def press(self, k): OUT["input"].append(["press", str(k)])
    def release(self, k): OUT["input"].append(["release", str(k)])
    def type(self, t): OUT["input"].append(["type", str(t)])
    def pressed(self, *k):
        class Held:
            def __enter__(s): OUT["input"].append(["hold", str(k)])
            def __exit__(s, *e): pass
        return Held()
class _Mouse:
    position = (0, 0)
    def click(self, b, n=1): OUT["input"].append(["click", list(self.position)])
    def scroll(self, dx, dy): OUT["input"].append(["scroll", dy])
    def press(self, b): OUT["input"].append(["mouse press", list(self.position)])
    def release(self, b): OUT["input"].append(["mouse release", list(self.position)])
_kb.Controller, _ms.Controller = _Keys, _Mouse
for _L in (_kb.Listener, _ms.Listener):
    _L.start = lambda self: None
    _L.stop = lambda self: None
    _L.join = lambda self, *a: None
    _L.is_alive = lambda self: True
    _L.wait = lambda self: None
try:
    import sounddevice as _sd
    class _NoMic:
        def __init__(self, *a, **k):
            OUT["mic_opens"] += 1
            raise _sd.PortAudioError("no input device")
    _sd.InputStream = _sd.RawInputStream = _NoMic
except Exception:
    pass

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel

FOLDER = {"finder": "Everything_Finder", "starmap": "Starmap"}[which]
SCRIPT = {"finder": "everything_finder_app.py", "starmap": "starmap_app.py"}[which]

def discovered():
    from core.skill_registry import discover_skills, resolve_script_path, tile_skills
    found = discover_skills(stage)
    tiles = {s.id for s in tile_skills(found)}
    by = {s.id: s for s in found}
    OUT["discovered"] = {sid: None if sid not in by else {
        "name": by[sid].name, "tile": sid in tiles, "script": bool(resolve_script_path(by[sid], stage))}
        for sid in ("everything_finder", "starmap")}

def map_checks(panel):
    """The Star Map panel, in its own window or as the Everything Finder's tab."""
    m = OUT["map"] = {"panel": type(panel).__name__}
    t0 = time.time()
    while time.time() - t0 < (90 if online else 40) and "items index" not in panel._voicebar.status():
        yield 0.3
    m["index_status"] = panel._voicebar.status()
    m["systems"] = sorted(s.name for s in panel._galaxy_data.systems)
    m["help"] = panel.run_command("help")
    m["goto"] = panel.run_command("go to Stanton")
    yield 0.5
    m["unknown"] = panel.run_command("frobnicate the widget")
    m["in_game_at_start"] = panel._btn_game.isChecked()
    # In-Game on, by the button, on a machine that has never calibrated
    if not panel._btn_game.isChecked():
        panel._btn_game.click()
        yield 0.3
    m["in_game_on"] = panel._btn_game.isChecked()
    rs = sys.modules.get("assistant.set_route.route_setter")
    if rs is None:
        try:
            import importlib
            from_link = [v for k, v in sys.modules.items() if k.endswith("set_route_link")][0]
            rs = from_link._module("route_setter")
        except Exception as exc:
            m["route_setter_error"] = "%s: %s" % (type(exc).__name__, exc)
    if rs is not None:
        rs._set_clipboard = lambda text: OUT["input"].append(["clipboard", text])
        rs.time = type("NoWait", (), {"sleep": staticmethod(lambda s: None)})
        m["calibration_path"] = os.path.abspath(rs.calibration_path())
        m["calibration_loaded"] = rs.load_calibration()
    m["navigate"] = panel.run_command("navigate to Area 18")
    yield 2.5
    m["status_after_navigate"] = panel._voicebar.status()
    m["input_after_navigate"] = list(OUT["input"])
    if online:
        try:
            rows = panel._rows_for("Area 18", "ST")
            m["online_rows_area18"] = len(rows or [])
        except Exception as exc:
            m["online_rows_area18"] = "%s: %s" % (type(exc).__name__, exc)

def scenario():
    app = QApplication.instance()
    discovered()
    if which == "finder":
        from everything_finder.window import EverythingFinderWindow as Win
    else:
        from shared.qt.base_window import SCWindow as Win
    win, t0 = None, time.time()
    while win is None and time.time() - t0 < 30:
        yield 0.2
        win = next((w for w in app.topLevelWidgets() if isinstance(w, Win) and w.isVisible()), None)
    if win is None:
        OUT["error"] = "the window never appeared"
        return
    OUT["title"], OUT["shown"] = win.windowTitle(), win.isVisible()
    panel = None
    if which == "finder":
        tabs = win.tabs
        OUT["tabs"] = tabs.keys()
        yield 1.0
        OUT["opens_on"] = tabs.current_key()
        pages = OUT["pages"] = {}
        errors = OUT["page_errors"] = {}
        for key in tabs.keys():
            win.select_tab(key)
            yield 1.5
            w = tabs.built_widget(key)
            pages[key] = None if w is None else type(w).__name__
            if w is None:
                cur = tabs.currentWidget()
                errors[key] = cur.text() if isinstance(cur, QLabel) else "not built"
            OUT.setdefault("inner", {})[key] = type(win.inner(key)).__name__
        panel = win.inner("star_map")
        yield (20.0 if online else 12.0)
        item = win.inner("item_finder")
        trade = win.inner("trade_hub")
        def texts(w):
            seen = []
            for lbl in (w.findChildren(QLabel) if w is not None else []):
                t = lbl.text().strip()
                if 3 < len(t) < 160 and "<" not in t and t not in seen:
                    seen.append(t)
            return seen
        OUT["item_texts"] = texts(tabs.built_widget("item_finder"))[:12]
        OUT["trade_texts"] = [t for t in texts(tabs.built_widget("trade_hub"))
                              if any(w in t.lower() for w in ("route", "error", "offline", "fetch", "fail", "data"))][:12]
        try:
            OUT["item_status"] = [item._status_lbl.text() if hasattr(item, "_status_lbl") else None,
                                  len(getattr(getattr(item, "data", None), "items", None) or [])]
        except Exception as exc:
            OUT["item_status"] = str(exc)
        if online:
            try:
                OUT["online_trade_routes"] = len(getattr(trade, "_all_routes", None) or [])
            except Exception as exc:
                OUT["online_trade_routes"] = str(exc)
        win.select_tab("star_map")
        yield 0.3
    else:
        from starmap.panel import StarmapPanel
        panel = win.findChild(StarmapPanel)
    if panel is None:
        OUT["map"] = None
    else:
        yield from map_checks(panel)

    # tutorials
    tut = OUT["tutorial"] = {}
    try:
        if which == "finder":
            t = win._show_tutorial()
            yield 0.5
            tut["own"] = [type(t).__name__, t.isVisible(), t.windowTitle()]
            tut["own_tabs"] = len(t.tab_text(0)) if hasattr(t, "tab_text") else None
            t.close()
            s = win._show_star_map_tutorial()
            yield 0.5
            tut["star_map"] = None if s is None else [type(s).__name__, s.isVisible()]
            if s is not None:
                s.close()
            win.toggle_shopping_list()
            yield 0.5
            pop = win.shopping_popout()
            OUT["shopping"] = None if pop is None else [type(pop).__name__, pop.isVisible()]
            win.toggle_shopping_list()
        else:
            from starmap.tutorial import TutorialPopup
            s = TutorialPopup(win)
            yield 0.5
            tut["own"] = [type(s).__name__, s.isVisible(), s.windowTitle()]
            s.close()
    except Exception:
        import traceback
        tut["error"] = traceback.format_exc()[-1200:]

    from shared.ipc import ipc_write
    ipc_write(cmd_file, {"type": "hide"})
    yield 1.5
    OUT["after_hide"] = win.isVisible()
    ipc_write(cmd_file, {"type": "show"})
    yield 1.5
    OUT["after_show"] = win.isVisible()
    OUT["finished"] = True

def pump(gen):
    try:
        wait = next(gen)
    except StopIteration:
        finish()
        return
    except Exception:
        import traceback
        OUT["error"] = traceback.format_exc()[-2500:]
        finish()
        return
    QTimer.singleShot(int(wait * 1000), lambda: pump(gen))

def finish():
    print("PROBE " + json.dumps(OUT, default=str), flush=True)
    try:
        from shared.ipc import ipc_write
        ipc_write(cmd_file, {"type": "quit"})
    except Exception:
        QApplication.instance().quit()
    QTimer.singleShot(30000, lambda: os._exit(8))          # "quit" did not end the process

def too_long():
    OUT["error"] = "the probe was still running after 5 minutes"
    print("PROBE " + json.dumps(OUT, default=str), flush=True)
    os._exit(9)

_exec = QApplication.exec
def exec_(*a, **k):
    QTimer.singleShot(300, lambda: pump(scenario()))
    QTimer.singleShot(300000, too_long)
    return _exec()
QApplication.exec = staticmethod(exec_)

script = os.path.join(stage, "skills", FOLDER, SCRIPT)
os.chdir(os.path.dirname(script))
sys.argv = [script, "100", "100", "1300", "800", "0.95", cmd_file]
runpy.run_path(script, run_name="__main__")
'''


def _snapshot(root: Path, skip: tuple) -> dict:
    snap = {}
    for base, dirs, files in os.walk(root):
        rel_dir = os.path.relpath(base, root).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirs[:] = [d for d in dirs if d != "__pycache__" and (rel_dir + "/" + d).lstrip("/") not in skip]
        for f in files:
            st = os.stat(os.path.join(base, f))
            snap[(rel_dir + "/" + f).lstrip("/")] = (st.st_size, st.st_mtime_ns)
    return snap


def _walk(top: Path, skip_dirs: tuple = ()) -> dict:
    """relative path -> Path, for every file under *top* (no __pycache__, none of *skip_dirs*)."""
    out = {}
    for base, dirs, files in os.walk(top):
        rel_dir = os.path.relpath(base, top).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirs[:] = [d for d in dirs if d != "__pycache__" and (rel_dir + "/" + d).lstrip("/") not in skip_dirs]
        for f in files:
            out[(rel_dir + "/" + f).lstrip("/")] = Path(base) / f
    return out


def check_files(stage: Path, source, folder: str, spec: dict, bad) -> tuple:
    """1 and 2: exactly the run-time files, none with a path or a note in it. Returns (count, bytes)."""
    tool = stage / "skills" / folder
    staged = _walk(tool)
    total = 0
    for rel, p in sorted(staged.items()):
        total += p.stat().st_size
        rel_dir = rel.rpartition("/")[0]
        name = p.name
        if rel_dir.split("/")[0] in spec["source_only_dirs"]:
            continue                                              # reported below, once per folder
        if rel_dir not in spec["dirs"]:
            bad("unexpected file %s (only %s are run-time folders)" % (rel, ", ".join(d or "." for d in spec["dirs"])))
        elif p.suffix.lower() not in ALLOWED_SUFFIXES or ".bak" in name.lower() or name.startswith("."):
            bad("unexpected file %s (not a run-time file type)" % rel)
        elif rel in spec["source_only_files"]:
            bad("%s is staged; it is not used at run time" % rel)
        else:
            data = p.read_bytes()
            if HOME_PATH.search(data):
                bad("%s holds a home-folder path" % rel)
            note = DEV_NOTE.search(data)
            if note:
                bad("%s holds a developer's working note (%s)" % (rel, note.group(0).decode("ascii", "replace")))
    for d in spec["source_only_dirs"]:
        if (tool / d).exists():
            bad("%s/ is staged; it is not used at run time" % d)
    if total > spec["max_mb"] * 1e6:
        bad("staged folder is %.1f MB, over the %d MB limit: working files were probably copied"
            % (total / 1e6, spec["max_mb"]))
    if source is not None:
        src = source / "skills" / folder
        if not (src / "skill.json").is_file():
            bad("the source folder %s has no skill.json" % src)
        else:
            want = {rel: p for rel, p in _walk(src, spec["source_only_dirs"]).items()
                    if rel not in spec["source_only_files"]}
            for rel in sorted(set(want) - set(staged)):
                bad("%s is in the source folder and is not staged: build_installer.bat copies *.py and "
                    "data\\*.json; a new kind of run-time file needs a line there (or, if it is not a "
                    "run-time file, a place in this script's source_only lists)" % rel)
            for rel in sorted(set(staged) - set(want)):
                bad("%s is staged and is not a run-time file of the source folder" % rel)
            for rel in sorted(set(want) & set(staged)):
                if want[rel].read_bytes() != staged[rel].read_bytes():
                    bad("%s is not the source's copy (staged from somewhere else, or changed after staging)" % rel)
    return len(staged), total


def check_coverage(stage: Path, source: Path, bad) -> int:
    """5: everything the launcher would list from the source tree is staged, or named in NOT_SHIPPED."""
    listed = []
    for parent in ("skills", "tools"):
        top = source / parent
        if not top.is_dir():
            continue
        for entry in sorted(top.iterdir()):
            if entry.is_dir() and (entry / "skill.json").is_file():
                listed.append(parent + "/" + entry.name)
    if not listed:
        bad("no folder with a skill.json under %s: is this the toolbox source root?" % source)
    for rel in listed:
        there = (stage / rel / "skill.json").is_file()
        if rel in NOT_SHIPPED:
            if there:
                bad("%s is staged, and NOT_SHIPPED in check_finder_starmap_stage.py says it must not ship (%s)"
                    % (rel, NOT_SHIPPED[rel]))
            else:
                print("  [note] %s is not in the installer: %s" % (rel, NOT_SHIPPED[rel]))
        elif not there:
            bad("%s has a skill.json, so the launcher lists it, and it is not staged: nobody who installs "
                "gets it. Stage it in build_installer.bat, or name it in NOT_SHIPPED in "
                "check_finder_starmap_stage.py with the reason" % rel)
    for rel in sorted(set(NOT_SHIPPED) - set(listed)):
        bad("NOT_SHIPPED names %s, which is not a folder with a skill.json in the source: remove the entry" % rel)
    return len(listed)


def run_probe(stage: Path, which: str, online: bool, bad):
    """4: start the staged entry script alone. Returns (probe dict or None, files made in the empty home)."""
    folder = {"finder": "Everything_Finder", "starmap": "Starmap"}[which]
    logs = stage / "logs"
    logs_before = logs.exists()
    before = _snapshot(stage, skip=("python", "logs"))
    known_before = {k for k in KNOWN_WRITES if (stage / k).exists()}
    made = []
    with tempfile.TemporaryDirectory(prefix="finder_stage_check_") as tmp:
        box = {k: os.path.join(tmp, k) for k in ("home", "appdata", "localappdata", "temp")}
        for d in box.values():
            os.makedirs(d)
        cmd_file = os.path.join(box["temp"], "sc_toolbox_%s_1_1.jsonl" % which)
        open(cmd_file, "w").close()
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("SC_", "PYTHON", "QT_", "HF_", "OLLAMA", "SUITMK2_"))}
        env.update(APPDATA=box["appdata"], LOCALAPPDATA=box["localappdata"], USERPROFILE=box["home"], HOME=box["home"],
                   HOMEDRIVE=box["home"][:2], HOMEPATH=box["home"][2:], TEMP=box["temp"], TMP=box["temp"],
                   QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        try:
            run = subprocess.run([sys.executable, "-c", PROBE, str(stage), cmd_file, which, "1" if online else "0"],
                                 env=env, capture_output=True, timeout=400, cwd=str(stage / "skills" / folder))
            text = (run.stdout + run.stderr).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            run, text = None, "timed out after 400 s"
        for k in ("home", "appdata", "localappdata"):
            made += sorted(k + "/" + rel for rel in _walk(Path(box[k])))
        home = os.path.normcase(os.path.abspath(box["home"]))
    after = _snapshot(stage, skip=("python", "logs"))
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k) and k not in KNOWN_WRITES)
    for k in KNOWN_WRITES:
        if k not in known_before and (stage / k).exists():
            os.remove(stage / k)
    if changed:
        bad("starting it wrote into the staged folder (an install folder is replaced by every update): %s"
            % ", ".join(changed[:8]))
    if logs.exists() and not logs_before:
        shutil.rmtree(logs, ignore_errors=True)                   # the probe's own crash log and window state
        if logs.exists():
            bad("the logs folder this check made in staging could not be removed: %s" % logs)
    line = next((ln for ln in text.splitlines() if ln.startswith("PROBE ")), None)
    if run is None or line is None:
        bad("it does not start from the staged copy alone:\n" + text.strip()[-2000:])
        return None, made, home
    got = json.loads(line[6:])
    if got.get("error"):
        bad("the probe stopped: " + str(got["error"]))
    if run.returncode != 0:
        bad("it did not exit cleanly on the launcher's quit (exit code %s)" % run.returncode)
    return got, made, home


def judge_map(m, home: str, online: bool, bad) -> None:
    if not m or m.get("panel") != "StarmapPanel":
        bad("the Star Map panel was not built: %s" % (m,))
        return
    if len(m.get("systems") or []) < 50 or "Stanton" not in m["systems"]:
        bad("the galaxy has %d systems and Stanton is%s one of them; starmap/data/systems.json holds about ninety"
            % (len(m.get("systems") or []), "" if "Stanton" in (m.get("systems") or []) else " not"))
    ok, text = (m.get("help") or [False, ""])[:2]
    if not ok or "navigate to" not in str(text):
        bad("the map did not answer 'help' with its command list: %r" % (m.get("help"),))
    if not (m.get("goto") or [False])[0] or "command error" in str(m.get("goto")):
        bad("'go to Stanton' was not carried out from the staged galaxy data: %r" % (m.get("goto"),))
    if (m.get("unknown") or [True, ""])[0] or "did not understand" not in str(m.get("unknown")):
        bad("a nonsense command should be answered with 'did not understand': %r" % (m.get("unknown"),))
    status = str(m.get("index_status"))
    if not online and "offline" not in status and "cache" not in status:
        bad("with no network the map's status line should say its prices are offline; it said %r" % status)
    if online and "items index ready" not in status:
        bad("with the network the items index should load; the status line said %r" % status)
    if m.get("in_game_at_start"):
        bad("In-Game is ON in an empty home folder: a new install must start with it off")
    if m.get("route_setter_error"):
        bad("the Assistant's route setter could not be loaded from the map: %s" % m["route_setter_error"])
    where = os.path.normcase(str(m.get("calibration_path") or ""))
    if not where.startswith(home + os.sep):
        bad("the click calibration would be read from %s, which is not the user's own folder"
            % m.get("calibration_path"))
    if m.get("calibration_loaded") is not None:
        bad("click positions were loaded with no calibration file: %s" % m.get("calibration_loaded"))
    if not m.get("in_game_on"):
        bad("the In-Game button did not switch on")
    said = str(m.get("status_after_navigate"))
    if "not calibrated" not in said or "Calibrate Route" not in said:
        bad("with In-Game on and no calibration, 'navigate to Area 18' must leave 'not calibrated (press "
            "Calibrate Route)' on the status line; it says %r" % said)
    if m.get("input_after_navigate"):
        bad("keys, clicks or clipboard text were sent with no calibration: %s" % m["input_after_navigate"][:6])


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if len(args) not in (1, 2) or flags - {"--online", "--dump"}:
        print("Usage: check_finder_starmap_stage.py <staging_root> [<toolbox source root>] [--online] [--dump]",
              file=sys.stderr)
        return 2
    stage = Path(args[0]).resolve()
    source = Path(args[1]).resolve() if len(args) == 2 else None
    online, dump = "--online" in flags, "--dump" in flags
    fails: list = []
    label = [""]

    def bad(msg: str) -> None:
        fails.append(msg)
        print("  [FAIL] %s: %s" % (label[0], msg))

    sizes = {}
    for folder, spec in TOOLS.items():
        label[0] = spec["label"]
        if not (stage / "skills" / folder / "skill.json").is_file():
            bad("skills/%s/skill.json is not staged" % folder)
            continue
        sizes[folder] = check_files(stage, source, folder, spec, bad)
    label[0] = "Everything Finder"
    for rel, what in NEEDS.items():
        if not (stage / rel).is_file():
            bad("%s is not staged: %s cannot be built" % (rel, what))

    if source is not None:
        label[0] = "Installer"
        n = check_coverage(stage, source, bad)
        if not any(f for f in fails if "has a skill.json" in f):
            print("  [OK] all %d folders with a skill.json in skills\\ and tools\\ are staged" % n)

    if len(sizes) == len(TOOLS):
        for which, folder in (("starmap", "Starmap"), ("finder", "Everything_Finder")):
            label[0] = TOOLS[folder]["label"]
            before = len(fails)
            got, made, home = run_probe(stage, which, online, bad)
            if dump:
                print("  [dump] %s %s\n  [dump] made in the empty home: %s" % (which, json.dumps(got, indent=1), made))
            if got is None:
                continue
            name = json.loads((stage / "skills" / folder / "skill.json").read_text(encoding="utf-8")).get("name")
            d = got.get("discovered") or {}
            ef, sm = d.get("everything_finder"), d.get("starmap")
            if not ef or not ef["script"] or not ef["tile"]:
                bad("the launcher's tool discovery does not find the Everything Finder as a tile with a script: %s" % ef)
            if not sm or not sm["script"] or sm["tile"]:
                bad("the launcher's tool discovery should find the Star Map with a script and no tile of its "
                    "own (it is a tab of the Everything Finder): %s" % sm)
            if which == "finder":
                if got.get("title") != name:
                    bad("the window is titled %r; skill.json names it %r" % (got.get("title"), name))
                want = {"item_finder": "QWidget", "trade_hub": "QWidget", "star_map": "StarmapPanel"}
                pages = got.get("pages") or {}
                if got.get("tabs") != list(want) or got.get("page_errors") or any(not pages.get(k) for k in want) \
                        or pages.get("star_map") != "StarmapPanel":
                    bad("the three tabs were not all built: tabs %s, pages %s, errors %s"
                        % (got.get("tabs"), pages, got.get("page_errors")))
                inner = got.get("inner") or {}
                if inner.get("item_finder") != "MarketFinderApp" or inner.get("trade_hub") != "TradeHubWindow":
                    bad("the Item Finder and Trade Hub tabs are not those tools' own windows: %s" % inner)
                if got.get("opens_on") != "item_finder":
                    bad("a new install should open on the Item Finder tab; it opened on %r" % got.get("opens_on"))
                if (got.get("shopping") or [None, False])[1] is not True:
                    bad("the shared shopping list did not open: %s" % got.get("shopping"))
                if (got.get("tutorial", {}).get("star_map") or [None, False])[1] is not True:
                    bad("the Star Map's tutorial did not open from the Everything Finder's: %s" % got.get("tutorial"))
            elif "Starmap" not in str(got.get("title")):
                bad("the window is titled %r" % got.get("title"))
            tut = got.get("tutorial") or {}
            if tut.get("error") or (tut.get("own") or [None, False])[1] is not True:
                bad("its tutorial did not open: %s" % tut)
            judge_map(got.get("map"), home, online, bad)
            if got.get("mic_opens"):
                bad("it opened a microphone %d time(s); neither window has one" % got["mic_opens"])
            if got.get("refused"):
                bad("it tried to start another program: %s" % got["refused"][:4])
            if got.get("input"):
                bad("keys, clicks or clipboard text were sent: %s" % got["input"][:6])
            if not online and not got.get("connects"):
                bad("it never tried the network, so 'no network' was not exercised")
            if got.get("after_hide") is not False or got.get("after_show") is not True:
                bad("the launcher's hide / show did not work: after hide visible=%s, after show visible=%s"
                    % (got.get("after_hide"), got.get("after_show")))
            stray = [p for p in made if not p.startswith("home/.sctoolbox/")]
            if stray:
                bad("it wrote outside the user's .sctoolbox folder: %s" % stray[:6])
            if len(fails) == before:
                m = got.get("map") or {}
                print("  [OK] %s starts from staging alone%s: %s; map says %r; no calibration -> %r, no input sent"
                      % (TOOLS[folder]["label"], " (network on)" if online else " (no network)",
                         "tabs " + ", ".join(got["tabs"]) if which == "finder" else "%d systems" % len(m.get("systems") or []),
                         str(m.get("index_status"))[:70], str(m.get("status_after_navigate"))[:90]))

    for folder, (count, total) in sizes.items():
        print("  [%s] %s staged: %d files, %.2f MB" % ("OK" if not fails else "!!", TOOLS[folder]["label"], count, total / 1e6))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
