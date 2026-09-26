"""Glyph extraction from CONFIRMED, TRAIN-split captures + human approval.

Pools follow the registry's glyph_staging_dir, so ``signal`` and
``signal_inv`` share one pool (and one set of approvals), as do
``signal_rgb`` / ``signal_rgb_inv``. Held-out captures are never cut into
glyphs, so nothing from them can reach synth or training.
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Callable, Optional

from PIL import Image

from . import db, kinds, labels, paths, segment

log = logging.getLogger(__name__)

Progress = Optional[Callable[[float, str], None]]
GLYPH_STATUSES = ("pending", "approved", "rejected")


def extract_glyphs(kind: str, progress: Progress = None) -> int:
    """Cut glyphs from every confirmed train-split capture not yet
    extracted for this kind's pool. Returns the number of NEW glyphs."""
    fam = kinds.family(kind)
    pl = kinds.pool(kind)
    rows = labels.confirmed_rows(fam, "train")
    with db.connect() as con:
        done = {r[0] for r in con.execute("SELECT capture_id FROM extractions WHERE pool=?", (pl,))}
    todo = [r for r in rows if r["id"] not in done]
    added = 0
    total = max(1, len(todo))
    for i, cap in enumerate(todo):
        if progress:
            progress(i / total, f"cutting {cap['label']}")
        try:
            with Image.open(cap["image_path"]) as im:
                im.load()
                img = im.copy()
        except (OSError, ValueError) as exc:
            log.warning("devmode: capture %s unreadable: %s", cap["id"], exc)
            continue
        tiles = segment.glyphs_for(img, kind, cap["label"])
        with db.LOCK, db.connect() as con:
            # The label may have changed while we were cutting.
            cur = con.execute("SELECT status, label FROM captures WHERE id=?", (cap["id"],)).fetchone()
            if cur is None or cur["status"] != "confirmed" or cur["label"] != cap["label"]:
                continue
            n = 0
            if tiles:
                for pos, (ch, tile) in enumerate(zip(cap["label"], tiles)):
                    if segment.is_blacklisted(tile):
                        continue
                    gid = uuid.uuid4().hex[:16]
                    rel = f"glyphs/{pl}/{kinds.class_dirname(ch)}/{gid}.png"
                    dest = paths.dev_root() / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    Image.fromarray(tile).save(dest)
                    con.execute(
                        "INSERT INTO glyphs (id, pool, capture_id, char, pos, file, status, created)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (gid, pl, cap["id"], ch, pos, rel, "pending", time.time()))
                    n += 1
            con.execute("INSERT OR REPLACE INTO extractions (capture_id, pool, ok, n) VALUES (?,?,?,?)",
                        (cap["id"], pl, 1 if tiles else 0, n))
            added += n
    if progress:
        progress(1.0, f"{added} new glyphs")
    return added


def _gdict(r) -> dict:
    return {"id": r["id"], "char": r["char"], "image_path": str(paths.dev_root() / r["file"]),
            "status": r["status"], "capture_id": r["capture_id"], "pool": r["pool"]}


def list_glyphs(kind: str, char: Optional[str] = None, status: Optional[str] = None) -> list[dict]:
    sql, args = "SELECT * FROM glyphs WHERE pool=?", [kinds.pool(kind)]
    if char is not None:
        sql += " AND char=?"
        args.append(char)
    if status is not None:
        if status not in GLYPH_STATUSES:
            raise ValueError(f"status must be one of {GLYPH_STATUSES}")
        sql += " AND status=?"
        args.append(status)
    sql += " ORDER BY created, rowid"
    with db.connect() as con:
        return [_gdict(r) for r in con.execute(sql, args).fetchall()]


def _set(glyph_id: str, status: str) -> None:
    with db.LOCK, db.connect() as con:
        cur = con.execute("UPDATE glyphs SET status=? WHERE id=?", (status, glyph_id))
        if cur.rowcount == 0:
            raise KeyError(f"no glyph {glyph_id!r}")


def approve_glyph(glyph_id: str) -> None:
    _set(glyph_id, "approved")


def reject_glyph(glyph_id: str) -> None:
    _set(glyph_id, "rejected")


def glyph_stats(kind: str) -> dict:
    out = {ch: {"approved": 0, "pending": 0, "rejected": 0} for ch in kinds.classes_for(kind)}
    with db.connect() as con:
        for ch, st, n in con.execute(
                "SELECT char, status, COUNT(*) FROM glyphs WHERE pool=? GROUP BY char, status",
                (kinds.pool(kind),)).fetchall():
            out.setdefault(ch, {"approved": 0, "pending": 0, "rejected": 0})[st] = int(n)
    return out


def approved_training_glyphs(kind: str) -> list[dict]:
    """Approved glyphs whose source capture is STILL confirmed and in the
    train split (re-checked here, not trusted from extraction time)."""
    fam = kinds.family(kind)
    train_ids = {c["id"] for c in labels.confirmed_rows(fam, "train")}
    return [g for g in list_glyphs(kind, status="approved") if g["capture_id"] in train_ids]
