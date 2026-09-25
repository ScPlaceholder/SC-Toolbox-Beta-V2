"""Pure, stdlib-only helpers shared by the Assistant and its tool workers.

Nothing here imports a toolbox skill, touches the network or writes a
file, so every function is unit-testable in isolation. The per-tool
worker subprocesses load this file by path (see workers/_worker_main.py)
because a worker's sys.path deliberately does NOT contain the Assistant
package: the Assistant ships its own ``config.py``/``tools.py`` which
would shadow the skill's modules of the same name.
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Callable, Iterable, Optional


class ToolFail(Exception):
    """A tool could not answer, with a message fit to say to the user.

    Distinct from a crash: a worker that raises ToolFail reports
    ``kind="tool"`` and the Assistant relays the message as-is.
    """


# ── fuzzy name matching ───────────────────────────────────────────────────

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}


def norm(s: Any) -> str:
    """Lowercase, punctuation to spaces, collapse whitespace.

    Standalone roman numerals I-IV become digits, so a spoken "Helix 1"
    or "Focus 3" finds "Helix I Mining Laser" / "Focus III Module".
    """
    toks = _NON_ALNUM.sub(" ", str(s or "").lower()).split()
    return " ".join(_ROMAN.get(t, t) for t in toks)


def squash(s: Any) -> str:
    return norm(s).replace(" ", "")


def match_score(query: str, candidate: str) -> float:
    """0..1 similarity of *query* against *candidate* (higher is better).

    Tiered so that exact / prefix / all-words matches always beat a mere
    spelling-distance match, and spelling distance still finds
    "quantanium" -> "Quantainium (Raw)".
    """
    q, c = norm(query), norm(candidate)
    if not q or not c:
        return 0.0
    if q == c:
        return 1.0
    qs, cs = q.replace(" ", ""), c.replace(" ", "")
    if qs == cs:
        return 0.98
    extra = max(0, len(c) - len(q))
    if c.startswith(q + " ") or c.startswith(q):
        return 0.9 - min(0.08, extra * 0.002)
    q_toks, c_toks = q.split(), c.split()
    if all(any(qt == ct for ct in c_toks) for qt in q_toks):
        return 0.85 - min(0.08, extra * 0.002)
    if all(any(qt in ct for ct in c_toks) for qt in q_toks):
        return 0.8 - min(0.08, extra * 0.002)
    if qs in cs:
        return 0.75 - min(0.08, extra * 0.002)
    # spelling distance, token by token
    per_tok = []
    for qt in q_toks:
        best = 0.0
        for ct in c_toks:
            if abs(len(ct) - len(qt)) > max(3, len(qt) // 2):
                continue
            r = difflib.SequenceMatcher(None, qt, ct).ratio()
            if r > best:
                best = r
        per_tok.append(best)
    tok = sum(per_tok) / len(per_tok) if per_tok else 0.0
    whole = difflib.SequenceMatcher(None, qs, cs).ratio()
    return 0.7 * max(tok, whole)


def best_matches(query: str, candidates: Iterable, key: Optional[Callable] = None,
                 limit: int = 5, min_score: float = 0.55) -> list:
    """Rank *candidates* by match_score; returns [(score, candidate), ...]."""
    key = key or (lambda x: x)
    scored = []
    for cand in candidates:
        name = key(cand)
        if not name:
            continue
        s = match_score(query, name)
        if s >= min_score:
            scored.append((s, cand))
    scored.sort(key=lambda t: (-t[0], len(str(key(t[1])))))
    return scored[:limit]


def resolve_one(query: str, candidates: Iterable, key: Optional[Callable] = None,
                what: str = "item", min_score: float = 0.55):
    """Best single match or ToolFail naming the closest alternatives.

    Returns (candidate, alternatives_names) where alternatives are the
    runner-up names, so the LLM can offer "did you mean".
    """
    key = key or (lambda x: x)
    ranked = best_matches(query, candidates, key=key, limit=6, min_score=min_score)
    if not ranked:
        near = best_matches(query, candidates, key=key, limit=3, min_score=0.3)
        hint = ", ".join(str(key(c)) for _, c in near)
        raise ToolFail(f"no {what} matching '{query}'"
                       + (f" (closest: {hint})" if hint else ""))
    best = ranked[0][1]
    alts = [str(key(c)) for _, c in ranked[1:] if key(c) != key(best)]
    seen, uniq = set(), []
    for a in alts:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return best, uniq[:4]


# ── Mission DB: which missions reward a blueprint ─────────────────────────
# Extracted from Mission_Database ui/modals/blueprint_detail.py (the
# "MISSIONS THAT REWARD THIS" block, ~lines 574-595) so it runs without Qt.

def blueprint_pool_ids(bp_name: str, blueprint_pools: dict) -> set:
    """Pool ids whose blueprint list contains *bp_name* (exact name)."""
    ids = set()
    for pool_id, pool in (blueprint_pools or {}).items():
        for bp_item in (pool or {}).get("blueprints", []) or []:
            name_check = bp_item.get("name", "") if isinstance(bp_item, dict) else ""
            if name_check == bp_name:
                ids.add(pool_id)
                break
    return ids


def missions_for_blueprint(bp_name: str, blueprint_pools: dict, contracts: list,
                           get_faction: Callable[[str], dict]) -> list:
    """Contracts whose blueprintRewards draw from a pool holding *bp_name*.

    Same join as the Mission DB blueprint modal: one row per contract,
    first matching reward entry wins. Adds a few fields the modal did not
    show (type, systems, reward) because a voice answer wants them.
    """
    pools = blueprint_pool_ids(bp_name, blueprint_pools)
    if not pools:
        return []
    out = []
    for contract in contracts or []:
        for reward_entry in (contract.get("blueprintRewards") or []):
            if reward_entry.get("blueprintPool") in pools:
                title = contract.get("title", "?") or "?"
                if title.startswith("@"):
                    title = contract.get("debugName", title)
                faction = get_faction(contract.get("factionGuid", "")) or {}
                out.append({
                    "title": title,
                    "faction": faction.get("name", "?"),
                    "chance": reward_entry.get("chance", 1),
                    "mission_type": contract.get("missionType", ""),
                    "systems": contract.get("systems") or contract.get("availableSystems") or [],
                    "reward_uec": contract.get("rewardUEC"),
                    "pool": reward_entry.get("poolName", ""),
                })
                break
    return out


def collapse_missions(rows: list, limit: int = 10) -> list:
    """Group identical (title, faction, type) rows; count variants."""
    groups: dict = {}
    for r in rows:
        k = (r.get("title"), r.get("faction"), r.get("mission_type"))
        g = groups.get(k)
        if g is None:
            g = dict(r)
            g["variants"] = 1
            g["systems"] = list(r.get("systems") or [])
            groups[k] = g
        else:
            g["variants"] += 1
            for s in r.get("systems") or []:
                if s not in g["systems"]:
                    g["systems"].append(s)
            if (r.get("reward_uec") or 0) > (g.get("reward_uec") or 0):
                g["reward_uec"] = r.get("reward_uec")
    out = list(groups.values())
    out.sort(key=lambda g: (-(g.get("chance") or 0), -(g.get("reward_uec") or 0)))
    return out[:limit]


# ── Mining: resource -> locations, deduplicated ───────────────────────────

def dedupe_locations(rows: list, group_labels: Optional[dict] = None) -> list:
    """Collapse resource_to_locations rows to one per (location, group).

    Keeps the highest max_pct / probability seen; sorts richest first.
    """
    group_labels = group_labels or {}
    best: dict = {}
    for r in rows or []:
        k = (r.get("location"), r.get("group"))
        prob = r.get("probability") or 0
        mx = r.get("max_pct") or 0
        cur = best.get(k)
        if cur is None or (prob, mx) > (cur["probability"], cur["max_pct"]):
            lab = group_labels.get(r.get("group"), {})
            best[k] = {
                "location": r.get("location"),
                "system": r.get("system"),
                "type": r.get("type"),
                "method": lab.get("label") if isinstance(lab, dict) else (lab or r.get("group")),
                "group": r.get("group"),
                "max_pct": round(float(mx), 1),
                "probability": round(float(prob), 3),
            }
    out = list(best.values())
    out.sort(key=lambda d: (-d["probability"], -d["max_pct"], str(d["location"])))
    return out


# ── confirmation parsing (agent yes/no gate) ──────────────────────────────

_NEG_PHRASES = ("not now", "not yet", "do not", "don't", "dont", "never mind",
                "nevermind", "hold off", "no thanks", "no thank you", "cancel",
                "not really", "rather not", "skip it")
_NEG_TOKENS = {"no", "nope", "nah", "negative", "not", "don't", "stop",
               "cancel", "wait", "later", "skip", "never"}
_YES_PHRASES = ("go for it", "go ahead", "do it", "sounds good", "pin it",
                "open it", "please do", "yes please", "of course", "make it so",
                "let's do it", "lets do it", "why yes")
_YES_TOKENS = {"y", "yes", "yeah", "yea", "ya", "yep", "yup", "sure", "ok",
               "okay", "k", "please", "affirmative", "absolutely", "definitely",
               "certainly", "confirm", "confirmed", "correct", "aye", "alright"}
_FILLER = {"please", "thanks", "thank", "you", "it", "that", "then", "and",
           "just", "right", "now", "go", "do", "one", "this", "sir", "mate",
           "buddy", "cool", "great", "fine", "yes", "oh"}


def classify_confirmation(text: str) -> Optional[str]:
    """'yes', 'no', or None (the user said something else).

    Any negative marker wins, so "okay no" and "yes but not now" are NOT
    yes. A yes needs a yes word/phrase and nothing but filler around it,
    so "sure, what ship?" is neither and goes back to the model.
    """
    t = (text or "").strip().lower()
    t = re.sub(r"[^\w\s']", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t:
        return None
    padded = f" {t} "
    toks = t.split()
    if any(f" {p} " in padded for p in _NEG_PHRASES) or any(w in _NEG_TOKENS for w in toks):
        return "no"
    rest = padded
    hit = False
    for p in sorted(_YES_PHRASES, key=len, reverse=True):
        if f" {p} " in rest:
            rest = rest.replace(f" {p} ", " ")
            hit = True
    left = rest.split()
    if any(w in _YES_TOKENS for w in left):
        hit = True
    if hit and all(w in _YES_TOKENS or w in _FILLER for w in left):
        return "yes"
    return None
