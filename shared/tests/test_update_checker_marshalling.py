"""Issue #14 — the update button hangs.

Two independent defects, one test file:

1. MARSHALLING.  ``check_for_updates_async`` invokes its callback on a plain
   ``threading.Thread``.  ``LauncherWindow._on_update_result`` used to hand the
   result to the GUI with ``QTimer.singleShot(0, ...)``, but a QTimer created in
   a thread with no Qt event dispatcher never fires, so ``_show_update_result``
   was unreachable on EVERY path — the manual check, the startup check, and the
   ``* NEW`` badge alike.  The fix routes through the window's ``_cb_queue``,
   drained by a window-owned QTimer on the GUI thread.

   ``test_singleshot_from_plain_thread_never_fires`` pins the underlying Qt
   behaviour so this cannot be "fixed" back to a singleShot, and carries a
   main-thread control so a dead event loop cannot make it pass vacuously.

2. ERROR REPORTING.  ``UpdateResult.error`` was assigned nowhere, so an offline
   check fell through to the no-release-info branch and was reported to the user
   as a green "Up to date" — a confident lie about a check that never completed.
"""
from __future__ import annotations

import threading
import urllib.error

import pytest

from PySide6.QtCore import QObject, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from shared import update_checker
from shared.update_checker import UpdateResult, check_for_updates


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _pump(app, ms: int) -> None:
    """Run a real event loop for *ms* milliseconds."""
    done = []
    QTimer.singleShot(ms, lambda: done.append(True))
    while not done:
        app.processEvents()


# ─────────────────────────────────────────────────────────────────────────────
# 1. Marshalling
# ─────────────────────────────────────────────────────────────────────────────


def test_singleshot_from_plain_thread_never_fires(qapp):
    """The defect itself: QTimer.singleShot cannot marshal from a plain thread.

    This is the regression guard. If a future change replaces the queue with a
    singleShot again, the mechanism is still broken and this test says so.
    """
    from_thread: list[bool] = []
    from_main: list[bool] = []

    def worker():
        QTimer.singleShot(0, lambda: from_thread.append(True))

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join()

    # Control: the identical call from the GUI thread MUST fire, otherwise a
    # dead event loop would make the assertion below pass for the wrong reason.
    QTimer.singleShot(0, lambda: from_main.append(True))

    _pump(qapp, 600)

    assert from_main == [True], "control failed: no live event loop, test is vacuous"
    assert from_thread == [], "expected a cross-thread singleShot to be lost"


class _FakeWindow(QObject):
    """The marshalling half of LauncherWindow, wired exactly as it now is.

    Constructing a real LauncherWindow needs the full skill/config/theme stack;
    what issue #14 is about is the hand-off, so this exercises the hand-off with
    the production code paths copied verbatim from ui.main_window.
    """

    def __init__(self):
        super().__init__()
        import queue as _queue

        self._q = _queue.Queue()
        self._queue_mod = _queue
        self.handled: list[tuple[UpdateResult, bool, int]] = []
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    def _drain(self):
        while True:
            try:
                fn = self._q.get_nowait()
            except self._queue_mod.Empty:
                break
            fn()

    # mirrors LauncherWindow._on_update_result
    def on_update_result(self, result: UpdateResult) -> None:
        self._q.put(lambda: self._show(result, silent=False))

    def _show(self, result: UpdateResult, silent: bool) -> None:
        self.handled.append((result, silent, threading.get_ident()))


def test_callback_reaches_gui_thread_via_queue(qapp):
    """The fix: a result produced on a worker thread is handled on the GUI thread."""
    win = _FakeWindow()
    gui_thread_id = threading.get_ident()

    result = UpdateResult(
        available=True, latest_version="9.9.9", current_version="1.0.0",
        release_url="http://example.invalid/releases",
    )

    # Deliver the callback the way check_for_updates_async does: from a plain
    # threading.Thread with no Qt event dispatcher.
    t = threading.Thread(target=lambda: win.on_update_result(result), daemon=True)
    t.start()
    t.join()

    assert win.handled == [], "should not have run before the event loop pumped"

    _pump(qapp, 400)

    assert len(win.handled) == 1, "GUI handler never ran — the update button still hangs"
    got, silent, ran_on = win.handled[0]
    assert got is result
    assert silent is False
    assert ran_on == gui_thread_id, "handler ran off the GUI thread"


def test_async_callback_delivered_on_a_worker_thread(qapp, monkeypatch):
    """Confirms the premise: check_for_updates_async really does call back off-thread."""
    monkeypatch.setattr(
        update_checker, "check_for_updates",
        lambda: UpdateResult(False, "1.0.0", "1.0.0", "http://example.invalid"),
    )
    seen: list[int] = []
    done = threading.Event()

    def cb(res):
        seen.append(threading.get_ident())
        done.set()

    update_checker.check_for_updates_async(cb)
    assert done.wait(5), "async check never called back"
    assert seen[0] != threading.get_ident(), "callback was not off-thread"


def test_badge_and_bubble_path_is_reachable(qapp):
    """The `* NEW` badge lives behind the same hand-off, so prove it now lands."""
    win = _FakeWindow()
    # Hold the parent in a local: a temporary QWidget is collected immediately
    # and Qt deletes its children with it.
    holder = QWidget()
    label = QLabel(holder)
    label.setVisible(False)

    def _apply(result: UpdateResult, silent: bool):
        if result.available:
            label.setText(f"NEW v{result.latest_version}")
            label.setVisible(True)

    win._show = _apply  # type: ignore[method-assign]

    result = UpdateResult(True, "2.5.0", "1.0.0", "http://example.invalid")
    t = threading.Thread(target=lambda: win.on_update_result(result), daemon=True)
    t.start()
    t.join()
    _pump(qapp, 400)

    assert label.text() == "NEW v2.5.0", "the NEW badge never got set"


def test_production_handlers_enqueue_rather_than_singleshot(qapp):
    """Guard the REAL ui.main_window methods, not a copy of them.

    The tests above prove the mechanism; this one proves production USES it.
    Calling the unbound methods against a stub exercises the shipped bodies
    without building the whole launcher, so switching either of them back to
    QTimer.singleShot leaves the queue empty and fails here.
    """
    import queue as _queue

    from ui.main_window import LauncherWindow

    class _Stub:
        def __init__(self):
            self._cb_queue = _queue.Queue()
            self.shown: list[tuple[UpdateResult, bool]] = []

        def _show_update_result(self, result, silent):
            self.shown.append((result, silent))

    result = UpdateResult(True, "3.0.0", "1.0.0", "http://example.invalid")

    for method, expect_silent in (
        (LauncherWindow._on_update_result, False),
        (LauncherWindow._on_startup_update_result, True),
    ):
        stub = _Stub()
        method(stub, result)
        assert not stub._cb_queue.empty(), (
            f"{method.__name__} did not enqueue — it is not marshalling to the GUI thread"
        )
        # Draining is what the GUI-thread timer does.
        stub._cb_queue.get_nowait()()
        assert stub.shown == [(result, expect_silent)]


def test_show_update_result_never_shows_up_to_date_on_error(qapp):
    """A failed check must not paint a green 'Up to date' (the worse bug)."""
    from ui.main_window import LauncherWindow

    class _Stub:
        def __init__(self):
            self.statuses: list[tuple[str, object]] = []
            self._status_label = QLabel()
            self._update_bubble = None
            self._last_update_result = None

        def set_status(self, text, color=None):
            self.statuses.append((text, color))

    failed = UpdateResult(
        available=False, latest_version="1.0.0", current_version="1.0.0",
        release_url="http://example.invalid", error="Could not reach GitHub: offline",
    )

    stub = _Stub()
    LauncherWindow._show_update_result(stub, failed, silent=False)
    joined = " ".join(t for t, _c in stub.statuses)
    assert "Up to date" not in joined, f"reported success for a failed check: {joined}"
    assert "failed" in joined.lower()

    # Silent (startup) check: says nothing at all.
    quiet = _Stub()
    LauncherWindow._show_update_result(quiet, failed, silent=True)
    assert quiet.statuses == []


# ─────────────────────────────────────────────────────────────────────────────
# 2. Error reporting — an offline check must not read as "Up to date"
# ─────────────────────────────────────────────────────────────────────────────


def _offline(*_a, **_k):
    raise urllib.error.URLError("getaddrinfo failed")


def test_offline_check_reports_an_error(monkeypatch):
    """Both endpoints unreachable -> error set, so the UI cannot claim success."""
    monkeypatch.setattr(update_checker.urllib.request, "urlopen", _offline)
    res = check_for_updates()

    assert res.available is False
    assert res.error, "offline check reported no error — the UI will show 'Up to date'"
    assert "GitHub" in res.error or "reach" in res.error


def test_rate_limited_check_reports_an_error(monkeypatch):
    """A 403 is a real failure, not an absence of releases."""
    def _forbidden(*_a, **_k):
        raise urllib.error.HTTPError("u", 403, "rate limited", {}, None)

    monkeypatch.setattr(update_checker.urllib.request, "urlopen", _forbidden)
    res = check_for_updates()
    assert res.error and "403" in res.error


def test_404_is_not_an_error(monkeypatch):
    """A repo with no releases and no tags is genuinely 'up to date', not broken."""
    def _missing(*_a, **_k):
        raise urllib.error.HTTPError("u", 404, "not found", {}, None)

    monkeypatch.setattr(update_checker.urllib.request, "urlopen", _missing)
    res = check_for_updates()
    assert res.error == "", f"404 should not be an error, got {res.error!r}"
    assert res.available is False


def test_successful_check_has_no_error(monkeypatch):
    """A working check must leave error empty, or every check looks broken."""
    payload = {
        "tag_name": "v9.9.9",
        "html_url": "http://example.invalid/r/9.9.9",
        "assets": [{"name": "SC_Toolbox_Setup_9.9.9.exe",
                    "browser_download_url": "http://example.invalid/dl.exe"}],
    }
    monkeypatch.setattr(update_checker, "_github_get", lambda url: (payload, ""))
    res = check_for_updates()
    assert res.error == ""
    assert res.latest_version == "9.9.9"
    assert res.available is True
