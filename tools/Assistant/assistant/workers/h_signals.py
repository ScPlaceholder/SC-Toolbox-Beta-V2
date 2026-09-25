"""Mining Signals worker: what does a scanner signal number mean?

SheetFetcher pulls the community signal sheet (Google Sheets CSV); its
disk cache is read when fresh and never written from here.
"""
from __future__ import annotations

import time

import _assist_logic as L

from services.sheet_fetcher import SheetFetcher
from services.signal_matcher import SignalMatcher

_f = SheetFetcher()
_f._cache.save = lambda *a, **k: None
_state = {"m": None, "ts": 0.0, "rows": 0}
_TTL = 3600.0


def _matcher() -> SignalMatcher:
    if _state["m"] is None or time.time() - _state["ts"] > _TTL:
        r = _f.load()
        if not r.ok or not r.data:
            raise L.ToolFail("signal sheet could not be loaded: "
                             + str(getattr(r, "error", "") or "no rows"))
        _state.update(m=SignalMatcher(r.data), ts=time.time(), rows=len(r.data))
    return _state["m"]


def identify_signal(value) -> dict:
    try:
        v = int(round(float(str(value).replace(",", "").strip())))
    except ValueError:
        raise L.ToolFail(f"'{value}' is not a signal number; read me the digits on the scanner")
    m = _matcher()
    exact = m.find_all_exact(v)
    near = exact or m.match_all(v, tolerance=200)
    out = [{"resource": s.name, "rarity": s.rarity, "rocks": s.rock_count,
            "expected_signal": s.expected_value, "off_by": s.delta} for s in near[:6]]
    res = {"signal": v, "exact": bool(exact), "matches": out, "sheet_rows": _state["rows"]}
    if not out:
        res["empty"] = True
        res["note"] = "no resource cluster within 200 of that signal; re-read the number"
    return res


EXPORTS = {"identify_signal": identify_signal}
