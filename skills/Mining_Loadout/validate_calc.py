#!/usr/bin/env python3
"""
Mining loadout calc validation script.

Uses the shared calc_stats and fetch_mining_data from the refactored services
instead of duplicating them. Run standalone to verify calculations against
live UEX API data.
"""
import os
import sys
from typing import Dict, List

# Bootstrap project root and skill directory
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.normpath(os.path.join(_SCRIPT_DIR, '..', '..')))
from shared.app_bootstrap import bootstrap_skill  # noqa: E402
bootstrap_skill(__file__)

from models.items import GadgetItem, LaserItem, ModuleItem
from services.api_client import fetch_mining_data
from services.calc_service import calc_stats


# ── Display formatting ────────────────────────────────────────────────────────

def fmt_power(v: float) -> str:
    if v < 1000:
        return str(int(v + 0.5))
    return f"{v:.1f}"


def fmt_pct(v: float) -> str:
    r = round(v)
    if r > 0:
        return f"+{r}%"
    elif r < 0:
        return f"{r}%"
    return "0%"


def print_stats(label: str, stats: Dict[str, float]) -> None:
    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"{'='*70}")
    print(f"  Min Power:     {fmt_power(stats['min_power'])}"
          f"    Max Power: {fmt_power(stats['max_power'])}")
    print(f"  Ext Power:     {fmt_power(stats['ext_power'])}")
    print(f"  Opt Range:     {stats['opt_range']:.0f} m"
          f"    Max Range: {stats['max_range']:.0f} m")
    pct_fields = [
        ("Resistance",    "resistance"),
        ("Instability",   "instability"),
        ("Inert",         "inert"),
        ("Charge Window", "charge_window"),
        ("Charge Rate",   "charge_rate"),
        ("Overcharge",    "overcharge"),
        ("Shatter",       "shatter"),
        ("Cluster",       "cluster"),
    ]
    parts = []
    for disp, key in pct_fields:
        v = stats[key]
        if v != 0:
            parts.append(f"{disp}: {fmt_pct(v)}")
    if parts:
        print("  " + "    ".join(parts))
    else:
        print("  (no % modifiers)")


# ── Lookup helpers ────────────────────────────────────────────────────────────

def find_laser(lasers: List[LaserItem], name: str) -> LaserItem:
    name_l = name.lower()
    for las in lasers:
        if las.name.lower() == name_l:
            return las
    matches = [las for las in lasers if name_l in las.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"Laser not found: {name!r}  (available: {[las.name for las in lasers]})")


def find_module(modules: List[ModuleItem], name: str) -> ModuleItem:
    name_l = name.lower()
    for mod in modules:
        if mod.name.lower() == name_l:
            return mod
    matches = [mod for mod in modules if name_l in mod.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"Module not found: {name!r}  (available: {[mod.name for mod in modules]})")


def find_gadget(gadgets: List[GadgetItem], name: str) -> GadgetItem:
    name_l = name.lower()
    for gad in gadgets:
        if gad.name.lower() == name_l:
            return gad
    matches = [gad for gad in gadgets if name_l in gad.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"Gadget not found: {name!r}  (available: {[gad.name for gad in gadgets]})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    print("Mining Loadout Calc Validator")
    print("=" * 70)

    lasers, modules, gadgets = fetch_mining_data(use_cache=False)

    # Print all available items for reference
    print("AVAILABLE LASERS:")
    for las in sorted(lasers, key=lambda x: x.name):
        print(f"  [{las.id:4d}] {las.name:<40} size={las.size}  min={las.min_power}"
              f"  max={las.max_power}  ext={las.ext_power}  slots={las.module_slots}")

    print("\nAVAILABLE MODULES:")
    for mod in sorted(modules, key=lambda x: x.name):
        print(f"  [{mod.id:4d}] {mod.name:<40} type={mod.item_type:<8}  pwr%={mod.power_pct}"
              f"  ext%={mod.ext_power_pct}  res={mod.resistance}  inst={mod.instability}"
              f"  inert={mod.inert}  cw={mod.charge_window}  cr={mod.charge_rate}"
              f"  oc={mod.overcharge}  shat={mod.shatter}")

    print("\nAVAILABLE GADGETS:")
    for gad in sorted(gadgets, key=lambda x: x.name):
        print(f"  [{gad.id:4d}] {gad.name:<40} cw={gad.charge_window}  cr={gad.charge_rate}"
              f"  inst={gad.instability}  res={gad.resistance}  cluster={gad.cluster}")

    print("\n\n" + "=" * 70)
    print("LOADOUT CALCULATIONS")
    print("=" * 70)

    # ── Lookup items ──────────────────────────────────────────────────────────
    arbor_mh1 = find_laser(lasers, "Arbor MH1 Mining Laser")
    arbor_mh2 = find_laser(lasers, "Arbor MH2 Mining Laser")
    hofstede = find_laser(lasers, "Hofstede-S1 Mining Laser")
    pitman = find_laser(lasers, "Pitman Mining Laser")

    brandt = find_module(modules, "Brandt Module")
    focus3 = find_module(modules, "Focus III Module")
    lifeline = find_module(modules, "Lifeline Module")
    surge = find_module(modules, "Surge Module")
    forel = find_module(modules, "Forel Module")
    stampede = find_module(modules, "Stampede Module")

    print("\nNOTE: 'Gastropod' not found in UEX API. Available gadgets:")
    for gad in gadgets:
        print(f"  {gad.name}: cw={gad.charge_window} cr={gad.charge_rate}"
              f" inst={gad.instability} res={gad.resistance} cluster={gad.cluster}")
    print("Using 'BoreMax' for gadget tests (has cluster + resistance + instability).")
    gastropod = find_gadget(gadgets, "BoreMax")

    # ── PROSPECTOR TESTS ──────────────────────────────────────────────────────
    s = calc_stats("Prospector", [arbor_mh1], [[]], None)
    print_stats("TEST 1 — Prospector: Stock (Arbor MH1, no modules, no gadget)", s)

    s = calc_stats("Prospector", [arbor_mh1], [[brandt]], None)
    print_stats("TEST 2 — Prospector: Arbor MH1 + Brandt (slot 1)", s)

    s = calc_stats("Prospector", [arbor_mh1], [[focus3]], None)
    print_stats("TEST 3 — Prospector: Arbor MH1 + Focus III (slot 1)", s)

    s = calc_stats("Prospector", [arbor_mh1], [[lifeline]], None)
    print_stats("TEST 4 — Prospector: Arbor MH1 + Lifeline (slot 1)", s)

    s = calc_stats("Prospector", [hofstede], [[surge]], None)
    print_stats("TEST 5 — Prospector: Hofstede-S1 + Surge (slot 1)", s)

    # ── MOLE TESTS ────────────────────────────────────────────────────────────
    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2], [[], [], []], None)
    print_stats("TEST 6 — MOLE: Stock (3x Arbor MH2, no modules)", s)

    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2],
                   [[brandt], [brandt], [brandt]], None)
    print_stats("TEST 7 — MOLE: 3x Arbor MH2 + Brandt each", s)

    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2],
                   [[brandt, forel], [brandt, forel], [brandt, forel]], None)
    print_stats("TEST 8 — MOLE: 3x Arbor MH2 + Brandt+Forel each", s)

    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2],
                   [[focus3, surge], [focus3, surge], [focus3, surge]], None)
    print_stats("TEST 9 — MOLE: 3x Arbor MH2 + Focus III+Surge each", s)

    # ── GOLEM TESTS ───────────────────────────────────────────────────────────
    s = calc_stats("Golem", [pitman], [[]], None)
    print_stats("TEST 10 — Golem: Stock (Pitman, no modules)", s)

    s = calc_stats("Golem", [pitman], [[forel, focus3]], None)
    print_stats("TEST 11 — Golem: Pitman + Forel+Focus III", s)

    s = calc_stats("Golem", [pitman], [[surge, brandt]], None)
    print_stats("TEST 12 — Golem: Pitman + Surge+Brandt", s)

    s = calc_stats("Golem", [pitman], [[brandt, stampede]], None)
    print_stats("TEST 13 — Golem: Pitman + Brandt+Stampede", s)

    # ── GADGET TESTS ──────────────────────────────────────────────────────────
    s = calc_stats("Prospector", [arbor_mh1], [[]], gastropod)
    print_stats("TEST 14 — Prospector: Arbor MH1 + Gastropod gadget (no modules)", s)

    s = calc_stats("Prospector", [arbor_mh1], [[brandt]], gastropod)
    print_stats("TEST 15 — Prospector: Arbor MH1 + Brandt + Gastropod gadget", s)

    print("\n\n" + "=" * 70)
    print("ALL TESTS COMPLETE")
    print("=" * 70)


if __name__ == "__main__":
    main()
