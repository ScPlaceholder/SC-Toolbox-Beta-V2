"""Crafted lasers in Mining Loadout: the maths, the files, and the turret panel.

Nothing here touches the user's real settings: every file is a temp file, and
the window's config load/save are redirected to one. The only real data read is
the blueprint cache (read-only), and only by the tests that say so.
"""
import json
import os
import sys

import pytest

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

from shared import mining_crafting as mc
from models.items import LaserItem, ModuleItem, NONE_GADGET, NONE_LASER, NONE_MODULE
from services.calc_service import calc_stats
from services.config_service import CONFIG_VERSION, _validate_config, load_config, save_config

SKILL_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

CRAFTABLE = "Arbor MH1 Mining Laser"      # Prospector stock laser; has a blueprint
NOT_CRAFTABLE = "Unobtainium Laser"       # in the item list, no blueprint


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


def _blueprints():
    return [{
        "Output": {"Type": "WeaponMining", "Name": CRAFTABLE},
        "Tiers": [{"Requirements": {"Children": [
            _group("FRAME", "Frame", "health_maxhealth", "Integrity", "Iron"),
            _group("EMITTER", "Emitter", "weapon_damage", "Laser Power", "Sadaryx"),
            _group("BUS BARS", "Bus Bars", "weapon_damage", "Laser Power", "Copper"),
        ]}}],
    }]


INDEX = mc.build_index(_blueprints())


def _laser(name=CRAFTABLE, **kw):
    d = dict(id=1, name=name, size=1, company="T", min_power=400.0, max_power=2000.0,
             ext_power=1000.0, opt_range=60.0, max_range=180.0, resistance=None,
             instability=None, inert=None, charge_window=None, charge_rate=None,
             module_slots=2, price=0.0)
    d.update(kw)
    return LaserItem(**d)


def _module(name="Mod", power_pct=None, item_type="Passive", **kw):
    d = dict(id=100, name=name, item_type=item_type, power_pct=power_pct, ext_power_pct=None,
             resistance=None, instability=None, inert=None, charge_rate=None,
             charge_window=None, overcharge=None, shatter=None, uses=0, duration=None, price=0.0)
    d.update(kw)
    return ModuleItem(**d)


# ── calc_stats ───────────────────────────────────────────────────────────────

class TestCalcStatsCrafted:
    def test_without_factors_is_unchanged(self):
        base = calc_stats("Prospector", [_laser()], [[]], None)
        assert base["min_power"] == pytest.approx(400.0)
        assert base["max_power"] == pytest.approx(2000.0)
        assert calc_stats("Prospector", [_laser()], [[]], None, power_factors=None) == base
        assert calc_stats("Prospector", [_laser()], [[]], None, power_factors=[1.0]) == base

    def test_crafted_scales_laser_power(self):
        s = calc_stats("Prospector", [_laser()], [[]], None, power_factors=[1.44])
        assert s["min_power"] == pytest.approx(576.0)
        assert s["max_power"] == pytest.approx(2880.0)

    def test_crafted_applies_before_the_module_multiplier(self):
        # +35% and -5% modules -> x1.30; crafted x1.44 scales the laser first.
        mods = [[_module(power_pct=135.0), _module(power_pct=95.0)]]
        s = calc_stats("Prospector", [_laser()], mods, None, power_factors=[1.44])
        assert s["min_power"] == pytest.approx(400.0 * 1.44 * 1.30)
        assert s["max_power"] == pytest.approx(2000.0 * 1.44 * 1.30)

    def test_extraction_power_and_percent_stats_are_not_touched(self):
        laser = _laser(resistance=25.0, instability=-10.0)
        plain = calc_stats("Prospector", [laser], [[]], None)
        crafted = calc_stats("Prospector", [laser], [[]], None, power_factors=[0.64])
        for key in plain:
            if key not in ("min_power", "max_power"):
                assert crafted[key] == plain[key], key

    def test_factor_is_per_turret(self):
        lasers = [_laser(), _laser(), None]
        s = calc_stats("MOLE", lasers, [[], [], []], None, power_factors=[1.2, 1.0, 5.0])
        assert s["max_power"] == pytest.approx(2000.0 * 1.2 + 2000.0)

    def test_short_factor_list_leaves_the_rest_stock(self):
        s = calc_stats("MOLE", [_laser(), _laser()], [[], []], None, power_factors=[1.2])
        assert s["max_power"] == pytest.approx(2000.0 * 1.2 + 2000.0)


# ── config file (version 2) ──────────────────────────────────────────────────

class TestConfigCrafted:
    def test_roundtrip_keeps_the_inputs(self, tmp_path):
        path = str(tmp_path / "config.json")
        entry = mc.crafted_entry(CRAFTABLE, {"EMITTER": 750, "BUS BARS": 900}, "average", INDEX)
        assert save_config("Prospector", "", [CRAFTABLE], [[NONE_MODULE] * 3], NONE_GADGET, path,
                           turret_crafted=[entry], crafted_combine="average")
        cfg = load_config(path)
        assert cfg["loadout"]["turret_0"]["crafted"]["qualities"] == {"EMITTER": 750, "BUS BARS": 900}
        assert cfg["loadout"]["turret_0"]["crafted"]["power_factor"] == pytest.approx((1.1 + 1.16) / 2)
        assert cfg["crafted_combine"] == "average"
        assert cfg["version"] == CONFIG_VERSION

    def test_no_crafted_laser_writes_the_old_file(self, tmp_path):
        old, new = str(tmp_path / "old.json"), str(tmp_path / "new.json")
        args = ("MOLE", "", [NONE_LASER] * 3, [[NONE_MODULE] * 3] * 3, NONE_GADGET)
        save_config(*args, old)
        save_config(*args, new, turret_crafted=[None, None, None], crafted_combine=None)
        assert open(old, "rb").read() == open(new, "rb").read()
        raw = json.load(open(new, encoding="utf-8"))
        assert "crafted_combine" not in raw
        assert all("crafted" not in t for t in raw["loadout"].values())

    def test_real_v2_file_without_the_field_loads_unchanged(self, tmp_path):
        """The config file shipped in the repo, copied to a temp dir."""
        src = os.path.join(SKILL_DIR, "mining_loadout_config.json")
        if not os.path.isfile(src):
            pytest.skip("no shipped config file in this tree")
        raw = json.load(open(src, encoding="utf-8"))
        copy = tmp_path / "config.json"
        copy.write_bytes(open(src, "rb").read())
        cfg = load_config(str(copy))
        assert cfg["ship"] == raw["ship"]
        assert cfg["loadout"] == raw["loadout"]
        assert cfg["gadget"] == raw["gadget"]
        assert "crafted_combine" not in cfg
        assert all(set(t) == {"laser", "modules"} for t in cfg["loadout"].values())

    def test_junk_in_the_field_is_dropped_not_fatal(self):
        cfg = _validate_config({
            "ship": "Prospector", "crafted_combine": "sideways",
            "loadout": {"turret_0": {"laser": CRAFTABLE, "modules": [], "crafted": "yes please"}},
        })
        assert "crafted" not in cfg["loadout"]["turret_0"]
        assert "crafted_combine" not in cfg


# ── the window (offscreen; never shown) ──────────────────────────────────────

V1_FILE = {
    "version": 1,
    "ship": "Prospector",
    "turrets": [{"laser": CRAFTABLE,
                 "modules": ["Power Module", NONE_MODULE, NONE_MODULE]}],
    "gadget": NONE_GADGET,
}


@pytest.fixture(scope="module")
def qapp():
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def _make_window(monkeypatch, tmp_path, index):
    import queue
    import ui.main_window as mw
    cfg_path = str(tmp_path / "live_config.json")
    monkeypatch.setattr(mw.mining_crafting, "load_index", lambda path=None: index)
    real_save, real_load = mw.save_config, mw.load_config
    monkeypatch.setattr(mw, "save_config", lambda *a, **k: real_save(*a, path=cfg_path, **k))
    monkeypatch.setattr(mw, "load_config", lambda *a, **k: real_load(cfg_path))
    win = mw.MiningLoadoutWindow(queue.Queue())
    win._on_ship_changed("Prospector")
    win._on_data_loaded(
        [_laser(), _laser(NOT_CRAFTABLE, id=2)],
        [_module("Power Module", power_pct=135.0)],
        [],
    )
    return win, cfg_path


@pytest.fixture
def window(qapp, monkeypatch, tmp_path):
    win, cfg_path = _make_window(monkeypatch, tmp_path, INDEX)
    yield win, cfg_path
    win.deleteLater()


def _select_laser(win, name, turret=0):
    combo = win._laser_combos[turret]
    combo.setCurrentIndex(combo.findText(name))


def _craft(win, qualities, turret=0):
    ctrl = win._crafted_controls[turret]
    ctrl._check.setChecked(True)
    for key, value in qualities.items():
        ctrl._spins[key].setValue(value)


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return str(p)


def _export(win):
    n = len(win._turret_laser_selections)
    turrets = [{"laser": win._turret_laser_selections[i],
                "modules": list(win._turret_module_selections[i])} for i in range(n)]
    return win._loadout_file_data(turrets, NONE_GADGET)


class TestWindowCrafted:
    def test_crafted_laser_changes_the_stats(self, window):
        win, _cfg = window
        _select_laser(win, CRAFTABLE)
        assert win._stat_labels["max_power"].text() == "2,000.0"
        _craft(win, {"EMITTER": 1000, "BUS BARS": 1000})
        assert win._turret_crafted[0] == {"EMITTER": 1000, "BUS BARS": 1000}
        assert win._stat_labels["max_power"].text() == "2,880.0"      # x1.44 (multiply)
        assert win._stat_labels["min_power"].text() == "576"
        assert "+44.0%" in win._crafted_controls[0]._pct.text()
        assert win._stat_labels["ext_power"].text() == "1,000.0"      # extraction untouched

    def test_combine_rule_is_a_setting_and_defaults_to_the_constant(self, window):
        win, _cfg = window
        assert win._crafted_combine == mc.DEFAULT_COMBINE
        assert win._combine_combo.currentData() == mc.DEFAULT_COMBINE
        _select_laser(win, CRAFTABLE)
        _craft(win, {"EMITTER": 1000, "BUS BARS": 0})
        win._combine_combo.setCurrentIndex(win._combine_combo.findData("multiply"))
        assert win._stat_labels["max_power"].text() == "1,920.0"      # 1.2 * 0.8 = 0.96
        win._combine_combo.setCurrentIndex(win._combine_combo.findData("average"))
        assert win._stat_labels["max_power"].text() == "2,000.0"      # (1.2 + 0.8) / 2 = 1.0
        assert "+0.0%" in win._crafted_controls[0]._pct.text()

    def test_modules_apply_after_the_crafted_factor(self, window):
        win, _cfg = window
        _select_laser(win, CRAFTABLE)
        _craft(win, {"EMITTER": 1000, "BUS BARS": 1000})
        mcombo = win._module_combos[0][0]
        mcombo.setCurrentIndex(mcombo.findText("Power Module"))
        assert win._stat_labels["max_power"].text() == f"{2000.0 * 1.44 * 1.35:,.1f}"

    def test_a_laser_without_a_blueprint_cannot_be_marked(self, window):
        win, _cfg = window
        _select_laser(win, NOT_CRAFTABLE)
        ctrl = win._crafted_controls[0]
        assert not ctrl._check.isEnabled()
        ctrl._check.setChecked(True)            # even if something forces the tick
        assert ctrl.state() is None
        assert win._turret_crafted[0] is None
        assert win._stat_labels["max_power"].text() == "2,000.0"
        assert _export(win)["turrets"][0].keys() == {"laser", "modules"}

    def test_changing_the_laser_clears_the_tick(self, window):
        win, _cfg = window
        _select_laser(win, CRAFTABLE)
        _craft(win, {"EMITTER": 1000, "BUS BARS": 1000})
        _select_laser(win, NOT_CRAFTABLE)
        assert win._turret_crafted[0] is None
        _select_laser(win, CRAFTABLE)
        assert win._turret_crafted[0] is None
        assert win._stat_labels["max_power"].text() == "2,000.0"

    def test_export_then_load_keeps_the_inputs(self, window, tmp_path):
        win, _cfg = window
        _select_laser(win, CRAFTABLE)
        _craft(win, {"EMITTER": 750, "BUS BARS": 900})
        win._combine_combo.setCurrentIndex(win._combine_combo.findData("average"))
        data = _export(win)
        assert data["version"] == 1
        assert data["crafted_combine"] == "average"
        assert data["turrets"][0]["crafted"] == {
            "qualities": {"EMITTER": 750, "BUS BARS": 900},
            "power_factor": pytest.approx(1.13),
        }
        path = _write(tmp_path, "exported.json", data)

        win._reset_loadout()
        win._combine_combo.setCurrentIndex(win._combine_combo.findData("multiply"))
        assert win._turret_crafted[0] is None

        win._load_loadout_path(path)
        assert win._turret_laser_selections[0] == CRAFTABLE
        assert win._turret_crafted[0] == {"EMITTER": 750, "BUS BARS": 900}
        assert win._crafted_combine == "average"
        assert win._crafted_controls[0]._check.isChecked()
        assert win._crafted_controls[0]._spins["EMITTER"].value() == 750
        assert win._stat_labels["max_power"].text() == f"{2000.0 * 1.13:,.1f}"

    def test_live_config_carries_the_crafted_laser(self, window):
        win, cfg_path = window
        _select_laser(win, CRAFTABLE)
        _craft(win, {"EMITTER": 600, "BUS BARS": 400})
        raw = json.load(open(cfg_path, encoding="utf-8"))
        assert raw["version"] == CONFIG_VERSION
        assert raw["loadout"]["turret_0"]["crafted"]["qualities"] == {"EMITTER": 600, "BUS BARS": 400}
        assert raw["crafted_combine"] == mc.DEFAULT_COMBINE

    def test_v1_file_without_the_field_loads_as_before(self, window, tmp_path):
        win, _cfg = window
        _select_laser(win, CRAFTABLE)
        _craft(win, {"EMITTER": 1000, "BUS BARS": 1000})      # must not survive the load
        win._load_loadout_path(_write(tmp_path, "old_v1.json", V1_FILE))
        assert win._turret_laser_selections[0] == CRAFTABLE
        assert win._turret_module_selections[0][0] == "Power Module"
        assert win._turret_crafted[0] is None
        assert not win._crafted_controls[0]._check.isChecked()
        assert win._stat_labels["max_power"].text() == f"{2000.0 * 1.35:,.1f}"
        assert _export(win) == V1_FILE                        # and it writes the same file back


class TestWindowWithoutBlueprintData:
    def test_feature_is_absent_and_the_tool_still_works(self, qapp, monkeypatch, tmp_path):
        win, _cfg = _make_window(monkeypatch, tmp_path, {})
        try:
            _select_laser(win, CRAFTABLE)
            ctrl = win._crafted_controls[0]
            assert ctrl.isHidden()
            assert not ctrl._check.isEnabled()
            assert win._combine_combo is None
            assert win._stat_labels["max_power"].text() == "2,000.0"
            # A file that says "crafted" still loads; with no data here nothing is applied.
            crafted = dict(V1_FILE, turrets=[dict(V1_FILE["turrets"][0], crafted={
                "qualities": {"EMITTER": 1000, "BUS BARS": 1000}, "power_factor": 1.44})])
            win._load_loadout_path(_write(tmp_path, "crafted.json", crafted))
            assert win._turret_laser_selections[0] == CRAFTABLE
            assert win._stat_labels["max_power"].text() == f"{2000.0 * 1.35:,.1f}"
        finally:
            win.deleteLater()

    def test_a_failing_loader_does_not_stop_the_window(self, qapp, monkeypatch, tmp_path):
        import queue
        import ui.main_window as mw

        def boom(path=None):
            raise RuntimeError("cache exploded")

        cfg_path = str(tmp_path / "live_config.json")
        real_save, real_load = mw.save_config, mw.load_config
        monkeypatch.setattr(mw.mining_crafting, "load_index", boom)
        monkeypatch.setattr(mw, "save_config", lambda *a, **k: real_save(*a, path=cfg_path, **k))
        monkeypatch.setattr(mw, "load_config", lambda *a, **k: real_load(cfg_path))
        win = mw.MiningLoadoutWindow(queue.Queue())
        try:
            assert win._craft_index == {}
        finally:
            win.deleteLater()


class TestRealBlueprintData:
    def test_the_stock_lasers_are_craftable_in_the_real_data(self):
        path = mc.default_blueprint_path()
        if not os.path.isfile(path):
            pytest.skip(f"real blueprint cache not on this machine: {path}")
        idx = mc.load_index(path)
        from models.items import SHIPS
        for ship in SHIPS.values():
            assert mc.is_craftable(ship.stock_laser, idx), ship.stock_laser
        assert not mc.is_craftable(NONE_LASER, idx)
