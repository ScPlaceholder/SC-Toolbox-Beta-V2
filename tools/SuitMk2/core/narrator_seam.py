"""narrator_seam.py - SuitMk2 must stay a NARRATOR: it may never send input or read another tool's tracking.

If the companion AI were able to target track, it would no longer be a narration tool but an aimbot
with no control output. And on the toolbox as a whole: the OCR tools already TRACK things, so the bot is two pieces
and a seam. What keeps it a narrator is that SuitMk2 never holds the dangerous half of either piece:

  1. NO OUTPUT TO THE GAME. Nothing that presses keys, moves or clicks the mouse, or drives a virtual controller:
     pynput *Controller*, pyautogui, pydirectinput, vgamepad, interception, the `keyboard`/`mouse` packages, AutoIt/AHK
     bridges, and the Win32 calls underneath all of them (SendInput, keybd_event, mouse_event, SetCursorPos,
     PostMessage/SendMessage). pynput *Listener* stays allowed: push-to-talk LISTENS for a key, it never sends one.
  2. NO IMPORT OF ANOTHER TOOL'S TRACKING. Not Mining_Signals, not Battle_Buddy, no OCR engine. The companion's eyes
     say WHAT is on screen (eyes.POSITION_WORDS keeps it that way); the trackers say WHERE. They never meet here.
  3. NO BORROWED HANDS. set_route_ai is the one toolbox tool that sends input (keystrokes written to the clipboard and
     played as a macro: an accessibility aid for plotting routes, and input injection by another name).
     Anyone who reads through all of the code will be able to connect the dots. So SuitMk2 may not import it either.

AST-based, so a docstring that NAMES a banned call (like this one) is not a violation; only code is.
    python narrator_seam.py            scan SuitMk2, rc 1 on any violation
    python narrator_seam.py --selftest scan + prove the scanner catches every banned pattern (positive control)
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # the SuitMk2 tool folder

BANNED_MODULES = {"pyautogui", "pydirectinput", "vgamepad", "interception", "keyboard", "mouse", "autoit",
                  "ahk", "pywinauto", "win32api", "win32con", "win32gui",
                  # other tools' tracking, and OCR engines
                  "Mining_Signals", "mining_signals", "Battle_Buddy", "battle_buddy", "set_route_ai", "set_route",
                  "pytesseract", "tesseract",
                  "rapidocr", "rapidocr_onnxruntime", "easyocr", "paddleocr"}
BANNED_CALLS = {"SendInput", "keybd_event", "mouse_event", "SetCursorPos", "PostMessageW", "PostMessageA",
                "PostMessage", "SendMessageW", "SendMessageA", "SendMessage", "SendKeys"}
BANNED_ATTRS = {"Controller"}                          # pynput.keyboard.Controller / pynput.mouse.Controller


def violations_in(src: str, name: str = "<src>") -> list[str]:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [f"{name}: cannot parse ({e.msg} line {e.lineno}) - an unreadable file cannot be cleared"]
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in BANNED_MODULES:
                    out.append(f"{name}:{node.lineno} import {a.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod.split(".")[0] in BANNED_MODULES:
                out.append(f"{name}:{node.lineno} from {mod} import ...")
            for a in node.names:
                if a.name in BANNED_ATTRS | BANNED_CALLS:
                    out.append(f"{name}:{node.lineno} from {mod} import {a.name}")
                # `from pynput import keyboard` is the LISTENER path and allowed; the stdlib has no 'mouse'/'keyboard'
                elif a.name in BANNED_MODULES and mod not in ("pynput",):
                    out.append(f"{name}:{node.lineno} from {mod} import {a.name}")
        elif isinstance(node, ast.Attribute) and node.attr in BANNED_ATTRS | BANNED_CALLS:
            out.append(f"{name}:{node.lineno} .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in BANNED_CALLS:
            out.append(f"{name}:{node.lineno} {node.id}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "__import__":
            if node.args and isinstance(node.args[0], ast.Constant) and \
                    str(node.args[0].value).split(".")[0] in BANNED_MODULES:
                out.append(f"{name}:{node.lineno} __import__({node.args[0].value!r})")
    return out


def scan(root: Path = ROOT) -> tuple[int, list[str]]:
    files = [p for p in root.rglob("*.py") if "__pycache__" not in p.parts and p.name != Path(__file__).name]
    bad = []
    for p in files:
        bad += violations_in(p.read_text(encoding="utf-8", errors="replace"), str(p.relative_to(root)))
    return len(files), bad


def _selftest() -> int:
    ok = True

    def case(label, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + label)
    # Positive control FIRST: a scanner that finds nothing proves nothing unless it can find something.
    must_catch = {
        "pynput keyboard Controller": "from pynput import keyboard\nk = keyboard.Controller()\n",
        "pynput Controller by name": "from pynput.mouse import Controller\n",
        "pyautogui": "import pyautogui\n",
        "pydirectinput": "import pydirectinput as p\n",
        "virtual gamepad": "from vgamepad import VX360Gamepad\n",
        "SendInput via ctypes": "import ctypes\nctypes.windll.user32.SendInput(1, None, 0)\n",
        "keybd_event": "import ctypes\nu = ctypes.windll.user32\nu.keybd_event(0x57, 0, 0, 0)\n",
        "SetCursorPos": "import ctypes\nctypes.windll.user32.SetCursorPos(10, 10)\n",
        "the keyboard package": "import keyboard\n",
        "another tool's tracker": "from Mining_Signals.core import tracker\n",
        "the route tool (it types via a clipboard macro)": "from set_route_ai import router\n",
        "an OCR engine": "import pytesseract\n",
        "dynamic import": "m = __import__('pyautogui')\n",
    }
    for label, src in must_catch.items():
        case(f"catches {label}", violations_in(src))
    must_allow = {
        "pynput keyboard Listener (push-to-talk)": "from pynput import keyboard\nl = keyboard.Listener(on_press=f)\n",
        "pynput mouse Listener": "from pynput import mouse\nl = mouse.Listener(on_click=f)\n",
        "a docstring naming SendInput": '"""never call SendInput or pyautogui"""\n',
        "HeadroomController (not .Controller)": "c = HeadroomController(now=0)\n",
    }
    for label, src in must_allow.items():
        case(f"allows {label}", not violations_in(src))
    n, bad = scan()
    case(f"SuitMk2 itself: {n} files, {len(bad)} violation(s)", n > 20 and not bad)
    for b in bad:
        print("        " + b)
    print("narrator_seam selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    n, bad = scan()
    print(f"narrator_seam: {n} files scanned, {len(bad)} violation(s)")
    for b in bad:
        print("  " + b)
    sys.exit(1 if bad else 0)
