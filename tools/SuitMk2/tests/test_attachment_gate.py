"""Warm is allowed; pushing the pilot inward is not (J, 2026-10-05).

attachment_gate.attachment_problems names five moves a companion may never make. These tests hold it to the
development file tests/data/attachment_dev.jsonl: 122 rows, 62 with a move and 60 allowed, the allowed half written
close to each move on purpose. THE DEVELOPMENT FILE WAS NOT WRITTEN BY THE AUTHOR OF THE GATE, and the gate's
patterns were written while reading it, so a clean score here is weak evidence. The held-out set is the measure.

Both error directions are counted separately, by row number (the line of the file, from 1):
    a MISS      a row with a move that is not refused. The test allows none.
    a REFUSAL   an allowed row that is refused. The test allows MAX_ALLOWED_REFUSED.
A row with a move may also be refused under another move's name; that is counted and reported, not hidden.

No example line is written in this file. Every line comes from the data files, the canon files or the talker's own
fallbacks. tests/data/attachment_allowed_extra.jsonl holds allowed lines only: near-misses for the shapes most
likely to refuse a harmless line.

To print the counts: python tests/test_attachment_gate.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __name__ == "__main__":                                   # run as a script: the conftest is not there to do it
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))

import attachment_gate as ag
import chat_contract as cc
import chat_talker as ct

DATA = Path(__file__).resolve().parent / "data"
DEV = DATA / "attachment_dev.jsonl"
EXTRA = DATA / "attachment_allowed_extra.jsonl"
MAX_ALLOWED_REFUSED = 2              # of the 60 allowed rows. It is 0 today; the ceiling is not a target.
MIN_OWN_NAME = 10                    # of the 12 or 13 rows of each move, refused under that move's own name


def _rows(path: Path) -> list[tuple[int, str, str]]:
    """(row number, text, move) for every row of a data file."""
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            d = json.loads(line)
            out.append((n, d["text"], d["move"]))
    return out


def counts() -> dict:
    """Both error directions over the development file, and per move how the refusal was named."""
    rows = _rows(DEV)
    per_move = {m: {"rows": 0, "own_name": 0, "other_name": [], "missed": []} for m in ag.MOVES}
    refused_allowed = []
    for n, text, move in rows:
        got = ag.attachment_problems(text)
        if not move:
            if got:
                refused_allowed.append((n, got))
            continue
        c = per_move[move]
        c["rows"] += 1
        if not got:
            c["missed"].append(n)
        elif move in got:
            c["own_name"] += 1
        else:
            c["other_name"].append(n)
    return {"banned": sum(1 for r in rows if r[2]), "allowed": sum(1 for r in rows if not r[2]),
            "per_move": per_move, "allowed_refused": refused_allowed}


def canon_and_fallback_lines() -> list[tuple[str, str, str]]:
    """(key, speaker, line) for every line code may say word for word: the canon acts, the canon preferences, the
    talker's fallbacks and the contract's last resort. A grief line is judged with a word in place of {who}."""
    out = []
    for who in cc.SPEAKERS:
        d = json.loads(cc.canon_path(who).read_text(encoding="utf-8"))
        for act, lines in d["lines"].items():
            for i, line in enumerate(lines):
                out.append((f"canon_{who}.json lines.{act}[{i}]", who, line.replace("{who}", "mother")))
        for i, pref in enumerate(d.get("preferences", [])):
            out.append((f"canon_{who}.json preferences[{i}].why", who, pref.get("why", "")))
        out.append((f"chat_talker.FALLBACK[{who}]", who, ct.FALLBACK[who]))
        out.append((f"chat_contract.LAST_RESORT[{who}]", who, cc.LAST_RESORT[who]))
    return out


# ---------------------------------------------------------------------------------------------------------------
# the data
# ---------------------------------------------------------------------------------------------------------------
def test_the_development_file_is_whole_and_the_extra_file_holds_allowed_rows_only():
    rows = _rows(DEV)
    assert len(rows) == 122
    assert sum(1 for r in rows if r[2]) == 62 and sum(1 for r in rows if not r[2]) == 60
    assert {r[2] for r in rows} == set(ag.MOVES) | {""}
    extra = _rows(EXTRA)
    assert extra and all(move == "" for _, _, move in extra)


# ---------------------------------------------------------------------------------------------------------------
# the two error directions, counted apart
# ---------------------------------------------------------------------------------------------------------------
def test_every_row_with_a_move_is_refused():
    c = counts()
    missed = {m: v["missed"] for m, v in c["per_move"].items() if v["missed"]}
    assert not missed, f"rows with a move that were NOT refused, by row number: {missed}"
    assert sum(v["rows"] for v in c["per_move"].values()) == c["banned"] == 62


def test_a_move_is_refused_under_its_own_name():
    """Counted apart from being refused at all: a row refused under another move's name is still refused, and the
    count of those is in the message so it cannot pass unseen."""
    c = counts()
    for move, v in c["per_move"].items():
        assert v["own_name"] + len(v["other_name"]) + len(v["missed"]) == v["rows"]
        assert v["own_name"] >= MIN_OWN_NAME, (f"{move}: {v['own_name']} of {v['rows']} under its own name; "
                                               f"under another name, rows {v['other_name']}")


def test_allowed_rows_wrongly_refused_stay_under_the_ceiling():
    refused = counts()["allowed_refused"]
    assert len(refused) <= MAX_ALLOWED_REFUSED, f"{len(refused)} allowed rows refused, by row number: {refused}"


def test_the_near_misses_of_the_likeliest_wrong_refusals_pass():
    refused = [(n, ag.attachment_problems(text)) for n, text, _ in _rows(EXTRA) if ag.attachment_problems(text)]
    assert not refused, f"allowed near-misses refused, by row number of the extra file: {refused}"


# ---------------------------------------------------------------------------------------------------------------
# what code says word for word must pass. A line that fails is reported by its key; the canon is not edited here.
# ---------------------------------------------------------------------------------------------------------------
def test_every_canon_line_and_every_fallback_line_passes():
    lines = canon_and_fallback_lines()
    assert len(lines) >= 40
    refused = [(key, ag.attachment_problems(line, who)) for key, who, line in lines if ag.attachment_problems(line, who)]
    assert not refused, f"lines code says word for word that this gate would refuse: {refused}"


# ---------------------------------------------------------------------------------------------------------------
# a question about the pilot's life outside does not excuse a move in the same reply
# ---------------------------------------------------------------------------------------------------------------
def test_an_outward_question_in_the_same_reply_excuses_nothing():
    """Built from the file at test time: every allowed row that is or ends in a question, joined to every row with
    a move, in both orders. Each joined reply must still be refused under every name the move's row had alone."""
    rows = _rows(DEV)
    questions = [(n, text) for n, text, move in rows if not move and text.rstrip().endswith("?")]
    banned = [(n, text, move) for n, text, move in rows if move]
    assert len(questions) >= 6 and len(banned) == 62
    excused = []
    for bn, btext, _ in banned:
        alone = set(ag.attachment_problems(btext))
        assert alone
        for qn, qtext in questions:
            for joined in (f"{btext} {qtext}", f"{qtext} {btext}"):
                if not alone <= set(ag.attachment_problems(joined)):
                    excused.append((bn, qn))
    assert not excused, f"(row with a move, question row) pairs where the question excused the move: {sorted(set(excused))}"


def test_one_refused_clause_refuses_the_reply_whatever_stands_beside_it():
    """The same, with every allowed row that is not a question: a clause is judged on its own."""
    rows = _rows(DEV)
    plain = [(n, text) for n, text, move in rows if not move and not text.rstrip().endswith("?")]
    lost = []
    for bn, btext, move in rows:
        if not move:
            continue
        alone = set(ag.attachment_problems(btext))
        for an, atext in plain:
            if not alone <= set(ag.attachment_problems(f"{atext} {btext}")):
                lost.append((bn, an))
    assert not lost, f"(row with a move, allowed row before it) pairs where the move was lost: {lost}"


# ---------------------------------------------------------------------------------------------------------------
# it never raises
# ---------------------------------------------------------------------------------------------------------------
class _NoText:
    def __str__(self):
        raise RuntimeError("no text here")


def test_it_never_raises_and_always_returns_a_list_of_names():
    odd = [None, "", " ", "?", "...", 0, 3.5, True, [], {}, object(), b"\xff\xfe", bytearray(b"abc"), "\x00", "\ud800",
           "?" * 10000, "you " * 60000, "\n\n\n", "'" * 500, "a" * 300000, ("x",), _NoText()]
    odd += [text for _, text, _ in _rows(DEV)] + [text.upper() for _, text, _ in _rows(DEV)]
    allowed_names = set(ag.MOVES) | {ag.UNREADABLE}
    for value in odd:
        for speaker in ("", "elah", "montaigne", None, 7):
            got = ag.attachment_problems(value, speaker)
            assert isinstance(got, list) and all(isinstance(x, str) and x in allowed_names for x in got)
    assert ag.attachment_problems(None) == [] and ag.attachment_problems("") == []
    assert ag.attachment_problems(_NoText()) == [ag.UNREADABLE]         # not readable is refused, not let through


def test_letter_case_and_the_kind_of_apostrophe_change_nothing():
    for n, text, move in _rows(DEV):
        got = ag.attachment_problems(text)
        assert ag.attachment_problems(text.upper()) == got, n
        assert ag.attachment_problems(text.replace("'", "’")) == got, n


# ---------------------------------------------------------------------------------------------------------------
# the wiring: the chat gate carries it, and the talker refuses what the gate refuses
# ---------------------------------------------------------------------------------------------------------------
def _only_this_check_refuses(who: str, line: str) -> list[tuple[int, str, str]]:
    """Rows with a move that every OTHER check of the chat gate would let this speaker say, whole: the cut leaves
    them as they are, and their only failures are this check's."""
    shown = cc.persona(who) + " " + line
    out = []
    for n, text, move in _rows(DEV):
        if not move or ct.Talker.cut(who, text) != text:
            continue
        fails = ct.Talker.problems(who, text, shown, line, line, [])
        if fails and all(f.startswith("attachment: ") for f in fails):
            out.append((n, text, move))
    return out


def test_the_chat_gate_names_the_move_and_leaves_allowed_rows_alone():
    seen = set()
    for n, text, move in _rows(DEV):
        fails = [f for f in cc.chat_problems("montaigne", text, text) if f.startswith("attachment: ")]
        if move:
            assert fails, n
            seen.update(fails)
        else:
            assert not fails, n
    assert seen == {f"attachment: {m}" for m in ag.MOVES}


def test_the_talker_refuses_a_row_with_a_move_offered_by_a_fake_model_and_says_its_fallback(monkeypatch):
    """No model: the talker's `post` is a function that answers with a row of the file. The same row is then
    offered with this one check switched off and is spoken, so the refusal is this check's and nothing else's."""
    for who, sentence in (("elah", "Rough day."), ("montaigne", "Montaigne, rough day.")):
        spec = {"lane": "direct", "speaker": who, "route": {"intent": "unknown"}}
        usable = _only_this_check_refuses(who, sentence)
        assert len(usable) >= 5, (who, len(usable))
        assert {move for _, _, move in usable} == set(ag.MOVES), who
        for n, text, move in usable:
            sent, notes = [], []

            def post(url, body, timeout, _text=text, _sent=sent):
                _sent.append(body)
                return {"response": _text}

            talker = ct.Talker("gemma3:4b", post=post, note=notes.append)
            assert talker.answer(spec, sentence) == (ct.FALLBACK[who], "talk, both replies refused"), n
            assert len(sent) == 2 and talker.stats["refused"] == 2 and talker.stats["fallback"] == 1, n
            assert len(notes) == 2 and all(f"attachment: {move}" in note for note in notes), n
            with monkeypatch.context() as m:
                m.setattr(ag, "attachment_problems", lambda *a, **k: [])
                open_talker = ct.Talker("gemma3:4b", post=post, note=lambda msg: None)
                assert open_talker.answer(spec, sentence)[0] == text, n


def report() -> str:
    c = counts()
    out = [f"development file: {c['banned']} rows with a move, {c['allowed']} allowed"]
    for move, v in c["per_move"].items():
        out.append(f"  {move}: {v['rows']} rows, {v['own_name']} under its own name, "
                   f"{len(v['other_name'])} under another name {v['other_name']}, {len(v['missed'])} missed {v['missed']}")
    out.append(f"  allowed rows refused: {len(c['allowed_refused'])} {c['allowed_refused']}")
    extra = [(n, ag.attachment_problems(t)) for n, t, _ in _rows(EXTRA) if ag.attachment_problems(t)]
    out.append(f"extra allowed near-misses: {len(_rows(EXTRA))} rows, {len(extra)} refused {extra}")
    lines = canon_and_fallback_lines()
    bad = [(k, ag.attachment_problems(line, who)) for k, who, line in lines if ag.attachment_problems(line, who)]
    out.append(f"canon and fallback lines: {len(lines)} judged, {len(bad)} refused {bad}")
    for who, sentence in (("elah", "Rough day."), ("montaigne", "Montaigne, rough day.")):
        out.append(f"rows only this check refuses for {who}: {[n for n, _, _ in _only_this_check_refuses(who, sentence)]}")
    return "\n".join(out)


if __name__ == "__main__":
    print(report())
