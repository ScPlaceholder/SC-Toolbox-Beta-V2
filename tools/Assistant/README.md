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
user voice --> ears (whisper) --> AssistantAgent --> provider (LLM)
                                      |  ^
                              tool calls |  | tool results
                                      v  |
                              ToolRegistry --> worker_pool --> one worker
                                               subprocess per tool, running
                                               that tool's own data code
                                            --> ipc_bus --> launcher / skill
                                               windows (ask-first actions)
```

The skills reuse top-level package names (`data`, `services`, `core`,
`models`, `config`), so two of them can never be imported into one
Python. Each tool's headless calls therefore run in a **per-tool worker
subprocess** (`assistant/workers/_worker_main.py`): `sys.path` is that
tool's folder plus the toolbox root, requests and replies are JSON lines,
every call has a timeout, and a crash comes back with its error text and
the worker's stderr tail instead of being swallowed. Workers use the same
interpreter as the launcher, stay warm (their fetched data is cached in
memory), at most two live at once, and results are cached for ~3 minutes.
Workers never write the tools' caches or settings, and run with
`PYTHONDONTWRITEBYTECODE=1`.

* `assistant/config.py`     - endpoint config (the plug-in point)
* `assistant/providers.py`  - OpenAI-compatible + Anthropic adapters, stdlib only
* `assistant/tools.py`      - `@tool` decorator, registry, arg coercion, confirm gate flag
* `assistant/builtin_tools.py` - the toolset (below); extend here
* `assistant/worker_pool.py` - per-tool worker subprocesses, timeouts, caches
* `assistant/workers/h_*.py` - one handler per tool, run inside its worker
* `assistant/logic.py`      - pure helpers: fuzzy names, the Mission DB blueprint
  join, mining-location dedupe, yes/no parsing
* `assistant/headless.py`   - in-process helpers (ship SCU, popup shaping, tool names)
* `assistant/ipc_bus.py`    - finds a skill's command file by a LIVE process that
  holds it on its command line; talks to the launcher
* `assistant/agent.py`      - conversation loop + yes/no confirmation gate
* `assistant/voice.py`      - ears (mic + faster-whisper) + mouth (SAPI TTS), optional deps
* `assistant/panel.py`      - the HUD window
* `assistant/selftest.py`   - `python -m assistant.selftest` from `tools/Assistant`

## Tools

| Tool | Source tool | Asks first |
|---|---|---|
| `ship_info(name)` | shared ship presets | |
| `find_trade_routes(ship, commodity?, system?, top_n=5, allow_illegal=true)` | Trade Hub (`commodities_prices_all`) | |
| `find_item_price(item, top_n=5)` | Market Finder | |
| `ship_buy_rent(ship)` | Market Finder | |
| `missions_for_blueprint(name)` | Mission DB | |
| `where_to_mine(resource)` | Mission DB | |
| `search_missions(faction?, system?, mission_type?)` | Mission DB | |
| `blueprint_recipe(name)` | Craft Database (active LIVE version) | |
| `identify_signal(value)` | Mining Signals | |
| `mining_loadout_stats(ship, laser?, modules[]?, gadget?)` | Mining Loadout | |
| `cargo_layout(ship)` | Cargo Loader | |
| `jump_route(from_system, to_system)` | Starmap | |
| `current_loadout()` | Battle Buddy (Game.log, read-only) | |
| `playtime_summary()` | PlayTime | |
| `show_route_popup(route, ship?, show_on_map?)` | Trade Hub window | yes |
| `open_trade_hub()` | Trade Hub window | yes |
| `launch_tool(name)` | launcher `launch_skill` | yes |

Names are fuzzy-matched inside the tools ("quantanium" finds
"Quantainium (Raw)", "helix 1" finds "Helix I Mining Laser"). A result
with `"empty": true` is a valid "nothing matched", not an error.
DPS / optimal ship builds are not wired yet.

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
* `launch_tool` needs the launcher to be reading a command file, which is
  the case when WingmanAI starts it; a launcher started from LAUNCH.bat
  reads none, and the tool says so.
* Cargo layouts use Cargo Loader's own ship-grid cache as it is on disk;
  refreshing that cache is done by opening Cargo Loader.
