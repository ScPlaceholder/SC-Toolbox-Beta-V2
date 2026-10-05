"""Pico has a tutorial ("How Pico works"): reachable two ways, text on every tab, and only names that exist.

Every name set in bold in the tutorial must be a string in sprite_pal.py or
pico/signs.py, or the tile's name in skill.json, or be listed below as prose
(the rule is in shared/tutorial_guard.py). Rename a menu entry or a Customise
row and this goes red until the tutorial says the new name.

The windows are built in a child process, offscreen, with APPDATA pointed at a
temp folder, like the other Pico window tests: a test must never show a window
on a desktop that already has a Pico, nor touch the user's settings.json.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
ROOT = HERE.parent.parent
for _p in (str(ROOT), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pico_tutorial  # noqa: E402
from shared import tutorial_guard as guard  # noqa: E402

SOURCES = [str(HERE / "sprite_pal.py"), str(HERE / "pico" / "signs.py")]

# The tutorial's own menu entry: its text is pico_tutorial.MENU_TEXT, which the wiring test below pins.
PROSE = ["How Pico works..."]
ALIASES: dict = {}

CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QTabWidget, QTextBrowser, QWidget
import pico_tutorial, sprite_pal

app = QApplication([])
out = {"settings": str(sprite_pal.SETTINGS), "platform": app.platformName()}

dlg = pico_tutorial.Tutorial(None)                      # never shown
tabs = dlg.findChild(QTabWidget)
out["title"] = dlg.windowTitle()
out["tabs"] = [tabs.tabText(i) for i in range(tabs.count())]
out["words"] = [len(tabs.widget(i).toPlainText().split()) for i in range(tabs.count())]
out["all_text_views"] = all(isinstance(tabs.widget(i), QTextBrowser) for i in range(tabs.count()))

host = QWidget()
box = sprite_pal.Customise(host, Path(sys.argv[2]), 280)      # an empty folder: no outfit is loaded
bb = box.findChild(QDialogButtonBox)
out["customise_buttons"] = [b.text() for b in bb.buttons()]
out["how_role_is_help"] = bb.buttonRole(box.how) == QDialogButtonBox.HelpRole
out["how_is_not_the_default"] = not box.how.isDefault() and not box.how.autoDefault()
out["settings_written"] = Path(sprite_pal.SETTINGS).exists()
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def child(tmp_path_factory):
    pytest.importorskip("PySide6.QtWidgets")
    tmp = tmp_path_factory.mktemp("pico_tutorial")
    empty = tmp / "no_outfits"
    empty.mkdir()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=str(tmp), PYTHONIOENCODING="utf-8")
    env.pop("QT_SCALE_FACTOR", None)
    r = subprocess.run([sys.executable, "-c", CHILD, str(HERE), str(empty)], env=env, capture_output=True,
                       encoding="utf-8", errors="replace", timeout=120)
    line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line, "child printed no result:\n%s\n%s" % (r.stdout[-2000:], r.stderr[-2000:])
    out = json.loads(line[len("RESULT "):])
    out["tmp"] = str(tmp)
    return out


def test_the_test_windows_cannot_touch_the_real_settings(child):
    assert child["platform"] == "offscreen"
    assert child["settings"].startswith(child["tmp"])
    assert not child["settings_written"]


def test_every_tab_has_a_title_and_text():
    pages = pico_tutorial.pages()
    assert len(pages) >= 3
    for title, html in pages:
        assert title.strip() and "&" not in title
        assert len(guard.plain_text(html)) > 200, "tab %r is nearly empty" % title


def test_the_window_builds_with_a_tab_for_each_page(child):
    assert child["title"] == pico_tutorial.TITLE
    assert child["tabs"] == [title for title, _html in pico_tutorial.pages()]
    assert child["all_text_views"] and min(child["words"]) > 40


def test_customise_offers_the_tutorial_without_changing_what_enter_does(child):
    assert pico_tutorial.MENU_TEXT in child["customise_buttons"]
    assert child["how_role_is_help"]
    assert child["how_is_not_the_default"], "Enter in Customise must still be OK, not the tutorial"


def test_the_right_click_menu_offers_the_tutorial():
    assert pico_tutorial.MENU_TEXT == "How Pico works..."
    src = (HERE / "sprite_pal.py").read_text(encoding="utf-8")
    assert "menu.addAction(pico_tutorial.MENU_TEXT, self.show_tutorial)" in src
    assert src.index('menu.addAction("Customise Pico..."') < src.index("menu.addAction(pico_tutorial.MENU_TEXT")
    assert src.index("menu.addAction(pico_tutorial.MENU_TEXT") < src.index('menu.addAction("Quit Pico"')


def test_nothing_opens_the_tutorial_by_itself():
    # Every start opens Customise (PICO_CONTRACT.md). The tutorial is offered, never shown unasked.
    src = (HERE / "sprite_pal.py").read_text(encoding="utf-8")
    assert "singleShot(300, pal.customise)" in src
    assert "singleShot" not in src.split("def show_tutorial")[1].split("def ")[0]
    assert src.count("show_tutorial") == 4      # the button's connect, the menu entry, and the two methods


def test_the_hotkey_shown_is_the_launchers_not_a_literal(monkeypatch):
    import shared.hotkey_label as hk
    monkeypatch.setattr(hk, "hotkey_label", lambda key, default: "Ctrl+Alt+F9" if key == "hotkey_pico" else default)
    assert "Ctrl+Alt+F9" in pico_tutorial.markup()


def test_the_gag_gap_the_tutorial_names_is_the_real_default():
    from pico import sprites
    assert sprites.GAG_COOLDOWN_S == 15 * 60        # "every 15 min", "four an hour at the very most"
    assert "every 15 min" in pico_tutorial.markup()


def test_every_name_in_bold_exists_where_pico_shows_it():
    markup = pico_tutorial.markup()
    assert len(guard.bold_spans(markup)) > 15          # the check below is not passing on an empty list
    strings = guard.source_strings(SOURCES)
    with open(HERE / "skill.json", encoding="utf-8") as f:
        strings.append(json.load(f)["name"])            # the tile's name, "Pico Pals"
    assert guard.problems(markup, strings, PROSE, ALIASES) == []


def test_no_qt_at_import_so_the_text_can_be_read_without_a_display():
    code = "import sys; sys.path.insert(0, %r); import pico_tutorial; pico_tutorial.pages(); " \
           "print(any(m.startswith('PySide6') for m in sys.modules))" % str(HERE)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", timeout=60)
    assert r.stdout.strip() == "False", r.stdout + r.stderr
