#!/usr/bin/env python3
"""
Mining Loadout — standalone GUI process.
Launched by the WingmanAI skill via subprocess using the system Python.
Fetches mining laser, module, and gadget data from UEX Corp API.
IPC via a JSONL temp file (same pattern as Trade Hub).
Requires only Python stdlib + tkinter + requests (or urllib fallback).
"""
import ctypes
import ctypes.wintypes
import json
import logging
import os
import queue
import re
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
import urllib.request
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from tkinter import ttk
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from shared.data_utils import retry_request, parse_cli_args
from shared.ipc import ipc_read_incremental
from shared.platform_utils import set_dpi_awareness, deterministic_hotkey_id

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mining_loadout.log")


def _setup_log() -> logging.Logger:
    lg = logging.getLogger("MiningLoadout")
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
log.info("Mining Loadout starting — Python %s", sys.version.split()[0])
log.info("Script: %s", os.path.abspath(__file__))

# ── Win32 ─────────────────────────────────────────────────────────────────────
if sys.platform == "win32":
    _user32   = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
else:
    _user32   = None
    _kernel32 = None
_HWND_TOPMOST   = -1
_SWP_NOSIZE     = 0x0001
_SWP_NOMOVE     = 0x0002
_SWP_NOACTIVATE = 0x0010
_SW_RESTORE     = 9

# ── Hotkey ────────────────────────────────────────────────────────────────────
_WM_HOTKEY   = 0x0312
_PM_REMOVE   = 0x0001
_MOD_ALT     = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT   = 0x0004
_MOD_WIN     = 0x0008
_VK_MAP: Dict[str, int] = {
    **{c: 0x41 + i for i, c in enumerate("ABCDEFGHIJKLMNOPQRSTUVWXYZ")},
    **{str(i): 0x30 + i for i in range(10)},
    "F1": 0x70, "F2": 0x71, "F3": 0x72,  "F4": 0x73,
    "F5": 0x74, "F6": 0x75, "F7": 0x76,  "F8": 0x77,
    "F9": 0x78, "F10":0x79, "F11":0x7A,  "F12":0x7B,
    "HOME":0x24, "END":0x23, "PGUP":0x21, "PGDN":0x22,
    "INS":0x2D, "DEL":0x2E, "SPACE":0x20, "TAB":0x09,
}
_DEFAULT_HOTKEY = "ctrl+shift+m"


def _parse_hotkey(hk: str) -> Tuple[int, int]:
    mods, vk = 0, 0
    for part in hk.upper().split("+"):
        part = part.strip()
        if   part in ("CTRL","CONTROL"): mods |= _MOD_CONTROL
        elif part == "SHIFT":            mods |= _MOD_SHIFT
        elif part == "ALT":              mods |= _MOD_ALT
        elif part in ("WIN","WINDOWS"):  mods |= _MOD_WIN
        else:                            vk    = _VK_MAP.get(part, 0)
    return mods, vk


UEX_BASE    = "https://api.uexcorp.space/2.0"
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mining_loadout_config.json")

# ── Colour palette (matching Trade Hub) ───────────────────────────────────────
C: Dict[str, str] = {
    "bg":    "#06080d",
    "bg2":   "#080c14",
    "hdr":   "#0b1120",
    "flt":   "#07090f",
    "even":  "#06080d",
    "odd":   "#090c15",
    "sel":   "#10203c",
    "fg":    "#b8ccde",
    "fg2":   "#3a5572",
    "fg3":   "#e8f2ff",
    "blue":  "#0090e0",
    "blue2": "#001a2e",
    "sep":   "#0d1824",
    "btn":   "#0c1626",
    "bar":   "#030407",
    "green": "#00dd70",
    "yellow":"#e0c000",
    "red":   "#e04020",
    "ibg":   "#080e1c",
    "ifg":   "#b8ccde",
    "panel": "#080c18",
    "amber": "#d4820a",
    "turret":"#0a1225",
}

# ── Data models ───────────────────────────────────────────────────────────────

@dataclass
class LaserItem:
    id:            int
    name:          str
    size:          int                  # 1 or 2  (0 = unspecified)
    company:       str
    min_power:     float                # aUEC
    max_power:     float                # aUEC
    ext_power:     Optional[float]      # aUEC (extraction laser power)
    opt_range:     Optional[float]      # m
    max_range:     Optional[float]      # m
    resistance:    Optional[float]      # % modifier  (e.g. +25)
    instability:   Optional[float]      # % modifier  (e.g. -35)
    inert:         Optional[float]      # % modifier  (e.g. -40)
    charge_window: Optional[float]      # % modifier
    charge_rate:   Optional[float]      # % modifier
    module_slots:  int                  # default 2
    price:         float = 0            # min buy price aUEC


@dataclass
class ModuleItem:
    id:            int
    name:          str
    item_type:     str                  # "Active" or "Passive"
    power_pct:     Optional[float]      # mining laser power multiplier ×100 (e.g. 135 = +35%)
    ext_power_pct: Optional[float]      # extraction laser power multiplier ×100 (e.g. 150 = +50%)
    resistance:    Optional[float]
    instability:   Optional[float]
    inert:         Optional[float]
    charge_rate:   Optional[float]
    charge_window: Optional[float]
    overcharge:    Optional[float]
    shatter:       Optional[float]
    uses:          int
    duration:      Optional[float]      # seconds (active only)
    price:         float = 0


@dataclass
class GadgetItem:
    id:            int
    name:          str
    charge_window: Optional[float]
    charge_rate:   Optional[float]
    instability:   Optional[float]
    resistance:    Optional[float]
    cluster:       Optional[float]
    price:         float = 0


# ── Ship configurations ────────────────────────────────────────────────────────
SHIPS: Dict[str, Dict] = {
    "Prospector": {
        "turrets":      1,
        "laser_size":   1,
        "module_slots": 2,
        "turret_names": ["Main Turret"],
        "stock_laser":  "Arbor MH1 Mining Laser",
    },
    "MOLE": {
        "turrets":      3,
        "laser_size":   2,
        "module_slots": 2,
        "turret_names": ["Front Turret", "Port Turret", "Starboard Turret"],
        "stock_laser":  "Arbor MH2 Mining Laser",
    },
    "Golem": {
        "turrets":      1,
        "laser_size":   1,
        "module_slots": 2,
        "turret_names": ["Main Turret"],
        "stock_laser":  "Pitman Mining Laser",
    },
}

_NONE_LASER  = "— No Laser —"
_NONE_MODULE = "— No Module —"
_NONE_GADGET = "— No Gadget —"

# ── Stat calculations ─────────────────────────────────────────────────────────

def _mult_stack(values: List[float]) -> float:
    """Multiplicative stacking: product(1 + v/100) - 1, result in %."""
    result = 1.0
    for v in values:
        if v != 0:
            result *= (1.0 + v / 100.0)
    return (result - 1.0) * 100.0


def calc_stats(
    ship: str,
    laser_items: List[Optional[LaserItem]],
    module_items: List[List[Optional[ModuleItem]]],
    gadget_item: Optional[GadgetItem],
) -> Dict[str, float]:
    """Calculate combined loadout stats using multiplicative stacking."""
    # Power: per-turret laser power × module power multipliers, summed across turrets.
    # UEX stores module Mining Laser Power as a multiplier ×100 (e.g. 135 = ×1.35 = +35%).
    # Power: module deltas additive within turret, then applied to laser.
    # e.g. Focus III (95 = -5%) + Surge (150 = +50%) → +45% not +42.5%
    min_pwr = 0.0
    max_pwr = 0.0
    ext_pwr = 0.0
    for i, laser in enumerate(laser_items):
        if not laser:
            continue
        mods = module_items[i] if i < len(module_items) else []
        # power_pct is ×100 (e.g. 135 = +35%). None means "not applicable" (from _float_attr).
        pwr_delta  = sum((m.power_pct - 100) / 100.0     for m in mods if m and m.power_pct is not None)
        ext_delta  = sum((m.ext_power_pct - 100) / 100.0 for m in mods if m and m.ext_power_pct is not None)
        pwr_mult = 1.0 + pwr_delta
        ext_mult = 1.0 + ext_delta
        min_pwr += laser.min_power * pwr_mult
        max_pwr += laser.max_power * pwr_mult
        ext_pwr += (laser.ext_power if laser.ext_power is not None else 0.0) * ext_mult

    # Range from first equipped laser
    first_laser = next((l for l in laser_items if l), None)
    opt_rng = first_laser.opt_range if first_laser and first_laser.opt_range is not None else 0.0
    max_rng = first_laser.max_range if first_laser and first_laser.max_range is not None else 0.0

    # Collect % modifiers
    resistances  = []
    instabilities= []
    inerts       = []
    chrg_windows = []
    chrg_rates   = []
    overcharges  = []
    shatters     = []
    clusters     = []

    for laser in laser_items:
        if not laser: continue
        if laser.resistance is not None:    resistances.append(laser.resistance)
        if laser.instability is not None:   instabilities.append(laser.instability)
        if laser.inert is not None:         inerts.append(laser.inert)
        if laser.charge_window is not None: chrg_windows.append(laser.charge_window)
        if laser.charge_rate is not None:   chrg_rates.append(laser.charge_rate)

    for turret_mods in module_items:
        # Module % stats are additive within a turret, then the per-turret
        # sum becomes one multiplicative term (matches Regolith's formula).
        t_res  = sum(m.resistance    for m in turret_mods if m and m.resistance is not None)
        t_inst = sum(m.instability   for m in turret_mods if m and m.instability is not None)
        t_inert= sum(m.inert         for m in turret_mods if m and m.inert is not None)
        t_cw   = sum(m.charge_window for m in turret_mods if m and m.charge_window is not None)
        t_cr   = sum(m.charge_rate   for m in turret_mods if m and m.charge_rate is not None)
        t_oc   = sum(m.overcharge    for m in turret_mods if m and m.overcharge is not None)
        t_shat = sum(m.shatter       for m in turret_mods if m and m.shatter is not None)
        if t_res != 0:   resistances.append(t_res)
        if t_inst != 0:  instabilities.append(t_inst)
        if t_inert != 0: inerts.append(t_inert)
        if t_cw != 0:    chrg_windows.append(t_cw)
        if t_cr != 0:    chrg_rates.append(t_cr)
        if t_oc != 0:    overcharges.append(t_oc)
        if t_shat != 0:  shatters.append(t_shat)

    if gadget_item:
        if gadget_item.resistance is not None:    resistances.append(gadget_item.resistance)
        if gadget_item.instability is not None:   instabilities.append(gadget_item.instability)
        if gadget_item.charge_window is not None: chrg_windows.append(gadget_item.charge_window)
        if gadget_item.charge_rate is not None:   chrg_rates.append(gadget_item.charge_rate)
        if gadget_item.cluster is not None:       clusters.append(gadget_item.cluster)

    return {
        "min_power":     min_pwr,
        "max_power":     max_pwr,
        "ext_power":     ext_pwr,
        "opt_range":     opt_rng,
        "max_range":     max_rng,
        "resistance":    _mult_stack(resistances),
        "instability":   _mult_stack(instabilities),
        "inert":         _mult_stack(inerts),
        "charge_window": _mult_stack(chrg_windows),
        "charge_rate":   _mult_stack(chrg_rates),
        "overcharge":    _mult_stack(overcharges),
        "shatter":       _mult_stack(shatters),
        "cluster":       _mult_stack(clusters),
    }


def calc_loadout_price(
    ship: str,
    laser_items: List[Optional[LaserItem]],
    module_items: List[List[Optional[ModuleItem]]],
    gadget_item: Optional[GadgetItem],
) -> float:
    stock_name = SHIPS.get(ship, {}).get("stock_laser", "")
    total = 0.0
    for laser in laser_items:
        if laser and laser.name != stock_name:
            total += laser.price
    for turret_mods in module_items:
        for mod in turret_mods:
            if mod:
                total += mod.price
    if gadget_item:
        total += gadget_item.price
    return total


# ── UEX API fetcher ───────────────────────────────────────────────────────────

def _uex_get(path: str) -> list:
    url = f"{UEX_BASE}/{path}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "WingmanAI-MiningLoadout/1.0", "Accept": "application/json"},
    )
    def _do_request():
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = resp.read()
            try:
                return json.loads(body).get("data", [])
            except (json.JSONDecodeError, ValueError):
                log.warning("Non-JSON response from %s", url)
                return []
    return retry_request(_do_request, retries=1)


def _parse_power(val: str) -> Tuple[float, float]:
    """Parse '480-2400' → (480.0, 2400.0). Single value → (v, v)."""
    s = str(val).strip() if val else ""
    if not s:
        return 0.0, 0.0
    m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', s)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError:
            log.debug("Failed to parse power range: %r", s)
    try:
        v = float(s)
        return v, v
    except ValueError:
        return 0.0, 0.0


def _float_attr(attrs: Dict[int, Dict[str, str]], iid: int, name: str) -> Optional[float]:
    raw = (attrs.get(iid, {}).get(name) or "").replace("%", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _str_attr(attrs: Dict[int, Dict[str, str]], iid: int, name: str) -> str:
    return (attrs.get(iid, {}).get(name) or "").strip()


def fetch_mining_data() -> Tuple[List[LaserItem], List[ModuleItem], List[GadgetItem]]:
    """Fetch all mining items from UEX API and build typed lists."""
    log.info("Fetching mining data from UEX API...")

    raw_lasers  = _uex_get("items/id_category/29")
    raw_modules = _uex_get("items/id_category/30")
    raw_gadgets = _uex_get("items/id_category/28")

    raw_laser_attrs  = _uex_get("items_attributes/id_category/29")
    raw_module_attrs = _uex_get("items_attributes/id_category/30")
    raw_gadget_attrs = _uex_get("items_attributes/id_category/28")

    raw_laser_prices  = _uex_get("items_prices/id_category/29")
    raw_module_prices = _uex_get("items_prices/id_category/30")
    raw_gadget_prices = _uex_get("items_prices/id_category/28")

    def build_attr_map(raw: list) -> Dict[int, Dict[str, str]]:
        m: Dict[int, Dict[str, str]] = {}
        for a in raw:
            m.setdefault(a.get("id_item", 0), {})[a.get("attribute_name", "")] = (a.get("value") or "")
        return m

    def build_price_map(raw: list) -> Dict[int, float]:
        m: Dict[int, float] = {}
        for p in raw:
            iid = p.get("id_item", 0)
            buy = float(p.get("price_buy") or 0)
            if buy > 0:
                m[iid] = min(m.get(iid, buy), buy)
        return m

    la = build_attr_map(raw_laser_attrs)
    ma = build_attr_map(raw_module_attrs)
    ga = build_attr_map(raw_gadget_attrs)

    lp = build_price_map(raw_laser_prices)
    mp = build_price_map(raw_module_prices)
    gp = build_price_map(raw_gadget_prices)

    # ── Lasers ────────────────────────────────────────────────────────────────
    lasers: List[LaserItem] = []
    for r in raw_lasers:
        iid = r.get("id")
        if iid is None: continue
        try:
            sz = int(r.get("size") or _str_attr(la, iid, "Size") or 0)
        except Exception:
            sz = 0
        min_p, max_p = _parse_power(_str_attr(la, iid, "Mining Laser Power"))
        lasers.append(LaserItem(
            id           = iid,
            name         = r.get("name", ""),
            size         = sz,
            company      = r.get("company_name", ""),
            min_power    = min_p,
            max_power    = max_p,
            ext_power    = _float_attr(la, iid, "Extraction Laser Power"),
            opt_range    = _float_attr(la, iid, "Optimal Range"),
            max_range    = _float_attr(la, iid, "Maximum Range"),
            resistance   = _float_attr(la, iid, "Resistance"),
            instability  = _float_attr(la, iid, "Laser Instability"),
            inert        = _float_attr(la, iid, "Inert Material Level"),
            charge_window= _float_attr(la, iid, "Optimal Charge Window Size"),
            charge_rate  = _float_attr(la, iid, "Optimal Charge Window Rate"),
            module_slots = int(ms if (ms := _float_attr(la, iid, "Module Slots")) is not None else 2),
            price        = lp.get(iid, 0),
        ))

    # ── Modules ───────────────────────────────────────────────────────────────
    modules: List[ModuleItem] = []
    for r in raw_modules:
        iid = r.get("id")
        if iid is None: continue
        modules.append(ModuleItem(
            id           = iid,
            name         = r.get("name", ""),
            item_type    = _str_attr(ma, iid, "Item Type") or "Passive",
            power_pct    = _float_attr(ma, iid, "Mining Laser Power"),
            ext_power_pct= _float_attr(ma, iid, "Extraction Laser Power"),
            resistance   = _float_attr(ma, iid, "Resistance"),
            instability  = _float_attr(ma, iid, "Laser Instability"),
            inert        = _float_attr(ma, iid, "Inert Material Level"),
            charge_rate  = _float_attr(ma, iid, "Optimal Charge Rate"),
            charge_window= _float_attr(ma, iid, "Optimal Charge Window Size"),
            overcharge   = _float_attr(ma, iid, "Catastrophic Charge Rate"),
            shatter      = _float_attr(ma, iid, "Shatter Damage"),
            uses         = int(_float_attr(ma, iid, "Uses") or 0),
            duration     = _float_attr(ma, iid, "Duration"),
            price        = mp.get(iid, 0),
        ))

    # ── Gadgets ───────────────────────────────────────────────────────────────
    gadgets: List[GadgetItem] = []
    for r in raw_gadgets:
        iid = r.get("id")
        if iid is None: continue
        gadgets.append(GadgetItem(
            id           = iid,
            name         = r.get("name", ""),
            charge_window= _float_attr(ga, iid, "Optimal Charge Window Size"),
            charge_rate  = _float_attr(ga, iid, "Optimal Charge Window Rate"),
            instability  = _float_attr(ga, iid, "Laser Instability"),
            resistance   = _float_attr(ga, iid, "Resistance"),
            cluster      = _float_attr(ga, iid, "Cluster Modifier"),
            price        = gp.get(iid, 0),
        ))

    log.info("Fetched %d lasers, %d modules, %d gadgets", len(lasers), len(modules), len(gadgets))
    return lasers, modules, gadgets


# ── GUI ───────────────────────────────────────────────────────────────────────

MAX_PINNED = 5


class MiningLoadoutWindow:
    def __init__(
        self,
        cmd_queue: queue.Queue,
        win_x: int = 80,
        win_y: int = 80,
        win_w: int = 1200,
        win_h: int = 720,
        refresh_interval: float = 86400.0,
        opacity: float = 0.95,
    ):
        self.cmd_queue        = cmd_queue
        self.win_x            = win_x
        self.win_y            = win_y
        self.win_w            = win_w
        self.win_h            = win_h
        self.opacity          = max(0.3, min(1.0, opacity))
        self.refresh_interval = refresh_interval

        # Data
        self.all_lasers:  List[LaserItem]  = []
        self.all_modules: List[ModuleItem] = []
        self.all_gadgets: List[GadgetItem] = []
        self._data_loaded     = False
        self._fetching        = False
        self._last_fetch_ts: Optional[float] = None

        # Loadout state
        self.ship_name = "MOLE"
        self._turret_laser_vars:  List[tk.StringVar]        = []
        self._turret_module_vars: List[List[tk.StringVar]]  = []

        # UI references
        self.root:             Optional[tk.Tk]      = None
        self._turret_frames:   List[tk.Frame]       = []
        self._turret_area:     Optional[tk.Frame]   = None
        self._stat_vars:       Dict[str, tk.StringVar] = {}
        self._price_detail_var: Optional[tk.StringVar] = None
        self._src_detail_var:  Optional[tk.StringVar]  = None
        self._price_var:       Optional[tk.StringVar]  = None
        self._status_var:      Optional[tk.StringVar]  = None
        self._upd_var:         Optional[tk.StringVar]  = None
        self._src_var:         Optional[tk.StringVar]  = None
        self._gadget_var:      Optional[tk.StringVar]  = None
        self._gadget_combo:    Optional[ttk.Combobox]  = None
        self._laser_combos:    List[ttk.Combobox]      = []
        self._module_combos:   List[List[ttk.Combobox]] = []
        self._ship_btns:       Dict[str, tk.Button]    = {}
        self._pinned_cards:    List[Dict]               = []

        # Hotkey
        self._hotkey           = _DEFAULT_HOTKEY
        self._hotkey_stop:     Optional[threading.Event] = None
        self._hotkey_entry_var:  Optional[tk.StringVar]  = None
        self._hotkey_status_var: Optional[tk.StringVar]  = None

        # Drag
        self._drag_x = self._drag_y = 0

    # ── Entry ──────────────────────────────────────────────────────────────────

    def run(self) -> None:
        try:
            self._run_inner()
        except Exception:
            tb = traceback.format_exc()
            log.critical("FATAL:\n%s", tb)
            try:
                p = os.path.join(tempfile.gettempdir(), "mining_loadout_error.txt")
                with open(p, "w") as f:
                    f.write(tb)
            except Exception:
                log.debug("Could not write crash file: %s", traceback.format_exc())

    def _run_inner(self) -> None:
        log.info("Window init — %dx%d+%d+%d  opacity=%.2f", self.win_w, self.win_h, self.win_x, self.win_y, self.opacity)
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
        self.root.configure(bg=C["bg"])
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
        self._keepalive()
        self.root.after(400, self._start_load)
        log.info("Entering mainloop")
        self.root.mainloop()
        log.info("mainloop exited")

    # ── Win32 ─────────────────────────────────────────────────────────────────

    def _get_hwnd(self) -> Optional[int]:
        try:
            frame = self.root.wm_frame()
            try:    return int(frame)
            except Exception: return int(frame, 16)
        except Exception:
            try:    return self.root.winfo_id()
            except Exception: return None

    def _apply_topmost(self) -> None:
        hwnd = self._get_hwnd()
        if hwnd:
            try:
                _user32.SetWindowPos(hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                                     _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)
            except Exception:
                log.debug("SetWindowPos topmost failed: %s", traceback.format_exc())

    def _force_show(self) -> None:
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
            if self.root.state() == "withdrawn":
                self.root.after(2000, self._keepalive)
                return
            self._apply_topmost()
            self.root.after(2000, self._keepalive)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TFrame",  background=C["bg"])
        s.configure("T.TCombobox",
                    fieldbackground=C["ibg"], foreground=C["ifg"],
                    background=C["btn"], arrowcolor=C["amber"],
                    selectbackground=C["sel"], borderwidth=0, font=("Consolas", 9))
        s.map("T.TCombobox",
              fieldbackground=[("readonly", C["ibg"])],
              selectbackground=[("readonly", C["sel"])])
        s.configure("T.TEntry",
                    fieldbackground=C["ibg"], foreground=C["ifg"],
                    insertcolor=C["amber"], borderwidth=0, relief="flat", font=("Consolas", 9))
        for orient in ("Vertical", "Horizontal"):
            s.configure(f"T.{orient}.TScrollbar",
                        background=C["bg2"], troughcolor=C["bg"],
                        arrowcolor=C["blue2"], borderwidth=0, relief="flat")

    # ── Build UI ──────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── Title bar ─────────────────────────────────────────────────────────
        bar = tk.Frame(self.root, bg=C["bar"], height=42)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        def drag(w):
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_motion)

        drag(bar)
        t = tk.Label(bar, text="⛏  MINING LOADOUT", bg=C["bar"], fg=C["amber"],
                     font=("Consolas", 14, "bold"), cursor="fleur")
        t.pack(side="left", padx=12, pady=9)
        drag(t)
        s = tk.Label(bar, text="LASER CONFIGURATION TERMINAL",
                     bg=C["bar"], fg=C["fg2"], font=("Consolas", 8), cursor="fleur")
        s.pack(side="left", pady=13)
        drag(s)

        close = tk.Label(bar, text=" ✕ ", bg=C["bar"], fg=C["amber"],
                         font=("Consolas", 11, "bold"), cursor="hand2")
        close.pack(side="right", padx=(4, 10), pady=8)
        close.bind("<Button-1>", lambda _: self.root.withdraw())
        close.bind("<Enter>",    lambda _: close.config(fg="#ff4040"))
        close.bind("<Leave>",    lambda _: close.config(fg=C["amber"]))

        rbtn = tk.Label(bar, text=" ⟳ ", bg=C["bar"], fg=C["amber"],
                        font=("Consolas", 14), cursor="hand2")
        rbtn.pack(side="right", padx=4, pady=8)
        rbtn.bind("<Button-1>", lambda _: self._do_refresh())
        rbtn.bind("<Enter>",    lambda _: rbtn.config(fg=C["fg3"]))
        rbtn.bind("<Leave>",    lambda _: rbtn.config(fg=C["amber"]))

        self._upd_var = tk.StringVar(value="")
        self._src_var = tk.StringVar(value="")
        tk.Label(bar, textvariable=self._src_var, bg=C["bar"], fg=C["fg2"],
                 font=("Consolas", 8)).pack(side="right", padx=6, pady=13)
        tk.Label(bar, textvariable=self._upd_var, bg=C["bar"], fg=C["fg2"],
                 font=("Consolas", 9)).pack(side="right", padx=6, pady=13)

        tk.Frame(self.root, bg=C["amber"], height=1).pack(fill="x")

        # ── Body ──────────────────────────────────────────────────────────────
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True)

        # ── Left sidebar ───────────────────────────────────────────────────────
        SB_W = 195
        sb_outer = tk.Frame(body, bg=C["flt"], width=SB_W)
        sb_outer.pack(side="left", fill="y")
        sb_outer.pack_propagate(False)

        sb_canvas = tk.Canvas(sb_outer, bg=C["flt"], highlightthickness=0, width=SB_W)
        sb_canvas.pack(side="left", fill="both", expand=True)
        sb_frame = tk.Frame(sb_canvas, bg=C["flt"])
        sb_canvas.create_window((0, 0), window=sb_frame, anchor="nw", width=SB_W)
        sb_frame.bind("<Configure>", lambda e: sb_canvas.configure(scrollregion=sb_canvas.bbox("all")))
        sb_canvas.bind("<MouseWheel>", lambda e: sb_canvas.yview_scroll(int(-1*(e.delta/120)), "units"))

        def sb_label(txt, pad_top=6):
            tk.Label(sb_frame, text=txt, bg=C["flt"], fg=C["fg2"],
                     font=("Consolas", 8), anchor="w").pack(fill="x", padx=10, pady=(pad_top, 0))

        # Ship selector
        sb_label("MINING SHIP:", pad_top=10)
        for ship in SHIPS:
            btn = tk.Button(
                sb_frame, text=ship.upper(),
                font=("Consolas", 9, "bold"), relief="flat", cursor="hand2", pady=4,
                command=lambda s=ship: self._on_ship_changed(s),
            )
            btn.pack(fill="x", padx=10, pady=(2, 0))
            self._ship_btns[ship] = btn
        self._update_ship_btn_styles()

        tk.Frame(sb_frame, bg=C["sep"], height=1).pack(fill="x", padx=6, pady=8)

        # Reset button
        rst_btn = tk.Button(
            sb_frame, text="↺  RESET LOADOUT",
            bg=C["btn"], fg=C["fg"],
            font=("Consolas", 9), relief="flat", cursor="hand2", pady=4,
            command=self._reset_loadout,
        )
        rst_btn.pack(fill="x", padx=10, pady=(0, 4))
        rst_btn.bind("<Enter>", lambda _: rst_btn.config(bg=C["sel"]))
        rst_btn.bind("<Leave>", lambda _: rst_btn.config(bg=C["btn"]))

        # Copy stats button
        copy_btn = tk.Button(
            sb_frame, text="📋  COPY STATS",
            bg=C["btn"], fg=C["green"],
            font=("Consolas", 9), relief="flat", cursor="hand2", pady=4,
            command=self._copy_stats,
        )
        copy_btn.pack(fill="x", padx=10, pady=(0, 10))
        copy_btn.bind("<Enter>", lambda _: copy_btn.config(bg=C["sel"]))
        copy_btn.bind("<Leave>", lambda _: copy_btn.config(bg=C["btn"]))

        tk.Frame(sb_frame, bg=C["sep"], height=1).pack(fill="x", padx=10, pady=(4, 0))

        # Hotkey
        sb_label("HOTKEY:", pad_top=8)
        self._hotkey_entry_var  = tk.StringVar(value=self._hotkey)
        self._hotkey_status_var = tk.StringVar(value="")
        hk_entry = tk.Entry(
            sb_frame, textvariable=self._hotkey_entry_var,
            bg=C["ibg"], fg=C["ifg"], insertbackground=C["amber"],
            font=("Consolas", 9), relief="flat", borderwidth=0,
        )
        hk_entry.pack(fill="x", padx=10, pady=(2, 0), ipady=4)
        tk.Label(sb_frame, text="e.g. ctrl+shift+m",
                 bg=C["flt"], fg=C["fg2"], font=("Consolas", 7)).pack(anchor="w", padx=12, pady=(1, 2))
        tk.Label(sb_frame, textvariable=self._hotkey_status_var,
                 bg=C["flt"], fg=C["green"], font=("Consolas", 7)).pack(anchor="w", padx=12)
        hk_btn = tk.Button(
            sb_frame, text="APPLY HOTKEY",
            bg=C["btn"], fg=C["blue"],
            font=("Consolas", 8), relief="flat", cursor="hand2", pady=3,
            command=self._apply_hotkey,
        )
        hk_btn.pack(fill="x", padx=10, pady=(4, 0))
        hk_btn.bind("<Enter>", lambda _: hk_btn.config(bg=C["blue2"]))
        hk_btn.bind("<Leave>", lambda _: hk_btn.config(bg=C["btn"]))

        tk.Frame(sb_frame, bg=C["sep"], height=1).pack(fill="x", padx=10, pady=8)

        # Status
        self._status_var = tk.StringVar(value="  Loading data…")
        tk.Label(sb_frame, textvariable=self._status_var,
                 bg=C["flt"], fg=C["fg2"], font=("Consolas", 7),
                 wraplength=175, justify="left").pack(anchor="w", padx=10, pady=(0, 8))

        # ── Right: Stats panel ─────────────────────────────────────────────────
        STATS_W = 268
        tk.Frame(body, bg=C["amber"], width=1).pack(side="right", fill="y")
        stats_outer = tk.Frame(body, bg=C["panel"], width=STATS_W)
        stats_outer.pack(side="right", fill="y")
        stats_outer.pack_propagate(False)
        self._build_stats_panel(stats_outer)

        # ── Center: turret area + inventory ───────────────────────────────────
        center = tk.Frame(body, bg=C["bg"])
        center.pack(side="left", fill="both", expand=True)

        self._turret_area = tk.Frame(center, bg=C["bg"])
        self._turret_area.pack(fill="both", expand=True, padx=6, pady=6)

        # Inventory strip (gadget)
        inv_frame = tk.Frame(center, bg=C["hdr"])
        inv_frame.pack(fill="x", padx=6, pady=(0, 6))
        tk.Label(inv_frame, text="  INVENTORY — GADGET",
                 bg=C["hdr"], fg=C["amber"],
                 font=("Consolas", 9, "bold")).pack(side="left", pady=6)
        self._gadget_var = tk.StringVar(value=_NONE_GADGET)
        self._gadget_combo = ttk.Combobox(
            inv_frame, textvariable=self._gadget_var,
            values=[_NONE_GADGET], width=30, style="T.TCombobox", state="readonly",
        )
        self._gadget_combo.pack(side="left", padx=(4, 4), pady=6)
        self._gadget_combo.bind("<<ComboboxSelected>>", lambda _: self._on_loadout_changed())

        ginfo = tk.Label(inv_frame, text=" ⓘ ", bg=C["hdr"], fg=C["blue"],
                         font=("Consolas", 11), cursor="hand2")
        ginfo.pack(side="left", padx=2)
        ginfo.bind("<Button-1>", lambda _: self._pin_item("gadget"))

        # Status bar
        tk.Frame(self.root, bg=C["amber"], height=1).pack(fill="x", side="bottom")
        status_bar = tk.Frame(self.root, bg=C["hdr"], height=22)
        status_bar.pack(fill="x", side="bottom")
        status_bar.pack_propagate(False)
        self._price_var = tk.StringVar(value="  Loadout Price:  — aUEC")
        tk.Label(status_bar, textvariable=self._price_var,
                 bg=C["hdr"], fg=C["green"], font=("Consolas", 9)).pack(side="left", padx=8, pady=2)

        # Build turret panels for default ship
        self._rebuild_turret_panels()

    def _build_stats_panel(self, parent: tk.Frame) -> None:
        tk.Label(parent, text="  LOADOUT STATS", bg=C["panel"], fg=C["amber"],
                 font=("Consolas", 10, "bold")).pack(fill="x", pady=(12, 2))
        tk.Frame(parent, bg=C["sep"], height=1).pack(fill="x", padx=8, pady=2)

        # (key, label, unit, good_direction: 1=positive, -1=negative, 0=neutral)
        STATS_DEF = [
            ("min_power",     "Min Power",     " aUEC", 0),
            ("max_power",     "Max Power",     " aUEC", 0),
            ("ext_power",     "Ext Power",     " aUEC", 0),
            None,
            ("opt_range",     "Opt Range",     " m",    0),
            ("max_range",     "Max Range",     " m",    0),
            None,
            ("resistance",    "Resistance",    "%",     1),
            ("instability",   "Instability",   "%",    -1),
            ("inert",         "Inert Mat.",    "%",    -1),
            None,
            ("charge_window", "Opt Chrg Wnd",  "%",     1),
            ("charge_rate",   "Opt Chrg Rate", "%",     0),
            ("overcharge",    "Overcharge",    "%",    -1),
            ("cluster",       "Cluster",       "%",     0),
            ("shatter",       "Shatter",       "%",    -1),
        ]

        self._stat_colors: Dict[str, Tuple[str, int]] = {}
        for entry in STATS_DEF:
            if entry is None:
                tk.Frame(parent, bg=C["sep"], height=1).pack(fill="x", padx=8, pady=3)
                continue
            key, label, unit, direction = entry
            row = tk.Frame(parent, bg=C["panel"])
            row.pack(fill="x", padx=12, pady=1)
            tk.Label(row, text=f"{label}:", bg=C["panel"], fg=C["fg2"],
                     font=("Consolas", 8), width=14, anchor="w").pack(side="left")
            var = tk.StringVar(value="—")
            lbl = tk.Label(row, textvariable=var, bg=C["panel"], fg=C["fg"],
                           font=("Consolas", 9, "bold"), anchor="e")
            lbl.pack(side="right")
            self._stat_vars[key]    = var
            self._stat_colors[key]  = (lbl, direction)

        tk.Frame(parent, bg=C["sep"], height=1).pack(fill="x", padx=8, pady=(8, 3))
        tk.Label(parent, text="  LOADOUT PRICE", bg=C["panel"], fg=C["amber"],
                 font=("Consolas", 9, "bold")).pack(anchor="w", pady=(4, 0))
        self._price_detail_var = tk.StringVar(value="0 aUEC")
        tk.Label(parent, textvariable=self._price_detail_var,
                 bg=C["panel"], fg=C["green"],
                 font=("Consolas", 11, "bold")).pack(anchor="w", padx=16, pady=(2, 0))

        tk.Frame(parent, bg=C["sep"], height=1).pack(fill="x", padx=8, pady=4)
        self._src_detail_var = tk.StringVar(value="Select a ship and equipment above")
        tk.Label(parent, textvariable=self._src_detail_var,
                 bg=C["panel"], fg=C["fg2"], font=("Consolas", 7),
                 wraplength=248, justify="left").pack(anchor="w", padx=12, pady=(0, 8))

    # ── Turret panel builder ───────────────────────────────────────────────────

    def _rebuild_turret_panels(self) -> None:
        if not self._turret_area:
            return
        for w in self._turret_area.winfo_children():
            w.destroy()
        self._turret_frames.clear()
        self._laser_combos.clear()
        self._module_combos.clear()

        cfg    = SHIPS[self.ship_name]
        n      = cfg["turrets"]
        names  = cfg["turret_names"]
        lsize  = cfg["laser_size"]
        stock  = cfg.get("stock_laser", "")

        # Ensure StringVars exist
        while len(self._turret_laser_vars)  < n:
            self._turret_laser_vars.append(tk.StringVar(value=_NONE_LASER))
        while len(self._turret_module_vars) < n:
            self._turret_module_vars.append([
                tk.StringVar(value=_NONE_MODULE),
                tk.StringVar(value=_NONE_MODULE),
            ])

        # Trim lists to match current ship's turret count
        self._turret_laser_vars = self._turret_laser_vars[:n]
        self._turret_module_vars = self._turret_module_vars[:n]

        # Reset values to stock
        for i in range(n):
            self._turret_laser_vars[i].set(stock or _NONE_LASER)
            for j in range(2):
                self._turret_module_vars[i][j].set(_NONE_MODULE)

        for i in range(n):
            outer = tk.Frame(self._turret_area, bg=C["turret"], bd=0,
                             highlightthickness=1, highlightbackground=C["amber"])
            outer.pack(side="left", fill="both", expand=True,
                       padx=(0 if i == 0 else 4, 0))
            self._turret_frames.append(outer)

            # Title bar
            tbar = tk.Frame(outer, bg=C["amber"])
            tbar.pack(fill="x")
            tk.Label(tbar, text=f"  {names[i].upper()}", bg=C["amber"], fg=C["bg"],
                     font=("Consolas", 9, "bold")).pack(side="left", pady=4)
            tk.Label(tbar, text=f"SIZE {lsize}  ", bg=C["amber"], fg=C["bg"],
                     font=("Consolas", 7)).pack(side="right")

            content = tk.Frame(outer, bg=C["turret"])
            content.pack(fill="both", expand=True, padx=8, pady=8)

            # Laser
            tk.Label(content, text="LASER HEAD", bg=C["turret"], fg=C["fg2"],
                     font=("Consolas", 7)).pack(anchor="w", pady=(0, 2))
            laser_combo = ttk.Combobox(
                content, textvariable=self._turret_laser_vars[i],
                values=[_NONE_LASER], width=26, style="T.TCombobox", state="readonly",
            )
            laser_combo.pack(fill="x", pady=(0, 2))
            laser_combo.bind("<<ComboboxSelected>>", lambda _, idx=i: self._on_loadout_changed())
            self._laser_combos.append(laser_combo)

            lrow = tk.Frame(content, bg=C["turret"])
            lrow.pack(fill="x", pady=(0, 6))
            li = tk.Label(lrow, text=" ⓘ Details", bg=C["turret"], fg=C["blue"],
                          font=("Consolas", 8), cursor="hand2")
            li.pack(side="left")
            li.bind("<Button-1>", lambda _, ti=i: self._pin_item("laser", ti))

            # Module slots
            turret_mod_combos = []
            for slot in range(2):
                tk.Label(content, text=f"MODULE SLOT {slot + 1}", bg=C["turret"], fg=C["fg2"],
                         font=("Consolas", 7)).pack(anchor="w", pady=(4, 2))
                mc = ttk.Combobox(
                    content, textvariable=self._turret_module_vars[i][slot],
                    values=[_NONE_MODULE], width=26, style="T.TCombobox", state="readonly",
                )
                mc.pack(fill="x", pady=(0, 2))
                mc.bind("<<ComboboxSelected>>", lambda _, idx=i, s=slot: self._on_loadout_changed())
                turret_mod_combos.append(mc)

                mrow = tk.Frame(content, bg=C["turret"])
                mrow.pack(fill="x", pady=(0, 2))
                mi = tk.Label(mrow, text=" ⓘ Details", bg=C["turret"], fg=C["blue"],
                              font=("Consolas", 8), cursor="hand2")
                mi.pack(side="left")
                mi.bind("<Button-1>", lambda _, ti=i, sl=slot: self._pin_item("module", ti, sl))

            self._module_combos.append(turret_mod_combos)

        if self._data_loaded:
            self._populate_dropdowns()
        self._on_loadout_changed()

    def _populate_dropdowns(self) -> None:
        cfg   = SHIPS[self.ship_name]
        lsize = cfg["laser_size"]
        n     = cfg["turrets"]
        stock = cfg.get("stock_laser", "")

        laser_names  = [_NONE_LASER] + sorted(
            l.name for l in self.all_lasers if l.size == 0 or l.size == lsize
        )
        module_names = [_NONE_MODULE] + sorted(m.name for m in self.all_modules)
        gadget_names = [_NONE_GADGET] + sorted(g.name for g in self.all_gadgets)

        for i in range(n):
            if i < len(self._laser_combos):
                self._laser_combos[i]["values"] = laser_names
                cur = self._turret_laser_vars[i].get()
                if cur not in laser_names:
                    self._turret_laser_vars[i].set(stock if stock in laser_names else _NONE_LASER)
            if i < len(self._module_combos):
                for j, mc in enumerate(self._module_combos[i]):
                    mc["values"] = module_names
                    cur = self._turret_module_vars[i][j].get()
                    if cur not in module_names:
                        self._turret_module_vars[i][j].set(_NONE_MODULE)

        if self._gadget_combo is not None:
            self._gadget_combo["values"] = gadget_names
            cur = self._gadget_var.get() if self._gadget_var else ""
            if cur not in gadget_names and self._gadget_var:
                self._gadget_var.set(_NONE_GADGET)

    # ── Stat calculation & update ──────────────────────────────────────────────

    def _on_loadout_changed(self, _=None) -> None:
        self._update_module_slot_states()
        self._update_stats()
        self._save_config()

    def _update_module_slot_states(self) -> None:
        """Enable/disable module slot dropdowns based on the selected laser's module_slots count."""
        cfg = SHIPS[self.ship_name]
        n = cfg["turrets"]
        for i in range(n):
            lname = self._turret_laser_vars[i].get() if i < len(self._turret_laser_vars) else ""
            laser = self._get_laser(lname)
            slots = laser.module_slots if laser else 2
            if i < len(self._module_combos):
                for j, mc in enumerate(self._module_combos[i]):
                    if j < slots:
                        mc.configure(state="readonly")
                    else:
                        mc.configure(state="disabled")
                        if i < len(self._turret_module_vars) and j < len(self._turret_module_vars[i]):
                            self._turret_module_vars[i][j].set(_NONE_MODULE)

    def _get_laser(self, name: str) -> Optional[LaserItem]:
        return next((l for l in self.all_lasers if l.name == name), None)

    def _get_module(self, name: str) -> Optional[ModuleItem]:
        return next((m for m in self.all_modules if m.name == name), None)

    def _get_gadget(self, name: str) -> Optional[GadgetItem]:
        return next((g for g in self.all_gadgets if g.name == name), None)

    def _update_stats(self) -> None:
        cfg   = SHIPS[self.ship_name]
        n     = cfg["turrets"]
        stock = cfg.get("stock_laser", "")

        laser_items:  List[Optional[LaserItem]]              = []
        module_items: List[List[Optional[ModuleItem]]]       = []

        for i in range(n):
            lname = self._turret_laser_vars[i].get() if i < len(self._turret_laser_vars) else ""
            laser_items.append(self._get_laser(lname))
            mods: List[Optional[ModuleItem]] = []
            for j in range(2):
                mname = self._turret_module_vars[i][j].get() if i < len(self._turret_module_vars) else ""
                mods.append(self._get_module(mname))
            module_items.append(mods)

        gname  = self._gadget_var.get() if self._gadget_var else ""
        gadget = self._get_gadget(gname)

        stats = calc_stats(self.ship_name, laser_items, module_items, gadget)
        price = calc_loadout_price(self.ship_name, laser_items, module_items, gadget)

        def fmt_pct(v: float) -> str:
            if v == 0: return "0%"
            return f"{v:+.0f}%"

        def fmt_pwr(v: float) -> str:
            if v is None:
                return "—"
            if v < 1000:
                return f"{int(v + 0.5):,}"   # JS-style rounding for sub-1000 values
            return f"{v:,.1f}"           # 1 decimal for larger values

        def fmt_rng(v: float) -> str:
            return f"{v:.0f} m" if v else "—"

        for key, var in self._stat_vars.items():
            v = stats.get(key, 0)
            if key in ("min_power", "max_power", "ext_power"):
                var.set(fmt_pwr(v))
            elif key in ("opt_range", "max_range"):
                var.set(fmt_rng(v))
            else:
                var.set(fmt_pct(v))
            # Update label colour based on value direction
            if key in self._stat_colors:
                lbl, direction = self._stat_colors[key]
                if direction == 0 or v == 0:
                    color = C["fg"]
                elif direction == 1:
                    color = C["green"] if v > 0 else (C["red"] if v < 0 else C["fg"])
                else:  # direction == -1: negative is good
                    color = C["green"] if v < 0 else (C["red"] if v > 0 else C["fg"])
                lbl.config(fg=color)

        if self._price_detail_var:
            self._price_detail_var.set(f"{price:,.0f} aUEC")
        if self._price_var:
            self._price_var.set(f"  Loadout Price:  {price:,.0f} aUEC")
        if self._src_detail_var:
            self._src_detail_var.set(
                f"Stock laser: {stock} (free)\nData: UEX Corp API"
            )

    # ── Pop-out detail cards ──────────────────────────────────────────────────

    def _card_position(self, idx: int) -> Tuple[int, int]:
        wx = self.root.winfo_x()
        wy = self.root.winfo_y()
        ww = self.root.winfo_width()
        wh = self.root.winfo_height()
        card_w, card_h = 430, 460
        base_x = wx + (ww - card_w) // 2
        base_y = wy + (wh - card_h) // 2
        return base_x + idx * 26, base_y + idx * 30

    def _make_card(self, key: str, title: str, subtitle: str) -> Tuple[tk.Toplevel, tk.Text, tk.BooleanVar]:
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.wm_attributes("-topmost", True)
        popup.wm_attributes("-alpha", self.opacity)
        popup.configure(bg=C["bar"])

        idx = len(self._pinned_cards)
        cx, cy = self._card_position(idx)
        popup.geometry(f"430x460+{cx}+{cy}")

        # Title bar
        bar = tk.Frame(popup, bg=C["bar"], height=36)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        def drag_start(e): popup._dx = e.x; popup._dy = e.y
        def drag_motion(e):
            popup.geometry(f"+{popup.winfo_x()+e.x-popup._dx}+{popup.winfo_y()+e.y-popup._dy}")

        bar.bind("<Button-1>",  drag_start)
        bar.bind("<B1-Motion>", drag_motion)
        tk.Label(bar, text=f"  {title}", bg=C["bar"], fg=C["amber"],
                 font=("Consolas", 10, "bold")).pack(side="left", pady=8)
        tk.Label(bar, text=subtitle, bg=C["bar"], fg=C["fg2"],
                 font=("Consolas", 7)).pack(side="left", padx=4, pady=12)

        x_btn = tk.Label(bar, text=" ✕ ", bg=C["bar"], fg=C["amber"],
                         font=("Consolas", 10, "bold"), cursor="hand2")
        x_btn.pack(side="right", padx=(4, 6))
        x_btn.bind("<Button-1>", lambda _: self._close_card(key))
        x_btn.bind("<Enter>",    lambda _: x_btn.config(fg="#ff4040"))
        x_btn.bind("<Leave>",    lambda _: x_btn.config(fg=C["amber"]))

        lock_var = tk.BooleanVar(value=False)
        def _on_lock():
            lock_btn.config(fg=C["green"] if lock_var.get() else C["fg2"])
        lock_btn = tk.Checkbutton(
            bar, text="⚲", variable=lock_var, command=_on_lock,
            bg=C["bar"], fg=C["fg2"], activebackground=C["bar"], activeforeground=C["green"],
            selectcolor=C["bar"], font=("Consolas", 13), cursor="hand2",
            relief="flat", borderwidth=0, indicatoron=False,
        )
        lock_btn.pack(side="right", padx=(0, 2), pady=8)

        tk.Frame(popup, bg=C["amber"], height=1).pack(fill="x")

        dt = tk.Text(popup, bg=C["bg2"], fg=C["fg"], font=("Consolas", 9),
                     wrap="word", borderwidth=0, relief="flat", padx=14, pady=10,
                     selectbackground=C["sel"])
        dt.pack(fill="both", expand=True)
        dt.tag_configure("heading",  foreground=C["amber"],  font=("Consolas", 10, "bold"))
        dt.tag_configure("label",    foreground=C["fg2"],    font=("Consolas", 9))
        dt.tag_configure("value",    foreground=C["fg3"],    font=("Consolas", 9, "bold"))
        dt.tag_configure("positive", foreground=C["green"],  font=("Consolas", 9, "bold"))
        dt.tag_configure("negative", foreground=C["red"],    font=("Consolas", 9, "bold"))
        dt.tag_configure("neutral",  foreground=C["yellow"], font=("Consolas", 9, "bold"))
        dt.tag_configure("divider",  foreground=C["sep"])
        dt.tag_configure("section",  foreground=C["blue"],   font=("Consolas", 9, "bold"))

        # Resize grip
        grip = tk.Label(popup, text="⠿", bg=C["bar"], fg=C["fg2"],
                        font=("Consolas", 10), cursor="size_nw_se")
        grip.pack(side="bottom", anchor="se", padx=4, pady=2)
        grip._sx = grip._sy = grip._sw = grip._sh = 0
        def rz_start(e):
            grip._sx = popup.winfo_rootx(); grip._sy = popup.winfo_rooty()
            grip._sw = popup.winfo_width(); grip._sh = popup.winfo_height()
        def rz_move(e):
            nw = max(320, grip._sw + (e.x_root - grip._sx - grip._sw + grip.winfo_width()))
            nh = max(200, grip._sh + (e.y_root - grip._sy - grip._sh + grip.winfo_height()))
            popup.geometry(f"{int(nw)}x{int(nh)}")
        grip.bind("<Button-1>",  rz_start)
        grip.bind("<B1-Motion>", rz_move)
        popup.protocol("WM_DELETE_WINDOW", lambda: self._close_card(key))
        return popup, dt, lock_var

    def _close_card(self, key: str) -> None:
        card = next((c for c in self._pinned_cards if c["key"] == key), None)
        if card:
            try: card["popup"].destroy()
            except Exception: log.debug("Failed to destroy card popup for key=%s", key)
            self._pinned_cards = [c for c in self._pinned_cards if c["key"] != key]

    def _evict_oldest_unlocked(self) -> bool:
        for card in self._pinned_cards:
            lock_var = card.get("lock_var")
            if not (lock_var and lock_var.get()):
                self._close_card(card["key"])
                return True
        return False

    def _pin_item(self, kind: str, turret_idx: int = 0, slot: int = 0) -> None:
        item: Any = None
        if kind == "laser":
            lname = self._turret_laser_vars[turret_idx].get() if turret_idx < len(self._turret_laser_vars) else ""
            item  = self._get_laser(lname)
            if not item: return
            key = f"laser_{item.id}"
            title, subtitle = "LASER DETAIL", item.name
        elif kind == "module":
            mname = self._turret_module_vars[turret_idx][slot].get() if turret_idx < len(self._turret_module_vars) else ""
            item  = self._get_module(mname)
            if not item: return
            key = f"module_{item.id}"
            title, subtitle = "MODULE DETAIL", item.name
        elif kind == "gadget":
            gname = self._gadget_var.get() if self._gadget_var else ""
            item  = self._get_gadget(gname)
            if not item: return
            key = f"gadget_{item.id}"
            title, subtitle = "GADGET DETAIL", item.name
        else:
            return

        existing = next((c for c in self._pinned_cards if c["key"] == key), None)
        if existing:
            try: existing["popup"].lift()
            except Exception: log.debug("Failed to lift popup for key=%s", key)
            return

        if len(self._pinned_cards) >= MAX_PINNED:
            if not self._evict_oldest_unlocked():
                return

        popup, dt, lock_var = self._make_card(key, title, subtitle)
        self._fill_item_card(dt, kind, item)
        self._pinned_cards.append({
            "key": key, "popup": popup, "text": dt,
            "type": kind, "data": item, "lock_var": lock_var,
        })

    def _fill_item_card(self, dt: tk.Text, kind: str, item: Any) -> None:
        dt.config(state="normal")
        dt.delete("1.0", "end")

        def w(text, tag=None):
            if tag: dt.insert("end", text, tag)
            else:   dt.insert("end", text)

        def fmt_pct(v: float) -> str:
            return f"{v:+.1f}%" if v != 0 else "0%"

        def pct_tag_good(v: float) -> str:
            return "positive" if v > 0 else ("negative" if v < 0 else "neutral")

        def pct_tag_bad(v: float) -> str:
            return "negative" if v > 0 else ("positive" if v < 0 else "neutral")

        if kind == "laser":
            l: LaserItem = item
            w(f"◈  {l.name}\n", "heading")
            w("─" * 50 + "\n", "divider")
            w("  Company:        ", "label"); w(f"{l.company}\n",        "value")
            w("  Size:           ", "label"); w(f"Size {l.size}\n",      "value")
            w("\n")
            w("  POWER\n", "section")
            w("  Min Power:      ", "label"); w(f"{l.min_power:,.1f} aUEC\n", "value")
            w("  Max Power:      ", "label"); w(f"{l.max_power:,.1f} aUEC\n", "value")
            if l.ext_power:
                w("  Ext Power:      ", "label"); w(f"{l.ext_power:,.1f} aUEC\n", "value")
            w("\n")
            w("  RANGE\n", "section")
            if l.opt_range is not None:
                w("  Opt Range:      ", "label"); w(f"{l.opt_range:.0f} m\n",  "value")
            if l.max_range is not None:
                w("  Max Range:      ", "label"); w(f"{l.max_range:.0f} m\n",  "value")
            w("\n")
            w("  MODIFIERS\n", "section")
            if l.resistance is not None:
                v = l.resistance;    w("  Resistance:     ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if l.instability is not None:
                v = l.instability;   w("  Instability:    ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_bad(v))
            if l.inert is not None:
                v = l.inert;         w("  Inert Mat.:     ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_bad(v))
            if l.charge_window is not None:
                v = l.charge_window; w("  Chrg Window:    ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if l.charge_rate is not None:
                v = l.charge_rate;   w("  Chrg Rate:      ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            w(f"\n  Module Slots:   ", "label"); w(f"{l.module_slots}\n", "value")
            w("\n")
            if l.price > 0:
                w("  Buy Price:      ", "label"); w(f"{l.price:,.0f} aUEC\n", "value")
            else:
                w("  Buy Price:      ", "label"); w("Stock / Free\n", "positive")

        elif kind == "module":
            m: ModuleItem = item
            w(f"◈  {m.name}\n", "heading")
            w("─" * 50 + "\n", "divider")
            type_color = "neutral" if m.item_type.lower() == "active" else "value"
            w("  Type:           ", "label"); w(f"{m.item_type}\n", type_color)
            w("\n")
            w("  MODIFIERS\n", "section")
            if m.power_pct is not None:
                v = m.power_pct - 100.0  # UEX stores as multiplier×100; convert to delta %
                color = pct_tag_good(v) if v >= 0 else "negative"
                w("  Laser Power:    ", "label"); w(f"{fmt_pct(v)}\n", color)
            if m.ext_power_pct is not None:
                v = m.ext_power_pct - 100.0
                color = pct_tag_good(v) if v >= 0 else "negative"
                w("  Ext Power:      ", "label"); w(f"{fmt_pct(v)}\n", color)
            if m.resistance is not None:
                v = m.resistance;    w("  Resistance:     ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if m.instability is not None:
                v = m.instability;   w("  Instability:    ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_bad(v))
            if m.inert is not None:
                v = m.inert;         w("  Inert Mat.:     ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_bad(v))
            if m.charge_window is not None:
                v = m.charge_window; w("  Chrg Window:    ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if m.charge_rate is not None:
                v = m.charge_rate;   w("  Chrg Rate:      ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if m.overcharge is not None:
                v = m.overcharge;    w("  Overcharge:     ", "label"); w(f"{fmt_pct(v)}\n", "negative")
            if m.shatter is not None:
                v = m.shatter;       w("  Shatter:        ", "label"); w(f"{fmt_pct(v)}\n", "negative")
            if m.item_type.lower() == "active" and (m.uses or m.duration):
                w("\n")
                w("  ACTIVE USE\n", "section")
                if m.uses:     w("  Uses:           ", "label"); w(f"{m.uses}\n",       "value")
                if m.duration: w("  Duration:       ", "label"); w(f"{m.duration:.0f} s\n", "value")
            w("\n")
            if m.price > 0:
                w("  Buy Price:      ", "label"); w(f"{m.price:,.0f} aUEC\n", "value")

        elif kind == "gadget":
            g: GadgetItem = item
            w(f"◈  {g.name}\n", "heading")
            w("─" * 50 + "\n", "divider")
            w("  Type:           ", "label"); w("Gadget\n", "value")
            w("\n")
            w("  MODIFIERS\n", "section")
            if g.resistance is not None:
                v = g.resistance;    w("  Resistance:     ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if g.instability is not None:
                v = g.instability;   w("  Instability:    ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_bad(v))
            if g.charge_window is not None:
                v = g.charge_window; w("  Chrg Window:    ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if g.charge_rate is not None:
                v = g.charge_rate;   w("  Chrg Rate:      ", "label"); w(f"{fmt_pct(v)}\n", pct_tag_good(v))
            if g.cluster is not None:
                v = g.cluster;       w("  Cluster:        ", "label"); w(f"{fmt_pct(v)}\n", "neutral")
            w("\n")
            if g.price > 0:
                w("  Buy Price:      ", "label"); w(f"{g.price:,.0f} aUEC\n", "value")

        dt.config(state="disabled")
        dt.yview_moveto(0.0)

    # ── Ship helpers ──────────────────────────────────────────────────────────

    def _update_ship_btn_styles(self) -> None:
        for ship, btn in self._ship_btns.items():
            active = (ship == self.ship_name)
            btn.config(
                bg=C["amber"]   if active else C["btn"],
                fg="#000000"    if active else C["fg2"],
            )

    def _on_ship_changed(self, ship: str) -> None:
        if ship == self.ship_name:
            return
        self.ship_name = ship
        self._update_ship_btn_styles()
        self._rebuild_turret_panels()
        log.info("Ship changed to %s", ship)

    def _reset_loadout(self) -> None:
        cfg   = SHIPS[self.ship_name]
        stock = cfg.get("stock_laser", "")
        n     = cfg["turrets"]
        for i in range(n):
            if i < len(self._turret_laser_vars):
                self._turret_laser_vars[i].set(stock or _NONE_LASER)
            if i < len(self._turret_module_vars):
                for j in range(2):
                    self._turret_module_vars[i][j].set(_NONE_MODULE)
        if self._gadget_var:
            self._gadget_var.set(_NONE_GADGET)
        self._on_loadout_changed()
        log.info("Loadout reset for %s", self.ship_name)

    def _copy_stats(self) -> None:
        if not self.root:
            return
        cfg  = SHIPS[self.ship_name]
        n    = cfg["turrets"]
        lines = [f"Mining Loadout — {self.ship_name}", ""]
        for i in range(n):
            lname = self._turret_laser_vars[i].get() if i < len(self._turret_laser_vars) else _NONE_LASER
            m1    = self._turret_module_vars[i][0].get() if i < len(self._turret_module_vars) else _NONE_MODULE
            m2    = self._turret_module_vars[i][1].get() if i < len(self._turret_module_vars) else _NONE_MODULE
            lines.append(f"{cfg['turret_names'][i]}: {lname}  |  {m1}  |  {m2}")
        gname = self._gadget_var.get() if self._gadget_var else _NONE_GADGET
        lines.append(f"Gadget: {gname}")
        lines.append("")
        label_map = {
            "min_power": "Min Power", "max_power": "Max Power", "ext_power": "Ext Power",
            "opt_range": "Opt Range", "max_range": "Max Range",
            "resistance": "Resistance", "instability": "Instability", "inert": "Inert Mat.",
            "charge_window": "Opt Chrg Wnd", "charge_rate": "Opt Chrg Rate",
            "overcharge": "Overcharge", "cluster": "Cluster", "shatter": "Shatter",
        }
        for key, var in self._stat_vars.items():
            lines.append(f"{label_map.get(key, key)}: {var.get()}")
        if self._price_detail_var:
            lines.append(f"Price: {self._price_detail_var.get()}")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
        except Exception:
            log.debug("Clipboard copy failed: %s", traceback.format_exc())

    # ── Data loading ──────────────────────────────────────────────────────────

    def _start_load(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        if self._status_var:
            self._status_var.set("  Fetching UEX data…")
        threading.Thread(target=self._fetch_worker, daemon=True, name="MiningFetch").start()

    def _fetch_worker(self) -> None:
        try:
            lasers, modules, gadgets = fetch_mining_data()
            if self.root:
                try:
                    self.root.after(0, lambda: self._on_data_loaded(lasers, modules, gadgets))
                except tk.TclError:
                    pass  # Root destroyed during shutdown
        except Exception:
            tb = traceback.format_exc()
            log.error("Fetch failed:\n%s", tb)
            if self.root and self._status_var:
                try:
                    self.root.after(0, lambda: self._status_var.set("  API fetch failed — check internet"))
                except tk.TclError:
                    pass  # Root destroyed during shutdown
        finally:
            self._fetching = False

    def _on_data_loaded(self, lasers: List[LaserItem], modules: List[ModuleItem], gadgets: List[GadgetItem]) -> None:
        self.all_lasers  = lasers
        self.all_modules = modules
        self.all_gadgets = gadgets
        self._data_loaded     = True
        self._last_fetch_ts   = time.time()
        self._populate_dropdowns()
        self._on_loadout_changed()
        ts = time.strftime("%H:%M:%S")
        if self._status_var:
            self._status_var.set(f"  {len(lasers)} lasers · {len(modules)} modules · {len(gadgets)} gadgets")
        if self._upd_var:
            self._upd_var.set(f"Updated {ts}")
        if self._src_var:
            self._src_var.set("[UEX API]")
        log.info("Data loaded: %d lasers, %d modules, %d gadgets", len(lasers), len(modules), len(gadgets))
        # Schedule the auto-refresh loop now that initial data is loaded
        self.root.after(3_600_000, self._auto_refresh_loop)

    def _do_refresh(self) -> None:
        if self._fetching:
            return
        self._fetching = True
        if self._status_var:
            self._status_var.set("  Refreshing data…")
        threading.Thread(target=self._fetch_worker, daemon=True, name="MiningRefresh").start()

    def _auto_refresh_loop(self) -> None:
        if self._last_fetch_ts and (time.time() - self._last_fetch_ts) >= self.refresh_interval:
            self._do_refresh()
        if self.root:
            self.root.after(3_600_000, self._auto_refresh_loop)  # check every hour

    # ── IPC polling ───────────────────────────────────────────────────────────

    def _poll_queue(self) -> None:
        try:
            while True:
                cmd = self.cmd_queue.get_nowait()
                log.debug("IPC: %s", cmd)
                self._dispatch(cmd)
        except queue.Empty:
            pass
        if self.root:
            self.root.after(150, self._poll_queue)

    def _dispatch(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.root.deiconify(); self._force_show()
        elif t == "hide":
            self.root.withdraw()
        elif t == "quit":
            self.root.quit()
        elif t == "refresh":
            self._do_refresh()
        elif t == "reset":
            self._reset_loadout()
        elif t == "set_ship":
            ship = cmd.get("ship", "")
            if ship in SHIPS:
                self._on_ship_changed(ship)
        elif t == "set_laser":
            ti    = int(cmd.get("turret", 0))
            lname = cmd.get("name", cmd.get("laser_name", ""))
            if lname and ti < len(self._turret_laser_vars):
                # fuzzy match against loaded laser names
                match = next((l.name for l in self.all_lasers if lname.lower() in l.name.lower()), None)
                if match:
                    self._turret_laser_vars[ti].set(match)
                    self._on_loadout_changed()
        elif t == "set_module":
            ti    = int(cmd.get("turret", 0))
            slot  = int(cmd.get("slot", 0))
            mname = cmd.get("name", cmd.get("module_name", ""))
            if mname and ti < len(self._turret_module_vars) and slot < 2:
                match = next((m.name for m in self.all_modules if mname.lower() in m.name.lower()), None)
                if match:
                    self._turret_module_vars[ti][slot].set(match)
                    self._on_loadout_changed()
        elif t == "set_gadget":
            gname = cmd.get("name", cmd.get("gadget_name", ""))
            if gname and self._gadget_var:
                match = next((g.name for g in self.all_gadgets if gname.lower() in g.name.lower()), None)
                if match:
                    self._gadget_var.set(match)
                    self._on_loadout_changed()

    # ── Drag ──────────────────────────────────────────────────────────────────

    def _drag_start(self, e) -> None:
        self._drag_x = e.x; self._drag_y = e.y

    def _drag_motion(self, e) -> None:
        x = self.root.winfo_x() + e.x - self._drag_x
        y = self.root.winfo_y() + e.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    # ── Config persistence ────────────────────────────────────────────────────

    def _load_config(self) -> None:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
            saved_ship = cfg.get("ship", "MOLE")
            if saved_ship in SHIPS:
                self.ship_name = saved_ship
                self._update_ship_btn_styles()
            saved_hk = cfg.get("hotkey", "")
            if saved_hk:
                self._hotkey = saved_hk
                if self._hotkey_entry_var:
                    self._hotkey_entry_var.set(saved_hk)
            loadout = cfg.get("loadout", {})
            ship_cfg = SHIPS.get(self.ship_name, {})
            n = ship_cfg.get("turrets", 1)
            while len(self._turret_laser_vars)  < n:
                self._turret_laser_vars.append(tk.StringVar(value=_NONE_LASER))
            while len(self._turret_module_vars) < n:
                self._turret_module_vars.append([tk.StringVar(value=_NONE_MODULE), tk.StringVar(value=_NONE_MODULE)])
            for i in range(n):
                key = f"turret_{i}"
                if key in loadout:
                    td = loadout[key]
                    self._turret_laser_vars[i].set(td.get("laser", _NONE_LASER))
                    mods = td.get("modules", [_NONE_MODULE, _NONE_MODULE])
                    for j in range(2):
                        self._turret_module_vars[i][j].set(mods[j] if j < len(mods) else _NONE_MODULE)
            if self._gadget_var:
                self._gadget_var.set(cfg.get("gadget", _NONE_GADGET))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass

    def _save_config(self) -> None:
        try:
            cfg_data = SHIPS.get(self.ship_name, {})
            n = cfg_data.get("turrets", 1)
            loadout = {}
            for i in range(n):
                lname = self._turret_laser_vars[i].get() if i < len(self._turret_laser_vars) else _NONE_LASER
                mods  = [
                    self._turret_module_vars[i][j].get() if i < len(self._turret_module_vars) and j < len(self._turret_module_vars[i]) else _NONE_MODULE
                    for j in range(2)
                ]
                loadout[f"turret_{i}"] = {"laser": lname, "modules": mods}
            gname = self._gadget_var.get() if self._gadget_var else _NONE_GADGET
            cfg = {"ship": self.ship_name, "hotkey": self._hotkey, "loadout": loadout, "gadget": gname}
            with open(_CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
        except Exception as e:
            log.warning("Config save failed: %s", e)

    # ── Hotkey ────────────────────────────────────────────────────────────────

    def _apply_hotkey(self) -> None:
        hk = (self._hotkey_entry_var.get() if self._hotkey_entry_var else "").strip()
        if not hk:
            return
        mods, vk = _parse_hotkey(hk)
        if not vk:
            if self._hotkey_status_var:
                self._hotkey_status_var.set("Invalid key")
            return
        self._hotkey = hk
        self._save_config()
        self._start_hotkey_listener()
        if self._hotkey_status_var:
            self._hotkey_status_var.set("✓ Applied")

    def _start_hotkey_listener(self) -> None:
        if self._hotkey_stop:
            self._hotkey_stop.set()
        stop = threading.Event()
        self._hotkey_stop = stop
        threading.Thread(
            target=self._hotkey_thread, args=(self._hotkey, stop),
            daemon=True, name="HotkeyListener",
        ).start()

    def _hotkey_thread(self, hk: str, stop: threading.Event) -> None:
        mods, vk = _parse_hotkey(hk)
        if not vk or not mods:
            return
        hk_id = deterministic_hotkey_id(hk)
        registered = False
        try:
            if _user32.RegisterHotKey(None, hk_id, mods, vk):
                registered = True
                msg = ctypes.wintypes.MSG()
                while not stop.is_set():
                    r = _user32.PeekMessageW(ctypes.byref(msg), None, _WM_HOTKEY, _WM_HOTKEY, _PM_REMOVE)
                    if r and msg.message == _WM_HOTKEY and msg.wParam == hk_id:
                        if self.root:
                            self.root.after(0, self._toggle_visibility)
                    time.sleep(0.05)
        finally:
            if registered:
                try: _user32.UnregisterHotKey(None, hk_id)
                except Exception: log.debug("UnregisterHotKey failed")

    def _toggle_visibility(self) -> None:
        if not self.root:
            return
        if self.root.state() == "withdrawn":
            self.root.deiconify(); self._force_show()
        else:
            self.root.withdraw()


# ── IPC file reader ───────────────────────────────────────────────────────────

def _ipc_reader(cmd_file: str, cmd_queue: queue.Queue, stop: threading.Event) -> None:
    offset = 0
    while not stop.is_set():
        try:
            commands, offset = ipc_read_incremental(cmd_file, offset)
            for cmd in commands:
                cmd_queue.put(cmd)
        except Exception:
            log.debug("IPC reader error: %s", traceback.format_exc())
        time.sleep(0.15)


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: mining_loadout_app.py <x> <y> <w> <h> <opacity> <cmd_file>")
        sys.exit(1)

    set_dpi_awareness()

    parsed = parse_cli_args(sys.argv[1:], {"w": 1200, "h": 720})
    win_x    = parsed["x"]
    win_y    = parsed["y"]
    win_w    = parsed["w"]
    win_h    = parsed["h"]
    opacity  = parsed["opacity"]
    cmd_file = parsed["cmd_file"]
    refresh  = 86400.0

    cmd_queue = queue.Queue()
    stop_evt  = threading.Event()

    if cmd_file:
        threading.Thread(
            target=_ipc_reader, args=(cmd_file, cmd_queue, stop_evt),
            daemon=True, name="IPCReader",
        ).start()

    window = MiningLoadoutWindow(
        cmd_queue        = cmd_queue,
        win_x            = win_x,
        win_y            = win_y,
        win_w            = win_w,
        win_h            = win_h,
        refresh_interval = refresh,
        opacity          = opacity,
    )
    window.run()
    stop_evt.set()


if __name__ == "__main__":
    main()
