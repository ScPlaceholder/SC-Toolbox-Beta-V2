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
* When the user asks for something you cannot do, say what you CAN do: \
trade routes, market prices, ship cargo info, pinning routes on the map.
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

        If a confirm-gated tool is waiting, this utterance answers the
        question instead of going to the model.
        """
        text = (text or "").strip()
        if not text:
            return ""

        pending = self._pending_confirm
        if pending is not None:
            if _is_affirmative(text):
                self._pending_confirm = None
                result = self._execute(pending["tool"], pending["args"],
                                       pending["call_id"])
                return self._continue_with_result(result, pending["call_id"])
            if _is_negative(text):
                self._pending_confirm = None
                return "Okay — not pinning it."
            self._pending_confirm = None   # anything else cancels the gate

        self._messages.append({"role": "user", "content": text})
        return self._run_loop()

    def _run_loop(self) -> str:
        if self.provider is None:
            return "No LLM configured — open Settings and set an endpoint."
        try:
            text, calls = self.provider.chat(self._messages,
                                             self.registry.specs())
        except ProviderError as exc:
            return str(exc)

        for _ in range(_MAX_ITERATIONS):
            if not calls:
                self._messages.append({"role": "assistant", "content": text})
                self._trim()
                if text:
                    self.on_speak(text)
                return text

            for call in calls:
                tool = self.registry.get(call.get("name", ""))
                if tool is None:
                    self._messages.append(_tool_result(
                        call, {"error": "unknown tool " + str(call.get("name"))}))
                    continue
                if tool.confirm:
                    question = ("Do you want me to run " + tool.name +
                                "? Say yes or no.")
                    self._pending_confirm = {
                        "tool": tool,
                        "args": call.get("arguments") or {},
                        "call_id": call.get("id", ""),
                    }
                    self._messages.append({"role": "assistant", "content": text,
                                           "tool_calls": [call]})
                    self.on_speak(question)
                    return question
                self._messages.append({"role": "assistant", "content": text,
                                       "tool_calls": [call]})
                self._execute(tool, call.get("arguments") or {},
                              call.get("id", ""))

            try:
                text, calls = self.provider.chat(self._messages,
                                                 self.registry.specs())
            except ProviderError as exc:
                return str(exc)

        return "That got complicated — ask me again step by step."

    def _continue_with_result(self, result: dict, call_id: str) -> str:
        """After a confirmed yes: record the result and let the model close
        the conversation (usually just confirming the popup is up)."""
        self._messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False, default=str),
        })
        try:
            text, calls = self.provider.chat(self._messages,
                                             self.registry.specs())
        except ProviderError as exc:
            return str(exc)
        if calls:
            return self._run_loop()
        self._messages.append({"role": "assistant", "content": text})
        if text:
            self.on_speak(text)
        return text

    def _execute(self, tool, args: dict, call_id: str) -> dict:
        try:
            result = tool.run(self.ctx, args)
        except ToolError as exc:
            result = {"error": str(exc)}
        self._messages.append({
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(result, ensure_ascii=False, default=str),
        })
        return result

    def _trim(self) -> None:
        """Keep the system prompt plus the most recent turns."""
        if len(self._messages) > _HISTORY_SOFT_CAP + 1:
            head = self._messages[:1]
            tail = self._messages[-_HISTORY_SOFT_CAP:]
            self._messages = head + tail


def _tool_result(call: dict, result) -> dict:
    content = result
    if not isinstance(content, str):
        content = json.dumps(result, ensure_ascii=False, default=str)
    return {
        "role": "tool",
        "tool_call_id": call.get("id", ""),
        "content": content,
    }


_YES = ("yes", "yeah", "yep", "yup", "sure", "ok", "okay", "pin it",
        "do it", "go ahead", "please do", "affirmative", "sounds good")
_NO = ("no", "nope", "nah", "negative", "do not", "not now",
       "no thanks", "no thank you")


def _is_affirmative(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    if any(t == n or t.startswith(n) for n in _NO):
        return False
    if any(t == y or t.startswith(y + " ") for y in _YES):
        return True
    phrases = ("pin it", "do it", "go ahead", "sounds good")
    return any(p in t for p in phrases)


def _is_negative(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    return any(t == n or t.startswith(n) for n in _NO)
