"""Opening "Customise Pico" and pressing OK must never change what Pico does.

2026-10-04: the gag cooldown's default became 15 minutes (sprites.GAG_COOLDOWN_S = 900), but the "Gags at
most" box only offered 30 / 60 / 120 / 240 and showed "once an hour" when nothing was saved. Pressing OK
then saved 60, and the dialog opens on every launch, so the new default lasted until the first OK.

Each case runs the real Pal.customise in a child process, offscreen, with APPDATA pointed at a temp folder:
a test must never show a window on a desktop that already has a Pico, nor touch the user's settings.json.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from pico import sprites  # noqa: E402

CHILD = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget
import sprite_pal
from pico import sprites

spec = json.loads(sys.argv[2])
root = Path(sys.argv[3])                 # an empty folder: no outfit is loaded, the settings are still saved
if spec.get("saved") is not None:
    sprite_pal.save_settings(spec["saved"])
app = QApplication([])
out = {"settings": str(sprite_pal.SETTINGS), "platform": app.platformName(),
       "choices": [m for _l, m in sprite_pal.GAG_CHOICES]}


def state(dlg):
    return {"mins": dlg.often.currentData(), "label": dlg.often.currentText(),
            "items": [dlg.often.itemData(i) for i in range(dlg.often.count())],
            "lively": dlg.lively.currentData(), "size": dlg.size.value(),
            "lively_items": [dlg.lively.itemData(i) for i in range(dlg.lively.count())]}


def fake_exec(dlg):                      # the user: look, maybe pick a cooldown, press OK. Never shown.
    out["shown"] = state(dlg)
    if spec.get("pick") is not None:
        i = dlg.often.findData(spec["pick"])
        out["pick_index"] = i
        dlg.often.setCurrentIndex(i)
    dlg.accept()
    return QDialog.Accepted


sprite_pal.Customise.exec = fake_exec


class Host(QWidget):
    """Just enough of Pal to run its own customise() and remember()."""
    customise = sprite_pal.Pal.customise
    remember = sprite_pal.Pal.remember

    def __init__(self):
        super().__init__()
        self.chooser = SimpleNamespace(catalog=SimpleNamespace(root=root))
        self.height_px = int((spec.get("saved") or {}).get("height", sprite_pal.HEIGHT))
        self.why = QLabel(self)

    def tick(self):
        pass


host = Host()
host.customise()
out["file"] = sprite_pal.load_settings()
chooser = SimpleNamespace()
sprites.LoopChooser.apply_prefs(chooser, out["file"])       # what the chooser makes of the saved file
out["cooldown_s"] = chooser.gag_cooldown_s
out["rest_scale"] = chooser.rest_scale
out["height_px"] = host.height_px
out["reopened"] = state(sprite_pal.Customise(None, root, host.height_px))
print("RESULT " + json.dumps(out))
'''


def accept(tmp_path, saved=None, pick=None):
    """Open the dialog over `saved` settings, optionally pick a cooldown, press OK. What happened."""
    appdata = tmp_path / "appdata"
    root = tmp_path / "no_outfit"
    appdata.mkdir()
    root.mkdir()
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=str(appdata), PYTHONIOENCODING="utf-8")
    env.pop("QT_SCALE_FACTOR", None)
    r = subprocess.run([sys.executable, "-c", CHILD, str(HERE), json.dumps({"saved": saved, "pick": pick}),
                        str(root)], env=env, capture_output=True, text=True, timeout=120)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert r.returncode == 0 and lines, "child failed:\n%s\n%s" % (r.stdout, r.stderr)
    out = json.loads(lines[-1][len("RESULT "):])
    assert out["platform"] == "offscreen"
    assert Path(out["settings"]).parent.parent == appdata      # never the user's own settings file
    return out


def test_ok_with_nothing_saved_keeps_the_default_cooldown(tmp_path):
    out = accept(tmp_path)
    assert out["shown"]["mins"] * 60 == sprites.GAG_COOLDOWN_S
    assert out["cooldown_s"] == sprites.GAG_COOLDOWN_S
    assert "gag_cooldown_min" not in out["file"]                # nobody chose one, so none is saved


def test_the_dialogs_default_is_the_choosers_constant_and_is_on_the_list(tmp_path):
    out = accept(tmp_path)
    default_min = sprites.GAG_COOLDOWN_S / 60
    assert out["shown"]["mins"] == default_min
    assert default_min in out["choices"], "GAG_COOLDOWN_S changed: give it a line in sprite_pal.GAG_CHOICES"
    assert out["shown"]["items"] == out["choices"]              # the default needed no line of its own


def test_fifteen_minutes_can_be_chosen_saved_and_loaded(tmp_path):
    out = accept(tmp_path, saved={"gag_cooldown_min": 60}, pick=15)
    assert out["shown"]["mins"] == 60 and out["shown"]["label"] == "once an hour"
    assert out["pick_index"] >= 0, "the box has no 15-minute choice"
    assert out["file"]["gag_cooldown_min"] == 15
    assert out["cooldown_s"] == 900.0
    assert out["reopened"]["mins"] == 15 and out["reopened"]["label"] == "every 15 min"


def test_a_saved_value_that_is_not_a_choice_is_shown_and_survives_ok(tmp_path):
    out = accept(tmp_path, saved={"gag_cooldown_min": 45})
    assert out["shown"]["mins"] == 45 and out["shown"]["label"] == "every 45 min"
    assert out["file"]["gag_cooldown_min"] == 45
    assert out["cooldown_s"] == 45 * 60


def test_ok_rewrites_no_other_setting_it_could_not_show_exactly(tmp_path):
    saved = {"gag_cooldown_min": 30, "liveliness": "hyper", "height": 600, "gags": False, "signs": False}
    out = accept(tmp_path, saved=saved)
    assert out["shown"]["lively"] == "normal"                   # an unknown word rests as "normal" does
    assert out["shown"]["size"] == 560                          # the slider stops there
    assert {k: out["file"][k] for k in saved} == saved
    assert out["height_px"] == 600 and out["rest_scale"] == 1.0


def test_the_how_lively_box_offers_exactly_the_choosers_words(tmp_path):
    out = accept(tmp_path)
    assert sorted(out["shown"]["lively_items"]) == sorted(sprites.LIVELINESS)
    assert out["shown"]["lively"] == "normal" and out["rest_scale"] == 1.0
    assert "liveliness" not in out["file"]
