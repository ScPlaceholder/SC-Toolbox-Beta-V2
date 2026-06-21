"""Trade-activity overlay model.

Turns the Trade Hub's reported routes (built from UEX's crowd-sourced prices)
into map-drawable aggregates that the galaxy / system views render as toggleable
layers:

* **flows**  — system↔system links (galaxy) and terminal↔terminal links (system),
               weighted by how many reported routes run between them.
* **top**    — the most-profitable reported routes, drawn as distinct lines:
               cross-system on the galaxy, and *per-system* in the system view
               (intra-system endpoint→endpoint, or origin→jump-gateway for the
               cross-system runs that touch this system).
* **activity** — per-system / per-body report *recency* (how fresh the data is),
               from each terminal's last report time.

Computed once per route load; cheap (pure in-memory aggregation over routes).
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable, Dict, List, Optional, Tuple

from .data import norm_loc

_RECENCY_WINDOW = 30 * 86400.0   # data older than this is treated as fully stale


def terminal_recency() -> Dict[int, float]:
    """``id_terminal`` -> report freshness 0..1 (1 = just reported) from UEX's
    last-modified timestamps, fresh over the last ~30 days. Reads the cached
    ``commodities_prices_all`` dump, so it's cheap to call repeatedly."""
    try:
        from . import uex
        rows = uex.prices_all()
    except Exception:
        return {}
    now = time.time()
    rec: Dict[int, float] = {}
    for r in rows:
        tid = r.get("id_terminal")
        dm = r.get("date_modified") or r.get("date_added")
        if not tid or not dm:
            continue
        fresh = max(0.0, min(1.0, 1.0 - (now - float(dm)) / _RECENCY_WINDOW))
        if fresh > rec.get(tid, 0.0):
            rec[tid] = fresh
    return rec


TOP_CROSS = 14        # top cross-system routes to highlight on the galaxy
TOP_PER_SYS = 10      # top routes per system to highlight in a system view
MAX_FLOWS_SYS = 100   # cap terminal links per system (busiest first) — fuller but readable


class TradeOverlay:
    def __init__(self, routes, sys_code: Callable[[str], str],
                 recency: Optional[Dict[int, float]] = None,
                 career: Optional[dict] = None) -> None:
        self.routes = list(routes or [])
        self._sys_code = sys_code
        self._rec_term = recency or {}                      # id_terminal -> freshness 0..1
        # 'My Runs' overlay (from the career ledger): heat rings + paths flown.
        career = career or {}
        self.career_system = career.get("system_net", {})           # code -> net aUEC
        self.career_body = career.get("body_net", {})               # (code, norm body) -> net aUEC
        self.career_galaxy_lines = career.get("galaxy_lines", [])   # [(codeA, codeB, net, count)]
        self.career_system_lines = career.get("system_lines", {})   # code -> [(nameA, nameB, net, count)]
        ring_nets = list(self.career_system.values()) + list(self.career_body.values())
        self.career_vmax = max((abs(v) for v in ring_nets), default=1.0)
        _lines = list(self.career_galaxy_lines) + \
            [t for lst in self.career_system_lines.values() for t in lst]
        self.career_line_vmax = max((abs(n) for (_a, _b, n, _c) in _lines), default=1.0)
        self.career_max_count = max((c for (_a, _b, _n, c) in _lines), default=1)

        self.system_flows: List[Tuple[str, str, int, float]] = []          # (codeA, codeB, count, profit)
        self.terminal_flows: Dict[str, List[Tuple[str, str, int, float]]] = {}
        self.top_cross: List = []                            # Route objects (cross-system)
        self.top_lines: Dict[str, List[Tuple[str, str]]] = {}  # code -> [(nameA, nameB)] to draw in that system
        self.system_recency: Dict[str, float] = {}           # code -> 0..1 (freshest)
        self.body_recency: Dict[Tuple[str, str], float] = {}  # (code, norm body) -> 0..1
        self.max_flow = 1
        self._build()

    @staticmethod
    def _profit(r) -> float:
        return float(getattr(r, "profit", 0) or 0)

    def _build(self) -> None:
        sysflow: Dict[Tuple[str, str], List] = defaultdict(lambda: [0, 0.0])
        termflow: Dict[str, Dict[Tuple[str, str], List]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0.0]))
        sysrec: Dict[str, float] = {}
        bodyrec: Dict[Tuple[str, str], float] = {}
        cross: List = []
        touching: Dict[str, list] = defaultdict(list)        # code -> routes that start OR end here

        for r in self.routes:
            bs = self._sys_code(getattr(r, "buy_system", "") or "")
            ss = self._sys_code(getattr(r, "sell_system", "") or "")
            profit = self._profit(r)
            if bs and ss:
                if bs != ss:
                    e = sysflow[tuple(sorted((bs, ss)))]; e[0] += 1; e[1] += profit
                    cross.append(r)
                    touching[bs].append(r); touching[ss].append(r)
                else:
                    a, b = getattr(r, "buy_location", ""), getattr(r, "sell_location", "")
                    if a and b and a != b:
                        e = termflow[bs][tuple(sorted((a, b)))]; e[0] += 1; e[1] += profit
                    touching[bs].append(r)
            for tid_attr, code, loc_attr in (
                    ("id_terminal_buy", bs, "buy_location"),
                    ("id_terminal_sell", ss, "sell_location")):
                fresh = self._rec_term.get(getattr(r, tid_attr, 0) or 0)
                if fresh is None or not code:
                    continue
                sysrec[code] = max(sysrec.get(code, 0.0), fresh)
                key = (code, norm_loc(getattr(r, loc_attr, "") or ""))
                bodyrec[key] = max(bodyrec.get(key, 0.0), fresh)

        self.system_flows = [(a, b, c[0], c[1]) for (a, b), c in sysflow.items()]
        self.terminal_flows = {
            s: sorted(((a, b, c[0], c[1]) for (a, b), c in d.items()),
                      key=lambda t: -t[2])[:MAX_FLOWS_SYS]
            for s, d in termflow.items()}
        counts = [c for _a, _b, c, _p in self.system_flows] + \
                 [c for fl in self.terminal_flows.values() for _a, _b, c, _p in fl]
        self.max_flow = max(counts) if counts else 1
        self.system_recency = sysrec
        self.body_recency = bodyrec
        self.top_cross = sorted(cross, key=self._profit, reverse=True)[:TOP_CROSS]

        # Per-system top routes, as drawable name pairs (resolve in that system).
        for code, rs in touching.items():
            lines: List[Tuple[str, str]] = []
            for r in sorted(rs, key=self._profit, reverse=True)[:TOP_PER_SYS]:
                bs = self._sys_code(getattr(r, "buy_system", "") or "")
                ss = self._sys_code(getattr(r, "sell_system", "") or "")
                bl = getattr(r, "buy_location", "")
                sl = getattr(r, "sell_location", "")
                if bs == code and ss == code:
                    lines.append((bl, sl))                                  # intra-system leg
                elif bs == code:
                    lines.append((bl, f"{getattr(r, 'sell_system', '')} Gateway"))   # → jump out
                elif ss == code:
                    lines.append((f"{getattr(r, 'buy_system', '')} Gateway", sl))     # jump in →
            self.top_lines[code] = lines

    # ── view-facing queries ──────────────────────────────────────────────────
    def top_cross_system(self) -> List:
        return self.top_cross

    def top_lines_for(self, code: str) -> List[Tuple[str, str]]:
        return self.top_lines.get(code, [])
