"""One-shot re-populate of bodies.json for ALL lore systems from the RSI starmap.

Keeps the in-game systems (STANTON/PYRO/NYX) exactly as-is, replaces every other
system with fresh RSI cartography: stars, planets (+orbits), moons, stations,
asteroid belts, and JUMP POINTS (named after their destination system so the
system view can offer a 'Jump to X' button). Radial log scale so inner and outer
bodies are all legible. Re-adds the CIG HQ surface easter egg on Earth.
"""
import json, urllib.request, math, time, os
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sj = json.load(open(os.path.join(HERE, "systems.json"), encoding="utf-8"))
NAME = {s["code"]: (s.get("name") or s["code"].title()) for s in sj["systems"]}
KEEP = {"STANTON", "PYRO", "NYX"}
CODES = [s["code"] for s in sj["systems"] if s["code"] not in KEEP]
GOLDEN = math.pi * (3 - math.sqrt(5))


def fetch(code):
    for a in range(5):
        try:
            req = urllib.request.Request(
                f"https://robertsspaceindustries.com/api/starmap/star-systems/{code}",
                data=b"{}",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                         "Content-Type": "application/json"},
                method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            rs = (d.get("data") or {}).get("resultset") or []
            return code, (rs[0].get("celestial_objects") if rs else [])
        except Exception:
            time.sleep(1.0 * (a + 1))
    return code, None


def log_r(au):
    return math.log10(1.0 + max(au or 0.0, 0.0) / 0.05) * 1e11


def base_ang(name):
    return (sum(ord(c) for c in name) % 360) / 360.0 * 2 * math.pi


results = {}
with ThreadPoolExecutor(max_workers=3) as ex:
    for code, cobs in ex.map(fetch, CODES):
        results[code] = cobs

bd = json.load(open(os.path.join(HERE, "bodies.json"), encoding="utf-8"))
ok = 0
nodata = []
failed = []
for code in CODES:
    cobs = results.get(code)
    if cobs is None:
        failed.append(code)
        continue
    if not cobs:
        nodata.append(code)
        continue
    byid = {c.get("id"): c for c in cobs}

    def nm(c):
        return c.get("name") or c.get("designation") or c.get("code", "").split(".")[-1].title()

    stars = [c for c in cobs if c.get("type") == "STAR"]
    planets = [c for c in cobs if c.get("type") == "PLANET"]
    moons = [c for c in cobs if c.get("type") in ("SATELLITE", "MOON")]
    manmade = [c for c in cobs if c.get("type") == "MANMADE"]
    jumps = [c for c in cobs if c.get("type") == "JUMPPOINT"]
    belts = [c for c in cobs if c.get("type") == "ASTEROID_BELT"]

    pos = {}
    out = []
    prim = stars[0] if stars else None
    prim_name = nm(prim) if prim else NAME.get(code, code.title())
    if prim:
        pos[prim["id"]] = (0.0, 0.0, 0.0)
    out.append({"name": prim_name, "type": "Star", "x": 0.0, "y": 0.0, "z": 0.0, "parent": None})
    # extra stars (binary) — small offset around the barycentre
    for i, s in enumerate(stars[1:], start=1):
        r = 2.2e10
        ang = GOLDEN * i
        x, y = r * math.cos(ang), r * math.sin(ang)
        pos[s["id"]] = (x, y, 0.0)
        out.append({"name": nm(s), "type": "Star", "x": x, "y": y, "z": 0.0, "parent": prim_name})
    # planets (orbit barycentre)
    for i, c in enumerate(sorted(planets, key=lambda c: (c.get("distance") or 0))):
        au = c.get("distance") or (0.4 + i * 0.6)
        r = log_r(au)
        ang = GOLDEN * i + base_ang(nm(c))
        x, y = r * math.cos(ang), r * math.sin(ang)
        pos[c["id"]] = (x, y, 0.0)
        out.append({"name": nm(c), "type": "Planet", "x": x, "y": y, "z": 0.0, "parent": prim_name})
    # moons (orbit their parent planet)
    by_parent = {}
    for c in moons:
        by_parent.setdefault(c.get("parent_id"), []).append(c)
    for pid, group in by_parent.items():
        ppos = pos.get(pid, (0.0, 0.0, 0.0))
        pname = nm(byid[pid]) if pid in byid else prim_name
        for j, c in enumerate(sorted(group, key=lambda c: (c.get("distance") or 0))):
            r = max(log_r(c.get("distance")), 8e8)
            ang = GOLDEN * j + base_ang(nm(c))
            x, y = ppos[0] + r * math.cos(ang), ppos[1] + r * math.sin(ang)
            pos[c["id"]] = (x, y, 0.0)
            out.append({"name": nm(c), "type": "Moon", "x": x, "y": y, "z": 0.0, "parent": pname})
    # stations (orbit their parent body)
    for k, c in enumerate(manmade):
        pid = c.get("parent_id")
        ppos = pos.get(pid, (0.0, 0.0, 0.0))
        pname = nm(byid[pid]) if pid in byid else prim_name
        r = max(log_r(c.get("distance")), 5e8)
        ang = GOLDEN * k + 0.5
        x, y = ppos[0] + r * math.cos(ang), ppos[1] + r * math.sin(ang)
        out.append({"name": nm(c), "type": "Manmade", "x": x, "y": y, "z": 0.0, "parent": pname})
    # jump points — named after their DESTINATION system (drives 'Jump to X')
    jk = 0
    for c in jumps:
        destcode = (c.get("code", "").split(".")[-1] or "").upper()
        if destcode not in NAME:
            continue                       # skip jumps to systems we can't open
        destname = NAME[destcode]
        r = max(log_r(c.get("distance")), 1.0e11)
        ang = GOLDEN * jk + 1.1
        jk += 1
        x, y = r * math.cos(ang), r * math.sin(ang)
        out.append({"name": destname, "type": "Jumppoint", "x": x, "y": y, "z": 0.0, "parent": prim_name})
    # asteroid belts — a ring at their orbital radius
    for c in belts:
        r = log_r(c.get("distance"))
        out.append({"name": nm(c), "type": "AsteroidBelt", "x": r, "y": 0.0, "z": 0.0, "parent": prim_name})

    bd["systems"][code] = {"bodies": out}
    ok += 1

# re-add the CIG HQ surface easter egg on Earth (Sol)
sol = bd["systems"].get("SOL", {}).get("bodies")
if sol:
    earth = next((b for b in sol if b["name"] == "Earth"), None)
    if earth and not any(b["name"] == "CIG Headquarters" for b in sol):
        sol.append({"name": "CIG Headquarters", "type": "LandingZone",
                    "x": earth["x"] + 1e7, "y": earth["y"] + 1.2e7, "z": earth["z"] + 4e7,
                    "parent": "Earth"})

tmp = os.path.join(HERE, "bodies.json.tmp")
json.dump(bd, open(tmp, "w", encoding="utf-8"))
os.replace(tmp, os.path.join(HERE, "bodies.json"))
print(f"populated={ok}  nodata={len(nodata)}  failed={failed}")
print("nodata systems:", nodata)
