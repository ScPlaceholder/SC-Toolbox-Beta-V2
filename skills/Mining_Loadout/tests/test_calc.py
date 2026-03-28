"""Tests for stat calculation logic."""
import os
import sys
import pytest

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')))

from models.items import LaserItem, ModuleItem, GadgetItem
from services.calc_service import mult_stack, calc_stats, calc_loadout_price


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_laser(**overrides) -> LaserItem:
    defaults = dict(
        id=1, name="Test Laser", size=1, company="TestCo",
        min_power=480.0, max_power=2400.0, ext_power=1200.0,
        opt_range=150.0, max_range=300.0,
        resistance=None, instability=None, inert=None,
        charge_window=None, charge_rate=None,
        module_slots=2, price=0.0,
    )
    defaults.update(overrides)
    return LaserItem(**defaults)


def _make_module(**overrides) -> ModuleItem:
    defaults = dict(
        id=100, name="Test Module", item_type="Passive",
        power_pct=None, ext_power_pct=None,
        resistance=None, instability=None, inert=None,
        charge_rate=None, charge_window=None,
        overcharge=None, shatter=None,
        uses=0, duration=None, price=0.0,
    )
    defaults.update(overrides)
    return ModuleItem(**defaults)


def _make_gadget(**overrides) -> GadgetItem:
    defaults = dict(
        id=200, name="Test Gadget",
        charge_window=None, charge_rate=None,
        instability=None, resistance=None, cluster=None,
        price=0.0,
    )
    defaults.update(overrides)
    return GadgetItem(**defaults)


# ── mult_stack tests ──────────────────────────────────────────────────────────

class TestMultStack:
    def test_empty(self):
        assert mult_stack([]) == 0.0

    def test_single_positive(self):
        assert mult_stack([25.0]) == pytest.approx(25.0)

    def test_single_negative(self):
        assert mult_stack([-10.0]) == pytest.approx(-10.0)

    def test_two_values(self):
        # (1+0.25) * (1-0.10) - 1 = 1.125 - 1 = 0.125 = 12.5%
        assert mult_stack([25.0, -10.0]) == pytest.approx(12.5)

    def test_zeros_ignored(self):
        assert mult_stack([0.0, 25.0, 0.0]) == pytest.approx(25.0)

    def test_all_zeros(self):
        assert mult_stack([0.0, 0.0]) == 0.0

    def test_three_values(self):
        # (1+0.10) * (1+0.20) * (1-0.05) - 1
        result = (1.10 * 1.20 * 0.95 - 1.0) * 100.0
        assert mult_stack([10.0, 20.0, -5.0]) == pytest.approx(result)


# ── calc_stats tests ──────────────────────────────────────────────────────────

class TestCalcStats:
    def test_stock_single_laser(self):
        laser = _make_laser(min_power=480, max_power=2400, ext_power=1200)
        stats = calc_stats("Prospector", [laser], [[]], None)
        assert stats["min_power"] == pytest.approx(480.0)
        assert stats["max_power"] == pytest.approx(2400.0)
        assert stats["ext_power"] == pytest.approx(1200.0)

    def test_power_with_module(self):
        """Module with 135% power_pct -> +35% power."""
        laser = _make_laser(min_power=100, max_power=200)
        mod = _make_module(power_pct=135.0)
        stats = calc_stats("Prospector", [laser], [[mod]], None)
        assert stats["min_power"] == pytest.approx(135.0)
        assert stats["max_power"] == pytest.approx(270.0)

    def test_power_two_modules_additive(self):
        """Two modules: 95% (-5%) and 150% (+50%) -> +45% total, not multiplicative."""
        laser = _make_laser(min_power=100, max_power=200)
        mod1 = _make_module(id=101, power_pct=95.0)   # -5%
        mod2 = _make_module(id=102, power_pct=150.0)   # +50%
        stats = calc_stats("Prospector", [laser], [[mod1, mod2]], None)
        assert stats["min_power"] == pytest.approx(145.0)
        assert stats["max_power"] == pytest.approx(290.0)

    def test_multi_turret_power_sums(self):
        """MOLE: 3 turrets, power sums across turrets."""
        laser = _make_laser(min_power=100, max_power=200, size=2)
        stats = calc_stats("MOLE", [laser, laser, laser], [[], [], []], None)
        assert stats["min_power"] == pytest.approx(300.0)
        assert stats["max_power"] == pytest.approx(600.0)

    def test_range_from_first_laser(self):
        laser1 = _make_laser(opt_range=150.0, max_range=300.0)
        laser2 = _make_laser(opt_range=200.0, max_range=400.0)
        stats = calc_stats("MOLE", [laser1, laser2], [[], []], None)
        assert stats["opt_range"] == pytest.approx(150.0)
        assert stats["max_range"] == pytest.approx(300.0)

    def test_pct_modifiers_multiplicative_across_turrets(self):
        """Laser resistance + module resistance -> multiplicative stacking."""
        laser = _make_laser(resistance=25.0)
        mod = _make_module(resistance=-10.0)
        stats = calc_stats("Prospector", [laser], [[mod]], None)
        expected = mult_stack([25.0, -10.0])
        assert stats["resistance"] == pytest.approx(expected)

    def test_gadget_modifiers(self):
        laser = _make_laser()
        gadget = _make_gadget(resistance=15.0, cluster=10.0)
        stats = calc_stats("Prospector", [laser], [[]], gadget)
        assert stats["resistance"] == pytest.approx(15.0)
        assert stats["cluster"] == pytest.approx(10.0)

    def test_no_laser_returns_zeros(self):
        stats = calc_stats("Prospector", [None], [[]], None)
        assert stats["min_power"] == 0.0
        assert stats["max_power"] == 0.0

    def test_ext_power_none_treated_as_zero(self):
        laser = _make_laser(ext_power=None)
        stats = calc_stats("Prospector", [laser], [[]], None)
        assert stats["ext_power"] == 0.0


# ── calc_loadout_price tests ──────────────────────────────────────────────────

class TestCalcLoadoutPrice:
    def test_stock_laser_free(self):
        laser = _make_laser(name="Arbor MH1 Mining Laser", price=5000.0)
        price = calc_loadout_price("Prospector", [laser], [[]], None)
        assert price == 0.0  # stock laser excluded

    def test_non_stock_laser_counted(self):
        laser = _make_laser(name="Hofstede-S1 Mining Laser", price=5000.0)
        price = calc_loadout_price("Prospector", [laser], [[]], None)
        assert price == 5000.0

    def test_modules_and_gadget_counted(self):
        laser = _make_laser(name="Arbor MH1 Mining Laser", price=0.0)
        mod = _make_module(price=1000.0)
        gadget = _make_gadget(price=500.0)
        price = calc_loadout_price("Prospector", [laser], [[mod]], gadget)
        assert price == 1500.0
