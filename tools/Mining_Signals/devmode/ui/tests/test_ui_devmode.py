"""UI tests for Mining Signals Dev Mode, run offscreen against FakeBackend.

    QT_QPA_PLATFORM=offscreen python -m pytest tools/Mining_Signals/devmode/ui/tests -q
"""

from __future__ import annotations

import os
import sys
import zipfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_SKILL = Path(__file__).resolve().parents[3]          # tools/Mining_Signals
_ROOT = _SKILL.parents[1]                             # repo root
for _p in (str(_ROOT), str(_SKILL)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402
import shiboken6  # noqa: E402
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from shared.qt.theme import apply_theme  # noqa: E402
from devmode.ui import window as window_mod  # noqa: E402
from devmode.ui._fake import FakeBackend  # noqa: E402
from devmode.ui.backend import BackendHandle, CONTRACT, load_backend, missing_functions  # noqa: E402
from devmode.ui.window import DevModeWindow, open_dev_mode  # noqa: E402
from devmode.ui.workers import drain  # noqa: E402

_APP = QApplication.instance() or QApplication([])
apply_theme(_APP)


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("SC_DEVMODE_ROOT", str(tmp_path))
    return FakeBackend(root=str(tmp_path), delay=0, torch_installed=False)


@pytest.fixture
def win(fake):
    w = DevModeWindow(handle=BackendHandle(fake, "test"))
    w.show()
    assert drain(), "background jobs did not finish"
    yield w
    if shiboken6.isValid(w):
        w.close()
    drain()


def _calls(fake, name):
    return [c for c in fake.calls if c[0] == name]


# ── backend adapter ──────────────────────────────────────────────────────

def test_fake_backend_implements_whole_contract(fake):
    assert missing_functions(fake) == []
    assert len(CONTRACT) >= 26


def test_env_selects_fake_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("SC_DEVMODE_FAKE", "1")
    monkeypatch.setenv("SC_DEVMODE_ROOT", str(tmp_path))
    h = load_backend()
    assert h.is_fake and isinstance(h.api, FakeBackend)


# ── window ───────────────────────────────────────────────────────────────

def test_window_opens_with_rail_and_demo_banner(win):
    assert win.isVisible()
    assert [i.name.text() for i in win.rail_items] == [
        "ENGINE", "CAPTURE", "LABEL", "GLYPHS", "SYNTH", "TRAIN & TEST", "EXPORT"]
    assert win.demo_banner is not None and "DEMO DATA" in win.demo_banner.label.text()
    assert win.current_step == 2                  # opens on Label, the most-used screen


def test_every_step_renders(win):
    for i, page in enumerate(win.pages):
        win.select_step(i)
        assert drain()
        assert win.current_step == i
        assert page.isVisible()
        assert win.rail_items[i].isChecked()
        assert not win.grab().isNull()
    # every page reported a status line for the rail except Synth (no action yet)
    subs = [item.sub.text() for item in win.rail_items]
    assert all(subs[i] for i in (0, 1, 2, 3, 5, 6)), subs


def test_ctrl_number_switches_step(win):
    from PySide6.QtGui import QKeySequence, QShortcut
    shortcuts = {sc.key().toString(): sc for sc in win.findChildren(QShortcut)}
    for n in range(1, 8):
        assert f"Ctrl+{n}" in shortcuts
    shortcuts["Ctrl+6"].activated.emit()
    drain()
    assert win.current_step == 5
    shortcuts["Ctrl+1"].activated.emit()
    drain()
    assert win.current_step == 0


# ── label: the keyboard flow ─────────────────────────────────────────────

def test_label_enter_confirms_proposal(win, fake):
    lp = win.label_page
    first = lp._current()
    assert first["status"] == "proposed" and first["proposed"]
    QTest.keyClick(lp.entry, Qt.Key_Return)
    drain()
    assert _calls(fake, "confirm") == [("confirm", first["id"], first["proposed"])]
    assert first["status"] == "confirmed"
    assert lp._current() is not first               # advanced to the next crop


def test_label_typing_replaces_suggestion_then_enter(win, fake):
    lp = win.label_page
    target = lp._current()
    QTest.keyClicks(lp.entry, "12,345")               # suggestion arrives pre-selected
    assert lp.entry.text() == "12,345"
    QTest.keyClick(lp.entry, Qt.Key_Enter)
    drain()
    assert ("confirm", target["id"], "12,345") in fake.calls
    assert "corrected" in lp.message.text()


def test_label_r_rejects_and_letters_are_not_typed(win, fake):
    lp = win.label_page
    target = lp._current()
    QTest.keyClick(lp.entry, Qt.Key_R)
    drain()
    assert _calls(fake, "reject") == [("reject", target["id"])]
    assert "r" not in lp.entry.text().lower()
    QTest.keyClicks(lp.entry, "abc")                  # the validator keeps it numeric
    assert not any(ch.isalpha() for ch in lp.entry.text())


def test_label_arrows_move_without_saving(win, fake):
    lp = win.label_page
    start = lp._idx
    QTest.keyClick(lp.entry, Qt.Key_Right)
    assert lp._idx == start + 1
    QTest.keyClick(lp.entry, Qt.Key_Left)
    assert lp._idx == start
    QTest.keyClick(lp.entry, Qt.Key_PageDown)
    assert lp._idx == min(start + 10, len(lp._items) - 1)
    drain()
    assert not _calls(fake, "confirm") and not _calls(fake, "reject")


def test_label_escape_restores_suggestion(win):
    lp = win.label_page
    proposed = lp._current()["proposed"]
    QTest.keyClicks(lp.entry, "999")
    QTest.keyClick(lp.entry, Qt.Key_Escape)
    assert lp.entry.text() == proposed


def test_label_empty_value_is_not_confirmed(win, fake):
    lp = win.label_page
    unl = next(i for i, it in enumerate(lp._items) if not it.get("proposed"))
    lp.goto(unl)
    assert lp.entry.text() == ""
    QTest.keyClick(lp.entry, Qt.Key_Return)
    drain()
    assert not _calls(fake, "confirm")
    assert "Type the value" in lp.message.text()


def test_label_failed_save_is_put_back(win, fake, monkeypatch):
    lp = win.label_page
    target = lp._current()

    def boom(capture_id, label):
        raise OSError("disk full")
    monkeypatch.setattr(fake, "confirm", boom)
    QTest.keyClick(lp.entry, Qt.Key_Return)
    assert drain()
    assert target["status"] == "proposed"
    assert "disk full" in lp.message.text()


def test_label_shows_proposal_source(win):
    lp = win.label_page
    seen = set()
    for i, it in enumerate(lp._items):
        lp.goto(i)
        src = it.get("proposal_source") if it.get("proposed") else None
        seen.add(src)
        badge = lp.badge.text()
        if src == "consensus":
            assert badge.startswith("CONSENSUS")
        elif src == "import":
            assert badge == "IMPORT" and "1 in 5" in lp.trust.text()
        elif src == "reader":
            assert badge == "READER"
        else:
            assert badge == "NO PROPOSAL"
    assert {"consensus", "reader", "import", None} <= seen


def test_label_stats_bar(win, fake):
    lp = win.label_page
    QTest.qWait(400)                                  # stats are debounced by 250 ms
    assert drain()
    st = fake.label_stats()
    assert lp.chips["confirmed"].text().endswith(f"<b>{st['confirmed']}</b>")


# ── glyphs ───────────────────────────────────────────────────────────────

def test_glyphs_weak_classes_flagged_and_approve(win, fake):
    win.select_step(3)
    drain()
    gp = win.pages[3]
    assert "weak:" in gp.status.text() and "9" in gp.status.text()
    gp.set_char("5")
    drain()
    assert gp.current_char == "5" and gp.grid.count() > 0
    gp.grid.item(0).setSelected(True)
    gid = gp.grid.item(0).data(Qt.UserRole)
    gp.approve_selected()
    drain()
    assert ("approve_glyph", gid) in fake.calls


# ── engine gate + training ───────────────────────────────────────────────

def test_train_disabled_until_engine_installed(win, fake):
    win.select_step(5)
    drain()
    tp = win.train_page
    assert not tp.banner.isHidden()
    for row in tp.rows.values():
        assert not row.train.isEnabled()
        assert "Engine" in row.train.toolTip()
    tp.train("signal")                                # a stray call must not train
    drain()
    assert not _calls(fake, "train")

    win.select_step(0)
    win.engine_page.install()
    assert drain()
    assert _calls(fake, "install_torch")
    assert tp.banner.isHidden()
    assert all(row.train.isEnabled() for row in tp.rows.values())


def test_activate_only_when_candidate_is_better(fake, tmp_path):
    fake._torch = True
    w = DevModeWindow(handle=BackendHandle(fake, "test"))
    w.show()
    drain()
    w.select_step(5)
    drain()
    tp = w.train_page
    assert not any(r.activate.isEnabled() for r in tp.rows.values())   # nothing trained

    tp.train("hud")                                   # fake: worse than stock
    tp.train("signal_rgb")                            # fake: better than stock
    assert drain()
    assert not tp.rows["hud"].activate.isEnabled()
    assert "not better" in tp.rows["hud"].reason.text()
    assert tp.rows["signal_rgb"].activate.isEnabled()

    tp.rows["signal_rgb"].activate.click()
    assert drain()
    assert ("activate", "signal_rgb") in fake.calls
    assert fake.active_model_path("signal_rgb")
    assert tp.rows["signal_rgb"].revert.isEnabled()
    tp.rows["signal_rgb"].revert.click()
    assert drain()
    assert fake.active_model_path("signal_rgb") is None
    w.close()
    drain()


# ── synth / capture / export ─────────────────────────────────────────────

def test_synth_generates_and_shows_counts(win, fake):
    win.select_step(4)
    drain()
    sp = win.pages[4]
    sp.per_class.setValue(100)
    sp.generate()
    assert drain()
    assert ("generate_synth", "signal_rgb", 100) in fake.calls
    assert sp.table.item(0, 4).text() != "—"
    assert "Generated" in sp.status.text()


def test_capture_toggle_and_import(win, fake, tmp_path):
    win.select_step(1)
    drain()
    cp = win.pages[1]
    cp.toggle.click()
    drain()
    assert fake.capture_enabled() is True and "ON" in cp.toggle.text()
    old = tmp_path / "old"
    old.mkdir()
    from PIL import Image
    Image.new("RGB", (40, 20)).save(old / "sig_115439_901_7080.png")
    Image.new("RGB", (40, 20)).save(old / "sig_090406_206_none.png")
    before = {c["id"] for c in fake.list_captures(limit=1000)}
    cp.import_folder(str(old))
    assert drain()
    assert "Imported 2" in cp.import_status.text()
    new = [c for c in fake.list_captures(limit=1000) if c["id"] not in before]
    assert len(new) == 2
    assert {c["proposed"] for c in new} == {"7080", None}
    assert all(c["status"] in ("proposed", "unlabeled") for c in new)   # proposals only


def test_export_preview_and_zip(win, fake, tmp_path):
    win.select_step(6)
    drain()
    ep = win.export_page
    assert ep.table.rowCount() == len(fake.export_preview()) > 0
    assert ep.zip_btn.isEnabled()
    ep.create_zip(str(tmp_path / "out"))
    assert drain()
    assert ep.zip_path and Path(ep.zip_path).is_file()
    assert not ep.result.isHidden()
    assert "Send this file to J" in ep.result.label.text()
    with zipfile.ZipFile(ep.zip_path) as z:
        assert "labels.jsonl" in z.namelist() and "settings.json" in z.namelist()


# ── lifecycle ────────────────────────────────────────────────────────────

def test_open_dev_mode_is_single_instance(fake):
    h = BackendHandle(fake, "test")
    a = open_dev_mode(handle=h)
    b = open_dev_mode(handle=h)
    assert a is b
    drain()
    a.close()
    drain()
    QApplication.sendPostedEvents(None, 0)            # run the deferred delete
    _APP.processEvents()
    assert window_mod._INSTANCE is None
    c = open_dev_mode(handle=h)
    assert c is not a
    c.close()
    drain()
    QApplication.sendPostedEvents(None, 0)


def test_close_while_busy_hides_instead_of_killing_work(tmp_path):
    slow = FakeBackend(root=str(tmp_path), delay=0.02, torch_installed=False)
    w = DevModeWindow(handle=BackendHandle(slow, "test"))
    w.show()
    drain()
    w.engine_page.install()
    assert w.busy
    w.close()
    assert shiboken6.isValid(w) and not w.isVisible()
    assert drain(15)
    assert slow.torch_status()["installed"]
    w.close()                                          # idle now: really closes
    drain()


def test_close_does_not_quit_host_app(win, monkeypatch):
    monkeypatch.setenv("SC_TOOLBOX_EXIT_ON_CLOSE", "1")
    quit_called = []
    monkeypatch.setattr(_APP, "quit", lambda: quit_called.append(True))
    win.title_bar._on_close_btn()
    drain()
    assert not quit_called
