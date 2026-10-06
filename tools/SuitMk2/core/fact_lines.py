"""fact_lines.py - the WORDING for a thing a companion knows about the pilot (J, 2026-10-05).

pilot_facts.py keeps the THING and never the sentence: that the pilot flies a Cutlass Black, goes to Grim HEX,
wants a Kraken. This module turns one such fact into one line a companion may say unasked. It gets a FACT, never
a sentence, and it never looks the fact's sentence up anywhere.

    line_for(fact, speaker, variant=0) -> str | None

fact is what the fact store hands out when it is asked for the next one: {"relation", "thing", "thing_kind",
...}. speaker is "elah" or "montaigne". The line is a template from data/fact_lines.json with the thing's name
dropped in, word for word; no model words it. variant picks which template (variant % how many there are), so
a caller that counts up never gets the same one twice running.

J's decisions the lines are held to:
  * the companions remember the thing, not the sentence, and never repeat the pilot's words back;
  * they may be warm; they may never push the pilot inward (attachment_gate's five moves) and never comment on
    an absence;
  * a gesture must never leave the pilot owing, and a companion's reasons are in plain sight. So a line here is
    a small, light remark or question about the thing: never a show of how much the companion remembers, never
    "I remembered that for you".

None, and so nothing is said, when:
  * the relation is not one of pilot_facts.RELATIONS, or the speaker is not one of the two;
  * the thing is not exactly a name the game data holds for that kind (pilot_facts.is_canonical), or has
    anything in it a name does not have;
  * the data file is missing, is not JSON, or has no template for that speaker, relation and kind;
  * the template does not have {thing} exactly once, or is longer than the character speaks (Elah one sentence,
    Montaigne two);
  * the FILLED line (not just the template: a name can break a line a template alone would pass) fails the chat
    gate (chat_contract.chat_problems, with the thing's name as all it was given), fails the attachment gate
    (attachment_gate.attachment_problems), or says it was told ("you told me", "I remember", "you said", a
    quotation mark).

Pure: no state, nothing written, no model, no network. Never raises. The data file is re-read when it changes on
disk, as the canon files are.

spec_for() wraps the line in the same fixed-text spec the core already speaks (a dev-history fact is one), so a
fact line goes down the path every other unprompted line goes down and meets every gate on it. The one claim in
the spec is the thing's own name, so the grounding gate lets a name with a number in it through (Area18, C2
Hercules) and refuses any other number.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Optional

log = logging.getLogger("suitmk2.fact_lines")

DATA = Path(__file__).resolve().parent.parent / "data"
PATH = DATA / "fact_lines.json"
SPEAKERS = ("elah", "montaigne")
SLOT = "{thing}"
SCENARIO = "pilot_fact"
MAX_SENTENCES = {"elah": 1, "montaigne": 2}
MAX_THING_CHARS = 60
# A companion never says it was told, and never quotes. The chat gate refuses the double quotation mark; the
# single ones are refused here, where no reply needs an apostrophe that is not inside a word.
_TOLD = re.compile(r"\byou (?:told|said|mentioned)\b|\byou(?:'ve| have) (?:told|said|mentioned)\b|"
                   r"\bi (?:remember|recall|remembered|noted)\b|\bas you said\b|\byou once\b|"
                   "[\"“”‘]|(?<![A-Za-z])'|'(?![A-Za-z])")
_SENTENCE_END = re.compile(r"[.!?]+(?=\s|$)")
_NOT_IN_A_NAME = re.compile(r"[{}\"“”\n\r\t<>\[\]|\\]")

_cache: tuple = (None, None, {})          # (path, mtime_ns, {"elah": {...}, "montaigne": {...}})


def _data() -> dict:
    """The "lines" object of the data file, re-read whenever the file has changed. {} when it cannot be read."""
    global _cache
    p = PATH
    try:
        m = p.stat().st_mtime_ns
    except OSError:
        if _cache[1] != -1 or _cache[0] != p:
            log.warning("fact lines file missing (%s); no fact line will be said", p)
        _cache = (p, -1, {})
        return {}
    if _cache[0] == p and _cache[1] == m:
        return _cache[2]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        lines = raw.get("lines") if isinstance(raw, dict) else None
        if not isinstance(lines, dict):
            raise ValueError("no 'lines' object")
    except (OSError, ValueError) as e:
        log.warning("fact lines file is not readable (%s: %s); no fact line will be said", type(e).__name__, e)
        lines = {}
    _cache = (p, m, lines)
    return lines


def _sentences(template: str) -> int:
    return len(_SENTENCE_END.findall(template.replace(SLOT, "X")))


def templates(speaker: str, relation: str, thing_kind: str) -> list[str]:
    """The usable templates for this speaker, relation and kind of thing, in file order. A template that does
    not have {thing} exactly once, or is too many sentences for the speaker, is left out. Never raises."""
    try:
        groups = (_data().get(speaker) or {}).get(relation)
        if not isinstance(groups, dict) or speaker not in SPEAKERS:
            return []
        out = []
        for kinds, lines in groups.items():
            if not isinstance(kinds, str) or thing_kind not in kinds.split() or not isinstance(lines, list):
                continue
            for ln in lines:
                if (isinstance(ln, str) and ln.count(SLOT) == 1 and "{" not in ln.replace(SLOT, "")
                        and "}" not in ln.replace(SLOT, "") and 1 <= _sentences(ln) <= MAX_SENTENCES[speaker]):
                    out.append(ln.strip())
        return out
    except Exception:
        return []


def all_templates() -> list[tuple]:
    """(speaker, relation, the kinds a group serves, template) for every line in the file, usable or not. For
    the tests and for whoever edits the file."""
    out = []
    try:
        for speaker, rels in _data().items():
            for relation, groups in (rels if isinstance(rels, dict) else {}).items():
                for kinds, lines in (groups if isinstance(groups, dict) else {}).items():
                    for ln in (lines if isinstance(lines, list) else []):
                        out.append((speaker, relation, tuple(str(kinds).split()), ln))
    except Exception:
        return []
    return out


def thing_ok(relation, thing, thing_kind) -> bool:
    """Is this a fact a line may be made from: a closed-set relation about a name exactly as the game data holds
    it for that kind? Free text can never be dropped into a line this way."""
    try:
        import pilot_facts as pf
        if relation not in pf.RELATIONS or thing_kind not in pf.THING_KINDS:
            return False
        if not isinstance(thing, str) or not thing.strip() or thing != thing.strip() or len(thing) > MAX_THING_CHARS:
            return False
        if _NOT_IN_A_NAME.search(thing):
            return False
        return pf.is_canonical(thing, thing_kind)
    except Exception:
        return False


def fill(template: str, thing: str) -> str:
    """The template with the name dropped in. A name that is all lower case (an activity: mining, salvage) gets
    a capital when it opens the line; a name with its own capitals (microTech) is left as the data spells it."""
    line = template.replace(SLOT, thing)
    if template.startswith(SLOT) and thing == thing.lower():
        line = line[:1].upper() + line[1:]
    return line


def line_problems(line: str, speaker: str, thing: str) -> list[str]:
    """Why a FILLED line may not be said; [] = it may. Both gates, and the two things neither of them looks for.
    Something that cannot be checked is a reason too."""
    try:
        import attachment_gate
        import chat_contract
        fails = list(chat_contract.chat_problems(speaker, line, thing))
        fails += [f"attachment: {m}" for m in attachment_gate.attachment_problems(line, speaker)
                  if f"attachment: {m}" not in fails]
        if _TOLD.search(line.lower().replace("’", "'")):
            fails.append("says it was told, or quotes")
        return fails
    except Exception as e:
        return [f"could not be checked ({type(e).__name__})"]


def line_for(fact: dict, speaker: str, variant: int = 0) -> Optional[str]:
    """One line this companion may say about this fact, or None. See the module's note for every way it is None."""
    try:
        if not isinstance(fact, dict) or speaker not in SPEAKERS:
            return None
        relation, thing, kind = fact.get("relation"), fact.get("thing"), fact.get("thing_kind")
        if not thing_ok(relation, thing, kind):
            return None
        have = templates(speaker, relation, kind)
        if not have:
            return None
        line = fill(have[int(variant) % len(have)], thing)
        if line_problems(line, speaker, thing):
            return None
        return line
    except Exception:
        return None


def spec_for(fact: dict, speaker: str, variant: int = 0) -> Optional[dict]:
    """The line as a fixed-text spec the core can speak like any other unprompted line, or None. Its only claim
    is the thing's own name; no model sees it."""
    try:
        line = line_for(fact, speaker, variant)
        if not line:
            return None
        n = len(line.split())
        return {"id": f"{SCENARIO}:{fact['relation']}:{speaker}", "scenario": SCENARIO, "speaker": speaker,
                "fixed_text": line,
                "claims": [{"id": "C1", "predicate": f"pilot.{fact['relation']}", "value": fact["thing"]}],
                "required_values": [], "length_words": [max(1, n - 3), n], "allowed_names": []}
    except Exception:
        return None
