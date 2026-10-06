"""LLM chat providers — stdlib urllib only, no pip dependency.

Two adapters behind one contract:

  * OpenAICompatibleProvider -- any /v1/chat/completions server:
    Ollama, LM Studio, OpenAI, vLLM, llama.cpp server, ...
  * AnthropicProvider        -- api.anthropic.com /v1/messages.

Both speak the same normalised types:

  * Message  -- {"role": ..., "content": str, optional tool_call ids}
  * ToolCall -- {"id": str, "name": str, "arguments": dict}
  * ToolSpec -- {"name", "description", "parameters": JSON Schema}

chat(messages, tools) -> (text, tool_calls) is all the agent needs.
Tool arguments are parsed defensively: a model that emits malformed
JSON simply gets an error string back as the tool result, and the
conversation continues.
"""
from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

from .config import LLMConfig

log = logging.getLogger(__name__)

Message = dict          # role / content / tool_call_id / tool_calls
ToolCall = dict         # id / name / arguments
ToolSpec = dict         # name / description / parameters (JSON schema)


class ProviderError(Exception):
    """LLM endpoint failure with a user-presentable message."""


def _norm_openai(resp: dict) -> tuple:
    """Normalise an OpenAI chat.completion response."""
    text = ""
    calls: list = []
    for ch in resp.get("choices", [])[:1]:
        msg = ch.get("message") or {}
        text = (msg.get("content") or "").strip()
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except (ValueError, TypeError):
                args = {"_malformed": str(raw)[:400]}
            calls.append({
                "id": tc.get("id", ""),
                "name": fn.get("name", ""),
                "arguments": args,
            })
    return text, calls


def _norm_anthropic(resp: dict) -> tuple:
    """Normalise an Anthropic messages response."""
    text_parts: list = []
    calls: list = []
    for blk in resp.get("content", []):
        btype = blk.get("type")
        if btype == "text":
            text_parts.append(blk.get("text", ""))
        elif btype == "tool_use":
            calls.append({
                "id": blk.get("id", ""),
                "name": blk.get("name", ""),
                "arguments": blk.get("input") or {},
            })
    return " ".join(text_parts).strip(), calls


def _to_anthropic(messages: list, tools: list) -> dict:
    """Convert normalised messages+tools into the Anthropic wire shape."""
    sys_parts, out = [], []
    for m in messages:
        role = m.get("role")
        if role == "system":
            sys_parts.append(m.get("content", ""))
            continue
        if role == "assistant":
            blocks: list = []
            if m.get("content"):
                blocks.append({"type": "text", "text": m["content"]})
            for tc in m.get("tool_calls") or []:
                blocks.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": tc.get("name", ""),
                    "input": tc.get("arguments") or {},
                })
            out.append({"role": "assistant", "content": blocks})
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
                "is_error": bool(m.get("is_error")),
            }
            prev = out[-1] if out else None
            # all results for one assistant turn go in ONE user message
            if (prev and prev["role"] == "user" and isinstance(prev["content"], list)
                    and all(b.get("type") == "tool_result" for b in prev["content"])):
                prev["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        out.append({"role": "user", "content": m.get("content", "")})
    body: dict = {
        "model": "",           # filled by caller
        "max_tokens": 1024,    # filled by caller
        "system": "\n".join(sys_parts),
        "messages": out,
    }
    if tools:
        body["tools"] = [
            {
                "name": t["name"],
                "description": t.get("description", ""),
                "input_schema": t.get("parameters")
                or {"type": "object", "properties": {}},
            }
            for t in tools
        ]
    return body



def _to_openai_wire(messages: list) -> list:
    """Internal normalised tool_calls -> strict OpenAI wire shape.

    Strict OpenAI-compatible servers reject assistant messages whose
    tool_calls are not {"id", "type": "function", "function": {...}} with
    JSON-string arguments; local servers usually tolerate anything.
    """
    out = []
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            wire = []
            for tc in m["tool_calls"]:
                wire.append({
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(
                            tc.get("arguments") or {}, ensure_ascii=False),
                    },
                })
            out.append({"role": "assistant", "content": m.get("content"),
                        "tool_calls": wire})
            continue
        out.append(m)
    return out

def _ipv4_loopback(url: str) -> str:
    """http://localhost:... -> http://127.0.0.1:...

    On Windows, urllib resolves "localhost" to ::1 first; Ollama and LM
    Studio listen on IPv4 only, so every request waited ~2 s for the IPv6
    attempt to fail before falling back (measured: 2.06-2.39 s
    per call via localhost vs 0.02-0.04 s via 127.0.0.1).
    """
    return re.sub(r"^(https?://)localhost(?=[:/]|$)", r"\g<1>127.0.0.1", url, flags=re.I)


class OpenAICompatibleProvider:
    """Any server exposing POST base_url/chat/completions."""

    kind = "openai"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def chat(self, messages: list, tools: list) -> tuple:
        url = _ipv4_loopback(self.cfg.base_url.rstrip("/")) + "/chat/completions"
        body: dict = {
            "model": self.cfg.model,
            "messages": _to_openai_wire(messages),
            "max_tokens": self.cfg.max_tokens,
            "temperature": self.cfg.temperature,
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": t} for t in tools
            ]
            body["tool_choice"] = "auto"
        resp = self._post(url, body)
        return _norm_openai(resp)

    def _post(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.cfg.api_key:
            req.add_header("Authorization", "Bearer " + self.cfg.api_key)
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ProviderError(
                f"{self.cfg.model} returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"cannot reach LLM at {url} ({exc})") from exc


class AnthropicProvider:
    """api.anthropic.com /v1/messages (or a compatible proxy)."""

    kind = "anthropic"

    def __init__(self, cfg: LLMConfig) -> None:
        self.cfg = cfg

    def chat(self, messages: list, tools: list) -> tuple:
        url = self.cfg.base_url.rstrip("/")
        if not url.endswith("/messages"):
            url = url.rstrip("/") + "/v1/messages"
        body = _to_anthropic(messages, tools)
        body["model"] = self.cfg.model
        body["max_tokens"] = self.cfg.max_tokens
        resp = self._post(url, body)
        return _norm_anthropic(resp)

    def _post(self, url: str, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("x-api-key", self.cfg.api_key)
        req.add_header("anthropic-version", "2023-06-01")
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ProviderError(
                f"{self.cfg.model} returned HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                f"cannot reach LLM at {url} ({exc})") from exc


def make_provider(cfg: LLMConfig):
    """Factory — the single place that maps provider name to adapter."""
    if cfg.provider.lower() == "anthropic":
        return AnthropicProvider(cfg)
    return OpenAICompatibleProvider(cfg)
