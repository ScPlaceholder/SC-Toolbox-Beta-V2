"""Footer display: shield regen direction, pip-linked resistance, QD restore level.

Covers issue #7 (e), (g), (j). The reasoning lives in
`services.loadout_aggregator.select_shield_display` and
`services.power_engine.PowerAllocatorEngine._restore_level`; this file pins the
behaviour those docstrings describe.

* The regen tests are DIRECTIONAL on purpose. A test asserting only "a number came out"
  passed the shipped bug: switching the shield bank OFF displayed 1204.0 HP/s against
  481.6 with it ON, because both sides were valid numbers and only their ORDER was
  wrong.

** The fixture is a REAL loadout, not a hand-made one: `fixtures/gladius_power.json` is
  the stock Aegis Gladius and the seven power components it resolves, lifted verbatim
  from the scunpacked window index for build 4.10.1-LIVE.12660092 (see its
  `_provenance`). Shields are 2x GODI AllStop, maxShieldRegen 602.0 each, powerSegment
  3.0, conversionMinimumFraction 1/3, resistance phys 0->0.25 / energy 0->0 /
  distortion 0.75->0.95, with erkul powerRanges modifiers 0.7 / 0.85 / 1.0. Copied into
  the repo rather than read from the user cache so the test is hermetic -- a fixture that
  silently vanishes turns these assertions into a skip, and a skipped direction test is
  how the bug shipped in the first place.
"""
import json
import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from services.loadout_aggregator import compute_footer_totals, select_shield_display
from services.power_engine import PowerAllocatorEngine

_HERE = os.path.dirname(os.path.abspath(__file__))

# The app.py the source-level guard reads. The pre-fix differential harness points this
# at a HEAD copy so the guard can be shown to fail.
APP_PY = os.path.join(os.path.dirname(_HERE), "dps_ui", "app.py")

with open(os.path.join(_HERE, "fixtures", "gladius_power.json"), "r", encoding="utf-8") as _fh:
    _FIX = json.load(_fh)
SHIP = _FIX["ship"]
PRAW = _FIX["power_raw"]
_BY_REF = {r["ref"]: r for r in PRAW.values() if r.get("ref")}
SHIELD_KEY = "shld_godi_s01_allstop_scitem"


def _raw_lookup(ident):
    return PRAW.get(ident) or PRAW.get(str(ident).lower()) or _BY_REF.get(ident)


def _engine(mode="SCM"):
    eng = PowerAllocatorEngine(lambda _ln: None, _raw_lookup)
    if mode != "SCM":
        eng.set_mode(mode)
    eng.load_ship(SHIP)
    return eng


def _totals():
    """Footer totals for the Gladius' two shields -- i.e. the UNPOWERED maxima."""
    sh = PRAW[SHIELD_KEY]["shield"]
    res = sh["resistance"]

    def find_shield(_name):
        return {
            "hp": 0.0,
            "regen": float(sh["maxShieldRegen"]),
            "res_phys_max": float(res["physicalMax"]),
            "res_energy_max": float(res["energyMax"]),
            "res_dist_max": float(res["distortionMax"]),
            "power_draw": 0.0,
        }

    def _none(_name):
        return None

    return compute_footer_totals(
        {"defenses": {"shield_left": "AllStop", "shield_right": "AllStop"}},
        find_weapon=_none, find_missile=_none, find_shield=find_shield,
        find_cooler=_none, find_radar=_none, find_powerplant=_none,
    )


def _display(eng, totals, power_sim=True):
    return select_shield_display(
        power_sim, totals,
        powered_regen=eng.shield_regen_powered,
        powered_res=eng.shield_res_powered,
        powered_count=eng.shield_powered_count,
        shield_power_ratio=eng.shield_power_ratio,
    )


def _avg(disp, key):
    return disp["res"][key] / disp["count"] if disp["count"] else 0.0


# -- fixture sanity: if these move, every number below is about a different ship --

def test_fixture_is_the_real_gladius():
    assert _FIX["_provenance"]["ship"] == "Aegis Gladius"
    assert float(PRAW[SHIELD_KEY]["shield"]["maxShieldRegen"]) == 602.0
    t = _totals()
    assert t["shield_count"] == 2
    assert t["shield_regen"] == 1204.0
    eng = _engine()
    assert eng.shield_powered_count == 2
    assert eng.shield_power_ratio == 0.5, "SCM gives the bank 3 of its 6 pips"
    assert abs(eng.shield_regen_powered - 481.6) < 0.01


# -- (e) regen direction ----------------------------------------------------------

def test_shield_bank_at_default_pips_regens():
    eng, t = _engine(), _totals()
    d = _display(eng, t)
    assert d["source"] == "sim"
    assert d["regen"] > 0, "the SCM default allocation must leave the bank regenerating"


def test_switching_the_bank_off_never_raises_the_displayed_regen():
    """THE defect: shields OFF printed the full maximum, above every powered fit."""
    eng, t = _engine(), _totals()
    on = _display(eng, t)
    assert abs(on["regen"] - 481.6) < 0.01
    eng.toggle_by_type("shield", 0)          # the SHD icon / right-click
    off = _display(eng, t)
    assert off["regen"] <= on["regen"], (
        f"shields OFF displayed {off['regen']} against {on['regen']} ON -- inversion is back")
    assert off["regen"] == 0.0
    assert off["source"] == "sim", "a switched-off bank is a sim RESULT, not missing data"
    assert off["sim_zero"] is True, "0.0 here must print as 0.0, not as an em-dash"


def test_direction_holds_at_a_partial_allocation():
    """Where the inversion was worst: 140.5 powered against 1204.0 switched off."""
    eng, t = _engine(), _totals()
    eng.set_level_by_type("shield", 0, 1)    # user drags the shield pips down
    on = _display(eng, t)
    assert abs(on["regen"] - 140.47) < 0.05
    eng.toggle_by_type("shield", 0)
    off = _display(eng, t)
    assert off["regen"] <= on["regen"], (
        f"shields OFF displayed {off['regen']} against {on['regen']} ON")
    assert off["regen"] < t["shield_regen"], (
        "the unpowered maxima must not be reachable by switching the bank off")


def test_regen_is_monotonic_in_pips():
    t = _totals()
    eng = _engine()
    eng.set_level_by_type("shield", 0, eng._categories["shield"][0]["max_segments"])
    full = _display(eng, t)["regen"]
    eng.set_level_by_type("shield", 0, 1)
    one = _display(eng, t)["regen"]
    eng.toggle_by_type("shield", 0)
    off = _display(eng, t)["regen"]
    assert full >= one >= off, f"regen not monotonic in pips: {full} / {one} / {off}"
    assert full > off


def test_nav_mode_powers_the_bank_down_and_says_so():
    eng, t = _engine(mode="NAV"), _totals()
    assert eng.shield_power_ratio == 0.0
    d = _display(eng, t)
    assert (d["source"], d["regen"], d["sim_zero"]) == ("sim", 0.0, True), (
        f"NAV powers shields down, so the footer must read 0.0, not {d['regen']} "
        f"(source {d['source']})")


def test_no_shield_data_keeps_the_unpowered_maxima():
    """The other cause of powered_count == 0: the sim saw no shields. Keep the maxima."""
    t = _totals()
    eng = PowerAllocatorEngine(lambda _ln: None, _raw_lookup)   # never loaded a ship
    d = _display(eng, t)
    assert d["source"] == "raw"
    assert d["regen"] == t["shield_regen"] == 1204.0
    assert d["sim_zero"] is False


def test_power_sim_off_uses_the_maxima():
    eng, t = _engine(), _totals()
    d = _display(eng, t, power_sim=False)
    assert d["source"] == "raw"
    assert d["regen"] == t["shield_regen"]


def test_no_shields_equipped_is_not_a_sim_zero():
    eng = _engine()
    empty = compute_footer_totals(
        {}, find_weapon=lambda _n: None, find_missile=lambda _n: None,
        find_shield=lambda _n: None, find_cooler=lambda _n: None,
        find_radar=lambda _n: None, find_powerplant=lambda _n: None)
    eng.toggle_by_type("shield", 0)
    d = _display(eng, empty)
    assert d["source"] == "raw" and d["sim_zero"] is False, (
        "with nothing equipped the footer must fall through to the em-dash")


# -- (j) resistance follows the pip allocation ------------------------------------

def test_resistance_follows_the_pips():
    t = _totals()
    eng = _engine()
    eng.set_level_by_type("shield", 0, eng._categories["shield"][0]["max_segments"])
    full = _display(eng, t)
    eng2 = _engine()
    eng2.set_level_by_type("shield", 0, 1)
    part = _display(eng2, t)

    assert abs(_avg(full, "phys") - 0.25) < 1e-9, "at full pips the maxima ARE correct"
    assert abs(_avg(full, "dist") - 0.95) < 1e-9
    assert abs(_avg(part, "phys") - 0.02917) < 1e-4, (
        "the figure the footer discarded: 2.9% phys, not the 25% maximum it printed")
    assert abs(_avg(part, "dist") - 0.77333) < 1e-4
    assert _avg(part, "phys") < _avg(full, "phys")
    assert _avg(part, "dist") < _avg(full, "dist")


def test_resistance_at_full_pips_matches_the_maxima():
    """Why the old 'always show the maxima to match Erkul' comment looked true."""
    t = _totals()
    eng = _engine()
    eng.set_level_by_type("shield", 0, eng._categories["shield"][0]["max_segments"])
    d = _display(eng, t)
    for key, tot_key in (("phys", "phys"), ("enrg", "enrg"), ("dist", "dist")):
        assert abs(_avg(d, key) - t["shield_res"][tot_key] / t["shield_count"]) < 1e-9


def test_footer_reads_the_selected_resistance_not_the_maxima():
    """Source guard for the dead assignment: the resist rows must read the selected
    source, not `totals["shield_res"]`.

    ! This is a STATIC check on app.py. It proves the WIRING, not the rendering:
    `_update_footer` needs a live repository and a Qt window to call. It is the
    assertion that fails against the pre-fix file.
    """
    with open(APP_PY, "r", encoding="utf-8") as fh:
        src = fh.read()
    start = src.index('_set("shld_phys"')
    block = src[start:src.index('_set("cooling"', start)]
    assert "shld_res[" in block, "resist rows no longer read the selected shield source"
    assert 'totals["shield_res"]' not in block, (
        "resist rows read the unpowered maxima again -- the pip allocation is ignored")
    assert src.count("shld_res") >= 2, "shld_res is computed and never read again"


# -- (g) the quantum drive's restore level ----------------------------------------

def test_qdrive_restores_a_real_pip_level_when_switched_on_in_scm():
    eng = _engine()
    qd = eng._categories["quantumDrive"][0]
    assert qd["max_segments"] == 3, "Beacon: powerSegment 3.0, min fraction 1.0 -> one pip"
    assert qd["enabled"] is False, "SCM leaves the QD unpowered (erkul-exact)"
    assert qd["current_seg"] == 0, "and unallocated, so it draws nothing at load"
    assert qd["default_seg"] > 0, (
        "default_seg is the level a toggle restores -- 0 here means 'not in SCM's "
        "priority list', and the QD icon handed that absence straight to current_seg")
    eng.toggle_by_type("quantumDrive", 0)
    qd = eng._categories["quantumDrive"][0]
    assert qd["enabled"] is True
    assert qd["current_seg"] == 3, "switching the QD on must give it its power segments"
    assert eng._power_config["qdrive"]["power"] is True
    assert eng._power_config["qdrive"]["segment"] == 3, (
        "and the allocation must reach the power config, or the draw stays fictional")


def test_restore_level_is_the_first_pip_not_the_whole_bar():
    """NAV: shields are the unpowered category, and they have SIX pips, so first-pip
    and whole-bar are distinguishable here (the QD's single pip cannot tell them apart).
    """
    eng = _engine(mode="NAV")
    shd = eng._categories["shield"][0]
    assert shd["max_segments"] == 6 and shd["current_seg"] == 0
    assert shd["default_seg"] == 1, (
        "the restore level is what select_first() would have given it, not max_segments "
        "-- claiming the whole bar would claim headroom the ship may not have")


def test_powered_categories_keep_their_real_allocation():
    """The fix must not invent a default for a category the allocator DID allocate."""
    eng = _engine()
    seen = 0
    for cat in ("shield", "cooler", "radar", "thruster", "lifeSupport"):
        for slot in eng._categories.get(cat, []):
            assert slot["default_seg"] == slot["current_seg"], (
                f"{cat}: an allocated category's default must be its allocation")
            seen += 1
    assert seen >= 4, "fixture lost its powered categories"


def test_scm_default_allocation_is_unchanged_by_the_fix():
    """Parity guard: the restore level is read on TOGGLE, never at load."""
    eng = _engine()
    assert eng._power_config["qdrive"]["power"] is False
    assert eng._power_config["qdrive"]["segment"] == 0
    assert eng._power_config["totalAvailablePowerSegments"] == 16.0
    assert eng.recalculate()["consumption_pct"] == 100.0


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL  {name}: {e}")
            except (KeyError, TypeError, ValueError, AttributeError, RuntimeError,
                    IndexError) as e:
                failures += 1
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"Done. {failures} failing.")
