# SC_Toolbox — unified skill launcher with global hotkeys
"""
SC_Toolbox: a compact tkinter overlay that shows skill tiles and provides
global hotkeys (via pynput) to toggle each skill's window.

Usage:
    python skill_launcher.py <x> <y> <w> <h> <opacity> <cmd_file>
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional

# Ensure shared/ package is importable
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared.ipc import ipc_write, ipc_read_and_clear

logger = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────────────
BG          = "#0b0e14"
BG2         = "#111620"
BG3         = "#161c28"
BG4         = "#1c2233"
BORDER      = "#1e2738"
FG          = "#c8d4e8"
FG_DIM      = "#5a6480"
FG_DIMMER   = "#3a4460"
ACCENT      = "#44aaff"
GREEN       = "#33dd88"
YELLOW      = "#ffaa22"
RED         = "#ff5533"
ORANGE      = "#ff7733"
PURPLE      = "#aa66ff"
CYAN        = "#33ccdd"
HEADER_BG   = "#0e1420"
CARD_BG     = "#141a26"
CARD_BORDER = "#1e2738"

# ── Default hotkey constants ─────────────────────────────────────────────────
LAUNCHER_HOTKEY = "<shift>+`"

# ── Settings file ────────────────────────────────────────────────────────────
_skill_dir = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_skill_dir, "skill_launcher_settings.json")

# ── Skill registry ───────────────────────────────────────────────────────────
# Each entry describes a skill the launcher can toggle.
SKILLS = [
    {
        "id":      "dps",
        "name":    "DPS Calculator",
        "icon":    "\u2694",   # crossed swords
        "color":   ORANGE,
        "folder":  "DPS_Calculator",
        "script":  "dps_calc_app.py",
        "hotkey":  "<shift>+1",
        "settings_key": "hotkey_dps",
    },
    {
        "id":      "cargo",
        "name":    "Cargo Loader",
        "icon":    "\U0001f4e6",  # package
        "color":   CYAN,
        "folder":  "Cargo_loader",
        "script":  "cargo_app.py",
        "hotkey":  "<shift>+2",
        "settings_key": "hotkey_cargo",
    },
    {
        "id":      "missions",
        "name":    "Mission Database",
        "icon":    "\U0001f4cb",  # clipboard
        "color":   GREEN,
        "folder":  "Mission_Database",
        "script":  "mission_db_app.py",
        "hotkey":  "<shift>+3",
        "settings_key": "hotkey_missions",
    },
    {
        "id":      "mining",
        "name":    "Mining Loadout",
        "icon":    "\u26cf",   # pick
        "color":   YELLOW,
        "folder":  "Mining_Loadout",
        "script":  "mining_loadout_app.py",
        "hotkey":  "<shift>+4",
        "settings_key": "hotkey_mining",
    },
    {
        "id":      "market",
        "name":    "Market Finder",
        "icon":    "\U0001f6d2",  # shopping cart
        "color":   PURPLE,
        "folder":  "Market_Finder",
        "script":  "uex_item_browser.py",
        "hotkey":  "<shift>+5",
        "settings_key": "hotkey_market",
    },
    {
        "id":      "trade",
        "name":    "Trade Hub",
        "icon":    "\U0001f4b0",  # money bag
        "color":   "#ffcc00",
        "folder":  "Trade_Hub",
        "script":  "trade_hub_app.py",
        "hotkey":  "<shift>+6",
        "settings_key": "hotkey_trade",
        "custom_args": ["300", "500"],  # refresh_interval, max_routes (before opacity)
    },
]


def _find_python() -> Optional[str]:
    """Find a system Python executable that has tkinter.

    Searches (in order):
      1. LOCALAPPDATA/Programs/Python/PythonXXX/  (standard installer)
      2. LOCALAPPDATA/Python/pythoncore-X.YY-64/   (Windows package manager / winget)
      3. LOCALAPPDATA/Python/                       (custom installs)
      4. Program Files/Python/PythonXXX/            (all-users install)
      5. C:/PythonXX/                               (legacy installs)
      6. PATH lookup via shutil.which
      7. sys.executable (the Python running THIS script)
    """
    import shutil
    candidates = []

    base = os.environ.get("LOCALAPPDATA", "")
    if base:
        # Standard Python.org installer
        for ver in ("314", "313", "312", "311", "310", "39", "38"):
            candidates.append(
                os.path.join(base, "Programs", "Python", f"Python{ver}", "python.exe"))

        # Windows package manager / winget installs (pythoncore-X.YY-64)
        py_local = os.path.join(base, "Python")
        if os.path.isdir(py_local):
            for d in sorted(os.listdir(py_local), reverse=True):
                p = os.path.join(py_local, d, "python.exe")
                if os.path.isfile(p):
                    candidates.append(p)

        candidates.append(os.path.join(base, "Python", "bin", "python.exe"))
        candidates.append(os.path.join(base, "Python", "python.exe"))

    # Program Files (all-users installs)
    pf = os.environ.get("ProgramFiles", "C:\\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")
    for base_dir in (pf, pf86):
        for ver in ("3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8"):
            candidates.append(
                os.path.join(base_dir, "Python",
                             f"Python{ver.replace('.', '')}", "python.exe"))

    # Legacy C:\PythonXX installs
    for ver in ("314", "313", "312", "311", "310", "39", "38"):
        candidates.append(f"C:\\Python{ver}\\python.exe")

    # PATH lookup
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    # Fallback: the Python running this script (if it has tkinter)
    candidates.append(sys.executable)

    for exe in candidates:
        if "WindowsApps" in exe:
            continue
        if not os.path.isfile(exe):
            continue
        try:
            result = subprocess.run(
                [exe, "-c", "import tkinter; print('ok')"],
                capture_output=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW)
            if result.returncode == 0 and b"ok" in result.stdout:
                return exe
        except Exception:
            continue
    return None


def get_hotkey_display(key: str) -> str:
    """Format a pynput hotkey string for display on a badge.
    '<shift>+1' -> 'S1', '<ctrl>+F2' -> '^F2', '<alt>+q' -> 'Aq', 'F5' -> 'F5'
    """
    if not key:
        return "—"
    s = key
    s = s.replace("<shift>+", "\u21e7")
    s = s.replace("<ctrl>+", "^")
    s = s.replace("<alt>+", "\u2325")
    s = s.replace("<cmd>+", "\u2318")
    # Remove any remaining angle brackets
    s = s.replace("<", "").replace(">", "")
    return s


def _load_settings() -> dict:
    try:
        if os.path.isfile(SETTINGS_FILE):
            with open(SETTINGS_FILE) as f:
                return json.load(f)
    except Exception as e:
        print(f"[SC_Toolbox] Warning: could not load settings: {e}")
    return {}


def _save_settings(data: dict):
    try:
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SETTINGS_FILE)
    except Exception as e:
        print(f"[SC_Toolbox] Warning: could not save settings: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# Skill Process Manager
# ══════════════════════════════════════════════════════════════════════════════

class SkillProcess:
    """Manages a single skill subprocess."""

    def __init__(self, skill_info: dict, python_exe: str, settings: dict):
        self.info = skill_info
        self._python = python_exe
        self._proc: Optional[subprocess.Popen] = None
        self._cmd_file: Optional[str] = None
        self._visible = False
        self._stopping = False
        self._settings = settings

        # Resolve paths — check local skills/ subfolder first, then parent custom_skills/
        local_skills = os.path.join(_skill_dir, "skills")
        local_folder = os.path.join(local_skills, skill_info["folder"])
        parent_skills = os.path.dirname(_skill_dir)
        parent_folder = os.path.join(parent_skills, skill_info["folder"])

        if os.path.isdir(local_folder):
            self._folder = local_folder
        else:
            self._folder = parent_folder
        self._script = os.path.join(self._folder, skill_info["script"])

    @property
    def available(self) -> bool:
        return os.path.isfile(self._script)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def visible(self) -> bool:
        return self._visible

    def launch(self):
        if self.running or not self.available:
            return
        import tempfile
        self._cmd_file = os.path.join(
            tempfile.gettempdir(),
            f"sc_toolbox_{self.info['id']}_{os.getpid()}.jsonl")
        with open(self._cmd_file, "w"):
            pass

        # Default window params
        sid = self.info["id"]
        try:
            x = int(self._settings.get(f"{sid}_x", 100))
        except (ValueError, TypeError):
            x = 100
        try:
            y = int(self._settings.get(f"{sid}_y", 100))
        except (ValueError, TypeError):
            y = 100
        try:
            w = int(self._settings.get(f"{sid}_w", 1300))
        except (ValueError, TypeError):
            w = 1300
        try:
            h = int(self._settings.get(f"{sid}_h", 800))
        except (ValueError, TypeError):
            h = 800
        try:
            opacity = float(self._settings.get(f"{sid}_opacity", 0.95))
        except (ValueError, TypeError):
            opacity = 0.95

        # Build launch args — some skills have custom args between h and opacity
        # e.g. Trade Hub: <x> <y> <w> <h> <refresh_interval> <max_routes> <opacity> <cmd>
        custom = self.info.get("custom_args", [])
        args = [self._python, self._script, str(x), str(y), str(w), str(h)]
        args.extend(custom)
        args.extend([str(opacity), self._cmd_file])

        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                cwd=self._folder,
                creationflags=subprocess.CREATE_NO_WINDOW)
        except Exception:
            # Clean up leaked temp file
            for f in (self._cmd_file, self._cmd_file + ".lock"):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self._cmd_file = None
            raise
        self._visible = True
        # Note: no blocking sleep — caller should use root.after() if delay needed

    def send(self, cmd: dict):
        if self._cmd_file and self.running:
            try:
                ipc_write(self._cmd_file, cmd)
            except Exception as e:
                print(f"[SC_Toolbox] Warning: IPC send failed for {self.info['id']}: {e}")

    def toggle(self):
        if not self.running:
            self.launch()
            if not self.running:
                return  # Launch failed — don't mark visible
            return
        if self._visible:
            self.send({"type": "hide"})
            self._visible = False
        else:
            self.send({"type": "show"})
            self._visible = True

    def show(self):
        if not self.running:
            self.launch()
        else:
            self.send({"type": "show"})
            self._visible = True

    def hide(self):
        if self.running:
            self.send({"type": "hide"})
            self._visible = False

    def stop(self):
        if not self.running or self._stopping:
            return
        self._stopping = True
        try:
            self.send({"type": "quit"})
            try:
                self._proc.wait(timeout=2)
            except Exception:
                if self._proc:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=1)
                    except Exception:
                        # Fallback: kill the entire process tree
                        try:
                            if self._proc.poll() is None:
                                subprocess.run(
                                    ["taskkill", "/F", "/T", "/PID", str(self._proc.pid)],
                                    capture_output=True, timeout=5,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
                        except Exception:
                            pass
            self._proc = None
            self._visible = False
            if self._cmd_file and os.path.exists(self._cmd_file):
                try:
                    os.remove(self._cmd_file)
                except Exception:
                    pass
                self._cmd_file = None
        finally:
            self._stopping = False


# ══════════════════════════════════════════════════════════════════════════════
# SC_Toolbox App
# ══════════════════════════════════════════════════════════════════════════════

class SCToolboxApp:
    """Main launcher window with skill tiles and global hotkeys."""

    def __init__(self, x, y, w, h, opacity, cmd_file):
        self.cmd_file = cmd_file
        self._running = threading.Event()
        self._running.set()
        self._settings = _load_settings()
        self._python = _find_python()

        # Load hotkeys from settings (override defaults)
        self._launcher_hotkey = self._settings.get("hotkey_launcher", LAUNCHER_HOTKEY)
        self._skills = [dict(s) for s in SKILLS]  # shallow copy to avoid mutating module-level list
        for skill in self._skills:
            saved = self._settings.get(skill["settings_key"], "")
            if saved:
                skill["hotkey"] = saved

        # Skill processes
        self._procs: dict[str, SkillProcess] = {}
        if self._python:
            for skill in self._skills:
                self._procs[skill["id"]] = SkillProcess(
                    skill, self._python, self._settings)

        self._hotkey_listener = None
        self._build_ui(x, y, w, h, opacity)
        self._start_hotkeys()
        self._start_cmd_watcher()

    def _build_ui(self, x, y, w, h, opacity):
        self.root = tk.Tk()
        self.root.title("SC_Toolbox")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", opacity)
        self.root.attributes("-topmost", True)
        self.root.minsize(400, 200)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TScrollbar", troughcolor=BG2, background=BORDER,
                        arrowcolor=FG_DIM)

        # ── Header ──
        header = tk.Frame(self.root, bg=HEADER_BG, height=40)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="SC", font=("Consolas", 12, "bold"),
                 bg=HEADER_BG, fg=ACCENT).pack(side="left", padx=(10, 0))
        tk.Label(header, text="_Toolbox", font=("Consolas", 12, "bold"),
                 bg=HEADER_BG, fg=FG).pack(side="left")

        # Launcher hotkey badge
        self._launcher_badge_var = tk.StringVar(
            value=get_hotkey_display(self._launcher_hotkey))
        tk.Label(header, textvariable=self._launcher_badge_var,
                 font=("Consolas", 8, "bold"),
                 bg="#1a2538", fg=ACCENT, padx=4, pady=1).pack(
                     side="left", padx=(8, 0))

        # Pledge Store link (centered)
        pledge_lbl = tk.Label(
            header, text="Pledge Store", font=("Consolas", 10, "bold"),
            bg=HEADER_BG, fg="#00ff66", cursor="hand2")
        pledge_lbl.pack(side="left", expand=True)
        pledge_lbl.bind("<Button-1>",
                        lambda e: __import__("webbrowser").open(
                            "https://robertsspaceindustries.com/en/pledge"))
        pledge_lbl.bind("<Enter>", lambda e: pledge_lbl.configure(fg="#66ff99"))
        pledge_lbl.bind("<Leave>", lambda e: pledge_lbl.configure(fg="#00ff66"))

        # Discord link
        discord_lbl = tk.Label(
            header, text="Discord", font=("Consolas", 8),
            bg=HEADER_BG, fg="#7289da", cursor="hand2")
        discord_lbl.pack(side="right", padx=(0, 10))
        discord_lbl.bind("<Button-1>",
                         lambda e: __import__("webbrowser").open("https://discord.gg/A7JDCxmC"))
        discord_lbl.bind("<Enter>", lambda e: discord_lbl.configure(fg="#99aaff"))
        discord_lbl.bind("<Leave>", lambda e: discord_lbl.configure(fg="#7289da"))

        # Status
        self._status_var = tk.StringVar(value="Ready")
        self._status_label = tk.Label(
            header, textvariable=self._status_var, font=("Consolas", 8),
            bg=HEADER_BG, fg=FG_DIM)
        self._status_label.pack(side="right", padx=10)

        # Python info
        if self._python:
            parts = self._python.split(os.sep)
            py_info = parts[-2] if len(parts) >= 2 else "Python"
            py_color = FG_DIMMER
        else:
            py_info = "Python not found!"
            py_color = RED
            self._status_var.set("Install Python 3.10+ from python.org")
        tk.Label(header, text=py_info, font=("Consolas", 7),
                 bg=HEADER_BG, fg=py_color).pack(side="right", padx=(0, 6))

        # ── Skill tiles ──
        self._tiles_frame = tk.Frame(self.root, bg=BG)
        self._tiles_frame.pack(fill="both", expand=True, padx=10, pady=10)

        self._tile_widgets = {}
        self._hotkey_badge_vars = {}

        for idx, skill in enumerate(self._skills):
            self._build_tile(skill, idx)

        # ── Settings panel (collapsible) ──
        self._settings_visible = False
        self._settings_frame = tk.Frame(self.root, bg=BG2)
        # Not packed initially

        self._settings_toggle = tk.Button(
            self.root, text="\u2699 Settings & Keybinds", font=("Consolas", 9, "bold"),
            bg="#1a2538", fg=ACCENT, relief="flat", bd=0, cursor="hand2",
            padx=10, pady=6, activebackground="#223050", activeforeground=ACCENT,
            command=self._toggle_settings)
        self._settings_toggle.pack(fill="x", padx=10, pady=(0, 8))

        self._build_settings_panel()

    def _build_tile(self, skill: dict, idx: int):
        """Build a single skill tile with icon, name, hotkey badge, toggle button."""
        sid = skill["id"]
        proc = self._procs.get(sid)
        available = proc.available if proc else False
        color = skill["color"] if available else FG_DIMMER

        tile = tk.Frame(self._tiles_frame, bg=CARD_BG, cursor="hand2",
                        highlightbackground=CARD_BORDER, highlightthickness=1)
        tile.grid(row=idx // 2, column=idx % 2, padx=6, pady=6, sticky="nsew")
        self._tiles_frame.columnconfigure(idx % 2, weight=1)
        self._tiles_frame.rowconfigure(idx // 2, weight=1)

        inner = tk.Frame(tile, bg=CARD_BG, padx=12, pady=10)
        inner.pack(fill="both", expand=True)

        # Row 1: icon + name + hotkey badge
        row1 = tk.Frame(inner, bg=CARD_BG)
        row1.pack(fill="x")

        tk.Label(row1, text=skill["icon"], font=("Consolas", 16),
                 bg=CARD_BG, fg=color).pack(side="left", padx=(0, 8))

        tk.Label(row1, text=skill["name"], font=("Consolas", 10, "bold"),
                 bg=CARD_BG, fg=FG if available else FG_DIMMER,
                 anchor="w").pack(side="left", fill="x", expand=True)

        # Hotkey badge
        hk_var = tk.StringVar(value=get_hotkey_display(skill["hotkey"]))
        self._hotkey_badge_vars[sid] = hk_var
        tk.Label(row1, textvariable=hk_var, font=("Consolas", 8, "bold"),
                 bg="#1a2538", fg=color, padx=4, pady=1).pack(side="right")

        # Row 2: status + launch button
        row2 = tk.Frame(inner, bg=CARD_BG)
        row2.pack(fill="x", pady=(6, 0))

        status_var = tk.StringVar(
            value="Available" if available else "Not installed")
        status_lbl = tk.Label(row2, textvariable=status_var,
                              font=("Consolas", 8),
                              bg=CARD_BG, fg=FG_DIM if available else RED)
        status_lbl.pack(side="left")

        if available:
            launch_btn = tk.Button(
                row2, text="\u25b6 Launch", font=("Consolas", 8, "bold"),
                bg="#1a3020", fg=GREEN, relief="flat", bd=0,
                cursor="hand2", padx=8, pady=2,
                command=lambda s=sid: self._toggle_skill(s))
            launch_btn.pack(side="right")
        else:
            tk.Label(row2, text="—", font=("Consolas", 8),
                     bg=CARD_BG, fg=FG_DIMMER).pack(side="right")

        self._tile_widgets[sid] = {
            "tile": tile, "status_var": status_var,
            "status_lbl": status_lbl,
        }

        # Hover effect
        def _enter(e, t=tile):
            t.configure(highlightbackground=ACCENT)
        def _leave(e, t=tile):
            t.configure(highlightbackground=CARD_BORDER)

        def _bind_all(widget, enter_fn, leave_fn):
            widget.bind("<Enter>", enter_fn)
            widget.bind("<Leave>", leave_fn)
            for child in widget.winfo_children():
                _bind_all(child, enter_fn, leave_fn)
        _bind_all(tile, _enter, _leave)

        # Click anywhere on tile to toggle
        def _click(e, s=sid):
            self._toggle_skill(s)
        def _bind_click(widget, fn):
            widget.bind("<Button-1>", fn)
            for child in widget.winfo_children():
                _bind_click(child, fn)
        if available:
            _bind_click(tile, _click)

    def _build_settings_panel(self):
        """Build the collapsible settings panel with keybind customization."""
        sf = self._settings_frame
        pad = {"padx": 10, "pady": (0, 4)}

        # ── KEYBINDS section ──
        tk.Label(sf, text="KEYBINDS", font=("Consolas", 9, "bold"),
                 bg=BG2, fg=ACCENT, anchor="w").pack(
                     fill="x", padx=10, pady=(8, 4))

        hint = tk.Label(sf, text="Format: <shift>+1  <ctrl>+F2  <alt>+q  F5",
                        font=("Consolas", 7), bg=BG2, fg=FG_DIMMER, anchor="w")
        hint.pack(fill="x", padx=10, pady=(0, 6))

        self._keybind_entries = {}

        # Launcher toggle keybind
        row = tk.Frame(sf, bg=BG2)
        row.pack(fill="x", **pad)
        tk.Label(row, text="SC_Toolbox", font=("Consolas", 9),
                 bg=BG2, fg=FG, width=18, anchor="w").pack(side="left")
        tk.Label(row, text="Hotkey:", font=("Consolas", 8),
                 bg=BG2, fg=FG_DIM).pack(side="left", padx=(0, 4))
        launcher_entry = tk.Entry(
            row, font=("Consolas", 9), bg=BG4, fg=FG,
            insertbackground="white", relief="flat", width=14,
            highlightthickness=1, highlightcolor=ACCENT,
            highlightbackground=BORDER)
        launcher_entry.insert(0, self._launcher_hotkey)
        launcher_entry.pack(side="left")
        self._keybind_entries["launcher"] = launcher_entry

        # Per-skill keybind entries
        for skill in self._skills:
            row = tk.Frame(sf, bg=BG2)
            row.pack(fill="x", **pad)
            tk.Label(row, text=f"{skill['icon']} {skill['name']}",
                     font=("Consolas", 9),
                     bg=BG2, fg=FG, width=18, anchor="w").pack(side="left")
            tk.Label(row, text="Hotkey:", font=("Consolas", 8),
                     bg=BG2, fg=FG_DIM).pack(side="left", padx=(0, 4))
            entry = tk.Entry(
                row, font=("Consolas", 9), bg=BG4, fg=FG,
                insertbackground="white", relief="flat", width=14,
                highlightthickness=1, highlightcolor=ACCENT,
                highlightbackground=BORDER)
            entry.insert(0, skill["hotkey"])
            entry.pack(side="left")
            self._keybind_entries[skill["id"]] = entry

        # Apply button + status
        btn_row = tk.Frame(sf, bg=BG2)
        btn_row.pack(fill="x", padx=10, pady=(6, 8))

        self._keybind_status_var = tk.StringVar(value="")
        tk.Label(btn_row, textvariable=self._keybind_status_var,
                 font=("Consolas", 8), bg=BG2, fg=GREEN).pack(
                     side="left", padx=(0, 8))

        tk.Button(btn_row, text="Apply Hotkeys", font=("Consolas", 8, "bold"),
                  bg="#1a3020", fg=ACCENT, relief="flat", bd=0,
                  cursor="hand2", padx=10, pady=3,
                  command=self._apply_hotkeys).pack(side="right")

        # Separator
        tk.Frame(sf, bg=BORDER, height=1).pack(fill="x", padx=10, pady=(0, 6))

        # ── WINDOW POSITION section (placeholder for future) ──
        tk.Label(sf, text="Window positions are stored per-skill in settings.",
                 font=("Consolas", 7), bg=BG2, fg=FG_DIMMER, anchor="w").pack(
                     fill="x", padx=10, pady=(0, 8))

    def _toggle_settings(self):
        if self._settings_visible:
            self._settings_frame.pack_forget()
            self._settings_visible = False
            self._settings_toggle.configure(
                text="\u2699 Settings & Keybinds",
                bg="#1a2538", fg=ACCENT)
        else:
            self._settings_frame.pack(fill="x", before=self._settings_toggle)
            self._settings_visible = True
            self._settings_toggle.configure(
                text="\u25b2 Close Settings",
                bg="#2a1a18", fg=ORANGE)

    # ── Hotkey management ────────────────────────────────────────────────────

    def _start_hotkeys(self):
        """Start the pynput global hotkey listener."""
        try:
            from pynput.keyboard import GlobalHotKeys
        except ImportError:
            self._status_var.set("pynput not installed — hotkeys disabled")
            return

        bindings = self._build_hotkey_bindings()
        if not bindings:
            return

        try:
            self._hotkey_listener = GlobalHotKeys(bindings)
            self._hotkey_listener.daemon = True
            self._hotkey_listener.start()
        except Exception as e:
            self._status_var.set(f"Hotkey error: {e}")

    def _stop_hotkeys(self):
        if self._hotkey_listener:
            try:
                self._hotkey_listener.stop()
            except Exception:
                pass
            self._hotkey_listener = None

    def _build_hotkey_bindings(self) -> dict:
        bindings = {}

        # Launcher toggle
        if self._launcher_hotkey:
            bindings[self._launcher_hotkey] = lambda: self.root.after(
                0, self._toggle_launcher_visibility)

        # Per-skill toggles
        for skill in self._skills:
            hk = skill["hotkey"]
            sid = skill["id"]
            if hk:
                bindings[hk] = lambda s=sid: self.root.after(
                    0, self._toggle_skill, s)

        return bindings

    def _apply_hotkeys(self):
        """Read entries, validate, restart listener, save settings."""
        new_launcher = self._keybind_entries["launcher"].get().strip()
        new_skills = {}
        for skill in self._skills:
            val = self._keybind_entries[skill["id"]].get().strip()
            new_skills[skill["id"]] = val

        # Validate: non-empty, contains at least one non-modifier char
        for key, val in [("launcher", new_launcher)] + list(new_skills.items()):
            if val and not any(c.isalnum() or c in "`~!@#$%^&*" for c in val):
                self._keybind_status_var.set(f"\u2717 Invalid hotkey: {val}")
                self._status_label.configure(fg=RED)
                def _clear_invalid():
                    self._keybind_status_var.set("")
                    self._status_label.configure(fg=FG_DIM)
                self.root.after(3000, _clear_invalid)
                return

        # Stop current listener
        self._stop_hotkeys()

        # Apply new values
        self._launcher_hotkey = new_launcher
        for skill in self._skills:
            skill["hotkey"] = new_skills[skill["id"]]

        # Try starting with new bindings
        try:
            from pynput.keyboard import GlobalHotKeys
            bindings = self._build_hotkey_bindings()
            if bindings:
                self._hotkey_listener = GlobalHotKeys(bindings)
                self._hotkey_listener.daemon = True
                self._hotkey_listener.start()
        except Exception as e:
            self._keybind_status_var.set(f"\u2717 Error: {e}")
            self._status_label.configure(fg=RED)
            def _clear_error():
                self._keybind_status_var.set("")
                self._status_label.configure(fg=FG_DIM)
            self.root.after(3000, _clear_error)
            return

        # Save to settings
        self._settings["hotkey_launcher"] = new_launcher
        for skill in self._skills:
            self._settings[skill["settings_key"]] = skill["hotkey"]
        _save_settings(self._settings)

        # Update badge displays
        self._launcher_badge_var.set(get_hotkey_display(self._launcher_hotkey))
        for skill in self._skills:
            sid = skill["id"]
            if sid in self._hotkey_badge_vars:
                self._hotkey_badge_vars[sid].set(
                    get_hotkey_display(skill["hotkey"]))

        # Success message
        self._keybind_status_var.set("\u2713 Hotkeys applied")
        self._status_label.configure(fg=GREEN)
        def _clear_success():
            self._keybind_status_var.set("")
            self._status_label.configure(fg=FG_DIM)
        self.root.after(2000, _clear_success)

    # ── Skill toggle ─────────────────────────────────────────────────────────

    def _toggle_skill(self, skill_id: str):
        proc = self._procs.get(skill_id)
        if not proc:
            return
        proc.toggle()
        # Update tile status
        tw = self._tile_widgets.get(skill_id)
        if tw:
            if proc.running:
                tw["status_var"].set("Running" if proc.visible else "Hidden")
                tw["status_lbl"].configure(fg=GREEN if proc.visible else YELLOW)
            else:
                tw["status_var"].set("Available")
                tw["status_lbl"].configure(fg=FG_DIM)

    def _toggle_launcher_visibility(self):
        state = self.root.state()
        if state in ("withdrawn", "iconic"):
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        else:
            self.root.withdraw()

    # ── JSONL command watcher ────────────────────────────────────────────────

    def _start_cmd_watcher(self):
        if not self.cmd_file or self.cmd_file == "NUL":
            return
        t = threading.Thread(target=self._watch_cmds, daemon=True)
        t.start()

    def _watch_cmds(self):
        while self._running.is_set():
            try:
                if not os.path.isfile(self.cmd_file):
                    time.sleep(0.5)
                    continue
                try:
                    commands = ipc_read_and_clear(self.cmd_file)
                except (OSError, IOError):
                    time.sleep(0.5)
                    continue
                if not commands:
                    time.sleep(0.3)
                    continue
                for cmd in commands:
                    try:
                        self.root.after(0, self._dispatch, cmd)
                    except tk.TclError:
                        return  # Root destroyed
            except Exception as e:
                print(f"[SC_Toolbox] Command watcher error: {e}")
            time.sleep(0.3)

    def _dispatch(self, cmd: dict):
        t = cmd.get("type", "")
        if t == "show":
            self.root.deiconify()
            self.root.lift()
        elif t == "hide":
            self.root.withdraw()
        elif t == "quit":
            self._shutdown()
        elif t == "toggle_skill":
            sid = cmd.get("skill_id", "")
            if sid:
                self._toggle_skill(sid)
        elif t == "launch_skill":
            sid = cmd.get("skill_id", "")
            proc = self._procs.get(sid)
            if proc:
                proc.show()
        elif t == "stop_skill":
            sid = cmd.get("skill_id", "")
            proc = self._procs.get(sid)
            if proc:
                proc.stop()

    def _shutdown(self):
        self._running.clear()
        self._stop_hotkeys()
        for proc in self._procs.values():
            proc.stop()
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._shutdown)
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    from shared.platform_utils import set_dpi_awareness
    set_dpi_awareness()

    args = sys.argv[1:]

    def _int(idx, default):
        try:
            return int(args[idx]) if len(args) > idx else default
        except (ValueError, IndexError):
            return default

    def _float(idx, default):
        try:
            return float(args[idx]) if len(args) > idx else default
        except (ValueError, IndexError):
            return default

    x       = _int(0, 100)
    y       = _int(1, 100)
    w       = _int(2, 500)
    h       = _int(3, 400)
    opacity = _float(4, 0.95)
    cmd     = args[5] if len(args) > 5 else "NUL"

    # Clamp to screen bounds to prevent off-screen windows
    try:
        _probe = tk.Tk()
        screen_w = _probe.winfo_screenwidth()
        screen_h = _probe.winfo_screenheight()
        _probe.destroy()
        x = max(0, min(x, screen_w - w))
        y = max(0, min(y, screen_h - h))
    except Exception:
        pass

    app = SCToolboxApp(x, y, w, h, opacity, cmd)
    app.run()


if __name__ == "__main__":
    main()
