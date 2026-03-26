#!/usr/bin/env python3
"""
Trade Hub — standalone GUI process.
Launched by the WingmanAI skill via subprocess using the system Python.
Fetches trade data from the UEX API.
Receives control commands from the parent via stdin (one JSON line per command).
Requires Python stdlib + tkinter.  uex_client.py also needs the `requests` package.
"""
import json
import logging
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import urllib.request
from dataclasses import dataclass, field
from logging.handlers import RotatingFileHandler
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

# Allow imports from the shared package two levels up
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from shared.ships import SHIP_PRESETS, scu_for_ship, QUICK_SHIPS  # noqa: E402
from shared.theme import COLORS  # noqa: E402
from shared.data_utils import retry_request, parse_cli_args  # noqa: E402

# Platform-guarded Win32 imports
if sys.platform == 'win32':
    import ctypes
    import ctypes.wintypes
else:
    ctypes = None

# ── Logging setup ─────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_hub.log")

def _setup_log() -> logging.Logger:
    lg = logging.getLogger("TradeHub")
    lg.setLevel(logging.DEBUG)
    if not lg.handlers:
        fh = RotatingFileHandler(_LOG_PATH, maxBytes=1_500_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        lg.addHandler(fh)
    return lg

log = _setup_log()
log.info("=" * 60)
log.info("Trade Hub starting — Python %s", sys.version.split()[0])
log.info("Script: %s", os.path.abspath(__file__))

# ── Win32 constants ───────────────────────────────────────────────────────────
if sys.platform == 'win32':
    _user32         = ctypes.windll.user32
    _kernel32       = ctypes.windll.kernel32
else:
    _user32 = _kernel32 = None
_HWND_TOPMOST   = -1
_SWP_NOSIZE     = 0x0001
_SWP_NOMOVE     = 0x0002
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE     = 9

# ── Global hotkey constants ────────────────────────────────────────────────────
_WM_HOTKEY   = 0x0312
_PM_REMOVE   = 0x0001
_MOD_ALT     = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT   = 0x0004
_MOD_WIN     = 0x0008
_VK_MAP: Dict[str, int] = {
    **{c: 0x41 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    **{str(i): 0x30 + i for i in range(10)},
    "F1":  0x70, "F2":  0x71, "F3":  0x72, "F4":  0x73,
    "F5":  0x74, "F6":  0x75, "F7":  0x76, "F8":  0x77,
    "F9":  0x78, "F10": 0x79, "F11": 0x7A, "F12": 0x7B,
    "HOME": 0x24, "END": 0x23, "PGUP": 0x21, "PGDN": 0x22,
    "INS": 0x2D, "DEL": 0x2E, "SPACE": 0x20, "TAB": 0x09,
}
_DEFAULT_HOTKEY = "ctrl+shift+t"


def _parse_hotkey(hk: str) -> Tuple[int, int]:
    """Parse e.g. 'ctrl+shift+t' → (modifier_flags, vk_code). (0, 0) on error."""
    mods = 0
    vk   = 0
    for part in hk.upper().split("+"):
        part = part.strip()
        if   part in ("CTRL", "CONTROL"): mods |= _MOD_CONTROL
        elif part == "SHIFT":             mods |= _MOD_SHIFT
        elif part == "ALT":               mods |= _MOD_ALT
        elif part in ("WIN", "WINDOWS"):  mods |= _MOD_WIN
        else:                             vk    = _VK_MAP.get(part, 0)
    return mods, vk

UEX_BASE = "https://api.uexcorp.space/2.0"

# ── Persistent config ─────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_hub_config.json")

# ── Colour palette ────────────────────────────────────────────────────────────
# COLORS (colour palette) imported from shared.theme
# SHIP_PRESETS, scu_for_ship, QUICK_SHIPS imported from shared.ships

# ── Column definitions ────────────────────────────────────────────────────────
COLUMNS: List[Tuple[str, str, int, str]] = [
    ("commodity",       "Item",          120, "w"),
    ("buy_terminal",    "Buy At",        148, "w"),
    ("cs_origin",       "CS",             46, "center"),
    ("investment",      "Invest...",      90, "e"),
    ("available_scu",   "SCU",            58, "e"),
    ("scu_user_origin", "SCU-U",          58, "e"),
    ("sell_terminal",   "Sell At",       148, "w"),
    ("cs_dest",         "CS",             46, "center"),
    ("invest_dest",     "Invest...",      90, "e"),
    ("scu_demand",      "SCU-C",          58, "e"),
    ("scu_user_dest",   "SCU-U",          58, "e"),
    ("distance",        "Distance",       72, "e"),
    ("eta",             "ETA",            60, "e"),
    ("roi",             "ROI",            58, "e"),
    ("est_profit",      "Income",        100, "e"),
]

COLUMN_KEYS = tuple(c[0] for c in COLUMNS)

# ── Multi-leg route column definitions ────────────────────────────────────────
LOOP_COLUMNS: List[Tuple[str, str, int, str]] = [
    ("origin",       "Origin Terminal",  175, "w"),
    ("origin_sys",   "Sys",               65, "w"),
    ("legs",         "Legs",              42, "e"),
    ("commodities",  "Commodity Chain",  265, "w"),
    ("avail",        "Min Avail SCU",     95, "e"),
    ("total_profit", "Est. Total Profit",145, "e"),
]

LOOP_COLUMN_KEYS = tuple(c[0] for c in LOOP_COLUMNS)

# ── Route data ────────────────────────────────────────────────────────────────

@dataclass
class Route:
    commodity:     str   = ""
    buy_terminal:  str   = ""
    buy_location:  str   = ""
    buy_system:    str   = ""
    sell_terminal: str   = ""
    sell_location: str   = ""
    sell_system:   str   = ""
    scu_available: int   = 0
    scu_demand:    int   = 0
    price_buy:     float = 0.0
    price_sell:    float = 0.0
    margin:        float = 0.0
    score:         float = 0.0
    investment:    float = 0.0
    profit:        float = 0.0
    price_roi:     float = 0.0
    distance:      float = 0.0
    container_sizes_origin: str = ""
    container_sizes_destination: str = ""
    scu_user_origin: int = 0
    scu_user_destination: int = 0

    def effective_scu(self, ship_scu: int) -> int:
        if ship_scu <= 0:
            cap = 0  # no ship cap — use available stock
        else:
            cap = ship_scu
        if self.scu_available > 0 and self.scu_demand > 0:
            effective = min(cap, self.scu_available, self.scu_demand) if cap > 0 else min(self.scu_available, self.scu_demand)
        else:
            stock = max(self.scu_available, self.scu_demand)
            effective = min(cap, stock) if cap > 0 else stock
        return effective

    def estimated_profit(self, ship_scu: int) -> float:
        return self.effective_scu(ship_scu) * self.margin

    def roi(self) -> float:
        """Return on investment as a percentage."""
        if self.price_buy <= 0:
            return 0.0
        return (self.margin / self.price_buy) * 100.0


@dataclass
class MultiRoute:
    """A multi-leg trade route chain: sell-terminal of leg N == buy-terminal of leg N+1."""
    legs: List[Route]

    def total_profit(self, ship_scu: int) -> float:
        return sum(r.estimated_profit(ship_scu) for r in self.legs)

    def total_investment(self, ship_scu: int) -> float:
        return sum(r.effective_scu(ship_scu) * r.price_buy for r in self.legs)

    def roi_pct(self, ship_scu: int) -> float:
        inv = self.total_investment(ship_scu)
        return (self.total_profit(ship_scu) / inv * 100.0) if inv > 0 else 0.0

    def avg_margin(self) -> float:
        return sum(r.margin for r in self.legs) / len(self.legs) if self.legs else 0.0

    def min_avail(self) -> int:
        return min(r.scu_available for r in self.legs) if self.legs else 0

    def commodity_chain(self) -> str:
        return " › ".join(r.commodity for r in self.legs)

    @property
    def start_terminal(self) -> str:
        return self.legs[0].buy_terminal if self.legs else ""

    @property
    def start_system(self) -> str:
        return self.legs[0].buy_system if self.legs else ""

    @property
    def end_terminal(self) -> str:
        return self.legs[-1].sell_terminal if self.legs else ""

    @property
    def num_legs(self) -> int:
        return len(self.legs)


def _safe(d: dict, key: str, default=""):
    v = d.get(key)
    return v if v is not None else default


def _best_loc_api(r: dict, suffix: str) -> str:
    for key in (f"outpost_{suffix}", f"city_{suffix}", f"space_station_{suffix}",
                f"moon_{suffix}", f"planet_{suffix}", f"star_system_{suffix}"):
        v = (r.get(key) or "").strip()
        if v:
            return v
    return ""


def route_from_api(r: dict) -> Optional[Route]:
    margin = float(r.get("profit_margin", 0) or r.get("margin", 0) or 0)
    buy    = float(r.get("price_origin",  0) or r.get("price_buy",  0) or 0)
    sell   = float(r.get("price_destination", 0) or r.get("price_sell", 0) or 0)
    if margin == 0 and sell > buy:
        margin = sell - buy
    commodity = _safe(r, "commodity_name")
    if not commodity or margin <= 0:
        return None
    # Container sizes — API may return e.g. "1,2,8,16" or "1-16" style
    cs_origin = _safe(r, "container_sizes_origin")
    cs_dest   = _safe(r, "container_sizes_destination")
    return Route(
        commodity    = commodity,
        buy_terminal = (_safe(r, "terminal_origin") or _safe(r, "terminal_name_origin")),
        buy_location = _best_loc_api(r, "origin"),
        buy_system   = _safe(r, "star_system_origin"),
        sell_terminal= (_safe(r, "terminal_destination") or _safe(r, "terminal_name_destination")),
        sell_location= _best_loc_api(r, "destination"),
        sell_system  = _safe(r, "star_system_destination"),
        scu_available= int(_safe(r, "scu_origin",      0) or _safe(r, "scu_buy",  0) or 0),
        scu_demand   = int(_safe(r, "scu_destination", 0) or _safe(r, "scu_sell", 0) or 0),
        price_buy    = buy,
        price_sell   = sell,
        margin       = margin,
        score        = float(r.get("score", 0) or 0),
        investment   = float(r.get("investment", 0) or 0),
        profit       = float(r.get("profit", 0) or 0),
        price_roi    = float(r.get("price_roi", 0) or 0),
        distance     = float(r.get("distance", 0) or 0),
        container_sizes_origin      = str(cs_origin) if cs_origin else "",
        container_sizes_destination = str(cs_dest) if cs_dest else "",
        scu_user_origin      = int(r.get("scu_origin_users", 0) or 0),
        scu_user_destination = int(r.get("scu_destination_users", 0) or 0),
    )



# ── Filter & sort ─────────────────────────────────────────────────────────────

@dataclass
class FilterState:
    # Generic / voice-command filters
    system:         str   = ""
    location:       str   = ""
    commodity:      str   = ""
    search:         str   = ""
    min_margin_scu: float = 0.0
    # Sidebar specific filters
    buy_system:     str   = ""
    sell_system:    str   = ""
    buy_location:   str   = ""
    sell_location:  str   = ""
    buy_terminal:   str   = ""
    sell_terminal:  str   = ""
    min_scu:        int   = 0


def apply_filters(routes: List[Route], f: FilterState) -> List[Route]:
    result = routes
    # Generic system filter (matches either side)
    if f.system:
        s = f.system.lower()
        result = [r for r in result if s in r.buy_system.lower() or s in r.sell_system.lower()]
    # Side-specific system filters
    if f.buy_system:
        s = f.buy_system.lower()
        result = [r for r in result if s in r.buy_system.lower()]
    if f.sell_system:
        s = f.sell_system.lower()
        result = [r for r in result if s in r.sell_system.lower()]
    # Generic location filter (matches any field)
    if f.location:
        loc = f.location.lower()
        result = [r for r in result if any(loc in x.lower() for x in [
            r.buy_location, r.buy_terminal, r.buy_system,
            r.sell_location, r.sell_terminal, r.sell_system])]
    # Side-specific location/terminal filters
    if f.buy_location:
        bl = f.buy_location.lower()
        result = [r for r in result if bl in r.buy_location.lower() or bl in r.buy_terminal.lower()]
    if f.sell_location:
        sl = f.sell_location.lower()
        result = [r for r in result if sl in r.sell_location.lower() or sl in r.sell_terminal.lower()]
    if f.buy_terminal:
        bt = f.buy_terminal.lower()
        result = [r for r in result if bt in r.buy_terminal.lower()]
    if f.sell_terminal:
        st = f.sell_terminal.lower()
        result = [r for r in result if st in r.sell_terminal.lower()]
    if f.commodity:
        c = f.commodity.lower()
        result = [r for r in result if c in r.commodity.lower()]
    if f.search:
        q = f.search.lower()
        result = [r for r in result if any(q in x.lower() for x in [
            r.commodity, r.buy_location, r.buy_terminal, r.buy_system,
            r.sell_location, r.sell_terminal, r.sell_system])]
    if f.min_margin_scu > 0:
        result = [r for r in result if r.margin >= f.min_margin_scu]
    if f.min_scu > 0:
        result = [r for r in result if r.scu_available >= f.min_scu]
    return result


def sort_routes(routes: List[Route], col: str, reverse: bool, ship_scu: int = 0) -> List[Route]:
    km = {
        "commodity":       lambda r: r.commodity.lower(),
        "buy_terminal":    lambda r: r.buy_terminal.lower(),
        "sell_terminal":   lambda r: r.sell_terminal.lower(),
        "cs_origin":       lambda r: r.container_sizes_origin,
        "cs_dest":         lambda r: r.container_sizes_destination,
        "investment":      lambda r: r.price_buy * r.effective_scu(ship_scu),
        "invest_dest":     lambda r: r.price_sell * r.effective_scu(ship_scu),
        "available_scu":   lambda r: r.effective_scu(ship_scu),
        "scu_user_origin": lambda r: r.scu_user_origin,
        "scu_demand":      lambda r: r.scu_demand,
        "scu_user_dest":   lambda r: r.scu_user_destination,
        "distance":        lambda r: r.distance,
        "eta":             lambda r: r.distance,
        "roi":             lambda r: r.roi(),
        "est_profit":      lambda r: r.estimated_profit(ship_scu),
    }
    return sorted(routes, key=km.get(col, lambda r: r.score), reverse=reverse)


def find_multi_routes(routes: List[Route], ship_scu: int = 0,
                      max_steps: int = 3, top_k: int = 300) -> List[MultiRoute]:
    """Find best N-step trade routes using greedy best-first search.

    Builds a terminal adjacency graph from all profitable routes.  From every
    starting terminal the algorithm greedily extends to the highest-profit next
    hop (profit = margin × effective_scu), avoiding revisiting intermediate
    terminals but allowing a return to the origin terminal to close a loop.

    This mirrors Cabal's deterministic best-first search so the two tools
    agree on which routes are profitable.
    """
    if not routes:
        return []

    # Build adjacency: buy_terminal → routes sorted by estimated_profit desc
    adj: Dict[str, List[Route]] = {}
    for r in routes:
        if r.buy_terminal and r.sell_terminal and r.margin > 0:
            adj.setdefault(r.buy_terminal, []).append(r)
    for t in adj:
        # Sort by profit for current ship; fall back to margin × capped SCU
        if ship_scu > 0:
            adj[t].sort(key=lambda r: r.estimated_profit(ship_scu), reverse=True)
        else:
            adj[t].sort(key=lambda r: r.margin * min(r.scu_available, 5000), reverse=True)

    seen_sigs: set = set()
    candidates: List[MultiRoute] = []

    for start_terminal, outgoing in adj.items():
        # Try the top 5 starting commodities from each terminal for variety
        for start_route in outgoing[:5]:
            path = [start_route.buy_terminal, start_route.sell_terminal]
            legs: List[Route] = [start_route]
            current = start_route.sell_terminal

            for _ in range(max_steps - 1):
                options = adj.get(current, [])
                # Never revisit an intermediate terminal already in the path
                # (allow returning to the very first terminal to close a loop)
                intermediates = set(path[1:])   # excludes path[0] = start
                options = [r for r in options
                           if r.sell_terminal not in intermediates
                           and r.sell_terminal != current]
                if not options:
                    break
                best = options[0]
                legs.append(best)
                path.append(best.sell_terminal)
                current = best.sell_terminal

            sig = "->".join(f"{r.buy_terminal}:{r.commodity}" for r in legs)
            if sig in seen_sigs:
                continue
            seen_sigs.add(sig)
            candidates.append(MultiRoute(legs=list(legs)))

    # Sort by total estimated profit (ship-aware if ship_scu given)
    candidates.sort(key=lambda m: m.total_profit(ship_scu), reverse=True)
    return candidates[:top_k]


def sort_multi_routes(multi: List[MultiRoute], col: str, reverse: bool,
                      ship_scu: int = 0) -> List[MultiRoute]:
    km: Dict[str, Any] = {
        "origin":       lambda m: m.start_terminal.lower(),
        "origin_sys":   lambda m: m.start_system.lower(),
        "legs":         lambda m: m.num_legs,
        "commodities":  lambda m: m.commodity_chain().lower(),
        "avail":        lambda m: m.min_avail(),
        "total_profit": lambda m: m.total_profit(ship_scu),
    }
    return sorted(multi, key=km.get(col, lambda m: m.total_profit(ship_scu)), reverse=reverse)


def profit_tier(margin: float) -> str:
    return "high" if margin >= 1000 else ("med" if margin >= 300 else "low")


def get_unique_commodities(routes: List[Route]) -> List[str]:
    return sorted({r.commodity for r in routes if r.commodity})


## scu_for_ship imported from shared.ships

# ── Data fetcher ──────────────────────────────────────────────────────────────

class DataFetcher:
    def __init__(self, refresh_interval: float = 300.0):
        self.refresh_interval = refresh_interval

    def fetch_async(self, callback, on_error=None) -> None:
        threading.Thread(target=self._worker, args=(callback, on_error),
                         daemon=True, name="TradeHubFetch").start()

    def _worker(self, callback, on_error=None) -> None:
        routes, source = self._fetch()
        try:
            callback(routes, source)
        except Exception:
            log.warning("Fetch callback failed: %s", traceback.format_exc())
            if on_error:
                try:
                    on_error()
                except Exception:
                    pass

    def _fetch(self):
        try:
            routes = self._fetch_api()
            return routes, "UEX API"
        except Exception as exc:
            return [], f"Error: {exc}"

    @staticmethod
    def _fetch_api() -> List[Route]:
        """Fetch all prices + terminals from UEX, compute routes client-side.
        Uses commodities_prices_all (single call) + terminals + commodities.
        """
        headers = {"User-Agent": "WingmanAI-TradeHub/1.0", "Accept": "application/json"}

        def _get(path):
            url = f"{UEX_BASE}/{path}"
            req = urllib.request.Request(url, headers=headers)
            def _do_request():
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return json.loads(resp.read()).get("data", [])
            return retry_request(_do_request, retries=1, backoff=1.0)

        # Fetch all data in parallel-ish (sequential but fast — 3 calls total)
        prices = _get("commodities_prices_all")
        terminals = {t["id"]: t for t in _get("terminals")}
        commodities = {c["id"]: c for c in _get("commodities")}

        # Group prices by commodity: {id_commodity: {"buys": [...], "sells": [...]}}
        by_commodity: Dict[int, Dict[str, list]] = {}
        for p in prices:
            cid = p.get("id_commodity", 0)
            if not cid:
                continue
            entry = by_commodity.setdefault(cid, {"buys": [], "sells": []})
            pb = p.get("price_buy", 0) or 0
            ps = p.get("price_sell", 0) or 0
            if pb > 0:
                entry["buys"].append(p)
            if ps > 0:
                entry["sells"].append(p)

        def _loc(t):
            """Return best location name from a terminal dict."""
            for k in ("city_name", "space_station_name", "outpost_name",
                      "moon_name", "planet_name"):
                v = (t.get(k) or "").strip()
                if v:
                    return v
            return ""

        # Compute routes: for each commodity, match every buy terminal with every sell terminal
        routes: List[Route] = []
        for cid, data in by_commodity.items():
            comm = commodities.get(cid, {})
            comm_name = comm.get("name", f"Commodity {cid}")

            for buy in data["buys"]:
                for sell in data["sells"]:
                    # Skip same terminal
                    bt_id = buy.get("id_terminal", 0)
                    st_id = sell.get("id_terminal", 0)
                    if bt_id == st_id:
                        continue

                    pb = float(buy.get("price_buy", 0) or 0)
                    ps = float(sell.get("price_sell", 0) or 0)
                    if ps <= pb or pb <= 0:
                        continue  # no profit

                    margin = ps - pb
                    scu_avail = int(buy.get("scu_buy", buy.get("scu_buy_avg", 0)) or 0)
                    scu_demand = int(sell.get("scu_sell", sell.get("scu_sell_avg", 0)) or 0)
                    if scu_avail <= 0 and scu_demand <= 0:
                        continue
                    scu = min(scu_avail, scu_demand) if scu_avail > 0 and scu_demand > 0 else max(scu_avail, scu_demand)
                    if scu <= 0:
                        continue  # skip phantom routes with no supply/demand

                    investment = pb * scu
                    profit = margin * scu
                    roi = (margin / pb * 100) if pb > 0 else 0

                    bt = terminals.get(bt_id, {})
                    st = terminals.get(st_id, {})

                    r = Route(
                        commodity=comm_name,
                        buy_terminal=bt.get("name", bt.get("displayname", f"T{bt_id}")),
                        buy_location=_loc(bt),
                        buy_system=bt.get("star_system_name", ""),
                        sell_terminal=st.get("name", st.get("displayname", f"T{st_id}")),
                        sell_location=_loc(st),
                        sell_system=st.get("star_system_name", ""),
                        scu_available=scu,
                        scu_demand=scu_demand,
                        price_buy=pb,
                        price_sell=ps,
                        margin=margin,
                        score=profit,  # use profit as score
                        investment=investment,
                        profit=profit,
                        price_roi=roi,
                        distance=0,  # UEX prices_all doesn't include distance
                        container_sizes_origin=buy.get("container_sizes", ""),
                        container_sizes_destination=sell.get("container_sizes", ""),
                        scu_user_origin=int(buy.get("scu_buy_users", 0) or 0),
                        scu_user_destination=int(sell.get("scu_sell_users", 0) or 0),
                    )
                    routes.append(r)

        routes.sort(key=lambda r: r.profit, reverse=True)
        # Limit to top routes to avoid overwhelming the UI
        return routes[:5000]

# ── GUI window ────────────────────────────────────────────────────────────────

class TradeHubWindow:
    def __init__(self, cmd_queue: queue.Queue, win_x=80, win_y=80,
                 win_w=1400, win_h=900, refresh_interval=300.0,
                 max_routes=500, opacity=0.95):
        self.cmd_queue        = cmd_queue
        self.win_x            = win_x
        self.win_y            = win_y
        self.win_w            = win_w
        self.win_h            = win_h
        self.refresh_interval = refresh_interval
        self.max_routes       = max_routes
        self.opacity          = max(0.3, min(1.0, opacity))
        self.fetcher          = DataFetcher(refresh_interval)

        self.all_routes:      List[Route] = []
        self.filtered_routes: List[Route] = []
        self.sort_col         = "est_profit"
        self.sort_reverse     = True
        self.ship_name        = ""
        self.ship_scu         = 0
        self.filters          = FilterState()
        self.last_refresh_ts: Optional[float] = None
        self._data_source     = "—"
        self._debounce_id     = None

        self._drag_x = self._drag_y = 0
        self._resizing = False
        self._resize_sx = self._resize_sy = 0
        self._resize_sw = self._resize_sh = 0

        self.root:         Optional[tk.Tk]        = None
        self.tree:         Optional[ttk.Treeview] = None
        self.status_var:      Optional[tk.StringVar] = None
        self.count_var:       Optional[tk.StringVar] = None
        self.upd_label:       Optional[tk.Label]     = None
        self.src_label:       Optional[tk.Label]     = None
        # Sidebar filter vars
        self.buy_system_var:  Optional[tk.StringVar] = None
        self.sell_system_var: Optional[tk.StringVar] = None
        self.buy_loc_var:     Optional[tk.StringVar] = None
        self.sell_loc_var:    Optional[tk.StringVar] = None
        self.buy_term_var:    Optional[tk.StringVar] = None
        self.sell_term_var:   Optional[tk.StringVar] = None
        self.minscu_var:      Optional[tk.StringVar] = None
        self.search_var:   Optional[tk.StringVar] = None
        self.ship_var:     Optional[tk.StringVar] = None
        self.system_var:   Optional[tk.StringVar] = None
        self.location_var: Optional[tk.StringVar] = None
        self.comm_var:     Optional[tk.StringVar] = None
        self.comm_combo:     Optional[ttk.Combobox] = None
        self.buy_sys_combo:  Optional[ttk.Combobox] = None
        self.sell_sys_combo: Optional[ttk.Combobox] = None
        self.buy_loc_combo:  Optional[ttk.Combobox] = None
        self.sell_loc_combo: Optional[ttk.Combobox] = None
        self.buy_term_combo: Optional[ttk.Combobox] = None
        self.sell_term_combo:Optional[ttk.Combobox] = None
        self.minprofit_var:  Optional[tk.StringVar] = None
        # Data source (always UEX)
        self.data_source:    str       = "UEX"
        # View mode (ROUTES or LOOPS)
        self.view_mode:      str       = "ROUTES"
        self._btn_routes:    Optional[tk.Button] = None
        self._btn_loops:     Optional[tk.Button] = None
        # Loop data
        self.all_loops:      List[MultiRoute] = []
        self.filtered_loops: List[MultiRoute] = []
        self.loop_sort_col     = "total_profit"
        self.loop_sort_reverse = True
        # Separate treeview frames for routes vs loops
        self._route_tbl_frame:  Optional[tk.Frame]       = None
        self._loop_tbl_frame:   Optional[tk.Frame]       = None
        self._route_tree:       Optional[ttk.Treeview]   = None
        self._loop_tree:        Optional[ttk.Treeview]   = None
        # Pinned route/loop detail cards — up to MAX_PINNED simultaneously
        # Each entry: {"key": str, "popup": Toplevel, "text": Text,
        #              "type": "loop"|"route", "data": MultiRoute|Route}
        self._pinned_cards: List[Dict] = []
        # Window visibility tracking (wm_state() unreliable with overrideredirect)
        self._visible:     bool                  = True
        # Global hotkey
        self._hotkey:      str                   = _DEFAULT_HOTKEY
        self._hotkey_stop: Optional[threading.Event] = None
        self._hotkey_thread: Optional[threading.Thread] = None
        # Profit calculator popup (singleton)
        self._calc_popup:       Optional[tk.Toplevel]  = None
        # Hotkey settings entry var (sidebar)
        self._hotkey_entry_var: Optional[tk.StringVar] = None
        self._hotkey_status_var: Optional[tk.StringVar] = None

    # ── Entry ─────────────────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            self._run_inner()
        except Exception:
            tb = traceback.format_exc()
            log.critical("FATAL — unhandled exception:\n%s", tb)
            try:
                p = os.path.join(tempfile.gettempdir(), "trade_hub_error.txt")
                with open(p, "w") as f:
                    f.write(tb)
            except Exception:
                log.debug("Could not write crash file: %s", traceback.format_exc())

    def _run_inner(self) -> None:
        log.info("Window init — geometry %dx%d+%d+%d  opacity=%.2f  hotkey=%s",
                 self.win_w, self.win_h, self.win_x, self.win_y,
                 self.opacity, self._hotkey)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
        self.root.configure(bg=COLORS["bg"])
        self.root.wm_attributes("-alpha", self.opacity)
        self.root.wm_attributes("-topmost", True)

        self._setup_styles()
        self._build_ui()
        self._load_config()
        self._start_hotkey_listener()

        self.root.update_idletasks()
        self.root.deiconify()
        self.root.lift()
        self.root.update()
        self._force_show()

        self._poll_queue()
        self._auto_refresh_loop()
        self._keepalive()

        self.root.after(500, self._start_load)
        log.info("Entering mainloop")
        self.root.mainloop()
        log.info("mainloop exited")

    # ── Win32 ─────────────────────────────────────────────────────────────────

    def _get_hwnd(self) -> Optional[int]:
        try:
            frame = self.root.wm_frame()
            try:
                return int(frame)
            except ValueError:
                return int(frame, 16)
        except Exception:
            try:
                return self.root.winfo_id()
            except Exception:
                return None

    def _apply_topmost(self) -> None:
        if not _user32:
            return
        hwnd = self._get_hwnd()
        if hwnd:
            try:
                _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                     _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
            except Exception:
                log.debug("SetWindowPos topmost failed: %s", traceback.format_exc())

    def _force_show(self) -> None:
        if not _user32:
            return
        hwnd = self._get_hwnd()
        if not hwnd:
            return
        try:
            _user32.ShowWindow(hwnd, _SW_RESTORE)
            _user32.BringWindowToTop(hwnd)
            fg   = _user32.GetForegroundWindow()
            ftid = _user32.GetWindowThreadProcessId(fg, None)
            otid = _kernel32.GetCurrentThreadId()
            if ftid and ftid != otid:
                _user32.AttachThreadInput(ftid, otid, True)
                _user32.SetForegroundWindow(hwnd)
                _user32.AttachThreadInput(ftid, otid, False)
            else:
                _user32.SetForegroundWindow(hwnd)
        except Exception:
            log.debug("_force_show Win32 call failed: %s", traceback.format_exc())
        self._apply_topmost()

    def _keepalive(self) -> None:
        if self.root:
            if self._visible:
                self._apply_topmost()
            self.root.after(2000, self._keepalive)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TFrame",  background=COLORS["bg"])
        s.configure("TLabel",  background=COLORS["bg"], foreground=COLORS["fg"], font=("Consolas", 9))
        s.configure("T.Treeview",
                    background=COLORS["even"], foreground=COLORS["fg"],
                    fieldbackground=COLORS["even"], rowheight=20,
                    font=("Consolas", 9), borderwidth=0, relief="flat")
        s.configure("T.Treeview.Heading",
                    background=COLORS["hdr"], foreground=COLORS["blue"],
                    font=("Consolas", 9, "bold"), relief="flat", borderwidth=0)
        s.map("T.Treeview",
              background=[("selected", COLORS["sel"])],
              foreground=[("selected", COLORS["fg3"])])
        s.map("T.Treeview.Heading",
              background=[("active", COLORS["blue2"])],
              foreground=[("active", COLORS["blue"])])
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"T.{orient}.TScrollbar",
                        background=COLORS["bg2"], troughcolor=COLORS["bg"],
                        arrowcolor=COLORS["blue2"], borderwidth=0, relief="flat")
        s.configure("T.TEntry",
                    fieldbackground=COLORS["ibg"], foreground=COLORS["ifg"],
                    insertcolor=COLORS["blue"], borderwidth=0, relief="flat", font=("Consolas", 9))
        s.configure("T.TCombobox",
                    fieldbackground=COLORS["ibg"], foreground=COLORS["ifg"],
                    background=COLORS["btn"], arrowcolor=COLORS["blue"],
                    selectbackground=COLORS["sel"], borderwidth=0, font=("Consolas", 9))
        s.map("T.TCombobox",
              fieldbackground=[("readonly", COLORS["ibg"])],
              selectbackground=[("readonly", COLORS["sel"])])
        s.configure("T.TButton",
                    background=COLORS["btn"], foreground=COLORS["blue"],
                    font=("Consolas", 9), borderwidth=0, relief="flat", padding=(6, 2))
        s.map("T.TButton", background=[("active", COLORS["blue2"])])

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Title bar ─────────────────────────────────────────────────────────
        bar = tk.Frame(self.root, bg=COLORS["bar"], height=42)
        bar.pack(fill="x"); bar.pack_propagate(False)

        def drag(w):
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_motion)

        drag(bar)
        t = tk.Label(bar, text="◈  TRADE HUB", bg=COLORS["bar"], fg=COLORS["blue"],
                     font=("Consolas", 14, "bold"), cursor="fleur")
        t.pack(side="left", padx=12, pady=9); drag(t)
        s = tk.Label(bar, text="ECONOMIC INTELLIGENCE TERMINAL",
                     bg=COLORS["bar"], fg=COLORS["fg2"], font=("Consolas", 8), cursor="fleur")
        s.pack(side="left", pady=13); drag(s)
        close = tk.Label(bar, text=" ✕ ", bg=COLORS["bar"], fg=COLORS["blue"],
                         font=("Consolas", 11, "bold"), cursor="hand2")
        close.pack(side="right", padx=(4, 10), pady=8)
        close.bind("<Button-1>", lambda _: (self.root.withdraw(), setattr(self, '_visible', False)))
        close.bind("<Enter>",    lambda _: close.config(fg="#ff4040"))
        close.bind("<Leave>",    lambda _: close.config(fg=COLORS["blue"]))
        rbtn = tk.Label(bar, text=" ⟳ ", bg=COLORS["bar"], fg=COLORS["blue"],
                        font=("Consolas", 14), cursor="hand2")
        rbtn.pack(side="right", padx=4, pady=8)
        rbtn.bind("<Button-1>", lambda _: self._do_refresh())
        rbtn.bind("<Enter>",    lambda _: rbtn.config(fg=COLORS["fg3"]))
        rbtn.bind("<Leave>",    lambda _: rbtn.config(fg=COLORS["blue"]))
        self.src_label = tk.Label(bar, text="", bg=COLORS["bar"], fg=COLORS["fg2"], font=("Consolas", 8))
        self.src_label.pack(side="right", padx=6, pady=13)
        self.upd_label = tk.Label(bar, text="", bg=COLORS["bar"], fg=COLORS["fg2"], font=("Consolas", 9))
        self.upd_label.pack(side="right", padx=6, pady=13)
        tk.Frame(self.root, bg=COLORS["blue2"], height=1).pack(fill="x")

        # ── Body: sidebar + table ──────────────────────────────────────────────
        body = tk.Frame(self.root, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)

        # Left sidebar
        SB_W = 195
        sb_outer = tk.Frame(body, bg=COLORS["flt"], width=SB_W)
        sb_outer.pack(side="left", fill="y"); sb_outer.pack_propagate(False)

        # Scrollable canvas for sidebar content
        sb_canvas = tk.Canvas(sb_outer, bg=COLORS["flt"], highlightthickness=0, width=SB_W)
        sb_canvas.pack(side="left", fill="both", expand=True)
        sb_frame = tk.Frame(sb_canvas, bg=COLORS["flt"])
        sb_win = sb_canvas.create_window((0, 0), window=sb_frame, anchor="nw", width=SB_W)
        sb_frame.bind("<Configure>", lambda e: sb_canvas.configure(
            scrollregion=sb_canvas.bbox("all")))
        sb_canvas.bind("<MouseWheel>", lambda e: sb_canvas.yview_scroll(
            int(-1 * (e.delta / 120)), "units"))

        def sb_label(txt, pad_top=6):
            tk.Label(sb_frame, text=txt, bg=COLORS["flt"], fg=COLORS["fg2"],
                     font=("Consolas", 8), anchor="w").pack(
                         fill="x", padx=10, pady=(pad_top, 0))

        def sb_entry(var, width=20):
            e = ttk.Entry(sb_frame, textvariable=var, width=width, style="T.TEntry")
            e.pack(fill="x", padx=10, pady=(2, 0))
            e.bind("<Return>", lambda _: self._apply_search())
            return e

        def sb_combo(var, values, width=20, state="normal"):
            """Fuzzy-search entry with dropdown list popup."""
            frame = tk.Frame(sb_frame, bg=COLORS["flt"])
            frame.pack(fill="x", padx=10, pady=(2, 0))

            entry = tk.Entry(frame, textvariable=var, width=width,
                             font=("Consolas", 9), bg=COLORS["bg2"], fg=COLORS["fg"],
                             insertbackground=COLORS["fg"], relief="flat",
                             highlightthickness=1, highlightcolor=COLORS["blue"],
                             highlightbackground=COLORS["bg2"])
            entry.pack(fill="x", side="left", expand=True)

            # Small dropdown arrow button
            arrow = tk.Button(frame, text="\u25bc", font=("Consolas", 7),
                              bg=COLORS["bg2"], fg=COLORS["fg2"], relief="flat", width=2,
                              cursor="hand2")
            arrow.pack(side="right")

            entry._all_values = list(values)
            entry._popup = None
            entry._listbox = None

            def _show_popup(filtered=None):
                """Show or update the dropdown popup with filtered values."""
                items = filtered if filtered is not None else getattr(entry, '_all_values', [])
                if not items:
                    _hide_popup()
                    return

                if entry._popup and entry._popup.winfo_exists():
                    # Update existing listbox
                    lb = entry._listbox
                    lb.delete(0, "end")
                    for item in items[:50]:  # limit for performance
                        lb.insert("end", item)
                    return

                # Create new popup
                popup = tk.Toplevel(self.root)
                popup.overrideredirect(True)
                popup.attributes("-topmost", True)

                # Position below the entry
                entry.update_idletasks()
                x = entry.winfo_rootx()
                y = entry.winfo_rooty() + entry.winfo_height()
                w = entry.winfo_width() + arrow.winfo_width()
                h = min(200, len(items) * 18 + 4)

                popup.geometry(f"{w}x{h}+{x}+{y}")

                lb = tk.Listbox(popup, bg=COLORS["bg2"], fg=COLORS["fg"],
                                selectbackground=COLORS["blue"], selectforeground="#ffffff",
                                font=("Consolas", 8), relief="flat", bd=1,
                                highlightthickness=1, highlightcolor=COLORS["blue"],
                                activestyle="none")
                lb.pack(fill="both", expand=True)

                for item in items[:50]:
                    lb.insert("end", item)

                def _on_select(event):
                    sel = lb.curselection()
                    if sel:
                        val = lb.get(sel[0])
                        var.set(val)
                        _hide_popup()
                        self._apply_search()

                lb.bind("<ButtonRelease-1>", _on_select)
                lb.bind("<Return>", _on_select)

                entry._popup = popup
                entry._listbox = lb

                # Close popup when clicking elsewhere
                def _on_focus_out(event):
                    # Delay to allow listbox click to register
                    self.root.after(150, lambda: _hide_popup()
                                   if entry._popup and not entry.focus_get() == entry
                                   else None)
                entry.bind("<FocusOut>", _on_focus_out, add="+")

            def _hide_popup():
                if entry._popup and entry._popup.winfo_exists():
                    entry._popup.destroy()
                entry._popup = None
                entry._listbox = None

            def _on_key(event):
                if event.keysym in ("Return", "Escape", "Tab"):
                    _hide_popup()
                    if event.keysym == "Return":
                        self._apply_search()
                    return

                typed = var.get().lower().strip()
                if not typed:
                    _show_popup(getattr(entry, '_all_values', []))
                else:
                    filtered = [v for v in getattr(entry, '_all_values', []) if typed in v.lower()]
                    if filtered:
                        _show_popup(filtered)
                    else:
                        _hide_popup()

            def _on_arrow():
                if entry._popup and entry._popup.winfo_exists():
                    _hide_popup()
                else:
                    _show_popup(getattr(entry, '_all_values', []))

            entry.bind("<KeyRelease>", _on_key)
            arrow.configure(command=_on_arrow)

            # Store reference for value updates
            entry._frame = frame
            return entry

        # ── Data source label ──────────────────────────────────────────────────
        sb_label("DATA SOURCE:", pad_top=10)
        tk.Label(sb_frame, text="  UEX", bg=COLORS["blue"], fg="#ffffff",
                 font=("Consolas", 9, "bold"), anchor="center", pady=3
                 ).pack(fill="x", padx=10, pady=(2, 0))

        # ── View mode toggle ───────────────────────────────────────────────────
        sb_label("VIEW MODE:", pad_top=8)
        vm_row = tk.Frame(sb_frame, bg=COLORS["flt"])
        vm_row.pack(fill="x", padx=10, pady=(2, 0))

        def _vm_btn_style(btn, active: bool):
            btn.config(bg=COLORS["blue"] if active else COLORS["btn"],
                       fg="#ffffff" if active else COLORS["fg2"])

        self._btn_routes = tk.Button(
            vm_row, text="ROUTES",
            font=("Consolas", 9, "bold"), relief="flat", cursor="hand2", pady=3,
            command=lambda: self._on_view_mode_changed("ROUTES"),
        )
        self._btn_routes.pack(side="left", fill="x", expand=True, padx=(0, 2))

        self._btn_loops = tk.Button(
            vm_row, text="LOOPS",
            font=("Consolas", 9, "bold"), relief="flat", cursor="hand2", pady=3,
            command=lambda: self._on_view_mode_changed("LOOPS"),
        )
        self._btn_loops.pack(side="left", fill="x", expand=True, padx=(2, 0))

        _vm_btn_style(self._btn_routes, True)
        _vm_btn_style(self._btn_loops,  False)

        # Vehicle
        sb_label("VEHICLE:", pad_top=10)
        self.ship_var = tk.StringVar(value=QUICK_SHIPS[0][1])
        sc = sb_combo(self.ship_var, [d for _, d in QUICK_SHIPS], state="normal")
        sc.bind("<Return>",   self._on_ship_changed)
        sc.bind("<FocusOut>", self._on_ship_changed)

        # System filters
        sb_label("SYSTEM: BUY")
        self.buy_system_var = tk.StringVar()
        self.buy_sys_combo = sb_combo(self.buy_system_var, [""])
        self.buy_sys_combo.bind("<FocusOut>", self._on_buy_system_changed)

        sb_label("SYSTEM: SELL")
        self.sell_system_var = tk.StringVar()
        self.sell_sys_combo = sb_combo(self.sell_system_var, [""])
        self.sell_sys_combo.bind("<FocusOut>", self._on_sell_system_changed)

        # Location filters
        sb_label("BUY LOCATION")
        self.buy_loc_var = tk.StringVar()
        self.buy_loc_combo = sb_combo(self.buy_loc_var, [""])
        self.buy_loc_combo.bind("<FocusOut>", self._on_buy_location_changed)

        sb_label("SELL LOCATION")
        self.sell_loc_var = tk.StringVar()
        self.sell_loc_combo = sb_combo(self.sell_loc_var, [""])
        self.sell_loc_combo.bind("<FocusOut>", self._on_sell_location_changed)

        # Terminal filters
        sb_label("BUY TERMINAL")
        self.buy_term_var = tk.StringVar()
        self.buy_term_combo = sb_combo(self.buy_term_var, [""])

        sb_label("SELL TERMINAL")
        self.sell_term_var = tk.StringVar()
        self.sell_term_combo = sb_combo(self.sell_term_var, [""])

        # Commodity
        sb_label("COMMODITY")
        self.comm_var = tk.StringVar()
        self.comm_combo = ttk.Combobox(sb_frame, textvariable=self.comm_var,
                                       values=[], width=20, style="T.TCombobox")
        self.comm_combo.pack(fill="x", padx=10, pady=(2, 0))
        self.comm_combo.bind("<FocusOut>", lambda _: self._apply_search())
        self.comm_combo.bind("<Return>", lambda _: self._apply_search())

        # SCU min
        sb_label("MIN SCU")
        self.minscu_var = tk.StringVar(value="")
        sb_entry(self.minscu_var)

        # Min profit/SCU
        sb_label("MIN PROFIT/SCU")
        self.minprofit_var = tk.StringVar(value="")
        sb_entry(self.minprofit_var)

        # Quick search
        sb_label("SEARCH")
        self.search_var = tk.StringVar()
        self.system_var  = tk.StringVar()   # kept for voice-command compat
        self.location_var = tk.StringVar()  # kept for voice-command compat
        se = sb_entry(self.search_var)
        se.bind("<KeyRelease>", lambda _: self._refresh_debounced())

        tk.Frame(sb_frame, bg=COLORS["sep"], height=1).pack(fill="x", padx=6, pady=8)

        # Search button
        srch_btn = tk.Button(
            sb_frame, text="⌕  SEARCH",
            bg=COLORS["blue"], fg="#ffffff",
            font=("Consolas", 10, "bold"),
            relief="flat", cursor="hand2", pady=6,
            command=self._apply_search,
        )
        srch_btn.pack(fill="x", padx=10, pady=(0, 4))
        srch_btn.bind("<Enter>", lambda _: srch_btn.config(bg="#00b0ff"))
        srch_btn.bind("<Leave>", lambda _: srch_btn.config(bg=COLORS["blue"]))

        # Clear button
        clr_btn = tk.Button(
            sb_frame, text="✕  CLEAR",
            bg=COLORS["btn"], fg=COLORS["fg"],
            font=("Consolas", 9),
            relief="flat", cursor="hand2", pady=4,
            command=self._clear_filters,
        )
        clr_btn.pack(fill="x", padx=10, pady=(0, 6))
        clr_btn.bind("<Enter>", lambda _: clr_btn.config(bg=COLORS["sel"]))
        clr_btn.bind("<Leave>", lambda _: clr_btn.config(bg=COLORS["btn"]))

        # Profit calculator button
        calc_btn = tk.Button(
            sb_frame, text="$  PROFIT CALC",
            bg=COLORS["btn"], fg=COLORS["green"],
            font=("Consolas", 9),
            relief="flat", cursor="hand2", pady=4,
            command=self._open_profit_calculator,
        )
        calc_btn.pack(fill="x", padx=10, pady=(0, 10))
        calc_btn.bind("<Enter>", lambda _: calc_btn.config(bg=COLORS["sel"]))
        calc_btn.bind("<Leave>", lambda _: calc_btn.config(bg=COLORS["btn"]))

        # ── Hotkey options ─────────────────────────────────────────────────────
        tk.Frame(sb_frame, bg=COLORS["sep"], height=1).pack(fill="x", padx=10, pady=(4, 0))
        sb_label("HOTKEY:", pad_top=8)

        self._hotkey_entry_var  = tk.StringVar(value=self._hotkey)
        self._hotkey_status_var = tk.StringVar(value="")

        hk_entry = tk.Entry(
            sb_frame,
            textvariable=self._hotkey_entry_var,
            bg=COLORS["ibg"], fg=COLORS["ifg"],
            insertbackground=COLORS["blue"],
            font=("Consolas", 9),
            relief="flat", borderwidth=0,
        )
        hk_entry.pack(fill="x", padx=10, pady=(2, 0), ipady=4)

        # hint label below the entry
        tk.Label(sb_frame, text="e.g. ctrl+shift+t  |  ctrl+alt+h",
                 bg=COLORS["flt"], fg=COLORS["fg2"],
                 font=("Consolas", 7), anchor="w").pack(fill="x", padx=10)

        hk_status_lbl = tk.Label(
            sb_frame, textvariable=self._hotkey_status_var,
            bg=COLORS["flt"], fg=COLORS["green"],
            font=("Consolas", 8), anchor="w",
        )
        hk_status_lbl.pack(fill="x", padx=10)

        def _apply_hotkey(*_):
            raw = self._hotkey_entry_var.get().strip().lower()
            mods, vk = _parse_hotkey(raw)
            if not mods or not vk:
                self._hotkey_status_var.set("⚠  Invalid — needs modifier + key")
                hk_status_lbl.config(fg=COLORS["red"])
                return
            self.cmd_queue.put({"type": "set_hotkey", "hotkey": raw})
            self._hotkey_status_var.set(f"✓  {raw}")
            hk_status_lbl.config(fg=COLORS["green"])

        hk_apply_btn = tk.Button(
            sb_frame, text="APPLY HOTKEY",
            bg=COLORS["btn"], fg=COLORS["blue"],
            font=("Consolas", 9),
            relief="flat", cursor="hand2", pady=3,
            command=_apply_hotkey,
        )
        hk_apply_btn.pack(fill="x", padx=10, pady=(4, 10))
        hk_apply_btn.bind("<Enter>", lambda _: hk_apply_btn.config(bg=COLORS["sel"]))
        hk_apply_btn.bind("<Leave>", lambda _: hk_apply_btn.config(bg=COLORS["btn"]))
        hk_entry.bind("<Return>", _apply_hotkey)

        # Sidebar divider
        tk.Frame(body, bg=COLORS["sep"], width=1).pack(side="left", fill="y")

        # ── Right side: tables (routes + loops) ───────────────────────────────
        right = tk.Frame(body, bg=COLORS["bg"])
        right.pack(side="left", fill="both", expand=True)

        # ── Routes treeview ────────────────────────────────────────────────────
        self._route_tbl_frame = tk.Frame(right, bg=COLORS["bg"])
        self._route_tbl_frame.pack(fill="both", expand=True)  # visible by default

        rtbl = tk.Frame(self._route_tbl_frame, bg=COLORS["bg"])
        rtbl.pack(fill="both", expand=True)
        rvsb = ttk.Scrollbar(rtbl, orient="vertical",   style="T.Vertical.TScrollbar")
        rhsb = ttk.Scrollbar(rtbl, orient="horizontal", style="T.Horizontal.TScrollbar")
        self._route_tree = ttk.Treeview(rtbl, columns=[c[0] for c in COLUMNS],
                                        show="headings", style="T.Treeview",
                                        yscrollcommand=rvsb.set, xscrollcommand=rhsb.set,
                                        selectmode="browse")
        rvsb.config(command=self._route_tree.yview)
        rhsb.config(command=self._route_tree.xview)
        for key, hdr, width, anchor in COLUMNS:
            self._route_tree.heading(key, text=hdr, anchor="center",
                                     command=lambda k=key: self._on_hdr_click(k))
            self._route_tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=False)
        self._route_tree.tag_configure("high", foreground=COLORS["green"])
        self._route_tree.tag_configure("med",  foreground=COLORS["yellow"])
        self._route_tree.tag_configure("low",  foreground=COLORS["red"])
        self._route_tree.tag_configure("odd",  background=COLORS["odd"])
        self._route_tree.tag_configure("even", background=COLORS["even"])
        rvsb.pack(side="right",  fill="y")
        rhsb.pack(side="bottom", fill="x")
        self._route_tree.pack(fill="both", expand=True)
        self.tree = self._route_tree  # active tree pointer

        # ── Loops treeview ─────────────────────────────────────────────────────
        self._loop_tbl_frame = tk.Frame(right, bg=COLORS["bg"])
        # NOT packed yet — shown only when view mode == LOOPS

        ltbl = tk.Frame(self._loop_tbl_frame, bg=COLORS["bg"])
        ltbl.pack(fill="both", expand=True)
        lvsb = ttk.Scrollbar(ltbl, orient="vertical",   style="T.Vertical.TScrollbar")
        lhsb = ttk.Scrollbar(ltbl, orient="horizontal", style="T.Horizontal.TScrollbar")
        self._loop_tree = ttk.Treeview(ltbl, columns=[c[0] for c in LOOP_COLUMNS],
                                       show="headings", style="T.Treeview",
                                       yscrollcommand=lvsb.set, xscrollcommand=lhsb.set,
                                       selectmode="browse")
        lvsb.config(command=self._loop_tree.yview)
        lhsb.config(command=self._loop_tree.xview)
        for key, hdr, width, anchor in LOOP_COLUMNS:
            self._loop_tree.heading(key, text=hdr, anchor="center",
                                    command=lambda k=key: self._on_loop_hdr_click(k))
            self._loop_tree.column(key, width=width, minwidth=40, anchor=anchor, stretch=False)
        self._loop_tree.tag_configure("high", foreground=COLORS["green"])
        self._loop_tree.tag_configure("med",  foreground=COLORS["yellow"])
        self._loop_tree.tag_configure("low",  foreground=COLORS["red"])
        self._loop_tree.tag_configure("odd",  background=COLORS["odd"])
        self._loop_tree.tag_configure("even", background=COLORS["even"])
        lvsb.pack(side="right",  fill="y")
        lhsb.pack(side="bottom", fill="x")
        self._loop_tree.pack(fill="both", expand=True)

        # Bind row selection to open pinned detail cards
        self._loop_tree.bind("<<TreeviewSelect>>",  self._on_loop_select)
        self._route_tree.bind("<<TreeviewSelect>>", self._on_route_select)

        # ── Status bar ────────────────────────────────────────────────────────
        tk.Frame(self.root, bg=COLORS["blue2"], height=1).pack(fill="x")
        sbar = tk.Frame(self.root, bg=COLORS["flt"], height=22)
        sbar.pack(fill="x"); sbar.pack_propagate(False)
        self.status_var = tk.StringVar(value="  Initializing…")
        tk.Label(sbar, textvariable=self.status_var, bg=COLORS["flt"], fg=COLORS["fg2"],
                 font=("Consolas", 9), anchor="w").pack(side="left", padx=10)
        self.count_var = tk.StringVar(value="")
        tk.Label(sbar, textvariable=self.count_var, bg=COLORS["flt"], fg=COLORS["blue"],
                 font=("Consolas", 9, "bold"), anchor="e").pack(side="right", padx=10)
        grip = tk.Label(sbar, text="⤡", bg=COLORS["flt"], fg=COLORS["fg2"],
                        font=("Consolas", 10), cursor="size_nw_se")
        grip.pack(side="right", padx=(0, 2))
        grip.bind("<Button-1>",        self._resize_start)
        grip.bind("<B1-Motion>",       self._resize_motion)
        grip.bind("<ButtonRelease-1>", lambda _: setattr(self, "_resizing", False))

    # ── Drag & resize ─────────────────────────────────────────────────────────

    def _drag_start(self, e):
        self._drag_x = e.x_root - self.root.winfo_x()
        self._drag_y = e.y_root - self.root.winfo_y()

    def _drag_motion(self, e):
        self.root.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _resize_start(self, e):
        self._resizing = True
        self._resize_sx, self._resize_sy = e.x_root, e.y_root
        self._resize_sw = self.root.winfo_width()
        self._resize_sh = self.root.winfo_height()

    def _resize_motion(self, e):
        if not self._resizing: return
        w = max(800, self._resize_sw + e.x_root - self._resize_sx)
        h = max(400, self._resize_sh + e.y_root - self._resize_sy)
        self.root.geometry(f"{w}x{h}")

    # ── Data ──────────────────────────────────────────────────────────────────

    def _start_load(self):
        self._set_status("Loading trade data…")
        self.fetcher.fetch_async(self._on_routes)

    def _on_routes(self, routes: List[Route], source: str = "API"):
        # Build multi-leg routes on background thread (expensive) before marshalling to UI
        scu = self.ship_scu  # capture for thread safety
        loops = find_multi_routes(routes, scu) if routes else []
        def _apply():
            self.all_routes      = routes
            self.all_loops       = loops
            self.last_refresh_ts = time.time()
            self._data_source    = source
            log.info("Data loaded: %d routes from %s | %d unfiltered loops built",
                     len(routes), source, len(self.all_loops))
            if routes:
                systems = sorted({r.buy_system for r in routes if r.buy_system})
                log.debug("  buy_system values in DB: %s", systems[:20])
            self._refresh_display()
        if self.root:
            self.root.after(0, _apply)

    def _refresh_display(self):
        try:
            self._refresh_display_inner()
        except Exception:
            log.error("_refresh_display crashed:\n%s", traceback.format_exc())

    def _refresh_display_inner(self):
        if self.view_mode == "LOOPS":
            # Filter pre-built loops instead of rebuilding (expensive) on every filter change
            f = self._read_filters()
            log.debug("LOOPS refresh — filters: buy_sys=%r sell_sys=%r buy_loc=%r sell_loc=%r "
                      "buy_term=%r sell_term=%r comm=%r min_margin=%.0f min_scu=%d search=%r",
                      f.buy_system, f.sell_system, f.buy_location, f.sell_location,
                      f.buy_terminal, f.sell_terminal, f.commodity,
                      f.min_margin_scu, f.min_scu, f.search)
            f.search = ""  # text search applied post-filter below
            loops = self._filter_loops(self.all_loops, f) if self.all_loops else []
            log.debug("  all_loops=%d  filtered=%d", len(self.all_loops), len(loops))
            # Text search on the assembled multi-leg routes
            q = (self.search_var.get().strip().lower() if self.search_var else "")
            if q:
                loops = [m for m in loops if any(q in x.lower() for x in [
                    m.start_terminal, m.start_system,
                    m.end_terminal,   m.commodity_chain()])]
            loops = sort_multi_routes(loops, self.loop_sort_col, self.loop_sort_reverse, self.ship_scu)
            self.filtered_loops = loops[:self.max_routes]
            log.debug("  filtered_loops displayed=%d", len(self.filtered_loops))
            self._populate_loop_table()
            self._update_status()
            self._update_header()
            return

        # ── ROUTES mode ────────────────────────────────────────────────────
        f = self._read_filters()
        result = apply_filters(self.all_routes, f)
        result = sort_routes(result, self.sort_col, self.sort_reverse, self.ship_scu)
        self.filtered_routes = result[:self.max_routes]

        if self.comm_combo is not None:
            curr = self.comm_var.get() if self.comm_var else ""
            vals = [""] + get_unique_commodities(self.all_routes)
            self.comm_combo['values'] = vals
            if self.comm_var:
                self.comm_var.set(curr)

        self._update_all_dropdown_values()
        self._populate_table()
        self._update_status()
        self._update_header()

    @staticmethod
    def _filter_loops(loops: List[MultiRoute], f) -> List[MultiRoute]:
        """Filter pre-built multi-leg loops by sidebar filter criteria."""
        result = list(loops)
        if f.buy_system:
            bs = f.buy_system.lower()
            result = [m for m in result if any(bs in r.buy_system.lower() for r in m.legs)]
        if f.sell_system:
            ss = f.sell_system.lower()
            result = [m for m in result if any(ss in r.sell_system.lower() for r in m.legs)]
        if f.buy_location:
            bl = f.buy_location.lower()
            result = [m for m in result if any(bl in r.buy_location.lower() or bl in r.buy_terminal.lower() for r in m.legs)]
        if f.sell_location:
            sl = f.sell_location.lower()
            result = [m for m in result if any(sl in r.sell_location.lower() or sl in r.sell_terminal.lower() for r in m.legs)]
        if f.buy_terminal:
            bt = f.buy_terminal.lower()
            result = [m for m in result if any(bt in r.buy_terminal.lower() for r in m.legs)]
        if f.sell_terminal:
            st = f.sell_terminal.lower()
            result = [m for m in result if any(st in r.sell_terminal.lower() for r in m.legs)]
        if f.commodity:
            c = f.commodity.lower()
            result = [m for m in result if any(c in r.commodity.lower() for r in m.legs)]
        if f.min_margin_scu > 0:
            result = [m for m in result if all(r.margin >= f.min_margin_scu for r in m.legs)]
        if f.min_scu > 0:
            result = [m for m in result if m.min_avail() >= f.min_scu]
        return result

    @staticmethod
    def _fmt_distance(d: float) -> str:
        if d <= 0:
            return "—"
        if d >= 1000:
            return f"{d / 1000:.1f}Tm"  # 1000 Gm = 1 Tm
        return f"{d:.1f}Gm"

    @staticmethod
    def _fmt_eta(distance_gm: float, speed_kms: float = 0.283) -> str:
        """Estimate travel time.  Default speed ~283 m/s (quantum)."""
        if distance_gm <= 0:
            return "—"
        secs = (distance_gm * 1e6) / speed_kms if speed_kms > 0 else 0  # Gm→km / speed(km/s)
        if secs < 60:
            return f"{secs:.0f}s"
        mins = secs / 60
        if mins < 60:
            return f"{mins:.0f}m"
        return f"{mins / 60:.1f}h"

    def _populate_table(self):
        t = self._route_tree
        if not t:
            return
        try:
            y = t.yview()[0]
        except Exception:
            y = 0.0
        t.delete(*t.get_children())
        for i, r in enumerate(self.filtered_routes):
            # Cap SCU at ship capacity for all calculations
            eff_scu = r.effective_scu(self.ship_scu)
            profit = r.margin * eff_scu
            roi    = r.roi()
            invest = r.price_buy * eff_scu
            invest_dest = r.price_sell * eff_scu
            vals = (
                r.commodity,
                r.buy_terminal or r.buy_location,
                r.container_sizes_origin or "—",
                f"{invest:,.0f}" if invest else "—",
                f"{eff_scu:,}",
                f"{r.scu_user_origin:,}" if r.scu_user_origin else "—",
                r.sell_terminal or r.sell_location,
                r.container_sizes_destination or "—",
                f"{invest_dest:,.0f}" if invest_dest else "—",
                f"{r.scu_demand:,}" if r.scu_demand else "—",
                f"{r.scu_user_destination:,}" if r.scu_user_destination else "—",
                self._fmt_distance(r.distance),
                self._fmt_eta(r.distance),
                f"{roi:.1f}%" if roi > 0 else "—",
                f"{profit:,.0f}" if profit else "—",
            )
            tag = "even" if i % 2 == 0 else "odd"
            t.insert("", "end", values=vals, tags=(profit_tier(r.margin), tag))
        if y > 0.001:
            try: t.yview_moveto(y)
            except Exception: log.debug("yview_moveto failed on route tree")

    def _populate_loop_table(self):
        if not self._loop_tree:
            return
        try:
            y = self._loop_tree.yview()[0]
        except Exception:
            y = 0.0
        self._loop_tree.delete(*self._loop_tree.get_children())
        for i, mr in enumerate(self.filtered_loops):
            tp = mr.total_profit(self.ship_scu)
            vals = (
                mr.start_terminal or mr.start_system,
                mr.start_system,
                str(mr.num_legs),
                mr.commodity_chain(),
                f"{mr.min_avail():,} SCU",
                f"{tp:,.0f} aUEC",
            )
            tag = "even" if i % 2 == 0 else "odd"
            self._loop_tree.insert("", "end", values=vals,
                                   tags=(profit_tier(mr.avg_margin()), tag))
        if y > 0.001:
            try: self._loop_tree.yview_moveto(y)
            except Exception: log.debug("yview_moveto failed on loop tree")

    # ── View mode ──────────────────────────────────────────────────────────────

    def _on_view_mode_changed(self, mode: str) -> None:
        self.view_mode = mode
        # Update button styles
        if self._btn_routes and self._btn_loops:
            if mode == "ROUTES":
                self._btn_routes.config(bg=COLORS["blue"], fg="#ffffff")
                self._btn_loops.config(bg=COLORS["btn"],   fg=COLORS["fg2"])
            else:
                self._btn_routes.config(bg=COLORS["btn"],   fg=COLORS["fg2"])
                self._btn_loops.config(bg=COLORS["blue"],  fg="#ffffff")
        # Swap visible frame and active tree pointer
        if mode == "LOOPS":
            if self._route_tbl_frame:
                self._route_tbl_frame.pack_forget()
            if self._loop_tbl_frame:
                self._loop_tbl_frame.pack(fill="both", expand=True)
            self.tree = self._loop_tree
        else:
            if self._loop_tbl_frame:
                self._loop_tbl_frame.pack_forget()
            if self._route_tbl_frame:
                self._route_tbl_frame.pack(fill="both", expand=True)
            self.tree = self._route_tree
        self._refresh_display()

    def _on_loop_hdr_click(self, col: str) -> None:
        if self.loop_sort_col == col:
            self.loop_sort_reverse = not self.loop_sort_reverse
        else:
            self.loop_sort_col     = col
            self.loop_sort_reverse = col in ("legs", "avail", "total_profit")
        self._update_loop_sort_arrows()
        self._refresh_display()

    def _update_loop_sort_arrows(self) -> None:
        if not self._loop_tree:
            return
        for key, hdr, _, _ in LOOP_COLUMNS:
            suf = (" ▼" if self.loop_sort_reverse else " ▲") if key == self.loop_sort_col else ""
            self._loop_tree.heading(key, text=hdr + suf)

    # ── Pinned card system ─────────────────────────────────────────────────────

    MAX_PINNED = 5

    @staticmethod
    def _card_key_loop(mr: MultiRoute) -> str:
        return f"multi::{mr.start_terminal}::{mr.commodity_chain()}"

    @staticmethod
    def _card_key_route(route: Route) -> str:
        return f"route::{route.commodity}::{route.buy_terminal}::{route.sell_terminal}"

    def _card_position(self, idx: int) -> Tuple[int, int]:
        """Spawn cards centred over the Trade Hub window, staggered so they don't overlap."""
        wx = self.root.winfo_x()
        wy = self.root.winfo_y()
        ww = self.root.winfo_width()
        wh = self.root.winfo_height()
        card_w, card_h = 480, 520
        base_x = wx + (ww - card_w) // 2
        base_y = wy + (wh - card_h) // 2
        return base_x + idx * 26, base_y + idx * 30

    def _make_card(self, key: str, title: str, subtitle: str) -> Tuple[tk.Toplevel, tk.Text, tk.BooleanVar]:
        """Build a floating detail card window; return (popup, text_widget, lock_var)."""
        idx = len(self._pinned_cards)
        px, py = self._card_position(idx)
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.configure(bg=COLORS["bg"])
        popup.wm_attributes("-alpha", 0.97)
        popup.wm_attributes("-topmost", True)
        popup.geometry(f"480x520+{px}+{py}")

        # ── Title bar ────────────────────────────────────────────────────────
        bar = tk.Frame(popup, bg=COLORS["bar"], height=40)
        bar.pack(fill="x"); bar.pack_propagate(False)

        def _drag_start(e):
            popup._dx = e.x_root - popup.winfo_x()
            popup._dy = e.y_root - popup.winfo_y()

        def _drag_motion(e):
            popup.geometry(f"+{e.x_root - popup._dx}+{e.y_root - popup._dy}")

        bar.bind("<Button-1>",  _drag_start)
        bar.bind("<B1-Motion>", _drag_motion)

        title_lbl = tk.Label(bar, text=f"◈  {title}",
                             bg=COLORS["bar"], fg=COLORS["blue"],
                             font=("Consolas", 12, "bold"), cursor="fleur")
        title_lbl.pack(side="left", padx=12, pady=10)
        title_lbl.bind("<Button-1>",  _drag_start)
        title_lbl.bind("<B1-Motion>", _drag_motion)

        sub_lbl = tk.Label(bar, text=subtitle,
                           bg=COLORS["bar"], fg=COLORS["fg2"],
                           font=("Consolas", 8), cursor="fleur")
        sub_lbl.pack(side="left", pady=14)
        sub_lbl.bind("<Button-1>",  _drag_start)
        sub_lbl.bind("<B1-Motion>", _drag_motion)

        close_btn = tk.Label(bar, text=" ✕ ", bg=COLORS["bar"], fg=COLORS["blue"],
                             font=("Consolas", 12, "bold"), cursor="hand2")
        close_btn.pack(side="right", padx=(4, 10), pady=8)
        close_btn.bind("<Button-1>", lambda _, k=key: self._close_card(k))
        close_btn.bind("<Enter>",    lambda _: close_btn.config(fg="#ff4040"))
        close_btn.bind("<Leave>",    lambda _: close_btn.config(fg=COLORS["blue"]))

        # Lock checkbox — prevents this card from being auto-evicted
        lock_var = tk.BooleanVar(value=False)

        def _on_lock_toggle():
            lock_btn.config(fg=COLORS["green"] if lock_var.get() else COLORS["fg2"])

        lock_btn = tk.Checkbutton(
            bar, text="⚲", variable=lock_var, command=_on_lock_toggle,
            bg=COLORS["bar"], fg=COLORS["fg2"],
            activebackground=COLORS["bar"], activeforeground=COLORS["green"],
            selectcolor=COLORS["bar"],
            font=("Consolas", 13), cursor="hand2",
            relief="flat", borderwidth=0, indicatoron=False,
        )
        lock_btn.pack(side="right", padx=(0, 2), pady=8)

        tk.Frame(popup, bg=COLORS["blue2"], height=1).pack(fill="x")

        # ── Content area ─────────────────────────────────────────────────────
        content = tk.Frame(popup, bg=COLORS["bg2"])
        content.pack(fill="both", expand=True)
        dvsb = ttk.Scrollbar(content, orient="vertical", style="T.Vertical.TScrollbar")
        dt = tk.Text(
            content,
            bg=COLORS["bg2"], fg=COLORS["fg"],
            font=("Consolas", 9),
            wrap="word",
            state="disabled", cursor="arrow",
            relief="flat", borderwidth=0,
            padx=16, pady=10,
            yscrollcommand=dvsb.set,
        )
        dvsb.config(command=dt.yview)
        dt.tag_configure("heading",  font=("Consolas", 11, "bold"), foreground=COLORS["fg3"])
        dt.tag_configure("divider",  foreground=COLORS["fg2"])
        dt.tag_configure("label",    foreground=COLORS["fg2"])
        dt.tag_configure("value",    foreground=COLORS["fg3"])
        dt.tag_configure("profit",   foreground=COLORS["green"], font=("Consolas", 10, "bold"))
        dt.tag_configure("step_hdr", font=("Consolas", 10, "bold"), foreground=COLORS["blue"])
        dt.tag_configure("step_txt", foreground=COLORS["fg3"])
        dvsb.pack(side="right", fill="y")
        dt.pack(fill="both", expand=True)

        # ── Resize grip ──────────────────────────────────────────────────────
        tk.Frame(popup, bg=COLORS["blue2"], height=1).pack(fill="x")
        gbar = tk.Frame(popup, bg=COLORS["flt"], height=20)
        gbar.pack(fill="x"); gbar.pack_propagate(False)
        grip = tk.Label(gbar, text="⤡", bg=COLORS["flt"], fg=COLORS["fg2"],
                        font=("Consolas", 10), cursor="size_nw_se")
        grip.pack(side="right", padx=(0, 3))

        popup._rx = popup._ry = popup._rw = popup._rh = 0
        popup._resizing = False

        def _rs(e):
            popup._resizing = True
            popup._rx, popup._ry = e.x_root, e.y_root
            popup._rw, popup._rh = popup.winfo_width(), popup.winfo_height()

        def _rm(e):
            if not popup._resizing:
                return
            popup.geometry(f"{max(360, popup._rw + e.x_root - popup._rx)}"
                           f"x{max(280, popup._rh + e.y_root - popup._ry)}")

        grip.bind("<Button-1>",        _rs)
        grip.bind("<B1-Motion>",       _rm)
        grip.bind("<ButtonRelease-1>", lambda _: setattr(popup, "_resizing", False))

        return popup, dt, lock_var

    def _close_card(self, key: str) -> None:
        """Destroy the popup for the given key and remove from _pinned_cards."""
        for card in list(self._pinned_cards):
            if card["key"] == key:
                try:
                    if card["popup"].winfo_exists():
                        card["popup"].destroy()
                except Exception:
                    log.debug("Failed to destroy card popup for key=%s", key)
                self._pinned_cards.remove(card)
                return

    def _evict_oldest_unlocked(self) -> bool:
        """Close the oldest unlocked card. Returns True if one was evicted."""
        for card in self._pinned_cards:
            lv = card.get("lock_var")
            if not (lv.get() if lv else False):
                self._close_card(card["key"])
                return True
        return False  # all cards are locked

    def _pin_loop(self, mr: MultiRoute) -> None:
        """Open (or bring to front) a pinned detail card for this multi-leg route."""
        loop = mr   # alias kept for local var brevity
        key = self._card_key_loop(mr)
        # Already open — refresh content and raise
        for card in list(self._pinned_cards):
            if card["key"] == key:
                try:
                    if card["popup"].winfo_exists():
                        card["data"] = loop
                        self._fill_loop_card(card["text"], loop, self.ship_scu)
                        card["popup"].lift()
                        return
                except Exception:
                    log.debug("Failed to refresh loop card for key=%s", key)
                self._pinned_cards.remove(card)
                break
        # At limit — evict oldest unlocked card (skip if all locked)
        if len(self._pinned_cards) >= self.MAX_PINNED:
            if not self._evict_oldest_unlocked():
                return
        popup, dt, lock_var = self._make_card(key, "ROUTE DETAIL", "LOOP ROUTE OVERVIEW")
        self._fill_loop_card(dt, loop, self.ship_scu)
        self._pinned_cards.append({"key": key, "popup": popup, "text": dt,
                                   "type": "loop", "data": loop, "lock_var": lock_var})

    def _pin_route(self, route: Route) -> None:
        """Open (or bring to front) a pinned detail card for this single route."""
        key = self._card_key_route(route)
        # Already open — refresh content and raise
        for card in list(self._pinned_cards):
            if card["key"] == key:
                try:
                    if card["popup"].winfo_exists():
                        card["data"] = route
                        self._fill_route_card(card["text"], route, self.ship_scu)
                        card["popup"].lift()
                        return
                except Exception:
                    log.debug("Failed to refresh route card for key=%s", key)
                self._pinned_cards.remove(card)
                break
        # At limit — evict oldest unlocked card (skip if all locked)
        if len(self._pinned_cards) >= self.MAX_PINNED:
            if not self._evict_oldest_unlocked():
                return
        popup, dt, lock_var = self._make_card(key, "ROUTE DETAIL", "SINGLE ROUTE")
        self._fill_route_card(dt, route, self.ship_scu)
        self._pinned_cards.append({"key": key, "popup": popup, "text": dt,
                                   "type": "route", "data": route, "lock_var": lock_var})

    def _fill_loop_card(self, dt: tk.Text, mr: MultiRoute, scu: int) -> None:
        """Write multi-leg route detail into the given Text widget."""
        ship_lbl = f"{self.ship_name} ({scu:,} SCU)" if scu else (self.ship_name or "No ship")
        total  = mr.total_profit(scu)
        invest = mr.total_investment(scu)
        roi    = mr.roi_pct(scu)
        n      = mr.num_legs
        label  = f"{n} {'leg' if n == 1 else 'legs'}"

        dt.config(state="normal")
        dt.delete("1.0", "end")

        def w(text, tag=None):
            dt.insert("end", text, tag) if tag else dt.insert("end", text)

        w(f"◈  MULTI-LEG ROUTE  ({label})\n", "heading")
        w("─" * 52 + "\n", "divider")
        w("  Ship:              ", "label"); w(f"{ship_lbl}\n",        "value")
        w("  Total Profit:      ", "label"); w(f"{total:,.0f} aUEC\n", "profit")
        if invest > 0:
            w("  Total Investment:  ", "label"); w(f"{invest:,.0f} aUEC\n", "value")
            w("  ROI:               ", "label"); w(f"{roi:.1f}%\n",         "value")
        w("\n")

        for step_num, r in enumerate(mr.legs, 1):
            eff        = r.effective_scu(scu)
            leg_profit = eff * r.margin
            leg_cost   = eff * r.price_buy if r.price_buy > 0 else 0
            price_s    = f"  @ {r.price_buy:,.0f} aUEC/SCU" if r.price_buy > 0 else ""
            sell_s     = f"  (sell {r.price_sell:,.0f} aUEC/SCU)" if r.price_sell > 0 else ""

            w(f"  Leg {step_num}  ", "step_hdr"); w("─" * 40 + "\n", "divider")
            w("  Origin:      ", "label"); w(f"{r.buy_terminal or r.buy_location}", "step_txt")
            if r.buy_system: w(f"  ({r.buy_system})", "label")
            w("\n")
            w("  Cargo:       ", "label")
            w(f"{r.commodity}", "step_txt")
            w(f"  —  {eff:,} SCU{price_s}\n", "label")
            if leg_cost > 0:
                w("  Buy Cost:    ", "label"); w(f"{leg_cost:,.0f} aUEC\n", "value")
            w("  Destination: ", "label"); w(f"{r.sell_terminal or r.sell_location}", "step_txt")
            if r.sell_system: w(f"  ({r.sell_system})", "label")
            w("\n")
            w("  Profit:      ", "label"); w(f"+{leg_profit:,.0f} aUEC", "profit")
            w(f"  ({r.margin:,.0f} aUEC/SCU{sell_s})\n\n", "label")

        w("  " + "─" * 50 + "\n", "divider")
        w("  Total Profit:      ", "label"); w(f"{total:,.0f} aUEC\n",   "profit")
        if invest > 0:
            w("  Total Investment:  ", "label"); w(f"{invest:,.0f} aUEC\n", "value")
            w("  ROI:               ", "label"); w(f"{roi:.1f}%\n",         "value")

        dt.config(state="disabled")
        dt.yview_moveto(0.0)

    def _fill_route_card(self, dt: tk.Text, route: Route, scu: int) -> None:
        """Write single-route detail content into the given Text widget."""
        ship_lbl = f"{self.ship_name} ({scu:,} SCU)" if scu else (self.ship_name or "No ship")
        eff_scu  = route.effective_scu(scu)
        profit   = eff_scu * route.margin
        roi      = route.roi()
        invest   = eff_scu * route.price_buy

        dt.config(state="normal")
        dt.delete("1.0", "end")

        def w(text, tag=None):
            dt.insert("end", text, tag) if tag else dt.insert("end", text)

        w("◈  SINGLE ROUTE\n", "heading")
        w("─" * 52 + "\n", "divider")
        w("  Ship:              ", "label"); w(f"{ship_lbl}\n",          "value")
        w("  Commodity:         ", "label"); w(f"{route.commodity}\n",   "value")
        w("  Est. Profit:       ", "label"); w(f"{profit:,.0f} aUEC\n",  "profit")
        if invest > 0:
            w("  Investment:        ", "label"); w(f"{invest:,.0f} aUEC\n", "value")
        if roi > 0:
            w("  ROI:               ", "label"); w(f"{roi:.1f}%\n",         "value")
        w("\n")

        w("  BUY  ", "step_hdr"); w("─" * 45 + "\n", "divider")
        w("  Terminal:    ", "label")
        w(f"{route.buy_terminal or route.buy_location}\n", "step_txt")
        if route.buy_location and route.buy_terminal:
            w("  Location:    ", "label"); w(f"{route.buy_location}\n", "step_txt")
        w("  System:      ", "label"); w(f"{route.buy_system}\n", "step_txt")
        price_s = f"{route.price_buy:,.0f} aUEC/SCU" if route.price_buy > 0 else "—"
        w("  Price:       ", "label"); w(f"{price_s}\n", "value")
        w("  Available:   ", "label"); w(f"{route.scu_available:,} SCU\n", "value")
        w("  Load:        ", "label"); w(f"{eff_scu:,} SCU\n\n", "value")

        w("  SELL  ", "step_hdr"); w("─" * 44 + "\n", "divider")
        w("  Terminal:    ", "label")
        w(f"{route.sell_terminal or route.sell_location}\n", "step_txt")
        if route.sell_location and route.sell_terminal:
            w("  Location:    ", "label"); w(f"{route.sell_location}\n", "step_txt")
        w("  System:      ", "label"); w(f"{route.sell_system}\n", "step_txt")
        price_s2 = f"{route.price_sell:,.0f} aUEC/SCU" if route.price_sell > 0 else "—"
        w("  Price:       ", "label"); w(f"{price_s2}\n", "value")
        if route.scu_demand > 0:
            w("  Demand:      ", "label"); w(f"{route.scu_demand:,} SCU\n", "value")
        w("\n")

        w("  " + "─" * 50 + "\n", "divider")
        w("  Margin/SCU:        ", "label"); w(f"{route.margin:,.0f} aUEC/SCU\n", "profit")
        w("  Est. Profit:       ", "label"); w(f"{profit:,.0f} aUEC\n",           "profit")

        dt.config(state="disabled")
        dt.yview_moveto(0.0)

    def _apply_loops(self, loops: list) -> None:
        """Apply recomputed loops to the model and refresh the LOOPS view if active."""
        self.all_loops = loops
        if self.view_mode == "LOOPS":
            self._refresh_display()

    def _refresh_all_cards(self) -> None:
        """Re-render all open detail cards (called after ship change)."""
        dead = []
        for card in self._pinned_cards:
            try:
                if not card["popup"].winfo_exists():
                    dead.append(card)
                    continue
                if card["type"] == "loop":
                    self._fill_loop_card(card["text"], card["data"], self.ship_scu)
                else:
                    self._fill_route_card(card["text"], card["data"], self.ship_scu)
            except Exception:
                dead.append(card)
        for card in dead:
            if card in self._pinned_cards:
                self._pinned_cards.remove(card)

    def _on_loop_select(self, _=None) -> None:
        if not self._loop_tree:
            return
        sel = self._loop_tree.selection()
        if not sel:
            return
        try:
            idx = self._loop_tree.index(sel[0])
        except Exception:
            return
        if 0 <= idx < len(self.filtered_loops):
            self._pin_loop(self.filtered_loops[idx])

    def _on_route_select(self, _=None) -> None:
        if not self._route_tree:
            return
        sel = self._route_tree.selection()
        if not sel:
            return
        try:
            idx = self._route_tree.index(sel[0])
        except Exception:
            return
        if 0 <= idx < len(self.filtered_routes):
            self._pin_route(self.filtered_routes[idx])

    def _update_status(self):
        ship = f" │ {self.ship_name} ({self.ship_scu:,} SCU)" if self.ship_scu else ""
        if self.view_mode == "LOOPS":
            total = len(self.all_loops)
            shown = len(self.filtered_loops)
            if self.status_var:
                self.status_var.set(f"  {shown:,} / {total:,} loops{ship}")
            if self.count_var:
                self.count_var.set(f"{shown:,} loops  ")
        else:
            total = len(self.all_routes)
            shown = len(self.filtered_routes)
            flt   = " │ Filters active" if self._has_filters() else ""
            if self.status_var:
                self.status_var.set(f"  {shown:,} / {total:,} routes{ship}{flt}")
            if self.count_var:
                self.count_var.set(f"{shown:,} routes  ")

    def _update_header(self):
        if self.upd_label and self.last_refresh_ts:
            t = time.strftime("%H:%M:%S", time.localtime(self.last_refresh_ts))
            self.upd_label.config(text=f"Updated {t}")
        if self.src_label:
            self.src_label.config(text=f"[{self._data_source}]")

    def _set_status(self, msg: str):
        if self.status_var and self.root:
            self.status_var.set(f"  {msg}")

    def _do_refresh(self):
        self._set_status("Refreshing…")
        self.fetcher.fetch_async(self._on_routes)

    # ── Dropdown cascade helpers ───────────────────────────────────────────────

    def _update_all_dropdown_values(self):
        """Populate all sidebar combo dropdowns from loaded route data."""
        routes = self.all_routes
        if not routes:
            return
        # Buy systems
        if self.buy_sys_combo is not None:
            curr = self.buy_system_var.get() if self.buy_system_var else ""
            systems = sorted({r.buy_system for r in routes if r.buy_system})
            vals = [""] + systems
            self.buy_sys_combo._all_values = vals
            if self.buy_system_var:
                self.buy_system_var.set(curr)
        # Sell systems
        if self.sell_sys_combo is not None:
            curr = self.sell_system_var.get() if self.sell_system_var else ""
            systems = sorted({r.sell_system for r in routes if r.sell_system})
            vals = [""] + systems
            self.sell_sys_combo._all_values = vals
            if self.sell_system_var:
                self.sell_system_var.set(curr)
        # Cascade location & terminal values (respects current system selection)
        self._update_buy_location_values()
        self._update_sell_location_values()

    def _update_buy_location_values(self):
        """Refresh buy-location dropdown, filtered by selected buy system."""
        if self.buy_loc_combo is None or not self.all_routes:
            return
        sys_filter = (self.buy_system_var.get() or "").strip().lower()
        routes = self.all_routes
        if sys_filter:
            routes = [r for r in routes if sys_filter in r.buy_system.lower()]
        locs = sorted({r.buy_location for r in routes if r.buy_location})
        curr = self.buy_loc_var.get() if self.buy_loc_var else ""
        vals = [""] + locs
        self.buy_loc_combo._all_values = vals
        if curr and curr not in locs:
            if self.buy_loc_var:
                self.buy_loc_var.set("")
        self._update_buy_terminal_values()

    def _update_buy_terminal_values(self):
        """Refresh buy-terminal dropdown, filtered by selected buy system + location."""
        if self.buy_term_combo is None or not self.all_routes:
            return
        sys_filter = (self.buy_system_var.get() or "").strip().lower()
        loc_filter = (self.buy_loc_var.get() or "").strip().lower()
        routes = self.all_routes
        if sys_filter:
            routes = [r for r in routes if sys_filter in r.buy_system.lower()]
        if loc_filter:
            routes = [r for r in routes if loc_filter in r.buy_location.lower()]
        terms = sorted({r.buy_terminal for r in routes if r.buy_terminal})
        curr = self.buy_term_var.get() if self.buy_term_var else ""
        vals = [""] + terms
        self.buy_term_combo._all_values = vals
        if curr and curr not in terms:
            if self.buy_term_var:
                self.buy_term_var.set("")

    def _update_sell_location_values(self):
        """Refresh sell-location dropdown, filtered by selected sell system."""
        if self.sell_loc_combo is None or not self.all_routes:
            return
        sys_filter = (self.sell_system_var.get() or "").strip().lower()
        routes = self.all_routes
        if sys_filter:
            routes = [r for r in routes if sys_filter in r.sell_system.lower()]
        locs = sorted({r.sell_location for r in routes if r.sell_location})
        curr = self.sell_loc_var.get() if self.sell_loc_var else ""
        vals = [""] + locs
        self.sell_loc_combo._all_values = vals
        if curr and curr not in locs:
            if self.sell_loc_var:
                self.sell_loc_var.set("")
        self._update_sell_terminal_values()

    def _update_sell_terminal_values(self):
        """Refresh sell-terminal dropdown, filtered by selected sell system + location."""
        if self.sell_term_combo is None or not self.all_routes:
            return
        sys_filter = (self.sell_system_var.get() or "").strip().lower()
        loc_filter = (self.sell_loc_var.get() or "").strip().lower()
        routes = self.all_routes
        if sys_filter:
            routes = [r for r in routes if sys_filter in r.sell_system.lower()]
        if loc_filter:
            routes = [r for r in routes if loc_filter in r.sell_location.lower()]
        terms = sorted({r.sell_terminal for r in routes if r.sell_terminal})
        curr = self.sell_term_var.get() if self.sell_term_var else ""
        vals = [""] + terms
        self.sell_term_combo._all_values = vals
        if curr and curr not in terms:
            if self.sell_term_var:
                self.sell_term_var.set("")

    def _on_buy_system_changed(self, _=None):
        self._update_buy_location_values()
        self._apply_search()

    def _on_buy_location_changed(self, _=None):
        self._update_buy_terminal_values()
        self._apply_search()

    def _on_sell_system_changed(self, _=None):
        self._update_sell_location_values()
        self._apply_search()

    def _on_sell_location_changed(self, _=None):
        self._update_sell_terminal_values()
        self._apply_search()

    # ── Filters ───────────────────────────────────────────────────────────────

    def _apply_search(self):
        """Triggered by the Search button or Enter key — runs filters immediately."""
        if self.root:
            self.root.after(0, self._refresh_display)

    def _read_filters(self) -> FilterState:
        f = FilterState()
        # Voice-command / generic filters
        f.system   = self.system_var.get().strip()   if self.system_var   else ""
        f.location = self.location_var.get().strip() if self.location_var else ""
        f.commodity= self.comm_var.get().strip()     if self.comm_var     else ""
        f.search   = self.search_var.get().strip()   if self.search_var   else ""
        try:
            f.min_margin_scu = float(self.minprofit_var.get()) if self.minprofit_var else 0
        except ValueError:
            f.min_margin_scu = 0
        # Sidebar filters
        f.buy_system  = self.buy_system_var.get().strip()  if self.buy_system_var  else ""
        f.sell_system = self.sell_system_var.get().strip() if self.sell_system_var else ""
        f.buy_location  = self.buy_loc_var.get().strip()  if self.buy_loc_var  else ""
        f.sell_location = self.sell_loc_var.get().strip() if self.sell_loc_var else ""
        f.buy_terminal  = self.buy_term_var.get().strip()  if self.buy_term_var  else ""
        f.sell_terminal = self.sell_term_var.get().strip() if self.sell_term_var else ""
        try:
            f.min_scu = int(self.minscu_var.get()) if self.minscu_var and self.minscu_var.get() else 0
        except ValueError:
            f.min_scu = 0
        return f

    def _has_filters(self) -> bool:
        f = self._read_filters()
        return bool(
            f.system or f.location or f.commodity or f.search or f.min_margin_scu > 0
            or f.buy_system or f.sell_system
            or f.buy_location or f.sell_location
            or f.buy_terminal or f.sell_terminal
            or f.min_scu > 0
        )

    def _refresh(self):
        if self.root: self.root.after(0, self._refresh_display)

    def _refresh_debounced(self, delay=320):
        if self._debounce_id:
            try: self.root.after_cancel(self._debounce_id)
            except Exception: log.debug("after_cancel failed for debounce")
        self._debounce_id = self.root.after(delay, self._refresh_display)

    def _clear_filters(self):
        for var, val in [
            (self.system_var, ""),    (self.location_var, ""),
            (self.comm_var, ""),      (self.search_var, ""),
            (self.minprofit_var, ""), (self.minscu_var, ""),
            (self.buy_system_var, ""), (self.sell_system_var, ""),
            (self.buy_loc_var, ""),   (self.sell_loc_var, ""),
            (self.buy_term_var, ""),  (self.sell_term_var, ""),
        ]:
            if var: var.set(val)
        # Restore full dropdown options now that system filters are cleared
        self._update_buy_location_values()
        self._update_sell_location_values()
        self._refresh()

    # ── Ship ──────────────────────────────────────────────────────────────────

    def _on_ship_changed(self, _=None):
        sel = (self.ship_var.get() if self.ship_var else "").strip()
        # Match against display strings first (dropdown selection)
        for name, display in QUICK_SHIPS:
            if display == sel:
                self._set_ship(name, 0)
                return
        # Match against canonical ship names (typed input)
        sel_lo = sel.lower()
        for name, _ in QUICK_SHIPS:
            if name and name.lower() == sel_lo:
                self._set_ship(name, 0)
                return
        # Last resort: pass typed text directly to scu_for_ship
        if sel:
            self._set_ship(sel, 0)
        else:
            self._set_ship("", 0)

    # ── Config persistence ────────────────────────────────────────────────────

    def _load_config(self) -> None:
        """Load saved settings from disk and apply them."""
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            saved_ship = cfg.get("ship_name", "")
            if saved_ship:
                self._set_ship(saved_ship)
            saved_hk = cfg.get("hotkey", "")
            if saved_hk:
                self._hotkey = saved_hk
                if self._hotkey_entry_var:
                    self._hotkey_entry_var.set(saved_hk)
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass  # first run or corrupt file — ignore

    def _save_config(self) -> None:
        """Persist current settings to disk."""
        try:
            cfg = {"ship_name": self.ship_name, "hotkey": self._hotkey}
            with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
        except OSError:
            pass  # non-fatal — skip silently

    def _set_ship(self, name: str, scu: int = 0):
        self.ship_name = name
        self.ship_scu  = scu if scu > 0 else scu_for_ship(name)
        log.info("Ship set: %r  SCU=%d", self.ship_name, self.ship_scu)
        self._save_config()
        if self.ship_var and self.root:
            for n, d in QUICK_SHIPS:
                if n.lower() == name.lower():
                    self.ship_var.set(d); break
            else:
                if not name: self.ship_var.set(QUICK_SHIPS[0][1])
        # Rebuild multi-leg routes with new ship SCU (greedy leg ordering changes)
        # Run expensive computation on a background thread to avoid freezing the GUI
        scu = self.ship_scu  # capture for thread safety
        routes_ref = self.all_routes
        def _recompute():
            loops = find_multi_routes(routes_ref, scu) if routes_ref else []
            if self.root:
                self.root.after(0, lambda: self._apply_loops(loops))
        threading.Thread(target=_recompute, daemon=True).start()
        # For LOOPS mode, _apply_loops callback handles the refresh once
        # recomputation finishes — refreshing now would show stale data.
        if self.view_mode != "LOOPS":
            self._refresh()
        # Re-render all open detail cards with updated ship SCU
        self._refresh_all_cards()

    # ── Profit calculator ─────────────────────────────────────────────────────

    def _open_profit_calculator(self) -> None:
        """Open (or raise) the floating profit calculator window."""
        # If already open just raise it
        if self._calc_popup and self._calc_popup.winfo_exists():
            self._calc_popup.lift()
            self._calc_popup.focus_force()
            return

        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        cw, ch = 370, 300
        px = (sw - cw) // 2
        py = (sh - ch) // 2

        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.configure(bg=COLORS["bg"])
        popup.wm_attributes("-alpha", 0.97)
        popup.wm_attributes("-topmost", True)
        popup.geometry(f"{cw}x{ch}+{px}+{py}")
        self._calc_popup = popup

        # ── Title bar ────────────────────────────────────────────────────
        bar = tk.Frame(popup, bg=COLORS["bar"], height=40)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        def _drag_start(e):
            popup._dx = e.x_root - popup.winfo_x()
            popup._dy = e.y_root - popup.winfo_y()

        def _drag_motion(e):
            popup.geometry(f"+{e.x_root - popup._dx}+{e.y_root - popup._dy}")

        bar.bind("<Button-1>",  _drag_start)
        bar.bind("<B1-Motion>", _drag_motion)

        title_lbl = tk.Label(bar, text="◈  PROFIT CALC",
                             bg=COLORS["bar"], fg=COLORS["blue"],
                             font=("Consolas", 12, "bold"), cursor="fleur")
        title_lbl.pack(side="left", padx=12, pady=10)
        title_lbl.bind("<Button-1>",  _drag_start)
        title_lbl.bind("<B1-Motion>", _drag_motion)

        def _close():
            popup.destroy()
            self._calc_popup = None

        close_btn = tk.Label(bar, text=" ✕ ", bg=COLORS["bar"], fg=COLORS["blue"],
                             font=("Consolas", 12, "bold"), cursor="hand2")
        close_btn.pack(side="right", padx=(4, 10), pady=8)
        close_btn.bind("<Button-1>", lambda _: _close())
        close_btn.bind("<Enter>",    lambda _: close_btn.config(fg="#ff4040"))
        close_btn.bind("<Leave>",    lambda _: close_btn.config(fg=COLORS["blue"]))

        tk.Frame(popup, bg=COLORS["blue2"], height=1).pack(fill="x")

        # ── Body ─────────────────────────────────────────────────────────
        body = tk.Frame(popup, bg=COLORS["bg2"])
        body.pack(fill="both", expand=True, padx=20, pady=14)

        def _field_label(text: str) -> None:
            tk.Label(body, text=text,
                     bg=COLORS["bg2"], fg=COLORS["fg2"],
                     font=("Consolas", 9), anchor="w").pack(fill="x", pady=(8, 2))

        def _field_entry() -> tk.Entry:
            e = tk.Entry(body,
                         bg=COLORS["ibg"], fg=COLORS["ifg"],
                         insertbackground=COLORS["blue"],
                         font=("Consolas", 11),
                         relief="flat", borderwidth=0)
            e.pack(fill="x", ipady=5)
            return e

        _field_label("Starting Income  (aUEC)")
        start_ent = _field_entry()

        _field_label("Ending Income  (aUEC)")
        end_ent = _field_entry()

        result_var = tk.StringVar(value="")
        result_lbl = tk.Label(body, textvariable=result_var,
                              bg=COLORS["bg2"], fg=COLORS["fg"],
                              font=("Consolas", 13, "bold"), anchor="w")

        def _parse_num(raw: str) -> float:
            """Accept plain numbers, comma-separated, or k/m suffix."""
            s = raw.strip().replace(",", "").replace(" ", "").lower()
            s = s.replace("auec", "").replace("uec", "")
            if s.endswith("k"):
                return float(s[:-1]) * 1_000
            if s.endswith("m"):
                return float(s[:-1]) * 1_000_000
            return float(s)

        def _calculate(*_) -> None:
            try:
                start = _parse_num(start_ent.get())
            except ValueError:
                result_var.set("⚠  Invalid starting income")
                result_lbl.config(fg=COLORS["red"])
                result_lbl.pack(fill="x", pady=(10, 0))
                return
            try:
                end = _parse_num(end_ent.get())
            except ValueError:
                result_var.set("⚠  Invalid ending income")
                result_lbl.config(fg=COLORS["red"])
                result_lbl.pack(fill="x", pady=(10, 0))
                return

            diff  = end - start
            sign  = "+" if diff >= 0 else ""
            color = COLORS["green"] if diff >= 0 else COLORS["red"]
            result_var.set(f"  {sign}{diff:,.0f}  aUEC")
            result_lbl.config(fg=color)
            result_lbl.pack(fill="x", pady=(10, 0))

        go_btn = tk.Button(body, text="CALCULATE",
                           bg=COLORS["blue"], fg="#ffffff",
                           font=("Consolas", 10, "bold"),
                           relief="flat", cursor="hand2",
                           pady=7, command=_calculate)
        go_btn.pack(fill="x", pady=(14, 0))
        go_btn.bind("<Enter>", lambda _: go_btn.config(bg="#0070b0"))
        go_btn.bind("<Leave>", lambda _: go_btn.config(bg=COLORS["blue"]))

        # Enter key in either field triggers calculate
        start_ent.bind("<Return>", _calculate)
        end_ent.bind("<Return>",   _calculate)

        # ── Resize grip ──────────────────────────────────────────────────
        tk.Frame(popup, bg=COLORS["blue2"], height=1).pack(fill="x")
        gbar = tk.Frame(popup, bg=COLORS["flt"], height=20)
        gbar.pack(fill="x")
        gbar.pack_propagate(False)
        grip = tk.Label(gbar, text="⤡", bg=COLORS["flt"], fg=COLORS["fg2"],
                        font=("Consolas", 10), cursor="size_nw_se")
        grip.pack(side="right", padx=(0, 3))

        popup._rx = popup._ry = popup._rw = popup._rh = 0
        popup._resizing = False

        def _rs(e):
            popup._resizing = True
            popup._rx, popup._ry = e.x_root, e.y_root
            popup._rw, popup._rh = popup.winfo_width(), popup.winfo_height()

        def _rm(e):
            if not popup._resizing:
                return
            popup.geometry(f"{max(300, popup._rw + e.x_root - popup._rx)}"
                           f"x{max(240, popup._rh + e.y_root - popup._ry)}")

        grip.bind("<Button-1>",        _rs)
        grip.bind("<B1-Motion>",       _rm)
        grip.bind("<ButtonRelease-1>", lambda _: setattr(popup, "_resizing", False))

        start_ent.focus_set()

    # ── Global hotkey listener ────────────────────────────────────────────────

    def _hotkey_listener_thread(
        self,
        mods: int,
        vk: int,
        stop_evt: threading.Event,
    ) -> None:
        """Background thread: registers a Win32 global hotkey and forwards
        presses to the main queue as {"type": "toggle"}."""
        HOTKEY_ID = 2001
        try:
            if not _user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
                return  # hotkey already claimed by another app — give up silently
            msg = ctypes.wintypes.MSG()
            while not stop_evt.is_set():
                if _user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                    if msg.message == _WM_HOTKEY and msg.wParam == HOTKEY_ID:
                        self.cmd_queue.put({"type": "toggle"})
                else:
                    time.sleep(0.05)
        except Exception:
            log.debug("Hotkey listener error: %s", traceback.format_exc())
        finally:
            try:
                _user32.UnregisterHotKey(None, HOTKEY_ID)
            except Exception:
                log.debug("UnregisterHotKey failed")

    def _start_hotkey_listener(self) -> None:
        """Parse the configured hotkey and start the listener thread."""
        if not _user32:
            return  # Win32 hotkeys not available on this platform
        mods, vk = _parse_hotkey(self._hotkey)
        if not mods or not vk:
            return
        # Stop and join the previous hotkey thread if still running
        if hasattr(self, '_hotkey_thread') and self._hotkey_thread and self._hotkey_thread.is_alive():
            self._hotkey_stop.set()
            self._hotkey_thread.join(timeout=1.0)
        self._hotkey_stop = threading.Event()
        self._hotkey_thread = threading.Thread(
            target=self._hotkey_listener_thread,
            args=(mods, vk, self._hotkey_stop),
            daemon=True,
            name="trade-hub-hotkey",
        )
        self._hotkey_thread.start()

    # ── Sort ──────────────────────────────────────────────────────────────────

    def _on_hdr_click(self, col: str):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col     = col
            self.sort_reverse = col in ("available_scu", "scu_user_origin", "scu_demand",
                                        "scu_user_dest", "investment", "invest_dest",
                                        "distance", "eta", "roi", "est_profit")
        self._update_sort_arrows()
        self._refresh()

    def _update_sort_arrows(self):
        if not self._route_tree:
            return
        for key, hdr, _, _ in COLUMNS:
            suf = (" ▼" if self.sort_reverse else " ▲") if key == self.sort_col else ""
            self._route_tree.heading(key, text=hdr + suf)

    # ── Queue polling & auto-refresh ──────────────────────────────────────────

    def _poll_queue(self):
        try:
            while True:
                self._handle_cmd(self.cmd_queue.get_nowait())
        except queue.Empty:
            pass
        except Exception:
            log.debug("_poll_queue error: %s", traceback.format_exc())
        if self.root:
            self.root.after(100, self._poll_queue)

    def _auto_refresh_loop(self):
        self.root.after(int(self.refresh_interval * 1000), self._do_auto_refresh)

    def _do_auto_refresh(self):
        self.fetcher.fetch_async(self._on_routes)
        self._auto_refresh_loop()

    # ── Command handler ───────────────────────────────────────────────────────

    def _handle_cmd(self, cmd: dict):
        t = cmd.get("type", "")
        log.debug("CMD: %s", cmd)
        if t == "show":
            self.root.deiconify()
            self._visible = True
            self.root.wm_attributes("-topmost", True)
            self.root.lift()
            self.root.after(50, self._force_show)
        elif t == "hide":
            self.root.withdraw()
            self._visible = False
        elif t == "toggle":
            if not self._visible:
                self.root.deiconify()
                self._visible = True
                self.root.wm_attributes("-topmost", True)
                self.root.lift()
                self.root.after(50, self._force_show)
            else:
                self.root.withdraw()
                self._visible = False
        elif t == "set_hotkey":
            new_hk = cmd.get("hotkey", "")
            if new_hk:
                # Stop old listener and join its thread
                if self._hotkey_stop:
                    self._hotkey_stop.set()
                    self._hotkey_stop = None
                if self._hotkey_thread and self._hotkey_thread.is_alive():
                    self._hotkey_thread.join(timeout=1.0)
                    self._hotkey_thread = None
                self._hotkey = new_hk
                self._save_config()
                self._start_hotkey_listener()
                # Keep sidebar entry in sync if changed externally
                if self._hotkey_entry_var:
                    self._hotkey_entry_var.set(new_hk)
        elif t == "quit":
            if self._hotkey_stop:
                self._hotkey_stop.set()
            if self._hotkey_thread and self._hotkey_thread.is_alive():
                self._hotkey_thread.join(timeout=1.0)
            self.root.quit()
        elif t == "set_ship":
            self._set_ship(cmd.get("ship_name", ""), cmd.get("ship_scu", 0))
        elif t == "filter":
            if cmd.get("system")        and self.system_var:   self.system_var.set(cmd["system"])
            if cmd.get("location")      and self.location_var: self.location_var.set(cmd["location"])
            if cmd.get("commodity")     and self.comm_var:      self.comm_var.set(cmd["commodity"])
            if cmd.get("min_profit_scu") and self.minprofit_var:
                self.minprofit_var.set(str(cmd["min_profit_scu"]))
            self._refresh()
        elif t == "sort":
            col = cmd.get("column", "est_profit")
            if col in COLUMN_KEYS:
                self.sort_col     = col
                self.sort_reverse = col in ("available_scu", "scu_demand", "roi", "est_profit")
                self._update_sort_arrows()
                self._refresh()
        elif t == "clear_filters":
            self._clear_filters()
        elif t == "refresh":
            self._do_refresh()
        elif t == "status":
            self._set_status(cmd.get("message", ""))
        elif t == "opacity":
            self.root.wm_attributes("-alpha", max(0.3, min(1.0, float(cmd.get("value", 0.95)))))


# ── Command file reader (polls a temp .jsonl file written by the parent) ──────

def _cmd_file_reader(cmd_queue: queue.Queue, cmd_file: str) -> None:
    """Poll a temp file for newline-delimited JSON commands from the parent process."""
    try:
        from shared.ipc import ipc_read_incremental
    except ImportError:
        ipc_read_incremental = None

    offset = 0
    while True:
        try:
            if not os.path.exists(cmd_file):
                # Parent deleted the file — signal quit
                cmd_queue.put({"type": "quit"})
                return

            if ipc_read_incremental is not None:
                commands, offset = ipc_read_incremental(cmd_file, offset)
                for cmd in commands:
                    cmd_queue.put(cmd)
                    if cmd.get("type") == "quit":
                        return
            else:
                # Fallback: manual reading with incomplete-line buffering
                # Open in binary mode to avoid Windows \r\n offset drift.
                with open(cmd_file, "rb") as f:
                    f.seek(offset)
                    raw_bytes = f.read()
                    new_offset = f.tell()
                data = raw_bytes.decode("utf-8", errors="replace")
                # Only process complete lines (ending with newline)
                if data:
                    if not data.endswith('\n'):
                        # Incomplete last line — don't advance past it
                        last_nl = raw_bytes.rfind(b'\n')
                        if last_nl == -1:
                            # No complete lines yet — wait for more data
                            pass
                        else:
                            offset += last_nl + 1
                            complete = raw_bytes[:last_nl + 1].decode("utf-8", errors="replace")
                            for raw in complete.splitlines():
                                raw = raw.strip()
                                if not raw:
                                    continue
                                try:
                                    cmd = json.loads(raw)
                                    cmd_queue.put(cmd)
                                    if cmd.get("type") == "quit":
                                        return
                                except Exception:
                                    log.debug("Failed to parse command JSON: %s", raw)
                    else:
                        offset = new_offset
                        for raw in data.splitlines():
                            raw = raw.strip()
                            if not raw:
                                continue
                            try:
                                cmd = json.loads(raw)
                                cmd_queue.put(cmd)
                                if cmd.get("type") == "quit":
                                    return
                            except Exception:
                                log.debug("Failed to parse command JSON: %s", raw)
        except Exception:
            log.debug("Command file reader error: %s", traceback.format_exc())
        time.sleep(0.15)


# ── Stdin reader (fallback for manual/test use) ───────────────────────────────

def _stdin_reader(cmd_queue: queue.Queue) -> None:
    try:
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                cmd_queue.put(json.loads(raw))
            except Exception:
                log.debug("Failed to parse stdin JSON: %s", raw)
    except Exception:
        log.debug("Stdin reader error: %s", traceback.format_exc())
    cmd_queue.put({"type": "quit"})


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    argv = sys.argv[1:]

    def _safe_arg(i, default, type_fn):
        try:
            return type_fn(argv[i])
        except (IndexError, ValueError, TypeError):
            return default

    win_x            = _safe_arg(0, 80, int)
    win_y            = _safe_arg(1, 80, int)
    win_w            = _safe_arg(2, 1400, int)
    win_h            = _safe_arg(3, 900, int)
    refresh_interval = _safe_arg(4, 300.0, float)
    max_routes       = _safe_arg(5, 500, int)
    opacity          = _safe_arg(6, 0.95, float)
    cmd_file         = _safe_arg(7, "", str)

    cmd_q = queue.Queue()

    if cmd_file and os.path.exists(cmd_file):
        threading.Thread(target=_cmd_file_reader, args=(cmd_q, cmd_file),
                         daemon=True, name="CmdFileReader").start()
    else:
        # Fallback: read from stdin (manual/test use)
        threading.Thread(target=_stdin_reader, args=(cmd_q,),
                         daemon=True, name="StdinReader").start()

    win = TradeHubWindow(cmd_q, win_x, win_y, win_w, win_h,
                         refresh_interval, max_routes, opacity)
    win.run()
