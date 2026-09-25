"""AssistantAgent — the conversation loop.

Flow for one user utterance:

  1. Append the user message, call the provider with the tool specs.
  2. While the model asks for tool calls (max _MAX_ITERATIONS):
       * confirm=True tools are gated: the agent asks the user aloud /
         on screen and only executes after an explicit yes arrives as
         the next user message (see handle_user_text).
       * results are appended as tool messages; call again.
  3. Speak + return the final text.

The confirmation gate is what makes this safe to leave running: a model
can *read* anything, but it can only *act* (open windows, pin popups)
with the user's spoken yes.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from .logic import classify_confirmation
from .providers import ProviderError, make_provider
from .tools import ToolContext, ToolError, ToolRegistry

log = logging.getLogger(__name__)

_MAX_ITERATIONS = 6
_HISTORY_SOFT_CAP = 40        # trim oldest turns on long sessions

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


class AssistantAgent:
    """One conversation with one LLM endpoint."""

    def __init__(self, registry: ToolRegistry, ctx: ToolContext) -> None:
        self.registry = registry
        self.ctx = ctx
        self._messages: list = [{"role": "system", "content": SYSTEM_PROMPT}]
        self._pending_confirm: Optional[dict] = None
        self.provider = None                       # refreshed per configure()
        self.on_speak: Callable[[str], None] = lambda s: None

    def configure(self, cfg) -> None:
        """(Re)build the provider from an LLMConfig."""
        self.provider = make_provider(cfg)

    def reset(self) -> None:
        self._messages = self._messages[:1]
        self._pending_confirm = None

    def handle_user_text(self, text: str) -> str:
        """Feed one user utterance; returns the assistant's reply text.

        If a confirm-gated tool is waiting, this utterance answers it:
          * yes   -> run the tool, record its result, let the model close;
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
                self._execute(tool, args, call_id)
                return self._run_loop()
            if verdict == "no":
                self._messages.append(_tool_result_msg(call_id, {
                    "declined": True,
                    "message": "The user said no. Nothing was done."}))
                reply = "Okay, I won't " + tool.describe_action(args) + "."
                self._messages.append({"role": "assistant", "content": reply})
                self._trim()
                self.on_speak(reply)
                return reply
            self._messages.append(_tool_result_msg(call_id, {
                "cancelled": True,
                "message": "The user did not confirm and moved on; nothing was done. "
                           "Do not run it unless they ask again."}))

        self._messages.append({"role": "user", "content": text})
        return self._run_loop()

    def _run_loop(self) -> str:
        if self.provider is None:
            return "No LLM configured — open Settings and set an endpoint."

        for _ in range(_MAX_ITERATIONS):
            try:
                text, calls = self.provider.chat(self._messages,
                                                 self.registry.specs())
            except ProviderError as exc:
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
            calls = [dict(c, id=c.get("id") or f"call_{i}") for i, c in enumerate(calls)]
            self._messages.append({"role": "assistant", "content": text,
                                   "tool_calls": calls})
            gated = None
            for call in calls:
                tool = self.registry.get(call.get("name", ""))
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
                                         "call_id": call["id"]}
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
