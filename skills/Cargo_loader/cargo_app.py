"""
Cargo Loader — Star Citizen cargo grid viewer and container optimizer.
Launched as a subprocess by main.py via WingmanAI.
Data sourced from sc-cargo.space (JS bundle, auto-detected URL).

Args: <x> <y> <w> <h> <opacity> <cmd_file>
"""
import json
import logging
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import requests

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from shared.ipc import ipc_read_and_clear
from shared.data_utils import parse_cli_args
from shared.platform_utils import set_dpi_awareness

from cargo_common import (
    CONTAINER_SIZES, CONTAINER_DIMS, CONTAINER_MAX_CH,
    load_reference_loadouts, find_reference_loadout,
    best_rotation, max_containers_in_slot,
    place_containers_3d, build_slots,
    greedy_optimize_3d, assign_slots_from_counts,
)

log = logging.getLogger(__name__)

# ── Paths ──────────────────────────────────────────────────────────────────────
_DIR       = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(_DIR, ".cargo_cache.json")
CACHE_TTL  = 6 * 3600

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


REFERENCE_LOADOUTS: dict[str, dict[int, int]] = load_reference_loadouts(_DIR)

# ── Layout JSON loader (cargo_grid_editor.html exports) ───────────────────────
LAYOUTS_DIR = os.path.join(_DIR, "layouts")

def _load_ship_layouts() -> dict[str, dict]:
    """Load all *_cargo_layout.json files from the layouts/ directory.
    Returns {ship_name_lower: layout_data} where layout_data has
    placements, containers, gridW/gridZ/gridH, totalCapacity.
    """
    result: dict[str, dict] = {}
    if not os.path.isdir(LAYOUTS_DIR):
        return result
    import glob
    for path in glob.glob(os.path.join(LAYOUTS_DIR, "*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            ship = data.get("ship", "")
            if ship and ship != "Custom":
                result[ship.lower()] = data
        except Exception as exc:
            print(f"[Cargo] Failed to load layout {path}: {exc}")
            continue
    return result

# Pre-load at import time (fast — just JSON parsing, no network)
SHIP_LAYOUTS = _load_ship_layouts()


def _layout_to_slots(layout: dict) -> tuple[list[dict], tuple]:
    """Convert a cargo_grid_editor layout (placements) into slot list + bounds.
    Each placement becomes its own slot with exact position and dimensions.

    IMPORTANT: The HTML editor stores dims with rotation ALREADY APPLIED.
    The saved w/h/l are the final footprint — do NOT swap them again.
    """
    placements = layout.get("placements", [])
    if not placements:
        return [], (0, 0, 1, 1)

    slots = []
    for p in placements:
        dims = p["dims"]
        # Dims are already rotated in the JSON — use as-is
        pw = dims["w"]
        ph = dims["h"]
        pl = dims["l"]
        px = p["pos"]["x"]
        py = p["pos"]["y"]
        pz = p["pos"]["z"]

        slots.append({
            "x": px, "y0": py, "z": pz,
            "w": pw, "h": ph, "l": pl,
            "capacity": p["scu"],
            "scu": p["scu"],
            "placed_size": p["scu"],
            "maxSize": p["scu"],
            "minSize": p["scu"],
        })

    x_min = min(s["x"]           for s in slots)
    z_min = min(s["z"]           for s in slots)
    x_max = max(s["x"] + s["w"]  for s in slots)
    z_max = max(s["z"] + s["l"]  for s in slots)
    return slots, (x_min, z_min, x_max, z_max)


def _find_reference_loadout(ship_name: str) -> dict[int, int] | None:
    """Thin wrapper for backward compat -- delegates to cargo_common."""
    return find_reference_loadout(ship_name, REFERENCE_LOADOUTS)


# ── Palette ────────────────────────────────────────────────────────────────────
BG        = "#0f1117"
BG2       = "#181c26"
BG3       = "#1e2333"
BORDER    = "#2a3050"
FG        = "#d0d8f0"
FG_DIM    = "#6a7490"
ACCENT    = "#4af"
GREEN     = "#3d9"
YELLOW    = "#fb3"
RED       = "#f64"
HEADER_BG = "#141826"

CONT_COL = {
    1:  "#2196f3",
    2:  "#00bcd4",
    4:  "#4caf50",
    8:  "#ff9800",
    16: "#9c27b0",
    24: "#c8a882",   # tan/khaki like the sc-cargo screenshot
    32: "#e91e63",
}
GRID_LINE   = "#1e2740"
SLOT_FILL   = "#111827"
SLOT_OUTLINE= "#252f48"


# ── Fuzzy-search combobox ─────────────────────────────────────────────────────

class FuzzyCombo(tk.Frame):
    """Drop-down combo with fuzzy-search filtering.

    Typing in the entry narrows the listbox to items whose name contains
    every whitespace-separated token (case-insensitive, order-independent).
    """

    def __init__(self, parent, width=30, font=("Consolas", 10),
                 on_select=None, **kw):
        super().__init__(parent, bg=kw.pop("bg", parent.cget("bg")))
        self._all_values: list[str] = []
        self._on_select = on_select
        self._open = False

        # Entry
        self._var = tk.StringVar()
        self._entry = tk.Entry(
            self, textvariable=self._var, width=width, font=font,
            bg=BG2, fg=FG, insertbackground=FG, relief="flat",
            highlightthickness=1, highlightcolor=ACCENT,
            highlightbackground=BORDER, state="disabled")
        self._entry.pack(side="left", fill="x", expand=True)

        # Arrow button
        self._arrow = tk.Label(
            self, text="\u25bc", font=("Consolas", 8), bg=BG2, fg=FG_DIM,
            cursor="hand2", padx=4)
        self._arrow.pack(side="left")
        self._arrow.bind("<Button-1>", lambda e: self._toggle_dropdown())

        # Popup toplevel (created lazily)
        self._popup: tk.Toplevel | None = None
        self._listbox: tk.Listbox | None = None

        # Bindings
        self._var.trace_add("write", lambda *_: self._on_typing())
        self._entry.bind("<Return>", lambda e: self._pick_top())
        self._entry.bind("<Down>", lambda e: self._move_selection(1))
        self._entry.bind("<Up>", lambda e: self._move_selection(-1))
        self._entry.bind("<Escape>", lambda e: self._close_dropdown())
        self._entry.bind("<FocusIn>", lambda e: self._show_dropdown())
        self._entry.bind("<FocusOut>", lambda e: self.after(150, self._close_dropdown))

    # -- public API -----------------------------------------------------------

    def configure_values(self, values: list[str]):
        self._all_values = list(values)
        self._entry.configure(state="normal")

    def set(self, value: str):
        self._var.set(value)

    def get(self) -> str:
        return self._var.get()

    def configure_state(self, state: str):
        self._entry.configure(state=state)

    # -- fuzzy filter ---------------------------------------------------------

    @staticmethod
    def _fuzzy_match(query: str, item: str) -> bool:
        tokens = query.lower().split()
        item_low = item.lower()
        return all(t in item_low for t in tokens)

    def _filtered(self) -> list[str]:
        q = self._var.get().strip()
        if not q:
            return self._all_values
        return [v for v in self._all_values if self._fuzzy_match(q, v)]

    # -- dropdown management --------------------------------------------------

    def _ensure_popup(self):
        if self._popup and self._popup.winfo_exists():
            return
        self._popup = tk.Toplevel(self)
        self._popup.overrideredirect(True)
        self._popup.attributes("-topmost", True)
        self._popup.configure(bg=BORDER)

        self._listbox = tk.Listbox(
            self._popup, bg=BG2, fg=FG, font=("Consolas", 10),
            selectbackground=ACCENT, selectforeground="#fff",
            relief="flat", bd=1, highlightthickness=0,
            activestyle="none", exportselection=False)
        self._listbox.pack(fill="both", expand=True, padx=1, pady=1)
        self._listbox.bind("<ButtonRelease-1>", lambda e: self._pick_current())
        self._listbox.bind("<Motion>", self._on_listbox_motion)

    def _show_dropdown(self):
        self._ensure_popup()
        self._refresh_listbox()
        # Position below the entry
        x = self._entry.winfo_rootx()
        y = self._entry.winfo_rooty() + self._entry.winfo_height()
        w = self._entry.winfo_width() + self._arrow.winfo_width()
        items = self._listbox.size()
        h = min(items, 12) * 20 + 4
        if h < 24:
            h = 24
        self._popup.geometry(f"{w}x{h}+{x}+{y}")
        self._popup.deiconify()
        self._open = True

    def _close_dropdown(self):
        if self._popup and self._popup.winfo_exists():
            self._popup.withdraw()
        self._open = False

    def _toggle_dropdown(self):
        if self._open:
            self._close_dropdown()
        else:
            self._entry.focus_set()
            self._show_dropdown()

    def _refresh_listbox(self):
        if not self._listbox:
            return
        self._listbox.delete(0, "end")
        for item in self._filtered():
            self._listbox.insert("end", item)
        if self._listbox.size() > 0:
            self._listbox.selection_set(0)
            self._listbox.see(0)

    # -- interaction ----------------------------------------------------------

    def _on_typing(self):
        if self._open:
            self._refresh_listbox()
        elif self._entry.focus_get() == self._entry:
            self._show_dropdown()

    def _on_listbox_motion(self, event):
        idx = self._listbox.nearest(event.y)
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(idx)

    def _pick_top(self):
        if self._listbox and self._listbox.size() > 0:
            sel = self._listbox.curselection()
            idx = sel[0] if sel else 0
            value = self._listbox.get(idx)
            self._var.set(value)
            self._close_dropdown()
            if self._on_select:
                self._on_select(value)

    def _pick_current(self):
        if not self._listbox:
            return
        sel = self._listbox.curselection()
        if sel:
            value = self._listbox.get(sel[0])
            self._var.set(value)
            self._close_dropdown()
            if self._on_select:
                self._on_select(value)

    def _move_selection(self, delta: int):
        if not self._listbox or self._listbox.size() == 0:
            return
        sel = self._listbox.curselection()
        idx = sel[0] if sel else -1
        idx = max(0, min(self._listbox.size() - 1, idx + delta))
        self._listbox.selection_clear(0, "end")
        self._listbox.selection_set(idx)
        self._listbox.see(idx)


# ── Colour helpers ─────────────────────────────────────────────────────────────

def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    return int(h[1:3], 16), int(h[3:5], 16), int(h[5:7], 16)

def _rgb_to_hex(r: int, g: int, b: int) -> str:
    return (
        f"#{max(0, min(255, r)):02x}"
        f"{max(0, min(255, g)):02x}"
        f"{max(0, min(255, b)):02x}"
    )

def _shade(hex_col: str, factor: float) -> str:
    r, g, b = _hex_to_rgb(hex_col)
    return _rgb_to_hex(int(r * factor), int(g * factor), int(b * factor))

def _label_color(hex_col: str) -> str:
    """White label on dark base, dark label on light base."""
    r, g, b = _hex_to_rgb(hex_col)
    lum = 0.299 * r + 0.587 * g + 0.114 * b
    return "#000000" if lum > 140 else "#ffffff"


# ── Isometric projection ───────────────────────────────────────────────────────
#
#  Camera sits at (+∞ X, +∞ Y, +∞ Z) looking toward origin.
#  Visible faces per box: TOP (y=max), RIGHT (x=max), LEFT/FRONT (z=max).
#
#  Screen axes:
#    +X world  →  (+cell,  +cell*0.5)  screen   (right and down)
#    +Z world  →  (-cell,  +cell*0.5)  screen   (left  and down)
#    +Y world  →  (0,      -cell    )  screen   (straight up)

def iso_pt(wx: float, wy: float, wz: float,
           cell: float, ox: float, oy: float) -> tuple[int, int]:
    """World (X, Y, Z) → integer screen (sx, sy)."""
    sx = ox + (wx - wz) * cell
    sy = oy + (wx + wz) * cell * 0.5 - wy * cell
    return int(sx), int(sy)


# ── Data loader ────────────────────────────────────────────────────────────────

class ShipDataLoader:
    def __init__(self):
        self._lock = threading.Lock()
        self.ships: list   = []
        self.by_name: dict = {}
        self.error: str    = ""
        self.loaded        = False

    def load_async(self, callback):
        t = threading.Thread(target=self._run, args=(callback,), daemon=True)
        t.start()

    def _run(self, callback):
        try:
            cached = self._load_cache()
            if cached:
                self._index(cached)
            else:
                ships = self._fetch_and_parse()
                self._save_cache(ships)
                self._index(ships)
        except Exception as e:
            with self._lock:
                self.error = str(e)
        finally:
            if not self.loaded:
                with self._lock:
                    self.loaded = True  # unblock UI even on failure
            if callback:
                callback()  # callback fires via root.after(); sets self.loaded there

    def _fetch_and_parse(self) -> list:
        try:
            r = requests.get("https://sc-cargo.space/", headers=HEADERS, timeout=10)
            m = re.search(r'src="(/assets/index-[^"]+\.js)"', r.text)
            if m:
                bundle_url = "https://sc-cargo.space" + m.group(1)
            else:
                raise RuntimeError("Could not find bundle URL on sc-cargo.space homepage")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Failed to fetch sc-cargo.space homepage: {exc}") from exc

        r = requests.get(bundle_url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        ships = self._parse_js(r.text)
        if not ships:
            raise ValueError("No ships parsed from bundle — the JS format may have changed")
        return ships

    def _parse_js(self, text: str) -> list:
        str_vars: dict[str, str] = {}
        for m in re.finditer(r'([A-Za-z_$][A-Za-z0-9_$]*)="([^"]{1,100})"', text):
            str_vars[m.group(1)] = m.group(2)

        ship_refs = []
        for m in re.finditer(
            r'\{manufacturer:([A-Za-z_$][A-Za-z0-9_$]*)'
            r',name:([A-Za-z_$][A-Za-z0-9_$]*)'
            r',official:([A-Za-z_$][A-Za-z0-9_$]*)\}',
            text,
        ):
            ship_refs.append((m.group(1), m.group(2), m.group(3)))

        ships = []
        for mfr_v, name_v, off_v in ship_refs:
            raw = self._extract_obj(text, off_v)
            if not raw:
                continue
            try:
                j = re.sub(r'(?<!\w)!0(?!\w)', 'true', raw)
                j = re.sub(r'(?<!\w)!1(?!\w)', 'false', j)
                j = re.sub(r'([{,\[])([A-Za-z_$][A-Za-z0-9_$]*):', r'\1"\2":', j)
                obj = json.loads(j)
                ships.append({
                    "manufacturer": str_vars.get(mfr_v, mfr_v),
                    "name":         str_vars.get(name_v, name_v),
                    **obj,
                })
            except Exception as exc:
                print(f"[Cargo] Failed to parse ship object: {exc}")
        return ships

    def _extract_obj(self, text: str, var_name: str) -> str | None:
        marker    = var_name + "={capacity:"
        start     = text.find(marker)
        if start == -1:
            return None
        obj_start = start + len(var_name) + 1
        depth = 0
        i     = obj_start
        while i < len(text):
            if   text[i] == "{": depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    return text[obj_start : i + 1]
            i += 1
        return None

    def _index(self, ships: list):
        local_by_name = {s["name"].lower(): s for s in ships}
        with self._lock:
            self.ships   = ships
            self.by_name = local_by_name

    # Ships to exclude from dropdown (API duplicates with hyphenated names)
    _EXCLUDE = {"idris-m", "idris-p", "idrisp"}

    def get_ship_names(self) -> list[str]:
        # Start with erkul ships
        with self._lock:
            ships = self.ships
        names = set(
            s["name"] for s in ships
            if s["name"].lower() not in self._EXCLUDE
        )
        # Add layout-only ships (not in erkul but have cargo layouts)
        for layout_key, layout in SHIP_LAYOUTS.items():
            display = layout.get("ship") or layout.get("shipName") or layout_key.title()
            if display.lower() not in {n.lower() for n in names}:
                names.add(display)
        return sorted(names)

    def find(self, name: str):
        if not name:
            return None
        with self._lock:
            by_name = self.by_name
        key = name.strip().lower()
        if key in by_name:
            return by_name[key]
        # Check layout-only ships (exact match) before fuzzy API lookups
        for layout_key, layout in SHIP_LAYOUTS.items():
            display = layout.get("ship") or layout.get("shipName") or layout_key.title()
            if key == display.lower() or key == layout_key:
                cap = layout.get("totalCapacity", 0)
                return {
                    "name": display,
                    "ref": f"layout_{layout_key}",
                    "scu": cap,
                    "cargo": cap,
                    "maxSize": 32,
                    "minSize": 1,
                    "loadout": [],
                }
        for k, v in by_name.items():
            if key in k or k in key:
                return v
        tokens = set(key.split())
        best, best_score = None, 0
        for k, v in by_name.items():
            score = len(tokens & set(k.split()))
            if score > best_score:
                best, best_score = v, score
        if best_score >= 2:
            return best
        return None

    def _load_cache(self) -> list | None:
        if not os.path.exists(CACHE_FILE):
            return None
        try:
            with open(CACHE_FILE, encoding="utf-8") as f:
                obj = json.load(f)
            if not isinstance(obj, dict) or "ships" not in obj:
                log.warning("Cache file has unexpected structure, ignoring")
                return None
            if time.time() - obj.get("ts", 0) < CACHE_TTL:
                return obj["ships"]
        except Exception as exc:
            log.warning("Cache load failed: %s", exc)
        return None

    def _save_cache(self, ships: list):
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "ships": ships}, f)
        except Exception as exc:
            print(f"[Cargo] Cache save failed: {exc}")


# ── Main App ───────────────────────────────────────────────────────────────────

class CargoApp:
    def __init__(self):
        a = parse_cli_args(sys.argv[1:], {"w": 1200, "h": 700})
        x  = a["x"]
        y  = a["y"]
        w  = a["w"]
        h  = a["h"]
        op = a["opacity"]
        self._cmd_file = a["cmd_file"]

        self._current_ship: dict | None    = None
        self._slots:        list[dict]     = []
        self._bounds:       tuple          = (0, 0, 1, 1)
        self._slot_assignment: list[dict]  = []
        self._counts: dict[int, tk.IntVar] = {}

        self.root = tk.Tk()
        self.root.title("Cargo Loader")
        self.root.configure(bg=BG)
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", op)
        self.root.resizable(True, True)

        for s in CONTAINER_SIZES:
            self._counts[s] = tk.IntVar(value=0)

        self._build_ui()

        self._data = ShipDataLoader()
        self._data.load_async(lambda: self.root.after(0, self._on_data_loaded))
        self._status_var.set("Loading ship data from sc-cargo.space…")

        self._poll_commands()
        self.root.mainloop()

    # ── UI ─────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        style = ttk.Style()
        style.theme_use("clam")
        for widget in ("TCombobox", "TScrollbar",
                       "Vertical.TScrollbar", "Horizontal.TScrollbar"):
            style.configure(widget, background=BG2, fieldbackground=BG2,
                            foreground=FG, selectbackground=BG3,
                            bordercolor=BORDER, arrowcolor=FG_DIM,
                            troughcolor=BG)
        style.map("TCombobox", fieldbackground=[("readonly", BG2)],
                  selectbackground=[("readonly", BG2)])

        hdr = tk.Frame(self.root, bg=HEADER_BG, height=48)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        tk.Label(hdr, text="⬡  CARGO LOADER",
                 font=("Consolas", 12, "bold"), bg=HEADER_BG,
                 fg=ACCENT).pack(side="left", padx=12, pady=10)

        self._ship_cb = FuzzyCombo(
            hdr, width=30, font=("Consolas", 10), bg=HEADER_BG,
            on_select=lambda name: self._load_ship(name))
        self._ship_cb.pack(side="left", padx=8, pady=10)

        tk.Button(
            hdr, text="⟳", font=("Consolas", 11),
            bg=BG3, fg=FG_DIM, bd=0, padx=8,
            activebackground=BORDER, activeforeground=FG,
            command=self._refresh,
        ).pack(side="left", padx=2)

        self._status_var = tk.StringVar(value="—")
        tk.Label(hdr, textvariable=self._status_var,
                 font=("Consolas", 9), bg=HEADER_BG,
                 fg=FG_DIM).pack(side="right", padx=12)

        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True)

        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        self._build_grid_panel(left)

        tk.Frame(body, bg=BORDER, width=1).pack(side="left", fill="y")

        right = tk.Frame(body, bg=BG2, width=280)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)
        self._build_config_panel(right)

    def _build_grid_panel(self, parent):
        tb = tk.Frame(parent, bg=BG3, height=26)
        tb.pack(fill="x")
        tb.pack_propagate(False)
        tk.Label(tb, text="ISOMETRIC VIEW  ·  camera: +X +Y +Z  ·  top=bright  right=mid  left=dark",
                 font=("Consolas", 8), bg=BG3, fg=FG_DIM).pack(
                     side="left", padx=8, pady=4)
        self._grid_info_var = tk.StringVar(value="")
        tk.Label(tb, textvariable=self._grid_info_var,
                 font=("Consolas", 8), bg=BG3, fg=ACCENT).pack(
                     side="right", padx=8)

        cf = tk.Frame(parent, bg=BG)
        cf.pack(fill="both", expand=True, padx=4, pady=4)

        self._canvas = tk.Canvas(cf, bg=BG, highlightthickness=0, cursor="crosshair")
        vsb = ttk.Scrollbar(cf, orient="vertical",   command=self._canvas.yview)
        hsb = ttk.Scrollbar(cf, orient="horizontal", command=self._canvas.xview)
        self._canvas.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self._canvas.pack(fill="both", expand=True)

        self._canvas.bind("<MouseWheel>",
            lambda e: self._canvas.yview_scroll(-1 * (int(e.delta / 120) or (1 if e.delta > 0 else (-1 if e.delta < 0 else 0))), "units"))
        self._render_job = None
        def _on_configure(_evt):
            if self._render_job:
                self._canvas.after_cancel(self._render_job)
            self._render_job = self._canvas.after(50, self._render_grid)
        self._canvas.bind("<Configure>", _on_configure)

        leg = tk.Frame(parent, bg=BG3, height=24)
        leg.pack(fill="x")
        leg.pack_propagate(False)
        tk.Label(leg, text="■ Empty  ", font=("Consolas", 7),
                 bg=BG3, fg=FG_DIM).pack(side="left", padx=6, pady=4)
        for size in CONTAINER_SIZES:
            f = tk.Frame(leg, bg=CONT_COL[size], width=10, height=10)
            f.pack(side="left", padx=(3, 1), pady=6)
            tk.Label(leg, text=f"{size}", font=("Consolas", 7),
                     bg=BG3, fg=FG_DIM).pack(side="left", padx=(0, 3))
        tk.Label(leg, text="SCU", font=("Consolas", 7),
                 bg=BG3, fg=FG_DIM).pack(side="left")

    def _build_config_panel(self, parent):
        pad = tk.Frame(parent, bg=BG2)
        pad.pack(fill="both", expand=True, padx=12, pady=10)

        tk.Label(pad, text="CARGO CONFIGURATION",
                 font=("Consolas", 9, "bold"), bg=BG2,
                 fg=ACCENT, anchor="w").pack(fill="x", pady=(0, 8))

        cap_outer = tk.Frame(pad, bg=BG3, padx=8, pady=6)
        cap_outer.pack(fill="x", pady=(0, 10))

        cap_row = tk.Frame(cap_outer, bg=BG3)
        cap_row.pack(fill="x")
        tk.Label(cap_row, text="Capacity",
                 font=("Consolas", 9), bg=BG3, fg=FG_DIM).pack(side="left")
        self._cap_var = tk.StringVar(value="0 / 0 SCU")
        self._cap_lbl = tk.Label(cap_row, textvariable=self._cap_var,
                                 font=("Consolas", 9, "bold"), bg=BG3, fg=GREEN)
        self._cap_lbl.pack(side="right")

        self._bar_canvas = tk.Canvas(cap_outer, bg=BORDER, height=10,
                                     highlightthickness=0)
        self._bar_canvas.pack(fill="x", pady=(6, 0))
        self._bar_rect = self._bar_canvas.create_rectangle(
            0, 0, 0, 10, fill=GREEN, width=0)

        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=(0, 8))

        self._cont_labels: dict[int, tk.Label] = {}
        for size in CONTAINER_SIZES:
            row = tk.Frame(pad, bg=BG2)
            row.pack(fill="x", pady=2)
            tk.Frame(row, bg=CONT_COL[size], width=10, height=10).pack(
                side="left", padx=(0, 5))
            tk.Label(row, text=f"{size:>2} SCU",
                     font=("Consolas", 9), bg=BG2,
                     fg=FG, width=6, anchor="w").pack(side="left")
            var = self._counts[size]
            sb  = tk.Spinbox(
                row, textvariable=var, from_=0, to=9999,
                width=5, font=("Consolas", 9),
                bg=BG3, fg=FG, buttonbackground=BG3,
                insertbackground=FG, relief="flat", bd=1,
                command=self._update_fill,
            )
            sb.pack(side="left", padx=4)
            sb.bind("<KeyRelease>",
                    lambda _e: self.root.after(100, self._update_fill))
            lbl = tk.Label(row, text="=   0", font=("Consolas", 8),
                           bg=BG2, fg=FG_DIM, width=7, anchor="e")
            lbl.pack(side="right")
            self._cont_labels[size] = lbl

        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=(10, 8))
        btn_row = tk.Frame(pad, bg=BG2)
        btn_row.pack(fill="x")

        tk.Button(
            btn_row, text="▶  Optimize",
            font=("Consolas", 9, "bold"),
            bg=ACCENT, fg=BG, bd=0, padx=10, pady=6, cursor="hand2",
            activebackground="#6cf", activeforeground=BG,
            command=self._optimize,
        ).pack(side="left")

        tk.Button(
            btn_row, text="✕ Clear",
            font=("Consolas", 9),
            bg=BG3, fg=RED, bd=0, padx=8, pady=6, cursor="hand2",
            activebackground=BORDER, activeforeground=RED,
            command=self._clear_containers,
        ).pack(side="left", padx=(8, 0))

        tk.Button(
            btn_row, text="↺ Reset",
            font=("Consolas", 9),
            bg=BG3, fg=YELLOW, bd=0, padx=8, pady=6, cursor="hand2",
            activebackground=BORDER, activeforeground=YELLOW,
            command=self._reset_containers,
        ).pack(side="right")

        tk.Frame(pad, bg=BORDER, height=1).pack(fill="x", pady=(10, 6))
        self._info_var = tk.StringVar(value="Select a ship to begin.")
        tk.Label(pad, textvariable=self._info_var,
                 font=("Consolas", 8), bg=BG2, fg=FG_DIM,
                 wraplength=240, justify="left", anchor="w").pack(fill="x")

    # ── Data ───────────────────────────────────────────────────────────────────

    def _on_data_loaded(self):
        self._data.loaded = True  # thread-safe: now on main thread via after()
        if self._data.error:
            self._status_var.set(f"Error: {self._data.error}")
            return
        names = self._data.get_ship_names()
        self._ship_cb.configure_values(names)
        self._status_var.set(f"Ready — {len(names)} ships  |  sc-cargo.space")

    def _load_ship(self, name: str):
        if not name:
            return
        ship = self._data.find(name)
        if not ship:
            self._status_var.set(f"Ship not found: '{name}'")
            return
        self._current_ship     = ship
        self._ship_cb.set(ship["name"])

        # Check for a layout JSON (from cargo_grid_editor.html) first
        layout_key = ship["name"].lower()
        layout = SHIP_LAYOUTS.get(layout_key)
        if layout:
            self._slots, self._bounds = _layout_to_slots(layout)
            # Override bounds to use the full grid dimensions from the layout
            grid_w = layout.get("gridW", self._bounds[2])
            grid_z = layout.get("gridZ", self._bounds[3])
            self._bounds = (0, 0, grid_w, grid_z)
            self._has_layout = True
        else:
            self._slots, self._bounds = build_slots(ship)
            self._has_layout = False

        self._slot_assignment  = []
        # Auto-optimize on ship load: layout ships use their stored counts,
        # non-layout ships run the optimizer to fill optimally.
        if self._has_layout:
            self._reset_containers()
        else:
            self._optimize()

        cap   = ship.get("capacity") or ship.get("cargo") or ship.get("scu") or 0
        # For layout ships, use the layout's totalCapacity if available
        if layout and not cap:
            cap = layout.get("totalCapacity", 0)
        # Store capacity on the ship dict so _update_fill can use it
        ship["capacity"] = cap
        mfr   = ship.get("manufacturer", ship.get("company_name", ""))
        n_grp = len(ship.get("groups", []))
        max_h = max((s.get("y0", 0) + s["h"] for s in self._slots), default=1)
        self._info_var.set(
            f"{mfr}  {ship['name']}\n"
            f"{cap:,} SCU  ·  {len(self._slots)} grid slot(s)\n"
            f"{n_grp} section(s)  ·  max slot height {max_h} SCU"
        )
        self._status_var.set(f"{ship['name']}  —  {cap:,} SCU")

    def _refresh(self):
        self._status_var.set("Refreshing data…")
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
            except Exception as exc:
                print(f"[Cargo] Failed to remove cache file: {exc}")
        self._ship_cb.configure_state("disabled")
        self._data = ShipDataLoader()
        self._data.load_async(lambda: self.root.after(0, self._on_data_loaded))

    # ── Isometric renderer ─────────────────────────────────────────────────────

    def _render_grid(self):
        # TODO: consider incremental canvas updates instead of full redraw
        self._canvas.delete("all")

        if not self._current_ship or not self._slots:
            cw = max(self._canvas.winfo_width(),  100)
            ch = max(self._canvas.winfo_height(), 100)
            self._canvas.create_text(
                cw // 2, ch // 2,
                text="Select a ship to view its cargo grid",
                font=("Consolas", 11), fill=FG_DIM,
            )
            return

        x_min, z_min, x_max, z_max = self._bounds
        gw    = x_max - x_min          # world width  (X)
        gl    = z_max - z_min          # world depth  (Z)
        # max_h must include each slot's Y offset so stacked grids are fully visible
        max_h = max((s.get("y0", 0) + s["h"] for s in self._slots), default=1)

        cw_px = max(self._canvas.winfo_width(),  400)
        ch_px = max(self._canvas.winfo_height(), 300)
        PAD   = 48

        # ── Auto-fit cell size ────────────────────────────────────────────────
        # Screen extent of the unshifted scene (ox=oy=0):
        #   sx range: [−gl·cell … gw·cell]           width  = (gw+gl)·cell
        #   sy range: [−max_h·cell … (gw+gl)/2·cell] height = (max_h + (gw+gl)/2)·cell
        span_w = gw + gl
        span_h = max_h + (gw + gl) * 0.5

        cell = max(8.0, min(
            (cw_px - PAD * 2) / max(span_w, 1),
            (ch_px - PAD * 2) / max(span_h, 1),
            42.0,
        ))

        # ── Origin offset to centre the scene ────────────────────────────────
        # Left-most screen point  (wx=0,  wy=0, wz=gl) → sx = -gl·cell
        # Right-most screen point (wx=gw, wy=0, wz=0 ) → sx =  gw·cell
        # Top-most screen point   (wx=0,  wy=max_h, wz=0) → sy = -max_h·cell
        # Bottom-most             (wx=gw, wy=0, wz=gl ) → sy = (gw+gl)/2·cell
        scene_left   = -gl   * cell
        scene_right  =  gw   * cell
        scene_top    = -max_h * cell
        scene_bottom = (gw + gl) * 0.5 * cell

        scene_pw = scene_right  - scene_left
        scene_ph = scene_bottom - scene_top

        ox = (cw_px - scene_pw) / 2.0 - scene_left
        oy = (ch_px - scene_ph) / 2.0 - scene_top + PAD * 0.25

        def pt(wx, wy, wz):
            return iso_pt(wx, wy, wz, cell, ox, oy)

        # ── Draw ground footprints ────────────────────────────────────────────
        if getattr(self, "_has_layout", False) and self._current_ship:
            # Layout mode: draw the full grid floor from gridW × gridZ
            layout_key = self._current_ship["name"].lower()
            layout = SHIP_LAYOUTS.get(layout_key, {})
            floor_w = layout.get("gridW", gw)
            floor_l = layout.get("gridZ", gl)
            # Draw single floor slab
            corners = [pt(0, 0, 0), pt(floor_w, 0, 0),
                       pt(floor_w, 0, floor_l), pt(0, 0, floor_l)]
            flat = [c for p in corners for c in p]
            self._canvas.create_polygon(*flat,
                                        fill=SLOT_FILL, outline=SLOT_OUTLINE,
                                        width=1)
            # 1×1 sub-grid lines
            if cell >= 6:
                for lx in range(floor_w + 1):
                    p1, p2 = pt(lx, 0, 0), pt(lx, 0, floor_l)
                    self._canvas.create_line(
                        p1[0], p1[1], p2[0], p2[1],
                        fill=GRID_LINE, width=1)
                for lz in range(floor_l + 1):
                    p1, p2 = pt(0, 0, lz), pt(floor_w, 0, lz)
                    self._canvas.create_line(
                        p1[0], p1[1], p2[0], p2[1],
                        fill=GRID_LINE, width=1)
        else:
            for slot in self._slots:
                x0    = slot["x"] - x_min
                yf    = slot.get("y0", 0)
                z0    = slot["z"] - z_min
                w     = slot["w"]
                l     = slot["l"]

                corners = [pt(x0, yf, z0), pt(x0+w, yf, z0),
                           pt(x0+w, yf, z0+l), pt(x0, yf, z0+l)]
                flat = [c for p in corners for c in p]
                self._canvas.create_polygon(*flat,
                                            fill=SLOT_FILL, outline=SLOT_OUTLINE,
                                            width=1)

                if cell >= 9:
                    for lx in range(w + 1):
                        p1, p2 = pt(x0+lx, yf, z0), pt(x0+lx, yf, z0+l)
                        self._canvas.create_line(
                            p1[0], p1[1], p2[0], p2[1],
                            fill=GRID_LINE, width=1)
                    for lz in range(l + 1):
                        p1, p2 = pt(x0, yf, z0+lz), pt(x0+w, yf, z0+lz)
                        self._canvas.create_line(
                            p1[0], p1[1], p2[0], p2[1],
                            fill=GRID_LINE, width=1)

        # ── Collect all 3-D box placements ────────────────────────────────────
        all_boxes: list[tuple] = []
        if getattr(self, "_has_layout", False):
            # Layout mode: each slot IS a placed container at its exact position.
            # - If assignment matches original size ({32:1} in a 32 SCU slot),
            #   draw using the slot's exact dims (preserves layout geometry).
            # - If assignment is different (substituted or multi-container),
            #   use place_containers_3d to pack them inside the slot volume.
            for i, slot in enumerate(self._slots):
                asgn = self._slot_assignment[i] if i < len(self._slot_assignment) else {}
                if not asgn:
                    continue
                bx = slot["x"] - x_min
                by = slot.get("y0", 0)
                bz = slot["z"] - z_min
                original_sz = slot.get("placed_size", 0)

                # Check if this is the original single container
                is_original = (len(asgn) == 1
                               and original_sz in asgn
                               and asgn[original_sz] == 1)

                if is_original:
                    # Draw at exact layout position with exact dims
                    all_boxes.append((bx, by, bz,
                                      slot["w"], slot["h"], slot["l"],
                                      original_sz))
                else:
                    # Substituted or multi-container: pack inside slot volume
                    for (lx, ly, lz, dw, dh, dl, size) in place_containers_3d(slot, asgn):
                        all_boxes.append((bx + lx, by + ly, bz + lz,
                                          dw, dh, dl, size))
        else:
            for i, slot in enumerate(self._slots):
                asgn = self._slot_assignment[i] if i < len(self._slot_assignment) else {}
                if not asgn:
                    continue
                x0 = slot["x"]  - x_min
                y0 = slot.get("y0", 0)
                z0 = slot["z"]  - z_min
                for (lx, ly, lz, dw, dh, dl, size) in place_containers_3d(slot, asgn):
                    all_boxes.append((x0 + lx, y0 + ly, z0 + lz, dw, dh, dl, size))

        # ── Painter's algorithm: back → front ─────────────────────────────────
        # Isometric camera at +X +Y +Z corner.  Screen‐Y increases with
        # (wx + wz) and decreases with wy.  To avoid see‐through artefacts
        # we must draw the furthest‐from‐camera box first.
        #
        # Primary key:  lower (x + z) drawn first  (they are behind)
        # Secondary:    lower y drawn first         (below = behind)
        # Tertiary:     lower x drawn first          (disambiguate rows)
        all_boxes.sort(key=lambda b: (
            b[0] + b[2],           # x + z  (iso depth)
            b[1],                  # y      (height layer)
            b[0],                  # x      (left-right tiebreak)
        ))

        # ── Draw each box with 3 faces ─────────────────────────────────────────
        for (wx, wy, wz, dw, dh, dl, size) in all_boxes:
            base    = CONT_COL.get(size, "#888888")
            c_top   = base                     # brightest
            c_right = _shade(base, 0.72)       # medium
            c_lft   = _shade(base, 0.50)       # darkest  (z = wz+dl face)
            edge    = _shade(base, 0.32)

            # ── Left face  (z = wz + dl, faces +Z = left side of iso box) ────
            pts_l = [
                pt(wx,    wy,    wz+dl),
                pt(wx+dw, wy,    wz+dl),
                pt(wx+dw, wy+dh, wz+dl),
                pt(wx,    wy+dh, wz+dl),
            ]
            # ── Right face (x = wx + dw, faces +X = right side of iso box) ───
            pts_r = [
                pt(wx+dw, wy,    wz),
                pt(wx+dw, wy,    wz+dl),
                pt(wx+dw, wy+dh, wz+dl),
                pt(wx+dw, wy+dh, wz),
            ]
            # ── Top face   (y = wy + dh) ──────────────────────────────────────
            pts_t = [
                pt(wx,    wy+dh, wz),
                pt(wx+dw, wy+dh, wz),
                pt(wx+dw, wy+dh, wz+dl),
                pt(wx,    wy+dh, wz+dl),
            ]

            for pts, color in (
                (pts_l, c_lft),
                (pts_r, c_right),
                (pts_t, c_top),
            ):
                flat = [c for p in pts for c in p]
                self._canvas.create_polygon(*flat,
                                            fill=color, outline=edge, width=1)

            # ── Size label on the left (z-max) face ───────────────────────────
            face_px_h = abs(pts_l[2][1] - pts_l[0][1])   # ≈ dh * cell
            face_px_w = abs(pts_l[1][0] - pts_l[0][0])   # ≈ dl * cell
            if face_px_h >= 14 and face_px_w >= 10:
                cx = (pts_l[0][0] + pts_l[1][0] + pts_l[2][0] + pts_l[3][0]) // 4
                cy = (pts_l[0][1] + pts_l[1][1] + pts_l[2][1] + pts_l[3][1]) // 4
                fs = max(6, min(int(face_px_h * 0.38), int(face_px_w * 0.28), 14))
                self._canvas.create_text(
                    cx, cy, text=str(size),
                    font=("Consolas", fs, "bold"),
                    fill=_label_color(c_lft),
                )

        # ── Info bar ──────────────────────────────────────────────────────────
        tot_scu = sum(s["capacity"] for s in self._slots)
        self._grid_info_var.set(
            f"footprint {gw}×{gl}  ·  {len(self._slots)} slots"
            f"  ·  max H:{max_h}  ·  {tot_scu:,} SCU"
        )

        # Keep scrollregion slightly larger than canvas so bars show up
        sr_w = max(cw_px, int(scene_pw) + PAD * 2)
        sr_h = max(ch_px, int(scene_ph) + PAD * 2)
        self._canvas.configure(scrollregion=(0, 0, sr_w, sr_h))

    # ── Container calc ─────────────────────────────────────────────────────────

    @staticmethod
    def _safe_int(var):
        try:
            return int(var.get())
        except (tk.TclError, ValueError):
            return 0

    def _update_fill(self):
        cap  = self._current_ship.get("capacity", 0) if self._current_ship else 0
        used = sum(self._safe_int(self._counts[s]) * s for s in CONTAINER_SIZES)
        pct  = min(used / cap, 1.0) if cap > 0 else 0.0

        color = RED if used > cap else GREEN
        self._cap_var.set(f"{used:,} / {cap:,} SCU")
        self._cap_lbl.configure(fg=color)

        bw = self._bar_canvas.winfo_width()
        self._bar_canvas.coords(self._bar_rect, 0, 0, bw * pct, 10)
        self._bar_canvas.itemconfig(self._bar_rect, fill=color)

        for size in CONTAINER_SIZES:
            n = self._safe_int(self._counts[size])
            self._cont_labels[size].configure(text=f"= {n * size:>5,}")

        self._update_assignment()
        self._render_grid()

    def _update_assignment(self):
        if getattr(self, "_has_layout", False):
            # Layout-imported ship: each slot IS a placed container.
            # First pass: fill slots with their original size if count allows.
            # Second pass: freed slots can be filled by smaller containers.
            counts = {s: self._counts[s].get() for s in CONTAINER_SIZES}
            remaining = dict(counts)  # how many of each size still need placement
            used_original = {s: 0 for s in CONTAINER_SIZES}
            self._slot_assignment = [{} for _ in self._slots]

            # Pass 1: assign original-size containers to their native slots
            for i, slot in enumerate(self._slots):
                sz = slot.get("placed_size", 0)
                if sz and sz > 0 and sz in remaining and remaining[sz] > 0:
                    self._slot_assignment[i] = {sz: 1}
                    remaining[sz] -= 1

            # Pass 2: for empty slots, greedily pack remaining containers
            # A freed 32 SCU slot can hold multiple smaller containers
            # (e.g. 4×8 SCU, or 2×16 SCU, or 32×1 SCU)
            for i, slot in enumerate(self._slots):
                if self._slot_assignment[i]:
                    continue  # already filled
                slot_vol = slot.get("placed_size", 0)
                if slot_vol <= 0:
                    continue
                # Greedily fill this slot with remaining containers (largest first)
                fill = {}
                vol_left = slot_vol
                for sz in sorted(remaining.keys(), reverse=True):
                    if sz > vol_left or remaining[sz] <= 0:
                        continue
                    n = min(remaining[sz], vol_left // sz)
                    if n > 0:
                        fill[sz] = n
                        remaining[sz] -= n
                        vol_left -= n * sz
                    if vol_left <= 0:
                        break
                if fill:
                    self._slot_assignment[i] = fill
        else:
            counts = {s: self._counts[s].get() for s in CONTAINER_SIZES}
            self._slot_assignment = assign_slots_from_counts(self._slots, counts)

    def _optimize(self):
        if not self._current_ship or not self._slots:
            return
        ship_name = self._current_ship.get("name", "")
        ref = _find_reference_loadout(ship_name)
        result = ref if ref is not None else greedy_optimize_3d(self._slots)
        # Reset all counts first so sizes absent from result show 0
        for v in self._counts.values():
            v.set(0)
        for size, count in result.items():
            if size in self._counts:
                self._counts[size].set(count)
        self._update_fill()

    def _reset_containers(self):
        for v in self._counts.values():
            v.set(0)
        # If a layout is loaded, auto-fill with its container counts
        if getattr(self, "_has_layout", False) and self._current_ship:
            layout_key = self._current_ship["name"].lower()
            layout = SHIP_LAYOUTS.get(layout_key)
            if layout:
                containers = layout.get("containers", {})
                for size_str, count in containers.items():
                    sz = int(size_str)
                    if sz in self._counts:
                        self._counts[sz].set(int(count))
        self._slot_assignment = []
        self._update_fill()

    def _clear_containers(self):
        """Set all container counts to zero and clear the grid."""
        for v in self._counts.values():
            v.set(0)
        self._slot_assignment = []
        self._update_fill()

    # ── Command polling ────────────────────────────────────────────────────────

    def _poll_commands(self):
        if self._cmd_file:
            try:
                commands = ipc_read_and_clear(self._cmd_file)
                for cmd in commands:
                    try:
                        self._dispatch(cmd)
                    except Exception as exc:
                        log.warning("Failed to dispatch command: %s", exc)
            except Exception as exc:
                log.warning("Command poll error: %s", exc)
        self.root.after(200, self._poll_commands)

    def _dispatch(self, cmd: dict):
        t = cmd.get("type", "")
        if   t == "show":
            self.root.deiconify(); self.root.lift()
        elif t == "hide":
            self.root.withdraw()
        elif t == "set_ship":
            name = cmd.get("ship", "") or cmd.get("name", "")
            if name and self._data.loaded:
                self._load_ship(name)
                self.root.deiconify(); self.root.lift()
        elif t == "optimize":
            self._optimize()
        elif t == "reset":
            self._reset_containers()
        elif t == "set_container":
            try:
                size  = int(cmd.get("size",  0))
                count = int(cmd.get("count", 0))
            except (ValueError, TypeError):
                return
            if size in self._counts:
                self._counts[size].set(count)
                self._update_fill()
        elif t == "import_layout":
            # Accepts a cargo_layout.json exported by cargo_grid_editor.html.
            # Payload: {"type": "import_layout", "containers": {"1":0,"2":0,...}}
            containers = cmd.get("containers", {})
            for size, count in containers.items():
                try:
                    s = int(size)
                    if s in self._counts:
                        self._counts[s].set(int(count))
                except (ValueError, TypeError):
                    pass
            self._update_fill()
        elif t == "refresh":
            self._refresh()
        elif t == "quit":
            self.root.destroy()


if __name__ == "__main__":
    set_dpi_awareness()
    CargoApp()
