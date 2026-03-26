# Market Finder — powered by uexcorp.space API v2

import tkinter as tk
from tkinter import ttk
import threading
import json
import logging
import os
import sys
import time
import webbrowser
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from shared.data_utils import retry_request, parse_cli_args
from shared.ipc import ipc_read_and_clear
from shared.platform_utils import set_dpi_awareness

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Color constants
# ---------------------------------------------------------------------------
BG = "#0d1117"
BG2 = "#111827"
BG3 = "#161b25"
BG4 = "#1c2233"
BORDER = "#252e42"
FG = "#c8d4e8"
FG_DIM = "#5a6480"
FG_DIMMER = "#3a4460"
ACCENT = "#44aaff"
GREEN = "#33dd88"
YELLOW = "#ffaa22"
RED = "#ff5533"
ORANGE = "#ff7733"
CYAN = "#33ccdd"
PURPLE = "#aa66ff"
HEADER_BG = "#0e1420"
SECT_HDR_BG = "#131928"
ROW_EVEN = "#161b25"
ROW_ODD = "#1c2233"
ROW_HOVER = "#1e2840"
ROW_SEL = "#1e3050"
CAT_COLORS = {
    "Armor": "#336699",
    "Weapons": "#993333",
    "Ship Weapons": "#883322",
    "Missiles": "#aa3344",
    "Clothing": "#336633",
    "Sustenance": "#886622",
    "Ship Components": "#334488",
    "Utility": "#554433",
    "Misc": "#443355",
    "Ships": "#335566",
    "Rentals": "#335544",
}

SECTION_TO_TAB = {
    "Armor": "Armor",
    "Clothing": "Clothing",
    "Undersuits": "Clothing",
    "Personal Weapons": "Weapons",
    "Vehicle Weapons": "Ship Weapons",
    "Systems": "Ship Components",
    "Utility": "Utility",
    "Liveries": "Misc",
    "Miscellaneous": "Misc",
    "Other": "Misc",
    "Commodities": "Misc",
    "General": "Misc",
}

# Categories that go under the Missiles tab instead of Ship Weapons
MISSILE_CATEGORIES = {"Missiles", "Missile Racks"}

# Categories that go under the Sustenance tab
SUSTENANCE_CATEGORIES = {"Foods", "Drinks"}

TAB_DEFS = [
    ("\U0001f50d", "All"),
    ("\U0001f6e1", "Armor"),
    ("\U0001f52b", "Weapons"),
    ("\U0001f4a5", "Ship Weapons"),
    ("\U0001f3af", "Missiles"),
    ("\U0001f455", "Clothing"),
    ("\u2699", "Ship Components"),
    ("\U0001f527", "Utility"),
    ("\U0001f356", "Sustenance"),
    ("\U0001f4e6", "Misc"),
    ("\U0001f6f8", "Ships"),
    ("\U0001f680", "Rentals"),
]

def _item_tab(item: dict) -> str:
    """Get the tab name for an item, handling the Missiles and Sustenance splits."""
    cat = item.get("category", "")
    if cat in SUSTENANCE_CATEGORIES:
        return "Sustenance"
    tab = SECTION_TO_TAB.get(item.get("section", ""), "Misc")
    if tab == "Ship Weapons" and cat in MISSILE_CATEGORIES:
        tab = "Missiles"
    return tab

FONT = "Consolas"
ROW_H = 26
API_BASE = "https://api.uexcorp.space/2.0"
CACHE_TTL = 3600  # 1 hour
AUTO_REFRESH_MS = 3600 * 1000  # auto-refresh every 1 hour
CACHE_VERSION = 2
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".uex_cache.json")

HEADERS = {
    "User-Agent": "MarketFinder/1.0",
    "Accept": "application/json",
}


# ---------------------------------------------------------------------------
# DataManager — threaded fetch with disk cache
# ---------------------------------------------------------------------------
class DataManager:
    def __init__(self):
        self.items: list[dict] = []
        self.vehicles: list[dict] = []
        self.rentals: list[dict] = []
        self.terminals: dict[int, dict] = {}
        self.item_prices: dict[int, list[dict]] = {}
        self._fetching_prices: set[int] = set()
        self.rental_by_vehicle: dict[int, list[dict]] = {}
        self.vehicle_purchases: list[dict] = []
        self.purchase_by_vehicle: dict[int, list[dict]] = {}
        self.loaded = False
        self.progress = ""
        self.error = ""
        self.cache_ttl = CACHE_TTL
        self._lock = threading.Lock()
        self._search_index: dict[int, str] = {}

    # -- API helpers --------------------------------------------------------
    @staticmethod
    def _get(endpoint: str) -> list[dict]:
        url = f"{API_BASE}/{endpoint}"
        req = urllib.request.Request(url, headers=HEADERS)
        try:
            def _do_request():
                with urllib.request.urlopen(req, timeout=30) as resp:
                    body = json.loads(resp.read().decode())
                    if body.get("status") == "ok" and isinstance(body.get("data"), list):
                        return body["data"]
                    return []
            return retry_request(_do_request, retries=1)
        except Exception as exc:
            log.warning("API error for %s: %s", endpoint, exc)
            return []

    # -- Cache --------------------------------------------------------------
    def _load_cache(self) -> bool:
        if not os.path.exists(CACHE_FILE):
            return False
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            if cache.get("version") != CACHE_VERSION:
                return False
            if time.time() - cache.get("timestamp", 0) > self.cache_ttl:
                return False
            items = cache.get("items")
            if not isinstance(items, list):
                return False  # Invalid cache schema
            # Validate structure of a sample of items
            for sample_item in items[:min(5, len(items))]:
                if not isinstance(sample_item, dict):
                    return False  # Invalid item format
            vehicles = cache.get("vehicles", [])
            if not isinstance(vehicles, list):
                return False
            rentals = cache.get("rentals", [])
            if not isinstance(rentals, list):
                return False
            vehicle_purchases = cache.get("vehicle_purchases", [])
            if not isinstance(vehicle_purchases, list):
                return False
            terminals_raw = cache.get("terminals", {})
            terminals = {int(k): v for k, v in terminals_raw.items()}
            with self._lock:
                self.items = items
                self.vehicles = vehicles
                self.rentals = rentals
                self.vehicle_purchases = vehicle_purchases
                self.terminals = terminals
                self._index_rentals()
                self._index_purchases()
                self._index_items_by_tab()
                self.loaded = True
            return True
        except Exception as exc:
            log.warning("Cache load failed: %s", exc)
            return False

    def _save_cache(self):
        try:
            cache = {
                "version": CACHE_VERSION,
                "timestamp": time.time(),
                "items": self.items,
                "vehicles": self.vehicles,
                "rentals": self.rentals,
                "vehicle_purchases": self.vehicle_purchases,
                "terminals": {str(k): v for k, v in self.terminals.items()},
            }
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
        except Exception as exc:
            print(f"[MarketFinder] Cache save failed: {exc}")

    def _index_rentals(self):
        self.rental_by_vehicle = {}
        for r in self.rentals:
            vid = r.get("id_vehicle")
            if vid is not None:
                self.rental_by_vehicle.setdefault(vid, []).append(r)

    def _index_purchases(self):
        self.purchase_by_vehicle = {}
        for p in self.vehicle_purchases:
            vid = p.get("id_vehicle")
            if vid is not None:
                self.purchase_by_vehicle.setdefault(vid, []).append(p)

    def _index_items_by_tab(self):
        """Pre-index items by tab name for instant category switching."""
        self.items_by_tab = {"All": self.items}
        for item in self.items:
            tab = _item_tab(item)
            self.items_by_tab.setdefault(tab, []).append(item)
        # Pre-build lowercase search fields in a separate index (thread-safe, no in-place mutation)
        search_idx = {}
        for item in self.items:
            item_id = item.get("id")
            if item_id is not None:
                search_idx[item_id] = " ".join([
                    (item.get("name") or "").lower(),
                    (item.get("category") or "").lower(),
                    (item.get("company_name") or "").lower(),
                    (item.get("section") or "").lower(),
                ])
        self._search_index = search_idx

    # -- Fetch all data -----------------------------------------------------
    def fetch_all(self, force: bool = False):
        if not force and self._load_cache():
            with self._lock:
                self.progress = "Loaded from cache"
            return

        try:
            # Terminals
            with self._lock:
                self.progress = "Fetching terminals..."
            terminals_raw = self._get("terminals")
            term_map = {}
            for t in terminals_raw:
                tid = t.get("id")
                if tid is not None:
                    term_map[tid] = t

            # Vehicles
            with self._lock:
                self.progress = "Fetching vehicles..."
            vehicles = self._get("vehicles")

            # UEX categories 1-90; many are empty. Consider using a manifest endpoint.
            # For now, the ThreadPoolExecutor(max_workers=8) limits concurrency.
            all_items = []
            with self._lock:
                self.progress = "Fetching items (parallel)..."
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(self._get, f"items?id_category={i}"): i for i in range(1, 91)}
                for f in as_completed(futures):
                    try:
                        result = f.result()
                        if result:
                            all_items.extend(result)
                    except Exception as e:
                        log.warning("Category fetch failed: %s", e)

            # Rentals
            with self._lock:
                self.progress = "Fetching rentals..."
            rentals = self._get("vehicles_rentals_prices_all")

            # Vehicle purchase locations
            with self._lock:
                self.progress = "Fetching ship purchase prices..."
            vehicle_purchases = self._get("vehicles_purchases_prices_all")

            # Assign all data inside lock for thread safety
            with self._lock:
                self.terminals = term_map
                self.vehicles = vehicles
                self.items = all_items
                self.rentals = rentals
                self.vehicle_purchases = vehicle_purchases
                self._index_rentals()
                self._index_purchases()
                self._index_items_by_tab()
                self.loaded = True
                self.progress = f"Loaded {len(self.items)} items, {len(self.vehicles)} vehicles"
                self._save_cache()

        except Exception as exc:
            with self._lock:
                self.error = str(exc)
                self.progress = f"Error: {exc}"

    def fetch_item_prices(self, item_id: int) -> list[dict]:
        with self._lock:
            if item_id in self.item_prices:
                return self.item_prices[item_id]
            if item_id in self._fetching_prices:
                return []  # Another thread is fetching
            self._fetching_prices.add(item_id)
        prices = []
        try:
            prices = self._get(f"items_prices?id_item={item_id}")
        finally:
            with self._lock:
                self.item_prices[item_id] = prices
                self._fetching_prices.discard(item_id)
        return prices

    def get_status(self) -> str:
        with self._lock:
            return self.progress

    def is_loaded(self) -> bool:
        with self._lock:
            return self.loaded

    def clear_cache(self):
        try:
            if os.path.exists(CACHE_FILE):
                os.remove(CACHE_FILE)
        except OSError as exc:
            log.warning("Failed to remove cache file: %s", exc)
        with self._lock:
            self.item_prices.clear()


# ---------------------------------------------------------------------------
# Scrollable frame helper
# ---------------------------------------------------------------------------
class ScrollableFrame(tk.Frame):
    def __init__(self, parent, **kw):
        super().__init__(parent, bg=BG, **kw)
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self._bind_mousewheel(self.canvas)
        self._bind_mousewheel(self.inner)

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_mousewheel(self, widget):
        widget.bind("<MouseWheel>", self._on_mousewheel)
        widget.bind("<Button-4>", lambda e: self.canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda e: self.canvas.yview_scroll(1, "units"))

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def scroll_to_top(self):
        self.canvas.yview_moveto(0)


# ---------------------------------------------------------------------------
# Virtual-scroll item table (Canvas + Frame rows)
# ---------------------------------------------------------------------------
class VirtualTable(tk.Frame):
    COLUMNS = [
        ("Name", 260),
        ("Category", 130),
        ("Section", 110),
        ("Manufacturer", 130),
    ]

    def __init__(self, parent, on_select=None):
        super().__init__(parent, bg=BG)
        self._on_select = on_select
        self._items: list[dict] = []
        self._sorted_col = 0
        self._sorted_asc = True
        self._selected_idx = -1
        self._hover_idx = -1
        self._scroll_offset = 0

        # Header
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")
        self._hdr_labels = []
        for i, (name, w) in enumerate(self.COLUMNS):
            lbl = tk.Label(
                hdr, text=name, bg=HEADER_BG, fg=FG_DIM,
                font=(FONT, 9, "bold"), anchor="w", padx=6, width=0,
            )
            lbl.pack(side="left", fill="x", expand=(i == 0))
            lbl.configure(width=w // 8)
            lbl.bind("<Button-1>", lambda e, ci=i: self._sort_by(ci))
            self._hdr_labels.append(lbl)

        # Canvas
        self.canvas = tk.Canvas(self, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self._draw())
        self.canvas.bind("<MouseWheel>", self._on_scroll)
        self.canvas.bind("<Button-4>", lambda e: self._scroll(-3))
        self.canvas.bind("<Button-5>", lambda e: self._scroll(3))
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)

        # Scrollbar
        self.scrollbar = tk.Scrollbar(self, orient="vertical", command=self._sb_scroll)
        self.scrollbar.pack(side="right", fill="y", before=self.canvas)

    def set_items(self, items: list[dict]):
        self._items = list(items)
        self._selected_idx = -1
        self._scroll_offset = 0
        self._apply_sort()

    def _apply_sort(self):
        keys = ["name", "category", "section", "company_name"]
        key = keys[self._sorted_col]
        self._items.sort(key=lambda x: (x.get(key) or "").lower(), reverse=not self._sorted_asc)
        self._draw()

    def _sort_by(self, col):
        if self._sorted_col == col:
            self._sorted_asc = not self._sorted_asc
        else:
            self._sorted_col = col
            self._sorted_asc = True
        for i, lbl in enumerate(self._hdr_labels):
            name = self.COLUMNS[i][0]
            if i == col:
                arrow = " \u25b2" if self._sorted_asc else " \u25bc"
                lbl.configure(text=name + arrow, fg=ACCENT)
            else:
                lbl.configure(text=name, fg=FG_DIM)
        self._apply_sort()

    def _visible_count(self) -> int:
        return max(1, self.canvas.winfo_height() // ROW_H)

    def _max_offset(self) -> int:
        return max(0, len(self._items) - self._visible_count())

    def _clamp_offset(self):
        self._scroll_offset = max(0, min(self._scroll_offset, self._max_offset()))

    def _on_scroll(self, event):
        delta = -1 if event.delta > 0 else 1
        self._scroll(delta * 3)

    def _scroll(self, delta):
        self._scroll_offset += delta
        self._clamp_offset()
        self._draw()

    def _sb_scroll(self, *args):
        if args[0] == "moveto":
            frac = float(args[1])
            self._scroll_offset = int(frac * len(self._items))
            self._clamp_offset()
            self._draw()
        elif args[0] == "scroll":
            delta = int(args[1])
            if args[2] == "units":
                self._scroll(delta)
            else:
                self._scroll(delta * self._visible_count())

    def _on_click(self, event):
        row = event.y // ROW_H
        idx = self._scroll_offset + row
        if 0 <= idx < len(self._items):
            self._selected_idx = idx
            self._draw()
            if self._on_select:
                self._on_select(self._items[idx])

    def _on_motion(self, event):
        row = event.y // ROW_H
        idx = self._scroll_offset + row
        if idx != self._hover_idx:
            self._hover_idx = idx
            self._draw()

    def _on_leave(self, _):
        self._hover_idx = -1
        self._draw()

    def _draw(self):
        c = self.canvas
        c.delete("all")
        cw = c.winfo_width()
        ch = c.winfo_height()
        if cw < 10:
            return
        self._clamp_offset()
        vis = self._visible_count()
        total = len(self._items)

        # Update scrollbar
        if total > 0:
            lo = self._scroll_offset / total
            hi = min(1.0, (self._scroll_offset + vis) / total)
            self.scrollbar.set(lo, hi)
        else:
            self.scrollbar.set(0, 1)

        # Column positions
        col_widths = [cw * 0.38, cw * 0.22, cw * 0.18, cw * 0.22]
        col_x = [0]
        for w in col_widths[:-1]:
            col_x.append(col_x[-1] + w)

        for vi in range(vis + 1):
            idx = self._scroll_offset + vi
            if idx >= total:
                break
            item = self._items[idx]
            y = vi * ROW_H

            # Row background
            if idx == self._selected_idx:
                bg = ROW_SEL
            elif idx == self._hover_idx:
                bg = ROW_HOVER
            else:
                bg = ROW_EVEN if idx % 2 == 0 else ROW_ODD

            c.create_rectangle(0, y, cw, y + ROW_H, fill=bg, outline="")

            vals = [
                item.get("name", ""),
                item.get("category", ""),
                item.get("section", ""),
                item.get("company_name", ""),
            ]
            colors = [FG, FG_DIM, FG_DIM, FG_DIM]
            for ci, val in enumerate(vals):
                txt = str(val) if val else ""
                max_chars = int(col_widths[ci] / 7.5)
                if len(txt) > max_chars:
                    txt = txt[: max_chars - 1] + "\u2026"
                c.create_text(
                    col_x[ci] + 8, y + ROW_H // 2,
                    text=txt, fill=colors[ci], font=(FONT, 9),
                    anchor="w",
                )

        if total == 0:
            c.create_text(
                cw // 2, ch // 2,
                text="No items found", fill=FG_DIM, font=(FONT, 11),
            )


# ---------------------------------------------------------------------------
# Rental table with expandable rows
# ---------------------------------------------------------------------------
class RentalTable(tk.Frame):
    COLUMNS = [
        ("Ship", 220),
        ("Manufacturer", 140),
        ("SCU", 60),
        ("Crew", 50),
        ("Locations", 80),
    ]

    def __init__(self, parent, data_mgr: DataManager):
        super().__init__(parent, bg=BG)
        self.data = data_mgr
        self._vehicles: list[dict] = []
        self._expanded: set[int] = set()
        self._scroll_offset = 0
        self._hover_idx = -1
        self._sorted_col = 0
        self._sorted_asc = True

        # Header
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")
        self._hdr_labels = []
        for i, (name, w) in enumerate(self.COLUMNS):
            lbl = tk.Label(
                hdr, text=name, bg=HEADER_BG, fg=FG_DIM,
                font=(FONT, 9, "bold"), anchor="w", padx=6,
            )
            lbl.pack(side="left", fill="x", expand=(i == 0))
            lbl.configure(width=w // 8)
            lbl.bind("<Button-1>", lambda e, ci=i: self._sort_by(ci))
            self._hdr_labels.append(lbl)

        # Canvas area
        canvas_frame = tk.Frame(self, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        self.scrollbar = tk.Scrollbar(canvas_frame, orient="vertical")
        self.scrollbar.pack(side="right", fill="y")

        self.canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0, bd=0,
                                yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.configure(command=self.canvas.yview)

        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_cfg)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    def _on_canvas_cfg(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def set_vehicles(self, vehicles: list[dict]):
        rentable_ids = set(self.data.rental_by_vehicle.keys())
        self._vehicles = [v for v in vehicles if v.get("id") in rentable_ids]
        self._expanded.clear()
        self._apply_sort()
        self._build_rows()

    def _sort_by(self, col):
        if self._sorted_col == col:
            self._sorted_asc = not self._sorted_asc
        else:
            self._sorted_col = col
            self._sorted_asc = True
        for i, lbl in enumerate(self._hdr_labels):
            name = self.COLUMNS[i][0]
            if i == col:
                arrow = " \u25b2" if self._sorted_asc else " \u25bc"
                lbl.configure(text=name + arrow, fg=ACCENT)
            else:
                lbl.configure(text=name, fg=FG_DIM)
        self._apply_sort()
        self._build_rows()

    def _apply_sort(self):
        keys = ["name", "company_name", "scu", "crew", None]
        key = keys[self._sorted_col]
        if key is None:
            key_fn = lambda v: len(self.data.rental_by_vehicle.get(v.get("id"), []))
        elif key in ("scu", "crew"):
            key_fn = lambda v: v.get(key) or 0
        else:
            key_fn = lambda v: (v.get(key) or "").lower()
        self._vehicles.sort(key=key_fn, reverse=not self._sorted_asc)

    def _build_rows(self):
        for w in self.inner.winfo_children():
            w.destroy()

        for vi, veh in enumerate(self._vehicles):
            vid = veh.get("id")
            bg = ROW_EVEN if vi % 2 == 0 else ROW_ODD
            rentals = self.data.rental_by_vehicle.get(vid, [])
            expanded = vid in self._expanded

            row = tk.Frame(self.inner, bg=bg)
            row.pack(fill="x")

            arrow = "\u25bc" if expanded else "\u25b6"
            tk.Label(
                row, text=f" {arrow} {veh.get('name', '')}", bg=bg, fg=FG,
                font=(FONT, 9), anchor="w", padx=4,
            ).pack(side="left", fill="x", expand=True)
            tk.Label(
                row, text=veh.get("company_name", ""), bg=bg, fg=FG_DIM,
                font=(FONT, 9), anchor="w", width=18,
            ).pack(side="left")
            tk.Label(
                row, text=str(veh.get("scu") or "—"), bg=bg, fg=FG_DIM,
                font=(FONT, 9), anchor="e", width=6,
            ).pack(side="left")
            tk.Label(
                row, text=str(veh.get("crew") or "—"), bg=bg, fg=FG_DIM,
                font=(FONT, 9), anchor="e", width=6,
            ).pack(side="left")
            tk.Label(
                row, text=str(len(rentals)), bg=bg, fg=ACCENT,
                font=(FONT, 9), anchor="e", width=8,
            ).pack(side="left")

            row.bind("<Button-1>", lambda e, v=vid: self._toggle(v))
            for child in row.winfo_children():
                child.bind("<Button-1>", lambda e, v=vid: self._toggle(v))

            if expanded and rentals:
                exp_frame = tk.Frame(self.inner, bg=BG2)
                exp_frame.pack(fill="x", padx=(20, 0))
                for ri, rent in enumerate(rentals):
                    rbg = BG3 if ri % 2 == 0 else BG4
                    rrow = tk.Frame(exp_frame, bg=rbg)
                    rrow.pack(fill="x")
                    tname = rent.get("terminal_name", "Unknown")
                    price = rent.get("price_rent")
                    price_str = f"{price:,.0f} aUEC" if price else "—"

                    # Try to resolve location from terminal ID
                    tid = rent.get("id_terminal")
                    loc_parts = []
                    if tid and tid in self.data.terminals:
                        term = self.data.terminals[tid]
                        for fld in ("star_system_name", "planet_name", "city_name", "space_station_name"):
                            val = term.get(fld)
                            if val:
                                loc_parts.append(val)
                    loc_str = " > ".join(loc_parts) if loc_parts else ""

                    tk.Label(
                        rrow, text=f"  {tname}", bg=rbg, fg=FG,
                        font=(FONT, 8), anchor="w",
                    ).pack(side="left", padx=(4, 10))
                    if loc_str:
                        tk.Label(
                            rrow, text=loc_str, bg=rbg, fg=FG_DIM,
                            font=(FONT, 8), anchor="w",
                        ).pack(side="left", padx=(0, 10))
                    tk.Label(
                        rrow, text=price_str, bg=rbg, fg=GREEN,
                        font=(FONT, 8), anchor="e",
                    ).pack(side="right", padx=8)

        self.canvas.yview_moveto(0)

    def _toggle(self, vid):
        if vid in self._expanded:
            self._expanded.discard(vid)
        else:
            self._expanded.add(vid)
        self._build_rows()


# ---------------------------------------------------------------------------
# Ship table (all vehicles with specs)
# ---------------------------------------------------------------------------
class ShipTable(tk.Frame):
    COLUMNS = [
        ("Ship", 180),
        ("Manufacturer", 120),
        ("Size", 45),
        ("Buy Price", 80),
        ("SCU", 50),
        ("Crew", 45),
        ("QT Fuel", 55),
        ("Mass", 65),
    ]
    MAX_VISIBLE = 50  # only render this many rows at a time

    def __init__(self, parent, data_mgr: DataManager, on_select=None):
        super().__init__(parent, bg=BG)
        self.data = data_mgr
        self._vehicles: list[dict] = []
        self._on_select = on_select
        self._selected_idx = -1
        self._sorted_col = 0
        self._sorted_asc = True
        self._filter_text = ""

        # Filter bar
        filt_bar = tk.Frame(self, bg=BG2, height=30)
        filt_bar.pack(fill="x")
        filt_bar.pack_propagate(False)

        tk.Label(filt_bar, text="\U0001f50d", font=(FONT, 9),
                 bg=BG2, fg=FG_DIM).pack(side="left", padx=(8, 4))
        self._filter_var = tk.StringVar()
        self._filter_var.trace_add("write", lambda *_: self._on_filter())
        filt_entry = tk.Entry(filt_bar, textvariable=self._filter_var,
                              font=(FONT, 9), bg=BG3, fg=FG,
                              insertbackground="white", relief="flat",
                              highlightthickness=1, highlightcolor=ACCENT,
                              highlightbackground=BORDER)
        filt_entry.pack(side="left", fill="x", expand=True, padx=(0, 8), pady=4)

        # Type filter pills
        self._type_btns = {}
        for t_label in ("Spaceship", "Ground"):
            var = tk.BooleanVar(value=False)
            btn = tk.Button(filt_bar, text=t_label, font=(FONT, 7),
                            bg=BG3, fg=FG_DIM, relief="flat", bd=0,
                            cursor="hand2", padx=6, pady=1)
            def _tog(v=var, b=btn):
                v.set(not v.get())
                b.configure(bg="#1a3030" if v.get() else BG3,
                            fg=ACCENT if v.get() else FG_DIM)
                self._on_filter()
            btn.configure(command=_tog)
            btn.pack(side="left", padx=(0, 4), pady=4)
            self._type_btns[t_label] = (btn, var)

        # Header
        hdr = tk.Frame(self, bg=HEADER_BG)
        hdr.pack(fill="x")
        self._hdr_labels = []
        for i, (name, w) in enumerate(self.COLUMNS):
            lbl = tk.Label(
                hdr, text=name, bg=HEADER_BG, fg=FG_DIM,
                font=(FONT, 8, "bold"), anchor="w", padx=4,
            )
            lbl.pack(side="left", fill="x", expand=(i == 0))
            lbl.configure(width=w // 8)
            lbl.bind("<Button-1>", lambda e, ci=i: self._sort_by(ci))
            self._hdr_labels.append(lbl)

        # Canvas
        canvas_frame = tk.Frame(self, bg=BG)
        canvas_frame.pack(fill="both", expand=True)
        self.scrollbar = tk.Scrollbar(canvas_frame, orient="vertical")
        self.scrollbar.pack(side="right", fill="y")
        self.canvas = tk.Canvas(canvas_frame, bg=BG, highlightthickness=0, bd=0,
                                yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.configure(command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.bind("<Configure>", self._on_canvas_cfg)
        self.inner.bind("<Configure>",
                        lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<MouseWheel>",
                         lambda e: self.canvas.yview_scroll(int(-1 * (e.delta / 120)), "units"))

        # Count label
        self._count_var = tk.StringVar(value="")
        tk.Label(self, textvariable=self._count_var, font=(FONT, 8),
                 bg=BG, fg=FG_DIM, padx=8).pack(fill="x")

    def _on_canvas_cfg(self, event):
        self.canvas.itemconfigure(self._win, width=event.width)

    def set_vehicles(self, vehicles: list[dict]):
        self._vehicles = list(vehicles)
        self._apply_filter_and_sort()

    def _on_filter(self):
        self._filter_text = (self._filter_var.get() or "").lower().strip()
        self._apply_filter_and_sort()

    def _sort_by(self, col):
        if self._sorted_col == col:
            self._sorted_asc = not self._sorted_asc
        else:
            self._sorted_col = col
            self._sorted_asc = True
        for i, lbl in enumerate(self._hdr_labels):
            name = self.COLUMNS[i][0]
            if i == col:
                arrow = " \u25b2" if self._sorted_asc else " \u25bc"
                lbl.configure(text=name + arrow, fg=ACCENT)
            else:
                lbl.configure(text=name, fg=FG_DIM)
        self._apply_filter_and_sort()

    def _apply_filter_and_sort(self):
        items = self._vehicles
        q = self._filter_text

        # Type filter
        show_space = self._type_btns.get("Spaceship", (None, tk.BooleanVar()))[1].get()
        show_ground = self._type_btns.get("Ground", (None, tk.BooleanVar()))[1].get()
        if show_space and not show_ground:
            items = [v for v in items if v.get("is_spaceship")]
        elif show_ground and not show_space:
            items = [v for v in items if v.get("is_ground_vehicle")]

        # Text filter
        if q:
            items = [v for v in items
                     if q in (v.get("name") or "").lower()
                     or q in (v.get("name_full") or "").lower()
                     or q in (v.get("company_name") or "").lower()]

        # Sort — column order matches COLUMNS: Ship, Manufacturer, Size, Buy Price, SCU, Crew, QT Fuel, Mass
        col_keys = ["name", "company_name", "pad_type", "_best_buy", "scu", "crew",
                     "fuel_quantum", "mass"]
        key = col_keys[self._sorted_col] if self._sorted_col < len(col_keys) else "name"
        if key == "_best_buy":
            # Sort by best purchase price (0 = no price = sort to end)
            key_fn = lambda v: self._get_best_buy(v) or (999_999_999 if self._sorted_asc else 0)
        elif key in ("scu", "crew", "fuel_quantum", "mass"):
            key_fn = lambda v, k=key: v.get(k) or 0
        else:
            key_fn = lambda v, k=key: (v.get(k) or "").lower()
        items.sort(key=key_fn, reverse=not self._sorted_asc)

        self._filtered = items
        self._count_var.set(f"{len(items)} ships")
        self._build_rows()

    def _get_best_buy(self, veh) -> int:
        """Get cheapest purchase price for a vehicle, or 0 if not buyable."""
        vid = veh.get("id")
        purchases = self.data.purchase_by_vehicle.get(vid, [])
        if not purchases:
            return 0
        return min((p.get("price_buy", 0) for p in purchases if p.get("price_buy")), default=0)

    def _fmt_num(self, val):
        if not val:
            return "—"
        try:
            n = float(val)
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f}M"
            if n >= 1000:
                return f"{n/1000:.0f}K"
            return f"{n:,.0f}"
        except (ValueError, TypeError):
            return str(val)

    def _build_rows(self):
        for w in self.inner.winfo_children():
            w.destroy()
        self._visible_offset = 0
        self._render_batch(0)

    def _render_batch(self, start):
        """Render a batch of MAX_VISIBLE rows starting at offset."""
        end = min(start + self.MAX_VISIBLE, len(self._filtered))
        for vi in range(start, end):
            veh = self._filtered[vi]
            bg = ROW_EVEN if vi % 2 == 0 else ROW_ODD
            row = tk.Frame(self.inner, bg=bg, cursor="hand2")
            row.pack(fill="x")

            # Ship name with type icon
            icon = "\U0001f680" if veh.get("is_spaceship") else "\U0001f699"
            name = veh.get("name", "?")
            tk.Label(row, text=f" {icon} {name}", bg=bg, fg=FG,
                     font=(FONT, 9), anchor="w", padx=4).pack(
                         side="left", fill="x", expand=True)

            # Manufacturer
            tk.Label(row, text=veh.get("company_name", ""), bg=bg, fg=FG_DIM,
                     font=(FONT, 8), anchor="w", width=15).pack(side="left")

            # Size (pad_type)
            pad = veh.get("pad_type") or "—"
            tk.Label(row, text=pad, bg=bg, fg=FG_DIM,
                     font=(FONT, 8), anchor="center", width=5).pack(side="left")

            # Buy Price
            best_buy = self._get_best_buy(veh)
            if best_buy:
                bp_str = self._fmt_num(best_buy)
                bp_color = GREEN
            else:
                bp_str = "—"
                bp_color = FG_DIMMER
            tk.Label(row, text=bp_str, bg=bg, fg=bp_color,
                     font=(FONT, 8, "bold"), anchor="e", width=9).pack(side="left")

            # SCU
            scu = veh.get("scu")
            scu_color = ACCENT if scu and scu > 0 else FG_DIM
            tk.Label(row, text=self._fmt_num(scu) if scu else "—",
                     bg=bg, fg=scu_color, font=(FONT, 8), anchor="e",
                     width=6).pack(side="left")

            # Crew
            tk.Label(row, text=str(veh.get("crew") or "—"), bg=bg, fg=FG_DIM,
                     font=(FONT, 8), anchor="e", width=5).pack(side="left")

            # QT Fuel
            tk.Label(row, text=self._fmt_num(veh.get("fuel_quantum")),
                     bg=bg, fg=FG_DIM, font=(FONT, 8), anchor="e",
                     width=6).pack(side="left")

            # Mass
            tk.Label(row, text=self._fmt_num(veh.get("mass")),
                     bg=bg, fg=FG_DIM, font=(FONT, 8), anchor="e",
                     width=7).pack(side="left")

            # Hover
            def _enter(e, r=row):
                for w in r.winfo_children():
                    try: w.configure(bg=ROW_HOVER)
                    except Exception: log.warning("[MarketFinder] Hover enter configure failed for widget %s", w)
                r.configure(bg=ROW_HOVER)
            def _leave(e, r=row, b=bg):
                for w in r.winfo_children():
                    try: w.configure(bg=b)
                    except Exception: log.warning("[MarketFinder] Hover leave configure failed for widget %s", w)
                r.configure(bg=b)

            row.bind("<Enter>", _enter)
            row.bind("<Leave>", _leave)
            for child in row.winfo_children():
                child.bind("<Enter>", _enter)
                child.bind("<Leave>", _leave)

            # Click → show ship detail
            def _click(e, v=veh):
                if self._on_select:
                    self._on_select(v)
            row.bind("<Button-1>", _click)
            for child in row.winfo_children():
                child.bind("<Button-1>", _click)

        # "Load more" button if there are more rows
        if end < len(self._filtered):
            remaining = len(self._filtered) - end
            more_btn = tk.Button(
                self.inner, text=f"Load {min(remaining, self.MAX_VISIBLE)} more ({remaining} remaining)",
                font=(FONT, 8), bg=BG3, fg=ACCENT, relief="flat", bd=0,
                cursor="hand2", padx=10, pady=4,
                command=lambda s=end: self._load_more(s))
            more_btn.pack(fill="x", padx=20, pady=8)

        if start == 0:
            self.canvas.yview_moveto(0)

        # Propagate mousewheel
        def _bw(w):
            w.bind("<MouseWheel>", lambda e: self.canvas.yview_scroll(
                int(-1 * (e.delta / 120)), "units"))
            for c in w.winfo_children():
                _bw(c)
        _bw(self.inner)

    def _load_more(self, start):
        """Append more rows without clearing existing ones."""
        # Remove the "Load more" button
        children = self.inner.winfo_children()
        if children and isinstance(children[-1], tk.Button):
            children[-1].destroy()
        self._render_batch(start)


# ---------------------------------------------------------------------------
# Detail panel
# ---------------------------------------------------------------------------
class DetailPanel(tk.Frame):
    def __init__(self, parent, data_mgr: DataManager):
        super().__init__(parent, bg=BG)
        self.data = data_mgr
        self._price_gen = 0
        self._price_lock = threading.Lock()
        self.scroll = ScrollableFrame(self)
        self.scroll.pack(fill="both", expand=True)
        self._show_placeholder()

    def _show_placeholder(self):
        for w in self.scroll.inner.winfo_children():
            w.destroy()
        tk.Label(
            self.scroll.inner, text="Select an item to view details",
            bg=BG, fg=FG_DIM, font=(FONT, 10), wraplength=280,
        ).pack(pady=40, padx=10)

    def show_item(self, item: dict):
        for w in self.scroll.inner.winfo_children():
            w.destroy()
        self.scroll.scroll_to_top()

        inner = self.scroll.inner

        # Item name
        tk.Label(
            inner, text=item.get("name", "Unknown"), bg=BG, fg=ACCENT,
            font=(FONT, 12, "bold"), anchor="w", wraplength=320,
        ).pack(fill="x", padx=8, pady=(10, 2))

        # Metadata rows
        meta = [
            ("Category", item.get("category", "—")),
            ("Section", item.get("section", "—")),
            ("Manufacturer", item.get("company_name", "—")),
            ("Size", item.get("size", "—")),
        ]
        for label, val in meta:
            if val and val != "—":
                row = tk.Frame(inner, bg=BG)
                row.pack(fill="x", padx=8, pady=1)
                tk.Label(row, text=f"{label}:", bg=BG, fg=FG_DIM, font=(FONT, 9), anchor="w", width=14).pack(side="left")
                tk.Label(row, text=str(val), bg=BG, fg=FG, font=(FONT, 9), anchor="w").pack(side="left")

        # Separator
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=8)

        # Fetch prices in background
        tk.Label(
            inner, text="Loading prices...", bg=BG, fg=FG_DIM,
            font=(FONT, 9), anchor="w",
        ).pack(fill="x", padx=8)

        item_id = item.get("id")
        threading.Thread(target=self._load_prices, args=(item_id, item), daemon=True).start()

    def show_ship(self, vehicle: dict):
        """Show vehicle/ship details in the panel."""
        for w in self.scroll.inner.winfo_children():
            w.destroy()
        self.scroll.scroll_to_top()
        inner = self.scroll.inner

        # Ship name
        name = vehicle.get("name_full") or vehicle.get("name", "?")
        tk.Label(inner, text=name, bg=BG, fg=ACCENT,
                 font=(FONT, 12, "bold"), anchor="w", wraplength=320,
                 ).pack(fill="x", padx=8, pady=(10, 2))

        # Manufacturer
        mfr = vehicle.get("company_name", "")
        if mfr:
            tk.Label(inner, text=mfr, bg=BG, fg=FG_DIM,
                     font=(FONT, 9), anchor="w").pack(fill="x", padx=8)

        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=6)

        # Specs grid
        icon = "\U0001f680" if vehicle.get("is_spaceship") else "\U0001f699"
        vtype = "Spaceship" if vehicle.get("is_spaceship") else "Ground Vehicle"

        specs = [
            ("Type", f"{icon} {vtype}"),
            ("Pad Size", vehicle.get("pad_type", "—")),
            ("Crew", vehicle.get("crew", "—")),
            ("Cargo (SCU)", vehicle.get("scu", "—")),
            ("H2 Fuel", self._fmt(vehicle.get("fuel_hydrogen"))),
            ("QT Fuel", self._fmt(vehicle.get("fuel_quantum"))),
            ("Mass", f"{self._fmt(vehicle.get('mass'))} kg"),
            ("Length", f"{vehicle.get('length', '—')} m"),
            ("Width", f"{vehicle.get('width', '—')} m"),
            ("Height", f"{vehicle.get('height', '—')} m"),
        ]
        for label, val in specs:
            if val and val != "—" and val != "— kg" and val != "— m":
                row = tk.Frame(inner, bg=BG)
                row.pack(fill="x", padx=8, pady=1)
                tk.Label(row, text=f"{label}:", bg=BG, fg=FG_DIM,
                         font=(FONT, 9), anchor="w", width=14).pack(side="left")
                tk.Label(row, text=str(val), bg=BG, fg=FG,
                         font=(FONT, 9), anchor="w").pack(side="left")

        # Role tags
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=6)
        roles_frame = tk.Frame(inner, bg=BG)
        roles_frame.pack(fill="x", padx=8, pady=2)
        role_keys = [
            ("is_cargo", "Cargo", CYAN), ("is_mining", "Mining", YELLOW),
            ("is_salvage", "Salvage", ORANGE), ("is_medical", "Medical", GREEN),
            ("is_exploration", "Explorer", ACCENT), ("is_military", "Military", RED),
            ("is_racing", "Racing", YELLOW), ("is_stealth", "Stealth", FG_DIM),
            ("is_passenger", "Passenger", GREEN), ("is_refuel", "Refuel", CYAN),
            ("is_repair", "Repair", ORANGE), ("is_bomber", "Bomber", RED),
            ("is_carrier", "Carrier", ACCENT), ("is_starter", "Starter", GREEN),
        ]
        for key, label, color in role_keys:
            if vehicle.get(key):
                tk.Label(roles_frame, text=label, bg=BG3, fg=color,
                         font=(FONT, 7, "bold"), padx=4, pady=1).pack(
                             side="left", padx=(0, 4), pady=1)

        # Purchase locations
        vid = vehicle.get("id")
        purchases = self.data.purchase_by_vehicle.get(vid, [])
        if purchases:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=6)
            self._section_header(inner, f"WHERE TO BUY ({len(purchases)})", ACCENT)
            purchases_sorted = sorted(purchases, key=lambda p: p.get("price_buy", 0))
            for i, pur in enumerate(purchases_sorted[:15]):
                bg = ROW_EVEN if i % 2 == 0 else ROW_ODD
                prow = tk.Frame(inner, bg=bg)
                prow.pack(fill="x", padx=4)
                tname = pur.get("terminal_name", "?")
                price = pur.get("price_buy")
                price_str = f"{price:,.0f} aUEC" if price else "—"

                # Resolve location from terminal
                tid = pur.get("id_terminal")
                loc_parts = []
                if tid and tid in self.data.terminals:
                    term = self.data.terminals[tid]
                    for fld in ("star_system_name", "planet_name", "city_name",
                                "space_station_name"):
                        v = term.get(fld)
                        if v:
                            loc_parts.append(v)
                loc = " > ".join(loc_parts)

                left = tk.Frame(prow, bg=bg)
                left.pack(side="left", fill="x", expand=True, padx=4, pady=2)
                tk.Label(left, text=tname, bg=bg, fg=FG,
                         font=(FONT, 8), anchor="w").pack(fill="x")
                if loc:
                    tk.Label(left, text=loc, bg=bg, fg=FG_DIM,
                             font=(FONT, 7), anchor="w").pack(fill="x")
                # Best price badge
                best_str = " BEST" if i == 0 else ""
                tk.Label(prow, text=price_str + best_str, bg=bg,
                         fg=GREEN if i == 0 else ACCENT,
                         font=(FONT, 9, "bold"), anchor="e", padx=6).pack(
                             side="right", pady=2)
        else:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=6)
            tk.Label(inner, text="Not available for in-game purchase", bg=BG,
                     fg=FG_DIM, font=(FONT, 8), padx=8).pack(fill="x")

        # Rental info
        rentals = self.data.rental_by_vehicle.get(vid, [])
        if rentals:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=6)
            self._section_header(inner, f"RENTAL LOCATIONS ({len(rentals)})", GREEN)
            rentals_sorted = sorted(rentals, key=lambda r: r.get("price_rent", 0))
            for i, rent in enumerate(rentals_sorted[:15]):
                bg = ROW_EVEN if i % 2 == 0 else ROW_ODD
                rrow = tk.Frame(inner, bg=bg)
                rrow.pack(fill="x", padx=4)
                tname = rent.get("terminal_name", "?")
                price = rent.get("price_rent")
                price_str = f"{price:,.0f} aUEC/day" if price else "—"

                # Resolve location
                tid = rent.get("id_terminal")
                loc_parts = []
                if tid and tid in self.data.terminals:
                    term = self.data.terminals[tid]
                    for fld in ("star_system_name", "planet_name", "city_name"):
                        v = term.get(fld)
                        if v:
                            loc_parts.append(v)
                loc = " > ".join(loc_parts)

                left = tk.Frame(rrow, bg=bg)
                left.pack(side="left", fill="x", expand=True, padx=4, pady=2)
                tk.Label(left, text=tname, bg=bg, fg=FG,
                         font=(FONT, 8), anchor="w").pack(fill="x")
                if loc:
                    tk.Label(left, text=loc, bg=bg, fg=FG_DIM,
                             font=(FONT, 7), anchor="w").pack(fill="x")
                tk.Label(rrow, text=price_str, bg=bg, fg=GREEN,
                         font=(FONT, 9, "bold"), anchor="e", padx=6).pack(
                             side="right", pady=2)

        # Store links
        urls = []
        if vehicle.get("url_store"):
            urls.append(("RSI Store", vehicle["url_store"]))
        if vehicle.get("url_brochure"):
            urls.append(("Brochure", vehicle["url_brochure"]))
        if urls:
            tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", padx=8, pady=6)
            for label, url in urls:
                lnk = tk.Label(inner, text=f"\U0001f517 {label}", bg=BG, fg=ACCENT,
                               font=(FONT, 8), cursor="hand2", anchor="w")
                lnk.pack(fill="x", padx=8, pady=1)
                lnk.bind("<Button-1>", lambda e, u=url: webbrowser.open(u))

    def _fmt(self, val):
        if not val:
            return "—"
        try:
            n = float(val)
            if n >= 1_000_000:
                return f"{n/1_000_000:.1f}M"
            if n >= 1000:
                return f"{n:,.0f}"
            return f"{n:.0f}"
        except (ValueError, TypeError):
            return str(val)

    def _load_prices(self, item_id: int, item: dict):
        with self._price_lock:
            self._price_gen += 1
            gen = self._price_gen
        prices = self.data.fetch_item_prices(item_id)
        try:
            self.after(0, lambda p=prices, it=item, g=gen: self._render_prices(p, it, g))
        except tk.TclError:
            pass  # widget destroyed

    def _render_prices(self, prices: list[dict], item: dict, gen: int = None):
        # Check staleness under lock
        if gen is not None:
            with self._price_lock:
                if self._price_gen != gen:
                    return  # stale

        inner = self.scroll.inner

        # Remove "Loading prices..." label
        children = inner.winfo_children()
        if children:
            last = children[-1]
            if isinstance(last, tk.Label) and "Loading" in str(last.cget("text")):
                last.destroy()

        buy_prices = [p for p in prices if p.get("price_buy") and p["price_buy"] > 0]
        sell_prices = [p for p in prices if p.get("price_sell") and p["price_sell"] > 0]

        if not buy_prices and not sell_prices:
            tk.Label(
                inner, text="No market data available", bg=BG, fg=FG_DIM,
                font=(FONT, 9),
            ).pack(fill="x", padx=8, pady=4)
            return

        # WHERE TO BUY
        if buy_prices:
            buy_prices.sort(key=lambda p: p.get("price_buy", 0))
            self._section_header(inner, "WHERE TO BUY", GREEN)
            for i, p in enumerate(buy_prices[:20]):
                self._price_row(inner, p, "buy", i)

        # WHERE TO SELL
        if sell_prices:
            sell_prices.sort(key=lambda p: p.get("price_sell", 0), reverse=True)
            self._section_header(inner, "WHERE TO SELL", ORANGE)
            for i, p in enumerate(sell_prices[:20]):
                self._price_row(inner, p, "sell", i)

    def _section_header(self, parent, text, color):
        hdr = tk.Frame(parent, bg=SECT_HDR_BG)
        hdr.pack(fill="x", padx=4, pady=(10, 2))
        tk.Label(
            hdr, text=text, bg=SECT_HDR_BG, fg=color,
            font=(FONT, 9, "bold"), anchor="w", padx=6, pady=3,
        ).pack(fill="x")

    def _price_row(self, parent, price_data: dict, mode: str, idx: int):
        bg = ROW_EVEN if idx % 2 == 0 else ROW_ODD
        row = tk.Frame(parent, bg=bg)
        row.pack(fill="x", padx=4)

        tname = price_data.get("terminal_name", "Unknown")
        loc_parts = []
        for field in ("star_system_name", "planet_name", "moon_name", "city_name", "space_station_name"):
            val = price_data.get(field)
            if val:
                loc_parts.append(val)
        loc = " > ".join(loc_parts) if loc_parts else ""

        if mode == "buy":
            price_val = price_data.get("price_buy", 0)
            color = GREEN
        else:
            price_val = price_data.get("price_sell", 0)
            color = ORANGE
        price_str = f"{price_val:,.0f}" if price_val else "—"

        left = tk.Frame(row, bg=bg)
        left.pack(side="left", fill="x", expand=True, padx=4, pady=2)
        tk.Label(
            left, text=tname, bg=bg, fg=FG,
            font=(FONT, 8), anchor="w",
        ).pack(fill="x")
        if loc:
            tk.Label(
                left, text=loc, bg=bg, fg=FG_DIM,
                font=(FONT, 7), anchor="w",
            ).pack(fill="x")

        tk.Label(
            row, text=price_str, bg=bg, fg=color,
            font=(FONT, 9, "bold"), anchor="e", padx=6,
        ).pack(side="right", pady=2)


# ---------------------------------------------------------------------------
# Search bubble popup
# ---------------------------------------------------------------------------
class SearchBubble(tk.Toplevel):
    MAX_RESULTS = 30

    def __init__(self, parent, items: list[dict], on_select):
        super().__init__(parent)
        self.overrideredirect(True)
        self.attributes("-topmost", True)
        self.configure(bg=BORDER)
        self._on_select = on_select

        self.inner = tk.Frame(self, bg=BG2, padx=1, pady=1)
        self.inner.pack(fill="both", expand=True, padx=1, pady=1)

        # Group by tab
        groups: dict[str, list[dict]] = {}
        for item in items[: self.MAX_RESULTS]:
            tab = _item_tab(item)
            groups.setdefault(tab, []).append(item)

        for tab_name, tab_items in groups.items():
            color = CAT_COLORS.get(tab_name, FG_DIM)
            tk.Label(
                self.inner, text=tab_name.upper(), bg=BG2, fg=color,
                font=(FONT, 8, "bold"), anchor="w", padx=6, pady=(4, 1),
            ).pack(fill="x")
            for it in tab_items[:8]:
                lbl = tk.Label(
                    self.inner,
                    text=f"  {it.get('name', '')}  —  {it.get('category', '')}",
                    bg=BG2, fg=FG, font=(FONT, 9), anchor="w", padx=6, pady=1,
                    cursor="hand2",
                )
                lbl.pack(fill="x")
                lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=ROW_HOVER))
                lbl.bind("<Leave>", lambda e, l=lbl: l.configure(bg=BG2))
                lbl.bind("<Button-1>", lambda e, i=it: self._select(i))

        self.bind("<FocusOut>", lambda e: self.destroy())

    def _select(self, item):
        self._on_select(item)
        self.destroy()

    def position_below(self, widget):
        widget.update_idletasks()
        x = widget.winfo_rootx()
        y = widget.winfo_rooty() + widget.winfo_height()
        self.geometry(f"+{x}+{y}")
        self.update_idletasks()
        # Constrain width to widget width
        w = max(widget.winfo_width(), 400)
        h = self.winfo_reqheight()
        self.geometry(f"{w}x{min(h, 500)}+{x}+{y}")


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class MarketFinderApp:
    def __init__(self, root: tk.Tk, x=100, y=100, w=1100, h=720, opacity=0.95, cmd_file=None):
        self.root = root
        self.root.title("Market Finder")
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.configure(bg=BG)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", opacity)
        self.root.minsize(700, 400)

        self.data = DataManager()
        self._current_tab = "All"
        self._search_text = ""
        self._search_after_id = None
        self._bubble: SearchBubble | None = None
        self._cmd_file = cmd_file
        self._settings_visible = False
        self._opacity = opacity
        self._always_on_top = True
        self._auto_refresh_id = None
        self._fetching = False

        self._build_ui()
        self._start_loading()

        if cmd_file:
            self._poll_commands()

    # -- UI construction ----------------------------------------------------
    def _build_ui(self):
        # Header bar
        header = tk.Frame(self.root, bg=HEADER_BG, height=38)
        header.pack(fill="x")
        header.pack_propagate(False)

        title_frame = tk.Frame(header, bg=HEADER_BG)
        title_frame.pack(side="left", padx=10)
        tk.Label(
            title_frame, text="Market", bg=HEADER_BG, fg=ACCENT,
            font=(FONT, 14, "bold"),
        ).pack(side="left")
        tk.Label(
            title_frame, text="Finder", bg=HEADER_BG, fg="#ffffff",
            font=(FONT, 14),
        ).pack(side="left", padx=(2, 0))

        # Settings gear
        gear_btn = tk.Label(
            header, text="\u2699", bg=HEADER_BG, fg=FG_DIM,
            font=(FONT, 14), cursor="hand2",
        )
        gear_btn.pack(side="right", padx=10)
        gear_btn.bind("<Button-1>", lambda e: self._toggle_settings())

        # Status label
        self._status_lbl = tk.Label(
            header, text="Loading...", bg=HEADER_BG, fg=FG_DIM,
            font=(FONT, 8), anchor="e",
        )
        self._status_lbl.pack(side="right", padx=6)

        # (Discord link removed)

        # Settings panel (hidden by default)
        self._settings_frame = tk.Frame(self.root, bg=BG4)
        self._build_settings()

        # Search bar
        search_frame = tk.Frame(self.root, bg=BG2, height=34)
        search_frame.pack(fill="x", padx=6, pady=(4, 2))
        search_frame.pack_propagate(False)

        tk.Label(
            search_frame, text="\U0001f50d", bg=BG2, fg=FG_DIM,
            font=(FONT, 10),
        ).pack(side="left", padx=(8, 4))

        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            search_frame, textvariable=self._search_var,
            bg=BG3, fg=FG, insertbackground=FG,
            font=(FONT, 10), bd=0, highlightthickness=1,
            highlightcolor=ACCENT, highlightbackground=BORDER,
        )
        self._search_entry.pack(side="left", fill="both", expand=True, padx=4, pady=4)
        self._search_var.trace_add("write", self._on_search_changed)
        self._search_entry.bind("<Escape>", self._dismiss_bubble)
        self._search_entry.bind("<Return>", self._on_search_enter)

        # Tab bar
        tab_frame = tk.Frame(self.root, bg=BG)
        tab_frame.pack(fill="x", padx=6)
        self._tab_labels: dict[str, tk.Label] = {}
        for emoji, name in TAB_DEFS:
            lbl = tk.Label(
                tab_frame, text=f" {emoji} {name} ", bg=BG, fg=FG_DIM,
                font=(FONT, 9), cursor="hand2", padx=4, pady=3,
            )
            lbl.pack(side="left", padx=1)
            lbl.bind("<Button-1>", lambda e, n=name: self._select_tab(n))
            lbl.bind("<Enter>", lambda e, l=lbl: l.configure(bg=BG4) if l.cget("fg") != ACCENT else None)
            lbl.bind("<Leave>", lambda e, l=lbl: l.configure(bg=BG) if l.cget("fg") != ACCENT else None)
            self._tab_labels[name] = lbl
        self._select_tab("All", update_view=False)

        # Separator
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x", padx=6, pady=2)

        # Main content area
        self._content = tk.Frame(self.root, bg=BG)
        self._content.pack(fill="both", expand=True, padx=6, pady=(0, 6))

        # Split: left table (70%) + right detail (30%)
        self._paned = tk.PanedWindow(
            self._content, orient="horizontal", bg=BORDER,
            sashwidth=3, sashrelief="flat",
        )
        self._paned.pack(fill="both", expand=True)

        self._left_frame = tk.Frame(self._paned, bg=BG)
        self._right_frame = tk.Frame(self._paned, bg=BG)
        self._paned.add(self._left_frame, minsize=300)
        self._paned.add(self._right_frame, minsize=200)

        # Item table (in left frame)
        self._item_table = VirtualTable(self._left_frame, on_select=self._on_item_select)
        self._item_table.pack(fill="both", expand=True)

        # Rental table (hidden initially)
        self._rental_table = RentalTable(self._left_frame, self.data)

        # Ship table (hidden initially)
        self._ship_table = ShipTable(self._left_frame, self.data,
                                      on_select=self._on_ship_select)

        # Detail panel (in right frame)
        self._detail_panel = DetailPanel(self._right_frame, self.data)
        self._detail_panel.pack(fill="both", expand=True)

        # Set initial sash position after layout
        self.root.after(100, self._set_sash_position)

    def _set_sash_position(self):
        try:
            total_w = self._paned.winfo_width()
            if total_w > 100:
                self._paned.sash_place(0, int(total_w * 0.70), 0)
        except Exception as exc:
            print(f"[MarketFinder] Sash position failed: {exc}")

    def _build_settings(self):
        f = self._settings_frame

        row1 = tk.Frame(f, bg=BG4)
        row1.pack(fill="x", padx=10, pady=(8, 4))
        tk.Label(row1, text="Opacity:", bg=BG4, fg=FG_DIM, font=(FONT, 9)).pack(side="left")
        self._opacity_var = tk.DoubleVar(value=self._opacity)
        opacity_scale = tk.Scale(
            row1, from_=0.3, to=1.0, resolution=0.05,
            orient="horizontal", variable=self._opacity_var,
            bg=BG4, fg=FG, troughcolor=BG2, highlightthickness=0,
            font=(FONT, 8), length=150, command=self._on_opacity_change,
        )
        opacity_scale.pack(side="left", padx=8)

        row2 = tk.Frame(f, bg=BG4)
        row2.pack(fill="x", padx=10, pady=2)
        self._topmost_var = tk.BooleanVar(value=True)
        cb = tk.Checkbutton(
            row2, text="Always on top", variable=self._topmost_var,
            bg=BG4, fg=FG, selectcolor=BG2, activebackground=BG4,
            activeforeground=FG, font=(FONT, 9),
            command=self._on_topmost_change,
        )
        cb.pack(side="left")

        row3 = tk.Frame(f, bg=BG4)
        row3.pack(fill="x", padx=10, pady=(2, 4))
        tk.Label(row3, text="Cache TTL:", bg=BG4, fg=FG_DIM, font=(FONT, 9)).pack(side="left")
        self._ttl_var = tk.StringVar(value="2h")
        ttl_options = ["30m", "1h", "2h", "4h", "8h"]
        ttl_menu = tk.OptionMenu(row3, self._ttl_var, *ttl_options, command=self._on_ttl_change)
        ttl_menu.configure(bg=BG3, fg=FG, font=(FONT, 8), highlightthickness=0, bd=0)
        ttl_menu["menu"].configure(bg=BG3, fg=FG, font=(FONT, 8))
        ttl_menu.pack(side="left", padx=8)

        refresh_btn = tk.Label(
            row3, text="  Refresh Data  ", bg=ACCENT, fg=BG,
            font=(FONT, 9, "bold"), cursor="hand2", padx=6, pady=2,
        )
        refresh_btn.pack(side="right", padx=4)
        refresh_btn.bind("<Button-1>", lambda e: self._refresh_data())

    def _toggle_settings(self):
        if self._settings_visible:
            self._settings_frame.pack_forget()
            self._settings_visible = False
        else:
            # Insert after header (index 1)
            self._settings_frame.pack(fill="x", padx=6, pady=2, after=self.root.winfo_children()[0])
            self._settings_visible = True

    def _on_opacity_change(self, val):
        self._opacity = float(val)
        self.root.attributes("-alpha", self._opacity)

    def _on_topmost_change(self):
        self._always_on_top = self._topmost_var.get()
        self.root.attributes("-topmost", self._always_on_top)

    def _on_ttl_change(self, val):
        mapping = {"30m": 1800, "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800}
        self.data.cache_ttl = mapping.get(val, 7200)

    def _refresh_data(self):
        self.data.clear_cache()
        self._start_loading(force=True)

    # -- Data loading -------------------------------------------------------
    def _start_loading(self, force=False):
        if self._fetching:
            return
        self._fetching = True
        self._status_lbl.configure(text="Loading...", fg=YELLOW)
        t = threading.Thread(target=self.data.fetch_all, args=(force,), daemon=True)
        t.start()
        self._poll_loading()

    def _poll_loading(self):
        status = self.data.get_status()
        self._status_lbl.configure(text=status)
        if self.data.is_loaded():
            self._status_lbl.configure(fg=GREEN)
            self._on_data_loaded()
        else:
            self.root.after(200, self._poll_loading)

    def _on_data_loaded(self):
        self._fetching = False
        self._update_view()
        # Schedule auto-refresh every hour
        self._schedule_auto_refresh()

    def _schedule_auto_refresh(self):
        """Silently re-fetch all data every AUTO_REFRESH_MS milliseconds."""
        if hasattr(self, "_auto_refresh_id") and self._auto_refresh_id:
            self.root.after_cancel(self._auto_refresh_id)
        self._auto_refresh_id = self.root.after(AUTO_REFRESH_MS, self._do_auto_refresh)

    def _do_auto_refresh(self):
        if not self.data.is_loaded():
            self._schedule_auto_refresh()
            return
        if self._fetching:
            # Retry in 30 seconds
            self._auto_refresh_id = self.root.after(30000, self._do_auto_refresh)
            return
        self._status_lbl.configure(text="Auto-refreshing...", fg=YELLOW)
        self._start_loading(force=True)

    # -- Tab switching ------------------------------------------------------
    def _select_tab(self, name, update_view=True):
        self._current_tab = name
        for tname, lbl in self._tab_labels.items():
            if tname == name:
                lbl.configure(fg=ACCENT, bg=BG)
            else:
                lbl.configure(fg=FG_DIM, bg=BG)

        if update_view:
            self._update_view()

    # -- View update --------------------------------------------------------
    def _update_view(self):
        if not self.data.is_loaded():
            return

        is_rental = self._current_tab == "Rentals"
        is_ships = self._current_tab == "Ships"

        # Hide all tables first
        self._item_table.pack_forget()
        self._rental_table.pack_forget()
        self._ship_table.pack_forget()

        query = (self._search_var.get() if hasattr(self, "_search_var") else "").lower().strip()

        if is_rental:
            self._rental_table.pack(fill="both", expand=True)
            vehicles = self.data.vehicles
            if query:
                vehicles = [v for v in vehicles
                            if query in (v.get("name") or "").lower()
                            or query in (v.get("company_name") or "").lower()]
            self._rental_table.set_vehicles(vehicles)
        elif is_ships:
            self._ship_table.pack(fill="both", expand=True)
            vehicles = self.data.vehicles
            if query:
                vehicles = [v for v in vehicles
                            if query in (v.get("name") or "").lower()
                            or query in (v.get("name_full") or "").lower()
                            or query in (v.get("company_name") or "").lower()]
            self._ship_table.set_vehicles(vehicles)
        else:
            self._item_table.pack(fill="both", expand=True)
            filtered = self._get_filtered_items()
            self._item_table.set_items(filtered)

    def _get_filtered_items(self) -> list[dict]:
        tab = self._current_tab
        # Always read directly from the entry widget for live filtering
        query = (self._search_var.get() if hasattr(self, "_search_var") else self._search_text).lower().strip()

        # Use pre-indexed tab lookup (instant, no iteration)
        items_by_tab = getattr(self.data, "items_by_tab", None)
        if items_by_tab:
            items = items_by_tab.get(tab, items_by_tab.get("All", []))
        else:
            # Fallback if index not built yet
            items = self.data.items
            if tab != "All":
                items = [it for it in items if _item_tab(it) == tab]

        # Filter by search (uses pre-built _search_index)
        if query:
            si = getattr(self.data, "_search_index", {})
            items = [it for it in items if query in si.get(it.get("id"), "")]

        return items

    # -- Search handling ----------------------------------------------------
    def _on_search_changed(self, *_args):
        if self._search_after_id:
            self.root.after_cancel(self._search_after_id)
        # Update table immediately (filtering uses pre-built _search field, so it's instant)
        self._search_text = self._search_var.get().strip()
        self._update_view()
        # Debounce the search bubble (popup) since it's heavier
        self._search_after_id = self.root.after(300, self._do_search_bubble)

    def _do_search_bubble(self):
        """Debounced: show/hide the search result bubble popup."""
        text = self._search_var.get().strip()
        if len(text) >= 2 and self.data.is_loaded():
            results = self._get_search_results(text.lower())
            if results:
                self._show_bubble(results)
            else:
                self._dismiss_bubble()
        else:
            self._dismiss_bubble()

    def _do_search(self):
        """Legacy compatibility — called from JSONL command dispatch."""
        text = self._search_var.get().strip()
        self._search_text = text
        self._update_view()
        self._do_search_bubble()

    def _get_search_results(self, query: str) -> list[dict]:
        matches = []
        for it in self.data.items:
            if query in (it.get("name") or "").lower():
                matches.append(it)
                if len(matches) >= 30:
                    break
        return matches

    def _show_bubble(self, results):
        self._dismiss_bubble()
        self._bubble = SearchBubble(self.root, results, self._on_bubble_select)
        self._bubble.position_below(self._search_entry)

    def _dismiss_bubble(self, _event=None):
        if self._bubble:
            try:
                self._bubble.destroy()
            except Exception as exc:
                print(f"[MarketFinder] Bubble destroy failed: {exc}")
            self._bubble = None

    def _on_bubble_select(self, item):
        self._dismiss_bubble()
        tab = _item_tab(item)
        self._select_tab(tab)
        self._detail_panel.show_item(item)

    def _on_search_enter(self, _event=None):
        self._dismiss_bubble()
        self._do_search()

    # -- Item selection -----------------------------------------------------
    def _on_item_select(self, item: dict):
        self._detail_panel.show_item(item)

    def _on_ship_select(self, vehicle: dict):
        self._detail_panel.show_ship(vehicle)

    # -- JSONL command protocol ---------------------------------------------
    def _poll_commands(self):
        if not self._cmd_file or not os.path.exists(self._cmd_file):
            self.root.after(500, self._poll_commands)
            return

        try:
            commands = ipc_read_and_clear(self._cmd_file)
            for cmd in commands:
                self._handle_command(cmd)
        except Exception as exc:
            log.warning("Command poll error: %s", exc)
        self.root.after(500, self._poll_commands)

    def _handle_command(self, cmd: dict):
        action = cmd.get("type", cmd.get("action", ""))
        if action == "show":
            self.root.deiconify()
            self.root.lift()
        elif action == "hide":
            self.root.withdraw()
        elif action == "quit":
            self.root.quit()
        elif action == "search":
            query = cmd.get("query", "")
            self._search_var.set(query)
        elif action == "tab":
            tab = cmd.get("tab", "All")
            self._select_tab(tab)
        elif action == "refresh":
            self._refresh_data()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    set_dpi_awareness()

    parsed = parse_cli_args(sys.argv[1:], {"w": 1100, "h": 720})
    x = parsed["x"]
    y = parsed["y"]
    w = parsed["w"]
    h = parsed["h"]
    opacity = parsed["opacity"]
    cmd_file = parsed["cmd_file"]

    root = tk.Tk()

    # Dark title bar on Windows
    try:
        from ctypes import windll
        root.update()
        hwnd = windll.user32.GetParent(root.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            __import__("ctypes").byref(__import__("ctypes").c_int(1)),
            __import__("ctypes").sizeof(__import__("ctypes").c_int),
        )
    except Exception as exc:
        print(f"[MarketFinder] Dark mode DWM attribute failed: {exc}")

    app = MarketFinderApp(root, x, y, w, h, opacity, cmd_file)
    root.mainloop()


if __name__ == "__main__":
    main()
