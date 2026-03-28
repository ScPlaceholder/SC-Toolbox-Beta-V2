"""Pure-logic power allocation engine -- no tkinter dependency.

Rewritten to exactly match erkul.games' power allocator formulas,
reverse-engineered from main.df4e6a453b05ea42.js.
"""

from __future__ import annotations
import math


class PowerAllocatorEngine:
    """Headless power-allocation engine matching erkul.games' power widget.

    Computes stacked pip bars for each powered component category,
    consumption, signature readouts, and SCM/NAV mode toggle — without
    any UI.
    """

    CATEGORY_ORDER = [
        ("weaponGun",    "WPN",  "\u2694",  "#ff7733"),
        ("thruster",     "THR",  "\u2708",  "#33ccdd"),
        ("shield",       "SHD",  "\U0001f6e1", "#aa66ff"),
        ("radar",        "RDR",  "\U0001f4e1", "#5a6480"),
        ("lifeSupport",  "LSP",  "\u2764",  "#5a9a70"),
        ("cooler",       "CLR",  "\u2744",  "#33ccdd"),
        ("quantumDrive", "QDR",  "\U0001f680", "#44aaff"),
        ("utility",      "UTL",  "\U0001f527", "#ffaa22"),
    ]

    def __init__(self, item_lookup_fn, raw_lookup_fn=None):
        self._lookup = item_lookup_fn
        self._lookup_raw = raw_lookup_fn or (lambda ln: None)
        self._mode = "SCM"

        # Legacy compat: _slots and _categories for UI/audit access
        self._slots: list[dict] = []
        self._categories: dict[str, list[dict]] = {}

        # Erkul-style segment config: category -> list of pip dicts
        # Each pip: {number: int, selected: bool, disabled: bool, index?: int, critical?: bool}
        self._seg_config: dict[str, list] = {}

        # Erkul-style power config: category -> {power: bool, segment: int, ...}
        self._power_config: dict = {}

        # Signature / ratio attributes
        self.em_signature = 0.0
        self.ir_signature = 0.0
        self.cs_signature = 0.0
        self.weapon_power_ratio = 1.0
        self.shield_power_ratio = 1.0

        # Armor signature multipliers (set by load_ship, default to neutral)
        self._armor_sig_em = 1.0
        self._armor_sig_ir = 1.0
        self._armor_sig_cs = 1.0
        self._ship_cs = 0.0
        self._pp_count = 0

        # Raw component data extracted during load_ship
        self._components: dict[str, list] = {}
        self._ship_data = None

    @property
    def slots(self) -> list:
        return self._slots

    @property
    def categories(self) -> dict:
        return self._categories

    @property
    def mode(self) -> str:
        return self._mode

    # ── public API ───────────────────────────────────────────────────────────

    def load_ship(self, ship_data: dict):
        """Build power configuration from ship data using erkul-exact formulas."""
        self._ship_data = dict(ship_data) if isinstance(ship_data, dict) else {}
        self._slots.clear()
        self._categories.clear()
        self._seg_config.clear()

        if not self._ship_data:
            self.recalculate()
            return

        loadout = self._ship_data.get("loadout", [])
        if not isinstance(loadout, list):
            loadout = []

        # Store armor signal multipliers
        armor_d = self._ship_data.get("armor", {})
        if isinstance(armor_d, dict):
            armor_d = armor_d.get("data", armor_d)
        arm = armor_d.get("armor", {}) if isinstance(armor_d, dict) else {}
        self._armor_sig_em = float(arm.get("signalElectromagnetic", 1) or 1)
        self._armor_sig_ir = float(arm.get("signalInfrared", 1) or 1)
        self._armor_sig_cs = float(arm.get("signalCrossSection", 1) or 1)

        # Store cross-section
        cs_raw = self._ship_data.get("crossSection", 0)
        if isinstance(cs_raw, dict):
            self._ship_cs = max(float(cs_raw.get("x", 0) or 0),
                                float(cs_raw.get("y", 0) or 0),
                                float(cs_raw.get("z", 0) or 0))
        elif isinstance(cs_raw, (int, float)):
            self._ship_cs = float(cs_raw)
        else:
            self._ship_cs = 0

        # ── Extract components from loadout ──
        self._components = {
            "powerPlants": [], "weapons": [], "shields": [], "coolers": [],
            "radars": [], "qdrives": [], "lifeSupports": [], "thrusters": [],
        }

        _SKIP_SUBSTRINGS = ("blanking", "blade_rack", "missilerack_blade",
                            "missile_cap", "fuel_intake", "intk_", "_remote_top_turret")

        def _find_nested_weapons(port):
            """Recursively find actual WeaponGun items in nested loadout."""
            found = []
            for child in port.get("loadout", []):
                child_ln = child.get("localName", "")
                child_lr = child.get("localReference", "")
                child_ident = child_ln or child_lr
                if child_ident:
                    raw = self._lookup_raw(child_ident)
                    if raw and raw.get("type") == "WeaponGun":
                        found.append(raw)
                        continue
                # Recurse deeper
                found.extend(_find_nested_weapons(child))
            return found

        def _walk(ports):
            for port in (ports if isinstance(ports, list) else []):
                itypes = [it.get("type", "") for it in port.get("itemTypes", [])]
                ln = port.get("localName", "")
                lr = port.get("localReference", "")
                ident = ln or lr

                if ident and any(s in ident.lower() for s in _SKIP_SUBSTRINGS):
                    _walk(port.get("loadout", []))
                    continue

                matched = False
                for pt in itypes:
                    if pt == "PowerPlant" and ident:
                        raw = self._lookup_raw(ident)
                        if raw:
                            self._components["powerPlants"].append(raw)
                        matched = True
                        break
                    elif pt == "Shield" and ident:
                        raw = self._lookup_raw(ident)
                        if raw:
                            self._components["shields"].append(raw)
                        matched = True
                        break
                    elif pt == "Cooler" and ident:
                        raw = self._lookup_raw(ident)
                        if raw:
                            self._components["coolers"].append(raw)
                        matched = True
                        break
                    elif pt == "Radar" and ident:
                        raw = self._lookup_raw(ident)
                        if raw:
                            self._components["radars"].append(raw)
                        matched = True
                        break
                    elif pt == "QuantumDrive" and ident:
                        raw = self._lookup_raw(ident)
                        if raw:
                            self._components["qdrives"].append(raw)
                        matched = True
                        break
                    elif pt == "LifeSupportGenerator" and ident:
                        raw = self._lookup_raw(ident)
                        if raw:
                            self._components["lifeSupports"].append(raw)
                        matched = True
                        break
                    elif pt in ("WeaponGun", "Turret") and ident:
                        # Try direct lookup first
                        raw = self._lookup_raw(ident)
                        if raw and raw.get("type") == "WeaponGun":
                            self._components["weapons"].append(raw)
                        else:
                            # Weapons are often nested: port -> gimbal -> weapon
                            nested = _find_nested_weapons(port)
                            self._components["weapons"].extend(nested)
                        matched = True
                        break

                if not matched:
                    _walk(port.get("loadout", []))
        _walk(loadout)

        # Also check items section for life support
        items_dict = self._ship_data.get("items", {})
        if isinstance(items_dict, dict):
            for item in items_dict.get("lifeSupports", []):
                if isinstance(item, dict):
                    idata = item.get("data", {})
                    if idata.get("type") == "LifeSupportGenerator":
                        if idata not in self._components["lifeSupports"]:
                            self._components["lifeSupports"].append(idata)

        # ── 1. PP Output (erkul-exact formula) ──
        pps = self._components["powerPlants"]
        num_pps = len(pps)
        if num_pps > 0:
            rounded_seg_sum = 0
            total_size = 0
            for pp in pps:
                res = pp.get("resource", {}).get("online", {})
                gen = res.get("generation", {})
                seg = float(gen.get("powerSegment", 0) or 0)
                sz = float(pp.get("size", 0) or 0)
                rounded_seg_sum += round(seg / num_pps)
                total_size += sz
            self._total_pp_output = rounded_seg_sum + (num_pps - 1) * total_size
        else:
            self._total_pp_output = 0
        self._pp_count = num_pps

        # Store PP EM/power ranges for signature calculations
        self._pp_data = pps

        # ── 2. Build segment configurations ──

        # Weapon segments: fixed pool from rnPowerPools
        rn_pools = self._ship_data.get("rnPowerPools", {})
        wpn_pool = rn_pools.get("weaponGun", {})
        wpn_pool_size = wpn_pool.get("poolSize", 0) or 0

        # Total weapon power consumption
        wpn_consumption = 0
        for w in self._components["weapons"]:
            res = w.get("resource", {}).get("online", {})
            wpn_consumption += float(res.get("consumption", {}).get("power", 0) or 0)
        wpn_consumption = math.ceil(wpn_consumption)

        self._seg_config["weapon"] = []
        for i in range(wpn_pool_size):
            self._seg_config["weapon"].append({
                "number": 1, "selected": False,
                "disabled": i >= wpn_consumption
            })

        # Engine segments
        ifcs = self._ship_data.get("ifcs", {})
        ifcs_res = ifcs.get("resource", {}).get("online", {}) if isinstance(ifcs.get("resource"), dict) else {}
        engine_seg = int(ifcs_res.get("consumption", {}).get("powerSegment", 0) or 0) if isinstance(ifcs_res.get("consumption"), dict) else 0
        engine_min_frac = float(ifcs_res.get("powerConsumptionMinimumFraction", 1) or 1)

        # Check for WheeledController fallback
        if not engine_seg:
            for ctrl in items_dict.get("controllers", []) if isinstance(items_dict, dict) else []:
                if isinstance(ctrl, dict):
                    cd = ctrl.get("data", {})
                    if cd.get("type") == "WheeledController":
                        res = cd.get("resource", {}).get("online", {})
                        engine_seg = int(res.get("consumption", {}).get("powerSegment", 0) or 0)
                        engine_min_frac = float(res.get("powerConsumptionMinimumFraction", 1) or 1)
                        break

        critical_engine = round(engine_seg * engine_min_frac)
        self._seg_config["engine"] = []
        if engine_seg > 0:
            self._seg_config["engine"].append({"number": critical_engine, "selected": False, "disabled": False})
            for _ in range(engine_seg - critical_engine):
                self._seg_config["engine"].append({"number": 1, "selected": False, "disabled": False})

        # Generic component setup helper
        def _setup_component(comp_list, key, use_conversion_frac=False):
            self._seg_config[key] = []
            for comp in comp_list:
                res = comp.get("resource", {}).get("online", {})
                cons = res.get("consumption", {})
                total_seg = int(cons.get("powerSegment", cons.get("power", 0)) or 0)
                if total_seg <= 0:
                    continue

                if use_conversion_frac:
                    min_frac = float(res.get("conversionMinimumFraction", 0) or 0)
                    if not min_frac:
                        min_frac = 1.0 / (total_seg or 1)
                else:
                    min_frac = float(res.get("powerConsumptionMinimumFraction", 1) or 1)

                critical = round(total_seg * min_frac)
                pips = [{"number": critical, "selected": False, "disabled": False}]
                for _ in range(total_seg - critical):
                    pips.append({"number": 1, "selected": False, "disabled": False})
                # Sort by number descending
                pips.sort(key=lambda p: p["number"], reverse=True)
                self._seg_config[key].extend(pips)

        # QDrive
        _setup_component(self._components["qdrives"], "qdrive")
        # Radar
        _setup_component(self._components["radars"], "radar")
        # Life Support
        _setup_component(self._components["lifeSupports"], "lifeSupport", use_conversion_frac=True)

        # Coolers: erkul stores as array of arrays (one per cooler)
        self._seg_config["coolers"] = []
        for cooler in self._components["coolers"]:
            res = cooler.get("resource", {}).get("online", {})
            cons = res.get("consumption", {})
            total_seg = int(cons.get("powerSegment", 0) or 0)
            if total_seg <= 0:
                continue
            min_frac = float(res.get("conversionMinimumFraction", 0) or 0)
            if not min_frac:
                min_frac = 1.0 / (total_seg or 1)
            critical = round(total_seg * min_frac)
            pips = [{"number": critical, "selected": False, "disabled": False}]
            for _ in range(total_seg - critical):
                pips.append({"number": 1, "selected": False, "disabled": False})
            pips.sort(key=lambda p: p["number"], reverse=True)
            self._seg_config["coolers"].append(pips)

        # Shields: erkul-exact init with per-shield indexing
        self._seg_config["shield"] = []
        shields = self._components["shields"]
        # If >2 non-empty shields, turn off shields after index 1
        # (erkul behavior for ships with 3+ shields)
        powered_on = list(range(len(shields)))
        if len(shields) > 2:
            powered_on = [0, 1]

        for shield_idx in powered_on:
            shield = shields[shield_idx]
            res = shield.get("resource", {}).get("online", {})
            cons = res.get("consumption", {})
            total_seg = int(cons.get("powerSegment", 0) or 0)
            if total_seg <= 0:
                continue
            min_frac = float(res.get("conversionMinimumFraction", 0) or 0)
            if not min_frac:
                min_frac = 1.0 / (total_seg or 1)
            critical = round(total_seg * min_frac)
            self._seg_config["shield"].append({
                "number": critical, "selected": False, "disabled": False,
                "index": shield_idx, "critical": True
            })
            for _ in range(total_seg - critical):
                self._seg_config["shield"].append({
                    "number": 1, "selected": False, "disabled": False,
                    "index": shield_idx, "critical": False
                })

        # Powered-off shields: disabled pips
        for shield_idx in range(len(shields)):
            if shield_idx not in powered_on:
                shield = shields[shield_idx]
                res = shield.get("resource", {}).get("online", {})
                total_seg = int(res.get("consumption", {}).get("powerSegment", 0) or 0)
                for _ in range(total_seg):
                    self._seg_config["shield"].append({
                        "number": 1, "selected": False, "disabled": True,
                        "index": shield_idx, "critical": False
                    })

        # Sort shield pips: critical first, then by size desc, enabled before disabled
        self._seg_config["shield"].sort(
            key=lambda p: (-int(p.get("critical", False)), -p["number"],
                           int(p.get("disabled", False)), p.get("index", 0))
        )

        # Initialize power config
        is_scm = self._mode == "SCM"
        self._power_config = {
            "totalAvailablePowerSegments": self._total_pp_output,
            "weapon": {"power": is_scm, "segment": 0, "usage": 0},
            "engine": {"power": True, "segment": 0},
            "shield": {"power": is_scm, "segment": 0},
            "qdrive": {"power": not is_scm, "segment": 0},
            "lifeSupport": {"power": True, "segment": 0},
            "radar": {"power": True, "segment": 0},
            "coolers": [{"power": True, "segment": 0, "coolingGeneration": 0}
                        for _ in self._seg_config.get("coolers", [])],
            "coolingGeneration": 0,
            "maxCoolingGeneration": 0,
            "coolingConsumption": 0,
            # Placeholder categories erkul has but we may not populate
            "qed": {"power": False, "segment": 0},
            "emp": {"power": False, "segment": 0},
            "miningLaser": {"power": False, "segment": 0},
            "salvage": {"power": False, "segment": 0},
            "tractorBeam": {"power": False, "segment": 0},
        }

        # ── 3. Default pip allocation (erkul two-phase greedy) ──
        self._init_segments_distribution()

        # ── 4. Build legacy _slots/_categories for UI compatibility ──
        self._build_legacy_slots()

        self.recalculate()

    def _get_empty_segments(self) -> int:
        """Return remaining available power segments."""
        used = 0
        pc = self._power_config
        for key in ("weapon", "engine", "shield", "qdrive", "lifeSupport",
                     "radar", "qed", "emp", "miningLaser", "salvage", "tractorBeam"):
            cfg = pc.get(key, {})
            if cfg.get("power"):
                used += cfg.get("segment", 0)
        for c in pc.get("coolers", []):
            if c.get("power"):
                used += c.get("segment", 0)
        return pc.get("totalAvailablePowerSegments", 0) - used

    def _init_segments_distribution(self):
        """Erkul-exact two-phase greedy pip allocation."""

        def add_segment(category, seg_count, cooler_idx=None):
            cfg = self._power_config.get(category)
            if cfg is None:
                return
            if category == "coolers":
                coolers = self._power_config.get("coolers", [])
                if cooler_idx is not None and cooler_idx < len(coolers):
                    if self._get_empty_segments() >= seg_count:
                        coolers[cooler_idx]["segment"] += seg_count
            else:
                if self._get_empty_segments() >= seg_count:
                    cfg["segment"] += seg_count

        def select_first(category, segments, critical_only=False, cooler_idx=None):
            if critical_only and segments:
                # Select ALL critical non-disabled pips (for shields)
                for pip in segments:
                    if pip.get("critical") and not pip.get("disabled"):
                        if not pip["selected"]:
                            pip["selected"] = True
                            add_segment(category, pip["number"], cooler_idx)
            elif segments:
                # Select the FIRST non-disabled pip
                for pip in segments:
                    if not pip.get("disabled"):
                        pip["selected"] = True
                        add_segment(category, pip["number"], cooler_idx)
                        break

        def fill_remaining(category, segments, cooler_idx=None):
            while True:
                available = [p for p in segments if not p["selected"] and not p.get("disabled")]
                if not available:
                    break
                empty = self._get_empty_segments()
                if empty <= 0:
                    break
                nxt = available[0]
                if nxt["number"] > empty:
                    break
                nxt["selected"] = True
                add_segment(category, nxt["number"], cooler_idx)

        # PHASE 1: Select first/critical pip for each category
        for category in self._seg_config:
            if category == "coolers":
                for idx, cooler_pips in enumerate(self._seg_config["coolers"]):
                    select_first("coolers", cooler_pips, cooler_idx=idx)
            elif category == "shield":
                if self._mode == "SCM":
                    select_first("shield", self._seg_config["shield"], critical_only=True)
            elif category == "qdrive":
                if self._mode == "NAV":
                    select_first("qdrive", self._seg_config["qdrive"])
            elif category == "tractorBeam":
                continue  # skip tractor beam in phase 1
            else:
                select_first(category, self._seg_config.get(category, []))

        # PHASE 2: Fill remaining in priority order
        if self._mode == "SCM":
            fill_order = ["coolers", "lifeSupport", "miningLaser", "weapon",
                          "shield", "engine", "radar", "emp", "qed", "salvage"]
        else:
            fill_order = ["coolers", "lifeSupport", "qdrive", "miningLaser",
                          "engine", "radar", "salvage", "weapon", "emp", "qed"]

        for category in fill_order:
            if category == "coolers":
                for idx, cooler_pips in enumerate(self._seg_config.get("coolers", [])):
                    fill_remaining("coolers", cooler_pips, cooler_idx=idx)
            elif category in self._seg_config:
                fill_remaining(category, self._seg_config[category])

    def _build_legacy_slots(self):
        """Build _slots and _categories from seg_config for UI/audit compat."""
        self._slots.clear()
        self._categories.clear()
        slot_id = 0

        cat_map = {
            "weapon": "weaponGun", "engine": "thruster",
            "shield": "shield", "radar": "radar",
            "lifeSupport": "lifeSupport", "qdrive": "quantumDrive",
        }

        for seg_key, cat_key in cat_map.items():
            pips = self._seg_config.get(seg_key, [])
            if not pips:
                continue
            total_pips = sum(p["number"] for p in pips)
            selected_pips = sum(p["number"] for p in pips if p["selected"])
            cfg = self._power_config.get(seg_key, {})
            enabled = cfg.get("power", True)

            slot = {
                "id": f"slot_{slot_id}",
                "name": seg_key.title(),
                "category": cat_key,
                "max_segments": total_pips,
                "default_seg": selected_pips,
                "current_seg": selected_pips,
                "enabled": enabled,
                "draw_per_seg": 1.0,
                "em_per_seg": 0, "ir_per_seg": 0,
                "em_total": 0, "ir_total": 0,
                "cooling_gen": 0, "power_ranges": None,
                "is_generator": False, "output": 0,
            }
            self._slots.append(slot)
            self._categories.setdefault(cat_key, []).append(slot)
            slot_id += 1

        # Coolers: one slot per cooler
        for idx, cooler_pips in enumerate(self._seg_config.get("coolers", [])):
            total_pips = sum(p["number"] for p in cooler_pips)
            selected_pips = sum(p["number"] for p in cooler_pips if p["selected"])
            cfg = self._power_config.get("coolers", [{}])[idx] if idx < len(self._power_config.get("coolers", [])) else {}

            name = "Cooler"
            if idx < len(self._components.get("coolers", [])):
                name = self._components["coolers"][idx].get("name", "Cooler")

            slot = {
                "id": f"slot_{slot_id}",
                "name": name,
                "category": "cooler",
                "max_segments": total_pips,
                "default_seg": selected_pips,
                "current_seg": selected_pips,
                "enabled": cfg.get("power", True),
                "draw_per_seg": 1.0,
                "em_per_seg": 0, "ir_per_seg": 0,
                "em_total": 0, "ir_total": 0,
                "cooling_gen": 0, "power_ranges": None,
                "is_generator": False, "output": 0,
            }
            self._slots.append(slot)
            self._categories.setdefault("cooler", []).append(slot)
            slot_id += 1

    # Map UI category keys to seg_config keys
    _CAT_TO_SEG = {
        "weaponGun": "weapon", "thruster": "engine", "shield": "shield",
        "radar": "radar", "lifeSupport": "lifeSupport",
        "quantumDrive": "qdrive", "cooler": "coolers",
    }

    def set_level_by_type(self, category: str, slot_idx: int, level: int):
        """Set a slot's pip level by category key and index."""
        slots = self._categories.get(category, [])
        if 0 <= slot_idx < len(slots):
            s = slots[slot_idx]
            s["current_seg"] = max(0, min(level, s["max_segments"]))
            self.sync_seg_config_from_slots()
            self.recalculate()

    def toggle_by_type(self, category: str, slot_idx: int):
        slots = self._categories.get(category, [])
        if 0 <= slot_idx < len(slots):
            s = slots[slot_idx]
            s["enabled"] = not s["enabled"]
            if not s["enabled"]:
                s["current_seg"] = 0
            else:
                s["current_seg"] = s["default_seg"]
            self.sync_seg_config_from_slots()
            self.recalculate()

    def set_mode(self, mode: str):
        mode = mode.upper()
        if mode not in ("SCM", "NAV") or mode == self._mode:
            return
        self._mode = mode
        if self._ship_data:
            self.load_ship(self._ship_data)

    def sync_seg_config_from_slots(self):
        """Sync _seg_config and _power_config from legacy _slots state.

        Called after UI changes pip levels or toggles categories.
        Reads current_seg and enabled from each slot and updates the
        corresponding seg_config pips and power_config entries.
        """
        for slot in self._slots:
            cat = slot["category"]
            seg_key = self._CAT_TO_SEG.get(cat)
            if not seg_key:
                continue

            enabled = slot["enabled"]
            current = slot["current_seg"]
            max_seg = slot["max_segments"]

            if seg_key == "coolers":
                # Find which cooler index this slot corresponds to
                cooler_slots = self._categories.get("cooler", [])
                try:
                    cidx = cooler_slots.index(slot)
                except ValueError:
                    continue
                cooler_pips = self._seg_config.get("coolers", [])
                if cidx < len(cooler_pips):
                    pips = cooler_pips[cidx]
                    # Deselect all, then select first N pips up to current
                    remaining = current
                    for p in pips:
                        if remaining >= p["number"] and not p.get("disabled"):
                            p["selected"] = True
                            remaining -= p["number"]
                        else:
                            p["selected"] = False
                    configs = self._power_config.get("coolers", [])
                    if cidx < len(configs):
                        configs[cidx]["power"] = enabled
                        configs[cidx]["segment"] = current
            else:
                # For non-cooler categories (shield, weapon, etc.)
                # Select first N pips matching current segment level
                pips = self._seg_config.get(seg_key, [])
                remaining = current
                for p in pips:
                    if remaining >= p["number"] and not p.get("disabled"):
                        p["selected"] = True
                        remaining -= p["number"]
                    else:
                        p["selected"] = False

                cfg = self._power_config.get(seg_key, {})
                if isinstance(cfg, dict):
                    cfg["power"] = enabled
                    cfg["segment"] = current

    # ── recalculate ──────────────────────────────────────────────────────────

    @staticmethod
    def _find_range_modifier(power_ranges, value):
        """Erkul findRangeObject: find modifier for a given pip count."""
        if not power_ranges:
            return 1.0
        ranges = []
        if isinstance(power_ranges, dict):
            for rk in ("low", "medium", "high"):
                rd = power_ranges.get(rk, {})
                if rd:
                    ranges.append(rd)
        elif isinstance(power_ranges, list):
            ranges = power_ranges
        if not ranges:
            return 1.0

        for i, rng in enumerate(ranges):
            start = rng.get("start", 0) or 0
            next_start = ranges[i + 1].get("start", 0) if i + 1 < len(ranges) else float("inf")
            if value >= start and value < next_start:
                return rng.get("modifier", 1.0) or 1.0
        return 1.0

    def _get_pp_usage_ratio(self) -> float:
        """Erkul getPowerPlantUsageRatio."""
        total_used = 0
        pc = self._power_config

        # Non-weapon categories
        for c in pc.get("coolers", []):
            if c.get("power"):
                total_used += c.get("segment", 0)
        for key in ("engine", "lifeSupport", "qdrive", "radar", "shield",
                     "qed", "emp", "miningLaser", "salvage", "tractorBeam"):
            cfg = pc.get(key, {})
            if cfg.get("power"):
                total_used += cfg.get("segment", 0)

        # Weapons: min(selectedPips, actualConsumption)
        wpn_selected = sum(p["number"] for p in self._seg_config.get("weapon", []) if p["selected"])
        wpn_actual = sum(
            float((w.get("resource", {}).get("online", {}).get("consumption", {}).get("power", 0)) or 0)
            for w in self._components.get("weapons", [])
        )
        total_used += min(wpn_selected, wpn_actual)

        total_available = pc.get("totalAvailablePowerSegments", 0)
        return total_used / total_available if total_available > 0 else 0

    def _compute_cooling_generation(self):
        """Erkul getCoolingGeneration."""
        total_gen = 0
        max_gen = 0
        coolers = self._components.get("coolers", [])

        for idx, cooler in enumerate(coolers):
            cooler_max = float(cooler.get("resource", {}).get("online", {}).get("generation", {}).get("cooling", 0) or 0)
            max_gen += cooler_max

            cooler_configs = self._power_config.get("coolers", [])
            if idx < len(cooler_configs) and cooler_configs[idx].get("power"):
                allocated = cooler_configs[idx].get("segment", 0)
                total_seg = int(cooler.get("resource", {}).get("online", {}).get("consumption", {}).get("powerSegment", 0) or 0)

                modifier = 1.0
                pr = cooler.get("resource", {}).get("online", {}).get("powerRanges")
                if pr:
                    modifier = self._find_range_modifier(pr, allocated)

                seg_ratio_gen = (allocated / total_seg * cooler_max) if total_seg > 0 else 0
                cooler_configs[idx]["coolingGeneration"] = seg_ratio_gen * modifier
                total_gen += seg_ratio_gen * modifier
            elif idx < len(cooler_configs):
                cooler_configs[idx]["coolingGeneration"] = 0

        self._power_config["coolingGeneration"] = total_gen
        self._power_config["maxCoolingGeneration"] = max_gen

    def _compute_cooling_consumption(self):
        """Erkul getCoolingConsumption."""
        pc = self._power_config

        # Base = sum of ALL allocated segments
        wpn_selected = sum(p["number"] for p in self._seg_config.get("weapon", []) if p["selected"])
        wpn_actual = sum(
            float((w.get("resource", {}).get("online", {}).get("consumption", {}).get("power", 0)) or 0)
            for w in self._components.get("weapons", [])
        )
        wpn_contrib = min(wpn_selected, wpn_actual)

        base = (sum(c.get("segment", 0) for c in pc.get("coolers", []))
                + pc.get("engine", {}).get("segment", 0)
                + pc.get("lifeSupport", {}).get("segment", 0)
                + pc.get("miningLaser", {}).get("segment", 0)
                + pc.get("qdrive", {}).get("segment", 0)
                + pc.get("qed", {}).get("segment", 0)
                + pc.get("emp", {}).get("segment", 0)
                + pc.get("radar", {}).get("segment", 0)
                + pc.get("salvage", {}).get("segment", 0)
                + pc.get("shield", {}).get("segment", 0)
                + pc.get("tractorBeam", {}).get("segment", 0)
                + wpn_contrib)

        cooling_cons = base

        # Shield additional cooling
        for shield_idx, shield in enumerate(self._components.get("shields", [])):
            selected = sum(p["number"] for p in self._seg_config.get("shield", [])
                           if p.get("index") == shield_idx and p["selected"])
            pr = shield.get("resource", {}).get("online", {}).get("powerRanges")
            modifier = self._find_range_modifier(pr, selected) if pr else 1.0
            cooling_cons += selected * modifier

        # Life Support additional cooling
        for ls in self._components.get("lifeSupports", []):
            selected = sum(p["number"] for p in self._seg_config.get("lifeSupport", []) if p["selected"])
            pr = ls.get("resource", {}).get("online", {}).get("powerRanges")
            modifier = self._find_range_modifier(pr, selected) if pr else 1.0
            cooling_cons += selected * modifier

        # Radar additional cooling
        for rd in self._components.get("radars", []):
            selected = sum(p["number"] for p in self._seg_config.get("radar", []) if p["selected"])
            pr = rd.get("resource", {}).get("online", {}).get("powerRanges")
            modifier = self._find_range_modifier(pr, selected) if pr else 1.0
            cooling_cons += selected * modifier

        # QDrive additional cooling
        for qd in self._components.get("qdrives", []):
            selected = sum(p["number"] for p in self._seg_config.get("qdrive", []) if p["selected"])
            pr = qd.get("resource", {}).get("online", {}).get("powerRanges")
            modifier = self._find_range_modifier(pr, selected) if pr else 1.0
            cooling_cons += selected * modifier

        self._power_config["coolingConsumption"] = cooling_cons

    def recalculate(self) -> dict:
        """Recompute all signatures and ratios using erkul-exact formulas."""
        total_capacity = self._power_config.get("totalAvailablePowerSegments", 0)

        # ── Cooling ──
        self._compute_cooling_generation()
        self._compute_cooling_consumption()

        cooling_gen = self._power_config.get("coolingGeneration", 0)
        cooling_cons = self._power_config.get("coolingConsumption", 0)
        cooling_ratio = min(cooling_cons / cooling_gen, 1.0) if cooling_gen > 0 else 0

        # ── Total draw (for consumption %) ──
        total_draw = 0
        pc = self._power_config
        for key in ("weapon", "engine", "shield", "qdrive", "lifeSupport",
                     "radar", "qed", "emp", "miningLaser", "salvage", "tractorBeam"):
            cfg = pc.get(key, {})
            if cfg.get("power"):
                total_draw += cfg.get("segment", 0)
        for c in pc.get("coolers", []):
            if c.get("power"):
                total_draw += c.get("segment", 0)

        consumption_pct = (total_draw / total_capacity * 100) if total_capacity > 0 else 0

        # ── EM Signature (erkul-exact) ──
        pp_usage_ratio = self._get_pp_usage_ratio()
        em_sig = 0.0

        # PP EM contribution
        pps = self._components.get("powerPlants", [])
        if pps:
            pp_em_sum = 0
            for pp in pps:
                res = pp.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                segs_per_pp = round(total_capacity * pp_usage_ratio) / len(pps) if len(pps) > 0 else 0
                pr = res.get("powerRanges")
                modifier = self._find_range_modifier(pr, segs_per_pp) if pr else 1.0
                pp_em_sum += nom_em * modifier
            em_sig += pp_em_sum * pp_usage_ratio

        # Weapon EM: NO range modifier, just sum of nominalSignature
        if self._power_config.get("weapon", {}).get("power"):
            for w in self._components.get("weapons", []):
                res = w.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                em_sig += float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)

        # Life Support EM
        lsp = self._components.get("lifeSupports", [])
        if lsp and self._power_config.get("lifeSupport", {}).get("power"):
            allocated = pc.get("lifeSupport", {}).get("segment", 0)
            total_lsp_seg = sum(p["number"] for p in self._seg_config.get("lifeSupport", []))
            for ls in lsp:
                res = ls.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                pr = res.get("powerRanges")
                modifier = self._find_range_modifier(pr, allocated / len(lsp)) if pr and len(lsp) > 0 else 1.0
                em_sig += nom_em * modifier * (allocated / total_lsp_seg if total_lsp_seg > 0 else 1)

        # QDrive EM
        qds = self._components.get("qdrives", [])
        if qds and self._power_config.get("qdrive", {}).get("power"):
            allocated = pc.get("qdrive", {}).get("segment", 0)
            total_qd_seg = sum(p["number"] for p in self._seg_config.get("qdrive", []))
            for qd in qds:
                res = qd.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                pr = res.get("powerRanges")
                # Erkul bug: uses lifeSupports.length for qdrive range lookup
                divisor = len(lsp) if len(lsp) > 0 else 1
                modifier = self._find_range_modifier(pr, allocated / divisor) if pr else 1.0
                em_sig += nom_em * modifier * (allocated / total_qd_seg if total_qd_seg > 0 else 1)

        # Radar EM
        rds = self._components.get("radars", [])
        if rds and self._power_config.get("radar", {}).get("power"):
            allocated = pc.get("radar", {}).get("segment", 0)
            total_rd_seg = sum(p["number"] for p in self._seg_config.get("radar", []))
            for rd in rds:
                res = rd.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                pr = res.get("powerRanges")
                modifier = self._find_range_modifier(pr, allocated / len(rds)) if pr and len(rds) > 0 else 1.0
                em_sig += nom_em * modifier * (allocated / total_rd_seg if total_rd_seg > 0 else 1)

        # Shield EM
        shields = self._components.get("shields", [])
        if shields and self._power_config.get("shield", {}).get("power"):
            total_selected_shield = sum(p["number"] for p in self._seg_config.get("shield", [])
                                        if p["selected"] and not p.get("disabled"))
            for shield_idx, shield in enumerate(shields):
                this_selected = sum(p["number"] for p in self._seg_config.get("shield", [])
                                    if p.get("index") == shield_idx and p["selected"] and not p.get("disabled"))
                this_total = sum(p["number"] for p in self._seg_config.get("shield", [])
                                 if p.get("index") == shield_idx and not p.get("disabled"))
                this_ratio = this_selected / this_total if this_total > 0 else 0

                res = shield.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                pr = res.get("powerRanges")
                modifier = self._find_range_modifier(pr, total_selected_shield) if pr else 1.0

                if this_selected >= 1:
                    em_sig += nom_em * this_ratio * modifier

        # Cooler EM
        coolers = self._components.get("coolers", [])
        for idx, cooler in enumerate(coolers):
            cooler_configs = self._power_config.get("coolers", [])
            if idx < len(cooler_configs) and cooler_configs[idx].get("power"):
                allocated = cooler_configs[idx].get("segment", 0)
                cooler_pips = self._seg_config.get("coolers", [[]])[idx] if idx < len(self._seg_config.get("coolers", [])) else []
                total_cooler_seg = sum(p["number"] for p in cooler_pips)
                seg_ratio = allocated / total_cooler_seg if total_cooler_seg > 0 else 1

                res = cooler.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_em = float((sig.get("em", {}) or {}).get("nominalSignature", 0) or 0)
                pr = res.get("powerRanges")
                modifier = self._find_range_modifier(pr, allocated) if pr else 1.0
                em_sig += nom_em * seg_ratio * modifier

        em_sig *= self._armor_sig_em
        self.em_signature = em_sig

        # ── IR Signature (erkul-exact: only from coolers) ──
        ir_sig = 0.0
        for idx, cooler in enumerate(coolers):
            cooler_configs = self._power_config.get("coolers", [])
            if idx < len(cooler_configs) and cooler_configs[idx].get("power"):
                allocated = cooler_configs[idx].get("segment", 0)
                cooler_pips = self._seg_config.get("coolers", [[]])[idx] if idx < len(self._seg_config.get("coolers", [])) else []
                selected_seg = sum(p["number"] for p in cooler_pips if p["selected"])
                total_cooler_seg = sum(p["number"] for p in cooler_pips)
                seg_ratio = selected_seg / total_cooler_seg if total_cooler_seg > 0 else 0

                res = cooler.get("resource", {}).get("online", {})
                sig = res.get("signatureParams", {})
                nom_ir = float((sig.get("ir", {}) or {}).get("nominalSignature", 0) or 0)
                pr = res.get("powerRanges")
                modifier = self._find_range_modifier(pr, allocated) if pr else 1.0

                ir_sig += nom_ir * seg_ratio * cooling_ratio * modifier * self._armor_sig_ir

        self.ir_signature = ir_sig

        # ── CS Signature ──
        self.cs_signature = self._ship_cs * self._armor_sig_cs

        # ── Weapon Power Ratio (erkul-exact) ──
        wpn_pool_size = (self._ship_data or {}).get("rnPowerPools", {}).get("weaponGun", {}).get("poolSize", 0) or 0
        wpn_consumption = sum(
            float((w.get("resource", {}).get("online", {}).get("consumption", {}).get("power", 0)) or 0)
            for w in self._components.get("weapons", [])
        )
        buff_mult = (self._ship_data or {}).get("buff", {}).get("regenModifier", {}).get("powerRatioMultiplier", 1) or 1
        if wpn_consumption > 0:
            ratio = wpn_pool_size * buff_mult / wpn_consumption
            self.weapon_power_ratio = min(ratio, 1.0)
        else:
            self.weapon_power_ratio = 1.0 if wpn_pool_size > 0 else 0.0

        if not self._power_config.get("weapon", {}).get("power"):
            self.weapon_power_ratio = 0.0

        # ── Shield Power Ratio ──
        shield_selected = sum(p["number"] for p in self._seg_config.get("shield", [])
                              if p["selected"] and not p.get("disabled"))
        shield_total = sum(p["number"] for p in self._seg_config.get("shield", [])
                           if not p.get("disabled"))
        if shield_total > 0:
            self.shield_power_ratio = shield_selected / shield_total
        else:
            self.shield_power_ratio = 1.0
        if not self._power_config.get("shield", {}).get("power"):
            self.shield_power_ratio = 0.0

        return {
            "em_sig": em_sig,
            "ir_sig": ir_sig,
            "cs_sig": self.cs_signature,
            "consumption_pct": consumption_pct,
            "total_draw": total_draw,
            "total_capacity": total_capacity,
            "weapon_power_ratio": self.weapon_power_ratio,
            "shield_power_ratio": self.shield_power_ratio,
            "pp_online": self._pp_count,
        }
