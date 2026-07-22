#!/usr/bin/env python3
"""
erkul_config_diff.py — find ships where the DPS Calculator's own slot extraction
DISAGREES with an independent leaf-level enumeration of the Erkul data.

Honest framing (Elah, 2026-07-10): I do NOT have Erkul's exact UI-filtering
logic, so I can't declare "confirmed bug" from data alone. What I CAN do is run
two independent methods over the same raw data and surface every ship where they
disagree — turning "audit 219 ships by hand" into "spot-check the N ships that
diverge against live erkul.games". That's the triage list.

Method A (the local calc): services.slot_extractor.extract_slots_by_type, the
production heuristic maze, normalized to leaf count (turret groups expanded via
gun_count so grouping never causes a false diff).

Method B (independent oracle): walk the raw loadout tree and count the actual
LEAF weapon/missile positions — a gun position is a leaf port that holds/accepts
a weapon and has no weapon-bearing child; a missile is a *_attach leaf inside a
rack. Deliberately separate code from Method A so a bug in one can't hide in both.

A divergence means one side is wrong — either the heuristic dropped/invented a
slot, or my oracle's leaf rule is off for that ship. Both are worth a human's eye.

Usage:
    python erkul_config_diff.py                 # rank all ships by divergence
    python erkul_config_diff.py <ship>          # detail one ship (both methods)
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from services.slot_extractor import extract_slots_by_type  # noqa: E402

CACHE = HERE / ".erkul_cache.json"


def load_ships():
    return {s.get("localName"): s
            for s in json.loads(CACHE.read_text(encoding="utf-8"))["data"]["/live/ships"]}


# ---- Method A: local extractor, normalized to leaf count --------------------
def local_leaf_count(loadout):
    guns = extract_slots_by_type(loadout, {"WeaponGun"})
    misl = extract_slots_by_type(loadout, {"MissileLauncher"})
    # turret housings emit one slot carrying gun_count=N; expand to N leaves.
    g = sum(max(int(s.get("gun_count", 1) or 1), 1) for s in guns)
    m = sum(1 for s in misl if s.get("is_missile")) + sum(1 for s in misl if not s.get("is_missile") and not s.get("is_rack"))
    return g, m


# ---- Method B: independent raw-tree leaf oracle -----------------------------
_GUN_TYPES = {"WeaponGun"}


def _types(port):
    return {t.get("type", "") for t in (port.get("itemTypes") or [])}
def _subs(port):
    return {t.get("subType", "") for t in (port.get("itemTypes") or [])}


def oracle_leaf_count(loadout):
    """Count leaf gun positions and leaf missiles by an independent walk."""
    guns = 0
    missiles = 0

    def is_gun_leaf(port):
        # a WeaponGun-typed port with no child that is itself a weapon/gun port
        if "WeaponGun" not in _types(port):
            return False
        for c in (port.get("loadout") or []):
            ipn = c.get("itemPortName", "").lower()
            if "WeaponGun" in _types(c) or any(k in ipn for k in ("class", "gun", "weapon", "turret")):
                return False
        return True

    def walk(ports):
        nonlocal guns, missiles
        for p in (ports or []):
            ipn = p.get("itemPortName", "").lower()
            ty = _types(p)
            sub = _subs(p)
            # leaf gun position
            if is_gun_leaf(p):
                guns += 1
            # gun positions inside turrets that carry no itemTypes (inner arms)
            elif not ty and any(k in ipn for k in ("hardpoint_class", "turret_", "hardpoint_gun_", "hardpoint_weapon_")) \
                    and not (p.get("loadout")):
                guns += 1
            # missile leaf: *_attach node inside a rack, holding a missile.
            # A rack is identified THREE ways (matching the extractor): typed
            # MissileLauncher/MissileRack, localName mrck_*, OR (untyped ground-
            # vehicle racks) any child whose port name is a missile attach point.
            is_rack = (("MissileLauncher" in ty and "MissileRack" in sub)
                       or p.get("localName", "").startswith("mrck_")
                       or any("missile" in (c.get("itemPortName", "").lower())
                              and not c.get("loadout")
                              for c in (p.get("loadout") or [])))
            if is_rack:
                for c in (p.get("loadout") or []):
                    cipn = c.get("itemPortName", "").lower()
                    cref = c.get("localReference") or c.get("localName") or ""
                    # count a LOADED missile/torpedo sub-slot. The ref may be a
                    # misl_/torp_ localName (Gladius) OR a UUID localReference
                    # (Cutlass) — both mean "a missile is equipped here". Requiring
                    # only a non-empty ref catches both representations.
                    if ("missile" in cipn or "torpedo" in cipn) and cref:
                        missiles += 1
            walk(p.get("loadout"))

    walk(loadout)
    return guns, missiles


def main(argv):
    ships = load_ships()
    target = next((a for a in argv[1:] if not a.startswith("-")), None)

    if target:
        s = ships.get(target)
        if not s:
            near = [k for k in ships if target.lower() in k.lower()][:8]
            print("unknown ship." + (f" near: {near}" if near else ""))
            return
        lo = s["data"].get("loadout") or []
        la, lm = local_leaf_count(lo)
        oa, om = oracle_leaf_count(lo)
        print(f"{target}:")
        print(f"  guns     local={la:3d}  oracle={oa:3d}  {'DIVERGE' if la!=oa else 'agree'}")
        print(f"  missiles local={lm:3d}  oracle={om:3d}  {'DIVERGE' if lm!=om else 'agree'}")
        return

    rows = []
    for ln, s in ships.items():
        lo = s["data"].get("loadout") or []
        try:
            la, lm = local_leaf_count(lo)
            oa, om = oracle_leaf_count(lo)
        except Exception as e:
            rows.append((999, ln, f"ERROR {e}"))
            continue
        d = abs(la - oa) + abs(lm - om)
        if d:
            rows.append((d, ln, f"guns {la} vs {oa}   missiles {lm} vs {om}"))
    rows.sort(reverse=True)
    print(f"# ships where local extractor and independent oracle DIVERGE (triage list)")
    print(f"# {len(rows)} of {len(ships)} ships diverge — spot-check these vs live erkul\n")
    for d, ln, msg in rows[:40]:
        print(f"  [{d:3d}] {ln:32} {msg}")


if __name__ == "__main__":
    main(sys.argv)
