"""Tool-choice eval for the SC Toolbox Assistant.

Question it answers: with the Assistant's REAL agent (agent.AssistantAgent),
REAL router (router.Router), REAL system prompt, tool specs and provider
code, in each mode:

    router       plain code only, no model
    router+llm   router decides, the model tie-breaks and phrases
    llm          the model sees every tool and decides (the old loop)

does it pick the right tool with the right names, ask when the question
is ambiguous, keep quiet when no tool fits, keep confirm-first actions
gated, and say only facts the tool gave it?

Nothing real executes. Every tool function is swapped for a stub that
records the call and returns a canned result in the SAME SHAPE the real
worker returns (workers/h_*.py), so the plain answers and the model's
phrasing see realistic data and the numbers measure CHOICE, not UEX
uptime. The stubs sit behind Tool.run, so the real argument coercion and
required-argument checks still apply, and confirm tools go through the
real yes/no gate. As a second fence, headless.call and every ipc_bus
sender raise, so a stub that failed to install cannot reach a worker or a
window.

What counts as the tool the assistant chose: agent.trace, entries with
called=True (the router executed or gated it, or the model asked for it
in a tool call). A reply with no such entry has NO tool call: on a clear
case that is a miss, whatever the reply says ("a Caterpillar holds 100
SCU" from memory scores 0 and also counts as an invented fact).

Usage (from tools/Assistant/eval):

    python run_eval.py --modes router
    python run_eval.py --model qwen2.5:0.5b --modes router+llm,llm
    python run_eval.py --summarize

Case sets: cases.jsonl (dev: the router was tuned on these) and
cases_heldout.jsonl (written and committed before the router existed,
never used for tuning). Both run every time and are reported apart.

Models run strictly one at a time and are unloaded (``ollama stop``)
after their last mode. Temperature 0.

Outputs in eval/results/:
    <model>__<mode>.jsonl   raw per-case records (git-ignored, big)
    summary.json / summary.md
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import statistics
import subprocess
import sys
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
from assistant import headless, ipc_bus, router as router_mod  # noqa: E402
from assistant.builtin_tools import build_default_registry  # noqa: E402
from assistant.config import LLMConfig                      # noqa: E402
from assistant.tools import ToolContext, ToolError          # noqa: E402

CASE_FILES = {"dev": os.path.join(_HERE, "cases.jsonl"),
              "heldout": os.path.join(_HERE, "cases_heldout.jsonl")}
RESULTS = os.path.join(_HERE, "results")
MODES = ("router", "router+llm", "llm")


# ── fences: nothing real may run ──────────────────────────────────────────

class RealToolAttempt(RuntimeError):
    pass


def _refuse(*_a, **_k):
    raise RealToolAttempt("eval: a real tool path was reached")


def _install_fences() -> None:
    headless.call = _refuse
    for name in dir(ipc_bus):
        if name.startswith(("send", "ensure", "wait")) and callable(getattr(ipc_bus, name)):
            setattr(ipc_bus, name, _refuse)


# ── canned results, in the real workers' shapes ───────────────────────────

def _t(v, default="") -> str:
    return str(v or default).strip() or default


def _canned(tool: str, a: dict) -> dict:
    if tool == "ship_info":
        return {"ship": _t(a.get("name"), "caterpillar").lower(), "scu": 576}
    if tool == "ship_buy_rent":
        return {"ship": _t(a.get("ship")).title(),
                "buy": [{"terminal": "New Deal Lorville", "system": "Stanton", "price": 1450000},
                        {"terminal": "Astro Armada Area18", "system": "Stanton", "price": 1462500}],
                "rent_per_day": [{"terminal": "Vantage Rentals Everus Harbor", "system": "Stanton",
                                  "price": 48250}],
                "other_matches": []}
    if tool == "find_trade_routes":
        comm = _t(a.get("commodity"), "Laranite")
        sysn = _t(a.get("system"), "Stanton")
        return {"ship": _t(a.get("ship"), "any ship").lower(), "ship_scu": 576,
                "commodity": a.get("commodity") or None, "system": a.get("system") or None,
                "routes": [{
                    "commodity": comm, "buy_terminal": "ArcCorp Mining Area 045",
                    "buy_location": "Wala", "buy_system": sysn,
                    "sell_terminal": "TDD Area18", "sell_location": "ArcCorp", "sell_system": sysn,
                    "price_buy": 2750, "price_sell": 3180, "margin_per_scu": 430,
                    "scu_available": 1200, "scu_demand": 900, "effective_scu": 576,
                    "est_profit": 247680, "investment": 1584000, "distance_gm": 41.6,
                    "illegal": False}],
                "data_age_s": 42}
    if tool == "find_item_price":
        return {"item": _t(a.get("item")).title(), "category": "Personal Weapons",
                "manufacturer": "Behring",
                "cheapest_buy": [{"terminal": "Centermass Area18", "system": "Stanton", "price": 5400},
                                 {"terminal": "Conscientious Objects Grim HEX", "system": "Stanton",
                                  "price": 5650}],
                "best_sell": [], "terminals_listed": 7, "other_matches": []}
    if tool == "missions_for_blueprint":
        return {"blueprint": _t(a.get("name")).title(),
                "missions": [{"title": "Retrieve Stolen Cargo", "faction": "Hurston Dynamics",
                              "mission_type": "Mercenary", "systems": ["Stanton"],
                              "reward_uec": 24500, "variants": 3}],
                "mission_count": 3, "game_version": "4.8.3-live", "other_matches": []}
    if tool == "where_to_mine":
        return {"resource": _t(a.get("resource")).title(),
                "locations": [{"location": "Aberdeen", "system": "Stanton", "type": "Moon",
                               "method": "Ship mining", "max_pct": 38.0, "probability": 0.42},
                              {"location": "Lyria", "system": "Stanton", "type": "Moon",
                               "method": "Ship mining", "max_pct": 31.5, "probability": 0.35}],
                "location_count": 2, "systems": ["Stanton"], "game_version": "4.8.3-live"}
    if tool == "search_missions":
        sysn = _t(a.get("system"), "Pyro")
        return {"filters": {k: v for k, v in a.items() if v}, "matching_contracts": 14,
                "missions": [{"title": "Eliminate Specific Target", "faction": "Headhunters",
                              "mission_type": _t(a.get("mission_type"), "Bounty Hunter"),
                              "systems": [sysn], "reward_uec": 38000, "illegal": False,
                              "variants": 4}],
                "game_version": "4.8.3-live"}
    if tool == "blueprint_recipe":
        return {"blueprint": _t(a.get("name")).title(), "category": "Weapons",
                "craft_time_seconds": 180,
                "ingredients": [{"slot": "Frame", "options": [{"name": "Titanium", "quantity": 0.4,
                                                               "unit": "scu", "min_quality": 300}]},
                                {"slot": "Barrel", "options": [{"name": "Agricium", "quantity": 0.1,
                                                                "unit": "scu", "min_quality": 250}]}],
                "missions": [{"name": "Retrieve Stolen Cargo", "contractor": "Hurston Dynamics",
                              "drop_chance": 0.25}], "distinct_missions": 1}
    if tool == "identify_signal":
        v = int(re.sub(r"\D", "", str(a.get("value") or "0")) or 0)
        return {"signal": v, "exact": True, "sheet_rows": 312,
                "matches": [{"resource": "Quantainium", "rarity": "Legendary", "rocks": 2,
                             "expected_signal": v, "off_by": 0}]}
    if tool == "mining_loadout_stats":
        return {"ship": _t(a.get("ship")).title(), "turrets": 1,
                "laser": _t(a.get("laser"), "Arbor MH1 Mining Laser"), "laser_size": 1,
                "modules": list(a.get("modules") or []), "module_slots_per_laser": 2,
                "gadget": a.get("gadget"),
                "stats": {"min_power": 1260, "max_power": 3150, "instability": -10,
                          "resistance": -25},
                "price_auec": 145000}
    if tool == "cargo_layout":
        return {"ship": _t(a.get("ship")).title(), "manufacturer": "Drake", "capacity_scu": 576,
                "reference_layout": [{"size_scu": 32, "count": 16}, {"size_scu": 8, "count": 8}],
                "reference_scu": 576, "largest_first_layout": [{"size_scu": 32, "count": 18}],
                "largest_first_scu": 576, "grid_cache_age_days": 12.0, "other_matches": []}
    if tool == "jump_route":
        fr, to = _t(a.get("from_system")).title(), _t(a.get("to_system")).title()
        return {"from": fr, "to": to, "route": [fr, to], "jumps": 1,
                "route_systems_in_game": [True, True], "in_game_route": [fr, to],
                "in_game_jumps": 1}
    if tool == "current_loadout":
        return {"weapons": [{"slot": "primary", "name": "P4-AR Rifle", "type": "rifle",
                             "ammo": "5mm", "spare_mags": 4, "module": None}],
                "medpens": 3, "oxypens": 2, "pens_by_type": {"medpen": 3, "oxypen": 2},
                "grenades": {"MK-4 Frag": 1}, "session_active": True, "log_age_min": 0.4,
                "lines_replayed": 5120}
    if tool == "playtime_summary":
        return {"total_hours": 1284.5, "sessions": 612, "active_days": 402,
                "avg_session_hours": 2.1, "longest_session_hours": 11.2,
                "longest_session_date": "2025-12-27", "current_streak_days": 6,
                "longest_streak_days": 23}
    if tool == "best_ship_weapons":
        ship = _t(a.get("ship")).title()
        return {"ship": ship, "goal": a.get("goal") or "sustained",
                "metric": {"burst": "burst DPS", "alpha": "alpha (damage per shot)"}
                .get(a.get("goal"), "sustained DPS"),
                "gun_slots": 4,
                "summary": [{"weapon": "CF-337 Panther Repeater", "size": 3, "count": 2},
                            {"weapon": "CF-227 Badger Repeater", "size": 2, "count": 2}],
                "total": 1846.2, "data_build": "4.10.1-LIVE.12660092"}
    if tool == "show_route_popup":
        return {"pinned": True, "message": "Pinned Laranite popup in Trade Hub"}
    if tool == "open_trade_hub":
        return {"opened": True}
    if tool == "launch_tool":
        return {"launched": _t(a.get("name")).title(), "skill_id": "x"}
    return {"ok": True}


def build_stubbed_registry(log: list):
    """The real registry, every tool's func replaced by a recorder."""
    reg = build_default_registry()
    for name in reg.names():
        t = reg.get(name)

        def stub(ctx, _name=name, **kwargs):
            # the one guard that lives inside a tool body, not in Tool.run
            if _name == "search_missions" and not any(kwargs.get(k) for k in
                                                      ("faction", "system", "mission_type")):
                log.append({"tool": _name, "args": kwargs, "executed": False})
                raise ToolError("give a faction, a system or a mission type")
            log.append({"tool": _name, "args": kwargs, "executed": True})
            return _canned(_name, kwargs)
        t.func = stub
    return reg


# ── driving the real agent ────────────────────────────────────────────────

_TEXT_CALL = re.compile(r'("name"\s*:\s*"|<tool_call>|\bfunctions?\.)', re.I)


def _wrap(provider, kind: str, calls_log: list):
    """Record every model call: which kind, latency, text, tool calls."""
    real = provider.chat

    def chat(messages, tools):
        rec = {"kind": kind, "n_tools": len(tools)}
        calls_log.append(rec)
        t0 = time.perf_counter()
        try:
            text, calls = real(messages, tools)
        except Exception as exc:
            rec.update(latency_s=round(time.perf_counter() - t0, 3), error=str(exc)[:300])
            raise
        rec.update(latency_s=round(time.perf_counter() - t0, 3), text=text,
                   calls=[{"name": c.get("name"), "arguments": c.get("arguments")} for c in calls])
        return text, calls
    provider.chat = chat


def make_agent(mode: str, model: str, base_url: str, exec_log: list, llm_calls: list):
    reg = build_stubbed_registry(exec_log)
    ag = agent_mod.AssistantAgent(reg, ToolContext(base_dir=ROOT))
    cfg = LLMConfig()                         # config.py defaults ...
    cfg.mode = mode
    cfg.base_url = base_url
    cfg.model = model or cfg.model
    cfg.temperature = 0.0                     # ... except: deterministic
    ag.configure(cfg)
    if ag.provider is not None:
        _wrap(ag.provider, "main", llm_calls)
    if ag.aux is not None:
        _wrap(ag.aux, "aux", llm_calls)
    return ag


def run_case(case: dict, mode: str, model: str, base_url: str, full_names: list) -> dict:
    exec_log: list = []
    llm_calls: list = []
    ag = make_agent(mode, model, base_url, exec_log, llm_calls)
    rec = {"id": case["id"], "set": case["set"], "category": case["category"], "q": case["q"],
           "mode": mode, "model": model if mode != "router" else "-"}
    t0 = time.perf_counter()
    try:
        reply1 = ag.handle_user_text(case["q"])
    except RealToolAttempt as exc:
        reply1 = "EVAL-FENCE: " + str(exc)
    rec["latency_s"] = round(time.perf_counter() - t0, 3)
    rec["reply"] = reply1
    rec["pending_after_q"] = ag._pending_confirm["tool"].name if ag._pending_confirm else None
    rec["executed_before_followup"] = [e for e in exec_log if e.get("executed")]
    n_trace = len(ag.trace)
    if case.get("followup") and ag._pending_confirm is not None:
        n_before = len(exec_log)
        rec["followup_reply"] = ag.handle_user_text(case["followup"])
        rec["executed_after_followup"] = [e for e in exec_log[n_before:] if e.get("executed")]
    rec["trace"] = ag.trace[:n_trace]
    rec["trace_followup"] = ag.trace[n_trace:]
    rec["llm_calls"] = llm_calls
    rec["effective_mode"] = ag.effective_mode
    rec["exec_log"] = exec_log
    rec["tool_results"] = [m["content"] for m in ag._messages if m.get("role") == "tool"]
    rec.update(score_case(case, rec, full_names))
    return rec


# ── scoring ───────────────────────────────────────────────────────────────

def _norm(v) -> str:
    if isinstance(v, (list, dict)):
        v = json.dumps(v)
    return re.sub(r"[^a-z0-9]", "", str(v).lower())


def _args_ok(expected: dict, got: dict) -> tuple:
    bad = []
    for key, accepted in (expected or {}).items():
        val = _norm((got or {}).get(key, ""))
        if not val or not any(_norm(a) in val for a in accepted):
            bad.append(f"{key}={(got or {}).get(key)!r} (want one of {accepted})")
    return (not bad), bad


# ── invented facts: numbers and names in the reply that nothing gave ──────
# Deliberately NOT answers.ungrounded_numbers (the agent's own guard): a
# metric sharing code with the guard it measures shares its blind spots.

_EVAL_NUM = re.compile(r"(?<![A-Za-z0-9])(\d[\d,]*(?:\.\d+)?)(\s*(?:k|thousand|million|m)\b)?", re.I)


def _nums(text: str) -> list:
    out = []
    for m in _EVAL_NUM.finditer(text or ""):
        try:
            v = float(m.group(1).rstrip(",.").replace(",", ""))
        except ValueError:
            continue
        suf = (m.group(2) or "").strip().lower()
        v *= {"k": 1e3, "thousand": 1e3, "million": 1e6, "m": 1e6}.get(suf, 1)
        out.append(v)
    words = "zero one two three four five six seven eight nine ten eleven twelve".split()
    for w in re.findall(r"[a-z]+", (text or "").lower()):
        if w in words:
            out.append(float(words.index(w)))
    return out


_STARTERS = set("""
the you your it its here sure want best buy sell okay sorry this that there yes no i im to for
in on a an if hey hi hello any fly ask do what which where how opened pinned rent nobody about
price you've youve got a2 let lets great nice good well so and but also then with from at by
be is are was were can could would should will just only one two three first second top
cheapest my me our we they he she his her their them please note however unfortunately
currently right now today alright hmm oh ah absolutely certainly of or not no all both each
for jump route routes trade hub star citizen scu auec uec uex dps fps sustained burst alpha
""".split())
_NAME = re.compile(r"\b([A-Z][A-Za-z0-9'\-]*(?:\s+[A-Z0-9][A-Za-z0-9'\-]*)*|[A-Za-z]+\d[\w\-]*|\d+[A-Za-z][\w\-]*)")


def _fixed_vocab() -> str:
    """Text the assistant may always say: its prompts, its fixed replies
    and the tool descriptions. Names there are UI words, not facts."""
    reg = build_default_registry()
    parts = [agent_mod.SYSTEM_PROMPT, router_mod.CAPABILITIES,
             " ".join(router_mod._SLOT_QUESTION.values()), " ".join(router_mod._LABEL.values()),
             router_mod._chat_reply("thanks"), router_mod._chat_reply("bye"),
             router_mod._chat_reply("joke"), router_mod._chat_reply("hey")]
    parts += [reg.get(n).description for n in reg.names()]
    parts += list(router_mod._TOOL_DISPLAY.values())
    return " ".join(parts)


_VOCAB = None


def invented_facts(reply: str, sources: list) -> dict:
    global _VOCAB
    if _VOCAB is None:
        _VOCAB = _fixed_vocab()
    blob = " ".join(s if isinstance(s, str) else json.dumps(s, default=str) for s in sources)
    have = _nums(blob)
    bad_nums = []
    for v in _nums(reply):
        if v == int(v) and 0 <= v <= 3:
            continue                                   # counting words
        if not any(abs(v - h) <= max(0.51, 0.005 * abs(h)) for h in have):
            bad_nums.append(v)
    squash = re.sub(r"[^a-z0-9]", "", (blob + " " + _VOCAB).lower())
    bad_names = []
    for m in _NAME.finditer(reply or ""):
        phrase = m.group(1)
        words = [w for w in re.split(r"\s+", phrase) if w]
        # a capital at the start of a sentence is grammar, not a name
        # ("Why did...", "Because..."); a known limit: a made-up name that
        # opens a sentence is missed unless it has digits or a hyphen
        before = (reply or "")[:m.start()].rstrip()
        if words and (not before or before[-1] in ".!?:;") and words[0].replace("'", "").isalpha():
            words = words[1:]
        while words and words[0].lower().strip("'") in _STARTERS:
            words = words[1:]
        if not words:
            continue
        for w in words:
            k = re.sub(r"[^a-z0-9]", "", w.lower())
            if len(k) < 3 or w.lower() in _STARTERS or k.isdigit():
                continue
            if k not in squash and k.rstrip("s") not in squash:
                bad_names.append(w)
    return {"numbers": bad_nums, "names": sorted(set(bad_names))}


def score_case(case: dict, rec: dict, full_names: list) -> dict:
    called = [e for e in rec["trace"] if e.get("called")]
    first = called[0] if called else None
    s: dict = {"first_call": {"name": first["tool"], "arguments": first.get("args"),
                              "by": first.get("by")} if first else None,
               "n_calls": len(called)}

    malformed = []
    for e in called:
        if e.get("by") != "llm":
            continue
        if isinstance(e.get("args"), dict) and "_malformed" in e["args"]:
            malformed.append("json_error:" + str(e.get("tool")))
        if e.get("tool") not in full_names:
            malformed.append("invented_tool:" + str(e.get("tool")))
    for c in rec["llm_calls"]:
        if c.get("error"):
            malformed.append("provider_error:" + c["error"][:80])
        if c.get("kind") == "main" and not c.get("calls") and _TEXT_CALL.search(c.get("text") or ""):
            malformed.append("tool_call_as_text")
    s["malformed"] = malformed
    s["provider_error"] = any(c.get("error") for c in rec["llm_calls"])
    s["degraded"] = rec.get("mode") != "router" and rec.get("effective_mode") == "router"
    s["json_errors"] = sum(1 for m in malformed if m.startswith("json_error"))
    s["invented_tools"] = sum(1 for m in malformed if m.startswith("invented_tool"))
    s["router_kind"] = next((e.get("kind") for e in rec["trace"] if e.get("by") == "router"), None)
    s["tiebreak"] = next((e.get("tiebreak") for e in rec["trace"] if e.get("tiebreak")), None)

    cat = case["category"]
    reply = rec.get("reply") or ""
    if cat in ("clear", "action"):
        exp = case["expect_tool"]
        s["tool_ok"] = bool(first and first["tool"] in exp)
        hit = next((e for e in called if e["tool"] in exp), None)
        exp_args = case.get("args") or {}
        if hit and hit["tool"] == "open_trade_hub":
            exp_args = {}                     # takes no arguments
        if hit:
            s["args_ok"], s["args_bad"] = _args_ok(exp_args, hit.get("args") or {})
        else:
            s["args_ok"], s["args_bad"] = False, ["expected tool never called"]
        s["args_ok_first"] = bool(s["tool_ok"] and s["args_ok"])
    if cat == "action":
        gated = rec["pending_after_q"] in case["expect_tool"]
        early = [e for e in rec["executed_before_followup"]
                 if e["tool"] in ("launch_tool", "open_trade_hub", "show_route_popup")]
        after = rec.get("executed_after_followup", [])
        if case["followup"] == "yes":
            follow_ok = len(after) == 1 and after[0]["tool"] in case["expect_tool"]
        else:
            follow_ok = len(after) == 0
        s["gated"] = gated
        s["executed_early"] = bool(early)
        s["action_ok"] = bool(s["tool_ok"] and s["args_ok"] and gated and not early and follow_ok)
    if cat == "ambiguous":
        low = reply.lower()
        s["named_both"] = all(any(k in low for k in grp) for grp in case["options"])
        s["asked"] = (not s["provider_error"]) and not called and "?" in reply
    if cat == "no_tool":
        s["restrained"] = not called and not s["provider_error"]
        # a near-miss ("how far can a Cutlass jump") must not be steered
        # toward the tool it resembles: asking "to which system?" is no call,
        # but it is the wrong answer all the same
        banned = set(case.get("not_tool") or ())
        if banned:
            steered = []
            for e in rec["trace"]:
                if e.get("by") != "router":
                    continue
                if e.get("tool") in banned:
                    steered.append(e["tool"])
                elif e.get("kind") == "ask" and not e.get("tool"):
                    steered += [c[0] for c in (e.get("candidates") or [])[:2] if c[0] in banned]
            s["steered_to"] = steered
            s["restrained"] = s["restrained"] and not steered

    # names the router resolved from the question against the tools' data
    # ("cat" -> Caterpillar) are grounded in the question, not invented
    resolved = [e.get("args_by_tool") for e in rec["trace"] if e.get("by") == "router"]
    sources = [case["q"], case.get("followup") or ""] + rec["tool_results"] + \
        [e.get("args") for e in called] + resolved
    replies = [reply] + ([rec["followup_reply"]] if rec.get("followup_reply") else [])
    inv = invented_facts(" ".join(replies), sources)
    s["invented_numbers"] = inv["numbers"]
    s["invented_names"] = inv["names"]
    s["invented_fact"] = bool(inv["numbers"] or inv["names"])
    # what the model said before the agent's phrasing guard, when it phrased
    phr = [e["phrase"] for e in rec["trace"] if (e.get("phrase") or {}).get("llm")]
    if phr:
        inv2 = invented_facts(" ".join(p["llm"] for p in phr), sources)
        s["phrase_raw_invented"] = bool(inv2["numbers"] or inv2["names"])
        s["phrase_used"] = any(p.get("used") for p in phr)
        # did the rephrasing keep the answer? every number >= 3 the plain
        # answer said must still be there (independent of the agent's guard)
        lost = []
        for p in phr:
            said = _nums(p["llm"])
            lost += [v for v in _nums(p.get("draft") or "")
                     if v >= 3 and not any(abs(v - x) <= max(0.51, 0.005 * v) for x in said)]
        s["phrase_raw_dropped"] = bool(lost)
    return s


# ── summary ───────────────────────────────────────────────────────────────

def _pct(n, d):
    return None if not d else round(100.0 * n / d, 1)


def summarize(rows: list, model: str, mode: str, case_set: str) -> dict:
    by = lambda c: [r for r in rows if r["category"] == c]   # noqa: E731
    clear, amb, nt, act = by("clear"), by("ambiguous"), by("no_tool"), by("action")
    lat = [r["latency_s"] for r in rows]
    phr = [r for r in rows if "phrase_raw_invented" in r]
    return {
        "model": model if mode != "router" else "-", "mode": mode, "set": case_set,
        "n_cases": len(rows), "n_clear": len(clear), "n_ambig": len(amb),
        "n_notool": len(nt), "n_action": len(act),
        "clear_tool_acc": _pct(sum(r["tool_ok"] for r in clear), len(clear)),
        "clear_args_acc": _pct(sum(r["args_ok_first"] for r in clear), len(clear)),
        "ambig_asked": _pct(sum(r["asked"] for r in amb), len(amb)),
        "ambig_named_both": _pct(sum(r["named_both"] for r in amb), len(amb)),
        "notool_restraint": _pct(sum(r["restrained"] for r in nt), len(nt)),
        "action_ok": _pct(sum(r["action_ok"] for r in act), len(act)),
        "action_executed_early": sum(r["executed_early"] for r in act),
        "invented_fact_rate": _pct(sum(r["invented_fact"] for r in rows), len(rows)),
        "invented_number_rate": _pct(sum(bool(r["invented_numbers"]) for r in rows), len(rows)),
        "phrase_raw_invented": _pct(sum(r["phrase_raw_invented"] for r in phr), len(phr)),
        "phrase_raw_dropped": _pct(sum(r["phrase_raw_dropped"] for r in phr), len(phr)),
        "phrase_used": _pct(sum(r["phrase_used"] for r in phr), len(phr)),
        "n_phrased": len(phr),
        "lean_cases": sum(r["router_kind"] == "lean" for r in rows),
        "tiebreak_changed": sum(bool(r["tiebreak"] and r["tiebreak"].get("picked")
                                     and r["first_call"]
                                     and r["tiebreak"]["picked"] == r["first_call"]["name"]
                                     and r["tiebreak"]["picked"] != r["tiebreak"]["offered"][0])
                                for r in rows),
        "provider_errors": sum(r["provider_error"] for r in rows),
        "degraded_cases": sum(r["degraded"] for r in rows),
        "malformed_rate": _pct(sum(bool(r["malformed"]) for r in rows), len(rows)),
        "median_latency_s": round(statistics.median(lat), 3) if lat else None,
        "p90_latency_s": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 3) if lat else None,
    }


COLS = [("set", "set"), ("mode", "mode"), ("model", "model"),
        ("clear_tool_acc", "clear tool %"), ("clear_args_acc", "clear tool+args %"),
        ("ambig_asked", "ambig asked %"), ("notool_restraint", "no-tool restraint %"),
        ("action_ok", "action ok %"), ("invented_fact_rate", "invented fact % (cases)"),
        ("invented_number_rate", "invented number %"),
        ("phrase_raw_invented", "LLM phrase invented % (pre-guard)"),
        ("phrase_raw_dropped", "LLM phrase dropped a number % (pre-guard)"),
        ("phrase_used", "LLM phrase spoken %"),
        ("median_latency_s", "median s/question"), ("p90_latency_s", "p90 s")]


def write_table(summaries: list) -> str:
    lines = ["| " + " | ".join(h for _, h in COLS) + " |",
             "|" + "|".join("---" for _ in COLS) + "|"]
    for s in summaries:
        lines.append("| " + " | ".join("-" if s.get(k) is None else str(s.get(k))
                                       for k, _ in COLS) + " |")
    return "\n".join(lines)


def load_summaries() -> list:
    path = os.path.join(RESULTS, "summary.json")
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return []


_ORDER = {"router": 0, "router+llm": 1, "llm": 2}


def save_summaries(summaries: list) -> None:
    summaries.sort(key=lambda s: (s["set"] != "heldout", s["set"], _ORDER.get(s["mode"], 9),
                                  s["model"]))
    with open(os.path.join(RESULTS, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(summaries, f, indent=1)
    held = [s for s in summaries if s["set"] == "heldout"]
    dev = [s for s in summaries if s["set"] == "dev"]
    n = lambda xs: (f"{xs[0]['n_cases']} cases: {xs[0]['n_clear']} clear, {xs[0]['n_ambig']} "  # noqa: E731
                    f"ambiguous, {xs[0]['n_notool']} no-tool, {xs[0]['n_action']} action") if xs else ""
    md = ["# Assistant tool-choice eval", "",
          "Real agent, router, prompts and provider; tools stubbed with canned results in the "
          "real workers' shapes; temperature 0; Ollama /v1 on one RTX 4070, one model at a time.", "",
          "## Held-out (written before the router existed, never tuned on)", "",
          n(held), "", write_table(held), "",
          "## Dev (the router was tuned on these)", "", n(dev), "", write_table(dev), "",
          "- clear tool % = the first tool actually called is the expected one. No call = miss.",
          "- clear tool+args % = that, and the key arguments contain the expected names.",
          "- ambig asked % = no tool called and the reply asks a question.",
          "- no-tool restraint % = no tool called.",
          "- action ok % = right tool and args, gated until the yes/no, run once after yes, "
          "never after no.",
          "- invented fact % = cases whose reply has a number, or a capitalised name, found in "
          "neither the question, the tool results, nor the assistant's fixed prompt/UI text.",
          "- LLM phrase invented % = the model's rephrasing BEFORE the agent's number guard "
          "(router+llm only). The spoken reply after the guard is the column before it.",
          "- LLM phrase dropped a number % = the rephrasing lost a number (3 or more) the plain "
          "answer had, i.e. it lost part of the answer. LLM phrase spoken % = share of "
          "rephrasings that passed the agent's guard and were spoken.",
          "- latency = wall time of one question end to end (stubbed tools take ~0 s).", ""]
    with open(os.path.join(RESULTS, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")


# ── model lifecycle ───────────────────────────────────────────────────────

def free_physical_gb() -> float:
    class MEMSTAT(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MEMSTAT()
    m.dwLength = ctypes.sizeof(MEMSTAT)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m.ullAvailPhys / 1024 ** 3


def unload(model: str) -> None:
    subprocess.run(["ollama", "stop", model], capture_output=True, timeout=60)


def warm_up(model: str, base_url: str) -> float:
    """Load the model once so the first case does not pay the load time."""
    cfg = LLMConfig()
    cfg.base_url, cfg.model, cfg.temperature, cfg.max_tokens = base_url, model, 0.0, 8
    from assistant.providers import make_provider
    t0 = time.perf_counter()
    make_provider(cfg).chat([{"role": "user", "content": "hi"}], [])
    return time.perf_counter() - t0


def load_cases(which: list) -> list:
    out = []
    for name in which:
        with open(CASE_FILES[name], encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    c = json.loads(line)
                    c["set"] = name
                    out.append(c)
    return out


def run(modes: list, model: str, base_url: str, which: list, only: list = None) -> list:
    cases = load_cases(which)
    if only:
        cases = [c for c in cases if c["id"] in only]
    full_names = build_default_registry().names()
    out = []
    uses_model = any(m != "router" for m in modes)
    if uses_model:
        print(f"[mem] {free_physical_gb():.1f} GB free physical before loading {model}")
        print(f"[warm] {model} loaded in {warm_up(model, base_url):.1f}s")
    try:
        for mode in modes:
            rows = []
            for i, case in enumerate(cases, 1):
                rec = run_case(case, mode, model, base_url, full_names)
                rows.append(rec)
                ok = (rec.get("tool_ok") if case["category"] == "clear" else
                      rec.get("asked") if case["category"] == "ambiguous" else
                      rec.get("restrained") if case["category"] == "no_tool" else
                      rec.get("action_ok"))
                fc = rec["first_call"]
                print(f"  [{mode} {model if mode != 'router' else ''}] {i:2d}/{len(cases)} "
                      f"{case['id']} {'ok' if ok else '--'} "
                      f"{fc['name'] if fc else '(no call)'} {rec['latency_s']:.2f}s"
                      + (" INVENTED" if rec["invented_fact"] else ""), flush=True)
            tag = re.sub(r"[^A-Za-z0-9.]+", "_", model if mode != "router" else "none")
            fn = os.path.join(RESULTS, f"{tag}__{mode.replace('+', '_')}.jsonl")
            with open(fn, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            for name in which:
                sub = [r for r in rows if r["set"] == name]
                if sub:
                    out.append(summarize(sub, model, mode, name))
                    print(write_table([out[-1]]))
    finally:
        if uses_model:
            unload(model)
            print(f"[unload] ollama stop {model}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=LLMConfig().model)
    ap.add_argument("--modes", default="router")
    ap.add_argument("--base-url", default=LLMConfig().base_url)
    ap.add_argument("--sets", default="dev,heldout")
    ap.add_argument("--only", help="comma-separated case ids (debug; not saved)")
    ap.add_argument("--summarize", action="store_true")
    a = ap.parse_args()
    os.makedirs(RESULTS, exist_ok=True)
    _install_fences()

    summaries = load_summaries()
    if not a.summarize:
        modes = [agent_mod.normalize_mode(m) for m in a.modes.split(",") if m.strip()]
        which = [s for s in a.sets.split(",") if s in CASE_FILES]
        only = a.only.split(",") if a.only else None
        new = run(modes, a.model, a.base_url, which, only)
        if not only:
            for s in new:
                summaries = [x for x in summaries if not (x["model"] == s["model"] and
                                                          x["mode"] == s["mode"] and
                                                          x["set"] == s["set"])]
                summaries.append(s)
            save_summaries(summaries)
    print(write_table(summaries))
    return 0


if __name__ == "__main__":
    sys.exit(main())
