"""Unit tests for services.dps_calculator."""
import math
import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.dps_calculator import (
    fire_rate_rps, alpha_max, dps_sustained, dmg_breakdown, compute_weapon_stats,
)


def test_fire_rate_looping():
    """Looping mode: fireRate / 60."""
    data = {"weapon": {"mode": "Looping", "fireActions": [{"fireRate": 600}]}}
    assert fire_rate_rps(data) == 10.0


def test_fire_rate_sequential():
    """Sequential delays: 1 / sum(delays/60)."""
    data = {"weapon": {"mode": "Sequential", "fireActions": [
        {"delay": 30}, {"delay": 30},
    ]}}
    rps = fire_rate_rps(data)
    assert abs(rps - 1.0) < 0.001


def test_fire_rate_single():
    """Single fireRate entry."""
    data = {"weapon": {"fireActions": [{"fireRate": 120}]}}
    assert abs(fire_rate_rps(data) - 2.0) < 0.001


def test_fire_rate_dict():
    """fireActions as dict."""
    data = {"weapon": {"fireActions": {"fireRate": 300}}}
    assert abs(fire_rate_rps(data) - 5.0) < 0.001


def test_fire_rate_empty():
    data = {"weapon": {"fireActions": []}}
    assert fire_rate_rps(data) == 0.0


def test_alpha_basic():
    """Basic damage: sum of damage values."""
    data = {
        "weapon": {"fireActions": [{}]},
        "ammo": {"data": {"damage": {"damagePhysical": 50, "damageEnergy": 30}}},
    }
    assert alpha_max(data) == 80.0


def test_alpha_pellets():
    """Damage multiplied by pellet count."""
    data = {
        "weapon": {"fireActions": [{"pelletCount": 5}]},
        "ammo": {"data": {"damage": {"damagePhysical": 10}}},
    }
    assert alpha_max(data) == 50.0


def test_alpha_charge_mult():
    """Charge weapons multiply damage."""
    data = {
        "weapon": {"fireActions": [{"maxChargeDamageMultiplier": 2}]},
        "ammo": {"data": {"damage": {"damagePhysical": 100}}},
    }
    assert alpha_max(data) == 200.0


def test_alpha_explosion():
    """Explosion damage adds to total."""
    data = {
        "weapon": {"fireActions": [{}]},
        "ammo": {"data": {
            "damage": {"damagePhysical": 50},
            "explosion": {"damage": {"damageThermal": 25}},
        }},
    }
    assert alpha_max(data) == 75.0


def test_dps_sustained_regen():
    """Regen weapon sustained DPS formula."""
    data = {
        "weapon": {
            "regen": {
                "maxAmmoLoad": 100,
                "maxRegenPerSec": 50,
                "regenerationCooldown": 1.0,
            },
            "fireActions": [{"fireRate": 600}],
        },
        "ammo": {"data": {"damage": {"damagePhysical": 10}}},
    }
    rps = fire_rate_rps(data)
    alp = alpha_max(data)
    sus = dps_sustained(data, alp, rps)
    # ammos=100, chargeTime=1+100/50=3, fireTime=100/10=10
    # sustained = (100*10) / (3+10) = 76.92
    expected = (100 * 10) / (3 + 10)
    assert abs(sus - expected) < 0.1


def test_dps_sustained_heat():
    """Heat-based weapon sustained DPS."""
    data = {
        "weapon": {
            "fireActions": [{"fireRate": 300, "heatPerShot": 10}],
            "connection": {"simplifiedHeat": {
                "overheatTemperature": 100,
                "temperatureAfterOverheatFix": 0,
                "overheatFixTime": 2.0,
                "timeTillCoolingStarts": 0,
                "coolingPerSecond": 0,
            }},
        },
        "ammo": {"data": {"damage": {"damagePhysical": 20}}},
    }
    rps = 300 / 60  # 5 rps
    alp = 20
    sus = dps_sustained(data, alp, rps)
    # effective_hps=10, oh_time=100/(10*5)=2, shots=ceil(2*5)=10
    # cycle=2+2=4, sustained=10*20/4=50
    assert abs(sus - 50.0) < 0.1


def test_dps_sustained_no_overheat():
    """Weapon that never overheats."""
    data = {
        "weapon": {
            "fireActions": [{"fireRate": 120, "heatPerShot": 1}],
            "connection": {"simplifiedHeat": {
                "overheatTemperature": 100,
                "temperatureAfterOverheatFix": 0,
                "overheatFixTime": 1.0,
                "timeTillCoolingStarts": 0,
                "coolingPerSecond": 10,
            }},
        },
        "ammo": {"data": {"damage": {"damagePhysical": 50}}},
    }
    rps = 2.0
    alp = 50.0
    sus = dps_sustained(data, alp, rps)
    # time_between_shots=0.5, cooling=0.5*10=5, effective_hps=1-5=-4 < 0
    # Never overheats → raw DPS
    assert abs(sus - 100.0) < 0.1


def test_dmg_breakdown():
    data = {
        "ammo": {"data": {
            "damage": {"damagePhysical": 10, "damageEnergy": 20},
            "explosion": {"damage": {"damageThermal": 5}},
        }},
    }
    brk = dmg_breakdown(data)
    assert brk["damagePhysical"] == 10
    assert brk["damageEnergy"] == 20
    assert brk["damageThermal"] == 5
    assert brk["damageDistortion"] == 0


def test_compute_weapon_stats():
    raw = {
        "localName": "weap_test",
        "data": {
            "name": "Test Gun",
            "ref": "abc-123",
            "size": 3,
            "group": "laser repeater",
            "weapon": {"mode": "Looping", "fireActions": [{"fireRate": 600}]},
            "ammo": {"data": {"damage": {"damageEnergy": 25}}},
            "ammoContainer": {"maxAmmoCount": 0},
        },
    }
    stats = compute_weapon_stats(raw)
    assert stats["name"] == "Test Gun"
    assert stats["ref"] == "abc-123"
    assert stats["size"] == 3
    assert stats["rps"] == 10.0
    assert stats["alpha"] == 25.0
    assert abs(stats["dps_raw"] - 250.0) < 0.1
    assert stats["dom"] == "damageEnergy"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
            except (KeyError, TypeError, ValueError, AttributeError, RuntimeError) as e:
                print(f"  ERROR {name}: {e}")
    print("Done.")
