"""eyes_reference.py - the eyes' ship reference: what the public reference site holds, fetched when needed, kept.

No Qt, no numpy at import, nothing from the rest of the Suit. Tested without a window or a network
(tests/test_eyes_reference.py). The format of the index is written down in EYES_REFERENCE.md beside the Suit.

WHAT THIS IS, AND WHAT IT IS NOT
    The reference site holds FINGERPRINTS of ship footage: one list of numbers per video frame, made by a
    vision model, with the ship each frame shows. This module knows what is there (the index), fetches a
    table the first time something asks for it, checks it, and keeps it.

    ⛔ RECOGNITION IS NOT CONNECTED (RECOGNITION_CONNECTED is False, and nothing here pretends otherwise).
    A table can only be compared with a fingerprint made by the SAME model and the SAME preprocessing
    (CLIP ViT-H/14 or DINOv2; the index names them). The Suit has neither: its eyes are a frame compare, a
    nearest-neighbour over 32x18 thumbnails, and an optional local vision model that answers in words
    (eyes.py). Nothing in the Suit can turn a screen frame into one of these fingerprints, so nothing here
    names a ship. And the site's own measurement says whole-frame fingerprints cannot name a ship from one
    frame anyway (Index.limits; report.md on the site). What it would take is in EYES_REFERENCE.md.

WHERE THINGS LIVE
    <install>/tools/SuitMk2/data/eyes_reference_index.json   the index the toolbox was built with (read only)
    ~/.sctoolbox/suitmk2/eyes_reference/index.json           the newest index the site gave
    ~/.sctoolbox/suitmk2/eyes_reference/have.json            the record: which files are on this PC (see RECORD)
    ~/.sctoolbox/suitmk2/eyes_reference/files/<site path>    files fetched on this PC, at the path the site uses

THE RULES
    * One address: BASE_URL. The settings key "eyes_reference_url" replaces it; "" means OFFLINE (the index
      that shipped and the files already on disk are used, and nothing is ever requested).
    * At start-up at most ONE request: index.json (about 22 kB), and not again for a day. Nothing else.
      No table is fetched until something asks for that table.
    * Asking for a file looks on disk first. There, and its sha256 right: used, nothing requested. Missing
      or damaged: fetched. A file is fetched once and never again; the site never changes a published file.
    * A download is written to <name>.part and renamed only after its size and sha256 match the index, so a
      half download or a wrong file is never taken for a table. The host answers a MISSING address with its
      front page and status 200, so "it arrived" proves nothing; the checksum is the only proof.
    * https only, redirects included, and only to the one host. The only requests ever made are GETs for
      index.json and for files the index or a batch manifest names. Nothing is sent about the pilot, the PC
      or the game: no frame, no id, no query string. The User-Agent names the toolbox and nothing else (the
      host answers 403 to Python's default one).
    * Every failure is a ReferenceProblem with a sentence in it. startup() and start_in_background() never
      raise at all: a companion with no internet is a companion, not a traceback.

RECORD (have.json)
    One row per fetched file: sha256, bytes, modified time, when it arrived, the index version it was checked
    against. A memory, never the evidence: reconcile() compares it with the disk. A recorded file that is gone
    is dropped. A file the record does not know, or whose size or time differ, is checksummed in full: right,
    it is adopted (a table copied in by hand is not downloaded again); wrong, it is dropped and will be
    fetched when next asked for. path_of() always checksums in full before handing a file out.

Command line (no window, no game):
    python eyes_reference.py --status            what is known and what is on this PC; requests nothing
    python eyes_reference.py --refresh           ask the site for index.json now
    python eyes_reference.py --pull dinov2_s_all fetch one table and its row labels, check them, print the requests
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# THE ONE ADDRESS. The site went up on 2026-10-06. Empty would mean offline.
# "eyes_reference_url" in the Suit's settings.json overrides it; set that to "" to stay offline.
BASE_URL = "https://suitmk2-eyes.pages.dev"
SETTINGS_KEY = "eyes_reference_url"
# The host refuses Python's default agent ("Python-urllib/x.y") with HTTP 403, so every request names the
# toolbox instead. It identifies the program only: no version of the pilot's system, no id.
USER_AGENT = "SC-Toolbox-SuitEyes/1"

# Nothing in the Suit can make a fingerprint these tables can be compared with. See the module docstring.
RECOGNITION_CONNECTED = False
RECOGNITION_NOTE = ("not connected: the Suit has no model that can make a fingerprint these tables can be compared "
                    "with, and the site's own measurement says whole-frame fingerprints cannot name a ship from "
                    "one frame")

INDEX_NAME = "index.json"
RECORD_NAME = "have.json"
FILES_DIR = "files"
FORMAT = 1
MAX_INDEX_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 64 * 1024 * 1024       # the largest published file is 20.0 MB
RECHECK_S = 24 * 3600.0                 # start-up asks for the index at most this often
TIMEOUT_S = 20
CHUNK = 128 * 1024
TABLE_KINDS = ("f16_npy", "rows")       # what load_table() needs: the float16 array and its row labels

BUNDLED_INDEX = Path(__file__).resolve().parent.parent / "data" / "eyes_reference_index.json"

_SITE_PATH = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9_.+-]*(/[A-Za-z0-9_-][A-Za-z0-9_.+-]*)*$")
_KEY = re.compile(r"^[a-z0-9_]{1,40}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | {"LPT%d" % i for i in range(1, 10)}


class ReferenceProblem(Exception):
    """Something the reference could not do. str() is a sentence that can be shown or logged."""


def home_dir() -> Path:
    """Beside the Suit's settings (settings.DIR), never inside the install."""
    return Path.home() / ".sctoolbox" / "suitmk2" / "eyes_reference"


def base_url_from(settings: Optional[dict]) -> str:
    """The address to use: the settings key if it holds a string ("" = offline), else BASE_URL."""
    v = (settings or {}).get(SETTINGS_KEY)
    return v.strip() if isinstance(v, str) else BASE_URL


def mb(n: int) -> str:
    return "%.1f MB" % (n / 1e6)


def safe_site_path(path) -> bool:
    """True for a path the site could publish and this PC can safely write under files/."""
    if not isinstance(path, str) or len(path) > 200 or not _SITE_PATH.match(path):
        return False
    return all(seg.split(".")[0].upper() not in _RESERVED and not seg.endswith(".") for seg in path.split("/"))


@dataclass(frozen=True)
class FileRef:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True)
class Table:
    id: str
    since: int
    batch: str
    model: str
    space: str
    rows: int
    width: int
    rows_per_ship: dict
    files: dict                 # kind -> FileRef
    subset_of: Optional[str]


@dataclass(frozen=True)
class Ship:
    key: str
    name: str
    family: str
    since: int
    frames: dict
    tables: dict
    videos: tuple


@dataclass(frozen=True)
class Index:
    version: int
    date: str
    limits: str
    ships: dict                 # key -> Ship, in the index's order
    tables: dict                # id -> Table
    batches: dict               # id -> {"since": int, "manifest": FileRef, "ships": [...]}
    models: dict
    raw: bytes

    @classmethod
    def empty(cls) -> "Index":
        return cls(0, "", "", {}, {}, {}, {}, b"")

    def families(self) -> dict:
        out: dict = {}
        for s in self.ships.values():
            out.setdefault(s.family, []).append(s.key)
        return out

    def new_since(self, version: int) -> dict:
        """What arrived after index `version`: {"ships": [keys], "tables": [ids]}."""
        return {"ships": [k for k, s in self.ships.items() if s.since > version],
                "tables": [k for k, t in self.tables.items() if t.since > version]}

    def space(self, space: str) -> list:
        """Every table that can be stacked into one reference for this recipe, oldest first, parts left out."""
        return [t for t in self.tables.values() if t.space == space and not t.subset_of]


def _ref(o, limit: int = 0) -> FileRef:
    r = FileRef(str(o["path"]), int(o["bytes"]), str(o["sha256"]).lower())
    if not safe_site_path(r.path) or not _SHA.match(r.sha256) or r.bytes <= 0 or (limit and r.bytes > limit):
        raise ValueError(r.path[:60])
    return r


def parse_index(raw: bytes) -> Index:
    """A checked Index from the bytes of an index.json. Anything odd is a ReferenceProblem: a name from a
    server is never used as a path until it has passed safe_site_path. Keys this version does not know are
    ignored, so a later index that only ADDS things still reads."""
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ReferenceProblem("the reference index cannot be read")
    if not isinstance(obj, dict) or obj.get("format") != FORMAT:
        raise ReferenceProblem("the reference index is not one this version of the Suit understands")
    try:
        version = int(obj["version"])
        if version < 1:
            raise ValueError("version")
        tables: dict = {}
        for t in obj["tables"]:
            tid = str(t["id"])
            if not _KEY.match(tid) or tid in tables:
                raise ValueError(tid)
            tables[tid] = Table(tid, int(t["since"]), str(t["batch"]), str(t["model"]), str(t["space"]),
                                int(t["rows"]), int(t["width"]), dict(t.get("rows_per_ship") or {}),
                                {str(k): _ref(v) for k, v in t["files"].items()}, t.get("subset_of") or None)
        batches: dict = {}
        for b in obj["batches"]:
            bid = str(b["id"])
            if not _KEY.match(bid) or bid in batches:
                raise ValueError(bid)
            batches[bid] = {"since": int(b["since"]), "manifest": _ref(b["manifest"], MAX_MANIFEST_BYTES),
                            "ships": [str(k) for k in b.get("ships", [])]}
        ships: dict = {}
        for s in obj["ships"]:
            key = str(s["key"])
            if not _KEY.match(key) or key in ships:
                raise ValueError(key)
            ships[key] = Ship(key, str(s["name"])[:60], str(s["family"])[:40], int(s["since"]),
                              dict(s.get("frames") or {}), dict(s.get("tables") or {}),
                              tuple(str(v.get("id", "")) for v in s.get("videos", [])))
        models = obj.get("models") if isinstance(obj.get("models"), dict) else {}
    except (KeyError, TypeError, ValueError, AttributeError) as ex:
        raise ReferenceProblem("the reference index has an entry that cannot be read (%s)" % str(ex)[:60])
    return Index(version, str(obj.get("date", "")), str(obj.get("limits", "")), ships, tables, batches, models, raw)


def sha256_of(path: Path) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _reason(ex: BaseException) -> str:
    r = getattr(ex, "reason", None)
    text = str(r if r is not None else ex) or ex.__class__.__name__
    return text[:120]


def _looks_like_a_page(head: bytes) -> bool:
    return head.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))


class _SameRules(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to an address the store would have asked for itself."""

    def __init__(self, store: "ReferenceStore"):
        self.store = store

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.store.check_url(newurl)
        self.store.requests.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class ReferenceStore:
    def __init__(self, home: Optional[Path] = None, base_url: Optional[str] = None,
                 bundled: Optional[Path] = None, allow_loopback_http: bool = False, clock=time.time):
        self.home = Path(home) if home is not None else home_dir()
        self.files_dir = self.home / FILES_DIR
        self.base_url = (BASE_URL if base_url is None else base_url or "").strip().rstrip("/")
        # Tests serve the site from http://127.0.0.1. Nothing in the Suit passes True.
        self.allow_loopback_http = allow_loopback_http
        self.clock = clock
        self.requests: list = []          # every address asked for, in order
        self.transfers: list = []         # one row per answered request: {"url", "bytes", "seconds", "ok"}
        self._lock = threading.RLock()
        self._manifests: dict = {}
        self.bundled_index = self._read_index(Path(bundled) if bundled is not None else BUNDLED_INDEX)
        own = self._read_index(self.home / INDEX_NAME)
        # The newer of the two. A toolbox update can ship a newer index than the one fetched long ago.
        self.index = own if own.version >= self.bundled_index.version else self.bundled_index
        rec = self._read_record()
        self.record: dict = rec["files"]
        self.checked_at: float = rec["checked_at"]
        self.seen_version: int = rec["seen_version"]

    # ---- the index ------------------------------------------------------------------------------------
    @staticmethod
    def _read_index(path: Path) -> Index:
        try:
            return parse_index(Path(path).read_bytes())
        except (OSError, ReferenceProblem):
            return Index.empty()

    @property
    def online(self) -> bool:
        return bool(self.base_url)

    def table(self, table_id: str) -> Table:
        t = self.index.tables.get(table_id)
        if t is None:
            raise ReferenceProblem("there is no table called %s in the reference index" % str(table_id)[:40])
        return t

    def whats_new(self) -> dict:
        """Ships and tables that arrived since mark_seen() was last called."""
        return self.index.new_since(self.seen_version)

    def mark_seen(self) -> None:
        if self.seen_version != self.index.version:
            self.seen_version = self.index.version
            self._write_record()

    # ---- the record -----------------------------------------------------------------------------------
    def _read_record(self) -> dict:
        out = {"files": {}, "checked_at": 0.0, "seen_version": 0}
        try:
            d = json.loads((self.home / RECORD_NAME).read_text(encoding="utf-8"))
            out["files"] = {p: r for p, r in d.get("files", {}).items() if isinstance(r, dict) and safe_site_path(p)}
            out["checked_at"] = float(d.get("checked_at") or 0.0)
            out["seen_version"] = int(d.get("seen_version") or 0)
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return out

    def _write_record(self) -> None:
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            tmp = self.home / (RECORD_NAME + ".tmp")
            tmp.write_text(json.dumps({"format": FORMAT, "checked_at": self.checked_at,
                                       "seen_version": self.seen_version, "files": self.record}, indent=1),
                           encoding="utf-8")
            os.replace(tmp, self.home / RECORD_NAME)
        except OSError:
            pass                              # a read-only profile must not take the companion down

    def _dest(self, path: str) -> Path:
        if not safe_site_path(path):
            raise ReferenceProblem("that is not a path the reference site could hold")
        return self.files_dir.joinpath(*path.split("/"))

    def _remember(self, ref: FileRef, origin: str) -> None:
        st = self._dest(ref.path).stat()
        old = self.record.get(ref.path, {})
        same = old.get("sha256") == ref.sha256
        new = {"sha256": ref.sha256, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
               "fetched": old.get("fetched") if same else round(self.clock()),
               "origin": old.get("origin", origin) if same else origin,
               "version": old.get("version") if same else self.index.version}
        if new != old:
            self.record[ref.path] = new
            self._write_record()

    def _forget(self, path: str) -> None:
        if self.record.pop(path, None) is not None:
            self._write_record()

    def _quick(self, ref: FileRef) -> bool:
        """The record says this file is here and right, and its size and modified time still agree."""
        r = self.record.get(ref.path)
        try:
            st = self._dest(ref.path).stat()
        except (OSError, ReferenceProblem):
            return False
        return bool(r) and r.get("sha256") == ref.sha256 and r.get("bytes") == st.st_size \
            and r.get("mtime_ns") == st.st_mtime_ns

    def _known_refs(self) -> dict:
        refs = {}
        for t in self.index.tables.values():
            for r in t.files.values():
                refs[r.path] = r
        for b in self.index.batches.values():
            refs[b["manifest"].path] = b["manifest"]
        return refs

    def reconcile(self) -> dict:
        """Make the record agree with the disk. Run at start. {"kept", "adopted", "dropped"}: lists of paths.
        Reads a file in full only when the record does not already vouch for it."""
        out = {"kept": [], "adopted": [], "dropped": []}
        with self._lock:
            refs = self._known_refs()
            changed = False
            for path in sorted(set(self.record) | set(refs)):
                ref = refs.get(path)
                p = self._dest(path)
                if not p.is_file():
                    if path in self.record:
                        del self.record[path]
                        out["dropped"].append(path)
                        changed = True
                    continue
                if ref is not None and self._quick(ref):
                    out["kept"].append(path)
                    continue
                if ref is None:
                    # Recorded, but the index in hand does not name it (a file reached through a batch
                    # manifest). Believe the record only while size and time still agree.
                    r = self.record[path]
                    st = p.stat()
                    if r.get("bytes") == st.st_size and r.get("mtime_ns") == st.st_mtime_ns:
                        out["kept"].append(path)
                    else:
                        del self.record[path]
                        out["dropped"].append(path)
                        changed = True
                    continue
                if sha256_of(p) == ref.sha256:       # first sight, or the file changed: read all of it
                    known = path in self.record
                    self._remember(ref, "adopted")
                    out["kept" if known else "adopted"].append(path)
                elif path in self.record:
                    del self.record[path]
                    out["dropped"].append(path)
                    changed = True
            if changed:
                self._write_record()
        return out

    # ---- is it here -----------------------------------------------------------------------------------
    def present(self, ref: FileRef) -> bool:
        """Quick, for lists: here as far as the record, the size and the modified time say. Reads no file."""
        return self._quick(ref)

    def path_of(self, ref: FileRef) -> Optional[Path]:
        """The file on this PC, its sha256 computed now and found right; else None. Never a request."""
        with self._lock:
            p = self._dest(ref.path)
            if not p.is_file():
                return None
            if sha256_of(p) == ref.sha256:
                self._remember(ref, "adopted")
                return p
            self._forget(ref.path)
            return None

    def cached_bytes(self) -> int:
        total = 0
        for path in list(self.record):
            try:
                total += self._dest(path).stat().st_size
            except (OSError, ReferenceProblem):
                pass
        return total

    def plan(self, table_id: str, kinds=TABLE_KINDS) -> dict:
        """What using this table would cost: {"to_fetch": bytes, "on_disk": bytes, "files": [(path, bytes, here)]}.
        Requests nothing and reads no table."""
        t = self.table(table_id)
        rows = [(t.files[k].path, t.files[k].bytes, self.present(t.files[k])) for k in kinds if k in t.files]
        return {"to_fetch": sum(b for _, b, here in rows if not here),
                "on_disk": sum(b for _, b, here in rows if here), "files": rows}

    # ---- the network ----------------------------------------------------------------------------------
    def check_url(self, url: str) -> None:
        u, base = urllib.parse.urlsplit(url), urllib.parse.urlsplit(self.base_url)
        inside = u.hostname == base.hostname and u.port == base.port and \
            (u.path == base.path or u.path.startswith(base.path.rstrip("/") + "/")) and not u.query
        if u.scheme == "https" and u.hostname and inside:
            return
        if self.allow_loopback_http and u.scheme == "http" and u.hostname in _LOOPBACK and inside:
            return
        raise ReferenceProblem("the reference address is not an https address on the reference site, so nothing "
                               "was requested")

    def _open(self, path: str):
        if not self.online:
            raise ReferenceProblem("the ship reference is set to offline, so nothing was requested")
        url = "%s/%s" % (self.base_url, path)
        self.check_url(url)
        self.requests.append(url)
        opener = urllib.request.build_opener(_SameRules(self))
        try:
            return opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT_S)
        except ReferenceProblem:
            raise
        except urllib.error.HTTPError as ex:
            raise ReferenceProblem("the reference site answered with an error (%s)" % ex.code)
        except Exception as ex:                   # noqa: BLE001 - no network fault may reach the companion
            raise ReferenceProblem("the reference site could not be reached (%s)" % _reason(ex))

    def _note(self, path: str, t0: float, n: int, ok: bool) -> None:
        self.transfers.append({"url": "%s/%s" % (self.base_url, path), "bytes": n,
                               "seconds": round(time.perf_counter() - t0, 3), "ok": ok})

    def _read_small(self, path: str, limit: int, what: str) -> bytes:
        t0 = time.perf_counter()
        raw = b""
        asked = len(self.requests)
        try:
            with self._open(path) as r:
                try:
                    raw = r.read(limit + 1)
                except Exception as ex:           # noqa: BLE001
                    raise ReferenceProblem("%s did not arrive (%s)" % (what, _reason(ex)))
        finally:
            if len(self.requests) > asked:
                self._note(path, t0, len(raw), bool(raw) and len(raw) <= limit)
        if len(raw) > limit:
            raise ReferenceProblem("%s from the site is too big to be real" % what)
        return raw

    def refresh_index(self) -> dict:
        """Ask the site for index.json (ONE request) and keep it if it is the same or newer. The index in hand
        stays if this fails. {"version", "ships": [new keys], "tables": [new ids], "changed": [table ids whose
        files differ from what the old index said - the site promises this list is always empty]}."""
        with self._lock:
            raw = self._read_small(INDEX_NAME, MAX_INDEX_BYTES, "the reference index")
            self.checked_at = float(self.clock())   # the site answered; do not ask again at every start
            self._write_record()
            if _looks_like_a_page(raw[:64]):
                raise ReferenceProblem("the reference site has no index yet (it answered with its front page), so "
                                       "the index that came with the toolbox is used")
            new = parse_index(raw)
            old = self.index
            if new.version < old.version:
                raise ReferenceProblem("the site's reference index (version %d) is older than the one in hand "
                                       "(version %d), so the one in hand is kept" % (new.version, old.version))
            changed = sorted(tid for tid, t in old.tables.items()
                             if tid in new.tables and new.tables[tid].files != t.files)
            missing = sorted(set(old.tables) - set(new.tables))
            if new.version == old.version and new.raw != old.raw and (changed or missing):
                raise ReferenceProblem("the site's reference index has the same version as the one in hand but "
                                       "describes different files, so the one in hand is kept")
            try:
                self.home.mkdir(parents=True, exist_ok=True)
                tmp = self.home / (INDEX_NAME + ".tmp")
                tmp.write_bytes(raw)
                os.replace(tmp, self.home / INDEX_NAME)
            except OSError:
                pass
            self.index = new
            self._manifests.clear()
            added = new.new_since(old.version) if old.version else {"ships": list(new.ships),
                                                                   "tables": list(new.tables)}
            return {"version": new.version, "ships": added["ships"], "tables": added["tables"],
                    "changed": changed + missing}

    def _download(self, ref: FileRef, part: Path, progress, cancel) -> tuple:
        """Stream one file into `part`. (sha256, bytes, the first bytes that arrived)."""
        h = hashlib.sha256()
        done, head = 0, b""
        t0 = time.perf_counter()
        ok = False
        asked = len(self.requests)
        try:
            with self._open(ref.path) as r:
                try:
                    with open(part, "wb") as f:
                        while True:
                            if cancel is not None and cancel.is_set():
                                raise ReferenceProblem("the download was cancelled")
                            chunk = r.read1(CHUNK)
                            if not chunk:
                                break
                            if not head:
                                head = chunk[:64]
                            done += len(chunk)
                            if done > ref.bytes or done > MAX_FILE_BYTES:
                                raise ReferenceProblem("the site sent more than the index says this file holds, "
                                                       "so it was thrown away")
                            h.update(chunk)
                            f.write(chunk)
                            if progress is not None:
                                progress(ref.path, done, ref.bytes)
                except ReferenceProblem:
                    raise
                except OSError as ex:
                    if isinstance(ex, (urllib.error.URLError, TimeoutError, ConnectionError)):
                        raise ReferenceProblem("the download stopped part way (%s)" % _reason(ex))
                    raise ReferenceProblem("the file could not be saved on this PC (%s)" % _reason(ex))
                except Exception as ex:           # noqa: BLE001
                    raise ReferenceProblem("the download stopped part way (%s)" % _reason(ex))
            ok = True
        finally:
            if len(self.requests) > asked:
                self._note(ref.path, t0, done, ok)
        return h.hexdigest(), done, head

    def get(self, ref: FileRef, progress: Optional[Callable] = None, cancel=None) -> Path:
        """This file, on this PC and checked. On disk and right: no request. Otherwise ONE request, and the
        file is kept. Raises ReferenceProblem; a failed fetch leaves nothing behind that looks like the file."""
        with self._lock:
            here = self.path_of(ref)
            if here is not None:
                return here
            if ref.bytes > MAX_FILE_BYTES:
                raise ReferenceProblem("%s is %s, more than the Suit will download for one file"
                                       % (ref.path, mb(ref.bytes)))
            final = self._dest(ref.path)
            part = final.with_name(final.name + ".part")
            try:
                try:
                    final.parent.mkdir(parents=True, exist_ok=True)
                except OSError as ex:
                    raise ReferenceProblem("the reference folder cannot be made on this PC (%s)" % _reason(ex))
                sha, n, head = self._download(ref, part, progress, cancel)
                if sha != ref.sha256 or n != ref.bytes:
                    if _looks_like_a_page(head):
                        raise ReferenceProblem("the reference site does not have %s (it answered with its front "
                                               "page), so nothing was kept" % ref.path)
                    raise ReferenceProblem("the file that arrived for %s is not the one in the index (checksum "
                                           "mismatch), so it was thrown away" % ref.path)
                os.replace(part, final)           # only now is it a reference file
                self._remember(ref, "fetched")
                return final
            except OSError as ex:
                raise ReferenceProblem("the file could not be saved on this PC (%s)" % _reason(ex))
            finally:
                try:
                    part.unlink()
                except OSError:
                    pass

    # ---- tables ---------------------------------------------------------------------------------------
    def table_file(self, table_id: str, kind: str = "f16_npy", progress=None, cancel=None) -> Path:
        t = self.table(table_id)
        if kind not in t.files:
            raise ReferenceProblem("table %s has no %s file" % (table_id, str(kind)[:20]))
        return self.get(t.files[kind], progress, cancel)

    def table_rows(self, table_id: str) -> list:
        """The label of every row of a table: ship, view, room, source video id, time."""
        try:
            rows = json.loads(self.table_file(table_id, "rows").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ReferenceProblem("the row labels of table %s cannot be read" % table_id)
        if not isinstance(rows, list) or len(rows) != self.table(table_id).rows:
            raise ReferenceProblem("the row labels of table %s do not match its size" % table_id)
        return rows

    def load_table(self, table_id: str):
        """The table as a read-only numpy array (rows x width, float16), fetched first if need be. Mapped from
        the file, so opening it reads almost nothing. Needs numpy; without it this is a ReferenceProblem and
        table_file() still works."""
        path = self.table_file(table_id, "f16_npy")
        try:
            import numpy as np
        except ImportError:
            raise ReferenceProblem("numpy is not installed, so the table is on this PC but cannot be opened")
        t = self.table(table_id)
        try:
            arr = np.load(str(path), mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError) as ex:
            raise ReferenceProblem("table %s cannot be opened (%s)" % (table_id, _reason(ex)))
        if tuple(arr.shape) != (t.rows, t.width):
            raise ReferenceProblem("table %s is %s, not the %d x %d the index says"
                                   % (table_id, "x".join(map(str, arr.shape)), t.rows, t.width))
        return arr

    # ---- any other published file, through a batch's own list ---------------------------------------------
    def manifest(self, batch_id: str) -> dict:
        """{site path: FileRef} for every file of one batch. The list itself is fetched once (batch 1: 321 kB)
        and checked against the sha256 the index holds for it."""
        b = self.index.batches.get(batch_id)
        if b is None:
            raise ReferenceProblem("there is no batch called %s in the reference index" % str(batch_id)[:20])
        with self._lock:
            if batch_id not in self._manifests:
                p = self.get(b["manifest"])
                out = {}
                try:
                    for row in json.loads(p.read_text(encoding="utf-8"))["files"]:
                        try:
                            r = _ref(row)
                        except (KeyError, TypeError, ValueError):
                            continue                  # a row this PC would not write to disk is left out
                        out[r.path] = r
                except (OSError, ValueError, KeyError, TypeError):
                    raise ReferenceProblem("the file list of batch %s cannot be read" % batch_id)
                self._manifests[batch_id] = out
            return self._manifests[batch_id]

    def file(self, path: str, progress=None, cancel=None) -> Path:
        """Any published file by its site path, fetched once and checked against its batch's list."""
        for bid in self.index.batches:
            ref = self.manifest(bid).get(path)
            if ref is not None:
                return self.get(ref, progress, cancel)
        raise ReferenceProblem("no batch of the reference lists a file called %s" % str(path)[:80])

    # ---- start-up -------------------------------------------------------------------------------------
    def startup(self, force: bool = False) -> dict:
        """What the Suit does with the reference when it starts. NEVER raises.
        Makes the record agree with the disk, then asks the site for index.json: one request, at most once a
        day, and none at all when offline. No table is fetched here, ever.
        {"ok", "online", "asked", "bytes", "version", "ships", "new_ships", "message"}."""
        out = {"ok": True, "online": self.online, "asked": False, "bytes": 0, "version": self.index.version,
               "ships": len(self.index.ships), "new_ships": [], "message": ""}
        try:
            self.reconcile()
            if not self.online:
                out["message"] = "offline: using the index that came with the toolbox and what is on this PC"
            elif not force and self.checked_at > 0 and 0 <= self.clock() - self.checked_at < RECHECK_S:
                out["message"] = "the index was checked less than a day ago; nothing requested"
            else:
                before = len(self.transfers)
                out["asked"] = True
                try:
                    got = self.refresh_index()
                    out["message"] = "index version %d from the site" % got["version"]
                    if got["changed"]:
                        out["message"] += "; the site changed files it had published: " + ", ".join(got["changed"])
                except ReferenceProblem as ex:
                    out["ok"] = False
                    out["message"] = str(ex)
                finally:
                    out["bytes"] = sum(t["bytes"] for t in self.transfers[before:])
            out["version"], out["ships"] = self.index.version, len(self.index.ships)
            # Ships this PC has not been told about before. The first run only sets the mark: on a new PC
            # every ship is "new" and saying so would be noise.
            if self.seen_version:
                out["new_ships"] = self.whats_new()["ships"]
                if out["new_ships"]:
                    out["message"] += "; new ships: " + ", ".join(out["new_ships"])
            self.mark_seen()
        except Exception as ex:                   # noqa: BLE001 - start-up must not depend on this module
            out["ok"] = False
            out["message"] = "the ship reference could not start (%s: %s)" % (type(ex).__name__, str(ex)[:120])
        return out

    def status(self) -> dict:
        """What is known and what is on this PC. Requests nothing, reads no table."""
        return {"address": self.base_url or "(offline)", "index_version": self.index.version,
                "ships": {k: s.name for k, s in self.index.ships.items()},
                "tables": {tid: {"model": t.model, "rows": t.rows, "width": t.width, "since": t.since,
                                 "bytes": t.files["f16_npy"].bytes if "f16_npy" in t.files else 0,
                                 "here": "f16_npy" in t.files and self.present(t.files["f16_npy"])}
                           for tid, t in self.index.tables.items()},
                "on_this_pc_bytes": self.cached_bytes(), "new": self.whats_new(),
                "recognition": RECOGNITION_NOTE}


def for_settings(settings: Optional[dict]) -> ReferenceStore:
    return ReferenceStore(base_url=base_url_from(settings))


def start_in_background(log: Callable[[str], None], settings: Optional[dict] = None) -> Optional[threading.Thread]:
    """Run startup() on a thread of its own and log one line. Never raises and never blocks the caller: the
    companion starts the same whether the site answers, refuses, or does not exist."""
    def work():
        try:
            r = for_settings(settings).startup()
            log("eyes reference: index version %d, %d ships; %s; recognition %s"
                % (r["version"], r["ships"], r["message"], RECOGNITION_NOTE.split(":")[0]))
        except Exception as ex:                   # noqa: BLE001
            try:
                log("eyes reference: not available (%s: %s)" % (type(ex).__name__, str(ex)[:120]))
            except Exception:                     # noqa: BLE001
                pass
    try:
        t = threading.Thread(target=work, name="eyes-reference", daemon=True)
        t.start()
        return t
    except Exception:                             # noqa: BLE001
        return None


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="the eyes' ship reference")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--refresh", action="store_true", help="ask the site for index.json now")
    ap.add_argument("--pull", metavar="TABLE", help="fetch one table and its row labels")
    ap.add_argument("--home", help="use this folder instead of the pilot's")
    ap.add_argument("--url", help="use this address instead of the built-in one; '' = offline")
    a = ap.parse_args(argv)
    store = ReferenceStore(home=Path(a.home) if a.home else None, base_url=a.url)
    rc = 0
    try:
        if a.refresh:
            print(json.dumps(store.startup(force=True), indent=1))
        if a.pull:
            plan = store.plan(a.pull)
            print("plan: %s to fetch, %s already here" % (mb(plan["to_fetch"]), mb(plan["on_disk"])))
            for kind in TABLE_KINDS:
                print("%s: %s" % (kind, store.table_file(a.pull, kind)))
        if a.status or not (a.refresh or a.pull):
            print(json.dumps(store.status(), indent=1))
    except ReferenceProblem as ex:
        print("could not: %s" % ex)
        rc = 1
    for t in store.transfers:
        print("GET %s  %d bytes  %.3f s  %s" % (t["url"], t["bytes"], t["seconds"], "ok" if t["ok"] else "FAILED"))
    print("%d request(s)" % len(store.requests))
    return rc


if __name__ == "__main__":
    sys.exit(main())
