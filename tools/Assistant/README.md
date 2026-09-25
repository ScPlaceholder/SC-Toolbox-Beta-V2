# AI Assistant (toolbox-wide LLM voice copilot)

The user talks; the assistant summons toolbox data through tools and
answers. Side effects (pinning a route popup, opening Trade Hub) always
ask the user first.

## Plug in an LLM

The endpoint is **not hardcoded**. It is read from
`~/.sctoolbox/assistant_llm.json` (created on first save, or edit it by
hand):

```json
{
  "provider": "openai",          // "openai" = any /v1/chat/completions server
                                 // "anthropic" = api.anthropic.com
  "base_url": "http://localhost:11434/v1",
  "api_key": "",
  "model": "qwen2.5:14b",
  "max_tokens": 1024,
  "temperature": 0.2
}
```

Works with Ollama, LM Studio, OpenAI, vLLM — anything OpenAI-compatible —
or Anthropic directly. Environment overrides: `SC_LLM_PROVIDER`,
`SC_LLM_BASE_URL`, `SC_LLM_API_KEY`, `SC_LLM_MODEL`. The panel's
**Settings…** button edits the same file.

## Architecture

```
user voice ──▶ ears (whisper) ──▶ AssistantAgent ──▶ provider (LLM)
                                      │  ▲
                              tool calls │  │ tool results
                                      ▼  │
                              ToolRegistry ──▶ headless services (UEX routes,
                                               ships, market prices)
                                            ──▶ ipc_bus ──▶ skill subprocesses
                                               (pinned popup in Trade Hub)
```

* `assistant/config.py`     — endpoint config (the plug-in point)
* `assistant/providers.py`  — OpenAI-compatible + Anthropic adapters, stdlib only
* `assistant/tools.py`      — `@tool` decorator, registry, arg coercion, confirm gate flag
* `assistant/headless.py`   — data services: routes via Trade Hub's own UEXClient/route_engine
* `assistant/ipc_bus.py`    — finds live skill IPC files (`sc_toolbox_<skill>_*.jsonl`) by PID liveness
* `assistant/agent.py`      — conversation loop + yes/no confirmation gate
* `assistant/voice.py`      — ears (mic + faster-whisper) + mouth (SAPI TTS), optional deps
* `assistant/builtin_tools.py` — the reference toolset; extend here
* `assistant/panel.py`      — the HUD window

## Extending: give the LLM a new tool

```python
from assistant.tools import tool

@tool(
    name="my_feature",
    description="What it does, phrased for the model.",
    params={"arg1": {"type": "string", "description": "..."}},
    required=["arg1"],
    confirm=True,        # True = agent asks the user before running
)
def _my_feature(ctx, arg1: str) -> dict:
    ...
```

Register it in `build_default_registry()` — the model sees it on the next
turn. Tools returning JSON-serialisable dicts; raise `ToolError` for
user-presentable failures.

## The example flow (bulk freight for the Caterpillar)

1. `find_trade_routes(ship="Caterpillar")` — headless UEX fetch, ranked by
   full-ship profit.
2. Assistant speaks the top route and asks once whether to pin it.
3. User says yes → `show_route_popup(route, ship="Caterpillar")`
   (confirm-gated) → `ipc_bus` ensures Trade Hub is running → sends the new
   `route_detail` IPC command → Trade Hub opens the Route Detail popup
   **pinned** and draws the route on its star map.

## Known limitations

* If the assistant spawns Trade Hub itself and the launcher later spawns
  its own copy (hotkey), two Trade Hub windows can coexist until restart.
  Harmless but worth knowing.
* Voice deps (`sounddevice faster-whisper numpy pynput`) are optional;
  without them the ears button reports what to `pip install`.
* No Python runtime was available where this was built — first run needs
  a smoke test on a machine with the toolbox Python.
