"""Component pickers must obey the whole port rule, not just its size ceiling.

Before this suite existed, ``ComponentRepository._list_for_size`` filtered on
``size <= max_size`` alone.  Measured over the cached scunpacked build, across
310 ships and the 2,125 slots of the five core component types (Shield, Cooler,
Radar, PowerPlant, QuantumDrive):

    1,820 slots offered an item BELOW the port's MinSize
      (1,000 of them an undersized item of size >= 1 -- a size-1 cooler in a
       size-3 bay; the rest differed only by size-0 rows)
       83 non-editable slots offered a swappable list, inviting a change the
          game does not allow (Idris-M shield port: 65 choices)

Every assertion here fails against that logic.  That is checkable rather than
asserted -- set ``DPS_PREFIX_LOGIC=1`` and the pre-fix rule is installed over
the real one for the whole run:

    DPS_PREFIX_LOGIC=1 python -m pytest skills/DPS_Calculator/tests/test_component_port_fit.py

A test that the bug passes is not a regression test, so any case that survives
that run is not protecting anything and should be read as a gap.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import repository as R  # noqa: E402


# ── the pre-fix rule, for the falsifiability run ──────────────────────────────

def _prefix_list_for_size(self, by_name, max_size, **_ignored):
    """``_list_for_size`` exactly as it stood before the fix.

    The new keyword arguments are swallowed: the old signature had none, so a
    caller could not have passed them, and ignoring them reproduces the old
    DECISION rather than merely the old text.
    """
    return sorted(
        [v for v in by_name.values()
         if v["size"] <= max_size and v.get("listable") is not False],
        key=lambda x: (-x["size"], x["name"]),
    )


PREFIX = os.environ.get("DPS_PREFIX_LOGIC") == "1"
if PREFIX:
    R.ComponentRepository._list_for_size = _prefix_list_for_size


# ── fixtures (plain helpers, as elsewhere in this directory) ──────────────────

def _row(local_name, size, name=None, ref=None, listable=None, required_tags=""):
    r = {"local_name": local_name, "size": size,
         "name": name or local_name.upper(),
         "ref": ref or f"ref-{local_name}",
         "required_tags": required_tags}
    if listable is not None:
        r["listable"] = listable
    return r


#  One size-0..size-4 cooler each, keyed the way _run_scunpacked keys them.
COOLERS = {f"{r['local_name']}_{r['size']}": r for r in [
    _row("cool_s0", 0), _row("cool_s1", 1), _row("cool_s2", 2),
    _row("cool_s3", 3), _row("cool_s4", 4),
]}


def _repo(**by_name):
    """A ComponentRepository with a hand-built index and no __init__ -- no
    network, no Qt, no background thread."""
    repo = object.__new__(R.ComponentRepository)
    snap = R._IndexSnapshot()
    for attr, d in by_name.items():
        setattr(snap, attr, d)
    repo._idx = snap
    return repo


def _ship(port_name, min_size, max_size, editable=True,
          local_name="", required_tags=None):
    """A ship record in the shape get_ship_data returns: one component port,
    erkul-shaped, as data/scunpacked_provider._translate emits it."""
    port = {"itemPortName": port_name,
            "itemTypes": [{"type": "Cooler", "subType": ""}],
            "editable": editable, "minSize": min_size, "maxSize": max_size,
            "localName": local_name, "localReference": ""}
    if required_tags:
        port["requiredTags"] = required_tags
    return {"name": "Test Ship", "loadout": [port]}


def _slot(port_name, max_size, editable=True, local_ref=""):
    """A slot as services.slot_extractor.extract_slots_by_type returns it for a
    component: five keys, and NO min_size -- that is the gap under test."""
    return {"id": port_name, "label": port_name, "max_size": max_size,
            "editable": editable, "local_ref": local_ref}


def _sizes(items):
    return sorted(i["size"] for i in items)


# ── the size range ───────────────────────────────────────────────────────────

class TestSizeRange:

    def test_undersized_item_is_not_offered(self):
        """A size-3 bay must not offer a size-1 cooler."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 3),
            _ship("hardpoint_cooler", min_size=3, max_size=3))
        assert _sizes(got) == [3]

    def test_a_range_port_offers_its_whole_range_and_no_more(self):
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 3),
            _ship("hardpoint_cooler", min_size=2, max_size=3))
        assert _sizes(got) == [2, 3]

    def test_oversized_item_is_still_not_offered(self):
        """The half of the rule that already worked, pinned so the fix cannot
        trade one failure for the other."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 2),
            _ship("hardpoint_cooler", min_size=0, max_size=2))
        assert max(_sizes(got)) == 2

    def test_min_size_zero_rejects_nothing(self):
        """MinSize 0 is the data saying any size fits; it must not be read as
        'exact fit' just because MinSize is usually MaxSize."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 4),
            _ship("hardpoint_cooler", min_size=0, max_size=4))
        assert _sizes(got) == [0, 1, 2, 3, 4]

    def test_stock_item_below_min_size_is_still_offered(self):
        """shared/scunpacked.py's rule exempts the port's own stock item from
        MinSize -- CIG fits a few undersized stock components, and a picker that
        hid the part actually installed would be worse than one that offers too
        much."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 3, local_ref="cool_s1"),
            _ship("hardpoint_cooler", min_size=3, max_size=3))
        assert _sizes(got) == [1, 3]


# ── the lock ─────────────────────────────────────────────────────────────────

class TestNonEditablePort:

    def test_offers_only_its_fitted_item(self):
        """Read-only, not absent: one row naming what the game has fitted."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler",
            _slot("hardpoint_cooler", 3, editable=False, local_ref="cool_s3"),
            _ship("hardpoint_cooler", min_size=3, max_size=3, editable=False))
        assert [i["local_name"] for i in got] == ["cool_s3"]

    def test_resolves_the_fitted_item_by_uuid_too(self):
        """A slot's local_ref is a localName under scunpacked and a UUID under
        erkul; both must find the stock row."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler",
            _slot("hardpoint_cooler", 3, editable=False, local_ref="REF-cool_s3"),
            _ship("hardpoint_cooler", min_size=3, max_size=3, editable=False))
        assert [i["local_name"] for i in got] == ["cool_s3"]

    def test_empty_locked_port_offers_nothing(self):
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 3, editable=False),
            _ship("hardpoint_cooler", min_size=3, max_size=3, editable=False))
        assert got == []

    def test_the_lock_comes_from_the_port_not_the_slot(self):
        """extract_slots_by_type carries `editable`, but the port tree is the
        source of truth and a stale slot dict must not unlock a locked port."""
        repo = _repo(coolers_by_name=COOLERS)
        got = repo.components_for_slot(
            "Cooler",
            _slot("hardpoint_cooler", 3, editable=True, local_ref="cool_s3"),
            _ship("hardpoint_cooler", min_size=3, max_size=3, editable=False))
        assert [i["local_name"] for i in got] == ["cool_s3"]


# ── the plumbing gap ─────────────────────────────────────────────────────────

class TestPortConstraints:

    def test_min_size_is_recovered_from_the_loadout_tree(self):
        """The datum the extractor drops is still in the ship record."""
        slot = _slot("hardpoint_cooler", 3)
        assert "min_size" not in slot
        pc = R.ComponentRepository.port_constraints(
            _ship("hardpoint_cooler", min_size=2, max_size=3), slot)
        assert (pc["min_size"], pc["max_size"], pc["editable"]) == (2, 3, True)

    def test_required_tags_are_recovered_too(self):
        pc = R.ComponentRepository.port_constraints(
            _ship("hardpoint_cooler", 1, 1, required_tags="Starfarer_Base"),
            _slot("hardpoint_cooler", 1))
        assert pc["required_tags"] == "Starfarer_Base"

    def test_a_port_that_cannot_be_found_falls_back_to_the_slot(self):
        """No ship, or a renamed port: no worse than before, never an exception
        and never a silently emptied picker."""
        slot = _slot("hardpoint_cooler", 3)
        for ship in (None, {}, _ship("hardpoint_something_else", 1, 1)):
            pc = R.ComponentRepository.port_constraints(ship, slot)
            assert (pc["min_size"], pc["editable"]) == (0, True)

    def test_an_unknown_component_kind_is_an_error_not_an_empty_list(self):
        repo = _repo(coolers_by_name=COOLERS)
        with pytest.raises(ValueError):
            repo.components_for_slot("Nonesuch", _slot("x", 1), None)


# ── backward compatibility of the size-only call ─────────────────────────────

class TestSizeOnlyCallUnchanged:
    """Every existing *_for_size caller passes a size and nothing else.  Those
    must keep returning exactly what they returned before, or this fix is a
    behaviour change dressed as a bug fix."""

    def test_matches_the_pre_fix_rule_exactly(self):
        repo = _repo(coolers_by_name=COOLERS)
        for sz in range(0, 6):
            assert repo.coolers_for_size(sz) == \
                _prefix_list_for_size(repo, COOLERS, sz)

    def test_a_non_listable_row_is_never_offered(self):
        rows = dict(COOLERS)
        npc = _row("cool_npc", 3, listable=False)
        rows[f"{npc['local_name']}_{npc['size']}"] = npc
        repo = _repo(coolers_by_name=rows)
        assert "cool_npc" not in [i["local_name"] for i in repo.coolers_for_size(3)]
        got = repo.components_for_slot(
            "Cooler", _slot("hardpoint_cooler", 3),
            _ship("hardpoint_cooler", min_size=3, max_size=3))
        assert "cool_npc" not in [i["local_name"] for i in got]


# ── the whole corpus ─────────────────────────────────────────────────────────

def _corpus():
    """(index, repo) over the cached scunpacked build, or a skip."""
    try:
        from data import scunpacked_provider as scp
        idx = scp.load_window_index(allow_fetch=False)
    except Exception as exc:                          # noqa: BLE001
        pytest.skip(f"scunpacked build not cached: {exc}")
    items = idx.get("items") or {}
    by_name = {}
    for attr, kind in (("shields_by_name", "shields"), ("coolers_by_name", "coolers"),
                       ("radars_by_name", "radars"),
                       ("powerplants_by_name", "powerplants"),
                       ("qdrives_by_name", "qdrives")):
        by_name[attr] = {f"{st['local_name']}_{st['size']}": st
                         for st in items.get(kind) or []}
    return idx, _repo(**by_name)


#  accept_type -> index attribute, for the five core component sections.
CORE = [("Shield", "shields_by_name"), ("Cooler", "coolers_by_name"),
        ("Radar", "radars_by_name"), ("PowerPlant", "powerplants_by_name"),
        ("QuantumDrive", "qdrives_by_name")]


def test_no_component_slot_offers_an_item_outside_its_ports_range():
    """The countable claim, over every ship in the cached build.

    Guards its own denominator: an empty candidate list would score zero
    violations too, so the total number of offered items is asserted to be
    large and the number of editable slots with nothing to offer to be zero.
    """
    from services.slot_extractor import extract_slots_by_type
    idx, repo = _corpus()

    slots = offered = undersized = oversized = locked_swappable = 0
    empty_editable = 0
    worst = []

    for _cls, ship in sorted((idx.get("ships") or {}).items()):
        loadout = ship.get("loadout") or []
        if not loadout:
            continue
        for kind, _attr in CORE:
            for slot in extract_slots_by_type(loadout, {kind}):
                slots += 1
                pc = repo.port_constraints(ship, slot)
                mn = pc["min_size"]
                mx = pc["max_size"] if pc["max_size"] is not None else \
                    (slot.get("max_size") or 1)
                stock = (slot.get("local_ref") or "").lower()
                cand = repo.components_for_slot(kind, slot, ship)
                offered += len(cand)

                def _is_stock(c):
                    return bool(stock) and stock in {(c.get("ref") or "").lower(),
                                                     (c.get("local_name") or "").lower()}

                small = [c for c in cand if c["size"] < mn and not _is_stock(c)]
                big = [c for c in cand if c["size"] > mx]
                if small:
                    undersized += 1
                    if len(worst) < 5:
                        worst.append(f"{ship.get('name')}/{slot['id']} ({kind}) "
                                     f"S{mn}-S{mx} offers "
                                     f"{sorted({c['size'] for c in small})}")
                if big:
                    oversized += 1
                if not pc["editable"] and len(cand) > 1:
                    locked_swappable += 1
                if pc["editable"] and not cand:
                    empty_editable += 1

    assert slots > 2000, f"only {slots} component slots scanned -- corpus too small"
    assert offered > 20000, \
        f"only {offered} items offered across {slots} slots -- empty denominator"
    assert empty_editable == 0, \
        f"{empty_editable} EDITABLE slots have nothing to offer"
    assert undersized == 0, f"{undersized}/{slots} slots offer an undersized " \
                            f"item; e.g. {worst}"
    assert oversized == 0, f"{oversized}/{slots} slots offer an oversized item"
    assert locked_swappable == 0, \
        f"{locked_swappable}/{slots} non-editable slots still get a swappable picker"
