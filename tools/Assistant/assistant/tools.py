"""Tool registry — how toolbox features become LLM-callable functions.

A tool is a plain function decorated with @tool:

    @tool(
        name="find_trade_routes",
        description="Find the most profitable trade routes ...",
        params={
            "ship":     {"type": "string", "description": "Ship name, e.g. Caterpillar"},
            "top_n":    {"type": "integer", "description": "How many routes to return"},
        },
        required=["ship"],
        confirm=False,          # True -> agent must ask the user first
    )
    def _find(ctx: ToolContext, ship: str, top_n: int = 5) -> dict:
        ...

Design rules:
  * Tool functions always take ``ctx`` first and return a JSON-serialisable
    dict (or raise ToolError with a user-presentable message).
  * ``confirm=True`` marks side-effecting actions (opening windows, pinning
    popups). The agent surfaces those to the user and only proceeds on an
    explicit yes — the LLM can never spend the user's UI behind their back.
  * Registration order == prompt order; keep it deliberate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)


class ToolError(Exception):
    """Tool failure with a user-presentable message."""


@dataclass
class Tool:
    name: str
    description: str
    func: Callable
    parameters: dict = field(default_factory=dict)   # JSON schema properties
    required: list = field(default_factory=list)
    confirm: bool = False
    # For confirm tools: what the action does, as a verb phrase with
    # optional {arg} placeholders, e.g. "open {name}". Used for the yes/no
    # question and for the reply when the user declines.
    action: str = ""

    def describe_action(self, arguments: Optional[dict] = None) -> str:
        """Human phrase for this call, e.g. 'open Trade Hub'."""
        if not self.action:
            return "run " + self.name.replace("_", " ")
        try:
            return self.action.format_map(_Blank(arguments or {}))
        except (ValueError, IndexError):
            return self.action

    def spec(self) -> dict:
        """OpenAI/Anthropic tool spec."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "object",
                "properties": self.parameters,
                "required": self.required,
            },
        }

    def run(self, ctx: "ToolContext", arguments: dict) -> dict:
        """Execute with coercion + a JSON-serialisable result."""
        args = self._coerce(dict(arguments or {}))
        try:
            result = self.func(ctx, **args)
        except ToolError:
            raise
        except TypeError as exc:
            raise ToolError(f"bad arguments for {self.name}: {exc}") from exc
        except Exception as exc:                       # noqa: BLE001
            log.exception("tool %s failed", self.name)
            raise ToolError(f"{self.name} failed: {exc}") from exc
        return _jsonable(result)

    # ── argument coercion ────────────────────────────────────────────────
    def _coerce(self, args: dict) -> dict:
        """Drop unknown keys, fill defaults, coerce scalar types.

        LLMs are sloppy with types (quoting ints, etc.) — be forgiving
        but never pass garbage into toolbox code.
        """
        props = self.parameters
        out = {}
        for key, spec in props.items():
            if key in args and args[key] is not None:
                out[key] = _coerce_value(args[key], spec.get("type"))
            elif key in self.required:
                raise ToolError(f"{self.name}: missing required argument '{key}'")
        return out


class _Blank(dict):
    def __missing__(self, key):
        return "it"


def _coerce_value(value: Any, type_name: Optional[str]) -> Any:
    if type_name == "integer":
        try:
            return int(value)
        except (TypeError, ValueError):
            raise ToolError(f"expected an integer, got {value!r}")
    if type_name == "number":
        try:
            return float(value)
        except (TypeError, ValueError):
            raise ToolError(f"expected a number, got {value!r}")
    if type_name == "boolean":
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on")
    if type_name == "array":
        if isinstance(value, list):
            return value
        return [value]
    return str(value) if not isinstance(value, (dict, list)) else value


def _jsonable(value: Any) -> Any:
    """Best-effort JSON normalisation with truncation for LLM context size."""
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"result": str(value)[:2000]}
    limit = 6000
    if len(text) > limit:
        return {"result": text[:limit] + " ... (truncated)"}
    return value


@dataclass
class ToolContext:
    """Everything a tool needs from the host app."""
    base_dir: str                          # SC_Toolbox root
    speak: Callable[[str], None] = lambda s: None      # TTS shortcut
    status: Callable[[str], None] = lambda s: None     # status-line update
    extra: dict = field(default_factory=dict)


class ToolRegistry:
    """Ordered registry of Tool objects."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            log.warning("tool %s already registered — replacing", tool.name)
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def specs(self) -> list:
        return [t.spec() for t in self._tools.values()]

    def names(self) -> list:
        return list(self._tools)


def tool(name: str, description: str, params: Optional[dict] = None,
         required: Optional[list] = None, confirm: bool = False,
         action: str = "") -> Callable:
    """Decorator registering ``fn`` as a tool on the decorated registry."""
    def wrap(fn: Callable) -> Tool:
        return Tool(
            name=name,
            description=description,
            func=fn,
            parameters=params or {},
            required=required or [],
            confirm=confirm,
            action=action,
        )
    return wrap
