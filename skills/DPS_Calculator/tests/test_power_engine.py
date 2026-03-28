"""Unit tests for services.power_engine."""
import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.power_engine import PowerAllocatorEngine


def _dummy_lookup(ln):
    """Minimal lookup returning power/em data for known components."""
    _catalog = {
        "test_weapon": {"name": "Test Gun", "power_draw": 3.0, "em_max": 100, "ir_max": 0},
        "test_shield": {"name": "Test Shield", "power_draw": 2.0, "em_max": 50, "ir_max": 0},
        "test_cooler": {"name": "Test Cooler", "power_draw": 1.0, "em_max": 20, "ir_max": 30, "cooling_rate": 5000},
        "test_pp": {"name": "Test PP", "output": 10.0, "em_max": 200, "em_idle": 200},
    }
    return _catalog.get(ln)


def _dummy_raw_lookup(ident):
    _raw = {
        "test_cooler": {
            "name": "Test Cooler",
            "resource": {
                "online": {
                    "consumption": {"powerSegment": 1},
                    "generation": {"cooling": 50},
                    "signatureParams": {
                        "em": {"nominalSignature": 20},
                        "ir": {"nominalSignature": 30},
                    },
                }
            },
        },
    }
    return _raw.get(ident)


def test_engine_init():
    engine = PowerAllocatorEngine(_dummy_lookup, _dummy_raw_lookup)
    assert engine.slots == []
    assert engine.categories == {}
    assert engine.mode == "SCM"


def test_find_range_modifier():
    ranges = [
        {"start": 0, "modifier": 0.5},
        {"start": 3, "modifier": 1.0},
        {"start": 6, "modifier": 1.5},
    ]
    assert PowerAllocatorEngine._find_range_modifier(ranges, 0) == 0.5
    assert PowerAllocatorEngine._find_range_modifier(ranges, 2) == 0.5
    assert PowerAllocatorEngine._find_range_modifier(ranges, 3) == 1.0
    assert PowerAllocatorEngine._find_range_modifier(ranges, 5) == 1.0
    assert PowerAllocatorEngine._find_range_modifier(ranges, 6) == 1.5
    assert PowerAllocatorEngine._find_range_modifier(ranges, 100) == 1.5


def test_find_range_modifier_empty():
    assert PowerAllocatorEngine._find_range_modifier(None, 5) == 1.0
    assert PowerAllocatorEngine._find_range_modifier([], 5) == 1.0


def test_set_mode_nav():
    engine = PowerAllocatorEngine(_dummy_lookup, _dummy_raw_lookup)
    assert engine.mode == "SCM"
    engine.set_mode("NAV")
    assert engine.mode == "NAV"


def test_set_mode_scm():
    engine = PowerAllocatorEngine(_dummy_lookup, _dummy_raw_lookup)
    engine._mode = "NAV"
    engine.set_mode("SCM")
    assert engine.mode == "SCM"


def test_set_mode_ignores_same():
    engine = PowerAllocatorEngine(_dummy_lookup, _dummy_raw_lookup)
    engine.set_mode("SCM")  # already SCM
    assert engine.mode == "SCM"


def test_set_mode_ignores_invalid():
    engine = PowerAllocatorEngine(_dummy_lookup, _dummy_raw_lookup)
    engine.set_mode("INVALID")
    assert engine.mode == "SCM"


def test_set_level_clamp():
    engine = PowerAllocatorEngine(_dummy_lookup)
    engine._slots = [
        {"id": "s0", "name": "X", "category": "shield", "max_segments": 5,
         "default_seg": 3, "current_seg": 3, "enabled": True,
         "draw_per_seg": 1, "em_per_seg": 0, "ir_per_seg": 0,
         "em_total": 0, "ir_total": 0, "cooling_gen": 0,
         "power_ranges": None, "is_generator": False, "output": 0},
    ]
    engine._categories = {"shield": [engine._slots[0]]}

    engine.set_level_by_type("shield", 0, 10)
    assert engine._slots[0]["current_seg"] == 5  # clamped to max

    engine.set_level_by_type("shield", 0, -1)
    assert engine._slots[0]["current_seg"] == 0  # clamped to 0


def test_toggle():
    engine = PowerAllocatorEngine(_dummy_lookup)
    engine._slots = [
        {"id": "s0", "name": "X", "category": "radar", "max_segments": 2,
         "default_seg": 2, "current_seg": 2, "enabled": True,
         "draw_per_seg": 1, "em_per_seg": 0, "ir_per_seg": 0,
         "em_total": 0, "ir_total": 0, "cooling_gen": 0,
         "power_ranges": None, "is_generator": False, "output": 0},
    ]
    engine._categories = {"radar": [engine._slots[0]]}

    engine.toggle_by_type("radar", 0)
    assert engine._slots[0]["enabled"] == False

    engine.toggle_by_type("radar", 0)
    assert engine._slots[0]["enabled"] == True


def test_recalculate_empty():
    engine = PowerAllocatorEngine(_dummy_lookup)
    result = engine.recalculate()
    assert result["em_sig"] == 0
    assert result["ir_sig"] == 0
    assert result["consumption_pct"] == 0
    # No weapons/shields powered → ratios are 0
    assert result["weapon_power_ratio"] == 0.0
    assert result["shield_power_ratio"] == 0.0


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
