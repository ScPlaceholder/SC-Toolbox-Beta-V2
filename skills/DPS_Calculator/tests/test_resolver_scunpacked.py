"""erkul_item_resolver falls back to scunpacked-data when erkul cannot answer.

Before this, an expired erkul cache plus the disabled erkul network left the index
EMPTY, so bespoke racks, UUID turrets and PDC housings resolved to nothing.
"""

import json
import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import erkul_item_resolver as R  # noqa: E402

RACK_REF = "11111111-2222-3333-4444-555555555555"
TURRET_REF = "66666666-7777-8888-9999-000000000000"
TRACTOR_REF = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _fixture(tmp_path):
    items = [
        {"className": "MRCK_S03_Test", "name": "Test Rack", "type": "MissileLauncher",
         "subType": "MissileRack", "reference": RACK_REF,
         "stdItem": {"MissileRack": {"MissileCount": 4, "MissileSize": 3}}},
        {"className": "Test_Remote_Turret", "name": "Remote Turret", "type": "Turret",
         "subType": "GunTurret", "reference": TURRET_REF,
         "stdItem": {"Ports": [{"Types": ["WeaponGun"]}, {"Types": ["WeaponGun"]},
                               {"Types": ["Turret"]}]}},
        {"className": "Test_Tractor_Turret", "name": "Tractor Beam", "type": "Turret",
         "subType": "Utility", "reference": TRACTOR_REF, "stdItem": {}},
        {"className": "TURRET_PDC_TEST_A", "name": "PDC", "type": "Turret",
         "subType": "PDCTurret", "stdItem": {}},
    ]
    ships = [{"ClassName": "TEST_Ship", "Loadout": [
        {"ClassName": "turret_pdc_test_a", "Loadout": [
            {"ClassName": "BEHR_LaserRepeater_PDC_S1", "Type": "WeaponGun.Gun"}]}]}]
    (tmp_path / "ship-items.json").write_text(json.dumps(items), encoding="utf-8")
    (tmp_path / "ships.json").write_text(json.dumps(ships), encoding="utf-8")
    return R._build_scunpacked(str(tmp_path))


def _use(idx, monkeypatch):
    monkeypatch.setattr(R, "_INDEX", idx)


def test_rack_capacity_from_missile_count(tmp_path, monkeypatch):
    _use(_fixture(tmp_path), monkeypatch)
    assert R.rack_capacity(RACK_REF) == 4


def test_turret_gun_ports_counts_only_gun_ports(tmp_path, monkeypatch):
    _use(_fixture(tmp_path), monkeypatch)
    assert R.turret_gun_ports(TURRET_REF) == 2


def test_pdc_default_gun_from_ship_loadout(tmp_path, monkeypatch):
    _use(_fixture(tmp_path), monkeypatch)
    assert R.turret_default_gun("turret_pdc_test_a") == "behr_laserrepeater_pdc_s1"


def test_tractor_turret_is_utility(tmp_path, monkeypatch):
    _use(_fixture(tmp_path), monkeypatch)
    assert R.is_nonweapon_utility(TRACTOR_REF) is True
    assert R.is_nonweapon_utility(TURRET_REF) is False


def test_load_falls_back_when_erkul_cannot_answer(tmp_path, monkeypatch):
    idx = _fixture(tmp_path)
    monkeypatch.setattr(R, "ITEMS_CACHE", tmp_path / "no_such_cache.json")
    monkeypatch.setattr(R, "_network_allowed", lambda: False)
    monkeypatch.setattr(R, "_build_scunpacked", lambda d=None: idx)
    got = R._load()
    assert got.get(RACK_REF, {}).get("port_count") == 4
    assert not (tmp_path / "no_such_cache.json").exists()   # never written into erkul's cache
