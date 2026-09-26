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
* a fetch function with a byte-progress callback, so the UI can show the ~3.5 MB
  first-run index download instead of an unexplained wait;
* a lock around the first index load, because search and excerpt workers can
  race to trigger it;
* ``describe_error`` — turns urllib/network exceptions into one plain sentence.
"""
from __future__ import annotations

import importlib
import logging
import os
import socket
import sys
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

ATTRIBUTION: str = engine.ATTRIBUTION
INDEX_PATH: str = engine.INDEX_PATH
INDEX_SIZE_HINT = "~3.5 MB"

ProgressCb = Callable[[int, int], None]   # (bytes_received, bytes_total or 0)


def describe_error(exc: BaseException, ref: str = "") -> str:
    """One human sentence for a failure to reach GitHub."""
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

    def __init__(self, ref: str = "corpus", cache: Optional[str] = None) -> None:
        self.ref = ref
        self.cache = cache or str(engine.CACHE)
        self.progress_cb: Optional[ProgressCb] = None
        self._lock = threading.Lock()
        self._dh = engine.DevHistory(fetch=self._fetch, cache=engine.Path(self.cache), ref=ref)

    # ── network ──────────────────────────────────────────────────────────────

    def _fetch(self, path: str) -> bytes:
        url = engine.raw_url(path, self.ref)
        req = urllib.request.Request(url, headers={"User-Agent": "SC-Toolbox-DevHistory/1"})
        with urllib.request.urlopen(req, timeout=30.0) as r:
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
        return b"".join(chunks)

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
        """Load (downloading if needed).  Returns ``{"n_docs": int}``."""
        with self._lock:
            idx = self._dh.index()
        return {"n_docs": int(idx.get("n_docs", len(idx.get("docs", []))))}

    def is_loaded(self) -> bool:
        return self._dh._index is not None

    # ── queries ──────────────────────────────────────────────────────────────

    def search(self, query: str, n: int = 50) -> list[dict]:
        with self._lock:
            self._dh.index()
        return self._dh.search(query, n)

    def excerpts(self, doc: dict, query: str, n: int = 3) -> list[str]:
        """Matching 30-word excerpts; fetches that one transcript on demand."""
        return self._dh.lines(doc, query, n)

    @staticmethod
    def query_terms(query: str) -> list[str]:
        return engine.WORD.findall((query or "").lower())
