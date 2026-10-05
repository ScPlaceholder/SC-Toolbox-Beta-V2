"""memory_store.py -- portable per-pilot memory store for the SC companion AI.

Two characters (Elah, the suit AI; Montaigne, the ship AI) share one memory store per
pilot. This module is the storage layer only: it has no opinion about character voice,
rhetoric, or realization. It exists to satisfy two principles from ARCHITECTURE.md:

  Principle 3 (deterministic data is untouchable) and Principle 4 (every proposition
  authorized for speech has provenance, traceable to the propositions that authorized
  it) -- so every memory here carries a `kind`, and an INTERPRETATION always carries
  the `confidence` and `grounds` that justify it. `grounds` are checked against real,
  already-recorded memory ids: a claim cannot cite evidence that does not exist.

  Roadmap item 8, "Portable memory": export/backup each pilot's Elah + Montaigne store
  so a lost machine is not a lost relationship. `export_pilot` / `import_pilot` /
  `snapshot` are that mechanism -- file-hash-verified, tamper-evident, and safe against
  a hostile or corrupt zip.

Storage layout, one folder per pilot under <root>/<pilot_id>/:
    memories.jsonl     one JSON object per line -- the claims (see `add_memory`)
    essays.jsonl        Montaigne's per-pilot essays, grounded in memory ids
    relationship.json   milestones + counters (hours_together, first_death_together, ...)
    callbacks.jsonl      things said, and when, so nothing repeats and callbacks are possible
    moves.json           rhetorical move lifecycle state + usage counts (anti-canned tracking)
    manifest.json        schema_version, pilot_id, created, updated, per-file sha256
    _snapshots/          timestamped exports, pruned to the newest `keep` (see `snapshot`)

Everything is stdlib only, Python 3.10+. Every on-disk write goes through
`_atomic_write_bytes` (write a temp file in the same directory, then `os.replace`), so a
crash mid-write cannot leave a half-written file in place.

Run `python memory_store.py --selftest` to exercise the whole module end to end.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from uuid import uuid4

# --------------------------------------------------------------------------------------
# Constants / schema
# --------------------------------------------------------------------------------------

CURRENT_SCHEMA_VERSION = 1

OWNERS = {"elah", "montaigne", "shared"}
KINDS = {"OBSERVED", "HISTORY", "INTERPRETATION", "UNKNOWN", "PILOT_SUBMISSION"}
MOVE_STATES = {"PROPOSED", "VALIDATED", "TRIAL", "PROMOTED", "RETIRED", "REJECTED"}

# The fixed set of per-pilot data files (manifest.json is tracked separately -- it
# describes these, it does not describe itself).
STORE_FILES = [
    "memories.jsonl",
    "essays.jsonl",
    "relationship.json",
    "callbacks.jsonl",
    "moves.json",
]

# Files that are part of a pilot's memory when they exist, and are simply absent when they do not: the
# conversation log and its summary nodes (tree_memory.py, "Christmas Tree Storage", 2026-10-05). They are listed
# in the manifest under "extra_files" with their own checksums, travel in the export zip, and are restored by
# import. A fixed list of names, so an archive cannot use this to write anywhere else. They are NOT written
# through the atomic helpers below: the log is unbounded, and _append_jsonl_atomic rewrites a whole file per line.
# A build from before 2026-10-05 imports such a zip without error and takes the five STORE_FILES only.
EXTRA_FILES = [
    "tree/log.jsonl",
    "tree/nodes_elah.jsonl",
    "tree/nodes_montaigne.jsonl",
]

_BAD_PILOT_ID_CHARS = set('\\/:*?"<>|')

_SNAPSHOT_RE = re.compile(r"^snapshot_(?P<pilot>.+)_(?P<ts>\d+)\.zip$")

__all__ = [
    "Store",
    "open_store",
    "add_memory",
    "query",
    "add_essay",
    "record_callback",
    "recent_callbacks",
    "update_relationship",
    "set_move_state",
    "export_pilot",
    "import_pilot",
    "migrate",
    "snapshot",
]


# --------------------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_pilot_id(pilot_id: str) -> None:
    if not pilot_id or not isinstance(pilot_id, str):
        raise ValueError(f"pilot_id must be a non-empty string, got {pilot_id!r}")
    if pilot_id in (".", "..") or any(c in _BAD_PILOT_ID_CHARS for c in pilot_id):
        raise ValueError(f"unsafe pilot_id: {pilot_id!r}")


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp{uuid4().hex}"
    with open(tmp, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)  # atomic on the same filesystem


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8"))


def _append_jsonl_atomic(path: Path, obj: dict) -> None:
    existing = path.read_bytes() if path.exists() else b""
    line = (json.dumps(obj, sort_keys=False, ensure_ascii=False) + "\n").encode("utf-8")
    _atomic_write_bytes(path, existing + line)


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    n = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                n += 1
    return n


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------------------
# Store handle
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Store:
    root: Path
    pilot_id: str
    dir: Path
    paths: dict[str, Path]  # keys: memories, essays, relationship, callbacks, moves, manifest


def _ensure_base_files(pilot_dir: Path) -> None:
    defaults = {
        "memories.jsonl": b"",
        "essays.jsonl": b"",
        "callbacks.jsonl": b"",
        "relationship.json": (json.dumps({"milestones": [], "counters": {}}, indent=2) + "\n").encode("utf-8"),
        "moves.json": (json.dumps({}, indent=2) + "\n").encode("utf-8"),
    }
    for fname, data in defaults.items():
        fpath = pilot_dir / fname
        if not fpath.exists():
            _atomic_write_bytes(fpath, data)


def _refresh_manifest(pilot_dir: Path, pilot_id: str) -> dict:
    """Recompute per-file checksums and rewrite manifest.json. Preserves `created`
    from the existing manifest if there is one (a refresh is not a rebirth)."""
    manifest_path = pilot_dir / "manifest.json"
    created = _now_iso()
    if manifest_path.exists():
        try:
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            created = old.get("created", created)
        except (json.JSONDecodeError, OSError):
            pass
    files = {}
    for fname in STORE_FILES:
        fpath = pilot_dir / fname
        if not fpath.exists():
            _atomic_write_bytes(fpath, b"")
        files[fname] = _sha256_file(fpath)
    extra = {name: _sha256_file(pilot_dir / name) for name in EXTRA_FILES if (pilot_dir / name).is_file()}
    manifest = {
        "schema_version": CURRENT_SCHEMA_VERSION,
        "pilot_id": pilot_id,
        "created": created,
        "updated": _now_iso(),
        "files": files,
    }
    if extra:
        manifest["extra_files"] = extra
    _atomic_write_json(manifest_path, manifest)
    return manifest


def open_store(root: str | Path, pilot_id: str) -> Store:
    """Open (creating if necessary) the memory store for one pilot. Idempotent: never
    overwrites files that already exist."""
    _validate_pilot_id(pilot_id)
    root = Path(root)
    pilot_dir = root / pilot_id
    pilot_dir.mkdir(parents=True, exist_ok=True)
    _ensure_base_files(pilot_dir)
    if not (pilot_dir / "manifest.json").exists():
        _refresh_manifest(pilot_dir, pilot_id)
    paths = {
        "memories": pilot_dir / "memories.jsonl",
        "essays": pilot_dir / "essays.jsonl",
        "relationship": pilot_dir / "relationship.json",
        "callbacks": pilot_dir / "callbacks.jsonl",
        "moves": pilot_dir / "moves.json",
        "manifest": pilot_dir / "manifest.json",
    }
    return Store(root=root, pilot_id=pilot_id, dir=pilot_dir, paths=paths)


def _known_memory_ids(store: Store) -> set[str]:
    return {r["id"] for r in _read_jsonl(store.paths["memories"])}


# --------------------------------------------------------------------------------------
# Memories
# --------------------------------------------------------------------------------------


def add_memory(
    store: Store,
    owner: str,
    kind: str,
    predicate: str,
    value: Any,
    *,
    confidence: float | None = None,
    grounds: Iterable[str] | None = None,
    text: str | None = None,
    created: str | None = None,
) -> dict:
    """Append one memory. Raises ValueError (nothing is written) if:
      - owner/kind is not a recognized value
      - kind == INTERPRETATION and confidence is None
      - confidence is given but not a number in [0, 1]
      - grounds references any memory id not already present in this store
    """
    if owner not in OWNERS:
        raise ValueError(f"unknown owner {owner!r}; must be one of {sorted(OWNERS)}")
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}; must be one of {sorted(KINDS)}")
    if kind == "INTERPRETATION" and confidence is None:
        raise ValueError("INTERPRETATION requires a confidence (0-1)")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError(f"confidence must be a number in [0, 1], got {confidence!r}")
        confidence = float(confidence)
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(f"confidence must be in [0, 1], got {confidence!r}")

    grounds_list = list(grounds) if grounds else []
    if grounds_list:
        known = _known_memory_ids(store)
        unknown = [g for g in grounds_list if g not in known]
        if unknown:
            raise ValueError(f"grounds reference unknown memory id(s): {unknown}")

    mem_path = store.paths["memories"]
    idx = _count_jsonl(mem_path) + 1
    obj = {
        "id": f"M{idx:06d}",
        "created": created or _now_iso(),
        "owner": owner,
        "kind": kind,
        "predicate": predicate,
        "value": value,
        "confidence": confidence,
        "grounds": grounds_list,
        "text": text,
    }
    _append_jsonl_atomic(mem_path, obj)
    return obj


def query(
    store: Store,
    *,
    owner: str | None = None,
    kind: str | None = None,
    predicate: str | None = None,
) -> list[dict]:
    """Return memories matching all given filters (None = don't filter on that field)."""
    rows = _read_jsonl(store.paths["memories"])

    def match(r: dict) -> bool:
        if owner is not None and r.get("owner") != owner:
            return False
        if kind is not None and r.get("kind") != kind:
            return False
        if predicate is not None and r.get("predicate") != predicate:
            return False
        return True

    return [r for r in rows if match(r)]


# --------------------------------------------------------------------------------------
# Essays (Montaigne's per-pilot essays)
# --------------------------------------------------------------------------------------


def add_essay(
    store: Store,
    title: str,
    body: str,
    *,
    grounds: Iterable[str] | None = None,
    created: str | None = None,
) -> dict:
    """Same provenance rule as add_memory: grounds must cite real memory ids."""
    grounds_list = list(grounds) if grounds else []
    if grounds_list:
        known = _known_memory_ids(store)
        unknown = [g for g in grounds_list if g not in known]
        if unknown:
            raise ValueError(f"essay grounds reference unknown memory id(s): {unknown}")

    path = store.paths["essays"]
    idx = _count_jsonl(path) + 1
    obj = {
        "id": f"E{idx:06d}",
        "created": created or _now_iso(),
        "title": title,
        "body": body,
        "grounds": grounds_list,
    }
    _append_jsonl_atomic(path, obj)
    return obj


# --------------------------------------------------------------------------------------
# Callbacks
# --------------------------------------------------------------------------------------


def record_callback(
    store: Store,
    text: str,
    *,
    kind: str | None = None,
    meta: dict | None = None,
    created: str | None = None,
) -> dict:
    path = store.paths["callbacks"]
    idx = _count_jsonl(path) + 1
    obj = {
        "id": f"CB{idx:06d}",
        "created": created or _now_iso(),
        "text": text,
        "kind": kind,
        "meta": meta or {},
    }
    _append_jsonl_atomic(path, obj)
    return obj


def recent_callbacks(store: Store, n: int = 10) -> list[dict]:
    """Most-recent-first, up to n."""
    rows = _read_jsonl(store.paths["callbacks"])
    rows.sort(key=lambda r: r.get("created", ""), reverse=True)
    return rows[:n]


# --------------------------------------------------------------------------------------
# Relationship
# --------------------------------------------------------------------------------------


def update_relationship(
    store: Store,
    *,
    milestone: dict | None = None,
    counters: dict[str, float] | None = None,
    **fields: Any,
) -> dict:
    """Merge updates into relationship.json.
      - milestone: appended (as-is, with `created` filled in if absent) to `milestones`.
      - counters: each value is ADDED to the existing counter (numeric accumulation,
        e.g. hours_together); a first-seen counter is initialized to that value.
      - any other keyword is set directly as a top-level field (last write wins).
    """
    path = store.paths["relationship"]
    data = _read_json(path, {"milestones": [], "counters": {}})
    data.setdefault("milestones", [])
    data.setdefault("counters", {})

    if milestone is not None:
        m = dict(milestone)
        m.setdefault("created", _now_iso())
        data["milestones"].append(m)

    if counters:
        for k, v in counters.items():
            existing = data["counters"].get(k)
            if isinstance(existing, (int, float)) and not isinstance(existing, bool) and isinstance(v, (int, float)):
                data["counters"][k] = existing + v
            else:
                data["counters"][k] = v

    for k, v in fields.items():
        data[k] = v

    data["updated"] = _now_iso()
    _atomic_write_json(path, data)
    return data


# --------------------------------------------------------------------------------------
# Rhetorical move lifecycle
# --------------------------------------------------------------------------------------

# PROPOSED -> VALIDATED -> TRIAL -> PROMOTED -> RETIRED, with REJECTED reachable from
# any pre-PROMOTED state and RETIRED reachable from PROMOTED or TRIAL (a move can be
# pulled back out of trial without ever being promoted).
_MOVE_TRANSITIONS = {
    "PROPOSED": {"VALIDATED", "REJECTED"},
    "VALIDATED": {"TRIAL", "REJECTED"},
    "TRIAL": {"PROMOTED", "RETIRED", "REJECTED"},
    "PROMOTED": {"RETIRED"},
    "RETIRED": set(),
    "REJECTED": set(),
}


def set_move_state(
    store: Store,
    move_id: str,
    state: str,
    *,
    note: str | None = None,
    increment_usage: bool = False,
    created: str | None = None,
) -> dict:
    """Create-or-transition a rhetorical move's lifecycle entry in moves.json.
    Raises ValueError on an unrecognized state or an illegal transition (see
    _MOVE_TRANSITIONS). Setting the same state again is a no-op transition (allowed)."""
    if state not in MOVE_STATES:
        raise ValueError(f"unknown move state {state!r}; must be one of {sorted(MOVE_STATES)}")

    path = store.paths["moves"]
    data = _read_json(path, {})
    now = created or _now_iso()
    entry = data.get(move_id)

    if entry is None:
        entry = {
            "move_id": move_id,
            "state": state,
            "usage_count": 0,
            "created": now,
            "updated": now,
            "history": [{"state": state, "at": now, "note": note}],
        }
    else:
        prev = entry["state"]
        if state != prev:
            allowed = _MOVE_TRANSITIONS.get(prev, set())
            if state not in allowed:
                raise ValueError(f"illegal move transition for {move_id!r}: {prev} -> {state}")
            entry["state"] = state
            entry["history"].append({"state": state, "at": now, "note": note})
        entry["updated"] = now

    if increment_usage:
        entry["usage_count"] = entry.get("usage_count", 0) + 1

    data[move_id] = entry
    _atomic_write_json(path, data)
    return entry


# --------------------------------------------------------------------------------------
# Export / import (portable memory)
# --------------------------------------------------------------------------------------


def export_pilot(root: str | Path, pilot_id: str, out_zip: str | Path) -> Path:
    """Refresh manifest checksums, then write a zip with a single top-level
    `<pilot_id>/` folder containing manifest.json + the store files. Atomic: builds a
    temp zip alongside the destination, then os.replace's it into place."""
    root = Path(root)
    pilot_dir = root / pilot_id
    if not pilot_dir.is_dir():
        raise FileNotFoundError(f"no such pilot store: {pilot_dir}")

    manifest = _refresh_manifest(pilot_dir, pilot_id)

    out_zip = Path(out_zip)
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_zip.parent / f".{out_zip.name}.tmp{uuid4().hex}"
    # The conversation log can gain a line at any moment. Each extra file is read ONCE, and the checksum in the
    # zip's manifest is the checksum of those bytes, so the archive always passes its own check.
    extra = {}
    for name in list(manifest.get("extra_files", {})):
        try:
            extra[name] = (pilot_dir / name).read_bytes()
        except OSError:
            del manifest["extra_files"][name]
            continue
        manifest["extra_files"][name] = hashlib.sha256(extra[name]).hexdigest()
    if "extra_files" in manifest and not manifest["extra_files"]:
        del manifest["extra_files"]
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{pilot_id}/manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for fname in STORE_FILES:
            zf.write(pilot_dir / fname, arcname=f"{pilot_id}/{fname}")
        for name, data in extra.items():
            zf.writestr(f"{pilot_id}/{name}", data)
    os.replace(tmp, out_zip)
    return out_zip


def _check_safe_zip_member(name: str) -> None:
    """Reject anything that could write outside the destination pilot folder."""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) >= 2 and normalized[1] == ":"):
        raise ValueError(f"unsafe zip entry (absolute path): {name!r}")
    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        raise ValueError(f"unsafe zip entry (path traversal): {name!r}")


def migrate(manifest: dict, pilot_dir: Path | None = None) -> dict:
    """Upgrade an older manifest (and, if a future migration needs it, the files in
    pilot_dir) up to CURRENT_SCHEMA_VERSION. Only schema_version 1 exists today, so
    this is a no-op pass-through -- but it is structured as a chain of registered
    steps so a future schema bump has one place to add itself.

    Register a new step by adding an entry to _MIGRATIONS: {from_version: fn}, where
    fn(manifest, pilot_dir) -> manifest with schema_version bumped by exactly one.
    """
    version = manifest.get("schema_version", CURRENT_SCHEMA_VERSION)
    seen = set()
    while version < CURRENT_SCHEMA_VERSION:
        if version in seen:
            raise ValueError(f"migration cycle detected at schema_version {version}")
        seen.add(version)
        step = _MIGRATIONS.get(version)
        if step is None:
            raise ValueError(
                f"no migration registered to upgrade schema_version {version} to {CURRENT_SCHEMA_VERSION}"
            )
        manifest = step(manifest, pilot_dir)
        version = manifest.get("schema_version", version)
    return manifest


# No migrations exist yet (only v1 is defined). Future entries: {1: _migrate_v1_to_v2, ...}
_MIGRATIONS: dict[int, Any] = {}


def import_pilot(zip_path: str | Path, root: str | Path, *, overwrite: bool = False) -> Path:
    """Validate and restore a pilot store from a zip made by export_pilot/snapshot.

    Refuses (raises ValueError / FileExistsError, extracts nothing) if:
      - any zip entry would escape the archive (absolute path or `..` component)
      - the zip does not contain exactly one top-level folder
      - manifest.json is missing, unreadable, or names an unsupported schema_version
      - the top-level folder name does not match manifest['pilot_id']
      - any store file's sha256 does not match the manifest (tampered or corrupt)
      - the destination pilot folder already exists and overwrite is not True

    On success, extraction happens into a temp folder first and is only swapped into
    place (os.replace) once every check has passed -- a failed or interrupted import
    never leaves a partial pilot folder behind.
    """
    zip_path = Path(zip_path)
    root = Path(root)

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = [n for n in zf.namelist() if not n.endswith("/")]
        if not names:
            raise ValueError("zip archive is empty")

        top_dirs = set()
        for name in names:
            _check_safe_zip_member(name)
            top_dirs.add(name.replace("\\", "/").split("/", 1)[0])

        if len(top_dirs) != 1:
            raise ValueError(f"zip must contain exactly one top-level pilot folder, found: {sorted(top_dirs)}")
        top = next(iter(top_dirs))

        manifest_name = f"{top}/manifest.json"
        if manifest_name not in names:
            raise ValueError("manifest.json not found in zip archive")
        try:
            manifest = json.loads(zf.read(manifest_name))
        except json.JSONDecodeError as e:
            raise ValueError(f"manifest.json in zip is not valid JSON: {e}") from e

        schema_version = manifest.get("schema_version")
        if not isinstance(schema_version, int) or schema_version < 1:
            raise ValueError(f"manifest has invalid schema_version: {schema_version!r}")
        if schema_version > CURRENT_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported schema_version {schema_version}; this build supports up to {CURRENT_SCHEMA_VERSION}"
            )

        pilot_id = manifest.get("pilot_id")
        if pilot_id != top:
            raise ValueError(f"pilot folder name {top!r} does not match manifest pilot_id {pilot_id!r}")
        _validate_pilot_id(pilot_id)

        files = manifest.get("files", {})
        for fname in STORE_FILES:
            expected = files.get(fname)
            if expected is None:
                raise ValueError(f"manifest is missing a checksum for {fname}")
            entry_name = f"{top}/{fname}"
            if entry_name not in names:
                raise ValueError(f"zip is missing file listed in manifest: {fname}")
            actual = hashlib.sha256(zf.read(entry_name)).hexdigest()
            if actual != expected:
                raise ValueError(f"checksum mismatch for {fname}: zip contents do not match manifest (tampered or corrupt)")

        extra = manifest.get("extra_files", {})
        if not isinstance(extra, dict):
            raise ValueError("manifest extra_files is not a mapping")
        for name, expected in extra.items():
            if name not in EXTRA_FILES:
                raise ValueError(f"manifest lists an extra file this build does not know: {name!r}")
            entry_name = f"{top}/{name}"
            if entry_name not in names:
                raise ValueError(f"zip is missing file listed in manifest: {name}")
            if hashlib.sha256(zf.read(entry_name)).hexdigest() != expected:
                raise ValueError(f"checksum mismatch for {name}: zip contents do not match manifest (tampered or corrupt)")

        dest = root / pilot_id
        if dest.exists() and not overwrite:
            raise FileExistsError(f"pilot store already exists at {dest}; pass overwrite=True to replace it")

        tmp_dest = root / f".import_{pilot_id}_{uuid4().hex}"
        tmp_dest.mkdir(parents=True)
        try:
            for fname in ["manifest.json"] + STORE_FILES:
                (tmp_dest / fname).write_bytes(zf.read(f"{top}/{fname}"))
            for name in extra:
                (tmp_dest / name).parent.mkdir(parents=True, exist_ok=True)
                (tmp_dest / name).write_bytes(zf.read(f"{top}/{name}"))
            migrate(manifest, tmp_dest)
            # Always leave a self-consistent manifest behind (schema_version current,
            # checksums matching whatever ended up on disk after migration).
            _refresh_manifest(tmp_dest, pilot_id)
        except Exception:
            shutil.rmtree(tmp_dest, ignore_errors=True)
            raise

    root.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        shutil.rmtree(dest)
    os.replace(tmp_dest, dest)
    return dest


# --------------------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------------------


def snapshot(root: str | Path, pilot_id: str, keep: int = 10) -> Path:
    """Write a timestamped export into <root>/<pilot_id>/_snapshots/, then prune down
    to the newest `keep` snapshots for this pilot. Pruning matches ONLY the exact
    filename pattern `snapshot_<pilot_id>_<digits>.zip`; any other file in
    _snapshots/ (or a snapshot belonging to a different pilot_id, which should not
    happen but is guarded anyway) is left untouched."""
    store = open_store(root, pilot_id)  # ensures the store exists without touching data
    snap_dir = store.dir / "_snapshots"
    snap_dir.mkdir(parents=True, exist_ok=True)

    ts = time.time_ns()
    while True:
        name = f"snapshot_{pilot_id}_{ts}.zip"
        path = snap_dir / name
        if not path.exists():
            break
        ts += 1  # guarantee uniqueness even under coarse clock resolution

    export_pilot(root, pilot_id, path)

    existing = []
    for f in snap_dir.iterdir():
        if not f.is_file():
            continue
        m = _SNAPSHOT_RE.match(f.name)
        if m and m.group("pilot") == pilot_id:
            existing.append((int(m.group("ts")), f))
    existing.sort(key=lambda t: t[0])  # oldest first

    excess = len(existing) - keep
    if excess > 0:
        for _, f in existing[:excess]:
            f.unlink()

    return path


# ========================================================================================
# Self-test
# ========================================================================================


def _make_tampered_zip(src: Path, dst: Path) -> None:
    """Copy a zip byte-for-byte at the archive-entry level, except flip a byte inside
    memories.jsonl -- the manifest (with the OLD checksum) is left untouched, so the
    result is a zip whose declared checksum no longer matches its content."""
    with zipfile.ZipFile(src, "r") as zin:
        items = {name: zin.read(name) for name in zin.namelist()}
    mem_name = next(n for n in items if n.endswith("/memories.jsonl"))
    tampered = bytearray(items[mem_name])
    if tampered:
        tampered[0] ^= 0xFF
    else:
        tampered = bytearray(b'{"tampered": true}\n')
    items[mem_name] = bytes(tampered)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in items.items():
            zout.writestr(name, data)


def _make_traversal_zip(src: Path, dst: Path) -> None:
    """Copy a valid zip and add one path-traversal entry."""
    with zipfile.ZipFile(src, "r") as zin:
        items = {name: zin.read(name) for name in zin.namelist()}
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in items.items():
            zout.writestr(name, data)
        zout.writestr("../evil.txt", b"pwned")


def _selftest() -> int:
    results: list[tuple[str, str]] = []

    def check(name: str, cond: bool) -> bool:
        status = "PASS" if cond else "FAIL"
        results.append((name, status))
        print(f"[{status}] {name}")
        return cond

    with tempfile.TemporaryDirectory(prefix="memstore_selftest_") as tmp_s:
        tmp = Path(tmp_s)
        root = tmp / "pilots"
        pilot_id = "PilotMoth"

        store = open_store(root, pilot_id)
        check("open_store creates the pilot directory", store.dir.is_dir())
        check(
            "open_store creates all base files",
            all((store.dir / f).exists() for f in STORE_FILES + ["manifest.json"]),
        )

        m1 = add_memory(
            store, owner="elah", kind="OBSERVED", predicate="ship.fuel", value="8%",
            text="Fuel reads 8 percent.",
        )
        m2 = add_memory(
            store, owner="montaigne", kind="INTERPRETATION", predicate="pilot.cautious",
            value=True, confidence=0.74, grounds=[m1["id"]],
            text="He hesitates at low fuel; I suspect prudence, or fear wearing its coat.",
        )
        check("add_memory assigns distinct ids", bool(m1["id"]) and bool(m2["id"]) and m1["id"] != m2["id"])

        check("query(owner=) filters correctly", [r["id"] for r in query(store, owner="montaigne")] == [m2["id"]])
        check("query(kind=) filters correctly", [r["id"] for r in query(store, kind="OBSERVED")] == [m1["id"]])
        check("query(predicate=) filters correctly", [r["id"] for r in query(store, predicate="ship.fuel")] == [m1["id"]])

        raised = False
        try:
            add_memory(store, owner="montaigne", kind="INTERPRETATION", predicate="x", value="y")
        except ValueError:
            raised = True
        check("INTERPRETATION without confidence is rejected", raised)
        check("...and nothing was written on that rejection", _count_jsonl(store.paths["memories"]) == 2)

        raised = False
        try:
            add_memory(store, owner="elah", kind="OBSERVED", predicate="x", value="y", grounds=["M999999"])
        except ValueError:
            raised = True
        check("grounds referencing an unknown memory id are rejected", raised)

        essay = add_essay(
            store, "Of the Pilot Who Cannot Leave a Box Behind",
            "He returns for the crate every time; I no longer ask why.",
            grounds=[m1["id"]],
        )
        check("add_essay writes an essay", essay["id"].startswith("E"))
        raised = False
        try:
            add_essay(store, "title", "body", grounds=["M999999"])
        except ValueError:
            raised = True
        check("essay grounds referencing an unknown memory id are rejected", raised)

        record_callback(store, "First quantum jump together.", kind="milestone")
        cb2 = record_callback(store, "Second mention of the crate.", kind="joke")
        rc = recent_callbacks(store, 1)
        check("recent_callbacks returns the most recent entry", len(rc) == 1 and rc[0]["id"] == cb2["id"])

        update_relationship(store, milestone={"name": "first_flight"}, counters={"hours_together": 3})
        update_relationship(store, counters={"hours_together": 2})
        rel = json.loads(store.paths["relationship"].read_text(encoding="utf-8"))
        check("update_relationship accumulates a numeric counter", rel["counters"].get("hours_together") == 5)
        check("update_relationship records a milestone", any(ms.get("name") == "first_flight" for ms in rel["milestones"]))

        set_move_state(store, "SELF_DEPRECATION", "PROPOSED")
        set_move_state(store, "SELF_DEPRECATION", "VALIDATED", increment_usage=True)
        set_move_state(store, "SELF_DEPRECATION", "TRIAL", increment_usage=True)
        moves = json.loads(store.paths["moves"].read_text(encoding="utf-8"))
        check(
            "set_move_state tracks lifecycle state + usage_count",
            moves["SELF_DEPRECATION"]["state"] == "TRIAL" and moves["SELF_DEPRECATION"]["usage_count"] == 2,
        )
        illegal = False
        try:
            set_move_state(store, "SELF_DEPRECATION", "PROPOSED")  # TRIAL -> PROPOSED is not a legal edge
        except ValueError:
            illegal = True
        check("an illegal move-state transition is rejected", illegal)

        export_zip = tmp / "export.zip"
        export_pilot(root, pilot_id, export_zip)
        check("export_pilot writes a zip", export_zip.exists())

        restored_root = tmp / "restored"
        dest = import_pilot(export_zip, restored_root)
        check("import_pilot restores a pilot directory", dest.is_dir())

        round_trip_ok = all((store.dir / f).read_bytes() == (dest / f).read_bytes() for f in STORE_FILES)
        check("export -> import round trip is byte-identical", round_trip_ok)

        tampered_zip = tmp / "tampered.zip"
        _make_tampered_zip(export_zip, tampered_zip)
        rejected = False
        try:
            import_pilot(tampered_zip, tmp / "restored_tampered")
        except ValueError:
            rejected = True
        check("a tampered zip (1 byte flipped in memories.jsonl) is rejected", rejected)
        check("...and nothing was extracted from the tampered zip", not (tmp / "restored_tampered" / pilot_id).exists())

        traversal_zip = tmp / "traversal.zip"
        _make_traversal_zip(export_zip, traversal_zip)
        rejected2 = False
        try:
            import_pilot(traversal_zip, tmp / "restored_traversal")
        except ValueError:
            rejected2 = True
        check("a zip containing '../evil.txt' is rejected", rejected2)
        check("...and nothing escaped onto disk", not (tmp / "evil.txt").exists() and not (tmp.parent / "evil.txt").exists())

        no_overwrite = False
        try:
            import_pilot(export_zip, restored_root, overwrite=False)
        except FileExistsError:
            no_overwrite = True
        check("import_pilot refuses to clobber an existing pilot folder without overwrite=True", no_overwrite)

        snap_dir = store.dir / "_snapshots"
        snap_dir.mkdir(parents=True, exist_ok=True)
        sentinel = snap_dir / "notes.txt"
        sentinel.write_text("keep me", encoding="utf-8")
        for _ in range(5):
            snapshot(root, pilot_id, keep=3)
        snap_files = sorted(p.name for p in snap_dir.glob(f"snapshot_{pilot_id}_*.zip"))
        check("snapshot pruning keeps exactly `keep` snapshots", len(snap_files) == 3)
        check(
            "snapshot pruning leaves a non-snapshot file in _snapshots/ untouched",
            sentinel.exists() and sentinel.read_text(encoding="utf-8") == "keep me",
        )
        # oldest-first pruning: the surviving files should be the 3 most recent timestamps
        all_ts = sorted(int(_SNAPSHOT_RE.match(p.name).group("ts")) for p in snap_dir.glob(f"snapshot_{pilot_id}_*.zip"))
        check("surviving snapshots are the newest ones (oldest pruned first)", all_ts == sorted(all_ts))

    passed = sum(1 for _, s in results if s == "PASS")
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    return 0 if passed == total else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selftest", action="store_true", help="run the self-test suite and exit")
    args = parser.parse_args(argv)
    if args.selftest:
        return _selftest()
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
