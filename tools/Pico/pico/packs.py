"""Outfit packs: each outfit's loops in ONE file, fetched when first picked, kept, never fetched twice.

No Qt here. sprite_pal.py draws the progress bar; everything that decides is in this file and is tested
without a window (tests/test_pico_packs.py).

WHAT A PACK IS
    <code>.tar.xz: a tar archive, xz-compressed as one stream, read with the standard library (tarfile, lzma).
    Flat members: index.json, <loop>.webp, <loop>.anchors.json. The loops of one outfit share frames, so one
    solid stream packs about 21 MB of loops into about 13 MB; a zip of the same files saves nothing.
    Unpacking one outfit takes about half a second, so a pack is unpacked whole and Catalog reads a folder,
    exactly as it does in the developer's loop folders.

WHERE THINGS LIVE
    <install>/tools/Pico/packs/packs.json        the list the toolbox was built with
    <install>/tools/Pico/packs/<code>.tar.xz     the outfit that ships with the toolbox (read in place)
                                                 (the environment variable PICO_PACKS_DIR moves this folder)
    %APPDATA%/PicoPal/packs/<code>.tar.xz        packs fetched on this PC, or copied in by hand
    %APPDATA%/PicoPal/packs/packs.json           the newest list the pack server gave
    %APPDATA%/PicoPal/packs/have.json            the record: which packs are on this PC (see RECORD)
    %APPDATA%/PicoPal/worn/<code>/<loop folder>/ the outfit being worn, unpacked

THE RULES
    * Picking an outfit looks on disk first. Pack there and its checksum right: use it, request nothing.
      Missing or damaged: fetch it. Nothing is requested at start-up when the worn outfit's pack is there.
    * A download is written to <name>.part and renamed only after its sha256 matches the list, so a half
      download or a wrong file is never mistaken for a pack.
    * https only (PackStore refuses anything else, redirects included). The only requests ever made are for
      packs.json and for pack files, under the one base URL. Nothing is sent about the user or the PC.
    * An empty PACKS_URL means OFFLINE: only the shipped pack and packs already on disk are used.
    * Only the worn outfit is unpacked. Switching removes the previous outfit's unpacked files; its pack
      stays, so switching back needs no internet. EXCEPT when every pack in the list is on this PC: then
      nothing unpacked is removed (see KEEPING).
    * Every failure is a PackError with a sentence for the user. Callers keep the current outfit.

RECORD (have.json)
    One row per pack in the user's folder: file, sha256, bytes, mtime, when and how it got here, and the list
    version it was checked against. It is a memory, never the evidence: reconcile() at start compares it with
    the disk. A recorded pack whose file is gone is dropped. A file whose size and modified time are what the
    record says is trusted for LISTING without being read (19 packs, 261 MB: reading them all costs about
    0.15 s from the disk cache and seconds from a cold disk). A file the record does not know, or whose size
    or time differ, is checksummed in full: right, it is adopted (a pack copied in by hand is not downloaded
    again); wrong, it is dropped from the record and offered for download. Before a pack is WORN its sha256
    is always computed in full (about 6 ms), whatever the record says.

KEEPING
    While some pack in the list is not on this PC, switching outfits removes the previous outfit's unpacked
    folder. Once every pack is here, nothing is removed any more: an outfit is unpacked the first time it is
    worn and stays ready (at most about 426 MB for all 19, on top of 261 MB of packs). Outfits are not all
    unpacked at the moment the set becomes complete; that would write 426 MB nobody asked for. Each folder
    left in place while the set was complete is marked "keep", and a kept folder is never removed later, so a
    newer list that adds an outfit (the set is incomplete again) takes nothing away; removal applies only to
    outfits unpacked from then on.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# THE ONE ADDRESS. The folder on the pack server that holds packs.json and the <code>.tar.xz files.
# The site went up on 2026-10-06 (all 19 packs, checked from the public address). Empty would mean offline.
# A "packs_url" in settings.json overrides it (sprite_pal.py); set that to "" to stay offline.
PACKS_URL = "https://pico-pals.pages.dev/packs"

MANIFEST_NAME = "packs.json"
RECORD_NAME = "have.json"
MARKER_NAME = ".pack.json"
FORMAT = 1
LOOP_FOLDER = "pico_anim_sequences"     # sprites.DEFAULT_DIR.name: outfit_name() and brand_allows() read it
BASE_OUTFIT = "drake"                   # the outfit whose folder has no _<brand> suffix
PREFIX = "pack:"                        # settings.json "outfit": "pack:o08" = a pack; anything else = a folder
MAX_PACK_BYTES = 256 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
TIMEOUT_S = 20
# The pack host refuses Python's default agent ("Python-urllib/x.y") with HTTP 403, so every request names
# the toolbox instead. It identifies the program only: no version of the user's system, no id.
USER_AGENT = "SC-Toolbox-PicoPals/1"
CHUNK = 128 * 1024

# Where the toolbox keeps the pack it ships with. PICO_PACKS_DIR moves it: for the tests, and for an
# installer that would rather not put a 13 MB file inside the tool's folder.
BUNDLED_DIR = Path(os.environ.get("PICO_PACKS_DIR") or Path(__file__).resolve().parent.parent / "packs")

_CODE = re.compile(r"^[a-z0-9]{1,16}$")
_FILE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_OUTFIT = re.compile(r"^[a-z0-9_]{1,32}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_MEMBER = re.compile(r"^[A-Za-z0-9_.+-]{1,120}$")
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class PackError(Exception):
    """Something a pack could not do. str() is a sentence the user can be shown."""


class PackCancelled(PackError):
    """The user pressed Cancel."""


def home_dir() -> Path:
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "PicoPal"


def mb(n: int) -> str:
    return "%.1f MB" % (n / 1e6)


@dataclass(frozen=True)
class Entry:
    code: str
    name: str            # shown to the user: "Drake"
    outfit: str          # the loop folder's suffix: "drake"
    pack: str            # file name: "o08.tar.xz"
    bytes: int
    sha256: str
    loops: int
    unpacked_bytes: int

    @property
    def folder(self) -> str:
        return LOOP_FOLDER if self.outfit == BASE_OUTFIT else "%s_%s" % (LOOP_FOLDER, self.outfit)


@dataclass(frozen=True)
class Manifest:
    version: str
    default: Optional[str]
    entries: dict                # code -> Entry, in the list's order

    @classmethod
    def empty(cls) -> "Manifest":
        return cls("", None, {})


def parse_manifest(obj) -> Manifest:
    """A checked Manifest from decoded JSON. Anything odd is a PackError: names from a server are never
    used as paths until they have passed the patterns above."""
    if not isinstance(obj, dict) or obj.get("format") != FORMAT or not isinstance(obj.get("outfits"), list):
        raise PackError("the outfit list is not one this version of Pico understands")
    entries = {}
    for o in obj["outfits"]:
        try:
            e = Entry(code=str(o["code"]), name=str(o["name"]), outfit=str(o["outfit"]), pack=str(o["pack"]),
                      bytes=int(o["bytes"]), sha256=str(o["sha256"]).lower(), loops=int(o.get("loops", 0)),
                      unpacked_bytes=int(o.get("unpacked_bytes", 0)))
        except (KeyError, TypeError, ValueError):
            raise PackError("the outfit list has a row that cannot be read")
        if not (_CODE.match(e.code) and _FILE.match(e.pack) and _OUTFIT.match(e.outfit) and _SHA.match(e.sha256)
                and 0 < e.bytes <= MAX_PACK_BYTES and 0 < len(e.name) <= 40) or e.code in entries:
            raise PackError("the outfit list has a row that is not allowed (%s)" % e.code[:16])
        entries[e.code] = e
    default = obj.get("default")
    return Manifest(str(obj.get("version", "")), default if default in entries else None, entries)


def sha256_of(path: Path) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


class _SameRules(urllib.request.HTTPRedirectHandler):
    """A redirect is followed only to an address the store would have asked for itself."""

    def __init__(self, store: "PackStore"):
        self.store = store

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.store.check_url(newurl)
        self.store.requests.append(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class PackStore:
    def __init__(self, home: Optional[Path] = None, bundled: Optional[Path] = None,
                 base_url: Optional[str] = None, allow_loopback_http: bool = False, clock=time.time):
        self.home = Path(home) if home is not None else home_dir()
        self.packs_dir = self.home / "packs"
        self.worn_dir = self.home / "worn"
        self.bundled_dir = Path(bundled) if bundled is not None else BUNDLED_DIR
        self.base_url = (PACKS_URL if base_url is None else base_url or "").strip().rstrip("/")
        # Tests serve packs from http://127.0.0.1. Nothing in the app passes True.
        self.allow_loopback_http = allow_loopback_http
        self.clock = clock
        self.requests: list = []          # every address asked for, in order
        self.bundled_manifest = self._read_manifest(self.bundled_dir / MANIFEST_NAME)
        self.manifest = self._read_manifest(self.packs_dir / MANIFEST_NAME)
        if not self.manifest.entries:
            self.manifest = self.bundled_manifest
        self.record = self._read_record()

    # ---- the list -------------------------------------------------------------------------------------
    @staticmethod
    def _read_manifest(path: Path) -> Manifest:
        try:
            return parse_manifest(json.loads(Path(path).read_text(encoding="utf-8")))
        except (OSError, ValueError, PackError):
            return Manifest.empty()

    @property
    def online(self) -> bool:
        return bool(self.base_url)

    def entry(self, code: str) -> Entry:
        e = self.manifest.entries.get(code)
        if e is None:
            raise PackError("there is no outfit called %s in the outfit list" % str(code)[:16])
        return e

    def name(self, code: Optional[str]) -> str:
        e = self.manifest.entries.get(code) or self.bundled_manifest.entries.get(code)
        return e.name if e else "his outfit"

    def default_code(self) -> Optional[str]:
        """The outfit that ships with the toolbox, if its pack is really there; else any pack on disk."""
        for code in (self.bundled_manifest.default, self.manifest.default):
            if code and self.present(code):
                return code
        return next((c for c in self.manifest.entries if self.present(c)), None)

    # ---- the record -----------------------------------------------------------------------------------
    def _read_record(self) -> dict:
        try:
            d = json.loads((self.packs_dir / RECORD_NAME).read_text(encoding="utf-8"))
            rows = d.get("packs", {})
            return {c: r for c, r in rows.items() if isinstance(r, dict) and _CODE.match(str(c))}
        except (OSError, ValueError, AttributeError):
            return {}

    def _write_record(self) -> None:
        try:
            self.packs_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.packs_dir / (RECORD_NAME + ".tmp")
            tmp.write_text(json.dumps({"format": FORMAT, "packs": self.record}, indent=1), encoding="utf-8")
            os.replace(tmp, self.packs_dir / RECORD_NAME)
        except OSError:
            pass                              # a read-only profile must not take Pico down

    def _remember(self, code: str, path: Path, sha: str, origin: str) -> None:
        st = path.stat()
        old = self.record.get(code, {})
        same = old.get("sha256") == sha
        new = {"file": path.name, "sha256": sha, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns,
               "fetched": old.get("fetched") if same else round(self.clock()),
               "origin": old.get("origin", origin) if same else origin,
               "version": self.manifest.version}
        if new != old:
            self.record[code] = new
            self._write_record()

    def _forget(self, code: str) -> None:
        if self.record.pop(code, None) is not None:
            self._write_record()

    def _quick(self, code: str, path: Path) -> Optional[str]:
        """The sha256 the record holds for this file, if the file is still the size and age recorded."""
        r = self.record.get(code)
        try:
            st = path.stat()
        except OSError:
            return None
        if r and r.get("file") == path.name and r.get("bytes") == st.st_size and r.get("mtime_ns") == st.st_mtime_ns:
            return r.get("sha256")
        return None

    def reconcile(self) -> dict:
        """Make the record agree with the disk. Run at start. {"kept", "adopted", "dropped"}: lists of codes."""
        out = {"kept": [], "adopted": [], "dropped": []}
        changed = False
        for code in sorted(set(self.record) | set(self.manifest.entries)):
            e = self.manifest.entries.get(code)
            name = e.pack if e else str(self.record[code].get("file", ""))
            path = self.packs_dir / name if _FILE.match(name) else None
            if path is None or not path.is_file():
                if code in self.record:
                    del self.record[code]
                    out["dropped"].append(code)
                    changed = True
                continue
            if self._quick(code, path) is not None:
                out["kept"].append(code)
                continue
            sha = sha256_of(path)                 # first sight, or the file changed: read all of it
            if e is not None and sha == e.sha256:
                known = code in self.record
                self._remember(code, path, sha, "adopted")
                out["kept" if known else "adopted"].append(code)
            elif code in self.record:
                del self.record[code]
                out["dropped"].append(code)
                changed = True
        if changed:
            self._write_record()
        return out

    # ---- is it here -----------------------------------------------------------------------------------
    def _bundled(self, code: str) -> Optional[Path]:
        e = self.bundled_manifest.entries.get(code)
        if e is None:
            return None
        p = self.bundled_dir / e.pack
        return p if p.is_file() else None

    def present(self, code: str) -> bool:
        """Quick, for lists: the pack is on this PC as far as size and modified time say. Reads no pack."""
        e = self.manifest.entries.get(code)
        if e is not None and self._quick(code, self.packs_dir / e.pack) == e.sha256:
            return True
        b = self._bundled(code)
        be = self.bundled_manifest.entries.get(code)
        try:
            return b is not None and b.stat().st_size == be.bytes
        except OSError:
            return False

    def pack_path(self, code: str) -> Optional[Path]:
        """The pack file to unpack for this outfit, its sha256 computed now and found right; else None.
        A pack in the user's folder that turns out wrong is dropped from the record."""
        e = self.manifest.entries.get(code)
        if e is not None:
            p = self.packs_dir / e.pack
            if p.is_file():
                sha = sha256_of(p)
                if sha == e.sha256:
                    self._remember(code, p, sha, "adopted")
                    return p
                r = self.record.get(code)
                if not (r and r.get("sha256") == sha):
                    self._forget(code)
        b, be = self._bundled(code), self.bundled_manifest.entries.get(code)
        if b is not None and sha256_of(b) == be.sha256:
            return b
        return None

    def older_pack(self, code: str) -> Optional[Path]:
        """A whole pack that the newest list has since replaced: the file is exactly what the record says
        was once checked. Worn only when the new one cannot be fetched."""
        r = self.record.get(code)
        name = str((r or {}).get("file", ""))
        if not r or not _FILE.match(name):
            return None
        p = self.packs_dir / name
        return p if p.is_file() and sha256_of(p) == r.get("sha256") else None

    def have(self, code: str) -> bool:
        return self.pack_path(code) is not None

    def complete(self) -> bool:
        """Every outfit in the list has its pack on this PC."""
        return bool(self.manifest.entries) and all(self.present(c) for c in self.manifest.entries)

    def missing(self) -> list:
        return [c for c in self.manifest.entries if not self.present(c)]

    def listing(self) -> list:
        """One row per outfit for the picker: (code, name, state, bytes). state is "included" (shipped with
        the toolbox), "downloaded", "available" (can be fetched now) or "unavailable" (offline)."""
        rows = []
        for code, e in self.manifest.entries.items():
            if self.present(code):
                user = self._quick(code, self.packs_dir / e.pack) == e.sha256
                state = "downloaded" if user else "included"
            else:
                state = "available" if self.online else "unavailable"
            rows.append((code, e.name, state, e.bytes))
        return rows

    # ---- the network ----------------------------------------------------------------------------------
    def check_url(self, url: str) -> None:
        u = urllib.parse.urlsplit(url)
        if u.scheme == "https" and u.hostname:
            return
        if self.allow_loopback_http and u.scheme == "http" and u.hostname in _LOOPBACK:
            return
        raise PackError("the pack address is not an https address, so nothing was downloaded")

    def _open(self, name: str):
        if not self.online:
            raise PackError("online packs are not set up yet, so nothing can be downloaded")
        url = "%s/%s" % (self.base_url, name)
        self.check_url(url)
        self.requests.append(url)
        opener = urllib.request.build_opener(_SameRules(self))
        try:
            return opener.open(urllib.request.Request(url, headers={"User-Agent": USER_AGENT}), timeout=TIMEOUT_S)
        except PackError:
            raise
        except urllib.error.HTTPError as ex:
            raise PackError("the pack server answered with an error (%s)" % ex.code)
        except Exception as ex:                   # noqa: BLE001 - no network fault may reach Pico's window
            raise PackError("the pack server could not be reached (%s)" % _reason(ex))

    def refresh_manifest(self) -> Manifest:
        """Ask the server for packs.json (one request) and keep it. The old list stays if this fails."""
        with self._open(MANIFEST_NAME) as r:
            try:
                raw = r.read(MAX_MANIFEST_BYTES + 1)
            except Exception as ex:               # noqa: BLE001
                raise PackError("the outfit list did not arrive (%s)" % _reason(ex))
        if len(raw) > MAX_MANIFEST_BYTES:
            raise PackError("the outfit list from the server is too big to be real")
        try:
            man = parse_manifest(json.loads(raw.decode("utf-8")))
        except ValueError:
            raise PackError("the outfit list from the server cannot be read")
        try:
            self.packs_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.packs_dir / (MANIFEST_NAME + ".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, self.packs_dir / MANIFEST_NAME)
        except OSError:
            pass
        self.manifest = man
        return man

    def _download(self, e: Entry, part: Path, progress, cancel, index: int, count: int) -> str:
        """Stream one pack into `part`. Its sha256."""
        h = hashlib.sha256()
        done = 0
        with self._open(e.pack) as r:
            try:
                with open(part, "wb") as f:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise PackCancelled("cancelled")
                        chunk = r.read1(CHUNK)          # what has arrived, so the bar moves on a slow line
                        if not chunk:
                            break
                        done += len(chunk)
                        if done > MAX_PACK_BYTES:
                            raise PackError("the server sent far more than a pack")
                        h.update(chunk)
                        f.write(chunk)
                        if progress is not None:
                            progress(e.code, done, e.bytes, index, count)
            except PackError:
                raise
            except OSError as ex:
                if isinstance(ex, (urllib.error.URLError, TimeoutError, ConnectionError)):
                    raise PackError("the download stopped part way (%s)" % _reason(ex))
                raise PackError("the pack could not be saved on this PC (%s)" % _reason(ex))
            except Exception as ex:               # noqa: BLE001
                raise PackError("the download stopped part way (%s)" % _reason(ex))
        return h.hexdigest()

    def fetch(self, code: str, progress: Optional[Callable] = None, cancel=None,
              index: int = 1, count: int = 1) -> Path:
        """Download one pack. Normally ONE request. If what arrives does not match the list in hand, the
        list is asked for once (the packs may have been rebuilt): the download is kept if it matches the
        new list and thrown away if it does not. Raises PackError; a failed fetch leaves no pack behind."""
        e = self.entry(code)
        try:
            self.packs_dir.mkdir(parents=True, exist_ok=True)
        except OSError as ex:
            raise PackError("the pack folder cannot be made on this PC (%s)" % _reason(ex))
        part = self.packs_dir / (e.pack + ".part")
        try:
            try:
                sha = self._download(e, part, progress, cancel, index, count)
            except PackCancelled:
                raise
            except PackError as first:
                # A pack the list names but the server does not have: the list in hand may be old.
                if "error (404)" not in str(first):
                    raise
                old = e
                e = self.refresh_manifest().entries.get(code)
                if e is None or (e.pack, e.sha256) == (old.pack, old.sha256):
                    raise first
                part = self.packs_dir / (e.pack + ".part")
                sha = self._download(e, part, progress, cancel, index, count)
            if sha != e.sha256:
                try:
                    fresh = self.refresh_manifest().entries.get(code)
                except PackError:
                    fresh = None
                if fresh is None or fresh.sha256 != sha or fresh.pack != e.pack:
                    raise PackError("the %s pack that arrived is not the one in the outfit list "
                                    "(checksum mismatch), so it was thrown away" % e.name)
                e = fresh
            final = self.packs_dir / e.pack
            os.replace(part, final)               # only now is it a pack
            self._remember(code, final, sha, "fetched")
            return final
        except OSError as ex:
            raise PackError("the pack could not be saved on this PC (%s)" % _reason(ex))
        finally:
            for p in {part, self.packs_dir / (e.pack + ".part") if e else part}:
                try:
                    p.unlink()
                except OSError:
                    pass

    def fetch_all(self, progress: Optional[Callable] = None, cancel=None) -> dict:
        """"Download every Pal now". The list is refreshed first (one request), then every pack not on this
        PC is fetched; packs already here are skipped and cost no request. Stops at the first failure: the
        packs fetched so far are kept. {"fetched", "skipped", "failed": {code: sentence}, "left": [codes]}."""
        out = {"fetched": [], "skipped": [], "failed": {}, "left": []}
        if not self.online:
            raise PackError("online packs are not set up yet, so nothing can be downloaded")
        self.refresh_manifest()
        self.reconcile()
        codes = list(self.manifest.entries)
        todo = [c for c in codes if not self.have(c)]
        out["skipped"] = [c for c in codes if c not in todo]
        for i, code in enumerate(todo):
            try:
                self.fetch(code, progress, cancel, index=i + 1, count=len(todo))
                out["fetched"].append(code)
            except PackCancelled:
                out["left"] = todo[i:]
                out["cancelled"] = True
                break
            except PackError as ex:
                out["failed"][code] = str(ex)
                out["left"] = todo[i + 1:]
                break
        return out

    # ---- wearing --------------------------------------------------------------------------------------
    def _marker(self, code: str) -> dict:
        try:
            return json.loads((self.worn_dir / code / MARKER_NAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_marker(self, folder: Path, d: dict) -> None:
        (folder / MARKER_NAME).write_text(json.dumps(d), encoding="utf-8")

    def code_of(self, loop_folder) -> Optional[str]:
        """The pack code a loop folder was unpacked from, or None for any other folder."""
        try:
            rel = Path(loop_folder).resolve().relative_to(self.worn_dir.resolve())
        except (OSError, ValueError):
            return None
        return rel.parts[0] if len(rel.parts) == 2 and _CODE.match(rel.parts[0]) else None

    def unpacked(self) -> list:
        try:
            return sorted(d.name for d in self.worn_dir.iterdir() if d.is_dir() and _CODE.match(d.name))
        except OSError:
            return []

    def activate(self, code: str) -> Path:
        """Unpack this outfit (unless it already is) and return its loop folder for Catalog.scan.
        Never asks the network for anything."""
        pack = self.pack_path(code) or self.older_pack(code)
        if pack is None:
            raise PackError("the %s pack is not on this PC" % self.name(code))
        e = self.manifest.entries.get(code) or self.bundled_manifest.entries.get(code)
        if pack.parent == self.bundled_dir and code in self.bundled_manifest.entries:
            e = self.bundled_manifest.entries[code]
        sha = sha256_of(pack)
        target = self.worn_dir / code
        loops = target / e.folder
        if self._marker(code).get("sha256") == sha and loops.is_dir():
            return loops
        new = self.worn_dir / (code + ".new")
        try:
            shutil.rmtree(new, ignore_errors=True)
            (new / e.folder).mkdir(parents=True)
            n = 0
            with tarfile.open(pack, mode="r:xz") as tar:
                for m in tar:
                    # flat, plain files with plain names only: nothing in a pack chooses where it lands
                    if not m.isfile() or not _MEMBER.match(m.name) or m.name.startswith("."):
                        raise PackError("the %s pack holds something that is not a loop" % e.name)
                    if not m.name.endswith((".webp", ".json")):
                        raise PackError("the %s pack holds something that is not a loop" % e.name)
                    src = tar.extractfile(m)
                    with open(new / e.folder / m.name, "wb") as f:
                        shutil.copyfileobj(src, f)
                    n += m.name.endswith(".webp")
            if not n:
                raise PackError("the %s pack is empty" % e.name)
            self._write_marker(new, {"sha256": sha, "keep": bool(self._marker(code).get("keep"))})
            if not self._remove(target):
                raise PackError("the old %s files are still in use" % e.name)
            os.replace(new, target)
        except PackError:
            shutil.rmtree(new, ignore_errors=True)
            raise
        except Exception as ex:                   # noqa: BLE001 - a damaged archive, a full disk
            shutil.rmtree(new, ignore_errors=True)
            raise PackError("the %s pack could not be unpacked (%s)" % (e.name, _reason(ex)))
        return loops

    def wear(self, code: str, progress: Optional[Callable] = None, cancel=None) -> Path:
        """Everything picking an outfit needs: use the pack on disk, or fetch it, then unpack it. The loop
        folder. On PackError nothing about the outfit already worn has been touched."""
        if not self.have(code):
            try:
                self.fetch(code, progress, cancel)
            except PackCancelled:
                raise
            except PackError:
                if self.older_pack(code) is None:
                    raise
        return self.activate(code)

    def keep(self, code: str) -> None:
        d = self._marker(code)
        if d and not d.get("keep"):
            d["keep"] = True
            try:
                self._write_marker(self.worn_dir / code, d)
            except OSError:
                pass

    def deactivate(self, code: Optional[str]) -> bool:
        """The outfit just taken off. True if its unpacked files were removed. They are left alone, and
        marked to be kept from now on, while every pack is on this PC. The pack file is never touched."""
        if not code or not _CODE.match(code) or not (self.worn_dir / code).is_dir():
            return False
        if self.complete():
            self.keep(code)
            return False
        if self._marker(code).get("keep"):
            return False
        return self._remove(self.worn_dir / code)

    def _remove(self, folder: Path) -> bool:
        """Remove an unpacked outfit, all of it or none of it. The folder is renamed first: Windows refuses
        that while any loop in it is still open, and then nothing has been deleted and it can be asked
        again. (Deleting file by file would leave half an outfit behind a loop that was still playing.)"""
        if not folder.exists():
            return True
        gone = folder.with_name(folder.name + ".old")
        try:
            shutil.rmtree(gone, ignore_errors=True)
            os.replace(folder, gone)
        except OSError:
            return False
        shutil.rmtree(gone, ignore_errors=True)
        return True

    def sweep(self, active: Optional[str] = None) -> list:
        """At start: leftovers of an unpack that never finished go, and every unpacked outfit except the
        worn one is taken off (deactivate's rules). What was removed."""
        gone = []
        try:
            for d in self.worn_dir.iterdir():
                if d.is_dir() and d.name.endswith((".new", ".old")):
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            return gone
        if active and self.complete():
            self.keep(active)
        for code in self.unpacked():
            if code != active and self.deactivate(code):
                gone.append(code)
        return gone


def _reason(ex: BaseException) -> str:
    r = getattr(ex, "reason", None)
    text = str(r if r is not None else ex) or ex.__class__.__name__
    return text[:120]
