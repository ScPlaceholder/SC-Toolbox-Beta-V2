"""banter.py - two-voice exchanges (Elah <-> Montaigne), planned SEMANTICALLY up front.

ARCHITECTURE.md: "Banter: the second speaker's engine reads the first speaker's SEMANTIC message, not its
English. Hard turn cap + cooldown on every exchange." and "Two models in dialogue echo each other unless each
turn's stance is designed in."

So an exchange is NOT two realizer calls where the second one reads the first one's text. It is a short list of
specs planned together, before any wording exists:
  * turn 1 is an ordinary ambient-shaped spec (claims read by ambient_spec's own situation builders: this module
    invents no game data and extracts none of its own);
  * turn 2 is for the OTHER character. Its claims are a SUBSET of turn 1's (same id, predicate, value): it may
    not introduce a fact. Its stance RESPONDS to turn 1's stance through a designed relation (correct, deflate,
    disagree, digress) and it carries `responds_to` = {speaker, stance, move, relation} so a later realizer
    prompt can show it what it is answering. The realizer prompt format is NOT changed here;
  * an optional turn 3 goes back to the first speaker, claims a subset of turn 2's.
Only the move labels the character adapters were trained on (ambient_spec._ELAH_MOVES / _MONT_MOVES).

Repeating the other speaker's number is the most obvious echo there is, so a responding turn only REQUIRES a
number when its `restate` flag is set (a correction or deflation that answers with the fact). Otherwise the
number is allowed (it is in the claims) but not required.

BanterPolicy decides WHETHER an exchange may start or continue; run_exchange() speaks it turn by turn and stops at
the first turn that fails (no line, refused by the grounding gate, speech refused, or a hold came up). A half
exchange is fine; a turn answering a line that was never spoken is not.

Selftest (+ mutation run showing the selftest catches disabled guards):
    python banter.py --selftest
Designed pairs as a table:
    python banter.py --table
"""
from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ambient_spec import (_ELAH_MOVES, _MONT_MOVES, _situation_injury, _situation_reward,  # noqa: E402
                          _situation_extended_stay, _situation_busy_session, _situation_jurisdiction,
                          _situation_ship_context)

Spec = dict[str, Any]
_REPLY_LEN = (6, 30)          # a reply is shorter than the line it answers
RELATIONS = {"correct", "deflate", "disagree", "digress", "concede"}   # concede: turn 3 only

# Situations banter may be built from, highest-signal first (same order as ambient_spec._SITUATIONS).
# regen and quiet_interval are deliberately excluded: banter about a death or about nothing is not wanted.
_SITUATIONS: list[tuple[str, Callable[[dict, int], Optional[Spec]]]] = [
    ("injury_followup", _situation_injury),
    ("session_reward", _situation_reward),
    ("extended_stay", _situation_extended_stay),
    ("busy_session", _situation_busy_session),
    ("jurisdiction", _situation_jurisdiction),
    ("ship_context", _situation_ship_context),
]


def _t1(speaker: str, move: str, stance: str) -> dict:
    return {"speaker": speaker, "move": move, "stance": stance}


def _tn(speaker: str, move: str, stance: str, relation: str, carry: Iterable[str], restate: bool = False) -> dict:
    """A responding turn. `carry` names the parent's predicates it may keep; `restate` = must say the number."""
    return {"speaker": speaker, "move": move, "stance": stance, "relation": relation,
            "carry": tuple(carry), "restate": restate}


# Designed stance pairs. Montaigne only ever knows things secondhand (the suit's feed, the ship's log, a book);
# every Montaigne stance says or implies so. Elah states facts and does not decorate them.
PAIRS: dict[str, list[list[dict]]] = {
    "injury_followup": [
        [_t1("montaigne", "PILOT_CHARACTER", "the suit reports a wound; he admires how little the pilot makes of it"),
         _tn("elah", "CORRECTION", "making little of it is not treatment; the tier stands", "correct",
             ["suit.injury_body_part", "suit.injury_tier"], restate=True),
         _tn("montaigne", "SELF_DEPRECATION", "concedes the suit knows a body better than a ship does", "concede",
             ["suit.injury_body_part"])],
        [_t1("elah", "DEADPAN", "worth a caution flag, not a panic"),
         _tn("montaigne", "ESSAY_DIGRESSION", "he has read that pain is a teacher; he has only had it reported",
             "digress", ["suit.injury_body_part"])],
        [_t1("elah", "PRACTICAL", "a med bed will sort it; no rush yet"),
         _tn("montaigne", "SKEPTICAL_REVERSAL", "doubts the pilot will go, then allows the suit is usually right",
             "disagree", ["suit.injury_body_part"])],
    ],
    "session_reward": [
        [_t1("elah", "PRACTICAL", "worth banking before the next risk"),
         _tn("montaigne", "ESSAY_DIGRESSION", "he read somewhere that money is a servant that becomes a master",
             "digress", ["session.reward_auec"])],
        [_t1("montaigne", "GRAND_PHILOSOPHY_TO_TRIVIAL", "a fortune, by the ledger he was shown"),
         _tn("elah", "CORRECTION", "not a fortune; it is exactly the amount, no more", "correct",
             ["session.reward_auec"], restate=True)],
        [_t1("montaigne", "SKEPTICAL_REVERSAL", "money well earned, though he doubts it buys wisdom"),
         _tn("elah", "DEADPAN", "it was never meant to buy wisdom; it buys fuel", "deflate",
             ["session.reward_auec"])],
    ],
    "extended_stay": [
        [_t1("montaigne", "ESSAY_DIGRESSION", "lingering in one place is its own kind of travel"),
         _tn("elah", "DEADPAN", "it is a number of minutes, not a pilgrimage", "deflate",
             ["location.name", "location.minutes_this_visit"], restate=True)],
        [_t1("elah", "CALLBACK", "taking your time here, and that's fine"),
         _tn("montaigne", "NEAR_RECOGNITION", "the name is in the log; he half believes he has been here before",
             "digress", ["location.name"]),
         _tn("elah", "DEADPAN", "he has not; he has read about it", "correct", ["location.name"])],
    ],
    "busy_session": [
        [_t1("montaigne", "PILOT_CHARACTER", "a restless pilot, and he rather admires it"),
         _tn("elah", "DEADPAN", "it is a count of stops, not a character", "deflate",
             ["session.locations_visited"], restate=True)],
        [_t1("elah", "DEADPAN", "a lot of ground covered today"),
         _tn("montaigne", "HORSE_ANALOGY", "even a willing horse, he is told, needs a stable eventually",
             "digress", ["session.locations_visited"])],
    ],
    "jurisdiction": [
        [_t1("montaigne", "EVIDENCE_SKEPTIC", "laws change at every border; he doubts they grow wiser"),
         _tn("elah", "PRACTICAL", "wise or not, these are the ones that apply here", "disagree",
             ["jurisdiction.zone"])],
        [_t1("elah", "PRACTICAL", "know what the local law allows here"),
         _tn("montaigne", "SELF_DEPRECATION", "a ship obeys whatever law it is parked under; no moral achievement",
             "deflate", ["jurisdiction.zone"])],
    ],
    "ship_context": [
        [_t1("montaigne", "HORSE_ANALOGY", "the ship carried them here the way a horse carries a rider"),
         _tn("elah", "CORRECTION", "it is a ship; it went where it was flown", "correct", ["ship.name"])],
        [_t1("elah", "DEADPAN", "just noting where we are"),
         _tn("montaigne", "ESSAY_DIGRESSION", "the log says where we are; he wonders what it says of us",
             "digress", ["ship.system"])],
    ],
}


# ---- guards -----------------------------------------------------------------------------------------------------
def _move_ok(speaker: str, move: str) -> bool:
    return move in (_ELAH_MOVES if speaker == "elah" else _MONT_MOVES if speaker == "montaigne" else set())


def _check_pairs() -> None:
    """Import-time: every designed turn uses a trained move, speakers alternate, relations are known."""
    for scen, pool in PAIRS.items():
        assert pool, f"{scen}: empty pair pool"
        for pair in pool:
            assert 2 <= len(pair) <= 3, f"{scen}: an exchange is 2 or 3 turns"
            for i, t in enumerate(pair):
                assert _move_ok(t["speaker"], t["move"]), f"{scen}: {t['move']} is not a trained {t['speaker']} move"
                if i:
                    assert t["speaker"] != pair[i - 1]["speaker"], f"{scen}: turn {i + 1} is a monologue"
                    assert t["relation"] in RELATIONS, f"{scen}: unknown relation {t['relation']}"
                    assert t["relation"] != "concede" or i == 2, f"{scen}: concede is a turn-3 relation"


_check_pairs()


def _claim_key(c: dict) -> tuple:
    return (c["id"], c["predicate"], repr(c["value"]))


def _subset_violation(parent: Spec, child: Spec) -> list[str]:
    """The HARD rule for a responding turn: it may repeat, never add. Returns the offending claims (empty = ok)."""
    have = {_claim_key(c) for c in parent["claims"]}
    bad = [f"{c['predicate']}={c['value']!r}" for c in child["claims"] if _claim_key(c) not in have]
    ids = {c["id"] for c in child["claims"]}
    bad += [f"required claim {r} not in claims" for r in child.get("required_claims", []) if r not in ids]
    vals = {str(c["value"]) for c in child["claims"]}
    bad += [f"required value {v} not in claims" for v in child.get("required_values", []) if v not in vals]
    return bad


def _carry(parent: Spec, predicates: Iterable[str]) -> list[dict]:
    """The parent's claims whose predicate this turn keeps. Copies, never constructs: subset by construction."""
    want = set(predicates)
    return [dict(c) for c in parent["claims"] if c["predicate"] in want]


# ---- planning ---------------------------------------------------------------------------------------------------
def _match(state: dict) -> Optional[tuple[str, Spec]]:
    for scen, fn in _SITUATIONS:
        base = fn(state, 0)
        if base is not None:
            return scen, base
    return None


def _history_ids(history: Optional[list]) -> list[str]:
    out = []
    for h in history or []:
        if isinstance(h, str):
            out.append(h)
        elif isinstance(h, list) and h and isinstance(h[0], dict):
            out.append(h[0].get("exchange_id", ""))
        elif isinstance(h, dict):
            out.append(h.get("exchange_id", ""))
    return out


def _as_tier(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _pick(scen: str, variant: int, history: Optional[list]) -> int:
    """variant rotates; a pair used in the last (pool-1) exchanges is skipped when another is available."""
    pool = PAIRS[scen]
    recent = set(_history_ids(history)[-(len(pool) - 1):]) if len(pool) > 1 else set()
    for k in range(len(pool)):
        idx = (variant + k) % len(pool)
        if f"ban_{scen}_p{idx}" not in recent:
            return idx
    return variant % len(pool)


def plan_exchange(state: dict, variant: int = 0, history: Optional[list] = None, max_turns: int = 3) -> list[Spec]:
    """state -> a 2- or 3-turn exchange as a list of specs, or [] if no banter situation matches.

    `history`: previously planned exchanges (exchange-id strings, or the spec lists themselves).
    Stops early (returns the turns planned so far, or [] if that is only one) when a responding turn would carry
    no claim, or would break the subset rule. A single turn is not banter; ambient_spec already covers that.
    """
    m = _match(state)
    if m is None:
        return []
    scen, base = m
    idx = _pick(scen, variant, history)
    # A tier-3 injury is never banter about how little it matters: only pair 0, where Elah insists the tier stands.
    # Same defect as ambient_spec._SERIOUS_INJURY (a "no rush yet" cue on the worst tier), same correction.
    # Tier 1 is the severe one in SC (Tier 3 = Minor); this is the corrected reading.
    if scen == "injury_followup" and any(c.get("predicate") == "suit.injury_tier" and _as_tier(c.get("value")) == 1
                                         for c in base.get("claims", [])):
        idx = 0
    pair = PAIRS[scen][idx]
    ex_id = f"ban_{scen}_p{idx}"
    specs: list[Spec] = []
    for n, t in enumerate(pair[:max(0, max_turns)], start=1):
        if n == 1:
            claims = [dict(c) for c in base["claims"]]
            req, vals, length = list(base["required_claims"]), list(base["required_values"]), list(base["length_words"])
        else:
            parent = specs[-1]
            claims = _carry(parent, t["carry"])
            if not claims:
                break
            ids = {c["id"] for c in claims}
            req = [r for r in parent["required_claims"] if r in ids] or [claims[0]["id"]]
            nums = {str(c["value"]) for c in claims if not isinstance(c["value"], bool)}
            vals = [v for v in parent["required_values"] if v in nums] if t["restate"] else []
            length = list(_REPLY_LEN)
        spec = {"scenario": f"banter_{scen}", "speaker": t["speaker"], "rhetoric": [t["move"]], "claims": claims,
                "interpretation": {"owner": t["speaker"], "text": t["stance"]}, "required_claims": req,
                "required_values": vals, "length_words": length, "id": f"{ex_id}_t{n}",
                "exchange_id": ex_id, "turn": n}
        if n > 1:
            prev = specs[-1]
            spec["responds_to"] = {"speaker": prev["speaker"], "stance": prev["interpretation"]["text"],
                                   "move": prev["rhetoric"][0], "relation": t["relation"]}
            if _subset_violation(prev, spec) or _subset_violation(specs[0], spec):
                break
        specs.append(spec)
    return specs if len(specs) >= 2 else []


# ---- policy -----------------------------------------------------------------------------------------------------
# Each hold is (name, fn(policy, ctx) -> reason or None). A list, so the selftest can prove each one is load-bearing.
def _hold_combat(p, ctx):
    return "in combat" if ctx.get("in_combat") else None


def _hold_pilot(p, ctx):
    return "pilot is talking" if ctx.get("pilot_speaking") else None


def _hold_recent_event(p, ctx):
    t = ctx.get("last_event_spoken_at")
    if t is not None and p.now() - t < p.event_quiet_s:
        return f"a real event spoke {p.now() - t:.0f}s ago (<{p.event_quiet_s:.0f}s)"
    return None


def _hold_cap(p, ctx):
    if p.last_exchange_at is not None and p.now() - p.last_exchange_at < p.min_gap_s:
        return f"exchange cap: last one {p.now() - p.last_exchange_at:.0f}s ago (<{p.min_gap_s:.0f}s)"
    return None


_START_HOLDS = [("cap", _hold_cap), ("combat", _hold_combat), ("pilot", _hold_pilot), ("event", _hold_recent_event)]
_TURN_HOLDS = [("combat", _hold_combat), ("pilot", _hold_pilot), ("event", _hold_recent_event)]
_EVENT_PRIORITIES = ("EVENT", "PRACTICAL", "URGENT")


class BanterPolicy:
    """When banter may happen. Clock injectable. The cap counts ATTEMPTS (from start), not successes: an exchange
    whose realizer keeps failing must not retry every tick."""

    def __init__(self, now: Callable[[], float] = time.time, min_gap_s: float = 20 * 60,
                 event_quiet_s: float = 60.0, max_turns: int = 3):
        self.now, self.min_gap_s, self.event_quiet_s = now, min_gap_s, event_quiet_s
        self.max_turns = min(3, max_turns)
        self.last_exchange_at: Optional[float] = None
        self.history: list[str] = []          # exchange ids, for plan_exchange's rotation

    @staticmethod
    def context_from(speak_state) -> dict:
        """Build the hold context from speak_gate.SpeakState: combat, pilot mic, newest EVENT+ line."""
        by = getattr(speak_state, "last_spoken_at_by_priority", {}) or {}
        ev = [t for pr, t in by.items() if getattr(pr, "name", str(pr)) in _EVENT_PRIORITIES]
        return {"in_combat": bool(getattr(speak_state, "in_combat", False)),
                "pilot_speaking": bool(getattr(speak_state, "pilot_speaking", False)),
                "last_event_spoken_at": max(ev) if ev else None}

    def _first_hold(self, holds, ctx) -> Optional[str]:
        for _, fn in holds:
            r = fn(self, ctx)
            if r:
                return r
        return None

    def may_start(self, ctx: dict) -> tuple[bool, str]:
        r = self._first_hold(_START_HOLDS, ctx)
        return (False, r) if r else (True, "ok")

    def may_continue(self, ctx: dict) -> Optional[str]:
        """Checked before every turn after the first: a reason to stop, or None."""
        return self._first_hold(_TURN_HOLDS, ctx)

    def started(self, specs: list[Spec]) -> None:
        self.last_exchange_at = self.now()
        if specs:
            self.history.append(specs[0]["exchange_id"])
            del self.history[:-20]


# ---- running ----------------------------------------------------------------------------------------------------
class Spoken(list):
    """What was actually spoken, in order: [{"speaker", "text", "spec_id"}]. `.stop_reason` says why it ended."""
    stop_reason: str = "complete"


def _gate_ok(fails: list) -> bool:
    return not fails


def run_exchange(specs: list[Spec], realize: Callable[[Spec], Optional[str]],
                 ground: Callable[[Spec, str], list], say: Callable[[str, str], bool],
                 max_turns: int = 3, hold: Optional[Callable[[], Optional[str]]] = None) -> Spoken:
    """Realize and speak turn by turn; stop at the first turn that is not spoken. Nothing after it is realized.

    realize(spec) -> text or None; ground(spec, text) -> [] if safe; say(text, speaker) -> True if accepted;
    hold() -> a reason to stop before the next turn (checked before turns 2+), or None.
    """
    out = Spoken()
    specs = list(specs)                  # our own list: turn n+1 is rewritten below to carry turn n's spoken line
    for n in range(1, min(len(specs), max_turns) + 1):
        spec = specs[n - 1]              # by index, NOT a slice taken up front: that copy never saw the rewrite
        if n > 1 and hold is not None:
            r = hold()
            if r:
                out.stop_reason = f"turn {n} held: {r}"
                return out
        try:
            text = realize(spec)
        except Exception as e:                           # a realizer crash is a silent turn, not a crash
            text, out.stop_reason = None, f"turn {n} realizer error: {e!r}"
        if not text:
            if out.stop_reason == "complete":
                out.stop_reason = f"turn {n}: no line from realizer"
            return out
        fails = ground(spec, text)
        if not _gate_ok(fails):
            out.stop_reason = f"turn {n} refused by grounding gate {fails}"
            return out
        if not say(text, spec["speaker"]):
            out.stop_reason = f"turn {n}: speech refused it"
            return out
        out.append({"speaker": spec["speaker"], "text": text, "spec_id": spec["id"]})
        # The next turn hears THIS line, not just its stance: without it the reply could only restate the shared
        # fact, and the exchange sounded staged. A copy, so the planned specs are not mutated.
        if n < len(specs) and specs[n].get("responds_to") is not None:
            specs[n] = dict(specs[n], responds_to=dict(specs[n]["responds_to"], text=text))
    return out


# ---- selftest ---------------------------------------------------------------------------------------------------
STATES = {
    "injury_followup": {"recent_injuries": [{"body_part": "left leg", "severity": "minor", "tier": 2}]},
    "session_reward": {"recent_rewards": 15000},
    "extended_stay": {"location": "Lorville", "minutes_at_location": 42},
    "busy_session": {"recent_locations": ["A", "B", "C", "D"]},
    "jurisdiction": {"jurisdiction": "Hurston Dynamics", "in_armistice": True},
    "ship_context": {"ship": "Cutlass Black", "system": "Stanton"},
}


def _run_checks() -> list[tuple[str, bool, str]]:
    from grounding_validator import ground as real_ground
    res: list[tuple[str, bool, str]] = []

    def check(name, cond, detail=""):
        res.append((name, bool(cond), detail))

    # 1. every designed pair: subset, trained moves, alternating speakers, responds_to present
    subset_ok, moves_ok, echo_ok, n_ex = True, True, True, 0
    detail = []
    for scen, st in STATES.items():
        for v in range(len(PAIRS[scen])):
            ex = plan_exchange(st, v, [])
            n_ex += bool(ex)
            if len(ex) < 2 or ex[0]["exchange_id"] != f"ban_{scen}_p{v}":
                subset_ok = False
                detail.append(f"{scen} v{v}: got {len(ex)} turns")
                continue
            for i, s in enumerate(ex):
                if not _move_ok(s["speaker"], s["rhetoric"][0]):
                    moves_ok = False
                    detail.append(f"{s['id']}: untrained {s['rhetoric'][0]}")
                if i:
                    t1 = {_claim_key(c) for c in ex[0]["claims"]}
                    prev = {_claim_key(c) for c in ex[i - 1]["claims"]}
                    mine = {_claim_key(c) for c in s["claims"]}
                    if not (mine <= t1 and mine <= prev):
                        subset_ok = False
                        detail.append(f"{s['id']}: adds {sorted(mine - t1)}")
                    rt = s.get("responds_to") or {}
                    if (s["speaker"] == ex[i - 1]["speaker"] or rt.get("stance") == s["interpretation"]["text"]
                            or rt.get("speaker") != ex[i - 1]["speaker"]):
                        echo_ok = False
                        detail.append(f"{s['id']}: echoes or mislabels its parent")
    n_pairs = sum(len(p) for p in PAIRS.values())
    check(f"turn-N claims are a subset of turn 1 and of turn N-1 ({n_ex}/{n_pairs} designed exchanges)",
          subset_ok and n_ex == n_pairs, "; ".join(detail))
    check("only trained move labels in every turn", moves_ok, "; ".join(detail))
    check("each reply is the other speaker, with a different stance and a correct responds_to", echo_ok)

    # 2. a leaky carry (a reply that tries to add a fact) must be refused by the guard
    orig_carry = globals()["_carry"]
    globals()["_carry"] = lambda parent, preds: orig_carry(parent, preds) + [
        {"id": "C9", "kind": "OBSERVED", "predicate": "ship.name", "value": "Idris"}]
    try:
        leak = plan_exchange(STATES["session_reward"], 0, [])
    finally:
        globals()["_carry"] = orig_carry
    check("a reply that introduces a new fact is not planned", leak == [], f"got {len(leak)} turns")

    # 3. a responding turn only restates a number when designed to (echo is designed out)
    corr = plan_exchange(STATES["session_reward"], 1, [])
    digr = plan_exchange(STATES["session_reward"], 0, [])
    check("a correction must restate the number; a digression need not",
          corr and corr[1]["required_values"] == ["15000"] and digr and digr[1]["required_values"] == [])

    # 4. variant rotation produces different pairs, and history steers away from the last one
    ids = {plan_exchange(STATES["injury_followup"], v, [])[0]["exchange_id"] for v in range(3)}
    check("variant rotation gives different pairs", len(ids) == 3, str(ids))
    again = plan_exchange(STATES["session_reward"], 0, ["ban_session_reward_p0"])
    check("history skips the pair just used", again and again[0]["exchange_id"] != "ban_session_reward_p0")
    check("max_turns=2 truncates a 3-turn exchange", len(plan_exchange(STATES["injury_followup"], 0, [], 2)) == 2)
    check("nothing to talk about -> no banter", plan_exchange({}, 0, []) == [])

    # 5. policy: cap, combat, pilot talking, recent event
    clock = [100_000.0]
    pol = BanterPolicy(now=lambda: clock[0])
    calm = {"in_combat": False, "pilot_speaking": False, "last_event_spoken_at": None}
    check("calm and never bantered: may start", pol.may_start(calm)[0])
    pol.started(plan_exchange(STATES["session_reward"], 0, []))
    clock[0] += 19 * 60
    check("cap: 19 min after an exchange, held", not pol.may_start(calm)[0], pol.may_start(calm)[1])
    clock[0] += 61
    check("cap: 20 min after, allowed", pol.may_start(calm)[0])
    check("combat holds", not pol.may_start({**calm, "in_combat": True})[0])
    check("pilot talking holds", not pol.may_start({**calm, "pilot_speaking": True})[0])
    check("an event 30s ago holds", not pol.may_start({**calm, "last_event_spoken_at": clock[0] - 30})[0])
    check("an event 90s ago does not", pol.may_start({**calm, "last_event_spoken_at": clock[0] - 90})[0])
    check("mid-exchange: combat stops the next turn", pol.may_continue({**calm, "in_combat": True}) is not None)

    from speak_gate import SpeakState, Priority
    ss = SpeakState(pilot_speaking=True)
    ss.last_spoken_at_by_priority = {Priority.BANTER: 9.0, Priority.EVENT: 7.0, Priority.AMBIENT: 8.0}
    ctx = BanterPolicy.context_from(ss)
    check("context_from(real SpeakState) takes the newest EVENT+ line, ignores AMBIENT/BANTER",
          ctx["last_event_spoken_at"] == 7.0 and ctx["pilot_speaking"], str(ctx))

    # 6. run_exchange: honest realizer speaks every turn; a turn failing the REAL gate aborts the rest
    def honest(spec):
        vals = [str(c["value"]) for c in spec["claims"] if not isinstance(c["value"], bool)]
        words = ("Well, " + " and ".join(vals) + " is what the record shows, pilot.").split()
        lo, hi = spec["length_words"]
        return " ".join((words + ["indeed"] * lo)[:max(lo, min(len(words), hi))])

    ex = plan_exchange(STATES["injury_followup"], 0, [])
    said = []
    out = run_exchange(ex, honest, real_ground, lambda t, s: said.append((s, t)) or True)
    check("honest 3-turn exchange through the REAL gate: all turns spoken", len(out) == 3 and len(said) == 3,
          out.stop_reason)

    calls, said = [], []

    def lies_on_turn2(spec):
        calls.append(spec["turn"])
        return "Seven hundred and 99999 things went wrong today, pilot." if spec["turn"] == 2 else honest(spec)
    out = run_exchange(ex, lies_on_turn2, real_ground, lambda t, s: said.append((s, t)) or True)
    check("turn 2 refused by the gate: turn 1 stays spoken, turn 3 is never realized",
          len(out) == 1 and len(said) == 1 and calls == [1, 2], f"{out.stop_reason} calls={calls}")

    said = []
    out = run_exchange(ex, lambda s: None if s["turn"] == 1 else honest(s), real_ground,
                       lambda t, s: said.append(t) or True)
    check("turn 1 silent: nothing spoken, no orphan reply", not out and not said, out.stop_reason)

    out = run_exchange(ex, honest, real_ground, lambda t, s: s != "elah")
    check("speech refuses turn 2: stop, no turn 3", len(out) == 1, out.stop_reason)

    out = run_exchange(ex, honest, real_ground, lambda t, s: True, hold=lambda: "pilot is talking")
    check("a hold before turn 2 leaves a clean half-exchange", len(out) == 1 and "held" in out.stop_reason)

    out = run_exchange(ex, honest, real_ground, lambda t, s: True, max_turns=2)
    check("run_exchange respects the turn cap", len(out) == 2)
    return res


def _print(res) -> int:
    for name, ok, detail in res:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else ""))
    bad = sum(not ok for _, ok, _ in res)
    print(f"banter selftest: {len(res) - bad}/{len(res)} passed")
    return bad


# Mutants: each disables ONE guard. The selftest must FAIL on every one, or that guard is untested.
def _mutants():
    g = globals()

    def swap(name, value):
        old = g[name]
        g[name] = value
        return lambda: g.__setitem__(name, old)

    def drop_hold(hold_list_name, hold):
        lst = g[hold_list_name]
        saved = list(lst)
        lst[:] = [h for h in lst if h[0] != hold]
        return lambda: lst.__setitem__(slice(None), saved)

    def bad_move():
        pair = PAIRS["busy_session"][1]
        old = pair[0]["move"]
        pair[0]["move"] = "HORSE_ANALOGY"        # an Elah turn with a Montaigne-only move
        return lambda: pair[0].__setitem__("move", old)

    return [
        ("subset guard disabled (_subset_violation -> [])", lambda: swap("_subset_violation", lambda p, c: [])),
        ("combat hold removed", lambda: (drop_hold("_START_HOLDS", "combat"), drop_hold("_TURN_HOLDS", "combat"))),
        ("pilot-talking hold removed", lambda: drop_hold("_START_HOLDS", "pilot")),
        ("recent-event hold removed", lambda: drop_hold("_START_HOLDS", "event")),
        ("exchange cap removed", lambda: drop_hold("_START_HOLDS", "cap")),
        ("abort-on-failed-turn removed (_gate_ok -> True)", lambda: swap("_gate_ok", lambda f: True)),
        ("untrained move slipped into a pair (after the import check)", bad_move),
    ]


def _mutation_run() -> int:
    print("\n-- mutation run: every mutant must be CAUGHT (selftest fails) --")
    base = _run_checks()
    if any(not ok for _, ok, _ in base):
        print("  BASELINE FAILS: a mutation score against a failing control is meaningless. Not scoring.")
        return 1
    print("  control: unmutated baseline passes")
    missed = 0
    for name, apply in _mutants():
        undo = apply()
        try:
            res = _run_checks()
        finally:
            for u in (undo if isinstance(undo, tuple) else (undo,)):
                u()
        failed = [n for n, ok, _ in res if not ok]
        caught = bool(failed)
        missed += not caught
        print(f"  {'CAUGHT' if caught else 'MISSED'}  {name}" + (f"  <- {failed[0]}" if caught else ""))
    restored = all(ok for _, ok, _ in _run_checks())
    print(f"  baseline passes again after restoring every mutant: {restored}")
    print(f"mutation run: {len(_mutants()) - missed}/{len(_mutants())} caught")
    return 1 if missed or not restored else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        bad = _print(_run_checks())
        sys.exit(1 if (_mutation_run() or bad) else 0)
    if "--table" in sys.argv:
        for scen, pool in PAIRS.items():
            for i, pair in enumerate(pool):
                print(f"{scen} p{i}: " + "  ->  ".join(
                    f"{t['speaker']}/{t['move']}" + (f"[{t['relation']}{'+num' if t['restate'] else ''}]"
                                                     if 'relation' in t else "") + f" '{t['stance']}'"
                    for t in pair))
        sys.exit(0)
    print(__doc__)
