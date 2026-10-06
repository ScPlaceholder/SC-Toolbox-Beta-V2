"""Outfit packs (pico/packs.py): one file per outfit, fetched once, only the worn one unpacked.

What these hold the store to:

  - a pack on disk with the right checksum is used and NOTHING is requested;
  - a missing pack is fetched with one request, and never a second time;
  - a damaged pack is fetched again, even when its size and modified time look right;
  - a download that does not match the list is thrown away and the outfit being worn is untouched;
  - with the server down the outfit being worn is untouched and the error is a sentence;
  - switching outfits removes the old unpacked folder and keeps its pack, while some pack is missing;
    with every pack on the PC nothing unpacked is removed, then or later;
  - "download every Pal" skips what is already here;
  - the record of packs survives a restart, is not believed over the disk, and adopts a pack copied in;
  - nothing is requested at start-up, and a loop FOLDER still wins over any pack (the developer's setup);
  - only https is ever asked (http only to this PC, and only when a test says so), redirects included.

The server is http.server on 127.0.0.1 (tests/_pack_fixtures.py) and it counts requests. The packs are small
made-up ones in the real format. The last tests use the real Drake pack if one is in tools/Pico/packs.
"""
import json
import os
import sys
import threading
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent
for _p in (str(HERE), str(Path(__file__).resolve().parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from pico import packs, sprites  # noqa: E402
import _pack_fixtures as fx  # noqa: E402

BLOBS = {code: fx.make_pack(code, outfit) for code, outfit in fx.OUTFITS.items()}


@pytest.fixture
def server():
    s = fx.PretendServer(fx.site(BLOBS))
    yield s
    s.stop()


def make_store(tmp_path, url="", blobs=BLOBS, bundled=("o08",), user=(), flag=True, manifest=None):
    """A store in tmp_path: `bundled` packs in the pretend install, `user` packs already in the user's folder."""
    home, inst = tmp_path / "home", tmp_path / "install"
    (home / "packs").mkdir(parents=True, exist_ok=True)
    inst.mkdir(exist_ok=True)
    (inst / "packs.json").write_text(json.dumps(manifest or fx.make_manifest(blobs)), encoding="utf-8")
    for code in bundled:
        (inst / (code + ".tar.xz")).write_bytes(blobs[code])
    for code in user:
        (home / "packs" / (code + ".tar.xz")).write_bytes(blobs[code])
    s = packs.PackStore(home=home, bundled=inst, base_url=url, allow_loopback_http=flag)
    s.reconcile()
    return s


def again(store, url=None):
    """The same PC after a restart: a new store over the same folders."""
    s = packs.PackStore(home=store.home, bundled=store.bundled_dir,
                        base_url=store.base_url if url is None else url, allow_loopback_http=True)
    s.reconcile()
    return s


def user_pack(store, code):
    return store.packs_dir / (code + ".tar.xz")


# ---- on disk first -----------------------------------------------------------------------------------------

def test_a_pack_on_disk_with_the_right_checksum_is_used_and_nothing_is_requested(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02",))
    folder = store.wear("o02")
    assert sorted(p.stem for p in folder.glob("*.webp")) == sorted(fx.LOOPS)
    assert (folder / "idle_look_default.anchors.json").is_file()
    assert server.hits == [] and store.requests == []


def test_the_pack_shipped_with_the_toolbox_is_worn_from_the_install_without_a_request(tmp_path, server):
    store = make_store(tmp_path, server.url)
    assert store.default_code() == "o08"
    folder = store.wear("o08")
    assert folder.name == "pico_anim_sequences" and folder.parent == store.worn_dir / "o08"
    assert not user_pack(store, "o08").exists()              # read in place, not copied
    assert server.hits == []


def test_a_missing_pack_is_fetched_with_one_request_and_never_again(tmp_path, server):
    store = make_store(tmp_path, server.url)
    assert not store.have("o02")
    store.wear("o02")
    assert server.hits == ["/packs/o02.tar.xz"]
    assert user_pack(store, "o02").read_bytes() == BLOBS["o02"]
    store.deactivate("o02")
    store.wear("o02")
    again(store).wear("o02")                                  # nor after a restart
    assert server.hits == ["/packs/o02.tar.xz"]


def test_a_damaged_pack_is_fetched_again(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02",))
    user_pack(store, "o02").write_bytes(b"not a pack")
    store.wear("o02")
    assert server.packs_asked() == ["o02.tar.xz"]
    assert user_pack(store, "o02").read_bytes() == BLOBS["o02"]


def test_damage_that_keeps_the_size_and_the_modified_time_is_still_caught_before_wearing(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02",))
    p = user_pack(store, "o02")
    st = p.stat()
    bad = bytearray(BLOBS["o02"])
    bad[len(bad) // 2] ^= 0xFF
    p.write_bytes(bytes(bad))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert store.present("o02")                               # the quick look cannot tell
    store.wear("o02")                                         # the full checksum can
    assert server.packs_asked() == ["o02.tar.xz"]
    assert p.read_bytes() == BLOBS["o02"]


# ---- what is refused ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("served", ["other art", "half"])
def test_a_download_that_does_not_match_the_list_is_thrown_away_and_the_worn_outfit_is_kept(tmp_path, server, served):
    store = make_store(tmp_path, server.url)
    worn = store.wear("o08")
    server.files["o02.tar.xz"] = (fx.make_pack("o02", "anvil", salt="wrong") if served == "other art"
                                  else BLOBS["o02"][:len(BLOBS["o02"]) // 2])
    with pytest.raises(packs.PackError) as ex:
        store.wear("o02")
    assert "checksum" in str(ex.value)
    assert not user_pack(store, "o02").exists() and not list(store.packs_dir.glob("*.part"))
    assert not store.have("o02") and "o02" not in store.record
    assert store.unpacked() == ["o08"] and len(sprites.Catalog.scan(worn).loops) == len(fx.LOOPS)
    assert server.packs_asked() == ["o02.tar.xz", "packs.json"]      # the list was checked once, no retry


def test_with_the_server_down_the_worn_outfit_is_kept_and_the_error_is_a_sentence(tmp_path):
    store = make_store(tmp_path, fx.dead_url())
    worn = store.wear("o08")
    with pytest.raises(packs.PackError) as ex:
        store.wear("o02")
    assert "could not be reached" in str(ex.value) and "Traceback" not in str(ex.value)
    assert not isinstance(ex.value, packs.PackCancelled)
    assert store.unpacked() == ["o08"] and sprites.Catalog.scan(worn).loops
    assert not list(store.packs_dir.glob("*.part"))
    with pytest.raises(packs.PackError):
        store.fetch_all()                                     # the same for "download every Pal"
    assert store.unpacked() == ["o08"]


def test_a_cancelled_download_leaves_no_pack_and_no_part_file(tmp_path):
    big = dict(BLOBS, o02=fx.make_pack("o02", "anvil", size=20000))
    server = fx.PretendServer(fx.site(big), delay=0.01)
    try:
        store = make_store(tmp_path, server.url, blobs=big)
        cancel, seen = threading.Event(), []

        def progress(code, done, total, index, count):
            seen.append((code, done, total))
            cancel.set()

        with pytest.raises(packs.PackCancelled):
            store.wear("o02", progress, cancel)
        assert seen and seen[0][0] == "o02" and seen[0][1] < seen[0][2] == len(big["o02"])
        assert not user_pack(store, "o02").exists() and not list(store.packs_dir.glob("*.part"))
    finally:
        server.stop()


def test_only_https_is_asked_and_http_to_this_pc_only_when_a_test_says_so(tmp_path, server):
    plain = make_store(tmp_path / "a", server.url, flag=False)
    with pytest.raises(packs.PackError) as ex:
        plain.wear("o02")
    assert "https" in str(ex.value) and server.hits == [] and plain.requests == []
    far = make_store(tmp_path / "b", "http://192.0.2.1/packs")           # not this PC: refused unasked
    with pytest.raises(packs.PackError):
        far.wear("o02")
    assert far.requests == []
    for bad in ("ftp://127.0.0.1/packs", "file:///C:/packs", "//127.0.0.1/packs"):
        with pytest.raises(packs.PackError):
            make_store(tmp_path / "c", bad).wear("o02")


def test_a_redirect_to_an_address_that_is_not_allowed_is_not_followed(tmp_path, server):
    server.files["o02.tar.xz"] = ("redirect", "http://192.0.2.1/elsewhere/o02.tar.xz")
    store = make_store(tmp_path, server.url)
    with pytest.raises(packs.PackError) as ex:
        store.wear("o02")
    assert "https" in str(ex.value)
    assert store.requests == [server.url + "/o02.tar.xz"]      # the refused address was never asked
    assert not user_pack(store, "o02").exists()


def test_with_no_pack_address_nothing_is_ever_requested(tmp_path, server):
    assert packs.PACKS_URL == "https://pico-pals.pages.dev/packs", "the one address the toolbox ships with"
    assert "pico-pals.pages.dev" == packs.PACKS_URL.split("/")[2] and packs.PACKS_URL.startswith("https://")
    store = make_store(tmp_path, "")
    assert not store.online
    assert {state for _c, _n, state, _b in store.listing()} == {"included", "unavailable"}
    store.wear("o08")
    with pytest.raises(packs.PackError) as ex:
        store.wear("o02")
    assert "not set up" in str(ex.value)
    with pytest.raises(packs.PackError):
        store.fetch_all()
    assert store.requests == [] and server.hits == []


@pytest.mark.parametrize("name", ["../evil.webp", "sub/evil.webp", "evil.exe", ".hidden.webp"])
def test_a_pack_cannot_put_a_file_anywhere_but_its_own_folder(tmp_path, name):
    blobs = dict(BLOBS, o02=fx.make_pack("o02", "anvil", extra=[(name, b"x")]))
    store = make_store(tmp_path, "", blobs=blobs, user=("o02",))
    with pytest.raises(packs.PackError):
        store.wear("o02")
    assert store.unpacked() == []
    assert not [p for p in tmp_path.rglob("*evil*")] and not [p for p in tmp_path.rglob(".hidden*")]


@pytest.mark.parametrize("field,value", [("pack", "../o02.tar.xz"), ("pack", "C:\\o02.tar.xz"), ("code", "../x"),
                                         ("outfit", "..\\up"), ("sha256", "zz"), ("bytes", -1)])
def test_a_list_with_a_row_that_could_be_a_path_is_refused(field, value):
    man = fx.make_manifest(BLOBS)
    man["outfits"][0][field] = value
    with pytest.raises(packs.PackError):
        packs.parse_manifest(man)


def test_packs_rebuilt_on_the_server_are_accepted_through_the_new_list_with_one_download(tmp_path):
    new = dict(BLOBS, o02=fx.make_pack("o02", "anvil", salt="eyes fixed"))
    server = fx.PretendServer(fx.site(new, version="t2"))
    try:
        store = make_store(tmp_path, server.url)                      # still holds the old list
        store.wear("o02")
        assert server.packs_asked() == ["o02.tar.xz", "packs.json"]
        assert user_pack(store, "o02").read_bytes() == new["o02"]
        assert again(store).manifest.version == "t2" and again(store).have("o02")
    finally:
        server.stop()


def test_an_older_whole_pack_is_still_worn_when_its_replacement_cannot_be_fetched(tmp_path, server):
    store = make_store(tmp_path, server.url)
    store.wear("o02")
    store.deactivate("o02")
    new = dict(BLOBS, o02=fx.make_pack("o02", "anvil", salt="newer"))
    (store.packs_dir / "packs.json").write_text(json.dumps(fx.make_manifest(new, version="t2")), encoding="utf-8")
    later = again(store, url=fx.dead_url())
    assert not later.have("o02")
    assert later.wear("o02").is_dir()


# ---- taking outfits off ------------------------------------------------------------------------------------

def test_switching_removes_the_old_unpacked_folder_and_keeps_its_pack(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02", "o05"))     # o16 is not here: the set is incomplete
    assert not store.complete()
    store.wear("o02")
    store.wear("o05")
    assert store.deactivate("o02") is True
    assert store.unpacked() == ["o05"] and not (store.worn_dir / "o02").exists()
    assert user_pack(store, "o02").read_bytes() == BLOBS["o02"]
    store.wear("o02")                                                 # back again: no internet needed
    assert server.hits == []


def test_with_every_pack_on_the_pc_switching_removes_nothing(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02", "o05", "o16"))
    assert store.complete()
    store.wear("o02")
    store.wear("o05")
    assert store.deactivate("o02") is False
    assert store.unpacked() == ["o02", "o05"]
    assert again(store).sweep(active="o05") == [] and again(store).unpacked() == ["o02", "o05"]


def test_the_set_becoming_complete_stops_removal_from_then_on(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02", "o05"))
    store.wear("o02")
    store.wear("o05")
    assert store.deactivate("o02") is True                            # incomplete: removed
    out = store.fetch_all()
    assert out["fetched"] == ["o16"] and store.complete()
    store.wear("o02")
    assert store.deactivate("o05") is False                           # complete: stays
    assert store.unpacked() == ["o02", "o05"]                         # o16 is not unpacked until it is worn


def test_a_longer_list_later_removes_nothing_that_was_kept_only_what_is_unpacked_after(tmp_path, server):
    three = {c: BLOBS[c] for c in ("o02", "o05", "o08")}
    store = make_store(tmp_path, server.url, blobs=three, user=("o02", "o05"))
    assert store.complete()
    store.wear("o02")
    store.wear("o08")
    assert store.deactivate("o02") is False                           # kept, and marked kept
    (store.packs_dir / "packs.json").write_text(json.dumps(fx.make_manifest(BLOBS, version="t2")), encoding="utf-8")
    later = again(store)
    assert not later.complete()                                       # the new list has o16
    later.wear("o05")
    assert later.deactivate("o02") is False and "o02" in later.unpacked()
    later.wear("o08")
    assert later.deactivate("o05") is True                            # unpacked after: removed as usual
    assert later.unpacked() == ["o02", "o08"]


@pytest.mark.skipif(sys.platform != "win32", reason="only Windows refuses to move a folder with an open file in it")
def test_an_outfit_whose_loop_is_still_open_is_left_whole_not_half_removed(tmp_path):
    store = make_store(tmp_path, "", user=("o02", "o05"))
    folder = store.wear("o02")
    before = sorted(p.name for p in folder.iterdir())
    with open(folder / "sad_sad.webp", "rb"):                         # a loop that is still playing
        assert store.deactivate("o02") is False
        assert sorted(p.name for p in folder.iterdir()) == before     # nothing was deleted around it
        assert store.activate("o02") == folder                        # and it can still be worn
    assert store.deactivate("o02") is True and store.unpacked() == []


def test_start_up_clears_outfits_left_unpacked_except_the_worn_one(tmp_path):
    store = make_store(tmp_path, "", user=("o02", "o05"))
    store.wear("o02")
    store.wear("o05")
    (store.worn_dir / "o16.new" / "x").mkdir(parents=True)            # an unpack that never finished
    assert again(store).sweep(active="o05") == ["o02"]
    assert sorted(d.name for d in store.worn_dir.iterdir()) == ["o05"]
    assert user_pack(store, "o02").exists()


# ---- download every Pal ------------------------------------------------------------------------------------

def test_download_every_pal_skips_the_packs_already_here(tmp_path, server):
    store = make_store(tmp_path, server.url, user=("o02",))
    seen = []
    out = store.fetch_all(lambda code, done, total, index, count: seen.append((code, index, count)))
    assert out["fetched"] == ["o05", "o16"] and sorted(out["skipped"]) == ["o02", "o08"] and not out["failed"]
    assert server.packs_asked() == ["packs.json", "o05.tar.xz", "o16.tar.xz"]
    assert {(c, i, n) for c, i, n in seen} == {("o05", 1, 2), ("o16", 2, 2)}
    assert store.complete() and store.missing() == []
    store.fetch_all()                                                 # again: only the list is asked for
    assert server.packs_asked()[3:] == ["packs.json"]


def test_download_every_pal_stops_at_a_bad_pack_and_keeps_the_good_ones(tmp_path, server):
    server.files["o05.tar.xz"] = b"garbage"
    store = make_store(tmp_path, server.url, user=("o02",))
    out = store.fetch_all()
    assert list(out["failed"]) == ["o05"] and out["left"] == ["o16"] and out["fetched"] == []
    assert store.have("o02") and not store.have("o05")


# ---- the record --------------------------------------------------------------------------------------------

def test_the_record_survives_a_restart_and_lists_without_reading_a_pack(tmp_path, server, monkeypatch):
    store = make_store(tmp_path, server.url)
    store.wear("o02")
    row = json.loads((store.packs_dir / "have.json").read_text(encoding="utf-8"))["packs"]["o02"]
    assert row["file"] == "o02.tar.xz" and row["bytes"] == len(BLOBS["o02"]) and row["origin"] == "fetched"
    assert row["sha256"] == store.entry("o02").sha256 and row["version"] == "t1" and row["fetched"] > 0
    read = []
    real = packs.sha256_of
    monkeypatch.setattr(packs, "sha256_of", lambda p: read.append(Path(p).name) or real(p))
    later = again(store)
    assert later.record["o02"] == row
    assert dict((c, s) for c, _n, s, _b in later.listing())["o02"] == "downloaded"
    assert later.present("o02") and read == []                        # size and time were enough
    later.wear("o02")
    assert "o02.tar.xz" in read and server.packs_asked() == ["o02.tar.xz"]   # read in full before wearing


def test_a_deleted_pack_is_noticed_and_the_record_is_not_believed(tmp_path, server):
    store = make_store(tmp_path, server.url)
    store.wear("o02")
    store.deactivate("o02")
    user_pack(store, "o02").unlink()
    assert "o02" in json.loads((store.packs_dir / "have.json").read_text(encoding="utf-8"))["packs"]
    raw = packs.PackStore(home=store.home, bundled=store.bundled_dir, base_url=server.url, allow_loopback_http=True)
    assert not raw.present("o02") and not raw.have("o02")             # even before the record is tidied
    assert raw.reconcile()["dropped"] == ["o02"]
    assert "o02" not in json.loads((store.packs_dir / "have.json").read_text(encoding="utf-8"))["packs"]
    assert dict((c, s) for c, _n, s, _b in raw.listing())["o02"] == "available"
    raw.wear("o02")
    assert server.packs_asked() == ["o02.tar.xz", "o02.tar.xz"]       # offered, and fetched, again


def test_a_record_row_for_a_pack_that_was_swapped_for_another_file_is_dropped(tmp_path, server):
    store = make_store(tmp_path, server.url)
    store.wear("o02")
    user_pack(store, "o02").write_bytes(b"something else entirely, longer or shorter than the pack")
    later = again(store)
    assert "o02" not in later.record and not later.present("o02")


def test_a_pack_copied_in_by_hand_is_adopted_without_a_download(tmp_path, server):
    store = make_store(tmp_path, server.url)
    assert not store.present("o05")
    user_pack(store, "o05").write_bytes(BLOBS["o05"])                 # the user copies it in; no record of it
    user_pack(store, "o16").write_bytes(b"a file with the right name and the wrong contents")
    later = again(store)
    assert later.record["o05"]["origin"] == "adopted" and "o16" not in later.record
    states = dict((c, s) for c, _n, s, _b in later.listing())
    assert states["o05"] == "downloaded" and states["o16"] == "available"
    later.wear("o05")
    assert server.hits == []


# ---- starting up, and the developer's folders --------------------------------------------------------------

def test_nothing_is_requested_at_start_up_when_the_worn_pack_is_here(tmp_path, server):
    import sprite_pal
    first = make_store(tmp_path, server.url)
    first.wear("o02")
    asked = len(server.hits)
    store = again(first)
    folder, code = sprite_pal.start_outfit(sprites.DEFAULT_DIR, {"outfit": "pack:o02"}, store)
    assert code == "o02" and store.code_of(folder) == "o02"
    assert sprites.LoopChooser(sprites.Catalog.scan(folder)).pools
    assert store.requests == [] and len(server.hits) == asked


def test_start_up_never_fetches_a_missing_pack_it_wears_the_shipped_outfit(tmp_path, server, monkeypatch):
    import sprite_pal
    monkeypatch.setattr(sprites, "DEFAULT_DIR", tmp_path / "no_such_folder")
    store = make_store(tmp_path, server.url)
    folder, code = sprite_pal.start_outfit(sprites.DEFAULT_DIR, {"outfit": "pack:o05"}, store)
    assert code == "o08" and sprite_pal.outfit_name(folder) == "Drake"
    assert server.hits == [] and store.requests == []
    folder, code = sprite_pal.start_outfit(sprites.DEFAULT_DIR, {}, store)     # a first launch
    assert code == "o08" and server.hits == []


def test_a_loop_folder_still_wins_and_no_pack_is_touched(tmp_path, server, monkeypatch):
    import sprite_pal
    dev = tmp_path / "VNCCS" / "pico_anim_sequences"
    cnou = tmp_path / "VNCCS" / "pico_anim_sequences_cnou"
    for d in (dev, cnou):
        d.mkdir(parents=True)
        (d / "idle_look_default.webp").write_bytes(b"x")
    monkeypatch.setattr(sprites, "DEFAULT_DIR", dev)
    store = make_store(tmp_path, server.url)
    assert sprite_pal.start_outfit(dev, {"outfit": str(cnou)}, store) == (cnou, None)     # a saved folder
    assert sprite_pal.start_outfit(dev, {}, store) == (dev, None)                        # the working tree
    assert sprite_pal.start_outfit(dev, {"outfit": str(tmp_path / "gone")}, store) == (dev, None)
    other = tmp_path / "elsewhere"
    assert sprite_pal.start_outfit(other, {"outfit": "pack:o08"}, store) == (other, None)   # --loops
    assert store.unpacked() == [] and store.requests == [] and server.hits == []
    assert sprite_pal.outfits(dev) == {"Drake": dev, "Cnou": cnou}
    assert sprite_pal.outfit_name(cnou, dev) == "Cnou" and sprite_pal.outfit_name(dev, dev) == "Drake"


def test_an_unpacked_outfit_is_named_and_branded_like_its_loop_folder(tmp_path):
    import sprite_pal
    store = make_store(tmp_path, "", user=("o05", "o16"))
    banu, origin, drake = store.wear("o05"), store.wear("o16"), store.wear("o08")
    assert [sprite_pal.outfit_name(f) for f in (banu, origin, drake)] == ["Banu", "Origin", "Drake"]
    assert sprites.brand_allows("idle_shuffle_default+bmm_table", banu)
    assert not sprites.brand_allows("idle_shuffle_default+bmm_table", drake)
    assert store.code_of(banu) == "o05" and store.code_of(tmp_path) is None


# ---- the real thing, when it is on this PC -----------------------------------------------------------------

REAL = packs.BUNDLED_DIR


def test_no_pack_file_name_names_a_maker_or_the_game():
    man = packs.PackStore._read_manifest(REAL / "packs.json")
    if not man.entries:
        pytest.skip("no tools/Pico/packs/packs.json on this PC")
    for e in man.entries.values():
        assert e.pack == e.code + ".tar.xz" and e.outfit not in e.pack, e


def test_the_real_shipped_pack_unpacks_into_an_outfit_every_mood_can_use(tmp_path):
    man = packs.PackStore._read_manifest(REAL / "packs.json")
    if not man.default or not (REAL / man.entries[man.default].pack).is_file():
        pytest.skip("no shipped pack in tools/Pico/packs on this PC")
    store = packs.PackStore(home=tmp_path / "home", bundled=REAL, base_url="")
    assert store.default_code() == man.default and store.have(man.default)
    folder = store.wear(man.default)
    cat = sprites.Catalog.scan(folder)
    assert len(cat.loops) == man.entries[man.default].loops
    cat.check()                                                       # raises if a mood has no loop
    assert len(list(folder.glob("*.anchors.json"))) > 100
    assert store.requests == []
