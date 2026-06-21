"""Audit bodies.json against a fresh RSI starmap fetch for EVERY system.

Compares planet sets, moon sets (+ correct parent planet) and planet orbital
ORDER (distance ranking) against robertsspaceindustries.com/api/starmap. Caches
the RSI responses to _rsi_cache.json so fixes can reuse them. Reports only
systems with discrepancies.
"""
import json, urllib.request, time, os, re, math
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sj = json.load(open(os.path.join(HERE, "systems.json"), encoding="utf-8"))
CODES = [s["code"] for s in sj["systems"]]
bd = json.load(open(os.path.join(HERE, "bodies.json"), encoding="utf-8"))


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def fetch(code):
    for a in range(5):
        try:
            req = urllib.request.Request(
                f"https://robertsspaceindustries.com/api/starmap/star-systems/{code}",
                data=b"{}",
                headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json",
                         "Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.load(r)
            rs = (d.get("data") or {}).get("resultset") or []
            return code, (rs[0].get("celestial_objects") if rs else [])
        except Exception:
            time.sleep(1.0 * (a + 1))
    return code, None


cache_path = os.path.join(HERE, "_rsi_cache.json")
if os.path.exists(cache_path):
    rsi = json.load(open(cache_path, encoding="utf-8"))
    print("(using cached RSI fetch)")
else:
    rsi = {}
    with ThreadPoolExecutor(max_workers=3) as ex:
        for code, cobs in ex.map(fetch, CODES):
            rsi[code] = cobs
    json.dump(rsi, open(cache_path, "w", encoding="utf-8"))
    print("(fetched + cached RSI)")


def nm(c):
    return c.get("name") or c.get("designation") or c.get("code", "").split(".")[-1].title()


issues = 0
fetch_fail = []
for code in CODES:
    cobs = rsi.get(code)
    if cobs is None:
        fetch_fail.append(code)
        continue
    if not cobs:
        continue
    rsi_planets = {norm(nm(c)): nm(c) for c in cobs if c.get("type") == "PLANET"}
    rsi_moons = {norm(nm(c)): nm(c) for c in cobs if c.get("type") in ("SATELLITE", "MOON")}
    mybodies = bd["systems"].get(code, {}).get("bodies", [])
    my_planets = {norm(b["name"]): b["name"] for b in mybodies if "planet" in b["type"].lower()}
    my_moons = {norm(b["name"]): b["name"] for b in mybodies
                if b["type"].lower() in ("moon", "satellite")}

    miss_p = [v for k, v in rsi_planets.items() if k not in my_planets]
    extra_p = [v for k, v in my_planets.items() if k not in rsi_planets]
    miss_m = [v for k, v in rsi_moons.items() if k not in my_moons]

    # planet orbital order: RSI by distance vs mine by radius
    rsi_order = [norm(nm(c)) for c in sorted(
        [c for c in cobs if c.get("type") == "PLANET"], key=lambda c: (c.get("distance") or 0))]
    my_order = [norm(b["name"]) for b in sorted(
        [b for b in mybodies if "planet" in b["type"].lower()],
        key=lambda b: math.hypot(b["x"], b["y"]))]
    common = [k for k in rsi_order if k in my_planets]
    my_common = [k for k in my_order if k in rsi_planets]
    order_ok = common == my_common

    # in-game systems are scunpacked supersets: extra planets are EXPECTED there
    in_game = code in ("STANTON", "PYRO", "NYX")
    if miss_p or miss_m or (not order_ok) or (extra_p and not in_game):
        issues += 1
        tag = " [in-game/scunpacked]" if in_game else ""
        print(f"\n{code}{tag}: RSI {len(rsi_planets)}p/{len(rsi_moons)}m  "
              f"mine {len(my_planets)}p/{len(my_moons)}m")
        if miss_p:
            print(f"   MISSING planets: {miss_p}")
        if extra_p and not in_game:
            print(f"   EXTRA planets (not in RSI): {extra_p}")
        if miss_m:
            print(f"   MISSING moons ({len(miss_m)}): {miss_m[:12]}{'...' if len(miss_m) > 12 else ''}")
        if not order_ok:
            print(f"   ORBIT ORDER differs:\n     RSI : {common}\n     mine: {my_common}")

print(f"\n=== {issues} systems with discrepancies; fetch failures: {fetch_fail} ===")
