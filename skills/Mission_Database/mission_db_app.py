# Mission Database GUI — visual clone of scmdb.net
"""
Star Citizen Mission Database — standalone Tkinter GUI.
Data from scmdb.net static JSON endpoints.
Launched as a subprocess by the WingmanAI Mission_Database skill (main.py).

Usage:
    python mission_db_app.py <x> <y> <w> <h> <opacity> <cmd_file>
"""

import json
import logging
import math
import os
import re
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Optional

# Allow importing from the shared/ directory two levels up
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

import requests
from collections import defaultdict
from shared.ipc import ipc_read_incremental

log = logging.getLogger(__name__)

# ── API ───────────────────────────────────────────────────────────────────────
SCMDB_BASE  = "https://scmdb.net"
API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Accept":     "application/json, text/plain, */*",
}
CACHE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scmdb_cache.json")
CACHE_TTL     = 2 * 3600
CACHE_VERSION = 1

# ── Palette (scmdb.net dark theme) ────────────────────────────────────────────
BG          = "#0b0e14"
BG2         = "#111620"
BG3         = "#161c28"
BG4         = "#1c2233"
BORDER      = "#1e2738"
FG          = "#c8d4e8"
FG_DIM      = "#5a6480"
FG_DIMMER   = "#3a4460"
ACCENT      = "#33ccaa"
YELLOW      = "#ffaa22"
GREEN       = "#33dd88"
RED         = "#ff5533"
ORANGE      = "#ff7733"
PURPLE      = "#aa66ff"
CYAN        = "#44ccdd"
HEADER_BG   = "#0a0d12"
CARD_BG     = "#111822"
CARD_HOVER  = "#182030"
CARD_BORDER = "#1a2030"

# Tag color map
TAG_COLORS = {
    # Mission types
    "Delivery":       ("#1a3322", "#33cc88"),
    "Combat":         ("#331a1a", "#ff5533"),
    "Salvage":        ("#332a1a", "#ffaa22"),
    "Investigation":  ("#221133", "#aa66ff"),
    "Bounty Hunt":    ("#331a1a", "#ff5533"),
    "Rescue":         ("#1a2233", "#44aaff"),
    "Escort":         ("#1a2233", "#44aaff"),
    "Mercenary":      ("#331a1a", "#ff5533"),
    "Mining":         ("#332a1a", "#ffaa22"),
    "Racing":         ("#1a3322", "#33cc88"),
    # Categories
    "career":         ("#1a2233", "#44aaff"),
    "story":          ("#222228", "#888899"),
    # Special
    "LEGAL":          ("#1a3322", "#33dd88"),
    "ILLEGAL":        ("#331a1a", "#ff5533"),
    "CHAIN":          ("#332a11", "#ffaa22"),
    "ONCE":           ("#332a11", "#ffaa22"),
    # Systems
    "Stanton":        ("#0a2218", "#33cc88"),
    "Pyro":           ("#331a0a", "#ff7733"),
    "Nyx":            ("#1a1a33", "#7777cc"),
    "Multi":          ("#222228", "#888899"),
}

def _tag_colors(text: str):
    """Return (bg, fg) for a tag label."""
    if text in TAG_COLORS:
        return TAG_COLORS[text]
    # Default
    return ("#1a2030", FG_DIM)

def _faction_initials(name: str) -> str:
    """Extract 2-letter initials from faction name."""
    words = name.split()
    if len(words) >= 2:
        return (words[0][0] + words[1][0]).upper()
    return name[:2].upper() if name else "??"

def _strip_html(text: str) -> str:
    """Remove HTML tags and convert erkul-style tags to readable text."""
    if not text:
        return ""
    text = re.sub(r"<EM4>", "", text)
    text = re.sub(r"</EM4>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\\n", "\n")
    return text.strip()

def _fmt_uec(val) -> str:
    """Format aUEC value with comma separators."""
    if val is None:
        return "—"
    try:
        return f"{int(val):,} aUEC"
    except (ValueError, TypeError):
        return str(val)

def _fmt_time(minutes) -> str:
    """Format minutes into readable time."""
    if not minutes:
        return "—"
    if minutes < 60:
        return f"{minutes}m"
    h = minutes // 60
    m = minutes % 60
    return f"{h}h {m}m" if m else f"{h}h"


# ══════════════════════════════════════════════════════════════════════════════
# Virtual Scroll Grid — renders only visible cards, pools widget frames
# ══════════════════════════════════════════════════════════════════════════════

class _CardSlot:
    """Pre-built card with fixed label widgets — content updated via .configure(), never destroyed."""
    __slots__ = ("frame", "badge", "title", "faction", "tags_frame",
                 "tag_labels", "sep", "rew_label", "rew_value",
                 "extra_line", "bound_idx", "_hovered")

    MAX_TAGS = 5

    def __init__(self, canvas, wheel_fn):
        self.bound_idx = -1

        self.frame = tk.Frame(canvas, bg=CARD_BG, cursor="hand2",
                              highlightbackground=CARD_BORDER, highlightthickness=1)
        inner = tk.Frame(self.frame, bg=CARD_BG, padx=10, pady=8)
        inner.pack(fill="both", expand=True)

        # Row 1: badge + title column
        row1 = tk.Frame(inner, bg=CARD_BG)
        row1.pack(fill="x")

        self.badge = tk.Label(row1, text="??", font=("Consolas", 8, "bold"),
                              bg="#1a2538", fg=ACCENT, width=3, padx=2, pady=2)
        self.badge.pack(side="left", padx=(0, 6))

        tcol = tk.Frame(row1, bg=CARD_BG)
        tcol.pack(side="left", fill="x", expand=True)

        self.title = tk.Label(tcol, text="", font=("Consolas", 9, "bold"),
                              bg=CARD_BG, fg=FG, anchor="w")
        self.title.pack(fill="x")

        self.faction = tk.Label(tcol, text="", font=("Consolas", 8),
                                bg=CARD_BG, fg=FG_DIM, anchor="w")
        self.faction.pack(fill="x")

        # Row 2: tags
        self.tags_frame = tk.Frame(inner, bg=CARD_BG)
        self.tags_frame.pack(fill="x", pady=(4, 0))
        self.tag_labels = []
        for _ in range(self.MAX_TAGS):
            lbl = tk.Label(self.tags_frame, text="", font=("Consolas", 7),
                           bg=CARD_BG, fg=FG_DIM, padx=2)
            # NOT packed — shown dynamically
            self.tag_labels.append(lbl)

        # Separator
        self.sep = tk.Frame(inner, bg=BORDER, height=1)
        self.sep.pack(fill="x", pady=(6, 4))

        # Row 3: reward
        row3 = tk.Frame(inner, bg=CARD_BG)
        row3.pack(fill="x")
        self.rew_label = tk.Label(row3, text="REWARD", font=("Consolas", 7),
                                  bg=CARD_BG, fg=FG_DIM, anchor="w")
        self.rew_label.pack(side="left")
        self.rew_value = tk.Label(row3, text="—", font=("Consolas", 9, "bold"),
                                  bg=CARD_BG, fg=YELLOW, anchor="e")
        self.rew_value.pack(side="right")

        # Optional extra line (blueprint/chain indicator)
        self.extra_line = tk.Label(inner, text="", font=("Consolas", 7),
                                   bg=CARD_BG, fg=FG_DIMMER, anchor="w")
        # NOT packed by default

        # Bind mousewheel to all widgets
        for w in (self.frame, inner, row1, tcol, self.tags_frame, row3,
                  self.badge, self.title, self.faction,
                  self.rew_label, self.rew_value, self.extra_line):
            w.bind("<MouseWheel>", wheel_fn)
        for tl in self.tag_labels:
            tl.bind("<MouseWheel>", wheel_fn)

    def update(self, title, initials, fname, tags, reward_text, reward_color, extra=""):
        """Update all labels — zero widget creation."""
        self.badge.configure(text=initials)
        self.title.configure(text=title)
        self.faction.configure(text=fname)

        # Update tag labels (show/hide as needed)
        for i, lbl in enumerate(self.tag_labels):
            if i < len(tags):
                text, bg_c, fg_c, bold = tags[i]
                font = ("Consolas", 7, "bold") if bold else ("Consolas", 7)
                lbl.configure(text=f" {text} ", bg=bg_c, fg=fg_c, font=font)
                lbl.pack(side="left", padx=(0, 3))
            else:
                lbl.pack_forget()

        # Reward
        self.rew_value.configure(text=reward_text, fg=reward_color)

        # Extra line
        if extra:
            self.extra_line.configure(text=extra)
            self.extra_line.pack(fill="x")
        else:
            self.extra_line.pack_forget()

    def bind_click(self, fn):
        """Bind click to all interactive surfaces."""
        for w in (self.frame, self.badge, self.title, self.faction,
                  self.tags_frame, self.rew_label, self.rew_value, self.extra_line):
            w.bind("<Button-1>", fn)
        for tl in self.tag_labels:
            tl.bind("<Button-1>", fn)

    def bind_hover(self):
        """Bind hover highlight to ALL widgets in the card."""
        HOVER_BG = "#1a2535"
        self._hovered = False

        def _set_bg(widget, bg):
            try:
                widget.configure(bg=bg)
            except Exception:
                pass
            for child in widget.winfo_children():
                _set_bg(child, bg)

        def _pointer_in_frame():
            """Check if the mouse pointer is inside the card frame."""
            try:
                fx = self.frame.winfo_rootx()
                fy = self.frame.winfo_rooty()
                fw = self.frame.winfo_width()
                fh = self.frame.winfo_height()
                mx = self.frame.winfo_pointerx()
                my = self.frame.winfo_pointery()
                return fx <= mx < fx + fw and fy <= my < fy + fh
            except Exception:
                return False

        def _enter(e):
            if not self._hovered:
                self._hovered = True
                self.frame.configure(highlightbackground=ACCENT)
                _set_bg(self.frame, HOVER_BG)

        def _leave(e):
            if self._hovered and not _pointer_in_frame():
                self._hovered = False
                self.frame.configure(highlightbackground=CARD_BORDER)
                _set_bg(self.frame, CARD_BG)

        def _bind_all(widget):
            widget.bind("<Enter>", _enter)
            widget.bind("<Leave>", _leave)
            for child in widget.winfo_children():
                _bind_all(child)

        _bind_all(self.frame)


class _FabCardSlot:
    """Pre-built fabricator card — content updated via .configure()."""
    __slots__ = ("frame", "name_lbl", "type_lbl", "sub_lbl", "res_lbl",
                 "time_lbl", "tag_frame", "bound_idx", "_hovered")

    def __init__(self, canvas, wheel_fn):
        self.bound_idx = -1
        self.frame = tk.Frame(canvas, bg=CARD_BG, cursor="hand2",
                              highlightbackground=CARD_BORDER, highlightthickness=1)
        inner = tk.Frame(self.frame, bg=CARD_BG, padx=8, pady=6)
        inner.pack(fill="both", expand=True)

        self.name_lbl = tk.Label(inner, text="", font=("Consolas", 9, "bold"),
                                 bg=CARD_BG, fg=FG, anchor="w", wraplength=260)
        self.name_lbl.pack(fill="x")

        self.tag_frame = tk.Frame(inner, bg=CARD_BG)
        self.tag_frame.pack(fill="x", pady=(2, 0))
        self.type_lbl = tk.Label(self.tag_frame, text="", font=("Consolas", 7, "bold"),
                                 padx=4, pady=0)
        self.type_lbl.pack(side="left")
        self.sub_lbl = tk.Label(self.tag_frame, text="", font=("Consolas", 7),
                                bg=CARD_BG, fg=FG_DIM, padx=4)
        self.sub_lbl.pack(side="left")

        self.res_lbl = tk.Label(inner, text="", font=("Consolas", 7),
                                bg=CARD_BG, fg=FG_DIM, anchor="w")
        self.res_lbl.pack(fill="x", pady=(3, 0))

        self.time_lbl = tk.Label(inner, text="", font=("Consolas", 7),
                                 bg=CARD_BG, fg=FG_DIMMER, anchor="w")
        self.time_lbl.pack(fill="x")

        for w in (self.frame, inner, self.name_lbl, self.tag_frame,
                  self.type_lbl, self.sub_lbl, self.res_lbl, self.time_lbl):
            w.bind("<MouseWheel>", wheel_fn)

    def update(self, name, type_text, type_bg, type_fg, sub_text, res_text, time_text):
        self.name_lbl.configure(text=name)
        self.type_lbl.configure(text=type_text, bg=type_bg, fg=type_fg)
        self.sub_lbl.configure(text=sub_text)
        self.res_lbl.configure(text=res_text)
        self.time_lbl.configure(text=time_text)

    def bind_click(self, fn):
        for w in (self.frame, self.name_lbl, self.tag_frame,
                  self.type_lbl, self.sub_lbl, self.res_lbl, self.time_lbl):
            w.bind("<Button-1>", fn)

    def bind_hover(self):
        FAB_HOVER_BG = "#1a2535"
        FAB_NORMAL_BG = CARD_BG
        self._hovered = False

        def _set_bg(widget, bg):
            try:
                widget.configure(bg=bg)
            except Exception:
                pass
            for child in widget.winfo_children():
                _set_bg(child, bg)

        def _pointer_in_frame():
            try:
                fx = self.frame.winfo_rootx()
                fy = self.frame.winfo_rooty()
                fw = self.frame.winfo_width()
                fh = self.frame.winfo_height()
                mx = self.frame.winfo_pointerx()
                my = self.frame.winfo_pointery()
                return fx <= mx < fx + fw and fy <= my < fy + fh
            except Exception:
                return False

        def _enter(e):
            if not self._hovered:
                self._hovered = True
                self.frame.configure(highlightbackground=ACCENT)
                _set_bg(self.frame, FAB_HOVER_BG)

        def _leave(e):
            if self._hovered and not _pointer_in_frame():
                self._hovered = False
                self.frame.configure(highlightbackground=CARD_BORDER)
                _set_bg(self.frame, FAB_NORMAL_BG)

        def _bind_all(widget):
            widget.bind("<Enter>", _enter)
            widget.bind("<Leave>", _leave)
            for child in widget.winfo_children():
                _bind_all(child)

        _bind_all(self.frame)


class VirtualScrollGrid(tk.Frame):
    """
    Virtualized scrollable card grid — zero-flicker scrolling.

    Cards are pre-built with fixed widget structure (_CardSlot / _FabCardSlot).
    On scroll, only .configure(text=...) calls update content — no widget
    creation or destruction happens during scrolling.
    """

    BUFFER_ROWS = 8  # generous buffer — cards ready long before visible

    # Offscreen parking coordinate (moved here instead of hidden/shown)
    _PARK_Y = -9999

    def __init__(self, parent, card_width=320, row_height=130,
                 fill_fn=None, on_click_fn=None, slot_class=None, bg=BG):
        super().__init__(parent, bg=bg)

        self._card_width = card_width
        self._row_height = row_height
        self._fill_fn = fill_fn
        self._click_fn = on_click_fn
        self._slot_class = slot_class
        self._bg = bg

        self._items: list = []
        self._num_cols = 1
        self._num_rows = 0
        self._prev_first = -1
        self._prev_last = -1

        self._slots: dict = {}       # sid -> slot instance
        self._active: dict = {}      # (row, col) -> sid
        self._free: list = []        # sids parked offscreen
        self._slot_pos: dict = {}    # sid -> (row, col) currently placed at

        self._vbar = ttk.Scrollbar(self, orient="vertical")
        self._vbar.pack(side="right", fill="y")
        self._canvas = tk.Canvas(self, bg=bg, highlightthickness=0,
                                 yscrollcommand=self._vbar.set)
        self._canvas.pack(fill="both", expand=True)
        self._vbar.configure(command=self._on_scrollbar)

        self._canvas.bind("<Configure>", self._on_resize)
        self._canvas.bind("<MouseWheel>", self._on_mousewheel)
        self._viewport_h = 400

        # Pre-allocate a pool of card slots on first data load
        self._pool_prebuilt = False

    def set_data(self, items: list):
        self._items = items or []
        self._release_all()
        self._recalc()
        self._canvas.yview_moveto(0)
        # Pre-build pool for visible area + buffer to avoid creation during scroll
        if not self._pool_prebuilt and self._items:
            self._prebuild_pool()
            self._pool_prebuilt = True
        self._prev_first = -1
        self._render()

    def _prebuild_pool(self):
        """Create enough card slots to cover viewport + both buffers."""
        visible_rows = max(1, self._viewport_h // self._row_height) + 1
        total_slots_needed = (visible_rows + 2 * self.BUFFER_ROWS) * self._num_cols
        existing = len(self._slots)
        for _ in range(max(0, total_slots_needed - existing)):
            sid = self._create_slot()
            self._free.append(sid)

    def _recalc(self):
        w = self._canvas.winfo_width() or 800
        self._viewport_h = self._canvas.winfo_height() or 400
        self._num_cols = max(1, w // self._card_width)
        n = len(self._items)
        self._num_rows = (n + self._num_cols - 1) // self._num_cols if n else 0
        total_h = max(1, self._num_rows * self._row_height + 20)
        self._canvas.configure(scrollregion=(0, 0, w, total_h))

    def _on_resize(self, event):
        old = self._num_cols
        self._viewport_h = event.height
        self._num_cols = max(1, event.width // self._card_width)
        if self._num_cols != old:
            self._release_all()
            self._pool_prebuilt = False
        self._recalc()
        if not self._pool_prebuilt and self._items:
            self._prebuild_pool()
            self._pool_prebuilt = True
        self._prev_first = -1
        self._render()

    def _on_scrollbar(self, *args):
        self._canvas.yview(*args)
        self._render()

    def _on_mousewheel(self, event):
        self._canvas.yview_scroll(-1 * (event.delta // 120), "units")
        self._render()

    def _render(self):
        """Core render — positions cards, fills new ones, parks old ones.

        Key anti-flicker strategy:
        1. FIRST fill and position all newly needed cards (they appear instantly)
        2. THEN park cards that scrolled out of range (user can't see them leave)
        This order means the user never sees an empty gap.
        """
        if not self._items or self._num_rows == 0:
            self._release_all()
            return

        top_frac = self._canvas.yview()[0]
        total_h = self._num_rows * self._row_height + 20
        y_top = top_frac * total_h
        y_bot = y_top + self._viewport_h
        buf = self.BUFFER_ROWS

        first = max(0, int(y_top // self._row_height) - buf)
        last = min(self._num_rows - 1, int(y_bot // self._row_height) + buf)

        if first == self._prev_first and last == self._prev_last:
            return
        self._prev_first = first
        self._prev_last = last

        needed = set()
        for r in range(first, last + 1):
            for c in range(self._num_cols):
                if r * self._num_cols + c < len(self._items):
                    needed.add((r, c))

        canvas_w = self._canvas.winfo_width() or 800
        col_w = canvas_w // self._num_cols

        # Identify which active cards are no longer needed
        # BUT DON'T park them yet — collect them first
        to_park = []
        for pos in list(self._active):
            if pos not in needed:
                to_park.append(pos)

        # Move to-park slots to free list (available for reuse below)
        # but don't move them offscreen yet
        park_sids = []
        for pos in to_park:
            sid = self._active.pop(pos)
            park_sids.append(sid)
            self._free.append(sid)

        # STEP 1: Place all needed cards (reposition existing + fill new)
        for pos in needed:
            r, c = pos
            idx = r * self._num_cols + c
            x = c * col_w + 4
            y = r * self._row_height + 4

            if pos in self._active:
                # Already active — just reposition
                sid = self._active[pos]
                self._canvas.coords(sid, x, y)
                self._canvas.itemconfigure(sid, width=col_w - 8)
                continue

            # Need a new slot for this position
            sid = self._acquire()
            self._active[pos] = sid
            slot = self._slots[sid]

            # Fill content if item changed
            if slot.bound_idx != idx:
                item = self._items[idx]
                if self._fill_fn:
                    self._fill_fn(slot, item, idx)
                slot.bound_idx = idx

                def _click(e, _item=item, _idx=idx):
                    if self._click_fn:
                        self._click_fn(_item, _idx)
                slot.bind_click(_click)
                slot.bind_hover()

            # Position and ensure visible
            self._canvas.coords(sid, x, y)
            self._canvas.itemconfigure(sid, width=col_w - 8, state="normal")

        # STEP 2: Now park the old cards offscreen (user already sees new ones)
        for sid in park_sids:
            if sid not in self._active.values():
                # Only park if it wasn't reused in step 1
                self._canvas.coords(sid, 0, self._PARK_Y)

    def _acquire(self):
        """Get a free slot. Pops from free list or creates new."""
        if self._free:
            sid = self._free.pop()
            return sid
        return self._create_slot()

    def _create_slot(self):
        """Create a new card slot, parked offscreen."""
        slot_cls = self._slot_class or _CardSlot
        slot = slot_cls(self._canvas, self._on_mousewheel)
        sid = self._canvas.create_window(
            0, self._PARK_Y, window=slot.frame, anchor="nw",
            state="normal", width=self._card_width - 8)
        self._slots[sid] = slot
        return sid

    def _release_all(self):
        """Park all active cards offscreen."""
        for pos, sid in self._active.items():
            self._canvas.coords(sid, 0, self._PARK_Y)
            self._free.append(sid)
        self._active.clear()
        self._prev_first = -1
        self._prev_last = -1
        for sid, slot in self._slots.items():
            slot.bound_idx = -1

    def destroy_pool(self):
        for sid, slot in self._slots.items():
            slot.frame.destroy()
            self._canvas.delete(sid)
        self._slots.clear()
        self._active.clear()
        self._free.clear()


# ══════════════════════════════════════════════════════════════════════════════
# Data Manager
# ══════════════════════════════════════════════════════════════════════════════

class MissionDataManager:
    """Fetches and caches mission data from scmdb.net."""

    HIDDEN_LOCATIONS = frozenset({
        "Akiro Cluster", "Pyro Belt (Cool 1)", "Pyro Belt (Cool 2)",
        "Pyro Belt (Warm 1)", "Pyro Belt (Warm 2)", "Lagrange G",
        "Lagrange (Occupied)", "Asteroid Cluster (Low Yield)",
        "Asteroid Cluster (Medium Yield)", "Ship Graveyard", "Space Derelict",
    })

    def __init__(self):
        self.loaded = False
        self.loading = False
        self.error: Optional[str] = None
        self._lock = threading.Lock()

        self.version = ""
        self.contracts: list = []
        self.legacy_contracts: list = []
        self.factions: dict = {}
        self.location_pools: dict = {}
        self.ship_pools: dict = {}
        self.blueprint_pools: dict = {}
        self.scopes: dict = {}
        self.availability_pools: list = []
        self.faction_rewards_pools: list = []
        self.partial_reward_pools: list = []

        # Derived lookups
        self.all_categories: list = []
        self.all_systems: list = []
        self.all_mission_types: list = []
        self.all_faction_names: list = []
        self.faction_by_guid: dict = {}
        self.min_reward = 0
        self.max_reward = 0
        self.available_versions: list = []  # [{version, file}, ...]

        # Crafting / Fabricator data
        self.crafting_blueprints: list = []     # from crafting_blueprints-{ver}.json
        self.crafting_items: list = []          # from crafting_items-{ver}.json
        self.crafting_resources: list = []      # resource names
        self.crafting_gem_items: list = []      # gem/ore item names
        self.crafting_properties: dict = {}     # stat modifier definitions
        self.crafting_dismantle: dict = {}      # dismantle rules
        self.crafting_meta: dict = {}           # totals metadata
        self.crafting_items_map: dict = {}      # entityClass -> item stats
        self.crafting_manufacturers: dict = {}  # code -> {name, guid}
        self.crafting_loaded = False
        self.crafting_loading = False

        # Mining / Resources data
        self.mining_locations: list = []        # from mining_data-{ver}.json
        self.mining_elements: dict = {}         # guid -> element info
        self.mining_compositions: dict = {}     # guid -> composition
        self.mining_clustering: dict = {}       # guid -> clustering preset
        self.mining_equipment_lasers: list = []
        self.mining_equipment_modules: list = []
        self.mining_equipment_gadgets: list = []
        self.mining_loaded = False
        self.mining_loading = False
        # Derived: resource_name -> [{location, system, type, group, min_pct, max_pct}]
        self.resource_to_locations: dict = {}
        # Derived: location_name -> [{resource, min_pct, max_pct, group}]
        self.location_to_resources: dict = {}
        self.all_resource_names: list = []
        self.all_location_types: list = []
        self.all_mining_systems: list = []
        # Resource categories matching scmdb
        self.resource_categories: dict = {}  # {cat_label: [resource_names]}

        # Mining group type definitions (matching scmdb)
        self.MINING_GROUP_TYPES = {
            "SpaceShip_Mineables":       {"label": "Ship Mining",       "short": "Ship",       "icon": "\u26cf", "category": "mining"},
            "SpaceShip_Mineables_Rare":  {"label": "Ship Mining (Rare)","short": "Ship (Rare)","icon": "\u2b50", "category": "mining"},
            "FPS_Mineables":             {"label": "FPS Mining",        "short": "FPS",        "icon": "\u26cf", "category": "mining"},
            "GroundVehicle_Mineables":   {"label": "ROC Mining",        "short": "ROC",        "icon": "\U0001f69c","category": "mining"},
            "Harvestables":              {"label": "Harvesting",        "short": "Harvest",    "icon": "\U0001f33f","category": "mining"},
            "Salvage_FreshDerelicts":    {"label": "Derelict Salvage",  "short": "Wrecks",     "icon": "\U0001f6f8","category": "salvage"},
            "Salvage_BrokenShips_Poor":  {"label": "Debris (Small)",    "short": "S Debris",   "icon": "\u2699",  "category": "salvage"},
            "Salvage_BrokenShips_Normal":{"label": "Debris (Medium)",   "short": "M Debris",   "icon": "\u2699",  "category": "salvage"},
            "Salvage_BrokenShips_Elite": {"label": "Debris (Large)",    "short": "L Debris",   "icon": "\u2699",  "category": "salvage"},
        }

    def load(self, on_done=None):
        with self._lock:
            if self.loading:
                return
            self.loading = True

        def _run():
            try:
                data = self._load_cache()
                if not data:
                    data = self._fetch_fresh()
                    if data:
                        self._save_cache(data)

                if not data:
                    self.error = "Failed to fetch mission data"
                    return

                self._index(data, mark_loaded=True)

            except Exception as exc:
                self.error = str(exc)
                with self._lock:
                    self.loading = False
            finally:
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    def _fetch(self, url: str) -> Optional[dict]:
        try:
            r = requests.get(url, headers=API_HEADERS, timeout=30)
            if r.ok:
                return r.json()
            log.warning("Fetch HTTP %s for %s", r.status_code, url)
        except Exception as e:
            log.warning("Fetch error (%s) for %s: %s", type(e).__name__, url, e)
        return None

    def _fetch_versions(self) -> list:
        """Fetch the versions index from scmdb."""
        return self._fetch(f"{SCMDB_BASE}/data/versions.json") or []

    def _fetch_fresh(self, prefer: str = "live") -> Optional[dict]:
        """Fetch versions.json then the preferred merged data (live or ptu)."""
        versions = self._fetch_versions()
        if not versions:
            return None
        self.available_versions = versions

        # Pick target version
        target = None
        for v in versions:
            ver = v.get("version", "")
            if prefer.lower() in ver.lower():
                target = v
                break
        if not target:
            target = versions[0] if versions else None
        if not target:
            return None

        self.version = target.get("version", "")
        file_name = target.get("file", "")
        if not file_name:
            return None

        data = self._fetch(f"{SCMDB_BASE}/data/{file_name}")
        if data:
            data["_scmdb_version"] = self.version
            data["_versions"] = versions
        return data

    def load_version(self, version_str: str, on_done=None):
        """Load a specific game version (e.g. '4.7.0-ptu...' or '4.6.0-live...')."""
        # Check if we already have it cached
        cache_key = version_str.replace(".", "_").replace("-", "_")
        cache_path = os.path.join(os.path.dirname(CACHE_FILE),
                                  f".scmdb_cache_{cache_key}.json")

        def _run():
            try:
                # Try version-specific cache first
                data = None
                if os.path.isfile(cache_path):
                    try:
                        with open(cache_path, "r", encoding="utf-8") as f:
                            obj = json.load(f)
                        if (obj.get("_cache_version") == CACHE_VERSION and
                                time.time() - obj.get("_ts", 0) < CACHE_TTL):
                            data = obj
                    except Exception:
                        pass

                if not data:
                    # Find the file for this version
                    versions = self.available_versions or self._fetch_versions()
                    target = None
                    for v in versions:
                        if v.get("version") == version_str:
                            target = v
                            break
                    if not target:
                        self.error = f"Version {version_str} not found"
                        return

                    file_name = target.get("file", "")
                    data = self._fetch(f"{SCMDB_BASE}/data/{file_name}")
                    if data:
                        data["_scmdb_version"] = version_str
                        data["_versions"] = versions
                        data["_cache_version"] = CACHE_VERSION
                        data["_ts"] = time.time()
                        try:
                            with open(cache_path, "w", encoding="utf-8") as f:
                                json.dump(data, f)
                        except Exception:
                            pass

                if not data:
                    self.error = f"Failed to fetch {version_str}"
                    return

                self.version = version_str
                self._index(data, mark_loaded=True)

                with self._lock:
                    self.error = None

            except Exception as exc:
                self.error = str(exc)
                with self._lock:
                    self.loading = False
            finally:
                if on_done:
                    on_done()

        with self._lock:
            self.loading = True
            self.loaded = False
        threading.Thread(target=_run, daemon=True).start()

    def _load_cache(self) -> Optional[dict]:
        if not os.path.isfile(CACHE_FILE):
            return None
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                obj = json.load(f)
            if obj.get("_cache_version") != CACHE_VERSION:
                return None
            if time.time() - obj.get("_ts", 0) > CACHE_TTL:
                return None
            self.version = obj.get("_scmdb_version", "")
            self.available_versions = obj.get("_versions", [])
            return obj
        except Exception:
            return None

    def _save_cache(self, data: dict):
        try:
            to_save = {**data, "_cache_version": CACHE_VERSION, "_ts": time.time()}
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(to_save, f)
        except Exception:
            pass

    def _index(self, data: dict, mark_loaded: bool = False):
        """Index all mission data for fast filtering."""
        contracts = data.get("contracts", [])
        if isinstance(contracts, dict):
            contracts = list(contracts.values())
        legacy = data.get("legacyContracts", [])
        if isinstance(legacy, dict):
            legacy = list(legacy.values())
        # Mark legacy contracts so we can distinguish them in the UI
        for c in legacy:
            c["_legacy"] = True
        # Merge both lists — scmdb.net shows both in the same view
        _contracts = contracts + legacy
        _legacy_contracts = legacy

        _factions = data.get("factions", {})
        _location_pools = data.get("locationPools", {})
        _ship_pools = data.get("shipPools", {})
        _blueprint_pools = data.get("blueprintPools", {})
        _scopes = data.get("scopes", {})
        _availability_pools = data.get("availabilityPools", [])
        _faction_rewards_pools = data.get("factionRewardsPools", [])
        _partial_reward_pools = data.get("partialRewardPayoutPools", [])

        # Build faction GUID lookup
        _faction_by_guid = {}
        if isinstance(_factions, dict):
            for guid, f in _factions.items():
                _faction_by_guid[guid] = f

        # Collect unique values for filters
        cats = set()
        systems = set()
        types = set()
        fnames = set()
        rewards = []

        for c in _contracts:
            cat = c.get("category", "")
            if cat:
                cats.add(cat)
            for s in (c.get("systems") or []):
                if s:
                    systems.add(s)
            mt = c.get("missionType", "")
            if mt:
                types.add(mt)
            fg = c.get("factionGuid", "")
            if fg and fg in _faction_by_guid:
                fnames.add(_faction_by_guid[fg].get("name", ""))
            r = c.get("rewardUEC")
            if r is not None and isinstance(r, (int, float)):
                rewards.append(int(r))

        _all_categories = sorted(cats)
        _all_systems = sorted(systems)
        _all_mission_types = sorted(types)
        _all_faction_names = sorted(fnames)
        _min_reward = min(rewards) if rewards else 0
        _max_reward = max(rewards) if rewards else 0

        # Atomically swap all indexed data under lock
        with self._lock:
            self.contracts = _contracts
            self.legacy_contracts = _legacy_contracts
            self.factions = _factions
            self.location_pools = _location_pools
            self.ship_pools = _ship_pools
            self.blueprint_pools = _blueprint_pools
            self.scopes = _scopes
            self.availability_pools = _availability_pools
            self.faction_rewards_pools = _faction_rewards_pools
            self.partial_reward_pools = _partial_reward_pools
            self.faction_by_guid = _faction_by_guid
            self.all_categories = _all_categories
            self.all_systems = _all_systems
            self.all_mission_types = _all_mission_types
            self.all_faction_names = _all_faction_names
            self.min_reward = _min_reward
            self.max_reward = _max_reward
            if mark_loaded:
                self.loaded = True
                self.loading = False

    def get_faction(self, guid: str) -> dict:
        return self.faction_by_guid.get(guid, {})

    def get_location(self, guid: str) -> dict:
        return self.location_pools.get(guid, {})

    def get_availability(self, idx) -> dict:
        try:
            return self.availability_pools[idx]
        except (IndexError, TypeError):
            return {}

    def is_crafting_loaded(self) -> bool:
        with self._lock:
            return self.crafting_loaded

    def is_mining_loaded(self) -> bool:
        with self._lock:
            return self.mining_loaded

    def is_data_loaded(self) -> bool:
        with self._lock:
            return self.loaded

    def is_data_loading(self) -> bool:
        with self._lock:
            return self.loading

    def set_crafting_loaded(self, value: bool):
        with self._lock:
            self.crafting_loaded = value

    def set_loaded(self, value: bool):
        with self._lock:
            self.loaded = value

    # ── Crafting / Fabricator data ──────────────────────────────────────

    def load_crafting(self, on_done=None):
        """Fetch crafting_blueprints and crafting_items JSONs for the current version."""
        with self._lock:
            if self.crafting_loading:
                return
            self.crafting_loading = True

        ver = self.version
        if not ver:
            with self._lock:
                self.crafting_loading = False
            if on_done:
                on_done()
            return

        def _run():
            try:
                bp_url = f"{SCMDB_BASE}/data/crafting_blueprints-{ver}.json"
                items_url = f"{SCMDB_BASE}/data/crafting_items-{ver}.json"

                bp_data = self._fetch(bp_url)
                items_data = self._fetch(items_url)

                # Build all data in local variables first
                _blueprints = bp_data.get("blueprints", []) if bp_data else []
                _resources = bp_data.get("resources", []) if bp_data else []
                _gem_items = bp_data.get("items", []) if bp_data else []
                _properties = bp_data.get("properties", {}) if bp_data else {}
                _dismantle = bp_data.get("dismantle", {}) if bp_data else {}
                _meta = bp_data.get("meta", {}) if bp_data else {}

                _items = items_data.get("items", []) if items_data else []
                _manufacturers = items_data.get("manufacturers", {}) if items_data else {}
                _items_map = {}
                _items_by_name = {}
                for item in _items:
                    ec = item.get("entityClass", "")
                    if ec:
                        _items_map[ec] = item
                    name = item.get("name", "")
                    if name:
                        _items_by_name[name] = item

                _loaded = bool(bp_data)

                # Atomically swap under lock
                with self._lock:
                    self.crafting_blueprints = _blueprints
                    self.crafting_resources = _resources
                    self.crafting_gem_items = _gem_items
                    self.crafting_properties = _properties
                    self.crafting_dismantle = _dismantle
                    self.crafting_meta = _meta
                    self.crafting_items = _items
                    self.crafting_manufacturers = _manufacturers
                    self.crafting_items_map = _items_map
                    self.crafting_items_by_name = _items_by_name
                    self.crafting_loaded = _loaded
                    if not bp_data and items_data:
                        # Blueprints failed but items succeeded — clear items too
                        self.crafting_items = []
                        self.crafting_items_map = {}
                        self.crafting_items_by_name = {}

            except Exception as exc:
                with self._lock:
                    self.crafting_loaded = False
            finally:
                with self._lock:
                    self.crafting_loading = False
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    def get_blueprint_product(self, bp: dict) -> Optional[dict]:
        """Get the crafting_items entry for a blueprint's product."""
        ec = bp.get("productEntityClass", "")
        return self.crafting_items_map.get(ec)

    def get_blueprint_product_name(self, bp: dict) -> str:
        """Get display name for a blueprint product."""
        prod = self.get_blueprint_product(bp)
        if prod:
            return prod.get("name", bp.get("productName", bp.get("tag", "?")))
        return bp.get("productName", bp.get("tag", "?"))

    # ── Mining / Resources data ─────────────────────────────────────────

    def load_mining(self, on_done=None):
        """Fetch mining_data and mining_equipment JSONs for the current version."""
        with self._lock:
            if self.mining_loading:
                return
            self.mining_loading = True

        ver = self.version
        if not ver:
            with self._lock:
                self.mining_loading = False
            if on_done:
                on_done()
            return

        def _run():
            try:
                mining_url = f"{SCMDB_BASE}/data/mining_data-{ver}.json"
                equip_url = f"{SCMDB_BASE}/data/mining_equipment-{ver}.json"

                mining_data = self._fetch(mining_url)
                equip_data = self._fetch(equip_url)

                # Build in locals
                _locations = mining_data.get("locations", []) if mining_data else []
                _elements = mining_data.get("mineableElements", {}) if mining_data else {}
                _compositions = mining_data.get("compositions", {}) if mining_data else {}
                _clustering = mining_data.get("clusteringPresets", {}) if mining_data else {}
                _lasers = equip_data.get("lasers", []) if equip_data else []
                _modules = equip_data.get("modules", []) if equip_data else []
                _gadgets = equip_data.get("gadgets", []) if equip_data else []

                # Swap atomically under lock, then index
                with self._lock:
                    self.mining_locations = _locations
                    self.mining_elements = _elements
                    self.mining_compositions = _compositions
                    self.mining_clustering = _clustering
                    self.mining_equipment_lasers = _lasers
                    self.mining_equipment_modules = _modules
                    self.mining_equipment_gadgets = _gadgets

                if mining_data:
                    self._index_mining()

                with self._lock:
                    self.mining_loaded = bool(mining_data and _locations)

            except Exception:
                with self._lock:
                    self.mining_loaded = False
            finally:
                with self._lock:
                    self.mining_loading = False
                if on_done:
                    on_done()

        threading.Thread(target=_run, daemon=True).start()

    def _index_mining(self):
        """Build resource-to-location and location-to-resource lookups."""
        r2l = defaultdict(list)  # resource -> locations
        l2r = defaultdict(list)  # location -> resources
        res_cats = defaultdict(set)  # category_label -> {resource_names}
        all_types = set()
        all_systems = set()

        hidden = self.HIDDEN_LOCATIONS

        cat_labels = {
            "SpaceShip_Mineables": "Ores",
            "SpaceShip_Mineables_Rare": "Ores",
            "GroundVehicle_Mineables": "Vehicle Mining",
            "FPS_Mineables": "FPS Mining",
            "Harvestables": "Plants",
        }

        for loc in self.mining_locations:
            loc_name = loc.get("locationName", "")
            if loc_name in hidden:
                continue

            loc_type = loc.get("locationType", "")
            system = loc.get("system", "")
            all_types.add(loc_type)
            all_systems.add(system)

            for group in loc.get("groups", []):
                grp_name = group.get("groupName", "")
                deposits = group.get("deposits", [])
                total_prob = sum(d.get("relativeProbability", 0) for d in deposits)

                for dep in deposits:
                    comp_guid = dep.get("compositionGuid", "")
                    comp = self.mining_compositions.get(comp_guid, {})
                    dep_prob = dep.get("relativeProbability", 0) / total_prob if total_prob else 0

                    # Harvestables use presetName instead of compositions
                    preset_name = dep.get("presetName", "")
                    if preset_name and not comp.get("parts"):
                        entry = {
                            "location": loc_name,
                            "system": system,
                            "type": loc_type,
                            "group": grp_name,
                            "min_pct": 0,
                            "max_pct": dep_prob * 100,
                            "probability": dep_prob,
                        }
                        r2l[preset_name].append(entry)
                        l2r[loc_name].append({
                            "resource": preset_name,
                            "group": grp_name,
                            "min_pct": 0,
                            "max_pct": dep_prob * 100,
                        })
                        cat = cat_labels.get(grp_name, "")
                        if cat:
                            res_cats[cat].add(preset_name)
                        continue

                    for part in comp.get("parts", []):
                        elem_name = part.get("elementName", "")
                        if not elem_name:
                            continue
                        min_pct = part.get("minPercent", 0)
                        max_pct = part.get("maxPercent", 0)

                        entry = {
                            "location": loc_name,
                            "system": system,
                            "type": loc_type,
                            "group": grp_name,
                            "min_pct": min_pct,
                            "max_pct": max_pct,
                            "probability": dep_prob,
                        }
                        r2l[elem_name].append(entry)
                        l2r[loc_name].append({
                            "resource": elem_name,
                            "group": grp_name,
                            "min_pct": min_pct,
                            "max_pct": max_pct,
                        })

                        # Categorize resource by group type
                        cat = cat_labels.get(grp_name, "")
                        if cat:
                            res_cats[cat].add(elem_name)

        _r2l = dict(r2l)
        _l2r = dict(l2r)
        _all_res = sorted(r2l.keys())
        _all_lt = sorted(all_types)
        _all_ms = sorted(all_systems)
        _res_cats = {k: sorted(v) for k, v in res_cats.items()}

        with self._lock:
            self.resource_to_locations = _r2l
            self.location_to_resources = _l2r
            self.all_resource_names = _all_res
            self.all_location_types = _all_lt
            self.all_mining_systems = _all_ms
            self.resource_categories = _res_cats

    def get_location_resources(self, loc_name: str) -> list:
        """Get deduplicated resources for a location, sorted by max_pct desc."""
        entries = self.location_to_resources.get(loc_name, [])
        # Deduplicate by resource name, keeping max percentages
        best = {}
        for e in entries:
            name = e["resource"]
            if name not in best or e["max_pct"] > best[name]["max_pct"]:
                best[name] = e
        return sorted(best.values(), key=lambda x: -x["max_pct"])

    # ── Pseudo-category detection (matches scmdb.net JS logic) ──────────

    def is_ace(self, c: dict) -> bool:
        """ACE = shipEncounters has AcePilot group with spawnChance > 0, not ShipAmbush."""
        se = c.get("shipEncounters")
        if not se:
            return False
        sc = se.get("spawnConfig", {})
        groups = sc.get("groups", [])
        has_ace_pilot = any(g.get("role") == "AcePilot" and (g.get("spawnChance") or 0) > 0
                           for g in groups)
        is_ambush = "ShipAmbush" in (c.get("debugName") or "")
        return has_ace_pilot and not is_ambush

    def is_asd(self, c: dict) -> bool:
        """ASD = debugName matches /^Hockrow_(FacilityDelve|ASD)_/."""
        dn = c.get("debugName") or ""
        return bool(re.match(r"^Hockrow_(FacilityDelve|ASD)_", dn))

    def is_wikelo(self, c: dict) -> bool:
        """Wikelo = faction is 'Wikelo Emporium'."""
        fg = c.get("factionGuid", "")
        fn = self.faction_by_guid.get(fg, {}).get("name", "")
        return fn == "Wikelo Emporium"

    def is_blueprint(self, c: dict) -> bool:
        """Blueprint = has blueprintRewards with resolved pools."""
        bp = c.get("blueprintRewards") or c.get("itemRewards") or []
        if not bp:
            return False
        # Check if any reward references a blueprint pool
        for reward in bp:
            if isinstance(reward, dict):
                pool_id = reward.get("blueprintPool", "")
                if pool_id and pool_id in self.blueprint_pools:
                    return True
        return False

    def _matches_pseudo_category(self, c: dict, cat: str) -> bool:
        """Check if contract matches a pseudo-category filter."""
        if cat == "ace":
            return self.is_ace(c)
        if cat == "asd":
            return self.is_asd(c)
        if cat == "wikelo":
            return self.is_wikelo(c)
        if cat == "blueprints":
            return self.is_blueprint(c)
        return False

    def filter_contracts(self, filters: dict) -> list:
        """Apply all filters and return matching contracts."""
        # Snapshot shared data under lock so we iterate a consistent view
        with self._lock:
            _contracts = self.contracts
            _faction_by_guid = self.faction_by_guid
            _location_pools = self.location_pools
            _availability_pools = self.availability_pools
            _scopes = self.scopes
            _blueprint_pools = self.blueprint_pools

        results = []
        search = (filters.get("search") or "").lower()
        categories = filters.get("categories", set())
        systems = filters.get("systems", set())
        mission_type = filters.get("mission_type", "")
        factions = filters.get("factions", set())
        legality = filters.get("legality", "")  # "legal", "illegal", ""
        sharing = filters.get("sharing", "")     # "sharable", "solo", ""
        availability = filters.get("availability", "")  # "unique", "repeatable", ""
        rank_max = filters.get("rank_max", 6)
        reward_min = filters.get("reward_min", 0)
        reward_max = filters.get("reward_max", 999999999)

        for c in _contracts:
            # Search
            if search:
                title = (c.get("title") or "").lower()
                desc = (c.get("description") or "").lower()
                debug = (c.get("debugName") or "").lower()
                if search not in title and search not in desc and search not in debug:
                    continue

            # Category — real categories (career/story/event) + pseudo-categories (ace/asd/wikelo/blueprints)
            if categories:
                PSEUDO = {"ace", "asd", "wikelo", "blueprints"}
                real_cats = categories - PSEUDO
                pseudo_cats = categories & PSEUDO
                matched = False
                # Check real categories
                if real_cats and c.get("category", "") in real_cats:
                    matched = True
                # Check pseudo-categories (any match = include)
                if pseudo_cats:
                    for pc in pseudo_cats:
                        if self._matches_pseudo_category(c, pc):
                            matched = True
                            break
                # If only real cats selected and no pseudo, or vice versa
                if not matched:
                    continue

            # Systems ("Multi" = contracts with 2+ systems)
            if systems:
                c_sys = set(c.get("systems") or [])
                want = set(systems)
                if "Multi" in want:
                    want.discard("Multi")
                    # "Multi" matches contracts in 2+ systems
                    if len(c_sys) >= 2:
                        pass  # multi match
                    elif want and c_sys.intersection(want):
                        pass  # specific system match
                    elif not want:
                        if len(c_sys) < 2:
                            continue
                    else:
                        continue
                else:
                    if not c_sys.intersection(want):
                        continue

            # Mission type
            if mission_type:
                if c.get("missionType", "") != mission_type:
                    continue

            # Factions
            if factions:
                fg = c.get("factionGuid", "")
                fn = _faction_by_guid.get(fg, {}).get("name", "")
                if fn not in factions:
                    continue

            # Legality
            if legality == "legal" and c.get("illegal"):
                continue
            if legality == "illegal" and not c.get("illegal"):
                continue

            # Sharing
            if sharing == "sharable" and not c.get("canBeShared"):
                continue
            if sharing == "solo" and c.get("canBeShared"):
                continue

            # Availability
            if availability:
                try:
                    avail = _availability_pools[c.get("availabilityIndex")]
                except (IndexError, TypeError):
                    avail = {}
                if availability == "unique" and not avail.get("onceOnly"):
                    continue
                if availability == "repeatable" and avail.get("onceOnly"):
                    continue

            # Rank
            ms = c.get("minStanding") or {}
            rank_idx = 0
            if isinstance(ms, dict):
                # Try to extract rank index from scopes
                rank_idx = ms.get("rankIndex", 0) or 0
            if rank_idx > rank_max:
                continue

            # Reward
            reward = c.get("rewardUEC")
            if reward is not None:
                if isinstance(reward, (int, float)):
                    if reward < reward_min or reward > reward_max:
                        continue

            results.append(c)

        return results


# ══════════════════════════════════════════════════════════════════════════════
# Mission Detail Modal
# ══════════════════════════════════════════════════════════════════════════════

class MissionDetailModal(tk.Toplevel):
    """Popup modal showing full mission details with 4 tabs."""

    def __init__(self, root, contract: dict, data_mgr: MissionDataManager):
        super().__init__(root)
        self._contract = contract
        self._data = data_mgr

        self.title("Mission Details")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.geometry("650x550")
        self.resizable(True, True)

        # Center on parent
        self.update_idletasks()
        px = root.winfo_x() + (root.winfo_width() - 650) // 2
        py = root.winfo_y() + (root.winfo_height() - 550) // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        self._build_ui()
        self.grab_set()
        self.focus_set()

    def _build_ui(self):
        c = self._contract
        faction = self._data.get_faction(c.get("factionGuid", ""))
        fname = faction.get("name", "Unknown")
        initials = _faction_initials(fname)

        # ── Header ──
        hdr = tk.Frame(self, bg=BG2, padx=12, pady=10)
        hdr.pack(fill="x")

        # Faction badge
        badge = tk.Label(hdr, text=initials, font=("Consolas", 10, "bold"),
                         bg="#1a2538", fg=ACCENT, width=4, height=2)
        badge.pack(side="left", padx=(0, 10))

        # Title + faction
        title_fr = tk.Frame(hdr, bg=BG2)
        title_fr.pack(side="left", fill="x", expand=True)
        title_text = c.get("title", "Unknown Mission")
        # Clean up localization keys
        if title_text.startswith("@"):
            title_text = c.get("debugName", title_text)
        tk.Label(title_fr, text=title_text, font=("Consolas", 11, "bold"),
                 bg=BG2, fg=FG, anchor="w", wraplength=450).pack(fill="x")
        tk.Label(title_fr, text=fname, font=("Consolas", 9),
                 bg=BG2, fg=FG_DIM, anchor="w").pack(fill="x")

        # Close button
        tk.Button(hdr, text="✕", font=("Consolas", 11), bg=BG2, fg=FG_DIM,
                  relief="flat", bd=0, cursor="hand2",
                  command=self.destroy).pack(side="right")

        # ── Tags row ──
        tags_fr = tk.Frame(self, bg=BG, padx=12, pady=4)
        tags_fr.pack(fill="x")
        tags = []
        for s in (c.get("systems") or []):
            tags.append(("system", f"{s}"))
        mt = c.get("missionType", "")
        if mt:
            tags.append(("type", mt))
        if not c.get("illegal"):
            tags.append(("legal", "LEGAL"))
        else:
            tags.append(("illegal", "ILLEGAL"))
        # Chain detection
        prereqs = c.get("prerequisites") or {}
        if prereqs.get("completedContractTags"):
            tags.append(("chain", "CHAIN"))
        cat = c.get("category", "")
        if cat:
            tags.append(("cat", cat.title()))

        for tag_type, tag_text in tags:
            if tag_type == "system":
                bg_c, fg_c = _tag_colors(tag_text)
            elif tag_type == "type":
                bg_c, fg_c = _tag_colors(tag_text)
            elif tag_type in ("legal", "illegal"):
                bg_c, fg_c = _tag_colors(tag_text.upper())
            elif tag_type == "chain":
                bg_c, fg_c = _tag_colors("CHAIN")
            else:
                bg_c, fg_c = _tag_colors(tag_text.lower())
            tk.Label(tags_fr, text=f" {tag_text} ", font=("Consolas", 8, "bold"),
                     bg=bg_c, fg=fg_c, padx=4, pady=1).pack(side="left", padx=(0, 4))

        # ── Tab bar ──
        tab_bar = tk.Frame(self, bg=BG, padx=12, pady=4)
        tab_bar.pack(fill="x")
        self._tab_frames = []
        self._tab_btns = []
        self._active_tab = 0

        for idx, name in enumerate(["OVERVIEW", "REQUIREMENTS", "CALCULATOR", "COMMUNITY"]):
            btn = tk.Button(tab_bar, text=name, font=("Consolas", 9, "bold"),
                            bg=BG, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=8, pady=4,
                            command=lambda i=idx: self._switch_tab(i))
            btn.pack(side="left")
            self._tab_btns.append(btn)

        # ── Tab content area (scrollable) ──
        content_outer = tk.Frame(self, bg=BG)
        content_outer.pack(fill="both", expand=True)

        for i in range(4):
            fr = tk.Frame(content_outer, bg=BG)
            self._tab_frames.append(fr)

        self._build_overview_tab()
        self._build_requirements_tab()
        self._build_calculator_tab()
        self._build_community_tab()
        self._switch_tab(0)

    def _switch_tab(self, idx):
        self._active_tab = idx
        for i, fr in enumerate(self._tab_frames):
            if i == idx:
                fr.pack(fill="both", expand=True, padx=12, pady=6)
            else:
                fr.pack_forget()
        for i, btn in enumerate(self._tab_btns):
            if i == idx:
                btn.configure(fg=ACCENT)
            else:
                btn.configure(fg=FG_DIM)

    def _build_overview_tab(self):
        fr = self._tab_frames[0]
        c = self._contract
        faction = self._data.get_faction(c.get("factionGuid", ""))

        # Scrollable
        canvas = tk.Canvas(fr, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(fr, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))

        # Faction info
        fname = faction.get("name", "Unknown")
        tk.Label(inner, text=fname, font=("Consolas", 10, "bold"),
                 bg=BG, fg=FG, anchor="w").pack(fill="x", pady=(4, 2))

        # Description
        desc = _strip_html(c.get("description", ""))
        if desc and not desc.startswith("@"):
            tk.Label(inner, text=desc, font=("Consolas", 9),
                     bg=BG, fg=FG, anchor="w", wraplength=580,
                     justify="left").pack(fill="x", pady=(8, 4))

        # Reward box
        reward = c.get("rewardUEC")
        if reward is not None:
            rw_fr = tk.Frame(inner, bg=BG3, padx=12, pady=8)
            rw_fr.pack(fill="x", pady=(12, 4))
            tk.Label(rw_fr, text="REWARD", font=("Consolas", 8),
                     bg=BG3, fg=FG_DIM, anchor="w").pack(fill="x")
            tk.Label(rw_fr, text=_fmt_uec(reward), font=("Consolas", 13, "bold"),
                     bg=BG3, fg=YELLOW, anchor="w").pack(fill="x")

        # Buy-in
        buyin = c.get("buyIn")
        if buyin:
            bi_fr = tk.Frame(inner, bg=BG3, padx=12, pady=8)
            bi_fr.pack(fill="x", pady=(4, 4))
            tk.Label(bi_fr, text="BUY-IN", font=("Consolas", 8),
                     bg=BG3, fg=FG_DIM, anchor="w").pack(fill="x")
            tk.Label(bi_fr, text=_fmt_uec(buyin), font=("Consolas", 11, "bold"),
                     bg=BG3, fg=RED, anchor="w").pack(fill="x")

        # Hauling orders
        hauling = c.get("haulingOrders")
        if hauling:
            tk.Label(inner, text="HAULING ORDERS", font=("Consolas", 8, "bold"),
                     bg=BG, fg=FG_DIM, anchor="w").pack(fill="x", pady=(12, 4))
            for ho in hauling:
                res = ho.get("resource", {}).get("name", "Unknown cargo")
                mn = ho.get("minSCU", 0)
                mx = ho.get("maxSCU", 0)
                txt = f"  {res}: {mn}–{mx} SCU"
                tk.Label(inner, text=txt, font=("Consolas", 9),
                         bg=BG, fg=ORANGE, anchor="w").pack(fill="x")

        # Blueprint rewards
        bp_rewards = c.get("blueprintRewards")
        if bp_rewards and isinstance(bp_rewards, list):
            tk.Label(inner, text="BLUEPRINT REWARDS", font=("Consolas", 8, "bold"),
                     bg=BG, fg=FG_DIM, anchor="w").pack(fill="x", pady=(12, 4))
            for reward in bp_rewards:
                if not isinstance(reward, dict):
                    continue
                pool_id = reward.get("blueprintPool", "")
                pool_name = reward.get("poolName", "")
                chance = reward.get("chance", 1)

                # Resolve pool to get actual blueprint item names
                pool = self._data.blueprint_pools.get(pool_id, {})
                pool_display = pool.get("name", pool_name) or pool_name
                blueprints = pool.get("blueprints", [])

                # Pool header with chance
                chance_str = f" ({int(chance * 100)}%)" if chance < 1 else ""
                tk.Label(inner, text=f"  🔧 {pool_display}{chance_str}",
                         font=("Consolas", 8, "bold"),
                         bg=BG, fg=ACCENT, anchor="w").pack(fill="x")

                # Individual blueprint items from the pool
                if blueprints:
                    for bp_item in blueprints[:8]:
                        bp_name = bp_item.get("name", "?") if isinstance(bp_item, dict) else str(bp_item)
                        tk.Label(inner, text=f"     ↘ {bp_name}",
                                 font=("Consolas", 8),
                                 bg=BG, fg=GREEN, anchor="w").pack(fill="x")
                    if len(blueprints) > 8:
                        tk.Label(inner, text=f"     +{len(blueprints) - 8} more",
                                 font=("Consolas", 8),
                                 bg=BG, fg=FG_DIM, anchor="w").pack(fill="x")
                elif pool_display:
                    # Pool exists but no resolved blueprints — show pool name
                    tk.Label(inner, text=f"     (pool: {pool_display})",
                             font=("Consolas", 8),
                             bg=BG, fg=FG_DIM, anchor="w").pack(fill="x")

    def _build_requirements_tab(self):
        fr = self._tab_frames[1]
        c = self._contract

        # Scrollable
        canvas = tk.Canvas(fr, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(fr, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))

        prereqs = c.get("prerequisites") or {}

        # Mission chain
        ct = prereqs.get("completedContractTags")
        if ct:
            tk.Label(inner, text="MISSION CHAIN", font=("Consolas", 9, "bold"),
                     bg=BG, fg=FG, anchor="w").pack(fill="x", pady=(4, 8))
            req_tags = ct.get("tags", [])
            if req_tags:
                tk.Label(inner, text="REQUIRES COMPLETION OF:",
                         font=("Consolas", 8), bg=BG, fg=FG_DIM,
                         anchor="w").pack(fill="x", pady=(0, 4))
                tag_row = tk.Frame(inner, bg=BG)
                tag_row.pack(fill="x")
                for tag in req_tags:
                    # Try to find contract by tag
                    tag_name = str(tag)
                    tk.Label(tag_row, text=f" {tag_name} ", font=("Consolas", 8),
                             bg="#1a2538", fg=ACCENT, padx=4, pady=2).pack(
                        side="left", padx=(0, 4), pady=2)

        # Linked intros
        intros = c.get("linkedIntros")
        if intros:
            tk.Label(inner, text="CHAIN STARTS WITH:",
                     font=("Consolas", 8), bg=BG, fg=FG_DIM,
                     anchor="w").pack(fill="x", pady=(8, 4))
            for intro in intros:
                name = intro.get("title", intro.get("debugName", "?"))
                if name.startswith("@"):
                    name = intro.get("debugName", name)
                tk.Label(inner, text=f" {name} ", font=("Consolas", 8),
                         bg="#1a3322", fg=GREEN, padx=4, pady=2).pack(
                    anchor="w", padx=4, pady=2)

        # Boolean flags grid (2x3)
        avail = self._data.get_availability(c.get("availabilityIndex"))
        flags = [
            ("SHAREABLE", c.get("canBeShared", False)),
            ("ILLEGAL", c.get("illegal", False)),
            ("ONCE ONLY", avail.get("onceOnly", False)),
            ("RE-ACCEPT AFTER ABANDON", avail.get("canReacceptAfterAbandoning", False)),
            ("RE-ACCEPT AFTER FAIL", avail.get("canReacceptAfterFailing", False)),
            ("AVAILABLE IN PRISON", avail.get("availableInPrison", False)),
        ]

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(12, 8))

        flags_grid = tk.Frame(inner, bg=BG)
        flags_grid.pack(fill="x")
        for i, (label, val) in enumerate(flags):
            row = i // 2
            col = i % 2
            cell = tk.Frame(flags_grid, bg=BG3, padx=10, pady=6)
            cell.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")
            flags_grid.columnconfigure(col, weight=1)
            tk.Label(cell, text=label, font=("Consolas", 7),
                     bg=BG3, fg=FG_DIM, anchor="w").pack(fill="x")
            color = GREEN if val else RED
            tk.Label(cell, text="Yes" if val else "No",
                     font=("Consolas", 10, "bold"),
                     bg=BG3, fg=color, anchor="w").pack(fill="x")

        # Cooldown
        cd = avail.get("personalCooldownTime", 0)
        if cd:
            cd_fr = tk.Frame(inner, bg=BG3, padx=10, pady=6)
            cd_fr.pack(fill="x", pady=(4, 0))
            tk.Label(cd_fr, text="COOLDOWN", font=("Consolas", 7),
                     bg=BG3, fg=FG_DIM, anchor="w").pack(fill="x")
            tk.Label(cd_fr, text=_fmt_time(cd), font=("Consolas", 10, "bold"),
                     bg=BG3, fg=FG, anchor="w").pack(fill="x")
            tk.Label(cd_fr, text="Estimated minimum wait time before you can accept "
                     "this mission again.", font=("Consolas", 7),
                     bg=BG3, fg=FG_DIMMER, anchor="w", wraplength=580).pack(fill="x")

    def _build_calculator_tab(self):
        fr = self._tab_frames[2]
        c = self._contract

        # Scrollable
        canvas = tk.Canvas(fr, bg=BG, highlightthickness=0)
        vbar = ttk.Scrollbar(fr, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=BG)
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))

        faction = self._data.get_faction(c.get("factionGuid", ""))
        fname = faction.get("name", "Unknown")

        # ── REWARDS summary line ──
        tk.Label(inner, text="REWARDS", font=("Consolas", 10, "bold"),
                 bg=BG, fg=YELLOW, anchor="w").pack(fill="x", pady=(6, 4))

        reward = c.get("rewardUEC")
        # Find faction reward XP from the factionRewardsPools
        rep_xp = 0
        scope_name = ""
        scope_guid = ""
        fri = c.get("factionRewardsIndex")
        if fri is not None:
            try:
                fr_pool = self._data.faction_rewards_pools[fri]
                if isinstance(fr_pool, list) and fr_pool:
                    for entry in fr_pool:
                        if isinstance(entry, dict):
                            rep_xp = entry.get("amount", 0) or 0
                            scope_guid = entry.get("scopeGuid", "")
                            break
            except (IndexError, TypeError):
                pass

        # Resolve scope name from scopes
        if scope_guid and scope_guid in self._data.scopes:
            scope_obj = self._data.scopes[scope_guid]
            scope_name = scope_obj.get("scopeName", "")

        info_row = tk.Frame(inner, bg=BG)
        info_row.pack(fill="x", pady=(0, 8))
        parts = []
        if reward:
            parts.append(f"UEC: {reward:,}")
        if rep_xp:
            parts.append(f"REP/MISSION: {rep_xp} XP")
        parts.append(f"FACTION: {fname}")
        if scope_name:
            parts.append(f"SCOPE: {scope_name}")
        info_text = "  ".join(parts)
        tk.Label(info_row, text=info_text, font=("Consolas", 8),
                 bg=BG, fg=FG, anchor="w", wraplength=580).pack(fill="x")

        # ── Solo / Multicrew tabs (display only) ──
        mode_fr = tk.Frame(inner, bg=BG)
        mode_fr.pack(fill="x", pady=(0, 8))
        shared = c.get("canBeShared", False)
        for txt, active in [("Solo", True), ("Multicrew", shared)]:
            bg_c = "#1a3030" if active else BG3
            fg_c = FG if active else FG_DIMMER
            tk.Label(mode_fr, text=f" {txt} ", font=("Consolas", 9),
                     bg=bg_c, fg=fg_c, padx=6, pady=2).pack(side="left", padx=(0, 4))

        # ── Min rank notice ──
        ms = c.get("minStanding") or {}
        min_rank_name = ""
        min_rank_idx = 0
        if isinstance(ms, dict):
            min_rank_name = ms.get("name", "")
            min_rank_idx = ms.get("rankIndex", 0) or 0
            min_rep = ms.get("minReputation", 0) or 0

        if min_rank_name and min_rank_name != "Neutral":
            notice_fr = tk.Frame(inner, bg="#332a11", padx=10, pady=6)
            notice_fr.pack(fill="x", pady=(0, 8))
            tk.Label(notice_fr,
                     text=f"Contract requires {min_rank_name} rank to accept "
                          f"\u2014 lower ranks are grayed out.",
                     font=("Consolas", 8), bg="#332a11", fg=YELLOW,
                     anchor="w", wraplength=560).pack(fill="x")

        # ── Rank progression table ──
        # Find the scope ranks
        ranks = []
        if scope_guid and scope_guid in self._data.scopes:
            scope_obj = self._data.scopes[scope_guid]
            ranks = scope_obj.get("ranks", [])

        if ranks:
            # Table header
            hdr_fr = tk.Frame(inner, bg=BG)
            hdr_fr.pack(fill="x")
            tk.Label(hdr_fr, text="RANK", font=("Consolas", 8, "bold"),
                     bg=BG, fg=FG_DIM, anchor="w", width=28).pack(side="left")
            tk.Label(hdr_fr, text="XP TO FILL", font=("Consolas", 8, "bold"),
                     bg=BG, fg=FG_DIM, anchor="e", width=12).pack(side="left")
            tk.Label(hdr_fr, text="MISSIONS", font=("Consolas", 8, "bold"),
                     bg=BG, fg=FG_DIM, anchor="e", width=10).pack(side="left")

            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=2)

            for rank in sorted(ranks, key=lambda r: r.get("rankIndex", 0)):
                r_idx = rank.get("rankIndex", 0)
                r_name = rank.get("name", "?")
                if r_name.startswith("@"):
                    # Fallback: use nameKey stripped
                    r_name = r_name.split("_")[-1] if "_" in r_name else r_name
                r_xp = rank.get("rangeXP", 0) or 0

                # Calculate missions to fill this rank
                missions_to_fill = math.ceil(r_xp / rep_xp) if rep_xp > 0 else 0

                # Determine if this rank is below min requirement (gray it out)
                is_min = (r_idx == min_rank_idx and min_rank_name)
                is_max = (r_idx == len(ranks) - 1)
                is_below_min = (r_idx < min_rank_idx)

                row_bg = BG3 if r_idx % 2 == 0 else BG
                if is_min:
                    row_bg = "#1a2a1a"  # Highlight min rank
                fg_color = FG_DIMMER if is_below_min else FG

                row_fr = tk.Frame(inner, bg=row_bg)
                row_fr.pack(fill="x")

                # Rank name + badges
                name_fr = tk.Frame(row_fr, bg=row_bg)
                name_fr.pack(side="left", fill="x", expand=True)
                font_w = "bold" if is_min else "normal"
                tk.Label(name_fr, text=r_name,
                         font=("Consolas", 9, font_w),
                         bg=row_bg, fg=fg_color, anchor="w",
                         padx=6, pady=3).pack(side="left")
                if is_min:
                    tk.Label(name_fr, text=" MIN ", font=("Consolas", 7, "bold"),
                             bg="#1a3322", fg=GREEN, padx=2).pack(side="left")
                if is_max:
                    tk.Label(name_fr, text=" MAX ", font=("Consolas", 7, "bold"),
                             bg="#332211", fg=ORANGE, padx=2).pack(side="left")

                # XP to fill
                xp_text = f"{r_xp:,}" if r_xp and not is_max else "\u2014"
                tk.Label(row_fr, text=xp_text, font=("Consolas", 9),
                         bg=row_bg, fg=fg_color, anchor="e",
                         width=12, padx=6, pady=3).pack(side="left")

                # Missions to fill
                m_text = str(missions_to_fill) if missions_to_fill and not is_max else "\u2014"
                m_color = FG if not is_below_min else FG_DIMMER
                if is_min:
                    m_color = ACCENT
                tk.Label(row_fr, text=m_text, font=("Consolas", 9, "bold"),
                         bg=row_bg, fg=m_color, anchor="e",
                         width=10, padx=6, pady=3).pack(side="left")

        # ── Hauling orders ──
        hauling = c.get("haulingOrders")
        if hauling:
            tk.Label(inner, text="CARGO REQUIREMENTS", font=("Consolas", 9, "bold"),
                     bg=BG, fg=FG, anchor="w").pack(fill="x", pady=(12, 4))
            for ho in hauling:
                res = ho.get("resource", {}).get("name", "Unknown")
                mn = ho.get("minSCU", 0)
                mx = ho.get("maxSCU", 0)
                row = tk.Frame(inner, bg=BG3, padx=10, pady=6)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=res, font=("Consolas", 10, "bold"),
                         bg=BG3, fg=ORANGE, anchor="w").pack(fill="x")
                tk.Label(row, text=f"SCU: {mn} \u2013 {mx}", font=("Consolas", 9),
                         bg=BG3, fg=FG, anchor="w").pack(fill="x")

        # ── Ship encounters ──
        encounters = c.get("shipEncounters")
        if encounters:
            tk.Label(inner, text="SHIP ENCOUNTERS", font=("Consolas", 9, "bold"),
                     bg=BG, fg=FG, anchor="w").pack(fill="x", pady=(12, 4))
            sc = encounters.get("spawnConfig", {})
            groups = sc.get("groups", [])
            for g in groups:
                role = g.get("role", "Unknown").replace("SpawnDescription", "").strip()
                chance = g.get("spawnChance", 1)
                waves = g.get("waves", [])
                row = tk.Frame(inner, bg=BG3, padx=10, pady=6)
                row.pack(fill="x", pady=2)
                tk.Label(row, text=role, font=("Consolas", 10, "bold"),
                         bg=BG3, fg=RED, anchor="w").pack(fill="x")
                if chance < 1:
                    tk.Label(row, text=f"Spawn chance: {chance*100:.0f}%",
                             font=("Consolas", 8), bg=BG3, fg=FG_DIM,
                             anchor="w").pack(fill="x")
                for w in waves:
                    wname = w.get("name", "Wave")
                    mn = w.get("minShips", 0)
                    mx = w.get("maxShips", 0)
                    tk.Label(row, text=f"  {wname}: {mn}\u2013{mx} ships",
                             font=("Consolas", 9), bg=BG3, fg=FG,
                             anchor="w").pack(fill="x")

    def _build_community_tab(self):
        fr = self._tab_frames[3]
        tk.Label(fr, text="Community data coming soon.",
                 font=("Consolas", 9), bg=BG, fg=FG_DIM,
                 anchor="w").pack(fill="x", pady=20)
        tk.Label(fr, text="(Requires Supabase integration for time entries,\n"
                 " difficulty ratings, and satisfaction scores.)",
                 font=("Consolas", 8), bg=BG, fg=FG_DIMMER,
                 anchor="w").pack(fill="x")


# ══════════════════════════════════════════════════════════════════════════════
# Blueprint Detail Modal
# ══════════════════════════════════════════════════════════════════════════════

class BlueprintDetailModal(tk.Toplevel):
    """Popup showing full crafting recipe for a blueprint."""

    def __init__(self, root, bp: dict, data_mgr: MissionDataManager):
        super().__init__(root)
        self._bp = bp
        self._data = data_mgr

        name = data_mgr.get_blueprint_product_name(bp)
        self.title(f"Blueprint: {name}")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.geometry("600x520")
        self.resizable(True, True)

        self.update_idletasks()
        px = root.winfo_x() + (root.winfo_width() - 600) // 2
        py = root.winfo_y() + (root.winfo_height() - 520) // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        self._build_ui()
        self.grab_set()
        self.focus_set()

    def _build_ui(self):
        bp = self._bp
        name = self._data.get_blueprint_product_name(bp)
        product = self._data.get_blueprint_product(bp)
        bp_type = bp.get("type", "?")
        bp_sub = bp.get("subtype", "").replace("_", " ").title()
        tiers = bp.get("tiers", [])
        dismantle = self._data.crafting_dismantle

        TYPE_COLORS = {"weapons": ORANGE, "armour": ACCENT, "ammo": YELLOW}
        type_color = TYPE_COLORS.get(bp_type, FG)

        # Use a Text widget as scrollable container (reliable width propagation)
        vbar = ttk.Scrollbar(self, orient="vertical")
        vbar.pack(side="right", fill="y")

        self._txt = tk.Text(self, bg=BG, bd=0, highlightthickness=0,
                            yscrollcommand=vbar.set, cursor="arrow",
                            state="disabled", wrap="none", padx=0, pady=0)
        self._txt.pack(fill="both", expand=True)
        vbar.configure(command=self._txt.yview)

        inner = tk.Frame(self._txt, bg=BG)

        # Pack all content into inner, then embed inner into the Text widget
        # Build content FIRST, then embed — this ensures inner has its natural size

        # NOTE: Python 3.14 Tk rejects tuple pady on Label constructors.
        # Use px shorthand for .pack() only, never on widget constructors.
        px = {"padx": 16, "pady": (0, 2)}

        # ── Header ──
        tk.Label(inner, text=name, font=("Consolas", 14, "bold"),
                 bg=BG, fg=FG, anchor="w").pack(fill="x", padx=16, pady=(12, 2))

        # Type + subtype
        tag_fr = tk.Frame(inner, bg=BG)
        tag_fr.pack(fill="x", **px)
        tk.Label(tag_fr, text=bp_type.title(), font=("Consolas", 8, "bold"),
                 bg=type_color, fg="white" if bp_type != "ammo" else BG,
                 padx=6).pack(side="left")
        if bp_sub:
            tk.Label(tag_fr, text=bp_sub, font=("Consolas", 8),
                     bg=BG, fg=FG_DIM, padx=6).pack(side="left")

        # Tag
        tag = bp.get("tag", "")
        if tag:
            tk.Label(inner, text=tag, font=("Consolas", 7),
                     bg=BG, fg=FG_DIMMER, anchor="w").pack(fill="x", **px)

        # ── Product stats (if available) ──
        if product:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(8, 4))
            tk.Label(inner, text="PRODUCT STATS", font=("Consolas", 9, "bold"),
                     bg=BG, fg=FG_DIM, anchor="w").pack(fill="x", **px)

            mfr = product.get("manufacturer", "")
            mfr_code = product.get("manufacturerCode", "")
            if mfr:
                mfr_display = f"{mfr_code} — {mfr}" if mfr_code else mfr
                tk.Label(inner, text=f"  Manufacturer: {mfr_display}",
                         font=("Consolas", 8), bg=BG, fg=FG, anchor="w").pack(fill="x", padx=16)

            for key, label, color in [
                ("size", "Size", FG),
                ("grade", "Grade", FG),
                ("mass", "Mass", FG_DIM),
            ]:
                val = product.get(key)
                if val is not None:
                    tk.Label(inner, text=f"  {label}: {val}",
                             font=("Consolas", 8), bg=BG, fg=color, anchor="w").pack(fill="x", padx=16)

            # Combat range
            cr = product.get("combatRange")
            if cr and isinstance(cr, dict):
                ideal = cr.get("ideal", 0)
                mx = cr.get("max", 0)
                cat = cr.get("category", "")
                tk.Label(inner, text=f"  Combat Range: {ideal}-{mx}m ({cat})",
                         font=("Consolas", 8), bg=BG, fg=GREEN, anchor="w").pack(fill="x", padx=16)

        # ── Crafting Tiers ──
        for ti, tier in enumerate(tiers):
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(10, 4))
            craft_time = tier.get("craftTimeSeconds", 0)
            mins = craft_time // 60
            secs = craft_time % 60
            time_str = f"{mins}m {secs}s" if mins else f"{secs}s"

            tier_label = f"CRAFTING RECIPE" if len(tiers) == 1 else f"TIER {ti+1}"
            hdr_fr = tk.Frame(inner, bg=BG)
            hdr_fr.pack(fill="x", **px)
            tk.Label(hdr_fr, text=tier_label, font=("Consolas", 9, "bold"),
                     bg=BG, fg=type_color, anchor="w").pack(side="left")
            tk.Label(hdr_fr, text=f"⏱ {time_str}", font=("Consolas", 8),
                     bg=BG, fg=FG_DIM).pack(side="right")

            # Slots (ingredients) with interactive quality sliders
            for slot in tier.get("slots", []):
                slot_name = slot.get("name", "?")
                options = slot.get("options", [])
                modifiers = slot.get("modifiers", [])

                slot_fr = tk.Frame(inner, bg=BG3, highlightbackground=BORDER,
                                   highlightthickness=1)
                slot_fr.pack(fill="x", padx=16, pady=(4, 0))

                slot_inner = tk.Frame(slot_fr, bg=BG3, padx=10, pady=8)
                slot_inner.pack(fill="x")

                # Slot name (green like scmdb)
                tk.Label(slot_inner, text=slot_name, font=("Consolas", 10, "bold"),
                         bg=BG3, fg=ACCENT, anchor="w").pack(fill="x")

                # Resource/item info row
                for opt in options:
                    opt_type = opt.get("type", "")
                    qty = opt.get("quantity", 0)
                    min_q = opt.get("minQuality", 0)

                    res_row = tk.Frame(slot_inner, bg=BG3)
                    res_row.pack(fill="x", pady=(2, 0))

                    if opt_type == "resource":
                        res_name = opt.get("resourceName", "?")
                        tk.Label(res_row, text="⛏", font=("Consolas", 9),
                                 bg=BG3, fg=ORANGE).pack(side="left")
                        tk.Label(res_row, text=f"  {res_name}", font=("Consolas", 9, "bold"),
                                 bg=BG3, fg=FG, anchor="w").pack(side="left")
                        tk.Label(res_row, text=f"(min {min_q})",
                                 font=("Consolas", 8), bg=BG3, fg=FG_DIM).pack(side="right")
                        tk.Label(res_row, text=f"{qty} SCU",
                                 font=("Consolas", 9), bg=BG3, fg=FG).pack(side="right", padx=8)
                    elif opt_type == "item":
                        item_name = opt.get("itemName", "?")
                        tk.Label(res_row, text="💎", font=("Consolas", 9),
                                 bg=BG3, fg=PURPLE).pack(side="left")
                        tk.Label(res_row, text=f"  {item_name}", font=("Consolas", 9, "bold"),
                                 bg=BG3, fg=FG, anchor="w").pack(side="left")
                        tk.Label(res_row, text=f"(min {min_q})",
                                 font=("Consolas", 8), bg=BG3, fg=FG_DIM).pack(side="right")
                        tk.Label(res_row, text=f"{qty} SCU",
                                 font=("Consolas", 9), bg=BG3, fg=FG).pack(side="right", padx=8)

                # Quality slider + modifiers
                if modifiers:
                    # Separator
                    tk.Frame(slot_inner, bg=BORDER, height=1).pack(fill="x", pady=(6, 4))

                    # Slider row: "QUALITY" label + Scale + value display
                    slider_row = tk.Frame(slot_inner, bg=BG3)
                    slider_row.pack(fill="x")

                    tk.Label(slider_row, text="QUALITY", font=("Consolas", 7, "bold"),
                             bg=BG3, fg=FG_DIM).pack(side="left")

                    q_var = tk.IntVar(value=750)
                    q_display = tk.Label(slider_row, text="750", font=("Consolas", 9, "bold"),
                                         bg=BG4, fg=FG, width=5, relief="flat")
                    q_display.pack(side="right", padx=(4, 0))

                    # Collect modifier labels for this slot
                    mod_labels = []

                    def _make_updater(qv, disp, mods, labels):
                        def _update(*_):
                            quality = qv.get()
                            disp.configure(text=str(quality))
                            for i, mod in enumerate(mods):
                                if i >= len(labels):
                                    break
                                start_q = mod.get("startQuality", 0)
                                end_q = mod.get("endQuality", 1000)
                                mod_start = mod.get("modifierAtStart", 1)
                                mod_end = mod.get("modifierAtEnd", 1)
                                # Linear interpolation
                                if end_q != start_q:
                                    t = max(0, min(1, (quality - start_q) / (end_q - start_q)))
                                else:
                                    t = 1
                                factor = mod_start + (mod_end - mod_start) * t
                                pct = (factor - 1) * 100
                                pct_str = f"+{pct:.1f}%" if pct >= 0 else f"{pct:.1f}%"
                                pct_color = GREEN if pct >= 0 else RED
                                name_lbl, factor_lbl, pct_lbl = labels[i]
                                factor_lbl.configure(text=f"×{factor:.3f}")
                                pct_lbl.configure(text=pct_str, fg=pct_color)
                        return _update

                    scale = ttk.Scale(slider_row, from_=0, to=1000, orient="horizontal",
                                      variable=q_var)
                    scale.pack(side="left", fill="x", expand=True, padx=(6, 6))

                    # Separator
                    tk.Frame(slot_inner, bg=BORDER, height=1).pack(fill="x", pady=(4, 2))

                    # Modifier stat rows
                    for mod in modifiers:
                        prop = mod.get("propertyName", mod.get("propertyKey", "?"))
                        start_q = mod.get("startQuality", 0)
                        end_q = mod.get("endQuality", 1000)
                        mod_start = mod.get("modifierAtStart", 1)
                        mod_end = mod.get("modifierAtEnd", 1)

                        mod_row = tk.Frame(slot_inner, bg=BG3)
                        mod_row.pack(fill="x")

                        name_lbl = tk.Label(mod_row, text=prop,
                                            font=("Consolas", 8), bg=BG3, fg=FG, anchor="w")
                        name_lbl.pack(side="left")

                        pct_lbl = tk.Label(mod_row, text="", font=("Consolas", 9, "bold"),
                                           bg=BG3, fg=GREEN, anchor="e")
                        pct_lbl.pack(side="right")

                        factor_lbl = tk.Label(mod_row, text="", font=("Consolas", 8),
                                              bg=BG3, fg=FG_DIM, anchor="e")
                        factor_lbl.pack(side="right", padx=(0, 6))

                        mod_labels.append((name_lbl, factor_lbl, pct_lbl))

                    # Quality range info
                    m0 = modifiers[0]
                    info_text = (f"Quality: {m0.get('startQuality',0)}-{m0.get('endQuality',1000)}"
                                 f" · Factor: ×{m0.get('modifierAtStart',1)}-{m0.get('modifierAtEnd',1)}"
                                 f" · Base: 500")
                    tk.Label(slot_inner, text=info_text, font=("Consolas", 7),
                             bg=BG3, fg=FG_DIMMER, anchor="center").pack(fill="x", pady=(2, 0))

                    # Wire up slider callback
                    updater = _make_updater(q_var, q_display, modifiers, mod_labels)
                    q_var.trace_add("write", updater)
                    # Initial calculation
                    updater()

        # ── Dismantle info ──
        if dismantle:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(10, 4))
            tk.Label(inner, text="DISMANTLE", font=("Consolas", 9, "bold"),
                     bg=BG, fg=RED, anchor="w").pack(fill="x", **px)
            eff = dismantle.get("efficiency", 0.5)
            dt = dismantle.get("dismantleTimeSeconds", 15)
            tk.Label(inner, text=f"  Efficiency: {eff*100:.0f}%  •  Time: {dt}s",
                     font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w", padx=16).pack(fill="x")

        # ── Missions that reward this blueprint ──
        bp_pool_ids = set()
        # Find which pools contain this product
        for pool_id, pool in self._data.blueprint_pools.items():
            for bp_item in pool.get("blueprints", []):
                bp_name = bp_item.get("name", "") if isinstance(bp_item, dict) else ""
                if bp_name == name:
                    bp_pool_ids.add(pool_id)
                    break

        if bp_pool_ids:
            missions = []
            for c in self._data.contracts:
                for reward in (c.get("blueprintRewards") or []):
                    if reward.get("blueprintPool") in bp_pool_ids:
                        title = c.get("title", "?")
                        if title.startswith("@"):
                            title = c.get("debugName", title)
                        faction = self._data.get_faction(c.get("factionGuid", ""))
                        fname = faction.get("name", "?")
                        chance = reward.get("chance", 1)
                        missions.append((title, fname, chance))
                        break

            if missions:
                tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=16, pady=(10, 4))
                tk.Label(inner, text=f"MISSIONS THAT REWARD THIS ({len(missions)})",
                         font=("Consolas", 9, "bold"),
                         bg=BG, fg=GREEN, anchor="w").pack(fill="x", **px)
                for title, fname, chance in missions[:15]:
                    chance_str = f" ({int(chance*100)}%)" if chance < 1 else ""
                    tk.Label(inner, text=f"  • {title}{chance_str}",
                             font=("Consolas", 8), bg=BG, fg=FG, anchor="w",
                             padx=16, wraplength=550).pack(fill="x")
                    tk.Label(inner, text=f"    {fname}",
                             font=("Consolas", 7), bg=BG, fg=FG_DIM, anchor="w",
                             padx=16).pack(fill="x")
                if len(missions) > 15:
                    tk.Label(inner, text=f"  +{len(missions)-15} more missions",
                             font=("Consolas", 8), bg=BG, fg=FG_DIM, anchor="w",
                             padx=16).pack(fill="x")

        # Bottom padding
        tk.Frame(inner, bg=BG, height=20).pack(fill="x")

        # Now embed the fully-built inner frame into the Text widget
        self._txt.configure(state="normal")
        self._txt.window_create("1.0", window=inner)
        self._txt.configure(state="disabled")

        # Make inner stretch to Text width
        def _sync_width(e):
            inner.configure(width=e.width - 4)
        self._txt.bind("<Configure>", _sync_width)

        # Mousewheel on all children
        def _bind_wheel(w):
            w.bind("<MouseWheel>", MissionDBApp._mw_scroll(self._txt))
            for ch in w.winfo_children():
                _bind_wheel(ch)
        _bind_wheel(inner)


# ══════════════════════════════════════════════════════════════════════════════
# Location Detail Modal
# ══════════════════════════════════════════════════════════════════════════════

class LocationDetailModal(tk.Toplevel):
    """Popup showing full resource breakdown for a mining location."""

    def __init__(self, root, loc: dict, data_mgr: MissionDataManager):
        super().__init__(root)
        self._loc = loc
        self._data = data_mgr

        name = loc.get("locationName", "?")
        self.title(f"Location: {name}")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        self.geometry("550x500")
        self.resizable(True, True)

        self.update_idletasks()
        px = root.winfo_x() + (root.winfo_width() - 550) // 2
        py = root.winfo_y() + (root.winfo_height() - 500) // 2
        self.geometry(f"+{max(0,px)}+{max(0,py)}")

        self._build_ui()
        self.grab_set()
        self.focus_set()

    def _build_ui(self):
        loc = self._loc
        loc_name = loc.get("locationName", "?")
        system = loc.get("system", "")
        loc_type = loc.get("locationType", "")

        sys_colors = {
            "Stanton": ACCENT, "Pyro": ORANGE, "Nyx": PURPLE,
        }
        sys_color = sys_colors.get(system, FG)

        # Use Text widget for scrollable content
        vbar = ttk.Scrollbar(self, orient="vertical")
        vbar.pack(side="right", fill="y")
        txt = tk.Text(self, bg=BG, bd=0, highlightthickness=0, wrap="none",
                      yscrollcommand=vbar.set, cursor="arrow", state="disabled")
        txt.pack(fill="both", expand=True)
        vbar.configure(command=txt.yview)

        inner = tk.Frame(txt, bg=BG)

        pad = {"padx": 16}

        # Header
        tk.Label(inner, text=loc_name, font=("Consolas", 14, "bold"),
                 bg=BG, fg=FG, anchor="w").pack(fill="x", **pad, pady=(12, 0))

        # System + type badges
        badge_fr = tk.Frame(inner, bg=BG)
        badge_fr.pack(fill="x", **pad, pady=(4, 0))
        tk.Label(badge_fr, text=f" {system} ", font=("Consolas", 8, "bold"),
                 bg=BG3, fg=sys_color).pack(side="left", padx=(0, 4))
        tk.Label(badge_fr, text=f" {loc_type.title()} ", font=("Consolas", 8),
                 bg=BG3, fg=FG_DIM).pack(side="left", padx=(0, 4))

        # Group type badges
        groups = loc.get("groups", [])
        for g in groups:
            gn = g.get("groupName", "")
            gt_info = self._data.MINING_GROUP_TYPES.get(gn, {})
            if gt_info:
                tk.Label(badge_fr, text=f" {gt_info['icon']} {gt_info['short']} ",
                         font=("Consolas", 8), bg=BG3, fg=FG_DIM).pack(
                             side="left", padx=(0, 4))

        # Separator
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", **pad, pady=(10, 6))

        # Resources table header
        hdr_fr = tk.Frame(inner, bg=BG)
        hdr_fr.pack(fill="x", **pad)
        tk.Label(hdr_fr, text="RESOURCE", font=("Consolas", 8, "bold"),
                 bg=BG, fg=FG_DIM, width=22, anchor="w").pack(side="left")
        tk.Label(hdr_fr, text="TYPE", font=("Consolas", 8, "bold"),
                 bg=BG, fg=FG_DIM, width=12, anchor="w").pack(side="left")
        tk.Label(hdr_fr, text="MAX %", font=("Consolas", 8, "bold"),
                 bg=BG, fg=FG_DIM, width=8, anchor="e").pack(side="right")

        # Get resources at this location
        resources = self._data.get_location_resources(loc_name)

        if not resources:
            tk.Label(inner, text="  No resources found at this location.",
                     font=("Consolas", 9), bg=BG, fg=FG_DIM).pack(
                         fill="x", **pad, pady=10)
        else:
            for i, r in enumerate(resources):
                bg = CARD_BG if i % 2 == 0 else BG3
                row = tk.Frame(inner, bg=bg)
                row.pack(fill="x", **pad, pady=1)

                name = r["resource"]
                group = r.get("group", "")
                min_pct = r.get("min_pct", 0)
                max_pct = r.get("max_pct", 0)

                # Color by max percentage
                if max_pct >= 40:
                    pct_color = GREEN
                elif max_pct >= 15:
                    pct_color = YELLOW
                elif max_pct >= 5:
                    pct_color = ORANGE
                else:
                    pct_color = FG_DIM

                # Shorten name for display
                display_name = name
                for suffix in [" (Ore)", " (Raw)", " (Gem)"]:
                    display_name = display_name.replace(suffix, "")

                # Group short name + color
                gt_info = self._data.MINING_GROUP_TYPES.get(group, {})
                group_short = gt_info.get("short", group[:8] if group else "—")
                _GRP_FG = {
                    "SpaceShip_Mineables": ACCENT,
                    "SpaceShip_Mineables_Rare": YELLOW,
                    "FPS_Mineables": PURPLE,
                    "GroundVehicle_Mineables": ORANGE,
                    "Harvestables": GREEN,
                    "Salvage_FreshDerelicts": RED,
                    "Salvage_BrokenShips_Poor": "#cc6644",
                    "Salvage_BrokenShips_Normal": "#cc6644",
                    "Salvage_BrokenShips_Elite": "#cc6644",
                }
                grp_fg = _GRP_FG.get(group, FG_DIM)

                tk.Label(row, text=display_name, font=("Consolas", 9),
                         bg=bg, fg=FG, width=22, anchor="w",
                         padx=4, pady=3).pack(side="left")
                tk.Label(row, text=group_short, font=("Consolas", 8, "bold"),
                         bg=bg, fg=grp_fg, width=12, anchor="w").pack(side="left")
                tk.Label(row, text=f"{max_pct:.0f}%", font=("Consolas", 9, "bold"),
                         bg=bg, fg=pct_color, width=8, anchor="e",
                         padx=4, pady=3).pack(side="right")

                # Percentage bar
                bar_fr = tk.Frame(row, bg=BG4, height=4, width=80)
                bar_fr.pack(side="right", padx=(4, 8), pady=6)
                bar_fr.pack_propagate(False)
                fill_w = max(1, int(80 * max_pct / 100))
                tk.Frame(bar_fr, bg=pct_color, width=fill_w).pack(
                    side="left", fill="y")

        # Footer: location stats
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", **pad, pady=(10, 6))
        stats_fr = tk.Frame(inner, bg=BG)
        stats_fr.pack(fill="x", **pad)
        n_groups = len(groups)
        n_res = len(resources)
        tk.Label(stats_fr, text=f"{n_res} resources  ·  {n_groups} deposit groups",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM).pack(side="left")

        # Bottom padding
        tk.Frame(inner, bg=BG, height=16).pack(fill="x")

        # Embed inner into Text widget
        txt.configure(state="normal")
        txt.window_create("1.0", window=inner)
        txt.configure(state="disabled")

        def _sync_width(e):
            inner.configure(width=e.width - 4)
        txt.bind("<Configure>", _sync_width)

        def _bind_wheel(w):
            w.bind("<MouseWheel>", MissionDBApp._mw_scroll(txt))
            for ch in w.winfo_children():
                _bind_wheel(ch)
        _bind_wheel(inner)


# ══════════════════════════════════════════════════════════════════════════════
# Main App
# ══════════════════════════════════════════════════════════════════════════════

class MissionDBApp:
    """Main application window — scmdb.net visual clone."""

    def __init__(self, x, y, w, h, opacity, cmd_file):
        self.cmd_file = cmd_file
        self._data = MissionDataManager()
        self._filters: dict = {}
        self._all_results: list = []
        self._card_widgets: list = []

        # Filter state
        self._search_var = None
        self._category_btns: dict = {}
        self._system_btns: dict = {}
        self._type_var = None
        self._legality_var = None
        self._sharing_var = None
        self._avail_var = None
        self._rank_var = None
        self._reward_min_var = None
        self._reward_max_var = None
        self._count_var = None
        self._status_var = None

        self._build_ui(x, y, w, h, opacity)
        self._start_cmd_watcher()
        self._data.load(on_done=lambda: self.root.after(0, self._on_data_loaded))

    def _build_ui(self, x, y, w, h, opacity):
        self.root = tk.Tk()
        self.root.title("SCMDB // Mission Database")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.configure(bg=BG)
        self.root.attributes("-alpha", opacity)
        self.root.attributes("-topmost", True)
        self.root.minsize(800, 500)

        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TCombobox",
                        fieldbackground=BG3, background=BG3, foreground=FG,
                        arrowcolor=ACCENT, selectbackground=BG3, selectforeground=FG,
                        bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)
        style.map("TCombobox",
                  fieldbackground=[("readonly", BG3)],
                  selectbackground=[("readonly", BG3)],
                  foreground=[("readonly", FG)])
        style.configure("TScrollbar", troughcolor=BG2, background=BORDER,
                        arrowcolor=FG_DIM)
        style.configure("TScale", troughcolor=BG3, background=ACCENT,
                        bordercolor=BORDER, lightcolor=ACCENT, darkcolor=ACCENT)

        # ── Header bar ──
        header = tk.Frame(self.root, bg=HEADER_BG, height=40)
        header.pack(fill="x")
        header.pack_propagate(False)

        tk.Label(header, text="SC", font=("Consolas", 12, "bold"),
                 bg=HEADER_BG, fg=ACCENT).pack(side="left", padx=(10, 0))
        tk.Label(header, text="MDB", font=("Consolas", 12, "bold"),
                 bg=HEADER_BG, fg=FG).pack(side="left")
        tk.Label(header, text="  //  MISSION DATABASE", font=("Consolas", 9),
                 bg=HEADER_BG, fg=FG_DIM).pack(side="left", padx=(4, 0))

        # Discord link button
        discord_btn = tk.Button(
            header, text="Discord: SCMDB", font=("Consolas", 8),
            bg=HEADER_BG, fg="#7289da", relief="flat", bd=0,
            cursor="hand2", padx=6,
            activebackground=HEADER_BG, activeforeground="#99aaee",
            command=lambda: __import__("webbrowser").open("https://discord.gg/qbDQBvSzPN"))
        discord_btn.pack(side="left", padx=(10, 0))

        # Version badge + LIVE/PTU toggle
        self._version_var = tk.StringVar(value="Loading...")
        tk.Label(header, textvariable=self._version_var, font=("Consolas", 8),
                 bg=HEADER_BG, fg=FG_DIMMER).pack(side="right", padx=(0, 10))

        self._ver_toggle_frame = tk.Frame(header, bg=HEADER_BG)
        self._ver_toggle_frame.pack(side="right", padx=(4, 4))
        self._ver_btns = {}

        self._live_btn = tk.Button(
            self._ver_toggle_frame, text="LIVE", font=("Consolas", 8, "bold"),
            bg="#1a3020", fg=GREEN, relief="flat", bd=0, padx=8, pady=1,
            cursor="hand2", command=lambda: self._switch_version("live"))
        self._live_btn.pack(side="left", padx=(0, 2))
        self._ver_btns["live"] = self._live_btn

        self._ptu_btn = tk.Button(
            self._ver_toggle_frame, text="PTU", font=("Consolas", 8, "bold"),
            bg=BG3, fg=FG_DIM, relief="flat", bd=0, padx=8, pady=1,
            cursor="hand2", command=lambda: self._switch_version("ptu"))
        self._ptu_btn.pack(side="left")
        self._ver_btns["ptu"] = self._ptu_btn

        self._active_channel = "live"  # current selected channel

        # Keybind toggle button
        self._keybind_file = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), ".keybind.json")
        self._keybind = self._load_keybind()
        keybind_text = self._keybind or "Set hotkey"
        self._keybind_var = tk.StringVar(value=f"⌨ {keybind_text}")
        keybind_btn = tk.Button(
            header, textvariable=self._keybind_var, font=("Consolas", 7),
            bg=HEADER_BG, fg=FG_DIMMER, relief="flat", bd=0,
            cursor="hand2", padx=4,
            activebackground=HEADER_BG, activeforeground=FG_DIM,
            command=self._set_keybind_dialog)
        keybind_btn.pack(side="right", padx=(0, 6))
        self._register_keybind()

        # Status
        self._status_var = tk.StringVar(value="Loading data...")
        tk.Label(header, textvariable=self._status_var, font=("Consolas", 8),
                 bg=HEADER_BG, fg=FG_DIM).pack(side="right", padx=10)

        # ── Page navigation bar ──
        nav = tk.Frame(self.root, bg=BG2, height=34)
        nav.pack(fill="x")
        nav.pack_propagate(False)

        self._page_btns = {}
        self._current_page = "missions"
        for page_key, page_label in [("missions", "📋 Missions"),
                                      ("fabricator", "🔧 Fabricator"),
                                      ("resources", "⛏ Resources")]:
            btn = tk.Button(
                nav, text=page_label, font=("Consolas", 9, "bold"),
                relief="flat", bd=0, cursor="hand2", padx=14, pady=4,
                command=lambda pk=page_key: self._switch_page(pk))
            btn.pack(side="left", padx=(2, 0))
            self._page_btns[page_key] = btn
        self._update_page_btn_style()

        # ── Page container (holds missions page + fabricator page) ──
        self._page_container = tk.Frame(self.root, bg=BG)
        self._page_container.pack(fill="both", expand=True)

        # ── MISSIONS PAGE ──
        self._missions_page = tk.Frame(self._page_container, bg=BG)
        main = self._missions_page  # alias for existing code

        # ── FABRICATOR PAGE (built lazily on first switch) ──
        # ── RESOURCES PAGE (built lazily on first switch) ──
        self._res_page = None
        self._res_built = False
        self._fab_page = None
        self._fab_built = False

        # Show missions page by default
        self._missions_page.pack(fill="both", expand=True)

        # Sidebar
        sidebar_outer = tk.Frame(main, bg=BG2, width=220)
        sidebar_outer.pack(side="left", fill="y")
        sidebar_outer.pack_propagate(False)

        # Sidebar scroll
        sb_canvas = tk.Canvas(sidebar_outer, bg=BG2, highlightthickness=0, width=210)
        sb_vbar = ttk.Scrollbar(sidebar_outer, orient="vertical", command=sb_canvas.yview)
        sb_vbar.pack(side="right", fill="y")
        sb_canvas.pack(fill="both", expand=True)
        sb_canvas.configure(yscrollcommand=sb_vbar.set)
        self._sidebar = tk.Frame(sb_canvas, bg=BG2)
        sb_win = sb_canvas.create_window((0, 0), window=self._sidebar, anchor="nw")
        self._sidebar.bind("<Configure>",
                           lambda e: sb_canvas.configure(scrollregion=sb_canvas.bbox("all")))
        sb_canvas.bind("<Configure>",
                       lambda e, c=sb_canvas, w=sb_win: c.itemconfig(w, width=e.width))
        sb_canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(sb_canvas))

        self._build_sidebar()

        # Separator
        tk.Frame(main, bg=BORDER, width=1).pack(side="left", fill="y")

        # Cards area
        cards_outer = tk.Frame(main, bg=BG)
        cards_outer.pack(side="left", fill="both", expand=True)

        # Count bar
        count_bar = tk.Frame(cards_outer, bg=BG, height=28)
        count_bar.pack(fill="x")
        count_bar.pack_propagate(False)
        self._count_var = tk.StringVar(value="Loading...")
        tk.Label(count_bar, textvariable=self._count_var, font=("Consolas", 9),
                 bg=BG, fg=FG_DIM, padx=10).pack(side="left")

        # Virtual scroll grid for mission cards
        self._vgrid = VirtualScrollGrid(
            cards_outer, card_width=320, row_height=130,
            fill_fn=self._fill_mission_card,
            on_click_fn=self._on_mission_click,
            slot_class=_CardSlot, bg=BG)
        self._vgrid.pack(fill="both", expand=True)

        # Legacy compat aliases (used by _switch_version clear)
        self._cards_canvas = self._vgrid._canvas
        self._cards_frame = self._vgrid  # for winfo_children destroy calls

    def _build_sidebar(self):
        sb = self._sidebar
        pad = {"padx": 8, "pady": (0, 2)}

        # ── FILTERS header ──
        hdr = tk.Frame(sb, bg=BG2)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(hdr, text="FILTERS", font=("Consolas", 10, "bold"),
                 bg=BG2, fg=FG).pack(side="left")
        tk.Button(hdr, text="Clear all", font=("Consolas", 8),
                  bg=BG2, fg=RED, relief="flat", bd=0, cursor="hand2",
                  command=self._clear_all_filters).pack(side="right")

        # ── SEARCH ──
        self._section_label(sb, "SEARCH")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._on_filter_change())
        search_entry = tk.Entry(sb, textvariable=self._search_var,
                                font=("Consolas", 9), bg=BG3, fg=FG,
                                insertbackground=FG, relief="flat",
                                highlightthickness=1, highlightcolor=BORDER,
                                highlightbackground=BORDER)
        search_entry.pack(fill="x", **pad)

        # ── CATEGORY ──
        self._section_label(sb, "CATEGORY")
        cat_fr1 = tk.Frame(sb, bg=BG2)
        cat_fr1.pack(fill="x", **pad)
        for cat in ["career", "story", "wikelo", "asd"]:
            self._make_pill_toggle(cat_fr1, cat.title() if cat != "asd" else "ASD", "category", cat)
        cat_fr2 = tk.Frame(sb, bg=BG2)
        cat_fr2.pack(fill="x", **pad)
        self._make_pill_toggle(cat_fr2, "\U0001f527 Blueprints", "category", "blueprints")
        self._make_pill_toggle(cat_fr2, "ACE", "category", "ace")

        # ── STAR SYSTEM ──
        self._section_label(sb, "STAR SYSTEM")
        sys_fr = tk.Frame(sb, bg=BG2)
        sys_fr.pack(fill="x", **pad)
        for s in ["Multi", "Nyx", "Pyro", "Stanton"]:
            self._make_pill_toggle(sys_fr, s, "system", s)

        # ── MISSION TYPE ──
        self._section_label(sb, "MISSION TYPE")
        self._type_var = tk.StringVar(value="")
        type_cb = ttk.Combobox(sb, textvariable=self._type_var,
                               state="readonly", font=("Consolas", 9))
        type_cb["values"] = ["All types"]
        type_cb.current(0)
        type_cb.bind("<<ComboboxSelected>>", lambda _: self._on_filter_change())
        type_cb.pack(fill="x", **pad)
        self._type_combo = type_cb

        # ── FACTION ──
        self._section_label(sb, "FACTION")
        self._faction_var = tk.StringVar(value="All factions")
        faction_cb = ttk.Combobox(sb, textvariable=self._faction_var,
                                  state="readonly", font=("Consolas", 9))
        faction_cb["values"] = ["All factions"]
        faction_cb.current(0)
        faction_cb.bind("<<ComboboxSelected>>", lambda _: self._on_filter_change())
        faction_cb.pack(fill="x", **pad)
        self._faction_combo = faction_cb

        # ── LEGALITY ──
        self._section_label(sb, "LEGALITY")
        leg_fr = tk.Frame(sb, bg=BG2)
        leg_fr.pack(fill="x", **pad)
        self._legality_var = tk.StringVar(value="all")
        for val, text in [("all", "All"), ("legal", "Legal"), ("illegal", "Illegal")]:
            self._make_radio_pill(leg_fr, text, self._legality_var, val)

        # ── SHARING ──
        self._section_label(sb, "SHARING")
        shr_fr = tk.Frame(sb, bg=BG2)
        shr_fr.pack(fill="x", **pad)
        self._sharing_var = tk.StringVar(value="all")
        for val, text in [("all", "All"), ("sharable", "Sharable"), ("solo", "Solo")]:
            self._make_radio_pill(shr_fr, text, self._sharing_var, val)

        # ── AVAILABILITY ──
        self._section_label(sb, "AVAILABILITY")
        avail_fr = tk.Frame(sb, bg=BG2)
        avail_fr.pack(fill="x", **pad)
        self._avail_var = tk.StringVar(value="all")
        for val, text in [("all", "All"), ("unique", "Unique"), ("repeatable", "Repeatable")]:
            self._make_radio_pill(avail_fr, text, self._avail_var, val)

        # ── RANK INDEX ──
        self._section_label(sb, "RANK INDEX")
        self._rank_var = tk.IntVar(value=6)
        rank_scale = tk.Scale(sb, from_=0, to=6, orient="horizontal",
                              variable=self._rank_var, bg=BG2, fg=FG,
                              troughcolor=BG3, highlightthickness=0,
                              activebackground=ACCENT, font=("Consolas", 8),
                              command=lambda _: self._on_filter_change())
        rank_scale.pack(fill="x", **pad)

        # ── REWARD UEC ──
        self._section_label(sb, "REWARD UEC")
        rew_fr = tk.Frame(sb, bg=BG2)
        rew_fr.pack(fill="x", **pad)
        self._reward_min_var = tk.StringVar(value="0")
        self._reward_max_var = tk.StringVar(value="9999999")
        tk.Label(rew_fr, text="Min", font=("Consolas", 8), bg=BG2, fg=FG_DIM).pack(side="left")
        tk.Entry(rew_fr, textvariable=self._reward_min_var, width=9,
                 font=("Consolas", 8), bg=BG3, fg=FG, insertbackground=FG,
                 relief="flat").pack(side="left", padx=2)
        tk.Label(rew_fr, text="–", font=("Consolas", 8), bg=BG2, fg=FG_DIM).pack(side="left")
        tk.Label(rew_fr, text="Max", font=("Consolas", 8), bg=BG2, fg=FG_DIM).pack(side="left")
        tk.Entry(rew_fr, textvariable=self._reward_max_var, width=9,
                 font=("Consolas", 8), bg=BG3, fg=FG, insertbackground=FG,
                 relief="flat").pack(side="left", padx=2)

        # Apply reward filter on Enter
        for var in (self._reward_min_var, self._reward_max_var):
            var.trace_add("write", lambda *_: self._on_filter_change_debounced())

        self._filter_timer = None

    def _section_label(self, parent, text):
        tk.Label(parent, text=text, font=("Consolas", 8, "bold"),
                 bg=BG2, fg=FG_DIM, anchor="w").pack(fill="x", padx=8, pady=(8, 2))

    def _make_pill_toggle(self, parent, text, group, value):
        """Create a toggle pill button for multi-select filters."""
        var = tk.BooleanVar(value=False)
        btn = tk.Button(parent, text=text, font=("Consolas", 8),
                        bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                        cursor="hand2", padx=6, pady=2)

        def _toggle():
            var.set(not var.get())
            if var.get():
                btn.configure(bg="#1a3030", fg=ACCENT)
            else:
                btn.configure(bg=BG3, fg=FG_DIM)
            self._on_filter_change()

        btn.configure(command=_toggle)
        btn.pack(side="left", padx=(0, 4), pady=2)

        if group == "category":
            self._category_btns[value] = (btn, var)
        elif group == "system":
            self._system_btns[value] = (btn, var)

    def _make_radio_pill(self, parent, text, var, value):
        """Create a radio-style pill button."""
        btn = tk.Button(parent, text=text, font=("Consolas", 8),
                        bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                        cursor="hand2", padx=6, pady=2)

        def _select():
            var.set(value)
            self._on_filter_change()
            # Update all siblings
            for child in parent.winfo_children():
                if isinstance(child, tk.Button):
                    child.configure(bg=BG3, fg=FG_DIM)
            btn.configure(bg="#1a3030", fg=ACCENT)

        btn.configure(command=_select)
        btn.pack(side="left", padx=(0, 4), pady=2)
        # Set initial active state
        if var.get() == value:
            btn.configure(bg="#1a3030", fg=ACCENT)

    def _clear_all_filters(self):
        """Reset all filters to defaults."""
        if self._search_var:
            self._search_var.set("")
        for btn, var in self._category_btns.values():
            var.set(False)
            btn.configure(bg=BG3, fg=FG_DIM)
        for btn, var in self._system_btns.values():
            var.set(False)
            btn.configure(bg=BG3, fg=FG_DIM)
        if self._type_var:
            self._type_var.set("")
            self._type_combo.current(0)
        if self._faction_var:
            self._faction_var.set("All factions")
            self._faction_combo.current(0)
        if self._legality_var:
            self._legality_var.set("all")
        if self._sharing_var:
            self._sharing_var.set("all")
        if self._avail_var:
            self._avail_var.set("all")
        if self._rank_var:
            self._rank_var.set(6)
        if self._reward_min_var:
            self._reward_min_var.set("0")
        if self._reward_max_var:
            self._reward_max_var.set("9999999")
        self._on_filter_change()

    # ── Helpers ──

    @staticmethod
    def _mw_scroll(canvas):
        """Return a mousewheel handler for the given canvas."""
        return lambda e: canvas.yview_scroll(
            -1 * (int(e.delta / 120) or (1 if e.delta > 0 else (-1 if e.delta < 0 else 0))),
            "units")

    # ── Filter logic ──

    def _on_filter_change_debounced(self):
        """Debounce reward entry changes."""
        if self._filter_timer:
            self.root.after_cancel(self._filter_timer)
        self._filter_timer = self.root.after(300, self._on_filter_change)

    def _on_filter_change(self):
        """Collect all filter values and rebuild card grid."""
        if not self._data.loaded:
            return

        filters = {}

        # Search
        filters["search"] = self._search_var.get() if self._search_var else ""

        # Categories
        active_cats = set()
        for cat, (btn, var) in self._category_btns.items():
            if var.get():
                active_cats.add(cat)
        filters["categories"] = active_cats

        # Systems
        active_sys = set()
        for sys_name, (btn, var) in self._system_btns.items():
            if var.get():
                active_sys.add(sys_name)
        filters["systems"] = active_sys

        # Mission type
        mt = self._type_var.get() if self._type_var else ""
        if mt == "All types" or mt == "":
            mt = ""
        filters["mission_type"] = mt

        # Faction
        fn = self._faction_var.get() if self._faction_var else ""
        if fn == "All factions" or fn == "":
            filters["factions"] = set()
        else:
            filters["factions"] = {fn}

        # Legality
        leg = self._legality_var.get() if self._legality_var else "all"
        filters["legality"] = "" if leg == "all" else leg

        # Sharing
        shr = self._sharing_var.get() if self._sharing_var else "all"
        filters["sharing"] = "" if shr == "all" else shr

        # Availability
        avl = self._avail_var.get() if self._avail_var else "all"
        filters["availability"] = "" if avl == "all" else avl

        # Rank
        filters["rank_max"] = self._rank_var.get() if self._rank_var else 6

        # Reward
        try:
            filters["reward_min"] = int(self._reward_min_var.get() or 0)
        except ValueError:
            filters["reward_min"] = 0
        try:
            filters["reward_max"] = int(self._reward_max_var.get() or 9999999)
        except ValueError:
            filters["reward_max"] = 9999999

        self._all_results = self._data.filter_contracts(filters)
        total = len(self._data.contracts)
        shown = len(self._all_results)
        if self._count_var:
            self._count_var.set(f"{shown} of {total}")
        self._rebuild_cards()

    # ── Card grid ──

    def _rebuild_cards(self):
        """Push current results into the virtual scroll grid."""
        self._vgrid.set_data(self._all_results)

    def _fill_mission_card(self, slot, contract, idx):
        """Fill a _CardSlot with mission data — zero widget creation."""
        c = contract
        faction = self._data.get_faction(c.get("factionGuid", ""))
        fname = faction.get("name", "Unknown")
        initials = _faction_initials(fname)

        title = c.get("title", "Unknown Mission")
        if title.startswith("@"):
            title = c.get("debugName", title)
        if len(title) > 50:
            title = title[:47] + "..."

        # Build tag list: (text, bg, fg, bold)
        tags = []
        for s in (c.get("systems") or [])[:2]:
            bg_c, fg_c = _tag_colors(s)
            tags.append((s, bg_c, fg_c, False))
        mt = c.get("missionType", "")
        if mt:
            bg_c, fg_c = _tag_colors(mt)
            tags.append((mt, bg_c, fg_c, True))
        prereqs = c.get("prerequisites") or {}
        if prereqs.get("completedContractTags"):
            bg_c, fg_c = _tag_colors("CHAIN")
            tags.append(("CHAIN", bg_c, fg_c, True))

        reward = c.get("rewardUEC")
        reward_text = _fmt_uec(reward) if reward else "—"
        reward_color = YELLOW if reward else FG_DIM

        slot.update(title, initials, fname, tags, reward_text, reward_color)

    def _on_mission_click(self, contract, idx):
        """Open detail modal for a mission card."""
        MissionDetailModal(self.root, contract, self._data)

    def _on_fab_click(self, bp, idx):
        """Open detail modal for a fabricator blueprint card."""
        BlueprintDetailModal(self.root, bp, self._data)

    def _on_resource_click(self, loc, idx):
        """Open detail modal for a resource/mining location card."""
        LocationDetailModal(self.root, loc, self._data)

    # ── Keybind ───────────────────────────────────────────────────────────

    def _load_keybind(self) -> str:
        """Load saved keybind from .keybind.json."""
        try:
            if os.path.isfile(self._keybind_file):
                with open(self._keybind_file, encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("keybind", "")
        except Exception:
            pass
        return ""

    def _save_keybind(self, keybind: str):
        """Save keybind to .keybind.json."""
        try:
            with open(self._keybind_file, "w", encoding="utf-8") as f:
                json.dump({"keybind": keybind}, f)
        except Exception:
            pass

    def _register_keybind(self):
        """Register the global keybind to toggle window visibility.

        NOTE: bind_all() only fires when this app has OS focus.
        For true global hotkeys, use pynput or Win32 RegisterHotKey.
        """
        if not self._keybind:
            return
        # Require at least one modifier key to avoid capturing plain letter keys
        _MODIFIERS = {"Control", "Shift", "Alt"}
        parts = self._keybind.split("-")
        if not any(p in _MODIFIERS for p in parts):
            log.warning("Keybind '%s' has no modifier key (Ctrl/Alt/Shift) — not binding", self._keybind)
            return
        try:
            self.root.bind_all(f"<{self._keybind}>", self._toggle_visibility)
        except Exception:
            pass

    def _unregister_keybind(self):
        """Unregister the current keybind."""
        if not self._keybind:
            return
        try:
            self.root.unbind_all(f"<{self._keybind}>")
        except Exception:
            pass

    def _toggle_visibility(self, event=None):
        """Toggle window between visible and hidden."""
        if self.root.state() == "withdrawn":
            self.root.deiconify()
            self.root.lift()
            self.root.focus_force()
        else:
            self.root.withdraw()

    def _set_keybind_dialog(self):
        """Open a dialog to capture a new keybind."""
        dlg = tk.Toplevel(self.root)
        dlg.title("Set Hotkey")
        dlg.configure(bg=BG)
        dlg.attributes("-topmost", True)
        dlg.geometry("300x150")
        dlg.resizable(False, False)

        # Center on parent
        dlg.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - 300) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - 150) // 2
        dlg.geometry(f"+{max(0,px)}+{max(0,py)}")

        tk.Label(dlg, text="Press any key combination...",
                 font=("Consolas", 10, "bold"), bg=BG, fg=FG,
                 pady=10).pack(fill="x")

        current = self._keybind or "None"
        tk.Label(dlg, text=f"Current: {current}",
                 font=("Consolas", 8), bg=BG, fg=FG_DIM).pack(fill="x")

        capture_var = tk.StringVar(value="Waiting for keypress...")
        tk.Label(dlg, textvariable=capture_var,
                 font=("Consolas", 9), bg=BG3, fg=ACCENT,
                 pady=8).pack(fill="x", padx=20, pady=10)

        captured = {"key": ""}

        def _on_key(e):
            parts = []
            if e.state & 0x4:
                parts.append("Control")
            if e.state & 0x1:
                parts.append("Shift")
            if e.state & 0x20000 or e.state & 0x8:
                parts.append("Alt")
            keysym = e.keysym
            if keysym not in ("Control_L", "Control_R", "Shift_L", "Shift_R",
                              "Alt_L", "Alt_R", "Meta_L", "Meta_R"):
                parts.append(keysym)
                combo = "-".join(parts)
                captured["key"] = combo
                capture_var.set(combo)

        def _save():
            key = captured["key"]
            if key:
                self._unregister_keybind()
                self._keybind = key
                self._save_keybind(key)
                self._register_keybind()
                self._keybind_var.set(f"⌨ {key}")
            dlg.destroy()

        def _clear():
            self._unregister_keybind()
            self._keybind = ""
            self._save_keybind("")
            self._keybind_var.set("⌨ Set hotkey")
            dlg.destroy()

        dlg.bind("<KeyPress>", _on_key)
        dlg.focus_set()

        btn_fr = tk.Frame(dlg, bg=BG)
        btn_fr.pack(fill="x", padx=20)
        tk.Button(btn_fr, text="Save", font=("Consolas", 8, "bold"),
                  bg="#1a3020", fg=GREEN, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=3,
                  command=_save).pack(side="left", padx=(0, 6))
        tk.Button(btn_fr, text="Clear", font=("Consolas", 8),
                  bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=3,
                  command=_clear).pack(side="left")
        tk.Button(btn_fr, text="Cancel", font=("Consolas", 8),
                  bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                  cursor="hand2", padx=12, pady=3,
                  command=dlg.destroy).pack(side="right")

    # ── Page navigation ─────────────────────────────────────────────────

    def _update_page_btn_style(self):
        for key, btn in self._page_btns.items():
            if key == self._current_page:
                btn.configure(bg="#1a2a30", fg=ACCENT,
                              activebackground="#1a2a30", activeforeground=ACCENT)
            else:
                btn.configure(bg=BG2, fg=FG_DIM,
                              activebackground=BG2, activeforeground=FG_DIM)

    def _switch_page(self, page_key):
        if page_key == self._current_page:
            return
        self._current_page = page_key
        self._update_page_btn_style()

        # Hide all pages
        for child in self._page_container.winfo_children():
            child.pack_forget()

        if page_key == "missions":
            self._missions_page.pack(fill="both", expand=True)
        elif page_key == "fabricator":
            if not self._fab_built:
                self._build_fabricator_page()
            if self._fab_page:
                self._fab_page.pack(fill="both", expand=True)
            # Load crafting data if not yet loaded
            _crafting_loaded = self._data.is_crafting_loaded()
            _loaded = self._data.is_data_loaded()
            if not _crafting_loaded and _loaded:
                self._status_var.set("Loading crafting data...")
                self._data.load_crafting(
                    on_done=lambda: self.root.after(0, self._on_crafting_loaded))

        elif page_key == "resources":
            if not self._res_built:
                self._build_resources_page()
            if self._res_page:
                self._res_page.pack(fill="both", expand=True)
            # Load mining data if not yet loaded
            _mining_loaded = self._data.is_mining_loaded()
            _loaded = self._data.is_data_loaded()
            if not _mining_loaded and _loaded:
                self._status_var.set("Loading mining/resource data...")
                self._data.load_mining(
                    on_done=lambda: self.root.after(0, self._on_mining_loaded))

    # ── Fabricator Page ──────────────────────────────────────────────────

    def _build_fabricator_page(self):
        """Build the fabricator page frame with sidebar + blueprint grid."""
        self._fab_page = tk.Frame(self._page_container, bg=BG)
        self._fab_built = True

        # ── Fabricator sidebar ──
        fab_sb_outer = tk.Frame(self._fab_page, bg=BG2, width=220)
        fab_sb_outer.pack(side="left", fill="y")
        fab_sb_outer.pack_propagate(False)

        fab_sb_canvas = tk.Canvas(fab_sb_outer, bg=BG2, highlightthickness=0, width=210)
        fab_sb_vbar = ttk.Scrollbar(fab_sb_outer, orient="vertical",
                                     command=fab_sb_canvas.yview)
        fab_sb_vbar.pack(side="right", fill="y")
        fab_sb_canvas.pack(fill="both", expand=True)
        fab_sb_canvas.configure(yscrollcommand=fab_sb_vbar.set)
        fab_sb = tk.Frame(fab_sb_canvas, bg=BG2)
        fab_sb_win = fab_sb_canvas.create_window((0, 0), window=fab_sb, anchor="nw")
        fab_sb.bind("<Configure>",
                    lambda e: fab_sb_canvas.configure(scrollregion=fab_sb_canvas.bbox("all")))
        fab_sb_canvas.bind("<Configure>",
                           lambda e, c=fab_sb_canvas, w=fab_sb_win: c.itemconfig(w, width=e.width))
        fab_sb_canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(fab_sb_canvas))

        pad = {"padx": 8, "pady": (0, 2)}

        # SEARCH
        hdr = tk.Frame(fab_sb, bg=BG2)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(hdr, text="FILTERS", font=("Consolas", 10, "bold"),
                 bg=BG2, fg=FG).pack(side="left")
        tk.Button(hdr, text="Clear", font=("Consolas", 7), bg=BG2, fg=FG_DIM,
                  relief="flat", bd=0, cursor="hand2",
                  command=self._fab_clear_filters).pack(side="right")

        self._section_label(fab_sb, "SEARCH")
        self._fab_search_var = tk.StringVar()
        self._fab_search_var.trace_add("write", lambda *_: self._fab_on_filter_change())
        tk.Entry(fab_sb, textvariable=self._fab_search_var,
                 font=("Consolas", 9), bg=BG3, fg=FG,
                 insertbackground=FG, relief="flat",
                 highlightthickness=1, highlightcolor=BORDER,
                 highlightbackground=BORDER).pack(fill="x", **pad)

        # ── Multi-select checkbox dropdown with optional search ──
        def _check_dropdown(parent, section_name, values, on_change,
                            searchable=False, visible_fn=None):
            """
            Button that opens a checkbox popup. Returns {value: BooleanVar}.
            searchable: adds a fuzzy filter entry at top of popup.
            visible_fn: optional callable() -> set of values to show (for dynamic filtering).
            """
            self._section_label(parent, section_name)
            vars_dict = {v: tk.BooleanVar(value=False) for v in values}
            display_var = tk.StringVar(value="All")

            trigger_btn = tk.Button(parent, textvariable=display_var,
                                    font=("Consolas", 8), bg=BG3, fg=FG_DIM,
                                    relief="flat", bd=0, cursor="hand2",
                                    anchor="w", padx=8, pady=3)
            trigger_btn.pack(fill="x", **pad)

            _active_popup = [None]  # Track active popup to prevent stacking

            def _update_label():
                sel = [k for k, v in vars_dict.items() if v.get()]
                if not sel:
                    display_var.set("All")
                    trigger_btn.configure(fg=FG_DIM)
                elif len(sel) <= 2:
                    display_var.set(", ".join(sel))
                    trigger_btn.configure(fg=ACCENT)
                else:
                    display_var.set(f"{len(sel)} selected")
                    trigger_btn.configure(fg=ACCENT)

            def _show_popup():
                # Destroy existing popup if present
                if _active_popup[0] is not None:
                    try:
                        _active_popup[0].destroy()
                    except Exception:
                        pass
                    _active_popup[0] = None

                # Determine which values to show
                visible = set(values)
                if visible_fn:
                    visible = visible_fn()

                popup = tk.Toplevel(self.root)
                _active_popup[0] = popup
                popup.overrideredirect(True)
                popup.configure(bg=BG3, highlightbackground=BORDER,
                                highlightthickness=1)
                popup.attributes("-topmost", True)

                bx = trigger_btn.winfo_rootx()
                by = trigger_btn.winfo_rooty() + trigger_btn.winfo_height()
                popup_w = max(trigger_btn.winfo_width(), 180)
                search_h = 28 if searchable else 0
                max_h = min(350, len(visible) * 22 + 8 + search_h)
                popup.geometry(f"{popup_w}x{max_h}+{bx}+{by}")

                # Search entry (if searchable)
                search_var = tk.StringVar()
                if searchable:
                    sf = tk.Frame(popup, bg=BG4)
                    sf.pack(fill="x")
                    tk.Entry(sf, textvariable=search_var, font=("Consolas", 8),
                             bg=BG4, fg=FG, insertbackground=FG, relief="flat",
                             highlightthickness=0).pack(fill="x", padx=4, pady=3)

                # Scrollable list
                canvas = tk.Canvas(popup, bg=BG3, highlightthickness=0)
                vbar = ttk.Scrollbar(popup, orient="vertical", command=canvas.yview)
                vbar.pack(side="right", fill="y")
                canvas.configure(yscrollcommand=vbar.set)
                canvas.pack(fill="both", expand=True)

                inner = tk.Frame(canvas, bg=BG3)
                win = canvas.create_window((0, 0), window=inner, anchor="nw")
                inner.bind("<Configure>",
                           lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
                canvas.bind("<Configure>",
                            lambda e: canvas.itemconfig(win, width=e.width))
                canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))

                # Build checkbox rows using grid (maintains order on show/hide)
                inner.columnconfigure(0, weight=1)
                row_widgets = []  # [(frame, check_lbl, text_lbl, value_str, grid_row)]
                for ri, val in enumerate(values):
                    var = vars_dict[val]
                    row = tk.Frame(inner, bg=BG3, cursor="hand2")
                    row.grid(row=ri, column=0, sticky="ew", padx=4, pady=1)

                    cb_text = val.title() if len(val) > 3 else val
                    check_lbl = tk.Label(row, text="☑" if var.get() else "☐",
                                         font=("Consolas", 9),
                                         bg=BG3, fg=ACCENT if var.get() else FG_DIM)
                    check_lbl.pack(side="left", padx=(2, 4))
                    text_lbl = tk.Label(row, text=cb_text, font=("Consolas", 8),
                                        bg=BG3, fg=FG if var.get() else FG_DIM, anchor="w")
                    text_lbl.pack(side="left", fill="x")

                    def _toggle(v=var, cl=check_lbl, tl=text_lbl):
                        v.set(not v.get())
                        cl.configure(text="☑" if v.get() else "☐",
                                     fg=ACCENT if v.get() else FG_DIM)
                        tl.configure(fg=FG if v.get() else FG_DIM)
                        _update_label()
                        on_change()

                    for w in (row, check_lbl, text_lbl):
                        w.bind("<Button-1>", lambda e, t=_toggle: t())
                        w.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))

                    row_widgets.append((row, check_lbl, text_lbl, val, ri))

                # Filter function for search + visibility
                def _apply_filter(*_):
                    q = search_var.get().lower() if searchable else ""
                    # Recompute visible set each time (for dynamic type filtering)
                    vis = visible_fn() if visible_fn else set(values)
                    for row, cl, tl, val, ri in row_widgets:
                        show = val in vis
                        if show and q:
                            show = q in val.lower()
                        if show:
                            row.grid()
                        else:
                            row.grid_remove()

                if searchable:
                    search_var.trace_add("write", _apply_filter)

                # Apply initial visibility filter
                _apply_filter()

                # Close on click outside
                def _close_check(event):
                    try:
                        wx, wy = popup.winfo_rootx(), popup.winfo_rooty()
                        ww, wh = popup.winfo_width(), popup.winfo_height()
                        if not (wx <= event.x_root <= wx+ww and wy <= event.y_root <= wy+wh):
                            popup.destroy()
                            _active_popup[0] = None
                            self.root.unbind("<Button-1>", close_id)
                    except tk.TclError:
                        pass
                close_id = self.root.bind("<Button-1>", _close_check, add="+")
                popup.bind("<Destroy>", lambda e: (
                    self.root.unbind("<Button-1>", close_id) if close_id else None), add="+")

            trigger_btn.configure(command=_show_popup)
            return vars_dict

        # Subtype-per-type mapping
        _SUBTYPES_BY_TYPE = {
            "weapons": {"lmg", "pistol", "rifle", "shotgun", "smg", "sniper"},
            "armour":  {"combat", "cosmonaut", "engineer", "environment", "explorer",
                        "flightsuit", "hunter", "medic", "miner", "racer",
                        "radiation", "salvager", "stealth", "undersuit"},
            "ammo":    {"ballistic", "electron", "laser", "plasma", "shotgun"},
        }
        _ALL_SUBTYPES = sorted(set().union(*_SUBTYPES_BY_TYPE.values()))

        def _visible_subtypes():
            """Return subtypes matching the currently active type filters."""
            active = {t for t, (b, v) in self._fab_type_btns.items() if v.get()}
            if not active:
                return {s.title() for s in _ALL_SUBTYPES}
            result = set()
            for t in active:
                for s in _SUBTYPES_BY_TYPE.get(t, set()):
                    result.add(s.title())
            return result

        # TYPE filter (pills — only 3 items)
        self._section_label(fab_sb, "TYPE")
        type_fr = tk.Frame(fab_sb, bg=BG2)
        type_fr.pack(fill="x", **pad)
        self._fab_type_btns = {}
        for t in ["weapons", "armour", "ammo"]:
            var = tk.BooleanVar(value=False)
            btn = tk.Button(type_fr, text=t.title(), font=("Consolas", 8),
                            bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=6, pady=2)
            def _toggle_type(v=var, b=btn):
                v.set(not v.get())
                b.configure(bg="#1a3030" if v.get() else BG3,
                            fg=ACCENT if v.get() else FG_DIM)
                self._fab_on_filter_change()
            btn.configure(command=_toggle_type)
            btn.pack(side="left", padx=(0, 4), pady=2)
            self._fab_type_btns[t] = (btn, var)

        # ARMOR CLASS (pills — only 3)
        self._section_label(fab_sb, "ARMOR CLASS")
        ac_fr = tk.Frame(fab_sb, bg=BG2)
        ac_fr.pack(fill="x", **pad)
        self._fab_armor_class_btns = {}
        for val in ["Light", "Medium", "Heavy"]:
            var = tk.BooleanVar(value=False)
            btn = tk.Button(ac_fr, text=val, font=("Consolas", 8),
                            bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=6, pady=2)
            def _toggle_ac(v=var, b=btn):
                v.set(not v.get())
                b.configure(bg="#1a3030" if v.get() else BG3,
                            fg=ACCENT if v.get() else FG_DIM)
                self._fab_on_filter_change()
            btn.configure(command=_toggle_ac)
            btn.pack(side="left", padx=(0, 4), pady=2)
            self._fab_armor_class_btns[val] = (btn, var)

        # ARMOR SLOT (dropdown — 6 items)
        self._fab_armor_slot_vars = _check_dropdown(
            fab_sb, "ARMOR SLOT",
            ["Helmet", "Torso", "Arms", "Legs", "Backpack", "Undersuit"],
            self._fab_on_filter_change)

        # SUBTYPE (searchable dropdown, filtered by active type)
        self._fab_subtype_vars = _check_dropdown(
            fab_sb, "SUBTYPE",
            [s.title() for s in _ALL_SUBTYPES],
            self._fab_on_filter_change,
            searchable=True, visible_fn=_visible_subtypes)

        # MANUFACTURER (searchable dropdown — 20 items)
        self._fab_mfr_vars = _check_dropdown(
            fab_sb, "MANUFACTURER",
            ["BEH", "CCC", "CDS", "CLDA", "DOOM", "GEM", "GRIN",
             "GYS", "HDGW", "HDTC", "KAP", "KLA", "KSAR", "MIS",
             "RRS", "RSI", "SYFB", "THP", "UNKN", "VGL"],
            self._fab_on_filter_change, searchable=True)

        # MATERIAL (searchable dropdown — 30 items)
        self._fab_material_vars = _check_dropdown(
            fab_sb, "MATERIAL",
            ["Agricium", "Aluminum", "Aphorite", "Aslarite",
             "Beradom", "Beryl", "Carinite", "Copper", "Corundum",
             "Dolivine", "Gold", "Hadanite", "Hephaestanite",
             "Iron", "Janalite", "Laranite", "Lindinium", "Ouratite",
             "Quartz", "Riccite", "Sadaryx", "Saldynium (Ore)",
             "Savrilium", "Silicon", "Stileron", "Taranite",
             "Tin", "Titanium", "Torite", "Tungsten"],
            self._fab_on_filter_change, searchable=True)

        # Separator
        tk.Frame(self._fab_page, bg=BORDER, width=1).pack(side="left", fill="y")

        # ── Fabricator main area ──
        fab_main = tk.Frame(self._fab_page, bg=BG)
        fab_main.pack(side="left", fill="both", expand=True)

        # Count + stats bar
        fab_count_bar = tk.Frame(fab_main, bg=BG, height=28)
        fab_count_bar.pack(fill="x")
        fab_count_bar.pack_propagate(False)
        self._fab_count_var = tk.StringVar(value="Loading crafting data...")
        tk.Label(fab_count_bar, textvariable=self._fab_count_var,
                 font=("Consolas", 9), bg=BG, fg=FG_DIM, padx=10).pack(side="left")

        # Blueprint virtual scroll grid
        self._fab_vgrid = VirtualScrollGrid(
            fab_main, card_width=300, row_height=120,
            fill_fn=self._fill_fab_card,
            on_click_fn=self._on_fab_click,
            slot_class=_FabCardSlot, bg=BG)
        self._fab_vgrid.pack(fill="both", expand=True)
        self._fab_all_results = []

    def _fab_clear_filters(self):
        self._fab_search_var.set("")
        # Pill groups (type, armor class) — (btn, var) tuples
        for group in (self._fab_type_btns, self._fab_armor_class_btns):
            for k, (btn, var) in group.items():
                var.set(False)
                btn.configure(bg=BG3, fg=FG_DIM)
        # Checkbox dropdown groups — plain {key: BooleanVar} dicts
        for group in (self._fab_armor_slot_vars, self._fab_subtype_vars,
                      self._fab_mfr_vars, self._fab_material_vars):
            for k, var in group.items():
                var.set(False)
        self._fab_on_filter_change()

    def _fill_fab_card(self, slot, bp, idx):
        """Fill a _FabCardSlot with blueprint data — zero widget creation."""
        TYPE_COLORS = {"weapons": ORANGE, "armour": ACCENT, "ammo": YELLOW}

        name = self._data.get_blueprint_product_name(bp)
        bp_type = bp.get("type", "?")
        bp_sub = bp.get("subtype", "").replace("_", " ").title()
        tiers = bp.get("tiers", [])
        type_color = TYPE_COLORS.get(bp_type, FG)
        type_fg = "white" if bp_type != "ammo" else BG

        res_text = ""
        time_text = ""
        if tiers:
            tier = tiers[0]
            craft_time = tier.get("craftTimeSeconds", 0)
            resources = []
            for s in tier.get("slots", []):
                for opt in s.get("options", []):
                    rn = opt.get("resourceName", "")
                    qty = opt.get("quantity", 0)
                    if rn:
                        resources.append(f"{rn} x{qty}")
            if resources:
                res_text = "  |  ".join(resources[:3])
                if len(resources) > 3:
                    res_text += f"  +{len(resources)-3}"
            mins = craft_time // 60
            secs = craft_time % 60
            time_text = f"  {mins}m {secs}s" if mins else f"  {secs}s"

        slot.update(name, bp_type.title(), type_color, type_fg,
                    bp_sub, res_text, time_text)

    def _on_crafting_loaded(self):
        """Called when crafting data finishes loading."""
        if not self._data.crafting_loaded or not self._data.crafting_blueprints:
            ver = self._data.version or "?"
            is_live = "live" in ver.lower()
            if is_live:
                # Auto-switch to PTU if available
                has_ptu = any("ptu" in v.get("version", "").lower()
                              for v in self._data.available_versions)
                if has_ptu:
                    self._status_var.set("Fabricator requires PTU — switching...")
                    self._switch_version("ptu")
                    return
                msg = "Fabricator has no data on LIVE — switch to PTU for crafting blueprints"
            else:
                msg = "No crafting data available for this version"
            self._status_var.set(msg)
            if hasattr(self, "_fab_count_var"):
                self._fab_count_var.set(msg)
            return

        self._status_var.set("Ready")

        # Trigger initial display
        self._fab_on_filter_change()

    def _fab_on_filter_change(self):
        """Filter and display crafting blueprints."""
        if not self._data.crafting_loaded:
            return

        search = (self._fab_search_var.get() or "").lower()
        # Pill groups: (btn, var) tuples
        active_types = {t for t, (b, v) in self._fab_type_btns.items() if v.get()}
        active_armor_class = {k for k, (b, v) in self._fab_armor_class_btns.items() if v.get()}
        # Checkbox dropdown groups: plain {key: BooleanVar} dicts
        active_subtypes = {k.lower() for k, v in self._fab_subtype_vars.items() if v.get()}
        active_armor_slot = {k for k, v in self._fab_armor_slot_vars.items() if v.get()}
        active_mfr = {k for k, v in self._fab_mfr_vars.items() if v.get()}
        active_material = {k for k, v in self._fab_material_vars.items() if v.get()}

        results = []
        for bp in self._data.crafting_blueprints:
            prod = self._data.get_blueprint_product(bp)

            # Search
            if search:
                name = self._data.get_blueprint_product_name(bp).lower()
                tag = (bp.get("tag") or "").lower()
                if search not in name and search not in tag:
                    continue

            # Type filter
            if active_types and bp.get("type", "") not in active_types:
                continue

            # Subtype filter
            if active_subtypes and bp.get("subtype", "") not in active_subtypes:
                continue

            # Armor class filter (Light/Medium/Heavy from item attachSubType)
            if active_armor_class and prod:
                ast = (prod.get("attachSubType", "") or "").title()
                # Also check tags for class
                tags = (prod.get("tags", "") or "").lower()
                item_classes = set()
                if ast in ("Light", "Lightarmor"):
                    item_classes.add("Light")
                elif ast == "Medium":
                    item_classes.add("Medium")
                elif ast == "Heavy":
                    item_classes.add("Heavy")
                # Fallback: check tags
                if "light" in tags or "kap_light" in tags:
                    item_classes.add("Light")
                if "medium" in tags:
                    item_classes.add("Medium")
                if "heavy" in tags:
                    item_classes.add("Heavy")
                if not item_classes & active_armor_class:
                    continue
            elif active_armor_class and not prod:
                continue

            # Armor slot filter (Helmet/Torso/Arms/Legs/Backpack/Undersuit)
            if active_armor_slot:
                name_lower = self._data.get_blueprint_product_name(bp).lower()
                item_slot = ""
                if "helmet" in name_lower or "helm" in name_lower:
                    item_slot = "Helmet"
                elif "arms" in name_lower:
                    item_slot = "Arms"
                elif "legs" in name_lower:
                    item_slot = "Legs"
                elif "core" in name_lower or "torso" in name_lower:
                    item_slot = "Torso"
                elif "backpack" in name_lower:
                    item_slot = "Backpack"
                elif "undersuit" in name_lower:
                    item_slot = "Undersuit"
                if item_slot not in active_armor_slot:
                    continue

            # Manufacturer filter
            if active_mfr and prod:
                mfr_code = prod.get("manufacturerCode", "")
                if mfr_code not in active_mfr:
                    continue
            elif active_mfr and not prod:
                continue

            # Material filter (resources + items used in recipe)
            if active_material:
                tiers = bp.get("tiers", [])
                bp_materials = set()
                for tier in tiers:
                    for slot in tier.get("slots", []):
                        for opt in slot.get("options", []):
                            rn = opt.get("resourceName", "")
                            if rn:
                                bp_materials.add(rn)
                            itn = opt.get("itemName", "")
                            if itn:
                                bp_materials.add(itn)
                if not bp_materials & active_material:
                    continue

            results.append(bp)

        self._fab_all_results = results
        total = len(self._data.crafting_blueprints)
        shown = len(results)
        if hasattr(self, "_fab_count_var"):
            suffix = f" of {total}" if shown != total else ""
            self._fab_count_var.set(f"{shown}{suffix} Blueprints")
        self._fab_rebuild_grid()

    def _fab_rebuild_grid(self):
        """Push current results into the fabricator virtual scroll grid."""
        if hasattr(self, "_fab_vgrid"):
            self._fab_vgrid.set_data(self._fab_all_results)

    # ── Resources Page ────────────────────────────────────────────────────

    def _build_resources_page(self):
        """Build the resources/mining page with sidebar filters + location card grid."""
        self._res_page = tk.Frame(self._page_container, bg=BG)
        self._res_built = True

        # ── Sidebar ──
        sb_outer = tk.Frame(self._res_page, bg=BG2, width=220)
        sb_outer.pack(side="left", fill="y")
        sb_outer.pack_propagate(False)

        sb_canvas = tk.Canvas(sb_outer, bg=BG2, highlightthickness=0, width=210)
        sb_vbar = ttk.Scrollbar(sb_outer, orient="vertical", command=sb_canvas.yview)
        sb_vbar.pack(side="right", fill="y")
        sb_canvas.pack(fill="both", expand=True)
        sb_canvas.configure(yscrollcommand=sb_vbar.set)
        sb = tk.Frame(sb_canvas, bg=BG2)
        sb_win = sb_canvas.create_window((0, 0), window=sb, anchor="nw")
        sb.bind("<Configure>", lambda e: sb_canvas.configure(scrollregion=sb_canvas.bbox("all")))
        sb_canvas.bind("<Configure>", lambda e, c=sb_canvas, w=sb_win: c.itemconfig(w, width=e.width))
        sb_canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(sb_canvas))

        pad = {"padx": 8, "pady": (0, 2)}

        # Header
        hdr = tk.Frame(sb, bg=BG2)
        hdr.pack(fill="x", padx=8, pady=(8, 4))
        tk.Label(hdr, text="FILTERS", font=("Consolas", 10, "bold"),
                 bg=BG2, fg=FG).pack(side="left")
        tk.Button(hdr, text="Clear", font=("Consolas", 7), bg=BG2, fg=FG_DIM,
                  relief="flat", bd=0, cursor="hand2",
                  command=self._res_clear_filters).pack(side="right")

        # Search
        self._section_label(sb, "SEARCH")
        self._res_search_var = tk.StringVar()
        self._res_search_var.trace_add("write", lambda *_: self._res_on_filter_change())
        tk.Entry(sb, textvariable=self._res_search_var, font=("Consolas", 9),
                 bg=BG3, fg=FG, insertbackground=FG, relief="flat",
                 highlightthickness=1, highlightcolor=BORDER,
                 highlightbackground=BORDER).pack(fill="x", **pad)

        # System filter
        self._section_label(sb, "SYSTEM")
        sys_fr = tk.Frame(sb, bg=BG2)
        sys_fr.pack(fill="x", **pad)
        self._res_system_btns = {}
        for s in ["Stanton", "Pyro", "Nyx"]:
            var = tk.BooleanVar(value=False)
            btn = tk.Button(sys_fr, text=s, font=("Consolas", 8),
                            bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=6, pady=2)
            def _toggle(v=var, b=btn):
                v.set(not v.get())
                b.configure(bg="#1a3030" if v.get() else BG3,
                            fg=ACCENT if v.get() else FG_DIM)
                self._res_on_filter_change()
            btn.configure(command=_toggle)
            btn.pack(side="left", padx=(0, 4), pady=2)
            self._res_system_btns[s] = (btn, var)

        # Location Type filter
        self._section_label(sb, "LOCATION TYPE")
        lt_fr = tk.Frame(sb, bg=BG2)
        lt_fr.pack(fill="x", **pad)
        self._res_loctype_btns = {}
        for lt in ["Planet", "Moon", "Belt", "Lagrange", "Cluster", "Event", "Special"]:
            var = tk.BooleanVar(value=False)
            btn = tk.Button(lt_fr, text=lt, font=("Consolas", 7),
                            bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=4, pady=1)
            def _toggle_lt(v=var, b=btn):
                v.set(not v.get())
                b.configure(bg="#1a3030" if v.get() else BG3,
                            fg=ACCENT if v.get() else FG_DIM)
                self._res_on_filter_change()
            btn.configure(command=_toggle_lt)
            btn.pack(side="left", padx=(0, 3), pady=1)
            self._res_loctype_btns[lt.lower()] = (btn, var)

        # Mining Type filter
        self._section_label(sb, "DEPOSIT TYPE")
        dt_fr = tk.Frame(sb, bg=BG2)
        dt_fr.pack(fill="x", **pad)
        self._res_deptype_btns = {}
        _dt_colors = {
            "SpaceShip_Mineables":     ("#0a1a2a", ACCENT),
            "FPS_Mineables":           ("#1a0a1a", PURPLE),
            "GroundVehicle_Mineables": ("#1a1a0a", ORANGE),
            "Harvestables":            ("#0a1a0a", GREEN),
        }
        for key, info in [("SpaceShip_Mineables", "Ship"),
                           ("FPS_Mineables", "FPS"),
                           ("GroundVehicle_Mineables", "ROC"),
                           ("Harvestables", "Harvest")]:
            var = tk.BooleanVar(value=False)
            active_bg, active_fg = _dt_colors.get(key, ("#1a3030", ACCENT))
            btn = tk.Button(dt_fr, text=info, font=("Consolas", 8),
                            bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=6, pady=2)
            def _toggle_dt(v=var, b=btn, abg=active_bg, afg=active_fg):
                v.set(not v.get())
                b.configure(bg=abg if v.get() else BG3,
                            fg=afg if v.get() else FG_DIM)
                self._res_on_filter_change()
            btn.configure(command=_toggle_dt)
            btn.pack(side="left", padx=(0, 3), pady=2)
            self._res_deptype_btns[key] = (btn, var)

        # Resources filter (checkbox dropdown with search)
        # Reuse the same _check_dropdown pattern as the fabricator page
        self._res_selected_resources = set()
        self._res_resource_values = []  # populated on data load
        self._res_resource_vars = {}    # populated on data load

        def _res_check_dropdown(parent_frame, section_name, on_change, searchable=True):
            """Build a checkbox dropdown button for resource filtering."""
            self._section_label(parent_frame, section_name)
            display_var = tk.StringVar(value="All")
            trigger_btn = tk.Button(parent_frame, textvariable=display_var,
                                    font=("Consolas", 8), bg=BG3, fg=FG_DIM,
                                    relief="flat", bd=0, cursor="hand2",
                                    anchor="w", padx=8, pady=3)
            trigger_btn.pack(fill="x", **pad)

            def _update_label():
                sel = [k for k, v in self._res_resource_vars.items() if v.get()]
                self._res_selected_resources = set(sel)
                if not sel:
                    display_var.set("All")
                    trigger_btn.configure(fg=FG_DIM)
                elif len(sel) <= 2:
                    display_var.set(", ".join(sel))
                    trigger_btn.configure(fg=ACCENT)
                else:
                    display_var.set(f"{len(sel)} selected")
                    trigger_btn.configure(fg=ACCENT)
                on_change()

            def _show_popup():
                # Destroy any existing popup to prevent stacking on rapid clicks
                if hasattr(self, '_res_popup') and self._res_popup and self._res_popup.winfo_exists():
                    self._res_popup.destroy()
                # Filter resource list by active deposit types
                active_deptypes = {t for t, (b, v) in self._res_deptype_btns.items() if v.get()}
                if active_deptypes and self._data.mining_loaded:
                    # Only show resources found in locations that have the selected deposit types
                    visible_res = set()
                    hidden = MissionDataManager.HIDDEN_LOCATIONS
                    for loc in self._data.mining_locations:
                        loc_name = loc.get("locationName", "")
                        if loc_name in hidden:
                            continue
                        group_names = {g.get("groupName", "") for g in loc.get("groups", [])}
                        if not group_names.intersection(active_deptypes):
                            continue
                        # Add resources from matching groups only
                        for g in loc.get("groups", []):
                            gn = g.get("groupName", "")
                            if gn not in active_deptypes:
                                continue
                            for dep in g.get("deposits", []):
                                # Harvestables use presetName
                                pn = dep.get("presetName", "")
                                if pn:
                                    visible_res.add(pn)
                                # Mining uses compositions
                                comp = self._data.mining_compositions.get(
                                    dep.get("compositionGuid", ""), {})
                                for part in comp.get("parts", []):
                                    en = part.get("elementName", "")
                                    if en:
                                        visible_res.add(en)
                    values = sorted(visible_res)
                else:
                    values = self._res_resource_values
                if not values:
                    return
                popup = tk.Toplevel(self.root)
                self._res_popup = popup
                popup.overrideredirect(True)
                popup.configure(bg=BG3, highlightbackground=BORDER,
                                highlightthickness=1)
                popup.attributes("-topmost", True)

                bx = trigger_btn.winfo_rootx()
                by = trigger_btn.winfo_rooty() + trigger_btn.winfo_height()
                popup_w = max(trigger_btn.winfo_width(), 200)
                search_h = 28 if searchable else 0
                max_h = min(350, len(values) * 22 + 8 + search_h)
                popup.geometry(f"{popup_w}x{max_h}+{bx}+{by}")

                search_var = tk.StringVar()
                if searchable:
                    sf = tk.Frame(popup, bg=BG4)
                    sf.pack(fill="x")
                    se = tk.Entry(sf, textvariable=search_var, font=("Consolas", 8),
                                  bg=BG4, fg=FG, insertbackground=FG, relief="flat",
                                  highlightthickness=0)
                    se.pack(fill="x", padx=4, pady=3)
                    se.focus_set()

                canvas = tk.Canvas(popup, bg=BG3, highlightthickness=0)
                vbar = ttk.Scrollbar(popup, orient="vertical", command=canvas.yview)
                vbar.pack(side="right", fill="y")
                canvas.pack(fill="both", expand=True)
                canvas.configure(yscrollcommand=vbar.set)
                inner = tk.Frame(canvas, bg=BG3)
                win_id = canvas.create_window((0, 0), window=inner, anchor="nw")
                inner.bind("<Configure>",
                           lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
                canvas.bind("<Configure>",
                            lambda e, c=canvas, w=win_id: c.itemconfig(w, width=e.width))
                canvas.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))

                # Ensure vars exist for all values
                for v in values:
                    if v not in self._res_resource_vars:
                        self._res_resource_vars[v] = tk.BooleanVar(value=False)

                check_widgets = []

                def _build_checks(q=""):
                    for w in inner.winfo_children():
                        w.destroy()
                    check_widgets.clear()
                    q = q.lower()
                    for val in values:
                        if q and q not in val.lower():
                            continue
                        var = self._res_resource_vars[val]
                        cb = tk.Checkbutton(
                            inner, text=val, variable=var,
                            font=("Consolas", 8), bg=BG3, fg=FG,
                            selectcolor=BG4, activebackground=BG3,
                            activeforeground=ACCENT, anchor="w",
                            command=_update_label)
                        cb.pack(fill="x", padx=4)
                        cb.bind("<MouseWheel>", MissionDBApp._mw_scroll(canvas))
                        check_widgets.append(cb)

                _build_checks()
                _debounce_id = [None]
                if searchable:
                    def _debounced_rebuild(*_):
                        if _debounce_id[0] is not None:
                            try:
                                self.root.after_cancel(_debounce_id[0])
                            except Exception:
                                pass
                        # 300ms debounce — _build_checks destroys/recreates widgets each time
                        _debounce_id[0] = self.root.after(300, lambda: _build_checks(search_var.get()))
                    search_var.trace_add("write", _debounced_rebuild)

                _close_bind_id = [None]

                def _close(e=None):
                    try:
                        if _close_bind_id[0] is not None:
                            self.root.unbind("<Button-1>", _close_bind_id[0])
                            _close_bind_id[0] = None
                        if popup.winfo_exists():
                            popup.destroy()
                    except Exception:
                        pass

                def _on_click_outside(e):
                    try:
                        if not popup.winfo_exists():
                            _close()
                            return
                        # Check if click is inside the popup
                        px, py = popup.winfo_rootx(), popup.winfo_rooty()
                        pw, ph = popup.winfo_width(), popup.winfo_height()
                        if px <= e.x_root <= px + pw and py <= e.y_root <= py + ph:
                            return  # click inside popup — don't close
                        _close()
                    except Exception:
                        _close()
                _close_bind_id[0] = self.root.bind("<Button-1>", _on_click_outside, add="+")
                popup.bind("<Destroy>", lambda e: _close() if e.widget == popup else None, add="+")

            trigger_btn.configure(command=_show_popup)
            return trigger_btn, display_var

        self._res_resource_btn, self._res_resource_display_var = _res_check_dropdown(
            sb, "RESOURCES", self._res_on_filter_change, searchable=True)

        # Match mode (Any/All)
        match_fr = tk.Frame(sb, bg=BG2)
        match_fr.pack(fill="x", **pad)
        self._res_match_mode = tk.StringVar(value="any")
        for mode, label in [("any", "Any"), ("all", "All")]:
            tk.Radiobutton(match_fr, text=label, variable=self._res_match_mode,
                           value=mode, font=("Consolas", 8),
                           bg=BG2, fg=FG_DIM, selectcolor=BG3,
                           activebackground=BG2, activeforeground=ACCENT,
                           indicatoron=True, command=self._res_on_filter_change).pack(
                               side="left", padx=(0, 8))

        # Separator
        tk.Frame(self._res_page, bg=BORDER, width=1).pack(side="left", fill="y")

        # ── Main area: location cards ──
        res_main = tk.Frame(self._res_page, bg=BG)
        res_main.pack(side="left", fill="both", expand=True)

        # Count bar
        count_bar = tk.Frame(res_main, bg=BG, height=28)
        count_bar.pack(fill="x")
        count_bar.pack_propagate(False)
        self._res_count_var = tk.StringVar(value="Loading resource data...")
        tk.Label(count_bar, textvariable=self._res_count_var, font=("Consolas", 9),
                 bg=BG, fg=FG_DIM, padx=10).pack(side="left")

        # Location card grid (virtual scroll)
        self._res_vgrid = VirtualScrollGrid(
            res_main, card_width=320, row_height=280,
            fill_fn=self._res_fill_card,
            on_click_fn=self._on_resource_click)
        self._res_vgrid.pack(fill="both", expand=True)
        self._res_all_results = []

    def _res_clear_filters(self):
        self._res_search_var.set("")
        for key, (btn, var) in self._res_system_btns.items():
            var.set(False)
            btn.configure(bg=BG3, fg=FG_DIM)
        for key, (btn, var) in self._res_loctype_btns.items():
            var.set(False)
            btn.configure(bg=BG3, fg=FG_DIM)
        for key, (btn, var) in self._res_deptype_btns.items():
            var.set(False)
            btn.configure(bg=BG3, fg=FG_DIM)
        self._res_selected_resources = set()
        # Clear all resource vars and reset the dropdown button text
        for k, v in self._res_resource_vars.items():
            v.set(False)
        if hasattr(self, "_res_resource_display_var"):
            self._res_resource_display_var.set("All")
        if hasattr(self, "_res_resource_btn") and self._res_resource_btn:
            try:
                self._res_resource_btn.configure(fg=FG_DIM)
            except Exception:
                pass
        self._res_match_mode.set("any")
        self._res_on_filter_change()

    def _res_on_filter_change(self):
        """Filter locations and rebuild the grid."""
        if not self._data.mining_loaded:
            return

        search = (self._res_search_var.get() or "").lower()
        active_systems = {s for s, (b, v) in self._res_system_btns.items() if v.get()}
        active_loctypes = {t for t, (b, v) in self._res_loctype_btns.items() if v.get()}
        active_deptypes = {t for t, (b, v) in self._res_deptype_btns.items() if v.get()}
        selected_res = self._res_selected_resources
        match_mode = self._res_match_mode.get()

        # Hidden locations (matching scmdb)
        hidden = MissionDataManager.HIDDEN_LOCATIONS

        results = []
        for loc in self._data.mining_locations:
            loc_name = loc.get("locationName", "")
            if loc_name in hidden:
                continue

            system = loc.get("system", "")
            loc_type = loc.get("locationType", "")

            # System filter
            if active_systems and system not in active_systems:
                continue

            # Location type filter
            if active_loctypes and loc_type not in active_loctypes:
                continue

            # Deposit type filter
            if active_deptypes:
                group_names = {g.get("groupName", "") for g in loc.get("groups", [])}
                if not group_names.intersection(active_deptypes):
                    continue

            # Get resources at this location
            loc_resources = self._data.get_location_resources(loc_name)
            resource_names = {r["resource"] for r in loc_resources}

            # Resource filter
            if selected_res:
                if match_mode == "all":
                    if not selected_res.issubset(resource_names):
                        continue
                else:  # any
                    if not selected_res.intersection(resource_names):
                        continue

            # Search filter (match location name or resource names)
            if search:
                all_text = loc_name.lower() + " " + " ".join(r.lower() for r in resource_names)
                if search not in all_text:
                    continue

            results.append(loc)

        self._res_all_results = results
        total = len([l for l in self._data.mining_locations
                     if l.get("locationName", "") not in hidden])
        shown = len(results)
        suffix = f" of {total}" if shown != total else ""
        self._res_count_var.set(f"{shown}{suffix} Locations  ·  {len(self._data.all_resource_names)} Resources")
        if hasattr(self, "_res_vgrid"):
            self._res_vgrid.set_data(results)

    def _res_fill_card(self, slot, loc, idx):
        """Fill a VirtualScrollGrid card slot with location data."""
        loc_name = loc.get("locationName", "?")
        system = loc.get("system", "")
        loc_type = loc.get("locationType", "")

        # System color
        sys_colors = {
            "Stanton": (ACCENT, "#0a2020"),
            "Pyro": (ORANGE, "#2a1a0a"),
            "Nyx": (PURPLE, "#1a0a2a"),
        }
        sys_fg, sys_bg = sys_colors.get(system, (FG_DIM, BG3))

        # Build tags
        tags = [(system, sys_bg, sys_fg, True)]

        # Location type badge with color
        LOC_TYPE_COLORS = {
            "planet":   ("#0a1a20", "#55bbaa"),
            "moon":     ("#0a1520", "#7799bb"),
            "belt":     ("#1a1a0a", "#bbaa55"),
            "lagrange": ("#0a0a1a", "#8888cc"),
            "cluster":  ("#1a0a1a", "#aa77bb"),
            "event":    ("#1a1a0a", YELLOW),
            "special":  ("#1a0a0a", RED),
            "cave":     ("#1a1510", "#aa8866"),
        }
        type_labels = {"planet": "Planet", "moon": "Moon", "belt": "Belt",
                       "lagrange": "Lagrange", "cluster": "Cluster",
                       "event": "Event", "special": "Special", "cave": "Cave"}
        type_label = type_labels.get(loc_type, loc_type.title())
        lt_bg, lt_fg = LOC_TYPE_COLORS.get(loc_type, (BG3, FG_DIM))
        tags.append((type_label, lt_bg, lt_fg, True))

        # Group type badges with distinct colors
        GROUP_COLORS = {
            "SpaceShip_Mineables":       ("#0a1a2a", ACCENT),       # teal
            "SpaceShip_Mineables_Rare":  ("#1a1a0a", YELLOW),       # gold
            "FPS_Mineables":             ("#1a0a1a", PURPLE),        # purple
            "GroundVehicle_Mineables":   ("#1a1a0a", ORANGE),        # orange
            "Harvestables":              ("#0a1a0a", GREEN),          # green
            "Salvage_FreshDerelicts":    ("#1a0a0a", RED),            # red
            "Salvage_BrokenShips_Poor":  ("#1a0a0a", "#cc6644"),      # rust
            "Salvage_BrokenShips_Normal":("#1a0a0a", "#cc6644"),
            "Salvage_BrokenShips_Elite": ("#1a0a0a", "#cc6644"),
        }
        groups = loc.get("groups", [])
        for g in groups[:3]:
            gn = g.get("groupName", "")
            gt_info = self._data.MINING_GROUP_TYPES.get(gn, {})
            if gt_info:
                bg_c, fg_c = GROUP_COLORS.get(gn, (BG3, FG_DIM))
                tags.append((gt_info["short"], bg_c, fg_c, True))

        # Resources — build compact list for the extra field
        resources = self._data.get_location_resources(loc_name)
        lines = []
        for r in resources[:8]:
            pct = f"{r['max_pct']:.0f}%" if r['max_pct'] else ""
            name = r["resource"]
            for suffix in [" (Ore)", " (Raw)", " (Gem)"]:
                name = name.replace(suffix, "")
            lines.append(f"{name:<16s} {pct:>5s}")
        if len(resources) > 8:
            lines.append(f"+{len(resources)-8} more")
        extra = "\n".join(lines)

        reward_text = f"{len(resources)} resources"
        reward_color = GREEN if resources else FG_DIM

        initials = loc_name[:2].upper()

        slot.update(loc_name, initials, system, tags, reward_text, reward_color, extra=extra)

    def _on_mining_loaded(self):
        """Called when mining data finishes loading."""
        if not self._data.mining_loaded:
            self._status_var.set("Mining data not available for this version")
            if hasattr(self, "_res_count_var"):
                self._res_count_var.set("No mining data for this version")
            return

        self._status_var.set("Ready")

        # Populate resource dropdown values
        self._res_resource_values = sorted(self._data.all_resource_names)

        # Initial filter
        self._res_on_filter_change()

    # ── Data loaded callback ──

    def _on_data_loaded(self):
        if self._data.error:
            self._status_var.set(f"Error: {self._data.error}")
            return

        self._status_var.set("Ready")
        self._version_var.set(self._data.version)

        # Update LIVE/PTU toggle styling
        ver_lower = self._data.version.lower()
        if "ptu" in ver_lower:
            self._active_channel = "ptu"
        else:
            self._active_channel = "live"
        self._update_ver_btn_style()

        # Populate filter dropdowns
        types = ["All types"] + self._data.all_mission_types
        self._type_combo["values"] = types
        self._type_combo.current(0)

        factions = ["All factions"] + self._data.all_faction_names
        self._faction_combo["values"] = factions
        self._faction_combo.current(0)

        # Set reward range
        if self._data.min_reward:
            self._reward_min_var.set(str(self._data.min_reward))
        if self._data.max_reward:
            self._reward_max_var.set(str(self._data.max_reward))

        # Initial display
        self._on_filter_change()

        # If fabricator tab is active, auto-load crafting data
        if self._current_page == "fabricator":
            self._data.load_crafting(
                on_done=lambda: self.root.after(0, self._on_crafting_loaded))

        # Schedule periodic auto-refresh (every 30 minutes)
        self._schedule_auto_refresh()

    def _schedule_auto_refresh(self):
        """Check for data updates every 30 minutes."""
        AUTO_REFRESH_MS = 30 * 60 * 1000  # 30 minutes

        def _check():
            _loaded = self._data.is_data_loaded()
            _loading = self._data.is_data_loading()
            if not _loaded or _loading:
                self.root.after(AUTO_REFRESH_MS, _check)
                return

            def _do_check():
                fresh = self._data._fetch_versions()
                if not fresh:
                    return
                # Find the version matching current channel
                channel = self._active_channel
                new_ver = None
                for v in fresh:
                    if channel in v.get("version", "").lower():
                        new_ver = v.get("version", "")
                        break
                if new_ver and new_ver != self._data.version:
                    # New data available — update versions on main thread, then reload
                    def _apply():
                        self._data.available_versions = fresh
                        self._status_var.set(f"Update found: {new_ver} — refreshing...")
                    self.root.after(0, _apply)
                    self._data.load_version(
                        new_ver,
                        on_done=lambda: self.root.after(0, self._on_data_loaded))
                else:
                    self.root.after(0, self._status_var.set, "Ready (up to date)")

            threading.Thread(target=_do_check, daemon=True).start()
            self.root.after(AUTO_REFRESH_MS, _check)

        self.root.after(AUTO_REFRESH_MS, _check)

    def _update_ver_btn_style(self):
        """Update LIVE/PTU toggle button colors."""
        for key, btn in self._ver_btns.items():
            if key == self._active_channel:
                if key == "live":
                    btn.configure(bg="#1a3020", fg=GREEN,
                                  activebackground="#1a3020", activeforeground=GREEN)
                else:
                    btn.configure(bg="#1a2040", fg=YELLOW,
                                  activebackground="#1a2040", activeforeground=YELLOW)
            else:
                btn.configure(bg=BG3, fg=FG_DIM,
                              activebackground=BG3, activeforeground=FG_DIM)

    def _switch_version(self, channel: str):
        """Switch between LIVE and PTU data."""
        if channel == self._active_channel:
            return  # already on this version
        if self._data.loading:
            return  # still loading

        self._active_channel = channel
        self._update_ver_btn_style()
        self._status_var.set(f"Loading {channel.upper()} data...")

        # Clear current display
        self._vgrid.set_data([])
        if hasattr(self, "_fab_vgrid"):
            self._fab_vgrid.set_data([])
            self._data.set_crafting_loaded(False)

        def _do_switch():
            # Re-fetch versions.json to get the LATEST file names
            # (PTU builds change frequently)
            fresh = self._data._fetch_versions()
            if fresh:
                # Update available_versions on the main thread
                self.root.after(0, lambda: setattr(self._data, "available_versions", fresh))

            target_ver = None
            versions_to_check = fresh if fresh is not None else self._data.available_versions
            for v in versions_to_check:
                ver = v.get("version", "")
                if channel.lower() in ver.lower():
                    target_ver = ver
                    break

            if not target_ver:
                self.root.after(0, self._status_var.set,
                                f"No {channel.upper()} version available")
                return

            self.root.after(0, self._version_var.set, f"→ {target_ver}")
            self._data.load_version(
                target_ver,
                on_done=lambda: self.root.after(0, self._on_data_loaded),
            )

        threading.Thread(target=_do_switch, daemon=True).start()

    # ── JSONL command watcher ──

    def _start_cmd_watcher(self):
        if not self.cmd_file or self.cmd_file == "NUL":
            return
        t = threading.Thread(target=self._watch_cmds, daemon=True)
        t.start()

    def _watch_cmds(self):
        offset = 0
        _err_count = 0
        while True:
            try:
                commands, offset = ipc_read_incremental(self.cmd_file, offset)
                for cmd in commands:
                    if isinstance(cmd, dict):
                        self.root.after(0, self._dispatch, cmd)
                _err_count = 0
            except Exception as e:
                _err_count += 1
                log.warning("_watch_cmds error (%d): %s", _err_count, e)
                time.sleep(min(0.2 * (2 ** min(_err_count, 5)), 10))
                continue
            time.sleep(0.2)

    def _dispatch(self, cmd: dict):
        t = cmd.get("type", "")
        if t == "quit":
            self.root.destroy()
            sys.exit(0)
        elif t == "show":
            self.root.deiconify()
            self.root.lift()
        elif t == "hide":
            self.root.withdraw()
        elif t == "refresh":
            self._status_var.set("Refreshing...")
            try:
                os.remove(CACHE_FILE)
            except Exception:
                pass
            self._data.set_loaded(False)
            self._data.load(on_done=lambda: self.root.after(0, self._on_data_loaded))
        elif t == "search":
            query = cmd.get("query", "")
            if self._search_var:
                self._search_var.set(query)
        elif t == "filter":
            if "category" in cmd:
                cat = cmd["category"].lower()
                if cat in self._category_btns:
                    btn, var = self._category_btns[cat]
                    var.set(True)
                    btn.configure(bg="#1a3030", fg=ACCENT)
            if "system" in cmd:
                s = cmd["system"]
                if s in self._system_btns:
                    btn, var = self._system_btns[s]
                    var.set(True)
                    btn.configure(bg="#1a3030", fg=ACCENT)
            if "mission_type" in cmd:
                self._type_var.set(cmd["mission_type"])
            if "legality" in cmd:
                self._legality_var.set(cmd["legality"])
            self._on_filter_change()

    def run(self):
        self.root.mainloop()


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

def main():
    from shared.data_utils import parse_cli_args
    from shared.platform_utils import set_dpi_awareness

    set_dpi_awareness()

    d = parse_cli_args(sys.argv[1:], defaults={"w": 1300, "h": 800})
    app = MissionDBApp(d["x"], d["y"], d["w"], d["h"], d["opacity"],
                       d["cmd_file"] or "NUL")
    app.run()


if __name__ == "__main__":
    main()
