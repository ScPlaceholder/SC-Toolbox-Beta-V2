"""tree_memory.py - CHRISTMAS TREE STORAGE: the conversation memory of Suit Mk2.

A permanent log on disk is the trunk; above it a hierarchy that gets coarser with
distance from now (exchange -> session -> day -> week -> month -> year); the model sits at the star and is handed
a small assembled view. The rule for the whole thing: compression changes accessibility, not existence.

WHAT IS ON DISK, under <memory root>/<pilot>/tree/ :

    log.jsonl               THE TRUNK. One line per sentence the pilot said to the Suit and per line said back.
                            Appended and fsynced, never rewritten. Shared by both companions, and plain JSON lines
                            so another tool of the toolbox can read it (the first line describes the fields).
    nodes_elah.jsonl        Each companion's own UPPER tree: summary nodes for sessions, days, weeks, months and
    nodes_montaigne.jsonl   years. Append-only, the newest line for an id wins.

    One log line:  {"id": "L000042", "t": 1791201600.0, "session": "20261005_101500", "who": "pilot",
                    "to": "elah", "kind": "said", "x": "L000042", "text": "...", "observed": {"location": ...}}
    `x` is the EXCHANGE the line belongs to: the id of the pilot's line that opened it. A reply carries the same x.
    `observed` is what the trackers held at that moment (place, ship). It is a record of what was canonical THEN;
    nothing here is ever read back as a fact about the world. Conversation cannot overwrite an observed fact
    because nothing in this module is consulted for one.

A SUMMARY IS A SELECTION, NOT A PARAPHRASE. A node's text is the most telling of the ORIGINAL pilot sentences
beneath it, word for word, each with the id of the log line it came from. A day node selects from its sessions'
selections, a week from its days', and what a year node holds is still the pilot's own words. Measured:
a model summary of twenty exchanges took 10.8 s on the CPU at 1.5B and
was wrong twice, and 26 s at 4B and wrong once; a summary of THAT would be worse. A selection cannot drift,
needs no model, and costs milliseconds. "Most telling" is a fixed score (rare words, names and numbers, plans,
things the pilot asked to have remembered), weighted a little differently for each companion.

NOTHING IS DESTROYED. Every node keeps the ids of the children it was made from; originals() walks from any
node down to the exact log lines. Nodes live in their own files: building, rebuilding or deleting one never
opens the log for writing.

RETRIEVAL ends at ORIGINAL log lines, two ways:
    search()    a plain index over the raw log. Does not depend on any summary being right.
    descend()   from the top level down through the nodes, then into the originals.

LEVELS: every level above the session is one row of LEVELS and the same code. A level with no finished period
under it produces no node, so nothing empty exists; adding a level is a row and a test.

Selftest: python tree_memory.py --selftest
"""
from __future__ import annotations

import json
import math
import os
import re
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

SCHEMA = {"schema": "suitmk2.tree.log", "version": 1, "kind": "schema",
          "about": "Suit Mk2 conversation log (Christmas Tree Storage). One JSON object per line, append-only.",
          "fields": {"id": "L + six digits, in order", "t": "seconds since 1970", "session": "play session id",
                     "who": "pilot | elah | montaigne", "to": "who was addressed, if known",
                     "kind": "said (the pilot) | answer (a companion's reply)",
                     "x": "the exchange: id of the pilot's line that opened it", "text": "the words",
                     "observed": "what the Suit's trackers held then (place, ship); a record, never a source"}}
COMPANIONS = ("elah", "montaigne")
LOG_NAME = "log.jsonl"


def _session_key(t: float, session: str) -> str:
    return session


def _day_key(t: float, _=None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m-%d")


def _week_key(t: float, _=None) -> str:
    y, w, _d = datetime.fromtimestamp(t).isocalendar()
    return f"{y}-W{w:02d}"


def _month_key(t: float, _=None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y-%m")


def _year_key(t: float, _=None) -> str:
    return datetime.fromtimestamp(t).strftime("%Y")


# The hierarchy, bottom to top. name, what it is made of, how a child's start time names its period, and how many
# of the pilot's sentences a node of that level keeps. A node is built for every FINISHED period that has at
# least one child (and, on request, for the period still open). Adding a level is a row here and a test.
LEVELS = (
    {"name": "session", "of": "exchange", "key": _session_key, "lines": 5},
    {"name": "day", "of": "session", "key": _day_key, "lines": 6},
    {"name": "week", "of": "day", "key": _week_key, "lines": 6},
    {"name": "month", "of": "week", "key": _month_key, "lines": 6},
    {"name": "year", "of": "month", "key": _year_key, "lines": 8},
)
MAX_LINE_WORDS = 45          # a longer sentence is never selected into a node (it is still in the log)

_STOP = set("""a an the and or but so if then than that this these those there here it its i me my mine we us our you
your he him his she her they them their is are was were be been being am do does did done have has had will would
can could should may might must not no yes of to in on at for with by from as about into out up down over off again
what which who where when why how all any some just very too also really got get going go went well oh hey ok okay
uh um yeah like know think said say tell told thing things one want wanted lets let s t re ll ve d m dont im its
elah montaigne ship suit""".split())
_PLAN = re.compile(r"\b(?:i want to|i wanna|i need to|i(?:'| a)?m going to|i(?:'| wi)ll |next (?:week|time|month|session)|"
                   r"tomorrow|tonight|saving up|save up|plan(?:ning)? to|one day|someday)\b")
_REMEMBER = re.compile(r"\b(?:remind me|remember (?:that|this|to)|don'?t (?:let me )?forget|note that|for the record|"
                       r"keep in mind)\b")
_SELF = re.compile(r"\b(?:i (?:feel|felt|think|hate|love|miss|like|wish|hope|am|was)|i'm|my (?:\w+ )?(?:sister|brother|wife|"
                   r"husband|mum|mom|dad|kid|son|daughter|dog|cat|friend|job|boss|work))\b")
_QUESTION = re.compile(r"^(?:what|where|who|when|why|how|which|is|are|do|does|did|can|could|would|will|should)\b|\?\s*$")
# What each companion keeps hold of. The same sentences, weighed differently: she keeps what is useful, he keeps
# what the pilot says about themselves.
PROFILES = {
    "elah": {"plan": 2.0, "remember": 3.0, "self": 0.5, "name": 1.0},
    "montaigne": {"plan": 1.0, "remember": 3.0, "self": 2.5, "name": 0.5},
}


def words(text: str) -> list[str]:
    """Content words of a sentence, lightly stemmed: what the index and the scores work on."""
    out = []
    for w in re.findall(r"[a-z0-9]+", str(text).lower().replace("’", "'").replace("'", "")):
        if w in _STOP or len(w) < 3:
            continue
        for suf in ("ing", "ed", "es", "s"):
            if w.endswith(suf) and len(w) - len(suf) >= 3:
                w = w[: len(w) - len(suf)]
                break
        out.append(w)
    return out


class TreeStore:
    """The trunk, the two upper trees, and both ways of finding something in them. Thread-safe.
    current_session: the play session that is still open (its node is built only on request)."""

    def __init__(self, tree_dir: Path | str, now: Callable[[], float] = time.time, current_session: str = ""):
        self.dir = Path(tree_dir)
        self.now, self.current_session = now, current_session
        self.log_path = self.dir / LOG_NAME
        self._lock = threading.RLock()
        self._recs: Optional[list[dict]] = None            # the log, read once, then kept in step with append()
        self._by_id: dict[str, dict] = {}
        self._index: dict[str, set] = {}                    # word -> indexes into _recs (rebuilt from the log)
        self._nodes: dict[str, Optional[dict]] = {c: None for c in COMPANIONS}

    # -- the trunk ---------------------------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        if self._recs is None:
            recs = []
            if self.log_path.exists():
                with open(self.log_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            r = json.loads(line)
                        except ValueError:
                            continue                         # a torn last line after a crash: skipped, not fatal
                        if isinstance(r, dict) and r.get("kind") != "schema" and r.get("id") and "text" in r:
                            recs.append(r)
            self._recs = recs
            self._by_id = {r["id"]: r for r in recs}
            self._index = {}
            for i, r in enumerate(recs):
                for w in set(words(r["text"])):
                    self._index.setdefault(w, set()).add(i)
        return self._recs

    def append(self, who: str, text: str, to: str = "", kind: str = "", x: str = "", session: str = "",
               observed: Optional[dict] = None, t: Optional[float] = None) -> dict:
        """Write one line to the trunk and return it. x: the exchange it answers (empty for a pilot line, which
        opens its own). The file is only ever opened for append."""
        text = " ".join(str(text or "").split())
        if not text or who not in ("pilot",) + COMPANIONS:
            raise ValueError("a log line needs a speaker (pilot, elah or montaigne) and some text")
        with self._lock:
            recs = self._load()
            rid = f"L{len(recs) + 1:06d}"
            while rid in self._by_id:                       # ids stay unique even after a torn line was skipped
                rid = f"L{int(rid[1:]) + 1:06d}"
            rec = {"id": rid, "t": float(self.now() if t is None else t), "session": session or self.current_session,
                   "who": who, "to": to or "", "kind": kind or ("said" if who == "pilot" else "answer"),
                   "x": x or rid, "text": text}
            if observed:
                rec["observed"] = {k: v for k, v in observed.items() if v not in (None, "")}
            self.dir.mkdir(parents=True, exist_ok=True)
            new = not self.log_path.exists() or self.log_path.stat().st_size == 0
            with open(self.log_path, "a", encoding="utf-8", newline="\n") as f:
                if new:
                    f.write(json.dumps(SCHEMA, ensure_ascii=False) + "\n")
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
            recs.append(rec)
            self._by_id[rid] = rec
            for w in set(words(text)):
                self._index.setdefault(w, set()).add(len(recs) - 1)
            return rec

    def records(self) -> list[dict]:
        with self._lock:
            return list(self._load())

    def get(self, rid: str) -> Optional[dict]:
        with self._lock:
            self._load()
            return self._by_id.get(rid)

    def exchange(self, x: str) -> list[dict]:
        """Every log line of one exchange, in order: the pilot's sentence, then what was said back."""
        with self._lock:
            return [r for r in self._load() if r.get("x") == x]

    def _exchanges(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in self._load():
            out.setdefault(r.get("x") or r["id"], []).append(r)
        return out

    # -- the upper tree ----------------------------------------------------------------------------------------
    def _nodes_path(self, companion: str) -> Path:
        return self.dir / f"nodes_{companion}.jsonl"

    def _load_nodes(self, companion: str) -> dict:
        if companion not in COMPANIONS:
            raise ValueError(f"no such companion: {companion!r}")
        if self._nodes[companion] is None:
            nodes: dict = {}
            p = self._nodes_path(companion)
            if p.exists():
                with open(p, encoding="utf-8") as f:
                    for line in f:
                        try:
                            n = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(n, dict) and n.get("id"):
                            if n.get("deleted"):
                                nodes.pop(n["id"], None)
                            else:
                                nodes[n["id"]] = n
            self._nodes[companion] = nodes
        return self._nodes[companion]

    def _write_node(self, companion: str, node: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self._nodes_path(companion), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(node, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def nodes(self, companion: str, level: Optional[str] = None) -> list[dict]:
        with self._lock:
            ns = [n for n in self._load_nodes(companion).values() if level is None or n["level"] == level]
            return sorted(ns, key=lambda n: (n["t0"], n["id"]))

    def node(self, companion: str, nid: str) -> Optional[dict]:
        with self._lock:
            return self._load_nodes(companion).get(nid)

    def delete_node(self, companion: str, nid: str) -> bool:
        """Forget a summary. The log is not opened; the next build() makes the node again from what is under it."""
        with self._lock:
            nodes = self._load_nodes(companion)
            if nid not in nodes:
                return False
            del nodes[nid]
            self._write_node(companion, {"id": nid, "deleted": True, "made": self.now()})
            return True

    def _idf(self) -> Callable[[str], float]:
        n = max(1, sum(1 for r in self._load() if r["who"] == "pilot"))
        df: dict[str, int] = {}
        for r in self._load():
            if r["who"] == "pilot":
                for w in set(words(r["text"])):
                    df[w] = df.get(w, 0) + 1
        return lambda w: math.log(1.0 + n / (1.0 + df.get(w, 0)))

    def _score(self, companion: str, text: str, idf: Callable[[str], float]) -> float:
        ws = set(words(text))
        if len(ws) < 2 or len(text.split()) > MAX_LINE_WORDS:
            return 0.0
        p, low = PROFILES[companion], text.lower()
        s = sum(idf(w) for w in ws) / math.sqrt(len(ws))
        s += p["plan"] * bool(_PLAN.search(low)) + p["remember"] * bool(_REMEMBER.search(low))
        s += p["self"] * bool(_SELF.search(low))
        s += p["name"] * min(3, len(re.findall(r"\b\d[\d,.]*\b|(?<!^)(?<![.!?] )\b[A-Z][a-z]{2,}", text)))
        if _QUESTION.search(low):
            s *= 0.4                                      # a question to the Suit says little about the pilot
        return s

    def _select(self, companion: str, candidates: list[dict], keep: int, idf) -> list[dict]:
        """The `keep` most telling candidate lines, returned in the order they were said. Every one is an
        original pilot sentence with the id of its log line."""
        seen, scored = set(), []
        for c in candidates:
            key = " ".join(words(c["text"]))
            if key in seen:
                continue
            seen.add(key)
            s = self._score(companion, c["text"], idf)
            if s > 0:
                scored.append((s, c))
        top = sorted(scored, key=lambda sc: (-sc[0], sc[1]["t"], sc[1]["id"]))[:keep]
        return [{"id": c["id"], "t": c["t"], "text": c["text"]} for _, c in sorted(top, key=lambda sc: (sc[1]["t"], sc[1]["id"]))]

    def _finished(self, level: str, key: str, t_now: float) -> bool:
        if level == "session":
            return key != self.current_session
        row = next(r for r in LEVELS if r["name"] == level)
        return key != row["key"](t_now, None)

    def build(self, companion: str, include_open: bool = False) -> list[dict]:
        """Bring this companion's upper tree up to date: every finished period with something under it gets its
        node, bottom level first; include_open also builds the periods still running (for the running summary).
        A node whose children have not changed is left alone. Returns the nodes written. Never writes the log."""
        with self._lock:
            nodes = self._load_nodes(companion)
            idf = self._idf()
            t_now = self.now()
            written = []
            # the children of the lowest level are exchanges; of every other level, the nodes of the level below
            ex = self._exchanges()
            children: dict[str, list] = {}
            for x, lines in ex.items():
                pilot = [r for r in lines if r["who"] == "pilot"]
                if pilot:
                    children.setdefault(lines[0].get("session") or "", []).append(
                        {"id": x, "t0": lines[0]["t"], "t1": lines[-1]["t"], "lines": pilot})
            for row in LEVELS:
                level, made = row["name"], []
                for key, kids in sorted(children.items()):
                    kids = sorted(kids, key=lambda k: (k["t0"], k["id"]))
                    finished = self._finished(level, key, t_now)
                    if not kids or not (finished or include_open):
                        continue
                    nid = f"{companion}:{level}:{key}"
                    src = [k["id"] for k in kids]
                    old = nodes.get(nid)
                    if old is not None and old.get("children") == src and old.get("open") == (not finished):
                        made.append(old)
                        continue
                    cands = [ln for k in kids for ln in k["lines"]]
                    node = {"id": nid, "companion": companion, "level": level, "key": key, "of": row["of"],
                            "t0": kids[0]["t0"], "t1": max(k["t1"] for k in kids), "children": src,
                            "lines": self._select(companion, cands, row["lines"], idf),
                            "open": not finished, "made": t_now}
                    nodes[nid] = node
                    self._write_node(companion, node)
                    written.append(node)
                    made.append(node)
                # this level's nodes are the next level's children, grouped by the period their start falls in
                nxt = LEVELS[LEVELS.index(row) + 1] if row is not LEVELS[-1] else None
                children = {}
                if nxt is not None:
                    for n in made:
                        children.setdefault(nxt["key"](n["t0"], None), []).append(n)
            return written

    def originals(self, companion: str, nid: str) -> list[dict]:
        """Every ORIGINAL log line under a node, in order: walk its children down to the exchanges. This is the
        proof that a summary took nothing away."""
        with self._lock:
            nodes = self._load_nodes(companion)
            node = nodes.get(nid)
            if node is None:
                return []
            if node["of"] == "exchange":
                out = [r for x in node["children"] for r in self.exchange(x)]
            else:
                out = [r for cid in node["children"] for r in self.originals(companion, cid)]
            return sorted(out, key=lambda r: (r["t"], r["id"]))

    def running_summary(self, companion: str, max_lines: int = 4) -> list[dict]:
        """The pilot's own sentences that stand for 'so far': the newest day node and the newest session node."""
        with self._lock:
            self.build(companion, include_open=True)
            picked: list[dict] = []
            for level in ("day", "session"):
                ns = self.nodes(companion, level)
                if ns:
                    picked += ns[-1]["lines"]
            seen, out = set(), []
            for ln in sorted(picked, key=lambda ln: (ln["t"], ln["id"])):
                if ln["id"] not in seen:
                    seen.add(ln["id"])
                    out.append(ln)
            return out[-max_lines:]

    # -- retrieval: both ways end at original log lines ----------------------------------------------------------
    def search(self, text: str, k: int = 2, exclude: tuple = (), before: Optional[float] = None) -> list[list[dict]]:
        """The exchanges whose ORIGINAL words best match `text`, best first, straight from the raw log.
        exclude: exchange ids to leave out (the one being asked, and anything already in the prompt).
        Each result is the exchange's own log lines."""
        with self._lock:
            recs = self._load()
            q = set(words(text))
            if not q:
                return []
            idf = self._idf()
            hits: dict[str, set] = {}
            for w in q:
                for i in self._index.get(w, ()):
                    r = recs[i]
                    if r["x"] in exclude or (before is not None and r["t"] >= before):
                        continue
                    hits.setdefault(r["x"], set()).add(w)
            need = 1 if len(q) == 1 else 2
            scored = [(sum(idf(w) for w in ws), x) for x, ws in hits.items() if len(ws) >= need]
            scored.sort(key=lambda sx: (-sx[0], sx[1]))
            return [self.exchange(x) for _, x in scored[:k]]

    def descend(self, companion: str, text: str, k: int = 2, exclude: tuple = ()) -> list[list[dict]]:
        """The same question asked of the tree: from the highest level that exists, follow the nodes whose kept
        sentences match, down to the exchanges under them. Returns original log lines, like search()."""
        with self._lock:
            q = set(words(text))
            nodes = self._load_nodes(companion)
            if not q or not nodes:
                return []

            def match(node: dict) -> int:
                return len(q & {w for ln in node["lines"] for w in words(ln["text"])})

            top = None
            for row in reversed(LEVELS):
                level_nodes = [n for n in nodes.values() if n["level"] == row["name"]]
                if level_nodes:
                    top = level_nodes
                    break
            frontier = sorted((n for n in top or [] if match(n) > 0), key=lambda n: (-match(n), -n["t0"]))[:3]
            found: list[tuple] = []
            while frontier:
                nxt = []
                for n in frontier:
                    if n["of"] == "exchange":
                        for x in n["children"]:
                            if x in exclude:
                                continue
                            lines = self.exchange(x)
                            score = len(q & {w for r in lines for w in words(r["text"])})
                            if score:
                                found.append((score, x))
                    else:
                        kids = [nodes[c] for c in n["children"] if c in nodes]
                        nxt += sorted((c for c in kids if match(c) > 0), key=lambda c: (-match(c), -c["t0"]))[:3]
                frontier = nxt
            found.sort(key=lambda sx: (-sx[0], sx[1]))
            out, seen = [], set()
            for _, x in found:
                if x not in seen:
                    seen.add(x)
                    out.append(self.exchange(x))
            return out[:k]

    def recall(self, companion: str, text: str, k: int = 2, exclude: tuple = ()) -> list[list[dict]]:
        """What a companion can bring back for a sentence: the raw index first, then the tree for anything it
        missed. Original lines only."""
        got = self.search(text, k, exclude)
        have = {ex[0]["x"] for ex in got}
        for ex in self.descend(companion, text, k, exclude):
            if ex[0]["x"] not in have and len(got) < k:
                got.append(ex)
        return got

    # -- clearing ------------------------------------------------------------------------------------------------
    def clear(self) -> int:
        """Forget every conversation: the log and both upper trees. Returns how many lines the log held."""
        with self._lock:
            n = len(self._load())
            for p in [self.log_path] + [self._nodes_path(c) for c in COMPANIONS]:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
            self._recs, self._by_id, self._index = [], {}, {}
            self._nodes = {c: {} for c in COMPANIONS}
            return n


def tree_files(pilot_dir: Path | str) -> list[str]:
    """The tree's files that exist under a pilot's memory folder, as paths relative to it, for export."""
    d = Path(pilot_dir) / "tree"
    names = [LOG_NAME] + [f"nodes_{c}.jsonl" for c in COMPANIONS]
    return [f"tree/{n}" for n in names if (d / n).is_file()]


def open_tree(pilot_dir: Path | str, session: str = "", now: Callable[[], float] = time.time) -> TreeStore:
    return TreeStore(Path(pilot_dir) / "tree", now=now, current_session=session)


def spoken_date(t: float, now: float) -> str:
    """How a companion says when something was said: Earlier today, Yesterday, or the day and month."""
    then, today = datetime.fromtimestamp(t), datetime.fromtimestamp(now)
    days = (today.date() - then.date()).days
    if days <= 0:
        return "Earlier today"
    if days == 1:
        return "Yesterday"
    months = "January February March April May June July August September October November December".split()
    return f"{then.day} {months[then.month - 1]}" + ("" if then.year == today.year else f" {then.year}")


# ---------------------------------------------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    results = []

    def case(name, cond, detail=""):
        results.append((name, bool(cond), str(detail)))

    day = 86400.0
    t0 = datetime(2026, 9, 7, 20, 0).timestamp()            # a Monday evening
    clock = [t0]
    with tempfile.TemporaryDirectory() as tmp:
        st = TreeStore(Path(tmp) / "tree", now=lambda: clock[0], current_session="s1")
        a = st.append("pilot", "We lost the ROC on that cargo run to Shubin, it rolled out the back over Lyria.", to="elah")
        st.append("elah", "Then Lyria has a mining vehicle and we have a lesson.", x=a["id"])
        b = st.append("pilot", "I want to save up for a Prospector.")
        st.append("elah", "A Prospector. Mining, then.", x=b["id"])
        raw = st.log_path.read_bytes()
        case("the first line of the log describes its fields", json.loads(raw.splitlines()[0])["kind"] == "schema")
        case("a reply carries the exchange of the sentence it answers", [r["who"] for r in st.exchange(a["id"])] == ["pilot", "elah"])
        got = st.search("remember that cargo run where we lost the roc")
        case("the raw index finds the original sentence", got and got[0][0]["id"] == a["id"])
        clock[0] = t0 + day
        st.current_session = "s2"
        st.build("elah")
        n = st.node("elah", "elah:session:s1")
        case("a finished session gets a node of the pilot's own sentences", n and {ln["text"] for ln in n["lines"]} <= {a["text"], b["text"]})
        case("...and the node leads back to every original line", [r["id"] for r in st.originals("elah", n["id"])] == [r["id"] for r in st.records()])
        case("building nodes did not touch the log", st.log_path.read_bytes() == raw)
        st2 = TreeStore(Path(tmp) / "tree", now=lambda: clock[0], current_session="s2")
        case("a second reader sees the same log and nodes", len(st2.records()) == 4 and st2.node("elah", n["id"]) is not None)
        case("clear() removes the log and the nodes", st.clear() == 4 and not st.log_path.exists() and st.records() == [])
    for name, ok, detail in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   <{detail[:200]}>" if not ok else ""))
    bad = sum(not ok for _, ok, _ in results)
    print(f"tree_memory selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
