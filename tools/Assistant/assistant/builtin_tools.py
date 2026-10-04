"""The Assistant's toolset: every toolbox tool, callable headlessly.

Two kinds of tools:

  * Information (confirm=False): answered without opening any window.
    Each runs in its tool's own worker subprocess (worker_pool.py), so the
    Mission DB's ``data`` package never meets Market Finder's.
  * Action (confirm=True): show_route_popup, open_trade_hub, launch_tool.
    These change what is on the user's screen, so the agent asks first.
  * starmap_command: relays a spoken map command to the Star Map, which has
    no microphone of its own (voice-to-text lives here). Not confirm-gated:
    it was a direct voice command when the map had its own ears, and asking
    "say yes or no" before every "zoom in" would make it unusable.

Descriptions are written for a small local model: each says WHEN to use
the tool in the user's own words, and what comes back.

Phase B (guns only): best_ship_weapons picks the best gun per hardpoint
from scunpacked-data. Full builds (power, shields, coolers) are not here.
"""
from __future__ import annotations

import logging

from . import headless, ipc_bus, starmap_bridge
from shared.scunpacked import ATTRIBUTION as _SCUNPACKED_ATTRIBUTION
from .tools import ToolContext, ToolError, ToolRegistry, tool

log = logging.getLogger(__name__)


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    for t in (_ship_info, _find_trade_routes, _find_item_price, _ship_buy_rent,
              _missions_for_blueprint, _where_to_mine, _search_missions,
              _blueprint_recipe, _identify_signal, _mining_loadout_stats,
              _cargo_layout, _jump_route, _current_loadout, _playtime_summary,
              _best_ship_weapons, _starmap_command,
              _show_route_popup, _open_trade_hub, _launch_tool):
        reg.register(t)
    return reg


def _w(ctx: ToolContext, key: str, fn: str, timeout: float = 90.0, **args):
    return headless.call(ctx.base_dir, key, fn, args, timeout=timeout)


# ── ships ─────────────────────────────────────────────────────────────────

@tool(
    name="ship_info",
    description="Cargo capacity in SCU of a Star Citizen ship. Use for "
                "'how much does a Caterpillar hold'.",
    params={"name": {"type": "string", "description": "Ship name, e.g. Caterpillar"}},
    required=["name"],
)
def _ship_info(ctx: ToolContext, name: str) -> dict:
    return headless.ship_info(ctx.base_dir, name)


@tool(
    name="ship_buy_rent",
    description="Where a ship can be bought or rented in-game with aUEC, and "
                "the price. Use for 'where can I buy a Cutlass', 'rent a Prospector'.",
    params={"ship": {"type": "string", "description": "Ship name, e.g. Cutlass Black"}},
    required=["ship"],
)
def _ship_buy_rent(ctx: ToolContext, ship: str) -> dict:
    return _w(ctx, "market", "ship_buy_rent", ship=ship)


@tool(
    name="best_ship_weapons",
    description=(
        "Best gun for every gun hardpoint of a ship, by sustained DPS, burst "
        "DPS or alpha. Use for 'best loadout for a Gladius', 'what guns should "
        "I put on my Arrow', 'max burst build for a Hammerhead'. GUNS ONLY "
        "(no missiles, power, shields or coolers) and an upper bound: the "
        "ship's weapon power pool is not modelled. Returns each slot with its "
        "size and the chosen weapon, totals, and the data build. "
        + _SCUNPACKED_ATTRIBUTION),
    params={
        "ship": {"type": "string", "description": "Ship name, e.g. Gladius or Cutlass Black"},
        "goal": {"type": "string", "enum": ["sustained", "burst", "alpha"],
                 "description": "What to maximise: sustained (default), burst or alpha"},
        "realistic": {"type": "boolean",
                      "description": "Default false: scatter guns count every pellet hitting. "
                                     "True: scatter guns are rated by an ESTIMATE of the pellets "
                                     "that hit a target at a range (from the gun's spread cone); "
                                     "other guns are unchanged"},
        "range_m": {"type": "number",
                    "description": "Realistic mode: engagement range in metres (default 300)"},
        "target_size_m": {"type": "number",
                          "description": "Realistic mode: target width in metres "
                                         "(default 10, a light/medium fighter)"},
    },
    required=["ship"],
)
def _best_ship_weapons(ctx: ToolContext, ship: str, goal: str = "sustained",
                       realistic: bool = False, range_m: float = None,
                       target_size_m: float = None) -> dict:
    args = {"ship": ship, "goal": goal}
    if realistic:
        args.update(realistic=True, range_m=range_m, target_size_m=target_size_m)
    return _w(ctx, "dps", "best_ship_weapons", **args)


# ── trading and prices ────────────────────────────────────────────────────

@tool(
    name="find_trade_routes",
    description=(
        "Most profitable cargo trade routes right now for a ship, from live "
        "UEX prices. Use for trading, cargo runs, 'best trade route for a "
        "Caterpillar'. Returns commodity, buy and sell terminal with system, "
        "prices, how many SCU, and estimated profit."),
    params={
        "ship": {"type": "string", "description": "Ship name; its cargo size sizes the profit"},
        "commodity": {"type": "string", "description": "Optional: only this commodity, e.g. Gold"},
        "system": {"type": "string", "description": "Optional: star system, e.g. Stanton or Pyro"},
        "top_n": {"type": "integer", "description": "How many routes (default 5, max 20)"},
        "allow_illegal": {"type": "boolean", "description": "Include illegal goods (default true)"},
    },
    required=["ship"],
)
def _find_trade_routes(ctx: ToolContext, ship: str, commodity: str = "", system: str = "",
                       top_n: int = 5, allow_illegal: bool = True) -> dict:
    return _w(ctx, "trade", "find_trade_routes", ship=ship, commodity=commodity,
              system=system, top_n=top_n, allow_illegal=allow_illegal)


@tool(
    name="find_item_price",
    description=(
        "Cheapest shops to buy an item (weapon, armor, ship component, "
        "attachment, food) and where it sells, from UEX. Use for 'where do I "
        "buy a P4-AR', 'how much is a ...'."),
    params={
        "item": {"type": "string", "description": "Item name, e.g. P4-AR"},
        "top_n": {"type": "integer", "description": "How many shops (default 5)"},
    },
    required=["item"],
)
def _find_item_price(ctx: ToolContext, item: str, top_n: int = 5) -> dict:
    return _w(ctx, "market", "find_item_price", item=item, top_n=top_n)


# ── missions, mining, crafting ────────────────────────────────────────────

@tool(
    name="missions_for_blueprint",
    description=(
        "Which missions reward a crafting blueprint. Use for 'what mission "
        "gives the Trawler Scraper Module blueprint', 'how do I get the ... "
        "blueprint'. Returns mission titles, faction, systems, reward."),
    params={"name": {"type": "string",
                     "description": "Blueprint / item name, e.g. Trawler Scraper Module"}},
    required=["name"],
)
def _missions_for_blueprint(ctx: ToolContext, name: str) -> dict:
    return _w(ctx, "missions", "missions_for_blueprint", timeout=150, name=name)


@tool(
    name="where_to_mine",
    description=(
        "Where a mineable resource is found: planets, moons, belts, and "
        "whether it is ship, hand (FPS) or ROC mining. Use for 'where do I "
        "find Quantanium', 'where to mine Hadanite'."),
    params={"resource": {"type": "string", "description": "Resource name, e.g. Quantanium"}},
    required=["resource"],
)
def _where_to_mine(ctx: ToolContext, resource: str) -> dict:
    return _w(ctx, "missions", "where_to_mine", timeout=150, resource=resource)


@tool(
    name="search_missions",
    description=(
        "Find missions (contracts) by faction, star system and/or type, "
        "highest paying first. Use for 'bounty missions in Pyro', 'what "
        "missions does Headhunters give'. Give at least one filter."),
    params={
        "faction": {"type": "string", "description": "Optional faction, e.g. Headhunters"},
        "system": {"type": "string", "description": "Optional system: Stanton, Pyro or Nyx"},
        "mission_type": {"type": "string",
                         "description": "Optional type, e.g. Bounty Hunter, Salvage, Delivery"},
    },
)
def _search_missions(ctx: ToolContext, faction: str = "", system: str = "",
                     mission_type: str = "") -> dict:
    if not (faction or system or mission_type):
        raise ToolError("give a faction, a system or a mission type")
    return _w(ctx, "missions", "search_missions", timeout=150, faction=faction,
              system=system, mission_type=mission_type)


@tool(
    name="blueprint_recipe",
    description=(
        "Crafting recipe for a blueprint: ingredients with amounts, craft "
        "time, and missions that drop it. Use for 'what do I need to craft "
        "a P4-AR', 'recipe for ...'."),
    params={"name": {"type": "string", "description": "Blueprint / item name"}},
    required=["name"],
)
def _blueprint_recipe(ctx: ToolContext, name: str) -> dict:
    return _w(ctx, "craft", "blueprint_recipe", name=name)


@tool(
    name="identify_signal",
    description=(
        "What a mining scanner signal number means: which resource and how "
        "many rocks. Use when the user reads out a number like 8620 from "
        "their scanner."),
    params={"value": {"type": "string",
                      "description": "The signal number as read, e.g. 8620"}},
    required=["value"],
)
def _identify_signal(ctx: ToolContext, value: str) -> dict:
    return _w(ctx, "signals", "identify_signal", value=value)


@tool(
    name="mining_loadout_stats",
    description=(
        "Stats and price of a mining ship loadout (laser, modules, gadget) "
        "for a Prospector, MOLE or Golem. Use for 'how strong is a "
        "Prospector with a Helix 1 and two Surge modules'."),
    params={
        "ship": {"type": "string", "description": "Prospector, MOLE or Golem"},
        "laser": {"type": "string", "description": "Laser name; omit for the stock laser"},
        "modules": {"type": "array", "items": {"type": "string"},
                    "description": "Module names, e.g. [\"Surge\", \"Focus III\"]"},
        "gadget": {"type": "string", "description": "Optional gadget, e.g. Sabir"},
    },
    required=["ship"],
)
def _mining_loadout_stats(ctx: ToolContext, ship: str, laser: str = "",
                          modules: list = None, gadget: str = "") -> dict:
    return _w(ctx, "mining", "mining_loadout_stats", ship=ship, laser=laser,
              modules=list(modules or []), gadget=gadget)


# ── cargo, navigation, the player ─────────────────────────────────────────

@tool(
    name="cargo_layout",
    description=(
        "How to fill a ship's cargo grid with containers: which box sizes "
        "and how many. Use for 'how do I load my Hercules', 'cargo layout "
        "for a Caterpillar'."),
    params={"ship": {"type": "string", "description": "Ship name"}},
    required=["ship"],
)
def _cargo_layout(ctx: ToolContext, ship: str) -> dict:
    return _w(ctx, "cargo", "cargo_layout", ship=ship)


@tool(
    name="jump_route",
    description=(
        "Jump-point route between two star systems, and whether every hop "
        "is in the game today. Use for 'jump route from Stanton to Pyro', "
        "'how do I get to Nyx'."),
    params={
        "from_system": {"type": "string", "description": "Start system, e.g. Stanton"},
        "to_system": {"type": "string", "description": "Destination system, e.g. Pyro"},
    },
    required=["from_system", "to_system"],
)
def _jump_route(ctx: ToolContext, from_system: str, to_system: str) -> dict:
    return _w(ctx, "starmap", "jump_route", from_system=from_system, to_system=to_system)


@tool(
    name="current_loadout",
    description=(
        "The player's current FPS loadout from the game log: weapons, spare "
        "magazines, med and oxy pens, grenades. Use for 'what's in my "
        "loadout', 'how many medpens do I have'."),
    params={},
)
def _current_loadout(ctx: ToolContext) -> dict:
    return _w(ctx, "battle_buddy", "current_loadout")


@tool(
    name="playtime_summary",
    description=(
        "How long the player has played Star Citizen in total, plus "
        "sessions, streaks and longest session. Use for 'how many hours "
        "have I played'."),
    params={},
)
def _playtime_summary(ctx: ToolContext) -> dict:
    return _w(ctx, "playtime", "playtime_summary", timeout=120)


# ── the Star Map's commands (voice lives here; the map has no mic) ───────

@tool(
    name="starmap_command",
    description=(
        "Tell the open Star Map to do something: 'navigate to Area 18', 'set route "
        "to Port Tressler', 'route to Pyro', 'clear route', 'zoom in', 'zoom out', "
        "'back to galaxy', 'take me home', 'open the shopping list'. Pass the "
        "command in the user's own words. Use jump_route instead when the user "
        "only asks how many jumps or which systems lie between two systems."),
    params={"command": {"type": "string",
                        "description": "The map command as spoken, e.g. navigate to Area 18"}},
    required=["command"],
)
def _starmap_command(ctx: ToolContext, command: str) -> dict:
    """Relay one command to the Star Map and report what it answered.

    The map's later narration (the in-game route macro says each step) is
    spoken through ctx.speak, i.e. by the Assistant's own mouth: its ears
    know to ignore that voice, and would hear a second one as the user."""
    command = (command or "").strip()
    if not command:
        raise ToolError("no map command was given")
    return starmap_bridge.send_command(command, on_say=ctx.speak)


# ── actions (ask first) ───────────────────────────────────────────────────

@tool(
    name="show_route_popup",
    description=(
        "Show a trade route as a pinned popup in Trade Hub, and draw it on "
        "the star map. Call this only after the user explicitly agrees to "
        "have a route pinned."
    ),
    params={
        "route": {"type": "object",
                  "description": "One route dict exactly as returned by find_trade_routes"},
        "ship": {"type": "string", "description": "Ship name the route was computed for"},
        "show_on_map": {"type": "boolean",
                        "description": "Also draw the route on the Trade Hub star map (default true)"},
    },
    required=["route"],
    confirm=True,
    action="pin that route in Trade Hub",
)
def _show_route_popup(ctx: ToolContext, route: dict, ship: str = "",
                      show_on_map: bool = True) -> dict:
    if not isinstance(route, dict) or "buy_location" not in route:
        raise ToolError("route must be one dict from find_trade_routes")
    scu = headless.ship_scu(ctx.base_dir, ship) if ship else 0
    data = headless.route_popup_data(route, ship=ship, ship_scu=scu)

    if not ipc_bus.ensure_trade_hub(ctx.base_dir, show=True):
        raise ToolError("Trade Hub could not be started")
    if not ipc_bus.wait_ready("trade"):
        raise ToolError("Trade Hub did not come up in time")

    ipc_bus.send("trade", {"type": "show"})
    ok = ipc_bus.send("trade", {
        "type": "route_detail",
        "data": data,
        "pin": True,
        "show_on_map": bool(show_on_map),
    })
    if not ok:
        raise ToolError("could not reach Trade Hub")
    comm = data.get("commodity", "route")
    return {"pinned": True,
            "message": f"Pinned {comm} popup in Trade Hub"}


@tool(
    name="open_trade_hub",
    description="Open (or focus) the Trade Hub tool window.",
    params={},
    confirm=True,
    action="open Trade Hub",
)
def _open_trade_hub(ctx: ToolContext) -> dict:
    if not ipc_bus.ensure_trade_hub(ctx.base_dir, show=True):
        raise ToolError("Trade Hub could not be started")
    ipc_bus.wait_ready("trade")
    ipc_bus.send("trade", {"type": "show"})
    return {"opened": True}


@tool(
    name="launch_tool",
    description=(
        "Open one of the toolbox's tool windows: Trade Hub, Item Finder, "
        "Mission DB, Craft Database, Mining Loadout, Mining Signals, Cargo "
        "Loader, DPS Calculator, Battle Buddy, PlayTime, Starmap, Dev "
        "History, Mouse Blocker, SuitMk2. Only when the user asks to open "
        "or show a tool."),
    params={"name": {"type": "string", "description": "Tool name, e.g. Mining Signals"}},
    required=["name"],
    confirm=True,
    action="open {name}",
)
def _launch_tool(ctx: ToolContext, name: str) -> dict:
    """Open a tool window.

    Three paths, in order of preference:
      1. already running -> IPC ``show`` (no second window, no cold start);
      2. the launcher reads a command file -> ``launch_skill``, so the
         launcher owns the process and its tile state stays right;
      3. otherwise spawn it ourselves (ipc_bus.spawn_skill).

    (3) is not a nicety: LAUNCH.bat and SC_Toolbox.vbs both start the
    launcher without a command file, so (2) is unavailable in the normal
    way people run the toolbox, and without (3) this tool can open nothing
    at all. Its cost is that the launcher does not know about the window,
    so a later hotkey press can open a second copy — said in the result so
    the assistant can pass it on.
    """
    skill = headless.resolve_skill(ctx.base_dir, name)
    sid, label = skill["id"], skill["name"]

    if sid == "assistant":
        # Path 3 below would start a SECOND assistant, which then competes
        # for the microphone with this one. Nothing to open: it is me.
        return {"launched": label, "skill_id": sid, "via": "already running",
                "note": "that is me, already open"}

    if ipc_bus.is_running(sid):
        ipc_bus.send(sid, {"type": "show"})
        return {"launched": label, "skill_id": sid, "via": "already running"}

    if ipc_bus.launcher_cmd_file():
        if not ipc_bus.send_to_launcher({"type": "launch_skill", "skill_id": sid}):
            raise ToolError(f"could not reach the launcher to open {label}")
        return {"launched": label, "skill_id": sid, "via": "launcher"}

    if not ipc_bus.spawn_skill(ctx.base_dir, sid):
        raise ToolError(f"could not start {label} — its entry script is missing "
                        f"or would not launch")
    if not ipc_bus.wait_ready(sid, timeout=20.0):
        raise ToolError(f"{label} was started but did not come up in time")
    return {"launched": label, "skill_id": sid, "via": "spawned directly",
            "note": "the launcher was not listening for commands, so I started "
                    "it myself; its launcher tile will not show it as running"}
