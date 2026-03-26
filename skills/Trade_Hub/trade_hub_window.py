# NOTE: This file may be superseded by trade_hub_app.py — verify usage before editing
"""
Trade Hub — borderless interactive overlay window.
Styled to match SC Trade Tools visual design.
If the window crashes on startup, error details are written to %TEMP%\\trade_hub_error.txt.
"""
import ctypes
import os
import queue
import sys
import time
import tkinter as tk
from tkinter import ttk
from typing import Dict, List, Optional, Tuple

# Allow imports from the shared package two levels up
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))

from shared.ships import SHIP_PRESETS, scu_for_ship, QUICK_SHIPS  # noqa: E402
from shared.theme import COLORS  # noqa: E402

from uex_client import RouteData, UEXClient
from route_engine import (
    FilterState,
    apply_filters,
    get_unique_commodities,
    profit_tier,
    sort_routes,
)

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

# C (colour palette) imported from shared.theme
# SHIP_PRESETS, scu_for_ship, QUICK_SHIPS imported from shared.ships

# ── Columns (matching second screenshot) ──────────────────────────────────────
COLUMNS: List[Tuple[str, str, int, str]] = [
    ("commodity",     "Commodity",     132, "w"),
    ("buy_terminal",  "Buy Terminal",  172, "w"),
    ("buy_system",    "System",         72, "w"),
    ("sell_terminal", "Sell Terminal", 172, "w"),
    ("sell_system",   "System",         72, "w"),
    ("available_scu", "Avail SCU",      80, "e"),
    ("scu_demand",    "Demand SCU",     82, "e"),
    ("margin_scu",    "Margin/SCU",    120, "e"),
    ("est_profit",    "Est. Profit",   140, "e"),
]

## QUICK_SHIPS imported from shared.ships


class TradeHubWindow:
    """Borderless always-on-top overlay window for the Trade Hub skill."""

    def __init__(
        self,
        command_queue: queue.Queue,
        uex_client: UEXClient,
        win_x: int = 80,
        win_y: int = 80,
        win_w: int = 1400,
        win_h: int = 900,
        refresh_interval: float = 300.0,
        max_routes: int = 500,
        opacity: float = 0.95,
    ):
        self.command_queue    = command_queue
        self.uex_client       = uex_client
        self.win_x            = win_x
        self.win_y            = win_y
        self.win_w            = win_w
        self.win_h            = win_h
        self.refresh_interval = refresh_interval
        self.max_routes       = max_routes
        self.opacity          = max(0.3, min(1.0, opacity))

        # State
        self.all_routes:       List[RouteData] = []
        self.filtered_routes:  List[RouteData] = []
        self.sort_col          = "est_profit"
        self.sort_reverse      = True
        self.ship_name         = ""
        self.ship_scu          = 0
        self.filters           = FilterState()
        self.last_refresh_time: Optional[float] = None
        self._debounce_id      = None
        self._data_source      = "—"

        # Drag / resize state
        self._drag_x = self._drag_y = 0
        self._resizing = False
        self._resize_sx = self._resize_sy = 0
        self._resize_sw = self._resize_sh = 0

        # Widget refs (populated in _build_ui)
        self.root:          Optional[tk.Tk]        = None
        self.tree:          Optional[ttk.Treeview] = None
        self.status_var:    Optional[tk.StringVar] = None
        self.count_var:     Optional[tk.StringVar] = None
        self.upd_label:     Optional[tk.Label]     = None
        self.src_label:     Optional[tk.Label]     = None
        self.search_var:    Optional[tk.StringVar] = None
        self.ship_var:      Optional[tk.StringVar] = None
        self.system_var:    Optional[tk.StringVar] = None
        self.location_var:  Optional[tk.StringVar] = None
        self.commodity_var: Optional[tk.StringVar] = None
        self.comm_combo:    Optional[ttk.Combobox] = None
        self.minprofit_var: Optional[tk.StringVar] = None

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self) -> None:
        """Called from background thread. Errors are written to %TEMP%\\trade_hub_error.txt."""
        try:
            self._run_inner()
        except Exception:
            import traceback
            try:
                p = os.path.join(os.environ.get("TEMP", os.getcwd()), "trade_hub_error.txt")
                with open(p, "w") as f:
                    f.write(traceback.format_exc())
            except Exception:
                pass

    def _run_inner(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()                            # hide while building UI

        self.root.overrideredirect(True)                # borderless
        self.root.geometry(f"{self.win_w}x{self.win_h}+{self.win_x}+{self.win_y}")
        self.root.configure(bg=COLORS["bg"])
        self.root.wm_attributes("-alpha", self.opacity)
        self.root.wm_attributes("-topmost", True)

        self._setup_styles()
        self._build_ui()

        self.root.update_idletasks()                    # ensure widgets are laid out
        self.root.deiconify()                           # show the window
        self.root.lift()
        self.root.update()
        self._force_show()                              # Win32 bring-to-front

        self._schedule_queue_poll()
        self._schedule_auto_refresh()
        self._keepalive()

        self.root.after(600, self._initial_load)
        self.root.mainloop()

    # ── Win32 topmost ─────────────────────────────────────────────────────────

    def _get_hwnd(self) -> Optional[int]:
        """Get the Windows HWND for this window (handles various Tk return formats)."""
        try:
            frame = self.root.wm_frame()
            # wm_frame() can return decimal ("12345678") or hex ("0xabcd1234")
            try:
                return int(frame)
            except ValueError:
                return int(frame, 16)
        except Exception:
            pass
        try:
            return self.root.winfo_id()
        except Exception:
            return None

    def _apply_topmost(self) -> None:
        hwnd = self._get_hwnd()
        if hwnd and _user32:
            try:
                _user32.SetWindowPos(
                    hwnd, _HWND_TOPMOST, 0, 0, 0, 0,
                    _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE,
                )
            except Exception:
                pass

    def _force_show(self) -> None:
        """Bring window to front using Win32 AttachThreadInput trick.
        Works even when WingmanAI (also HWND_TOPMOST) has focus."""
        hwnd = self._get_hwnd()
        if not hwnd or not _user32:
            return
        try:
            _user32.ShowWindow(hwnd, _SW_RESTORE)
            _user32.BringWindowToTop(hwnd)
            fg_hwnd = _user32.GetForegroundWindow()
            fg_tid  = _user32.GetWindowThreadProcessId(fg_hwnd, None)
            our_tid = _kernel32.GetCurrentThreadId() if _kernel32 else 0
            if fg_tid and fg_tid != our_tid:
                _user32.AttachThreadInput(fg_tid, our_tid, True)
                _user32.SetForegroundWindow(hwnd)
                _user32.AttachThreadInput(fg_tid, our_tid, False)
            else:
                _user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self._apply_topmost()

    def _keepalive(self) -> None:
        """Re-assert topmost every 2 s so the game can't push us behind."""
        if self.root:
            self._apply_topmost()
            self.root.after(2000, self._keepalive)

    # ── Styles ────────────────────────────────────────────────────────────────

    def _setup_styles(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")

        s.configure("TFrame",  background=COLORS["bg"])
        s.configure("TLabel",  background=COLORS["bg"], foreground=COLORS["fg"],
                    font=("Consolas", 9))

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

        s.configure("T.Vertical.TScrollbar",
                    background=COLORS["bg2"], troughcolor=COLORS["bg"],
                    arrowcolor=COLORS["blue2"], borderwidth=0, relief="flat")
        s.configure("T.Horizontal.TScrollbar",
                    background=COLORS["bg2"], troughcolor=COLORS["bg"],
                    arrowcolor=COLORS["blue2"], borderwidth=0, relief="flat")

        s.configure("T.TEntry",
                    fieldbackground=COLORS["ibg"], foreground=COLORS["ifg"],
                    insertcolor=COLORS["blue"], borderwidth=0, relief="flat",
                    font=("Consolas", 9))
        s.configure("T.TCombobox",
                    fieldbackground=COLORS["ibg"], foreground=COLORS["ifg"],
                    background=COLORS["btn"], arrowcolor=COLORS["blue"],
                    selectbackground=COLORS["sel"], borderwidth=0,
                    font=("Consolas", 9))
        s.map("T.TCombobox",
              fieldbackground=[("readonly", COLORS["ibg"])],
              selectbackground=[("readonly", COLORS["sel"])])

        s.configure("T.TButton",
                    background=COLORS["btn"], foreground=COLORS["blue"],
                    font=("Consolas", 9), borderwidth=0, relief="flat",
                    padding=(6, 2))
        s.map("T.TButton",
              background=[("active", COLORS["blue2"])])

    # ── UI builder ────────────────────────────────────────────────────────────

    def _build_ui(self) -> None:

        # ── Title bar ──────────────────────────────────────────────────────
        bar = tk.Frame(self.root, bg=COLORS["bar"], height=42)
        bar.pack(fill="x")
        bar.pack_propagate(False)

        def _bind_drag(w: tk.Widget) -> None:
            w.bind("<Button-1>",  self._drag_start)
            w.bind("<B1-Motion>", self._drag_motion)

        _bind_drag(bar)

        lbl = tk.Label(bar, text="◈  TRADE HUB", bg=COLORS["bar"], fg=COLORS["blue"],
                       font=("Consolas", 14, "bold"), cursor="fleur")
        lbl.pack(side="left", padx=12, pady=9)
        _bind_drag(lbl)

        sub = tk.Label(bar, text="ECONOMIC INTELLIGENCE TERMINAL",
                       bg=COLORS["bar"], fg=COLORS["fg2"], font=("Consolas", 8), cursor="fleur")
        sub.pack(side="left", pady=13)
        _bind_drag(sub)

        close = tk.Label(bar, text=" ✕ ", bg=COLORS["bar"], fg=COLORS["blue"],
                         font=("Consolas", 11, "bold"), cursor="hand2")
        close.pack(side="right", padx=(4, 10), pady=8)
        close.bind("<Button-1>", lambda _e: self.root.withdraw())
        close.bind("<Enter>",    lambda _e: close.config(fg="#ff4040"))
        close.bind("<Leave>",    lambda _e: close.config(fg=COLORS["blue"]))

        rbtn = tk.Label(bar, text=" ⟳ ", bg=COLORS["bar"], fg=COLORS["blue"],
                        font=("Consolas", 14), cursor="hand2")
        rbtn.pack(side="right", padx=4, pady=8)
        rbtn.bind("<Button-1>", lambda _e: self._on_refresh())
        rbtn.bind("<Enter>",    lambda _e: rbtn.config(fg=COLORS["fg3"]))
        rbtn.bind("<Leave>",    lambda _e: rbtn.config(fg=COLORS["blue"]))

        self.src_label = tk.Label(bar, text="", bg=COLORS["bar"], fg=COLORS["fg2"],
                                  font=("Consolas", 8))
        self.src_label.pack(side="right", padx=6, pady=13)

        self.upd_label = tk.Label(bar, text="", bg=COLORS["bar"], fg=COLORS["fg2"],
                                  font=("Consolas", 9))
        self.upd_label.pack(side="right", padx=6, pady=13)

        tk.Frame(self.root, bg=COLORS["blue2"], height=1).pack(fill="x")

        # ── Filter bar ─────────────────────────────────────────────────────
        flt = tk.Frame(self.root, bg=COLORS["flt"])
        flt.pack(fill="x")

        def lbl_f(text: str) -> None:
            tk.Label(flt, text=text, bg=COLORS["flt"], fg=COLORS["fg2"],
                     font=("Consolas", 8)).pack(side="left", padx=(8, 2), pady=4)

        lbl_f("SHIP:")
        self.ship_var = tk.StringVar(value=QUICK_SHIPS[0][1])
        ship_cb = ttk.Combobox(flt, textvariable=self.ship_var,
                               values=[d for _, d in QUICK_SHIPS],
                               state="readonly", width=22, style="T.TCombobox")
        ship_cb.current(0)
        ship_cb.pack(side="left", padx=(0, 8))
        ship_cb.bind("<<ComboboxSelected>>", self._on_ship_changed)

        lbl_f("SYSTEM:")
        self.system_var = tk.StringVar()
        sys_cb = ttk.Combobox(flt, textvariable=self.system_var,
                              values=["", "Stanton", "Pyro"],
                              width=9, style="T.TCombobox")
        sys_cb.pack(side="left", padx=(0, 8))
        sys_cb.bind("<<ComboboxSelected>>", lambda _e: self._refresh())
        sys_cb.bind("<Return>", lambda _e: self._refresh())

        lbl_f("LOCATION:")
        self.location_var = tk.StringVar()
        loc_e = ttk.Entry(flt, textvariable=self.location_var,
                          width=13, style="T.TEntry")
        loc_e.pack(side="left", padx=(0, 8))
        loc_e.bind("<Return>",     lambda _e: self._refresh())
        loc_e.bind("<KeyRelease>", lambda _e: self._refresh_debounced())

        lbl_f("COMMODITY:")
        self.commodity_var = tk.StringVar()
        self.comm_combo = ttk.Combobox(flt, textvariable=self.commodity_var,
                                       values=[], width=16, style="T.TCombobox")
        self.comm_combo.pack(side="left", padx=(0, 8))
        self.comm_combo.bind("<<ComboboxSelected>>", lambda _e: self._refresh())
        self.comm_combo.bind("<Return>",     lambda _e: self._refresh())
        self.comm_combo.bind("<KeyRelease>", lambda _e: self._refresh_debounced())

        lbl_f("MIN/SCU:")
        self.minprofit_var = tk.StringVar(value="0")
        min_e = ttk.Entry(flt, textvariable=self.minprofit_var,
                          width=7, style="T.TEntry")
        min_e.pack(side="left", padx=(0, 8))
        min_e.bind("<Return>", lambda _e: self._refresh())

        ttk.Button(flt, text="CLEAR", style="T.TButton",
                   command=self._clear_filters).pack(side="left", padx=4)

        # Search — right-aligned
        tk.Label(flt, text="SEARCH:", bg=COLORS["flt"], fg=COLORS["fg2"],
                 font=("Consolas", 8)).pack(side="right", padx=(0, 2))
        self.search_var = tk.StringVar()
        se = ttk.Entry(flt, textvariable=self.search_var,
                       width=18, style="T.TEntry")
        se.pack(side="right", padx=(0, 8))
        se.bind("<KeyRelease>", lambda _e: self._refresh_debounced())
        tk.Label(flt, text="⌕", bg=COLORS["flt"], fg=COLORS["blue"],
                 font=("Consolas", 13)).pack(side="right", padx=(8, 0))

        tk.Frame(self.root, bg=COLORS["sep"], height=1).pack(fill="x")

        # ── Table ──────────────────────────────────────────────────────────
        tbl = tk.Frame(self.root, bg=COLORS["bg"])
        tbl.pack(fill="both", expand=True)

        vsb = ttk.Scrollbar(tbl, orient="vertical",   style="T.Vertical.TScrollbar")
        hsb = ttk.Scrollbar(tbl, orient="horizontal", style="T.Horizontal.TScrollbar")

        self.tree = ttk.Treeview(
            tbl,
            columns=[c[0] for c in COLUMNS],
            show="headings",
            style="T.Treeview",
            yscrollcommand=vsb.set,
            xscrollcommand=hsb.set,
            selectmode="browse",
        )
        vsb.config(command=self.tree.yview)
        hsb.config(command=self.tree.xview)

        for key, hdr, width, anchor in COLUMNS:
            self.tree.heading(key, text=hdr, anchor="center",
                              command=lambda k=key: self._on_hdr_click(k))
            self.tree.column(key, width=width, minwidth=40,
                             anchor=anchor, stretch=False)

        self.tree.tag_configure("high", foreground=COLORS["green"])
        self.tree.tag_configure("med",  foreground=COLORS["yellow"])
        self.tree.tag_configure("low",  foreground=COLORS["red"])
        self.tree.tag_configure("odd",  background=COLORS["odd"])
        self.tree.tag_configure("even", background=COLORS["even"])

        vsb.pack(side="right",  fill="y")
        hsb.pack(side="bottom", fill="x")
        self.tree.pack(fill="both", expand=True)

        # ── Status bar ─────────────────────────────────────────────────────
        tk.Frame(self.root, bg=COLORS["blue2"], height=1).pack(fill="x")

        sb = tk.Frame(self.root, bg=COLORS["flt"], height=22)
        sb.pack(fill="x")
        sb.pack_propagate(False)

        self.status_var = tk.StringVar(value="  Initializing…")
        tk.Label(sb, textvariable=self.status_var,
                 bg=COLORS["flt"], fg=COLORS["fg2"],
                 font=("Consolas", 9), anchor="w").pack(side="left", padx=10)

        self.count_var = tk.StringVar(value="")
        tk.Label(sb, textvariable=self.count_var,
                 bg=COLORS["flt"], fg=COLORS["blue"],
                 font=("Consolas", 9, "bold"), anchor="e").pack(side="right", padx=10)

        # Resize grip
        grip = tk.Label(sb, text="⤡", bg=COLORS["flt"], fg=COLORS["fg2"],
                        font=("Consolas", 10), cursor="size_nw_se")
        grip.pack(side="right", padx=(0, 2))
        grip.bind("<Button-1>",        self._resize_start)
        grip.bind("<B1-Motion>",       self._resize_motion)
        grip.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_resizing", False))

    # ── Drag & resize ─────────────────────────────────────────────────────────

    def _drag_start(self, e: tk.Event) -> None:
        self._drag_x = e.x_root - self.root.winfo_x()
        self._drag_y = e.y_root - self.root.winfo_y()

    def _drag_motion(self, e: tk.Event) -> None:
        self.root.geometry(f"+{e.x_root - self._drag_x}+{e.y_root - self._drag_y}")

    def _resize_start(self, e: tk.Event) -> None:
        self._resizing = True
        self._resize_sx, self._resize_sy = e.x_root, e.y_root
        self._resize_sw = self.root.winfo_width()
        self._resize_sh = self.root.winfo_height()

    def _resize_motion(self, e: tk.Event) -> None:
        if not self._resizing:
            return
        w = max(800, self._resize_sw + e.x_root - self._resize_sx)
        h = max(400, self._resize_sh + e.y_root - self._resize_sy)
        self.root.geometry(f"{w}x{h}")

    # ── Data loading ──────────────────────────────────────────────────────────

    def _initial_load(self) -> None:
        self._set_status("Loading trade data…")
        self.uex_client.refresh_async(callback=self._on_routes_received)

    def _on_routes_received(self, routes: List[RouteData], source: str = "API") -> None:
        def _apply():
            self.all_routes        = routes
            self.last_refresh_time = time.time()
            self._data_source      = source
            self._refresh_display()
        if self.root:
            self.root.after(0, _apply)

    def _refresh_display(self) -> None:
        self.filters = self._read_filters()
        result = apply_filters(self.all_routes, self.filters)
        result = sort_routes(result, self.sort_col, self.sort_reverse, self.ship_scu)
        self.filtered_routes = result[: self.max_routes]

        if self.comm_combo is not None:
            curr = self.commodity_var.get() if self.commodity_var else ""
            self.comm_combo["values"] = [""] + get_unique_commodities(self.all_routes)
            if self.commodity_var:
                self.commodity_var.set(curr)

        self._populate_table()
        self._update_status()
        self._update_header()

    def _populate_table(self) -> None:
        try:
            y = self.tree.yview()[0]
        except Exception:
            y = 0.0

        self.tree.delete(*self.tree.get_children())

        for i, r in enumerate(self.filtered_routes):
            profit = r.estimated_profit(self.ship_scu)
            vals = (
                r.commodity,
                r.buy_terminal or r.buy_location,
                r.buy_system,
                r.sell_terminal or r.sell_location,
                r.sell_system,
                f"{r.scu_available:,} SCU",
                f"{r.scu_demand:,} SCU",
                f"{r.margin:,.0f} aUEC/SCU",
                f"{profit:,.0f} aUEC",
            )
            tier    = profit_tier(r.margin)
            row_tag = "even" if i % 2 == 0 else "odd"
            self.tree.insert("", "end", values=vals, tags=(tier, row_tag))

        if y > 0.001:
            try:
                self.tree.yview_moveto(y)
            except Exception:
                pass

    def _update_status(self) -> None:
        total = len(self.all_routes)
        shown = len(self.filtered_routes)
        ship  = f" │ {self.ship_name} ({self.ship_scu:,} SCU)" if self.ship_scu else ""
        flt   = " │ Filters active" if self._has_filters() else ""
        if self.status_var:
            self.status_var.set(f"  {shown:,} / {total:,} routes{ship}{flt}")
        if self.count_var:
            self.count_var.set(f"{shown:,} routes  ")

    def _update_header(self) -> None:
        if self.upd_label and self.last_refresh_time:
            t = time.strftime("%H:%M:%S", time.localtime(self.last_refresh_time))
            self.upd_label.config(text=f"Updated {t}")
        if self.src_label:
            self.src_label.config(text=f"[{self._data_source}]")

    def _set_status(self, msg: str) -> None:
        if self.status_var and self.root:
            self.status_var.set(f"  {msg}")

    # ── Filters ───────────────────────────────────────────────────────────────

    def _read_filters(self) -> FilterState:
        f = FilterState()
        f.system    = self.system_var.get().strip()    if self.system_var    else ""
        f.location  = self.location_var.get().strip()  if self.location_var  else ""
        f.commodity = self.commodity_var.get().strip()  if self.commodity_var else ""
        f.search    = self.search_var.get().strip()    if self.search_var    else ""
        try:
            f.min_margin_scu = float(
                self.minprofit_var.get()) if self.minprofit_var else 0.0
        except ValueError:
            f.min_margin_scu = 0.0
        return f

    def _has_filters(self) -> bool:
        f = self._read_filters()
        return bool(f.system or f.location or f.commodity or f.search or f.min_margin_scu > 0)

    def _refresh(self) -> None:
        if self.root:
            self.root.after(0, self._refresh_display)

    def _refresh_debounced(self, delay: int = 320) -> None:
        if self._debounce_id:
            try:
                self.root.after_cancel(self._debounce_id)
            except Exception:
                pass
        self._debounce_id = self.root.after(delay, self._refresh_display)

    def _clear_filters(self) -> None:
        for var, val in [
            (self.system_var,    ""),
            (self.location_var,  ""),
            (self.commodity_var, ""),
            (self.search_var,    ""),
            (self.minprofit_var, "0"),
        ]:
            if var:
                var.set(val)
        self._refresh()

    # ── Ship ──────────────────────────────────────────────────────────────────

    def _on_ship_changed(self, _event=None) -> None:
        selected = self.ship_var.get() if self.ship_var else ""
        for name, display in QUICK_SHIPS:
            if display == selected:
                self._set_ship(name, 0)
                return
        self._set_ship("", 0)

    def _set_ship(self, ship_name: str, ship_scu: int = 0) -> None:
        self.ship_name = ship_name
        self.ship_scu  = ship_scu if ship_scu > 0 else scu_for_ship(ship_name)
        if self.ship_var and self.root:
            for name, display in QUICK_SHIPS:
                if name.lower() == ship_name.lower():
                    self.ship_var.set(display)
                    break
            else:
                if not ship_name:
                    self.ship_var.set(QUICK_SHIPS[0][1])
        self._refresh()

    # ── Sorting ───────────────────────────────────────────────────────────────

    def _on_hdr_click(self, col: str) -> None:
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col     = col
            self.sort_reverse = col in (
                "available_scu", "scu_demand", "margin_scu", "est_profit"
            )
        self._update_sort_arrows()
        self._refresh()

    def _update_sort_arrows(self) -> None:
        for key, hdr, _, _ in COLUMNS:
            suf = (" ▼" if self.sort_reverse else " ▲") if key == self.sort_col else ""
            self.tree.heading(key, text=hdr + suf)

    # ── Scheduling ────────────────────────────────────────────────────────────

    def _schedule_queue_poll(self) -> None:
        self._process_queue()

    def _process_queue(self) -> None:
        try:
            while True:
                cmd = self.command_queue.get_nowait()
                self._handle_cmd(cmd)
        except queue.Empty:
            pass
        except Exception:
            pass
        if self.root:
            self.root.after(100, self._process_queue)

    def _schedule_auto_refresh(self) -> None:
        self.root.after(int(self.refresh_interval * 1000), self._auto_refresh)

    def _auto_refresh(self) -> None:
        self.uex_client.refresh_async(callback=self._on_routes_received)
        self._schedule_auto_refresh()

    # ── Command handler ───────────────────────────────────────────────────────

    def _handle_cmd(self, cmd: dict) -> None:
        t = cmd.get("type", "")
        if t == "show":
            self.root.deiconify()
            self.root.wm_attributes("-topmost", True)
            self.root.lift()
            self.root.after(50, self._force_show)       # slight delay so lift() completes first
        elif t == "hide":
            self.root.withdraw()
        elif t == "quit":
            self.root.quit()
        elif t == "set_ship":
            self._set_ship(cmd.get("ship_name", ""), cmd.get("ship_scu", 0))
        elif t == "filter":
            if cmd.get("system")        and self.system_var:    self.system_var.set(cmd["system"])
            if cmd.get("location")      and self.location_var:  self.location_var.set(cmd["location"])
            if cmd.get("commodity")     and self.commodity_var: self.commodity_var.set(cmd["commodity"])
            if cmd.get("min_profit_scu") and self.minprofit_var:
                self.minprofit_var.set(str(cmd["min_profit_scu"]))
            self._refresh()
        elif t == "sort":
            col = cmd.get("column", "est_profit")
            valid_cols = {c[0] for c in COLUMNS}
            if col in valid_cols:
                self.sort_col     = col
                self.sort_reverse = col in (
                    "available_scu", "scu_demand", "margin_scu", "est_profit"
                )
                self._update_sort_arrows()
                self._refresh()
        elif t == "clear_filters":
            self._clear_filters()
        elif t == "refresh":
            self._on_refresh()
        elif t == "opacity":
            val = max(0.3, min(1.0, float(cmd.get("value", 0.95))))
            self.root.wm_attributes("-alpha", val)

    # ── Button handlers ───────────────────────────────────────────────────────

    def _on_refresh(self) -> None:
        self._set_status("Refreshing…")
        self.uex_client.refresh_async(callback=self._on_routes_received)
