# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""The shared shopping list: items AND commodities, one route.

Pure data layer (no Qt). Two parts:

1. :class:`ShoppingList` - the entries the user wants, persisted to
   ``~/.sctoolbox/everything_finder/shopping_list.json``.

2. :func:`plan_routes` - turns those entries into pickup routes using
   **Trade Hub's route math**, not a copy of it. J's standing directive
   (2026-09-27) is that other tools use the Trade Hub's math because a
   duplicate drifts. Concretely, this module never ranks, orders or scores a
   stop itself:

   * commodity offers come from ``basket_engine.build_sellers_index`` over Trade
     Hub ``Route`` objects (the same call Trade Hub's BASKET view makes);
   * item offers are put into the SAME ``SellersIndex`` shape - ``TerminalKey``
     -> ``{label: Offer}`` - using Item Finder's own price normaliser
     (``market_finder.grocery.buy_locations``), so items and commodities sit in
     one index keyed by UEX terminal id;
   * the route itself is ``basket_engine.plan_variants`` (greedy set cover:
     MIN STOPS / SHORTEST TRIP / BEST PRICE, plus forced-first variants), with
     distances from Trade Hub's ``DistanceCache`` warmed by
     ``basket_engine.distance_pairs_needed`` - exactly the BASKET view's flow.

   The only things decided here are INPUTS to that math, and each is a user
   parameter: which terminals are candidates (the cheapest ``per_entry_limit``
   per entry, because the planner fetches every candidate pair's distance), the
   starting terminal, and which strategy the user prefers to see first.

Labels: an item and a commodity can share a name, and the planner tracks
coverage by name, so every offer is labelled ``"item:<name>"`` or
``"commodity:<name>"``. :func:`entry_label` / :func:`split_label` convert.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

KINDS = ("item", "commodity")

#: Candidate terminals per entry handed to the planner (cheapest first). The
#: planner warms the distance of EVERY candidate pair, so this is a cost knob
#: as much as a quality one: 6 entries x 4 terminals = up to 576 pairs.
PER_ENTRY_DEFAULT = 4
PER_ENTRY_MAX = 10

#: The strategy labels basket_engine.plan_variants produces, in its own order.
STRATEGIES = ("MIN STOPS", "SHORTEST TRIP", "BEST PRICE")


def _store_path() -> str:
    try:
        from shared.user_settings import settings_path
        return settings_path("everything_finder", "shopping_list.json")
    except ImportError:          # running outside the toolbox (tests set their own path)
        return os.path.join(os.path.expanduser("~"), ".sctoolbox", "everything_finder",
                            "shopping_list.json")


@dataclass
class Entry:
    kind: str                     # "item" | "commodity"
    name: str
    qty: int = 1                  # units for an item, SCU for a commodity (informational)
    item_id: Optional[int] = None  # UEX id_item, items only

    def label(self) -> str:
        return entry_label(self.kind, self.name)


def entry_label(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def split_label(label: str) -> Tuple[str, str]:
    kind, _, name = label.partition(":")
    return (kind, name) if kind in KINDS and name else ("", label)


class ShoppingList:
    """Ordered, de-duplicated entries of either kind, persisted on every change."""

    def __init__(self, path: Optional[str] = None, autosave: bool = True) -> None:
        self.path = path or _store_path()
        self.autosave = autosave
        self._entries: List[Entry] = []
        self._listeners: List[Callable[[], None]] = []

    # observers
    def subscribe(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        if self.autosave:
            self.save()
        for fn in list(self._listeners):
            fn()

    # queries
    def entries(self) -> List[Entry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def of_kind(self, kind: str) -> List[Entry]:
        return [e for e in self._entries if e.kind == kind]

    def find(self, kind: str, name: str) -> Optional[Entry]:
        key = name.strip().casefold()
        for e in self._entries:
            if e.kind == kind and e.name.casefold() == key:
                return e
        return None

    # mutations
    def add(self, kind: str, name: str, qty: int = 1, item_id: Optional[int] = None) -> Entry:
        """Add an entry, or bump the quantity of an existing one of the same kind+name."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
        name = (name or "").strip()
        if not name:
            raise ValueError("an entry needs a name")
        qty = max(1, int(qty or 1))
        cur = self.find(kind, name)
        if cur is not None:
            cur.qty += qty
            if item_id is not None and cur.item_id is None:
                cur.item_id = int(item_id)
            self._changed()
            return cur
        e = Entry(kind=kind, name=name, qty=qty,
                  item_id=int(item_id) if item_id is not None else None)
        self._entries.append(e)
        self._changed()
        return e

    def remove(self, kind: str, name: str) -> bool:
        cur = self.find(kind, name)
        if cur is None:
            return False
        self._entries.remove(cur)
        self._changed()
        return True

    def clear(self) -> None:
        if self._entries:
            self._entries.clear()
            self._changed()

    # persistence
    def to_json(self) -> List[Dict[str, Any]]:
        return [asdict(e) for e in self._entries]

    def load(self) -> "ShoppingList":
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            raw = []
        out: List[Entry] = []
        for d in raw if isinstance(raw, list) else []:
            try:
                if d.get("kind") in KINDS and str(d.get("name") or "").strip():
                    out.append(Entry(kind=d["kind"], name=str(d["name"]).strip(),
                                     qty=max(1, int(d.get("qty") or 1)),
                                     item_id=(int(d["item_id"]) if d.get("item_id") is not None
                                              else None)))
            except (AttributeError, TypeError, ValueError):
                continue
        self._entries = out
        return self

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_json(), fh, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass


# ── route planning (Trade Hub's math) ────────────────────────────────────────

def _basket_engine():
    """Trade Hub's basket_engine module (imports trade_hub_data on first use)."""
    from .tool_loader import ensure_trade_hub_path
    ensure_trade_hub_path()
    import basket_engine          # noqa: WPS433 - deliberate late import, it is Trade Hub's
    return basket_engine


@dataclass
class PlanInput:
    """What :func:`build_index` produced, for display and for the planner."""
    index: Dict[Any, Dict[str, Any]] = field(default_factory=dict)
    selected: List[str] = field(default_factory=list)     # labels the planner must cover
    no_offers: List[str] = field(default_factory=list)    # labels with no terminal at all


def build_index(entries: Sequence[Entry], routes: Iterable[Any],
                item_prices: Dict[str, List[dict]],
                per_entry_limit: int = PER_ENTRY_DEFAULT,
                engine=None) -> PlanInput:
    """One SellersIndex for items AND commodities.

    *routes*: Trade Hub ``Route`` objects (commodities). *item_prices*: item name ->
    raw UEX ``items_prices`` rows (as Item Finder's DataService returns them).
    Each entry keeps its cheapest ``per_entry_limit`` terminals.
    """
    be = engine or _basket_engine()
    limit = max(1, min(PER_ENTRY_MAX, int(per_entry_limit or PER_ENTRY_DEFAULT)))
    canon: Dict[int, Any] = {}           # terminal id -> the one TerminalKey used for it

    def key_for(tk):
        return canon.setdefault(tk.terminal_id, tk)

    # label -> [(TerminalKey, Offer)]
    offers: Dict[str, List[Tuple[Any, Any]]] = {}

    commodities = [e for e in entries if e.kind == "commodity"]
    if commodities:
        names = {e.name for e in commodities}
        by_name = {e.name.casefold(): e for e in commodities}
        sellers = be.build_sellers_index(list(routes or []), names)
        for tk, per in sellers.items():
            for cname, off in per.items():
                e = by_name.get(cname.casefold())
                if e is None:
                    continue
                lbl = e.label()
                offers.setdefault(lbl, []).append(
                    (key_for(tk), be.Offer(lbl, off.scu_available, off.price_buy)))

    items = [e for e in entries if e.kind == "item"]
    if items:
        from .tool_loader import ensure_item_finder_path
        ensure_item_finder_path()
        from market_finder.grocery import buy_locations
        for e in items:
            for row in buy_locations(item_prices.get(e.name) or []):
                tid = int(row.get("terminal_id") or 0)
                if not tid:
                    continue
                places = row.get("places") or []
                tk = be.TerminalKey(terminal_id=tid,
                                    terminal_name=str(row.get("terminal") or ""),
                                    location=str(places[0] if places else row.get("location") or ""),
                                    system=str(row.get("system") or ""))
                offers.setdefault(e.label(), []).append(
                    (key_for(tk), be.Offer(e.label(), 0, float(row["price"]))))

    out = PlanInput()
    for e in entries:
        lbl = e.label()
        cands = offers.get(lbl) or []
        if not cands:
            out.no_offers.append(lbl)
            continue
        out.selected.append(lbl)
        # cheapest first; one offer per terminal (keep its cheapest)
        best: Dict[int, Tuple[Any, Any]] = {}
        for tk, off in cands:
            cur = best.get(tk.terminal_id)
            if cur is None or off.price_buy < cur[1].price_buy:
                best[tk.terminal_id] = (tk, off)
        for tk, off in sorted(best.values(), key=lambda p: p[1].price_buy)[:limit]:
            out.index.setdefault(tk, {})[lbl] = off
    return out


def plan_routes(entries: Sequence[Entry], routes: Iterable[Any],
                item_prices: Dict[str, List[dict]], dist_cache,
                start_terminal_id: Optional[int] = None,
                per_entry_limit: int = PER_ENTRY_DEFAULT,
                prefer: str = "MIN STOPS",
                max_variants: int = 5,
                on_progress: Optional[Callable[[int, int], None]] = None,
                engine=None) -> Tuple[List[Any], PlanInput]:
    """Plan pickup routes for *entries* with Trade Hub's basket planner.

    Returns ``(plans, plan_input)``; ``plans`` are ``basket_engine.BasketPlan``
    with the *prefer* strategy first when it is among them. Entries no terminal
    sells are in ``plan_input.no_offers`` (and so in no plan's stops)."""
    be = engine or _basket_engine()
    pi = build_index(entries, routes, item_prices, per_entry_limit, engine=be)
    if not pi.index:
        return [], pi
    selected = set(pi.selected)
    pairs = be.distance_pairs_needed(pi.index, start_terminal_id)
    if pairs:
        try:
            dist_cache.fetch_missing(pairs, on_progress=on_progress)
        except TypeError:        # a cache without the progress hook
            dist_cache.fetch_missing(pairs)
    plans = be.plan_variants(pi.index, selected, start_terminal_id, dist_cache,
                             max_variants=max_variants)
    return order_by_preference(plans, prefer), pi


def order_by_preference(plans: List[Any], prefer: str) -> List[Any]:
    """Stable re-order: the preferred strategy's plan first, the rest as planned."""
    pref = [p for p in plans if getattr(p, "label", "") == prefer]
    return pref + [p for p in plans if getattr(p, "label", "") != prefer]


def plan_summary(plan) -> List[str]:
    """Human lines for one BasketPlan (used by the pop-out and by tests)."""
    lines = []
    for i, stop in enumerate(plan.stops, 1):
        t = stop.terminal
        loc = t.location if t.location and t.location.casefold() not in t.terminal_name.casefold() else ""
        where = ", ".join(x for x in (loc, t.system) if x)
        picks = "; ".join(f"{split_label(o.commodity)[1]} ({split_label(o.commodity)[0]}) "
                          f"@ {o.price_buy:,.0f}" for o in stop.picks)
        lines.append(f"{i}. {t.terminal_name}" + (f" - {where}" if where else "") + f": {picks}")
    return lines


def plan_stops_for_map(plan) -> List[dict]:
    """A BasketPlan as the Star Map's ``plot_shopping_route`` stop dicts, in order."""
    out = []
    for stop in plan.stops:
        t = stop.terminal
        for o in stop.picks:
            out.append({"item_id": None, "name": split_label(o.commodity)[1],
                        "terminal": t.terminal_name, "terminal_id": t.terminal_id,
                        "system": t.system, "location": t.location,
                        "places": [t.location] if t.location else [],
                        "price": o.price_buy})
    return out
