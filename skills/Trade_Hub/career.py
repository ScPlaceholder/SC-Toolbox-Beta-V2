"""Trader career tracking — persisted completed/failed routes + favorite routes,
with profit/loss stats, a per-ship leaderboard, and a UEC-cycle reset (with undo).

Saved atomically to ``~/.sctoolbox/trade_hub/career.json`` so progress survives
crashes, updates and version changes — the same place the star map persists.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from datetime import date
from typing import Dict, List, Optional, Tuple

_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "trade_hub")
_PATH = os.path.join(_DIR, "career.json")
_LISTS = ("completed", "failures", "favorites")


def _day_of(entry: dict) -> date:
    try:
        return date.fromtimestamp(float(entry.get("ts", 0)))
    except (ValueError, OSError, OverflowError):
        return date.today()


def _fav_key(d: dict) -> tuple:
    return (d.get("commodity", ""), d.get("buy_location", ""),
            d.get("sell_location", ""), d.get("ship", ""))


def _load() -> dict:
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError):
        d = {}
    for k in _LISTS:
        if not isinstance(d.get(k), list):
            d[k] = []
    d.setdefault("undo", None)
    return d


def _save(data: dict) -> None:
    try:
        os.makedirs(_DIR, exist_ok=True)
        tmp = _PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, _PATH)
    except OSError:
        pass


class Career:
    def __init__(self) -> None:
        self.data = _load()

    def save(self) -> None:
        _save(self.data)

    # ── logging ───────────────────────────────────────────────────────────────
    def complete(self, entry: dict) -> None:
        e = dict(entry)
        e.setdefault("ts", time.time())
        self.data["completed"].append(e)
        self.save()

    def fail(self, entry: dict) -> None:
        e = dict(entry)
        e.setdefault("ts", time.time())
        self.data["failures"].append(e)
        self.save()

    # ── edit / remove logged runs ──────────────────────────────────────────────
    def completed_runs(self) -> List[dict]:
        return list(self.data["completed"])

    def failed_runs(self) -> List[dict]:
        return list(self.data["failures"])

    def remove_entry(self, entry: dict, failed: bool) -> bool:
        """Delete a single logged run, matched by object identity."""
        lst = self.data["failures"] if failed else self.data["completed"]
        for i, e in enumerate(lst):
            if e is entry:
                lst.pop(i)
                self.save()
                return True
        return False

    def set_completed(self, entry: dict) -> bool:
        """Move a failed run back to completed, restoring its profit (recomputed
        from its stored prices if the profit wasn't recorded on an old entry)."""
        for i, e in enumerate(self.data["failures"]):
            if e is entry:
                self.data["failures"].pop(i)
                if not e.get("profit"):
                    e["profit"] = self._infer_profit(e)
                self.data["completed"].append(e)
                self.save()
                return True
        return False

    def set_failed(self, entry: dict, kind: str = "full", loss=None, reasons=None) -> bool:
        """Move a completed run to failures with explicit failure details (kind,
        loss, reasons). A full loss defaults to the run's investment."""
        kind = kind or "full"
        for i, e in enumerate(self.data["completed"]):
            if e is entry:
                self.data["completed"].pop(i)
                e["kind"] = kind
                e["reasons"] = list(reasons or [])
                e["loss"] = (e.get("investment", 0) or 0) if kind == "full" else float(loss or 0)
                self.data["failures"].append(e)
                self.save()
                return True
        return False

    @staticmethod
    def _infer_profit(e: dict) -> float:
        """Best-effort profit for a run = (sell - buy) * SCU, when the prices were
        stored with it; else falls back to whatever 'profit' is recorded."""
        try:
            ps = float(e.get("price_sell") or 0)
            pb = float(e.get("price_buy") or 0)
            scu = float(e.get("scu") or 0)
            if ps and scu:
                return (ps - pb) * scu
        except (TypeError, ValueError):
            pass
        return float(e.get("profit") or 0)

    # ── favorites ─────────────────────────────────────────────────────────────
    def add_favorite(self, fav: dict) -> bool:
        key = _fav_key(fav)
        if any(_fav_key(f) == key for f in self.data["favorites"]):
            return False
        self.data["favorites"].append(dict(fav))
        self.save()
        return True

    def remove_favorite(self, index: int) -> None:
        if 0 <= index < len(self.data["favorites"]):
            self.data["favorites"].pop(index)
            self.save()

    def favorites(self) -> List[dict]:
        return list(self.data["favorites"])

    def favorite_ships(self) -> List[str]:
        out: List[str] = []
        for f in self.data["favorites"]:
            s = f.get("ship", "")
            if s and s not in out:
                out.append(s)
        return out

    # ── reset / undo ──────────────────────────────────────────────────────────
    def reset(self) -> None:
        """Hard-reset the trade ledger for a new UEC cycle (keeps favorites).
        The cleared history is stashed so a single undo can restore it."""
        self.data["undo"] = {"completed": self.data["completed"],
                             "failures": self.data["failures"]}
        self.data["completed"] = []
        self.data["failures"] = []
        self.save()

    def undo_reset(self) -> bool:
        u = self.data.get("undo")
        if not u:
            return False
        self.data["completed"] = u.get("completed", [])
        self.data["failures"] = u.get("failures", [])
        self.data["undo"] = None
        self.save()
        return True

    def has_undo(self) -> bool:
        return bool(self.data.get("undo"))

    # ── stats ─────────────────────────────────────────────────────────────────
    def gross_profit(self) -> float:
        return sum(float(e.get("profit", 0) or 0) for e in self.data["completed"])

    def total_loss(self) -> float:
        return sum(float(e.get("loss", 0) or 0) for e in self.data["failures"])

    def net_profit(self) -> float:
        return self.gross_profit() - self.total_loss()

    def counts(self) -> Tuple[int, int, float]:
        c = len(self.data["completed"])
        f = len(self.data["failures"])
        return c, f, (c / (c + f) * 100.0 if (c + f) else 0.0)

    def per_day(self) -> Dict[date, float]:
        by: Dict[date, float] = defaultdict(float)
        for e in self.data["completed"]:
            by[_day_of(e)] += float(e.get("profit", 0) or 0)
        for e in self.data["failures"]:
            by[_day_of(e)] -= float(e.get("loss", 0) or 0)
        return dict(by)

    def cumulative(self) -> List[Tuple[date, float]]:
        by = self.per_day()
        run = 0.0
        out = []
        for d in sorted(by):
            run += by[d]
            out.append((d, run))
        return out

    def trades_on(self, d: date) -> Tuple[List[dict], List[dict]]:
        comp = [e for e in self.data["completed"] if _day_of(e) == d]
        fail = [e for e in self.data["failures"] if _day_of(e) == d]
        return comp, fail

    def ship_stats(self) -> List[dict]:
        agg: Dict[str, dict] = defaultdict(
            lambda: {"completed": 0, "failed": 0, "profit": 0.0, "loss": 0.0, "investment": 0.0})
        for e in self.data["completed"]:
            s = agg[e.get("ship") or "Unknown"]
            s["completed"] += 1
            s["profit"] += float(e.get("profit", 0) or 0)
            s["investment"] += float(e.get("investment", 0) or 0)
        for e in self.data["failures"]:
            s = agg[e.get("ship") or "Unknown"]
            s["failed"] += 1
            s["loss"] += float(e.get("loss", 0) or 0)
        rows = []
        for ship, s in agg.items():
            tot = s["completed"] + s["failed"]
            net = s["profit"] - s["loss"]
            rows.append({
                "ship": ship,
                "completed": s["completed"], "failed": s["failed"], "routes": tot,
                "success": (s["completed"] / tot * 100.0) if tot else 0.0,
                "net": net, "investment": s["investment"],
                "roi": (net / s["investment"] * 100.0) if s["investment"] else 0.0,
                "avg": (net / s["completed"]) if s["completed"] else 0.0,
            })
        return rows

    def commodity_stats(self) -> List[dict]:
        """Per-commodity finished/failed run counts + net result, busiest first —
        feeds the career commodity heatmap."""
        agg: Dict[str, dict] = defaultdict(
            lambda: {"finished": 0, "failed": 0, "profit": 0.0, "loss": 0.0})
        for e in self.data["completed"]:
            s = agg[e.get("commodity") or "Unknown"]
            s["finished"] += 1
            s["profit"] += float(e.get("profit", 0) or 0)
        for e in self.data["failures"]:
            s = agg[e.get("commodity") or "Unknown"]
            s["failed"] += 1
            s["loss"] += float(e.get("loss", 0) or 0)
        rows = [{
            "commodity": com, "finished": s["finished"], "failed": s["failed"],
            "runs": s["finished"] + s["failed"], "net": s["profit"] - s["loss"],
            "profit": s["profit"], "loss": s["loss"],
        } for com, s in agg.items()]
        rows.sort(key=lambda r: (-r["runs"], -r["net"]))
        return rows

    def map_data(self, sys_code) -> dict:
        """Everything the star map 'My Runs' overlay needs: per-system & per-body
        net result (heat rings) PLUS the actual paths flown (lines). Cross-system
        runs draw to/from the jump gateway in each endpoint system. ``sys_code``
        maps a system NAME to its galaxy code. Lines are aggregated per
        terminal-pair (count = how many times the user flew it)."""
        from starmap.data import norm_loc
        sysnet: Dict[str, float] = defaultdict(float)
        bodynet: Dict[Tuple[str, str], float] = defaultdict(float)
        galaxy: Dict[Tuple[str, str], list] = defaultdict(lambda: [0, 0.0])
        sysl: Dict[str, Dict[Tuple[str, str], list]] = defaultdict(
            lambda: defaultdict(lambda: [0, 0.0]))

        def attribute(e: dict, val: float) -> None:
            bs = sys_code(e.get("buy_system", "") or "")
            ss = sys_code(e.get("sell_system", "") or "")
            bl = e.get("buy_location", "") or ""
            sl = e.get("sell_location", "") or ""
            seen_s, seen_b = set(), set()
            for code, loc in ((bs, bl), (ss, sl)):
                if code and code not in seen_s:
                    sysnet[code] += val
                    seen_s.add(code)
                if code and loc:
                    k = (code, norm_loc(loc))
                    if k not in seen_b:
                        bodynet[k] += val
                        seen_b.add(k)
            if not (bs and ss):
                return
            if bs == ss:
                if bl and sl and bl != sl:
                    c = sysl[bs][tuple(sorted((bl, sl)))]; c[0] += 1; c[1] += val
            else:
                g = galaxy[tuple(sorted((bs, ss)))]; g[0] += 1; g[1] += val
                if bl:
                    c = sysl[bs][(bl, f"{e.get('sell_system','')} Gateway")]; c[0] += 1; c[1] += val
                if sl:
                    c = sysl[ss][(f"{e.get('buy_system','')} Gateway", sl)]; c[0] += 1; c[1] += val

        for e in self.data["completed"]:
            attribute(e, float(e.get("profit", 0) or 0))
        for e in self.data["failures"]:
            attribute(e, -float(e.get("loss", 0) or 0))
        return {
            "system_net": dict(sysnet),
            "body_net": dict(bodynet),
            "galaxy_lines": [(a, b, c[1], c[0]) for (a, b), c in galaxy.items()],
            "system_lines": {code: [(a, b, c[1], c[0]) for (a, b), c in d.items()]
                             for code, d in sysl.items()},
        }

    def best_by(self, field: str) -> Optional[Tuple[str, float]]:
        agg: Dict[str, float] = defaultdict(float)
        for e in self.data["completed"]:
            agg[e.get(field) or ""] += float(e.get("profit", 0) or 0)
        for e in self.data["failures"]:
            agg[e.get(field) or ""] -= float(e.get("loss", 0) or 0)
        agg.pop("", None)
        if not agg:
            return None
        k = max(agg, key=lambda x: agg[x])
        return k, agg[k]
