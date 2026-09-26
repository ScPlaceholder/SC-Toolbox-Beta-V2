"""The five component sections must hand `_build_table_slot` a SLOT-AWARE picker.

`test_component_port_fit.py` proves `components_for_slot` applies the port's MinSize and
Editable flag. This file proves the UI actually CALLS it — which is a separate claim, and for
most of tonight it was false: the rule existed, was tested, and 1,820 of 2,125 slots kept
offering undersized items because `app.py` still called `<kind>_for_size(max_sz)` with a bare
int and no way to learn which slot it was being asked about.

★ WHY THIS IS A STATIC GUARD AND NOT A BEHAVIOURAL ONE, stated because a static test is the
  weaker kind and should not be mistaken for the strong one. Driving `_rebuild_shields_section`
  for real needs a populated `ComponentRepository`, and the real catalogs load through an async
  path that no test in this directory drives — the port-fit suite builds its index by hand with
  `object.__new__`. So the honest options were this guard or nothing, and nothing is how the
  wiring went missing in the first place.
⚠ WHAT THIS DOES NOT PROVE: that the numbers on screen changed. It proves the call site passes
  the slot. The filtering itself is covered by `test_component_port_fit.py` against real data
  (1,820 undersized offers -> 0 across 310 ships).

⚠ AND IT GUARDS THE LATE-BINDING TRAP TOO. The lambdas must capture the loop variable by
  DEFAULT ARGUMENT (`_s=slot`). Written as a bare closure over `slot`, every lambda in the loop
  would resolve to the LAST slot of the section at call time, so every shield row would offer
  the final shield port's list — a bug that would pass any test that only checks one slot.
"""
import os
import re

APP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dps_ui", "app.py")

#: kind -> the finder that sits beside it in the same `_build_table_slot` call, used to locate
#: the section without depending on line numbers.
SECTIONS = {
    "PowerPlant": "find_powerplant",
    "QuantumDrive": "find_qdrive",
    "Shield": "find_shield",
    "Cooler": "find_cooler",
    "Radar": "find_radar",
}


def _src():
    with open(APP, encoding="utf-8") as fh:
        return fh.read()


def test_every_core_component_section_passes_the_slot():
    src = _src()
    missing = [k for k in SECTIONS
               if f'components_for_slot("{k}"' not in src]
    assert not missing, (
        "these component sections no longer pass the slot, so the port's MinSize and Editable "
        "flag are ignored again: %s" % missing)


def test_no_core_section_still_calls_the_size_only_picker():
    """The old call must be GONE, not merely shadowed by a new one sitting beside it."""
    src = _src()
    stale = [m for m in ("powerplants_for_size", "qdrives_for_size", "shields_for_size",
                         "coolers_for_size", "radars_for_size")
             if re.search(r"self\._data\.%s\b" % m, src)]
    assert not stale, (
        "app.py still calls the size-only picker for: %s — that is the exact call that offered "
        "items below MinSize in 1,820 of 2,125 slots" % stale)


def test_the_lambdas_capture_the_slot_by_default_argument():
    """`_s=slot`, not a bare closure — otherwise every row gets the LAST slot of the section.

    This is the failure a single-slot test cannot see, and every one of these sections builds
    its pickers inside a `for slot in ...` loop.
    """
    src = _src()
    calls = re.findall(r"lambda\s+([^:]*):\s*self\._data\.components_for_slot\(\"(\w+)\"",
                       src)
    assert calls, "found no components_for_slot lambdas at all"
    bad = [kind for params, kind in calls if "_s=slot" not in params]
    assert not bad, (
        "these lambdas close over the loop variable instead of binding it as a default, so "
        "every row in the section would resolve to the last slot: %s" % bad)


def test_the_lambda_still_accepts_the_size_argument():
    """`_build_table_slot` calls `list_fn(max_sz)`; a zero-arg lambda would TypeError at build."""
    src = _src()
    calls = re.findall(r"lambda\s+([^:]*):\s*self\._data\.components_for_slot\(", src)
    assert calls
    bad = [p for p in calls if not p.strip().startswith("sz")]
    assert not bad, (
        "these lambdas do not take the size positional that _build_table_slot passes: %s" % bad)


def test_all_five_kinds_are_known_to_the_repository():
    """A kind string with a typo raises ValueError at picker-build time, i.e. only in the UI."""
    import data.repository as R
    known = R.ComponentRepository._COMPONENT_KINDS
    unknown = [k for k in SECTIONS if k not in known]
    assert not unknown, "app.py names component kinds the repository cannot map: %s" % unknown
