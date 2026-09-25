"""scunpacked-data adapter: ships, gun hardpoints and ship weapons.

Source: github.com/StarCitizenWiki/scunpacked-data, pinned to one commit
(one game build). Two files are fetched once and cached under
``~/.sctoolbox/scunpacked/<build>/`` next to a ``meta.json`` that records
the build, the commit and each file's sha256:

    ships.json       320 entries; each has a nested ``Loadout`` port tree
    ship-items.json  5,412 ship items; guns carry ``stdItem.Weapon``

From those two a compact ``guns_index.json`` is derived (a few hundred KB),
so answering a question never parses the 55 MB of raw JSON again. After
the first build nothing here writes a file.

Stdlib only. Loaded by path inside the DPS worker (workers/h_dps.py) and
imported directly by the selftest, so it must not import any skill.

What counts as a gun hardpoint
------------------------------
Walk every port of the ship's Loadout tree:

  * a port holding an item of type ``WeaponGun.Gun`` is ONE gun slot; its
    size range is that port's MinSize..MaxSize. The weapon's own children
    (barrel, power array, ...) are not walked.
  * ``WeaponGun.Rocket`` (rocket pods), missile racks, tractor beams,
    mining and salvage heads are not guns and are skipped.
  * a port holding anything else (a gimbal mount, a turret, a manned
    turret base, a door mount) is a container: walk into it. A container
    with no gun under it (a tractor turret) contributes nothing.
  * an EMPTY port that accepts ``WeaponGun`` directly is an empty gun slot.
    An empty port that only accepts a turret is listed separately: it
    cannot take a gun until a turret is chosen, so it is not optimised.
  * guns under a ``Turret.PDCTurret`` are automated point defence, and
    guns on a ``WeaponMount`` are crew-operated door guns (erkul shows
    neither); both are listed but excluded from the totals.
  * a port holding a ``WeaponGun.Gun`` that has no fire modes (the Taurus
    tractor beam is typed that way) is not a gun slot.

Which weapon may go in a slot (the fit rule)
--------------------------------------------
  size    MinSize <= weapon size <= MaxSize of the gun port (the stock
          weapon is exempt from MinSize only: CIG ships a few undersized
          stock guns, e.g. an S3 cannon on the Titan Renegade's S4 nose)
  type    WeaponGun / Gun, with the tags flightReady and weaponMountUsable,
          and not an NPC / test variant (see _NON_PLAYER_MARKERS)
  tags    every tag the weapon REQUIRES must be offered at that port: the
          ship's PortTags, plus the tags carried by the ports and items on
          the path down to it (this is what keeps the Storm-only Reign-3
          off every other hull)
  locked  a port marked not Editable keeps its stock weapon
The stock weapon always fits its own slot (never above MaxSize).
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
import urllib.request
from typing import Optional

BUILD = "4.10.1-LIVE.12660092"
COMMIT = "e96132078ae6a1a5f62a183fb1523dc006dcfddb"
REPO = "StarCitizenWiki/scunpacked-data"
RAW_URL = "https://raw.githubusercontent.com/" + REPO + "/{commit}/{file}"
FILES = ("ships.json", "ship-items.json")
INDEX_FILE = "guns_index.json"
ADAPTER_VERSION = 4

ATTRIBUTION = ("Ship and weapon data: StarCitizenWiki/scunpacked-data. "
               "Calculator lineage: erkul.games. Star Citizen content (c) "
               "Cloud Imperium Games; not affiliated.")

GOALS = {"sustained": "dps_sus", "burst": "dps_raw", "alpha": "alpha"}
_REQUIRED_WEAPON_TAGS = ("flightReady", "weaponMountUsable")

# ship-items.json also carries NPC, capital-AI, cutscene and test variants of
# guns (LowPoly/Dummy LODs, security-network turrets, Bengal and Vanduul
# weapons, placeholders). They are valid in their own stock slot but never
# offered as an upgrade. Found by diffing the 190 scunpacked guns against the
# 136 player guns erkul lists for the same build (local copy): every one of
# the 54 extras is either matched here or harmless (a same-stat variant, or
# a tag-locked bespoke gun).
_NON_PLAYER_MARKERS = ("lowpoly", "dummy", "securitynetwork", "cleanair", "bengal_",
                       "_mounted_", "pdc", "_atls", "vncl_", "vanduul_",
                       "automatedturret", "fps_balance", "fakehologram",
                       "_collector")              # Wikelo reward-ship stock guns
_NON_PLAYER_CLASSES = {"behr_javelinballisticcannon_s7", "behr_laserrepeater_s10",
                       "behr_massdriver_s12"}


def is_player_weapon(cls: str, name: str, mfr: Optional[str]) -> bool:
    c = (cls or "").lower()
    if not name or "PLACEHOLDER" in name or (mfr or "UNKN") == "UNKN":
        return False
    if c in _NON_PLAYER_CLASSES or c.endswith("_turret"):
        return False
    return not any(m in c for m in _NON_PLAYER_MARKERS)


class ScunpackedError(Exception):
    """Data could not be fetched, parsed or matched; message is user-fit."""


def cache_dir(build: str = BUILD) -> str:
    return os.path.join(os.path.expanduser("~"), ".sctoolbox", "scunpacked", build)


# ── fetch + cache ─────────────────────────────────────────────────────────

def _atomic_write(path: str, data: bytes) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp_", suffix=os.path.basename(path))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_raw(build: str = BUILD, commit: str = COMMIT, timeout: float = 120.0) -> dict:
    """Download the raw files once (skips files already on disk); writes meta.json."""
    d = cache_dir(build)
    meta_path = os.path.join(d, "meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    files = meta.get("files", {})
    for name in FILES:
        path = os.path.join(d, name)
        if not os.path.isfile(path):
            url = RAW_URL.format(commit=commit, file=name)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "SC-Toolbox-Assistant"})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    data = r.read()
            except Exception as exc:                        # noqa: BLE001
                raise ScunpackedError(f"could not download {name} from {REPO}: {exc}")
            if data.lstrip()[:1] not in (b"[", b"{"):
                raise ScunpackedError(f"{name} from {REPO} is not JSON (LFS pointer?)")
            _atomic_write(path, data)
        files[name] = {"bytes": os.path.getsize(path), "sha256": _sha256_file(path)}
    meta.update({
        "source": REPO, "build": build, "commit": commit, "files": files,
        "fetched_at": meta.get("fetched_at")
        or datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    })
    _atomic_write(meta_path, json.dumps(meta, indent=2).encode("utf-8"))
    return meta


# ── derived index ─────────────────────────────────────────────────────────

def _f(x) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def weapon_stats(item: dict) -> Optional[dict]:
    """Normalised stats for one ship-items.json WeaponGun, or None if not a gun.

    burst  = damage per shot x rounds per minute / 60, computed here from
             the raw mode fields (DamagePerShot already totals all pellets
             of a scatter gun). Beams have no per-shot damage; their mode
             DPS is used instead.
    sustained, alpha: taken from scunpacked's own Damage block
             (Sustained / AlphaTotal); sustained models heat and capacitor
             limits of the weapon alone, not the ship's power pool.
    """
    if item.get("type") != "WeaponGun" or item.get("subType") != "Gun":
        return None
    std = item.get("stdItem") or {}
    w = std.get("Weapon") or {}
    modes = w.get("Modes") or []
    if not modes:
        return None
    m = modes[0]
    dmg = w.get("Damage") or {}
    per_shot = _f(m.get("DamagePerShot")) or 0.0
    rpm = _f(m.get("RoundsPerMinute")) or 0.0
    fire_type = m.get("FireType")
    if fire_type == "beam":
        burst = _f(m.get("Dps")) or 0.0
    else:
        burst = per_shot * rpm / 60.0
    sus = _f(dmg.get("Sustained"))
    alpha = _f(dmg.get("AlphaTotal"))
    if alpha is None and fire_type != "beam":
        alpha = per_shot
    tags = list(std.get("Tags") or [])
    req = std.get("RequiredTags") or []
    if isinstance(req, str):
        req = req.split()
    return {
        "cls": item.get("className"),
        "name": item.get("name") or std.get("Name") or item.get("className"),
        "size": int(item.get("size") or std.get("Size") or 0),
        "mfr": (std.get("Manufacturer") or {}).get("Code"),
        "fire": fire_type,
        "per_shot": round(per_shot, 3),
        "rpm": rpm,
        "dps_raw": round(burst, 2),
        "dps_sus": None if sus is None else round(sus, 2),
        "alpha": None if alpha is None else round(alpha, 2),
        "src_burst": _f(dmg.get("Burst")),
        "tags": tags,
        "req": list(req),
        "mountable": all(t in tags for t in _REQUIRED_WEAPON_TAGS),
        "player": is_player_weapon(item.get("className"),
                                   item.get("name") or std.get("Name"),
                                   (std.get("Manufacturer") or {}).get("Code")),
    }


def _compat_types(e: dict) -> list:
    out = []
    for c in e.get("CompatibleTypes") or []:
        out.append((c.get("Type"), tuple(c.get("SubTypes") or ())))
    return out


def _accepts_gun(e: dict) -> bool:
    return any(t == "WeaponGun" and (not st or "Gun" in st) for t, st in _compat_types(e))


def _accepts_turret(e: dict) -> bool:
    return any(t in ("Turret", "TurretBase") for t, _ in _compat_types(e))


def _mount_kind(ancestors: list) -> tuple:
    """(mount, turret_hardpoint) for a gun port given the entries above it."""
    turret = None
    for a in ancestors:
        typ = a.get("Type") or ""
        cls = a.get("ClassName") or ""
        if typ.startswith("TurretBase"):
            turret = ("manned turret", a.get("HardpointName"))
        elif typ == "Turret.PDCTurret":
            turret = ("pdc", a.get("HardpointName"))
        elif typ.startswith("Turret") and not cls.startswith("Mount_"):
            sub = typ.split(".", 1)[-1]
            kind = {"BallTurret": "ball turret", "NoseMounted": "nose turret",
                    "CanardTurret": "canard turret", "TopTurret": "top turret"}.get(sub, "turret")
            if "remote" in cls.lower():
                kind = "remote turret"
            turret = (kind, a.get("HardpointName"))
        elif typ.startswith("WeaponMount"):
            turret = ("crew mount", a.get("HardpointName"))
    parent = ancestors[-1] if ancestors else None
    pcls = (parent or {}).get("ClassName") or ""
    if turret:
        return turret
    if "Gimbal" in pcls:
        return ("gimbal", None)
    return ("fixed", None)


def extract_ship(ship: dict) -> dict:
    """Gun slots of one ships.json entry (see the module docstring)."""
    ship_tags = list(ship.get("PortTags") or [])
    slots, empty_turret_ports = [], []

    def tags_of(e: dict) -> list:
        return list(e.get("PortTags") or []) + list(e.get("RequiredTags") or [])

    def add_slot(e, ancestors, p, here_tags, stock, editable):
        mount, turret = _mount_kind(ancestors)
        slots.append({
            "id": "/".join(p), "hardpoint": p[0], "port": p[-1],
            "min_size": int(e.get("MinSize") or 0),
            "max_size": int(e.get("MaxSize") or 0),
            "mount": mount, "turret": turret,
            "editable": editable, "stock": stock, "tags": sorted(here_tags),
        })

    def walk(entries, ancestors, path, avail):
        for e in entries or []:
            p = path + [e.get("HardpointName") or "?"]
            typ = e.get("Type") or ""
            cls = e.get("ClassName")
            here_tags = avail | set(tags_of(e))
            if typ.startswith("WeaponGun"):
                if typ == "WeaponGun.Gun":        # rocket pods are not guns
                    add_slot(e, ancestors, p, here_tags, cls, bool(e.get("Editable")))
                continue
            if not cls:
                if _accepts_gun(e):
                    add_slot(e, ancestors, p, here_tags, None, True)
                elif _accepts_turret(e) and not any(
                        t in ("MissileLauncher", "BombLauncher") for t, _ in _compat_types(e)):
                    empty_turret_ports.append({
                        "id": "/".join(p), "min_size": e.get("MinSize"),
                        "max_size": e.get("MaxSize"),
                        "accepts": [t + ("." + "/".join(st) if st else "")
                                    for t, st in _compat_types(e)]})
                continue
            kids = e.get("Loadout") or []
            if kids:
                walk(kids, ancestors + [e], p, here_tags)

    walk(ship.get("Loadout") or [], [], [], set(ship_tags))
    wpn = ship.get("Weaponry") or {}
    return {
        "cls": ship.get("ClassName"),
        "name": ship.get("Name"),
        "vehicle": bool(ship.get("IsVehicle")),
        "tags": ship_tags,
        "slots": slots,
        "empty_turret_ports": empty_turret_ports,
        "src_pilot_dps": wpn.get("PilotDps"),
        "src_turret_dps": wpn.get("TurretDps"),
    }


def build_index(build: str = BUILD) -> dict:
    d = cache_dir(build)
    with open(os.path.join(d, "ship-items.json"), encoding="utf-8") as f:
        items = json.load(f)
    weapons = {}
    for it in items:
        st = weapon_stats(it)
        if st:
            weapons[st["cls"]] = st
    del items
    with open(os.path.join(d, "ships.json"), encoding="utf-8") as f:
        raw_ships = json.load(f)
    ships = {}
    for s in raw_ships:
        if s.get("IsPowerSuit") or not s.get("ClassName"):
            continue
        ent = extract_ship(s)
        # a WeaponGun.Gun with no fire modes is not a gun (the Taurus
        # tractor beam is typed WeaponGun.Gun): keep it off the slot list
        ent["non_gun_ports"] = [x for x in ent["slots"]
                                if x["stock"] and x["stock"] not in weapons]
        ent["slots"] = [x for x in ent["slots"]
                        if not x["stock"] or x["stock"] in weapons]
        ships[s["ClassName"]] = ent
    del raw_ships
    meta = {}
    mp = os.path.join(d, "meta.json")
    if os.path.isfile(mp):
        with open(mp, encoding="utf-8") as f:
            meta = json.load(f)
    return {"adapter_version": ADAPTER_VERSION, "build": build,
            "commit": meta.get("commit", COMMIT), "source": REPO,
            "ships": ships, "weapons": weapons}


_INDEX_CACHE: dict = {}


def load_index(build: str = BUILD, allow_fetch: bool = True) -> dict:
    """The derived index; fetches + builds it the first time only."""
    if build in _INDEX_CACHE:
        return _INDEX_CACHE[build]
    path = os.path.join(cache_dir(build), INDEX_FILE)
    idx = None
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            idx = json.load(f)
        if idx.get("adapter_version") != ADAPTER_VERSION:
            idx = None
    if idx is None:
        have_raw = all(os.path.isfile(os.path.join(cache_dir(build), n)) for n in FILES)
        if not have_raw:
            if not allow_fetch:
                raise ScunpackedError(f"scunpacked {build} is not cached yet")
            fetch_raw(build)
        idx = build_index(build)
        _atomic_write(path, json.dumps(idx, separators=(",", ":")).encode("utf-8"))
    _INDEX_CACHE[build] = idx
    return idx


# ── ship lookup ───────────────────────────────────────────────────────────

def _scorer():
    try:
        from . import logic                                  # package import
    except ImportError:
        import _assist_logic as logic                        # inside a worker
    return logic.match_score


def find_ship(idx: dict, query: str, min_score: float = 0.6) -> tuple:
    """(ship_entry, alternatives). Ties go to the shortest ClassName, so
    'Hammerhead' is AEGS_Hammerhead, not AEGS_Hammerhead_GS."""
    score = _scorer()
    q = (query or "").strip()
    if not q:
        raise ScunpackedError("say a ship name, e.g. Gladius")
    ranked = []
    for cls, s in idx["ships"].items():
        name = s.get("name") or cls
        mfr_less = name.split(" ", 1)[1] if " " in name else name
        sc = max(score(q, name), score(q, mfr_less), score(q, cls.replace("_", " ")))
        ranked.append((sc, len(cls), cls))
    ranked.sort(key=lambda t: (-t[0], t[1]))
    if not ranked or ranked[0][0] < min_score:
        near = [idx["ships"][c]["name"] for s, _, c in ranked[:3] if s >= 0.3]
        raise ScunpackedError(f"no ship matching '{q}' in scunpacked {idx['build']}"
                              + (f" (closest: {', '.join(near)})" if near else ""))
    best = ranked[0]
    alts, seen = [], {idx["ships"][best[2]]["name"]}
    for s, _, c in ranked[1:8]:
        n = idx["ships"][c]["name"]
        if s >= min_score and n not in seen:
            seen.add(n)
            alts.append(n)
    return idx["ships"][best[2]], alts[:4]


# ── the fit rule ──────────────────────────────────────────────────────────

def fits(slot: dict, w: dict) -> bool:
    """Size, type/tags and required-tag rule (module docstring)."""
    if w["size"] > slot["max_size"]:
        return False
    if w["cls"] == slot.get("stock"):
        return True                      # CIG ships a few undersized stock guns
    if w["size"] < slot["min_size"]:
        return False
    if not slot.get("editable"):
        return False
    if not (w.get("mountable") and w.get("player")):
        return False
    avail = set(slot.get("tags") or ())
    return all(t in avail for t in w.get("req") or ())


def candidates_for(slot: dict, weapons: dict, key: str) -> list:
    """Weapons that fit *slot*, as optimizer candidate dicts (need 'size' + key)."""
    return [w for w in weapons.values() if w.get(key) is not None and fits(slot, w)]
