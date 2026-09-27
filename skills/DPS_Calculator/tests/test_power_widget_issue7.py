"""PowerAllocatorWidget: the three defects reported in issue #7.

  (1) NAV mode appeared not to let shields be turned off.
  (2) The cooler control was doubled — two bars, one icon, one toggle.
  (3) Power overdraw was shown only as a red bar and a bare percentage.

** THESE TESTS RUN AGAINST A REAL SHIP, ON PURPOSE. `fixtures/gladius_power.json` is a
   scunpacked capture of the Aegis Gladius (build 4.10.1-LIVE.12660092) and it is the
   fixture these defects need: TWO Bracer coolers, TWO AllStop shields, and a 16-segment
   Regulus power plant. An engine with no ship loaded — which is what every existing test
   in `test_power_engine.py` uses — has NO shield slots and NO cooler slots, so
   "the shield category is unpowered in NAV" and "two coolers render two columns" are
   both VACUOUSLY TRUE there and pass whether or not the fixes exist. A test that cannot
   fail is not evidence.

** WHAT THESE TESTS DO NOT COVER: they assert the widget's MODEL — which slot dict each
   pip bar is bound to, how many columns exist, what each icon's handler does, and what
   the OVER CAPACITY label's text is. They do NOT assert pixels. Nothing here proves a
   pip changed colour on screen, because `_PipCanvas.paintEvent` is only exercised by a
   real paint and `QLabel.isVisible()` is False for a window that was never shown. The
   binding is the defect in (1) and (2) — the old code painted an orphaned dict and gave
   two components one icon — so the model is the right place to pin them, but "the test
   passes" is not "the number on screen changed".
"""
import json
import os
import sys

# Bootstrap project root so shared.path_setup is importable
sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..')))
import shared.path_setup  # noqa: E402  # centralised path config
shared.path_setup.ensure_path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A QApplication must exist before any QWidget is constructed, and the box running the
# suite has no display. NOT guarded with importorskip: if PySide6 is missing that is the
# interpreter, and it must fail loudly rather than skip a whole file behind a caveat.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QLabel  # noqa: E402

_APP = QApplication.instance() or QApplication([])

from dps_ui.power_widget import PowerAllocatorWidget, _PipCanvas  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(_HERE, "fixtures", "gladius_power.json"), "r", encoding="utf-8") as _fh:
    _FIX = json.load(_fh)
SHIP = _FIX["ship"]
PRAW = _FIX["power_raw"]


def _raw_lookup(ident, table=None):
    table = PRAW if table is None else table
    if ident in table:
        return table[ident]
    for rec in table.values():
        if rec.get("ref") == ident:
            return rec
    return None


def _widget(raw_table=None):
    w = PowerAllocatorWidget(None, lambda ln: None,
                             lambda ln: _raw_lookup(ln, raw_table))
    w.load_ship(SHIP)
    return w


# -- introspection helpers: read the widget the way the user's eye does ---------------

def _columns(w):
    """The column QWidgets currently in the grid, left to right."""
    return [w._col_layout.itemAt(i).widget()
            for i in range(w._col_layout.count())
            if w._col_layout.itemAt(i).widget() is not None]


def _col_pips(col):
    return col.findChildren(_PipCanvas)


def _col_slots(col):
    """The slot dicts the pip bars of this column are actually painting."""
    return [p._slot for p in _col_pips(col)]


def _col_icon(col):
    """The clickable icon QLabel at the bottom of the column (the last QLabel added)."""
    return col.findChildren(QLabel)[-1]


def _cols_for(w, category):
    return [c for c in _columns(w)
            if _col_slots(c) and {s["category"] for s in _col_slots(c)} == {category}]


def _drawn(w, category):
    """The slot dicts bound to the pip bars — NOT engine.categories[...]. The whole of
    defect (1) is that these two can be different objects."""
    return [s for _pw, s in w._pip_widgets if s["category"] == category]


# -- (1) NAV mode / shields -----------------------------------------------------------

def test_fixture_is_the_multi_component_ship_these_tests_need():
    """Guard the guard: if the fixture ever loses its second cooler, the layout tests
    below would pass vacuously instead of failing."""
    w = _widget()
    assert len(w._engine.categories.get("cooler", [])) == 2, "fixture must have 2 coolers"
    assert len(w._engine._components["shields"]) == 2, "fixture must have 2 shields"
    assert w._engine._power_config["totalAvailablePowerSegments"] == 16.0


def test_pip_bars_are_bound_to_slots_the_engine_still_owns_after_a_mode_change():
    """The core of defect (1). `engine.set_mode` rebuilds every slot dict, so a widget
    that does not rebuild its columns is painting orphans."""
    w = _widget()
    w.set_mode("NAV")

    live = {id(s) for s in w._slots}
    drawn = {id(s) for _pw, s in w._pip_widgets}
    assert drawn, "no pip bars were built at all"
    orphans = drawn - live
    assert not orphans, (
        f"{len(orphans)} of {len(drawn)} pip bars are bound to slot dicts the engine "
        f"discarded when it rebuilt for NAV"
    )


def test_nav_mode_draws_the_shield_bar_as_unpowered():
    """What the reporter actually saw: shields still rendering as powered in NAV.

    NAV leaving shields unpowered is INTENDED (erkul-exact) and is not changed here —
    this asserts the widget shows the state the engine computed."""
    w = _widget()
    scm_shield = _drawn(w, "shield")
    assert len(scm_shield) == 1
    assert scm_shield[0]["enabled"] is True, "SCM baseline: shields are powered"
    assert scm_shield[0]["current_seg"] > 0

    w.set_mode("NAV")
    nav_shield = _drawn(w, "shield")
    assert len(nav_shield) == 1
    assert nav_shield[0]["enabled"] is False, "the drawn shield bar must read unpowered in NAV"
    assert nav_shield[0]["current_seg"] == 0


def test_clicking_the_drawn_shield_bar_in_nav_reaches_the_allocator():
    """The other half of defect (1): clicks on a stale bar changed nothing but pixels.

    `_on_pip_set` / `_toggle_slot` mutate the slot dict and then ask the engine to
    `sync_seg_config_from_slots()`, which iterates `engine._slots`. A dict that is not in
    that list absorbs the click silently."""
    w = _widget()
    w.set_mode("NAV")
    slot = _drawn(w, "shield")[0]

    # NAV leaves shields off, so the first toggle is an ON.
    w._toggle_slot(slot)
    cfg = w._engine._power_config["shield"]
    assert cfg["power"] is True, "toggling the drawn shield bar did not reach the engine"
    assert cfg["segment"] == slot["default_seg"] > 0

    # ...and lowering the energy on that same bar must come back down.
    w._on_pip_set(slot, 0)
    assert w._engine._power_config["shield"]["segment"] == 0


# -- (2) the doubled cooler control ---------------------------------------------------

def test_two_coolers_get_two_columns_of_one_bar_each():
    w = _widget()
    cooler_cols = _cols_for(w, "cooler")
    assert len(cooler_cols) == 2, (
        f"expected one column per cooler, got {len(cooler_cols)} column(s) holding "
        f"{[len(_col_pips(c)) for c in _cols_for(w, 'cooler')]} bar(s)"
    )
    for col in cooler_cols:
        assert len(_col_pips(col)) == 1, "a cooler column must hold exactly one bar"


def test_single_slot_categories_keep_exactly_one_column_each():
    """The split is generic on slot count, so this is the guard that it did not change
    the layout of any category nobody complained about."""
    w = _widget()
    for category in ("weaponGun", "thruster", "shield", "radar",
                     "lifeSupport", "quantumDrive"):
        cols = _cols_for(w, category)
        assert len(cols) == 1, f"{category} should render one column, got {len(cols)}"
        assert len(_col_pips(cols[0])) == 1
    assert len(_columns(w)) == 8, "6 single-slot categories + 2 coolers"


def test_a_cooler_icon_toggles_only_its_own_cooler():
    """Before the split, the one shared snowflake ran `_toggle_category("cooler")` and
    switched BOTH coolers, so a single cooler could not be unpowered at all."""
    w = _widget()
    cooler_cols = _cols_for(w, "cooler")
    assert len(cooler_cols) == 2

    configs = w._engine._power_config["coolers"]
    assert [c["power"] for c in configs] == [True, True], "both coolers start powered"

    _col_icon(cooler_cols[0]).mousePressEvent(None)

    assert configs[0]["power"] is False, "the clicked cooler should be off"
    assert configs[1]["power"] is True, "the OTHER cooler must be untouched"


# -- (3) silent overdraw --------------------------------------------------------------

def test_no_over_capacity_marker_when_the_allocation_fits():
    w = _widget()
    result = w._engine.recalculate()
    assert result["consumption_pct"] <= 100
    assert w.over_capacity_text == ""


def test_over_capacity_is_stated_in_words_above_100_percent():
    w = _widget()
    slot = next(s for s in w._slots
                if s["enabled"] and s["current_seg"] < s["max_segments"])
    w._on_pip_set(slot, slot["max_segments"])

    result = w._engine.recalculate()
    assert result["consumption_pct"] > 100, "did not manage to overdraw the plant"

    text = w.over_capacity_text
    assert "OVER CAPACITY" in text, f"overdraw announced only as {w._lbl_pct.text()!r}"
    over = result["total_draw"] - result["total_capacity"]
    assert f"+{over:.0f}" in text, f"marker should carry the excess, got {text!r}"


def test_capacity_zero_with_a_live_draw_is_not_reported_as_a_healthy_zero():
    """An absence is not a value. `recalculate` returns consumption_pct 0 whenever
    total_capacity is 0 (power_engine.py:923) — that 0 is a missing denominator, and a
    ship with no resolvable power plant must not read like an idle one."""
    no_pp = {k: v for k, v in PRAW.items() if v.get("type") != "PowerPlant"}
    w = _widget(raw_table=no_pp)

    result = w._engine.recalculate()
    assert result["total_capacity"] == 0
    assert result["total_draw"] == 0, "nothing can be allocated against no capacity"
    assert w.over_capacity_text == "", "quiet while genuinely nothing is drawn"

    # A pip click writes current_seg with no capacity check, so a draw IS reachable.
    slot = next(s for s in w._slots if s["max_segments"] > 0)
    w._on_pip_set(slot, slot["max_segments"])

    result = w._engine.recalculate()
    assert result["total_draw"] > 0
    assert result["consumption_pct"] == 0, "the engine's 0% here is an absent denominator"
    assert "OVER CAPACITY" in w.over_capacity_text
    assert "+" not in w.over_capacity_text, (
        "with no known capacity there is no 'over by N' to state"
    )


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except AssertionError as e:
                print(f"  FAIL  {name}: {e}")
            except (KeyError, TypeError, ValueError, AttributeError, RuntimeError,
                    StopIteration) as e:
                print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print("Done.")
