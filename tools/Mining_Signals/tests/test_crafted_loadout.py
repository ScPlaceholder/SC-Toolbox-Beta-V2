"""Mining Signals reads a crafted loadout and gets Mining Loadout's numbers.

The two tools each carry their own turret maths (Mining Loadout's ``calc_stats``
and ``loadout_loader._compute_turret_stats`` here). The crafted factor itself
lives in one shared function; these tests pin the two turret calculations to
each other so they cannot drift apart for a crafted laser.

No network and no user files: the item database and the blueprint index are
injected, and every loadout file is a temp file.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SIGNALS = os.path.dirname(_HERE)
_ROOT = os.path.normpath(os.path.join(_SIGNALS, "..", ".."))
_ML = os.path.join(_ROOT, "skills", "Mining_Loadout")
for _p in (_ROOT, _SIGNALS):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared import mining_crafting as mc  # noqa: E402
from services import loadout_loader as ll  # noqa: E402

LASER = "Arbor MH2 Mining Laser"
PLAIN = "Unobtainium Laser"
NONE_MODULE = "— No Module —"
NONE_GADGET = "— No Gadget —"


def _group(key, name, mod_key, label, material):
    return {
        "Kind": "group", "Key": key, "Name": name,
        "Modifiers": [{
            "Key": mod_key, "Name": label,
            "QualityRange": {"Min": 0, "Max": 1000},
            "ModifierRange": {"AtMinQuality": 0.8, "AtMaxQuality": 1.2},
            "ValueRangeType": "linear",
        }],
        "Children": [{"Kind": "resource", "Name": material}],
    }


INDEX = mc.build_index([{
    "Output": {"Type": "WeaponMining", "Name": LASER},
    "Tiers": [{"Requirements": {"Children": [
        _group("FRAME", "Frame", "health_maxhealth", "Integrity", "Iron"),
        _group("EMITTER", "Emitter", "weapon_damage", "Laser Power", "Sadaryx"),
        _group("BUS BARS", "Bus Bars", "weapon_damage", "Laser Power", "Copper"),
    ]}}],
}])


@pytest.fixture(scope="module")
def ml():
    """Mining Loadout's models + its calc_stats, loaded beside Mining Signals' ``services``."""
    mods = ll._ensure_ml_modules()
    if not mods:
        pytest.skip("Mining_Loadout skill not importable from this tree")
    spec = importlib.util.spec_from_file_location(
        "_ml_calc_service", os.path.join(_ML, "services", "calc_service.py"))
    calc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(calc)
    return mods["items"], calc.calc_stats


@pytest.fixture
def db(ml, monkeypatch):
    items, _calc = ml

    def laser(name, lo, hi):
        return items.LaserItem(
            id=1, name=name, size=2, company="T", min_power=lo, max_power=hi, ext_power=2590.0,
            opt_range=90.0, max_range=270.0, resistance=25.0, instability=-35.0, inert=-40.0,
            charge_window=40.0, charge_rate=None, module_slots=2)

    def module(name, pct, kind="Passive", resistance=None, uses=0):
        return items.ModuleItem(
            id=2, name=name, item_type=kind, power_pct=pct, ext_power_pct=None,
            resistance=resistance, instability=None, inert=None, charge_rate=None,
            charge_window=None, overcharge=None, shatter=None, uses=uses, duration=60.0)

    database = {
        "lasers": {LASER: laser(LASER, 480.0, 2400.0), PLAIN: laser(PLAIN, 630.0, 3150.0)},
        "modules": {
            "Passive Plus": module("Passive Plus", 135.0, resistance=-10.0),
            "Passive Minus": module("Passive Minus", 95.0),
            "Active Surge": module("Active Surge", 150.0, kind="Active", uses=5),
        },
        "gadgets": {},
    }
    monkeypatch.setattr(ll, "_item_db", database)
    monkeypatch.setattr(mc, "load_index", lambda path=None: INDEX)
    return database


def _write(tmp_path, data, name="loadout.json"):
    p = tmp_path / name
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(p)


def _v1(turrets, combine=None, ship="MOLE"):
    data = {"version": 1, "ship": ship, "turrets": turrets, "gadget": NONE_GADGET}
    if combine:
        data["crafted_combine"] = combine
    return data


def _turret(laser, modules, qualities=None, combine=None):
    td = {"laser": laser, "modules": list(modules)}
    if qualities is not None:
        td["crafted"] = mc.crafted_entry(laser, qualities, combine, INDEX)
    return td


def _loadout_side(ml, db, turret, factor, active):
    """What Mining Loadout's calc_stats says for ONE turret."""
    _items, calc_stats = ml
    mods = [db["modules"][m] for m in turret["modules"] if m in db["modules"]]
    if not active:
        mods = [m for m in mods if m.item_type != "Active"]
    return calc_stats("Prospector", [db["lasers"][turret["laser"]]], [mods], None,
                      power_factors=[factor])


@pytest.mark.parametrize("combine", ["multiply", "average"])
def test_turret_numbers_equal_mining_loadout(ml, db, tmp_path, combine):
    turrets = [
        _turret(LASER, ["Passive Plus", "Active Surge"], {"EMITTER": 1000, "BUS BARS": 700}, combine),
        _turret(LASER, ["Passive Minus", NONE_MODULE], {"EMITTER": 0, "BUS BARS": 250}, combine),
        _turret(PLAIN, ["Passive Plus", NONE_MODULE]),                     # not crafted
    ]
    snap = ll.load_loadout_file(_write(tmp_path, _v1(turrets, combine)))
    assert snap.crafted_combine == combine
    configs = ll.snapshot_to_laser_configs(snap)
    assert len(configs) == 3

    expected_factors = [
        mc.power_factor(LASER, {"EMITTER": 1000, "BUS BARS": 700}, combine, INDEX),
        mc.power_factor(LASER, {"EMITTER": 0, "BUS BARS": 250}, combine, INDEX),
        1.0,
    ]
    assert ll.crafted_power_factors(snap) == pytest.approx(expected_factors)
    assert expected_factors[0] != pytest.approx(1.0)          # the test is not vacuous

    for cfg, turret, factor in zip(configs, turrets, expected_factors):
        passive = _loadout_side(ml, db, turret, factor, active=False)
        active = _loadout_side(ml, db, turret, factor, active=True)
        assert cfg.min_power == pytest.approx(passive["min_power"])
        assert cfg.max_power == pytest.approx(passive["max_power"])
        assert cfg.max_power_active == pytest.approx(active["max_power"])

    # And the whole ship: Mining Loadout's total is the sum of the turrets.
    _items, calc_stats = ml
    total = calc_stats(
        "MOLE",
        [db["lasers"][t["laser"]] for t in turrets],
        [[db["modules"][m] for m in t["modules"] if m in db["modules"]] for t in turrets],
        None, power_factors=expected_factors,
    )
    assert sum(c.max_power_active for c in configs) == pytest.approx(total["max_power"])


def test_worked_number(ml, db, tmp_path):
    """One number checked by hand: 2400 x (1.2 x 1.2) x 1.35 = 4665.6."""
    turrets = [_turret(LASER, ["Passive Plus"], {"EMITTER": 1000, "BUS BARS": 1000}, "multiply")]
    snap = ll.load_loadout_file(_write(tmp_path, _v1(turrets, "multiply", ship="Prospector")))
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(4665.6)
    assert cfg.min_power == pytest.approx(933.12)
    # Crafting does not touch resistance: laser +25%, module -10%.
    assert cfg.resistance_modifier == pytest.approx(1.25 * 0.90)


def test_v2_live_config_shape_is_read_too(ml, db, tmp_path):
    entry = mc.crafted_entry(LASER, {"EMITTER": 1000, "BUS BARS": 1000}, "average", INDEX)
    data = {
        "version": 2, "ship": "Prospector", "hotkey": "",
        "loadout": {"turret_0": {"laser": LASER, "modules": [NONE_MODULE] * 3, "crafted": entry}},
        "gadget": NONE_GADGET, "crafted_combine": "average",
    }
    snap = ll.load_loadout_file(_write(tmp_path, data))
    assert snap.turrets[0].crafted == entry
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(2400.0 * 1.2)


@pytest.mark.parametrize("shape", ["v1", "v2"])
def test_files_without_the_field_load_exactly_as_before(ml, db, tmp_path, shape):
    td = {"laser": LASER, "modules": ["Passive Plus", NONE_MODULE, NONE_MODULE]}
    if shape == "v1":
        data = {"version": 1, "ship": "Prospector", "turrets": [td], "gadget": NONE_GADGET}
    else:
        data = {"version": 2, "ship": "Prospector", "hotkey": "",
                "loadout": {"turret_0": td}, "gadget": NONE_GADGET}
    snap = ll.load_loadout_file(_write(tmp_path, data))
    assert snap.turrets == [ll.TurretSnapshot(laser=LASER, modules=["Passive Plus", NONE_MODULE, NONE_MODULE])]
    assert snap.turrets[0].crafted is None
    assert snap.crafted_combine == ""
    assert ll.crafted_power_factors(snap) == [1.0]
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(2400.0 * 1.35)
    assert cfg.min_power == pytest.approx(480.0 * 1.35)


def test_file_without_a_combine_rule_uses_the_default(ml, db, tmp_path):
    turrets = [_turret(LASER, [], {"EMITTER": 1000, "BUS BARS": 0}, None)]
    snap = ll.load_loadout_file(_write(tmp_path, _v1(turrets, None, ship="Prospector")))
    expected = mc.power_factor(LASER, {"EMITTER": 1000, "BUS BARS": 0}, mc.DEFAULT_COMBINE, INDEX)
    assert ll.crafted_power_factors(snap) == pytest.approx([expected])


def test_inputs_win_over_the_stored_factor_when_data_is_present(ml, db, tmp_path):
    td = {"laser": LASER, "modules": [],
          "crafted": {"qualities": {"EMITTER": 1000, "BUS BARS": 1000}, "power_factor": 7.0}}
    snap = ll.load_loadout_file(_write(tmp_path, _v1([td], "multiply", ship="Prospector")))
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(2400.0 * 1.44)


def test_without_blueprint_data_the_stored_factor_is_used(ml, db, tmp_path, monkeypatch):
    monkeypatch.setattr(mc, "load_index", lambda path=None: {})
    td = {"laser": LASER, "modules": [],
          "crafted": {"qualities": {"EMITTER": 1000, "BUS BARS": 1000}, "power_factor": 1.44}}
    snap = ll.load_loadout_file(_write(tmp_path, _v1([td], "multiply", ship="Prospector")))
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(2400.0 * 1.44)


def test_a_laser_with_no_blueprint_is_never_scaled(ml, db, tmp_path):
    td = {"laser": PLAIN, "modules": [],
          "crafted": {"qualities": {"EMITTER": 1000, "BUS BARS": 1000}, "power_factor": 1.44}}
    snap = ll.load_loadout_file(_write(tmp_path, _v1([td], "multiply", ship="Prospector")))
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(3150.0)


def test_junk_in_the_field_does_not_break_the_load(ml, db, tmp_path):
    td = {"laser": LASER, "modules": [], "crafted": "yes"}
    data = dict(_v1([td], ship="Prospector"), crafted_combine=7)
    snap = ll.load_loadout_file(_write(tmp_path, data))
    assert snap.turrets[0].crafted is None and snap.crafted_combine == ""
    (cfg,) = ll.snapshot_to_laser_configs(snap)
    assert cfg.max_power == pytest.approx(2400.0)
