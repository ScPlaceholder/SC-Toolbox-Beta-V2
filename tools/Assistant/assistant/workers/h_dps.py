"""DPS worker: best weapon per gun hardpoint, from scunpacked-data.

Runs in the DPS Calculator's folder so it can reuse that tool's greedy
per-slot optimizer (services/optimizer.py optimize_weapons). Ship,
hardpoint and weapon data come from the Assistant's own scunpacked
adapter (assistant/scunpacked.py, loaded by path), not from the
calculator's erkul cache: server.erkul.games is gone and that cache is
empty.

The adapter decides which weapons may go in which slot (size range,
required tags, locked ports). The optimizer is then asked about ONE slot
at a time with exactly that slot's candidates, because its own filter
only checks ``size <= max_size`` and would happily put an S3 gun in an S4
turret. Every pick is re-checked against the size range before returning.
"""
from __future__ import annotations

import importlib.util
import os
import sys

import _assist_logic as L

from services.optimizer import optimize_weapons   # DPS_Calculator/services

_HERE = os.path.dirname(os.path.abspath(__file__))


def _adapter():
    name = "_assist_scunpacked"
    mod = sys.modules.get(name)
    if mod is None:
        path = os.path.join(os.path.dirname(_HERE), "scunpacked.py")
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


S = _adapter()

NOTE = ("Guns only (no missiles, power, shields or coolers). Upper bound: each "
        "slot gets its best gun independently and the ship's weapon power pool "
        "is not modelled, so an energy-heavy build may not sustain it in game. "
        "Sustained DPS is per weapon (heat/capacitor). Mount swaps are not "
        "modelled; each slot keeps its size range.")

_EXCLUDED = {"pdc": "automated point-defence turret (not a pilot/gunner weapon)",
             "crew mount": "crew-operated door gun, locked to its stock weapon"}


def _slot_label(slot: dict) -> str:
    parts = slot["id"].split("/")
    if len(parts) > 1 and parts[-1].startswith("hardpoint_class"):
        parts = parts[:-1]
    return "/".join(parts)


def _r(x):
    return None if x is None else round(float(x), 1)


def best_ship_weapons(ship: str, goal: str = "sustained") -> dict:
    goal = (goal or "sustained").strip().lower()
    goal = {"sustain": "sustained", "dps": "sustained", "raw": "burst",
            "peak": "burst", "alpha strike": "alpha"}.get(goal, goal)
    if goal not in S.GOALS:
        raise L.ToolFail(f"goal must be sustained, burst or alpha (got '{goal}')")
    key = S.GOALS[goal]
    try:
        idx = S.load_index()
        entry, alts = S.find_ship(idx, ship)
    except S.ScunpackedError as exc:
        raise L.ToolFail(str(exc))
    weapons = idx["weapons"]

    picks, not_counted = [], []
    tot = {"goal": 0.0, "dps_sus": 0.0, "dps_raw": 0.0, "alpha": 0.0}
    stock = {"dps_sus": 0.0, "dps_raw": 0.0, "alpha": 0.0}
    summary: dict = {}
    for slot in entry["slots"]:
        sw = weapons.get(slot["stock"]) if slot["stock"] else None
        label = _slot_label(slot)
        if slot["mount"] in _EXCLUDED:
            reason = _EXCLUDED[slot["mount"]]
            grp = next((g for g in not_counted if g["reason"] == reason), None)
            if grp is None:
                grp = {"reason": reason, "guns": 0, "weapons": [], "example_slot": label}
                not_counted.append(grp)
            grp["guns"] += 1
            wn = sw["name"] if sw else "empty"
            if wn not in grp["weapons"]:
                grp["weapons"].append(wn)
            continue
        cands = sorted(S.candidates_for(slot, weapons, key), key=lambda w: w["name"])
        res = optimize_weapons([slot], lambda msz, c=cands: c, key=key)
        best = res["picks"][0]["weapon"]
        if best is not None and not S.fits(slot, best):
            raise L.ToolFail(f"size rule violated: {best['name']} (S{best['size']}) "
                             f"in {label} (S{slot['min_size']}-S{slot['max_size']})")
        row = {"slot": slot["hardpoint"], "guns": 1, "size": slot["max_size"],
               "mount": slot["mount"]}
        if best is None:
            row.update({"weapon": None, "dps": 0.0,
                        "why": "no weapon fits this slot"})
        else:
            row.update({"weapon": best["name"], "weapon_size": best["size"],
                        "dps": _r(best[key])})
            if best.get("fire") == "charged":
                row["caveat"] = "charged weapon: DPS depends on charge time; unverified"
            elif best.get("fire") == "beam":
                row["caveat"] = "beam: no sustained figure in the data"
            elif "ScatterGun" in (best.get("tags") or ()):
                row["caveat"] = "scatter gun: every pellet hitting"
            for k in ("dps_sus", "dps_raw", "alpha"):
                tot[k] += float(best.get(k) or 0)
            tot["goal"] += float(best[key] or 0)
            sk = (best["name"], best["size"])
            summary[sk] = summary.get(sk, 0) + 1
        row["stock"] = sw["name"] if sw else ("empty" if not slot["stock"] else slot["stock"])
        if not slot["editable"]:
            row["locked"] = True
        prev = picks[-1] if picks else None
        if prev and all(prev.get(k) == row.get(k) for k in
                        ("slot", "size", "mount", "weapon", "stock", "locked", "caveat")):
            prev["guns"] += 1                 # same hardpoint (a turret), same pick
        else:
            picks.append(row)
        if sw:
            for k in stock:
                stock[k] += float(sw.get(k) or 0)

    result = {
        "ship": entry["name"],
        "ship_class": entry["cls"],
        "vehicle": entry["vehicle"],
        "goal": goal,
        "metric": {"sustained": "sustained DPS", "burst": "burst DPS",
                   "alpha": "alpha (damage per shot)"}[goal],
        "gun_slots": sum(p["guns"] for p in picks),
        "picks": picks,          # one row per hardpoint; "guns" = gun slots in it, dps is per gun
        "summary": [{"weapon": n, "size": s, "count": c}
                    for (n, s), c in sorted(summary.items(), key=lambda kv: (-kv[0][1], kv[0][0]))],
        "total": _r(tot["goal"]),
        "totals": {"sustained_dps": _r(tot["dps_sus"]), "burst_dps": _r(tot["dps_raw"]),
                   "alpha": _r(tot["alpha"])},
        "stock_totals": {"sustained_dps": _r(stock["dps_sus"]), "burst_dps": _r(stock["dps_raw"]),
                         "alpha": _r(stock["alpha"])},
        "data_build": idx["build"],
        "data_commit": idx["commit"][:12],
        "note": NOTE,
        "attribution": S.ATTRIBUTION,
    }
    if not_counted:
        result["not_counted"] = not_counted
    if entry.get("empty_turret_ports"):
        result["empty_turret_ports"] = len(entry["empty_turret_ports"])
    if alts:
        result["did_you_mean"] = alts
    if not picks:
        result["empty"] = True
        result["note"] = f"{entry['name']} has no gun hardpoints in this data. " + NOTE
    return result


EXPORTS = {"best_ship_weapons": best_ship_weapons}
