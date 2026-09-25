"""Mining Loadout worker: stats and price of a laser/module/gadget build.

Items come from UEX via services.api_client.fetch_mining_data with
use_cache=False (that switch is what keeps it from writing the tool's
disk cache); the list is kept warm in this process for an hour.
The same laser and modules are fitted to every turret of the ship.
"""
from __future__ import annotations

import time

import _assist_logic as L

from models.items import SHIPS
from services.api_client import fetch_mining_data
from services.calc_service import calc_loadout_price, calc_stats

_state = {"lasers": None, "modules": None, "gadgets": None, "ts": 0.0}
_TTL = 3600.0


def _items():
    if _state["lasers"] is None or time.time() - _state["ts"] > _TTL:
        lasers, modules, gadgets = fetch_mining_data(use_cache=False)
        if not lasers:
            raise L.ToolFail("UEX returned no mining lasers; try again in a moment")
        _state.update(lasers=lasers, modules=modules, gadgets=gadgets, ts=time.time())
    return _state["lasers"], _state["modules"], _state["gadgets"]


def mining_loadout_stats(ship: str, laser: str = "", modules=None, gadget: str = "") -> dict:
    lasers, mods, gadgets = _items()
    ship_key, _ = L.resolve_one(ship, SHIPS.keys(), what="mining ship (Prospector, MOLE, Golem)")
    cfg = SHIPS[ship_key]
    fit = [l for l in lasers if not cfg.laser_size or l.size in (0, cfg.laser_size)]
    if laser:
        las, _ = L.resolve_one(laser, fit, key=lambda x: x.name,
                               what=f"size {cfg.laser_size} mining laser")
    else:
        las = next((l for l in lasers if l.name == cfg.stock_laser), None)
        if las is None:
            raise L.ToolFail(f"say which laser; stock laser {cfg.stock_laser!r} not in UEX data")
    if isinstance(modules, str):
        modules = [m for m in modules.split(",") if m.strip()]
    slots = int(getattr(las, "module_slots", 0) or cfg.module_slots or 0)
    picked = []
    for q in (modules or [])[:slots]:
        mod, _ = L.resolve_one(str(q), mods, key=lambda x: x.name, what="mining module")
        picked.append(mod)
    dropped = max(0, len(modules or []) - slots)
    gad = None
    if gadget:
        gad, _ = L.resolve_one(gadget, gadgets, key=lambda x: x.name, what="mining gadget")
    laser_items = [las] * cfg.turrets
    module_items = [list(picked) + [None] * (slots - len(picked)) for _ in range(cfg.turrets)]
    stats = calc_stats(ship_key, laser_items, module_items, gad)
    price = calc_loadout_price(ship_key, laser_items, module_items, gad)
    res = {
        "ship": ship_key, "turrets": cfg.turrets,
        "laser": las.name, "laser_size": las.size,
        "modules": [m.name for m in picked], "module_slots_per_laser": slots,
        "gadget": gad.name if gad else None,
        "stats": {k: round(v, 2) for k, v in stats.items()},
        "price_auec": round(price),
        "note": "same laser and modules on every turret; price excludes the stock laser",
    }
    if dropped:
        res["warning"] = f"{dropped} module(s) ignored: this laser has only {slots} slot(s)"
    return res


EXPORTS = {"mining_loadout_stats": mining_loadout_stats}
