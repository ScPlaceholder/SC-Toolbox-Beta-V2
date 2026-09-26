"""Craft Database on the pinned scunpacked-data blueprints (datamine).

Offscreen, on a small fixture in the shape of the real ``blueprints.json``
(build 4.10.1-LIVE.12660092). Every cache directory is a temp dir and
``scunpacked.cache_dir`` is redirected, so nothing is written to
~/.sctoolbox and nothing touches the network (urlopen is replaced).
"""
import hashlib
import json
import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from shared import scunpacked
from data import datamine
from data.datamine import DatamineSource, build_index, normalize
from data.repository import BlueprintQuery, CraftRepository, query_blueprints
from domain.models import Blueprint


_REAL_CACHE = os.path.join(os.path.expanduser("~"), ".sctoolbox")


def _snapshot(root):
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            out[p] = os.stat(p).st_mtime_ns
    return out


_BEFORE = _snapshot(_REAL_CACHE)


def _mod(name, lo=0.9, hi=1.1):
    return {"UUID": "m-" + name, "Key": name.lower(), "Name": name,
            "QualityRange": {"Min": 0, "Max": 1000},
            "ModifierRange": {"AtMinQuality": lo, "AtMaxQuality": hi},
            "ValueRangeType": "linear"}


def _group(name, children, mods=(), count=1):
    return {"Kind": "group", "Key": name.upper(), "Name": name, "RequiredCount": count,
            "Modifiers": list(mods), "Children": children}


def _res(name, scu, q=1):
    return {"Kind": "resource", "UUID": "r-" + name, "Name": name, "QuantityScu": scu,
            "MinQuality": q}


def _bp(key, out, groups, default=False, pools=(), secs=540):
    return {"UUID": "u-" + key, "Key": key, "Kind": "creation", "CategoryUUID": "c",
            "Output": out,
            "Availability": {"Default": default,
                             "RewardPools": [{"UUID": "p", "Key": k} for k in pools]},
            "Tiers": [{"TierIndex": 0, "CraftTimeSeconds": secs,
                       "Requirements": {"Kind": "root", "Children": groups}}],
            "Dismantle": {"TimeSeconds": 15, "Efficiency": 0.5, "Returns": []}}


FIXTURE = [
    _bp("BP_CRAFT_AMRS_LaserCannon_S1",
        {"UUID": "o1", "Class": "amrs_lasercannon_s1", "Type": "WeaponGun", "Subtype": "Gun",
         "Grade": "1", "Name": "Omnisky III Cannon"},
        [_group("Frame", [_res("Agricium", 0.36)], [_mod("Integrity")]),
         _group("Emitter", [{"Kind": "item", "UUID": "i-h", "Name": "Hadanite",
                             "Quantity": 7, "MinQuality": 1}], [_mod("Impact Force", 0.95, 1.05)])],
        pools=("BP_REWARDS_NyxFoxwellEasy",)),
    _bp("BP_CRAFT_POWR_ACOM_S01_LumaCore_SCItem",
        {"UUID": "o2", "Class": "powr_acom_s01_lumacore_scitem", "Type": "PowerPlant",
         "Subtype": "Power", "Grade": "3", "Name": "LumaCore"},
        [_group("Core", [_res("Tungsten", 0.12)],
                [{"UUID": "pp", "Key": "itemresource_powergeneration", "Name": "Power Pips",
                  "QualityRange": {"Min": 0, "Max": 249}, "ModifierRange": [],
                  "ValueRangeType": "linear_integer_additive",
                  "ValueSegments": [
                      {"QualityMin": 0, "QualityMax": 499, "AdditiveAtStart": -1, "AdditiveAtEnd": -1},
                      {"QualityMin": 500, "QualityMax": 1000, "AdditiveAtStart": 2, "AdditiveAtEnd": 2}]},
                 {"UUID": "hp", "Key": "health_maxhealth", "Name": "Integrity",
                  "QualityRange": {"Min": 0, "Max": 500},
                  "ModifierRange": {"AtMinQuality": 0.8, "AtMaxQuality": 1},
                  "ValueRangeType": "linear",
                  "ValueSegments": [
                      {"QualityMin": 0, "QualityMax": 500, "ModifierAtStart": 0.8, "ModifierAtEnd": 1},
                      {"QualityMin": 501, "QualityMax": 1000, "ModifierAtStart": 1, "ModifierAtEnd": 1.2}]}])]),
    _bp("BP_CRAFT_thp_light_legs_01_01_01",
        {"UUID": "o3", "Class": "thp_light_legs_01_01_01", "Type": "Char_Armor_Legs",
         "Subtype": "Light", "Grade": "1", "Name": "Aztalan Legs"},
        [_group("<= PLACEHOLDER =>", [
            _group("Segment Paneling", [_res("Taranite", 0.03, 0)], [_mod("Damage Mitigation")]),
            _group("Insulative Liner", [_res("Aslarite", 0.02, 0)], [_mod("Max Temp", 0.8, 1.2)]),
            _group("Panel Covering", [_res("Lindinium", 0.03, 0)],
                   [{"UUID": "e", "Key": "armor_damagemitigation", "Name": "Damage Mitigation",
                     "QualityRange": [], "ModifierRange": []}]),
        ], count=2)], default=True, secs=140),
    _bp("BP_CRAFT_COOL_S04_CNOU_Pioneer", [],
        [_group("Shell", [_res("Iron", 1.5)], [_mod("Integrity")])]),
]


def _write_raw(d, records=FIXTURE):
    os.makedirs(d, exist_ok=True)
    data = json.dumps(records).encode("utf-8")
    with open(os.path.join(d, "blueprints.json"), "wb") as f:
        f.write(data)
    return data


@pytest.fixture(autouse=True)
def _no_home_writes(tmp_path, monkeypatch):
    """Redirect the shared cache and forbid the network in every test."""
    home = tmp_path / "home"
    monkeypatch.setattr(scunpacked, "cache_dir",
                        lambda build=scunpacked.BUILD: str(home / "scunpacked" / build))

    def _no_net(*a, **k):
        raise AssertionError("network access in a test")
    monkeypatch.setattr(scunpacked.urllib.request, "urlopen", _no_net)
    return home


def _source(tmp_path, build="9.9.9-TEST.1"):
    return DatamineSource(cache_dir=str(tmp_path / "cache"), build=build)


# ── index build ──────────────────────────────────────────────────────────

def test_index_build_normalises_the_datamine():
    idx = build_index(FIXTURE)
    assert idx["index_version"] == datamine.INDEX_VERSION
    assert idx["stats"] == {"totalBlueprints": 4, "uniqueIngredients": 7,
                            "version": "LIVE-4.10.1-12660092"}
    names = [b["name"] for b in idx["blueprints"]]
    assert names == sorted(names, key=str.lower)            # sorted by name
    by = {b["blueprint_id"]: b for b in idx["blueprints"]}
    assert by["BP_CRAFT_AMRS_LaserCannon_S1"]["category"] == "Ship Weapons / Laser Cannon"
    assert by["BP_CRAFT_POWR_ACOM_S01_LumaCore_SCItem"]["category"] == "Ship Components / Power Plant"
    assert by["BP_CRAFT_thp_light_legs_01_01_01"]["category"] == "Armour / Light / Legs"
    # the record whose Output is [] still gets a name and a category
    broken = by["BP_CRAFT_COOL_S04_CNOU_Pioneer"]
    assert broken["name"] == "COOL S04 CNOU Pioneer"
    assert broken["category"] == "Ship Components / Cooler"
    assert set(idx["hints"]["category"]) == {b["category"] for b in idx["blueprints"]}
    assert [r["name"] for r in idx["hints"]["resource"]] == sorted(
        ["Agricium", "Hadanite", "Tungsten", "Taranite", "Aslarite", "Lindinium", "Iron"])
    # obtainable = default OR a reward pool
    assert by["BP_CRAFT_AMRS_LaserCannon_S1"]["obtainable"] is True
    assert by["BP_CRAFT_AMRS_LaserCannon_S1"]["sources"] == ["Nyx Foxwell Easy"]
    assert by["BP_CRAFT_thp_light_legs_01_01_01"]["obtainable"] is True
    assert by["BP_CRAFT_POWR_ACOM_S01_LumaCore_SCItem"]["obtainable"] is False


def test_nested_choice_group_and_empty_modifier():
    b = normalize(FIXTURE[2])
    assert [s["slot"] for s in b["ingredients"]] == [
        "Segment Paneling", "Insulative Liner", "Panel Covering"]
    assert all(s["choose"] == "any 2 of 3" for s in b["ingredients"])
    assert b["ingredients"][2]["quality_effects"] == []     # empty ranges carry no effect


# ── recipe lookup with materials ─────────────────────────────────────────

def test_recipe_lookup_with_materials(tmp_path):
    src = _source(tmp_path)
    _write_raw(src.dir)
    repo = CraftRepository(src)
    assert repo.load() is True
    bp = repo.find("BP_CRAFT_AMRS_LaserCannon_S1")
    assert isinstance(bp, Blueprint)
    assert bp.name == "Omnisky III Cannon"
    assert bp.craft_time_seconds == 540 and bp.craft_time_display == "9m"
    frame, emitter = bp.ingredients
    assert (frame.slot, frame.name, frame.quantity_scu, frame.unit) == ("Frame", "Agricium", 36.0, "cSCU")
    assert frame.options[0].min_quality == 1
    assert (emitter.name, emitter.quantity_scu, emitter.unit) == ("Hadanite", 7.0, "pcs")
    assert frame.quality_effects[0].stat == "Integrity"
    assert frame.quality_effects[0].pct_at(1000) == pytest.approx(10.0)
    assert bp.dismantle_seconds == 15 and bp.dismantle_efficiency == 0.5

    pp = repo.find("BP_CRAFT_POWR_ACOM_S01_LumaCore_SCItem")
    pips, integ = pp.ingredients[0].quality_effects
    assert pips.additive and pips.label_at(100) == "-1" and pips.label_at(900) == "+2"
    assert integ.pct_at(250) == pytest.approx(-10.0)       # segment 1: 0.8 -> 1.0
    assert integ.pct_at(1000) == pytest.approx(20.0)       # segment 2: 1.0 -> 1.2


# ── search ───────────────────────────────────────────────────────────────

def test_search_filters_and_pages(tmp_path):
    bps = [Blueprint.from_dict(d) for d in build_index(FIXTURE)["blueprints"]]
    ids = lambda q: [b.blueprint_id for b in query_blueprints(bps, q)[0]]
    assert ids(BlueprintQuery(search="omnisky", ownable=None)) == ["BP_CRAFT_AMRS_LaserCannon_S1"]
    assert ids(BlueprintQuery(search="hadanite", ownable=None)) == ["BP_CRAFT_AMRS_LaserCannon_S1"]
    assert ids(BlueprintQuery(search="lumacore", ownable=None)) == ["BP_CRAFT_POWR_ACOM_S01_LumaCore_SCItem"]
    assert ids(BlueprintQuery(resource="Taranite", ownable=None)) == ["BP_CRAFT_thp_light_legs_01_01_01"]
    assert len(ids(BlueprintQuery(category="Ship Components", ownable=None))) == 2
    assert len(ids(BlueprintQuery(ownable=True))) == 2      # the two obtainable ones
    chunk, pag = query_blueprints(bps, BlueprintQuery(limit=3, page=2, ownable=None))
    assert (pag.total, pag.pages, pag.page, len(chunk)) == (4, 2, 2, 1)


# ── offline with a cache ─────────────────────────────────────────────────

def test_offline_with_cache_uses_the_index_only(tmp_path):
    src = _source(tmp_path)
    _write_raw(src.dir)
    assert src.status() == "raw"
    src.load()                                   # builds craft_index.json once
    assert src.status() == "ready"
    os.remove(src.raw_path)                      # raw gone, network forbidden (fixture)
    repo = CraftRepository(_source(tmp_path))
    assert repo.load() is True and not repo.is_missing()
    assert repo.get_stats().total_blueprints == 4


def test_download_checks_pinned_sha256(tmp_path, monkeypatch):
    payload = json.dumps(FIXTURE).encode("utf-8")

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return payload

    seen = []
    monkeypatch.setattr(scunpacked.urllib.request, "urlopen",
                        lambda req, timeout=0: seen.append(req.full_url) or _Resp())
    # the real pinned build: the fixture is not the pinned file -> refused and removed
    real = DatamineSource(cache_dir=str(tmp_path / "real"))
    with pytest.raises(scunpacked.ScunpackedError, match="sha256"):
        real.download()
    assert not os.path.exists(real.raw_path)
    assert seen[0] == (f"https://raw.githubusercontent.com/{scunpacked.REPO}/"
                       f"{scunpacked.COMMIT}/blueprints.json")
    # an unpinned test build downloads, records the hash and indexes
    src = _source(tmp_path)
    idx = src.download()
    assert idx["stats"]["totalBlueprints"] == 4
    meta = json.load(open(os.path.join(src.dir, "meta.json"), encoding="utf-8"))
    assert meta["files"]["blueprints.json"]["sha256"] == hashlib.sha256(payload).hexdigest()


# ── the window ───────────────────────────────────────────────────────────

@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    from shared.qt.theme import apply_theme
    app = QApplication.instance() or QApplication([])
    apply_theme(app)
    return app


def _window(qapp, cache_root=None):
    from ui.app import CraftDatabaseApp
    win = CraftDatabaseApp()
    deadline = time.time() + 10
    while win._repo.is_loading() or not (win._repo.is_loaded() or win._repo.get_error()):
        qapp.processEvents()
        if time.time() > deadline:
            break
        time.sleep(0.01)
    qapp.processEvents()
    return win


def test_no_cache_says_so_and_offers_download(qapp, _no_home_writes):
    win = _window(qapp)
    try:
        assert win._repo.is_missing()
        assert not win._nodata.isHidden()
        assert "not downloaded yet" in win._nodata_lbl.text()
        assert not win._download_btn.isHidden() and win._download_btn.isEnabled()
        assert win._version_lbl.text() == "Game data: 4.10.1-LIVE"
    finally:
        win.hide()
        win.deleteLater()
    assert not os.path.exists(scunpacked.cache_dir())       # nothing written, nothing fetched


def test_window_offline_with_cache_shows_blueprints(qapp, _no_home_writes):
    d = scunpacked.cache_dir()
    _write_raw(d)
    # pin the fixture's own hash for this test so the real-build path accepts it
    sha = hashlib.sha256(json.dumps(FIXTURE).encode("utf-8")).hexdigest()
    old = dict(scunpacked.PINNED_SHA256)
    scunpacked.PINNED_SHA256["blueprints.json"] = sha
    try:
        win = _window(qapp)
        try:
            assert win._repo.is_loaded()
            assert win._nodata.isHidden()
            assert win._bp_count_lbl.text() == "4"
            assert win._result_lbl.text() == "2 results"        # "Obtainable" is on
            assert win._version_lbl.text() == "Game data: 4.10.1-LIVE"
            # the mission filters are hidden: the datamine has no missions
            assert win._filter_panel._mission_combo.isHidden()
            assert os.path.isfile(os.path.join(d, datamine.INDEX_FILE))
        finally:
            win.hide()
            win.deleteLater()
    finally:
        scunpacked.PINNED_SHA256.clear()
        scunpacked.PINNED_SHA256.update(old)


def test_build_label():
    assert datamine.game_label("4.10.1-LIVE.12660092") == "Game data: 4.10.1-LIVE"
    assert datamine.version_string("4.10.1-LIVE.12660092") == "LIVE-4.10.1-12660092"
    assert DatamineSource(cache_dir="x").label() == "Game data: 4.10.1-LIVE"


def test_zz_nothing_written_to_the_real_cache():
    assert _snapshot(_REAL_CACHE) == _BEFORE
