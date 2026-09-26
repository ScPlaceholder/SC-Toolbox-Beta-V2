"""Issue #26 — Mining Signals listed locations where the searched ore is not minable.

Cause: ``mining_chart_data._index`` summed EVERY element in a rock's
composition, primary and trace alike.  A composition's first part is the
deposit's TYPE — the name the in-game mining kiosk lists — and anything else in
it is a trace inclusion you can only obtain by mining that type.  So searching
Tungsten returned Lyria and Wala, where Tungsten is a 5-10% trace inside a
``Laranite (Raw)`` deposit and the kiosk shows no Tungsten at all.

Note the earlier 28%->2% change (2178c3d) did not fix this and could not: it
changed the MAGNITUDE while both the filter and the painter test ``> 0``, so the
row survived at 2%.  The question was never the threshold, it was primary vs
trace.

Fixture data is the real scmdb payload from the tool's own cache, so these are
the reporter's actual locations rather than a hand-made rock.
"""
from __future__ import annotations

import json
import os

import pytest

from tools.Mining_Signals.services.mining_chart_data import (
    LocationRow, MiningChartFetcher, _clean_element,
)

_CACHE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    ".mining_chart_cache.json",
)


@pytest.fixture(scope="module")
def chart():
    if not os.path.isfile(_CACHE):
        pytest.skip("no cached scmdb payload to index")
    with open(_CACHE, encoding="utf-8") as f:
        raw = json.load(f).get("mining_data")
    if not raw:
        pytest.skip("cache holds no mining_data blob")
    return MiningChartFetcher._index(raw, "test")


@pytest.fixture(scope="module")
def rows(chart):
    return {r.name: r for r in chart.rows if r.depth > 0}


# ─────────────────────────────────────────────────────────────────────────────
# The reported defect
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("where", ["Lyria", "Wala"])
def test_trace_tungsten_is_not_a_deposit_type(rows, where):
    """The reporter's two screenshots: Tungsten must not read as minable here."""
    row = rows[where]
    assert row.ship_resources.get("Tungsten", 0.0) == 0.0, (
        f"{where} still lists Tungsten as a deposit type; the kiosk shows none"
    )
    # ...but the ore genuinely IS in the rock, so the fact is kept, not dropped.
    assert row.ship_trace.get("Tungsten", 0.0) > 0.0, (
        f"{where} lost the Tungsten trace entirely — that information is real"
    )


def test_the_deposit_that_carries_the_trace_is_still_listed(rows):
    """Tungsten at Lyria/Wala comes from Laranite, which IS a deposit type."""
    for where in ("Lyria", "Wala"):
        assert rows[where].ship_resources.get("Laranite", 0.0) > 10.0


def test_primary_ore_keeps_all_of_its_quality_bands(rows):
    """Guards the subtle half: 'primary' is not just parts[0].

    scmdb splits a deposit's own ore across two parts (two quality bands).
    Counting only parts[0] would report Lyria Iron at ~4% instead of ~25% —
    a fix for the reported bug that quietly introduced a worse one.
    """
    iron = rows["Lyria"].ship_resources.get("Iron", 0.0)
    assert iron > 20.0, f"Lyria Iron collapsed to {iron:.1f}% — bands were dropped"


def test_an_ore_can_be_both_primary_and_trace_at_one_location(rows):
    """NOT a defect, and this test exists because I first assumed it was.

    Pyro I has a Tin deposit type AND Tin traces inside its Copper deposits, so
    the two dicts legitimately overlap.  The invariant is therefore not
    disjointness — it is that each contribution is banked once, in the bucket
    matching the deposit it came from.  A location where an ore is minable must
    NOT be marked as trace-only just because a trace share also exists.
    """
    row = rows["Pyro I"]
    assert row.ship_resources.get("Tin", 0.0) > 0, "Tin is a real deposit at Pyro I"
    assert row.ship_trace.get("Tin", 0.0) > 0, "and also a trace inside Copper"

    from tools.Mining_Signals.ui.mining_chart import _cell_label
    label = _cell_label(row.ship_resources["Tin"], row.ship_trace["Tin"])
    assert not label.startswith("~"), (
        f"Pyro I Tin rendered as trace-only ({label}) — it is scannable there"
    )


def test_both_present_cell_shows_the_summed_yield(rows):
    """The displayed number is the full expectation, not the primary share alone."""
    from tools.Mining_Signals.ui.mining_chart import _cell_label
    row = rows["Pyro I"]
    p, t = row.ship_resources["Tin"], row.ship_trace["Tin"]
    assert _cell_label(p, t) == f"{int(round(p + t))}%"
    # Concretely: ~20.2% primary + ~2.4% trace reads as 23%, not 20%.
    assert _cell_label(p, t) == "23%"


def test_tungsten_deposit_locations_are_the_minority(rows):
    """The scale of the bug: most Tungsten rows the user saw were not minable.

    3 locations carry Tungsten as a scannable deposit; 6 more only as a trace.
    (A 4th primary location, Pyro Belt (Warm 2), is in _HIDDEN and excluded from
    the chart by design.)
    """
    primary = sorted(n for n, r in rows.items() if r.ship_resources.get("Tungsten", 0) > 0)
    trace_only = sorted(
        n for n, r in rows.items()
        if r.ship_trace.get("Tungsten", 0) > 0 and not r.ship_resources.get("Tungsten", 0)
    )
    assert primary == ["Lagrange F", "Pyro V-c (Adir)", "Pyro V-d (Fairo)"]
    assert set(trace_only) >= {"Lyria", "Wala"}
    assert len(trace_only) > len(primary), "expected trace rows to outnumber real ones"


def test_no_column_is_lost_to_the_split(chart):
    """Every ore is a deposit type SOMEWHERE, so no column disappears.

    If some ore were trace-everywhere, hiding trace would erase it from the
    chart entirely — this asserts the column set did not silently shrink.
    """
    assert "Tungsten" in chart.ship_columns
    assert len(chart.ship_columns) >= 20


# ─────────────────────────────────────────────────────────────────────────────
# The rendered distinction — what the user actually sees
# ─────────────────────────────────────────────────────────────────────────────


def test_cell_label_marks_trace_distinctly():
    from tools.Mining_Signals.ui.mining_chart import _cell_label

    assert _cell_label(17.6, 0.0) == "18%", "a deposit type reads as a plain value"
    assert _cell_label(0.0, 2.14) == "~2%", "a trace-only ore must be marked"
    assert _cell_label(0.0, 0.0) == "", "absent stays blank"
    # A real but sub-1% trace must not render as a flat "~0%".
    assert _cell_label(0.0, 0.4) == "~1%"
    # Both present: minable, so unmarked, and the value is the full yield.
    assert _cell_label(12.0, 3.0) == "15%"


def test_sort_ranks_any_deposit_above_any_trace():
    """A scannable 3% must outrank an unscannable 8% when sorting by that ore."""
    from tools.Mining_Signals.ui.mining_chart import MiningChartGrid, VIEW_SHIP

    grid = MiningChartGrid.__new__(MiningChartGrid)  # no Qt init needed
    grid._view_mode = VIEW_SHIP

    minable = LocationRow(name="minable", system="s", loc_type="moon", depth=1,
                          ship_resources={"Tungsten": 3.0})
    rich_trace = LocationRow(name="rich_trace", system="s", loc_type="moon", depth=1,
                             ship_trace={"Tungsten": 8.0})

    assert grid._sort_key(minable, "Tungsten") > grid._sort_key(rich_trace, "Tungsten")
    # And the presence test still sees both, so neither row silently disappears.
    assert grid._row_total(minable, "Tungsten") > 0
    assert grid._row_total(rich_trace, "Tungsten") > 0


def test_grid_paints_a_tungsten_search_end_to_end(chart):
    """Drive the real paintEvent: catches a painter signature/arity mistake.

    The pure ``_cell_label`` tests cannot see a broken call into
    ``_paint_pct_cell``, and that call is where the new trace argument is passed.
    """
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication

    from tools.Mining_Signals.ui.mining_chart import MiningChartGrid, _cell_label

    QApplication.instance() or QApplication([])

    grid = MiningChartGrid()
    grid.set_data(chart)
    grid.set_resource_filter("Tungsten")

    visible = [r for r in grid._visible_rows if r.depth > 0]
    assert grid._visible_cols == ["Tungsten"]
    assert len(visible) >= 5, "the search should still find Tungsten locations"

    grid.resize(max(400, grid.width()), max(400, grid.height()))
    img = QImage(grid.size(), QImage.Format_ARGB32)
    img.fill(0)
    grid.render(img)  # raises if the painter chain is broken
    assert img.width() > 0 and img.height() > 0

    kinds = {
        r.name: _cell_label(r.ship_resources.get("Tungsten", 0.0),
                            r.ship_trace.get("Tungsten", 0.0))
        for r in visible
    }
    # The reporter's two locations must be marked, and real ones must not be.
    assert kinds["Lyria"].startswith("~")
    assert kinds["Wala"].startswith("~")
    assert not kinds["Pyro V-c (Adir)"].startswith("~")
    # Both kinds coexist on one screen — that is the point of labelling.
    assert any(v.startswith("~") for v in kinds.values())
    assert any(not v.startswith("~") for v in kinds.values())


def test_clean_element_strips_scmdb_suffixes():
    assert _clean_element("Tungsten (Ore)") == "Tungsten"
    assert _clean_element("Laranite (Raw)") == "Laranite"
    assert _clean_element("Hadanite (Gem)") == "Hadanite"
    assert _clean_element("Aphorite") == "Aphorite"


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic rock: pins the rule independently of the live payload
# ─────────────────────────────────────────────────────────────────────────────


def test_index_splits_a_hand_built_composition():
    """One location, one deposit: Laranite primary with a Tungsten trace."""
    raw = {
        "compositions": {
            "g1": {
                "name": "Laranite (Raw)",
                "parts": [
                    {"elementName": "Laranite (Raw)", "probability": 1.0,
                     "minPercent": 10.0, "maxPercent": 20.0},
                    {"elementName": "Laranite (Raw)", "probability": 1.0,
                     "minPercent": 30.0, "maxPercent": 50.0},
                    {"elementName": "Tungsten (Ore)", "probability": 1.0,
                     "minPercent": 5.0, "maxPercent": 10.0},
                ],
            }
        },
        "locations": [{
            "locationName": "Testmoon", "locationType": "moon", "system": "Stanton",
            "groups": [{
                "groupName": "SpaceShip_Mineables",
                "deposits": [{"compositionGuid": "g1", "relativeProbability": 100}],
            }],
        }],
    }
    data = MiningChartFetcher._index(raw, "v")
    row = next(r for r in data.rows if r.name == "Testmoon")

    # Primary = both Laranite bands: mid(15) + mid(40) = 55, at dep_prob 1.0
    assert row.ship_resources == pytest.approx({"Laranite": 55.0})
    # Trace = Tungsten only: mid(7.5)
    assert row.ship_trace == pytest.approx({"Tungsten": 7.5})
    # Both still get a column so the ore remains findable.
    assert set(data.ship_columns) == {"Laranite", "Tungsten"}


def test_trace_only_location_is_still_kept():
    """A row whose ONLY content is a trace inclusion must not vanish silently."""
    raw = {
        "compositions": {
            "g1": {"name": "Ice (Raw)", "parts": [
                {"elementName": "Ice (Raw)", "probability": 1.0,
                 "minPercent": 50.0, "maxPercent": 100.0},
                {"elementName": "Tungsten (Ore)", "probability": 1.0,
                 "minPercent": 1.0, "maxPercent": 3.0},
            ]},
        },
        "locations": [{
            "locationName": "Tracemoon", "locationType": "moon", "system": "Stanton",
            "groups": [{"groupName": "SpaceShip_Mineables",
                        "deposits": [{"compositionGuid": "g1", "relativeProbability": 100}]}],
        }],
    }
    data = MiningChartFetcher._index(raw, "v")
    row = next((r for r in data.rows if r.name == "Tracemoon"), None)
    assert row is not None
    assert row.ship_trace.get("Tungsten", 0) > 0
    assert row.ship_resources.get("Tungsten", 0) == 0


def test_locationrow_defaults_are_not_shared():
    """Dataclass field(default_factory) sanity — a shared dict would cross-talk."""
    a = LocationRow(name="a", system="s", loc_type="moon", depth=1)
    b = LocationRow(name="b", system="s", loc_type="moon", depth=1)
    a.ship_trace["Tungsten"] = 1.0
    assert b.ship_trace == {}
