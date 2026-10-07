"""Thin service around the shared ``sc_dev_history`` engine.

The engine (``sc_dev_history.py``, stdlib only) is IMPORTED, not re-implemented.
It is looked up in this order:

1. already importable (the tool folder is on ``sys.path`` via ``bootstrap_skill``,
   so a deployed copy sitting next to ``dev_history_app.py`` is found here);
2. the folder above the tool (the staging layout
   ``toolbox_port/dev_history/sc_dev_history.py`` + ``.../Dev_History/``);
3. the folder named by env ``SC_DEV_HISTORY_ENGINE_DIR``.

What this module adds on top of the engine — nothing that changes a result:

* the git ref comes from the tool's settings (default ``"corpus"``), passed to
  ``DevHistory(ref=...)``;
* fetch functions with a byte-progress callback, so the UI can show the first-run
  index download instead of an unexplained wait; the index re-check is a conditional
  request (ETag), so on most days it downloads nothing;
* a lock around the first index load, because search and excerpt workers can
  race to trigger it;
* ``describe_error`` — turns urllib/network exceptions into one plain sentence;
* LIVE fallbacks straight from robertsspaceindustries.com, one request per click,
  for what the archive does not have yet: a comm-link's article text (plus a digest
  of it), the latest Devtracker pages, and a dev post's full text. They use
  ``core/rsi.py`` (a copy of the archiver's parser module) and are cached under
  ``<cache>/live/`` so nothing is fetched twice. Settings key ``live_rsi`` turns
  them off.
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import logging
import os
import re
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

log = logging.getLogger(__name__)

_TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_engine():
    try:
        return importlib.import_module("sc_dev_history")
    except ImportError:
        pass
    candidates = [os.path.dirname(_TOOL_DIR)]
    env_dir = os.environ.get("SC_DEV_HISTORY_ENGINE_DIR")
    if env_dir:
        candidates.insert(0, env_dir)
    for d in candidates:
        if os.path.isfile(os.path.join(d, "sc_dev_history.py")):
            if d not in sys.path:
                sys.path.append(d)
            return importlib.import_module("sc_dev_history")
    raise ImportError(
        "sc_dev_history.py not found — copy it into the Dev_History tool folder "
        "(next to dev_history_app.py)")


engine = _load_engine()

try:                                    # the tool folder is on sys.path (bootstrap_skill)
    from core import rsi                # noqa: E402
except ImportError:                     # imported from elsewhere (tests, companions)
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rsi                          # noqa: E402

RSI_USER_AGENT = "SC-Toolbox-DevHistory/1 (+https://github.com/ScPlaceholder/sc-dev-history)"


class LiveError(Exception):
    """A live RSI fetch failed; the message is already a plain sentence."""

ATTRIBUTION: str = engine.ATTRIBUTION
INDEX_PATH: str = engine.INDEX_PATH
INDEX_SIZE_HINT = "a few MB"
kind_label = engine.kind_label

ProgressCb = Callable[[int, int], None]   # (bytes_received, bytes_total or 0)


def describe_error(exc: BaseException, ref: str = "") -> str:
    """One human sentence for a failure to reach GitHub."""
    if isinstance(exc, LiveError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 404:
            return (f"Not found on GitHub (HTTP 404) — does branch '{ref}' of "
                    f"{engine.REPO} exist? Check 'ref' in settings.json.")
        return f"GitHub returned HTTP {exc.code} ({exc.reason})."
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return "Timed out reaching GitHub — you may be offline."
        return f"Could not reach GitHub — you appear to be offline ({reason})."
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "Timed out reaching GitHub — you may be offline."
    if isinstance(exc, OSError):
        return f"Network or disk error: {exc}"
    return f"{type(exc).__name__}: {exc}"


class DevHistoryService:
    """Owns one ``engine.DevHistory``; safe to call from worker threads."""

    def __init__(self, ref: str = "corpus", cache: Optional[str] = None, live: bool = True) -> None:
        self.ref = ref
        self.cache = cache or str(engine.CACHE)
        self.live = live
        self.progress_cb: Optional[ProgressCb] = None
        self._lock = threading.Lock()
        self._live_lock = threading.Lock()
        self._rsi = rsi.Client(delay=0.3, timeout=20.0, retries=1, user_agent=RSI_USER_AGENT)
        self._dh = engine.DevHistory(fetch=self._fetch, fetch_cond=self._fetch_cond,
                                     cache=engine.Path(self.cache), ref=ref)

    # ── network ──────────────────────────────────────────────────────────────

    def _fetch(self, path: str) -> bytes:
        return self._fetch_cond(path, "")[0] or b""

    def _fetch_cond(self, path: str, etag: str) -> tuple[Optional[bytes], str]:
        """GET with an optional If-None-Match; ``(None, etag)`` means 304 Not Modified."""
        url = engine.raw_url(path, self.ref)
        headers = {"User-Agent": "SC-Toolbox-DevHistory/1"}
        if etag:
            headers["If-None-Match"] = etag
        req = urllib.request.Request(url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=30.0)
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None, etag
            raise
        with resp as r:
            new_etag = r.headers.get("ETag") or ""
            try:
                total = int(r.headers.get("Content-Length") or 0)
            except ValueError:
                total = 0
            chunks: list[bytes] = []
            got = 0
            cb = self.progress_cb if path == INDEX_PATH else None
            while True:
                block = r.read(64 * 1024)
                if not block:
                    break
                chunks.append(block)
                got += len(block)
                if cb:
                    cb(got, total)
        return b"".join(chunks), new_etag

    # ── index ────────────────────────────────────────────────────────────────

    def index_file(self) -> str:
        return os.path.join(self.cache, *INDEX_PATH.split("/"))

    def index_state(self) -> str:
        """``"missing"`` (first run: will download), ``"stale"`` (will re-check
        GitHub, falls back to the cached copy offline) or ``"fresh"``."""
        p = self.index_file()
        if not os.path.isfile(p):
            return "missing"
        age = time.time() - os.path.getmtime(p)
        return "fresh" if age < engine.INDEX_MAX_AGE_S else "stale"

    def load_index(self) -> dict:
        """Load (downloading if needed), then fold in what was fetched live on earlier runs, so it is searchable.
        Returns ``{"n_docs": int}``."""
        with self._lock:
            idx = self._dh.index()
            n_published = int(idx.get("n_docs", len(idx.get("docs", []))))
        self._merge_local()
        return {"n_docs": n_published}

    def is_loaded(self) -> bool:
        return self._dh._index is not None

    # ── queries ──────────────────────────────────────────────────────────────

    def search(self, query: str, n: int = 50) -> list[dict]:
        with self._lock:                # a worker may be merging a freshly fetched text right now
            self._dh.index()
            return self._dh.search(query, n)

    # Search by phrase ────────────────────────────────────────────────────────

    def phrase_search(self, query: str) -> list[dict]:
        """Every record that has all the phrase's concepts (word forms + synonyms), best first."""
        with self._lock:
            self._dh.index()
            return self._dh.phrase_search(query, None)

    def phrase_terms(self, query: str) -> list[str]:
        """All the words that count for the phrase (for highlighting)."""
        with self._lock:
            return sorted({t for _w, terms in self._dh.phrase_concepts(query) for t in terms}, key=len,
                          reverse=True)

    def phrase_concepts(self, query: str) -> list:
        with self._lock:
            return self._dh.phrase_concepts(query)

    def phrase_excerpts(self, doc: dict, query: str, n: int = 3) -> list[str]:
        """Where the document comes closest to saying the phrase. Network (one text): worker only."""
        return self._dh.phrase_lines(doc, query, n)

    def phrase_rank(self, docs: list[dict], query: str, limit: int = 40) -> list[dict]:
        """Re-rank the first *limit* results by how close together the phrase is said. Network: worker only."""
        return self._dh.phrase_rank([dict(d) for d in docs], query, limit)

    @property
    def last_total(self) -> int:
        """How many records the last search matched in all (it returns at most ``n`` of them)."""
        return self._dh.last_total

    def excerpts(self, doc: dict, query: str, n: int = 3) -> list[str]:
        """Matching 30-word excerpts; fetches that one text on demand."""
        return self._dh.lines(doc, query, n)

    def prefetch(self, docs: list[dict]) -> int:
        """Download (into the cache) the archive texts of *docs* that are not cached yet, so opening them is
        instant. Archive (GitHub) texts only, never RSI. Returns how many were fetched. Worker thread only."""
        n = 0
        for d in docs:
            path = d.get("p")
            if not path or os.path.isfile(os.path.join(self.cache, *path.split("/"))):
                continue
            try:
                self._dh.text(d)
                n += 1
            except (OSError, urllib.error.URLError) as exc:     # best effort: the click will retry and report
                log.info("dev_history: prefetch of %s skipped: %s", path, exc)
        return n

    def full_text(self, doc: dict) -> str:
        """The whole article / post / transcript ('' when the corpus only has a summary). Network: worker only."""
        return self._dh.text(doc)

    def monthly_reports(self) -> list[dict]:
        """Every Monthly Report in the loaded index (in-memory; call after load_index). Each has "have": True when
        its text is on hand, from the archive or fetched live earlier."""
        with self._lock:
            self._dh.index()
            reports = self._dh.monthly_reports()
        for r in reports:
            r["have"] = bool(r.get("p")) or self._commlink_cache(r) is not None
        return reports

    @staticmethod
    def query_terms(query: str) -> list[str]:
        return engine.WORD.findall((query or "").lower())

    # ── live RSI fallbacks (worker threads only) ─────────────────────────────

    def _live_path(self, *parts: str) -> str:
        return os.path.join(self.cache, "live", *parts)

    def _read(self, path: str) -> Optional[str]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return None

    def _write(self, path: str, text: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Two workers may write the same file at once: each writes its own temp file, then swaps it in.
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=os.path.basename(path) + ".", suffix=".part")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

    def _rsi_call(self, what: str, fn):
        if not self.live:
            raise LiveError(f"{what} is not in the archive yet, and live RSI fetching is off "
                            f"(\"live_rsi\" in settings.json).")
        try:
            return fn()
        except rsi.PrivateForum:
            raise
        except urllib.error.HTTPError as exc:
            raise LiveError(f"robertsspaceindustries.com answered HTTP {exc.code} for {what}.") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LiveError(f"Could not reach robertsspaceindustries.com for {what} — you may be offline.") from exc
        except (ValueError, RuntimeError) as exc:
            raise LiveError(f"RSI sent something unexpected for {what} ({exc}). The page may have changed.") from exc

    @staticmethod
    def _commlink_num(doc: dict) -> str:
        m = re.search(r"/comm-link/[\w-]+/(\d+)-", doc.get("u") or doc.get("url") or doc.get("id") or "")
        return m.group(1) if m else ""

    def _commlink_cache(self, doc: dict) -> Optional[str]:
        num = self._commlink_num(doc)
        path = self._live_path("commlinks", num + ".txt") if num else ""
        return path if path and os.path.isfile(path) else None

    def _merge(self, doc: dict, text: str) -> None:
        with self._lock:
            self._dh.add_text(doc, text)

    @staticmethod
    def _post_doc(p: dict) -> dict:
        """A live Devtracker post as an index document (kind "d")."""
        return {"id": p["id"], "k": "d", "t": p.get("thread") or "(Spectrum post)", "d": p.get("date", ""),
                "s": p.get("teaser", ""), "u": p.get("url", ""), "a": p.get("author", ""),
                "c": p.get("category", ""), "live": 1, "slug": p.get("slug", ""), "reply_id": p.get("reply_id", "")}

    def _merge_local(self) -> None:
        """Everything fetched live on earlier runs: comm-link texts and Devtracker posts (+ their texts)."""
        with self._lock:
            idx = self._dh.index()
            by_num = {}
            for d in idx["docs"]:
                if d.get("k") == "c" and not d.get("p"):
                    num = self._commlink_num(d)
                    if num:
                        by_num[num] = d
        folder = self._live_path("commlinks")
        for name in os.listdir(folder) if os.path.isdir(folder) else []:
            d = by_num.get(name[:-4]) if name.endswith(".txt") else None
            if d is not None:
                self._merge(d, self._read(os.path.join(folder, name)) or "")
        with self._live_lock:
            live = self._load_live_posts()
        for p in live:
            if p.get("id"):
                body = self._read(self._live_path("devposts", p["id"] + ".txt")) or ""
                self._merge(self._post_doc(p),
                            " ".join([p.get("author", ""), p.get("category", ""), p.get("teaser", ""), body]))

    def article(self, doc: dict) -> dict:
        """A comm-link's text and a digest of what it says.
        Returns {"text", "summary", "source"}: source is "archive", "rsi" (fetched just now) or "cached"."""
        title = doc.get("t", "")
        if doc.get("p"):
            text = self._dh.text(doc)
            summary = doc.get("s", "") if doc.get("sd") else rsi.summarize(text, title)
            return {"text": text, "summary": summary or rsi.summarize(text, title), "source": "archive"}
        url = doc.get("u") or doc.get("url") or ""
        m = re.search(r"/comm-link/[\w-]+/(\d+)-", url)
        if not m:
            return {"text": "", "summary": doc.get("s", ""), "source": "none"}
        path = self._live_path("commlinks", m.group(1) + ".txt")
        text = self._read(path)
        source = "cached"
        if text is None:
            text = self._rsi_call("this comm-link", lambda: rsi.commlink_body(self._rsi, url))
            source = "rsi"
            if text.strip():
                self._write(path, text)
        if text.strip():
            self._merge(doc, text)      # now searchable, this session and (from the cache) every later one
        return {"text": text, "summary": rsi.summarize(text, title) or doc.get("s", ""), "source": source}

    # Dev Tracker ─────────────────────────────────────────────────────────────

    def _live_posts_file(self) -> str:
        return self._live_path("devtracker.json")

    def _load_live_posts(self) -> list[dict]:
        raw = self._read(self._live_posts_file())
        if not raw:
            return []
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("dev_history: %s is corrupt; starting it over", self._live_posts_file())
            return []
        return data if isinstance(data, list) else []

    def devtracker_posts(self) -> list[dict]:
        """Every dev post we know of: the archive's (index kind "d") plus those seen live, newest first.
        In-memory plus one small local file: fine on the UI thread once the index is loaded."""
        with self._lock:
            idx = self._dh.index()
        posts: dict[str, dict] = {}
        for d in idx.get("docs", []):
            if d.get("k") == "d" and not d.get("live"):
                posts[d["id"]] = {"id": d["id"], "thread": d.get("t", ""), "author": d.get("a", ""),
                                  "category": d.get("c", ""), "date": d.get("d", ""), "teaser": d.get("s", ""),
                                  "url": d.get("u", ""), "p": d.get("p", ""), "archived": True,
                                  "slug": d.get("slug", ""), "reply_id": d.get("reply_id", "")}
        with self._live_lock:
            live = self._load_live_posts()
        for p in live:
            if not p.get("id"):
                continue
            have = posts.get(p["id"])
            if have is None:
                posts[p["id"]] = dict(p, archived=False)
            elif not have.get("slug") and p.get("slug"):   # older index without slugs: borrow the live one
                have["slug"], have["reply_id"] = p["slug"], p.get("reply_id", "")
        return sorted(posts.values(), key=lambda p: (p.get("date") or "", p.get("time") or 0, p["id"]),
                      reverse=True)

    def devtracker_fetch(self, page: int, day: str) -> dict:
        """One live Devtracker page from RSI; merged into the local list. Returns {"posts", "day", "new"}."""
        found, day = self._rsi_call("the Devtracker", lambda: rsi.tracker_page(self._rsi, page, day))
        with self._live_lock:
            live = self._load_live_posts()
            have = {p["id"] for p in live}
            new = [p for p in found if p["id"] not in have]
            if new:
                live = sorted(live + new, key=lambda p: (p.get("date") or "", p["id"]), reverse=True)
                self._write(self._live_posts_file(), json.dumps(live, ensure_ascii=False))
        for p in new:
            self._merge(self._post_doc(p), f"{p.get('author', '')} {p.get('category', '')} {p.get('teaser', '')}")
        return {"posts": found, "day": day, "new": len(new)}

    # Bulk jobs (worker thread; ``progress(done, total)``; stop when ``stop()`` is true) ──────────────────────

    def _history_state(self) -> dict:
        raw = self._read(self._live_path("devtracker_state.json"))
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            return {}

    def tracker_history_status(self) -> dict:
        """{"page": next page to fetch, "done": bool} for the full-history walk (resumes across runs)."""
        st = self._history_state()
        return {"page": int(st.get("page") or 1), "done": bool(st.get("done"))}

    def fetch_tracker_history(self, progress: Callable[[int, int], None], stop: Callable[[], bool],
                              expected_pages: int = 911) -> dict:
        """Walk the whole Devtracker, resuming where the last walk stopped. It goes back to Spectrum's launch in
        February 2017: about 900 pages of 18 posts. Returns {"pages", "new", "done"}."""
        st = self._history_state()
        page = int(st.get("page") or 1)
        day = st.get("day") or (dt.date.today() + dt.timedelta(days=1)).isoformat()
        pages = new = 0
        done = False
        while not stop():
            res = self.devtracker_fetch(page, day)
            if not res["posts"]:
                done = True
                break
            pages += 1
            new += res["new"]
            day = res["day"] or day
            page += 1
            self._write(self._live_path("devtracker_state.json"), json.dumps({"page": page, "day": day}))
            progress(page - 1, max(expected_pages, page))
        if done:
            self._write(self._live_path("devtracker_state.json"),
                        json.dumps({"page": page, "day": day, "done": True}))
        return {"pages": pages, "new": new, "done": done}

    def fetch_all_reports(self, progress: Callable[[int, int], None], stop: Callable[[], bool]) -> dict:
        """Fetch every Monthly Report the archive does not have yet from RSI (cached, and searchable after).
        Returns {"fetched", "failed", "remaining"}."""
        todo = [r for r in self.monthly_reports() if not r["have"]]
        fetched = failed = 0
        for i, r in enumerate(todo):
            if stop():
                break
            try:
                art = self.article(r)
                if art.get("text", "").strip():
                    fetched += 1
                else:
                    failed += 1
            except LiveError as exc:
                failed += 1
                log.warning("dev_history: monthly report %s: %s", r.get("t"), exc)
            progress(i + 1, len(todo))
        return {"fetched": fetched, "failed": failed, "remaining": len(todo) - fetched - failed}

    def devpost_text(self, post: dict) -> dict:
        """Full text of a dev post: {"text", "source", "private"}."""
        if post.get("p"):
            return {"text": self._dh.text({"p": post["p"]}), "source": "archive", "private": False}
        path = self._live_path("devposts", post["id"] + ".txt")
        text = self._read(path)
        if text is not None:
            return {"text": text, "source": "cached", "private": False}
        if not post.get("slug"):
            return {"text": post.get("teaser", ""), "source": "teaser", "private": False}
        try:
            got = self._rsi_call("this Spectrum post", lambda: rsi.devpost(self._rsi, post))
        except rsi.PrivateForum:
            return {"text": post.get("teaser", ""), "source": "teaser", "private": True}
        if got["text"].strip():
            self._write(path, got["text"])
            self._merge(self._post_doc(post), got["text"])
        return {"text": got["text"] or post.get("teaser", ""), "source": "rsi", "private": False}
