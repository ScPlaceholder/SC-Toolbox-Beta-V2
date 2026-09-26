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
  * the empty, non-editable gun ports inside a camera turret (Idris P/M
    "Remote Camera Turret") are not gun slots: nothing can be fitted there.

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
import math
import os
import tempfile
import urllib.request
from typing import Optional

BUILD = "4.10.1-LIVE.12660092"
COMMIT = "e96132078ae6a1a5f62a183fb1523dc006dcfddb"
REPO = "StarCitizenWiki/scunpacked-data"
RAW_URL = "https://raw.githubusercontent.com/" + REPO + "/{commit}/{file}"
FILES = ("ships.json", "ship-items.json")
# Cargo Loader personal crates (fetched only when a crate needs them, never by
# the DPS path): every FPS item (26 MB) and the full item list (67 MB, where
# the Stor*All crates live), from the SAME pinned commit.
LOOT_FILES = ("fps-items.json", "items.json")
# Craft Database: the crafting blueprints of the SAME pinned commit (3.7 MB,
# 1,607 recipes with ingredient names already resolved). Never fetched by the
# DPS path. The sha256 is pinned so a changed or corrupted file is refused.
CRAFT_FILES = ("blueprints.json",)
PINNED_SHA256 = {
    "blueprints.json": "1166b9b77382e6866c7a3f42be971204ca242ca5e2fa136073ba6e212e788cf3",
    # ★ ADDED 2026-09-26. The DPS pair had its hashes RECORDED in meta.json and never
    #   CHECKED against anything — recording a hash proves only that we hashed what we
    #   downloaded, which a corrupted or substituted file satisfies just as well. These
    #   two ARE the shipping dataset (56 MB of the 60), so they were the least pinned and
    #   the most load-bearing files in the tree.
    #   Both computed from the pinned build on disk and cross-checked against meta.json's
    #   recorded values; blueprints.json was recomputed the same way as a CONTROL and
    #   reproduced the pin above exactly, which is what licensed trusting the method.
    "ships.json": "c71a772f5d9909b32f2eb47758982b92ef4557b5476b868f42fa31fb3b803fd4",
    "ship-items.json": "a5e1ab01cced503d8ee994124c82f59ed5df6cf125127adfb701c3e3ffa74fb8",
}
INDEX_FILE = "guns_index.json"
ADAPTER_VERSION = 7          # 7: Slayer fire interval + per-shot-overheat sustain; 6: camera-turret ports; 5: pellets, spread, range

ATTRIBUTION = ("Ship and weapon data: StarCitizenWiki/scunpacked-data. "
               "Calculator lineage: erkul.games. Star Citizen content (c) "
               "Cloud Imperium Games; not affiliated.")

GOALS = {"sustained": "dps_sus", "burst": "dps_raw", "alpha": "alpha"}

# Realistic mode (scatter guns only): the defaults a player can override.
# 300 m is the CLOSE end of a dogfight, chosen because it favours the
# scatter gun: if it loses there, it loses at longer range too. 10 m is
# the width of a light/medium fighter's silhouette (a Gladius is ~17 m
# wide, an Arrow ~14 m, both ~4-5 m tall), modelled as a 10 m disc.
REALISTIC_RANGE_M = 300.0
REALISTIC_TARGET_SIZE_M = 10.0
REALISTIC_MODEL = (
    "ESTIMATE for scatter guns only: pellets leave in a cone whose half-angle is the "
    "gun's Spread (degrees, stdItem.Weapon.Modes[].Spread.Maximum), spread evenly over "
    "the cone's cross-section; aim is dead centre on a still target seen as a disc of "
    "the stated size at the stated range. Expected pellets hitting = pellets x "
    "(target radius / cone radius)^2, capped at all of them; DPS and alpha are scaled "
    "by hits / pellets. Beyond the projectile's range nothing hits. Reading Spread as a "
    "half-angle is an assumption (not documented in the data); read as a full angle "
    "the cone is half as wide and about 4x as many pellets hit. Other guns are unchanged "
    "(their own spread is not modelled).")
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


def fetch_raw(build: str = BUILD, commit: str = COMMIT, timeout: float = 120.0,
              files_wanted: tuple = FILES, dest: Optional[str] = None,
              expect_sha256: Optional[dict] = None) -> dict:
    """Download the raw files once (skips files already on disk); writes meta.json.

    files_wanted: which files of the pinned commit (default: the DPS pair;
    the Cargo Loader's crates ask for ``("ship-items.json",) + LOOT_FILES``;
    the Craft Database asks for CRAFT_FILES).
    dest: a directory to use instead of the cache (tests, verification).
    expect_sha256: {file: sha256}; a file (new or already on disk) whose hash
    differs is deleted and ScunpackedError is raised. Default: no check.

    ⚠ CALLERS MUST PASS THE PINS. Only the Craft Database did, which is why the DPS pair
      went unverified for its whole life — ships.json and ship-items.json, 56 MB of the
      60 MB we ship, had their hashes dutifully recorded in meta.json and compared to
      nothing. The DPS ``download()`` now passes them too.
    ⛔ AND ``None`` MUST KEEP MEANING "NO CHECK". I briefly made None default to
      PINNED_SHA256 on the reasoning that an opt-in check is one callers forget. That
      reasoning is fine and the change was still wrong: the pins are keyed by FILENAME,
      not by build, and ``DatamineSource.download`` passes None deliberately to mean
      "this is a synthetic build, do not compare its blueprints.json to the real pin".
      Making None strict therefore broke a caller that was already correct, and
      ``test_download_checks_pinned_sha256`` caught it immediately.
    ★ The lesson is about the SHAPE of the fix, not the hash: a shared default carries
      meaning that existing callers have already built on, so tightening it edits their
      behaviour from underneath. Tighten at the call site, where the intent is local.

    Each file asked for gets its size and sha256 recorded in meta.json.
    """
    d = dest or cache_dir(build)
    meta_path = os.path.join(d, "meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    files = meta.get("files", {})
    for name in files_wanted:
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
        sha = _sha256_file(path)
        want = (expect_sha256 or {}).get(name)
        if want and sha != want:
            os.remove(path)
            raise ScunpackedError(f"{name} from {REPO} failed its sha256 check "
                                  f"(got {sha[:12]}, pinned {want[:12]})")
        files[name] = {"bytes": os.path.getsize(path), "sha256": sha}
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


# scunpacked reads a fire rate whose game unit is seconds (an interval) as rounds per minute. The Slayer's
# 12 is one shot every 12 s, not 12 a minute, which turned 2,667 DPS into 6,400. Keyed by class AND the
# value we expect, so if scunpacked fixes it (or the gun changes) the override stops applying on its own.
# Same table as SuitMk2's tools/build_ship_weapons.py, where this was found (2026-09-25).
_SECONDS_NOT_RPM = {"hrst_nova_ballisticcannon_s5": 12.0}


def _per_shot_overheat(alpha: float, interval: float, heat: dict) -> Optional[float]:
    """Sustained DPS for a gun that overheats on EVERY shot (the Slayer: 91,125 heat per shot against a
    limit of 100). scunpacked's Sustained reads ~5 DPS for it because its duty-cycle formula assumes many
    shots per overheat. The real cycle is the longer of the fire interval and the overheat lockout."""
    hps = _f(heat.get("HeatPerShot")) or 0.0
    limit = _f(heat.get("OverheatTemperature")) or 0.0
    if limit <= 0 or hps < limit or interval <= 0 or not alpha:
        return None
    return alpha / max(interval, _f(heat.get("OverheatFixTime")) or 0.0)


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
    cls_key = str(item.get("className") or "").lower()
    if cls_key in _SECONDS_NOT_RPM and rpm == _SECONDS_NOT_RPM[cls_key]:
        rpm = 60.0 / rpm
    fire_type = m.get("FireType")
    if fire_type == "beam":
        burst = _f(m.get("Dps")) or 0.0
    else:
        burst = per_shot * rpm / 60.0
    sus = _f(dmg.get("Sustained"))
    alpha = _f(dmg.get("AlphaTotal"))
    if alpha is None and fire_type != "beam":
        alpha = per_shot
    if fire_type != "beam" and rpm:
        pso = _per_shot_overheat(per_shot, 60.0 / rpm, w.get("Heat") or {})
        if pso is not None:
            sus = pso
    tags = list(std.get("Tags") or [])
    pellets = int(_f(m.get("PelletsPerShot")) or 1)
    spread = m.get("Spread") or {}
    ammo = std.get("Ammunition") or {}
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
        "pellets": max(1, pellets),
        "spread_min": _f(spread.get("Minimum")),
        "spread_max": _f(spread.get("Maximum")),
        "range_m": _f(ammo.get("Range")) or _f(w.get("EffectiveRange")),
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
                # Camera turrets (Idris P/M "Remote Camera Turret") carry two
                # non-editable, always-empty gun ports. They are sensors, not
                # weapon mounts, so they are not gun slots. Scoped to camera
                # turrets only: other non-editable empty ports (Mustang, C1,
                # Starlifter) were not verified and are left as they were.
                if any("camera" in (a.get("ClassName") or "").lower() for a in ancestors) \
                        and not e.get("Editable"):
                    continue
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


# ── realistic mode: scatter guns ──────────────────────────────────────────

def is_scatter(w: dict) -> bool:
    """More than one pellet per shot. The tag alone misses the Distortion
    scatter guns (tagged DistortionScatterGun) and an untagged AA gun."""
    return (w.get("pellets") or 1) > 1 or any("ScatterGun" in t for t in w.get("tags") or ())


def scatter_hits(w: dict, range_m: float = REALISTIC_RANGE_M,
                 target_size_m: float = REALISTIC_TARGET_SIZE_M) -> dict:
    """Expected pellets of one shot that hit (see REALISTIC_MODEL)."""
    n = int(w.get("pellets") or 1)
    sp = w.get("spread_max") or w.get("spread_min")
    out = {"pellets": n}
    if not sp or sp <= 0:
        out.update(expected_hits=1.0, hit_fraction=round(1.0 / n, 4),
                   basis="no spread data: ranked by single-pellet damage (one pellet hits)")
        return out
    out["spread_deg"] = sp
    rng = w.get("range_m")
    if rng and range_m > rng:
        out.update(expected_hits=0.0, hit_fraction=0.0,
                   basis=f"target beyond the projectile's {rng:.0f} m range")
        return out
    cone_r = range_m * math.tan(math.radians(sp))
    tr = target_size_m / 2.0
    frac = 1.0 if cone_r <= tr else (tr / cone_r) ** 2
    out.update(cone_radius_m=round(cone_r, 2), hit_fraction=round(frac, 4),
               expected_hits=round(n * frac, 3), basis="spread cone")
    return out


def realistic_weapon(w: dict, range_m: float = REALISTIC_RANGE_M,
                     target_size_m: float = REALISTIC_TARGET_SIZE_M) -> dict:
    """*w* itself for a non-scatter gun; for a scatter gun a copy whose
    dps_sus / dps_raw / alpha are scaled by expected hits / pellets."""
    if not is_scatter(w):
        return w
    h = scatter_hits(w, range_m, target_size_m)
    f = h["expected_hits"] / max(1, h["pellets"])
    c = dict(w)
    for k in ("dps_sus", "dps_raw", "alpha"):
        if w.get(k) is not None:
            c[k] = round(w[k] * f, 2)
            c[k + "_all_pellets"] = w[k]
    c["scatter_estimate"] = h
    return c
