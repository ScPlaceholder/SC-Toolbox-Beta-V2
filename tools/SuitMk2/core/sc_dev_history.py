"""sc_dev_history.py - search Star Citizen's development history, streamed from GitHub.

The corpus (1,368 dev-video transcripts + 5,152 RSI comm-link records, ~51 MB) lives in the public repo
ScPlaceholder/sc-dev-history. Nothing is bundled. This client:
  1. downloads ONE file, chronology/search_index.json.gz (~3.5 MB), the first time, and caches it;
  2. searches it offline (IDF-weighted term match over every transcript and comm-link);
  3. fetches a single transcript from raw.githubusercontent.com only when a result is opened, to show the lines
     that matched, and caches that too.
Stdlib only, so both the Toolbox tool and the SuitMk2 companions can import it.

Content is unofficial fan transcription: Star Citizen content (c) Cloud Imperium Games; not affiliated. Every
result carries a link back to the original video or comm-link. `ATTRIBUTION` is the line to show in any UI.

    python sc_dev_history.py "laser head"             # top results
    python sc_dev_history.py "quantum drive" --lines  # with matching transcript lines
    python sc_dev_history.py --selftest
"""
from __future__ import annotations

import gzip
import json
import math
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

REPO = "ScPlaceholder/sc-dev-history"
REF = os.environ.get("SC_DEV_HISTORY_REF", "main")      # "corpus" until the corpus PR is merged
INDEX_PATH = "chronology/search_index.json.gz"
ATTRIBUTION = ("Unofficial fan transcripts. Star Citizen content (c) Cloud Imperium Games; not affiliated. "
               "Source: github.com/" + REPO)
CACHE = Path(os.environ.get("SC_DEV_HISTORY_CACHE", Path.home() / ".sctoolbox" / "dev_history"))
INDEX_MAX_AGE_S = 7 * 24 * 3600            # re-check the index weekly; transcripts never change
WORD = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
EXCERPT_WORDS = 30


def raw_url(path: str, ref: str = REF) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "SC-Toolbox-DevHistory/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class DevHistory:
    def __init__(self, fetch: Optional[Callable[[str], bytes]] = None, cache: Path = CACHE, ref: str = REF,
                 now: Callable[[], float] = time.time):
        self._fetch = fetch or (lambda path: _http_get(raw_url(path, ref)))
        self.cache = Path(cache)
        self.now = now
        self._index = None

    # ---- data -------------------------------------------------------------------------------------------------
    def _cached(self, path: str, max_age: Optional[float]) -> bytes:
        local = self.cache / path
        if local.exists() and (max_age is None or self.now() - local.stat().st_mtime < max_age):
            return local.read_bytes()
        try:
            data = self._fetch(path)
        except Exception:
            if local.exists():              # offline: a stale copy beats no answer
                return local.read_bytes()
            raise
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(local)
        return data

    def index(self) -> dict:
        if self._index is None:
            self._index = json.loads(gzip.decompress(self._cached(INDEX_PATH, INDEX_MAX_AGE_S)))
        return self._index

    def transcript(self, doc: dict) -> str:
        if doc.get("k") != "v":
            return ""
        return self._cached(doc["p"], None).decode("utf-8", errors="replace")

    # ---- search -----------------------------------------------------------------------------------------------
    def search(self, query: str, n: int = 10) -> list[dict]:
        idx = self.index()
        terms = [w for w in WORD.findall(query.lower()) if w in idx["postings"]]
        if not terms:
            return []
        n_docs = idx["n_docs"]
        scores: dict[int, float] = {}
        hit_terms: dict[int, int] = {}
        for w in terms:
            post = idx["postings"][w]
            idf = math.log(1 + n_docs / len(post))
            for i, c in post.items():
                i = int(i)
                scores[i] = scores.get(i, 0.0) + idf * (1 + math.log(c))
                hit_terms[i] = hit_terms.get(i, 0) + 1
        # A document matching every query term outranks one that repeats a single term a lot. A term in the TITLE
        # counts again: long transcripts contain every common word somewhere, so without this a comm-link ranked
        # #347 for its own exact title ("Give These People Air").
        tset = set(terms)
        title_hits = {i: len(tset & set(WORD.findall(idx["docs"][i].get("t", "").lower()))) for i in scores}
        # The index keeps only 3+ character words, so "Alpha 4.0" searched just "alpha" and returned the 3.23 stream,
        # and "Hull C" returned the Hull B. The TITLES are in the index, so check the dropped tokens (and the whole
        # query as a phrase) against them: a title that carries "4.0" or "hull c" ranks first.
        short = [t for t in query.lower().split() if not WORD.fullmatch(t.strip(".,:;!?\"'()"))]
        short = [t.strip(".,:;!?\"'()") for t in short if t.strip(".,:;!?\"'()")]
        phrase = " ".join(query.lower().split())

        def _has(title: str, tok: str) -> bool:
            return re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", title) is not None
        titles = {i: idx["docs"][i].get("t", "").lower() for i in scores}
        exact = {i: _has(titles[i], phrase) for i in scores}
        short_hits = {i: sum(_has(titles[i], t) for t in short) for i in scores}
        ranked = sorted(scores, key=lambda i: (exact[i], short_hits[i], hit_terms[i] + title_hits[i], scores[i]),
                        reverse=True)[:n]
        out = []
        for i in ranked:
            d = dict(idx["docs"][i])
            d["score"] = round(scores[i], 2)
            d["matched"] = hit_terms[i]
            d["url"] = d.get("u") or f"https://www.youtube.com/watch?v={d['id']}"
            out.append(d)
        return out

    def lines(self, doc: dict, query: str, n: int = 3) -> list[str]:
        """The transcript lines that best match the query (fetches that one transcript, once)."""
        terms = set(WORD.findall(query.lower()))
        text = self.transcript(doc)
        if not text:
            return [doc.get("s", "")] if doc.get("s") else []
        # Caption transcripts are often one very long line, so score fixed word WINDOWS, not lines.
        words = text.split()
        best = []
        for start in range(0, max(1, len(words)), EXCERPT_WORDS // 2):
            chunk = words[start:start + EXCERPT_WORDS]
            k = len(terms & set(WORD.findall(" ".join(chunk).lower())))
            if k:
                best.append((k, start, " ".join(chunk)))
        best.sort(key=lambda x: (-x[0], x[1]))
        picked, used = [], []
        for k, start, chunk in best:
            if all(abs(start - u) >= EXCERPT_WORDS for u in used):   # no overlapping excerpts
                picked.append(chunk)
                used.append(start)
            if len(picked) == n:
                break
        return picked


# ---- selftest ------------------------------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    docs = [{"id": "vidA", "k": "v", "t": "ISC: Mining", "d": "2020-01-01", "p": "transcripts/captions/vidA.txt"},
            {"id": "https://rsi/1", "k": "c", "t": "Monthly Report: Laser", "d": "2019-05-01", "s": "laser head work",
             "u": "https://rsi/1"}]
    postings = {"laser": {"0": 1, "1": 3}, "head": {"0": 5, "1": 1}, "mining": {"0": 9},
                "report": {"0": 9, "1": 1}}     # body of doc 0 repeats it; only doc 1 has it in the TITLE
    index = {"version": 1, "n_docs": 2, "docs": docs, "postings": postings}
    files = {INDEX_PATH: gzip.compress(json.dumps(index).encode()),
             "transcripts/captions/vidA.txt": b"intro\nthe laser head overheats\nmining is fun\n"}
    calls = []

    def fake(path):
        calls.append(path)
        return files[path]
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)
    with tempfile.TemporaryDirectory() as tmp:
        dh = DevHistory(fetch=fake, cache=Path(tmp))
        r = dh.search("laser head")
        case("both docs match 'laser head'", len(r) == 2)
        case("a doc matching every term ranks first", r[0]["matched"] == 2)
        case("comm-link result links to its own url", any(x["url"] == "https://rsi/1" for x in r))
        case("video result links to youtube", any("youtube.com/watch?v=vidA" in x["url"] for x in r))
        case("index fetched once", calls.count(INDEX_PATH) == 1)
        v = [x for x in r if x["k"] == "v"][0]
        case("matching excerpt found", "laser head overheats" in dh.lines(v, "laser head")[0])
        long = " ".join(["filler"] * 200) + " the laser head overheats " + " ".join(["filler"] * 200)
        files["transcripts/captions/vidL.txt"] = long.encode()
        lx = dh.lines({"k": "v", "p": "transcripts/captions/vidL.txt"}, "laser head")
        case("one-line transcript: excerpt is the matching WINDOW, not the start",
             len(lx) >= 1 and "laser head" in lx[0] and len(lx[0].split()) <= 30)
        dh.lines(v, "mining")
        case("transcript fetched once, then cached", calls.count("transcripts/captions/vidA.txt") == 1)
        dh2 = DevHistory(fetch=lambda p: (_ for _ in ()).throw(OSError("offline")), cache=Path(tmp))
        case("offline: cached index still searches", len(dh2.search("mining")) == 1)
        case("unknown terms return nothing, not an error", dh.search("zzqqxx") == [])
        case("a title match outranks a body that repeats the word", dh.search("report")[0]["t"] == "Monthly Report: Laser")
        docs.append({"id": "vidB", "k": "v", "t": "All About Alpha 3.23", "d": "2024-01-01", "p": "x"})
        docs.append({"id": "vidC", "k": "v", "t": "All About Alpha 4.0", "d": "2024-12-01", "p": "y"})
        postings["alpha"] = {"2": 9, "3": 1}      # the 3.23 video says "alpha" far more often
        files[INDEX_PATH] = gzip.compress(json.dumps(dict(index, n_docs=4)).encode())
        dh4 = DevHistory(fetch=fake, cache=Path(tmp) / "four")
        case("a short token the index drops ('4.0') still picks the right title",
             dh4.search("Alpha 4.0")[0]["t"] == "All About Alpha 4.0")
        case("attribution names CIG", "Cloud Imperium Games" in ATTRIBUTION)
    print("sc_dev_history selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(_selftest())
    if not args:
        print(__doc__)
        sys.exit(2)
    want_lines = "--lines" in args
    q = " ".join(a for a in args if not a.startswith("--"))
    dh = DevHistory()
    for r in dh.search(q):
        print(f"{r['d'] or '????-??-??'}  [{'video' if r['k'] == 'v' else 'comm-link'}]  {r['t']}\n    {r['url']}")
        if want_lines:
            for l in dh.lines(r, q):
                print(f"      > {l[:160]}")
    print("\n" + ATTRIBUTION)
