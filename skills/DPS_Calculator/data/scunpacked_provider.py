"""scunpacked-data provider for the DPS Calculator window.

Replaces the dead erkul.games data layer. Source: github.com/StarCitizenWiki/
scunpacked-data, two files per game build (ships.json, ship-items.json),
cached under ``~/.sctoolbox/scunpacked/<build>/`` with a ``meta.json`` that
records the build, the commit and each file's sha256 (the same layout and the
same directory the Assistant's adapter uses, so one download serves both).

What comes from where
---------------------
* Gun hardpoints, the weapon list and the fit rule (size min..max, player-gun
  filter, ship-lock tags, locked ports, burst/sustained/alpha) come from the
  shared adapter, shared/scunpacked.py, loaded by path.
  Nothing here re-derives them: ``S.load_index`` gives the slots, ``S.fits`` /
  ``S.candidates_for`` decide what may go in a slot, and each window weapon's
  dps_raw / dps_sus / alpha are copied from the adapter's own weapon record.
* Everything else the window shows (shields, coolers, power plants, quantum
  drives, radars, missiles and racks, EMP, QED, bombs, mining, salvage, fuel
  pods, cargo pods) is mapped here from ship-items.json into the stat dicts the
  window's tables already expect, and each ship's Loadout tree is translated
  into the erkul-shaped port tree that ``services.slot_extractor`` walks.

Freshness
---------
``latest_build()`` asks the GitHub API for the newest commit whose message is
a LIVE build label (the repo commits one per build). ``ensure_data()`` downloads
that build if it is not cached and records it as active; the window checks for
a newer build in the background and offers it through Refresh.

Heavy work (parsing 55 MB of JSON) happens only in ``load_window_index`` and
only when the derived ``dpscalc_index.json`` is missing or stale. Call it on
the main thread before Qt starts (PySide6 + Python 3.14 crash when a worker
thread allocates heavily while the event loop runs; see dps_calc_app.main).
"""
from __future__ import annotations

import datetime
import importlib.util
import json
import logging
import os
import re
import sys
import urllib.request
from typing import Callable, Optional

_log = logging.getLogger(__name__)

PROVIDER_VERSION = 6          # 6: resource-network power records (power_raw), power_gaps
INDEX_FILE = "dpscalc_index.json"
ACTIVE_FILE = "dpscalc_active.json"
GITHUB_COMMITS = "https://api.github.com/repos/StarCitizenWiki/scunpacked-data/commits"
_BUILD_RE = re.compile(r"^\d+\.\d+(\.\d+)?-LIVE\.\d+$")

_HERE = os.path.dirname(os.path.abspath(__file__))
# In shared/ since 2026-09-26. It lived in tools/Assistant/assistant/, which the installer does not
# ship, so every installed 2.4.0 test build crashed the DPS Calculator on launch (FileNotFoundError).
_ADAPTER_PATH = os.path.normpath(os.path.join(_HERE, "..", "..", "..", "shared", "scunpacked.py"))


def adapter():
    """The shared scunpacked adapter, loaded by path (same module name as
    workers/h_dps.py uses, so the two share one instance in one process)."""
    name = "_assist_scunpacked"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, _ADAPTER_PATH)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


S = adapter()
ATTRIBUTION = S.ATTRIBUTION


class ProviderError(Exception):
    """User-fit message: data could not be fetched or read."""


# ── build discovery + download ────────────────────────────────────────────

def _root() -> str:
    return os.path.dirname(S.cache_dir("x"))


def _dir_name(build: str, commit: str) -> str:
    """``<build>`` when that directory is empty or already holds *commit*;
    ``<build>+<commit12>`` when it holds a different commit of the same build."""
    d = S.cache_dir(build)
    meta = _read_json(os.path.join(d, "meta.json")) or {}
    if not meta or meta.get("commit") == commit:
        return build
    return f"{build}+{commit[:12]}"


def _read_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def latest_build(timeout: float = 15.0) -> tuple:
    """(build, commit) of the newest LIVE build in scunpacked-data (one GitHub
    API request). Raises ProviderError when GitHub cannot be reached."""
    url = GITHUB_COMMITS + "?sha=master&per_page=20"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "SC-Toolbox-DPS-Calculator"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            commits = json.loads(r.read().decode("utf-8"))
    except Exception as exc:                                    # noqa: BLE001
        raise ProviderError(f"could not reach GitHub for the latest scunpacked build: {exc}")
    for c in commits or []:
        first = ((c.get("commit") or {}).get("message") or "").splitlines()[0].strip()
        if _BUILD_RE.match(first):
            return first, c.get("sha")
    raise ProviderError("no LIVE build label in the last 20 scunpacked-data commits")


def active() -> Optional[dict]:
    """The build the window last loaded: {build, commit, dir} or None."""
    a = _read_json(os.path.join(_root(), ACTIVE_FILE))
    if a and os.path.isfile(os.path.join(_root(), a.get("dir", ""), "meta.json")):
        return a
    return None


def _set_active(build: str, commit: str, dirname: str) -> None:
    S._atomic_write(os.path.join(_root(), ACTIVE_FILE), json.dumps({
        "build": build, "commit": commit, "dir": dirname,
        "activated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }, indent=2).encode("utf-8"))


def download(build: str, commit: str) -> str:
    """Fetch ships.json + ship-items.json for (build, commit) if not already on
    disk; writes meta.json (build, commit, sha256s). Returns the dir name.
    Network + disk only, no JSON parsing, so it is safe on a worker thread."""
    dirname = _dir_name(build, commit)
    # ★ 2026-09-26: PASS THE PINS. This call omitted expect_sha256, so ships.json and
    #   ship-items.json — the 56 MB that IS the shipping dataset — had their hashes
    #   recorded in meta.json and checked against nothing. Recording a hash proves only
    #   that we hashed whatever arrived, which a corrupted or substituted file satisfies
    #   just as well.
    # ⚠ Pinned ONLY for the real pinned build, mirroring DatamineSource.download: the pin
    #   table is keyed by FILENAME, so passing it for some other build would compare a
    #   different build's ships.json against 4.10.1's hash and refuse it. That is exactly
    #   the mistake that broke the Craft Database test when I tried to make the check the
    #   shared default instead of a call-site decision.
    pinned = S.PINNED_SHA256 if (build == S.BUILD and commit == S.COMMIT) else None
    meta = S.fetch_raw(build=dirname, commit=commit,      # the adapter's own fetcher
                       expect_sha256=pinned)
    if meta.get("build") != build:                        # it records the dir name
        meta["build"] = build
        meta["dir"] = dirname
        S._atomic_write(os.path.join(S.cache_dir(dirname), "meta.json"),
                        json.dumps(meta, indent=2).encode("utf-8"))
    return dirname


def ensure_data(allow_fetch: bool = True) -> dict:
    """Make sure one build is on disk and active. Order: the active build; else
    the adapter's pinned build, cached or fetched.

    ⛔ THIS USED TO FALL THROUGH TO ``latest_build()`` — THE NEWEST LIVE COMMIT ON
      GITHUB — WHENEVER NOTHING WAS CACHED, and that made a fresh install
      non-reproducible. Found 2026-09-26 while auditing what the DPS calculator
      actually reads. The machine that has been running a while keeps the pinned
      build forever and never notices; a brand-new install silently comes up on a
      DIFFERENT dataset, so two people comparing numbers can both be running
      "the tool" and disagree, with nothing on screen to say why.
    ★ The bug was invisible to the developer BY CONSTRUCTION: the broken branch is
      the only one a populated cache never takes.
    ⇒ First-run now fetches the PINNED (build, commit), the same one
      ``shared.scunpacked`` hashes and the same one every other skill reads. If
      that commit cannot be fetched we RAISE rather than substituting whatever is
      newest: silently shipping different data is the failure this fix exists to
      prevent, and an error the user can read is strictly better than a number
      they cannot check.
    ⚠ Upgrading is unaffected and stays deliberate — ``check_update`` plus
      ``activate`` (the Refresh the window offers) is how a newer build is adopted,
      and that path is untouched.
    """
    a = active()
    if a:
        return a
    pinned = S.cache_dir(S.BUILD)
    if all(os.path.isfile(os.path.join(pinned, n)) for n in S.FILES):
        meta = _read_json(os.path.join(pinned, "meta.json")) or {}
        _set_active(S.BUILD, meta.get("commit", S.COMMIT), S.BUILD)
        return active()
    if not allow_fetch:
        raise ProviderError("scunpacked-data is not cached yet")
    try:
        dirname = download(S.BUILD, S.COMMIT)
    except Exception as exc:
        raise ProviderError(
            "could not fetch the pinned scunpacked-data build %s (%s). Refusing to "
            "fall back to the newest commit: that would put this install on a "
            "different dataset than every other one, silently." % (S.BUILD, exc))
    _set_active(S.BUILD, S.COMMIT, dirname)
    return active()


def check_update(timeout: float = 15.0) -> Optional[tuple]:
    """(build, commit) when scunpacked-data has a newer build than the active
    one, else None. Raises ProviderError when GitHub is unreachable."""
    a = active()
    build, commit = latest_build(timeout)
    if a and a.get("commit") == commit:
        return None
    return build, commit


def activate(build: str, commit: str) -> dict:
    """Download (if needed) and make (build, commit) the active build."""
    dirname = download(build, commit)
    _set_active(build, commit, dirname)
    return active()


# ── item stats (the dict shapes the window's tables read) ─────────────────

_JUNK = ("placeholder", "_fake", "template", "_test", "dummy", "lowpoly", "_ai_",
         "_npc", "tutorial", "_teach")


def _num(x):
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


# ── resource network (power / cooling / signatures) ───────────────────────
#
# services/power_engine.py simulates erkul's allocator and reads erkul's item
# shape, ``resource.online``. scunpacked carries the same game data as
# ``stdItem.ResourceNetwork.States[Name="Online"]``; this translates one into
# the other, field for field, and nothing else:
#
#   generation.powerSegment          <- Deltas[Type=Generation, Resource=Power].Rate
#   consumption.powerSegment         <- Deltas[Type=Conversion|Consumption, Resource=Power].Rate
#   consumption.power  (guns only)   <- same Rate (erkul keeps guns fractional, e.g. 0.1)
#   conversionMinimumFraction        <- Deltas[Type=Conversion, Resource=Power].MinimumFraction
#   powerConsumptionMinimumFraction  <- Deltas[Type=Consumption, Resource=Power].MinimumFraction
#   generation.cooling / shield / lifeSupport
#                                    <- Deltas[Type=Conversion].GeneratedRate by GeneratedResource
#   powerRanges.low / medium / high  <- PowerRanges[0] / [1] / [2]  (Start, Modifier, RegisterRange)
#   signatureParams.em / ir .nominalSignature
#                                    <- Signature.EM / .IR (absent = 0; stdItem.Emission
#                                       carries the same numbers for every item in 4.10.1)
#
# stdItem.PowerConnection / HeatConnection are the pre-resource-network model
# (kW draw, PowerToEM, TemperatureToIR). In 4.10.1 they appear on one base-
# building item and no ship component, so they are not read.
#
# An item with no Online state, or whose power delta has no Rate (the
# "NetworkReflection" radars), has no power record: its draw shows "—" and
# the engine does not count it, rather than being given a made-up number.

POWER_TYPES = ("WeaponGun", "Shield", "Cooler", "PowerPlant", "Radar", "QuantumDrive",
               "LifeSupportGenerator", "FlightController")
_GENERATED = {"Coolant": "cooling", "Shield": "shield", "LifeSupport": "lifeSupport"}
_BANDS = ("low", "medium", "high")


def _online_state(std: dict) -> Optional[dict]:
    for s in ((std.get("ResourceNetwork") or {}).get("States") or []):
        if s.get("Name") == "Online":
            return s
    return None


def _resource_online(std: dict, gun: bool = False) -> Optional[dict]:
    """erkul ``resource.online`` from a scunpacked item, or None when the item
    has no Online state or no power rate (then nothing about its power is known)."""
    st = _online_state(std)
    if st is None:
        return None
    cons, gen, out = {}, {}, {}
    has_power = False
    for d in st.get("Deltas") or []:
        kind, res = d.get("Type"), d.get("Resource")
        if res == "Power":
            rate = _num(d.get("Rate"))
            if kind == "Generation" and rate is not None:
                gen["powerSegment"] = rate
                has_power = True
            elif kind in ("Conversion", "Consumption") and rate is not None:
                cons["power" if gun else "powerSegment"] = rate
                frac = _num(d.get("MinimumFraction"))
                if frac is not None:
                    key = ("conversionMinimumFraction" if kind == "Conversion"
                           else "powerConsumptionMinimumFraction")
                    out[key] = frac
                if kind == "Conversion" and d.get("GeneratedResource") in _GENERATED:
                    gen[_GENERATED[d["GeneratedResource"]]] = _num(d.get("GeneratedRate")) or 0.0
                has_power = True
        elif res == "Coolant" and kind == "Consumption":
            cons["cooling"] = _num(d.get("Rate")) or 0.0
    if not has_power:
        return None
    ranges = st.get("PowerRanges") or []
    sig = st.get("Signature") or {}
    out.update({
        "consumption": cons,
        "generation": gen,
        "powerRanges": {band: {"start": _num(r.get("Start")) or 0,
                               "modifier": _num(r.get("Modifier")),
                               "registerRange": _num(r.get("RegisterRange")) or 0}
                        for band, r in zip(_BANDS, ranges)},
        "signatureParams": {"em": {"nominalSignature": _num(sig.get("EM")) or 0.0},
                            "ir": {"nominalSignature": _num(sig.get("IR")) or 0.0}},
    })
    return out


def _power_raw(it: dict) -> dict:
    """The erkul-shaped item record power_engine's raw lookup returns."""
    std = it.get("stdItem") or {}
    t = it.get("type") or ""
    raw = {"type": t, "subType": it.get("subType") or "",
           "size": int(it.get("size") or std.get("Size") or 0),
           "name": it.get("name") or std.get("Name") or it.get("className") or "",
           "ref": it.get("reference") or std.get("UUID") or "",
           "localName": (it.get("className") or "").lower()}
    onl = _resource_online(std, gun=(t == "WeaponGun"))
    if onl is not None:
        raw["resource"] = {"online": onl}
    if t == "Shield":
        sh = std.get("Shield") or {}
        res = sh.get("Resistance") or {}
        raw["shield"] = {
            "maxShieldRegen": _num(sh.get("MaxShieldRegen")) or 0.0,
            "resistance": {f"{k}{m}": _num((res.get(v) or {}).get(mm)) or 0.0
                           for k, v in (("physical", "Physical"), ("energy", "Energy"),
                                        ("distortion", "Distortion"))
                           for m, mm in (("Min", "Minimum"), ("Max", "Maximum"))},
        }
    return raw


def _power_draw(it: dict) -> Optional[float]:
    """Segments the item consumes (erkul's ``consumption.powerSegment``); 0 for a
    generator; None when the item has no power record."""
    onl = _resource_online(it.get("stdItem") or {})
    if onl is None:
        return None
    return (onl["consumption"].get("powerSegment") or 0.0) if not onl["generation"].get(
        "powerSegment") else 0.0


def _common(it: dict) -> dict:
    std = it.get("stdItem") or {}
    dd = std.get("DescriptionData") or {}
    dur = std.get("Durability") or {}
    emis = std.get("Emission") or {}
    em = emis.get("Em")
    ir = emis.get("Ir")
    req = std.get("RequiredTags") or []
    if isinstance(req, str):
        req = req.split()
    cls = it.get("className") or ""
    name = it.get("name") or std.get("Name") or cls
    mfr = (std.get("Manufacturer") or {}).get("Code") or it.get("manufacturer") or ""
    junk = (not name or "PLACEHOLDER" in name
            or any(j in cls.lower() for j in _JUNK))
    return {
        "name": name,
        "local_name": cls.lower(),
        "cls": cls,
        "ref": it.get("reference") or std.get("UUID") or "",
        "size": int(it.get("size") or std.get("Size") or 0),
        "grade": dd.get("Grade") or "",
        "class": dd.get("Class") or "",
        "mfr": mfr,
        "hp": _num(dur.get("Health")),
        "em_max": _num(em.get("Maximum")) if isinstance(em, dict) else _num(em),
        "ir_max": _num(ir.get("Maximum")) if isinstance(ir, dict) else _num(ir),
        # power segments consumed (resource network); None = no power record
        "power_draw": _power_draw(it),
        "required_tags": " ".join(req),
        "tags": std.get("Tags") or [],
        "listable": not junk and not req,
        "sub_type": it.get("subType") or "",
    }


def _weapon(it: dict, core: dict) -> dict:
    """Window weapon row. dps_raw / dps_sus / alpha / size are the adapter's."""
    st = _common(it)
    std = it.get("stdItem") or {}
    w = std.get("Weapon") or {}
    m = (w.get("Modes") or [{}])[0]
    ammo = std.get("Ammunition") or {}
    cap = w.get("Capacitor") or {}
    dmg = (w.get("Damage") or {}).get("Alpha") or {}
    brk = {"damagePhysical": _num(dmg.get("Physical")) or 0.0,
           "damageEnergy": _num(dmg.get("Energy")) or 0.0,
           "damageDistortion": _num(dmg.get("Distortion")) or 0.0,
           "damageThermal": _num(dmg.get("Thermal")) or 0.0}
    dom = max(brk, key=brk.get) if any(brk.values()) else "damagePhysical"
    rn = std.get("ResourceNetwork") or {}
    power = 0.0
    for s_ in rn.get("States") or []:
        for d in s_.get("Deltas") or []:
            if d.get("Type") == "Consumption" and d.get("Resource") == "Power":
                power = max(power, _num(d.get("Rate")) or 0.0)
    rpm = core.get("rpm") or 0.0
    dps_raw = core.get("dps_raw") or 0.0
    st.update({
        "size": core["size"],
        "group": (std.get("DescriptionData") or {}).get("Item Type") or "",
        "alpha": core.get("alpha") or 0.0,
        "rps": rpm / 60.0,
        "dps_raw": dps_raw,
        "dps_sus": core.get("dps_sus") or 0.0,
        "ammo": int(_num(w.get("Capacity")) or _num(cap.get("MaxAmmoLoad")) or 0),
        "speed": _num(ammo.get("Speed")) or 0.0,
        "range": _num(ammo.get("Range")) or 0.0,
        "spread": _num((m.get("Spread") or {}).get("Maximum")) or 0.0,
        "power": power,
        "pen": _num((ammo.get("Penetration") or {}).get("BasePenetrationDistance")) or 0.0,
        "wp_hp": st.get("hp") or 0.0,
        "efficiency": 0.0,
        "dmg": brk,
        "dom": dom,
        "fire": core.get("fire"),
        "player": bool(core.get("player")),
        "mountable": bool(core.get("mountable")),
        "listable": bool(core.get("player") and core.get("mountable")),
    })
    return st


def _shield(it):
    st = _common(it)
    sh = (it.get("stdItem") or {}).get("Shield") or {}
    res = sh.get("Resistance") or {}
    st.update({
        "hp": _num(sh.get("MaxShieldHealth")) or 0.0,
        "regen": _num(sh.get("MaxShieldRegen")) or 0.0,
        "res_phys_max": _num((res.get("Physical") or {}).get("Maximum")) or 0.0,
        "res_energy_max": _num((res.get("Energy") or {}).get("Maximum")) or 0.0,
        "res_dist_max": _num((res.get("Distortion") or {}).get("Maximum")) or 0.0,
        "item_hp": _num(((it.get("stdItem") or {}).get("Durability") or {}).get("Health")),
    })
    return st


def _cooler(it):
    st = _common(it)
    gen = (((it.get("stdItem") or {}).get("ResourceNetwork") or {}).get("Generation") or {})
    st["cooling_rate"] = _num(gen.get("Coolant")) or 0.0
    return st


def _powerplant(it):
    st = _common(it)
    gen = (((it.get("stdItem") or {}).get("ResourceNetwork") or {}).get("Generation") or {})
    st["output"] = _num(gen.get("Power")) or 0.0
    return st


def _qdrive(it):
    st = _common(it)
    q = (it.get("stdItem") or {}).get("QuantumDrive") or {}
    j = q.get("StandardJump") or {}
    fr = _num(q.get("FuelRate"))
    st.update({
        "speed": _num(j.get("DriveSpeed")) or 0.0,
        "jump_range": _num(q.get("JumpRange")) or 0.0,
        "spool": _num(j.get("SpoolUpTime")) or 0.0,
        "cooldown": _num(j.get("CooldownTime")) or 0.0,
        "fuel_rate": fr * 1e6 if fr else 0.0,          # per Mm
    })
    return st


def _radar(it):
    st = _common(it)
    st.update({"detection_min": None, "detection_max": None})
    return st


_TRACK = {"Infrared": "IR", "Electromagnetic": "EM", "CrossSection": "CS"}


def _missile(it):
    st = _common(it)
    m = (it.get("stdItem") or {}).get("Missile") or {}
    tg = m.get("Targeting") or {}
    st.update({
        "tracking": _TRACK.get(tg.get("TrackingSignalType"), tg.get("TrackingSignalType") or ""),
        "total_dmg": _num(m.get("DamageTotal")) or 0.0,
        "speed": _num((m.get("GCS") or {}).get("LinearSpeed")) or 0.0,
        "lock_range": _num(tg.get("LockRangeMax")) or 0.0,
        "lock_time": _num(tg.get("LockTime")) or 0.0,
    })
    return st


def _rack(it):
    st = _common(it)
    r = (it.get("stdItem") or {}).get("MissileRack") or {}
    st.update({"missile_count": _num(r.get("MissileCount")) or 0,
               "missile_size": _num(r.get("MissileSize")) or 0})
    return st


def _emp(it):
    st = _common(it)
    e = (it.get("stdItem") or {}).get("Emp") or {}
    st.update({"charge_time": _num(e.get("ChargeTime")), "cooldown_time": _num(e.get("CooldownTime")),
               "emp_radius": _num(e.get("EmpRadius")), "distortion_dmg": _num(e.get("DistortionDamage"))})
    return st


def _bomb(it):
    st = _common(it)
    b = (it.get("stdItem") or {}).get("Bomb") or {}
    er = b.get("ExplosionRadius") or {}
    st.update({"arm_time": _num(b.get("ArmTime")), "max_lifetime": _num(b.get("MaxLifetime")),
               "min_radius": _num(er.get("Minimum")), "max_radius": _num(er.get("Maximum"))})
    return st


def _mining_laser(it):
    st = _common(it)
    ml = (it.get("stdItem") or {}).get("MiningLaser") or {}
    mods = ml.get("Modifiers") or {}
    st.update({"instability": _num(mods.get("Instability")),
               "resistance_mod": _num(mods.get("Resistance")),
               "filter_mod": None, "throttle_min": _num(ml.get("ThrottleMinimum")),
               "module_slots": None})
    return st


def _container(it):
    st = _common(it)
    rc = (it.get("stdItem") or {}).get("ResourceContainer") or {}
    st["capacity"] = _num((rc.get("Capacity") or {}).get("SCU"))
    return st


# ship-items.json type -> (window kind, stats fn)
_KINDS = {
    "Shield": ("shields", _shield), "Cooler": ("coolers", _cooler),
    "PowerPlant": ("powerplants", _powerplant), "QuantumDrive": ("qdrives", _qdrive),
    "Radar": ("radars", _radar), "Missile": ("missiles", _missile),
    "MissileLauncher": ("missile_racks", _rack), "EMP": ("emps", _emp),
    "QuantumInterdictionGenerator": ("qeds", _common), "Bomb": ("bombs", _bomb),
    "BombLauncher": ("bombs", _common), "WeaponMining": ("mining_lasers", _mining_laser),
    "ToolArm": ("tool_arms", _common), "SalvageHead": ("salvage_heads", _common),
    "SalvageModifier": ("salvage_modifiers", _common), "MiningModifier": ("mining_modifiers", _common),
    "ExternalFuelTank": ("fuel_tanks", _container), "Module": ("modules", _common),
    "Turret": ("mounts", _common), "TurretBase": ("turrets", _common),
}
_SUBTYPE_KINDS = {("Container", "Cargo"): ("ore_pods", _container)}


def _uniquify(rows: list) -> None:
    """Give every row of one kind a unique display name. The window stores a
    selection by NAME, so two items sharing one (a player gun and its NPC twin)
    must not collide: the listable / shortest-class one keeps the plain name,
    the others get the class name appended."""
    by = {}
    for r in rows:
        by.setdefault(r["name"], []).append(r)
    for name, grp in by.items():
        if len(grp) < 2:
            continue
        grp.sort(key=lambda r: (not r.get("listable"), len(r["cls"]), r["cls"]))
        for r in grp[1:]:
            r["name"] = f"{name} ({r['cls']})"


# ── ship tree translation (scunpacked Loadout -> erkul-shaped ports) ──────

_KEEP_TYPES = {"WeaponGun", "Turret", "TurretBase", "MissileLauncher", "Missile",
               "BombLauncher", "Bomb", "Shield", "Cooler", "Radar", "PowerPlant",
               "QuantumDrive", "JumpDrive", "EMP", "QuantumInterdictionGenerator",
               "WeaponMining", "MiningModifier", "ToolArm", "SalvageHead",
               "SalvageModifier", "Container", "ExternalFuelTank", "Module",
               "TractorBeam", "WeaponMount"}


def _types_of(e: dict) -> set:
    t = {c.get("Type") for c in e.get("CompatibleTypes") or []}
    if e.get("Type"):
        t.add(e["Type"].split(".", 1)[0])
    return t


def _translate(entries: list) -> list:
    """Keep the ports the window's extractors care about, in erkul's shape:
    itemPortName / itemTypes / editable / minSize / maxSize / localName /
    localReference / loadout. Weapon attachments, thrusters, seats, lights,
    doors and controllers are dropped."""
    out = []
    for e in entries or []:
        kids = [] if (e.get("Type") or "").startswith("WeaponGun") else _translate(e.get("Loadout"))
        rel = _types_of(e) & _KEEP_TYPES
        if rel == {"Container"} and not (e.get("ClassName") or "").lower().startswith("cargo_"):
            rel = set()          # only mining ore pods are shown; bay walls, covers, racks are not
        if not rel and not kids:
            continue
        types = []
        if e.get("ClassName") and e.get("Type"):
            # an occupied port is classified by its INSTALLED item, as erkul
            # did: a missile-rack port that could also take a bomb rack is a
            # missile rack while it holds one (otherwise every such port also
            # shows up as an empty bomb slot)
            t, _, sub = e["Type"].partition(".")
            types.append({"type": t, "subType": "" if sub in ("", "UNDEFINED") else sub})
        else:
            for c in e.get("CompatibleTypes") or []:
                subs = c.get("SubTypes") or [""]
                for sub in subs:
                    types.append({"type": c.get("Type"), "subType": sub})
        p = {"itemPortName": e.get("HardpointName") or "",
             "itemTypes": types,
             "editable": bool(e.get("Editable")),
             "minSize": e.get("MinSize"),
             "maxSize": e.get("MaxSize")}
        if e.get("ClassName"):
            p["localName"] = e["ClassName"].lower()
            p["localReference"] = e.get("UUID") or ""
            p["itemName"] = e.get("Name") or ""
        if e.get("RequiredTags"):
            p["requiredTags"] = " ".join(e["RequiredTags"])
        if kids:
            p["loadout"] = kids
        out.append(p)
    return out


def _path_map(entries, path=(), out=None) -> dict:
    out = {} if out is None else out
    for e in entries or []:
        p = path + (e.get("HardpointName") or "?",)
        out["/".join(p)] = e
        _path_map(e.get("Loadout"), p, out)
    return out


def _label(hp: str) -> str:
    s = re.sub(r"hardpoint_|_weapon$|weapon_", "", hp or "", flags=re.I)
    s = re.sub(r"_+", " ", s).strip()
    return s.title() if s else (hp or "?")


_TURRET_MOUNTS = {"manned turret", "ball turret", "nose turret", "canard turret",
                  "top turret", "remote turret", "turret"}
_EXCLUDED = {"pdc": "automated point-defence turret (not a pilot/gunner weapon)",
             "crew mount": "crew-operated door gun, locked to its stock weapon"}


def _gun_slots(ship_raw: dict, entry: dict, weapons: dict) -> tuple:
    """Group the adapter's gun slots into window rows (one row per hardpoint
    whose guns share size range, stock, lock and tags; ``gun_count`` = guns).
    Returns (rows, not_counted)."""
    paths = _path_map(ship_raw.get("Loadout"))
    rows, not_counted = [], []
    for sl in entry["slots"]:
        parts = sl["id"].split("/")
        parent = paths.get("/".join(parts[:-1])) if len(parts) > 1 else None
        pcls = (parent or {}).get("ClassName") or ""
        mount_name = (parent or {}).get("Name") if pcls.lower().startswith("mount_") else ""
        stock_w = weapons.get(sl["stock"]) if sl["stock"] else None
        if sl["mount"] in _EXCLUDED:
            not_counted.append({
                "id": sl["id"], "label": _label(parts[0]), "reason": _EXCLUDED[sl["mount"]],
                "size": sl["max_size"], "weapon": stock_w["name"] if stock_w else "empty"})
            continue
        is_turret = sl["mount"] in _TURRET_MOUNTS
        key = (parts[0], sl["min_size"], sl["max_size"], sl["stock"], sl["editable"],
               tuple(sl["tags"]), sl["mount"])
        prev = rows[-1] if rows else None
        if prev and prev["_key"] == key:
            prev["gun_count"] += 1
            prev["sc_slots"].append(sl)
            continue
        # the adapter's slot["turret"] is the turret's HardpointName (a string)
        label = _label(parts[0])
        turret_hp = sl.get("turret") or ""
        turret_item = ""
        if is_turret and turret_hp:
            if turret_hp != parts[0]:
                label = f"{label} / {_label(turret_hp)}"
            for i in range(len(parts) - 1, 0, -1):
                if parts[i - 1] == turret_hp:
                    turret_item = (paths.get("/".join(parts[:i])) or {}).get("Name") or ""
                    break
        extra = []
        if sl["min_size"] != sl["max_size"]:
            extra.append(f"S{sl['min_size']}-S{sl['max_size']}")
        if mount_name:
            extra.append(mount_name)
        elif sl["mount"] not in ("fixed", "gimbal"):
            extra.append(sl["mount"] + (f": {turret_item}" if turret_item
                                        and "PLACEHOLDER" not in turret_item else ""))
        if not sl["editable"]:
            extra.append("locked")
        if extra:
            label += "  [" + ", ".join(extra) + "]"
        rows.append({
            "_key": key,
            "id": "sc:" + sl["id"],
            "label": label,
            "max_size": sl["max_size"],
            "min_size": sl["min_size"],
            "weapon_max_size": sl["max_size"],
            "editable": sl["editable"],
            "local_ref": (sl["stock"] or "").lower(),
            "outer_ref": "",
            "mount": sl["mount"],
            "mount_name": mount_name or "",
            "turret": is_turret,
            "gun_count": 1,
            "sc_slots": [sl],
        })
    for r in rows:
        r.pop("_key", None)
    return rows, not_counted


def _untype_not_counted(loadout: list, not_counted: list) -> None:
    """Point-defence turrets and crew door guns are left out of the window's gun
    totals (``_EXCLUDED``); leave them out of the power budget the same way.
    erkul's allocator never counted them either, by accident of its data
    (checked against the May erkul cache): its PDC ports (Idris, Polaris, 890
    Jump, Perseus, Phoenix) were untyped, pointed at a turret its catalog did
    not hold, and had no gun under them; its crew-mount GT-210 (Asgard,
    Valkyrie, Cutlass Steel, Starlancer TAC) is a class its weapon catalog did
    not hold, so the lookup failed. Here the gun port under each one gets no
    item type and its item is kept under ``stockLocalName`` / ``stockReference``
    instead of the keys the power allocator resolves: the same outcome, stated
    as a rule instead of inherited from gaps."""
    for nc in not_counted or []:
        ports, port = loadout, None
        for part in nc["id"].split("/"):
            port = next((p for p in ports if (p.get("itemPortName") or "").lower()
                         == part.lower()), None)
            if port is None:
                break
            ports = port.get("loadout") or []
        if port is not None:
            port["itemTypes"] = []
            if "localName" in port:
                port["stockLocalName"] = port.pop("localName")
            if "localReference" in port:
                port["stockReference"] = port.pop("localReference")
            port["not_counted"] = nc["reason"]


def _installed(entries, type_prefix: str, out=None) -> list:
    """Class names (lower) of every item of one type installed anywhere in a
    scunpacked Loadout tree, in tree order."""
    out = [] if out is None else out
    for e in entries or []:
        if (e.get("Type") or "").split(".", 1)[0] == type_prefix and e.get("ClassName"):
            out.append(e["ClassName"].lower())
        _installed(e.get("Loadout"), type_prefix, out)
    return out


def _power_ship_fields(raw: dict, power_raw: dict) -> dict:
    """Ship-level inputs of services/power_engine.py, in erkul's shape:

      rnPowerPools.weaponGun.poolSize  <- PowerPools.WeaponGun.Size (Size -1 = dynamic
                                          pool: no fixed weapon pips, as erkul's
                                          {"type": "dynamic"})
      ifcs.resource.online             <- the installed FlightController item's Online
                                          state (engine pips + minimum fraction)
      items.lifeSupports[].data        <- every installed LifeSupportGenerator
      armor.data.armor.signal*         <- Armor.SignalMultipliers.{Electromagnetic,
                                          Infrared, CrossSection}
    Not carried by scunpacked: erkul's ``buff.regenModifier`` (crew weapon-regen
    multipliers). It is left out, so the engine uses its neutral 1.0."""
    pools = {}
    for key, name in (("weaponGun", "WeaponGun"),):
        size = ((raw.get("PowerPools") or {}).get(name) or {}).get("Size")
        if isinstance(size, (int, float)) and size >= 0:
            pools[key] = {"type": "fixed", "poolSize": int(size)}
        elif size is not None:
            pools[key] = {"type": "dynamic"}
    fc_res = None
    for cls in _installed(raw.get("Loadout"), "FlightController"):
        rec = power_raw.get(cls)
        if rec and rec.get("resource"):
            fc_res = rec["resource"]
            break
    lss = [{"localName": cls, "data": power_raw[cls]}
           for cls in _installed(raw.get("Loadout"), "LifeSupportGenerator")
           if cls in power_raw and power_raw[cls].get("resource")]
    sm = (raw.get("Armor") or {}).get("SignalMultipliers") or {}
    # Installed power components the engine cannot see, said out loud (the
    # window shows "—" for the signatures of a ship with any gap)
    gaps = []
    if _installed(raw.get("Loadout"), "WheeledController"):
        gaps.append("ground-vehicle drive controller (WheeledController): no item "
                    "power record in scunpacked-data (only its segment count, not "
                    "its minimum fraction)")
    elif fc_res is None and raw.get("IsSpaceship"):
        gaps.append("flight controller: no power record")
    for t in POWER_TYPES:
        if t in ("WeaponGun", "FlightController"):
            continue
        for cls in _installed(raw.get("Loadout"), t):
            if not (power_raw.get(cls) or {}).get("resource"):
                gaps.append(f"{t} {cls}: no power record")
    return {
        "rnPowerPools": pools,
        "ifcs_resource": fc_res,
        "power_gaps": gaps,
        "items": {"lifeSupports": lss},
        "armor_signals": {"signalElectromagnetic": sm.get("Electromagnetic", 1),
                          "signalInfrared": sm.get("Infrared", 1),
                          "signalCrossSection": sm.get("CrossSection", 1)},
    }


def _ship(raw: dict, entry: dict, weapons: dict, armor_types: dict,
          power_raw: Optional[dict] = None) -> dict:
    arm = raw.get("Armor") or {}
    rm = arm.get("ResistanceMultipliers") or {}
    ifcs = ((raw.get("FlightCharacteristics") or {}).get("IFCS") or {})
    cs = raw.get("CrossSection") or {}
    rows, not_counted = _gun_slots(raw, entry, weapons)
    pw = _power_ship_fields(raw, power_raw or {})
    ifcs_d = {"scmSpeed": ifcs.get("ScmSpeed") or 0,
              "maxAfterburnSpeed": ifcs.get("BoostSpeedForward") or 0}
    if pw["ifcs_resource"]:
        ifcs_d["resource"] = pw["ifcs_resource"]
    loadout = _translate(raw.get("Loadout"))
    _untype_not_counted(loadout, not_counted)
    return {
        "name": raw.get("Name"),
        "ref": raw.get("UUID") or "",
        "className": raw.get("ClassName"),
        "loadout": loadout,
        "power_gaps": pw["power_gaps"],
        "armor": {"data": {
            "subType": armor_types.get(arm.get("UUID"), ""),
            "health": {"hp": arm.get("Health") or 0,
                       "damageResistanceMultiplier": {
                           "physical": rm.get("Physical", 1), "energy": rm.get("Energy", 1),
                           "distortion": rm.get("Distortion", 1)}},
            "armor": pw["armor_signals"]}},
        "rnPowerPools": pw["rnPowerPools"],
        "items": pw["items"],
        "hull": {"totalHp": raw.get("Health") or 0},
        "ifcs": ifcs_d,
        "qtFuelCapacity": (raw.get("QuantumTravel") or {}).get("FuelCapacity") or 0,
        "fuelCapacity": (raw.get("Propulsion") or {}).get("FuelCapacity") or 0,
        "cargo": raw.get("Cargo") or 0,
        "vehicle": {"crewSize": raw.get("Crew") or "?"},
        "crossSection": {"x": cs.get("X", 0), "y": cs.get("Y", 0), "z": cs.get("Z", 0)},
        "is_vehicle": bool(raw.get("IsVehicle")),
        "sc_gun_slots": rows,
        "sc_not_counted": not_counted,
        "sc_empty_turret_ports": entry.get("empty_turret_ports") or [],
    }


# ── the window index ──────────────────────────────────────────────────────

def build_window_index(a: dict) -> dict:
    """Parse the raw files once and derive everything the window needs."""
    dirname = a["dir"]
    d = S.cache_dir(dirname)
    gidx = S.load_index(dirname, allow_fetch=False)       # adapter: slots + weapons
    gw = gidx["weapons"]

    with open(os.path.join(d, "ship-items.json"), encoding="utf-8") as f:
        items = json.load(f)
    kinds: dict = {k: [] for k, _fn in _KINDS.values()}
    kinds["weapons"] = []
    kinds["ore_pods"] = []
    armor_types = {}
    power_raw = {}           # class (lower) -> erkul-shaped record for power_engine
    for it in items:
        t, sub = it.get("type"), it.get("subType")
        if t in POWER_TYPES and it.get("className"):
            power_raw[it["className"].lower()] = _power_raw(it)
        if t == "Armor":
            armor_types[it.get("reference")] = sub or ""
            continue
        if t == "WeaponGun":
            core = gw.get(it.get("className"))
            if core:
                kinds["weapons"].append(_weapon(it, core))
            continue
        spec = _SUBTYPE_KINDS.get((t, sub)) or _KINDS.get(t)
        if not spec:
            continue
        kind, fn = spec
        try:
            kinds[kind].append(fn(it))
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            _log.warning("scunpacked item %s skipped: %s", it.get("className"), exc)
    del items
    for rows in kinds.values():
        _uniquify(rows)

    with open(os.path.join(d, "ships.json"), encoding="utf-8") as f:
        raw_ships = json.load(f)
    ships, seen = {}, {}
    weapons_by_cls = {w["cls"]: w for w in kinds["weapons"]}
    ordered = sorted((s for s in raw_ships if s.get("ClassName") and not s.get("IsPowerSuit")),
                     key=lambda s: (len(s["ClassName"]), s["ClassName"]))
    for raw in ordered:
        entry = gidx["ships"].get(raw["ClassName"])
        if not entry:
            continue
        sd = _ship(raw, entry, weapons_by_cls, armor_types, power_raw)
        name = sd["name"] or raw["ClassName"]
        if name in seen:                        # duplicate display names: variants
            name = f"{name} ({raw['ClassName']})"
        seen[name] = 1
        sd["name"] = name
        ships[name] = sd
    del raw_ships

    meta = _read_json(os.path.join(d, "meta.json")) or {}
    return {
        "provider_version": PROVIDER_VERSION,
        "adapter_version": S.ADAPTER_VERSION,
        "build": a["build"], "commit": a["commit"], "dir": dirname,
        "source": S.REPO, "fetched_at": meta.get("fetched_at"),
        "file_sha256": {k: v.get("sha256") for k, v in (meta.get("files") or {}).items()},
        "ships": ships,
        "items": kinds,
        "adapter_weapons": gw,
        "power_raw": power_raw,
    }


def load_window_index(allow_fetch: bool = True, on_status: Optional[Callable] = None) -> dict:
    """The window index for the active build; builds it once per build."""
    a = ensure_data(allow_fetch=allow_fetch)
    path = os.path.join(S.cache_dir(a["dir"]), INDEX_FILE)
    idx = _read_json(path)
    if (idx and idx.get("provider_version") == PROVIDER_VERSION
            and idx.get("adapter_version") == S.ADAPTER_VERSION
            and idx.get("commit") == a["commit"]):
        return idx
    if on_status:
        on_status(f"Building scunpacked index for {a['build']}…")
    idx = build_window_index(a)
    S._atomic_write(path, json.dumps(idx, separators=(",", ":")).encode("utf-8"))
    return idx


# ── slot-aware candidates (the fit rule is the adapter's) ─────────────────

def candidates(slot: dict, adapter_weapons: dict, window_weapons: dict) -> list:
    """Window weapon rows that fit a window gun slot: ``S.fits`` on the first
    adapter slot of the group (all guns of a group share size, stock, lock and
    tags). The stock weapon is always first."""
    sl = slot["sc_slots"][0]
    out = [window_weapons[w["cls"]] for w in S.candidates_for(sl, adapter_weapons, "dps_raw")
           if w["cls"] in window_weapons]
    out.sort(key=lambda w: (-w["size"], w["name"]))
    stock = window_weapons.get(sl["stock"]) if sl.get("stock") else None
    if stock is not None:
        out = [stock] + [w for w in out if w is not stock]
    return out
