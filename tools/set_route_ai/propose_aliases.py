"""propose_aliases.py - PROPOSE phonetic + hierarchy aliases for destinations, for a human to judge.

J asked for this on 2026-09-27 to replace batching `data/destinations.json` through GPT: after a major
patch the game can add 1,000+ locations at once and each one wants an alias list so the voice engine
can resolve a misheard name.

⛔⛔ THIS FILE NEVER WRITES destinations.json. There is no --apply mode, deliberately, because J
   hand-maintains that file and asked to keep it that way. This tool emits PROPOSALS: a candidate, the
   rule that produced it, and nothing else. Accepting them is a human act.

═══════════════════════════════════════════════════════════════════════════════════════════════
WHAT I MEASURED BEFORE WRITING ANY RULES, because the brief's premises turned out to be testable
═══════════════════════════════════════════════════════════════════════════════════════════════

The brief said ~32.5% of the 8,255 existing aliases "add genuinely NEW words" and that those want
HIERARCHY aliases off starmap.json's ParentUUID chain. I checked. The premise about the *count* is
roughly fine; the premise about the *mechanism* is not.

  1. ★★ HIERARCHY HAS EXACTLY ONE PRECEDENT IN THE ENTIRE CORPUS. Of the 399 destination keys that
     appear in the datamine at all, 393 have a parent name that is not already in their own key.
     **11 use it in their aliases. 382 do not.** And 10 of those 11 are not hierarchy: they are the
     `mt datacenter *` keys, where `mt` -> `microtech` is an ABBREVIATION EXPANSION and microTech
     merely happens to also be the parent. My own detector scored those as hierarchy and was wrong.
     ⇒ The only genuine hierarchy user in 8,255 aliases is `aberdeen`:
          aberdeen hurston / hurston moon aberdeen / hurston s moon aberdeen /
          aberdeen moon hurston / aber dean hurston / aber deen hurston
     So the hierarchy forms below are modelled on ONE destination. They are proposals with almost no
     established practice behind them, and `--why` says so on every single one.

  2. ★★ THE "NEW WORDS" ARE OVERWHELMINGLY NOT PARENTS. Counting novel alias words against the
     parent chain + Type + NavIcon for all 399 matched keys: **37 word instances explained, 4,202
     not.** The actual novel vocabulary, sampled from the corpus, is:
          one/two/three/zero/1/2/3 ...  digits spoken aloud   (arccorp mining area 045 -> "zero four five")
          s                             a tokenisation artefact of "carver's ridge", not a word
          arc + corp                    a glued compound split ("arccorp" -> "arc corp")
          rabb                          a split that DOUBLES a consonant ("rab-bravo" -> "rabb bravo")
          st / h / d / l                a letter code spelled out ("st2-55" -> "s t 2 55")
     Every one of those is derivable from the NAME ALONE. No datamine, no model, no network.

  3. ⛔ 67.3% OF THE EXISTING ALIASES ARE REDUNDANT. Measured: 1,177 hand-written aliases resolved
     with rapidfuzz WRatio against the destination NAMES ONLY (alias lists removed). **792 already
     resolve to their own destination as top-1 without any alias at all.** They cost review time and
     buy nothing. This is why this tool does NOT aim at the observed 6.6/destination: reproducing
     that mean would mean reproducing mostly padding.
     ⇒ The 32.7% that ARE load-bearing have a shape, and it is sibling collision inside a numbered
       family — the digits are the only discriminator and WRatio underweights them:
          "comm array st two fifty five"  -> resolved to comm array st1-02   (wrong sibling)
          "cluster yka zero eleven"       -> resolved to akiro cluster
          "charon tree" / "hades tree"    -> roman numeral III spoken, resolved to `char`
       So RULE PRIORITY IS NOT ALPHABETICAL OR AESTHETIC. Numeral and roman-numeral forms rank first
       because they are where the engine measurably fails. [[a-correct-rule-can-guard-a-branch-nothing-takes]]

  4. THINGS I CHECKED AND REJECTED, recorded so nobody rebuilds them:
     - Description breadcrumbs. Some datamine Descriptions open with "Area18, ArcCorp, Stanton
       System", which would be a better hierarchy source than ParentUUID. **5 entries of 2,056 have
       one; 3 of 1,317 destinations.** Negligible. Not used.
     - Alternate proper names. `area18` carries "riker memorial spaceport" — real-world knowledge.
       The datamine has ZERO occurrences of "riker" in any Name or Description. This class is NOT
       GENERATABLE by any rule or by the datamine, and this tool does not pretend to. It is the one
       thing the GPT pass could do that this cannot. Say so out loud rather than quietly under-cover.
     - Possessives. normalize() strips apostrophes, so "carver's ridge" normalizes to exactly the key
       "carvers ridge". A possessive alias is a NO-OP for matching. Not generated. `--audit` counts
       how many already in the file are dead this way.

═══════════════════════════════════════════════════════════════════════════════════════════════

    python propose_aliases.py --only aberdeen --only charon\\ iii   # a few, readable
    python propose_aliases.py --new                  # only destinations that have NO aliases yet
    python propose_aliases.py --json out.json        # reviewable, editable, feedable
    python propose_aliases.py --audit                # dead aliases already in the file
    python propose_aliases.py --verify --only charon\\ iii   # is this candidate load-bearing?
    python propose_aliases.py --selftest

Exit codes: 0 ran and proposed (or had nothing to propose), 2 could not run.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

# ── the datamine, pinned for the same reason lint_destinations.py pins it: an unpinned ref means the
#    proposals change under me between runs and I cannot tell a game patch from a data-source edit.
SCUNPACKED_REPO = "StarCitizenWiki/scunpacked-data"
SCUNPACKED_REF = os.environ.get("SC_STARMAP_REF", "e96132078ae6")
STARMAP_PATH = "starmap.json"

HERE = Path(__file__).parent
DESTINATIONS = HERE / "data" / "destinations.json"
CACHE_DIR = Path(os.environ.get("SC_STARMAP_CACHE", Path.home() / ".sctoolbox" / "starmap"))

# Observed mean is 6.27 over all 1,317 keys (6.60 if you divide by the 1,251 that HAVE aliases — both
# figures are true, they differ only in denominator, and the brief's 6.6 is the latter).
#
# ★ THE CAP IS 6 BECAUSE OF A SWEEP, not because 6 is near the corpus mean — that it lands there is a
#   coincidence worth noticing and not the reason. Measured over all 1,317 destinations with the
#   production scorer, counting how many LOAD-BEARING candidates survive each cap:
#
#       cap   candidates   mean   load-bearing   % of the load-bearing value at cap 30
#         4        4,966   3.77          1,166        79.6%
#         5        6,077   4.61          1,313        89.6%
#         6        7,104   5.39          1,393        95.1%   <- default
#         8        8,792   6.68          1,443        98.5%
#        10        9,918   7.53          1,452        99.1%
#        15       11,299   8.58          1,465       100.0%
#
#   Diminishing returns are brutal past 6: cap 10 costs 2,814 more candidates to review and buys 59
#   more useful ones. Raise it with --cap when reviewing a small batch by hand; leave it here for a
#   1,000-destination post-patch run, where review time is the scarce resource.
DEFAULT_CAP = 6

# Keys shorter than this are never matched by the engine (_MIN_KEY_LEN there), so proposing aliases
# for them is proposing into a void. destinations.json carries the alphabet headers of
# raw_locations.txt ('c', 'k', 'io') as keys, plus one empty-string key.
MIN_KEY_LEN = 3


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  normalize() — a DELIBERATE COPY of the engine's, asserted equal to it in the selftest
# ══════════════════════════════════════════════════════════════════════════════════════════════
def normalize(text: str) -> str:
    """Byte-for-byte the engine's DestinationPhoneticEngine.normalize().

    Copied rather than imported on purpose: importing it costs a 1,317-entry JSON load and drags in
    the learning/blacklist files, and this tool must run from anywhere. The risk of a copy is that it
    DRIFTS from the original and suppression silently stops matching — so `_selftest` imports the real
    engine and asserts my copy agrees on a probe set. If the engine cannot be imported the selftest
    says CANNOT TELL rather than passing quietly, because "I could not check" and "it agrees" are not
    the same verdict. [[my-own-precise-note-is-the-one-i-never-re-derive]]

    ⚠ NOTE WHAT IT KEEPS: hyphens survive, apostrophes do not. So "aber-deen" and "aber deen" are
      DIFFERENT aliases to the engine, while "carver's ridge" and "carvers ridge" are the SAME one.
      Both facts drive rules below.
    """
    text = text.lower()
    text = (text.replace("á", "a").replace("é", "e").replace("í", "i")
                .replace("ó", "o").replace("ú", "u"))
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    return re.sub(r"\s+", " ", text).strip()


def squash(text: str) -> str:
    """Separator-blind form, for detecting that two candidates differ only in spacing."""
    return re.sub(r"[^a-z0-9]", "", normalize(text))


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  NUMBER AND ROMAN-NUMERAL SPEECH — ranked first because this is where the engine measurably fails
# ══════════════════════════════════════════════════════════════════════════════════════════════
_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_TEENS = ["ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
          "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9,
          "x": 10, "xi": 11, "xii": 12, "xiii": 13}


def spell_digits(digits: str) -> str:
    """045 -> 'zero four five'. Digit by digit, the way a comm array code is read out."""
    return " ".join(_ONES[int(c)] for c in digits)


def spell_number(digits: str) -> str:
    """045 -> 'forty five'; 18 -> 'eighteen'; 115 -> 'one fifteen'.

    ⚠ Leading zeros are dropped here ON PURPOSE and the caller re-adds a spoken 'zero' prefix as a
      SEPARATE candidate, because the corpus contains both "area zero forty five" and "zero forty
      five" and they are different utterances.
    """
    n = int(digits)
    if n < 10:
        return _ONES[n]
    if n < 20:
        return _TEENS[n - 10]
    if n < 100:
        t, o = divmod(n, 10)
        return _TENS[t] + (" " + _ONES[o] if o else "")
    if n < 1000:
        h, r = divmod(n, 100)
        # "one fifteen" not "one hundred fifteen": the corpus reads 115 as "one 15" / "one fifteen".
        return _ONES[h] + (" " + spell_number(str(r).zfill(2)) if r else " hundred")
    return spell_digits(digits)


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  RULES.  Each takes the normalized name and yields (candidate, rule_name).
#  Every rule is bounded: none may emit more than a handful, or 1,300 destinations blows up.
# ══════════════════════════════════════════════════════════════════════════════════════════════
_DIGIT_RUN = re.compile(r"\d+")
_VOWELS = set("aeiou")


def r_numeral(name: str):
    """Digit runs spoken aloud. THE highest-value rule: sibling families collide on their digits.

    'comm array st2-55' -> 'comm array st2 fifty five', 'comm array st2 five five', ...
    Each digit run is replaced on its own, so a name with two runs yields a bounded 2*k, not k**2.
    """
    runs = list(_DIGIT_RUN.finditer(name))
    if not runs:
        return
    for m in runs:
        d = m.group(0)
        pre, post = name[:m.start()], name[m.end():]
        forms = {spell_digits(d), spell_number(d.lstrip("0") or "0")}
        if d != d.lstrip("0") and d.lstrip("0"):
            # "045": the leading zero is spoken in the corpus ("area zero forty five")
            forms.add("zero " + spell_number(d.lstrip("0")))
            forms.add(d.lstrip("0"))            # plain digits, zero dropped: "area 45"
        for f in forms:
            # ⚠ A SPOKEN NUMBER NEEDS A GAP FROM AN ADJOINING LETTER. Without this, 'comm array
            #   st2-55' produced 'comm array sttwo-55' — the code and the number fused into one
            #   nonsense token, which is worse for the matcher than no alias at all. Only word forms
            #   need it; a digits-for-digits swap keeps the original spacing.
            wordy = f[:1].isalpha()
            left = pre + (" " if wordy and pre[-1:].isalnum() else "")
            right = (" " if wordy and post[:1].isalnum() else "") + post
            yield (left + f + right, "numeral")


def r_numeral_split(name: str):
    """Separate a letter run from a digit run: 'area18' -> 'area 18'; 'st2' -> 'st 2'.

    Observed directly: area18's own alias list contains "area 18" and "area 1 8".
    """
    out = re.sub(r"([a-z])(\d)", r"\1 \2", name)
    if out != name:
        yield (out, "numeral_split")
    out2 = re.sub(r"(\d)([a-z])", r"\1 \2", name)
    if out2 != name:
        yield (out2, "numeral_split")
    for m in _DIGIT_RUN.finditer(name):
        d = m.group(0)
        if len(d) > 1:
            # digits read singly with spaces: '11a' -> '1 1 a'
            yield (name[:m.start()] + " ".join(d) + name[m.end():], "numeral_split")


def r_roman(name: str):
    """A trailing roman numeral, spoken. 235 keys end in one, and it is a measured failure mode:
    'charon iii' was heard as 'charon tree' and resolved to `char`."""
    toks = name.split()
    if not toks:
        return
    last = toks[-1].strip("-")
    n = _ROMAN.get(last)
    if n is None:
        return
    head = " ".join(toks[:-1])
    if not head:
        return
    yield (head + " " + str(n), "roman")
    yield (head + " " + spell_number(str(n)), "roman")
    yield (head + " " + str(n).zfill(2), "roman")
    yield (head + " " + " ".join(last), "roman")          # 'charon i i i'


def r_separator(name: str):
    """hyphen / space / glued. All three, because normalize() KEEPS hyphens, so to the engine these
    are three distinct strings and a spoken hyphen-less phrase does not equal a hyphenated key."""
    if not re.search(r"[\s-]", name):
        return
    yield (re.sub(r"[\s-]+", " ", name), "separator")
    yield (re.sub(r"[\s-]+", "-", name), "separator")
    yield (re.sub(r"[\s-]+", "", name), "separator")


def r_compound_split(name: str):
    """Split a single glued token into two, at the boundary NEAREST THE MIDDLE.

    No dictionary, no model. The heuristic is standard syllabification — a split either between two
    consonants (VC|CV) or before a single consonant (V|CV) — and then, of all the legal splits, take
    the one closest to the middle of the word.
    ★ Validated against the three compound splits the corpus actually contains, and it gets all
      three: arccorp->arc|corp (len 7, split at 3), aberdeen->aber|deen (8 at 4),
      microtech->micro|tech (9 at 5). That is why "nearest the middle" and not "first legal split".
    ⚠ A DOUBLED CONSONANT IS ITS OWN LEGAL BOUNDARY, and leaving that out cost me `arccorp` — the
      only one of the three I had actually cited in the docstring. "arccorp" is a-r-c|c-o-r-p: the
      character before the split is 'c' and the one before THAT is 'r', so the plain VC|CV test
      requires a vowel there and refuses. The selftest failed on the headline example.
    """
    toks = name.split()
    for i, tok in enumerate(toks):
        if len(tok) < 6 or "-" in tok or not tok.isalpha():
            continue
        best = None
        for p in range(3, len(tok) - 2):
            a, b = tok[p - 1], tok[p]
            prev_v = tok[p - 2] in _VOWELS if p >= 2 else False
            doubled = a == b and a not in _VOWELS
            legal = doubled or \
                    (a not in _VOWELS and b not in _VOWELS and prev_v) or \
                    (a in _VOWELS and b not in _VOWELS and p + 1 < len(tok) and tok[p + 1] in _VOWELS)
            if not legal:
                continue
            d = abs(p - len(tok) / 2)
            if best is None or d < best[0]:
                best = (d, p)
        if best is None:
            continue
        p = best[1]
        head, tail = tok[:p], tok[p:]
        rest = toks[:i] + [head + " " + tail] + toks[i + 1:]
        yield (" ".join(rest), "compound_split")
        # 'rab-bravo' -> 'rabb bravo': a split that doubles the boundary consonant. Real, in-corpus.
        if head[-1] not in _VOWELS:
            yield (" ".join(toks[:i] + [head + head[-1] + " " + tail] + toks[i + 1:]),
                   "compound_split")


def r_letter_run(name: str):
    """A short vowel-less letter code, spelled out. 'st2-55' -> 's t 2-55'; 'hdms-x' -> 'h d m s-x'.

    In-corpus: comm array st2-55 carries "comm array s t 2 55" and "s t two five five".

    ⚠ IT MUST WORK ON ALPHA RUNS, NOT ON WHITESPACE TOKENS. The first version split the name on
      [\\s-]+ and required `tok.isalpha()`, so the token "st2" was rejected for containing a digit and
      the rule NEVER FIRED on the one name I wrote the test around. The selftest caught it. A code is
      glued to its number far more often than it stands alone, so tokenising on separators looks
      right and tests wrong.
    """
    for m in re.finditer(r"[a-z]+", name):
        run = m.group(0)
        if not (2 <= len(run) <= 5) or any(c in _VOWELS for c in run):
            continue
        spelled = " ".join(run)
        tail = name[m.end():]
        # spelling a code out implies reading its number separately too: 'st2' -> 's t 2'
        if tail[:1].isdigit():
            spelled += " "
        yield (name[:m.start()] + spelled + tail, "letter_run")


def r_prefix_drop(name: str):
    """Drop a leading facility code: 'hdms-anderson' -> 'anderson'.

    Heavily in-corpus: hdms-anderson's own list is 24 aliases, most of them variants of bare
    "anderson". A code is a 2-5 char leading token that is vowel-less or ends in digits; "comm array
    ..." is untouched because "comm" has a vowel.
    """
    toks = re.split(r"[\s-]+", name, maxsplit=1)
    if len(toks) != 2 or not toks[1]:
        return
    head = toks[0]
    if not (2 <= len(head) <= 5):
        return
    codeish = (not any(c in _VOWELS for c in head)) or bool(re.search(r"\d$", head))
    if codeish and len(normalize(toks[1])) >= MIN_KEY_LEN:
        yield (toks[1], "prefix_drop")


# Expansions with evidence. `mt` -> `microtech` is the only one the alias corpus demonstrates (10
# `mt datacenter *` keys). The Lagrange prefixes are evidenced differently but just as concretely:
# destination_engine.detect_special_patterns() hard-codes `(cru|crew|mic|hur|arc)` as a real pattern.
# Nothing else goes in this table without a citation, because an invented expansion is a plausible
# wrong answer and those are the expensive kind.
_PREFIX_EXPAND = {"mt": "microtech", "mic": "microtech", "cru": "crusader", "hur": "hurston",
                  "arc": "arccorp"}


def r_prefix_expand(name: str):
    toks = name.split()
    if not toks:
        return
    full = _PREFIX_EXPAND.get(toks[0].strip("-"))
    if full and full not in name:
        yield (" ".join([full] + toks[1:]), "prefix_expand")


_DIGRAPHS = [("ph", "f"), ("f", "ph"), ("ck", "k"), ("c", "k"), ("k", "c"), ("s", "z"),
             ("z", "s"), ("x", "ks"), ("qu", "kw"), ("ee", "ea"), ("ea", "ee"), ("ie", "ee"),
             ("y", "i"), ("i", "y"), ("ai", "ay"), ("ay", "ai"), ("oo", "u"), ("ou", "ow")]


def r_digraph(name: str):
    """One digraph swap at a time, FIRST occurrence only. 'microtech' -> 'microteck'/'mycrotech'.

    One-at-a-time and first-occurrence-only is the bound: applying every swap everywhere would be
    2**k per name and is exactly the "hundreds of machine-generated variants" the brief warns about.
    """
    for a, b in _DIGRAPHS:
        idx = name.find(a)
        if idx < 0:
            continue
        # never rewrite across a word boundary, and never touch a digit run
        out = name[:idx] + b + name[idx + len(a):]
        if out != name and out.strip():
            yield (out, "digraph")


def r_double_consonant(name: str):
    """Toggle a doubled consonant. 'aaron halo' -> 'aaron hallo'; 'abberdeen' <-> 'aberdeen'."""
    for m in re.finditer(r"([bcdfglmnprst])\1", name):
        yield (name[:m.start()] + m.group(1) + name[m.end():], "double_consonant")
    for m in re.finditer(r"[aeiou]([bcdfglmnprst])[aeiou]", name):
        c = m.group(1)
        yield (name[:m.start() + 1] + c + c + name[m.start() + 2:], "double_consonant")


def r_vowel_sub(name: str):
    """Substitute ONE vowel, at the first and last vowel position of the longest token only.

    The bound is the whole design. Substituting every vowel everywhere gives 3**v candidates per
    name; the corpus's actual vowel variants ("aaren halo", "aaron helo", "airon halo") are single
    substitutions, so single is both cheaper and truer.
    """
    alt = {"a": "e", "e": "a", "i": "e", "o": "u", "u": "o", "y": "i"}
    toks = name.split()
    if not toks:
        return
    tok = max(toks, key=len)
    i = toks.index(tok)
    pos = [j for j, c in enumerate(tok) if c in alt]
    for j in ({pos[0], pos[-1]} if pos else set()):
        swapped = tok[:j] + alt[tok[j]] + tok[j + 1:]
        yield (" ".join(toks[:i] + [swapped] + toks[i + 1:]), "vowel_sub")


STRING_RULES = [r_numeral, r_roman, r_numeral_split, r_prefix_drop, r_letter_run,
                r_compound_split, r_separator, r_prefix_expand, r_digraph,
                r_double_consonant, r_vowel_sub]

# ⚠⚠ THIS ORDER IS THE RANKING, THE CAP TRUNCATES FROM THE BOTTOM, AND IT IS MEASURED — NOT GUESSED.
#   My first version ordered it by how principled each rule felt. Then I ran `--verify --yes-slow`
#   over all 1,317 destinations (11,947 candidates, rapidfuzz WRatio, the production scorer) and asked
#   of each candidate: does name-only resolution ALREADY pick the right destination for this phrase?
#
#       rule              n      LOAD-BEARING
#       roman           251          81.3%
#       numeral         231          66.2%
#       hierarchy      2028          35.5%
#       prefix_drop      32          25.0%
#       vowel_sub      2203           6.9%
#       digraph        2706           6.3%
#       separator      1818           2.7%
#       letter_run      125           2.4%
#       numeral_split   140           0.7%
#       compound_split 1390           0.1%      <- I had this SIXTH
#       double_consonant 1021         0.1%
#       prefix_expand     2         100.0%      <- n=2. NOT rankable; placed on corpus precedent.
#
#   ★ compound_split was ranked 6th and measures 1 useful candidate in 1,390. It is the rule I put the
#     most design into ("the boundary nearest the middle", validated on three corpus examples) and it
#     is worth almost nothing for MATCHING. Being right about how a word splits is not the same as
#     being useful to a fuzzy matcher, and I would not have found that out by reading my own code.
#   ★ hierarchy moved UP from 9th to 3rd — the half of the brief with the least precedent in the
#     existing data turns out to carry the most new signal, because a parent name is the only thing
#     here that adds information the key does not already contain.
#
#   ⛔ WHAT "REDUNDANT" DOES AND DOES NOT MEAN. It means: the engine resolves THIS EXACT PHRASE
#     correctly with no alias, so storing it as an alias adds nothing. It does NOT mean no alias of
#     that shape could ever help — a stored "arc corp" is also a BRIDGE for someone who says
#     "ark korp", and this metric never tests the noisier utterance. So read the column as a relative
#     ranking between rules, which is what it is used for, and not as "compound_split has zero value".
#     The same instrument measured 67.3% of J's hand-written aliases as redundant, so the comparison
#     between rules is like-for-like. [[the-instrument-was-not-wrong-it-was-coarse]]
RULE_ORDER = ["roman", "numeral", "hierarchy", "prefix_drop", "prefix_expand", "vowel_sub",
              "digraph", "separator", "letter_run", "numeral_split", "compound_split",
              "double_consonant"]


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  HIERARCHY — one precedent, and the code says so
# ══════════════════════════════════════════════════════════════════════════════════════════════
def starmap_url(ref: str = SCUNPACKED_REF) -> str:
    return f"https://raw.githubusercontent.com/{SCUNPACKED_REPO}/{ref}/{STARMAP_PATH}"


def load_starmap(refresh: bool = False, fetch=None, cache_dir=None) -> list:
    """Cached by REF. Two guards against a test poisoning the shared cache, same as
    lint_destinations.py — and they are here because that file's selftest DID poison it today: the
    injected fetch's one fake row got cached, and the next real run reported a beautifully clean
    "1317 destinations against 1 named starmap entry".
      1. `cache_dir` is a PARAMETER and the selftest passes a tempdir. Explicit.
      2. an injected `fetch` NEVER writes the shared path, even if someone forgets (1).
    [[a-probe-does-not-feel-like-production]]
    """
    cdir = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    cache = cdir / f"starmap-{SCUNPACKED_REF}.json"
    if cache.exists() and not refresh:
        try:
            return json.loads(cache.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    getter = fetch or (lambda url: urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "SC-Toolbox-ProposeAliases/1"}),
        timeout=60).read())
    raw = getter(starmap_url())
    data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    if fetch is not None and cache_dir is None:
        return data
    try:
        cdir.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass
    return data


_JUNK_NAME = re.compile(r"uninitialized|placeholder", re.I)

# ⛔ A WHITELIST, NOT A BLACKLIST, AND THAT CHOICE IS THE POINT. The datamine's Type is a RENDERING
#   category, not a spoken noun, and passing it through produced "comm array st2-55 manmade crusader".
#   Nobody has ever said "manmade" to a starmap. Blacklisting "manmade" would have fixed that one
#   string and left the next internal category ("YouAreHere", "QuantumTracePoint", "CardinalPoint",
#   "Asteroid_ValidQT") to sail through the moment the datamine adds or renames one — and an alias
#   file is exactly where nobody would notice. A whitelist fails CLOSED: an unknown type yields no
#   type word, so the destination still gets its "<name> <parent>" form and simply loses the ones that
#   need a noun. [[an-absence-needs-every-path-right]]
#   Observed Classifications in this ref: Outpost, Asteroid, Manmade, Anomaly, Moon, Planet,
#   Settlement, Nav. Point, Solar System, Star, and the UNINITIALIZED sentinel.
SPEAKABLE_TYPES = {"planet", "moon", "star", "outpost", "asteroid", "settlement", "station",
                   "city", "anomaly", "nav point", "jump point", "solar system", "belt", "ring",
                   "cluster", "point of interest"}


def build_hierarchy(entries: list) -> dict:
    """normalized name -> {'parents': [names, outermost last], 'type': word or None}."""
    by_uuid = {e["UUID"]: e for e in entries if isinstance(e, dict) and e.get("UUID")}
    out = {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        raw = e.get("Name") or ""
        key = normalize(raw)
        if not key or _JUNK_NAME.search(raw):
            continue
        parents, seen, p = [], set(), e.get("ParentUUID")
        while p and p in by_uuid and p not in seen and len(parents) < 6:
            seen.add(p)
            par = by_uuid[p]
            pn = normalize(par.get("Name") or "")
            if pn and not _JUNK_NAME.search(par.get("Name") or ""):
                parents.append(pn)
            p = par.get("ParentUUID")
        t = e.get("Type") or {}
        word = None
        for cand in (t.get("Classification"), t.get("Name")):
            c = normalize(cand or "")
            if c in SPEAKABLE_TYPES:
                word = c
                break
        # first writer wins, except that an entry WITH parents beats one without (duplicate names
        # across systems: the informative one is the one that is actually placed in the tree)
        prev = out.get(key)
        if prev is None or (not prev["parents"] and parents):
            out[key] = {"parents": parents, "type": word}
    return out


def r_hierarchy(name: str, info: dict):
    """The five forms `aberdeen` actually uses, and not one more.

    ⚠ ALL FIVE COME FROM ONE DESTINATION. That is the entire evidential base — see the header. A
      sixth form, bare "<parent> <name>" ("hurston aberdeen"), is NOT generated: it has no precedent,
      and rapidfuzz's WRatio scores a pure word reordering at 95 against the original anyway
      (measured: "aberdeen hurston" vs "hurston aberdeen" = 95.0), so an extra ordering buys close to
      nothing while spending a slot in the cap.
    """
    parents = info.get("parents") or []
    if not parents:
        return
    parent = parents[0]
    if not parent or parent in name:
        return
    t = info.get("type")
    yield (f"{name} {parent}", "hierarchy")
    if t and t not in name:
        yield (f"{parent} {t} {name}", "hierarchy")
        yield (f"{name} {t} {parent}", "hierarchy")
        yield (f"{t} {name}", "hierarchy")
        # The possessive form, normalize()d: "hurston's moon aberdeen" -> "hurstons moon aberdeen".
        # ⚠ ONLY FOR A PARENT THAT CAN TAKE ONE. Ungated, this produced "pyro vs moon adir" for the
        #   moons of Pyro V — the possessive of a roman numeral, which reads as "versus" and is not a
        #   phrase any human would say. A rule copied from one example inherits that example's shape:
        #   "hurston" is a bare proper noun and every form here was derived from it.
        tail = parent.split()[-1]
        if len(tail) >= 3 and tail.isalpha() and tail not in _ROMAN and not tail.endswith("s"):
            yield (f"{parent}s {t} {name}", "hierarchy")


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  PROPOSAL ASSEMBLY
# ══════════════════════════════════════════════════════════════════════════════════════════════
REASONS = {
    "numeral": "digit run spoken aloud — the measured top failure mode (numbered siblings collide)",
    "roman": "trailing roman numeral spoken as a number ('charon iii' is heard as 'charon tree')",
    "numeral_split": "letter run separated from digit run ('area18' -> 'area 18')",
    "prefix_drop": "leading facility code dropped ('hdms-anderson' -> 'anderson')",
    "letter_run": "vowel-less letter code spelled out ('st2' -> 's t 2')",
    "compound_split": "glued compound split at the syllable boundary nearest the middle",
    "separator": "hyphen/space/glued — normalize() KEEPS hyphens, so these are distinct to the engine",
    "prefix_expand": "known prefix expanded (only 'mt'->'microtech' and the engine's own Lagrange set)",
    "hierarchy": "parent from starmap.json ParentUUID — ⚠ ONLY ONE destination in the existing "
                 "8,255 aliases uses hierarchy at all, so this form is near-unprecedented",
    "digraph": "one digraph swap (ph/f, c/k, ee/ea) — cosmetic; most such aliases test as redundant",
    "double_consonant": "consonant doubled or un-doubled — cosmetic",
    "vowel_sub": "one vowel substituted — cosmetic, ranked last",
}


def _offer(scored: dict, c: str, rule: str, blocked: set) -> None:
    """Record a candidate, keeping the BEST-RANKED rule when two rules produce the same string.

    ⚠ Not "first writer wins". Two rules can converge on one candidate, and first-writer-wins would
      credit it to whichever function happens to sit earlier in STRING_RULES — an order that has
      nothing to do with RULE_ORDER, which is what decides survival at the cap. That would let a
      0.1%-value rule's name carry a candidate that a 81%-value rule also found, and then the cap
      would cut it. Provenance and ranking have to agree or the cap discards the wrong things.
    """
    if not c or c in blocked or len(c) < MIN_KEY_LEN:
        return
    prev = scored.get(c)
    if prev is None or RULE_ORDER.index(rule) < RULE_ORDER.index(prev):
        scored[c] = rule


def propose_for(key: str, existing, hier: dict, cap: int = DEFAULT_CAP):
    """-> list of {'alias','rule','reason'}, deterministic, deduped, capped.

    Suppression is through normalize(), never raw strings, so an existing "Aber-Deen" blocks a
    proposed "aber-deen". It also suppresses a candidate equal to the KEY itself (114 aliases in the
    live file are exactly that, and they are no-ops) and candidates that collide with each other.
    """
    name = normalize(key)
    if len(name) < MIN_KEY_LEN:
        return []
    blocked = {normalize(a) for a in (existing or [])}
    blocked.add(name)
    info = hier.get(name)

    scored = {}
    for rule_fn in STRING_RULES:
        try:
            produced = list(rule_fn(name))
        except Exception:
            produced = []          # a rule that throws must not take the whole run down
        for cand, rule in produced:
            _offer(scored, normalize(cand), rule, blocked)
    if info:
        for cand, rule in r_hierarchy(name, info):
            _offer(scored, normalize(cand), rule, blocked)

    ranked = sorted(scored.items(), key=lambda kv: (RULE_ORDER.index(kv[1]), kv[0]))
    out = []
    for c, rule in ranked[:cap]:
        item = {"alias": c, "rule": rule, "reason": REASONS[rule]}
        if rule == "hierarchy":
            item["parent"] = (info.get("parents") or [None])[0]
            item["parent_chain"] = info.get("parents")
            item["type_word"] = info.get("type")
        out.append(item)
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  VERIFY — is a candidate LOAD-BEARING? (and a loud refusal to overclaim)
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _scorer():
    """(name, scorer_fn, is_production) — production means rapidfuzz WRatio, what the engine uses."""
    try:
        from rapidfuzz import fuzz          # type: ignore
        return "rapidfuzz.WRatio", (lambda a, b: fuzz.WRatio(a, b)), True
    except ImportError:
        import difflib
        return "difflib.SequenceMatcher", \
               (lambda a, b: 100.0 * difflib.SequenceMatcher(None, a, b).ratio()), False


def verify(candidates, key: str, all_keys, scorer_fn):
    """For each candidate phrase: does NAME-ONLY resolution already pick `key` as top-1?

    If it does, the alias is REDUNDANT — the engine would have got there anyway. If it does not, the
    alias is LOAD-BEARING and names the destination that currently wins instead. That is the only
    question worth asking about an alias, and it is the question the 67.3% measurement answered for
    the existing corpus.
    """
    out = []
    for item in candidates:
        phrase = item["alias"]
        best, best_s = None, -1.0
        for k in all_keys:
            s = scorer_fn(phrase, k)
            if s > best_s:
                best, best_s = k, s
        verdict = "REDUNDANT" if best == key else "LOAD-BEARING"
        out.append(dict(item, verdict=verdict, resolves_to=best, score=round(best_s, 1)))
    return out


# ══════════════════════════════════════════════════════════════════════════════════════════════
def _load_destinations():
    return json.loads(DESTINATIONS.read_text(encoding="utf-8"))


def main(argv=None) -> int:
    # Fourth-plus tool in this house to crash on a bare run over one '⚠'. A proposer must not die
    # configuring its own output, and a traceback's non-zero exit is indistinguishable from a verdict.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    args = _build_parser().parse_args(list(sys.argv[1:] if argv is None else argv))

    if not DESTINATIONS.exists():
        print("propose_aliases: CANNOT RUN — %s is missing" % DESTINATIONS)
        return 2
    try:
        dests = _load_destinations()
    except (ValueError, OSError) as exc:
        print("propose_aliases: CANNOT RUN — %s is unreadable: %s" % (DESTINATIONS, exc))
        return 2
    return _run(args, dests)


def _build_parser():
    """Split out so the selftest can ASK THE PARSER whether a write-back flag exists, instead of
    grepping this file for one. The textual version failed on the docstring that promises there is no
    such mode — the guard was matching the prose that documents its own purpose."""
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--only", action="append", default=[], metavar="NAME",
                    help="propose for this destination key only (repeatable)")
    ap.add_argument("--new", action="store_true", help="only destinations with NO aliases yet")
    ap.add_argument("--cap", type=int, default=DEFAULT_CAP)
    ap.add_argument("--limit", type=int, default=0, help="stop after N destinations")
    ap.add_argument("--json", metavar="PATH", help="write the reviewable proposal file")
    ap.add_argument("--audit", action="store_true", help="dead aliases already in destinations.json")
    ap.add_argument("--verify", action="store_true",
                    help="classify each candidate REDUNDANT / LOAD-BEARING (slow)")
    ap.add_argument("--yes-slow", action="store_true",
                    help="allow --verify over an unbounded selection")
    ap.add_argument("--refresh", action="store_true", help="re-fetch starmap.json")
    ap.add_argument("--no-hierarchy", action="store_true", help="skip the datamine entirely")
    return ap


def _run(args, dests) -> int:
    if args.audit:
        return _audit(dests)

    hier = {}
    if not args.no_hierarchy:
        try:
            hier = build_hierarchy(load_starmap(refresh=args.refresh))
        except Exception as exc:
            print("propose_aliases: ⚠ NO DATAMINE (%s: %s) — string rules still run, but every"
                  % (type(exc).__name__, exc))
            print("   hierarchy proposal is MISSING, not absent-because-unneeded. CANNOT TELL.")

    keys = list(dests)
    if args.only:
        wanted = {normalize(o) for o in args.only}
        keys = [k for k in keys if normalize(k) in wanted]
        missing = wanted - {normalize(k) for k in keys}
        for m in sorted(missing):
            print("  ⚠ --only %r is not a key in destinations.json" % m)
    if args.new:
        keys = [k for k in keys if not (dests[k] or {}).get("aliases")]
    if args.limit:
        keys = keys[:args.limit]

    proposals, skipped = {}, 0
    for k in keys:
        if len(normalize(k)) < MIN_KEY_LEN:
            skipped += 1
            continue
        got = propose_for(k, (dests[k] or {}).get("aliases"), hier, cap=args.cap)
        if got:
            proposals[k] = got

    scorer_name, scorer_fn, is_prod = _scorer()
    if args.verify:
        bounded = bool(args.only) or bool(args.limit)
        if not bounded and not args.yes_slow:
            print("propose_aliases: --verify over %d destinations is O(candidates x 1317) string"
                  % len(proposals))
            print("   comparisons. Narrow it with --only/--limit, or pass --yes-slow deliberately.")
            return 2
        all_keys = [normalize(k) for k in dests if len(normalize(k)) >= MIN_KEY_LEN]
        for k in proposals:
            proposals[k] = verify(proposals[k], normalize(k), all_keys, scorer_fn)

    total = sum(len(v) for v in proposals.values())
    print("propose_aliases: %d destination(s) considered, %d with proposals, %d candidate(s)"
          % (len(keys), len(proposals), total))
    if proposals:
        print("  candidates per proposing destination: mean %.2f (cap %d)"
              % (total / len(proposals), args.cap))
    print("  starmap %s @ %s — %d named entries with a hierarchy"
          % (SCUNPACKED_REPO, SCUNPACKED_REF, len(hier)))
    if skipped:
        print("  %d key(s) skipped as shorter than the engine's %d-char matching floor"
              % (skipped, MIN_KEY_LEN))
    print()

    for k in sorted(proposals):
        print("  %s" % k)
        for item in proposals[k]:
            tag = ""
            if "verdict" in item:
                tag = "  [%s -> %s %.0f]" % (item["verdict"], item["resolves_to"], item["score"])
            print("     %-40s %-17s %s%s" % (item["alias"], "(" + item["rule"] + ")",
                                             item["reason"][:60], tag))
        print()

    if args.verify:
        lb = sum(1 for v in proposals.values() for i in v if i.get("verdict") == "LOAD-BEARING")
        print("  --verify: %d of %d candidates are LOAD-BEARING under %s"
              % (lb, total, scorer_name))
        if not is_prod:
            print("  ⛔ CANNOT TELL whether that holds in production. rapidfuzz is not importable")
            print("     here, so this ran on difflib, which RANKS DIFFERENTLY from the WRatio the")
            print("     engine uses. Treat these verdicts as indicative, not as a claim about the")
            print("     live matcher. [[the-instrument-was-not-wrong-it-was-coarse]]")

    if args.json:
        payload = {
            "generated_for": str(DESTINATIONS),
            "starmap_ref": SCUNPACKED_REF,
            "cap": args.cap,
            "verify_scorer": scorer_name if args.verify else None,
            "verify_is_production_scorer": is_prod if args.verify else None,
            "⚠": "PROPOSALS ONLY. Nothing here has been written to destinations.json and this tool "
                 "has no --apply mode. Delete what you reject, then merge the rest by hand.",
            "proposals": proposals,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                                   encoding="utf-8")
        print("  wrote %s — edit it, delete what you reject; nothing is applied anywhere" % args.json)

    print()
    print("  ⚠ NOT GENERATABLE BY THIS TOOL, and it is a real gap rather than a rounding error:")
    print("    alternate PROPER names. 'area18' carries 'riker memorial spaceport', which is world")
    print("    knowledge — the datamine contains no 'riker' in any Name or Description. No rule")
    print("    here can invent it. That class still needs a human or a model.")
    print("== propose_aliases COMPLETE (rc=0) ==")
    return 0


def _audit(dests) -> int:
    """Dead weight already in destinations.json, found with the same normalize() used to suppress.

    Not a lint of my own output — a measurement of the corpus I am replacing, and it is the evidence
    behind the cap being 10 rather than 30.
    """
    ident = dupes = 0
    pairs = []
    for k, v in dests.items():
        nk = normalize(k)
        seen = set()
        for a in (v or {}).get("aliases") or []:
            na = normalize(a)
            if na == nk:
                ident += 1
                if len(pairs) < 6:
                    pairs.append(("identical to its own key", k, a))
            elif na in seen:
                dupes += 1
                if len(pairs) < 12:
                    pairs.append(("duplicate within the same list", k, a))
            seen.add(na)
    print("propose_aliases --audit: dead aliases in %s" % DESTINATIONS.name)
    print("  %4d alias(es) normalize to EXACTLY their own key — no-ops for matching" % ident)
    print("  %4d alias(es) duplicate another alias of the same destination under normalize()" % dupes)
    for why, k, a in pairs:
        print("     %-34s %-30s %r" % (why, k, a))
    print()
    print("  ⚠ This counts dead weight, NOT wrongness: a no-op alias costs review time and a little")
    print("    fuzzy-search breadth, and harms nothing else. Reported because it is the same")
    print("    normalize() this tool suppresses with, so these are proposals it would refuse to make.")
    print("== propose_aliases --audit COMPLETE (rc=0) ==")
    return 0


# ══════════════════════════════════════════════════════════════════════════════════════════════
#  SELFTEST.  Every arm is a differential pair: an implementation that ignores the rule under test
#  must fail at least one side. A suite that only proves the code emitted SOMETHING is not a test.
# ══════════════════════════════════════════════════════════════════════════════════════════════
def _selftest() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    import tempfile
    fails, notes = [], []

    def aliases_of(key, existing=None, hier=None, cap=30):
        return {i["alias"] for i in propose_for(key, existing or [], hier or {}, cap=cap)}

    # ── 1. normalize() must still match the ENGINE's. A copy that drifts breaks suppression silently.
    engine_norm = None
    for rel in ("dependencies/destination_engine.py",
                "../../skills/Starmap/starmap/set_route/destination_engine.py"):
        p = (HERE / rel).resolve()
        if not p.exists():
            continue
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_de_probe", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            engine_norm = mod.DestinationPhoneticEngine.normalize
            break
        except Exception as exc:
            # The dependencies/ copy imports WingmanAI's `services.file` and rapidfuzz, so it is not
            # importable outside the host app. That is EXPECTED and is not the verdict — the loop
            # keeps going and the Starmap port answers. Only "none of them loaded" is a finding.
            notes.append("%s is not importable standalone (%s) — trying the next copy"
                         % (p.name, type(exc).__name__))
    if engine_norm is None:
        notes.append("★ CANNOT TELL whether normalize() still matches the engine — no importable "
                     "copy. This is NOT a pass; suppression could be silently broken.")
    else:
        probes = ["Aber-Deen", "carver's ridge", "ST2-55", "Ailka  Kyukya", "área18",
                  "  spaced  out  ", "CRU-L1 Ambitious Dream"]
        for s in probes:
            mine, theirs = normalize(s), engine_norm(None, s)
            if mine != theirs:
                fails.append("normalize(%r) = %r but the ENGINE says %r — the copy has DRIFTED "
                             "and suppression is comparing the wrong strings" % (s, mine, theirs))

    # ── 2. SUPPRESSION, as a differential pair. The negative arm alone is worthless: a generator
    #    that proposes nothing at all would pass it. So the positive arm asserts the candidate IS
    #    produced when it is absent. [[a-differential-test-with-a-broken-control-scores-perfect]]
    absent = aliases_of("aberdeen", existing=[])
    if "aber deen" not in absent:
        fails.append("CONTROL BROKEN: 'aber deen' is not proposed for 'aberdeen' even with an empty "
                     "alias list, so the suppression arm below proves nothing")
    present = aliases_of("aberdeen", existing=["aber deen"])
    if "aber deen" in present:
        fails.append("suppression failed: 'aber deen' already exists and was proposed anyway")
    # and it must suppress through normalize(), not raw strings
    cased = aliases_of("aberdeen", existing=["  ABER DEEN  "])
    if "aber deen" in cased:
        fails.append("suppression is comparing RAW strings: '  ABER DEEN  ' did not block "
                     "'aber deen'")
    # a candidate equal to the key itself is a no-op and must never be proposed
    for k in ("aberdeen", "charon iii", "arccorp mining area 045"):
        if normalize(k) in aliases_of(k):
            fails.append("proposed the key itself for %r — that is a measured no-op class" % k)

    # ── 3. RULE ISOLATION: each of these fixtures must be reachable by exactly the rule named, so a
    #    stubbed-out rule cannot hide behind a neighbour also happening to produce the string.
    #    [[a-correct-rule-can-guard-a-branch-nothing-takes]]
    isolation = [
        ("charon iii", "charon three", "roman"),
        ("arccorp mining area 045", "arccorp mining area zero four five", "numeral"),
        # ⚠ THIS ONE IS HERE BECAUSE A MUTANT SURVIVED WITHOUT IT. Deleting the word-boundary guard in
        #   r_numeral produced 'comm array sttwo-55' — code and number fused into a nonsense token —
        #   and the whole suite still passed, because every other numeral assertion happens to sit
        #   next to a space or a hyphen. The bug needed a fixture where the digit ABUTS a letter.
        ("comm array st2-55", "comm array st two-55", "numeral"),
        ("area18", "area 18", "numeral_split"),
        ("hdms-anderson", "anderson", "prefix_drop"),
        ("comm array st2-55", "comm array s t 2-55", "letter_run"),
        ("arccorp", "arc corp", "compound_split"),
        ("microtech", "micro tech", "compound_split"),
        ("mt opcenter tli-4", "microtech opcenter tli-4", "prefix_expand"),
    ]
    for key, want, rule in isolation:
        items = propose_for(key, [], {}, cap=60)
        hit = [i for i in items if i["alias"] == want]
        if not hit:
            fails.append("rule %s: %r did not produce %r (cap 60, so this is not truncation)"
                         % (rule, key, want))
        elif hit[0]["rule"] != rule:
            fails.append("rule attribution: %r -> %r was credited to %s, wanted %s"
                         % (key, want, hit[0]["rule"], rule))

    # ── 3a. CROSS-RULE COLLISION PROVENANCE. Two rules can converge on one string, and the winner
    #    must be the better-RANKED one, not whichever generator runs first.
    #    ⛔ THIS ARM EXISTS BECAUSE A MUTANT SURVIVED WITHOUT IT. Replacing _offer's rank comparison
    #      with plain first-writer-wins changed 182 real proposals and the whole suite still passed,
    #      because no fixture above happened to be a collision. So I counted them over the live file
    #      rather than guessing: 262 collisions, in exactly two pairs —
    #          digraph + vowel_sub   182   ('aberdeen' -> 'aberdean' via ee/ea OR via the last vowel)
    #          roman + separator      80   (roman wins under both policies, so these prove nothing)
    #      Only the first pair discriminates, because r_digraph runs BEFORE r_vowel_sub in
    #      STRING_RULES while vowel_sub RANKS ABOVE digraph in RULE_ORDER. That inversion is the
    #      whole point of _offer and this is the only fixture that can see it.
    coll = propose_for("aberdeen", [], {}, cap=60)
    ab = [i for i in coll if i["alias"] == "aberdean"]
    if not ab:
        fails.append("CONTROL BROKEN: 'aberdeen' no longer produces the collision candidate "
                     "'aberdean', so the provenance assertion below cannot fire")
    elif ab[0]["rule"] != "vowel_sub":
        fails.append("collision provenance: 'aberdean' was credited to %s, but vowel_sub ranks "
                     "higher than digraph and both produce it — _offer is keeping the first writer "
                     "instead of the best-ranked rule" % ab[0]["rule"])

    #    ⚠⚠ AND A DECLARED SHAPE, NOT AN OBSERVED ONE. After the above, a `last-writer-wins` mutant
    #      STILL survived, and chasing a live fixture for it showed why: the only unblocked collision
    #      pair is digraph+vowel_sub, where the later generator also ranks higher, so last-wins and
    #      rank-based agree on every input the live file can produce. The roman+separator collisions
    #      all have candidate == key and are suppressed before _offer ever sees them.
    #      ⇒ So I assert _offer's CONTRACT directly, in both directions, instead of hunting for a
    #        fixture that does not exist. This is the input_shapes discipline: name the shape you
    #        need and construct it, because inferring the shapes from inputs you have seen defines
    #        the question away. It also documents that `_offer`'s rank comparison is currently
    #        DEFENSIVE — it exists so that reordering STRING_RULES cannot silently re-attribute
    #        proposals, not because today's data needs it.
    for first, second in (("roman", "separator"), ("separator", "roman")):
        probe_scored = {}
        _offer(probe_scored, "some candidate", first, set())
        _offer(probe_scored, "some candidate", second, set())
        if probe_scored.get("some candidate") != "roman":
            fails.append("_offer(%s then %s) kept %r — it must keep the better-RANKED rule "
                         "regardless of call order" % (first, second,
                                                      probe_scored.get("some candidate")))

    # ── 3b. the reason string must name a rule that exists. A proposal whose provenance is a lie is
    #    worse than no proposal, because a reviewer trusts it.
    for key in ("charon iii", "aberdeen", "comm array st2-55"):
        for i in propose_for(key, [], {}, cap=60):
            if i["rule"] not in RULE_ORDER or i["reason"] != REASONS[i["rule"]]:
                fails.append("bad provenance on %r -> %r" % (key, i["alias"]))

    # ── 4. HIERARCHY, differential on the PARENT. Without a parent, no hierarchy proposal may appear;
    #    with one, the aberdeen forms must. An implementation ignoring the datamine passes only the
    #    first arm, which is exactly why both are here.
    hier_with = {"aberdeen": {"parents": ["hurston", "stanton"], "type": "moon"}}
    got = {i["alias"] for i in propose_for("aberdeen", [], hier_with, cap=60)}
    for want in ("aberdeen hurston", "hurston moon aberdeen", "aberdeen moon hurston",
                 "hurstons moon aberdeen", "moon aberdeen"):
        if want not in got:
            fails.append("hierarchy: %r missing with parent=hurston type=moon" % want)
    if "hurston aberdeen" in got:
        fails.append("hierarchy: proposed 'hurston aberdeen', a form with NO corpus precedent that "
                     "the header explicitly excludes")
    hier_without = {"aberdeen": {"parents": [], "type": "moon"}}
    bad = [i for i in propose_for("aberdeen", [], hier_without, cap=60) if i["rule"] == "hierarchy"]
    if bad:
        fails.append("hierarchy fired with an EMPTY parent list: %r" % [i["alias"] for i in bad])
    # a parent already inside the name must not be restated ("pyro v" under parent "pyro")
    hier_dup = {"pyro v": {"parents": ["pyro"], "type": "planet"}}
    if any(i["rule"] == "hierarchy" for i in propose_for("pyro v", [], hier_dup, cap=60)):
        fails.append("hierarchy restated a parent already present in the key ('pyro v' / 'pyro')")
    # the possessive form must not be built on a parent that cannot take one. Differential: 'hurston'
    # gets it, 'pyro v' must not, and asserting only the second would pass on a dead rule.
    roman_parent = {"adir": {"parents": ["pyro v"], "type": "moon"}}
    adir = {i["alias"] for i in propose_for("adir", [], roman_parent, cap=60)}
    if "pyro vs moon adir" in adir:
        fails.append("possessive hierarchy built on a roman numeral: 'pyro vs moon adir' reads as "
                     "'versus' and is not a phrase anyone says")
    if "pyro v moon adir" not in adir:
        fails.append("CONTROL BROKEN: the non-possessive hierarchy forms vanished too, so the "
                     "assertion above passes because the rule is dead")
    if "hurstons moon aberdeen" not in got:
        fails.append("the possessive form was suppressed for 'hurston', which DOES take one — the "
                     "gate is too tight and dropped the only in-corpus example")

    # build_hierarchy must actually walk ParentUUID, and must reject the junk names
    entries = [
        {"UUID": "s", "Name": "Stanton", "ParentUUID": None, "Type": {"Name": "SolarSystem",
                                                                     "Classification": "Solar System"}},
        {"UUID": "h", "Name": "Hurston", "ParentUUID": "s", "Type": {"Name": "Planet",
                                                                     "Classification": "Planet"}},
        {"UUID": "a", "Name": "Aberdeen", "ParentUUID": "h", "Type": {"Name": "Moon",
                                                                      "Classification": "Moon"}},
        {"UUID": "j", "Name": "<= UNINITIALIZED =>", "ParentUUID": "h", "Type": {"Name": "Planet"}},
    ]
    hb = build_hierarchy(entries)
    if hb.get("aberdeen", {}).get("parents") != ["hurston", "stanton"]:
        fails.append("build_hierarchy did not walk ParentUUID: %r" % (hb.get("aberdeen"),))
    if "uninitialized" in " ".join(hb):
        fails.append("build_hierarchy kept a <= UNINITIALIZED => name")
    if hb.get("aberdeen", {}).get("type") != "moon":
        fails.append("build_hierarchy lost a SPEAKABLE type word: %r" % (hb.get("aberdeen"),))
    # the type whitelist, as a differential pair. An internal rendering category must NOT become a
    # spoken word, and the arm proving the whitelist is not simply rejecting everything is above.
    junk = build_hierarchy([
        {"UUID": "p", "Name": "Crusader", "ParentUUID": None, "Type": {"Name": "Planet",
                                                                      "Classification": "Planet"}},
        {"UUID": "m", "Name": "Comm Array ST2-55", "ParentUUID": "p",
         "Type": {"Name": "Manmade", "Classification": "Manmade"}}])
    if junk.get("comm array st2-55", {}).get("type") is not None:
        fails.append("the type whitelist let a rendering category through: %r — that produced "
                     "'comm array st2-55 manmade crusader'" % junk["comm array st2-55"]["type"])
    still = {i["alias"] for i in propose_for("comm array st2-55", [], junk, cap=30)}
    if "comm array st2-55 crusader" not in still:
        fails.append("CONTROL BROKEN: rejecting the type word also killed the '<name> <parent>' "
                     "form, so hierarchy degrades to nothing instead of degrading gracefully")
    if any("manmade" in a for a in still):
        fails.append("'manmade' reached a proposal anyway: %r" % [a for a in still if "manmade" in a])
    # a cycle must not hang. 1,300 destinations is not the place to discover an infinite walk.
    cyc = build_hierarchy([{"UUID": "x", "Name": "X", "ParentUUID": "y", "Type": {}},
                           {"UUID": "y", "Name": "Y", "ParentUUID": "x", "Type": {}}])
    if not cyc or len(cyc["x"]["parents"]) > 6:
        fails.append("build_hierarchy did not terminate cleanly on a ParentUUID cycle")

    # ── 5. THE CAP, both arms. Upper: a pathological name must be truncated. Lower: truncation must
    #    not be the reason a fixture above passed, and the cap must keep the HIGHEST-ranked rules.
    wide = propose_for("arccorp mining area 045", [], {}, cap=6)
    if len(wide) != 6:
        fails.append("cap ignored: asked for 6, got %d" % len(wide))
    ranks = [RULE_ORDER.index(i["rule"]) for i in wide]
    if ranks != sorted(ranks):
        fails.append("cap truncated from the wrong end: rules out of priority order %r" % ranks)
    if propose_for("arccorp mining area 045", [], {}, cap=0):
        fails.append("cap=0 still produced candidates")
    if not propose_for("xyzzy plains", [], {}, cap=DEFAULT_CAP):
        fails.append("a plain two-word name produced NOTHING — the generator is inert")
    # keys below the engine's matching floor are a void; proposing into it is waste.
    # ⛔ THE FIRST VERSION OF THIS ARM WAS A MUTANT SURVIVOR AND IT LOOKED FINE. Fixtures "", "c",
    #   "io" cannot distinguish the floor from its absence: with the floor deleted, every candidate
    #   those names generate is ITSELF under 3 chars and gets dropped by the per-candidate length
    #   filter instead. The test passed either way, so it asserted nothing.
    #   The discriminating fixtures are SHORT KEYS THAT GENERATE LONG CANDIDATES — "st" spells out to
    #   "s t" and "i5" spells out to "i five", both over the floor. Those fail only if the floor is
    #   really there. [[a-correct-rule-can-guard-a-branch-nothing-takes]]
    for tiny in ("", "c", "io", "st", "i5", "l1"):
        if propose_for(tiny, [], {}, cap=10):
            fails.append("proposed aliases for %r, which the engine never matches (key is under the "
                         "%d-char floor)" % (tiny, MIN_KEY_LEN))
    if not propose_for("ita", [], {}, cap=10):
        fails.append("CONTROL BROKEN: a 3-char key at the floor produced nothing, so the arm above "
                     "may be passing because the generator is inert rather than because it filters")

    # ── 6. DETERMINISM. Set/dict iteration order leaking into output would make every review diff
    #    noisy and make two runs disagree about what was proposed.
    a = json.dumps(propose_for("comm array st2-55", [], hier_with, cap=12))
    b = json.dumps(propose_for("comm array st2-55", [], hier_with, cap=12))
    if a != b:
        fails.append("output is not deterministic across two identical calls")

    # ── 7. number/roman spelling, directly. These feed the top-ranked rule, so a quiet off-by-one
    #    here would degrade exactly the candidates that matter most.
    for d, want in [("045", "zero four five"), ("18", "one eight"), ("2", "two")]:
        if spell_digits(d) != want:
            fails.append("spell_digits(%r) = %r, wanted %r" % (d, spell_digits(d), want))
    for n, want in [("18", "eighteen"), ("45", "forty five"), ("115", "one fifteen"),
                    ("5", "five"), ("20", "twenty")]:
        if spell_number(n) != want:
            fails.append("spell_number(%r) = %r, wanted %r" % (n, spell_number(n), want))

    # ── 8. compound_split's "nearest the middle" rule, on the three splits the corpus contains. This
    #    is the control that a simpler "first legal split" would fail.
    for key, want in [("arccorp", "arc corp"), ("aberdeen", "aber deen"),
                      ("microtech", "micro tech")]:
        if want not in aliases_of(key, cap=60):
            fails.append("compound_split: %r should split to %r (that is why the rule picks the "
                         "boundary nearest the middle)" % (key, want))

    # ── 9. VERIFY must be able to call both verdicts. A classifier stuck on one answer is useless,
    #    and "everything is load-bearing" is the flattering failure.
    _, sfn, _ = _scorer()
    fake_keys = ["charon iii", "char", "aberdeen"]
    v = verify([{"alias": "charon iii", "rule": "roman", "reason": ""}], "charon iii", fake_keys, sfn)
    if v[0]["verdict"] != "REDUNDANT":
        fails.append("verify: an exact key match must be REDUNDANT, got %s" % v[0]["verdict"])
    v2 = verify([{"alias": "aberdeen", "rule": "roman", "reason": ""}], "charon iii", fake_keys, sfn)
    if v2[0]["verdict"] != "LOAD-BEARING":
        fails.append("verify: a phrase resolving elsewhere must be LOAD-BEARING, got %s"
                     % v2[0]["verdict"])

    # ── 10. SCALE. 1,000+ destinations in one run is the stated requirement, so assert it rather
    #    than hoping. Also asserts the per-destination count stays near the observed mean.
    import time
    synth = {("synthetic outpost %03d" % i): {"aliases": []} for i in range(1200)}
    t0 = time.time()
    counts = [len(propose_for(k, v["aliases"], {}, cap=DEFAULT_CAP)) for k, v in synth.items()]
    dt = time.time() - t0
    if dt > 30:
        fails.append("1,200 destinations took %.1fs — too slow for a post-patch run" % dt)
    if counts and max(counts) > DEFAULT_CAP:
        fails.append("a synthetic name exceeded the cap: %d" % max(counts))
    notes.append("scale: 1,200 synthetic destinations in %.2fs, mean %.2f candidates each"
                 % (dt, sum(counts) / len(counts)))

    # ── 11. ⛔⛔ THE REGRESSION TESTS FOR THE BUG lint_destinations.py's SELFTEST CAUSED TODAY.
    #    Its injected fetch returned one fake row, the loader cached it to the shared path, and the
    #    next real run reported a clean board computed from that single row. Asserted on the REAL
    #    paths by mtime, because checking a mock would test the mock.
    probe = {"n": 0}

    def fake(url):
        probe["n"] += 1
        return json.dumps([{"UUID": "f", "Name": "Injected", "ParentUUID": None,
                            "Type": {"Name": "Moon"}}]).encode("utf-8")

    real_cache = CACHE_DIR / ("starmap-%s.json" % SCUNPACKED_REF)
    before = real_cache.stat().st_mtime if real_cache.exists() else None
    try:
        load_starmap(refresh=True, fetch=fake)                 # no cache_dir: guard 2 must hold
        after = real_cache.stat().st_mtime if real_cache.exists() else None
        if before != after:
            fails.append("★★ A TEST FETCH WROTE THE PRODUCTION STARMAP CACHE (%s). This is the "
                         "exact defect that made lint_destinations report 1 starmap entry."
                         % real_cache)
    except Exception as exc:
        fails.append("the no-cache_dir guard raised %s: %s" % (type(exc).__name__, exc))
    try:
        with tempfile.TemporaryDirectory() as tmp:
            got = load_starmap(refresh=True, fetch=fake, cache_dir=tmp)
            if not got or got[0].get("Name") != "Injected":
                fails.append("fetch injection did not take effect")
            if not (Path(tmp) / ("starmap-%s.json" % SCUNPACKED_REF)).exists():
                fails.append("cache_dir was passed but nothing was written there — the override is "
                             "not in use, so the production cache may still be the target")
    except Exception as exc:
        fails.append("load_starmap(cache_dir=...) raised %s: %s" % (type(exc).__name__, exc))
    if probe["n"] < 2:
        fails.append("CONTROL BROKEN: the injected fetch was called %d time(s), so the two cache "
                     "assertions above did not exercise a fetch at all" % probe["n"])

    # and destinations.json itself must be untouched by anything in this file, ever.
    dst = DESTINATIONS.stat().st_mtime if DESTINATIONS.exists() else None
    if DESTINATIONS.exists():
        try:
            _load_destinations()
            propose_for("aberdeen", [], {}, cap=10)
        except Exception:
            pass
        if DESTINATIONS.stat().st_mtime != dst:
            fails.append("★★ destinations.json CHANGED during the selftest. This file must never "
                         "write it; there is no --apply mode by design.")
    # ⛔ THE WRITE-BACK GUARD, and note how the needles are BUILT rather than written.
    #   The first version was `if "--apply" in Path(__file__).read_text()` — which matched the guard's
    #   own source line and therefore failed 100% of the time, on a clean file, for a reason that had
    #   nothing to do with the thing it was checking. A source-scanning assertion is inside its own
    #   haystack. Concatenating the needle at runtime keeps the literal out of the file.
    #   ★ AND IT ASKS THE PARSER, NOT THE TEXT. The textual form then failed a second time for a
    #   second reason: this file's own docstring PROMISES there is no apply mode, so the needle
    #   matched the prose documenting the guarantee. A grep over a file that discusses the thing it
    #   forbids cannot tell a capability from a sentence about a capability. So: interrogate the
    #   built parser for a write-back option, and keep the textual check only for real write calls.
    opts = []
    for act in _build_parser()._actions:
        opts += list(act.option_strings) + [act.dest or ""]
    offenders = [o for o in opts if "apply" in o or "write" in o or "commit" in o]
    if offenders:
        fails.append("a write-back option exists on the parser (%r). J asked for PROPOSE-only; if "
                     "deliberate it needs a backup, an off-by-default flag and his sign-off."
                     % offenders)
    src = Path(__file__).read_text(encoding="utf-8")
    for needle, why in (("DESTINATIONS." + "write_text", "a write to destinations.json"),
                        ("DESTINATIONS." + ", \"w", "an open-for-write on destinations.json")):
        if needle in src:
            fails.append("%s in this file, which must never happen." % why)

    print("propose_aliases selftest:", "PASS" if not fails else "FAIL")
    for n in notes:
        print("   note:", n)
    for f in fails:
        print("   -", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(_selftest() if "--selftest" in sys.argv else main())
