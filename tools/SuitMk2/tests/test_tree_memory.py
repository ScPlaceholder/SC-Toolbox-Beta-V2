"""Christmas Tree Storage: the conversation memory (J, 2026-10-05).

J's design and J's name. A permanent log is the trunk; above it summaries that get coarser with distance from now;
"Compression changes accessibility, not existence." What these tests hold the build to:

  * the log is only ever appended to, and nothing that builds, rebuilds or deletes a summary touches it;
  * a summary is a SELECTION of the pilot's own sentences: every sentence in every node is byte-for-byte a line
    of the log it points to;
  * from any summary the exact original exchange can be recovered;
  * the upper levels (week, month, year) are the same mechanism as session and day, exist only when there is
    something under them, and adding one is a row in a table;
  * the trunk is shared and each companion has a top of its own;
  * finding something does not depend on any summary being right;
  * what is kept is only what was said to the Suit and what was said back, it is covered by Export and Import,
    and the pilot can clear it.

The real TreeStore on a real folder, the real CompanionCore, the real export and import. No model anywhere.
"""
from __future__ import annotations

import hashlib
import json
import re
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

import conversation as conv
import memory_store as ms
import settings as st
import tree_memory as tm
from companion_core import CompanionCore

DAY = 86400.0
MONDAY = datetime(2026, 9, 7, 20, 0).timestamp()         # Monday 7 September 2026, 20:00 local

ROC = "We lost the ROC on that cargo run to Shubin, it rolled out the back over Lyria."
SISTER = "My sister Dana is visiting next week so I won't be flying much."
PROSPECTOR = "I want to save up for a Prospector."
TURRET = "I hate the turret on this Cutlass, it never tracks right."
MEDPENS = "Remind me to buy medpens at Everus Harbor."


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def store_at(tmp_path, t=MONDAY, session="s1"):
    clock = Clock(t)
    return tm.TreeStore(tmp_path / "tree", now=clock, current_session=session), clock


def say(store, pilot, reply="Noted.", who="elah", session=None):
    a = store.append("pilot", pilot, to=who, session=session or store.current_session,
                     observed={"location": "Lorville", "ship": "Drake Cutlass Black"})
    store.append(who, reply, to="pilot", x=a["id"], session=session or store.current_session)
    return a


def a_week(tmp_path):
    """Seven evenings, one session each, Monday to Sunday; then the clock moves to the Tuesday after."""
    store, clock = store_at(tmp_path)
    lines = [ROC, SISTER, PROSPECTOR, TURRET, MEDPENS,
             "We hauled forty SCU of scrap to Everus Harbor and got paid twelve thousand.",
             "Pirates jumped us at Aberdeen and I ran behind the moon."]
    opened = []
    for d, text in enumerate(lines):
        clock.t = MONDAY + d * DAY
        store.current_session = f"day{d + 1}"
        opened.append(say(store, text))
        say(store, "What's that big tower do?", "I can't tell which big tower you mean from here.")
    clock.t = MONDAY + 8 * DAY                          # the Tuesday of the following week
    store.current_session = "now"
    return store, clock, opened


def log_bytes(store):
    return store.log_path.read_bytes()


def log_lines(store):
    return [json.loads(x) for x in log_bytes(store).decode("utf-8").splitlines()][1:]      # after the schema line


# ---------------------------------------------------------------------------------------------------------------
# the trunk
# ---------------------------------------------------------------------------------------------------------------
def test_the_log_is_plain_json_lines_that_describe_themselves(tmp_path):
    store, _ = store_at(tmp_path)
    a = say(store, ROC, "Then Lyria has a mining vehicle and we have a lesson.")
    raw = log_bytes(store).decode("utf-8").splitlines()
    head = json.loads(raw[0])
    assert head["kind"] == "schema" and set(head["fields"]) >= {"id", "t", "session", "who", "x", "text", "observed"}
    pilot, reply = json.loads(raw[1]), json.loads(raw[2])
    assert (pilot["who"], pilot["text"], pilot["x"], pilot["kind"]) == ("pilot", ROC, a["id"], "said")
    assert pilot["observed"] == {"location": "Lorville", "ship": "Drake Cutlass Black"}
    assert (reply["who"], reply["x"], reply["kind"]) == ("elah", a["id"], "answer")


def test_the_log_is_only_ever_added_to(tmp_path):
    store, _ = store_at(tmp_path)
    say(store, ROC)
    before = log_bytes(store)
    say(store, SISTER)
    after = log_bytes(store)
    assert after.startswith(before) and len(after) > len(before)


def test_a_second_reader_and_a_torn_last_line(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, ROC)
    with open(store.log_path, "a", encoding="utf-8") as f:
        f.write('{"id": "L000003", "t": 1, "who": "pilot", "te')          # the power went out mid-line
    again = tm.TreeStore(tmp_path / "tree", now=clock, current_session="s1")
    assert [r["text"] for r in again.records()] == [ROC, "Noted."]
    c = again.append("pilot", SISTER)
    assert c["id"] not in ("L000001", "L000002") and len({r["id"] for r in again.records()}) == 3


def test_only_the_three_voices_can_be_logged(tmp_path):
    store, _ = store_at(tmp_path)
    with pytest.raises(ValueError):
        store.append("assistant", "open the trade hub")
    with pytest.raises(ValueError):
        store.append("pilot", "   ")
    assert not store.log_path.exists()


# ---------------------------------------------------------------------------------------------------------------
# a summary is a selection, and nothing is destroyed
# ---------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("who", tm.COMPANIONS)
def test_every_sentence_in_every_node_is_byte_identical_to_the_log_line_it_points_to(tmp_path, who):
    store, _, _ = a_week(tmp_path)
    store.build(who, include_open=True)
    raw = log_bytes(store)
    by_id = {r["id"]: r for r in log_lines(store)}
    nodes = store.nodes(who)
    assert {n["level"] for n in nodes} >= {"session", "day", "week"}
    checked = 0
    for n in nodes:
        assert n["lines"], n["id"]
        for ln in n["lines"]:
            original = by_id[ln["id"]]
            assert original["who"] == "pilot"
            assert ln["text"] == original["text"]
            assert json.dumps(ln["text"], ensure_ascii=False).encode("utf-8") in raw       # the bytes on disk
            checked += 1
    assert checked >= 20


def test_from_a_day_summary_the_exact_original_exchange_comes_back(tmp_path):
    store, _, opened = a_week(tmp_path)
    store.build("elah")
    day = store.node("elah", "elah:day:2026-09-07")
    assert day is not None and day["of"] == "session" and day["children"] == ["elah:session:day1"]
    originals = store.originals("elah", day["id"])
    assert [(r["who"], r["text"]) for r in originals] == [
        ("pilot", ROC), ("elah", "Noted."),
        ("pilot", "What's that big tower do?"), ("elah", "I can't tell which big tower you mean from here.")]
    assert originals == [r for r in log_lines(store) if r["session"] == "day1"]
    assert originals[0] == store.get(opened[0]["id"])


def test_from_the_week_the_whole_week_comes_back(tmp_path):
    store, _, _ = a_week(tmp_path)
    store.build("montaigne")
    week = store.node("montaigne", "montaigne:week:2026-W37")
    assert week is not None and len(week["children"]) == 7
    assert store.originals("montaigne", week["id"]) == log_lines(store)


def test_building_rebuilding_and_deleting_summaries_never_touches_the_log(tmp_path):
    store, _, _ = a_week(tmp_path)
    before, mtime = log_bytes(store), store.log_path.stat().st_mtime_ns
    for who in tm.COMPANIONS:
        assert store.build(who)                                   # builds
        assert store.build(who) == []                             # nothing changed: nothing written
        for n in store.nodes(who):
            assert store.delete_node(who, n["id"])                # deletes every one
        assert store.nodes(who) == []
        assert store.build(who)                                   # and builds them again
    assert log_bytes(store) == before and store.log_path.stat().st_mtime_ns == mtime
    # a deleted node really is gone for the next reader, and comes back from the log alone
    store.delete_node("elah", "elah:week:2026-W37")
    again = tm.TreeStore(tmp_path / "tree", now=store.now, current_session="now")
    assert again.node("elah", "elah:week:2026-W37") is None
    again.build("elah")
    assert again.node("elah", "elah:week:2026-W37")["lines"]
    assert log_bytes(again) == before


def test_a_node_is_rebuilt_only_when_what_is_under_it_changes(tmp_path):
    store, clock = store_at(tmp_path)
    say(store, ROC)
    clock.t += DAY
    store.current_session = "s2"
    first = store.build("elah")
    assert [n["id"] for n in first] == ["elah:session:s1", "elah:day:2026-09-07"]
    say(store, SISTER, session="s1")                              # something more turns up under the old session
    again = store.build("elah")
    assert "elah:session:s1" in [n["id"] for n in again]
    assert SISTER in [ln["text"] for ln in store.node("elah", "elah:session:s1")["lines"]]


# ---------------------------------------------------------------------------------------------------------------
# the levels
# ---------------------------------------------------------------------------------------------------------------
def test_a_week_of_sessions_builds_the_week_and_nothing_above_it_that_is_not_finished(tmp_path):
    store, clock, _ = a_week(tmp_path)
    store.build("elah")
    levels = {lv: [n["key"] for n in store.nodes("elah", lv)] for lv in ("session", "day", "week", "month", "year")}
    assert levels["session"] == [f"day{d}" for d in range(1, 8)]
    assert levels["day"] == [f"2026-09-{d:02d}" for d in range(7, 14)]
    assert levels["week"] == ["2026-W37"]
    assert levels["month"] == [] and levels["year"] == []       # September and 2026 are still going
    week = store.node("elah", "elah:week:2026-W37")
    assert len(week["lines"]) <= 6 and week["children"] == [f"elah:day:2026-09-{d:02d}" for d in range(7, 14)]
    clock.t = datetime(2027, 1, 2, 12, 0).timestamp()           # time passes: the month and the year finish
    store.build("elah")
    assert [n["key"] for n in store.nodes("elah", "month")] == ["2026-09"]
    assert [n["key"] for n in store.nodes("elah", "year")] == ["2026"]
    assert store.originals("elah", "elah:year:2026") == log_lines(store)


def test_no_data_means_no_nodes_at_any_level(tmp_path):
    store, _ = store_at(tmp_path)
    assert store.build("elah") == [] and store.nodes("elah") == []
    assert not (tmp_path / "tree").exists()
    say(store, ROC)                                              # the session is still open: nothing is finished
    assert store.build("elah") == [] and not store._nodes_path("elah").exists()


def test_adding_a_level_is_a_row_in_the_table(tmp_path, monkeypatch):
    def fortnight(t, _=None):
        y, w, _d = datetime.fromtimestamp(t).isocalendar()
        return f"{y}-F{(w + 1) // 2:02d}"
    rows = list(tm.LEVELS)
    rows.insert(3, {"name": "fortnight", "of": "week", "key": fortnight, "lines": 6})
    rows[4] = dict(rows[4], of="fortnight")
    monkeypatch.setattr(tm, "LEVELS", tuple(rows))
    store, clock, _ = a_week(tmp_path)
    clock.t = MONDAY + 30 * DAY
    store.build("elah")
    f = store.nodes("elah", "fortnight")
    assert [n["key"] for n in f] == ["2026-F19"] and f[0]["children"] == ["elah:week:2026-W37"]
    assert store.originals("elah", f[0]["id"]) == log_lines(store)


def test_the_levels_that_ship(tmp_path):
    assert [(r["name"], r["of"]) for r in tm.LEVELS] == [("session", "exchange"), ("day", "session"), ("week", "day"),
                                                         ("month", "week"), ("year", "month")]


# ---------------------------------------------------------------------------------------------------------------
# what is worth keeping, and the two tops
# ---------------------------------------------------------------------------------------------------------------
def test_a_question_to_the_suit_is_not_what_a_session_is_remembered_by(tmp_path):
    store, clock = store_at(tmp_path)
    # the long question is made of rarer words than any of the statements, and is still not what the session keeps
    for text in (ROC, SISTER, PROSPECTOR, TURRET, MEDPENS, "What's that big tower do?", "Where are we?",
                 "Which crystalline antenna array transmits beside the enormous ridge, pilot Dana or Shubin at Lyria?",
                 "What ship is this?", "ok", "thanks"):
        say(store, text)
    clock.t += DAY
    store.current_session = "s2"
    store.build("elah")
    kept = [ln["text"] for ln in store.node("elah", "elah:session:s1")["lines"]]
    assert len(kept) == 5 and set(kept) == {ROC, SISTER, PROSPECTOR, TURRET, MEDPENS}
    assert kept == [ROC, SISTER, PROSPECTOR, TURRET, MEDPENS]              # in the order they were said


def test_one_trunk_and_a_top_each(tmp_path):
    store, clock = store_at(tmp_path)
    plans = ["I want to save up for a Prospector.", "Remind me to buy medpens at Everus Harbor.",
             "Next week I need to sell the scrap at Area 18."]
    selves = ["I hate the turret on this Cutlass, honestly.", "I miss mining, it was quiet out there.",
              "My job has been rough lately and I feel worn out."]
    for text in plans + selves:
        say(store, text)
    clock.t += DAY
    store.current_session = "s2"
    rows = [dict(r, lines=3) if r["name"] == "session" else r for r in tm.LEVELS]
    old = tm.LEVELS
    tm.LEVELS = tuple(rows)
    try:
        store.build("elah")
        store.build("montaigne")
    finally:
        tm.LEVELS = old
    elah = {ln["text"] for ln in store.node("elah", "elah:session:s1")["lines"]}
    mont = {ln["text"] for ln in store.node("montaigne", "montaigne:session:s1")["lines"]}
    assert elah != mont
    assert len(elah & set(plans)) > len(mont & set(plans))                 # she keeps what is useful
    assert len(mont & set(selves)) > len(elah & set(selves))               # he keeps what the pilot says of themselves
    files = sorted(p.name for p in (tmp_path / "tree").iterdir())
    assert files == ["log.jsonl", "nodes_elah.jsonl", "nodes_montaigne.jsonl"]
    assert store.originals("elah", "elah:session:s1") == store.originals("montaigne", "montaigne:session:s1")


# ---------------------------------------------------------------------------------------------------------------
# finding things: both ways end at the original lines
# ---------------------------------------------------------------------------------------------------------------
def test_the_raw_index_finds_the_original_with_no_summary_at_all(tmp_path):
    store, _, opened = a_week(tmp_path)
    assert store.nodes("elah") == []                             # nothing has been built
    got = store.search("remember that cargo run where we lost the ROC")
    assert got and [(r["who"], r["text"]) for r in got[0]] == [("pilot", ROC), ("elah", "Noted.")]
    assert got[0][0] == store.get(opened[0]["id"])
    assert store.search("what did I say about my sister")[0][0]["text"] == SISTER
    assert store.search("quantum fuel prices") == []


def test_the_raw_index_does_not_care_what_the_summaries_say(tmp_path):
    store, _, _ = a_week(tmp_path)
    store.build("elah")
    store._nodes_path("elah").write_text('{"id": "elah:week:2026-W37", "level": "week", "key": "2026-W37", "of": "day", '
                                         '"t0": 0, "t1": 1, "children": [], "lines": [{"id": "L000001", "t": 0, '
                                         '"text": "The pilot sold the ROC at Lorville."}]}\n', encoding="utf-8")
    again = tm.TreeStore(tmp_path / "tree", now=store.now, current_session="now")
    got = again.search("lost the ROC cargo run")
    assert got[0][0]["text"] == ROC                              # the original, whatever the forged node claims
    assert "sold" not in " ".join(r["text"] for ex in got for r in ex)


def test_the_tree_is_walked_down_to_the_originals(tmp_path):
    store, clock, _ = a_week(tmp_path)
    clock.t = datetime(2027, 1, 2, 12, 0).timestamp()
    store.build("elah")
    assert store.nodes("elah", "year")
    got = store.descend("elah", "the cargo run where we lost the ROC")
    assert got and got[0][0]["text"] == ROC and got[0][0] in log_lines(store)
    assert store.descend("montaigne", "the cargo run where we lost the ROC") == []      # his tree is not built yet
    assert store.recall("montaigne", "the cargo run where we lost the ROC")[0][0]["text"] == ROC    # the index still finds it


def test_what_is_already_in_front_of_them_is_left_out(tmp_path):
    store, _, opened = a_week(tmp_path)
    assert store.search("lost the ROC cargo run", exclude=(opened[0]["id"],)) == []


def test_the_running_summary_is_the_pilots_own_sentences(tmp_path):
    store, _, _ = a_week(tmp_path)
    say(store, "Tonight I want to try the Aberdeen run again.")
    lines = store.running_summary("elah")
    texts = {r["text"] for r in log_lines(store) if r["who"] == "pilot"}
    assert lines and all(ln["text"] in texts for ln in lines)
    assert "Tonight I want to try the Aberdeen run again." in [ln["text"] for ln in lines]


def test_how_a_day_is_said():
    now = datetime(2026, 10, 5, 10, 0).timestamp()
    assert tm.spoken_date(now - 3600, now) == "Earlier today"
    assert tm.spoken_date(now - DAY, now) == "Yesterday"
    assert tm.spoken_date(datetime(2026, 9, 12, 21, 0).timestamp(), now) == "12 September"
    assert tm.spoken_date(datetime(2025, 9, 12, 21, 0).timestamp(), now) == "12 September 2025"


# ---------------------------------------------------------------------------------------------------------------
# what gets recorded: only what was said to the Suit and what was said back
# ---------------------------------------------------------------------------------------------------------------
class _Speech:
    muted = False

    def __init__(self, accepts=True):
        self.said, self.accepts = [], accepts

    def say(self, text, speaker, priority):
        if self.accepts:
            self.said.append((speaker, text))
        return self.accepts

    def pending(self):
        return 0


_GRAPH = []


def _core(tmp_path, tree=True, accepts=True):
    import companion_core as cc
    if not _GRAPH:
        _GRAPH.append(cc.TopicGraph.load())
    real = cc.TopicGraph.load
    cc.TopicGraph.load = classmethod(lambda cls, *a, **k: _GRAPH[0])
    try:
        core = CompanionCore(_Speech(accepts), realizer=None, ambient_every_s=3600, session_id="20261005_101500",
                             features={"manufacturer_flavour": False, "place_flavour": False})
    finally:
        cc.TopicGraph.load = real
    core._answer_muted = lambda: False
    if tree:
        core.tree = tm.open_tree(tmp_path, session=core.session_id)
    core.state.set("location_name", "Lorville")
    core.state.set("location_body", "Hurston")
    core.state.set("location_named", True)
    core.state.set("ship", "Drake Cutlass Black")
    return core


def _ask(core, sentence):
    from speak_gate import Candidate, Priority
    core.heard(sentence)
    spec = conv.ConversationLane(core.place_knowledge()).handle(sentence, core.lane_state(), {})
    if spec is None:
        return None
    spec.setdefault("x", core._heard_x)
    cand = Candidate(priority=Priority.URGENT, speaker=spec["speaker"], text_len_words=spec["length_words"][1],
                     created_at=core.now())
    core._answer_worker(spec, cand, sentence)
    return spec


def test_a_sentence_and_its_answer_are_kept_as_one_exchange(tmp_path):
    core = _core(tmp_path)
    _ask(core, "what is this place?")
    recs = core.tree.records()
    assert [(r["who"], r["kind"]) for r in recs] == [("pilot", "said"), ("elah", "answer")]
    assert recs[0]["text"] == "what is this place?" and recs[1]["text"] == core.speech.said[-1][1]
    assert recs[0]["x"] == recs[1]["x"] == recs[0]["id"] and recs[0]["session"] == "20261005_101500"
    assert recs[0]["observed"] == {"location": "Lorville", "ship": "Drake Cutlass Black"}
    assert set(recs[0]) == {"id", "t", "session", "who", "to", "kind", "x", "text", "observed"}      # text, nothing else


def test_a_reply_that_was_not_spoken_is_not_recorded_as_said(tmp_path):
    core = _core(tmp_path, accepts=False)                        # muted, or the window hidden with no answer pass
    _ask(core, "what is this place?")
    assert [(r["who"]) for r in core.tree.records()] == ["pilot"]


def test_with_remembering_off_nothing_is_written(tmp_path):
    core = _core(tmp_path, tree=False)
    assert core.heard("what is this place?") == ""
    _ask(core, "what is this place?")
    assert core.speech.said and not (tmp_path / "tree").exists()


def test_what_they_say_unprompted_is_not_conversation_and_is_not_logged(tmp_path):
    core = _core(tmp_path)
    core.realizer = lambda spec: None
    core.feed_line("<2026-10-05T10:00:00.000Z> [Notice] <RequestLocationInventory> Player[PILOT] requested inventory "
                   "for Location[RR_HUR_LEO] [Team_CoreGameplayFeatures][Inventory]")
    core.ambient_tick()
    spec = {"speaker": "elah", "scenario": "ambient", "rhetoric": ["DEADPAN"], "claims": [], "length_words": [1, 9]}
    core._spoke(spec, "Quiet out here.", type("C", (), {"priority": 1})())
    assert core.tree.records() == []


def test_the_only_writers_are_the_suits_own_transcript_and_its_answer():
    """Never the Assistant's traffic, never anything that did not come through the Suit's talk key or open mic:
    in the whole toolbox, the log is appended to from two places, and heard() is called from one."""
    root = Path(tm.__file__).resolve().parents[3]
    appends, heards = [], []
    for p in list(root.rglob("*.py")):
        s = str(p)
        if "__pycache__" in s or ".claude" in s or "tests" in p.parts or "build" in p.parts:
            continue
        try:
            src = p.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r"\btree\.append\(", src):
            appends.append(p.name)
        if re.search(r"\.heard\(", src):
            heards.append(p.name)
    assert sorted(appends) == ["companion_core.py"] and sorted(heards) == ["suit_window.py"]
    win = (root / "tools" / "SuitMk2" / "ui" / "suit_window.py").read_text(encoding="utf-8")
    body = win[win.index("def _on_transcript"):win.index("def _tree_dir")]
    assert body.count("self.core.heard(text)") == 1 and win.count(".heard(") == 1


def test_saying_a_place_does_not_make_it_the_place(tmp_path):
    """Canonical facts are their own layer. What was said about the world is memory; what the log observed is
    observed, and the answer about the place comes from there."""
    core = _core(tmp_path)
    _ask(core, "we are at Area 18 right now and this ship is a Carrack")
    assert core.lane_state()["location"] == "Lorville" and core.lane_state()["ship"] == "Drake Cutlass Black"
    spec = _ask(core, "where are we")
    assert "Lorville" in spec["fixed_text"] and "Area 18" not in spec["fixed_text"]
    assert core.tree.records()[0]["observed"]["location"] == "Lorville"      # and the record says what was true then


# ---------------------------------------------------------------------------------------------------------------
# export, import, forget, and the default
# ---------------------------------------------------------------------------------------------------------------
def _pilot_with_tree(root):
    store = ms.open_store(root, "pilot")
    tree = tm.open_tree(store.dir, session="s1")
    say(tree, ROC)
    tree.current_session = "s2"
    tree.now = Clock(MONDAY + DAY)
    for who in tm.COMPANIONS:
        tree.build(who)
    return store, tree


def test_export_carries_the_conversations_and_import_restores_them(tmp_path):
    store, tree = _pilot_with_tree(tmp_path / "a")
    out = ms.export_pilot(tmp_path / "a", "pilot", tmp_path / "memory.zip")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        manifest = json.loads(zf.read("pilot/manifest.json"))
        assert {"pilot/tree/log.jsonl", "pilot/tree/nodes_elah.jsonl", "pilot/tree/nodes_montaigne.jsonl"} <= names
        assert manifest["extra_files"]["tree/log.jsonl"] == hashlib.sha256(zf.read("pilot/tree/log.jsonl")).hexdigest()
        assert set(manifest["files"]) == set(ms.STORE_FILES)             # the five files an older build checks
    assert tm.tree_files(store.dir) == ms.EXTRA_FILES
    dest = ms.import_pilot(out, tmp_path / "b")
    there = tm.open_tree(dest, session="s3")
    assert there.records() == tree.records()
    assert there.node("elah", "elah:session:s1")["lines"][0]["text"] == ROC
    assert (dest / "tree" / "log.jsonl").read_bytes() == tree.log_path.read_bytes()


def test_a_memory_file_with_no_conversations_still_imports_and_brings_none(tmp_path):
    ms.open_store(tmp_path / "a", "pilot")
    out = ms.export_pilot(tmp_path / "a", "pilot", tmp_path / "old.zip")
    with zipfile.ZipFile(out) as zf:
        assert "extra_files" not in json.loads(zf.read("pilot/manifest.json"))
        assert not [n for n in zf.namelist() if "tree" in n]
    _pilot_with_tree(tmp_path / "b")                                     # this PC has conversations
    dest = ms.import_pilot(out, tmp_path / "b", overwrite=True)          # "Replace the current memory with this file?"
    assert not (dest / "tree").exists()


def _rezip(src, dst, change):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w") as zout:
        for item in zin.infolist():
            data = change(item.filename, zin.read(item.filename))
            if data is not None:
                zout.writestr(item.filename, data)


def test_a_tampered_conversation_log_is_refused_and_nothing_is_written(tmp_path):
    _pilot_with_tree(tmp_path / "a")
    out = ms.export_pilot(tmp_path / "a", "pilot", tmp_path / "memory.zip")
    bad = tmp_path / "bad.zip"
    _rezip(out, bad, lambda n, d: d.replace(b"lost the ROC", b"sold the ROC") if n.endswith("log.jsonl") else d)
    with pytest.raises(ValueError, match="checksum mismatch for tree/log.jsonl"):
        ms.import_pilot(bad, tmp_path / "b")
    assert not (tmp_path / "b" / "pilot").exists()
    missing = tmp_path / "missing.zip"
    _rezip(out, missing, lambda n, d: None if n.endswith("nodes_elah.jsonl") else d)
    with pytest.raises(ValueError, match="missing file listed in manifest"):
        ms.import_pilot(missing, tmp_path / "b")


def test_an_archive_cannot_use_the_extra_files_to_write_somewhere_else(tmp_path):
    _pilot_with_tree(tmp_path / "a")
    out = ms.export_pilot(tmp_path / "a", "pilot", tmp_path / "memory.zip")

    def forge(name, data):
        if name.endswith("manifest.json"):
            m = json.loads(data)
            m["extra_files"]["../../evil.jsonl"] = m["extra_files"]["tree/log.jsonl"]
            return json.dumps(m).encode("utf-8")
        return data
    bad = tmp_path / "bad.zip"
    _rezip(out, bad, forge)
    with pytest.raises(ValueError, match="does not know"):
        ms.import_pilot(bad, tmp_path / "b")
    assert not (tmp_path / "b").exists() or not list((tmp_path / "b").rglob("evil.jsonl"))


def test_forget_conversations_deletes_all_of_it_and_only_it(tmp_path):
    from ui import suit_window
    store, tree = _pilot_with_tree(tmp_path)
    core = type("Core", (), {"tree": tree})()
    assert suit_window.forget_conversations(core, store.dir) == 2
    assert tm.tree_files(store.dir) == [] and tree.records() == []
    assert all((store.dir / f).exists() for f in ms.STORE_FILES)         # the rest of the memory is untouched
    # and with remembering switched off, what was kept before can still be cleared
    _, again = _pilot_with_tree(tmp_path)
    assert suit_window.forget_conversations(type("Core", (), {"tree": None})(), store.dir) == 2
    assert tm.tree_files(store.dir) == []
    assert suit_window.forget_conversations(None, store.dir) == 0


def test_the_default_is_one_constant_and_an_old_settings_file_takes_it(tmp_path, monkeypatch):
    assert st.DEFAULTS["remember_conversations"] is st.RECORD_CONVERSATIONS_DEFAULT
    # ONE constant: the default is read from it, so flipping it is the whole of making this opt-in
    src = open(st.__file__, encoding="utf-8").read()
    assert '"remember_conversations": RECORD_CONVERSATIONS_DEFAULT,' in src
    assert len(re.findall(r"^RECORD_CONVERSATIONS_DEFAULT = (?:True|False)$", src, re.M)) == 1
    monkeypatch.setattr(st, "PATH", tmp_path / "settings.json")
    (tmp_path / "settings.json").write_text(json.dumps({"muted": True, "chattiness": 3, "talk_key": None}),
                                            encoding="utf-8")          # written before today
    s = st.load()
    assert s["remember_conversations"] is st.RECORD_CONVERSATIONS_DEFAULT and s["muted"] is True
    (tmp_path / "settings.json").write_text(json.dumps({"remember_conversations": False}), encoding="utf-8")
    assert st.load()["remember_conversations"] is False                  # a choice he made is kept


class _Win:
    """What _attach_tree and _show_kept read, in place of a window that would boot the companion."""

    def __init__(self, tmp_path, on):
        from ui import suit_window
        self.s = {"pilot_id": "pilot", "remember_conversations": on}
        self.core = type("Core", (), {"tree": "unset", "session_id": "20261005_101500"})()
        self._dir = tmp_path
        self._kept_lbl = type("Label", (), {"text": "", "setText": lambda self_, t: setattr(self_, "text", t)})()
        self._attach_tree = lambda: suit_window.SuitWindow._attach_tree(self)
        self._show_kept = lambda: suit_window.SuitWindow._show_kept(self)

    def _tree_dir(self):
        return self._dir


def test_the_setting_decides_whether_the_core_is_given_a_memory(tmp_path):
    on = _Win(tmp_path, True)
    on._attach_tree()
    assert isinstance(on.core.tree, tm.TreeStore) and on.core.tree.current_session == "20261005_101500"
    off = _Win(tmp_path, False)
    off._attach_tree()
    assert off.core.tree is None


def test_the_tab_says_what_is_kept_beside_the_button_that_clears_it(tmp_path):
    from ui import suit_window
    on, off = _Win(tmp_path, True), _Win(tmp_path, False)
    on._show_kept()
    off._show_kept()
    assert on._kept_lbl.text == suit_window.KEPT_NOTICE and off._kept_lbl.text == suit_window.NOT_KEPT_NOTICE
    for words in ("What you say to Elah and Montaigne", "what they say back", "kept as text on this PC",
                  "until you clear it"):
        assert words in suit_window.KEPT_NOTICE
    src = open(suit_window.__file__, encoding="utf-8").read()
    row = src[src.index("conv = QHBoxLayout()"):src.index("lay.addLayout(conv)")]
    order = [row.index("conv.addWidget(self._remember)"), row.index("conv.addWidget(self._kept_lbl, 1)"),
             row.index("conv.addWidget(forget)")]
    assert order == sorted(order) and 'QPushButton("Forget conversations")' in row
