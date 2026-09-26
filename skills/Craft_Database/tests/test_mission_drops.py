"""The mission drop join — on synthetic caches, so the maths is checkable by hand.

Nothing here reads the real 40 MB ``.scmdb_cache.json`` or the network. Each test writes a
small cache into a temp dir in the shape of the real one and asserts against numbers you can
verify by reading the fixture.

Every test below corresponds to a defect that actually occurred while building this, not to a
line I wanted to cover:

  * ``test_picks_richest_cache_not_first_filename`` — the unsuffixed "current" cache can have
    an EMPTY blueprintPools. Choosing by filename silently produced no drops at all.
  * ``test_joins_on_uuid_not_name`` — item names drift between game versions; the UUID join
    is exact where the name join orphans records.
  * ``test_unnamed_slot_keeps_its_weight`` — normalising the unnamed slot away would inflate
    every real chance. The named shares MUST sum to less than one here.
  * ``test_unknown_blueprint_returns_empty`` — "no data" and "does not drop" are different
    claims and the empty list means the first.
  * ``test_localisation_keys_are_dropped`` — 9 real location pools are named '@something'.
  * ``test_normalize_without_drops_is_unchanged`` — the hook must not disturb existing callers.
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from data import mission_drops
from data.datamine import normalize
from domain.models import Mission

BP_A = "uuid-blueprint-a"
BP_B = "uuid-blueprint-b"
BP_C = "uuid-blueprint-c"
OUT_A = "uuid-output-a"
POOL = "uuid-pool-1"


def _cache(pools=None, contracts=None, version="test-1"):
    """A cache in the real shape. Weights 1, 1, 2 named plus 1 UNNAMED -> total 5."""
    return {
        "version": version,
        "locationPools": {
            "loc1": {"name": "Stanton"},
            "loc2": {"name": "HUR L1"},
            "loc3": {"name": "@generic_locations_blank"},
        },
        "factions": {"fac1": {"name": "Covalex Shipping"}},
        "blueprintPools": pools if pools is not None else {
            POOL: {
                "name": "BP_REWARDS_Test",
                "blueprints": [
                    {"blueprintRecord": BP_A, "entityClass": OUT_A, "weight": 1,
                     "name": "Alpha"},
                    {"blueprintRecord": BP_B, "weight": 1, "name": "Beta"},
                    {"blueprintRecord": BP_C, "weight": 2, "name": "Gamma"},
                    {"weight": 1},                      # the UNNAMED slot: real, keeps weight
                ],
            },
        },
        "contracts": contracts if contracts is not None else [
            {
                "title": "Recover the Cargo",
                "debugName": "Covalex_Test_RecoverCargo",
                "missionType": "Hauling",
                "category": "career",
                "illegal": False,
                "timeToComplete": 30,
                "factionGuid": "fac1",
                "locations": ["loc1", "loc2", "loc3"],
                "description": "Go and get it.",
                "blueprintRewards": [{"blueprintPool": POOL, "chance": 0.5,
                                      "poolName": "BP_REWARDS_Test"}],
            },
        ],
    }


def _write(directory, name, doc):
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)
    return path


@pytest.fixture()
def drops(tmp_path):
    _write(str(tmp_path), ".scmdb_cache_test.json", _cache())
    d = mission_drops.load(str(tmp_path))
    assert d is not None, "fixture cache should load"
    return d


# ── choosing the cache ───────────────────────────────────────────────────────


def test_picks_richest_cache_not_first_filename(tmp_path):
    """An EMPTY blueprintPools must lose, even when its name sorts first.

    This is the real failure: `.scmdb_cache.json` sorts before every suffixed name and in one
    checkout holds zero pools, so a filename-ordered pick returned nothing while appearing to
    work. Content is the only honest selector.
    """
    _write(str(tmp_path), ".scmdb_cache.json", _cache(pools={}, contracts=[]))
    _write(str(tmp_path), ".scmdb_cache_4_7_0_ptu_1.json", _cache())
    path, doc = mission_drops.pick_cache(str(tmp_path))
    assert os.path.basename(path) == ".scmdb_cache_4_7_0_ptu_1.json"
    assert len(doc["blueprintPools"]) == 1


def test_no_usable_cache_returns_none(tmp_path):
    """A missing or pool-less cache is survivable: the skill shows no drops, it does not crash."""
    assert mission_drops.pick_cache(str(tmp_path)) == (None, None)
    assert mission_drops.load(str(tmp_path)) is None
    _write(str(tmp_path), ".scmdb_cache.json", _cache(pools={}, contracts=[]))
    assert mission_drops.load(str(tmp_path)) is None


def test_corrupt_cache_does_not_raise(tmp_path):
    """One unreadable cache must not take the others down with it."""
    with open(os.path.join(str(tmp_path), ".scmdb_cache_bad.json"), "w", encoding="utf-8") as fh:
        fh.write("{not json")
    _write(str(tmp_path), ".scmdb_cache_good.json", _cache())
    d = mission_drops.load(str(tmp_path))
    assert d is not None and d.blueprint_count == 3


# ── the join ─────────────────────────────────────────────────────────────────


def test_joins_on_uuid_not_name(drops):
    """The record UUID is the key. A name is not, and names drift between versions."""
    assert drops.for_blueprint(BP_A), "blueprintRecord UUID must resolve"
    assert not drops.for_blueprint("Alpha"), "the item NAME must not be a join key"


def test_entity_class_is_a_secondary_key(drops):
    """A caller holding only the output item's UUID can still find the missions."""
    assert drops.for_blueprint(None, OUT_A)
    assert drops.for_blueprint("nope", OUT_A)


def test_unknown_blueprint_returns_empty(drops):
    """Empty means NO DATA. It must never be confused with 'does not drop'."""
    assert drops.for_blueprint("uuid-not-in-any-pool") == []
    assert drops.for_blueprint(None) == []


def test_drop_chance_is_pool_chance_times_weight_share(drops):
    """chance * weight/total. Total is 5 (1+1+2 named plus the unnamed 1), chance is 0.5."""
    assert drops.for_blueprint(BP_A)[0]["drop_chance"] == pytest.approx(0.5 * 1 / 5)
    assert drops.for_blueprint(BP_C)[0]["drop_chance"] == pytest.approx(0.5 * 2 / 5)


def test_unnamed_slot_keeps_its_weight(drops):
    """Named chances sum to LESS than the pool chance, and the remainder is reported.

    4 of the 5 weight is named, so the named shares come to 0.8 of the pool, not 1.0.
    Normalising that away would inflate every named blueprint's chance to cover an outcome
    that is not a blueprint.
    """
    named = sum(drops.for_blueprint(b)[0]["drop_chance"] for b in (BP_A, BP_B, BP_C))
    assert named == pytest.approx(0.5 * 4 / 5)
    assert named < 0.5
    assert drops.unattributed_share(POOL) == pytest.approx(1 / 5)


def test_a_pool_no_contract_awards_yields_nothing(tmp_path):
    """A pool can list blueprints and be reachable from NO mission.

    This is why measured coverage (645) is lower than pool membership suggests (666): 29 of
    the 116 real pools are awarded by no contract. Joining the pool is not the same as a
    mission giving the item.
    """
    _write(str(tmp_path), ".scmdb_cache_x.json", _cache(contracts=[]))
    d = mission_drops.load(str(tmp_path))
    assert d is not None
    assert d.pool_count == 1 and d.linked_pool_count == 0
    assert d.for_blueprint(BP_A) == []


def test_missing_chance_is_omitted_not_guessed(tmp_path):
    """No chance field means no probability. Omit the row rather than invent 1.0 or 0.0."""
    doc = _cache()
    doc["contracts"][0]["blueprintRewards"][0].pop("chance")
    _write(str(tmp_path), ".scmdb_cache_x.json", doc)
    d = mission_drops.load(str(tmp_path))
    assert d.for_blueprint(BP_A) == []


# ── presentation ─────────────────────────────────────────────────────────────


def test_localisation_keys_are_dropped(drops):
    """9 real location pools are named '@something'. A raw key in the UI reads as a bug."""
    locations = drops.for_blueprint(BP_A)[0]["locations"]
    assert "Stanton" in locations and "HUR L1" in locations
    assert "@" not in locations


def test_locations_say_when_they_are_truncated(tmp_path):
    """Showing six of forty without saying so implies the mission is only offered in six."""
    doc = _cache()
    doc["locationPools"] = {"l%d" % i: {"name": "Place%d" % i} for i in range(20)}
    doc["contracts"][0]["locations"] = ["l%d" % i for i in range(20)]
    _write(str(tmp_path), ".scmdb_cache_x.json", doc)
    d = mission_drops.load(str(tmp_path))
    loc = d.for_blueprint(BP_A)[0]["locations"]
    assert loc.count(",") == mission_drops.LOCATION_CAP - 1
    assert "(+%d more)" % (20 - mission_drops.LOCATION_CAP) in loc


def test_rows_feed_the_mission_model(drops):
    """The dicts must be exactly what domain.models.Mission.from_dict reads."""
    m = Mission.from_dict(drops.for_blueprint(BP_C)[0])
    assert m.name == "Recover the Cargo"
    assert m.contractor == "Covalex Shipping"
    assert m.mission_type == "Hauling"
    assert m.lawful == 1
    assert m.time_to_complete_minutes == 30
    assert m.drop_pct == "20%"           # 0.5 * 2/5


def test_illegal_contract_is_not_lawful(tmp_path):
    """The cache says `illegal`; the model wants `lawful`. Inverting it is easy to get wrong."""
    doc = _cache()
    doc["contracts"][0]["illegal"] = True
    _write(str(tmp_path), ".scmdb_cache_x.json", doc)
    d = mission_drops.load(str(tmp_path))
    assert Mission.from_dict(d.for_blueprint(BP_A)[0]).lawful == 0


def test_best_chance_first(tmp_path):
    """'Where do I get this' is answered by the likeliest source, so sort by chance."""
    doc = _cache()
    doc["contracts"].append({
        "title": "A Worse Bet", "missionType": "Hauling", "factionGuid": "fac1",
        "locations": ["loc1"],
        "blueprintRewards": [{"blueprintPool": POOL, "chance": 0.1}],
    })
    _write(str(tmp_path), ".scmdb_cache_x.json", doc)
    d = mission_drops.load(str(tmp_path))
    rows = d.for_blueprint(BP_A)
    assert [r["name"] for r in rows] == ["Recover the Cargo", "A Worse Bet"]
    assert rows[0]["drop_chance"] > rows[1]["drop_chance"]


# ── the hook into normalize() ────────────────────────────────────────────────


_RAW = {
    "UUID": BP_A,
    "Key": "BP_CRAFT_Test_Alpha",
    "Output": {"UUID": OUT_A, "Name": "Alpha", "Type": "WeaponGun", "Class": "alpha"},
    "Availability": {"Default": False, "RewardPools": [{"Key": "BP_REWARDS_Test"}]},
    "Tiers": [{"CraftTimeSeconds": 60, "Requirements": {"Children": []}}],
    "Dismantle": {},
}


def test_normalize_without_drops_is_unchanged():
    """The default must leave existing callers and the existing tests alone."""
    assert normalize(dict(_RAW), 0, "v")["missions"] == []


def test_normalize_with_drops_fills_missions(drops):
    rec = normalize(dict(_RAW), 0, "v", drops)
    assert len(rec["missions"]) == 1
    assert rec["missions"][0]["contractor"] == "Covalex Shipping"
    # sources (the pool labels) must survive alongside the richer missions, not be replaced
    assert rec["sources"]


def test_normalize_keeps_missions_empty_for_an_unknown_blueprint(drops):
    raw = dict(_RAW, UUID="uuid-unknown", Output=dict(_RAW["Output"], UUID="uuid-unknown-out"))
    assert normalize(raw, 0, "v", drops)["missions"] == []
