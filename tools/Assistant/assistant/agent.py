"""AssistantAgent — the conversation loop.

Three modes (LLMConfig.mode):

  * "router"      -- no model at all. router.Router picks the tool, the
                     names and whether to ask; answers.plain_answer says
                     the result. Every number it speaks is in the result.
  * "router+llm"  -- the default. The router still decides. The model is
                     only asked to (a) choose among the router's top 2-3
                     tools when the router leans but is not sure, and
                     (b) rephrase the plain answer. A rephrasing that says
                     a number the result never did, or drops a number or
                     most of the names the plain answer had, is thrown
                     away and the plain answer is spoken instead.
  * "llm"         -- the original loop: the model sees every tool and
                     decides everything.

Either LLM mode falls back to "router" by itself when the model cannot be
reached (no config, connection refused, HTTP error), and retries the
model after _LLM_RETRY_S seconds.

Flow of the original "llm" loop, for one user utterance:

  1. Append the user message, call the provider with the tool specs.
  2. While the model asks for tool calls (max _MAX_ITERATIONS):
       * confirm=True tools are gated: the agent asks the user aloud /
         on screen and only executes after an explicit yes arrives as
         the next user message (see handle_user_text).
       * results are appended as tool messages; call again.
  3. Speak + return the final text.

The confirmation gate is what makes this safe to leave running, in every
mode: anything can *read*, but nothing *acts* (open windows, pin popups)
without the user's spoken yes.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import re
import time
from typing import Callable, Optional

from .answers import dropped_names, dropped_numbers, plain_answer, ungrounded_numbers
from .logic import classify_confirmation, norm
from .providers import ProviderError, make_provider
from .router import Decision, Router
from .tools import ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

_MAX_ITERATIONS = 6
_HISTORY_SOFT_CAP = 40        # trim oldest turns on long sessions
_LLM_RETRY_S = 60.0           # after a model failure, router-only for this long
_AUX_MAX_TOKENS = 160         # tie-break / phrasing calls are short
_AUX_TIMEOUT_S = 30

MODES = ("router", "router+llm", "llm")
DEFAULT_MODE = "router+llm"

SYSTEM_PROMPT = """You are the SC Toolbox voice assistant — a concise, \
spoken-first copilot for Star Citizen players.

Rules:
* Answer in one or two short sentences. The user is often flying; they \
cannot read a wall of text. Numbers matter, prose does not.
* Use tools to fetch live data. Never invent prices, routes or stock — \
if a tool errors, say so plainly.
* For trade routes: name the commodity, the buy and sell locations with \
systems, the estimated profit, and the ship capacity you assumed.
* After presenting a route, ask exactly once whether the user wants it \
pinned. If they say yes, call show_route_popup. If they say no, drop it.
* Pick the tool whose description matches the question. Pass names as \
the user said them; the tools resolve spelling ("quantanium", "helix 1").
* A result with "empty": true is a real answer: nothing matched. Say so; \
do not retry with invented names.
* When the user asks for something you cannot do, say what you CAN do: \
trade routes, item and ship prices, missions and blueprint rewards, where \
to mine, crafting recipes, mining signals and loadouts, cargo layouts, \
jump routes, play time, the current FPS loadout, and opening toolbox tools.
"""

TIEBREAK_PROMPT = (
    "You route a Star Citizen player's question to one tool. Call exactly one of "
    "the tools offered, the one that answers the question. Pass names exactly as "
    "the player said them. Do not answer the question yourself.")

PHRASE_PROMPT = (
    "Rewrite the facts as one or two short spoken sentences for a Star Citizen "
    "pilot. Keep every name and number exactly as given. Add nothing: no advice, "
    "no extra facts, no greetings.")

CHAT_PROMPT = (
    "You are a brief, friendly Star Citizen copilot. Reply to small talk in one "
    "short sentence. State no facts, names or numbers.")


def _persona() -> str:
    """The chosen character (Elah by default) appended to prompts that produce spoken words; '' for the Windows
    voice. Read every call, so a Settings change applies to the next answer. The tie-break prompt stays neutral:
    it only picks a tool and never speaks."""
    try:
        from shared.character_voice import persona_prompt
        return persona_prompt()
    except Exception:
        return ""


def normalize_mode(mode) -> str:
    m = str(mode or "").strip().lower().replace(" ", "")
    m = {"router-llm": "router+llm", "router_llm": "router+llm", "hybrid": "router+llm",
         "plain": "router", "nollm": "router", "none": "router"}.get(m, m)
    return m if m in MODES else DEFAULT_MODE


class _FirstCallFailed(Exception):
    """The model could not be reached before anything happened this turn."""


class AssistantAgent:
    """One conversation, router-first, with an optional LLM endpoint."""

    def __init__(self, registry: ToolRegistry, ctx: ToolContext,
                 mode: str = DEFAULT_MODE) -> None:
        self.registry = registry
        self.ctx = ctx
        self._messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._pending_confirm: Optional[dict] = None
        self._pending_route: Optional[dict] = None
        self.provider = None                       # refreshed per configure()
        self.aux = None                            # short calls (tie-break, phrasing)
        self.mode = normalize_mode(mode)
        self.router = Router(registry, base_dir=getattr(ctx, "base_dir", "") or "")
        self._llm_down_until = 0.0
        self.last_llm_error = ""
        self._n_calls = 0
        # one entry per decision: who chose (router / llm), which tool,
        # which args, and whether it ran or was gated. Read by the eval.
        self.trace: list = []
        self.on_speak: Callable[[str], None] = lambda s: None

    # ── configuration ────────────────────────────────────────────────────
    def configure(self, cfg) -> None:
        """(Re)build the providers and mode from an LLMConfig."""
        self.mode = normalize_mode(getattr(cfg, "mode", DEFAULT_MODE))
        self._llm_down_until = 0.0
        if self.mode == "router" or not cfg.ready():
            self.provider = self.aux = None
            return
        self.provider = make_provider(cfg)
        aux_cfg = dataclasses.replace(cfg, max_tokens=min(int(cfg.max_tokens), _AUX_MAX_TOKENS),
                                      timeout=min(int(cfg.timeout), _AUX_TIMEOUT_S))
        self.aux = make_provider(aux_cfg)

    def reset(self) -> None:
        self._messages = self._messages[:1]
        self._pending_confirm = None
        self._pending_route = None

    def _llm_ok(self) -> bool:
        return self.provider is not None and time.time() >= self._llm_down_until

    def _llm_failed(self, exc) -> None:
        self._llm_down_until = time.time() + _LLM_RETRY_S
        self.last_llm_error = str(exc)
        log.warning("assistant: LLM unavailable, router only for %ss: %s", _LLM_RETRY_S, exc)

    @property
    def effective_mode(self) -> str:
        """The mode actually in use right now (LLM modes degrade to router)."""
        if self.mode != "router" and not self._llm_ok():
            return "router"
        return self.mode

    # ── one utterance ────────────────────────────────────────────────────
    def handle_user_text(self, text: str) -> str:
        if self._messages and self._messages[0].get("role") == "system":
            self._messages[0]["content"] = SYSTEM_PROMPT + _persona()
        """Feed one user utterance; returns the assistant's reply text.

        If a confirm-gated tool is waiting, this utterance answers it:
          * yes   -> run the tool, record its result, then close the turn;
          * no    -> record a "declined" result, reply locally;
          * other -> record a "not confirmed" result, then treat the
                     utterance as a new request.
        Whatever the answer, the pending tool call gets exactly ONE result,
        so the history never carries an unanswered or doubled tool call.
        """
        text = (text or "").strip()
        if not text:
            return ""

        pending = self._pending_confirm
        if pending is not None:
            self._pending_confirm = None
            verdict = classify_confirmation(text)
            tool, args, call_id = pending["tool"], pending["args"], pending["call_id"]
            if verdict == "yes":
                result = self._execute(tool, args, call_id)
                if pending.get("by") == "llm" and self.effective_mode == "llm":
                    return self._run_loop()
                return self._close(text, tool, args, result)
            if verdict == "no":
                self._messages.append(_tool_result_msg(call_id, {
                    "declined": True,
                    "message": "The user said no. Nothing was done."}))
                reply = "Okay, I won't " + tool.describe_action(args) + "."
                return self._say(reply)
            self._messages.append(_tool_result_msg(call_id, {
                "cancelled": True,
                "message": "The user did not confirm and moved on; nothing was done. "
                           "Do not run it unless they ask again."}))

        if self.effective_mode == "llm":
            self._pending_route = None
            self._messages.append({"role": "user", "content": text})
            try:
                return self._run_loop(first_call_raises=True)
            except _FirstCallFailed as exc:
                self._messages.pop()               # the router re-adds it
                self._llm_failed(exc)
        return self._routed(text)

    # ── router path ──────────────────────────────────────────────────────
    def _routed(self, text: str) -> str:
        pending, self._pending_route = self._pending_route, None
        d = self.router.decide(text, pending)
        rec = {"by": "router", "text": text, "kind": d.kind, "tool": d.tool,
               "args": dict(d.args), "candidates": d.candidates[:4], "reason": d.reason,
               "args_by_tool": d.args_by_tool,
               "called": False, "mode": self.effective_mode}
        self.trace.append(rec)
        self._messages.append({"role": "user", "content": text})

        if d.kind == "lean" and self.effective_mode == "router+llm":
            d = self._tiebreak(text, d, rec)

        if d.kind == "ask":
            self._pending_route = d.pending
            return self._say(d.question)
        if d.kind == "chat":
            reply = d.reply
            if self.effective_mode == "router+llm":
                reply = self._llm_chat(text, rec) or d.reply
            return self._say(reply)
        if d.kind == "none" or not d.tool:
            return self._say(d.reply or "I can't answer that yet.")

        tool = self.registry.get(d.tool)
        if tool is None:
            return self._say("I can't answer that yet.")
        args = dict(d.args)
        if tool.name == "show_route_popup":
            return self._say("Ask me for a trade route first, then I can pin it.")
        call_id = self._new_call_id()
        self._messages.append({"role": "assistant", "content": "",
                               "tool_calls": [{"id": call_id, "name": tool.name, "arguments": args}]})
        rec.update(tool=tool.name, args=args, called=True)
        if tool.confirm:
            self._pending_confirm = {"tool": tool, "args": args, "call_id": call_id, "by": "router"}
            return self._say("Want me to " + tool.describe_action(args) + "? Say yes or no.",
                             record=False)
        result = self._execute(tool, args, call_id)
        return self._close(text, tool, args, result, rec)

    def _new_call_id(self) -> str:
        self._n_calls += 1
        return f"r{self._n_calls}"

    def _close(self, question: str, tool, args: dict, result, rec: Optional[dict] = None) -> str:
        """Say the result: plain answer, optionally rephrased by the model;
        after a trade route, offer (once) to pin it."""
        draft = plain_answer(tool.name, args, result)
        reply = draft
        # actions are said in fixed words ("Opened Starmap."); nothing to phrase
        if (self.effective_mode == "router+llm" and not tool.confirm
                and not (isinstance(result, dict) and result.get("error"))):
            reply = self._phrase(question, tool.name, result, draft, rec)
        routes = result.get("routes") if isinstance(result, dict) else None
        pin = self.registry.get("show_route_popup")
        if tool.name == "find_trade_routes" and routes and pin is not None:
            call_id = self._new_call_id()
            pin_args = {"route": routes[0], "ship": result.get("ship") or args.get("ship", "")}
            reply = reply.rstrip() + " Want it pinned in Trade Hub?"
            self._messages.append({"role": "assistant", "content": reply, "tool_calls": [
                {"id": call_id, "name": pin.name, "arguments": pin_args}]})
            self._pending_confirm = {"tool": pin, "args": pin_args, "call_id": call_id, "by": "router"}
            self._trim()
            self.on_speak(reply)
            return reply
        return self._say(reply)

    def _say(self, reply: str, record: bool = True) -> str:
        if record:
            self._messages.append({"role": "assistant", "content": reply})
            self._trim()
        if reply:
            self.on_speak(reply)
        return reply

    # ── the optional model, inside router+llm ────────────────────────────
    def _aux_chat(self, messages: list, tools: list):
        prov = self.aux or self.provider
        return prov.chat(messages, tools)

    def _tiebreak(self, text: str, d: Decision, rec: dict) -> Decision:
        cands = [n for n, _ in d.candidates[:3]
                 if self.registry.get(n) is not None and not self.registry.get(n).confirm]
        if len(cands) < 2:
            return d
        specs = [self.registry.get(n).spec() for n in cands]
        msgs = [{"role": "system", "content": TIEBREAK_PROMPT}, {"role": "user", "content": text}]
        t0 = time.perf_counter()
        try:
            said, calls = self._aux_chat(msgs, specs)
        except ProviderError as exc:
            self._llm_failed(exc)
            rec["tiebreak"] = {"offered": cands, "error": str(exc)[:200]}
            return d
        pick = next((c for c in calls if c.get("name") in cands), None)
        rec["tiebreak"] = {"offered": cands, "picked": pick and pick.get("name"),
                           "llm_args": pick and pick.get("arguments"), "text": (said or "")[:200],
                           "latency_s": round(time.perf_counter() - t0, 3)}
        if pick is None:
            return d                                  # keep the router's lean
        name = pick["name"]
        args = dict(d.args_by_tool.get(name) or {})
        said_by_user = norm(text)
        for k, v in (pick.get("arguments") or {}).items():
            # only fill a gap, and only with words the user actually said:
            # a model's guess ("Caterpillar") must not become a fact
            if k not in args and isinstance(v, str) and v.strip() and norm(v) in said_by_user:
                args[k] = v
        missing = self.router.missing(name, args)
        if missing:
            return Decision("ask", tool=name, args=args, missing=missing,
                            question=self.router.slot_question(missing, args),
                            reason="llm tie-break picked " + name,
                            pending={"tool": name, "args": args, "missing": missing})
        return Decision("call", tool=name, args=args, candidates=d.candidates,
                        reason="llm tie-break picked " + name)

    def _phrase(self, question: str, tool_name: str, result, draft: str,
                rec: Optional[dict]) -> str:
        if not self._llm_ok():
            return draft
        msgs = [{"role": "system", "content": PHRASE_PROMPT + _persona()},
                {"role": "user", "content": f"Question: {question}\nFacts: {draft}"}]
        t0 = time.perf_counter()
        try:
            said, _ = self._aux_chat(msgs, [])
        except ProviderError as exc:
            self._llm_failed(exc)
            if rec is not None:
                rec["phrase"] = {"error": str(exc)[:200]}
            return draft
        said = (said or "").strip().strip('"').strip()
        bad = ungrounded_numbers(said, draft, result, question)
        lost = dropped_numbers(said, draft) + dropped_names(said, draft, result)
        too_long = len(said) > max(300, 3 * len(draft))
        use = bool(said) and not bad and not lost and not too_long
        if rec is not None:
            rec["phrase"] = {"llm": said, "draft": draft, "ungrounded": bad, "dropped": lost,
                             "too_long": too_long,
                             "used": use, "latency_s": round(time.perf_counter() - t0, 3)}
        return said if use else draft

    def _llm_chat(self, text: str, rec: dict) -> str:
        if not self._llm_ok():
            return ""
        msgs = [{"role": "system", "content": CHAT_PROMPT + _persona()}, {"role": "user", "content": text}]
        try:
            said, _ = self._aux_chat(msgs, [])
        except ProviderError as exc:
            self._llm_failed(exc)
            return ""
        said = (said or "").strip()
        ok = bool(said) and not re.search(r"\d", said) and len(said) <= 200
        rec["chat"] = {"llm": said, "used": ok}
        return said if ok else ""

    # ── the original all-LLM loop ────────────────────────────────────────
    def _run_loop(self, first_call_raises: bool = False) -> str:
        if self.provider is None:
            if first_call_raises:
                raise _FirstCallFailed("no LLM configured")
            return "No LLM configured — open Settings and set an endpoint."

        for i in range(_MAX_ITERATIONS):
            try:
                text, calls = self.provider.chat(self._messages,
                                                 self.registry.specs())
            except ProviderError as exc:
                if first_call_raises and i == 0:
                    raise _FirstCallFailed(str(exc)) from exc
                return str(exc)

            if not calls:
                self._messages.append({"role": "assistant", "content": text})
                self._trim()
                if text:
                    self.on_speak(text)
                return text

            # One assistant message carries every call of this turn; each
            # call then gets exactly one tool result (or is the single
            # confirm-gated call whose result arrives with the user's answer).
            calls = [dict(c, id=c.get("id") or f"call_{i}_{j}") for j, c in enumerate(calls)]
            self._messages.append({"role": "assistant", "content": text,
                                   "tool_calls": calls})
            gated = None
            for call in calls:
                tool = self.registry.get(call.get("name", ""))
                self.trace.append({"by": "llm", "tool": call.get("name", ""),
                                   "args": call.get("arguments") or {},
                                   "called": tool is not None, "mode": "llm"})
                if tool is None:
                    self._messages.append(_tool_result_msg(call["id"], {
                        "error": "unknown tool " + repr(call.get("name")),
                        "available_tools": self.registry.names()}, is_error=True))
                    continue
                if tool.confirm:
                    if gated is None:
                        gated = (tool, call)
                    else:
                        self._messages.append(_tool_result_msg(call["id"], {
                            "error": "only one action that needs the user's OK can be "
                                     "asked per turn; ask for this one afterwards"},
                            is_error=True))
                    continue
                self._execute(tool, call.get("arguments") or {}, call["id"])

            if gated is not None:
                tool, call = gated
                args = call.get("arguments") or {}
                self._pending_confirm = {"tool": tool, "args": args,
                                         "call_id": call["id"], "by": "llm"}
                question = ("Want me to " + tool.describe_action(args) +
                            "? Say yes or no.")
                self.on_speak(question)
                return question

        return "That got complicated — ask me again step by step."

    def _execute(self, tool, args: dict, call_id: str) -> dict:
        is_error = False
        try:
            result = tool.run(self.ctx, args)
        except ToolError as exc:
            result = {"error": str(exc)}
            is_error = True
        self._messages.append(_tool_result_msg(call_id, result, is_error=is_error))
        return result

    def _trim(self) -> None:
        """Keep the system prompt plus the most recent turns."""
        if len(self._messages) > _HISTORY_SOFT_CAP + 1:
            head = self._messages[:1]
            tail = self._messages[-_HISTORY_SOFT_CAP:]
            # never start the kept history mid tool-call group: a leading
            # tool result would answer a call that was trimmed away
            while tail and tail[0].get("role") != "user":
                tail = tail[1:]
            self._messages = head + tail


def _tool_result_msg(call_id: str, result, is_error: bool = False) -> dict:
    content = result
    if not isinstance(content, str):
        content = json.dumps(result, ensure_ascii=False, default=str)
    msg = {"role": "tool", "tool_call_id": call_id, "content": content}
    if is_error:
        msg["is_error"] = True
    return msg


def _is_affirmative(text: str) -> bool:
    return classify_confirmation(text) == "yes"


def _is_negative(text: str) -> bool:
    return classify_confirmation(text) == "no"
