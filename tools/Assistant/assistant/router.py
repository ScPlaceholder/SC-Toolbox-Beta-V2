"""Router-first tool chooser: plain code decides which tool, which entity,
and whether to ask. The LLM is optional (see agent.py modes).

Three parts:

  * Catalog  -- real name lists read from the tools' own local data
                (ship SCU table, scunpacked ship index, Cargo Loader grids,
                UEX commodity and item caches, Mission DB cache, Mining
                Loadout cache, Starmap systems). Every source is optional:
                a missing file just leaves that list shorter. Nothing here
                touches the network or writes a file. The only hand-made
                list is NICKNAMES (what players call things, which no data
                file carries: "cat", "clad", "quant").
  * score()  -- a weighted phrase table per tool (RULES) plus features
                driven by the names found (a resource makes "where do I
                find" mean mining, a mining laser makes "stats" mean the
                mining loadout, a ship plus "fill" is packing OR trading).
  * decide() -- one clear winner with its names -> call it;
                runner-up within TIE_RATIO -> ask one line naming both;
                winner with a required name missing -> ask for it;
                nothing above MIN_SCORE -> say so honestly.
                A winner that leads but not decisively is "lean": router
                mode takes it, router+llm lets the model pick among the
                top candidates.

Stdlib only, no model. The catalog loads once per process.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Optional

from . import logic

log = logging.getLogger(__name__)

MIN_SCORE = 2.0       # below this no tool is claimed
TIE_RATIO = 0.8       # runner-up >= 80% of the winner -> ask which
LEAN_RATIO = 0.6      # runner-up >= 60% -> "lean" (LLM may tie-break)


# ── what players call things (the one hand-made list) ────────────────────
# alias -> (type, canonical). Only words the data does not already carry.

NICKNAMES = {
    "cat": ("ship", "Caterpillar"), "catty": ("ship", "Caterpillar"),
    "clad": ("ship", "Ironclad"), "iron clad": ("ship", "Ironclad"),
    "connie": ("ship", "Constellation Andromeda"),
    "conny": ("ship", "Constellation Andromeda"),
    "herc": ("ship", "C2 Hercules"), "hercules": ("ship", "C2 Hercules"),
    "starlifter": ("ship", "C2 Hercules"),
    "cutty": ("ship", "Cutlass Black"), "cutlass": ("ship", "Cutlass Black"),
    "prospy": ("ship", "Prospector"), "prospie": ("ship", "Prospector"),
    "glad": ("ship", "Gladius"), "valk": ("ship", "Valkyrie"),
    "bucc": ("ship", "Buccaneer"), "tali": ("ship", "Retaliator"),
    "msr": ("ship", "Mercury Star Runner"),
    "c2": ("ship", "C2 Hercules"), "m2": ("ship", "M2 Hercules"), "a2": ("ship", "A2 Hercules"),
    "hull c": ("ship", "Hull C"),
    "quant": ("resource", "Quantainium"), "quanta": ("resource", "Quantainium"),
    "quantanium": ("resource", "Quantainium"),
    "trade hub": ("tool", "Trade Hub"), "star map": ("tool", "Starmap"),
    "cargo loader": ("tool", "Cargo Loader"), "mission database": ("tool", "Mission DB"),
    "dps calculator": ("tool", "DPS Calculator"),
}

# launcher skill id -> spoken display name (ids are the launcher's own)
_TOOL_DISPLAY = {
    "trade": "Trade Hub", "market": "Item Finder", "missions": "Mission DB",
    "craft_db": "Craft Database", "mining": "Mining Loadout",
    "mining_signals": "Mining Signals", "cargo": "Cargo Loader",
    "dps": "DPS Calculator", "battle_buddy": "Battle Buddy", "playtime": "PlayTime",
    "starmap": "Starmap", "mouse_blocker": "Mouse Blocker", "suitmk2": "SuitMk2",
    "assistant": "Toolbox Assistant",
}

# words too common in speech to count as a one-word name on their own
_COMMON = set("""
a an the i im me my mine you your is are be am do does did can could would should to of in on
for at from with and or what whats where how which who this that it its there any some get got
give show tell please just about right now use when like one two per as by if not only have has
much many best good great fast fastest big biggest small top most more less new old stock hold
holds carry route routes run runs trade trading cargo mining signal ship ships gun guns
weapon weapons loadout build map open help need want way fill up out day time play played hours
game price cost buy sell rent find where make craft recipe mission missions contract contracts
blueprint layout box boxes grid load pack scan scanner ping reading radar spot rock rocks hunting
jump gate gates system systems damage power shield shields cooler module modules laser lasers
gadget focus surge storm hawk fury nova mule spirit fortune defender guardian nomad pioneer
star citizen joke going morning evening thanks thank hey hello hi yes no ok okay sure bye all
home base station city moon moons planet planets belt ore raw pure refined item items thing
things stuff money profit profitable margin haul hauling vs hurt drop drops reward armor
sniper rifle pistol smg lmg shotgun helmet suit backpack arms legs core medical
""".split())

_STRIP_PAREN = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")
_STRIP_QUOTE = re.compile(r'\s*["“”][^"“”]*["“”]')
_ORE_SUFFIX = re.compile(r"\s*\((ore|raw|pure|refined)\)\s*$", re.I)
_NON_ITEM = re.compile(r"\b(livery|paint|skin|magazine|battery|mag|decal|poster|plushie|model)\b", re.I)


def _norm(s) -> str:
    return logic.norm(s)


def _sq(s) -> str:
    return logic.squash(s)


def _toks(text: str) -> tuple:
    orig = re.findall(r"[A-Za-z0-9]+", text or "")
    return orig, [logic._ROMAN.get(t.lower(), t.lower()) for t in orig]


# ── catalog ───────────────────────────────────────────────────────────────

class Catalog:
    """Names the router can recognise, by type, from the tools' own data."""

    TYPES = ("ship", "mining_ship", "commodity", "resource", "item", "blueprint",
             "system", "faction", "mission_type", "laser", "module", "gadget", "tool")

    def __init__(self) -> None:
        self.alias: dict = {t: {} for t in self.TYPES}     # norm alias -> canonical
        self.sq: dict = {t: {} for t in self.TYPES}        # squashed alias -> canonical
        self.names: dict = {t: set() for t in self.TYPES}  # canonical names
        self.sources: dict = {t: [] for t in self.TYPES}   # where each list came from
        self.in_game_systems: set = set()

    # ── building ─────────────────────────────────────────────────────────
    def add(self, typ: str, alias: str, canonical: str, force: bool = False) -> None:
        a = _norm(alias)
        if not a or not canonical:
            return
        if not force and len(a.split()) == 1 and (a in _COMMON or len(a) < 2):
            return
        if force or a not in self.alias[typ]:
            self.alias[typ][a] = canonical
        s = a.replace(" ", "")
        if len(s) >= 3 and (force or s not in self.sq[typ]):
            self.sq[typ][s] = canonical
        self.names[typ].add(canonical)

    @classmethod
    def load(cls, base_dir: str) -> "Catalog":
        c = cls()
        home = os.path.join(os.path.expanduser("~"), ".sctoolbox")
        skills = os.path.join(base_dir, "skills")
        for fn in (c._load_ships, c._load_uex, c._load_mission_db, c._load_mining,
                   c._load_systems, c._load_tools):
            try:
                fn(base_dir, home, skills)
            except Exception as exc:                        # noqa: BLE001
                log.warning("router catalog: %s failed: %s", fn.__name__, exc)
        c._add_heads()
        for alias, (typ, canon) in NICKNAMES.items():
            c.add(typ, alias, canon, force=True)
        if not c.in_game_systems:
            for s in ("Stanton", "Pyro", "Nyx"):
                c.add("system", s, s, force=True)
            c.in_game_systems = {"Stanton", "Pyro", "Nyx"}
            c.sources["system"].append("fallback: Stanton, Pyro, Nyx")
        return c

    def _load_ships(self, base_dir, home, skills) -> None:
        try:
            from shared.ships import SHIP_PRESETS
            for key in SHIP_PRESETS:
                self.add("ship", key, _title(key))
            self.sources["ship"].append(f"shared.ships.SHIP_PRESETS ({len(SHIP_PRESETS)})")
        except Exception as exc:                            # noqa: BLE001
            log.info("router: no SHIP_PRESETS: %s", exc)
        cargo = os.path.join(skills, "Cargo_loader", ".cargo_cache.json")
        if os.path.isfile(cargo):
            with open(cargo, encoding="utf-8") as f:
                ships = (json.load(f) or {}).get("ships") or []
            for s in ships:
                name = _STRIP_PAREN.sub("", s.get("name") or "").strip()
                if name:
                    self.add("ship", name, name, force=True)
            self.sources["ship"].append(f"Cargo Loader grid cache ({len(ships)})")
        try:
            from shared.scunpacked import cache_dir, INDEX_FILE
            idx = os.path.join(cache_dir(), INDEX_FILE)
        except Exception:                                   # noqa: BLE001
            idx = ""
        if idx and os.path.isfile(idx):
            with open(idx, encoding="utf-8") as f:
                raw = [v.get("name") or "" for v in (json.load(f).get("ships") or {}).values()]
            firsts: dict = {}
            for n in raw:
                w = n.split(" ")[0]
                firsts[w] = firsts.get(w, 0) + 1
            makers = {w for w, k in firsts.items() if k >= 3}
            for n in raw:
                if not n or "wikelo" in n.lower():
                    continue
                parts = n.split()
                bare = " ".join(parts[1:]) if parts[0] in makers and len(parts) > 1 else n
                self.add("ship", bare, bare)
                self.add("ship", n, bare)
            self.sources["ship"].append(f"scunpacked ship index ({len(raw)})")
        self._ship_token_aliases()

    def _ship_token_aliases(self) -> None:
        """'taurus' -> Constellation Taurus when every ship carrying that
        word contains the same shortest name ('titan' -> Avenger Titan)."""
        by_tok: dict = {}
        for canon in self.names["ship"]:
            for t in _norm(canon).split():
                if len(t) >= 4 and not t.isdigit() and t not in _COMMON:
                    by_tok.setdefault(t, set()).add(canon)
        for t, canons in by_tok.items():
            if t in self.alias["ship"]:
                continue
            shortest = min(canons, key=lambda c: (len(c), c))
            sn = _norm(shortest)
            if all(sn in _norm(c) for c in canons):
                self.add("ship", t, shortest)

    def _load_uex(self, base_dir, home, skills) -> None:
        path = os.path.join(home, "trade_hub", "uex_cache", "commodities.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
            rows = rows.get("data", rows) if isinstance(rows, dict) else rows
            for r in rows:
                name = r.get("name") or ""
                bare = _ORE_SUFFIX.sub("", name).strip()
                self.add("commodity", name, bare, force=True)
                self.add("commodity", bare, bare, force=True)
                if r.get("is_mineral") or r.get("is_raw"):
                    self.add("resource", name, bare, force=True)
                    self.add("resource", bare, bare, force=True)
            self.sources["commodity"].append(f"UEX commodities cache ({len(rows)})")
            self.sources["resource"].append("UEX commodities cache: is_mineral or is_raw")
        path = os.path.join(home, "market_finder", "uex_cache", "items_prices_all.json")
        if not os.path.isfile(path):
            path = os.path.join(home, "starmap", "uex_cache", "items_prices_all.json")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                rows = json.load(f)
            rows = rows.get("data", rows) if isinstance(rows, dict) else rows
            names = {r.get("item_name") for r in rows if r.get("item_name")}
            for n in names:
                self.add("item", n, n)
                clean = _STRIP_QUOTE.sub("", _STRIP_PAREN.sub("", n)).strip()
                if clean and clean != n:
                    self.add("item", clean, n)
            self.sources["item"].append(f"UEX item price cache ({len(names)} names)")

    def _load_mission_db(self, base_dir, home, skills) -> None:
        path = os.path.join(skills, "Mission_Database", ".scmdb_cache.json")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        bps = {b.get("name") for p in (d.get("blueprintPools") or {}).values()
               for b in (p.get("blueprints") or []) if isinstance(b, dict) and b.get("name")}
        for n in bps:
            self.add("blueprint", n, n)
            clean = _STRIP_QUOTE.sub("", _STRIP_PAREN.sub("", n)).strip()
            if clean != n:
                self.add("blueprint", clean, n)
        facs = {v.get("name") for v in (d.get("factions") or {}).values()
                if v.get("name") and "PLACEHOLDER" not in v.get("name")}
        for n in facs:
            self.add("faction", n, n, force=True)
            if n.endswith("s"):
                self.add("faction", n[:-1], n)
        types, systems = set(), set()
        for c in d.get("contracts") or []:
            if c.get("missionType") and c["missionType"] != "local":
                types.add(c["missionType"])
            systems.update(c.get("systems") or [])
        for t in types:
            self.add("mission_type", t, t, force=True)
            for w in _norm(t).split():                     # "salvage", "delivery"
                if w not in ("hauling", "mining", "missions", "ship", "other") and len(w) >= 5:
                    self.add("mission_type", w, t)
        for alias, t in (("bounty", "Bounty Hunter"), ("bounties", "Bounty Hunter"),
                         ("merc", "Mercenary"), ("hauling", "Hauling"),
                         ("salvaging", "Salvage")):
            if t in types:
                self.add("mission_type", alias, t, force=True)
        self.in_game_systems |= systems
        self.sources["blueprint"].append(f"Mission DB cache blueprint pools ({len(bps)})")
        self.sources["faction"].append(f"Mission DB cache ({len(facs)})")
        self.sources["mission_type"].append(f"Mission DB contracts ({len(types)})")

    def _load_mining(self, base_dir, home, skills) -> None:
        cache = os.path.join(skills, "Mining_Loadout", ".api_cache")
        cats = {"Mining Laser Heads": "laser", "Mining Modules": "module", "Gadgets": "gadget"}
        if os.path.isdir(cache):
            for fn in os.listdir(cache):
                try:
                    with open(os.path.join(cache, fn), encoding="utf-8") as f:
                        rows = json.load(f)
                except (OSError, ValueError):
                    continue
                if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict) \
                        or "name" not in rows[0]:
                    continue
                for r in rows:
                    typ = cats.get(r.get("category") or "")
                    if not typ:
                        continue
                    n = r["name"]
                    self.add(typ, n, n, force=True)
                    short = re.sub(r"\s+(mining laser|module)$", "", n, flags=re.I)
                    self.add(typ, short, n, force=True)
                    head = _norm(short).split()[0]
                    if typ != "gadget" and len(head) >= 4:
                        self.add(typ, head, n)
            for typ in ("laser", "module", "gadget"):
                self.sources[typ].append(f"Mining Loadout UEX cache ({len(self.names[typ])})")
        items_py = os.path.join(skills, "Mining_Loadout", "models", "items.py")
        ships = []
        if os.path.isfile(items_py):
            with open(items_py, encoding="utf-8") as f:
                ships = re.findall(r'^\s{4}"([^"]+)":\s*ShipConfig\(', f.read(), re.M)
        for s in ships or ["Prospector", "MOLE", "Golem"]:
            self.add("mining_ship", s, s, force=True)
        self.sources["mining_ship"].append("Mining_Loadout/models/items.py SHIPS"
                                           if ships else "fallback: Prospector, MOLE, Golem")

    def _load_systems(self, base_dir, home, skills) -> None:
        path = os.path.join(skills, "Starmap", "starmap", "data", "systems.json")
        if not os.path.isfile(path):
            return
        with open(path, encoding="utf-8") as f:
            systems = json.load(f).get("systems") or []
        for s in systems:
            name = s.get("name") or ""
            self.add("system", name, name, force=True)
            bare = _STRIP_PAREN.sub("", name).strip()
            if bare != name:
                self.add("system", bare, name, force=True)
            if s.get("in_game"):
                self.in_game_systems.add(name)
        self.sources["system"].append(f"Starmap systems.json ({len(systems)})")

    def _load_tools(self, base_dir, home, skills) -> None:
        from .headless import _SKILL_ALIASES
        for alias, sid in _SKILL_ALIASES.items():
            disp = _TOOL_DISPLAY.get(sid, alias.title())
            if len(alias.split()) > 1 or alias in ("starmap", "suitmk2", "playtime"):
                self.add("tool", alias, disp, force=True)
        for disp in _TOOL_DISPLAY.values():
            self.add("tool", disp, disp, force=True)
        self.sources["tool"].append("headless._SKILL_ALIASES")

    def _add_heads(self) -> None:
        """Items and blueprints are said by their head word: 'P4-AR' for
        'P4-AR Rifle', 'Karna' for 'Karna Rifle'. A head counts when it is
        not a common word and not another kind of name; it names the
        shortest real item starting with it (magazines and liveries only
        when nothing else does)."""
        taken = set()
        for t in ("ship", "commodity", "resource", "system", "faction", "mining_ship",
                  "laser", "module", "tool"):
            taken.update(self.alias[t].keys())
        for typ in ("item", "blueprint"):
            heads: dict = {}
            for canon in self.names[typ]:
                h = _norm(canon.split(" ")[0])
                if not h or h in _COMMON or len(_sq(h)) < 3 or h in taken:
                    continue
                heads.setdefault(h, []).append(canon)
            for h, canons in heads.items():
                if len(canons) > 25:
                    continue
                pool = [c for c in canons if not _NON_ITEM.search(c)] or canons
                self.add(typ, h, min(pool, key=lambda c: (len(c), c)))

    # ── matching ─────────────────────────────────────────────────────────
    def find(self, text: str) -> list:
        """Every recognised name in *text*, as Spans of every type."""
        orig, toks = _toks(text)
        spans: list = []
        n_tok = len(toks)
        for typ in self.TYPES:
            found: list = []
            for n in range(min(6, n_tok), 0, -1):
                for i in range(0, n_tok - n + 1):
                    if any(s.start <= i and i + n <= s.end for s in found):
                        continue
                    key = " ".join(toks[i:i + n])
                    canon = self.alias[typ].get(key)
                    if canon is None and n <= 4:
                        sq = key.replace(" ", "")
                        if len(sq) >= 3 and (n > 1 or not sq.isalpha()):
                            canon = self.sq[typ].get(sq)
                    if canon is None:
                        continue
                    if typ == "system" and canon not in self.in_game_systems:
                        # lore systems share names with ships and words
                        # ("Vanguard", "Min"): only when said as a place
                        prev = toks[i - 1] if i else ""
                        if not (orig[i][:1].isupper() and prev in ("to", "from", "in", "at")):
                            continue
                    found.append(Span(typ, canon, " ".join(orig[i:i + n]), i, i + n, 1.0))
            # spelling slips on one word ("hadnite", "laranit")
            if typ in ("resource", "commodity", "ship", "laser"):
                vocab = [a for a in self.alias[typ] if " " not in a and len(a) >= 5]
                for i, t in enumerate(toks):
                    if len(t) < 5 or t in _COMMON or any(s.start <= i < s.end for s in found):
                        continue
                    m = difflib.get_close_matches(t, vocab, n=1, cutoff=0.86)
                    if m:
                        found.append(Span(typ, self.alias[typ][m[0]], orig[i], i, i + 1, 0.9))
            spans.extend(found)
        return spans

    def summary(self) -> dict:
        return {t: {"names": len(self.names[t]), "aliases": len(self.alias[t]),
                    "sources": self.sources[t]} for t in self.TYPES}


def _title(key: str) -> str:
    keep_upper = {"msr", "mpuv", "srv", "mole", "dur", "mis", "max", "tac", "es", "mr", "cl",
                  "ln", "lx", "mx"}
    out = []
    for w in key.split():
        if w in keep_upper or (len(w) <= 4 and any(ch.isdigit() for ch in w)):
            out.append(w.upper())
        else:
            out.append(w.capitalize())
    return " ".join(out)


_CATALOGS: dict = {}
_CAT_LOCK = threading.Lock()


def get_catalog(base_dir: str) -> Catalog:
    with _CAT_LOCK:
        cat = _CATALOGS.get(base_dir)
        if cat is None:
            cat = Catalog.load(base_dir)
            _CATALOGS[base_dir] = cat
        return cat


# ── spans and decisions ───────────────────────────────────────────────────

@dataclass
class Span:
    type: str
    value: str
    text: str
    start: int
    end: int
    score: float = 1.0


@dataclass
class Decision:
    kind: str                       # call | lean | ask | none | chat
    tool: str = ""
    args: dict = field(default_factory=dict)
    candidates: list = field(default_factory=list)      # [(tool, score)] best first
    args_by_tool: dict = field(default_factory=dict)
    question: str = ""
    reply: str = ""
    missing: str = ""
    reason: str = ""
    pending: Optional[dict] = None


# ── intent table ──────────────────────────────────────────────────────────
# (tool, regex, weight) over the cleaned text: lowercase, apostrophes
# dropped, every other non-alphanumeric run -> one space, space-padded.

_BUY = r"\b(buy|buying|purchase|purchasing|pick (one|it|them) up|pick up|order)\b"
_ACQ = r"\b(get|obtain|acquire|source|find|farm)\b"
_OPEN = r"\b(open|launch|start|pull up|bring up|show|run|fire up|load up)\b"
_DISTANCE = r"\b(how far|far away|far is it|distance|how many jumps|how long does it take|how long to get)\b"
_PRICE = r"\b(how much is|how much for|how much does|price|prices|cost|costs|cheapest|cheap|auec)\b"

RULES = [
    ("ship_info", r"\bhow much (cargo )?(does|do|can|will|would) (a |an |the |my )?[a-z0-9 ]{0,30}(hold|carry|take|fit)\b", 3.5),
    ("ship_info", r"\bhow (many|much) scu\b", 3.5),
    ("ship_info", r"\b(scu|cargo capacity|capacity|cargo space|cargo hold|hold size)\b", 2.5),
    ("ship_info", r"\b(hold|holds|carry|carries)\b", 1.0),

    ("ship_buy_rent", r"\b(rent|rental|renting|lease|loaner)\b", 1.5),

    ("find_trade_routes", r"\btrade (route|routes|run|runs|loop)\b", 4.0),
    ("find_trade_routes", r"\b(haul|hauling|trade|trading|trader)\b", 3.0),
    ("find_trade_routes", r"\b(profit|profitable|money|earn|earning|margin|margins)\b", 3.0),
    ("find_trade_routes", r"\b(cargo run|cargo runs|cargo loop)\b", 3.0),
    ("find_trade_routes", r"\bbuy and sell\b", 2.0),
    ("find_trade_routes", r"\b(sell|selling)\b", 1.5),
    ("find_trade_routes", r"\b(run|runs|loop)\b", 1.0),
    ("find_trade_routes", r"\bcargo\b", 1.0),

    ("find_item_price", r"\b(who|which shop|what shop|what store|where) (sells|stocks|has)\b", 3.0),
    ("find_item_price", r"\b(shop|shops|store|stores|vendor)\b", 1.5),

    ("missions_for_blueprint", r"\bblueprints?\b", 2.5),
    ("missions_for_blueprint", r"\b(missions?|contracts?)\b[a-z0-9 ]{0,20}\b(give|gives|reward|rewards|drop|drops|award|awards)\b", 3.0),
    ("missions_for_blueprint", r"\b(unlock|unlocks|unlocking)\b", 2.0),
    ("missions_for_blueprint", r"\b(drop|drops|dropped|reward|rewards|rewarded)\b", 1.5),

    ("blueprint_recipe", r"\brecipes?\b", 4.0),
    ("blueprint_recipe", r"\b(ingredients|materials|mats)\b", 3.0),
    ("blueprint_recipe", r"\b(craft|crafting|crafted|fabricate)\b", 3.0),
    ("blueprint_recipe", r"\bwhat do i need to\b", 1.5),
    ("blueprint_recipe", r"\bblueprints?\b", 1.0),

    ("where_to_mine", r"\b(mine|mining|mined)\b", 2.0),
    ("where_to_mine", r"\b(rock hunting|rocks|deposits?|ore|asteroids?|harvest|harvestable)\b", 2.0),
    ("where_to_mine", r"\b(which|what) (moons?|planets?|belts?|locations?|places?)\b", 1.5),

    ("search_missions", r"\b(missions?|contracts?|jobs?|gigs?|bounties)\b", 3.0),

    ("identify_signal", r"\b(scanner|scanning|scan|signal|signals|ping|pinging|reading|radar|signature|rs)\b", 2.5),

    ("mining_loadout_stats", r"\b(stats|numbers|how strong|instability|resistance|optimal charge)\b", 1.5),

    ("cargo_layout", r"\b(layout|load|loading|containers?|boxes|box|grid|pack|packing|stack|stacking)\b", 3.0),
    ("cargo_layout", r"\bcargo\b", 1.0),

    ("jump_route", r"\b(jump|jumps|jump point|jump points|gate|gates|wormhole)\b", 3.0),

    ("current_loadout", r"\b(my loadout|my current loadout|in my loadout|what am i carrying|am i carrying|on me|do i have|i have on|my inventory|my gear|my kit)\b", 3.0),
    ("current_loadout", r"\b(medpens?|med pens?|oxypens?|oxy pens?|grenades?|mags|magazines|ammo)\b", 2.5),
    ("current_loadout", r"\bcarrying\b", 1.5),

    ("playtime_summary", r"\bhow long have i (been )?play", 4.0),
    ("playtime_summary", r"\b(hours|played|playtime|play time|streak|sunk|sessions?)\b", 2.5),
    ("playtime_summary", r"\blongest (play )?session\b", 2.0),

    ("best_ship_weapons", r"\b(dps|burst|alpha|sustained|damage)\b", 3.0),
    ("best_ship_weapons", r"\b(loadout|build|fit|fitting|outfit|setup)\b", 1.5),
    ("best_ship_weapons", r"\bbest (guns?|weapons?)\b", 1.0),

    ("show_route_popup", r"\bpin\b", 3.0),
]
_COMPILED = [(t, re.compile(p), w) for t, p, w in RULES]

_CHAT = re.compile(
    r"^\s*(hi|hey|hello|yo|howdy|good (morning|evening|afternoon|night)|thanks|thank you|cheers|"
    r"bye|goodbye|see you|thats all|how s it going|hows it going|how are you|"
    r"tell me a joke|joke|you there|nice|cool|awesome|lol|haha)\b")

# short labels for the one-line "which did you mean" question
_LABEL = {
    "ship_info": "its cargo capacity",
    "ship_buy_rent": "where to buy or rent it",
    "find_trade_routes": "the most profitable trade route",
    "find_item_price": "where to buy it",
    "missions_for_blueprint": "which missions give its blueprint",
    "blueprint_recipe": "how to craft it",
    "where_to_mine": "where to mine it",
    "search_missions": "missions",
    "identify_signal": "what a scanner signal means",
    "mining_loadout_stats": "a mining loadout",
    "cargo_layout": "how to pack its cargo grid with boxes",
    "jump_route": "a jump route between systems",
    "current_loadout": "your current FPS loadout",
    "playtime_summary": "your play time",
    "best_ship_weapons": "the best guns for a ship",
    "show_route_popup": "pinning the route",
    "open_trade_hub": "opening Trade Hub",
    "launch_tool": "opening a tool",
    "starmap_command": "a Star Map command",
    "set_route": "setting a route in the game",
    "plot_route_in_game": "setting the route in the game",
}

# words in a follow-up answer that pick one of the offered options
_PICK = {
    "cargo_layout": r"\b(pack|packing|layout|box|boxes|container|containers|grid|load|first)\b",
    "find_trade_routes": r"\b(profit|profitable|trade|trading|route|money|sell|haul|second)\b",
    "find_item_price": r"\b(buy|buying|shop|store|price|purchase)\b",
    "blueprint_recipe": r"\b(craft|crafting|recipe|make)\b",
    "missions_for_blueprint": r"\b(blueprint|mission|missions|unlock)\b",
    "where_to_mine": r"\b(mine|mining|dig)\b",
    "ship_buy_rent": r"\b(buy|rent|purchase)\b",
    "jump_route": r"\b(jump|travel|gate|gates|fly|navigate)\b",
    "best_ship_weapons": r"\b(gun|guns|weapon|weapons|dps|ship)\b",
    "mining_loadout_stats": r"\b(mining|laser|prospector|mole|golem)\b",
    "current_loadout": r"\b(current|my|fps|carrying)\b",
    "ship_info": r"\b(capacity|scu|hold|size)\b",
}

# which name type fills which required argument
_SLOTS = {
    "ship_info": [("name", "ship")],
    "ship_buy_rent": [("ship", "ship")],
    "find_trade_routes": [("ship", "ship")],
    "find_item_price": [("item", "item")],
    "missions_for_blueprint": [("name", "blueprint")],
    "blueprint_recipe": [("name", "blueprint")],
    "where_to_mine": [("resource", "resource")],
    "identify_signal": [("value", "signal")],
    "mining_loadout_stats": [("ship", "mining_ship")],
    "cargo_layout": [("ship", "ship")],
    "best_ship_weapons": [("ship", "ship")],
    "launch_tool": [("name", "tool")],
}

_SLOT_QUESTION = {
    "ship": "Which ship?",
    "item": "Which item?",
    "blueprint": "Which blueprint?",
    "resource": "Which resource?",
    "signal": "What number does your scanner show?",
    "mining_ship": "Which mining ship: {mining_ships}?",
    "tool": "Which tool should I open? For example Trade Hub, Mining Signals or Cargo Loader.",
    "from_system": "A jump route to {to_system} from which system?",
    "to_system": "A jump route from {from_system} to which system?",
    "filters": "Which star system or mission type? For example: bounty missions in Pyro.",
    "route": "Ask me for a trade route first, then I can pin it.",
}

CAPABILITIES = ("trade routes, ship cargo sizes, ship and item prices, missions and "
                "blueprint rewards, where to mine, crafting recipes, scanner signals, "
                "mining loadouts, cargo layouts, jump routes, your FPS loadout, play time, "
                "the best guns for a ship, opening toolbox tools, and Star Map commands "
                "like navigate to Area 18")

_SIGNAL_NUM = re.compile(
    r"(?<![\w.,-])(\d{3,6})(?![\w-])(?![.,]\d)(?!\s*(?:scu|auec|uec|k\b|hours?\b|h\b|%|m\b|gm\b|km\b))",
    re.I)


def _clean(text: str) -> str:
    t = (text or "").lower().replace("'", "").replace("’", "")
    return " " + re.sub(r"[^a-z0-9]+", " ", t).strip() + " "


# phrase slots when no catalog name matched (the tools resolve spelling)
_OBJ_STOP = re.compile(r"\b(and|for|in|at|with|on|from|right now|now|please|anywhere|cheap|"
                       r"cheapest|how much|where|what|who|is|it|price|cost|costs)\b.*$")
_BP_PHRASE = re.compile(r"(?:the |a |an )?([a-z0-9][a-z0-9\- ]{1,40}?) blueprints?\b")
_OBJ_PHRASE = re.compile(
    r"\b(?:recipe for|craft|make|build|unlock|buy|purchase|grab|sells?|get|price of|cost of|"
    r"price on|price for)(?: an?| the| some| me an?| me the)? ([a-z0-9][a-z0-9\- ]{1,40})")
_FOLLOW_STOP = {"blueprint", "blueprints", "and", "for", "in", "at", "with", "on", "from", "to",
                "is", "it", "right", "now", "please", "anywhere", "cost", "costs", "price",
                "recipe", "the", "a", "an", "or", "vs", "s", "rifle", "smg", "lmg", "pistol"}
_PHRASE_JUNK = {"it", "one", "that", "this", "them", "some", "thing", "stuff", "me", "the"}


def _phrase_for(text: str, kind: str) -> str:
    low = (text or "").lower().replace("’", "'").replace("'", "")
    low = re.sub(r"[^a-z0-9\- ]+", " ", low)
    m = _BP_PHRASE.search(low) if kind == "blueprint" else None
    if not m:
        m = _OBJ_PHRASE.search(low)
    if not m:
        return ""
    ph = _OBJ_STOP.sub("", m.group(1)).strip(" -")
    ph = re.sub(r"^(what|which) (mission|missions|contract|contracts) (gives|give|drops|drop|rewards) ", "", ph)
    ph = re.sub(r"^(how do i (get|unlock)|do i get) ", "", ph)
    ph = re.sub(r"^(the|a|an|my|some)\s+", "", ph).strip()
    if not ph or ph in _PHRASE_JUNK or ph in _COMMON:
        return ""
    return ph


# ── the router ────────────────────────────────────────────────────────────

class Router:
    """decide(text) -> Decision. Holds no conversation state: the agent
    passes back ``pending`` (from the last Decision) when the user is
    answering a question the router asked."""

    def __init__(self, registry=None, base_dir: str = "", catalog: Optional[Catalog] = None) -> None:
        self.registry = registry
        self.base_dir = base_dir
        self._catalog = catalog

    @property
    def catalog(self) -> Catalog:
        if self._catalog is None:
            self._catalog = get_catalog(self.base_dir)
        return self._catalog

    def _has(self, tool: str) -> bool:
        return self.registry is None or self.registry.get(tool) is not None

    # ── scoring ──────────────────────────────────────────────────────────
    def score(self, text: str, spans: Optional[list] = None) -> dict:
        spans = self.catalog.find(text) if spans is None else spans
        by = _by_type(spans)
        t = _clean(text)
        # a named toolbox tool + an open verb is an action; its words
        # ("mining signals", "cargo loader") must not also vote as intents
        tool_spans = by.get("tool", [])
        opening = bool(re.search(_OPEN, t)) and bool(tool_spans)
        for s in tool_spans:
            t = _clean(t.replace(" " + _clean(s.text).strip() + " ", " "))
        sc: dict = {}

        def add(tool, w):
            if self._has(tool):
                sc[tool] = sc.get(tool, 0.0) + w

        for tool, rx, w in _COMPILED:
            if rx.search(t):
                add(tool, w)

        ship = by.get("ship")
        mship = by.get("mining_ship")
        others = ("ship", "commodity", "resource", "mining_ship", "laser", "module")
        item = [s for s in by.get("item", []) if not _covered(s, spans, others)]
        bp = [s for s in by.get("blueprint", []) if not _covered(s, spans, others)]
        res = by.get("resource")
        comm = by.get("commodity")
        mgear = by.get("laser", []) + by.get("module", []) + by.get("gadget", [])
        systems = by.get("system", [])
        signal = self._signal(text)
        buy = bool(re.search(_BUY, t))
        acq = bool(re.search(_ACQ, t))
        price = bool(re.search(_PRICE, t))
        mining_ctx = bool(mship or mgear or re.search(r"\bmining\b", t))
        craft_words = bool(re.search(r"\b(recipe|craft|crafting|blueprint|blueprints|unlock)\b", t))

        # ships
        if ship:
            add("ship_info", 1.0)
            add("cargo_layout", 0.5)
            if re.search(r"\bfill\b", t):
                add("cargo_layout", 2.0)
                add("find_trade_routes", 2.0)
            if buy or re.search(r"\b(rent|rental)\b", t):
                add("ship_buy_rent", 2.5 if not item else 1.5)
            if price and not item:
                add("ship_buy_rent", 1.5)
            if re.search(r"\b(guns?|weapons?)\b", t) and not mining_ctx:
                add("best_ship_weapons", 2.0)
            if not mining_ctx:
                add("best_ship_weapons", 0.5)
        else:
            add("ship_info", -1.5)
            if not mship:
                add("best_ship_weapons", -1.0)      # "loadout" alone is not a ship build
        if mining_ctx:
            add("best_ship_weapons", -3.0)
            add("mining_loadout_stats", 1.0)
            if re.search(r"\b(loadout|build|setup|fit)\b", t):
                add("mining_loadout_stats", 2.0)
        if mship:
            add("mining_loadout_stats", 1.0)
            if re.search(r"\b(stats|numbers|how strong|power|loadout|setup|running|with)\b", t):
                add("mining_loadout_stats", 1.0)
        if mgear:
            add("mining_loadout_stats", 3.0)
        if mship or mgear:
            add("where_to_mine", -2.0)

        # items and blueprints
        if item or bp:
            if buy:
                add("find_item_price", 3.0)
            if price:
                add("find_item_price", 2.0)
            if re.search(r"\bgrab\b", t):
                add("find_item_price", 1.5)
            add("find_item_price", 0.5)
            if acq and not buy and not price and not craft_words and not res:
                # "where do I get a P4-AR": buy it, or craft it?
                add("find_item_price", 2.5)
                add("blueprint_recipe", 2.5)
            if re.search(r"\bmake\b", t):
                add("blueprint_recipe", 1.5)
        elif not craft_words:
            add("blueprint_recipe", -1.0)
            add("find_item_price", -1.0)
            add("missions_for_blueprint", -2.0)

        # resources and commodities
        if res:
            add("where_to_mine", 2.0)
            if acq or re.search(r"\bwhere\b", t):
                add("where_to_mine", 1.0)
        trade_words = re.search(r"\b(trade|trading|haul|hauling|sell|selling|profit|profitable|run|runs|"
                                r"route|routes|money)\b", t)
        if comm and trade_words:
            add("find_trade_routes", 1.5)
        if comm and res and (buy or acq) and not trade_words and not re.search(r"\b(mine|mining|find)\b", t):
            # "where can I get gold": mine it, or buy it?
            add("find_trade_routes", 3.0)

        # missions
        if re.search(r"\b(missions?|contracts?)\b", t) and re.search(r"\bblueprints?\b", t):
            add("search_missions", -3.0)
        if re.search(r"\b(missions?|contracts?|jobs?|gigs?|bounties)\b", t) and not re.search(
                r"\b(loadout|inventory|gear|kit|carrying|medpens?|med pens?|oxypens?|oxy pens?|"
                r"grenades?|mags|magazines|ammo)\b", t):
            # "what missions do I have": "do I have" is a loadout phrase, but
            # with a missions word and nothing carried named, it is missions
            add("current_loadout", -3.0)
        if by.get("mission_type"):
            add("search_missions", 2.0)
        if by.get("faction"):
            add("search_missions", 2.0)

        if signal:
            add("identify_signal", 2.5)
        else:
            add("identify_signal", -1.5)

        # travel
        if systems:
            if re.search(r"\b(get|go|travel|fly|head|jump|route|way|path)\b[a-z0-9 ]{0,12}\bto\b", t):
                add("jump_route", 3.0)
            if len(systems) >= 2:
                add("jump_route", 1.5)
            if re.search(r"\broute\b", t):
                add("jump_route", 1.5)
            if re.search(_DISTANCE, t):
                # "how far is Pyro from Nyx", "distance between Pyro and Nyx":
                # between two systems the only distance there is is jumps
                add("jump_route", 3.0 if len(systems) >= 2 else 1.5)
            elif len(systems) >= 2 and re.search(r"\bfrom\b", t) and re.search(r"\bto\b", t):
                add("jump_route", 1.5)              # "... from Stanton to Nyx"
        elif ship and re.search(r"\bjump\w*\b", t):
            # "how far can a Cutlass jump": a ship's quantum range, not a
            # route between systems (none was named); no tool covers it
            add("jump_route", -3.0)
        elif re.search(r"\broute\b", t) and not ship and not comm:
            # "I need a route": travel or trade, nothing tells them apart
            add("jump_route", 2.0)
            add("find_trade_routes", 2.0)

        if re.search(r"\bloadout\b", t) and not ship and not mining_ctx:
            if re.search(r"\bmy\b", t):
                add("current_loadout", 1.0)
            elif re.search(r"\b(best|good|optimal|recommended)\b", t):
                # "what's the best loadout": ship guns or a mining setup?
                add("best_ship_weapons", 1.5)
                add("mining_loadout_stats", 2.0)

        # actions
        if opening:
            if "Trade Hub" in {s.value for s in tool_spans}:
                add("open_trade_hub", 5.0)
            add("launch_tool", 4.5)
        elif re.search(r"^\s*(open|launch|start|pull up|bring up)\b", t):
            add("launch_tool", 2.5)

        return {k: round(v, 2) for k, v in sc.items() if v > 0}

    @staticmethod
    def _signal(text: str) -> str:
        m = _SIGNAL_NUM.search(text or "")
        return m.group(1) if m else ""

    # ── arguments ────────────────────────────────────────────────────────
    def args_for(self, tool: str, text: str, spans: list) -> dict:
        by = _by_type(spans)
        t = _clean(text)
        a: dict = {}

        def first(typ):
            xs = sorted(by.get(typ) or [], key=lambda s: (-(s.end - s.start), s.start))
            return xs[0].value if xs else ""

        def ship_name():
            xs = by.get("ship", []) + by.get("mining_ship", [])
            xs = sorted(xs, key=lambda s: (-(s.end - s.start), s.start))
            return xs[0].value if xs else ""

        if tool == "ship_info":
            a["name"] = ship_name()
        elif tool in ("ship_buy_rent", "find_trade_routes", "cargo_layout", "best_ship_weapons"):
            a["ship"] = ship_name()
        if tool == "find_trade_routes":
            a["commodity"] = first("commodity")
            a["system"] = self._in_game_system(by)
            if re.search(r"\b(legal only|only legal|no illegal)\b", t):
                a["allow_illegal"] = False
        if tool == "best_ship_weapons":
            for g in ("burst", "alpha"):
                if re.search(r"\b" + g + r"\b", t):
                    a["goal"] = g
        if tool == "find_item_price":
            a["item"] = self._named_thing(by, text, spans, ("item", "blueprint"))
        if tool in ("missions_for_blueprint", "blueprint_recipe"):
            a["name"] = self._named_thing(by, text, spans, ("blueprint", "item"))
        if tool == "where_to_mine":
            a["resource"] = first("resource") or first("commodity")
        if tool == "identify_signal":
            a["value"] = self._signal(text)
        if tool == "mining_loadout_stats":
            a["ship"] = first("mining_ship")
            a["laser"] = first("laser")
            mods = []
            for s in by.get("module", []):
                mods.append(s.value)
                cnt = re.search(r"\b(two|2|three|3)\s+" + re.escape(_clean(s.text).strip()), t)
                if cnt:
                    mods.extend([s.value] * (1 if cnt.group(1) in ("two", "2") else 2))
            a["modules"] = mods
            a["gadget"] = first("gadget")
        if tool == "search_missions":
            a["faction"] = first("faction")
            a["system"] = self._in_game_system(by)
            a["mission_type"] = first("mission_type")
        if tool == "jump_route":
            a["from_system"], a["to_system"] = self._from_to(text, by.get("system", []))
        if tool == "launch_tool":
            a["name"] = first("tool")
        return {k: v for k, v in a.items() if v not in ("", None, [])}

    def _named_thing(self, by, text, spans, types) -> str:
        others = ("ship", "commodity", "resource", "mining_ship", "laser", "module")
        orig, toks = _toks(text)
        for typ in types:
            xs = [s for s in by.get(typ, []) if not _covered(s, spans, others)]
            if xs:
                xs.sort(key=lambda s: (-(s.end - s.start), s.start))
                s = xs[0]
                nxt = toks[s.end] if s.end < len(toks) else ""
                # a head word said with its own noun ("Pembroke armor") is
                # passed as said: the head alone picked the shortest item
                # ("Pembroke Helmet"), which is a guess about which one
                if (s.end - s.start == 1 and nxt and nxt not in _FOLLOW_STOP
                        and nxt not in _norm(s.value).split()):
                    return s.text + " " + orig[s.end]
                return s.value
        return _phrase_for(text, types[0])

    def _in_game_system(self, by) -> str:
        for s in by.get("system", []):
            if s.value in self.catalog.in_game_systems:
                return s.value
        return ""

    @staticmethod
    def _from_to(text: str, systems: list) -> tuple:
        if not systems:
            return "", ""
        systems = sorted(systems, key=lambda s: s.start)
        _, toks = _toks(text)
        fr = to = ""
        for s in systems:
            prev = toks[s.start - 1] if s.start else ""
            prev2 = toks[s.start - 2] if s.start >= 2 else ""
            if prev == "to" or (prev2 == "to" and prev == "the"):
                to = to or s.value
            elif prev in ("from", "in", "at", "leaving") or prev2 in ("from", "in", "at"):
                fr = fr or s.value
        rest = [s.value for s in systems if s.value not in (fr, to)]
        if not fr and not to and len(rest) >= 2:
            fr, to = rest[0], rest[1]
        elif not to and rest:
            to = rest[0]
        elif not fr and rest and len(systems) >= 2:
            fr = rest[0]
        return fr, to

    # ── decision ─────────────────────────────────────────────────────────
    def decide(self, text: str, pending: Optional[dict] = None) -> Decision:
        # A Star Map command ("navigate to Area 18", "zoom in", "star map, ...") is a
        # fixed phrase, not an intent to score: the map's own command router decides
        # what it means. Checked first so an open question from the turn before
        # ("Which ship?") cannot swallow it.
        #
        # A route IN THE GAME is looked at first and is the Assistant's own
        # (set_route/): "navigate to Area 18" must work with the Star Map closed,
        # so it is never relayed, with or without "star map," in front. What is
        # left for the map is what the map draws: "route to <system>", zoom, back.
        cmd = ""
        if self._has("starmap_command"):
            from .starmap_bridge import command_text
            cmd = command_text(text)
        if self._has("set_route"):
            from .set_route.phrases import destination
            # the catalogue is only opened for a bare "route to X" (is X a system?)
            dest = destination(text, lambda: (getattr(self.catalog, "alias", None) or {})
                               .get("system") or ())
            if dest:
                return Decision("call", tool="set_route", args={"destination": dest},
                                reason="set route")
        if cmd:
            return Decision("call", tool="starmap_command", args={"command": cmd},
                            reason="star map command")
        spans = self.catalog.find(text)
        scores = self.score(text, spans)
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

        if pending:
            d = self._resolve_pending(text, spans, ranked, pending)
            if d is not None:
                return d

        if not ranked or ranked[0][1] < MIN_SCORE:
            if len(ranked) >= 2 and ranked[1][1] >= 1.0 and ranked[1][1] >= TIE_RATIO * ranked[0][1]:
                # vague, but it touches two tools evenly ("help me with cargo")
                a, b = ranked[0][0], ranked[1][0]
                return Decision("ask", candidates=ranked,
                                question=f"Do you want {_LABEL.get(a, a)}, or {_LABEL.get(b, b)}?",
                                reason=f"weak tie {ranked[0]} {ranked[1]}",
                                pending={"choices": [a, b], "args_by_tool": {}})
            if _CHAT.search(_clean(text)):
                return Decision("chat", candidates=ranked, reason="small talk",
                                reply=_chat_reply(text))
            return Decision("none", candidates=ranked,
                            reason=f"no tool scored {MIN_SCORE} or more",
                            reply="I can't answer that yet. I can do " + CAPABILITIES + ".")

        top, s1 = ranked[0]
        args_by = {name: self.args_for(name, text, spans) for name, _ in ranked[:3]}
        rest = [r for r in ranked[1:]
                if not ({top, r[0]} == {"open_trade_hub", "launch_tool"})]  # same action
        second = rest[0] if rest else None

        if second and second[1] >= TIE_RATIO * s1:
            a, b = top, second[0]
            q = self._entity_phrase(args_by.get(a) or {}, args_by.get(b) or {}) + \
                f"do you want {_LABEL.get(a, a)}, or {_LABEL.get(b, b)}?"
            q = q[0].upper() + q[1:]
            return Decision("ask", candidates=ranked, args_by_tool=args_by, question=q,
                            reason=f"{a} {s1} vs {b} {second[1]}",
                            pending={"choices": [a, b], "args_by_tool": args_by})

        kind = "lean" if (second and second[1] >= LEAN_RATIO * s1) else "call"
        args = args_by.get(top) or {}
        missing = self.missing(top, args)
        if missing:
            return Decision("ask", tool=top, args=args, candidates=ranked, args_by_tool=args_by,
                            missing=missing, question=self.slot_question(missing, args),
                            reason=f"{top} needs {missing}",
                            pending={"tool": top, "args": args, "missing": missing})
        return Decision(kind, tool=top, args=args, candidates=ranked, args_by_tool=args_by,
                        reason=f"{top} {s1}" + (f" vs {second[0]} {second[1]}" if second else ""))

    @staticmethod
    def missing(tool: str, args: dict) -> str:
        if tool == "search_missions":
            return "" if any(args.get(k) for k in ("faction", "system", "mission_type")) else "filters"
        if tool == "jump_route":
            if not args.get("to_system"):
                return "to_system"
            return "" if args.get("from_system") else "from_system"
        if tool == "show_route_popup":
            return "" if isinstance(args.get("route"), dict) else "route"
        for arg, typ in _SLOTS.get(tool, []):
            if not args.get(arg):
                return typ
        return ""

    def slot_question(self, missing: str, args: dict) -> str:
        q = _SLOT_QUESTION.get(missing, f"Which {missing}?")
        ms = sorted(self.catalog.names["mining_ship"]) or ["Prospector", "MOLE", "Golem"]
        ms_txt = ", ".join(ms[:-1]) + " or " + ms[-1] if len(ms) > 1 else ms[0]
        return q.format(mining_ships=ms_txt, to_system=args.get("to_system", "there"),
                        from_system=args.get("from_system", "here"))

    @staticmethod
    def _entity_phrase(a: dict, b: dict) -> str:
        for d in (a, b):
            if d.get("ship"):
                return f"For the {d['ship']}, "
        for d in (a, b):
            for k in ("name", "item", "resource"):
                if isinstance(d.get(k), str) and d.get(k):
                    return f"For {d[k]}, "
        return ""

    def _resolve_pending(self, text, spans, ranked, pending) -> Optional[Decision]:
        """The user is answering a question the router asked. None means
        'not an answer, treat it as a new request'."""
        t = _clean(text)
        old = pending.get("choices") or [pending.get("tool")]
        if ranked and ranked[0][1] >= 3.5 and ranked[0][0] not in old:
            return None                                     # a clear new request
        if "choices" in pending:
            hits = [c for c in pending["choices"] if c in _PICK and re.search(_PICK[c], t)]
            if len(hits) != 1:
                local = [c for c, _ in ranked if c in pending["choices"]]
                hits = local[:1]
            if len(hits) != 1:
                return None
            tool = hits[0]
            args = dict(pending["args_by_tool"].get(tool) or {})
            args.update(self.args_for(tool, text, spans))
            return self._fill_or_ask(tool, args, "follow-up picked " + tool)
        tool, args, missing = pending["tool"], dict(pending["args"]), pending["missing"]
        if missing in ("from_system", "to_system"):
            sy = [s.value for s in _by_type(spans).get("system", [])]
            if sy:
                args[missing] = sy[0]
        elif missing == "filters":
            args.update(self.args_for(tool, text, spans))
        elif missing == "signal":
            if self._signal(text):
                args["value"] = self._signal(text)
        else:
            new = self.args_for(tool, text, spans)
            for arg, typ in _SLOTS.get(tool, []):
                if typ == missing:
                    val = new.get(arg) or self._bare_name(missing, spans, text)
                    if val:
                        args[arg] = val
        if self.missing(tool, args) == missing:
            return None                                     # did not answer it
        return self._fill_or_ask(tool, args, "slot filled from follow-up")

    def _fill_or_ask(self, tool, args, reason) -> Decision:
        left = self.missing(tool, args)
        if left:
            return Decision("ask", tool=tool, args=args, missing=left,
                            question=self.slot_question(left, args), reason=reason,
                            pending={"tool": tool, "args": args, "missing": left})
        return Decision("call", tool=tool, args=args, reason=reason)

    def _bare_name(self, typ: str, spans: list, text: str) -> str:
        by = _by_type(spans)
        pool = {"ship": ("ship", "mining_ship"), "mining_ship": ("mining_ship",),
                "item": ("item", "blueprint"), "blueprint": ("blueprint", "item"),
                "resource": ("resource", "commodity"), "tool": ("tool",)}.get(typ, (typ,))
        for p in pool:
            if by.get(p):
                return sorted(by[p], key=lambda s: -(s.end - s.start))[0].value
        words = [w for w in _clean(text).split() if w not in ("the", "a", "an", "my")]
        if typ in ("item", "blueprint") and 0 < len(words) <= 4:
            return " ".join(words)
        return ""


def _by_type(spans: list) -> dict:
    out: dict = {}
    for s in spans:
        out.setdefault(s.type, []).append(s)
    return out


def _covered(span: Span, spans: list, types: tuple) -> bool:
    """True when a name of one of *types* covers this whole span."""
    for o in spans:
        if o is not span and o.type in types and o.start <= span.start and span.end <= o.end:
            return True
    return False


def _chat_reply(text: str) -> str:
    t = _clean(text)
    if re.search(r"\b(thanks|thank you|cheers)\b", t):
        return "Any time."
    if re.search(r"\b(bye|goodbye|see you|thats all)\b", t):
        return "Fly safe."
    if re.search(r"\bjoke\b", t):
        return "I'm better with numbers than jokes. Ask me for a trade route."
    return "Hey. Ask me about " + CAPABILITIES + "."
