"""Plain answers: a tool result turned into one or two spoken sentences
with no model, plus the grounding check the optional LLM phrasing must
pass.

plain_answer(tool, args, result) reads the result shapes the workers
actually return (workers/h_*.py, headless.py). Every number and name it
says is copied from the result, so router mode cannot invent a fact. An
unknown shape falls back to the result's own top-level scalars.

numbers_in() / ungrounded_numbers() are what agent.py uses to reject an
LLM rephrasing that says a number the result and the question never did.
"""
from __future__ import annotations

import json
import re


def _n(x) -> str:
    """A number as it should be spoken: 247680 -> 247,680, 3.50 -> 3.5."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if f == int(f):
        return f"{int(f):,}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


def _join(parts: list) -> str:
    parts = [p for p in parts if p]
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _where(row: dict) -> str:
    t = row.get("terminal") or row.get("location") or "?"
    s = row.get("system")
    return f"{t} ({s})" if s else t


def plain_answer(tool: str, args: dict, result) -> str:
    if isinstance(result, dict) and result.get("error"):
        return f"I couldn't get that: {result['error']}"
    if not isinstance(result, dict):
        return str(result)[:300]
    try:
        fn = _FORMATTERS.get(tool)
        text = fn(args or {}, result) if fn else ""
    except Exception:                                       # noqa: BLE001
        text = ""
    if not text:
        text = _generic(result)
    if result.get("empty") and result.get("note") and result["note"] not in text:
        text = (text + " " if text else "") + _cap(result["note"]) + "."
    return text.strip()


def _cap(s: str) -> str:
    s = str(s).strip().rstrip(".")
    return s[:1].upper() + s[1:]


def _generic(r: dict) -> str:
    bits = []
    for k, v in r.items():
        if k in ("note", "other_matches", "attribution", "log_path", "sc_folder") or \
                isinstance(v, (dict, list)) or v in (None, ""):
            continue
        bits.append(f"{k.replace('_', ' ')} {v if not isinstance(v, (int, float)) else _n(v)}")
        if len(bits) == 4:
            break
    return ("Here's what I found: " + "; ".join(bits) + ".") if bits else "Done."


# ── one formatter per tool, over the real result shapes ───────────────────

def _ship_info(a, r):
    return f"The {_proper(r.get('ship', '?'))} holds {_n(r.get('scu'))} SCU."


def _ship_buy_rent(a, r):
    ship = r.get("ship") or a.get("ship")
    if r.get("empty"):
        return f"You can't buy or rent the {ship} in game right now."
    out = []
    buy = r.get("buy") or []
    if buy:
        out.append(f"Buy the {ship} at {_where(buy[0])} for {_n(buy[0].get('price'))} aUEC"
                   + (f", or {_where(buy[1])} for {_n(buy[1].get('price'))}" if len(buy) > 1 else "") + ".")
    rent = r.get("rent_per_day") or []
    if rent:
        out.append(f"Rent it at {_where(rent[0])} for {_n(rent[0].get('price'))} aUEC a day.")
    return " ".join(out)


def _proper(name) -> str:
    """The trade and cargo workers return SHIP_PRESETS keys, all lowercase."""
    s = str(name or "")
    return s.title() if s and s == s.lower() else s


def _trade(a, r):
    routes = r.get("routes") or []
    if not routes:
        return ""
    x = routes[0]
    ship = _proper(r.get("ship") or a.get("ship")) or "your ship"
    scu = r.get("ship_scu")
    head = f"Best run for the {ship}" + (f" ({_n(scu)} SCU)" if scu else "")
    return (f"{head}: {x.get('commodity')}, buy at {x.get('buy_terminal')} in "
            f"{x.get('buy_system')} for {_n(x.get('price_buy'))}, sell at "
            f"{x.get('sell_terminal')} in {x.get('sell_system')} for {_n(x.get('price_sell'))}. "
            f"About {_n(x.get('est_profit'))} aUEC profit on {_n(x.get('effective_scu'))} SCU.")


def _item_price(a, r):
    buys = r.get("cheapest_buy") or []
    item = r.get("item") or a.get("item")
    if not buys:
        sells = r.get("best_sell") or []
        return (f"Nobody lists {item} for sale; it sells best at {_where(sells[0])} for "
                f"{_n(sells[0].get('price'))} aUEC.") if sells else ""
    s = f"The {item} is cheapest at {_where(buys[0])} for {_n(buys[0].get('price'))} aUEC"
    if len(buys) > 1:
        s += f", then {_where(buys[1])} for {_n(buys[1].get('price'))}"
    return s + "."


def _mission_list(ms: list, k: int = 3) -> str:
    out = []
    for m in ms[:k]:
        title = m.get("title") or m.get("name") or "?"
        fac = m.get("faction") or m.get("contractor")
        out.append(title + (f" for {fac}" if fac else ""))
    return _join(out)


def _missions_for_bp(a, r):
    ms = r.get("missions") or []
    if not ms:
        return ""
    n = r.get("mission_count") or len(ms)
    return f"The {r.get('blueprint')} blueprint comes from {_n(n)} mission(s), like {_mission_list(ms)}."


def _where_to_mine(a, r):
    locs = r.get("locations") or []
    if not locs:
        return ""
    parts = []
    for l in locs[:3]:
        extra = ", ".join(x for x in (l.get("system"), l.get("method")) if x)
        parts.append(f"{l.get('location')}" + (f" ({extra})" if extra else ""))
    return f"{r.get('resource')}: best spots are {_join(parts)}."


def _search_missions(a, r):
    ms = r.get("missions") or []
    if not ms:
        return ""
    top = ms[0]
    pay = f", paying {_n(top.get('reward_uec'))} aUEC" if top.get("reward_uec") else ""
    return (f"{_n(r.get('matching_contracts', len(ms)))} contracts match. The best paying is "
            f"{top.get('title')} for {top.get('faction')}{pay}.")


def _recipe(a, r):
    ing = []
    for slot in r.get("ingredients") or []:
        opts = slot.get("options") or []
        if opts:
            o = opts[0]
            q = o.get("quantity")
            ing.append((f"{_n(q)} {o.get('unit') or 'SCU'} of " if q not in (None, "") else "")
                       + str(o.get("name")))
    s = f"The {r.get('blueprint')} needs {_join(ing[:5]) or 'no listed ingredients'}"
    if r.get("craft_time_seconds"):
        s += f", and takes {_n(r['craft_time_seconds'])} seconds to craft"
    return s + "."


def _signal(a, r):
    ms = r.get("matches") or []
    if not ms:
        return ""
    m = ms[0]
    lead = f"{_n(r.get('signal'))} is" if r.get("exact") else f"{_n(r.get('signal'))} is closest to"
    s = f"{lead} {m.get('resource')}, {_n(m.get('rocks'))} rock(s)"
    if len(ms) > 1:
        s += f"; it could also be {ms[1].get('resource')} with {_n(ms[1].get('rocks'))}"
    return s + "."


def _mining(a, r):
    st = r.get("stats") or {}
    bits = [f"{k.replace('_', ' ')} {_n(v)}" for k, v in list(st.items())[:3]]
    counts: dict = {}
    for m in r.get("modules") or []:
        counts[m] = counts.get(m, 0) + 1
    mods = [(f"{k} x {m}" if k > 1 else m) for m, k in ((m, counts[m]) for m in counts)]
    s = f"A {r.get('ship')} with the {r.get('laser')}" + (f" and {_join(mods)}" if mods else "")
    if bits:
        s += ": " + ", ".join(bits)
    if r.get("price_auec") is not None:
        s += f". Price {_n(r['price_auec'])} aUEC"
    return s + "."


def _cargo(a, r):
    lay = r.get("reference_layout") or r.get("largest_first_layout") or []
    boxes = _join([f"{_n(x.get('count'))} x {_n(x.get('size_scu'))} SCU" for x in lay[:4]])
    cap = r.get("capacity_scu")
    head = f"The {r.get('ship')}" + (f" ({_n(cap)} SCU)" if cap else "")
    return f"{head} fits {boxes}." if boxes else ""


def _jump(a, r):
    route = r.get("in_game_route") or r.get("route")
    if not route:
        return ""
    jumps = r.get("in_game_jumps") if r.get("in_game_route") else r.get("jumps")
    s = f"{r.get('from')} to {r.get('to')}: {_n(jumps)} jump(s)"
    if len(route) > 2:
        s += ", via " + _join(route[1:-1])
    if not r.get("in_game_route"):
        s += ". Not every system on it is in the game yet"
    return s + "."


def _loadout(a, r):
    ws = r.get("weapons") or []
    guns = _join([f"{w.get('name')}" + (f" with {_n(w.get('spare_mags'))} spare mags"
                                          if w.get("spare_mags") not in (None, "") else "")
                  for w in ws[:3]])
    kit = f"{_n(r.get('medpens', 0))} medpens and {_n(r.get('oxypens', 0))} oxypens"
    gr = r.get("grenades")
    if isinstance(gr, dict) and gr:
        kit += ", grenades: " + _join([f"{_n(v)} {k}" for k, v in gr.items()])
    return (f"You have {guns}, " if guns else "You have no guns logged, ") + kit + "."


def _playtime(a, r):
    s = f"You've played {_n(r.get('total_hours'))} hours over {_n(r.get('sessions'))} sessions"
    if r.get("longest_session_hours") is not None:
        s += f"; the longest was {_n(r['longest_session_hours'])} hours"
    return s + "."


def _weapons(a, r):
    summ = r.get("summary") or []
    if not summ:
        return ""
    guns = _join([f"{_n(x.get('count'))} x {x.get('weapon')} (size {_n(x.get('size'))})" for x in summ[:4]])
    out = (f"Best {r.get('metric', 'DPS')} for the {r.get('ship')}: {guns}, "
           f"{_n(r.get('total'))} total. Guns only.")
    rl = r.get("realistic")
    if isinstance(rl, dict):
        out += (f" Scatter guns rated by an estimate of pellets hitting a "
                f"{_n(rl.get('target_size_m'))} m target at {_n(rl.get('range_m'))} m"
                + (", and the total includes that estimate." if r.get("total_is_estimate") else "."))
    return out


def _pin(a, r):
    return (r.get("message") or "Pinned it") + "."


def _starmap(a, r):
    """What the Star Map answered, in its words (it does not speak for itself)."""
    reply = str(r.get("reply") or "").strip()
    if r.get("not_running"):
        return reply
    where = r.get("target") or "the Star Map"
    if r.get("timed_out"):
        return f"I sent that to {where}, but it has not answered yet."
    if not reply:
        return f"Sent to {where}."
    out = _cap(reply)                      # _cap drops a trailing full stop
    return out if out.endswith(("?", "!")) else out + "."


def _route_said(a, r):
    """set_route / plot_route_in_game: the tool's own line, as it worded it."""
    reply = str(r.get("reply") or "").strip()
    if not reply:
        return "Done."
    return reply if reply.endswith(("?", "!", ".")) else reply + "."


def _opened(a, r):
    return f"Opened {r.get('launched') or 'Trade Hub'}."


_FORMATTERS = {
    "ship_info": _ship_info, "ship_buy_rent": _ship_buy_rent, "find_trade_routes": _trade,
    "find_item_price": _item_price, "missions_for_blueprint": _missions_for_bp,
    "where_to_mine": _where_to_mine, "search_missions": _search_missions,
    "blueprint_recipe": _recipe, "identify_signal": _signal,
    "mining_loadout_stats": _mining, "cargo_layout": _cargo, "jump_route": _jump,
    "current_loadout": _loadout, "playtime_summary": _playtime,
    "best_ship_weapons": _weapons, "show_route_popup": _pin,
    "open_trade_hub": _opened, "launch_tool": _opened,
    "starmap_command": _starmap,
    "set_route": _route_said, "plot_route_in_game": _route_said,
}


# ── grounding ─────────────────────────────────────────────────────────────

_NUM = re.compile(r"(?<![A-Za-z])(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|m|million|mil)?\b", re.I)
_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "mil": 1e6}


def numbers_in(text: str) -> list:
    """Every number in *text* as a float ('247,680' -> 247680.0, '1.2
    million' -> 1200000.0). Digits glued to letters ('P4', 'S3') are
    names, not quantities, and are skipped."""
    out = []
    for m in _NUM.finditer(text or ""):
        raw = m.group(1).rstrip(",.")
        if re.search(r"[A-Za-z]$", (text or "")[max(0, m.start() - 1):m.start()]):
            continue
        try:
            v = float(raw.replace(",", ""))
        except ValueError:
            continue
        if m.group(2):
            v *= _MULT[m.group(2).lower()]
        out.append(v)
    for m in _WORD_NUM.finditer(text or ""):         # "three missions"
        out.append(float(_WORDS[m.group(1).lower()]))
    return out


_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve".split())}
_WORD_NUM = re.compile(r"\b(" + "|".join(_WORDS) + r")\b", re.I)
_CAPWORD = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][A-Za-z0-9\-']{3,})")


def dropped_names(reply: str, draft: str, result) -> list:
    """Names from the result that the plain answer said and a rephrasing
    lost, when it lost more than half of them. 'A 4,700 ping is a ping
    value for a specific game' has lost 'Quantainium', i.e. the answer."""
    data = _flat(result).lower()
    names = {w for w in _CAPWORD.findall(draft or "") if w.lower() in data}
    if not names:
        return []
    low = (reply or "").lower()
    lost = sorted(w for w in names if w.lower() not in low)
    return lost if len(lost) * 2 > len(names) else []


def _flat(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str) if not isinstance(obj, str) else obj


def ungrounded_numbers(reply: str, *sources) -> list:
    """Numbers in *reply* that no source contains, allowing rounding
    (1284.5 said as 1,285 or 1.3 thousand) and small counting words."""
    have = []
    for s in sources:
        have.extend(numbers_in(_flat(s)))
    bad = []
    for v in numbers_in(reply):
        if v in (0, 1, 2) and v == int(v):
            continue                        # "one route", "two options"
        if any(abs(v - h) <= max(0.5, 0.01 * abs(h)) or
               (h and abs(v - h) / abs(h) <= 0.05 and _sig(v) <= 2) for h in have):
            continue
        bad.append(v)
    return bad


def dropped_numbers(reply: str, draft: str) -> list:
    """Numbers the plain answer said (3 and up) that a rephrasing lost.
    A rephrasing that drops the price or the count has lost the answer
    ("Yes, you can rent a Prospector anywhere.")."""
    said = numbers_in(reply)
    out = []
    for v in numbers_in(draft):
        if v >= 3 and not any(abs(v - s) <= max(0.5, 0.01 * abs(v)) for s in said):
            out.append(v)
    return out


def _sig(v: float) -> int:
    """Significant digits of a spoken, probably rounded number."""
    s = f"{v:.6g}".replace(".", "").replace("-", "").lstrip("0").rstrip("0")
    return len(s)
