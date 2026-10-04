"""Blueprints must be current on every launch AND every show of the window.

Every test here runs against a fake scmdb.net: the real HTTP client is
replaced by one that refuses to connect, and all cache files are redirected
to a temp dir, so nothing touches the network or the player's real cache.
"""

import json
import os
import sys
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(_SKILL_DIR, "..", "..")))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(_SKILL_DIR)

from shared.errors import Result  # noqa: E402

_OWNED_PREFIXES = ("config", "data", "ui", "services", "utils")


def _is_skill_module(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in _OWNED_PREFIXES)


class FakeScmdb:
    """In-memory stand-in for scmdb.net's static JSON files."""

    def __init__(self, live: str = "4.8.3-live.1"):
        self.online = True
        self.crafting_online = True
        self.live = live
        self.calls: dict[str, int] = {}

    def _hit(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    # api.fetch_versions
    def fetch_versions(self):
        self._hit("versions")
        if not self.online:
            return []
        return [{"version": "4.9.0-ptu.9", "file": "merged-4.9.0-ptu.9.json"},
                {"version": self.live, "file": f"merged-{self.live}.json"}]

    def fetch_game_data(self, file_name):
        self._hit("game_data")
        if not self.online:
            return None
        ver = file_name[len("merged-"):-len(".json")]
        return {"contracts": [{"id": ver, "title": f"Contract {ver}"}]}

    def _bp(self, ver):
        return {"blueprints": [{"tag": f"BP_{ver}", "type": "weapon",
                                "productEntityClass": f"ec_{ver}",
                                "productName": f"Gun {ver}", "tiers": []}],
                "resources": [], "items": [], "properties": {},
                "dismantle": {}, "meta": {}}

    def _items(self, ver):
        return {"items": [{"entityClass": f"ec_{ver}", "name": f"Gun {ver}"}],
                "manufacturers": {}}

    def _up(self):
        return self.online and self.crafting_online

    # original API surface
    def fetch_crafting_blueprints(self, ver):
        self._hit("crafting")
        return self._bp(ver) if self._up() else None

    def fetch_crafting_items(self, ver):
        self._hit("crafting_items")
        return self._items(ver) if self._up() else None

    # result-returning API surface
    def fetch_crafting_blueprints_result(self, ver):
        self._hit("crafting")
        if self._up():
            return Result.success(self._bp(ver))
        return Result.failure("Network error: offline", "network")

    def fetch_crafting_items_result(self, ver):
        self._hit("crafting_items")
        if self._up():
            return Result.success(self._items(ver))
        return Result.failure("Network error: offline", "network")


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh skill modules, a fake scmdb.net, and a temp cache dir."""
    saved = dict(sys.modules)
    for name in list(sys.modules):
        if _is_skill_module(name):
            del sys.modules[name]

    from data import api, cache
    import data.manager as manager

    fake = FakeScmdb()

    def _blocked(endpoint):
        raise AssertionError(f"test tried to reach the network: {endpoint}")

    monkeypatch.setattr(api._client, "get_json", _blocked)
    for fn in ("fetch_versions", "fetch_game_data", "fetch_crafting_blueprints",
               "fetch_crafting_items", "fetch_crafting_blueprints_result",
               "fetch_crafting_items_result"):
        monkeypatch.setattr(api, fn, getattr(fake, fn), raising=False)
    monkeypatch.setattr(cache, "_CACHE_DIR", str(tmp_path))

    class Env:
        pass

    e = Env()
    e.fake, e.api, e.cache, e.manager, e.tmp = fake, api, cache, manager, tmp_path
    yield e

    for name in list(sys.modules):
        if _is_skill_module(name):
            del sys.modules[name]
    sys.modules.update({k: v for k, v in saved.items() if _is_skill_module(k)})


def _wait(pred, timeout=10.0, app=None):
    end = time.time() + timeout
    while time.time() < end:
        if app is not None:
            app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    if app is not None:
        app.processEvents()
    return pred()


def _write_cache(path, payload, age_s):
    payload = dict(payload)
    payload["_cache_version"] = 1
    payload["version"] = 1
    payload["_ts"] = payload["timestamp"] = time.time() - age_s
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _merged(ver):
    return {"contracts": [{"id": ver, "title": f"Contract {ver}"}],
            "_scmdb_version": ver,
            "_versions": [{"version": ver, "file": f"merged-{ver}.json"}]}


# ── manager level ────────────────────────────────────────────────────────────

def test_offline_launch_serves_expired_cache_and_says_how_old(env):
    """Offline + only an expired cache: show it, flagged, never an empty window."""
    env.fake.online = False
    _write_cache(env.cache.default_cache_path(), _merged("4.8.0-live.7"),
                 age_s=30 * 86400)

    mgr = env.manager.MissionDataManager()
    done = []
    mgr.load(on_done=lambda: done.append(1))
    assert _wait(lambda: done)

    assert mgr.is_data_loaded(), mgr.error
    assert mgr.version == "4.8.0-live.7"
    assert len(mgr.contracts) == 1
    assert mgr.data_source == "stale"
    assert "as of" in (mgr.notice or "")


def test_failed_launch_does_not_wedge_the_loader(env):
    """A launch with no network and no cache must leave load() retryable."""
    env.fake.online = False
    mgr = env.manager.MissionDataManager()
    done = []
    mgr.load(on_done=lambda: done.append(1))
    assert _wait(lambda: done)
    assert not mgr.is_data_loaded()
    assert not mgr.is_data_loading(), "loading flag stuck: every retry is ignored"

    env.fake.online = True
    mgr.load(on_done=lambda: done.append(2))
    assert _wait(lambda: len(done) == 2)
    assert mgr.is_data_loaded() and mgr.version == env.fake.live


def test_crafting_blueprints_are_cached_per_version_and_survive_offline(env):
    """Blueprints persist: a relaunch reuses them without refetching, and an
    offline relaunch after they expire still shows them, flagged stale."""
    mgr = env.manager.MissionDataManager()
    done = []
    mgr.load(on_done=lambda: done.append("d"))
    assert _wait(lambda: done)
    mgr.load_crafting(on_done=lambda: done.append("c"))
    assert _wait(lambda: "c" in done)
    assert [b["tag"] for b in mgr.crafting_blueprints] == [f"BP_{env.fake.live}"]
    assert env.fake.calls["crafting"] == 1

    # Relaunch within the TTL: served from disk, no crafting request.
    mgr2 = env.manager.MissionDataManager()
    done2 = []
    mgr2.load(on_done=lambda: done2.append("d"))
    assert _wait(lambda: done2)
    mgr2.load_crafting(on_done=lambda: done2.append("c"))
    assert _wait(lambda: "c" in done2)
    assert mgr2.crafting_loaded and len(mgr2.crafting_blueprints) == 1
    assert env.fake.calls["crafting"] == 1

    # Relaunch offline after everything expired: stale but shown, with its time.
    for name in os.listdir(env.tmp):
        path = os.path.join(env.tmp, name)
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        _write_cache(path, payload, age_s=10 * 86400)
    env.fake.online = False
    mgr3 = env.manager.MissionDataManager()
    done3 = []
    mgr3.load(on_done=lambda: done3.append("d"))
    assert _wait(lambda: done3)
    mgr3.load_crafting(on_done=lambda: done3.append("c"))
    assert _wait(lambda: "c" in done3)
    assert mgr3.crafting_loaded and len(mgr3.crafting_blueprints) == 1
    assert mgr3.crafting_stale is True
    assert mgr3.crafting_as_of > 0


# ── window level (the launcher's show/hide path) ─────────────────────────────

@pytest.fixture
def window_env(env, monkeypatch):
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    import ui.app as ui_app

    class _NoInventory:
        """Keeps the test away from ~/.sctoolbox."""
        def __getattr__(self, name):
            return lambda *a, **k: None

    class _NoSync:
        """Keeps the test away from the real game logs and scan state."""
        def __init__(self, *a, **k):
            pass

        def __getattr__(self, name):
            return lambda *a, **k: None

    monkeypatch.setattr(ui_app, "InventoryService", _NoInventory)
    monkeypatch.setattr(ui_app, "OwnedBlueprintSync", _NoSync)
    env.app, env.ui_app = app, ui_app
    env.windows = []

    def make():
        w = ui_app.MissionDBApp(0, 0, 1000, 700, 1.0, os.devnull)
        env.windows.append(w)
        return w

    env.make = make
    yield env
    from PySide6.QtCore import QEvent
    for w in env.windows:
        _wait(lambda: _settled(w) or not w._data.is_data_loading(), app=app, timeout=5)
        w.close()
        w.deleteLater()
    app.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


def _settled(w):
    d = w._data
    return (d.is_data_loaded() and not d.is_data_loading()
            and not d.crafting_loading and not getattr(d, "_check_inflight", False))


def test_show_of_running_window_picks_up_a_new_patch(window_env):
    """The launcher hides/shows one long-lived process; each show must notice
    a new scmdb.net version and replace the blueprints, politely."""
    e = window_env
    w = e.make()
    assert _wait(lambda: _settled(w), app=e.app)
    w._switch_page("fabricator")
    assert _wait(lambda: _settled(w) and w._data.crafting_loaded, app=e.app)
    assert w._data.crafting_blueprints[0]["tag"] == "BP_4.8.3-live.1"

    w._dispatch({"type": "hide"})
    e.fake.live = "4.8.4-live.2"          # a patch lands while hidden
    w._data._last_check = 0.0             # the throttle window has passed
    before = e.fake.calls.get("versions", 0)

    w._dispatch({"type": "show"})
    assert _wait(lambda: w._data.version == "4.8.4-live.2" and _settled(w)
                 and w._data.crafting_blueprints
                 and w._data.crafting_blueprints[0]["tag"] == "BP_4.8.4-live.2",
                 app=e.app), (w._data.version, w._status_label.text())
    assert w._version_label.text() == "4.8.4-live.2"

    # Politeness: rapid re-shows inside the interval do not re-ask scmdb.net.
    for _ in range(3):
        w._dispatch({"type": "hide"})
        w._dispatch({"type": "show"})
    assert _wait(lambda: _settled(w), app=e.app)
    assert e.fake.calls.get("versions", 0) - before == 1


def test_launch_from_fresh_cache_still_detects_a_new_patch(window_env):
    """A cache inside its TTL must not hide a patch published since."""
    e = window_env
    _write_cache(e.cache.default_cache_path(), _merged("4.8.2-live.0"), age_s=60)
    w = e.make()
    assert _wait(lambda: w._data.version == e.fake.live and _settled(w),
                 app=e.app), w._data.version
    assert w._data.contracts[0]["id"] == e.fake.live


def test_crafting_network_failure_is_not_reported_as_no_data(window_env):
    """Unreachable crafting files must not be read as 'LIVE has no blueprints'
    (which auto-switched the whole window to PTU)."""
    e = window_env
    e.fake.crafting_online = False
    w = e.make()
    assert _wait(lambda: _settled(w), app=e.app)
    w._switch_page("fabricator")
    assert _wait(lambda: "ptu" in w._data.version.lower()
                 or "scmdb.net" in w._status_label.text(), app=e.app)
    assert _wait(lambda: _settled(w), app=e.app)
    assert w._active_channel == "live"
    assert "ptu" not in w._data.version.lower()
    assert "scmdb.net" in w._status_label.text()


def test_refresh_while_offline_keeps_the_data_and_the_cache(window_env):
    e = window_env
    w = e.make()
    assert _wait(lambda: _settled(w), app=e.app)
    e.fake.online = False
    w._dispatch({"type": "refresh"})
    assert _wait(lambda: _settled(w), app=e.app)
    assert os.path.exists(e.cache.default_cache_path())
    assert w._data.is_data_loaded() and w._data.contracts
    assert not w._status_label.text().startswith("Error")


def test_owned_blueprints_fill_in_without_any_click(env, monkeypatch, tmp_path):
    """Opening the window is enough, whichever tab is opened first.

    The scan used to live inside the Owned Blueprints page and to start only
    from the "crafting data loaded" callback.  Open Fabricator first and that
    callback had already fired by the time the Owned page existed, so nothing
    was scanned until "Scan Game Log" was clicked.
    """
    from PySide6.QtWidgets import QApplication, QMessageBox
    from PySide6.QtCore import QEvent
    app = QApplication.instance() or QApplication([])
    import ui.app as ui_app
    from services import inventory, sc_log_scanner
    from ui import owned_sync

    ver = env.fake.live
    live_dir = tmp_path / "StarCitizen" / "LIVE"
    live_dir.mkdir(parents=True)
    line = ('<2026-10-04T18:29:21.297Z> [Notice] <SHUDEvent_OnNotification> Added '
            'notification "Received Blueprint: Gun ' + ver + ': " [79] to queue. New queue '
            'size: 3, MissionId: [00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
            '[Team_CoreGameplayFeatures][Missions][Comms]\r\n')
    (live_dir / "Game.log").write_bytes(
        b"<2026-10-04T16:36:49.990Z> Log started on Sun Oct  4 16:36:49 2026\r\n"
        + line.encode())

    dialogs = []
    monkeypatch.setattr(QMessageBox, "exec", lambda *a, **k: dialogs.append(a))
    inv_path = str(tmp_path / "inventory.json")
    monkeypatch.setattr(ui_app, "InventoryService",
                        lambda: inventory.InventoryService(path=inv_path))

    def make_sync(parent, data, inv, on_changed=None):
        scanner = sc_log_scanner.IncrementalLogScanner(
            state_path=str(tmp_path / "state.json"),
            folder_resolver=lambda: str(live_dir), backlog_pause=0)
        # One poll at start and no second one inside this test: what is
        # asserted below can only come from the window's own wiring.
        return owned_sync.OwnedBlueprintSync(
            parent, data, inv, on_changed=on_changed, scanner=scanner,
            interval_ms=600_000)

    monkeypatch.setattr(ui_app, "OwnedBlueprintSync", make_sync)

    w = ui_app.MissionDBApp(0, 0, 1000, 700, 1.0, os.devnull)
    try:
        assert _wait(lambda: _settled(w), app=app)
        # The logs have been read although no blueprint tab was ever opened...
        assert _wait(lambda: w._owned_sync._scanner.names() == {"Gun " + ver}, app=app)
        assert w._inventory.owned_count() == 0      # ...no blueprint data yet

        w._switch_page("fabricator")                # the order that used to break it
        assert _wait(lambda: w._data.crafting_loaded
                     and w._inventory.owned_count() == 1, app=app, timeout=5)

        w._switch_page("owned")
        page = w._owned_page
        assert len(page._grid_items) == 1
        assert page._grid_items[0]["tag"] == "BP_" + ver
        assert "found in logs" in page.status_text()
        assert not dialogs
    finally:
        _wait(lambda: _settled(w) or not w._data.is_data_loading(), app=app, timeout=5)
        w.close()
        stopped = (w._owned_sync._stop.is_set()
                   and not w._owned_sync._timer.isActive())
        w.deleteLater()
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()
    assert stopped, "closing the window stops the polling"
