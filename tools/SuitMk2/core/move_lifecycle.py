"""move_lifecycle.py - the anti-canned lifecycle for LEARNED rhetorical moves (ARCHITECTURE.md "Anti-canned"; 2026-09-23).

Dreaming (dream_models.py, kind "moves") writes a candidate move as an INTERPRETATION memory, predicate
"move.proposal". This module walks each candidate through memory_store.set_move_state:

  PROPOSED  -> VALIDATED   structural checks pass (dream_models.check_move_definition + grounded + not a duplicate)
  PROPOSED  -> REJECTED    structural checks fail. Including: base_move is not a TRAINED move of the SAME owner.
  VALIDATED -> TRIAL       a trial slot is free for that owner (at most MAX_CONCURRENT_TRIALS at once)
  TRIAL     -> PROMOTED    >= MIN_TRIALS uses AND net evidence >= PROMOTE_SCORE
  TRIAL     -> REJECTED    actively disliked: >= NEG_LIMIT mutes/skips within FAST_WINDOW_S of its line, or >= STOP_LIMIT "stop"
  TRIAL     -> RETIRED     inconclusive: MAX_TRIALS uses without earning promotion (a timeout, not a verdict)
  PROMOTED  -> RETIRED     disliked since promotion (same negative rule), OR stale: its share of that owner's
                           recent lines exceeds STALE_SHARE (a favourite said too often IS the canned sound)

The realizer never sees a learned move's name. Its adapters were trained on a closed set (ambient_spec._ELAH_MOVES /
_MONT_MOVES), so `realizer_spec(move_id)` hands it the TRAINED base_move plus the learned move's stance as the
interpretation text. Only TRIAL (capped per session) and PROMOTED moves are usable; nothing else is ever handed out.

Evidence (`record_evidence(move, kind)`), weights:
  reaction_positive +1   repeat_request +1   interrupt -0.5   stop -1 (and counts toward STOP_LIMIT)
  mute / skip       -1 ONLY within FAST_WINDOW_S of that move's last line; later it is not attributable -> 0
  silence            0   ALWAYS. Silence is not disapproval (ARCHITECTURE.md), and it is not approval either:
                         it never moves the score, never counts toward a negative limit, never triggers anything.
Evidence on a move that has not been used yet, or on one that is not in TRIAL/PROMOTED, is logged with weight 0.

State: lifecycle state lives in moves.json (memory_store.set_move_state, exported with the pilot). Uses, evidence and
this module's transitions are an append-only log, <pilot>/move_events.jsonl, from which every score is RE-DERIVED;
nothing is cached, so the decision is a pure function of (moves.json, move_events.jsonl, the proposal memories).
NOTE: move_events.jsonl is not in memory_store.STORE_FILES, so export_pilot does not carry it yet (see report).

Deterministic; the clock is injected (`now`). Selftest: python move_lifecycle.py --selftest
Mutation check:  python move_lifecycle.py --mutation-check
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import memory_store as ms  # noqa: E402
from dream_models import TRAINED_MOVES, check_move_definition  # noqa: E402

PROPOSAL_PREDICATE = "move.proposal"

EVIDENCE_WEIGHTS = {"reaction_positive": 1.0, "repeat_request": 1.0, "interrupt": -0.5, "stop": -1.0,
                    "mute": -1.0, "skip": -1.0, "silence": 0.0}
FAST_KINDS = {"mute", "skip"}

DEFAULTS = {
    "TRIAL_USES_PER_SESSION": 2,     # N: a trial move is heard at most twice a session
    "MAX_CONCURRENT_TRIALS": 2,      # per owner
    "MIN_TRIALS": 5,                 # K: uses before promotion can even be considered
    "PROMOTE_SCORE": 2.0,            # net weighted evidence (a SUM, so silent uses do not dilute it)
    "MAX_TRIALS": 20,                # uses without earning promotion -> RETIRED (inconclusive)
    "FAST_WINDOW_S": 15.0,           # matches feedback.py window_s (2026-09-23): a "shut up" 10 s after a line is about it
    "NEG_LIMIT": 2,                  # fast mutes/skips before REJECTED (trial) / RETIRED (promoted)
    "STOP_LIMIT": 2,                 # "stop" requests, same outcome
    "STALE_WINDOW": 40,              # look at that owner's last 40 lines...
    "STALE_MIN_LINES": 20,           # ...once there are at least 20...
    "STALE_SHARE": 0.35,             # ...and retire a promoted move that is more than 35% of them
}


def trained_id(owner: str, move: str) -> str:
    return f"{owner}.{move}"


class MoveLifecycle:
    def __init__(self, store: "ms.Store", now: Callable[[], float] = time.time, **config):
        unknown = set(config) - set(DEFAULTS)
        if unknown:
            raise ValueError(f"unknown config {sorted(unknown)}")
        self.store, self.now = store, now
        self.cfg = {**DEFAULTS, **config}
        self.log_path = store.dir / "move_events.jsonl"

    # ---- storage ------------------------------------------------------------------------------------------------
    def _iso(self) -> str:
        return datetime.fromtimestamp(self.now(), timezone.utc).isoformat()

    def _log(self, row: dict) -> dict:
        row = {"t": self.now(), **row}
        with open(self.log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
        return row

    def events(self, move: Optional[str] = None) -> list[dict]:
        if not self.log_path.exists():
            return []
        out = []
        for line in self.log_path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                     # a torn last line from a crash is expected
            if move is None or r.get("move") == move:
                out.append(r)
        return out

    def moves(self) -> dict:
        p = self.store.paths["moves"]
        return json.loads(p.read_text(encoding="utf-8") or "{}") if p.exists() else {}

    def state(self, move_id: str) -> Optional[str]:
        e = self.moves().get(move_id)
        return e["state"] if e else None

    def _set(self, move_id: str, state: str, note: str, usage: bool = False) -> None:
        prev = self.state(move_id)
        ms.set_move_state(self.store, move_id, state, note=note, increment_usage=usage, created=self._iso())
        if prev != state:
            self._log({"type": "transition", "move": move_id, "from": prev, "to": state, "note": note})

    # ---- proposals ----------------------------------------------------------------------------------------------
    def _proposals(self) -> list[dict]:
        return ms.query(self.store, kind="INTERPRETATION", predicate=PROPOSAL_PREDICATE)

    def definition(self, move_id: str) -> Optional[dict]:
        """The proposal memory that registered this move (its id is on the PROPOSED history entry)."""
        e = self.moves().get(move_id)
        if not e:
            return None
        note = (e.get("history") or [{}])[0].get("note") or ""
        src = note.split("src=", 1)[1].split()[0] if "src=" in note else None
        return next((m for m in self._proposals() if m["id"] == src), None)

    def sync_proposals(self) -> list[str]:
        """Register every not-yet-seen proposal memory as PROPOSED. First proposal of an id wins; a later one with
        the same id (including one for a RETIRED/REJECTED move) is ignored: a rejected move cannot be re-proposed
        back into circulation by dreaming it again."""
        known = self.moves()
        registered_srcs = {((e.get("history") or [{}])[0].get("note") or "") for e in known.values()}
        new = []
        for m in self._proposals():
            if any(f"src={m['id']}" == n.split()[0] for n in registered_srcs if n):
                continue
            v = m.get("value") if isinstance(m.get("value"), dict) else {}
            owner, name = v.get("owner"), v.get("name")
            mid = f"{owner}.{name}" if isinstance(owner, str) and isinstance(name, str) and owner and name \
                else f"invalid.{m['id']}"
            if mid in known:
                continue
            self._set(mid, "PROPOSED", f"src={m['id']}")
            known = self.moves()
            new.append(mid)
        return new

    def check_structure(self, move_id: str) -> Optional[str]:
        """Why this PROPOSED move may NOT become VALIDATED, or None if it may."""
        mem = self.definition(move_id)
        if mem is None:
            return "no proposal memory"
        d = mem.get("value")
        err = check_move_definition(d)
        if err:
            return err
        if move_id != f"{d['owner']}.{d['name']}":
            return "move id does not match its definition"
        if not mem.get("grounds"):
            return "proposal has no grounds"
        if mem.get("owner") not in ("shared", d["owner"]):
            return f"proposal memory owner {mem.get('owner')!r} cannot propose a {d['owner']} move"
        if d["base_move"] not in TRAINED_MOVES[d["owner"]]:           # belt and braces: the realizer depends on it
            return "base_move not trained for owner"
        return None

    def validate_pending(self) -> dict:
        out = {}
        for mid, e in self.moves().items():
            if e["state"] != "PROPOSED":
                continue
            err = self.check_structure(mid)
            if err:
                self._set(mid, "REJECTED", f"structure: {err}")
            else:
                self._set(mid, "VALIDATED", "structure ok")
            out[mid] = err or "ok"
        return out

    def start_trials(self) -> list[str]:
        started = []
        mv = self.moves()
        for owner in TRAINED_MOVES:
            active = sum(1 for k, e in mv.items() if e["state"] == "TRIAL" and k.startswith(owner + "."))
            for mid in sorted(k for k, e in mv.items() if e["state"] == "VALIDATED" and k.startswith(owner + ".")):
                if active >= self.cfg["MAX_CONCURRENT_TRIALS"]:
                    break
                self._set(mid, "TRIAL", "trial slot free")
                active += 1
                started.append(mid)
        return started

    # ---- use ------------------------------------------------------------------------------------------------------
    def _is_trained(self, move_id: str) -> bool:
        owner, _, name = move_id.partition(".")
        return name in TRAINED_MOVES.get(owner, ())

    def _session_uses(self, move_id: str, session: str) -> int:
        return sum(1 for r in self.events(move_id) if r["type"] == "use" and r.get("session") == session)

    def can_use(self, move_id: str, session: str) -> bool:
        if self._is_trained(move_id):
            return True
        s = self.state(move_id)
        if s == "PROMOTED":
            return True
        if s == "TRIAL":
            return self._session_uses(move_id, session) < self.cfg["TRIAL_USES_PER_SESSION"]
        return False                                   # PROPOSED/VALIDATED/RETIRED/REJECTED/unknown: never

    def realizer_spec(self, move_id: str, session: str) -> Optional[dict]:
        """What the planner hands the realizer for this move: the TRAINED label and the stance. None if not usable."""
        if not self.can_use(move_id, session):
            return None
        if self._is_trained(move_id):
            owner, _, name = move_id.partition(".")
            return {"speaker": owner, "rhetoric": [name], "stance": None, "learned_move": None}
        d = self.definition(move_id)["value"]
        return {"speaker": d["owner"], "rhetoric": [d["base_move"]], "stance": d["example_stance"],
                "learned_move": move_id}

    def record_use(self, move_id: str, session: str) -> bool:
        """The line built with this move was SPOKEN (call at speech end). Trained moves are logged for the staleness
        denominator only. Returns False (and records nothing) if the move was not usable."""
        if not self.can_use(move_id, session):
            return False
        self._log({"type": "use", "move": move_id, "owner": move_id.partition(".")[0], "session": session})
        if not self._is_trained(move_id):
            ms.set_move_state(self.store, move_id, self.state(move_id), increment_usage=True, created=self._iso())
            self.evaluate(move_id)
        self._check_stale(move_id.partition(".")[0])
        return True

    # ---- evidence -------------------------------------------------------------------------------------------------
    def record_evidence(self, move_id: str, kind: str) -> dict:
        if kind not in EVIDENCE_WEIGHTS:
            raise ValueError(f"unknown evidence kind {kind!r}; one of {sorted(EVIDENCE_WEIGHTS)}")
        uses = [r for r in self.events(move_id) if r["type"] == "use"]
        live = self._is_trained(move_id) or self.state(move_id) in ("TRIAL", "PROMOTED")
        weight, why = EVIDENCE_WEIGHTS[kind], "attributed"
        if not uses or not live:
            weight, why = 0.0, "no line / not live"
        elif kind in FAST_KINDS and self.now() - uses[-1]["t"] > self.cfg["FAST_WINDOW_S"]:
            weight, why = 0.0, "outside fast window: not attributable to the line"
        row = self._log({"type": "evidence", "move": move_id, "kind": kind, "weight": weight, "why": why})
        if not self._is_trained(move_id):
            self.evaluate(move_id)
        return row

    def _since_state(self, move_id: str) -> list[dict]:
        """This move's log rows since it entered its CURRENT state (so a promoted move is judged afresh)."""
        rows = self.events(move_id)
        last = max((i for i, r in enumerate(rows) if r["type"] == "transition"), default=-1)
        return rows[last + 1:]

    def score(self, move_id: str) -> dict:
        rows = self._since_state(move_id)
        ev = [r for r in rows if r["type"] == "evidence" and r["weight"] != 0.0]   # silence is inert BY CONSTRUCTION
        return {"uses": sum(1 for r in rows if r["type"] == "use"),
                "net": round(sum(r["weight"] for r in ev), 3),
                "fast_negatives": sum(1 for r in ev if r["kind"] in FAST_KINDS),
                "stops": sum(1 for r in ev if r["kind"] == "stop")}

    def evaluate(self, move_id: str) -> Optional[str]:
        """Apply the TRIAL/PROMOTED rules. Returns the new state if it changed."""
        s, c = self.state(move_id), self.cfg
        if s not in ("TRIAL", "PROMOTED"):
            return None
        sc = self.score(move_id)
        disliked = sc["fast_negatives"] >= c["NEG_LIMIT"] or sc["stops"] >= c["STOP_LIMIT"]
        if s == "TRIAL":
            if disliked:
                self._set(move_id, "REJECTED", f"disliked in trial: {sc}")
            elif sc["uses"] >= c["MIN_TRIALS"] and sc["net"] >= c["PROMOTE_SCORE"]:
                self._set(move_id, "PROMOTED", f"earned it: {sc}")
            elif sc["uses"] >= c["MAX_TRIALS"]:
                self._set(move_id, "RETIRED", f"inconclusive after {sc['uses']} trials: {sc}")
        elif disliked:
            self._set(move_id, "RETIRED", f"disliked since promotion: {sc}")
        new = self.state(move_id)
        return new if new != s else None

    def _check_stale(self, owner: str) -> list[str]:
        c = self.cfg
        lines = [r for r in self.events() if r["type"] == "use" and r.get("owner") == owner][-c["STALE_WINDOW"]:]
        if len(lines) < c["STALE_MIN_LINES"]:
            return []
        retired = []
        for mid, e in self.moves().items():
            if e["state"] == "PROMOTED" and mid.startswith(owner + "."):
                share = sum(1 for r in lines if r["move"] == mid) / len(lines)
                if share > c["STALE_SHARE"]:
                    self._set(mid, "RETIRED", f"stale: {share:.0%} of the last {len(lines)} {owner} lines")
                    retired.append(mid)
        return retired

    # ---- the deterministic dream job ------------------------------------------------------------------------------
    def tick(self) -> dict:
        """Cheap, no model: what DreamQueue's deterministic phase (or any quiet window) runs."""
        return {"registered": self.sync_proposals(), "validated": self.validate_pending(),
                "trials": self.start_trials(),
                "evaluated": {m: s for m in list(self.moves()) if (s := self.evaluate(m))},
                "stale": [x for o in TRAINED_MOVES for x in self._check_stale(o)]}


# ---- selftest ------------------------------------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    root = Path(tempfile.mkdtemp(prefix="move_lifecycle_st_"))
    store = ms.open_store(root, "p1")
    g1 = ms.add_memory(store, "shared", "HISTORY", "history.location_first_visit", "Lorville")["id"]
    g2 = ms.add_memory(store, "shared", "HISTORY", "history.ship_first_flown", "Cutlass Black")["id"]
    clock = [1_000_000.0]
    lc = MoveLifecycle(store, now=lambda: clock[0])

    def propose(name, owner="montaigne", base="ESSAY_DIGRESSION", grounds=(g1,), **kw):
        v = {"name": name, "owner": owner, "base_move": base,
             "description": kw.get("description", "Reads the pilot's route as a life story."),
             "example_stance": kw.get("example_stance", "the places visited say more than the pilot does")}
        return ms.add_memory(store, "shared", "INTERPRETATION", PROPOSAL_PREDICATE, v, confidence=0.6,
                             grounds=list(grounds), text=v["description"])

    def use(mid, session="S1", dt=1.0):
        clock[0] += dt
        return lc.record_use(mid, session)

    def ev(mid, kind, dt=0.5):
        clock[0] += dt
        return lc.record_evidence(mid, kind)

    # --- structure gate ---
    propose("ITINERARY_AS_BIOGRAPHY")
    propose("WRONG_OWNER_BASE", base="DEADPAN")                     # an Elah move under a Montaigne owner
    propose("NO_SUCH_BASE", base="SONNET")
    propose("NO_BASE", base=None)
    propose("HORSE_ANALOGY")                                         # a trained name re-proposed
    propose("DRY_INVENTORY", owner="elah", base="PRACTICAL")
    propose("NUMBERED", description="Mentions the 3 last stops.")
    propose("UNGROUNDED", grounds=())
    reg = lc.sync_proposals()
    case("every proposal is registered as PROPOSED", len(reg) == 8 and all(lc.state(m) == "PROPOSED" for m in reg))
    case("sync is idempotent", lc.sync_proposals() == [])
    case("a PROPOSED move is not usable", not lc.can_use("montaigne.ITINERARY_AS_BIOGRAPHY", "S1")
         and lc.realizer_spec("montaigne.ITINERARY_AS_BIOGRAPHY", "S1") is None)
    v = lc.validate_pending()
    case("valid proposals -> VALIDATED", lc.state("montaigne.ITINERARY_AS_BIOGRAPHY") == "VALIDATED"
         and lc.state("elah.DRY_INVENTORY") == "VALIDATED")
    case("base_move of the other owner cannot reach VALIDATED", lc.state("montaigne.WRONG_OWNER_BASE") == "REJECTED")
    case("untrained base_move cannot reach VALIDATED", lc.state("montaigne.NO_SUCH_BASE") == "REJECTED")
    case("missing base_move cannot reach VALIDATED", lc.state("montaigne.NO_BASE") == "REJECTED")
    case("a trained name re-proposed is REJECTED", lc.state("montaigne.HORSE_ANALOGY") == "REJECTED")
    case("a number in the description is REJECTED", lc.state("montaigne.NUMBERED") == "REJECTED")
    case("an ungrounded proposal is REJECTED", lc.state("montaigne.UNGROUNDED") == "REJECTED")
    case("rejection reason is recorded", "not a trained montaigne move" in v["montaigne.WRONG_OWNER_BASE"])
    propose("WRONG_OWNER_BASE", base="SKEPTICAL_REVERSAL")            # re-dreamed after rejection, now "valid"
    lc.sync_proposals()
    case("a REJECTED move cannot be re-proposed into circulation", lc.state("montaigne.WRONG_OWNER_BASE") == "REJECTED")
    bad = ms.add_memory(store, "shared", "INTERPRETATION", PROPOSAL_PREDICATE, "not a dict", confidence=0.5)
    lc.sync_proposals()
    lc.validate_pending()
    case("a malformed proposal gets an invalid.* record and is REJECTED", lc.state(f"invalid.{bad['id']}") == "REJECTED")

    # --- trial ---
    propose("SECOND_M")
    propose("THIRD_M")
    lc.tick()
    trials = [m for m, e in lc.moves().items() if e["state"] == "TRIAL"]
    case("trial slots are capped per owner (2 montaigne + 1 elah)", sorted(trials) == sorted(
        ["montaigne.ITINERARY_AS_BIOGRAPHY", "montaigne.SECOND_M", "elah.DRY_INVENTORY"])
         and lc.state("montaigne.THIRD_M") == "VALIDATED")
    A = "montaigne.ITINERARY_AS_BIOGRAPHY"
    spec = lc.realizer_spec(A, "S1")
    case("realizer gets the TRAINED base move, never the learned name",
         spec["rhetoric"] == ["ESSAY_DIGRESSION"] and spec["learned_move"] == A and "ITINERARY" not in spec["rhetoric"][0])
    case("trial use 1 and 2 allowed", use(A) and use(A))
    case("trial use 3 in the same session refused", not lc.can_use(A, "S1") and not use(A))
    case("next session the cap resets", lc.can_use(A, "S2"))

    # --- PROMOTED path, with silence proven inert ---
    use(A, "S2")
    ev(A, "reaction_positive")
    ev(A, "reaction_positive")
    ev(A, "repeat_request")
    case("not promoted before MIN_TRIALS uses, even with net 3.0", lc.state(A) == "TRIAL"
         and lc.score(A) == {"uses": 3, "net": 3.0, "fast_negatives": 0, "stops": 0})
    use(A, "S2")
    case("TRIAL -> PROMOTED still waits at 4 uses", lc.state(A) == "TRIAL")
    for _ in range(5):
        ev(A, "silence")
    use(A, "S3")
    case("TRIAL -> PROMOTED on the use that reaches K=5 with net >= threshold", lc.state(A) == "PROMOTED")
    use(A, "S3")
    case("PROMOTED is usable without a per-session cap", all(lc.can_use(A, "S3") for _ in range(3)))

    # silence invariance: two identical twins, one drowned in silence
    B1, B2 = "elah.DRY_INVENTORY", None
    propose("DRY_TWIN", owner="elah", base="DEADPAN")
    lc.tick()
    B2 = "elah.DRY_TWIN"
    case("twin enters trial", lc.state(B2) == "TRIAL")
    for i, s in enumerate(("T1", "T2", "T3", "T4")):
        for mid in (B1, B2):
            use(mid, s)
            if i < 1:
                ev(mid, "reaction_positive")
            if mid == B2:
                for _ in range(10):
                    ev(mid, "silence", dt=0.1)
    case("silence never counts against a move (twin scores match)",
         lc.score(B1)["net"] == lc.score(B2)["net"] and lc.state(B1) == lc.state(B2) == "TRIAL")
    case("silence rows are logged with weight 0", all(r["weight"] == 0.0 for r in lc.events(B2) if r.get("kind") == "silence"))
    case("silence right after a line is not a mute", lc.score(B2)["fast_negatives"] == 0)

    # --- REJECTED paths ---
    lc.tick()
    C = "montaigne.THIRD_M"           # still VALIDATED: montaigne has one TRIAL slot... A is PROMOTED now
    case("a freed slot lets the queued move into TRIAL", lc.state(C) == "TRIAL")
    use(C, "R1")
    ev(C, "mute", dt=2.0)
    use(C, "R1")
    ev(C, "skip", dt=4.9)
    case("TRIAL -> REJECTED on 2 mutes/skips within 5 s", lc.state(C) == "REJECTED")
    case("a REJECTED move is never usable again", not lc.can_use(C, "R9") and not use(C, "R9"))

    D = "montaigne.SECOND_M"
    use(D, "R2")
    ev(D, "mute", dt=16.0)
    use(D, "R2")
    ev(D, "skip", dt=30.0)
    case("a mute/skip later than FAST_WINDOW_S (15 s) is not attributable (weight 0, still TRIAL)",
         lc.state(D) == "TRIAL" and lc.score(D)["fast_negatives"] == 0)
    ev(D, "stop")
    ev(D, "stop")
    case("TRIAL -> REJECTED on repeated 'stop'", lc.state(D) == "REJECTED")

    # --- inconclusive timeout ---
    lc2 = MoveLifecycle(store, now=lambda: clock[0], MAX_TRIALS=6)
    propose("QUIET_ONE")
    lc2.tick()
    E = "montaigne.QUIET_ONE"
    case("quiet move in trial", lc2.state(E) == "TRIAL")
    for i in range(6):
        clock[0] += 1
        lc2.record_use(E, f"Q{i}")
        clock[0] += 1
        lc2.record_evidence(E, "silence")
    case("TRIAL -> RETIRED (inconclusive) after MAX_TRIALS all-silent uses, NOT REJECTED", lc2.state(E) == "RETIRED")

    # --- PROMOTED -> RETIRED: disliked, and stale ---
    use(A, "P1")
    ev(A, "mute", dt=1.0)
    use(A, "P1")
    ev(A, "skip", dt=1.0)
    case("PROMOTED -> RETIRED when disliked after promotion", lc.state(A) == "RETIRED")

    store2 = ms.open_store(root, "p2")                         # fresh pilot: the share window must be clean
    h1 = ms.add_memory(store2, "shared", "HISTORY", "history.location_first_visit", "Area18")["id"]
    ms.add_memory(store2, "shared", "INTERPRETATION", PROPOSAL_PREDICATE,
                  {"name": "FAVOURITE", "owner": "montaigne", "base_move": "PILOT_CHARACTER",
                   "description": "Admires one trait of the pilot at length.",
                   "example_stance": "the pilot's stubbornness is a kind of virtue"},
                  confidence=0.6, grounds=[h1])
    lc3 = MoveLifecycle(store2, now=lambda: clock[0], MIN_TRIALS=1, PROMOTE_SCORE=1.0)
    lc3.tick()
    F = "montaigne.FAVOURITE"
    lc3.record_use(F, "F1")
    lc3.record_evidence(F, "reaction_positive")
    case("second move promoted", lc3.state(F) == "PROMOTED")
    trained = [trained_id("montaigne", m) for m in sorted(TRAINED_MOVES["montaigne"])]
    for i in range(14):                                          # 14 trained + 6 F = 20 lines, F 30%: fine
        clock[0] += 1
        lc3.record_use(trained[i % len(trained)], "F1")
    for _ in range(5):
        clock[0] += 1
        lc3.record_use(F, "F1")
    case("share below STALE_SHARE keeps it", lc3.state(F) == "PROMOTED")
    for _ in range(3):
        clock[0] += 1
        lc3.record_use(F, "F1")
    case("PROMOTED -> RETIRED when it becomes > 35% of recent lines (canned)", lc3.state(F) == "RETIRED"
         and "stale" in lc3.moves()[F]["history"][-1]["note"])

    # --- bookkeeping ---
    case("illegal transitions are refused by memory_store (RETIRED is terminal)",
         _raises(lambda: ms.set_move_state(store2, F, "PROMOTED")))
    case("evidence on an unknown kind is refused", _raises(lambda: lc.record_evidence(A, "shrug")))
    case("usage_count in moves.json tracks spoken uses", lc.moves()[A]["usage_count"] == 8)
    case("trained moves are always usable and never get a lifecycle entry",
         lc.can_use("elah.DEADPAN", "X") and "elah.DEADPAN" not in lc.moves())
    with open(lc.log_path, "a", encoding="utf-8") as f:
        f.write('{"t": 1, "type": "us')
    case("a torn log line does not break scoring", isinstance(lc.score(B1), dict))
    reached = {h["state"] for e in lc.moves().values() for h in e["history"]}
    case("every state in MOVE_STATES was reached", reached == ms.MOVE_STATES)

    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad_n = sum(not ok for _, ok in results)
    print(f"move_lifecycle selftest: {len(results) - bad_n}/{len(results)} passed")
    return 1 if bad_n else 0


def _raises(fn) -> bool:
    try:
        fn()
    except ValueError:
        return True
    return False


MUTATIONS = [
    ("silence given a negative weight",
     '                    "mute": -1.0, "skip": -1.0, "silence": 0.0}\n',
     '                    "mute": -1.0, "skip": -1.0, "silence": -0.2}  # MUTANT\n'),
    ("VALIDATED gate skips the structure check",
     "            err = self.check_structure(mid)\n",
     "            err = None  # MUTANT\n"),
]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--mutation-check" in sys.argv:
        from dream_models import _mutation_check
        sys.exit(_mutation_check(Path(__file__).resolve(), MUTATIONS, HERE))
    print(__doc__)
