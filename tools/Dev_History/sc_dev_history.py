"""sc_dev_history.py - search Star Citizen's development history, streamed from GitHub (J 2026-09-24).

The corpus (dev-video transcripts, RSI comm-links with full text incl. every Monthly Report, and CIG posts from the
Spectrum Devtracker) lives in the public repo ScPlaceholder/sc-dev-history, refreshed daily by a GitHub Action there.
Nothing is bundled. This client:
  1. downloads ONE file, chronology/search_index.json.gz (a few MB), the first time, and caches it; after that it
     re-checks at most every INDEX_MAX_AGE_S with a conditional request, so an unchanged index costs ~nothing;
  2. searches it offline (IDF-weighted term match over every transcript, comm-link and dev post);
  3. fetches a single text (transcript, article or post) from raw.githubusercontent.com only when a result is
     opened, to show the lines that matched or the whole article, and caches that too.
Stdlib only, so both the Toolbox tool and the SuitMk2 companions can import it.

Document kinds ("k" in the index): v = dev video, c = RSI comm-link, d = CIG Devtracker post.

Content is unofficial fan transcription: Star Citizen content (c) Cloud Imperium Games; not affiliated. Every
result carries a link back to the original video or comm-link. `ATTRIBUTION` is the line to show in any UI.

    python sc_dev_history.py "laser head"             # top results
    python sc_dev_history.py "quantum drive" --lines  # with matching transcript lines
    python sc_dev_history.py --monthly                # list Monthly Reports, newest first
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
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

REPO = "ScPlaceholder/sc-dev-history"
REF = os.environ.get("SC_DEV_HISTORY_REF", "corpus")    # the corpus lives on this branch until its PR is merged
INDEX_PATH = "chronology/search_index.json.gz"
ATTRIBUTION = ("Unofficial fan archive. Star Citizen content (c) Cloud Imperium Games; not affiliated. "
               "Source: github.com/" + REPO)
CACHE = Path(os.environ.get("SC_DEV_HISTORY_CACHE", Path.home() / ".sctoolbox" / "dev_history"))
INDEX_MAX_AGE_S = 12 * 3600               # re-check the index twice a day (it updates daily); texts never change
KIND_LABEL = {"v": "video", "c": "comm-link", "d": "dev post", "s": "ship page", "b": "brochure"}
MONTHLY = re.compile(r"\bmonthly (studio )?report\b", re.I)
_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
           "november", "december"]
WORD = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
_POSSESSIVE = re.compile(r"'s?$")


def _norm(w: str) -> str:
    """kraken's -> kraken: the corpus index stores possessives as the plain word (tools/build_index.py norm)."""
    return _POSSESSIVE.sub("", w)


# Same stop list as the corpus's tools/build_index.py: these never get postings.
STOP = set("""the and for that this with you are was but not have they what there from just going can all about
we're it's that's i'm like know yeah really one would get think out some more also which will when were been them
their then than into our your its his her she him has had how who why where well very okay right thing things
lot kind sort gonna want make made let see look need because those these other over only even much any here now
""".split())
EXCERPT_WORDS = 30


def raw_url(path: str, ref: str = REF) -> str:
    return f"https://raw.githubusercontent.com/{REPO}/{ref}/{path}"


def _http_get(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "SC-Toolbox-DevHistory/1"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _http_get_cond(url: str, etag: str, timeout: float = 30.0) -> tuple[Optional[bytes], str]:
    """GET with If-None-Match. Returns (None, etag) when the server says 304 Not Modified."""
    headers = {"User-Agent": "SC-Toolbox-DevHistory/1"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as r:
            return r.read(), r.headers.get("ETag") or ""
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return None, etag
        raise


def kind_label(doc: dict) -> str:
    return KIND_LABEL.get(doc.get("k", ""), "record")


def is_monthly_report(doc: dict) -> bool:
    return doc.get("g") == "mr" or (doc.get("k") == "c" and bool(MONTHLY.search(doc.get("t", ""))))


def report_month(doc: dict) -> str:
    """'YYYY-MM' the report COVERS, from its title ('... Monthly Report: November-December 2021' -> 2021-12);
    falls back to the publish date's month."""
    t = doc.get("t", "").lower()
    m = re.search(r"(" + "|".join(_MONTHS) + r")(?:\s*[-/&]\s*(" + "|".join(_MONTHS) + r"))?,?\s+(\d{4})", t)
    if m:
        return f"{m.group(3)}-{_MONTHS.index(m.group(2) or m.group(1)) + 1:02d}"
    return (doc.get("d") or "")[:7]


def report_series(doc: dict) -> str:
    t = doc.get("t", "").lower()
    if "squadron" in t:
        return "Squadron 42"
    if "studio" in t:
        return "Studio"
    return "Star Citizen"


class DevHistory:
    def __init__(self, fetch: Optional[Callable[[str], bytes]] = None, cache: Path = CACHE, ref: str = REF,
                 now: Callable[[], float] = time.time,
                 fetch_cond: Optional[Callable[[str, str], tuple]] = None):
        """``fetch(path) -> bytes``. ``fetch_cond(path, etag) -> (bytes or None, etag)`` is used for the index
        re-check when given (or by default when ``fetch`` is not overridden): None means unchanged."""
        self._fetch = fetch or (lambda path: _http_get(raw_url(path, ref)))
        self._fetch_cond = fetch_cond or (None if fetch else (lambda path, etag: _http_get_cond(raw_url(path, ref),
                                                                                                 etag)))
        self.cache = Path(cache)
        self.now = now
        self._index = None
        self.last_total = 0                  # how many documents the last search matched (before the cut to n)
        self._merged: set = set()            # ids whose extra text is already in the in-memory postings

    # ---- data -------------------------------------------------------------------------------------------------
    def _cached(self, path: str, max_age: Optional[float]) -> bytes:
        local = self.cache / path
        if local.exists() and (max_age is None or self.now() - local.stat().st_mtime < max_age):
            return local.read_bytes()
        etag_file = local.with_name(local.name + ".etag")
        use_cond = self._fetch_cond is not None and max_age is not None     # only files that get re-checked
        try:
            if use_cond:
                etag = (etag_file.read_text(encoding="utf-8").strip()
                        if etag_file.exists() and local.exists() else "")
                data, new_etag = self._fetch_cond(path, etag)
                if data is None and local.exists():     # 304: unchanged; restart the age clock, keep the copy
                    os.utime(local, (self.now(), self.now()))
                    return local.read_bytes()
            else:
                data, new_etag = self._fetch(path), ""
        except Exception:
            if local.exists():              # offline: a stale copy beats no answer
                return local.read_bytes()
            raise
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_suffix(local.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(local)
        if new_etag:
            etag_file.write_text(new_etag, encoding="utf-8")
        return data

    def index(self) -> dict:
        if self._index is None:
            self._index = json.loads(gzip.decompress(self._cached(INDEX_PATH, INDEX_MAX_AGE_S)))
        return self._index

    def text(self, doc: dict) -> str:
        """The full text behind a result: a video transcript, a comm-link article or a Devtracker post.
        Empty when the corpus has no full text for it (yet): use doc['s'], the summary, instead."""
        if not doc.get("p"):
            return ""
        return self._cached(doc["p"], None).decode("utf-8", errors="replace")

    transcript = text                        # the old name; SuitMk2 companions call it

    def monthly_reports(self) -> list[dict]:
        """Every Monthly Report comm-link, newest report month first, each with 'month' (YYYY-MM it covers),
        'series' (Star Citizen / Squadron 42 / Studio) and 'url'."""
        out = []
        for d in self.index()["docs"]:
            if is_monthly_report(d):
                r = dict(d)
                r["month"], r["series"] = report_month(d), report_series(d)
                r["url"] = d.get("u") or ""
                out.append(r)
        out.sort(key=lambda r: (r["month"], r.get("d") or "", r["series"]), reverse=True)
        return out

    # ---- search -----------------------------------------------------------------------------------------------
    def search(self, query: str, n: int = 10) -> list[dict]:
        idx = self.index()
        terms = [w for w in map(_norm, WORD.findall(query.lower())) if w in idx["postings"]]
        self.last_total = 0
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
        # #347 for its own exact title ("Give These People Air"), found 2026-09-25.
        tset = set(terms)
        title_hits = {i: len(tset & set(WORD.findall(idx["docs"][i].get("t", "").lower()))) for i in scores}
        # The index keeps only 3+ character words, so "Alpha 4.0" searched just "alpha" and returned the 3.23 stream,
        # and "Hull C" returned the Hull B. The TITLES are in the index, so check the dropped tokens (and the whole
        # query as a phrase) against them: a title that carries "4.0" or "hull c" ranks first. Found 2026-09-25.
        short = [t for t in query.lower().split() if not WORD.fullmatch(t.strip(".,:;!?\"'()"))]
        short = [t.strip(".,:;!?\"'()") for t in short if t.strip(".,:;!?\"'()")]
        phrase = " ".join(query.lower().split())

        def _has(title: str, tok: str) -> bool:
            return re.search(r"(?<![a-z0-9])" + re.escape(tok) + r"(?![a-z0-9])", title) is not None
        titles = {i: idx["docs"][i].get("t", "").lower() for i in scores}
        exact = {i: _has(titles[i], phrase) for i in scores}
        short_hits = {i: sum(_has(titles[i], t) for t in short) for i in scores}
        self.last_total = len(scores)
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

    def add_text(self, doc: dict, text: str) -> bool:
        """Make *text* searchable for *doc* in the in-memory index (the file on disk is untouched).
        *doc* is matched by its "id"; a doc the index does not have is appended. Used for texts the client got
        from somewhere other than the published index (e.g. fetched live from RSI before the archive has them).
        Returns False if that id was already merged."""
        idx = self.index()
        key = doc.get("id")
        if not key or key in self._merged:
            return False
        ids = idx.setdefault("_ids", {})
        if not ids:
            ids.update({d.get("id"): i for i, d in enumerate(idx["docs"])})
        i = ids.get(key)
        if i is None:
            i = len(idx["docs"])
            idx["docs"].append(doc)
            ids[key] = i
            idx["n_docs"] = len(idx["docs"])
        postings = idx["postings"]
        counts: dict[str, int] = {}
        for w in WORD.findall(f"{doc.get('t', '')} {text}".lower()):
            if w not in STOP:
                counts[w] = counts.get(w, 0) + 1
        si = str(i)
        for w, c in counts.items():
            post = postings.setdefault(w, {})
            post[si] = post.get(si, 0) + c
        self._merged.add(key)
        return True

    def lines(self, doc: dict, query: str, n: int = 3) -> list[str]:
        """The transcript lines that best match the query (fetches that one transcript, once)."""
        terms = set(WORD.findall(query.lower()))
        text = self.text(doc)
        if not text:
            return [doc.get("s", "")] if doc.get("s") else []
        # Caption transcripts are often one very long line, so score fixed word WINDOWS, not lines.
        # Article/post texts mark headings '## ' and bullets '- ' for the reader; excerpts drop the markers.
        words = re.sub(r"(?m)^(## |- )", "", text).split()
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


# ---- search by phrase -----------------------------------------------------------------------------------------
# "Kraken land on planets" should find the Kraken LANDING on a PLANET, or PLANETSIDE, or on a planet's SURFACE,
# in any wording. So each word of the phrase becomes a CONCEPT: the word's forms in the index (land, lands,
# landed, landing...) plus a few domain synonyms. A document matches when it has every concept; the closer
# together they appear in its text, the better (checked on the text itself: see phrase_window).
_FORM_SUFFIXES = ("", "s", "es", "d", "ed", "ing", "ings", "er", "ers", "ary", "al", "side", "able", "'s")
PHRASE_SYNONYMS = {
    "planet": ["planetside", "planetary", "surface", "surfaces", "moon", "moons"],
    "land": ["touchdown", "landable"],
    "ship": ["ships", "vessel", "vessels"],
    "fly": ["flight", "flying", "flew"],
    "big": ["large", "huge"],
    "remove": ["removed", "removing", "cut"],
    "add": ["added", "adding"],
    "gun": ["weapon", "weapons", "turret", "turrets"],
    "npc": ["npcs", "ai"],
}


def _base(word: str) -> str:
    """Strip one inflection so 'landing'/'planets'/'landed' give 'land'/'planet'/'land'."""
    w = word.lower().strip("'")
    for suf in ("ings", "ing", "ies", "ed", "es", "s", "'s"):
        if w.endswith(suf) and len(w) - len(suf) >= 3 and not (suf == "s" and w.endswith("ss")):
            w = w[: -len(suf)] + ("y" if suf == "ies" else "")
            break
    if len(w) > 4 and w[-1] == w[-2] and w[-1] not in "lsz":     # 'stopped' -> 'stopp' -> 'stop'
        w = w[:-1]
    return w


class DevHistoryPhrase:
    """Mixin: phrase search over DevHistory's index (kept separate so the plain search stays as it was)."""

    def _vocab_by_prefix(self) -> dict:
        idx = self.index()
        pv = idx.get("_by_prefix")
        if pv is None:
            pv = {}
            for t in idx["postings"]:
                pv.setdefault(t[:3], []).append(t)
            idx["_by_prefix"] = pv
        return pv

    def concept_terms(self, word: str) -> set:
        """Every index term that is a form of *word* (or a listed synonym of it)."""
        postings = self.index()["postings"]
        pv = self._vocab_by_prefix()
        base = _base(word)
        stems = {base} | ({base[:-1]} if base.endswith("e") else set())
        out = set()
        for st in stems:
            for t in pv.get(st[:3], []):
                if t.startswith(st) and t[len(st):] in _FORM_SUFFIXES:
                    out.add(t)
        for syn in PHRASE_SYNONYMS.get(base, []):
            if syn in postings:
                out.add(syn)
        if word.lower() in postings:
            out.add(word.lower())
        return out

    def phrase_concepts(self, query: str) -> list:
        """[(word, {terms})] for the meaningful words of *query*; words the index has never seen are dropped."""
        seen, out = set(), []
        for w in map(_norm, WORD.findall(query.lower())):
            if w in STOP or w in seen:
                continue
            seen.add(w)
            terms = self.concept_terms(w)
            if terms:
                out.append((w, terms))
        return out

    def phrase_search(self, query: str, n: Optional[int] = None) -> list:
        """Documents that contain EVERY concept of the phrase, best first. Each result carries "near": None
        (not checked yet); DevHistory.phrase_window() on its text tells how close together they are."""
        idx = self.index()
        concepts = self.phrase_concepts(query)
        self.last_total = 0
        if not concepts:
            return []
        n_docs = idx["n_docs"]
        per_concept = []
        for _w, terms in concepts:
            tf: dict = {}
            for t in terms:
                for i, c in idx["postings"][t].items():
                    tf[int(i)] = tf.get(int(i), 0) + c
            per_concept.append(tf)
        common = set(per_concept[0])
        for tf in per_concept[1:]:
            common &= set(tf)
        scores = {}
        for i in common:
            scores[i] = sum(math.log(1 + n_docs / len(tf)) * (1 + math.log(tf[i])) for tf in per_concept)
        title_hits = {}
        for i in common:
            words = set(WORD.findall(idx["docs"][i].get("t", "").lower()))
            title_hits[i] = sum(1 for _w, terms in concepts if terms & words)
        ranked = sorted(common, key=lambda i: (title_hits[i], scores[i]), reverse=True)
        self.last_total = len(ranked)
        out = []
        for i in ranked[:n]:
            d = dict(idx["docs"][i])
            d["score"] = round(scores[i], 2)
            d["matched"] = len(concepts)
            d["near"] = None
            d["url"] = d.get("u") or f"https://www.youtube.com/watch?v={d['id']}"
            out.append(d)
        return out

    @staticmethod
    def phrase_window(text: str, concepts: list, max_span: int = 40):
        """The tightest stretch of *text* containing every concept: (span in words, start, end) or None.
        Only stretches of at most *max_span* words count as the phrase being said."""
        words = re.sub(r"(?m)^(## |- )", "", text or "").split()
        low = [re.sub(r"[^a-z0-9'\-]", "", w.lower()) for w in words]
        k = len(concepts)
        if k == 0:
            return None
        need = [c[1] for c in concepts]
        hits = [(i, j) for i, w in enumerate(low) for j in range(k) if w in need[j]]
        best = None
        count: dict = {}
        have = 0
        left = 0
        for right in range(len(hits)):
            j = hits[right][1]
            count[j] = count.get(j, 0) + 1
            if count[j] == 1:
                have += 1
            while have == k:
                span = hits[right][0] - hits[left][0] + 1
                if best is None or span < best[0]:
                    best = (span, hits[left][0], hits[right][0])
                lj = hits[left][1]
                count[lj] -= 1
                if count[lj] == 0:
                    have -= 1
                left += 1
        if best is None or best[0] > max_span:
            return None
        return best

    def phrase_lines(self, doc: dict, query: str, n: int = 3, width: int = 60) -> list:
        """Excerpts that say the most of the phrase: *width*-word stretches ranked by how many of its concepts
        they contain (rare words count more), then by how tightly. The whole phrase within a sentence or two
        comes first; when a document never says it all in one place, the stretches that come closest do."""
        concepts = self.phrase_concepts(query)
        text = self.text(doc)
        if not text or not concepts:
            return [doc.get("s", "")] if doc.get("s") else []
        idx = self.index()
        n_docs = idx["n_docs"]
        weight = []
        for _w, terms in concepts:
            df = len({i for t in terms for i in idx["postings"].get(t, {})}) or 1
            weight.append(math.log(1 + n_docs / df))
        words = re.sub(r"(?m)^(## |- )", "", text).split()
        low = [re.sub(r"[^a-z0-9'\-]", "", w.lower()) for w in words]
        hit = [[j for j, (_w, terms) in enumerate(concepts) if w in terms] for w in low]
        cands = []
        step = max(1, width // 4)
        for start in range(0, max(1, len(words) - width // 2), step):
            seen = {}
            for pos in range(start, min(len(words), start + width)):
                for j in hit[pos]:
                    seen.setdefault(j, []).append(pos)
            if len(seen) < min(2, len(concepts)):
                continue
            firsts = [v[0] for v in seen.values()]
            lasts = [v[-1] for v in seen.values()]
            tight = max(lasts) - min(firsts) + 1
            score = sum(weight[j] for j in seen)
            cands.append((score, -tight, start, min(firsts), max(lasts)))
        cands.sort(reverse=True)
        out, used = [], []
        for score, _t, start, a, b in cands:
            if any(abs(a - u) < width for u in used):
                continue
            lo, hi = max(0, a - 10), min(len(words), b + 11)
            out.append(("… " if lo > 0 else "") + " ".join(words[lo:hi]) + (" …" if hi < len(words) else ""))
            used.append(a)
            if len(out) == n:
                break
        return out or self.lines(doc, " ".join(t for _w, terms in concepts for t in terms), n)

    def phrase_rank(self, docs: list, query: str, limit: int = 60) -> list:
        """Check the first *limit* results on their text (fetching it once, cached) and move the ones that say
        the whole phrase closest together to the top. Sets doc["near"] = words between the first and last
        concept (small = said together), or 0 when the text never has them all. Network: worker thread only."""
        concepts = self.phrase_concepts(query)
        head, tail = docs[:limit], docs[limit:]
        for d in head:
            try:
                txt = self.text(d) if d.get("p") else (d.get("s") or "")
            except OSError:
                txt = d.get("s") or ""
            win = self.phrase_window(txt, concepts, max_span=10 ** 9)
            d["near"] = win[0] if win else 0
        head.sort(key=lambda d: (d["near"] == 0, d["near"] or 0))
        return head + tail


for _name, _fn in list(vars(DevHistoryPhrase).items()):     # the phrase methods are DevHistory methods too
    if not _name.startswith("__"):
        setattr(DevHistory, _name, _fn)


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

        # v2 index: comm-links and dev posts with full text, Monthly Reports, conditional refresh.
        docs5 = [{"id": "https://rsi/mr1", "k": "c", "t": "Star Citizen Monthly Report: November-December 2021",
                  "d": "2022-01-10", "s": "Welcome", "u": "https://rsi/mr1", "g": "mr", "p": "commlinks/1.txt"},
                 {"id": "https://rsi/mr2", "k": "c", "t": "Squadron 42 Monthly Report: August 2022", "d": "2022-08-01",
                  "s": "S42", "u": "https://rsi/mr2"},
                 {"id": "https://rsi/store", "k": "c", "t": "Monthly Store Bundle 2026", "d": "2026-09-01", "u": "x"},
                 {"id": "111", "k": "d", "t": "Life support?", "d": "2026-09-26", "s": "We have been tracking",
                  "u": "https://rsi/spectrum/111", "a": "Nicou-CIG", "c": "General", "p": "devposts/111.txt"}]
        post5 = {"cargo": {"0": 2, "3": 1}, "grid": {"0": 1}, "life": {"3": 2}, "support": {"3": 2}}
        files["commlinks/1.txt"] = b"PU Monthly Report\n## Vehicles\nThe Hull C got a new cargo grid this month."
        files["devposts/111.txt"] = b"Hey folks, the life support fix is in 4.10.2."
        files[INDEX_PATH] = gzip.compress(json.dumps({"version": 2, "n_docs": 4, "docs": docs5,
                                                      "postings": post5}).encode())
        dh5 = DevHistory(fetch=fake, cache=Path(tmp) / "five")
        mrs = dh5.monthly_reports()
        case("monthly reports: store bundle is not a report", [m["t"][:8] for m in mrs] == ["Squadron", "Star Cit"])
        case("monthly reports: month from the title, newest first; series",
             [(m["month"], m["series"]) for m in mrs] == [("2022-08", "Squadron 42"), ("2021-12", "Star Citizen")])
        r5 = dh5.search("cargo grid")
        case("comm-link full text: excerpt comes from the article body",
             "new cargo grid" in dh5.lines(r5[0], "cargo grid")[0])
        d5 = dh5.search("life support")[0]
        case("dev post: kind, url, body", kind_label(d5) == "dev post" and d5["url"] == "https://rsi/spectrum/111"
             and "4.10.2" in dh5.text(d5))
        case("no full text: falls back to the summary", dh5.lines(mrs[0], "anything") == ["S42"])
        case("old name transcript() still works", dh5.transcript(d5) == dh5.text(d5))
        case("last_total counts every match", dh5.search("cargo", 1) and dh5.last_total == 2)
        mr2 = next(m for m in mrs if m["series"] == "Squadron 42")
        dh5.add_text(mr2, "The Idris bridge got a lighting pass this month.")
        case("add_text: a fetched article becomes searchable",
             dh5.search("idris bridge")[0]["t"].startswith("Squadron 42 Monthly"))
        case("add_text: merging twice is a no-op", dh5.add_text(mr2, "idris") is False)
        dh5.add_text({"id": "999", "k": "d", "t": "Hull C tractor beams", "d": "2026-09-27", "u": "x"}, "tractor")
        case("add_text: an unknown doc is appended", dh5.search("tractor")[0]["id"] == "999")

        # Search by phrase.
        dh5.add_text({"id": "p1", "k": "d", "t": "Kraken Q&A", "d": "2026-01-01", "u": "x"},
                     "Yes, the Kraken can land on planets, it touches down planetside on its landing gear.")
        dh5.add_text({"id": "p2", "k": "d", "t": "Hangar news", "d": "2026-01-02", "u": "y"},
                     "The Kraken hangar is big. Much later, unrelated, a planet was mentioned and ships landed.")
        dh5.add_text({"id": "p3", "k": "d", "t": "Kraken lore", "d": "2026-01-03", "u": "z"}, "no landing here")
        cons = dict(dh5.phrase_concepts("Kraken land on planets"))
        case("phrase: word forms and synonyms", {"land", "landed", "landing"} <= cons["land"]
             and {"planets", "planetside"} <= cons["planets"] and "on" not in cons)
        pr = dh5.phrase_search("Kraken land on planets")
        case("phrase: every concept required", {d["id"] for d in pr} == {"p1", "p2"})
        texts = {"p1": "Yes, the Kraken can land on planets, it touches down planetside on its landing gear.",
                 "p2": "The Kraken hangar is big. " + "filler " * 80 + "a planet was mentioned and ships landed."}
        files["devposts/p1.txt"], files["devposts/p2.txt"] = texts["p1"].encode(), texts["p2"].encode()
        for d in pr:
            d["p"] = f"devposts/{d['id']}.txt"
        ranked = dh5.phrase_rank(pr, "Kraken land on planets")
        case("phrase: said-together ranks first, with its span",
             ranked[0]["id"] == "p1" and 0 < ranked[0]["near"] <= 6 and ranked[1]["near"] > 60)
        case("phrase: excerpt is where it is said",
             "Kraken can land on planets" in dh5.phrase_lines(ranked[0], "Kraken land on planets", 1)[0])

        seen = []

        def cond(path, etag):
            seen.append(etag)
            return (None, etag) if etag == "E1" else (files[path], "E1")
        clock = [time.time()]
        dh6 = DevHistory(fetch=fake, fetch_cond=cond, cache=Path(tmp) / "five", now=lambda: clock[0])
        clock[0] += INDEX_MAX_AGE_S + 5
        dh6.index()
        case("stale index: conditional GET without an etag the first time, etag stored",
             seen == [""] and (Path(tmp) / "five" / (INDEX_PATH + ".etag")).read_text() == "E1")
        dh7 = DevHistory(fetch=fake, fetch_cond=cond, cache=Path(tmp) / "five", now=lambda: clock[0])
        clock[0] += INDEX_MAX_AGE_S + 5
        n7 = dh7.index()["n_docs"]
        local = Path(tmp) / "five" / INDEX_PATH
        case("unchanged index: 304 keeps the copy and restarts its age clock",
             seen == ["", "E1"] and n7 == 4 and abs(local.stat().st_mtime - clock[0]) < 2)
    print("sc_dev_history selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--selftest" in args:
        sys.exit(_selftest())
    if not args:
        print(__doc__)
        sys.exit(2)
    if "--monthly" in args:
        for r in DevHistory().monthly_reports():
            print(f"{r['month']}  {r['series']:<13} {r['t']}\n    {r['url']}{'' if r.get('p') else '  (summary only)'}")
        sys.exit(0)
    want_lines = "--lines" in args
    q = " ".join(a for a in args if not a.startswith("--"))
    dh = DevHistory()
    for r in dh.search(q):
        print(f"{r['d'] or '????-??-??'}  [{kind_label(r)}]  {r['t']}\n    {r['url']}")
        if want_lines:
            for l in dh.lines(r, q):
                print(f"      > {l[:160]}")
    print("\n" + ATTRIBUTION)
