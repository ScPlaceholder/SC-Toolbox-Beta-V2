"""The eyes' ship reference (core/eyes_reference.py): an index, tables fetched when first needed, kept.

What these hold the store to:

  - every request names the toolbox (the real host answers 403 to Python's default agent);
  - start-up asks for index.json and nothing else, once, and not again for a day;
  - a table on disk with the right checksum is used and NOTHING is requested;
  - a missing table is fetched with one request per file and never a second time, restarts included;
  - a damaged file is fetched again, even when its size and modified time look right;
  - a download that does not match the index is thrown away, and so is a front page served for a missing file;
  - with the site down nothing raises out of start-up and the error is a sentence;
  - with the address set to "" nothing is ever requested;
  - a newer index adds ships and tables without one old table being fetched again;
  - an older index from the site does not replace a newer one in hand;
  - only https is ever asked (http only to this PC, and only when a test says so), redirects included;
  - a path from a server is never written outside the reference folder;
  - recognition is not connected, and the module says so.

The site is http.server on 127.0.0.1 (tests/_reference_fixtures.py). No real network, no game, no window.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import eyes_reference as er
import _reference_fixtures as fx

V1 = fx.make_site(1)
V2 = fx.make_site(2)
ARRAY, LABELS = "fingerprint_table/t_one.f16.npy", "fingerprint_table/t_one.rows.json"


@pytest.fixture
def site():
    s = fx.PretendSite(V1)
    yield s
    s.stop()


def make_store(tmp_path, url="", bundled=V1, have=(), clock=None):
    """A store in tmp_path. `bundled` is the site whose index ships with the toolbox (None = none ships);
    `have` are site paths already in the pilot's folder."""
    home = tmp_path / "home"
    inst = tmp_path / "install" / "index.json"
    inst.parent.mkdir(parents=True, exist_ok=True)
    if bundled is not None:
        inst.write_bytes(bundled["index.json"])
    for path in have:
        p = home / "files" / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(V2[path])
    kw = {"clock": clock} if clock else {}
    s = er.ReferenceStore(home=home, base_url=url, bundled=inst, allow_loopback_http=True, **kw)
    s.reconcile()
    return s


def again(store, url=None, clock=None):
    """The same PC after a restart: a new store over the same folders."""
    kw = {"clock": clock} if clock else {}
    s = er.ReferenceStore(home=store.home, base_url=store.base_url if url is None else url,
                          bundled=store.home.parent / "install" / "index.json", allow_loopback_http=True, **kw)
    s.reconcile()
    return s


def on_disk(store, path):
    return store.home / "files" / Path(path)


# ---- who is asking -------------------------------------------------------------------------------------------

def test_every_request_names_the_toolbox(tmp_path, site):
    store = make_store(tmp_path, site.url)
    store.refresh_index()
    store.table_file("t_one")
    assert site.agents == [er.USER_AGENT, er.USER_AGENT]
    assert er.USER_AGENT.startswith("SC-Toolbox-SuitEyes/") and "Python" not in er.USER_AGENT


def test_the_pretend_site_refuses_pythons_default_agent(site):
    """The fixture itself: without this the test above could pass against a server that takes anyone."""
    import urllib.error
    import urllib.request
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(site.url + "/index.json", timeout=5)
    assert e.value.code == 403


def test_nothing_about_the_pilot_is_sent_only_gets_under_the_one_address(tmp_path, site):
    store = make_store(tmp_path, site.url)
    store.startup()
    store.table_rows("t_one")
    store.file("emb/m/vid0.npy")
    assert all(u.startswith(site.url + "/") and "?" not in u for u in store.requests)
    assert site.hits == ["index.json", LABELS, "manifest.json", "emb/m/vid0.npy"]


# ---- start-up ------------------------------------------------------------------------------------------------

def test_start_up_asks_for_the_index_and_nothing_else(tmp_path, site):
    store = make_store(tmp_path, site.url)
    r = store.startup()
    assert r["ok"] and r["asked"] and r["version"] == 1 and r["ships"] == 2
    assert site.hits == ["index.json"]
    assert r["bytes"] == len(V1["index.json"]) < er.MAX_INDEX_BYTES
    assert not (store.home / "files").exists()                 # no table, no manifest


def test_start_up_does_not_ask_again_within_a_day_and_does_after(tmp_path, site):
    now = [1000.0]
    store = make_store(tmp_path, site.url, clock=lambda: now[0])
    store.startup()
    now[0] += 3600
    r = again(store, clock=lambda: now[0]).startup()
    assert not r["asked"] and r["ok"] and site.hits == ["index.json"]
    now[0] += er.RECHECK_S
    assert again(store, clock=lambda: now[0]).startup()["asked"]
    assert site.hits == ["index.json", "index.json"]


def test_start_up_with_no_index_shipped_takes_the_sites(tmp_path, site):
    store = make_store(tmp_path, site.url, bundled=None)
    assert store.index.version == 0 and store.index.ships == {}
    assert store.startup()["version"] == 1
    assert again(store, url="").index.version == 1             # kept for the next start, offline included


# ---- on disk first -------------------------------------------------------------------------------------------

def test_a_table_on_disk_with_the_right_checksum_is_used_and_nothing_is_requested(tmp_path, site):
    store = make_store(tmp_path, site.url, have=(ARRAY, LABELS))
    assert store.table_file("t_one").read_bytes() == V1[ARRAY]
    assert len(store.table_rows("t_one")) == 4
    assert site.hits == [] and store.requests == []


def test_a_missing_table_is_fetched_once_and_never_again(tmp_path, site):
    store = make_store(tmp_path, site.url)
    assert store.plan("t_one")["to_fetch"] == len(V1[ARRAY]) + len(V1[LABELS])
    p = store.table_file("t_one")
    assert p == on_disk(store, ARRAY) and p.read_bytes() == V1[ARRAY]
    store.table_file("t_one")
    again(store).table_file("t_one")                           # nor after a restart
    assert site.hits == [ARRAY]
    assert store.plan("t_one")["to_fetch"] == len(V1[LABELS])   # the labels were never asked for
    assert not list(store.home.rglob("*.part"))


def test_only_the_table_asked_for_is_fetched(tmp_path, site):
    store = make_store(tmp_path, site.url)
    store.table_file("t_two")
    assert site.hits == ["fingerprint_table/t_two.f16.npy"]
    assert not on_disk(store, ARRAY).exists()


def test_two_threads_asking_at_once_make_one_request(tmp_path, site):
    store = make_store(tmp_path, site.url)
    ts = [threading.Thread(target=store.table_file, args=("t_one",)) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert site.hits == [ARRAY]


# ---- damage --------------------------------------------------------------------------------------------------

def test_a_damaged_file_is_fetched_again(tmp_path, site):
    store = make_store(tmp_path, site.url, have=(ARRAY,))
    on_disk(store, ARRAY).write_bytes(b"not a table")
    assert store.table_file("t_one").read_bytes() == V1[ARRAY]
    assert site.hits == [ARRAY]


def test_damage_that_keeps_the_size_and_the_modified_time_is_still_caught(tmp_path, site):
    store = make_store(tmp_path, site.url, have=(ARRAY,))
    p = on_disk(store, ARRAY)
    ref = store.table("t_one").files["f16_npy"]
    st = p.stat()
    bad = bytearray(V1[ARRAY])
    bad[len(bad) // 2] ^= 0xFF
    p.write_bytes(bytes(bad))
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert store.present(ref)                                   # the quick look cannot tell
    assert store.path_of(ref) is None                           # the full look can
    assert store.table_file("t_one").read_bytes() == V1[ARRAY]
    assert site.hits == [ARRAY]


def test_a_download_that_does_not_match_the_index_is_thrown_away(tmp_path):
    wrong = dict(V1)
    wrong[ARRAY] = fx.blob("someone else's file", len(V1[ARRAY]))        # same size, other content
    s = fx.PretendSite(wrong)
    try:
        store = make_store(tmp_path, s.url)
        with pytest.raises(er.ReferenceProblem, match="checksum mismatch"):
            store.table_file("t_one")
        assert not on_disk(store, ARRAY).exists() and not list(store.home.rglob("*.part"))
        assert ARRAY not in store.record
    finally:
        s.stop()


def test_a_bad_download_does_not_replace_a_good_file_already_there(tmp_path):
    wrong = dict(V1)
    wrong[LABELS] = b"[]"
    s = fx.PretendSite(wrong)
    try:
        store = make_store(tmp_path, s.url, have=(ARRAY,))
        with pytest.raises(er.ReferenceProblem):
            store.table_rows("t_one")
        assert on_disk(store, ARRAY).read_bytes() == V1[ARRAY]
    finally:
        s.stop()


def test_a_file_longer_than_the_index_says_is_cut_off_and_thrown_away(tmp_path):
    wrong = dict(V1)
    wrong[ARRAY] = V1[ARRAY] + fx.blob("padding", 400000)
    s = fx.PretendSite(wrong)
    try:
        store = make_store(tmp_path, s.url)
        with pytest.raises(er.ReferenceProblem, match="more than the index says"):
            store.table_file("t_one")
        assert not on_disk(store, ARRAY).exists() and not list(store.home.rglob("*.part"))
        assert store.transfers[-1]["bytes"] < len(wrong[ARRAY])            # it stopped reading
    finally:
        s.stop()


def test_the_front_page_served_for_a_missing_file_is_not_taken_for_the_file(tmp_path):
    """The real host answers a missing address with its front page and status 200."""
    files = {k: v for k, v in V1.items() if k not in (ARRAY, "index.json")}
    s = fx.PretendSite(files, front_page=fx.FRONT_PAGE)
    try:
        store = make_store(tmp_path, s.url)
        with pytest.raises(er.ReferenceProblem, match="does not have"):
            store.table_file("t_one")
        assert not on_disk(store, ARRAY).exists()
        r = store.startup()                                    # and the same for an index that is not there yet
        assert not r["ok"] and "no index yet" in r["message"] and r["version"] == 1
        assert not (store.home / "index.json").exists()
        assert not again(store).startup()["asked"]             # an answer is an answer: not asked at every start
    finally:
        s.stop()


# ---- the site is down, or switched off ---------------------------------------------------------------------------

def test_with_the_site_down_start_up_says_so_and_nothing_raises(tmp_path, site):
    store = make_store(tmp_path, site.url, have=(ARRAY,))
    site.stop()
    r = store.startup()
    assert r["ok"] is False and r["asked"] and r["message"].startswith("the reference site could not be reached")
    assert r["version"] == 1 and r["ships"] == 2               # the index that shipped is still in hand
    assert store.table_file("t_one").read_bytes() == V1[ARRAY]  # and what is on disk still works
    with pytest.raises(er.ReferenceProblem, match="could not be reached"):
        store.table_file("t_two")
    assert again(store).startup()["asked"]                     # no answer: try again at the next start


def test_start_up_never_raises_whatever_goes_wrong_inside_it(tmp_path, site, monkeypatch):
    store = make_store(tmp_path, site.url)

    def broken():
        raise RuntimeError("the disk is on fire")
    monkeypatch.setattr(store, "reconcile", broken)
    r = store.startup()
    assert r["ok"] is False and "could not start" in r["message"] and "the disk is on fire" in r["message"]
    assert site.hits == []


def test_a_site_that_refuses_is_a_sentence_not_a_crash(tmp_path):
    s = fx.PretendSite({})
    try:
        store = make_store(tmp_path, s.url)
        r = store.startup()
        assert r["ok"] is False and "(404)" in r["message"]
        with pytest.raises(er.ReferenceProblem, match="404"):
            store.table_file("t_one")
    finally:
        s.stop()


def test_start_in_background_logs_one_line_and_never_raises(tmp_path, site, monkeypatch):
    monkeypatch.setattr(er, "home_dir", lambda: tmp_path / "home")
    lines = []
    t = er.start_in_background(lines.append, {er.SETTINGS_KEY: "https://127.0.0.1:1"})
    t.join(30)
    assert len(lines) == 1 and "could not be reached" in lines[0] and "recognition not connected" in lines[0]

    def broken(_msg):
        raise RuntimeError("the log is gone")
    t = er.start_in_background(broken, {er.SETTINGS_KEY: ""})
    t.join(30)
    assert not t.is_alive()


def test_offline_setting_means_no_requests_at_all(tmp_path, site):
    store = make_store(tmp_path, "", have=(ARRAY,))
    r = store.startup()
    assert r["ok"] and not r["online"] and not r["asked"] and r["ships"] == 2
    assert store.table_file("t_one").read_bytes() == V1[ARRAY]
    with pytest.raises(er.ReferenceProblem, match="offline"):
        store.table_file("t_two")
    with pytest.raises(er.ReferenceProblem, match="offline"):
        store.refresh_index()
    assert store.requests == [] and site.hits == []


def test_the_settings_key_chooses_the_address(tmp_path):
    assert er.base_url_from({}) == er.BASE_URL == "https://suitmk2-eyes.pages.dev"
    assert er.base_url_from(None) == er.BASE_URL
    assert er.base_url_from({er.SETTINGS_KEY: None}) == er.BASE_URL
    assert er.base_url_from({er.SETTINGS_KEY: ""}) == ""
    assert er.base_url_from({er.SETTINGS_KEY: "  https://example.org/ref  "}) == "https://example.org/ref"
    import settings
    assert er.SETTINGS_KEY in settings.DEFAULTS and settings.DEFAULTS[er.SETTINGS_KEY] is None
    assert er.home_dir().parent == settings.DIR                 # beside the settings, not in the install


# ---- the index grows ---------------------------------------------------------------------------------------------

def test_a_newer_index_adds_ships_without_refetching_old_tables(tmp_path):
    s = fx.PretendSite(V1)
    try:
        store = make_store(tmp_path, s.url)
        store.startup()
        store.table_file("t_one")
        store.table_rows("t_one")
        assert list(store.index.ships) == ["alpha", "beta"] and store.whats_new() == {"ships": [], "tables": []}
        before = (on_disk(store, ARRAY).stat().st_mtime_ns, dict(store.record[ARRAY]))
        s.files = dict(V2)                                      # the site gains a batch
        s.hits.clear()
        later = again(store)
        got = later.refresh_index()
        assert got == {"version": 2, "ships": ["gamma"], "tables": ["b0002_m"], "changed": []}
        assert list(later.index.ships) == ["alpha", "beta", "gamma"]
        assert later.whats_new() == {"ships": ["gamma"], "tables": ["b0002_m"]}
        assert later.index.ships["alpha"].since == 1 and later.index.ships["gamma"].since == 2
        later.table_file("t_one")
        later.table_rows("t_one")
        assert s.hits == ["index.json"]                         # the old table: not one request
        assert (on_disk(later, ARRAY).stat().st_mtime_ns, later.record[ARRAY]) == before
        later.table_file("b0002_m")
        assert s.hits == ["index.json", "fingerprint_table/b0002/b0002_m.f16.npy"]
        later.mark_seen()
        restarted = again(later)                                # the fetched index is newer than the shipped one
        assert restarted.index.version == 2 and list(restarted.index.ships) == ["alpha", "beta", "gamma"]
        assert restarted.whats_new() == {"ships": [], "tables": []}
        assert again(later, url="").index.version == 2          # and offline too
    finally:
        s.stop()


def test_start_up_reports_new_ships_once(tmp_path):
    now = [5000.0]
    s = fx.PretendSite(V1)
    try:
        store = make_store(tmp_path, s.url, clock=lambda: now[0])
        assert store.startup()["new_ships"] == []               # a new PC: every ship is new, so none is news
        s.files = dict(V2)
        now[0] += er.RECHECK_S + 1
        r = again(store, clock=lambda: now[0]).startup()
        assert r["new_ships"] == ["gamma"] and "gamma" in r["message"] and r["ships"] == 3
        now[0] += er.RECHECK_S + 1
        assert again(store, clock=lambda: now[0]).startup()["new_ships"] == []
    finally:
        s.stop()


def test_an_index_from_a_later_version_with_keys_this_one_does_not_know_still_reads(tmp_path):
    s = fx.PretendSite(fx.make_site(2, extra_unknown_keys=True))
    try:
        store = make_store(tmp_path, s.url)
        assert store.refresh_index()["ships"] == ["gamma"]
    finally:
        s.stop()


def test_an_older_index_from_the_site_does_not_replace_the_one_in_hand(tmp_path, site):
    store = make_store(tmp_path, site.url, bundled=V2)          # the toolbox shipped version 2; the site says 1
    r = store.startup()
    assert r["ok"] is False and "older" in r["message"] and r["version"] == 2 and r["ships"] == 3
    assert not (store.home / "index.json").exists()


def test_the_newer_of_the_shipped_index_and_the_fetched_one_is_used(tmp_path, site):
    store = make_store(tmp_path, site.url, bundled=None)
    store.refresh_index()                                       # fetched: version 1
    inst = store.home.parent / "install" / "index.json"
    inst.write_bytes(V2["index.json"])                          # a toolbox update ships version 2
    assert again(store).index.version == 2
    inst.write_bytes(b"{ not json")
    assert again(store).index.version == 1                      # a broken shipped index: the fetched one


def test_the_same_version_describing_other_files_is_refused(tmp_path):
    other = json.loads(V1["index.json"])
    other["tables"][0]["files"]["f16_npy"]["sha256"] = "f" * 64
    files = dict(V1)
    files["index.json"] = json.dumps(other).encode()
    s = fx.PretendSite(files)
    try:
        store = make_store(tmp_path, s.url)
        with pytest.raises(er.ReferenceProblem, match="same version"):
            store.refresh_index()
        assert store.table("t_one").files["f16_npy"].sha256 != "f" * 64
    finally:
        s.stop()


# ---- any other file, through the batch's list --------------------------------------------------------------------

def test_any_published_file_comes_through_the_batch_list_which_is_itself_checked(tmp_path, site):
    store = make_store(tmp_path, site.url)
    p = store.file("emb/m/vid0.npy")
    assert p.read_bytes() == V1["emb/m/vid0.npy"]
    store.file("emb/m/vid0.npy")
    again(store).file("emb/m/vid0.npy")
    assert site.hits == ["manifest.json", "emb/m/vid0.npy"]
    with pytest.raises(er.ReferenceProblem, match="no batch"):
        store.file("emb/m/nothing.npy")


def test_a_batch_list_that_is_not_the_one_the_index_pinned_is_refused(tmp_path):
    files = dict(V1)
    files["manifest.json"] = V1["manifest.json"].replace(b"made up", b"MADE UP")
    s = fx.PretendSite(files)
    try:
        store = make_store(tmp_path, s.url)
        with pytest.raises(er.ReferenceProblem, match="checksum mismatch"):
            store.file("emb/m/vid0.npy")
        assert s.hits == ["manifest.json"]                      # the file itself was never asked for
    finally:
        s.stop()


# ---- the record --------------------------------------------------------------------------------------------------

def test_the_record_survives_a_restart_and_is_not_believed_over_the_disk(tmp_path, site):
    store = make_store(tmp_path, site.url)
    store.table_file("t_one")
    assert again(store).reconcile() == {"kept": [ARRAY], "adopted": [], "dropped": []}
    on_disk(store, ARRAY).unlink()
    later = again(store)
    assert ARRAY not in later.record and not later.present(later.table("t_one").files["f16_npy"])
    later.table_file("t_one")
    assert site.hits == [ARRAY, ARRAY]


def test_a_table_copied_in_by_hand_is_adopted_not_downloaded(tmp_path, site):
    store = make_store(tmp_path, site.url)
    p = on_disk(store, ARRAY)
    p.parent.mkdir(parents=True)
    p.write_bytes(V1[ARRAY])
    assert again(store).reconcile()["kept"] == [ARRAY]          # again() adopted it; this second look keeps it
    later = again(store)
    assert later.record[ARRAY]["origin"] == "adopted"
    later.table_file("t_one")
    assert site.hits == []


def test_a_wrong_file_copied_in_is_not_adopted(tmp_path, site):
    store = make_store(tmp_path, site.url)
    p = on_disk(store, ARRAY)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"something else")
    later = again(store)
    assert ARRAY not in later.record
    assert later.table_file("t_one").read_bytes() == V1[ARRAY]


def test_a_broken_record_is_an_empty_record(tmp_path, site):
    store = make_store(tmp_path, site.url, have=(ARRAY,))
    (store.home / "have.json").write_text("{ not json", encoding="utf-8")
    later = again(store)
    assert later.reconcile()["kept"] == [ARRAY]
    later.table_file("t_one")
    assert site.hits == []


# ---- addresses and paths -----------------------------------------------------------------------------------------

def test_only_https_is_asked_unless_a_test_allows_this_pc(tmp_path, site):
    for url in (site.url, "http://example.org", "ftp://example.org", "file:///C:/Windows"):
        store = er.ReferenceStore(home=tmp_path / "h", base_url=url, bundled=tmp_path / "none.json")
        with pytest.raises(er.ReferenceProblem, match="not an https address"):
            store.refresh_index()
        assert store.startup(force=True)["ok"] is False
    assert site.hits == []


def test_a_redirect_to_another_host_or_to_http_is_not_followed(tmp_path):
    elsewhere = fx.PretendSite(V1)
    s = fx.PretendSite({**V1, ARRAY: ("redirect", elsewhere.url + "/" + ARRAY)})
    try:
        store = make_store(tmp_path, s.url)
        with pytest.raises(er.ReferenceProblem, match="not an https address"):
            store.table_file("t_one")
        assert elsewhere.hits == [] and not on_disk(store, ARRAY).exists()
    finally:
        s.stop()
        elsewhere.stop()


def test_a_redirect_inside_the_site_is_followed(tmp_path):
    s = fx.PretendSite(V1)
    s.files[ARRAY] = ("redirect", s.url + "/moved/" + ARRAY)
    s.files["moved/" + ARRAY] = V1[ARRAY]
    try:
        store = make_store(tmp_path, s.url)
        assert store.table_file("t_one").read_bytes() == V1[ARRAY]
        assert len(store.requests) == 2
    finally:
        s.stop()


@pytest.mark.parametrize("bad", ["../outside.npy", "fingerprint_table/../../x", "/etc/passwd", "C:/x.npy",
                                 "a\\b.npy", "a//b", "", ".hidden", "con/x.npy", "a/NUL.npy", "a/b.", "a b",
                                 "x" * 201, "a/?q=1", "a/%2e%2e/b"])
def test_a_path_from_a_server_never_leaves_the_reference_folder(tmp_path, bad):
    assert not er.safe_site_path(bad)
    index = json.loads(V1["index.json"])
    index["tables"][0]["files"]["f16_npy"]["path"] = bad
    with pytest.raises(er.ReferenceProblem):
        er.parse_index(json.dumps(index).encode())
    store = make_store(tmp_path)
    with pytest.raises(er.ReferenceProblem):
        store._dest(bad)


def test_real_site_paths_are_accepted():
    for good in ("manifest.json", "fingerprint_table/v2_clip_pm_all.gallery_mean.npy", "emb/clip_h__cc__pm/-5-MoABPbWw.npy",
                 "meta/frame_index/_x-Y.json", "batches/b0002/manifest.json", "index_history/v0002.json"):
        assert er.safe_site_path(good), good


@pytest.mark.parametrize("change", [
    lambda d: d.update(format=2),
    lambda d: d.update(version=0),
    lambda d: d.pop("tables"),
    lambda d: d["tables"][0]["files"]["f16_npy"].update(sha256="xyz"),
    lambda d: d["tables"][0]["files"]["f16_npy"].update(bytes=0),
    lambda d: d["tables"].append(d["tables"][0]),
    lambda d: d["ships"][0].update(key="Not Plain"),
    lambda d: d["batches"][0]["manifest"].update(bytes=er.MAX_MANIFEST_BYTES + 1),
])
def test_an_index_that_is_not_right_is_refused_whole(change):
    d = json.loads(V1["index.json"])
    change(d)
    with pytest.raises(er.ReferenceProblem):
        er.parse_index(json.dumps(d).encode())
    with pytest.raises(er.ReferenceProblem):
        er.parse_index(b"<!DOCTYPE html><html></html>")


def test_a_file_bigger_than_the_suit_will_take_is_not_requested(tmp_path, site):
    d = json.loads(V1["index.json"])
    d["tables"][0]["files"]["f16_npy"]["bytes"] = er.MAX_FILE_BYTES + 1
    store = make_store(tmp_path, site.url, bundled={"index.json": json.dumps(d).encode()})
    with pytest.raises(er.ReferenceProblem, match="more than the Suit will download"):
        store.table_file("t_one")
    assert site.hits == []


# ---- what ships with the toolbox ---------------------------------------------------------------------------------

def test_the_index_that_ships_with_the_toolbox_reads_and_is_version_1_of_ten_ships():
    idx = er.parse_index(er.BUNDLED_INDEX.read_bytes())
    assert idx.version == 1 and len(idx.ships) == 10 and len(idx.tables) == 5
    assert all(s.since == 1 for s in idx.ships.values())
    assert idx.families()["ironclads"] == ["ironclad", "ironclad_assault"]
    assert idx.batches["b0001"]["manifest"].path == "manifest.json"
    biggest = max(r.bytes for t in idx.tables.values() for r in t.files.values())
    assert biggest == 19993728 < er.MAX_FILE_BYTES
    assert "cannot name a ship from one frame" in idx.limits
    # one table is a part of another: stacking a space must not count its rows twice
    assert [t.id for t in idx.space("clip_h")] == ["clip_h_all"]
    for t in idx.tables.values():
        assert sum(t.rows_per_ship.values()) == t.rows and set(t.rows_per_ship) == set(idx.ships)


def test_no_file_or_folder_name_in_the_shipped_index_carries_a_maker_or_the_game():
    idx = er.parse_index(er.BUNDLED_INDEX.read_bytes())
    paths = [r.path for t in idx.tables.values() for r in t.files.values()]
    paths += [b["manifest"].path for b in idx.batches.values()]
    banned = ("starcitizen", "star_citizen", "rsi", "cig", "drake", "aegis", "anvil", "crusader", "origin", "misc")
    for p in paths:
        words = [w for w in p.lower().replace("/", "_").replace(".", "_").replace("-", "_").split("_") if w]
        assert not set(words) & set(banned), p


# ---- recognition, and what this module needs ---------------------------------------------------------------------

def test_recognition_is_not_connected_and_the_module_says_so(tmp_path):
    assert er.RECOGNITION_CONNECTED is False
    assert make_store(tmp_path).status()["recognition"].startswith("not connected")
    public = [n for n in dir(er.ReferenceStore) if not n.startswith("_")]
    assert not [n for n in public if any(w in n for w in ("recogni", "identify", "classify", "match", "name_ship"))]


def test_importing_the_module_loads_no_numpy_no_qt_and_nothing_else_of_the_suit():
    code = ("import sys; sys.path.insert(0, r'%s'); import eyes_reference; "
            "print(sorted(m for m in ('numpy', 'PySide6', 'eyes', 'settings', 'requests') if m in sys.modules))"
            % Path(er.__file__).parent)
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == "[]", out.stdout + out.stderr


def test_a_real_array_loads_with_numpy_mapped_and_a_wrong_shape_is_refused(tmp_path):
    np = pytest.importorskip("numpy")
    import io
    arr = (np.arange(32, dtype=np.float16).reshape(4, 8) / 32).astype(np.float16)
    buf = io.BytesIO()
    np.save(buf, arr)
    files = dict(V1)
    files[ARRAY] = buf.getvalue()
    d = json.loads(V1["index.json"])
    d["tables"][0]["files"]["f16_npy"] = fx.ref(ARRAY, files[ARRAY])
    files["index.json"] = json.dumps(d).encode()
    s = fx.PretendSite(files)
    try:
        store = make_store(tmp_path, s.url, bundled=files)
        got = store.load_table("t_one")
        assert got.shape == (4, 8) and got.dtype == np.float16 and np.array_equal(np.asarray(got), arr)
        assert isinstance(got, np.memmap) and not got.flags.writeable
        store.load_table("t_one")
        assert s.hits == [ARRAY]
        del got
        d["tables"][0]["rows"] = 5
        wrong = make_store(tmp_path / "b", s.url, bundled={"index.json": json.dumps(d).encode()})
        with pytest.raises(er.ReferenceProblem, match="not the 5 x 8"):
            wrong.load_table("t_one")
    finally:
        s.stop()


def test_the_checksum_is_sha256_of_the_whole_file(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(V1[ARRAY])
    assert er.sha256_of(p) == hashlib.sha256(V1[ARRAY]).hexdigest()
    assert er.sha256_of(tmp_path / "missing") is None
