"""Outfit packs in "Customise Pico": what the list says, and what picking, cancelling and failing do.

  - the list shows the shipped outfit, the downloaded ones, and the download size of the ones not here;
    opening the box requests nothing;
  - with no pack address the outfits not here are shown but cannot be picked, and the box says why;
  - picking an outfit that is not here downloads it (real thread, real progress box), saves it as his
    outfit only once it is unpacked, and takes the old one off;
  - a download that fails or is cancelled changes nothing: same outfit, same settings.json, and a failure
    is said in plain words in a box that stays until it is closed;
  - "Download every Pal now" fetches what is missing and the list then says so;
  - the developer's loop folders, when the PC has them, stay first in the list and behave as before.

Each case runs the real Pal.customise in a child process, offscreen, with APPDATA pointed at a temp folder,
like the other Pico window tests. The pack server is tests/_pack_fixtures.py, in this process.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
for _p in (str(HERE), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pico import packs  # noqa: E402
import _pack_fixtures as fx  # noqa: E402

BLOBS = {code: fx.make_pack(code, outfit) for code, outfit in fx.OUTFITS.items()}

CHILD = r'''
import json, sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QLabel, QWidget
import sprite_pal
from pico import packs, sprites

spec = json.loads(sys.argv[2])
if spec.get("saved") is not None:
    sprite_pal.save_settings(spec["saved"])
real_outfits = sprite_pal.outfits
dev = spec.get("dev")
sprite_pal.outfits = (lambda: real_outfits(Path(dev))) if dev else (lambda: {})   # never this PC's own folders
app = QApplication([])
store = packs.PackStore(home=spec["home"], bundled=spec["bundled"], base_url=spec["url"], allow_loopback_http=True)
store.reconcile()
out = {"settings": str(sprite_pal.SETTINGS), "platform": app.platformName(), "opens": [], "boxes": [],
       "ticks": 0}


def state(dlg):
    box = dlg.outfit
    return {"rows": [{"text": box.itemText(i), "data": box.itemData(i), "on": box.model().item(i).isEnabled()}
                     for i in range(box.count())],
            "current": box.currentData(),
            "note": "" if dlg.pack_note.isHidden() else dlg.pack_note.text(),
            "button": dlg.all_btn.text(), "button_on": dlg.all_btn.isEnabled(),
            "button_shown": not dlg.all_btn.isHidden(),
            "cost": "" if dlg.all_note.isHidden() else dlg.all_note.text(),
            "asked": list(store.requests)}


def fake_exec(dlg):                      # the user: look, maybe press the button, maybe pick, then OK or Cancel
    out["opens"].append(state(dlg))
    if spec.get("all"):
        dlg.all_btn.click()
        out["after_all"] = state(dlg)
    if spec.get("pick") is not None:
        i = dlg.outfit.findData(spec["pick"])
        out["pick_index"] = i
        dlg.outfit.setCurrentIndex(i)
        out["picked"] = dlg.outfit.currentData()
    if spec.get("close") == "cancel":
        dlg.reject()
        return QDialog.Rejected
    dlg.accept()
    return QDialog.Accepted


sprite_pal.Customise.exec = fake_exec


def watch():                             # the user again, at the progress box
    for w in app.topLevelWidgets():
        if not isinstance(w, sprite_pal.PackProgress) or not w.isVisible():
            continue
        if w.failed and not getattr(w, "_seen", False):
            w._seen = True
            out["boxes"].append({"failed": w.failed, "text": w.text.text(), "button": w.button.text(),
                                 "bar": not w.bar.isHidden(), "title": w.windowTitle()})
            w.button.click()
        elif spec.get("cancel") and w.text.text().startswith("Downloading") and not getattr(w, "_c", False):
            w._c = True
            out["boxes"].append({"cancelled_at": w.text.text(), "button": w.button.text()})
            w.button.click()


timer = QTimer()
timer.timeout.connect(watch)
timer.start(20)


class Host(QWidget):
    """Just enough of Pal to run its own customise(), with a real pack store."""
    customise = sprite_pal.Pal.customise
    remember = sprite_pal.Pal.remember
    get_pack = sprite_pal.Pal.get_pack
    take_off = sprite_pal.Pal.take_off

    def __init__(self):
        super().__init__()
        self.store, self.worn = store, spec.get("wearing")
        root = store.activate(self.worn) if self.worn else Path(spec["empty"])
        self.chooser = SimpleNamespace(catalog=SimpleNamespace(root=root))
        self.height_px = sprite_pal.HEIGHT
        self.why, self.pic = QLabel(self), QLabel(self)

    def tick(self):
        out["ticks"] += 1


host = Host()
out["root_before"] = str(host.chooser.catalog.root)
for _ in range(spec.get("times", 1)):
    host.customise()
out["file"] = sprite_pal.load_settings()
out["root"] = str(host.chooser.catalog.root)
out["worn"] = host.worn
out["unpacked"] = store.unpacked()
out["asked"] = list(store.requests)
out["tooltip"] = host.pic.toolTip()
out["pack_files"] = sorted(p.name for p in store.packs_dir.iterdir()) if store.packs_dir.is_dir() else []
print("RESULT " + json.dumps(out))
'''


def run(tmp_path, url, bundled=("o08",), user=(), blobs=BLOBS, **spec):
    appdata, home, inst, empty = tmp_path / "appdata", tmp_path / "appdata" / "PicoPal", tmp_path / "install", tmp_path / "none"
    (home / "packs").mkdir(parents=True)
    inst.mkdir()
    empty.mkdir()
    (inst / "packs.json").write_text(json.dumps(fx.make_manifest(blobs)), encoding="utf-8")
    for code in bundled:
        (inst / (code + ".tar.xz")).write_bytes(blobs[code])
    for code in user:
        (home / "packs" / (code + ".tar.xz")).write_bytes(blobs[code])
    spec.update(home=str(home), bundled=str(inst), url=url, empty=str(empty))
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=str(appdata), PYTHONIOENCODING="utf-8")
    env.pop("QT_SCALE_FACTOR", None)
    r = subprocess.run([sys.executable, "-c", CHILD, str(HERE), json.dumps(spec)], env=env,
                       capture_output=True, text=True, timeout=180)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert r.returncode == 0 and lines, "child failed:\n%s\n%s" % (r.stdout, r.stderr)
    out = json.loads(lines[-1][len("RESULT "):])
    assert out["platform"] == "offscreen"
    assert Path(out["settings"]).parent.parent == appdata      # never the user's own settings file
    return out


@pytest.fixture
def server():
    s = fx.PretendServer(fx.site(BLOBS))
    yield s
    s.stop()


def texts(state):
    return [r["text"] for r in state["rows"]]


def size(code):
    return packs.mb(len(BLOBS[code]))


def test_the_list_shows_what_is_here_and_the_size_of_what_is_not_and_asks_for_nothing(tmp_path, server):
    out = run(tmp_path, server.url, user=("o02",), wearing="o08")
    s = out["opens"][0]
    assert texts(s) == ["Anvil  (downloaded)", "Banu  (%s download)" % size("o05"), "Drake  (included)",
                        "Origin  (%s download)" % size("o16")]
    assert all(r["on"] for r in s["rows"]) and s["current"] == "pack:o08"
    two = packs.mb(len(BLOBS["o05"]) + len(BLOBS["o16"]))
    assert s["button"] == "Download every Pal now  (2 to fetch, %s)" % two and s["button_on"]
    assert "first time you pick it" in s["note"] and "kept on this PC" in s["note"]
    assert "stay unpacked" in s["cost"] and packs.mb(sum(len(b) for b in BLOBS.values())) in s["cost"]
    assert s["asked"] == [] and out["asked"] == [] and server.hits == []
    assert "outfit" not in out["file"] and out["worn"] == "o08"       # OK on an untouched box changes nothing


def test_with_no_pack_address_outfits_not_here_are_shown_but_cannot_be_picked(tmp_path, server):
    out = run(tmp_path, "", wearing="o08")
    s = out["opens"][0]
    assert texts(s) == ["Anvil  (not available yet)", "Banu  (not available yet)", "Drake  (included)",
                        "Origin  (not available yet)"]
    assert [r["on"] for r in s["rows"]] == [False, False, True, False]
    assert s["note"] == "More outfits will be available here once online packs are set up."
    assert s["button_shown"] and not s["button_on"]
    assert out["asked"] == [] and server.hits == []


def test_picking_an_outfit_that_is_not_here_downloads_it_wears_it_and_takes_the_old_one_off(tmp_path, server):
    out = run(tmp_path, server.url, wearing="o08", pick="pack:o02")
    assert out["picked"] == "pack:o02" and out["boxes"] == []
    assert server.hits == ["/packs/o02.tar.xz"]
    assert out["file"]["outfit"] == "pack:o02" and out["worn"] == "o02"
    assert Path(out["root"]).name == "pico_anim_sequences_anvil" and Path(out["root"]).parent.name == "o02"
    assert out["unpacked"] == ["o02"]                                 # Drake's unpacked files are gone
    assert out["pack_files"] == ["have.json", "o02.tar.xz"] and out["ticks"] == 1


def test_picking_an_outfit_that_is_here_asks_for_nothing(tmp_path, server):
    out = run(tmp_path, server.url, user=("o05",), wearing="o08", pick="pack:o05")
    assert out["file"]["outfit"] == "pack:o05" and out["worn"] == "o05" and out["boxes"] == []
    assert server.hits == [] and out["asked"] == []


def test_with_the_server_down_he_keeps_his_outfit_and_a_box_says_so_plainly(tmp_path):
    out = run(tmp_path, fx.dead_url(), wearing="o08", pick="pack:o02")
    assert len(out["boxes"]) == 1
    box = out["boxes"][0]
    assert box["text"].startswith("That did not work: the pack server could not be reached")
    assert box["text"].endswith("Pico keeps wearing Drake.") and "Traceback" not in box["text"]
    assert box["button"] == "Close" and not box["bar"]
    assert "outfit" not in out["file"] and out["worn"] == "o08" and out["root"] == out["root_before"]
    assert out["unpacked"] == ["o08"] and "outfit not changed" in out["tooltip"]


def test_a_pack_that_fails_its_checksum_is_not_worn(tmp_path, server):
    server.files["o02.tar.xz"] = fx.make_pack("o02", "anvil", salt="not the listed one")
    out = run(tmp_path, server.url, wearing="o08", pick="pack:o02")
    assert len(out["boxes"]) == 1 and "checksum" in out["boxes"][0]["text"]
    assert out["boxes"][0]["text"].endswith("Pico keeps wearing Drake.")
    assert "outfit" not in out["file"] and out["worn"] == "o08" and out["unpacked"] == ["o08"]
    assert "o02.tar.xz" not in out["pack_files"] and not [f for f in out["pack_files"] if f.endswith(".part")]


def test_a_download_can_be_cancelled_and_nothing_changes(tmp_path):
    big = dict(BLOBS, o02=fx.make_pack("o02", "anvil", size=40000))
    server = fx.PretendServer(fx.site(big), delay=0.02)
    try:
        out = run(tmp_path, server.url, blobs=big, wearing="o08", pick="pack:o02", cancel=True)
    finally:
        server.stop()
    assert len(out["boxes"]) == 1 and out["boxes"][0]["button"] == "Cancel"
    assert out["boxes"][0]["cancelled_at"].startswith("Downloading Anvil: ")
    assert "outfit" not in out["file"] and out["worn"] == "o08" and out["unpacked"] == ["o08"]
    assert not [f for f in out["pack_files"] if f.startswith("o02")]


def test_download_every_pal_fetches_what_is_missing_and_the_list_says_so(tmp_path, server):
    out = run(tmp_path, server.url, user=("o02",), wearing="o08", all=True)
    assert server.packs_asked() == ["packs.json", "o05.tar.xz", "o16.tar.xz"] and out["boxes"] == []
    s = out["after_all"]
    assert texts(s) == ["Anvil  (downloaded)", "Banu  (downloaded)", "Drake  (included)", "Origin  (downloaded)"]
    assert s["button"] == "Every Pal is downloaded" and not s["button_on"]
    assert s["cost"].startswith("Every Pal is on this PC") and s["current"] == "pack:o08"
    assert "outfit" not in out["file"] and out["unpacked"] == ["o08"]  # downloading unpacks nothing


def test_download_every_pal_with_the_server_down_says_so_and_changes_nothing(tmp_path):
    out = run(tmp_path, fx.dead_url(), wearing="o08", all=True)
    assert len(out["boxes"]) == 1 and "could not be reached" in out["boxes"][0]["text"]
    assert out["boxes"][0]["title"] == "Download every Pal"
    assert texts(out["after_all"]) == texts(out["opens"][0]) and out["after_all"]["button_on"]


def test_download_every_pal_says_which_pal_stopped_it_and_keeps_the_ones_that_arrived(tmp_path, server):
    server.files["o16.tar.xz"] = b"not the pack in the list"
    out = run(tmp_path, server.url, wearing="o08", all=True)
    assert len(out["boxes"]) == 1
    text = out["boxes"][0]["text"]
    assert "checksum" in text and "stopped the download at Origin" in text
    assert text.endswith("Every Pal downloaded so far is kept.")
    s = out["after_all"]
    assert texts(s)[:2] == ["Anvil  (downloaded)", "Banu  (downloaded)"] and "download)" in texts(s)[3]
    assert s["button"].startswith("Download every Pal now  (1 to fetch") and s["button_on"]


def test_download_every_pal_says_which_pal_stopped_it_and_keeps_the_ones_that_arrived(tmp_path, server):
    server.files["o16.tar.xz"] = b"not the pack in the list"
    out = run(tmp_path, server.url, wearing="o08", all=True)
    assert len(out["boxes"]) == 1
    text = out["boxes"][0]["text"]
    assert "checksum" in text and "stopped the download at Origin" in text
    assert text.endswith("Every Pal downloaded so far is kept.")
    s = out["after_all"]
    assert texts(s)[:2] == ["Anvil  (downloaded)", "Banu  (downloaded)"] and "download)" in texts(s)[3]
    assert s["button"].startswith("Download every Pal now  (1 to fetch") and s["button_on"]


def test_with_every_pal_downloaded_the_old_outfit_stays_unpacked(tmp_path, server):
    out = run(tmp_path, server.url, user=("o02", "o05", "o16"), wearing="o08", pick="pack:o02")
    assert out["worn"] == "o02" and out["unpacked"] == ["o02", "o08"] and server.hits == []


def test_the_download_on_first_pick_sentence_is_shown_once(tmp_path, server):
    out = run(tmp_path, server.url, wearing="o08", times=2, close="cancel")
    assert "first time you pick it" in out["opens"][0]["note"]
    assert out["opens"][1]["note"] == ""
    assert out["file"] == {"packs_told": True}                        # Cancel saved nothing else
    assert out["opens"][1]["button_on"]                               # the option itself is always there


def test_the_developers_loop_folders_stay_first_and_are_picked_as_before(tmp_path, server):
    dev = tmp_path / "VNCCS" / "pico_anim_sequences"
    cnou = tmp_path / "VNCCS" / "pico_anim_sequences_cnou"
    for d in (dev, cnou):
        d.mkdir(parents=True)
        for name in fx.LOOPS:
            (d / (name + ".webp")).write_bytes(b"x")
    out = run(tmp_path, server.url, wearing="o08", dev=str(dev), pick=str(cnou))
    rows = out["opens"][0]["rows"]
    assert [(r["text"], r["data"]) for r in rows[:2]] == [("Drake", str(dev)), ("Cnou", str(cnou))]
    assert rows[2]["data"] is None and rows[2]["text"] == ""          # a separator, then the packs
    assert [r["data"] for r in rows[3:]] == ["pack:o02", "pack:o05", "pack:o08", "pack:o16"]
    assert out["file"]["outfit"] == str(cnou) and out["root"] == str(cnou) and out["worn"] is None
    assert out["unpacked"] == [] and server.hits == []                # the pack outfit he wore is taken off


# ---- the real window, with real loops ------------------------------------------------------------------------
# The cases above run customise() on a stand-in with no animation playing. This one is the real Pal window
# playing real loops, because that is where Windows gets a say: a folder cannot be removed while a loop in
# it is open, and the first version of this left every outfit but the first unpacked for that reason.
# It needs real art, so it uses the shipped pack (tools/Pico/packs) twice, under two codes, and is skipped
# on a PC that has no shipped pack.

REAL_CHILD = r"""
import json, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from PySide6.QtWidgets import QApplication, QDialog
import sprite_pal
from pico import packs, sprites

spec = json.loads(sys.argv[2])
sprite_pal.outfits = lambda: {}
app = QApplication([])
store = packs.PackStore(home=spec["home"], bundled=spec["bundled"], base_url="")
store.reconcile()
folder, code = sprite_pal.start_outfit(sprites.DEFAULT_DIR, {"outfit": "pack:o08"}, store)
pal = sprite_pal.Pal(sprites.LoopChooser(sprites.Catalog.scan(folder)), None, None, pinned="happy")
pal.store, pal.worn = store, code
pal.show()
picks = iter(spec["picks"])


def fake_exec(dlg):
    dlg.outfit.setCurrentIndex(dlg.outfit.findData(next(picks)))
    dlg.accept()
    return QDialog.Accepted


sprite_pal.Customise.exec = fake_exec
out = {"platform": app.platformName(), "settings": str(sprite_pal.SETTINGS), "start": code, "steps": []}
for _ in spec["picks"]:
    pal.customise()
    end = time.time() + 0.6
    while time.time() < end:                 # let him play for a moment, as he would
        app.processEvents()
        time.sleep(0.01)
    out["steps"].append({"worn": pal.worn, "unpacked": store.unpacked(), "name": sprite_pal.outfit_name(
        pal.chooser.catalog.root), "movie_in": store.code_of(Path(pal.movie.fileName()).parent),
        "frames": pal.movie.frameCount(), "saved": sprite_pal.load_settings().get("outfit")})
print("RESULT " + json.dumps(out))
"""


def test_the_real_window_takes_the_old_outfit_off_while_loops_are_playing(tmp_path):
    man = packs.PackStore._read_manifest(packs.BUNDLED_DIR / "packs.json")
    real = packs.BUNDLED_DIR / man.entries[man.default].pack if man.default else None
    if real is None or not real.is_file():
        pytest.skip("no shipped pack in tools/Pico/packs on this PC")
    blob = real.read_bytes()
    blobs = {"o05": blob, "o08": blob, "o16": blob}            # real art under three codes; o16 is not on disk
    appdata, home, inst = tmp_path / "appdata", tmp_path / "appdata" / "PicoPal", tmp_path / "install"
    (home / "packs").mkdir(parents=True)
    inst.mkdir()
    (inst / "packs.json").write_text(json.dumps(fx.make_manifest(blobs)), encoding="utf-8")
    (inst / "o08.tar.xz").write_bytes(blob)
    (home / "packs" / "o05.tar.xz").write_bytes(blob)
    spec = {"home": str(home), "bundled": str(inst), "picks": ["pack:o05", "pack:o08", "pack:o05"]}
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen", APPDATA=str(appdata), PYTHONIOENCODING="utf-8")
    env.pop("QT_SCALE_FACTOR", None)
    r = subprocess.run([sys.executable, "-c", REAL_CHILD, str(HERE), json.dumps(spec)], env=env,
                       capture_output=True, text=True, timeout=180)
    lines = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    assert r.returncode == 0 and lines, "child failed:\n%s\n%s" % (r.stdout, r.stderr)
    out = json.loads(lines[-1][len("RESULT "):])
    assert out["platform"] == "offscreen" and Path(out["settings"]).parent.parent == appdata
    assert out["start"] == "o08"
    assert [s["worn"] for s in out["steps"]] == ["o05", "o08", "o05"]
    assert [s["name"] for s in out["steps"]] == ["Banu", "Drake", "Banu"]
    for s in out["steps"]:
        assert s["unpacked"] == [s["worn"]], out["steps"]      # only the worn outfit is unpacked
        assert s["movie_in"] == s["worn"] and s["frames"] > 1 and s["saved"] == "pack:" + s["worn"]
    assert sorted(p.name for p in (home / "packs").glob("*.tar.xz")) == ["o05.tar.xz"]     # the pack stayed

def test_an_empty_packs_url_in_settings_keeps_pico_offline_and_no_setting_uses_the_site():
    import sprite_pal as sp
    from pico import packs as pk
    assert sp.open_store({"packs_url": ""}).online is False
    assert sp.open_store({}).base_url == pk.PACKS_URL.rstrip("/") and sp.open_store({}).online is True
    assert sp.open_store({"packs_url": None}).base_url == pk.PACKS_URL.rstrip("/")
