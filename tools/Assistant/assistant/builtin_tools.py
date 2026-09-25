"""The reference toolset — what the assistant can do out of the box.

Two kinds of tools:

  * Information (confirm=False): ship_info, find_trade_routes,
    find_market_price. These summon data headlessly — no window opens.
  * Action (confirm=True): show_route_popup, open_trade_hub. These act on
    the user's screen, so the agent must ask first.

To extend the toolbox: write a function, decorate it with @tool, and
register it in build_default_registry(). The LLM sees it immediately.
"""
from __future__ import annotations

import logging

from . import headless, ipc_bus
from .tools import ToolContext, ToolError, ToolRegistry, tool

log = logging.getLogger(__name__)


def build_default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(_ship_info)
    reg.register(_find_trade_routes)
    reg.register(_find_market_price)
    reg.register(_show_route_popup)
    reg.register(_open_trade_hub)
    return reg


@tool(
    name="ship_info",
    description="Cargo capacity (SCU) and preset info for a Star Citizen ship.",
    params={
        "name": {"type": "string", "description": "Ship name, e.g. Caterpillar"},
    },
    required=["name"],
)
def _ship_info(ctx: ToolContext, name: str) -> dict:
    return headless.ship_info(ctx.base_dir, name)


@tool(
    name="find_trade_routes",
    description=(
        "Find the most profitable trade routes right now, ranked by "
        "estimated profit for the given ship. Use this whenever the user "
        "asks about cargo runs, freight, trading, or the best route for a "
        "ship. Returns buy/sell locations, prices, stock, margin and "
        "estimated profit per route."
    ),
    params={
        "ship": {"type": "string",
                 "description": "Ship name — its cargo SCU sizes the profit, e.g. Caterpillar"},
        "commodity": {"type": "string", "description": "Optional commodity filter"},
        "system": {"type": "string", "description": "Optional star system filter, e.g. Stanton"},
        "top_n": {"type": "integer", "description": "Routes to return (1-20, default 5)"},
        "allow_illegal": {"type": "boolean", "description": "Include vice/illegal goods (default true)"},
    },
    required=["ship"],
)
def _find_trade_routes(ctx: ToolContext, ship: str, commodity: str = "",
                       system: str = "", top_n: int = 5,
                       allow_illegal: bool = True) -> dict:
    routes = headless.find_trade_routes(
        ctx.base_dir, ship=ship, commodity=commodity, system=system,
        top_n=top_n, allow_illegal=allow_illegal)
    return {"routes": routes,
            "note": "est_profit assumes a full effective SCU load"}


@tool(
    name="find_market_price",
    description="Cheapest places to buy an item (weapons, armor, components) from UEX market data.",
    params={
        "item_name": {"type": "string", "description": "Item name, e.g. P4-AR rifle"},
        "top_n": {"type": "integer", "description": "How many offers (default 5)"},
    },
    required=["item_name"],
)
def _find_market_price(ctx: ToolContext, item_name: str, top_n: int = 5) -> dict:
    return headless.find_market_price(ctx.base_dir, item_name, top_n=top_n)


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

    # Re-send show after the spawn — the window is up now.
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
)
def _open_trade_hub(ctx: ToolContext) -> dict:
    if not ipc_bus.ensure_trade_hub(ctx.base_dir, show=True):
        raise ToolError("Trade Hub could not be started")
    ipc_bus.wait_ready("trade")
    ipc_bus.send("trade", {"type": "show"})
    return {"opened": True}
