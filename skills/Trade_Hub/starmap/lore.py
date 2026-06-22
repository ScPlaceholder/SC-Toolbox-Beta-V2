"""Location lore — text + a streamed picture from the Star Citizen wiki.

Lore is pulled on demand from starcitizen.tools (MediaWiki API): the intro
extract + a thumbnail image URL, by location/body name (the wiki resolves
redirects, so 'GrimHex' -> 'Grim HEX' etc.). Text is cached to disk (tiny);
images are streamed one at a time and kept only in memory for the session, so
we never bulk-save thousands of pictures. All network work runs off the UI
thread; the result is delivered via a Qt signal.
"""
from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
import urllib.request
from typing import List, Optional

from PySide6.QtCore import QByteArray, QObject, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout,
)

from shared.qt.theme import P

from .ui import make_close_button

_WIKI = "https://starcitizen.tools/api.php"
_UA = "WingmanAI-TradeHub-StarMap/1.0"
_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "trade_hub")
_CACHE_PATH = os.path.join(_CACHE_DIR, "lore_cache.json")
_CACHE_VERSION = 2          # bump to invalidate cached entries (e.g. when the schema grows)
_THUMB_PX = 520
_IMG_CAP = 60

# Map a location/body name -> the wiki article that actually holds its lore, for
# places that have no article of their own (or an easter-egg stand-in).
_TITLE_ALIASES = {
    "cig headquarters": "Roberts Space Industries",
}

_lock = threading.Lock()
_lore_mem: Optional[dict] = None      # disk text cache {name_lower: info|None}
_img_mem: dict = {}                   # url -> bytes (session only)


# ── network ─────────────────────────────────────────────────────────────────
def _http_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _http_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def _sources(extlinks) -> List[dict]:
    """The RSI comm-link articles a wiki page cites — deeper canonical lore.
    Labels derived from the URL slug ('16762-On-The-Run-Pt-2' -> 'On The Run Pt 2')."""
    out: List[dict] = []
    seen = set()
    for el in extlinks:
        u = el.get("*") or ""
        if "comm-link" not in u or u in seen:
            continue
        slug = u.rstrip("/").split("/")[-1]
        label = re.sub(r"^\d+-", "", slug).replace("-", " ").strip()
        if not label:
            continue
        seen.add(u)
        out.append({"label": label, "url": u})
        if len(out) >= 6:
            break
    return out


def _query(title: str) -> Optional[dict]:
    q = urllib.parse.urlencode({
        "action": "query", "prop": "extracts|pageimages|extlinks",
        "exintro": 1, "explaintext": 1, "exlimit": "max",
        "piprop": "thumbnail", "pithumbsize": _THUMB_PX,
        "ellimit": "max", "redirects": 1,
        "format": "json", "titles": title,
    })
    d = _http_json(f"{_WIKI}?{q}")
    for pg in (d.get("query") or {}).get("pages", {}).values():
        if "missing" in pg:
            continue
        ex = (pg.get("extract") or "").strip()
        if not ex:
            continue
        return {"title": pg.get("title") or title, "extract": ex,
                "image_url": (pg.get("thumbnail") or {}).get("source"),
                "sources": _sources(pg.get("extlinks", []))}
    return None


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _search_title(name: str) -> Optional[str]:
    """Fallback: ask the wiki search for the closest article title."""
    q = urllib.parse.urlencode({"action": "query", "list": "search",
                                "srsearch": name, "srlimit": 3, "format": "json"})
    d = _http_json(f"{_WIKI}?{q}")
    want = _norm(name)
    for hit in (d.get("query") or {}).get("search", []):
        t = hit.get("title", "")
        nt = _norm(t)
        if want and (want in nt or nt in want):   # guard against unrelated hits
            return t
    return None


def wiki_lore(name: str) -> Optional[dict]:
    """{title, extract, image_url} for a location/body, or None if none exists.
    Raises on network failure (so the caller can avoid caching a transient miss)."""
    info = _query(name)
    if info is None:
        alt = _search_title(name)
        if alt and _norm(alt) != _norm(name):
            info = _query(alt)
    return info


# ── caches ──────────────────────────────────────────────────────────────────
def _load_cache() -> dict:
    global _lore_mem
    if _lore_mem is None:
        try:
            with open(_CACHE_PATH, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            _lore_mem = raw.get("e", {}) if raw.get("v") == _CACHE_VERSION else {}
        except (OSError, json.JSONDecodeError, AttributeError):
            _lore_mem = {}
    return _lore_mem


def _save_cache() -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = _CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": _CACHE_VERSION, "e": _lore_mem}, fh)
        os.replace(tmp, _CACHE_PATH)
    except OSError:
        pass


def cached_lore(name: str) -> Optional[dict]:
    title = _TITLE_ALIASES.get(name.strip().lower(), name)
    key = title.strip().lower()       # cache + query under the aliased title
    with _lock:
        cache = _load_cache()
        if key in cache:
            return cache[key]
    info = wiki_lore(title)           # network (outside the lock); may raise
    with _lock:
        _load_cache()[key] = info     # cache genuine misses (None) too
        _save_cache()
    return info


def cached_image(url: str) -> Optional[bytes]:
    if not url:
        return None
    with _lock:
        if url in _img_mem:
            return _img_mem[url]
    data = _http_bytes(url)           # stream (outside the lock); may raise
    with _lock:
        if len(_img_mem) >= _IMG_CAP:
            _img_mem.pop(next(iter(_img_mem)))   # evict oldest (session cap)
        _img_mem[url] = data
    return data


# ── async fetcher ───────────────────────────────────────────────────────────
class LoreFetcher(QObject):
    """Fetches lore text + image bytes off the UI thread; delivers via `done`."""
    done = Signal(str, object)        # (requested_name, info|{"error":True}|None)

    def fetch(self, name: str) -> None:
        threading.Thread(target=self._work, args=(name,), daemon=True,
                         name="LoreFetch").start()

    def _work(self, name: str) -> None:
        try:
            info = cached_lore(name)
        except Exception:
            self.done.emit(name, {"error": True})
            return
        if info:
            info = dict(info)
            try:
                info["image_bytes"] = cached_image(info.get("image_url"))
            except Exception:
                info["image_bytes"] = None
        self.done.emit(name, info)


# ── the bubble ──────────────────────────────────────────────────────────────
class LoreBubble(QDialog):
    """Single floating lore card: name + streamed picture + scrollable text +
    an exit button. The owner keeps just one alive at a time."""
    closed = Signal(object)

    def __init__(self, req_name: str, display_name: str, parent=None) -> None:
        super().__init__(parent)
        self._req_name = req_name
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(False)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame()
        card.setObjectName("loreCard")
        card.setStyleSheet(
            f"#loreCard{{background:{P.bg_card}; border:1px solid {P.accent}; border-radius:12px;}}")
        outer.addWidget(card)
        lay = QVBoxLayout(card)
        lay.setContentsMargins(16, 13, 16, 14)
        lay.setSpacing(8)

        hdr = QHBoxLayout()
        hdr.setSpacing(8)
        self._title = QLabel(display_name)
        self._title.setWordWrap(True)
        self._title.setStyleSheet(f"color:{P.fg_bright}; font-size:18px; font-weight:bold;")
        hdr.addWidget(self._title, 1)
        hdr.addWidget(make_close_button(self.close), 0, Qt.AlignTop)
        lay.addLayout(hdr)

        src = QLabel("lore · starcitizen.tools")
        src.setStyleSheet(f"color:{P.fg_dim}; font-size:11px;")
        lay.addWidget(src)

        self._img = QLabel()
        self._img.setAlignment(Qt.AlignCenter)
        self._img.setVisible(False)
        lay.addWidget(self._img)

        self._txt = QTextEdit()
        self._txt.setReadOnly(True)
        self._txt.setFixedWidth(396)
        self._txt.setMinimumHeight(110)
        self._txt.setMaximumHeight(340)
        self._txt.setStyleSheet(
            f"QTextEdit{{background:transparent; color:{P.fg}; border:none; "
            f"font-size:15px; line-height:140%;}}")
        self._txt.setPlainText("Loading lore…")
        lay.addWidget(self._txt)

        self._sources = QLabel()
        self._sources.setTextFormat(Qt.RichText)
        self._sources.setOpenExternalLinks(True)
        self._sources.setWordWrap(True)
        self._sources.setVisible(False)
        self._sources.setStyleSheet(f"color:{P.fg_dim}; font-size:13px;")
        lay.addWidget(self._sources)

        self.setFixedWidth(430)

    def apply(self, info) -> None:
        if isinstance(info, dict) and info.get("error"):
            self._txt.setPlainText("Couldn't load lore right now — check your "
                                   "connection and try again.")
            self._img.setVisible(False)
            self._sources.setVisible(False)
            return
        if not info or not info.get("extract"):
            self._txt.setPlainText("No lore entry found for this location.")
            self._img.setVisible(False)
            self._sources.setVisible(False)
            return
        self._title.setText(info.get("title") or self._title.text())
        self._txt.setPlainText(info["extract"])
        data = info.get("image_bytes")
        if data:
            im = QImage()
            if im.loadFromData(QByteArray(data)):
                pm = QPixmap.fromImage(im)
                if pm.width() > 396:
                    pm = pm.scaledToWidth(396, Qt.SmoothTransformation)
                if pm.height() > 260:
                    pm = pm.scaledToHeight(260, Qt.SmoothTransformation)
                self._img.setPixmap(pm)
                self._img.setVisible(True)
        srcs = info.get("sources") or []
        if srcs:
            head = (f"<span style='color:{P.fg_dim};'>"
                    f"<b>Further reading</b> · comm-links</span><br>")
            body = "<br>".join(
                f"↗ <a href=\"{s['url']}\" style='color:#5cc8ff; "
                f"text-decoration:none;'>{s['label']}</a>" for s in srcs)
            self._sources.setText(head + body)
            self._sources.setVisible(True)
        else:
            self._sources.setVisible(False)
        self.adjustSize()

    def closeEvent(self, ev) -> None:
        self.closed.emit(self)
        super().closeEvent(ev)
