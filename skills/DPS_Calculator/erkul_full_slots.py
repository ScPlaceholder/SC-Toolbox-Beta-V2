#!/usr/bin/env python3
"""
erkul_full_slots.py — the EXHAUSTIVE recursive slot extractor for Erkul ships.

Why this exists (Elah, 2026-07-10): CC's audits kept "missing items" because a
ship's loadout is a TREE, not a flat list. Certain ports (guns, turrets, missile
racks) carry editableChildren=True AND their own nested `loadout` key; the real
gimbals, turret guns, rack missiles, PDCs and utility items live DOWN INSIDE those
child loadouts, up to 3 levels deep. CC read the top-level list and stopped, so on
the Constellation Andromeda it saw 102 ports and was blind to 84 real ones.

This walker descends every port's `loadout` to the leaf, records the full path +
size range + item types + installed item at each node, and — critically — asserts
its own coverage: it counts every node in the raw tree independently and FAILS if
the flattened output has fewer, so an incomplete walk can never silently pass.

Usage:
    python erkul_full_slots.py aegs_gladius        # full flattened chart for one ship
    python erkul_full_slots.py --counts            # top-vs-recursed port counts, all ships
    python erkul_full_slots.py --coverage-check     # assert extractor sees every node (all ships)
"""
import json
import sys
from pathlib import Path

CACHE = Path(__file__).resolve().parent / ".erkul_cache.json"


def load_ships():
    data = json.loads(CACHE.read_text(encoding="utf-8"))["data"]
    return {s.get("localName"): s for s in data["/live/ships"]}


def _port_types(port):
    """Compact 'Type/SubType' list for a port's acceptable item types."""
    out = []
    for t in (port.get("itemTypes") or []):
        if isinstance(t, dict):
            s = t.get("type", "?")
            if t.get("subType"):
                s += "/" + t["subType"]
            out.append(s)
    return out


def walk_ports(ports, path="", depth=0):
    """Yield one dict per port at every depth, following each port's own `loadout`."""
    for p in (ports or []):
        name = p.get("itemPortName", "?")
        here = f"{path}/{name}" if path else name
        installed = p.get("localReference") or p.get("localName") or None
        yield {
            "path": here,
            "depth": depth,
            "port": name,
            "minSize": p.get("minSize"),
            "maxSize": p.get("maxSize"),
            "editableChildren": p.get("editableChildren"),
            "types": _port_types(p),
            "installed": installed,
        }
        kids = p.get("loadout")
        if isinstance(kids, list):
            yield from walk_ports(kids, here, depth + 1)


def raw_node_count(ports):
    """Independent count of every port node in the raw tree — the coverage oracle.
    Deliberately NOT sharing code with walk_ports, so a bug in one can't hide in both."""
    n = 0
    stack = list(ports or [])
    while stack:
        p = stack.pop()
        n += 1
        kids = p.get("loadout")
        if isinstance(kids, list):
            stack.extend(kids)
    return n


def extract_ship(ship):
    lo = ship["data"].get("loadout") or []
    slots = list(walk_ports(lo))
    oracle = raw_node_count(lo)
    if len(slots) != oracle:
        raise AssertionError(
            f"COVERAGE FAILURE on {ship.get('localName')}: extractor saw {len(slots)} "
            f"ports but the raw tree has {oracle}. The walk is incomplete.")
    return slots


def main(argv):
    ships = load_ships()

    if "--counts" in argv:
        print(f"{'ship':32} {'top':>5} {'recursed':>9} {'depth':>6}")
        for ln, s in sorted(ships.items()):
            lo = s["data"].get("loadout") or []
            slots = extract_ship(s)
            depth = max((x["depth"] for x in slots), default=0)
            print(f"{ln:32} {len(lo):5d} {len(slots):9d} {depth:6d}")
        return

    if "--coverage-check" in argv:
        bad = 0
        for ln, s in ships.items():
            try:
                extract_ship(s)
            except AssertionError as e:
                bad += 1
                print("FAIL:", e)
        print(f"\ncoverage check: {len(ships)-bad}/{len(ships)} ships fully covered"
              f"{' — ALL PASS' if not bad else f' — {bad} FAILURES'}")
        raise SystemExit(0 if not bad else 3)

    target = next((a for a in argv[1:] if not a.startswith("-")), None)
    if not target or target not in ships:
        print("usage: erkul_full_slots.py <ship_localName> | --counts | --coverage-check")
        if target:
            near = [k for k in ships if target.lower() in k.lower()][:8]
            if near:
                print("did you mean:", near)
        return
    slots = extract_ship(ships[target])
    print(f"# {target}: {len(slots)} total ports (recursive, coverage-verified)\n")
    for x in slots:
        indent = "  " * x["depth"]
        sz = f"s{x['minSize']}" if x["minSize"] == x["maxSize"] else f"s{x['minSize']}-{x['maxSize']}"
        types = ",".join(x["types"])[:40]
        inst = f"  = {x['installed']}" if x["installed"] else ""
        print(f"{indent}{x['port']:38} {sz:8} [{types}]{inst}")


if __name__ == "__main__":
    main(sys.argv)
