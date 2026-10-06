"""Check the staged Toolbox Assistant (staging/tools/Assistant) before it is packaged.

    staging\\python\\python.exe build\\check_assistant_stage.py <staging_root> [<source tools\\Assistant>]

build_installer.bat stages the Assistant file by file. This fails the build when that went wrong in either
direction, or when the staged copy would not behave for somebody who has just installed it:

  1. the staged files are not exactly the run-time files: something extra (tests, the eval set, notes, backups,
     the old Star Map ears, a calibration file), or something missing. With the source folder given, the
     run-time files are worked out from it, so a new module that was not added to the list in
     build_installer.bat is named here, and every staged file is compared with its source byte for byte;
  2. a click calibration is staged. mouse_calibration.json holds the screen positions the in-game route setter
     clicks, measured on one person's screen: it must never travel with the code. tools/set_route_ai (the
     WingmanAI skill's folder, which the route setter reads a calibration from when it is there) must not be
     staged either;
  3. a staged .py or .json holds a home-folder path or a developer's working note;
  4. a tool the Assistant looks answers up in is not staged (each one in assistant/worker_pool.py TOOLS, apart
     from the ones named in NOT_SHIPPED below);
  5. the Assistant does not work from the staged copy ALONE: a fresh interpreter with an empty APPDATA, an
     empty home folder and an empty temp folder, no network and no model service, started on the entry script
     with the launcher's own arguments, must
       - be found by the launcher's tool discovery, with SuitMk2 as a tab of it;
       - build the window with both tabs (Assistant, Suit Mk2), neither replaced by an error page;
       - answer a typed question from the toolbox's own data with no model reachable;
       - start the worker of every staged tool it looks answers up in;
       - with In-Game switched on and no calibration, say so, and send no key, click or clipboard text;
       - say why when there is no microphone;
       - obey the launcher's hide / show / quit and exit with code 0;
       - write nothing into the staged folder (the logs folder it makes is removed again).

The probe never touches the desktop: it runs offscreen, and the keyboard and mouse controllers, the global
key listeners, the microphone and the speakers are replaced before the Assistant is imported. Child processes
other than the Assistant's own workers, and every network connection, are refused.

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

MAX_MB = 8              # measured 2026-10-06: 4.1 MB in 45 files (the two penguin images are 3.3 MB of it)
ALLOWED_DIRS = ("", "assets", "assistant", "assistant/set_route", "assistant/workers", "assistant/data/set_route")
ALLOWED_SUFFIXES = {".py", ".json", ".png"}
# In the source folder but not run-time files. Nothing the entry script reaches imports any of them.
SOURCE_ONLY_DIRS = ("tests", "eval", "assistant/starmap_ears")
SOURCE_ONLY_FILES = ("README.md", "assistant_app.py", "assistant/selftest.py")
CALIBRATION = "mouse_calibration.json"
# Tools the Assistant can look answers up in that the installer does not ship. Asking for one of these gets a
# sentence saying the tool is missing; every other tool in worker_pool.TOOLS must be staged.
NOT_SHIPPED = {"starmap"}
HOME_PATH = re.compile(rb"[A-Za-z]:[\\/]+Users[\\/]")
# A developer's working folders and note links, and comments that say who asked for something and when
# instead of what the rule is (the shipped files were reworded; this keeps them that way).
DEV_NOTE = re.compile(rb"elah-audio|BrAi|_forJ|\[\[[a-z0-9]+(?:-[a-z0-9]+)+\]\]"
                      rb"|\bJ,? 20\d\d-\d\d-\d\d|\bJ's\b|\xe2\x9b\x94|\xe2\x98\x85")

PROBE = r'''
import json, os, runpy, subprocess, sys, threading, time
stage, cmd_file = sys.argv[1], sys.argv[2]
OUT = {"input": [], "refused": [], "connects": 0, "mic_opens": 0}

def hook(event, args):
    if event == "subprocess.Popen":
        line = " ".join(str(a) for a in (args[1] if isinstance(args[1], (list, tuple)) else [args[1]]))
        if "/workers/_worker_main.py" not in line.replace(os.sep, "/"):
            OUT["refused"].append(line[:120])
            raise PermissionError("staging check: child process refused")
    elif event in ("socket.connect", "socket.getaddrinfo"):
        OUT["connects"] += 1
        raise OSError(10051, "staging check: no network")
    elif event in ("os.startfile", "os.system"):
        OUT["refused"].append(event)
        raise PermissionError("staging check: refused")
sys.addaudithook(hook)

# Nothing reaches the desktop: no key or click is sent, no global listener is installed, no microphone or
# speaker is opened.
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
import sounddevice as _sd
class _NoMic:
    def __init__(self, *a, **k):
        OUT["mic_opens"] += 1
        raise _sd.PortAudioError("no input device")
class _NoSpeaker:
    def __init__(self, *a, **k): pass
    def start(self): pass
    def stop(self): pass
    def close(self): pass
    def write(self, *a): pass
    def __enter__(self): return self
    def __exit__(self, *e): pass
_sd.InputStream = _sd.RawInputStream = _NoMic
_sd.OutputStream = _sd.RawOutputStream = _NoSpeaker
_sd.play = lambda *a, **k: None

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

def scenario():
    app = QApplication.instance()
    from assistant.hub import HubWindow
    win, t0 = None, time.time()
    while win is None and time.time() - t0 < 30:
        yield 0.2
        win = next((w for w in app.topLevelWidgets() if isinstance(w, HubWindow)), None)
    if win is None:
        OUT["error"] = "the window never appeared"
        return
    OUT["title"], OUT["tabs"], OUT["shown"] = win.windowTitle(), win.tab_keys(), win.isVisible()
    t0 = time.time()
    while time.time() - t0 < 60 and any(win.page(k) is None for k in win.tab_keys()):
        yield 0.3                                   # the tab behind is built a few seconds after start-up
    OUT["pages"] = {k: type(win.page(k)).__name__ for k in win.tab_keys()}
    OUT["page_errors"] = {k: win.page(k).text() for k in win.tab_keys() if isinstance(win.page(k), QLabel)}
    from core.skill_registry import discover_skills, resolve_script_path
    found = {s.id: s for s in discover_skills(stage)}
    a = found.get("assistant")
    OUT["discovered"] = None if a is None else {
        "name": a.name, "hidden": bool(getattr(a, "hidden", False)),
        "script": bool(resolve_script_path(a, stage)),
        "suit_tab_of": getattr(found.get("suitmk2"), "tab_of", None)}
    p = win.page("assistant")
    if p is None or isinstance(p, QLabel):
        return
    win.select("assistant")
    yield 0.3
    said = []
    class Mouth:
        def speak(self, t): said.append(t)
        def stop(self): pass
    p._mouth = Mouth()
    from assistant.set_route import route_setter
    route_setter._set_clipboard = lambda text: OUT["input"].append(["clipboard", text])
    route_setter.time = type("NoWait", (), {"sleep": staticmethod(lambda s: None)})
    OUT["buttons"] = [b.text() for b in p.findChildren(QPushButton)]
    OUT["penguin"] = p._penguin is not None
    OUT["in_game_at_start"] = p._btn_game.isChecked()

    def ask(text):
        p._txt_input.setText(text)
        p._txt_input.returnPressed.emit()
        t = time.time()
        yield 0.3
        while time.time() - t < 90:
            if p._worker is not None and not p._worker.isRunning() and p._lbl_status.text() != "thinking…":
                break
            yield 0.2
        yield 0.5
        return p._lbl_reply.text()

    OUT["cargo_reply"] = yield from ask("what does a Cutlass Black hold")
    OUT["llm_error"] = p._agent.last_llm_error
    OUT["mode_in_use"] = p._agent.effective_mode

    # every staged tool the Assistant looks answers up in: its worker starts and says which questions it takes
    from assistant import worker_pool
    workers = OUT["workers"] = {}
    def start_all():
        python = worker_pool.find_toolbox_python()
        for key, (folder, _handler) in worker_pool.TOOLS.items():
            if not os.path.isdir(os.path.join(stage, folder)):
                workers[key] = {"staged": False, "folder": folder.replace(os.sep, "/")}
                continue
            try:
                w = worker_pool._Worker(key, python, stage)
            except Exception as exc:
                workers[key] = {"staged": True, "ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
                continue
            try:
                hello = w.start_wait(90)
                workers[key] = {"staged": True, "ok": bool(hello.get("ok")), "fns": hello.get("fns"),
                                "error": (str(hello.get("error") or "") + " " + str(hello.get("traceback") or "")[-600:]).strip()}
            except Exception as exc:
                workers[key] = {"staged": True, "ok": False,
                                "error": "%s; %s" % (type(exc).__name__, w.err_tail()[-600:])}
            finally:
                w.stop()
    th = threading.Thread(target=start_all, daemon=True)
    th.start()
    t0 = time.time()
    while th.is_alive() and time.time() - t0 < 400:
        yield 0.3
    OUT["workers_finished"] = not th.is_alive()

    # In-Game on, by the button, on a machine that has never calibrated
    if not p._btn_game.isChecked():
        p._btn_game.click()
        yield 0.3
    OUT["in_game_on"] = p._btn_game.isChecked()
    OUT["calibration_path"] = os.path.abspath(route_setter.calibration_path())
    OUT["calibration_loaded"] = route_setter.load_calibration()
    OUT["route_reply"] = yield from ask("navigate to Area 18")
    OUT["yes_reply"] = yield from ask("yes")
    from assistant import builtin_tools
    svc = builtin_tools._routes(p._agent.ctx)
    OUT["plot"] = list(svc.plot("area18"))
    macro = []
    OUT["macro_started"] = svc.setter().set_route("area18", status_cb=macro.append, done_cb=macro.append)
    yield 2.0
    OUT["macro_said"] = macro
    OUT["input_after_route"] = list(OUT["input"])

    heard = []
    p._ears.statusChanged.connect(heard.append)
    p._ears._begin()
    yield 0.5
    OUT["no_mic"] = heard

    from shared.ipc import ipc_write
    ipc_write(cmd_file, {"type": "hide"})
    yield 1.0
    OUT["after_hide"] = win.isVisible()
    ipc_write(cmd_file, {"type": "show", "tab": "suitmk2"})
    yield 1.0
    OUT["after_show"] = [win.isVisible(), win.current_tab()]
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
    OUT["error"] = "the probe was still running after 9 minutes"
    print("PROBE " + json.dumps(OUT, default=str), flush=True)
    os._exit(9)

_exec = QApplication.exec
def exec_(*a, **k):
    QTimer.singleShot(300, lambda: pump(scenario()))
    QTimer.singleShot(540000, too_long)
    return _exec()
QApplication.exec = staticmethod(exec_)

script = os.path.join(stage, "tools", "Assistant", "toolbox_assistant_app.py")
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


def _runtime_files(source: Path) -> set:
    """The files of *source* (tools/Assistant in the repository) that run: everything except SOURCE_ONLY_*."""
    out = set()
    for base, dirs, files in os.walk(source):
        rel_dir = os.path.relpath(base, source).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        dirs[:] = [d for d in dirs if d != "__pycache__" and (rel_dir + "/" + d).lstrip("/") not in SOURCE_ONLY_DIRS]
        for f in files:
            rel = (rel_dir + "/" + f).lstrip("/")
            if rel in SOURCE_ONLY_FILES or f == CALIBRATION:
                continue
            out.add(rel)
    return out


def main() -> int:
    if len(sys.argv) not in (2, 3):
        print("Usage: check_assistant_stage.py <staging_root> [<source tools\\Assistant>]", file=sys.stderr)
        return 2
    stage = Path(sys.argv[1]).resolve()
    source = Path(sys.argv[2]).resolve() if len(sys.argv) == 3 else None
    tool = stage / "tools" / "Assistant"
    fails: list = []

    def bad(msg: str) -> None:
        fails.append(msg)
        print("  [FAIL] Assistant: " + msg)

    if not (tool / "skill.json").is_file():
        print("  [FAIL] Assistant: tools/Assistant/skill.json is not staged (no tile, and SuitMk2, which is a tab "
              "of this window, has no window to open in)")
        return 1

    # 1. exactly the run-time files
    staged, total = set(), 0
    for base, dirs, files in os.walk(tool):
        dirs[:] = [d for d in dirs if d != "__pycache__"]         # removed later by the build's own cleanup
        rel_dir = os.path.relpath(base, tool).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        for f in files:
            p = Path(base) / f
            rel = (rel_dir + "/" + f).lstrip("/")
            staged.add(rel)
            total += p.stat().st_size
            if f == CALIBRATION:
                continue                                          # reported below, once, with the reason
            if rel_dir not in ALLOWED_DIRS:
                bad("unexpected file %s (only %s are run-time folders)" % (rel, ", ".join(d or "." for d in ALLOWED_DIRS)))
            elif p.suffix.lower() not in ALLOWED_SUFFIXES or ".bak" in f.lower() or f.startswith("."):
                bad("unexpected file %s (not a run-time file type)" % rel)
            elif rel in SOURCE_ONLY_FILES:
                bad("%s is staged; it is not used at run time" % rel)
            elif p.suffix.lower() in (".py", ".json"):
                data = p.read_bytes()
                if HOME_PATH.search(data):
                    bad("%s holds a home-folder path" % rel)
                note = DEV_NOTE.search(data)
                if note:
                    bad("%s holds a developer's working note (%s)" % (rel, note.group(0).decode("ascii", "replace")))
    for d in SOURCE_ONLY_DIRS:
        if (tool / d).exists():
            bad("%s/ is staged; it is not used at run time" % d)
    if total > MAX_MB * 1e6:
        bad("staged Assistant is %.1f MB, over the %d MB limit: working files were probably copied" % (total / 1e6, MAX_MB))
    if source is not None:
        if not (source / "skill.json").is_file():
            bad("the source folder %s has no skill.json" % source)
        else:
            want = _runtime_files(source)
            for rel in sorted(want - staged):
                bad("%s is a run-time file and is not staged: add it to the Toolbox Assistant list in "
                    "build_installer.bat" % rel)
            for rel in sorted(staged - want):
                if os.path.basename(rel) != CALIBRATION:
                    bad("%s is staged and is not a run-time file of the source folder" % rel)
            for rel in sorted(want & staged):
                if (source / rel).read_bytes() != (tool / rel).read_bytes():
                    bad("%s is not the source's copy (staged from somewhere else, or changed after staging)" % rel)

    # 2. no click calibration travels with the code
    for found in sorted(str(p.relative_to(stage)) for p in tool.rglob(CALIBRATION)):
        bad("%s is staged: it holds one person's screen positions for the in-game route setter, and an "
            "installed Assistant would click there on everybody's screen" % found)
    if (stage / "tools" / "set_route_ai").exists():
        bad("tools/set_route_ai is staged: it is the WingmanAI skill, not part of the toolbox, and the route "
            "setter reads a calibration from its data folder when that folder exists")

    # the destination list the route setter resolves names against
    try:
        places = json.loads((tool / "assistant" / "data" / "set_route" / "destinations.json").read_text(encoding="utf-8"))
        if len(places) < 100:
            bad("assistant/data/set_route/destinations.json holds %d destinations; the real list has hundreds" % len(places))
    except Exception as exc:                                      # noqa: BLE001
        bad("assistant/data/set_route/destinations.json unreadable: %s (no destination can be resolved)" % exc)

    # 5. it works from the staged copy alone
    logs = stage / "logs"
    logs_before = logs.exists()
    before = _snapshot(stage, skip=("python", "logs"))
    with tempfile.TemporaryDirectory(prefix="assistant_stage_check_") as tmp:
        box = {k: os.path.join(tmp, k) for k in ("home", "appdata", "localappdata", "temp")}
        for d in box.values():
            os.makedirs(d)
        cmd_file = os.path.join(box["temp"], "sc_toolbox_assistant_1_1.jsonl")
        open(cmd_file, "w").close()
        env = {k: v for k, v in os.environ.items()
               if not k.upper().startswith(("SC_", "PYTHON", "QT_", "HF_", "OLLAMA", "SUITMK2_"))}
        env.update(APPDATA=box["appdata"], LOCALAPPDATA=box["localappdata"], USERPROFILE=box["home"], HOME=box["home"],
                   HOMEDRIVE=box["home"][:2], HOMEPATH=box["home"][2:], TEMP=box["temp"], TMP=box["temp"],
                   QT_QPA_PLATFORM="offscreen", PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        home = os.path.normcase(os.path.abspath(box["home"]))
        try:
            run = subprocess.run([sys.executable, "-c", PROBE, str(stage), cmd_file], env=env, capture_output=True,
                                 timeout=660, cwd=str(tool))
            text = (run.stdout + run.stderr).decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            run, text = None, "timed out after 660 s"
    after = _snapshot(stage, skip=("python", "logs"))
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    if changed:
        bad("starting it wrote into the staged folder (an install folder is replaced by every update): %s"
            % ", ".join(changed[:8]))
    if logs.exists() and not logs_before:
        shutil.rmtree(logs, ignore_errors=True)                   # the probe's own crash log and window state
        if logs.exists():
            bad("the logs folder this check made in staging could not be removed: %s" % logs)

    workers: dict = {}
    line = next((ln for ln in text.splitlines() if ln.startswith("PROBE ")), None)
    if run is None or line is None:
        bad("the Assistant does not start from the staged copy alone:\n" + text.strip()[-2000:])
    else:
        got = json.loads(line[6:])
        name = json.loads((tool / "skill.json").read_text(encoding="utf-8")).get("name")
        if got.get("error"):
            bad("the probe stopped: " + str(got["error"]))
        if run.returncode != 0:
            bad("it did not exit cleanly on the launcher's quit (exit code %s)" % run.returncode)
        d = got.get("discovered")
        if not d or not d["script"] or d["hidden"]:
            bad("the launcher's tool discovery does not find it as a tile with a script: %s" % d)
        elif d["suit_tab_of"] != "assistant":
            bad("SuitMk2's skill.json says tab_of=%r, not 'assistant': its hotkey would not open this window" % d["suit_tab_of"])
        if got.get("title") != name:
            bad("the window is titled %r; skill.json names it %r" % (got.get("title"), name))
        if got.get("tabs") != ["assistant", "suitmk2"] or got.get("page_errors") or \
                got.get("pages") != {"assistant": "AssistantPanel", "suitmk2": "SuitPanel"}:
            bad("the two tabs were not both built: tabs %s, pages %s, errors %s"
                % (got.get("tabs"), got.get("pages"), got.get("page_errors")))
        if got.get("pages", {}).get("assistant") == "AssistantPanel":
            for label in ("In-Game", "Calibrate Route", "Send"):
                if label not in got.get("buttons", []):
                    bad("the Assistant tab has no %r button (it has %s)" % (label, got.get("buttons")))
            if not got.get("penguin"):
                bad("the listener penguin did not load: assets/listener_penguin_sheet.png or listener_penguin.png "
                    "is missing or unreadable")
            if got.get("in_game_at_start"):
                bad("In-Game is ON in an empty home folder: a new install must start with it off")
            if "SCU" not in str(got.get("cargo_reply")):
                bad("a typed question was not answered from the toolbox's own data: asked what a Cutlass Black "
                    "holds, got %r" % got.get("cargo_reply"))
            if not got.get("llm_error") or got.get("mode_in_use") != "router":
                bad("with no model service reachable it should answer by itself (mode 'router') and keep the "
                    "reason: mode %r, reason %r" % (got.get("mode_in_use"), got.get("llm_error")))
            workers = got.get("workers") or {}
            if not got.get("workers_finished") or not workers:
                bad("the tool workers were not all tried: %s" % sorted(workers))
            for key, w in sorted(workers.items()):
                if not w["staged"]:
                    if key not in NOT_SHIPPED:
                        bad("%s is not staged, so the Assistant cannot look anything up in it" % w["folder"])
                    else:
                        print("  [note] Assistant: %s is not in the installer; asking for it gets a sentence "
                              "saying so" % w["folder"])
                elif not w["ok"]:
                    bad("the %s worker does not start from staging: %s" % (key, w["error"]))
                elif not w["fns"]:
                    bad("the %s worker started and takes no questions" % key)
            # the in-game route setter on a machine that has never calibrated
            where = os.path.normcase(str(got.get("calibration_path") or ""))
            if not where.startswith(home + os.sep):
                bad("the click calibration would be read from %s, which is not the user's own folder"
                    % got.get("calibration_path"))
            if got.get("calibration_loaded") is not None:
                bad("click positions were loaded with no calibration file: %s" % got.get("calibration_loaded"))
            if not got.get("in_game_on"):
                bad("the In-Game button did not switch on")
            if "Calibrate Route" not in str(got.get("route_reply")) or "yes or no" in str(got.get("route_reply")).lower():
                bad("with In-Game on and no calibration it must tell the player to calibrate and not offer to "
                    "set the route; it said %r" % got.get("route_reply"))
            if (got.get("plot") or [True])[0] is not False:
                bad("the route service started the in-game macro with no calibration: %s" % got.get("plot"))
            if not any(str(m).startswith("error:") for m in got.get("macro_said") or []):
                bad("the in-game macro itself did not refuse with no calibration: %s" % got.get("macro_said"))
            if got.get("input"):
                bad("keys, clicks or clipboard text were sent with no calibration: %s" % got["input"][:6])
            if not any("mic error" in str(m) for m in got.get("no_mic") or []):
                bad("with no microphone the status line should say so; it said %s" % got.get("no_mic"))
            if got.get("after_hide") is not False or got.get("after_show") != [True, "suitmk2"]:
                bad("the launcher's hide / show did not work: after hide visible=%s, after show %s"
                    % (got.get("after_hide"), got.get("after_show")))
        if not fails:
            print("  [OK] Assistant starts from staging alone: both tabs, %d tool workers, typed answer %r, "
                  "no calibration -> no input sent" % (sum(1 for w in workers.values() if w.get("ok")),
                                                       str(got.get("cargo_reply"))[:60]))

    print("  [%s] Toolbox Assistant staged: %d files, %.1f MB" % ("OK" if not fails else "!!", len(staged), total / 1e6))
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
