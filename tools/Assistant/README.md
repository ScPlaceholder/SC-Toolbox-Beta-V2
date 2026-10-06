# Toolbox Assistant (toolbox-wide LLM voice copilot)

The user talks; the assistant summons toolbox data through tools and
answers. Side effects (pinning a route popup, opening Trade Hub) always
ask the user first.

**This is the toolbox's one microphone.** The Star Map used to have its own
voice ears; since 2026-10-04 it has none, and what is said for the map
("zoom in", "route to Pyro", "star map, show Hurston") is relayed to it
from here. See "Star Map commands" below. Setting a route in the game
("navigate to Area 18") is this tool's own: see "Set route".

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
  holds it on its command line; talks to the launcher; and spawns any
  discovered skill itself (`spawn_plan` / `spawn_skill` / `ensure_skill`)
  with the launcher's own argv contract when the launcher reads no commands
* `assistant/agent.py`      - conversation loop + yes/no confirmation gate
* `assistant/voice.py`      - ears (mic + faster-whisper) + mouth (SAPI TTS), optional deps
* `assistant/starmap_bridge.py` - the Star Map's voice: which utterances are map
  commands, relaying them over IPC and saying the map's answer, and the
  one-time move of the Star Map's saved mic settings
* `assistant/starmap_ears/` - the Star Map's former ears, kept but not imported
  (joystick / gamepad mic triggers live only there; see its `__init__`)
* `assistant/panel.py`      - the HUD window
* `assistant/selftest.py`   - `python -m assistant.selftest` from `tools/Assistant`

## Tools

| Tool | Source tool | Asks first |
|---|---|---|
| `ship_info(name)` | shared ship presets | |
| `find_trade_routes(ship, commodity?, system?, top_n=5, allow_illegal=true)` | Trade Hub (`commodities_prices_all`) | |
| `find_item_price(item, top_n=5)` | Item Finder | |
| `ship_buy_rent(ship)` | Item Finder | |
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
| `starmap_command(command)` | Star Map window (or the Everything Finder's Star Map tab) | |
| `set_route(destination)` | `assistant/set_route/` (here; no other tool needed). Looks the destination up, touches nothing | |
| `plot_route_in_game(destination)` | `assistant/set_route/` in-game macro: mouse and keyboard in Star Citizen | yes, and In-Game must be on |
| `show_route_popup(route, ship?, show_on_map?)` | Trade Hub window | yes |
| `open_trade_hub()` | Trade Hub window | yes |
| `launch_tool(name)` | launcher `launch_skill`, else a direct spawn | yes |

Names are fuzzy-matched inside the tools ("quantanium" finds
"Quantainium (Raw)", "helix 1" finds "Helix I Mining Laser"). A result
with `"empty": true` is a valid "nothing matched", not an error.
DPS / optimal ship builds are not wired yet.

## Star Map commands

Voice-to-text lives here and nowhere else. A map phrase is recognised by rule
(`starmap_bridge.command_text`), before any intent scoring:

* said plainly: *route to <system>* (a jump route drawn on the map), *clear
  route*, *zoom in / out*, *back to galaxy*, *take me home*, *open the
  shopping list*;
* anything else the map understands, with **star map** in front: *star map,
  show Hurston*, *star map, commodities*, *star map, help*.

`starmap_command` sends `{"type": "map_command", ...}` to the standalone Star
Map if it is running, else to the Everything Finder (which opens its Star Map
tab), waits up to 6 s for the map's answer in a temp reply file, and says it.
The map never speaks itself: its ears-era TTS is gone, because a second voice
would come back through this window's open mic as the user's next utterance.
It is not confirm-gated: it only changes what the map window shows. If no
Star Map is open, it says so; it does not open one.

## Set route (in the game)

*Navigate to / set route to / set course to / plot a course to <destination>*
and *route to <a place that is not a star system>* set a route inside Star
Citizen. The code is here, `assistant/set_route/` (moved from the Star Map on
2026-10-04), and it is the only copy: the Star Map's typed "navigate to ..."
and its In-Game button call it through `starmap/set_route_link.py`. It works
with the Star Map closed. With "star map," in front it is still handled here.

What happens, in order:

1. `set_route` resolves the destination (J's phonetic engine and destination
   list). An unknown or ambiguous name is said and nothing else happens.
2. If the **In-Game** switch is off, it says so and stops. Nothing is asked.
3. Otherwise it asks: "Area18. Want me to set that route in the game? Say yes
   or no." Nothing has been sent to the game yet.
4. On yes, `plot_route_in_game` runs the macro (F2, clicks, a clipboard paste,
   R x6, F2) and narrates its steps. The switch is read again at this point,
   inside `RouteService.plot`, which is the only caller of the macro.

**In-Game** and **Calibrate Route** are buttons in this window. In-Game is one
saved switch (`~/.sctoolbox/set_route/settings.json`) shared with the Star
Map's In-Game button; until it is first set here, the Star Map's old saved
choice is used. Calibrate Route is the 3-click calibration that used to be the
Star Map's "Calibrate Star Map"; it is only here now. It saves the three click
positions in the pilot's own folder (`~/.sctoolbox/set_route/mouse_calibration.json`),
which an update leaves alone. Until the pilot calibrates here, a calibration
made with the WingmanAI skill is read from `tools/set_route_ai/data/` where that
folder exists (a developer's checkout; an installed toolbox has none). There
are no default positions: with no calibration nothing is clicked, and the
Assistant says to press Calibrate Route. No calibration file ships.

What it needs: pynput, the Windows clipboard, Star Citizen in the foreground
with its star map on F2, and calibrated click positions. It does not check
that the game has focus: the keys and clicks go to whatever window is in
front. It needs nothing from a running Star Map. If an open Star Map exists it
is sent `{"type": "map_goto", "name": ...}` so its view follows; that is all.

The macro has never been run end to end in the game from here. The tests run
the real macro on a fake `pynput` and a fake clipboard.

*Stop listening* / *ears off* closes an always-open mic (drops to
push-to-talk). That was a Star Map command; the mic is this window's now.

**The Star Map's saved mic settings** are folded in once, at the first launch
after the move (`migrate_starmap_voice`). A setting this window already has is
the user's own choice and is kept; a Star Map setting only fills a gap; what
was not applied is shown in the window once and recorded under
`starmap_voice_migrated` in `~/.sctoolbox/assistant_panel.json`. The Star
Map's file is only read. Not carried over: a joystick or gamepad mic trigger
(the Assistant binds keyboard keys and side mouse buttons only).

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

* If the assistant spawns a tool itself and the launcher later spawns its
  own copy (hotkey), two windows of that tool can coexist until restart:
  the launcher has no way to be told about a process it did not start.
  `launch_tool` prefers the launcher whenever one is listening, and says
  in its result when it fell back to spawning.
* Voice deps (`sounddevice faster-whisper numpy pynput`) are optional;
  without them the ears button reports what to `pip install`.
* `launch_tool` prefers the launcher's `launch_skill` IPC, but the launcher
  only reads a command file when WingmanAI's `main.py` started it (it
  appends one as argv[6]). **LAUNCH.bat passes the literal `nul` and
  SC_Toolbox.vbs passes no args**, so in both of the ways a person starts
  the toolbox by hand, `skill_launcher.py` takes its `cmd_file ==
  os.devnull` branch and never starts the IPC reader. Until 2026-09-26
  that made `launch_tool` fail for all fourteen tools, leaving Trade Hub
  the only one the assistant could open (via `ensure_trade_hub`'s direct
  spawn). `ipc_bus.spawn_skill` now covers the other thirteen. Giving the
  launcher a real command file in LAUNCH.bat / the .vbs would restore the
  better path and is a launcher-side change, not an assistant one.
* Cargo layouts use Cargo Loader's own ship-grid cache as it is on disk;
  refreshing that cache is done by opening Cargo Loader.
