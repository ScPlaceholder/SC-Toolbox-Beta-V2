"""Offscreen regression tests for two reported Market Finder defects.

Issue #25 -- "Some ships in Market Finder are showing different values."
    The ship table and the detail panel read the SAME field from the SAME
    index and the table aggregates with ``min()``, so no data path can make
    the table read high.  The display could: ``f"{n/1_000_000:.1f}M"`` rounds
    a price to the nearest 100,000 aUEC, and rounding is symmetric, so the
    table read HIGHER than the true price on 97 of the 174 vehicles that
    carry one.  These tests pin the exact-price formatter and the column
    width that makes room for it.

Issue #9 -- "Some searches do not return accurate results."
    Ships were added to the search corpus in cd57a1d, but the bubble slices
    the flat result list to ``SEARCH_BUBBLE_MAX`` *before* grouping it, and
    the item loop filled that whole budget first.  Any query with a full page
    of item matches therefore lost its entire Ships group.  These tests pin
    the reserved-budget ordering, and assert against the real SearchBubble.

Runs offscreen (QT_QPA_PLATFORM=offscreen).  No network and no disk cache:
every fixture is a hand-built dict.
"""

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', '..', '..')))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..')))

import pytest  # noqa: E402

QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from market_finder.config import (  # noqa: E402
    SEARCH_BUBBLE_MAX, SEARCH_BUBBLE_PER_TAB,
)
from market_finder.ui.app import MarketFinderApp  # noqa: E402
from market_finder.ui.ship_table import ShipTable, _fmt_num, _fmt_price  # noqa: E402
from market_finder.ui.widgets import SearchBubble  # noqa: E402


_WIDGETS: list = []


@pytest.fixture
def qapp():
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
    while _WIDGETS:
        w = _WIDGETS.pop()
        w.hide()
        w.deleteLater()
    app.processEvents()


class _StubData:
    """Just enough of DataService for the code under test."""

    def __init__(self, items=None, vehicles=None, purchases=None):
        self.items = items or []
        self.vehicles = vehicles or []
        self.purchase_by_vehicle = purchases or {}
        self.rental_by_vehicle = {}
        self.terminals = {}


# ---------------------------------------------------------------------------
# Issue #25 -- the table must not read higher than the true price
# ---------------------------------------------------------------------------

# (true price, the string the OLD abbreviating formatter produced)
_REPORTED_CASES = [
    (2_783_020, "2.8M"),      # Prospector, the ship in the report
    (11_850_300, "11.9M"),    # Caterpillar, worst overshoot in the sweep: +49,700
    (4_650_340, "4.7M"),      # Sabre Comet
    (19_845_000, "19.8M"),    # Valkyrie -- this one read LOW, also wrong
]


@pytest.mark.parametrize("true_price,old_display", _REPORTED_CASES)
def test_price_no_longer_abbreviated(true_price, old_display):
    """The table shows the exact figure, not the rounded one it used to."""
    assert _fmt_num(true_price) == old_display          # the old behaviour
    assert _fmt_price(true_price) == f"{true_price:,}"  # the new one
    assert "M" not in _fmt_price(true_price)


@pytest.mark.parametrize("true_price,_old", _REPORTED_CASES)
def test_price_matches_detail_panel_text(true_price, _old):
    """Cross-view check: this is literally what issue #25 reported.

    ``detail_panel._add_location_row`` renders ``f"{price:,.0f} aUEC"``.  The
    table must agree with it digit for digit, because both read ``price_buy``
    from ``purchase_by_vehicle`` and the table takes ``min()`` of exactly the
    rows the panel sorts.
    """
    detail_panel_text = f"{true_price:,.0f} aUEC"
    assert detail_panel_text.startswith(_fmt_price(true_price) + " ")


def test_price_never_reads_high_across_the_range():
    """No value in the plausible price range may display above its truth."""
    offenders = []
    for n in range(1_000_000, 40_000_001, 10_007):  # ~3,900 values
        shown = _fmt_price(n)
        if float(shown.replace(",", "")) != float(n):
            offenders.append((n, shown))
    assert offenders == [], f"{len(offenders)} prices misreported, e.g. {offenders[:3]}"


def test_price_formatter_edge_cases():
    assert _fmt_price(0) == "—"
    assert _fmt_price(None) == "—"
    assert _fmt_price("") == "—"
    assert _fmt_price("n/a") == "n/a"     # unparseable passes through
    assert _fmt_price(999) == "999"
    assert _fmt_price(15_461) == "15,461"


def test_buy_price_column_is_exact_and_wide_enough(qapp):
    """The column must use the exact formatter AND have room to show it."""
    from PySide6.QtGui import QFont, QFontMetrics

    table = ShipTable(None, _StubData())
    _WIDGETS.append(table)
    cols = {c.header: c for c in table._table._columns}
    buy = cols["Buy Price"]

    assert buy.fmt(11_850_300) == "11,850,300"
    # Cells are Consolas 9pt with 8px padding either side (shared/qt/theme.py).
    needed = QFontMetrics(QFont("Consolas", 9)).horizontalAdvance("11,850,300") + 16
    assert buy.width >= needed, f"width {buy.width} clips a {needed}px price"
    assert buy.tooltip, "the header should say the unit"

    # Spec columns keep the abbreviation on purpose -- the detail panel
    # abbreviates them too, so there is no cross-view mismatch to fix.
    assert cols["Mass"].fmt(2_783_020) == "2.8M"


# ---------------------------------------------------------------------------
# Issue #9 -- a full page of item matches must not starve the Ships group
# ---------------------------------------------------------------------------

def _corpus(n_items: int, n_vehicles: int, token: str = "aurora"):
    items = [
        {"id": i, "name": f"{token} panel {i}", "section": "Armor", "category": "Armor"}
        for i in range(n_items)
    ]
    vehicles = [
        {"id": 1000 + i, "name": f"{token.title()} MR {i}",
         "name_full": f"RSI {token.title()} MR {i}",
         "company_name": "Roberts Space Industries"}
        for i in range(n_vehicles)
    ]
    return items, vehicles


def _results(n_items: int, n_vehicles: int, query: str = "aurora"):
    items, vehicles = _corpus(n_items, n_vehicles)
    stub = type("S", (), {"data": _StubData(items=items, vehicles=vehicles)})()
    return MarketFinderApp._get_search_results(stub, query)


def _groups_the_bubble_would_build(results):
    """Replicate SearchBubble's slice-then-group, without building a widget."""
    from market_finder.config import item_tab
    groups: dict[str, list[dict]] = {}
    for it in results[:SEARCH_BUBBLE_MAX]:
        tab = "Ships" if it.get("_is_vehicle") else item_tab(it)
        groups.setdefault(tab, []).append(it)
    return groups


def test_full_item_page_still_yields_ships():
    """THE regression: 30+ item matches used to delete the Ships group."""
    results = _results(n_items=200, n_vehicles=10)
    assert len([r for r in results if not r.get("_is_vehicle")]) >= 20
    groups = _groups_the_bubble_would_build(results)
    assert "Ships" in groups, "Ships starved by a full item page (issue #9)"
    assert len(groups["Ships"]) == SEARCH_BUBBLE_PER_TAB


@pytest.mark.parametrize("n_items", [0, 1, 29, 30, 31, 500])
def test_ships_survive_at_every_item_count(n_items):
    groups = _groups_the_bubble_would_build(_results(n_items, n_vehicles=5))
    assert "Ships" in groups
    assert len(groups["Ships"]) == 5


def test_results_fit_inside_the_bubble_budget():
    """Nothing is collected that the slice would only throw away."""
    results = _results(n_items=500, n_vehicles=50)
    assert len(results) <= SEARCH_BUBBLE_MAX
    assert sum(1 for r in results if r.get("_is_vehicle")) == SEARCH_BUBBLE_PER_TAB


def test_items_are_not_starved_by_ships():
    """The mirror image of the bug must not be introduced."""
    results = _results(n_items=200, n_vehicles=200)
    items = [r for r in results if not r.get("_is_vehicle")]
    assert len(items) == SEARCH_BUBBLE_MAX - SEARCH_BUBBLE_PER_TAB


def test_no_ships_group_when_no_vehicle_matches():
    _items, vehicles = _corpus(200, 10)
    items2 = [{"id": 1, "name": "gravlev thruster",
               "section": "Systems", "category": "Thrusters"}]
    stub2 = type("S", (), {"data": _StubData(items=items2, vehicles=vehicles)})()
    r2 = MarketFinderApp._get_search_results(stub2, "gravlev")
    assert r2 and not any(x.get("_is_vehicle") for x in r2)
    assert "Ships" not in _groups_the_bubble_would_build(r2)


def test_real_search_bubble_renders_a_ships_header(qapp):
    """End-to-end through the actual widget, not a replica of its logic."""
    results = _results(n_items=200, n_vehicles=10)
    parent = QtWidgets.QWidget()
    _WIDGETS.append(parent)
    bubble = SearchBubble(parent, results, lambda item: None)
    _WIDGETS.append(bubble)

    headers = [
        w.text() for w in bubble.findChildren(QtWidgets.QLabel)
        if w.text().isupper() and "—" not in w.text()
    ]
    assert "SHIPS" in headers, f"no SHIPS group rendered; headers={headers}"

    rows = [w.text() for w in bubble.findChildren(QtWidgets.QLabel) if "—" in w.text()]
    assert any("Roberts Space Industries" in r for r in rows), "ship rows missing"
