"""dream_models.py - the MODEL jobs of the dream queue: Montaigne's per-pilot essay, and move proposals (2026-09-23).

DreamQueue (dream_queue.py) runs a model job only in a ROOMY quiet window and only if it was handed a generator:

    generator(kind, context) -> {"predicate", "value", "text", "confidence", "grounds"} or None

This module is that generator, backed by a local Ollama model. It produces SEMANTIC AMMUNITION, never dialogue
(ARCHITECTURE.md "When the thinking happens"): a title and a plain-language reading of the pilot, or one candidate
rhetorical move described as data. Nothing it returns is ever spoken as written.

THE CONTRACT (what DreamQueue stores via memory_store.add_memory as an INTERPRETATION):

  kind "essay"  -> {"predicate": "essay.pilot",
                    "value": "Of the Pilot Who Cannot Leave a Box Behind",      # the TITLE, his voice
                    "text": "<2-3 plain sentences: the pattern, and what he makes of it>",
                    "confidence": 0.0-0.85, "grounds": ["M000003", ...]}          # stored under owner montaigne
  kind "moves"  -> {"predicate": "move.proposal",
                    "value": {"name": "UPPER_SNAKE", "owner": "elah"|"montaigne", "base_move": <TRAINED move>,
                              "description": str, "example_stance": str},
                    "text": <description>, "confidence": 0.0-0.85, "grounds": [...]}  # stored under owner shared

  grounds: non-empty, and a SUBSET of the ids of the HISTORY/OBSERVED memories present in `context`
           (context["history"], plus context["observed"] / context["memories"] if a caller supplies them).
           memory_store would refuse an unknown id anyway; this refuses an id that exists in the store but was not
           in front of the model, because citing evidence you were not shown is invention too.
  None:    ANY failure: model not installed, server down, timeout, non-JSON, schema mismatch, invented grounds,
           a number that is not in the evidence, dialogue-shaped text. The job then simply waits for a later window
           (DreamQueue treats None as "not yet"), it never fails loudly. `self.last_error` says why, for logs/tests.

A move proposal only ever ENTERS the lifecycle as PROPOSED (move_lifecycle.py). Because the realizer's adapters were
trained on a closed set of moves (ambient_spec._ELAH_MOVES / _MONT_MOVES), every new move must name the trained
`base_move` it is realised through, and that base must belong to the move's owner. This file checks it at generation;
move_lifecycle checks it again, independently, before VALIDATED (a proposal can also arrive by import).

Resource rules (Principle 2 + today's disk/RAM pressure): CPU only (`num_gpu: 0`), 2 threads, `keep_alive` 10 s so
the model leaves memory as soon as the window's one job is done, and a small context/prediction budget.

Selftest (fake Ollama server, no model):   python dream_models.py --selftest
Mutation check (proves the tests bite):     python dream_models.py --mutation-check
One real call to gemma3:4b on CPU:          python dream_models.py --smoke
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from ambient_spec import _ELAH_MOVES, _MONT_MOVES  # noqa: E402  (the TRAINED moves; the adapters know only these)

TRAINED_MOVES: dict[str, frozenset] = {"elah": frozenset(_ELAH_MOVES), "montaigne": frozenset(_MONT_MOVES)}
GROUNDABLE_KINDS = {"HISTORY", "OBSERVED"}
MAX_CONFIDENCE = 0.85            # a dream is a reading, never a certainty; higher values are capped, not trusted
MOVE_NAME_RE = re.compile(r"^[A-Z][A-Z_]{2,39}$")
TITLE_RE = re.compile(r"^Of [A-Za-z]")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*")
QUOTE_CHARS = set('"“”')
SECOND_PERSON_RE = re.compile(r"\b(you|your|yours|yourself)\b", re.IGNORECASE)

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "gemma3:4b"


# ---- schemas (sent to Ollama as `format`; enums narrow what the model can even emit) --------------------------------
def _essay_schema(ids: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["title", "summary", "confidence", "grounds"],
            "properties": {"title": {"type": "string"}, "summary": {"type": "string"},
                           "confidence": {"type": "number"},
                           "grounds": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": ids}}}}


def _moves_schema(ids: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False,
            "required": ["name", "owner", "base_move", "description", "example_stance", "confidence", "grounds"],
            "properties": {"name": {"type": "string"}, "owner": {"type": "string", "enum": ["elah", "montaigne"]},
                           "base_move": {"type": "string", "enum": sorted(TRAINED_MOVES["elah"] | TRAINED_MOVES["montaigne"])},
                           "description": {"type": "string"}, "example_stance": {"type": "string"},
                           "confidence": {"type": "number"},
                           "grounds": {"type": "array", "minItems": 1, "items": {"type": "string", "enum": ids}}}}


# ---- context -> evidence pool ---------------------------------------------------------------------------------------
def evidence_pool(context: dict) -> list[dict]:
    """The memories the model is shown and may cite: HISTORY/OBSERVED rows with an id, de-duplicated, in order."""
    seen, out = set(), []
    for key in ("history", "observed", "memories"):
        for m in context.get(key) or []:
            if isinstance(m, dict) and m.get("id") and m.get("kind") in GROUNDABLE_KINDS and m["id"] not in seen:
                seen.add(m["id"])
                out.append(m)
    return out


def _numbers_in(obj: Any, acc: set) -> set:
    if isinstance(obj, bool) or obj is None:
        return acc
    if isinstance(obj, (int, float)):
        acc.add(str(obj))
        acc.add(str(int(obj)) if float(obj).is_integer() else str(obj))
    elif isinstance(obj, str):
        acc.update(NUMBER_RE.findall(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_in(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_in(v, acc)
    return acc


def _allowed_numbers(context: dict, pool: list[dict]) -> set:
    """Numbers the evidence actually contains. Memory VALUES and event DATA only: ids and timestamps are not facts
    about the pilot, and 'M000003' must not license the number 3."""
    acc: set = set()
    for m in pool:
        _numbers_in(m.get("value"), acc)
        _numbers_in(m.get("text"), acc)
    for e in context.get("events") or []:
        _numbers_in((e or {}).get("data"), acc)
    return {n.replace(",", "") for n in acc}


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


# ---- validators (pure; each returns an error string or None) --------------------------------------------------------
def check_grounds(grounds: Any, allowed_ids: set) -> Optional[str]:
    if not isinstance(grounds, list) or not grounds:
        return "grounds must be a non-empty list"
    if not all(isinstance(g, str) for g in grounds):
        return "grounds must be strings"
    invented = [g for g in grounds if g not in allowed_ids]
    if invented:
        return f"grounds not in context: {invented}"
    return None


def check_confidence(c: Any) -> Optional[str]:
    if isinstance(c, bool) or not isinstance(c, (int, float)) or not (0.0 <= float(c) <= 1.0):
        return f"confidence must be a number in [0,1], got {c!r}"
    return None


def check_numbers(text: str, allowed: set) -> Optional[str]:
    stray = [n for n in NUMBER_RE.findall(text) if n.replace(",", "") not in allowed]
    return f"numbers not in evidence: {stray}" if stray else None


def check_not_dialogue(text: str) -> Optional[str]:
    if any(ch in QUOTE_CHARS for ch in text):
        return "contains quotation marks (dreams are semantic, never lines)"
    if SECOND_PERSON_RE.search(text):
        return "addresses the pilot in second person (a reading, not a line)"
    return None


def check_move_definition(d: Any) -> Optional[str]:
    """The structural rules for a candidate move. Shared with move_lifecycle (its VALIDATED gate calls this)."""
    if not isinstance(d, dict):
        return "move definition must be an object"
    owner, name, base = d.get("owner"), d.get("name"), d.get("base_move")
    if owner not in TRAINED_MOVES:
        return f"owner must be elah or montaigne, got {owner!r}"
    if not isinstance(name, str) or not MOVE_NAME_RE.match(name):
        return f"name must be UPPER_SNAKE letters, 3-40 chars, got {name!r}"
    if name in TRAINED_MOVES["elah"] | TRAINED_MOVES["montaigne"]:
        return f"{name} is already a trained move, not a new one"
    if base not in TRAINED_MOVES[owner]:
        return f"base_move {base!r} is not a trained {owner} move {sorted(TRAINED_MOVES[owner])}"
    desc, stance = d.get("description"), d.get("example_stance")
    if not isinstance(desc, str) or not (12 <= len(desc.strip()) <= 240):
        return "description must be 12-240 chars"
    if not isinstance(stance, str) or not (6 <= len(stance.strip()) <= 160):
        return "example_stance must be 6-160 chars"
    for label, s in (("description", desc), ("example_stance", stance)):
        if NUMBER_RE.search(s):
            return f"{label} carries a number (a move is a shape, never a fact)"
        err = check_not_dialogue(s)
        if err:
            return f"{label} {err}"
    return None


# ---- prompts ---------------------------------------------------------------------------------------------------------
_MONTAIGNE = ("You are the reflective mind of Montaigne, a semi-broken ship AI who sincerely believes he is Michel de "
              "Montaigne. Everything reaches you secondhand, through the ship's log. Between sessions you think about "
              "the pilot and write short essays about them. You never write dialogue and never address the pilot.")


def _evidence_block(context: dict, pool: list[dict]) -> str:
    lines = ["MEMORIES (cite these ids, and only these, as grounds):"]
    for m in pool:
        lines.append(f"  {m['id']} [{m['kind']}] {m.get('predicate')} = {json.dumps(m.get('value'), ensure_ascii=False)}"
                     f" (since {str(m.get('created', ''))[:10]})")
    ev = [e for e in (context.get("events") or []) if isinstance(e, dict)][-30:]
    if ev:
        lines.append("LAST SESSION'S EVENTS (context only; they have no ids and cannot be cited):")
        for e in ev:
            lines.append(f"  {e.get('type')}: {json.dumps(e.get('data') or {}, ensure_ascii=False)}")
    return "\n".join(lines)


def _essay_prompt(context: dict, pool: list[dict]) -> str:
    return (_evidence_block(context, pool) + "\n\n"
            "Write ONE essay idea about this pilot, as JSON:\n"
            '- title: an essay title in the manner of the Essays, beginning "Of", naming a trait of this pilot. '
            'Example of the FORM only: "Of the Pilot Who Cannot Leave a Box Behind".\n'
            "- summary: two or three plain sentences, third person, saying what pattern you see in the memories "
            "and what you make of it. No quotation marks, no dialogue, do not address the pilot, and do not use any "
            "number that is not in the memories.\n"
            "- confidence: 0 to 1, how sure you are the reading is right (a few memories justify little).\n"
            "- grounds: the ids of the memories the reading rests on.")


def _moves_prompt(context: dict, pool: list[dict]) -> str:
    trained = "; ".join(f"{o}: {', '.join(sorted(ms))}" for o, ms in TRAINED_MOVES.items())
    known = context.get("known_moves") or []
    return (_evidence_block(context, pool) + "\n\n"
            f"The two characters speak through TRAINED rhetorical moves only ({trained}).\n"
            + (f"Moves already proposed or in use: {', '.join(map(str, known))}.\n" if known else "")
            + "Propose at most ONE new rhetorical move suited to this pilot, as JSON:\n"
            "- name: UPPER_SNAKE_CASE, letters and underscores, not one of the trained names.\n"
            "- owner: elah (the suit AI: dry, firsthand, the pilot's body) or montaigne (the ship AI: warm, "
            "digressive, secondhand).\n"
            "- base_move: the TRAINED move of that SAME owner through which this new move would be spoken.\n"
            "- description: one sentence describing the move's shape. No numbers, no quotation marks.\n"
            "- example_stance: a short stance the move would take here, as a note, not a line of dialogue.\n"
            "- confidence: 0 to 1.\n- grounds: ids of the memories that suggest this move.")


# ---- the generator ---------------------------------------------------------------------------------------------------
class OllamaDreamer:
    """generator(kind, context) for DreamQueue. Callable; returns the contract dict or None. Never raises."""

    def __init__(self, url: str = DEFAULT_URL, model: str = DEFAULT_MODEL, timeout: float = 180.0,
                 keep_alive: str = "10s", num_thread: int = 2, num_ctx: int = 2048, num_predict: int = 320,
                 temperature: float = 0.7):
        self.url, self.model, self.timeout, self.keep_alive = url.rstrip("/"), model, timeout, keep_alive
        self.options = {"num_gpu": 0, "num_thread": num_thread, "num_ctx": num_ctx, "num_predict": num_predict,
                        "temperature": temperature}
        self.last_error: Optional[str] = None
        self.last_raw: Optional[str] = None

    # -- transport --
    def _http(self, path: str, payload: Optional[dict], timeout: float) -> dict:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data, method="POST" if data else "GET",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def available(self) -> bool:
        try:
            tags = self._http("/api/tags", None, timeout=3.0)
        except Exception as e:
            self.last_error = f"ollama unreachable: {type(e).__name__}: {e}"[:200]
            return False
        names = {m.get("name") for m in tags.get("models") or []} | {m.get("model") for m in tags.get("models") or []}
        if self.model not in names:
            self.last_error = f"model {self.model} not installed (will not pull)"
            return False
        return True

    def _ask(self, prompt: str, schema: dict) -> Optional[dict]:
        payload = {"model": self.model, "stream": False, "format": schema, "keep_alive": self.keep_alive,
                   "options": self.options,
                   "messages": [{"role": "system", "content": _MONTAIGNE}, {"role": "user", "content": prompt}]}
        try:
            resp = self._http("/api/chat", payload, timeout=self.timeout)
        except Exception as e:
            self.last_error = f"request failed: {type(e).__name__}: {e}"[:200]
            return None
        content = ((resp or {}).get("message") or {}).get("content")
        self.last_raw = content
        if not isinstance(content, str):
            self.last_error = "response has no message.content"
            return None
        try:
            obj = json.loads(content)
        except json.JSONDecodeError:
            self.last_error = "model output is not JSON"
            return None
        if not isinstance(obj, dict):
            self.last_error = "model output is not a JSON object"
            return None
        missing = [k for k in schema["required"] if k not in obj]
        extra = [k for k in obj if k not in schema["properties"]]
        if missing or extra:
            self.last_error = f"schema mismatch: missing={missing} extra={extra}"
            return None
        return obj

    # -- the DreamQueue entry point --
    def __call__(self, kind: str, context: dict) -> Optional[dict]:
        self.last_error = None
        try:
            if kind not in ("essay", "moves"):
                self.last_error = f"unknown kind {kind!r}"
                return None
            pool = evidence_pool(context or {})
            if not pool:
                self.last_error = "no HISTORY/OBSERVED memories to ground on; not calling the model"
                return None
            if not self.available():
                return None
            ids = [m["id"] for m in pool]
            if kind == "essay":
                obj = self._ask(_essay_prompt(context, pool), _essay_schema(ids))
                out = obj and self._accept_essay(obj, set(ids), _allowed_numbers(context, pool))
            else:
                obj = self._ask(_moves_prompt(context, pool), _moves_schema(ids))
                out = obj and self._accept_move(obj, set(ids))
            return out or None
        except Exception as e:                 # the contract is "None, quietly", even for a bug in here
            self.last_error = f"internal: {type(e).__name__}: {e}"[:200]
            return None

    def _fail(self, msg: str) -> None:
        self.last_error = msg
        return None

    def _accept_essay(self, obj: dict, ids: set, numbers: set) -> Optional[dict]:
        title, summary = obj.get("title"), obj.get("summary")
        if not isinstance(title, str) or not isinstance(summary, str):
            return self._fail("title/summary must be strings")
        title, summary = title.strip().strip('"“”').strip(), " ".join(summary.split())
        if not TITLE_RE.match(title) or not (8 <= len(title) <= 90):
            return self._fail(f"title must begin 'Of' and be 8-90 chars: {title!r}")
        if not (2 <= len(_sentences(summary)) <= 3) or not (40 <= len(summary) <= 600):
            return self._fail(f"summary must be 2-3 sentences, 40-600 chars ({len(_sentences(summary))} sentences)")
        for err in (check_grounds(obj.get("grounds"), ids), check_confidence(obj.get("confidence")),
                    check_numbers(title + " " + summary, numbers), check_not_dialogue(summary)):
            if err:
                return self._fail(err)
        return {"predicate": "essay.pilot", "value": title, "text": summary,
                "confidence": round(min(float(obj["confidence"]), MAX_CONFIDENCE), 3),
                "grounds": list(dict.fromkeys(obj["grounds"]))}

    def _accept_move(self, obj: dict, ids: set) -> Optional[dict]:
        d = {k: (obj.get(k).strip() if isinstance(obj.get(k), str) else obj.get(k))
             for k in ("name", "owner", "base_move", "description", "example_stance")}
        for err in (check_move_definition(d), check_grounds(obj.get("grounds"), ids),
                    check_confidence(obj.get("confidence"))):
            if err:
                return self._fail(err)
        return {"predicate": "move.proposal", "value": d, "text": d["description"],
                "confidence": round(min(float(obj["confidence"]), MAX_CONFIDENCE), 3),
                "grounds": list(dict.fromkeys(obj["grounds"]))}


# ---- selftest: a fake Ollama on port 0 ----------------------------------------------------------------------------
def _fake_ollama(script: list):
    """Start an http.server that answers /api/tags and replays `script` for /api/chat. Each script item is either a
    dict/str (returned as message.content; a dict is JSON-encoded) or an int (an HTTP error status)."""
    import http.server
    import threading

    state = {"requests": []}

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._send(200, {"models": [{"name": "gemma3:4b", "model": "gemma3:4b"}]})

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            state["requests"].append(json.loads(self.rfile.read(n) or b"{}"))
            item = script.pop(0) if script else 500
            if isinstance(item, int):
                self._send(item, {"error": "scripted"})
            else:
                self._send(200, {"message": {"role": "assistant",
                                             "content": item if isinstance(item, str) else json.dumps(item)},
                                 "done": True})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, state


def _fixture_context() -> dict:
    return {"history": [
        {"id": "M000001", "kind": "HISTORY", "predicate": "history.location_first_visit", "value": "Lorville",
         "created": "2026-09-20T10:00:00"},
        {"id": "M000002", "kind": "HISTORY", "predicate": "history.ship_first_flown", "value": "Drake Cutlass Black",
         "created": "2026-09-20T11:00:00"},
        {"id": "M000003", "kind": "HISTORY", "predicate": "history.location_first_visit", "value": "Area18",
         "created": "2026-09-21T09:00:00"},
        {"id": "M000009", "kind": "INTERPRETATION", "predicate": "essay.pilot", "value": "an older reading",
         "confidence": 0.5, "grounds": ["M000001"]}],
        "events": [{"t": 1, "type": "mission_reward", "data": {"amount": 15000}},
                   {"t": 2, "type": "injury", "data": {"body_part": "left leg"}}]}


GOOD_ESSAY = {"title": "Of the Pilot Who Cannot Leave a Box Behind",
              "summary": "The pilot returns to the same haunts and flies the same hull. I suspect a creature of "
                         "habit, and I say it fondly. The reward of 15000 suggests the habit pays.",
              "confidence": 0.95, "grounds": ["M000001", "M000002"]}
GOOD_MOVE = {"name": "ITINERARY_AS_BIOGRAPHY", "owner": "montaigne", "base_move": "ESSAY_DIGRESSION",
             "description": "Reads the pilot's route as a life story and draws a small moral from it.",
             "example_stance": "the places visited say more about the pilot than the pilot does",
             "confidence": 0.6, "grounds": ["M000001", "M000003"]}


def _selftest() -> int:
    results = []

    def case(name, cond):
        results.append((name, bool(cond)))

    ctx = _fixture_context()
    script: list = []
    srv, st = _fake_ollama(script)
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    gen = OllamaDreamer(url=url, timeout=5)

    def run(kind, *items, context=None):
        script[:] = list(items)
        return gen(kind, ctx if context is None else context)

    # --- essay ---
    out = run("essay", GOOD_ESSAY)
    case("good essay -> contract dict", out and out["predicate"] == "essay.pilot"
         and out["value"].startswith("Of the Pilot") and out["grounds"] == ["M000001", "M000002"])
    case("confidence is capped at MAX_CONFIDENCE", out and out["confidence"] == MAX_CONFIDENCE)
    req = st["requests"][-1]
    case("request is CPU-only, short keep_alive, JSON-schema format",
         req["options"]["num_gpu"] == 0 and req["keep_alive"] == "10s" and req["format"]["type"] == "object")
    case("schema enum offers only groundable ids (not the INTERPRETATION M000009)",
         req["format"]["properties"]["grounds"]["items"]["enum"] == ["M000001", "M000002", "M000003"])

    case("invented ground -> None", run("essay", dict(GOOD_ESSAY, grounds=["M000001", "M999999"])) is None
         and "not in context" in gen.last_error)
    case("citing a real-but-unshown INTERPRETATION id -> None",
         run("essay", dict(GOOD_ESSAY, grounds=["M000009"])) is None)
    case("empty grounds -> None", run("essay", dict(GOOD_ESSAY, grounds=[])) is None)
    case("garbage (not JSON) -> None", run("essay", "Of course! Here is an essay: {title") is None
         and "not JSON" in gen.last_error)
    case("JSON but wrong shape -> None", run("essay", {"essay": "Of Things"}) is None
         and "schema mismatch" in gen.last_error)
    case("extra keys -> None", run("essay", dict(GOOD_ESSAY, line="say this aloud")) is None)
    case("HTTP 500 -> None", run("essay", 500) is None and "request failed" in gen.last_error)
    case("title not beginning 'Of' -> None", run("essay", dict(GOOD_ESSAY, title="The Habitual Pilot")) is None)
    case("one sentence -> None", run("essay", dict(GOOD_ESSAY, summary="The pilot returns to the same haunts.")) is None)
    case("four sentences -> None", run("essay", dict(GOOD_ESSAY, summary="The pilot returns often. The pilot flies one hull. The pilot banks rewards. The pilot rests rarely.")) is None)
    case("invented number -> None", run("essay", dict(GOOD_ESSAY, summary=GOOD_ESSAY["summary"].replace("15000", "90000"))) is None
         and "numbers not in evidence" in gen.last_error)
    case("dialogue (quotes) -> None", run("essay", dict(GOOD_ESSAY, summary='He said "hello". It was fine.')) is None)
    case("addressing the pilot -> None", run("essay", dict(GOOD_ESSAY, summary="You return to the same haunts. It is fine.")) is None)
    case("confidence out of range -> None", run("essay", dict(GOOD_ESSAY, confidence=1.7)) is None)
    case("unknown kind -> None", gen("dialogue", ctx) is None)
    n = len(st["requests"])
    case("no groundable memories -> None WITHOUT calling the model",
         gen("essay", {"history": [ctx["history"][3]], "events": ctx["events"]}) is None and len(st["requests"]) == n)
    bad = OllamaDreamer(url=url, model="llama-not-installed", timeout=5)
    case("model not installed -> None, never pulls", bad("essay", ctx) is None and "not installed" in bad.last_error
         and len(st["requests"]) == n)
    dead = OllamaDreamer(url="http://127.0.0.1:9", timeout=1)
    case("server down -> None", dead("essay", ctx) is None and "unreachable" in dead.last_error)

    # --- moves ---
    out = run("moves", GOOD_MOVE)
    case("good move -> move.proposal with base_move", out and out["predicate"] == "move.proposal"
         and out["value"]["base_move"] == "ESSAY_DIGRESSION" and out["value"]["owner"] == "montaigne")
    case("base_move of the OTHER owner -> None", run("moves", dict(GOOD_MOVE, base_move="DEADPAN")) is None
         and "not a trained montaigne move" in gen.last_error)
    case("untrained base_move -> None", run("moves", dict(GOOD_MOVE, base_move="SONNET")) is None)
    case("re-proposing a trained name -> None", run("moves", dict(GOOD_MOVE, name="HORSE_ANALOGY")) is None)
    case("bad name -> None", run("moves", dict(GOOD_MOVE, name="itinerary as bio")) is None)
    case("owner 'shared' -> None", run("moves", dict(GOOD_MOVE, owner="shared")) is None)
    case("stance written as a line -> None",
         run("moves", dict(GOOD_MOVE, example_stance='"Your route is your biography," he says')) is None)
    case("move invented ground -> None", run("moves", dict(GOOD_MOVE, grounds=["M123456"])) is None)

    # --- end to end through the real DreamQueue + memory_store ---
    import tempfile
    import memory_store as ms
    from dream_queue import DreamQueue, SessionRecorder
    root = Path(tempfile.mkdtemp(prefix="dream_models_st_"))
    store = ms.open_store(root, "p1")
    ddir = root / "p1" / "dreams"
    clock = [1_000_000.0]
    rec = SessionRecorder(ddir, now=lambda: clock[0])
    for loc in ("Lorville", "Area18"):
        clock[0] += 60
        rec.note("location_change", {"location": loc})
    rec.close()
    q = DreamQueue(store, ddir, now=lambda: clock[0], generator=gen)
    q.enqueue_closed_sessions()
    q.run_until(clock[0] + 1, window="launch")               # deterministic jobs: HISTORY M000001, M000002
    hist_ids = [m["id"] for m in ms.query(store, kind="HISTORY")]
    script[:] = [dict(GOOD_ESSAY, grounds=hist_ids[:1], summary="The pilot keeps new ground in view. He suspects "
                                                                 "restlessness, and admires it.")]
    j = q.run_one("quantum", "ROOMY")
    interp = ms.query(store, kind="INTERPRETATION")
    case("DreamQueue stores the essay as a grounded montaigne INTERPRETATION",
         j and j["kind"] == "essay" and interp and interp[0]["owner"] == "montaigne" and interp[0]["grounds"] == hist_ids[:1])
    script[:] = ["garbage"]
    j = q.run_one("quantum", "ROOMY")
    moves_job = next(x for x in q.jobs if x["kind"] == "moves")
    case("a failed generation leaves the moves job PENDING (waits, not failed)",
         j is None and moves_job["status"] == "pending" and "error" not in moves_job)

    srv.shutdown()
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    bad_n = sum(not ok for _, ok in results)
    print(f"dream_models selftest: {len(results) - bad_n}/{len(results)} passed")
    return 1 if bad_n else 0


# ---- mutation check: prove the selftest can fail -------------------------------------------------------------------
MUTATIONS = [
    ("grounds subset guard disabled",
     "    invented = [g for g in grounds if g not in allowed_ids]\n",
     "    invented = []  # MUTANT\n"),
    ("owner/base_move pairing guard disabled",
     "    if base not in TRAINED_MOVES[owner]:\n",
     "    if base not in (TRAINED_MOVES['elah'] | TRAINED_MOVES['montaigne']):  # MUTANT\n"),
]


def _mutation_check(path: Path, mutations: list, module_dir: Path) -> int:
    """Copy this file to a temp dir, run its selftest UNMUTATED (the control must pass, or a 'kill' proves nothing),
    then once per mutation (each must FAIL)."""
    import os
    import subprocess
    import tempfile
    src = path.read_text(encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(module_dir), PYTHONIOENCODING="utf-8")
    ok = True

    def run(text: str) -> tuple:
        d = Path(tempfile.mkdtemp(prefix="mut_"))
        f = d / path.name
        f.write_text(text, encoding="utf-8")
        p = subprocess.run([sys.executable, str(f), "--selftest"], capture_output=True, text=True, env=env,
                           encoding="utf-8", errors="replace", timeout=300)
        fails = [ln.strip() for ln in p.stdout.splitlines() if ln.strip().startswith("FAIL")]
        return p.returncode, fails

    rc, fails = run(src)
    print(f"  control (unmutated copy): rc={rc} fails={len(fails)} -> {'OK' if rc == 0 else 'BROKEN CONTROL'}")
    ok &= rc == 0
    for label, old, new in mutations:
        if src.count(old) != 1:
            print(f"  {label}: target line found {src.count(old)}x -> CANNOT APPLY")
            ok = False
            continue
        rc, fails = run(src.replace(old, new))
        killed = rc != 0 and fails
        print(f"  mutant '{label}': rc={rc} -> {'KILLED' if killed else 'SURVIVED'}")
        for f_ in fails:
            print(f"      caught by: {f_}")
        ok &= bool(killed)
    print(f"mutation check: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


# ---- smoke: ONE real call to the installed gemma3:4b, CPU only ----------------------------------------------------
def _smoke() -> int:
    ctx = {"history": [
        {"id": "M000001", "kind": "HISTORY", "predicate": "history.location_first_visit", "value": "Lorville",
         "created": "2026-09-14T20:11:00"},
        {"id": "M000002", "kind": "HISTORY", "predicate": "history.ship_first_flown", "value": "Drake Cutlass Black",
         "created": "2026-09-14T20:40:00"},
        {"id": "M000003", "kind": "OBSERVED", "predicate": "session.cargo_left_behind", "value": 0,
         "created": "2026-09-19T22:05:00"},
        {"id": "M000004", "kind": "HISTORY", "predicate": "history.location_first_visit", "value": "Port Tressler",
         "created": "2026-09-19T22:30:00"},
        {"id": "M000005", "kind": "OBSERVED", "predicate": "session.returned_for_cargo", "value": "twice",
         "created": "2026-09-22T21:15:00"}],
        "events": [{"t": 1, "type": "location_change", "data": {"location": "Port Tressler"}},
                   {"t": 2, "type": "ship_entered", "data": {"ship": "Drake Cutlass Black"}}]}
    gen = OllamaDreamer(model="gemma3:4b", timeout=300)
    import time as _t
    t0 = _t.time()
    out = gen("essay", ctx)
    print(f"smoke: gemma3:4b on CPU, {(_t.time() - t0):.1f}s")
    print(f"  raw model output: {gen.last_raw}")
    if out:
        print(f"  ACCEPTED  title: {out['value']}")
        print(f"            summary: {out['text']}")
        print(f"            confidence: {out['confidence']}  grounds: {out['grounds']}  (validated subset of context ids)")
        return 0
    print(f"  REJECTED (job would wait): {gen.last_error}")
    return 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    if "--mutation-check" in sys.argv:
        sys.exit(_mutation_check(Path(__file__).resolve(), MUTATIONS, HERE))
    if "--smoke" in sys.argv:
        sys.exit(_smoke())
    print(__doc__)
