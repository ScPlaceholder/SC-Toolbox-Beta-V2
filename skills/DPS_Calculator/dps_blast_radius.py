# -*- coding: utf-8 -*-
"""Blast-radius harness for the DPS/Erkul divergence audit.

The problem this solves (recorded catch, 2026-07-13): every candidate rule-fix
for a one-off divergence risks REGRESSING other ships. The Hornet nose/center
rule looked like a +6 win but was net 9->18 once you counted the 15 ships it
broke. Until now that blast-radius number was HAND-counted — slow, error-prone,
and the reason "fix the last 3 one-offs" kept stalling.

This makes it mechanical. It reuses erkul_truth_parity's own truth_counts /
local_counts (the rendered-Erkul arbiter) — it does NOT reimplement counting, so
it can't drift from the real audit. Two modes:

    python dps_blast_radius.py snapshot [out.json]   # capture per-ship divergence baseline
    python dps_blast_radius.py diff BEFORE.json AFTER.json

`diff` reports exactly what a with-J session needs: which ships CHANGED, whether
each got better or worse, and the NET divergence delta (the 9->18 number),
so a proposed rule is judged on its whole blast radius, not just the ship it
was aimed at. Measure-only — imports and reads, never mutates the extractor.

Elah, 2026-07-13 — built ON erkul_truth_parity.py per retrieval-first (the record
said the next real move needs this harness; no harness existed, so here it is).
"""
import sys, json
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
import erkul_truth_parity as etp  # reuse the real arbiter's counting — no reimplementation


def snapshot():
    """Per-ship {guns_div, missiles_div} vs the Erkul rendered truth. The baseline."""
    truth = etp.load_truth()
    cache = etp.load_cache_ships()
    bridge = etp.build_bridge(truth, cache)
    ships, unmatched, errors = {}, 0, 0
    for disp, rec in truth.items():
        ln = bridge.get(disp)
        if not ln:
            unmatched += 1
            continue
        try:
            tg, tm = etp.truth_counts(rec)
            lo = cache[ln]["data"].get("loadout") or []
            lg, lm = etp.local_counts(lo)
        except Exception:
            errors += 1
            continue
        ships[disp] = {"g": tg - lg, "m": tm - lm}  # signed: +local undercounts, -local overcounts
    return {"ships": ships, "unmatched": unmatched, "errors": errors}


def _totals(snap):
    g = sum(abs(v["g"]) for v in snap["ships"].values())
    m = sum(abs(v["m"]) for v in snap["ships"].values())
    gships = sum(1 for v in snap["ships"].values() if v["g"])
    mships = sum(1 for v in snap["ships"].values() if v["m"])
    return g, m, gships, mships


def cmd_snapshot(out):
    snap = snapshot()
    g, m, gs, ms = _totals(snap)
    Path(out).write_text(json.dumps(snap, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[blast-radius] snapshot -> {out}")
    print(f"  gun divergence total: {g} across {gs} ships")
    print(f"  msl divergence total: {m} across {ms} ships")
    print(f"  ({snap['unmatched']} truth ships unmatched, {snap['errors']} errored)")


def cmd_diff(before_p, after_p):
    b = json.loads(Path(before_p).read_text(encoding="utf-8"))
    a = json.loads(Path(after_p).read_text(encoding="utf-8"))
    bg, bm, _, _ = _totals(b)
    ag, am, _, _ = _totals(a)
    changed = []
    for ship in sorted(set(b["ships"]) | set(a["ships"])):
        bv = b["ships"].get(ship, {"g": 0, "m": 0})
        av = a["ships"].get(ship, {"g": 0, "m": 0})
        if bv != av:
            # did this ship's total divergence shrink (good) or grow (regression)?
            db = abs(bv["g"]) + abs(bv["m"])
            da = abs(av["g"]) + abs(av["m"])
            tag = "FIXED " if da < db else "WORSE " if da > db else "shift "
            changed.append((tag, ship, bv, av))
    print(f"# BLAST RADIUS: {before_p} -> {after_p}")
    print(f"# gun divergence  {bg} -> {ag}  (net {ag-bg:+d})")
    print(f"# msl divergence  {bm} -> {am}  (net {am-bm:+d})")
    print(f"# {len(changed)} ships changed\n")
    for tag, ship, bv, av in changed:
        print(f"  {tag} {ship:28}  g {bv['g']:+d}->{av['g']:+d}   m {bv['m']:+d}->{av['m']:+d}")
    if not changed:
        print("  (identical — the rule changed nothing)")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a and a[0] == "snapshot":
        cmd_snapshot(a[1] if len(a) > 1 else str(HERE / "blast_baseline.json"))
    elif a and a[0] == "diff" and len(a) >= 3:
        cmd_diff(a[1], a[2])
    else:
        print("usage: dps_blast_radius.py snapshot [out.json] | diff BEFORE.json AFTER.json")
