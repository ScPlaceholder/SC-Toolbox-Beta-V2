"""Searching for a place takes the map all the way to it.

J, 2026-10-04: "for the starmap if someone types in a location it should zoom
all the way into it". Before, the search box stopped one scene short: a system
name only centred the galaxy on it, and a location opened its system scene at
zoom 4 with the camera on the location's coordinates - so Area18 was a label
hidden under ArcCorp's, and an outpost (which the system scene does not draw at
all) was an empty spot in space.

The map is four scenes deep (galaxy > system > planet & moons > globe) and a
place on a planet or moon is a pin on that body's globe; there is no scene of
the place itself. "All the way" is therefore:
  * a system         -> its system scene (what double-clicking it does);
  * a planet / moon  -> its globe;
  * a place on one   -> that globe, turned to face the place, zoomed, ringed;
  * anything else    -> the system scene, centred on it at the close zoom, ringed.

Every assertion below is on the state the scenes really draw and hit-test from
(the nav stack, the camera, the globe's yaw / pitch / zoom), never on a flag
that exists only to be tested.

NOTHING HERE TOUCHES THE GAME, THE MOUSE OR THE KEYBOARD: the in-game macro is
a recording fake, state files are redirected to tmp, the network is blocked.
"""
import math

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent

from test_no_microphone import _pump, audio, panel  # noqa: F401  (fixtures)


class _Setter:
    """Stands in for the in-game route macro; records what it was asked to plot."""

    def __init__(self):
        self.plotted = []

    def available(self):
        return True

    def busy(self):
        return False

    def set_route(self, destination, status_cb=None, done_cb=None):
        self.plotted.append(destination)
        return True


@pytest.fixture
def routed(panel, monkeypatch):
    """The panel with the Assistant's real RouteService and a recording setter."""
    from starmap import set_route_link
    svc = set_route_link.service()
    setter = _Setter()
    svc._setter_factory = lambda: setter
    monkeypatch.setattr(panel, "_route_svc", svc)
    panel.setter = setter
    # every way into the route code, counted
    panel.route_calls = []
    for name in ("resolve", "plot"):
        real = getattr(svc, name)
        monkeypatch.setattr(svc, name, lambda *a, _n=name, _r=real, **k:
                            (panel.route_calls.append(_n), _r(*a, **k))[1])
    return panel


def _search(panel, text):
    """Pick *text* in the search box: what a click on a result, or Enter, emits."""
    panel._search.item_selected.emit(text)
    _pump(0.05)


def _type_search(panel, text):
    """Type into the search box key by key (the suggestions follow), press Enter."""
    inp = panel._search._input
    inp.clear()
    for ch in text:
        inp.setText(inp.text() + ch)
    inp.returnPressed.emit()
    _pump(0.05)
    return inp.text()


def _type(panel, text):
    """Type into the command bar and press Enter."""
    bar = panel._voicebar
    bar._input.setText(text)
    bar._input.returnPressed.emit()
    _pump(0.05)
    return bar.status()


def _crumbs(panel):
    return [label for label, _w in panel._nav]


def _top(panel):
    view = panel._nav[-1][1]
    assert panel._stack.currentWidget() is view, "the scene on screen is not the top of the nav stack"
    return view


def _body(panel, code, name):
    return next(b for b in panel._bodies[code] if b.name == name)


def _assert_on_globe_pin(panel, code, globe_name, place_name):
    """The scene on screen is *globe_name*'s globe with *place_name* as its subject."""
    from starmap import planet_view
    view = _top(panel)
    assert type(view).__name__ == "PlanetView", _crumbs(panel)
    assert view._planet_name == globe_name and view._system == code
    place = _body(panel, code, place_name)
    assert place in view._locs, "the place is not a pin on this globe"
    # zoomed in: past where the labels reveal, inside the wheel's range
    assert view._zoom == planet_view.FOCUS_ZOOM
    assert planet_view._LABEL_REVEAL_ZOOM < view._zoom <= planet_view._ZOOM_MAX
    assert abs(view._pitch) <= planet_view._PITCH_MAX
    # facing the viewer: the globe's own rotation puts the pin at the centre, in front
    x, y, z = view._rot(view._dirs[view._locs.index(place)])
    assert z > 0.99 and abs(x) < 0.05 and abs(y) < 0.05, (x, y, z)
    # ... which is where the scene's own hit test finds it
    w, h = view.width(), view.height()
    assert view._hit(QPointF(w / 2.0, h / 2.0), w / 2.0, h / 2.0, view._radius(w, h)) is place
    assert view.focused() is place


def _globe_state(panel):
    v = _top(panel)
    return (type(v).__name__, v._planet_name, v._system, v._yaw, v._pitch, v._zoom, v.focused())


# ── a location ───────────────────────────────────────────────────────────────

def test_a_location_in_the_current_system_ends_on_the_location(panel):
    panel._galaxy.systemEntered.emit("STANTON")          # by hand: double-click Stanton
    assert _crumbs(panel) == ["Galaxy", "STANTON system"]
    _search(panel, "Shubin Mining Facility SAL-2")
    assert _crumbs(panel) == ["Galaxy", "STANTON system", "ArcCorp & moons", "LYRIA"]
    _assert_on_globe_pin(panel, "STANTON", "Lyria", "Shubin Mining Facility SAL-2")


def test_a_location_in_another_system_switches_system_and_ends_on_it(panel):
    panel._galaxy.systemEntered.emit("STANTON")
    _search(panel, "Blackrock Exchange")                  # an outpost on Terminus, in Pyro
    assert _crumbs(panel) == ["Galaxy", "PYRO system", "Terminus & moons", "TERMINUS"]
    _assert_on_globe_pin(panel, "PYRO", "Terminus", "Blackrock Exchange")
    assert panel._galaxy._selected == "PYRO"


def test_a_landing_zone_is_found_by_its_spoken_name(panel):
    """The data calls it "Area18"; a pilot types "area 18"."""
    _search(panel, "area 18")
    assert _crumbs(panel) == ["Galaxy", "STANTON system", "ArcCorp & moons", "ARCCORP"]
    _assert_on_globe_pin(panel, "STANTON", "ArcCorp", "Area18")


def test_a_place_near_the_pole_stays_inside_the_drag_limits(panel):
    """Scuttle sits past the tilt a drag can reach; the search must not leave the
    globe in a state a drag would then snap out of."""
    _search(panel, "Scuttle")
    view = _top(panel)
    place = next(b for b in view._locs if b.name == "Scuttle")
    from starmap import planet_view
    assert abs(view._pitch) <= planet_view._PITCH_MAX
    x, y, z = view._rot(view._dirs[view._locs.index(place)])
    assert z > 0.99, (x, y, z)


def test_a_place_with_no_globe_is_centred_and_ringed_in_the_system_scene(panel):
    """A Lagrange station orbits the star: the system scene is its deepest home."""
    from starmap import system_view
    _search(panel, "ARC-L1 Wide Forest Station")
    assert _crumbs(panel) == ["Galaxy", "STANTON system"]
    view = _top(panel)
    b = _body(panel, "STANTON", "ARC-L1 Wide Forest Station")
    cam = view._cam
    assert (cam.fx, cam.fy, cam.fz) == (b.x, b.y, b.z)
    assert (cam.pan_x, cam.pan_y) == (0.0, 0.0)
    assert cam.zoom == system_view.FOCUS_ZOOM
    # close, but still below the zoom at which the wheel opens a planet instead
    assert 4.0 < cam.zoom < system_view._DRILL_IN_ZOOM
    w, h = view.width(), view.height()
    sx, sy, _ = cam.project(b.x, b.y, b.z, w / 2.0, h / 2.0, view._scale(w, h))
    assert (round(sx), round(sy)) == (round(w / 2.0), round(h / 2.0))
    assert view._hit(QPointF(w / 2.0, h / 2.0)) is b
    assert view.focused() is b


# ── a body, a system ─────────────────────────────────────────────────────────

def test_a_planet_and_a_moon_end_on_their_own_globe(panel):
    _search(panel, "Hurston")
    assert _crumbs(panel) == ["Galaxy", "STANTON system", "Hurston & moons", "HURSTON"]
    view = _top(panel)
    assert type(view).__name__ == "PlanetView" and view._planet_name == "Hurston"
    assert view._zoom == 1.0 and view.focused() is None   # the whole globe is the subject
    _search(panel, "Lyria")
    assert _crumbs(panel) == ["Galaxy", "STANTON system", "ArcCorp & moons", "LYRIA"]
    assert _top(panel)._planet_name == "Lyria"


def test_a_system_name_enters_the_system(panel):
    _search(panel, "Pyro")
    assert _crumbs(panel) == ["Galaxy", "PYRO system"]
    view = _top(panel)
    assert type(view).__name__ == "SystemView" and view._code == "PYRO"
    assert view._cam.zoom == 1.0 and view.focused() is None   # framed whole, as on entry by hand
    assert panel._galaxy._selected == "PYRO"


def test_a_name_in_two_systems_means_the_one_on_screen(panel):
    """Both ends of a jump have a "Pyro Gateway" (Stanton's and Nyx's)."""
    _search(panel, "Pyro Gateway")                        # from the galaxy: the data's first
    assert _top(panel)._code == "STANTON"
    panel._home_clicked()
    panel._galaxy.systemEntered.emit("NYX")
    _search(panel, "Pyro Gateway")
    view = _top(panel)
    assert view._code == "NYX"
    assert view.focused() is _body(panel, "NYX", "Pyro Gateway")


# ── an unknown name ──────────────────────────────────────────────────────────

def test_an_unknown_name_changes_nothing(panel):
    _search(panel, "Area18")
    before = (_crumbs(panel), _top(panel), _globe_state(panel), panel._galaxy.get_state())
    _search(panel, "zzqx nowhere")
    assert (_crumbs(panel), _top(panel), _globe_state(panel), panel._galaxy.get_state()) == before
    assert _type(panel, "go to zzqx nowhere") == "not found: zzqx nowhere"
    assert (_crumbs(panel), _top(panel), _globe_state(panel), panel._galaxy.get_state()) == before
    # ... and from the galaxy too
    panel._home_clicked()
    gal = panel._galaxy.get_state()
    _search(panel, "zzqx nowhere")
    assert _crumbs(panel) == ["Galaxy"] and panel._galaxy.get_state() == gal


def test_typed_text_that_names_no_place_goes_nowhere(panel):
    """Enter used to take the first row of a fuzzy (or stale) suggestion list:
    "Area 18" went to ArcCorp Mining Area 141, "zzqx" to whatever "z" had listed."""
    _search(panel, "Area18")
    before = (_crumbs(panel), _top(panel), _globe_state(panel))
    _type_search(panel, "zzqx")
    assert (_crumbs(panel), _top(panel), _globe_state(panel)) == before
    assert panel._voicebar.status() == "no place matches 'zzqx'"
    _type_search(panel, "arc 1")                          # fuzzily like many, named like none
    assert (_crumbs(panel), _top(panel), _globe_state(panel)) == before
    assert "places partly match 'arc 1'" in panel._voicebar.status()


def test_typing_a_place_and_pressing_enter_goes_to_that_place(panel):
    assert _type_search(panel, "Area 18") == "Area18"
    _assert_on_globe_pin(panel, "STANTON", "ArcCorp", "Area18")
    panel._home_clicked()
    assert _type_search(panel, "Lorvil") == "Lorville"    # part of one name: as before
    _assert_on_globe_pin(panel, "STANTON", "Hurston", "Lorville")
    assert _type_search(panel, "pyro") == "Pyro"
    assert _crumbs(panel) == ["Galaxy", "PYRO system"]


# ── afterwards the map is the map ────────────────────────────────────────────

def test_home_after_a_search_zoom_is_the_normal_galaxy_view(panel):
    _search(panel, "Area18")
    assert len(panel._nav) == 4
    panel._btn_home.click()
    _pump(0.05)
    assert _crumbs(panel) == ["Galaxy"]
    assert panel._stack.currentWidget() is panel._galaxy
    assert panel._stack.count() == 1, "a scene the search built was left behind in the stack"
    assert not panel._navbar.isVisible() and panel._btn_route.isEnabled()
    home = panel._galaxy._galaxy.get(panel._galaxy.home)
    cam = panel._galaxy._cam
    assert (cam.fx, cam.fy, cam.fz, cam.zoom) == (home.x, home.y, home.z, 1.0)


def test_back_and_back_to_galaxy_after_a_search_zoom(panel):
    _search(panel, "Area18")
    panel._btn_back.click()
    assert _crumbs(panel) == ["Galaxy", "STANTON system", "ArcCorp & moons"]
    panel._btn_back.click()
    assert _crumbs(panel) == ["Galaxy", "STANTON system"]
    _search(panel, "Area18")
    assert _type(panel, "back to galaxy") == "galaxy"
    assert _crumbs(panel) == ["Galaxy"] and panel._stack.currentWidget() is panel._galaxy


def test_the_scenes_are_the_ones_walking_there_by_hand_builds(panel):
    """Same stack, same scene types and wiring: the wheel and a double-click then
    do what they do after arriving by hand."""
    panel._galaxy.systemEntered.emit("STANTON")           # double-click the system
    panel._nav[-1][1].planetEntered.emit("ArcCorp")       # double-click the planet
    panel._nav[-1][1].bodyEntered.emit("ArcCorp")         # double-click it again: the globe
    by_hand = [(lbl, type(w).__name__) for lbl, w in panel._nav]
    panel._home_clicked()
    _search(panel, "Area18")
    assert [(lbl, type(w).__name__) for lbl, w in panel._nav] == by_hand
    view = _top(panel)

    def wheel(delta):
        pos = QPointF(view.width() / 2.0, view.height() / 2.0)
        view.wheelEvent(QWheelEvent(pos, pos, QPoint(0, 0), QPoint(0, delta), Qt.NoButton,
                                    Qt.NoModifier, Qt.NoScrollPhase, False))
    z0 = view._zoom
    wheel(120)                                            # one notch in: just zooms
    assert view._zoom == pytest.approx(z0 * 1.0015 ** 120) and _top(panel) is view
    for _ in range(20):                                   # all the way out: back to the planet scene
        if _top(panel) is not view:
            break
        wheel(-240)
    assert _crumbs(panel) == ["Galaxy", "STANTON system", "ArcCorp & moons"]


# ── the game is not involved ─────────────────────────────────────────────────

def test_the_search_box_never_reaches_the_in_game_route_code(routed):
    routed._btn_game.setChecked(True)
    assert routed._in_game() is True, "the In-Game switch is not on: this test would prove nothing"
    _search(routed, "Area18")
    _search(routed, "Pyro")
    _search(routed, "zzqx nowhere")
    _type_search(routed, "Area 18")
    assert routed.setter.plotted == []
    assert routed.route_calls == [], "the search box called the Assistant's route service"


def test_navigate_and_map_goto_end_at_the_same_zoom_as_the_search_box(routed):
    routed._btn_game.setChecked(True)
    _search(routed, "Area18")
    want = _globe_state(routed)
    _assert_on_globe_pin(routed, "STANTON", "ArcCorp", "Area18")

    # the command bar: the map follows AND (In-Game on) the route is set, as before
    routed._home_clicked()
    status = _type(routed, "navigate to Area 18")
    assert routed.setter.plotted == ["area18"]
    assert status == "setting route to area18 in game"
    assert _globe_state(routed) == want

    # "go to": map only
    routed._home_clicked()
    assert _type(routed, "go to area 18") == "going to area 18"
    assert _globe_state(routed) == want
    assert routed.setter.plotted == ["area18"]

    # the Assistant set a route itself and asks the map to show the place
    routed._home_clicked()
    routed.route_calls.clear()
    routed._on_ipc({"type": "map_goto", "name": "area18"})
    assert _globe_state(routed) == want
    assert routed.setter.plotted == ["area18"] and routed.route_calls == []
