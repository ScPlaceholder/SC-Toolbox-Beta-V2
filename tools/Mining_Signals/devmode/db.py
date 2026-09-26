"""SQLite index for captures and glyphs + a small JSON state file.

One connection per call (safe from any worker thread); a process-wide
RLock serialises multi-statement writes such as add-then-evict.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from pathlib import Path

from . import paths

log = logging.getLogger(__name__)

LOCK = threading.RLock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS captures (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    family          TEXT NOT NULL,
    file            TEXT NOT NULL,
    bytes           INTEGER NOT NULL,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    sha             TEXT NOT NULL,
    created         REAL NOT NULL,
    proposed        TEXT,
    proposal_source TEXT,
    engines         TEXT NOT NULL DEFAULT '{}',
    meta            TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL,
    label           TEXT,
    labeled_at      REAL
);
CREATE INDEX IF NOT EXISTS ix_cap_family_status ON captures(family, status);
CREATE UNIQUE INDEX IF NOT EXISTS ix_cap_sha ON captures(family, sha);

CREATE TABLE IF NOT EXISTS glyphs (
    id          TEXT PRIMARY KEY,
    pool        TEXT NOT NULL,
    capture_id  TEXT NOT NULL,
    char        TEXT NOT NULL,
    pos         INTEGER NOT NULL,
    file        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_glyph_pool ON glyphs(pool, char, status);

CREATE TABLE IF NOT EXISTS extractions (
    capture_id  TEXT NOT NULL,
    pool        TEXT NOT NULL,
    ok          INTEGER NOT NULL,
    n           INTEGER NOT NULL,
    PRIMARY KEY (capture_id, pool)
);
"""

_initialised: set[str] = set()


def db_path() -> Path:
    return paths.dev_root() / "devmode.sqlite3"


@contextlib.contextmanager
def connect():
    p = db_path()
    con = sqlite3.connect(str(p), timeout=30)
    con.row_factory = sqlite3.Row
    try:
        key = str(p)
        if key not in _initialised or not p.exists():
            con.executescript(_SCHEMA)
            _initialised.add(key)
        yield con
        con.commit()
    finally:
        con.close()


# ── state.json ──────────────────────────────────────────────────────

DEFAULT_STATE = {
    "capture_enabled": False,
    "cap_bytes": 500 * 1024 * 1024,
    "game_resolution": None,
    "hud_colour": None,
}


def _state_path() -> Path:
    return paths.dev_root() / "state.json"


def read_state() -> dict:
    st = dict(DEFAULT_STATE)
    p = _state_path()
    if p.is_file():
        try:
            st.update(json.loads(p.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            log.warning("devmode: state.json unreadable (%s); using defaults", exc)
    return st


def write_state(**changes) -> dict:
    with LOCK:
        st = read_state()
        st.update(changes)
        p = _state_path()
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
        tmp.replace(p)
        return st
