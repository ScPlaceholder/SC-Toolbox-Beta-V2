"""THE CONTROL: the four gate tests must FAIL on wrong implementations.

A differential test with a broken control scores a perfect 100%, because every
mutant dies regardless of what it does. So this file asserts BOTH halves:

    BASELINE   the contract maths passes all ten checks
    CONTROL    each of the seven wrong implementations is caught by >= 1 check

and then goes further, asserting per-mutation WHICH check catches it. That last
part is the one that earns its keep: it is how the two inert checks were found
on 2026-09-27. `loop_returns_to_start` in its first form passed for all five
wrong ops including the accumulating one, and `arms_down` in its first form
passed `drop_parent_rotation` because that mutation makes the idle MORE still
and the check only had an upper bound.

Nothing here writes to disk.
"""

from __future__ import annotations

import pytest

from pico import selftest as st
from pico.rig import TransformOps


@pytest.fixture(scope="module")
def baseline() -> dict:
    return st._run_checks(st.build())


def test_the_baseline_passes_every_check(baseline):
    failed = {k: v for k, v in baseline.items() if v is not None}
    assert not failed, failed


def test_every_declared_check_actually_ran(baseline):
    """A check defined and left out of `CHECKS` is a check that never runs.

    That happened here: `check_gate_six_clips_two_skins` was written, was
    correct, and was not in the tuple, so the owner's own freeze criterion was
    dark until the control table showed it missing from every row.
    """
    declared = {name for name, _ in st.CHECKS}
    assert set(baseline) == declared
    assert len(declared) == 12
    module_checks = {n for n in dir(st) if n.startswith("check_")}
    wired = {fn.__name__ for _, fn in st.CHECKS}
    assert module_checks == wired, (
        "defined but never wired into CHECKS: %s" % sorted(module_checks - wired))


@pytest.mark.parametrize("name", [n for n, _ in st.CHECKS])
def test_each_check_passes_on_the_baseline(name, baseline):
    """One pytest node per check, so a failure names itself in the summary."""
    assert baseline[name] is None, baseline[name]


@pytest.mark.parametrize("label", sorted(st.WRONG_OPS))
def test_a_wrong_transform_is_caught_by_at_least_one_check(label):
    try:
        res = st._run_checks(st.build(st.WRONG_OPS[label]))
    except Exception:
        return  # refused at load: caught, and more decisively
    caught = [k for k, v in res.items() if v is not None]
    assert caught, (
        "%s passed every check. A fixture that accepts a wrong answer is not a "
        "test." % label)


@pytest.mark.parametrize("label", sorted(st.WRONG_CONSTRAINTS))
def test_a_wrong_constraint_is_caught_by_at_least_one_check(label):
    sk, pol = st.WRONG_CONSTRAINTS[label]()
    res = st._run_checks(st.build(skeleton=sk, policy=pol))
    caught = [k for k, v in res.items() if v is not None]
    assert caught, "%s passed every check" % label


#: WHICH check must catch WHICH mutation. Pinning this is what turns "something
#: failed" into a statement about coverage. A check that disappears from every
#: row here is a check nothing exercises.
EXPECTED_CATCHES = {
    "order_SRT": {"rest_positions", "arms_down"},
    "reversed_compose": {"rest_positions", "arms_down"},
    "parent_relative_coords": {"rest_positions"},
    "drop_parent_rotation": {"rest_positions", "arms_down", "every_clip_moves"},
    "accumulating": {"arms_down", "skin_swap_moves_nothing",
                     "loop_returns_to_start", "gate_six_clips_two_skins"},
    "tidied_flipper_symmetric": {"flipper_asymmetry"},
    "clamping_not_wired": {"flipper_asymmetry", "clamp_is_loud"},
}


@pytest.mark.parametrize("label,expected", sorted(EXPECTED_CATCHES.items()))
def test_the_named_check_is_the_one_that_catches_it(label, expected):
    if label in st.WRONG_OPS:
        res = st._run_checks(st.build(st.WRONG_OPS[label]))
    else:
        sk, pol = st.WRONG_CONSTRAINTS[label]()
        res = st._run_checks(st.build(skeleton=sk, policy=pol))
    caught = {k for k, v in res.items() if v is not None}
    assert expected <= caught, (
        "%s: expected %s to catch it, but only %s did"
        % (label, sorted(expected - caught), sorted(caught)))


def test_every_ops_mutation_is_reached_by_some_expectation():
    """No mutation may sit in the suite without a named catcher."""
    assert set(st.WRONG_OPS) | set(st.WRONG_CONSTRAINTS) == set(EXPECTED_CATCHES)


def test_each_gate_check_is_the_named_catcher_for_something():
    """Coverage of the CHECKS, not of the code. A check no mutation reaches is
    a check that could be deleted and no test would notice."""
    named = set().union(*EXPECTED_CATCHES.values())
    for check in ("rest_positions", "arms_down", "loop_returns_to_start",
                  "skin_swap_moves_nothing", "every_clip_moves",
                  "gate_six_clips_two_skins", "flipper_asymmetry",
                  "clamp_is_loud"):
        assert check in named, (
            "%s is never the reason any mutation fails; nothing exercises it"
            % check)


def test_the_checks_no_mutation_reaches_are_named_and_justified():
    """Four checks catch no mutation. Recorded, not papered over.

    `channels_commute` and `loop_pop_refused` are STRUCTURAL: no `TransformOps`
    substitution can break them, because neither the channel order nor a bad
    loop can reach the transform maths — which is the property. They carry
    their own internal negative controls instead: each asserts that the invalid
    input is REFUSED, so the branch is exercised from the failing side.

    `skeleton_shape` and `no_qt` are facts about the data and the import graph,
    not about the maths, and a transform mutation cannot move either.
    """
    named = set().union(*EXPECTED_CATCHES.values())
    unreached = {name for name, _ in st.CHECKS} - named
    assert unreached == {"skeleton_shape", "channels_commute",
                         "loop_pop_refused", "no_qt"}, (
        "the set of checks no mutation reaches has changed to %s; either a new "
        "mutation should cover one, or this list needs updating with a reason"
        % sorted(unreached))


def test_the_selftest_entry_point_returns_zero():
    assert st.main(["--quiet"]) == 0


def test_an_ops_that_is_wrong_in_no_way_is_the_default():
    assert TransformOps().name == "contract"
    assert TransformOps().accumulate is False
