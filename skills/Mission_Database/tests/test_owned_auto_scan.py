"""Owned blueprints must appear by themselves.

What the player asked for: "I had to do a bunch of steps to have them
populate and it would be nice to have them pop in without needing to do all
of that."

Every log here is a small synthetic file built from line shapes copied out of
real Game.log files (CRLF line ends, the three ways one blueprint is logged,
names with quotes in them, a name with a space before its colon).  Nothing in
this file reads the player's real logs or writes under ~/.sctoolbox, except
the one opt-in real-data check at the bottom, which only reads.
"""

import json
import os
import shutil
import sys
import threading
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.normpath(os.path.join(_SKILL_DIR, "..", "..")))
import shared.path_setup  # noqa: E402
shared.path_setup.ensure_path(_SKILL_DIR)

# ── real line shapes ─────────────────────────────────────────────────────────

HEADER = (
    '<2026-10-04T16:36:49.{ms}Z> BackupNameAttachment=" Build(12660092) 04 Oct 26 '
    '(12 36 37)"  -- used by backup system\r\n'
    "<2026-10-04T16:36:49.990Z> Log started on Sun Oct  4 16:36:49 2026\r\n"
    "<2026-10-04T16:36:49.990Z> Built on Sep 15 2026 13:12:47\r\n")
NOISE = ("<2026-10-04T18:29:20.101Z> [Notice] <CEntityComponentPhysicalProxy> "
         "Some unrelated line about a ship [Team_Physics]\r\n")


def bp_lines(name: str, n: int = 79) -> str:
    """The three lines the game writes when it hands out one blueprint."""
    return (
        f'<2026-10-04T18:29:21.297Z> [Notice] <SHUDEvent_OnNotification> Added notification '
        f'"Received Blueprint: {name}: " [{n}] to queue. New queue size: 3, MissionId: '
        f'[00000000-0000-0000-0000-000000000000], ObjectiveId: [] '
        f'[Team_CoreGameplayFeatures][Missions][Comms]\r\n'
        f'<2026-10-04T18:29:21.297Z>    "Received Blueprint: {name}: " [{n}]\r\n'
        f'<2026-07-09T00:37:55.926Z> [Notice] <UpdateNotificationItem> Notification '
        f'"Received Blueprint: {name}: " [{n}], Action: Next '
        f'[Team_CoreGameplayFeatures][Missions][Comms]\r\n')


def bp(tag: str, name: str) -> dict:
    return {"tag": tag, "productEntityClass": f"ec-{tag}", "productName": name,
            "type": "weapons", "tiers": []}


# Names exactly as they appear in real logs.
POD = "GOLEM MC-4 Ore Pod "            # the game logs this one with a space before ":"
SHOTGUN = 'BR-2 "Purgatory Camo" Shotgun'
HELMET = "Monde Helmet"
FR66 = "FR-66"
RIFLE = 'Zenith "Darkwave" Laser Sniper Rifle'

BLUEPRINTS = [
    bp("BP_pod", "GOLEM MC-4 Ore Pod"),
    bp("BP_shotgun", SHOTGUN),
    bp("BP_helmet", HELMET),
    bp("BP_fr66", FR66),
    bp("BP_rifle", RIFLE),
    bp("BP_never_received", "Bolide"),
]


class FakeData:
    """The two things the matcher needs from MissionDataManager."""

    def __init__(self, loaded=True):
        self.crafting_loaded = loaded
        self.crafting_blueprints = list(BLUEPRINTS)

    def get_blueprint_product(self, b):
        return None

    def get_blueprint_product_name(self, b):
        return b.get("productName") or b.get("tag") or "?"


@pytest.fixture
def mods(tmp_path, monkeypatch):
    """The skill's modules, with every default path pointed into tmp_path."""
    from services import inventory, sc_log_scanner

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(sc_log_scanner, "_STATE_PATH", str(home / "log_scan_state.json"))
    monkeypatch.setattr(sc_log_scanner, "_SETTINGS_PATH", str(home / "settings.json"))
    monkeypatch.setattr(sc_log_scanner, "_OLD_SETTINGS_PATH", str(home / "old_settings.json"))
    monkeypatch.setattr(inventory, "_NEW_INV_PATH", str(home / "inventory.json"))
    monkeypatch.setattr(inventory, "_OLD_INV_PATH", str(home / "legacy_inventory.json"))

    class M:
        pass

    m = M()
    m.inv, m.sls, m.home = inventory, sc_log_scanner, home
    return m


class Game:
    """A pretend Star Citizen channel folder."""

    def __init__(self, root):
        self.folder = root / "LIVE"
        self.backups = self.folder / "logbackups"
        self.backups.mkdir(parents=True)
        self.live = self.folder / "Game.log"
        self._session = 0

    def start_session(self, body: str = "") -> None:
        self._session += 1
        self.live.write_bytes(
            (HEADER.format(ms=f"{self._session:03d}") + body).encode("utf-8"))

    def append(self, text: str) -> None:
        with open(self.live, "ab") as f:
            f.write(text.encode("utf-8"))

    def backup(self, name: str, body: str) -> None:
        (self.backups / name).write_bytes(
            (HEADER.format(ms="777") + body).encode("utf-8"))

    def rotate(self, new_body: str = "") -> str:
        """What the game does at launch: move Game.log away, start a new one."""
        name = f"Game Build(12660092) 04 Oct 26 (12 36 {self._session:02d}).log"
        shutil.move(str(self.live), str(self.backups / name))
        self.start_session(new_body)
        return name


@pytest.fixture
def game(tmp_path):
    return Game(tmp_path)


def scanner_for(mods, game, **kw):
    kw.setdefault("backlog_pause", 0)
    s = mods.sls.IncrementalLogScanner(
        state_path=str(mods.home / "state.json"),
        folder_resolver=lambda: str(game.folder), **kw)
    s.RELIST_S = 0          # these tests want every poll to look at logbackups
    return s


# ── reading one file ─────────────────────────────────────────────────────────

def test_all_three_real_line_shapes_give_one_clean_name(mods, tmp_path):
    p = tmp_path / "one.log"
    p.write_bytes((NOISE + bp_lines(POD) + bp_lines(SHOTGUN, 90) + NOISE).encode())
    names, end, read, finished = mods.sls.read_blueprint_names(str(p))
    assert names == {"GOLEM MC-4 Ore Pod", SHOTGUN}
    assert end == read == p.stat().st_size and finished


def test_a_name_split_across_two_read_chunks_is_still_found(mods, tmp_path, monkeypatch):
    p = tmp_path / "split.log"
    p.write_bytes((NOISE * 3 + bp_lines(RIFLE) + NOISE).encode())
    monkeypatch.setattr(mods.sls, "_CHUNK", 37)     # every line straddles a chunk
    assert mods.sls.read_blueprint_names(str(p))[0] == {RIFLE}


# ── the live log ─────────────────────────────────────────────────────────────

def test_blueprint_appended_while_the_tool_is_open_is_found_on_the_next_poll(mods, game):
    game.start_session(NOISE * 50 + bp_lines(HELMET))
    s = scanner_for(mods, game)
    first = s.poll()
    assert first.new_names == [HELMET]
    assert first.bytes_read == game.live.stat().st_size

    quiet = s.poll()
    assert quiet.new_names == [] and quiet.bytes_read == 0, \
        "nothing was written, so nothing may be read"

    added = NOISE + bp_lines(FR66)
    game.append(added)
    nxt = s.poll()
    assert nxt.new_names == [FR66]
    assert nxt.bytes_read == len(added.encode()), \
        "only the appended part may be read, not the log from the start"
    assert s.names() == {HELMET, FR66}


def test_a_half_written_line_waits_for_its_line_end(mods, game):
    game.start_session(NOISE)
    s = scanner_for(mods, game)
    s.poll()
    whole = bp_lines(FR66).split("\r\n")[0] + "\r\n"     # one line, one mention
    cut = whole.index("FR-66") + 3                         # ...Received Blueprint: FR-
    game.append(whole[:cut])
    assert s.poll().new_names == []
    game.append(whole[cut:])
    assert s.poll().new_names == [FR66], \
        "the line was skipped: the offset moved past its first half"
    assert s.poll().new_names == []


def test_live_offset_survives_a_restart_of_the_tool(mods, game):
    game.start_session(NOISE * 20 + bp_lines(HELMET))
    s = scanner_for(mods, game)
    s.poll()
    s.flush()
    game.append(bp_lines(FR66))

    again = scanner_for(mods, game)
    assert again.names() == {HELMET}, "names found earlier are remembered"
    res = again.poll()
    assert res.new_names == [FR66]
    assert res.bytes_read == len(bp_lines(FR66).encode())


# ── backups ──────────────────────────────────────────────────────────────────

def test_backups_are_read_once_and_a_new_one_is_picked_up(mods, game):
    game.start_session(NOISE)
    game.backup("Game Build(1) 01 Aug 26 (10 00 00).log", bp_lines(HELMET))
    game.backup("Game Build(1) 02 Aug 26 (10 00 00).log", NOISE)
    game.backup("Game Build(1) 03 Aug 26 (10 00 00).log", bp_lines(SHOTGUN))
    (game.backups / "notes.txt").write_text("not a log")

    s = scanner_for(mods, game)
    first = s.poll()
    assert first.backups_read == 3
    assert s.names() == {HELMET, SHOTGUN}

    second = s.poll()
    assert second.listed_backups and second.backups_read == 0, \
        "unchanged backups must not be read again"
    assert second.bytes_read == 0

    # ... nor by the next launch of the tool
    relaunch = scanner_for(mods, game)
    assert relaunch.poll().backups_read == 0

    game.backup("Game Build(1) 04 Aug 26 (10 00 00).log", bp_lines(FR66))
    third = s.poll()
    assert third.backups_read == 1 and third.new_names == [FR66]


def test_a_backup_that_changed_is_read_again(mods, game):
    game.start_session(NOISE)
    name = "Game Build(1) 01 Aug 26 (10 00 00).log"
    game.backup(name, bp_lines(HELMET))
    s = scanner_for(mods, game)
    s.poll()
    game.backup(name, bp_lines(HELMET) + bp_lines(RIFLE))
    res = s.poll()
    assert res.backups_read == 1 and res.new_names == [RIFLE]


def test_backups_are_not_listed_on_every_poll(mods, game):
    game.start_session(NOISE)
    game.backup("Game Build(1) 01 Aug 26 (10 00 00).log", bp_lines(HELMET))
    s = scanner_for(mods, game)
    s.RELIST_S = 300
    assert s.poll().listed_backups
    game.append(NOISE)
    assert not s.poll().listed_backups, "a plain tail poll must not walk logbackups"


def test_an_interrupted_first_scan_resumes_instead_of_starting_over(mods, game):
    game.start_session(NOISE)
    for d in range(1, 7):
        game.backup(f"Game Build(1) 0{d} Aug 26 (10 00 00).log", bp_lines(f"Item {d}"))
    stop = threading.Event()
    s = scanner_for(mods, game)
    seen = []

    def progress(done, total):
        seen.append(done)

    # stop after the third backup
    orig = mods.sls.read_blueprint_names
    count = {"n": 0}

    def counting(path, **kw):
        if "logbackups" in path:
            count["n"] += 1
            if count["n"] == 3:
                stop.set()
        return orig(path, **kw)

    mods.sls.read_blueprint_names, saved = counting, orig
    try:
        first = s.poll(on_progress=progress, stop=stop)
    finally:
        mods.sls.read_blueprint_names = saved
    assert not first.backlog_done
    done_so_far = first.backups_read
    assert 0 < done_so_far < 6
    s.flush()

    resumed = scanner_for(mods, game)
    rest = resumed.poll()
    assert rest.backups_read == 6 - done_so_far
    assert resumed.names() == {f"Item {d}" for d in range(1, 7)}, \
        "the file that was interrupted must be read, not marked as done"


# ── rotation ─────────────────────────────────────────────────────────────────

def test_rotation_to_a_shorter_log_misses_nothing_and_counts_nothing_twice(mods, game):
    game.start_session(NOISE * 200 + bp_lines(HELMET))
    s = scanner_for(mods, game)
    s.poll()
    # Written after the last poll, then the game is restarted before the next:
    game.append(bp_lines(FR66))
    game.rotate(NOISE + bp_lines(SHOTGUN))
    assert game.live.stat().st_size < 200 * len(NOISE)

    res = s.poll()
    assert res.rotated
    assert sorted(res.new_names) == sorted([FR66, SHOTGUN]), \
        "the end of the old session (now in logbackups) and the new log"
    assert s.names() == {HELMET, FR66, SHOTGUN}

    quiet = s.poll()
    assert quiet.new_names == [] and quiet.bytes_read == 0 and not quiet.rotated


def test_rotation_makes_the_scanner_look_at_logbackups_at_once(mods, game):
    """Backups are normally re-listed only every few minutes.  A rotation must
    not wait for that: the old session has just become a backup."""
    game.start_session(NOISE * 20)
    s = scanner_for(mods, game)
    s.RELIST_S = 300
    s.poll()
    game.append(bp_lines(FR66))
    game.rotate(NOISE)
    res = s.poll()
    assert res.rotated and res.listed_backups and res.new_names == [FR66]


def test_rotation_to_a_longer_log_is_noticed_too(mods, game):
    """The new log can outgrow the old offset before the next poll, so
    'the file got smaller' is not enough to notice a rotation."""
    game.start_session(NOISE + bp_lines(HELMET))
    s = scanner_for(mods, game)
    s.poll()
    game.rotate(bp_lines(RIFLE) + NOISE * 300)
    res = s.poll()
    assert res.rotated
    assert res.new_names == [RIFLE], "it sits before the old offset in the new file"


def test_rotation_while_the_tool_was_closed(mods, game):
    game.start_session(NOISE * 10 + bp_lines(HELMET))
    s = scanner_for(mods, game)
    s.poll()
    s.flush()
    game.append(bp_lines(FR66))
    game.rotate(bp_lines(SHOTGUN) + NOISE * 200)

    res = scanner_for(mods, game).poll()
    assert res.rotated and sorted(res.new_names) == sorted([FR66, SHOTGUN])


# ── scans only add; the player's hand is authoritative ───────────────────────

def owned_names(inv):
    return sorted(b["productName"] for b in inv.get_all())


def test_a_scan_that_finds_less_removes_nothing_and_keeps_folders(mods, game):
    game.start_session(bp_lines(HELMET))
    old = "Game Build(1) 01 Aug 26 (10 00 00).log"
    game.backup(old, bp_lines(SHOTGUN) + bp_lines(FR66))
    inv = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    data = FakeData()
    s = scanner_for(mods, game)
    s.poll()
    first = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert len(first.added) == 3

    armour = inv.create_folder("/", "Armour")
    inv.move_blueprint("BP_helmet|ec-BP_helmet", armour)
    before = json.dumps(inv.get_folder_tree(), sort_keys=True)

    # The game deletes the old backup and truncates the live log.
    (game.backups / old).unlink()
    game.start_session(NOISE)
    for full in (False, True):
        s.poll(full=full)
        res = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
        assert res.added == []
    # ... and even a scanner that has forgotten everything it ever read:
    blank = mods.sls.IncrementalLogScanner(
        state_path=str(mods.home / "blank.json"),
        folder_resolver=lambda: str(game.folder), backlog_pause=0)
    blank.poll()
    assert blank.names() == set()
    mods.sls.apply_log_names(blank.names(), data.crafting_blueprints, inv, data)

    assert owned_names(inv) == sorted(["Monde Helmet", SHOTGUN, FR66])
    assert json.dumps(inv.get_folder_tree(), sort_keys=True) == before
    assert inv.get_blueprint_folder("BP_helmet|ec-BP_helmet") == armour

    # and all of it is on disk
    reloaded = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    assert owned_names(reloaded) == owned_names(inv)
    assert reloaded.get_blueprint_folder("BP_helmet|ec-BP_helmet") == armour


def test_a_blueprint_removed_by_hand_is_not_put_back_by_a_scan(mods, game):
    game.start_session(bp_lines(HELMET) + bp_lines(FR66))
    inv = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    data = FakeData()
    s = scanner_for(mods, game)
    s.poll()
    mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert owned_names(inv) == [FR66, HELMET]

    inv.remove("BP_fr66|ec-BP_fr66")                     # "Remove from owned"
    s.poll(full=True)
    res = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert res.added == [] and res.held_back == ["BP_fr66|ec-BP_fr66"]
    assert owned_names(inv) == [HELMET]

    # remembered across a restart of the tool
    inv2 = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    res = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv2, data)
    assert res.added == [] and owned_names(inv2) == [HELMET]

    # the player marks it owned again by hand: that wins too, and sticks
    inv2.add("BP_fr66|ec-BP_fr66", BLUEPRINTS[3])
    assert "BP_fr66|ec-BP_fr66" not in inv2.removed_ids()
    assert owned_names(inv2) == [FR66, HELMET]


def test_removed_blueprints_come_back_only_when_asked(mods, game):
    game.start_session(bp_lines(HELMET) + bp_lines(FR66))
    inv = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    data = FakeData()
    s = scanner_for(mods, game)
    s.poll()
    mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert inv.remove_all() == 2                          # "Clear All"
    assert mods.sls.apply_log_names(
        s.names(), data.crafting_blueprints, inv, data).added == []
    assert inv.owned_count() == 0
    res = mods.sls.apply_log_names(
        s.names(), data.crafting_blueprints, inv, data, restore_removed=True)
    assert len(res.added) == 2 and inv.removed_ids() == set()


def test_a_blueprint_marked_by_hand_is_untouched_by_scans(mods, game):
    game.start_session(bp_lines(HELMET))
    inv = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    data = FakeData()
    inv.add("BP_never_received|ec-BP_never_received", BLUEPRINTS[5])   # by hand
    s = scanner_for(mods, game)
    s.poll()
    mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert owned_names(inv) == ["Bolide", HELMET]


def test_a_name_missing_from_the_blueprint_list_is_reported_not_dropped(mods, game):
    game.start_session(bp_lines(HELMET) + bp_lines("Brand New Thing"))
    inv = mods.inv.InventoryService(path=str(mods.home / "inv.json"))
    data = FakeData()
    s = scanner_for(mods, game)
    s.poll()
    res = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert res.unmatched == ["Brand New Thing"]
    # A later patch adds it to the blueprint list: no log has to be read again.
    data.crafting_blueprints.append(bp("BP_new", "Brand New Thing"))
    quiet = s.poll()
    assert quiet.bytes_read == 0
    res = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    assert res.added == ["BP_new|ec-BP_new"] and res.unmatched == []


# ── where the inventory lives ────────────────────────────────────────────────

def test_an_explicit_inventory_path_never_touches_the_default_one(mods):
    """InventoryService(path=...) used to load the legacy inventory from the
    install folder and SAVE it over the default (the player's real) file."""
    default = mods.home / "inventory.json"
    default.write_text(json.dumps({"version": 2, "owned": {"mine": {"tag": "mine"}},
                                   "folders": {"/": {"name": "Root", "children": [],
                                                     "blueprints": ["mine"]}}}))
    (mods.home / "legacy_inventory.json").write_text(
        json.dumps({"owned": {"old": {"tag": "old"}}}))
    before = default.read_bytes()

    for name in ("missing.json", "copy.json"):
        target = mods.home / name
        if name == "copy.json":
            target.write_bytes(before)
        inv = mods.inv.InventoryService(path=str(target))
        inv.add("x", {"tag": "x"})
        assert target.exists()
    assert "old" not in mods.inv.InventoryService(path=str(mods.home / "missing.json")).owned_ids()
    assert default.read_bytes() == before


def test_an_unreadable_inventory_is_kept_not_replaced_by_the_legacy_one(mods):
    default = mods.home / "inventory.json"
    default.write_text('{"version": 2, "owned": {"mine": {"ta')      # cut off mid-write
    (mods.home / "legacy_inventory.json").write_text(
        json.dumps({"owned": {"old": {"tag": "old"}}}))
    inv = mods.inv.InventoryService()
    assert inv.owned_ids() == set(), "months-old legacy data must not be resurrected"
    kept = [p for p in mods.home.iterdir() if ".unreadable-" in p.name]
    assert len(kept) == 1 and kept[0].read_text().startswith('{"version": 2, "owned": {"mine"')


def test_legacy_inventory_is_still_migrated_when_there_is_none_yet(mods):
    (mods.home / "legacy_inventory.json").write_text(
        json.dumps({"owned": {"old": {"tag": "old"}}}))
    inv = mods.inv.InventoryService()
    assert inv.owned_ids() == {"old"}
    assert (mods.home / "inventory.json").exists()
    assert (mods.home / "legacy_inventory.json").exists(), "copied, never deleted"


def test_a_failed_save_leaves_the_previous_inventory_readable(mods, monkeypatch):
    path = mods.home / "inv.json"
    inv = mods.inv.InventoryService(path=str(path))
    inv.add("a", {"tag": "a"})
    good = path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(mods.inv.json, "dump", boom)
    with pytest.raises(OSError):
        inv.add("b", {"tag": "b"})
    assert path.read_bytes() == good


# ── no Star Citizen folder ───────────────────────────────────────────────────

def test_no_star_citizen_folder_is_not_an_error(mods):
    calls = []

    def nowhere():
        calls.append(1)
        return None

    s = mods.sls.IncrementalLogScanner(
        state_path=str(mods.home / "state.json"), folder_resolver=nowhere)
    for _ in range(5):
        res = s.poll()
        assert res.folder is None and res.new_names == []
    assert len(calls) == 1, "drives are not probed again on every poll"
    assert not (mods.home / "state.json").exists()


def test_a_folder_that_disappears_keeps_what_is_known(mods, game):
    game.start_session(bp_lines(HELMET))
    where = {"folder": str(game.folder)}
    s = mods.sls.IncrementalLogScanner(
        state_path=str(mods.home / "state.json"),
        folder_resolver=lambda: where["folder"], backlog_pause=0)
    s.poll()
    shutil.rmtree(game.folder)
    where["folder"] = None
    res = s.poll()
    assert res.folder is None
    assert s.names() == {HELMET}


# ── the Qt side: background thread, UI-thread updates, no dialogs ────────────

@pytest.fixture
def qt(mods, monkeypatch):
    from PySide6.QtWidgets import QApplication, QMessageBox, QFileDialog
    app = QApplication.instance() or QApplication([])
    from ui import owned_sync

    opened = []

    def no_dialog(*a, **k):
        opened.append(a)
        return None

    monkeypatch.setattr(QMessageBox, "exec", no_dialog)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", no_dialog)
    mods.app, mods.owned_sync = app, owned_sync
    yield mods
    assert not opened, "a dialog was opened; owned blueprints must need none"


def wait(app, cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


class RecordingInventory:
    """Wraps a real InventoryService and notes which thread writes to it."""

    def __init__(self, inner):
        self._inner = inner
        self.write_threads = []

    def add_from_scan(self, *a, **k):
        self.write_threads.append(threading.current_thread())
        return self._inner.add_from_scan(*a, **k)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def make_sync(qt, game, data, interval_ms=30, resolver=None):
    inv = RecordingInventory(qt.inv.InventoryService(path=str(qt.home / "inv.json")))
    scanner = qt.sls.IncrementalLogScanner(
        state_path=str(qt.home / "state.json"),
        folder_resolver=resolver or (lambda: str(game.folder)), backlog_pause=0)
    poll_threads = []
    real_poll = scanner.poll

    def poll(*a, **k):
        poll_threads.append(threading.current_thread())
        return real_poll(*a, **k)

    scanner.poll = poll
    changed = []
    sync = qt.owned_sync.OwnedBlueprintSync(
        None, data, inv, on_changed=lambda: changed.append(1),
        scanner=scanner, interval_ms=interval_ms)
    sync.poll_threads, sync.changed_calls, sync.inv = poll_threads, changed, inv
    return sync


def test_blueprints_appear_by_themselves_and_keep_appearing(qt, game):
    game.start_session(bp_lines(HELMET))
    game.backup("Game Build(1) 01 Aug 26 (10 00 00).log", bp_lines(SHOTGUN))
    sync = make_sync(qt, game, FakeData())
    try:
        sync.start()                    # what opening the window does. No clicks.
        assert wait(qt.app, lambda: sync.inv.owned_count() == 2)
        assert owned_names(sync.inv) == sorted([HELMET, SHOTGUN])

        game.append(bp_lines(FR66))     # received while the tool is open
        assert wait(qt.app, lambda: sync.inv.owned_count() == 3)
        assert len(sync.changed_calls) >= 2, "the page is told to refresh"

        main = threading.main_thread()
        assert sync.poll_threads and all(t is not main for t in sync.poll_threads), \
            "logs are read off the UI thread"
        assert sync.inv.write_threads and all(t is main for t in sync.inv.write_threads), \
            "the inventory (and so the page) is updated on the UI thread"
        assert "3 blueprints found in logs" in sync.status_text()
    finally:
        sync.stop()


def test_it_does_not_matter_whether_logs_or_blueprint_data_arrive_first(qt, game):
    """The old auto-scan only ran from the 'crafting data loaded' callback, and
    only if the Owned page already existed.  Opening Fabricator first meant
    it never ran at all."""
    game.start_session(bp_lines(HELMET))
    data = FakeData(loaded=False)
    sync = make_sync(qt, game, data)
    try:
        sync.start()
        assert wait(qt.app, lambda: "waiting for blueprint data" in sync.status_text())
        assert sync.inv.owned_count() == 0
        data.crafting_loaded = True
        sync.on_crafting_changed()      # what MissionDBApp._on_crafting_loaded calls
        assert owned_names(sync.inv) == [HELMET]
    finally:
        sync.stop()


def test_no_star_citizen_folder_says_so_once_in_plain_words(qt, game):
    data = FakeData()
    sync = make_sync(qt, game, data, resolver=lambda: None)
    sync.inv.add("BP_helmet|ec-BP_helmet", BLUEPRINTS[2])
    texts = []
    sync.status_changed.connect(texts.append)
    try:
        sync.start()
        assert wait(qt.app, lambda: sync.status_text() == qt.owned_sync.NO_FOLDER_TEXT)
        assert wait(qt.app, lambda: len(sync.poll_threads) >= 5)
        assert texts.count(qt.owned_sync.NO_FOLDER_TEXT) == 1, "said once, not on every poll"
        assert "not found" in texts[-1] and "still here" in texts[-1]
        assert owned_names(sync.inv) == [HELMET], "the existing list is intact"
    finally:
        sync.stop()
    # (the qt fixture fails the test if any dialog was opened)


def test_manual_scan_reports_in_the_status_line_and_respects_removals(qt, game):
    game.start_session(bp_lines(HELMET) + bp_lines(FR66))
    sync = make_sync(qt, game, FakeData())
    reports = []
    sync.manual_done.connect(reports.append)
    try:
        sync.start()
        assert wait(qt.app, lambda: sync.inv.owned_count() == 2)
        sync.inv.remove("BP_fr66|ec-BP_fr66")
        sync.rescan()                   # the "Scan Game Log" button
        assert wait(qt.app, lambda: reports)
        assert reports[0]["names"] == 2 and reports[0]["added"] == 0
        assert reports[0]["held_back"] == 1
        assert owned_names(sync.inv) == [HELMET]
        assert "1 left off because you removed it" in sync.status_text()
        assert sync.restore_removed() == 1          # only when the player says yes
        assert owned_names(sync.inv) == [FR66, HELMET]
    finally:
        sync.stop()


def test_owned_page_shows_the_status_and_needs_no_click(qt, game):
    from ui.pages.owned_blueprints import OwnedBlueprintsPage
    game.start_session(bp_lines(HELMET))
    data = FakeData()
    sync = make_sync(qt, game, data)
    page = None
    sync._on_changed = lambda: page.refresh() if page is not None else None
    try:
        sync.start()
        page = OwnedBlueprintsPage(None, data, sync.inv, sync=sync)
        page.refresh()
        assert wait(qt.app, lambda: len(page._grid_items) == 1)
        assert wait(qt.app, lambda: "found in logs" in page.status_text())
        game.append(bp_lines(SHOTGUN))
        assert wait(qt.app, lambda: len(page._grid_items) == 2)
    finally:
        sync.stop()
        if page is not None:
            page.deleteLater()


def test_stopping_saves_the_position_and_ends_the_worker(qt, game):
    game.start_session(NOISE * 5 + bp_lines(HELMET))
    sync = make_sync(qt, game, FakeData())
    sync.start()
    assert wait(qt.app, lambda: sync.inv.owned_count() == 1)
    # Lines with no blueprint in them: the offset moves, but a poll only
    # writes that to disk once a minute.  Closing the tool must write it.
    game.append(NOISE * 40)
    size = game.live.stat().st_size
    assert wait(qt.app, lambda: sync._scanner._live.get("offset") == size)
    assert json.loads((qt.home / "state.json").read_text())["live"]["offset"] < size
    sync.stop()
    state = json.loads((qt.home / "state.json").read_text())
    assert state["live"]["offset"] == game.live.stat().st_size
    assert state["names"] == [HELMET]
    n = len(sync.poll_threads)
    time.sleep(0.15)
    qt.app.processEvents()
    assert len(sync.poll_threads) == n, "no polling after stop()"


# ── the player's real logs (opt-in, read-only) ───────────────────────────────

@pytest.mark.skipif(not os.environ.get("SCTB_REAL_LOGS"),
                    reason="set SCTB_REAL_LOGS=1 to read the real game logs (read-only)")
def test_real_logs_against_the_real_inventory(mods, tmp_path, capsys):
    """Reads the real logs and a COPY of the real inventory.  Writes only
    under tmp_path."""
    import glob
    real_home = os.path.join(os.path.expanduser("~"), ".sctoolbox", "mission_db")
    folder = None
    try:
        with open(os.path.join(real_home, "settings.json"), encoding="utf-8") as f:
            folder = json.load(f).get("sc_folder")
    except (OSError, ValueError):
        pass
    if not folder or not os.path.isdir(folder):
        from shared.sc_install import get_sc_root, newest_game_log
        g = newest_game_log(get_sc_root())
        folder = os.path.dirname(g) if g else None
    if not folder:
        pytest.skip("no Star Citizen folder on this machine")
    caches = sorted(glob.glob(os.path.join(_SKILL_DIR, ".scmdb_cache_crafting_*.json")),
                    key=os.path.getmtime)
    if not caches:
        pytest.skip("no cached blueprint list")
    with open(caches[-1], encoding="utf-8") as f:
        c = json.load(f)
    items = {i.get("entityClass"): i for i in (c.get("items") or {}).get("items", [])}

    class Data(FakeData):
        def __init__(self):
            self.crafting_loaded = True
            self.crafting_blueprints = (c.get("bp") or {}).get("blueprints", [])

        def get_blueprint_product(self, b):
            return items.get(b.get("productEntityClass") or "")

        def get_blueprint_product_name(self, b):
            return ((self.get_blueprint_product(b) or {}).get("name")
                    or b.get("productName") or b.get("tag") or "?")

    data = Data()
    inv_copy = tmp_path / "inventory_copy.json"
    real_inv = os.path.join(real_home, "inventory.json")
    if os.path.exists(real_inv):
        shutil.copyfile(real_inv, inv_copy)
    inv = mods.inv.InventoryService(path=str(inv_copy))
    held = inv.owned_ids()

    s = mods.sls.IncrementalLogScanner(
        state_path=str(tmp_path / "state.json"), folder_resolver=lambda: folder)
    t = time.perf_counter()
    first = s.poll()
    t_first = time.perf_counter() - t
    polls = []
    for _ in range(20):
        t = time.perf_counter()
        r = s.poll()
        polls.append((time.perf_counter() - t, r.bytes_read))
    s.RELIST_S = 0
    t = time.perf_counter()
    relist = s.poll()
    t_relist = time.perf_counter() - t

    matched, unmatched = mods.sls.match_names(s.names(), data.crafting_blueprints, data)
    from services.inventory import blueprint_key
    found = {blueprint_key(b) for b in matched}
    res = mods.sls.apply_log_names(s.names(), data.crafting_blueprints, inv, data)
    with capsys.disabled():
        print(f"\nREAL DATA  folder={folder}")
        print(f"  first scan: {first.files_read} files, {first.bytes_read / 1e9:.2f} GB, "
              f"{t_first:.1f} s, {len(s.names())} names")
        print(f"  tail poll : median {sorted(p[0] for p in polls)[10] * 1000:.2f} ms, "
              f"max {max(p[0] for p in polls) * 1000:.2f} ms, "
              f"bytes read {sorted(p[1] for p in polls)[10]}")
        print(f"  poll that re-lists logbackups: {t_relist * 1000:.1f} ms, "
              f"{relist.backups_read} backups read")
        print(f"  state file: {os.path.getsize(tmp_path / 'state.json'):,} bytes")
        print(f"  blueprint list: {len(data.crafting_blueprints)} ({os.path.basename(caches[-1])})")
        print(f"  logs -> {len(found)} blueprints; inventory holds {len(held)}")
        print(f"  in logs, missing from inventory: {len(found - held)} {sorted(found - held)[:5]}")
        print(f"  in inventory, not in logs      : {len(held - found)} {sorted(held - found)[:5]}")
        print(f"  names matching nothing: {unmatched}")
        print(f"  a scan now would add {len(res.added)}, hold back {len(res.held_back)}")
    assert relist.backups_read == 0
    assert first.folder
