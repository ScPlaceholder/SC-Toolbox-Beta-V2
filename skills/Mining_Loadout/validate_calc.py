#!/usr/bin/env python3
"""
Mining loadout calc validation script.
Replicates calc_stats from mining_loadout_app.py and runs test loadouts.
"""
import json
import logging
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from shared.data_utils import retry_request

log = logging.getLogger(__name__)

UEX_BASE = "https://api.uexcorp.space/2.0"


# ── Data models (exact copy from mining_loadout_app.py) ───────────────────────

@dataclass
class LaserItem:
    id:            int
    name:          str
    size:          int
    company:       str
    min_power:     float
    max_power:     float
    ext_power:     Optional[float]
    opt_range:     Optional[float]
    max_range:     Optional[float]
    resistance:    Optional[float]
    instability:   Optional[float]
    inert:         Optional[float]
    charge_window: Optional[float]
    charge_rate:   Optional[float]
    module_slots:  int
    price:         float = 0


@dataclass
class ModuleItem:
    id:            int
    name:          str
    item_type:     str
    power_pct:     Optional[float]
    ext_power_pct: Optional[float]
    resistance:    Optional[float]
    instability:   Optional[float]
    inert:         Optional[float]
    charge_rate:   Optional[float]
    charge_window: Optional[float]
    overcharge:    Optional[float]
    shatter:       Optional[float]
    uses:          int
    duration:      Optional[float]
    price:         float = 0


@dataclass
class GadgetItem:
    id:            int
    name:          str
    charge_window: Optional[float]
    charge_rate:   Optional[float]
    instability:   Optional[float]
    resistance:    Optional[float]
    cluster:       Optional[float]
    price:         float = 0


# ── Calc logic (exact copy from mining_loadout_app.py) ────────────────────────

def _mult_stack(values: List[float]) -> float:
    """Multiplicative stacking: product(1 + v/100) - 1, result in %."""
    result = 1.0
    for v in values:
        if v != 0:
            result *= (1.0 + v / 100.0)
    return (result - 1.0) * 100.0


def calc_stats(
    ship: str,
    laser_items: List[Optional[LaserItem]],
    module_items: List[List[Optional[ModuleItem]]],
    gadget_item: Optional[GadgetItem],
) -> Dict[str, float]:
    """Calculate combined loadout stats using multiplicative stacking."""
    min_pwr = 0.0
    max_pwr = 0.0
    ext_pwr = 0.0
    for i, laser in enumerate(laser_items):
        if not laser:
            continue
        mods = module_items[i] if i < len(module_items) else []
        # power_pct is ×100 (e.g. 135 = +35%). None means "not applicable" (from _float_attr).
        pwr_delta = sum((m.power_pct - 100) / 100.0     for m in mods if m and m.power_pct is not None)
        ext_delta = sum((m.ext_power_pct - 100) / 100.0 for m in mods if m and m.ext_power_pct is not None)
        min_pwr += laser.min_power * (1.0 + pwr_delta)
        max_pwr += laser.max_power * (1.0 + pwr_delta)
        ext_pwr += (laser.ext_power if laser.ext_power is not None else 0.0) * (1.0 + ext_delta)

    first_laser = next((l for l in laser_items if l), None)
    opt_rng = first_laser.opt_range if first_laser and first_laser.opt_range is not None else 0.0
    max_rng = first_laser.max_range if first_laser and first_laser.max_range is not None else 0.0

    resistances   = []
    instabilities = []
    inerts        = []
    chrg_windows  = []
    chrg_rates    = []
    overcharges   = []
    shatters      = []
    clusters      = []

    for laser in laser_items:
        if not laser: continue
        if laser.resistance is not None:    resistances.append(laser.resistance)
        if laser.instability is not None:   instabilities.append(laser.instability)
        if laser.inert is not None:         inerts.append(laser.inert)
        if laser.charge_window is not None: chrg_windows.append(laser.charge_window)
        if laser.charge_rate is not None:   chrg_rates.append(laser.charge_rate)

    for turret_mods in module_items:
        t_res  = sum(m.resistance    for m in turret_mods if m and m.resistance is not None)
        t_inst = sum(m.instability   for m in turret_mods if m and m.instability is not None)
        t_inert= sum(m.inert         for m in turret_mods if m and m.inert is not None)
        t_cw   = sum(m.charge_window for m in turret_mods if m and m.charge_window is not None)
        t_cr   = sum(m.charge_rate   for m in turret_mods if m and m.charge_rate is not None)
        t_oc   = sum(m.overcharge    for m in turret_mods if m and m.overcharge is not None)
        t_shat = sum(m.shatter       for m in turret_mods if m and m.shatter is not None)
        if t_res != 0:   resistances.append(t_res)
        if t_inst != 0:  instabilities.append(t_inst)
        if t_inert != 0: inerts.append(t_inert)
        if t_cw != 0:    chrg_windows.append(t_cw)
        if t_cr != 0:    chrg_rates.append(t_cr)
        if t_oc != 0:    overcharges.append(t_oc)
        if t_shat != 0:  shatters.append(t_shat)

    if gadget_item:
        if gadget_item.resistance is not None:    resistances.append(gadget_item.resistance)
        if gadget_item.instability is not None:   instabilities.append(gadget_item.instability)
        if gadget_item.charge_window is not None: chrg_windows.append(gadget_item.charge_window)
        if gadget_item.charge_rate is not None:   chrg_rates.append(gadget_item.charge_rate)
        if gadget_item.cluster is not None:       clusters.append(gadget_item.cluster)

    return {
        "min_power":     min_pwr,
        "max_power":     max_pwr,
        "ext_power":     ext_pwr,
        "opt_range":     opt_rng,
        "max_range":     max_rng,
        "resistance":    _mult_stack(resistances),
        "instability":   _mult_stack(instabilities),
        "inert":         _mult_stack(inerts),
        "charge_window": _mult_stack(chrg_windows),
        "charge_rate":   _mult_stack(chrg_rates),
        "overcharge":    _mult_stack(overcharges),
        "shatter":       _mult_stack(shatters),
        "cluster":       _mult_stack(clusters),
    }


# ── UEX API fetcher (exact copy from mining_loadout_app.py) ───────────────────

def _uex_get(path: str) -> list:
    url = f"{UEX_BASE}/{path}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "WingmanAI-MiningLoadout/1.0", "Accept": "application/json"},
    )
    def _do_request():
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read()).get("data", [])
    return retry_request(_do_request, retries=1)


def _parse_power(val: str) -> Tuple[float, float]:
    s = str(val).strip() if val else ""
    if not s:
        return 0.0, 0.0
    m = re.match(r'(\d+\.?\d*)\s*-\s*(\d+\.?\d*)', s)
    if m:
        try:
            return float(m.group(1)), float(m.group(2))
        except ValueError as e:
            log.warning("Failed to parse power range %r: %s", s, e)
    try:
        v = float(s)
        return v, v
    except ValueError as e:
        log.warning("Failed to parse power value %r: %s", s, e)
        return 0.0, 0.0


def _float_attr(attrs: Dict[int, Dict[str, str]], iid: int, name: str) -> Optional[float]:
    raw = (attrs.get(iid, {}).get(name) or "").replace("%", "").replace(",", "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _str_attr(attrs: Dict[int, Dict[str, str]], iid: int, name: str) -> str:
    return (attrs.get(iid, {}).get(name) or "").strip()


def fetch_mining_data() -> Tuple[List[LaserItem], List[ModuleItem], List[GadgetItem]]:
    print("Fetching items/id_category/29 (lasers)...")
    raw_lasers  = _uex_get("items/id_category/29")
    print("Fetching items/id_category/30 (modules)...")
    raw_modules = _uex_get("items/id_category/30")
    print("Fetching items/id_category/28 (gadgets)...")
    raw_gadgets = _uex_get("items/id_category/28")

    print("Fetching items_attributes/id_category/29 (laser attrs)...")
    raw_laser_attrs  = _uex_get("items_attributes/id_category/29")
    print("Fetching items_attributes/id_category/30 (module attrs)...")
    raw_module_attrs = _uex_get("items_attributes/id_category/30")
    print("Fetching items_attributes/id_category/28 (gadget attrs)...")
    raw_gadget_attrs = _uex_get("items_attributes/id_category/28")

    print("Fetching items_prices/id_category/29 (laser prices)...")
    raw_laser_prices  = _uex_get("items_prices/id_category/29")
    print("Fetching items_prices/id_category/30 (module prices)...")
    raw_module_prices = _uex_get("items_prices/id_category/30")
    print("Fetching items_prices/id_category/28 (gadget prices)...")
    raw_gadget_prices = _uex_get("items_prices/id_category/28")

    def build_attr_map(raw: list) -> Dict[int, Dict[str, str]]:
        m: Dict[int, Dict[str, str]] = {}
        for a in raw:
            m.setdefault(a.get("id_item", 0), {})[a.get("attribute_name", "")] = (a.get("value") or "")
        return m

    def build_price_map(raw: list) -> Dict[int, float]:
        m: Dict[int, float] = {}
        for p in raw:
            iid = p.get("id_item", 0)
            buy = float(p.get("price_buy") or 0)
            if buy > 0:
                m[iid] = min(m.get(iid, buy), buy)
        return m

    la = build_attr_map(raw_laser_attrs)
    ma = build_attr_map(raw_module_attrs)
    ga = build_attr_map(raw_gadget_attrs)

    lp = build_price_map(raw_laser_prices)
    mp = build_price_map(raw_module_prices)
    gp = build_price_map(raw_gadget_prices)

    lasers: List[LaserItem] = []
    for r in raw_lasers:
        iid = r.get("id")
        if iid is None: continue
        try:
            sz = int(r.get("size") or _str_attr(la, iid, "Size") or 0)
        except Exception:
            sz = 0
        min_p, max_p = _parse_power(_str_attr(la, iid, "Mining Laser Power"))
        lasers.append(LaserItem(
            id           = iid,
            name         = r.get("name", ""),
            size         = sz,
            company      = r.get("company_name", ""),
            min_power    = min_p,
            max_power    = max_p,
            ext_power    = _float_attr(la, iid, "Extraction Laser Power"),
            opt_range    = _float_attr(la, iid, "Optimal Range"),
            max_range    = _float_attr(la, iid, "Maximum Range"),
            resistance   = _float_attr(la, iid, "Resistance"),
            instability  = _float_attr(la, iid, "Laser Instability"),
            inert        = _float_attr(la, iid, "Inert Material Level"),
            charge_window= _float_attr(la, iid, "Optimal Charge Window Size"),
            charge_rate  = _float_attr(la, iid, "Optimal Charge Window Rate"),
            module_slots = int(ms if (ms := _float_attr(la, iid, "Module Slots")) is not None else 2),
            price        = lp.get(iid, 0),
        ))

    modules: List[ModuleItem] = []
    for r in raw_modules:
        iid = r.get("id")
        if iid is None: continue
        modules.append(ModuleItem(
            id           = iid,
            name         = r.get("name", ""),
            item_type    = _str_attr(ma, iid, "Item Type") or "Passive",
            power_pct    = _float_attr(ma, iid, "Mining Laser Power"),
            ext_power_pct= _float_attr(ma, iid, "Extraction Laser Power"),
            resistance   = _float_attr(ma, iid, "Resistance"),
            instability  = _float_attr(ma, iid, "Laser Instability"),
            inert        = _float_attr(ma, iid, "Inert Material Level"),
            charge_rate  = _float_attr(ma, iid, "Optimal Charge Rate"),
            charge_window= _float_attr(ma, iid, "Optimal Charge Window Size"),
            overcharge   = _float_attr(ma, iid, "Catastrophic Charge Rate"),
            shatter      = _float_attr(ma, iid, "Shatter Damage"),
            uses         = int(_float_attr(ma, iid, "Uses") or 0),
            duration     = _float_attr(ma, iid, "Duration"),
            price        = mp.get(iid, 0),
        ))

    gadgets: List[GadgetItem] = []
    for r in raw_gadgets:
        iid = r.get("id")
        if iid is None: continue
        gadgets.append(GadgetItem(
            id           = iid,
            name         = r.get("name", ""),
            charge_window= _float_attr(ga, iid, "Optimal Charge Window Size"),
            charge_rate  = _float_attr(ga, iid, "Optimal Charge Window Rate"),
            instability  = _float_attr(ga, iid, "Laser Instability"),
            resistance   = _float_attr(ga, iid, "Resistance"),
            cluster      = _float_attr(ga, iid, "Cluster Modifier"),
            price        = gp.get(iid, 0),
        ))

    print(f"  => {len(lasers)} lasers, {len(modules)} modules, {len(gadgets)} gadgets\n")
    return lasers, modules, gadgets


# ── Display formatting ────────────────────────────────────────────────────────

def fmt_power(v: float) -> str:
    if v < 1000:
        return str(int(v + 0.5))
    else:
        return f"{v:.1f}"


def fmt_pct(v: float) -> str:
    # Use banker's rounding (round-half-to-even) to match mining_loadout_app.py's f"{v:+.0f}%"
    r = round(v)  # Python's built-in round() uses banker's rounding
    if r > 0:
        return f"+{r}%"
    elif r < 0:
        return f"{r}%"
    else:
        return "0%"


def print_stats(label: str, stats: Dict[str, float]):
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
    for l in lasers:
        if l.name.lower() == name_l:
            return l
    # fuzzy: substring
    matches = [l for l in lasers if name_l in l.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"Laser not found: {name!r}  (available: {[l.name for l in lasers]})")


def find_module(modules: List[ModuleItem], name: str) -> ModuleItem:
    name_l = name.lower()
    for m in modules:
        if m.name.lower() == name_l:
            return m
    matches = [m for m in modules if name_l in m.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"Module not found: {name!r}  (available: {[m.name for m in modules]})")


def find_gadget(gadgets: List[GadgetItem], name: str) -> GadgetItem:
    name_l = name.lower()
    for g in gadgets:
        if g.name.lower() == name_l:
            return g
    matches = [g for g in gadgets if name_l in g.name.lower()]
    if len(matches) == 1:
        return matches[0]
    raise KeyError(f"Gadget not found: {name!r}  (available: {[g.name for g in gadgets]})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Mining Loadout Calc Validator")
    print("="*70)

    lasers, modules, gadgets = fetch_mining_data()

    # Print all available items for reference
    print("AVAILABLE LASERS:")
    for l in sorted(lasers, key=lambda x: x.name):
        print(f"  [{l.id:4d}] {l.name:<40} size={l.size}  min={l.min_power}  max={l.max_power}  ext={l.ext_power}  slots={l.module_slots}")

    print("\nAVAILABLE MODULES:")
    for m in sorted(modules, key=lambda x: x.name):
        print(f"  [{m.id:4d}] {m.name:<40} type={m.item_type:<8}  pwr%={m.power_pct}  ext%={m.ext_power_pct}"
              f"  res={m.resistance}  inst={m.instability}  inert={m.inert}"
              f"  cw={m.charge_window}  cr={m.charge_rate}"
              f"  oc={m.overcharge}  shat={m.shatter}")

    print("\nAVAILABLE GADGETS:")
    for g in sorted(gadgets, key=lambda x: x.name):
        print(f"  [{g.id:4d}] {g.name:<40} cw={g.charge_window}  cr={g.charge_rate}"
              f"  inst={g.instability}  res={g.resistance}  cluster={g.cluster}")

    print("\n\n" + "="*70)
    print("LOADOUT CALCULATIONS")
    print("="*70)

    # ── Lookup items ──────────────────────────────────────────────────────────
    arbor_mh1 = find_laser(lasers, "Arbor MH1 Mining Laser")
    arbor_mh2 = find_laser(lasers, "Arbor MH2 Mining Laser")
    hofstede  = find_laser(lasers, "Hofstede-S1 Mining Laser")
    pitman    = find_laser(lasers, "Pitman Mining Laser")

    brandt    = find_module(modules, "Brandt Module")
    focus3    = find_module(modules, "Focus III Module")
    lifeline  = find_module(modules, "Lifeline Module")
    surge     = find_module(modules, "Surge Module")
    forel     = find_module(modules, "Forel Module")
    stampede  = find_module(modules, "Stampede Module")

    # Gastropod is not in UEX data by that name — use BoreMax (instability reducer + cluster)
    # as representative gadget with cluster modifier; also test Waveshift (charge_window)
    print("\nNOTE: 'Gastropod' not found in UEX API. Available gadgets:")
    for g in gadgets:
        print(f"  {g.name}: cw={g.charge_window} cr={g.charge_rate} inst={g.instability} res={g.resistance} cluster={g.cluster}")
    print("Using 'BoreMax' for gadget tests (has cluster + resistance + instability).")
    gastropod = find_gadget(gadgets, "BoreMax")

    print("\nItem verification:")
    print(f"  Arbor MH1:  min={arbor_mh1.min_power}  max={arbor_mh1.max_power}  ext={arbor_mh1.ext_power}  slots={arbor_mh1.module_slots}")
    print(f"  Arbor MH2:  min={arbor_mh2.min_power}  max={arbor_mh2.max_power}  ext={arbor_mh2.ext_power}  slots={arbor_mh2.module_slots}")
    print(f"  Hofstede:   min={hofstede.min_power}  max={hofstede.max_power}  ext={hofstede.ext_power}  slots={hofstede.module_slots}")
    print(f"  Pitman:     min={pitman.min_power}  max={pitman.max_power}  ext={pitman.ext_power}  slots={pitman.module_slots}")
    print(f"  Brandt:     pwr%={brandt.power_pct}  ext%={brandt.ext_power_pct}  res={brandt.resistance}  inst={brandt.instability}  inert={brandt.inert}  cw={brandt.charge_window}  cr={brandt.charge_rate}  oc={brandt.overcharge}  shat={brandt.shatter}")
    print(f"  Focus III:  pwr%={focus3.power_pct}  ext%={focus3.ext_power_pct}  res={focus3.resistance}  inst={focus3.instability}  inert={focus3.inert}  cw={focus3.charge_window}  cr={focus3.charge_rate}  oc={focus3.overcharge}  shat={focus3.shatter}")
    print(f"  Lifeline:   pwr%={lifeline.power_pct}  ext%={lifeline.ext_power_pct}  res={lifeline.resistance}  inst={lifeline.instability}  inert={lifeline.inert}  cw={lifeline.charge_window}  cr={lifeline.charge_rate}  oc={lifeline.overcharge}  shat={lifeline.shatter}")
    print(f"  Surge:      pwr%={surge.power_pct}  ext%={surge.ext_power_pct}  res={surge.resistance}  inst={surge.instability}  inert={surge.inert}  cw={surge.charge_window}  cr={surge.charge_rate}  oc={surge.overcharge}  shat={surge.shatter}")
    print(f"  Forel:      pwr%={forel.power_pct}  ext%={forel.ext_power_pct}  res={forel.resistance}  inst={forel.instability}  inert={forel.inert}  cw={forel.charge_window}  cr={forel.charge_rate}  oc={forel.overcharge}  shat={forel.shatter}")
    print(f"  Stampede:   pwr%={stampede.power_pct}  ext%={stampede.ext_power_pct}  res={stampede.resistance}  inst={stampede.instability}  inert={stampede.inert}  cw={stampede.charge_window}  cr={stampede.charge_rate}  oc={stampede.overcharge}  shat={stampede.shatter}")
    print(f"  Gastropod:  cw={gastropod.charge_window}  cr={gastropod.charge_rate}  inst={gastropod.instability}  res={gastropod.resistance}  cluster={gastropod.cluster}")

    # ── PROSPECTOR TESTS ──────────────────────────────────────────────────────

    # 1. Stock Prospector (Arbor MH1, no modules, no gadget)
    s = calc_stats("Prospector", [arbor_mh1], [[]], None)
    print_stats("TEST 1 — Prospector: Stock (Arbor MH1, no modules, no gadget)", s)

    # 2. Arbor MH1 + Brandt (slot 1)
    s = calc_stats("Prospector", [arbor_mh1], [[brandt]], None)
    print_stats("TEST 2 — Prospector: Arbor MH1 + Brandt (slot 1)", s)

    # 3. Arbor MH1 + Focus III (slot 1) — only 1 slot used
    s = calc_stats("Prospector", [arbor_mh1], [[focus3]], None)
    print_stats("TEST 3 — Prospector: Arbor MH1 + Focus III (slot 1)", s)

    # 4. Arbor MH1 + Lifeline (slot 1)
    s = calc_stats("Prospector", [arbor_mh1], [[lifeline]], None)
    print_stats("TEST 4 — Prospector: Arbor MH1 + Lifeline (slot 1)", s)

    # 5. Hofstede-S1 + Surge (slot 1)
    s = calc_stats("Prospector", [hofstede], [[surge]], None)
    print_stats("TEST 5 — Prospector: Hofstede-S1 + Surge (slot 1)", s)

    # ── MOLE TESTS ────────────────────────────────────────────────────────────

    # 6. Stock MOLE (3x Arbor MH2, no modules)
    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2], [[], [], []], None)
    print_stats("TEST 6 — MOLE: Stock (3x Arbor MH2, no modules)", s)

    # 7. 3x Arbor MH2, each with Brandt in slot 1
    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2],
                   [[brandt], [brandt], [brandt]], None)
    print_stats("TEST 7 — MOLE: 3x Arbor MH2 + Brandt each", s)

    # 8. 3x Arbor MH2, each with Brandt slot 1 + Forel slot 2
    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2],
                   [[brandt, forel], [brandt, forel], [brandt, forel]], None)
    print_stats("TEST 8 — MOLE: 3x Arbor MH2 + Brandt+Forel each", s)

    # 9. 3x Arbor MH2, each with Focus III slot 1 + Surge slot 2
    s = calc_stats("MOLE", [arbor_mh2, arbor_mh2, arbor_mh2],
                   [[focus3, surge], [focus3, surge], [focus3, surge]], None)
    print_stats("TEST 9 — MOLE: 3x Arbor MH2 + Focus III+Surge each", s)

    # ── GOLEM TESTS ───────────────────────────────────────────────────────────

    # 10. Stock Golem (Pitman, no modules)
    s = calc_stats("Golem", [pitman], [[]], None)
    print_stats("TEST 10 — Golem: Stock (Pitman, no modules)", s)

    # 11. Pitman + Forel (slot 1) + Focus III (slot 2)
    s = calc_stats("Golem", [pitman], [[forel, focus3]], None)
    print_stats("TEST 11 — Golem: Pitman + Forel+Focus III", s)

    # 12. Pitman + Surge (slot 1) + Brandt (slot 2)
    s = calc_stats("Golem", [pitman], [[surge, brandt]], None)
    print_stats("TEST 12 — Golem: Pitman + Surge+Brandt", s)

    # 13. Pitman + Brandt (slot 1) + Stampede (slot 2)
    s = calc_stats("Golem", [pitman], [[brandt, stampede]], None)
    print_stats("TEST 13 — Golem: Pitman + Brandt+Stampede", s)

    # ── GADGET TESTS ──────────────────────────────────────────────────────────

    # 14. Arbor MH1 + Gastropod gadget (no modules)
    s = calc_stats("Prospector", [arbor_mh1], [[]], gastropod)
    print_stats("TEST 14 — Prospector: Arbor MH1 + Gastropod gadget (no modules)", s)

    # 15. Arbor MH1 + Brandt (slot 1) + Gastropod gadget
    s = calc_stats("Prospector", [arbor_mh1], [[brandt]], gastropod)
    print_stats("TEST 15 — Prospector: Arbor MH1 + Brandt + Gastropod gadget", s)

    print("\n\n" + "="*70)
    print("ALL TESTS COMPLETE")
    print("="*70)


if __name__ == "__main__":
    main()
