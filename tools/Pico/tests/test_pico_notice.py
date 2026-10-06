"""Pico Pals shows its legal notice: the exact words, in one place in code, where a user sees them.

The notice (pico_notice.py) is legal wording taken from Cloud Imperium's fan kit
documents. These fail if:

  - a character of it changes (the text is written out again below, and hashed);
  - a second copy of it appears in the tool, or it is wrapped in a translation
    call, or it turns up in the translators' template;
  - it is no longer in Customise Pico (the box every start opens) or no longer
    behind "About Pico Pals..." in his right-click menu;
  - it is shown smaller than 10 point, hidden, cut off, or as rich text;
  - it is put on Pico's own window.

The windows are built in a child process, offscreen, with APPDATA pointed at a
temp folder, like the other Pico window tests: a test must never show a window
on a desktop that already has a Pico, nor touch the user's settings.json.
"""
import ast
import hashlib
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

import pico_notice  # noqa: E402

# Written out a second time on purpose, with the (R) signs as escapes, so that this file and
# pico_notice.py cannot both be changed by one search-and-replace of the visible text.
CHARACTER_LINE = "Pico is a character from Star Citizen."
SHORT_FORM = (
    "Made By The Community. Star Citizen®, Roberts Space Industries® and Cloud Imperium® are"
    " registered trademarks of Cloud Imperium Rights LLC. Pico Pals is unofficial, free fan art, not"
    " endorsed by or affiliated with the Cloud Imperium or Roberts Space Industries group of companies."
    " Not for sale."
)
SHORT_FORM_SHA256 = "fde80d4f231dbda37277711f6c9c7af8ad21338ba127af5e968e082378158507"
MARKERS = {"_", "s_", "_t", "N_"}           # tools/extract_strings.py MARKER_FUNCS

CHILD = r'''
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QWidget
import pico_notice, sprite_pal

app = QApplication([])
out = {"settings": str(sprite_pal.SETTINGS), "platform": app.platformName()}


def facts(window):
    """What a user would get from the notice label inside *window*, once the window is laid out."""
    window.setAttribute(Qt.WA_DontShowOnScreen, True)
    window.show()
    app.processEvents()
    labels = window.findChildren(QLabel, pico_notice.LABEL_NAME)
    f = {"count": len(labels)}
    if labels:
        lbl = labels[0]
        f.update(text=lbl.text(), point=lbl.font().pointSizeF(), wraps=lbl.wordWrap(),
                 plain=lbl.textFormat() == Qt.PlainText, visible=lbl.isVisible(),
                 width=lbl.width(), height=lbl.height(), needs=lbl.heightForWidth(lbl.width()),
                 inside=window.rect().contains(lbl.geometry()))
    window.hide()
    return f


about = pico_notice.About(None)
out["about_title"] = about.windowTitle()
out["about_has_close"] = about.findChild(QDialogButtonBox).button(QDialogButtonBox.Close) is not None
out["about"] = facts(about)

host = QWidget()
box = sprite_pal.Customise(host, Path(sys.argv[2]), 280)      # an empty folder: no outfit is loaded
before = box.values()
out["customise"] = facts(box)
out["customise_values_unchanged"] = box.values() == before and box.changes() == {}

# The menu entry's handler, run on a plain widget standing in for Pico: it must open the About window.
opened = sprite_pal.Pal.show_about(host)
out["menu_opens_a_dialog"] = isinstance(opened, QDialog)
out["menu_dialog_title"] = opened.windowTitle()
out["menu_dialog_not_modal"] = not opened.isModal()
out["menu_dialog_is_kept"] = sprite_pal.Pal.show_about(host) is opened
opened.hide()
out["menu"] = facts(opened)

out["settings_written"] = Path(sprite_pal.SETTINGS).exists()
print("RESULT " + json.dumps(out))
'''


@pytest.fixture(scope="module")
def child(tmp_path_factory):
    pytest.importorskip("PySide6.QtWidgets")
    tmp = tmp_path_factory.mktemp("pico_notice")
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


def _calls(path: Path):
    return [n for n in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(n, ast.Call)]


def _marker(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Name) and f.id in MARKERS) or (isinstance(f, ast.Attribute) and f.attr in MARKERS)


# ── the words ────────────────────────────────────────────────────────────────

def test_the_notice_is_the_approved_wording_to_the_character():
    assert pico_notice.CHARACTER_LINE == CHARACTER_LINE
    assert pico_notice.SHORT_FORM == SHORT_FORM
    assert hashlib.sha256(pico_notice.SHORT_FORM.encode("utf-8")).hexdigest() == SHORT_FORM_SHA256
    assert pico_notice.SHORT_FORM.count("®") == 3
    assert pico_notice.text() == CHARACTER_LINE + "\n\n" + SHORT_FORM


def test_the_notice_is_written_in_one_place_only():
    # Any other file of the tool that spells the legal sentences out is a second copy that can drift.
    tells = ("registered trademarks of Cloud Imperium", "not endorsed by or affiliated with",
             "Made By The Community")
    copies = []
    for path in sorted(list(HERE.glob("*.py")) + list((HERE / "pico").glob("*.py"))):
        if path.name == "pico_notice.py":
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        copies += ["%s: %s" % (path.name, t) for t in tells if t in body]
    assert copies == []
    own = (HERE / "pico_notice.py").read_text(encoding="utf-8")
    assert own.count("registered trademarks of Cloud Imperium Rights LLC") == 1


def test_the_legal_wording_is_never_handed_to_a_translator():
    # 1. No translation call anywhere in pico_notice.py, and none around it where it is used.
    assert [ast.unparse(c) for c in _calls(HERE / "pico_notice.py") if _marker(c)] == []
    for name in ("sprite_pal.py", "pico_tutorial.py"):
        wrapped = [ast.unparse(c) for c in _calls(HERE / name) if _marker(c) and "pico_notice" in ast.unparse(c)]
        assert wrapped == [], name
    # 2. The toolbox's own extractor finds nothing to translate in the file.
    sys.path.insert(0, str(ROOT / "tools"))
    import extract_strings
    assert extract_strings.MARKER_FUNCS == MARKERS, "the extractor has new markers: check them here too"
    assert extract_strings.extract_strings_from_file(HERE / "pico_notice.py") == []
    # 3. And it is not in the translators' template (read only; this test never writes it).
    pot = ROOT / "locales" / "SC_Toolbox_Source.pot"
    if pot.is_file():
        body = pot.read_text(encoding="utf-8", errors="replace")
        for tell in ("registered trademarks of Cloud Imperium", "not endorsed by or affiliated with",
                     CHARACTER_LINE):
            assert tell not in body


def test_the_text_can_be_read_without_a_display_and_from_a_command_line():
    code = "import sys; sys.path.insert(0, %r); import pico_notice; pico_notice.text(); " \
           "print(any(m.startswith('PySide6') for m in sys.modules))" % str(HERE)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, encoding="utf-8", timeout=60)
    assert r.stdout.strip() == "False", r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(HERE / "pico_notice.py"), "--json"], capture_output=True, timeout=60)
    assert json.loads(r.stdout.decode("utf-8")) == {"character_line": CHARACTER_LINE, "short_form": SHORT_FORM}
    r = subprocess.run([sys.executable, str(HERE / "pico_notice.py")], capture_output=True, timeout=60)
    assert r.stdout.decode("utf-8").replace("\r\n", "\n") == CHARACTER_LINE + "\n\n" + SHORT_FORM + "\n"


# ── where a user sees them ───────────────────────────────────────────────────

def test_the_test_windows_cannot_touch_the_real_settings(child):
    assert child["platform"] == "offscreen"
    assert child["settings"].startswith(child["tmp"])
    assert not child["settings_written"]


@pytest.mark.parametrize("where", ["customise", "about", "menu"])
def test_the_notice_is_shown_whole_and_legible(child, where):
    f = child[where]
    assert f["count"] == 1, "%s: expected one notice label, found %d" % (where, f["count"])
    assert f["text"] == CHARACTER_LINE + "\n\n" + SHORT_FORM
    assert f["point"] >= 10.0, "%s: %.1f point; Cloud Imperium asks for no less than 10" % (where, f["point"])
    assert f["visible"] and f["inside"], "%s: the notice is hidden or outside its window" % where
    assert f["wraps"] and f["height"] >= f["needs"], "%s: the notice is cut off" % where
    assert f["plain"], "%s: the notice must be plain text, not markup" % where


def test_customise_shows_the_notice_and_it_changes_nothing_ok_saves(child):
    assert child["customise"]["count"] == 1
    assert child["customise_values_unchanged"]


def test_about_is_a_small_window_with_a_close_button(child):
    assert pico_notice.TITLE == "About Pico Pals"
    assert child["about_title"] == pico_notice.TITLE
    assert child["about_has_close"]


def test_the_right_click_menu_opens_about(child):
    assert pico_notice.MENU_TEXT == "About Pico Pals..."
    src = (HERE / "sprite_pal.py").read_text(encoding="utf-8")
    entry = "menu.addAction(pico_notice.MENU_TEXT, self.show_about)"
    assert src.count(entry) == 1
    menu = src.split("menu = QMenu(self)")[1].split("menu.exec(")[0]
    assert entry in menu, "the entry is not in Pico's right-click menu"
    assert menu.index("menu.addAction(pico_tutorial.MENU_TEXT") < menu.index(entry) < menu.index('"Quit Pico"')
    # ... and choosing it opens the window with the notice in it (run in the child, on a stand-in for Pico).
    assert child["menu_opens_a_dialog"] and child["menu_dialog_title"] == pico_notice.TITLE
    assert child["menu_dialog_not_modal"], "About from the menu must not block dragging Pico"
    assert child["menu_dialog_is_kept"], "asking twice must bring the same window forward, not open two"


def test_the_notice_is_never_on_pico_himself():
    # One label in sprite_pal.py, and it is the Customise box's. Nothing is drawn on or over the penguin.
    src = (HERE / "sprite_pal.py").read_text(encoding="utf-8")
    assert src.count("pico_notice.notice_label(") == 1
    customise = src.split("class Customise(QDialog):")[1].split("\nclass ")[0]
    assert "pico_notice.notice_label(self)" in customise
    pal = src.split("class Pal(QWidget):")[1].split("\ndef ")[0]
    assert "notice_label" not in pal and "SHORT_FORM" not in pal and "CHARACTER_LINE" not in pal
    assert "show_about" not in src.split("def main(")[1], "nothing opens About by itself"
