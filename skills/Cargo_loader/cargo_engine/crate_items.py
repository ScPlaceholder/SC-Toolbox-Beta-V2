"""
Personal crates for the Cargo Loader. Pure logic, no UI.

J, 2026-09-26: "add personal boxes and then have them each have a number on
it ... fuzzy search and assign any item ingame that will fit in the crate so
that users can organize giant loot runs. The storage crates will also have to
calculate the actual storage inventory space so that you can't shove a Kraken
engine into a handheld crate".

Crates
------
The five Stor*All crates, read from the pinned scunpacked items.json
(4.10.1-LIVE.12660092, commit e9613207). Capacity is the crate's
``stdItem.InventoryContainer`` (SCU + UnitName); its hold footprint is its
own ``InventoryOccupancy`` box via item_catalog.footprint (1 cell = 1.25 m).
``CRATES`` below is that same data, pinned, so crates can be placed before
(or without) the 67 MB download; ``crates_from_entries`` re-derives it from
the file and a test holds the two equal.

Items that go in a crate
------------------------
Every entry of fps-items.json (guns, armour, clothes, food, gadgets), the
carryable entries of ship-items.json (components, ship guns, missiles, bombs,
mining/salvage heads, flair) and the loot-type extras of items.json
(minerals, data chips, batteries, carryable boxes, decorations). Each keeps
one number that matters: ``stdItem.InventoryOccupancy.Volume``. That volume
is stored in integer micro-SCU, because values run from 1 µSCU to 16 SCU
and the file mixes units: ``SCU`` is rounded to 4 decimals, the exact
figure is ``SCUConverted`` in ``Unit`` (SCU, cSCU or µSCU).

The fit rule is volume and nothing else, and it is STRICT: an item goes in
only if its volume times the quantity fits the crate's remaining capacity.
(Star Citizen's personal inventories are volume-limited; a crate does not
check an item's shape.)

The raw files are 26 + 67 + 14 MB. ``build_index`` streams them one object
at a time and keeps five fields per item, so the in-memory index is a few
MB and ``load_index`` reads a derived file after the first build.
"""

from __future__ import annotations

import json
import os
import re
import tempfile

from . import item_catalog

INDEX_FILE = "crate_items_index.json"
INDEX_VERSION = 1
SOURCE_FILES = ("fps-items.json", "ship-items.json", "items.json")

MICRO = 1_000_000          # µSCU per SCU

# (className, short label) in size order. Values checked against items.json.
CRATES: list[dict] = [
    {"cls": "Carryable_2H_FL_05x05x05_DestroyedInventory_BoxExtInventory",
     "name": "Stor*All 1/8 SCU Storage Box", "short": "1/8 SCU",
     "capacity_u": 122_500, "grid_m": (0.5, 0.5, 0.5)},
    {"cls": "Carryable_TBO_InventoryContainer_1SCU",
     "name": "Stor*All 1 SCU Self-Storage Container", "short": "1 SCU",
     "capacity_u": 1_000_000, "grid_m": (1.25, 1.25, 1.25)},
    {"cls": "Carryable_TBO_InventoryContainer_2SCU",
     "name": "Stor*All 2 SCU Self-Storage Container", "short": "2 SCU",
     "capacity_u": 2_000_000, "grid_m": (1.25, 1.25, 2.5)},
    {"cls": "Carryable_TBO_InventoryContainer_4SCU",
     "name": "Stor*All 4 SCU Self-Storage Container", "short": "4 SCU",
     "capacity_u": 4_000_000, "grid_m": (2.5, 1.25, 2.5)},
    {"cls": "Carryable_TBO_InventoryContainer_8SCU",
     "name": "Stor*All 8 SCU Self-Storage Container", "short": "8 SCU",
     "capacity_u": 8_000_000, "grid_m": (2.5, 2.5, 2.5)},
]
CRATE_CLASSES = tuple(c["cls"] for c in CRATES)
CRATE_COLOR = "#e0b84d"


def crate_dims(grid_m) -> tuple[int, int, int]:
    """(w, h, l) cells for a crate's (Width, Height, Length) grid in metres."""
    w, h, l = grid_m
    fp = item_catalog.footprint({"CargoGrid": {"Width": w, "Height": h, "Length": l}})
    return fp[0]


def crate_defs(crates: list[dict] | None = None) -> list[dict]:
    """Item-catalogue-shaped definitions for the crates (category "crate")."""
    out = []
    for c in crates or CRATES:
        out.append({
            "key": c["cls"], "name": c["name"], "category": "crate", "size": 0,
            "dims": crate_dims(c["grid_m"]), "source": "grid", "approx": False,
            "scu": c["capacity_u"] / MICRO, "label": c["short"],
            "short": c["short"], "capacity_u": int(c["capacity_u"]),
        })
    return out


# ── units ─────────────────────────────────────────────────────────────────────

def _unit_scale(unit) -> float | None:
    """SCU per one ``unit``: SCU 1, cSCU 0.01, mSCU 0.001, µSCU 1e-6.

    The micro sign arrives as U+00B5, U+03BC, "u", or mangled ("Âµ").
    """
    u = str(unit or "SCU").strip()
    if not u.endswith("SCU"):
        return None
    p = u[:-3]
    if p == "":
        return 1.0
    if p == "c":
        return 0.01
    if p == "m":
        return 0.001
    if p[-1:] in ("µ", "μ", "u"):
        return 1e-6
    return None


def volume_u(vol) -> int | None:
    """``InventoryOccupancy.Volume`` -> integer µSCU, or None if absent/zero."""
    if not isinstance(vol, dict):
        return None
    scale = _unit_scale(vol.get("Unit"))
    val = None
    if scale is not None and vol.get("SCUConverted") is not None:
        try:
            val = float(vol["SCUConverted"]) * scale
        except (TypeError, ValueError):
            val = None
    if val is None:
        try:
            val = float(vol.get("SCU"))
        except (TypeError, ValueError):
            return None
    u = int(round(val * MICRO))
    return u if u > 0 else None


def capacity_u(inv) -> int | None:
    """``stdItem.InventoryContainer`` -> capacity in integer µSCU."""
    if not isinstance(inv, dict):
        return None
    scale = _unit_scale(inv.get("UnitName") or "SCU")
    try:
        val = float(inv.get("SCU")) * (scale if scale is not None else 1.0)
    except (TypeError, ValueError):
        return None
    u = int(round(val * MICRO))
    return u if u > 0 else None


def fmt_u(u: int) -> str:
    """A volume for people: SCU down to 0.01, then µSCU."""
    u = int(u)
    if u >= 10_000:
        s = f"{u / MICRO:.4f}".rstrip("0").rstrip(".")
        return f"{s} SCU"
    return f"{u:,} µSCU"


# ── crates from the datamine ─────────────────────────────────────────────────

def crates_from_entries(entries) -> list[dict]:
    """The Stor*All crates as found in items.json entries (CRATES order)."""
    found = {}
    for e in entries:
        cls = e.get("className") or ""
        if cls not in CRATE_CLASSES:
            continue
        std = e.get("stdItem") or {}
        cap = capacity_u(std.get("InventoryContainer"))
        grid = (std.get("InventoryOccupancy") or {}).get("CargoGrid") or {}
        try:
            g = (float(grid["Width"]), float(grid["Height"]), float(grid["Length"]))
        except (KeyError, TypeError, ValueError):
            continue
        if cap is None:
            continue
        base = next(c for c in CRATES if c["cls"] == cls)
        found[cls] = {"cls": cls, "name": (std.get("Name") or e.get("name") or base["name"]),
                      "short": base["short"], "capacity_u": cap, "grid_m": g}
    return [found[c] for c in CRATE_CLASSES if c in found]


# ── which items can go in a crate ────────────────────────────────────────────

# ship-items.json: parts a player can carry and store. Paints, seats,
# thrusters, fuel tanks, turrets and controllers are part of a ship.
_SHIP_TYPES = {
    "Cooler", "PowerPlant", "Shield", "QuantumDrive", "WeaponGun", "Missile",
    "Bomb", "MissileLauncher", "BombLauncher", "Radar", "WeaponMining",
    "TractorBeam", "TowingBeam", "SalvageHead", "SalvageModifier", "EMP",
    "LifeSupportGenerator", "QuantumInterdictionGenerator", "JumpDrive",
    "Flair_Surface", "Flair_Wall", "Flair_Floor", "Flair_Cockpit",
}
# items.json only (fps and ship items are read from their own files):
# loot types, plus anything whose class says it is carried by hand.
_EXTRA_TYPES = {
    "Misc", "RemovableChip", "MiningModifier", "Gadget", "Battery", "Currency",
    "InventoryContainer", "Suit", "FPS_Consumable", "Char_Accessory_Head",
}

_CAMEL = re.compile(r"(?<=[a-z])(?=[A-Z])")
_KIND_NAMES = {"Cargo": "Commodity Box", "MobiGlas": "mobiGlas",
               "WeaponGun": "Ship Weapon", "WeaponPersonal": "Personal Weapon"}


def kind_label(typ: str, sub: str = "", size=None) -> str:
    """A short type label: "Armor Helmet", "PowerPlant S4" -> "Power Plant S4"."""
    t = (typ or "").split(".")[0]
    for pre in ("Char_", "FPS_"):
        if t.startswith(pre):
            t = t[len(pre):]
    t = _KIND_NAMES.get(t) or _CAMEL.sub(" ", t.replace("_", " ")).strip()
    try:
        s = int(size or 0)
    except (TypeError, ValueError):
        s = 0
    return f"{t} S{s}" if s and t else t


def _is_placeholder(name: str) -> bool:
    return not name or "PLACEHOLDER" in name


def item_row(e: dict, source: str):
    """[name, vol_u, kind, className, uuid] for one raw entry, or None."""
    if not isinstance(e, dict):
        return None
    std = e.get("stdItem") or {}
    name = (std.get("Name") or e.get("name") or "").strip()
    if _is_placeholder(name):
        return None
    cls = e.get("className") or std.get("ClassName") or ""
    low = cls.lower()
    if "template" in low or low.endswith("_test") or low.startswith("test_"):
        return None
    typ = (e.get("type") or std.get("Type") or "").split(".")[0]
    if source == "ship-items.json" and typ not in _SHIP_TYPES:
        return None
    if source == "items.json" and typ not in _EXTRA_TYPES \
            and not cls.startswith("Carryable_"):
        return None
    vu = volume_u((std.get("InventoryOccupancy") or {}).get("Volume"))
    if vu is None:
        return None
    size = e.get("size") if source == "ship-items.json" else None
    return [name, vu, kind_label(typ, e.get("subType") or "", size), cls,
            e.get("reference") or std.get("UUID") or ""]


# ── streaming JSON array reader ──────────────────────────────────────────────

CHUNK = 1 << 20             # characters read at a time by iter_json_array


def iter_json_array(path: str, chunk: int | None = None):
    """Yield the objects of a top-level JSON array one by one.

    Reads *chunk* characters at a time, so a 67 MB file never becomes one
    Python object tree.
    """
    chunk = chunk or CHUNK
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8") as f:
        buf = f.read(chunk)
        eof = len(buf) < chunk
        i = 0
        # opening bracket
        while True:
            while i < len(buf) and buf[i] in " \t\r\n﻿":
                i += 1
            if i < len(buf):
                break
            if eof:
                return
            buf, i = f.read(chunk), 0
            eof = len(buf) < chunk
        if buf[i] != "[":
            raise ValueError(f"{os.path.basename(path)} is not a JSON array")
        i += 1
        while True:
            while i < len(buf) and buf[i] in " \t\r\n,":
                i += 1
            if i < len(buf) and buf[i] == "]":
                return
            try:
                if i >= len(buf):
                    raise json.JSONDecodeError("need more", buf, i)
                obj, j = dec.raw_decode(buf, i)
            except json.JSONDecodeError:
                if eof:
                    raise
                more = f.read(chunk)
                eof = len(more) < chunk
                buf = buf[i:] + more
                i = 0
                continue
            yield obj
            i = j
            if i > chunk:                 # drop what has been consumed
                buf, i = buf[i:], 0


# ── the derived index ─────────────────────────────────────────────────────────

def build_index(paths: dict, meta: dict | None = None) -> dict:
    """Stream the raw files into the compact index.

    paths: {"fps-items.json": path, "ship-items.json": path, "items.json": path};
    a missing entry is skipped (the index says which sources it read).
    """
    rows: list[list] = []
    seen: set = set()
    crates: list[dict] = []
    read = []
    for name in SOURCE_FILES:
        p = paths.get(name)
        if not p or not os.path.isfile(p):
            continue
        read.append(name)
        crate_entries = []
        for e in iter_json_array(p):
            if name == "items.json" and (e.get("className") or "") in CRATE_CLASSES:
                crate_entries.append(e)
            r = item_row(e, name)
            if r is None:
                continue
            k = (r[0].lower(), r[1])
            if k in seen:
                continue
            seen.add(k)
            rows.append(r)
        if crate_entries:
            crates = crates_from_entries(crate_entries)
    rows.sort(key=lambda r: r[0].lower())
    out = {"version": INDEX_VERSION, "sources": read, "crates": crates, "items": rows}
    if meta:
        out.update({k: meta[k] for k in ("build", "commit") if k in meta})
        files = meta.get("files") or {}
        out["sha256"] = {n: (files.get(n) or {}).get("sha256") for n in read}
    return out


def index_path(cache_dir: str) -> str:
    return os.path.join(cache_dir, INDEX_FILE)


def have_sources(cache_dir: str) -> bool:
    return all(os.path.isfile(os.path.join(cache_dir, n)) for n in SOURCE_FILES)


def load_index(cache_dir: str, allow_build: bool = True) -> dict | None:
    """The derived index for *cache_dir*: read it, or build + write it once.

    None when the raw files are not downloaded yet (and no index exists).
    """
    ip = index_path(cache_dir)
    if os.path.isfile(ip):
        try:
            with open(ip, encoding="utf-8") as f:
                idx = json.load(f)
            if idx.get("version") == INDEX_VERSION \
                    and set(idx.get("sources") or ()) == set(SOURCE_FILES):
                return idx
        except (OSError, ValueError):
            pass
    if not allow_build or not have_sources(cache_dir):
        return None
    meta = {}
    mp = os.path.join(cache_dir, "meta.json")
    if os.path.isfile(mp):
        try:
            with open(mp, encoding="utf-8") as f:
                meta = json.load(f)
        except (OSError, ValueError):
            meta = {}
    idx = build_index({n: os.path.join(cache_dir, n) for n in SOURCE_FILES}, meta)
    data = json.dumps(idx, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=cache_dir, prefix=".tmp_", suffix=INDEX_FILE)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, ip)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return idx


# ── search ────────────────────────────────────────────────────────────────────

def _subseq(q: str, t: str) -> bool:
    it = iter(t)
    return all(ch in it for ch in q)


def search(rows: list, query: str, limit: int = 60) -> list:
    """Fuzzy search over index rows. Every word of the query must appear in
    the name or the type (any order); failing that, the query's letters in
    order (the toolbox's usual fuzzy rule). Name starts and word starts rank
    first, then shorter names."""
    q = (query or "").strip().lower()
    if not q:
        return []
    words = q.split()
    hits = []
    for r in rows:
        name = r[0].lower()
        hay = name + " " + str(r[2]).lower()
        if all(w in hay for w in words):
            if name.startswith(q):
                rank = 0
            elif (" " + name).find(" " + words[0]) >= 0:
                rank = 1
            else:
                rank = 2
            hits.append((rank, len(name), name, r))
    if not hits and len(q) >= 3:
        # Typos: each query word is the letters, in order, of one word of the
        # name that starts with the same letter ("stelate", "p4ar").
        for r in rows:
            name = r[0].lower()
            nwords = re.findall(r"[a-z0-9]+", name.replace("-", ""))
            if all(any(nw[:1] == w[:1] and _subseq(w, nw) for nw in nwords)
                   for w in (re.sub(r"[^a-z0-9]", "", x) for x in words) if w):
                hits.append((3, len(name), name, r))
    hits.sort(key=lambda t: t[:3])
    return [h[3] for h in hits[:limit]]


# ── crate contents (strict volume rule) ──────────────────────────────────────

def used_u(contents: list[dict]) -> int:
    return sum(int(c["vol_u"]) * int(c["qty"]) for c in contents)


def check_fit(capacity: int, contents: list[dict], vol: int, qty: int = 1
              ) -> tuple[bool, str]:
    """Can *qty* more of an item of *vol* µSCU go in? (ok, reason)."""
    if qty < 1:
        return False, "quantity must be at least 1"
    left = int(capacity) - used_u(contents)
    need = int(vol) * int(qty)
    if need <= left:
        return True, ""
    return False, f"needs {fmt_u(need)}, {fmt_u(max(left, 0))} left"


def add_item(capacity: int, contents: list[dict], entry: dict, qty: int = 1
             ) -> tuple[bool, str]:
    """Add *qty* of *entry* ({key, name, vol_u, ...}) in place; merges a row."""
    ok, why = check_fit(capacity, contents, entry["vol_u"], qty)
    if not ok:
        return False, why
    for c in contents:
        if c["key"] == entry["key"]:
            c["qty"] += int(qty)
            return True, ""
    row = {k: entry[k] for k in ("key", "name", "vol_u") if k in entry}
    row["kind"] = entry.get("kind", "")
    row["uuid"] = entry.get("uuid", "")
    row["qty"] = int(qty)
    contents.append(row)
    return True, ""


def set_qty(capacity: int, contents: list[dict], key: str, qty: int
            ) -> tuple[bool, str]:
    """Change a row's quantity (strict); 0 removes the row."""
    for i, c in enumerate(contents):
        if c["key"] != key:
            continue
        if qty <= 0:
            del contents[i]
            return True, ""
        if qty > c["qty"]:
            # only the extra units need room; the reason names just those
            ok, why = check_fit(capacity, contents, c["vol_u"], qty - c["qty"])
            if not ok:
                return False, why
        c["qty"] = int(qty)
        return True, ""
    return False, "not in this crate"


def entry_from_row(r: list) -> dict:
    return {"name": r[0], "vol_u": int(r[1]), "kind": r[2], "key": r[3], "uuid": r[4]}


# ── UEX (read-only, from the Item Finder's own cache) ────────────────────────

UEX_ITEM_URL = "https://uexcorp.space/items/info?name={slug}"


def default_uex_cache(skill_dir: str) -> str:
    """The Item Finder's UEX cache (skills/Market_Finder/.uex_cache.json)."""
    return os.path.normpath(os.path.join(skill_dir, "..", "Market_Finder", ".uex_cache.json"))


def load_uex(path: str) -> dict | None:
    """{"by_uuid": {...}, "by_name": {...}} from the Item Finder cache, or
    None when there is no cache. Nothing is fetched."""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    by_uuid, by_name = {}, {}
    for it in data.get("items") or []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        rec = {"id": it.get("id"), "name": it.get("name"), "slug": it.get("slug") or "",
               "section": it.get("section") or "", "category": it.get("category") or "",
               "company": it.get("company_name") or ""}
        if it.get("uuid"):
            by_uuid[str(it["uuid"]).lower()] = rec
        by_name.setdefault(str(it["name"]).lower(), rec)
    return {"by_uuid": by_uuid, "by_name": by_name,
            "timestamp": data.get("timestamp")}


def uex_match(uex: dict | None, entry: dict) -> dict | None:
    """The UEX record for an item: by game UUID, else by exact name."""
    if not uex:
        return None
    u = str(entry.get("uuid") or "").lower()
    if u and u in uex["by_uuid"]:
        return uex["by_uuid"][u]
    return uex["by_name"].get(str(entry.get("name") or "").lower())


def uex_url(rec: dict | None) -> str | None:
    if not rec or not rec.get("slug"):
        return None
    return UEX_ITEM_URL.format(slug=rec["slug"])
