#!/usr/bin/env python3
"""
erkul_item_resolver.py — resolve the UUID-referenced items the DPS Calculator's
cache never fetched.

Root cause of the whole "UUID swamp" (Elah, 2026-07-10): the cache pulls 9 item
catalogs from erkul (weapons/shields/coolers/missiles/radars/powerplants/qdrives/
thrusters/paints) but NOT these three, which erkul also serves:
  * /live/utilities    (151) — tractor beams, mining lasers, tools, ping/scanner
  * /live/missile-racks (151) — the racks themselves, WITH capacity (ports list)
  * /live/turrets      (234) — turret housings
Every item installed on a ship by a bare UUID (rather than a misl_/grin_ localName)
lives in one of these lists. Without them the extractor can't tell a tractor-beam
turret from a gun turret, or know a bespoke rack holds 4 missiles — the two
remaining classes of erkul-parity bugs.

This module fetches those three catalogs (cached to .erkul_items_cache.json, TTL
7d) and exposes lookups by ref UUID:
    resolve(ref) -> {"name","type","subType","port_count"} or None
    rack_capacity(ref) -> int missiles the rack holds (len of its ports), or 0
    is_nonweapon_utility(ref) -> True if a tractor/mining/tool/scanner utility

Standalone (stdlib urllib) so it has no dependency on the skill's shared/ modules.
"""
import json
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ITEMS_CACHE = HERE / ".erkul_items_cache.json"
TTL = 7 * 24 * 3600
CACHE_VERSION = 2  # bump to invalidate old caches lacking default_gun / localName keys

BASE = "https://server.erkul.games"
ENDPOINTS = ("/live/utilities", "/live/missile-racks", "/live/turrets")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "application/json",
    "Origin": "https://www.erkul.games",
    "Referer": "https://www.erkul.games/",
}

# utility subTypes/names erkul renders OUTSIDE its weapons section (not guns)
_NONWEAPON_HINTS = ("tractor", "mining", "salvage", "tractorbeam", "towing",
                    "quantumenforcement", "scanner", "ping", "cargo", "tool")


def _network_allowed():
    """erkul.games requests are off (data/source.py); fail closed."""
    try:
        from data.source import erkul_network_allowed
        return erkul_network_allowed()
    except Exception:
        return False


def _fetch(path):
    if not _network_allowed():
        raise RuntimeError("erkul.games requests are disabled (data/source.py)")
    req = urllib.request.Request(BASE + path, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _build():
    """Fetch the 3 catalogs, flatten into ref -> compact item record."""
    index = {}
    for ep in ENDPOINTS:
        try:
            data = _fetch(ep)
        except Exception as e:
            print(f"  warn: {ep} fetch failed: {e}")
            continue
        for e in data:
            d = e.get("data", {}) if isinstance(e, dict) else {}
            ref = d.get("ref", "")
            local_name = e.get("localName", "") if isinstance(e, dict) else ""
            if not ref and not local_name:
                continue
            ports = d.get("ports", []) or []
            # count ports that accept a WeaponGun (turret gun mounts), so a
            # UUID-installed turret with no tree children can still report how
            # many guns it holds (Scorpius remote turret = 4 VariPuck gimbals).
            gun_ports = 0
            for p in ports:
                its = p.get("itemTypes", []) or []
                if any((t.get("type") == "WeaponGun") for t in its):
                    gun_ports += 1
            # The gun a turret ships with (from its own loadout tree), so a
            # localName-referenced PDC turret housing can resolve to the gun that
            # actually deals the DPS (turret_pdc_behr_a -> behr_laserrepeater_pdc_s1).
            default_gun = ""
            for le in (d.get("loadout") or []):
                pn = (le.get("itemPortName") or "").lower()
                gln = le.get("localName") or ""
                if gln and any(k in pn for k in ("weapon", "gun", "class")):
                    default_gun = gln
                    break
            rec = {
                "name": d.get("name", ""),
                "type": d.get("type", ""),
                "subType": d.get("subType", ""),
                "port_count": len(ports),
                "gun_ports": gun_ports,
                "default_gun": default_gun,
                "local_name": local_name,
                "src": ep,
            }
            if ref:
                index[ref] = rec
            if local_name:
                index[local_name] = rec
    return index


def _scunpacked_dir():
    """The active scunpacked build folder the DPS window already uses, or None."""
    try:
        from data import scunpacked_provider as P
        return P.S.cache_dir(P.ensure_data(allow_fetch=False)["dir"])
    except Exception:
        return None


def _build_scunpacked(d=None):
    """The same ref/localName -> record index, built from scunpacked-data.

    Used whenever erkul cannot answer (its network is off by default, and the cache
    expires after TTL), which otherwise left this index EMPTY: every bespoke rack,
    UUID turret and PDC housing resolved to nothing. Scored 2026-09-25 against the
    last erkul cache on the 331 refs both know: rack capacity 138/138, turret gun
    ports 81/81, utility flag 327/331 (the 4 differences are tractor-beam remote
    turrets that scunpacked names correctly and erkul called "Remote Turret").
    Default guns come from the ship loadouts (turret_pdc_behr_a ->
    behr_laserrepeater_pdc_s1 on the Polaris)."""
    d = d or _scunpacked_dir()
    if not d:
        return {}
    try:
        items = json.loads((Path(d) / "ship-items.json").read_text(encoding="utf-8"))
        ships = json.loads((Path(d) / "ships.json").read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  warn: scunpacked fallback unavailable: {e}")
        return {}

    default_gun = {}

    def walk(entries):
        for e in entries or []:
            cn = (e.get("ClassName") or "").lower()
            kids = e.get("Loadout") or []
            if cn and cn not in default_gun:
                for k in kids:
                    if (k.get("Type") or "").startswith("WeaponGun") and k.get("ClassName"):
                        default_gun[cn] = k["ClassName"].lower()
                        break
            walk(kids)

    for s in ships:
        walk(s.get("Loadout"))

    index = {}
    for it in items:
        si = it.get("stdItem") or {}
        ports = si.get("Ports") or []
        gun_ports = 0
        for p in ports:
            types = set(p.get("Types") or []) | {t.get("Type") for t in (p.get("CompatibleTypes") or [])}
            if "WeaponGun" in types:
                gun_ports += 1
        rack = si.get("MissileRack") or {}
        local_name = (it.get("className") or "").lower()
        rec = {
            "name": it.get("name") or si.get("Name", ""),
            "type": it.get("type", ""),
            "subType": it.get("subType", ""),
            "port_count": int(rack.get("MissileCount") or len(ports)),
            "gun_ports": gun_ports,
            "default_gun": default_gun.get(local_name, ""),
            "local_name": local_name,
            "src": "scunpacked",
        }
        if it.get("reference"):
            index[it["reference"]] = rec
        if local_name:
            index[local_name] = rec
    return index


def _load():
    if ITEMS_CACHE.exists():
        try:
            blob = json.loads(ITEMS_CACHE.read_text(encoding="utf-8"))
            if (blob.get("v") == CACHE_VERSION
                    and time.time() - blob.get("ts", 0) < TTL and blob.get("items")):
                return blob["items"]
        except Exception as e:
            print(f"  warn: erkul item cache unreadable ({type(e).__name__}); rebuilding")
    idx = _build() if _network_allowed() else {}
    if idx:  # only persist a non-empty fetch
        ITEMS_CACHE.write_text(
            json.dumps({"ts": time.time(), "v": CACHE_VERSION, "items": idx}),
            encoding="utf-8")
        return idx
    # erkul could not answer: build from scunpacked (kept in memory, never written
    # into the erkul cache file, so the two sources are never mixed on disk)
    return _build_scunpacked()


_INDEX = None


def _idx():
    global _INDEX
    if _INDEX is None:
        _INDEX = _load()
    return _INDEX


def resolve(ref):
    return _idx().get(ref or "")


def rack_capacity(ref):
    """How many missiles a bespoke rack (referenced by UUID) holds."""
    it = resolve(ref)
    return it["port_count"] if it and "MissileRack" in it.get("subType", "") else 0


def turret_gun_ports(ref):
    """How many gun mounts a UUID-installed turret holds (0 if not a turret)."""
    it = resolve(ref)
    return it.get("gun_ports", 0) if it else 0


def turret_default_gun(name_or_ref):
    """The gun a turret ships with, by localName or UUID ('' if none / not a turret).

    Lets a PDC turret housing referenced by localName (turret_pdc_behr_a) resolve to
    the actual DPS-dealing gun (behr_laserrepeater_pdc_s1) so the slot is not blank."""
    it = resolve(name_or_ref)
    return (it.get("default_gun") or "") if it else ""


def is_nonweapon_utility(ref):
    """True if this ref is a tractor/mining/tool/scanner utility (not a gun)."""
    it = resolve(ref)
    if not it:
        return False
    hay = (it.get("type", "") + it.get("subType", "") + it.get("name", "")).lower()
    return any(h in hay for h in _NONWEAPON_HINTS)


if __name__ == "__main__":
    import sys
    idx = _idx()
    print(f"resolver index: {len(idx)} items across {ENDPOINTS}")
    # proof cases
    tests = {
        "d0cc229d-53f9-485d-8242-9c7fbfa586a4": "Gladiator rack -> expect cap 4",
        "f627cdcb-5597-4d05": "Golem Remote Turret",
    }
    for ref, desc in tests.items():
        # allow prefix lookup for the truncated one
        hit = resolve(ref) or next((v for k, v in idx.items() if k.startswith(ref)), None)
        print(f"  {desc}: {hit}")
    for ref in sys.argv[1:]:
        print(f"  {ref}: resolve={resolve(ref)} rack_cap={rack_capacity(ref)} nonweap={is_nonweapon_utility(ref)}")
