# Moved from skills/Everything_Finder/everything_finder/shopping_list.py to shared/shopping/ and extended on
# 2026-10-04, when the three shopping lists became one.
"""The shared shopping list: items AND commodities, one list, one route.

Pure data layer (no Qt). Two parts:

1. :class:`ShoppingList` - the entries the user wants, persisted to ONE file,
   ``~/.sctoolbox/shopping/shopping_list.json``. Item Finder, the Star Map and
   the Everything Finder are separate processes and all three show this list,
   so the file is the meeting point: every change is written at once, and
   :meth:`ShoppingList.refresh` picks up a change another tool made. Inside
   one process :func:`shared_list` hands every widget the same object.

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
   per entry, because the planner fetches every candidate pair's distance; or
   the one place an entry is PINNED to), the starting terminal, and which
   strategy the user prefers to see first.

Where this came from (2026-10-04). The toolbox had three lists. This module was
the Everything Finder's (everything_finder/shopping_list.py) and is the base of
the one that is left; from the other two it takes

  * the Star Map grocery panel's PIN: an item added from a place on the map is
    to be bought THERE (:attr:`Entry.pin`). A pin narrows that entry's
    candidate terminals; it adds no math;
  * Item Finder's "where to buy and for how much" list per item: every offer,
    cheapest first, is kept in :attr:`PlanInput.offers` for the panel to show.

Both retired lists planned routes with their own code (market_finder and
starmap ``route_planner``). Nothing here calls either.

Labels: an item and a commodity can share a name, and the planner tracks
coverage by name, so every offer is labelled ``"item:<name>"`` or
``"commodity:<name>"``. :func:`entry_label` / :func:`split_label` convert.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

KINDS = ("item", "commodity")

#: Candidate terminals per entry handed to the planner (cheapest first). The
#: planner warms the distance of EVERY candidate pair, so this is a cost knob
#: as much as a quality one: 6 entries x 4 terminals = up to 576 pairs.
PER_ENTRY_DEFAULT = 4
PER_ENTRY_MAX = 10

#: The strategy labels basket_engine.plan_variants produces, in its own order.
STRATEGIES = ("MIN STOPS", "SHORTEST TRIP", "BEST PRICE")


def _root() -> str:
    try:
        from shared.user_settings import ROOT
        return ROOT
    except ImportError:          # running outside the toolbox
        return os.path.join(os.path.expanduser("~"), ".sctoolbox")


def store_path() -> str:
    return os.path.join(_root(), "shopping", "shopping_list.json")


def legacy_stores() -> List[Tuple[str, str]]:
    """The retired lists' files, read ONCE when the shared file does not exist yet.

    ``("entries", path)`` is the Everything Finder's first shared list (same
    format); ``("grocery", path)`` is the Star Map's grocery.json (item dicts
    pinned to the place they were added from). Item Finder's Grocery List was
    never saved to disk, so it has nothing to bring. Neither file is changed
    or removed."""
    root = _root()
    return [("entries", os.path.join(root, "everything_finder", "shopping_list.json")),
            ("grocery", os.path.join(root, "starmap", "grocery.json"))]


@dataclass
class Entry:
    kind: str                     # "item" | "commodity"
    name: str
    qty: int = 1                  # units for an item, SCU for a commodity (informational)
    item_id: Optional[int] = None  # UEX id_item, items only
    #: Buy it HERE: ``{"system", "location", "price"}`` - set when the entry was
    #: added from a place on the Star Map. None = the planner chooses where.
    pin: Optional[Dict[str, Any]] = None

    def label(self) -> str:
        return entry_label(self.kind, self.name)

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"kind": self.kind, "name": self.name, "qty": self.qty,
                             "item_id": self.item_id}
        if self.pin:
            d["pin"] = dict(self.pin)
        return d


def entry_label(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def split_label(label: str) -> Tuple[str, str]:
    kind, _, name = label.partition(":")
    return (kind, name) if kind in KINDS and name else ("", label)


def clean_pin(pin) -> Optional[Dict[str, Any]]:
    """A usable pin, or None. A pin needs a location; system and price are optional."""
    if not isinstance(pin, dict):
        return None
    loc = str(pin.get("location") or "").strip()
    if not loc:
        return None
    out: Dict[str, Any] = {"location": loc, "system": str(pin.get("system") or "").strip()}
    try:
        price = float(pin.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    if price > 0:
        out["price"] = price
    return out


def item_to_entry_args(item: dict) -> Optional[Dict[str, Any]]:
    """An item dict as the tools pass it around (Item Finder's table rows, the Star
    Map's item pop-outs, a drag-and-drop payload) -> ``ShoppingList.add`` kwargs.

    A dict that names a location (the Star Map's do: the item is shown at the
    terminal it was found at) becomes a PINNED entry."""
    if not isinstance(item, dict):
        return None
    name = str(item.get("name") or item.get("name_full") or "").strip()
    if not name:
        return None
    iid = item.get("id", item.get("item_id"))
    try:
        iid = int(iid) if iid is not None else None
    except (TypeError, ValueError):
        iid = None
    return {"kind": "item", "name": name, "item_id": iid,
            "pin": clean_pin({"location": item.get("location"), "system": item.get("system"),
                              "price": item.get("price")})}


class ShoppingList:
    """Ordered, de-duplicated entries of either kind, persisted on every change."""

    def __init__(self, path: Optional[str] = None, autosave: bool = True,
                 legacy: Optional[Sequence[Tuple[str, str]]] = None) -> None:
        self.path = path or store_path()
        # The retired lists are imported only into the real store, never into a
        # path a caller chose (tests, tools): pass legacy=[...] to ask for it.
        self._legacy = list(legacy if legacy is not None else (legacy_stores() if path is None else []))
        self.autosave = autosave
        self._entries: List[Entry] = []
        self._listeners: List[Callable[[], None]] = []
        self._stamp: Optional[Tuple[int, int]] = None

    # observers
    def subscribe(self, fn: Callable[[], None]) -> None:
        self._listeners.append(fn)

    def unsubscribe(self, fn: Callable[[], None]) -> None:
        if fn in self._listeners:
            self._listeners.remove(fn)

    def _notify(self) -> None:
        for fn in list(self._listeners):
            fn()

    def _changed(self) -> None:
        if self.autosave:
            self.save()
        self._notify()

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
    def add(self, kind: str, name: str, qty: int = 1, item_id: Optional[int] = None,
            pin: Optional[dict] = None, bump: bool = True) -> Entry:
        """Add an entry, or update the existing one of the same kind+name.

        An existing entry gets *qty* added (``bump=False``: left as it is - a
        second drag of the same item is not an order for two), learns the item
        id if it lacked one, and takes a new *pin* if one is given."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {KINDS}, not {kind!r}")
        name = (name or "").strip()
        if not name:
            raise ValueError("an entry needs a name")
        qty = max(1, int(qty or 1))
        pin = clean_pin(pin)
        self.refresh(notify=False)       # another tool may have changed the file
        cur = self.find(kind, name)
        if cur is not None:
            if bump:
                cur.qty += qty
            if item_id is not None and cur.item_id is None:
                cur.item_id = int(item_id)
            if pin is not None:
                cur.pin = pin
            self._changed()
            return cur
        e = Entry(kind=kind, name=name, qty=qty,
                  item_id=int(item_id) if item_id is not None else None, pin=pin)
        self._entries.append(e)
        self._changed()
        return e

    def add_item(self, item: dict) -> Optional[Entry]:
        """Add an item dict from Item Finder or the Star Map (see :func:`item_to_entry_args`).
        Adding one that is already listed does not raise its quantity."""
        args = item_to_entry_args(item)
        if args is None:
            return None
        return self.add(bump=False, **args)

    def set_qty(self, kind: str, name: str, qty: int) -> bool:
        self.refresh(notify=False)
        cur = self.find(kind, name)
        if cur is None:
            return False
        cur.qty = max(1, int(qty or 1))
        self._changed()
        return True

    def set_pin(self, kind: str, name: str, pin: Optional[dict]) -> bool:
        """Pin an entry to a place, or (``pin=None``) let the planner choose again."""
        self.refresh(notify=False)
        cur = self.find(kind, name)
        if cur is None:
            return False
        cur.pin = clean_pin(pin)
        self._changed()
        return True

    def remove(self, kind: str, name: str) -> bool:
        self.refresh(notify=False)
        cur = self.find(kind, name)
        if cur is None:
            return False
        self._entries.remove(cur)
        self._changed()
        return True

    def clear(self) -> None:
        self.refresh(notify=False)
        if self._entries:
            self._entries.clear()
            self._changed()

    # persistence
    def to_json(self) -> List[Dict[str, Any]]:
        return [e.to_json() for e in self._entries]

    @staticmethod
    def _parse(raw) -> List[Entry]:
        out: List[Entry] = []
        for d in raw if isinstance(raw, list) else []:
            try:
                if d.get("kind") in KINDS and str(d.get("name") or "").strip():
                    out.append(Entry(kind=d["kind"], name=str(d["name"]).strip(),
                                     qty=max(1, int(d.get("qty") or 1)),
                                     item_id=(int(d["item_id"]) if d.get("item_id") is not None
                                              else None),
                                     pin=clean_pin(d.get("pin"))))
            except (AttributeError, TypeError, ValueError):
                continue
        return out

    def _file_stamp(self) -> Optional[Tuple[int, int]]:
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self) -> "ShoppingList":
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except OSError:
            raw = None
        except ValueError:
            raw = []
        self._stamp = self._file_stamp()
        if raw is None:                       # no shared file yet: bring the old lists in, once
            self._entries = self._import_legacy()
            if self._entries and self.autosave:
                self.save()
            return self
        self._entries = self._parse(raw)
        return self

    def _import_legacy(self) -> List[Entry]:
        merged: List[Entry] = []

        def put(e: Entry) -> None:
            for cur in merged:
                if cur.kind == e.kind and cur.name.casefold() == e.name.casefold():
                    cur.item_id = cur.item_id if cur.item_id is not None else e.item_id
                    cur.pin = cur.pin or e.pin
                    return
            merged.append(e)

        for fmt, path in self._legacy:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    raw = json.load(fh)
            except (OSError, ValueError):
                continue
            if fmt == "entries":
                for e in self._parse(raw):
                    put(e)
            elif fmt == "grocery":
                for it in raw if isinstance(raw, list) else []:
                    args = item_to_entry_args(it)
                    if args is not None:
                        put(Entry(kind="item", name=args["name"], qty=1,
                                  item_id=args["item_id"], pin=args["pin"]))
        return merged

    def refresh(self, notify: bool = True) -> bool:
        """Reload if the file changed under us (another tool wrote it). True if it did."""
        stamp = self._file_stamp()
        if stamp == self._stamp or stamp is None:
            return False
        before = self.to_json()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            return False                      # mid-write or unreadable: keep what we have
        self._stamp = stamp
        self._entries = self._parse(raw)
        changed = self.to_json() != before
        if changed and notify:
            self._notify()
        return changed

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".%d.tmp" % os.getpid()
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.to_json(), fh, indent=2)
            os.replace(tmp, self.path)
            self._stamp = self._file_stamp()
        except OSError:
            pass


_shared: Optional[ShoppingList] = None


def shared_list() -> ShoppingList:
    """The process-wide list every tool in this process shows (loaded on first use)."""
    global _shared
    if _shared is None:
        _shared = ShoppingList().load()
    return _shared


# ── route planning (Trade Hub's math) ────────────────────────────────────────

def _basket_engine():
    """Trade Hub's basket_engine module (imports trade_hub_data on first use)."""
    from .paths import ensure_trade_hub_path
    ensure_trade_hub_path()
    import basket_engine          # noqa: WPS433 - deliberate late import, it is Trade Hub's
    return basket_engine


@dataclass
class PlanInput:
    """What :func:`build_index` produced, for display and for the planner."""
    index: Dict[Any, Dict[str, Any]] = field(default_factory=dict)
    selected: List[str] = field(default_factory=list)     # labels the planner must cover
    no_offers: List[str] = field(default_factory=list)    # labels with no terminal at all
    #: label -> every (TerminalKey, Offer) that sells it, cheapest first - what the
    #: panel lists under an entry ("where to buy and for how much").
    offers: Dict[str, List[Tuple[Any, Any]]] = field(default_factory=dict)
    #: labels whose pin matched no terminal that sells them; planned unpinned.
    pin_missed: List[str] = field(default_factory=list)


_NORM = re.compile(r"[^a-z0-9]+")


def _norm(s) -> str:
    return _NORM.sub("", str(s or "").casefold())


def pin_matches(pin: Dict[str, Any], terminal_key, places: Sequence[str] = ()) -> bool:
    """Is this terminal at the pinned place?

    The pin holds the Star Map's name for the place ("Area 18"); UEX spells the
    same place its own way ("Area18", "TDD - Area18"). Compared with spaces and
    punctuation removed: equal to the terminal's location or one of its place
    names, or contained in the terminal's name."""
    want = _norm(pin.get("location"))
    if not want:
        return False
    psys = _norm(pin.get("system"))
    if psys and _norm(terminal_key.system) and psys != _norm(terminal_key.system):
        return False
    names = [_norm(terminal_key.location)] + [_norm(p) for p in places]
    if want in [n for n in names if n]:
        return True
    return len(want) >= 4 and want in _norm(terminal_key.terminal_name)


def build_index(entries: Sequence[Entry], routes: Iterable[Any],
                item_prices: Dict[str, List[dict]],
                per_entry_limit: int = PER_ENTRY_DEFAULT,
                engine=None) -> PlanInput:
    """One SellersIndex for items AND commodities.

    *routes*: Trade Hub ``Route`` objects (commodities). *item_prices*: item name ->
    raw UEX ``items_prices`` rows (as Item Finder's DataService returns them).
    Each entry keeps its cheapest ``per_entry_limit`` terminals - or, if it is
    pinned, only the terminals at the pinned place.
    """
    be = engine or _basket_engine()
    limit = max(1, min(PER_ENTRY_MAX, int(per_entry_limit or PER_ENTRY_DEFAULT)))
    canon: Dict[int, Any] = {}           # terminal id -> the one TerminalKey used for it
    places_of: Dict[int, List[str]] = {}

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
        from .paths import ensure_item_finder_path
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
                places_of.setdefault(tid, [])
                places_of[tid] += [p for p in places if p not in places_of[tid]]
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
        ranked = sorted(best.values(), key=lambda p: p[1].price_buy)
        out.offers[lbl] = ranked
        chosen = ranked[:limit]
        if e.pin:
            at_pin = [p for p in ranked
                      if pin_matches(e.pin, p[0], places_of.get(p[0].terminal_id, ()))]
            if at_pin:
                chosen = at_pin[:limit]
            else:
                out.pin_missed.append(lbl)
        for tk, off in chosen:
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


def plan_cost(plan) -> float:
    """Sum of the unit buy prices a plan picked (a display total, not a ranking)."""
    return float(sum(o.price_buy for s in plan.stops for o in s.picks))


def plan_summary(plan) -> List[str]:
    """Human lines for one BasketPlan (used by the panel and by tests)."""
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
    """A BasketPlan as the star maps' ``plot_shopping_route`` stop dicts, in order."""
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
