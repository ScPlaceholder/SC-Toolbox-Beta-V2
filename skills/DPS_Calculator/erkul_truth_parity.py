#!/usr/bin/env python3
"""
erkul_truth_parity.py — the audit CC never actually ran.

THE BUG IN EVERY PRIOR AUDIT (Elah, 2026-07-10):
CC went to real trouble and captured erkul's LIVE RENDERED slot structure for
217 ships into erkul_slot_truth.json (via a Playwright scrape, erkul_slot_truth.py).
That file is the ground truth — what erkul's own UI shows a player.

Then every parity audit ignored it. slot_parity_audit.py compares the local
extractor against count_erkul_raw_slots() — a SECOND heuristic that re-walks the
raw .erkul_cache.json tree with the same turret_/hardpoint_class/hardpoint_weapon
prefix rules the extractor itself uses. Heuristic vs heuristic over the same
swamp: they share blind spots, so they AGREE (→ "audit passes", confidence) and
where they disagree there's no arbiter to say who's right (→ fixes are guesses).
That is exactly how you get "confidently wrong for weeks."

(I nearly repeated it — erkul_config_diff.py diffs local vs my own raw-tree
oracle. Same mistake. The truth file was on disk the whole time.)

THIS TOOL diffs the local extractor against erkul_slot_truth.json — the real
rendered arbiter — so a divergence means the CALCULATOR disagrees with ERKUL,
not with another homemade heuristic.

Parsing note: erkul groups identical sibling slots and marks the group "xN" in
the item label (e.g. a nose mount "CF-337 Panther x2", or a mount row
"VariPuck S3 x2" with one gun under it = two guns). The true gun count is the
product of the xN multipliers along each weapons-leaf's ancestor chain. We
reconstruct erkul's tree from its flat depth list and propagate multipliers.

Usage:
    python erkul_truth_parity.py                # rank ships by |local - erkul truth|
    python erkul_truth_parity.py <ship>         # detail one ship, both sides
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from services.slot_extractor import extract_slots_by_type  # noqa: E402

CACHE = HERE / ".erkul_cache.json"
TRUTH = HERE / "erkul_slot_truth.json"

_XN = re.compile(r"\bx(\d+)\s*$")


def _mult(item: str) -> int:
    """Trailing 'xN' on an erkul item label -> N (default 1)."""
    m = _XN.search(item or "")
    return int(m.group(1)) if m else 1


# ---- ground truth: parse erkul's rendered slots into real gun/missile counts -
def truth_counts(ship_rec):
    """From erkul's flat depth-ordered slot list, count TRUE gun SLOTS & missiles.

    Gun-slot rule (a DPS-calculator counts HARDPOINTS, filled or empty — verified
    on Aurora/Valkyrie):
      * a 'weapons' row = an equipped gun (xN captures dual mounts, e.g. the
        Valkyrie nose 'Panther x2') -> count it.
      * an EMPTY 'gimbal-turret' mount (no 'weapons' descendant) = an available
        but unfilled gun hardpoint -> count it too. erkul renders these as mount
        rows; the calculator rightly shows them as configurable slots. Counting
        only filled 'weapons' rows under-counts erkul and falsely flagged the
        Aurora/Mustang/Nox/Cyclone families as local +2 (they were correct).
      * a filled 'gimbal-turret' mount is NOT counted itself — its 'weapons'
        child already is (no double-count).
    Missiles: 'missile' rows only (the 'missiles' pylon row is a housing, skip).
    """
    slots = ship_rec["slots"]
    guns = 0
    missiles = 0
    mult_at = {}   # depth -> cumulative xN multiplier at that node
    # Weapon zone = the leading run of slots erkul renders in the guns/turrets
    # area, before the first missiles / component / paint section. The scrape
    # stores an EMPTY slot with category="" (it loses the unfilled hardpoint's
    # type), so an empty GUN mount is indistinguishable from empty paint by
    # category alone — but erkul's UI shows it as a configurable gun slot, and
    # the local extractor (reading real port types) counts it. Count a depth-0
    # empty row as a gun IFF it falls in the weapon zone. Fixes the 6 false
    # positives (Nox/Nox Kue/Mustang A/B/Ghost Mk II/Hornet Mk II) where the
    # calculator was right and this arbiter was blind to empty slots.
    _ZONE_STOP = {"missiles", "missile", "shields", "coolers", "power-plants",
                  "quantum-drives", "jump-module", "radars", "paints", "salvage",
                  "utilities", "qeds", "bombs"}
    in_weapon_zone = True
    for i, s in enumerate(slots):
        d = s.get("depth", 0)
        cat = str(s.get("category", "")).lower()
        item = s.get("item", "") or ""
        if cat in _ZONE_STOP:
            in_weapon_zone = False
        parent_mult = mult_at.get(d - 1, 1) if d > 0 else 1
        cum = parent_mult * _mult(item)
        mult_at[d] = cum
        for k in [k for k in mult_at if k > d]:
            del mult_at[k]

        if cat == "weapons":
            guns += cum
        elif cat == "gimbal-turret":
            # empty mount = a gun slot with no equipped weapon. "Empty" = no
            # 'weapons' row appears among this node's descendants (the contiguous
            # run of deeper-depth rows that follow it). BUT erkul also renders a
            # mount OCCUPIED by a non-weapon (salvage/mining arm, scanner, sensor,
            # camera, utility cap) under 'gimbal-turret' — those are not gun slots
            # (Reliant utility/camera mounts, Vulture/ROC arms). Same classify-by-
            # installed-item rule as the extractor fix. Item-name distribution was
            # enumerated across all 217 ships to pick these keywords.
            # A mount is a gun slot only if it's genuinely EMPTY (available for a
            # gun). Skip it if a descendant is a real gun ('weapons' — counted via
            # that row) OR a NON-gun that occupies the mount ('tractor-beams',
            # missiles, emps, utilities...). The item-name filter is a backstop, but
            # brand-named tractors like "SureGrip" dodge the word "tractor" — the
            # child-CATEGORY check is what actually catches them (Reliant Kore
            # Remote Turret holds a SureGrip tractor; erkul shows 4 guns, my old
            # ruler wrongly counted 5). Verified by eyeballing live erkul 2026-07-10.
            has_weapon_child = False
            has_nongun_child = False
            for t in slots[i + 1:]:
                if t.get("depth", 0) <= d:
                    break
                tc = str(t.get("category", "")).lower()
                if tc == "weapons":
                    has_weapon_child = True
                elif tc in ("tractor-beams", "missiles", "missile"):
                    # A mount OCCUPIED by a non-gun item (tractor beam OR a
                    # missile rack) is not an empty gun slot. Tractor: Reliant
                    # Kore Remote Turret (SureGrip). Missile: Starlancer TAC has
                    # 2 gimbal mounts loaded with TAC-462 missile racks — erkul
                    # shows 12 guns, not 14. (The earlier "missiles too aggressive"
                    # note predated the extractor turret-shell fix: local read 14
                    # only via a compensating exterior over-count; with that fixed
                    # local is correctly 12 and the arbiter must agree.)
                    has_nongun_child = True
            _nonweapon = ("salvage", "mining", "scanner", "sensor",
                          "camera", "utility", "cap", "tractor")
            if (not has_weapon_child and not has_nongun_child
                    and not any(w in item.lower() for w in _nonweapon)):
                guns += cum
        elif cat == "missile":
            missiles += cum
        elif (cat == "" and in_weapon_zone and d == 0
              and item.strip().lower() == "empty"):
            # unfilled gun hardpoint in the weapon zone (see note above)
            guns += 1
    return guns, missiles


# ---- local calculator side (normalized to leaf gun count) -------------------
def local_counts(loadout):
    guns = extract_slots_by_type(loadout, {"WeaponGun"})
    misl = extract_slots_by_type(loadout, {"MissileLauncher"})
    g = sum(max(int(s.get("gun_count", 1) or 1), 1) for s in guns)
    m = sum(1 for s in misl if s.get("is_missile"))
    return g, m


def load_truth():
    return json.loads(TRUTH.read_text(encoding="utf-8"))["ships"]


def load_cache_ships():
    data = json.loads(CACHE.read_text(encoding="utf-8"))["data"]["/live/ships"]
    return {s.get("localName"): s for s in data}


# erkul_slot_truth keys are display names ("Valkyrie"); cache keys are localNames
# ("anvl_valkyrie"). Match on erkul_name / manufacturer where possible, else by
# a normalized-substring bridge.
def _norm(x):
    return re.sub(r"[^a-z0-9]", "", str(x).lower())


def build_bridge(truth, cache):
    """Map truth-display-name -> cache-localName on the cache's OWN display name
    (data['name']), which is the same human label erkul renders. This is an
    exact normalized join — 216/217 — instead of the old fragile localName
    substring guess (169/217, with false hits like BANU->banu_defender). The one
    holdout, 'BANU', is a truth-side concept row with 0 slots and nothing to audit."""
    by_name = {}
    for ln, s in cache.items():
        nm = (s.get("data") or {}).get("name")
        if nm:
            by_name.setdefault(_norm(nm), ln)
    bridge = {}
    for disp in truth:
        ln = by_name.get(_norm(disp))
        if ln:
            bridge[disp] = ln
    return bridge


def main(argv):
    truth = load_truth()
    cache = load_cache_ships()
    bridge = build_bridge(truth, cache)
    target = next((a for a in argv[1:] if not a.startswith("-")), None)

    if target:
        disp = next((d for d in truth if target.lower() in d.lower()
                     or (bridge.get(d, "").find(target.lower()) >= 0)), None)
        if not disp:
            print("unknown ship in truth set.")
            return
        tg, tm = truth_counts(truth[disp])
        ln = bridge.get(disp)
        print(f"{disp}  (cache: {ln})")
        if ln:
            lo = cache[ln]["data"].get("loadout") or []
            lg, lm = local_counts(lo)
            print(f"  guns     erkul_truth={tg:3d}   local={lg:3d}   {'DIVERGE' if tg!=lg else 'agree'}")
            print(f"  missiles erkul_truth={tm:3d}   local={lm:3d}   {'DIVERGE' if tm!=lm else 'agree'}")
        else:
            print("  (no cache match — cannot run local extractor)")
        return

    rows = []
    unmatched = 0
    for disp, rec in truth.items():
        ln = bridge.get(disp)
        if not ln:
            unmatched += 1
            continue
        try:
            tg, tm = truth_counts(rec)
            lo = cache[ln]["data"].get("loadout") or []
            lg, lm = local_counts(lo)
        except Exception as e:
            rows.append((999, disp, f"ERROR {e}"))
            continue
        d = abs(tg - lg) + abs(tm - lm)
        if d:
            rows.append((d, disp, f"guns erkul={tg} local={lg}   missiles erkul={tm} local={lm}"))
    rows.sort(reverse=True)
    matched = len(truth) - unmatched
    print(f"# LOCAL EXTRACTOR vs ERKUL RENDERED TRUTH (the real arbiter)")
    print(f"# {len(rows)} of {matched} matched ships diverge  "
          f"({unmatched} truth ships had no cache match)\n")
    for d, disp, msg in rows[:45]:
        print(f"  [{d:3d}] {disp:28} {msg}")


if __name__ == "__main__":
    main(sys.argv)
