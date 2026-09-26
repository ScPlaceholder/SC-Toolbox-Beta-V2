"""Capture store: add, import, size cap, toggle.

A capture is ONE region crop (a signal panel or a HUD value strip) — never
a full screenshot; add_capture refuses screen-sized images so the export
can promise "crops only".

Proposals: engine consensus (>=2 engines agreeing and a strict majority)
proposes with source "consensus"; otherwise the live reader's value
(meta["reader"]) or a lone engine read proposes with source "reader";
imported filename values propose with source "import". None of these is a
label. Only confirm() (labels.py) makes a label.

Size cap (default 500 MB): when a new capture would exceed it, the oldest
UNCONFIRMED captures are evicted — rejected ones first, then unlabeled /
proposed. Confirmed captures are never evicted; if they alone fill the cap
the new capture is refused with CapFullError rather than silently dropping
human work.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from PIL import Image

from . import db, kinds, paths

log = logging.getLogger(__name__)

Progress = Optional[Callable[[float, str], None]]

# A crop at least this big in BOTH dimensions is treated as a screenshot.
SCREENSHOT_MIN_W = 1280
SCREENSHOT_MIN_H = 720


class CapFullError(RuntimeError):
    """Confirmed captures alone fill the size cap; nothing may be evicted."""


class ScreenshotRefusedError(ValueError):
    """The image is screen-sized; Dev Mode stores crops only."""


def looks_like_screenshot(w: int, h: int) -> bool:
    return w >= SCREENSHOT_MIN_W and h >= SCREENSHOT_MIN_H


# ── toggle / cap ────────────────────────────────────────────────────

def capture_enabled() -> bool:
    return bool(db.read_state()["capture_enabled"])


def set_capture_enabled(on: bool) -> None:
    db.write_state(capture_enabled=bool(on))


def set_capture_cap(cap_bytes: int) -> None:
    if int(cap_bytes) <= 0:
        raise ValueError("cap must be positive")
    db.write_state(cap_bytes=int(cap_bytes))


def capture_stats() -> dict:
    cap = int(db.read_state()["cap_bytes"])
    with db.connect() as con:
        row = con.execute("SELECT COUNT(*), COALESCE(SUM(bytes),0) FROM captures").fetchone()
        conf = con.execute(
            "SELECT COALESCE(SUM(bytes),0) FROM captures WHERE status='confirmed'"
        ).fetchone()[0]
    return {
        "count": int(row[0]),
        "bytes": int(row[1]),
        "cap_bytes": cap,
        "confirmed_bytes": int(conf),
        "over_cap": int(row[1]) > cap,
    }


# ── proposals ───────────────────────────────────────────────────────

def propose(kind: str, engines: dict, meta: dict) -> tuple[Optional[str], Optional[str]]:
    vals = [v for v in (kinds.normalize_value(kind, x) for x in (engines or {}).values()) if v]
    if vals:
        top, n = Counter(vals).most_common(1)[0]
        if n >= 2 and n * 2 > len(vals):
            return top, "consensus"
    reader = kinds.normalize_value(kind, (meta or {}).get("reader"))
    if reader:
        return reader, "reader"
    if vals:
        return Counter(vals).most_common(1)[0][0], "reader"
    return None, None


# ── storage ─────────────────────────────────────────────────────────

def _new_id() -> str:
    return f"{int(time.time() * 1000):013d}{uuid.uuid4().hex[:8]}"


def _pixel_sha(img: Image.Image) -> str:
    h = hashlib.sha1()
    h.update(f"{img.mode}{img.size}".encode())
    h.update(img.tobytes())
    return h.hexdigest()


def _abs(rel: str) -> Path:
    return paths.dev_root() / rel


def delete_capture_files(con, capture_id: str) -> None:
    """Remove a capture, its glyphs and their files (caller holds LOCK)."""
    row = con.execute("SELECT file FROM captures WHERE id=?", (capture_id,)).fetchone()
    for g in con.execute("SELECT file FROM glyphs WHERE capture_id=?", (capture_id,)).fetchall():
        _unlink(_abs(g["file"]))
    con.execute("DELETE FROM glyphs WHERE capture_id=?", (capture_id,))
    con.execute("DELETE FROM extractions WHERE capture_id=?", (capture_id,))
    if row is not None:
        _unlink(_abs(row["file"]))
    con.execute("DELETE FROM captures WHERE id=?", (capture_id,))


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("devmode: could not delete %s: %s", p, exc)


def _make_room(con, need: int) -> None:
    cap = int(db.read_state()["cap_bytes"])
    total = int(con.execute("SELECT COALESCE(SUM(bytes),0) FROM captures").fetchone()[0])
    if total + need <= cap:
        return
    victims = con.execute(
        "SELECT id, bytes FROM captures WHERE status != 'confirmed' "
        "ORDER BY CASE status WHEN 'rejected' THEN 0 ELSE 1 END, created, rowid"
    ).fetchall()
    freeable = sum(int(v["bytes"]) for v in victims)
    if total - freeable + need > cap:
        raise CapFullError(
            f"capture cap {cap} bytes is full of CONFIRMED captures "
            f"({total - freeable} bytes); raise the cap or export and prune — "
            "confirmed work is never evicted silently"
        )
    for v in victims:
        if total + need <= cap:
            break
        delete_capture_files(con, v["id"])
        total -= int(v["bytes"])
        log.info("devmode: evicted capture %s (cap)", v["id"])


def add_capture(image: Image.Image, kind: str, engines: dict, meta: dict,
                *, created: Optional[float] = None,
                proposal: Optional[tuple[Optional[str], Optional[str]]] = None) -> str:
    """Store one crop; returns its id (an existing id for an exact duplicate)."""
    kinds.check_kind(kind)
    fam = kinds.family(kind)
    if image is None:
        raise ValueError("image is None")
    w, h = image.size
    if looks_like_screenshot(w, h):
        raise ScreenshotRefusedError(
            f"{w}x{h} looks like a full screenshot; Dev Mode stores region crops only"
        )
    img = image.convert("RGB")
    sha = _pixel_sha(img)
    buf = io.BytesIO()
    img.save(buf, format="PNG")          # re-encode: strips any metadata
    data = buf.getvalue()
    engines = {str(k): (None if v is None else str(v)) for k, v in (engines or {}).items()}
    meta = dict(meta or {})
    if proposal is None:
        proposal = propose(kind, engines, meta)
    proposed, source = proposal
    status = "proposed" if proposed else "unlabeled"

    with db.LOCK, db.connect() as con:
        dup = con.execute(
            "SELECT id FROM captures WHERE family=? AND sha=?", (fam, sha)
        ).fetchone()
        if dup is not None:
            return dup["id"]
        _make_room(con, len(data))
        cid = _new_id()
        rel = f"captures/{fam}/{cid}.png"
        dest = _abs(rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        con.execute(
            "INSERT INTO captures (id, kind, family, file, bytes, width, height, sha, created,"
            " proposed, proposal_source, engines, meta, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, kind, fam, rel, len(data), w, h, sha,
             float(created if created is not None else time.time()),
             proposed, source, json.dumps(engines), json.dumps(meta, default=str), status),
        )
        return cid


# ── import ──────────────────────────────────────────────────────────

_LIVE_RE = re.compile(r"^sig_\d+_\d+_(?P<v>[^_]+)$")


def value_from_filename(stem: str) -> Optional[str]:
    """``sig_<t>_<ms>_<value>`` (screen_reader live samples) or the last
    ``_`` token of any other name. A PROPOSAL only: the reader that wrote
    these names was wrong about 1 time in 5."""
    m = _LIVE_RE.match(stem)
    if m:
        return m.group("v")
    if "_" in stem:
        return stem.rsplit("_", 1)[1]
    return None


def import_folder(path: str, kind: str, progress: Progress = None) -> int:
    kinds.check_kind(kind)
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"not a folder: {path}")
    files = sorted(
        (p for p in root.rglob("*") if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp")),
        key=lambda p: (p.stat().st_mtime, p.name),
    )
    added = 0
    with db.connect() as con:
        before = {r[0] for r in con.execute("SELECT id FROM captures")}
    total = max(1, len(files))
    for i, f in enumerate(files):
        if progress:
            progress(i / total, f"importing {f.name}")
        value = kinds.normalize_value(kind, value_from_filename(f.stem))
        try:
            with Image.open(f) as im:
                im.load()
                img = im.copy()
        except (OSError, ValueError) as exc:
            log.warning("devmode: import skipped unreadable %s: %s", f.name, exc)
            continue
        try:
            cid = add_capture(
                img, kind, {}, {"imported": True, "source_name": f.name},
                created=f.stat().st_mtime,
                proposal=(value, "import") if value else (None, None),
            )
        except ScreenshotRefusedError as exc:
            log.info("devmode: import skipped %s: %s", f.name, exc)
            continue
        except CapFullError as exc:
            log.warning("devmode: import stopped at %s: %s", f.name, exc)
            if progress:
                progress(1.0, f"stopped: {exc}")
            break
        if cid not in before:
            before.add(cid)
            added += 1
    if progress:
        progress(1.0, f"imported {added}")
    return added
