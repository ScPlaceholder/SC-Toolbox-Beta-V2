"""Label discipline.

status:  unlabeled -> (engines/import propose) -> proposed
         proposed | unlabeled -> confirm(label) -> confirmed   (the ONLY path to a label)
         any -> reject() -> rejected

``proposed`` is never read as ground truth anywhere: glyph extraction,
training and benchmarking all select ``status='confirmed'`` and use the
``label`` column, which only confirm() writes.

``kind`` filters select the capture FAMILY (a signal capture serves all four
signal kinds).
"""
from __future__ import annotations

import json
import time
from typing import Optional

from . import captures, db, kinds, paths, split

STATUSES = ("unlabeled", "proposed", "confirmed", "rejected")


def row_to_dict(r) -> dict:
    fam = r["family"]
    return {
        "id": r["id"],
        "kind": r["kind"],
        "family": fam,
        "image_path": str(paths.dev_root() / r["file"]),
        "proposed": r["proposed"],
        "proposal_source": r["proposal_source"],
        "engines": json.loads(r["engines"] or "{}"),
        "meta": json.loads(r["meta"] or "{}"),
        "status": r["status"],
        "label": r["label"],
        "split": split.split_of(fam, r["label"]) if r["status"] == "confirmed" else None,
        "created": r["created"],
        "bytes": r["bytes"],
        "width": r["width"],
        "height": r["height"],
    }


def list_captures(status: Optional[str] = None, kind: Optional[str] = None,
                  limit: int = 200, offset: int = 0) -> list[dict]:
    """Newest first."""
    where, args = [], []
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}")
        where.append("status=?")
        args.append(status)
    if kind is not None:
        where.append("family=?")
        args.append(kinds.family(kind))
    sql = "SELECT * FROM captures"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created DESC, rowid DESC LIMIT ? OFFSET ?"
    args += [int(limit), int(offset)]
    with db.connect() as con:
        return [row_to_dict(r) for r in con.execute(sql, args).fetchall()]


def get_capture(capture_id: str) -> dict:
    with db.connect() as con:
        r = con.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
    if r is None:
        raise KeyError(f"no capture {capture_id!r}")
    return row_to_dict(r)


def _drop_glyphs(con, capture_id: str) -> None:
    for g in con.execute("SELECT file FROM glyphs WHERE capture_id=?", (capture_id,)).fetchall():
        captures._unlink(paths.dev_root() / g["file"])
    con.execute("DELETE FROM glyphs WHERE capture_id=?", (capture_id,))
    con.execute("DELETE FROM extractions WHERE capture_id=?", (capture_id,))


def confirm(capture_id: str, label: str) -> None:
    with db.LOCK, db.connect() as con:
        r = con.execute("SELECT kind, status, label FROM captures WHERE id=?",
                        (capture_id,)).fetchone()
        if r is None:
            raise KeyError(f"no capture {capture_id!r}")
        norm = kinds.normalize_label(r["kind"], label)
        if r["status"] == "confirmed" and r["label"] == norm:
            return
        # A changed label invalidates glyphs cut from the old one.
        _drop_glyphs(con, capture_id)
        con.execute("UPDATE captures SET status='confirmed', label=?, labeled_at=? WHERE id=?",
                    (norm, time.time(), capture_id))


def reject(capture_id: str) -> None:
    with db.LOCK, db.connect() as con:
        r = con.execute("SELECT id FROM captures WHERE id=?", (capture_id,)).fetchone()
        if r is None:
            raise KeyError(f"no capture {capture_id!r}")
        _drop_glyphs(con, capture_id)
        con.execute("UPDATE captures SET status='rejected', label=NULL, labeled_at=? WHERE id=?",
                    (time.time(), capture_id))


def label_stats(kind: Optional[str] = None) -> dict:
    out = {s: 0 for s in STATUSES}
    sql = "SELECT status, COUNT(*) FROM captures"
    args: list = []
    if kind is not None:
        sql += " WHERE family=?"
        args.append(kinds.family(kind))
    sql += " GROUP BY status"
    with db.connect() as con:
        for st, n in con.execute(sql, args).fetchall():
            out[st] = int(n)
    return out


def confirmed_rows(fam: str, which: str) -> list[dict]:
    """Confirmed captures of a family on one side of the split
    (which = 'train' | 'heldout' | 'all')."""
    with db.connect() as con:
        rows = con.execute(
            "SELECT * FROM captures WHERE family=? AND status='confirmed' AND label IS NOT NULL "
            "ORDER BY created, rowid", (fam,)).fetchall()
    out = [row_to_dict(r) for r in rows]
    if which == "all":
        return out
    return [d for d in out if d["split"] == which]
