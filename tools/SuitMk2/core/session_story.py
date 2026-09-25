"""session_story.py - SESSION CONTINUITY: the welcome at launch and the recap at shutdown (2026-09-23).

Two characters remember the last flight: Elah (suit AI, dry, brief, factual) and Montaigne (the semi-broken ship
AI who believes he is Michel de Montaigne and only ever knows things secondhand, from the log). This module decides
WHAT they may recall. It writes no sentences: it emits one semantic spec (same shape as ambient_spec / event_spec),
the local realizer words it, and grounding_validator.ground() gates it.

    SHUTDOWN   recap_record(events, store)      one HISTORY memory, predicate "session.recap", value = a small
                                                dict (ended_at, minutes, injuries, deaths, rewards_auec,
                                                locations, last_location). ONE add_memory call, no model: instant.
    LAUNCH     welcome_spec(store, state, now)  at most ONE spec, every claim kind HISTORY, every claim value
                                                traceable to a memory (spec["provenance"] says which). None when
                                                there is no previous session, when the last one ended < 10 min ago
                                                (a crash-restart is not a homecoming), or when it already ran
                                                this session.
               first_meeting_spec(store, state, now)  only when nothing is remembered at all: live state only.
               mark_welcomed(store, spec, now, session_file)  record the facts used, so the next launch picks others.

WHY ONE "session.recap" MEMORY AND NOT relationship.json OR PER-FIELD MEMORIES
  * relationship.json counters ACCUMULATE (update_relationship adds) and DreamQueue's counters job already adds
    each session into them. Writing the recap there would double-count, and a top-level field is last-write-wins
    with no id, so a claim built from it could not cite provenance (ARCHITECTURE principle 4).
  * One memory per field ("session.last_recap.deaths", ...) would be seven full-file atomic rewrites + fsyncs at
    shutdown (add_memory rewrites memories.jsonl each time), and a kill between them leaves half a recap. One
    memory with a dict value is one write, all-or-nothing, and has one id every claim can cite.
  * It is append-only history: the recap of every session survives, keyed by its session file name, so the
    welcome can also be rebuilt for a CRASHED session (no shutdown ran) by backfilling from the session file.

NEVER INVENT: a field absent from the recap is absent from the spec. Zero counts are true but dull, so they are
not claimed. welcome_spec traces its own output (trace()) before returning it and returns None on any failure.

Selftest:        python session_story.py --selftest
Mutation check:  python session_story.py --mutation-check   (lets an absent fact become a guessed default and
                                                             asserts the selftest FAILS)
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import memory_store as ms                                   # noqa: E402
from ambient_spec import _ELAH_MOVES, _MONT_MOVES, _claim   # noqa: E402

Spec = dict[str, Any]

RECAP_PRED = "session.recap"
WELCOME_KIND = "welcome"
FIRST_MEETING_KIND = "first_meeting"
MIN_GAP_S = 600                  # last session ended < 10 min ago -> crash-restart, not "welcome back"
MIN_SESSION_MIN = 1              # a session under a minute with no events is not worth recalling
MAX_CLAIMS = 4
_LEN = (8, 36)
_REWARD_TYPES = {"mission_reward", "reward_earned"}
_LOCATION_TYPES = {"location_change", "quantum_arrived", "qt_arrived"}
_BAD_LOCATIONS = {"", "unknown", "INVALID LOCATION ID"}

# (speaker, move, stance). Stances are views, not lines. Montaigne's always admit the log is his source.
_POOLS: dict[str, list[tuple[str, str, str]]] = {
    "recap": [("elah", "DEADPAN", "picking up where the last flight left off"),
              ("elah", "PRACTICAL", "a short recap before flying")],
    "callback": [("elah", "CALLBACK", "last time held a first; worth remembering")],
    "essay": [("montaigne", "ESSAY_DIGRESSION", "he wrote about the pilot while they were away, working from the log"),
              ("montaigne", "NEAR_RECOGNITION", "he half recalls the last flight, secondhand from the log"),
              ("montaigne", "PILOT_CHARACTER", "what the log suggests about the pilot's character")],
    "first": [("elah", "DEADPAN", "first flight on record; nothing remembered yet"),
              ("elah", "PRACTICAL", "first flight together; noting where it starts")],
}
for _pool in _POOLS.values():
    for _spk, _mv, _ in _pool:
        assert _mv in (_ELAH_MOVES if _spk == "elah" else _MONT_MOVES), f"untrained move {_mv}"

# Selftest hook ONLY. None = the honest guard. See _mutation_check().
_MUTATION: Optional[dict] = None
_SELF_TRACE = True


# ---- small helpers ---------------------------------------------------------------------------------------------
def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat()


def _field(recap: dict, key: str) -> Any:
    """THE absent-fact guard: a missing field is None, never a default."""
    if _MUTATION is not None and key in _MUTATION:
        return recap.get(key, _MUTATION[key])          # the defect the mutation check plants
    return recap.get(key)


def _dreams_dir(store: "ms.Store", state: dict) -> Path:
    return Path(state.get("dreams_dir") or (store.dir / "dreams"))


def read_session(path: Path) -> list[dict]:
    out = []
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue                                     # torn last line after a crash
    return out


def _session_files(dd: Path, exclude: Optional[str]) -> list[Path]:
    sdir = dd / "sessions"
    if not sdir.exists():
        return []
    files = [f for f in sdir.glob("*.jsonl") if f.name != exclude]
    return sorted(files, key=lambda f: f.name, reverse=True)       # names start with the local timestamp


def _loc(e: dict) -> Optional[str]:
    d = e.get("data") or {}
    v = d.get("location_name") or d.get("location")      # live events carry location_name; dream fixtures location
    v = str(v).strip() if v else ""
    return None if v in _BAD_LOCATIONS else v


def summarize(events: list[dict], session: Optional[str] = None) -> dict:
    """Pure: session events -> recap dict. Only fields the events support are present."""
    ts = [e["t"] for e in events if isinstance(e.get("t"), (int, float))]
    if not ts:
        return {}
    notable = [e for e in events if e.get("type") not in ("session_start", "session_end")]
    r: dict[str, Any] = {"started_at": min(ts), "ended_at": max(ts),
                         "minutes": int(round((max(ts) - min(ts)) / 60)),
                         "ended_cleanly": any(e.get("type") == "session_end" for e in events),
                         "events": len(notable),
                         "injuries": sum(e.get("type") == "injury" for e in events),
                         "deaths": sum(e.get("type") == "incapacitated" for e in events)}
    rewards = [int((e.get("data") or {}).get("amount") or 0) for e in events if e.get("type") in _REWARD_TYPES]
    if rewards:
        r["rewards_auec"] = sum(rewards)
    locs = []
    for e in events:
        if e.get("type") in _LOCATION_TYPES:
            v = _loc(e)
            if v and (not locs or locs[-1] != v):
                locs.append(v)
    if locs:
        r["locations"] = locs
        r["last_location"] = locs[-1]
    if session:
        r["session"] = session
    return r


def _recaps(store: "ms.Store") -> list[dict]:
    rows = [m for m in ms.query(store, kind="HISTORY", predicate=RECAP_PRED) if isinstance(m.get("value"), dict)]
    return sorted(rows, key=lambda m: m["value"].get("ended_at", 0))


def _callbacks(store: "ms.Store", kind: str) -> list[dict]:
    return [c for c in ms.recent_callbacks(store, n=500) if c.get("kind") == kind]


# ---- 2. shutdown ----------------------------------------------------------------------------------------------
def recap_record(session_events: list[dict], store: "ms.Store", session: Optional[str] = None) -> Optional[dict]:
    """Store the recap of one session as ONE HISTORY memory. Instant: no model, one atomic write.
    Idempotent per session name (shutdown + crash backfill never double-write). Returns the memory row, or None
    when the events hold nothing (no timestamps)."""
    recap = summarize(session_events, session)
    if not recap:
        return None
    if session:
        for m in _recaps(store):
            if m["value"].get("session") == session:
                return m
    return ms.add_memory(store, "shared", "HISTORY", RECAP_PRED, recap, created=_iso(recap["ended_at"]))


# ---- 1. launch -------------------------------------------------------------------------------------------------
def _latest_end(store: "ms.Store", dd: Path, cur: Optional[str]) -> Optional[float]:
    ends = []
    files = _session_files(dd, cur)
    if files:
        ev = read_session(files[0])
        ts = [e["t"] for e in ev if isinstance(e.get("t"), (int, float))]
        if ts:
            ends.append(max(ts))
    rec = _recaps(store)
    if rec:
        ends.append(rec[-1]["value"].get("ended_at", 0))
    return max(ends) if ends else None


def _substantive_recap(store: "ms.Store", dd: Path, cur: Optional[str]) -> Optional[dict]:
    """Newest session worth recalling, as its recap memory. Backfills the recap of a session that never got one
    (crash: no shutdown ran), so the session file on disk is the source either way."""
    by_session = {m["value"].get("session"): m for m in _recaps(store)}
    for f in _session_files(dd, cur):
        m = by_session.get(f.name)
        v = m["value"] if m else summarize(read_session(f), f.name)
        if not ((v.get("minutes") or 0) >= MIN_SESSION_MIN or (v.get("events") or 0) > 0):
            continue                                     # trivial: neither recalled nor backfilled
        return m or recap_record(read_session(f), store, session=f.name)
    rec = [m for m in _recaps(store) if m["value"].get("session") != cur]
    return rec[-1] if rec else None


def _facts(store: "ms.Store", recap_mem: dict, now: float) -> list[dict]:
    """Candidate facts, each with its provenance. Order = priority within its group."""
    r, rid, out = recap_mem["value"], recap_mem["id"], []

    def add(group, pred, value, prov, number=False):
        out.append({"group": group, "pred": pred, "value": value, "prov": prov, "number": number})

    ended = _field(r, "ended_at")
    if isinstance(ended, (int, float)):
        gap = max(0.0, now - ended)
        unit, n = (("days", int(gap // 86400)) if gap >= 172800 else
                   ("hours", int(gap // 3600)) if gap >= 3600 else ("minutes", int(gap // 60)))
        add("when", f"session.last.{unit}_ago", n, {"src": "derived", "id": rid, "field": "ended_at", "unit": unit},
            number=True)
    for key, pred in (("deaths", "session.last.deaths"), ("injuries", "session.last.injuries"),
                      ("rewards_auec", "session.last.rewards_auec")):
        v = _field(r, key)
        if isinstance(v, int) and v > 0:
            add("count", pred, v, {"src": "memory", "id": rid, "field": key}, number=True)
    v = _field(r, "last_location")
    if v:
        add("count", "session.last.ended_at_location", v, {"src": "memory", "id": rid, "field": "last_location"})
    v = _field(r, "minutes")
    if isinstance(v, int) and v > 0:
        add("count", "session.last.minutes", v, {"src": "memory", "id": rid, "field": "minutes"}, number=True)

    start, end = r.get("started_at"), r.get("ended_at")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)):
        rel = ms._read_json(store.paths["relationship"], {"milestones": []})
        order = {"first_death_together": 0, "first_injury_together": 2, "first_reward_together": 3}
        for m in sorted(rel.get("milestones", []), key=lambda m: order.get(m.get("name"), 9)):
            t = m.get("t")
            if m.get("name") and isinstance(t, (int, float)) and start - 1 <= t <= end + 1:
                add("callback", "relationship.milestone", m["name"], {"src": "relationship", "milestone": m["name"]})
        for h in ms.query(store, kind="HISTORY", predicate="history.location_first_visit"):
            try:
                t = time.mktime(time.strptime(h["created"][:19], "%Y-%m-%dT%H:%M:%S"))   # dream job: local time
            except (ValueError, TypeError):
                continue
            if start - 1 <= t <= end + 1:
                add("callback", "history.location_first_visit", h["value"], {"src": "memory", "id": h["id"], "field": None})
        out.sort(key=lambda f: (f["group"] != "callback", f["pred"] == "history.location_first_visit"))

    # Montaigne's essays: an INTERPRETATION he owns (dream essay job) or an essays.jsonl entry. The FACT is that he
    # wrote it, so the claim is HISTORY; its content stays his, labelled by the stance.
    essays = []
    for m in ms.query(store, owner="montaigne", kind="INTERPRETATION"):
        if (m.get("confidence") or 0) >= 0.5 and m.get("grounds"):
            if m.get("text"):
                essays.append((m["created"], "montaigne.essay_title", m["text"], {"src": "memory", "id": m["id"], "field": "text"}))
            else:
                essays.append((m["created"], "montaigne.essay_on_pilot", m["value"], {"src": "memory", "id": m["id"], "field": None}))
    for e in ms._read_jsonl(store.paths["essays"]):
        if e.get("title") and e.get("grounds"):
            essays.append((e["created"], "montaigne.essay_title", e["title"], {"src": "essay", "id": e["id"]}))
    for _, pred, val, prov in sorted(essays, key=lambda x: x[0], reverse=True):
        add("essay", pred, val, prov)
    return out


def _key(pred: str, value: Any) -> str:
    return f"{pred}={json.dumps(value, sort_keys=True)}"


def welcome_spec(store: "ms.Store", state: Optional[dict], now: float) -> Optional[Spec]:
    """ONE welcome spec from memory, or None. state: {"session_file": <current recorder file name>, optional
    "dreams_dir"}. Call once, after the dream catch-up (so milestones/first visits of the last session exist)."""
    state = state or {}
    cur = state.get("session_file")
    dd = _dreams_dir(store, state)
    last_end = _latest_end(store, dd, cur)
    if last_end is None:
        return None                                      # nothing remembered: first_meeting_spec's job
    if now - last_end < MIN_GAP_S:
        return None                                      # crash-restart, not a homecoming
    prev = _callbacks(store, WELCOME_KIND)
    if cur and prev and (prev[0].get("meta") or {}).get("session") == cur:
        return None                                      # once per session
    recap = _substantive_recap(store, dd, cur)
    if recap is None:
        return None
    used = set((prev[0].get("meta") or {}).get("facts", [])) if prev else set()
    facts = [f for f in _facts(store, recap, now) if _key(f["pred"], f["value"]) not in used]
    if not facts:
        return None

    essay = next((f for f in facts if f["group"] == "essay"), None)
    callback = next((f for f in facts if f["group"] == "callback"), None)
    when = next((f for f in facts if f["group"] == "when"), None)
    counts = [f for f in facts if f["group"] == "count"]
    if essay:
        mode, chosen = "essay", [essay] + [f for f in (when, *counts[:1]) if f]
    else:
        mode = "callback" if callback else "recap"
        chosen = [f for f in (when, callback) if f]
        chosen += counts[:MAX_CLAIMS - len(chosen)]
    if not chosen:
        return None
    chosen = chosen[:MAX_CLAIMS]
    pool = _POOLS[mode]
    variant = len(prev) % len(pool)
    speaker, move, stance = pool[variant]

    claims, prov = [], {}
    for i, f in enumerate(chosen, 1):
        cid = f"C{i}"
        claims.append(_claim(cid, "HISTORY", f["pred"], f["value"]))
        prov[cid] = f["prov"]
    headline = next((f for f in chosen if f["number"] and f["group"] == "count"), None)
    spec = {"scenario": f"welcome_{mode}", "speaker": speaker, "rhetoric": [move], "claims": claims,
            "interpretation": {"owner": speaker, "text": stance}, "required_claims": ["C1"],
            "required_values": [str(headline["value"])] if headline else [],
            "length_words": list(_LEN), "id": f"welcome_{mode}_v{variant}",
            "provenance": prov, "session_recalled": recap["value"].get("session")}
    if _SELF_TRACE and trace(spec, store, now):
        return None                                      # never speak a claim that does not trace
    return spec


def first_meeting_spec(store: "ms.Store", state: Optional[dict], now: float) -> Optional[Spec]:
    """Only when nothing is remembered: no recap, no earlier session file, no earlier first meeting. Live state
    only (location, ship: OBSERVED), plus the one memory fact that makes it a first: zero sessions on record."""
    state = state or {}
    if _recaps(store) or _session_files(_dreams_dir(store, state), state.get("session_file")):
        return None
    if _callbacks(store, FIRST_MEETING_KIND):
        return None
    claims, prov = [_claim("C1", "HISTORY", "memory.sessions_on_record", 0)], {"C1": {"src": "count", "predicate": RECAP_PRED}}
    for key, pred in (("location", "location.name"), ("ship", "ship.name")):
        v = state.get(key)
        if v and str(v) not in _BAD_LOCATIONS:
            prov[f"C{len(claims) + 1}"] = {"src": "state", "key": key}
            claims.append(_claim(f"C{len(claims) + 1}", "OBSERVED", pred, v))
    if len(claims) == 1:
        return None                                      # nothing live to anchor it; silence beats a vague hello
    speaker, move, stance = _POOLS["first"][0]
    return {"scenario": "first_meeting", "speaker": speaker, "rhetoric": [move], "claims": claims,
            "interpretation": {"owner": speaker, "text": stance}, "required_claims": ["C2"],
            "required_values": [], "length_words": list(_LEN), "id": "first_meeting_v0", "provenance": prov}


def mark_welcomed(store: "ms.Store", spec: Spec, now: float, session_file: Optional[str]) -> dict:
    """Record which facts this launch used (on ATTEMPT: a refused line still rotates the facts, which errs toward
    novelty, never repetition). record_callback stores fact keys, not text: no sentence exists here."""
    kind = FIRST_MEETING_KIND if spec["scenario"] == "first_meeting" else WELCOME_KIND
    return ms.record_callback(store, spec["id"], kind=kind, created=_iso(now),
                              meta={"session": session_file, "scenario": spec["scenario"],
                                    "facts": [_key(c["predicate"], c["value"]) for c in spec["claims"]]})


# ---- provenance check ------------------------------------------------------------------------------------------
def trace(spec: Spec, store: "ms.Store", now: float, state: Optional[dict] = None) -> list[str]:
    """[] when every claim value is re-derivable from memory (or live state for first-meeting OBSERVED claims)."""
    probs = []
    mems = {m["id"]: m for m in ms._read_jsonl(store.paths["memories"])}
    essays = {e["id"]: e for e in ms._read_jsonl(store.paths["essays"])}
    rel = ms._read_json(store.paths["relationship"], {"milestones": []})
    allowed = _ELAH_MOVES if spec["speaker"] == "elah" else _MONT_MOVES
    if any(mv not in allowed for mv in spec["rhetoric"]):
        probs.append(f"untrained move {spec['rhetoric']} for {spec['speaker']}")
    for c in spec["claims"]:
        p = (spec.get("provenance") or {}).get(c["id"])
        if not p:
            probs.append(f"{c['id']} has no provenance")
            continue
        src = p.get("src")
        if spec["scenario"].startswith("welcome") and c["kind"] != "HISTORY":
            probs.append(f"{c['id']} kind {c['kind']} in a welcome")
        if src in ("memory", "derived"):
            m = mems.get(p.get("id"))
            if m is None:
                probs.append(f"{c['id']} cites missing memory {p.get('id')}")
                continue
            if p.get("field") is None:
                truth, present = m["value"], True
            elif p["field"] == "text":
                truth, present = m.get("text"), bool(m.get("text"))
            else:
                present = isinstance(m["value"], dict) and p["field"] in m["value"]
                truth = m["value"].get(p["field"]) if present else None
            if not present:
                probs.append(f"{c['id']} {c['predicate']}={c['value']!r}: field {p['field']!r} absent from {m['id']}")
                continue
            if src == "derived":
                gap = max(0.0, now - truth)
                truth = int(gap // {"days": 86400, "hours": 3600, "minutes": 60}[p["unit"]])
            if truth != c["value"]:
                probs.append(f"{c['id']} {c['predicate']}={c['value']!r} but memory says {truth!r}")
        elif src == "relationship":
            if not any(m.get("name") == c["value"] == p.get("milestone") for m in rel.get("milestones", [])):
                probs.append(f"{c['id']} milestone {c['value']!r} not in relationship.json")
        elif src == "essay":
            if (essays.get(p.get("id")) or {}).get("title") != c["value"]:
                probs.append(f"{c['id']} essay title does not match {p.get('id')}")
        elif src == "count":
            if len(ms.query(store, kind="HISTORY", predicate=p["predicate"])) != c["value"]:
                probs.append(f"{c['id']} count mismatch")
        elif src == "state":
            if state is not None and state.get(p["key"]) != c["value"]:
                probs.append(f"{c['id']} not in live state")
        else:
            probs.append(f"{c['id']} unknown provenance {src!r}")
    for v in spec.get("required_values", []):
        if not any(str(c["value"]) == v for c in spec["claims"]):
            probs.append(f"required value {v} is not a claim value")
    if str(spec["interpretation"]["text"]).rstrip()[-1:] in ".!?":
        probs.append("stance reads as a finished sentence")
    return probs


# ---- 4. selftest -----------------------------------------------------------------------------------------------
def _selftest(verbose: bool = True) -> int:
    import tempfile
    from dream_queue import SessionRecorder, DreamQueue
    from grounding_validator import ground

    results, shown = [], {}

    def case(name, cond, detail=""):
        results.append((name, bool(cond), detail))

    def honest(spec):          # echoes claim values, like a faithful realizer
        vals = [str(c["value"]) for c in spec["claims"] if not isinstance(c["value"], bool)]
        words = ("Recap: " + "; ".join(vals) + ".").split()
        lo, _ = spec["length_words"]
        return " ".join(words + ["steady"] * max(0, lo - len(words)))

    def liar(spec):
        return honest(spec).rstrip(".") + " and 4817 credits more."

    def check_spec(label, spec, store, now, state=None, welcome=True):
        shown[label] = spec
        case(f"{label}: every claim traces to memory", not trace(spec, store, now, state), trace(spec, store, now, state))
        if welcome:
            case(f"{label}: all claims kind HISTORY", all(c["kind"] == "HISTORY" for c in spec["claims"]))
        allowed = _ELAH_MOVES if spec["speaker"] == "elah" else _MONT_MOVES
        case(f"{label}: trained moves only", all(m in allowed for m in spec["rhetoric"]))
        case(f"{label}: honest realizer passes grounding", not ground(spec, honest(spec)), ground(spec, honest(spec)))
        case(f"{label}: invented number fails grounding", bool(ground(spec, liar(spec))))

    def session(dreams, clock, evs, close=True):
        rec = SessionRecorder(dreams, now=lambda: clock[0])
        for dt, et, data in evs:
            clock[0] += dt
            rec.note(et, data)
        if close:
            path = rec.close()
        else:
            path = rec.path
        return path

    def dream(store, dreams, clock, generator=None):
        q = DreamQueue(store, dreams, now=lambda: clock[0], generator=generator)
        q.enqueue_closed_sessions()
        q.run_until(clock[0] + 60, window="launch")
        if generator:
            q.run_one("quantum", "ROOMY")

    root = Path(tempfile.mkdtemp(prefix="session_story_"))
    T0 = 1_790_000_000.0

    # ---- fixture 0: no prior session ---------------------------------------------------------------------------
    s0 = ms.open_store(root, "p0")
    d0 = s0.dir / "dreams"
    c0 = [T0]
    cur = SessionRecorder(d0, now=lambda: c0[0])
    st0 = {"session_file": cur.path.name, "location": "Lorville", "ship": "Cutlass Black"}
    case("0 sessions: no welcome", welcome_spec(s0, st0, c0[0]) is None)
    fm = first_meeting_spec(s0, st0, c0[0])
    case("0 sessions: first meeting spec exists", fm is not None)
    if fm:
        check_spec("first_meeting", fm, s0, c0[0], state=st0, welcome=False)
        mark_welcomed(s0, fm, c0[0], cur.path.name)
        case("first meeting happens once ever", first_meeting_spec(s0, st0, c0[0] + 5) is None)
    s0b = ms.open_store(root, "p0b")
    case("first meeting with no live facts: silence", first_meeting_spec(s0b, {}, T0) is None)

    # ---- fixture 1: one prior session (+ crash, + gap guard) ---------------------------------------------------
    s1 = ms.open_store(root, "p1")
    d1 = s1.dir / "dreams"
    c1 = [T0]
    p = session(d1, c1, [(60, "location_change", {"location": "Lorville"}),
                         (540, "injury", {"body_part": "left leg"}),
                         (600, "mission_reward", {"amount": 15000}),
                         (600, "location_change", {"location_name": "Area18"}),
                         (600, "chat_noise", {})])
    t_shut = time.perf_counter()
    rm = recap_record(read_session(p), s1, session=p.name)            # what shutdown does
    shut_ms = (time.perf_counter() - t_shut) * 1000
    case("recap_record is instant (< 100 ms, no model)", shut_ms < 100, f"{shut_ms:.1f} ms")
    case("recap_record is idempotent per session", recap_record(read_session(p), s1, session=p.name)["id"] == rm["id"]
         and len(_recaps(s1)) == 1)
    dream(s1, d1, c1)
    cur1 = SessionRecorder(d1, now=lambda: c1[0] + 300)
    case("1 session, relaunch 5 min later: no welcome", welcome_spec(s1, {"session_file": cur1.path.name}, c1[0] + 300) is None)
    now1 = c1[0] + 3 * 3600 + 120
    cur1b = SessionRecorder(d1, now=lambda: now1)
    w1 = welcome_spec(s1, {"session_file": cur1b.path.name}, now1)
    case("1 session, 3 h later: welcome exists", w1 is not None)
    if w1:
        check_spec("welcome_1_session", w1, s1, now1)
        case("factual recap is Elah's", w1["speaker"] == "elah")
        case("a first in that session makes it a CALLBACK", w1["rhetoric"] == ["CALLBACK"])
        mark_welcomed(s1, w1, now1, cur1b.path.name)
        case("once per session", welcome_spec(s1, {"session_file": cur1b.path.name}, now1 + 60) is None)

    # crash: session left .open, no recap written; relaunch 20 min later backfills from the file
    sc = ms.open_store(root, "pcrash")
    dc = sc.dir / "dreams"
    cc = [T0]
    session(dc, cc, [(60, "injury", {"body_part": "torso"}), (900, "incapacitated", {})], close=False)
    case("crash-restart 2 min later: no welcome", welcome_spec(sc, {"session_file": "x"}, cc[0] + 120) is None)
    wc = welcome_spec(sc, {"session_file": "x"}, cc[0] + 1200)
    case("crashed session: recap backfilled from the session file", len(_recaps(sc)) == 1
         and _recaps(sc)[0]["value"]["ended_cleanly"] is False)
    case("crashed session 20 min later: welcome exists", wc is not None)
    if wc:
        check_spec("welcome_after_crash", wc, sc, cc[0] + 1200)
        case("no location recorded -> no location claim (absent fact stays absent)",
             not any(c["predicate"] == "session.last.ended_at_location" for c in wc["claims"]))

    # ---- fixture 3: three prior sessions, an essay, first death --------------------------------------------------
    s3 = ms.open_store(root, "p3")
    d3 = s3.dir / "dreams"
    c3 = [T0]
    for i, evs in enumerate([
        [(120, "location_change", {"location": "Lorville"}), (1500, "mission_reward", {"amount": 8000})],
        [(90, "location_change", {"location": "New Babbage"}), (700, "injury", {"body_part": "right arm"})],
        [(60, "location_change", {"location": "Orison"}), (400, "incapacitated", {}), (300, "injury", {"body_part": "head"}),
         (800, "incapacitated", {}), (600, "mission_reward", {"amount": 22500})],
    ]):
        pth = session(d3, c3, evs)
        recap_record(read_session(pth), s3, session=pth.name)
        dream(s3, d3, c3)
        c3[0] += 86400
    orison = [m for m in ms.query(s3, kind="HISTORY", predicate="history.location_first_visit") if m["value"] == "Orison"]
    dream(s3, d3, c3, generator=lambda kind, ctx: {"predicate": f"{kind}.draft", "value": "the pilot dies and returns",
                                                  "confidence": 0.7, "grounds": [orison[0]["id"]],
                                                  "text": "Of a Pilot Who Dies Twice Before Supper"})
    now3 = c3[0] + 3600
    cur3 = SessionRecorder(d3, now=lambda: now3)
    w3a = welcome_spec(s3, {"session_file": cur3.path.name}, now3)
    case("3 sessions: welcome exists", w3a is not None)
    if w3a:
        check_spec("welcome_3_sessions_essay", w3a, s3, now3)
        case("an essay to allude to makes it Montaigne's", w3a["speaker"] == "montaigne")
        case("it recalls the LATEST session", w3a["session_recalled"] == sorted(
            f.name for f in (d3 / "sessions").glob("*.closed.jsonl") if f.name != cur3.path.name)[-1])
        mark_welcomed(s3, w3a, now3, cur3.path.name)
    cur3.close()                                        # a 0-minute launch: nothing happened, closed at once
    now3b = now3 + 86400
    cur3b = SessionRecorder(d3, now=lambda: now3b)
    w3b = welcome_spec(s3, {"session_file": cur3b.path.name}, now3b)
    case("next launch: welcome exists", w3b is not None)
    if w3a and w3b:
        check_spec("welcome_3_sessions_next_launch", w3b, s3, now3b)
        ka = {_key(c["predicate"], c["value"]) for c in w3a["claims"]}
        kb = {_key(c["predicate"], c["value"]) for c in w3b["claims"]}
        case("no welcome fact repeats on consecutive launches", not (ka & kb), sorted(ka & kb))
        case("a trivial launch in between is skipped as the recalled session",
             w3b["session_recalled"] == w3a["session_recalled"])

    for name, ok, detail in results:
        if verbose or not ok:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"   [{detail}]" if detail and not ok else
                                                             f"   ({detail})" if detail and verbose else ""))
    import shutil
    shutil.rmtree(root, ignore_errors=True)             # tight disk: leave nothing behind
    bad = sum(not ok for _, ok, _ in results)
    print(f"session_story selftest: {len(results) - bad}/{len(results)} passed")
    if verbose and "--show" in sys.argv:
        print(json.dumps(shown, indent=1, default=str))
    return 1 if bad else 0


def _mutation_check() -> int:
    """Plant the defect (an absent last_location becomes the guessed default 'Lorville') and require the selftest
    to FAIL -- with the runtime self-trace on (the spec disappears) and off (trace() flags the claim)."""
    global _MUTATION, _SELF_TRACE
    caught = []
    for self_trace in (True, False):
        _MUTATION, _SELF_TRACE = {"last_location": "Lorville"}, self_trace
        print(f"-- mutation: absent last_location -> 'Lorville'; runtime self-trace {'ON' if self_trace else 'OFF'}")
        rc = _selftest(verbose=False)
        caught.append(rc != 0)
        print(f"   {'CAUGHT' if rc else 'MISSED'}")
    _MUTATION, _SELF_TRACE = None, True
    ok = all(caught)
    print(f"mutation check: {'PASS (selftest catches the planted guess)' if ok else 'FAIL (a guess slipped through)'}")
    return 0 if ok else 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--mutation-check" in sys.argv:
        sys.exit(_mutation_check())
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
