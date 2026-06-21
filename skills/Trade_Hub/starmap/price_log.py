"""Local price-history logger.

UEX's price-history endpoint is sparse (recently reset), so the commodity trend
charts have almost nothing to draw. This records the toolbox's OWN snapshot of
every terminal's buy/sell price into a small local SQLite DB, so trends build up
from our own observations regardless of UEX's history feature.

One row per (commodity, terminal, day), upserted — so a launch snapshot and the
daily snapshot collapse to a single deduped point per day (matching how the
charts bin history). At ~2,600 pairs that's at most ~2,600 rows/day (~1 MB/day),
and far fewer in practice since unchanged prices just overwrite the day's row.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import List

from . import uex

log = logging.getLogger("TradeHub.price_log")

_DB_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "trade_hub")
_DB_PATH = os.path.join(_DB_DIR, "price_history.db")
_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    os.makedirs(_DB_DIR, exist_ok=True)
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute(
        "CREATE TABLE IF NOT EXISTS prices ("
        "id_commodity INTEGER, id_terminal INTEGER, day INTEGER, ts INTEGER, "
        "price_buy REAL, price_sell REAL, "
        "PRIMARY KEY (id_commodity, id_terminal, day))")
    return con


def _day(ts: float) -> int:
    return (int(ts) // 86400) * 86400      # UTC-midnight epoch (matches chart day-binning)


def _has_today() -> bool:
    try:
        con = _connect()
        row = con.execute("SELECT 1 FROM prices WHERE day=? LIMIT 1",
                          (_day(time.time()),)).fetchone()
        con.close()
        return row is not None
    except Exception:
        return False


def snapshot(prices: List[dict]) -> int:
    """Upsert today's buy/sell price for every (commodity, terminal) in *prices*
    (UEX ``commodities_prices_all`` rows). Returns rows written."""
    ts = time.time()
    day = _day(ts)
    rows = []
    for r in prices:
        cid, tid = r.get("id_commodity"), r.get("id_terminal")
        if not cid or not tid:
            continue
        pb, ps = r.get("price_buy") or 0, r.get("price_sell") or 0
        if not pb and not ps:
            continue
        rows.append((cid, tid, day, int(ts), pb, ps))
    if not rows:
        return 0
    with _LOCK:
        con = _connect()
        con.executemany(
            "INSERT INTO prices (id_commodity, id_terminal, day, ts, price_buy, price_sell) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(id_commodity, id_terminal, day) DO UPDATE SET "
            "ts=excluded.ts, price_buy=excluded.price_buy, price_sell=excluded.price_sell",
            rows)
        con.commit()
        con.close()
    return len(rows)


def maybe_snapshot(force: bool = False) -> None:
    """Snapshot in a background thread when forced (launch) or today has no row
    yet (the daily snapshot). Retries a few times because at launch the Trade Hub
    is hammering UEX in parallel and ``prices_all()`` can transiently come back
    empty. Never blocks or raises into the caller."""
    def run() -> None:
        try:
            if not (force or not _has_today()):
                return                 # today already captured — quiet (fires every refresh)
            for attempt in range(4):
                rows = uex.prices_all()
                if rows:
                    n = snapshot(rows)
                    log.info("price_log: snapshot wrote %d rows (force=%s)", n, force)
                    return
                log.warning("price_log: prices_all() empty, attempt %d", attempt + 1)
                time.sleep(4.0)        # transient empty fetch — back off and retry
        except Exception:
            log.exception("price_log: snapshot failed")
    threading.Thread(target=run, daemon=True, name="PriceLogSnapshot").start()


def history_for_commodity(cid: int) -> List[dict]:
    """Local daily snapshots for a commodity, shaped like UEX history rows
    (``date_added``/``price_buy``/``price_sell``) so the chart bins them by day."""
    out: List[dict] = []
    try:
        con = _connect()
        for day, pb, ps in con.execute(
                "SELECT day, price_buy, price_sell FROM prices WHERE id_commodity=?", (cid,)):
            out.append({"date_added": day, "price_buy": pb, "price_sell": ps})
        con.close()
    except Exception:
        pass
    return out
