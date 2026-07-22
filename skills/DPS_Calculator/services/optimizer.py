# -*- coding: utf-8 -*-
"""optimizer.py — recommend the best weapon per hardpoint for a chosen goal.

Erkul makes you guess: try a fit, read the number, try another. This picks, for
each weapon hardpoint, the highest-scoring weapon that fits it — so you can see the
theoretical max your ship can put out, and by how much your current fit trails it.

v0 is greedy per-slot (best fitting weapon for each hardpoint independently) — it does
NOT yet model the shared power pool, so treat the number as an upper-bound target, not
a guaranteed sustainable fit. Read-only: it recommends, it does not touch your loadout.

The scoring metric is injected via `key` ('dps_sus' | 'dps_raw' | 'alpha') so the same
routine serves the sustained / burst / alpha goals.
"""
from __future__ import annotations

from typing import Callable, Optional


def optimize_weapons(slots: list, weapon_candidates: Callable, key: str = "dps_sus") -> dict:
    """Best weapon per slot by `key`.

    slots: list of {id, max_size, label, gun_count?}.
    weapon_candidates: fn(max_size) -> list of weapon-stat dicts (each with 'size' + the key +
        'name'/'local_name'), already limited to weapons that fit that size.
    Returns {picks: [{slot, weapon, score}], total: float, current_hint: None}.
    """
    picks = []
    total = 0.0
    for slot in slots or []:
        msz = slot.get("max_size") or slot.get("weapon_max_size") or 0
        n = slot.get("gun_count", 1) or 1
        try:
            cands = weapon_candidates(msz) or []
        except Exception:
            cands = []
        fitting = [w for w in cands if (w.get("size") or 0) <= msz]
        if not fitting:
            picks.append({"slot": slot, "weapon": None, "score": 0.0})
            continue
        best = max(fitting, key=lambda w: float(w.get(key, 0) or 0))
        score = float(best.get(key, 0) or 0) * n
        picks.append({"slot": slot, "weapon": best, "score": score})
        total += score
    return {"picks": picks, "total": total}


def compare_to_current(optimized_total: float, current_total: float) -> dict:
    """How much a current fit trails the optimized upper bound."""
    cur = float(current_total or 0)
    opt = float(optimized_total or 0)
    delta = opt - cur
    pct = (delta / cur * 100.0) if cur > 0 else None
    return {"optimized": opt, "current": cur, "delta": delta, "pct_below": pct}
