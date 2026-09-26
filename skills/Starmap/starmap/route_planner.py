"""Pure helpers for the grocery list's "Plot Route" feature.

(Copy of ``market_finder.route_planner`` for the standalone Starmap tool --
keep the two in sync.  The Starmap grocery list pins each item to the
location it was added from, so it only uses :func:`order_stops` and
:func:`visits`.)

No Qt imports here.  This module turns the grocery cards' buy rows into an
ordered list of shopping stops that

* covers every item that has a buy price (an item is never dropped),
* chooses WHICH terminal to buy each item at so the trip is as short as
  possible, among the terminals whose price is within
  :data:`MAX_PRICE_PREMIUM` of that item's cheapest price, and
* orders the stops to minimise total travel.

Terminals at the same station / city / outpost form one *site*: moving
between them costs no travel, so the route is planned over sites.

Cost model: ``dist_fn(site_a, site_b)`` returns the travel cost between two
sites in gigametres (the real one is
:func:`.distances.site_distance`: UEX telemetry, else
body coordinates, plus a flat penalty per system-gateway jump).  ``None``
means unknown; such pairs cost :data:`UNKNOWN_DISTANCE`, so they are avoided
but never make an item vanish.

The route is an open path (you do not have to come back) with a free start
unless *start* is given.

Solver: EXACT for realistic lists -- a shortest-path search over
(items-covered, current-site) states, which jointly picks the terminals and
their order and is provably optimal.  If it has not finished after
:data:`EXACT_MAX_EXPANSIONS` states (very long, very spread-out lists) it
gives up and a heuristic answers instead: multi-start greedy covering, then
drop / swap local search with an exact or 2-opt ordering.  The heuristic is
NOT guaranteed optimal.
"""

from __future__ import annotations

import heapq
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# -- tunables (product decisions, one constant each) --------------------------

#: How much more than an item's cheapest price the planner may pay to save
#: travel.  0.0 = never pay more than the cheapest price: the route only
#: chooses among equally-cheapest terminals (UEX prices tie very often), so
#: the bill is exactly what each card's cheapest row shows.  Raise it (e.g.
#: 0.10 = up to 10 % more per item) to trade aUEC for fewer stops.
MAX_PRICE_PREMIUM = 0.0

#: Cost of a site pair whose distance is unknown (Gm).  Large enough that any
#: known leg wins, finite so a route always exists.
UNKNOWN_DISTANCE = 1.0e6

#: States the exact search may expand before handing over to the heuristic.
#: Keeps the worst case around half a second in CPython; lists of up to ~15
#: items normally finish exactly in well under 100 ms.
EXACT_MAX_EXPANSIONS = 40_000

#: Greedy starting sites tried by the heuristic fallback.
HEURISTIC_STARTS = 8

#: Largest number of sites ordered exactly (Held-Karp); above this, 2-opt.
EXACT_ORDER_MAX = 10


# -- cards -> wants -------------------------------------------------------------

def collect_wants(cards: List[Any]) -> List[dict]:
    """One want per loaded grocery card that has buy rows.

    ``{"item_id", "name", "rows"}`` where *rows* are the card's normalized
    buy rows (:func:`market_finder.grocery.buy_locations`).  Cards whose
    prices have not loaded (or that nobody sells) are skipped -- see
    :func:`missing_price_cards`.
    """
    wants: List[dict] = []
    for card in cards:
        rows = getattr(card, "_buy_rows", None) or []
        if not rows or not getattr(card, "_loaded", False):
            continue
        wants.append({
            "item_id": card.item_id(),
            "name": (card._item.get("name") or "Unknown"),
            "rows": list(rows),
        })
    return wants


def collect_stops(cards: List[Any]) -> List[dict]:
    """Legacy: one stop per card at its cheapest buy row (no route choice)."""
    return [_stop_from_row(w, w["rows"][0]) for w in collect_wants(cards)]


def missing_price_cards(cards: List[Any]) -> List[Any]:
    """Cards that could not contribute a stop (prices still loading / none)."""
    return [c for c in cards
            if not getattr(c, "_loaded", False) or not (getattr(c, "_buy_rows", None) or [])]


def unique_terminals(stops: List[dict]) -> List[Tuple[int, str]]:
    """Deduplicated (terminal_id, system) pairs, order-preserving."""
    seen: Dict[int, str] = {}
    for s in stops:
        tid = int(s.get("terminal_id") or 0)
        if tid and tid not in seen:
            seen[tid] = s.get("system") or ""
    return list(seen.items())


# -- sites ------------------------------------------------------------------------

def row_places(row: dict) -> List[str]:
    """Place names of a buy row, finest first.

    Uses ``row["places"]`` when present, else derives them from the
    ``"System > Planet > City"`` display string.
    """
    places = [p for p in (row.get("places") or []) if p]
    if places:
        return list(places)
    parts = [p.strip() for p in str(row.get("location") or "").split(">") if p.strip()]
    system = (row.get("system") or "").strip().lower()
    if parts and parts[0].lower() == system:
        parts = parts[1:]
    return list(reversed(parts))


def site_key(row: dict) -> Tuple[str, str]:
    """(system, finest place), lower-cased -- equal keys = the same stop."""
    places = row_places(row)
    if places:
        finest = places[0]
    else:
        finest = "#%s" % (row.get("terminal_id") or row.get("terminal") or "?")
    return ((row.get("system") or "").strip().lower(), finest.strip().lower())


def candidate_sites(wants: List[dict],
                    max_price_premium: float = MAX_PRICE_PREMIUM
                    ) -> Tuple[List[dict], List[List[int]]]:
    """Sites worth considering, and for each want the indices of its eligible sites.

    A site is eligible for a want when it sells that item for at most
    ``cheapest * (1 + max_price_premium)``.
    """
    sites: List[dict] = []
    index: Dict[Tuple[str, str], int] = {}
    eligible: List[List[int]] = []
    for w in wants:
        priced = [r for r in w["rows"] if _price(r) > 0]
        if not priced:
            eligible.append([])
            continue
        cap = min(_price(r) for r in priced) * (1.0 + max(0.0, max_price_premium)) + 1e-6
        mine: List[int] = []
        for r in priced:
            if _price(r) > cap:
                continue
            k = site_key(r)
            i = index.get(k)
            if i is None:
                i = index[k] = len(sites)
                sites.append({"key": k, "system": r.get("system") or "",
                              "places": row_places(r), "location": r.get("location") or "",
                              "terminal_ids": []})
            tid = int(r.get("terminal_id") or 0)
            if tid and tid not in sites[i]["terminal_ids"]:
                sites[i]["terminal_ids"].append(tid)
            if i not in mine:
                mine.append(i)
        eligible.append(mine)
    return sites, eligible


# -- planning -----------------------------------------------------------------------

def plan_route(wants: List[dict],
               dist_fn: Callable[[dict, dict], Optional[float]],
               start: Optional[dict] = None,
               max_price_premium: float = MAX_PRICE_PREMIUM) -> List[dict]:
    """Choose a terminal for every want and order the visits.

    Returns one stop dict per want (``item_id, name, terminal, terminal_id,
    system, location, places, price, site, stop``) in visit order; ``stop``
    is the 1-based site-visit number, shared by items bought at one site.
    Wants without any buy price are omitted (there is nothing to buy).
    """
    sites, eligible = candidate_sites(wants, max_price_premium)
    live = [i for i, e in enumerate(eligible) if e]
    if not live:
        return []
    n = len(sites)
    D = [[0.0 if a == b else _dist(dist_fn, sites[a], sites[b]) for b in range(n)]
         for a in range(n)]
    S0 = [(_dist(dist_fn, start, sites[j]) if start is not None else 0.0) for j in range(n)]

    # Wants with identical eligible-site sets are one requirement.
    groups: Dict[frozenset, int] = {}
    for i in live:
        groups.setdefault(frozenset(eligible[i]), len(groups))
    cover = [0] * n
    for fs, g in groups.items():
        for j in fs:
            cover[j] |= 1 << g
    G = len(groups)

    seq = _exact_cover_path(cover, G, D, S0, EXACT_MAX_EXPANSIONS)
    if seq is None:
        seq = _heuristic_cover_path(cover, G, D, S0)

    # Buy each want at the cheapest eligible site on the route (earliest on ties).
    pos = {j: k for k, j in enumerate(seq)}
    chosen: Dict[int, Tuple[int, dict]] = {}
    for i in live:
        best = None
        for j in eligible[i]:
            if j not in pos:
                continue
            row = _best_row_at(wants[i], sites[j]["key"])
            cand = (_price(row), pos[j], j, row)
            if best is None or cand[:2] < best[:2]:
                best = cand
        chosen[i] = (best[2], best[3])
    used = {j for j, _r in chosen.values()}
    # Dropping a site nobody buys at never lengthens the path (triangle inequality).
    seq = [j for j in seq if j in used]

    out: List[dict] = []
    for k, j in enumerate(seq, 1):
        here = sorted((i for i in live if chosen[i][0] == j),
                      key=lambda i: str(wants[i].get("name") or ""))
        for i in here:
            stop = _stop_from_row(wants[i], chosen[i][1])
            stop["site"] = sites[j]["key"]
            stop["stop"] = k
            out.append(stop)
    return out


def visits(stops: List[dict]) -> List[dict]:
    """Collapse an ordered stop list into site visits (consecutive same-site
    stops are one visit): ``{"key", "system", "places", "location",
    "terminal_ids", "items"}``."""
    out: List[dict] = []
    for s in stops:
        k = tuple(s.get("site") or site_key(s))
        tid = int(s.get("terminal_id") or 0)
        if not out or out[-1]["key"] != k:
            out.append({"key": k, "system": s.get("system") or "",
                        "places": row_places(s), "location": s.get("location") or "",
                        "terminal_ids": [], "items": []})
        if tid and tid not in out[-1]["terminal_ids"]:
            out[-1]["terminal_ids"].append(tid)
        out[-1]["items"].append(s.get("name") or "")
    return out


def route_cost(stops: List[dict],
               dist_fn: Callable[[dict, dict], Optional[float]],
               start: Optional[dict] = None) -> float:
    """Total travel of an ordered stop list (Gm)."""
    vs = visits(stops)
    total = _dist(dist_fn, start, vs[0]) if (start is not None and vs) else 0.0
    for a, b in zip(vs, vs[1:]):
        total += _dist(dist_fn, a, b)
    return total


def order_stops(stops: List[dict],
                dist_fn: Callable[[dict, dict], Optional[float]],
                start: Optional[dict] = None) -> List[dict]:
    """Order FIXED shopping stops to minimise travel (terminals are not re-chosen).

    Stops at the same site are kept together.  Exact (Held-Karp) up to
    :data:`EXACT_ORDER_MAX` distinct sites, else nearest-neighbour from every
    start plus 2-opt.  *dist_fn(a, b)* takes two stop dicts; ``None`` means
    unknown.  Returns a new list.
    """
    if len(stops) < 2:
        return list(stops)
    reps: List[dict] = []
    members: Dict[Tuple[str, str], List[dict]] = {}
    for s in stops:
        k = tuple(s.get("site") or site_key(s))
        if k not in members:
            members[k] = []
            reps.append(s)
        members[k].append(s)
    n = len(reps)
    D = [[0.0 if a == b else _dist(dist_fn, reps[a], reps[b]) for b in range(n)]
         for a in range(n)]
    S0 = [(_dist(dist_fn, start, reps[j]) if start is not None else 0.0) for j in range(n)]
    out: List[dict] = []
    for j in _order(list(range(n)), D, S0):
        out.extend(members[tuple(reps[j].get("site") or site_key(reps[j]))])
    return out


# -- solvers -------------------------------------------------------------------------

def _exact_cover_path(cover: List[int], G: int, D: List[List[float]],
                      S0: List[float], max_expansions: Optional[int] = None
                      ) -> Optional[List[int]]:
    """Shortest open path whose visited sites cover every group, or None if
    more than *max_expansions* states would be needed.

    Dijkstra over (covered-mask, current-site) states; a site is only
    visited when it adds coverage, so no stop is ever needless.  With
    non-negative costs the first full-mask state popped is optimal.
    """
    full = (1 << G) - 1
    n = len(cover)
    best: Dict[Tuple[int, int], float] = {}
    prev: Dict[Tuple[int, int], Optional[Tuple[int, int]]] = {}
    pq: List[Tuple[float, int, int]] = []
    for j in range(n):
        if cover[j]:
            st = (cover[j], j)
            if S0[j] < best.get(st, float("inf")):
                best[st] = S0[j]
                prev[st] = None
                heapq.heappush(pq, (S0[j], cover[j], j))
    goal = None
    expanded = 0
    while pq:
        d, mask, j = heapq.heappop(pq)
        if d > best.get((mask, j), float("inf")):
            continue
        expanded += 1
        if max_expansions is not None and expanded > max_expansions:
            return None
        if mask == full:
            goal = (mask, j)
            break
        row = D[j]
        for k in range(n):
            add = cover[k] & ~mask
            if not add:
                continue
            nm = mask | add
            nd = d + row[k]
            if nd < best.get((nm, k), float("inf")):
                best[(nm, k)] = nd
                prev[(nm, k)] = (mask, j)
                heapq.heappush(pq, (nd, nm, k))
    seq: List[int] = []
    st = goal
    while st is not None:
        seq.append(st[1])
        st = prev[st]
    seq.reverse()
    return seq


def _heuristic_cover_path(cover: List[int], G: int, D: List[List[float]],
                          S0: List[float]) -> List[int]:
    """Multi-start greedy set cover, then drop / swap local search on the
    ordered cost of the best start."""
    full = (1 << G) - 1
    n = len(cover)

    def greedy(first: int) -> List[int]:
        chosen = [first]
        mask = cover[first]
        while mask != full:
            def score(k: int) -> Tuple[float, int]:
                gain = bin(cover[k] & ~mask).count("1")
                near = min(D[c][k] for c in chosen)
                return (near / gain, -gain)          # cheapest travel per new item
            k = min((k for k in range(n) if cover[k] & ~mask), key=score)
            chosen.append(k)
            mask |= cover[k]
        return chosen

    # Start at the sites that sell the hardest-to-find requirement, plus the
    # sites that cover the most.
    rarest = min(range(G), key=lambda g: sum(1 for c in cover if (c >> g) & 1))
    starts = [k for k in range(n) if (cover[k] >> rarest) & 1]
    starts += sorted(range(n), key=lambda k: -bin(cover[k]).count("1"))
    seen_starts: List[int] = []
    for k in starts:
        if k not in seen_starts and cover[k]:
            seen_starts.append(k)
        if len(seen_starts) >= HEURISTIC_STARTS:
            break

    def covers(ss: Sequence[int]) -> bool:
        m = 0
        for s in ss:
            m |= cover[s]
        return m == full

    def cost(ss: List[int]) -> Tuple[float, List[int]]:
        seq = _order(ss, D, S0)
        return _path_len(seq, D, S0), seq

    cur_cost, cur_seq = min((cost(greedy(k)) for k in seen_starts), key=lambda t: t[0])
    evals = 0
    improved = True
    while improved and evals < 2000:
        improved = False
        for s in list(cur_seq):                      # drop a site
            trial = [x for x in cur_seq if x != s]
            if trial and covers(trial):
                c, q = cost(trial)
                evals += 1
                if c < cur_cost - 1e-9:
                    cur_cost, cur_seq, improved = c, q, True
                    break
        if improved:
            continue
        for s in list(cur_seq):                      # swap a site for another
            for k in range(n):
                if k in cur_seq or not cover[k]:
                    continue
                trial = [x for x in cur_seq if x != s] + [k]
                if not covers(trial):
                    continue
                c, q = cost(trial)
                evals += 1
                if c < cur_cost - 1e-9:
                    cur_cost, cur_seq, improved = c, q, True
                    break
            if improved:
                break
    return cur_seq


def _order(nodes: List[int], D: List[List[float]], S0: List[float]) -> List[int]:
    """Best open-path order of *nodes*: exact when small, else NN + 2-opt."""
    if len(nodes) <= 1:
        return list(nodes)
    if len(nodes) <= EXACT_ORDER_MAX:
        return _held_karp(nodes, D, S0)
    best = None
    for first in nodes:
        seq = [first]
        rest = [x for x in nodes if x != first]
        while rest:
            nxt = min(rest, key=lambda x: D[seq[-1]][x])
            seq.append(nxt)
            rest.remove(nxt)
        seq = _two_opt(seq, D, S0)
        c = _path_len(seq, D, S0)
        if best is None or c < best[0]:
            best = (c, seq)
    return best[1]


def _held_karp(nodes: List[int], D: List[List[float]], S0: List[float]) -> List[int]:
    m = len(nodes)
    INF = float("inf")
    dp = [[INF] * m for _ in range(1 << m)]
    par = [[-1] * m for _ in range(1 << m)]
    for i in range(m):
        dp[1 << i][i] = S0[nodes[i]]
    for mask in range(1, 1 << m):
        row = dp[mask]
        for i in range(m):
            c = row[i]
            if c == INF or not (mask >> i) & 1:
                continue
            di = D[nodes[i]]
            for k in range(m):
                if (mask >> k) & 1:
                    continue
                nm = mask | (1 << k)
                nc = c + di[nodes[k]]
                if nc < dp[nm][k]:
                    dp[nm][k] = nc
                    par[nm][k] = i
    full = (1 << m) - 1
    i = min(range(m), key=lambda x: dp[full][x])
    seq: List[int] = []
    mask = full
    while i != -1:
        seq.append(nodes[i])
        pi = par[mask][i]
        mask &= ~(1 << i)
        i = pi
    seq.reverse()
    return seq


def _two_opt(seq: List[int], D: List[List[float]], S0: List[float]) -> List[int]:
    best = list(seq)
    best_c = _path_len(best, D, S0)
    improved = True
    while improved:
        improved = False
        for i in range(0, len(best) - 1):
            for k in range(i + 1, len(best)):
                trial = best[:i] + best[i:k + 1][::-1] + best[k + 1:]
                c = _path_len(trial, D, S0)
                if c < best_c - 1e-9:
                    best, best_c, improved = trial, c, True
    return best


def _path_len(seq: Sequence[int], D: List[List[float]], S0: List[float]) -> float:
    if not seq:
        return 0.0
    return S0[seq[0]] + sum(D[a][b] for a, b in zip(seq, seq[1:]))


# -- small helpers -------------------------------------------------------------------

def _price(row: dict) -> float:
    try:
        return float(row.get("price") or 0)
    except (TypeError, ValueError):
        return 0.0


def _best_row_at(want: dict, key: Tuple[str, str]) -> dict:
    rows = [r for r in want["rows"] if site_key(r) == key and _price(r) > 0]
    return min(rows, key=_price)


def _stop_from_row(want: dict, row: dict) -> dict:
    return {
        "item_id": want.get("item_id"),
        "name": want.get("name") or "Unknown",
        "terminal": row.get("terminal") or "",
        "terminal_id": row.get("terminal_id") or 0,
        "system": row.get("system") or "",
        "location": row.get("location") or "",
        "places": row_places(row),
        "price": row.get("price") or 0,
    }


def _dist(dist_fn: Callable[[dict, dict], Optional[float]], a: dict, b: dict) -> float:
    try:
        d = dist_fn(a, b)
    except Exception:
        d = None
    if d is None or d != d or d < 0:
        return UNKNOWN_DISTANCE
    return float(d)
