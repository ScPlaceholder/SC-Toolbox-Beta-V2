"""Crafted mining lasers: the factor both mining tools apply.

Two kinds of test:
  * against a small blueprint file written by the test (always runs);
  * against the REAL pinned blueprints.json in the user's cache, read-only
    (skipped with a reason when that file is not on the machine). These are the
    ones that tell us if a game patch changed the 0.8 .. 1.2 range.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), '..', '..')))

from shared import mining_crafting as mc


def _group(key, name, mod_key, label, material, at_min=0.8, at_max=1.2,
           q_min=0, q_max=1000, kind="linear"):
    return {
        "Kind": "group", "Key": key, "Name": name, "RequiredCount": 1,
        "Modifiers": [{
            "Key": mod_key, "Name": label,
            "QualityRange": {"Min": q_min, "Max": q_max},
            "ModifierRange": {"AtMinQuality": at_min, "AtMaxQuality": at_max},
            "ValueRangeType": kind,
        }],
        "Children": [{"Kind": "resource", "Name": material, "MinQuality": 1}],
    }


def _laser_record(name, groups):
    return {
        "Key": "BP_TEST", "Kind": "creation",
        "Output": {"Type": "WeaponMining", "Name": name},
        "Tiers": [{"TierIndex": 0, "Requirements": {"Kind": "root", "Children": groups}}],
    }


def _standard_groups(**kw):
    return [
        _group("FRAME", "Frame", "health_maxhealth", "Integrity", "Iron"),
        _group("EMITTER", "Emitter", "weapon_damage", "Laser Power", "Sadaryx", **kw),
        _group("BUS BARS", "Bus Bars", "weapon_damage", "Laser Power", "Copper", **kw),
    ]


RECORDS = [
    _laser_record("Test Laser S1", _standard_groups()),
    # A module blueprint: no modifiers, not a laser.
    {"Output": {"Type": "MiningModifier", "Name": "Stampede Module"},
     "Tiers": [{"Requirements": {"Children": [{"Kind": "group", "Key": "X", "Children": []}]}}]},
]


@pytest.fixture
def index():
    return mc.build_index(RECORDS)


# ── index ────────────────────────────────────────────────────────────────────

def test_index_holds_only_lasers_with_a_power_group(index):
    assert list(index) == ["Test Laser S1"]
    assert [g["key"] for g in index["Test Laser S1"]["power"]] == ["EMITTER", "BUS BARS"]
    assert [g["key"] for g in index["Test Laser S1"]["integrity"]] == ["FRAME"]
    assert index["Test Laser S1"]["power"][0]["material"] == "Sadaryx"


def test_ranges_come_from_the_data_not_from_the_code():
    idx = mc.build_index([_laser_record("Patched", _standard_groups(at_min=0.9, at_max=1.3))])
    assert mc.power_factor("Patched", {"EMITTER": 0, "BUS BARS": 0}, "multiply", idx) == pytest.approx(0.81)
    assert mc.power_factor("Patched", {"EMITTER": 1000, "BUS BARS": 1000}, "average", idx) == pytest.approx(1.3)


def test_unknown_range_type_is_not_offered():
    idx = mc.build_index([_laser_record("Odd", _standard_groups(kind="segmented"))])
    assert idx == {}


def test_garbage_input_gives_an_empty_index():
    assert mc.build_index(None) == {}
    assert mc.build_index({"not": "a list"}) == {}
    assert mc.build_index([None, 3, {"Output": None}, {"Output": {"Type": "WeaponMining"}}]) == {}


# ── factor ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("quality,multiply,average", [
    (0, 0.64, 0.8),
    (500, 1.0, 1.0),
    (1000, 1.44, 1.2),
])
def test_factor_both_rules(index, quality, multiply, average):
    q = {"EMITTER": quality, "BUS BARS": quality}
    assert mc.power_factor("Test Laser S1", q, "multiply", index) == pytest.approx(multiply)
    assert mc.power_factor("Test Laser S1", q, "average", index) == pytest.approx(average)


def test_factor_uneven_qualities(index):
    q = {"EMITTER": 1000, "BUS BARS": 0}
    assert mc.power_factor("Test Laser S1", q, "multiply", index) == pytest.approx(0.96)
    assert mc.power_factor("Test Laser S1", q, "average", index) == pytest.approx(1.0)


def test_quality_is_clamped_and_a_missing_part_counts_as_the_middle(index):
    assert mc.power_factor("Test Laser S1", {"EMITTER": 5000, "BUS BARS": -9}, "multiply", index) \
        == pytest.approx(1.2 * 0.8)
    assert mc.power_factor("Test Laser S1", {"EMITTER": 1000}, "multiply", index) == pytest.approx(1.2)
    assert mc.power_factor("Test Laser S1", None, "multiply", index) == pytest.approx(1.0)


def test_non_craftable_laser_has_no_factor(index):
    assert not mc.is_craftable("Some Other Laser", index)
    assert mc.power_factor("Some Other Laser", {"EMITTER": 1000, "BUS BARS": 1000}, "multiply", index) == 1.0
    assert mc.crafted_entry("Some Other Laser", {"EMITTER": 1000}, "multiply", index) is None


def test_default_rule_is_one_named_constant(index):
    q = {"EMITTER": 1000, "BUS BARS": 1000}
    assert mc.DEFAULT_COMBINE in mc.COMBINE_RULES
    assert mc.power_factor("Test Laser S1", q, None, index) \
        == mc.power_factor("Test Laser S1", q, mc.DEFAULT_COMBINE, index)
    assert mc.normalize_combine("nonsense") == mc.DEFAULT_COMBINE


def test_integrity_is_reported_separately(index):
    assert mc.integrity_factor("Test Laser S1", {"FRAME": 1000}, index) == pytest.approx(1.2)
    assert mc.integrity_factor("Some Other Laser", {"FRAME": 1000}, index) is None


# ── what a file reader applies ───────────────────────────────────────────────

def test_entry_stores_inputs_and_the_factor(index):
    entry = mc.crafted_entry("Test Laser S1", {"EMITTER": 750, "BUS BARS": 1000}, "multiply", index)
    assert entry == {"qualities": {"EMITTER": 750, "BUS BARS": 1000},
                     "power_factor": pytest.approx(1.1 * 1.2)}


def test_reader_with_blueprint_data_recomputes_from_the_inputs(index):
    stale = {"qualities": {"EMITTER": 1000, "BUS BARS": 1000}, "power_factor": 9.9}
    assert mc.resolve_power_factor("Test Laser S1", stale, "multiply", index) == pytest.approx(1.44)
    assert mc.resolve_power_factor("Test Laser S1", stale, "average", index) == pytest.approx(1.2)
    # The data says this laser cannot be crafted: the file's claim is ignored.
    assert mc.resolve_power_factor("Some Other Laser", stale, "multiply", index) == 1.0


def test_reader_without_blueprint_data_falls_back_to_the_stored_factor():
    crafted = {"qualities": {"EMITTER": 1000, "BUS BARS": 1000}, "power_factor": 1.44}
    assert mc.resolve_power_factor("Test Laser S1", crafted, "multiply", {}) == pytest.approx(1.44)
    assert mc.resolve_power_factor("Test Laser S1", crafted, "multiply", None) == pytest.approx(1.44)
    for bad in (None, "x", -1, 0, float("nan"), float("inf"), True):
        assert mc.resolve_power_factor("Test Laser S1", {"power_factor": bad}, None, {}) == 1.0


def test_no_crafted_object_means_no_change(index):
    for absent in (None, "", [], 5):
        assert mc.resolve_power_factor("Test Laser S1", absent, "multiply", index) == 1.0


def test_clean_crafted():
    assert mc.clean_crafted(None) is None
    assert mc.clean_crafted("yes") is None
    assert mc.clean_crafted({"qualities": {"EMITTER": 700.4, "X": "bad", 3: 1}, "power_factor": "x"}) \
        == {"qualities": {"EMITTER": 700}}


# ── the cache file ───────────────────────────────────────────────────────────

def test_missing_cache_gives_an_empty_index_without_raising(tmp_path):
    assert mc.load_index(str(tmp_path / "nope" / "blueprints.json")) == {}


def test_unreadable_cache_gives_an_empty_index_without_raising(tmp_path):
    bad = tmp_path / "blueprints.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    assert mc.load_index(str(bad)) == {}


def test_load_index_reads_a_file(tmp_path):
    p = tmp_path / "blueprints.json"
    p.write_text(json.dumps(RECORDS), encoding="utf-8")
    assert list(mc.load_index(str(p))) == ["Test Laser S1"]


# ── the REAL pinned data (read-only) ─────────────────────────────────────────

REAL_LASER = "Arbor MH2 Mining Laser"


@pytest.fixture(scope="module")
def real_index():
    path = mc.default_blueprint_path()
    if not os.path.isfile(path):
        pytest.skip(f"real blueprint cache not on this machine: {path}")
    idx = mc.load_index(path)
    assert idx, f"{path} exists but no craftable mining laser could be read from it"
    return idx


@pytest.mark.parametrize("quality,multiply,average", [
    (0, 0.64, 0.8),
    (500, 1.0, 1.0),
    (1000, 1.44, 1.2),
])
def test_real_laser_factor(real_index, quality, multiply, average):
    assert REAL_LASER in real_index
    groups = real_index[REAL_LASER]["power"]
    assert [(g["key"], g["label"]) for g in groups] == [("EMITTER", "Laser Power"), ("BUS BARS", "Laser Power")]
    q = {g["key"]: quality for g in groups}
    assert mc.power_factor(REAL_LASER, q, "multiply", real_index) == pytest.approx(multiply)
    assert mc.power_factor(REAL_LASER, q, "average", real_index) == pytest.approx(average)


def test_real_data_has_the_seventeen_lasers_all_the_same_shape(real_index):
    assert len(real_index) == 17
    shapes = {
        tuple((g["key"], g["q_min"], g["q_max"], g["at_min"], g["at_max"]) for g in e["power"])
        for e in real_index.values()
    }
    assert shapes == {(("EMITTER", 0.0, 1000.0, 0.8, 1.2), ("BUS BARS", 0.0, 1000.0, 0.8, 1.2))}
