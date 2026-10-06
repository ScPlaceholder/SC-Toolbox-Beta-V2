"""dream_queue.py - dreaming as a QUEUE, not a session (ARCHITECTURE.md "When the thinking happens").

A lot of users will close the toolbox upon closing the game. So there is no after-game dreaming session to
rely on. Instead:

  DURING PLAY   SessionRecorder.note(event) appends one line per notable event to the open session file. It is
                written as it happens, so closing the toolbox (or a crash) loses nothing.
  QUIET WINDOW  DreamQueue.run_one(window) takes ONE job, if the window and headroom allow it, and stops. A job
                is small and checkpointed: preemption loses at most the job in hand.
  NEXT LAUNCH   DreamQueue.enqueue_closed_sessions() turns any closed-but-unprocessed session into jobs, and
                run_until(deadline) catches up while Star Citizen is still loading.

Job order: CHEAP DETERMINISTIC first (no model): counters, first-time HISTORY facts, milestones, snapshot.
MODEL jobs last (Montaigne's essay, move proposals): they need a ROOMY window and a generator, and they wait,
never fail, when either is missing. Dreams produce SEMANTIC AMMUNITION (memories with provenance), never dialogue:
ambient_spec reads the HISTORY facts back as claims the realizer may use for callbacks.

State lives next to the pilot's memory store: <root>/<pilot>/dreams/{sessions/*.jsonl, queue.json}.
Selftest: python dream_queue.py --selftest
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import memory_store as ms  # noqa: E402

# job kind -> (needs model?, minimum window)
JOB_KINDS = {
    "counters":   (False, "any"),
    "history":    (False, "any"),
    "milestones": (False, "any"),
    "snapshot":   (False, "any"),
    "essay":      (True, "ROOMY"),
    "moves":      (True, "ROOMY"),
}
DETERMINISTIC_ORDER = ["counters", "history", "milestones", "snapshot"]
MODEL_ORDER = ["essay", "moves"]
QUIET_WINDOWS = {"quantum", "parked", "menu", "refinery", "launch"}      # "launch" = catch-up before SC is up
# Live event names come from event_parser/event_classifier: reward_earned, qt_arrived, ship_channel_joined,
# player_respawned. The first version listed invented names (mission_reward, quantum_arrived), so real sessions
# recorded no rewards or arrivals. Old names kept for existing records.
NOTABLE = {"injury", "incapacitated", "med_bed_heal", "location_change", "jurisdiction_change",
           "reward_earned", "qt_arrived", "ship_channel_joined", "player_respawned", "heartbeat",
           "quantum_arrived", "ship_entered", "mission_reward"}
REWARD_EVENTS = {"reward_earned", "mission_reward"}
HEARTBEAT_S = 300.0


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex[:6]}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, path)


class SessionRecorder:
    """Append-only record of one play session. Each note() is one line, flushed immediately."""

    def __init__(self, dreams_dir: Path, now: Callable[[], float] = time.time):
        self.now = now
        self.dir = dreams_dir / "sessions"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / f"{time.strftime('%Y%m%d_%H%M%S', time.localtime(now()))}_{uuid.uuid4().hex[:4]}.open.jsonl"
        self._write({"t": now(), "type": "session_start"})

    def _write(self, obj: dict) -> None:
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, default=str) + "\n")
            f.flush()

    def note(self, event_type: str, data: dict) -> bool:
        # A heartbeat at most every HEARTBEAT_S keeps a crashed session's length honest even when nothing notable
        # happened for a long stretch before the crash.
        t = self.now()
        if event_type not in NOTABLE:
            if t - getattr(self, "_last_write", 0) >= HEARTBEAT_S:
                self._write({"t": t, "type": "heartbeat"})
                self._last_write = t
            return False
        self._last_write = t
        self._write({"t": self.now(), "type": event_type, "data": data})
        return True

    def close(self) -> Path:
        """Instant: one line and a rename. This is ALL that happens when the toolbox closes."""
        self._write({"t": self.now(), "type": "session_end"})
        closed = self.path.with_name(self.path.name.replace(".open.jsonl", ".closed.jsonl"))
        os.replace(self.path, closed)
        self.path = closed
        return closed


class DreamQueue:
    def __init__(self, store: "ms.Store", dreams_dir: Path, now: Callable[[], float] = time.time,
                 generator: Optional[Callable[[str, dict], Optional[dict]]] = None):
        """generator(kind, context) -> {"predicate", "value", "text", "confidence", "grounds"} or None.
        None (no model loaded, or it declined) leaves the job queued for a later window."""
        self.store, self.dir, self.now, self.generator = store, dreams_dir, now, generator
        self.qpath = dreams_dir / "queue.json"
        self.jobs: list[dict] = json.loads(self.qpath.read_text(encoding="utf-8")) if self.qpath.exists() else []

    def _save(self) -> None:
        _atomic_write(self.qpath, json.dumps(self.jobs, indent=1))

    # -- intake -----------------------------------------------------------------------------------------------
    def enqueue_closed_sessions(self) -> int:
        """Every closed session file becomes one job per kind. A crashed session (still .open) older than an
        hour is treated as closed: the record is intact up to the crash."""
        n = 0
        sessions = self.dir / "sessions"
        if not sessions.exists():
            return 0
        queued = {j["session"] for j in self.jobs}
        for f in sorted(sessions.glob("*.jsonl")):
            stale_open = f.name.endswith(".open.jsonl") and self.now() - f.stat().st_mtime > 3600
            if not (f.name.endswith(".closed.jsonl") or stale_open) or f.name in queued:
                continue
            for kind in DETERMINISTIC_ORDER + MODEL_ORDER:
                self.jobs.append({"id": uuid.uuid4().hex[:8], "session": f.name, "kind": kind, "status": "pending",
                                  "attempts": 0})
            n += 1
        if n:
            self._save()
        return n

    def pending(self) -> list[dict]:
        order = {k: i for i, k in enumerate(DETERMINISTIC_ORDER + MODEL_ORDER)}
        return sorted((j for j in self.jobs if j["status"] == "pending"), key=lambda j: (order[j["kind"]], j["session"]))

    # -- running ----------------------------------------------------------------------------------------------
    def run_one(self, window: Optional[str], headroom: str = "OK") -> Optional[dict]:
        """Run at most ONE job. Returns the job it finished, or None (nothing runnable in this window)."""
        if window not in QUIET_WINDOWS or headroom == "TIGHT":
            return None
        for job in self.pending():
            needs_model, min_window = JOB_KINDS[job["kind"]]
            if needs_model and (headroom != "ROOMY" or self.generator is None):
                continue                      # wait for a better window; deterministic work never waits on this
            job["attempts"] += 1
            try:
                job["result"] = getattr(self, f"_job_{job['kind']}")(self._events(job["session"]))
                job["status"] = "done" if job["result"] is not None else "pending"
                # A generator that keeps declining (bad model output, model missing) must not burn a ROOMY
                # window forever: give up after 5 empty attempts.
                if job["result"] is None and job["attempts"] >= 5:
                    job["status"] = "failed"
                    job["error"] = "generator declined 5 times"
            except Exception as e:           # a bad record must not wedge the queue forever
                job["error"] = f"{type(e).__name__}: {e}"[:200]
                job["status"] = "failed" if job["attempts"] >= 3 else "pending"
            self._save()
            return job if job["status"] == "done" else None
        return None

    def run_until(self, deadline: float, window: str = "launch", headroom: Callable[[], str] = lambda: "OK") -> int:
        done = 0
        while self.now() < deadline:
            if self.run_one(window, headroom()) is None:
                break
            done += 1
        return done

    def _events(self, session_file: str) -> list[dict]:
        path = self.dir / "sessions" / session_file
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue                      # a torn last line from a crash is expected, not an error
        return out

    # -- deterministic jobs -----------------------------------------------------------------------------------
    def _job_counters(self, ev: list[dict]) -> dict:
        t = [e["t"] for e in ev]
        c = {"sessions": 1, "minutes_together": round((max(t) - min(t)) / 60, 1) if t else 0,
             "injuries": sum(e["type"] == "injury" for e in ev),
             "deaths": sum(e["type"] == "incapacitated" for e in ev),
             "rewards_auec": sum(int((e.get("data") or {}).get("amount") or 0) for e in ev if e["type"] in REWARD_EVENTS)}
        ms.update_relationship(self.store, counters=c)
        return c

    def _job_history(self, ev: list[dict]) -> dict:
        """First-time facts only, as HISTORY memories. 'First' is checked against the store, not the session."""
        added = []
        # Live events carry location_name (location_change) and ship_type (ship_channel_joined); the old keys are
        # kept for records written before the fix.
        for pred, etypes, keys in (("history.location_first_visit", {"location_change"}, ("location_name", "location")),
                                   ("history.ship_first_flown", {"ship_channel_joined", "ship_entered"},
                                    ("ship_type", "ship"))):
            known = {m["value"] for m in ms.query(self.store, kind="HISTORY", predicate=pred)}
            for e in ev:
                d = e.get("data") or {}
                v = next((d[k] for k in keys if d.get(k)), None)
                if e["type"] in etypes and v and v not in known:
                    ms.add_memory(self.store, "shared", "HISTORY", pred, v,
                                  created=time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(e["t"])))
                    known.add(v)
                    added.append(v)
        return {"added": added}

    def _job_milestones(self, ev: list[dict]) -> dict:
        rel = json.loads(self.store.paths["relationship"].read_text(encoding="utf-8")) \
            if self.store.paths["relationship"].exists() else {}
        have = {m.get("name") for m in rel.get("milestones", [])}
        new = []
        for name, etype in (("first_death_together", "incapacitated"), ("first_injury_together", "injury"),
                            ("first_reward_together", "mission_reward")):
            hit = next((e for e in ev if e["type"] == etype), None)
            if hit and name not in have:
                ms.update_relationship(self.store, milestone={"name": name, "t": hit["t"]})
                new.append(name)
        return {"new": new}

    def _job_snapshot(self, ev: list[dict]) -> dict:
        p = ms.snapshot(self.store.root, self.store.pilot_id, keep=10)
        return {"snapshot": Path(p).name}

    # -- model jobs: semantic ammunition, never dialogue -------------------------------------------------------
    def _model_job(self, kind: str, ev: list[dict]) -> Optional[dict]:
        ctx = {"events": [e for e in ev if e["type"] in NOTABLE][-200:],
               "history": ms.query(self.store, kind="HISTORY")[-50:]}
        out = self.generator(kind, ctx)
        if not out:
            return None
        grounds = [g for g in out.get("grounds", []) if g]
        mem = ms.add_memory(self.store, "montaigne" if kind == "essay" else "shared", "INTERPRETATION",
                            out["predicate"], out["value"], confidence=float(out.get("confidence", 0.5)),
                            grounds=grounds, text=out.get("text"))
        return {"memory": mem["id"]}

    def _job_essay(self, ev):
        return self._model_job("essay", ev)

    def _job_moves(self, ev):
        return self._model_job("moves", ev)


def history_facts(store: "ms.Store", location: Optional[str] = None, ship: Optional[str] = None) -> dict:
    """What ambient_spec may cite as HISTORY: has the pilot been here / flown this before, and since when."""
    out = {}
    if location:
        m = [r for r in ms.query(store, kind="HISTORY", predicate="history.location_first_visit") if r["value"] == location]
        if m:
            out["location_first_visit"] = m[0]["created"][:10]
    if ship:
        m = [r for r in ms.query(store, kind="HISTORY", predicate="history.ship_first_flown") if r["value"] == ship]
        if m:
            out["ship_first_flown"] = m[0]["created"][:10]
    return out


# ---- selftest ------------------------------------------------------------------------------------------------
def _selftest() -> int:
    import tempfile
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    root = Path(tempfile.mkdtemp())
    store = ms.open_store(root, "pilot1")
    dreams = root / "pilot1" / "dreams"
    clock = [1_000_000.0]
    now = lambda: clock[0]

    rec = SessionRecorder(dreams, now=now)
    for etype, data in [("location_change", {"location": "Lorville"}), ("injury", {"body_part": "left leg"}),
                        ("chat_noise", {}), ("mission_reward", {"amount": 15000}),
                        ("location_change", {"location": "Area18"}), ("incapacitated", {})]:
        clock[0] += 300
        rec.note(etype, data)
    case("non-notable events are not recorded", "chat_noise" not in rec.path.read_text(encoding="utf-8"))
    case("record is on disk mid-session (crash-safe)", rec.path.exists() and rec.path.stat().st_size > 0)
    rec.close()

    q = DreamQueue(store, dreams, now=now)
    case("closed session becomes jobs", q.enqueue_closed_sessions() == 1 and len(q.pending()) == 6)
    case("re-enqueue is idempotent", q.enqueue_closed_sessions() == 0)

    case("nothing runs outside a quiet window", q.run_one("combat") is None)
    case("nothing runs when TIGHT", q.run_one("quantum", "TIGHT") is None)

    j = q.run_one("quantum", "OK")
    case("first job is deterministic counters", j and j["kind"] == "counters" and j["result"]["injuries"] == 1)
    rel = json.loads(store.paths["relationship"].read_text(encoding="utf-8"))
    case("counters reach the relationship file", rel["counters"]["rewards_auec"] == 15000)

    q2 = DreamQueue(store, dreams, now=now)            # a NEW process: the queue survived on disk
    case("queue checkpoint survives restart", [x["kind"] for x in q2.pending()][:1] == ["history"])

    done = q2.run_until(deadline=clock[0] + 10, window="launch")
    case("catch-up runs every deterministic job", done == 3)
    case("model jobs wait without a generator", {x["kind"] for x in q2.pending()} == {"essay", "moves"})
    hist = history_facts(store, location="Lorville")
    case("first visit recorded as HISTORY", "location_first_visit" in hist)

    rec2 = SessionRecorder(dreams, now=now)
    clock[0] += 500
    rec2.note("location_change", {"location": "Lorville"})
    rec2.close()
    q2.enqueue_closed_sessions()
    q2.run_until(deadline=clock[0] + 10, window="launch")
    lorville = [m for m in ms.query(store, kind="HISTORY") if m["value"] == "Lorville"]
    case("second visit is NOT a new first-visit", len(lorville) == 1)

    q3 = DreamQueue(store, dreams, now=now,
                    generator=lambda kind, ctx: {"predicate": f"{kind}.draft", "value": "the pilot rushes",
                                                 "confidence": 0.6, "grounds": [lorville[0]["id"]],
                                                 "text": "Of the Pilot Who Cannot Sit Still"})
    case("model job waits for ROOMY", q3.run_one("quantum", "OK") is None)
    j = q3.run_one("quantum", "ROOMY")
    case("model job writes an INTERPRETATION with grounds", j and j["kind"] == "essay"
         and ms.query(store, kind="INTERPRETATION")[0]["grounds"] == [lorville[0]["id"]])

    q4 = DreamQueue(store, dreams, now=now, generator=lambda k, c: {"predicate": "x", "value": 1, "confidence": 0.5,
                                                                     "grounds": ["M999999"]})
    for _ in range(3):
        q4.run_one("quantum", "ROOMY")
    case("an invented ground is refused, job fails after 3", any(x["status"] == "failed" for x in q4.jobs))

    crash = SessionRecorder(dreams, now=now)
    crash.note("injury", {"body_part": "torso"})
    with open(crash.path, "a", encoding="utf-8") as f:
        f.write('{"t": 1, "type": "inj')            # torn line, as a crash leaves it
    os.utime(crash.path, (clock[0] - 7200, clock[0] - 7200))
    q5 = DreamQueue(store, dreams, now=now)
    case("a crashed (stale .open) session is still processed", q5.enqueue_closed_sessions() == 1)
    j = q5.run_one("launch")
    case("a torn final line does not break the job", j and j["result"]["injuries"] == 1)

    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad = sum(not ok for _, ok in results)
    print(f"dream_queue selftest: {len(results) - bad}/{len(results)} passed")
    return 1 if bad else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
