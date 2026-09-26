"""Fun-stats engine — mines Star Citizen Game.logs for gameplay trivia.

Unlike the play-time scanner (which only reads each file's first/last line), this
reads full log contents to extract events that are still emitted by current
builds:

* **Handle**        — ``<Legacy login response> ... Handle[Name]``.
* **Ships flown**   — ``<Vehicle Control Flow> ... Local client node [id] ...
  control token for 'MANU_Ship_<instance>'`` (the local player taking the helm;
  every such line is the local player, so all count).
* **Loadout / gear** — ``<EquipItem> ... Class[item]``: favourite weapon (by how
  often you equip it / its magazines), consumables jabbed, multitool heads,
  plushies collected, total loadout changes.
* **Money activities** — hauling / mining / salvage / bounty / mercenary markers.
* **Systems** — which solar systems were actually visited (Stanton, Pyro, Nyx…),
  by reference density rather than star-map mentions.
* **Injuries** — HUD injury notifications (body part + tier) and med bed
  surgeries; parsed by :mod:`core.injuries`.

NOTE: the old ``<Actor Death>`` kill/death feed was removed from the game logs
around Nov 2025, so kill counts, K/D and "nemesis" are no longer tracked and are
deliberately omitted.

Full scans are expensive, so per-file aggregates are cached by (size, mtime);
the cache carries a version so a parser change invalidates stale entries.
"""
from __future__ import annotations

import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import settings as _settings
from . import injuries as _injuries

log = logging.getLogger(__name__)

_CACHE_VERSION = 7  # bump when the per-file parse changes (7: injuries)

# ── Regexes ──────────────────────────────────────────────────────────────────
_HANDLE_RE = re.compile(r"Handle\[([^\]]+)\]")
_SHIP_RE = re.compile(
    r"Local client node \[(\d+)\] (?:requesting|granted) control token "
    r"for '([A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*?)_(\d{5,})'")
_EQUIP_RE = re.compile(r"<EquipItem>.*?Class\[([A-Za-z0-9_]+)\]")
# Mission lifecycle: completion type per MissionId, and the mission "generator
# name" (which encodes the contractor + objective, e.g. BountyHuntersGuild_KillShip).
_ENDMISSION_RE = re.compile(
    r"<EndMission>.*?MissionId\[([0-9a-fA-F-]+)\].*?CompletionType\[([A-Za-z]+)\]")
_MARKER_RE = re.compile(
    r"missionId \[([0-9a-fA-F-]+)\], generator name \[([A-Za-z0-9_]+)\]")
# Commodity sale at a trade terminal (sell request — may be retried/failed).
_SELL_RE = re.compile(
    r"SendCommoditySellRequest.*?shopName\[([A-Za-z0-9_]+)\]")

_MANUFACTURERS = {
    "ANVL": "Anvil", "AEGS": "Aegis", "ARGO": "Argo", "BANU": "Banu",
    "CNOU": "Consolidated Outland", "CRUS": "Crusader", "DRAK": "Drake",
    "ESPR": "Esperia", "GAMA": "Gatac", "GATAC": "Gatac", "GRIN": "Greycat",
    "KRIG": "Kruger", "MIRA": "Mirai", "MRAI": "Mirai", "MISC": "MISC",
    "ORIG": "Origin", "RSI": "RSI", "TMBL": "Tumbril", "VNCL": "Vanduul",
    "XIAN": "Xi'an", "XNAA": "Aopoa", "AOPOA": "Aopoa", "KRGR": "Kruger",
}

_ACTIVITY_MARKERS = {
    "Hauling": ("createhaulingobjectivehandler",),
    "Bounty Hunting": ("bountyhunter", "bounty_beacon"),
    "Mining": ("prospector", "argo_mole", "_golem", "grin_roc"),
    "Salvage": ("reclaimer", "vulture", "hullscraper"),
    "Mercenary": ("mercenary",),
    "Smuggling": ("smuggl",),
}

# Solar systems: distinctive log token + the per-file occurrence count above
# which the player was actually *there* (not just incidental star-map / jump-
# gateway references).  Visited systems show hundreds of hits/session; nav-only
# gateways (Magnus, Terra, Castra…) only a handful, so the threshold separates
# them.  Order is the display order.  Add new live systems here as CIG ships them.
_SYSTEM_TOKENS = (
    ("Stanton", "stanton", 200),
    ("Pyro", "pyro", 400),
    ("Nyx", "nyx", 80),   # Nyx isn't on Stanton's star map, so refs ≈ real presence
)

_WEAPON_TOKENS = {"rifle", "pistol", "smg", "lmg", "sniper", "shotgun",
                  "gren", "knife", "launcher", "ballistic", "energy"}
_WEAPON_CORE = {"rifle", "pistol", "smg", "lmg", "sniper", "shotgun",
                "gren", "knife", "launcher"}


# Mission "generator name" -> contract category.  Checked in order; the first
# keyword that matches wins (action verbs before contractor names).
_MISSION_CATEGORY_RULES = (
    ("Hauling", ("hauling", "covalex", "redwind")),
    ("Bounty", ("bounty", "killship", "shipwave", "enforcement", "foxwell")),
    ("Mercenary", ("intersec", "patrol", "mercenary", "defen")),
    ("Investigation", ("recover", "missingperson", "delve", "hockrow",
                       "ftl", "investigat", "data")),
    ("Delivery", ("collector", "delivery", "courier", "fetch")),
    ("Event / Story", ("event", "finale", "luminalia", "content",
                       "wtp", "_ch", "fffinale", "2025")),
)


def _mission_category(gen: Optional[str]) -> str:
    if not gen:
        return "Other"
    g = gen.lower()
    for name, keys in _MISSION_CATEGORY_RULES:
        if any(k in g for k in keys):
            return name
    return "Other"


def _mission_employer(gen: Optional[str]) -> str:
    """Contractor name from the generator (the part before the objective verb)."""
    if not gen:
        return "Unknown"
    # First token before the underscored objective/suffix.
    base = re.split(r"_(generator|hauling|killship|shipwave|patrol|recover"
                    r"|missingperson|facilitydelve|delve|enforcement|phase\d*"
                    r"|stanton|pyro| kill|data|item)", gen, flags=re.I)[0]
    base = re.sub(r"_+$", "", base) or gen.split("_")[0]
    # camelCase -> spaced (BountyHuntersGuild -> Bounty Hunters Guild)
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", base).replace("_", " ").strip()


_TRADE_LOCATIONS = (
    "Lorville", "Area18", "NewBabbage", "Orison", "GrimHex", "Levski",
    "Olisar", "Everus", "Baijini", "Tressler", "Seraphim", "Rayari", "Shubin",
    "Ruin", "Checkmate", "Bloom", "Orbituary", "Endgame", "Patch", "RestStop",
)


def _pretty_shop(name: str) -> str:
    """Best-effort trading-location label from a messy shop id.

    'SCShop_CommEx_TDD_Orison' -> 'Orison'; unknown outposts -> 'Outpost'.
    """
    low = name.lower()
    for loc in _TRADE_LOCATIONS:
        if loc.lower() in low:
            return "New Babbage" if loc == "NewBabbage" else (
                "Rest Stop" if loc == "RestStop" else loc)
    if "pyro" in low:
        return "Pyro outpost"
    return "Outpost"

def _clean_weapon(raw: str) -> str:
    """Reduce a weapon/magazine entity name to a groupable base id.

    'volt_lmg_energy_01_mag' -> 'volt_lmg_energy_01'
    'ksar_pistol_ballistic_01_iae2023' -> 'ksar_pistol_ballistic_01'
    """
    w = re.sub(r"_(mag|magazine)$", "", raw)
    w = re.sub(r"_\d{4,}$", "", w)
    w = re.sub(r"_(store|tint|black|white|default|skin|loaner|iae\w*|"
               r"firerats\w*|fps|cz|contestedzonereward)\w*$", "", w, flags=re.I)
    return w


def _pretty_weapon(base: str) -> str:
    if not base:
        return "Unknown"
    base = re.sub(r"_0?\d+$", "", base)  # drop trailing version (e.g. _01)
    return base.replace("_", " ").title()


def _ship_manufacturer(cls: str) -> str:
    return _MANUFACTURERS.get(cls.split("_")[0].upper(), cls.split("_")[0])


def _pretty_ship(cls: str) -> str:
    parts = cls.split("_")
    rest = " ".join(parts[1:]).replace("_", " ")
    return f"{_ship_manufacturer(cls)} {rest}".strip()


def _is_weapon(cls: str) -> bool:
    low = cls.lower()
    if low.startswith("none"):   # placeholder / default starter item
        return False
    tokens = set(low.split("_"))
    if tokens & {"optics", "barrel", "ubarrel", "scope", "suppressor",
                 "compensator", "attach", "module", "magazine"}:
        return False
    return bool(tokens & _WEAPON_CORE)


# Multitool attachment heads -> clean display name (skips cosmetic skins).
_TOOL_HEAD_KEYWORDS = (
    ("tractor", "Tractor Beam"), ("salvage", "Salvage"), ("repair", "Repair"),
    ("mining", "Mining"), ("cutter", "Cutter"), ("medical", "Medical"),
)


def _classify_tool_head(cls_low: str) -> Optional[str]:
    for key, name in _TOOL_HEAD_KEYWORDS:
        if key in cls_low:
            return name
    return None


# ── Per-file scan ────────────────────────────────────────────────────────────

def _scan_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            text = f.read()
    except OSError as exc:
        log.warning("fun_stats: cannot read %s: %s", path, exc)
        return {}

    handle = ""
    m = _HANDLE_RE.search(text)
    if m:
        handle = m.group(1).strip()

    # Count every time the player takes the helm (SetDriver control token), not
    # distinct hulls — re-flying the same parked ship should still count.
    ships: Counter = Counter()
    if "control token for '" in text:
        for sm in _SHIP_RE.finditer(text):
            _node, cls, inst = sm.groups()
            if cls.split("_")[0].upper() in _MANUFACTURERS and "_" in cls:
                ships[cls] += 1

    weapons: Counter = Counter()
    consumables = 0
    plushies = 0
    tool_heads: Counter = Counter()
    total_equips = 0
    if "<EquipItem>" in text:
        for em in _EQUIP_RE.finditer(text):
            cls = em.group(1)
            total_equips += 1
            low = cls.lower()
            if "consumable" in low:
                consumables += 1
            elif low.startswith("plushy"):
                plushies += 1
            elif "multitool" in low or "_cutter" in low:
                head = _classify_tool_head(low)
                if head:
                    tool_heads[head] += 1
            elif _is_weapon(cls):
                weapons[_clean_weapon(cls)] += 1

    # ── Missions: join generator-name (type/employer) to completions ──
    id_to_gen: dict[str, str] = {}
    if "generator name [" in text:
        for mm in _MARKER_RE.finditer(text):
            id_to_gen.setdefault(mm.group(1), mm.group(2))
    comp_ids: set[str] = set()
    ab_ids: set[str] = set()
    fail_ids: set[str] = set()
    if "<EndMission>" in text:
        for xm in _ENDMISSION_RE.finditer(text):
            mid, ctype = xm.groups()
            if ctype == "Complete":
                comp_ids.add(mid)
            elif ctype == "Abandon":
                ab_ids.add(mid)
            elif ctype == "Fail":
                fail_ids.add(mid)
    by_cat: Counter = Counter()
    by_emp: Counter = Counter()
    for mid in comp_ids:
        gen = id_to_gen.get(mid)
        by_cat[_mission_category(gen)] += 1
        by_emp[_mission_employer(gen)] += 1

    # ── Trading: commodity sell transactions per terminal ──
    sells = 0
    shops: Counter = Counter()
    if "SendCommoditySellRequest" in text:
        for cm in _SELL_RE.finditer(text):
            sells += 1
            shops[_pretty_shop(cm.group(1))] += 1

    low_text = text.lower()
    activities = [name for name, keys in _ACTIVITY_MARKERS.items()
                  if any(k in low_text for k in keys)]
    # Per-system reference counts; thresholded later to tell actual presence
    # from incidental star-map references.
    sys_hits = {name: low_text.count(tok) for name, tok, _thr in _SYSTEM_TOKENS}

    return {
        "handle": handle,
        "ships": dict(ships),
        "weapons": dict(weapons),
        "consumables": consumables,
        "plushies": plushies,
        "tool_heads": dict(tool_heads),
        "total_equips": total_equips,
        "activities": activities,
        "sys_hits": sys_hits,
        "missions": {"comp": len(comp_ids), "abandon": len(ab_ids),
                     "fail": len(fail_ids), "by_cat": dict(by_cat),
                     "by_emp": dict(by_emp)},
        "trade": {"sells": sells, "shops": dict(shops)},
        "injuries": _injuries.scan_text(text),
    }


# ── Aggregated result ────────────────────────────────────────────────────────

@dataclass
class FunStats:
    player_handle: str = ""
    ships: Counter = field(default_factory=Counter)        # pretty ship -> times flown
    manufacturers: Counter = field(default_factory=Counter)
    weapons: Counter = field(default_factory=Counter)      # pretty weapon -> equips
    activities: Counter = field(default_factory=Counter)   # activity -> sessions
    tool_heads: Counter = field(default_factory=Counter)   # head -> equips
    consumables: int = 0
    plushies: int = 0
    total_equips: int = 0
    system_sessions: Counter = field(default_factory=Counter)  # system -> sessions present
    sessions_scanned: int = 0
    # ── Career ──
    missions_complete: int = 0
    missions_abandon: int = 0
    missions_fail: int = 0
    mission_types: Counter = field(default_factory=Counter)      # category -> completed
    mission_employers: Counter = field(default_factory=Counter)  # contractor -> completed
    trade_sells: int = 0
    trade_terminals: Counter = field(default_factory=Counter)    # location -> sells
    # ── Injuries (HUD injury notifications + med bed surgeries) ──
    injuries: _injuries.InjuryStats = field(default_factory=_injuries.InjuryStats)

    @property
    def is_empty(self) -> bool:
        return not self.ships and not self.weapons and not self.activities

    @property
    def missions_total(self) -> int:
        return self.missions_complete + self.missions_abandon + self.missions_fail

    @property
    def completion_rate(self) -> float:
        return (self.missions_complete / self.missions_total * 100.0
                if self.missions_total else 0.0)

    @property
    def systems(self) -> list[str]:
        order = {name: i for i, (name, _t, _h) in enumerate(_SYSTEM_TOKENS)}
        return sorted((s for s, n in self.system_sessions.items() if n > 0),
                      key=lambda s: order.get(s, 99))

    @property
    def distinct_ships(self) -> int:
        return len(self.ships)


def _aggregate(records: list[dict]) -> FunStats:
    fs = FunStats()
    fs.sessions_scanned = len(records)
    handle_votes: Counter = Counter()

    for r in records:
        h = r.get("handle")
        if h:
            handle_votes[h] += 1
        for cls, n in r.get("ships", {}).items():
            fs.ships[_pretty_ship(cls)] += n
            fs.manufacturers[_ship_manufacturer(cls)] += n
        for wep, n in r.get("weapons", {}).items():
            fs.weapons[_pretty_weapon(wep)] += n
        for head, n in r.get("tool_heads", {}).items():
            fs.tool_heads[head] += n
        fs.consumables += r.get("consumables", 0)
        fs.plushies += r.get("plushies", 0)
        fs.total_equips += r.get("total_equips", 0)
        for act in r.get("activities", []):
            fs.activities[act] += 1
        sh = r.get("sys_hits") or {}
        for name, _tok, thr in _SYSTEM_TOKENS:
            if sh.get(name, 0) > thr:
                fs.system_sessions[name] += 1
        ms = r.get("missions") or {}
        fs.missions_complete += ms.get("comp", 0)
        fs.missions_abandon += ms.get("abandon", 0)
        fs.missions_fail += ms.get("fail", 0)
        for cat, n in ms.get("by_cat", {}).items():
            fs.mission_types[cat] += n
        for emp, n in ms.get("by_emp", {}).items():
            if emp and emp.lower() not in ("unknown", ""):
                fs.mission_employers[emp] += n
        tr = r.get("trade") or {}
        fs.trade_sells += tr.get("sells", 0)
        for loc, n in tr.get("shops", {}).items():
            fs.trade_terminals[loc] += n

    fs.injuries = _injuries.aggregate(r.get("injuries") or {} for r in records)

    if handle_votes:
        fs.player_handle = handle_votes.most_common(1)[0][0]
    return fs


def scan_fun_stats(
    paths: list[str],
    progress_cb: Optional[Callable[[int, int], None]] = None,
    cancel_cb: Optional[Callable[[], bool]] = None,
) -> FunStats:
    """Full-content scan of *paths* with per-file caching; returns aggregate."""
    cache = _settings.load_fun_cache()
    new_cache: dict = {}
    records: list[dict] = []
    total = len(paths)

    for i, path in enumerate(paths):
        if cancel_cb and cancel_cb():
            break
        try:
            stt = os.stat(path)
            size, mtime = stt.st_size, stt.st_mtime
        except OSError:
            continue
        rec = cache.get(path)
        if not (rec and rec.get("v") == _CACHE_VERSION
                and rec.get("size") == size and rec.get("mtime") == mtime):
            rec = {"v": _CACHE_VERSION, "size": size, "mtime": mtime,
                   "data": _scan_file(path)}
        new_cache[path] = rec
        if rec.get("data"):
            records.append(rec["data"])
        if progress_cb:
            progress_cb(i + 1, total)

    _settings.save_fun_cache(new_cache)
    return _aggregate(records)
