"""The Star Map TAB's search box takes the map all the way to the place, too.

J, 2026-10-04: "for the starmap if someone types in a location it should zoom
all the way into it". The tab is skills/Starmap's own panel, so the behaviour
is tested there in detail (skills/Starmap/tests/test_search_zooms_all_the_way.py);
this drives the real EverythingFinderWindow and checks the tab ends in the same
place: a location on its parent body's globe, turned to face the viewer and
zoomed in; a system inside its system scene; an unknown name nowhere new; Home
back on the galaxy.

Run in a clean subprocess with HOME redirected, like the other tab probes.
No network, no microphone, nothing sent to the game.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
EF_DIR = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

pytest.importorskip("PySide6.QtWidgets")

_PROBE = textwrap.dedent(r'''
    import json, os, sys, time
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    sys.path.insert(0, ROOT)

    sdir = os.path.join(TMP, ".sctoolbox", "starmap")
    os.makedirs(sdir, exist_ok=True)
    with open(os.path.join(sdir, "starmap_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"galaxy": {"selected": "STANTON", "home": "STANTON"}}, fh)

    import urllib.request
    def _no_network(*a, **k): raise OSError("network disabled in tests")
    urllib.request.urlopen = _no_network

    from shared.app_bootstrap import bootstrap_skill
    bootstrap_skill(os.path.join(EF_DIR, "everything_finder_app.py"))
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QCoreApplication, QPointF
    app = QApplication([])
    from everything_finder import window as wmod
    wmod.EverythingFinderWindow._state_path = staticmethod(lambda: os.path.join(TMP, "w.json"))

    def pump(seconds):
        end = time.time() + seconds
        while time.time() < end:
            QCoreApplication.processEvents()
            time.sleep(0.01)

    w = wmod.EverythingFinderWindow(initial_tab=wmod.TAB_MAP)
    w.show()
    pump(1.0)
    panel = w.inner(wmod.TAB_MAP)

    def snap():
        view = panel._nav[-1][1]
        s = {"crumbs": [lbl for lbl, _w in panel._nav],
             "scene": type(view).__name__,
             "on_screen": panel._stack.currentWidget() is view,
             "tab": w.tabs.current_key() == wmod.TAB_MAP}
        if s["scene"] == "PlanetView":
            f = view.focused()
            s["zoom"] = view._zoom
            s["focus"] = f.name if f is not None else None
            if f is not None:
                s["facing"] = list(view._rot(view._dirs[view._locs.index(f)]))
                ww, hh = view.width(), view.height()
                hit = view._hit(QPointF(ww / 2.0, hh / 2.0), ww / 2.0, hh / 2.0, view._radius(ww, hh))
                s["centre_hit"] = hit.name if hit is not None else None
        elif s["scene"] == "SystemView":
            s["system"] = view._code
            s["zoom"] = view._cam.zoom
        return s

    def search(text):
        panel._search.item_selected.emit(text)       # what picking a result emits
        pump(0.1)
        return snap()

    out = {"panel": type(panel).__name__, "module": type(panel).__module__}
    out["start"] = snap()
    out["location"] = search("Area18")
    panel._btn_home.click()
    inp = panel._search._input                       # typed key by key, then Enter
    for ch in "Area 18":
        inp.setText(inp.text() + ch)
    inp.returnPressed.emit()
    pump(0.1)
    out["typed"] = snap()
    out["unknown"] = search("zzqx nowhere")
    out["other_system"] = search("Blackrock Exchange")
    out["system"] = search("Pyro")
    panel._btn_home.click()
    pump(0.1)
    out["home"] = snap()
    out["home_is_galaxy"] = panel._stack.currentWidget() is panel._galaxy
    out["home_zoom"] = panel._galaxy._cam.zoom
    out["state_file"] = os.path.normcase(sys.modules[type(panel).__module__.rsplit(".", 1)[0]
                                                     + ".data"]._STATE_PATH)
    print("PROBE " + json.dumps(out), flush=True)
    os._exit(0)
''')


def _run_probe(tmp_path):
    code = f"ROOT = {ROOT!r}\nEF_DIR = {EF_DIR!r}\nTMP = {str(tmp_path)!r}\n" + _PROBE
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PYTHONIOENCODING="utf-8")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env, encoding="utf-8", errors="replace")
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE ")), None)
    assert line is not None, f"probe printed nothing (rc={proc.returncode}):\n{proc.stderr[-3000:]}"
    return json.loads(line[6:])


def test_the_star_map_tab_search_goes_all_the_way(tmp_path):
    got = _run_probe(tmp_path)
    assert got["panel"] == "StarmapPanel", got
    # the probe's map state lives under tmp, never the pilot's ~/.sctoolbox
    assert got["state_file"].startswith(os.path.normcase(str(tmp_path))), got["state_file"]
    assert got["start"]["crumbs"] == ["Galaxy"], got["start"]

    loc = got["location"]
    assert loc["crumbs"] == ["Galaxy", "STANTON system", "ArcCorp & moons", "ARCCORP"], loc
    assert loc["scene"] == "PlanetView" and loc["on_screen"] and loc["tab"], loc
    assert loc["focus"] == "Area18" and loc["centre_hit"] == "Area18", loc
    assert loc["zoom"] == 3.0, loc
    x, y, z = loc["facing"]
    assert z > 0.99 and abs(x) < 0.05 and abs(y) < 0.05, loc["facing"]

    assert got["typed"] == loc, "typing 'Area 18' and pressing Enter did not end on Area18"
    assert got["unknown"] == loc, "an unknown name moved the map"

    other = got["other_system"]
    assert other["crumbs"] == ["Galaxy", "PYRO system", "Terminus & moons", "TERMINUS"], other
    assert other["focus"] == "Blackrock Exchange" and other["centre_hit"] == "Blackrock Exchange"
    assert other["zoom"] == 3.0 and other["facing"][2] > 0.99, other

    system = got["system"]
    assert system["crumbs"] == ["Galaxy", "PYRO system"], system
    assert system["scene"] == "SystemView" and system["system"] == "PYRO" and system["zoom"] == 1.0

    assert got["home"]["crumbs"] == ["Galaxy"] and got["home_is_galaxy"], got["home"]
    assert got["home_zoom"] == 1.0
