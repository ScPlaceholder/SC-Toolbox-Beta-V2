"""Assistant selftest: every tool once, headlessly, with a real question.

Run from tools/Assistant with the toolbox's Python:

    python -m assistant.selftest            # everything
    python -m assistant.selftest --no-live  # skip the tool calls (offline)

Verdicts:
    PASS         the tool answered and the answer has what the question needs
    EMPTY-VALID  the tool answered correctly that nothing matched; NOT a pass
    FAIL         error, crash, timeout, or an answer missing what was asked
    SKIP         deliberately not executed (it would change the user's screen)

Also checked: the agent's tool-call/result pairing with a scripted model
(no duplicate results, no orphans, "no" still produces a result), the
yes/no parser, ipc_bus liveness against a real process, and that no tool
wrote into the toolbox or ~/.sctoolbox while answering.

Runs one worker at a time (memory is tight on the dev box) and loads no
ML model. Exit code: 0 when nothing FAILed, 1 otherwise.
"""
from __future__ import annotations

import collections
import json
import os
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ASSISTANT_DIR = os.path.dirname(_HERE)
ROOT = os.path.normpath(os.path.join(_ASSISTANT_DIR, "..", ".."))
for p in (ROOT, _ASSISTANT_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

try:
    sys.stdout.reconfigure(encoding="utf-8")
except (AttributeError, ValueError):
    pass

from assistant import agent as agent_mod                    # noqa: E402
from assistant import headless, ipc_bus, worker_pool        # noqa: E402
from assistant.builtin_tools import build_default_registry  # noqa: E402
from assistant.logic import classify_confirmation           # noqa: E402
from assistant.providers import _to_anthropic, _to_openai_wire  # noqa: E402
from assistant.tools import ToolContext, ToolError          # noqa: E402

_counts: "collections.Counter" = collections.Counter()


def _say(verdict: str, label: str, detail: str = "") -> None:
    _counts[verdict] += 1
    print(f"[{verdict:11s}] {label}")
    if detail:
        for line in detail.splitlines():
            print("              " + line)


def _short(obj, n: int = 700) -> str:
    s = json.dumps(obj, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + " ..."


# ── 1. every tool, one real question each ─────────────────────────────────

def _nonempty(key):
    return lambda r: bool(r.get(key))


CASES = [
    # (label, tool, args, check(result) -> bool, what the check means)
    ("J: what's the cargo size of a Caterpillar", "ship_info", {"name": "Caterpillar"},
     lambda r: r.get("scu") == 576, "scu == 576"),
    ("J: best trade route for a Caterpillar", "find_trade_routes",
     {"ship": "Caterpillar", "top_n": 3}, _nonempty("routes"), "routes listed"),
    ("trade route for Gold in Stanton, Caterpillar", "find_trade_routes",
     {"ship": "Caterpillar", "commodity": "gold", "system": "Stanton", "top_n": 2},
     _nonempty("routes"), "routes listed"),
    ("where do I buy a P4-AR", "find_item_price", {"item": "P4-AR", "top_n": 3},
     _nonempty("cheapest_buy"), "shops listed"),
    ("where can I buy or rent a Cutlass Black", "ship_buy_rent", {"ship": "Cutlass Black"},
     lambda r: bool(r.get("buy") or r.get("rent_per_day")), "buy or rent listed"),
    ("J: what mission gives the Trawler Scraper Module blueprint", "missions_for_blueprint",
     {"name": "Trawler Scraper Module"}, _nonempty("missions"), "missions listed"),
    ("J: where do I find Quantanium", "where_to_mine", {"resource": "Quantanium"},
     lambda r: str(r.get("resource", "")).startswith("Quantainium") and bool(r.get("locations")),
     "resolved to Quantainium, locations listed"),
    ("bounty missions for Headhunters in Pyro", "search_missions",
     {"faction": "headhunters", "system": "Pyro"}, _nonempty("missions"), "missions listed"),
    ("what do I need to craft a P4-AR", "blueprint_recipe", {"name": "P4-AR"},
     lambda r: bool(r.get("ingredients")) and str(r.get("game_version", "")).startswith("LIVE-4.10"),
     "ingredients listed, LIVE 4.10 data"),
    ("my scanner says 8620", "identify_signal", {"value": "8620"},
     _nonempty("matches"), "resource candidates"),
    # expected EMPTY-VALID: a correct "nothing matches" must not read as PASS
    ("my scanner says 123 (no such signal)", "identify_signal", {"value": "123"},
     _nonempty("matches"), "resource candidates"),
    ("Prospector with a Helix 1, Surge and Focus 3, Sabir", "mining_loadout_stats",
     {"ship": "prospector", "laser": "helix 1", "modules": ["surge", "focus 3"], "gadget": "sabir"},
     lambda r: r.get("laser") == "Helix I Mining Laser" and len(r.get("modules") or []) == 2,
     "Helix I + 2 modules resolved"),
    ("cargo layout for a C2 Hercules", "cargo_layout", {"ship": "C2 Hercules"},
     _nonempty("largest_first_layout"), "container counts"),
    ("J: jump route from Stanton to Pyro", "jump_route",
     {"from_system": "Stanton", "to_system": "Pyro"},
     lambda r: (r.get("route") or [None])[0] == "Stanton" and (r.get("route") or [None])[-1] == "Pyro",
     "Stanton ... Pyro"),
    ("J: what's in my loadout", "current_loadout", {}, _nonempty("weapons"), "weapons listed"),
    ("how many hours have I played", "playtime_summary", {},
     lambda r: (r.get("total_hours") or 0) > 0, "total_hours > 0"),
]


def _snapshot(paths: list) -> dict:
    snap = {}
    for base in paths:
        if os.path.isfile(base):
            st = os.stat(base)
            snap[base] = (st.st_mtime_ns, st.st_size)
            continue
        for d, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in ("__pycache__", ".git", ".claude",
                                                   ".pytest_cache", "node_modules")]
            for f in files:
                p = os.path.join(d, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                snap[p] = (st.st_mtime_ns, st.st_size)
    return snap


def run_tools(reg, ctx) -> dict:
    print("\n== 1. tools, one real question each (one worker alive at a time)")
    pool = worker_pool.get_pool(ROOT)
    pool.max_live = 1
    watched = [os.path.join(ROOT, f) for f, _ in worker_pool.TOOLS.values()]
    watched.append(os.path.join(os.path.expanduser("~"), ".sctoolbox"))
    before = _snapshot(watched)
    results = {}
    for label, name, args, check, means in CASES:
        t0 = time.perf_counter()
        try:
            r = reg.get(name).run(ctx, args)
        except ToolError as exc:
            _say("FAIL", f"{name}({_short(args, 200)})  <- {label}",
                 f"error: {exc}  [{time.perf_counter() - t0:.1f}s]")
            continue
        dt = time.perf_counter() - t0
        results.setdefault(name, r)
        head = f"{name}({_short(args, 200)})  <- {label}  [{dt:.1f}s]"
        if isinstance(r, dict) and r.get("empty"):
            _say("EMPTY-VALID", head, _short(r))
        elif isinstance(r, dict) and check(r):
            _say("PASS", head, f"check: {means}\n{_short(r)}")
        else:
            _say("FAIL", head, f"check failed: {means}\n{_short(r)}")
    pool.shutdown()

    # the side-effecting tools: exercised up to (not including) the screen
    print("\n== 1b. action tools (dry run: nothing is opened or pinned)")
    route = ((results.get("find_trade_routes") or {}).get("routes") or [None])[0]
    if route:
        d = headless.route_popup_data(route, ship="Caterpillar", ship_scu=576)
        ok = d["commodity"] == route["commodity"] and d["eff_scu"] == route["effective_scu"]
        _say("PASS" if ok else "FAIL", "show_route_popup: route_popup_data from a live route",
             _short(d, 400))
    else:
        _say("FAIL", "show_route_popup: no live route to shape (trade tool failed)")
    _say("SKIP", "show_route_popup / open_trade_hub: would open Trade Hub on screen")
    try:
        picks = {q: headless.resolve_skill(ROOT, q)["id"]
                 for q in ("mining signals", "trade hub", "craft", "mission db", "star map")}
        want = {"mining signals": "mining_signals", "trade hub": "trade", "craft": "craft_db",
                "mission db": "missions", "star map": "starmap"}
        n_skills = len(headless.list_skills(ROOT))
        lf = ipc_bus.launcher_cmd_file()
        _say("PASS" if picks == want else "FAIL",
             f"launch_tool: name -> skill id for {n_skills} discovered skills (NOT sent)",
             f"{picks}\nlauncher command file: {lf or 'none (launcher not running under WingmanAI)'}")
    except ToolError as exc:
        _say("FAIL", "launch_tool: resolve", str(exc))

    after = _snapshot(watched)
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    if changed:
        _say("FAIL", f"no-writes guard: {len(changed)} file(s) changed during the tool run",
             "\n".join(changed[:20]) + "\n(another running toolbox process can also cause this)")
    else:
        _say("PASS", f"no-writes guard: {len(before)} files in the 10 tool folders "
                     "and ~/.sctoolbox unchanged")
    return results


# ── 2. ipc_bus liveness ───────────────────────────────────────────────────

def run_ipc() -> None:
    print("\n== 2. ipc_bus.is_running keys on a live process, not the file's PID")
    sid = "zzselftest"
    path = os.path.join(tempfile.gettempdir(), f"sc_toolbox_{sid}_{os.getpid()}_1.jsonl")
    open(path, "w").close()
    child = None
    try:
        ipc_bus.live_cmd_files(force=True)
        r1 = ipc_bus.is_running(sid)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", path],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        ipc_bus.live_cmd_files(force=True)
        r2 = ipc_bus.is_running(sid)
        child.kill()
        child.wait(5)
        ipc_bus.live_cmd_files(force=True)
        r3 = ipc_bus.is_running(sid)
        ok = (r1, r2, r3) == (False, True, False)
        _say("PASS" if ok else "FAIL",
             "file named with OUR pid but no reader -> not running; reader alive -> "
             "running; reader killed -> not running",
             f"got {(r1, r2, r3)}, want (False, True, False)")
        # exact id: 'mining' must not see 'mining_signals'
        p2 = os.path.join(tempfile.gettempdir(), f"sc_toolbox_{sid}_x_{os.getpid()}_1.jsonl")
        open(p2, "w").close()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", p2],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
        time.sleep(0.5)
        ipc_bus.live_cmd_files(force=True)
        r4 = ipc_bus.is_running(sid)
        _say("PASS" if r4 is False else "FAIL",
             f"skill id is exact: '{sid}' does not match a live '{sid}_x' file", f"got {r4}")
        child.kill()
        child.wait(5)
        os.remove(p2)
    finally:
        if child and child.poll() is None:
            child.kill()
        for p in (path, path + ".lock"):
            if os.path.exists(p):
                os.remove(p)


# ── 3. scripted-model conversations ───────────────────────────────────────

class ScriptedModel:
    def __init__(self, script):
        self.script = collections.deque(script)
        self.calls = 0

    def chat(self, messages, tools):
        self.calls += 1
        return self.script.popleft() if self.script else ("(done)", [])


def wire_problems(messages: list) -> list:
    """Every tool_call answered exactly once, before the next user/assistant
    turn; every tool result answers a call. Checked on the OpenAI wire and
    on the Anthropic conversion."""
    probs, open_ids, answered = [], set(), set()
    for i, m in enumerate(_to_openai_wire(messages)):
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            if open_ids - answered:
                probs.append(f"msg{i}: new tool_calls while {sorted(open_ids - answered)} unanswered")
            open_ids, answered = {tc["id"] for tc in m["tool_calls"]}, set()
        elif role == "tool":
            tid = m.get("tool_call_id")
            if tid not in open_ids:
                probs.append(f"msg{i}: ORPHAN result for {tid!r}")
            elif tid in answered:
                probs.append(f"msg{i}: DUPLICATE result for {tid!r}")
            answered.add(tid)
        elif role in ("user", "assistant"):
            if open_ids - answered:
                probs.append(f"msg{i} ({role}): calls {sorted(open_ids - answered)} have NO result")
            open_ids, answered = set(), set()
    if open_ids - answered:
        probs.append(f"end: calls {sorted(open_ids - answered)} have NO result")
    body = _to_anthropic(messages, [])
    msgs = body["messages"]
    for i, m in enumerate(msgs):
        if m["role"] == "assistant":
            uses = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
            if uses:
                nxt = msgs[i + 1] if i + 1 < len(msgs) else {"content": []}
                got = [b.get("tool_use_id") for b in (nxt["content"] if isinstance(nxt["content"], list) else [])
                       if b.get("type") == "tool_result"]
                if sorted(got) != sorted(uses):
                    probs.append(f"anthropic msg{i}: tool_use {uses} answered by {got}")
    return probs


def run_conversations() -> None:
    print("\n== 3. scripted-model conversations (tool-call / result pairing)")
    reg = build_default_registry()
    opened = []
    reg.get("open_trade_hub").func = lambda ctx: (opened.append(1), {"opened": True, "STUB": True})[1]
    ctx = ToolContext(base_dir=ROOT)

    def convo(label, script, turns, expect):
        opened.clear()
        a = agent_mod.AssistantAgent(reg, ctx)
        m = ScriptedModel(script)
        a.provider = m
        replies, bad = [], []
        try:
            replies = [a.handle_user_text(t) for t in turns]
            bad.extend(wire_problems(a._messages))
            for what, want, got in expect(a, m, replies):
                if want != got:
                    bad.append(f"{what}: want {want!r}, got {got!r}")
        except Exception as exc:                            # noqa: BLE001
            bad.append(f"check raised {type(exc).__name__}: {exc}")
        roles = [x["role"] + ("+tc" if x.get("tool_calls") else "") for x in a._messages[1:]]
        _say("PASS" if not bad else "FAIL", label,
             f"turns={turns} replies={replies}\nhistory={roles}"
             + ("\n" + "\n".join(bad) if bad else ""))

    def results_for(a, cid):
        return [x for x in a._messages if x.get("role") == "tool" and x.get("tool_call_id") == cid]

    oth = lambda cid: {"id": cid, "name": "open_trade_hub", "arguments": {}}

    convo("read tool, then answer",
          [("", [{"id": "c1", "name": "ship_info", "arguments": {"name": "Caterpillar"}}]),
           ("576 SCU.", [])],
          ["how much does a cat hold"],
          lambda a, m, r: [("reply", "576 SCU.", r[0])])

    convo("unknown tool name gets an error result (no orphan)",
          [("", [{"id": "c1", "name": "launch_mining_signals", "arguments": {}}]),
           ("I can't do that one.", [])],
          ["open mining signals"],
          lambda a, m, r: [("results for c1", 1, len(results_for(a, "c1"))),
                           ("result is an error", True, "unknown tool" in results_for(a, "c1")[0]["content"])])

    convo("confirm tool + 'yes' -> runs once, exactly one result",
          [("", [oth("c9")]), ("Trade Hub is open.", [])],
          ["open trade hub", "yes"],
          lambda a, m, r: [("executed", 1, len(opened)),
                           ("results for c9", 1, len(results_for(a, "c9"))),
                           ("question asked", True, r[0].endswith("Say yes or no.")),
                           ("closing reply", "Trade Hub is open.", r[1])])

    convo("confirm tool + 'no' -> not run, still gets a result, generic reply",
          [("", [oth("c7")])],
          ["open trade hub", "no"],
          lambda a, m, r: [("executed", 0, len(opened)),
                           ("results for c7", 1, len(results_for(a, "c7"))),
                           ("declined recorded", True, '"declined": true' in results_for(a, "c7")[0]["content"]),
                           ("reply", "Okay, I won't open Trade Hub.", r[1]),
                           ("model not called for the 'no'", 1, m.calls)])

    convo("confirm tool + unrelated reply -> cancelled result, new request handled",
          [("", [oth("c5")]), ("Hull C holds the most.", [])],
          ["open trade hub", "actually what's the best ship for cargo"],
          lambda a, m, r: [("executed", 0, len(opened)),
                           ("results for c5", 1, len(results_for(a, "c5"))),
                           ("cancel recorded", True, '"cancelled": true' in results_for(a, "c5")[0]["content"]),
                           ("reply", "Hull C holds the most.", r[1])])

    convo("confirm tool + 'okay no' -> treated as no",
          [("", [oth("c4")])],
          ["open trade hub", "okay no"],
          lambda a, m, r: [("executed", 0, len(opened)),
                           ("results for c4", 1, len(results_for(a, "c4")))])

    convo("confirm tool + 'yes but not now' -> not run",
          [("", [oth("c3")])],
          ["open trade hub", "yes but not now"],
          lambda a, m, r: [("executed", 0, len(opened)),
                           ("results for c3", 1, len(results_for(a, "c3")))])

    convo("parallel calls: two reads + one confirm in one turn, then 'go for it'",
          [("", [{"id": "a", "name": "ship_info", "arguments": {"name": "Hull C"}},
                 {"id": "b", "name": "ship_info", "arguments": {"name": "Caterpillar"}},
                 oth("c")]),
           ("Hull C 4608, Cat 576, Trade Hub open.", [])],
          ["compare hull c and cat and open trade hub", "go for it"],
          lambda a, m, r: [("executed", 1, len(opened)),
                           ("results a/b/c", [1, 1, 1],
                            [len(results_for(a, k)) for k in ("a", "b", "c")])])

    convo("two confirm calls in one turn: second refused with a result",
          [("", [oth("x1"), {"id": "x2", "name": "launch_tool", "arguments": {"name": "starmap"}}]),
           ("Opened Trade Hub; ask me again for the map.", [])],
          ["open trade hub and the star map", "y"],
          lambda a, m, r: [("executed", 1, len(opened)),
                           ("results x1/x2", [1, 1], [len(results_for(a, k)) for k in ("x1", "x2")])])


# ── 4. yes/no parser ──────────────────────────────────────────────────────

YESNO = [
    ("yes", "yes"), ("Yes please", "yes"), ("yeah do it", "yes"), ("ok", "yes"),
    ("sure", "yes"), ("y", "yes"), ("go for it", "yes"), ("please", "yes"),
    ("yep.", "yes"), ("okay, go ahead", "yes"),
    ("no", "no"), ("nope", "no"), ("not now", "no"), ("no, pin it", "no"),
    ("okay no", "no"), ("yes but not now", "no"), ("don't", "no"), ("cancel that", "no"),
    ("sure, what ship?", None), ("what's the best route", None), ("how much is it", None),
]


def run_yesno() -> None:
    print("\n== 4. yes/no parsing (yes / no / None = something else)")
    for text, want in YESNO:
        got = classify_confirmation(text)
        _say("PASS" if got == want else "FAIL", f"{text!r:24s} -> {got!r}"
             + ("" if got == want else f"  (want {want!r})"))


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    print("SC Toolbox Assistant selftest")
    print(f"toolbox: {ROOT}")
    print(f"worker python: {worker_pool.find_toolbox_python()}")
    reg = build_default_registry()
    print(f"registry: {len(reg.names())} tools: {', '.join(reg.names())}")
    ctx = ToolContext(base_dir=ROOT)
    if "--no-live" not in argv:
        run_tools(reg, ctx)
    run_ipc()
    run_conversations()
    run_yesno()
    worker_pool.shutdown_all()
    print("\n== SUMMARY " + "  ".join(f"{k}={_counts[k]}" for k in
                                      ("PASS", "EMPTY-VALID", "FAIL", "SKIP")))
    return 1 if _counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
