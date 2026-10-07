"""rsi.py - the small, polite HTTP client, HTML/JSON parsers and summarizer shared by the RSI collectors.

Stdlib only (the GitHub Action installs nothing). Everything that knows the SHAPE of an RSI response lives here, so
when RSI changes a page there is one file to fix and one selftest to update. SC Toolbox's Dev History tool ships an
identical copy (tools/Dev_History/core/rsi.py) for its on-demand fetches: keep the two in sync.

    python tools/rsi.py --selftest

Endpoints (checked against the live site 2026-09-27, all anonymous, no login or token):
  * POST /api/hub/getCommlinkItems      comm-link listing, newest first; data = HTML of <a> cards
  * GET  /comm-link/<type>/<id>-<slug>  article page. Newer articles load their body from an "alexandria" URL
                                        named in an inline `const s3Url = '...'`; older ones render it in #post .segment
  * POST /api/community/getTrackedPosts the Devtracker listing; data.html = <h3> day headers + <a class="devpost"> cards
  * POST /api/spectrum/forum/thread/nested  a Spectrum thread with its replies (full text of a tracked post).
                                        Private forums (e.g. Focus Testing) answer ErrPermissionDenied: we keep the
                                        public Devtracker teaser for those and never try to get around it.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Iterable, Optional

BASE = "https://robertsspaceindustries.com"
USER_AGENT = "sc-dev-history-archiver/1 (+https://github.com/ScPlaceholder/sc-dev-history)"


# ---- HTTP ----------------------------------------------------------------------------------------------------------
class Client:
    """Sequential, rate-limited, retrying. One request at a time on purpose: this runs once a day and is not in a
    hurry, and RSI should never notice it."""

    def __init__(self, delay: float = 1.0, timeout: float = 30.0, retries: int = 3, opener=None,
                 user_agent: str = USER_AGENT):
        self.user_agent = user_agent
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self._last = 0.0
        self._open = opener or urllib.request.urlopen
        self.requests = 0

    def _wait(self) -> None:
        gap = self.delay - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()

    def request(self, path: str, body=None, headers: Optional[dict] = None) -> bytes:
        url = path if path.startswith("http") else BASE + path
        data = json.dumps(body).encode() if body is not None else None
        extra = headers or {}
        headers = {"User-Agent": self.user_agent, "Accept-Language": "en"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        headers.update(extra)
        last_exc: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            self._wait()
            self.requests += 1
            try:
                req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
                with self._open(req, timeout=self.timeout) as r:
                    return r.read()
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code == 404:
                    raise
                if exc.code == 429 or exc.code >= 500:      # back off, then retry
                    time.sleep(min(120, 10 * (2 ** attempt)))
                    continue
                raise
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                last_exc = exc
                time.sleep(5 * (attempt + 1))
        assert last_exc is not None
        raise last_exc

    def json(self, path: str, body, headers: Optional[dict] = None):
        return json.loads(self.request(path, body, headers).decode("utf-8", errors="replace"))

    def text(self, path: str) -> str:
        return self.request(path).decode("utf-8", errors="replace")


# ---- HTML -> text --------------------------------------------------------------------------------------------------
_BLOCK = {"p", "div", "section", "article", "li", "ul", "ol", "br", "tr", "table", "blockquote", "figure",
          "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "pre"}
_HEAD = {"h1", "h2", "h3", "h4", "h5", "h6"}
_SKIP = {"script", "style", "noscript", "svg", "iframe", "template"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.head = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP:
            self.skip += 1
        elif tag in _BLOCK:
            self.out.append("\n")
            if tag in _HEAD:
                self.head += 1
                self.out.append("## ")
            elif tag == "li":
                self.out.append("- ")

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _BLOCK:
            if tag in _HEAD:
                self.head = max(0, self.head - 1)
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_to_text(fragment: str) -> str:
    """Readable plain text: one paragraph per line, headings as '## Heading', list items as '- item'."""
    p = _Text()
    p.feed(fragment or "")
    p.close()
    lines = []
    for line in "".join(p.out).split("\n"):
        line = re.sub(r"[ \t\r\f\v ]+", " ", line).strip()
        if line in ("##", "-"):
            continue
        if line:
            lines.append(line)
    return "\n".join(lines)


# ---- comm-link listing -----------------------------------------------------------------------------------------------
_CARD = re.compile(r'<a\b[^>]*?href="(?P<href>/(?:\w\w/)?comm-link/(?P<type>[\w-]+)/(?P<id>\d+)-[^"?#]*)[^"]*"[^>]*>'
                   r'(?P<inner>.*?)</a>', re.S)


def _cls(name: str) -> str:
    """Regex for a class attribute that contains exactly the token *name* ('title' must not match 'title-holder')."""
    return r'class="(?:[^"]*\s)?' + re.escape(name) + r'(?:\s[^"]*)?"'


def _class_text(inner: str, cls: str) -> str:
    m = re.search(r'<div\b[^>]*' + _cls(cls) + r'[^>]*>(.*?)</div>', inner, re.S)
    return html_to_text(m.group(1)).replace("\n", " ").strip() if m else ""


def parse_commlink_cards(fragment: str) -> list[dict]:
    """The HTML in getCommlinkItems' `data`: one <a class="content-block2 ..."> card per comm-link. Cards hold no
    nested links, so each card is simply its <a> up to the first </a> (whatever follows the last card)."""
    out = []
    for m in _CARD.finditer(fragment or ""):
        inner = m.group("inner")
        href = re.sub(r"^/\w\w/", "/", m.group("href"))
        age = re.search(r'<div\b[^>]*' + _cls("time_ago") + r'[^>]*>.*?<span\b[^>]*' + _cls("value") +
                        r'[^>]*>(.*?)</span>', inner, re.S)
        teaser = re.search(r'<div\b[^>]*' + _cls("body") + r'[^>]*>(.*?)</div>', inner, re.S)
        out.append({"id": m.group("id"), "type": m.group("type"), "url": BASE + href,
                    "title": _class_text(inner, "title"),
                    "age_text": html.unescape(age.group(1)).strip() if age else "",
                    "teaser": html_to_text(teaser.group(1)).replace("\n", " ") if teaser else ""})
    return out


def date_from_age(age: str, today: dt.date) -> tuple[str, str]:
    """RSI shows '3 hours ago', '6 days ago', '2 weeks ago', '1 month ago' or an absolute timestamp.
    Returns (YYYY-MM-DD, precision). Hours and days are exact to the day because the collector runs daily."""
    a = (age or "").strip().lower()
    m = re.match(r"(\d{4}-\d\d-\d\d)", a)
    if m:
        return m.group(1), "day"
    if a in ("just now", "now", "today") or re.match(r"(an?|\d+) (second|minute|hour)s? ago", a):
        return today.isoformat(), "day"
    if a == "yesterday":
        return (today - dt.timedelta(days=1)).isoformat(), "day"
    m = re.match(r"(an?|\d+) (day|week|month|year)s? ago", a)
    if m:
        n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        days = {"day": 1, "week": 7, "month": 30, "year": 365}[m.group(2)] * n
        return (today - dt.timedelta(days=days)).isoformat(), "day" if m.group(2) == "day" else "approx"
    return "", "unknown"


# ---- comm-link article body -----------------------------------------------------------------------------------------
_S3 = re.compile(r"""const\s+s3Url\s*=\s*['"]([^'"]+)['"]""")
_SKIP_KEY = re.compile(r"url|src|href|^key$|^id$|class|color|type|style|image|video|link|alt|size|layout|"
                       r"arrangement|position|plugin|options|media|emphasis|cloak", re.I)


def alexandria_url(page_html: str) -> Optional[str]:
    """Newer comm-link pages are an empty shell that fetches their body from this URL."""
    m = _S3.search(page_html)
    return html.unescape(m.group(1)) if m else None


class _Attrs(HTMLParser):
    """Collect (attribute name, value) pairs in document order."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.pairs: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        for k, v in attrs:
            if v:
                self.pairs.append((k, v))

    handle_startendtag = handle_starttag


def _walk_json(v, key: str, out: list[str]) -> None:
    if isinstance(v, str):
        if _SKIP_KEY.search(key or "") or re.match(r"^\s*(https?:|/)", v):
            return
        t = html_to_text(v) if "<" in v else html.unescape(v).strip()
        if len(t.split()) >= 2:
            out.append(t)
    elif isinstance(v, list):
        for x in v:
            _walk_json(x, key, out)
    elif isinstance(v, dict):
        for k, x in v.items():
            _walk_json(x, k, out)


def alexandria_text(fragment: str) -> str:
    """The alexandria body is Vue components whose text lives in ATTRIBUTES: `body="<h2>..</h2><p>..</p>"` on
    <g-article>, and JSON props such as `:info='{"title":..,"contents":"<p>..</p>"}'`."""
    p = _Attrs()
    p.feed(fragment or "")
    p.close()
    parts: list[str] = []
    for name, value in p.pairs:
        bare = name.lstrip(":")
        if name.startswith(":") or name.startswith("v-bind"):
            try:
                _walk_json(json.loads(value), bare, parts)
            except ValueError:
                continue                            # a JS expression, not JSON: carries no prose
        elif not _SKIP_KEY.search(bare) and ("<" in value or len(value.split()) >= 6):
            t = html_to_text(value) if "<" in value else value.strip()
            if t:
                parts.append(t)
    return "\n".join(parts).strip()


class _Segments(_Text):
    """Text of every <div class="... segment ..."> subtree (matched by real div nesting, not by regex), so the
    sidebars and 'related comm-links' lists around the article never leak into it."""
    _VOID = {"br", "hr", "img", "input", "meta", "link", "source", "wbr", "area", "col", "embed", "param", "track"}

    def __init__(self):
        super().__init__()
        self.depth = 0          # element depth inside the current segment; 0 = not in one

    def handle_starttag(self, tag, attrs):
        if self.depth:
            if tag not in self._VOID:
                self.depth += 1
            super().handle_starttag(tag, attrs)
        elif tag == "div" and "segment" in (dict(attrs).get("class") or "").split():
            self.depth = 1
            self.out.append("\n")

    def handle_endtag(self, tag):
        if self.depth:
            if tag not in self._VOID:
                self.depth -= 1
            if self.depth:
                super().handle_endtag(tag)
            else:
                self.out.append("\n")

    def handle_data(self, data):
        if self.depth:
            super().handle_data(data)


def legacy_body_text(page_html: str) -> str:
    """Older comm-links render the body server-side as #post ... <div class="segment">."""
    p = _Segments()
    p.feed(page_html or "")
    p.close()
    lines = []
    for line in "".join(p.out).split("\n"):
        line = re.sub(r"[ \t\r\f\v ]+", " ", line).strip()
        if line and line not in ("##", "-"):
            lines.append(line)
    return "\n".join(lines)


def commlink_body(client: Client, url: str) -> str:
    page = client.text(url)
    alex = alexandria_url(page)
    if alex:
        return alexandria_text(client.text(alex))
    return legacy_body_text(page)


# ---- Devtracker -----------------------------------------------------------------------------------------------------
_MONTHS = {m: i for i, m in enumerate(["january", "february", "march", "april", "may", "june", "july", "august",
                                        "september", "october", "november", "december"], 1)}


def parse_day_header(text: str) -> str:
    m = re.match(r"\s*([A-Za-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", text or "")
    if not m or m.group(1).lower() not in _MONTHS:
        return ""
    return f"{int(m.group(3)):04d}-{_MONTHS[m.group(1).lower()]:02d}-{int(m.group(2)):02d}"


_TRACK_URL = re.compile(r"/spectrum/community/(?P<community>\w+)/forum/(?P<forum>\d+)/thread/(?P<slug>[^/?#\"]+)"
                        r"(?:/(?P<reply>\d+))?")


def parse_tracked_posts(fragment: str, current_day: str = "") -> tuple[list[dict], str]:
    """The Devtracker HTML: <h3>September 25th 2026</h3> day headers, each followed by that day's
    <a class="devpost" href="/spectrum/community/SC/forum/3/thread/<slug>/<reply id>"> cards.
    Returns (posts, the last day header seen) so paging can carry the day across pages."""
    posts = []
    day = current_day
    for m in re.finditer(r'<h3[^>]*>(?P<h>.*?)</h3>|<a\b(?P<a>[^>]*class="[^"]*\bdevpost\b[^"]*"[^>]*)>(?P<inner>.*?)</a>',
                         fragment or "", re.S):
        if m.group("h") is not None:
            day = parse_day_header(html_to_text(m.group("h"))) or day
            continue
        href = re.search(r'href="([^"]+)"', m.group("a"))
        u = _TRACK_URL.search(href.group(1)) if href else None
        if not u:
            continue
        inner = m.group("inner")

        def cls(name, tag=r"\w+"):
            x = re.search(r'<(' + tag + r')\b[^>]*class="[^"]*\b' + name + r'\b[^"]*"[^>]*>(.*?)</\1>', inner, re.S)
            return html_to_text(x.group(2)).replace("\n", " ").strip() if x else ""
        reply = u.group("reply")
        posts.append({
            "id": reply or f"t-{u.group('forum')}-{u.group('slug')}",
            "reply_id": reply or "",
            "community": u.group("community"), "forum_id": u.group("forum"), "slug": u.group("slug"),
            "url": BASE + href.group(1).split("?")[0],
            "author": cls("nickname", "div"), "category": cls("category", "span"),
            "thread": cls("thread", "span"), "teaser": cls("details", "p"),
            "date": day,
        })
    return posts, day


def spectrum_blocks_text(content_blocks) -> str:
    """Spectrum post bodies are Draft.js blocks: [{type:'text', data:{blocks:[{type,text}]}}, {type:'image'}...]."""
    lines = []
    if isinstance(content_blocks, dict):
        content_blocks = [content_blocks]
    for cb in content_blocks if isinstance(content_blocks, list) else []:
        if not isinstance(cb, dict):
            continue
        data = cb.get("data")
        # Usually {"blocks": [...], "entityMap": ...}; older posts store the block list directly, or nothing ([]).
        blocks = data.get("blocks") if isinstance(data, dict) else data if isinstance(data, list) else []
        for b in blocks or []:
            if not isinstance(b, dict):
                continue
            t = (b.get("text") or "").strip()
            if not t:
                continue
            kind = b.get("type") or ""
            if kind.startswith("header"):
                t = "## " + t
            elif kind.endswith("list-item"):
                t = "- " + t
            lines.append(t)
    return "\n".join(lines)


def _lexical_text(node) -> str:
    """Newer posts may carry Lexical JSON instead of (or as well as) Draft.js blocks."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("text"), str):
                out.append(n["text"])
            kids = n.get("children") or (n.get("root") or {}).get("children") if isinstance(n, dict) else None
            for k in kids or []:
                walk(k)
            if n.get("type") in ("paragraph", "heading", "listitem", "quote"):
                out.append("\n")
        elif isinstance(n, list):
            for k in n:
                walk(k)
    if isinstance(node, str):
        try:
            node = json.loads(node)
        except ValueError:
            return ""
    walk(node)
    return "\n".join(x.strip() for x in "".join(out).split("\n") if x.strip())


def _any_text(node) -> str:
    """Last resort for a shape we have not seen: every "text" string in the tree, in order."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, dict):
            if isinstance(n.get("text"), str) and n["text"].strip():
                out.append(n["text"].strip())
            for v in n.values():
                if isinstance(v, (dict, list)):
                    walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)
    return "\n".join(out)


def post_text(post: dict) -> str:
    return (spectrum_blocks_text(post.get("content_blocks")) or _lexical_text(post.get("lexical_content"))
            or _any_text(post.get("content_blocks")) or (post.get("annotation_plaintext") or "").strip())


def find_reply(thread: dict, reply_id: str) -> Optional[dict]:
    """The thread itself when reply_id is empty or is the opening post, else the reply anywhere in the tree."""
    # A thread's own id is not its opening post's id: the opening post is "content_reply_id".
    if not reply_id or str(reply_id) in (str(thread.get("id")), str(thread.get("content_reply_id"))):
        return thread
    stack = [r for r in thread.get("replies") or [] if isinstance(r, dict)]
    while stack:
        r = stack.pop()
        if str(r.get("id")) == str(reply_id):
            return r
        stack.extend(x for x in r.get("replies") or [] if isinstance(x, dict))
    return None


def all_replies(thread: dict) -> Iterable[dict]:
    yield thread
    stack = [r for r in thread.get("replies") or [] if isinstance(r, dict)]
    while stack:
        r = stack.pop()
        yield r
        stack.extend(x for x in r.get("replies") or [] if isinstance(x, dict))


# ---- fetch helpers used by both the collectors and the Toolbox --------------------------------------------------------
def tracker_page(client: Client, page: int, day: str) -> tuple[list[dict], str]:
    """One Devtracker page. `day` is the day header already shown above this page (the server only emits a header
    when the day changes); pass tomorrow's date for page 1 so it starts with its own header."""
    j = client.json("/api/community/getTrackedPosts", {"pagesize": 9, "page": page, "date": day})
    if not j.get("success"):
        raise RuntimeError(f"getTrackedPosts page {page}: {j.get('code')} {j.get('msg')}")
    return parse_tracked_posts((j.get("data") or {}).get("html") or "", current_day=day)


class PrivateForum(Exception):
    """The post is in a forum that needs a login (Focus Testing, Evocati...). Keep the public teaser."""


def devpost(client: Client, post: dict) -> dict:
    """Full text of one tracked post: {'text', 'time'}. Raises PrivateForum for login-only forums."""
    j = client.json("/api/spectrum/forum/thread/nested",
                    {"slug": post["slug"], "sort": "newest", "target_reply_id": post.get("reply_id") or None})
    if not j.get("success"):
        if j.get("code") == "ErrPermissionDenied":
            raise PrivateForum(post.get("thread") or post["slug"])
        raise RuntimeError(f"thread {post['slug']}: {j.get('code')}")
    reply = find_reply(j.get("data") or {}, post.get("reply_id") or "")
    if reply is None:
        raise RuntimeError(f"thread {post['slug']}: reply {post.get('reply_id')} not in payload")
    return {"text": post_text(reply), "time": reply.get("time_created")}


# ---- summaries ----------------------------------------------------------------------------------------------------
_SENT = re.compile(r"(?<=[.!?])[\"\u201d\u2019)]?\s+(?=[A-Z0-9\"\u201c'(])")


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in _SENT.split(text) if x.strip()]


def _first_sentences(text: str, max_chars: int) -> str:
    out = ""
    for sent in _sentences(text):
        if out and len(out) + len(sent) + 1 > max_chars:
            break
        out = (out + " " + sent).strip()
        if len(out) >= max_chars:
            break
    if len(out) > max_chars:
        out = out[:max_chars].rsplit(" ", 1)[0].rstrip(",;:") + "\u2026"
    return out


def summarize(text: str, title: str = "", max_lead: int = 420, max_point: int = 200, max_points: int = 30) -> str:
    """An extractive digest of an article, built from what it actually says (no model, no network).

    Sectioned articles (Monthly Reports: '## AI Content', '## Animation', ...) become a lead sentence or two plus one
    line per section: '- AI Content: <its first sentence>'. Anything else becomes its opening sentences.
    Lines that only repeat the title or are too short to say anything are skipped.
    Output uses the corpus text conventions ('- ' bullets), so the Toolbox renders it like any other text."""
    title_l = (title or "").strip().lower()
    lead_paras: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
            continue
        if line.startswith("- "):
            line = line[2:]
        if len(line.split()) < 4 or line.lower() == title_l or line.lower() in title_l:
            continue                     # banner lines: 'PU Monthly Report', 'August 2026', the title again
        (sections[-1][1] if sections else lead_paras).append(line)
    lead = _first_sentences(" ".join(lead_paras), max_lead)
    points = []
    for head, paras in sections:
        if not paras:
            continue
        sents = _sentences(paras[0])
        first = _first_sentences(sents[0], max_point) if sents else ""
        if first:
            points.append(f"- {head}: {first}")
    if len(points) >= 2:
        return "\n".join(([lead] if lead else []) + points[:max_points])
    # Not sectioned: the opening of the article, headings or not.
    body = " ".join(p for _h, ps in sections for p in ps)
    return _first_sentences((" ".join(lead_paras) + " " + body).strip(), max_lead + max_point)


# ---- selftest -------------------------------------------------------------------------------------------------------
def _selftest() -> int:
    ok = True

    def case(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(("  PASS  " if cond else "  FAIL  ") + name)

    cards = ('<a class="content-block2 hub-block full " href="/comm-link/transmission/21307-Star-Citizen-Monthly-'
             'Report-August-2026?x=1"><div class="type post"><div class="icon"></div><span>post</span></div>'
             '<div class="background" style="background-image:url(/x.jpg)"></div><div class="title-holder">'
             '<div class="title trans-opacity trans-03s">Star Citizen Monthly Report: August 2026</div></div>'
             '<div class="text"><div class="comments">0</div><div class="time_ago"><span class="value">1 month ago'
             '</span></div><div class="section"></div></div><div class="over"><div class="scroller"><div class="body">'
             '<p>Welcome to August&#8217;s report.</p></div></div></div></a>'
             '<a class="content-block2 hub-block" href="/comm-link/engineering/21329-Q-A"><div class="title">'
             'Q&amp;A: Sabre</div><div class="time_ago"><span class="value">6 days ago</span></div>'
             '<div class="body"><p>Answers.</p></div></a>')
    c = parse_commlink_cards(cards)
    case("listing: two cards", len(c) == 2)
    case("listing: id, type, clean url", c[0]["id"] == "21307" and c[0]["type"] == "transmission"
         and c[0]["url"].endswith("August-2026"))
    case("listing: title and teaser unescaped", c[0]["title"] == "Star Citizen Monthly Report: August 2026"
         and c[0]["teaser"] == "Welcome to August’s report." and c[1]["title"] == "Q&A: Sabre")
    today = dt.date(2026, 9, 27)
    case("age: days are exact", date_from_age("6 days ago", today) == ("2026-09-21", "day"))
    case("age: hours are today", date_from_age("3 hours ago", today) == ("2026-09-27", "day"))
    case("age: months are approximate", date_from_age("1 month ago", today)[1] == "approx")
    case("age: absolute timestamp", date_from_age("2012-09-12 10:10:37", today) == ("2012-09-12", "day"))

    page = "<script>\n const s3Url =  'https://robertsspaceindustries.com/alexandria/html/a/b/c-en';\n fetch(s3Url)"
    case("alexandria url found", alexandria_url(page) == "https://robertsspaceindustries.com/alexandria/html/a/b/c-en")
    alex = ('<div data-plugin_key="plugin_trblt_banner_advanced"></div><g-banner-advanced :media="{&quot;background'
            '&quot;:{&quot;url&quot;:&quot;https://x/y.jpg&quot;}}" layout="full-width" v-cloak></g-banner-advanced>'
            '<g-introduction :info="{&quot;title&quot;:&quot;PU Monthly Report&quot;,&quot;subtitle&quot;:&quot;August'
            ' 2026&quot;,&quot;contents&quot;:&quot;&lt;p&gt;Welcome to August&amp;rsquo;s PU Monthly Report!&lt;/p'
            '&gt;&quot;}" img-url="https://x/z.jpg"></g-introduction>'
            '<g-article :show-emphasis="false" body="&lt;h2&gt;AI Content&lt;/h2&gt;&lt;p&gt;The recent Alpha 4.10 '
            'patch marked a milestone.&lt;/p&gt;" v-cloak></g-article>'
            '<g-illustration image-size="large" :simple-image="{&quot;url&quot;:&quot;https://x/i.jpg&quot;,'
            '&quot;alt&quot;:&quot;a picture of a ship&quot;}"></g-illustration>')
    t = alexandria_text(alex)
    case("alexandria: intro title/subtitle/contents in order",
         t.startswith("PU Monthly Report\nAugust 2026\nWelcome to August’s PU Monthly Report!"))
    case("alexandria: article heading and paragraph", "## AI Content\nThe recent Alpha 4.10 patch" in t)
    case("alexandria: urls, alt text and layout keys skipped", "https" not in t and "picture" not in t
         and "full-width" not in t)
    legacy = ('<div id="post"><div class="title-section">ID: 16919</div><div class=" segment"><div class="content">'
              '<p>This is a cross-post.</p><h2>Cinematics</h2><p>Work continued.</p></div></div>'
              '<div class=" segment"><p>More.</p></div><div class="comments">x</div></div>'
              '<div class="related"><div class="time_ago">Posted:<span class="value">2014-03-04</span></div></div>')
    lt = legacy_body_text(legacy)
    case("legacy: segments joined, heading marked", lt == "This is a cross-post.\n## Cinematics\nWork continued.\nMore.")

    tracked = ('<h3 class="js-lock">September 26th 2026</h3><a class="devpost js-lock" href="/spectrum/community/SC/'
               'forum/3/thread/cig-when-life-support/9152310"><div class="devpost-wrapper"><div class="info"><img src='
               '"x"><div class="poster"><div class="nickname">Nicou-CIG</div><div class="handle">Nicou-CIG</div></div>'
               '<div class="date"><span class="label">Date</span><span class="time">20 hours ago</span></div></div>'
               '<div class="topic"><span class="category">General</span><span class="thread">CIG, when?</span></div>'
               '<p class="details">Hey folks, we&#39;ve been tracking this...</p></div></a>'
               '<h3>September 25th 2026</h3><a class="devpost" href="/spectrum/community/SC/forum/1/thread/patch-'
               'notes-4-10-2"><div class="nickname">Shark-CIG</div><span class="category">Patch</span><span class='
               '"thread">Patch Notes</span><p class="details">Greetings</p></a>')
    posts, day = parse_tracked_posts(tracked)
    case("devtracker: two posts with their day headers", [p["date"] for p in posts] == ["2026-09-26", "2026-09-25"])
    case("devtracker: reply id, forum, slug, author, thread, teaser",
         posts[0]["reply_id"] == "9152310" and posts[0]["forum_id"] == "3" and posts[0]["author"] == "Nicou-CIG"
         and posts[0]["thread"] == "CIG, when?" and posts[0]["teaser"].startswith("Hey folks, we've"))
    case("devtracker: opening post (no reply id) gets a stable id", posts[1]["id"] == "t-1-patch-notes-4-10-2")
    p2, _ = parse_tracked_posts('<a class="devpost" href="/spectrum/community/SC/forum/4/thread/x/77">'
                                '<div class="nickname">A</div></a>', current_day=day)
    case("devtracker: day header carries across pages", p2[0]["date"] == "2026-09-25")

    thread = {"id": "100", "content_blocks": [{"type": "text", "data": {"blocks": [
        {"type": "header-one", "text": "Chart Yer Course"}, {"type": "unstyled", "text": "Every scallywag"},
        {"type": "unstyled", "text": ""}, {"type": "unordered-list-item", "text": "JPG"}]}}, {"type": "image"}],
        "replies": [{"id": "101", "replies": [{"id": "102", "content_blocks": [], "lexical_content": {"root": {
            "children": [{"type": "paragraph", "children": [{"text": "Nested "}, {"text": "reply"}]}]}}}]}]}
    case("spectrum: draft.js blocks to text", post_text(thread) == "## Chart Yer Course\nEvery scallywag\n- JPG")
    case("spectrum: finds a nested reply", find_reply(thread, "102")["id"] == "102")
    case("spectrum: lexical fallback", post_text(find_reply(thread, "102")) == "Nested reply")
    case("spectrum: empty reply id means the opening post", find_reply(thread, "")["id"] == "100")
    case("spectrum: the opening post is found by its content_reply_id",
         find_reply(dict(thread, content_reply_id="9151279"), "9151279")["id"] == "100")
    old_shapes = {"content_blocks": [{"type": "text", "data": [{"type": "unstyled", "text": "Old shape"}]},
                                     {"type": "image", "data": []}, "junk"]}
    case("spectrum: older block shapes (list data, empty data, junk) do not crash", post_text(old_shapes) == "Old shape")
    case("spectrum: unknown shape falls back to any text / plaintext",
         post_text({"content_blocks": [{"weird": {"text": "deep"}}]}) == "deep"
         and post_text({"content_blocks": [], "annotation_plaintext": " plain "}) == "plain")
    report = ("PU Monthly Report\nAugust 2026\nWelcome to August\u2019s PU Monthly Report! While most teams worked on "
              "Alpha 4.10, many devs continued with tasks for content coming soon. Read on for more.\n"
              "## AI Content\nThe recent Alpha 4.10 patch marked a milestone for AI Content. The team also fixed bugs."
              "\n## Animation\nLast month, animations were worked on for the apex valakkar.\n## Art (Ships)\n"
              "The UK team began the month with a final polish pass on the Kruger Stingray.\n- A bullet here.")
    sm = summarize(report, "Star Citizen Monthly Report: August 2026")
    lines = sm.splitlines()
    case("summary: lead is the intro, banner lines skipped", lines[0].startswith("Welcome to August"))
    case("summary: one line per section with its first sentence",
         lines[1:] == ["- AI Content: The recent Alpha 4.10 patch marked a milestone for AI Content.",
                       "- Animation: Last month, animations were worked on for the apex valakkar.",
                       "- Art (Ships): The UK team began the month with a final polish pass on the Kruger Stingray."])
    plain = ("Greetings Citizens! The Sabre Raven EX evolves its predecessor into a dedicated interdiction craft. "
             "It carries a new quantum dampener. Pledge now. " + "More words here. " * 80)
    sp = summarize(plain, "Aegis Sabre Raven EX")
    case("summary: unsectioned article gives its opening sentences, bounded",
         sp.startswith("Greetings Citizens! The Sabre Raven EX evolves") and len(sp) <= 640)
    case("summary: empty in, empty out", summarize("", "x") == "")
    print("rsi selftest:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else 2)
