#!/usr/bin/env python3
"""
Build tools/SuitMk2/data/galactapedia_pack.json: what Elah may say from the Galactapedia.

Usage:
    python tools/build_galactapedia_pack.py             # fetch what is not cached, write the pack
    python tools/build_galactapedia_pack.py --offline   # cached responses only; an article not cached is skipped
    python tools/build_galactapedia_pack.py --dry-run   # everything but the write
    python tools/build_galactapedia_pack.py --check     # re-check the pack that is on disk; no fetch, no write

This is run by hand, offline from the app. Nothing the Toolbox ships fetches a Galactapedia article: the companions
read the pack this writes (tools/SuitMk2/core/place_knowledge.py, galactapedia_source).

WHERE THE TEXT COMES FROM. The Galactapedia is Cloud Imperium's, at robertsspaceindustries.com/galactapedia. Its own
endpoint answered this tool with a 503 challenge page (2026-10-07), so the text is read from the community mirror,
GET https://api.star-citizen.wiki/api/v2/galactapedia/<article id>, which returns the article's English text and
its path on robertsspaceindustries.com. The `source` recorded for every fact is that official address.

HOW POLITE. One request at a time, PAUSE_S between requests, a User-Agent that says what this is, and every raw
response kept under the user's temp folder (never in the repository) so a second run asks for nothing it has.

WHAT GOES IN THE PACK. For each topics_lore.json node listed in ARTICLES, up to MAX_PER_NODE sentences of its
article, each one whole. `excerpt` is the sentence as the article has it (link and emphasis markup removed). `text`,
what is said aloud, is that sentence with its bracketed asides cut and nothing else changed. No sentence is
reworded, by hand or by a model. A sentence is left out when it:
    * does not name its subject, or starts with a word that points back at the sentence before it;
    * has a quotation mark, or is shorter or longer than a line a companion can say;
    * shares most of its content words with a lore fact the node already has, or with a sentence already taken;
    * fails place_knowledge.galactapedia_problems(), the same check the selftest runs on the pack that ships;
    * could not be said by Elah: the answer built from it is refused by the gate every place answer goes through.
The pack never holds an article, only these sentences and the link to where each came from.

No dependency beyond the standard library and the Toolbox's own modules.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SUIT = PROJECT_ROOT / "tools" / "SuitMk2"
LORE_PATH = SUIT / "data" / "topics_lore.json"
PACK_PATH = SUIT / "data" / "galactapedia_pack.json"
sys.path.insert(0, str(SUIT / "core"))

import place_knowledge as pk                                    # noqa: E402

API = "https://api.star-citizen.wiki/api/v2/galactapedia/{id}"
RSI = "https://robertsspaceindustries.com"
USER_AGENT = ("SC-Toolbox-galactapedia-pack-builder/1.0 "
              "(fan project, offline build tool; https://github.com/ScPlaceholder/SC-Toolbox-Beta-V2)")
PAUSE_S = 1.5
TIMEOUT_S = 30
CACHE_DIR = Path(tempfile.gettempdir()) / "galactapedia_cache"
MAX_PER_NODE = 3
TEXT_WORDS = (8, 30)
NEAR_DUPLICATE = 0.6        # share of the shorter sentence's content words (its subject's name aside) in the other

# topics_lore node id -> (Galactapedia article id, the title that article must have). Each pairing was looked up by
# hand in the Galactapedia's own search (2026-10-07): a title alone is not enough to pick one, because "Hurston" is
# a planet, a company and five people, and "microTech" and "ArcCorp" are each a planet and a company. The build
# refuses an article whose title is not the one written here.
ARTICLES = {
    "stanton": ("RX3lKBA3dq", "Stanton System"),
    "crusader": ("VarZxj1Qav", "Crusader (Stanton II)"),
    "orison": ("V7dgN8Pkrm", "Orison"),
    "stormwal": ("RX34BjN2NK", "Stormwal"),
    "crusader_industries": ("0OaBkLLG1N", "Crusader Industries"),
    "crusader_security": ("bmNrmYB8MP", "Crusader Security"),
    "cellin": ("Rw1Z9yz1jJ", "Cellin (Stanton 2a)"),
    "daymar": ("VpMGDvX48N", "Daymar (Stanton 2b)"),
    "yela": ("bo1xJPgMNL", "Yela (Stanton 2c)"),
    "grim_hex": ("0jGGDvYm93", "Green Imperial Housing Exchange (Grim HEX)"),
    "nine_tails": ("0Gxpleqwqo", "Nine Tails"),
    "hathor": ("R6v8Dg8o6a", "Hathor Group"),
    "hurston": ("0Qlx4dQnxL", "Hurston (Stanton I)"),
    "lorville": ("Rw1ZlJNE36", "Lorville"),
    "hurston_dynamics": ("RxNxAZpWlQ", "Hurston Dynamics"),
    "aberdeen": ("0qanJarxqa", "Aberdeen (Stanton 1b)"),
    "arial": ("VlMNJMYzy4", "Arial (Stanton 1a)"),
    "ita": ("bZkwADDA5d", "Ita (Stanton 1d)"),
    "magda": ("0qanJYxAG7", "Magda (Stanton 1c)"),
    "microtech": ("VYL46xl5Z1", "microTech (Stanton IV)"),
    "new_babbage": ("Rw1Zl3vdW7", "New Babbage"),
    "microtech_corp": ("bE3rBB4j7J", "microTech (Company)"),
    "calliope": ("Rz2PaqwwPM", "Calliope (Stanton 4a)"),
    "clio": ("01Ld63ezDB", "Clio (Stanton 4b)"),
    "euterpe": ("02kBNLWB33", "Euterpe (Stanton 4c)"),
    "arccorp": ("bBzpNY6ANd", "ArcCorp (Stanton III)"),
    "area18": ("bEz3N7zL66", "Area18"),
    "arccorp_corp": ("0dQ47zYd6O", "ArcCorp (Company)"),
    "lyria": ("RvlgwoxzpY", "Lyria (Stanton 3a)"),
    "wala": ("V3GqNknrr2", "Wala (Stanton 3b)"),
    "pyro": ("0ODxJM917N", "Pyro System"),
    "ruin_station": ("RX3AzPdnw8", "Ruin Station"),
    "checkmate": ("RPDeA41EZX", "Checkmate Station"),
    "pyro_i": ("RMNjxnZp3n", "Pyro I"),
    "monox": ("0jGdgLEjqJ", "Monox (Pyro II)"),
    "bloom": ("0KxjqgQdNY", "Bloom (Pyro III)"),
    "pyro_iv": ("RX3AzOBxeM", "Pyro IV"),
    "pyro_v": ("RAXv9Yeqay", "Pyro V"),
    "terminus": ("RAXv9kpk5m", "Terminus (Pyro VI)"),
    "nyx": ("0jGxP6kpG3", "Nyx System"),
    "delamar": ("0GxoZnAGrg", "Delamar"),
    "levski": ("0dXQqBMAOp", "Levski"),
    "peoples_alliance": ("Vy2v4qgG4v", "People's Alliance"),
}

# Nodes left out on purpose, and why. Every topics_lore node is in ARTICLES or here: the build stops if one is in
# neither, so a node added to the lore later is a decision somebody has to make, not a silent gap.
NO_ARTICLE = {
    "crusader_comm_arrays": "the Galactapedia search has no article for the comm arrays",
    "wikelo": "the Galactapedia search has no article for Wikelo (only the Banu in general)",
    "seraphim_station": "the Galactapedia search has no article for Seraphim Station",
    "security_post_kareah": "the Galactapedia search has no article for Security Post Kareah",
    "klescher": "the Galactapedia search has no article for Klescher",
    "pyro_gateway": "the Galactapedia search has no article for the gateway station (only jump points in general)",
    "pyro_lawless": "a topic of the lore graph, not a thing the Galactapedia has an article on",
    "orbituary": "the Galactapedia search has no article for Orbituary",
    "port_olisar": "there is an article (it says the station was decommissioned in 2943), but the lore holds "
                   "Port Olisar only as a place that has changed; whether Elah speaks of it at all is not this "
                   "tool's decision",
}

# Sentences left out by hand after reading the build's output, with the reason. Matched on how the sentence starts.
_SAME = "says what a lore fact of this node already says, in other words"
_BACK = "leans on the sentence before it"
LEFT_OUT: dict[str, list[tuple[str, str]]] = {
    "crusader": [("Crusader (Stanton II) is a low mass gas giant", _SAME)],
    "orison": [("The iconic hosanna tree", _SAME), ("One such animal", _BACK)],
    "stormwal": [("Four other elongated", _BACK)],
    "daymar": [("Due to the character's tendency", _BACK), ("Underground race Daymar Rally", _SAME)],
    "yela": [("Yela is the outermost natural satellite", _SAME)],
    "arial": [("Arial is the first moon of Hurston", _SAME)],
    "nine_tails": [("Over the next few years", _BACK)],
    "hathor": [("The Hathor Group was a Human inter-planetary mining concern", _SAME)],
    "new_babbage": [("New Babbage is the largest city", _SAME), ("Many public spaces in New Babbage", _SAME)],
    "clio": [("Rayari Inc. has established research outposts", _SAME)],
    "euterpe": [("Euterpe is the third and smallest moon", _SAME)],
    "area18": [("Riker Memorial Spaceport, the main spaceport", "calls the plaza Central Plaza where the lore, "
                "from the place itself, has ArcCorp Plaza")],
    "wala": [("ArcCorp operates a number of surface mining facilities", _SAME)],
    "pyro_iv": [("Pyro IV is the outermost moon", "the article runs two words together (ofPyro), and counts "
                 "Pyro IV as a moon where the lore counts it as one of six planets")],
    "pyro_v": [("Pyro V is the fourth planet", "counts Pyro V as the fourth planet where the lore counts six "
                "planets with Pyro V the fifth")],
    "terminus": [("Terminus' proximity to the station", _BACK)],
    "peoples_alliance": [("The People's Alliance is an independent Human political group", _SAME),
                         ("Named Levski after", "reads as if the Alliance were the thing named Levski"),
                         ("Today, the People's Alliance continues to accept", _SAME)],
}

_POINTS_BACK = set("""it its it's they their them he his him she her hers this that these those however but and also
additionally furthermore moreover instead still yet so thus therefore then such both another others
despite although though afterwards afterward later eventually meanwhile nevertheless nonetheless consequently
unfortunately fortunately similarly likewise otherwise""".split())
# "The moon was named ...", "The company has ...": which moon, which company, is in the sentence before.
_GENERIC = re.compile(r"^The (?:world|moon|planet|company|group|station|city|settlement|system|gang|organization|"
                      r"corporation|asteroid|facility|town|exchange)(?:'s)?\b")
_ABBREVIATION = re.compile(r"(?:\b[A-Z]|\b(?:Dr|Mr|Mrs|Ms|St|Mt|Inc|Co|Corp|Ltd|vs|No|Gen|Col|Capt|Lt|Sgt|Adm|Jr|Sr|"
                           r"etc|approx|ca))\.$")
_STOP = set("""a an the and or but of to in on at for with by from is are was were be been being it its this that
these those as not no so if then than there their they them what which who when where while how all any some more
most much many very just only also into over under about after before up down out can could would should will may
might must has have had does did do each every both other such own same one""".split())


# ---------------------------------------------------------------------------------------------------------------
# Fetching (the only part of this file that touches the network)
# ---------------------------------------------------------------------------------------------------------------
class Fetcher:
    def __init__(self, cache: Path = CACHE_DIR, offline: bool = False, log=print):
        self.cache, self.offline, self.log, self._last = Path(cache), offline, log, 0.0

    def article(self, article_id: str) -> dict | None:
        """{"fetched": date, "url": ..., "data": the API's article} from the cache, else from the network (once,
        politely, and then cached). None when it cannot be had."""
        f = self.cache / f"article_{article_id}.json"
        if f.exists():
            try:
                return json.loads(f.read_text(encoding="utf-8"))
            except ValueError:
                self.log(f"  cache file {f.name} is unreadable; fetching again")
        if self.offline:
            return None
        url = API.format(id=article_id)
        wait = PAUSE_S - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:        # one request at a time
                raw = json.loads(r.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.log(f"  could not fetch {url}: {e}")
            return None
        finally:
            self._last = time.monotonic()
        rec = {"fetched": datetime.date.today().isoformat(), "url": url, "data": raw.get("data", raw)}
        self.cache.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        return rec


# ---------------------------------------------------------------------------------------------------------------
# From an article to sentences
# ---------------------------------------------------------------------------------------------------------------
def plain_text(markdown: str) -> list[str]:
    """The article's paragraphs as plain text: headings, tables, images and list markers dropped, a link reduced
    to the words it shows, emphasis marks removed. No word of the article is changed."""
    out = []
    for para in re.split(r"\n\s*\n", str(markdown or "").replace("\r\n", "\n")):
        lines = [ln.strip() for ln in para.split("\n")]
        lines = [ln for ln in lines if ln and not re.match(r"(?:#|!\[|\||>|[-*+] |\d+\. )", ln)]
        s = " ".join(lines)
        s = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", s)
        s = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", s)
        s = re.sub(r"\*+|__+|`", "", s)
        s = " ".join(s.split())
        if s:
            out.append(s)
    return out


def sentences(paragraph: str) -> list[str]:
    """Split at a full stop followed by a capital, except after an initial or a title ("Gavin E. Hurston")."""
    parts, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+(?=[A-Z0-9])", paragraph):
        if _ABBREVIATION.search(paragraph[start:m.start()]):
            continue
        parts.append(paragraph[start:m.start()].strip())
        start = m.end()
    parts.append(paragraph[start:].strip())
    return [p for p in parts if p]


def spoken(sentence: str) -> str:
    """The sentence with its bracketed asides cut: "Hurston (Stanton I) is ..." -> "Hurston is ..."."""
    s = re.sub(r"\s*\([^()]*\)", "", sentence)
    s = re.sub(r"\s+([,.;:])", r"\1", s)
    return " ".join(s.split())


def content_words(text: str) -> set:
    """Lower-case content words, a hyphenated word as one word, a plain plural as its singular."""
    words = re.findall(r"[a-z0-9]+", text.lower().replace("-", ""))
    return {w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
            for w in words if len(w) >= 3 and w not in _STOP}


def near_duplicate(a: str, b: str, subject: frozenset = frozenset()) -> bool:
    """Two sentences that say much the same. The subject's own name is in every sentence about it, so it does not
    count, and a sentence with fewer than three other content words is too short to judge this way."""
    wa, wb = content_words(a) - subject, content_words(b) - subject
    return min(len(wa), len(wb)) >= 3 and len(wa & wb) / min(len(wa), len(wb)) >= NEAR_DUPLICATE


def proper_names(text: str, article_text: str, title: str) -> list[str]:
    """The proper names the sentence uses, as topics_lore.json lists them: runs of capitalised words ("United
    Empire of Earth", "Stanton 2a", "microTech"). The sentence's first word counts only when the article also
    writes it with a capital somewhere that is not the start of a sentence, or it is in the article's title."""
    elsewhere = set(re.findall(r"(?<=[a-z,;] )[A-Z][A-Za-z0-9'’-]*", article_text)) | set(title.split())
    names = []
    for part_no, part in enumerate(re.split(r"[,;:()]|(?<!\b[A-Z])\.(?:\s|$)", text)):   # a name does not cross a comma
        tokens = re.findall(r"[A-Z]\.(?= [A-Z])|[A-Za-z0-9][A-Za-z0-9'’-]*", part)     # the E. of Gavin E. Hurston

        def capital(i: int) -> bool:
            return tokens[i][0].isupper() or bool(re.match(r"[a-z]+[A-Z]", tokens[i]))   # Hurston, microTech

        def is_name(i: int) -> bool:
            if i == 0 and part_no == 0:                    # the sentence's first word has a capital whatever it is
                t = tokens[i].removesuffix("'s")
                return capital(i) and (t in elsewhere or (len(tokens) > 1 and capital(1)
                                                         and t.lower() not in _STOP | _POINTS_BACK))
            return capital(i)

        run = []
        for i, t in enumerate(tokens):
            joiner = t in ("of", "the", "for") and run and i + 1 < len(tokens) and tokens[i + 1][0].isupper()
            numeral = bool(run) and bool(re.fullmatch(r"\d[a-z]", t))            # the 2a of Stanton 2a
            if is_name(i) or joiner or numeral:
                run.append(t)
                continue
            if run:
                names.append(" ".join(run))
            run = []
        if run:
            names.append(" ".join(run))
    out = []
    for n in names:
        n = re.sub(r"['’]s$", "", n)
        if n and n not in out and n in text:
            out.append(n)
    return out


def sayable(node_title: str, fact: dict) -> list[str]:
    """Why Elah could not say this fact as the answer to "what is this place" ([] = she could): the real answer,
    built by the real code, put through the gate every place answer goes through."""
    import conversation as conv
    state = {"location": node_title, "location_named": True}
    spec = conv.place_spec(conv.route("what is this place"), state, 0, fact)
    return conv.ground_direct(spec, spec["fixed_text"])


def pick(node: dict, rec: dict, log=print) -> list[dict]:
    """The pack entries for one node from one fetched article."""
    data = rec["data"]
    title = str(data.get("title") or "")
    body = str((data.get("translations") or {}).get("en_EN") or "")
    paras = plain_text(body)
    article_text = " ".join(paras)
    source = RSI + str(data.get("rsi_url") or "")
    subjects = {node["title"].lower(), title.split(" (")[0].lower()}
    subject_words = frozenset(w for s in subjects for w in content_words(s))
    lore = [f["text"] for f in node.get("facts", [])]
    left_out = LEFT_OUT.get(node["id"], [])
    taken: list[dict] = []
    for sentence in (s for p in paras for s in sentences(p)):
        if len(taken) >= MAX_PER_NODE:
            break
        text = spoken(sentence)
        first = re.sub(r"[^a-z']", "", text.split()[0].lower()) if text.split() else ""
        why = None
        if not TEXT_WORDS[0] <= len(text.split()) <= TEXT_WORDS[1]:
            continue                                            # headings, captions, the long ones: not worth a line
        named = [m.start() for s in subjects
                 for m in [re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text.lower())] if m]
        if first in _POINTS_BACK or _GENERIC.match(text):
            why = "points back at the sentence before it"
        elif not named:
            why = "does not name its subject"
        elif re.search(r"\b(?:it|they)\b", text.lower()[:min(named)]):
            why = "says it or they before it has named anything"
        elif any(sentence.startswith(start) for start, _ in left_out):
            why = "left out by hand: " + next(r for start, r in left_out if sentence.startswith(start))
        elif any(near_duplicate(text, x, subject_words) for x in lore):
            why = "says what a lore fact of this node already says"
        elif any(near_duplicate(text, t["text"], subject_words) for t in taken):
            why = "says what a sentence already taken says"
        entry = {"text": text, "excerpt": sentence, "source": source, "article": title,
                 "retrieved": rec["fetched"], "names": proper_names(text, article_text, title)}
        if why is None:
            probs = pk.galactapedia_problems(entry)
            if probs:
                why = "; ".join(probs)
        if why is None:
            fact = {"text": text, "names": entry["names"], "source": source, "status": "lore", "node": node["id"],
                    "fact": len(taken), "title": node["title"], "kind": "lore"}
            gate = sayable(node["title"], fact)
            if gate:
                why = "the gate would refuse it: " + "; ".join(gate)
        if why is None:
            taken.append(entry)
        else:
            log(f"    left out ({why}): {text[:110]}")
    return taken


# ---------------------------------------------------------------------------------------------------------------
# The build and the check
# ---------------------------------------------------------------------------------------------------------------
def load_nodes() -> list[dict]:
    return json.loads(LORE_PATH.read_text(encoding="utf-8"))["nodes"]


def build(fetcher: Fetcher, log=print) -> dict:
    nodes = load_nodes()
    undecided = [n["id"] for n in nodes if n["id"] not in ARTICLES and n["id"] not in NO_ARTICLE]
    unknown = [k for k in list(ARTICLES) + list(NO_ARTICLE) if k not in {n["id"] for n in nodes}]
    if undecided or unknown:
        raise SystemExit(f"topics_lore nodes in neither ARTICLES nor NO_ARTICLE: {undecided}; "
                         f"ids here that topics_lore does not have: {unknown}")
    entries: dict = {}
    for node in nodes:
        nid = node["id"]
        if nid in NO_ARTICLE:
            log(f"{nid}: no entry ({NO_ARTICLE[nid]})")
            continue
        article_id, want_title = ARTICLES[nid]
        rec = fetcher.article(article_id)
        if rec is None:
            log(f"{nid}: NO ENTRY, the article {article_id} could not be read")
            continue
        got = str(rec["data"].get("title") or "")
        if got != want_title or not str(rec["data"].get("rsi_url") or "").startswith("/galactapedia/article/" + article_id):
            log(f"{nid}: NO ENTRY, article {article_id} is {got!r}, not {want_title!r}")
            continue
        log(f"{nid}: {got}")
        taken = pick(node, rec, log)
        if taken:
            entries[nid] = taken
        else:
            log(f"{nid}: NO ENTRY, no sentence of the article could be used")
    return {
        "version": 1,
        "compiled": datetime.date.today().isoformat(),
        "about": "Sentences from the Star Citizen Galactapedia for the places and companies in topics_lore.json, "
                 "keyed by topics_lore node id. Built by tools/build_galactapedia_pack.py; do not edit by hand. "
                 "text is what a companion says, excerpt is the article's own sentence, source is the article.",
        "credit": "The Galactapedia is the work of Cloud Imperium Games and its text is theirs. SC Toolbox is an "
                  "unofficial fan project, not affiliated with the Cloud Imperium group of companies. Each excerpt "
                  "is a sentence of the article named beside it, with a link to the full article on "
                  "robertsspaceindustries.com.",
        "fetched_from": API.format(id="<article id>"),
        "entries": entries,
    }


def check(pack: dict) -> list[str]:
    """Everything wrong with a pack ([] = nothing): every entry against galactapedia_problems(), with its node."""
    node_ids = {n["id"] for n in load_nodes()}
    bad = []
    entries = pack.get("entries")
    if not isinstance(entries, dict) or not entries:
        return ["the pack has no entries"]
    for nid, facts in entries.items():
        if not isinstance(facts, list) or not 1 <= len(facts) <= pk.GALACTAPEDIA_MAX_FACTS:
            bad.append(f"{nid}: not 1 to {pk.GALACTAPEDIA_MAX_FACTS} facts")
            continue
        for i, e in enumerate(facts):
            bad += [f"{nid}[{i}]: {p}" for p in pk.galactapedia_problems(e, nid, node_ids)]
    return bad


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--offline", action="store_true", help="use cached responses only")
    ap.add_argument("--dry-run", action="store_true", help="build and check, but do not write the pack")
    ap.add_argument("--check", action="store_true", help="re-check the pack on disk and exit")
    ap.add_argument("--cache", default=str(CACHE_DIR), help=f"where raw responses are kept (default {CACHE_DIR})")
    ap.add_argument("--out", default=str(PACK_PATH))
    args = ap.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out = Path(args.out)
    if args.check:
        problems = check(json.loads(out.read_text(encoding="utf-8")))
        print("\n".join(problems) or f"{out.name}: every entry passes")
        return 1 if problems else 0
    cache = Path(args.cache).resolve()
    if PROJECT_ROOT in cache.parents or cache == PROJECT_ROOT:
        print("the cache must not be inside the repository")
        return 2
    pack = build(Fetcher(cache, offline=args.offline))
    problems = check(pack)
    n = sum(len(v) for v in pack["entries"].values())
    print(f"\n{len(pack['entries'])} of {len(load_nodes())} nodes have an entry, {n} facts")
    if problems:
        print("NOT WRITTEN, the pack fails its own check:\n" + "\n".join(problems))
        return 1
    if not args.dry_run:
        tmp = out.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(pack, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.replace(tmp, out)
        print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
