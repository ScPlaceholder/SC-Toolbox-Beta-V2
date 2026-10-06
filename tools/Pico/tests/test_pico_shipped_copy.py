"""What the installer's copy of Pico relies on.

1. Loop folders. A shipped Pico looks for a developer's loop folders nowhere outside the toolbox: only
   PICO_LOOPS_DIR points at them. A folder in the home directory that happens to be laid out the way the
   developer's is must not be picked up.

2. Props. The installer stages each prop PNG far smaller than its art (build/shrink_pico_props.py) and
   writes the art's size into the record as "src_size". The window has to place the prop by THAT size,
   or the resized PNG's rounded aspect ratio moves the prop a pixel off the flipper.

No window is made and no file of the user's is read: the first part runs a child Python with a fake home,
the second calls Pal.place_prop on a stand-in object.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL = Path(__file__).resolve().parent.parent
REPO = TOOL.parent.parent
if str(TOOL) not in sys.path:
    sys.path.insert(0, str(TOOL))

from pico import snap  # noqa: E402

pytest.importorskip("PySide6")
import sprite_pal  # noqa: E402

# ── 1. loop folders ───────────────────────────────────────────────────────────

CHILD = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
from pico import sprites
import pico_pals_app
print("RESULT " + json.dumps({"default": str(sprites.DEFAULT_DIR), "exists": sprites.DEFAULT_DIR.is_dir()}))
'''


def default_dir(home: Path, loops_env: str | None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PICO_")}
    env.update(USERPROFILE=str(home), HOME=str(home), HOMEDRIVE=str(home)[:2], HOMEPATH=str(home)[2:],
               APPDATA=str(home / "AppData"), PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    if loops_env is not None:
        env["PICO_LOOPS_DIR"] = loops_env
    run = subprocess.run([sys.executable, "-c", CHILD, str(TOOL)], env=env, capture_output=True, text=True,
                         timeout=60)
    line = next((ln for ln in run.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert run.returncode == 0 and line, run.stdout + run.stderr
    return json.loads(line[7:])


@pytest.fixture()
def home_with_loops(tmp_path):
    """A home folder laid out like a developer's, with a loop folder in it."""
    home = tmp_path / "home"
    loops = home / "BrAi" / "_forJ" / "VNCCS" / "pico_anim_sequences"
    loops.mkdir(parents=True)
    (loops / "idle_look_default.gif").write_bytes(b"GIF89a")
    (home / "AppData").mkdir()
    return home, loops


def test_without_the_variable_nothing_in_the_home_folder_is_looked_at(home_with_loops):
    home, loops = home_with_loops
    got = default_dir(home, None)
    where = Path(got["default"])
    assert TOOL in where.parents, "DEFAULT_DIR is outside the tool: %s" % where
    assert home not in where.parents and where != loops
    assert got["exists"] is False, "%s exists: a user's Pico would wear it instead of a pack" % where
    assert where.name == "pico_anim_sequences"       # outfit_name() and brand_allows() read the name


def test_the_variable_points_pico_at_a_developers_loops(home_with_loops):
    home, loops = home_with_loops
    got = default_dir(home, str(loops))
    assert Path(got["default"]) == loops and got["exists"] is True


def test_an_empty_variable_is_the_same_as_none(home_with_loops):
    home, _loops = home_with_loops
    assert TOOL in Path(default_dir(home, "")["default"]).parents


def test_the_no_art_sentence_names_no_loop_folder_unless_asked(monkeypatch, capsys, tmp_path):
    """Exit 3's sentence is read by a user. It names the pack folder; the loop folder only for a developer."""
    import pico_pals_app
    from pico import sprites

    def no_art(_args, on_ready=None):
        raise sprites.SpriteError("no loop folder at somewhere")

    monkeypatch.setattr(sprite_pal, "main", no_art)
    monkeypatch.delenv("PICO_LOOPS_DIR", raising=False)
    assert pico_pals_app.main(["--demo"]) == pico_pals_app.EXIT_NO_ART
    plain = capsys.readouterr().err
    assert "outfit pack" in plain and "loop folders under" not in plain, plain
    monkeypatch.setenv("PICO_LOOPS_DIR", str(tmp_path / "pico_anim_sequences"))
    assert pico_pals_app.main(["--demo"]) == pico_pals_app.EXIT_NO_ART
    asked = capsys.readouterr().err
    assert "outfit pack" in asked and "loop folders under" in asked, asked


# ── 2. props ──────────────────────────────────────────────────────────────────

FULL = (454, 550)          # pistol_white.png as authored
STAGED = (214, 259)        # the same prop as the installer stages it
REC = {"size": 0.55, "layer": "front", "offset": [-0.05, 0.0], "anchor": "tip", "grip": [0.2577, 0.4545],
       "png": "pistol_white.png"}
ANCHORS = {"belly_w": 218.5, "size": [609, 560],
           "frames": [{"tip": [431.0, 251.5]}, {"tip": [433.5, 249.0]}, {"tip": [430.0, 255.0]}]}


class Pix:
    def __init__(self, size):
        self.size = size

    def width(self):
        return self.size[0]

    def height(self):
        return self.size[1]

    def isNull(self):
        return False

    def scaled(self, w, h):
        return (w, h)


class Label:
    def __init__(self):
        self.geo = {}

    def setPixmap(self, pm):
        self.geo["pix"] = pm

    def resize(self, w, h):
        self.geo["size"] = (w, h)

    def move(self, x, y):
        self.geo["at"] = (x, y)

    def lower(self):
        pass

    def raise_(self):
        pass

    def show(self):
        self.geo["shown"] = True

    def hide(self):
        self.geo["shown"] = False


def placed(rec: dict, png_size: tuple, height: int, frame: int) -> dict:
    """Where Pal.place_prop puts the prop label, for a prop whose PNG on disk is png_size."""
    movie = SimpleNamespace(scaledSize=lambda: SimpleNamespace(width=lambda: int(609 * height / 560)))
    pic = SimpleNamespace(x=lambda: 100, y=lambda: 40, width=lambda: 700, height=lambda: height)
    pal = SimpleNamespace(prop_rec=rec, anchors=ANCHORS, prop_pix=Pix(png_size), prop_lbl=Label(),
                          height_px=height, movie=movie, pic=pic)
    sprite_pal.Pal.place_prop(pal, frame)
    return pal.prop_lbl.geo


def all_placements(rec: dict, png_size: tuple) -> list:
    return [placed(rec, png_size, h, n) for h in range(140, 561, 10) for n in range(3)]


def test_a_staged_prop_sits_exactly_where_the_full_size_art_does():
    staged_rec = dict(REC, src_size=list(FULL))
    assert snap.authored_size(REC, FULL) == FULL
    assert snap.authored_size(staged_rec, STAGED) == FULL
    want = all_placements(REC, FULL)
    assert all(g.get("shown") for g in want)
    assert all_placements(staged_rec, STAGED) == want


def test_that_test_can_fail():
    """Placed by the resized PNG's own size, the same prop is off by a pixel at some heights. If this ever
    stops being true the test above proves nothing and needs a different prop."""
    assert all_placements(REC, STAGED) != all_placements(REC, FULL)


def test_a_bad_src_size_is_ignored():
    for bad in (None, [], [0, 10], [454], "454x550"):
        assert snap.authored_size(dict(REC, src_size=bad), STAGED) == STAGED


# ── the build's own numbers ───────────────────────────────────────────────────

def test_props_are_staged_for_the_sliders_top():
    """build/shrink_pico_props.py sizes every prop for the largest Pico the Customise box allows."""
    import re
    if str(REPO / "build") not in sys.path:
        sys.path.insert(0, str(REPO / "build"))
    import shrink_pico_props as shrink

    top = re.search(r"self\.size\.setRange\(\s*\d+\s*,\s*(\d+)\s*\)", (TOOL / "sprite_pal.py").read_text(encoding="utf-8"))
    assert top and int(top.group(1)) == shrink.MAX_HEIGHT
    # 0.55 belly-widths, belly at most BELLY_FRAC of the height, drawn at MAX_HEIGHT, staged at DRAW_SCALE times that
    assert shrink.needed_px(0.55) == 259
    assert shrink.needed_px(1.6) >= 2 * 1.6 * 229.4      # the widest belly measured, on a 560 px Pico


def test_the_build_refuses_to_stage_no_props(tmp_path, capsys):
    if str(REPO / "build") not in sys.path:
        sys.path.insert(0, str(REPO / "build"))
    import shrink_pico_props as shrink

    (tmp_path / "Pico").mkdir()
    assert shrink.stage(tmp_path / "Pico", tmp_path / "out") == 1
    assert "is missing" in capsys.readouterr().out and not (tmp_path / "out").exists()
    props = tmp_path / "Pico" / "out" / "snap_props"
    props.mkdir(parents=True)
    (props / "snap_props.json").write_text("{}", encoding="utf-8")
    assert shrink.stage(tmp_path / "Pico", props) == 1          # never writes into the authored art
    assert "never" in capsys.readouterr().out
